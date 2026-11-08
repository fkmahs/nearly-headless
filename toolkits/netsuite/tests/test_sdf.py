"""Offline tests for the SDF kit (no NetSuite calls):  py -m unittest discover -s toolkits/netsuite/tests"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sdf  # noqa: E402

# A client repo's sdf.toml, for a made-up client: every test runs the kit with it.
SDF_TOML = """requires = "1.39.0"
own_folder = "/SuiteScripts/ACME/"
wrapper_env = "ACME_SDF_WRAPPER"
reference = "reference/vendor"
replace_in_place_flag = "replace-vendor"
tool_files = ["tools/sdf.py", "sdf.toml"]
entry = "py tools/sdf.py"

[sb]
auth_id = "acme-sb"
domain = "1234567-sb1.app.netsuite.com"
env_prefix = "ACME_SB_"

[prod]
auth_id = "acme-prod"
domain = "1234567.app.netsuite.com"
env_prefix = "ACME_PROD_"
"""
sdf.CONFIG = {**sdf.DEFAULTS, **sdf.tomllib.loads(SDF_TOML)}
REAL_IMPORT_PRODUCTION = sdf.import_production


def refuses(test, fn, *args):
    """Assert fn(*args) stops through sdf.fail and return its message."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err), test.assertRaises(SystemExit):
        fn(*args)
    return err.getvalue()


class FakeEngine:
    """Stands in for work-kit's wk and for gh. wk refuses (exit 1) any command starting with one of `refuse`;
    `turns` is what `wk --json turn list sandbox` prints; `pr` is what `gh pr view` returns."""

    def __init__(self, refuse=(), turns=(), pr=None):
        self.calls, self.refuse, self.turns, self.pr = [], [tuple(r) for r in refuse], list(turns), pr

    def wk(self, *args):
        self.calls.append(args)
        if args[:3] == ("--json", "turn", "list"):
            return 0, json.dumps(self.turns)
        if any(args[:len(r)] == r for r in self.refuse):
            return 1, f"wk: refused: {' '.join(args)}"
        if args[:2] == ("gate", "approved"):   # like wk: it names the comment it accepted (the approver's line)
            ok = [c for c in (self.pr or {}).get("comments", []) if c.get("body", "").startswith("Approved by Owner:")]
            return 0, json.dumps({"ok": True, "comment": ok[-1].get("url", "") if ok else ""})
        return 0, "{}"

    def gh(self, *args):
        self.calls.append(("gh", *args))
        return self.pr or {"state": "OPEN", "comments": []}

    def patch(self):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(sdf, "wk", side_effect=self.wk))
        stack.enter_context(mock.patch.object(sdf, "gh_json", side_effect=self.gh))
        return stack


def merged_pr(merge, head, action="deploy", extra="", approver="Owner"):
    """gh pr view data for a merged PR approved for action at head (the PR's own head commit, not the merge)."""
    return {"state": "MERGED", "mergeCommit": {"oid": merge}, "headRefOid": head, "url": "https://x/pull/7",
            "comments": [{"body": "Ready @ " + head[:7], "url": "https://x/pull/7#c1"},
                         {"body": f"Approved by {approver}: {action} to production @ {head[:7]}\n{extra}".strip(),
                          "url": "https://x/pull/7#c2"}]}


def approval(pr, extra=""):
    """The approval release_compare gets once wk gate approved has passed for PR #pr."""
    return {"label": f"PR #{pr}", "text": f"Approved by Owner: deploy to production @ abc1234\n{extra}", "pr": pr}


class UploadTargetsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cabinet = Path(self.tmp.name) / "FileCabinet"
        self.js = self.cabinet / "SuiteScripts" / "ACME" / "ap" / "acme_ue_x.js"
        self.js.parent.mkdir(parents=True)
        self.js.write_text("define([], () => ({}));\n")
        (self.cabinet / "SuiteScripts" / "vendor.js").write_text("// VENDOR\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_accepts_acme_file_and_dedupes(self):
        path = "/SuiteScripts/ACME/ap/acme_ue_x.js"
        self.assertEqual(sdf.upload_targets([path, path], self.cabinet), [(path, self.js)])

    def test_undoes_git_bash_path_conversion(self):
        got = sdf.upload_targets(["C:/Program Files/Git/SuiteScripts/ACME/ap/acme_ue_x.js"], self.cabinet)
        self.assertEqual(got[0][0], "/SuiteScripts/ACME/ap/acme_ue_x.js")

    def test_refuses_outside_acme(self):
        for path in ("/SuiteScripts/vendor.js", "/SuiteScripts/ACMEX/a.js", "/SuiteScripts/ACME | R1 | P&L/a.js","/suitescripts/acme/ap/acme_ue_x.js", "SuiteScripts/ACME/ap/acme_ue_x.js"):
            self.assertIn("only takes files under", refuses(self, sdf.upload_targets, [path], self.cabinet), path)

    def test_refuses_traversal_wildcards_and_folders(self):
        for path in ("/SuiteScripts/ACME/../vendor.js", "/SuiteScripts/ACME/ap/*.js", "/SuiteScripts/ACME/", "/SuiteScripts/ACME//ap/acme_ue_x.js", "/SuiteScripts/ACME/./ap/acme_ue_x.js"):
            self.assertIn("plain file path", refuses(self, sdf.upload_targets, [path], self.cabinet), path)

    def test_refuses_missing_file_and_directory(self):
        for path in ("/SuiteScripts/ACME/ap/nope.js", "/SuiteScripts/ACME/ap"):
            self.assertIn("not found", refuses(self, sdf.upload_targets, [path], self.cabinet), path)

    def test_one_bad_path_refuses_all(self):
        refuses(self, sdf.upload_targets, ["/SuiteScripts/ACME/ap/acme_ue_x.js", "/SuiteScripts/vendor.js"], self.cabinet)

    def test_replace_vendor_maps_pipe_folders_to_local_dash(self):
        local = self.cabinet / "SuiteScripts" / "VENDOR - R1 & R2 - P&L" / "x.js"
        local.parent.mkdir(parents=True)
        local.write_text("// VENDOR\n")
        path = "/SuiteScripts/VENDOR | R1 & R2 | P&L/x.js"
        self.assertEqual(sdf.upload_targets([path], self.cabinet, True), [(path, local)])
        self.assertEqual(sdf.upload_targets(["/SuiteScripts/vendor.js"], self.cabinet, True)[0][1], self.cabinet / "SuiteScripts" / "vendor.js")

    def test_replace_vendor_limits(self):
        self.assertIn("without --replace-vendor", refuses(self, sdf.upload_targets, ["/SuiteScripts/ACME/ap/acme_ue_x.js"], self.cabinet, True))
        self.assertIn("only takes files under /SuiteScripts/", refuses(self, sdf.upload_targets, ["/Templates/x.js"], self.cabinet, True))
        for path in ("/SuiteScripts/../vendor.js", "/SuiteScripts/*.js", "/SuiteScripts//vendor.js"):
            self.assertIn("plain file path", refuses(self, sdf.upload_targets, [path], self.cabinet, True), path)
        self.assertIn("not found", refuses(self, sdf.upload_targets, ["/SuiteScripts/VENDOR | X/nope.js"], self.cabinet, True))


@unittest.skipUnless(shutil.which("node"), "node not on PATH")
class NodeCheckTest(unittest.TestCase):
    def test_good_and_bad_js(self):
        with tempfile.TemporaryDirectory() as tmp:
            good, bad, other = Path(tmp, "good.js"), Path(tmp, "bad.js"), Path(tmp, "template.html")
            good.write_text("define(['N/log'], (log) => ({ beforeSubmit: () => log.debug('x', 1) }));\n")
            bad.write_text("define([], () => ({ beforeSubmit: () => { }));\n")
            other.write_text("<p>not javascript {</p>\n")
            sdf.node_check([good, other])
            self.assertIn("node --check failed for bad.js", refuses(self, sdf.node_check, [good, bad]))


class GitRepoTest(unittest.TestCase):
    """A throwaway git repo as ROOT, on main."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "repo"
        self.root.mkdir()
        (self.root / "sdf.toml").write_text(SDF_TOML)
        self.production = {}   # src path -> production's copy (bytes); what the fake read-only import finds
        self.compared = []     # the items each production compare imported
        self.patches = [mock.patch.object(sdf, "ROOT", self.root), mock.patch.object(sdf, "CONFIG", dict(sdf.CONFIG)),
                        mock.patch.object(sdf, "SRC", self.root / "src"),
                        mock.patch.object(sdf, "CABINET", self.root / "src" / "FileCabinet"),
                        mock.patch.object(sdf, "find_root", return_value=self.root),
                        mock.patch.object(sdf, "import_production", side_effect=self.fake_production),
                        mock.patch.object(sdf, "compare_out_dir", side_effect=self.out_dir),
                        mock.patch.object(sdf, "kit_checkout_refusal", return_value=None)]
        for p in self.patches:
            p.start()
        self.clock = 1_790_000_000
        self.run_git("init", "-q")
        self.commit("src/FileCabinet/SuiteScripts/ACME/a.js", "1", "Start")
        self.run_git("branch", "-M", "main")
        self.prs = 0

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def run_git(self, *args):
        self.clock += 60  # distinct, increasing commit and tag dates
        env = dict(os.environ, GIT_COMMITTER_DATE=f"{self.clock} +0000", GIT_AUTHOR_DATE=f"{self.clock} +0000")
        res = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=self.root, check=True,
                             capture_output=True, text=True, env=env)
        return res.stdout.strip()

    def commit(self, rel, text, msg):
        path = self.root / rel
        if text is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        self.run_git("add", "-A")
        self.run_git("commit", "-q", "-m", msg)
        return self.head()

    def head(self):
        return self.run_git("rev-parse", "--short=7", "HEAD")

    def merge_pr(self, n, *changes):
        """Merge PR #n into the current branch as GitHub does (a merge commit), with changes [(path, text)] made
        on its own branch; returns the merge commit's short SHA."""
        here = self.run_git("rev-parse", "--abbrev-ref", "HEAD")
        self.run_git("checkout", "-q", "-b", f"pr{n}")
        for rel, text in changes:
            self.commit(rel, text, f"Change {rel.rsplit('/', 1)[-1]}")
        self.run_git("checkout", "-q", here)
        self.run_git("merge", "-q", "--no-ff", "-m", f"Merge pull request #{n} from acme/pr{n}", f"pr{n}")
        return self.head()

    def release(self, name, action="deploy", files=(), owner="PR #1"):
        self.run_git("tag", "-a", name, "-m", sdf.tag_message(owner, self.head(), action, files))

    def fake_production(self, items, head, out_dir):
        self.compared.append([p for p, _ in items])
        return {p: self.production.get(p) for p, _ in items}

    def out_dir(self):
        path = Path(self.tmp.name) / f"compare-{len(self.compared)}"
        path.mkdir(exist_ok=True)
        return path

    def live(self, path, text):
        """Production runs this copy of path."""
        self.production[path] = text.encode()

    def compare(self, pr, *paths, extra="", approved=True, upload_objects=None):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            refusal = sdf.release_compare(f"PR #{pr}", self.head(), "deploy", [(p, sdf.cabinet_of(p)) for p in paths],
                                          approval=approval(pr, extra) if approved else None,
                                          upload_objects=upload_objects)
        return refusal, out.getvalue()


class ProductionCompareTest(GitRepoTest):
    """Before a production release, each item it sends is compared with production's current copy."""
    JS = "src/FileCabinet/SuiteScripts/ACME/a.js"
    IC = "src/FileCabinet/SuiteScripts/ACME/ic/ic.js"
    XML = "src/Objects/customscript_acme_x.xml"
    RECORD = ('<suitelet scriptid="customscript_acme_x"><name>X</name><scriptfile>[/SuiteScripts/ACME/x.js]</scriptfile>'
              '<scriptdeployments><scriptdeployment scriptid="customdeploy_acme_x"><loglevel>{log}</loglevel>'
              '<audslctrole>A|B</audslctrole></scriptdeployment></scriptdeployments></suitelet>')

    def test_already_live_needs_no_approval(self):
        self.merge_pr(7, (self.JS, "2"))
        self.live(self.JS, "2\r\n".replace("\r\n", "") )
        refusal, out = self.compare(7, self.JS, approved=False)
        self.assertIsNone(refusal)
        self.assertIn(f"Already live (sent again, needs no approval):\n    {self.JS}", out)
        self.assertIn("Needs approval: nothing beyond the gates", out)

    def test_line_endings_do_not_count(self):
        self.merge_pr(7, (self.JS, "a\nb\n"))
        self.live(self.JS, "a\r\nb\r\n")
        self.assertIn("Already live", self.compare(7, self.JS)[1])

    def test_changed_since_production_needs_only_the_prs_since(self):
        self.merge_pr(7, (self.JS, "2"))
        self.live(self.JS, "2")              # PR #7 is live; PR #11 is what changed since
        self.merge_pr(11, (self.JS, "3"))
        refusal, out = self.compare(11, self.JS)
        self.assertIsNone(refusal)
        self.assertIn(f"{self.JS}   PR #11   (production has git's version of", out)
        self.assertIn("Needs approval: PR #11 (released PR)", out)

    def test_another_prs_unreleased_change_needs_naming(self):
        self.live(self.JS, "1")              # production still runs the first version
        self.merge_pr(7, (self.JS, "2"))
        self.merge_pr(11, (self.JS, "3"))
        refusal, out = self.compare(11, self.JS)
        self.assertIn("PR #7 (NOT COVERED), PR #11 (released PR)", out)
        self.assertIn(f"PR #7: {self.JS}", refusal)
        self.assertIn("adds 'Includes PR #7' to the approval comment on PR #11", refusal)
        self.assertIsNone(self.compare(11, self.JS, extra="Includes PR #7 (first version)")[0])
        self.assertIn("PR #7 (NOT COVERED)", self.compare(11, self.JS, approved=False)[1])

    def test_only_includes_lines_count(self):
        self.assertEqual(sdf.included("Approved by Owner: deploy to production @ abc1234\n"
                                      "Includes PR #7 (IC report), 9f8e7d6\nsee #12 and 1a2b3c4d in the thread\n"
                                      "> Includes PR #13\n- includes: pr #14"), {"PR #7", "9f8e7d6", "PR #14"})
        self.live(self.JS, "1")
        self.merge_pr(7, (self.JS, "2"))
        self.merge_pr(11, (self.JS, "3"))
        refusal, out = self.compare(11, self.JS, extra="Thanks, this replaces the old #7 report")
        self.assertIn(f"PR #7: {self.JS}", refusal)

    def test_an_unattributed_commit_needs_its_sha(self):
        self.live(self.JS, "1")
        loose = self.commit(self.JS, "2", "quick fix")
        self.merge_pr(11, (self.JS, "3"))
        refusal, out = self.compare(11, self.JS)
        self.assertIn(f"commit {loose} (no PR)", out)
        self.assertIn(f"{loose}: {self.JS}", refusal)
        self.assertIsNone(self.compare(11, self.JS, extra=f"Includes {loose} (fix)")[0])

    def test_new_items_need_the_prs_that_made_them(self):
        self.merge_pr(7, (self.IC, "ic"))
        self.merge_pr(11, (self.IC, "ic v2"))
        refusal, out = self.compare(11, self.IC)
        self.assertIn(f"New, not in production yet (needs the PRs that made it):\n    {self.IC}   PR #7, PR #11", out)
        self.assertIn(f"PR #7: {self.IC}", refusal)
        self.merge_pr(12, (self.XML, self.RECORD.format(log="DEBUG")))
        self.assertIsNone(self.compare(12, self.XML)[0])

    def test_a_version_git_never_had_refuses_with_a_diff(self):
        self.merge_pr(7, (self.JS, "2\n"))
        self.live(self.JS, "hotfix made in production\n")
        refusal, out = self.compare(7, self.JS, extra="Includes PR #1")
        self.assertIn("production holds a change git doesn't have", refusal)
        self.assertIn(self.JS, refusal)
        diff = Path(refusal.split("production -> git: ")[1].split(")")[0])
        text = diff.read_text(encoding="utf-8")
        self.assertIn("-hotfix made in production", text)
        self.assertIn("+2", text)
        self.assertIn("PRODUCTION HOLDS A CHANGE GIT DOESN'T HAVE", out)

    def test_a_version_from_a_prs_branch_still_counts_as_git(self):
        """Production may run a commit from inside a PR's branch (not on main's own line): that is git's too."""
        self.run_git("checkout", "-q", "-b", "pr7")
        self.commit(self.JS, "2 draft", "Draft")
        self.commit(self.JS, "2", "Final")
        self.run_git("checkout", "-q", "main")
        self.run_git("merge", "-q", "--no-ff", "-m", "Merge pull request #7 from acme/pr7", "pr7")
        self.live(self.JS, "2 draft")
        refusal, out = self.compare(7, self.JS)
        self.assertIsNone(refusal)
        self.assertIn(f"{self.JS}   PR #7   (production has git's version of", out)

    def test_objects_compare_by_meaning(self):
        self.merge_pr(7, (self.XML, self.RECORD.format(log="DEBUG")))
        # What NetSuite does on its own: children in another order, '|' lists reordered, fields it fills in.
        self.live(self.XML, '<suitelet scriptid="customscript_acme_x"><scriptdeployments><scriptdeployment '
                  'scriptid="customdeploy_acme_x"><audslctrole>B|A</audslctrole><loglevel>DEBUG</loglevel>'
                  '<enabledle>T</enabledle></scriptdeployment></scriptdeployments><name>X</name>'
                  '<scriptfile>[/SuiteScripts/ACME/x.js]</scriptfile><iconbuiltin>T</iconbuiltin></suitelet>')
        refusal, out = self.compare(7, self.XML, approved=False)
        self.assertIsNone(refusal)
        self.assertIn("Already live", out)
        self.merge_pr(11, (self.XML, self.RECORD.format(log="AUDIT")))
        refusal, out = self.compare(11, self.XML)
        self.assertIsNone(refusal)
        self.assertIn(f"{self.XML}   PR #11", out)

    def test_an_object_changed_in_production_refuses(self):
        self.merge_pr(7, (self.XML, self.RECORD.format(log="DEBUG")))
        self.live(self.XML, self.RECORD.format(log="ERROR"))
        refusal, _ = self.compare(7, self.XML)
        self.assertIn("production holds a change git doesn't have", refusal)
        diff = Path(refusal.split("production -> git: ")[1].split(")")[0]).read_text(encoding="utf-8")
        self.assertIn("loglevel: git='DEBUG' production='ERROR'", diff)

    def test_an_upload_whose_prs_have_objects_not_live_refuses(self):
        self.live(self.JS, "1")
        self.merge_pr(7, (self.JS, "2"), (self.XML, self.RECORD.format(log="DEBUG")))
        refusal = self.compare(7, self.JS, upload_objects={self.XML})[0]
        self.assertIn(f"objects production doesn't have yet ({self.XML}). That is an object change; use deploy", refusal)
        self.live(self.XML, self.RECORD.format(log="DEBUG"))
        self.assertIsNone(self.compare(7, self.JS, upload_objects={self.XML})[0])
        self.assertEqual(sdf.pr_objects({"PR #7"}), {self.XML})

    def test_tag_owner(self):
        self.assertEqual(sdf.tag_owner("prod-2026-10-11-pr12-2", ""), "PR #12")
        self.assertEqual(sdf.tag_owner("prod-2026-10-11-ACME-012-3", ""), "ACME-012")


class SdfCompareTest(unittest.TestCase):
    """What NetSuite changes on its own never counts as a difference."""
    GIT = ('<customrecordtype scriptid="customrecord_x"><customrecordcustomfields>'
           '<customrecordcustomfield scriptid="custrecord_amount"><applyformatting>F</applyformatting>'
           '<ismatrixoption>F</ismatrixoption><onparentdelete>NO_ACTION</onparentdelete><label>Amount</label>'
           '</customrecordcustomfield></customrecordcustomfields><description></description></customrecordtype>')
    PROD = ('<customrecordtype scriptid="customrecord_x"><description>filled in</description><customrecordcustomfields>'
            '<customrecordcustomfield scriptid="custrecord_amount"><label>Amount</label><applyformatting>T</applyformatting>'
            '<onparentdelete></onparentdelete><enabledle>T</enabledle></customrecordcustomfield>'
            '</customrecordcustomfields></customrecordtype>')

    def test_netsuites_own_changes_are_ignored(self):
        self.assertEqual(sdf.sdf_compare.object_differences(self.GIT, self.PROD), [])

    def test_real_changes_are_named(self):
        prod = self.PROD.replace("<label>Amount</label>", "<label>Total</label>")
        self.assertEqual(sdf.sdf_compare.object_differences(self.GIT, prod),
                         ["customrecordtype/customrecordcustomfields/customrecordcustomfield[custrecord_amount]/label: "
                          "git='Amount' production='Total'"])
        extra = self.PROD.replace("</customrecordcustomfields>", '<customrecordcustomfield scriptid="custrecord_ui">'
                                  '<label>Made in the UI</label></customrecordcustomfield></customrecordcustomfields>')
        self.assertTrue(any("custrecord_ui" in d and "only in production" in d
                            for d in sdf.sdf_compare.object_differences(self.GIT, extra)))

    def test_repo_ignore_rules(self):
        prod = self.PROD.replace("<label>Amount</label>", "<label>Total</label>")
        self.assertEqual(sdf.sdf_compare.object_differences(self.GIT, prod, ["custrecord_amount/label"]), [])
        self.assertEqual(sdf.sdf_compare.object_differences(self.GIT, prod, ["label"]), [])
        self.assertTrue(sdf.sdf_compare.object_differences(self.GIT, prod, ["custrecord_other/label"]))

    def test_unreadable_xml_compares_as_text(self):
        self.assertTrue(sdf.sdf_compare.same_object("not xml", "not xml\n"))
        self.assertFalse(sdf.sdf_compare.same_object("not xml", "<x/>"))


class ConfigTest(unittest.TestCase):
    def test_reads_sdf_toml_and_requires_auth_ids(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(sdf, "CONFIG", sdf.CONFIG), \
                mock.patch.object(sdf, "ROOT", sdf.ROOT):
            Path(tmp, "sdf.toml").write_text(SDF_TOML)
            self.assertEqual(sdf.configure(tmp)["prod"]["auth_id"], "acme-prod")
            self.assertEqual(sdf.find_root(Path(tmp) / "src" / "x"), Path(tmp).resolve())
            Path(tmp, "sdf.toml").write_text('requires = "1.39.0"\n[sb]\nauth_id = "x"\nenv_prefix = "X_"\n')
            self.assertIn("missing [prod] auth_id, [prod] env_prefix", refuses(self, sdf.configure, tmp))
            Path(tmp, "sdf.toml").unlink()
            self.assertIn("no sdf.toml", refuses(self, sdf.configure, tmp))

    def test_an_older_work_kit_refuses(self):
        with mock.patch.dict(sdf.CONFIG, {"requires": "99.0.0"}):
            self.assertIn("needs work-kit 99.0.0 or newer", sdf.kit_refusal())
        with mock.patch.dict(sdf.CONFIG, {"requires": sdf.kit_version()}):
            self.assertIsNone(sdf.kit_refusal())

    def test_account_check_reads_the_repos_env(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"NETSUITE_ENV_FILE": str(Path(tmp, ".env"))}):
            Path(tmp, ".env").write_text("ACME_PROD_ACCOUNT_ID=1234567\nACME_SB_ACCOUNT_ID=1234567_SB1\n")
            with mock.patch.object(sdf, "auth_info", return_value={"account": "1234567"}):
                self.assertEqual(sdf.check_env("prod"), "acme-prod")
                self.assertIn("not a sandbox", refuses(self, sdf.check_env, "sb"))
            with mock.patch.object(sdf, "auth_info", return_value={"account": "7654321"}):
                self.assertIn(".env expects 1234567", refuses(self, sdf.check_env, "prod"))
            with mock.patch.object(sdf, "auth_info", return_value={"account": "1234567_SB1"}):
                self.assertIn("expected production", refuses(self, sdf.check_env, "prod"))
                self.assertEqual(sdf.check_env("sb"), "acme-sb")


class SandboxObjectChangeTest(GitRepoTest):
    def test_objects_changed_against_a_base(self):
        base = self.run_git("rev-parse", "HEAD")
        self.assertEqual(sdf.objects_changed(base), [])
        self.commit("src/Objects/customscript_acme_x.xml", "<x/>", "Record")
        self.assertEqual(sdf.objects_changed(base), ["src/Objects/customscript_acme_x.xml"])


class ChangeOwnersTest(GitRepoTest):
    JS = "src/FileCabinet/SuiteScripts/ACME/a.js"

    def test_a_pr_merge_stands_for_its_commits_and_a_direct_commit_for_itself(self):
        start = self.run_git("rev-parse", "HEAD")
        self.merge_pr(7, (self.JS, "2"))
        direct = self.commit(self.JS, "3", "quick fix on main")
        self.assertEqual(sdf.change_owners([f"{start}..HEAD"], self.JS), (["PR #7"], [direct]))

    def test_a_local_merge_naming_its_pr_counts(self):
        start = self.run_git("rev-parse", "HEAD")
        self.run_git("checkout", "-q", "-b", "fix")
        self.commit(self.JS, "2", "Fix")
        self.run_git("checkout", "-q", "main")
        self.run_git("merge", "-q", "--no-ff", "-m", "Reports: merge PR #97 (audit fixes)", "fix")
        self.assertEqual(sdf.change_owners([f"{start}..HEAD"], self.JS), (["PR #97"], []))

    def test_main_merged_into_a_branch_is_followed_to_its_prs(self):
        start = self.run_git("rev-parse", "HEAD")
        self.run_git("checkout", "-q", "-b", "work")
        mine = self.commit("src/FileCabinet/SuiteScripts/ACME/b.js", "b", "My change")
        self.run_git("checkout", "-q", "main")
        self.merge_pr(8, (self.JS, "2"))
        self.run_git("checkout", "-q", "work")
        self.run_git("merge", "-q", "--no-ff", "-m", "Merge branch 'main' into work", "main")
        self.assertEqual(sdf.change_owners([f"{start}..HEAD"], self.JS), (["PR #8"], []))
        own = {self.run_git("rev-parse", "HEAD~1"): "PR #9"}   # the branch's own commit, previewed as PR #9
        self.assertEqual(sdf.change_owners([f"{start}..HEAD"], "src/FileCabinet/SuiteScripts/ACME/b.js", own), (["PR #9"], []))
        self.assertEqual(sdf.change_owners([f"{start}..HEAD"], "src/FileCabinet/SuiteScripts/ACME/b.js"), ([], [mine]))


class DeployScopeTest(GitRepoTest):
    """A deploy sends only what src/deploy.xml matches; VENDOR files replaced in place go out by upload."""
    DEPLOY_XML = ("<deploy>\n    <files>\n        <path>~/FileCabinet/SuiteScripts/ACME/*</path>\n    </files>\n"
                  "    <objects>\n        <path>~/Objects/*</path>\n    </objects>\n</deploy>\n")
    JS = "src/FileCabinet/SuiteScripts/ACME/a.js"
    IC_XML = "src/Objects/customscript_acme_ic.xml"
    VENDOR = "src/FileCabinet/SuiteScripts/VENDOR - R1 & R2 - P&L/lib/vendor_lib.js"

    def setUp(self):
        super().setUp()
        self.commit("src/deploy.xml", self.DEPLOY_XML, "deploy.xml")
        self.commit(self.VENDOR, "vendor v1", "ACME-006: copy VENDOR's P&L lib from the account")

    def test_deploy_patterns(self):
        patterns = sdf.deploy_patterns()
        for path in (self.JS, "src/FileCabinet/SuiteScripts/ACME/x/y/z.html", self.IC_XML, "src/manifest.xml", "src/deploy.xml"):
            self.assertTrue(sdf.is_deployed(path, patterns), path)
        for path in (self.VENDOR, "src/FileCabinet/SuiteScripts/ACMEX/a.js", "src/FileCabinet/SuiteScripts/a.js"):
            self.assertFalse(sdf.is_deployed(path, patterns), path)
        self.commit("src/deploy.xml", None, "drop deploy.xml")
        self.assertIsNone(sdf.deploy_patterns())  # no deploy.xml: everything counts

class TagTest(GitRepoTest):
    def test_a_release_is_tagged_by_pr_with_a_suffix_when_taken(self):
        origin = Path(self.tmp.name) / "origin.git"
        self.run_git("init", "-q", "--bare", str(origin))
        self.run_git("remote", "add", "origin", str(origin))
        self.run_git("config", "user.name", "t")
        self.run_git("config", "user.email", "t@t")
        with contextlib.redirect_stdout(io.StringIO()):
            sdf.tag_release(7, self.head(), "deploy", ["src/FileCabinet/SuiteScripts/ACME/a.js"])
            sdf.tag_release(7, self.head(), "upload", ["src/FileCabinet/SuiteScripts/ACME/a.js"])
        names = self.run_git("tag", "-l").split()
        self.assertEqual(len(names), 2)
        self.assertRegex(names[0], r"^prod-\d{4}-\d{2}-\d{2}-pr7$")
        self.assertEqual(names[1], names[0] + "-2")
        self.assertIn(f"PR #7 production deploy of {self.head()}", self.run_git("tag", "-l", "--format=%(contents)", names[0]))


class ReleaseAncestryTest(GitRepoTest):
    """Every prod-* tag must be in the history of the commit being released."""
    LIB = "src/FileCabinet/SuiteScripts/ACME/AllocationHealth/acme_lib_alloc_health.js"
    AP = "src/FileCabinet/SuiteScripts/ACME/ap/acme_ue_ap.js"

    def incident(self):
        """One change released from its branch; another then branches from main without it."""
        self.merge_pr(8, (self.LIB, "old"))
        self.release("prod-2026-10-09-pr8")
        self.run_git("checkout", "-q", "-b", "fix-x")
        self.commit(self.LIB, "fixed", "Retry check")
        self.release("prod-2026-10-11-pr12", owner="PR #12")
        fix = self.commit(self.LIB, "fixed again", "Retry check counts only failing records")
        self.release("prod-2026-10-11-pr12-2", owner="PR #12")
        self.run_git("checkout", "-q", "main")
        self.run_git("checkout", "-q", "-b", "ap-x")
        self.commit(self.AP, "ap", "AP capture")
        return fix

    def test_incident_refuses_and_names_the_commit_to_merge(self):
        fix = self.incident()
        self.assertEqual([t[0] for t in sdf.unmerged_releases()], ["prod-2026-10-11-pr12", "prod-2026-10-11-pr12-2"])
        msg = sdf.ancestry_refusal()
        self.assertIn("prod-2026-10-11-pr12 ", msg)
        self.assertIn("prod-2026-10-11-pr12-2", msg)
        self.assertIn(f"git merge {fix}    # PR #12", msg)
        self.assertEqual(msg.count("git merge "), 1)  # -2 contains the first; one merge covers both

    def test_an_old_ticket_tag_counts_too(self):
        self.run_git("checkout", "-q", "-b", "old")
        old = self.commit(self.LIB, "x", "ACME-012: change")
        self.run_git("tag", "-a", "prod-2026-10-11-ACME-012-3", "-m", f"ACME-012 production deploy of {old}")
        self.run_git("checkout", "-q", "main")
        self.assertEqual(sdf.unmerged_releases()[0][2], "ACME-012")
        self.assertIn(f"git merge {old}    # ACME-012", sdf.ancestry_refusal())

    def test_merging_the_released_commit_clears_it(self):
        fix = self.incident()
        self.run_git("merge", "-q", "--no-edit", fix)
        self.assertEqual(sdf.unmerged_releases(), [])
        self.assertIsNone(sdf.ancestry_refusal())

    def test_an_older_tag_counts_not_just_the_newest(self):
        self.incident()
        self.commit(self.AP, "ap2", "AP capture v2")
        self.release("prod-2026-10-11-pr17", owner="PR #17")  # newest tag is on this branch; PR #12's are still missing
        self.assertEqual([t[2] for t in sdf.unmerged_releases()], ["PR #12", "PR #12"])

    def test_divergent_releases_each_need_a_merge(self):
        tips = []
        for n in (12, 19):
            self.run_git("checkout", "-q", "-b", f"b{n}", "main")
            tips.append(self.commit(f"src/FileCabinet/SuiteScripts/ACME/f{n}.js", "x", "change"))
            self.release(f"prod-2026-10-11-pr{n}", owner=f"PR #{n}")
        self.run_git("checkout", "-q", "main")
        msg = sdf.ancestry_refusal()
        self.assertIn(f"git merge {tips[0]}    # PR #12", msg)
        self.assertIn(f"git merge {tips[1]}    # PR #19", msg)

    def test_no_tags_or_all_merged_passes(self):
        self.assertIsNone(sdf.ancestry_refusal())
        self.release("prod-2026-10-08-pr1")
        self.commit(self.AP, "ap", "AP capture")
        self.assertIsNone(sdf.ancestry_refusal())

    def test_fetch_failure_stops_a_release_but_not_a_dry_run(self):
        self.incident()  # no origin in this repo
        self.assertIn("could not fetch prod-* tags", refuses(self, sdf.check_release_ancestry, "deploy"))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            msg = sdf.check_release_ancestry("deploy", strict=False)
        self.assertIn("checking local tags only", out.getvalue())
        self.assertIn("prod-2026-10-11-pr12-2", msg)

    def run_main(self, *argv, engine=None):
        err, out = io.StringIO(), io.StringIO()
        engine = engine or self.engine
        with mock.patch.object(sys, "argv", ["sdf.py", *argv]), mock.patch.object(sdf, "suitecloud") as cli, \
                engine.patch(), contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            try:
                code = sdf.main()
            except SystemExit as e:
                code = e.code
        return code, out.getvalue() + err.getvalue(), cli

    def with_origin(self):
        """Run the incident with a bare origin holding the tags; an AP change is merged as PR #17 and approved
        (the gates pass), so the ancestry guard is what refuses."""
        self.incident()
        self.merge_pr(17, (self.AP, "ap v2"))
        origin = Path(self.tmp.name) / "origin.git"
        self.run_git("init", "-q", "--bare", str(origin))
        self.run_git("remote", "add", "origin", str(origin))
        self.run_git("push", "-q", "origin", "--all")
        self.run_git("push", "-q", "origin", "--tags")
        rev, head = self.run_git("rev-parse", "HEAD"), self.run_git("rev-parse", "HEAD^2")
        self.engine = FakeEngine(pr=merged_pr(rev, head))
        self.engine_upload = FakeEngine(pr=merged_pr(rev, head, "upload"))
        return rev[:7]

    def test_deploy_prod_refuses_before_contacting_netsuite(self):
        self.with_origin()
        self.run_git("tag", "-d", "prod-2026-10-11-pr12-2")  # only origin has it: the fetch must bring it back
        code, out, cli = self.run_main("deploy", "prod", "--pr", "17")
        self.assertEqual(code, 2)
        self.assertIn("production runs releases this commit does not contain", out)
        self.assertIn("prod-2026-10-11-pr12-2", out)
        cli.assert_not_called()

    def test_upload_prod_refuses_too(self):
        self.with_origin()
        path = "/SuiteScripts/ACME/ap/acme_ue_ap.js"
        with mock.patch.object(sdf, "upload_targets", return_value=[(path, self.root / self.AP)]), \
                mock.patch.object(sdf, "node_check"):
            code, out, cli = self.run_main("upload", "prod", "--pr", "17", "--paths", path, engine=self.engine_upload)
        self.assertEqual(code, 2)
        self.assertIn("prod-2026-10-11-pr12-2", out)
        cli.assert_not_called()

    def test_dry_run_reports_the_refusal(self):
        self.with_origin()
        code, out, cli = self.run_main("deploy", "prod", "--pr", "17", "--dryrun")
        self.assertEqual(code, 1)
        self.assertIn("a real deploy would REFUSE: production runs releases this commit does not contain", out)
        cli.assert_not_called()

    def test_dry_run_also_lists_the_gates_that_refuse(self):
        rev = self.with_origin()
        engine = FakeEngine(refuse=[("gate", "release")], pr=self.engine.pr)
        code, out, cli = self.run_main("deploy", "prod", "--pr", "17", "--dryrun", engine=engine)
        self.assertEqual(code, 1)
        self.assertIn("production runs releases this commit does not contain", out)
        self.assertIn("wk gate release: wk: refused", out)
        cli.assert_not_called()


class OnlyDeployXmlTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = Path(self.tmp.name)
        (self.src / "Objects").mkdir()
        (self.src / "Objects" / "customscript_acme_sl_x.xml").write_text(
            "<suitelet><scriptfile>[/SuiteScripts/ACME/PnL/acme_sl_x.js]</scriptfile></suitelet>", encoding="utf-8")
        (self.src / "Objects" / "customscript_vendor_y.xml").write_text(
            "<suitelet><scriptfile>[/SuiteScripts/VENDOR | X/y.js]</scriptfile></suitelet>", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_object_and_its_folder_only(self):
        xml = sdf.only_deploy_xml(["customscript_acme_sl_x"], self.src)
        self.assertIn("<path>~/FileCabinet/SuiteScripts/ACME/PnL/*</path>", xml)
        self.assertIn("<path>~/Objects/customscript_acme_sl_x.xml</path>", xml)
        self.assertNotIn("Objects/*", xml)

    def test_refuses_unknown_bad_or_non_acme(self):
        self.assertIn("no src/Objects", refuses(self, sdf.only_deploy_xml, ["customscript_acme_nope"], self.src))
        self.assertIn("IDs like", refuses(self, sdf.only_deploy_xml, ["../x"], self.src))
        self.assertIn("not under /SuiteScripts/ACME/", refuses(self, sdf.only_deploy_xml, ["customscript_vendor_y"], self.src))


class ChangeScopeTest(GitRepoTest):
    """A deploy sends only one change's own files and objects: the branch's (sandbox) or the PR's (production)."""
    PNL = "src/FileCabinet/SuiteScripts/ACME/PnL/acme_sl_pnl.js"
    PNL_XML = "src/Objects/customscript_acme_sl_pnl.xml"
    HEALTH = "src/FileCabinet/SuiteScripts/ACME/AllocationHealth/acme_lib_alloc_health.js"
    SHARED_SL = "src/FileCabinet/SuiteScripts/ACME/shared/acme_sl_shared.js"

    def setUp(self):
        super().setUp()
        self.commit("src/deploy.xml", "<deploy><files><path>~/FileCabinet/SuiteScripts/ACME/*</path></files>"
                    "<objects><path>~/Objects/*</path></objects></deploy>", "Project deploy.xml")
        self.commit(self.HEALTH, "health v1", "Health lib")
        self.base = self.run_git("rev-parse", "HEAD")
        self.run_git("checkout", "-q", "-b", "pnl")
        self.commit("src/manifest.xml", "<manifest/>", "Manifest dependency")
        self.commit(self.PNL, "pnl", "P&L suitelet")
        self.commit(self.PNL_XML, "<suitelet><scriptfile>[/SuiteScripts/ACME/PnL/acme_sl_pnl.js]</scriptfile></suitelet>", "Script record")

    def test_the_branchs_own_files_and_objects_only(self):
        self.assertEqual(sdf.changed_paths(self.base), [self.PNL, self.PNL_XML])   # not manifest.xml, not main's lib
        self.assertEqual(sdf.changed_paths("HEAD"), [])

    def test_a_script_records_file_comes_along_even_if_the_branch_did_not_change_it(self):
        self.run_git("checkout", "-q", "main")
        self.commit(self.SHARED_SL, "x", "Shared suitelet")
        base = self.run_git("rev-parse", "HEAD")
        self.commit("src/Objects/customscript_acme_sl_shared.xml",
                    "<suitelet><scriptfile>[/SuiteScripts/ACME/shared/acme_sl_shared.js]</scriptfile></suitelet>", "Deployment for AP roles")
        self.assertEqual(sdf.changed_paths(base), [self.SHARED_SL, "src/Objects/customscript_acme_sl_shared.xml"])

    def test_deletions_and_attributes_are_left_out(self):
        self.commit("src/FileCabinet/SuiteScripts/ACME/PnL/.attributes/acme_sl_pnl.js.attr.xml", "<a/>", "Attributes")
        self.commit("src/FileCabinet/SuiteScripts/ACME/PnL/old.js", "old", "Old file")
        self.commit("src/FileCabinet/SuiteScripts/ACME/PnL/old.js", None, "Remove old file")
        self.commit(self.HEALTH, None, "Remove main's lib")
        self.assertEqual(sdf.changed_paths(self.base), [self.PNL, self.PNL_XML])

    def test_a_file_touched_and_put_back_is_not_sent(self):
        self.commit(self.HEALTH, "health v1 + export hook", "Export hook in the lib")
        self.commit(self.HEALTH, "health v1", "Revert the lib change")
        self.assertEqual(sdf.changed_paths(self.base), [self.PNL, self.PNL_XML])
        self.commit(self.HEALTH, "health v1 + kept change", "Lib change, kept this time")
        self.assertIn(self.HEALTH, sdf.changed_paths(self.base))       # a net change counts again

    def test_main_merged_into_the_branch_is_not_the_branchs(self):
        self.run_git("checkout", "-q", "main")
        self.merge_pr(12, (self.HEALTH, "health v2"))
        self.run_git("checkout", "-q", "pnl")
        self.run_git("merge", "-q", "--no-ff", "--no-edit", "main")
        base = self.run_git("merge-base", "HEAD", "main")
        self.assertEqual(sdf.changed_paths(base), [self.PNL, self.PNL_XML])

    def test_a_pr_sends_what_its_merge_commit_changed(self):
        self.run_git("checkout", "-q", "main")
        self.run_git("merge", "-q", "--no-ff", "-m", "Merge pull request #19 from acme/pnl", "pnl")
        merge = self.run_git("rev-parse", "HEAD")
        self.merge_pr(20, (self.HEALTH, "health v2"))
        paths, head, own = sdf.pr_scope(19, {"state": "MERGED", "mergeCommit": {"oid": merge}})
        self.assertEqual((paths, head, own), ([self.PNL, self.PNL_XML], "HEAD", None))   # not PR #20's lib
        paths, _, _ = sdf.pr_scope(20, {"state": "MERGED", "mergeCommit": {"oid": self.run_git("rev-parse", "HEAD")}})
        self.assertEqual(paths, [self.HEALTH])
        self.merge_pr(21, ("docs/x.md", "docs only"))
        self.assertIn("nothing to deploy", refuses(self, sdf.pr_scope, 21, {"state": "MERGED", "mergeCommit": {"oid": self.run_git("rev-parse", "HEAD")}}))

    def test_a_pr_previewed_before_its_merge_goes_by_its_head(self):
        tip = self.run_git("rev-parse", "HEAD")
        self.run_git("checkout", "-q", "main")
        with mock.patch.object(sdf, "has_commit", side_effect=lambda rev: rev != "origin/main" and bool(rev)):
            paths, head, own = sdf.pr_scope(19, {"state": "OPEN", "headRefOid": tip})
        self.assertEqual((paths, head), ([self.PNL, self.PNL_XML], tip))
        self.assertEqual(set(own.values()), {"PR #19"})
        self.assertEqual(len(own), 3)   # the branch's three commits

    def test_deploy_xml_lists_exactly_the_paths(self):
        xml = sdf.paths_deploy_xml([self.PNL, self.PNL_XML])
        self.assertIn("<path>~/FileCabinet/SuiteScripts/ACME/PnL/acme_sl_pnl.js</path>", xml)
        self.assertIn("<path>~/Objects/customscript_acme_sl_pnl.xml</path>", xml)
        self.assertNotIn("*", xml)

    def test_files_only_and_mixed_sets(self):
        """NetSuite refuses a deploy.xml without <objects> ('The object field "objects" is missing');
        a files-only set goes by file:upload instead."""
        mixed = sdf.paths_deploy_xml([self.PNL, self.PNL_XML])
        self.assertIn("<files>", mixed)
        self.assertIn("<objects>", mixed)
        self.assertFalse(sdf.files_only([self.PNL, self.PNL_XML]))
        self.assertNotIn("<objects>", sdf.paths_deploy_xml([self.PNL]))   # so it must never reach project:deploy
        self.assertTrue(sdf.files_only([self.PNL]))
        self.assertTrue(sdf.files_only([]))

    def test_deploy_xml_puts_a_referenced_record_first(self):
        """A first deploy fails when a record refers to one deploy.xml lists after it; objects come after the
        objects they refer to."""
        res, month, item = "src/Objects/customrecord_acme_cl_result.xml", "src/Objects/customrecord_acme_cl_loanmonth.xml", "src/Objects/customrecord_acme_cl_item.xml"
        self.commit(item, "<customrecordtype scriptid='customrecord_acme_cl_item'/>", "item")
        self.commit(res, "<customrecordtype scriptid='customrecord_acme_cl_result'><selectrecordtype>[scriptid=customrecord_acme_cl_item]</selectrecordtype></customrecordtype>", "result")
        self.commit(month, "<customrecordtype scriptid='customrecord_acme_cl_loanmonth'><selectrecordtype>[scriptid=customrecord_acme_cl_result]</selectrecordtype></customrecordtype>", "sign-off")
        xml = sdf.paths_deploy_xml([month, res, item])
        order = [xml.index(p[4:]) for p in (item, res, month)]
        self.assertEqual(order, sorted(order), xml)

    def run_main(self, *argv, engine=None):
        err, out = io.StringIO(), io.StringIO()
        sent = {}
        self.engine = engine or FakeEngine()   # wk turn check passes: this branch holds the turn
        def cli(args, auth_id=None, **kw):
            sent["deploy.xml"] = (self.root / "src" / "deploy.xml").read_text(encoding="utf-8")
            return 0, ""
        with mock.patch.object(sys, "argv", ["sdf.py", *argv]), mock.patch.object(sdf, "suitecloud", side_effect=cli), \
                mock.patch.object(sdf, "check_env", return_value="acme-sb"), mock.patch.object(sdf, "SRC", self.root / "src"), \
                mock.patch.object(sdf, "tool_refusal", return_value=None), \
                mock.patch.object(sdf, "branch_base", return_value=self.base), self.engine.patch(), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            try:
                code = sdf.main()
            except SystemExit as e:
                code = e.code
        return code, out.getvalue() + err.getvalue(), sent

    def test_sandbox_deploy_sends_the_branchs_changes_and_puts_deploy_xml_back(self):
        before = (self.root / "src" / "deploy.xml").read_text()
        code, out, sent = self.run_main("deploy", "sb")
        self.assertEqual(code, 0, out)
        self.assertIn("<path>~/FileCabinet/SuiteScripts/ACME/PnL/acme_sl_pnl.js</path>", sent["deploy.xml"])
        self.assertNotIn("AllocationHealth", sent["deploy.xml"])
        self.assertIn("this branch's own 2 file(s) and object(s)", out)
        self.assertEqual((self.root / "src" / "deploy.xml").read_text(), before)

    def test_a_branch_with_nothing_to_deploy_refuses(self):
        self.base = self.run_git("rev-parse", "HEAD")
        code, out, sent = self.run_main("deploy", "sb")
        self.assertEqual(code, 2)
        self.assertIn("nothing to deploy", out)
        self.assertEqual(sent, {})

    def test_pr_is_for_production_and_ticket_is_gone(self):
        code, out, sent = self.run_main("deploy", "sb", "--pr", "7")
        self.assertEqual(code, 2)
        self.assertIn("--pr is for production", out)
        code, out, sent = self.run_main("deploy", "sb", "--ticket", "X")
        self.assertEqual(code, 2)
        self.assertIn("unrecognized arguments: --ticket", out)
        self.assertEqual(sent, {})

    def test_only_still_narrows_in_sandbox_and_is_refused_in_production(self):
        code, out, sent = self.run_main("deploy", "sb", "--only", "customscript_acme_sl_pnl")
        self.assertEqual(code, 0, out)
        self.assertIn("<path>~/FileCabinet/SuiteScripts/ACME/PnL/*</path>", sent["deploy.xml"])
        code, out, sent = self.run_main("deploy", "prod", "--pr", "19", "--only", "customscript_acme_sl_pnl")
        self.assertEqual(code, 2)
        self.assertIn("--only is for sandbox", out)
        self.assertEqual(sent, {})


class ToolVersionTest(GitRepoTest):
    """deploy and upload refuse while origin/main has a newer tools/sdf.py."""
    TOOL = "tools/sdf.py"

    def setUp(self):
        super().setUp()
        self.commit(self.TOOL, "v1", "Release guard")
        self.origin = Path(self.tmp.name) / "origin.git"
        self.run_git("init", "-q", "--bare", str(self.origin))
        self.run_git("remote", "add", "origin", str(self.origin))
        self.run_git("push", "-q", "origin", "main")
        self.run_git("checkout", "-q", "-b", "pnl")
        self.commit("src/FileCabinet/SuiteScripts/ACME/PnL/p.js", "p", "P&L")

    def advance_main(self):
        self.run_git("checkout", "-q", "main")
        self.commit(self.TOOL, "v2", "Deploys pick files by PR")
        self.run_git("push", "-q", "origin", "main")
        self.run_git("checkout", "-q", "pnl")

    def test_same_or_newer_tool_passes(self):
        self.assertIsNone(sdf.tool_refusal())
        self.commit(self.TOOL, "v1 + branch change", "Work in progress")   # ahead of main: allowed
        self.assertIsNone(sdf.tool_refusal())

    def test_older_tool_refuses_and_says_how_to_take_main(self):
        self.advance_main()
        msg = sdf.tool_refusal()
        self.assertIn("is older than main's", msg)
        self.assertIn("Deploys pick files by PR", msg)
        self.assertIn("git merge origin/main", msg)
        self.run_git("merge", "-q", "--no-edit", "origin/main")
        self.assertIsNone(sdf.tool_refusal())

    def test_other_changes_on_main_do_not_matter(self):
        self.run_git("checkout", "-q", "main")
        self.commit("docs/x.md", "x", "Docs")
        self.run_git("push", "-q", "origin", "main")
        self.run_git("checkout", "-q", "pnl")
        self.assertIsNone(sdf.tool_refusal())

    def test_no_origin_refuses(self):
        self.run_git("remote", "remove", "origin")
        self.assertIn("could not fetch origin/main", sdf.tool_refusal())

    def test_deploy_and_upload_refuse_before_anything_else(self):
        self.advance_main()
        for argv in (["deploy", "sb"], ["deploy", "prod", "--pr", "19", "--dryrun"],
                     ["upload", "sb", "--paths", "/SuiteScripts/ACME/PnL/p.js"]):
            err = io.StringIO()
            with mock.patch.object(sys, "argv", ["sdf.py", *argv]), mock.patch.object(sdf, "suitecloud") as cli, \
                    contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                sdf.main()
            self.assertIn("older than main's", err.getvalue(), argv)
            cli.assert_not_called()


class ProdFromMainTest(GitRepoTest):
    """Production receives deploys only after the merge, from main, for an approved PR."""
    PNL = "src/FileCabinet/SuiteScripts/ACME/PnL/acme_sl_pnl.js"
    LIB = "src/FileCabinet/SuiteScripts/ACME/PnL/acme_lib_pnl.js"

    def setUp(self):
        super().setUp()
        self.commit("src/deploy.xml", "<deploy><files><path>~/FileCabinet/SuiteScripts/ACME/*</path></files>"
                    "<objects><path>~/Objects/*</path></objects></deploy>", "Project deploy.xml")
        self.commit(self.LIB, "lib", "P&L lib")
        self.release("prod-2026-10-20-pr1")
        self.live(self.LIB, "lib")   # production runs main's lib
        self.origin = Path(self.tmp.name) / "origin.git"
        self.run_git("init", "-q", "--bare", str(self.origin))
        self.run_git("remote", "add", "origin", str(self.origin))
        self.run_git("checkout", "-q", "-b", "pnl")
        self.commit(self.PNL, "pnl", "P&L suitelet")
        self.pr_head = self.run_git("rev-parse", "HEAD")
        self.run_git("checkout", "-q", "main")
        self.run_git("merge", "-q", "--no-ff", "--no-edit", "-m", "Merge pull request #7 from acme/pnl", "pnl")
        self.merge = self.run_git("rev-parse", "HEAD")
        self.run_git("push", "-q", "origin", "main", "--tags")
        self.run_git("config", "user.name", "t")   # tag_release tags with the repo's identity
        self.run_git("config", "user.email", "t@t")

    def run_main(self, *argv, engine):
        err, out = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["sdf.py", *argv]), \
                mock.patch.object(sdf, "suitecloud", return_value=(0, "")) as cli, \
                mock.patch.object(sdf, "check_env", return_value="acme-prod"), mock.patch.object(sdf, "SRC", self.root / "src"), \
                engine.patch(), contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            try:
                code = sdf.main()
            except SystemExit as e:
                code = e.code
        return code, out.getvalue() + err.getvalue(), cli

    def deploy(self, engine, *extra):
        return self.run_main("deploy", "prod", *extra, engine=engine)

    def test_merged_and_approved_on_main_deploys_the_merge_and_tags_the_pr(self):
        engine = FakeEngine(pr=merged_pr(self.merge, self.pr_head))
        code, out, cli = self.deploy(engine, "--pr", "7")
        self.assertEqual(code, 0, out)
        self.assertIn(("gate", "release"), engine.calls)
        self.assertIn(("gate", "approved", "--pr", "7", "--action", "deploy to production"), engine.calls)
        self.assertIn("PR #7 (released PR)", out)
        self.assertIn(f"PR #7's changes: {self.PNL}", out)          # exactly what the merge changed, not the lib
        cli.assert_called_once()
        self.assertEqual(cli.call_args[0][0], ["file:upload", "--paths", "/SuiteScripts/ACME/PnL/acme_sl_pnl.js"])
        tag = self.run_git("tag", "-l", "prod-*-pr7")
        self.assertRegex(tag, r"^prod-\d{4}-\d{2}-\d{2}-pr7$")
        body = self.run_git("tag", "-l", "--format=%(contents)", tag)
        self.assertIn("PR #7 production deploy of", body)
        self.assertIn(f"file: {self.PNL}", body)
        self.assertIn(tag, subprocess.run(["git", "ls-remote", "--tags", str(self.origin)],
                                          capture_output=True, text=True).stdout)

    def test_the_approval_names_the_pr_head_not_the_merge_commit(self):
        """After the merge main's HEAD is the merge commit; the approval still names the PR's head (wk gate approved
        checks it against headRefOid), and sdf.py checks that the merge is in HEAD."""
        self.assertNotEqual(self.merge, self.pr_head)
        code, out, _ = self.deploy(FakeEngine(pr=merged_pr(self.merge, self.pr_head)), "--pr", "7")
        self.assertEqual(code, 0, out)

    def test_no_pr_refuses(self):
        code, out, cli = self.deploy(FakeEngine())
        self.assertEqual(code, 2)
        self.assertIn("--pr is needed", out)
        cli.assert_not_called()

    def test_off_main_or_not_level_refuses(self):
        engine = FakeEngine(refuse=[("gate", "release")], pr=merged_pr(self.merge, self.pr_head))
        code, out, cli = self.deploy(engine, "--pr", "7")
        self.assertEqual(code, 2)
        self.assertIn("Production releases run after the merge, from main", out)
        cli.assert_not_called()

    def test_an_open_pr_refuses(self):
        pr = merged_pr(self.merge, self.pr_head)
        pr["state"] = "OPEN"
        code, out, cli = self.deploy(FakeEngine(pr=pr), "--pr", "7")
        self.assertEqual(code, 2)
        self.assertIn("PR #7 is open, not merged", out)
        cli.assert_not_called()

    def test_a_merge_not_yet_pulled_refuses(self):
        self.run_git("checkout", "-q", "-b", "later", "pnl")
        self.commit(self.PNL, "pnl v2", "v2")
        later = self.run_git("rev-parse", "HEAD")
        self.run_git("checkout", "-q", "main")
        code, out, cli = self.deploy(FakeEngine(pr=merged_pr(later, later)), "--pr", "7")
        self.assertEqual(code, 2)
        self.assertIn("is not in this checkout's main; pull main first", out)
        cli.assert_not_called()

    def test_no_approval_for_the_pr_head_refuses(self):
        engine = FakeEngine(refuse=[("gate", "approved")], pr=merged_pr(self.merge, self.pr_head))
        code, out, cli = self.deploy(engine, "--pr", "7")
        self.assertEqual(code, 2)
        self.assertIn("wk gate approved --pr 7 --action deploy to production: wk: refused", out)
        cli.assert_not_called()

    def test_upload_asks_for_the_upload_approval(self):
        engine = FakeEngine(pr=merged_pr(self.merge, self.pr_head, "upload"))
        with mock.patch.object(sdf, "node_check"), mock.patch.object(sdf, "CABINET", self.root / "src" / "FileCabinet"):
            code, out, cli = self.run_main("upload", "prod", "--pr", "7",
                                           "--paths", "/SuiteScripts/ACME/PnL/acme_sl_pnl.js", engine=engine)
        self.assertEqual(code, 0, out)
        self.assertIn(("gate", "approved", "--pr", "7", "--action", "upload to production"), engine.calls)
        cli.assert_called_once()
        self.assertTrue(self.run_git("tag", "-l", "prod-*-pr7"))

    def test_another_prs_change_in_a_sent_file_needs_naming_on_the_approval_comment(self):
        self.run_git("checkout", "-q", "-b", "v2")
        self.commit(self.PNL, "pnl v2", "P&L v2")
        head = self.run_git("rev-parse", "HEAD")
        self.run_git("checkout", "-q", "main")
        self.run_git("merge", "-q", "--no-ff", "-m", "Merge pull request #8 from acme/v2", "v2")
        merge = self.run_git("rev-parse", "HEAD")
        code, out, cli = self.deploy(FakeEngine(pr=merged_pr(merge, head)), "--pr", "8")
        self.assertEqual(code, 2)
        self.assertIn(f"PR #7: {self.PNL}", out)                     # PR #7's change to the file is not released yet
        self.assertIn("adds 'Includes PR #7' to the approval comment on PR #8", out)
        cli.assert_not_called()
        code, out, cli = self.deploy(FakeEngine(pr=merged_pr(merge, head, extra="Includes PR #7 (first version)")), "--pr", "8")
        self.assertEqual(code, 0, out)
        self.assertIn("PR #7 (named on PR #8's approval)", out)

    def test_only_the_comment_wk_accepted_counts(self):
        """Another commenter's look-alike approval, or a quote of the line with an 'Includes', widens nothing: only the
        comment wk gate approved accepted (the configured approver's line at the PR head) is read."""
        self.run_git("checkout", "-q", "-b", "v2")
        self.commit(self.PNL, "pnl v2", "P&L v2")
        head = self.run_git("rev-parse", "HEAD")
        self.run_git("checkout", "-q", "main")
        self.run_git("merge", "-q", "--no-ff", "-m", "Merge pull request #8 from acme/v2", "v2")
        pr = merged_pr(self.run_git("rev-parse", "HEAD"), head)
        pr["comments"].append({"body": f"Approved by Bob: deploy to production @ {head[:7]}\nIncludes PR #7",
                               "url": "https://x/pull/7#c3"})
        pr["comments"].append({"body": f"> Approved by Owner: deploy to production @ {head[:7]}\nIncludes PR #7",
                               "url": "https://x/pull/7#c4"})
        code, out, cli = self.deploy(FakeEngine(pr=pr), "--pr", "8")
        self.assertEqual(code, 2)
        self.assertIn("PR #7 (NOT COVERED)", out)
        cli.assert_not_called()
        self.assertEqual(sdf.approval_comment(pr, json.dumps({"comment": "https://x/pull/7#c2"})), pr["comments"][1]["body"])
        self.assertEqual(sdf.approval_comment(pr, "not json"), "")

    def test_a_squash_or_rebase_merge_refuses(self):
        self.run_git("checkout", "-q", "-b", "squashed")
        self.commit(self.PNL, "pnl v2", "P&L v2")
        head = self.run_git("rev-parse", "HEAD")
        self.run_git("checkout", "-q", "main")
        self.commit(self.LIB, "lib v2", "Earlier commit of the PR (rebase merge)")
        self.commit(self.PNL, "pnl v2", "P&L v2 (#8)")   # one parent: what GitHub writes for a squash or rebase
        single = self.run_git("rev-parse", "HEAD")
        for extra in ((), ("--dryrun",)):
            code, out, cli = self.deploy(FakeEngine(pr=merged_pr(single, head)), "--pr", "8", *extra)
            self.assertEqual(code, 2, out)
            self.assertIn("was not merged with a merge commit", out)
            cli.assert_not_called()
        self.assertIsNone(sdf.not_a_merge_commit(7, self.merge))

    def test_production_already_running_the_other_prs_change_needs_no_naming(self):
        """The release guard asked again for work already live; the compare reads production instead."""
        self.run_git("checkout", "-q", "-b", "v2")
        self.commit(self.PNL, "pnl v2", "P&L v2")
        head = self.run_git("rev-parse", "HEAD")
        self.run_git("checkout", "-q", "main")
        self.run_git("merge", "-q", "--no-ff", "-m", "Merge pull request #8 from acme/v2", "v2")
        self.live(self.PNL, "pnl")   # PR #7 went out already
        code, out, cli = self.deploy(FakeEngine(pr=merged_pr(self.run_git("rev-parse", "HEAD"), head)), "--pr", "8")
        self.assertEqual(code, 0, out)
        self.assertIn("Needs approval: PR #8 (released PR)", out)
        self.assertEqual(self.compared, [[self.PNL]])
        cli.assert_called_once()

    def test_production_holding_a_change_git_lacks_refuses_and_sends_nothing(self):
        self.live(self.PNL, "edited in production")
        for extra in ((), ("--dryrun",)):
            code, out, cli = self.deploy(FakeEngine(pr=merged_pr(self.merge, self.pr_head)), "--pr", "7", *extra)
            self.assertNotEqual(code, 0, out)
            self.assertIn("production holds a change git doesn't have", out)
            cli.assert_not_called()
        self.assertFalse(self.run_git("tag", "-l", "prod-*-pr7"))

    def test_dry_run_passes_when_everything_does(self):
        code, out, cli = self.deploy(FakeEngine(pr=merged_pr(self.merge, self.pr_head)), "--pr", "7", "--dryrun")
        self.assertEqual(code, 0, out)
        self.assertIn("a real deploy would pass the gates and the production compare", out)
        cli.assert_not_called()

    def test_dry_run_before_the_merge_previews_the_prs_head(self):
        self.run_git("checkout", "-q", "-b", "next", "main")
        self.commit(self.LIB, "lib v2", "Lib v2")
        head = self.run_git("rev-parse", "HEAD")
        pr = {"state": "OPEN", "headRefOid": head, "comments": []}
        code, out, cli = self.deploy(FakeEngine(pr=pr), "--pr", "9", "--dryrun")
        self.assertEqual(code, 1)
        self.assertIn(f"{self.LIB}   PR #9   (production has git's version of", out)
        self.assertIn("PR #9 is open, not merged", out)
        cli.assert_not_called()


class SandboxTurnTest(GitRepoTest):
    """A sandbox deploy or upload sends only files this branch holds a turn on."""
    PNL = "src/FileCabinet/SuiteScripts/ACME/PnL/acme_sl_pnl.js"
    PNL_LIB = "src/FileCabinet/SuiteScripts/ACME/PnL/acme_lib_pnl.js"
    PNL_XML = "src/Objects/customscript_acme_sl_pnl.xml"

    def setUp(self):
        super().setUp()
        self.commit("src/deploy.xml", "<deploy><files><path>~/FileCabinet/SuiteScripts/ACME/*</path></files>"
                    "<objects><path>~/Objects/*</path></objects></deploy>", "Project deploy.xml")
        self.commit(self.PNL_LIB, "lib", "P&L lib")
        self.base = self.run_git("rev-parse", "HEAD")
        self.run_git("checkout", "-q", "-b", "pnl")
        self.commit(self.PNL, "pnl", "P&L suitelet")
        self.commit(self.PNL_XML, "<suitelet><scriptfile>[/SuiteScripts/ACME/PnL/acme_sl_pnl.js]</scriptfile></suitelet>",
                    "Record")
        self.commit("src/FileCabinet/SuiteScripts/ACME/PnL/.attributes/acme_sl_pnl.js.attr.xml", "<a/>", "Attributes")

    def run_main(self, *argv, engine, base=None):
        err, out = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["sdf.py", *argv]), \
                mock.patch.object(sdf, "suitecloud", return_value=(0, "")) as cli, \
                mock.patch.object(sdf, "check_env", return_value="acme-sb"), mock.patch.object(sdf, "SRC", self.root / "src"), \
                mock.patch.object(sdf, "CABINET", self.root / "src" / "FileCabinet"), \
                mock.patch.object(sdf, "tool_refusal", return_value=None), \
                mock.patch.object(sdf, "branch_base", return_value=base or self.base), \
                mock.patch.object(sdf, "node_check"), engine.patch(), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            try:
                code = sdf.main()
            except SystemExit as e:
                code = e.code
        return code, out.getvalue() + err.getvalue(), cli

    def checks(self, engine):
        return [c[3:] for c in engine.calls if c[:3] == ("turn", "check", "sandbox")]

    def test_deploy_checks_a_turn_on_exactly_the_files_it_sends(self):
        engine = FakeEngine()
        code, out, cli = self.run_main("deploy", "sb", engine=engine)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.checks(engine), [(self.PNL, self.PNL_XML)])
        cli.assert_called_once()
        self.assertEqual(cli.call_args[0][0], ["project:deploy", "--validate"])   # files and objects: a project deploy

    def test_a_files_only_deploy_goes_by_upload(self):
        """A project deploy without <objects> is refused by NetSuite."""
        self.run_git("checkout", "-q", "-b", "lib-only", self.base)
        self.commit(self.PNL_LIB, "lib v2", "Lib v2")
        engine = FakeEngine()
        code, out, cli = self.run_main("deploy", "sb", engine=engine)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.checks(engine), [(self.PNL_LIB,)])
        self.assertEqual(cli.call_args[0][0], ["file:upload", "--paths", "/SuiteScripts/ACME/PnL/acme_lib_pnl.js"])
        code, out, cli = self.run_main("deploy", "sb", "--dryrun", engine=FakeEngine())
        self.assertEqual(code, 0, out)
        self.assertIn("files only, so a real deploy uploads them", out)
        cli.assert_not_called()

    def test_without_the_turn_nothing_is_sent_and_it_says_how_to_take_it(self):
        engine = FakeEngine(refuse=[("turn", "check")])
        code, out, cli = self.run_main("deploy", "sb", engine=engine)
        self.assertEqual(code, 2)
        self.assertIn(f"  wk turn take sandbox {self.PNL} {self.PNL_XML}", out)
        self.assertIn("py tools/sdf.py release sb", out)
        cli.assert_not_called()

    def test_only_checks_the_record_and_every_file_in_its_folder(self):
        engine = FakeEngine()
        code, out, _ = self.run_main("deploy", "sb", "--only", "customscript_acme_sl_pnl", engine=engine)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.checks(engine), [(self.PNL_LIB, self.PNL, self.PNL_XML)])   # main's lib goes too

    def test_upload_checks_its_files(self):
        argv = ("upload", "sb", "--paths", "/SuiteScripts/ACME/PnL/acme_sl_pnl.js")
        engine = FakeEngine()
        code, out, cli = self.run_main(*argv, engine=engine, base="HEAD")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.checks(engine), [(self.PNL,)])
        code, out, cli = self.run_main(*argv, engine=FakeEngine(refuse=[("turn", "check")]), base="HEAD")
        self.assertEqual(code, 2)
        cli.assert_not_called()
        code, out, cli = self.run_main(*argv, engine=FakeEngine())   # the branch changed an object
        self.assertEqual(code, 2)
        self.assertIn("That is an object change; use deploy", out)

    def test_a_dry_run_changes_nothing_so_needs_no_turn(self):
        engine = FakeEngine(refuse=[("turn", "check")])
        code, out, cli = self.run_main("deploy", "sb", "--dryrun", engine=engine)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.checks(engine), [])

    def test_paths_with_spaces_are_quoted_in_the_hint(self):
        self.assertEqual(sdf.quoted(["src/a.js", "src/FileCabinet/SuiteScripts/VENDOR | X & Y/y.js"]),
                         'src/a.js "src/FileCabinet/SuiteScripts/VENDOR | X & Y/y.js"')


class WkCommandTest(unittest.TestCase):
    """sdf.py always runs work-kit's wk as `python -m wk` from the work-kit checkout: the one on PATH, else
    C:/Projects/work-kit."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.checkout("work-kit", work_kit=True)

    def tearDown(self):
        self.tmp.cleanup()

    def checkout(self, name, work_kit):
        home = Path(self.tmp.name) / name
        (home / "bin").mkdir(parents=True)
        (home / "bin" / "wk.cmd").write_text("@echo off\n")
        if work_kit:
            (home / "wk").mkdir()
            (home / "wk" / "__main__.py").write_text("")
            (home / "wk" / "turns.py").write_text("")
        else:   # the old command center: a bin/wk.cmd that runs tools/wk.py
            (home / "tools").mkdir()
            (home / "tools" / "wk.py").write_text("")
        return home

    def test_the_work_kit_on_path_runs_as_python_module(self):
        with mock.patch.object(sdf.shutil, "which", return_value=str(self.home / "bin" / "wk.cmd")), \
                mock.patch.object(sdf, "WORK_KIT", Path(self.tmp.name) / "nope"):
            cmd, env = sdf.wk_command()
        self.assertEqual(cmd, [sys.executable, "-m", "wk"])
        self.assertEqual(env["PYTHONPATH"].split(os.pathsep)[0], str(self.home.resolve()))

    def test_another_wk_on_path_is_never_run(self):
        """A wk.cmd on PATH that is not work-kit (the old command center's) is skipped, never run through cmd.exe."""
        old = self.checkout("command-center", work_kit=False)
        with mock.patch.object(sdf.shutil, "which", return_value=str(old / "bin" / "wk.cmd")), \
                mock.patch.object(sdf, "WORK_KIT", self.home):
            cmd, env = sdf.wk_command()
        self.assertEqual(cmd, [sys.executable, "-m", "wk"])
        self.assertEqual(env["PYTHONPATH"].split(os.pathsep)[0], str(self.home))
        with mock.patch.object(sdf.shutil, "which", return_value=str(old / "bin" / "wk.cmd")), \
                mock.patch.object(sdf, "WORK_KIT", Path(self.tmp.name) / "nope"):
            self.assertIn("work-kit was not found", refuses(self, sdf.wk_command))

    def test_the_default_checkout_when_not_on_path(self):
        with mock.patch.object(sdf.shutil, "which", return_value=None), mock.patch.object(sdf, "WORK_KIT", self.home):
            self.assertEqual(sdf.wk_command()[0], [sys.executable, "-m", "wk"])

    def test_missing_work_kit_fails_clearly(self):
        with mock.patch.object(sdf.shutil, "which", return_value=None), \
                mock.patch.object(sdf, "WORK_KIT", Path(self.tmp.name) / "nope"):
            msg = refuses(self, sdf.wk, "turn", "list")
        self.assertIn("work-kit was not found", msg)


class ReleaseSandboxTest(GitRepoTest):
    """release sb puts main's version of the turn's files back in the sandbox, then frees the turn."""
    LIB = "src/FileCabinet/SuiteScripts/ACME/PnL/acme_lib_pnl.js"
    XML = "src/Objects/customscript_acme_sl_pnl.xml"
    NEW = "src/FileCabinet/SuiteScripts/ACME/PnL/acme_new.js"
    VENDOR = "src/FileCabinet/SuiteScripts/VENDOR | X/y.js"   # held as its File Cabinet path; stored locally as 'VENDOR - X'

    def setUp(self):
        super().setUp()
        self.commit("src/deploy.xml", "<deploy><files><path>~/FileCabinet/SuiteScripts/ACME/*</path></files>"
                    "<objects><path>~/Objects/*</path></objects></deploy>", "Project deploy.xml")
        self.commit(self.LIB, "lib main", "Lib")
        self.commit(self.XML, "<suitelet>main</suitelet>", "Record")
        self.commit("src/FileCabinet/SuiteScripts/VENDOR - X/y.js", "vendor main", "VENDOR copy")
        self.origin = Path(self.tmp.name) / "origin.git"
        self.run_git("init", "-q", "--bare", str(self.origin))
        self.run_git("remote", "add", "origin", str(self.origin))
        self.run_git("push", "-q", "origin", "main")
        self.run_git("checkout", "-q", "-b", "pnl")
        self.commit(self.LIB, "lib branch", "Lib change")
        self.commit(self.NEW, "new", "New file")

    def run_release(self, engine, code=0, *argv):
        err, out = io.StringIO(), io.StringIO()
        sent = []

        def cli(args, auth_id=None, cwd=None, **kw):
            sent.append({"args": args, "cwd": cwd, "deploy.xml": (Path(cwd) / "src" / "deploy.xml").read_text(encoding="utf-8"),
                         "lib": (Path(cwd) / self.LIB).read_text(), "head": self.run_git("-C", str(cwd), "rev-parse", "HEAD")})
            return code, ""
        with mock.patch.object(sys, "argv", ["sdf.py", "release", "sb", *argv]), \
                mock.patch.object(sdf, "suitecloud", side_effect=cli), \
                mock.patch.object(sdf, "check_env", return_value="acme-sb"), mock.patch.object(sdf, "import_env", return_value={}), \
                engine.patch(), contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            try:
                rc = sdf.main()
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue() + err.getvalue(), sent

    def held(self, *items, holder="pnl"):
        return FakeEngine(turns=[{"resource": "sandbox", "holder": holder, "items": list(items), "state": "1h00m left"}])

    def test_main_versions_go_back_then_the_turn_is_freed(self):
        engine = self.held(self.LIB, self.XML, self.NEW, self.VENDOR)
        rc, out, sent = self.run_release(engine)
        self.assertEqual(rc, 0, out)
        deploy, upload = sent
        self.assertEqual(deploy["args"], ["project:deploy", "--validate"])
        self.assertEqual(deploy["head"], self.run_git("rev-parse", "origin/main"))   # a checkout of main
        self.assertEqual(deploy["lib"], "lib main")                                    # main's version, not the branch's
        self.assertIn("<path>~/FileCabinet/SuiteScripts/ACME/PnL/acme_lib_pnl.js</path>", deploy["deploy.xml"])
        self.assertIn("<path>~/Objects/customscript_acme_sl_pnl.xml</path>", deploy["deploy.xml"])
        self.assertNotIn("acme_new.js", deploy["deploy.xml"])
        self.assertEqual(upload["args"], ["file:upload", "--paths", "/SuiteScripts/VENDOR | X/y.js"])
        self.assertIn(f"not on main, stays in the sandbox as this branch sent it (remove by hand if it must go): {self.NEW}", out)
        self.assertIn(("turn", "release", "sandbox", self.LIB, self.XML, self.NEW, self.VENDOR), engine.calls)
        self.assertFalse(Path(deploy["cwd"]).exists())                                 # the checkout of main is gone
        self.assertEqual(self.run_git("worktree", "list").count("\n"), 0)
        self.assertEqual((self.root / self.LIB).read_text(), "lib branch")             # this checkout is untouched

    def test_a_files_only_turn_goes_back_by_upload(self):
        """A turn of files only goes back by upload: a project deploy without objects is refused."""
        engine = self.held(self.LIB, self.VENDOR)
        rc, out, sent = self.run_release(engine)
        self.assertEqual(rc, 0, out)
        self.assertEqual([s["args"] for s in sent],
                         [["file:upload", "--paths", "/SuiteScripts/ACME/PnL/acme_lib_pnl.js", "/SuiteScripts/VENDOR | X/y.js"]])
        self.assertEqual(sent[0]["lib"], "lib main")                                   # from the checkout of main
        self.assertIn("  upload /SuiteScripts/ACME/PnL/acme_lib_pnl.js", out)
        self.assertIn(("turn", "release", "sandbox", self.LIB, self.VENDOR), engine.calls)

    def test_a_refused_deploy_keeps_the_turn(self):
        engine = self.held(self.LIB)
        rc, out, sent = self.run_release(engine, 1)
        self.assertEqual(rc, 2)
        self.assertIn("the turn is kept", out)
        self.assertFalse([c for c in engine.calls if c[:2] == ("turn", "release")])

    def test_only_this_branchs_turns(self):
        rc, out, sent = self.run_release(self.held(self.LIB, holder="other-branch"))
        self.assertEqual(rc, 2)
        self.assertIn("holds no sandbox turn", out)
        self.assertEqual(sent, [])

    def test_dry_run_sends_nothing_and_keeps_the_turn(self):
        engine = self.held(self.LIB, self.NEW)
        rc, out, sent = self.run_release(engine, 0, "--dryrun")
        self.assertEqual(rc, 0, out)
        self.assertEqual(sent, [])
        self.assertIn("upload /SuiteScripts/ACME/PnL/acme_lib_pnl.js", out)   # files only: by upload
        self.assertFalse([c for c in engine.calls if c[:2] == ("turn", "release")])

    def test_files_only_on_the_branch_need_no_netsuite_call(self):
        engine = self.held(self.NEW)
        rc, out, sent = self.run_release(engine)
        self.assertEqual(rc, 0, out)
        self.assertEqual(sent, [])
        self.assertIn(("turn", "release", "sandbox", self.NEW), engine.calls)


def work_kit_found():
    found = shutil.which("wk")
    return any(sdf.is_work_kit(h) for h in ([Path(found).resolve().parent.parent] if found else []) + [sdf.WORK_KIT])


@unittest.skipUnless(work_kit_found(), "work-kit is not on this machine (CI)")
class RealEngineTest(GitRepoTest):
    """sdf.py against work-kit's real wk in a throwaway repo: turns are per file and per branch."""

    def setUp(self):
        super().setUp()
        origin = Path(self.tmp.name) / "origin.git"
        self.run_git("init", "-q", "--bare", str(origin))
        self.run_git("remote", "add", "origin", str(origin))
        self.run_git("push", "-q", "origin", "main")
        self.run_git("checkout", "-q", "-b", "pnl")

    def test_take_check_list_release(self):
        a, b = "src/FileCabinet/SuiteScripts/ACME/a.js", "src/FileCabinet/SuiteScripts/ACME/b.js"
        self.assertEqual(sdf.wk("turn", "check", "sandbox", a)[0], 1)
        self.assertEqual(sdf.wk("turn", "take", "sandbox", a)[0], 0)
        self.assertEqual(sdf.wk("turn", "check", "sandbox", a)[0], 0)
        self.assertEqual(sdf.wk("turn", "check", "sandbox", a, b)[0], 1)    # every file it sends
        self.assertEqual(sdf.held_items(), [a])
        self.run_git("checkout", "-q", "-b", "other", "main")
        code, out = sdf.wk("turn", "take", "sandbox", a, b)
        self.assertEqual(code, 1)
        self.assertIn("a.js is held by pnl", out)
        self.assertEqual(sdf.wk("turn", "take", "sandbox", b)[0], 0)        # a different file: no wait
        self.run_git("checkout", "-q", "pnl")
        self.assertIsNone(sdf.wk_refusal("turn", "release", "sandbox", a))
        self.assertEqual(sdf.held_items(), [])

    def test_vendor_paths_pass_through_unharmed(self):
        vendor = "src/FileCabinet/SuiteScripts/VENDOR | X & Y/y.js"
        self.assertEqual(sdf.wk("turn", "take", "sandbox", vendor)[0], 0)
        self.assertEqual(sdf.held_items(), [vendor])
        self.assertIsNone(sdf.wk_refusal("turn", "release", "sandbox", vendor))

    def test_gate_release_refuses_off_main(self):
        self.assertIn("Releases run only from main", sdf.wk_refusal("gate", "release"))
        self.run_git("checkout", "-q", "main")
        self.assertIsNone(sdf.wk_refusal("gate", "release"))


class ImportFailureTest(GitRepoTest):
    """A failed import never passes as 'new': an item without a copy is new only when a listing confirms production
    doesn't have it (review of 2026-11-04, blocker 1)."""
    JS = "src/FileCabinet/SuiteScripts/ACME/a.js"
    LIB = "src/FileCabinet/SuiteScripts/ACME/lib.js"
    OBJ = "src/Objects/customscript_acme_x.xml"

    def setUp(self):
        super().setUp()
        self.commit(self.LIB, "lib", "Lib")
        self.commit(self.OBJ, "<usereventscript/>", "Object")
        self.listing = {}    # 'file:list' / 'object:list', or (that, its folder or ID) -> (exit, output)
        self.listed = []     # what each listing asked for
        self.imported = {}   # src path -> text the fake import writes

    def fake_cli(self, args, auth_id=None, capture=False, env=None, cwd=None):
        cmd = args[0]
        if cmd in ("file:import", "object:import"):
            for path, text in self.imported.items():
                if sdf.is_object(path) == (cmd == "object:import"):
                    (Path(cwd) / path).parent.mkdir(parents=True, exist_ok=True)
                    (Path(cwd) / path).write_text(text)
            return 1, "\x1b[2K\x1b[1GThe following files were imported:\n... other items could not be imported"
        self.listed.append(args[-1])
        return self.listing.get((cmd, args[-1]), self.listing.get(cmd, (0, "")))

    def run_import(self, *paths):
        items = [(p, sdf.cabinet_of(p)) for p in paths]
        with mock.patch.object(sdf, "suitecloud", side_effect=self.fake_cli), \
                mock.patch.object(sdf, "check_env", return_value="acme-prod"), \
                mock.patch.object(sdf, "import_env", return_value={}):
            return REAL_IMPORT_PRODUCTION(items, "HEAD", Path(self.tmp.name) / "out")

    def test_a_file_production_lists_but_the_import_missed_refuses(self):
        self.imported = {self.JS: "1"}
        self.listing["file:list"] = (0, "\x1b[2K\x1b[1G/SuiteScripts/ACME/a.js\n/SuiteScripts/ACME/lib.js")
        self.assertIn(f"could not import production's copy of {self.LIB}", refuses(self, self.run_import, self.JS, self.LIB))

    def test_a_file_the_listing_confirms_absent_is_new(self):
        self.imported = {self.JS: "1"}
        self.listing["file:list"] = (0, "/SuiteScripts/ACME/a.js")
        self.assertEqual(self.run_import(self.JS, self.LIB), {self.JS: b"1", self.LIB: None})

    def test_a_file_in_a_folder_production_doesnt_have_is_new(self):
        # file:list says 'No files found.' for a missing folder (or fails, in some CLI versions): the nearest parent
        # that lists files, and shows nothing under the missing folder, confirms it.
        other = "\x1b[2K\x1b[1G/SuiteScripts/Other/x.js\n/SuiteScripts/Other/sub/y.js"
        for leaf in ((0, "\x1b[2K\x1b[1GNo files found."), (1, "The folder does not exist")):
            self.listing = {("file:list", "/SuiteScripts/ACME"): leaf, ("file:list", "/SuiteScripts"): (0, other)}
            self.listed = []
            self.assertEqual(self.run_import(self.LIB), {self.LIB: None})
            self.assertEqual(self.listed, ["/SuiteScripts/ACME", "/SuiteScripts"])

    def test_a_listing_that_fails_or_says_nothing_refuses(self):
        for listing in ((1, "An error occurred"), (0, ""), (0, "No files found.")):
            self.listing = {"file:list": listing}   # every folder up to the top
            self.assertIn("could not import", refuses(self, self.run_import, self.LIB))

    def test_a_parent_listing_that_shows_the_folder_refuses(self):
        # The folder's own listing failed, but the parent shows files in it: production has the folder.
        self.listing = {("file:list", "/SuiteScripts/ACME"): (1, "An error occurred"),
                        ("file:list", "/SuiteScripts"): (0, "/SuiteScripts/ACME/other.js")}
        self.assertIn("could not import", refuses(self, self.run_import, self.LIB))

    def test_an_object_production_has_but_the_import_missed_refuses(self):
        self.listing["object:list"] = (0, "The following objects were found in your account:\n"
                                          "usereventscript:customscript_acme_x")
        self.assertIn(f"could not import production's copy of {self.OBJ}", refuses(self, self.run_import, self.OBJ))
        self.listing["object:list"] = (1, "There are no objects matching your search criteria.")
        self.assertIn("could not import", refuses(self, self.run_import, self.OBJ))

    def test_an_object_production_has_none_of_is_new(self):
        self.listing["object:list"] = (0, "\x1b[2K\x1b[1GThere are no objects matching your search criteria.")
        self.assertEqual(self.run_import(self.OBJ), {self.OBJ: None})
        # a listing matches script IDs loosely: a longer ID is not this one
        self.listing["object:list"] = (0, "The following objects were found in your account:\n"
                                          "usereventscript:customscript_acme_x_old")
        self.assertEqual(self.run_import(self.OBJ), {self.OBJ: None})


class ContentKeyedRowsTest(unittest.TestCase):
    """A row without a scriptid (a role's permission, a list's value, a translation) added in production is a real
    difference, even though git has the element it sits in (review of 2026-11-04, blocker 2)."""
    ROLE = ('<role scriptid="customrole_acme_ap"><name>ACME AP</name><centertype>BASIC</centertype>'
            '<permissions>'
            '<permission><permkey>TRAN_VENDBILL</permkey><permlevel>FULL</permlevel></permission>'
            '<permission><permkey>LIST_VENDOR</permkey><permlevel>VIEW</permlevel></permission>'
            '</permissions></role>')
    LIST = ('<customlist scriptid="customlist_acme_box"><name>Box</name><customvalues>'
            '<customvalue scriptid="val_1"><value>Box 1</value></customvalue>'
            '<customvalue scriptid="val_2"><value>Box 2</value></customvalue></customvalues>'
            '<translations><translation><locale>es_ES</locale><label>Caja</label></translation>'
            '<translation><locale>fr_FR</locale><label>Boite</label></translation></translations></customlist>')

    def diffs(self, git, prod):
        return sdf.sdf_compare.object_differences(git, prod)

    def test_a_permission_added_in_production_is_real(self):
        prod = self.ROLE.replace("</permissions>", "<permission><permkey>ADMI_ACCOUNTING</permkey>"
                                                   "<permlevel>FULL</permlevel></permission></permissions>")
        found = self.diffs(self.ROLE, prod)
        self.assertTrue(found and all("only in production" in d and "ADMI_ACCOUNTING" in d for d in found), found)

    def test_a_permission_level_raised_in_production_is_real(self):
        prod = self.ROLE.replace("<permlevel>VIEW</permlevel>", "<permlevel>FULL</permlevel>")
        self.assertTrue(self.diffs(self.ROLE, prod))

    def test_reordered_rows_are_the_same(self):
        perms = ("<permission><permkey>TRAN_VENDBILL</permkey><permlevel>FULL</permlevel></permission>",
                 "<permission><permkey>LIST_VENDOR</permkey><permlevel>VIEW</permlevel></permission>")
        prod = self.ROLE.replace(perms[0] + perms[1], perms[1] + perms[0])
        self.assertEqual(self.diffs(self.ROLE, prod), [])

    def test_a_translation_added_in_production_is_real(self):
        prod = self.LIST.replace("</translations>", "<translation><locale>de_DE</locale><label>Kasten</label>"
                                                    "</translation></translations>")
        self.assertTrue(any("de_DE" in d for d in self.diffs(self.LIST, prod)))

    def test_a_field_netsuite_fills_in_on_a_scripted_row_is_still_ignored(self):
        prod = self.LIST.replace("<value>Box 1</value>", "<value>Box 1</value><isinactive>F</isinactive>"
                                                         "<abbreviation>B1</abbreviation>")
        self.assertEqual(self.diffs(self.LIST, prod), [])

    def test_a_blank_field_on_a_permission_row_git_has_is_ignored(self):
        # A custom record's export writes <restriction></restriction> on every permission row git already has.
        git = ('<customrecordtype scriptid="customrecord_acme_doc"><permissions>'
               '<permission><permittedlevel>EDIT</permittedlevel><permittedrole>ADMINISTRATOR</permittedrole></permission>'
               '<permission><permittedlevel>VIEW</permittedlevel><permittedrole>[scriptid=customrole_acme_ap]'
               '</permittedrole></permission></permissions></customrecordtype>')
        prod = git.replace("</permittedrole>", "</permittedrole><restriction></restriction>")
        self.assertEqual(self.diffs(git, prod), [])
        added = prod.replace("</permissions>", "<permission><permittedlevel>FULL</permittedlevel><permittedrole>"
                                               "CFO</permittedrole><restriction></restriction></permission></permissions>")
        found = self.diffs(git, added)
        self.assertTrue(found and all("CFO" in d for d in found), found)

    def test_a_row_added_in_production_with_default_values_is_real(self):
        prod = self.ROLE.replace("</permissions>", "<permission><permkey>ADMI_ACCOUNTING</permkey>"
                                                   "<restricted>F</restricted></permission></permissions>")
        self.assertTrue(self.diffs(self.ROLE, prod))


class StrictConfigTest(unittest.TestCase):
    """Missing settings never weaken a guard silently (review of 2026-11-04, should-fix 1 and 3)."""

    def configure(self, text):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(sdf, "CONFIG", sdf.CONFIG), \
                mock.patch.object(sdf, "ROOT", sdf.ROOT):
            Path(tmp, "sdf.toml").write_text(text)
            return refuses(self, sdf.configure, tmp)

    def test_requires_is_required(self):
        self.assertIn("missing requires", self.configure(SDF_TOML.replace('requires = "1.39.0"\n', "")))

    def test_env_prefix_is_required_for_each_env(self):
        self.assertIn("missing [sb] env_prefix", self.configure(SDF_TOML.replace('env_prefix = "ACME_SB_"\n', "")))
        self.assertIn("missing [prod] env_prefix", self.configure(SDF_TOML.replace('env_prefix = "ACME_PROD_"\n', "")))

    def test_an_account_env_doesnt_name_refuses_every_call(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"NETSUITE_ENV_FILE": str(Path(tmp, ".env"))}):
            Path(tmp, ".env").write_text("ACME_PROD_ACCOUNT_ID=1234567\nACME_SBX_ACCOUNT_ID=1234567_SB1\n")
            with mock.patch.object(sdf, "auth_info", return_value={"account": "1234567_SB1"}):
                self.assertIn("names no ACME_SB_ACCOUNT_ID", refuses(self, sdf.check_env, "sb"))
            Path(tmp, ".env").write_text("ACME_SB_ACCOUNT_ID=1234567_SB1\n")
            with mock.patch.object(sdf, "auth_info", return_value={"account": "1234567"}):
                self.assertIn("names no ACME_PROD_ACCOUNT_ID", refuses(self, sdf.check_env, "prod"))

    def test_a_worktree_outside_the_main_checkout_uses_the_main_checkouts_env(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NETSUITE_ENV_FILE", None)
            main, wt = Path(tmp, "main"), Path(tmp, "elsewhere", "wt")
            main.mkdir()
            git = lambda *a, cwd=main: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a],
                                                      cwd=cwd, check=True, capture_output=True)
            git("init", "-q")
            git("commit", "-q", "--allow-empty", "-m", "x")
            git("worktree", "add", "-q", "--detach", str(wt))
            (main / ".env").write_text("ACME_PROD_ACCOUNT_ID=1234567\n")
            with mock.patch.object(sdf, "ROOT", wt.resolve()):
                self.assertEqual(sdf.expected_account("prod"), "1234567")


class KitCheckoutTest(unittest.TestCase):
    """Production runs only a released kit: clean, and on work-kit's origin/main (should-fix 2)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.origin, self.home = base / "origin.git", base / "work-kit"
        self.git("init", "-q", "--bare", "-b", "main", str(self.origin), cwd=base)
        self.git("clone", "-q", str(self.origin), str(self.home), cwd=base)
        self.write("toolkits/netsuite/sdf.py", "kit 1")
        self.write("wk/gates.py", "gates 1")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "kit")
        self.git("branch", "-M", "main")
        self.git("push", "-q", "origin", "main")
        self.wk_home = self.home   # the wk on PATH is this checkout's, unless a test makes another
        self.patches = [mock.patch.object(sdf, "KIT_HOME", self.home),
                        mock.patch.object(sdf, "work_kit_home", side_effect=lambda: self.wk_home)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def git(self, *args, cwd=None):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd or self.home,
                              check=True, capture_output=True, text=True).stdout

    def write(self, rel, text, home=None):
        path = (home or self.home) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def installed_work_kit(self):
        """A second clone of work-kit, as the installed one whose wk runs the gates."""
        self.wk_home = Path(self.tmp.name) / "installed"
        self.git("clone", "-q", "-b", "main", str(self.origin), str(self.wk_home), cwd=self.tmp.name)
        return self.wk_home

    def test_wk_changes_refuse_too(self):
        self.write("wk/gates.py", "gates edited")
        self.assertIn("uncommitted changes", sdf.kit_checkout_refusal())

    def test_the_installed_work_kit_is_checked_when_it_is_another_checkout(self):
        home = self.installed_work_kit()
        self.assertIsNone(sdf.kit_checkout_refusal())
        self.write("wk/gates.py", "gates edited", home)
        msg = sdf.kit_checkout_refusal()
        self.assertIn(f"work-kit (its wk runs the production gates) at {home.resolve().as_posix()} has uncommitted", msg)
        self.git("checkout", "--", ".", cwd=home)
        self.git("switch", "-q", "-c", "feature", cwd=home)
        self.write("wk/gates.py", "gates 2", home)
        self.git("commit", "-q", "-am", "unreleased gates", cwd=home)
        self.assertIn("on 'feature', which is not in work-kit's origin/main", sdf.kit_checkout_refusal())

    def test_the_kits_own_checkout_is_still_checked_beside_the_installed_one(self):
        self.installed_work_kit()
        self.write("toolkits/netsuite/sdf.py", "edited")
        msg = sdf.kit_checkout_refusal()
        self.assertIn(f"the SDF kit at {self.home.resolve().as_posix()} has uncommitted changes", msg)
        self.assertNotIn("work-kit (its wk", msg)

    def test_a_clean_kit_on_main_passes(self):
        self.assertIsNone(sdf.kit_checkout_refusal())

    def test_uncommitted_or_untracked_kit_changes_refuse(self):
        self.write("toolkits/netsuite/sdf.py", "edited")
        self.assertIn("uncommitted changes", sdf.kit_checkout_refusal())
        self.git("checkout", "--", ".")
        self.write("toolkits/netsuite/query.py", "planted")
        self.assertIn("uncommitted changes", sdf.kit_checkout_refusal())

    def test_a_feature_branch_kit_refuses(self):
        self.git("switch", "-q", "-c", "feature")
        self.write("toolkits/netsuite/sdf.py", "kit 2")
        self.git("commit", "-q", "-am", "unreleased kit")
        msg = sdf.kit_checkout_refusal()
        self.assertIn("on 'feature', which is not in work-kit's origin/main", msg)
        self.assertIn("wk update", msg)

    def test_main_ahead_only_by_its_records_passes(self):
        self.write(".work/list.tsv", "a piece")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "wk: record")
        self.assertIsNone(sdf.kit_checkout_refusal())

    def test_the_dry_run_lists_it_with_the_other_gates(self):
        self.write("toolkits/netsuite/sdf.py", "edited")
        with mock.patch.object(sdf, "wk_refusal", return_value=None):
            refusals, _, _ = sdf.prod_gates("deploy", None)
        self.assertIn("uncommitted changes", refusals[0])


if __name__ == "__main__":
    unittest.main()
