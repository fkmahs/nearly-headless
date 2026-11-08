"""The work state each execution repo carries in its own `.work/` folder, written only through wk and committed and
pushed on the repo's base branch, so a clone on any laptop brings it along.

    .work/products.toml            the repo's products, each with its invoice line and supervisor's chat
    .work/hours.tsv                hours the owner stated, per product, naming the pieces they pay for
    .work/<product>/pieces.tsv     one row per piece of work, with its PR and builder's chat
    .work/<product>/notes/P3.md    a piece's memory: brief, the owner's decisions, findings, where it stands
    .work/<product>/questions.md   what waits on the owner; removed once answered
    .work/<product>/orders.md      the owner's standing orders, one numbered line each
    .work/<product>/inbox.md       messages routed to the list, a link and one line each; removed once handled
    .work/communication_channels.md  where a client's work is talked about, for the scheduled routing run

Nothing here holds money or rates, and `.work/` is removed before a repo is handed over.
There is no status file: `status` is computed every time.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import time
import tomllib
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from . import core, settings
from .core import KitError
from .turns import Refused  # exit 1: an expected no
from .turns import ci_green, rows as turn_rows

NL, TAB = chr(10), chr(9)

DIR = ".work"
LOCK = "wk-work.lock"
# Older files lack the last columns. finished: the day the piece last became merged or done after being open, or
# became merged after done. rank: the supervisor's recommended order (1 first), recorded at each catch-up.
# queue: its place in the queue the owner approved (1 first; to-do pieces only), so the supervisor starts it when a
# builder is free without asking again; rank can't be it, since the supervisor changes rank on its own at every
# catch-up. files: what the piece will touch (paths, folders, globs, or none), so a queued piece never starts on
# files an unmerged PR or a turn holds. after: the pieces whose merge it needs first.
PIECE_COLUMNS = ["id", "piece", "state", "branch", "pr", "approved", "updated", "chat", "bill", "finished", "rank",
                 "queue", "files", "after"]
HOUR_COLUMNS = ["date", "product", "hours", "note", "by", "billed", "pieces", "line_key"]  # older lack the last two
# A list's extra invoice lines: `line-<key> = "..."` in products.toml, beside its usual `line`. Hours whose line_key
# names one bill on that row of the invoice; hours without one bill on the usual line.
KEYED_LINE = re.compile(r"\s*([^\s=]+)\s*=\s*(.*)", re.S)  # key=text, any case, spaces around = allowed
KEY = re.compile(r"[a-z0-9][a-z0-9-]*")


def keyed_line(line: str | None):
    """(key, text) when --line names an extra line (fixes="Bug fixes", Fixes = Bug fixes), else None. A word=... that
    is no valid key is refused, so it never becomes the list's usual wording on an invoice."""
    m = KEYED_LINE.fullmatch(line or "")
    if not m:
        return None
    if not KEY.fullmatch(m[1].lower()):
        raise KitError(f"'{m[1]}' can't name an invoice line: use lowercase letters, digits and dashes, e.g. "
                           f"--line fixes=\"Bug fixes\". The list's usual line has no '=' after its first word.")
    return m[1].lower(), m[2]
BILL = {"yes": "", "later": "later", "no": "no"}  # a piece's billing choice; empty means bill it with its hours
STATES = ["todo", "building", "ready", "merged", "done", "dropped"]
OPEN = {"todo", "building", "ready"}
FINISHED = {"merged", "done"}  # merged: ended in a PR; done: finished without one (a finding, an outcome)


class Repo:
    """One execution repo's `.work/` folder, reached through this laptop's settings. `top` is the code checkout (PRs,
    CI, turns, gates); `list_top` holds `.work/`: the same checkout, unless the repo's settings name another (`list`)."""

    def __init__(self, s: dict, name: str):
        self.name, self.top = name, settings.repo(s, name)
        self.base = settings.base_of(s, self.top)
        self.list_top = settings.list_of(s, name)
        self.apart = not core.same_path(self.list_top, self.top)
        self.list_base = (s["repos"][name].get("list_base") or "main") if self.apart else self.base
        self.dir = self.list_top / DIR

    def products(self) -> dict:
        path = self.dir / "products.toml"
        if not path.is_file():
            return {}
        try:
            return tomllib.loads(path.read_text(encoding="utf-8")).get("products", {})
        except tomllib.TOMLDecodeError as exc:
            raise KitError(f"Cannot read {path}: {exc}") from exc

    def product(self, name: str) -> Path:
        """A declared product's folder; an unknown one is an error listing the known ones, so typos never split work."""
        known = self.products()
        if name not in known:
            raise KitError(f"Unknown product '{name}' in {self.name}. Known: {', '.join(known) or 'none'}. "
                               f"Declare it: nh product add {self.name} {name} --line \"...\"")
        return self.dir / name

    def write(self, what: str, edit) -> dict:
        """Under the repo's lock: edit() writes files and returns (paths, message, result); then commit and push."""
        with core.locked(self.list_top, what, LOCK):
            paths, message, result = edit()
            result["push"] = core.save(self.list_top, self.list_base, paths, f"List: {message}")
        return result


# How a list save reads in git: "List: Audit fixes ready for review", not field=value pairs.
SAID = {"todo": "back to do", "building": "being built", "ready": "ready for review", "merged": "merged",
        "done": "done", "dropped": "dropped"}


def said(row: dict, changes: dict) -> str:
    """A piece change in plain words, for its list-save commit."""
    name = row.get("piece") or row.get("id", "")
    if "state" in changes:
        return f"{name} {SAID.get(changes['state'], changes['state'])}"
    if "piece" in changes:
        return f"piece renamed to {name}"
    return f"{name}: {', '.join(k for k in changes)} updated"


def _clean(value) -> str:
    return " ".join(str(value or "").split())


def _next_id(prefix: str, ids: list[str]) -> str:
    return f"{prefix}{max([int(i[1:]) for i in ids if i[1:].isdigit()] or [0]) + 1}"


def _read_tsv(path: Path, columns: list[str]) -> list[dict]:
    """Rows by the file's own header, so columns a newer nearly-headless added survive this version rewriting the file."""
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    head = lines[0].split("\t") if lines else []
    head = head if head[:1] == columns[:1] else columns  # no header line: read it as this version's columns
    return [{c: "" for c in columns} | dict(zip(head, line.split("\t") + [""] * len(head)))
            for line in lines[1:] if line.strip()]


def _write_tsv(path: Path, columns: list[str], rows: list[dict]) -> None:
    """This version's columns, then any a newer nearly-headless added, kept as they were."""
    columns = columns + [k for k in dict.fromkeys(k for r in rows for k in r) if k not in columns]
    core.atomic_text(path, "\n".join(["\t".join(columns)] + ["\t".join(r.get(c, "") for c in columns)
                                                              for r in rows]) + "\n")


# ---------------------------------------------------------------- products

def add_product(r: Repo, name: str, line: str) -> dict:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name or ""):
        raise KitError("Name a product in lowercase letters, digits and dashes, e.g. monthly-reports")
    line = _clean(line)
    if not line:
        raise KitError("Give the product's invoice line (--line): what it is, in the client's words")
    if KEYED_LINE.fullmatch(line):
        raise KitError("product add takes the list's usual line, without key=. Add the list with its usual "
                           f"line, then its named lines: nh product set {r.name} {name} --line fixes=\"...\"")

    def edit():
        products = r.products()
        products[name] = products.get(name, {}) | {"line": line}
        return [_write_products(r, products)], f"product {name}: {line}", {"product": name, "line": line}
    return r.write("product add", edit)


def set_product(r: Repo, name: str, supervisor: str | None = None, title: str | None = None,
                line: str | None = None) -> dict:
    """Record a product's supervisor chat (a link that opens it) and the name the command center shows for it.
    An empty value clears it."""
    r.product(name)
    keyed = keyed_line(line)  # fixes="Bug fixes": a named extra line; fixes= removes it
    field = f"line-{keyed[0]}" if keyed else "line"
    changes = {k: _clean(v) for k, v in (("supervisor", supervisor), ("title", title),
                                         (field, keyed[1] if keyed else line)) if v is not None}
    if not changes:
        raise KitError("Nothing to change: give --supervisor, --title or --line")
    if "line" in changes and not changes["line"]:
        raise KitError("A list's invoice line can't be empty")
    if keyed and not changes[field]:
        held = [x for x in _read_tsv(r.dir / "hours.tsv", HOUR_COLUMNS)
                if not x["billed"] and x["product"] == name and x["line_key"] == keyed[0]]
        if held:
            raise KitError(f"{len(held)} unbilled hours entr{'y' if len(held) == 1 else 'ies'} bill on the line "
                               f"'{keyed[0]}'; move them first (nh hours fix ... --line-key \"\")")

    def edit():
        products = r.products()
        fields = {k: v for k, v in products[name].items() if k not in changes}
        products[name] = fields | {k: v for k, v in changes.items() if v}
        return ([_write_products(r, products)], f"product {name} " + ", ".join(f"{k}: {v or 'none'}" for k, v in changes.items()),
                {"product": name} | changes)
    return r.write("product set", edit)


def _write_products(r: Repo, products: dict) -> Path:
    path = r.dir / "products.toml"
    core.atomic_text(path, "".join(
        f"[products.{p}]\n" + "".join(f"{k} = {core.toml_str(v)}\n" for k, v in fields.items())
        + "\n" for p, fields in sorted(products.items())))
    return path


# ---------------------------------------------------------------- pieces

def _write_pieces(path: Path, rows: list[dict]) -> None:
    """Every write drops the rank of a piece that isn't open (even one closed by an older nearly-headless), so a reopened
    piece never jumps the queue, and the queue place of a piece that is no longer to do: it has started."""
    for x in rows:
        if x["state"] not in OPEN:
            x["rank"] = ""
        if x["state"] != "todo":
            x["queue"] = ""
    _write_tsv(path, PIECE_COLUMNS, rows)


def pieces(r: Repo, product: str) -> list[dict]:
    return _read_tsv(r.product(product) / "pieces.tsv", PIECE_COLUMNS)


def _plain_name(piece: str) -> None:
    """Refuse a name nh check would flag (check.name_problems: the one rule, so the two can't drift)."""
    from . import check  # check reads work's records, so it is imported when used
    why = check.name_problems(piece)
    if why:
        raise KitError(f"A piece's name is plain words, {check.WORDS} at most, with no ticket number or file name; "
                           f"this one has {' and '.join(why)}. Put the detail in its note")


def add_piece(r: Repo, product: str, piece: str) -> dict:
    piece = _clean(piece)
    if not piece:
        raise KitError("Name the piece in a few words")
    _plain_name(piece)
    path = r.product(product) / "pieces.tsv"

    def edit():
        rows = _read_tsv(path, PIECE_COLUMNS)
        row = {c: "" for c in PIECE_COLUMNS} | {"id": _next_id("P", [x["id"] for x in rows]), "piece": piece,
                                                "state": "todo", "updated": date.today().isoformat()}
        _write_pieces(path, rows + [row])
        return [path], f"new piece: {piece}", {"added": row}
    return r.write("piece add", edit)


def set_piece(r: Repo, product: str, pid: str, **changes) -> dict:
    changes = {k: _clean(v) for k, v in changes.items() if v is not None}
    if "bill" in changes:
        if changes["bill"] not in BILL:
            raise KitError(f"--bill must be one of: {', '.join(BILL)}")
        changes["bill"] = BILL[changes["bill"]]
        named = [x for x in _read_tsv(r.dir / "hours.tsv", HOUR_COLUMNS) if not x["billed"]
                 and x["product"] == product and pid.upper() in x["pieces"].split(",")]
        if changes["bill"] in ("no", "later") and named:
            raise KitError(f"{pid.upper()} has unbilled hours ({', '.join(x['date'] for x in named)}). Move them to "
                               f"the right piece with nh hours fix ... --for, or remove them, before marking it "
                               f"{'not billing' if changes['bill'] == 'no' else 'bill later'}")
    if "piece" in changes and not changes["piece"]:
        raise KitError("A piece's name can't be empty")
    if "piece" in changes:
        _plain_name(changes["piece"])
    if "files" in changes:
        changes["files"] = _paths(changes["files"])
    if "after" in changes:
        changes["after"] = ",".join(dict.fromkeys(_ids(changes["after"])))
    if changes.get("state") and changes["state"] not in STATES:
        raise KitError(f"State must be one of: {', '.join(STATES)}")
    if not changes:
        raise KitError("Nothing to change")
    path = r.product(product) / "pieces.tsv"

    def edit():
        rows = _read_tsv(path, PIECE_COLUMNS)
        row = next((x for x in rows if x["id"].lower() == pid.lower()), None)
        if not row:
            raise KitError(f"No piece {pid} in {r.name} {product}")
        if wrong := [a for a in _ids(changes.get("after", "")) if a == row["id"].upper()
                     or a not in {x["id"].upper() for x in rows}]:
            raise KitError(f"--after names pieces of this list other than {row['id']}; not one: {', '.join(wrong)}")
        new = changes.get("state")
        if new == "merged" and not changes.get("pr", row["pr"]):
            raise KitError(f"{row['id']} has no PR, so it can't be merged. Work finished without a PR (a finding, an "
                               f"outcome) is done: nh piece set {r.name} {product} {row['id']} --state done")
        if new in FINISHED and (row["state"] not in FINISHED or (row["state"], new) == ("done", "merged")):
            # finished now, or again after being reopened; done then merged is new work (a fix built after the
            # finding), so it finishes again too. merged then done keeps its day.
            row["finished"] = date.today().isoformat()
        elif row["state"] in FINISHED and not row["finished"]:
            row["finished"] = row["updated"]  # finished before nearly-headless kept this date: its last change is when it finished
        row.update(changes, updated=date.today().isoformat())
        _write_pieces(path, rows)
        return [path], said(row, changes), {"updated": row}
    return r.write("piece set", edit)


def rank_pieces(r: Repo, product: str, ids: str) -> dict:
    """The supervisor's recommended order of its open pieces, first to last (e.g. "P3,P1,P7"), in one write. Open
    pieces left out lose their rank and fall back to the command center's default order. Leaves `updated` alone:
    reordering is not progress."""
    order = [i.upper() for i in re.split(r"[,\s]+", ids or "") if i]
    if not order:
        raise KitError("Give the open pieces in the order you recommend, e.g. P3,P1,P7")
    if len(set(order)) != len(order):
        raise KitError("Name each piece once")
    path = r.product(product) / "pieces.tsv"

    def edit():
        rows = _read_tsv(path, PIECE_COLUMNS)
        wrong = [i for i in order if i not in {x["id"].upper() for x in rows if x["state"] in OPEN}]
        if wrong:
            raise KitError(f"Not an open piece in {r.name} {product}: {', '.join(wrong)}")
        for x in rows:
            x["rank"] = str(order.index(x["id"].upper()) + 1) if x["id"].upper() in order else ""
        _write_pieces(path, rows)
        return [path], f"{product} pieces reordered", {"order": order}
    return r.write("piece rank", edit)


# ---------------------------------------------------------------- piece notes

def _piece(r: Repo, product: str, pid: str) -> dict:
    row = next((x for x in pieces(r, product) if x["id"].lower() == pid.lower()), None)
    if not row:
        raise KitError(f"No piece {pid} in {r.name} {product}")
    return row


def note_path(r: Repo, product: str, pid: str) -> Path:
    return r.product(product) / "notes" / f"{pid.upper()}.md"


def read_note(r: Repo, product: str, pid: str) -> str:
    _piece(r, product, pid)
    path = note_path(r, product, pid)
    return path.read_text(encoding="utf-8") if path.is_file() else ""


# A date typed at the start of a line ("2026-10-30:", "**2026-10-30:**", "2026-10-30 -"): wk dates every line itself.
TYPED_DATE = re.compile(r"^\*{0,2}(\d{4}-\d{2}-\d{2})\*{0,2}\s*[:\u2014\u2013-]?\s*\*{0,2}\s*")
DATED = re.compile(r"^- (\d{4}-\d{2}-\d{2}): ")  # a dated note line, the one form wk reads


def strip_date(line: str, day: str = "") -> str:
    """The line without a date typed at its start, so a note never carries two. A typed date other than the line's
    own (`day`, default today) says when something happened: it stays, in brackets."""
    m = TYPED_DATE.match(line)
    rest = line[m.end():] if m else ""
    if not rest.strip():
        return line
    return rest if m[1] == (day or date.today().isoformat()) else f"({m[1]}) {rest}"


def _noted(path: Path, row: dict, add: str = "", text: str | None = None) -> None:
    body = text if text is not None else (path.read_text(encoding="utf-8") if path.is_file()
                                          else f"# {row['id']} · {row['piece']}\n\n")
    if add:
        body = body.rstrip("\n") + f"\n- {date.today().isoformat()}: {add}\n"
    core.atomic_text(path, body if body.endswith("\n") else body + "\n")


def billed(r: Repo, product: str, pid: str) -> str:
    """The invoice a finished piece's hours went on ("INV-007"), or "" when it isn't finished or none of the hours
    naming it are billed yet."""
    row = _piece(r, product, pid)
    if row["state"] not in FINISHED:
        return ""
    return next((x["billed"] for x in _read_tsv(r.dir / "hours.tsv", HOUR_COLUMNS) if x["billed"]
                 and x["product"] == product and row["id"] in x["pieces"].split(",")), "")


NOTE_LINE = "a note line is one short line: the detail belongs in the PR or a finding, with the line pointing to it"


def note_line(add: str) -> str:
    """The line `note(add=...)` would write, refused before anything is written when it is too long, so a command that
    writes something else first (nh piece set --bill) can check its note line up front."""
    from . import check  # the limit nh check reports (check.long_lines), so the two can't drift
    add = strip_date(_clean(add))
    if add and check.long_lines(f"- {date.today().isoformat()}: {add}"):
        raise KitError(f"That line is over {check.LINE_CHARS} characters, and {NOTE_LINE}")
    return add


def note(r: Repo, product: str, pid: str, add: str = "", text: str | None = None, new_work: bool = False) -> dict:
    """A piece's memory: its brief, the owner's decisions, findings and where it stands. `text` replaces the whole note;
    `add` appends one line dated today (a date typed at its start is dropped). It lives in the repo, so it survives a
    chat losing its memory and a new laptop. `new_work` (the `nh piece note --add` a supervisor writes) is refused on
    a finished piece that is already billed: that work is a new piece, so its hours reach the next invoice."""
    from . import check  # the limit nh check reports (check.long_lines), so the two can't drift
    row = _piece(r, product, pid)
    if new_work and add and (invoice := billed(r, product, pid)):
        raise KitError(f"{row['id']} is {row['state']} and billed ({invoice}). New work on it is a new piece: "
                           f"nh piece add, with a note that points to {row['id']}. A release or teardown note "
                           f"(no new work) goes through with --billed-ok")
    add = note_line(add)
    if text is None and not add:
        raise KitError("Give the note's text (--file) or a line to add (--add)")
    if text is not None and (n := check.long_lines(text)):
        raise KitError(f"{n} line(s) of that note are over {check.LINE_CHARS} characters, and {NOTE_LINE}")
    path = note_path(r, product, row["id"])

    def edit():
        _noted(path, row, add, text)
        return [path], f"{row['piece'] or row['id']}: " + ("note added" if add else "note rewritten"), \
            {"note": core.posix(path), "piece": row["id"]}
    return r.write("piece note", edit)


def latest(note_text: str) -> str:
    """The note's newest dated line, for the command center."""
    lines = [x[2:] for x in note_text.splitlines() if DATED.match(x)]
    return lines[-1] if lines else ""


# ---------------------------------------------------------------- questions

FIELDS = ["Options", "Recommended", "PR", "Piece", "Scope", "Asked"]
LIST_WIDE = "whole list"  # a question about the whole list, not one piece
HEAD = re.compile(r"^## (Q\d+) · (.+)$")


def questions(r: Repo, product: str) -> list[dict]:
    return _read_questions(r.product(product) / "questions.md")


def _read_questions(path: Path) -> list[dict]:
    items = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            m = HEAD.match(line)
            if m:
                items.append({"id": m[1], "question": m[2]})
            elif items and line.startswith("- ") and ": " in line:
                key, value = line[2:].split(": ", 1)
                items[-1][key.lower()] = value
    return items


def _write_questions(path: Path, items: list[dict]) -> None:
    out = ["# Questions for the owner", ""]
    for q in items:
        out.append(f"## {q['id']} · {q['question']}")
        out += [f"- {f}: {q[f.lower()]}" for f in FIELDS if q.get(f.lower())]
        out.append("")
    core.atomic_text(path, "\n".join(out))


def ask(r: Repo, product: str, question: str, options: str = "", recommended: str = "", pr: str = "",
        piece: str = "", list_wide: bool = False) -> dict:
    """A decision waiting on the owner. Every question belongs to a piece (`piece`: answering it then writes the
    answer into that piece's note), unless `list_wide` says it is about the whole list."""
    question = _clean(question)
    if not question:
        raise KitError("Ask a question")
    if bool(_clean(piece)) == bool(list_wide):
        raise KitError("A question is about one piece or the whole list, not both" if list_wide else
                           "Say which piece the question is about (--piece P3), or --list-wide when it is about the "
                           "whole list")
    path = r.product(product) / "questions.md"
    piece = _piece(r, product, piece)["id"] if _clean(piece) else ""

    def edit():
        items = _read_questions(path)
        q = {"id": _next_id("Q", [i["id"] for i in items]), "question": question, "options": _clean(options),
             "recommended": _clean(recommended), "pr": _clean(pr), "piece": piece, "scope": LIST_WIDE if list_wide else "", "asked": date.today().isoformat()}
        _write_questions(path, items + [q])
        return [path], f"{product} {q['id']} asked: {question}", {"asked": q}
    return r.write("question add", edit)


def link_question(r: Repo, product: str, qid: str, piece: str = "", list_wide: bool = False) -> dict:
    """Tie an open question to the piece it is about (it then shows as that piece's "waiting on you"), or mark it as
    about the whole list."""
    path = r.product(product) / "questions.md"
    if bool(_clean(piece)) == bool(list_wide):
        raise KitError("A question is about one piece or the whole list, not both" if list_wide else
                           "Say which piece the question is about (--piece P3), or --list-wide")
    pid = _piece(r, product, piece)["id"] if piece else ""

    def edit():
        items = _read_questions(path)
        q = next((i for i in items if i["id"].lower() == qid.lower()), None)
        if not q:
            raise KitError(f"No open question {qid} in {r.name} {product}")
        q["piece"], q["scope"] = pid, "" if pid else LIST_WIDE
        _write_questions(path, items)
        return [path], f"{product} {q['id']} is about {pid or 'the whole list'}", {"question": q}
    return r.write("question set", edit)


def answer(r: Repo, product: str, qid: str, reply: str) -> dict:
    """Remove an answered question. When it names a piece, the answer goes into that piece's note in the same commit
    ("Owner answered Q2 (<question>): <answer>"); the supervisor also says it on the PR, where it acts on it."""
    reply = _clean(reply)
    if not reply:
        raise KitError("Give the owner's answer")
    return _close(r, product, qid, "answered", "Owner answered", reply)


def withdraw(r: Repo, product: str, qid: str, reason: str, piece: str = "") -> dict:
    """Remove a question the supervisor can decide itself after all (a reversible step inside the piece's agreed
    scope). Its piece's note says so, as the supervisor's call, never as the owner's answer: "Supervisor withdrew Q2
    (<question>), deciding it: <reason>". A whole-list question names the piece whose note keeps that line (`piece`),
    so the decision lives where the supervisor rereads it, not only in a commit message."""
    reason = _clean(reason)
    if not reason:
        raise KitError("Say what you decided and why it can be undone (--reason)")
    return _close(r, product, qid, "withdrawn", "Supervisor withdrew", reason, _clean(piece))


def _close(r: Repo, product: str, qid: str, done: str, said: str, text: str, piece: str = "") -> dict:
    reply = text
    path = r.product(product) / "questions.md"

    def edit():
        items = _read_questions(path)
        q = next((i for i in items if i["id"].lower() == qid.lower()), None)
        if not q:
            raise KitError(f"No open question {qid} in {r.name} {product}")
        on = q.get("piece") or piece
        if done == "withdrawn" and not on:
            raise KitError(f"{q['id']} is about the whole list: name the piece whose note keeps your decision "
                               "(--piece P3)")
        row = next((x for x in pieces(r, product) if on and x["id"].lower() == on.lower()), None)
        if piece and not row:
            raise KitError(f"No piece {piece} in {r.name} {product}")
        _write_questions(path, [i for i in items if i is not q])
        paths, result = [path], {done: q | {"answer" if done == "answered" else "reason": reply}}
        if row:
            where = note_path(r, product, row["id"])
            _noted(where, row, f"{said} {q['id']} ({_short(q['question'], 200)})"
                               + (": " if done == "answered" else ", deciding it: ") + reply)
            paths.append(where)
            result["note"] = core.posix(where)
        return paths, f"{product} {q['id']} {done}: {reply}", result
    return r.write(f"question {'answer' if done == 'answered' else 'withdraw'}", edit)


# ---------------------------------------------------------------- standing orders

def orders(r: Repo, product: str) -> list[str]:
    return _read_orders(r.product(product) / "orders.md")


def _read_orders(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return [re.sub(r"^\d+\.\s*", "", line) for line in path.read_text(encoding="utf-8").splitlines()
            if re.match(r"^\d+\.\s", line)]


def order(r: Repo, product: str, add: str = "", drop: int = 0) -> dict:
    path = r.product(product) / "orders.md"

    def edit():
        lines = _read_orders(path)
        if add:
            lines.append(_clean(add))
            what = f"order added: {_clean(add)}"
        elif drop:
            if not 1 <= drop <= len(lines):
                raise KitError(f"No standing order {drop}")
            what = f"order dropped: {lines.pop(drop - 1)}"
        else:
            raise KitError("Give an order to add, or a number to drop")
        core.atomic_text(path, "\n".join(["# Standing orders", ""] + [f"{n}. {t}" for n, t in enumerate(lines, 1)])
                         + "\n")
        return [path], f"{product} {what}", {"orders": lines}
    return r.write("order", edit)


# ---------------------------------------------------------------- communication channels

CHANNELS = "communication_channels.md"  # in the client repo's .work/: its own how-to for each channel
CHANNELS_TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / CHANNELS
CHECKED, CHECKED_LOCK = "wk-channels.json", "wk-channels.lock"  # this laptop's last-checked times, in the Git folder
TEMPLATE_MARK = "TEMPLATE:"  # the template's warning line; no channel is read or moved while it is there
CHANNEL = re.compile(r"^## +(\S[^\r\n]*?)[ \t\r]*$", re.M)  # a channel's heading, in an LF or CRLF checkout
AHEAD = timedelta(minutes=5)  # how far ahead of this laptop's clock a last-checked time may be
OVERLAP = timedelta(minutes=30)  # each check rereads this much before the last one; filed links are skipped
FIRST_LOOK = timedelta(days=1)  # a channel never checked on this laptop is read from this long ago


def channels(r: Repo, channel: str = "", at: str = "") -> dict:
    """The client's channels file and each channel's last-checked time on this laptop. Starts the file from the
    template when missing and saves the owner's hand edits (each a commit); otherwise only reads, so a quiet run writes
    nothing. With `channel` and `at`, moves that channel's last-checked time: local state in the repo's Git folder,
    never committed, never backwards, never ahead of now, never while the template's examples are there."""
    path = r.dir / CHANNELS
    now = datetime.now().astimezone().replace(second=0, microsecond=0)
    if channel or at:
        return _move_checked(r, path, _clean(channel), at, now)
    edited = path.is_file() and bool(core.git(r.list_top, "status", "--porcelain", "--", str(path), check=False))
    if path.is_file() and not edited:
        return _channels_result(r, path, path.read_text(encoding="utf-8"), now)

    def edit():
        started = not path.is_file()
        text = CHANNELS_TEMPLATE.read_text(encoding="utf-8") if started else path.read_text(encoding="utf-8")
        core.atomic_text(path, text)
        return [path], "channels started from the template" if started else "channels edited", \
            _channels_result(r, path, text, now)
    return r.write("channels", edit)


def _move_checked(r: Repo, path: Path, channel: str, at: str, now: datetime) -> dict:
    if not (channel and at):
        raise KitError("Give the channel (--checked \"<its heading>\") and when its read started (--at)")
    when = _time(at, "--at")
    if when is None:
        raise KitError("--at needs a time, e.g. 2026-11-04T10:30+05:00")
    if when > now + AHEAD:
        raise KitError(f"--at {_iso(when)} is in the future (now is {_iso(now)}): give the time the read started")
    if not path.is_file():
        raise KitError(f"{r.name} has no {DIR}/{CHANNELS} yet: nh channels {r.name} starts it")
    text = path.read_text(encoding="utf-8")
    if TEMPLATE_MARK in text:
        raise KitError(f"{core.posix(path)} still holds the template's example channels: fill it in with the owner "
                           f"and delete its {TEMPLATE_MARK} line first")
    names = CHANNEL.findall(text)
    if channel not in names:
        raise KitError(f"No channel '{channel}' in {core.posix(path)}. Its channels: {', '.join(names) or 'none'}")
    state = core.git_common_dir(r.top) / CHECKED
    with core.locked(r.top, "channels", CHECKED_LOCK):
        checked = _checked(state)
        was = _time(checked.get(channel, ""), f"The last check of {channel} in {core.posix(state)}")
        if was and when < was:
            raise KitError(f"{channel} was last checked at {_iso(was)}; a last-checked time never moves backwards")
        checked[channel] = _iso(when)
        core.write_json(state, {"channels": checked})
    return _channels_result(r, path, text, now) | {"moved": {channel: _iso(when)}}


def _checked(state: Path) -> dict:
    return dict(core.read_json(state).get("channels") or {}) if state.is_file() else {}


def _time(value: str, what: str):
    """An ISO date and time; without a zone it is this laptop's local time. None for 'never' or nothing."""
    value = (value or "").strip()
    if value.lower() in ("", "never"):
        return None
    try:
        at = datetime.fromisoformat(value)
    except ValueError as exc:
        raise KitError(f"{what} '{value}' is not a time: give it like 2026-11-04T10:30+05:00") from exc
    return (at if at.tzinfo else at.astimezone()).replace(second=0, microsecond=0)


def _iso(at: datetime) -> str:
    return at.isoformat(timespec="minutes")


def _channels_result(r: Repo, path: Path, text: str, now: datetime) -> dict:
    checked = _checked(core.git_common_dir(r.top) / CHECKED)
    rows = []
    for name in CHANNEL.findall(text):
        was = _time(checked.get(name, ""), f"The last check of {name}")
        rows.append({"channel": name, "last_checked": _iso(was) if was else "never",
                     "read_from": _iso((was - OVERLAP) if was else (now - FIRST_LOOK))})
    return {"channels": core.posix(path), "now": _iso(now), "template": TEMPLATE_MARK in text, "list": rows,
            "text": text}


# ---------------------------------------------------------------- inbox (messages routed to a list)

INBOX = "inbox.md"
ENTRY = re.compile(r"^## (I\d+) · (.+)$")
INBOX_FIELDS = ["Link", "Channel", "When", "Piece", "Filed"]
LINE_MAX = 200  # an entry is one line: what the message is about, never its text
LAST_ID = re.compile(r"^Last id: I(\d+)[ \t\r]*$", re.M)  # the highest id ever used, so a handled one's is never reused
# A link counts only whole: not inside a longer one (.../m/10 is not .../m/101, a thread link not one of its messages).
# Before it: the line's start, a space, an opening bracket or a quote; after it: the end, a space, a closing bracket or
# quote, or one punctuation mark that ends a sentence.
BEFORE, AFTER = r"(?<![^\s(\[<\"'])", r"(?=$|[\s)\]>\"']|[.,;:](?:\s|$))"


def inbox(r: Repo, product: str) -> list[dict]:
    return _read_inbox(r.product(product) / INBOX)


def _read_inbox(path: Path) -> list[dict]:
    items = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            m = ENTRY.match(line)
            if m:
                items.append({"id": m[1], "line": m[2]})
            elif items and line.startswith("- ") and ": " in line:
                key, value = line[2:].split(": ", 1)
                items[-1][key.lower()] = value
    return items


def _used(path: Path, items: list[dict]) -> int:
    """The highest inbox id number ever used in this list: the file's own record, or its entries (an older file)."""
    found = LAST_ID.search(path.read_text(encoding="utf-8")) if path.is_file() else None
    return max([int(i["id"][1:]) for i in items] + [int(found[1]) if found else 0])


def _write_inbox(path: Path, items: list[dict], used: int) -> None:
    out = ["# Inbox", "", "Messages routed to this list, each a link and one line. Its supervisor handles each one and "
           "marks it handled (nh inbox done), which removes it.", "", f"Last id: I{used}", ""]
    for x in items:
        out.append(f"## {x['id']} · {x['line']}")
        out += [f"- {f}: {x[f.lower()]}" for f in INBOX_FIELDS if x.get(f.lower())]
        out.append("")
    core.atomic_text(path, "\n".join(out))


def filed(r: Repo, link: str) -> str:
    """Where a message's link is already filed in this repo, or "": any list's inbox now, or any commit that added or
    removed it anywhere in .work/ (an inbox entry since handled, a piece's note line, a question since answered)."""
    link = _clean(link)
    if not link:
        raise KitError("Give the message's link (--link)")
    for product in r.products():
        for x in _read_inbox(r.dir / product / INBOX):
            if x.get("link") == link:
                return f"{product} inbox {x['id']}"
    whole = re.compile(BEFORE + re.escape(link) + AFTER)
    # -S finds each commit whose .work/ change holds the text; keep the oldest where it is the whole link
    for commit in reversed(core.git(r.list_top, "log", "-S", link, "--format=%h %s", "--", DIR, check=False).splitlines()):
        diff = core.git(r.list_top, "show", "--format=", "--unified=0", commit.split(" ", 1)[0], "--", DIR, check=False)
        if any(whole.search(x[1:]) for x in diff.splitlines() if x[:1] in "+-" and x[:3] not in ("+++", "---")):
            return f"commit {commit}"
    return ""


def inbox_add(r: Repo, product: str, line: str, link: str, channel: str, when: str = "", piece: str = "") -> dict:
    """Route one message to a list: its link and one line about it, never its text. Refused when the link is filed
    already, so a rerun over the same messages adds nothing."""
    line, link, channel = _clean(line), _clean(link), _clean(channel)
    if not line:
        raise KitError("Say in one line what the message is about")
    if len(line) > LINE_MAX:
        raise KitError(f"One line, at most {LINE_MAX} characters: the link carries the message")
    if not link or " " in link:
        raise KitError("Give the message's link (--link), one that opens it")
    if not channel:
        raise KitError("Name the channel it came from (--channel), as its heading in the channels file")
    path = r.product(product) / INBOX
    pid = _piece(r, product, piece)["id"] if _clean(piece) else ""
    if when:
        when_at = _time(when, "--when")
        when = _iso(when_at) if when_at else ""

    def edit():
        where = filed(r, link)
        if where:
            raise Refused(f"Already filed ({where}): {link}")
        items = _read_inbox(path)
        used = _used(path, items) + 1  # the next number after the highest ever used, never a handled one's
        x = {"id": f"I{used}", "line": line, "link": link, "channel": channel,
             "when": when, "piece": pid, "filed": _iso(datetime.now().astimezone())}
        _write_inbox(path, items + [x], used)
        return [path], f"{product} inbox {x['id']}: {line}", {"added": x}
    return r.write("inbox add", edit)


def inbox_done(r: Repo, product: str, iid: str, how: str = "") -> dict:
    """The supervisor handled an entry (an FYI, a line on a piece, a new piece, a drafted reply): it is removed. The
    commit keeps its link, so the duplicate check still finds it."""
    how = _clean(how)
    if not how:
        raise KitError("Say how it was handled (--how), e.g. \"FYI\", \"note on P3\", \"new piece P9\"")
    path = r.product(product) / INBOX

    def edit():
        items = _read_inbox(path)
        x = next((i for i in items if i["id"].lower() == iid.lower()), None)
        if not x:
            raise KitError(f"No inbox entry {iid} in {r.name} {product}")
        _write_inbox(path, [i for i in items if i is not x], _used(path, items))
        return [path], f"{product} inbox {x['id']} handled: {how}", {"handled": x, "how": how}
    return r.write("inbox done", edit)


# ---------------------------------------------------------------- hours

def _who(cwd: Path) -> str:
    try:
        top, branch = core.checkout_of(cwd)
    except KitError:
        return core.posix(cwd)
    return f"{top.name}@{branch or 'detached'}"


def _day(day: str, flag: str = "--date") -> str:
    try:
        return date.fromisoformat(day).isoformat() if day else date.today().isoformat()  # 20261001 is stored as 2026-10-01
    except ValueError as exc:
        raise KitError(f"{flag} must be YYYY-MM-DD") from exc


def _line_key(r: Repo, product: str, key: str) -> str:
    """A named extra invoice line of this list ("" is its usual line), refused unless the list has it."""
    key, fields = (key or "").strip().lower(), r.products()[product]
    if key and f"line-{key}" not in fields:
        have = [k[5:] for k in fields if k.startswith("line-")]
        raise KitError(f"{r.name} {product} has no invoice line '{key}' (has: {', '.join(have) or 'none'}). "
                           f"Add it: nh product set {r.name} {product} --line {key}=\"...\"")
    return key


def _for(r: Repo, product: str, ids: str) -> str:
    """The pieces some hours pay for ("P3,P7"), each a piece of this product."""
    known = {x["id"].upper(): x for x in pieces(r, product)}
    chosen = [i.strip().upper() for i in (ids or "").split(",") if i.strip()]
    missing = [i for i in chosen if i not in known]
    if missing:
        raise KitError(f"No piece {', '.join(missing)} in {r.name} {product}")
    for choice, said in (("no", "not billing"), ("later", "bill later")):
        held = [i for i in chosen if known[i]["bill"] == choice]
        if held:
            raise KitError(f"{', '.join(held)} is marked {said} by the owner. If he now bills it, first "
                               f"nh piece set {r.name} {product} {held[0]} --bill yes")
    return ",".join(dict.fromkeys(chosen))


def add_hours(r: Repo, cwd: Path, product: str, hours: float, note: str, day: str = "", again: bool = False,
              for_pieces: str = "", line_key: str = "") -> dict:
    r.product(product)
    if not 0 < hours <= 24:
        raise KitError("Hours must be more than 0 and at most 24")
    note = _clean(note)
    if not note:
        raise KitError("Say what the hours were for (--note)")
    day = _day(day)
    paid_for, line_key = _for(r, product, for_pieces), _line_key(r, product, line_key)
    path = r.dir / "hours.tsv"

    def edit():
        rows = _read_tsv(path, HOUR_COLUMNS)
        same = [x for x in rows if (x["date"], x["product"]) == (day, product) and not x["billed"]]
        if same and not again:
            first = same[0]
            raise KitError(f"{r.name} {product} already has {first['hours']}h on {day} ('{first['note']}', "
                               f"recorded by {first['by']}). If this is separate work the owner stated, add --again.")
        row = {"date": day, "product": product, "hours": f"{hours:g}", "note": note, "by": _who(cwd), "billed": "",
               "pieces": paid_for, "line_key": line_key}
        _write_tsv(path, HOUR_COLUMNS, rows + [row])
        return [path], f"{hours:g}h {product} {day}: {note}", {"recorded": row}
    return r.write("hours add", edit)


def fix_hours(r: Repo, product: str, day: str, hours: float | None = None, note: str | None = None,
              for_pieces: str | None = None, remove: bool = False, was: str = "", nth: int = 0,
              line_key: str | None = None) -> dict:
    """Change or remove one unbilled entry: the day's only one, the one whose note is `was` (else contains it), or the
    `nth` of the day's entries in the order `nh hours list` shows them. `line_key` moves it to a named invoice line of
    the list ("" back to its usual line). Billed entries are frozen: they are on an invoice."""
    r.product(product)
    day = _day(day)
    if not remove and hours is None and note is None and for_pieces is None and line_key is None:
        raise KitError("Say what to change (hours, --note, --for, --line-key) or --remove")
    if hours is not None and not 0 < hours <= 24:
        raise KitError("Hours must be more than 0 and at most 24")
    if note is not None and not _clean(note):
        raise KitError("A note can't be empty")
    paid_for = None if for_pieces is None else _for(r, product, for_pieces)
    line_key = None if line_key is None else _line_key(r, product, line_key)
    path = r.dir / "hours.tsv"

    def edit():
        rows = _read_tsv(path, HOUR_COLUMNS)
        day_rows = [x for x in rows if (x["date"], x["product"]) == (day, product)]
        if nth:
            if not 0 < nth <= len(day_rows):
                raise KitError(f"{r.name} {product} has {len(day_rows)} entries on {day}, so no --nth {nth}")
            same = [day_rows[nth - 1]]
        else:
            want = _clean(was).lower()
            same = ([x for x in day_rows if x["note"].lower() == want]
                    or [x for x in day_rows if want in x["note"].lower()])
        if not same:
            raise KitError(f"{r.name} {product} has no hours on {day}" + (f" with '{was}' in the note" if was else ""))
        if len(same) > 1:
            raise KitError(f"{r.name} {product} has {len(same)} entries on {day} that match; choose one with --was \"<its whole note>\" or --nth: "
                               + "; ".join(f"{i}. {x['hours']}h '{x['note']}'" + (f" (on {x['billed']})" if x["billed"]
                                                                                   else "")
                                           for i, x in enumerate(day_rows, 1) if any(x is y for y in same)))
        row = same[0]
        if row["billed"]:
            raise KitError(f"That entry is on {row['billed']} and stays as it is. A billed entry is never changed.")
        before = dict(row)
        if remove:
            rows.remove(row)
        else:
            row.update({k: v for k, v in {"hours": None if hours is None else f"{hours:g}",
                                          "note": None if note is None else _clean(note),
                                          "pieces": paid_for, "line_key": line_key}.items() if v is not None})
        _write_tsv(path, HOUR_COLUMNS, rows)
        what = "removed" if remove else (f"now {row['hours']}h: {row['note']}" + (f" ({row['pieces']})" if row["pieces"] else "")
                                         + (f" on line {row['line_key']}" if row["line_key"] else ""))
        return [path], f"hours {product} {day} {what}", {"was": before, "now": None if remove else row}
    return r.write("hours fix", edit)


def rows_digest(rows: list[dict]) -> str:
    """sha256 of the rows' date, product, hours and note: one line each, tab-separated, sorted, joined by newlines."""
    lines = sorted(TAB.join(x[k] for k in ("date", "product", "hours", "note")) for x in rows)
    return hashlib.sha256(NL.join(lines).encode("utf-8")).hexdigest()


def mark_hours(r: Repo, through: str, label: str, expect: float, products: list[str] = (), digest: str = "") -> dict:
    """Mark unbilled rows dated on or before `through` as done under `label` (an invoice number, a timesheet week).
    Used by add-ons such as invoicing. `expect` is the hours the add-on counted, and `digest` (optional) the
    rows_digest of the rows it counted: any difference means hours changed after it looked, so nothing is marked. A
    label is used once per repo, and a marked row is never unmarked."""
    label = _clean(label)
    if not label:
        raise KitError("Give the label the hours are marked with (--as), e.g. INV-007")
    try:
        through = date.fromisoformat(through).isoformat()
    except ValueError as exc:
        raise KitError("--through must be YYYY-MM-DD") from exc
    for p in products:
        r.product(p)
    path = r.dir / "hours.tsv"

    def edit():
        rows = _read_tsv(path, HOUR_COLUMNS)
        if any(x["billed"] == label for x in rows):
            raise KitError(f"{r.name} already has hours marked {label}; a label is used once")
        chosen = [x for x in rows if not x["billed"] and x["date"] <= through
                  and (not products or x["product"] in products)]
        total = round(sum(float(x["hours"]) for x in chosen), 2)
        if not chosen:
            raise KitError(f"{r.name} has no unmarked hours through {through}")
        if abs(total - expect) > 0.005:
            raise KitError(f"{r.name} has {total:g}h unmarked through {through}, not the {expect:g}h expected. "
                               "Hours changed since they were counted: count again.")
        if digest and rows_digest(chosen) != digest:
            raise KitError(f"{r.name}'s unmarked hours through {through} changed since they were counted (same "
                               "total, different entries): count again.")
        for x in chosen:
            x["billed"] = label
        _write_tsv(path, HOUR_COLUMNS, rows)
        return [path], f"{total:g}h through {through} marked {label}", {"marked": chosen, "label": label,
                                                                          "total": total}
    return r.write("hours mark", edit)


def hours(r: Repo, unbilled: bool = False) -> dict:
    rows = [x for x in _read_tsv(r.dir / "hours.tsv", HOUR_COLUMNS) if not unbilled or not x["billed"]]
    totals = defaultdict(float)
    for x in rows:
        totals[x["product"]] += float(x["hours"])
    return {"repo": r.name, "rows": rows, "totals": {k: round(v, 2) for k, v in sorted(totals.items())},
            "total": round(sum(totals.values()), 2)}


# ---------------------------------------------------------------- status (computed, never stored)

def status(s: dict, recent_days: int = 7) -> list[dict]:
    """Every repo's lists, computed from `.work/`: open pieces (with their notes), questions, and what finished lately.
    A repo whose files can't be read is reported on its own and never hides the others."""
    out, since = [], (date.today() - timedelta(days=recent_days)).isoformat()
    for name in s.get("repos") or {}:
        try:
            r = Repo(s, name)
            products = r.products()
        except KitError as exc:
            out.append({"repo": name, "error": str(exc)})
            continue
        for product, fields in products.items():
            rows = _read_tsv(r.dir / product / "pieces.tsv", PIECE_COLUMNS)
            open_rows = [x for x in rows if x["state"] in OPEN]
            for x in open_rows:
                path = r.dir / product / "notes" / f"{x['id']}.md"
                text = path.read_text(encoding="utf-8") if path.is_file() else ""
                x["note"], x["note_text"], x["latest"] = (core.posix(path) if text else ""), text, latest(text)
            out.append({"repo": name, "product": product, "supervisor": fields.get("supervisor", ""),
                        "title": fields.get("title", ""), "line": fields.get("line", ""),
                        "open": open_rows,
                        # by the day it finished (older pieces: their last change), so editing an old piece never
                        # brings it back
                        "recent": [{"id": x["id"], "piece": x["piece"], "updated": x["updated"], "pr": x["pr"],
                                    "finished": x["finished"] or x["updated"]}
                                   for x in rows if x["state"] in FINISHED and (x["finished"] or x["updated"]) >= since],
                        "counts": {st: sum(x["state"] == st for x in rows) for st in STATES},
                        "questions": _read_questions(r.dir / product / "questions.md"),
                        "inbox": len(_read_inbox(r.dir / product / INBOX)),
                        "orders": len(_read_orders(r.dir / product / "orders.md"))})
    return out


# ---------------------------------------------------------------- recall (what a supervisor reads at start)

RECALL_CAP = 4000  # characters: Claude cuts hook output over 10,000 to a short preview, and every turn re-reads it
GH_WAIT = 10       # seconds in all for GitHub to list the repos' PRs (the hook allows 30); past it, recall says so
WHY = {"startup": "this chat started", "resume": "this chat resumed", "compact": "your memory was compacted",
       "clear": "the chat was cleared"}  # the hook's `source`: what happened to this chat


def _short(text: str, width: int = 300) -> str:
    """One line, cut at a word boundary."""
    text = _clean(text)
    if len(text) <= width:
        return text
    cut = text[:width - 1]
    return (cut[:cut.rfind(" ")] if " " in cut else cut).rstrip(" ,;:") + "…"


def _pr_number(pr: str) -> str:
    return pr.strip().lstrip("#").rstrip("/").rsplit("/", 1)[-1]


def _gh_prs(top: Path, wait: float = GH_WAIT) -> dict:
    """The repo's recent PRs by number, with state and checks. KitError when GitHub doesn't answer in `wait`
    seconds."""
    try:
        result = core.run(["gh", "pr", "list", "--state", "all", "--limit", "100", "--json",
                           "number,state,isDraft,statusCheckRollup"], wait, cwd=str(top))
    except OSError as exc:
        raise KitError(f"gh failed: {exc}") from exc
    if result.returncode:
        raise KitError("gh: " + (result.stderr.strip() or "failed"))
    return {str(x["number"]): x for x in json.loads(result.stdout or "[]")}


def _pr_state(pr: dict) -> str:
    """draft / ready with its CI result, or merged / closed."""
    if pr.get("state") in ("MERGED", "CLOSED"):
        return pr["state"].lower()
    why = ci_green(pr.get("statusCheckRollup") or [])
    ci = ("CI passed" if not why else "CI running" if "running" in why else "CI failed" if "failed" in why
          else "no CI result")
    return ("draft" if pr.get("isDraft") else "ready") + ", " + ci


def _decisions(text: str, name: str) -> list[str]:
    """The note's dated lines that record the owner's decision, oldest first: "- <date>: <name>: ..." (a typed
    "(<date>)" before the name allowed) and wk's own "Owner answered Q<n> (...): ..." lines."""
    who = re.escape(name) + ":|" if name else ""
    mark = re.compile(r"^- \d{4}-\d{2}-\d{2}: (?:\(\d{4}-\d{2}-\d{2}\) )?(?:" + who + r"Owner answered Q\d+ )")
    return [x[2:] for x in text.splitlines() if mark.match(x)]


def _recall_list(s: dict, r: Repo, product: str, fields: dict, owner: str, prs: dict, deadline: float) -> dict:
    """One list's parts for recall; prs caches each repo's PRs (None: GitHub didn't answer before the deadline, a
    time.monotonic() shared by every repo)."""
    d = r.dir / product
    rows = [x for x in _read_tsv(d / "pieces.tsv", PIECE_COLUMNS) if x["state"] in OPEN]
    rows.sort(key=lambda x: int(x["rank"]) if x["rank"].isdigit() else 10 ** 6)  # the recommended order first
    if any(x["pr"] for x in rows) and r.name not in prs:
        left = deadline - time.monotonic()
        try:
            prs[r.name] = _gh_prs(r.top, left) if left >= 1 else None
        except (KitError, ValueError, TypeError, KeyError):
            prs[r.name] = None
    known = prs.get(r.name)
    pieces = []
    for x in rows:
        path = d / "notes" / f"{x['id']}.md"
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        last, n = latest(text), _pr_number(x["pr"])
        head = (f"- {x['id']} {x['state']}: {x['piece']}" + (f" · builder {x['chat']}" if x["chat"] else "")
                + (f" · branch {x['branch']}" if x["branch"] else "")
                + (f" · PR {n}" + ("" if known is None else f" ({_pr_state(known[n]) if n in known else 'unknown'})")
                   if n else ""))
        notes = [f"  latest: {_short(last)}"] if last else []
        notes += [f"  decided: {_short(t)}" for t in [t for t in _decisions(text, owner) if t != last][-2:]]
        pieces.append({"todo": x["state"] == "todo", "head": head, "notes": notes, "branch": x["branch"]})
    branches = {p["branch"] for p in pieces if p["branch"]}
    try:
        held = [g for g in turn_rows(r.top) if g["holder"] in branches]
    except (KitError, OSError, ValueError, KeyError):
        held = []
    waiting = _read_inbox(d / INBOX)
    try:
        queue = queue_lines(queue_status(s, r, product, wait=deadline - time.monotonic()))
    except Exception as exc:  # noqa: BLE001 - the start block must never fail
        queue = [f"Queue: couldn't be read ({_short(str(exc), 120)}): nh piece next {r.name} {product}"]
    return {"fixes": _to_fix(s, r, product), "queue": queue, "title": f"{r.name} / {product}" + (f" ({fields['title']})" if fields.get("title") else ""),
            "repo": r.name, "product": product, "questions": _read_questions(d / "questions.md"), "pieces": pieces,
            "gh": "PR status unavailable (GitHub didn't answer)" if prs.get(r.name, {}) is None else "",
            "turns": "Turns held by this list's builders: " + "; ".join(
                f"{g['holder']} on {g['resource']} ({', '.join(g['items'])}; {g['state']})" for g in held)
            if held else "",
            "inbox": f"Inbox: {len(waiting)} message(s) to handle: nh inbox list {r.name} {product}" if waiting else "",
            "orders": _read_orders(d / "orders.md")}


def _to_fix(s: dict, r: Repo, product: str) -> list[dict]:
    """The list's nh check findings that need no network (check.at_start: its own files only, so the start stays
    fast), one line each; those only the owner's yes settles are marked. Never fails: an unreadable list shows none."""
    from . import check  # check reads work's records, so it is imported when used
    try:
        found = check.at_start(s, r.name, product)
    except Exception:  # noqa: BLE001 - the start block must never fail
        return []
    return [{"line": f"- {p['item'] or p['kind']}" + (" (needs the owner's yes)" if check.needs_owner(p) else "")
             + f": {_short(p['what'] + '. Fix: ' + p['fix'], 220)}"} for p in found]


def _importance(lists: list[dict]) -> list[int]:
    """Every list's open pieces and to-fix lines (by id()) most important first: all pieces being built or ready, then
    all to-fix lines, then all to-dos."""
    return ([id(p) for x in lists for p in x["pieces"] if not p["todo"]] + [id(f) for x in lists for f in x["fixes"]]
            + [id(p) for x in lists for p in x["pieces"] if p["todo"]])


def _render(why: str, lists: list[dict], todo_notes: bool = True, keep: int | None = None) -> str:
    """Most important first: questions, pieces being built or ready, to-fix lines, to-do pieces, inbox, standing
    orders. keep: how many open pieces and to-fix lines to show in all, by importance across the lists; the rest are
    counted on one line per list."""
    out = [f"nearly-headless: your list as it stands now{f' ({why})' if why else ''}. "
           "Full notes: nh piece note <repo> <product> <id>."]
    kept = None if keep is None else set(_importance(lists)[:keep])
    for x in lists:
        out.append(f"\n{x['title']}")
        qs = x["questions"]
        out += ["Waiting on the owner:"] + [f"- {q['id']}: {_short(q['question'], 160)}"
                                            + (f" ({q['piece']})" if q.get("piece") else "") for q in qs] if qs else []
        ordered = [p for p in x["pieces"] if not p["todo"]] + [p for p in x["pieces"] if p["todo"]]
        shown = [p for p in ordered if kept is None or id(p) in kept]
        out += [] if x["pieces"] else ["Open pieces: none"]
        for label, todo in (("Being built or ready:", False), ("To do:", True)):
            group = [p for p in shown if p["todo"] == todo]
            out += [label] if group else []
            for p in group:
                out += [p["head"]] + (p["notes"] if todo_notes or not todo else [])
            if not todo:  # the queue, then to-fix lines, rank after the questions and the pieces being built
                out += x.get("queue") or []
                fixes = [f for f in x["fixes"] if kept is None or id(f) in kept]
                out += [f"To fix (yours, unless marked; nh check {x['repo']} {x['product']} for all):"] + \
                    [f["line"] for f in fixes] if fixes else []
                gone = len(x["fixes"]) - len(fixes)
                out += [f"{gone} more to fix: nh check {x['repo']} {x['product']}"] if gone else []
        hidden = len(ordered) - len(shown)
        out += [f"{hidden} more open pieces: nh piece list {x['repo']} {x['product']}"] if hidden else []
        out += [y for y in (x["gh"], x["turns"], x["inbox"]) if y]
        out += ["Standing orders:"] + [f"{n}. {t}" for n, t in enumerate(x["orders"], 1)] if x["orders"] else []
    return "\n".join(out)


def _capped(why: str, lists: list[dict]) -> str:
    """The whole block when it fits RECALL_CAP; else to-do pieces lose their note lines, then the least important
    pieces and to-fix lines (_importance) are counted instead of shown."""
    text = _render(why, lists)
    for keep in range(sum(len(x["pieces"]) + len(x["fixes"]) for x in lists), -1, -1):
        if len(text) <= RECALL_CAP:
            return text
        text = _render(why, lists, False, keep)
    return text if len(text) <= RECALL_CAP else text[:RECALL_CAP - 1] + "…"  # questions and orders alone too long


def recall(s: dict, ids: list[str], cwd: Path, source: str = "") -> str:
    """What a supervisor chat reads when it starts or resumes and after its context is compacted or cleared (`source`,
    from Claude's hook), short enough not to flood it. A list whose recorded supervisor link names one of `ids` (this
    chat's session ids) is shown: questions for the owner, pieces being built or ready (builder, branch, PR state and
    CI), to-do pieces, each with its newest note line and the owner's newest decisions, turns held, inbox and standing
    orders, within RECALL_CAP. Otherwise, after a compaction or clear in a checkout of a repo in the settings, one line
    per list with open work; anywhere else, nothing."""
    ids = [i for i in ids if len(i) >= 8]  # never match a link on a stray short value
    ours = lambda fields: any(i in str(fields.get("supervisor", "")) for i in ids)  # noqa: E731
    try:
        common = core.git_common_dir(cwd)
    except KitError:
        common = None  # not in a checkout: only a recorded supervisor link can match
    mine, here = [], []
    for name in s.get("repos") or {}:
        try:
            r = Repo(s, name)
            products = r.products()
            in_repo = common is not None and any(core.same_path(core.git_common_dir(t), common)
                                                 for t in {r.top, r.list_top})
        except KitError:
            continue  # a repo that can't be read never stops the others
        for product, fields in products.items():
            if ours(fields):
                mine.append((r, product, fields))
            elif in_repo:
                here.append((r, product, fields))
    if mine:
        try:
            owner = settings.approver(s)
        except KitError:
            owner = ""
        prs, deadline = {}, time.monotonic() + GH_WAIT
        return _capped(WHY.get(source, ""), [_recall_list(s, r, p, f, owner, prs, deadline) for r, p, f in mine])
    if source in ("startup", "resume"):
        return ""  # a chat that supervises no list starts as Claude starts it
    lines = []
    for r, product, _ in here:
        rows = [x for x in _read_tsv(r.dir / product / "pieces.tsv", PIECE_COLUMNS) if x["state"] in OPEN]
        qs = _read_questions(r.dir / product / "questions.md")
        if rows or qs:
            lines.append(f"{r.name} {product}: {len(rows)} open, {len(qs)} question(s)")
    if not lines:
        return ""
    return ("nearly-headless lists in this repo: " + "; ".join(lines) + ". Each list in full: nh piece list, "
            "nh question list, nh order list (each <repo> <product>).")


# ---------------------------------------------------------------- the owner-approved queue

def _paths(value: str) -> str:
    """--files: paths, folders (ending in /) or globs, comma or space separated, stored as one comma list; `none` for
    work that touches no repo files."""
    items = [p.strip().replace("\\", "/").removeprefix("./") for p in re.split(r"[,\s]+", value or "") if p.strip()]
    if any(i.lower() == "none" for i in items):
        if len(items) > 1:
            raise KitError("--files is either the files the piece will touch or none, not both")
        return "none"
    return ",".join(dict.fromkeys(items))


def _ids(value: str) -> list[str]:
    return [i.upper() for i in re.split(r"[,\s]+", value or "") if i]


def files_of(row: dict) -> list[str]:
    """A piece's named files (none and unnamed are both no files)."""
    return [f for f in (row.get("files") or "").split(",") if f and f != "none"]


def _hits(mine: list[str], theirs: list[str]) -> list[str]:
    """Their files that one of mine is, is inside (a folder, with or without its /) or matches as a glob, or the
    other way round."""
    def one(a: str, b: str) -> bool:
        return a == b or b.startswith(a.rstrip("/") + "/") or fnmatch.fnmatchcase(b, a)
    return [b for b in theirs if any(one(a, b) or one(b, a) for a in mine)]


def queue_plan(rows: list[dict], limit: int | None, prs: dict | None, held: list[dict] | None) -> dict:
    """Which queued pieces start now and why the others wait, computed, never stored. rows: the list's pieces; prs:
    the repo's open PRs {number: {"files": [...], "branch": ...}}, None when GitHub didn't answer; held: the repo's
    turns, None when they couldn't be read. In queue order, a piece waits while a piece it comes after hasn't merged,
    while it names no files, while what holds files is unknown, while its files overlap an open PR, a turn, a piece
    being built or one starting before it, and while the list already runs `limit` builders (a piece being built
    counts; one waiting on a merge doesn't; None: no limit)."""
    by_id = {x["id"].upper(): x for x in rows}
    building = [x for x in rows if x["state"] == "building"]
    queued = sorted((x for x in rows if x["state"] == "todo" and str(x.get("queue", "")).isdigit()),
                    key=lambda x: int(x["queue"]))
    holders = [(f"PR {n}", p.get("branch", ""), p.get("files") or []) for n, p in (prs or {}).items()]
    holders += [(f"a turn on {g['resource']} held by {g['holder']}", g["holder"], g.get("items") or [])
                for g in held or []]
    holders += [(f"{x['id']}, being built", x["branch"], files_of(x)) for x in building]
    free, items = (float("inf") if limit is None else limit - len(building)), []
    for x in queued:
        files, why = files_of(x), ""
        unmet = [a for a in _ids(x.get("after", "")) if (by_id.get(a) or {}).get("state") not in FINISHED]
        if unmet:
            why = f"after {', '.join(unmet)}, not merged yet"
        elif not x.get("files"):
            why = f"names no files: nh piece set <repo> <product> {x['id']} --files ... (or none)"
        elif files and prs is None:
            why = "GitHub didn't answer, so the open PRs' files are unknown"
        elif files and held is None:
            why = "the turns couldn't be read"
        else:
            for label, branch, theirs in holders:
                if label == f"PR {_pr_number(x['pr'])}" or (branch and branch == x["branch"]):
                    continue  # its own PR or branch
                if hit := _hits(files, theirs):
                    why = f"overlaps {label} ({', '.join(hit[:3])}{', ...' if len(hit) > 3 else ''})"
                    break
        if not why and free <= 0:
            why = f"no free builder ({limit} of {limit} running)"
        if not why:
            free -= 1
            holders.append((f"{x['id']}, starting now", x["branch"], files))
        items.append({"id": x["id"], "piece": x["piece"], "place": int(x["queue"]), "start": not why, "why": why})
    return {"limit": limit, "building": len(building), "items": items}


OPEN_PRS = "1000"  # every open PR, so the overlap check misses none (gh lists 30 by default)


def open_pr_files(top: Path, wait: float = GH_WAIT) -> dict:
    """The repo's open PRs (any list's) with the files each changes. KitError when GitHub doesn't answer."""
    try:
        result = core.run(["gh", "pr", "list", "--state", "open", "--limit", OPEN_PRS, "--json",
                           "number,headRefName,files"], wait, cwd=str(top))
    except OSError as exc:
        raise KitError(f"gh failed: {exc}") from exc
    if result.returncode:
        raise KitError("gh: " + (result.stderr.strip() or "failed"))
    return pr_files(json.loads(result.stdout or "[]"))


def pr_files(prs: list[dict]) -> dict:
    """gh's PR list (with files) as queue_plan takes it."""
    return {str(p["number"]): {"branch": p.get("headRefName", ""), "files": [f.get("path", "") for f in p.get("files") or []]}
            for p in prs}


NOT_ASKED = object()


def queue_status(s: dict, r: Repo, product: str, prs=NOT_ASKED, wait: float = GH_WAIT) -> dict:
    """The list's queue as it stands: queue_plan over its pieces, the repo's open PRs (asked of GitHub only when
    something is queued and the caller has none) and turns."""
    rows = pieces(r, product)
    limit = settings.builders_per_list(s)
    if not any(x["state"] == "todo" and x["queue"] for x in rows):
        return {"limit": limit, "building": sum(x["state"] == "building" for x in rows), "items": []}
    if prs is NOT_ASKED:
        try:
            prs = open_pr_files(r.top, wait) if wait >= 1 else None
        except (KitError, ValueError, TypeError, KeyError):
            prs = None
    try:
        held = turn_rows(r.top)
    except (KitError, OSError, ValueError, KeyError):
        held = None
    return queue_plan(rows, limit, prs, held)


def queue_lines(q: dict) -> list[str]:
    """The queue as recall and nh piece next print it."""
    if not q["items"]:
        return []
    if q["limit"] is None:
        running = f"{q['building']} builder{'' if q['building'] == 1 else 's'} running"
    else:
        running = f"{q['building']} of {q['limit']} builders running"
    return [f"Queue, approved by the owner ({running}; start each 'start now' "
            "at once with its brief):"] + [f"{x['place']}. {x['id']} {x['piece']}: "
                                          + ("start now" if x["start"] else f"waits: {x['why']}") for x in q["items"]]


def queue_pieces(r: Repo, product: str, ids: str) -> dict:
    """The queue the owner approved, first to last (e.g. "P5,P7"), in one write; `none` empties it. Only to-do pieces
    with their brief in their note and their files named (--files, or none) can be queued; starting a piece (any
    state but todo) takes it off. Leaves `updated` alone: queueing is not progress."""
    empty = (ids or "").strip().lower() == "none"
    order = [] if empty else _ids(ids)
    if not empty and not order:
        raise KitError("Give the to-do pieces in the order the owner approved, e.g. P5,P7, or none to empty the queue")
    if len(set(order)) != len(order):
        raise KitError("Name each piece once")
    path = r.product(product) / "pieces.tsv"

    def edit():
        rows = _read_tsv(path, PIECE_COLUMNS)
        todo = {x["id"].upper(): x for x in rows if x["state"] == "todo"}
        if wrong := [i for i in order if i not in todo]:
            raise KitError(f"Only a to-do piece can be queued; not one in {r.name} {product}: {', '.join(wrong)}")
        if no_brief := [i for i in order if not (p := note_path(r, product, i)).is_file()
                        or not p.read_text(encoding="utf-8").strip()]:
            raise KitError(f"A queued piece starts from its brief, in its note, and {', '.join(no_brief)} has none: "
                               f"nh piece note {r.name} {product} <id> --file brief.md")
        if no_files := [i for i in order if not todo[i]["files"]]:
            raise KitError(f"Name the files each queued piece will touch, so it never starts on files an unmerged PR "
                               f"or a turn holds: nh piece set {r.name} {product} <id> --files \"a.py,docs/\" (or "
                               f"--files none). Missing: {', '.join(no_files)}")
        for x in rows:
            x["queue"] = str(order.index(x["id"].upper()) + 1) if x["id"].upper() in order else ""
        _write_pieces(path, rows)
        return [path], f"{product} queue: {','.join(order) or 'empty'}", {"queue": order}
    return r.write("piece queue", edit)
