from pathlib import Path
import shutil
import tempfile
import unittest

from paper_plot_library.release_export import export_site
from paper_plot_library.storage import write_json

PROJECT = Path(__file__).resolve().parents[1]


class ReleaseTests(unittest.TestCase):
    def test_only_whitelisted_files_export_and_no_user_file_deleted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(PROJECT / "web", root / "web")
            write_json(root / "data" / "catalog.json", {"authorCodeExecuted": False, "papers": [], "inputSpecs": {}, "dimensions": {}})
            (root / "site").mkdir()
            user_file = root / "site" / "personal-note.txt"
            user_file.write_text("should not publish, should not delete")
            write_json(root / ".ppl" / "private-secret.json", {"secret": "not public"})
            output = export_site(root)
            self.assertTrue(user_file.exists())
            self.assertFalse((output / "personal-note.txt").exists())
            self.assertFalse((output / "private-secret.json").exists())
            self.assertTrue((output / "index.html").exists())
            first = output
            second = export_site(root)
            self.assertNotEqual(first, second)
            self.assertTrue(first.exists(), "Old exports are recoverable, never recursively deleted")


if __name__ == "__main__":
    unittest.main()
