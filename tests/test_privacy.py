"""The privacy guard (private names never reach a public repo), the clean public tree, and the launcher's forwarding."""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
KIT = Path(__file__).resolve().parents[1]
os.environ["NH_AUTO_UPDATE"] = "0"

from wk import cli, core, privacy, turns, upkeep  # noqa: E402


def sh(cwd, *args, env=None, check=True):
    return subprocess.run(["git", "-C", str(cwd), *args], check=check, capture_output=True, text=True,
                          encoding="utf-8", env=env)


class Names(unittest.TestCase):
    def test_the_list_comes_from_the_settings_without_public_names(self):
        s = {"owner": "Pat Lee", "privacy": {"terms": ["Globex", "TKT"]},
             "repos": {"acme-co": {"path": "C:/Projects/acme-co", "client": "Acme Corp"},
                       "kit": {"path": "C:/Projects/open-kit", "public": True, "list": "C:/Projects/kit"}}}
        names = privacy.terms(s)
        self.assertIn("Acme Corp", names)
        self.assertIn("acme-co", names)
        self.assertIn("Globex", names)
        self.assertIn("TKT", names)
        self.assertIn("Pat", names)
        for public in ("kit", "open-kit"):
            self.assertNotIn(public, names)
        pat = privacy.pattern(names)
        self.assertEqual(privacy.lines_with("ok\nfor ACME co today\nAcmeCo\nacme_co\nacme-copper", pat), [2, 3, 4])
        self.assertEqual(privacy.lines_with("Patience is fine; pat. is not", pat), [1])
        self.assertIsNone(privacy.pattern([]))

    def test_pr_text_needs_four_sections_and_a_public_pr_no_private_names(self):
        good = "## What was wrong\nx\n## What changed\ny\n## How it was tested\nz\n## What it means for you\nw"
        self.assertEqual(turns.missing_sections(good), [])
        self.assertEqual(turns.missing_sections("## What was wrong\n## What changed:"),
                         ["How it was tested", "What it means for you"])
        s = {"owner": "Pat Lee", "pr_sections_from": "2026-11-08T00:00:00Z"}
        new = {"body": "## What was wrong\nx", "createdAt": "2026-11-09T10:00:00Z"}
        old = {"body": "## What was wrong\nx", "createdAt": "2026-11-01T10:00:00Z"}
        with self.assertRaises(turns.Refused) as no:
            turns._text_checks(s, Path(tempfile.mkdtemp()), new, "o/r", 5)
        self.assertIn("lacks What changed, How it was tested, What it means for you", str(no.exception))
        warned = turns._text_checks(s, Path(tempfile.mkdtemp()), old, "o/r", 5)
        self.assertIn("only warns", warned[0])
        self.assertEqual(turns._text_checks({}, Path(tempfile.mkdtemp()), new, "o/r", 5)[0][:30],
                         "the description lacks What cha")  # no pr_sections_from yet: a warning only


class Guard(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.origin)], check=True, capture_output=True)
        self.pub = self.tmp / "open-kit"
        self.pub.mkdir()
        for args in (("init", "-b", "main"), ("config", "user.name", "t"), ("config", "user.email", "t@example.org"),
                     ("remote", "add", "origin", str(self.origin))):
            sh(self.pub, *args)
        (self.pub / "a.txt").write_text("hello\n", encoding="utf-8")
        sh(self.pub, "add", "-A")
        sh(self.pub, "commit", "-m", "Start")
        self.settings = self.tmp / "settings.toml"
        self.settings.write_text(f'owner = "Pat Lee"\n\n[privacy]\nterms = ["Globex"]\n\n[repos.acme]\n'
                                 f'path = "{core.posix(self.tmp / "acme")}"\nclient = "Acme Corp"\n\n[repos.kit]\n'
                                 f'path = "{core.posix(self.pub)}"\npublic = true\n', encoding="utf-8")
        self.env = os.environ | {"NH_SETTINGS": str(self.settings), "NH_AUTO_UPDATE": "0"}
        done = privacy.install_hook(self.pub, KIT / "bin" / "nh")
        self.assertTrue(done["installed"])
        self.assertTrue(privacy.hook_installed(self.pub))

    def push(self, branch="main"):
        return sh(self.pub, "push", "origin", f"HEAD:refs/heads/{branch}", env=self.env, check=False)

    def test_a_push_with_a_private_name_stops_and_a_clean_one_goes(self):
        self.assertEqual(self.push().returncode, 0, self.push().stderr)
        (self.pub / "b.txt").write_text("one\nbuilt for Acme Corp\n", encoding="utf-8")
        sh(self.pub, "add", "-A")
        sh(self.pub, "commit", "-m", "Add b")
        out = self.push()
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("push stopped", out.stderr)
        self.assertIn("b.txt line 2", out.stderr)
        self.assertNotIn("Acme", out.stderr)  # the place, never the name
        sh(self.pub, "reset", "--hard", "HEAD~1")
        (self.pub / "c.txt").write_text("generic\n", encoding="utf-8")
        sh(self.pub, "add", "-A")
        sh(self.pub, "commit", "-m", "Add c\n\nfor the globex team")
        out = self.push()
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("message, line 3", out.stderr)
        sh(self.pub, "commit", "--amend", "-m", "Add c")
        self.assertEqual(self.push().returncode, 0)
        self.assertNotEqual(self.push("acme-fix").returncode, 0)  # the branch name
        self.assertEqual(self.push("generic-fix").returncode, 0)

    def test_someone_elses_hook_is_kept(self):
        path = privacy.hook_path(self.pub)
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.assertFalse(privacy.install_hook(self.pub, KIT / "bin" / "nh")["installed"])
        self.assertEqual(path.read_text(encoding="utf-8"), "#!/bin/sh\nexit 0\n")

    def test_the_hook_an_older_release_wrote_is_replaced(self):
        path = privacy.hook_path(self.pub)
        path.write_text('#!/bin/sh\n# work-kit: the privacy guard: a push ...\nexec sh "/old/bin/wk" privacy push "$@"\n',
                        encoding="utf-8")
        self.assertFalse(privacy.hook_installed(self.pub))  # doctor asks for setup
        self.assertTrue(privacy.install_hook(self.pub, KIT / "bin" / "nh")["changed"])
        self.assertEqual(path.read_text(encoding="utf-8"), privacy.hook_text(KIT / "bin" / "nh"))

    def test_the_clean_tree_leaves_out_the_list_and_is_scanned(self):
        (self.pub / ".work").mkdir()
        (self.pub / ".work" / "pieces.tsv").write_text("Acme Corp's piece\n", encoding="utf-8")
        (self.pub / "bin").mkdir()
        (self.pub / "bin" / "run").write_text("#!/bin/sh\n", encoding="utf-8")
        sh(self.pub, "add", "-A")
        sh(self.pub, "commit", "-m", "More")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--settings", str(self.settings), "--cwd", str(self.pub), "privacy", "tree",
                             str(self.tmp / "clean"), "--ref", "main"])
        self.assertEqual(code, 0, out.getvalue() + err.getvalue())
        self.assertEqual(json.loads(out.getvalue())["hits"], 0)
        self.assertFalse((self.tmp / "clean" / ".work").exists())
        self.assertTrue((self.tmp / "clean" / "bin" / "run").is_file())
        (self.tmp / "clean" / "x.md").write_text("see Globex\n", encoding="utf-8")
        with redirect_stdout(io.StringIO()) as out2:
            code = cli.main(["--settings", str(self.settings), "privacy", "scan", str(self.tmp / "clean")])
        self.assertEqual(code, 1)
        self.assertIn("x.md line 1", out2.getvalue())
        self.assertNotIn("Globex", out2.getvalue())

    def test_the_github_job(self):
        sh(self.pub, "commit", "--allow-empty", "-m", "Second\n\nAcme Corp asked")
        base, head = (sh(self.pub, "rev-parse", r).stdout.strip() for r in ("HEAD~1", "HEAD"))
        event = self.tmp / "event.json"
        pr = {"title": "Faster list", "body": "## What was wrong\nslow", "head": {"sha": head, "ref": "faster"},
              "base": {"sha": base}}
        event.write_text(json.dumps({"pull_request": pr}), encoding="utf-8")
        env = {"GITHUB_EVENT_PATH": str(event), "PRIVATE_TERMS": "Acme Corp\nGlobex\n"}
        code, text = privacy.ci(env, self.pub)
        self.assertEqual(code, 1)
        self.assertIn("message, line 3", text)
        self.assertNotIn("Acme", text)
        code, text = privacy.ci({"GITHUB_EVENT_PATH": str(event)}, self.pub)
        self.assertEqual((code, text[:30]), (0, "Privacy check: not checked (no"))
        env["PRIVATE_TERMS"] = "Globex"
        self.assertEqual(privacy.ci(env, self.pub)[0], 0)


class Forwarding(unittest.TestCase):
    """bin/nh forwards to the kit the settings name, so `nh` from another folder on PATH runs that clone; bin/wk is
    bin/nh by its first name; and setup never forwards, so it makes the checkout it runs from the kit."""

    def test_bin_nh_forwards_to_the_named_kit_and_setup_never_does(self):
        tmp = Path(tempfile.mkdtemp())
        new = tmp / "new kit"
        (new / "bin").mkdir(parents=True)
        (new / "bin" / "nh").write_text('#!/bin/sh\necho "NEW $NH_FORWARDED $*"\nexit 7\n', encoding="utf-8")
        s = tmp / "s.toml"
        s.write_text(f'owner = "x"\nkit = "{core.posix(new)}"\n\n[repos.a]\nkit = "nowhere"\n', encoding="utf-8")
        env = os.environ | {"NH_SETTINGS": str(s)}
        env.pop("NH_FORWARDED", None)
        for launcher in ("nh", "wk"):
            done = subprocess.run(["sh", str(KIT / "bin" / launcher), "version", "a b"], capture_output=True,
                                  text=True, env=env)
            self.assertEqual(done.returncode, 7, done.stderr)
            self.assertTrue(done.stdout.startswith("NEW "))
            self.assertTrue(done.stdout.rstrip().endswith("version a b"))
        done = subprocess.run(["sh", str(KIT / "bin" / "nh"), "setup", "--help"], capture_output=True, text=True,
                              env=env)
        self.assertEqual(done.returncode, 0, done.stderr)  # ran here: setup makes this checkout the kit
        self.assertNotIn("NEW", done.stdout)
        old = env | {"WORKKIT_SETTINGS": str(s)}  # the settings variable before 1.56.0 still counts
        old.pop("NH_SETTINGS")
        self.assertEqual(subprocess.run(["sh", str(KIT / "bin" / "nh"), "version"], capture_output=True,
                                        text=True, env=old).returncode, 7)
        s.write_text('owner = "x"\n', encoding="utf-8")  # no kit named: it runs here
        for launcher in ("nh", "wk"):
            done = subprocess.run(["sh", str(KIT / "bin" / launcher), "version"], capture_output=True, text=True,
                                  env=env)
            self.assertEqual((done.returncode, done.stdout.strip()), (0, cli.__version__))

    def test_doctor_says_which_checkout_ran(self):
        line = upkeep.ran_from({}, {"NH_FORWARDED": "/somewhere/old"})
        self.assertIn(f"ran from {core.posix(upkeep.KIT)}", line)
        self.assertIn("forwarded by /somewhere/old/bin", line)
        self.assertNotIn("forwarded", upkeep.ran_from({}, {}))


if __name__ == "__main__":
    unittest.main()
