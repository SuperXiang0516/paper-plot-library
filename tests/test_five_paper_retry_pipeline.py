"""Five-paper collection with bounded repeated AI attempts; all traffic is fake."""
from collections import Counter
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from paper_plot_library.config import Settings
from paper_plot_library.http import HttpClient
from paper_plot_library.pipeline import run_pipeline
from paper_plot_library.storage import read_json, write_json
from test_bootstrap import NOW, RASTER
from test_bootstrap_pipeline import materials
from test_enrichment import ai_payload
from test_http import Response
from test_pipeline_audit import adapters, record


class PaperRetryOpener:
    def __init__(self, invalid_before_success=2, fatal_status=None, always_invalid=False):
        self.invalid_before_success = invalid_before_success
        self.fatal_status = fatal_status
        self.always_invalid = always_invalid
        self.methods = []
        self.calls_by_doi = Counter()

    def open(self, request, timeout):
        self.methods.append(request.get_method())
        if request.data is None:
            return Response(b'{"ok":true}', url=request.full_url)
        payload = json.loads(request.data)
        facts = json.loads(next(item["content"] for item in payload["messages"] if item["role"] == "user"))
        doi = facts["doi"]
        self.calls_by_doi[doi] += 1
        if self.fatal_status:
            raise HTTPError(request.full_url, self.fatal_status, "synthetic fatal status", {}, io.BytesIO(b"private provider body"))
        response = ai_payload(evidence_ids=["publication"])
        if self.always_invalid or self.calls_by_doi[doi] <= self.invalid_before_success:
            response["choices"][0]["message"]["content"] = "{}"
        return Response(json.dumps(response, ensure_ascii=False).encode(), url=request.full_url)


class FivePaperRetryPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ppl-five-retry-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "data" / "assets").mkdir(parents=True)
        (self.root / "data" / "assets" / "fixture.png").write_bytes(RASTER)
        self.settings = Settings(initialize_existing_first=True, bootstrap_batch_limit=5,
            max_papers_per_run=5, max_ai_calls=15, max_ai_attempts_per_paper=3,
            max_requests=150, llm_api_key="offline-test-key", llm_model="offline-test-model",
            citation_enrichment=False)
        self.sources = Counter()
        self.discoveries = []
        source = materials()
        def copied(stage, value):
            def read(*args):
                self.sources[stage] += 1
                return deepcopy(value)
            return read
        self.active = adapters(lookup=copied("metadata", {"status": "matched"}),
            article=copied("article", source["article"]),
            repository=copied("repository", source["repository"]),
            metrics=copied("metrics", source["metrics"]),
            discover=lambda *args: self.discoveries.append(True) or [record(i) for i in range(100, 110)])
        self.active.pop("summary")
        self.catalog([dict(record(i), figure="Figure 1") for i in range(1, 11)])

    def catalog(self, papers):
        write_json(self.root / "data" / "catalog.json", {"schemaVersion": 4,
            "authorCodeExecuted": False, "papers": papers, "inputSpecs": {}, "dimensions": {}})

    def run_fixture(self, opener=None, active=None, **kwargs):
        kwargs.setdefault("mode", "backfill")
        self.opener = opener or PaperRetryOpener()
        client = HttpClient(self.root / ".ppl" / "cache", self.settings.max_requests,
            max_post_attempts=self.settings.max_ai_calls, retries=3,
            opener=self.opener, sleep=lambda delay: None)
        with patch("paper_plot_library.pipeline.build_site"), patch("paper_plot_library.pipeline.stamp", return_value=NOW):
            return run_pipeline(self.root, self.settings, client, active or self.active, **kwargs)

    def papers(self):
        return read_json(self.root / "data" / "catalog.json")["papers"]

    def test_five_papers_each_invalid_twice_then_success_without_repeating_sources(self):
        report = self.run_fixture()
        self.assertEqual((report["checkedCount"], report["newCount"]), (5, 0))
        self.assertEqual(self.sources, Counter(metadata=5, article=5, repository=5, metrics=5))
        self.assertEqual(set(self.opener.calls_by_doi.values()), {3})
        self.assertEqual(self.opener.methods.count("POST"), 15)
        self.assertEqual((report["aiAttempts"], report["aiCasesAttempted"], report["aiRetriedCaseCount"], report["aiRetrySuccessCount"]), (15, 5, 5, 5))
        self.assertEqual((report["paperBatchLimit"], report["maxAiAttemptsPerPaper"]), (5, 3))
        self.assertEqual(report["bootstrap"]["completeCount"], 5)
        self.assertEqual(self.discoveries, [])

    def test_fatal_key_permission_or_payment_stops_entire_paid_lane(self):
        for status, label in ((401, "authentication_error"), (402, "payment_required"), (403, "permission_denied")):
            with self.subTest(status=status):
                self.catalog([dict(record(i), figure="Figure 1") for i in range(1, 6)])
                report = self.run_fixture(PaperRetryOpener(fatal_status=status))
                self.assertEqual((report["checkedCount"], report["aiAttempts"]), (5, 1))
                self.assertEqual(report["aiCasesAttempted"], 1)
                self.assertEqual(report["aiRetriedCaseCount"], 0)
                self.assertEqual(report["aiStoppedStatus"], label)
                self.assertEqual(self.opener.methods, ["POST"])
                deferred = [p for p in self.papers() if p["automation"]["stages"]["ai"] == "deferred"]
                self.assertEqual(len(deferred), 4)
                self.assertTrue(all(p["ai"]["ai_attempts"] == 0 for p in deferred))
                self.assertNotIn("private provider body", json.dumps(read_json(self.root / "data" / "updates.json")))

    def test_validation_failure_stops_at_three_and_does_not_falsely_complete(self):
        report = self.run_fixture(PaperRetryOpener(always_invalid=True))
        self.assertEqual(report["aiAttempts"], 15)
        self.assertEqual(report["aiRetrySuccessCount"], 0)
        self.assertEqual(report["bootstrap"]["completeCount"], 0)
        checked = [p for p in self.papers() if p.get("automation", {}).get("lastCheckedAt")]
        self.assertEqual(len(checked), 5)
        self.assertTrue(all(p["ai"]["status"] == "invalid_response" for p in checked))
        self.assertEqual(self.sources["article"], 5)

    def test_current_source_failure_does_not_pay_despite_larger_ai_budget(self):
        active = dict(self.active, article=lambda *args: {"status": "unavailable", "figures": []})
        report = self.run_fixture(active=active)
        self.assertEqual((report["checkedCount"], report["aiAttempts"]), (5, 0))
        self.assertEqual(self.opener.methods, [])

    def test_missing_image_or_input_remains_pending_even_when_ai_succeeds(self):
        source = materials()
        source["article"]["figures"] = []
        source["repository"]["inputs"] = []
        active = dict(self.active, article=lambda *args: deepcopy(source["article"]),
                      repository=lambda *args: deepcopy(source["repository"]))
        report = self.run_fixture(PaperRetryOpener(invalid_before_success=0), active)
        self.assertEqual(report["aiAttempts"], 5)
        self.assertEqual(report["bootstrap"]["completeCount"], 0)
        self.assertTrue(all("input_undocumented" in issue["reasonCodes"]
                            and "selected_figure_missing" in issue["reasonCodes"]
                            for issue in report["bootstrap"]["issues"][:5]))

    def test_daily_discovery_and_maintenance_share_five_source_slots(self):
        self.settings.initialize_existing_first = False
        self.catalog([dict(record(i), figure="Figure 1") for i in range(1, 11)])
        report = self.run_fixture(PaperRetryOpener(invalid_before_success=0), mode="auto")
        self.assertEqual((report["repairCount"], report["newCount"], report["checkedCount"]), (2, 3, 5))
        self.assertEqual(self.sources["article"], 5)
        self.assertEqual(report["discoveryStatus"], "paper_batch_limit_reached")
        self.assertLessEqual(report["aiAttempts"], 15)

    def test_smaller_global_budget_never_overspends_and_retries_stop_at_cap(self):
        self.settings.max_ai_calls = 4
        report = self.run_fixture()
        self.assertEqual(report["aiAttempts"], 4)
        self.assertEqual(report["checkedCount"], 5)
        self.assertTrue(all(count <= 3 for count in self.opener.calls_by_doi.values()))
        self.assertEqual(report["bootstrap"]["completeCount"], 1)

    def test_bootstrap_batch_override_cannot_exceed_shared_five_paper_cap(self):
        self.settings.bootstrap_batch_limit = 20
        report = self.run_fixture(PaperRetryOpener(invalid_before_success=0))
        self.assertEqual(report["checkedCount"], 5)
        self.assertEqual(self.sources["repository"], 5)

    def test_single_paper_daily_batch_alternates_maintenance_and_discovery(self):
        self.settings.initialize_existing_first = False
        self.settings.max_papers_per_run = 1
        first = self.run_fixture(PaperRetryOpener(invalid_before_success=0), mode="auto")
        second = self.run_fixture(PaperRetryOpener(invalid_before_success=0), mode="auto")
        third = self.run_fixture(PaperRetryOpener(invalid_before_success=0), mode="auto")
        self.assertEqual((first["repairCount"], first["newCount"], first["checkedCount"]), (1, 0, 1))
        self.assertEqual((second["repairCount"], second["newCount"], second["checkedCount"]), (0, 1, 1))
        self.assertEqual((third["repairCount"], third["newCount"], third["checkedCount"]), (1, 0, 1))

    def test_two_or_three_paper_daily_cap_still_checks_an_existing_case(self):
        self.settings.initialize_existing_first = False
        for cap in (2, 3):
            with self.subTest(cap=cap):
                self.settings.max_papers_per_run = cap
                self.catalog([dict(record(i), figure="Figure 1") for i in range(1, 11)])
                write_json(self.root / "data" / "ledger.json", {"days": {}, "candidates": {}})
                report = self.run_fixture(PaperRetryOpener(invalid_before_success=0), mode="auto")
                self.assertEqual(report["repairCount"], 1)
                self.assertEqual(report["newCount"], cap - 1)
                self.assertEqual(report["checkedCount"], cap)


if __name__ == "__main__":
    unittest.main()
