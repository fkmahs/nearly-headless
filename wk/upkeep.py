"""Setup, update and doctor: keeping a laptop's nearly-headless current, like any SaaS product.

setup   writes this laptop's settings file (if it has none; else adds any --repo it lacks), and the WWID profile repo
        (`wwid_profile`) when --profile names it or a `wwid` checkout sits beside nearly-headless; then installs the skills and the instructions every chat
        reads (the file `instructions` names in the settings, else the kit's neutral default) for every coding agent on
        the laptop: Claude always, Codex when it is installed; and Claude's
        hook that gives a supervisor its list when a chat starts or resumes and after its memory is compacted or cleared
        (`nh recall`), the hook
        that holds a supervisor's reply to the format (`nh reply-check`, off until the settings file lists the parts)
        and the one that names those parts before each turn (`nh reply-check --remind`),
        and an earlier auto-compact window, so long chats re-read less each turn; and, in every repo it lists, git's
        local exclude list of files that never go to GitHub (keepout.py), and its separate list checkout's
update  moves this nearly-headless checkout to the newest release on GitHub, then lets that release install itself
auto    once a day, any nh command checks GitHub and updates on its own, when `auto_update = true` (setup's
        --auto-update; off on a new laptop, kept on for one that already had it)
doctor  checks the settings, every repo, the skills, the instructions and CI against what nearly-headless expects
"""

from __future__ import annotations

import filecmp
import json
import os
import re
import shutil
import sys
import tomllib
from datetime import date
from pathlib import Path

from . import __version__, core, guard, keepout, privacy, settings, testmap, turns
from .core import KitError

KIT = Path(__file__).resolve().parents[1]

OURS = "<!-- nearly-headless:"  # how an instructions file the kit wrote starts, so it is replaced, never doubled
MARK = OURS + " the owner's expectations for every chat. Edit AGENTS.md in nearly-headless; nh keeps this copy current. -->"
DEFAULT_MARK = (OURS + " the kit's default instructions for every chat. To use your own, set `instructions` in the "
                "settings file to your file; nh keeps this copy current. -->")
DEFAULT_INSTRUCTIONS = KIT / "templates" / "instructions.md"  # neutral: a new laptop's chats get these until replaced


# ---------------------------------------------------------------- where each agent reads its instructions and skills

def claude_dir() -> Path:
    return Path.home() / ".claude"


def codex_dir() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def agents_dir() -> Path:
    """Codex reads personal skills from ~/.agents/skills."""
    return Path.home() / ".agents"


def skills_dir() -> Path:
    return claude_dir() / "skills"


def agents() -> list[dict]:
    """The coding agents on this laptop: Claude always; Codex when its home folder exists."""
    found = [{"agent": "Claude", "instructions": claude_dir() / "CLAUDE.md", "skills": skills_dir()}]
    if codex_dir().is_dir():
        found.append({"agent": "Codex", "instructions": codex_dir() / "AGENTS.md", "skills": agents_dir() / "skills"})
    return found


# ---------------------------------------------------------------- installing

COMPACT = KIT / "templates" / "compact-instructions.md"  # what Claude keeps when it compacts a chat


def _current(s: dict | None) -> dict:
    return settings.load(settings.path()) if s is None else s


def instructions_source(s: dict | None = None) -> Path:
    """The file every chat's instructions come from: `instructions` in the settings, else the neutral default."""
    return settings.instructions(_current(s)) or DEFAULT_INSTRUCTIONS


def _mark(src: Path) -> str:
    if core.same_path(src, DEFAULT_INSTRUCTIONS):
        return DEFAULT_MARK
    if any(core.same_path(src, k / "AGENTS.md") for k in (KIT, installed_kit())):
        return MARK
    return f"{OURS} your instructions for every chat. Edit {core.posix(src)}; nh keeps this copy current. -->"


def _expectations(agent: str = "Claude", s: dict | None = None) -> str:
    """The instructions every chat reads, marked as the kit's copy; Claude's copy also carries the compact
    instructions, which Claude reads from CLAUDE.md under a "# Compact instructions" heading. KitError when the
    source file can't be read."""
    src = instructions_source(s)
    try:
        body = src.read_text(encoding="utf-8")
    except OSError as exc:
        raise KitError(f"the instructions file {core.posix(src)} can't be read ({exc}); fix `instructions` in the "
                       "settings file") from exc
    text = _mark(src) + "\n\n" + body
    return text + "\n" + COMPACT.read_text(encoding="utf-8") if agent == "Claude" else text


def install_expectations(s: dict | None = None) -> dict:
    """Put the instructions (instructions_source) in each agent's own instructions file, which every chat reads in
    every repo. A file nearly-headless did not write is never overwritten: it only needs one line that pulls them in."""
    done, src = {}, instructions_source(s)
    for a in agents():
        dest = a["instructions"]
        if dest.is_file() and not dest.read_text(encoding="utf-8").startswith(OURS):
            done[a["agent"]] = {"installed": False, "file": core.posix(dest),
                                "why": f"it holds your own instructions; add the line @{core.posix(src)}"
                                       + (f" and the section in {core.posix(COMPACT)}" if a["agent"] == "Claude" else "")}
            continue
        try:
            core.atomic_text(dest, _expectations(a["agent"], s))
        except KitError as exc:
            done[a["agent"]] = {"installed": False, "file": core.posix(dest), "why": str(exc)}
            continue
        done[a["agent"]] = {"installed": True, "file": core.posix(dest)}
    return done


def keep_instructions(where: Path, created: bool) -> dict:
    """A laptop already running the kit's AGENTS.md as every chat's instructions (its CLAUDE.md carries that mark)
    keeps them: setup records `instructions` pointing at it, once. A new laptop gets the neutral default."""
    s = settings.load(where)
    mine = claude_dir() / "CLAUDE.md"
    if created or settings.instructions(s) is not None or not mine.is_file() \
            or not mine.read_text(encoding="utf-8").startswith(MARK):
        return {"instructions": core.posix(instructions_source(s)), "changed": False}
    mine = core.posix(installed_kit() / "AGENTS.md")  # the laptop's kit, as `kit` is: never a builder's worktree
    _top_level(where, "instructions", mine, keep=True)
    return {"instructions": mine, "changed": True}


def skill_files(skill: Path) -> list[Path]:
    """A skill's files: SKILL.md (always loaded) and the reference files beside it that it says when to read."""
    return sorted(f for f in skill.iterdir() if f.is_file() and f.suffix == ".md")


def install_skills(target: Path | None = None) -> list[str]:
    """Copy each skill, with its reference files, into every agent's skills folder (or only into target), removing
    .md files the release no longer ships from the kit's own skill folders."""
    targets = [target] if target else [a["skills"] for a in agents()]
    done = []
    for skill in sorted((KIT / "skills").iterdir()):
        if (skill / "SKILL.md").is_file():
            ships = {f.name for f in skill_files(skill)}
            for t in targets:
                (t / skill.name).mkdir(parents=True, exist_ok=True)
                for f in skill_files(skill):
                    shutil.copy2(f, t / skill.name / f.name)
                for old in skill_files(t / skill.name):  # a reference file this release dropped: its rules are stale
                    if old.name not in ships:
                        old.unlink()
            done.append(skill.name)
    for t in targets:  # a skill nearly-headless renamed: the old copy would still load, with stale rules
        for name in RETIRED:
            if (t / name / "SKILL.md").is_file():
                shutil.rmtree(t / name)
    return done


RETIRED = ("wwfd",)  # skills nearly-headless no longer ships (wwfd became wwid in 1.54.0)


# Claude Code hooks nearly-headless installs: (event, matcher or None, the nh command it runs)
RECALL = ("SessionStart", "compact", "recall")  # after compaction, gives a supervisor its list back
RECALL_CLEAR = ("SessionStart", "clear", "recall")  # the same when the owner clears the chat
RECALL_START = ("SessionStart", "startup", "recall")  # and when a chat starts, so a supervisor reads its list in one go
RECALL_RESUME = ("SessionStart", "resume", "recall")  # and when the owner resumes a chat
REPLY = ("Stop", None, "reply-check")           # holds a supervisor's reply to the format; off until configured
REMIND = ("UserPromptSubmit", None, "reply-check --remind")  # names those parts before each turn; off the same way
HOOKS = (RECALL_START, RECALL_RESUME, RECALL, RECALL_CLEAR, REMIND, REPLY)  # every hook setup installs, in a chat's order


def hook_command(spec: tuple = RECALL) -> str:
    """The kit's own launcher by its absolute path, never through PATH, so a hook runs this checkout whatever PATH
    holds. Claude Code runs hook commands in Git Bash on Windows, and directly on macOS and Linux, where bin/nh is
    executable (git keeps its mode), so the sh launcher works everywhere."""
    return f'"{core.posix(KIT / "bin" / "nh")}" {spec[2]}'


def _hook(spec: tuple = RECALL) -> dict:
    return {"type": "command", "command": hook_command(spec), "timeout": 30}


_LAUNCHER = re.compile(r'^"?(?:(.*)/bin/)?(?:wk|nh)(?:\.cmd)?"?\s')  # the kit's launcher: by path, or bare on PATH


def _ours(h, spec: tuple = RECALL) -> bool:
    """A hook of this kind that the kit wrote, from any folder or release: bin/nh or bin/wk by its path, or a bare
    `wk`/`nh` that PATH finds."""
    command = str(h.get("command", "")).replace("\\", "/").strip() if isinstance(h, dict) else ""
    m = _LAUNCHER.match(command)
    return bool(m) and command[m.end():].strip() == spec[2]


def _hook_groups(path: Path, spec: tuple = RECALL) -> tuple[dict, list]:
    """Claude's settings file and this event's hook groups; KitError when it can't be read safely."""
    data = core.read_json(path) if path.is_file() else {}
    hooks = data.get("hooks", {}) if isinstance(data, dict) else None
    groups = hooks.get(spec[0], []) if isinstance(hooks, dict) else None
    if not isinstance(groups, list):
        raise KitError(f"{core.posix(path)} is not shaped as Claude expects")
    return data, groups


def _has_hook(groups: list, spec: tuple = RECALL) -> bool:
    return any(g.get("matcher") == spec[1] and _hook(spec) in (g.get("hooks") or [])
               for g in groups if isinstance(g, dict))


def install_hook(spec: tuple = RECALL) -> dict:
    """Add one of the kit's hooks (RECALL by default, or REPLY) to ~/.claude/settings.json beside everything already
    there; an older copy of it (nearly-headless moved folders) is replaced, and a settings file that can't be read is never
    rewritten."""
    path = claude_dir() / "settings.json"
    try:
        data, groups = _hook_groups(path, spec)
    except KitError as exc:
        return {"installed": False, "file": core.posix(path), "why": f"{exc}; add the hook by hand (docs/install.md)"}
    if _has_hook(groups, spec):
        return {"installed": True, "changed": False, "file": core.posix(path)}
    kept = []
    for g in groups:
        hooks = g.get("hooks") if isinstance(g, dict) and g.get("matcher") == spec[1] else None
        if isinstance(hooks, list) and any(_ours(h, spec) for h in hooks):
            g = g | {"hooks": [h for h in hooks if not _ours(h, spec)]}
            if not g["hooks"]:
                continue  # it held only our older hook
        kept.append(g)
    ours = {"hooks": [_hook(spec)]} if spec[1] is None else {"matcher": spec[1], "hooks": [_hook(spec)]}
    data.setdefault("hooks", {})[spec[0]] = kept + [ours]
    core.write_json(path, data)
    return {"installed": True, "changed": True, "file": core.posix(path)}


PUSH = "agentPushNotifEnabled"  # Claude's /config switch "Push when Claude decides"
WINDOW = "autoCompactWindow"     # the context size (tokens) at which Claude compacts a chat, for every model
# Claude's default compacts near the model's full window (measured: median 494K, up to about 967K), so a chat can run
# away and re-read hundreds of thousands of tokens every turn. 256K stops those runaway chats (about half of all
# re-read tokens) while keeping the 200K-280K range that has worked well, and leaves about 170K of room above the
# ~85K a chat is back to after compacting, so a large read doesn't set off compaction after compaction.
WINDOW_DEFAULT = 256_000


def _setting(path: Path, key: str):
    """Claude's settings file and one top-level setting (None when unset); KitError when it can't be read safely."""
    data = core.read_json(path) if path.is_file() else {}
    if not isinstance(data, dict):
        raise KitError(f"{core.posix(path)} is not shaped as Claude expects")
    return data, data.get(key)


def _install_setting(key: str, value) -> dict:
    """Set one of Claude's settings in ~/.claude/settings.json beside everything already there. A value the owner set
    is left as it is, and a file that can't be read is never rewritten."""
    path = claude_dir() / "settings.json"
    try:
        data, now = _setting(path, key)
    except KitError as exc:
        return {"value": None, "file": core.posix(path), "why": str(exc)}
    if now is not None:
        return {"value": now, "changed": False, "file": core.posix(path)}
    core.write_json(path, data | {key: value})
    return {"value": value, "changed": True, "file": core.posix(path)}


def install_push() -> dict:
    """Claude pushes to the phone when it needs you; a switch the owner set (on or off) is kept."""
    done = _install_setting(PUSH, True)
    if "why" in done:
        done["why"] += '; turn on "Push when Claude decides" in /config'
    return {"on": done.pop("value") is True} | done


def install_window() -> dict:
    """Claude compacts long chats earlier (WINDOW_DEFAULT); a window the owner set is kept, and said to be theirs."""
    done = _install_setting(WINDOW, WINDOW_DEFAULT)
    if "why" in done:
        done["why"] += f'; add "{WINDOW}": {WINDOW_DEFAULT} to it by hand'
    elif done["value"] != WINDOW_DEFAULT:
        done["yours"] = True  # the owner's own choice: kept
    return {"window": done.pop("value")} | done


def _repo_spec(spec: str) -> tuple[str, str, str, str]:
    """--repo name=path[,client=Client name][,url=git address]: the repo's name, folder, client and where to clone it."""
    head, *extras = spec.split(",")
    name, _, path = head.partition("=")
    if not (name and path):
        raise KitError(f"--repo needs name=path (optionally ,client=... and ,url=...), got '{spec}'")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name.strip()):
        raise KitError(f"--repo name '{name.strip()}' may use only letters, digits, - and _")
    opts = dict(e.partition("=")[::2] for e in extras)
    unknown = set(opts) - {"client", "url"}
    if unknown:
        raise KitError(f"--repo {name}: unknown option(s) {', '.join(sorted(unknown))}; use client= and url=")
    return name.strip(), path.strip(), opts.get("client", "").strip(), opts.get("url", "").strip()


def _clone(url: str, path: str) -> None:
    run = core.run(["git", "clone", "--quiet", url, path], 900)
    if run.returncode:
        raise KitError(f"Could not clone {url}: {run.stderr.strip()[-300:]}")


def _plan(repos: list[str], known: set[str]) -> tuple[list[str], list[str], list[str], list[tuple[str, str]]]:
    """The --repos as settings lines, with nothing done yet: the names added, those already known, their lines, and
    the (url, folder) clones they need. Every --repo is checked here, before anything is cloned or written."""
    added, already, lines, clones = [], [], [], []
    for spec in repos:
        name, path, client, url = _repo_spec(spec)
        if name in known:
            already.append(name)
            continue
        if not Path(path).is_dir():
            if not url:
                raise KitError(f"{path} doesn't exist: clone it first, or add ,url=<git address> to --repo {name}")
            clones.append((url, path))
        known.add(name)
        added.append(name)
        lines += [f"[repos.{name}]", f"path = {core.toml_str(core.posix(Path(path).resolve()))}"]
        lines += ([f"client = {core.toml_str(client)}"] if client else []) + [""]
    return added, already, lines, clones


def add_repos(where: Path, repos: list[str]) -> dict:
    """Add each repo not yet in an existing settings file at its end, leaving every other line as it is. A repo already
    there changes nothing; a file that can't be read, or would stop reading, is never written."""
    try:
        text = where.read_text(encoding="utf-8")
        known = tomllib.loads(text).get("repos", {})
        if not isinstance(known, dict):
            raise ValueError("its repos are not [repos.<name>] tables")
    except (OSError, ValueError) as exc:  # TOMLDecodeError and UnicodeDecodeError are ValueErrors
        raise KitError(f"Cannot read {core.posix(where)} ({exc}); nothing changed. "
                           "Fix it, or add the repo by hand") from exc
    added, already, lines, clones = _plan(repos, set(known))
    if lines:
        gap = "" if not text.strip() or text.endswith("\n\n") else "\n" if text.endswith("\n") else "\n\n"
        new = text + gap + "\n".join(lines)
        try:
            tomllib.loads(new)
        except tomllib.TOMLDecodeError as exc:
            raise KitError(f"Adding {', '.join(added)} would break {core.posix(where)} ({exc}); nothing changed. "
                               "Add the repo by hand") from exc
        for url, path in clones:  # only once the new file is known to read
            _clone(url, path)
        core.atomic_text(where, new)
    return {"added": added, "already_there": already}


def install_keepout(s: dict) -> dict:
    """Each listed repo's local git exclude file gets the kit's block of files that never go to GitHub, and so does
    the checkout holding its list when that is a separate one (`list` in its settings entry)."""
    done = {}
    for name in s.get("repos") or {}:
        try:
            top = settings.repo(s, name)
            done[name] = keepout.install(top)
            lst = settings.list_of(s, name)
            if not core.same_path(core.git_common_dir(lst), core.git_common_dir(top)):
                done[name] = done[name] | {"list": keepout.install(lst)}
        except (KitError, OSError) as exc:
            done[name] = {"installed": False} | done.get(name, {}) | {"why": str(exc)}
    return done


def beside() -> Path:
    """Where setup looks for the WWID profile repo when --profile doesn't say: `wwid` in the folder holding nearly-headless."""
    return KIT.parent / "wwid"


def add_profile(where: Path, given: str = "") -> dict:
    """The WWID profile repo in the settings file (`wwid_profile`, optional): --profile's folder, else a `wwid`
    checkout beside nearly-headless, added when the file has none yet, before its first table, leaving every other line
    as it is. One already set is kept: change it by editing the file. Nothing found, nothing is added: the wwid skill
    is off on this laptop and nothing else needs it."""
    text = where.read_text(encoding="utf-8")
    current = settings.profile(tomllib.loads(text))
    if current is not None:
        return {"profile": core.posix(current), "added": False}
    folder = Path(given) if given else beside()
    if not given and not ((folder / ".git").exists() and (folder / "verdicts.md").is_file()):
        return {"profile": None, "added": False}
    value = core.posix(folder.resolve())
    lines = text.splitlines(keepends=True)
    at = next((i for i, line in enumerate(lines) if line.lstrip().startswith("[")), len(lines))
    while at > 0 and not lines[at - 1].strip():  # with the top-level lines, above the blank line before a table
        at -= 1
    if at == len(lines) and lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    new = "".join(lines[:at] + [f"wwid_profile = {core.toml_str(value)}\n"] + lines[at:])
    if tomllib.loads(new).get("wwid_profile") != value:
        raise KitError(f"Adding wwid_profile would change how {core.posix(where)} reads; nothing changed. "
                           "Add it by hand, above the first [table]")
    core.atomic_text(where, new)
    return {"profile": value, "added": True}


def _top_level(where: Path, key: str, value: str | bool, keep: bool) -> dict:
    """Set one top-level line of the settings file (above its first table, every other line as it is). With `keep`,
    a value already there stays."""
    text = where.read_text(encoding="utf-8")
    current = tomllib.loads(text).get(key)
    if current == value or (keep and current not in (None, "")):
        return {key: current, "changed": False}
    entry = f"{key} = {core.toml_str(value) if isinstance(value, str) else json.dumps(value)}\n"
    lines = text.splitlines(keepends=True)
    old = next((i for i, line in enumerate(lines) if re.match(rf"\s*{re.escape(key)}\s*=", line)
                and not any(x.lstrip().startswith("[") for x in lines[:i])), None)
    if old is not None:
        lines[old] = entry
    else:
        at = next((i for i, line in enumerate(lines) if line.lstrip().startswith("[")), len(lines))
        while at > 0 and not lines[at - 1].strip():
            at -= 1
        if at == len(lines) and lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines[at:at] = [entry]
    new = "".join(lines)
    if tomllib.loads(new).get(key) != value:
        raise KitError(f"Setting {key} would change how {core.posix(where)} reads; nothing changed. "
                           "Add it by hand, above the first [table]")
    core.atomic_text(where, new)
    return {key: value, "changed": True}


def add_kit(where: Path) -> dict:
    """`kit`: the nearly-headless checkout this laptop runs, the one setup runs from. bin/wk in any other checkout (the
    folder on PATH, say) forwards to it. Running setup from another checkout moves it there, which is also the way
    back."""
    return _top_level(where, "kit", core.posix(installed_kit()), keep=False)


def add_sections_from(where: Path) -> dict:
    """`pr_sections_from`: when this laptop took up the four PR sections. nh gate fresh refuses a PR opened since
    that lacks one, and only warns about an older one. Set once."""
    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return _top_level(where, "pr_sections_from", stamp, keep=True)


def add_auto_update(where: Path, created: bool, on: bool = False) -> dict:
    """`auto_update`: whether this laptop moves to each new release on its own. A new laptop gets it off, unless
    setup's --auto-update asks for it; a settings file from before the setting existed had it on, so it stays on;
    a value already there is kept, until --auto-update turns it on."""
    if on:
        done = _top_level(where, "auto_update", True, keep=False)
    elif "auto_update" in tomllib.loads(where.read_text(encoding="utf-8")):
        done = {"auto_update": settings.load(where).get("auto_update"), "changed": False}
    else:
        done = _top_level(where, "auto_update", not created, keep=True)
    return done | ({} if done["auto_update"] is True else {
        "how": "off: this laptop moves to a new release only when you run nh update. To have it check once a day "
               "and update on its own, run nh setup --auto-update"})


def install_guard(s: dict) -> dict:
    """The privacy guard's pre-push hook in each public repo's checkout (`public = true`)."""
    done = {}
    for name, info in (s.get("repos") or {}).items():
        if not settings.is_public(info):
            continue
        try:
            done[name] = privacy.install_hook(settings.repo(s, name), KIT / "bin" / "nh")
        except (KitError, OSError) as exc:
            done[name] = {"installed": False, "why": str(exc)}
    return done


WWID_MARK = "What would I do"  # the wwid skill's heading, which the reply check requires on a laptop with a WWID profile


def sync_mark(where: Path) -> dict:
    """The reply check's WWID mark follows the profile: with `wwid_profile` set, `[reply] require` holds WWID_MARK;
    without it, no such mark. Other parts and lines stay as they are; a file
    the change can't be made to safely is left alone and says why."""
    text = where.read_text(encoding="utf-8")
    s = tomllib.loads(text)
    on = settings.profile(s) is not None
    reply = s.get("reply")
    marks = reply.get("require") if isinstance(reply, dict) else None
    marks = marks if isinstance(marks, list) else []
    want = [m for m in marks if on or not (isinstance(m, str) and m.strip() == WWID_MARK)]
    if on and WWID_MARK not in want:
        want.append(WWID_MARK)
    if want == marks:
        return {"mark": on, "changed": False}
    line = "require = [" + ", ".join(core.toml_str(m) if isinstance(m, str) else json.dumps(m) for m in want) + "]"
    head = re.search(r"(?m)^\[reply\][ \t]*(#.*)?$", text)
    if head is None and reply is None:
        new = text + ("" if not text or text.endswith("\n") else "\n") + f"\n[reply]\n{line}\n"
    elif head is None:
        return {"mark": on, "changed": False, "why": "[reply] isn't a plain table; edit its require by hand"}
    else:
        nxt = re.compile(r"(?m)^[ \t]*\[").search(text, head.end())
        end = nxt.start() if nxt else len(text)
        body = text[head.end():end]
        found = list(re.finditer(r"(?ms)^[ \t]*require[ \t]*=[ \t]*\[.*?\][ \t]*(#[^\n]*)?$", body))
        if len(found) > 1 or (found and "require" in body[found[0].end():]):
            return {"mark": on, "changed": False, "why": "[reply] require is written in a way setup can't edit safely"}
        if found:
            body = body[:found[0].start()] + line + body[found[0].end():]
        else:
            body = "\n" + line + body
        new = text[:head.end()] + body + text[end:]
    expect = dict(s)
    expect["reply"] = {**(reply or {}), "require": want}
    try:
        if tomllib.loads(new) != expect:
            raise ValueError("it would read differently")
    except ValueError as exc:  # TOMLDecodeError is a ValueError
        return {"mark": on, "changed": False, "why": f"setup couldn't edit [reply] require safely ({exc}); edit it by hand"}
    core.atomic_text(where, new)
    return {"mark": on, "changed": True, "require": want}


def setup(where: Path, owner: str = "", repos: list[str] = (), skills: Path | None = None, profile: str = "",
          auto: bool = False) -> dict:
    """repos are name=path pairs. An existing settings file is never overwritten: repos it lacks are added at its end,
    and the WWID profile repo when it has none (add_profile)."""
    if profile and not Path(profile).is_dir():
        raise KitError(f"--profile {profile}: no such folder; clone the profile repo there first")
    created, repos_done = False, None
    if not where.is_file():
        if not owner:
            raise KitError("A new laptop needs --owner \"Your Name\" (and --repo name=path for each repo)")
        lines, clones = _plan(repos, set())[2:]
        for url, path in clones:
            _clone(url, path)
        core.atomic_text(where, "\n".join([f"owner = {core.toml_str(owner)}", ""] + lines))
        created = True
    elif repos:
        repos_done = add_repos(where, repos)
    kept = keep_instructions(where, created)
    return {"settings": core.posix(where), "created": created, "instructions": kept,
            **({"repos": repos_done} if repos_done is not None else {}), "wwid_profile": add_profile(where, profile),
            "kit": add_kit(where), "auto_update": add_auto_update(where, created, auto),
            "pr_sections": add_sections_from(where),
            "reply_mark": sync_mark(where),
            "skills": install_skills(skills),
            "expectations": install_expectations(settings.load(where)), "hook": install_hook(), "clear_hook": install_hook(RECALL_CLEAR),
            "start_hook": install_hook(RECALL_START), "resume_hook": install_hook(RECALL_RESUME), "reply_hook": install_hook(REPLY),
            "remind_hook": install_hook(REMIND), "push": install_push(), "compact_window": install_window(),
            "keep_out": install_keepout(settings.load(where)), "privacy_guard": install_guard(settings.load(where)),
            "agents": [a["agent"] for a in agents()],
            "version": __version__, "launcher": core.posix(KIT / "bin"),
            "next": "Run `nh doctor`: it checks that your PATH has this launcher folder, and gives the one command "
                    "that adds it when it doesn't."}


# ---------------------------------------------------------------- updating

def _number(version: str) -> tuple:
    return tuple(int(n) for n in version.split(".") if n.isdigit())


def installed_kit(kit: Path = KIT) -> Path:
    """This laptop's nearly-headless: the main checkout of the repo this code runs from. A builder running nearly-headless from its
    own worktree (not on main) still updates the kit the laptop uses, never its worktree."""
    try:
        common = core.git_common_dir(kit).resolve()
    except Exception:  # noqa: BLE001 - not a git checkout: it is the kit itself
        return kit
    top = common.parent
    return top if common.name == ".git" and (top / "wk" / "__init__.py").is_file() else kit


def fetched(kit: Path = KIT) -> str:
    """Fetch the kit's origin; the reason when it failed (empty when GitHub answered)."""
    done = core.run(["git", "-C", str(kit), "fetch", "--quiet", "origin"], 30)
    return "" if not done.returncode else ("GitHub didn't answer" if done.returncode == 124
                                           else "git fetch failed: " + done.stderr.strip()[-200:])


def latest_version(fetch: bool = False, kit: Path = KIT) -> str:
    """The newest release on GitHub (origin/main), fetched first when asked."""
    if fetch:
        fetched(kit)
    shown = core.git(kit, "show", "origin/main:wk/__init__.py", check=False).split('__version__ = "')
    return shown[1].split('"')[0] if len(shown) > 1 else installed_version(kit)


def installed_version(kit: Path = KIT) -> str:
    """The version in this nearly-headless folder now; a long-running command started on an older one still runs that."""
    return (kit / "wk" / "__init__.py").read_text(encoding="utf-8").split('__version__ = "')[1].split('"')[0]


def newer(latest: str, current: str = __version__) -> bool:
    return _number(latest) > _number(current)


def update(where: Path, kit: Path | None = None) -> dict:
    """Fast-forward this laptop's nearly-headless checkout (installed_kit) to origin/main (each release is a merge there), then
    run the new release's own setup, so whatever a release adds reaches the laptop on the first update."""
    kit = kit or installed_kit()
    branch = core.git(kit, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if branch != "main" or core.git(kit, "status", "--porcelain", "--untracked-files=no", check=False):
        raise KitError(f"nearly-headless at {core.posix(kit)} must be on main with no edits to update")
    before = installed_version(kit)
    pull = core.run(["git", "-C", str(kit), "pull", "--ff-only", "--quiet", "origin", "main"], 120,
                    env=os.environ | {"GIT_TERMINAL_PROMPT": "0"})
    if pull.returncode == 124:
        raise KitError("Could not update: GitHub didn't answer within 2 minutes")
    if pull.returncode:
        raise KitError("Could not update: " + pull.stderr.strip()[-300:])
    after = installed_version(kit)
    run = core.run([sys.executable, "-P", "-m", "wk", "--settings", str(where), "setup"], 300,
                   env=os.environ | {"PYTHONPATH": str(kit)})
    if run.returncode:
        raise KitError(f"Moved to {after}, but its setup failed: {run.stderr.strip()[-300:]}")
    done = json.loads(run.stdout)
    return {"from": before, "to": after, "skills": done["skills"], "expectations": done["expectations"],
            "hook": done.get("hook"), "clear_hook": done.get("clear_hook"),
            "start_hook": done.get("start_hook"), "resume_hook": done.get("resume_hook"), "reply_hook": done.get("reply_hook"),
            "push": done.get("push"), "compact_window": done.get("compact_window"), "keep_out": done.get("keep_out"),
            "kit": done.get("kit"), "auto_update": done.get("auto_update"), "privacy_guard": done.get("privacy_guard")}


def auto_update(where: Path, s: dict, today: str = "") -> dict | None:
    """Once a day, the first nh command checks GitHub and updates if a newer release (a merge to the kit's main) is
    out, when the settings file says `auto_update = true`. It never stops the command: a failure is reported
    and tried again: on the next command when the check itself failed (the day is stamped only once GitHub answered),
    else the next day. It updates the laptop's kit (installed_kit), wherever it is run from. Off unless the
    settings file has `auto_update = true` (add_auto_update)."""
    if s.get("auto_update") is not True or os.environ.get("NH_AUTO_UPDATE") == "0":
        return None
    stamp, today = where.parent / ".nearly-headless-checked", today or date.today().isoformat()
    if stamp.is_file() and stamp.read_text(encoding="utf-8").strip() == today:
        return None
    checked = False
    try:
        kit = installed_kit()
        failed = fetched(kit)
        if failed:
            raise KitError(failed)
        latest = latest_version(kit=kit)
        core.atomic_text(stamp, today)
        checked = True
        return update(where, kit) if newer(latest, installed_version(kit)) else None
    except Exception as exc:  # noqa: BLE001 - the daily check must never stop the command it rides on
        return {"error": str(exc) or type(exc).__name__, "retry": "tomorrow" if checked else "on the next command"}


# ---------------------------------------------------------------- checking

def skill_checks() -> list[dict]:
    """One check per skill and agent: exactly this release's files installed, each identical to its copy."""
    found = []
    for a in agents():
        for skill in sorted(p.name for p in (KIT / "skills").iterdir() if (p / "SKILL.md").is_file()):
            kit, mine = KIT / "skills" / skill, a["skills"] / skill
            ok = mine.is_dir() and {f.name for f in skill_files(mine)} == {f.name for f in skill_files(kit)}                 and all(filecmp.cmp(mine / f.name, f, shallow=False) for f in skill_files(kit))
            found.append({"ok": ok, "check": f"{a['agent']}: skill '{skill}' installed and current"})
    return found


def expectations_check(s: dict | None = None) -> list[dict]:
    found, src = [], instructions_source(s)
    for a in agents():
        dest = a["instructions"]
        text = dest.read_text(encoding="utf-8") if dest.is_file() else ""
        try:
            ok = text == _expectations(a["agent"], s) or f"@{core.posix(src)}" in text
        except KitError:
            ok = False
        found.append({"ok": ok, "check": f"{a['agent']}: your instructions ({core.posix(src)}) reach every chat"})
    return found


def _same_kit(groups: list, spec: tuple) -> bool:
    """A hook of this kind whose launcher is another working folder of this same nearly-headless checkout. A builder's
    worktree runs its own copy of wk, whose folder differs from the installed one the hooks name; that is still the
    laptop's nearly-headless, not a missing hook."""
    for g in groups:
        for h in (g.get("hooks") or []) if isinstance(g, dict) and g.get("matcher") == spec[1] else []:
            m = _LAUNCHER.match(str(h.get("command", "")).replace("\\", "/").strip()) if _ours(h, spec) else None
            try:
                if m and m[1] and Path(m[1]).is_dir() and core.same_path(core.git_common_dir(Path(m[1])), core.git_common_dir(KIT)):
                    return True
            except KitError:
                continue
    return False


def hook_check() -> list[dict]:
    found = []
    for spec, what in ((RECALL, "Claude: supervisors get their list back after compaction (nh recall hook)"),
                       (REPLY, "Claude: supervisors' replies are checked for the parts in [reply] require, when set, "
                               "and for recording support work, unless [reply] record = false (nh reply-check hook)"),
                       (RECALL_CLEAR, "Claude: supervisors get their list back after a clear (nh recall hook)"),
                       (RECALL_START, "Claude: supervisors get their list when a chat starts (nh recall hook)"),
                       (RECALL_RESUME, "Claude: supervisors get their list when a chat resumes (nh recall hook)"),
                       (REMIND, "Claude: supervisors are reminded of the parts in [reply] require before each turn, "
                                "when set (nh reply-check --remind hook)")):
        found.append({"ok": bool(hook_installed(spec)), "check": what})
    return found


def hook_installed(spec: tuple) -> bool | None:
    """Claude's settings file has this hook (from this nearly-headless, or another working folder of it); None when that
    file can't be read safely."""
    try:
        groups = _hook_groups(claude_dir() / "settings.json", spec)[1]
    except KitError:
        return None
    return _has_hook(groups, spec) or _same_kit(groups, spec)


def push_check() -> list[dict]:
    """Ok when the switch is on, or off by the owner's own choice; unset or unreadable needs `nh setup`."""
    try:
        value = _setting(claude_dir() / "settings.json", PUSH)[1]
    except KitError:
        value = None
    what = "Claude: pushes to your phone when it needs you" + (" (off by your choice)" if value is False else "")
    return [{"ok": isinstance(value, bool), "check": what}]


def window_check() -> list[dict]:
    """Ok when a window is set: the kit's, or any value the owner set (said to be theirs, since setup keeps it and
    could never fix it); unset or unreadable needs `nh setup`."""
    try:
        value = _setting(claude_dir() / "settings.json", WINDOW)[1]
    except KitError:
        value = None
    if value is None:
        return [{"ok": False, "check": f"Claude: compacts long chats at {WINDOW_DEFAULT:,} tokens at most"}]
    shown = f"{value:,}" if isinstance(value, int) and not isinstance(value, bool) else json.dumps(value)
    what = f"Claude: compacts long chats at {shown} tokens at most"
    return [{"ok": True, "check": what + (" (your choice)" if value != WINDOW_DEFAULT else "")}]


def kit_checks(s: dict | None = None) -> list[dict]:
    """What nearly-headless puts on the laptop: its skills, the instructions every chat reads, Claude's hooks, phone
    push and compact window."""
    return skill_checks() + expectations_check(s) + hook_check() + push_check() + window_check()


def docker_check(platform: str = sys.platform, which=shutil.which, run=None) -> list[dict]:
    """Off Linux, builders' tests run only in Docker's Linux container: missing or stopped Docker is a failure, with
    the fix. Linux needs no Docker for that."""
    from . import testmap
    if platform.startswith("linux"):
        return []
    state = testmap.docker_state(which, run)
    if state == "running":
        return [{"ok": True, "check": "Docker running: nh test runs in Linux"}]
    fix = f"{testmap.install_line(platform)}, then open it" if state == "isn't installed" else \
        "start Docker Desktop, and turn on its 'Start Docker Desktop when you sign in'"
    return [{"ok": False, "check": f"Docker {state}: builders' tests can't run (fix: {fix})"}]


def ran_from(s: dict, env: dict = os.environ) -> str:
    """Which nearly-headless checkout ran this command, and the forwarder it came through, if any."""
    via = env.get("NH_FORWARDED") or ""
    line = f"nearly-headless {__version__} ran from {core.posix(KIT)}"
    if via and not core.same_path(via, KIT):
        line += f" (forwarded by {core.posix(via)}/bin)"
    named = settings.kit(s)
    if named is not None and not core.same_path(named, installed_kit()):
        line += f"; the settings name {core.posix(named)} as this laptop's kit"
    return line


def fingerprint_check(name: str, repo: str, names: list[str], recorded=None) -> list[dict]:
    """A public repo's PRIVATE_TERMS secret matches this laptop's list: the fingerprint `nh privacy secret` recorded
    there (never the names) against this list's. A warning, with the command that sets both again."""
    value = (recorded or privacy.recorded)(repo)
    fix = f"run `nh privacy secret {repo}`"
    if value is None:
        what = f"{name}: couldn't read whether {repo}'s private name list is current (gh variable get)"
    elif not value:
        what = (f"{name}: {repo}'s private name list has no fingerprint, so whether it matches this laptop's is "
                f"unknown (fix: {fix})")
    elif privacy.matches(names, value):
        return [{"ok": True, "check": f"{name}: {repo}'s private name list (PRIVATE_TERMS) matches this laptop's"}]
    else:
        what = (f"{name}: this laptop's private name list no longer matches {repo}'s PRIVATE_TERMS, so GitHub's "
                f"checks miss what changed (fix: {fix})")
    return [{"ok": True, "warn": True, "check": what}]


def path_command(folder: Path, platform: str = sys.platform) -> str:
    """The one line the owner runs to put the kit's launcher folder first on their PATH (the kit never changes it)."""
    if platform.startswith("win"):
        win = str(folder).replace("/", "\\")
        return (f"[Environment]::SetEnvironmentVariable('Path', '{win};' + "
                "[Environment]::GetEnvironmentVariable('Path', 'User'), 'User')  (in PowerShell; then open a new terminal)")
    return f"echo 'export PATH=\"{core.posix(folder)}:$PATH\"' >> ~/.profile  (then open a new terminal)"


def path_check(env: dict = os.environ, platform: str = sys.platform) -> list[dict]:
    """`nh` works everywhere only when PATH has the kit's launcher folder (bin/ in the laptop's kit, or the checkout
    running this). Missing: the exact command to add it, for the owner to run."""
    want = installed_kit() / "bin"
    folders = [f for f in env.get("PATH", "").split(os.pathsep) if f]
    if any(core.same_path(f, want) or core.same_path(f, KIT / "bin") for f in folders):
        return [{"ok": True, "check": f"PATH has the kit's launcher folder {core.posix(want)}"}]
    return [{"ok": False, "check": f"PATH lacks the kit's launcher folder {core.posix(want)}, so `nh` isn't found "
                                   f"everywhere (fix, run it yourself: {path_command(want, platform)})"}]


def doctor(s: dict, where: Path) -> list[dict]:
    found = []

    def add(ok: bool, what: str):
        found.append({"ok": ok, "check": what})

    add(where.is_file(), f"settings file at {core.posix(where)}")
    found += path_check()
    found.append({"ok": True, "check": ran_from(s)})
    add(bool(s.get("owner")), "owner named in settings (approvals need it)")
    try:
        prof = settings.profile(s)
        if prof is None:
            add(True, "WWID profile: not set (optional)")
        else:
            add((prof / ".git").exists() and (prof / "verdicts.md").is_file(),
                f"WWID profile at {core.posix(prof)} (a git checkout with verdicts.md; the wwid skill reads it)")
    except KitError as exc:
        add(False, str(exc))
    add(shutil.which("gh") is not None, "GitHub CLI (gh) installed")
    found += kit_checks(s) + docker_check()
    for name in s.get("repos") or {}:
        try:
            top = settings.repo(s, name)
        except KitError as exc:
            add(False, str(exc))
            continue
        base = settings.base_of(s, top)
        branch = core.git(top, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
        add(branch == base, f"{name}: main checkout on {base} (found '{branch or 'detached'}')")
        issues = guard.check(top)
        add(not issues, f"{name}: CI follows the nearly-headless template" +
            ("" if not issues else ": " + "; ".join(f"{i['workflow']} {i['problem']}" for i in issues)))
        add(keepout.installed(top), f"{name}: spreadsheets and client files kept out of git on this laptop"
            " (local exclude list; nh setup adds it)")
        if settings.is_public((s.get("repos") or {})[name]):
            add(privacy.hook_installed(top), f"{name}: public: the privacy guard checks every push (pre-push hook; "
                "nh setup adds it)")
            repo = privacy.github_repo(top)
            if repo:
                found += fingerprint_check(name, repo, privacy.terms(s))
        try:
            lst = settings.list_of(s, name)
            if not core.same_path(lst, top):
                add((lst / ".git").exists(), f"{name}: its list is kept in {core.posix(lst)}")
        except KitError as exc:
            add(False, str(exc))
        if not testmap.rows_of(top):
            found.append({"ok": True, "warn": True, "check": f"{name}: no change → check map: nh test guesses; add one "
                          f"(a `## Change → check` section in {' or '.join(testmap.MAPS)})"})
        expired = [t for t in turns.rows(top) if t["expired"]]
        add(not expired, f"{name}: no expired turns" + ("" if not expired else f" ({len(expired)} expired)"))
    return found
