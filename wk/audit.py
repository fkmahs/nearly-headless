"""`nh audit measure`: the counting half of the weekly audit (skills/weekly-audit), read-only.

It reads this laptop's Claude Code chats (~/.claude/projects/<folder>/<session>.jsonl; agents' own files under
<session>/subagents/ and sidechain lines are skipped) changed in the last N days, one line at a time, and counts per
chat: turns, the chat's own tool calls per turn, agents, browser use, shell calls, compactions, merges and whether a
fresh gate ran before each, push notifications, and keyword hits in the owner's messages. It writes measure.json and a
short measure.md to a private folder beside the laptop's settings (audits/<date>/), never inside a repo. Only counts
are kept: no message text, ever. Judging the chats is the skill's agents' job.
"""

from __future__ import annotations

import json
import re
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import core, reply
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
_NOT_OWN = ("Agent", "Task", "ToolSearch", "SendMessage")  # handing work off, or loading tools, is not doing it
_APP = re.compile(r"__(?:update_plan|wrap_up)$")
_AGENT = ("Agent", "Task")
_SHELL = ("Bash", "PowerShell")
_PICK = re.compile(r"__(?:list|select|switch)_\w*browsers?$", re.I)  # choosing which browser, not using it
_INJECTED = ("<system-reminder>", "<local-command", "<command-", "Caveat:", "<user-prompt-submit-hook>")
_ROLES = (("project manager", re.compile(r"project[- ]manager", re.I)), ("builder", re.compile(r"\bbuilder\b", re.I)),
          ("supervisor", re.compile(r"\bsupervisor\b", re.I)))
_MERGE = re.compile(r"\bgh\s+pr\s+merge\b([^;&|\n]*)")
_FRESH = re.compile(r"\bgate\s+fresh\b([^;&|\n]*)")
_PR = re.compile(r"--pr[ =]+#?(\d+)")
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


def _typed(d: dict, blocks: list) -> str | None:
    """The owner's own typed words: not a tool result, hook, reminder, slash-command echo or compaction summary."""
    said = reply._owner_said(d, blocks)
    return None if said is None or said.lstrip().startswith(_INJECTED) else said


def _p90(values: list[int]) -> int:
    ordered = sorted(values)
    return ordered[max(0, -(-len(ordered) * 9 // 10) - 1)] if ordered else 0


def chat(path: Path, since: datetime) -> dict | None:
    """One chat's counts from since on; None when nothing in it is that recent."""
    row = {"session": path.stem, "project": path.parent.name, "first": "", "last": "", "role": "other",
           "owner_messages": 0, "turns": 0, "own_calls": {"total": 0, "median": 0, "p90": 0, "max": 0}, "agents": 0,
           "browser": 0, "browser_picking": 0, "shell": 0, "compactions": 0, "merges": 0, "merges_gated": 0,
           "push_notifications": 0, "keywords": {k: 0 for k in KEYWORDS}}
    turns: list[int] = []
    ended, gates, roled, inside, summaries = True, set(), False, False, 0
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
        said = reply._owner_said(d, blocks)
        if said is not None and not roled:  # the chat's first message says what it is, even before the window
            roled, row["role"] = True, next((r for r, p in _ROLES if p.search(said)), "other")
        if d.get("type") == "assistant":
            for b in blocks:
                if b.get("type") != "tool_use":
                    continue
                name = str(b.get("name", ""))
                given = b.get("input") if isinstance(b.get("input"), dict) else {}
                command = str(given.get("command", "")) if name in _SHELL else ""
                merges = [_pr_of(m.group(1), merge=True) for m in _MERGE.finditer(command)]
                if inside:
                    row["merges"] += len(merges)
                    row["merges_gated"] += sum(n in gates or (n is None and bool(gates)) for n in merges)
                gates |= {_pr_of(m.group(1)) for m in _FRESH.finditer(command)} - {None}
                if not inside:
                    continue
                if not turns:  # the turn began before the window: its calls inside it still count
                    turns.append(0)
                if name not in _NOT_OWN and not _APP.search(name):
                    turns[-1] += 1
                row["agents"] += name in _AGENT
                row["shell"] += name in _SHELL
                row["push_notifications"] += name.endswith("PushNotification")
                if reply._BROWSER.search(name):
                    row["browser"] += 1
                    row["browser_picking"] += bool(_PICK.search(name))
            continue
        if not inside:
            continue
        if reply._ended(d):
            ended = True
            continue
        if d.get("type") == "system" and d.get("subtype") == "compact_boundary":
            row["compactions"] += 1
            continue
        summaries += bool(d.get("isCompactSummary"))
        typed = _typed(d, blocks)
        if typed is not None:
            row["owner_messages"] += 1
            for k, p in _KEYS.items():
                row["keywords"][k] += bool(p.search(typed))
        if typed is not None or (reply._other(d, blocks) and ended):  # a hand-back mid-turn stays in that turn
            turns.append(0)
            ended = False
    row["compactions"] = max(row["compactions"], summaries)  # older chats leave only the summary
    if not (row["first"] or turns):
        return None
    row["turns"] = len(turns)
    if turns:
        row["own_calls"] = {"total": sum(turns), "median": int(statistics.median(turns)), "p90": _p90(turns),
                            "max": max(turns)}
    return row


def _pr_of(args: str, merge: bool = False) -> int | None:
    m = _PR.search(args) or (merge and _NUMBER.search(re.sub(r"--match-head-commit[ =]+\S+", "", args)) or None)
    return int(m.group(1)) if m else None


TOTALS = ("owner_messages", "turns", "agents", "browser", "browser_picking", "shell", "compactions", "merges",
          "merges_gated", "push_notifications")


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
            "picking", "shell", "compactions", "merges", "gated", "pushes", "corrections", "status asks", "sign-in"]
    lines = [f"# Chats measured: {m['chats']} (last {m['days']} days, since {m['since']})", "",
             "| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for r in m["rows"]:
        c, k = r["own_calls"], r["keywords"]
        cells = [r["project"], r["session"][:8], r["role"], r["owner_messages"], r["turns"], c["total"], c["median"],
                 c["p90"], c["max"], r["agents"], r["browser"], r["browser_picking"], r["shell"], r["compactions"],
                 r["merges"], r["merges_gated"], r["push_notifications"], k["corrections"], k["status_asks"],
                 k["sign_in_or_click"]]
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
