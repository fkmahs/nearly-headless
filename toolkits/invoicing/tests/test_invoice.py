"""The invoicing toolkit against a real work-kit: a client repo with .work/ hours and its billing/ record, pushed to a
bare origin."""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
KIT = HERE.parents[1]
PROFILE = {"name": "Pat Lee", "business": "Consulting", "address": ["1 Main St", "Town"], "email": "pat@example.org"}

import invoice  # noqa: E402


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.settings = self.tmp / "settings.toml"
        self.settings.write_text('owner = "Pat Lee"\n', encoding="utf-8")
        (self.tmp / "profile.json").write_text(json.dumps(PROFILE), encoding="utf-8")
        # no daily self-update: it would fetch from GitHub, and could change work-kit, in the middle of a test
        self.env = {"WORKKIT_HOME": str(KIT), "WORKKIT_SETTINGS": str(self.settings),
                    "INVOICING_PROFILE": str(self.tmp / "profile.json"), "WORKKIT_AUTO_UPDATE": "0"}
        self.old_env = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)

    def tearDown(self):
        for k, v in self.old_env.items():
            os.environ.pop(k) if v is None else os.environ.__setitem__(k, v)

    def repo(self, name):
        origin, app = self.tmp / f"{name}.git", self.tmp / name
        subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
        app.mkdir()
        git(app, "init", "-b", "main")
        git(app, "config", "user.name", "t")
        git(app, "config", "user.email", "t@example.org")
        (app / "code.js").write_text("x\n", encoding="utf-8")
        git(app, "add", "-A")
        git(app, "commit", "-m", "init")
        git(app, "remote", "add", "origin", str(origin))
        git(app, "push", "-u", "origin", "main")
        with self.settings.open("a", encoding="utf-8") as f:
            f.write(f'\n[repos.{name}]\npath = "{app.as_posix()}"\n')
        return app

    def wk(self, *args):
        run = subprocess.run([sys.executable, "-m", "wk", *args], capture_output=True, text=True, encoding="utf-8",
                             env=os.environ | {"PYTHONPATH": str(KIT)})
        self.assertEqual(run.returncode, 0, run.stderr)

    def client(self, name, toml):
        folder = self.tmp / name / "billing"  # in the client's own repo
        folder.mkdir()
        (folder / "client.toml").write_text(toml, encoding="utf-8")
        return invoice.Client(folder, name)

    def run_invoice(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = invoice.main(list(argv))
        return code, out.getvalue() + err.getvalue()


def old_statement(c, inv):
    """Turn a made invoice into how one made before 1.30 looks: the .md in the old layout, which the new reader can't
    read, plus the .csv holding the lines, both hashed in the ledger."""
    import csv
    rows = invoice.statement(c, inv)
    with (c.dir / f"{inv}.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, invoice.COLUMNS, quoting=csv.QUOTE_ALL, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    (c.dir / f"{inv}.md").write_text(f"# {inv}\n\n| Date | Repo | Product | Work | Hours |\n|---|---|---|---|---:|\n",
                                     encoding="utf-8", newline="\n")
    ledger = c.ledger()
    ledger[inv]["sha256"] = {k: invoice.digest(c.dir / f"{inv}.{k}") for k in ("md", "csv")}
    c.save_ledger(ledger)


ACME = """prefix = "ACM"
repos = ["acme"]
rate = 100
calls_product = "calls"
calls_section = "Calls with Acme"
coverage_from = "2026-10-01"
[client]
name = "Acme Ltd"
notes = ["Please quote <ID> with payment."]
[products.reports]
group = "Reporting"
ref = "R1"
"""


class Acme(Fixture):
    def setUp(self):
        super().setUp()
        self.app = self.repo("acme")
        self.wk("product", "add", "acme", "reports", "--line", "**Built** the monthly reports")
        self.wk("product", "add", "acme", "calls", "--line", "Calls")
        self.wk("piece", "add", "acme", "reports", "Monthly reports")
        self.wk("hours", "add", "acme", "reports", "2", "--note", "Aged debt — review", "--date", "2026-10-20",
                "--for", "P1")
        self.wk("hours", "add", "acme", "calls", "0.5", "--note", "Call with Sam: scope", "--date", "2026-10-21")
        self.wk("hours", "add", "acme", "reports", "1", "--note", "Next week", "--date", "2026-10-27", "--for", "p1")
        self.c = self.client("acme", ACME)


class Invoicing(Acme):

    def test_make_marks_hours_and_check_agrees(self):
        view = invoice.preview(self.c, "2026-10-23")
        self.assertEqual((view["id"], view["hours"], view["amount"]), ("ACM-INV-001", 2.5, 250.0))
        self.assertEqual([s["title"] for s in view["sections"]], ["Reporting", "Calls with Acme"])
        result = invoice.make(self.c, "2026-10-23", "2026-10-23", pdf=False)
        self.assertEqual(result["marked"], ["acme"])
        tsv = (self.app / ".work" / "hours.tsv").read_text(encoding="utf-8")
        self.assertEqual(tsv.count("\tACM-INV-001"), 2)
        page = invoice.render_html(invoice.invoice_data(self.c, "ACM-INV-001"), PROFILE)
        self.assertIn("<strong>Built</strong> the monthly reports", page)
        self.assertIn("Please quote ACM-INV-001", page)
        self.assertTrue(invoice.check(self.c)["ok"])
        for f in self.c.dir.glob("[!c]*"):  # what the toolkit writes is LF only, so a repo that stores LF keeps their hashes
            self.assertNotIn(b"\r", f.read_bytes(), f.name)
        nxt = invoice.preview(self.c, "2026-10-30")  # next week: only the new hours, period continues
        self.assertEqual((nxt["id"], nxt["hours"], nxt["period"]), ("ACM-INV-002", 1.0, ["2026-10-24", "2026-10-30"]))

    def test_paid_needs_sent_and_edits_are_caught(self):
        invoice.make(self.c, "2026-10-23", "2026-10-23", pdf=False)
        self.assertEqual(self.run_invoice("paid", "acme", "ACM-INV-001")[0], 1)
        self.assertEqual(self.run_invoice("sent", "acme", "acm-inv-001", "--date", "2026-10-23")[0], 0)
        self.assertEqual(self.run_invoice("paid", "acme", "ACM-INV-001")[0], 0)
        with (self.c.dir / "ACM-INV-001.md").open("a", encoding="utf-8") as f:
            f.write("edited\n")
        self.assertIn("ACM-INV-001.md changed after it was made", invoice.check(self.c)["problems"])
        with self.assertRaises(invoice.Refused) as caught:  # and its PDF is never printed again from the edit
            invoice.print_pdf(self.c, "ACM-INV-001")
        self.assertIn("ACM-INV-001.md is missing or was changed after the invoice was made, so no PDF is printed",
                      str(caught.exception))

    def test_line_breaks_of_any_kind_in_a_note_read_back_exactly(self):
        view = invoice.preview(self.c, "2026-10-23")
        tricky = "Word\x0bsoft break\x85x\x0c\x1c\x1d\x1e \r\n|&#11;&amp;<>"
        view["rows"][0]["note"] = tricky
        text = invoice.statement_md(self.c, "ACM-INV-001", "2026-10-23", view)
        self.assertEqual(text.splitlines(), text.split("\n")[:-1])  # no line breaks but the table's own
        self.assertEqual(invoice.read_md(text)[0]["note"], tricky)

    def test_unknown_product_or_jargon_blocks_making(self):
        self.wk("product", "add", "acme", "portal", "--line", "Rewrote portal.js with SuiteQL")
        self.wk("hours", "add", "acme", "portal", "1", "--note", "x", "--date", "2026-10-22")
        with self.assertRaises(invoice.Refused) as caught:
            invoice.make(self.c, "2026-10-23", pdf=False)
        self.assertIn("no group or ref", str(caught.exception))
        self.assertIn("mentions 'portal.js'", str(caught.exception))
        self.assertFalse(list(self.c.dir.glob("ACM-*")))

    def test_the_statement_is_one_readable_file_that_reads_back_exactly(self):
        self.wk("hours", "add", "acme", "reports", "1", "--note", "Fix a | b & <c> \\ d", "--date", "2026-10-22",
                "--for", "P1")
        view = invoice.preview(self.c, "2026-10-23")
        invoice.make(self.c, "2026-10-23", "2026-10-23", pdf=False)
        self.assertEqual(sorted(f.name for f in self.c.dir.iterdir()), ["ACM-INV-001.md", "client.toml", "ledger.json"])
        self.assertEqual(list(self.c.ledger()["ACM-INV-001"]["sha256"]), ["md"])
        text = (self.c.dir / "ACM-INV-001.md").read_text(encoding="utf-8")
        self.assertIn("| 2026-10-22 | acme | reports | Fix a &#124; b &amp; &lt;c&gt; \\ d | 1 | ", text)
        self.assertEqual(invoice.statement(self.c, "ACM-INV-001"),
                         [{k: str(r[k]) for k in invoice.COLUMNS} for r in view["rows"]])
        self.assertTrue(invoice.check(self.c)["ok"], invoice.check(self.c)["problems"])
        (self.c.dir / "ACM-INV-001.md").unlink()
        self.assertIn("ACM-INV-001.md is missing", invoice.check(self.c)["problems"])

    def test_an_invoice_made_before_one_file_statements_keeps_working_unchanged(self):
        invoice.make(self.c, "2026-10-23", "2026-10-23", pdf=False)
        old_statement(self.c, "ACM-INV-001")
        before = {f.name: f.read_bytes() for f in self.c.dir.iterdir()}
        self.assertTrue(invoice.check(self.c)["ok"], invoice.check(self.c)["problems"])
        self.assertEqual(invoice.invoice_data(self.c, "ACM-INV-001")["hours"], 2.5)  # read from its csv
        invoice.make(self.c, "2026-10-30", "2026-10-30", pdf=False)  # the next one is one file
        self.assertFalse((self.c.dir / "ACM-INV-002.csv").exists())
        self.assertTrue(invoice.check(self.c)["ok"], invoice.check(self.c)["problems"])
        self.assertEqual({n: (self.c.dir / n).read_bytes() for n in before if n != "ledger.json"},
                         {n: b for n, b in before.items() if n != "ledger.json"})  # the old statement never changes
        self.assertEqual(self.c.ledger()["ACM-INV-001"]["sha256"], json.loads(before["ledger.json"])["ACM-INV-001"]["sha256"])
        with (self.c.dir / "ACM-INV-001.csv").open("a", encoding="utf-8") as f:
            f.write("\n")  # any change at all
        self.assertIn("ACM-INV-001.csv changed after it was made", invoice.check(self.c)["problems"])

    def test_a_statement_that_would_not_read_back_is_never_made(self):
        self.wk("hours", "add", "acme", "reports", "1", "--note", "x", "--date", "2026-10-22", "--for", "P1")
        real = invoice.read_md
        invoice.read_md = lambda text: []
        try:
            with self.assertRaises(invoice.Refused) as caught:
                invoice.make(self.c, "2026-10-23", pdf=False)
        finally:
            invoice.read_md = real
        self.assertIn("would not read back exactly", str(caught.exception))
        self.assertEqual((self.c.ledger(), list(self.c.dir.glob("ACM-*"))), ({}, []))

    def test_cents_in_the_rate_never_split_total_and_lines(self):
        self.wk("product", "add", "acme", "portal", "--line", "Portal updates")
        self.wk("piece", "add", "acme", "portal", "Portal")
        self.wk("hours", "add", "acme", "portal", "0.25", "--note", "x", "--date", "2026-10-22", "--for", "P1")
        cfg = ACME.replace("rate = 100", "rate = 112.5") + '[products.portal]\ngroup = "Portal"\nref = "R2"\n'
        (self.c.dir / "client.toml").write_text(cfg, encoding="utf-8")
        c = invoice.Client(self.c.dir, "acme")
        view = invoice.preview(c, "2026-10-23")
        self.assertEqual(view["amount"], round(sum(s["amount"] for s in view["sections"]), 2))
        invoice.make(c, "2026-10-23", "2026-10-23", pdf=False)
        data = invoice.invoice_data(c, view["id"])  # used to refuse: lines and ledger disagreed by a cent
        self.assertEqual(data["amount"], view["amount"])
        self.assertTrue(invoice.check(c)["ok"], invoice.check(c)["problems"])

    def test_a_refused_mark_undoes_the_statement(self):
        real = invoice.wk

        def refuse(*args):
            if args[:2] == ("hours", "mark"):
                raise invoice.Refused("hours changed")
            return real(*args)
        invoice.wk = refuse
        try:
            with self.assertRaises(invoice.Refused):
                invoice.make(self.c, "2026-10-23", pdf=False)
        finally:
            invoice.wk = real
        self.assertEqual((self.c.ledger(), list(self.c.dir.glob("ACM-*"))), ({}, []))


class Coverage(Acme):
    """No finished piece is missed: each is paid for by hours, or marked bill later or not billing."""
    FINISH = "merged"

    def setUp(self):
        super().setUp()
        self.wk("piece", "add", "acme", "reports", "Aged debt report")
        self.wk("piece", "set", "acme", "reports", "P2", "--state", self.FINISH, *(["--pr", "1"] * (self.FINISH == "merged")))
        # the day work-kit recorded it finished, not this process's clock, so a run across midnight still agrees
        pieces = invoice.wk("piece", "list", "acme", "reports", "--all")
        self.today = next(x for x in pieces if x["id"] == "P2")["finished"]

    def refused(self, through):
        made = sorted(self.c.dir.glob("ACM-*"))
        with self.assertRaises(invoice.Refused) as caught:
            invoice.make(self.c, through, pdf=False)
        self.assertEqual(sorted(self.c.dir.glob("ACM-*")), made)  # nothing new was made
        return str(caught.exception)

    def test_finished_work_without_hours_blocks_the_invoice(self):
        view = invoice.preview(self.c, self.today)
        self.assertEqual(view["coverage"]["not_covered"], ["acme reports P2: Aged debt report"])
        self.assertIn("P2: Aged debt report: finished but not billed", self.refused(self.today))
        self.assertEqual(invoice.preview(self.c, "2026-10-23")["coverage"]["not_covered"], [])  # finished after then

    def test_bill_later_or_not_billing_needs_a_reason_and_clears_the_block(self):
        run = subprocess.run([sys.executable, "-m", "wk", "piece", "set", "acme", "reports", "P2", "--bill", "later"],
                             capture_output=True, text=True, env=os.environ | {"PYTHONPATH": str(KIT)})
        self.assertEqual(run.returncode, 2)
        self.wk("piece", "set", "acme", "reports", "P2", "--bill", "later", "--reason", "Not handed over yet")
        self.assertIn("Bill later: Not handed over yet",
                      (self.app / ".work" / "reports" / "notes" / "P2.md").read_text(encoding="utf-8"))
        view = invoice.preview(self.c, self.today)
        self.assertEqual((view["coverage"]["later"], view["problems"]), (["acme reports P2: Aged debt report"], []))
        self.wk("piece", "set", "acme", "reports", "P2", "--bill", "no", "--reason", "Covered by the fixed fee")
        self.assertEqual(invoice.preview(self.c, self.today)["coverage"]["later"], [])
        invoice.make(self.c, self.today, pdf=False)

    def test_hours_for_the_piece_cover_it(self):
        self.wk("hours", "add", "acme", "reports", "1", "--note", "Aged debt", "--date", "2026-10-22", "--for", "P2")
        view = invoice.preview(self.c, self.today)
        self.assertEqual((view["coverage"]["not_covered"], view["problems"]), ([], []))
        invoice.make(self.c, self.today, pdf=False)
        self.assertTrue(invoice.check(self.c)["ok"])

    def test_hours_must_name_their_pieces(self):
        self.wk("hours", "add", "acme", "reports", "1", "--note", "Aged debt", "--date", "2026-10-22", "--again")
        self.assertIn("name the pieces these hours pay for", self.refused("2026-10-23"))
        self.wk("hours", "fix", "acme", "reports", "--date", "2026-10-22", "--for", "P2")
        self.assertEqual(invoice.preview(self.c, "2026-10-23")["problems"], [])

    def backdate(self, pid, column, day):
        path = self.app / ".work" / "reports" / "pieces.tsv"
        lines = path.read_text(encoding="utf-8").splitlines()
        at = lines[0].split("\t").index(column)
        rows = [ln.split("\t") for ln in lines[1:]]
        for r in rows:
            if r[0] == pid:
                r[at] = day
        path.write_text("\n".join([lines[0]] + ["\t".join(r) for r in rows]) + "\n", encoding="utf-8", newline="\n")

    def test_editing_a_billed_piece_never_reopens_it(self):
        self.backdate("P2", "finished", "2026-10-20")
        self.wk("hours", "add", "acme", "reports", "1", "--note", "Aged debt", "--date", "2026-10-20", "--for", "P2",
                "--again")
        invoice.make(self.c, "2026-10-23", "2026-10-23", pdf=False)
        self.wk("piece", "set", "acme", "reports", "P2", "--name", "Aged debt report for finance", "--chat", "x")
        view = invoice.preview(self.c, self.today)
        self.assertEqual((view["coverage"]["not_covered"], view["problems"]), ([], []))

    def test_an_invoice_covers_only_up_to_its_period_end(self):
        self.backdate("P2", "finished", "2026-10-20")
        self.wk("hours", "add", "acme", "reports", "1", "--note", "Aged debt", "--date", "2026-10-20", "--for", "P2",
                "--again")
        invoice.make(self.c, "2026-10-23", self.today, pdf=False)  # made today, for the period up to 10-23
        self.backdate("P2", "finished", "2026-10-25")  # more work, finished after that period
        self.assertEqual(invoice.preview(self.c, self.today)["coverage"]["not_covered"],
                         ["acme reports P2: Aged debt report (finished after it was last billed, with no new hours)"])

    def test_a_later_piece_with_unbilled_hours_is_a_problem(self):
        self.wk("hours", "add", "acme", "reports", "1", "--note", "Aged debt", "--date", "2026-10-22", "--for", "P2")
        self.backdate("P2", "bill", "later")  # as an older work-kit or a hand edit could leave it
        self.assertIn("P2: Aged debt report is marked bill later but has unbilled hours",
                      " ".join(invoice.preview(self.c, self.today)["problems"]))

    def test_a_later_piece_billed_with_its_work_dates_is_done(self):
        self.wk("piece", "set", "acme", "reports", "P2", "--bill", "later", "--reason", "Not handed over")
        self.assertEqual(invoice.preview(self.c, self.today)["coverage"]["later"], ["acme reports P2: Aged debt report"])
        self.wk("piece", "set", "acme", "reports", "P2", "--bill", "yes")
        self.wk("hours", "add", "acme", "reports", "1", "--note", "Aged debt", "--date", "2026-10-22", "--for", "P2")
        invoice.make(self.c, self.today, self.today, pdf=False)  # its hours are dated before it finished
        view = invoice.preview(self.c, self.today)
        self.assertEqual((view["coverage"]["later"], view["coverage"]["not_covered"]), ([], []))

    def test_hours_changed_between_counting_and_marking_refuse(self):
        self.wk("piece", "set", "acme", "reports", "P2", "--bill", "no", "--reason", "x")
        real = invoice.wk

        def meddle(*args):
            if args[:2] == ("hours", "mark"):  # same total, different entry: someone fixed a note meanwhile
                self.wk("hours", "fix", "acme", "reports", "--date", "2026-10-20", "--note", "Something else")
            return real(*args)
        invoice.wk = meddle
        try:
            with self.assertRaises(invoice.Refused) as caught:
                invoice.make(self.c, "2026-10-23", pdf=False)
        finally:
            invoice.wk = real
        self.assertIn("same total, different entries", str(caught.exception))
        self.assertEqual((self.c.ledger(), list(self.c.dir.glob("ACM-*"))), ({}, []))

    def test_work_billed_before_is_shown_again(self):
        self.wk("piece", "set", "acme", "reports", "P2", "--bill", "no", "--reason", "x")
        invoice.make(self.c, "2026-10-23", "2026-10-23", pdf=False)
        view = invoice.preview(self.c, self.today)
        self.assertEqual(view["coverage"]["billed_before"], ["acme reports P1: Monthly reports (also on ACM-INV-001)"])

    def test_finishing_after_it_was_billed_needs_new_hours(self):
        self.wk("hours", "add", "acme", "reports", "1", "--note", "Aged debt", "--date", "2026-10-22", "--for", "P2")
        invoice.make(self.c, "2026-10-23", "2026-10-23", pdf=False)  # P2 billed in part, then finished today
        view = invoice.preview(self.c, self.today)
        self.assertEqual(view["coverage"]["not_covered"],
                         ["acme reports P2: Aged debt report (finished after it was last billed, with no new hours)"],
                         {k: view[k] for k in ("coverage", "problems")})
        self.assertIn("finished after it was last billed", self.refused(self.today))

    def test_not_billing_after_part_billing_never_blocks(self):
        self.wk("hours", "add", "acme", "reports", "1", "--note", "Aged debt", "--date", "2026-10-22", "--for", "P2")
        invoice.make(self.c, "2026-10-23", "2026-10-23", pdf=False)
        self.wk("piece", "set", "acme", "reports", "P2", "--bill", "no", "--reason", "Rest is in the fixed fee")
        self.assertEqual(invoice.preview(self.c, self.today)["problems"], [])

    def test_an_unquoted_start_date_works_and_a_bad_one_is_named(self):
        for value, problem in (("2026-10-01", None), ('"soon"', "needs coverage_from = \"YYYY-MM-DD\"")):
            (self.c.dir / "client.toml").write_text(ACME.replace('"2026-10-01"', value), encoding="utf-8")
            view = invoice.preview(invoice.Client(self.c.dir, "acme"), "2026-10-23")
            if problem:
                self.assertIn(problem, " ".join(view["problems"]))
            else:
                self.assertEqual((view["coverage"]["from"], view["problems"]), ("2026-10-01", []))

    def test_coverage_shows_even_with_no_hours_to_bill(self):
        self.wk("piece", "set", "acme", "reports", "P2", "--bill", "no", "--reason", "x")
        invoice.make(self.c, self.today, pdf=False)  # nothing unbilled left
        self.wk("piece", "set", "acme", "reports", "P2", "--bill", "yes")
        view = invoice.preview(self.c, self.today)
        self.assertEqual(view["coverage"]["not_covered"], ["acme reports P2: Aged debt report"])
        self.assertIn("No unbilled hours", self.refused(self.today))

    def test_no_start_date_blocks_the_invoice(self):
        (self.c.dir / "client.toml").write_text(ACME.replace('coverage_from = "2026-10-01"', ""), encoding="utf-8")
        self.c = invoice.Client(self.c.dir, "acme")
        self.assertIn("needs coverage_from", self.refused("2026-10-23"))

    def test_records_made_before_coverage_stay_valid(self):
        self.wk("piece", "set", "acme", "reports", "P2", "--bill", "no", "--reason", "x")
        invoice.make(self.c, "2026-10-23", "2026-10-23", pdf=False)
        hours, sheet = self.app / ".work" / "hours.tsv", self.c.dir / "ACM-INV-001.csv"
        old_statement(self.c, "ACM-INV-001")
        for f in (hours, sheet):  # drop the new last column, as files made by the older version look
            sep = "\t" if f == hours else ","
            f.write_text("".join(ln.rsplit(sep, 1)[0] + "\n" for ln in f.read_text(encoding="utf-8").splitlines()),
                         encoding="utf-8", newline="\n")
        ledger = self.c.ledger()
        ledger["ACM-INV-001"]["sha256"]["csv"] = invoice.digest(sheet)
        self.c.save_ledger(ledger)
        self.assertTrue(invoice.check(self.c)["ok"], invoice.check(self.c)["problems"])
        self.assertEqual(invoice.invoice_data(self.c, "ACM-INV-001")["hours"], 2.5)


class Preview(Acme):
    def test_the_preview_shows_billed_carried_forward_and_not_billing_tables(self):
        steps = [("add", "Aged debt report"), ("set", "P2", "--state", "merged", "--pr", "4"),
                 ("set", "P2", "--bill", "later", "--reason", "Not handed over yet"),
                 ("add", "Cash forecast"), ("set", "P3", "--state", "building", "--pr", "5"),
                 ("add", "Budget pack"), ("add", "Old export fix"), ("set", "P5", "--state", "done"),
                 ("set", "P5", "--bill", "no", "--reason", "Covered by the fixed fee")]
        for step in steps:
            self.wk("piece", step[0], "acme", "reports", *step[1:])
        today = date.today().isoformat()
        code, out = self.run_invoice("preview", "acme", "--through", today)
        self.assertEqual(code, 0, out)
        for row in (f"# ACM-INV-001 preview: 2026-10-20 to {today}",
                    "| reports | **Built** the monthly reports | Monthly reports | 3 |",
                    "| 2026-10-21 | Call with Sam: scope | 0.5 |",
                    "**Total:** 3.5 hours, 350.00 USD at 100 USD/h",
                    "| reports | Aged debt report | Not handed over yet |",
                    "| reports | Cash forecast | building, PR #5 |",
                    "| reports | Monthly reports | todo; part billed on this invoice |",
                    "| reports | Budget pack |",
                    "| reports | Old export fix | Covered by the fixed fee |"):
            self.assertIn(row, out)
        self.assertNotIn("Fix before make", out)
        code, out = self.run_invoice("preview", "acme", "--through", today, "--json")
        self.assertEqual(json.loads(out)["hours"], 3.5)
        self.assertNotIn("Billed before", self.run_invoice("preview", "acme", "--through", today)[1])  # none yet

    def test_first_invoice_not_billing_and_work_billed_before(self):
        self.wk("piece", "add", "acme", "reports", "Early clean-up")
        self.wk("piece", "set", "acme", "reports", "P2", "--state", "done")
        self.wk("piece", "set", "acme", "reports", "P2", "--bill", "no", "--reason", "Goodwill")
        path = self.app / ".work" / "reports" / "pieces.tsv"  # it finished after coverage_from, before the first hour
        lines = path.read_text(encoding="utf-8").splitlines()
        at = lines[0].split("\t").index("finished")
        rows = [ln.split("\t") for ln in lines[1:]]
        next(r for r in rows if r[0] == "P2")[at] = "2026-10-10"
        path.write_text("\n".join([lines[0]] + ["\t".join(r) for r in rows]) + "\n", encoding="utf-8", newline="\n")
        out = self.run_invoice("preview", "acme", "--through", "2026-10-23")[1]
        self.assertIn("| reports | Early clean-up | Goodwill |", out)
        invoice.make(self.c, "2026-10-23", "2026-10-23", pdf=False)
        out = self.run_invoice("preview", "acme", "--through", "2026-10-30")[1]
        self.assertIn("Billed before, with new hours:", out)
        self.assertIn("| reports | Monthly reports (also on ACM-INV-001) | 1 |", out)
        self.assertNotIn("Early clean-up", out)  # shown on the invoice its period ended in


class CoverageOfDone(Coverage):
    """Work finished without a PR (a finding, an outcome) is billed exactly like merged work: every coverage test again."""
    FINISH = "done"

    def test_still_open_work_is_never_asked_for(self):
        self.wk("piece", "set", "acme", "reports", "P2", "--state", "ready")
        self.assertEqual(invoice.preview(self.c, self.today)["coverage"]["not_covered"], [])

    def test_merged_after_billed_done_work_needs_new_hours(self):
        self.backdate("P2", "finished", "2026-10-20")
        self.wk("hours", "add", "acme", "reports", "1", "--note", "Aged debt", "--date", "2026-10-20", "--for", "P2",
                "--again")
        invoice.make(self.c, "2026-10-23", "2026-10-23", pdf=False)  # the finding, billed
        self.wk("piece", "set", "acme", "reports", "P2", "--state", "merged", "--pr", "1")  # then a fix built and merged today
        self.assertIn("finished after it was last billed", self.refused(self.today))
        self.wk("hours", "add", "acme", "reports", "1", "--note", "Aged debt fix", "--for", "P2")
        self.assertEqual(invoice.preview(self.c, self.today)["coverage"]["not_covered"], [])
        invoice.make(self.c, self.today, pdf=False)

    def test_reopened_and_done_again_after_billing_needs_new_hours(self):
        self.backdate("P2", "finished", "2026-10-20")
        self.wk("hours", "add", "acme", "reports", "1", "--note", "Aged debt", "--date", "2026-10-20", "--for", "P2",
                "--again")
        invoice.make(self.c, "2026-10-23", "2026-10-23", pdf=False)  # billed in full
        self.assertEqual(invoice.preview(self.c, self.today)["coverage"]["not_covered"], [])
        self.wk("piece", "set", "acme", "reports", "P2", "--state", "ready")  # reopened for more work
        self.wk("piece", "set", "acme", "reports", "P2", "--state", "done")  # and done again today
        self.assertIn("finished after it was last billed", self.refused(self.today))
        self.wk("hours", "add", "acme", "reports", "1", "--note", "Aged debt follow-up", "--for", "P2")
        self.assertEqual(invoice.preview(self.c, self.today)["coverage"]["not_covered"], [])
        invoice.make(self.c, self.today, pdf=False)


class LineKeys(Acme):
    """A list bills as several rows: hours that name one of its extra lines (--line-key) get that line's own row."""

    def setUp(self):
        super().setUp()
        self.wk("product", "set", "acme", "reports", "--line", "fixes=Fixed errors in the reports")
        self.wk("product", "set", "acme", "reports", "--line", "refine=Refined the report layouts")
        for h, key, day in (("1.5", "fixes", "2026-10-21"), ("0.5", "refine", "2026-10-22"),
                            ("1", "fixes", "2026-10-23")):
            self.wk("hours", "add", "acme", "reports", h, "--note", f"{key} work", "--date", day, "--for", "P1",
                    "--line-key", key)

    def refused(self, *args):
        run = subprocess.run([sys.executable, "-m", "wk", *args], capture_output=True, text=True, encoding="utf-8",
                             env=os.environ | {"PYTHONPATH": str(KIT)})
        self.assertNotEqual(run.returncode, 0, run.stdout)
        return run.stderr

    def test_two_named_rows_and_the_usual_row_in_one_list(self):
        view = invoice.preview(self.c, "2026-10-23")
        self.assertEqual(view["problems"], [])
        self.assertEqual([(ln["text"], ln["hours"]) for ln in view["sections"][0]["lines"]],
                         [("**Built** the monthly reports", 2.0), ("Fixed errors in the reports", 2.5),
                          ("Refined the report layouts", 0.5)])
        out = self.run_invoice("preview", "acme", "--through", "2026-10-23")[1]
        for row in ("| reports | **Built** the monthly reports | Monthly reports | 2 |",
                    "| reports | Fixed errors in the reports | Monthly reports | 2.5 |",
                    "| reports | Refined the report layouts | Monthly reports | 0.5 |"):
            self.assertIn(row, out)
        invoice.make(self.c, "2026-10-23", "2026-10-23", pdf=False)
        page = invoice.render_html(invoice.invoice_data(self.c, "ACM-INV-001"), PROFILE)
        self.assertIn('Fixed errors in the reports</td><td class="num">2.50', page)
        self.assertIn('Refined the report layouts</td><td class="num">0.50', page)
        self.assertTrue(invoice.check(self.c)["ok"], invoice.check(self.c)["problems"])

    def test_the_statement_with_named_rows_reads_back_exactly(self):
        view = invoice.preview(self.c, "2026-10-23")
        invoice.make(self.c, "2026-10-23", "2026-10-23", pdf=False)
        text = (self.c.dir / "ACM-INV-001.md").read_text(encoding="utf-8")
        self.assertIn("| Repo | List | Ref | Group | Line | Line key |", text)
        self.assertIn("| acme | reports | R1 | Reporting | Fixed errors in the reports | fixes |", text)
        self.assertEqual(invoice.statement(self.c, "ACM-INV-001"),
                         [{k: str(r[k]) for k in invoice.COLUMNS} for r in view["rows"]])
        self.assertEqual(invoice.sections(self.c, invoice.statement(self.c, "ACM-INV-001"), 100), view["sections"])

    def test_a_key_set_on_hours_already_booked_moves_them_to_that_row(self):
        self.wk("hours", "fix", "acme", "reports", "--date", "2026-10-20", "--line-key", "refine")
        lines = invoice.preview(self.c, "2026-10-23")["sections"][0]["lines"]
        self.assertEqual([(ln["text"], ln["hours"]) for ln in lines],
                         [("Fixed errors in the reports", 2.5), ("Refined the report layouts", 2.5)])
        self.wk("hours", "fix", "acme", "reports", "--date", "2026-10-20", "--line-key", "")  # back to the usual line
        self.assertEqual(len(invoice.preview(self.c, "2026-10-23")["sections"][0]["lines"]), 3)
        self.assertIn("has no invoice line 'typo'",
                      self.refused("hours", "fix", "acme", "reports", "--date", "2026-10-20", "--line-key", "typo"))
        self.assertIn("move them first", self.refused("product", "set", "acme", "reports", "--line", "fixes="))
        invoice.make(self.c, "2026-10-23", "2026-10-23", pdf=False)
        self.assertIn("stays as it is",  # a billed entry never moves
                      self.refused("hours", "fix", "acme", "reports", "--date", "2026-10-21", "--line-key", ""))

    def test_nth_picks_which_of_a_days_entries_moves(self):
        self.wk("hours", "add", "acme", "reports", "0.25", "--note", "more fixes", "--date", "2026-10-21", "--for",
                "P1", "--again")
        self.wk("hours", "fix", "acme", "reports", "--date", "2026-10-21", "--nth", "2", "--line-key", "refine")
        keys = {r["note"]: r["key"] for r in invoice.preview(self.c, "2026-10-23")["rows"] if r["date"] == "2026-10-21"
                and r["product"] == "reports"}
        self.assertEqual(keys, {"fixes work": "fixes", "more fixes": "refine"})

    def test_a_line_that_looks_keyed_never_becomes_the_usual_wording(self):
        products = self.app / ".work" / "products.toml"
        self.wk("product", "set", "acme", "reports", "--line", "Fixes = Fixed report errors")  # any case, spaces
        text = products.read_text(encoding="utf-8")
        self.assertIn('line-fixes = "Fixed report errors"', text)
        self.assertIn('line = "**Built** the monthly reports"', text)
        self.assertIn("can't name an invoice line", self.refused("product", "set", "acme", "reports", "--line",
                                                                  "bug_fixes=Fixed bugs"))
        self.assertIn("product add takes the list's usual line", self.refused("product", "add", "acme", "portal",
                                                                             "--line", "fixes=Fixed bugs"))
        self.assertEqual(products.read_text(encoding="utf-8"), text)


# A statement as 1.30 to 1.46 wrote it: no Line key columns.
OLD_MD = """# ACM-INV-001: Acme Ltd hours statement

- **Period:** 2026-10-20 to 2026-10-23
- **Prepared:** 2026-10-23
- **Total hours:** 2.5
- **Rate:** 100 USD/h; **Amount:** 250.00 USD

## Invoice lines

| Ref | Group | Line | Hours |
|---|---|---|---:|
| R1 | Reporting | **Built** the monthly reports | 2 |
| 2026-10-21 | Calls with Acme | Call with Sam: scope | 0.5 |

## Hours

| Date | Repo | List | Work | Hours | By | Pieces |
|---|---|---|---|---:|---|---|
| 2026-10-20 | acme | reports | Aged debt — review | 2 | Pat Lee | P1 |
| 2026-10-21 | acme | calls | Call with Sam: scope | 0.5 | Pat Lee |  |

## Lists

| Repo | List | Ref | Group | Line |
|---|---|---|---|---|
| acme | reports | R1 | Reporting | **Built** the monthly reports |
| acme | calls | calls | Services | Calls |
"""


class OlderStatements(Acme):
    """A list with no named lines invoices exactly as before 1.47, and statements made before it read unchanged."""

    def test_no_keys_writes_the_statement_as_before(self):
        by = invoice.preview(self.c, "2026-10-23")["rows"][0]["by"]  # who booked them depends on where tests run
        invoice.make(self.c, "2026-10-23", "2026-10-23", pdf=False)
        self.assertEqual((self.c.dir / "ACM-INV-001.md").read_text(encoding="utf-8"),
                         OLD_MD.replace("| Pat Lee |", f"| {by} |"))

    def test_a_statement_made_before_named_lines_reads_and_checks_unchanged(self):
        invoice.make(self.c, "2026-10-23", "2026-10-23", pdf=False)
        md = self.c.dir / "ACM-INV-001.md"
        md.write_bytes(OLD_MD.encode("utf-8"))
        ledger = self.c.ledger()
        ledger["ACM-INV-001"]["sha256"] = {"md": invoice.digest(md)}
        self.c.save_ledger(ledger)
        rows = invoice.statement(self.c, "ACM-INV-001")
        self.assertEqual([(r["note"], r["line"], r["key"]) for r in rows],
                         [("Aged debt — review", "**Built** the monthly reports", ""),
                          ("Call with Sam: scope", "Calls", "")])
        self.assertEqual(invoice.invoice_data(self.c, "ACM-INV-001")["amount"], 250.0)
        self.assertTrue(invoice.check(self.c)["ok"], invoice.check(self.c)["problems"])
        self.assertEqual(md.read_bytes(), OLD_MD.encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
