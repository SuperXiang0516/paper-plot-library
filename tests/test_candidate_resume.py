"""Disposable-runner resume acceptance: synthetic data, no paid/network calls."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper_plot_library.candidate_resume import CandidateResume, validate_checkpoint
from paper_plot_library.config import Settings
from paper_plot_library.pipeline import run_pipeline, validate_ledger
from paper_plot_library.bootstrap import bootstrap_assessment
from paper_plot_library.storage import read_json, write_json
from test_bootstrap import NOW, RASTER
from test_bootstrap_pipeline import materials
from test_pipeline_audit import Client, adapters, record


LATER = "2026-10-12T12:00:00Z"


def source():
    value = materials(complete=True)
    article = value["article"]
    article.update(xmlURL="https://pmc-oa-opendata.s3.amazonaws.com/PMC123.1/article.xml?md5=" + "a" * 32)
    article["figures"][0].update(width=20, height=20, license="CC BY 4.0",
        sourceURL="https://pmc-oa-opendata.s3.amazonaws.com/PMC123.1/figure.png?md5=" + hashlib.md5(RASTER).hexdigest())
    repo = value["repository"]
    repo["inputs"][0].update(path="input.csv", scriptURL=repo["scripts"][0]["url"],
        url=repo["url"] + "/blob/" + repo["commit"] + "/input.csv", line=1)
    return value


class Interrupted(BaseException):
    pass


class CandidateResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ppl-candidate-resume-offline-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        write_json(self.root / "data/catalog.json", {"authorCodeExecuted": False, "papers": [], "inputSpecs": {}, "dimensions": {}})
        self.asset = self.root / "data/assets/fixture.png"
        self.asset.parent.mkdir(parents=True)
        self.asset.write_bytes(RASTER)
        self.settings = Settings(require_complete_new_cases=True, repair_limit=0, citation_enrichment=False,
            llm_model="offline-model", llm_api_key="never-log-or-persist-this-key")
        self.data = source()
        self.metadata = dict(record(), status="matched", abstract="PRIVATE FULL ABSTRACT NEVER PERSIST")
        self.calls = {"lookup": 0, "article": 0, "repository": 0, "summary": 0}

    def active(self, **changes):
        def tracked(name, result):
            def call(*args):
                self.calls[name] += 1
                return deepcopy(result)
            return call
        active = adapters(lookup=tracked("lookup", self.metadata), article=tracked("article", self.data["article"]),
            repository=tracked("repository", self.data["repository"]), metrics=lambda *args: deepcopy(self.data["metrics"]),
            summary=tracked("summary", self.data["ai"]), discover=lambda *args: [record()])
        active.update(changes)
        return active

    def run_batch(self, active, now=NOW):
        with patch("paper_plot_library.pipeline.stamp", return_value=now), patch("paper_plot_library.pipeline.build_site"):
            return run_pipeline(self.root, self.settings, Client(), active)

    def checkpoint(self):
        return read_json(self.root / "data/ledger.json")["candidates"][record()["doi"]]["checkpoint"]

    def interrupted_after_sources(self):
        def stop(*args):
            raise Interrupted()
        with self.assertRaises(Interrupted):
            self.run_batch(self.active(metrics=stop))
        return self.checkpoint()

    def test_successful_stages_saved_before_later_stage_interrupts(self):
        checkpoint = self.interrupted_after_sources()
        self.assertEqual(checkpoint["repository"]["commit"], self.data["repository"]["commit"])
        self.assertEqual(checkpoint["article"]["figures"][0]["asset"], "assets/fixture.png")
        self.assertNotIn("ai", checkpoint)
        self.assertEqual(read_json(self.root / "data/catalog.json")["papers"], [])

    def test_checkpoint_contains_no_private_abstract_code_prompt_response_or_key(self):
        checkpoint = self.interrupted_after_sources()
        encoded = json.dumps(checkpoint)
        for forbidden in (self.metadata["abstract"], "read_csv", self.settings.llm_api_key,
                          '"text"', '"prompt"', '"response"', '"readerOptions"'):
            self.assertNotIn(forbidden, encoded)
        self.assertIn('"age"', encoded)
        self.assertEqual(validate_checkpoint(checkpoint, record()["doi"]), checkpoint)

    def test_next_day_uses_saved_seed_even_when_discovery_returns_nothing(self):
        self.interrupted_after_sources()
        active = self.active(discover=lambda *args: [], checkpoint_repository_current=lambda *args: True)
        result = self.run_batch(active, LATER)
        self.assertEqual(result["newCount"], 1)
        self.assertEqual(self.calls["lookup"], 2)  # Canonical DOI is always checked again.
        self.assertEqual(self.calls["repository"], 1)  # Commit-pinned scripts/header reused.
        catalog = read_json(self.root / "data/catalog.json")
        self.assertEqual(catalog["papers"][0]["admittedAt"], LATER)
        self.assertTrue(catalog["papers"][0]["repository"]["checkpointReused"])
        self.assertNotIn("checkpoint", read_json(self.root / "data/ledger.json")["candidates"][record()["doi"]])

    def test_fresh_commit_failure_does_not_silently_claim_saved_repository_fresh(self):
        self.interrupted_after_sources()
        active = self.active(checkpoint_repository_current=lambda *args: False,
                             repository=lambda *args: {"status": "unavailable", "scripts": [], "inputs": []})
        report = self.run_batch(active, LATER)
        self.assertEqual(report["newCount"], 0)
        self.assertEqual(self.calls["summary"], 0)

    def test_search_outage_does_not_prevent_resuming_known_doi(self):
        self.interrupted_after_sources()
        def unavailable(*args):
            raise RuntimeError("synthetic search outage")
        active = self.active(discover=unavailable, checkpoint_repository_current=lambda *args: True)
        report = self.run_batch(active, LATER)
        self.assertEqual(report["newCount"], 1)
        self.assertTrue(any(item["stage"] == "discovery" for item in report["failures"]))

    def test_metadata_change_discards_saved_stage_binding(self):
        self.interrupted_after_sources()
        self.metadata["abstract"] = "Different current metadata"
        self.run_batch(self.active(checkpoint_repository_current=lambda *args: True), LATER)
        self.assertEqual(self.calls["repository"], 2)

    def test_retraction_cannot_resume_or_pay(self):
        self.interrupted_after_sources()
        report = self.run_batch(self.active(lookup=lambda *args: {"status": "excluded", "isRetracted": True}), LATER)
        self.assertEqual(report["newCount"], 0)
        self.assertEqual(self.calls["summary"], 0)

    def test_corrupt_nested_state_fails_before_any_source_or_paid_call(self):
        checkpoint = self.interrupted_after_sources()
        checkpoint["repository"]["scripts"][0]["text"] = "secret author source"
        ledger = read_json(self.root / "data/ledger.json")
        ledger["candidates"][record()["doi"]]["checkpoint"] = checkpoint
        write_json(self.root / "data/ledger.json", ledger)
        before = dict(self.calls)
        with self.assertRaises(ValueError):
            self.run_batch(self.active(), LATER)
        self.assertEqual(self.calls, before)

    def test_corrupt_hash_path_url_fields_and_quotas_do_not_get_reset(self):
        checkpoint = self.interrupted_after_sources()
        for path, value in ((["article", "figures", 0, "asset"], "assets/../../.env"),
                            (["repository", "inputs", 0, "url"], "https://github.com/fixture/plot?token=secret"),
                            (["repository", "scripts", 0, "sha256"], "bad"),
                            (["version"], True), (["ai"], {"raw_response": "unsafe"})):
            changed = deepcopy(checkpoint)
            cursor = changed
            for key in path[:-1]:
                cursor = cursor[key]
            cursor[path[-1]] = value
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_checkpoint(changed, record()["doi"])
        ledger = read_json(self.root / "data/ledger.json")
        ledger["dailyCollectionSession"] = {"requests": 111, "aiAttempts": 22}
        bounded = validate_ledger(ledger, LATER[:10])
        self.assertEqual(bounded["dailyCollectionSession"], ledger["dailyCollectionSession"])

    def test_catalog_saved_but_ledger_missing_admission_is_not_duplicate(self):
        self.run_batch(self.active())
        catalog = read_json(self.root / "data/catalog.json")
        admitted = catalog["papers"][0]["admittedAt"]
        write_json(self.root / "data/ledger.json", {"days": {}, "candidates": {}})
        report = self.run_batch(self.active())
        self.assertEqual((report["newCount"], report["dailyNewCount"]), (0, 1))
        self.assertEqual(read_json(self.root / "data/catalog.json")["papers"][0]["admittedAt"], admitted)

    def test_same_day_cooldown_still_prevents_repeated_paid_requests(self):
        report = self.run_batch(self.active(summary=lambda *args: {"status": "invalid_response"}))
        self.assertEqual(report["newCount"], 0)
        report = self.run_batch(self.active())
        self.assertEqual((report["newCount"], report["checkedCount"]), (0, 0))
        self.assertEqual(self.calls["summary"], 0)

    def ai_checkpoint(self):
        stored = self.interrupted_after_sources()
        saved = []
        resume = CandidateResume(self.root, record()["doi"], stored, saved.append, self.settings, self.active(), lambda: NOW)
        wrapped = resume.wrapped(lambda *args: deepcopy(self.metadata))
        wrapped["lookup"](Client(), record()["doi"])
        current = deepcopy(self.data)
        current.update(self.metadata)
        evidence = [{"id": "publication", "text": "Synthetic metadata"}]
        wrapped["summary"](Client(), current, evidence, self.settings)
        return saved[-1], current, evidence

    def test_valid_ai_reused_without_second_paid_adapter_call(self):
        saved, current, evidence = self.ai_checkpoint()
        before = self.calls["summary"]
        resume = CandidateResume(self.root, record()["doi"], saved, lambda value: None, self.settings, self.active(), lambda: LATER)
        wrapped = resume.wrapped(lambda *args: deepcopy(self.metadata))
        wrapped["lookup"](Client(), record()["doi"])
        ai = wrapped["summary"](Client(), current, evidence, self.settings)
        self.assertTrue(ai["checkpoint_cache_hit"])
        self.assertEqual(self.calls["summary"], before)

    def test_interruption_after_paid_success_preserves_ai_for_next_runner(self):
        assessments = []
        def assess(*args, **kwargs):
            assessments.append(True)
            if len(assessments) == 2:
                raise Interrupted()
            return bootstrap_assessment(*args, **kwargs)
        with patch("paper_plot_library.pipeline.bootstrap_assessment", assess), self.assertRaises(Interrupted):
            self.run_batch(self.active())
        self.assertIn("ai", self.checkpoint())
        self.assertEqual(self.calls["summary"], 1)
        result = self.run_batch(self.active(discover=lambda *args: [],
            checkpoint_repository_current=lambda *args: True), LATER)
        self.assertEqual((result["newCount"], result["dailyNewCount"], result["aiAttempts"]), (1, 1, 0))
        self.assertEqual(self.calls["summary"], 1)
        saved = read_json(self.root / "data/catalog.json")["papers"][0]
        self.assertTrue(saved["ai"]["checkpoint_cache_hit"])

    def test_missing_real_header_is_retried_not_frozen_as_complete_input(self):
        incomplete = deepcopy(self.data["repository"])
        incomplete["inputs"][0].update(status="header_fetch_failed", fields=[])
        with self.assertRaises(Interrupted):
            self.run_batch(self.active(repository=lambda *args: incomplete,
                metrics=lambda *args: (_ for _ in ()).throw(Interrupted())))
        self.assertNotIn("repository", self.checkpoint())
        result = self.run_batch(self.active(checkpoint_repository_current=lambda *args: True), LATER)
        self.assertEqual(result["newCount"], 1)
        self.assertEqual(self.calls["repository"], 1)

    def test_new_script_input_figure_or_model_invalidates_old_ai(self):
        saved, current, evidence = self.ai_checkpoint()
        for changed in ("commit", "script", "input", "figure", "model"):
            modified = deepcopy(current)
            settings = deepcopy(self.settings)
            if changed == "commit":
                modified["repository"]["commit"] = "d" * 40
            elif changed == "script":
                modified["repository"]["scripts"][0]["sha256"] = "e" * 64
            elif changed == "input":
                modified["repository"]["inputs"][0]["headerSHA256"] = "f" * 64
            elif changed == "figure":
                modified["article"]["figures"][0]["sha256"] = "b" * 64
            else:
                settings.llm_model = "different-model"
            before = self.calls["summary"]
            resume = CandidateResume(self.root, record()["doi"], saved, lambda value: None, settings, self.active(), lambda: LATER)
            wrapped = resume.wrapped(lambda *args: deepcopy(self.metadata))
            wrapped["lookup"](Client(), record()["doi"])
            result = wrapped["summary"](Client(), modified, evidence, settings)
            self.assertNotIn("checkpoint_cache_hit", result)
            self.assertEqual(self.calls["summary"], before + 1)

    def test_default_article_rechecks_source_and_reuses_verified_immutable_image(self):
        saved = self.interrupted_after_sources()
        full = deepcopy(self.data["article"])
        probe = deepcopy(full)
        probe["figures"][0].update(asset="", sha256="", downloadStatus="pending")
        calls = []
        def inspector(client, candidate, *args):
            calls.append(candidate.get("maxFigureDownloads", 2))
            return deepcopy(probe if candidate.get("maxFigureDownloads") == 0 else full)
        with patch("paper_plot_library.candidate_resume.inspect_article", inspector):
            resume = CandidateResume(self.root, record()["doi"], saved, lambda value: None,
                                     self.settings, {}, lambda: LATER)
            wrapped = resume.wrapped(lambda *args: deepcopy(self.metadata))
            wrapped["lookup"](Client(), record()["doi"])
            result = wrapped["article"](Client(), record(), self.asset.parent, True)
        self.assertEqual(calls, [0])
        self.assertTrue(result["figures"][0]["checkpointAssetReused"])

    def test_changed_asset_hash_or_permission_forces_normal_body_scan(self):
        saved = self.interrupted_after_sources()
        for mode in ("damaged", "permission", "changed-url"):
            self.asset.write_bytes(b"damaged" if mode == "damaged" else RASTER)
            full = deepcopy(self.data["article"])
            probe = deepcopy(full)
            probe["figures"][0].update(asset="", sha256="")
            if mode == "permission":
                probe["figures"][0]["reuseStatus"] = "link_only"
            elif mode == "changed-url":
                probe["figures"][0]["sourceURL"] += "changed"
            calls = []
            def inspector(client, candidate, *args):
                calls.append(candidate.get("maxFigureDownloads", 2))
                return deepcopy(probe if candidate.get("maxFigureDownloads") == 0 else full)
            with self.subTest(mode=mode), patch("paper_plot_library.candidate_resume.inspect_article", inspector):
                resume = CandidateResume(self.root, record()["doi"], saved, lambda value: None, self.settings, {}, lambda: LATER)
                wrapped = resume.wrapped(lambda *args: deepcopy(self.metadata))
                wrapped["lookup"](Client(), record()["doi"])
                wrapped["article"](Client(), record(), self.asset.parent, True)
            self.assertEqual(calls, [0, 2])

    def test_production_commit_probe_requires_public_exact_commit_and_license(self):
        saved = self.interrupted_after_sources()
        class ProbeClient:
            github_token = "private-never-persist"
            def __init__(self):
                self.metadata = {"private": False, "visibility": "public", "default_branch": "main", "license": {"spdx_id": "MIT"}}
                self.commit = {"sha": "a" * 40}
                self.calls = []
            def get_json(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return deepcopy(self.commit if "/commits/" in url else self.metadata)
        client = ProbeClient()
        selected = dict(record(), article=deepcopy(self.data["article"]))
        resume = CandidateResume(self.root, record()["doi"], saved, lambda value: None, self.settings, {}, lambda: LATER)
        self.assertTrue(resume._repository_current(client, selected, saved["repository"]))
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(all(kwargs["ttl"] == 0 for url, kwargs in client.calls))
        client.metadata["private"] = True
        self.assertFalse(resume._repository_current(client, selected, saved["repository"]))
        client.metadata["private"] = False
        client.commit["sha"] = "b" * 40
        self.assertFalse(resume._repository_current(client, selected, saved["repository"]))
        client.commit["sha"] = "a" * 40
        client.metadata["license"]["spdx_id"] = "GPL-3.0"
        self.assertFalse(resume._repository_current(client, selected, saved["repository"]))


if __name__ == "__main__":
    unittest.main()
