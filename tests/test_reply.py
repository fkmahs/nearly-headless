"""`nh reply-check`, the Stop hook that holds a supervisor's reply to the format, and its install. Temp home only."""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["NH_AUTO_UPDATE"] = "0"

from wk import cli, core, upkeep  # noqa: E402

ON = '[reply]\nrequire = ["Since you wrote:", "What would Pat do"]\n'
GOOD = "**Since you wrote:** Done: P1.\n\nAll well.\n\n**What would Pat do**\nSay yes."


def line(kind, content, **extra):
    """One transcript entry, shaped as Claude Code writes them (trimmed to what the check reads)."""
    return json.dumps({"type": kind, "isSidechain": False, "message": {"role": kind, "content": content}} | extra)


def skill_call(name):
    return line("assistant", [{"type": "tool_use", "id": "t1", "name": "Skill", "input": {"skill": name}}])


def skill_text(name):  # what Claude Code adds after a skill loads (also after the owner types /name)
    return line("user", [{"type": "text", "text": f"Base directory for this skill: C:\\Users\\me\\.claude\\skills\\"
                                                  f"{name}\n\n# {name.title()}\n"}], isMeta=True)


class ReplyCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.settings = self.tmp / "nh" / "settings.toml"
        self.settings.parent.mkdir()
        self.settings.write_text(ON, encoding="utf-8")
        self.log = self.settings.parent / "reply-check.log"

    def transcript(self, *entries, reply=GOOD):
        path = self.tmp / "chat.jsonl"
        body = [line("user", "Catch me up"), *entries, line("user", [{"type": "text", "text": "what next?"}]),
                line("assistant", [{"type": "text", "text": reply}])]
        path.write_text("\n".join(body) + "\n", encoding="utf-8")
        return path

    def run_hook(self, hook, raw=None):
        out = io.StringIO()
        stdin = io.StringIO(raw if raw is not None else json.dumps(hook))
        with mock.patch.object(sys, "stdin", stdin), redirect_stdout(out):
            code = cli.main(["--settings", str(self.settings), "reply-check"])
        self.assertEqual(code, 0)  # a Stop hook never fails
        return json.loads(out.getvalue()) if out.getvalue().strip() else None

    def hook(self, path, **extra):
        return {"session_id": "s1", "transcript_path": str(path), "hook_event_name": "Stop",
                "stop_hook_active": False} | extra

    def logged(self):
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.is_file() else []

    def test_a_supervisor_reply_missing_a_part_is_sent_back_naming_it(self):
        path = self.transcript(skill_call("supervisor"), reply="**Since you wrote:** Done: P1. All well.")
        got = self.run_hook(self.hook(path))
        self.assertEqual(got["decision"], "block")
        self.assertIn('"What would Pat do"', got["reason"])
        self.assertNotIn("Since you wrote", got["reason"])
        self.assertEqual(len(self.logged()), 1)
        self.assertTrue(self.logged()[0].endswith("\ts1\tmissing: What would Pat do"))
        self.assertNotIn("All well", self.log.read_text(encoding="utf-8"))  # no message text in the log

    def test_a_supervisor_reply_with_every_part_is_let_through(self):
        for loaded in (skill_call("supervisor"), skill_call("nearly-headless:supervisor"), skill_text("supervisor")):
            self.assertIsNone(self.run_hook(self.hook(self.transcript(loaded))))
        self.assertEqual([x.split("\t")[2] for x in self.logged()], ["ok"] * 3)

    def test_the_hooks_own_last_message_is_preferred_to_the_transcript(self):
        path = self.transcript(skill_call("supervisor"), reply="stale")
        self.assertIsNone(self.run_hook(self.hook(path, last_assistant_message=GOOD)))
        self.assertEqual(self.run_hook(self.hook(path))["decision"], "block")

    def test_chats_that_are_not_supervisors_are_never_checked(self):
        for other in (skill_call("builder"), skill_text("catch-up"), skill_call("supervisor-notes")):
            self.assertIsNone(self.run_hook(self.hook(self.transcript(other, reply="hi"))))
        sidechain = line("assistant", [{"type": "tool_use", "name": "Skill", "input": {"skill": "supervisor"}}],
                         isSidechain=True)
        self.assertIsNone(self.run_hook(self.hook(self.transcript(sidechain, reply="hi"))))
        path = self.transcript(skill_call("supervisor"), reply="hi")
        self.assertIsNone(self.run_hook(self.hook(path, agent_id="a1")))  # a builder or other subagent
        self.assertIsNone(self.run_hook(self.hook(path, hook_event_name="SubagentStop")))
        self.assertEqual(self.logged(), [])

    def test_one_nudge_per_turn_never_a_loop(self):
        path = self.transcript(skill_call("supervisor"), reply="still nothing")
        self.assertIsNone(self.run_hook(self.hook(path, stop_hook_active=True)))
        self.assertIn("after nudge: missing: Since you wrote:; What would Pat do", self.logged()[0])

    def test_off_when_nothing_is_configured_and_recording_is_turned_off(self):
        path = self.transcript(skill_call("supervisor"), reply="hi")
        for text in ("[reply]\nrecord = false\n", 'owner = "Pat"\n[reply]\nrecord = false\n',
                     "[reply]\nrequire = []\nrecord = false\n", '[reply]\nrequire = "x"\nrecord = false\n'):
            self.settings.write_text(text, encoding="utf-8")
            self.assertIsNone(self.run_hook(self.hook(path)))
        self.assertEqual(self.logged(), [])
        self.settings.unlink()  # no settings: only the record check, on by default, and this turn needs none
        self.assertIsNone(self.run_hook(self.hook(path)))
        self.assertEqual([x.split("\t")[2] for x in self.logged()], ["ok"])

    def remind(self, hook=None):
        out = io.StringIO()
        stdin = io.StringIO(json.dumps(hook or {"hook_event_name": "UserPromptSubmit"}))
        with mock.patch.object(sys, "stdin", stdin), redirect_stdout(out):
            self.assertEqual(cli.main(["--settings", str(self.settings), "reply-check", "--remind"]), 0)
        return out.getvalue()

    def test_remind_names_the_required_parts_in_one_short_line_or_says_nothing(self):
        got = self.remind()
        self.assertEqual(got.count("\n"), 1)
        self.assertLess(len(got), 200)
        self.assertIn('"Since you wrote:", "What would Pat do"', got)
        self.assertEqual(self.remind({"agent_id": "a1"}), "")  # never in a builder
        for text in ("", "[reply]\nrequire = []\n", "[reply\n"):
            self.settings.write_text(text, encoding="utf-8")
            self.assertEqual(self.remind(), "")
        self.assertEqual(self.logged(), [])  # the reminder logs nothing

    def test_any_error_lets_the_reply_through(self):
        path = self.transcript(skill_call("supervisor"), reply="hi")
        self.assertIsNone(self.run_hook({}, raw="{not json"))
        self.assertIsNone(self.run_hook({}, raw="[1, 2]"))
        self.assertIsNone(self.run_hook(self.hook(self.tmp / "missing.jsonl")))
        self.assertIsNone(self.run_hook(self.hook(self.tmp)))  # a folder, not a transcript
        self.settings.write_text("[reply\n", encoding="utf-8")  # settings that can't be read
        self.assertIsNone(self.run_hook(self.hook(path)))
        self.settings.write_text(ON, encoding="utf-8")
        path.write_bytes(b"\xff\xfe not text")
        self.assertIsNone(self.run_hook(self.hook(path)))


def tool(name, id="t9", **given):
    return line("assistant", [{"type": "tool_use", "id": id, "name": name, "input": given}])


def result(id, error=False):
    return line("user", [{"type": "tool_result", "tool_use_id": id, "is_error": error, "content": "x"}])


def said(text):
    return line("assistant", [{"type": "text", "text": text}])


DRAFT = "Here's the draft reply for Sam:\n\nHi Sam,\n\nThe fix goes out tonight.\n\nThanks"


class SupportRecord(unittest.TestCase):
    """On by default: a supervisor turn that did work for the owner and recorded none of it is sent back."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.settings = self.tmp / "nh" / "settings.toml"
        self.settings.parent.mkdir()
        self.settings.write_text("", encoding="utf-8")
        self.log = self.settings.parent / "reply-check.log"

    def turn(self, ask, *entries, attach=None):
        """A supervisor chat whose last turn is the owner's ask (with an attachment block, if given), then these."""
        path = self.tmp / "chat.jsonl"
        content = ([{"type": attach, "source": {}}] if attach else []) + [{"type": "text", "text": ask}]
        body = [skill_call("supervisor"), said("Caught up."), line("user", content), *entries]
        path.write_text("\n".join(body) + "\n", encoding="utf-8")
        return path

    def run_hook(self, path, **extra):
        out = io.StringIO()
        hook = {"session_id": "s1", "transcript_path": str(path), "hook_event_name": "Stop",
                "stop_hook_active": False} | extra
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(hook))), redirect_stdout(out):
            self.assertEqual(cli.main(["--settings", str(self.settings), "reply-check"]), 0)
        return json.loads(out.getvalue()) if out.getvalue().strip() else None

    def logged(self):
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.is_file() else []

    def test_a_drafted_reply_with_no_piece_write_is_sent_back(self):
        got = self.run_hook(self.turn("Read Sam's email and draft a reply for me", said(DRAFT)))
        self.assertEqual(got["decision"], "block")
        self.assertIn("missing a piece record", got["reason"])  # nh health counts feedback naming what's missing
        self.assertIn("nh piece add", got["reason"])
        self.assertIn("already billed", got["reason"])
        self.assertIn("`--billed-ok` is only for a release or teardown note", got["reason"])
        self.assertTrue(self.logged()[0].endswith("\ts1\tmissing: a piece record (a drafted message)"))
        self.assertNotIn("Sam", self.log.read_text(encoding="utf-8"))  # no message text in the log

    def test_the_same_turn_with_a_piece_add_or_a_note_line_passes(self):
        for write in ('nh piece add acme support "Reply to Sam about the fix"',
                      '& $nh piece note acme support P7 --add "Drafted the reply to Sam"',
                      'W=C:/nearly-headless/bin/wk.cmd; $W piece note acme support P7 --file note.md'):
            path = self.turn("Read Sam's email and draft a reply for me", tool("Bash", command=write), said(DRAFT))
            self.assertIsNone(self.run_hook(path), write)
        reading = self.turn("Draft a reply to Sam", tool("Bash", command="nh piece note acme support P7"), said(DRAFT))
        self.assertEqual(self.run_hook(reading)["decision"], "block")  # reading a note records nothing
        self.assertEqual([x.split("\t")[2] for x in self.logged()],
                         ["ok"] * 3 + ["missing: a piece record (a drafted message)"])

    def test_investigations_and_document_reviews_are_support_too(self):
        cases = [("an investigation", [tool("WebSearch", query="payment holds in a locked period")], None),
                 ("an investigation", [tool("mcp__claude-in-chrome__navigate", url="https://mail.google.com/x")], None),
                 ("an investigation", [tool("Agent", prompt="Research how Sage posts journals")], None),
                 ("a document review", [tool("Read", file_path="C:/Users/me/Downloads/guide v3.pdf")], None),
                 ("a document review", [], "document"),
                 ("a drafted message", [tool("mcp__gmail__create_draft", to="sam@example.com")], None)]
        for kind, calls, attach in cases:
            path = self.turn("Can you look into this?", *calls, said("Done: here is what I found."), attach=attach)
            got = self.run_hook(path)
            self.assertEqual(got["decision"], "block", kind)
            self.assertIn(f"({kind})", got["reason"])

    def test_list_questions_and_duty_work_pass(self):
        turns = [
            ("What's next on the list? Reply in a table", [tool("Bash", command="nh piece list acme support")],
             "Next is P3; its draft PR is ready."),
            ("How does nh refresh work?", [tool("Read", file_path="C:/Projects/nearly-headless/README.md")],
             "It merges main into the PR's branch."),
            ("Yes, merge and release", [tool("Bash", command="gh pr merge 12 --squash"),
                                        tool("Bash", command="nh piece set acme support P3 --state merged"),
                                        tool("mcp__Claude_Browser__navigate", url="http://localhost:8765/"),
                                        tool("mcp__Claude_Browser__computer", action="screenshot")],
             "Merged and released; the command center shows 1.48.0."),
            ("Start P4", [tool("Agent", prompt="Use the builder skill. Brief: ...", isolation="worktree"),
                          tool("Agent", prompt="Tidy the piece notes for the support list"),
                          tool("SendMessage", to="a1", message="Continue your PR")],
             "Builder started on P4; it opens a draft PR first."),
        ]
        for ask, calls, text in turns:
            self.assertIsNone(self.run_hook(self.turn(ask, *calls, said(text))), ask)
        self.assertEqual(len(self.logged()), 4)
        self.assertTrue(all(x.endswith("\tok") for x in self.logged()))

    def test_the_turn_runs_from_the_owners_last_message_across_notifications_and_compaction(self):
        later = [tool("Bash", command='nh piece add acme support "Reply to Sam"'),
                 line("user", [{"type": "text", "text": "<task-notification> agent done </task-notification>"}]),
                 line("user", [{"type": "text", "text": "This session is being continued from a previous "
                                                        "conversation."}], isCompactSummary=True),
                 said(DRAFT)]
        self.assertIsNone(self.run_hook(self.turn("Draft a reply to Sam", *later)))
        path = self.turn("Draft a reply to Sam", tool("Bash", command='nh piece add acme support "Reply"'), said(DRAFT))
        with path.open("a", encoding="utf-8") as f:
            f.write(line("user", "Now look into Pat's question") + "\n" + tool("WebSearch", query="x") + "\n")
        self.assertEqual(self.run_hook(path)["decision"], "block")  # a new ask needs its own record

    STOPPED = line("system", None, subtype="stop_hook_summary")  # what Claude Code writes when a turn stops
    PEER = line("user", [{"type": "text", "text": "1.48.0 is released. Please reread your supervisor skill."}],
                isMeta=True, origin={"kind": "peer", "from": "local_x", "name": "Supervisor"})

    def test_a_turn_another_chat_started_is_not_support_and_is_a_turn_of_its_own(self):
        owners = [tool("WebSearch", query="payment holds"), said("Here is what I found."), self.STOPPED]
        rereads = [tool("Skill", skill="supervisor"), tool("Read", file_path="C:/Users/me/.claude/skills/x/SKILL.md"),
                   said("Reread my skill.")]
        others = (self.PEER,
                  line("user", '<cross-session-message from="local_x">Reread your skill</cross-session-message>'),
                  line("user", "<task-notification> agent done </task-notification>",
                       origin={"kind": "task-notification"}),
                  line("user", [{"type": "text", "text": "<task-notification> done </task-notification>"}]))
        for other in others:  # the owner's earlier turn isn't counted again, nor is this one
            self.assertIsNone(self.run_hook(self.turn("Look into the payment holds", *owners, other, *rereads)), other)
            searched = self.turn("Look into it", self.STOPPED, other, tool("WebSearch", query="x"), said("Found it."))
            self.assertIsNone(self.run_hook(searched), other)  # whatever it does: the owner didn't ask for it
        midturn = self.turn("Look into the payment holds", tool("WebSearch", query="x"), self.PEER, said("Found it."))
        self.assertEqual(self.run_hook(midturn)["decision"], "block")  # arriving mid-turn, it is still his turn

    def test_rereading_skills_and_instructions_is_duty(self):
        rereads = [tool("Skill", skill="supervisor"), tool("Skill", skill="wwid"),
                   tool("Read", file_path="C:/Users/me/.claude/skills/supervisor/SKILL.md"),
                   tool("Read", file_path="C:/Projects/acme/AGENTS.md"),
                   tool("Read", file_path="C:/Projects/acme/CLAUDE.md"),
                   tool("Bash", command="cat C:/Users/me/.claude/skills/supervisor/SKILL.md | grep -n support")]
        self.assertIsNone(self.run_hook(self.turn("Reread your skill", *rereads, said("Reread it."))))

    def test_the_owners_own_search_is_still_sent_back_after_a_turn_another_started(self):
        path = self.turn("Reread your skill", said("Done."), self.STOPPED, self.PEER, said("Reread it."),
                         self.STOPPED, line("user", "Now look into Pat's question"), tool("WebSearch", query="x"),
                         said("Here is what I found."))
        got = self.run_hook(path)
        self.assertEqual(got["decision"], "block")
        self.assertIn("(an investigation)", got["reason"])
        revised = self.turn("Draft a reply to Sam", tool("Bash", command='nh piece add acme support "Reply"'),
                            said(DRAFT), self.STOPPED, self.PEER, said("Reread it."), self.STOPPED,
                            line("user", "Shorter reply please"), said(DRAFT))
        self.assertIsNone(self.run_hook(revised))  # the record from his last turn still covers a revision

    def test_revising_a_draft_passes_on_the_record_made_since_the_previous_ask(self):
        path = self.turn("Draft a reply to Sam", tool("Bash", command='nh piece note acme support P7 --add "Draft"'),
                         said(DRAFT), line("user", "Shorter reply, and drop the second line"), said(DRAFT))
        self.assertIsNone(self.run_hook(path))
        path = self.turn("Draft a reply to Sam", said(DRAFT), line("user", "Shorter reply please"), said(DRAFT))
        self.assertEqual(self.run_hook(path)["decision"], "block")  # nothing recorded in either turn

    def test_only_a_write_that_ran_counts(self):
        for spoof in ('grep -rn "piece add" wk/', "nh piece add --help", "rg 'nh piece note P7 --add' .",
                      'echo done # see nh piece add', "nh piece note acme support P7 -h --add",
                      "python bin/wk --help piece add", "grep -n 'py bin/wk piece add' README.md"):
            path = self.turn("Draft a reply to Sam", tool("Bash", command=spoof), said(DRAFT))
            self.assertEqual(self.run_hook(path)["decision"], "block", spoof)
        refused = self.turn("Draft a reply to Sam", tool("Bash", id="w1", command="nh piece note a s P7 --add x"),
                            result("w1", error=True), said(DRAFT))
        self.assertEqual(self.run_hook(refused)["decision"], "block")
        for ran in ("cd C:/repo && nh piece add a s x", "wk piece add a s x", '"C:/kit/bin/nh" piece add a s x', "py -m wk piece add a s x", '"C:/bin/wk.cmd" piece add a s x',
                    "$W = 'wk'; & $W piece note a s P7 --add x", "python bin/wk piece add a s x",
                    "py bin/wk piece note a s P7 --add x", "py -I C:/kit/bin/wk piece add a s x",
                    "nh --json piece add a s x", "bin/wk -C x piece add a s x",
                    "wk --settings=C:/s.toml --cwd C:/repo piece note a s P7 --add x --billed-ok"):
            path = self.turn("Draft a reply to Sam", tool("Bash", id="w2", command=ran), result("w2"), said(DRAFT))
            self.assertIsNone(self.run_hook(path), ran)

    def test_a_subagents_write_counts(self):
        write = tool("Bash", command='nh piece add acme support "Reply to Sam"')
        sidechain = json.loads(write) | {"isSidechain": True}
        path = self.turn("Draft a reply to Sam", json.dumps(sidechain), said(DRAFT))
        self.assertIsNone(self.run_hook(path))
        path = self.turn("Draft a reply to Sam", line("assistant", [{"type": "text", "text": DRAFT}],
                                                      timestamp="2020-01-01T10:00:05.000Z"))
        lines = path.read_text(encoding="utf-8").splitlines()
        lines[2] = json.dumps(json.loads(lines[2]) | {"timestamp": "2020-01-01T10:00:00.000Z"})
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        side = path.parent / path.stem / "subagents"
        side.mkdir(parents=True)
        (side / "agent-a1.jsonl").write_text(json.dumps(json.loads(write) | {"timestamp": "2020-01-01T10:00:02.000Z"})
                                             + "\n", encoding="utf-8")
        self.assertIsNone(self.run_hook(path))  # recorded in the subagent's own file
        (side / "agent-a1.jsonl").write_text(json.dumps(json.loads(write) | {"timestamp": "2020-01-01T09:00:00.000Z"})
                                             + "\n", encoding="utf-8")
        self.assertEqual(self.run_hook(path)["decision"], "block")  # a write from before this turn doesn't

    def test_duty_browsing_and_agents_and_named_doc_connectors(self):
        duty = [tool("mcp__Claude_Browser__computer", action="screenshot"),
                tool("mcp__claude-in-chrome__navigate", url="file:///C:/Users/me/report.html"),
                tool("mcp__claude-in-chrome__navigate", url="https://staging.example.com/release"),
                tool("mcp__Claude_Browser__navigate", url="http://app.localhost:3000/"),
                tool("Agent", prompt="Review PR #66 for spoofing and false alarms"),
                tool("Agent", prompt="Find where the doctor line lives", subagent_type="Explore")]
        for call in duty:
            self.assertIsNone(self.run_hook(self.turn("Release 1.48.0 and check it", call, said("Released."))), call)
        docs = tool("mcp__1a59c906-04da-521d-bda7-7f71b9f9e01c__create", title="Meeting notes")
        got = self.run_hook(self.turn("Write up the meeting notes", docs, said("Done.")))
        self.assertIn("(a document review)", got["reason"])

    def test_the_setting_switches_it(self):
        path = self.turn("Draft a reply to Sam", said(DRAFT))
        self.settings.write_text("[reply]\nrecord = false\n", encoding="utf-8")
        self.assertIsNone(self.run_hook(path))
        self.assertEqual(self.logged(), [])
        for on in ("", '[reply]\nrecord = "yes"\n', "[reply]\nrecord = true\n"):
            self.settings.write_text(on, encoding="utf-8")
            self.assertEqual(self.run_hook(path)["decision"], "block", on)
        self.log.unlink()
        self.settings.write_text('[reply]\nrequire = ["What would Pat do"]\n', encoding="utf-8")
        got = self.run_hook(path)
        self.assertIn('"What would Pat do"', got["reason"])
        self.assertIn("missing a piece record", got["reason"])
        self.assertIn("missing: What would Pat do; a piece record (a drafted message)", self.logged()[0])
        self.assertIsNone(self.run_hook(path, stop_hook_active=True))  # one nudge per turn, still logged
        self.assertIn("after nudge: missing:", self.logged()[1])
        self.assertIsNone(self.run_hook(path, agent_id="a1"))  # never a builder

class Install(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.claude = self.tmp / "claude"
        old = upkeep.claude_dir
        upkeep.claude_dir = lambda: self.claude
        self.addCleanup(setattr, upkeep, "claude_dir", old)

    def test_installed_once_beside_everything_else(self):
        path = self.claude / "settings.json"
        path.parent.mkdir(parents=True)
        mine = {"type": "command", "command": "echo hello"}
        old = {"type": "command", "command": '"D:/old place/nearly-headless/bin/wk" reply-check', "timeout": 30}
        path.write_text(json.dumps({"theme": "dark", "hooks": {"Stop": [{"hooks": [mine]}, {"hooks": [old]}],
                                                               "SessionStart": [{"matcher": "startup",
                                                                                 "hooks": [mine]}]}}),
                        encoding="utf-8")
        self.assertFalse(upkeep.hook_check()[1]["ok"])
        self.assertTrue(upkeep.install_hook(upkeep.REPLY)["changed"])
        after = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(after["theme"], "dark")
        self.assertEqual(after["hooks"]["SessionStart"], [{"matcher": "startup", "hooks": [mine]}])
        self.assertEqual(after["hooks"]["Stop"], [  # the owner's kept, our old copy replaced, no matcher
            {"hooks": [mine]}, {"hooks": [{"type": "command", "command": upkeep.hook_command(upkeep.REPLY),
                                           "timeout": 30}]}])
        self.assertTrue(upkeep.hook_command(upkeep.REPLY).endswith('/bin/nh" reply-check'))
        self.assertTrue(upkeep.hook_check()[1]["ok"])
        text = path.read_text(encoding="utf-8")
        self.assertEqual(upkeep.install_hook(upkeep.REPLY),
                         {"installed": True, "changed": False, "file": core.posix(path)})
        upkeep.install_hook()  # the recall hooks go in beside it, and none replaces another
        upkeep.install_hook(upkeep.RECALL_CLEAR)
        upkeep.install_hook(upkeep.RECALL_START)
        upkeep.install_hook(upkeep.RECALL_RESUME)
        upkeep.install_hook(upkeep.REPLY)
        upkeep.install_hook(upkeep.REMIND)
        after = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(after["hooks"]["Stop"]), 2)
        self.assertEqual(after["hooks"]["UserPromptSubmit"], [{"hooks": [upkeep._hook(upkeep.REMIND)]}])
        self.assertEqual(len(after["hooks"]["SessionStart"]), 5)
        self.assertTrue(all(c["ok"] for c in upkeep.hook_check()))
        self.assertNotEqual(text, path.read_text(encoding="utf-8"))

    def test_an_unreadable_settings_file_is_never_rewritten(self):
        path = self.claude / "settings.json"
        path.parent.mkdir(parents=True)
        for broken in ('{"hooks": ', '{"hooks": {"Stop": {}}}', "[]"):
            path.write_text(broken, encoding="utf-8")
            self.assertFalse(upkeep.install_hook(upkeep.REPLY)["installed"])
            self.assertEqual(path.read_text(encoding="utf-8"), broken)
            self.assertFalse(upkeep.hook_check()[1]["ok"])


if __name__ == "__main__":
    unittest.main()
