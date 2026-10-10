"""Guard against accidentally committing generated MobiWork/SharePoint data.

The check inspects *tracked* paths only, so developer caches are ignored.
It deliberately does not delete files or touch SharePoint production.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIRS = {"output", "runtime", ".runtime", "cache", "input", "feedback"}
PRIVATE_SUFFIXES = {".xlsx", ".xlsm", ".xlsb", ".xls", ".csv", ".parquet", ".sqlite"}
JUNK_SUFFIXES = {".pyc", ".pyo", ".bak", ".orig", ".rej", ".swp", ".swo", ".tmp"}
JUNK_NAMES = {".DS_Store", "Thumbs.db", ".coverage"}


class RepositoryHygieneTests(unittest.TestCase):
    def test_generated_data_and_temporary_files_are_not_tracked(self):
        if not (REPO_ROOT / ".git").exists():
            self.skipTest("Git metadata unavailable (e.g. source archive)")
        try:
            completed = subprocess.run(
                ["git", "ls-files", "-z"], cwd=REPO_ROOT, check=True,
                capture_output=True, text=True, encoding="utf-8",
            )
        except FileNotFoundError:
            self.skipTest("git executable unavailable")
        tracked_paths = [Path(name) for name in completed.stdout.split("\0") if name]
        violations = []
        for path in tracked_paths:
            parts = set(path.parts)
            name = path.name
            is_generated = bool(parts & RUNTIME_DIRS or "__pycache__" in parts)
            is_private_env = name == ".env" or (name.startswith(".env.") and name != ".env.example")
            is_temporary = name in JUNK_NAMES or name.startswith("~$")
            is_export = (path.suffix.casefold() in (PRIVATE_SUFFIXES | JUNK_SUFFIXES)
                         and path.parts[:2] != ("tests", "fixtures"))
            if is_generated or is_private_env or is_temporary or is_export:
                violations.append(str(path))
        self.assertEqual(
            sorted(set(violations)), [],
            "Remove generated data, secrets, or temporary files from the Git index",
        )
