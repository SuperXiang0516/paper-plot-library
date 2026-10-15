"""Offline migration fixtures: strict provenance, bounded writes, no transport."""
import base64
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from paper_plot_library.offline_scope import read_offline_scope_metadata, refresh_scope_offline
from paper_plot_library.scope import POLICY, assess_scope
from paper_plot_library.sources import EPMC
from paper_plot_library.storage import project_lock, read_json, write_json


def row(doi="10.1234/tool", **changes):
    return {"doi": doi, "source": "MED", "title": "CellRank for directed single-cell fate mapping.",
            "pubYear": "2025", "firstPublicationDate": "2025-01-02", "id": "123", "pmcid": "PMC123",
            "abstractText": "Here, we present CellRank. CellRank uses computational trajectory inference "
                            "from single-cell RNA sequencing.", **changes}


def make_cache(root, rows, *, requested=None, request_changes=None, at=1, response_changes=None):
    requested = requested or list(dict.fromkeys(item["doi"] for item in rows))
    query = " OR ".join('DOI:"' + doi + '"' for doi in requested)
    request = {"queryString": query, "resultType": "core", "cursorMark": "*", "pageSize": 50,
               **(request_changes or {})}
    response = {"request": request, "hitCount": len(rows), "resultList": {"result": rows}, **(response_changes or {})}
    params = {"query": query, "format": "json", "resultType": request["resultType"], "pageSize": request["pageSize"]}
    identity = json.dumps([EPMC + "?" + urlencode(params), {}, hashlib.sha256(b"").hexdigest()], sort_keys=True)
    path = root / ".ppl" / "cache" / ("http-" + hashlib.sha256(identity.encode()).hexdigest() + ".json")
    write_json(path, {"at": at, "data": base64.b64encode(json.dumps(response).encode()).decode()})
    return path


def make_catalog(root):
    paper = {"id": "preserved-id", "doi": "10.1234/tool", "paper": "Old curated title",
             "name": "Original case", "figure": "Fig. 2", "admittedAt": "2026-01-01T12:00:00Z",
             "article": {"status": "inspected", "figures": [{"label": "Fig. 2", "asset": "assets/x.png", "reuseStatus": "allowed"}]},
             "repository": {"status": "inspected", "commit": "a" * 40, "scripts": [{"path": "plot.py"}]},
             "ai": {"status": "generated", "summary_zh": "已经生成的中文内容"},
             "automation": {"status": "partial", "lastCheckedAt": "2026-01-01T12:00:00Z"}}
    value = {"authorCodeExecuted": False, "papers": [paper], "inputSpecs": {"preserved-id": {"files": []}},
             "dailySession": {"dailyNewCount": 2}, "researchScopePolicy": POLICY}
    write_json(root / "data" / "catalog.json", value)
    write_json(root / "data" / "ledger.json", {"days": {"2026-01-01": {"newDOIs": [paper["doi"]]}}})
    write_json(root / "config.json", {"research_scope": POLICY})
    return value


class OfflineScopeTests(unittest.TestCase):
    def test_historical_cache_restores_tool_with_only_title_and_scope_mutations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = make_catalog(root)
            ledger_before = (root / "data" / "ledger.json").read_bytes()
            cache = make_cache(root, [row()], at=1)  # expired TTL intentionally accepted as historical evidence
            with patch("paper_plot_library.http.HttpClient.get_json", side_effect=AssertionError("no network")), \
                    patch("paper_plot_library.enrichment.summarize", side_effect=AssertionError("no AI")):
                result = refresh_scope_offline(root, cache_paths=[cache])
            actual = read_json(root / "data" / "catalog.json")
            self.assertEqual(result["updatedCount"], 1)
            self.assertTrue(result["historicalMetadataOnly"])
            self.assertFalse(result["currentSourceSafetyRechecked"])
            self.assertEqual((result["httpRequests"], result["aiAttempts"]), (0, 0))
            self.assertEqual(actual["papers"][0]["paper"], row()["title"])
            self.assertEqual(actual["papers"][0]["researchScope"]["tier"], "tools")
            self.assertNotIn("abstract", actual["papers"][0])
            expected = deepcopy(original)
            for key in ("paper", "researchScope"):
                expected["papers"][0][key] = actual["papers"][0][key]
            self.assertEqual(actual, expected)
            self.assertEqual((root / "data" / "ledger.json").read_bytes(), ledger_before)
            before_bytes = (root / "data" / "catalog.json").read_bytes()
            before_time = (root / "data" / "catalog.json").stat().st_mtime_ns
            repeated = refresh_scope_offline(root, cache_paths=[cache])
            self.assertEqual((repeated["updatedCount"], repeated["unchangedCount"]), (0, 1))
            self.assertFalse(repeated["saved"])
            self.assertEqual((root / "data" / "catalog.json").read_bytes(), before_bytes)
            self.assertEqual((root / "data" / "catalog.json").stat().st_mtime_ns, before_time)

    def test_missing_cache_is_noop_and_does_not_invent_verified_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_catalog(root)
            original = (root / "data" / "catalog.json").read_bytes()
            result = refresh_scope_offline(root)
            self.assertEqual((result["checkedCount"], result["missingCount"], result["updatedCount"]), (0, 1, 0))
            self.assertEqual((root / "data" / "catalog.json").read_bytes(), original)

    def test_cache_provenance_must_be_doi_only_core_and_match_hashed_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for request_changes in ({"resultType": "lite"}, {"queryString": "genomics OR all"},
                                    {"cursorMark": "next"}, {"pageSize": 100}):
                path = make_cache(root, [row()], request_changes=request_changes)
                self.assertEqual(read_offline_scope_metadata(root, cache_paths=[path])[0], {})
            path = make_cache(root, [row()])
            renamed = path.with_name("http-" + "f" * 64 + ".json")
            path.rename(renamed)
            self.assertEqual(read_offline_scope_metadata(root, cache_paths=[renamed])[0], {})
            outside = root / "outside.json"
            write_json(outside, {"request": {"queryString": "anything"}})
            self.assertEqual(read_offline_scope_metadata(root, cache_paths=[outside])[0], {})

    def test_unmatched_noncanonical_preprint_retracted_future_and_conflicts_not_promoted(self):
        cases = [
            ([row("10.1234/other")], ["10.1234/tool"], ""),
            ([row("10.1234/TOOL")], ["10.1234/TOOL"], ""),
            ([row(source="PPR")], None, "invalid_metadata"),
            ([row(isRetracted="Y")], None, "nonfinal_or_retracted"),
            ([row(pubTypeList={"pubType": ["Preprint"]})], None, "nonfinal_or_retracted"),
            ([row(firstPublicationDate="2999-01-01", pubYear="2999")], None, "invalid_or_future_date"),
            ([row(), row(title="A conflicting formal title")], None, "conflicting_metadata"),
            ([row(), row(abstractText="Conflicting abstract")], None, "conflicting_metadata"),
        ]
        for rows, requested, reason in cases:
            with self.subTest(reason=reason, rows=rows), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                make_catalog(root)
                original = (root / "data" / "catalog.json").read_bytes()
                path = make_cache(root, rows, requested=requested)
                result = refresh_scope_offline(root, cache_paths=[path])
                self.assertEqual(result["updatedCount"], 0)
                self.assertEqual((root / "data" / "catalog.json").read_bytes(), original)
                if reason:
                    self.assertEqual(result["review"], [{"doi": "10.1234/tool", "reason": reason}])

    def test_identical_duplicate_metadata_is_idempotent_not_a_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = make_cache(root, [row(), row()])
            metadata, blocked, _ = read_offline_scope_metadata(root, cache_paths=[path])
            self.assertEqual(set(metadata), {"10.1234/tool"})
            self.assertEqual(blocked, {})
            self.assertEqual(metadata["10.1234/tool"]["status"], "matched")

    def test_conflicts_and_nonfinal_status_across_cache_files_are_not_last_writer_wins(self):
        for conflicting in (row(title="Different canonical title"), row(isRetracted="Y")):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                # Different valid exact-DOI queries produce two distinct cache identities.
                first = make_cache(root, [row()])
                second = make_cache(root, [conflicting], requested=["10.1234/tool", "10.1234/additional"])
                for paths in ([first, second], [second, first]):
                    metadata, blocked, _ = read_offline_scope_metadata(root, cache_paths=paths)
                    self.assertNotIn("10.1234/tool", metadata)
                    self.assertIn("10.1234/tool", blocked)

    def test_corrupt_base64_json_nan_timestamp_symlinks_and_limits_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = make_cache(root, [row()])
            good = read_json(path)
            for outer in ({**good, "data": "not-base64!"}, {**good, "at": True},
                          {**good, "at": datetime.now(timezone.utc).timestamp() + 10000},
                          {**good, "data": base64.b64encode(b'{"a":1,"a":2}').decode()}):
                write_json(path, outer)
                self.assertEqual(read_offline_scope_metadata(root, cache_paths=[path])[0], {})
            path.write_text('{"at":NaN,"data":"x"}')
            self.assertEqual(read_offline_scope_metadata(root, cache_paths=[path])[0], {})
            path.unlink()
            target = root / "outside-cache.json"
            write_json(target, good)
            path.symlink_to(target)
            self.assertEqual(read_offline_scope_metadata(root, cache_paths=[path])[0], {})
            path.unlink()
            make_cache(root, [row()])
            with patch("paper_plot_library.offline_scope.MAX_FILES", 0):
                metadata, _, report = read_offline_scope_metadata(root)
                self.assertEqual(metadata, {})
                self.assertTrue(report["cacheLimitReached"])

    def test_project_lock_and_invalid_policy_prevent_catalog_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_catalog(root)
            make_cache(root, [row()])
            original = (root / "data" / "catalog.json").read_bytes()
            with project_lock(root), self.assertRaises(RuntimeError):
                refresh_scope_offline(root)
            write_json(root / "config.json", {"research_scope": "unrestricted"})
            with self.assertRaises(ValueError):
                refresh_scope_offline(root)
            self.assertEqual((root / "data" / "catalog.json").read_bytes(), original)

    def test_unrelated_private_fields_are_not_silently_removed_during_scope_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = make_catalog(root)
            catalog["papers"][0]["repository"]["scripts"][0]["evidence"] = [{"text": "private source excerpt"}]
            write_json(root / "data" / "catalog.json", catalog)
            make_cache(root, [row()])
            original = (root / "data" / "catalog.json").read_bytes()
            with self.assertRaisesRegex(ValueError, "允许字段"):
                refresh_scope_offline(root)
            self.assertEqual((root / "data" / "catalog.json").read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
