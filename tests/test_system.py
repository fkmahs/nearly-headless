"""The work-kit page's parts (wk/system.py): hooks, roles and settings come from the real sources, never stale text.
Claude's and Codex's folders are temp folders; the real ~/.claude is never read or written."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["WORKKIT_AUTO_UPDATE"] = "0"

from wk import cli, dashboard, system, upkeep  # noqa: E402

KIT = Path(__file__).resolve().parents[1]


class System(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.claude = self.tmp / "claude"
        self._real = upkeep.claude_dir, upkeep.codex_dir, upkeep.agents_dir
        upkeep.claude_dir, upkeep.codex_dir = (lambda: self.claude), (lambda: self.tmp / "codex")
        upkeep.agents_dir = lambda: self.tmp / "agents"

    def tearDown(self):
        upkeep.claude_dir, upkeep.codex_dir, upkeep.agents_dir = self._real
        shutil.rmtree(self.tmp, ignore_errors=True)

    def setup_laptop(self):
        where = self.tmp / "settings.toml"
        with mock.patch.object(upkeep, "install_keepout", return_value={}):
            upkeep.setup(where, owner="Pat Lee")

    def test_hooks_are_every_hook_setup_writes_read_from_claudes_settings(self):
        self.assertTrue(all(h["on"] is False for h in system.hooks({})))  # nothing installed yet
        self.setup_laptop()
        written = json.loads((self.claude / "settings.json").read_text(encoding="utf-8"))["hooks"]
        pairs = {(event, g.get("matcher"), h["command"].rsplit("/bin/wk\" ", 1)[1])
                 for event, groups in written.items() for g in groups for h in g["hooks"]}
        self.assertEqual(pairs, set(upkeep.HOOKS))  # the page lists exactly what setup installs
        shown = system.hooks({})
        self.assertEqual([(h["event"], h["matcher"] or None, h["command"]) for h in shown],
                         [(e, m, "wk " + c) for e, m, c in upkeep.HOOKS])
        self.assertTrue(all(h["on"] for h in shown))
        # the reminder waits for reply parts; the reply check also records support work, on unless record = false
        self.assertEqual([h["command"] for h in shown if h["idle"]], ["wk reply-check --remind"])
        self.assertEqual([h["command"] for h in system.hooks({"reply": {"record": False}}) if h["idle"]],
                         ["wk reply-check --remind", "wk reply-check"])
        self.assertFalse(any(h["idle"] for h in system.hooks({"reply": {"require": ["Since you wrote:"]}})))
        stop = lambda s: {c["name"]: c["on"] for c in system.hooks(s)[-1]["checks"]}  # noqa: E731
        self.assertEqual(stop({}), {"Required reply parts": False, "Support work recorded": True})
        self.assertEqual(stop({"reply": {"require": ["x"], "record": False}}),
                         {"Required reply parts": True, "Support work recorded": False})
        self.assertEqual([h["checks"] for h in shown[:-1]], [[]] * 5)  # only the Stop hook carries the checks
        self.assertTrue(all(h["when"] and h["what"] for h in shown))  # each has its plain words
        # one hook taken out of Claude's settings shows as missing; a file that can't be read, as unknown
        written["Stop"] = []
        (self.claude / "settings.json").write_text(json.dumps({"hooks": written}), encoding="utf-8")
        self.assertEqual([h["command"] for h in system.hooks({}) if not h["on"]], ["wk reply-check"])
        (self.claude / "settings.json").write_text("[1]", encoding="utf-8")
        self.assertTrue(all(h["on"] is None for h in system.hooks({})))

    def test_roles_read_their_text_from_the_real_skills_and_commands(self):
        self.setup_laptop()
        view = system.overview({})
        roles = {r["id"]: r for r in view["roles"]}
        for want in ("supervisor", "builder", "reviewer", "pm", "routing", "check", "refresh", "release", "queue"):
            self.assertIn(want, roles)
        self.assertEqual([r["id"] for r in view["roles"] if r["state"] == "missing"], [])  # every source is here
        self.assertTrue(all(t in roles for r in roles.values() for t in r["to"]))  # every hand-off goes somewhere
        desc = (KIT / "skills" / "builder" / "SKILL.md").read_text(encoding="utf-8").split("description: ", 1)[1]
        self.assertEqual(roles["builder"]["detail"], desc.split("\n", 1)[0].strip())
        self.assertEqual(roles["builder"]["state"], "current")  # installed by setup, identical to work-kit's copy
        self.assertIn("builders_per_list", roles["queue"]["detail"])  # the approved queue section of moving.md
        self.assertIn("Sonnet", roles["reviewer"]["detail"])  # the review section of merge.md
        check = next(a for a in cli.parser()._actions if a.dest == "cmd").choices["check"]
        self.assertEqual(roles["check"]["detail"], check.description)
        self.assertEqual({k["name"] for k in view["skills"]},
                         {p.name for p in (KIT / "skills").iterdir() if (p / "SKILL.md").is_file()}
                         - {"supervisor", "builder", "project-manager"})  # any other skill still shows

    def test_a_changed_or_missing_source_shows_as_it_is_now(self):
        kit = self.tmp / "kit"
        shutil.copytree(KIT / "skills", kit / "skills")
        shutil.copytree(KIT / "templates", kit / "templates")
        (kit / "skills" / "builder" / "SKILL.md").write_text("---\nname: builder\ndescription: Builds things.\n---\n",
                                                            encoding="utf-8")
        (kit / "skills" / "supervisor" / "routing.md").unlink()
        shutil.rmtree(kit / "skills" / "project-manager")
        (kit / "templates" / "pull_request_template.md").unlink()
        shutil.copytree(KIT / "skills" / "catch-up", kit / "skills" / "brand-new")
        with mock.patch.object(system, "KIT", kit):
            view = system.overview({})
        roles = {r["id"]: r for r in view["roles"]}
        self.assertEqual(roles["builder"]["detail"], "Builds things.")
        for gone in ("routing", "pm", "pr"):  # a guide section, a skill and a template that are gone show as missing
            self.assertEqual((roles[gone]["state"], roles[gone]["detail"]), ("missing", ""), gone)
        self.assertEqual(roles["builder"]["state"], "not current")  # no skills installed in this test's Claude folder
        self.assertIn("brand-new", {k["name"] for k in view["skills"]})

    def test_the_command_center_still_draws_when_the_parts_cant_be_read(self):
        with mock.patch.object(system, "overview", side_effect=OSError("disk gone")), \
                mock.patch.object(upkeep, "latest_version", return_value=""):
            view = dashboard.overview({})
        self.assertEqual(view["system"], {"error": "OSError: disk gone"})
        self.assertIn('"error": "OSError: disk gone"', dashboard.render(view))  # the page says why, the rest still draws

    def test_settings_show_what_the_files_set(self):
        rows = lambda s: {x["name"]: x["value"] for x in system.laptop_settings(s)}  # noqa: E731
        self.assertEqual(rows({})["Builders per list"], "No limit")
        self.assertEqual(rows({"builders_per_list": 3})["Builders per list"], "3 at a time")
        self.assertTrue(rows({"builders_per_list": 0})["Builders per list"].startswith("Can't be read"))
        self.assertIn("off", rows({})["Required reply parts"])
        self.assertEqual(rows({"reply": {"require": ["Since you wrote:"]}})["Required reply parts"], '"Since you wrote:"')
        self.assertEqual(rows({})["Support work recorded"], "On")
        self.assertEqual(rows({"reply": {"record": False}})["Support work recorded"], "Off")
        self.assertEqual(rows({})["Compact window"], "Not set")
        self.setup_laptop()
        self.assertEqual(rows({})["Compact window"], f"{upkeep.WINDOW_DEFAULT:,} tokens")
        self.assertEqual(rows({})["Phone push"], "On")
        self.assertEqual(rows({"auto_update": False, "repos": {"a": {}, "b": {}}})["Repos"], "2")


if __name__ == "__main__":
    unittest.main()
