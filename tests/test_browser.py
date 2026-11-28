"""A chat picks its browser once: the reminder hook, and saved memory that says otherwise (wk/browser.py)."""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from wk import browser, cli, upkeep

CHROME = "mcp__claude-in-chrome__"


def call(name, uid=""):
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "id": uid, "input": {}}]}}


class Guard(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.chat = self.tmp / "chat.jsonl"

    def run_hook(self, calls, tool="select_browser", **more):
        self.chat.write_text("".join(json.dumps(c) + "\n" for c in calls), encoding="utf-8")
        hook = {"hook_event_name": "PreToolUse", "tool_name": CHROME + tool, "tool_input": {},
                "transcript_path": str(self.chat), "session_id": "s1"} | more
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(hook))), redirect_stdout(out):
            code = cli.main(["browser-guard"])
        self.assertEqual(code, 0)
        return out.getvalue()

    def test_picking_again_after_working_in_the_browser_gets_a_reminder_but_still_runs(self):
        picked = [call(CHROME + "list_connected_browsers"), call(CHROME + "select_browser"), call(CHROME + "navigate")]
        said = json.loads(self.run_hook(picked))["hookSpecificOutput"]
        self.assertEqual(said["hookEventName"], "PreToolUse")
        self.assertNotIn("permissionDecision", said)  # never a refusal
        self.assertIn("Keep using that one", said["additionalContext"])
        self.assertIn("Keep using that one", self.run_hook(picked, tool="list_connected_browsers"))

    def test_the_first_pick_and_picks_in_a_row_are_quiet(self):
        self.assertEqual(self.run_hook([]), "")
        self.assertEqual(self.run_hook([call(CHROME + "list_connected_browsers")]), "")
        self.assertEqual(self.run_hook([call(CHROME + "select_browser"), call(CHROME + "select_browser")]), "")
        # the call being decided, already in the transcript, doesn't count as a fresh pick
        self.assertIn("Keep using", self.run_hook([call(CHROME + "select_browser"), call(CHROME + "navigate"),
                                                   call(CHROME + "select_browser", "t9")], tool_use_id="t9"))

    def test_subagents_other_tools_and_bad_input_are_let_through(self):
        worked = [call(CHROME + "select_browser"), call(CHROME + "navigate")]
        self.assertEqual(self.run_hook(worked, agent_id="a1"), "")
        self.assertEqual(self.run_hook(worked, tool="navigate"), "")
        self.assertEqual(self.run_hook(worked, transcript_path=str(self.tmp / "gone.jsonl")), "")
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO("not json")), redirect_stdout(out):
            self.assertEqual(cli.main(["browser-guard"]), 0)
        self.assertEqual(out.getvalue(), "")

    def test_setup_installs_it_for_the_browser_picking_tools(self):
        import re
        self.assertIn(upkeep.BROWSER_GUARD, upkeep.HOOKS)
        matcher = re.compile(upkeep.BROWSER_GUARD[1])
        for tool in ("select_browser", "list_connected_browsers"):
            self.assertTrue(matcher.fullmatch(CHROME + tool), tool)
        self.assertFalse(matcher.fullmatch(CHROME + "navigate"))


class Conflicts(unittest.TestCase):
    def test_a_memory_telling_chats_to_pick_the_browser_every_turn_is_found(self):
        tmp = Path(tempfile.mkdtemp())
        top = tmp / "repos" / "app"
        memory = tmp / "projects" / browser.slug(top) / "memory"
        memory.mkdir(parents=True)
        (memory / "browser.md").write_text(
            "# Browser\nRe-select the work browser every turn.\nAlways call select_browser before each step.\n"
            "Don't re-select the browser every turn.\nUse the browser the map names.\n", encoding="utf-8")
        (memory / "other.md").write_text("Pick the right browser before any write.\n", encoding="utf-8")
        found = [(h["path"].name, h["line"]) for h in browser.conflicts(top, tmp / "projects")]
        self.assertEqual(found, [("browser.md", 2), ("browser.md", 3)])  # a check before a write is the kit's own
        self.assertEqual(browser.conflicts(tmp / "elsewhere", tmp / "projects"), [])

    def test_the_folder_name_is_claude_codes(self):
        self.assertEqual(browser.slug(Path("C:/Projects/work-kit")) if sys.platform == "win32" else
                         browser.slug(Path("/home/u/work-kit")),
                         "C--Projects-work-kit" if sys.platform == "win32" else "-home-u-work-kit")


if __name__ == "__main__":
    unittest.main()
