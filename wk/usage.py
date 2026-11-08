"""What Claude Code's own records on this laptop say about token use: each chat's transcript
(~/.claude/projects/<folder>/<session>.jsonl, its subagents under <session>/subagents/) logs every API call's usage.

Two things are read from them:
- the memory flag: a list's supervisor chat, or one of its builders, whose context (input + cache read + cache write of
  one call) went past the compact window set in Claude's settings (`autoCompactWindow`) without compacting first. Only
  the last FLAG_DAYS count, so an old breach stops showing on its own.
- the token and cost page: input, cache read, cache write and output tokens per week (Monday to Sunday) and model, by
  role (supervisor chats, their builders, everything else) and by repo, with what they would cost at Anthropic's API
  list prices (PRICES). The owner is on a subscription, so that is a yardstick, not a bill.

Speed: each transcript is parsed once and kept, by its size and change time, in one cache file beside this laptop's
settings; only a file that changed is read again. A call copied into another transcript (a resumed or forked chat)
is counted once.
"""

from __future__ import annotations

import html
import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import core, settings, upkeep, work
from .core import WorkkitError

FLAG_DAYS = 2      # a breach older than this no longer shows
CACHE_VERSION = 1

# $ per million tokens at Anthropic's API list prices, from the claude-api skill's pricing: input, output, cache read.
# Cache writes cost 1.25x input (kept 5 minutes) or 2x input (kept 1 hour); fast mode costs twice the standard rates.
# A model missing here shows "no price" rather than a guess.
PRICES = {
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    "claude-mythos-5-1": (10.0, 50.0, 0.25),
    "claude-fable-5": (10.0, 50.0, 1.0),
    "claude-mythos-5": (10.0, 50.0, 1.0),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-opus-4-8": (5.0, 25.0, 0.50),
    "claude-opus-4-7": (5.0, 25.0, 0.50),  # the Opus 4.x list price (the skill gives it for 4.8; 4.7 is the same)
    "claude-opus-4-6": (5.0, 25.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-sonnet-4-6": (3.0, 15.0, 0.30),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-haiku-4-5": (1.0, 5.0, 0.10),
}
LONG = {"claude-haiku-5-5": (100_000, (0.50, 2.50, 0.05))}  # a prompt over this many tokens is billed at these rates
ROLES = ("supervisor chats", "builders", "other")

# a row per API call: when, model, input, cache read, cache write (5 min), cache write (1 h), output, fast (0/1)
TS, MODEL, IN, CR, W5, W1, OUT, FAST = range(8)


# ---------------------------------------------------------------- where things are

def projects() -> Path:
    return upkeep.claude_dir() / "projects"


def window() -> int | None:
    """The compact window in Claude's settings, or None when it isn't set (then there is no limit to check)."""
    try:
        value = core.read_json(upkeep.claude_dir() / "settings.json").get(upkeep.WINDOW)
    except (WorkkitError, AttributeError):
        return None
    return value if isinstance(value, int) and value > 0 else None


def app_sessions() -> list[Path]:
    """Where the Claude desktop app keeps its chats' records (each names the Claude Code session it runs)."""
    places = [Path(os.environ["APPDATA"]) / "Claude"] if os.environ.get("APPDATA") else []
    places += [Path.home() / "Library" / "Application Support" / "Claude", Path.home() / ".config" / "Claude"]
    return [p / "claude-code-sessions" for p in places if (p / "claude-code-sessions").is_dir()]


UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def sessions_of(link: str, apps: list[Path] | None = None) -> set[str]:
    """The Claude Code session ids a supervisor link stands for: a desktop chat's link (local_<id>) names the app's
    chat, whose record names its session; any id in the link may also be the session itself."""
    found = set()
    for uid in UUID.findall(link or ""):
        found.add(uid.lower())
        for place in app_sessions() if apps is None else apps:
            for rec in place.glob(f"*/*/local_{uid}.json"):
                try:
                    cli = core.read_json(rec).get("cliSessionId")
                except (WorkkitError, AttributeError):
                    continue
                if isinstance(cli, str) and cli:
                    found.add(cli.lower())
    return found


def supervisors(s: dict, apps: list[Path] | None = None) -> dict[str, dict]:
    """Every list's supervisor chat on this laptop, by Claude Code session id: its repo, list and title."""
    out = {}
    for name in s.get("repos") or {}:
        try:
            products = work.Repo(s, name).products()
        except WorkkitError:
            continue
        for product, fields in products.items():
            for sid in sessions_of(str(fields.get("supervisor") or ""), apps):
                out[sid] = {"repo": name, "product": product, "title": fields.get("title") or product}
    return out


def where(path: Path) -> tuple[str, bool]:
    """A transcript's session id, and whether it is a subagent's (a builder's, under a supervisor)."""
    if path.parent.name == "subagents":
        return path.parent.parent.name.lower(), True
    return path.stem.lower(), False


def transcripts(root: Path, since: float = 0.0) -> list[Path]:
    """Every transcript changed since `since` (a time stamp): chats and their subagents."""
    if not root.is_dir():
        return []
    found = list(root.glob("*/*.jsonl")) + list(root.glob("*/*/subagents/*.jsonl"))
    out = []
    for p in found:
        try:
            if p.stat().st_mtime >= since:
                out.append(p)
        except OSError:
            continue
    return out


# ---------------------------------------------------------------- reading one transcript

def parse(path: Path) -> dict:
    """One transcript's API calls (by message id, so a call logged once per content block counts once), its
    compactions, and the folder it ran in."""
    rows, compacts, cwd = {}, [], ""
    with open(path, "rb") as handle:
        for raw in handle:
            if b'"compact_boundary"' in raw:
                try:
                    d = json.loads(raw)
                except ValueError:
                    continue
                if d.get("subtype") == "compact_boundary" and d.get("timestamp"):
                    compacts.append(d["timestamp"])
                continue
            if b'"assistant"' not in raw or b'"usage"' not in raw:
                continue
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            m = d.get("message") if d.get("type") == "assistant" else None
            if not isinstance(m, dict) or not isinstance(m.get("usage"), dict):
                continue
            model, u = str(m.get("model") or ""), m["usage"]
            if not model or model.startswith("<"):  # "<synthetic>": no API call behind it
                continue
            cwd = cwd or str(d.get("cwd") or "")
            written = int(u.get("cache_creation_input_tokens") or 0)
            split = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
            hour = int(split.get("ephemeral_1h_input_tokens") or 0)
            row = [str(d.get("timestamp") or ""), model, int(u.get("input_tokens") or 0),
                   int(u.get("cache_read_input_tokens") or 0), written - min(hour, written), min(hour, written),
                   int(u.get("output_tokens") or 0), int(u.get("speed") == "fast")]
            key = str(m.get("id") or d.get("requestId") or d.get("uuid"))
            seen = rows.get(key)
            rows[key] = row if seen is None else seen[:IN] + [max(a, b) for a, b in zip(seen[IN:], row[IN:])]
    return {"rows": rows, "compacts": compacts, "cwd": cwd}


class Records:
    """Parsed transcripts, kept in memory and in a cache file, read again only when a file's size or time changed."""

    _shared: dict[str, "Records"] = {}
    _lock = threading.Lock()
    version = CACHE_VERSION
    parse = None  # what is kept of one transcript: this module's parse; wk health keeps other things (health.Records)

    def __init__(self, cache: Path):
        self.cache, self.files, self.lock = cache, {}, threading.Lock()
        try:
            data = core.read_json(cache) if cache.is_file() else {}
            if data.get("version") == self.version:
                self.files = data.get("files") or {}
        except (WorkkitError, AttributeError):
            self.files = {}

    @classmethod
    def at(cls, cache: Path | None = None) -> "Records":
        """One per cache file in this process, so a command center keeps its parsed files between visits."""
        cache = cache or settings.path().parent / "usage-cache.json"
        with cls._lock:
            if str(cache) not in cls._shared:
                cls._shared[str(cache)] = cls(cache)
            return cls._shared[str(cache)]

    def read(self, paths: list[Path]) -> dict[str, dict]:
        with self.lock:
            out, changed = {}, False
            for p in paths:
                try:
                    st = p.stat()
                except OSError:
                    continue
                key, stamp = core.posix(p), [st.st_mtime_ns, st.st_size]
                kept = self.files.get(key)
                if not kept or kept.get("stamp") != stamp:
                    try:
                        kept = {"stamp": stamp, "data": (self.parse or parse)(p)}
                    except OSError:
                        continue
                    self.files[key], changed = kept, True
                out[key] = kept["data"]
            if changed:
                self.files = {k: v for k, v in self.files.items() if k in out or Path(k).is_file()}  # Claude cleans up
                try:
                    core.atomic_text(self.cache, json.dumps({"version": self.version, "files": self.files},
                                                            separators=(",", ":")))
                except OSError:
                    pass  # the page still shows; the next run parses again
            return out


# ---------------------------------------------------------------- the memory flag

def _at(ts: str) -> datetime | None:
    try:
        at = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def context(row: list) -> int:
    return row[IN] + row[CR] + row[W5] + row[W1]


def breaches(data: dict, limit: int) -> list[dict]:
    """Each stretch of calls whose context went past `limit`, ended by a compaction or a call back under it: when it
    started, its last call, and the largest context in it."""
    events = sorted([(r[TS], 1, context(r)) for r in data["rows"].values()] + [(c, 0, 0) for c in data["compacts"]])
    found, cur = [], None
    for ts, is_call, size in events:
        if is_call and size > limit:
            if cur is None:
                cur = {"start": ts, "last": ts, "peak": size}
                found.append(cur)
            cur["last"], cur["peak"] = ts, max(cur["peak"], size)
        else:
            cur = None
    return found


def _label(path: Path, is_sub: bool) -> str:
    if not is_sub:
        return "supervisor chat"
    try:
        what = str(core.read_json(path.with_suffix(".meta.json")).get("description") or "")
    except (WorkkitError, AttributeError):
        what = ""
    return f"builder \"{what}\"" if what else f"builder {path.stem}"


def memory(s: dict, repo: str, product: str, link: str, now: datetime | None = None, limit: int | None = None,
           root: Path | None = None, apps: list[Path] | None = None, records: Records | None = None) -> list[dict]:
    """A list's supervisor chat and its builders that went past the compact window in the last FLAG_DAYS: one entry
    each (the chat in plain words, how far it went, when), newest breach first."""
    limit = limit or window()
    sids = sessions_of(link, apps)
    if not limit or not sids:
        return []
    now, root = now or datetime.now(timezone.utc), root or projects()
    since = now - timedelta(days=FLAG_DAYS)
    paths = []
    for sid in sids:
        paths += list(root.glob(f"*/{sid}.jsonl")) + list(root.glob(f"*/{sid}/subagents/*.jsonl"))
    paths = [p for p in paths if p.is_file() and datetime.fromtimestamp(p.stat().st_mtime, timezone.utc) >= since]
    if not paths:
        return []
    parsed = (records or Records.at()).read(paths)
    found = []
    for p in paths:
        recent = [b for b in breaches(parsed.get(core.posix(p)) or {"rows": {}, "compacts": []}, limit)
                  if (_at(b["last"]) or since) > since]
        if recent:
            worst = max(recent, key=lambda b: b["peak"])
            last = _at(recent[-1]["last"]).astimezone()
            found.append({"chat": _label(p, where(p)[1]), "peak": worst["peak"], "limit": limit, "times": len(recent),
                          "last": last, "when": f"{last.day} {last:%b %H:%M}"})
    return sorted(found, key=lambda x: x["last"], reverse=True)


# ---------------------------------------------------------------- cost

def _model(name: str) -> str:
    return re.sub(r"-\d{8}$", "", re.sub(r"\[.*\]$", "", name.strip().lower()))


def costs(row: list) -> dict[str, float] | None:
    """What one call would cost at API list prices, in dollars, by token type (input, cache read, cache write, output);
    None for a model without a price."""
    model = _model(row[MODEL])
    rates = PRICES.get(model)
    if rates is None:
        return None
    long = LONG.get(model)
    if long and context(row) > long[0]:
        rates = long[1]
    inp, out, read = rates
    times = (2 if row[FAST] else 1) / 1e6
    return {"input": row[IN] * inp * times, "cache_read": row[CR] * read * times,
            "cache_write": (row[W5] * inp * 1.25 + row[W1] * inp * 2) * times, "output": row[OUT] * out * times}


def cost(row: list) -> float | None:
    """What one call would cost at API list prices, in dollars; None for a model without a price."""
    split = costs(row)
    return None if split is None else sum(split.values())


# ---------------------------------------------------------------- the page

def _monday(day) -> datetime:
    return datetime(day.year, day.month, day.day).astimezone() - timedelta(days=day.weekday())


def _repo_of(cwd: str, repos: dict[str, str]) -> str:
    here = core.posix(cwd).lower().rstrip("/") + "/"
    for name, top in sorted(repos.items(), key=lambda kv: -len(kv[1])):
        if here.startswith(top):
            return name
    return "other"


def _cell() -> dict:
    return {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0, "cost": 0.0, "priced": True, "calls": 0}


def report(s: dict, weeks: int = 2, now: datetime | None = None, root: Path | None = None,
           apps: list[Path] | None = None, records: Records | None = None) -> dict:
    """Token use and cost per week (Monday to Sunday, this laptop's time) for the last `weeks` weeks, this one
    included: by model, by role, by repo and by token type (each type's tokens at its own price)."""
    now = (now or datetime.now(timezone.utc)).astimezone()
    start = _monday(now.date()) - timedelta(weeks=max(1, weeks) - 1)
    root = root or projects()
    paths = transcripts(root, start.timestamp())
    parsed = (records or Records.at()).read(paths)
    sups = supervisors(s, apps)
    repos = {name: core.posix(info.get("path", "")).lower().rstrip("/") + "/"
             for name, info in (s.get("repos") or {}).items() if info.get("path")}
    # a call copied into another transcript (a resumed or forked chat) counts once, for the transcript that has it first
    # (the earliest call; on a tie, the file made first)
    def first(p: Path):
        rows = (parsed.get(core.posix(p)) or {"rows": {}})["rows"].values()
        try:
            made = p.stat().st_ctime
        except OSError:
            made = 0.0
        return min((r[TS] for r in rows), default="~"), made, core.posix(p)
    order = sorted(paths, key=first)
    seen, tables = set(), {"model": {}, "role": {}, "repo": {}, "type": {}}
    unpriced = set()
    # last week up to this moment of the week, so a week still running compares like with like
    last_week, same_point = (_monday(now.date()) - timedelta(weeks=1)).date().isoformat(), now - timedelta(weeks=1)
    to_date = {"cost": 0.0, "priced": True}
    for p in order:
        data = parsed.get(core.posix(p))
        if not data:
            continue
        sid, is_sub = where(p)
        sup = sups.get(sid)
        role = ROLES[1] if sup and is_sub else ROLES[0] if sup else ROLES[2]
        repo = sup["repo"] if sup else _repo_of(data.get("cwd") or "", repos)
        for key, r in data["rows"].items():
            at = _at(r[TS])
            if key in seen or at is None or at < start:
                continue
            seen.add(key)
            week = _monday(at.astimezone().date()).date().isoformat()
            split = costs(r)
            dollars = None if split is None else sum(split.values())
            if dollars is None:
                unpriced.add(_model(r[MODEL]))
            if week == last_week and at <= same_point:
                to_date["cost"] += dollars or 0.0
                to_date["priced"] = to_date["priced"] and dollars is not None
            for table, k in (("model", _model(r[MODEL])), ("role", role), ("repo", repo)):
                cell = tables[table].setdefault((week, k), _cell())
                cell["input"] += r[IN]
                cell["cache_read"] += r[CR]
                cell["cache_write"] += r[W5] + r[W1]
                cell["output"] += r[OUT]
                cell["calls"] += 1
                if dollars is None:
                    cell["priced"] = False
                else:
                    cell["cost"] += dollars
            for k, n in (("input", r[IN]), ("cache_read", r[CR]), ("cache_write", r[W5] + r[W1]), ("output", r[OUT])):
                cell = tables["type"].setdefault((week, k), _cell())
                cell[k] += n
                cell["calls"] += 1
                if split is None:
                    cell["priced"] = cell["priced"] and not n  # no tokens of this type: nothing left unpriced
                else:
                    cell["cost"] += split[k]
    week_list = [(start + timedelta(weeks=i)).date().isoformat() for i in range(max(1, weeks))]
    days_in = (now.date() - _monday(now.date()).date()).days + 1
    return {"weeks": week_list, "this_week_days": days_in, "unpriced": sorted(unpriced),
            "last_week_to_date": to_date if last_week in week_list else None,
            "tables": {t: [{"week": w, "key": k} | v for (w, k), v in sorted(cells.items())]
                       for t, cells in tables.items()},
            "files": len(paths), "at": now.strftime("%Y-%m-%d %H:%M")}


def _tokens(n: int) -> str:
    return f"{n / 1e9:.2f}B" if n >= 1e9 else f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}K" if n >= 1e3 else str(n)


def _dollars(cell: dict) -> str:
    return f"${cell['cost']:,.2f}" + ("" if cell["priced"] else " + no price") if cell["cost"] or cell["priced"] \
        else "no price"


def _change(new: float, old: float) -> str:
    if not old:
        return "new" if new else "–"
    return f"{(new - old) / old * 100:+.0f}%"


COLS = ("input", "cache_read", "cache_write", "output")


def _total(cells: list[dict]) -> dict:
    out = {c: sum(x[c] for x in cells) for c in COLS} | {"cost": sum(x["cost"] for x in cells)}
    return out | {"priced": all(x["priced"] for x in cells)}


def _table(rows: list[dict], weeks: list[str], what: str, label) -> str:
    head = f"<tr><th>Week</th><th>{what}</th>" + "".join(
        f"<th class='num'>{h}</th>" for h in ("Input", "Cache read", "Cache write", "Output", "Cost at API prices")) + "</tr>"
    body = []
    for w in weeks:
        cells = sorted((r for r in rows if r["week"] == w), key=lambda r: -r["cost"])
        if not cells:
            body.append(f"<tr><td>{label(w)}</td><td colspan='6' class='muted'>nothing logged</td></tr>")
            continue
        for i, r in enumerate(cells):
            body.append(f"<tr><td>{label(w) if i == 0 else ''}</td><td>{html.escape(r['key'])}</td>" + "".join(
                f"<td class='num'>{_tokens(r[c])}</td>" for c in COLS) + f"<td class='num'>{_dollars(r)}</td></tr>")
        if len(cells) > 1:
            t = _total(cells)
            body.append("<tr class='sum'><td></td><td>All</td>" + "".join(f"<td class='num'>{_tokens(t[c])}</td>"
                                                                          for c in COLS)
                        + f"<td class='num'>{_dollars(t)}</td></tr>")
    return f"<div class='wrap'><table>{head}{''.join(body)}</table></div>"


ROLE_NAMES = {"supervisor chats": "Supervisors", "builders": "Builders", "other": "Other chats"}


TYPE_NAMES = {"input": "Input", "output": "Output", "cache_read": "Cache read", "cache_write": "Cache write"}


def _shares(rows: list[dict], weeks: list[str], head, names: dict | None = None) -> str:
    """One column per week, oldest first: each key's cost that week as a share of the week's cost (a bar, the share
    and the cost), headed by `head(week, total)`. The names sit once on the left, biggest over the weeks shown first,
    so a row reads straight across."""
    cells = {(r["week"], r["key"]): r for r in rows if r["week"] in weeks}
    if not cells:
        return "<p class='muted'>Nothing logged.</p>"
    keys = sorted({k for _, k in cells}, key=lambda k: (-sum(cells[(w, k)]["cost"] for w in weeks if (w, k) in cells), k))
    totals = {w: _total([c for (cw, _), c in cells.items() if cw == w] or [_cell()]) for w in weeks}
    out = ["<tr><th class='name'></th>" + "".join(f"<th class='week'>{head(w, totals[w])}</th>" for w in weeks) + "</tr>"]
    for k in keys:
        row = [f"<th class='name'>{html.escape((names or {}).get(k, k))}</th>"]
        for w in weeks:
            c, total = cells.get((w, k)), totals[w]["cost"]
            if c is None:
                row.append("<td><div class='cell'><span class='bar'></span><span class='pct'>–</span>"
                           "<span class='usd muted'>–</span></div></td>")
                continue
            share = c["cost"] / total * 100 if total else 0
            row.append(f"<td><div class='cell'><span class='bar'><i style='width:{share:.1f}%'></i></span>"
                       f"<span class='pct'>{share:.0f}%</span><span class='usd'>{_dollars(c)}</span></div></td>")
        out.append("<tr>" + "".join(row) + "</tr>")
    return (f"<div class='weeks'><table class='shares' style='--n:{len(weeks)}'><colgroup><col class='namec'>"
            f"<col span='{len(weeks)}'></colgroup>{''.join(out)}</table></div>")


def page(view: dict) -> str:
    """The command center's Tokens & cost tab, drawn on the server (page.html styles it): cost first (this week
    against last), then where it goes by role, repo, model and token type, one column per week as shares of that
    week's cost, with the token counts in one closed details section. The weeks picker stays in the tab (#usage/N)."""
    weeks, days = view["weeks"], view["this_week_days"]

    def label(w):
        d = datetime.fromisoformat(w)
        text = f"{d.day} {d:%b} – {(d + timedelta(days=6)).day} {d + timedelta(days=6):%b}"
        return text + (f" (so far, {days} of 7 days)" if w == weeks[-1] and days < 7 else "")

    def head(w, cell):  # a week column's heading: its dates, then its cost, "so far" while the week runs
        d = datetime.fromisoformat(w)
        text = f"{d.day} {d:%b} – {(d + timedelta(days=6)).day} {d + timedelta(days=6):%b}"
        now = " so far" if w == weeks[-1] and days < 7 else ""
        return f"{text}<b>{_dollars(cell)}<span class='pct'>{now}</span></b>"

    def total(ws):
        zero = {c: 0 for c in COLS} | {"cost": 0.0, "priced": True}
        return _total([r for r in view["tables"]["model"] if r["week"] in ws] or [zero])
    picks = "".join(f"<b>{n} weeks</b>" if n == len(weeks) else f"<a href='#usage/{n}'>{n} weeks</a>"
                    for n in (2, 4, 8, 13))
    t, this, same = view["tables"], total(weeks[-1:]), view.get("last_week_to_date")
    stats = [("This week so far", _dollars(this), f"{days} of 7 days" if days < 7 else "the whole week")]
    if len(weeks) > 1:
        last = total(weeks[-2:-1])
        if days < 7 and same is not None:
            stats.append(("Last week, same days", _dollars(same), f"this week: {_change(this['cost'], same['cost'])}"))
        stats.append(("All of last week", _dollars(last),
                      "" if days < 7 else f"this week: {_change(this['cost'], last['cost'])}"))
    hero = "".join(f"<div class='stat'><div class='k'>{k}</div><div class='v'>{v}</div><div class='d'>{d}</div></div>"
                   for k, v, d in stats)
    note = (f"<p class='muted'>No price yet for: {html.escape(', '.join(view['unpriced']))}; their cost is left out."
            "</p>" if view["unpriced"] else "")
    return ("<p class='muted lead'>What your chats would cost at Anthropic's API list prices: a yardstick for comparing, "
            "not your bill (you pay a subscription).</p>"
            f"<section class='hero'>{hero}</section>"
            f"<section><div class='head'><h2>Where it goes: {_dollars(total(weeks))} over the last {len(weeks)} weeks"
            f"</h2><span class='pick'>{picks}</span></div>{note}"
            f"<div class='persp'><h3>By role</h3>{_shares(t['role'], weeks, head, ROLE_NAMES)}</div>"
            f"<div class='persp'><h3>By repo</h3>{_shares(t['repo'], weeks, head)}</div>"
            f"<div class='persp'><h3>By model</h3>{_shares(t['model'], weeks, head)}</div>"
            f"<div class='persp'><h3>By token type</h3>{_shares(t.get('type', []), weeks, head, TYPE_NAMES)}</div>"
            "</section>"
            "<details><summary>Details: tokens week by week</summary>"
            f"<p class='muted'>From Claude Code's records on this laptop ({view['files']} chat records), read "
            f"{html.escape(view['at'])}. Weeks run Monday to Sunday. Supervisors are the chats linked to a list, "
            "builders are the agents they start, other chats are everything else.</p>"
            "<h3>By model</h3>" + _table(t["model"], weeks, "Model", label)
            + "<h3>By role</h3>" + _table(t["role"], weeks, "Role", label)
            + "<h3>By repo</h3>" + _table(t["repo"], weeks, "Repo", label) + "</details>")
