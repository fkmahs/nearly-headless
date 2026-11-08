"""`wk check`: every list checked against GitHub and against work-kit's rules, read-only, one line per problem with
the command that fixes it. Supervisors run it when they start or resume and before every catch-up; the command
center shows the count per list, and code that reached the base branch without a PR as a red alert.

What it looks at, per repo: each list's pieces (against the repo's PRs and branches on GitHub, the state rules, plain
names), open pieces' notes (missing, no brief, dates wk can't read or that are doubled or in the future, size), questions
(stale, PR closed, piece finished, leading with a piece id), unbilled hours (naming no piece or a missing one), the
billing file's coverage_from, each list's supervisor chat and builders that lately went past Claude's compact window
(usage.py), leftover branches, files on the base branch that don't belong in GitHub (keepout.py), and
commits on the base branch since the repo took up work-kit that came neither through a merged PR nor touch only
`.work/` and the invoicing toolkit's files in `billing/` (it commits those itself). And the setup's health:
tests failing on the base branch or on a PR waiting for the owner's yes, a PR waiting for the owner's yes whose
files the base branch has changed since the PR's branch last took it (wk gate fresh refuses its merge), committed files that look like they hold a
key, token or password (git grep, once per commit), to-dos untouched for TODO_DAYS, instruction files over their word
budget (WORD_BUDGET), and open pieces with near-identical names. GitHub is asked three times per repo: its PRs, its
branches, and its CI runs (shared with the page's CI status, at most once per RUNS_EVERY); git fetches a waiting PR's
commits only when this laptop lacks them. Nothing is written, except by `--fix-dates` (opt-in, one list), `--ack` (one commit,
on the owner's word) and `--fix` (the mechanical findings only, see fix), all through the repo's usual lock, commit and
push. The rules a write can keep (name_problems, long_lines) live here once: wk piece refuses what they flag, and
`wk recall` shows a list's other findings that need no network (at_start).
"""

from __future__ import annotations

import json
import re
import time
import tomllib
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import date
from pathlib import Path

from . import core, keepout, settings, turns, usage, work
from .core import WorkkitError

STALE_DAYS = 2      # a question older than this gets a reminder (not a problem: the page shows its age instead)
NOTE_BYTES = 8192   # a note bigger than this costs every reread
LINE_CHARS = 600    # a note line longer than this belongs in the PR or a finding
WORDS = 8           # a piece's name, at most
ACK = "acknowledged.tsv"  # .work/: commits on the base branch the owner has looked at, one per line
ACK_COLUMNS = ["sha", "date", "reason"]
ALLOWED = (".work/",)  # paths a commit on the base branch may touch without a PR
# ...and the files the invoicing toolkit commits straight to the base branch, by design (each invoice, its sent and
# paid marks), directly in billing/: anything else there, or in a folder below it, still needs a PR
BILLING = re.compile(r"billing/(ledger\.json|client\.toml|[^/]+-INV-\d+\.(md|csv))")
KEEP = {"main", "master", "staging", "develop", "gh-pages"}  # long-lived branches, never leftovers

TICKET = re.compile(r"\b[A-Z]{2,6}-\d{2,}\b|(?i:\(was\b)")
# a file name is lowercase ("sdf.py", "deploy.xml", "export_job.py"); a product's name ("Node.js") is not one
FILENAME = re.compile(r"(?<![\w.])(?!(?:node|next|vue|nuxt|react|express|deno|d3|three|chart)\.js\b)"
                      r"[a-z0-9][a-z0-9_-]*\.(?i:py|js|mjs|ts|tsx|json|xml|md|csv|tsv|toml|ya?ml|sql|html|sh|ps1|cmd)\b")
LOOSE_DATE = re.compile(r"^- \*{0,2}(\d{4}-\d{2}-\d{2})\*{0,2}\s*[:—–,-]?\s*\*{0,2}\s*(.*)$")
CLEAR_DATE = re.compile(r"^- (?:\*\*\d{4}-\d{2}-\d{2}|\d{4}-\d{2}-\d{2}\s*(?::|—|–| - ))")  # surely a log line
RANGE = re.compile(r"^\*{0,2}\s*(?:to|through|thru|until|and|&|\.\.|/|-|–|—)\s*\*{0,2}\d{4}-\d{2}-\d{2}"
                   r"|^/\d{1,2}\b", re.I)  # "2026-10-15 to 2026-10-20", "2026-10-21/22"
LEADING_PIECE = re.compile(r"^\W*(?:after|for|on|re)?\s*P\d+\b", re.I)

TODO_DAYS = 30      # a to-do piece with no note line or change for this long: drop, park or start it
# Word budgets for the instructions chats read, every word paid for on each read: a skill's SKILL.md, its other .md
# (reference) files, and a repo's AGENTS.md and CLAUDE.md. The supervisor skill was trimmed to about 1,300 words and
# the longest reference file is under 800, so these leave room without letting a file double.
WORD_BUDGET = {"skill": 1500, "reference": 1000, "instructions": 1500}
SKILL_DIRS = ("skills", ".claude/skills")  # where a repo keeps its skills, each <dir>/<name>/SKILL.md
ALIKE = 0.6         # two open pieces whose names share this much of their words, two words at least, look alike
FILLER = {"a", "an", "the", "and", "or", "of", "to", "for", "in", "on", "with", "by", "from", "at", "is", "it", "its"}
RUNS_EVERY = 300.0  # seconds between reads of a repo's CI runs, shared by wk check and the page's CI status
FRESH = 15.0        # ...and how recent a read Check GitHub now takes as fresh, so check and page share one
# workflows that clearly run no tests; every other workflow, named or not, counts as tests
NOT_TESTS = re.compile(r"(?i)deploy|release|publish|\bpages\b|label|\bstale\b|greet|welcome|triage|auto-?merge"
                       r"|backport|changelog|\block\b|dependabot")
BILLING_WORDS = ("spending limit", "payments have failed", "billing")
# Likely secrets in committed files: (what it may be, the pattern). Only the file and line are ever reported.
SECRETS = [
    ("a private key", re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----")),
    ("an AWS key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("a GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})")),
    ("an Anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("an OpenAI key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9]{20,}")),
    ("a Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("a Google key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("a Stripe key", re.compile(r"\b[rs]k_live_[0-9A-Za-z]{20,}")),
    # a setting named like a secret (DB_PASSWORD, SECRET_KEY, apiKey, AWS_SECRET_ACCESS_KEY) given a literal value,
    # quoted anywhere or unquoted in a settings file (.env, YAML, TOML, ...); the value then has to look like one
    ("a password or key", re.compile(r"(?i)(?<![\w-])[\w.]*?(?:password|passwd|pwd|secret|api_?key|access_?key|token)"
                                     r"\w*[\"']?\s*[:=](?![=>])\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s\"'#,;]+))")),
]
NAMED = SECRETS[-1][0]
# git grep's cheap first pass (case-insensitive); the patterns above then decide
SECRET_HINT = (r"private key-----|a[ks]ia[0-9a-z]{16}|gh[pousr]_[a-z0-9]{36}|github_pat_|sk-[a-z0-9_-]{20}|xox[abprs]-"
               r"|aiza[0-9a-z_-]{35}|k_live_|(password|passwd|pwd|secret|api_?key|access_?key|token)[a-z0-9_]*"
               r"[\"']?[[:space:]]*[:=]")
SETTINGS_FILE = re.compile(r"(?i)(^|/)\.env[^/]*$|\.(ya?ml|toml|ini|cfg|conf|properties)$|(^|/)[^./]+$")
ENV_FILE = re.compile(r"(^|/)\.env(\.[^/]+)?$")  # a committed .env file: it exists to hold keys and passwords
# where a likely secret is surely not a real one: tests, docs and example files, and placeholder values
EXEMPT_PATH = re.compile(r"(?i)(^|/)(tests?|__tests__|fixtures?|mocks?|e2e|examples?|samples?|docs?)/"
                         r"|[._-](test|spec)\.|(^|/)test_[^/]*$|\.(example|sample|template|dist)(\.[^/]*)?$|(^|/)readme[^/]*$|\.md$")
PLACEHOLDER = re.compile(r"(?i)example|sample|placeholder|dummy|fake|test|your|redacted|change_?me|changeit|<|\$|%|\{|\("
                         r"|x{4,}|\*{3,}|0{6,}|\.{3}")


def _looks_secret(value: str) -> bool:
    """A literal that could be a real password or key: long enough, not a placeholder, a word, a name or a path."""
    return (len(value) >= 8 and not PLACEHOLDER.search(value) and not re.fullmatch(r"[A-Za-z_.-]+", value)
            and not re.match(r"/|\.\.?/|~|\w+://", value) and not re.search(r"\s", value))  # a path, URL or sentence


# each kind of finding, for the owner: a short heading, what it means in plain words, and who acts on it
KINDS = {
    "github": ("Links to GitHub", "The list and GitHub disagree about a piece's pull request or branch.", "supervisor"),
    "state": ("Piece status", "A piece's status doesn't match what it has.", "supervisor"),
    "name": ("Piece names", "A piece's name should be plain words, eight at most.", "supervisor"),
    "note": ("Notes to tidy", "A piece's note is missing, untidy or too long.", "supervisor"),
    "question": ("Questions", "A question isn't tied to a piece, or no longer matches it.", "supervisor"),
    "memory": ("Memory limit", "A chat ran past its memory limit, so it may have lost track of earlier work.",
               "nobody"),
    "hours": ("Hours", "Hours logged without saying which piece they were for.", "supervisor"),
    "billing": ("Billing", "The invoice needs the first day it covers.", "owner"),
    "git": ("Leftover branches", "Work left behind that no piece accounts for.", "owner"),
    "files": ("Files that shouldn't be on GitHub", "Files that don't belong in the repo are stored in it.", "supervisor"),
    "main": ("Changes without a pull request", "Code reached the main branch without going through a pull request.",
             "owner"),
    "repo": ("Settings", "work-kit couldn't read this repo's settings.", "supervisor"),
    "tests": ("Tests failing", "GitHub's tests failed on the main branch, or on a pull request waiting for your yes.",
              "supervisor"),
    "behind": ("Needs the latest main", "A pull request waiting for your yes lacks main's newest changes to files it "
               "also changes, so the two were never tested together. Its builder takes main and re-tests first.",
               "supervisor"),
    "secret": ("Possible passwords or keys", "A committed file looks like it holds a key, token or password, so anyone "
               "with the repo can read it.", "owner"),
    "stale": ("Untouched to-dos", f"A to-do nobody has touched for {TODO_DAYS} days or more: start it, park it or drop "
              "it.", "owner"),
    "bloat": ("Long instructions", "An instruction file every chat reads is over its word budget, so every chat pays "
              "for it.", "supervisor"),
    "alike": ("Look-alike pieces", "Two open pieces in the same repo have nearly the same name; they may be one piece.",
              "supervisor"),
}


def _problem(kind: str, repo: str, product: str, item: str, what: str, fix: str, name: str = "") -> dict:
    """One finding; `name` is the plain name of the piece `item` names, for the owner's eyes."""
    return {"kind": kind, "repo": repo, "product": product, "item": item, "what": what, "fix": fix, "name": name}


def line(p: dict) -> str:
    """One problem, one line. A repo-wide one names the list whose supervisor acts on it."""
    if p["product"]:
        where = " ".join(x for x in (p["repo"], p["product"], p["item"]) if x)
    else:
        where = f"{p['repo']} repo-wide ({'for ' + p['owner'] if p.get('owner') else 'no owner list: ask the owner'})" \
            + (f" {p['item']}" if p["item"] else "")
    return f"{where}: {p['what']}. Fix: {p['fix']}"


def _numbers(pr: str) -> list[str]:
    return re.findall(r"\d+", pr or "")


def _date(text: str) -> date | None:
    try:
        return date.fromisoformat(str(text)[:10])
    except ValueError:
        return None


# ---------------------------------------------------------------- note lines

def _log(lines: list[str]) -> dict[int, tuple[str, str]]:
    """The note's dated log lines, by index: (day, text after the date). The log starts at the first line in a clear
    dated form ("- 2026-10-30:", "- **2026-10-30**", "- 2026-10-30 —"), or at any dated line after the brief's first
    paragraph. A date line in that first paragraph, or a range ("- 2026-10-15 to 2026-10-20"), is never a log line."""
    found, started, text, past_brief = {}, False, False, False
    for i, raw in enumerate(lines):
        raw = raw.rstrip("\r")
        if i and not raw.strip():
            past_brief = past_brief or text  # a blank line after the brief's first text
        text = text or (bool(i) and bool(raw.strip()))
        m = LOOSE_DATE.match(raw)
        if not m or _date(m[1]) is None or RANGE.match(raw[m.end(1):]):
            continue
        started = started or past_brief or bool(work.DATED.match(raw) or CLEAR_DATE.match(raw))
        if started:
            found[i] = (m[1], m[2])
    return found


def tidy(text: str) -> str:
    """A note with its log lines in the form wk reads ("- 2026-10-30: ..."): a doubled date dropped (a different typed
    date is kept in brackets after it), a bold or dash form rewritten, a byte-order mark removed. The brief and any
    line that is not a dated log line stay as they are."""
    lines = text.lstrip("\ufeff").split("\n")
    for i, (day, rest) in _log(lines).items():
        lines[i] = f"- {day}: {work.strip_date(rest, day)}" + ("\r" if lines[i].endswith("\r") else "")
    return "\n".join(lines)


def _note_problems(text: str, today: date) -> list[str]:
    found, doubled, unreadable, future = [], 0, 0, 0
    lines = text.lstrip("\ufeff").splitlines()
    log = _log(lines)
    for i, raw in enumerate(lines):
        if i in log:
            day, rest = log[i]
            if not work.DATED.match(raw):
                unreadable += 1
            elif work.TYPED_DATE.match(rest) and _date(work.TYPED_DATE.match(rest)[1]):
                doubled += 1
            if _date(day) > today:
                future += 1
    if doubled:
        found.append(f"{doubled} note line(s) carry the date twice")
    if unreadable:
        found.append(f"{unreadable} note line(s) dated in a form wk can't read, so the latest one may not show")
    long = long_lines(text)
    if text.startswith("\ufeff"):
        found.append("the note starts with a byte-order mark")
    return found + ([f"{future} note line(s) dated in the future"] if future else []) + \
        ([f"{long} note line(s) over {LINE_CHARS} characters"] if long else [])


def long_lines(text: str) -> int:
    """A note's lines over LINE_CHARS, every line counted (a byte-order mark dropped): wk check reports them, and wk
    piece note refuses a note or line that has one."""
    return sum(len(x) > LINE_CHARS for x in text.lstrip("\ufeff").splitlines())


def name_problems(name: str) -> list[str]:
    """What is wrong with a piece's name ("9 words", "a ticket number", "a file name"): wk check reports it, and wk
    piece add and set --name refuse it."""
    words, why = len(name.split()), []
    if words > WORDS:
        why.append(f"{words} words")
    if TICKET.search(name):
        why.append("a ticket number")
    if FILENAME.search(name):
        why.append("a file name")
    return why


def _has_brief(text: str) -> bool:
    """Text other than the title before the log."""
    lines = text.lstrip("\ufeff").splitlines()
    first = min(_log(lines), default=len(lines))
    return any(x.strip() for x in lines[1:first])


# ---------------------------------------------------------------- GitHub and git, once per repo

def _gh(cwd: Path, *args: str) -> str:
    result = core.run(["gh", *args], 60, cwd=str(cwd))
    if result.returncode:
        raise WorkkitError("gh: " + (result.stderr.strip() or "failed"))
    return result.stdout


def _facts(top: Path, base: str, gh, fetch: bool = False, fetched: Future | None = None) -> dict:
    """What GitHub and git say about a repo: its PRs (any state), its branches on GitHub (None when unknown) and here,
    the working folders in use, and the base branch's history since the repo took up work-kit (for unreviewed).
    Speed: the three questions to GitHub and the history are asked at the same time; `fetched` is a git fetch still
    running, which only what reads the fetched branches waits for."""
    facts = {"prs": None, "remote": None, "error": [], "top": top, "gh": gh, "base": base, "refs": {}}
    has_remote = bool(core.git(top, "remote", check=False))

    def history() -> str:
        if fetched is not None:
            fetched.result()
        return _history(top, _ref(top, base))
    with ThreadPoolExecutor(max_workers=4) as pool:
        prs_job = pool.submit(gh, top, "pr", "list", "--state", "all", "--limit", "1000", "--json",
                              "number,state,headRefName,headRefOid,baseRefName,mergeCommit")
        runs_job = pool.submit(runs, top, gh, fetch)
        heads_job = pool.submit(core.run, ["git", "-C", str(top), "ls-remote", "--heads", "origin"], 60) \
            if has_remote else None
        history_job = pool.submit(history)
    facts["history"] = history_job.result()
    try:
        prs = json.loads(prs_job.result() or "[]")
        facts["prs"] = {str(p["number"]): p | {"state": p.get("state") or "OPEN"} for p in prs if isinstance(p, dict)}
    except (WorkkitError, ValueError, KeyError, TypeError, OSError) as exc:
        facts["error"].append(f"GitHub's PRs could not be read ({exc})")
    try:
        facts["runs"] = runs_job.result()
    except (WorkkitError, ValueError, KeyError, TypeError, OSError) as exc:
        facts["runs"] = None
        facts["error"].append(f"GitHub's test runs could not be read ({exc})")
    if heads_job is not None:
        heads = heads_job.result()
        if heads.returncode:
            facts["error"].append("GitHub's branches could not be read (" + heads.stderr.strip()[-200:] + ")")
        else:
            facts["remote"] = {x.split("refs/heads/", 1)[1] for x in heads.stdout.splitlines() if "refs/heads/" in x}
    facts["local"] = set(core.git(top, "for-each-ref", "--format=%(refname:short)", "refs/heads",
                                  check=False).splitlines())
    listed = core.git(top, "worktree", "list", "--porcelain", check=False).splitlines()
    # a builder's folder keeps the branch it was made with in its name (agent-<id> for worktree-agent-<id>), even after
    # the builder switched to its piece's branch
    facts["worktrees"] = {x[18:] for x in listed if x.startswith("branch refs/heads/")} | \
        {"worktree-" + Path(x[9:]).name for x in listed if x.startswith("worktree ")}
    return facts


def _acked(top: Path) -> set[str]:
    return {x["sha"] for x in work._read_tsv(top / work.DIR / ACK, ACK_COLUMNS)}


def _ref(top: Path, base: str) -> str:
    """The base branch as GitHub has it (origin/<base>), else the local one."""
    return f"origin/{base}" if core.git(top, "rev-parse", "--verify", "--quiet", f"origin/{base}", check=False) else base


_began: dict[str, str] = {}  # repo folder -> the commit that first added its products.toml (the adoption)


def _adopted(top: Path, ref: str) -> str:
    """The commit on `ref` that first added the repo's products.toml, or "" before the repo took up work-kit. Looking
    it up walks the whole history, so it is remembered per repo while it is still on `ref` (one quick git call)."""
    key = core.posix(top)
    kept = _began.get(key)
    if kept and core.run(["git", "-C", str(top), "merge-base", "--is-ancestor", kept, ref]).returncode == 0:
        return kept
    found = core.git(top, "log", "--format=%H", "--diff-filter=A", ref, "--", f"{work.DIR}/products.toml",
                     check=False).splitlines()
    _began[key] = found[-1] if found else ""
    return _began[key]


def _history(top: Path, ref: str) -> str:
    """The base branch's own line of commits since the repo took up work-kit, each with the files it changed against
    the one before, in one git call; "" before it took it up."""
    began = _adopted(top, ref)
    return core.git(top, "log", "--first-parent", "--diff-merges=first-parent", "--name-only",
                    "--format=%x00%H%x09%s", ref, "--not", f"{began}^@", check=False) if began else ""


def unreviewed(top: Path, base: str, prs: dict | None, acked: set[str] = frozenset(),
               history: str | None = None) -> list[dict]:
    """Commits on the base branch (as GitHub has it), since the repo took up work-kit, that came neither through a
    merged PR nor touch only `.work/` and the invoicing toolkit's files in `billing/` (BILLING), and that the
    owner hasn't acknowledged. `history` is _history's answer when it was read already."""
    out = _history(top, _ref(top, base)) if history is None else history
    merged = {(p.get("mergeCommit") or {}).get("oid", "") for p in (prs or {}).values() if p["state"] == "MERGED"}
    numbers = {n for n, p in (prs or {}).items() if p["state"] == "MERGED"}
    found = []
    for chunk in out.split("\0")[1:]:
        head, _, files = chunk.partition("\n")
        sha, _, subject = head.partition("\t")
        files = [f for f in files.splitlines() if f.strip()]
        via = re.search(r"^Merge pull request #(\d+)|\(#(\d+)\)$", subject)
        if (sha in merged or (via and (via[1] or via[2]) in numbers) or sha in acked
                or all(f.startswith(ALLOWED) or BILLING.fullmatch(f) for f in files)):
            continue
        found.append({"sha": sha, "subject": subject, "files": len(files)})
    return found


_behind: dict[str, list[str]] = {}  # "<repo folder>@<PR head>..<base sha>" -> files both changed: commits never change


def behind(top: Path, base: str, pr: str, head: str, refs: dict | None = None) -> list[str] | None:
    """Files a PR changes that the base branch (as last fetched here) has also changed since the PR's branch last
    took it (turns.changed_on_both): the PR needs the latest base before it merges. Remembered per pair of commits, so
    a refresh asks git again only when the PR or the base branch moved. None when the PR's head can't be read.
    `refs` keeps each base's ref and commit across one check's PRs."""
    refs = {} if refs is None else refs
    if base not in refs:
        ref = _ref(top, base)
        refs[base] = ref, core.git(top, "rev-parse", "--verify", "--quiet", ref, check=False)
    ref, tip = refs[base]
    key = f"{core.posix(top)}@{head}..{tip}"
    if key not in _behind:
        try:
            if not (tip and turns.has_commit(top, head, pr)):
                return None
            _behind[key] = turns.changed_on_both(top, head, ref)
        except WorkkitError:
            return None
    return _behind[key]


# ---------------------------------------------------------------- tests, secrets, instructions, look-alike names

_runs: dict[str, tuple[float, list]] = {}
_blocked: dict[str, str] = {}  # run id -> why its jobs never started ("" when they did); a finished run never changes


def runs(top: Path, gh, fetch: bool = False) -> list[dict] | None:
    """GitHub's 20 newest CI runs of a repo (any branch, newest first), read at most once per RUNS_EVERY seconds and
    shared with the page's CI status, so checking tests costs no extra call. None for a repo without workflows."""
    if not any((top / ".github" / "workflows").glob("*.y*ml")):
        return None
    key, now = core.posix(top), time.monotonic()
    seen = _runs.get(key)
    if seen and now - seen[0] < (FRESH if fetch else RUNS_EVERY):  # Check GitHub now: read once, by check or page
        return seen[1]
    found = json.loads(gh(top, "run", "list", "--limit", "20", "--json",
                          "databaseId,status,conclusion,headBranch,headSha,workflowName,url,createdAt") or "[]")
    found = [r for r in found if isinstance(r, dict)]
    _runs[key] = (now, found)
    return found


def never_started(top: Path, run: dict, gh) -> str:
    """Why a failed run's jobs never started (GitHub refusing to run CI: a failed payment or a spending limit looks
    like an ordinary failure, but no job has steps), or "" when they ran. Read once per run."""
    key = f"{core.posix(top)}#{run['databaseId']}"
    if key not in _blocked:
        jobs = json.loads(gh(top, "run", "view", str(run["databaseId"]), "--json", "jobs") or "{}").get("jobs") or []
        why = ""
        if jobs and all(not j.get("steps") for j in jobs):
            notes = json.loads(gh(top, "api", f"repos/{{owner}}/{{repo}}/check-runs/{jobs[0]['databaseId']}/annotations")
                               or "[]")
            text = " ".join(a.get("message", "") for a in notes if a.get("annotation_level") == "failure")
            why = "GitHub billing/limit" if any(w in text.lower() for w in BILLING_WORDS) else "jobs never started"
        _blocked[key] = why
    return _blocked[key]


def failed_run(top: Path, found: list[dict] | None, branch: str, gh, sha: str = "") -> dict | None:
    """A failed test run on a branch (at `sha`, when given), else None: the newest finished run of each test workflow
    is looked at, so a newer run still going leaves the last verdict standing, and a workflow that tests nothing
    (deploy, release) never counts. A run on another commit than `sha` says nothing; one GitHub refused to start is
    about billing, not the tests."""
    newest = {}
    for r in found or ():
        if (r.get("headBranch") == branch and r.get("status") == "completed"
                and r.get("conclusion") not in ("skipped", "cancelled", "neutral", "")
                and not (sha and r.get("headSha") and r["headSha"] != sha)):
            newest.setdefault(r.get("workflowName") or "", r)  # the list is newest first
    for name, run in newest.items():
        if NOT_TESTS.search(name) or run["conclusion"] not in ("failure", "timed_out", "startup_failure"):
            continue
        try:
            if never_started(top, run, gh):
                continue
        except (WorkkitError, ValueError, KeyError, TypeError, OSError):
            pass
        return run
    return None


def _secret_match(what: str, m: re.Match, path: str) -> bool:
    if what != NAMED:
        return not PLACEHOLDER.search(m[0])
    if m[3] is not None and not SETTINGS_FILE.search(path):
        return False  # unquoted in code is an expression (a variable, a call), not a literal
    return _looks_secret(next(v for v in m.groups() if v is not None))


_secrets: dict[str, list[dict]] = {}  # "<repo folder>@<sha>" -> its findings: a commit's files never change


def secrets(top: Path, ref: str) -> list[dict]:
    """Lines in the files committed on ref that look like a key, token or password: path, line and what it may be,
    never the value. One git grep per commit, remembered."""
    sha = core.git(top, "rev-parse", "--verify", "--quiet", ref, check=False)
    key = f"{core.posix(top)}@{sha}"
    if not sha or key in _secrets:
        return _secrets.get(key, [])
    found = [{"path": f, "line": 0, "what": "a committed .env file"}
             for f in core.git(top, "ls-tree", "-r", "--name-only", sha, check=False).splitlines()
             if ENV_FILE.search(f) and not EXEMPT_PATH.search(f)]
    out = core.run(["git", "-C", str(top), "grep", "-z", "-n", "-I", "-i", "-E", "-e", SECRET_HINT, sha, "--"], 120)
    for hit in out.stdout.splitlines() if out.returncode == 0 else []:
        parts = hit.split("\0", 2)  # <sha>:<path>, line, text
        if len(parts) < 3 or not parts[1].isdigit():
            continue
        path, number, text = parts[0][len(sha) + 1:], parts[1], parts[2]
        if EXEMPT_PATH.search(path) or ENV_FILE.search(path):
            continue  # a .env file is flagged whole, above
        for what, pattern in SECRETS:
            m = next((m for m in pattern.finditer(text) if _secret_match(what, m, path)), None)
            if m:
                found.append({"path": path, "line": int(number), "what": what})
                break
    _secrets[key] = found
    return found


def _words(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8", errors="replace").split())
    except OSError:
        return 0


def long_instructions(top: Path) -> list[tuple[str, int, int]]:
    """The repo's instruction files over their word budget (WORD_BUDGET): (path, words, budget)."""
    files = [(top / n, WORD_BUDGET["instructions"]) for n in ("AGENTS.md", "CLAUDE.md")]
    for d in SKILL_DIRS:
        for skill in sorted((top / d).glob("*/SKILL.md")):
            files += [(f, WORD_BUDGET["skill" if f.name == "SKILL.md" else "reference"])
                      for f in sorted(skill.parent.glob("*.md"))]
    return [(f.relative_to(top).as_posix(), n, budget) for f, budget in files
            if f.is_file() and (n := _words(f)) > budget]


def _name_words(name: str) -> set[str]:
    words = {w for w in re.findall(r"[a-z0-9]+", name.lower()) if w not in FILLER}
    return {w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w for w in words}


def look_alike(pieces: list[tuple[str, dict]]) -> list[tuple[tuple[str, dict], tuple[str, dict]]]:
    """Pairs of open pieces (list, row) whose names share at least ALIKE of their words, and two or more."""
    named = [(p, _name_words(p[1]["piece"])) for p in pieces]
    pairs = []
    for i, (a, wa) in enumerate(named):
        for b, wb in named[i + 1:]:
            shared = wa & wb
            if len(shared) >= 2 and len(shared) / len(wa | wb) >= ALIKE:
                pairs.append((a, b))
    return pairs


# ---------------------------------------------------------------- the check

def _pieces(name: str, product: str, r: work.Repo, facts: dict, today: date) -> tuple[list[dict], list[dict], set]:
    """Problems with one list's pieces, its questions, and the branches and PRs its pieces account for."""
    found, d = [], r.dir / product
    rows = work._read_tsv(d / "pieces.tsv", work.PIECE_COLUMNS)
    prs, remote = facts["prs"], facts["remote"]
    claimed = set()
    set_ = f"wk piece set {name} {product}"
    names = {x["id"]: x["piece"] for x in rows}

    def add(kind, item, what, fix, auto=None):  # auto: the mechanical change wk check --fix makes
        found.append(_problem(kind, name, product, item, what, fix, names.get(item, ""))
                     | ({"auto": auto} if auto else {}))

    for x in rows:
        pid, st, numbers = x["id"], x["state"], _numbers(x["pr"])
        if st == "dropped":
            continue
        is_open = st in work.OPEN
        claimed |= {f"#{n}" for n in numbers}  # a finished piece's open PR is flagged on the piece itself
        if is_open and x["branch"]:
            claimed.add(x["branch"])  # a finished piece's branch left on GitHub is a leftover
        # piece against GitHub
        for n in numbers if prs is not None else []:
            pr = prs.get(n)
            if pr is None:
                add("github", pid, f"records PR #{n}, which GitHub doesn't have", f"{set_} {pid} --pr <the right number>")
            elif is_open and pr["state"] == "MERGED":
                # mechanical only when the piece is ready or building and every PR it records merged: a newer open
                # PR means the piece goes on
                every = st in ("ready", "building") and all((prs.get(m) or {}).get("state") == "MERGED"
                                                             for m in numbers)
                add("github", pid, f"PR #{n} is merged but the piece is still {st}",
                    f"{set_} {pid} --state merged (or, if it has a newer PR, --pr <that number>)",
                    {"piece": pid, "set": {"state": "merged"}} if every else None)
            elif is_open and pr["state"] == "CLOSED":
                add("github", pid, f"PR #{n} was closed without merging but the piece is still {st}",
                    f"{set_} {pid} --pr \"\" (or --state dropped)")
            elif not is_open and pr["state"] == "OPEN":
                add("github", pid, f"is {st} but PR #{n} is still open",
                    f"gh pr close {n}, or reopen the piece: {set_} {pid} --state ready")
        if is_open and x["branch"] and remote is not None and x["branch"] not in remote | facts["local"]:
            add("github", pid, f"records branch {x['branch']}, which exists neither on GitHub nor here",
                f"{set_} {pid} --branch \"\"", {"piece": pid, "set": {"branch": ""}})
        # state rules
        if st == "merged" and not numbers:
            add("state", pid, "is merged but has no PR", f"{set_} {pid} --state done")
        if st == "todo" and (numbers or x["branch"] or x["chat"]):
            has = ", ".join(w for w, v in (("a PR", numbers), ("a branch", x["branch"]), ("a builder", x["chat"])) if v)
            add("state", pid, f"is todo but has {has}",
                f"{set_} {pid} --state {'ready' if numbers else 'building'} (or clear what is stale)")
        if st == "ready" and not numbers:
            add("state", pid, "is ready but has no PR",
                f"{set_} {pid} --pr <number>, or --state done if it ended in a finding")
        for n in numbers if st == "ready" and prs is not None else []:
            pr = prs.get(n) or {}
            run = failed_run(facts["top"], facts.get("runs"), pr.get("headRefName") or "", facts["gh"],
                             pr.get("headRefOid") or "") if pr.get("state") == "OPEN" else None
            if run:
                add("tests", pid, f"waits for the owner's yes, but the tests on PR #{n} failed",
                    f"send it back to its builder to fix the tests ({run.get('url', '')}), then post a new Ready "
                    f"comment; it is not ready for the owner's yes until they pass")
            into = pr.get("baseRefName") or facts["base"]  # the branch this PR merges into (a stacked PR's parent)
            both = behind(facts["top"], into, n, pr.get("headRefOid") or "", facts.get("refs")) \
                if pr.get("state") == "OPEN" else None
            if both:
                add("behind", pid, f"waits for the owner's yes, but {into} has since changed "
                    + ", ".join(both[:5]) + (f" and {len(both) - 5} more" if len(both) > 5 else "")
                    + f", which PR #{n} also changes",
                    f"before asking the owner, its builder takes {into}, re-runs its tests (and sandbox "
                    f"screenshots, where the repo has them) and posts a new Ready comment; wk gate fresh --pr {n} "
                    "refuses the merge until then")
        if st == "building" and not x["chat"].startswith("agent:"):
            add("state", pid, "is building but records no builder agent", f"{set_} {pid} --chat agent:<its name>")
        # name
        why = name_problems(x["piece"])
        if why:
            add("name", pid, "name has " + " and ".join(why) + f" (plain words, {WORDS} at most)",
                f"{set_} {pid} --name \"<plain name>\"")
        # note: a finished piece's note is history, so its tidiness is nobody's to fix
        if not is_open:
            continue
        path = d / "notes" / f"{pid}.md"
        if not path.is_file():
            add("note", pid, "has no note", f"wk piece note {name} {product} {pid} --file <brief.md>")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        touched = max(filter(None, [_date(x["updated"])] + [_date(m[1]) for m in map(work.DATED.match, text.splitlines())
                                                             if m]), default=None)
        if st == "todo" and touched and (today - touched).days >= TODO_DAYS:
            add("stale", pid, f"has been a to-do with no change or note line for {(today - touched).days} days",
                f"ask the owner: start it, park it (wk piece note {name} {product} {pid} --add \"Parked: <why>\") or "
                f"drop it ({set_} {pid} --state dropped)")
        if is_open and not _has_brief(text):
            add("note", pid, "note has no brief before its first dated line",
                f"wk piece note {name} {product} {pid} --file <note with the brief on top>")
        for what in _note_problems(text, today):
            # only a person knows the right date, or what a long line comes down to
            by_hand = "correct the dates" if "future" in what else "shorten them (detail goes in the PR)" \
                if "characters" in what else ""
            add("note", pid, what, f"{by_hand}: wk piece note {name} {product} {pid} --file <note>" if by_hand
                else f"wk check {name} {product} --fix-dates")
        if len(text.encode("utf-8")) > NOTE_BYTES:
            add("note", pid, f"note is {len(text.encode('utf-8')) // 1024} KB (over {NOTE_BYTES // 1024})",
                f"rewrite it to the brief, outcome, decisions and links: wk piece note {name} {product} {pid} --file <note>")
    # questions
    by_id = {x["id"].upper(): x for x in rows}
    for q in work._read_questions(d / "questions.md"):
        qid, answer = q["id"], f"wk question answer {name} {product} {q['id']} \"<the owner's answer>\""
        asked = _date(q.get("asked", ""))
        if asked and (today - asked).days > STALE_DAYS:
            add("reminder", qid, f"has waited {(today - asked).days} days", f"ask the owner again, or close it: {answer}")
        if not q.get("piece") and q.get("scope") != work.LIST_WIDE:
            add("question", qid, "isn't tied to a piece yet",
                f"wk question set {name} {product} {qid} --piece <the piece it is about> (or --list-wide when it "
                f"is about the whole list)")
        for n in _numbers(q.get("pr", "")) if prs is not None else []:
            if n in prs and prs[n]["state"] != "OPEN":
                add("question", qid, f"is about PR #{n}, which is {prs[n]['state'].lower()}", f"close or restate it: {answer}")
        piece = by_id.get((q.get("piece") or "").upper())
        if piece and piece["state"] not in work.OPEN:
            add("question", qid, f"is about {piece['id']}, which is {piece['state']}", f"close it: {answer}")
        if LEADING_PIECE.match(q["question"]):
            add("question", qid, "leads with a piece id instead of standing on its own",
                f"restate it in plain words: {answer}, then wk question add {name} {product} \"...\" --piece <id>")
    return found, rows, claimed


def _memory(s: dict, name: str, product: str, link: str, now=None) -> list[dict]:
    """The list's supervisor chat or a builder of it that went past Claude's compact window lately (usage.py)."""
    try:
        hits = usage.memory(s, name, product, link, now=now)
    except (OSError, ValueError, WorkkitError) as exc:
        return [_problem("memory", name, product, "", f"its chats' memory could not be checked ({exc})",
                         "run wk check again; if it persists, tell the owner")]
    return [_problem("memory", name, product, h["chat"],
                     f"went past the {h['limit'] // 1000}K memory limit without compacting (up to {h['peak'] // 1000}K"
                     + (f", {h['times']} times" if h["times"] > 1 else "") + f", last {h['when']})",
                     f"nothing to undo; tell the owner if it repeats. It clears from here {usage.FLAG_DAYS} days after")
            for h in hits]


def repo_problems(s: dict, name: str, only: str | None = None, today: date | None = None, gh=None,
                  fetch: bool = False, fetched: Future | None = None) -> list[dict]:
    """One repo's problems: its lists' (or only one list's) and the repo's shared ones. `fetch` reads GitHub's
    test runs afresh (Check GitHub now); `fetched` is a git fetch of the repo still running, which what reads the
    fetched branches waits for."""
    today, gh = today or date.today(), gh or _gh
    try:
        r = work.Repo(s, name)
        products = r.products()
    except WorkkitError as exc:
        return [_problem("repo", name, "", "", str(exc), "fix this laptop's settings file or the repo's .work/")]
    if only is not None and only not in products:
        r.product(only)  # the usual error, naming the known lists
    top, base, found, shared = r.top, r.base, [], []
    facts = _facts(top, base, gh, fetch, fetched)  # once it returns, the fetch is in
    shared += [_problem("github", name, "", "", e, "check gh auth status and the network, then run wk check again")
               for e in facts["error"]]
    claimed, all_rows = set(), {}
    for product in products:
        problems, rows, mine = _pieces(name, product, r, facts, today)
        claimed |= mine
        all_rows[product] = rows
        if only is None or product == only:
            found += problems + _memory(s, name, product, str(products[product].get("supervisor") or ""))
    # hours that name no piece, or a piece the list doesn't have (the calls list books hours without pieces)
    cfg = _billing(top)
    calls = str((cfg or {}).get("calls_product") or "")
    if cfg is not None and not cfg.get("coverage_from"):
        shared.append(_problem("billing", name, "", "billing/client.toml", "has no coverage_from, so the invoice refuses",
                               "the owner picks the first day to check; add coverage_from = \"YYYY-MM-DD\" to it"))
    found += _hours(name, r, all_rows, calls, only)
    # repo-wide: PRs and branches no piece accounts for, leftovers, code on the base branch without a PR
    prs, remote = facts["prs"], facts["remote"]
    for n, pr in sorted((prs or {}).items(), key=lambda kv: int(kv[0])):
        if pr["state"] == "OPEN" and f"#{n}" not in claimed and pr.get("headRefName") not in claimed:
            shared.append(_problem("github", name, "", f"PR #{n}", f"is open on {pr.get('headRefName')} but no piece records it",
                                   f"wk piece set {name} <list> <id> --pr {n} --branch {pr.get('headRefName')}"))
    open_heads = {p.get("headRefName") for p in (prs or {}).values() if p["state"] == "OPEN"}
    acked = _acked(r.list_top)
    for b in sorted(remote or ()):
        if b != base and b not in open_heads | claimed | KEEP and f"branch:{b}" not in acked:
            shared.append(_problem("git", name, "", f"branch {b}", "is on GitHub with no open PR and no open piece",
                                   f"after the owner's yes: git push origin --delete {b}; or, to keep it: "
                                   f"wk check {name} --ack branch:{b} --reason \"...\""))
    for b in sorted(facts["local"]):
        if b.startswith("worktree-agent-") and b not in facts["worktrees"]:
            ahead = core.git(top, "rev-list", "--count", b, "--not", f"origin/{base}", check=False)
            if ahead == "0":
                shared.append(_problem("git", name, "", f"branch {b}", "is left from a builder whose folder is gone",
                                       f"git -C {core.posix(top)} branch -d {b}") | {"auto": {"branch": b}})
    try:
        files = keepout.tracked(top, _ref(top, base))
    except WorkkitError as exc:
        files = []
        shared.append(_problem("files", name, "", "", f"its files on {base} could not be looked at ({exc})",
                               f"check that {base} exists here (git -C {core.posix(top)} fetch origin), "
                               "then run wk check again"))
    for path, why in files:
        if f"file:{path}" in acked:
            continue
        keep = f"; or, if a deliberate visual test uses it, after the owner's yes: wk check {name} --ack " \
               f"\"file:{path}\" --reason \"...\"" if path.lower().endswith(keepout.IMAGES) else ""
        shared.append(_problem("files", name, "", path, f"is {why} in git, so it is on GitHub",
                               f"git -C {core.posix(top)} rm --cached -- \"{path}\" (keeps your copy), "
                               "then commit that through a PR" + keep))
    for c in unreviewed(top, base, prs, acked, facts["history"]) if prs is not None else []:
        shared.append(_problem("main", name, "", c["sha"][:7], f"reached {base} without a PR: {c['subject'][:80]}",
                               f"the owner looks at it, then: wk check {name} --ack {c['sha'][:12]} --reason \"...\""))
    run = failed_run(top, facts.get("runs"), base, gh)
    if run:
        shared.append(_problem("tests", name, "", base, f"the newest tests on {base} failed",
                               f"start a builder to fix them through a PR ({run.get('url', '')})"))
    for hit in secrets(top, _ref(top, base)):
        if f"secret:{hit['path']}" in acked:
            continue
        where, what = ((f"{hit['path']} line {hit['line']}", f"may hold {hit['what']}") if hit["line"] else
                       (hit["path"], "is a committed .env file, which usually holds keys and passwords"))
        shared.append(_problem("secret", name, "", where, what,
                               "if it is real: the owner changes it where it was issued, then a PR takes it out of "
                               "the file (git history keeps the old one, so changing it is what protects it); if it "
                               f"isn't: after the owner's yes, wk check {name} --ack \"secret:{hit['path']}\" "
                               "--reason \"...\""))
    for path, words, budget in long_instructions(top):
        shared.append(_problem("bloat", name, "", path, f"is {words:,} words (budget {budget:,})",
                               "a builder trims it to what chats need every time and moves the rest to a reference "
                               "file read only when needed, or to docs/, through a PR"))
    for (pa, a), (pb, b) in look_alike([(p, x) for p, rows in all_rows.items() for x in rows if x["state"] in work.OPEN]):
        if only is None or pb == only:  # shown once, on the list of the second piece
            found.append(_problem("alike", name, pb, b["id"], f"looks like {a['id']} \"{a['piece']}\"" +
                                  (f" on the {pa} list" if pa != pb else ""),
                                  f"if they are one piece, fold {b['id']}'s note into {a['id']} and drop it (wk piece "
                                  f"set {name} {pb} {b['id']} --state dropped); if not, rename one so they read apart",
                                  b["piece"]))
    owner = owner_of(products, calls)
    if only is None or owner in ("", only):
        found += [p | {"owner": owner} for p in shared]
    return found


def _hours(name: str, r: work.Repo, all_rows: dict, calls: str, only: str | None) -> list[dict]:
    """Unbilled hours that name no piece, or a piece their list doesn't have (the calls list books hours without
    pieces); all_rows: each list's pieces."""
    found = []
    for h in work._read_tsv(r.dir / "hours.tsv", work.HOUR_COLUMNS):
        p = h["product"]
        if h["billed"] or p == calls or (only is not None and p != only) or p not in all_rows:
            continue
        fix = f"wk hours fix {name} {p} --date {h['date']} --was \"{h['note'][:40]}\" --for <pieces>"
        named = [i for i in h["pieces"].split(",") if i]
        known = {x["id"].upper() for x in all_rows[p]}
        if not named:
            found.append(_problem("hours", name, p, h["date"], f"{h['hours']}h names no piece", fix))
        elif any(i.upper() not in known for i in named):
            found.append(_problem("hours", name, p, h["date"], f"{h['hours']}h names a piece the list doesn't have", fix))
    return found


def _billing(top: Path) -> dict | None:
    """The repo's billing/client.toml (empty when unreadable), or None when the repo bills nothing."""
    if not (top / "billing").is_dir():
        return None
    try:
        return tomllib.loads((top / "billing" / "client.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def owner_of(products: dict, calls: str = "") -> str:
    """The list whose supervisor acts on a repo's shared problems: the project manager's calls list, else the repo's
    only supervised list, else its only list; "" when that is unclear (then every list sees them)."""
    if calls in products:
        return calls
    supervised = [p for p, f in products.items() if f.get("supervisor")]
    return supervised[0] if len(supervised) == 1 else next(iter(products)) if len(products) == 1 else ""


def check(s: dict, repo: str | None = None, product: str | None = None, today: date | None = None,
          gh=None) -> list[dict]:
    """Every problem in the chosen lists (one repo's, or one list and its repo's shared ones), read-only."""
    today, gh = today or date.today(), gh or _gh
    names = [repo] if repo else list(s.get("repos") or {})
    if repo:
        settings.repo(s, repo)  # an unknown repo is an error, not a clean check
    with ThreadPoolExecutor(max_workers=max(1, len(names))) as pool:
        return [p for found in pool.map(lambda n: repo_problems(s, n, product, today, gh), names) for p in found]


def at_start(s: dict, name: str, product: str, today: date | None = None) -> list[dict]:
    """One list's findings for `wk recall`'s start block, from its own files only: its pieces, notes, questions and
    hours, and, for the list that owns the repo's shared findings, its billing file and instruction files' word
    budgets. Nothing here asks GitHub, fetches, greps the history or reads chat records (PR and branch links, CI,
    leftover branches, secrets, memory stay with wk check), so the start stays fast and never waits on or fails for
    the network. Reminders are left out: the block shows the questions already."""
    r = work.Repo(s, name)
    products = r.products()
    facts = {"prs": None, "remote": None, "local": set(), "top": r.top, "gh": None, "base": r.base, "refs": {}}
    found, rows, _ = _pieces(name, product, r, facts, today or date.today())
    found = [p for p in found if p["kind"] != "reminder"]
    cfg = _billing(r.top)
    calls = str((cfg or {}).get("calls_product") or "")
    found += _hours(name, r, {product: rows}, calls, product)
    if owner_of(products, calls) in ("", product):
        if cfg is not None and not cfg.get("coverage_from"):
            found.append(_problem("billing", name, "", "billing/client.toml", "has no coverage_from, so the invoice "
                                  "refuses", "the owner picks the first day to check; add coverage_from = "
                                  "\"YYYY-MM-DD\" to it"))
        found += [_problem("bloat", name, "", path, f"is {words:,} words (budget {budget:,})",
                           "make it a piece: a builder trims it through a PR")
                  for path, words, budget in long_instructions(r.top)]
    return found


def needs_owner(p: dict) -> bool:
    """A finding only the owner's yes settles (KINDS)."""
    return KINDS.get(p["kind"], ("", "", ""))[2] == "owner"


# ---------------------------------------------------------------- the writes

def fix(s: dict, repo: str | None = None, product: str | None = None, dry_run: bool = False,
        today: date | None = None, gh=None) -> tuple[list[str], list[dict]]:
    """Apply only the mechanical findings, through wk's usual writes (recorded, committed and pushed as wk piece set
    does): a piece still ready or building whose PRs all merged becomes merged; an open piece's branch that exists
    neither on GitHub nor here is cleared; a builder's local branch with nothing off the base branch, whose folder
    is gone, is deleted with git branch -d (which keeps an unmerged one). Never a remote branch, a note, a question
    or code. dry_run says what it would change and changes nothing. Returns (one line per change, the findings
    left)."""
    lines, left, pieces = [], [], {}
    for p in check(s, repo, product, today, gh):
        auto = p.get("auto")
        if not auto:
            left.append(p)
        elif "piece" in auto:
            changes, why = pieces.setdefault((p["repo"], p["product"], auto["piece"]), ({}, []))
            changes.update(auto["set"])
            why.append(p)
        elif dry_run:
            lines.append(f"would delete {p['repo']} local branch {auto['branch']} ({p['what']})")
        else:
            done = core.run(["git", "-C", str(work.Repo(s, p["repo"]).top), "branch", "-d", auto["branch"]], 60)
            if done.returncode:
                lines.append(f"kept {p['repo']} local branch {auto['branch']}: {done.stderr.strip()[-200:]}")
                left.append(p)
            else:
                lines.append(f"deleted {p['repo']} local branch {auto['branch']} ({p['what']})")
    for (name, prod, pid), (changes, why) in pieces.items():
        said = ", ".join(f"{k} {v or 'cleared'}" for k, v in changes.items())
        what = f"{name} {prod} {pid}: {said} (" + "; ".join(p["what"] for p in why) + ")"
        if dry_run:
            lines.append("would set " + what)
            continue
        try:
            push = str(work.set_piece(work.Repo(s, name), prod, pid, **changes).get("push", ""))
        except WorkkitError as exc:
            lines.append(f"could not set {name} {prod} {pid}: {exc}")
            left += why
            continue
        lines.append("set " + what + (f"; {NOT_PUSHED}: {push}" if push.startswith("push failed") else ""))
    return lines, left


NOT_PUSHED = "RECORDED BUT NOT ON GITHUB"  # in a wk check --fix line whose push failed: the command exits 3


def fix_dates(s: dict, repo: str, product: str) -> dict:
    """Rewrite one list's note dates into the form wk reads, once (see tidy), in one commit."""
    r = work.Repo(s, repo)
    notes = r.product(product) / "notes"

    def edit():
        changed = []
        for path in sorted(notes.glob("*.md")) if notes.is_dir() else []:
            text = path.read_text(encoding="utf-8")
            new = tidy(text)
            if new != text:
                core.atomic_text(path, new)
                changed.append(path)
        return changed, f"{product} note dates tidied ({len(changed)} notes)", \
            {"tidied": [p.stem for p in changed]}
    with core.locked(r.list_top, "fix dates", work.LOCK):
        paths, message, result = edit()
        result["push"] = core.save(r.list_top, r.list_base, paths, f"List: {message}") if paths else \
            "nothing to change"
    return result


def ack(s: dict, repo: str, sha: str, reason: str) -> dict:
    """Record that the owner has looked at a commit that reached the base branch without a PR, or chose to keep a branch
    (branch:<name>), or allowed one tracked file that doesn't belong in GitHub (file:<path>) or whose possible secret
    is not one (secret:<path>), so it stops being reported."""
    r = work.Repo(s, repo)
    reason = " ".join((reason or "").split())
    if not reason:
        raise WorkkitError("Say what the owner decided about it (--reason)")
    if sha.startswith("branch:"):
        full = "branch:" + sha[7:].strip()
        if full == "branch:":
            raise WorkkitError("Name the branch to keep: --ack branch:<name>")
    elif sha.startswith(("file:", "secret:")):
        kind, _, given = sha.partition(":")
        full = f"{kind}:" + given.strip().replace("\\", "/")
        if full == f"{kind}:":
            raise WorkkitError(f"Name the file to allow, as wk check shows it: --ack {kind}:<path>")
    else:
        full = core.git(r.top, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}", check=False)
    if not full:
        raise WorkkitError(f"No commit {sha} in {repo}")
    path, label = r.dir / ACK, full if full.startswith(("branch:", "file:", "secret:")) else full[:12]

    def edit():
        rows = work._read_tsv(path, ACK_COLUMNS)
        if any(x["sha"] == full for x in rows):
            raise WorkkitError(f"{label} is already acknowledged")
        row = {"sha": full, "date": date.today().isoformat(), "reason": reason}
        work._write_tsv(path, ACK_COLUMNS, rows + [row])
        return [path], f"acknowledged {label}: {reason}", {"acknowledged": row}
    return r.write("check ack", edit)
