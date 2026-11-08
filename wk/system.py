"""The command center's nearly-headless page: every part of nearly-headless on this laptop and how the parts connect, read from what
is really here, so the page never goes stale:

hooks     the Claude Code hooks setup writes (upkeep.HOOKS), each checked in ~/.claude/settings.json (read-only)
roles     each role and step, with the text of the skill file or wk command that defines it (the skill's
          description, a section of its guide, or the command's help), and whether its skill is installed and current
skills    any other skill in skills/, so a new one shows without a change here
settings  what this laptop's settings file and Claude's settings set: the builder limit, the reply parts, and so on

What each part does in a line, and which part hands to which, is the page's own wording; the details come from the
sources. A source that is missing shows as missing, never as old text. Read-only.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import __version__, core, reply, settings, upkeep
from .core import KitError

KIT = upkeep.KIT

# id: (name, what it does in a line, where its detail comes from, the parts it hands to). A source is
# ("skill", name) the skill's description, ("section", file, heading) that section of a guide, or ("command", name)
# that wk command's help.
ROLES = {
    "you": ("You", "Talk to supervisors and say yes to merges and releases", None, ["supervisor"]),
    "routing": ("Inbox routing", "Drops each new client message into the right list's inbox",
                ("section", "skills/supervisor/routing.md", "Routing"), ["supervisor"]),
    "supervisor": ("Supervisor", "Keeps one list of work and talks to you about it", ("skill", "supervisor"),
                   ["queue", "lists"]),
    "pm": ("Project manager", "One per client: calls, hours, the invoice and messages no list took",
           ("skill", "project-manager"), ["supervisor"]),
    "queue": ("Approved queue", "Starts the next pieces you approved as builders free up",
              ("section", "skills/supervisor/moving.md", "The approved queue"), ["builder"]),
    "builder": ("Builder", "Does one piece in its own copy of the repo", ("skill", "builder"), ["pr"]),
    "pr": ("Pull request", "One per piece: the change and its record", ("template", "templates/pull_request_template.md"),
           ["reviewer"]),
    "refresh": ("nh refresh", "Brings a waiting PR up to date with main", ("command", "refresh"), ["pr"]),
    "reviewer": ("Reviewer", "Two fresh reads of the change before you see it",
                 ("section", "skills/supervisor/merge.md", "Before the yes"), ["release"]),
    "release": ("Merge & release", "Only on your yes: merge, tag, update the laptop",
                ("section", "skills/supervisor/merge.md", "Releasing"), []),
    "lists": ("Lists in each repo", "The memory: pieces, notes, decisions, hours", ("command", "piece"), ["check"]),
    "check": ("nh check", "Checks every list against GitHub and the rules", ("command", "check"), ["cc"]),
    "cc": ("Command center", "This page: reads the lists for you", ("command", "serve"), ["you"]),
}

# the plain words for each hook setup installs, by its spec in upkeep.HOOKS
HOOK_WORDS = {
    upkeep.RECALL_START: ("A chat starts", "Gives a supervisor its list in one block"),
    upkeep.RECALL_RESUME: ("A chat resumes", "Gives a supervisor its list back"),
    upkeep.RECALL: ("Claude compacts a long chat", "Gives a supervisor its list back after its memory shrinks"),
    upkeep.RECALL_CLEAR: ("You clear a chat", "Gives a supervisor its list back"),
    upkeep.REMIND: ("Before each turn", "Names the parts a supervisor's reply must have"),
    upkeep.REPLY: ("A supervisor is about to reply", "Sends the reply back once if a required part is missing, or "
                                                     "if work done for you wasn't recorded on the list"),
}


def reply_checks(s: dict) -> list[dict]:
    """What the Stop reply check holds a supervisor to, each on or off as the settings file says (wk/reply.py)."""
    parts = reply.required(s)
    return [{"name": "Required reply parts", "on": bool(parts),
             "says": ("Every reply must contain " + ", ".join(f'"{p}"' for p in parts)) if parts
             else "Off: no parts listed under [reply] require"},
            {"name": "Support work recorded", "on": reply.records(s),
             "says": "A turn that drafted a message, investigated or reviewed a document for you must record it as a "
                     "piece or a note line" if reply.records(s) else "Off: [reply] record = false"}]


def _text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def skill_description(name: str) -> str | None:
    text = _text(KIT / "skills" / name / "SKILL.md")
    m = re.search(r"^description:\s*(.+)$", text or "", re.MULTILINE)
    return m[1].strip() if m else None


def section(rel: str, heading: str) -> str | None:
    """The text under the first heading that starts with `heading`, up to the next heading of the same or higher
    level; None when the file or heading is gone."""
    lines = (_text(KIT / rel) or "").splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^(#+)\s+(.*)", line)
        if m and m[2].startswith(heading):
            body = []
            for nxt in lines[i + 1:]:
                h = re.match(r"^(#+)\s", nxt)
                if h and len(h[1]) <= len(m[1]):
                    break
                body.append(nxt)
            return "\n".join(body).strip() or None
    return None


def _commands() -> dict:
    """Each wk command's help, from the real command line (the description when it has one)."""
    from . import cli  # cli loads the command center lazily, so this is no cycle
    sub = next(a for a in cli.parser()._actions if a.dest == "cmd")
    helps = {a.dest: a.help for a in sub._choices_actions}
    return {name: p.description or helps.get(name, "") for name, p in sub.choices.items()}


def _detail(source, commands: dict) -> tuple[str | None, str]:
    """(the source's text or None when it is missing, where it lives)."""
    if source is None:
        return "", ""
    kind, *where = source
    if kind == "skill":
        return skill_description(where[0]), f"skills/{where[0]}/SKILL.md"
    if kind == "section":
        return section(*where), f"{where[0]} ({where[1]})"
    if kind == "template":
        heads = re.findall(r"^##\s+(.+)$", _text(KIT / where[0]) or "", re.MULTILINE)
        return ("Its sections: " + ", ".join(heads) + ".") if heads else None, where[0]
    return commands.get(where[0]) or None, f"nh {where[0]}"


def hooks(s: dict) -> list[dict]:
    """Every hook setup installs, whether ~/.claude/settings.json has it (None: that file can't be read), and, for the
    reply hooks, whether the settings file turns them on."""
    s_path, parts, checks = upkeep.claude_dir() / "settings.json", reply.required(s), reply_checks(s)
    out = []
    for spec in upkeep.HOOKS:
        when, what = HOOK_WORDS.get(spec, (f"{spec[0]} {spec[1] or ''}".strip(), f"Runs nh {spec[2]}"))
        mine = checks if spec == upkeep.REPLY else []
        idle = (spec == upkeep.REMIND and not parts) or (spec == upkeep.REPLY and not any(c["on"] for c in checks))
        out.append({"event": spec[0], "matcher": spec[1] or "", "command": f"nh {spec[2]}", "when": when,
                    "what": what, "on": upkeep.hook_installed(spec), "idle": idle, "checks": mine,
                    "file": core.posix(s_path)})
    return out


def _skill_state() -> dict:
    """skill name -> "current", "not current" (on some agent) or missing from the dict when not shipped."""
    state = {}
    for c in upkeep.skill_checks():
        m = re.search(r"skill '([^']+)'", c["check"])
        if m:
            state[m[1]] = state.get(m[1], "current") if c["ok"] else "not current"
    return state


def laptop_settings(s: dict) -> list[dict]:
    """What the settings file and Claude's settings set, each with where it is set."""
    try:
        limit = settings.builders_per_list(s)
        builders = "No limit" if limit is None else f"{limit} at a time"
    except KitError as exc:
        builders = f"Can't be read: {exc}"
    parts = reply.required(s)
    claude = upkeep.claude_dir() / "settings.json"
    try:
        window = upkeep._setting(claude, upkeep.WINDOW)[1]
        push = upkeep._setting(claude, upkeep.PUSH)[1]
    except KitError:
        window = push = "can't be read"
    return [
        {"name": "Builders per list", "value": builders, "where": "builders_per_list in the settings file",
         "means": "How many builders one list runs at once from the queue you approved."},
        {"name": "Required reply parts", "value": ", ".join(f'"{p}"' for p in parts) if parts else "None set: the reply check is off",
         "where": "[reply] require in the settings file",
         "means": "What every supervisor reply must contain; the reply hooks check it."},
        {"name": "Support work recorded", "value": "On" if reply.records(s) else "Off",
         "where": "[reply] record in the settings file (on unless false)",
         "means": "A supervisor that drafted, investigated or reviewed something for you is sent back to record it."},
        {"name": "Compact window", "value": f"{window:,} tokens" if isinstance(window, int) and not isinstance(window, bool)
         else str(window if window is not None else "Not set"), "where": f"{upkeep.WINDOW} in Claude's settings",
         "means": "How long a chat grows before Claude compacts it."},
        {"name": "Phone push", "value": {True: "On", False: "Off"}.get(push, str(push or "Not set")),
         "where": f"{upkeep.PUSH} in Claude's settings", "means": "Claude pushes to your phone when it needs you."},
        {"name": "Updates itself", "value": "Off" if s.get("auto_update") is False else "Once a day",
         "where": "auto_update in the settings file", "means": "Moves this laptop to the newest release on its own."},
        {"name": "Repos", "value": str(len(s.get("repos") or {})), "where": "[repos] in the settings file",
         "means": "The repos this laptop works on."},
    ]


def overview(s: dict) -> dict:
    commands, state = _commands(), _skill_state()
    roles = []
    for rid, (name, does, source, to) in ROLES.items():
        detail, where = _detail(source, commands)
        skill = source[1] if source and source[0] == "skill" else None
        status = "missing" if detail is None else (state.get(skill, "missing") if skill else "")
        roles.append({"id": rid, "name": name, "does": does, "detail": detail or "", "source": where,
                      "to": [t for t in to if t in ROLES], "state": status})
    named = {r[2][1] for r in ROLES.values() if r[2] and r[2][0] == "skill"}
    extra = [{"name": n, "detail": skill_description(n) or "", "state": state.get(n, "missing")}
             for n in sorted(p.name for p in (KIT / "skills").iterdir() if (p / "SKILL.md").is_file()) if n not in named]
    return {"version": __version__, "hooks": hooks(s), "roles": roles, "skills": extra, "settings": laptop_settings(s)}
