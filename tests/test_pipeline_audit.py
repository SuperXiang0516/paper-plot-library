"""Independent orchestration regressions; fixtures never leave this temporary project."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper_plot_library.config import Settings
from paper_plot_library.pipeline import inspect_record, run_pipeline


def record(index=1):
    return {"id": f"fixture-{index}", "doi": f"10.1234/fixture-{index}", "paper": f"Synthetic plotting fixture {index}",
            "journal": "Synthetic journal", "date": "2020-01-01", "year": "2020", "runPerformed": False,
            "automation": {"status": "seed", "lastCheckedAt": "", "stages": {}, "errors": []}}


def article_good():
    return {"status": "inspected", "license": "CC BY 4.0", "version": "1", "codeURLs": ["https://github.com/fixture/plot"],
            "codeURLEvidence": [{"url": "https://github.com/fixture/plot", "scope": "author-code-availability"}],
            "figures": [{"id": "F1", "label": "Figure 1", "caption": "Synthetic caption", "asset": "assets/fixture.png",
                         "sha256": "a" * 64, "reuseStatus": "allowed"}], "supplements": []}


def repo_good():
    return {"status": "inspected", "url": "https://github.com/fixture/plot", "commit": "a" * 40,
            "license": "MIT", "scripts": [{"path": "plot.py", "sha256": "b" * 64,
                                           "evidence": [{"text": "read_csv('input.csv')", "line": 1}]}], "inputs": []}


def adapters(**changes):
    result = {"lookup": lambda client, doi: {"status": "matched", "doi": doi, "isRetracted": False},
              "article": lambda *args: article_good(), "repository": lambda *args: repo_good(),
              "metrics": lambda *args: {"status": "missing_key"}, "citations": lambda *args: {"status": "disabled"},
              "summary": lambda *args: {"status": "generated", "title_zh": "模拟注释", "summary_zh": "仅测试用", "evidence_ids": ["publication"]},
              "discover": lambda *args: [record(1)]}
    result.update(changes)
    return result


class Client:
    requests = 0
    post_attempts = 0


class PipelineAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "data").mkdir()
        self.settings = Settings(repair_limit=0, citation_enrichment=False)

    def catalogue(self, papers):
        (self.root / "data" / "catalog.json").write_text(json.dumps({"schemaVersion": 4, "authorCodeExecuted": False,
                                                                    "papers": papers, "inputSpecs": {}, "dimensions": {}}))

    def test_unverified_metadata_cannot_be_new_admission(self):
        self.catalogue([])
        active = adapters(lookup=lambda *args: {"status": "metadata_unavailable"})
        with patch("paper_plot_library.pipeline.build_site"):
            result = run_pipeline(self.root, self.settings, Client(), active)
        self.assertEqual(result["newCount"], 0)
        catalog = json.loads((self.root / "data" / "catalog.json").read_text())
        self.assertEqual(catalog["papers"], [])

    def test_boolean_review_is_not_proof_of_current_exact_correspondence(self):
        selected = dict(record(), mappingReview={"approved": True})
        result = inspect_record(Client(), selected, self.root, self.settings, adapters())
        self.assertNotEqual(result["automation"]["status"], "ready")

    def test_unverified_metadata_cannot_be_ready_even_with_old_approval(self):
        selected = dict(record(), mappingReview={"approved": True})
        result = inspect_record(Client(), selected, self.root, self.settings,
                                adapters(lookup=lambda *args: {"status": "metadata_unavailable"}))
        self.assertNotEqual(result["automation"]["status"], "ready")

    def test_transient_article_failure_retains_prior_licensed_images_as_stale(self):
        selected = dict(record(), article=article_good())
        result = inspect_record(Client(), selected, self.root, self.settings,
                                adapters(article=lambda *args: {"status": "error", "figures": [], "limitations": ["temporary timeout"]}))
        self.assertTrue(result["article"]["figures"], "transient outage must not discard previously usable assets")
        self.assertNotEqual(result["automation"]["status"], "ready")

    def test_transient_repository_failure_retains_prior_source_evidence_as_stale(self):
        selected = dict(record(), repository=repo_good())
        result = inspect_record(Client(), selected, self.root, self.settings,
                                adapters(repository=lambda *args: {"status": "error", "scripts": [], "inputs": []}))
        self.assertTrue(result["repository"]["scripts"])
        self.assertNotEqual(result["automation"]["status"], "ready")

    def test_first_candidate_failure_does_not_abort_remaining_candidates(self):
        self.catalogue([])
        def lookup(client, doi):
            if doi.endswith("-1"):
                raise RuntimeError("synthetic provider failure")
            return {"status": "matched", "doi": doi}
        active = adapters(lookup=lookup, discover=lambda *args: [record(1), record(2)])
        with patch("paper_plot_library.pipeline.build_site"):
            result = run_pipeline(self.root, self.settings, Client(), active)
        self.assertEqual(result["newCount"], 1)
        catalog = json.loads((self.root / "data" / "catalog.json").read_text())
        self.assertEqual(catalog["papers"][0]["doi"], record(2)["doi"])

    def test_unadmitted_candidates_do_not_consume_paid_summary_budget(self):
        self.catalogue([])
        calls = []
        def summary(client, selected, *args):
            calls.append(selected["doi"])
            return {"status": "generated"}
        active = adapters(discover=lambda *args: [record(i) for i in range(1, 9)], summary=summary)
        with patch("paper_plot_library.pipeline.build_site"):
            result = run_pipeline(self.root, self.settings, Client(), active)
        self.assertEqual(result["newCount"], 5)
        self.assertLessEqual(len(calls), 5)

    def test_absent_code_statement_does_not_trigger_paid_summary(self):
        self.catalogue([])
        calls = []
        no_statement = article_good()
        no_statement["codeURLEvidence"] = []
        def summary(*args):
            calls.append(True)
            return {"status": "generated"}
        active = adapters(article=lambda *args: deepcopy(no_statement), summary=summary)
        with patch("paper_plot_library.pipeline.build_site"):
            result = run_pipeline(self.root, self.settings, Client(), active)
        self.assertEqual(result["newCount"], 0)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
