import tempfile
import unittest
from pathlib import Path

from scripts.build_public_mirror import FORBIDDEN_PUBLIC_MARKERS, build_public_mirror


class PublicMirrorBuilderTests(unittest.TestCase):
    def test_build_excludes_private_deployment_files_and_markers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = build_public_mirror(Path(temp_dir) / "mirror")

            self.assertTrue((output / "README.md").is_file())
            self.assertTrue((output / "config/reports.json").is_file())
            self.assertTrue((output / ".github/workflows/ci.yml").is_file())
            self.assertFalse((output / ".github/workflows/mobiwork-sync.yml").exists())
            self.assertFalse((output / "docs/OPERATIONS.md").exists())

            all_text = "\n".join(
                path.read_text(encoding="utf-8", errors="ignore").casefold()
                for path in output.rglob("*")
                if path.is_file()
            )
            for marker in FORBIDDEN_PUBLIC_MARKERS:
                self.assertNotIn(marker.casefold(), all_text)

    def test_public_sharepoint_config_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = build_public_mirror(Path(temp_dir) / "mirror")
            source = (output / "src/sharepoint.py").read_text(encoding="utf-8")

            self.assertIn('os.environ.get("SHAREPOINT_HOST", "")', source)
            self.assertIn('os.environ.get("SHAREPOINT_SITE_PATH", "")', source)
            self.assertIn('os.environ.get("SHAREPOINT_LIBRARY", "")', source)
            self.assertNotIn("vikodacomvn.sharepoint.com", source)


if __name__ == "__main__":
    unittest.main()
