"""nh check: every list against GitHub (a fake gh, a bare origin) and the rules; its two writes; and the record rules
that keep lists clean (typed dates, merged without a PR, questions tied to a piece)."""

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["NH_AUTO_UPDATE"] = "0"

from wk import check, cli, core, dashboard, settings, work  # noqa: E402

TODAY = date.today().isoformat()


def sh(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
                          encoding="utf-8").stdout.strip()


class Check(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.origin)], check=True, capture_output=True)
        self.app = self.tmp / "app"
        self.app.mkdir()
        sh(self.app, "init", "-b", "main")
        sh(self.app, "config", "user.name", "t")
        sh(self.app, "config", "user.email", "t@example.org")
        (self.app / "code.js").write_text("x\n", encoding="utf-8")
        sh(self.app, "add", "-A")
        sh(self.app, "commit", "-m", "init")
        sh(self.app, "remote", "add", "origin", str(self.origin))
        sh(self.app, "push", "-u", "origin", "main")
        self.settings = self.tmp / "settings.toml"
        self.settings.write_text(f'owner = "Pat Lee"\n\n[repos.acme]\npath = "{core.posix(self.app)}"\n',
                                 encoding="utf-8")
        self.s = settings.load(self.settings)
        self.prs, self.runs, self.steps = [], [], [{"name": "tests"}]
        self.real_gh = check._gh, dashboard._gh
        check._gh = dashboard._gh = lambda cwd, *a: json.dumps(
            self.runs if a[:2] == ("run", "list") else
            {"jobs": [{"databaseId": 1, "steps": self.steps}]} if a[:2] == ("run", "view") else
            [{"annotation_level": "failure", "message": "recent account payments have failed"}] if a[0] == "api" else
            self.prs)
        check._runs.clear()
        check._blocked.clear()
        self.wk("product", "add", "acme", "reports", "--line", "Monthly reports")
        self.wk("piece", "add", "acme", "reports", "Faster month end export")
        self.wk("piece", "note", "acme", "reports", "P1", "--file", str(self.brief()))

    def tearDown(self):
        check._gh, dashboard._gh = self.real_gh

    def brief(self, text="The export takes seconds.\n\nDone: the report downloads in under 5 seconds.\n"):
        path = self.tmp / "brief.md"
        path.write_text("# P1 · Faster month end export\n\n" + text, encoding="utf-8")
        return path

    def wk(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--settings", str(self.settings), "--cwd", str(self.app), *argv])
        return code, out.getvalue() + err.getvalue()

    def found(self, product=None):
        return check.check(self.s, "acme", product)

    def rows(self, **change):
        """Write the pieces file directly: older releases allowed what this one refuses."""
        path = self.app / ".work" / "reports" / "pieces.tsv"
        rows = work._read_tsv(path, work.PIECE_COLUMNS)
        rows[0].update(change)
        work._write_tsv(path, work.PIECE_COLUMNS, rows)

    def kinds(self, found):
        return sorted((p["item"], p["kind"]) for p in found)

    def test_a_clean_list_has_no_problems_and_exits_0(self):
        self.assertEqual(self.found(), [])
        code, out = self.wk("check", "acme", "reports")
        self.assertEqual((code, out.strip()), (0, "No problems found"))
        self.assertEqual(self.wk("--json", "check")[1].strip(), "[]")

    def test_states_names_and_notes_against_the_rules(self):
        self.rows(state="building", chat="agent:abc", piece="Export (was ABC-123) in export_job.py now runs much much faster")
        note = self.app / ".work" / "reports" / "notes" / "P1.md"
        future = (date.today() + timedelta(days=2)).isoformat()
        note.write_text("\ufeff# P1 · x\n\n- 2026-10-01: 2026-10-01: doubled\n- **2026-10-02:** bold\n"
                        f"- 2026-10-03 dash\n- {future}: tomorrow's\n- 2026-10-04: " + "y" * 700 + "\n",
                        encoding="utf-8")
        code, out = self.wk("check", "acme", "reports")
        self.assertEqual(code, 1)
        lines = out.strip().splitlines()
        self.assertTrue(any("10 words and a ticket number and a file name" in x for x in lines), out)
        self.assertTrue(any("1 note line(s) carry the date twice" in x and "--fix-dates" in x for x in lines), out)
        self.assertTrue(any("2 note line(s) dated in a form wk can't read" in x for x in lines), out)
        self.assertTrue(any("byte-order mark" in x for x in lines), out)
        self.assertTrue(any("1 note line(s) dated in the future. Fix: correct the dates" in x for x in lines), out)
        self.assertTrue(any("over 600 characters" in x for x in lines), out)
        self.assertEqual(lines[-1], f"{len(lines) - 1} problem(s)")
        # a finished piece's note is history: its tidiness is not reported; its other problems still are
        self.rows(state="merged", chat="")
        code, out = self.wk("check", "acme", "reports")
        self.assertIn("acme reports P1: is merged but has no PR. Fix: nh piece set acme reports P1 --state done",
                      out.splitlines())
        self.assertNotIn("note", [p["kind"] for p in self.found()])
        note.rename(note.with_suffix(".gone"))
        self.assertNotIn("has no note", [p["what"] for p in self.found()])
        note.with_suffix(".gone").rename(note)

        self.rows(state="done", piece="Node.js upgrade")
        self.assertNotIn("name", [p["kind"] for p in self.found()])
        self.rows(state="todo", piece="Faster export", pr="5", chat="agent:abc")
        self.prs = [{"number": 5, "state": "OPEN", "headRefName": "p1"}]
        self.assertIn(("P1", "state"), self.kinds(self.found()))
        self.assertIn("is todo but has a PR, a builder", " ".join(p["what"] for p in self.found()))
        self.rows(state="ready", pr="")
        self.assertIn("is ready but has no PR", [p["what"] for p in self.found()])
        self.rows(state="building", chat="https://chat")
        self.assertIn("is building but records no builder agent", [p["what"] for p in self.found()])
        self.rows(state="building", chat="agent:abc")
        self.prs = []
        note.unlink()
        self.assertEqual([(p["item"], p["what"]) for p in self.found()], [("P1", "has no note")])
        note.write_text("# P1 · x\n\n- 2026-10-01: started\n" + "z" * 9000 + "\n", encoding="utf-8")
        whats = [p["what"] for p in self.found()]
        self.assertIn("note has no brief before its first dated line", whats)
        self.assertTrue(any(w.startswith("note is 8 KB") or w.startswith("note is 9 KB") for w in whats), whats)

    def test_pieces_against_github(self):
        self.rows(state="building", pr="5", branch="gone", chat="agent:abc")
        self.prs = [{"number": 5, "state": "MERGED", "headRefName": "gone"},
                    {"number": 6, "state": "OPEN", "headRefName": "orphan-pr"}]
        self.assertEqual([(p["item"], p["what"]) for p in self.found()], [
            ("P1", "PR #5 is merged but the piece is still building"),
            ("P1", "records branch gone, which exists neither on GitHub nor here"),
            ("PR #6", "is open on orphan-pr but no piece records it")])
        self.rows(state="done", pr="5", branch="")
        self.prs[0]["state"] = "OPEN"
        self.assertIn("is done but PR #5 is still open", [p["what"] for p in self.found()])
        self.rows(state="building", pr="5")
        self.prs[0]["state"] = "CLOSED"
        self.assertIn("PR #5 was closed without merging but the piece is still building", [p["what"] for p in self.found()])
        self.rows(pr="9")
        self.assertIn("records PR #9, which GitHub doesn't have", [p["what"] for p in self.found()])

    def test_questions_hours_billing_and_branches(self):
        self.wk("question", "add", "acme", "reports", "P1: ship it?", "--pr", "5", "--piece", "P1")
        path = self.app / ".work" / "reports" / "questions.md"
        path.write_text(path.read_text(encoding="utf-8").replace(f"Asked: {TODAY}", "Asked: 2026-01-01"),
                        encoding="utf-8")
        self.prs = [{"number": 5, "state": "MERGED", "headRefName": "p1"}]
        self.rows(state="merged", pr="5")
        found = self.found()
        whats = [p["what"] for p in found if p["kind"] == "question"]
        # a long wait is a reminder for the supervisor, not a problem: the page shows the question's age instead
        self.assertTrue(any(p["what"].startswith("has waited") for p in found if p["kind"] == "reminder"), found)
        self.assertFalse(any(w.startswith("has waited") for w in whats), whats)
        self.assertIn("is about PR #5, which is merged", whats)
        self.assertIn("is about P1, which is merged", whats)
        self.assertIn("leads with a piece id instead of standing on its own", whats)

        self.wk("hours", "add", "acme", "reports", "2", "--note", "export work")
        (self.app / "billing").mkdir()
        (self.app / "billing" / "client.toml").write_text('calls_product = "calls"\n', encoding="utf-8")
        found = self.found()
        self.assertIn(("hours", f"{TODAY}", "2h names no piece"), [(p["kind"], p["item"], p["what"]) for p in found])
        self.assertIn(("billing", "has no coverage_from, so the invoice refuses"), [(p["kind"], p["what"]) for p in found])

        sh(self.app, "branch", "stray")
        sh(self.app, "push", "origin", "stray")
        sh(self.app, "branch", "worktree-agent-gone")
        sh(self.app, "push", "origin", "main:staging")
        items = [p["item"] for p in self.found() if p["kind"] == "git"]
        self.assertEqual(items, ["branch stray", "branch worktree-agent-gone"])  # staging is long-lived
        self.assertEqual(self.wk("check", "acme", "--ack", "branch:stray", "--reason", "Owner keeps it")[0], 0)
        self.assertEqual([p["item"] for p in self.found() if p["kind"] == "git"], ["branch worktree-agent-gone"])

    def test_repo_wide_problems_belong_to_one_list(self):
        self.prs = [{"number": 6, "state": "OPEN", "headRefName": "orphan-pr"}]
        self.assertEqual([check.line(p) for p in self.found("reports")], [
            "acme repo-wide (for reports) PR #6: is open on orphan-pr but no piece records it. "
            "Fix: nh piece set acme <list> <id> --pr 6 --branch orphan-pr"])
        self.wk("product", "add", "acme", "portal", "--line", "The customer portal")
        self.assertEqual(self.found("portal")[0]["owner"], "")  # two lists, neither supervised: unclear, both see it
        self.wk("product", "set", "acme", "portal", "--supervisor", "claude://x")
        self.assertEqual(self.found("reports"), [])  # only the supervised list acts on it
        self.assertEqual(self.found("portal")[0]["owner"], "portal")
        self.wk("product", "add", "acme", "calls", "--line", "Calls")
        (self.app / "billing").mkdir()
        (self.app / "billing" / "client.toml").write_text('calls_product = "calls"\ncoverage_from = "2026-10-01"\n',
                                                         encoding="utf-8")
        self.assertEqual(self.found("portal"), [])
        self.assertEqual([p["item"] for p in self.found("calls")], ["PR #6"])  # the project manager's list

    def test_spreadsheets_and_binaries_outside_docs_are_flagged_and_never_removed(self):
        files = {"data/Report.XLSX": b"PK\x03\x04\x00x", "export.csv": b"a,b\n1,2\n", "~$draft.docx": b"x",
                 "logo.png": b"\x89PNG\x00\x01", "docs/design/mock.png": b"\x89PNG\x00\x02", "notes.md": b"text\n",
                 "billing/INV-0001.csv": b'"date","hours"\n'}  # an invoice statement made before 1.30, already in git
        for name, data in files.items():
            (self.app / name).parent.mkdir(parents=True, exist_ok=True)
            (self.app / name).write_bytes(data)
        sh(self.app, "add", "-A")
        sh(self.app, "commit", "-m", "files")
        sh(self.app, "push", "origin", "main")
        found = {p["item"]: p for p in self.found() if p["kind"] == "files"}
        self.assertEqual(set(found), {"data/Report.XLSX", "export.csv", "~$draft.docx", "logo.png"})
        self.assertIn("(*.xlsx)", found["data/Report.XLSX"]["what"])
        self.assertIn("a binary file outside docs/", found["logo.png"]["what"])
        self.assertIn('rm --cached -- "export.csv" (keeps your copy)', check.line(found["export.csv"]))
        self.assertIn("acme repo-wide (for reports) export.csv: ", check.line(found["export.csv"]))
        self.assertIn("export.csv", sh(self.app, "ls-files"))  # the check deletes nothing
        self.assertTrue((self.app / "export.csv").is_file())
        self.assertIn('--ack "file:logo.png"', found["logo.png"]["fix"])  # an image may be allowed, one by one
        self.assertNotIn("--ack", found["export.csv"]["fix"])
        code, out = self.wk("check", "acme", "--ack", "file:logo.png", "--reason", "visual test snapshot")
        self.assertEqual(code, 0, out)
        found = {p["item"] for p in self.found() if p["kind"] == "files"}
        self.assertEqual(found, {"data/Report.XLSX", "export.csv", "~$draft.docx"})  # only the allowed file goes
        self.assertNotEqual(self.wk("check", "acme", "--ack", "file:logo.png", "--reason", "again")[0], 0)
        self.settings.write_text(self.settings.read_text(encoding="utf-8") + 'base = "trunk"\n', encoding="utf-8")
        self.s = settings.load(self.settings)  # a base branch that exists neither here nor on GitHub
        blind = [p for p in self.found() if p["kind"] == "files"]
        self.assertEqual(len(blind), 1)
        self.assertIn("its files on trunk could not be looked at", check.line(blind[0]))

    def test_code_on_main_without_a_pr_alerts_until_the_owner_acknowledges_it(self):
        (self.app / "code.js").write_text("changed\n", encoding="utf-8")
        sh(self.app, "commit", "-am", "hot fix")
        sh(self.app, "push", "origin", "main")
        sha = sh(self.app, "rev-parse", "HEAD")
        (self.app / "billing").mkdir(exist_ok=True)
        (self.app / "billing" / "ACM-INV-001.md").write_text("# ACM-INV-001\n", encoding="utf-8")
        (self.app / "billing" / "ledger.json").write_text("{}\n", encoding="utf-8")
        sh(self.app, "add", "-A")
        sh(self.app, "commit", "-m", "acme ACM-INV-001: 2h")  # the toolkit's own files, as make and sent commit: allowed
        flagged = []  # each with the ledger, still flagged: code, code in billing/, a folder below billing/
        for n, name in enumerate(("code.js", "billing/export.py", "billing/old/ACM-INV-001.md")):
            (self.app / "billing" / "ledger.json").write_text(f'{{"n": {n}}}\n', encoding="utf-8")
            (self.app / name).parent.mkdir(parents=True, exist_ok=True)
            (self.app / name).write_text(f"changed {n}\n", encoding="utf-8")
            sh(self.app, "add", "-A")
            sh(self.app, "commit", "-m", f"billing and {name}")
            flagged.append(sh(self.app, "rev-parse", "HEAD")[:7])
        sh(self.app, "push", "origin", "main")
        self.assertEqual({p["item"] for p in self.found() if p["kind"] == "main"}, {sha[:7], *flagged})
        sh(self.app, "reset", "-q", "--hard", "HEAD~4")  # back to before billing/, which has checks of its own
        sh(self.app, "push", "-q", "-f", "origin", "main")
        main = [p for p in self.found() if p["kind"] == "main"]
        self.assertEqual([(p["item"], p["what"]) for p in main], [(sha[:7], "reached main without a PR: hot fix")])
        self.assertEqual(self.found("reports"), main)  # a list's check shows its repo's shared problems too
        self.prs = [{"number": 3, "state": "MERGED", "headRefName": "x", "mergeCommit": {"oid": sha}}]
        self.assertEqual([p for p in self.found() if p["kind"] == "main"], [])  # it came through a PR after all

        self.prs = []
        view = {"products": [], "alerts": [], "path": "x"}
        dashboard._checks(self.s, "acme", "main", view, fetch=True)
        self.assertTrue(view["alerts"] and "code reached main without a PR" in view["alerts"][0], view)
        self.assertEqual(self.wk("check", "acme", "--ack", sha[:9])[0], 2)  # needs a reason
        self.assertEqual(self.wk("check", "acme", "--ack", sha[:9], "--reason", "Owner: fine, a typo fix")[0], 0)
        self.assertEqual([p for p in self.found() if p["kind"] == "main"], [])
        self.assertIn(sha, sh(self.origin, "show", "main:.work/acknowledged.tsv"))

    def test_a_list_kept_in_its_own_checkout_gets_the_same_main_branch_audit(self):
        lorigin, lst = self.tmp / "list-origin.git", self.tmp / "list"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(lorigin)], check=True, capture_output=True)
        subprocess.run(["git", "clone", "-q", str(lorigin), str(lst)], check=True, capture_output=True)
        for args in (("config", "user.name", "t"), ("config", "user.email", "t@example.org"),
                     ("commit", "-q", "--allow-empty", "-m", "init"), ("push", "-q", "origin", "HEAD:main")):
            sh(lst, *args)
        self.settings.write_text(f'owner = "Pat Lee"\n\n[repos.acme]\npath = "{core.posix(self.app)}"\n'
                                 f'list = "{core.posix(lst)}"\n', encoding="utf-8")
        self.s = settings.load(self.settings)
        self.wk("product", "add", "acme", "ops", "--line", "Ops")
        self.wk("piece", "add", "acme", "ops", "Tidy the exports")  # records only: never flagged
        self.assertEqual([p for p in self.found() if p["kind"] == "main"], [])
        (lst / "tool.py").write_text("x\n", encoding="utf-8")
        sh(lst, "add", "-A")
        sh(lst, "commit", "-q", "-m", "code in the list repo")
        sh(lst, "push", "-q", "origin", "main")
        sha = sh(lst, "rev-parse", "HEAD")
        main = [p for p in self.found() if p["kind"] == "main"]
        self.assertEqual([p["item"] for p in main], [sha[:7]])
        self.assertIn(f"of its list ({core.posix(lst)}) without a PR: code in the list repo", main[0]["what"])
        self.assertEqual(self.wk("check", "acme", "--ack", sha[:9], "--reason", "Owner: fine")[0], 0)
        self.assertEqual([p for p in self.found() if p["kind"] == "main"], [])

    def test_a_parked_lists_findings_wait_for_a_supervisor_and_count_nowhere(self):
        (self.app / ".work" / "reports" / "notes" / "P1.md").unlink()  # an open piece's finding: has no note
        (self.app / "code.js").write_text("changed\n", encoding="utf-8")
        sh(self.app, "commit", "-am", "hot fix")  # a repo-wide finding the only list owns, and an alert
        sh(self.app, "push", "origin", "main")
        for supervisor, waits in (("", True), ("https://claude.ai/chat/1", False)):
            view = {"products": [{"product": "reports", "supervisor": supervisor}], "alerts": [], "path": "p"}
            dashboard._checks(self.s, "acme", "main", view, fetch=True)
            fixes = view["products"][0]["fixes"] + [f for f in view["fixes"] if f["owner"] == "reports"]
            self.assertEqual({(f["kind"], f["waits"]) for f in fixes}, {("note", waits), ("main", waits)})
            self.assertEqual(bool(view["alerts"]), not waits, view["alerts"])  # parked: no alert either
        self.assertIn("f.waits", dashboard.PAGE.read_text(encoding="utf-8"))  # the page reads the mark

    def test_fix_dates_rewrites_one_lists_dates_once_in_one_commit(self):
        note = self.app / ".work" / "reports" / "notes" / "P1.md"
        note.write_text("\ufeff# P1 · x\n\nBrief.\n- 2026-09-01 cutover\n- 2026-10-15 to 2026-10-20 import window\n\n"
                        "- 2026-10-01: 2026-10-01: doubled\n- 2026-10-02: **2026-10-01:** "
                        "typed another day\n- **2026-10-03:** bold\n- 2026-10-04 — dash\n- 2026-10-05: fine\n"
                        "  - 2026-10-06 nested, left alone\n- 2026-10-07 – 2026-10-09 freeze\n", encoding="utf-8")
        before = sh(self.app, "rev-list", "--count", "HEAD")
        code, out = self.wk("check", "acme", "reports", "--fix-dates")
        self.assertEqual(code, 0, out)
        self.assertEqual(note.read_text(encoding="utf-8"),
                         "# P1 · x\n\nBrief.\n- 2026-09-01 cutover\n- 2026-10-15 to 2026-10-20 import window\n\n"
                         "- 2026-10-01: doubled\n- 2026-10-02: (2026-10-01) typed another day\n"
                         "- 2026-10-03: bold\n- 2026-10-04: dash\n- 2026-10-05: fine\n  - 2026-10-06 nested, left alone\n"
                         "- 2026-10-07 – 2026-10-09 freeze\n")
        self.assertEqual(int(sh(self.app, "rev-list", "--count", "HEAD")), int(before) + 1)
        self.assertEqual(self.found(), [])
        self.assertIn("nothing to change", self.wk("check", "acme", "reports", "--fix-dates")[1])
        self.assertEqual(self.wk("check", "acme", "--fix-dates")[0], 2)  # one list at a time

    def test_a_typed_date_is_dropped_from_a_note_line(self):
        for typed in (f"{TODAY}: shipped", f"**{TODAY}:** shipped", f"{TODAY} - shipped", "shipped",
                      "2026-01-05: owner said yes"):
            self.wk("piece", "note", "acme", "reports", "P1", "--add", typed)
        lines = (self.app / ".work" / "reports" / "notes" / "P1.md").read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[-5:], [f"- {TODAY}: shipped"] * 4 + [f"- {TODAY}: (2026-01-05) owner said yes"])
        self.assertEqual(work.strip_date("2026-10-30"), "2026-10-30")  # nothing but a date is kept as it is

    def test_a_push_whose_rebase_fails_is_undone(self):
        other = self.tmp / "other"
        subprocess.run(["git", "clone", "--quiet", str(self.origin), str(other)], check=True, capture_output=True)
        sh(other, "config", "user.name", "t")
        sh(other, "config", "user.email", "t@example.org")
        (other / ".work" / "reports" / "pieces.tsv").write_text("conflict\n", encoding="utf-8")
        sh(other, "commit", "-qam", "elsewhere")
        sh(other, "push", "origin", "main")
        code, out = self.wk("piece", "add", "acme", "reports", "Another piece")
        self.assertEqual(code, 3, out)
        self.assertIn("pull --rebase failed and was undone", out)
        self.assertFalse(core.git_path(self.app, "rebase-merge").exists() or core.git_path(self.app, "rebase-apply").exists())
        self.assertEqual(sh(self.app, "symbolic-ref", "--short", "HEAD"), "main")

    def test_merged_needs_a_pr(self):
        code, out = self.wk("piece", "set", "acme", "reports", "P1", "--state", "merged")
        self.assertEqual(code, 2)
        self.assertIn("nh piece set acme reports P1 --state done", out)
        self.assertEqual(self.wk("piece", "set", "acme", "reports", "P1", "--state", "merged", "--pr", "4")[0], 0)

    def test_every_question_names_its_piece_or_the_whole_list(self):
        code, out = self.wk("question", "add", "acme", "reports", "Ship it?")
        self.assertEqual(code, 2)
        self.assertIn("--list-wide", out)
        self.assertEqual(self.wk("question", "add", "acme", "reports", "Ship it?", "--piece", "P1", "--list-wide")[0], 2)
        self.assertEqual(self.wk("question", "add", "acme", "reports", "Hire a second reviewer?", "--list-wide")[0], 0)
        path = self.app / ".work" / "reports" / "questions.md"
        self.assertIn("- Scope: whole list", path.read_text(encoding="utf-8"))
        # one written by an older nearly-headless names no piece: nh check flags it, and nh question set ties it to one
        path.write_text(path.read_text(encoding="utf-8") + "## Q2 · Start a builder on the export?\n- Asked: "
                        f"{TODAY}\n", encoding="utf-8")
        found = [p for p in self.found() if p["kind"] == "question"]
        self.assertEqual([(p["item"], p["what"]) for p in found], [("Q2", "isn't tied to a piece yet")])
        self.assertIn("nh question set acme reports Q2 --piece", found[0]["fix"])
        self.assertEqual(self.wk("question", "set", "acme", "reports", "Q2")[0], 2)  # needs --piece
        self.assertEqual(self.wk("question", "set", "acme", "reports", "Q9", "--piece", "P1")[0], 2)  # no such question
        self.assertEqual(self.wk("question", "set", "acme", "reports", "Q2", "--piece", "P9")[0], 2)  # no such piece
        code, out = self.wk("question", "set", "acme", "reports", "q2", "--piece", "p1")
        self.assertEqual(code, 0, out)
        self.assertIn("## Q2 · Start a builder on the export?\n- Piece: P1", sh(self.origin, "show", "main:.work/reports/questions.md"))
        self.assertEqual([p for p in self.found() if p["kind"] == "question"], [])
        # one about the whole list is marked so without a made-up answer
        self.assertEqual(self.wk("question", "set", "acme", "reports", "Q2", "--piece", "P1", "--list-wide")[0], 2)
        code, out = self.wk("question", "set", "acme", "reports", "Q2", "--list-wide")
        self.assertEqual(code, 0, out)
        self.assertIn("## Q2 · Start a builder on the export?\n- Scope: whole list",
                      sh(self.origin, "show", "main:.work/reports/questions.md"))
        self.assertEqual([p for p in self.found() if p["kind"] == "question"], [])
        self.assertEqual(self.wk("question", "set", "acme", "reports", "Q2", "--piece", "P1")[0], 0)  # and back
        code, out = self.wk("question", "answer", "acme", "reports", "Q2", "yes")
        self.assertIn("Owner answered Q2", sh(self.origin, "show", "main:.work/reports/notes/P1.md"))

    def test_a_long_wait_is_a_reminder_not_a_problem(self):
        self.wk("question", "add", "acme", "reports", "Ship the faster export?", "--piece", "P1")
        path = self.app / ".work" / "reports" / "questions.md"
        path.write_text(path.read_text(encoding="utf-8").replace(f"Asked: {TODAY}", "Asked: 2026-01-01"), encoding="utf-8")
        code, out = self.wk("check", "acme", "reports")
        self.assertEqual(code, 0, out)  # only a reminder: nh check finds no problem
        self.assertIn("No problems found\nReminders (not problems):\nacme reports Q1: has waited", out)
        view = dashboard.overview(self.s)
        self.assertEqual(view["repos"][0]["products"][0]["fixes"], [])  # nothing red on the page

    def test_answering_a_question_about_a_piece_writes_its_note(self):
        self.assertEqual(self.wk("question", "add", "acme", "reports", "Ship it?", "--piece", "P9")[0], 2)
        self.wk("question", "add", "acme", "reports", "Ship the faster export?", "--piece", "p1")
        self.assertIn("- Piece: P1", (self.app / ".work" / "reports" / "questions.md").read_text(encoding="utf-8"))
        code, out = self.wk("question", "answer", "acme", "reports", "Q1", "yes, this week")
        self.assertEqual(code, 0, out)
        note = sh(self.origin, "show", "main:.work/reports/notes/P1.md")
        self.assertTrue(note.endswith(f"- {TODAY}: Owner answered Q1 (Ship the faster export?): yes, this week"), note)
        self.assertEqual(work.questions(work.Repo(self.s, "acme"), "reports"), [])

    # ---------------------------------------------------------------- the health checks added in 1.35.0

    def run_(self, i, branch, conclusion, sha="s1", status="completed", workflow=""):
        return {"databaseId": i, "status": status, "conclusion": conclusion, "headBranch": branch, "headSha": sha,
                "workflowName": workflow, "url": f"https://x/runs/{i}", "createdAt": "2026-11-01T10:00:00Z"}

    def test_failing_tests_are_read_per_workflow_and_kept_while_a_new_run_goes(self):
        (self.app / ".github" / "workflows").mkdir(parents=True)
        (self.app / ".github" / "workflows" / "tests.yml").write_text("name: tests\n", encoding="utf-8")
        tests = lambda **kw: [p["item"] for p in check.repo_problems(self.s, "acme", **kw)  # noqa: E731
                              if p["kind"] == "tests"]
        cases = [
            ([self.run_(2, "main", "failure", workflow="deploy"), self.run_(1, "main", "success", workflow="tests")],
             []),  # a deploy that failed is not the tests
            ([self.run_(2, "main", "success", workflow="tests"), self.run_(1, "main", "failure", workflow="lint")],
             ["main"]),  # lint's newest run failed, though the tests' newest passed
            ([self.run_(3, "main", "", status="in_progress", workflow="CI"), self.run_(2, "main", "failure", workflow="CI"),
              self.run_(1, "main", "success", workflow="CI")],
             ["main"]),  # a new run still going leaves the last failure standing
            ([self.run_(2, "main", "success", workflow="CI"), self.run_(1, "main", "failure", workflow="CI")], []),
            ([self.run_(1, "main", "failure", workflow="Python application")], ["main"]),  # any name but a deploy's
            ([self.run_(1, "main", "failure", workflow=".github/workflows/ci.yml")], ["main"]),  # named by its path
            ([self.run_(1, "main", "failure", workflow="Publish to PyPI")], []),
        ]
        for found, expected in cases:
            check._runs.clear()
            self.runs = found
            self.assertEqual(tests(), expected, found)
        self.runs = [self.run_(5, "main", "failure", workflow="tests")]
        self.assertEqual(tests(), [])  # read a moment ago: nh check waits for its next read
        check._runs.update({k: (v[0] - check.FRESH - 1, v[1]) for k, v in check._runs.items()})
        self.assertEqual(tests(fetch=True), ["main"])  # Check GitHub now reads afresh
        # on the page: the "CI failing" alert gives way to the finding, but only when there is one
        alerts = lambda: [a for a in dashboard.overview(self.s)["alerts"] if "CI failing" in a]  # noqa: E731
        dashboard._checked.clear(), dashboard._ci_runs.clear()
        self.assertEqual(alerts(), [])
        check._runs.clear(), dashboard._checked.clear(), dashboard._ci_runs.clear()
        self.runs = [self.run_(6, "main", "failure", workflow="deploy")]
        self.assertEqual(alerts(), ["acme: CI failing on main"])

    def test_failing_tests_on_main_or_on_a_pr_waiting_for_the_owner(self):
        (self.app / ".github" / "workflows").mkdir(parents=True)
        (self.app / ".github" / "workflows" / "tests.yml").write_text("name: tests\n", encoding="utf-8")
        self.rows(state="ready", pr="7", branch="p1-export")
        self.prs = [{"number": 7, "state": "OPEN", "headRefName": "p1-export", "headRefOid": "s2"}]
        tests = lambda: [(p["product"], p["item"], p["what"], p["name"]) for p in self.found()  # noqa: E731
                         if p["kind"] == "tests"]
        self.runs = [self.run_(3, "p1-export", "failure", "s2"), self.run_(2, "main", "failure"),
                     self.run_(1, "main", "success")]
        self.assertEqual(tests(), [
            ("reports", "P1", "waits for the owner's yes, but the tests on PR #7 failed", "Faster month end export"),
            ("", "main", "the newest tests on main failed", "")])
        check._runs.clear()
        self.runs = [self.run_(5, "p1-export", "", "s2", "in_progress"), self.run_(4, "p1-export", "failure", "s1"),
                     self.run_(3, "main", "skipped"), self.run_(2, "main", "success"), self.run_(1, "main", "failure")]
        self.assertEqual(tests(), [])  # still running on the PR, and main passed since
        check._runs.clear()
        self.runs = [self.run_(4, "p1-export", "failure", "s1"), self.run_(3, "other", "failure")]
        self.assertEqual(tests(), [])  # a run on an older commit says nothing; nor does another branch
        check._runs.clear()
        self.steps = []  # GitHub refused to start the jobs: billing, not the tests
        self.runs = [self.run_(9, "main", "failure")]
        self.assertEqual(tests(), [])
        self.rows(state="building", pr="7", chat="agent:a1")
        check._runs.clear()
        self.steps, self.runs = [{"name": "tests"}], [self.run_(8, "p1-export", "failure", "s2")]
        self.assertEqual(tests(), [])  # not waiting for the owner yet

    def test_a_pr_waiting_for_the_owner_needs_mains_changes_to_its_own_files(self):
        def commit(name, text):
            (self.app / name).write_text(text, encoding="utf-8")
            sh(self.app, "add", "-A")
            sh(self.app, "commit", "-q", "-m", f"change {name}")

        sh(self.app, "checkout", "-q", "-b", "p1-export")
        commit("code.js", "the PR's edit\n")
        head = sh(self.app, "rev-parse", "HEAD")
        sh(self.app, "checkout", "-q", "main")
        self.rows(state="ready", pr="7", branch="p1-export")
        self.prs = [{"number": 7, "state": "OPEN", "headRefName": "p1-export", "headRefOid": head}]
        behind = lambda: [(p["item"], p["what"], p["name"]) for p in self.found()  # noqa: E731
                          if p["kind"] == "behind"]
        self.assertEqual(behind(), [])  # main has only recorded work since
        sh(self.app, "push", "-q", "origin", "main:stack")  # a branch a stacked PR merges into, left behind
        commit("other.js", "x\n")
        sh(self.app, "push", "-q", "origin", "main")
        self.assertEqual(behind(), [])  # main moved on other files: no wait
        commit("code.js", "main's edit\n")
        sh(self.app, "push", "-q", "origin", "main")
        self.assertEqual(behind(), [("P1", "waits for the owner's yes, but main has since changed code.js, which PR #7 "
                                           "also changes", "Faster month end export")])
        self.assertIn("Needs the latest main", check.KINDS["behind"][0])
        self.prs[0]["baseRefName"] = "stack"
        self.assertEqual(behind(), [])  # judged against the branch it merges into, which hasn't moved
        self.prs[0]["baseRefName"] = "main"
        self.rows(state="building", pr="7", chat="agent:a1")
        self.assertEqual(behind(), [])  # not waiting for the owner yet
        self.rows(state="ready", pr="7", chat="")
        sh(self.app, "checkout", "-q", "p1-export")
        sh(self.app, "merge", "-q", "-X", "theirs", "origin/main")  # the builder takes main
        self.prs[0]["headRefOid"] = sh(self.app, "rev-parse", "HEAD")
        sh(self.app, "checkout", "-q", "main")
        self.assertEqual(behind(), [])

    def test_possible_secrets_name_the_file_and_line_never_the_value(self):
        # built here, so this file holds none of them
        key, word, long = "AKIA" + "QX7RT2LMNB4VZP3W", "hunter" + "2hunter2", "Zq8vT3nLw2" + "Kp9Xr4Ms6Yb1"
        slashed = "Zq8vT3nLw2/Kp9Xr4Ms6" + "Yb1+Hd5Fg7Jk0Qw3ErTy8Ui"
        files = {
            # likely secrets
            "config.py": f"region = 'eu'\naws = '{key}'\n",
            "app/db.js": f"const db = {{ password: '{word}' }}\n",
            "settings.py": f'DB_PASSWORD = "{word}"\nSECRET_KEY = "{long}"\n',  # named with a prefix or a suffix
            "app/pay.js": f'const STRIPE_SECRET = "{long}";\n',
            "app/aws.js": f'const awsSecretAccessKey = "{slashed}";\n',  # base64, so it may hold a /
            "config/aws.ini": f"AWS_SECRET_ACCESS_KEY={long}\n",  # unquoted, in a settings file
            "deploy/app.yaml": f"password: {word}\n",
            ".env": f"API_TOKEN={long}\n",  # a committed .env is reported whole
            # not secrets: test, docs and example files
            ".env.example": f"API_TOKEN={long}\n",
            "tests/fixtures/login.js": f"password = '{word}'\n",
            "tests/keys.py": f"aws = '{key}'\n",
            "tools/test_query.py": f"token = '{long}'\n",
            "docs/setup.txt": f"password: \"{word}\"\n",
            "README.md": f"password: \"{word}\"\n",
            "notes.md": "password: \"changeme-later\"\n",
            "config.example.yaml": f"password: {word}\n",
            "app.template.json": f'{{"api_key": "{long}"}}\n',
            # not secrets: placeholders, words, paths and expressions
            "src/stub.py": "\n".join(['access_key_id = "AKIA' + 'IOSFODNN7EXAMPLE"', 'api_key = "xxxxXXXXxxxx"',
                                      'password = "********"', 'token = "000000000000"', 'api_key = "your-api-key-here"',
                                      'password = "<password>"', 'password = "changeme"', 'label = {password: "Passwort"}',
                                      'rules = {password: "required"}', 'secret_file = "/run/secrets/db"',
                                      "password = get_password()",
                                      "notes = {'resetPassword': 'emails the user a link to set a new one'}"]) + "\n",
            "deploy.toml": "secret = \"env(DEPLOY_SECRET)\"\n",
            "Form.tsx": "autoComplete={signup ? 'new-password' : 'current-password'}\n",
        }
        for path, text in files.items():
            (self.app / path).parent.mkdir(parents=True, exist_ok=True)
            (self.app / path).write_text(text, encoding="utf-8")
        sh(self.app, "add", "-A")
        sh(self.app, "add", "-f", ".env", ".env.example")
        sh(self.app, "commit", "-m", "config")
        sh(self.app, "push", "origin", "main")
        found = [p for p in self.found() if p["kind"] == "secret"]
        named = "may hold a password or key"
        self.assertEqual(sorted((p["item"], p["what"]) for p in found), [
            (".env", "is a committed .env file, which usually holds keys and passwords"),
            ("app/aws.js line 1", named), ("app/db.js line 1", named), ("app/pay.js line 1", named), ("config.py line 2", "may hold an AWS key"),
            ("config/aws.ini line 1", named), ("deploy/app.yaml line 1", named),
            ("settings.py line 1", named), ("settings.py line 2", named)])
        self.assertFalse(any(v in json.dumps(p) for p in found for v in (key, word, long, slashed)))
        self.assertEqual(self.wk("check", "acme", "--ack", "secret:app/db.js", "--reason", "a sample, not real")[0], 0)
        self.assertNotIn("app/db.js line 1", [p["item"] for p in self.found() if p["kind"] == "secret"])

    def test_a_todo_untouched_for_a_month_is_flagged_until_something_happens(self):
        old = (date.today() - timedelta(days=check.TODO_DAYS + 5)).isoformat()
        self.rows(updated=old)
        stale = lambda: [(p["item"], p["name"], p["what"]) for p in self.found() if p["kind"] == "stale"]  # noqa: E731
        self.assertEqual(stale(), [("P1", "Faster month end export",
                                    f"has been a to-do with no change or note line for {check.TODO_DAYS + 5} days")])
        self.rows(state="building", chat="agent:a1")
        self.assertEqual(stale(), [])  # only a to-do goes stale
        self.rows(state="todo", chat="")
        note = self.app / ".work" / "reports" / "notes" / "P1.md"
        note.write_text(note.read_text(encoding="utf-8") + f"\n- {TODAY}: Parked: waits for the new export\n",
                        encoding="utf-8")
        self.assertEqual(stale(), [])  # a dated note line counts as a change

    def test_instruction_files_over_their_word_budget(self):
        words = lambda n: "word " * n  # noqa: E731
        (self.app / "skills" / "deploy").mkdir(parents=True)
        (self.app / ".claude" / "skills" / "close").mkdir(parents=True)
        (self.app / "AGENTS.md").write_text(words(check.WORD_BUDGET["instructions"] + 1), encoding="utf-8")
        (self.app / "CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8")
        (self.app / "skills" / "deploy" / "SKILL.md").write_text(words(check.WORD_BUDGET["skill"]), encoding="utf-8")
        (self.app / "skills" / "deploy" / "steps.md").write_text(words(check.WORD_BUDGET["reference"] + 10),
                                                                  encoding="utf-8")
        (self.app / ".claude" / "skills" / "close" / "SKILL.md").write_text(words(2000), encoding="utf-8")
        (self.app / "docs").mkdir()
        (self.app / "docs" / "guide.md").write_text(words(5000), encoding="utf-8")  # docs are read only when needed
        self.assertEqual([(p["item"], p["what"]) for p in self.found() if p["kind"] == "bloat"], [
            ("AGENTS.md", "is 1,501 words (budget 1,500)"),
            ("skills/deploy/steps.md", "is 1,010 words (budget 1,000)"),
            (".claude/skills/close/SKILL.md", "is 2,000 words (budget 1,500)")])

    def test_look_alike_pieces_in_the_same_repo(self):
        self.wk("product", "add", "acme", "billing", "--line", "Invoices")
        for product, name in (("reports", "Faster month-end exports"), ("reports", "Monthly invoice email"),
                              ("billing", "Month end export runs faster"), ("billing", "Faster invoice approvals")):
            self.wk("piece", "add", "acme", product, name)
        alike = lambda only=None: [(p["product"], p["item"], p["what"]) for p in self.found(only)  # noqa: E731
                                   if p["kind"] == "alike"]
        self.assertEqual(alike(), [
            ("reports", "P1", 'looks like P1 "Month end export runs faster" on the billing list'),
            ("reports", "P2", 'looks like P1 "Month end export runs faster" on the billing list'),
            ("reports", "P2", 'looks like P1 "Faster month end export"')])
        self.assertEqual(len(alike("billing")), 0)  # each pair shows once, on the list of its later piece
        self.wk("piece", "set", "acme", "reports", "P2", "--state", "dropped")
        self.assertEqual(len(alike()), 1)  # a dropped piece is not open


    # ------------------------------------------------------------ refused at write time, fixed by --fix

    def test_a_name_check_would_flag_is_refused_when_written(self):
        for name in ("one two three four five six seven eight nine", "Fix ABC-123 now", "Speed up export_job.py"):
            code, out = self.wk("piece", "add", "acme", "reports", name)
            self.assertEqual(code, 2, name)
            self.assertIn("plain words, 8 at most", out)
            code, out = self.wk("piece", "set", "acme", "reports", "P1", "--name", name)
            self.assertEqual(code, 2, name)
        self.assertEqual(self.wk("piece", "add", "acme", "reports", "one two three four five six seven eight")[0], 0)
        self.assertEqual(self.wk("piece", "set", "acme", "reports", "P1", "--name", "Node.js upgrade")[0], 0)
        self.assertEqual([p["piece"] for p in work.pieces(work.Repo(self.s, "acme"), "reports")],
                         ["Node.js upgrade", "one two three four five six seven eight"])

    def test_a_note_line_check_would_flag_is_refused_when_written(self):
        note = self.app / ".work" / "reports" / "notes" / "P1.md"
        before = note.read_text(encoding="utf-8")
        fits = "y" * (check.LINE_CHARS - len(f"- {TODAY}: "))  # the whole line, its date included, as check counts it
        code, out = self.wk("piece", "note", "acme", "reports", "P1", "--add", fits + "y")
        self.assertEqual(code, 2)
        self.assertIn("the detail belongs in the PR or a finding", out)
        self.assertEqual(note.read_text(encoding="utf-8"), before)
        long = self.brief("The export.\n\n- 2026-10-04: " + "y" * 700 + "\n" + "z" * 601 + "\n")
        code, out = self.wk("piece", "note", "acme", "reports", "P1", "--file", str(long))
        self.assertEqual(code, 2)
        self.assertIn("2 line(s) of that note are over 600 characters", out)
        self.assertEqual(note.read_text(encoding="utf-8"), before)
        self.assertEqual(self.wk("piece", "note", "acme", "reports", "P1", "--add", fits)[0], 0)
        self.assertEqual(check.long_lines(note.read_text(encoding="utf-8")), 0)
        self.assertEqual(self.found(), [])  # what wk wrote, nh check passes

    def test_fix_makes_only_the_mechanical_fixes(self):
        self.wk("piece", "add", "acme", "reports", "Second piece")
        self.wk("piece", "add", "acme", "reports", "Third piece")
        self.wk("piece", "add", "acme", "reports", "Fourth piece")
        path = self.app / ".work" / "reports" / "pieces.tsv"
        rows = work._read_tsv(path, work.PIECE_COLUMNS)
        rows[0].update(state="ready", pr="5", branch="p1")              # PR merged: becomes merged
        rows[1].update(state="building", branch="gone", chat="agent:b")  # branch gone: cleared
        rows[2].update(state="ready", pr="7, 8", branch="p3")           # one PR merged, a newer open: left
        rows[3].update(piece="A name with far too many words in it now")  # judgment: left
        work._write_tsv(path, work.PIECE_COLUMNS, rows)
        sh(self.app, "add", "-A")
        sh(self.app, "commit", "-m", "rows")
        sh(self.app, "push", "origin", "main")
        for b in ("p1", "p3"):
            sh(self.app, "push", "origin", f"main:{b}")
        self.prs = [{"number": 5, "state": "MERGED", "headRefName": "p1"},
                    {"number": 7, "state": "MERGED", "headRefName": "p3"},
                    {"number": 8, "state": "OPEN", "headRefName": "p3"}]
        self.wk("question", "add", "acme", "reports", "Old question?", "--piece", "P2")
        (self.app / ".work" / "reports" / "questions.md").write_text(
            (self.app / ".work" / "reports" / "questions.md").read_text(encoding="utf-8").replace("Piece: P2\n", ""),
            encoding="utf-8")  # an old question tied to no piece: left
        sh(self.app, "commit", "-qam", "old question")
        sh(self.app, "push", "-q", "origin", "main")
        sh(self.app, "branch", "worktree-agent-done")  # merged, folder gone: deleted
        sh(self.app, "branch", "worktree-agent-open")
        sh(self.app, "worktree", "add", "-q", str(self.tmp / "wt"), "worktree-agent-open")
        (self.tmp / "wt" / "new.txt").write_text("x", encoding="utf-8")
        sh(self.tmp / "wt", "add", "-A")
        sh(self.tmp / "wt", "commit", "-m", "unmerged")
        sh(self.app, "worktree", "remove", str(self.tmp / "wt"))  # unmerged, folder gone: kept
        sh(self.app, "push", "origin", "main:stray")  # a remote branch with no PR: the owner's, left
        state = lambda: (sh(self.app, "rev-parse", "HEAD"), sh(self.app, "branch", "--list"),  # noqa: E731
                         path.read_text(encoding="utf-8"), sh(self.app, "ls-remote", "--heads", "origin"))
        before, kinds = state(), sorted((p["item"], p["kind"]) for p in self.found())

        code, out = self.wk("check", "acme", "--fix", "--dry-run")
        self.assertEqual(code, 1)
        self.assertEqual(state(), before)  # changes nothing
        changes = out.split("\nLeft as findings:\n")[0].splitlines()
        self.assertEqual(changes, [
            "would delete acme local branch worktree-agent-done (is left from a builder whose folder is gone)",
            "would set acme reports P1: state merged (PR #5 is merged but the piece is still ready)",
            "would set acme reports P2: branch cleared (records branch gone, which exists neither on GitHub nor here)"])

        code, out = self.wk("check", "acme", "--fix")
        self.assertEqual(code, 1)
        self.assertEqual([x.replace("would ", "").replace("delete ", "deleted ") for x in changes],
                         out.split("\nLeft as findings:\n")[0].splitlines())
        rows = {x["id"]: x for x in work._read_tsv(path, work.PIECE_COLUMNS)}
        self.assertEqual((rows["P1"]["state"], rows["P2"]["branch"], rows["P3"]["state"]), ("merged", "", "ready"))
        self.assertEqual(rows["P4"]["piece"], "A name with far too many words in it now")
        branches = sh(self.app, "branch", "--list")
        self.assertNotIn("worktree-agent-done", branches)
        self.assertIn("worktree-agent-open", branches)  # unmerged: never deleted
        self.assertIn("refs/heads/stray", sh(self.app, "ls-remote", "--heads", "origin"))  # remote: never deleted
        self.assertEqual(sh(self.app, "status", "--porcelain"), "")
        self.assertEqual(sh(self.app, "rev-parse", "HEAD"), sh(self.app, "rev-parse", "origin/main"))  # pushed
        left = sorted((p["item"], p["kind"]) for p in self.found())
        self.assertEqual(left, sorted([k for k in kinds if k not in {
            ("P1", "github"), ("P2", "github"), ("branch worktree-agent-done", "git")}]
            + [("branch p1", "git")]))  # P1 finished, so its branch on GitHub is now a leftover: the owner's yes
        self.assertIn(("P3", "github"), left)
        self.assertIn(("P4", "name"), left)
        self.assertIn(("Q1", "question"), left)
        self.assertIn(("branch stray", "git"), left)
        code, out = self.wk("check", "acme", "--fix", "--dry-run")
        self.assertTrue(out.startswith("Nothing it would fix\nLeft as findings:\n"), out)
        self.assertEqual(self.wk("check", "acme", "--dry-run")[0], 2)  # --dry-run goes with --fix

    def test_fix_says_when_its_push_failed(self):
        path = self.app / ".work" / "reports" / "pieces.tsv"
        rows = work._read_tsv(path, work.PIECE_COLUMNS)
        rows[0].update(state="building", branch="gone", chat="agent:b")  # branch gone: cleared by --fix
        work._write_tsv(path, work.PIECE_COLUMNS, rows)
        sh(self.app, "add", "-A")
        sh(self.app, "commit", "-m", "rows")
        sh(self.app, "push", "origin", "main")
        hook = self.origin / "hooks" / "pre-receive"  # GitHub still reads, but refuses the push
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8", newline="\n")
        hook.chmod(0o755)
        code, out = self.wk("check", "acme", "--fix")
        self.assertEqual(code, 3, out)
        self.assertIn("set acme reports P1: branch cleared", out)
        self.assertIn("RECORDED BUT NOT ON GITHUB", out)

    def test_fix_keeps_a_branch_git_will_not_delete(self):
        sh(self.app, "branch", "worktree-agent-x")
        sh(self.app, "checkout", "-q", "worktree-agent-x")
        (self.app / "new.txt").write_text("x", encoding="utf-8")
        sh(self.app, "add", "-A")
        sh(self.app, "commit", "-m", "on main only on GitHub")
        sh(self.app, "push", "origin", "worktree-agent-x:main")
        sh(self.app, "checkout", "-q", "main")  # here, main lacks it: git branch -d refuses
        sh(self.app, "fetch", "-q", "origin")
        code, out = self.wk("check", "acme", "--fix")
        self.assertIn("kept acme local branch worktree-agent-x", out)
        self.assertIn("worktree-agent-x", sh(self.app, "branch", "--list"))
        self.assertIn("branch worktree-agent-x", out.split("\nLeft as findings:\n")[1])

class Commands(unittest.TestCase):
    def test_a_stuck_command_is_stopped_and_fails(self):
        start = time.monotonic()
        result = core.run([sys.executable, "-c", "import time; time.sleep(30)"], 1)
        self.assertEqual(result.returncode, 124)
        self.assertLess(time.monotonic() - start, 15)
        self.assertEqual(core.run([sys.executable, "-c", "import sys; print(sys.stdin.read() == '')"]).stdout.strip(),
                         "True")  # no input inherited: reading it ends at once

    def test_a_command_whose_helper_outlives_it_still_comes_back(self):
        child = "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(40)']); time.sleep(40)"
        start = time.monotonic()
        result = core.run([sys.executable, "-c", child], 2)
        self.assertEqual(result.returncode, 124)
        self.assertLess(time.monotonic() - start, 2 + core.GRACE + 6)
        orphan = "import subprocess, sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(40)'])"
        start = time.monotonic()  # it exits at once, but the helper it left holds its output open
        self.assertIn(core.run([sys.executable, "-c", orphan], 2).returncode, (0, 124))
        self.assertLess(time.monotonic() - start, 2 + core.GRACE + 6)

    def test_check_github_now_never_waits_behind_a_stuck_refresh(self):
        cache, real = dashboard.Cache({}), dashboard.overview
        try:
            dashboard.overview = lambda s, fetch=False: {"at": "09:00", "alerts": [], "attention": []}
            cache.refresh()
            cache.WAIT = 0.2
            cache.lock.acquire()  # a background refresh, stuck
            try:
                view = cache.get(fetch=True)
            finally:
                cache.lock.release()
            self.assertIn("view from 09:00: a refresh already running had not finished", view["alerts"][0])
        finally:
            dashboard.overview = real

    def test_a_failed_or_stuck_refresh_shows_on_the_page(self):
        cache, real = dashboard.Cache({}, max_age=0), dashboard.overview
        try:
            dashboard.overview = lambda s, fetch=False: {"at": "09:00", "alerts": [], "attention": []}
            cache.refresh()
            dashboard.overview = lambda s, fetch=False: (_ for _ in ()).throw(RuntimeError("git hung"))
            cache.get()
            for _ in range(100):
                if cache.failed:
                    break
                time.sleep(0.05)
            view = cache.get()
            self.assertIn("view from 09:00: the background refresh failed", view["alerts"][0])
            self.assertIn("git hung", view["alerts"][0])
            with cache.lock:
                pass  # the lock was given back
            cache.failed, cache.at = "", time.monotonic() - 400
            cache.lock.acquire()  # a refresh still running
            try:
                self.assertIn("refreshing has not finished for 6 minutes", cache.get()["alerts"][0])
            finally:
                cache.lock.release()
        finally:
            dashboard.overview = real

    def test_a_builders_own_worktree_is_not_a_loose_end(self):
        products = [{"open": [{"chat": "agent:a1b2", "branch": ""}]}]
        names = dashboard._builders(products)
        self.assertTrue({"agent-a1b2", "worktree-agent-a1b2"} <= names)
        self.assertEqual(dashboard._builders([{"open": [{"chat": "https://x", "branch": ""}]}]), set())


if __name__ == "__main__":
    unittest.main()
