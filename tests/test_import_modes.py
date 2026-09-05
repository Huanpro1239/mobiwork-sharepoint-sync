from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"


class ImportModeTests(unittest.TestCase):
    def _run_import(self, code: str, *, pythonpath: str | None = None) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        if pythonpath is not None:
            env["PYTHONPATH"] = pythonpath
        return subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_mobiwork_supports_package_import_without_pythonpath(self):
        result = self._run_import("import src.mobiwork")
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_mobiwork_keeps_script_style_import(self):
        result = self._run_import("import mobiwork", pythonpath=str(SRC_DIR))
        self.assertEqual(result.returncode, 0, msg=result.stderr)


if __name__ == "__main__":
    unittest.main()
