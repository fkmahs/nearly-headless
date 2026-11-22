"""`nh audit measure`: the counting half of the weekly audit (skills/weekly-audit), read-only.

It reads this laptop's Claude Code chats (~/.claude/projects/<folder>/<session>.jsonl; agents' own files under
<session>/subagents/ and sidechain lines are skipped) changed in the last N days, one line at a time, and counts per
chat: turns, the chat's own tool calls per turn, agents, browser use, shell calls, compactions, merges and whether a
fresh gate ran before each (an nh merge runs its own gate, so it counts as gated; a raw merge the merge guard refused
never ran), push notifications, and keyword hits in the owner's messages. It writes measure.json and a
short measure.md to a private folder beside the laptop's settings (audits/<date>/), never inside a repo. Only counts
are kept: no message text, ever. Judging the chats is the skill's agents' job.
"""

from __future__ import annotations

import json
import re
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import core, merge, reply
from .core import KitError

# what the owner's messages are counted for; one place to tune. Each message counts once per kind.
KEYWORDS = {
    "corrections": ["no,", "not what", "wrong", "i said", "i told you", "i asked", "again", "instead", "why did you",
                    "you didn't", "undo", "revert", "that's not"],
    "status_asks": ["status", "where are we", "where does it stand", "where things stand", "what's left", "progress",
                    "how is it going", "how's it going", "catch me up", "any news", "is it done", "done yet"],
    "sign_in_or_click": ["sign in", "signed in", "log in", "logged in", "login", "click", "password", "2fa",
                         "verification code", "open the browser"],
}
_KEYS = {k: re.compile("|".join(r"(?<!\w)" + re.escape(w) + (r"(?!\w)" if w[-1].isalnum() else "") for w in words),
                       re.I) for k, words in KEYWORDS.items()}
_AGENT = ("Agent", "Task")
_SHELL = ("Bash", "PowerShell")
_PICK = re.compile(r"__(?:list|select|switch)_\w*browsers?$", re.I)  # choosing which browser, not using it
_ROLES = (("project manager", re.compile(r"project[- ]manager", re.I)), ("builder", re.compile(r"\bbuilder\b", re.I)),
          ("supervisor", re.compile(r"\bsupervisor\b", re.I)))
_STEP = re.compile(r"&&|\|\||[;|\n]|[\"']")  # one command line holds steps; each must start with the command to count
_THEN = r"^\s*(?:if\s*\(\$\?\)\s*\{\s*)?"  # PowerShell's `; if ($?) { ... }` runs it too
_FLAGS = r"(?:\s+--?[\w-]+(?:[ =][^\s-]\S*)?)*"  # a tool's own flags before its subcommand: gh -R acme/app pr ...
_MERGE = re.compile(_THEN + r"(?:[^\s\"';&|]*[\\/])?gh(?:\.exe)?" + _FLAGS + r"\s+pr\s+merge\b(.*)$", re.S)
_NH = _THEN + r"&?\s*(?:" + reply._WK + r"|(?:py|python3?)(?:\.exe)?\s+(?:-\w+\s+)*-m\s+wk)" + _FLAGS
_FRESH = re.compile(_NH + r"\s+gate\s+fresh\b(.*)$", re.S)
_NH_MERGE = re.compile(_NH + r"\s+merge\b(.*)$", re.S)  # nh merge: the gate, CI and the merge in one step
_DRY = re.compile(r"(?:^|\s)--dry-run\b")
_GUARDED = merge.REFUSAL.split(" (")[0]  # the merge guard's refusal: that raw merge never ran
_MERGED = re.compile(r"^ok\s+merge:", re.M)  # nh merge's line once it merged, even if a later step stops
_PR = re.compile(r"--pr[ =]+#?(\d+)|/pull/(\d+)")
_VALUE = re.compile(r"--?(?:match-head-commit|repo|R|subject|t|body|b|author-email|A)[ =]+\S+")
_NUMBER = re.compile(r"(?:^|\s)#?(\d+)(?=\s|$)")

def root() -> Path:
    from . import usage
    return usage.projects()


def folder(settings_file: Path, day: datetime) -> Path:
    """The private per-laptop folder a run writes to: beside the settings file, audits/<YYYY-MM-DD>/."""
    return settings_file.parent / "audits" / day.strftime("%Y-%m-%d")


def _in_repo(where: Path) -> bool:
    return any((p / ".git").exists() for p in [where, *where.parents])


def _entries(path: Path):
    """A transcript's entries, streamed: a bad line, bad UTF-8 or a half-written last line is skipped."""
    with path.open("rb") as f:
        for raw in f:
            try:
                d = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if isinstance(d, dict):
                yield d


def _when(d: dict) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(d.get("timestamp")).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _p90(values: list[int]) -> int:
    ordered = sorted(values)
    return ordered[max(0, -(-len(ordered) * 9 // 10) - 1)] if ordered else 0


def steps(command: str) -> list[tuple[str, int | None]]:
    """The command's merges and fresh gates, in order, as ("merge"|"gate"|"nh merge", PR number or None): only steps
    that run the command itself, never one inside a grep or an echo, nor a --help or an nh merge --dry-run."""
    found = []
    for step in _split(command):
        if reply._HELP.search(step):
            continue
        if m := _MERGE.match(step):
            found.append(("merge", _pr_of(m.group(1), merge=True)))
        elif m := _FRESH.match(step):
            found.append(("gate", _pr_of(m.group(1))))
        elif (m := _NH_MERGE.match(step)) and not _DRY.search(m.group(1)):
            found.append(("nh merge", _pr_of(m.group(1))))
    return found


def _split(command: str) -> list[str]:
    """The command's steps, split at ;, &&, ||, | and new lines outside quotes."""
    found, start, quote = [], 0, ""
    for m in _STEP.finditer(command):
        sep = m.group(0)
        if sep in "\"'":
            quote = "" if quote == sep else quote or sep
        elif not quote:
            found.append(command[start:m.start()])
            start = m.end()
    return found + [command[start:]]


def _pr_of(args: str, merge: bool = False) -> int | None:
    m = _PR.search(args) or (merge and _NUMBER.search(_VALUE.sub("", args)) or None)
    return int(next(g for g in m.groups() if g)) if m else None


def _text(b: dict) -> str:
    content = b.get("content")
    return content if isinstance(content, str) else " ".join(
        str(c.get("text", "")) for c in content if isinstance(c, dict)) if isinstance(content, list) else ""


def _passed(b: dict) -> bool:
    """A tool result that is no error: the command ran and nothing in it says it was refused."""
    return not b.get("is_error") and not re.search(r'"ok":\s*false|^Exit code [1-9]|^nh: ', _text(b), re.M)


def chat(path: Path, since: datetime) -> dict | None:
    """One chat's counts from since on; None when nothing in it is that recent. A turn already running when the
    window opens counts once, whole."""
    row = {"session": path.stem, "project": path.parent.name, "first": "", "last": "", "role": "other",
           "owner_messages": 0, "turns": 0, "own_calls": {"total": 0, "median": 0, "p90": 0, "max": 0}, "agents": 0,
           "browser": 0, "browser_picking": 0, "shell": 0, "compactions": 0, "merges": 0, "merges_gated": 0,
           "merges_unknown": 0, "push_notifications": 0, "keywords": {k: 0 for k in KEYWORDS}}
    turns: list[list] = []  # [own calls, touched the window]
    pending: dict = {}  # a shell call's id -> (its merges and gates, inside the window), until its result comes
    gates: set = set()  # PRs with a passed fresh gate no merge has used yet
    ended, roled, inside, summaries = True, False, False, 0

    def settle(found, was_inside, ok, text=""):
        chained = False  # a gate earlier in the same command: a failed result may mean the merge never ran
        for kind, pr in found:
            if kind == "gate":
                chained = True
                if ok and pr is not None:
                    gates.add(pr)
                continue
            if (chained and not ok) or (kind == "merge" and _GUARDED in text):
                continue
            if kind == "nh merge" and not (ok or _MERGED.search(text)):
                continue  # it stopped before merging
            if was_inside:
                row["merges"] += 1
                row["merges_unknown"] += pr is None
                row["merges_gated"] += kind == "nh merge" or pr in gates
            gates.discard(pr)  # a gate is used up by the merge it came before
    for d in _entries(path):
        if d.get("isSidechain"):
            continue
        t = _when(d)
        if t is not None:
            inside = t >= since
        blocks = reply._blocks(d)
        if inside and t is not None:
            row["first"] = row["first"] or d["timestamp"]
            row["last"] = d["timestamp"]
        starts = reply._owner_said(d, blocks) is not None or (reply._other(d, blocks) and ended)
        if turns and inside and not starts:
            turns[-1][1] = True
        said = reply._owner_said(d, blocks)
        if said is not None and not roled:  # the chat's first message says what it is, even before the window
            roled, row["role"] = True, next((r for r, p in _ROLES if p.search(said)), "other")
        if d.get("type") == "assistant":
            for b in blocks:
                if b.get("type") != "tool_use":
                    continue
                name = str(b.get("name", ""))
                given = b.get("input") if isinstance(b.get("input"), dict) else {}
                if name in _SHELL and (found := steps(str(given.get("command", "")))):
                    pending[b.get("id")] = (found, inside)
                if not turns:  # the chat's first turn began before anything it wrote
                    turns.append([0, inside])
                if name not in reply._NOT_OWN and not reply._APP.search(name):  # as nh reply-check counts
                    turns[-1][0] += 1
                if not inside:
                    continue
                row["agents"] += name in _AGENT
                row["shell"] += name in _SHELL
                row["push_notifications"] += name.endswith("PushNotification")
                if reply._BROWSER.search(name):
                    row["browser"] += 1
                    row["browser_picking"] += bool(_PICK.search(name))
            continue
        for b in blocks:
            if b.get("type") == "tool_result" and b.get("tool_use_id") in pending:
                settle(*pending.pop(b.get("tool_use_id")), _passed(b), _text(b))
        if reply._ended(d):
            ended = True
            continue
        if d.get("type") == "system" and d.get("subtype") == "compact_boundary":
            row["compactions"] += inside
            continue
        summaries += bool(d.get("isCompactSummary")) and inside
        if said is not None and inside:
            row["owner_messages"] += 1
            for k, p in _KEYS.items():
                row["keywords"][k] += bool(p.search(said))
        if starts:  # a hand-back mid-turn stays in that turn
            turns.append([0, inside])
            ended = False
    for found, was_inside in pending.values():  # no result written: the gate's outcome is unknown
        settle(found, was_inside, False)
    row["compactions"] = max(row["compactions"], summaries)  # older chats leave only the summary
    kept = [n for n, touched in turns if touched]
    if not (row["first"] or kept):
        return None
    row["turns"] = len(kept)
    if kept:
        row["own_calls"] = {"total": sum(kept), "median": statistics.median(kept), "p90": _p90(kept),
                            "max": max(kept)}
    return row


TOTALS = ("owner_messages", "turns", "agents", "browser", "browser_picking", "shell", "compactions", "merges",
          "merges_gated", "merges_unknown", "push_notifications")


def measure(days: int = 7, where: Path | None = None, now: datetime | None = None) -> dict:
    """Every chat changed in the last `days` days, counted; with the totals across them."""
    if days < 1:
        raise KitError("--days must be 1 or more")
    now = now or datetime.now(timezone.utc)
    since, where = now - timedelta(days=days), where or root()
    rows = []
    for path in sorted(where.glob("*/*.jsonl")) if where.is_dir() else []:  # agents' files sit deeper: skipped
        try:
            if datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < since:
                continue
            row = chat(path, since)
        except OSError:
            continue
        if row:
            rows.append(row)
    totals = {k: sum(r[k] for r in rows) for k in TOTALS}
    totals["own_calls"] = sum(r["own_calls"]["total"] for r in rows)
    totals["keywords"] = {k: sum(r["keywords"][k] for r in rows) for k in KEYWORDS}
    roles = {}
    for r in rows:
        roles[r["role"]] = roles.get(r["role"], 0) + 1
    return {"days": days, "since": since.isoformat(timespec="seconds"), "until": now.isoformat(timespec="seconds"),
            "chats": len(rows), "roles": roles, "totals": totals, "rows": rows}


def table(m: dict) -> str:
    head = ["project", "session", "role", "owner msgs", "turns", "calls", "median", "p90", "max", "agents", "browser",
            "picking", "shell", "compactions", "merges", "gated", "unknown", "pushes", "corrections", "status asks",
            "sign-in"]
    lines = [f"# Chats measured: {m['chats']} (last {m['days']} days, since {m['since']})", "",
             "| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for r in m["rows"]:
        c, k = r["own_calls"], r["keywords"]
        cells = [r["project"], r["session"][:8], r["role"], r["owner_messages"], r["turns"], c["total"], c["median"],
                 c["p90"], c["max"], r["agents"], r["browser"], r["browser_picking"], r["shell"], r["compactions"],
                 r["merges"], r["merges_gated"], r["merges_unknown"], r["push_notifications"],
                 k["corrections"], k["status_asks"], k["sign_in_or_click"]]
        lines.append("| " + " | ".join(str(x) for x in cells) + " |")
    t = m["totals"]
    lines += ["", "Totals: " + ", ".join(f"{k.replace('_', ' ')} {t[k]}" for k in ("own_calls", *TOTALS))]
    return "\n".join(lines) + "\n"


def write(m: dict, out: Path) -> Path:
    """measure.json and measure.md in out, which must not be inside a repo (the counts name chats and folders)."""
    out = out.resolve()
    if _in_repo(out):
        raise KitError(f"{core.posix(out)} is inside a git repo; audits stay in a private folder outside every repo")
    out.mkdir(parents=True, exist_ok=True)
    core.atomic_text(out / "measure.json", json.dumps(m, indent=2) + "\n")
    core.atomic_text(out / "measure.md", table(m))
    return out
