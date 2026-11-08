# Contributing to nearly-headless

GitHub CI (`.github/workflows/tests.yml`) runs the whole suite on every ready PR. Locally, run only the checks for what you changed (the builder skill explains the map), from your worktree: `nh test` runs exactly those for your branch against origin/main, and says "full run needed" for the last row or a file no row names (add its row).

On Windows and macOS `nh test` runs the checks only in a Linux container, with Docker Desktop: minutes instead of tens of minutes (test_toolkits about 75s instead of 15 minutes). The image (Python 3.12 and git, from `wk-test.Dockerfile`) is built once, in under a minute, and again only when that file changes. The container gets a read-only view of your checkout, copies it to its own disk, has no network and none of your environment, and is removed when it ends. If Docker isn't running, `nh test` starts Docker Desktop and waits up to about 2 minutes; if it still isn't up, or isn't installed, no checks run and `nh test` exits 4 with one line naming what is needed; if the image won't build, no checks run either and it exits 5. It never quits Docker Desktop. `nh test --local` runs on the laptop; on Linux (CI) nothing changes. `nh test tests.test_work` runs one module, `nh test --full` the whole suite. Exit codes and the summary are the same either way.

Each test builds its own practice git repos in its own temp folder.

nearly-headless stays generic: no client names, tickets, billing figures or personal data in its code, docs, tests or toolkits; use made-up names (Acme, Pat). The WWID profile is not part of nearly-headless: it lives in its own repo, named by `wwid_profile` in the laptop's settings, and everything that uses it works, quietly off, without it.

## Change → check
| Change | Check |
|---|---|
| `wk/reply.py` (its feedback says "missing", which `nh health` counts) | `python -m unittest tests.test_reply tests.test_health` |
| `wk/turns.py` | `python -m unittest tests.test_turns` |
| `wk/work.py` | `python -m unittest tests.test_work` |
| `wk/check.py` | `python -m unittest tests.test_check` |
| `wk/usage.py` | `python -m unittest tests.test_usage tests.test_check tests.test_health` |
| `wk/health.py` | `python -m unittest tests.test_health` |
| `wk/upkeep.py`, `wk/dashboard.py`, `wk/page.html`, `wk/guard.py`, `templates/tests.yml`, adding or renaming a skill | `python -m unittest tests.test_upkeep tests.test_system` |
| `wk/system.py` | `python -m unittest tests.test_system tests.test_upkeep` |
| `wk/keepout.py` | `python -m unittest tests.test_check tests.test_upkeep` |
| `wk/settings.py` | `python -m unittest tests.test_work tests.test_upkeep` |
| `wk/cli.py`, `wk/core.py`, `wk/__main__.py` | the nearest tests: `python -m unittest tests.test_work`, plus the test files for the commands you changed |
| `wk/privacy.py` | `python -m unittest tests.test_privacy tests.test_turns tests.test_upkeep` |
| `bin/nh`, `bin/nh.cmd`, `bin/wk`, `bin/wk.cmd` (the launchers and their forwarding) | `python -m unittest tests.test_privacy tests.test_upkeep` |
| `wk/testmap.py` | `python -m unittest tests.test_testmap tests.test_turns` |
| `wk-test.Dockerfile`, `wk/images/` (the Linux images `nh test` runs in; a changed file builds a new one) | `python -m unittest tests.test_testmap tests.test_work` |
| `tests/test_<name>.py` | `python -m unittest tests.test_<name>` |
| `toolkits/<kit>/` | `python -m unittest discover -s toolkits/<kit>/tests` |
| `wk/__init__.py` (version only), `skills/`, `docs/`, `*.md`, `templates/pull_request_template.md`, `LICENSE` | none: read the diff |
| `.github/workflows/`, `tests/test_toolkits.py` (what CI runs to decide what it tests; `nh refresh` reads this row) | the full run: `python -m unittest discover -s tests` on that sha (the PR's own green CI doesn't count) |
