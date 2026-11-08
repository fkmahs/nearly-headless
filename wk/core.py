"""Small helpers shared by every wk module: errors, git, JSON files and a lock in a repo's shared Git folder."""

from __future__ import annotations

import functools
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class WorkkitError(Exception):
    pass


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def posix(path: Path | str) -> str:
    return str(Path(path)).replace("\\", "/")


def same_path(a: Path | str | None, b: Path | str | None) -> bool:
    if not a or not b:
        return False
    return os.path.normcase(os.path.realpath(str(a))) == os.path.normcase(os.path.realpath(str(b)))


GIT_TIMEOUT = 60.0     # seconds; a local git command takes well under one
WRITE_TIMEOUT = 300.0  # seconds for a commit, push or pull: stopping one midway leaves work to undo
GRACE = 5.0            # seconds to collect a stopped command's output before giving up on it
QUIET = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}  # git and its credential helper never ask


def _stop(proc: subprocess.Popen) -> None:
    """Stop a command and everything it started (git starts helpers: git-remote-https, a credential manager, ssh)."""
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=GRACE)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def run(cmd: list[str], timeout: float = GIT_TIMEOUT, env: dict | None = None, **kw) -> subprocess.CompletedProcess:
    """Run a command with no input (it never inherits the caller's, so it can't sit waiting on it), no prompts, and a
    time limit, so one stuck command can't freeze wk or the command center. On the limit the command and everything it
    started are stopped, and it comes back as failed (returncode 124) within a few seconds even when a helper it
    started still holds its output open."""
    group = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" \
        else {"start_new_session": True}
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", env=(env or os.environ) | QUIET, **group, **kw)
    try:
        out, err = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
    except subprocess.TimeoutExpired:
        _stop(proc)
        try:
            out, _ = proc.communicate(timeout=GRACE)
        except subprocess.TimeoutExpired:  # a helper outlived it and holds the output: leave it, report the stop
            out = ""
        return subprocess.CompletedProcess(cmd, 124, out or "", f"{' '.join(cmd[:4])} took over {timeout:g}s and was stopped")


def git(root: Path, *args: str, check: bool = True, timeout: float = GIT_TIMEOUT) -> str:
    result = run(["git", "-C", str(root), *args], timeout)
    if check and result.returncode:
        why = result.stderr.strip() or "failed"
        if result.returncode == 124:
            lock = git_path(root, "index.lock")
            if lock.exists():
                why += f". It may have left {posix(lock)}: if no git command is running there, delete it"
        raise WorkkitError(f"git {' '.join(args[:2])}: {why}")
    return result.stdout.strip()


def git_path(root: Path, name: str) -> Path:
    """A file in the checkout's own Git folder (index.lock, rebase-merge); a path that can't exist when git can't say."""
    found = run(["git", "-C", str(root), "rev-parse", "--git-path", name]).stdout.strip()
    if not found:
        return Path(root) / ".git" / "wk-no-such-path"
    return Path(found) if Path(found).is_absolute() else Path(root) / found


def git_common_dir(repo: Path) -> Path:
    """The Git folder every worktree of a repo shares. Remembered per folder: it never changes, and base branch
    lookups ask for it many times per command."""
    return _common_dir(str(Path(repo).resolve()))


@functools.lru_cache(maxsize=256)
def _common_dir(repo: str) -> Path:
    common = Path(git(Path(repo), "rev-parse", "--git-common-dir"))
    return common if common.is_absolute() else Path(repo) / common


def checkout_of(cwd: Path) -> tuple[Path, str]:
    top = git(cwd, "rev-parse", "--show-toplevel", check=False)
    if not top:
        raise WorkkitError(f"{cwd} is not inside a Git checkout")
    branch = git(Path(top), "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    return Path(top), branch


def toml_str(value) -> str:
    """A TOML basic string: backslashes and quotes escaped, line breaks flattened, so a value can never break the file."""
    text = " ".join(str(value).split())
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


REPLACE_TRIES = 20  # about 2 seconds in all


def _replace(tmp: str, path: Path) -> None:
    """os.replace, retried briefly: on Windows it fails (PermissionError, WinError 5) while another process has the file
    open, e.g. the command center or a virus scanner reading it."""
    for attempt in range(REPLACE_TRIES):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == REPLACE_TRIES - 1:
                raise
            time.sleep(0.1)


def atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".wk-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
        _replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WorkkitError(f"Cannot read {path}: {exc}") from exc


def write_json(path: Path, value) -> None:
    atomic_text(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def push(repo: Path, branch: str) -> str:
    """Push; on a rejection, rebase on origin and push again. Returns "pushed", "pushed after rebase",
    "not pushed: no remote" or "push failed: ...", which the CLI reports loudly."""
    if not git(repo, "remote", check=False):
        return "not pushed: no remote"
    go = lambda *a: run(["git", "-C", str(repo), *a], WRITE_TIMEOUT)  # noqa: E731
    if go("push", "origin", branch).returncode == 0:
        return "pushed"
    pull = go("pull", "--rebase", "--autostash", "origin", branch)
    if pull.returncode:
        if any(git_path(repo, d).exists() for d in ("rebase-merge", "rebase-apply")):
            undone = go("rebase", "--abort").returncode == 0  # never leave the checkout mid-rebase: later writes refuse
            return ("push failed; pull --rebase failed" + (" and was undone" if undone else
                    f" and is still in progress in {posix(repo)}: run git rebase --abort there") + ": "
                    + pull.stderr.strip()[-300:])
        return "push failed; pull --rebase failed: " + pull.stderr.strip()[-300:]
    again = go("push", "origin", branch)
    return "pushed after rebase" if again.returncode == 0 else "push failed: " + again.stderr.strip()[-300:]


def save(repo: Path, base: str, paths: list[Path], message: str) -> str:
    """Commit exactly these paths on the repo's base branch and push them; nothing else in the checkout is touched."""
    top, branch = checkout_of(repo)
    if branch != base:
        raise WorkkitError(f"{top} must be on {base} to record work (found '{branch or 'detached'}')")
    rel = sorted({posix(p.resolve().relative_to(top.resolve())) for p in paths})
    git(top, "add", "--", *rel, timeout=WRITE_TIMEOUT)
    if git(top, "status", "--porcelain", "--", *rel):
        git(top, "commit", "--only", "-m", message, "--", *rel, timeout=WRITE_TIMEOUT)
    return push(top, branch)


@contextmanager
def locked(repo: Path, what: str, name: str, timeout: float = 120.0):
    """Hold a lock file in the repo's shared Git folder; a lock older than 10 minutes is treated as stale."""
    lock = git_common_dir(repo) / name
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as handle:
                json.dump({"pid": os.getpid(), "at": timestamp(), "what": what}, handle)
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > 600:
                    lock.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.time() > deadline:
                raise WorkkitError(f"Lock is held ({lock}); retry, or remove it if stale")
            time.sleep(0.4)
    try:
        yield
    finally:
        try:
            lock.unlink()
        except FileNotFoundError:
            pass
