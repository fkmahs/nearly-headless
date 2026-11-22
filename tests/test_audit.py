"""`nh audit measure`: the weekly audit's counts, over small made-up transcripts in a temp folder."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["NH_AUTO_UPDATE"] = "0"

from wk import audit, cli  # noqa: E402
from wk.core import KitError  # noqa: E402

NOW = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)


def at(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z")


def owner(text, h, **extra):
    return {"type": "user", "timestamp": at(h), "message": {"role": "user", "content": text}} | extra


def calls(h, *names, ids=None, **inputs):
    return {"type": "assistant", "timestamp": at(h), "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": (ids or [f"t{i}" for i in range(len(names))])[i], "name": n,
         "input": inputs.get(n, {})} for i, n in enumerate(names)]}}


def shell(h, command, call="t0"):
    return calls(h, "Bash", ids=[call], Bash={"command": command})


def result(h, call="t0", text="ok", error=False):
    return {"type": "user", "timestamp": at(h), "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": call, "content": text, "is_error": error}]}}


def ran(h, command, call, text="ok", error=False):
    return [shell(h, command, call), result(h - 0.01, call, text, error)]


def ended(h):
    return {"type": "system", "subtype": "stop_hook_summary", "timestamp": at(h)}


def handback(h):
    return owner("<task-notification>\nagent done\n</task-notification>", h)


class Measure(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "projects"

    def chat(self, name, entries, folder="C--Projects-acme", tail=b"", age_days=0.0):
        path = self.root / folder / f"{name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"".join(json.dumps(e).encode() + b"\n" for e in entries) + tail)
        stamp = (NOW - timedelta(days=age_days)).timestamp()
        os.utime(path, (stamp, stamp))
        return path

    def measure(self, days=7):
        return audit.measure(days, self.root, now=NOW)

    def test_owner_text_is_counted_but_tool_results_hooks_and_injected_text_are_not(self):
        self.chat("s1", [owner("Start the supervisor for acme. Where are we?", 5),
                         owner("<system-reminder>be brief</system-reminder>", 5, isMeta=True),
                         owner("Stop hook feedback: add the recap", 5),
                         owner("hook text", 5, isMeta=True),
                         calls(4.9, "Read"), result(4.9), ended(4.8),
                         owner("<command-name>/catch-up</command-name>", 4),  # a typed slash command, as reply-check
                         calls(3.9, "Read"), ended(3.8),
                         owner("No, that's wrong. Please sign in and click Save", 3), calls(2.9, "Edit"), ended(2.8)])
        r = self.measure()["rows"][0]
        self.assertEqual((r["role"], r["owner_messages"], r["turns"]), ("supervisor", 3, 3))
        self.assertEqual(r["keywords"], {"corrections": 1, "status_asks": 1, "sign_in_or_click": 1})
        self.assertNotIn("Save", json.dumps(self.measure()))  # counts only: no message text is kept

    def test_own_calls_per_turn_skip_handoffs_and_app_tools_and_a_mid_turn_handback_stays_in_its_turn(self):
        self.chat("s1", [owner("You are the builder for this piece", 6),
                         calls(5.9, "Read", "Bash", "Agent", "ToolSearch", "SendMessage", "mcp__ccd_turn__update_plan"),
                         handback(5.8),  # arrives while the turn still runs: same turn
                         calls(5.7, "Edit", "mcp__ccd_turn__wrap_up"), ended(5.6),
                         handback(5),  # after the turn ended: a turn of its own
                         calls(4.9, "Read", "Read", "Read"), ended(4.8),
                         owner("thanks", 4), ended(3.9),
                         owner("one more", 3), calls(2.9, "Read", "Read"), ended(2.8)])
        r = self.measure()["rows"][0]
        self.assertEqual(r["role"], "builder")
        self.assertEqual(r["turns"], 4)
        self.assertEqual(r["own_calls"], {"total": 8, "median": 2.5, "p90": 3, "max": 3})
        self.assertEqual((r["agents"], r["shell"]), (1, 1))

    def test_browser_calls_and_picking_compactions_and_push_notifications(self):
        self.chat("s1", [owner("help me", 6), calls(5.9, "mcp__claude-in-chrome__list_connected_browsers",
                                                    "mcp__claude-in-chrome__select_browser",
                                                    "mcp__claude-in-chrome__navigate",
                                                    "mcp__Claude_Browser__navigate", "PushNotification"),
                         {"type": "system", "subtype": "compact_boundary", "timestamp": at(5)},
                         owner("summary of the chat so far", 5, isCompactSummary=True)])
        r = self.measure()["rows"][0]
        self.assertEqual((r["browser"], r["browser_picking"]), (4, 2))
        self.assertEqual((r["compactions"], r["push_notifications"], r["owner_messages"]), (1, 1, 1))

    def test_a_merge_counts_as_gated_only_after_a_passed_fresh_gate_on_the_same_pr_which_it_uses_up(self):
        self.chat("s1", [owner("merge them", 6),
                         *ran(5.9, "nh gate fresh --pr 12 --sha abc1234", "a"),
                         *ran(5.8, "gh pr merge https://github.com/acme/app/pull/12 --merge --match-head-commit 4567",
                              "b"),
                         *ran(5.7, "gh pr merge 12 --merge", "c"),  # the gate was used up by the merge before
                         *ran(5.6, "gh pr merge 13 --merge", "d"),
                         *ran(5.5, "nh gate fresh --pr 13", "e"),  # after its merge: too late
                         *ran(5.4, "nh gate fresh --pr 14", "f", "nh: refused: main moved", error=True),
                         *ran(5.3, "gh pr merge 14 --merge", "g"),  # its gate refused
                         *ran(5.2, 'nh gate fresh --pr 15 --sha abc1234 && gh pr merge 15 --merge', "h"),  # one line
                         *ran(5.1, "gh pr merge --merge", "i"),  # no PR number: unknown, never gated
                         *ran(5.0, "nh gate fresh --pr 16 && gh pr merge 16", "j", "nh: refused", error=True),  # may
                         *ran(4.9, "gh -R acme/app pr merge 17 --merge", "k")])  # never have run: not counted
        r = self.measure()["rows"][0]
        self.assertEqual((r["merges"], r["merges_gated"], r["merges_unknown"]), (7, 2, 1))

    def test_merges_and_gates_inside_a_grep_an_echo_or_help_are_not_counted(self):
        self.chat("s1", [owner("look", 6),
                         *ran(5.9, 'grep -rn "gh pr merge 12" skills/ | head', "a"),
                         *ran(5.8, 'echo "nh gate fresh --pr 12"; gh pr merge --help', "b"),
                         *ran(5.7, "nh gate fresh --help", "c"),
                         *ran(5.6, "cd app; if ($?) { gh pr merge 12 --merge }", "d")])
        self.assertEqual(audit.steps("cd app && nh --cwd app gate fresh --pr 9 && gh pr merge 9"),
                         [("gate", 9), ("merge", 9)])
        self.assertEqual(audit.steps('echo "x; gh pr merge 5" && echo \'a | nh gate fresh --pr 5\''), [])
        self.assertEqual(audit.steps('gh pr merge 6 --subject "a; b" ; gh --repo acme/app pr merge 7'),
                         [("merge", 6), ("merge", 7)])
        r = self.measure()["rows"][0]
        self.assertEqual((r["merges"], r["merges_gated"], r["merges_unknown"]), (1, 0, 0))

    def test_subagents_sidechains_bad_lines_and_a_half_written_last_line_are_skipped(self):
        self.chat("s1", [owner("start", 6), {"type": "assistant", "isSidechain": True, "timestamp": at(5.9),
                                              "message": {"content": [{"type": "tool_use", "name": "Read"}]}},
                         calls(5.8, "Read")], tail=b"\xff\xfe not json\n" + b'{"type": "assistant", "mess')
        self.chat("agent-1", [owner("You are the builder", 5), calls(4.9, "Read")],
                  folder="C--Projects-acme/s1/subagents")
        m = self.measure()
        self.assertEqual(m["chats"], 1)
        self.assertEqual(m["rows"][0]["own_calls"]["total"], 1)

    def test_the_window_cuts_off_old_chats_and_old_lines(self):
        self.chat("old", [owner("long ago", 24 * 10), calls(24 * 10, "Read")], age_days=10)
        self.chat("mixed", [owner("last month", 24 * 9), calls(24 * 9, "Read", "Read"), ended(24 * 9),
                            owner("today", 2), calls(1.9, "Read"), ended(1.8)])
        m = self.measure()
        self.assertEqual([r["session"] for r in m["rows"]], ["mixed"])
        r = m["rows"][0]
        self.assertEqual((r["turns"], r["owner_messages"], r["own_calls"]["total"]), (1, 1, 1))
        self.assertEqual(r["first"], at(2))
        self.assertEqual(self.measure(days=10)["rows"][0]["turns"], 2)

    def test_a_turn_running_when_the_window_opens_counts_once_whole(self):
        self.chat("s1", [owner("go", 24 * 7 + 1), calls(24 * 7 + 0.5, "Read", "Read"), calls(24 * 7 - 1, "Edit"),
                         ended(24 * 7 - 1.1), owner("next", 2), calls(1.9, "Read"), ended(1.8)])
        r = self.measure()["rows"][0]
        self.assertEqual((r["turns"], r["owner_messages"]), (2, 1))
        self.assertEqual(r["own_calls"], {"total": 4, "median": 2.0, "p90": 3, "max": 3})

    def test_it_writes_json_and_a_table_outside_any_repo_and_refuses_inside_one(self):
        self.chat("s1", [owner("hi", 1)])
        out = audit.write(self.measure(), self.tmp / "audits" / "2026-03-10")
        self.assertEqual(json.loads((out / "measure.json").read_text(encoding="utf-8"))["chats"], 1)
        self.assertIn("| C--Projects-acme | s1 |", (out / "measure.md").read_text(encoding="utf-8"))
        (self.tmp / "repo" / ".git").mkdir(parents=True)
        with self.assertRaises(KitError):
            audit.write(self.measure(), self.tmp / "repo" / "audits")
        self.assertEqual(audit.folder(self.tmp / "settings.toml", NOW), self.tmp / "audits" / "2026-03-10")

    def test_the_command_prints_the_folder_and_totals(self):
        import io
        from contextlib import redirect_stdout
        from unittest import mock
        self.chat("s1", [owner("hi", 1), calls(0.9, "Read")])
        real, buf = audit.measure, io.StringIO()
        with mock.patch.object(audit, "measure", lambda days: real(days, self.root, now=NOW)), redirect_stdout(buf):
            code = cli.main(["--settings", str(self.tmp / "settings.toml"), "audit", "measure", "--out",
                             str(self.tmp / "out")])
        self.assertEqual(code, 0)
        self.assertIn("1 chats, 1 turns, 1 own tool calls", buf.getvalue())
        self.assertTrue((self.tmp / "out" / "measure.md").is_file())


if __name__ == "__main__":
    unittest.main()
