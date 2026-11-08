"""Each execution repo's .work/ folder, written through wk on its main checkout and pushed to a bare origin."""

import io
import os
import json
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from datetime import date, datetime, timedelta
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
KIT = Path(__file__).resolve().parents[1]
os.environ["WORKKIT_AUTO_UPDATE"] = "0"  # tests never reach GitHub; the daily check has its own test

from wk import cli, core, settings, work  # noqa: E402


def sh(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
                          encoding="utf-8").stdout.strip()


def backdate_finished(path, day):
    """Set the first piece's finished day by its column, whatever columns follow it."""
    lines = path.read_text(encoding="utf-8").splitlines()
    head, row = lines[0].split("\t"), lines[1].split("\t")
    row[head.index("finished")] = day
    path.write_text("\n".join([lines[0], "\t".join(row)] + lines[2:]) + "\n", encoding="utf-8")


class Work(unittest.TestCase):
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
        (self.app / "wip.js").write_text("unrelated\n", encoding="utf-8")  # someone's uncommitted work
        self.settings = self.tmp / "settings.toml"
        self.settings.write_text(f'owner = "Pat Lee"\n\n[repos.acme]\npath = "{core.posix(self.app)}"\n',
                                 encoding="utf-8")
        self.assertEqual(self.wk("product", "add", "acme", "reports", "--line", "Monthly reports")[0], 0)
        self.wk("product", "add", "acme", "portal", "--line", "The customer portal")

    def wk(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--settings", str(self.settings), "--cwd", str(self.app), *argv])
        return code, out.getvalue() + err.getvalue()

    def on_github(self, path: str) -> str:
        return subprocess.run(["git", "--git-dir", str(self.origin), "show", f"main:{path}"],
                              capture_output=True, text=True, encoding="utf-8").stdout

    def test_a_list_can_live_in_another_checkout(self):
        """The code in one checkout, its list in another (`list =`): every list read and record goes there, and
        list saves read as plain words."""
        lorigin = self.tmp / "list.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(lorigin)], check=True, capture_output=True)
        lst = self.tmp / "app-list"
        subprocess.run(["git", "clone", "-q", str(lorigin), str(lst)], check=True, capture_output=True)
        sh(lst, "config", "user.name", "t")
        sh(lst, "config", "user.email", "t@example.org")
        sh(lst, "commit", "--allow-empty", "-m", "init")
        sh(lst, "push", "-q", "origin", "HEAD:main")
        before = sh(self.app, "rev-parse", "HEAD")
        self.settings.write_text(f'owner = "Pat Lee"\n\n[repos.acme]\npath = "{core.posix(self.app)}"\n'
                                 f'list = "{core.posix(lst)}"\n', encoding="utf-8")
        self.assertEqual(self.wk("product", "add", "acme", "reports", "--line", "Monthly reports")[0], 0)
        self.assertEqual(self.wk("piece", "add", "acme", "reports", "Audit fixes")[0], 0)
        self.assertEqual(self.wk("piece", "set", "acme", "reports", "P1", "--state", "ready")[0], 0)
        self.assertEqual(self.wk("piece", "note", "acme", "reports", "P1", "--add", "Ready at abc")[0], 0)
        self.assertEqual(sh(self.app, "rev-parse", "HEAD"), before)  # the code checkout got no commit
        shown = subprocess.run(["git", "--git-dir", str(lorigin), "log", "--format=%s", "main"], capture_output=True,
                               text=True, encoding="utf-8").stdout.splitlines()
        self.assertEqual(shown[:3], ["List: Audit fixes: note added", "List: Audit fixes ready for review",
                                     "List: new piece: Audit fixes"])
        self.assertIn("Audit fixes", self.wk("piece", "list", "acme", "reports")[1])
        self.assertIn("Audit fixes", self.wk("status")[1])
        r = work.Repo(settings.load(self.settings), "acme")
        self.assertEqual((r.top, r.list_top, r.apart, r.list_base), (self.app, lst, True, "main"))
        from wk import check
        self.assertIsInstance(check.at_start(settings.load(self.settings), "acme", "reports"), list)
        self.settings.write_text(f'owner = "Pat Lee"\n\n[repos.acme]\npath = "{core.posix(self.app)}"\n'
                                 f'list = "{core.posix(self.tmp / "nowhere")}"\n', encoding="utf-8")
        code, out = self.wk("piece", "list", "acme", "reports")
        self.assertEqual(code, 2)
        self.assertIn("keeps its list at", out)

    def test_products_are_declared_in_the_repo(self):
        self.assertIn("[products.portal]", self.on_github(".work/products.toml"))
        self.assertIn("reports: Monthly reports", self.wk("product", "list", "acme")[1])
        code, out = self.wk("piece", "add", "acme", "report", "typo")
        self.assertEqual(code, 2)
        self.assertIn("Known: portal, reports", out)
        self.assertEqual(self.wk("product", "add", "acme", "Bad Name", "--line", "x")[0], 2)
        self.assertEqual(self.wk("piece", "add", "nowhere", "reports", "x")[0], 2)  # unknown repo

    def test_pieces_are_one_row_each_and_status_is_computed(self):
        self.wk("piece", "add", "acme", "reports", "Aged debt view")
        self.wk("piece", "add", "acme", "reports", "Export to CSV")
        code, out = self.wk("piece", "set", "acme", "reports", "p1", "--state", "building", "--pr", "12")
        self.assertEqual(code, 0)
        self.assertIn('"push": "pushed"', out)
        self.assertIn("P1\tAged debt view\tbuilding\t\t12", self.on_github(".work/reports/pieces.tsv"))
        self.assertEqual(sh(self.app, "status", "--porcelain"), "?? wip.js")  # only .work/ was committed
        self.wk("piece", "set", "acme", "reports", "P2", "--state", "merged", "--pr", "1")
        out = self.wk("piece", "list", "acme", "reports")[1]
        self.assertIn("Aged debt view", out)
        self.assertNotIn("Export to CSV", out)  # merged pieces are closed rows
        self.assertIn("Export to CSV", self.wk("piece", "list", "acme", "reports", "--all")[1])
        status = self.wk("status")[1]
        self.assertIn("acme reports: 1 open", status)
        self.assertIn("acme portal: 0 open", status)
        self.assertFalse([p for p in (self.app / ".work").rglob("*") if "status" in p.name])  # never stored
        self.assertEqual(self.wk("piece", "set", "acme", "reports", "P9", "--state", "ready")[0], 2)
        self.assertEqual(self.wk("piece", "set", "acme", "reports", "P1")[0], 2)  # nothing to change
        code, out = self.wk("piece", "set", "acme", "reports", "P1", "--name", "Aged debt at a glance")
        self.assertEqual(code, 0)
        self.assertIn("P1\tAged debt at a glance\t", self.on_github(".work/reports/pieces.tsv"))
        self.assertEqual(self.wk("piece", "set", "acme", "reports", "P1", "--name", "  ")[0], 2)  # a name can't be empty

    def test_the_supervisor_records_its_recommended_order_in_one_write(self):
        for name in ("Aged debt view", "Export to CSV", "Fix totals", "Old idea"):
            self.wk("piece", "add", "acme", "reports", name)
        path = self.app / ".work" / "reports" / "pieces.tsv"
        head, *rows = path.read_text(encoding="utf-8").splitlines()
        keep = head.split("\t").index("rank")  # a file from before ranks: no rank column, nor the ones after it
        old = "\t".join(head.split("\t")[:keep])
        path.write_text("\n".join([old] + ["\t".join(r.split("\t")[:keep]) for r in rows]) + "\n", encoding="utf-8")
        self.wk("piece", "set", "acme", "reports", "P4", "--state", "dropped")
        before = sh(self.app, "rev-list", "--count", "HEAD")
        code, out = self.wk("piece", "rank", "acme", "reports", "p3,P1")
        self.assertEqual(code, 0, out)
        self.assertEqual(int(sh(self.app, "rev-list", "--count", "HEAD")), int(before) + 1)  # one commit
        rank = lambda: {x["id"]: x["rank"] for x in work.pieces(work.Repo(settings.load(self.settings), "acme"),
                                                                 "reports")}
        self.assertEqual(rank(), {"P1": "2", "P2": "", "P3": "1", "P4": ""})
        self.assertIn("\trank", self.on_github(".work/reports/pieces.tsv").splitlines()[0])
        self.assertEqual(self.wk("piece", "rank", "acme", "reports", "P4")[0], 2)  # not open
        self.assertEqual(self.wk("piece", "rank", "acme", "reports", "P1,P1")[0], 2)
        self.assertEqual(self.wk("piece", "rank", "acme", "reports")[0], 2)
        self.wk("piece", "set", "acme", "reports", "P3", "--state", "done")
        self.assertEqual(rank()["P3"], "")  # closed: out of the order
        text = path.read_text(encoding="utf-8").splitlines()
        cells, at = text[4].split("\t"), text[0].split("\t").index("rank")
        self.assertTrue(cells[0] == "P4" and cells[at] == "")
        cells[at] = "7"  # closed P4 with a rank, as a piece closed by an older work-kit could have
        text[4] = "\t".join(cells)
        path.write_text("\n".join(text) + "\n", encoding="utf-8")
        self.wk("piece", "add", "acme", "reports", "Anything")  # any write drops it
        self.assertEqual(rank()["P4"], "")

    def test_a_piece_note_is_its_memory(self):
        self.wk("piece", "add", "acme", "reports", "Aged debt view")
        self.assertIn("P1 has no note yet", self.wk("piece", "note", "acme", "reports", "P1")[1])
        brief = self.tmp / "brief.md"
        brief.write_text("# P1 · Aged debt view\n\nBrief: show debt by age band.\n", encoding="utf-8")
        self.assertEqual(self.wk("piece", "note", "acme", "reports", "p1", "--file", str(brief))[0], 0)
        code, out = self.wk("piece", "note", "acme", "reports", "P1", "--add", "Owner chose 30/60/90 bands")
        self.assertEqual(code, 0)
        self.assertIn('"push": "pushed"', out)
        on_github = self.on_github(".work/reports/notes/P1.md")
        self.assertIn("Brief: show debt by age band.", on_github)
        self.assertRegex(on_github, r"- \d{4}-\d{2}-\d{2}: Owner chose 30/60/90 bands\n$")
        self.assertIn("Owner chose 30/60/90 bands", self.wk("piece", "note", "acme", "reports", "P1")[1])
        status = json.loads(self.wk("--json", "status")[1])
        row = next(p for p in status if p["product"] == "reports")["open"][0]
        self.assertTrue(row["note"].endswith("reports/notes/P1.md"))
        self.assertRegex(row["latest"], r"^\d{4}-\d{2}-\d{2}: Owner chose 30/60/90 bands$")
        self.assertEqual(self.wk("piece", "note", "acme", "reports", "P9", "--add", "x")[0], 2)  # no such piece
        self.assertEqual(self.wk("piece", "note", "acme", "reports", "P1", "--add", "  ")[0], 2)  # nothing to add

    def test_new_work_on_a_finished_billed_piece_is_refused_as_a_note_line(self):
        self.wk("piece", "add", "acme", "reports", "Aged debt view")
        self.wk("piece", "add", "acme", "reports", "Debtor letters")
        self.wk("hours", "add", "acme", "reports", "2", "--note", "Aged debt view", "--date", "2026-10-01", "--for", "P1")
        self.wk("piece", "set", "acme", "reports", "P1", "--state", "done")
        self.wk("piece", "set", "acme", "reports", "P2", "--state", "done")
        self.assertEqual(self.wk("piece", "note", "acme", "reports", "P1", "--add", "Before billing")[0], 0)
        self.assertEqual(self.wk("hours", "mark", "acme", "--through", "2026-10-31", "--as", "INV-1", "--expect", "2")[0], 0)
        code, out = self.wk("piece", "note", "acme", "reports", "P1", "--add", "Drafted a reply to Sam")
        self.assertEqual(code, 2)
        self.assertIn("billed (INV-1). New work on it is a new piece: wk piece add", out)
        self.assertIn("--billed-ok", out)  # the message names both choices
        self.assertNotIn("Drafted a reply", self.on_github(".work/reports/notes/P1.md"))
        self.assertEqual(self.wk("piece", "note", "acme", "reports", "P1", "--add", "Released 2.1.0",
                                 "--billed-ok")[0], 0)  # duty after the invoice: a release or teardown note
        self.assertIn("Released 2.1.0", self.on_github(".work/reports/notes/P1.md"))
        self.assertEqual(self.wk("piece", "note", "acme", "reports", "P2", "--add", "Finished, no hours")[0], 0)
        brief = self.tmp / "final.md"
        brief.write_text("# P1 · Aged debt view\n\nOutcome: shipped.\n", encoding="utf-8")
        self.assertEqual(self.wk("piece", "note", "acme", "reports", "P1", "--file", str(brief))[0], 0)  # tidy-up
        self.wk("piece", "set", "acme", "reports", "P1", "--state", "building")  # reopened on the owner's word
        self.assertEqual(self.wk("piece", "note", "acme", "reports", "P1", "--add", "Reopened")[0], 0)

    def test_questions_wait_until_answered(self):
        self.wk("question", "add", "acme", "portal", "Ship", "the", "new", "login?",
                "--options", "now | after review", "--recommended", "after review", "--pr", "#4", "--list-wide")
        self.wk("question", "add", "acme", "portal", "Rename the menu?", "--list-wide")
        self.assertIn("Q1  Ship the new login?  (recommended: after review)",
                      self.wk("question", "list", "acme", "portal")[1])
        self.assertIn("2 question(s) for the owner", self.wk("status")[1])
        code, out = self.wk("question", "answer", "acme", "portal", "Q1", "after", "review")
        self.assertEqual(code, 0)
        self.assertIn('"answer": "after review"', out)
        md = self.on_github(".work/portal/questions.md")
        self.assertNotIn("Ship the new login", md)
        self.assertIn("## Q2 · Rename the menu?", md)
        self.assertEqual(self.wk("question", "answer", "acme", "portal", "Q1", "again")[0], 2)

    def test_standing_orders(self):
        self.wk("order", "add", "acme", "portal", "Production deploys only from main")
        self.wk("order", "add", "acme", "portal", "No new CI triggers")
        self.assertEqual(self.wk("order", "drop", "acme", "portal", "1")[0], 0)
        self.assertEqual(self.wk("order", "list", "acme", "portal")[1].strip(), "1. No new CI triggers")
        self.assertEqual(self.wk("order", "drop", "acme", "portal", "5")[0], 2)

    def test_odd_text_never_breaks_the_list_file(self):
        line = 'Exports to C:\\Finance\\new "draft" folder'
        self.assertEqual(self.wk("product", "add", "acme", "files", "--line", line)[0], 0)
        self.assertEqual(self.wk("product", "set", "acme", "files", "--title", 'A\\B "quoted"')[0], 0)
        code, out = self.wk("--json", "product", "list", "acme")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["files"], {"line": line, "title": 'A\\B "quoted"'})
        self.assertEqual(self.wk("status")[0], 0)
        self.assertEqual(self.wk("product", "set", "acme", "files", "--line", "Exports, renamed")[0], 0)
        self.assertEqual(json.loads(self.wk("--json", "product", "list", "acme")[1])["files"]["line"], "Exports, renamed")
        self.assertEqual(self.wk("product", "set", "acme", "files", "--line", " ")[0], 2)  # an invoice line can't be empty

    def test_compact_dates_are_stored_the_standard_way(self):
        self.assertEqual(self.wk("hours", "add", "acme", "reports", "1", "--note", "a", "--date", "20261001")[0], 0)
        self.assertIn("2026-10-01\treports\t1\t", self.on_github(".work/hours.tsv"))
        self.assertEqual(self.wk("hours", "add", "acme", "reports", "2", "--note", "b", "--date", "2026-10-01")[0], 2)  # same day
        self.assertEqual(self.wk("hours", "add", "acme", "reports", "2", "--note", "b", "--date", "2026-10-01", "--again")[0], 0)
        code, out = self.wk("hours", "mark", "acme", "--through", "20261027", "--as", "INV-1", "--expect", "3")
        self.assertEqual(code, 0, out)  # both rows counted: none slipped past the invoice

    def test_non_english_text_survives_a_piped_output(self):
        run = subprocess.run([sys.executable, "-m", "wk", "--settings", str(self.settings), "--cwd", str(self.app),
                              "piece", "add", "acme", "reports", "Arrow → fix, café"], capture_output=True, timeout=120,
                             env={k: v for k, v in os.environ.items() if k != "PYTHONIOENCODING"} | {"PYTHONPATH": str(KIT)})
        self.assertEqual(run.returncode, 0, run.stderr.decode("utf-8", "replace"))
        self.assertIn("Arrow → fix, café", run.stdout.decode("utf-8"))

    def test_one_broken_repo_never_hides_the_others(self):
        other = self.tmp / "other"
        other.mkdir()
        sh(other, "init", "-b", "main")
        (other / ".work").mkdir()
        (other / ".work" / "products.toml").write_text('[products.x]\nline = "bad \\ value"\n', encoding="utf-8")
        with self.settings.open("a", encoding="utf-8") as f:
            f.write(f'\n[repos.other]\npath = "{core.posix(other)}"\n')
        code, out = self.wk("--json", "status")
        self.assertEqual(code, 0, out)
        rows = json.loads(out)
        self.assertTrue(any(r["repo"] == "other" and "error" in r for r in rows))
        self.assertTrue(any(r.get("product") == "reports" for r in rows))

    def test_status_lists_what_finished_lately(self):
        self.wk("piece", "add", "acme", "reports", "Aged debt view")
        self.wk("piece", "set", "acme", "reports", "P1", "--state", "merged", "--pr", "9")
        row = next(r for r in json.loads(self.wk("--json", "status")[1]) if r.get("product") == "reports")
        self.assertEqual([(x["id"], x["pr"]) for x in row["recent"]], [("P1", "9")])
        self.assertEqual(row["open"], [])
        self.wk("piece", "add", "acme", "reports", "Answer the auditors' question")
        self.wk("piece", "set", "acme", "reports", "P2", "--state", "done")  # finished without a PR
        row = next(r for r in json.loads(self.wk("--json", "status")[1]) if r.get("product") == "reports")
        self.assertEqual([x["id"] for x in row["recent"]], ["P1", "P2"])
        self.assertEqual((row["counts"]["merged"], row["counts"]["done"], row["open"]), (1, 1, []))
        # P1 finished weeks ago: renaming it today never brings it back
        path = self.app / ".work" / "reports" / "pieces.tsv"
        lines = path.read_text(encoding="utf-8").splitlines()
        at = lines[0].split("\t").index("finished")
        rows = [ln.split("\t") for ln in lines[1:]]
        rows[0][at] = "2026-09-01"
        path.write_text("\n".join([lines[0]] + ["\t".join(r) for r in rows]) + "\n", encoding="utf-8")
        self.wk("piece", "set", "acme", "reports", "P1", "--name", "Aged debt view for finance")
        row = next(r for r in json.loads(self.wk("--json", "status")[1]) if r.get("product") == "reports")
        self.assertEqual([x["id"] for x in row["recent"]], ["P2"])

    def test_hours_live_with_the_repo(self):
        args = ["hours", "add", "acme", "reports", "1.5", "--note", "Aged debt review", "--date", "2026-10-26"]
        code, out = self.wk(*args)
        self.assertEqual(code, 0)
        self.assertIn("app@main", out)  # who recorded it
        code, out = self.wk(*args)
        self.assertEqual(code, 2)
        self.assertIn("already has 1.5h on 2026-10-26", out)
        self.assertEqual(self.wk(*args, "--again")[0], 0)
        self.wk("hours", "add", "acme", "portal", "2", "--note", "Call with the team", "--date", "2026-10-26")
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            cli.main(["--settings", str(self.settings), "--json", "hours", "list"])
        result = json.loads(out.getvalue())
        self.assertEqual(result[0]["totals"], {"portal": 2.0, "reports": 3.0})
        self.assertIn("2026-10-26\treports\t1.5\tAged debt review", self.on_github(".work/hours.tsv"))
        self.assertEqual(self.wk("hours", "add", "acme", "reports", "0", "--note", "x")[0], 2)
        self.assertEqual(self.wk("hours", "add", "acme", "reports", "1")[0], 2)  # a note is required

    def test_marking_hours_done(self):
        self.wk("hours", "add", "acme", "reports", "1.5", "--note", "Aged debt", "--date", "2026-10-20")
        self.wk("hours", "add", "acme", "portal", "2", "--note", "Login page", "--date", "2026-10-22")
        self.wk("hours", "add", "acme", "portal", "1", "--note", "Next week", "--date", "2026-10-27")
        mark = ["hours", "mark", "acme", "--through", "2026-10-23", "--as", "INV-007"]
        code, out = self.wk(*mark, "--expect", "3")  # someone added hours after counting
        self.assertEqual(code, 2)
        self.assertIn("3.5h unmarked through 2026-10-23, not the 3h expected", out)
        self.assertEqual(self.wk(*mark, "--expect", "3.5")[0], 0)
        tsv = self.on_github(".work/hours.tsv")
        self.assertIn("Aged debt\tapp@main\tINV-007", tsv)
        self.assertIn("Next week\tapp@main\t\t\t\n", tsv)  # after the cut-off: left alone (no pieces, no line key)
        code, out = self.wk(*mark, "--expect", "0")
        self.assertIn("a label is used once", out)
        code, out = self.wk("hours", "mark", "acme", "--through", "2026-10-23", "--as", "INV-008", "--expect", "1")
        self.assertIn("no unmarked hours", out)
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            cli.main(["--settings", str(self.settings), "--json", "hours", "list", "acme", "--unbilled"])
        self.assertEqual(json.loads(out.getvalue())[0]["total"], 1.0)

    def test_hours_name_the_pieces_they_pay_for(self):
        self.wk("piece", "add", "acme", "reports", "Aged debt report")
        self.wk("piece", "add", "acme", "reports", "Cash forecast")
        code, out = self.wk("hours", "add", "acme", "reports", "1", "--note", "x", "--for", "P9")
        self.assertEqual(code, 2)
        self.assertIn("No piece P9", out)
        self.assertEqual(self.wk("hours", "add", "acme", "reports", "2", "--note", "Reports", "--date", "2026-10-20",
                                 "--for", "p1, P2")[0], 0)
        self.assertIn("Reports\tapp@main\t\tP1,P2\t\n", self.on_github(".work/hours.tsv"))
        code, out = self.wk("piece", "set", "acme", "reports", "P2", "--bill", "no")
        self.assertIn("--reason", out)
        code, out = self.wk("piece", "set", "acme", "reports", "P2", "--bill", "no", "--reason", "Fixed fee")
        self.assertEqual(code, 2)  # unbilled hours name it: move them first
        self.assertIn("wk hours fix ... --for", out)
        self.wk("hours", "fix", "acme", "reports", "--date", "2026-10-20", "--for", "P1")
        self.wk("hours", "mark", "acme", "--through", "2026-10-20", "--as", "INV-1", "--expect", "2")
        self.wk("hours", "add", "acme", "reports", "1", "--note", "z", "--date", "2026-10-21", "--for", "P2")
        self.wk("hours", "mark", "acme", "--through", "2026-10-21", "--as", "INV-2", "--expect", "1")
        self.assertEqual(self.wk("piece", "set", "acme", "reports", "P2", "--bill", "no", "--reason",
                                 "Part of the fixed fee")[0], 0)  # billed hours never block it
        self.assertIn("Not billing: Part of the fixed fee", self.on_github(".work/reports/notes/P2.md"))
        code, out = self.wk("hours", "add", "acme", "reports", "1", "--note", "y", "--again", "--for", "P2")
        self.assertIn("marked not billing", out)

    def test_a_later_piece_takes_hours_only_once_billed_again(self):
        self.wk("piece", "add", "acme", "reports", "Aged debt report")
        self.wk("piece", "set", "acme", "reports", "P1", "--bill", "later", "--reason", "Not handed over")
        code, out = self.wk("hours", "add", "acme", "reports", "1", "--note", "x", "--for", "P1")
        self.assertEqual(code, 2)
        self.assertIn("marked bill later", out)
        self.wk("piece", "set", "acme", "reports", "P1", "--bill", "yes")
        self.assertEqual(self.wk("hours", "add", "acme", "reports", "1", "--note", "x", "--for", "P1")[0], 0)

    def test_finished_is_set_once(self):
        self.wk("piece", "add", "acme", "reports", "Aged debt report")
        self.wk("piece", "set", "acme", "reports", "P1", "--state", "merged", "--pr", "1")
        path = self.app / ".work" / "reports" / "pieces.tsv"
        self.assertEqual(work.pieces(work.Repo(settings.load(self.settings), "acme"), "reports")[0]["finished"],
                         date.today().isoformat())
        # a piece merged by an older work-kit: no finished, last changed 2026-10-20
        lines = path.read_text(encoding="utf-8").splitlines()
        head, row = lines[0].split("\t"), lines[1].split("\t")
        row[head.index("updated")], row[head.index("finished")] = "2026-10-20", ""
        path.write_text("\n".join([lines[0], "\t".join(row)]) + "\n", encoding="utf-8")
        self.wk("piece", "set", "acme", "reports", "P1", "--name", "Aged debt report for finance")
        self.assertEqual(work.pieces(work.Repo(settings.load(self.settings), "acme"), "reports")[0]["finished"],
                         "2026-10-20")

    def test_merged_again_after_reopening_finishes_again(self):
        self.wk("piece", "add", "acme", "reports", "Aged debt report")
        self.wk("piece", "set", "acme", "reports", "P1", "--state", "merged", "--pr", "1")
        path = self.app / ".work" / "reports" / "pieces.tsv"
        backdate_finished(path, "2026-10-20")  # it first finished on 10-20
        finished = lambda: work.pieces(work.Repo(settings.load(self.settings), "acme"), "reports")[0]["finished"]
        self.assertEqual(finished(), "2026-10-20")
        self.wk("piece", "set", "acme", "reports", "P1", "--state", "building")
        self.assertEqual(finished(), "2026-10-20")
        self.wk("piece", "set", "acme", "reports", "P1", "--state", "merged", "--pr", "1")
        self.assertEqual(finished(), date.today().isoformat())
        self.wk("piece", "set", "acme", "reports", "P1", "--name", "Aged debt report for finance")
        self.assertEqual(finished(), date.today().isoformat())

    def test_a_done_piece_without_a_finish_day_gets_its_last_change(self):
        self.wk("piece", "add", "acme", "reports", "Answer the auditors' question")
        self.wk("piece", "set", "acme", "reports", "P1", "--state", "done")
        path = self.app / ".work" / "reports" / "pieces.tsv"
        lines = path.read_text(encoding="utf-8").splitlines()
        head, row = lines[0].split("\t"), lines[1].split("\t")
        row[head.index("updated")], row[head.index("finished")] = "2026-10-20", ""  # a hand edit or a lost column
        path.write_text("\n".join([lines[0], "\t".join(row)]) + "\n", encoding="utf-8")
        self.wk("piece", "set", "acme", "reports", "P1", "--name", "Auditors answered")
        self.assertEqual(work.pieces(work.Repo(settings.load(self.settings), "acme"), "reports")[0]["finished"],
                         "2026-10-20")

    def test_done_finishes_like_merged(self):
        self.wk("piece", "add", "acme", "reports", "Answer the auditors' question")
        path = self.app / ".work" / "reports" / "pieces.tsv"
        finished = lambda: work.pieces(work.Repo(settings.load(self.settings), "acme"), "reports")[0]["finished"]
        self.wk("piece", "set", "acme", "reports", "P1", "--state", "done")
        self.assertEqual(finished(), date.today().isoformat())
        backdate = lambda: backdate_finished(path, "2026-10-20")  # noqa: E731  it finished on 10-20
        backdate()
        self.wk("piece", "set", "acme", "reports", "P1", "--name", "Auditors answered")
        self.assertEqual(finished(), "2026-10-20")
        self.wk("piece", "set", "acme", "reports", "P1", "--state", "merged", "--pr", "1")  # a fix built after the finding
        self.assertEqual(finished(), date.today().isoformat())
        backdate()
        self.wk("piece", "set", "acme", "reports", "P1", "--state", "done")  # merged then done keeps its day
        self.assertEqual(finished(), "2026-10-20")
        self.wk("piece", "set", "acme", "reports", "P1", "--state", "ready")  # reopened
        self.assertEqual(finished(), "2026-10-20")
        self.wk("piece", "set", "acme", "reports", "P1", "--state", "done")  # done again: finished again
        self.assertEqual(finished(), date.today().isoformat())
        code, out = self.wk("piece", "list", "acme", "reports")
        self.assertNotIn("Auditors answered", out)  # done pieces are closed rows
        self.assertIn("done", self.wk("piece", "list", "acme", "reports", "--all")[1])

    def test_bill_later_is_refused_while_unbilled_hours_name_it(self):
        self.wk("piece", "add", "acme", "reports", "Aged debt report")
        self.wk("hours", "add", "acme", "reports", "1", "--note", "x", "--for", "P1")
        code, out = self.wk("piece", "set", "acme", "reports", "P1", "--bill", "later", "--reason", "Not handed over")
        self.assertEqual(code, 2)
        self.assertIn("before marking it bill later", out)

    def test_columns_a_newer_version_added_survive(self):
        self.wk("hours", "add", "acme", "reports", "1", "--note", "a", "--date", "2026-10-20")
        path = self.app / ".work" / "hours.tsv"
        lines = path.read_text(encoding="utf-8").splitlines()
        path.write_text("\n".join([lines[0] + "\tlater_column", lines[1] + "\tkeep me"]) + "\n", encoding="utf-8")
        self.wk("hours", "add", "acme", "reports", "2", "--note", "b", "--date", "2026-10-21")
        text = self.on_github(".work/hours.tsv")
        self.assertIn("\tlater_column\n", text)
        self.assertIn("\tkeep me\n", text)

    def test_marking_checks_the_exact_rows(self):
        self.wk("hours", "add", "acme", "reports", "1", "--note", "a", "--date", "2026-10-20")
        rows = [{"date": "2026-10-20", "product": "reports", "hours": "1", "note": "b"}]
        mark = ["hours", "mark", "acme", "--through", "2026-10-20", "--as", "INV-1", "--expect", "1"]
        code, out = self.wk(*mark, "--digest", work.rows_digest(rows))
        self.assertIn("same total, different entries", out)
        rows[0]["note"] = "a"
        self.assertEqual(self.wk(*mark, "--digest", work.rows_digest(rows))[0], 0)

    def test_choosing_among_same_day_entries(self):
        for note in ("Aged debt", "Aged debt", "Aged debt review"):
            self.wk("hours", "add", "acme", "reports", "1", "--note", note, "--date", "2026-10-22", "--again")
        code, out = self.wk("hours", "fix", "acme", "reports", "2", "--date", "2026-10-22", "--was", "aged debt")
        self.assertIn("or --nth: 1. 1h 'Aged debt'; 2. 1h 'Aged debt'", out)  # exact matches only
        self.assertEqual(self.wk("hours", "fix", "acme", "reports", "2", "--date", "2026-10-22", "--nth", "2")[0], 0)
        self.assertEqual(self.wk("hours", "fix", "acme", "reports", "3", "--date", "2026-10-22", "--was", "review")[0], 0)
        tsv = self.on_github(".work/hours.tsv")
        self.assertIn("\t1\tAged debt\t", tsv)
        self.assertIn("\t2\tAged debt\t", tsv)
        self.assertIn("\t3\tAged debt review\t", tsv)

    def test_fixing_an_entry_only_before_it_is_billed(self):
        self.wk("hours", "add", "acme", "reports", "1.5", "--note", "Aged debt", "--date", "2026-10-20")
        self.wk("hours", "add", "acme", "reports", "1", "--note", "Cash forecast", "--date", "2026-10-21")
        self.wk("hours", "add", "acme", "reports", "2", "--note", "Board pack", "--date", "2026-10-21", "--again")
        self.assertEqual(self.wk("hours", "fix", "acme", "reports", "2", "--date", "2026-10-20", "--note",
                                 "Aged debt review")[0], 0)
        self.assertIn("2026-10-20\treports\t2\tAged debt review", self.on_github(".work/hours.tsv"))
        code, out = self.wk("hours", "fix", "acme", "reports", "--date", "2026-10-21", "--remove")
        self.assertIn("--was", out)  # two entries that day: say which
        self.assertEqual(self.wk("hours", "fix", "acme", "reports", "--date", "2026-10-21", "--was", "board",
                                 "--remove")[0], 0)
        self.assertNotIn("Board pack", self.on_github(".work/hours.tsv"))
        self.wk("hours", "mark", "acme", "--through", "2026-10-21", "--as", "INV-1", "--expect", "3")
        code, out = self.wk("hours", "fix", "acme", "reports", "3", "--date", "2026-10-20")
        self.assertEqual(code, 2)
        self.assertIn("on INV-1 and stays as it is", out)

    def test_main_checkout_must_be_on_base(self):
        sh(self.app, "switch", "-q", "-c", "side")
        code, out = self.wk("piece", "add", "acme", "reports", "x")
        self.assertEqual(code, 2)
        self.assertIn("must be on main", out)

    def test_bill_checks_its_note_first_and_reports_either_failed_push(self):
        self.wk("piece", "add", "acme", "reports", "Board pack")
        before = sh(self.app, "rev-parse", "HEAD")
        code, out = self.wk("piece", "set", "acme", "reports", "P1", "--bill", "later", "--reason", "x" * 700)
        self.assertEqual(code, 2)
        self.assertIn("characters", out)
        self.assertEqual(sh(self.app, "rev-parse", "HEAD"), before)  # refused before anything was written
        real = work.note
        work.note = lambda *a, **k: real(*a, **k) | {"push": "push failed: offline"}  # only the note's push fails
        try:
            code, out = self.wk("piece", "set", "acme", "reports", "P1", "--bill", "later", "--reason", "Not yet")
        finally:
            work.note = real
        self.assertEqual(code, 3)
        self.assertIn("NOT ON GITHUB", out)

    def test_failed_push_is_loud(self):
        sh(self.app, "remote", "set-url", "origin", core.posix(self.tmp / "missing.git"))
        code, out = self.wk("piece", "add", "acme", "reports", "Offline piece")
        self.assertEqual(code, 3)
        self.assertIn("NOT ON GITHUB", out)
        self.assertIn("Offline piece", sh(self.app, "log", "-1", "--format=%s"))  # still recorded locally

    def fill_channels(self):
        """The channels file started from the template, then filled in by the owner and saved."""
        self.assertEqual(self.wk("channels", "acme")[0], 0)
        path = self.app / ".work" / "communication_channels.md"
        path.write_text("# Communication channels\n\n## Mail\nRead with: a made-up connector.\n\n"
                        "## Chat\nRead with: a made-up browser profile.\n", encoding="utf-8")
        self.assertEqual(self.wk("channels", "acme")[0], 0)
        return path

    def test_channels_file_starts_from_the_template_and_refuses_while_unfilled(self):
        commits = lambda: sh(self.app, "rev-list", "--count", "HEAD")  # noqa: E731
        code, out = self.wk("channels", "acme")  # missing: started from the template and pushed
        self.assertEqual(code, 0, out)
        self.assertIn("TEMPLATE:", self.on_github(".work/communication_channels.md"))
        self.assertIn("channels started from the template", sh(self.app, "log", "-1", "--format=%s"))
        self.assertIn("NOT FILLED IN", out)
        self.assertIn("saved: pushed", out)
        self.assertRegex(out, r"now: \d{4}-\d\d-\d\dT\d\d:\d\d[+-]\d\d:\d\d")  # with its zone, for --at
        self.assertIn("Project email: last checked never, read from ", out)

        before = commits()
        code, out = self.wk("channels", "acme")  # a quiet look: prints it, commits nothing
        self.assertEqual((code, commits()), (0, before))
        self.assertIn("NOT FILLED IN", out)
        now = (datetime.now().astimezone() - timedelta(minutes=1)).isoformat(timespec="minutes")
        code, out = self.wk("channels", "acme", "--checked", "Project email", "--at", now)
        self.assertEqual((code, commits()), (2, before))  # the template's examples are never read
        self.assertIn("still holds the template's example channels", out)

        path = self.app / ".work" / "communication_channels.md"  # the owner fills it in; the next look saves it
        path.write_text(path.read_text(encoding="utf-8").replace("TEMPLATE:", "Filled in:"), encoding="utf-8")
        code, out = self.wk("channels", "acme")
        self.assertEqual(code, 0, out)
        self.assertIn("Filled in:", self.on_github(".work/communication_channels.md"))
        self.assertIn("channels edited", sh(self.app, "log", "-1", "--format=%s"))
        self.assertNotIn("NOT FILLED IN", out)
        self.assertNotIn("wip.js", sh(self.app, "show", "--stat", "--format=", "HEAD"))  # nothing else committed
        self.assertEqual(self.wk("channels", "acme", "--checked", "Project email", "--at", now)[0], 0)

    def test_each_channel_keeps_its_own_last_checked_time_on_this_laptop_and_never_commits_it(self):
        path = self.fill_channels()
        head = sh(self.app, "rev-parse", "HEAD")
        t1 = (datetime.now().astimezone() - timedelta(hours=2)).replace(second=0, microsecond=0)
        t2 = t1 + timedelta(hours=1)
        code, out = self.wk("channels", "acme", "--checked", "Mail", "--at", t1.isoformat())
        self.assertEqual(code, 0, out)
        self.assertIn(f"moved: Mail {t1.isoformat(timespec='minutes')}", out)
        code, out = self.wk("channels", "acme")
        # each channel on its own: Chat failed (never moved), so it is still read from a day back; Mail from its
        # last check less the overlap
        self.assertIn(f"Mail: last checked {t1.isoformat(timespec='minutes')}, read from "
                      f"{(t1 - work.OVERLAP).isoformat(timespec='minutes')}", out)
        self.assertRegex(out, r"Chat: last checked never, read from \d{4}-")
        self.assertEqual(self.wk("channels", "acme", "--checked", "Chat", "--at", t2.isoformat())[0], 0)
        self.assertIn(f"Mail: last checked {t1.isoformat(timespec='minutes')}", self.wk("channels", "acme")[1])
        # local state only: no commit, nothing changed in the checkout, kept in the repo's Git folder
        self.assertEqual(sh(self.app, "rev-parse", "HEAD"), head)
        self.assertEqual(sh(self.app, "status", "--porcelain"), "?? wip.js")
        state = json.loads((core.git_common_dir(self.app) / work.CHECKED).read_text(encoding="utf-8"))
        self.assertEqual(state["channels"], {"Mail": t1.isoformat(timespec="minutes"),
                                             "Chat": t2.isoformat(timespec="minutes")})

        self.assertEqual(self.wk("channels", "acme", "--checked", "Mail", "--at", t1.isoformat())[0], 0)  # equal
        code, out = self.wk("channels", "acme", "--checked", "Mail", "--at", (t1 - timedelta(minutes=5)).isoformat())
        self.assertEqual(code, 2)
        self.assertIn("never moves backwards", out)
        soon = (datetime.now().astimezone() + timedelta(hours=1)).isoformat(timespec="minutes")
        code, out = self.wk("channels", "acme", "--checked", "Mail", "--at", soon)
        self.assertEqual(code, 2)
        self.assertIn("is in the future", out)
        code, out = self.wk("channels", "acme", "--checked", "Mail", "--at", "after lunch")
        self.assertEqual(code, 2)
        self.assertIn("'after lunch' is not a time", out)
        code, out = self.wk("channels", "acme", "--checked", "Mial", "--at", t2.isoformat())
        self.assertEqual(code, 2)
        self.assertIn("No channel 'Mial'", out)
        self.assertIn("Its channels: Mail, Chat", out)
        self.assertEqual(self.wk("channels", "acme", "--checked", "Mail")[0], 2)  # a time is required
        local = (datetime.now() - timedelta(minutes=10)).replace(second=0, microsecond=0)  # no zone: local time
        self.assertEqual(self.wk("channels", "acme", "--checked", "Mail", "--at", local.isoformat())[0], 0)
        self.assertIn(f"Mail: last checked {local.astimezone().isoformat(timespec='minutes')}",
                      self.wk("channels", "acme")[1])
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))  # a CRLF checkout reads the same headings
        self.assertIn("Its channels: Mail, Chat", self.wk("channels", "acme", "--checked", "x", "--at", local.isoformat())[1])
        self.assertEqual(self.wk("channels", "nope")[0], 2)  # unknown repo

    def test_inbox_add_list_done_and_the_duplicate_check(self):
        self.wk("piece", "add", "acme", "reports", "Aged debt view")
        link = "https://mail.example.org/m/abc?x=1&y=2"
        self.assertEqual(self.wk("inbox", "check", "acme", "--link", link)[0], 0)  # new
        code, out = self.wk("inbox", "add", "acme", "reports", "Client asks", "when", "the", "view", "ships",
                            "--link", link, "--channel", "Mail", "--when", "2020-01-02T09:15+05:00", "--piece", "p1")
        self.assertEqual(code, 0, out)
        on = self.on_github(".work/reports/inbox.md")
        self.assertIn("## I1 · Client asks when the view ships\n- Link: " + link + "\n- Channel: Mail\n"
                      "- When: 2020-01-02T09:15+05:00\n- Piece: P1\n- Filed: ", on)
        self.assertIn("reports inbox I1: Client asks when the view ships", sh(self.app, "log", "-1", "--format=%s"))
        out = self.wk("inbox", "list", "acme", "reports")[1]
        self.assertIn("I1  Client asks when the view ships  (Mail, 2020-01-02T09:15+05:00, about P1)", out)
        # a rerun over the same message: refused wherever it would go, in the inbox or another list's
        code, out = self.wk("inbox", "add", "acme", "portal", "Same message", "--link", link, "--channel", "Mail")
        self.assertEqual(code, 1)
        self.assertIn("Already filed (reports inbox I1)", out)
        self.assertEqual(self.wk("inbox", "check", "acme", "--link", link)[0], 1)
        self.assertEqual(self.wk("--json", "status")[0], 0)
        self.assertEqual(next(x for x in work.status(settings.load(self.settings)) if x["product"] == "reports")["inbox"], 1)

        code, out = self.wk("inbox", "done", "acme", "reports", "i1", "--how", "note on P1")
        self.assertEqual(code, 0, out)
        self.assertNotIn("## I1", self.on_github(".work/reports/inbox.md"))  # deleted once handled
        self.assertIn("reports inbox I1 handled: note on P1", sh(self.app, "log", "-1", "--format=%s"))
        self.assertEqual(self.wk("inbox", "list", "acme", "reports")[1].strip(), "Inbox empty")
        # handled and gone, but the history still knows it: never filed twice
        code, out = self.wk("inbox", "add", "acme", "reports", "Again", "--link", link, "--channel", "Mail")
        self.assertEqual(code, 1)
        self.assertIn("Already filed (commit ", out)
        # a link a supervisor put in a note (an older routing, a call) counts too
        self.wk("piece", "note", "acme", "reports", "P1", "--add", "From Chat: asked about totals (https://chat.example.org/t/9)")
        self.assertEqual(self.wk("inbox", "check", "acme", "--link", "https://chat.example.org/t/9")[0], 1)

        # only the whole link counts: a shorter link inside a filed one is new, in the inbox and in the history
        self.assertEqual(self.wk("inbox", "check", "acme", "--link", "https://mail.example.org/m/abc?x=1")[0], 0)
        self.assertEqual(self.wk("inbox", "check", "acme", "--link", "https://chat.example.org/t/")[0], 0)
        self.assertEqual(self.wk("inbox", "add", "acme", "reports", "Totals follow-up", "--link",
                                 "https://chat.example.org/t/9#more", "--channel", "Chat")[0], 0)
        # two messages in one thread: the thread's link plus each message's sent time keeps them apart
        thread = "https://chat.example.org/space/AAA"
        for at in ("2020-01-02T14:32", "2020-01-02T15:05"):
            code, out = self.wk("inbox", "add", "acme", "reports", f"Thread message at {at}", "--link",
                                f"{thread}#{at}", "--channel", "Chat")
            self.assertEqual(code, 0, out)
        self.assertEqual(self.wk("inbox", "check", "acme", "--link", f"{thread}#2020-01-02T14:32")[0], 1)
        self.assertEqual(self.wk("inbox", "check", "acme", "--link", f"{thread}#2020-01-02T16:00")[0], 0)
        self.assertEqual(self.wk("inbox", "check", "acme", "--link", thread)[0], 0)
        # ids are never reused: I1 was handled, so the next ones are I2, I3, I4; handling the newest keeps the count
        self.assertEqual([x["id"] for x in work.inbox(work.Repo(settings.load(self.settings), "acme"), "reports")],
                         ["I2", "I3", "I4"])
        self.assertEqual(self.wk("inbox", "done", "acme", "reports", "I4", "--how", "FYI")[0], 0)
        self.wk("inbox", "add", "acme", "reports", "Next one", "--link", "https://x/next", "--channel", "Mail")
        self.assertIn("## I5 · Next one", self.on_github(".work/reports/inbox.md"))
        self.assertIn("Last id: I5\n", self.on_github(".work/reports/inbox.md"))

        self.assertEqual(self.wk("inbox", "done", "acme", "reports", "I1", "--how", "again")[0], 2)  # gone
        self.assertEqual(self.wk("inbox", "done", "acme", "reports", "I2")[0], 2)  # --how is required
        self.assertEqual(self.wk("inbox", "add", "acme", "reports", "No link", "--channel", "Mail")[0], 2)
        self.assertEqual(self.wk("inbox", "add", "acme", "reports", "No channel", "--link", "https://x/1")[0], 2)
        self.assertEqual(self.wk("inbox", "add", "acme", "reports", "x" * 201, "--link", "https://x/2",
                                 "--channel", "Mail")[0], 2)  # one line, never the message's text
        self.assertEqual(self.wk("inbox", "add", "acme", "reports", "Bad piece", "--link", "https://x/3",
                                 "--channel", "Mail", "--piece", "P9")[0], 2)
        self.assertEqual(self.wk("inbox", "add", "acme", "nope", "Bad list", "--link", "https://x/4",
                                 "--channel", "Mail")[0], 2)
        self.assertEqual(self.wk("inbox", "list", "acme")[0], 2)  # the list is needed

    def test_a_quiet_routing_run_writes_nothing(self):
        """What a run with nothing new does: show the channels, check a link already filed, move both times."""
        self.fill_channels()
        self.wk("inbox", "add", "acme", "reports", "Earlier message", "--link", "https://x/1", "--channel", "Mail")
        head, files = sh(self.app, "rev-parse", "HEAD"), sh(self.app, "status", "--porcelain")
        now = (datetime.now().astimezone() - timedelta(minutes=1)).isoformat(timespec="minutes")
        self.assertEqual(self.wk("channels", "acme")[0], 0)
        self.assertEqual(self.wk("inbox", "check", "acme", "--link", "https://x/1")[0], 1)  # the overlap's repeat
        self.assertEqual(self.wk("inbox", "add", "acme", "reports", "Earlier message", "--link", "https://x/1",
                                 "--channel", "Mail")[0], 1)
        self.assertEqual(self.wk("channels", "acme", "--checked", "Mail", "--at", now)[0], 0)
        self.assertEqual(self.wk("channels", "acme", "--checked", "Chat", "--at", now)[0], 0)
        self.assertEqual(sh(self.app, "rev-parse", "HEAD"), head)
        self.assertEqual(sh(self.app, "status", "--porcelain"), files)
        self.assertEqual(subprocess.run(["git", "--git-dir", str(self.origin), "rev-parse", "main"], capture_output=True,
                                        text=True).stdout.strip(), head)

    def recall(self, hook: dict | str, settings=None, host: str = "") -> tuple[int, str]:
        """wk recall as Claude's compaction hook runs it: the session as JSON on stdin."""
        out, real = io.StringIO(), (sys.stdin, os.environ.get("CLAUDE_CODE_HOST_SESSION_ID"))
        sys.stdin = io.StringIO(hook if isinstance(hook, str) else json.dumps(hook))
        os.environ["CLAUDE_CODE_HOST_SESSION_ID"] = host
        try:
            with redirect_stdout(out), redirect_stderr(out):
                code = cli.main(["--settings", str(settings or self.settings), "recall"])
        finally:
            sys.stdin = real[0]
            if real[1] is None:
                os.environ.pop("CLAUDE_CODE_HOST_SESSION_ID")
            else:
                os.environ["CLAUDE_CODE_HOST_SESSION_ID"] = real[1]
        return code, out.getvalue()

    def test_a_supervisor_gets_its_list_back_after_compaction(self):
        link = "claude://claude.ai/epitaxy/local_aaaa1111-2222"
        self.wk("product", "set", "acme", "reports", "--supervisor", link)
        self.wk("piece", "add", "acme", "reports", "Aged debt view")
        self.wk("piece", "add", "acme", "reports", "Old thing")
        self.wk("piece", "set", "acme", "reports", "P2", "--state", "merged", "--pr", "1")
        self.wk("piece", "note", "acme", "reports", "P1", "--add", "Pat wants it by Friday")
        self.wk("question", "add", "acme", "reports", "Ship before month end?", "--recommended", "yes", "--piece", "P1")
        self.wk("order", "add", "acme", "reports", "Never email the client")
        self.wk("piece", "add", "acme", "portal", "Login page")
        elsewhere = str(self.tmp)  # not a checkout: only the supervisor link can match
        for hook, host in (({"session_id": "local_aaaa1111-2222", "cwd": elsewhere}, ""),  # matched by the hook's id
                           ({"session_id": "cli-session", "cwd": elsewhere}, "local_aaaa1111-2222")):  # by the app's
            code, out = self.recall(hook, host=host)
            self.assertEqual(code, 0)
            self.assertIn("acme / reports", out)
            self.assertIn("P1 todo: Aged debt view", out)
            self.assertIn("Pat wants it by Friday", out)
            self.assertNotIn("Old thing", out)  # merged pieces stay out
            self.assertIn("Q1: Ship before month end?", out)
            self.assertIn("1. Never email the client", out)
            self.assertNotIn("Login page", out)  # another supervisor's list
        self.assertEqual(self.recall({"session_id": "local_aaaa1111-2222", "agent_id": "a1"})[1], "")  # a builder

    def test_recall_at_start_shows_builders_prs_decisions_and_turns_most_important_first(self):
        sid = "local_bbbb2222-3333"
        self.wk("product", "set", "acme", "reports", "--supervisor", f"claude://claude.ai/epitaxy/{sid}")
        self.wk("piece", "add", "acme", "reports", "Later idea")
        self.wk("piece", "add", "acme", "reports", "Faster export")
        self.wk("piece", "set", "acme", "reports", "P2", "--state", "building", "--branch", "p2-export", "--pr", "7",
                "--chat", "agent:abc123")
        self.wk("piece", "note", "acme", "reports", "P2", "--add", "Pat: ship it to staging first")
        self.wk("piece", "note", "acme", "reports", "P2", "--add", "Pat: keep the old export too")
        self.wk("piece", "note", "acme", "reports", "P2", "--add", "Pat: and email nobody")
        self.wk("piece", "note", "acme", "reports", "P2", "--add", "Builder started " + "word " * 80)
        self.wk("question", "add", "acme", "reports", "Merge the export?", "--piece", "P2")
        self.wk("order", "add", "acme", "reports", "Never email the client")
        prs = {"7": {"number": 7, "state": "OPEN", "isDraft": True, "statusCheckRollup": []}}
        held = [{"resource": "staging", "holder": "p2-export", "items": ["a.py"], "state": "1h left"},
                {"resource": "staging", "holder": "someone-else", "items": ["b.py"], "state": "1h left"}]
        hook = {"session_id": sid, "cwd": str(self.tmp)}
        with mock.patch.object(work, "_gh_prs", return_value=prs), mock.patch.object(work, "turn_rows",
                                                                                       return_value=held):
            heads = {src: self.recall(hook | {"source": src})[1].splitlines()[0]
                     for src in ("startup", "resume", "compact", "clear")}
            code, out = self.recall(hook | {"source": "startup"})
        self.assertIn("(this chat started)", heads["startup"])
        self.assertIn("(this chat resumed)", heads["resume"])
        self.assertIn("(your memory was compacted)", heads["compact"])
        self.assertNotIn("compacted", heads["startup"] + heads["resume"])
        self.assertEqual(code, 0)
        self.assertIn("- P2 building: Faster export · builder agent:abc123 · branch p2-export · PR 7 (draft, "
                      "no CI result)", out)
        latest = next(x for x in out.splitlines() if x.startswith("  latest:"))
        self.assertTrue(260 < len(latest) <= 312 and latest.endswith("word…"), latest)  # cut at a word, ~300
        decided = [x for x in out.splitlines() if x.startswith("  decided:")]
        self.assertEqual(len(decided), 2)  # the newest two of the owner's decisions
        self.assertIn("Pat: and email nobody", decided[1])
        self.assertNotIn("ship it to staging", out)
        self.assertIn("Turns held by this list's builders: p2-export on staging (a.py; 1h left)", out)
        self.assertNotIn("someone-else", out)
        order = [out.index(x) for x in ("Waiting on the owner:", "Being built or ready:", "To do:", "Standing orders:")]
        self.assertEqual(order, sorted(order))
        self.wk("piece", "note", "acme", "reports", "P2", "--add", "Pat: merge it")  # the newest line is a decision
        with mock.patch.object(work, "_gh_prs", side_effect=work.WorkkitError("gh: timed out")):
            code, out = self.recall(hook | {"source": "resume"})
        self.assertEqual(code, 0)
        self.assertEqual(out.count("Pat: merge it"), 1)  # shown once, as the newest line
        self.assertIn("PR status unavailable", out)
        self.assertIn("· PR 7\n", out)
        note = self.app / ".work" / "reports" / "notes" / "P2.md"
        with note.open("a", encoding="utf-8") as f:  # a date typed in brackets before the name still counts
            f.write("- 2026-01-03: (2026-01-03) Pat: bracketed call\n")
        self.wk("question", "answer", "acme", "reports", "Q1", "yes, merge")  # wk writes "Owner answered Q1 (...)"
        with mock.patch.object(work, "_gh_prs", return_value={}) as gh:  # GitHub answered, without PR 7
            out = self.recall(hook | {"source": "compact"})[1]
        self.assertLessEqual(gh.call_args[0][1], work.GH_WAIT)  # within the one shared deadline
        self.assertIn("· PR 7 (unknown)", out)
        self.assertEqual(out.count("Owner answered Q1"), 1)  # the newest line, not repeated
        self.assertIn("  latest: ", next(x for x in out.splitlines() if "Owner answered Q1" in x))
        self.assertIn("Pat: bracketed call", "".join(x for x in out.splitlines() if x.startswith("  decided:")))
        self.wk("piece", "note", "acme", "reports", "P2", "--add", "Builder resumed")
        with mock.patch.object(work, "GH_WAIT", 0), mock.patch.object(work, "_gh_prs") as gh:  # deadline passed
            out = self.recall(hook | {"source": "compact"})[1]
        gh.assert_not_called()
        self.assertIn("PR status unavailable", out)
        decided = [x for x in out.splitlines() if x.startswith("  decided:")]
        self.assertEqual(len(decided), 2)
        self.assertIn("bracketed call", decided[0])
        self.assertIn("Owner answered Q1 (Merge the export?): yes, merge", decided[1])
        self.assertEqual(self.recall(hook | {"agent_id": "a1", "source": "startup"})[1], "")  # a builder: nothing

    def test_recall_trims_by_importance_across_lists(self):
        sid = "local_dddd4444-5555"
        for product in ("reports", "portal"):
            if product == "portal":
                self.wk("product", "add", "acme", "portal", "--line", "Client portal")
            self.wk("product", "set", "acme", product, "--supervisor", f"claude://claude.ai/epitaxy/{sid}")
        cols = "\t".join(work.PIECE_COLUMNS)
        for product, rows in (("reports", [f"P{i}\tReports to-do number {i}\ttodo" + "\t" * 8 for i in range(1, 121)]),
                              ("portal", [f"P{i}\tPortal build number {i}\tbuilding" + "\t" * 8 for i in (1, 2)])):
            d = self.app / ".work" / product
            (d / "notes").mkdir(parents=True, exist_ok=True)
            (d / "pieces.tsv").write_text("\n".join([cols] + rows) + "\n", encoding="utf-8")
            for i in range(1, len(rows) + 1):
                (d / "notes" / f"P{i}.md").write_text(f"# P{i}\n\nBrief.\n\n- 2026-01-01: {product} note {i} " + "words " * 30
                                                      + "\n", encoding="utf-8")
        out = self.recall({"session_id": sid, "cwd": str(self.tmp), "source": "startup"})[1]
        self.assertLessEqual(len(out), work.RECALL_CAP + 1)
        self.assertIn("- P2 building: Portal build number 2", out)  # the second list's builds outrank the first's to-dos
        self.assertIn("portal note 2 ", out)
        self.assertNotIn("reports note 1 ", out)  # to-do notes go first, in every list
        self.assertIn("more open pieces: wk piece list acme reports", out)
        self.assertNotIn("more open pieces: wk piece list acme portal", out)

    def test_recall_stays_under_its_cap_trimming_to_do_notes_first(self):
        sid = "local_cccc3333-4444"
        self.wk("product", "set", "acme", "reports", "--supervisor", f"claude://claude.ai/epitaxy/{sid}")
        d = self.app / ".work" / "reports"
        (d / "notes").mkdir(parents=True, exist_ok=True)
        rows =["\t".join(work.PIECE_COLUMNS)] + ["\t".join([f"P{i}", f"Piece number {i}", "building" if i < 3 else
                                                              "todo"] + [""] * 8) for i in range(1, 201)]
        (d / "pieces.tsv").write_text("\n".join(rows) + "\n", encoding="utf-8")
        (d / "notes").mkdir(parents=True, exist_ok=True)
        for i in range(1, 201):
            (d / "notes" / f"P{i}.md").write_text(f"# P{i}\n\nBrief.\n\n- {date.today().isoformat()}: note {i} " + "words " * 40 + "\n",
                                                  encoding="utf-8")
        hook = {"session_id": sid, "cwd": str(self.tmp), "source": "startup"}
        out = self.recall(hook)[1]
        self.assertLessEqual(len(out), work.RECALL_CAP + 1)
        self.assertIn("note 1 ", out)  # pieces being built keep their notes
        self.assertNotIn("note 3 ", out)  # to-do pieces lost theirs first
        self.assertIn("- P3 todo: Piece number 3", out)
        hidden = next(x for x in out.splitlines() if "more open pieces" in x)
        self.assertEqual(hidden, f"{200 - out.count(' todo: ') - 2} more open pieces: wk piece list acme reports")
        (d / "pieces.tsv").write_text("\n".join(rows[:9]) + "\n", encoding="utf-8")  # 8 pieces fit with notes
        out = self.recall(hook)[1]
        self.assertIn("note 8 ", out)
        self.assertNotIn("more open pieces", out)

    def test_recall_shows_what_to_fix_after_the_builds_from_local_files_only(self):
        from wk import check
        sid = "local_eeee5555-6666"
        self.wk("product", "set", "acme", "reports", "--supervisor", f"claude://claude.ai/epitaxy/{sid}")
        d = self.app / ".work" / "reports"
        (d / "notes").mkdir(parents=True, exist_ok=True)
        rows = ["\t".join(work.PIECE_COLUMNS),
                "\t".join(["P1", "Faster export", "building", "p1", "7", "", "", "agent:a1"] + [""] * 3),
                "\t".join(["P2", "Old idea", "todo", "", "", "", "2026-01-01"] + [""] * 4),
                "\t".join(["P3", "A name with far too many words in it now", "todo"] + [""] * 8)]
        (d / "pieces.tsv").write_text("\n".join(rows) + "\n", encoding="utf-8")
        for i in (1, 2, 3):
            day = date.today().isoformat() if i == 3 else "2026-01-01"
            (d / "notes" / f"P{i}.md").write_text(f"# P{i}\n\nBrief.\n\n- {day}: note {i}\n", encoding="utf-8")
        (self.app / "billing").mkdir()
        (self.app / "billing" / "client.toml").write_text("", encoding="utf-8")  # no coverage_from: the owner's
        hook = {"session_id": sid, "cwd": str(self.tmp), "source": "startup"}
        no = mock.Mock(side_effect=AssertionError("network at start"))
        with mock.patch.object(work, "_gh_prs", return_value={}), mock.patch.object(check, "_gh", no), \
                mock.patch.object(check, "runs", no), mock.patch.object(check, "secrets", no), \
                mock.patch.object(check.usage, "memory", no), mock.patch.object(check, "_facts", no):
            out = self.recall(hook)[1]
        lines = out.splitlines()
        self.assertIn("To fix (yours, unless marked; wk check acme reports for all):", lines)
        fix = [x for x in lines if x.startswith("- P3: name has") or "(needs the owner's yes)" in x]
        self.assertEqual(len(fix), 3, out)  # P3's name; P2 untouched for a month and the billing file, the owner's
        self.assertTrue(any(x.startswith("- P2 (needs the owner's yes): has been a to-do") for x in fix), out)
        self.assertTrue(any(x.startswith("- billing/client.toml (needs the owner's yes)") for x in fix), out)
        order = [out.index(x) for x in ("Being built or ready:", "To fix (", "To do:")]
        self.assertEqual(order, sorted(order))  # after the builds, before the to-dos
        # trimmed under the cap: to-do notes, then to-dos, then to-fix lines, before any piece being built
        for i in range(4, 80):
            rows.append("\t".join([f"P{i}", f"Piece number {i}", "todo"] + [""] * 8))  # no note: a to-fix line
        (d / "pieces.tsv").write_text("\n".join(rows) + "\n", encoding="utf-8")
        with mock.patch.object(work, "_gh_prs", return_value={}):
            out = self.recall(hook)[1]
        self.assertLessEqual(len(out), work.RECALL_CAP + 1)
        self.assertIn("- P1 building: Faster export", out)
        self.assertIn("note 1", out)
        self.assertNotIn(" todo: ", out)  # the to-dos went before the to-fix lines
        lines = out.splitlines()
        start = lines.index("To fix (yours, unless marked; wk check acme reports for all):")
        more = next(i for i, x in enumerate(lines) if "more to fix" in x)
        self.assertGreater(more - start - 1, 0)  # some to-fix lines still show
        self.assertEqual(lines[more], f"{3 + 76 - (more - start - 1)} more to fix: wk check acme reports")

    def test_recall_is_quiet_in_other_chats_and_never_fails(self):
        self.wk("piece", "add", "acme", "reports", "Aged debt view")
        code, out = self.recall({"session_id": "someone-else", "cwd": str(self.app)})
        self.assertEqual(code, 0)
        self.assertEqual(out.count("\n"), 1)  # one line in a checkout of a known repo
        self.assertIn("acme reports: 1 open", out)
        self.assertNotIn("reread", out)  # a fact, not a command
        self.assertEqual(self.recall({"session_id": "someone-else", "cwd": str(self.app), "source": "clear"})[1], out)
        for source in ("startup", "resume"):  # a chat that supervises no list starts with nothing added
            self.assertEqual(self.recall({"session_id": "someone-else", "cwd": str(self.app), "source": source}),
                             (0, ""))
        self.assertEqual(self.recall({"session_id": "someone-else", "cwd": str(self.tmp)}), (0, ""))
        broken = self.tmp / "broken.toml"
        broken.write_text("owner = [", encoding="utf-8")
        for hook, where in (({"session_id": "x", "cwd": str(self.app)}, broken),
                            ({"cwd": str(self.app)}, self.tmp / "missing.toml"),
                            ("not json", self.settings), ("", self.settings), ("[1]", self.settings)):
            self.assertEqual(self.recall(hook, settings=where), (0, ""), (hook, where))


    # ------------------------------------------------------------ the owner-approved queue (P67)

    def queued_list(self):
        for name in ("Faster export", "Aged debt view", "Fix totals", "Old idea", "Login page"):
            self.wk("piece", "add", "acme", "reports", name)
        brief = self.tmp / "brief.md"
        brief.write_text("# brief\n\nGoal: ...\n", encoding="utf-8")
        for pid in ("P1", "P2", "P3", "P4", "P5"):
            self.wk("piece", "note", "acme", "reports", pid, "--file", str(brief))
        self.wk("piece", "set", "acme", "reports", "P1", "--state", "building", "--branch", "p1", "--files", "wk/a.py")
        self.wk("piece", "set", "acme", "reports", "P2", "--files", "wk/b.py")
        self.wk("piece", "set", "acme", "reports", "P3", "--files", "wk/")  # overlaps P1, being built
        self.wk("piece", "set", "acme", "reports", "P4", "--files", "docs/*.md", "--after", "P2")
        self.wk("piece", "set", "acme", "reports", "P5", "--files", "none")

    def test_the_owner_approved_queue_is_recorded_in_one_write_and_starting_takes_a_piece_off(self):
        self.queued_list()
        r = work.Repo(settings.load(self.settings), "acme")
        self.wk("piece", "add", "acme", "reports", "No brief yet")
        self.assertIn("has none", self.wk("piece", "queue", "acme", "reports", "P6")[1])  # no brief in its note
        self.wk("piece", "note", "acme", "reports", "P6", "--add", "brief: do it")
        self.assertIn("Name the files", self.wk("piece", "queue", "acme", "reports", "P6")[1])
        self.assertEqual(self.wk("piece", "queue", "acme", "reports", "P1")[0], 2)  # being built: not a to-do
        self.assertEqual(self.wk("piece", "queue", "acme", "reports", "P2,P2")[0], 2)
        self.assertEqual(self.wk("piece", "set", "acme", "reports", "P2", "--after", "P2")[0], 2)  # not itself
        self.assertEqual(self.wk("piece", "set", "acme", "reports", "P2", "--after", "P9")[0], 2)  # no such piece
        self.assertEqual(self.wk("piece", "set", "acme", "reports", "P2", "--files", "a.py,none")[0], 2)
        before = sh(self.app, "rev-list", "--count", "HEAD")
        code, out = self.wk("piece", "queue", "acme", "reports", "p3,P2,P4,P5")
        self.assertEqual(code, 0, out)
        self.assertEqual(int(sh(self.app, "rev-list", "--count", "HEAD")), int(before) + 1)
        place = lambda: {x["id"]: x["queue"] for x in work.pieces(r, "reports")}  # noqa: E731
        self.assertEqual(place(), {"P1": "", "P2": "2", "P3": "1", "P4": "3", "P5": "4", "P6": ""})
        self.assertIn("\tqueue\tfiles\tafter", self.on_github(".work/reports/pieces.tsv").splitlines()[0])
        self.wk("piece", "set", "acme", "reports", "P2", "--state", "building")
        self.assertEqual(place()["P2"], "")  # started: off the queue
        self.wk("piece", "queue", "acme", "reports", "none")
        self.assertEqual(set(place().values()), {""})

    def test_next_starts_what_fits_and_says_why_the_rest_wait(self):
        self.queued_list()
        self.wk("piece", "queue", "acme", "reports", "P3,P2,P4,P5")
        prs = {"12": {"branch": "other-list", "files": ["docs/guide.md"]}}
        with mock.patch.object(work, "open_pr_files", return_value=prs), \
                mock.patch.object(work, "turn_rows", return_value=[]):
            code, out = self.wk("piece", "next", "acme", "reports")
        self.assertEqual(code, 0, out)
        self.assertIn("(1 builder running;", out)  # no limit unless the settings file sets one
        self.assertIn("4. P5 Login page: start now", out)
        self.settings.write_text("builders_per_list = 2\n" + self.settings.read_text(encoding="utf-8"), encoding="utf-8")
        with mock.patch.object(work, "open_pr_files", return_value=prs), \
                mock.patch.object(work, "turn_rows", return_value=[]):
            code, out = self.wk("piece", "next", "acme", "reports")
        self.assertEqual(code, 0, out)
        self.assertIn("1 of 2 builders running", out)
        self.assertIn("1. P3 Fix totals: waits: overlaps P1, being built (wk/a.py)", out)
        self.assertIn("2. P2 Aged debt view: start now", out)
        self.assertIn("3. P4 Old idea: waits: after P2, not merged yet", out)
        self.assertIn("4. P5 Login page: waits: no free builder (2 of 2 running)", out)  # P2 took the slot
        with mock.patch.object(work, "open_pr_files", side_effect=work.WorkkitError("gh: timed out")), \
                mock.patch.object(work, "turn_rows", return_value=[]):
            out = self.wk("piece", "next", "acme", "reports")[1]
        self.assertIn("P2 Aged debt view: waits: GitHub didn't answer", out)  # unknown is never a start
        self.assertIn("P5 Login page: start now", out)  # touches no repo files

    def test_queue_plan_limits_overlaps_turns_and_dependencies(self):
        row = lambda pid, state="todo", queue="", files="", after="", branch="", pr="": {  # noqa: E731
            "id": pid, "piece": pid, "state": state, "queue": queue, "files": files, "after": after, "branch": branch,
            "pr": pr}
        rows = [row("P1", "ready", pr="7"), row("P2", "merged"), row("P3", queue="1", files="a.py", after="P2"),
                row("P4", queue="2", files="lib/x.py"), row("P5", queue="3", files="src/*.js"),
                row("P6", queue="4", files="b.py", after="P1"), row("P7", queue="5", files="c.py")]
        held = [{"resource": "staging", "holder": "someone", "items": ["src/app.js"]}]
        q = work.queue_plan(rows, 2, {"7": {"branch": "p1", "files": ["lib/x.py"]}}, held)
        why = {x["id"]: x["why"] for x in q["items"]}
        self.assertEqual(q["building"], 0)  # P1 waits on a merge: it holds no builder
        self.assertEqual(why["P3"], "")  # its dependency merged
        self.assertEqual(why["P4"], "overlaps PR 7 (lib/x.py)")  # an unmerged PR holds its file
        self.assertEqual(why["P5"], "overlaps a turn on staging held by someone (src/app.js)")
        self.assertEqual(why["P6"], "after P1, not merged yet")
        self.assertEqual(why["P7"], "")
        self.assertEqual(work.queue_plan(rows, 1, {}, [])["items"][-1]["why"],
                         "no free builder (1 of 1 running)")
        self.assertIsNone(settings.builders_per_list({}))  # no limit unless set
        self.assertEqual([x["why"] for x in work.queue_plan(rows, None, {}, [])["items"]].count(""), 4)
        self.assertEqual(settings.builders_per_list({"builders_per_list": 3}), 3)
        for bad in (0, "2", True):
            with self.assertRaises(work.WorkkitError):
                settings.builders_per_list({"builders_per_list": bad})

    def test_recall_shows_the_queue_and_why_each_piece_waits(self):
        sid = "local_cccc3333-4444"
        self.wk("product", "set", "acme", "reports", "--supervisor", f"claude://claude.ai/epitaxy/{sid}")
        self.queued_list()
        self.wk("piece", "queue", "acme", "reports", "P3,P2")
        with mock.patch.object(work, "open_pr_files", return_value={}), mock.patch.object(work, "_gh_prs",
                                                                                           return_value={}):
            out = self.recall({"session_id": sid, "cwd": str(self.tmp), "source": "startup"})[1]
        self.assertIn("Queue, approved by the owner (1 builder running;", out)
        self.assertIn("1. P3 Fix totals: waits: overlaps P1, being built (wk/a.py)", out)
        self.assertIn("2. P2 Aged debt view: start now", out)
        self.assertLess(out.index("Being built or ready:"), out.index("Queue, approved"))

    def test_a_withdrawn_question_is_the_supervisors_call_never_the_owners(self):
        self.wk("piece", "add", "acme", "reports", "Aged debt view")
        self.wk("question", "add", "acme", "reports", "Start the builder in sandbox now?", "--piece", "P1")
        self.assertEqual(self.wk("question", "withdraw", "acme", "reports", "Q1")[0], 2)  # needs --reason
        code, out = self.wk("question", "withdraw", "acme", "reports", "Q1", "--reason",
                            "started it: sandbox only, stopped any time")
        self.assertEqual(code, 0, out)
        r = work.Repo(settings.load(self.settings), "acme")
        self.assertEqual(work.questions(r, "reports"), [])
        text = work.read_note(r, "reports", "P1")
        self.assertIn("Supervisor withdrew Q1 (Start the builder in sandbox now?), deciding it: started it", text)
        self.assertEqual(work._decisions(text, "Pat"), [])  # not recorded as the owner's decision
        # a whole-list question names the piece whose note keeps the decision
        self.wk("question", "add", "acme", "reports", "Run reviews in sandbox first?", "--list-wide")
        qid = work.questions(r, "reports")[0]["id"]
        code, out = self.wk("question", "withdraw", "acme", "reports", qid, "--reason", "yes: reversible")
        self.assertEqual(code, 2, out)
        self.assertIn("--piece", out)
        self.assertEqual(len(work.questions(r, "reports")), 1)  # still waiting
        code, out = self.wk("question", "withdraw", "acme", "reports", qid, "--reason", "yes: reversible",
                            "--piece", "P1")
        self.assertEqual(code, 0, out)
        self.assertEqual(work.questions(r, "reports"), [])
        self.assertIn(f"Supervisor withdrew {qid} (Run reviews in sandbox first?), deciding it: yes",
                      work.read_note(r, "reports", "P1"))

if __name__ == "__main__":
    unittest.main()
