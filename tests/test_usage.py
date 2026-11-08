"""Claude's records read for the memory flag and the token and cost page: parsing, pricing, the flag in wk check, the
weekly tables, and the cache. Every transcript here is a small made-up one (no chat text)."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["WORKKIT_AUTO_UPDATE"] = "0"

from wk import check, core, settings, upkeep, usage, work  # noqa: E402

SUP = "11111111-2222-3333-4444-555555555555"   # the desktop app's chat id in the supervisor link
CLI = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"   # the Claude Code session it runs
OTHER = "99999999-8888-7777-6666-555555555555"
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)  # a Wednesday


def ts(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z")


def call(mid, hours_ago, model="claude-opus-5-5", inp=10, read=0, write=0, out=5, hour=0, cwd="", fast=False):
    usage_ = {"input_tokens": inp, "cache_read_input_tokens": read, "cache_creation_input_tokens": write,
              "output_tokens": out, "cache_creation": {"ephemeral_1h_input_tokens": hour,
                                                       "ephemeral_5m_input_tokens": write - hour}}
    if fast:
        usage_["speed"] = "fast"
    return {"type": "assistant", "timestamp": ts(hours_ago), "cwd": cwd,
            "message": {"id": mid, "model": model, "role": "assistant", "content": [{"type": "text", "text": "x"}],
                        "usage": usage_}}


def compacted(hours_ago):
    return {"type": "system", "subtype": "compact_boundary", "timestamp": ts(hours_ago)}


def write(path: Path, lines: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    os.utime(path, (NOW.timestamp(), NOW.timestamp()))  # last written at NOW, like a chat in use then
    return path


class Usage(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.claude = self.tmp / "claude"  # stands in for ~/.claude
        self.root = self.claude / "projects"
        self.apps = [self.tmp / "app" / "claude-code-sessions"]
        write(self.apps[0] / "org" / "acct" / f"local_{SUP}.json", [])
        (self.apps[0] / "org" / "acct" / f"local_{SUP}.json").write_text(
            json.dumps({"sessionId": f"local_{SUP}", "cliSessionId": CLI}), encoding="utf-8")
        (self.claude).mkdir(parents=True, exist_ok=True)
        (self.claude / "settings.json").write_text(json.dumps({upkeep.WINDOW: 1000}), encoding="utf-8")
        self.real = upkeep.claude_dir, usage.app_sessions, usage.Records.at
        upkeep.claude_dir = lambda: self.claude
        usage.app_sessions = lambda: self.apps
        self.records = usage.Records(self.tmp / "cache.json")
        usage.Records.at = lambda cache=None: self.records
        self.app = self.tmp / "repo"
        (self.app / work.DIR).mkdir(parents=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.app)], check=True)
        (self.app / work.DIR / "products.toml").write_text(
            f'[products.reports]\nline = "Reports"\nsupervisor = "claude://claude.ai/epitaxy/local_{SUP}"\n',
            encoding="utf-8")
        self.s = {"repos": {"acme": {"path": core.posix(self.app)}}}

    def tearDown(self):
        upkeep.claude_dir, usage.app_sessions, usage.Records.at = self.real

    def chat(self, sid, lines, folder="C--repo"):
        return write(self.root / folder / f"{sid}.jsonl", lines)

    def builder(self, sid, name, lines, what="Build the export"):
        path = write(self.root / "C--repo" / sid / "subagents" / f"{name}.jsonl", lines)
        path.with_suffix(".meta.json").write_text(json.dumps({"description": what}), encoding="utf-8")
        return path

    # ---------------------------------------------------------------- parsing and pricing

    def test_a_call_logged_once_per_block_counts_once_and_cache_writes_keep_their_kind(self):
        path = self.chat(CLI, [call("m1", 5, inp=3, read=100, write=40, hour=30, out=7, cwd="C:\\repo"),
                               call("m1", 5, inp=3, read=100, write=40, hour=30, out=7),
                               call("m2", 4, model="<synthetic>"), compacted(3), {"type": "user"}, "not json"])
        data = usage.parse(path)
        self.assertEqual(list(data["rows"]), ["m1"])
        row = data["rows"]["m1"]
        self.assertEqual(row[usage.IN:usage.FAST], [3, 100, 10, 30, 7])
        self.assertEqual(usage.context(row), 143)
        self.assertEqual(data["compacts"], [ts(3)])
        self.assertEqual(data["cwd"], "C:\\repo")

    def test_cost_is_at_api_list_prices_and_an_unknown_model_has_none(self):
        row = lambda model, **k: usage.parse(self.chat(OTHER, [call("m", 1, model=model, **k)]))["rows"]["m"]  # noqa: E731
        # Opus 5.5: $4 in, $20 out, $0.20 cache read, cache writes $5 (5 min) and $8 (1 hour) per million
        self.assertAlmostEqual(usage.cost(row("claude-opus-5-5", inp=1_000_000, read=1_000_000, write=2_000_000,
                                              hour=1_000_000, out=1_000_000)), 4 + 0.2 + 5 + 8 + 20)
        self.assertAlmostEqual(usage.cost(row("claude-opus-5-5", inp=1_000_000, out=0, fast=True)), 8)
        self.assertAlmostEqual(usage.cost(row("claude-sonnet-5-5-20261001", inp=1_000_000, out=0)), 2)
        self.assertAlmostEqual(usage.cost(row("claude-haiku-5-5", inp=50_000, out=0)), 0.005)
        self.assertAlmostEqual(usage.cost(row("claude-haiku-5-5", inp=200_000, out=0)), 0.1)  # the long-prompt rate
        self.assertAlmostEqual(usage.cost(row("claude-haiku-4-5-20251001", inp=1_000_000, read=1_000_000, out=0)), 1.1)
        self.assertAlmostEqual(usage.cost(row("claude-sonnet-4-6", inp=0, out=1_000_000)), 15)
        self.assertIsNone(usage.cost(row("claude-someday-9", inp=10)))

    # ---------------------------------------------------------------- the memory flag

    def test_breaches_end_at_a_compaction_or_a_call_back_under_the_limit(self):
        data = usage.parse(self.chat(OTHER, [
            call("a", 10, read=900), call("b", 9, read=1200), call("c", 8, read=1500), compacted(7.5),
            call("d", 7, read=1100), call("e", 6, read=200), call("f", 5, read=1050)]))
        found = usage.breaches(data, 1000)
        self.assertEqual([(b["start"], b["peak"]) for b in found], [(ts(9), 1510), (ts(7), 1110), (ts(5), 1060)])

    def test_the_supervisor_chat_and_its_builders_past_the_window_are_found_by_their_link(self):
        self.chat(CLI, [call("s1", 30, read=500), call("s2", 20, read=2000), call("s3", 19, read=2500), compacted(18)])
        self.builder(CLI, "agent-x1", [call("b1", 10, read=1500)], what="Build the export")
        self.builder(CLI, "agent-x2", [call("b2", 10, read=900)], what="Fine builder")
        self.chat(OTHER, [call("o1", 5, read=5000)])  # no list's supervisor: not this list's business
        link = f"claude://claude.ai/epitaxy/local_{SUP}"
        hits = usage.memory(self.s, "acme", "reports", link, now=NOW, apps=self.apps)
        self.assertEqual([(h["chat"], h["peak"]) for h in hits],
                         [('builder "Build the export"', 1510), ("supervisor chat", 2510)])
        # older than FLAG_DAYS: quiet again, with nothing to acknowledge
        self.assertEqual(usage.memory(self.s, "acme", "reports", link, now=NOW + timedelta(days=3), apps=self.apps), [])
        # no compact window in Claude's settings: nothing to measure against
        (self.claude / "settings.json").write_text("{}", encoding="utf-8")
        self.assertEqual(usage.memory(self.s, "acme", "reports", link, now=NOW, apps=self.apps), [])

    def test_wk_check_names_the_chat_in_plain_words(self):
        self.chat(CLI, [call("s1", 1, read=3000)])
        found = check._memory(self.s, "acme", "reports", f"local_{SUP}", now=NOW)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["kind"], "memory")
        self.assertIn("acme reports supervisor chat: went past the 1K memory limit without compacting (up to 3K",
                      check.line(found[0]))
        self.assertEqual(check._memory(self.s, "acme", "reports", "", now=NOW), [])

    # ---------------------------------------------------------------- the page

    def test_weekly_tables_by_model_role_and_repo_count_a_copied_call_once(self):
        # this week (Mon 2 Nov on): the supervisor and its builder; last week: another chat, in the repo's folder
        self.chat(CLI, [call("s1", 20, inp=1_000_000, out=0, cwd=str(self.app))])
        self.builder(CLI, "agent-x1", [call("b1", 10, model="claude-sonnet-5-5", inp=1_000_000, out=0)])
        self.chat(OTHER, [call("o1", 24 * 6, inp=1_000_000, out=0, cwd=str(self.app / "sub")),
                          call("o2", 24 * 6, model="claude-someday-9", inp=5)])
        self.chat("ffffffff-8888-7777-6666-555555555555", [call("s1", 20, inp=1_000_000, out=0)])  # a fork's copy
        view = usage.report(self.s, 2, now=NOW, apps=self.apps)
        this, last = view["weeks"][1], view["weeks"][0]
        self.assertEqual(datetime.fromisoformat(this).weekday(), 0)
        model = {(r["week"], r["key"]): r for r in view["tables"]["model"]}
        self.assertAlmostEqual(model[(this, "claude-opus-5-5")]["cost"], 4)
        self.assertEqual(model[(this, "claude-opus-5-5")]["calls"], 1)
        self.assertAlmostEqual(model[(this, "claude-sonnet-5-5")]["cost"], 2)
        self.assertFalse(model[(last, "claude-someday-9")]["priced"])
        self.assertEqual(view["unpriced"], ["claude-someday-9"])
        role = {(r["week"], r["key"]): r["cost"] for r in view["tables"]["role"]}
        self.assertEqual(role, {(this, "supervisor chats"): 4, (this, "builders"): 2, (last, "other"): 4})
        repo = {(r["week"], r["key"]) for r in view["tables"]["repo"]}
        self.assertEqual(repo, {(this, "acme"), (last, "acme")})
        page = usage.page(view)
        self.assertIn("API list prices", page)
        self.assertIn("no price", page)
        self.assertIn("Last week, same days", page)
        self.assertIn("All of last week", page)
        self.assertIn("33%", page)  # builders: $2 of this week's $6
        self.assertIn("<details>", page)  # the token counts sit in one closed section
        self.assertNotIn("<details open", page)

    def test_cost_by_token_type_is_each_types_own_price_and_adds_up_to_the_total(self):
        row = usage.parse(self.chat(OTHER, [call("m", 1, inp=1_000_000, read=1_000_000, write=2_000_000,
                                                 hour=1_000_000, out=1_000_000)]))["rows"]["m"]
        # Opus 5.5: $4 in, $0.20 cache read, cache writes $5 (5 min) + $8 (1 hour), $20 out
        split = usage.costs(row)
        for k, v in {"input": 4, "cache_read": 0.2, "cache_write": 13, "output": 20}.items():
            self.assertAlmostEqual(split[k], v)
        self.assertAlmostEqual(sum(split.values()), usage.cost(row))
        # two weeks of mixed calls, one fast, one with no price: per week, the four types add up to the week's cost
        self.chat(CLI, [call("s1", 20, inp=300_000, read=2_000_000, write=500_000, hour=200_000, out=40_000),
                        call("s2", 30, model="claude-sonnet-5-5", inp=10_000, out=90_000, fast=True)])
        self.chat(OTHER, [call("o1", 24 * 6, read=5_000_000, write=100_000, out=10_000),
                          call("o2", 24 * 6, model="claude-someday-9", inp=5)])
        view = usage.report(self.s, 2, now=NOW, apps=self.apps)
        for w in view["weeks"]:
            by_type = [r for r in view["tables"]["type"] if r["week"] == w]
            self.assertEqual(sorted(r["key"] for r in by_type), ["cache_read", "cache_write", "input", "output"])
            self.assertAlmostEqual(sum(r["cost"] for r in by_type),
                                   sum(r["cost"] for r in view["tables"]["model"] if r["week"] == w))
            self.assertEqual(sum(r["input"] + r["cache_read"] + r["cache_write"] + r["output"] for r in by_type),
                             sum(r["input"] + r["cache_read"] + r["cache_write"] + r["output"]
                                 for r in view["tables"]["model"] if r["week"] == w))
        this = {r["key"]: r for r in view["tables"]["type"] if r["week"] == view["weeks"][1]}
        self.assertAlmostEqual(this["output"]["cost"], 0.04 * 20 + 0.09 * 10 * 2)  # the fast call costs twice
        self.assertTrue(this["cache_write"]["priced"])
        last = {r["key"]: r for r in view["tables"]["type"] if r["week"] == view["weeks"][0]}
        self.assertFalse(last["input"]["priced"])  # the unpriced model's input
        self.assertTrue(last["cache_read"]["priced"])  # it had no cache reads: nothing left out
        page = usage.page(view)
        self.assertIn("By token type", page)
        for name in ("Input", "Output", "Cache read", "Cache write"):
            self.assertIn(f"<th class='name'>{name}</th>", page)

    def test_every_perspective_has_one_column_per_week_picked(self):
        self.chat(CLI, [call("s1", 20, inp=1_000_000, out=0)])
        self.chat(OTHER, [call("o1", 24 * 6, inp=1_000_000, out=0)])
        for n in (2, 4, 8, 13):
            view = usage.report(self.s, n, now=NOW, apps=self.apps)
            page = usage.page(view)
            self.assertEqual(page.count("<div class='persp'>"), 4)
            self.assertEqual(page.count("<th class='week'>"), 4 * n)
            # oldest on the left, this week (so far) on the right, each headed by its own total
            heads = page.split("<h3>By role</h3>")[1].split("</tr>")[0]
            self.assertTrue(heads.index("26 Oct") < heads.index("2 Nov"))
            self.assertIn("2 Nov – 8 Nov<b>$4.00<span class='pct'> so far</span></b>", heads)
            self.assertIn("26 Oct – 1 Nov<b>$4.00", heads)

    def test_this_week_is_compared_with_the_same_days_of_last_week(self):
        # Wednesday noon: last week up to Wednesday noon had $1, the rest of last week $4; this week so far $6
        self.chat(CLI, [call("s1", 20, inp=1_500_000, out=0)])
        self.chat(OTHER, [call("o1", 24 * 8, inp=250_000, out=0), call("o2", 24 * 6, inp=1_000_000, out=0)])
        view = usage.report(self.s, 2, now=NOW, apps=self.apps)
        self.assertAlmostEqual(view["last_week_to_date"]["cost"], 1)
        page = usage.page(view)
        self.assertIn("$6.00", page)
        self.assertIn("this week: +500%", page)  # against the same days, not the whole of last week
        self.assertIn("$5.00", page)

    def test_a_transcript_is_parsed_again_only_when_it_changed(self):
        path = self.chat(CLI, [call("s1", 1)])
        parsed, real = [], usage.parse
        usage.parse = lambda p: parsed.append(p) or real(p)
        try:
            self.records.read([path])
            usage.Records(self.tmp / "cache.json").read([path])  # a later run, from the cache file
            self.assertEqual(len(parsed), 1)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(call("s2", 0)) + "\n")
            self.assertEqual(set(self.records.read([path])[core.posix(path)]["rows"]), {"s1", "s2"})
            self.assertEqual(len(parsed), 2)
        finally:
            usage.parse = real


if __name__ == "__main__":
    unittest.main()
