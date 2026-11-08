"""Turns on shared resources, and release gates. Works in any Git repo; the repo is the current folder.

A turn gives one branch the use of some items of a shared resource (a test environment, a database) for a few
hours. State is one JSON file in the repo's shared Git folder (never committed), so every worktree of the repo
sees the same turns. Refusals raise `Refused`, which the CLI turns into exit status 1.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from . import core, settings, testmap
from .core import KitError

STATE, LOCK = "wk-turns.json", "wk-turns.lock"
HOURS = 2.0


class Refused(KitError):
    pass


def _now() -> float:
    return time.time()


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")


def _secs(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _left(t: dict, now: float) -> float:
    return t["left"] if t.get("paused") else _secs(t["expires"]) - now


def live(t: dict, now: float) -> bool:
    return _left(t, now) > 0


def _span(seconds: float) -> str:
    m = max(0, int(seconds // 60))
    return f"{m // 60}h{m % 60:02d}m"


def state_file(repo: Path) -> Path:
    return core.git_common_dir(repo) / STATE


def read(repo: Path) -> list[dict]:
    path = state_file(repo)
    return core.read_json(path).get("turns", []) if path.is_file() else []


def _here(cwd: Path) -> tuple[Path, str]:
    top, branch = core.checkout_of(cwd)
    if not branch:
        raise Refused("This folder is not on a branch")
    return top, branch


def _fetched(top: Path, base: str) -> str:
    """Fetch origin/<base> quietly and return the ref to compare with; the local base when there is no remote."""
    if not core.git(top, "remote", check=False):
        return base
    if core.run(["git", "-C", str(top), "fetch", "--quiet", "origin", base], 120).returncode:
        raise Refused(f"Cannot fetch origin/{base}, so it is unknown whether this branch is up to date. Retry.")
    return f"origin/{base}"


def _hours(hours: float) -> float:
    if not 0 < hours <= 24:
        raise KitError("--hours must be more than 0 and at most 24")
    return hours


def _has_latest(top: Path, ref: str) -> bool:
    """True when HEAD contains ref, or misses only commits that touch nothing but `.work/` (recorded work state)."""
    if not core.run(["git", "-C", str(top), "merge-base", "--is-ancestor", ref, "HEAD"]).returncode:
        return True
    fork = core.git(top, "merge-base", "HEAD", ref)
    changed = core.git(top, "diff", "--name-only", fork, ref).splitlines()
    return all(p.startswith(".work/") for p in changed)


def take(cwd: Path, s: dict, resource: str, items: list[str], hours: float = HOURS) -> dict:
    top, branch = _here(cwd)
    base = settings.base_of(s, top)
    ref = _fetched(top, base)
    if not _has_latest(top, ref):
        raise Refused(f"{branch} does not contain the latest {ref}. Merge {base} into it first, then take the turn.")
    now, span = _now(), _hours(hours) * 3600
    with core.locked(top, f"turn take {resource}", LOCK):
        turns = read(top)
        busy = [t for t in turns if t["resource"] == resource and t["item"] in items
                and t["holder"] != branch and live(t, now)]
        if busy:
            raise Refused("Not your turn: " + "; ".join(
                f"{t['item']} is held by {t['holder']} ({t.get('folder', '')}), "
                + ("paused, waiting on approval" if t.get("paused") else f"{_span(_left(t, now))} left")
                for t in busy))
        turns = [t for t in turns if not (t["resource"] == resource and t["item"] in items)]
        for item in dict.fromkeys(items):
            turns.append({"resource": resource, "item": item, "holder": branch, "folder": core.posix(top),
                          "taken": _iso(now), "expires": _iso(now + span), "paused": False})
        core.write_json(state_file(top), {"turns": turns})
    return {"taken": list(dict.fromkeys(items)), "resource": resource, "holder": branch, "expires": _iso(now + span)}


def _change(cwd: Path, what: str, resource: str | None, items: list[str], go) -> dict:
    """Apply go(turn, now) to this branch's turns (optionally one resource / some items); go returns True to drop."""
    top, branch = _here(cwd)
    now = _now()
    with core.locked(top, f"turn {what}", LOCK):
        kept, done = [], []
        for t in read(top):
            if t["holder"] == branch and (not resource or t["resource"] == resource) and \
                    (not items or t["item"] in items):
                done.append(f"{t['resource']}/{t['item']}")
                if go(t, now):
                    continue
            kept.append(t)
        if not done:
            raise Refused(f"{branch} holds no matching turn")
        core.write_json(state_file(top), {"turns": kept})
    return {what: done, "holder": branch}


def renew(cwd: Path, resource: str | None = None, items: list[str] = (), hours: float = HOURS) -> dict:
    span = _hours(hours) * 3600

    def go(t, now):
        if t.get("paused"):
            t["left"] = span
        else:
            t["expires"] = _iso(now + span)
    return _change(cwd, "renewed", resource, items, go)


def pause(cwd: Path, resource: str | None = None, items: list[str] = ()) -> dict:
    def go(t, now):
        if not t.get("paused"):
            t.update(paused=True, left=max(0.0, _secs(t["expires"]) - now))
    return _change(cwd, "paused", resource, items, go)


def resume(cwd: Path, resource: str | None = None, items: list[str] = ()) -> dict:
    def go(t, now):
        if t.get("paused"):
            t.update(paused=False, expires=_iso(now + t.pop("left", 0)))
    return _change(cwd, "resumed", resource, items, go)


def release(cwd: Path, resource: str | None = None, items: list[str] = ()) -> dict:
    return _change(cwd, "released", resource, items, lambda t, now: True)


def check(cwd: Path, resource: str, items: list[str]) -> dict:
    top, branch = _here(cwd)
    now = _now()
    held = {t["item"] for t in read(top) if t["resource"] == resource and t["holder"] == branch and live(t, now)}
    missing = [i for i in items if i not in held]
    if missing:
        raise Refused(f"{branch} does not hold a turn on {resource} for: {', '.join(missing)}. "
                      f"Take it first: nh turn take {resource} {' '.join(missing)}")
    return {"ok": True, "resource": resource, "items": items, "holder": branch}


def rows(repo: Path, now: float | None = None) -> list[dict]:
    """Current turns grouped by resource and holder, with a plain state: '1h20m left', 'paused' or 'expired'."""
    now = _now() if now is None else now
    groups = {}
    for t in read(repo):
        paused, expired = bool(t.get("paused")), not live(t, now)
        g = groups.setdefault((t["resource"], t["holder"], paused, expired), {
            "resource": t["resource"], "holder": t["holder"], "folder": t.get("folder", ""), "items": [],
            "paused": paused, "expired": expired, "expires": "" if paused else t["expires"]})
        g["items"].append(t["item"])
        if not paused and t["expires"] < g["expires"]:
            g["expires"] = t["expires"]
    for g in groups.values():
        g["state"] = "paused" if g["paused"] else "expired" if g["expired"] else \
            f"{_span(_secs(g['expires']) - now)} left"
    return sorted(groups.values(), key=lambda g: (g["resource"], g["holder"]))


def gate_release(cwd: Path, s: dict) -> dict:
    top, branch = core.checkout_of(cwd)
    base = settings.base_of(s, top)
    if branch != base:
        raise Refused(f"Releases run only from {base}; this folder is on '{branch or 'detached'}'. "
                      f"Merge the PR, then release from {base}.")
    if core.git(top, "status", "--porcelain", check=False):
        raise Refused("The working tree has uncommitted changes. A release must ship exactly what is on GitHub.")
    ref = _fetched(top, base)
    if ref != f"origin/{base}":
        raise Refused(f"This repo has no remote, so origin/{base} is unknown")
    head, remote = core.git(top, "rev-parse", "HEAD"), core.git(top, "rev-parse", ref)
    if head != remote:
        ahead, behind = core.git(top, "rev-list", "--left-right", "--count", f"HEAD...{ref}").split()
        raise Refused(f"{base} is not level with {ref} ({ahead} ahead, {behind} behind). Pull or push until they match.")
    return {"ok": True, "branch": base, "head": head[:12]}


def _gh(cwd: Path, *args: str) -> str:
    try:
        result = core.run(["gh", *args], 60, cwd=str(cwd))
    except OSError as exc:
        raise KitError(f"gh failed: {exc}") from exc
    if result.returncode:
        raise KitError("gh: " + (result.stderr.strip() or "failed"))
    return result.stdout


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def gate_approved(cwd: Path, s: dict, pr: int, action: str) -> dict:
    """Exit 0 only when a PR comment has the line 'Approved by the owner: <action> @ <sha>' and <sha> (7+ characters)
    is the PR's current head commit, so an approval covers exactly the code it names. An approval posted before 1.56.0
    names the approver from the settings instead, and still counts on a private repo."""
    names = settings.approvers(s, cwd)
    data = json.loads(_gh(cwd, "pr", "view", str(pr), "--json", "comments,headRefOid,url") or "{}")
    head = (data.get("headRefOid") or "").lower()
    who = "|".join(re.escape(n) for n in names)
    line = re.compile(rf"^\s*approved by (?:{who})\s*:\s*(.+?)\s*@\s*([0-9a-f]{{7,40}})\s*$", re.I | re.M)
    matches = [(c, sha.lower()) for c in data.get("comments", []) for text, sha in line.findall(c.get("body", ""))
               if _norm(text) == _norm(action)]
    current = [c for c, sha in matches if head and head.startswith(sha)]
    if current:
        c = max(current, key=lambda c: c.get("createdAt", ""))
        return {"ok": True, "pr": pr, "action": action, "head": head[:12], "comment": c.get("url", "")}
    if matches:
        c, sha = matches[-1]
        raise Refused(f"PR #{pr}: the approval for '{action}' ({c.get('url', '')}) names {sha}, but the PR's head is "
                      f"now {head[:12]}. Ask the owner again for this commit.")
    raise Refused(f"PR #{pr} has no comment with the line "
                  f"'Approved by {settings.APPROVER}: {action} @ {head[:12] or '<sha>'}'")


def changed_on_both(top: Path, head: str, ref: str) -> list[str]:
    """Files the branch at `head` changes that `ref` (the base branch as GitHub has it) has also changed since the two
    parted (their merge-base): edits Git would combine on merge without anyone having tested them together. Recorded
    work in `.work/` never counts. Empty when `ref` is already in `head`, or the two changed different files."""
    fork = core.git(top, "merge-base", head, ref)

    def files(a, b):
        return set(core.git(top, "diff", "--name-only", "--no-renames", a, b).splitlines())
    return sorted(p for p in files(fork, head) & files(fork, ref) if p and not p.startswith(".work/"))


def has_commit(top: Path, sha: str, pr: int | str) -> bool:
    """True when this repo holds commit `sha`, fetching the PR's head from GitHub (pull/<n>/head, which moves no
    branch) when it doesn't yet."""
    def here():
        return not core.run(["git", "-C", str(top), "cat-file", "-e", f"{sha}^{{commit}}"]).returncode
    if not sha:
        return False
    if here():
        return True
    core.run(["git", "-C", str(top), "fetch", "--quiet", "origin", f"pull/{pr}/head"], 120)
    return here()


def carried_over(top: Path, approved: str, head: str, ref: str) -> str:
    """Why the owner's yes at `approved` does not carry over to `head`, or "" when it does: every commit after it on
    the PR's own line (first parents) is a merge of `ref`'s history whose result is exactly what Git made of it (no
    conflict resolved by hand, no other edit: `git show --remerge-diff` is empty)."""
    rows = core.git(top, "rev-list", "--first-parent", "--parents", f"{approved}..{head}").splitlines()
    if not rows or rows[-1].split()[1:2] != [approved]:
        return f"{approved[:12]} is not an earlier version of this PR"
    for row in rows:
        c, *parents = row.split()
        if len(parents) != 2:
            return f"{c[:12]} is new work, not a merge of {ref}"
        if core.run(["git", "-C", str(top), "merge-base", "--is-ancestor", parents[1], ref]).returncode:
            return f"{c[:12]} merges something other than {ref}"
        if core.git(top, "show", "--remerge-diff", "--format=", c).strip():
            return f"{c[:12]} changes the merge by hand (a conflict resolved, or an edit)"
    return ""


def ci_green(rollup: list[dict]) -> str:
    """Why the PR head's GitHub checks are not green, or "": a test workflow (check.NOT_TESTS decides, as for wk
    check's failing tests) completed with success, and no check failed, was cancelled, went stale or is still running.
    A skipped run says nothing (a draft's), so skipped tests alone are not green."""
    from .check import NOT_TESTS  # here: check imports this module
    seen, tested = [], False
    for r in rollup or ():
        if r.get("__typename") == "CheckRun" and (r.get("status") or "").upper() != "COMPLETED":
            return "its checks are still running"
        v = (r.get("conclusion") or r.get("state") or "").upper()
        seen.append(v)
        name = r.get("workflowName") or ""
        tested = tested or (v == "SUCCESS" and bool(name) and not NOT_TESTS.search(name))
    if any(v in ("PENDING", "EXPECTED") for v in seen):
        return "its checks are still running"
    if any(v in ("FAILURE", "ERROR", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "CANCELLED", "STALE")
           for v in seen):
        return "a check on it failed, was cancelled or went stale"
    return "" if tested else "no test workflow passed on it"


SECTIONS = ("What was wrong", "What changed", "How it was tested", "What it means for you")  # every PR, in this order


def missing_sections(body: str) -> list[str]:
    """The four sections a PR description lacks, each a heading of its own (any level, case ignored)."""
    heads = [m[1].strip().rstrip(":").lower() for m in re.finditer(r"^\s*#+\s*(.+?)\s*#*\s*$", body or "", re.M)]
    return [h for h in SECTIONS if h.lower() not in heads]


def _text_checks(s: dict, top: Path, data: dict, where: str, pr: int) -> list[str]:
    """Refuses a PR whose description lacks one of the four sections (one opened before this laptop took them up,
    `pr_sections_from`, only gets a warning, returned), and, on a public repo, one whose title, description or
    comments hold a private name (where, never the name)."""
    from . import privacy
    warnings = []
    missing = missing_sections(data.get("body") or "")
    since = str(s.get("pr_sections_from") or "")
    if missing:
        if since and str(data.get("createdAt") or "") >= since:
            raise Refused(f"{where} PR #{pr}'s description lacks {', '.join(missing)}: every PR reads What was wrong, "
                          "What changed, How it was tested, What it means for you. Its builder adds them, then run "
                          "this gate again.")
        warnings.append(f"the description lacks {', '.join(missing)} (opened before the four sections were required, "
                        "so this only warns)")
    if settings.is_public(settings.info_of(s, top)[1]):
        pat = privacy.pattern(privacy.terms(s))
        hits = (["the title"] if privacy.lines_with(data.get("title") or "", pat) else []) + \
            [f"the description, line {n}" for n in privacy.lines_with(data.get("body") or "", pat)] + \
            [f"comment {i}" for i, c in enumerate(data.get("comments") or [], 1)
             if privacy.lines_with(c.get("body") or "", pat)]
        if hits:
            raise Refused(f"{where} is public, and PR #{pr} has a private name in {'; '.join(hits)}. Reword it (a "
                          "comment: edit or delete it), then run this gate again.")
    return warnings


def gate_fresh(cwd: Path, s: dict, pr: int, sha: str = "") -> dict:
    """Exit 0 only when the PR already holds every change its base branch (on GitHub, fetched now) has had to the files
    the PR changes, so what merges is what was tested. A PR that changes other files than the base branch did never
    waits. With `sha` (the commit the owner approved, 7+ characters) the PR's head must be that commit, or one the
    yes carries over to: only clean merges of the base branch since (carried_over) and CI green on it; otherwise the
    owner is asked again. Names the repo it judged, from the PR's link."""
    top = core.checkout_of(cwd)[0]
    data = json.loads(_gh(top, "pr", "view", str(pr), "--json",
                          "state,headRefOid,headRefName,baseRefName,url,statusCheckRollup,title,body,createdAt,"
                          "comments") or "{}")
    where = "/".join((data.get("url") or "").split("/")[3:5]) or core.posix(top)  # owner/repo, from the PR's link
    if data.get("state") != "OPEN":
        raise Refused(f"{where} PR #{pr} is {(data.get('state') or 'unknown').lower()}, not open")
    warnings = _text_checks(s, top, data, where, pr)
    base = data.get("baseRefName") or settings.base_of(s, top)
    head, sha = (data.get("headRefOid") or "").lower(), (sha or "").lower()
    if sha and len(sha) < 7:
        raise Refused(f"--sha {sha} is too short to name one commit: give at least 7 characters")
    ref = _fetched(top, base)
    if not has_commit(top, head, pr):
        raise Refused(f"{where} PR #{pr}'s head {head[:12]} could not be fetched, so whether it has the latest {base} "
                      "is unknown. Retry.")
    both = changed_on_both(top, head, ref)
    if both:
        shown = ", ".join(both[:8]) + (f" and {len(both) - 8} more" if len(both) > 8 else "")
        raise Refused(f"{where} PR #{pr} lacks the latest {base}: {ref} changed {shown} after this PR's branch last "
                      f"took it, so the two were never tested together. Do not merge. Its builder takes {base} into "
                      f"{data.get('headRefName') or 'its branch'} (git merge {ref}), re-runs its tests (and the "
                      "sandbox check with screenshots, where the repo has one), pushes and posts a new Ready comment; "
                      "a clean merge keeps the owner's yes (run this gate again with the approved --sha), anything "
                      "else needs a new one.")
    out = {"ok": True, "repo": where, "pr": pr, "head": head[:12], "base": ref} | \
        ({"warning": "; ".join(warnings)} if warnings else {})
    if not sha or head.startswith(sha):
        return out | ({"approval": f"approved at {head[:12]}"} if sha else {})
    approved = core.git(top, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}", check=False)
    why = carried_over(top, approved, head, ref) if approved else f"{sha} is not a commit in {where}"
    if not why and (ci := ci_green(data.get("statusCheckRollup") or [])):
        why = f"{ci} on {head[:12]}"
    if why:
        raise Refused(f"{where} PR #{pr}'s head is {head[:12]}, not the approved {sha}, and the yes does not carry "
                      f"over: {why}. Ask the owner for a yes on {head[:12]}.")
    return out | {"approval": f"approved at {approved[:12]}, carried over: only clean merges of {base} since, "
                              "CI green"}


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def builder_worktree(top: Path, branch: str) -> Path:
    """The worktree that has `branch` checked out (from `git worktree list`), never the repo's main checkout."""
    rows = [r for r in core.git(top, "worktree", "list", "--porcelain").split("\n\n") if r.strip()]
    found = [i for i, r in enumerate(rows) if f"branch refs/heads/{branch}" in r.splitlines()]
    if not found:
        raise KitError(f"No worktree has {branch} checked out, so there is no builder worktree to refresh. "
                           "Start a builder from the PR instead.")
    if found[0] == 0:
        raise Refused(f"{branch} is checked out in the repo's main checkout, which nh refresh never touches.")
    return Path(rows[found[0]].splitlines()[0].removeprefix("worktree "))


MAPS = testmap.MAPS  # where a repo keeps its `## Change → check` map
_READY = re.compile(r"^\s*Ready @ ([0-9a-f]{7,40})\b", re.I)
_GAPS = re.compile(r"^[ \t]*\**gap checks\**[ \t]*:\**[ \t]*(.*?)[ \t]*$", re.I | re.M)  # one line only


def full_run_paths(tree: Path, *refs: str) -> list[str]:
    """The paths whose change needs the full run (what CI runs to decide what it tests), read from the repo's own
    change → check map (the rows whose check is the full run) at each ref, so the list lives in one place. A repo's
    workflows always count."""
    found = {".github/workflows/"}
    for ref in refs:
        for name in MAPS:
            for row in testmap.map_rows(core.git(tree, "show", f"{ref}:{name}", check=False)):
                if row["kind"] == "full":
                    found |= {p for p in row["paths"] if "<" not in p and "*" not in p}
    return sorted(found)


def _touches(files: list[str], paths: list[str]) -> list[str]:
    """The files that are one of the paths or inside one (at a path boundary, with or without a trailing /)."""
    roots = [p.rstrip("/") for p in paths if p.rstrip("/")]
    return [f for f in files if any(f == r or f.startswith(r + "/") for r in roots)]


def ready_gaps(comments: list, head: str = "") -> str:
    """Why the latest `Ready @ <sha>` comment doesn't let nh refresh stand in for its builder, or "" when it does:
    it must name the PR's head (a push after it has no Ready yet) and say `Gap checks: none` (the builder skill's
    Ready format), so CI is the full proof."""
    ready = [c for c in comments if isinstance(c, dict) and _READY.match(str(c.get("body") or ""))]
    if not ready:
        return "it has no Ready @ <sha> comment"
    latest = max(ready, key=lambda c: c.get("createdAt") or "")
    sha = _READY.match(str(latest.get("body") or ""))[1].lower()
    if head and not head.lower().startswith(sha):
        return f"its latest Ready names {sha[:12]}, not the PR's head {head[:12]}, so the head has no Ready"
    said = _GAPS.findall(str(latest.get("body") or ""))
    if not said:
        return "its latest Ready doesn't say which gap checks it ran (no 'Gap checks:' line)"
    if not said[-1].strip():
        return "its latest Ready's 'Gap checks:' line couldn't be read (nothing after it on that line)"
    if said[-1].rstrip(".").strip().casefold() != "none":
        return f"its latest Ready lists gap checks ({said[-1]}), which CI doesn't run"
    return ""


def refresh(cwd: Path, s: dict, pr: int, wait_minutes: float = 30.0, poll: float = 20.0) -> dict:
    """A mechanical refresh with no builder: in the PR's builder worktree, merge origin/<base> (always a merge commit,
    so the owner's yes can carry over). Clean: push (never forced), wait for the PR's CI on the new head, post
    `Ready @ <sha>` and return it. A conflict: abort the merge, change nothing, and say the builder is needed."""
    top = core.checkout_of(cwd)[0]
    data = json.loads(_gh(top, "pr", "view", str(pr), "--json", "state,headRefOid,headRefName,baseRefName,comments")
                      or "{}")
    if data.get("state") != "OPEN":
        raise Refused(f"PR #{pr} is {(data.get('state') or 'unknown').lower()}, not open")
    if gaps := ready_gaps(data.get("comments") or [], data.get("headRefOid") or ""):
        raise Refused(f"PR #{pr}: {gaps}, so CI is not the full proof: the builder refreshes. Nothing was changed.")
    branch, base = data.get("headRefName") or "", data.get("baseRefName") or settings.base_of(s, top)
    tree = builder_worktree(top, branch)
    head = core.git(tree, "rev-parse", "HEAD")
    if core.git(tree, "status", "--porcelain", "--untracked-files=no"):
        raise Refused(f"{core.posix(tree)} has uncommitted changes: the builder is needed. Nothing was changed.")
    if head != (data.get("headRefOid") or "").lower():
        raise Refused(f"{core.posix(tree)} is at {head[:12]}, but PR #{pr}'s head is "
                      f"{(data.get('headRefOid') or '?')[:12]}: the builder is needed. Nothing was changed.")
    ref = _fetched(tree, base)
    ci_files = _touches(core.git(tree, "diff", "--name-only", f"{ref}...{head}").splitlines(),
                        full_run_paths(tree, ref, head))
    if ci_files:
        raise Refused(f"PR #{pr} changes what its CI runs ({', '.join(ci_files)}), so its own green CI is not the full "
                      "proof: the builder refreshes. Nothing was changed.")
    if not core.run(["git", "-C", str(tree), "merge-base", "--is-ancestor", ref, head]).returncode:
        return {"ok": True, "pr": pr, "sha": head, "refreshed": f"already has the latest {base}: nothing to do"}
    merged = core.run(["git", "-C", str(tree), "merge", "--no-ff", "--no-edit", ref], core.WRITE_TIMEOUT)
    if merged.returncode:
        clash = core.git(tree, "diff", "--name-only", "--diff-filter=U", check=False).split()
        core.run(["git", "-C", str(tree), "merge", "--abort"])
        raise Refused(f"PR #{pr}: merging {ref} conflicts" + (f" in {', '.join(clash)}" if clash else "")
                      + ". The merge was aborted and nothing changed: the builder is needed.")
    new = core.git(tree, "rev-parse", "HEAD")
    pushed = core.run(["git", "-C", str(tree), "push", "origin", f"HEAD:{branch}"], core.WRITE_TIMEOUT)
    if pushed.returncode:
        core.git(tree, "reset", "--keep", head)
        raise KitError(f"The push of {new[:12]} failed, so the merge was undone: {pushed.stderr.strip()[-300:]}")
    deadline, why, seen = _now() + wait_minutes * 60, "its checks never started", None
    while True:  # a verdict counts only when two polls in a row agree, so a workflow not queued yet isn't missed
        view = json.loads(_gh(top, "pr", "view", str(pr), "--json", "headRefOid,statusCheckRollup") or "{}")
        rollup = view.get("statusCheckRollup") or []
        if (view.get("headRefOid") or "").lower() == new and rollup:
            why = ci_green(rollup)
            verdict = (why, json.dumps(rollup, sort_keys=True))
            if why != "its checks are still running" and verdict == seen:
                break
            seen = verdict
        else:
            seen = None
        if _now() >= deadline:
            why = f"CI did not finish within {wait_minutes:g} minutes ({why})"
            break
        _sleep(poll)
    if why:
        raise Refused(f"PR #{pr}: pushed the clean merge {new[:12]}, but {why}. No Ready posted: the builder is needed.")
    _gh(top, "pr", "comment", str(pr), "--body",
        f"Ready @ {new}\n\nwk refresh: a clean merge of {ref}, with no builder and nothing resolved by hand. "
        f"CI green on {new[:12]}. Local checks were not re-run: CI is the full proof.\n\nGap checks: none")
    return {"ok": True, "pr": pr, "sha": new, "refreshed": f"clean merge of {ref}, CI green, Ready posted"}
