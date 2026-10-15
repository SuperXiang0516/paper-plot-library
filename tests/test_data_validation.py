"""Real checked-in data regression plus strict, read-only preflight fixtures."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper_plot_library.data_validation import DataValidationError, validate_data


PROJECT = Path(__file__).resolve().parents[1]


class DataValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "data").mkdir()
        self.values = {
            "catalog.json": {"authorCodeExecuted": False, "papers": [{"id": "synthetic", "doi": "10.1234/synthetic"}]},
            "ledger.json": {"days": {}, "candidates": {}, "extraHistory": {"preserve": True}},
            "updates.json": [],
        }
        for name, value in self.values.items():
            self.write(name, value)

    def write(self, name, value):
        (self.root / "data" / name).write_text(json.dumps(value), encoding="utf-8")

    def test_actual_checked_in_collection_state_is_valid_not_only_synthetic_fixtures(self):
        result = validate_data(PROJECT)
        self.assertEqual(result["status"], "valid")
        self.assertGreater(result["papers"], 0)
        self.assertEqual((result["httpRequests"], result["aiAttempts"], result["filesWritten"]), (0, 0, 0))

    def test_valid_files_and_unknown_ledger_extras_are_read_without_changes(self):
        paths = list((self.root / "data").iterdir())
        before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
        with patch("paper_plot_library.config.load_settings", side_effect=AssertionError("no settings")), \
                patch("paper_plot_library.http.HttpClient.get_json", side_effect=AssertionError("no HTTP")), \
                patch("paper_plot_library.http.HttpClient.post_json", side_effect=AssertionError("no AI")), \
                patch("paper_plot_library.storage.write_json", side_effect=AssertionError("no write")):
            result = validate_data(self.root)
        self.assertEqual(result["papers"], 1)
        self.assertEqual(before, {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths})
        self.assertFalse((self.root / ".ppl").exists())

    def test_each_required_file_is_mandatory(self):
        for name in self.values:
            with self.subTest(name=name):
                path = self.root / "data" / name
                saved = path.read_bytes()
                path.unlink()
                with self.assertRaisesRegex(DataValidationError, "data/" + name.replace(".", r"\.") + ":1:1:"):
                    validate_data(self.root)
                path.write_bytes(saved)

    def test_json_syntax_error_reports_file_line_and_column_without_raw_data(self):
        (self.root / "data" / "catalog.json").write_text('{\n  "secret": DO_NOT_PRINT\n}', encoding="utf-8")
        with self.assertRaises(DataValidationError) as caught:
            validate_data(self.root)
        self.assertIn("data/catalog.json:2:13:", str(caught.exception))
        self.assertNotIn("DO_NOT_PRINT", str(caught.exception))

    def test_merge_conflicts_fail_at_exact_line_before_json_decode(self):
        for marker in ("<<<<<<< HEAD", "=======", ">>>>>>> remote", "||||||| base"):
            with self.subTest(marker=marker):
                (self.root / "data" / "updates.json").write_text("[\n" + marker + "\n]\n", encoding="utf-8")
                with self.assertRaisesRegex(DataValidationError, "data/updates.json:2:1:.*Git"):
                    validate_data(self.root)

    def test_duplicate_keys_and_nonfinite_constants_are_rejected_in_every_file(self):
        for name in self.values:
            original = (self.root / "data" / name).read_bytes()
            for invalid in ('{"same":1,"same":2}', '{"nested":{"same":1,"same":2}}',
                            '[NaN]', '[Infinity]', '[-Infinity]', '[1e9999]', '[-1e9999]'):
                with self.subTest(name=name, invalid=invalid):
                    (self.root / "data" / name).write_text(invalid, encoding="utf-8")
                    with self.assertRaises(DataValidationError):
                        validate_data(self.root)
            (self.root / "data" / name).write_bytes(original)

    def test_shapes_and_existing_catalog_invariants_are_checked(self):
        invalid_catalogs = [[], {"authorCodeExecuted": True, "papers": []},
                            {"authorCodeExecuted": False, "papers": [None]}]
        duplicate = deepcopy(self.values["catalog.json"])
        duplicate["papers"] *= 2
        invalid_catalogs.append(duplicate)
        for catalog in invalid_catalogs:
            with self.subTest(catalog=catalog):
                self.write("catalog.json", catalog)
                with self.assertRaisesRegex(DataValidationError, "data/catalog.json:1:1:"):
                    validate_data(self.root)
        self.write("catalog.json", self.values["catalog.json"])
        for name, invalid in (("ledger.json", []), ("updates.json", {})):
            with self.subTest(name=name):
                self.write(name, invalid)
                with self.assertRaisesRegex(DataValidationError, "data/" + name.replace(".", r"\.") + ":1:1:"):
                    validate_data(self.root)
                self.write(name, self.values[name])

    def test_non_utf8_file_has_clear_error(self):
        (self.root / "data" / "ledger.json").write_bytes(b"\xff")
        with self.assertRaisesRegex(DataValidationError, "data/ledger.json:1:1:.*UTF-8"):
            validate_data(self.root)

    def test_workflow_preflight_is_before_regressions_and_collection(self):
        workflow = (PROJECT / ".github" / "workflows" / "site.yml").read_text(encoding="utf-8")
        install = workflow.index("run: python -m pip install .")
        preflight = workflow.index("run: python -m paper_plot_library validate-data")
        tests = workflow.index("run: python -m unittest discover -s tests")
        collect = workflow.index("run: python scripts/cloud_collect.py")
        self.assertLess(install, preflight)
        self.assertLess(preflight, tests)
        self.assertLess(tests, collect)
        self.assertIn("run: python -m unittest discover -s tests --buffer", workflow)
