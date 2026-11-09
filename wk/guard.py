"""CI cost guard: checks a repo's GitHub workflows against the nearly-headless template (templates/tests.yml).

CI minutes are a shared, capped budget. A workflow should run on PRs and pushes to the base branch only, skip
draft PRs, cancel superseded runs, cap its runtime, and ignore `.work/` so recording work never runs CI. A schedule is
allowed only for a small check: at most once a day, with every job capped at SCHEDULED_MINUTES (the daily privacy
sweep, say).
"""

from __future__ import annotations

import re
from pathlib import Path

SCHEDULED_MINUTES = 5  # a scheduled workflow's jobs each stop within this many minutes


def cheap_schedule(text: str) -> bool:
    """Every cron runs at most once a day (a fixed minute and hour) and every job has a timeout of SCHEDULED_MINUTES
    or less."""
    crons = re.findall(r"cron\s*:\s*['\"]([^'\"]+)['\"]", text)
    caps = [int(m) for m in re.findall(r"timeout-minutes\s*:\s*(\d+)", text)]
    jobs = len(re.findall(r"^\s*runs-on\s*:", text, re.M))
    daily = all(len(c.split()) == 5 and c.split()[0].isdigit() and c.split()[1].isdigit() for c in crons)
    return bool(crons) and daily and len(caps) >= jobs and all(m <= SCHEDULED_MINUTES for m in caps)


def check(repo: Path) -> list[dict]:
    issues = []
    for wf in sorted((repo / ".github" / "workflows").glob("*.y*ml")):
        text = wf.read_text(encoding="utf-8", errors="replace")
        name = wf.name
        if re.search(r"""['"]\*\*['"]""", text) and "branches" in text:
            issues.append({"workflow": name, "problem": "runs on pushes to every branch"})
        if re.search(r"^\s*schedule\s*:", text, re.M) and not cheap_schedule(text):
            issues.append({"workflow": name, "problem": "runs on a schedule"})
        if "concurrency:" not in text:
            issues.append({"workflow": name, "problem": "no concurrency group: superseded runs keep spending"})
        if "timeout-minutes" not in text:
            issues.append({"workflow": name, "problem": "no timeout-minutes"})
        if re.search(r"^\s*push\s*:", text, re.M) and ".work/" not in text:
            issues.append({"workflow": name, "problem": "pushes that only record work (.work/) run CI"})
        if "pull_request" in text and "draft" not in text:
            issues.append({"workflow": name, "problem": "draft PRs run CI"})
    return issues
