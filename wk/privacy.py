"""The privacy guard: private names never reach a public repo.

The private name list is built on this laptop and never written to any repo: every repo's name, folder and client in
the settings file, the owner's and approver's names, plus `[privacy] terms = [...]` there for names only the owner
knows (an employer, people, tickets). The names of public repos themselves are left out. It is checked:

    push    git's pre-push hook in each public checkout (nh setup installs it): the branch name, each pushed commit's
            message and every line it adds. A match stops the push, naming the file and line, never the name.
    pr      a job on GitHub, from the list held in the repo secret PRIVATE_TERMS: the PR's title, description, branch,
            commit messages and every line its diff adds; it fails without printing the name. Without the secret (a
            PR from a fork gets none) it says "not checked" and passes: nh gate fresh is the check at merge.
    sweep   a daily scheduled job on GitHub, from the same secret: every tracked file, the last days' commits, and
            open and recent issues, PRs, comments and reviews, so text that arrived another way (a web edit, an issue,
            another laptop) is found too. A hit fails the run, naming only where.
    scan    any folder, e.g. the clean public tree before it is pushed.
    tree    builds that tree: every file on main except the list (.work/), for one clean commit with no history.
    secret  sets PRIVATE_TERMS on a GitHub repo from this laptop's list (via gh, never printed), and records the
            list's fingerprint (salted and slow to guess, never the names) in the repo variable
            PRIVATE_TERMS_FINGERPRINT, so nh doctor can tell when this laptop's list has moved on.

One scanner (change_hits) reads commits and the lines they add for the push hook, the PR job, the sweep and nh gate
fresh, which also checks a public repo's PR title, description, comments and reviews (turns.py).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import secrets
import subprocess
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import core, settings
from .core import KitError

MIN = 3  # shorter names match too much ordinary text
ZERO = "0" * 40
HOOK_MARK = "# nearly-headless: the privacy guard"
FINGERPRINT = "PRIVATE_TERMS_FINGERPRINT"  # the repo variable holding the fingerprint of the list in PRIVATE_TERMS
SWEEP_DAYS = 8  # how far back the daily sweep reads commits and GitHub text: a day's run, with a week's margin


def terms(s: dict, extra: list[str] = ()) -> list[str]:
    """This laptop's private names, longest first, without the public repos' own names."""
    found, public = set(), set()
    for name, info in (s.get("repos") or {}).items():
        names = {name, Path(info.get("path", "")).name, str(info.get("client") or "")}
        (public if settings.is_public(info) else found).update(names)
        if settings.is_public(info) and info.get("list"):
            public.add(Path(info["list"]).name)
    for key in ("owner", "approver"):
        found.update(str(s.get(key) or "").split())
    found.update(str(t) for t in ((s.get("privacy") or {}).get("terms") or []))
    found.update(extra)
    public_low = {p.lower() for p in public}
    return sorted({t.strip() for t in found if len(t.strip()) >= MIN and t.strip().lower() not in public_low},
                  key=lambda t: (-len(t), t.lower()))


def pattern(names: list[str]) -> re.Pattern | None:
    """One case-blind pattern: whole words, with a dash, underscore or space in a name matching any of them or none
    (so "acme-co" also finds "Acme Co" and "AcmeCo")."""
    if not names:
        return None
    parts = [r"[-_ ]?".join(re.escape(w) for w in re.split(r"[-_ ]+", n) if w) for n in names]
    return re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(parts) + r")(?![A-Za-z0-9])", re.IGNORECASE)


def lines_with(text: str, pat: re.Pattern | None) -> list[int]:
    """The 1-based numbers of the lines of text a private name is on."""
    if pat is None:
        return []
    return [i for i, line in enumerate((text or "").splitlines(), 1) if pat.search(line)]


# ---------------------------------------------------------------- what a change publishes

def _git(top: Path, *args: str) -> str:
    return core.git(top, *args, check=False)


def _commits(top: Path, local: str, remote: str) -> list[str]:
    span = [local, "--not", "--remotes"] if remote == ZERO else [f"{remote}..{local}"]
    return [c for c in _git(top, "rev-list", *span).splitlines() if c]


def _show(top: Path, c: str) -> str:
    return _git(top, "show", "--format=", "-U0", "--no-color", "--no-ext-diff", c)


def change_hits(commits: list[tuple[str, str]], diff: str, pat: re.Pattern | None, suffix: str = "") -> list[str]:
    """The one scanner for what a change publishes: each commit's message (sha, message) and each line the unified
    diff adds (with `suffix` after each file place). Places only, never the name."""
    hits = []
    for sha, message in commits:
        hits += [f"commit {sha[:7]}'s message, line {n}" for n in lines_with(message, pat)]
    return hits + [where + suffix for where in added_hits(diff, pat)]


def push_hits(top: Path, refs: str, pat: re.Pattern | None) -> list[str]:
    """What a push would publish that holds a private name, from git's pre-push lines (local ref, local sha, remote
    ref, remote sha): the branch, each commit's message, each added line. Places only, never the name."""
    hits = []
    for line in refs.splitlines():
        parts = line.split()
        if len(parts) != 4 or parts[1] == ZERO:
            continue  # a deletion publishes nothing
        _, local, remote_ref, remote = parts
        branch = remote_ref.removeprefix("refs/heads/").removeprefix("refs/tags/")
        if lines_with(branch, pat):
            hits.append("the name of the " + ("tag" if remote_ref.startswith("refs/tags/") else "branch") + " pushed")
        for c in _commits(top, local, remote):
            hits += change_hits([(c, _git(top, "log", "-1", "--format=%B", c))], _show(top, c), pat,
                                f" (commit {c[:7]})")
    return hits


def branch_hits(top: Path, base: str, head: str, pat: re.Pattern | None) -> tuple[list[str], int]:
    """A PR's commits (base..head) and its diff (base...head, what merging it adds), through change_hits: (places,
    commits read). A line added and taken out again in a later commit is not in the diff, so it no longer blocks.
    KitError when git can't read them (the PR's history isn't fetched)."""
    log = core.run(["git", "-C", str(top), "log", "--format=%H%x1f%B%x1e", f"{base}..{head}"], 120)
    diff = core.run(["git", "-C", str(top), "diff", "-U0", "--no-color", "--no-ext-diff", f"{base}...{head}"], 120)
    if not (base and head) or log.returncode or diff.returncode:
        raise KitError("could not read the PR's commits and diff (check out with fetch-depth: 0)")
    commits = [tuple(x.strip("\n").split("\x1f", 1)) for x in log.stdout.split("\x1e") if "\x1f" in x]
    return change_hits(commits, diff.stdout, pat), len(commits)


def added_hits(diff: str, pat: re.Pattern | None) -> list[str]:
    """`<file> line <n>` for each added line of a unified diff that holds a private name, and each file whose new
    path does."""
    hits, path, at = [], "", 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[4:].removeprefix("b/")
            if lines_with(path, pat):
                hits.append(f"the file name {path}")
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            at = int(m[1]) if m else 0
        elif line.startswith("+"):
            if lines_with(line[1:], pat):
                hits.append(f"{path} line {at}")
            at += 1
    return hits


# ---------------------------------------------------------------- a folder

def scan(folder: Path, pat: re.Pattern | None) -> list[str]:
    """`<file> line <n>` for every line of every text file in a folder (git's own folder left out) that holds a
    private name, and each path that does."""
    hits = []
    for path in sorted(p for p in folder.rglob("*") if ".git" not in p.relative_to(folder).parts):
        rel = path.relative_to(folder).as_posix()
        if lines_with(rel, pat):
            hits.append(f"the path {rel}")
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary: nothing to read
        hits += [f"{rel} line {n}" for n in lines_with(text, pat)]
    return hits


# ---------------------------------------------------------------- on GitHub: each PR

def pr_hits(pr: dict, pat: re.Pattern | None) -> list[str]:
    """The PR's own text: its title, description and branch name (as GitHub's pull_request event has them)."""
    hits = ["the PR title" for _ in lines_with(pr.get("title") or "", pat)][:1]
    hits += [f"the PR description, line {n}" for n in lines_with(pr.get("body") or "", pat)]
    hits += ["the branch name" for _ in lines_with((pr.get("head") or {}).get("ref") or "", pat)][:1]
    return hits


def ci_names(env: dict) -> list[str]:
    """The names a GitHub job checks: PRIVATE_TERMS, one per line."""
    return [t.strip() for t in (env.get("PRIVATE_TERMS") or "").splitlines() if len(t.strip()) >= MIN]


def ci(env: dict = os.environ, cwd: Path = Path(".")) -> tuple[int, str]:
    """The PR job: (exit code, what to print)."""
    names = ci_names(env)
    if not names:
        return 0, "Privacy check: not checked (no private name list here; a PR from a fork gets no secrets). " \
                  "nh gate fresh checks it before merge."
    try:
        event = json.loads(Path(env.get("GITHUB_EVENT_PATH", "")).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 1, "Privacy check: could not read the pull request event"
    pr = event.get("pull_request") or {}
    base, head = (pr.get("base") or {}).get("sha", ""), (pr.get("head") or {}).get("sha", "")
    pat = pattern(names)
    try:
        found, count = branch_hits(cwd, base, head, pat)
    except KitError as exc:
        return 1, f"Privacy check: {exc}"
    hits = pr_hits(pr, pat) + found
    if hits:
        return 1, "Privacy check failed: a private name is in " + "; ".join(hits) + ". Reword it; the name is not shown."
    return 0, f"Privacy check: passed ({count} commits, the lines they add, title and description)"


# ---------------------------------------------------------------- on GitHub: the daily sweep

def _api(path: str) -> list:
    """Every item of a GitHub list (all pages), through gh with the job's token. KitError when GitHub won't answer."""
    done = core.run(["gh", "api", "--paginate", path], 120)
    if done.returncode:
        raise KitError(f"could not read {path.split('?')[0]} from GitHub: " + done.stderr.strip()[-200:])
    items, text, at = [], done.stdout.strip(), 0
    decoder = json.JSONDecoder()
    while at < len(text):  # --paginate prints one JSON array per page, back to back
        page, at = decoder.raw_decode(text, at)
        items += page if isinstance(page, list) else [page]
        while at < len(text) and text[at].isspace():
            at += 1
    return items


def github_hits(repo: str, since: str, pat: re.Pattern | None, api=_api) -> list[str]:
    """Open issues and PRs, and those changed since `since`: titles and descriptions, and a changed PR's reviews;
    comments and review comments on code since then. Places only (the number or the comment's link), never the
    name."""
    hits, seen = [], set()
    q = f"per_page=100&since={since}"
    for item in api(f"repos/{repo}/issues?state=open&per_page=100") + api(f"repos/{repo}/issues?state=all&{q}"):
        n = item.get("number")
        if n in seen:
            continue
        seen.add(n)
        kind = "PR" if item.get("pull_request") else "issue"
        hits += [f"{kind} #{n}'s title" for _ in lines_with(item.get("title") or "", pat)][:1]
        hits += [f"{kind} #{n}'s description, line {x}" for x in lines_with(item.get("body") or "", pat)]
        if kind == "PR" and (item.get("updated_at") or "") >= since:
            for r in api(f"repos/{repo}/pulls/{n}/reviews?per_page=100"):
                if lines_with(r.get("body") or "", pat):
                    hits.append(f"a review on PR #{n} ({r.get('html_url') or r.get('id')})")
    for kind, path in (("comment", "issues/comments"), ("review comment", "pulls/comments")):
        for c in api(f"repos/{repo}/{path}?{q}"):
            if lines_with(c.get("body") or "", pat):
                hits.append(f"a {kind} ({c.get('html_url') or c.get('id')})")
    return hits


def tracked_hits(top: Path, pat: re.Pattern | None) -> list[str]:
    """`<file> line <n>` for each tracked file (git ls-files) that holds a private name, and each path that does."""
    hits = []
    for rel in sorted(f for f in _git(top, "-c", "core.quotepath=false", "ls-files", "-z").split("\0") if f):
        if lines_with(rel, pat):
            hits.append(f"the path {rel}")
        try:
            text = (top / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary, or a submodule: nothing to read
        hits += [f"{rel} line {n}" for n in lines_with(text, pat)]
    return hits


def recent_hits(top: Path, since: str, pat: re.Pattern | None) -> tuple[list[str], int]:
    """The commits on the checked-out branch since `since`: their messages and the lines they add (change_hits)."""
    hits, shas = [], [c for c in _git(top, "rev-list", f"--since={since}", "HEAD").splitlines() if c]
    for c in shas:
        hits += change_hits([(c, _git(top, "log", "-1", "--format=%B", c))], _show(top, c), pat, f" (commit {c[:7]})")
    return hits, len(shas)


def sweep(env: dict = os.environ, cwd: Path = Path("."), api=_api, now: datetime | None = None) -> tuple[int, str]:
    """The daily job: (exit code, what to print). No list: passes as "not checked", unless a fingerprint is recorded
    (then the list was set and is gone, which fails)."""
    names = ci_names(env)
    if not names:
        if env.get(FINGERPRINT):
            return 1, f"Privacy sweep: PRIVATE_TERMS is empty but {FINGERPRINT} is set: run nh privacy secret again"
        return 0, "Privacy sweep: not checked (no PRIVATE_TERMS secret on this repo)"
    pat, repo = pattern(names), env.get("GITHUB_REPOSITORY", "")
    since = ((now or datetime.now(timezone.utc)) - timedelta(days=SWEEP_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        files = tracked_hits(cwd, pat)
        commits, count = recent_hits(cwd, since, pat)
        text = github_hits(repo, since, pat, api) if repo else []
    except KitError as exc:
        return 1, f"Privacy sweep: {exc}"
    hits = files + commits + text
    if hits:
        return 1, "Privacy sweep failed: a private name is in " + "; ".join(hits) + ". Reword or remove each; " \
                  "the name is not shown."
    return 0, (f"Privacy sweep: passed (tracked files, {count} commits and GitHub text since {since[:10]}"
               + ("" if repo else "; GitHub text not read: no GITHUB_REPOSITORY") + ")")


# ---------------------------------------------------------------- keeping the secret in step

def fingerprint(names: list[str], salt: str = "") -> str:
    """A list's fingerprint, `<salt>$<hash>`: the same names (in any order or case) give the same hash for one salt,
    and the salt with a slow hash keeps the names from being guessed from it."""
    salt = salt or secrets.token_hex(8)
    words = "\n".join(sorted({n.strip().lower() for n in names if len(n.strip()) >= MIN}))
    return salt + "$" + hashlib.pbkdf2_hmac("sha256", words.encode("utf-8"), salt.encode("utf-8"), 200_000).hex()


def matches(names: list[str], recorded_value: str) -> bool:
    """True when this list is the one the fingerprint was made from."""
    salt = (recorded_value or "").strip().partition("$")[0]
    return bool(salt) and fingerprint(names, salt) == recorded_value.strip()


def recorded(repo: str) -> str | None:
    """The fingerprint recorded on a GitHub repo ("" when none is), or None when gh can't say."""
    try:
        done = core.run(["gh", "variable", "get", FINGERPRINT, "--repo", repo], 30)
    except OSError:
        return None
    if done.returncode:
        return "" if "not found" in (done.stderr or "").lower() else None
    return done.stdout.strip()


def github_repo(top: Path) -> str:
    """owner/repo of a checkout's origin on GitHub; "" when it has none."""
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", _git(top, "remote", "get-url", "origin"))
    return m[1] if m else ""


def _gh_set(args: list[str], value: str, what: str) -> None:
    """One gh write whose value goes through standard input, never the command line, so it is never printed."""
    try:
        done = subprocess.run(["gh", *args], input=value, capture_output=True, text=True, encoding="utf-8", timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise KitError(f"gh {what} failed: {exc}") from exc
    if done.returncode:
        raise KitError(f"gh {what} failed: " + done.stderr.strip()[-300:])


def set_secret(repo: str, names: list[str]) -> dict:
    """Set the repo secret PRIVATE_TERMS from this laptop's list, then record that list's fingerprint in the repo
    variable PRIVATE_TERMS_FINGERPRINT (the names never, the fingerprint only)."""
    _gh_set(["secret", "set", "PRIVATE_TERMS", "--repo", repo], "\n".join(names) + "\n", "secret set")
    _gh_set(["variable", "set", FINGERPRINT, "--repo", repo], fingerprint(names), "variable set")
    return {"repo": repo, "secret": "PRIVATE_TERMS", "names": len(names), "fingerprint": FINGERPRINT}


# ---------------------------------------------------------------- the hook

def hook_path(top: Path) -> Path:
    return core.git_path(top, "hooks") / "pre-push"


def hook_text(launcher: Path) -> str:
    return (f"#!/bin/sh\n{HOOK_MARK}: a push that would publish a private name stops here (nh setup keeps this "
            f"file).\nexec sh \"{core.posix(launcher)}\" privacy push \"$@\"\n")  # sh: bin/nh needs no exec bit


def install_hook(top: Path, launcher: Path) -> dict:
    """Put the guard in a public checkout's pre-push hook (shared by its worktrees). A pre-push hook that isn't ours
    is never replaced."""
    path = hook_path(top)
    text = hook_text(launcher)
    if path.is_file():
        old = path.read_text(encoding="utf-8", errors="replace")
        if HOOK_MARK not in old:
            return {"installed": False, "file": core.posix(path),
                    "why": "it already has its own pre-push hook; add the line: " + text.splitlines()[-1]}
        if old == text:
            return {"installed": True, "changed": False, "file": core.posix(path)}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    try:
        path.chmod(0o755)
    except OSError:
        pass
    return {"installed": True, "changed": True, "file": core.posix(path)}


def hook_installed(top: Path) -> bool:
    path = hook_path(top)
    return path.is_file() and HOOK_MARK in path.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------- the clean public tree

def tree(top: Path, out: Path, ref: str = "origin/main") -> dict:
    """The public copy of a repo: every file on `ref` except the list (`.work/`), written to an empty folder `out`,
    ready to be one clean commit with no history."""
    if out.exists() and any(out.iterdir()):
        raise KitError(f"{core.posix(out)} is not empty; give a new folder")
    try:
        done = subprocess.run(["git", "-C", str(top), "-c", "core.autocrlf=false", "archive", "--format=tar", ref],
                              capture_output=True, timeout=120)  # the repo's own bytes, not this laptop's line endings
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise KitError(f"git archive {ref}: {exc}") from exc
    if done.returncode:
        raise KitError(f"git archive {ref}: " + done.stderr.decode("utf-8", "replace").strip()[-300:])
    out.mkdir(parents=True, exist_ok=True)
    kept = 0
    with tarfile.open(fileobj=io.BytesIO(done.stdout)) as tar:
        for m in tar.getmembers():
            name = m.name.removeprefix("./")
            if name == ".work" or name.startswith(".work/") or not m.isfile():
                continue
            target = out / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(tar.extractfile(m).read())
            if m.mode & 0o111:
                target.chmod(0o755)
            kept += 1
    return {"folder": core.posix(out), "ref": ref, "files": kept}
