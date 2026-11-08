"""nh health: is the system getting healthier on tokens? One short table over every Claude Code transcript on this
laptop (~/.claude/projects: chats, and their agents under <session>/subagents/), by period, per day.

Read-only. Message text is never kept or shown: a transcript is read only for API call usage, the app's agent run
notices (their token, tool and time counts, and whether the summary mentions a review), reply-check rejections (a
"Stop hook feedback" line naming something missing), and three commands with their exit status (`nh refresh`,
`gh pr create`'s PR link, `gh pr merge`).

Periods: today so far, yesterday, this week (Monday to today), last week, the previous N full days, in local time.
Every figure is per day; today counts as the part of it gone, so a partial period compares fairly.

Speed: each transcript is read once and kept, by its size and change time, in a cache file in the temp folder.
"""

from __future__ import annotations

import json
import re
import statistics
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

from . import core, usage

CACHE_VERSION = 3
THRESHOLD = 0.10  # a change smaller than this either way is flat

NOTE = re.compile(r"<task-id>(\w+)</task-id>.*?<summary>(.*?)</summary>.*?<subagent_tokens>(\d+)</subagent_tokens>"
                  r"\s*<tool_uses>(\d+)</tool_uses>\s*<duration_ms>(\d+)</duration_ms>", re.S)
# a command's start: the start of the line or of a segment after && || ; | or (, then an optional PowerShell call
# operator, `env` and VAR=value settings; so `grep "gh pr merge" f` or `rg "nh refresh" README.md` never count
START = r"""(?:^|&&|\|\||[;|\n(])\s*(?:&\s*)?(?:env\s+)?(?:[A-Za-z_]\w*=\S*\s+)*"""
WK = r"""(?:(?:"[^"\n]*[\\/]|[^\s"';&|()]*[\\/])?(?:wk|nh)(?:\.exe|\.cmd|\.bat)?"?|(?:py|python3?)(?:\.exe)?(?:\s+-\w+)*\s+-m\s+wk)"""
REFRESH = re.compile(START + WK + r"\s+refresh\b")
MERGE = re.compile(START + r"gh(?:\.exe)?\s+pr\s+merge\b([^;&|\n]*)")
CREATE = re.compile(START + r"gh(?:\.exe)?\s+pr\s+create\b")
MOVED = re.compile(r"(?:^|&&|[;|\n(])\s*(?:cd|pushd|Set-Location|sl)\s", re.I)  # the command ran in another folder
REPO_ARG = re.compile(r"""(?:--repo|-R)[\s=]+["']?(?:https?://github\.com/)?([\w.-]+/[\w.-]+)""")
PULL = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
NUMBER = re.compile(r"(?<![\w=/.-])(\d+)\b")

# (key, label, which way is healthier: "lower", or "" for a count of work done, with no better way)
MEASURES = [
    ("calls", "API calls", "lower"),
    ("input", "fresh input (M)", "lower"),
    ("cache_write", "cache write (M)", "lower"),
    ("cache_read", "cache read (M)", "lower"),
    ("output", "output (M)", "lower"),
    ("chat_p50", "chat context p50 (k)", "lower"),
    ("chat_p90", "chat context p90 (k)", "lower"),
    ("agent_p50", "agent context p50 (k)", "lower"),
    ("agent_p90", "agent context p90 (k)", "lower"),
    ("runs", "agent runs", ""),
    ("wakeups", "wake-ups (% of runs)", "lower"),
    ("run_tokens", "tokens per run, median (k)", "lower"),
    ("run_minutes", "minutes per run, median", "lower"),
    ("merged", "merged PRs", ""),
    ("reviews", "reviews per merged PR", "lower"),
    ("piece_tokens", "agent tokens per merged PR (M)", "lower"),
    ("rejections", "reply-check rejections", "lower"),
    ("refresh", "nh refresh uses", ""),
    ("refused", "nh refresh refusals", "lower"),
]


def cache_file() -> Path:
    return Path(tempfile.gettempdir()) / "nearly-headless" / "health-cache.json"


def _root(cwd: str) -> str:
    """The repo a folder belongs to: a builder's worktree counts as its repo."""
    return re.split(r"/\.claude/worktrees/", str(cwd or "").replace("\\", "/").lower())[0].rstrip("/")


def _notices(e: dict) -> list[str]:
    """The app's agent run notices in a record: a queued, attached or delivered <task-notification>, never a message
    or a tool's output that only quotes one."""
    kind = e.get("type")
    if kind == "queue-operation":
        found = [e.get("content")]
    elif kind == "attachment":
        a = e.get("attachment") if isinstance(e.get("attachment"), dict) else {}
        found = [v for v in a.values() if isinstance(v, str)]
    elif kind in ("user", "api-request-blob"):
        c = e.get("message").get("content") if isinstance(e.get("message"), dict) else None
        found = [c] if isinstance(c, str) else [b.get("text") for b in c or []
                                                if isinstance(b, dict) and b.get("type") == "text"]
    else:
        return []
    return [t for t in found if isinstance(t, str) and t.lstrip().startswith("<task-notification>")]


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return " ".join(str(b.get("text") or "") for b in content or [] if isinstance(b, dict))


# ---------------------------------------------------------------- reading one transcript

def parse(path: Path) -> dict:
    """What one transcript holds for nh health; nothing of its text but the markers in the module's note."""
    calls, notes, rejects, refresh, merges, creates, pending = {}, [], {}, {}, {}, [], {}
    with open(path, "rb") as handle:
        for raw in handle:
            wanted = (b"subagent_tokens" in raw or b'"usage"' in raw or b"Stop hook feedback" in raw or b"gh pr " in raw or b"refresh" in raw
                      or (pending and b'"tool_result"' in raw))
            if not wanted:
                continue
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(e, dict):
                continue
            m = e.get("message") if isinstance(e.get("message"), dict) else {}
            stamp, u = str(e.get("timestamp") or ""), m.get("usage")
            for said in _notices(e):
                for tid, summary, tok, tools, ms in NOTE.findall(said):
                    notes.append([tid, int(tok), int(tools), int(ms), stamp, "review" in summary.lower()])
            key = e.get("requestId") or m.get("id")
            if isinstance(u, dict) and key and stamp:
                row = [stamp, int(u.get("input_tokens") or 0), int(u.get("cache_creation_input_tokens") or 0),
                       int(u.get("cache_read_input_tokens") or 0), int(u.get("output_tokens") or 0)]
                seen = calls.get(key)
                calls[key] = row if seen is None else seen[:1] + [max(a, b) for a, b in zip(seen[1:], row[1:])]
            content = m.get("content")
            if e.get("type") == "user" and "Stop hook feedback" in (said := _text(content)) and "missing" in said:
                rejects[str(e.get("uuid") or stamp)] = stamp
            if not isinstance(content, list) or e.get("type") not in ("assistant", "user"):
                continue  # an "api-request-blob" repeats the chat's history, without times
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use" and b.get("name") in ("Bash", "PowerShell"):
                    cmd = str((b.get("input") or {}).get("command") or "")
                    kinds = [k for k, rx in (("refresh", REFRESH), ("merge", MERGE), ("create", CREATE))
                             if rx.search(cmd)]
                    if kinds:
                        folder = "" if MOVED.search(cmd) else _root(e.get("cwd"))
                        pending[str(b.get("id"))] = (kinds, cmd, stamp, folder)
                    if "refresh" in kinds:  # a use counts even before (or without) its result
                        refresh[str(b.get("id"))] = [stamp, False]
                elif b.get("type") == "tool_result" and str(b.get("tool_use_id")) in pending:
                    tid = str(b["tool_use_id"])
                    kinds, cmd, when, root = pending.pop(tid)
                    failed, result = bool(b.get("is_error")), _text(b.get("content"))
                    if "refresh" in kinds:  # exit 1 is wk's refusal; 2 is an error
                        refresh[tid] = [when, failed and result.lstrip().startswith("Exit code 1")]
                    if "merge" in kinds and not failed:
                        args = MERGE.search(cmd).group(1)
                        url, given = PULL.search(args), REPO_ARG.search(args)
                        n = int(url.group(2)) if url else int(x.group(1)) if (x := NUMBER.search(args)) else 0
                        repo = (url.group(1) if url else given.group(1) if given else "").lower()
                        merges[tid] = [when, root, repo, n]
                    if "create" in kinds and not failed and (url := PULL.search(result)):
                        creates.append([root, url.group(1).lower(), int(url.group(2))])
    return {"calls": calls, "notes": notes, "rejects": rejects, "refresh": refresh, "merges": merges,
            "creates": creates}


class Records(usage.Records):
    """Parsed transcripts, kept by size and change time (the token page's reader, keeping nh health's facts)."""
    version = CACHE_VERSION
    parse = staticmethod(parse)


# ---------------------------------------------------------------- all transcripts together

def gather(root: Path, cache: Path | None = None) -> dict:
    """Every transcript's facts as one set, each counted once: a call, rejection or command copied into a resumed or
    forked chat by its id, an agent run notice by its agent, tokens and time."""
    paths = usage.transcripts(root)
    parsed = Records(cache or cache_file()).read(paths)
    calls, notes, rejects, refresh, merges, built, repos = {}, {}, {}, {}, {}, {}, {}
    for p in paths:
        data = parsed.get(core.posix(p))
        if not data:
            continue
        sid, agent = usage.where(p)
        for key, row in data["calls"].items():
            seen = calls.get(key)
            calls[key] = row + [agent] if seen is None else \
                seen[:1] + [max(a, b) for a, b in zip(seen[1:5], row[1:])] + seen[5:]
        for tid, tok, tools, ms, stamp, review in data["notes"]:
            k = (tid, tok, ms)
            if k not in notes or (stamp and (not notes[k][3] or stamp < notes[k][3])):
                notes[k] = [tid, tok, ms, stamp, review]
        rejects.update(data["rejects"])
        for k, (when, refused) in data["refresh"].items():
            had = refresh.get(k) or ["", False]
            refresh[k] = [had[0] or when, had[1] or refused]
        merges.update(data["merges"])
        for folder, repo, n in data["creates"]:
            if folder:
                repos.setdefault(folder, set()).add(repo)
            if agent and p.stem.startswith("agent-"):
                built.setdefault((repo, n), set()).add(p.stem[len("agent-"):])
    merged = {}
    for when, folder, repo, n in sorted(merges.values()):
        # the repo a merge named, else the one PRs made from its folder go to; can't tell: counted, never tied
        known = repos.get(folder, set()) if folder else set()
        repo = repo or (next(iter(known)) if len(known) == 1 else "")
        merged.setdefault((repo, n) if repo and n else ("?", folder, n, when), when)
    return {"calls": list(calls.values()), "notes": [v for v in notes.values() if v[3]], "rejects": list(rejects.values()),
            "refresh": list(refresh.values()), "merged": merged, "built": built}


def tags(top: Path | None = None) -> list[tuple[datetime, str]]:
    """The kit's releases (vX.Y.Z tags) and when each was made, oldest first; none when this isn't a git checkout."""
    top = top or Path(__file__).resolve().parents[1]
    try:
        out = core.git(top, "tag", "-l", "v*", "--format=%(refname:short)|%(creatordate:iso-strict)", check=False)
    except (OSError, core.KitError):
        return []
    found = []
    for line in out.splitlines():
        name, _, when = line.partition("|")
        if re.fullmatch(r"v\d+\.\d+\.\d+", name):
            try:
                found.append((datetime.fromisoformat(when), name[1:]))
            except ValueError:
                continue
    return sorted(found)


# ---------------------------------------------------------------- the measures

def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def periods(now: datetime, days: int = 14) -> list[dict]:
    """The periods shown, each with its first and last day and how many days it counts as; `before` is the period a
    verdict compares it with."""
    today = now.date()
    gone = (now - now.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds() / 86400
    gone = max(gone, 1 / 1440)
    monday = today - timedelta(days=today.weekday())
    one = timedelta(days=1)
    return [
        {"name": "today", "first": today, "last": today, "count": gone, "before": "yesterday"},
        {"name": "yesterday", "first": today - one, "last": today - one, "count": 1, "before": None},
        {"name": "this week", "first": monday, "last": today, "count": (today - monday).days + gone,
         "before": "last week"},
        {"name": "last week", "first": monday - 7 * one, "last": monday - one, "count": 7, "before": None},
        {"name": f"{days} days", "first": today - days * one, "last": today - one, "count": days,
         "before": f"{days} days before"},
        {"name": f"{days} days before", "first": today - 2 * days * one, "last": today - (days + 1) * one,
         "count": days, "before": None, "hidden": True},
    ]


def _p90(values: list[int]) -> int:
    v = sorted(values)
    return v[int(len(v) * 0.9)]


def measure(facts: dict, first: date, last: date, count: float, day) -> dict:
    """Every measure for the days first..last (`day` turns a time stamp into a local date); None where there is
    nothing to measure."""
    inside = lambda ts: bool(ts) and first <= day(ts) <= last  # noqa: E731
    rows = [r for r in facts["calls"] if inside(r[0])]
    out = {"calls": len(rows) / count}
    for i, k in enumerate(("input", "cache_write", "cache_read", "output"), 1):
        out[k] = sum(r[i] for r in rows) / count / 1e6
    for who, agent in (("chat", False), ("agent", True)):
        ctx = [r[1] + r[2] + r[3] for r in rows if r[5] == agent]
        out[f"{who}_p50"] = statistics.median(ctx) / 1e3 if ctx else None
        out[f"{who}_p90"] = _p90(ctx) / 1e3 if ctx else None
    runs = [n for n in facts["notes"] if inside(n[3])]
    out["runs"] = len(runs) / count
    out["wakeups"] = 100 * sum(n[5] for n in runs) / len(runs) if runs else None
    out["run_tokens"] = statistics.median(n[1] for n in runs) / 1e3 if runs else None
    out["run_minutes"] = statistics.median(n[2] for n in runs) / 60000 if runs else None
    merged = [k for k, when in facts["merged"].items() if inside(when)]
    out["merged"] = len(merged) / count
    out["reviews"] = sum(n[4] for n in runs) / len(merged) if merged else None
    tokens = facts["agent_tokens"]
    tied = [sum(tokens.get(a, 0) for a in facts["built"][k]) for k in merged
            if k in facts["built"] and any(a in tokens for a in facts["built"][k])]
    out["piece_tokens"] = sum(tied) / len(tied) / 1e6 if tied else None
    out["tied"] = f"{len(tied)} of {len(merged)}"
    out["rejections"] = sum(1 for ts in facts["rejects"] if inside(ts)) / count
    out["refresh"] = sum(1 for ts, _ in facts["refresh"] if inside(ts)) / count
    out["refused"] = sum(1 for ts, no in facts["refresh"] if no and inside(ts)) / count
    return out


def verdict(now_value, before, way: str) -> str:
    """healthier / worse / flat for a measure that reads `way` ("lower" or "higher" is healthier); "-" for a count with
    no better way, "unknown" when either side has nothing to measure."""
    if not way:
        return "-"
    if now_value is None or before is None:
        return "unknown"
    if before == 0:
        change = 0 if now_value == 0 else 1
    else:
        change = (now_value - before) / before
    if abs(change) < THRESHOLD:
        return "flat"
    return "healthier" if (change < 0) == (way == "lower") else "worse"


def _versions(released: list[tuple[datetime, str]], first: date, last: date, day) -> list[str]:
    """The versions live on any of these days: the one live at the start and every one released during them."""
    live, during = None, []
    for when, name in released:
        d = day(when)
        if d < first:
            live = name
        elif d <= last:
            during.append(name)
    return ([live] if live else []) + during


def health(root: Path | None = None, now: datetime | None = None, days: int = 14, cache: Path | None = None,
           released: list[tuple[datetime, str]] | None = None, tz=None) -> dict:
    """Every period's measures, the versions live in it, and each measure's verdict against the period before."""
    now = (now or datetime.now().astimezone()).astimezone(tz)
    facts = gather(root or usage.projects(), cache)
    released = tags() if released is None else released
    memo: dict[str, date] = {}

    def day(ts) -> date:
        if isinstance(ts, datetime):
            return ts.astimezone(tz).date()
        k = ts[:16]
        if k not in memo:
            memo[k] = datetime.fromisoformat(k.replace("Z", "") + ":00+00:00").astimezone(tz).date()
        return memo[k]

    ordered = sorted(facts["notes"], key=lambda n: n[3])
    seen = set()
    for n in ordered:  # a notice after an agent's first is a wake-up
        n.append(n[0] in seen)
        seen.add(n[0])
    facts["agent_tokens"] = {}
    for n in ordered:
        facts["agent_tokens"][n[0]] = facts["agent_tokens"].get(n[0], 0) + n[1]
    out = {"now": now.isoformat(timespec="minutes"), "days": days, "threshold": THRESHOLD, "periods": []}
    by = {}
    for p in periods(now, days):
        m = measure(facts, p["first"], p["last"], p["count"], day)
        entry = {"name": p["name"], "first": p["first"].isoformat(), "last": p["last"].isoformat(),
                 "days": round(p["count"], 3), "versions": _versions(released, p["first"], p["last"], day),
                 "measures": m, "hidden": bool(p.get("hidden"))}
        by[p["name"]] = entry
        out["periods"].append(entry)
    for p, entry in zip(periods(now, days), out["periods"]):
        if p["before"]:
            entry["against"] = p["before"]
            entry["verdicts"] = {k: verdict(entry["measures"][k], by[p["before"]]["measures"][k], way)
                                 for k, _, way in MEASURES}
    out["version_by_day"] = {d.isoformat(): (_versions(released, d, d, day) or ["unknown"])[-1]
                             for d in _days(now.date() - timedelta(days=days), now.date())}
    return out


# ---------------------------------------------------------------- the table

def _num(v, key: str) -> str:
    if v is None:
        return "unknown" if key == "piece_tokens" else "-"
    if key == "wakeups":
        return f"{v:.0f}%"
    a = abs(v)
    return f"{v:.0f}" if a >= 100 else f"{v:.1f}" if a >= 10 else f"{v:.2f}" if a >= 0.01 or a == 0 else f"{v:.3f}"


def _span(versions: list[str]) -> str:
    if not versions:
        return "unknown"
    return versions[0] if len(versions) == 1 else f"{versions[0]}-{versions[-1]}"


def text(h: dict) -> str:
    shown = [p for p in h["periods"] if not p["hidden"]]
    week = next(p for p in shown if p["name"] == "this week")
    rows = [["per day"] + [p["name"] for p in shown] + ["healthier", "this vs last week"],
            ["nearly-headless version"] + [_span(p["versions"]) for p in shown] + ["", ""]]
    for key, label, way in MEASURES:
        rows.append([label] + [_num(p["measures"][key], key) for p in shown]
                    + [way or "-", week["verdicts"][key]])
        if key == "piece_tokens":
            rows.append(["  merged PRs tied to a builder"] + [p["measures"]["tied"] for p in shown] + ["", ""])
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    lines = [f"nearly-headless health at {h['now'].replace('T', ' ')[:16]}: every figure is per day; today counts as "
             f"{shown[0]['days']:.2f} of a day."]
    lines += ["  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows]
    worse = [label for key, label, _ in MEASURES if week["verdicts"][key] == "worse"]
    lines.append(f"Verdict: this week against last week, flat within +/-{h['threshold']:.0%}.")
    lines.append("Worse: " + ("; ".join(worse) if worse else "nothing."))
    return "\n".join(lines)
