"""A chat picks its browser once. Two parts:

guard      the PreToolUse hook (`nh browser-guard`): when a chat that already picked a browser and has worked in it
           lists or selects browsers again, Claude gets a reminder to keep the one it picked (the call still runs:
           looking for a sign-in in the other browsers, once per site, is allowed)
conflicts  saved project memory that tells chats to break this rule (pick the browser again every turn), for nh check:
           the kit's rule wins over a memory
"""

from __future__ import annotations

import re
from pathlib import Path

PICK = re.compile(r"__(?:select_browser|list_connected_browsers)$")  # choosing a browser, not using it

PINNED = ("This chat already picked its browser and has worked in it. Keep using that one: don't list or select "
          "browsers before each step, whatever a project memory or note says (the kit's rule wins; flag that note to "
          "your supervisor, or to the owner if you are the supervisor). Pick again only to look for a sign-in in the "
          "other connected browsers, once per site, then go back. If the browser tools show a different browser than "
          "the one you picked, stop and tell the owner plainly instead of selecting again.")


def guard(hook: dict) -> str:
    """The reminder for a browser pick in a chat that already picked one and has worked in it since; "" for the
    first pick, picks in a row (checking each browser for a sign-in), subagents and every other tool."""
    from . import reply
    if hook.get("agent_id") or not PICK.search(str(hook.get("tool_name") or "")):
        return ""
    transcript = Path(str(hook.get("transcript_path") or ""))
    if not (hook.get("transcript_path") and transcript.is_file()):
        return ""
    picked = worked = False
    this = hook.get("tool_use_id")
    for d in reply._entries(transcript):
        if d.get("isSidechain"):
            continue
        for b in reply._blocks(d):
            name = str(b.get("name", ""))
            if b.get("type") != "tool_use" or (this and b.get("id") == this):
                continue  # the call being decided may already be in the transcript
            if name.endswith("__select_browser"):
                picked, worked = True, False
            elif picked and reply._BROWSER.search(name) and not PICK.search(name):
                worked = True
    return PINNED if picked and worked else ""


# ---------------------------------------------------------------- saved memory that breaks a kit rule

_PICKING = re.compile(r"re-?select|select_browser|list_connected_browsers|switch_browser|"
                      r"\b(?:re-?)?(?:select|pick|choose)(?:ing)?\s+(?:\S+\s+){0,2}?(?:chrome|browser)", re.I)
_EVERY = re.compile(r"\b(?:every|each)\s+(?:single\s+)?(?:turn|step|time|action|call|message|reply|tool)|"
                    r"\bbefore\s+(?:every|each)\b", re.I)  # not "before any write": checking then is the kit's own
_NOT = re.compile(r"\b(?:don'?t|do not|never|no need|stop|instead of)\b", re.I)
CONFLICTS = ((lambda line: bool(_PICKING.search(line) and _EVERY.search(line) and not _NOT.search(line)),
              "tells chats to pick the browser again every turn (the kit: pick it once per chat)"),)


def slug(top: Path) -> str:
    """Claude Code's folder name for a project: its path with every character that isn't a letter or digit as -."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(top))


def conflicts(top: Path, root: Path | None = None) -> list[dict]:
    """Each line of a repo's saved project memory (~/.claude/projects/<slug>/memory/*.md) that breaks a kit rule."""
    if root is None:
        from . import usage
        root = usage.projects()
    found = []
    for path in sorted((root / slug(top) / "memory").glob("*.md")):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for n, line in enumerate(lines, 1):
            found += [{"path": path, "line": n, "what": what} for test, what in CONFLICTS if test(line)]
    return found
