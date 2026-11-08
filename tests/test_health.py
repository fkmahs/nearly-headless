"""wk health: what it reads from transcripts, counted once each, by period, with versions and verdicts. Every
transcript here is a small made-up one (no chat text)."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["WORKKIT_AUTO_UPDATE"] = "0"

from wk import cli, health  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)  # a Wednesday, noon: today counts as half a day
CHAT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def at(day_offset: int, hour: int = 10) -> str:
    """A time stamp `day_offset` days from NOW's day (0 = today), at `hour` UTC."""
    d = NOW.replace(hour=hour, minute=0) + timedelta(days=day_offset)
    return d.isoformat().replace("+00:00", "Z")


def call(key, when, inp=10, read=100, write=20, out=5, block="text"):
    return {"type": "assistant", "timestamp": when, "requestId": key, "cwd": "C:\\Projects\\demo",
            "message": {"id": "msg_" + key, "model": "claude-opus-5-5", "content": [{"type": block}],
                        "usage": {"input_tokens": inp, "cache_read_input_tokens": read,
                                  "cache_creation_input_tokens": write, "output_tokens": out}}}


def notice(tid, when, tokens, ms=60000, tools=3, summary="Agent finished"):
    body = (f"<task-notification>\n<task-id>{tid}</task-id>\n<status>completed</status>\n<summary>{summary}</summary>\n"
            f"<usage><subagent_tokens>{tokens}</subagent_tokens><tool_uses>{tools}</tool_uses>"
            f"<duration_ms>{ms}</duration_ms></usage>\n</task-notification>")
    return {"type": "queue-operation", "operation": "enqueue", "timestamp": when, "content": body}


def rejection(uid, when):
    return {"type": "user", "uuid": uid, "timestamp": when,
            "message": {"role": "user", "content": "Stop hook feedback:\nThe reply is missing its recap"}}


def bash(tid, when, command, cwd="C:\\Projects\\demo"):
    return {"type": "assistant", "timestamp": when, "cwd": cwd, "message": {"id": "m" + tid, "content": [
        {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": command}}]}}


def result(tid, when, content, error=False):
    return {"type": "user", "timestamp": when, "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid, "content": content, "is_error": error}]}}


def write(path: Path, lines: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    return path


class Health(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "projects"
        self.cache = self.tmp / "cache.json"
        self.released = [(datetime(2026, 10, 20, 15, tzinfo=UTC), "1.0.0"),
                         (datetime(2026, 11, 2, 15, tzinfo=UTC), "1.1.0"),
                         (datetime(2026, 11, 4, 9, tzinfo=UTC), "1.2.0")]

    def run_(self, days=14):
        return health.health(self.root, now=NOW, days=days, cache=self.cache, released=self.released, tz=UTC)

    def period(self, h, name):
        return next(p for p in h["periods"] if p["name"] == name)

    def test_empty_folder(self):
        h = self.run_()  # no projects folder at all
        m = self.period(h, "this week")["measures"]
        self.assertEqual(m["calls"], 0)
        self.assertIsNone(m["chat_p50"])
        self.assertEqual(m["tied"], "0 of 0")
        text = health.text(h)
        self.assertIn("unknown", text)            # no agent tokens per merged PR to show
        self.assertLess(len(text.splitlines()), 40)

    def test_calls_counted_once_by_type_chats_and_agents(self):
        folder = self.root / "C--Projects-demo"
        # one response logged as two blocks, the later with its full output; copied into a resumed chat too
        write(folder / f"{CHAT}.jsonl", [call("r1", at(-1), out=2), call("r1", at(-1), out=7, block="tool_use"),
                                         call("r2", at(-1), inp=1, read=1000, write=0, out=1)])
        write(folder / "bbbbbbbb-0000-0000-0000-000000000000.jsonl", [call("r1", at(-1), out=7)])
        write(folder / CHAT / "subagents" / "agent-a1.jsonl", [call("r3", at(-1), inp=0, read=50, write=0)])
        m = self.period(self.run_(), "yesterday")["measures"]
        self.assertEqual(m["calls"], 3)
        self.assertAlmostEqual(m["output"] * 1e6, 7 + 1 + 5)
        self.assertAlmostEqual(m["input"] * 1e6, 10 + 1 + 0)
        self.assertAlmostEqual(m["cache_read"] * 1e6, 100 + 1000 + 50)
        self.assertAlmostEqual(m["cache_write"] * 1e6, 20)
        self.assertAlmostEqual(m["agent_p50"], 0.05)        # the agent's one call: 50 tokens of context
        self.assertAlmostEqual(m["chat_p50"], (130 + 1001) / 2 / 1e3)

    def test_periods_and_boundaries(self):
        folder = self.root / "C--Projects-demo"
        write(folder / f"{CHAT}.jsonl", [
            call("today", at(0, 1)), call("yesterday", at(-1, 23)), call("monday", at(-2, 0)),
            call("sunday", at(-3, 23)), call("last-monday", at(-9, 0)), call("before", at(-10, 23)),
            call("day14", at(-14, 0)), call("day15", at(-15, 12))])
        h = self.run_()
        calls = {p["name"]: round(p["measures"]["calls"] * p["days"]) for p in h["periods"]}
        self.assertEqual(calls["today"], 1)
        self.assertEqual(calls["yesterday"], 1)
        self.assertEqual(calls["this week"], 3)            # Monday 00:00 to now
        self.assertEqual(calls["last week"], 2)            # the Monday and Sunday before
        self.assertEqual(calls["14 days"], 6)              # 14 full days, today left out
        self.assertEqual(calls["14 days before"], 1)
        self.assertAlmostEqual(self.period(h, "today")["days"], 0.5)
        self.assertAlmostEqual(self.period(h, "today")["measures"]["calls"], 2)  # per day: 1 call in half a day
        self.assertAlmostEqual(self.period(h, "this week")["days"], 2.5)

    def test_runs_wakeups_reviews_and_tokens_per_merged_pr(self):
        folder = self.root / "C--Projects-demo"
        sub = folder / CHAT / "subagents"
        write(folder / f"{CHAT}.jsonl", [
            notice("a1", at(-1), 1000, ms=120000), notice("a1", at(-1), 1000, ms=120000),  # the same notice twice
            notice("a1", at(-1, 14), 500),                                                 # a wake-up
            notice("a2", at(-1, 15), 3000, summary="Review of PR 7 done"),
            bash("t1", at(-1, 16), "gh pr merge 7 --merge --match-head-commit 9a8b7c6"),
            result("t1", at(-1, 16), "Merged"),
            bash("t2", at(-1, 16), "gh pr merge 8 --merge"), result("t2", at(-1, 16), "Merged"),
            bash("t3", at(-1, 17), "gh pr merge 9"), result("t3", at(-1, 17), "Exit code 1\nnot mergeable", True)])
        write(sub / "agent-a1.jsonl", [
            bash("c1", at(-1), "gh pr create --draft --title x", cwd="C:\\Projects\\demo\\.claude\\worktrees\\agent-a1"),
            result("c1", at(-1), "https://github.com/o/demo/pull/7")])
        # a notice copied into a forked chat, without a time stamp, still counts once
        write(folder / "cccccccc-0000-0000-0000-000000000000.jsonl", [notice("a2", "", 3000, summary="Review x")])
        m = self.period(self.run_(), "yesterday")["measures"]
        self.assertEqual(m["runs"], 3)
        self.assertAlmostEqual(m["wakeups"], 100 / 3)
        self.assertEqual(m["run_tokens"], 1.0)              # median of 1000, 500, 3000
        self.assertEqual(m["run_minutes"], 1.0)             # median of 2, 1, 1 minutes
        self.assertEqual(m["merged"], 2)                    # the refused merge doesn't count
        self.assertEqual(m["reviews"], 0.5)                 # one review, two merged PRs
        self.assertAlmostEqual(m["piece_tokens"], 1500 / 1e6)  # PR 7's builder a1: both its runs
        self.assertEqual(m["tied"], "1 of 2")               # PR 8 has no builder here: never guessed

    def test_rejections_and_refresh(self):
        folder = self.root / "C--Projects-demo"
        write(folder / f"{CHAT}.jsonl", [
            rejection("u1", at(-1)), rejection("u2", at(-1, 11)),
            {"type": "user", "uuid": "u3", "timestamp": at(-1),
             "message": {"role": "user", "content": "Stop hook feedback: fine"}},     # not a rejection
            bash("r1", at(-1), "wk refresh --pr 3"), result("r1", at(-1), "abc123\nrefreshed"),
            bash("r2", at(-1), 'py -P -m wk refresh --pr 4'),
            result("r2", at(-1), "Exit code 1\nwk: refused", True),
            bash("r3", at(-1), "wk refresh --pr 5"),                                   # no result yet
            bash("r4", at(-1), "grep -n 'refresh' README.md"),                          # not a use
            {"type": "api-request-blob", "message": bash("r1", "", "wk refresh --pr 3")["message"]}])  # history, no time
        write(folder / "bbbbbbbb-0000-0000-0000-000000000000.jsonl", [rejection("u1", at(-1))])  # a copy
        m = self.period(self.run_(), "yesterday")["measures"]
        self.assertEqual(m["rejections"], 2)
        self.assertEqual(m["refresh"], 3)
        self.assertEqual(m["refused"], 1)

    def test_only_commands_count_not_mentions(self):
        folder = self.root / "C--Projects-demo"
        write(folder / f"{CHAT}.jsonl", [
            bash("g1", at(-1), 'grep -n "gh pr merge" notes.txt'), result("g1", at(-1), "3: gh pr merge"),
            bash("g2", at(-1), 'rg "wk refresh" README.md'), result("g2", at(-1), "README.md: wk refresh"),
            bash("g3", at(-1), "echo gh pr merge 4"), result("g3", at(-1), "gh pr merge 4"),
            bash("c1", at(-1), "git fetch origin && WORKKIT_AUTO_UPDATE=0 py -P -m wk refresh --pr 3; gh pr merge 5 --merge"),
            result("c1", at(-1), "abc123\nrefreshed\nMerged")])
        m = self.period(self.run_(), "yesterday")["measures"]
        self.assertEqual(m["refresh"], 1)
        self.assertEqual(m["merged"], 1)

    def test_a_quoted_notice_is_not_a_run(self):
        folder = self.root / "C--Projects-demo"
        quoted = call("q1", at(-1), out=9)
        quoted["message"]["content"] = [{"type": "text", "text": notice("a9", at(-1), 5000)["content"]}]
        write(folder / f"{CHAT}.jsonl", [quoted, result("t9", at(-1), notice("a8", at(-1), 4000)["content"])])
        m = self.period(self.run_(), "yesterday")["measures"]
        self.assertEqual(m["calls"], 1)                     # its tokens still count
        self.assertAlmostEqual(m["output"] * 1e6, 9)
        self.assertEqual(m["runs"], 0)

    def test_a_merge_counts_for_the_repo_it_names(self):
        demo, other = self.root / "C--Projects-demo", self.root / "C--Projects-other"
        write(demo / f"{CHAT}.jsonl", [
            notice("a1", at(-2), 1000), notice("a2", at(-2), 7000),
            bash("m1", at(-1), "gh pr merge 7 --repo o/other --merge"), result("m1", at(-1), "Merged"),
            bash("m2", at(-1), "cd C:/Projects/third && gh pr merge 9 --merge"), result("m2", at(-1), "Merged")])
        write(demo / CHAT / "subagents" / "agent-a1.jsonl", [
            bash("c1", at(-2), "gh pr create --title x"), result("c1", at(-2), "https://github.com/o/demo/pull/7")])
        write(other / "dddddddd-0000-0000-0000-000000000000" / "subagents" / "agent-a2.jsonl", [
            bash("c2", at(-2), "gh pr create --title y", cwd="C:\\Projects\\other"),
            result("c2", at(-2), "https://github.com/o/other/pull/7")])
        m = self.period(self.run_(), "yesterday")["measures"]
        self.assertEqual(m["merged"], 2)
        self.assertEqual(m["tied"], "1 of 2")               # PR 9 ran in a folder it can't tell: not tied
        self.assertAlmostEqual(m["piece_tokens"], 7000 / 1e6)  # o/other#7's builder, not o/demo#7's

    def test_versions_by_day(self):
        h = self.run_(days=3)
        self.assertEqual(h["version_by_day"], {"2026-11-01": "1.0.0", "2026-11-02": "1.1.0", "2026-11-03": "1.1.0",
                                               "2026-11-04": "1.2.0"})
        self.assertEqual(self.period(h, "today")["versions"], ["1.1.0", "1.2.0"])
        self.assertEqual(self.period(h, "last week")["versions"], ["1.0.0"])
        self.assertEqual(self.period(h, "this week")["versions"], ["1.0.0", "1.1.0", "1.2.0"])
        self.assertIn("1.0.0-1.2.0", health.text(h))

    def test_verdict_thresholds(self):
        self.assertEqual(health.verdict(109, 100, "lower"), "flat")
        self.assertEqual(health.verdict(91, 100, "lower"), "flat")
        self.assertEqual(health.verdict(111, 100, "lower"), "worse")
        self.assertEqual(health.verdict(89, 100, "lower"), "healthier")
        self.assertEqual(health.verdict(5, 0, "lower"), "worse")
        self.assertEqual(health.verdict(0, 0, "lower"), "flat")
        self.assertEqual(health.verdict(None, 3, "lower"), "unknown")
        self.assertEqual(health.verdict(50, 1, ""), "-")

    def test_table_warns_and_json(self):
        folder = self.root / "C--Projects-demo"
        lines = [call(f"l{i}", at(-8)) for i in range(7)]          # last week: 1 call a day
        lines += [call(f"t{i}", at(-1), read=1000) for i in range(5)]  # this week: 2 a day, bigger context
        write(folder / f"{CHAT}.jsonl", lines)
        h = self.run_()
        week = self.period(h, "this week")
        self.assertEqual(week["against"], "last week")
        self.assertEqual(week["verdicts"]["calls"], "worse")
        self.assertEqual(week["verdicts"]["runs"], "-")
        text = health.text(h)
        self.assertLess(len(text.splitlines()), 40)
        self.assertIn("Worse: API calls;", text)
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(health.usage, "projects", return_value=self.root),                 mock.patch.object(health, "cache_file", return_value=self.cache):
            code = cli.main(["--settings", str(self.tmp / "none.toml"), "health", "--json", "--days", "3"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["days"], 3)

    def test_cache_reads_a_file_again_only_when_it_changed(self):
        folder = self.root / "C--Projects-demo"
        path = write(folder / f"{CHAT}.jsonl", [call("r1", at(-1))])
        self.run_()
        kept = json.loads(self.cache.read_text(encoding="utf-8"))
        self.assertEqual(kept["version"], health.CACHE_VERSION)
        write(path, [call("r1", at(-1)), call("r2", at(-1))])
        self.assertEqual(self.period(self.run_(), "yesterday")["measures"]["calls"], 2)


if __name__ == "__main__":
    unittest.main()
