"""`nh reply-check`: a Claude Code Stop hook that holds a supervisor's reply to the format the owner asked for.

Off until this laptop's settings file names the parts every supervisor reply must contain:

    [reply]
    require = ["Since you wrote:", "What would Pat do"]

When Claude is about to stop in a chat that loaded the supervisor skill, and its last message lacks one of them
(case ignored), the hook sends Claude back once to rewrite that message. Everything else is let through: builders
and other subagents, chats that aren't supervisors, the second stop of a turn (one nudge, never a loop), and any
error. Each check adds one line to reply-check.log beside the settings file: when, which chat, ok or what was
missing. No message text is logged.

It also holds a supervisor to recording its support work, unless the settings file turns that off:

    [reply]
    record = false

A turn (everything since the owner's last message) that did work for the owner - drafted a message or email for
someone, investigated (web search, a web page, a research agent), or reviewed a document - but ran no
`nh piece add` and no `nh piece note ... --add/--file` (itself or through a subagent; a refused one or --help
doesn't count) is sent back once to record it. A turn that only revises a draft also passes when the turn before
it recorded one. Only the turn's tool calls and, for a draft, the owner's ask and Claude's text are read: answering
questions about the list or nearly-headless, and the supervisor's own duties (keeping the list, starting and reviewing
builders, merging, releasing, checking local or test sites, rereading its skills or instructions), call none of
those tools, so they pass. A turn someone else started once the last one stopped - another chat's message, an
agent's hand-back or task notification, a hook - is not support for the owner, and is a turn of its own, so the
owner's earlier turn isn't counted again; one that arrives while the owner's turn is still running stays part of it.

`nh reply-check --remind` is its UserPromptSubmit hook: before each turn it prints one short line naming those parts,
so a reply is written right the first time; nothing when none are set. The Stop hook stays the safety net.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

SKILL = "supervisor"
_LOADED = re.compile(r"^Base directory for this skill: .*[\\/]skills[\\/](?:[^\\/\s]+[\\/])*" + SKILL + r"\s*$",
                     re.MULTILINE)


def required(s: dict) -> list[str]:
    reply = s.get("reply") if isinstance(s, dict) else None
    marks = reply.get("require") if isinstance(reply, dict) else None
    return [m for m in marks if isinstance(m, str) and m.strip()] if isinstance(marks, list) else []


def records(s: dict) -> bool:
    reply = s.get("reply") if isinstance(s, dict) else None
    return not (isinstance(reply, dict) and reply.get("record") is False)  # on unless turned off


def remind(s: dict) -> str:
    """One short line naming the required parts, or "" when none are set."""
    marks = required(s)
    return ("If you are a supervisor, your closing message must include: " + ", ".join(f'"{m}"' for m in marks)
            + ".") if marks else ""


def _entries(transcript: Path):
    with transcript.open(encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict):
                yield d


def _blocks(d: dict) -> list:
    content = (d.get("message") or {}).get("content") if isinstance(d.get("message"), dict) else None
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


RECORD = "a piece record"
_NOT_OWNER = ("<task-notification>", "<cross-session-message", "Stop hook feedback", "[Request interrupted",
              "This session is being continued")
_OTHERS = ("<task-notification>", "<cross-session-message")  # someone else's message, in a transcript with no origin
_ENDED = ("stop_hook_summary", "turn_duration")  # what Claude Code writes when a turn stops
_BROWSER = re.compile(r"^mcp__.*(?:chrome|browser|playwright|puppeteer)", re.I)
_MAIL = re.compile(r"^mcp__(?!ccd_).*(?:gmail|mail|outlook|slack|teams|chat)", re.I)
_DOCS = re.compile(r"^mcp__(?!ccd_).*(?:drive|docs|sheets|slides|notion|confluence|sharepoint|onedrive)"
                   r"|^mcp__[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}__(?:create|read|update|batch|query|export)$",
                   re.I)  # a connector may be named by its id alone: mcp__<uuid>__create
_DOCFILE = re.compile(r"\.(?:pdf|docx?|xlsx?|xlsm|csv|pptx?|rtf|odt)$", re.I)
_URL = re.compile(r"https?://[^\s\"'\\]+", re.I)
_LOCAL = re.compile(r"^https?://(?:localhost|127\.0\.0\.1|\[::1\]|[^/:]*\.(?:localhost|test)"  # the command center,
                    r"|(?:[^/:]*[.-])?(?:staging|stage|test|qa|uat|sandbox|preview)\d*[.-][^/]*)(?:[:/]|$)", re.I)
# a write is `nh piece add` or `nh piece note ... --add/--file`, run as a command of its own: nh or wk, nh.cmd, a
# path to it, a variable holding it ($nh, & $W), py -m wk or py bin/nh, with any global flags before `piece`; not a
# grep for the words, nor --help
_WK = r"[\"']?(?:[^\s\"';&|]*[\\/])?(?:wk|nh)(?:\.cmd|\.exe)?[\"']?"
_RECORDED = re.compile(r"(?:^|[;&|(\n]|\b(?:then|do)\s)\s*&?\s*"
                       r"(?:" + _WK + r"|\$\{?[\w:]+\}?|(?:py|python3?)(?:\.exe)?\s+(?:-\w+\s+)*(?:-m\s+wk|" + _WK + r"))"
                       r"(?:\s+--?[\w-]+(?:[ =](?!piece\b)[^\s;&|-][^\s;&|]*)?)*"
                       r"\s+piece\s+(?:add\b|note\b[^;&|\n]*?\s--(?:add|file)\b)[^;&|\n]*", re.I)
_HELP = re.compile(r"(?:^|\s)(?:--help|-h)\b")
_ASKED = re.compile(r"\b(?:draft|reply|respond|response|message|email|e-mail|write (?:to|back))", re.I)
_QUOTED = re.compile(r"<!--.*?-->|^[ \t]*>.*$", re.M)
_DRAFTED = re.compile(r"\bdraft(?:ed)?\s+(?:reply|response|message|email|e-mail|note|text|answer)\b"
                      r"|\b(?:the|this|my|a|here'?s|here is)\s+(?:new\s+|updated\s+|short\s+)?draft\b"
                      r"|^[\s*_#>]*(?:draft|subject)[*_]*\s*[:(]|^[\s>*_]*(?:Hi|Hello|Hey|Dear)\b[^\n]{0,40},[ \t]*$",
                      re.I | re.M)
_REVIEW = re.compile(r"\breview", re.I)
_LIST_AGENT = re.compile(r"\bpieces?\s+notes?\b|\bwk(?:\.cmd)?\s+(?:piece|question|hours|order)\b"
                         r"|\bbuilder\b|\bbrief\b|\breview|\bPRs?\b|\bpull request|#\d+", re.I)  # duty agents


def _owner_said(d: dict, blocks: list) -> str | None:
    """The owner's own words, when this entry is one: not a tool result, reminder, notification, hook feedback,
    interruption or the summary a compaction leaves."""
    if d.get("type") != "user" or d.get("isMeta") or d.get("isCompactSummary"):
        return None
    origin = d.get("origin")
    if isinstance(origin, dict) and origin.get("kind") not in (None, "", "human"):
        return None
    said = "\n".join(str(b.get("text", "")) for b in blocks if b.get("type") == "text")
    return said if said.strip() and not said.lstrip().startswith(_NOT_OWNER) else None


def _other(d: dict, blocks: list) -> bool:
    """A message that isn't the owner's and can start a turn of its own: another chat's cross-session message, an
    agent's hand-back or task notification, a hook. Claude Code names its sender (origin); older transcripts don't."""
    if d.get("type") != "user" or d.get("isCompactSummary") or d.get("isSidechain"):
        return False
    origin = d.get("origin")
    if isinstance(origin, dict) and origin.get("kind"):
        return origin.get("kind") != "human"
    said = "\n".join(str(b.get("text", "")) for b in blocks if b.get("type") == "text")
    return not d.get("isMeta") and said.lstrip().startswith(_OTHERS)


def _ended(d: dict) -> bool:
    return d.get("type") == "system" and d.get("subtype") in _ENDED and not d.get("isSidechain")


def _read_back(path: Path, stop) -> list[dict]:
    """One transcript's entries, read from its end until stop(entry) says so; in order."""
    kept = []
    for raw in reversed(path.read_bytes().splitlines()):
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        if stop(d):
            break
        kept.append(d)
    return kept[::-1]


def _turn(transcript: Path, asks: int = 2) -> list[dict]:
    """The transcript's last turns, read from the end so a long chat costs no more than a short one: back to the
    owner's last `asks` messages (the one before shows what the last ask revises), any attachment entries just
    before them, and what this chat's subagents did since (their own files, beside the transcript)."""
    found = [0]

    def stop(d):
        if found[0] >= asks and d.get("type") != "user":
            return True
        found[0] += not d.get("isSidechain") and _owner_said(d, _blocks(d)) is not None
        return False
    turn = _read_back(transcript, stop)
    since = next((str(d["timestamp"]) for d in turn if d.get("timestamp")), "")
    side = []
    if since:
        start = datetime.fromisoformat(since.replace("Z", "+00:00")).timestamp()
        for f in sorted((transcript.parent / transcript.stem / "subagents").glob("*.jsonl")):
            if f.stat().st_mtime >= start:
                side += [dict(d, isSidechain=True) for d in
                         _read_back(f, lambda d: str(d.get("timestamp") or "9") < since)]
    if not side:
        return turn
    last, keyed = since, []
    for d in turn:
        last = str(d.get("timestamp") or last)
        keyed.append((last, d))
    keyed += [(str(d.get("timestamp") or since), d) for d in side]
    return [d for _, d in sorted(keyed, key=lambda x: x[0])]


def _writes(calls: list, failed: set) -> bool:
    """A `nh piece add` or a writing `nh piece note` ran and wasn't refused."""
    for b in calls:
        given = b.get("input") if isinstance(b.get("input"), dict) else {}
        if b.get("name") in ("Bash", "PowerShell") and b.get("id") not in failed and any(
                not _HELP.search(m.group(0)) for m in _RECORDED.finditer(str(given.get("command", "")))):
            return True
    return False


def support(entries) -> list[str]:
    """What this turn (since the owner's last message) did for the owner that a piece should record; [] when it
    did none of it, or it (or one of its subagents) ran a `nh piece add` or a `nh piece note` that writes. A turn
    that only revises a draft also passes when the turn before it wrote one."""
    ask, attached, calls, text, sent = "", set(), [], [], set()  # sent: what the owner attached, maybe apart
    side, before, failed = [], [], set()  # side: subagents' calls; before: the previous turn's calls
    owner, ended = True, True  # owner: the owner started this turn; ended: the turn before it stopped
    for d in entries:
        blocks = _blocks(d)
        failed |= {b.get("tool_use_id") for b in blocks if b.get("type") == "tool_result" and b.get("is_error")}
        if d.get("isSidechain"):
            side += [b for b in blocks if b.get("type") == "tool_use"] if d.get("type") == "assistant" else []
            continue
        if _ended(d):
            ended = True
            continue
        said = _owner_said(d, blocks)
        if said is not None or (ended and _other(d, blocks)):  # another's message mid-turn stays part of the turn
            before = calls + side + ([] if owner else before)  # the owner's last turn, and any since that weren't
            owner, ended = said is not None, False
            ask, attached, calls, text, sent, side = said or "", sent | {b.get("type") for b in blocks}, [], [], \
                set(), []
        elif d.get("type") == "assistant":
            calls += [b for b in blocks if b.get("type") == "tool_use"]
            text += [str(b.get("text", "")) for b in blocks if b.get("type") == "text"]
            sent, ended = set(), False
        elif d.get("type") == "user" and not d.get("isMeta"):
            sent |= {b.get("type") for b in blocks} & {"image", "document"}
    if not owner or _writes(calls + side, failed):  # a turn someone else started is not support for the owner
        return []
    kinds, pages = set(), []
    for b in calls:
        name = str(b.get("name", ""))
        given = b.get("input") if isinstance(b.get("input"), dict) else {}
        if _MAIL.search(name):
            kinds.add("a drafted message")
        elif _DOCS.search(name) or (name == "Read" and _DOCFILE.search(str(given.get("file_path", "")))):
            kinds.add("a document review")
        elif name in ("WebSearch", "WebFetch"):
            kinds.add("an investigation")
        elif name in ("Agent", "Task"):  # a research agent; not a builder, a reviewer, a lookup or a list tidy
            if not (given.get("isolation") == "worktree" or given.get("subagent_type") == "Explore"
                    or _LIST_AGENT.search(str(given.get("prompt", "")))):
                kinds.add("an investigation")
        elif _BROWSER.search(name):  # only a page out on the web; the command center and test sites are duty
            pages += [u for u in _URL.findall(json.dumps(given)) if not _LOCAL.search(u)]
    if pages:
        kinds.add("an investigation")
    if "document" in attached or ("image" in attached and _REVIEW.search(ask)):
        kinds.add("a document review")
    if _ASKED.search(_QUOTED.sub("", ask)) and _DRAFTED.search("\n".join(text)):
        kinds.add("a drafted message")
    if kinds == {"a drafted message"} and _writes(before, failed):
        return []
    return sorted(kinds)


def is_supervisor(transcript: Path) -> bool:
    """This chat loaded the supervisor skill: Claude called the Skill tool for it, or the owner typed /supervisor
    (either way Claude Code then adds the skill's text, which opens with its folder). Subagents are other files."""
    for d in _entries(transcript):
        if d.get("isSidechain"):
            continue
        for b in _blocks(d):
            if b.get("type") == "tool_use" and b.get("name") == "Skill":
                name = str((b.get("input") or {}).get("skill", ""))
                if name.split(":")[-1] == SKILL:
                    return True
            if b.get("type") == "text" and _LOADED.search(str(b.get("text", ""))[:500]):
                return True
    return False


def last_text(transcript: Path) -> str:
    """The text Claude wrote since the owner's last message, when the hook didn't pass it."""
    text = []
    for d in _entries(transcript):
        if d.get("isSidechain"):
            continue
        blocks = _blocks(d)
        if d.get("type") == "user" and not d.get("isMeta") and any(b.get("type") == "text" for b in blocks):
            text = []
        elif d.get("type") == "assistant":
            text += [str(b.get("text", "")) for b in blocks if b.get("type") == "text"]
    return "\n".join(text)


def check(hook: dict, s: dict) -> tuple[list[str] | None, str]:
    """(the missing parts, or None when the reply isn't checked; the chat's id). A second stop in the same turn
    (stop_hook_active) is still checked, for the log, but is never sent back."""
    session = str(hook.get("session_id") or "")
    marks, record = required(s), records(s)
    if not (marks or record) or hook.get("agent_id"):
        return None, session
    if hook.get("hook_event_name") not in (None, "Stop"):
        return None, session
    transcript = Path(str(hook.get("transcript_path") or ""))
    if not (hook.get("transcript_path") and transcript.is_file() and is_supervisor(transcript)):
        return None, session
    message = hook.get("last_assistant_message")
    text = message if isinstance(message, str) and message.strip() else last_text(transcript) if marks else ""
    missing = [m for m in marks if m.casefold() not in text.casefold()]
    if record and (kinds := support(_turn(transcript))):
        missing.append(f"{RECORD} ({', '.join(kinds)})")
    return missing, session


def reason(missing: list[str]) -> str:
    said, marks = [], [m for m in missing if not m.startswith(RECORD)]
    if marks:
        parts = ", ".join(f'"{m}"' for m in marks)
        said.append(f"Your reply to the owner is missing {parts}. Rewrite your closing message so it includes "
                    f"{'it' if len(marks) == 1 else 'them'}, as the supervisor and WWID skills say.")
    for m in missing:
        if m.startswith(RECORD):
            did = m[len(RECORD):].strip(" ()")
            said.append(f"This turn is missing {RECORD}: it did work for the owner ({did}) and recorded none of it. "
                        "Record it before you stop: a new ask is its own new piece (`nh piece add`, with a note); "
                        "more work on the piece it belongs to is a dated line (`nh piece note <id> --add`). Never a "
                        "line on an unrelated piece, nor new work on one already billed: that is a new piece "
                        "(`--billed-ok` is only for a release or teardown note). Then end as you would have.")
    return " ".join(said)


def log(where: Path, session: str, missing: list[str], retry: bool = False) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')}\t{session or '-'}\t" + ("after nudge: " if retry else "") \
        + ("ok" if not missing else "missing: " + "; ".join(missing))
    where.parent.mkdir(parents=True, exist_ok=True)
    with where.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
