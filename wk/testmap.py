"""nh test: run only the checks a repo's own change -> check map names for what this branch changed, so a builder's
local proof takes minutes and CI stays the full proof.

The map is a `## Change → check` table in AGENTS.md or CONTRIBUTING.md, one row per area: the first cell names its
paths in backticks (`wk/work.py`, a folder `skills/`, a glob `*.md`, a placeholder `toolkits/<kit>/`), the last cell
its check: `none: ...`, a command in backticks (`<kit>` filled in from the path), or the full run. A check that
can't run in Linux starts with `CI only:` (skipped, with a one-line note: CI runs it) or `laptop only:` (run on the
laptop, never in the container), e.g. "laptop only: `npm run e2e` (needs a browser)".

On a laptop that isn't Linux, the checks run only in a Linux container (Windows is many times slower at the tests' git
and file work): the repo's own wk-test.Dockerfile, else the kit's default image for its stack (images/, Node or Python,
told from the repo's files). Docker off: nh starts Docker Desktop and waits for it; if it still can't, nothing runs and
nh test exits DOCKER_NEEDED; a Linux image that won't build exits IMAGE_FAILED, also running nothing. --local runs
here; Linux, and a repo of no stack the kit has an image for, run here.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import shlex
import shutil
import sys
import subprocess
import tempfile
import time
from pathlib import Path

from . import core, settings
from .core import KitError

MAPS = ("AGENTS.md", "CONTRIBUTING.md")  # where a repo keeps its map
DOCKERFILE = "wk-test.Dockerfile"  # a repo's own Linux image: wins over the kit's default
IMAGES = Path(__file__).resolve().parent / "images"  # the kit's default Linux images, one <stack>.Dockerfile each
STACKS = (("node", ("package.json",)),  # a repo's stack, told from its top folder's files: the first that matches
          ("python", ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt")))
PLACES = re.compile(r"^(ci|laptop)[ -]only\b", re.I)  # a check cell marked as one that can't run in Linux
PYTHONS = ("python", "py", "python3")
SECTION = re.compile(r"^##\s*Change\s*(?:→|->)\s*check\s*$(.*?)(?=^##\s|\Z)", re.M | re.S | re.I)


def map_rows(text: str) -> list[dict]:
    """The map's rows in a file's text: each with its path patterns, check text, kind (none, full or run) and
    commands (backticked text with a space in the check cell)."""
    section = SECTION.search(text or "")
    out = []
    for row in (section[1].splitlines() if section else []):
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        if len(cells) < 2 or set(cells[0]) <= set("-: "):
            continue
        paths = [p for p in re.findall(r"`([^`]+)`", cells[0]) if " " not in p]
        check = cells[-1]
        if not paths:
            continue
        place = PLACES.match(check)
        kind = place[1].casefold() if place else "full" if "full run" in check.casefold() else \
            "none" if check.casefold().startswith("none") else "run"
        out.append({"paths": paths, "check": check, "kind": kind,
                    "commands": [c for c in re.findall(r"`([^`]+)`", check) if " " in c]})
    return out


def _regex(pattern: str) -> re.Pattern:
    """A map path as a regex over a repo-relative path: a trailing / is a folder, * stays within one folder, <name>
    is one folder (captured), and a pattern with no / at all matches in any folder."""
    out = ""
    for part in re.split(r"(<\w+>|\*)", pattern.rstrip("/")):
        out += ("[^/]*" if part == "*" else f"(?P<{part[1:-1]}>[^/]+)" if re.fullmatch(r"<\w+>", part)
                else re.escape(part))
    out += "/.*" if pattern.endswith("/") else ""
    return re.compile(("(?:.*/)?" if "/" not in pattern else "") + out + "$")


def plan(files: list[str], rows: list[dict]) -> dict:
    """What the map asks for these changed files: each matched row with its files, the commands to run (unittest
    module lists merged into one run), the laptop-only commands, the CI-only checks skipped, and why a full run is
    needed (the full-run row, or a file no row names)."""
    matched, commands, full, laptop = [], [], [], []
    unmapped = []
    for f in files:
        hits = []
        for i, row in enumerate(rows):
            for p in row["paths"]:
                m = _regex(p).match(f)
                if m:
                    hits.append((i, m.groupdict()))
                    break
        if not hits:
            unmapped.append(f)
        for i, groups in hits:
            entry = next((x for x in matched if x["row"] == i), None)
            if not entry:
                entry = {"row": i, "check": rows[i]["check"], "kind": rows[i]["kind"], "files": []}
                matched.append(entry)
            entry["files"].append(f)
            if rows[i]["kind"] == "full":
                full.append(f"{f} needs the full run ({rows[i]['check']})")
            elif rows[i]["kind"] in ("run", "laptop"):
                into = commands if rows[i]["kind"] == "run" else laptop
                for c in rows[i]["commands"]:
                    for k, v in groups.items():
                        c = c.replace(f"<{k}>", v)
                    if c not in into:
                        into.append(c)
    full += [f"{f} is in no row of the map: run the full run, or add its row" for f in unmapped]
    matched.sort(key=lambda x: x["row"])
    return {"files": files, "matched": matched, "commands": _merged(commands), "laptop": laptop, "full": full,
            "skipped": [f"skipped, CI runs it: {m['check']}" for m in matched if m["kind"] == "ci"]}


UNITTEST = ("python", "-m", "unittest")


def _merged(commands: list[str]) -> list[str]:
    """`python -m unittest a b` and `python -m unittest b c` become one `python -m unittest a b c`; others stay."""
    modules, rest = [], []
    for c in commands:
        words = shlex.split(c)
        if tuple(words[:3]) == UNITTEST and len(words) > 3 and not any(w.startswith("-") or w == "discover"
                                                                         for w in words[3:]):
            modules += [w for w in words[3:] if w not in modules]
        else:
            rest.append(c)
    return ([" ".join(UNITTEST + tuple(modules))] if modules else []) + rest


def changed(top: Path, base: str) -> list[str]:
    """Files this branch changed against origin/<base> (from their merge base): committed, staged or edited. Untracked
    files don't count (a builder's scratch files stay untracked), and a move counts as both paths (--no-renames), so
    a file moved out of a mapped area keeps its check."""
    ref = f"origin/{base}"
    if not core.git(top, "rev-parse", "--verify", "--quiet", ref, check=False):
        raise KitError(f"No {ref} here: git fetch origin first")
    fork = core.git(top, "merge-base", ref, "HEAD")
    files = core.git(top, "diff", "--name-only", "--no-renames", fork).splitlines()
    return sorted({f.strip() for f in files if f.strip() and not f.startswith(".work/")})


def rows_of(top: Path) -> list[dict]:
    out = []
    for name in MAPS:
        path = top / name
        if path.is_file():
            out += map_rows(path.read_text(encoding="utf-8"))
    return out


def stack(top: Path) -> str | None:
    """The repo's stack the kit has a default image for: Node (package.json) before Python (its packaging files, or
    .py files at the top or in tests/); None for anything else."""
    for name, marks in STACKS:
        if any((top / m).is_file() for m in marks):
            return name
    return "python" if any(top.glob("*.py")) or any((top / "tests").glob("*.py")) else None


def image_file(top: Path) -> tuple[Path, str] | None:
    """The Dockerfile the checks run in and its image's repository name: the repo's own wk-test.Dockerfile, else the
    kit's default for its stack (wk-test-<stack>, so each stack keeps its own image cached), else None."""
    if (top / DOCKERFILE).is_file():
        return top / DOCKERFILE, IMAGE
    name = stack(top)
    return (IMAGES / f"{name}.Dockerfile", f"{IMAGE}-{name}") if name else None


def _argv(command: str) -> list[str]:
    words = shlex.split(command)
    return [sys.executable] + words[1:] if words and words[0] in PYTHONS else words


def where(top: Path, local: bool = False, platform: str = sys.platform, which=shutil.which, run=None
          ) -> tuple[str | None, str]:
    """The Docker image to run the checks in, or None and the one line saying why they run here (no line on Linux:
    CI and Linux laptops run as before). Docker off: start_docker; still off, or not installed: DockerNeeded. The
    image is built from image_file once, and again only when that file changes (its tag is the file's hash); a failed
    build is ImageFailed."""
    run = run or core.run
    if platform.startswith("linux"):
        return None, ""
    if local:
        return None, "nh test: running here (--local)"
    found = image_file(top)
    if not found:
        return None, (f"nh test: running here: this repo has no {DOCKERFILE} and isn't Node or Python (the kit's "
                      "default Linux images)")
    file, repo = found
    source = DOCKERFILE if repo == IMAGE else f"the kit's default {repo[len(IMAGE) + 1:]} image"
    state = docker_state(which, run)
    if state == "isn't installed":
        raise DockerNeeded(f"Docker isn't installed: {install_line(platform)}, open it once, then run nh test again")
    if state != "running" and not start_docker(platform, run):
        raise DockerNeeded("Docker isn't running and couldn't be started: start Docker Desktop, then run nh test again")
    tag = f"{repo}:" + hashlib.sha256(file.read_bytes().replace(b"\r\n", b"\n")).hexdigest()[:12]
    if run(["docker", "image", "inspect", "--format", "{{.Id}}", tag], 30).returncode:
        print(f"nh test: building the Linux image {tag} from {source} (once per change to that file)", flush=True)
        with tempfile.TemporaryDirectory() as empty:  # the image needs no files from the repo
            built = run(["docker", "build", "-q", "-t", tag, "-f", str(file), empty], 900)
        if built.returncode:
            last = ((built.stderr or built.stdout or "").strip().splitlines() or ["failed"])[-1]
            fix = DOCKERFILE if repo == IMAGE else f"{source} (or add a {DOCKERFILE})"
            raise ImageFailed(f"the Linux image didn't build ({last}): fix {fix}, then run nh test again")
        listed = run(["docker", "images", repo, "--format", "{{.Repository}}:{{.Tag}}"], 30)
        old = [t for t in (listed.stdout or "").split() if t.startswith(repo + ":") and t != tag]
        if old:  # one image kept on disk; one still in use by a running check stays (rmi refuses it)
            run(["docker", "rmi", *old], 120)
    return tag, f"nh test: running in Linux (Docker image {tag}, from {source})"


IMAGE = "wk-test"  # the repo's own image; the kit's defaults are wk-test-<stack>
DOCKER_NEEDED = 4  # nh test's exit code when Docker can't be used: not a test failure (1) or an error (2)
WAIT = 120.0  # how long nh test waits for a Docker Desktop it started


class DockerNeeded(KitError):
    """Tests run only in Linux on this laptop, and Docker can't be used: the one line naming what is needed."""
    code = DOCKER_NEEDED


IMAGE_FAILED = 5  # nh test's exit code when the Linux image didn't build: nothing runs on this laptop instead


class ImageFailed(DockerNeeded):
    """The repo's wk-test.Dockerfile didn't build: the checks don't fall back to this laptop."""
    code = IMAGE_FAILED


def install_line(platform: str = sys.platform) -> str:
    """The new-laptop checklist's install line (docs/new-laptop.md)."""
    return "brew install --cask docker" if platform == "darwin" else "winget install Docker.DockerDesktop"


def desktop_app(env=os.environ) -> Path | None:
    """Docker Desktop's app on Windows, where its installer puts it for any user: Program Files (the usual install),
    else the user's own programs folder (a per-user install)."""
    for root, rest in (("ProgramFiles", ("Docker", "Docker")), ("LOCALAPPDATA", ("Programs", "Docker", "Docker"))):
        if env.get(root):
            app = Path(env[root], *rest, "Docker Desktop.exe")
            if app.is_file():
                return app
    return None


def _desktop_open(run) -> bool:
    listed = run(["tasklist", "/FI", "IMAGENAME eq Docker Desktop.exe", "/NH"], 30)
    return "docker desktop.exe" in (listed.stdout or "").casefold()


def _launch(argv: list[str]) -> None:
    """Start an app and leave it running on its own: nh never waits on it, and never stops or quits it."""
    flags = {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} \
        if sys.platform == "win32" else {"start_new_session": True}
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **flags)


def start_docker(platform: str = sys.platform, run=None, launch=_launch, sleep=time.sleep, clock=time.monotonic,
                 wait: float = WAIT, env=os.environ) -> bool:
    """Start Docker Desktop (Windows: its app; macOS: open -a Docker), or find it already open, then wait up to wait
    seconds for its engine to answer, with one progress line. True once it answers. Docker Desktop is shared with other
    builders and projects: this only ever starts it."""
    run = run or core.run
    if platform == "darwin":
        print(f"nh test: Docker isn't running: starting Docker Desktop, waiting up to {wait:g}s", flush=True)
        launch(["open", "-a", "Docker"])
    elif platform == "win32":
        if _desktop_open(run):
            print(f"nh test: Docker Desktop is open but its engine isn't answering: waiting up to {wait:g}s", flush=True)
        else:
            app = desktop_app(env)
            if not app:
                return False
            print(f"nh test: Docker isn't running: starting Docker Desktop, waiting up to {wait:g}s", flush=True)
            launch([str(app)])
    else:
        return False
    end = clock() + wait
    while True:
        if not run(["docker", "info", "--format", "{{.ServerVersion}}"], 15).returncode:
            print("nh test: Docker is running", flush=True)
            return True
        if clock() >= end:
            return False
        sleep(3)


def docker_state(which=shutil.which, run=None) -> str:
    """'running', "isn't installed" or "isn't running": whether nh test can use Docker on this laptop."""
    if not which("docker"):
        return "isn't installed"
    return "running" if not (run or core.run)(["docker", "info", "--format", "{{.ServerVersion}}"], 30).returncode \
        else "isn't running"


# Inside the container: copy the checkout (mounted read-only) to the container's own disk, without its .git (a
# worktree's points at a Windows path) or bytecode, and run there. Faster than running on the mount, and nothing the
# tests do can reach the checkout.
COPY = "mkdir /work && tar -C /src --exclude=./.git --exclude=__pycache__ -cf - . | tar -C /work -xf - && cd /work"


def container_argv(tag: str, top: Path, command: str, name: str) -> list[str]:
    """docker run for one check: removed when it ends, no network, no host environment (docker passes none unless
    told to), the checkout mounted read-only."""
    words = shlex.split(command)
    if words and words[0] in PYTHONS:
        words[0] = "python"
    return ["docker", "run", "--rm", "--init", "--name", name, "--network", "none",
            "--mount", f"type=bind,source={top},target=/src,readonly", tag,
            "sh", "-c", f"{COPY} && exec {shlex.join(words)}"]


def in_container(tag: str, top: Path, command: str, timeout: float):
    name = f"wk-test-{secrets.token_hex(4)}"
    done = None
    try:
        done = core.run(container_argv(tag, top, command, name), timeout)
        return done
    finally:  # stopped (time limit, Ctrl+C): the docker client is gone but its container may not be
        if done is None or done.returncode == 124:
            core.run(["docker", "rm", "-f", name], 60)


def run(cwd: Path, s: dict, dry: bool = False, timeout: float = 3600.0, modules: list[str] | None = None,
        full: bool = False, local: bool = False) -> tuple[int, str]:
    """Plan, then run the commands in the checkout's top folder (or a Linux container: see where), printing as it
    goes. Exit 0 when every command passed and no full run is needed; 1 when one failed or a full run is needed (never
    run unasked: it is the owner's time, and CI runs it). Named unittest modules, or full (the map's full-run row),
    run just those instead. Laptop-only checks run here after the rest; CI-only ones are skipped with their note."""
    top = core.checkout_of(cwd)[0]
    rows = rows_of(top)
    if modules:
        p = {"commands": [" ".join(UNITTEST + tuple(modules))], "full": [], "laptop": [], "skipped": []}
        out = [f"nh test: {' '.join(modules)}"]
    elif not rows:
        raise KitError(f"No `## Change → check` map in {' or '.join(MAPS)} at {core.posix(top)}: run the nearest "
                           "tests by hand")
    elif full:
        commands = list(dict.fromkeys(c for r in rows if r["kind"] == "full" for c in r["commands"]))
        if not commands:
            raise KitError("The map's full-run row names no command in backticks")
        p = {"commands": commands, "full": [], "laptop": [], "skipped": []}
        out = ["nh test: the full run"]
    else:
        base = settings.base_of(s, top)
        p = plan(changed(top, base), rows)
        out = [f"nh test: {len(p['files'])} changed file(s) against origin/{base}"]
        out += [f"- {', '.join(m['files'])}: {m['check']}" for m in p["matched"]]
        out += [f"full run needed: {x}" for x in p["full"]]
        out += [f"nh test: {x}" for x in p["skipped"]]
    out += [f"run: {c}" for c in p["commands"]] + [f"run on the laptop: {c}" for c in p["laptop"]] \
        or ["run: nothing (no mapped command)"]
    print("\n".join(out), flush=True)
    if dry:
        return (1 if p["full"] else 0), ""
    try:
        image, why = where(top, local) if p["commands"] else (None, "")
    except DockerNeeded as exc:  # tests run only in Linux here: none run on this laptop
        return exc.code, f"nh test: {exc}"
    if why:
        print(why, flush=True)
    failed = []
    for c, there in [(c, image) for c in p["commands"]] + [(c, None) for c in p["laptop"]]:
        print(f"\n$ {c}" + ("" if there or not image else "  (laptop only: here)"), flush=True)
        done = in_container(there, top, c, timeout) if there else core.run(_argv(c), timeout, cwd=str(top))
        sys.stdout.write(done.stdout or "")
        sys.stdout.write(done.stderr or "")
        if done.returncode:
            failed.append(c)
    summary = ("FAILED: " + "; ".join(failed)) if failed else "passed" if p["commands"] or p["laptop"] \
        else "nothing to run"
    tail = f"\nnh test: {summary}" + ("; full run needed (see above)" if p["full"] else "")
    return (1 if failed or p["full"] else 0), tail
