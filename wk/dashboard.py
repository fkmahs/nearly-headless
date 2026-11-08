"""The command center: one local page that scans every repo in this laptop's settings and shows where each stands:
what needs the owner, each product with its supervisor, its open pieces with their PRs and builder chats, loose ends
left on the laptop, hours not yet billed, and whether work-kit itself is current. Read-only.

Speed: every repo is read at the same time, and `serve` keeps the newest view in memory and refreshes it in the
background, so the page opens at once. Client names come from this laptop's settings (`client = "..."` under a
repo), never from work-kit.
"""

from __future__ import annotations

import html
import json
import os
import queue
import signal
import socket
import subprocess
import sys
import urllib.request
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, check, core, guard, settings, system, turns, upkeep, usage, work
from .core import WorkkitError


def _gh(cwd: Path, *args: str) -> str:
    result = core.run(["gh", *args], 60, cwd=str(cwd))
    if result.returncode:
        raise WorkkitError("gh: " + (result.stderr.strip() or "failed"))
    return result.stdout


# ---------------------------------------------------------------- is CI running, and is each builder still active
#
# GitHub refusing to run CI (a failed payment or a spending limit) looks like an ordinary failed run; check.py tells
# them apart (never_started). Runs are read once per check.RUNS_EVERY seconds per repo, shared with wk check's
# failing-tests finding (Check GitHub now reads them at once); a finished run's jobs are read once.

CI_EVERY = check.RUNS_EVERY
IDLE_HOURS = 12  # a piece being built with nothing new on its branch, PR or note for this long is flagged
_ci_runs: dict[str, tuple[float, dict]] = {}
_ci_blocked = check._blocked  # run id -> why its jobs never started ("" when they did)


def _never_started(top: Path, run: dict) -> str:
    """Why a failed run's jobs never started, or "" when they ran (an ordinary failure)."""
    return check.never_started(top, run, lambda cwd, *a: _gh(cwd, *a))


def ci_state(top: Path, base: str, fetch: bool = False) -> dict | None:
    """Whether GitHub is running this repo's CI: state ok, failing, stopped or unknown, with a plain line. None for a
    repo without workflows."""
    if not any((top / ".github" / "workflows").glob("*.y*ml")):
        return None
    key, now = core.posix(top), time.monotonic()
    seen = _ci_runs.get(key)
    if seen and not fetch and now - seen[0] < CI_EVERY:
        return seen[1]
    try:
        runs = check.runs(top, lambda cwd, *a: _gh(cwd, *a), fetch) or []
        runs = [r for r in runs if r.get("status") == "completed" and r.get("conclusion") not in ("skipped", "cancelled", "")]
        failed = lambda r: bool(r) and r["conclusion"] in ("failure", "startup_failure")  # noqa: E731
        newest, on_base = (runs or [None])[0], next((r for r in runs if r.get("headBranch") == base), None)
        why = _never_started(top, newest) if failed(newest) else ""
        if why:  # the newest run, on any branch, was refused: nothing runs until that is fixed
            result = {"state": "stopped", "text": f"CI not running: {why}", "url": newest.get("url", "")}
        elif failed(on_base):
            stale = _never_started(top, on_base)  # refused earlier; runs since then on other branches went through
            result = {"state": "failing", "url": on_base.get("url", ""), "tests": not stale,
                      "text": f"CI on {base} has not run since it was stopped ({stale})" if stale else f"CI failing on {base}"}
        else:
            result = {"state": "ok", "text": "CI fine" if runs else "CI: no recent runs"}
    except (WorkkitError, ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError):
        result = {"state": "unknown", "text": "CI status unknown: GitHub could not be asked"}
    _ci_runs[key] = (now, result)
    return result


def _when(text: str) -> datetime | None:
    """A time from git, GitHub or a note's date, in UTC. A bare date counts from the start of that day here."""
    try:
        at = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (at if at.tzinfo else at.astimezone()).astimezone(timezone.utc)


def idle_hours(piece: dict, branch_at: str = "", now: datetime | None = None) -> int | None:
    """Hours since anything happened on a piece being built (its branch's last commit, its PR, its note), or None
    while that is under IDLE_HOURS."""
    try:  # the note's file says when it was last written, to the minute; the piece's date only gives the day
        noted = datetime.fromtimestamp(Path(piece["note"]).stat().st_mtime, timezone.utc).isoformat() \
            if piece.get("note") else ""
    except (OSError, ValueError):
        noted = ""
    seen = [t for t in (_when(piece.get("updated", "")), _when(noted), _when(branch_at),
                        _when((piece.get("pr_info") or {}).get("updatedAt", ""))) if t]
    if not seen:
        return None
    hours = int(((now or datetime.now(timezone.utc)) - max(seen)).total_seconds() // 3600)
    return hours if hours >= IDLE_HOURS else None


def _worktrees(top: Path) -> list[dict]:
    """Every working folder of the repo except the main checkout: path, branch, unsaved files, commits not on GitHub."""
    out, cur = [], {}
    for line in core.git(top, "worktree", "list", "--porcelain", check=False).splitlines() + [""]:
        if line.startswith("worktree "):
            cur = {"path": line[9:], "branch": ""}
        elif line.startswith("branch "):
            cur["branch"] = line[7:].removeprefix("refs/heads/")
        elif not line and cur:
            out.append(cur)
            cur = {}
    def look(w: dict) -> dict:
        path = Path(w["path"])
        dirty = [x for x in core.git(path, "status", "--porcelain", check=False).splitlines() if x]
        local = core.git(path, "rev-list", "--count", "HEAD", "--not", "--remotes", check=False)
        return w | {"dirty": len(dirty), "local": int(local) if local.isdigit() else 0}
    with ThreadPoolExecutor(max_workers=8) as pool:  # every folder is looked at at the same time
        return list(pool.map(look, [w for w in out[1:] if Path(w["path"]).is_dir()]))


def _builders(products: list[dict]) -> set[str]:
    """The names a builder's working folder or branch carries, for every open piece's recorded builder (agent:<id>):
    agent-<id> for the folder, worktree-agent-<id> for the branch it was made with."""
    ids = {x["chat"].removeprefix("agent:").strip() for p in products for x in p["open"]
           if str(x.get("chat", "")).startswith("agent:")} - {""}
    return {n for i in ids for n in (i, f"agent-{i}", f"worktree-{i}", f"worktree-agent-{i}")}


def _loose_ends(top: Path, base: str, dirty: int, in_use: set[str], builders: set[str] = frozenset()) -> list[str]:
    """What is left lying on this laptop: folders no open PR, piece or piece's builder uses, unsaved files, commits only
    here."""
    found = [f"{dirty} unsaved file(s) in the main checkout"] if dirty else []
    folders = _worktrees(top)
    for w in folders:
        used = (bool(w["branch"]) and w["branch"] in in_use | builders) or Path(w["path"]).name in builders
        problems = ([] if used else ["no open PR or piece uses it"]) + \
            [f"{w['dirty']} unsaved file(s)"] * bool(w["dirty"]) + [f"{w['local']} commit(s) not on GitHub"] * bool(w["local"])
        if problems:
            found.append(f"Working folder {Path(w['path']).name} ({w['branch'] or 'no branch'}): " + ", ".join(problems))
    if not core.git(top, "rev-list", "-1", "--branches", "--not", "--remotes", check=False):
        return found  # every local commit is on GitHub: no branch to check one by one
    checked_out = {w["branch"] for w in folders} | {base}
    for b in core.git(top, "for-each-ref", "--format=%(refname:short)", "refs/heads", check=False).splitlines():
        if b in checked_out:
            continue
        local = core.git(top, "rev-list", "--count", b, "--not", "--remotes", check=False)
        if local.isdigit() and int(local):
            found.append(f"Branch {b}: {local} commit(s) saved only on this laptop, not on GitHub")
    return found


_counted: dict[tuple[str, str, str], int] = {}  # (repo folder, base sha, branch sha) -> commits not on the base


def _ahead(top: Path, tip: str, sha: str) -> int:
    """How many commits `sha` has that the base branch's `tip` doesn't. Commits never change, so a pair is counted
    once; a branch or base that moved is counted again."""
    key = (core.posix(top), tip, sha)
    if key not in _counted:
        n = core.git(top, "rev-list", "--count", f"{tip}..{sha}", check=False)
        if not n.isdigit():
            return 0  # git could not say: asked again next time
        _counted[key] = int(n)
    return _counted[key]


def _branches(top: Path, base: str, prs: list[dict], products: list[dict]) -> list[dict]:
    """Every branch on GitHub except the base, and where it belongs: a piece, an open PR, already merged (safe to
    delete), or nowhere. Three git calls for the list, plus one count per branch that isn't merged and moved since it
    was last counted."""
    by_pr_branch = {p["headRefName"]: p for p in prs}
    on_piece = {}
    for p in products:
        for x in p["open"]:
            for b in {x.get("branch", ""), (x.get("pr_info") or {}).get("headRefName", "")} - {""}:
                on_piece[b] = f"{p.get('title') or p['product']} {x['id']}"
    merged = {line.strip().removeprefix("origin/") for line in
              core.git(top, "branch", "-r", "--merged", f"origin/{base}", check=False).splitlines()}
    tip = core.git(top, "rev-parse", "--verify", "--quiet", f"origin/{base}", check=False)
    folder = core.posix(top)
    for key in [k for k in _counted if k[0] == folder and k[1] != tip]:
        del _counted[key]  # counted against an old base and never asked again: memory stays at this repo's branches
    found = []
    for line in core.git(top, "for-each-ref", "--format=%(refname:short)|%(objectname)|%(committerdate:iso-strict)",
                         "refs/remotes/origin", check=False).splitlines():
        ref, _, rest = line.partition("|")
        sha, _, at = rest.partition("|")
        name = ref.removeprefix("origin/")
        if ref in ("origin", "origin/HEAD") or name == base:
            continue
        pr = by_pr_branch.get(name)
        where = "piece" if name in on_piece else "pr" if pr else "merged" if name in merged else "stray"
        found.append({"name": name, "where": where, "piece": on_piece.get(name, ""), "pr": pr["number"] if pr else "",
                      "last": at[:10], "at": at, "ahead": 0, "sha": sha})
    count = [b for b in found if b["where"] != "merged" and tip]
    with ThreadPoolExecutor(max_workers=8) as pool:  # each count is its own git call: asked at the same time
        for b, n in zip(count, pool.map(lambda b: _ahead(top, tip, b["sha"]), count)):
            b["ahead"] = n
    for b in found:
        del b["sha"]
    order = {"stray": 0, "merged": 1, "pr": 2, "piece": 3}
    return sorted(found, key=lambda b: (order[b["where"]], b["name"]))


PROBLEMS_EVERY = 300.0  # seconds between `wk check` runs per repo (Check GitHub now runs it at once)
_checked: dict[str, tuple[float, list[dict]]] = {}


def _problems(s: dict, name: str, path: str, fetch: bool, fetched: Future | None = None) -> list[dict]:
    """`wk check`'s findings for one repo, kept PROBLEMS_EVERY seconds (Check GitHub now asks afresh). `fetched` is the
    repo's git fetch still running."""
    key, now = f"{name}|{path}", time.monotonic()
    seen = _checked.get(key)
    if seen and not fetch and now - seen[0] < PROBLEMS_EVERY:
        return seen[1]
    found = [x for x in check.repo_problems(s, name, gh=lambda cwd, *a: _gh(cwd, *a), fetch=fetch, fetched=fetched)
             if x["kind"] != "reminder"]  # a question's age shows on the question, not as something to fix
    _checked[key] = (now, found)
    return found


def _checks(s: dict, name: str, base: str, out: dict, fetch: bool, job: Future | None = None) -> None:
    """`wk check` for one repo: each list's problem count on the page, its repo's shared ones, and code that reached
    the base branch without a PR as an alert. `job` is the findings already being gathered (_problems). A check
    that fails says so and never hides the rest."""
    try:
        found = job.result() if job is not None else _problems(s, name, out.get("path", ""), fetch)
    except Exception as exc:  # noqa: BLE001 - the page must still show everything else
        out["alerts"].append(f"{name}: wk check could not run ({exc})")
        return
    # a parked list (no supervisor) has nobody to fix its findings yet: they show, marked as waiting, and count nowhere
    parked = {p["product"] for p in out["products"] if not p.get("supervisor")}
    waits = lambda x: (x["product"] or x.get("owner", "")) in parked  # noqa: E731
    fix = lambda x: {"item": x["item"], "what": x["what"], "fix": x["fix"], "owner": x.get("owner", ""),  # noqa: E731
                     "line": check.line(x), "kind": x["kind"], "name": x.get("name", ""), "waits": waits(x)}
    for p in out["products"]:
        p["problems"] = [check.line(x) for x in found if x["product"] == p["product"]]
        p["fixes"] = [fix(x) for x in found if x["product"] == p["product"]]  # the same, in parts for the pop-up
    out["problems"] = [check.line(x) for x in found if not x["product"]]
    out["fixes"] = [fix(x) for x in found if not x["product"]]
    out["base_tests_failing"] = any(x["kind"] == "tests" and x["item"] == base and not x["product"] for x in found)
    for x in found:
        if x["kind"] == "main" and not waits(x):
            out["alerts"].append(f"{name}: code reached {base} without a PR ({x['item']}: {x['what'].split(': ', 1)[-1]}). "
                                 f"Ask the owner to look; then wk check {name} --ack {x['item']} --reason \"...\"")


def _repo(s: dict, name: str, fetch: bool) -> dict:
    out = {"name": name, "client": ((s.get("repos") or {}).get(name) or {}).get("client") or name,
           "attention": [], "alerts": []}
    try:
        top = settings.repo(s, name)
    except WorkkitError as exc:
        out["error"] = str(exc)
        out["attention"].append(f"{name}: {exc}")
        out["alerts"].append(f"{name}: {exc}")
        return out
    # Speed: the git fetch, GitHub's open PRs and wk check (the slowest part, whose own questions to GitHub need no
    # fetch) all start at once; what reads the fetched branches waits for the fetch.
    with ThreadPoolExecutor(max_workers=3) as pool:
        fetched = pool.submit(core.run, ["git", "-C", str(top), "fetch", "--quiet", "--prune", "origin"], 120) \
            if fetch else None
        prs_job = pool.submit(_gh, top, "pr", "list", "--state", "open", "--limit", work.OPEN_PRS, "--json",
                              "number,title,isDraft,headRefName,url,updatedAt,files")
        checks_job = pool.submit(_problems, s, name, core.posix(top), fetch, fetched)
        if fetched is not None:
            fetched.result()
        return _view(s, name, top, fetch, out, prs_job, checks_job)


def _view(s: dict, name: str, top: Path, fetch: bool, out: dict, prs_job: Future, checks_job: Future) -> dict:
    base = settings.base_of(s, top)
    branch = core.git(top, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    dirty = [line for line in core.git(top, "status", "--porcelain", check=False).splitlines() if line]
    counts = core.git(top, "rev-list", "--left-right", "--count", f"{base}...origin/{base}", check=False).split()
    ahead, behind = (int(counts[0]), int(counts[1])) if len(counts) == 2 else (0, 0)
    out.update(path=core.posix(top), base=base, branch=branch, ahead=ahead, behind=behind)
    if branch != base:
        out["alerts"].append(f"{name}: main checkout is on '{branch or 'detached'}', not {base}; recording work there is blocked")
    if ahead:
        out["alerts"].append(f"{name}: {ahead} commit(s) on {base} not on GitHub")
    try:
        prs = json.loads(prs_job.result() or "[]")
        out["prs"] = sorted(prs, key=lambda p: p.get("updatedAt", ""), reverse=True)
    except (WorkkitError, ValueError, OSError, subprocess.SubprocessError) as exc:
        out["prs"], out["prs_error"] = [], str(exc)
    # the PRs' files feed the queue's overlap check, then leave the page's data
    open_files = None if "prs_error" in out else work.pr_files(out["prs"])
    for p in out["prs"]:
        p.pop("files", None)
    by_number = {str(p["number"]): p for p in out["prs"]}
    out["products"] = [x for x in work.status({"repos": {name: (s.get("repos") or {})[name]}}) if "error" not in x]
    for p in out["products"]:
        try:
            p["queue"] = work.queue_status(s, work.Repo(s, name), p["product"], prs=open_files)
        except WorkkitError as exc:
            p["queue"] = {"items": [], "error": str(exc)}
    on_pieces = set()
    for p in out["products"]:
        for x in p["open"]:
            number = x["pr"].lstrip("#")
            x["pr_info"] = by_number.get(number)
            on_pieces.add(number)
            if x["state"] == "ready":
                out["attention"].append(f"{name} {p['product']}: {x['id']} {x['piece']} is ready for your review"
                                        + (f" (#{number})" if number else ""))
        for q in p["questions"]:
            out["attention"].append(f"{name} {p['product']}: {q['question']}"
                                    + (f" (recommended: {q['recommended']})" if q.get("recommended") else ""))
    out["loose_prs"] = [p for p in out["prs"] if str(p["number"]) not in on_pieces]
    out["branches"] = _branches(top, base, out["prs"], out["products"])
    branch_at = {b["name"]: b["at"] for b in out["branches"]}
    for p in out["products"]:
        for x in p["open"]:
            if x["state"] == "building":
                branch = x.get("branch") or (x.get("pr_info") or {}).get("headRefName", "")
                x["idle_hours"] = idle_hours(x, branch_at.get(branch, ""))
                if x["idle_hours"]:
                    out["alerts"].append(f"{name} {p['product']}: {x['id']} {x['piece']}: the builder has shown no "
                                         f"activity for {x['idle_hours']}h (no commit, PR update or note)")
    in_use = {p["headRefName"] for p in out["prs"]} | {x["branch"] for p in out["products"] for x in p["open"]}
    out["loose"] = _loose_ends(top, base, len(dirty), in_use - {""}, _builders(out["products"]))
    _checks(s, name, base, out, fetch, checks_job)
    out["unbilled"] = work.hours(work.Repo(s, name), unbilled=True)["totals"]
    out["turns"] = turns.rows(top)
    for t in out["turns"]:
        if t["expired"]:
            out["alerts"].append(f"{name}: expired turn on {t['resource']} held by {t['holder']}")
    out["ci"] = guard.check(top)
    out["ci_state"] = ci_state(top, base, fetch)
    # failing tests on the base branch, once wk check reports them as "Tests failing", need no alert as well
    if ((out["ci_state"] or {}).get("state") in ("stopped", "failing")
            and not (out["ci_state"].get("tests") and out.get("base_tests_failing"))):
        out["alerts"].append(f"{name}: {out['ci_state']['text']}")
    out["alerts"] += [f"{name}: CI {c['workflow']} {c['problem']}" for c in out["ci"]]
    out["attention"] += out["alerts"]
    return out


def kit(fetch: bool = False) -> dict:
    """This laptop's work-kit: its version, the newest release on GitHub, and whether the skills are current."""
    latest, installed = upkeep.latest_version(fetch), upkeep.installed_version()
    return {"version": __version__, "installed": installed, "restart": upkeep.newer(installed),
            "latest": latest, "behind": upkeep.newer(latest),
            "stale_skills": [f["check"] for f in upkeep.kit_checks() if not f["ok"]]}


def restart_alert(k: dict) -> str:
    return (f"This command center runs work-kit {k['version']}, and {k['installed']} is installed: it moves to "
            f"{k['installed']} on its own within a minute. Refresh then.")


def noted(view: dict, notice: str) -> dict:
    """The view with the keeper's notice (a restart that failed) on top, in place of the 'moves on its own' alert."""
    if not notice:
        return view
    k = view.get("kit") or {}
    drop = restart_alert(k) if k.get("restart") else None
    return view | {key: [notice] + [a for a in view.get(key, []) if a != drop] for key in ("alerts", "attention")} \
        | {"kit": k | {"restart_failed": True}}


def overview(s: dict, fetch: bool = False) -> dict:
    names = list(s.get("repos") or {})
    with ThreadPoolExecutor(max_workers=len(names) + 1) as pool:  # GitHub and git answer each repo in parallel
        kit_job = pool.submit(kit, fetch)
        repos = list(pool.map(lambda n: _repo(s, n, fetch), names))
        k = kit_job.result()
    alerts = [a for r in repos for a in r["alerts"]]
    if k["behind"]:
        alerts.append(f"work-kit {k['latest']} is out (this laptop has {k['version']}): run wk update")
    if k["restart"]:
        alerts.append(restart_alert(k))
    alerts += [f"{c}: run wk update" for c in k["stale_skills"]]
    attention = [a for r in repos for a in r["attention"] if a not in r["alerts"]] + alerts
    try:
        parts = system.overview(s)
    except Exception as exc:  # the work-kit page's picture must never take the rest of the page down
        parts = {"error": f"{type(exc).__name__}: {exc}"}
    return {"owner": s.get("owner", ""), "at": time.strftime("%Y-%m-%d %H:%M"), "repos": repos, "kit": k, "system": parts,
            "alerts": alerts, "attention": attention,
            "stale_days": check.STALE_DAYS,
            "kinds": {kind: {"heading": h, "means": m, "who": w} for kind, (h, m, w) in check.KINDS.items()}}


# ---------------------------------------------------------------- page

PAGE = Path(__file__).with_name("page.html")
NOTE_CSS = ("body{margin:0;background:#F7F5F3;color:#3D3B39;font:14px/1.6 'Segoe UI',system-ui,sans-serif}"
            "main{max-width:860px;margin:0 auto;padding:24px 20px}a{color:#3046C9;text-decoration:none}"
            "pre{white-space:pre-wrap;background:#fff;border:1px solid #E7E3DF;border-radius:12px;padding:18px;"
            "font:13.5px/1.6 'Segoe UI',system-ui,sans-serif}"
            "@media (prefers-color-scheme:dark){body{background:#151414;color:#D7D3CF}pre{background:#1D1C1B;"
            "border-color:#2E2C2A}a{color:#8C9BFF}}")


def _e(value) -> str:
    return html.escape(str(value or ""))


def render(view: dict, saved: bool = False) -> str:
    """The page: one template whose script draws everything from the view, so the server only gathers data."""
    data = json.dumps(view | {"saved": saved}, default=str).replace("</", "<\\/").replace("<!--", "<\\u0021--")
    return PAGE.read_text(encoding="utf-8").replace("__DATA__", data)


class Settings:
    """This laptop's settings as a running command center sees them: read again whenever the file changes, so a repo
    added to it shows on the next visit. A file that can't be read keeps the last good settings, and says why."""

    def __init__(self, s: dict, where: Path | None = None):
        self.s, self.where, self.error, self.lock = s, where, "", threading.Lock()
        self.stamp = self._stamp()

    def _stamp(self):
        try:
            found = self.where.stat()
            return found.st_mtime_ns, found.st_size
        except (AttributeError, OSError):  # no file given, or it is gone
            return None

    def changed(self) -> bool:
        return self.where is not None and self._stamp() != self.stamp

    def get(self) -> dict:
        if not self.changed():
            return self.s
        with self.lock:
            stamp = self._stamp()
            if stamp != self.stamp:
                self.stamp = stamp
                try:
                    if stamp is None:
                        raise WorkkitError(f"Cannot read {core.posix(self.where)}: the file is missing")
                    self.s, self.error = settings.load(self.where), ""
                except WorkkitError as exc:
                    self.error = f"{exc}. Showing the settings from before until it is fixed"
            return self.s


class Cache:
    """The newest view, kept in memory and refreshed in the background, so the page opens at once. A change to the
    settings file builds the view afresh on the next visit."""

    STALE_AFTER = 300.0  # seconds: a view older than this says so on the page, whatever held the refresh up
    WAIT = 45.0          # seconds a visit waits for a refresh another one is running (a full one takes about 8)

    def __init__(self, s: dict, max_age: float = 60.0, where: Path | None = None):
        self.settings, self.max_age, self.view, self.at, self.lock = Settings(s, where), max_age, None, 0.0, threading.Lock()
        self.failed = ""  # why the last background refresh failed; empty once one succeeds

    def get(self, fetch: bool = False) -> dict:
        if fetch or self.view is None or self.settings.changed():
            return self.refresh(fetch)
        view = self.view  # an old view is served at once; the fresh one is for the next visit
        if time.monotonic() - self.at > self.max_age and self.lock.acquire(blocking=False):
            threading.Thread(target=self._refresh_locked, daemon=True).start()
        return self._flagged(view)

    def _flagged(self, view: dict, busy: bool = False) -> dict:
        """The view with an alert on top when it is old: a refresh failed, one has run past STALE_AFTER, or (busy) a
        fresh view was asked for while one was still being built."""
        age = time.monotonic() - self.at
        why = self.failed or (f"refreshing has not finished for {int(age // 60)} minutes" if age > self.STALE_AFTER
                               else "a refresh already running had not finished, so this one waited no longer"
                               if busy else "")
        if not why:
            return view
        alert = f"This command center shows the view from {view.get('at', '?')}: {why}. Use Check GitHub now, or restart wk serve if it persists."
        flagged = view | {key: [alert] + view.get(key, []) for key in ("alerts", "attention")}
        return flagged | {"busy": True} if busy else flagged  # busy: not fresh, so Check GitHub now says it wasn't checked

    def refresh(self, fetch: bool = False) -> dict:
        """A fresh view; while another refresh holds things up past WAIT, the last view, saying so (the first view
        has nothing to fall back on, so it waits)."""
        if not self.lock.acquire(timeout=self.WAIT if self.view is not None else -1):
            return self._flagged(self.view, busy=True)
        try:
            return self._build(fetch)
        finally:
            self.lock.release()

    def _refresh_locked(self) -> None:
        try:
            self._build(False)
        except Exception as exc:  # noqa: BLE001 - a failed refresh must show on the page, not die silently
            self.failed = f"the background refresh failed at {time.strftime('%H:%M')} ({type(exc).__name__}: {exc})"
            print(f"wk serve: {self.failed}", file=sys.stderr, flush=True)
        finally:
            self.lock.release()

    def _build(self, fetch: bool) -> dict:
        view = overview(self.settings.get(), fetch)
        if self.settings.error:
            for key in ("alerts", "attention"):
                view.setdefault(key, []).insert(0, self.settings.error)
        if fetch:
            self.fetched_at = time.strftime("%H:%M")
        view["fetched_at"] = getattr(self, "fetched_at", "")
        self.view, self.at, self.failed = view, time.monotonic(), ""
        return view


class Server(ThreadingHTTPServer):
    """Never shares its address: on Windows a second server could otherwise bind it silently and answer instead.
    Closing it waits for the requests it is answering, so a server told to stop finishes them first."""
    allow_reuse_address = False
    daemon_threads = False

    def server_bind(self):
        if sys.platform == "win32":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def running(port: int) -> dict | None:
    """What already answers at 127.0.0.1:port: None when nothing does; else the command center's work-kit version, if
    it is one."""
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
    except OSError:
        return None
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api", timeout=15) as reply:
            return {"version": json.loads(reply.read().decode("utf-8")).get("kit", {}).get("version", "")}
    except (OSError, ValueError):
        return {"version": ""}


def _handler(cache: Cache, notice: list[str]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlparse(self.path)
            code, parts = 200, url.path.strip("/").split("/")
            if parts[0] == "note" and len(parts) == 4:  # /note/<repo>/<product>/<piece>
                try:
                    text = work.read_note(work.Repo(cache.settings.get(), parts[1]), parts[2], parts[3]) or "No note yet."
                except WorkkitError as exc:
                    code, text = 404, str(exc)
                body, kind = (f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' "
                              f"content='width=device-width,initial-scale=1'><title>{_e(parts[3])} note</title>"
                              f"<style>{NOTE_CSS}</style></head><body><main><a href='/'>Back to the command center</a>"
                              f"<pre>{_e(text)}</pre></main></body></html>").encode(), "text/html; charset=utf-8"
            elif parts[0] == "usage":  # the Tokens & cost tab's content: /usage/part?weeks=N
                weeks = (parse_qs(url.query).get("weeks") or ["2"])[0]
                if parts[1:] != ["part"]:  # an old link to the page it used to be: the tab
                    self.send_response(303)
                    self.send_header("Location", f"/#usage/{weeks if weeks.isdigit() else 2}")
                    self.end_headers()
                    return
                kind = "text/html; charset=utf-8"
                try:
                    body = usage.page(usage.report(cache.settings.get(), min(52, max(1, int(weeks)))))
                except (ValueError, OSError, WorkkitError) as exc:  # the tab says why, as plain text
                    code, body, kind = 500, str(exc), "text/plain; charset=utf-8"
                body = body.encode()
            else:
                fetch = parse_qs(url.query).get("fetch") == ["1"]
                view = noted(cache.get(fetch), notice[0])
                if fetch and url.path != "/api":  # after checking GitHub, show the plain address again
                    self.send_response(303)
                    self.send_header("Location", "/")
                    self.end_headers()
                    return
                body, kind = (json.dumps(view, default=str).encode(), "application/json") if url.path == "/api" else \
                    (render(view).encode(), "text/html; charset=utf-8")
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass
    return Handler


# ---------------------------------------------------------------- serving, and moving to a new release on its own
#
# `wk serve` is a small keeper: it holds the address and runs the command center itself in a child process, a worker.
# When work-kit's version on disk changes (wk update), the keeper starts a worker on the new code, on the same
# listening socket, waits until it answers, then tells the old one to finish its requests and stop. The address is
# never let go, so no other program can take it in between, and if the new worker fails the old one keeps serving and
# the page says so. The keeper itself stays on the code it started with, so it does as little as possible, and it
# talks to workers in plain lines that later releases must keep understanding:
#   keeper -> worker, on its input:  "socket win <hex>" or "socket fd <n>" first (the listening socket), then
#                                    "note <text>" (shown on top of the page; empty clears it); "stop", or the end of
#                                    its input (the keeper is gone): finish the requests in hand and exit
#   worker -> keeper, on its output: "ready <version>" once it answers on the socket

VERSION_EVERY = 3.0   # seconds between looks at the version on disk
START_WITHIN = 300.0  # seconds a new worker has to gather its first view and answer
RETRY_AFTER = 300.0   # seconds before a release that failed to start is tried again


def _share(sock: socket.socket, pid: int) -> str:
    return f"socket win {sock.share(pid).hex()}" if sys.platform == "win32" else f"socket fd {sock.fileno()}"


def _shared(line: str) -> socket.socket:
    _, kind, value = line.split()
    return socket.fromshare(bytes.fromhex(value)) if kind == "win" else socket.socket(fileno=int(value))


class Worker:
    """One command center process, started by the keeper on whatever work-kit is on disk now."""

    def __init__(self, cmd: list[str], kit: Path, sock: socket.socket):
        extra = {} if sys.platform == "win32" else {"pass_fds": [sock.fileno()]}
        env = os.environ | {"PYTHONPATH": str(kit), "WORKKIT_AUTO_UPDATE": "0"}  # only the keeper's own command updates
        self.proc = subprocess.Popen(cmd, cwd=str(kit), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     text=True, encoding="utf-8", errors="replace", **extra)
        self.version, self.lines = "", queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()
        self.send(_share(sock, self.proc.pid))

    def _read(self):
        for line in self.proc.stdout:
            self.lines.put(line.rstrip("\n"))
        self.proc.stdout.close()
        self.lines.put(None)

    def send(self, line: str) -> None:
        try:
            self.proc.stdin.write(line.replace("\n", " ") + "\n")
            self.proc.stdin.flush()
        except (OSError, ValueError):  # it has stopped
            pass

    def ready(self, within: float) -> bool:
        end = time.monotonic() + within
        while (left := end - time.monotonic()) > 0:
            try:
                line = self.lines.get(timeout=left)
            except queue.Empty:
                break
            if line is None:
                return False
            if line.startswith("ready "):
                self.version = line[6:].strip()
                return True
            print(line, flush=True)  # anything else it says goes on to the window
        return False

    def stop(self, within: float = 90.0) -> None:
        """Let it finish the requests in hand (a GitHub call takes at most a minute), then make sure it is gone."""
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(within)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()


class Keeper:
    """Holds the listening socket and keeps one worker answering on it, on the newest work-kit on disk."""

    def __init__(self, sock: socket.socket, cmd: list[str], kit: Path):
        self.sock, self.cmd, self.kit = sock, cmd, kit
        self.current: Worker | None = None
        self.seen, self.failed, self.stopping = "", {}, []

    def on_disk(self) -> str:
        try:
            return upkeep.installed_version(self.kit)
        except (OSError, IndexError):  # mid-update: look again next time
            return ""

    def _start(self) -> Worker | None:
        w = Worker(self.cmd, self.kit, self.sock)
        if w.ready(START_WITHIN):
            return w
        w.stop(5)
        return None

    def begin(self) -> bool:
        self.current = self._start()
        return self.current is not None

    def check(self) -> None:
        """One look: start a worker again if it stopped; move to a new release once its version has held still."""
        if self.current is None or self.current.proc.poll() is not None:
            print("wk serve: the command center stopped; starting it again", file=sys.stderr, flush=True)
            self.current = self._start()
            return
        version = self.on_disk()
        settled, self.seen = version == self.seen, version
        if not (version and settled) or version == self.current.version or \
                time.monotonic() - self.failed.get(version, -RETRY_AFTER) < RETRY_AFTER:
            return
        new = self._start()
        if new is None:
            self.failed[version] = time.monotonic()
            self.current.send(f"note work-kit {version} is installed, but this command center could not restart on "
                              f"it, so it still runs {self.current.version}. It tries again in 5 minutes; to restart by "
                              "hand, stop wk serve (Ctrl+C in its window) and run it again.")
            print(f"wk serve: could not move to work-kit {version}; still on {self.current.version}", file=sys.stderr,
                  flush=True)
            return
        old, self.current = self.current, new
        stopping = threading.Thread(target=old.stop, daemon=True)
        stopping.start()
        self.stopping.append(stopping)
        print(f"wk serve: moved to work-kit {new.version}", flush=True)

    def close(self) -> None:
        if self.current is not None:
            self.current.stop()
        for t in self.stopping:
            t.join()


def serve(s: dict, port: int = 8765, where: Path | None = None) -> None:
    url = f"http://127.0.0.1:{port}"
    other = running(port)
    if other is not None:
        if not other["version"]:
            raise WorkkitError(f"Something else is using {url}. Pick another address: wk serve --port {port + 1}")
        if upkeep.newer(__version__, other["version"]):
            raise WorkkitError(f"A command center is already running at {url}, on the older work-kit "
                               f"{other['version']} (this laptop has {__version__}). Stop it with Ctrl+C in its "
                               "terminal, then run wk serve again.")
        raise WorkkitError(f"The command center is already running: open {url}")
    try:
        held = Server(("127.0.0.1", port), BaseHTTPRequestHandler)  # the keeper only holds the address
    except OSError as exc:
        raise WorkkitError(f"Cannot use {url} ({exc.strerror or exc}). Pick another address: wk serve --port {port + 1}")
    cmd = [sys.executable, "-P", "-m", "wk", *(["--settings", str(where)] if where else []), "serve", "--port", str(port),
           "--worker"]
    keeper = Keeper(held.socket, cmd, upkeep.KIT)
    try:
        if not keeper.begin():
            raise WorkkitError("The command center could not start; the reason is above")
        print(f"Command center: {url}  (Ctrl+C to stop)", flush=True)
        while True:
            time.sleep(VERSION_EVERY)
            keeper.check()
    except KeyboardInterrupt:
        pass
    finally:
        keeper.close()
        held.server_close()


def work_on(s: dict, port: int, where: Path | None = None, inp=None, out=None) -> None:
    """A worker: answer on the keeper's socket until the keeper says stop (or is gone), then finish and exit."""
    inp, out = inp or sys.stdin, out or sys.stdout
    if inp is sys.stdin:
        signal.signal(signal.SIGINT, signal.SIG_IGN)  # Ctrl+C reaches the keeper, which stops this one in order
    sock = _shared(inp.readline())
    cache, notice = Cache(s, where=where), [""]  # the settings file is read again when it changes
    server = Server(("127.0.0.1", port), _handler(cache, notice), bind_and_activate=False)
    server.socket.close()
    server.socket, server.server_address = sock, sock.getsockname()
    cache.refresh()  # the first view is ready before it answers, so the move is not felt
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"ready {__version__}", file=out, flush=True)
    for line in inp:
        word, _, text = line.rstrip("\n").partition(" ")
        if word == "note":
            notice[0] = text
        elif word == "stop":
            break
    server.shutdown()
    server.server_close()  # waits for the requests in hand
