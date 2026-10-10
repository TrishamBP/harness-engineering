"""Regression tests for the hardening pass: each test pins one defect that was
found by review and reproduced before it was fixed."""

import asyncio
import contextlib
import io
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from helpers import make_executor, workspace

from harness.app import HarnessConfig, build_harness
from harness.core.events import EventKind, EventLog
from harness.extensions.hooks import HookBus, HookEvent, HookResult
from harness.extensions.skills import SkillRegistry
from harness.interface.cli import tree
from harness.llm.anthropic_provider import to_wire
from harness.llm.provider import ModelResponse, ScriptedModel, SystemPrompt, call
from harness.orchestration.worker import BUILTIN_AGENTS
from harness.safety.approval import ApproveAll, DenyAll
from harness.safety.policy import Mode, PolicyEngine, Rule
from harness.safety.sandbox import CommandRunner
from harness.tools.permissions import Decision, PermissionLevel
from harness.verification.engine import DiffValidator

EXEC = PermissionLevel.EXECUTE


class HardlineFloorTest(unittest.TestCase):
    def test_floor_sees_through_paths_case_wrappers_and_spelling(self):
        yolo = PolicyEngine(mode=Mode.YOLO)
        for cmd in ["/bin/rm -rf /", "env rm -rf /", "env FOO=1 rm -rf /", "nice -n 5 sudo id",
                    "timeout 10 shutdown -h now", "SHUTDOWN.EXE /s", "/sbin/mkfs.ext4 /dev/sda",
                    "rm -rf ./", "rm -rf ../", "rm -rf /.", "rm --recursive /", "chmod 0777 x", "chmod 1777 x"]:
            v = yolo.evaluate("run_command", EXEC, {"command": cmd})
            self.assertEqual((v.decision, v.rule), (Decision.DENY, "hardline"), cmd)

    def test_floor_does_not_overreach(self):
        yolo = PolicyEngine(mode=Mode.YOLO)
        for cmd in ["rm -rf build", "env python -m unittest", "chmod 644 x", "nice python x.py"]:
            self.assertEqual(yolo.evaluate("run_command", EXEC, {"command": cmd}).decision, Decision.ALLOW, cmd)

    def test_unbalanced_quote_is_denied_not_raised(self):
        v = PolicyEngine(mode=Mode.YOLO).evaluate("run_command", EXEC, {"command": 'python -c "print(1)'})
        self.assertEqual((v.decision, v.rule), (Decision.DENY, "parse"))

    @unittest.skipUnless(os.name == "nt", "Windows path semantics")
    def test_backslashes_survive_parsing_on_windows(self):
        from harness.safety.policy import parse_command
        self.assertEqual(parse_command(r"C:\Python\python.exe -m x"), [r"C:\Python\python.exe", "-m", "x"])

    def test_ask_rule_beats_allow_rule_in_any_order(self):
        ask = Rule("ask-tests", Decision.ASK, tool="run_command", command_prefix=("pytest",))
        allow = Rule("allow-tests", Decision.ALLOW, tool="run_command", command_prefix=("pytest",))
        for rules in [(ask, allow), (allow, ask)]:
            v = PolicyEngine(mode=Mode.AUTO_EDIT, rules=rules).evaluate("run_command", EXEC, {"command": "pytest"})
            self.assertEqual(v.decision, Decision.ASK)


class PathPolicyTest(unittest.IsolatedAsyncioTestCase):
    async def test_protected_paths_judged_after_resolution_and_case_folding(self):
        ws = workspace({".git/config": "[core]\n"})
        ex, ctx = make_executor(ws, mode=Mode.AUTO_EDIT)
        spellings = [".GIT/config", "sub/../.git/config", ".git"]
        if os.name == "nt":
            spellings += [".git./config", ".git /config"]  # Windows strips trailing dots and spaces
        for path in spellings:
            res = await ex.execute(call("write_file", path=path, content="pwned"), ctx)
            self.assertTrue(res.is_error, path)
            self.assertIn("protect-git", res.content, path)
        self.assertEqual((ws / ".git/config").read_text(), "[core]\n")

    async def test_grep_skips_secret_files(self):
        ws = workspace({".env": "API_KEY=sekrit\n", "a.py": "KEY = 1\n"})
        ex, ctx = make_executor(ws, mode=Mode.DEFAULT)
        res = await ex.execute(call("grep", pattern="KEY"), ctx)
        self.assertIn("a.py:1", res.content)
        self.assertNotIn("sekrit", res.content)

    async def test_grep_does_not_follow_symlinks_out_of_the_workspace(self):
        outside = workspace({"secret.txt": "TOPSECRET\n"})
        ws = workspace({"a.txt": "hello\n"})
        try:
            (ws / "link.txt").symlink_to(outside / "secret.txt")
        except OSError:
            self.skipTest("symlinks need extra privileges on this platform")
        ex, ctx = make_executor(ws)
        self.assertNotIn("TOPSECRET", (await ex.execute(call("grep", pattern="."), ctx)).content)

    async def test_grep_on_a_file_and_on_a_missing_path(self):
        ws = workspace({"pkg/a.py": "x = 1\n"})
        ex, ctx = make_executor(ws)
        self.assertIn("pkg/a.py:1", (await ex.execute(call("grep", pattern="x", path="pkg/a.py"), ctx)).content)
        self.assertTrue((await ex.execute(call("grep", pattern="x", path="nope"), ctx)).is_error)


class EditTest(unittest.IsolatedAsyncioTestCase):
    async def _edit(self, raw: bytes, **kw) -> tuple[bytes, object]:
        ws = workspace()
        (ws / "f.py").write_bytes(raw)
        ex, ctx = make_executor(ws)
        await ex.execute(call("read_file", path="f.py"), ctx)
        res = await ex.execute(call("edit_file", path="f.py", **kw), ctx)
        return (ws / "f.py").read_bytes(), res

    async def test_edit_preserves_line_endings(self):
        out, _ = await self._edit(b"a = 1\nb = 2\n", old_string="a = 1", new_string="a = 3")
        self.assertEqual(out, b"a = 3\nb = 2\n")
        out, _ = await self._edit(b"a = 1\r\nb = 2\r\n", old_string="a = 1\nb = 2", new_string="a = 3\nb = 4")
        self.assertEqual(out, b"a = 3\r\nb = 4\r\n")

    async def test_empty_old_string_is_rejected(self):
        out, res = await self._edit(b"abc", old_string="", new_string="X", replace_all=True)
        self.assertTrue(res.is_error)
        self.assertEqual(out, b"abc")


class HookTest(unittest.IsolatedAsyncioTestCase):
    async def test_crashing_pre_hook_fails_closed_and_other_hooks_are_skipped(self):
        async def boom(payload):
            raise RuntimeError("bug")

        bus = HookBus()
        bus.on(HookEvent.PRE_TOOL_USE, boom)
        bus.on(HookEvent.POST_TOOL_USE, boom)
        pre = await bus.emit(HookEvent.PRE_TOOL_USE, {"tool": "x"})
        self.assertTrue(pre.block)
        self.assertIn("hook error", pre.reason)
        self.assertFalse((await bus.emit(HookEvent.POST_TOOL_USE, {"tool": "x"})).block)

    async def test_stop_hook_block_without_reason_still_vetoes(self):
        ws = workspace()
        model = ScriptedModel([ModelResponse(text="done"), ModelResponse(text="really done")])
        h = build_harness(ws, model, HarnessConfig(persist=False))
        calls = []

        async def once(payload):
            calls.append(1)
            return HookResult(block=True) if len(calls) == 1 else None

        h.hooks.on(HookEvent.STOP, once)
        res = await h.run("go")
        self.assertEqual((res.status, res.final_text, res.turns), ("completed", "really done", 2))


class OrchestrationHardeningTest(unittest.IsolatedAsyncioTestCase):
    async def test_worker_cap_covers_every_depth(self):
        ws = workspace({"a.py": "A = 1\n"})
        root = ScriptedModel([
            ModelResponse(tool_calls=[call("spawn_agent", agent="explorer", task="look")], stop_reason="tool_use"),
            ModelResponse(text="ok")])
        child = ScriptedModel([
            ModelResponse(tool_calls=[call("spawn_agent", agent="explorer", task="deeper")], stop_reason="tool_use"),
            ModelResponse(text="child done")])
        h = build_harness(ws, root, HarnessConfig(enable_subagents=True, max_depth=2, persist=False))
        h.coordinator.max_workers = 1
        h.coordinator.model_for = lambda spec: child
        await h.run("go")
        grandchild_result = child.requests[1]["messages"][-1]["content"]
        self.assertIn('status="refused"', grandchild_result)
        self.assertEqual(h.coordinator.spawned, 1)

    async def test_read_only_spawn_never_shares_a_batch_with_a_write(self):
        ws = workspace()
        h = build_harness(ws, ScriptedModel([]), HarnessConfig(enable_subagents=True, persist=False))
        calls = [call("write_file", path="a.py", content="x"), call("spawn_agent", agent="explorer", task="read a.py"),
                 call("spawn_agent", agent="explorer", task="read b.py")]
        plan = [[c.name for c in b.calls] for b in h.scheduler.plan(calls, h.loop.tool_ctx)]
        self.assertEqual(plan, [["write_file"], ["spawn_agent", "spawn_agent"]])

    async def test_verifier_can_run_checks(self):
        spec = BUILTIN_AGENTS["verifier"]
        self.assertFalse(spec.read_only)  # keeps the parent's approver instead of DenyAll
        self.assertNotIn("edit_file", spec.tools)
        self.assertNotIn("write_file", spec.tools)

    async def test_child_tool_search_cannot_see_tools_outside_its_whitelist(self):
        ws = workspace()
        h = build_harness(ws, ScriptedModel([]), HarnessConfig(persist=False), allowed_tools={"read_file"})
        res = await h.registry.get("tool_search").handler({"query": "select:write_file,run_command"}, h.loop.tool_ctx)
        self.assertEqual(res.content, "No matching tools.")
        self.assertEqual(h.session.state.loaded_tools, set())


class SkillTest(unittest.TestCase):
    def test_scalar_paths_and_requires(self):
        ws = workspace({
            ".agents/skills/db/SKILL.md": "---\nname: db\ndescription: migrations\npaths: db/*\n---\nbody",
            ".agents/skills/py/SKILL.md": f"---\nname: py\ndescription: py\nrequires: {Path(sys.executable).stem}\n---\nb",
            ".agents/skills/bad/SKILL.md": "---\nname: bad\ndescription: no closing fence\n",
        })
        reg = SkillRegistry([ws / ".agents" / "skills"])
        self.assertEqual(reg.skills["db"].paths, ["db/*"])
        self.assertEqual(reg.on_file_touched("README.md"), "")
        self.assertIn("db", reg.on_file_touched("db/001.sql"))
        self.assertIn("py", reg.skills)
        self.assertIn("no closing fence", reg.skills["bad"].body())


class AdapterAndCliTest(unittest.IsolatedAsyncioTestCase):
    def test_empty_assistant_turn_is_not_sent(self):
        wire = to_wire([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "", "raw": []},
                        {"role": "user", "content": "continue"}])
        self.assertEqual([m["role"] for m in wire], ["user"])

    async def test_empty_dynamic_system_block_is_omitted(self):
        try:
            from harness.llm.anthropic_provider import AnthropicModel
            import anthropic  # noqa: F401
        except ImportError:
            self.skipTest("anthropic SDK not installed")
        sent = {}

        class Fake:
            class messages:
                @staticmethod
                async def create(**kw):
                    sent.update(kw)
                    raise RuntimeError("stop here")

        model = AnthropicModel(client=Fake(), fallbacks=False)
        with self.assertRaises(RuntimeError):
            await model.complete(SystemPrompt("static", ""), [{"role": "user", "content": "x"}], [])
        self.assertEqual([b["text"] for b in sent["system"]], ["static"])

    def test_tree_handles_long_sessions(self):
        path = workspace() / "s.jsonl"
        log = EventLog(path)
        for i in range(3000):
            log.append(EventKind.USER_MESSAGE, {"content": str(i)})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            tree(path)
        self.assertEqual(len(buf.getvalue().splitlines()), 3000)


class ProcessTest(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_command_does_not_leave_the_process_running(self):
        ws = workspace({"slow.py": "import time, pathlib\ntime.sleep(1.5)\npathlib.Path('marker').write_text('x')\n"})
        runner = CommandRunner(ws)
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(runner.run([sys.executable, "slow.py"], timeout_s=100), 0.5)
        await asyncio.sleep(2.0)
        self.assertFalse((ws / "marker").exists())

    async def test_relative_program_resolves_in_the_workspace(self):
        name = "tool.bat" if os.name == "nt" else "tool.sh"
        body = "@echo hi\n" if os.name == "nt" else "#!/bin/sh\necho hi\n"
        ws = workspace({name: body})
        (ws / name).chmod(0o755)
        res = await CommandRunner(ws).run([f"./{name}"])
        self.assertEqual(res.exit_code, 0, res.stderr)

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    async def test_diff_validator_counts_untracked_files(self):
        ws = workspace()
        subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
        (ws / "new.py").write_text("x = 1\n")
        h = build_harness(ws, ScriptedModel([]), HarnessConfig(persist=False))
        ev = await DiffValidator().check(h.session, h.runner)
        self.assertTrue(ev.passed, ev.details)


if __name__ == "__main__":
    unittest.main()
