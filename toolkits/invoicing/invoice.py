"""Invoicing: the kit's optional toolkit for billing a client by the hours recorded in its repos.

It reads each client repo's hours through `wk`, freezes them into a statement, records the invoice in the client's
ledger, prints the client PDF, and has `nh hours mark` label those hours with the invoice number so they are never
billed twice. It refuses to invoice while a piece finished since `coverage_from` is neither paid for by hours (`nh hours
add --for`) nor marked bill later or not billing (`nh piece set --bill`), so no finished work is missed. A client's billing record lives in its own repo's `billing/` folder (never in `.work/`, never here):

    billing/client.toml   prefix, repos, rate, terms, bill-to, coverage_from, and per product: group and ref
    billing/ledger.json   every invoice: period, hours, rate, amount, hashes, sent, paid
    billing/<ID>.md       the frozen statement of each invoice: readable, and its tables read back exactly
                          (invoices made before 1.30 also keep billing/<ID>.csv, which stays their source)

Commands: preview (tables for the owner; --json for the data), make, pdf, sent, paid, check. Rules: README.md.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import re
import shutil
import string
import subprocess
import sys
import tempfile
import tomllib
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
KIT = HERE.parents[1]  # this toolkit ships inside nearly-headless
TEMPLATE = HERE / "invoice.html"
PROJECTS = Path("C:/Projects")  # the profile and client PDFs never live under here
# key: the list's named invoice line the hours bill on (nh hours --line-key); "" is its usual line. Older lack the
# last ones.
COLUMNS = ["date", "repo", "product", "hours", "note", "by", "ref", "group", "line", "pieces", "key"]
# The statement's two data tables: the hours, one row each, and each list's ref, group and invoice line, one row per
# named line. A Line key column closes each table only when some hours name a line, so an invoice without any reads
# and looks as it did before 1.47.
HOURS = ("## Hours", ["Date", "Repo", "List", "Work", "Hours", "By", "Pieces", "Line key"],
         ["date", "repo", "product", "note", "hours", "by", "pieces", "key"])
LISTS = ("## Lists", ["Repo", "List", "Ref", "Group", "Line", "Line key"],
         ["repo", "product", "ref", "group", "line", "key"])
# Words the client's accountants should never see in an invoice line.
JARGON = re.compile(r"\b(SDF|SuiteQL|SuiteScript|M/R|Map/Reduce|customscript\w*|customdeploy\w*|"
                    r"customrecord\w*|custbody\w*|custcol\w*)\b|"
                    r"\b[A-Z]{3}-[0-9]{3,}\b|\b[\w-]+\.(js|py|ts|json|xml|md|csv)\b")


class Refused(Exception):
    pass


# ---------------------------------------------------------------- nearly-headless, through its CLI only

def wk(*args: str) -> object:
    """Run the kit's `nh --json ...` and return its JSON. The kit is $NH_HOME, else the one this ships in."""
    kit = os.environ.get("NH_HOME") or str(KIT)
    env = os.environ | {"PYTHONPATH": kit, "PYTHONIOENCODING": "utf-8",
                        "NH_AUTO_UPDATE": "0"}  # nearly-headless must not update itself between counting and marking
    run = subprocess.run([sys.executable, "-P", "-m", "wk", "--json", *args], capture_output=True, text=True,
                         encoding="utf-8", env=env)
    if run.returncode not in (0, 3):
        raise Refused(run.stderr.strip() or f"nh {' '.join(args)} failed")
    if run.returncode == 3:
        print("invoice: hours marked but NOT on GitHub yet; push that repo's main", file=sys.stderr)
    return json.loads(run.stdout)


# ---------------------------------------------------------------- the client's billing record

def billing_dir(repo: str) -> Path:
    """The billing/ folder in that repo's main checkout, found through this laptop's nearly-headless settings."""
    sys.path.insert(0, os.environ.get("NH_HOME") or str(KIT))
    from wk import settings
    from wk.core import KitError
    try:
        return settings.repo(settings.load(settings.path()), repo) / "billing"
    except KitError as exc:
        raise Refused(str(exc)) from exc


class Client:
    def __init__(self, folder: Path, name: str):
        self.name, self.dir = name, Path(folder)
        path = self.dir / "client.toml"
        if not path.is_file():
            raise Refused(f"No billing record for '{name}': {path} is missing")
        self.cfg = tomllib.loads(path.read_text(encoding="utf-8"))

    @property
    def ledger_path(self) -> Path:
        return self.dir / "ledger.json"

    def ledger(self) -> dict:
        return json.loads(self.ledger_path.read_text(encoding="utf-8")) if self.ledger_path.is_file() else {}

    def save_ledger(self, ledger: dict) -> None:
        self.ledger_path.write_text(json.dumps(ledger, indent=1, ensure_ascii=False) + "\n", encoding="utf-8",
                                    newline="\n")

    def product(self, name: str) -> dict:
        return (self.cfg.get("products") or {}).get(name) or {}

    def next_id(self) -> str:
        numbers = [int(k.rsplit("-", 1)[1]) for k in self.ledger()]
        return f"{self.cfg['prefix']}-INV-{max(numbers or [0]) + 1:03d}"


def rows_digest(rows: list[dict]) -> str:
    """The same digest as the kit's work.rows_digest, so `nh hours mark` marks exactly the rows this counted."""
    lines = sorted("\t".join(x[k] for k in ("date", "product", "hours", "note")) for x in rows)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def money(value: float) -> str:
    return f"{value:,.2f}"


# ---------------------------------------------------------------- counting

def unbilled(c: Client, through: str) -> list[dict]:
    """Every repo's unmarked hours through the cut-off, each with its product's line, group and ref frozen in."""
    rows = []
    for repo in c.cfg["repos"]:
        lines = wk("product", "list", repo)
        for x in wk("hours", "list", repo, "--unbilled")[0]["rows"]:
            if x["date"] <= through:
                p, key = c.product(x["product"]), x.get("line_key", "")
                rows.append({"date": x["date"], "repo": repo, "product": x["product"], "hours": x["hours"],
                             "note": x["note"], "by": x["by"], "ref": p.get("ref", x["product"]),
                             "group": p.get("group", "Services"),
                             "line": (lines.get(x["product"]) or {}).get(f"line-{key}" if key else "line", ""),
                             "pieces": x.get("pieces", ""), "key": key})
    return sorted(rows, key=lambda r: (r["date"], r["ref"]))


def problems(c: Client, rows: list[dict]) -> list[str]:
    calls = c.cfg.get("calls_product")
    found = []
    for r in rows:
        if r["product"] == calls:
            continue
        if r["product"] not in (c.cfg.get("products") or {}):
            found.append(f"{r['repo']} {r['product']}: no group or ref in client.toml")
        if r["key"] and not r["line"]:
            found.append(f"{r['repo']} {r['product']}: hours bill on the line '{r['key']}', which the list doesn't "
                         f"have. Add it (nh product set ... --line {r['key']}=\"...\") or move them (nh hours fix "
                         f"... --line-key \"\")")
        bad = JARGON.search(r["line"].replace("**", ""))
        if bad:
            found.append(f"{r['product']}: its line mentions '{bad.group(0)}'; write for the client's accountants")
    return sorted(set(found))


def coverage(c: Client, rows: list[dict], through: str) -> dict:
    """Every piece finished from coverage_from through the cut-off, against the hours that pay for it. A piece finished
    on the day it last became merged or done after being open, or became merged after done (older pieces: the day their row last changed, never before they merged). It is
    covered by unbilled hours, or by billed hours dated, or on an invoice whose period ends, on or after that day; hours billed earlier paid
    for the work up to then, not for the rest of it. The calls list holds no build work, so its pieces are skipped."""
    calls = c.cfg.get("calls_product")
    out = {"from": "", "not_covered": [], "later": [], "billed_before": [], "problems": [], "pieces": []}
    start = str(c.cfg.get("coverage_from") or "")  # an unquoted TOML date reads as a date, not text
    try:
        start = date.fromisoformat(start).isoformat()
    except ValueError:
        out["problems"].append("client.toml needs coverage_from = \"YYYY-MM-DD\": the first day whose finished work "
                               "must be billed" + (f" (got '{start}')" if start else ""))
        return out
    out["from"], hours = start, {}
    # an invoice covers work up to its period's end, whenever it was made
    upto = {inv: (rec.get("period") or ["", ""])[1] for inv, rec in c.ledger().items()}
    for repo in c.cfg["repos"]:
        for x in wk("hours", "list", repo)[0]["rows"]:
            for pid in filter(None, (x.get("pieces") or "").split(",")):
                hours.setdefault((repo, x["product"], pid), []).append((x["date"], x["billed"]))
    for r in rows:
        if r["product"] != calls and r["date"] >= start and not r["pieces"]:
            out["problems"].append(f"{r['repo']} {r['product']} {r['date']} '{r['note']}': name the pieces these "
                                   f"hours pay for (nh hours fix ... --for P3)")
    for repo in c.cfg["repos"]:
        for product in wk("product", "list", repo):
            if product == calls:
                continue
            for x in wk("piece", "list", repo, product, "--all"):
                key, name = (repo, product, x["id"]), f"{repo} {product} {x['id']}: {x['piece']}"
                unbilled_ = [d for d, b in hours.get(key, []) if not b]
                billed = sorted({b for _, b in hours.get(key, []) if b})
                if x.get("bill") == "no" and unbilled_:
                    out["problems"].append(f"{name} is marked not billing but has unbilled hours: move them to the "
                                           f"right piece (nh hours fix ... --for) or bill it (--bill yes)")
                if x.get("bill") == "later" and unbilled_:
                    out["problems"].append(f"{name} is marked bill later but has unbilled hours: switch it to "
                                           f"--bill yes to bill them, or move them (nh hours fix ... --for)")
                if unbilled_ and billed:
                    out["billed_before"].append(f"{name} (also on {', '.join(billed)})")
                day = x.get("finished") or x["updated"]  # pieces merged before nearly-headless 1.21 have no finished
                out["pieces"].append({"repo": repo, "product": product, "id": x["id"], "piece": x["piece"],
                                      "state": x["state"], "pr": x.get("pr") or "", "bill": x.get("bill") or "",
                                      "finished": day if x["state"] in ("merged", "done") else "",
                                      "worked": key in hours, "billed": billed})  # every piece, for the preview
                finished = x["state"] in ("merged", "done") and start <= day <= through  # done: finished without a PR
                covered = unbilled_ or any(b and max(d, upto.get(b, "")) >= day for d, b in hours.get(key, []))
                if not finished or covered or x.get("bill") == "no":
                    continue
                if x.get("bill") == "later":
                    out["later"].append(name)
                else:
                    out["not_covered"].append(name + (" (finished after it was last billed, with no new hours)"
                                                      if billed else ""))
    out["problems"] += [f"{n}: finished but not billed. Book its hours (nh hours add ... --for), or mark it "
                        f"--bill later or --bill no" for n in out["not_covered"]]
    return out


def sections(c: Client, rows: list[dict], rate: float) -> list[dict]:
    """One line per product and named line (hours summed; its usual line first) grouped by value to the client, then
    the calls, one line each. Statements made before 1.47 have no key: one line per product."""
    groups, calls, lines = {}, [], {}
    for r in rows:
        hours = float(r["hours"])
        if r["product"] == c.cfg.get("calls_product"):
            calls.append({"ref": r["date"], "text": r["note"], "hours": hours})
            continue
        key = (r["repo"], r["product"], r.get("key") or "")
        if key not in lines:
            lines[key] = {"ref": r["ref"], "text": r["line"], "hours": 0.0, "key": key[2]}
            groups.setdefault(r["group"], []).append(lines[key])
        lines[key]["hours"] += hours
    out = [{"title": g, "lines": sorted(ls, key=lambda x: (x["ref"], x["key"]))} for g, ls in groups.items()]
    if calls:
        out.append({"title": c.cfg.get("calls_section", "Calls"), "lines": calls})
    for s in out:
        for ln in s["lines"]:
            ln["amount"] = round(ln["hours"] * rate, 2)
        s["hours"] = round(sum(ln["hours"] for ln in s["lines"]), 2)
        s["amount"] = round(sum(ln["amount"] for ln in s["lines"]), 2)
    return out


def period_start(c: Client, rows: list[dict]) -> str:
    ends = [v["period"][1] for v in c.ledger().values()]
    return (date.fromisoformat(max(ends)) + timedelta(days=1)).isoformat() if ends else (rows[0]["date"] if rows else "")


# ---------------------------------------------------------------- commands

def preview(c: Client, through: str) -> dict:
    rows = unbilled(c, through)
    rate = c.cfg["rate"]
    secs = sections(c, rows, rate)
    total = round(sum(float(r["hours"]) for r in rows), 2)
    cover = coverage(c, rows, through)
    return {"id": c.next_id(), "period": [period_start(c, rows), through], "hours": total, "rate": rate,
            "amount": round(sum(s["amount"] for s in secs), 2),  # the sum of the lines the client sees, cent for cent
            "sections": secs, "rows": rows, "problems": ([] if rows else [f"No unbilled hours for {c.name} through {through}"])
            + problems(c, rows) + cover.pop("problems"), "coverage": cover,
            "per_repo": {repo: round(sum(float(r["hours"]) for r in rows if r["repo"] == repo), 2)
                         for repo in c.cfg["repos"]}}


def _md(head: list[str], rows: list[list]) -> list[str]:
    """A Markdown table for the owner to read (a | in the text shown as \\|), or 'None.' when it has no rows."""
    if not rows:
        return ["None."]
    esc = [[str(v).replace("|", "\\|").replace("\n", " ") for v in r] for r in rows]
    return ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"] + \
        ["| " + " | ".join(r) + " |" for r in esc]


def reason(repo: str, product: str, pid: str, said: str) -> str:
    """The newest reason in the piece's note for billing it later or not at all (nh piece set --bill ... --reason)."""
    text = wk("piece", "note", repo, product, pid).get("note") or ""
    found = [ln.split(f"{said}: ", 1)[1].strip() for ln in text.splitlines() if f"{said}: " in ln]
    return found[-1] if found else ""


def report(c: Client, view: dict) -> str:
    """What the owner reads before saying yes to make: Billed, Carried forward and Not billing, as tables built from
    the records (hours and the pieces they name, bill marks, piece states and PRs)."""
    calls, cur = c.cfg.get("calls_product"), c.cfg.get("currency", "USD")

    def area(repo: str, product: str) -> str:
        return f"{repo} {product}" if len(c.cfg["repos"]) > 1 else product
    pieces = {(p["repo"], p["product"], p["id"]): p for p in view["coverage"].get("pieces", [])}
    lines, call_rows, new = {}, [], {}
    for r in view["rows"]:
        if r["product"] == calls:
            call_rows.append([r["date"], r["note"], r["hours"]])
            continue
        ln = lines.setdefault((r["repo"], r["product"], r["key"]), {"line": r["line"], "ids": [], "hours": 0.0})
        ln["hours"] += float(r["hours"])
        for pid in filter(None, r["pieces"].upper().split(",")):
            ln["ids"] += [pid] * (pid not in ln["ids"])
            new[(r["repo"], r["product"], pid)] = new.get((r["repo"], r["product"], pid), 0.0) + float(r["hours"])
    billed = [[area(*k[:2]), ln["line"], "; ".join((pieces.get((*k[:2], i)) or {"piece": i})["piece"]
                                                   for i in ln["ids"]), f"{ln['hours']:g}"]
              for k, ln in sorted(lines.items())]  # one row per invoice line: a list's usual line first
    again = [[area(*k[:2]), f"{p['piece']} (also on {', '.join(p['billed'])})", f"{new[k]:g}"]
             for k, p in sorted(pieces.items()) if p.get("billed") and k in new]
    start, through = view["period"]
    # a first invoice: not billing counts from coverage_from, not from its first hour
    since = start if c.ledger() else (view["coverage"].get("from") or start)
    later, going, todo, nobill = [], [], [], []
    for key, p in sorted(pieces.items()):
        where, name, open_ = area(p["repo"], p["product"]), p["piece"], p["state"] in ("todo", "building", "ready")
        if p["bill"] == "no" and (open_ or since <= p["finished"] <= through):
            nobill.append([where, name, reason(p["repo"], p["product"], p["id"], "Not billing")])
        elif p["bill"] == "later" and p["finished"]:
            later.append([where, name, reason(p["repo"], p["product"], p["id"], "Bill later")])
        elif p["state"] in ("building", "ready") or (p["state"] == "todo" and p["worked"]):
            pr, here = p["pr"], any(key[2] in ln["ids"] for k, ln in lines.items() if k[:2] == key[:2])
            going.append([where, name, p["state"] + (f", PR #{pr}" if pr.isdigit() else f", PR {pr}" if pr else "")
                          + ("; part billed on this invoice" if here else "")])
        elif p["state"] == "todo":
            todo.append([where, name])
    out = [f"# {view['id']} preview: {start} to {through}", "", "## Billed", "",
           *_md(["Area (list)", "Exact line on the invoice", "Pieces it pays for", "Hours"], billed), "",
           "Calls:", "", *_md(["Date", "Call", "Hours"], call_rows), "",
           f"**Total:** {view['hours']:g} hours, {money(view['amount'])} {cur} at {view['rate']:g} {cur}/h", "",
           *(["Billed before, with new hours:", "", *_md(["Area", "Piece", "New hours"], again), ""] if again else []),
           "## Carried forward", "", "Finished, billing later:", "", *_md(["Area", "Piece", "Reason"], later), "",
           "In progress with work in it:", "", *_md(["Area", "Piece", "Where it stands"], going), "",
           "Not started:", "", *_md(["Area", "Piece"], todo), "",
           "## Not billing", "", *_md(["Area", "Piece", "Reason"], nobill)]
    if view["problems"]:
        out += ["", "## Fix before make", "", *[f"- {x}" for x in view["problems"]]]
    return "\n".join(out) + "\n"


def make(c: Client, through: str, day: str = "", pdf: bool = True) -> dict:
    """Freeze the statement, record it in the ledger, then mark the hours in each repo. A refused mark undoes it."""
    view = preview(c, through)
    if view["problems"]:
        raise Refused("Fix these before invoicing:\n  " + "\n  ".join(view["problems"]))
    inv, day = view["id"], day or date.today().isoformat()
    md, text = c.dir / f"{inv}.md", statement_md(c, inv, day, view)
    if read_md(text) != [{k: str(r[k]) for k in COLUMNS} for r in view["rows"]]:
        raise Refused(f"{inv}: the statement would not read back exactly (a note or line with leading or trailing "
                      "spaces?); nothing was made")
    # LF only: the client repo may store LF, and a checkout must not change the file's hash.
    md.write_text(text, encoding="utf-8", newline="\n")
    ledger = c.ledger()
    ledger[inv] = {"date": day, "period": view["period"], "hours": view["hours"], "rate": view["rate"],
                   "currency": c.cfg.get("currency", "USD"), "amount": view["amount"],
                   "sha256": {"md": digest(md)}, "sent": None, "paid": None}
    c.save_ledger(ledger)
    marked = []
    try:
        for repo, hours in view["per_repo"].items():
            if hours:
                wk("hours", "mark", repo, "--through", through, "--as", inv, "--expect", f"{hours:g}",
                   "--digest", rows_digest([r for r in view["rows"] if r["repo"] == repo]))
                marked.append(repo)
    except Refused:
        if not marked:
            md.unlink()
            ledger.pop(inv)
            c.save_ledger(ledger)
            raise
        raise Refused(f"{inv}: hours marked in {', '.join(marked)} but not in the rest. Run check and fix by hand.")
    result = {"invoice": inv, "hours": view["hours"], "amount": view["amount"], "marked": marked}
    commit(c, f"{inv}: {view['hours']:g}h, {money(view['amount'])} {c.cfg.get('currency', 'USD')}")
    if pdf:
        result["pdf"] = print_pdf(c, inv)
    return result


# | and every character splitlines() breaks a line at (a Word soft line break is \x0b), written as &#N;
SPECIAL = re.compile("[|\n\r\x0b\x0c\x1c\x1d\x1e\x85  ]")
REFERENCE = re.compile(r"&(?:(amp|lt|gt)|#(\d+));")


def cell(value) -> str:
    """A table cell that reads back exactly (uncell): &, < and > as &amp; &lt; &gt;, and SPECIAL as &#N;."""
    text = str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return SPECIAL.sub(lambda m: f"&#{ord(m[0])};", text)


def uncell(text: str) -> str:
    """cell() undone, exactly; html.unescape would turn &#11; or &#133; into something else."""
    return REFERENCE.sub(lambda m: {"amp": "&", "lt": "<", "gt": ">"}[m[1]] if m[1] else chr(int(m[2])), text)


def table(head: list[str], rows: list[list], right: tuple = ()) -> list[str]:
    """A Markdown table; columns named in right are right-aligned."""
    return (["| " + " | ".join(head) + " |", "|" + "|".join("---:" if h in right else "---" for h in head) + "|"]
            + ["| " + " | ".join(cell(v) for v in r) + " |" for r in rows])


def statement_md(c: Client, inv: str, day: str, view: dict) -> str:
    """The frozen statement: readable on GitHub, and its Hours and Lists tables are the data the toolkit reads back."""
    cur = c.cfg.get("currency", "USD")
    lists, keyed = {}, any(r["key"] for r in view["rows"])
    cols = (lambda t: t) if keyed else (lambda t: t[:-1])  # no Line key column unless some hours name a line
    for r in view["rows"]:
        lists.setdefault((r["repo"], r["product"], r["key"]), cols([r[k] for k in LISTS[2]]))
    out = [f"# {inv}: {c.cfg['client']['name']} hours statement", "",
           f"- **Period:** {view['period'][0]} to {view['period'][1]}", f"- **Prepared:** {day}",
           f"- **Total hours:** {view['hours']:g}",
           f"- **Rate:** {view['rate']:g} {cur}/h; **Amount:** {money(view['amount'])} {cur}", "",
           "## Invoice lines", "",
           *table(["Ref", "Group", "Line", "Hours"], [[ln["ref"], s["title"], ln["text"], f"{ln['hours']:g}"]
                                                      for s in view["sections"] for ln in s["lines"]], ("Hours",)),
           "", HOURS[0], "", *table(cols(HOURS[1]), [cols([r[k] for k in HOURS[2]]) for r in view["rows"]],
                                    ("Hours",)),
           "", LISTS[0], "", *table(cols(LISTS[1]), list(lists.values()))]
    return "\n".join(out) + "\n"


def _read_table(text: str, heading: str, keys: list[str]) -> list[dict]:
    """The rows of the table under that heading, each cell unescaped back to what was written."""
    lines = text.splitlines()
    if heading not in lines:
        raise Refused(f"the statement has no '{heading}' table")
    out = []
    for ln in lines[lines.index(heading) + 1:]:
        if ln.startswith("#"):
            break
        if ln.startswith("|") and not ln.startswith("|---"):
            out.append(dict(zip(keys, (uncell(v.strip()) for v in ln.strip()[1:-1].split("|")))))
    return out[1:]  # the first row is the header


def read_md(text: str) -> list[dict]:
    """The statement's hours, each with its list's ref, group and line: the rows make() froze, field for field. A
    statement without Line key columns (all before 1.47, and any since whose hours name no line) reads key ''."""
    lists = {(x["repo"], x["product"], x.get("key", "")): x for x in _read_table(text, *LISTS[::2])}
    return [{k: (r | lists.get((r["repo"], r["product"], r.get("key", "")), {})).get(k, "") for k in COLUMNS}
            for r in _read_table(text, *HOURS[::2])]


def statement(c: Client, inv: str) -> list[dict]:
    """The frozen hours of an invoice, from <ID>.md; an invoice made before 1.30 (its ledger entry hashes a csv) is
    read from its <ID>.csv, which is never rewritten."""
    rec = c.ledger().get(inv) or {}
    if "csv" in (rec.get("sha256") or {}):
        with (c.dir / f"{inv}.csv").open(encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))
    return read_md((c.dir / f"{inv}.md").read_text(encoding="utf-8"))


def invoice_data(c: Client, inv: str) -> dict:
    """What the client document shows, from the frozen statement and the ledger only."""
    rec = c.ledger().get(inv)
    if not rec or rec.get("imported"):
        raise Refused(f"{inv} has no statement made by this add-on")
    for kind, sha in rec["sha256"].items():  # never print a PDF that differs from what was made and sent
        path = c.dir / f"{inv}.{kind}"
        if not path.is_file() or digest(path) != sha:
            raise Refused(f"{inv}.{kind} is missing or was changed after the invoice was made, so no PDF is printed. "
                          f"Restore it from git (git checkout -- {path.name} in billing/), then print again")
    secs = sections(c, statement(c, inv), rec["rate"])
    total = round(sum(s["amount"] for s in secs), 2)
    if abs(total - rec["amount"]) > 0.005:
        raise Refused(f"{inv}: lines add up to {total} but the ledger says {rec['amount']}")
    issued, days = date.fromisoformat(rec["date"]), int(c.cfg.get("terms_days", 15))
    return {"id": inv, "client": c.cfg["client"], "rate": rec["rate"], "currency": rec["currency"],
            "date": issued.isoformat(), "due": (issued + timedelta(days=days)).isoformat(), "terms": f"Net {days}",
            "period": rec["period"], "hours": rec["hours"], "amount": rec["amount"], "sections": secs}


def _rich(text: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html.escape(text))


def render_html(data: dict, profile: dict) -> str:
    cur = data["currency"]
    rows = []
    for s in data["sections"]:
        rows.append(f'<tr class="group"><td colspan="5">{html.escape(s["title"])}</td></tr>')
        for ln in s["lines"]:
            rows.append(f'<tr><td class="ref">{html.escape(ln["ref"])}</td><td>{_rich(ln["text"])}</td>'
                        f'<td class="num">{ln["hours"]:.2f}</td><td class="num">{money(data["rate"])}</td>'
                        f'<td class="num">{money(ln["amount"])}</td></tr>')
        label = s["title"][:1].lower() + s["title"][1:]
        rows.append(f'<tr class="subtotal"><td colspan="2">Subtotal: {html.escape(label)}</td>'
                    f'<td class="num">{s["hours"]:.2f}</td><td></td><td class="num">{money(s["amount"])}</td></tr>')
    client = data["client"]
    bill_to = [client.get("name", ""), client.get("attn", ""), *client.get("address", []), client.get("email", "")]
    address = profile["address"] if isinstance(profile["address"], list) else [profile["address"]]
    notes = [n.replace("<ID>", data["id"]) for n in client.get("notes", [])]
    values = {
        "title": html.escape(f"Invoice {data['id']}"),
        "name": html.escape(profile["name"]), "business": html.escape(profile["business"]),
        "from_lines": "<br>".join(html.escape(x) for x in [*address, profile["email"], profile.get("phone", "")] if x),
        "bill_to": "<br>".join(html.escape(x) for x in bill_to if x),
        "invoice_id": html.escape(data["id"]), "invoice_date": data["date"],
        "period": f"{data['period'][0]} to {data['period'][1]}",
        "terms": html.escape(data["terms"]), "due": data["due"], "currency": html.escape(cur),
        "rows": "\n".join(rows), "total_hours": f"{data['hours']:.2f}",
        "total": ("$" if cur == "USD" else "") + money(data["amount"]),
        "notes": "\n".join("<p>" + re.sub(r"^(\w+:)", r"<strong>\1</strong>", html.escape(n)) + "</p>" for n in notes),
    }
    return string.Template(TEMPLATE.read_text(encoding="utf-8")).substitute(values)


def outside_projects(path: Path, what: str) -> Path:
    path = Path(path).expanduser().resolve()
    if path.is_relative_to(PROJECTS.resolve()):
        raise Refused(f"The {what} must live outside {PROJECTS.as_posix()} (got {path.as_posix()})")
    return path


def profile() -> dict:
    path = outside_projects(Path(os.environ.get("INVOICING_PROFILE") or Path.home() / ".wk" / "billing-profile.json"),
                            "billing profile")
    if not path.is_file():
        raise Refused(f"No billing profile at {path.as_posix()}")
    data = json.loads(path.read_text(encoding="utf-8"))
    missing = [k for k in ("name", "business", "address", "email") if not data.get(k)]
    if missing:
        raise Refused(f"Billing profile is missing: {', '.join(missing)}")
    return data


def print_pdf(c: Client, inv: str) -> str:
    data, prof = invoice_data(c, inv), profile()
    out = outside_projects(Path(os.environ.get("INVOICING_OUT") or Path.home() / "Documents" / "Invoices"),
                           "invoice folder")
    out.mkdir(parents=True, exist_ok=True)
    short = data["client"].get("short_name") or data["client"].get("name", "Client")
    target = out / re.sub(r'[<>:"/\\|?*]', "", f"Invoice {inv} - {prof['name']} - {short}.pdf")
    found = [b for b in [os.environ.get("INVOICING_BROWSER", ""),
                         r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                         r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                         r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                         shutil.which("chrome") or "", shutil.which("msedge") or ""] if b and Path(b).is_file()]
    with tempfile.TemporaryDirectory(prefix="invoice-") as tmp:
        page, printed = Path(tmp) / "invoice.html", Path(tmp) / "invoice.pdf"
        page.write_text(render_html(data, prof), encoding="utf-8")
        for browser in found:
            try:
                subprocess.run([browser, "--headless=new", "--disable-gpu", "--no-first-run",
                                "--no-pdf-header-footer", f"--user-data-dir={Path(tmp) / 'browser'}",
                                f"--print-to-pdf={printed}", page.as_uri()], capture_output=True, timeout=90)
            except (OSError, subprocess.SubprocessError):
                continue
            if printed.is_file() and printed.stat().st_size > 1000:
                shutil.move(str(printed), str(target))
                return target.as_posix()
    raise Refused("No Chrome or Edge could print the PDF (set INVOICING_BROWSER)")


def mark(c: Client, inv: str, field: str, day: str = "") -> dict:
    ledger = c.ledger()
    rec = ledger.get(inv)
    if not rec:
        raise Refused(f"No invoice {inv} for {c.name}")
    if rec.get(field):
        raise Refused(f"{inv} is already marked {field} on {rec[field]}")
    if field == "paid" and not rec.get("sent"):
        raise Refused(f"{inv} is not marked sent yet")
    rec[field] = day or date.today().isoformat()
    c.save_ledger(ledger)
    commit(c, f"{inv}: {field} {rec[field]}")
    return {"invoice": inv, field: rec[field]}


def check(c: Client) -> dict:
    """Every statement equals the hours its repos marked with its number, its hash is unchanged, amount = hours ×
    rate, numbers run without gaps, and nothing is paid before it is sent."""
    ledger, found = c.ledger(), []
    marked = {}
    for repo in c.cfg["repos"]:
        for x in wk("hours", "list", repo)[0]["rows"]:
            if x["billed"]:
                marked.setdefault(x["billed"], []).append((repo, x["date"], x["product"], x["hours"], x["note"]))
    numbers = sorted(int(k.rsplit("-", 1)[1]) for k in ledger)
    if numbers != list(range(1, len(numbers) + 1)):
        found.append(f"invoice numbers have gaps: {numbers}")
    for inv, rec in sorted(ledger.items()):
        if rec.get("paid") and not rec.get("sent"):
            found.append(f"{inv}: paid but not sent")
        if rec.get("imported"):
            continue
        missing = [k for k in rec["sha256"] if not (c.dir / f"{inv}.{k}").is_file()]
        if missing:
            found += [f"{inv}.{k} is missing" for k in missing]
            marked.pop(inv, None)
            continue
        lines = round(sum(s["amount"] for s in sections(c, statement(c, inv), rec["rate"])), 2)
        if abs(lines - rec["amount"]) > 0.005:
            found.append(f"{inv}: amount is not the sum of its lines")
        for kind, sha in rec["sha256"].items():  # md, and csv for invoices made before 1.30
            if digest(c.dir / f"{inv}.{kind}") != sha:
                found.append(f"{inv}.{kind} changed after it was made")
        frozen = sorted((r["repo"], r["date"], r["product"], r["hours"], r["note"]) for r in statement(c, inv))
        if frozen != sorted(marked.pop(inv, [])):
            found.append(f"{inv}: statement and the hours marked {inv} in the repos differ")
    found += [f"hours marked {label} in a repo but no such invoice" for label in marked]
    billed = sum(r["amount"] for r in ledger.values())
    unpaid = sum(r["amount"] for r in ledger.values() if not r.get("paid"))
    return {"ok": not found, "problems": found, "billed": round(billed, 2), "outstanding": round(unpaid, 2),
            "invoices": {k: {f: v.get(f) for f in ("period", "hours", "amount", "sent", "paid")}
                         for k, v in sorted(ledger.items())}}


def commit(c: Client, message: str) -> None:
    """Commit this client's folder if the data lives in a git repo. Pushing is the owner's call."""
    top = subprocess.run(["git", "-C", str(c.dir), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if top.returncode == 0:
        subprocess.run(["git", "-C", str(c.dir), "add", "--", "."], capture_output=True)
        subprocess.run(["git", "-C", str(c.dir), "commit", "-q", "-m", f"{c.name} {message}", "--", "."],
                       capture_output=True)


# ---------------------------------------------------------------- command line

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="invoice", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--billing", type=Path, help="the client's billing folder (default: billing/ in the repo)")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("preview", "make"):
        s = sub.add_parser(name)
        s.add_argument("client", metavar="repo", help="the client repo holding billing/")
        s.add_argument("--through", required=True, help="the last day billed (YYYY-MM-DD), usually the Friday")
        if name == "make":
            s.add_argument("--date", default="", help="invoice date (default: today)")
            s.add_argument("--no-pdf", action="store_true")
        else:
            s.add_argument("--json", action="store_true", help="the data instead of the tables")
    for name in ("pdf", "sent", "paid"):
        s = sub.add_parser(name)
        s.add_argument("client", metavar="repo", help="the client repo holding billing/")
        s.add_argument("invoice")
        if name != "pdf":
            s.add_argument("--date", default="")
    sub.add_parser("check").add_argument("client", metavar="repo", help="the client repo holding billing/")
    a = p.parse_args(argv)
    try:
        c = Client(a.billing or billing_dir(a.client), a.client)
        result = (preview(c, a.through) if a.cmd == "preview" else make(c, a.through, a.date, not a.no_pdf)
                  if a.cmd == "make" else {"pdf": print_pdf(c, a.invoice.upper())} if a.cmd == "pdf"
                  else mark(c, a.invoice.upper(), a.cmd, a.date) if a.cmd in ("sent", "paid") else check(c))
    except Refused as exc:
        print(f"invoice: {exc}", file=sys.stderr)
        return 1
    if a.cmd == "preview" and not a.json:
        print(report(c, result), end="")
        return 0
    if a.cmd == "preview":
        result = {k: v for k, v in result.items() if k != "rows"}
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
