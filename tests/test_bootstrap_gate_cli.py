import sys
import unittest
import io
import os
import json
import types
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


class BootstrapGateCLITests(unittest.TestCase):
    def test_run_emits_json_to_stdout(self):
        # Ensure env drive id is set so code path uses download_json
        os.environ["SHAREPOINT_DRIVE_ID"] = "drive"

        # Prepare a fake sharepoint_semantic module for this test only
        fake_module = types.ModuleType("sharepoint_semantic")
        class FakeSemanticClient:
            @staticmethod
            def from_env():
                return FakeSemanticClient()
            def download_json(self, drive_id, remote_path):
                return {"status": "complete", "bootstrap_complete": True}
            def get_site_id(self):
                return "site"
            def get_drive_id(self, site_id):
                return "drive"
        fake_module.SemanticSharePointClient = FakeSemanticClient

        orig = sys.modules.get("sharepoint_semantic")
        try:
            sys.modules["sharepoint_semantic"] = fake_module
            import importlib
            if "bootstrap_gate" in sys.modules:
                importlib.reload(sys.modules["bootstrap_gate"])
            else:
                import bootstrap_gate  # type: ignore
            buf = io.StringIO()
            old_stdout = sys.stdout
            try:
                sys.stdout = buf
                payload = sys.modules["bootstrap_gate"].run()
            finally:
                sys.stdout = old_stdout
        finally:
            # restore original module (or remove our fake)
            if orig is not None:
                sys.modules["sharepoint_semantic"] = orig
            else:
                del sys.modules["sharepoint_semantic"]

        output = buf.getvalue().strip()
        # Should be valid JSON and match payload
        parsed = json.loads(output)
        self.assertIsInstance(parsed, dict)
        self.assertEqual(parsed.get("ready"), payload.get("ready"))

if __name__ == "__main__":
    unittest.main()
