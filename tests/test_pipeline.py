"""Offline orchestration acceptance and recovery boundaries."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper_plot_library.config import Settings
from paper_plot_library.pipeline import classify, input_spec, mapping_is_current, run_pipeline, validate_catalog
from paper_plot_library.storage import project_lock, read_json, write_json
from test_pipeline_audit import adapters, article_good, Client, record, repo_good


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        write_json(self.root / "data" / "catalog.json", {"schemaVersion": 4, "authorCodeExecuted": False,
                   "papers": [], "inputSpecs": {}, "dimensions": {}})
        self.settings = Settings(repair_limit=0, citation_enrichment=False)

    def run_fixture(self, active=None):
        with patch("paper_plot_library.pipeline.build_site"):
            return run_pipeline(self.root, self.settings, Client(), active or adapters())

    def test_five_daily_distinct_dois_and_catalog_ledger_crash_recovery(self):
        active = adapters(discover=lambda *args: [record(i) for i in range(1, 10)])
        first = self.run_fixture(active)
        self.assertEqual(first["newCount"], 5)
        # Simulate ledger loss after committed catalog admission, not lost catalog.
        write_json(self.root / "data" / "ledger.json", {"days": {}, "candidates": {}})
        second = self.run_fixture(active)
        self.assertEqual(second["newCount"], 0)
        self.assertEqual(second["dailyNewCount"], 5)
        self.assertEqual(len(read_json(self.root / "data" / "catalog.json")["papers"]), 5)

    def test_duplicate_dois_not_admitted_twice_in_same_search(self):
        first = self.run_fixture(adapters(discover=lambda *args: [record(1), record(1)]))
        self.assertEqual(first["newCount"], 1)

    def test_declared_author_repository_must_match_inspected_repository(self):
        other = repo_good()
        other["url"] = "https://github.com/fixture/someone-elses-code"
        first = self.run_fixture(adapters(repository=lambda *args: deepcopy(other)))
        self.assertEqual(first["newCount"], 0)

    def test_new_cases_ai_precede_seed_repair_budget(self):
        write_json(self.root / "data" / "catalog.json", {"authorCodeExecuted": False, "papers": [record(10)], "inputSpecs": {}, "dimensions": {}})
        self.settings.repair_limit = 1
        order = []
        def summary(client, paper, *args):
            order.append(paper["doi"])
            return {"status": "missing_key"}
        self.run_fixture(adapters(summary=summary))
        self.assertEqual(order, [record(1)["doi"], record(10)["doi"]])

    def test_preserve_selected_input_notes_and_separate_all_repo_candidates(self):
        previous = {"tree": "selected.csv", "files": [{"name": "selected.csv", "fields": [["real_selected", "str", "manual", "verified"]]}]}
        p = dict(record(), repository={"inputs": [{"name": "candidate.csv", "fields": [{"name": "age"}], "line": 4}]})
        value = input_spec(p, previous)
        self.assertEqual(value["files"][0]["name"], "selected.csv")
        self.assertEqual(value["automaticInputs"]["files"][0]["name"], "candidate.csv")
        self.assertIn("方向/单位", input_spec(p)["shape"])

    def test_review_is_bound_to_hashes_commit_and_input(self):
        p = dict(record(), article=article_good(), repository=repo_good())
        script_url = "https://github.com/fixture/plot/blob/" + "a" * 40 + "/plot.py"
        p["repository"]["scripts"][0]["url"] = script_url
        p["repository"]["inputs"] = [{"path": "input.csv", "headerSHA256": "c" * 64, "scriptURL": script_url, "fields": [{"name": "age"}]}]
        p["mappingReview"] = {"approved": True, "reviewer": "fixture reviewer", "reviewedAt": "2026-01-01",
            "inputSchemaVerified": True, "commit": "a" * 40, "scriptPath": "plot.py", "scriptSHA256": "b" * 64,
            "figureId": "F1", "figureSHA256": "a" * 64, "inputPath": "input.csv", "inputSHA256": "c" * 64}
        self.assertTrue(mapping_is_current(p))
        p["repository"]["commit"] = "d" * 40
        self.assertFalse(mapping_is_current(p))

    def test_object_form_proposals_preserve_uncertainty_and_do_not_change_live_tags(self):
        p = dict(record(), repository=repo_good(), suggestedGraphForms=[{"id": "density", "evidence": "分布类型未核"}, {"id": "box"}])
        p["repository"]["scripts"][0]["evidence"].append({"text": "sns.boxplot(df); sns.scatter(df)", "line": 2})
        classify(p)
        self.assertEqual(p["suggestedGraphForms"], [{"id": "box"}, {"id": "density", "evidence": "分布类型未核"}, "scatter"])
        self.assertEqual(p["graphForms"], [])

    def test_concurrent_run_is_rejected_without_mutation(self):
        original = (self.root / "data" / "catalog.json").read_bytes()
        with project_lock(self.root):
            with self.assertRaises(RuntimeError):
                self.run_fixture()
        self.assertEqual(original, (self.root / "data" / "catalog.json").read_bytes())

    def test_retraction_removes_existing_images_and_no_paid_summary(self):
        write_json(self.root / "data" / "catalog.json", {"authorCodeExecuted": False, "papers": [dict(record(), article=article_good())], "inputSpecs": {}, "dimensions": {}})
        self.settings.repair_limit = 1
        calls = []
        result = self.run_fixture(adapters(lookup=lambda *args: {"status": "excluded", "isRetracted": True}, summary=lambda *args: calls.append(1)))
        current = read_json(self.root / "data" / "catalog.json")["papers"][0]
        self.assertEqual(current["article"]["figures"], [])
        self.assertEqual(current["automation"]["status"], "excluded")
        self.assertEqual(calls, [])

    def test_no_fake_headers_when_only_code_field_references_observed(self):
        p = dict(record(), repository={"inputs": [{"name": "missing.csv", "fields": [], "observedFieldReferences": [{"name": "age", "line": 7}]}]})
        value = input_spec(p)
        self.assertEqual(value["files"][0]["fields"], [])
        self.assertIn("未核实", value["files"][0]["csv"])

    def test_invalid_catalog_is_not_overwritten(self):
        c = read_json(self.root / "data" / "catalog.json")
        c["papers"] = [record(1), record(1)]
        write_json(self.root / "data" / "catalog.json", c)
        before = (self.root / "data" / "catalog.json").read_bytes()
        with self.assertRaises(ValueError):
            self.run_fixture()
        self.assertEqual(before, (self.root / "data" / "catalog.json").read_bytes())


if __name__ == "__main__":
    unittest.main()
