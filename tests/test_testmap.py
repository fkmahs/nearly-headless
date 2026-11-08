"""nh test: the change -> check map's checks for what a branch changed, never the whole suite."""

import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
KIT = Path(__file__).resolve().parents[1]
os.environ["NH_AUTO_UPDATE"] = "0"

from wk import cli, testmap  # noqa: E402

MAP = """# Contributing

## Change → check
| Change | Check |
|---|---|
| `wk/work.py` | `python -m unittest tests.test_work` |
| `wk/usage.py` | `python -m unittest tests.test_usage tests.test_work` |
| `tests/test_<name>.py` | `python -m unittest tests.test_<name>` |
| `toolkits/<kit>/` | `python -m unittest discover -s toolkits/<kit>/tests` |
| `wk/__init__.py` (version only), `skills/`, `*.md` | none: read the diff |
| `.github/workflows/`, `tests/test_toolkits.py` (what CI runs; `nh refresh` reads this row) | the full run: `python -m unittest discover -s tests` |

## Next section
| `ignored.py` | `python -m nothing` |
"""


class Plan(unittest.TestCase):
    rows = testmap.map_rows(MAP)

    def test_the_map_is_read_from_its_own_section(self):
        self.assertEqual([r["kind"] for r in self.rows], ["run", "run", "run", "run", "none", "full"])
        self.assertEqual(self.rows[-1]["paths"], [".github/workflows/", "tests/test_toolkits.py"])  # not `nh refresh`
        self.assertFalse(any("ignored.py" in r["paths"] for r in self.rows))

    def test_each_changed_file_maps_to_its_row_and_commands_run_once(self):
        p = testmap.plan(["wk/work.py", "wk/usage.py", "tests/test_work.py", "skills/supervisor/SKILL.md",
                          "README.md", "toolkits/netsuite/sdf.py", "toolkits/netsuite/tests/test_sdf.py"], self.rows)
        self.assertEqual(p["commands"], ["python -m unittest tests.test_work tests.test_usage",
                                         "python -m unittest discover -s toolkits/netsuite/tests"])
        self.assertEqual(p["full"], [])
        none = next(m for m in p["matched"] if m["kind"] == "none")
        self.assertEqual(none["files"], ["skills/supervisor/SKILL.md", "README.md"])

    def test_the_ci_row_and_unmapped_files_say_full_run_needed(self):
        p = testmap.plan(["tests/test_toolkits.py", ".github/workflows/tests.yml", "wk/new.py"], self.rows)
        self.assertEqual(len(p["full"]), 3)
        self.assertIn("tests/test_toolkits.py needs the full run", p["full"][0])
        self.assertIn("wk/new.py is in no row of the map", p["full"][2])
        self.assertIn("python -m unittest tests.test_toolkits", p["commands"])  # its own row still runs

    def test_ci_only_rows_are_skipped_and_laptop_only_rows_run_on_the_laptop(self):
        rows = testmap.map_rows("## Change → check\n| Change | Check |\n|---|---|\n"
                                "| `e2e/` | CI only: `npm run e2e` (needs a browser) |\n"
                                "| `db/<name>.py` | laptop-only: `python -m unittest tests.test_<name>` (needs a database) |\n"
                                "| `wk/work.py` | `python -m unittest tests.test_work` |\n")
        self.assertEqual([r["kind"] for r in rows], ["ci", "laptop", "run"])
        p = testmap.plan(["e2e/login.spec.js", "db/orders.py", "wk/work.py"], rows)
        self.assertEqual(p["commands"], ["python -m unittest tests.test_work"])
        self.assertEqual(p["laptop"], ["python -m unittest tests.test_orders"])
        self.assertEqual(p["skipped"], ["skipped, CI runs it: CI only: `npm run e2e` (needs a browser)"])
        self.assertEqual(p["full"], [])  # a CI-only file is mapped, not a full run

    def test_patterns(self):
        rx = testmap._regex
        self.assertTrue(rx("*.md").match("skills/a/SKILL.md"))
        self.assertFalse(rx("skills/").match("toolkits/x/skills/a.md"))
        self.assertTrue(rx(".github/workflows/").match(".github/workflows/tests.yml"))
        self.assertFalse(rx("wk/work.py").match("wk/work.pyc"))
        self.assertEqual(rx("toolkits/<kit>/").match("toolkits/netsuite/a/b.py")["kit"], "netsuite")

    def test_the_kits_own_map_covers_its_files(self):
        rows = testmap.rows_of(KIT)
        files = [p.relative_to(KIT).as_posix() for d in ("wk", "tests", "skills", "templates", "docs")
                 for p in (KIT / d).rglob("*") if p.is_file() and "__pycache__" not in p.parts]
        p = testmap.plan(files, rows)
        self.assertEqual([f for f in p["full"] if "no row" in f], [])


def sh(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


class Run(unittest.TestCase):
    def test_wk_test_runs_the_mapped_checks_for_the_branch(self):
        tmp = Path(tempfile.mkdtemp())
        origin, app = tmp / "origin.git", tmp / "app"
        subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(origin)], check=True)
        app.mkdir()
        sh(app, "init", "-q", "-b", "main")
        sh(app, "config", "user.name", "t")
        sh(app, "config", "user.email", "t@example.org")
        (app / "CONTRIBUTING.md").write_text(MAP.replace("tests.test_work", "ok_check"), encoding="utf-8")
        (app / "ok_check.py").write_text("import unittest\n\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n"
                                         "        print('mapped check ran')\n", encoding="utf-8")
        (app / "wk").mkdir()
        (app / "wk" / "work.py").write_text("x = 1\n", encoding="utf-8")
        sh(app, "add", "-A")
        sh(app, "commit", "-qm", "init")
        sh(app, "remote", "add", "origin", str(origin))
        sh(app, "push", "-q", "-u", "origin", "main")
        sh(app, "checkout", "-qb", "change")
        (app / "wk" / "work.py").write_text("x = 2\n", encoding="utf-8")  # uncommitted counts too
        settings = tmp / "settings.toml"
        settings.write_text("", encoding="utf-8")

        def wk(*argv):
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(out):
                code = cli.main(["--settings", str(settings), "--cwd", str(app), "test", *argv])
            return code, out.getvalue()
        stack = mock.patch.object(testmap, "stack", return_value=None)  # here, not in Docker, on any laptop
        stack.start()
        self.addCleanup(stack.stop)
        code, out = wk("--plan")
        self.assertEqual(code, 0, out)
        self.assertIn("run: python -m unittest ok_check", out)
        self.assertNotIn("mapped check ran", out)
        code, out = wk()
        self.assertEqual(code, 0, out)
        self.assertIn("nh test: passed", out)
        code, out = wk("ok_check", "--local")  # a named module, here
        self.assertEqual(code, 0, out)
        self.assertIn("mapped check ran", out)
        (app / "wk" / "other.py").write_text("y\n", encoding="utf-8")  # new and unmapped, untracked: a scratch file
        (app / "notes.txt").write_text("scratch\n", encoding="utf-8")
        code, out = wk("--plan")
        self.assertEqual(code, 0, out)
        self.assertNotIn("other.py", out)
        self.assertNotIn("notes.txt", out)
        sh(app, "add", "wk/other.py")  # staged: now it counts
        code, out = wk()
        self.assertEqual(code, 1, out)
        self.assertIn("full run needed: wk/other.py is in no row of the map", out)

    def test_a_file_moved_out_of_a_mapped_area_keeps_its_check(self):
        tmp = Path(tempfile.mkdtemp())
        origin, app = tmp / "origin.git", tmp / "app"
        subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(origin)], check=True)
        app.mkdir()
        sh(app, "init", "-q", "-b", "main")
        sh(app, "config", "user.name", "t")
        sh(app, "config", "user.email", "t@example.org")
        (app / "wk").mkdir()
        (app / "wk" / "work.py").write_text("x = 1\n" * 20, encoding="utf-8")
        sh(app, "add", "-A")
        sh(app, "commit", "-qm", "init")
        sh(app, "remote", "add", "origin", str(origin))
        sh(app, "push", "-q", "-u", "origin", "main")
        sh(app, "checkout", "-qb", "move")
        (app / "lib").mkdir()
        sh(app, "mv", "wk/work.py", "lib/work.py")
        sh(app, "commit", "-qm", "move")
        self.assertEqual(testmap.changed(app, "main"), ["lib/work.py", "wk/work.py"])


class FakeDocker:
    """docker as nh test calls it: running or not, with or without the image yet, holding the wk-test images in
    tags. Records every call."""
    def __init__(self, running=True, image=True, tags=(), starts=False, open_app=False):
        self.running, self.image, self.tags, self.calls = running, image, list(tags), []
        self.starts, self.open_app, self.launched = starts, open_app, []  # starts: its engine answers once launched

    def launch(self, argv):
        self.launched.append(argv)
        self.open_app = True
        self.running = self.running or self.starts

    def __call__(self, argv, timeout=0, **kw):
        self.calls.append(argv)
        code, out = 0, ""
        if argv[0] == "tasklist":
            out = "Docker Desktop.exe  1234 Console  1  90,000 K" if self.open_app else "INFO: No tasks are running"
        elif argv[1] == "images":
            out = "\n".join(self.tags)
        elif argv[1] == "rmi":
            self.tags = [t for t in self.tags if t not in argv[2:]]
        elif argv[1] == "info":
            code = 0 if self.running else 1
        elif argv[1] == "image":
            code = 0 if self.image else 1
        elif argv[1] == "build":
            self.image = True
            self.tags.append(argv[argv.index("-t") + 1])
        return subprocess.CompletedProcess(argv, code, out, "")


class Where(unittest.TestCase):
    """Where nh test runs its checks: off Linux with a wk-test.Dockerfile, only in a Linux container (starting Docker
    Desktop when it's off); with --local, on Linux or without the file, here."""
    def setUp(self):
        self.top = Path(tempfile.mkdtemp())
        (self.top / testmap.DOCKERFILE).write_text("FROM python:3.12-slim\n", encoding="utf-8")
        self.app = Path(tempfile.mkdtemp()) / "Docker" / "Docker" / "Docker Desktop.exe"
        self.app.parent.mkdir(parents=True)
        self.app.write_text("", encoding="utf-8")
        self.clock = 0.0

    def where(self, docker, platform="win32", local=False, installed=True):
        out = io.StringIO()
        real = testmap.start_docker

        def start(platform, run):  # the real one, with the fake's launch, clock and Program Files
            def sleep(s):
                self.clock += s
            return real(platform, run, launch=docker.launch, sleep=sleep, clock=lambda: self.clock,
                        env={"ProgramFiles": str(self.app.parents[2])})
        testmap.start_docker = start
        try:
            with redirect_stdout(out):
                got = testmap.where(self.top, local, platform, lambda name: "docker" if installed else None, docker)
        finally:
            testmap.start_docker = real
        return got, out.getvalue()

    def test_docker_present_runs_in_its_image_built_once(self):
        docker = FakeDocker(image=False)
        (tag, why), out = self.where(docker)
        self.assertTrue(tag.startswith("wk-test:"))
        self.assertIn("running in Linux", why)
        self.assertIn("building the Linux image", out)
        self.assertEqual([c[1] for c in docker.calls], ["info", "image", "build", "images"])
        (again, _), out = self.where(docker)  # warm: same tag, no build
        self.assertEqual((again, out), (tag, ""))
        self.assertEqual([c[1] for c in docker.calls].count("build"), 1)
        (self.top / testmap.DOCKERFILE).write_text("FROM python:3.13-slim\n", encoding="utf-8")
        docker.image = False
        (new, _), _ = self.where(docker)
        self.assertNotEqual(new, tag)  # a changed file is a new image
        self.assertEqual(docker.tags, [new])  # and the old one is removed: one kept on disk

    def test_a_new_image_removes_only_older_wk_test_images(self):
        docker = FakeDocker(image=False, tags=["wk-test:aaaaaaaaaaaa", "wk-test:bbbbbbbbbbbb"])
        (tag, _), _ = self.where(docker)
        rmi = next(c for c in docker.calls if c[1] == "rmi")
        self.assertEqual(rmi, ["docker", "rmi", "wk-test:aaaaaaaaaaaa", "wk-test:bbbbbbbbbbbb"])
        self.assertEqual(docker.tags, [tag])
        self.assertTrue(all(c[2] == testmap.IMAGE for c in docker.calls if c[1] == "images"))  # no other images

    def test_docker_stopped_is_started_then_used(self):
        docker = FakeDocker(running=False, starts=True)
        (tag, why), out = self.where(docker)
        self.assertTrue(tag.startswith("wk-test:"))
        self.assertEqual(docker.launched, [[str(self.app)]])  # Docker Desktop from Program Files
        self.assertIn("starting Docker Desktop, waiting up to 120s", out)
        self.assertIn("Docker is running", out)
        self.assertFalse(any(c[1] in ("stop", "kill", "rm") for c in docker.calls if c[0] == "docker"))

    def test_docker_open_but_not_answering_is_waited_for_not_launched_again(self):
        docker = FakeDocker(running=False, open_app=True)
        with self.assertRaises(testmap.DockerNeeded) as stop:
            self.where(docker)
        self.assertEqual(docker.launched, [])
        self.assertEqual(str(stop.exception),
                         "Docker isn't running and couldn't be started: start Docker Desktop, then run nh test again")
        self.assertGreaterEqual(self.clock, testmap.WAIT)  # waited the whole two minutes
        self.assertLess(self.clock, testmap.WAIT + 5)
        self.assertNotIn("run", [c[1] for c in docker.calls if c[0] == "docker"])  # no test ran

    def test_docker_that_wont_start_stops_with_its_own_line(self):
        docker = FakeDocker(running=False)  # launched, but its engine never answers
        with self.assertRaises(testmap.DockerNeeded) as stop:
            self.where(docker)
        self.assertEqual(len(docker.launched), 1)
        self.assertIn("couldn't be started: start Docker Desktop", str(stop.exception))
        self.app.unlink()  # not where its installer puts it: nothing to start
        with self.assertRaises(testmap.DockerNeeded):
            self.where(FakeDocker(running=False))

    def test_macos_opens_docker(self):
        docker = FakeDocker(running=False, starts=True)
        (tag, _), _ = self.where(docker, platform="darwin")
        self.assertTrue(tag)
        self.assertEqual(docker.launched, [["open", "-a", "Docker"]])

    def test_docker_not_installed_stops_with_the_install_line(self):
        with self.assertRaises(testmap.DockerNeeded) as stop:
            self.where(FakeDocker(), installed=False)
        self.assertEqual(str(stop.exception), "Docker isn't installed: winget install Docker.DockerDesktop, open it "
                                              "once, then run nh test again")
        with self.assertRaises(testmap.DockerNeeded) as stop:
            self.where(FakeDocker(), platform="darwin", installed=False)
        self.assertIn("brew install --cask docker", str(stop.exception))

    def test_an_image_that_wont_build_stops_never_runs_here(self):
        docker = FakeDocker(image=False)
        real = docker.__call__

        def failing(argv, timeout=0, **kw):
            if argv[1] == "build":
                docker.calls.append(argv)
                return subprocess.CompletedProcess(argv, 1, "", "ERROR: failed to solve: no such image")
            return real(argv, timeout, **kw)
        with self.assertRaises(testmap.ImageFailed) as stop:
            self.where(failing)
        self.assertEqual(str(stop.exception), "the Linux image didn't build (ERROR: failed to solve: no such image): "
                                              f"fix {testmap.DOCKERFILE}, then run nh test again")
        self.assertEqual(stop.exception.code, testmap.IMAGE_FAILED)
        self.assertNotIn(testmap.IMAGE_FAILED, (0, 1, 2, testmap.DOCKER_NEEDED))
        self.assertNotIn("run", [c[1] for c in docker.calls])  # no check ran, here or in a container

    def test_no_dockerfile_and_no_known_stack_runs_here(self):
        (self.top / testmap.DOCKERFILE).unlink()
        docker = FakeDocker(running=False)
        (tag, why), _ = self.where(docker)
        self.assertEqual((tag, why), (None, f"nh test: running here: this repo has no {testmap.DOCKERFILE} and isn't "
                                            "Node or Python (the kit's default Linux images)"))
        self.assertEqual(docker.calls, [])

    def test_the_stack_is_told_from_the_repos_files(self):
        top = Path(tempfile.mkdtemp())
        self.assertIsNone(testmap.stack(top))
        (top / "tests").mkdir()
        (top / "tests" / "test_a.py").write_text("", encoding="utf-8")
        self.assertEqual(testmap.stack(top), "python")
        (top / "package.json").write_text("{}", encoding="utf-8")
        self.assertEqual(testmap.stack(top), "node")  # Node first
        for name in ("node", "python"):
            self.assertTrue((testmap.IMAGES / f"{name}.Dockerfile").is_file())

    def test_without_its_own_file_a_repo_runs_in_the_kits_image_for_its_stack(self):
        (self.top / testmap.DOCKERFILE).unlink()
        (self.top / "package.json").write_text("{}", encoding="utf-8")
        docker = FakeDocker(image=False, tags=["wk-test-python:aaaaaaaaaaaa", "wk-test-node:bbbbbbbbbbbb"])
        (tag, why), out = self.where(docker)
        self.assertTrue(tag.startswith("wk-test-node:"))
        self.assertIn("from the kit's default node image", why)
        build = next(c for c in docker.calls if c[1] == "build")
        self.assertEqual(Path(build[build.index("-f") + 1]), testmap.IMAGES / "node.Dockerfile")
        self.assertEqual(docker.tags, ["wk-test-python:aaaaaaaaaaaa", tag])  # an older node image goes; python stays
        (again, _), out = self.where(docker)  # cached: the second run builds nothing
        self.assertEqual((again, out), (tag, ""))
        (self.top / "package.json").unlink()
        (self.top / "pyproject.toml").write_text("", encoding="utf-8")
        docker.image = False
        (py, why), _ = self.where(docker)
        self.assertTrue(py.startswith("wk-test-python:"))
        self.assertEqual(docker.tags, [tag, py])  # the stale python image goes, the node one stays

    def test_the_repos_own_file_wins_over_the_kits_default(self):
        (self.top / "package.json").write_text("{}", encoding="utf-8")
        (tag, why), _ = self.where(FakeDocker(image=False))
        self.assertTrue(tag.startswith("wk-test:"))
        self.assertIn(f"from {testmap.DOCKERFILE}", why)

    def test_laptop_only_checks_run_here_and_ci_only_ones_are_skipped(self):
        sh(self.top, "init", "-q", "-b", "main")
        sh(self.top, "config", "user.name", "t")
        sh(self.top, "config", "user.email", "t@example.org")
        (self.top / "CONTRIBUTING.md").write_text(
            "## Change → check\n| Change | Check |\n|---|---|\n| `e2e/` | CI only: `npm run e2e` (needs a browser) |\n"
            "| `db/` | laptop only: `python -m unittest tests.db` (needs a database) |\n"
            "| `app/` | `python -m unittest tests.app` |\n", encoding="utf-8")
        sh(self.top, "add", "-A")
        sh(self.top, "commit", "-qm", "init")
        sh(self.top, "update-ref", "refs/remotes/origin/main", "HEAD")
        for f in ("e2e/a.js", "db/a.py", "app/a.py"):
            (self.top / f).parent.mkdir(exist_ok=True)
            (self.top / f).write_text("x", encoding="utf-8")
        sh(self.top, "add", "-A")
        boxed, here, real = [], [], (testmap.where, testmap.in_container, testmap.core.run)

        def run(argv, *a, **k):
            if argv[0] != "git":
                here.append(argv)
                return subprocess.CompletedProcess(argv, 0, "", "")
            return real[2](argv, *a, **k)
        testmap.where = lambda top, local: ("wk-test:abc", "nh test: running in Linux")
        testmap.in_container = lambda tag, top, c, timeout: boxed.append(c) or subprocess.CompletedProcess(c, 0, "", "")
        testmap.core.run = run
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                code, tail = testmap.run(self.top, {})
        finally:
            testmap.where, testmap.in_container, testmap.core.run = real
        self.assertEqual((code, tail.strip()), (0, "nh test: passed"), out.getvalue())
        self.assertEqual(boxed, ["python -m unittest tests.app"])
        self.assertEqual([a[1:] for a in here], [["-m", "unittest", "tests.db"]])
        self.assertIn("nh test: skipped, CI runs it: CI only: `npm run e2e` (needs a browser)", out.getvalue())
        self.assertIn("(laptop only: here)", out.getvalue())
        self.assertNotIn("npm", "".join(boxed) + str(here))

    def test_wk_test_stops_before_any_check_with_its_own_exit_code(self):
        sh(self.top, "init", "-q", "-b", "main")
        (self.top / "CONTRIBUTING.md").write_text(
            "## Change → check\n| Change | Check |\n|---|---|\n| `*.md` | `python -m unittest tests.x` |\n",
            encoding="utf-8")
        ran, real = [], (testmap.where, testmap.core.run)

        def stopped(top, local):
            raise testmap.DockerNeeded("Docker isn't running and couldn't be started: start Docker Desktop, then "
                                       "run nh test again")
        def run(argv, *a, **k):  # git still works; any other command would be a check running
            if argv[0] != "git":
                ran.append(argv)
            return real[1](argv, *a, **k)
        testmap.where, testmap.core.run = stopped, run
        try:
            with redirect_stdout(io.StringIO()):
                code, tail = testmap.run(self.top, {}, modules=["tests.x"])
        finally:
            testmap.where, testmap.core.run = real
        self.assertEqual((code, ran), (testmap.DOCKER_NEEDED, []))
        self.assertNotIn(code, (0, 1, 2))  # not a pass, a test failure or an error
        self.assertEqual(tail, "nh test: Docker isn't running and couldn't be started: start Docker Desktop, then "
                               "run nh test again")

    def test_local_forces_here_and_linux_changes_nothing(self):
        docker = FakeDocker(running=False)  # Docker off: neither starts it
        self.assertEqual(self.where(docker, local=True)[0], (None, "nh test: running here (--local)"))
        self.assertEqual(self.where(docker, platform="linux")[0], (None, ""))
        self.assertEqual(docker.calls, [])

    def test_the_container_is_removed_offline_and_mounts_the_checkout_read_only(self):
        argv = testmap.container_argv("wk-test:abc", self.top, "py -m unittest tests.test_work", "wk-test-1")
        self.assertEqual(argv[:4], ["docker", "run", "--rm", "--init"])
        self.assertIn("none", argv[argv.index("--network") + 1])
        self.assertTrue(argv[argv.index("--mount") + 1].endswith(",target=/src,readonly"))
        self.assertFalse(any(a in ("-e", "--env", "--env-file") for a in argv))  # no host environment
        self.assertTrue(argv[-1].endswith("cd /work && exec python -m unittest tests.test_work"))

    def test_a_stopped_run_removes_its_container(self):
        calls = []

        def fake(argv, timeout=0, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 124 if argv[1] == "run" else 0, "", "stopped")
        real, testmap.core.run = testmap.core.run, fake
        try:
            done = testmap.in_container("wk-test:abc", self.top, "python -m unittest x", 1)
        finally:
            testmap.core.run = real
        self.assertEqual(done.returncode, 124)
        name = calls[0][calls[0].index("--name") + 1]
        self.assertEqual(calls[1], ["docker", "rm", "-f", name])


if __name__ == "__main__":
    unittest.main()
