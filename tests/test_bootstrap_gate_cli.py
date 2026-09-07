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
        # Capture original env so it can be restored
        orig_env = os.environ.get("SHAREPOINT_DRIVE_ID")
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
        had_bootstrap = "bootstrap_gate" in sys.modules
        try:
            # Inject fake SharePoint client module
            sys.modules["sharepoint_semantic"] = fake_module
            import importlib
            # Import bootstrap_gate module bound to a local name (avoids unused-import lint)
            bootstrap_gate = importlib.import_module("bootstrap_gate")

            buf = io.StringIO()
            old_stdout = sys.stdout
            try:
                sys.stdout = buf
                payload = bootstrap_gate.run()
            finally:
                sys.stdout = old_stdout
                # restore environment variable
                if orig_env is None:
                    os.environ.pop("SHAREPOINT_DRIVE_ID", None)
                else:
                    os.environ["SHAREPOINT_DRIVE_ID"] = orig_env
        finally:
            # restore original sharepoint_semantic module (or remove our fake)
            if orig is not None:
                sys.modules["sharepoint_semantic"] = orig
            else:
                sys.modules.pop("sharepoint_semantic", None)
            # Reload or remove bootstrap_gate to avoid caching test side-effects
            if had_bootstrap:
                import importlib as _importlib
                _importlib.reload(sys.modules["bootstrap_gate"])
            else:
                sys.modules.pop("bootstrap_gate", None)

        output = buf.getvalue().strip()
        # Should be valid JSON and match payload
        parsed = json.loads(output)
        self.assertIsInstance(parsed, dict)
        self.assertEqual(parsed.get("ready"), payload.get("ready"))

if __name__ == "__main__":
    unittest.main()
