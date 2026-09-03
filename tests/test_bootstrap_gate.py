import sys
import unittest
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from bootstrap_gate import (  # noqa: E402
    BOOTSTRAP_STATE_PATH,
    evaluate_bootstrap_state,
    read_bootstrap_state,
    require_bootstrap_ready,
)


class FakeStorage:
    def __init__(self, state):
        self.state = state
        self.calls = []

    def download_json(self, drive_id, remote_path):
        self.calls.append((drive_id, remote_path))
        return self.state


class BootstrapGateTests(unittest.TestCase):
    def test_missing_state_is_not_ready(self):
        ready, reason = evaluate_bootstrap_state(None)
        self.assertFalse(ready)
        self.assertIn("missing", reason)

    def test_running_state_is_not_ready(self):
        ready, reason = evaluate_bootstrap_state(
            {"status": "running", "bootstrap_complete": False}
        )
        self.assertFalse(ready)
        self.assertIn("running", reason)

    def test_complete_state_is_ready(self):
        ready, reason = evaluate_bootstrap_state(
            {"status": "complete", "bootstrap_complete": True}
        )
        self.assertTrue(ready)
        self.assertIn("complete", reason)

    def test_read_uses_canonical_state_path(self):
        storage = FakeStorage({"status": "complete", "bootstrap_complete": True})
        ready, _, state = read_bootstrap_state(storage, "drive")
        self.assertTrue(ready)
        self.assertEqual(state["status"], "complete")
        self.assertEqual(storage.calls, [("drive", BOOTSTRAP_STATE_PATH)])

    def test_require_ready_rejects_incomplete_bootstrap(self):
        storage = FakeStorage({"status": "failed", "bootstrap_complete": False})
        with self.assertRaisesRegex(RuntimeError, "full-history bootstrap"):
            require_bootstrap_ready(storage, "drive")


if __name__ == "__main__":
    unittest.main()
