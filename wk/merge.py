"""`nh merge --pr N --sha S`: the merge sequence (skills/supervisor/merge.md) as one command, for any repo.

Each step runs only when the one before it passed; the first that fails stops the run with a plain reason (exit 1),
so a skipped gate or a merge at the wrong commit can't happen:

    approve   post "Approved by the owner: merge @ S" on the PR (once; a re-run finds it)
    gate      nh gate fresh at S, in-process; its verdict is printed
    ci        every check on the head the gate passed completed green
    version   with --release vX.Y.Z: the repo's version file says X.Y.Z at S
    merge     gh pr merge N --merge --match-head-commit <head> (never squash or rebase)
    same      the merge commit's second parent is that head, and every file the PR changes is exactly as approved
    teardown  the PR branch's worktree removed (if any); git branch -d after an ancestor check (never -D); the remote
              branch deleted, confirmed with ls-remote
    piece     with --piece P3: the piece set to merged, approved at S
    tag       with --release: tag vX.Y.Z on the merge commit (by its id, never HEAD) and push the tag
    release   with --release: the repo's release steps: [repos.<name>] release = ["update", "command-center"]
              (update: nh update on this laptop; command-center: wait up to 2 minutes for the command center on port
              8765 to show the new version). The kit's own repo gets both when its settings name none.

--dry-run prints the steps it would run, and does none of them. nh merge never deploys to production.
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from pathlib import Path

from . import core, settings, turns
from .core import KitError
from .turns import Refused

APPROVAL = "Approved by {who}: merge @ {sha}"
VERSION_FILES = ("wk/__init__.py", "package.json", "pyproject.toml", "VERSION")  # looked for in this order
KIT_RELEASE = ["update", "command-center"]
RELEASE_STEPS = {"update", "command-center"}
PORT = 8765
WAIT = 120.0  # seconds the command center gets to show the new version
TAG = re.compile(r"v(\d+\.\d+\.\d+)")


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _view(top: Path, pr: int) -> dict:
    return json.loads(turns._gh(top, "pr", "view", str(pr), "--json",
                                "state,headRefOid,headRefName,baseRefName,url,statusCheckRollup,comments,"
                                "mergeCommit") or "{}")


def release_steps(s: dict, top: Path) -> list[str]:
    """The release steps the repo's settings name; the kit's own repo, with none named, updates this laptop and
    waits for the command center."""
    _, info = settings.info_of(s, top)
    steps = info.get("release")
    if steps is None:
        kit = settings.kit(s)
        try:
            mine = kit is not None and kit.is_dir() and core.same_path(core.git_common_dir(kit), core.git_common_dir(top))
        except KitError:
            mine = False
        return list(KIT_RELEASE) if mine else []
    if not isinstance(steps, list) or any(x not in RELEASE_STEPS for x in steps):
        raise KitError(f"release in the repo's settings must be a list of {', '.join(sorted(RELEASE_STEPS))}")
    return steps


def version_at(top: Path, sha: str, info: dict) -> tuple[str, str]:
    """(the version file, its text at sha): the repo's `version_file` setting, else the first of VERSION_FILES."""
    named = info.get("version_file")
    for path in [named] if named else VERSION_FILES:
        text = core.run(["git", "-C", str(top), "show", f"{sha}:{path}"])
        if not text.returncode:
            return path, text.stdout
    raise Refused(f"No version file at {sha[:12]} ({named or ', '.join(VERSION_FILES)}): name it with version_file "
                  "in the repo's settings")


def worktree_of(top: Path, branch: str) -> tuple[Path | None, bool]:
    """(the worktree that has `branch` checked out, or None; whether that is the repo's main checkout)."""
    rows = [r for r in core.git(top, "worktree", "list", "--porcelain").split("\n\n") if r.strip()]
    for i, r in enumerate(rows):
        if f"branch refs/heads/{branch}" in r.splitlines():
            return Path(r.splitlines()[0].removeprefix("worktree ")), i == 0
    return None, False


def command_center(version: str, port: int = PORT, wait: float = WAIT, every: float = 5.0) -> str:
    """Wait up to `wait` seconds for the command center to show `version`."""
    seen, until = "nothing answered", time.monotonic() + wait
    while True:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api", timeout=15) as reply:
                seen = json.loads(reply.read().decode("utf-8")).get("kit", {}).get("version", "") or "no version"
        except (OSError, ValueError):
            seen = "nothing answered"
        if seen == version:
            return f"the command center on port {port} shows {version}"
        if time.monotonic() >= until:
            raise Refused(f"the command center on port {port} still shows {seen}, not {version}, after "
                          f"{int(wait)} seconds: start it with nh serve, or check nh update's output")
        _sleep(every)


def plan(pr: int, sha: str, piece: str, release: str, steps: list[str]) -> list[str]:
    """The steps a run takes, in order, as --dry-run prints them."""
    out = [f"approve: post '{APPROVAL.format(who=settings.APPROVER, sha=sha)}' on PR #{pr}",
           f"gate: nh gate fresh --pr {pr} --sha {sha}; stop if it refuses",
           "ci: every check on the head the gate passed is green; stop if not"]
    if release:
        out.append(f"version: the version file says {release[1:]} at {sha}; stop if not")
    out += [f"merge: gh pr merge {pr} --merge --match-head-commit <that head>",
            "same: the merge commit's second parent is that head, and the PR's files are exactly as approved",
            "teardown: remove the branch's worktree, git branch -d after an ancestor check, delete the remote branch "
            "and confirm with ls-remote"]
    if piece:
        out.append(f"piece: set {piece} to merged, approved at {sha}")
    if release:
        out.append(f"tag: git tag {release} <merge commit>; git push origin {release}")
        out += [f"release: {x}" for x in steps] or ["release: no release steps in the repo's settings"]
    return out + ["never: no production deploy"]


class Run:
    def __init__(self):
        self.lines: list[str] = []

    def ok(self, step: str, detail: str) -> None:
        self.lines.append(f"ok    {step}: {detail}")

    def stop(self, step: str, why: str):
        self.lines.append(f"STOP  {step}: {why}")
        raise Refused("\n".join(self.lines))


def _find_piece(s: dict, top: Path, pid: str, pr: int, product: str) -> tuple:
    """(work.Repo, product) of the piece `pid` on the repo's lists: the one --list names, else the one whose piece
    records this PR."""
    from . import work
    name, _ = settings.info_of(s, top)
    if not name:
        raise KitError("--piece needs the repo in this laptop's settings")
    r = work.Repo(s, name)
    lists = [product] if product else list(r.products())
    hits = [p for p in lists for x in work.pieces(r, p)
            if x["id"].upper() == pid.upper() and (product or str(pr) in re.findall(r"\d+", x["pr"]))]
    if len(hits) != 1:
        raise KitError(f"{pid} recording PR #{pr} is on {len(hits)} lists: name the list with --list")
    return r, hits[0]


def merge(cwd: Path, s: dict, pr: int, sha: str, piece: str = "", release: str = "", product: str = "",
          dry_run: bool = False, where_settings: Path | None = None) -> str:
    """Run the sequence; returns the lines printed. Refused (exit 1) at the first failing step, with every line so
    far."""
    top = core.checkout_of(cwd)[0]
    sha = (sha or "").lower()
    if len(sha) < 7 or not re.fullmatch(r"[0-9a-f]+", sha):
        raise KitError("--sha needs the commit the owner approved, 7 or more characters")
    if release and not TAG.fullmatch(release):
        raise KitError("--release needs the tag, e.g. v1.2.3")
    steps = release_steps(s, top) if release else []
    where_settings = where_settings or settings.path()
    if dry_run:
        return "\n".join(["Dry run: nothing is done."] + plan(pr, sha, piece, release, steps))
    run = Run()
    data = _view(top, pr)
    where = "/".join((data.get("url") or "").split("/")[3:5]) or core.posix(top)
    if data.get("state") != "OPEN":
        run.stop("approve", f"{where} PR #{pr} is {(data.get('state') or 'unknown').lower()}, not open")

    line = APPROVAL.format(who=settings.APPROVER, sha=sha)
    if any(line.casefold() in (c.get("body") or "").casefold() for c in data.get("comments") or []):
        run.ok("approve", f"'{line}' is already on PR #{pr}")
    else:
        turns._gh(top, "pr", "comment", str(pr), "--body", line)
        run.ok("approve", f"posted '{line}' on PR #{pr}")

    try:
        gate = turns.gate_fresh(top, s, pr, sha)
    except KitError as exc:
        run.stop("gate", str(exc))
    head = str(gate.get("head", ""))
    run.ok("gate", "passed: " + json.dumps(gate))
    data = _view(top, pr)
    full = (data.get("headRefOid") or "").lower()
    if not full.startswith(head):
        run.stop("ci", f"PR #{pr}'s head moved to {full[:12]} after the gate passed {head}: run nh merge again")
    why = turns.ci_green(data.get("statusCheckRollup") or [])
    if why:
        run.stop("ci", f"not green on {full[:12]}: {why}")
    run.ok("ci", f"green on {full[:12]}")
    if not full.startswith(sha):
        turns._gh(top, "pr", "comment", str(pr), "--body",
                  f"Approved by {settings.APPROVER} @ {sha}, carried over to {full[:12]} (clean main merge, CI green)")

    if release:
        path, text = version_at(top, sha, settings.info_of(s, top)[1])
        number = release[1:]
        if not re.search(rf"""["']{re.escape(number)}["']|^{re.escape(number)}\s*$""", text, re.M):
            run.stop("version", f"{path} at {sha[:12]} doesn't say {number}")
        run.ok("version", f"{path} says {number} at {sha[:12]}")

    try:
        turns._gh(top, "pr", "merge", str(pr), "--merge", "--match-head-commit", full)
    except KitError as exc:
        run.stop("merge", str(exc))
    after = _view(top, pr)
    commit = ((after.get("mergeCommit") or {}).get("oid") or "").lower()
    if after.get("state") != "MERGED" or not commit:
        run.stop("merge", f"PR #{pr} is {(after.get('state') or 'unknown').lower()} with no merge commit after gh pr "
                          "merge: look on GitHub before anything else")
    run.ok("merge", f"PR #{pr} merged at {full[:12]} as {commit[:12]}")

    base = data.get("baseRefName") or settings.base_of(s, top)
    core.git(top, "fetch", "--quiet", "origin", timeout=core.WRITE_TIMEOUT)
    parents = core.git(top, "rev-list", "--parents", "-n", "1", commit).split()[1:]
    if parents[1:2] != [full]:
        run.stop("same", f"{commit[:12]} is not a merge of {full[:12]} (parents {', '.join(p[:12] for p in parents)})")
    fork = core.git(top, "merge-base", parents[0], full)
    files = [f for f in core.git(top, "diff", "--name-only", "--no-renames", fork, full).splitlines()
             if f and not f.startswith(".work/")]
    if files and core.git(top, "diff", "--name-only", full, commit, "--", *files):
        run.stop("same", f"git diff {full[:12]} {commit[:12]} is not empty on the PR's files: look before anything "
                         "else")
    run.ok("same", f"git diff {full[:12]} {commit[:12]} is empty on the PR's {len(files)} file(s)")

    branch = data.get("headRefName") or ""
    try:
        run.ok("teardown", _teardown(top, branch, base))
    except KitError as exc:
        run.stop("teardown", str(exc))

    if piece:
        from . import work
        try:
            r, lst = _find_piece(s, top, piece, pr, product)
            done = work.set_piece(r, lst, piece.upper(), state="merged", approved=sha)
        except KitError as exc:
            run.stop("piece", str(exc))
        run.ok("piece", f"{piece.upper()} on {r.name} {lst} is merged, approved at {sha}"
               + (f" ({done['push']})" if str(done.get("push", "")).startswith("push failed") else ""))

    if release:
        try:
            have = core.git(top, "rev-parse", "--verify", "--quiet", f"refs/tags/{release}^{{commit}}", check=False)
            if have and have != commit:
                raise Refused(f"{release} already names {have[:12]}, not {commit[:12]}")
            if not have:
                core.git(top, "tag", release, commit)
            core.git(top, "push", "--quiet", "origin", f"refs/tags/{release}", timeout=core.WRITE_TIMEOUT)
        except KitError as exc:
            run.stop("tag", str(exc))
        run.ok("tag", f"{release} on {commit[:12]}, pushed")
        for step in steps:
            try:
                run.ok("release", _release(step, release[1:], where_settings))
            except (KitError, OSError) as exc:
                run.stop("release", f"{step}: {exc}")
    return "\n".join(run.lines + ["Not done: no production deploy (nh merge never deploys)."])


def _teardown(top: Path, branch: str, base: str) -> str:
    if not branch or branch == base:
        raise Refused(f"the PR's branch is '{branch or 'unknown'}', so there is nothing safe to delete")
    said = []
    tree, main = worktree_of(top, branch)
    if tree is not None and main:
        raise Refused(f"{branch} is checked out in the repo's main checkout, which nh merge never touches: switch it "
                      "there yourself, then delete the branch")
    tip = core.git(top, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
    if tip and core.run(["git", "-C", str(top), "merge-base", "--is-ancestor", tip, f"origin/{base}"]).returncode:
        raise Refused(f"{branch} has commits that aren't in origin/{base}: kept, with its worktree (no git branch -D)")
    if tree is not None:
        if core.git(tree, "status", "--porcelain"):
            raise Refused(f"{core.posix(tree)} has uncommitted changes: kept, with its branch")
        core.git(top, "worktree", "remove", str(tree))
        said.append(f"removed worktree {core.posix(tree)}")
    if tip:
        core.git(top, "branch", "-d", branch)
        said.append(f"git branch -d {branch}")
    if core.git(top, "ls-remote", "--heads", "origin", branch, timeout=core.WRITE_TIMEOUT):
        core.git(top, "push", "--quiet", "origin", "--delete", branch, timeout=core.WRITE_TIMEOUT)
        if core.git(top, "ls-remote", "--heads", "origin", branch, timeout=core.WRITE_TIMEOUT):
            raise Refused(f"origin still has {branch} after deleting it")
        said.append(f"deleted origin/{branch} (ls-remote shows none)")
    return "; ".join(said) or f"{branch} was already gone here and on origin"


def _release(step: str, version: str, where: Path) -> str:
    if step == "update":
        from . import upkeep
        result = upkeep.update(where)
        return "nh update: " + json.dumps(result, ensure_ascii=False)[:300]
    return command_center(version)


# ---------------------------------------------------------------- the PreToolUse hook: no raw merge in a supervisor chat

RAW_MERGE = re.compile(r"(?<![\w-])gh(?:\.exe)?\s+pr\s+merge\b", re.I)
ESCAPE = "NH_RAW_MERGE"  # written in the command itself (NH_RAW_MERGE=1 gh pr merge ...): let through, and logged
_ESCAPED = re.compile(ESCAPE + r"""\s*=\s*['"]?1""")
REFUSAL = ("A raw gh pr merge skips the merge sequence (approval, freshness gate, green CI, tear-down). Use "
           "nh merge --pr N --sha S (add --piece P<n>, and --release vX.Y.Z where the repo releases by tag; "
           "--dry-run shows the steps). Only if nh merge can't do it and the owner said so: put " + ESCAPE + "=1 in "
           "the command; that is logged.")


def guard(hook: dict) -> tuple[str, str]:
    """(decision, the command's first line): "deny" for a Bash or PowerShell call that runs gh pr merge in a chat
    that loaded the supervisor skill; "escaped" for one carrying NH_RAW_MERGE=1; "" for everything else (builders and
    other subagents, chats that aren't supervisors, every other command)."""
    from . import reply
    if hook.get("agent_id") or hook.get("tool_name") not in ("Bash", "PowerShell"):
        return "", ""
    command = str((hook.get("tool_input") or {}).get("command") or "")
    if not RAW_MERGE.search(command):
        return "", ""
    transcript = Path(str(hook.get("transcript_path") or ""))
    if not (hook.get("transcript_path") and transcript.is_file() and reply.is_supervisor(transcript)):
        return "", ""
    first = command.strip().splitlines()[0][:200]
    return ("escaped" if _ESCAPED.search(command) else "deny"), first


def guard_log(where: Path, session: str, decision: str, command: str) -> None:
    where.parent.mkdir(parents=True, exist_ok=True)
    with where.open("a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')}\t{session or '-'}\t{decision}\t{command}\n")
