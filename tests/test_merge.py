"""nh merge and its hook, against throwaway repositories: gh is faked (its merge makes a real merge commit on a bare
origin); git is real. No real repo or remote is touched."""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["NH_AUTO_UPDATE"] = "0"

from wk import cli, core, merge, turns, upkeep, work  # noqa: E402

BODY = "## What was wrong\nx\n## What changed\nx\n## How it was tested\nx\n## What it means for you\nx\n"
GREEN = [{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS", "workflowName": "tests"}]


def sh(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


def commit(where, name, text, push=None):
    (where / name).write_text(text, encoding="utf-8")
    sh(where, "add", "-A")
    sh(where, "commit", "-q", "-m", f"change {name}")
    if push:
        sh(where, "push", "-q", "-u", "origin", push)
    return sh(where, "rev-parse", "HEAD")


class Merge(unittest.TestCase):
    """An app repo with an origin, a builder worktree on feat-a with PR #7 open, and a fake gh."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.origin)], check=True, capture_output=True)
        self.app = self.tmp / "app"
        self.app.mkdir()
        sh(self.app, "init", "-q", "-b", "main")
        sh(self.app, "config", "user.name", "t")
        sh(self.app, "config", "user.email", "t@example.org")
        commit(self.app, "VERSION", "1.2.3\n")
        commit(self.app, "README.md", "app\n")
        sh(self.app, "remote", "add", "origin", str(self.origin))
        sh(self.app, "push", "-q", "-u", "origin", "main")
        self.settings = self.tmp / "settings.toml"
        self.settings.write_text(f'owner = "Pat Lee"\n\n[repos.app]\npath = "{core.posix(self.app)}"\n'
                                 'release = []\n', encoding="utf-8")
        self.wt = self.tmp / "wa"
        sh(self.app, "worktree", "add", "-q", "-b", "feat-a", str(self.wt))
        self.head = commit(self.wt, "code.py", "print('new')\n", push="feat-a")
        self.pr = {"state": "OPEN", "headRefOid": self.head, "headRefName": "feat-a", "baseRefName": "main",
                   "url": "https://github.com/acme/app/pull/7", "statusCheckRollup": GREEN, "comments": [],
                   "mergeCommit": None, "title": "Faster export", "body": BODY, "createdAt": "2026-01-01T00:00:00Z",
                   "reviews": []}
        self.calls, self.extra = [], None
        self.real = turns._gh
        turns._gh = self.gh

    def tearDown(self):
        turns._gh = self.real

    def gh(self, cwd, *args):
        self.calls.append(args)
        if args[:2] == ("pr", "view"):
            return json.dumps(self.pr)
        if args[:2] == ("pr", "comment"):
            self.pr["comments"].append({"body": args[args.index("--body") + 1]})
            return ""
        if args[:2] == ("pr", "merge"):
            assert "--squash" not in args and "--rebase" not in args
            assert args[args.index("--match-head-commit") + 1] == self.pr["headRefOid"]
            work_ = self.tmp / "github"
            subprocess.run(["git", "clone", "-q", str(self.origin), str(work_)], check=True, capture_output=True)
            sh(work_, "config", "user.name", "gh")
            sh(work_, "config", "user.email", "gh@example.org")
            sh(work_, "merge", "-q", "--no-ff", "-m", "Merge pull request #7", "origin/feat-a")
            if self.extra:  # GitHub's merge holding something else than the approved code
                (work_ / "code.py").write_text(self.extra, encoding="utf-8")
                sh(work_, "commit", "-q", "-a", "--amend", "--no-edit")
            sh(work_, "push", "-q", "origin", "main")
            self.pr |= {"state": "MERGED", "mergeCommit": {"oid": sh(work_, "rev-parse", "HEAD")}}
            return ""
        if args[0] == "api":
            return "[]"
        raise AssertionError(args)

    def nh(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--settings", str(self.settings), "--cwd", str(self.app), *argv])
        return code, out.getvalue() + err.getvalue()

    def merged(self):
        return any(a[:2] == ("pr", "merge") for a in self.calls)

    def test_dry_run_prints_the_steps_and_does_nothing(self):
        code, out = self.nh("merge", "--pr", "7", "--sha", self.head[:9], "--piece", "P1", "--release", "v1.2.3",
                            "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.calls, [])
        for step in ("approve: post 'Approved by the owner: merge @", "gate: nh gate fresh --pr 7", "ci:",
                     "version: the version file says 1.2.3", "merge: gh pr merge 7 --merge --match-head-commit",
                     "same:", "teardown:", "piece: set P1 to merged", "tag: git tag v1.2.3", "never: no production"):
            self.assertIn(step, out)

    def test_happy_path_merges_tears_down_sets_the_piece_and_tags(self):
        self.nh("product", "add", "app", "reports", "--line", "Monthly reports")
        self.nh("piece", "add", "app", "reports", "Faster export")
        self.nh("piece", "set", "app", "reports", "P1", "--pr", "7", "--branch", "feat-a")
        sh(self.wt, "merge", "-q", "origin/main")  # the list's own commits on main: taken, as a builder would
        self.head = sh(self.wt, "rev-parse", "HEAD")
        sh(self.wt, "push", "-q", "origin", "feat-a")
        self.pr["headRefOid"] = self.head
        code, out = self.nh("merge", "--pr", "7", "--sha", self.head[:9], "--piece", "P1", "--release", "v1.2.3")
        self.assertEqual(code, 0, out)
        steps = [x.split(":")[0].split()[-1] for x in out.splitlines() if x.startswith("ok")]
        self.assertEqual(steps, ["approve", "gate", "ci", "version", "merge", "same", "piece", "tag", "teardown"])
        self.assertIn("Not done: no production deploy", out)
        self.assertEqual(self.pr["comments"][0]["body"], f"Approved by the owner: merge @ {self.head[:9]}")
        oid = self.pr["mergeCommit"]["oid"]
        self.assertEqual(sh(self.origin, "rev-parse", "v1.2.3^{commit}"), oid)  # on the merge commit, not HEAD
        self.assertFalse(self.wt.exists())
        self.assertEqual(sh(self.app, "branch", "--list", "feat-a"), "")
        self.assertEqual(sh(self.app, "ls-remote", "--heads", "origin", "feat-a"), "")
        row = work.pieces(work.Repo(__import__("wk").settings.load(self.settings), "app"), "reports")[0]
        self.assertEqual((row["state"], row["approved"]), ("merged", self.head[:9]))
        # a second run finds the PR merged at the approved code and finishes again, changing nothing
        code, out = self.nh("merge", "--pr", "7", "--sha", self.head[:9], "--piece", "P1", "--release", "v1.2.3")
        self.assertEqual(code, 0, out)
        self.assertIn("is already merged", out)
        self.assertIn("(it was already there)", out)
        self.assertIn("feat-a was already gone here and on origin", out)
        self.assertEqual(len(self.pr["comments"]), 1)
        self.assertEqual(sum(a[:2] == ("pr", "merge") for a in self.calls), 1)

    def test_a_rerun_after_a_stop_after_the_merge_resumes_and_finishes(self):
        self.settings.write_text(self.settings.read_text(encoding="utf-8").replace(
            "release = []", 'release = ["command-center"]'), encoding="utf-8")

        def down(v):
            raise turns.Refused("the command center still shows 1.2.2")
        with mock.patch.object(merge, "command_center", down):
            code, out = self.nh("merge", "--pr", "7", "--sha", self.head, "--release", "v1.2.3")
        self.assertEqual(code, 1)
        self.assertIn("STOP  release:", out)
        self.assertTrue(self.wt.exists())  # teardown comes after the release
        with mock.patch.object(merge, "command_center", lambda v: f"shows {v}"):
            code, out = self.nh("merge", "--pr", "7", "--sha", self.head, "--release", "v1.2.3")
        self.assertEqual(code, 0, out)
        self.assertIn("ok    tag: v1.2.3", out)
        self.assertIn("ok    release: shows 1.2.3", out)
        self.assertFalse(self.wt.exists())
        self.assertEqual(sum(a[:2] == ("pr", "merge") for a in self.calls), 1)

    def test_a_pr_merged_at_other_code_than_approved_stops(self):
        self.pr |= {"state": "MERGED", "mergeCommit": {"oid": self.head}}
        code, out = self.nh("merge", "--pr", "7", "--sha", "abcdef1")
        self.assertEqual(code, 1)
        self.assertIn("STOP  approve:", out)
        self.assertIn("not at the approved abcdef1", out)

    def test_run_from_the_prs_own_worktree_leaves_its_local_part_and_succeeds(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--settings", str(self.settings), "--cwd", str(self.wt), "merge", "--pr", "7", "--sha",
                             self.head])
        said = out.getvalue() + err.getvalue()
        self.assertEqual(code, 0, said)
        self.assertIn("LEFT  teardown: nh merge ran inside", said)
        self.assertIn("git worktree remove", said)
        self.assertTrue(self.wt.exists())
        self.assertEqual(sh(self.app, "ls-remote", "--heads", "origin", "feat-a"), "")

    def test_a_branch_checked_out_in_the_main_checkout_is_left_with_the_command(self):
        sh(self.app, "worktree", "remove", str(self.wt))
        sh(self.app, "checkout", "-q", "feat-a")
        code, out = self.nh("merge", "--pr", "7", "--sha", self.head)
        self.assertEqual(code, 0, out)
        self.assertIn("LEFT  teardown: feat-a is checked out in the repo's main checkout", out)
        self.assertIn("git switch main", out)
        self.assertEqual(sh(self.app, "branch", "--show-current"), "feat-a")  # never switched

    def test_release_refuses_in_a_repo_whose_settings_name_no_release_steps(self):
        self.settings.write_text(self.settings.read_text(encoding="utf-8").replace("release = []\n", ""),
                                 encoding="utf-8")
        code, out = self.nh("merge", "--pr", "7", "--sha", self.head, "--release", "v1.2.3")
        self.assertNotEqual(code, 0)
        self.assertIn("A pushed tag can trigger a deploy", out)
        self.assertEqual(self.calls, [])

    def test_the_version_is_read_from_its_field_not_any_quoted_match(self):
        self.assertEqual(merge.version_in("wk/__init__.py", 'OLD = "1.2.3"\n__version__ = "1.2.4"\n'), "1.2.4")
        self.assertEqual(merge.version_in("package.json", '{"name": "1.2.3", "version": "2.0.0"}'), "2.0.0")
        self.assertEqual(merge.version_in("pyproject.toml", 'name = "1.2.3"\nversion = "3.1.0"\n'), "3.1.0")
        self.assertEqual(merge.version_in("VERSION", "1.2.3\n"), "1.2.3")

    def test_a_carried_over_yes_already_on_the_pr_is_not_posted_again(self):
        old = "abcdef1"
        self.pr["comments"] = [{"body": f"Approved by the owner: merge @ {old}"}, {"body": merge._carried(old, self.head)}]
        with mock.patch.object(turns, "gate_fresh", lambda *a: {"head": self.head[:12]}):
            code, out = self.nh("merge", "--pr", "7", "--sha", old)
        self.assertEqual(code, 0, out)
        self.assertFalse(any(a[:2] == ("pr", "comment") for a in self.calls))

    def test_gate_refusal_stops_before_the_merge(self):
        commit(self.app, "code.py", "print('main')\n")
        sh(self.app, "push", "-q", "origin", "main")  # main changed the PR's own file
        code, out = self.nh("merge", "--pr", "7", "--sha", self.head)
        self.assertEqual(code, 1)
        self.assertIn("ok    approve:", out)
        self.assertIn("STOP  gate:", out)
        self.assertIn("lacks the latest main", out)
        self.assertFalse(self.merged())

    def test_red_ci_stops_before_the_merge(self):
        self.pr["statusCheckRollup"] = [GREEN[0] | {"conclusion": "FAILURE"}]
        code, out = self.nh("merge", "--pr", "7", "--sha", self.head)
        self.assertEqual(code, 1)
        self.assertIn("STOP  ci: not green", out)
        self.assertFalse(self.merged())

    def test_a_head_that_moved_past_the_approved_sha_stops(self):
        approved = self.head
        self.pr["headRefOid"] = commit(self.wt, "code.py", "print('later')\n", push="feat-a")
        code, out = self.nh("merge", "--pr", "7", "--sha", approved)
        self.assertEqual(code, 1)
        self.assertIn("STOP  gate:", out)
        self.assertIn("does not carry over", out)
        self.assertFalse(self.merged())

    def test_a_merge_that_isnt_the_approved_code_stops_before_teardown(self):
        self.extra = "print('something else')\n"
        code, out = self.nh("merge", "--pr", "7", "--sha", self.head)
        self.assertEqual(code, 1)
        self.assertIn("STOP  same:", out)
        self.assertTrue(self.wt.exists())  # nothing torn down

    def test_a_version_file_that_disagrees_stops_before_the_merge(self):
        code, out = self.nh("merge", "--pr", "7", "--sha", self.head, "--release", "v1.2.4")
        self.assertEqual(code, 1)
        self.assertIn("STOP  version: VERSION at", out)
        self.assertFalse(self.merged())

    def test_a_branch_with_work_outside_main_is_kept(self):
        commit(self.wt, "more.py", "x\n")  # committed after the PR's head, never pushed
        code, out = self.nh("merge", "--pr", "7", "--sha", self.head)
        self.assertEqual(code, 1)
        self.assertIn("ok    merge:", out)
        self.assertIn("ok    same:", out)
        self.assertIn("STOP  teardown: feat-a has commits that aren't in origin/main", out)
        self.assertTrue(self.wt.exists())
        self.assertEqual(sh(self.app, "branch", "--list", "feat-a").strip(" *+"), "feat-a")

    def test_release_steps_run_after_the_tag(self):
        self.settings.write_text(self.settings.read_text(encoding="utf-8").replace(
            "release = []", 'release = ["command-center"]'), encoding="utf-8")
        with mock.patch.object(merge, "command_center", lambda v: f"shows {v}"):
            code, out = self.nh("merge", "--pr", "7", "--sha", self.head, "--release", "v1.2.3")
        self.assertEqual(code, 0, out)
        self.assertIn("ok    release: shows 1.2.3", out)


class Guard(unittest.TestCase):
    """The PreToolUse hook: a raw gh pr merge is refused only in a supervisor chat."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.settings = self.tmp / "settings.toml"
        self.settings.write_text("", encoding="utf-8")
        self.sup, self.other = self.tmp / "sup.jsonl", self.tmp / "other.jsonl"
        skill = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Skill",
                                                               "input": {"skill": "supervisor"}}]}}
        self.sup.write_text(json.dumps(skill) + "\n", encoding="utf-8")
        self.other.write_text(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n", encoding="utf-8")

    def run_hook(self, command, transcript, **more):
        hook = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command},
                "transcript_path": str(transcript), "session_id": "s1"} | more
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(hook))), redirect_stdout(out):
            code = cli.main(["--settings", str(self.settings), "merge-guard"])
        self.assertEqual(code, 0)
        return out.getvalue()

    def test_a_supervisors_raw_merge_is_refused(self):
        said = json.loads(self.run_hook("cd x && gh pr merge 7 --merge", self.sup))
        decision = said["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("nh merge --pr N --sha S", decision["permissionDecisionReason"])
        self.assertIn("deny\tcd x && gh pr merge 7 --merge", (self.tmp / "merge-guard.log").read_text(encoding="utf-8"))

    def test_a_raw_merge_with_gh_flags_or_in_powershell_is_refused_but_its_words_in_text_are_not(self):
        for command in ("gh -R acme/app pr merge 14 --merge", "gh --repo acme/app pr merge 14",
                        "cd app; if ($?) { gh pr merge 14 }", "FOO=1 gh pr merge 14"):
            self.assertIn('"deny"', self.run_hook(command, self.sup), command)
        self.assertIn('"deny"', self.run_hook("gh pr merge 14", self.sup, tool_name="PowerShell"))
        for command in ('grep -n "gh pr merge" skills/supervisor/merge.md', 'git commit -m "use gh pr merge less"',
                        "echo 'gh pr merge 7'", "gh pr merge --help"):
            self.assertEqual(self.run_hook(command, self.sup), "", command)

    def test_everything_else_is_let_through(self):
        self.assertEqual(self.run_hook("gh pr merge 7 --merge", self.other), "")  # not a supervisor
        self.assertEqual(self.run_hook("gh pr merge 7", self.sup, agent_id="a1"), "")  # a builder
        self.assertEqual(self.run_hook("nh merge --pr 7 --sha abcdef1", self.sup), "")  # nh merge itself
        self.assertEqual(self.run_hook("gh pr view 7", self.sup), "")
        self.assertEqual(self.run_hook("NH_RAW_MERGE=1 gh pr merge 7 --merge", self.sup), "")  # the escape hatch
        self.assertIn("escaped\tNH_RAW_MERGE=1", (self.tmp / "merge-guard.log").read_text(encoding="utf-8"))

    def test_setup_installs_it_beside_the_other_hooks(self):
        self.assertIn(upkeep.MERGE_GUARD, upkeep.HOOKS)
        self.assertEqual(upkeep.MERGE_GUARD[:2], ("PreToolUse", "Bash|PowerShell"))


if __name__ == "__main__":
    unittest.main()
