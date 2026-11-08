"""Turns and release gates against throwaway repositories. Time and gh are stubbed; no real repo or remote is touched."""

import io
import os
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["NH_AUTO_UPDATE"] = "0"  # tests never reach GitHub; the daily check has its own test

from wk import cli, core, turns  # noqa: E402


def sh(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def make_repo(path: Path, files: dict) -> None:
    path.mkdir(parents=True)
    sh(path, "init", "-b", "main")
    sh(path, "config", "user.name", "t")
    sh(path, "config", "user.email", "t@example.org")
    for name, text in files.items():
        (path / name).write_text(text, encoding="utf-8")
    sh(path, "add", "-A")
    sh(path, "commit", "-m", "init")


def ready(sha, gaps="none", at="2026-01-01"):
    return {"body": f"Ready @ {sha}\n\nRan tests.test_x here; CI green.\n\nGap checks: {gaps}", "createdAt": at}


class Turns(unittest.TestCase):
    """An app repo with an origin and two worktrees, and this laptop's settings naming its owner."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.origin)], check=True, capture_output=True)
        self.app = self.tmp / "app"
        make_repo(self.app, {"README.md": "app\n"})
        sh(self.app, "remote", "add", "origin", str(self.origin))
        sh(self.app, "push", "-u", "origin", "main")
        self.settings = self.tmp / "settings.toml"
        self.settings.write_text(
            f'owner = "Pat Lee"\n\n[repos.app]\npath = "{core.posix(self.app)}"\nbase = "main"\n', encoding="utf-8")
        self.a, self.b = self.tmp / "wa", self.tmp / "wb"
        sh(self.app, "worktree", "add", "-q", "-b", "feat-a", str(self.a))
        sh(self.app, "worktree", "add", "-q", "-b", "feat-b", str(self.b))
        self.now = 1_800_000_000.0
        self._real = turns._now, turns._gh
        turns._now = lambda: self.now

    def tearDown(self):
        turns._now, turns._gh = self._real

    def wk(self, where, *argv, settings=None):
        out, err = io.StringIO(), io.StringIO()
        flags = ["--settings", str(settings or self.settings)]
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main([*flags, "--cwd", str(where), *argv])
        return code, out.getvalue() + err.getvalue()

    def test_refresh_merges_main_in_the_builder_worktree_or_changes_nothing(self):
        def commit(where, name, text, push=None):
            (where / name).write_text(text, encoding="utf-8")
            sh(where, "add", "-A")
            sh(where, "commit", "-q", "-m", f"change {name}")
            if push:
                sh(where, "push", "-q", "origin", push)
            return core.git(where, "rev-parse", "HEAD")

        def remote(branch):
            return core.git(self.origin, "rev-parse", branch)

        green = [{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS", "workflowName": "tests"}]
        ci, comments = {"rollup": green}, []
        pr = {"state": "OPEN", "headRefName": "feat-a", "baseRefName": "main"}

        def gh(cwd, *args):
            if args[:2] == ("pr", "comment"):
                comments.append(args[-1])
                return ""
            if "statusCheckRollup" in args[-1]:  # CI on what GitHub has for the branch now
                rollup = ci["polls"].pop(0) if ci.get("polls") else ci["rollup"]
                return json.dumps({"headRefOid": remote("feat-a"), "statusCheckRollup": rollup})
            return json.dumps(dict(pr, comments=[ready(pr["headRefOid"])]))  # the builder's clean Ready on its head
        turns._gh, real_sleep = gh, turns._sleep
        self.addCleanup(setattr, turns, "_sleep", real_sleep)
        turns._sleep = lambda secs: setattr(self, "now", self.now + secs)
        refresh = lambda: self.wk(self.app, "refresh", "--pr", "7")  # noqa: E731
        main_before = core.git(self.app, "rev-parse", "HEAD")

        pr["headRefOid"] = commit(self.a, "a.txt", "the PR's edit\n", push="feat-a")
        code, out = refresh()
        self.assertEqual((code, comments), (0, []))  # main hasn't moved: nothing to do
        self.assertIn("nothing to do", out)

        commit(self.app, "README.md", "main's edit\n", push="main")
        main_before = core.git(self.app, "rev-parse", "HEAD")
        code, out = refresh()
        self.assertEqual(code, 0, out)
        new = remote("feat-a")
        self.assertEqual(out.splitlines()[0], new)  # prints the new sha, pushed
        self.assertEqual(core.git(self.a, "rev-parse", "HEAD"), new)
        self.assertEqual(len(core.git(self.a, "rev-list", "--parents", "-1", new).split()), 3)  # a merge commit
        self.assertTrue(comments[0].startswith(f"Ready @ {new}"))
        self.assertEqual(turns.ready_gaps([{"body": comments[0]}]), "")  # its own Ready lets the next refresh run
        self.assertEqual(core.git(self.app, "rev-parse", "HEAD"), main_before)  # the main checkout untouched
        self.assertEqual(core.git(self.app, "symbolic-ref", "--short", "HEAD"), "main")

        pr["headRefOid"] = new
        commit(self.app, "a.txt", "main's own a.txt\n", push="main")  # conflicts with the PR's a.txt
        code, out = refresh()
        self.assertEqual(code, 1)
        self.assertIn("conflicts in a.txt", out)
        self.assertIn("the builder is needed", out)
        self.assertEqual((core.git(self.a, "rev-parse", "HEAD"), remote("feat-a")), (new, new))  # nothing changed
        self.assertEqual(core.git(self.a, "status", "--porcelain"), "")
        self.assertEqual(len(comments), 1)

        sh(self.a, "reset", "-q", "--hard", "origin/main")  # the builder starts over on main's a.txt
        pr["headRefOid"] = commit(self.a, "b.txt", "more\n")
        sh(self.a, "push", "-q", "--force", "origin", "feat-a")
        commit(self.app, "c.txt", "main again\n", push="main")
        ci["rollup"] = [dict(green[0], status="IN_PROGRESS", conclusion="")]
        code, out = refresh()
        self.assertEqual(code, 1)  # pushed, but no Ready without green CI
        self.assertIn("CI did not finish within 30 minutes", out)
        self.assertEqual(len(comments), 1)

        pr["headRefOid"] = remote("feat-a")
        commit(self.app, "d.txt", "main again\n", push="main")
        lint = dict(green[0], workflowName="lint", conclusion="FAILURE")
        ci["polls"], ci["rollup"] = [green, green + [lint]], green + [lint]  # a second workflow queues late
        code, out = refresh()
        self.assertEqual(code, 1)  # one green poll alone is not enough: the late failure is seen
        self.assertIn("a check on it failed", out)
        self.assertEqual(len(comments), 1)

        pr["headRefOid"] = remote("feat-a")
        (self.a / ".github" / "workflows").mkdir(parents=True)
        pr["headRefOid"] = commit(self.a, ".github/workflows/tests.yml", "on: push\n", push="feat-a")
        commit(self.app, "e.txt", "main again\n", push="main")
        code, out = refresh()
        self.assertEqual(code, 1)
        self.assertIn("changes what its CI runs (.github/workflows/tests.yml)", out)
        self.assertEqual(core.git(self.a, "rev-parse", "HEAD"), pr["headRefOid"])  # nothing changed
        sh(self.a, "reset", "-q", "--hard", "HEAD~1")
        sh(self.a, "push", "-q", "--force", "origin", "feat-a")

        pr["headRefOid"] = remote("feat-a")
        (self.a / "b.txt").write_text("unsaved\n", encoding="utf-8")
        self.assertIn("uncommitted changes", refresh()[1])
        pr["headRefName"] = "main"
        code, out = refresh()
        self.assertEqual(code, 1)
        self.assertIn("main checkout, which nh refresh never touches", out)

    def test_refresh_refuses_a_full_run_path_and_a_ready_that_is_missing_or_lists_gap_checks(self):
        def commit(where, name, text, push=None):
            (where / name).parent.mkdir(parents=True, exist_ok=True)
            (where / name).write_text(text, encoding="utf-8")
            sh(where, "add", "-A")
            sh(where, "commit", "-q", "-m", f"change {name}")
            if push:
                sh(where, "push", "-q", "origin", push)
            return core.git(where, "rev-parse", "HEAD")

        green = [{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS", "workflowName": "tests"}]
        posted = []
        pr = {"state": "OPEN", "headRefName": "feat-a", "baseRefName": "main"}

        def gh(cwd, *args):
            if args[:2] == ("pr", "comment"):
                posted.append(args[-1])
                return ""
            if "statusCheckRollup" in args[-1]:
                return json.dumps({"headRefOid": core.git(self.origin, "rev-parse", "feat-a"),
                                   "statusCheckRollup": green})
            return json.dumps(pr if "comments" in pr else dict(pr, comments=[ready(pr["headRefOid"])]))
        turns._gh = gh
        self.addCleanup(setattr, turns, "_sleep", turns._sleep)
        turns._sleep = lambda secs: None
        refresh = lambda: self.wk(self.app, "refresh", "--pr", "7")  # noqa: E731
        commit(self.app, "CONTRIBUTING.md", "# C\n\n## Change → check\n| Change | Check |\n|---|---|\n"
               "| `wk/x.py` | `python -m unittest tests.test_x` |\n"
               "| `.github/workflows/`, `tests/test_toolkits.py`, `ci` (what CI runs) | the full run |\n", push="main")
        sh(self.a, "pull", "-q", "origin", "main")

        pr["headRefOid"] = commit(self.a, "tests/test_toolkits.py", "x\n", push="feat-a")
        commit(self.app, "m1.txt", "main\n", push="main")
        code, out = refresh()
        self.assertEqual(code, 1)  # read from the map's full-run row, not only .github/workflows/
        self.assertIn("changes what its CI runs (tests/test_toolkits.py)", out)
        self.assertEqual(core.git(self.a, "rev-parse", "HEAD"), pr["headRefOid"])
        sh(self.a, "reset", "-q", "--hard", "HEAD~1")
        pr["headRefOid"] = commit(self.a, "ci/run.sh", "x\n")
        sh(self.a, "push", "-q", "--force", "origin", "feat-a")
        code, out = refresh()
        self.assertEqual(code, 1)  # a map path with no trailing / still covers what is inside it
        self.assertIn("changes what its CI runs (ci/run.sh)", out)
        sh(self.a, "reset", "-q", "--hard", "HEAD~1")
        old = core.git(self.a, "rev-parse", "HEAD")
        pr["headRefOid"] = commit(self.a, "a.txt", "the PR's edit\n")
        sh(self.a, "push", "-q", "--force", "origin", "feat-a")
        head = pr["headRefOid"]

        gaps = ready(head, "lint (passed)", "2026-02-01")
        for said, why in (([], "has no Ready @ <sha> comment"),
                          ([{"body": f"Ready @ {head}\n\nRan tests."}], "no 'Gap checks:' line"),
                          ([ready(head), gaps], "lists gap checks (lint (passed))"),
                          ([ready(head, "\n- lint (passed)")], "'Gap checks:' line couldn't be read"),
                          ([ready(old)], f"names {old[:12]}, not the PR's head {head[:12]}")):
            pr["comments"] = said
            code, out = refresh()
            self.assertEqual(code, 1, out)
            self.assertIn(why, out)
            self.assertEqual((core.git(self.a, "rev-parse", "HEAD"), posted), (pr["headRefOid"], []))

        pr["comments"] = [gaps, ready(head[:7], at="2026-03-01")]  # a later clean Ready is the one read
        code, out = refresh()
        self.assertEqual(code, 0, out)
        self.assertTrue(posted[0].startswith(f"Ready @ {out.splitlines()[0]}"))

    def test_take_refuse_expire_release_check(self):
        self.assertEqual(self.wk(self.a, "turn", "take", "env", "orders", "items")[0], 0)
        self.assertEqual(self.wk(self.a, "turn", "check", "env", "orders", "items")[0], 0)
        code, out = self.wk(self.b, "turn", "take", "env", "items")
        self.assertEqual(code, 1)
        self.assertIn("held by feat-a", out)
        self.assertIn("2h00m left", out)
        self.assertEqual(self.wk(self.b, "turn", "check", "env", "items")[0], 1)
        self.assertEqual(self.wk(self.b, "turn", "take", "env", "other")[0], 0)  # a different item is free
        self.assertEqual(self.wk(self.a, "turn", "check", "env", "orders", "other")[0], 1)  # must hold every item
        self.assertEqual(core.git(self.app, "status", "--porcelain"), "")  # state never lands in the work tree
        self.now += 3 * 3600  # expired: check fails, others may take it
        self.assertEqual(self.wk(self.a, "turn", "check", "env", "orders")[0], 1)
        self.assertEqual({(g["holder"], g["state"]) for g in turns.rows(self.app, self.now)},
                         {("feat-a", "expired"), ("feat-b", "expired")})
        self.assertEqual(self.wk(self.b, "turn", "take", "env", "items", "--hours", "1")[0], 0)
        self.assertEqual(self.wk(self.b, "turn", "check", "env", "items")[0], 0)
        self.now += 50 * 60
        self.assertIn("0h10m left", self.wk(self.b, "turn", "list")[1])
        self.assertEqual(self.wk(self.b, "turn", "renew", "env")[0], 0)
        self.now += 90 * 60
        self.assertEqual(self.wk(self.b, "turn", "check", "env", "items")[0], 0)
        self.assertEqual(self.wk(self.b, "turn", "release", "env", "items")[0], 0)
        self.assertEqual(self.wk(self.a, "turn", "take", "env", "items")[0], 0)
        self.assertEqual(self.wk(self.a, "turn", "release")[0], 0)
        self.assertEqual(self.wk(self.a, "turn", "release")[0], 1)  # nothing left to release
        self.assertEqual(self.wk(self.a, "turn", "take", "env")[0], 2)  # usage error, not a refusal

    def test_pause_stops_the_clock(self):
        self.wk(self.a, "turn", "take", "env", "db")
        self.now += 30 * 60
        self.assertEqual(self.wk(self.a, "turn", "pause")[0], 0)
        self.now += 10 * 3600  # waiting on approval: still held, nobody else may take it
        code, out = self.wk(self.b, "turn", "take", "env", "db")
        self.assertEqual(code, 1)
        self.assertIn("paused", out)
        self.assertEqual(self.wk(self.a, "turn", "check", "env", "db")[0], 0)
        self.assertEqual(self.wk(self.a, "turn", "resume")[0], 0)
        self.assertIn("1h30m left", self.wk(self.a, "turn", "list")[1])
        self.now += 2 * 3600
        self.assertEqual(self.wk(self.a, "turn", "check", "env", "db")[0], 1)

    def test_take_refused_until_branch_has_latest_base(self):
        (self.app / "new.txt").write_text("x", encoding="utf-8")
        sh(self.app, "add", "-A")
        sh(self.app, "commit", "-q", "-m", "moved on")
        sh(self.app, "push", "-q", "origin", "main")
        code, out = self.wk(self.a, "turn", "take", "env", "db")
        self.assertEqual(code, 1)
        self.assertIn("does not contain the latest origin/main", out)
        sh(self.a, "merge", "-q", "origin/main")
        self.assertEqual(self.wk(self.a, "turn", "take", "env", "db")[0], 0)

    def test_recorded_work_on_main_does_not_block_a_turn(self):
        (self.app / ".work").mkdir()
        (self.app / ".work" / "hours.tsv").write_text("date\n", encoding="utf-8")
        sh(self.app, "add", "-A")
        sh(self.app, "commit", "-q", "-m", "nh: recorded hours")
        sh(self.app, "push", "-q", "origin", "main")
        self.assertEqual(self.wk(self.a, "turn", "take", "env", "db")[0], 0)  # only .work/ moved on main
        (self.app / "code.js").write_text("x", encoding="utf-8")
        sh(self.app, "add", "-A")
        sh(self.app, "commit", "-q", "-m", "real change")
        sh(self.app, "push", "-q", "origin", "main")
        self.assertEqual(self.wk(self.b, "turn", "take", "env", "orders")[0], 1)  # code moved: merge first

    def test_gate_release(self):
        self.assertEqual(self.wk(self.app, "gate", "release")[0], 0)
        code, out = self.wk(self.a, "gate", "release")
        self.assertEqual(code, 1)
        self.assertIn("only from main", out)
        (self.app / "README.md").write_text("changed\n", encoding="utf-8")
        self.assertEqual(self.wk(self.app, "gate", "release")[0], 1)
        sh(self.app, "commit", "-q", "-am", "local only")
        code, out = self.wk(self.app, "gate", "release")
        self.assertEqual(code, 1)
        self.assertIn("1 ahead, 0 behind", out)
        sh(self.app, "push", "-q", "origin", "main")
        self.assertEqual(self.wk(self.app, "gate", "release")[0], 0)

    def test_base_branch_comes_from_settings(self):
        self.settings.write_text(
            f'owner = "Pat"\n\n[repos.app]\npath = "{core.posix(self.app)}"\nbase = "trunk"\n', encoding="utf-8")
        code, out = self.wk(self.app, "gate", "release")
        self.assertEqual(code, 1)
        self.assertIn("only from trunk", out)

    def test_gate_approved(self):
        head, old = "abcdef1234567890" * 2 + "abcdef12", "1234567" + "0" * 33
        pr = {"url": "https://example.org/pr/7", "headRefOid": head,
              "comments": [{"body": "Looks good.\napproved BY pat:  Deploy to production @ ABCDEF1 ", "url": "u-new",
                            "createdAt": "2026-10-26T11:00:00Z"},
                           {"body": f"Approved by Pat: run migration @ {old}", "url": "u-old",
                            "createdAt": "2026-10-26T12:00:00Z"},
                           {"body": "Approved by Pat: deploy to staging @ abcdef", "url": "u-short",
                            "createdAt": "2026-10-26T11:00:00Z"},
                           {"body": f"Approved by Sam: delete data @ {head}", "url": "u-sam",
                            "createdAt": "2026-10-26T11:00:00Z"}]}
        calls = []
        turns._gh = lambda cwd, *args: calls.append(args) or json.dumps(pr)
        code, out = self.wk(self.app, "gate", "approved", "--pr", "7", "--action", "deploy to production")
        self.assertEqual(code, 0)
        self.assertIn("u-new", out)
        self.assertEqual(calls[0][:3], ("pr", "view", "7"))
        code, out = self.wk(self.app, "gate", "approved", "--pr", "7", "--action", "run migration")
        self.assertEqual(code, 1)  # approved an older commit, however recent the comment
        self.assertIn(f"head is now {head[:12]}", out)
        code, out = self.wk(self.app, "gate", "approved", "--pr", "7", "--action", "deploy to staging")
        self.assertEqual(code, 1)  # a 6-character SHA is not enough
        self.assertIn(f"no comment with the line 'Approved by the owner: deploy to staging @ {head[:12]}'", out)
        self.assertEqual(self.wk(self.app, "gate", "approved", "--pr", "7", "--action", "ship it")[0], 1)
        pr["comments"].append({"body": f"Approved by the owner: ship it @ {head[:9]}", "url": "u-ship"})
        code, out = self.wk(self.app, "gate", "approved", "--pr", "7", "--action", "ship it")
        self.assertEqual(code, 0)  # the form every approval takes now: no one's name
        self.assertIn("u-ship", out)
        self.assertEqual(self.wk(self.app, "gate", "approved", "--pr", "7", "--action", "delete data")[0], 1)
        pr["headRefOid"] = "f" * 40  # a later push voids the approval
        self.assertEqual(self.wk(self.app, "gate", "approved", "--pr", "7", "--action", "deploy to production")[0], 1)

        # a public repo: the approval names no one, so the owner's name never reaches its readers
        self.settings.write_text(f'owner = "Pat"\n\n[repos.app]\npath = "{core.posix(self.app)}"\npublic = true\n',
                                 encoding="utf-8")
        pr["comments"].append({"body": f"Approved by Pat: merge @ {pr['headRefOid']}", "url": "u-pat"})
        code, out = self.wk(self.app, "gate", "approved", "--pr", "7", "--action", "merge")
        self.assertEqual(code, 1)
        self.assertIn("'Approved by the owner: merge @ ffffffffffff'", out)
        pr["comments"].append({"body": f"Approved by the owner: merge @ {pr['headRefOid'][:7]}", "url": "u-owner"})
        code, out = self.wk(self.app, "gate", "approved", "--pr", "7", "--action", "merge")
        self.assertEqual(code, 0)
        self.assertIn("u-owner", out)

    def test_gate_fresh_waits_only_for_main_changes_to_the_prs_own_files(self):
        def commit(where, name, text, push=False):
            (where / name).write_text(text, encoding="utf-8")
            sh(where, "add", "-A")
            sh(where, "commit", "-q", "-m", f"change {name}")
            if push:
                sh(where, "push", "-q", "origin", "main")
            return core.git(where, "rev-parse", "HEAD")

        pr = {"state": "OPEN", "headRefName": "feat-a", "baseRefName": "main",
              "url": "https://github.com/acme/app/pull/7", "headRefOid": commit(self.a, "README.md", "the PR's edit\n")}
        turns._gh = lambda cwd, *args: json.dumps(pr)
        gate = lambda *more: self.wk(self.app, "gate", "fresh", "--pr", "7", *more)  # noqa: E731
        code, out = gate("--sha", pr["headRefOid"][:7])
        self.assertEqual(code, 0)  # main hasn't moved
        self.assertIn('"repo": "acme/app"', out)  # names the repo it judged, and the head
        self.assertIn(pr["headRefOid"][:12], out)
        code, out = gate("--sha", "1234567")
        self.assertEqual(code, 1)  # not the approved version (or another repo's PR #7)
        self.assertIn(f"acme/app PR #7's head is {pr['headRefOid'][:12]}, not the approved 1234567", out)
        self.assertEqual(gate("--sha", pr["headRefOid"][:6])[0], 1)  # too short to name one commit
        commit(self.app, "other.txt", "x\n", push=True)
        self.assertEqual(gate()[0], 0)  # main moved, but on other files: no wait
        (self.app / ".work").mkdir()
        commit(self.app, ".work/README.md", "recorded\n", push=True)
        self.assertEqual(gate()[0], 0)  # recorded work never counts
        commit(self.app, "README.md", "main's edit\n", push=True)
        code, out = gate()
        self.assertEqual(code, 1)  # same file: Git would combine the two edits untested
        self.assertIn("acme/app PR #7 lacks the latest main: origin/main changed README.md", out)
        self.assertIn("Do not merge", out)
        self.assertIn("re-runs its tests", out)
        sh(self.a, "merge", "-q", "-X", "theirs", "origin/main")  # the builder takes main
        pr["headRefOid"] = core.git(self.a, "rev-parse", "HEAD")
        self.assertEqual(gate()[0], 0)
        pr["state"] = "MERGED"
        self.assertEqual(gate()[0], 1)
        self.assertEqual(self.wk(self.app, "gate", "fresh")[0], 2)  # needs --pr

    def test_a_yes_carries_over_only_clean_merges_of_main_with_ci_green(self):
        def commit(where, name, text, push=False):
            (where / name).write_text(text, encoding="utf-8")
            sh(where, "add", "-A")
            sh(where, "commit", "-q", "-m", f"change {name}")
            if push:
                sh(where, "push", "-q", "origin", "main")
            return core.git(where, "rev-parse", "HEAD")

        def run(conclusion, workflow="tests"):
            return {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": conclusion, "workflowName": workflow}
        green = [run("SKIPPED"), run("SUCCESS")]  # skipped while a draft, then passed
        pr = {"state": "OPEN", "headRefName": "feat-a", "baseRefName": "main", "statusCheckRollup": green,
              "url": "https://github.com/acme/app/pull/7"}
        turns._gh = lambda cwd, *args: json.dumps(pr)
        gate = lambda sha: self.wk(self.app, "gate", "fresh", "--pr", "7", "--sha", sha)  # noqa: E731
        # clean: the approved version, then a merge of main with nothing resolved by hand
        approved = commit(self.a, "README.md", "the PR's edit\n")
        commit(self.app, "other.txt", "main moved\n", push=True)
        sh(self.a, "merge", "-q", "--no-edit", "origin/main")
        pr["headRefOid"] = core.git(self.a, "rev-parse", "HEAD")
        code, out = gate(approved[:7])
        self.assertEqual(code, 0)
        self.assertIn(f"approved at {approved[:12]}, carried over: only clean merges of main since", out)
        pr["statusCheckRollup"] = [run("SKIPPED")]
        code, out = gate(approved[:7])
        self.assertEqual(code, 1)  # the tests must pass on the new head: skipped is not passed
        self.assertIn("no test workflow passed on it", out)
        pr["statusCheckRollup"] = [run("CANCELLED"), run("SUCCESS", "lint")]
        self.assertEqual(gate(approved[:7])[0], 1)  # tests cancelled; lint passing doesn't stand in
        pr["statusCheckRollup"] = [run("SKIPPED"), run("SUCCESS", "deploy"), run("SUCCESS", "")]
        self.assertIn("no test workflow passed on it", gate(approved[:7])[1])  # a deploy or a bot's check isn't tests
        pr["statusCheckRollup"] = green + [run("FAILURE", "lint")]
        self.assertIn("a check on it failed, was cancelled or went stale", gate(approved[:7])[1])
        pr["statusCheckRollup"] = green + [run("STALE", "lint")]
        self.assertEqual(gate(approved[:7])[0], 1)
        pr["statusCheckRollup"] = green
        # an extra commit after the merge is new work: a new yes
        pr["headRefOid"] = commit(self.a, "more.txt", "a fix\n")
        code, out = gate(approved[:7])
        self.assertEqual(code, 1)
        self.assertIn("is new work, not a merge of origin/main", out)
        self.assertIn("Ask the owner for a yes on", out)
        # a conflict resolved by hand: a new yes
        approved = commit(self.b, "README.md", "b's edit\n")
        commit(self.app, "README.md", "main's edit\n", push=True)
        self.assertNotEqual(subprocess.run(["git", "-C", str(self.b), "merge", "-q", "origin/main"],
                                           capture_output=True).returncode, 0)
        (self.b / "README.md").write_text("both edits\n", encoding="utf-8")
        sh(self.b, "commit", "-q", "-am", "merge main, by hand")
        pr["headRefOid"] = core.git(self.b, "rev-parse", "HEAD")
        code, out = gate(approved[:7])
        self.assertEqual(code, 1)
        self.assertIn("changes the merge by hand", out)

    def test_no_owner_means_no_approval(self):
        turns._gh = lambda cwd, *args: self.fail("gh must not be asked when nobody can approve")
        code, out = self.wk(self.app, "gate", "approved", "--pr", "7", "--action", "deploy",
                            settings=self.tmp / "missing.toml")
        self.assertEqual(code, 2)
        self.assertIn("No approver", out)


if __name__ == "__main__":
    unittest.main()
