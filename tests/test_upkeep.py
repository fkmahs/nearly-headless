"""The command center page, the CI guard, setup and doctor. gh is stubbed; skills install into a temp folder."""

import io
import os
import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import tomllib
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["NH_AUTO_UPDATE"] = "0"  # tests never reach GitHub; the daily check has its own test

from wk import __version__, cli, core, dashboard, guard, keepout, reply, settings, upkeep, work  # noqa: E402

KIT = Path(__file__).resolve().parents[1]
BAD_CI = """name: ci
on:
  push:
    branches: ['**']
  schedule:
    - cron: '0 3 * * *'
jobs:
  t:
    runs-on: ubuntu-latest
"""


def page_data(page: str) -> dict:
    """The view the command center page draws from (it is embedded in the page's script)."""
    return json.loads(page.split("const D = ", 1)[1].split(";\nconst esc", 1)[0].replace("<\\/", "</"))


def without_setup_lines(text: str) -> str:
    """A settings file less the two top lines setup writes itself (`kit`, `pr_sections_from`)."""
    return "".join(line for line in text.splitlines(keepends=True)
                   if not line.startswith(("kit = ", "pr_sections_from = ")))


def sh(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


class DockerCheck(unittest.TestCase):
    """nh doctor's Docker line: off Linux, builders' tests need it, so missing or stopped is a failure with the fix."""
    def check(self, installed=True, running=True, platform="win32"):
        info = lambda argv, timeout=0: subprocess.CompletedProcess(argv, 0 if running else 1, "", "")  # noqa: E731
        return upkeep.docker_check(platform, lambda name: "docker" if installed else None, info)

    def test_docker_line(self):
        self.assertEqual(self.check(), [{"ok": True, "check": "Docker running: nh test runs in Linux"}])
        stopped, = self.check(running=False)
        self.assertFalse(stopped["ok"])
        self.assertIn("fix: start Docker Desktop, and turn on its 'Start Docker Desktop when you sign in'",
                      stopped["check"])
        missing, = self.check(installed=False)
        self.assertFalse(missing["ok"])
        self.assertIn("fix: winget install Docker.DockerDesktop", missing["check"])
        self.assertIn("brew install --cask docker", self.check(installed=False, platform="darwin")[0]["check"])
        self.assertEqual(self.check(installed=False, platform="linux"), [])  # Linux runs tests without it

    def test_doctor_fails_on_stopped_docker(self):
        found = [{"ok": True, "check": "a"}, {"ok": False, "check": "Docker isn't running: b"}]
        out = io.StringIO()
        real, upkeep.doctor = upkeep.doctor, lambda s, where: found
        try:
            with redirect_stdout(out):
                code = cli.main(["--settings", str(Path(tempfile.mkdtemp()) / "s.toml"), "doctor"])
        finally:
            upkeep.doctor = real
        self.assertEqual(code, 1)
        self.assertIn("FIX  Docker isn't running: b", out.getvalue())


class Upkeep(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.app = self.tmp / "app"
        self.app.mkdir()
        sh(self.app, "init", "-b", "main")
        sh(self.app, "config", "user.name", "t")
        sh(self.app, "config", "user.email", "t@example.org")
        (self.app / ".github" / "workflows").mkdir(parents=True)
        shutil.copy(KIT / "templates" / "tests.yml", self.app / ".github" / "workflows" / "tests.yml")
        sh(self.app, "add", "-A")
        sh(self.app, "commit", "-m", "init")
        self.settings = self.tmp / "settings.toml"
        self.claude = self.tmp / "claude"  # stands in for ~/.claude: tests never touch the real one
        self.skills = self.claude / "skills"
        self.codex, self.agents = self.tmp / "codex", self.tmp / "agents"  # stand in for ~/.codex and ~/.agents
        self._real = dashboard._gh, upkeep.claude_dir, upkeep.codex_dir, upkeep.agents_dir, upkeep.beside
        upkeep.claude_dir, upkeep.codex_dir = (lambda: self.claude), (lambda: self.codex)
        upkeep.beside = lambda: self.tmp / "wwid"  # never the laptop's own profile repo beside nearly-headless
        upkeep.agents_dir = lambda: self.agents
        dashboard._gh = lambda cwd, *a: json.dumps([{"number": 7, "title": "Aged debt view", "isDraft": True,
                                                      "headRefName": "aged-debt", "url": "https://x/7",
                                                      "updatedAt": "2026-10-26T10:00:00Z"}])

    def tearDown(self):
        dashboard._gh, upkeep.claude_dir, upkeep.codex_dir, upkeep.agents_dir, upkeep.beside = self._real

    def test_template_passes_the_guard_and_a_wasteful_workflow_does_not(self):
        self.assertEqual(guard.check(self.app), [])
        (self.app / ".github" / "workflows" / "ci.yml").write_text(BAD_CI, encoding="utf-8")
        problems = {i["problem"] for i in guard.check(self.app)}
        self.assertTrue({"runs on pushes to every branch", "runs on a schedule", "no timeout-minutes",
                         "pushes that only record work (.work/) run CI"} <= problems)
        daily = ("on:\n  schedule:\n    - cron: \"17 5 * * *\"\nconcurrency:\n  group: s\njobs:\n  s:\n"
                 "    runs-on: ubuntu-latest\n    timeout-minutes: 5\n")
        (self.app / ".github" / "workflows" / "ci.yml").write_text(daily, encoding="utf-8")
        self.assertNotIn("runs on a schedule", {i["problem"] for i in guard.check(self.app)})  # once a day, capped
        for costly in (daily.replace("17 5", "*/10 *"), daily.replace("timeout-minutes: 5", "timeout-minutes: 30"),
                       daily.replace("    timeout-minutes: 5\n", "")):
            (self.app / ".github" / "workflows" / "ci.yml").write_text(costly, encoding="utf-8")
            self.assertIn("runs on a schedule", {i["problem"] for i in guard.check(self.app)})
        self.assertFalse([i for i in guard.check(KIT) if i["workflow"] == "privacy-sweep.yml"])

    def test_setup_writes_settings_once_and_installs_skills(self):
        result = upkeep.setup(self.settings, "Pat Lee", [f"app={self.app}"], self.skills)
        self.assertTrue(result["created"])
        self.assertEqual(set(result["skills"]), {"builder", "catch-up", "project-manager", "supervisor", "wwid"})
        self.assertTrue((self.skills / "supervisor" / "SKILL.md").is_file())
        for ref in ("merge.md", "brief.md", "handover.md"):  # reference files the skill says when to read
            self.assertTrue((self.skills / "supervisor" / ref).is_file())
        self.assertTrue((self.skills / "wwid" / "responder.md").is_file())
        s = settings.load(self.settings)
        self.assertEqual(s["owner"], "Pat Lee")
        self.assertTrue(core.same_path(settings.repo(s, "app"), self.app))
        self.assertFalse(upkeep.setup(self.settings, "Someone Else")["created"])  # never overwritten
        self.assertEqual(settings.load(self.settings)["owner"], "Pat Lee")
        with self.assertRaises(core.KitError):
            upkeep.setup(self.tmp / "other.toml")  # a new laptop must name its owner

    def test_setup_keeps_spreadsheets_out_of_every_repo_and_its_worktrees_and_keeps_the_owners_lines(self):
        sh(self.app, "worktree", "add", "-q", str(self.tmp / "wt"), "-b", "wt")
        exclude = self.app / ".git" / "info" / "exclude"
        exclude.write_text("# mine\nscratch/\n", encoding="utf-8")
        self.settings.write_text(f'owner = "Pat Lee"\n\n[repos.app]\npath = "{core.posix(self.app)}"\n',
                                 encoding="utf-8")
        s = settings.load(self.settings)

        def doctor_ok():
            return next(f for f in upkeep.doctor(s, self.settings) if "kept out of git" in f["check"])["ok"]

        def status(where):
            return subprocess.run(["git", "-C", str(where), "status", "--porcelain"], capture_output=True, text=True,
                                  check=True).stdout.strip()
        self.assertFalse(doctor_ok())
        self.assertTrue(upkeep.setup(self.settings, skills=self.skills)["keep_out"]["app"]["changed"])
        text = exclude.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# mine\nscratch/\n\n" + keepout.START))
        for name in ("Q3 Report.xlsx", "data.csv", "~$budget.xlsx", "deck.pptx", "code.py"):
            (self.tmp / "wt" / name).write_text("x", encoding="utf-8")
        self.assertEqual(status(self.tmp / "wt"), "?? code.py")  # the worktree shares the block: only code shows
        self.assertFalse(upkeep.setup(self.settings, skills=self.skills)["keep_out"]["app"]["changed"])
        exclude.write_text(text.replace("*.zip\n", "") + "mine-too/\n", encoding="utf-8")  # an older release's block
        upkeep.setup(self.settings, skills=self.skills)
        self.assertEqual(exclude.read_text(encoding="utf-8"), text + "mine-too/\n")  # refreshed in place, once
        lst = self.tmp / "app-list"  # the list kept in a checkout of its own gets the block as well
        subprocess.run(["git", "init", "-q", "-b", "main", str(lst)], check=True, capture_output=True)
        self.settings.write_text(f'owner = "Pat Lee"\n\n[repos.app]\npath = "{core.posix(self.app)}"\n'
                                 f'list = "{core.posix(lst)}"\n', encoding="utf-8")
        done = upkeep.setup(self.settings, skills=self.skills)["keep_out"]["app"]
        self.assertTrue(done["list"]["changed"])
        self.assertIn(keepout.START, (lst / ".git" / "info" / "exclude").read_text(encoding="utf-8"))
        self.assertFalse(upkeep.setup(self.settings, skills=self.skills)["keep_out"]["app"]["list"]["changed"])
        self.assertEqual(exclude.read_text(encoding="utf-8"), text + "mine-too/\n")
        self.assertTrue(doctor_ok())
        self.assertEqual(status(self.app), "")  # nothing committed or changed in the repo itself
        # an invoice statement still commits the way the invoicing toolkit does it (git add -- . in billing/): the
        # readable .md goes, a .csv there stays out like any other
        (self.tmp / "wt" / "billing").mkdir()
        (self.tmp / "wt" / "billing" / "INV-0001.md").write_text("# INV-0001\n", encoding="utf-8")
        (self.tmp / "wt" / "billing" / "INV-0001.csv").write_text('"date","hours"\n', encoding="utf-8")
        (self.tmp / "wt" / "billing" / "stray.xlsx").write_text("x", encoding="utf-8")
        sh(self.tmp / "wt" / "billing", "add", "--", ".")
        sh(self.tmp / "wt" / "billing", "commit", "-q", "-m", "INV-0001", "--", ".")
        files = subprocess.run(["git", "-C", str(self.tmp / "wt"), "ls-files", "billing"], capture_output=True,
                               text=True, check=True).stdout.split()
        self.assertEqual(files, ["billing/INV-0001.md"])

    def test_the_exclude_block_never_swallows_or_garbles_the_owners_lines(self):
        self.settings.write_text(f'owner = "Pat Lee"\n\n[repos.app]\npath = "{core.posix(self.app)}"\n',
                                 encoding="utf-8")
        exclude = self.app / ".git" / "info" / "exclude"
        exclude.write_text("# nearly-headless: a start line with no end\nmine/\n", encoding="utf-8")
        upkeep.setup(self.settings, skills=self.skills)
        first = exclude.read_text(encoding="utf-8")
        self.assertTrue(first.startswith("# nearly-headless: a start line with no end\nmine/\n\n" + keepout.START))
        upkeep.setup(self.settings, skills=self.skills)
        self.assertEqual(exclude.read_text(encoding="utf-8"), first)  # the next run finds its own block
        exclude.write_bytes(b"caf\xe9/\n")  # not UTF-8: left exactly as it is, and said so
        done = upkeep.setup(self.settings, skills=self.skills)["keep_out"]["app"]
        self.assertFalse(done["installed"])
        self.assertIn("not UTF-8", done["why"])
        self.assertEqual(exclude.read_bytes(), b"caf\xe9/\n")

    def test_expectations_reach_every_chat_and_never_overwrite_the_owners_own(self):
        result = upkeep.setup(self.settings, "Pat Lee", [f"app={self.app}"], self.skills)
        self.assertTrue(result["expectations"]["Claude"]["installed"])
        self.assertNotIn("Codex", result["expectations"])  # Codex is not on this test laptop
        self.assertFalse(result["instructions"]["changed"])
        mine = (self.claude / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertTrue(mine.startswith(upkeep.DEFAULT_MARK))  # a new laptop: the neutral default
        self.assertIn((KIT / "templates" / "instructions.md").read_text(encoding="utf-8"), mine)
        self.assertNotIn((KIT / "AGENTS.md").read_text(encoding="utf-8"), mine)
        self.assertIn("\n# Compact instructions\n", mine)  # what Claude keeps when it compacts a chat
        for keep in ("open piece", "waiting on the owner", "agent id", "this turn", "Drop finished work"):
            self.assertIn(keep, mine)
        self.assertTrue(all(c["ok"] for c in upkeep.expectations_check()))
        (self.claude / "CLAUDE.md").write_text("My own rules\n", encoding="utf-8")
        again = upkeep.install_expectations()["Claude"]
        self.assertFalse(again["installed"])
        self.assertIn("@", again["why"])
        self.assertIn("compact-instructions.md", again["why"])  # where to copy them from
        self.assertEqual((self.claude / "CLAUDE.md").read_text(encoding="utf-8"), "My own rules\n")
        self.assertFalse(all(c["ok"] for c in upkeep.expectations_check()))
        with (self.claude / "CLAUDE.md").open("a", encoding="utf-8") as f:
            f.write(f"@{core.posix(KIT / 'templates' / 'instructions.md')}\n")
        self.assertTrue(all(c["ok"] for c in upkeep.expectations_check()))

    def test_your_own_instructions_file_and_a_laptop_already_on_the_kits_keeps_them(self):
        own = self.tmp / "my-instructions.md"
        own.write_text("# How I work\n- Short answers.\n", encoding="utf-8")
        self.settings.write_text(f'owner = "Pat Lee"\ninstructions = "{core.posix(own)}"\n', encoding="utf-8")
        upkeep.setup(self.settings, skills=self.skills)
        mine = (self.claude / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertTrue(mine.startswith(upkeep.OURS) and "- Short answers.\n" in mine)
        self.assertTrue(all(c["ok"] for c in upkeep.expectations_check(settings.load(self.settings))))
        own.unlink()  # a file that is gone: said so, and the copy there stays
        done = upkeep.setup(self.settings, skills=self.skills)["expectations"]["Claude"]
        self.assertFalse(done["installed"])
        self.assertIn("can't be read", done["why"])
        self.assertEqual((self.claude / "CLAUDE.md").read_text(encoding="utf-8"), mine)
        # a laptop that ran the kit's AGENTS.md as its instructions: byte for byte the same after this release
        before = (upkeep.MARK + "\n\n" + (KIT / "AGENTS.md").read_text(encoding="utf-8") + "\n"
                  + upkeep.COMPACT.read_text(encoding="utf-8"))
        (self.claude / "CLAUDE.md").write_text(before, encoding="utf-8")
        self.settings.write_text('owner = "Pat Lee"\n\n[reply]\nrequire = []\n', encoding="utf-8")
        done = upkeep.setup(self.settings, skills=self.skills)
        self.assertTrue(done["instructions"]["changed"])
        self.assertEqual((self.claude / "CLAUDE.md").read_text(encoding="utf-8"), before)
        self.assertIn(f'instructions = "{core.posix(upkeep.installed_kit() / "AGENTS.md")}"\n',  # the laptop's kit
                      self.settings.read_text(encoding="utf-8").split("[reply]")[0])  # once, above the first table
        self.assertFalse(upkeep.setup(self.settings, skills=self.skills)["instructions"]["changed"])
        self.assertEqual((self.claude / "CLAUDE.md").read_text(encoding="utf-8"), before)

    def test_doctor_checks_path_has_the_kits_launcher_and_gives_the_command(self):
        ok, = upkeep.path_check({"PATH": str(KIT / "bin")})
        self.assertTrue(ok["ok"])
        bad, = upkeep.path_check({"PATH": "/usr/bin"}, "win32")
        self.assertFalse(bad["ok"])
        self.assertIn("[Environment]::SetEnvironmentVariable('Path', '", bad["check"])
        self.assertIn("bin;' + [Environment]::GetEnvironmentVariable('Path', 'User'), 'User')", bad["check"])
        self.assertIn("export PATH=", upkeep.path_check({"PATH": "/usr/bin"}, "linux")[0]["check"])

    def test_codex_gets_the_same_expectations_and_skills_when_installed(self):
        self.codex.mkdir()
        result = upkeep.setup(self.settings, "Pat Lee", [f"app={self.app}"])
        self.assertEqual(result["agents"], ["Claude", "Codex"])
        self.assertTrue(result["expectations"]["Codex"]["installed"])
        codex, claude = ((d / f).read_text(encoding="utf-8") for d, f in ((self.codex, "AGENTS.md"),
                                                                          (self.claude, "CLAUDE.md")))
        self.assertEqual(codex, upkeep._expectations("Codex"))  # the same expectations; only Claude reads
        self.assertTrue(claude.startswith(codex))                # compact instructions, so only Claude's has them
        self.assertNotIn("# Compact instructions", codex)
        for skill in ("builder", "supervisor"):
            self.assertTrue((self.agents / "skills" / skill / "SKILL.md").is_file())
            self.assertTrue((self.skills / skill / "SKILL.md").is_file())
        checks = upkeep.kit_checks()
        self.assertTrue(all(c["ok"] for c in checks))
        self.assertTrue(any(c["check"].startswith("Codex: skill") for c in checks))
        (self.agents / "skills" / "builder" / "SKILL.md").write_text("old", encoding="utf-8")
        self.assertIn("Codex: skill 'builder' installed and current",
                      [c["check"] for c in upkeep.kit_checks() if not c["ok"]])
        (self.agents / "skills" / "supervisor" / "merge.md").write_text("old", encoding="utf-8")
        self.assertIn("Codex: skill 'supervisor' installed and current",  # a stale reference file counts too
                      [c["check"] for c in upkeep.kit_checks() if not c["ok"]])
        upkeep.install_skills()
        (self.agents / "skills" / "supervisor" / "dropped.md").write_text("old rules", encoding="utf-8")
        self.assertIn("Codex: skill 'supervisor' installed and current",  # so does one a release no longer ships
                      [c["check"] for c in upkeep.kit_checks() if not c["ok"]])
        upkeep.install_skills()  # and the next install removes it
        self.assertFalse((self.agents / "skills" / "supervisor" / "dropped.md").exists())
        self.assertTrue(all(c["ok"] for c in upkeep.kit_checks()))

    def test_the_kit_updates_itself_once_a_day(self):
        upkeep.setup(self.settings, "Pat Lee", [f"app={self.app}"], self.skills)
        s = settings.load(self.settings)
        calls, real = [], (upkeep.latest_version, upkeep.update, upkeep.fetched)
        upkeep.latest_version = lambda fetch=False, kit=None: calls.append("check") or "9.9.9"
        upkeep.update = lambda where, kit=None: calls.append("update") or {"from": "1.4.0", "to": "9.9.9"}
        upkeep.fetched = lambda kit=None: ""
        os.environ.pop("NH_AUTO_UPDATE")
        try:
            self.assertEqual(upkeep.auto_update(self.settings, s, "2026-10-28")["to"], "9.9.9")
            self.assertIsNone(upkeep.auto_update(self.settings, s, "2026-10-28"))  # once a day
            self.assertEqual(calls, ["check", "update"])
            upkeep.latest_version = lambda fetch=False, kit=None: calls.append("check") or \
                upkeep.installed_version(upkeep.installed_kit())  # the laptop's kit, which a worktree's may differ from
            self.assertIsNone(upkeep.auto_update(self.settings, s, "2026-10-29"))  # already current
            self.assertIsNone(upkeep.auto_update(self.settings, s | {"auto_update": False}, "2026-10-30"))
            self.assertEqual(calls, ["check", "update", "check"])
            upkeep.latest_version = lambda fetch=False, kit=None: (_ for _ in ()).throw(core.KitError("offline"))
            self.assertEqual(upkeep.auto_update(self.settings, s, "2026-10-31"),
                             {"error": "offline", "retry": "on the next command"})
        finally:
            os.environ["NH_AUTO_UPDATE"] = "0"
            upkeep.latest_version, upkeep.update, upkeep.fetched = real

    def test_the_daily_check_stamps_the_day_only_once_github_answered(self):
        upkeep.setup(self.settings, "Pat Lee", [f"app={self.app}"], self.skills)
        s, stamp = settings.load(self.settings), self.settings.parent / ".nearly-headless-checked"
        real, answers = (upkeep.fetched, upkeep.latest_version, upkeep.update), ["git fetch failed: offline", ""]
        upkeep.fetched = lambda kit=None: answers.pop(0)
        upkeep.latest_version = lambda fetch=False, kit=None: "99.0.0"
        upkeep.update = lambda where, kit=None: (_ for _ in ()).throw(core.KitError("main checkout has edits"))
        os.environ.pop("NH_AUTO_UPDATE")
        try:
            self.assertEqual(upkeep.auto_update(self.settings, s, "2026-11-08"),
                             {"error": "git fetch failed: offline", "retry": "on the next command"})
            self.assertFalse(stamp.exists())  # one failed check never skips the day
            self.assertEqual(upkeep.auto_update(self.settings, s, "2026-11-08"),
                             {"error": "main checkout has edits", "retry": "tomorrow"})
            self.assertEqual(stamp.read_text(encoding="utf-8"), "2026-11-08")  # checked: once a day from here
            self.assertIsNone(upkeep.auto_update(self.settings, s, "2026-11-08"))
        finally:
            os.environ["NH_AUTO_UPDATE"] = "0"
            upkeep.fetched, upkeep.latest_version, upkeep.update = real

    def test_update_moves_the_laptops_kit_not_a_builders_worktree(self):
        kit = self.tmp / "kit"
        (kit / "wk").mkdir(parents=True)
        (kit / "wk" / "__init__.py").write_text('__version__ = "1.0.0"\n', encoding="utf-8")
        sh(kit, "init", "-q", "-b", "main")
        sh(kit, "-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-q", "--allow-empty", "-m", "i")
        sh(kit, "worktree", "add", "-q", "-b", "side", str(self.tmp / "wt"))
        self.assertEqual(upkeep.installed_kit(self.tmp / "wt").resolve(), kit.resolve())
        self.assertEqual(upkeep.installed_kit(kit).resolve(), kit.resolve())
        plain = self.tmp / "plain"  # not a git checkout: the folder itself
        plain.mkdir()
        self.assertEqual(upkeep.installed_kit(plain), plain)

    def test_doctor_and_command_center(self):
        upkeep.setup(self.settings, "Pat Lee", [f"app={self.app}"], self.skills)
        s = settings.load(self.settings)
        r = work.Repo(s, "app")
        work.add_product(r, "reports", "Monthly reports")
        work.add_piece(r, "reports", "Aged debt view")
        work.ask(r, "reports", "Ship before month end?", options="A (recommended): ship; undo: revert | B: wait",
                 recommended="yes", piece="P1")
        work.inbox_add(r, "reports", "Client asks about totals", "https://mail.example.org/m/1", "Mail")
        with mock.patch.dict(os.environ, {"PATH": str(KIT / "bin") + os.pathsep + os.environ.get("PATH", "")}):
            self.assertTrue(all(f["ok"] for f in upkeep.doctor(s, self.settings) if "gh" not in f["check"]))
        view = dashboard.overview(s)
        self.assertIn("app reports: Ship before month end? (recommended: yes)", view["attention"])
        page = dashboard.render(view)
        for text in ("Waiting on you", "Your work", "Loose ends on this laptop", "Repos and pull requests",
                     "Check GitHub now", 'id="veil"', "How nearly-headless runs your work", "active</span>", "data-fixes",
                     "Building · waiting on you", "waiting ${n} days", "Whole repo", "nothing to fix</button>"):
            self.assertIn(text, page)
        for gone in ('id="v-waiting"', 'id="v-hours"', "Hours not yet billed", "finished this week"):
            self.assertNotIn(gone, page)  # the Waiting on you page and the Hours tab are gone; hours data stays
        data = page_data(page)
        self.assertEqual(data["repos"][0]["client"], "app")  # no client name in settings: the repo's name
        self.assertEqual(data["repos"][0]["prs"][0]["title"], "Aged debt view")
        self.assertEqual(data["repos"][0]["products"][0]["open"][0]["piece"], "Aged debt view")
        self.assertEqual(data["repos"][0]["products"][0]["questions"][0]["options"],  # the decision card's options
                         "A (recommended): ship; undo: revert | B: wait")
        self.assertIn("openAsk", page)  # and a question's pop-up shows them
        self.assertEqual(data["repos"][0]["products"][0]["inbox"], 1)  # each list's unread inbox count
        self.assertIn('in its inbox, unread', page)
        self.assertIn("listNode(l, true)", page)  # every supervisor opens expanded, its pieces in a table
        product = data["repos"][0]["products"][0]
        self.assertEqual([f["line"] for f in product["fixes"]], product["problems"])  # nh check's findings, in parts
        self.assertEqual(data["kinds"]["note"]["heading"], "Notes to tidy")  # the to-fix pop-up's plain words
        self.assertEqual(data["kinds"]["memory"]["who"], "nobody")  # the page shows these as notes, never "to fix"
        self.assertNotIn("The fix:", page)  # and no commands in it: those stay in nh check for the supervisor
        self.assertIn("unbilled", data["repos"][0])
        self.assertIn("+a.rank", page)  # in the supervisor's recorded order
        self.assertNotIn("Finished this week", page)
        self.assertEqual(data["kit"]["version"], __version__)
        self.assertFalse(data["saved"])
        (self.app / ".github" / "workflows" / "ci.yml").write_text(BAD_CI, encoding="utf-8")
        sh(self.app, "switch", "-q", "-c", "side")
        found = {f["check"]: f["ok"] for f in upkeep.doctor(s, self.settings)}
        self.assertFalse(found["app: main checkout on main (found 'side')"])
        self.assertTrue(any(k.startswith("app: CI follows") and not ok for k, ok in found.items()))
        attention = dashboard.overview(s)["attention"]
        self.assertTrue(any("not main" in a for a in attention))
        self.assertTrue(any("not main" in a for a in dashboard.overview(s)["alerts"]))
        self.assertTrue(any("runs on a schedule" in a for a in attention))

    def test_command_center_shows_who_runs_what_and_loose_ends(self):
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.tmp / "origin.git")], check=True,
                       capture_output=True)
        sh(self.app, "remote", "add", "origin", str(self.tmp / "origin.git"))
        sh(self.app, "push", "-q", "-u", "origin", "main")
        upkeep.setup(self.settings, "Pat Lee", [f"app={self.app}"], self.skills)
        s = settings.load(self.settings)
        r = work.Repo(s, "app")
        work.add_product(r, "reports", "Monthly reports")
        work.set_product(r, "reports", "claude://claude.ai/epitaxy/local_sup")
        work.add_piece(r, "reports", "Aged debt view")
        work.set_piece(r, "reports", "P1", state="ready", pr="7", branch="aged-debt",
                       chat="claude://claude.ai/epitaxy/local_bld")
        self.assertEqual(r.products()["reports"], {"line": "Monthly reports",
                                                   "supervisor": "claude://claude.ai/epitaxy/local_sup"})
        sh(self.app, "worktree", "add", "-q", "-b", "aged-debt", str(self.tmp / "busy"))
        sh(self.app, "worktree", "add", "-q", "-b", "stray", str(self.tmp / "stray"))
        (self.tmp / "stray" / "notes.txt").write_text("unsaved\n", encoding="utf-8")
        view = dashboard.overview(s)
        self.assertIn("app reports: P1 Aged debt view is ready for your review (#7)", view["attention"])
        loose = view["repos"][0]["loose"]
        self.assertTrue(any(x.startswith("Working folder stray (stray): no open PR or piece uses it, 1 unsaved file(s)")
                            for x in loose), loose)
        self.assertFalse(any("folder busy" in x for x in loose), loose)  # its PR is open
        self.assertEqual(view["repos"][0]["loose_prs"], [])
        work.note(r, "reports", "P1", add="Owner chose the monthly layout")
        work.set_product(r, "reports", title="Monthly reports")
        data = page_data(dashboard.render(dashboard.overview(s)))
        product = data["repos"][0]["products"][0]
        piece = product["open"][0]
        self.assertEqual((product["supervisor"], product["title"]), ("claude://claude.ai/epitaxy/local_sup", "Monthly reports"))
        self.assertEqual((piece["chat"], piece["pr_info"]["url"]), ("claude://claude.ai/epitaxy/local_bld", "https://x/7"))
        self.assertRegex(piece["latest"], "Owner chose the monthly layout$")
        self.assertTrue(piece["note"].endswith("reports/notes/P1.md"))
        self.assertIn("Owner chose the monthly layout", piece["note_text"])  # the pop-up shows the whole note
        self.assertEqual(len(data["system"]["hooks"]), len(upkeep.HOOKS))  # the nearly-headless page's parts (wk/system.py)
        self.assertTrue(data["system"]["roles"] and data["system"]["settings"])
        self.assertTrue(page_data(dashboard.render(dashboard.overview(s), saved=True))["saved"])
        work.set_product(r, "reports", "")
        self.assertNotIn("supervisor", r.products()["reports"])
        self.assertEqual(r.products()["reports"]["title"], "Monthly reports")  # clearing one field keeps the others
        saved = self.tmp / "snapshot.html"
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["--settings", str(self.settings), "serve", "--save", str(saved)]), 0)
        self.assertTrue(page_data(saved.read_text(encoding="utf-8"))["saved"])

    def test_the_server_answers_from_memory_and_refreshes_in_the_background(self):
        upkeep.setup(self.settings, "Pat Lee", [f"app={self.app}"], self.skills)
        s, built = settings.load(self.settings), []
        real = dashboard.overview
        dashboard.overview = lambda s, fetch=False: built.append(fetch) or {"at": str(len(built))}
        try:
            cache = dashboard.Cache(s, max_age=60)
            self.assertEqual(cache.get()["at"], "1")  # first visit builds it
            self.assertEqual(cache.get()["at"], "1")  # then it comes from memory
            cache.at -= 120  # an old view is served at once while a fresh one is built
            self.assertEqual(cache.get()["at"], "1")
            for _ in range(50):
                if cache.view["at"] == "2":
                    break
                time.sleep(0.05)
            self.assertEqual(cache.view["at"], "2")
            self.assertEqual(cache.get(fetch=True)["at"], "3")  # Check GitHub now always builds afresh
            self.assertEqual(built, [False, False, True])
            self.assertNotIn("busy", cache.get(fetch=True))
            cache.WAIT = 0.1  # a refresh still running holds the lock: Check GitHub now gets the old view, marked busy
            cache.lock.acquire()
            try:
                held = cache.get(fetch=True)
            finally:
                cache.lock.release()
            self.assertTrue(held["busy"])
            self.assertEqual(held["at"], "4")  # the old view: GitHub was not asked
            self.assertIn("had not finished", held["alerts"][0])
            self.assertEqual(built, [False, False, True, True])
        finally:
            dashboard.overview = real

    def test_cost_is_a_tab_and_check_github_shows_progress(self):
        import http.client
        import threading
        from wk import usage
        upkeep.setup(self.settings, "Pat Lee", [f"app={self.app}"], self.skills)
        real = dashboard.overview, usage.Records.at, usage.app_sessions
        records = usage.Records(self.tmp / "cache.json")  # never the real ~/.nearly-headless
        usage.Records.at, usage.app_sessions = (lambda cache=None: records), (lambda: [])
        dashboard.overview = lambda s, fetch=False: {"at": "now", "repos": [], "alerts": [], "attention": [],
                                                     "fetched": fetch}
        server = dashboard.Server(("127.0.0.1", 0), dashboard._handler(dashboard.Cache(settings.load(self.settings)), [""]))
        threading.Thread(target=server.serve_forever, daemon=True).start()

        def get(path):
            c = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=15)
            c.request("GET", path)
            r = c.getresponse()
            return r.status, r.getheader("Location"), r.read().decode()
        try:
            self.assertEqual(get("/usage?weeks=13")[:2], (303, "/#usage/13"))  # old links open the tab
            self.assertEqual(get("/usage")[:2], (303, "/#usage/2"))
            status, _, part = get("/usage/part?weeks=13")
            self.assertEqual(status, 200)
            self.assertNotIn("<html", part)  # a piece of the page, drawn inside the shell
            self.assertIn("href='#usage/2'", part)  # the weeks picker stays inside the shell
            self.assertIn("<b>13 weeks</b>", part)
            page = get("/")[2]
            for marker in ('id="v-usage"', '["usage", "Tokens & cost", ""]', "/usage/part?weeks=",
                           'fetch("/api?fetch=1"', "Checking GitHub", "if (v.busy)", "Another check is still running"):
                self.assertIn(marker, page)
            self.assertTrue(json.loads(get("/api?fetch=1")[2])["fetched"])  # what the button asks for
        finally:
            server.shutdown()
            server.server_close()
            dashboard.overview, usage.Records.at, usage.app_sessions = real

    def test_a_repo_added_to_the_settings_shows_on_the_next_visit_without_a_restart(self):
        upkeep.setup(self.settings, "Pat Lee", [f"app={self.app}"], self.skills)
        real = dashboard.overview
        dashboard.overview = lambda s, fetch=False: {"repos": list(s.get("repos") or {}), "alerts": [], "attention": []}
        try:
            cache = dashboard.Cache(settings.load(self.settings), max_age=60, where=self.settings)
            self.assertEqual(cache.get()["repos"], ["app"])
            good = self.settings.read_text(encoding="utf-8")
            self.settings.write_text(good + f'\n[repos.later]\npath = "{core.posix(self.app)}"\n', encoding="utf-8")
            view = cache.get()  # the next visit, with no restart and a view not yet old
            self.assertEqual(view["repos"], ["app", "later"])
            self.assertEqual(view["alerts"], [])
            self.assertIn("later", cache.settings.get()["repos"])  # the note pages use the same settings
            self.settings.write_text(good + "\n[repos.broken\n", encoding="utf-8")  # a half-typed edit
            view = cache.get()
            self.assertEqual(view["repos"], ["app", "later"])  # the last good settings stay
            self.assertTrue(view["alerts"][0].startswith("Cannot read") and "settings from before" in view["alerts"][0])
            self.assertEqual(view["attention"][0], view["alerts"][0])
            self.settings.write_text(good, encoding="utf-8")  # fixed: the warning goes
            view = cache.get()
            self.assertEqual((view["repos"], view["alerts"]), (["app"], []))
        finally:
            dashboard.overview = real

    def test_serve_never_shares_its_address_and_says_what_is_already_there(self):
        import threading
        from http.server import BaseHTTPRequestHandler

        def stand_in(body: bytes):
            class H(BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, *a):
                    pass
            srv = dashboard.Server(("127.0.0.1", 0), H)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            return srv

        upkeep.setup(self.settings, "Pat Lee", [f"app={self.app}"], self.skills)
        s = settings.load(self.settings)
        old = stand_in(json.dumps({"kit": {"version": "1.0.0"}}).encode())
        port = old.server_address[1]
        try:
            self.assertEqual(dashboard.running(port), {"version": "1.0.0"})
            with self.assertRaises(core.KitError) as caught:
                dashboard.serve(s, port)
            self.assertIn("on the older nearly-headless 1.0.0", str(caught.exception))
            self.assertIn("Ctrl+C", str(caught.exception))
            with self.assertRaises(OSError):  # a second server cannot quietly take the same address
                dashboard.Server(("127.0.0.1", port), old.RequestHandlerClass)
        finally:
            old.shutdown()
            old.server_close()
        same = stand_in(json.dumps({"kit": {"version": __version__}}).encode())
        try:
            with self.assertRaises(core.KitError) as caught:
                dashboard.serve(s, same.server_address[1])
            self.assertIn("already running: open http://127.0.0.1:", str(caught.exception))
        finally:
            same.shutdown()
            same.server_close()
        other = stand_in(b"<html>not a command center</html>")
        try:
            with self.assertRaises(core.KitError) as caught:
                dashboard.serve(s, other.server_address[1])
            self.assertIn("Something else is using", str(caught.exception))
        finally:
            other.shutdown()
            other.server_close()
        self.assertIsNone(dashboard.running(other.server_address[1]))  # nothing there now

    def test_a_command_center_on_old_code_says_it_moves_on_its_own_or_that_the_move_failed(self):
        upkeep.setup(self.settings, "Pat Lee", [f"app={self.app}"], self.skills)
        s, real = settings.load(self.settings), upkeep.installed_version
        upkeep.installed_version = lambda *a: "99.0.0"
        try:
            view = dashboard.overview(s)
        finally:
            upkeep.installed_version = real
        self.assertTrue(any("99.0.0 is installed: it moves to 99.0.0 on its own" in a for a in view["alerts"]), view)
        failed = dashboard.noted(view, "could not restart on it")
        self.assertEqual(failed["alerts"][0], "could not restart on it")
        self.assertFalse(any("on its own" in a for a in failed["alerts"] + failed["attention"]))
        self.assertTrue(failed["kit"]["restart_failed"])
        self.assertFalse(any("on its own" in a for a in dashboard.overview(s)["alerts"]))

    def test_serve_moves_to_a_new_release_on_the_same_address_and_keeps_serving_if_that_fails(self):
        """A real keeper and real workers, on a copy of nearly-headless whose version is changed the way an update would."""
        import socket
        import threading
        from http.server import BaseHTTPRequestHandler
        kit = self.tmp / "kit"
        for part in ("wk", "skills", "templates"):
            shutil.copytree(KIT / part, kit / part, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy(KIT / "AGENTS.md", kit / "AGENTS.md")
        self.settings.write_text('owner = "Pat Lee"\n', encoding="utf-8")

        def api():
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api", timeout=30) as reply:
                return json.loads(reply.read().decode("utf-8"))

        def release(version: str):
            init = kit / "wk" / "__init__.py"
            init.write_text(init.read_text(encoding="utf-8").split('__version__ = "')[0] + f'__version__ = "{version}"\n',
                            encoding="utf-8")

        import urllib.request
        held = dashboard.Server(("127.0.0.1", 0), BaseHTTPRequestHandler)
        port = held.server_address[1]
        cmd = [sys.executable, "-m", "wk", "--settings", str(self.settings), "serve", "--port", str(port), "--worker"]
        keeper = dashboard.Keeper(held.socket, cmd, kit)
        try:
            self.assertTrue(keeper.begin())
            first = keeper.current
            self.assertEqual(api()["kit"]["version"], __version__)
            waiting = socket.create_connection(("127.0.0.1", port), timeout=30)  # a request the old worker is in the middle of
            waiting.sendall(b"GET /api HTTP/1.0\r\n")
            time.sleep(1)
            release("98.0.0")
            keeper.check()  # the version must hold still for one look before the move
            self.assertIs(keeper.current, first)
            keeper.check()
            self.assertEqual(keeper.current.version, "98.0.0")
            self.assertEqual(api()["kit"]["version"], "98.0.0")  # same address, nothing done by hand
            waiting.sendall(b"\r\n")  # the old worker finishes it, then stops
            try:
                answer = b"".join(iter(lambda: waiting.recv(65536), b""))
            except TimeoutError:
                self.fail("the old worker never finished the request it had in hand (waited 30s)")
            finally:
                waiting.close()
            self.assertIn(b"200", answer.split(b"\r\n")[0])
            self.assertEqual(json.loads(answer.split(b"\r\n\r\n", 1)[1])["kit"]["version"], __version__)
            try:
                first.proc.wait(30)
            except subprocess.TimeoutExpired:
                self.fail("the old worker did not stop within 30s of finishing its request")
            with self.assertRaises(OSError):  # the address was never let go
                dashboard.Server(("127.0.0.1", port), BaseHTTPRequestHandler)

            (kit / "wk" / "dashboard.py").write_text("raise SystemExit('broken release')\n", encoding="utf-8")
            release("98.1.0")
            keeper.check()
            keeper.check()  # the new worker fails: the old one keeps serving and says so
            self.assertEqual(keeper.current.version, "98.0.0")
            view = api()
            self.assertEqual(view["kit"]["version"], "98.0.0")
            self.assertTrue(view["kit"]["restart_failed"])
            self.assertIn("98.1.0 is installed, but this command center could not restart", view["alerts"][0])
            keeper.check()  # not tried again at once
            self.assertEqual(keeper.current.version, "98.0.0")
        finally:
            keeper.close()
            held.server_close()
        self.assertIsNotNone(keeper.current.proc.poll())  # Ctrl+C on nh serve leaves no worker behind

    def test_setup_clones_a_missing_repo_and_records_its_client(self):
        source = self.tmp / "source.git"
        subprocess.run(["git", "clone", "--quiet", "--bare", str(self.app), str(source)], check=True, capture_output=True)
        target = self.tmp / "cloned"
        upkeep.setup(self.settings, 'Pat "PL" Lee', [f"acme={target},client=Acme \\ Co,url={source}"], self.skills)
        s = settings.load(self.settings)
        self.assertTrue((target / ".git").exists())
        self.assertEqual(s["owner"], 'Pat "PL" Lee')
        self.assertEqual(s["repos"]["acme"]["client"], "Acme \\ Co")
        with self.assertRaises(core.KitError):
            upkeep.setup(self.tmp / "other.toml", "Pat", [f"x={self.tmp / 'missing'}"])  # no folder and no url
        with self.assertRaises(core.KitError):
            upkeep.setup(self.tmp / "other2.toml", "Pat", [f"x={self.app},colour=blue"])  # unknown option

    def test_the_wwid_profile_is_optional_and_setup_adds_it_once_when_found(self):
        def line():
            return next(f for f in upkeep.doctor(settings.load(self.settings), self.settings)
                        if f["check"].startswith("WWID profile"))

        result = upkeep.setup(self.settings, "Pat Lee", [f"app={self.app}"], self.skills)  # no profile repo
        self.assertEqual(result["wwid_profile"], {"profile": None, "added": False})
        s = settings.load(self.settings)
        self.assertIsNone(settings.profile(s))
        self.assertEqual(reply.required(s), [])  # nothing for the reply check to require
        self.assertEqual(line(), {"ok": True, "check": "WWID profile: not set (optional)"})
        self.assertTrue((self.skills / "wwid" / "SKILL.md").is_file())  # installed, and it says it is off

        before = self.settings.read_text(encoding="utf-8") + '\n[reply]\nrequire = ["Since you wrote:"]\n'
        self.settings.write_text(before, encoding="utf-8")
        beside = self.tmp / "wwid"  # a profile repo beside nearly-headless, as on a laptop that has one
        beside.mkdir()
        sh(beside, "init", "-b", "main")
        (beside / "verdicts.md").write_text("# WWID verdicts\n", encoding="utf-8")
        result = upkeep.setup(self.settings, "", [], self.skills)
        self.assertEqual(result["wwid_profile"], {"profile": core.posix(beside.resolve()), "added": True})
        after = self.settings.read_text(encoding="utf-8")
        self.assertEqual(after.replace(f'wwid_profile = "{core.posix(beside.resolve())}"\n', ""),  # one line, and
                         before.replace('["Since you wrote:"]', '["Since you wrote:", "What would I do"]'))  # the mark
        s = settings.load(self.settings)
        self.assertTrue(core.same_path(settings.profile(s), beside))
        self.assertEqual(s["repos"]["app"]["path"], core.posix(self.app.resolve()))  # still read as before
        self.assertTrue(line()["ok"])
        self.assertFalse(upkeep.setup(self.settings, "", [], self.skills)["wwid_profile"]["added"])
        self.assertEqual(self.settings.read_text(encoding="utf-8"), after)  # once

        (beside / "verdicts.md").unlink()  # set, but no longer a profile repo: doctor says so
        self.assertFalse(line()["ok"])

        other, mine = self.tmp / "other.toml", self.tmp / "profile"  # --profile names it on a new laptop
        mine.mkdir()
        upkeep.setup(other, "Pat Lee", [], self.skills, profile=str(mine))
        self.assertTrue(core.same_path(settings.profile(settings.load(other)), mine))
        with self.assertRaises(core.KitError):
            upkeep.setup(self.tmp / "third.toml", "Pat Lee", [], self.skills, profile=str(self.tmp / "missing"))
        self.assertFalse((self.tmp / "third.toml").exists())  # refused before anything is written

    def test_the_reply_mark_follows_the_wwid_profile_and_the_old_wwfd_skill_goes(self):
        def run(text):
            self.settings.write_text(text, encoding="utf-8")
            got = upkeep.setup(self.settings, "", [], self.skills)["reply_mark"]
            return got, without_setup_lines(self.settings.read_text(encoding="utf-8"))

        old = self.skills / "wwfd"  # the skill as an earlier release installed it
        old.mkdir(parents=True)
        (old / "SKILL.md").write_text("---\nname: wwfd\n---\n", encoding="utf-8")
        mine = self.skills / "mine"  # a skill of the owner's own: never touched
        mine.mkdir()
        (mine / "SKILL.md").write_text("---\nname: mine\n---\n", encoding="utf-8")
        prof = self.tmp / "profile"
        prof.mkdir()
        on = f'owner = "Pat"\nwwid_profile = "{core.posix(prof)}"\n'

        got, text = run(on + '\n[reply]  # mine\nrequire = ["Since you wrote:"]\nrecord = false\n')
        self.assertTrue(got["changed"])
        self.assertEqual(reply.required(tomllib.loads(text)), ["Since you wrote:", "What would I do"])
        self.assertIn("[reply]  # mine\n", text)
        self.assertIs(tomllib.loads(text)["reply"]["record"], False)
        self.assertFalse(old.exists())
        self.assertTrue((mine / "SKILL.md").is_file() and (self.skills / "wwid" / "SKILL.md").is_file())
        self.assertFalse(run(text)[0]["changed"])  # once

        got, text = run(on)  # no [reply] table yet: one is added
        self.assertEqual(text, on + '\n[reply]\nrequire = ["What would I do"]\n')
        got, text = run(on + '\n[reply]\nrecord = false\n')  # a table without require
        self.assertEqual(reply.required(tomllib.loads(text)), ["What would I do"])
        self.assertIs(tomllib.loads(text)["reply"]["record"], False)
        got, text = run(on + '\n[reply]\nrequire = [\n  "Since you wrote:",\n]\n\n[repos.app]\npath = "C:/a"\n')
        self.assertEqual(reply.required(tomllib.loads(text)), ["Since you wrote:", "What would I do"])
        self.assertEqual(tomllib.loads(text)["repos"], {"app": {"path": "C:/a"}})

        off = 'owner = "Pat"\n'  # no profile: no mark, and nothing added when there is none
        got, text = run(off + '\n[reply]\nrequire = ["Since you wrote:", "What would I do"]\n')
        self.assertEqual(text, off + '\n[reply]\nrequire = ["Since you wrote:"]\n')
        self.assertEqual(run(off), ({"mark": False, "changed": False}, off))
        got, text = run(off + 'reply = { require = ["What would I do"] }\n')  # a form setup won't edit
        self.assertFalse(got["changed"])
        self.assertIn("by hand", got["why"])

    def test_setup_adds_a_repo_to_an_existing_settings_file_and_keeps_everything_else(self):
        before = ('# my laptop\nowner = "Pat Lee"\n\n[repos.app]  # the main one\npath = "C:/Projects/app"\n'
                  'client = "Acme"\n\n[reply]\nrequire = ["Since you wrote:"]\n')
        self.settings.write_text(before, encoding="utf-8")
        clones, real = [], upkeep._clone
        upkeep._clone = lambda url, path: (clones.append((url, path)), Path(path).mkdir())  # no network
        try:
            new = self.tmp / "new"
            result = upkeep.setup(self.settings, "", [f"new={new},client=Beta,url=git@x:me/new.git",
                                                      f"app={self.tmp / 'elsewhere'},url=git@x:me/app.git"], self.skills)
            self.assertEqual(result["repos"], {"added": ["new"], "already_there": ["app"]})
            self.assertEqual(clones, [("git@x:me/new.git", str(new))])  # the present repo is never cloned
            after = self.settings.read_text(encoding="utf-8")
            self.assertTrue(without_setup_lines(after).startswith(before))  # nothing rewritten, reordered or dropped
            s = settings.load(self.settings)
            self.assertEqual(s["kit"], core.posix(upkeep.installed_kit()))  # setup's own two top lines
            self.assertRegex(s["pr_sections_from"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
            self.assertEqual(s["repos"]["new"], {"path": core.posix(new.resolve()), "client": "Beta"})
            self.assertEqual(s["repos"]["app"]["path"], "C:/Projects/app")
            again = upkeep.setup(self.settings, "", [f"new={new}"], self.skills)  # idempotent
            self.assertEqual(again["repos"], {"added": [], "already_there": ["new"]})
            self.assertEqual(self.settings.read_text(encoding="utf-8"), after)
            upkeep.setup(self.settings, "", [f"local={self.app}"], self.skills)  # folder exists: no clone
            self.assertEqual(len(clones), 1)
            self.assertEqual(settings.load(self.settings)["repos"]["local"]["path"], core.posix(self.app.resolve()))
        finally:
            upkeep._clone = real

    def test_setup_never_touches_a_settings_file_it_cannot_read(self):
        for broken in ('owner = "Pat\n[repos.app\n', 'owner = "Pat"\nrepos = "app"\n',
                       'repos = {app = {path = "C:/a"}}\n'):  # unreadable, odd shape, and one an addition would break
            self.settings.write_text(broken, encoding="utf-8")
            with self.assertRaises(core.KitError):
                upkeep.setup(self.settings, "", [f"new={self.app}"], self.skills)
            self.assertEqual(self.settings.read_text(encoding="utf-8"), broken)

    def test_setup_clones_nothing_when_the_addition_would_break_the_file(self):
        broken = 'repos = {app = {path = "C:/a"}}\n'  # an inline table can't take [repos.new]
        self.settings.write_text(broken, encoding="utf-8")
        clones, real = [], upkeep._clone
        upkeep._clone = lambda url, path: clones.append((url, path))
        try:
            with self.assertRaises(core.KitError):
                upkeep.setup(self.settings, "", [f"new={self.tmp / 'new'},url=git@x:me/new.git"], self.skills)
        finally:
            upkeep._clone = real
        self.assertEqual(clones, [])  # "nothing changed" is true: no folder was cloned either
        self.assertEqual(self.settings.read_text(encoding="utf-8"), broken)

    def test_setup_refuses_a_repo_name_that_would_land_inside_another(self):
        self.settings.write_text('owner = "Pat"\n\n[repos.app]\npath = "C:/a"\n', encoding="utf-8")
        before = self.settings.read_text(encoding="utf-8")
        for bad in ("app.b", "my repo", "a]b", ""):
            with self.assertRaises(core.KitError):
                upkeep.setup(self.settings, "", [f"{bad}={self.app}"], self.skills)
            with self.assertRaises(core.KitError):
                upkeep.setup(self.tmp / "fresh.toml", "Pat", [f"{bad}={self.app}"], self.skills)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), before)
        self.assertFalse((self.tmp / "fresh.toml").exists())
        upkeep.setup(self.settings, "", [f"My_repo-2={self.app}"], self.skills)  # letters, digits, - and _ are fine
        self.assertIn("My_repo-2", settings.load(self.settings)["repos"])

    def test_the_daily_update_survives_any_failure(self):
        upkeep.setup(self.settings, "Pat Lee", [f"app={self.app}"], self.skills)
        s, real = settings.load(self.settings), (upkeep.latest_version, upkeep.fetched)
        os.environ.pop("NH_AUTO_UPDATE")
        upkeep.fetched = lambda kit=None: ""
        upkeep.latest_version = lambda fetch=False, kit=None: (_ for _ in ()).throw(ValueError("odd output"))
        try:
            self.assertEqual(upkeep.auto_update(self.settings, s, "2026-11-02"),
                             {"error": "odd output", "retry": "on the next command"})
        finally:
            os.environ["NH_AUTO_UPDATE"] = "0"
            upkeep.latest_version, upkeep.fetched = real

    def test_page_data_can_never_break_out_of_the_script(self):
        page = dashboard.render({"owner": "x", "at": "2026-10-27 10:00", "repos": [{"name": "</script><!--<script>"}],
                                 "kit": {}, "alerts": []})
        script = page.split("<script>", 1)[1]
        self.assertNotIn("</script><!--", script.split("const esc", 1)[0])
        self.assertEqual(page_data(page)["repos"][0]["name"], "</script><!--<script>")

    def test_every_branch_is_placed_or_flagged(self):
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.tmp / "origin.git")], check=True, capture_output=True)
        sh(self.app, "remote", "add", "origin", str(self.tmp / "origin.git"))
        sh(self.app, "push", "-q", "-u", "origin", "main")
        upkeep.setup(self.settings, "Pat Lee", [f"app={self.app}"], self.skills)
        s = settings.load(self.settings)
        r = work.Repo(s, "app")
        work.add_product(r, "reports", "Monthly reports")
        work.add_piece(r, "reports", "Export to CSV")
        work.set_piece(r, "reports", "P1", state="building", branch="csv-export")

        def branch(name, commit=True):
            sh(self.app, "switch", "-q", "-c", name, "main")
            if commit:
                (self.app / f"{name}.txt").write_text("x\n", encoding="utf-8")
                sh(self.app, "add", f"{name}.txt")
                sh(self.app, "commit", "-q", "-m", name)
            sh(self.app, "push", "-q", "origin", name)
            sh(self.app, "switch", "-q", "main")
        branch("csv-export")          # on a piece
        branch("aged-debt")           # the stubbed open PR #7, on no piece
        branch("old-merged", False)   # nothing beyond main: merged
        branch("forgotten")           # unmerged, no PR, no piece
        sh(self.app, "fetch", "-q", "origin")
        got = {b["name"]: b for b in dashboard.overview(s)["repos"][0]["branches"]}
        self.assertEqual({k: v["where"] for k, v in got.items()},
                         {"csv-export": "piece", "aged-debt": "pr", "old-merged": "merged", "forgotten": "stray"})
        self.assertEqual(got["csv-export"]["piece"], "reports P1")
        self.assertEqual((got["forgotten"]["ahead"], got["aged-debt"]["pr"]), (1, 7))
        self.assertEqual(list(got)[0], "forgotten")  # what belongs nowhere comes first
        sh(self.app, "switch", "-q", "forgotten")  # counts are kept per pair of commits: a branch that moved counts again
        (self.app / "more.txt").write_text("x\n", encoding="utf-8")
        sh(self.app, "add", "more.txt")
        sh(self.app, "commit", "-q", "-m", "more")
        sh(self.app, "push", "-q", "origin", "forgotten")
        sh(self.app, "switch", "-q", "main")
        sh(self.app, "fetch", "-q", "origin")
        got = {b["name"]: b for b in dashboard.overview(s)["repos"][0]["branches"]}
        self.assertEqual((got["forgotten"]["ahead"], got["csv-export"]["ahead"]), (2, 1))
        (self.app / "main.txt").write_text("x\n", encoding="utf-8")  # main moves: counts against the old main go
        sh(self.app, "add", "main.txt")
        sh(self.app, "commit", "-q", "-m", "main moves")
        sh(self.app, "push", "-q", "origin", "main")
        got = {b["name"]: b for b in dashboard.overview(s)["repos"][0]["branches"]}
        self.assertEqual(got["forgotten"]["ahead"], 2)
        tip = core.git(self.app, "rev-parse", "origin/main")
        self.assertEqual({k[1] for k in dashboard._counted if k[0] == core.posix(self.app)}, {tip})

    def test_setup_adds_the_recall_hook_once_and_keeps_everything_else(self):
        path = self.claude / "settings.json"
        path.parent.mkdir(parents=True)
        mine = {"type": "command", "command": "echo hello"}
        old = {"type": "command", "command": '"D:/old place/work-kit/bin/wk" recall', "timeout": 30}
        bare = {"type": "command", "command": "wk recall"}  # a hook that reached the kit through PATH
        path.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}, "hooks": {
            "Stop": [{"hooks": [mine]}], "UserPromptSubmit": [{"hooks": [mine]}],
            "SessionStart": [{"matcher": "startup", "hooks": [mine]}, {"matcher": "compact", "hooks": [old]},
            {"matcher": "resume", "hooks": [bare]}]}}),
            encoding="utf-8")
        self.assertFalse(all(c["ok"] for c in upkeep.hook_check()))
        result = upkeep.setup(self.settings, "Pat Lee", [f"app={self.app}"], self.skills)
        self.assertEqual(result["hook"]["changed"], True)
        after = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(after["permissions"], {"allow": ["Bash(ls)"]})
        self.assertEqual(after["hooks"]["Stop"], [  # the owner's Stop hook kept, the reply check beside it
            {"hooks": [mine]}, {"hooks": [{"type": "command", "command": upkeep.hook_command(upkeep.REPLY),
                                           "timeout": 30}]}])
        self.assertEqual(result["reply_hook"]["changed"], True)
        self.assertEqual(result["remind_hook"]["changed"], True)
        self.assertEqual(after["hooks"]["UserPromptSubmit"], [  # the owner's kept, the reminder beside it
            {"hooks": [mine]}, {"hooks": [{"type": "command", "command": upkeep.hook_command(upkeep.REMIND),
                                           "timeout": 30}]}])
        self.assertTrue(upkeep.hook_command(upkeep.REMIND).endswith('/bin/nh" reply-check --remind'))
        recall = {"type": "command", "command": upkeep.hook_command(), "timeout": 30}
        self.assertEqual(result["clear_hook"]["changed"], True)
        self.assertEqual((result["start_hook"]["changed"], result["resume_hook"]["changed"]), (True, True))
        self.assertEqual(after["hooks"]["SessionStart"], [  # the old copy of ours is replaced, the owner's kept,
            {"matcher": "startup", "hooks": [mine]},       # and a cleared, started or resumed chat gets its list too
            {"matcher": "compact", "hooks": [recall]}, {"matcher": "clear", "hooks": [recall]},
            {"matcher": "startup", "hooks": [recall]}, {"matcher": "resume", "hooks": [recall]}])
        self.assertTrue(upkeep.hook_command().endswith('/bin/nh" recall'))
        self.assertTrue(upkeep.hook_command().startswith(f'"{core.posix(upkeep.KIT)}/bin/nh"'))  # by path, not PATH
        self.assertTrue(all(c["ok"] for c in upkeep.hook_check()))
        text = path.read_text(encoding="utf-8")
        self.assertEqual(upkeep.install_hook(), {"installed": True, "changed": False, "file": core.posix(path)})
        self.assertEqual(upkeep.install_hook(upkeep.RECALL_CLEAR)["changed"], False)
        self.assertEqual([upkeep.install_hook(x)["changed"] for x in (upkeep.RECALL_START, upkeep.RECALL_RESUME)],
                         [False, False])
        self.assertIn("Claude: supervisors get their list when a chat starts (nh recall hook)",
                      [c["check"] for c in upkeep.hook_check()])
        upkeep.setup(self.settings, "Pat Lee", [], self.skills)
        self.assertEqual(path.read_text(encoding="utf-8"), text)  # a second run changes nothing

    def test_hooks_installed_from_this_kit_count_when_nh_runs_from_one_of_its_worktrees(self):
        kit, other = self.tmp / "kit", self.tmp / "other-kit"
        for folder in (kit, other):
            folder.mkdir()
            sh(folder, "init", "-b", "main")
            sh(folder, "-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "--allow-empty", "-m", "init")
        sh(kit, "worktree", "add", "-q", "-b", "builder", str(self.tmp / "kit-builder"))
        path = self.claude / "settings.json"
        path.parent.mkdir(parents=True)
        real = upkeep.KIT

        def hooks_in(folder):
            command = lambda spec: f'"{core.posix(folder)}/bin/wk" {spec[2]}'  # noqa: E731
            path.write_text(json.dumps({"hooks": {
                "SessionStart": [{"matcher": m, "hooks": [{"type": "command", "command": command(upkeep.RECALL),
                                                           "timeout": 30}]} for m in ("compact", "clear", "startup", "resume")],
                "Stop": [{"hooks": [{"type": "command", "command": command(upkeep.REPLY), "timeout": 30}]}],
                "UserPromptSubmit": [{"hooks": [{"type": "command", "command": command(upkeep.REMIND),
                                                 "timeout": 30}]}]}}),
                encoding="utf-8")
        try:
            upkeep.KIT = self.tmp / "kit-builder"  # a builder's copy of wk, as its own command center runs it
            hooks_in(kit)
            self.assertEqual([c["ok"] for c in upkeep.hook_check()], [True] * 6)
            hooks_in(other)  # hooks of another nearly-headless folder are still missing ones
            self.assertEqual([c["ok"] for c in upkeep.hook_check()], [False] * 6)
        finally:
            upkeep.KIT = real

    def test_a_settings_file_claude_cant_read_is_left_alone(self):
        path = self.claude / "settings.json"
        path.parent.mkdir(parents=True)
        for broken in ('{"hooks": ', '{"hooks": {"SessionStart": {}}}', "[]"):
            path.write_text(broken, encoding="utf-8")
            result = upkeep.install_hook()
            self.assertFalse(result["installed"])
            self.assertEqual(path.read_text(encoding="utf-8"), broken)
            self.assertFalse(all(c["ok"] for c in upkeep.hook_check()))
        path.unlink()
        self.assertTrue(upkeep.install_hook()["installed"])  # no settings file yet: one is made with only the hook
        self.assertEqual(list(json.loads(path.read_text(encoding="utf-8"))), ["hooks"])

    def test_setup_turns_on_phone_push_once_and_keeps_everything_else(self):
        path = self.claude / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}, "theme": "dark"}), encoding="utf-8")
        self.assertFalse(all(c["ok"] for c in upkeep.push_check()))
        result = upkeep.setup(self.settings, "Pat Lee", [f"app={self.app}"], self.skills)
        self.assertEqual((result["push"]["on"], result["push"]["changed"]), (True, True))  # added when missing
        after = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual((after["permissions"], after["theme"]), ({"allow": ["Bash(ls)"]}, "dark"))  # others kept
        self.assertIs(after[upkeep.PUSH], True)
        self.assertTrue(all(c["ok"] for c in upkeep.push_check()))
        text = path.read_text(encoding="utf-8")
        self.assertEqual(upkeep.install_push(), {"on": True, "changed": False, "file": core.posix(path)})
        self.assertEqual(path.read_text(encoding="utf-8"), text)  # untouched when already true

    def test_phone_push_the_owner_turned_off_stays_off(self):
        path = self.claude / "settings.json"
        path.parent.mkdir(parents=True)
        text = json.dumps({upkeep.PUSH: False, "theme": "dark"})
        path.write_text(text, encoding="utf-8")
        self.assertEqual(upkeep.install_push(), {"on": False, "changed": False, "file": core.posix(path)})
        self.assertEqual(path.read_text(encoding="utf-8"), text)
        self.assertEqual(upkeep.push_check(),  # a choice, not a fault: doctor stays green and says so
                         [{"ok": True, "check": "Claude: pushes to your phone when it needs you (off by your choice)"}])
        path.write_text("[]", encoding="utf-8")  # a file Claude can't read is never rewritten
        self.assertEqual((upkeep.install_push()["on"], path.read_text(encoding="utf-8")), (False, "[]"))

    def test_setup_sets_the_compact_window_once_and_keeps_everything_else(self):
        path = self.claude / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}, "model": "opus"}), encoding="utf-8")
        self.assertFalse(upkeep.window_check()[0]["ok"])  # unset: doctor asks for nh setup
        result = upkeep.setup(self.settings, "Pat Lee", [f"app={self.app}"], self.skills)
        self.assertEqual(result["compact_window"],
                         {"window": upkeep.WINDOW_DEFAULT, "changed": True, "file": core.posix(path)})
        after = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual((after["permissions"], after["model"]), ({"allow": ["Bash(ls)"]}, "opus"))  # others kept
        self.assertEqual(after["autoCompactWindow"], 256_000)
        self.assertEqual(upkeep.window_check(), [
            {"ok": True, "check": "Claude: compacts long chats at 256,000 tokens at most"}])
        text = path.read_text(encoding="utf-8")
        self.assertEqual(upkeep.install_window()["changed"], False)
        self.assertEqual(path.read_text(encoding="utf-8"), text)  # a second run changes nothing

    def test_a_compact_window_the_owner_set_is_kept_and_reported(self):
        path = self.claude / "settings.json"
        path.parent.mkdir(parents=True)
        text = json.dumps({"autoCompactWindow": 300000, "theme": "dark"})
        path.write_text(text, encoding="utf-8")
        self.assertEqual(upkeep.install_window(),
                         {"window": 300000, "changed": False, "file": core.posix(path), "yours": True})
        self.assertEqual(path.read_text(encoding="utf-8"), text)
        self.assertEqual(upkeep.window_check(), [{"ok": True, "check": "Claude: compacts long chats at 300,000 "
                                                  "tokens at most (your choice)"}])
        text = json.dumps({"autoCompactWindow": "200k"})  # not a number: still the owner's, kept, never a FIX
        path.write_text(text, encoding="utf-8")
        self.assertEqual(upkeep.install_window()["yours"], True)
        self.assertEqual(path.read_text(encoding="utf-8"), text)
        self.assertEqual(upkeep.window_check(), [{"ok": True, "check": 'Claude: compacts long chats at "200k" '
                                                  "tokens at most (your choice)"}])
        path.write_text("[]", encoding="utf-8")  # a file Claude can't read is never rewritten
        result = upkeep.install_window()
        self.assertEqual((result["window"], path.read_text(encoding="utf-8")), (None, "[]"))
        self.assertIn("by hand", result["why"])
        self.assertFalse(upkeep.window_check()[0]["ok"])

    def fake_gh(self, runs, steps=(), note="", fail=False):
        """gh as GitHub answered on 2026-10-28 when a spending limit stopped CI (ids and links made up)."""
        calls = []

        def gh(cwd, *args):
            calls.append(args[:2])
            if fail:
                raise core.KitError("gh: not found")
            if args[:2] == ("run", "list"):
                return json.dumps(runs)
            if args[:2] == ("run", "view"):
                return json.dumps({"jobs": [{"databaseId": 9, "conclusion": "failure", "steps": list(steps)}]})
            if args[0] == "api":
                return json.dumps([{"annotation_level": "failure", "message": note},
                                   {"annotation_level": "notice", "message": "The ubuntu-latest label will migrate"}])
            return "[]"
        dashboard._gh = gh
        dashboard._ci_runs.clear()
        dashboard.check._runs.clear()
        dashboard._ci_blocked.clear()
        return calls

    def test_ci_that_github_refuses_to_run_is_told_apart_from_a_failing_one(self):
        run = lambda i, branch, c: {"databaseId": i, "status": "completed", "conclusion": c, "headBranch": branch,
                                    "url": f"https://x/runs/{i}", "createdAt": "2026-10-28T20:59:17Z"}  # noqa: E731
        billing = ("The job was not started because recent account payments have failed or your spending limit "
                   "needs to be increased. Please check the 'Billing & plans' section in your settings")
        calls = self.fake_gh([run(3, "p1", "skipped"), run(2, "main", "failure"), run(1, "main", "success")], note=billing)
        self.assertEqual(dashboard.ci_state(self.app, "main"),
                         {"state": "stopped", "text": "CI not running: GitHub billing/limit", "url": "https://x/runs/2"})
        self.assertEqual(dashboard.ci_state(self.app, "main")["state"], "stopped")
        self.assertEqual(len(calls), 3)  # the second look comes from memory
        dashboard.ci_state(self.app, "main", fetch=True)
        self.assertEqual(len(calls), 3)  # nh check read the runs a moment ago in the same Check GitHub now
        runs_seen = dashboard.check._runs
        runs_seen.update({k: (v[0] - dashboard.check.FRESH - 1, v[1]) for k, v in runs_seen.items()})
        dashboard.ci_state(self.app, "main", fetch=True)
        self.assertEqual(calls[3:], [("run", "list")])  # Check GitHub now reads runs again; a finished run's jobs once
        self.fake_gh([run(2, "main", "failure")], steps=[{"name": "tests", "conclusion": "failure"}])
        self.assertEqual(dashboard.ci_state(self.app, "main")["text"], "CI failing on main")
        self.fake_gh([run(2, "p1", "failure"), run(1, "main", "success")], steps=[{"name": "tests"}])
        self.assertEqual(dashboard.ci_state(self.app, "main")["text"], "CI fine")  # a PR's own failure is normal
        self.fake_gh([run(2, "p1", "success"), run(1, "main", "failure")], note=billing)
        self.assertEqual(dashboard.ci_state(self.app, "main")["text"],
                         "CI on main has not run since it was stopped (GitHub billing/limit)")
        self.fake_gh([], fail=True)
        self.assertEqual(dashboard.ci_state(self.app, "main")["state"], "unknown")
        shutil.rmtree(self.app / ".github")
        self.assertIsNone(dashboard.ci_state(self.app, "main"))  # no workflows: nothing to say

    def test_the_page_flags_stopped_ci_and_a_quiet_builder(self):
        upkeep.setup(self.settings, "Pat Lee", [f"app={self.app}"], self.skills)
        s = settings.load(self.settings)
        r = work.Repo(s, "app")
        work.add_product(r, "reports", "Monthly reports")
        work.add_piece(r, "reports", "Aged debt view")
        work.set_piece(r, "reports", "P1", state="building")
        self.fake_gh([{"databaseId": 2, "status": "completed", "conclusion": "failure", "headBranch": "main"}],
                     note="recent account payments have failed")
        real = dashboard.idle_hours
        dashboard.idle_hours = lambda piece, branch_at="": 30
        try:
            data = page_data(dashboard.render(dashboard.overview(s)))
        finally:
            dashboard.idle_hours = real
        self.assertIn("app: CI not running: GitHub billing/limit", data["alerts"])
        self.assertIn("app reports: P1 Aged debt view: the builder has shown no activity for 30h (no commit, PR update "
                      "or note)", data["alerts"])
        self.assertEqual(data["repos"][0]["products"][0]["open"][0]["idle_hours"], 30)

    def test_a_builder_is_quiet_after_twelve_hours_without_a_commit_pr_update_or_note(self):
        now = dashboard.datetime(2026, 10, 29, 12, tzinfo=dashboard.timezone.utc)
        piece = {"updated": "2026-10-27", "pr_info": {"updatedAt": "2026-10-28T20:00:00Z"}}
        self.assertEqual(dashboard.idle_hours(piece, "", now), 16)  # the PR is the newest sign of life
        self.assertIsNone(dashboard.idle_hours(piece, "2026-10-29T05:00:00+01:00", now))  # a commit 8h ago
        self.assertEqual(dashboard.idle_hours(piece, "", now.replace(day=30)), 40)
        self.assertEqual(dashboard.idle_hours(piece, "2026-10-30T01:00:00+01:00", now.replace(day=31)), 36)
        self.assertIsNone(dashboard.idle_hours({}, "", now))  # nothing to go on: say nothing
        note = self.tmp / "P1.md"
        note.write_text("note\n", encoding="utf-8")
        os.utime(note, (now.timestamp() - 2 * 3600,) * 2)
        self.assertIsNone(dashboard.idle_hours(piece | {"note": str(note)}, "", now))  # the note was written 2h ago

    def test_version(self):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["version"]), 0)
        self.assertEqual(out.getvalue().strip(), __version__)
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as done:
            cli.main(["--version"])
        self.assertEqual((done.exception.code, out.getvalue().strip()), (0, __version__))

    def test_a_file_error_is_a_plain_message(self):
        err = io.StringIO()
        real = upkeep.doctor
        upkeep.doctor = lambda s, where: (_ for _ in ()).throw(PermissionError(13, "Access is denied", "x.toml"))
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                self.assertEqual(cli.main(["--settings", str(Path(tempfile.mkdtemp()) / "s.toml"), "doctor"]), 2)
        finally:
            upkeep.doctor = real
        self.assertIn("nh: could not read or write a file: [Errno 13] Access is denied: 'x.toml'", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_a_save_waits_for_a_file_another_program_holds(self):
        path, real, tries = Path(tempfile.mkdtemp()) / "f.txt", os.replace, []

        def busy(times):
            def replace(a, b):
                tries.append(1)
                if len(tries) <= times:
                    raise PermissionError(13, "Access is denied")  # Windows, while another process has it open
                real(a, b)
            return replace
        with mock.patch.object(core.os, "replace", busy(2)), mock.patch.object(core.time, "sleep"):
            core.atomic_text(path, "saved")
        self.assertEqual((path.read_text(encoding="utf-8"), len(tries)), ("saved", 3))
        tries.clear()
        with mock.patch.object(core.os, "replace", busy(99)), mock.patch.object(core.time, "sleep"), \
                self.assertRaises(PermissionError):
            core.atomic_text(path, "never")
        self.assertEqual((path.read_text(encoding="utf-8"), len(tries)), ("saved", core.REPLACE_TRIES))
        self.assertEqual([p.name for p in path.parent.iterdir()], ["f.txt"])  # no temp file left

    def test_no_module_sets_one_name_twice(self):
        """Two top-level settings with one name: the second silently wins (the command center's 5-minute check
        cache once lasted 3 seconds that way)."""
        import ast
        for path in sorted((KIT / "wk").glob("*.py")):
            seen = {}
            for node in ast.parse(path.read_text(encoding="utf-8")).body:
                names = ([t.id for t in node.targets if isinstance(t, ast.Name)] if isinstance(node, ast.Assign)
                         else [node.target.id] if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
                         else [node.name] if isinstance(node, (ast.FunctionDef, ast.ClassDef)) else [])
                for name in names:
                    self.assertNotIn(name, seen, f"{path.name}: {name} set on lines {seen.get(name)} and {node.lineno}")
                    seen[name] = node.lineno
        self.assertEqual((dashboard.PROBLEMS_EVERY, dashboard.VERSION_EVERY), (300.0, 3.0))

    def test_repair_commands_run_with_another_module_broken(self):
        """A broken release must not block its own repair: update, version, recall and reply-check load only what
        they need, and the two hooks still exit 0."""
        from wk import turns
        self.assertEqual((cli.TURN_HOURS, cli.PIECE_STATES, cli.BILL_CHOICES),
                         (turns.HOURS, work.STATES, list(work.BILL)))
        copy = Path(tempfile.mkdtemp()) / "kit"
        shutil.copytree(KIT / "wk", copy / "wk", ignore=shutil.ignore_patterns("__pycache__"))
        with open(copy / "wk" / "work.py", "a", encoding="utf-8") as f:
            f.write("\nthis line is broken(\n")
        settings_file = copy / "s.toml"
        settings_file.write_text('owner = "Pat"\n', encoding="utf-8")

        def run(*argv, stdin=""):
            return subprocess.run([sys.executable, "-P", "-m", "wk", "--settings", str(settings_file), *argv],
                                  input=stdin, capture_output=True, text=True, cwd=copy,
                                  env=os.environ | {"PYTHONPATH": str(copy), "NH_AUTO_UPDATE": "0"})
        for argv in (["version"], ["--version"]):
            done = run(*argv)
            self.assertEqual((done.returncode, done.stdout.strip()), (0, __version__), done.stderr)
        hook = json.dumps({"session_id": "s1", "cwd": str(copy)})
        for argv in (["recall"], ["reply-check"], ["reply-check", "--remind"]):
            done = run(*argv, stdin=hook)
            self.assertEqual(done.returncode, 0, (argv, done.stderr))
            self.assertNotIn("Traceback", done.stderr)
        done = run("update")  # reaches its own check (this copy is no git checkout), not the broken module
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn("must be on main with no edits to update", done.stderr)
        self.assertNotIn("Traceback", done.stderr)
        self.assertNotEqual(run("status").returncode, 0)  # the broken module itself still fails loudly


if __name__ == "__main__":
    unittest.main()
