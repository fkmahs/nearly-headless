"""This laptop's settings: one small file outside every repo (--settings, else $NH_SETTINGS or the older
$WORKKIT_SETTINGS, else ~/.nearly-headless/settings.toml). Until setup moves it there, a laptop's settings folder from
before 1.56.0 (~/.workkit) is read where it is. It is the only thing that differs between laptops.

    owner = "Pat Lee"          # the owner; approvals name no one ("Approved by the owner: ..."), and the privacy guard
    approver = "Pat"           # keeps these names off public repos; `nh gate` still accepts "Approved by Pat: ..."
    builders_per_list = 2      # builders a list runs at once from the owner-approved queue (no limit unless set)
    wwid_profile = "C:/Projects/wwid"   # optional: the WWID profile repo the wwid skill reads and records to

    kit = "C:/Projects/nearly-headless"  # written by nh setup: the checkout `nh` runs (bin/nh elsewhere forwards)
    pr_sections_from = "2026-01-01T00:00:00Z"  # written by nh setup: PRs opened since need the four sections

    [repos.app]                # every execution repo this laptop works on
    path = "C:/Projects/app"   # its main checkout
    base = "main"              # default main
    list = "C:/Projects/app-list"  # optional: the checkout holding its list (.work/), when not this one
    public = true              # optional: anyone can read it, so the privacy guard checks its pushes and PRs

    [privacy]
    terms = ["Globex"]         # private names only the owner knows, added to the guard's list (privacy.py)

Each repo declares its own products in `.work/products.toml` (see work.py). The product ships no owner:
with no settings, the base branch is main and approval gates refuse.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from . import core
from .core import KitError


HOME = ".nearly-headless"  # the settings folder in the user's home
OLD_HOME = ".workkit"      # where releases before 1.56.0 kept it; setup moves it to HOME once


def home() -> Path:
    return Path.home() / HOME


def old_home() -> Path:
    return Path.home() / OLD_HOME


def named() -> str:
    """The settings file the environment names: $NH_SETTINGS, else the older $WORKKIT_SETTINGS; "" when neither."""
    return os.environ.get("NH_SETTINGS") or os.environ.get("WORKKIT_SETTINGS") or ""


def path(given: Path | None = None) -> Path:
    """--settings, else the file the environment names, else ~/.nearly-headless/settings.toml; the old folder's file
    while only it exists (a laptop that hasn't run setup since 1.56.0 keeps working)."""
    value = given or named()
    if value:
        return Path(value).resolve()
    new, old = home() / "settings.toml", old_home() / "settings.toml"
    return old if old.is_file() and not home().exists() else new


def move_home() -> dict:
    """Setup's one move of the settings folder: the whole of ~/.workkit becomes ~/.nearly-headless, when only the old
    one exists. With both, nothing moves: doctor fails and says which to keep."""
    new, old = home(), old_home()
    if not old.exists():
        return {"moved": False}
    if new.exists():
        return {"moved": False, "why": f"both {core.posix(old)} and {core.posix(new)} exist; keep one (nh doctor)"}
    old.rename(new)
    return {"moved": True, "from": core.posix(old), "to": core.posix(new)}


def load(where: Path) -> dict:
    if not where.is_file():
        return {}
    try:
        return tomllib.loads(where.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise KitError(f"Cannot read {where}: {exc}") from exc


def approver(s: dict) -> str:
    name = s.get("approver") or (s.get("owner") or "").split(" ")[0]
    if not name:
        raise KitError("No approver: set `owner` (or `approver`) in this laptop's settings file")
    return name


APPROVER = "the owner"  # how every approval comment names the approver, on every repo: no person's name


def approvers(s: dict, checkout: Path) -> list[str]:
    """The names an approval comment in this checkout's repo may carry: "the owner"; on a private repo also the
    approver's name from the settings, which approvals posted there before 1.56.0 used. No owner set: KitError, so
    nobody can approve."""
    name = approver(s)
    return [APPROVER] if is_public(info_of(s, checkout)[1]) else [APPROVER, name]


def builders_per_list(s: dict) -> int | None:
    """How many builders one list runs at once from its approved queue: `builders_per_list`; None (no limit) unless
    the settings file sets it."""
    value = s.get("builders_per_list")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise KitError("builders_per_list in the settings file must be a whole number, 1 or more")
    return value


def profile(s: dict) -> Path | None:
    """The WWID profile repo (`wwid_profile`): the wwid skill's profile and verdicts, kept outside nearly-headless so
    nearly-headless holds nothing personal. None when unset: the skill is off, and nothing else needs it."""
    value = s.get("wwid_profile")
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise KitError("wwid_profile in the settings file must be a folder path in quotes")
    return Path(value)


def repo(s: dict, name: str) -> Path:
    """An execution repo's main checkout, by its name in the settings."""
    info = (s.get("repos") or {}).get(name)
    if not info:
        raise KitError(f"Unknown repo '{name}'. Known: {', '.join(s.get('repos') or {}) or 'none'}. "
                           f"Add [repos.{name}] to this laptop's settings file.")
    top = Path(info.get("path", ""))
    if not top.is_dir():
        raise KitError(f"Repo '{name}' is not at {top}")
    return top


def base_of(s: dict, checkout: Path) -> str:
    """The base branch of the repo this checkout belongs to, matched by its shared Git folder; else main."""
    common = core.git_common_dir(checkout)
    for info in (s.get("repos") or {}).values():
        top = Path(info.get("path", ""))
        try:
            if top.is_dir() and core.same_path(core.git_common_dir(top), common):
                return info.get("base", "main")
        except KitError:
            continue
    return "main"


def info_of(s: dict, checkout: Path) -> tuple[str, dict]:
    """The settings entry (name, fields) of the repo whose code checkout this one belongs to, matched by its shared Git
    folder; ("", {}) when none is."""
    try:
        common = core.git_common_dir(checkout)
    except KitError:
        return "", {}
    for name, info in (s.get("repos") or {}).items():
        top = Path(info.get("path", ""))
        try:
            if top.is_dir() and core.same_path(core.git_common_dir(top), common):
                return name, info
        except KitError:
            continue
    return "", {}


def list_of(s: dict, name: str) -> Path:
    """The checkout holding a repo's list (`.work/`): `list = "<folder>"` in its settings entry when the list lives
    apart from the code (the kit's own list, say), else the repo's own checkout."""
    info = (s.get("repos") or {}).get(name) or {}
    value = info.get("list")
    if value in (None, ""):
        return repo(s, name)
    if not isinstance(value, str):
        raise KitError(f"list in [repos.{name}] must be a folder path in quotes")
    top = Path(value)
    if not top.is_dir():
        raise KitError(f"Repo '{name}' keeps its list at {value}, which is not a folder")
    return top


def is_public(info: dict) -> bool:
    """A repo anyone can read (`public = true`): its pushes, PR text and merges are checked for private names."""
    return info.get("public") is True


def kit(s: dict) -> Path | None:
    """The nearly-headless checkout this laptop runs (`kit`, written by nh setup). bin/nh in any other checkout forwards to
    it, so `nh` typed anywhere runs this one."""
    value = s.get("kit")
    return Path(value) if isinstance(value, str) and value else None
