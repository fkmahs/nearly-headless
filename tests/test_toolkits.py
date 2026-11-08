"""Each optional toolkit's own tests run with the kit's."""

import unittest
from pathlib import Path

TOOLKITS = Path(__file__).resolve().parents[1] / "toolkits"


def load_tests(loader, tests, pattern):
    for kit in sorted(TOOLKITS.glob("*/tests")):
        tests.addTests(loader.discover(str(kit), top_level_dir=str(kit)))
    return tests
