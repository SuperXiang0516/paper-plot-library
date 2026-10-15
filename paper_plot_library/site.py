"""Build a credential-free static site from the persistent catalogue.

Only referenced, explicitly reusable raster assets are copied. Build output is
not a transport/cache dump, and never contains the fetched author source files.
"""
from __future__ import annotations

import json
import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
from urllib.parse import parse_qsl, urlsplit


_PRIVATE_KEY = re.compile(r"(?:api[_-]?key|secret|token|authorization|password|credential|cookie|raw_response|raw_code|content|payload|prompt|headers|settings)", re.I)
_URL_FIELDS = {"url", "paperURL", "figureURL", "codeURL", "scriptURL", "dataURL", "xmlURL", "sourceURL"}
_PAPER_FIELDS = {
    "id", "name", "paper", "journal", "date", "year", "doi", "pmcid", "paperURL", "figureURL", "figureURLScope",
    "codeURL", "scriptURL", "dataURL", "figure", "lang", "langs", "topic", "method", "description", "required",
    "file", "evidence", "environment", "source", "notes", "codeLicense", "figureLicense", "limitations",
    "dataDescription", "sources", "checkedAt", "collectionGroup", "collectionBatch", "data", "label", "match",
    "graphForms", "formClassificationStatus", "formClassificationEvidence", "suggestedGraphForms", "suggestedAnalysisFunctions", "selectionScore",
    "analysisFunctions", "functionClassificationStatus", "functionClassificationEvidence",
    "dimensionLinkScope", "dimensionLinkEvidence", "selection", "citations", "citationMetrics", "citation_count",
}
_NESTED_FIELDS = {
    "repository": {"status", "url", "commit", "license", "scripts", "inputs", "figureMappings", "limitations", "lastError", "errorStatus", "last_attempt_status", "retained_from_previous"},
    "article": {"status", "license", "licenseURL", "version", "xmlURL", "figures", "supplements", "codeURLs", "codeURLEvidence", "limitations", "last_attempt_status", "retained_from_previous"},
    "metrics": {"status", "if", "jcr", "cas", "source", "retrieved_at", "metric_year", "query_name", "retained_field_asof", "persistent_cache_hit", "last_attempt_status", "stale", "error_reason", "http_status"},
    "ai": {"status", "title_zh", "summary_zh", "learning_points", "limitations", "evidence_ids", "reviewRequired", "review_required", "stale_reason", "last_attempt_status", "generated_at", "error_reason", "http_status", "ai_attempts", "max_ai_attempts", "retry_success_count"},
    "automation": {"status", "stages", "lastCheckedAt", "errors", "warnings", "attempts"},
}
_PUBLIC_STATUSES = {
    "seed", "pending", "ready", "partial", "error", "excluded", "unknown", "ok", "success", "generated",
    "complete", "completed", "matched", "inspected", "stale", "deferred", "deferred_until_selection",
    "disabled", "not_checked", "not_found", "not_applicable", "unavailable", "metadata_unavailable",
    "missing_key", "missing_model", "missing_evidence", "missing_endpoint", "invalid_endpoint", "invalid_response",
    "budget_exhausted", "authentication_error", "payment_required", "permission_denied", "invalid_request",
    "endpoint_not_found", "rate_limited", "service_unavailable", "other_http_error", "network_error",
    "invalid_doi", "doi_mismatch", "retracted", "no_final_version", "no_scripts_found", "partial_failure",
    "daily_quota_reached", "paper_batch_limit_reached", "missing_journal",
    "bootstrap_in_progress", "bootstrap_needs_attention", "bootstrap_complete_next_run",
    "existing_materials_pending",
}
_SAFE_REASONS = {
    "network_budget_exhausted", "collection_budget_exhausted", "ai_budget_exhausted", "session_time_exhausted", "network_error", "error",
    "invalid_endpoint", "invalid_response", "missing_key", "missing_model", "missing_evidence", "missing_endpoint",
    "response_truncated", "response_shape_invalid", "response_finish_reason_invalid", "response_content_invalid",
    "response_content_too_long", "response_markdown_fence", "response_json_malformed", "summary_shape_invalid",
    "summary_fields_invalid", "summary_text_invalid", "summary_chinese_missing", "summary_list_invalid",
    "summary_evidence_invalid", "summary_reproduction_claim", "metrics_response_shape_invalid",
    "metrics_provider_error", "metrics_rank_shape_invalid", "metrics_not_found", "missing_journal",
}
_REPORT_COUNTERS = {"reservedAiRequests", "collectionRequests", "maxRequests", "maxAiCalls", "aiRepairCount",
                    "untouchedRepairCount", "retryRepairCount", "cooldownSkippedCount", "untouchedRemainingCount",
                    "automaticCompleteCount", "incompleteCount", "paperBatchLimit", "maxAiAttemptsPerPaper",
                    "aiCasesAttempted", "aiRetriedCaseCount", "aiRetrySuccessCount", "rejectedCandidateCount", "removedCount"}
_AI_STOP_STATUSES = {"authentication_error", "payment_required", "permission_denied", "invalid_request",
                     "endpoint_not_found", "missing_key", "missing_model", "missing_endpoint", "invalid_endpoint"}
_AI_COUNTERS = {"ai_attempts", "max_ai_attempts", "retry_success_count"}


def _public_session(value: object) -> dict | None:
    """Expose session progress, never private DOI queues or callback payloads."""
    if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1:
        return None
    from .session import SESSION_COUNTERS, SESSION_END_REASONS, SESSION_STATUSES
    value = {**value, "removedCount": value.get("removedCount", 0)}
    if (not isinstance(value.get("status"), str) or value["status"] not in SESSION_STATUSES
            or not isinstance(value.get("id"), str) or not re.fullmatch(r"[a-fA-F0-9]{1,64}", value["id"])
            or not isinstance(value.get("materialsStatus"), str)
            or value["materialsStatus"] not in {"active", "needs_attention", "complete", "complete_with_removals"}
            or type(value.get("localCheckpointSaved")) is not bool):
        return None
    reason = value.get("endReason", "")
    if (not isinstance(reason, str) or reason and reason not in SESSION_END_REASONS
            or value["status"] != "active" and not reason):
        return None
    if any(type(value.get(field)) is not int or not 0 <= value[field] <= 1000000 for field in SESSION_COUNTERS):
        return None
    count_fields = ("targetCount", "plannedCount", "processedCount", "remainingCount", "inFlightCount",
                    "completeCount", "pendingCount", "attentionCount", "removedCount", "checkpointCount")
    if any(value[field] > 10000 for field in count_fields):
        return None
    if (value["completeCount"] + value["pendingCount"] + value["attentionCount"] + value["removedCount"] != value["targetCount"]
            or not value["processedCount"] <= value["plannedCount"] <= value["targetCount"]
            or value["remainingCount"] > value["targetCount"] or value["inFlightCount"] > value["targetCount"]
            or value["materialsStatus"] in {"complete", "complete_with_removals"} and (value["pendingCount"] or value["attentionCount"])
            or value["materialsStatus"] == "complete" and value["removedCount"]
            or value["materialsStatus"] == "complete_with_removals" and not value["removedCount"]):
        return None
    # A resumed session can lower its limits after historical usage. Do not
    # conceal that usage or equate checkpoints with completed batches/pushes.
    result = {"version": 1, "id": value["id"], "status": value["status"], "endReason": reason,
              "materialsStatus": value["materialsStatus"], "localCheckpointSaved": value["localCheckpointSaved"],
              **{field: value[field] for field in sorted(SESSION_COUNTERS)}}
    for field in ("startedAt", "updatedAt"):
        if isinstance(value.get(field), str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", value[field]):
            result[field] = value[field]
    return result


def _public_bootstrap(value: object) -> dict | None:
    """Public cohort progress is a strict summary, never a raw state dump."""
    if (not isinstance(value, dict) or not isinstance(value.get("status"), str)
            or value["status"] not in {"active", "needs_attention", "complete"}):
        return None
    fields = ("totalCount", "completeCount", "pendingCount", "attentionCount")
    if any(type(value.get(field)) is not int or not 0 <= value[field] <= 10000 for field in fields):
        return None
    removed = value.get("removedCount", 0)
    if type(removed) is not int or not 0 <= removed <= 10000:
        return None
    if value["completeCount"] + value["pendingCount"] + value["attentionCount"] + removed != value["totalCount"]:
        return None
    if value["status"] == "complete" and (value["pendingCount"] or value["attentionCount"]):
        return None
    from .bootstrap import BOOTSTRAP_REASON_CODES
    result = {"status": value["status"], **{field: value[field] for field in fields}, "issues": []}
    if "removedCount" in value:
        result["removedCount"] = removed
    if isinstance(value.get("startedAt"), str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", value["startedAt"]):
        result["startedAt"] = value["startedAt"]
    for issue in value.get("issues", [])[:100] if isinstance(value.get("issues"), list) else []:
        if not isinstance(issue, dict) or not isinstance(issue.get("doi"), str) or not re.fullmatch(r"10\.\d{4,9}/[^\s<>]{1,200}", issue["doi"], re.I):
            continue
        reasons = issue.get("reasonCodes", [])
        reasons = list(dict.fromkeys(reason for reason in reasons[:20] if isinstance(reason, str) and reason in BOOTSTRAP_REASON_CODES)) if isinstance(reasons, list) else []
        if reasons:
            result["issues"].append({"doi": issue["doi"], "reasonCodes": reasons})
    if result["status"] == "complete" and result["issues"]:
        return None
    return result


def _safe_http_status(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 400 <= value <= 599


def _safe_reason(value: object) -> bool:
    return isinstance(value, str) and (value in _SAFE_REASONS or re.fullmatch(r"http_[45]\d{2}", value) is not None)


def _safe_status(value: object) -> str:
    return value if isinstance(value, str) and value in _PUBLIC_STATUSES else "unknown"


def _public_url(value: object) -> str:
    if not isinstance(value, str):
        return ""
    try:
        url = urlsplit(value)
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.port:
            return ""
        if any(_PRIVATE_KEY.search(key) or key.lower() == "key" for key, _ in parse_qsl(url.query)):
            return ""
    except ValueError:
        return ""
    return value


def _asset(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"assets/[A-Za-z0-9][A-Za-z0-9._/-]*", value):
        return ""
    if "//" in value or any(part in {".", ".."} for part in value.split("/")):
        return ""
    if PurePosixPath(value).suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        return ""
    return value


def _public(value: object, depth: int = 0) -> object:
    """Bounded JSON projection; arbitrary private transport fields are dropped."""
    if depth > 15:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _public_url(value) if value.startswith(("https://", "http://")) else value[:30000]
    if isinstance(value, list):
        return [_public(item, depth + 1) for item in value[:2000]]
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:2000]:
            if key == "headerSHA256":
                # A content digest is provenance, not private HTTP headers.
                if isinstance(item, str) and re.fullmatch(r"[a-fA-F0-9]{64}", item):
                    result[key] = item
                continue
            if not isinstance(key, str) or _PRIVATE_KEY.search(key):
                continue
            if key == "error_reason":
                if _safe_reason(item):
                    result[key] = item
            elif key in {"http_status", "errorStatus"}:
                if _safe_http_status(item):
                    result[key] = item
            elif key == "last_attempt_status":
                result[key] = _safe_status(item)
            elif key in _URL_FIELDS or key.endswith("URL"):
                result[key] = _public_url(item)
            elif key == "asset":
                result[key] = _asset(item)
            else:
                result[key] = _public(item, depth + 1)
        return result
    return None


def public_catalog(catalog: dict) -> dict:
    if not isinstance(catalog, dict) or not isinstance(catalog.get("papers"), list):
        raise ValueError("Catalogue must contain a papers array.")
    if catalog.get("authorCodeExecuted") is not False:
        raise ValueError("This catalogue must explicitly declare authorCodeExecuted=false.")
    if len(catalog["papers"]) > 10000:
        raise ValueError("Catalogue exceeds this version's safety limit.")
    records, ids = [], set()
    for paper in catalog["papers"]:
        if not isinstance(paper, dict) or not isinstance(paper.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}", paper["id"]):
            raise ValueError("Each paper requires a safe stable identifier.")
        if paper["id"] in ids:
            raise ValueError("Duplicate paper identifier.")
        if paper.get("runPerformed") is True:
            raise ValueError("A static-inspection catalogue cannot claim author execution.")
        ids.add(paper["id"])
        record = _public({key: value for key, value in paper.items() if key in _PAPER_FIELDS})
        for key, fields in _NESTED_FIELDS.items():
            if isinstance(paper.get(key), dict):
                record[key] = _public({name: value for name, value in paper[key].items() if name in fields})
                if key in {"ai", "metrics"} and "status" in record[key]:
                    record[key]["status"] = _safe_status(record[key]["status"])
                if key == "ai":
                    for name in _AI_COUNTERS:
                        if name in record[key] and (type(record[key][name]) is not int or not 0 <= record[key][name] <= 5):
                            record[key].pop(name)
        for script in record.get("repository", {}).get("scripts", []):
            if isinstance(script, dict):
                for evidence in script.get("evidence", []):
                    if isinstance(evidence, dict):
                        evidence.pop("text", None)  # Public provenance links, not cached author code excerpts.
        for evidence in record.get("article", {}).get("codeURLEvidence", []):
            if isinstance(evidence, dict):
                evidence.pop("text", None)
        record["runPerformed"] = False
        records.append(record)
    result = {
        "schemaVersion": 4, "papers": records,
        "inputSpecs": _public({key: value for key, value in (catalog.get("inputSpecs") or {}).items() if key in ids}),
        "dimensions": _public(catalog.get("dimensions", {})),
        "generatedAt": catalog.get("generatedAt", catalog.get("checkedAt", "")),
        "checkedAt": catalog.get("checkedAt", ""),
        "authorCodeExecuted": False, "method": "static-source-review",
        "synthetic": catalog.get("synthetic") is True,
    }
    bootstrap = _public_bootstrap(catalog.get("bootstrap"))
    if bootstrap is not None:
        result["bootstrap"] = bootstrap
    session = _public_session(catalog.get("session"))
    if session is not None:
        result["session"] = session
    from .cloud_update import public_daily_summary
    daily = public_daily_summary(catalog.get("dailySession"))
    if daily is not None:
        result["dailySession"] = daily
    return result


def _atomic_json(path: Path, value: object) -> None:
    if path.is_symlink():
        raise ValueError("Refusing to replace a symlink in build output.")
    handle, temporary = tempfile.mkstemp(prefix=".ppl-build-", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _directory(path: Path) -> None:
    if path.is_symlink():
        raise ValueError("Refusing to write through a symlink in build output.")
    path.mkdir(parents=True, exist_ok=True)


def _verified_raster(root: Path, figure: dict, article_license: str) -> bytes | None:
    """Return only the exact, licensed, digest-checked, decodable image bytes."""
    from .figures import reuse_allowed
    asset = _asset(figure.get("asset"))
    digest = figure.get("assetSHA256") if "assetSHA256" in figure else figure.get("sha256")
    if (figure.get("reuseStatus") != "allowed" or not asset
            or not _public_url(figure.get("sourceURL"))
            or not reuse_allowed(article_license, str(figure.get("caption") or "") + " " + str(figure.get("credit") or ""),
                                 str(figure.get("license") or ""))
            or not isinstance(digest, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", digest)):
        figure.pop("asset", None)
        return None
    source_root = root / "data" / "assets"
    source = root / "data" / asset
    try:
        # Check the whole project-local parent chain, including data/. A
        # resolved-within-assets check alone accepts aliases inside that tree.
        for path in (source, *source.parents):
            if path.is_symlink():
                raise ValueError("Symlinked source image path")
            if path == root:
                break
        source.resolve().relative_to(source_root.resolve())
        if not source.is_file() or not 0 < source.stat().st_size <= 24000000:
            raise ValueError("Missing or oversized image")
        with source.open("rb") as stream:
            raw = stream.read(24000001)
        if not 0 < len(raw) <= 24000000 or hashlib.sha256(raw).hexdigest() != digest.lower():
            raise ValueError("Image digest mismatch")
        raster = (raw.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a"))
                  or raw.startswith(b"RIFF") and raw[8:12] == b"WEBP")
        if not raster:
            raise ValueError("Not a displayable raster")
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as image:
            if image.format not in {"PNG", "JPEG", "GIF", "WEBP"} or not 0 < image.width * image.height <= 64000000:
                raise ValueError("Unsupported or oversized raster")
            image.verify()
        # verify() checks structure; load() also catches truncated pixels that
        # a browser could not display. Neither executes author code.
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
        return raw
    except Exception:
        figure.pop("asset", None)
        figure["reuseStatus"] = "asset_unavailable"
        return None


def _body_reference(figure: dict) -> dict | None:
    """A supplemental/extended namespace must never be promoted to main."""
    from .figures import figure_reference
    if re.search(r"\b(?:supplement(?:ary|al|s)?|supporting|extended)\b|补充|附图|扩展", str(figure.get("label") or ""), re.I):
        return None
    label_reference = figure_reference(figure.get("label"))
    declared = figure.get("reference")
    if declared is not None:
        if (not isinstance(declared, dict) or declared.get("scope") != "main"
                or not re.fullmatch(r"[0-9]{1,6}", str(declared.get("number", "")))):
            return None
        reference = declared
        if label_reference and (label_reference["scope"] != "main"
                                or int(label_reference["number"]) != int(reference["number"])):
            return None
    else:
        reference = label_reference
    if not reference or reference.get("scope") != "main":
        return None
    if not isinstance(figure.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}", figure["id"]):
        return None
    return {"scope": "main", "number": str(int(reference["number"]))}


def _select_body_figure(root: Path, paper: dict) -> tuple | None:
    """Use one gate for both successful admissions and the public gallery."""
    from .figures import figure_reference
    if paper.get("automation", {}).get("status") == "excluded":
        return None
    figures = paper.get("article", {}).get("figures", [])
    figures = [figure for figure in figures if isinstance(figure, dict)] if isinstance(figures, list) else []
    article_license = str(paper.get("article", {}).get("license") or "")
    selected = figure_reference(paper.get("figure"))

    def preferred(figure):
        reference = _body_reference(figure)
        return bool(selected and reference and selected["scope"] == "main"
                    and int(selected["number"]) == int(reference["number"]))

    for figure in sorted(figures, key=preferred, reverse=True):
        reference = _body_reference(figure)
        if reference is not None:
            raw = _verified_raster(root, figure, article_license)
            if raw is not None:
                return figure, raw, reference
    return None


def has_publishable_body_figure(root: Path, paper: dict) -> bool:
    """Read-only check: a counted new paper must really be displayable.

    Sanitize just as publication does, and verify on a copy because invalid
    rasters are stripped from the public projection, never the source record.
    """
    try:
        public = public_catalog({"authorCodeExecuted": False, "papers": [paper], "inputSpecs": {}})
        return _select_body_figure(Path(root), public["papers"][0]) is not None
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def _publish_assets(root: Path, output: Path, catalogue: dict) -> list[str]:
    target_root = output / "assets"
    _directory(target_root)
    manifest_path = output / "data" / "assets-manifest.json"
    previous = []
    if manifest_path.is_symlink():
        raise ValueError("Refusing a symlinked asset manifest.")
    if manifest_path.exists():
        try:
            if not manifest_path.is_file() or manifest_path.stat().st_size > 16000000:
                raise ValueError("Invalid asset manifest")
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(previous, list) or any(not _asset(asset) for asset in previous):
                raise ValueError("Invalid asset manifest")
        except (OSError, ValueError) as exc:
            # Silently forgetting ownership would leave previously published
            # images accessible after their article was removed.
            raise ValueError("Cannot safely replace a malformed asset manifest.") from exc
    published, retained = set(), []
    total = len(catalogue["papers"])

    def copy_verified(figure, raw):
        asset = _asset(figure.get("asset"))
        destination = target_root / PurePosixPath(asset).relative_to("assets")
        for parent in destination.parents:
            if parent == target_root.parent:
                break
            if parent.is_symlink():
                raise ValueError("Refusing a symlink in asset output.")
        _directory(destination.parent)
        if destination.is_symlink():
            raise ValueError("Refusing a symlinked output image.")
        handle, temporary = tempfile.mkstemp(prefix=".ppl-asset-", dir=destination.parent)
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(raw)
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        published.add(asset)

    for paper in catalogue["papers"]:
        selected = _select_body_figure(root, paper)
        if selected is None:
            continue  # Internal queue entries never become public placeholder cards.
        body, body_bytes, reference = selected
        figures = paper.get("article", {}).get("figures", [])
        figures = [figure for figure in figures if isinstance(figure, dict)] if isinstance(figures, list) else []
        article_license = str(paper.get("article", {}).get("license") or "")
        copy_verified(body, body_bytes)
        paper["displayFigure"] = {"id": body["id"], "label": str(body.get("label") or "")[:600],
                                  "reference": reference}
        # This preview is independent of the originally selected figure and
        # its code/input mapping; publishing it does not verify that mapping.
        for figure in figures:
            if figure is body:
                continue
            raw = _verified_raster(root, figure, article_license)
            if raw is not None:
                copy_verified(figure, raw)
        retained.append(paper)
    catalogue["papers"] = retained
    ids = {paper["id"] for paper in retained}
    catalogue["inputSpecs"] = {key: value for key, value in catalogue["inputSpecs"].items() if key in ids}
    catalogue["publicationPolicy"] = "body-images-only"
    catalogue["publicationSummary"] = {"publishedCount": len(retained), "hiddenCount": total - len(retained), "totalCount": total}
    _filter_public_issues(catalogue.get("bootstrap"), retained)
    # Remove only files this builder previously published, never arbitrary user files.
    for obsolete in previous if isinstance(previous, list) else []:
        asset = _asset(obsolete)
        if asset and asset not in published:
            path = output / asset
            try:
                path.resolve().relative_to(target_root.resolve())
            except ValueError:
                continue
            parents_safe = True
            for parent in path.parents:
                if parent == target_root.parent:
                    break
                if parent.is_symlink():
                    parents_safe = False
                    break
            if path.is_file() and not path.is_symlink() and parents_safe:
                path.unlink()
    _atomic_json(manifest_path, sorted(published))
    return sorted(published)


def _filter_public_issues(summary: object, papers: list[dict]) -> None:
    """Keep aggregate queue counts, but no hidden candidate identifiers."""
    if isinstance(summary, dict) and isinstance(summary.get("issues"), list):
        dois = {str(paper.get("doi", "")).strip().lower() for paper in papers}
        summary["issues"] = [issue for issue in summary["issues"] if isinstance(issue, dict)
                             and str(issue.get("doi", "")).strip().lower() in dois]


def _public_report(value: dict, papers: list[dict]) -> dict:
    """Typed aggregate logs, never a second route to publish the private queue."""
    result = {}
    counters = _REPORT_COUNTERS | {"newCount", "repairCount", "checkedCount", "requests", "aiAttempts", "totalCount", "dailyNewCount"}
    for field in counters:
        count = value.get(field)
        if type(count) is int and 0 <= count <= 1000000:
            result[field] = count
    for field in ("date", "startedAt", "finishedAt", "generatedAt", "timestamp"):
        timestamp = value.get(field)
        if isinstance(timestamp, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z)?", timestamp):
            result[field] = timestamp
    for field in ("status", "discoveryStatus"):
        status = value.get(field)
        if isinstance(status, str) and status in _PUBLIC_STATUSES:
            result[field] = status
    if isinstance(value.get("mode"), str) and value["mode"] in {"auto", "backfill", "daily", "demo", "offline"}:
        result["mode"] = value["mode"]
    for field in ("demo", "synthetic", "collectionBudgetExhausted"):
        if type(value.get(field)) is bool:
            result[field] = value[field]
    if isinstance(value.get("missingKeys"), list):
        names = {"LLM_API_KEY", "LLM_MODEL", "LLM_API_URL", "EASYSCHOLAR_SECRET_KEY", "NCBI_API_KEY", "NCBI_EMAIL", "OPENALEX_API_KEY", "GH_TOKEN"}
        result["missingKeys"] = list(dict.fromkeys(name for name in value["missingKeys"][:100] if isinstance(name, str) and name in names))
    if isinstance(value.get("stageCounts"), dict):
        result["stageCounts"] = {
            stage: {status: count for status, count in counts.items()
                    if isinstance(status, str) and status in _PUBLIC_STATUSES
                    and type(count) is int and 0 <= count <= 10000}
            for stage, counts in value["stageCounts"].items()
            if stage in {"metadata", "article", "repository", "metrics", "ai"} and isinstance(counts, dict)
        }
    if isinstance(value.get("failures"), list):
        dois = {str(paper.get("doi", "")).strip().lower() for paper in papers}
        failures = []
        for failure in value["failures"][:2000]:
            if not isinstance(failure, dict):
                continue
            doi, stage = failure.get("doi"), failure.get("stage")
            if doi is not None and (not isinstance(doi, str) or doi.strip().lower() not in dois):
                continue
            if not isinstance(stage, str) or stage not in {"metadata", "article", "repository", "metrics", "ai", "summary", "repair", "candidate", "discovery"}:
                continue
            item = {"stage": stage}
            if doi is not None:
                item["doi"] = doi
            status = failure.get("status")
            if not isinstance(status, str) or status not in _PUBLIC_STATUSES:
                raw_error = failure.get("error")
                status = raw_error.rsplit(":", 1)[-1].strip() if isinstance(raw_error, str) else "error"
                status = status if status in _PUBLIC_STATUSES else "error"
            item["status"] = status
            if _safe_reason(failure.get("error_reason")):
                item["error_reason"] = failure["error_reason"]
            if _safe_http_status(failure.get("http_status")):
                item["http_status"] = failure["http_status"]
            failures.append(item)
        result["failures"] = failures
    return result


def _check_public_readiness(public: dict, source: dict) -> None:
    """Asset projection may invalidate a ready source record; do not overclaim."""
    from .pipeline import mapping_is_current  # Runtime import avoids a module initialization cycle.
    originals = {paper["id"]: paper for paper in source["papers"]}
    for paper in public["papers"]:
        automation = paper.get("automation", {})
        if automation.get("status") != "ready":
            continue
        stages = automation.get("stages", {})
        images = [figure for figure in paper.get("article", {}).get("figures", [])
                  if isinstance(figure, dict) and figure.get("reuseStatus") == "allowed" and _asset(figure.get("asset"))]
        review = originals[paper["id"]].get("mappingReview") or {}
        if isinstance(review, dict) and review.get("approved") is True:
            images = [figure for figure in images if figure.get("id") == review.get("figureId")
                      and figure.get("sha256") == review.get("figureSHA256")]
        facts_current = (mapping_is_current(originals[paper["id"]])
                         and stages.get("metadata") == "matched" and stages.get("article") == "inspected"
                         and stages.get("repository") == "inspected" and stages.get("ai") == "generated"
                         and paper.get("repository", {}).get("status") == "inspected"
                         and paper.get("repository", {}).get("scripts")
                         and paper.get("ai", {}).get("status") == "generated")
        if not images or not facts_current:
            automation["status"] = "partial"
            warnings = automation.setdefault("warnings", [])
            warnings.append("公开构建时原图资产缺失或当前阶段依据不足；已从静态核查就绪降为部分完成。")


def _public_availability(public: dict) -> None:
    """Describe published evidence, not execution or precise figure/code binding.

    This must run after the asset checks: an allowed filename in persistent data
    alone does not prove the image made it into the public release.
    """
    from .figures import figure_reference
    failures = {"error", "unavailable", "metadata_unavailable", "partial_failure", "authentication_error",
                "payment_required", "permission_denied", "invalid_request", "endpoint_not_found", "rate_limited",
                "service_unavailable", "other_http_error", "network_error", "invalid_endpoint", "invalid_response"}
    deferred = {"budget_exhausted", "missing_key", "missing_model", "missing_endpoint", "deferred", "deferred_until_selection"}
    def values(value):
        return value if isinstance(value, list) else []
    def object_value(value):
        return value if isinstance(value, dict) else {}
    counts = {"total": 0, "browsable": 0, "candidates": 0, "excluded": 0, "verified": 0, "errors": 0}
    for paper in public["papers"]:
        images = [figure for figure in values(object_value(paper.get("article")).get("figures")) if isinstance(figure, dict)]
        selected = figure_reference(paper.get("figure"))
        if selected:
            matching = []
            for figure in images:
                reference = figure.get("reference")
                if not (isinstance(reference, dict) and reference.get("scope") in {"main", "extended", "supplementary"}
                        and re.fullmatch(r"\d+", str(reference.get("number", "")))):
                    reference = figure_reference(figure.get("label"))
                if (reference and reference["scope"] == selected["scope"]
                        and int(reference["number"]) == int(selected["number"])):
                    matching.append(figure)
            images = matching
        elif str(paper.get("figure") or "").strip():
            images = []  # Do not substitute an unrelated image for an unparsed selected figure.
        has_image = any(figure.get("reuseStatus") == "allowed" and _asset(figure.get("asset")) for figure in images)
        repository = object_value(paper.get("repository"))
        commit = repository.get("commit", "")
        def fixed_script(script):
            if not isinstance(script, dict) or not re.fullmatch(r"[a-fA-F0-9]{64}", str(script.get("sha256", ""))):
                return False
            url = urlsplit(_public_url(script.get("url")))
            return url.hostname == "github.com" and f"/blob/{commit}/" in url.path
        has_code = (repository.get("status") == "inspected" and isinstance(commit, str)
                    and bool(re.fullmatch(r"[a-fA-F0-9]{40}", commit))
                    and any(fixed_script(script) for script in values(repository.get("scripts"))))
        spec = object_value(public.get("inputSpecs", {}).get(paper["id"]))
        selected_inputs = values(spec.get("files"))
        automatic = values(object_value(spec.get("automaticInputs")).get("files"))
        repository_inputs = values(repository.get("inputs"))
        inputs = selected_inputs or repository_inputs
        has_input_schema = any(isinstance(file, dict) and (values(file.get("fields")) or values(file.get("columns"))) for file in inputs + automatic)
        has_observed_header = any(file.get("status") == "header_observed"
                                  and re.fullmatch(r"[a-fA-F0-9]{64}", str(file.get("headerSHA256", "")))
                                  and any(isinstance(field, dict) and field.get("kind") == "actual_file_header"
                                          for field in values(file.get("fields")))
                                  for file in repository_inputs if isinstance(file, dict))
        input_state = "observed_header" if has_observed_header else "documented" if has_input_schema else "code_candidates" if inputs or automatic else "missing"
        ai = object_value(paper.get("ai"))
        has_ai = ai.get("status") == "generated" and isinstance(ai.get("summary_zh"), str) and bool(ai["summary_zh"].strip())
        has_old_ai = ai.get("status") == "stale" and isinstance(ai.get("summary_zh"), str) and bool(ai["summary_zh"].strip())
        metrics = object_value(paper.get("metrics"))
        has_metrics = any(metrics.get(field) not in (None, "") for field in ("if", "jcr") if field in metrics)
        automation = object_value(paper.get("automation"))
        stages = automation.get("stages")
        if isinstance(stages, dict):
            statuses = [value.get("last_attempt_status") or value.get("status") if isinstance(value, dict) else value for value in stages.values()]
        else:
            statuses = [object_value(paper.get(stage)).get("last_attempt_status") or object_value(paper.get(stage)).get("status")
                        for stage in ("article", "repository", "ai", "metrics")]
        has_errors = any(isinstance(status, str) and status in failures for status in statuses)
        health = "error" if has_errors else "deferred" if any(isinstance(status, str) and status in deferred for status in statuses) else "ok" if isinstance(stages, dict) and stages else "uninspected"
        excluded = automation.get("status") == "excluded"
        available = {"hasImage": bool(has_image), "hasCode": bool(has_code), "hasInputSchema": bool(has_input_schema),
                     "hasInputNotes": bool(inputs or automatic), "hasObservedHeaders": has_observed_header,
                     "inputState": input_state, "hasAi": bool(has_ai), "hasOldAi": bool(has_old_ai),
                     "hasMetrics": has_metrics, "browsable": bool(not excluded and has_image and has_code),
                     "verification": "verified" if not excluded and automation.get("status") == "ready" else "unverified",
                     "health": health}
        paper["availability"] = available
        counts["total"] += 1
        if excluded:
            counts["excluded"] += 1
            continue
        counts["browsable" if available["browsable"] else "candidates"] += 1
        counts["verified"] += available["verification"] == "verified"
        counts["errors"] += health == "error"
    public["availabilityCounts"] = counts


def build_site(root: Path | str, catalog: dict, reports: list | dict) -> Path:
    """Generate ``site/``. Persistent ``data/`` and the aging site stay untouched."""
    root = Path(root).absolute()
    output = root / "site"
    public = public_catalog(catalog)
    json.dumps(public, allow_nan=False)  # Fail before changing output if values are not valid JSON.
    _directory(output)
    _directory(output / "data")
    for name in ("index.html", "app.js", "styles.css"):
        source, destination = root / "web" / name, output / name
        if destination.is_symlink() or source.is_symlink():
            raise ValueError("Refusing a symlinked frontend file.")
        shutil.copyfile(source, destination)
    _publish_assets(root, output, public)
    _check_public_readiness(public, catalog)
    _public_availability(public)
    values = reports if isinstance(reports, list) else reports.get("reports", [reports]) if isinstance(reports, dict) else []
    values = sorted([report for report in values if isinstance(report, dict)], key=lambda report: str(report.get("finishedAt") or report.get("startedAt") or report.get("date") or ""), reverse=True)[:90]
    public_reports = [_public_report(report, public["papers"]) for report in values]
    for report, original in zip(public_reports, values):
        bootstrap = _public_bootstrap(original.get("bootstrap"))
        if bootstrap is not None:
            _filter_public_issues(bootstrap, public["papers"])
            report["bootstrap"] = bootstrap
        session = _public_session(original.get("session"))
        if session is not None:
            report["session"] = session
        from .cloud_update import public_daily_summary
        daily = public_daily_summary(original.get("dailySession"))
        if daily is not None:
            report["dailySession"] = daily
        if isinstance(original.get("aiStoppedStatus"), str) and original["aiStoppedStatus"] in _AI_STOP_STATUSES:
            report["aiStoppedStatus"] = original["aiStoppedStatus"]
    _atomic_json(output / "data" / "catalog.json", public)
    _atomic_json(output / "data" / "updates.json", public_reports)
    return output
