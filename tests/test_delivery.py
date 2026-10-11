"""Independent release acceptance: privacy, current review, state safety and attribution.

All catalogue material here is synthetic and lives in temporary directories.
"""
from copy import deepcopy
from datetime import date, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper_plot_library.config import Settings
from paper_plot_library.pipeline import mapping_is_current, persistent_catalog, refresh_status, run_pipeline
from paper_plot_library.storage import read_json, write_json


PROJECT = Path(__file__).resolve().parents[1]


def current_review_fixture():
    script_url = "https://github.com/fixture/repo/blob/" + "a" * 40 + "/plot.py"
    return {"id": "delivery-fixture", "doi": "10.1234/delivery-fixture", "paper": "Synthetic review acceptance", "runPerformed": False,
            "repository": {"status": "inspected", "commit": "a" * 40,
                "scripts": [{"path": "plot.py", "sha256": "b" * 64, "url": script_url}],
                "inputs": [{"path": "data.csv", "headerSHA256": "c" * 64, "scriptURL": script_url,
                            "fields": [{"name": "age", "kind": "actual_file_header"}]}]},
            "article": {"status": "inspected", "figures": [{"id": "F1", "sha256": "d" * 64,
                                                           "asset": "assets/fixture.png", "reuseStatus": "allowed"}]},
            "mappingReview": {"approved": True, "reviewer": "synthetic reviewer", "reviewedAt": "2026-01-01",
                              "inputSchemaVerified": True, "commit": "a" * 40, "scriptPath": "plot.py", "scriptSHA256": "b" * 64,
                              "figureId": "F1", "figureSHA256": "d" * 64, "inputPath": "data.csv", "inputSHA256": "c" * 64},
            "automation": {"status": "pending", "stages": {"metadata": "matched", "article": "inspected",
                                                           "repository": "inspected", "ai": "generated"}, "errors": []}}


class DeliveryTests(unittest.TestCase):
    def test_persistent_state_strips_abstract_and_source_excerpts_without_mutating_working_evidence(self):
        paper = current_review_fixture()
        paper["abstract"] = "WHOLE_ABSTRACT_DO_NOT_PUBLISH"
        paper["repository"]["scripts"][0]["evidence"] = [{"line": 4, "url": "https://github.com/fixture/repo/blob/commit/plot.py#L4", "text": "AUTHOR_SOURCE_DO_NOT_PUBLISH"}]
        paper["article"]["codeURLEvidence"] = [{"url": "https://github.com/fixture/repo", "text": "AUTHOR_DECLARATION_DO_NOT_PUBLISH"}]
        paper["article"]["figures"] += [{"id": "F2", "reuseStatus": "not_permitted", "caption": "UNLICENSED_CAPTION_DO_NOT_PUBLISH"}]
        paper["ai"] = {"status": "generated", "source_fingerprint": "1" * 64, "summary_fingerprint": "2" * 64}
        source = {"schemaVersion": 4, "authorCodeExecuted": False, "papers": [paper], "inputSpecs": {}}
        result = persistent_catalog(source)
        serialized = json.dumps(result)
        for sentinel in ("WHOLE_ABSTRACT_DO_NOT_PUBLISH", "AUTHOR_SOURCE_DO_NOT_PUBLISH", "AUTHOR_DECLARATION_DO_NOT_PUBLISH", "UNLICENSED_CAPTION_DO_NOT_PUBLISH"):
            self.assertNotIn(sentinel, serialized)
        self.assertIn("AUTHOR_SOURCE_DO_NOT_PUBLISH", json.dumps(source))
        self.assertEqual(result["papers"][0]["ai"]["summary_fingerprint"], "2" * 64)
        self.assertEqual(result["papers"][0]["repository"]["scripts"][0]["evidence"][0]["line"], 4)

    def test_positive_ready_requires_current_review_and_all_successful_stages(self):
        paper = current_review_fixture()
        self.assertTrue(mapping_is_current(paper))
        refresh_status(paper)
        self.assertEqual(paper["automation"]["status"], "ready")
        for stage in ("metadata", "article", "repository", "ai"):
            changed = deepcopy(paper)
            changed["automation"]["stages"][stage] = "unavailable"
            refresh_status(changed)
            self.assertNotEqual(changed["automation"]["status"], "ready")

    def test_missing_or_malformed_hashes_cannot_match_each_other_as_none(self):
        for field in ("commit", "script", "figure", "input"):
            paper = current_review_fixture()
            if field == "commit":
                paper["repository"].pop("commit")
                paper["mappingReview"].pop("commit")
            elif field == "script":
                paper["repository"]["scripts"][0].pop("sha256")
                paper["mappingReview"].pop("scriptSHA256")
            elif field == "figure":
                paper["article"]["figures"][0].pop("sha256")
                paper["mappingReview"].pop("figureSHA256")
            else:
                paper["repository"]["inputs"][0].pop("headerSHA256")
                paper["mappingReview"].pop("inputSHA256")
            with self.subTest(field=field):
                self.assertFalse(mapping_is_current(paper))
        paper = current_review_fixture()
        paper["repository"]["scripts"][0]["sha256"] = "not-a-hash"
        paper["mappingReview"]["scriptSHA256"] = "not-a-hash"
        self.assertFalse(mapping_is_current(paper))

    def test_input_for_another_script_does_not_verify_selected_script(self):
        paper = current_review_fixture()
        paper["repository"]["inputs"][0]["scriptURL"] = "https://github.com/fixture/repo/blob/" + "a" * 40 + "/another.py"
        self.assertFalse(mapping_is_current(paper))

    def test_original_mit_copyright_notice_is_retained_exactly(self):
        license_text = (PROJECT / "LICENSE").read_text()
        self.assertIn("Copyright (c) 2026 yin-huamin\n", license_text)
        self.assertIn("The above copyright notice and this permission notice shall be included", license_text)

    def test_poisoned_ledger_fails_closed_with_clear_validation_and_no_catalog_mutation(self):
        today = date.today().isoformat()
        poisoned = [{"days": [], "candidates": {}}, {"days": {today: {"newDOIs": "DO_NOT_LEAK"}}, "candidates": {}},
                    {"days": {today: {"newDOIs": ["not-a-doi"]}}, "candidates": {}}, {"days": {}, "candidates": []}]
        for ledger in poisoned:
            with tempfile.TemporaryDirectory() as directory, self.subTest(ledger=ledger):
                root = Path(directory)
                write_json(root / "data" / "catalog.json", {"authorCodeExecuted": False, "papers": [], "inputSpecs": {}, "dimensions": {}})
                write_json(root / "data" / "ledger.json", ledger)
                original = (root / "data" / "catalog.json").read_bytes()
                settings = Settings(repair_limit=0)
                class Client:
                    requests = 0
                    post_attempts = 0
                with patch("paper_plot_library.pipeline.build_site"), self.assertRaises(ValueError) as caught:
                    run_pipeline(root, settings, client=Client(), discover_new=False)
                self.assertNotIn("DO_NOT_LEAK", str(caught.exception))
                self.assertEqual(original, (root / "data" / "catalog.json").read_bytes())

    def test_old_ledger_history_is_pruned_instead_of_growing_without_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "data" / "catalog.json", {"authorCodeExecuted": False, "papers": [], "inputSpecs": {}, "dimensions": {}})
            old = date(2000, 1, 1)
            ledger = {"days": {(old + timedelta(days=i)).isoformat(): {"newDOIs": []} for i in range(500)},
                      "candidates": {f"10.1234/history-{i}": {"checkedAt": (old + timedelta(days=i % 500)).isoformat() + "T00:00:00Z", "status": "pending"} for i in range(4000)}}
            write_json(root / "data" / "ledger.json", ledger)
            class Client:
                requests = 0
                post_attempts = 0
            with patch("paper_plot_library.pipeline.build_site"):
                run_pipeline(root, Settings(repair_limit=0), client=Client(), discover_new=False)
            saved = read_json(root / "data" / "ledger.json")
            self.assertLessEqual(len(saved["days"]), 366)
            self.assertLessEqual(len(saved["candidates"]), 3000)

    def test_workflow_installs_only_own_project_and_uses_builtin_push_token(self):
        text = (PROJECT / ".github" / "workflows" / "site.yml").read_text()
        self.assertIn("run: python -m pip install .", text)
        self.assertIn("run: python -m unittest discover -s tests", text)
        self.assertIn("GH_TOKEN: ${{ github.token }}", text)
        self.assertNotIn("pull_request_target", text)
        self.assertNotIn("secrets.GH_TOKEN", text)
        self.assertNotIn("secrets.PAT", text)
        self.assertNotIn("pip install git+", text)
        self.assertNotIn("curl", text)
        self.assertNotIn("set -x", text)
        self.assertNotIn("echo $", text)


if __name__ == "__main__":
    unittest.main()
