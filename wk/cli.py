"""nh: the nearly-headless command line. Exit status 0 = done, 1 = refused (a gate or turn said no) or nh check found
problems, 2 = error,
3 = recorded but not on GitHub (push failed)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__, core
from .core import KitError

# Every other module loads only when a command needs it, so `nh update`, `nh version`, `recall` and `reply-check`
# still run when an unrelated module is broken: a broken release never blocks its own repair. So the parser names
# these values itself; a test keeps each equal to its module's.
TURN_HOURS = 2.0  # turns.HOURS
PIECE_STATES = ["todo", "building", "ready", "merged", "done", "dropped"]  # work.STATES
BILL_CHOICES = ["yes", "later", "no"]  # work.BILL


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="nh", description=__doc__)
    p.add_argument("--settings", type=Path,
                   help="this laptop's settings file (default: $NH_SETTINGS, else ~/.nearly-headless/settings.toml)")
    p.add_argument("--cwd", type=Path, default=Path.cwd(), help="checkout to act as (default: current folder)")
    p.add_argument("--json", action="store_true", help="print JSON instead of a table where a table is the default")
    p.add_argument("--version", action="version", version=__version__, help="print the nearly-headless version and stop")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("turn", help="Take turns on a shared resource, in any repo (exit 1 = refused)",
                       description="One branch at a time holds each item of a shared resource. A turn expires after "
                                   "--hours (default 2) unless renewed; pause stops the clock while waiting on approval. "
                                   "take is refused while another branch holds an item, or when this branch lacks the "
                                   "latest origin/<base> (commits that only record work in .work/ do not count). "
                                   "check exits 0 only when this branch holds every item: deploy tools call it. "
                                   "State lives in the repo's shared Git folder, never committed.")
    s.add_argument("action", choices=["take", "renew", "pause", "resume", "release", "list", "check"])
    s.add_argument("resource", nargs="?", help="e.g. test-env (optional for renew/pause/resume/release/list)")
    s.add_argument("items", nargs="*", help="items of the resource, e.g. the files a deploy sends")
    s.add_argument("--hours", type=float, default=TURN_HOURS, help="take/renew: hours until it expires (default 2)")

    s = sub.add_parser("gate", help="Release gates for any repo (exit 0 = pass, 1 = refused)",
                       description="release: passes only on the base branch, with a clean tree, at origin/<base> after "
                                   "a fetch. approved: passes only when a PR comment has its own line "
                                   "'Approved by the owner: <action> @ <sha>' (case-insensitive; <sha> is at least 7 "
                                   "characters of the PR's current head; on a private repo the approver's name from "
                                   "the settings, as approvals before 1.56.0 read, also counts). "
                                   "Any new push voids it. fresh: passes only when the PR "
                                   "already holds every change origin/<base> has had, since the PR's branch last took "
                                   "it, to the files the PR changes (PRs on other files never wait), and, with --sha "
                                   "(the approved commit), the PR's head is that commit or only clean merges of "
                                   "<base> after it with CI green (the yes carries over); run it before every merge.")
    s.add_argument("which", choices=["release", "approved", "fresh"])
    s.add_argument("--pr", type=int, help="approved, fresh: the pull request number")
    s.add_argument("--action", help="approved: the action text, e.g. \"deploy to production\"")
    s.add_argument("--sha", default="", help="fresh: the commit the owner approved (7+ characters)")

    s = sub.add_parser("refresh", help="Bring a stale PR up to date with its base branch, with no builder "
                                       "(exit 1 = the builder is needed)",
                       description="In the PR's builder worktree (the one with its branch checked out; never the main "
                                   "checkout), fetch and merge origin/<base>. Clean: push (never forced), wait for the "
                                   "PR's CI, post 'Ready @ <sha>' and print the new sha. A conflict, uncommitted "
                                   "changes, unpushed work, a change needing the full run (.github/workflows/ or the map's full-run row), a latest Ready that isn't 'Gap checks: none', or failing CI (a verdict counts when two polls in a "
                                   "row agree): change nothing (a conflict's merge is "
                                   "aborted) and say the builder is needed. Run it before nh gate fresh.")
    s.add_argument("--pr", type=int, required=True, help="the pull request number")
    s.add_argument("--wait", type=float, default=30.0, help="minutes to wait for CI (default 30)")

    s = sub.add_parser("product", help="Declare a product in a repo's .work/, with its invoice line and supervisor")
    s.add_argument("action", choices=["add", "set", "list"])
    s.add_argument("repo")
    s.add_argument("name", nargs="?")
    s.add_argument("--line", help="add/set: what the list is, in the client's words (its invoice line); set: "
                                  "key=\"...\" adds or changes a named extra line, so hours booked --line-key key "
                                  "bill as their own invoice row (key= removes it). Named lines are set with "
                                  "product set only; add refuses key=")
    s.add_argument("--supervisor", help="set: the supervisor's chat, as a link that opens it (empty clears it)")
    s.add_argument("--title", help="set: the name the command center shows, e.g. \"AP automation\"")

    def where(s):
        s.add_argument("repo", help="the repo's name in this laptop's settings")
        s.add_argument("product")

    s = sub.add_parser("piece", help="A product's pieces of work: one row each, with a note as its memory "
                                     "(add, set, note, rank, queue, next, list)")
    s.add_argument("action", choices=["add", "set", "note", "rank", "queue", "next", "list"])
    where(s)
    s.add_argument("text", nargs="?", help="add: the piece in a few words; set/note: its id (P3); rank: the open "
                                           "pieces in the order you recommend (P3,P1,P7); queue: the to-do pieces "
                                           "in the order the owner approved (P5,P7), or none; next: nothing, it "
                                           "says which queued pieces start now and why the others wait")
    s.add_argument("--add", dest="line", help="note: add one dated line (a decision, finding or where it stands)")
    s.add_argument("--file", type=Path, help="note: replace the whole note with this file's text")
    s.add_argument("--billed-ok", action="store_true", help="note: let an --add line onto a finished, billed piece "
                                                            "(a release or teardown note, not new work)")
    s.add_argument("--state", choices=PIECE_STATES)
    s.add_argument("--branch")
    s.add_argument("--pr")
    s.add_argument("--approved", help="the commit the owner approved")
    s.add_argument("--chat", help="set: the builder's chat, as a link that opens it")
    s.add_argument("--name", help="set: a new short name for the piece, in plain words (8 words at most)")
    s.add_argument("--bill", choices=BILL_CHOICES, help="set: yes (bill it with its hours, the default), later or "
                                                          "no; later and no need --reason, added to its note")
    s.add_argument("--reason", help="set: why it is billed later or not billed")
    s.add_argument("--files", help="set: the files the piece will touch, comma separated (paths, folders ending in /, "
                                   "globs), or none; a queued piece never starts on files an unmerged PR or turn holds")
    s.add_argument("--after", help="set: the pieces whose merge it needs first (P3,P4); a queued piece waits for them")
    s.add_argument("--all", action="store_true", help="list: include finished (merged, done) and dropped pieces")

    s = sub.add_parser("question", help="Questions waiting on the owner (add, answer, withdraw, set, list)")
    s.add_argument("action", choices=["add", "answer", "withdraw", "set", "list"])
    where(s)
    s.add_argument("text", nargs="*", help="add: the question; answer: its id (Q2) then the owner's answer; "
                                           "set: its id")
    s.add_argument("--options")
    s.add_argument("--recommended")
    s.add_argument("--pr")
    s.add_argument("--piece", help="add (required unless --list-wide), set: the piece it is about (P3); answering it "
                                   "adds the answer to that piece's note; withdraw: for a whole-list question, the "
                                   "piece whose note keeps your decision")
    s.add_argument("--list-wide", action="store_true", help="add, set: the question is about the whole list, not one piece")
    s.add_argument("--reason", help="withdraw: what the supervisor decided itself and why it can be undone")

    s = sub.add_parser("order", help="The owner's standing orders for a product (add, drop, list)")
    s.add_argument("action", choices=["add", "drop", "list"])
    where(s)
    s.add_argument("text", nargs="?", help="add: the order; drop: its number")

    s = sub.add_parser("channels", help="A client's channels file for the routing run (show; move a channel's "
                                        "last-checked time)",
                       description="Prints .work/communication_channels.md, starting it from the kit's template if "
                                   "missing and saving any hand edits, then each channel's last-checked time on this "
                                   "laptop and the time to read it from (that time less a short overlap). Otherwise "
                                   "it writes nothing. --checked with --at moves one channel's last-checked time: "
                                   "this laptop's own state in the repo's Git folder, never committed; never "
                                   "backwards, never into the future, never while the template's examples are there.")
    s.add_argument("repo", help="the repo's name in this laptop's settings")
    s.add_argument("--checked", default="", help="the channel read in full, by its heading in the file")
    s.add_argument("--at", default="", help="with --checked: when its read started, e.g. 2026-11-04T10:30+05:00 "
                                            "(no zone: local time)")

    s = sub.add_parser("inbox", help="Messages routed to a list, each a link and one line (add, list, done, check)",
                       description="add: file a message for the list's supervisor; refused (exit 1) when its link "
                                   "is already filed in the repo. done: the supervisor handled it, so it is removed. "
                                   "check: exit 0 when a link is new, 1 when it is filed (in any inbox, or in any "
                                   "commit to .work/).")
    s.add_argument("action", choices=["add", "list", "done", "check"])
    s.add_argument("repo", help="the repo's name in this laptop's settings")
    s.add_argument("product", nargs="?", help="the list (not needed for check)")
    s.add_argument("text", nargs="*", help="add: one line about the message (never its text); done: the entry's id (I2)")
    s.add_argument("--link", default="", help="add, check: the link that opens the message")
    s.add_argument("--channel", default="", help="add: the channel it came from, by its heading in the channels file")
    s.add_argument("--when", default="", help="add: when it was sent")
    s.add_argument("--piece", default="", help="add: the open piece it is likely about (P3)")
    s.add_argument("--how", default="", help="done: how it was handled (FYI, note on P3, new piece P9, draft Q4)")

    sub.add_parser("status", help="Every repo's open pieces and questions, computed from their .work/ folders")

    s = sub.add_parser("check", help="Check lists against GitHub and the rules: one line per problem and its fix "
                                     "(exit 1 = problems found)",
                       description="Read-only. Checks each list's pieces against the repo's PRs and branches on "
                                   "GitHub, the state rules, names, notes, questions, unbilled hours, billing, leftover "
                                   "branches, and commits on the base branch that came without a PR. With a list, its "
                                   "repo's shared problems are shown too. --fix-dates (one list) rewrites its notes' "
                                   "date lines into the form wk reads, once; --ack records that the owner has looked "
                                   "at a commit that reached the base branch without a PR. --fix makes only the "
                                   "mechanical fixes (a piece still ready or building after its PRs merged, a piece's "
                                   "branch that no longer exists, a builder's merged local branch whose folder is "
                                   "gone, with git branch -d), one line each, and lists the rest; --fix --dry-run "
                                   "only says what it would change.")
    s.add_argument("repo", nargs="?")
    s.add_argument("product", nargs="?")
    s.add_argument("--fix-dates", action="store_true", help="rewrite one list's doubled or unreadable note dates")
    s.add_argument("--fix", action="store_true", help="make the mechanical fixes only, through nh's usual writes")
    s.add_argument("--dry-run", action="store_true", help="--fix: say what it would change, change nothing")
    s.add_argument("--ack", metavar="SHA", help="a commit on the base branch the owner has looked at, or "
                                                "branch:<name>, a branch the owner keeps, or "
                                                "file:<path>, a tracked file the owner allows, or "
                                                "secret:<path>, a file whose possible secret is not one")
    s.add_argument("--reason", help="--ack: what the owner decided about it")

    s = sub.add_parser("hours", help="Hours the owner stated, per product (add, fix, list, mark)",
                       description="add: --for names the pieces the hours pay for, so an invoice can tell finished "
                                   "work that was never billed. fix: change or remove one unbilled entry (--date, "
                                   "and --was when the day has several); billed entries never change. mark: label a "
                                   "repo's unmarked hours through --through as done (an invoice or timesheet), for "
                                   "add-ons such as invoicing. --expect is the hours counted; any difference refuses. "
                                   "A label is used once per repo.")
    s.add_argument("action", choices=["add", "fix", "list", "mark"])
    s.add_argument("repo", nargs="?")
    s.add_argument("product", nargs="?")
    s.add_argument("hours", nargs="?", type=float)
    s.add_argument("--note")
    s.add_argument("--date", default="")
    s.add_argument("--again", action="store_true", help="separate work on a day that already has hours")
    s.add_argument("--for", dest="pieces", help="add/fix: the pieces the hours pay for, e.g. P3,P7")
    s.add_argument("--line-key", dest="line_key", help="add/fix: the list's named invoice line these hours bill on "
                                                      "(nh product set --line key=...); fix: \"\" for its usual line")
    s.add_argument("--was", default="", help="fix: words from the entry's note, when the day has several")
    s.add_argument("--remove", action="store_true", help="fix: remove the entry")
    s.add_argument("--unbilled", action="store_true")
    s.add_argument("--through", help="mark: the last day included (YYYY-MM-DD)")
    s.add_argument("--as", dest="label", help="mark: the label, e.g. INV-007")
    s.add_argument("--expect", type=float, help="mark: the hours counted")
    s.add_argument("--digest", default="", help="mark: the digest of the rows counted (see work.rows_digest)")
    s.add_argument("--nth", type=int, default=0, help="fix: the entry's place among that day's, from 1")

    sub.add_parser("recall", help="What a supervisor reads when its chat starts or resumes, or after its memory is "
                                  "compacted or cleared (run by a Claude hook)",
                   description="Prints the list(s) this chat supervises: questions for the owner, open pieces with "
                               "builder, branch, PR state and CI, their newest note line and the owner's decisions, "
                               "turns held, inbox, standing orders, within about 4,000 characters. Claude Code runs it "
                               "when a chat starts, resumes, is compacted or cleared, and passes the session as JSON "
                               "on stdin; the chat is matched to the list whose "
                               "supervisor link names it. Elsewhere it prints one line or nothing. Always exits 0.")

    s = sub.add_parser("reply-check", help="Sends a supervisor back to add a missing part of its reply (run by a Claude hook)",
                   description="Claude Code runs it when a chat is about to stop, passing the turn as JSON on stdin. "
                               "In a chat that loaded the supervisor skill: when the settings file lists [reply] "
                               "require = [\"...\", ...], a last message missing one of them is sent back once to be "
                               "rewritten; and, unless [reply] record = false, a turn that drafted a message, "
                               "investigated or reviewed a document for the owner but ran no nh piece add or nh piece "
                               "note is sent back once to record it. Anything else is let through. One line per check goes to "
                               "reply-check.log "
                               "beside the settings file. Always exits 0.")
    s.add_argument("--remind", action="store_true",
                   help="run by a UserPromptSubmit hook: print one line naming the required parts (nothing when none "
                        "are set), so the reply is right the first time")

    s = sub.add_parser("health", help="Is the system healthier on tokens? One table by period, per day, with the "
                                      "nearly-headless version live and a verdict per measure (read-only)",
                       description="Read-only, over every Claude Code transcript on this laptop: tokens by type, "
                                   "calls, context per call, agent runs, wake-ups, reviews, merged PRs, reply-check "
                                   "rejections and nh refresh uses, for today so far, yesterday, this week, last week "
                                   "and the previous N full days, each per day, with healthier / worse / flat against "
                                   "the period before.")
    s.add_argument("--days", type=int, default=14, help="the long period's length in full days (default 14)")
    s.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="print JSON instead of the table")

    s = sub.add_parser("serve", help="Open the command center: a local page showing every repo in the settings")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--save", type=Path, help="write the page once to this file, after checking GitHub, and stop")
    s.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)  # started by nh serve itself
    s = sub.add_parser("privacy", help="The privacy guard: stop private names reaching a public repo (exit 1 = a "
                                       "private name was found; only where, never the name, is printed)",
                       description="push: git's pre-push hook in a public checkout (nh setup installs it) checks the "
                                   "branch, commit messages and added lines. scan FOLDER: every file in a folder. pr: "
                                   "the GitHub job on each PR (its text, commits and the lines it adds), from the "
                                   "PRIVATE_TERMS secret. sweep: the daily GitHub job (tracked files, recent commits, "
                                   "issues, PRs and comments). secret OWNER/REPO: sets that secret from this laptop's "
                                   "list and records its fingerprint, which nh doctor compares. tree FOLDER: builds "
                                   "the clean public copy of this repo (every file on --ref except .work/) in a new "
                                   "folder, then scans it. The list: every repo, folder and client in the settings, "
                                   "the owner's names, and [privacy] terms.")
    s.add_argument("what", choices=["push", "scan", "pr", "sweep", "secret", "tree"])
    s.add_argument("rest", nargs="*", help="push: git's remote name and URL; scan, tree: the folder; secret: "
                                           "owner/repo")
    s.add_argument("--term", action="append", default=[], help="scan, tree: one more private name, for this run only")
    s.add_argument("--ref", default="origin/main", help="tree: what to build it from (default origin/main)")
    s = sub.add_parser("setup", help="Set up this laptop: settings file (if none; else adds any --repo it lacks) and the skills")
    s.add_argument("--owner", default="")
    s.add_argument("--repo", action="append", default=[], help="name=path, once per repo")
    s.add_argument("--profile", default="", help="optional: the WWID profile repo's folder (wwid_profile), which "
                                                 "the wwid skill reads and records verdicts to")
    s = sub.add_parser("test", help="Run only the checks the repo's change -> check map names for what this branch "
                                    "changed against origin/<base> (exit 1 = a check failed or a full run is needed; "
                                    "4 = Docker needed; 5 = the Linux image didn't build)",
                       description="Reads the `## Change → check` map in AGENTS.md or CONTRIBUTING.md, matches every "
                                   "file this branch changed (committed or not) and runs each mapped command once. A "
                                   "file no row names, or the full-run row, prints 'full run needed' and exits 1: nh "
                                   "test never runs the whole suite unasked; CI does. On a laptop that isn't Linux, the "
                                   "checks run only in a Linux container (the repo's wk-test.Dockerfile, else the kit's "
                                   "Node or Python image); CI-only map lines are skipped, laptop-only ones run here. Docker "
                                   "off, nh starts Docker Desktop and waits; if it can't, nothing runs (exit 4), "
                                   "and an image that won't build runs nothing either (exit 5). "
                                   "--local runs them here.")
    s.add_argument("modules", nargs="*", help="run just these unittest modules (e.g. tests.test_work) instead")
    s.add_argument("--plan", action="store_true", help="only say what it would run")
    s.add_argument("--full", action="store_true", help="run the map's full-run row (the whole suite) instead")
    s.add_argument("--local", action="store_true", help="run here, never in a container")
    sub.add_parser("update", help="Move to the newest nearly-headless release and reinstall the skills")
    sub.add_parser("doctor", help="Check settings, repos, skills and CI (exit 1 if anything is off)")
    sub.add_parser("version", help="Print the nearly-headless version")
    return p


def _unpushed(result) -> bool:
    return isinstance(result, dict) and str(result.get("push", "")).startswith("push failed")


def table(rows: list[list[str]]) -> str:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows)


def _turn(args, cwd, s):
    from . import turns
    a, res, items = args.action, args.resource, args.items
    if a in ("take", "check") and not (res and items):
        raise KitError(f"nh turn {a} needs a resource and at least one item")
    if a == "take":
        return turns.take(cwd, s, res, items, args.hours)
    if a == "renew":
        return turns.renew(cwd, res, items, args.hours)
    if a == "check":
        return turns.check(cwd, res, items)
    if a == "list":
        rows = [g for g in turns.rows(core.checkout_of(cwd)[0]) if not res or g["resource"] == res]
        return rows, table([["RESOURCE", "ITEMS", "HOLDER", "STATE"]] + [
            [g["resource"], " ".join(g["items"]), g["holder"], g["state"]] for g in rows]) if rows else "No turns held"
    return getattr(turns, a)(cwd, res, items)


def _gate(args, cwd, s):
    from . import turns
    if args.which == "release":
        return turns.gate_release(cwd, s)
    if args.which == "fresh":
        if not args.pr:
            raise KitError("nh gate fresh needs --pr N")
        return turns.gate_fresh(cwd, s, args.pr, args.sha)
    if not (args.pr and args.action):
        raise KitError("nh gate approved needs --pr N and --action \"...\"")
    return turns.gate_approved(cwd, s, args.pr, args.action)


def _product(args, s):
    from . import work
    r = work.Repo(s, args.repo)
    if args.action == "add":
        return work.add_product(r, args.name or "", args.line or "")
    if args.action == "set":
        if not args.name:
            raise KitError("nh product set needs the product's name")
        return work.set_product(r, args.name, args.supervisor, args.title, args.line)
    products = r.products()
    return products, "\n".join(f"{p}: {v.get('line', '')}" + (f"  (supervisor: {v['supervisor']})"
                                                                 if v.get("supervisor") else "")
                               + "".join(f"\n  {k[5:]}: {x}" for k, x in v.items() if k.startswith("line-"))
                               for p, v in products.items()) or "No products declared"


def _piece(args, s):
    from . import work
    r, p = work.Repo(s, args.repo), args.product
    if args.action == "add":
        return work.add_piece(r, p, args.text or "")
    if args.action == "set":
        if not args.text:
            raise KitError("nh piece set needs the piece's id")
        if args.bill in ("later", "no") and not (args.reason or "").strip():
            raise KitError(f"Say why it is billed {'later' if args.bill == 'later' else 'not at all'} (--reason)")
        line = ""
        if args.bill:  # checked before the first write, so a refused note never leaves half a change
            said = {"yes": "Billed with its hours", "later": "Bill later", "no": "Not billing"}[args.bill]
            line = work.note_line(f"{said}" + (f": {args.reason}" if args.reason else ""))
        done = work.set_piece(r, p, args.text, state=args.state, branch=args.branch, pr=args.pr,
                              approved=args.approved, chat=args.chat, piece=args.name, bill=args.bill,
                              files=args.files, after=args.after)
        if line:
            noted = work.note(r, p, args.text, line)
            if _unpushed(noted) and not _unpushed(done):  # either commit not on GitHub: exit 3
                done["push"] = noted["push"]
        return done
    if args.action == "rank":
        return work.rank_pieces(r, p, args.text or "")
    if args.action == "queue":
        return work.queue_pieces(r, p, args.text or "")
    if args.action == "next":
        q = work.queue_status(s, r, p)
        return q, "\n".join(work.queue_lines(q)) or "The queue is empty: nothing the owner approved waits to start"
    if args.action == "note":
        if not args.text:
            raise KitError("nh piece note needs the piece's id")
        if args.line or args.file:
            return work.note(r, p, args.text, args.line or "",
                             args.file.read_text(encoding="utf-8") if args.file else None,
                             new_work=not args.billed_ok)
        text = work.read_note(r, p, args.text)
        return {"piece": args.text.upper(), "note": text}, text or f"{args.text.upper()} has no note yet"
    rows = [x for x in work.pieces(r, p) if args.all or x["state"] in work.OPEN]
    return rows, table([["ID", "PIECE", "STATE", "PR", "APPROVED"]] + [
        [x["id"], x["piece"], x["state"], x["pr"], x["approved"]] for x in rows]) if rows else "No open pieces"


def _question(args, s):
    from . import work
    r, p = work.Repo(s, args.repo), args.product
    if args.action == "add":
        return work.ask(r, p, " ".join(args.text), args.options or "", args.recommended or "", args.pr or "",
                        args.piece or "", args.list_wide)
    if args.action == "set":
        if len(args.text) != 1:
            raise KitError("nh question set needs the question's id: nh question set <repo> <product> Q2 "
                               "--piece P3 (or --list-wide)")
        return work.link_question(r, p, args.text[0], args.piece or "", args.list_wide)
    if args.action == "answer":
        if len(args.text) < 2:
            raise KitError("nh question answer needs the question's id and the owner's answer")
        return work.answer(r, p, args.text[0], " ".join(args.text[1:]))
    if args.action == "withdraw":
        if len(args.text) != 1:
            raise KitError("nh question withdraw needs the question's id and --reason \"...\"")
        return work.withdraw(r, p, args.text[0], args.reason or "", args.piece or "")
    items = work.questions(r, p)
    return items, "\n".join(f"{q['id']}  {q['question']}" + (f"  (recommended: {q['recommended']})"
                                                               if q.get("recommended") else "") for q in items) \
        or "Nothing waiting on the owner"


def _order(args, s):
    from . import work
    r, p = work.Repo(s, args.repo), args.product
    if args.action == "add":
        return work.order(r, p, add=args.text or "")
    if args.action == "drop":
        if not (args.text or "").isdigit():
            raise KitError("nh order drop needs the order's number")
        return work.order(r, p, drop=int(args.text))
    lines = work.orders(r, p)
    return lines, "\n".join(f"{n}. {t}" for n, t in enumerate(lines, 1)) or "No standing orders"


def _channels(args, s):
    """Every run prints the same head, written or not; the file follows unless the run only moved a time."""
    from . import work
    result = work.channels(work.Repo(s, args.repo), args.checked, args.at)
    head = [result["channels"], f"now: {result['now']}"]
    if result["template"]:
        head.append("NOT FILLED IN: still the template's example channels; no routing run reads them until the "
                    "owner's replace them")
    head += [f"{x['channel']}: last checked {x['last_checked']}, read from {x['read_from']}" for x in result["list"]]
    if "push" in result:  # it wrote: say whether it reached GitHub
        head.append(f"saved: {result['push']}")
    if "moved" in result:
        return result, "\n".join(head + [f"moved: {k} {v}" for k, v in result["moved"].items()])
    return result, "\n".join(head) + f"\n\n{result['text']}"


def _inbox(args, s):
    from . import turns, work
    r, text = work.Repo(s, args.repo), " ".join(args.text)
    if args.action == "check":
        where = work.filed(r, args.link)
        if where:
            raise turns.Refused(f"Already filed ({where}): {work._clean(args.link)}")
        return {"link": work._clean(args.link), "filed": ""}, "new"
    if not args.product:
        raise KitError(f"nh inbox {args.action} needs the list: nh inbox {args.action} <repo> <list>")
    if args.action == "add":
        return work.inbox_add(r, args.product, text, args.link, args.channel, args.when, args.piece)
    if args.action == "done":
        if len(args.text) != 1:
            raise KitError("nh inbox done needs the entry's id: nh inbox done <repo> <list> I2 --how \"...\"")
        return work.inbox_done(r, args.product, args.text[0], args.how)
    items = work.inbox(r, args.product)
    return items, "\n".join(f"{x['id']}  {x['line']}  ({x.get('channel', '')}"
                             + (f", {x['when']}" if x.get("when") else "") + (f", about {x['piece']}" if x.get("piece") else "")
                             + f")\n    {x.get('link', '')}" for x in items) or "Inbox empty"


def _status(s):
    from . import work
    rows = work.status(s)
    out = []
    for x in rows:
        if "error" in x:
            out.append(f"{x['repo']}: {x['error']}")
            continue
        waiting = f"  · {len(x['questions'])} question(s) for the owner" if x["questions"] else ""
        out.append(f"{x['repo']} {x['product']}: {len(x['open'])} open{waiting}")
        out += [f"  {p['id']}  {p['state']:<8}  {p['piece']}" + (f"  #{p['pr'].lstrip('#')}" if p["pr"] else "")
                for p in x["open"]]
    return rows, "\n".join(out) or "No products declared in any repo"


def _check(args, s):
    from . import check
    if args.ack:
        if not args.repo:
            raise KitError("nh check --ack needs the repo: nh check <repo> --ack <sha> --reason \"...\"")
        return check.ack(s, args.repo, args.ack, args.reason or "")
    if args.fix_dates:
        if not (args.repo and args.product):
            raise KitError("nh check --fix-dates works on one list: nh check <repo> <product> --fix-dates")
        return check.fix_dates(s, args.repo, args.product)
    if args.dry_run and not args.fix:
        raise KitError("--dry-run goes with --fix: nh check [repo] [product] --fix --dry-run")
    changed = []
    if args.fix:
        changed, every = check.fix(s, args.repo, args.product, args.dry_run)
    else:
        every = check.check(s, args.repo, args.product)
    found = [p for p in every if p["kind"] != "reminder"]
    text = "\n".join(check.line(p) for p in found) or "No problems found"
    if args.fix:
        text = ("\n".join(changed) or ("Nothing it would fix" if args.dry_run else "Nothing to fix")) \
            + "\nLeft as findings:\n" + text
    if found:
        text += f"\n{len(found)} problem(s)"
    reminders = [p for p in every if p["kind"] == "reminder"]
    if reminders:  # not problems: nudges for the supervisor
        text += "\nReminders (not problems):\n" + "\n".join(check.line(p) for p in reminders)
    return found, text, changed


def _hours(args, s, cwd):
    from . import work
    if args.action == "add":
        if not (args.repo and args.product and args.hours is not None):
            raise KitError("nh hours add needs a repo, a product and the hours")
        return work.add_hours(work.Repo(s, args.repo), cwd, args.product, args.hours, args.note or "", args.date,
                              args.again, args.pieces or "", args.line_key or "")
    if args.action == "fix":
        if not (args.repo and args.product and args.date):
            raise KitError("nh hours fix needs a repo, a product and --date (then the new hours, --note, --for, "
                               "--line-key or --remove)")
        return work.fix_hours(work.Repo(s, args.repo), args.product, args.date, args.hours, args.note, args.pieces,
                              args.remove, args.was, args.nth, args.line_key)
    if args.action == "mark":
        if not (args.repo and args.through and args.label and args.expect is not None):
            raise KitError("nh hours mark needs a repo, --through, --as and --expect (optionally a product)")
        return work.mark_hours(work.Repo(s, args.repo), args.through, args.label, args.expect,
                               [args.product] if args.product else [], args.digest)
    names = [args.repo] if args.repo else list(s.get("repos") or {})
    result = [work.hours(work.Repo(s, n), args.unbilled) for n in names]
    lines = [f"{x['repo']} {p}: {h:g}h" for x in result for p, h in x["totals"].items()]
    return result, "\n".join(lines + [f"Total: {sum(x['total'] for x in result):g}h"])


def _recall(args, cwd) -> int:
    """Run by a Claude Code hook at session start, so it must never fail it: no daily update, and any error prints
    nothing and still exits 0. A subagent (a builder) gets nothing: its brief is its memory."""
    try:
        from . import settings, work
        hook = {}
        if not sys.stdin.isatty():
            hook = json.loads(sys.stdin.read() or "{}")
        if not isinstance(hook, dict) or hook.get("agent_id"):
            return 0
        ids = [str(hook.get("session_id") or ""), os.environ.get("CLAUDE_CODE_HOST_SESSION_ID", "")]
        here = Path(hook["cwd"]).resolve() if hook.get("cwd") else cwd
        text = work.recall(settings.load(settings.path(args.settings)), ids, here, str(hook.get("source") or ""))
        if text:
            print(text)
    except Exception:  # noqa: BLE001 - a broken settings file or repo must never break a chat's start
        pass
    return 0


def _reply_check(args) -> int:
    """Run by a Claude Code Stop hook, so it must never break a chat: no daily update, and any error lets Claude stop.
    Sending Claude back is a JSON "block" decision on stdout; letting it stop is printing nothing."""
    try:
        from . import reply, settings
        hook = json.loads(sys.stdin.read() or "{}") if not sys.stdin.isatty() else {}
        if not isinstance(hook, dict):
            return 0
        where = settings.path(args.settings)
        if args.remind:
            line = "" if hook.get("agent_id") else reply.remind(settings.load(where))
            if line:
                print(line)
            return 0
        missing, session = reply.check(hook, settings.load(where))
        if missing is None:
            return 0
        retry = bool(hook.get("stop_hook_active"))
        try:
            reply.log(where.parent / "reply-check.log", session, missing, retry)
        except OSError:
            pass  # a log that can't be written never changes what Claude does
        if missing and not retry:
            print(json.dumps({"decision": "block", "reason": reply.reason(missing)}))
    except Exception:  # noqa: BLE001 - bad input, settings or transcript: let the reply through
        pass
    return 0


def _privacy(args, cwd: Path) -> int:
    """Runs before the daily update, so a push is never held up by one."""
    from . import privacy, settings
    try:
        if args.what in ("pr", "sweep"):
            code, text = (privacy.ci if args.what == "pr" else privacy.sweep)(cwd=cwd)
            print(text)
            return code
        s = settings.load(settings.path(args.settings))
        names = privacy.terms(s, args.term)
        if args.what == "secret":
            if len(args.rest) != 1 or not names:
                raise KitError("nh privacy secret OWNER/REPO, on a laptop whose settings name its repos")
            print(json.dumps(privacy.set_secret(args.rest[0], names), indent=2))
            return 0
        pat = privacy.pattern(names)
        if args.what == "tree":
            if len(args.rest) != 1:
                raise KitError("nh privacy tree FOLDER [--ref REF], run in the repo's checkout")
            built = privacy.tree(core.checkout_of(cwd)[0], Path(args.rest[0]), args.ref)
            hits = privacy.scan(Path(args.rest[0]), pat)
            print(json.dumps(built | {"private_names": len(names), "hits": len(hits)}, indent=2))
            for h in hits:
                print(f"  {h}")
            return 1 if hits else 0
        if args.what == "scan":
            if len(args.rest) != 1 or not Path(args.rest[0]).is_dir():
                raise KitError("nh privacy scan FOLDER")
            hits = privacy.scan(Path(args.rest[0]), pat)
            print(f"{len(hits)} hits in {core.posix(Path(args.rest[0]).resolve())} ({len(names)} private names)")
            out = sys.stdout
        else:
            hits = privacy.push_hits(cwd, sys.stdin.read(), pat)
            out = sys.stderr
            if hits:
                print("nh privacy: push stopped: a private name is in", file=out)
        for h in hits:
            print(f"  {h}", file=out)
        if hits and args.what == "push":
            print("Reword those places (amend, or a new commit), then push again. The name is not shown.", file=out)
        return 1 if hits else 0
    except KitError as exc:
        print(f"nh: {exc}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # a test's StringIO, or a stream that can't change
            pass
    args = parser().parse_args(argv)
    cwd = args.cwd.resolve()
    if args.cmd == "recall":
        return _recall(args, cwd)
    if args.cmd == "reply-check":
        return _reply_check(args)
    if args.cmd == "version":
        print(__version__)
        return 0
    if args.cmd == "privacy":
        return _privacy(args, cwd)
    from . import turns  # its Refused is an expected no (exit 1); update needs it anyway (upkeep loads it)
    try:
        from . import settings, upkeep
        where = settings.path(args.settings)
        s = settings.load(where)
        c = args.cmd
        if c not in ("setup", "update"):
            updated = upkeep.auto_update(where, s)
            if updated:
                print(f"nh: nearly-headless updated itself from {updated['from']} to {updated['to']}" if "to" in updated
                      else f"nh: could not update nearly-headless ({updated['error']}); trying again {updated['retry']}",
                      file=sys.stderr)
        if c == "serve":
            from . import dashboard
            if args.save:
                core.atomic_text(args.save, dashboard.render(dashboard.overview(s, fetch=True), saved=True))
                print(core.posix(args.save.resolve()))
                return 0
            if args.worker:
                dashboard.work_on(s, args.port, where)
                return 0
            dashboard.serve(s, args.port, where)
            return 0
        if c == "health":
            from . import health
            if args.days < 1:
                raise KitError("--days must be 1 or more")
            h = health.health(days=args.days)
            print(json.dumps(h, indent=2) if args.json else health.text(h))
            return 0
        if c == "test":
            from . import testmap
            code, tail = testmap.run(cwd, s, args.plan, modules=args.modules, full=args.full,
                                      local=args.local)
            if tail:
                print(tail)
            return code
        if c == "doctor":
            found = upkeep.doctor(s, where)
            print(json.dumps(found, indent=2) if args.json else
                  "\n".join(("FIX  " if not f["ok"] else "WARN " if f.get("warn") else "ok   ") + f["check"]
                            for f in found))
            return 0 if all(f["ok"] for f in found) else 1
        if c in ("setup", "update"):
            result = upkeep.setup(where, args.owner, args.repo, profile=args.profile) if c == "setup" else upkeep.update(where)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        if c == "check" and not (args.ack or args.fix_dates):
            found, text, changed = _check(args, s)
            data = {"changed": changed, "left": found} if args.fix else found
            print(json.dumps(data, indent=2, ensure_ascii=False) if args.json else text)
            from . import check
            if any(check.NOT_PUSHED in line for line in changed):
                print(f"nh: {check.NOT_PUSHED}. Fix the push, then push the repo's base branch.", file=sys.stderr)
                return 3
            return 1 if found else 0
        if c == "check":
            result = _check(args, s)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 3 if str(result.get("push", "")).startswith("push failed") else 0
        if c == "refresh":
            result = turns.refresh(cwd, s, args.pr, args.wait)
            print(json.dumps(result, indent=2) if args.json else f"{result['sha']}\n{result['refreshed']}")
            return 0
        result = (_turn(args, cwd, s) if c == "turn" else _gate(args, cwd, s) if c == "gate"
                  else _product(args, s) if c == "product" else _piece(args, s) if c == "piece"
                  else _question(args, s) if c == "question" else _order(args, s) if c == "order"
                  else _channels(args, s) if c == "channels"
                  else _inbox(args, s) if c == "inbox" else _status(s) if c == "status" else _hours(args, s, cwd))
        if isinstance(result, tuple):  # (data, text): text unless --json
            data, text = result
            print(json.dumps(data, indent=2, ensure_ascii=False) if args.json else text)
            if isinstance(data, dict) and str(data.get("push", "")).startswith("push failed"):
                print("nh: RECORDED BUT NOT ON GITHUB. Fix the push, then push the repo's base branch.", file=sys.stderr)
                return 3
            return 0
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if str(result.get("push", "")).startswith("push failed"):
            print("nh: RECORDED BUT NOT ON GITHUB. Fix the push, then push the repo's base branch.", file=sys.stderr)
            return 3
        return 0
    except turns.Refused as exc:
        print(f"nh: {exc}", file=sys.stderr)
        return 1
    except KitError as exc:
        print(f"nh: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:  # a file another program holds, a full disk, a missing folder: say so, no traceback
        print(f"nh: could not read or write a file: {exc}", file=sys.stderr)
        return 2
