"""Build a credential-free static site from the persistent catalogue.

Only referenced, explicitly reusable raster assets are copied. Build output is
not a transport/cache dump, and never contains the fetched author source files.
"""
from __future__ import annotations

import json
import hashlib
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
    "metrics": {"status", "if", "jcr", "cas", "source", "retrieved_at", "metric_year", "query_name", "retained_field_asof", "persistent_cache_hit", "last_attempt_status", "stale"},
    "ai": {"status", "title_zh", "summary_zh", "learning_points", "limitations", "evidence_ids", "reviewRequired", "review_required", "stale_reason", "last_attempt_status", "generated_at"},
    "automation": {"status", "stages", "lastCheckedAt", "errors", "warnings", "attempts"},
}


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
            if not isinstance(key, str) or _PRIVATE_KEY.search(key):
                continue
            if key in _URL_FIELDS or key.endswith("URL"):
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
    return {
        "schemaVersion": 4, "papers": records,
        "inputSpecs": _public({key: value for key, value in (catalog.get("inputSpecs") or {}).items() if key in ids}),
        "dimensions": _public(catalog.get("dimensions", {})),
        "generatedAt": catalog.get("generatedAt", catalog.get("checkedAt", "")),
        "checkedAt": catalog.get("checkedAt", ""),
        "authorCodeExecuted": False, "method": "static-source-review",
        "synthetic": catalog.get("synthetic") is True,
    }


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


def _raster_file(path: Path) -> bool:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 24000000:
        return False
    with path.open("rb") as stream:
        header = stream.read(16)
    return header.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")) or header.startswith(b"RIFF") and header[8:12] == b"WEBP"


def _publish_assets(root: Path, output: Path, catalogue: dict) -> list[str]:
    source_root, target_root = root / "data" / "assets", output / "assets"
    _directory(target_root)
    if source_root.is_symlink():
        raise ValueError("Refusing to publish a symlinked source asset directory.")
    manifest_path = output / "data" / "assets-manifest.json"
    previous = []
    if manifest_path.is_file() and not manifest_path.is_symlink():
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = []
    published = set()
    for paper in catalogue["papers"]:
        for figure in paper.get("article", {}).get("figures", []):
            if not isinstance(figure, dict):
                continue
            asset = _asset(figure.get("asset"))
            if figure.get("reuseStatus") != "allowed" or not asset:
                figure.pop("asset", None)
                continue
            relative = PurePosixPath(asset).relative_to("assets")
            source, destination = source_root / relative, target_root / relative
            try:
                source.resolve().relative_to(source_root.resolve())
            except ValueError:
                figure.pop("asset", None)
                figure["reuseStatus"] = "source_path_rejected"
                continue
            if not _raster_file(source):
                figure.pop("asset", None)
                figure["reuseStatus"] = "asset_unavailable"
                continue
            raw = source.read_bytes()
            expected = figure.get("assetSHA256") if "assetSHA256" in figure else figure.get("sha256")
            if "assetSHA256" in figure or "sha256" in figure:
                if not isinstance(expected, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", expected) or hashlib.sha256(raw).hexdigest() != expected.lower():
                    figure.pop("asset", None)
                    figure["reuseStatus"] = "asset_integrity_failed"
                    continue
            for parent in list(destination.parents):
                if parent == target_root.parent:
                    break
                if parent.is_symlink():
                    raise ValueError("Refusing a symlink in asset output.")
            _directory(destination.parent)
            if destination.is_symlink():
                raise ValueError("Refusing a symlinked output image.")
            # Write exactly the verified bytes, never reopen a source that may
            # have changed between digest verification and copy.
            handle, temporary = tempfile.mkstemp(prefix=".ppl-asset-", dir=destination.parent)
            try:
                with os.fdopen(handle, "wb") as stream:
                    stream.write(raw)
                os.replace(temporary, destination)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            published.add(asset)
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


def build_site(root: Path | str, catalog: dict, reports: list | dict) -> Path:
    """Generate ``site/``. Persistent ``data/`` and the aging site stay untouched."""
    root = Path(root)
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
    report_fields = {"mode", "date", "startedAt", "finishedAt", "generatedAt", "timestamp", "newCount", "repairCount", "status", "note", "errors", "warnings", "records", "results", "processed", "counts", "summary", "demo", "synthetic", "checkedCount", "requests", "aiAttempts", "totalCount", "dailyNewCount", "discoveryStatus", "failures", "stageCounts", "missingKeys"}
    values = reports if isinstance(reports, list) else reports.get("reports", [reports]) if isinstance(reports, dict) else []
    values = sorted([report for report in values if isinstance(report, dict)], key=lambda report: str(report.get("finishedAt") or report.get("startedAt") or report.get("date") or ""), reverse=True)[:90]
    public_reports = [_public({key: value for key, value in report.items() if key in report_fields}) for report in values]
    _atomic_json(output / "data" / "catalog.json", public)
    _atomic_json(output / "data" / "updates.json", public_reports)
    return output
