"""The privacy guard: private names never reach a public repo.

The private name list is built on this laptop and never written to any repo: every repo's name, folder and client in
the settings file, the owner's and approver's names, plus `[privacy] terms = [...]` there for names only the owner
knows (an employer, people, tickets). The names of public repos themselves are left out. It is checked:

    push    git's pre-push hook in each public checkout (nh setup installs it): the branch name, each pushed commit's
            message and every line it adds. A match stops the push, naming the file and line, never the name.
    pr      a job on GitHub, from the list held in the repo secret PRIVATE_TERMS: the PR's title, description and
            commit messages; it fails without printing the name. Without the secret (a PR from a fork gets none) it
            says "not checked" and passes: nh gate fresh is the check at merge.
    scan    any folder, e.g. the clean public tree before it is pushed.
    tree    builds that tree: every file on main except the list (.work/), for one clean commit with no history.
    secret  sets PRIVATE_TERMS on a GitHub repo from this laptop's list (via gh, never printed).

nh gate fresh checks a public repo's PR title, description and comments with the same list (turns.py).
"""

from __future__ import annotations

import io
import json
import os
import tarfile
import re
import subprocess
from pathlib import Path

from . import core, settings
from .core import KitError

MIN = 3  # shorter names match too much ordinary text
ZERO = "0" * 40
HOOK_MARK = "# nearly-headless: the privacy guard"
OLD_MARK = "# work-kit: the privacy guard"  # the hook as releases before 1.56.0 wrote it: still ours, replaced


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


# ---------------------------------------------------------------- before a push

def _git(top: Path, *args: str) -> str:
    return core.git(top, *args, check=False)


def _commits(top: Path, local: str, remote: str) -> list[str]:
    span = [local, "--not", "--remotes"] if remote == ZERO else [f"{remote}..{local}"]
    return [c for c in _git(top, "rev-list", *span).splitlines() if c]


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
            short = c[:7]
            for n in lines_with(_git(top, "log", "-1", "--format=%B", c), pat):
                hits.append(f"commit {short}'s message, line {n}")
            hits += [f"{where} (commit {short})" for where in added_hits(_git(top, "show", "--format=", "-U0",
                                                                               "--no-color", "--no-ext-diff", c), pat)]
    return hits


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


# ---------------------------------------------------------------- on GitHub

def pr_hits(event: dict, commits: list[tuple[str, str]], pat: re.Pattern | None) -> list[str]:
    pr = event.get("pull_request") or {}
    hits = ["the PR title" for _ in lines_with(pr.get("title") or "", pat)][:1]
    hits += [f"the PR description, line {n}" for n in lines_with(pr.get("body") or "", pat)]
    hits += ["the branch name" for _ in lines_with((pr.get("head") or {}).get("ref") or "", pat)][:1]
    for sha, message in commits:
        hits += [f"commit {sha[:7]}'s message, line {n}" for n in lines_with(message, pat)]
    return hits


def ci(env: dict = os.environ, cwd: Path = Path(".")) -> tuple[int, str]:
    """The GitHub job: (exit code, what to print). The names come from PRIVATE_TERMS, one per line."""
    names = [t.strip() for t in (env.get("PRIVATE_TERMS") or "").splitlines() if len(t.strip()) >= MIN]
    if not names:
        return 0, "Privacy check: not checked (no private name list here; a PR from a fork gets no secrets). " \
                  "nh gate fresh checks it before merge."
    try:
        event = json.loads(Path(env.get("GITHUB_EVENT_PATH", "")).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 1, "Privacy check: could not read the pull request event"
    pr = event.get("pull_request") or {}
    base, head = (pr.get("base") or {}).get("sha", ""), (pr.get("head") or {}).get("sha", "")
    log = subprocess.run(["git", "log", "--format=%H%x1f%B%x1e", f"{base}..{head}"], cwd=cwd, capture_output=True,
                         text=True, encoding="utf-8")
    if log.returncode:
        return 1, "Privacy check: could not read the PR's commits (check out with fetch-depth: 0)"
    commits = [tuple(x.strip("\n").split("\x1f", 1)) for x in log.stdout.split("\x1e") if "\x1f" in x]
    hits = pr_hits(event, commits, pattern(names))
    if hits:
        return 1, "Privacy check failed: a private name is in " + "; ".join(hits) + ". Reword it; the name is not shown."
    return 0, f"Privacy check: passed ({len(commits)} commits, title and description)"


def set_secret(repo: str, names: list[str]) -> dict:
    """Set the repo secret PRIVATE_TERMS from this laptop's list, through gh's standard input (never printed)."""
    try:
        done = subprocess.run(["gh", "secret", "set", "PRIVATE_TERMS", "--repo", repo], input="\n".join(names) + "\n",
                              capture_output=True, text=True, encoding="utf-8", timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise KitError(f"gh secret set failed: {exc}") from exc
    if done.returncode:
        raise KitError("gh secret set failed: " + done.stderr.strip()[-300:])
    return {"repo": repo, "secret": "PRIVATE_TERMS", "names": len(names)}


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
        if HOOK_MARK not in old and OLD_MARK not in old:
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
