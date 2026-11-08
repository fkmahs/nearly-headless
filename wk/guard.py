"""CI cost guard: checks a repo's GitHub workflows against the nearly-headless template (templates/tests.yml).

CI minutes are a shared, capped budget. A workflow should run on PRs and pushes to the base branch only, skip
draft PRs, cancel superseded runs, cap its runtime, and ignore `.work/` so recording work never runs CI.
"""

from __future__ import annotations

import re
from pathlib import Path


def check(repo: Path) -> list[dict]:
    issues = []
    for wf in sorted((repo / ".github" / "workflows").glob("*.y*ml")):
        text = wf.read_text(encoding="utf-8", errors="replace")
        name = wf.name
        if re.search(r"""['"]\*\*['"]""", text) and "branches" in text:
            issues.append({"workflow": name, "problem": "runs on pushes to every branch"})
        if re.search(r"^\s*schedule\s*:", text, re.M):
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
