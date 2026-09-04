import sys
import unittest
import io
import os
import json
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import production_recovery as recovery  # noqa: E402

class ProductionRecoveryCLITests(unittest.TestCase):
    def test_main_emits_json_to_stdout(self):
        # Create a minimal manifest file
        manifest = {
            "status": "failed",
            "target_date": "2026-09-01",
            "reports": [],
            "image_state": {"status": "success"},
        }
        tmp = Path("output/test_manifest.json")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(manifest), encoding="utf-8")
        os.environ["SMOKE_MANIFEST_PATH"] = str(tmp)

        buf = io.StringIO()
        old_stdout = sys.stdout
        try:
            sys.stdout = buf
            recovery.main()
        finally:
            sys.stdout = old_stdout
        output = buf.getvalue().strip()
        parsed = json.loads(output)
        self.assertIn("eligible", parsed)

if __name__ == "__main__":
    unittest.main()
