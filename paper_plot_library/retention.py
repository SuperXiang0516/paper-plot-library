"""Last-good source retention, separate from current-check and safety decisions.

Keeping a source record never grants a publication exception: the ordinary
license, DOI and raster hash gates still apply. No author source is executed.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import re

from .figures import normalize_license, reuse_allowed
from .sources import normalize_doi


_HARD_SOURCE_FAILURES = {"excluded", "retracted", "doi_mismatch", "invalid_doi"}
_KNOWN_LICENSES = {"CC BY", "CC BY 4.0", "CC BY 3.0", "CC BY 2.5", "CC BY 2.0", "CC0",
                   "CC BY NC", "CC BY NC 4.0", "CC BY ND", "CC BY ND 4.0", "CC BY SA",
                   "CC BY SA 4.0", "CC BY NC ND", "CC BY NC ND 4.0", "ALL RIGHTS RESERVED"}


def _known_license(value):
    return value in _KNOWN_LICENSES or bool(re.fullmatch(
        r"CC BY(?: (?:NC|ND|SA)){1,3}(?: [1-4](?:\.\d)?)?", value))


def admission_is_valid(record, now=None):
    """A DOI-bound UTC admission is history, not proof of figure permission."""
    doi, raw = normalize_doi(record.get("doi")), record.get("admittedAt")
    if not doi or doi != record.get("doi") or not isinstance(raw, str):
        return False
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|\+00:00)", raw):
        return False
    try:
        admitted = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        current = datetime.fromisoformat(now.replace("Z", "+00:00")) if now else datetime.now(timezone.utc)
        return datetime(2000, 1, 1, tzinfo=timezone.utc) <= admitted <= current
    except (ValueError, TypeError):
        return False


def protected_library_record(record, root=None, now=None):
    """Published/admitted history is never a disposable failed candidate.

    Admission protects the record only from deletion. It cannot make a bad
    license, corrupt raster, wrong DOI or retracted article publicly visible.
    """
    if admission_is_valid(record, now):
        return True
    if root is None:
        return False
    from .site import has_publishable_body_figure
    return has_publishable_body_figure(root, record)


def article_safety_failure(value, previous=None):
    """Return only positive safety evidence, never infer revocation from 404."""
    status = value.get("status")
    if status in _HARD_SOURCE_FAILURES:
        return status
    if status != "inspected":
        return ""
    license_code = normalize_license(value.get("license"))
    current_figures = [f for f in value.get("figures", []) if isinstance(f, dict)]
    explicitly_open_figure = any(f.get("reuseStatus") == "allowed" and f.get("license")
                                 and reuse_allowed(f["license"]) for f in current_figures)
    if _known_license(license_code) and not reuse_allowed(license_code) and not explicitly_open_figure:
        return "permission_denied"
    old_figures = [f for f in (previous or {}).get("figures", []) if isinstance(f, dict)]
    for figure in current_figures:
        old = next((item for item in old_figures if item.get("id") == figure.get("id")
                    and item.get("label") == figure.get("label")), None)
        if old is None:
            continue
        own_license = normalize_license(figure.get("license"))
        if _known_license(own_license) and not reuse_allowed(own_license):
            return "permission_denied"
        if (figure.get("reuseStatus") == "link_only" and figure.get("sourceURL")
                and _known_license(own_license)):
            return "permission_denied"
        # A new file normally has a new content-addressed path. Conflicting
        # digest declarations for the same cached path need isolation, not a
        # silent fallback to whichever declaration happens to pass.
        if old.get("asset") and old.get("asset") == figure.get("asset"):
            for field in ("sha256", "assetSHA256"):
                if old.get(field) and figure.get(field) and old[field] != figure[field]:
                    return "invalid_response"
    return ""


def retain_source(previous, value, stage, current_usable=None):
    """Retain old successful content while exposing the true attempt status."""
    if not isinstance(previous, dict) or previous.get("status") != "inspected":
        return deepcopy(value)
    status = value.get("status", "unknown")
    has_previous = bool(previous.get("figures")) if stage == "article" else bool(previous.get("scripts"))
    has_current = (any(f.get("asset") and f.get("reuseStatus") == "allowed"
                       for f in value.get("figures", []) if isinstance(f, dict))
                   if stage == "article" else bool(value.get("scripts")))
    if current_usable is not None:
        has_current = current_usable
    if stage == "repository" and any(item.get("fields") for item in previous.get("inputs", [])):
        has_current = has_current and any(item.get("fields") for item in value.get("inputs", []))
    if has_previous and (status != "inspected" or not has_current):
        retained = deepcopy(previous)
        retained.update(retained_from_previous=True, last_attempt_status=status)
        return retained
    return deepcopy(value)


def retain_enrichment(previous, value, stage):
    """A failed refresh must not erase an existing successful explanation/rank."""
    if not isinstance(previous, dict) or not isinstance(value, dict):
        return value
    status = value.get("last_attempt_status") or value.get("status", "unknown")
    good_old = (previous.get("status") in {"generated", "stale"} and bool(previous.get("summary_zh"))
                if stage == "ai" else previous.get("status") == "matched"
                and any(previous.get(key) for key in ("if", "jcr", "cas")))
    good_new = (value.get("status") == "generated" and bool(value.get("summary_zh"))
                if stage == "ai" else value.get("status") == "matched"
                and any(value.get(key) for key in ("if", "jcr", "cas")))
    if good_old and not good_new:
        if status in {"generated", "matched"}:
            status = "invalid_response"
        result = deepcopy(previous)
        result.update(last_attempt_status=status)
        if stage == "ai":
            result.update(status="stale", stale_reason="本轮更新未成功；保留上次有效注释，不能视为本轮核验成功。")
        else:
            result["stale"] = True
        for key in ("error_reason", "http_status"):
            if key in value:
                result[key] = value[key]
            else:
                result.pop(key, None)
        return result
    return value


def sanitized_archive(record):
    """Project a previously stored archive before catalog.json enters Git.

    Do not trust the creation path: imported/old archives may contain nested
    abstract, XML, captions or code excerpts. Each nested shape is allowlisted,
    not recursively copied as arbitrary provider data.
    """
    archive = record.get("retentionArchive")
    doi = normalize_doi(record.get("doi"))
    reasons = {"excluded", "retracted", "doi_mismatch", "invalid_doi", "permission_denied",
               "invalid_response", "scope_excluded"}
    if (not isinstance(archive, dict) or type(archive.get("version")) is not int
            or archive["version"] != 1 or not doi or doi != record.get("doi")
            or archive.get("doi") != doi or archive.get("reason") not in reasons
            or not admission_is_valid({"doi": doi, "admittedAt": archive.get("archivedAt")})):
        return None
    from .site import _asset, _public_url, _safe_status

    field = {"name": str, "kind": str, "line": int}
    io_path = {"path": str, "reader": str, "writer": str, "line": int,
               "status": str, "format": str}
    source_link = {"type": str, "line": int, "url": str}
    script = {"path": str, "url": str, "sha256": str, "language": str,
              "lineScope": str, "inputs": [io_path], "outputs": [io_path],
              "fields": [field], "evidence": [source_link]}
    input_file = {"name": str, "path": str, "reader": str, "format": str, "status": str,
                  "orientation": str, "scale": str, "scriptURL": str, "line": int,
                  "url": str, "headerSHA256": str, "fields": [field],
                  "observedFieldReferences": [field]}
    figure = {"id": str, "label": str, "asset": str, "sha256": str, "assetSHA256": str,
              "sourceURL": str, "articleURL": str, "license": str, "licenseURL": str,
              "reuseStatus": str, "downloadStatus": str, "width": int, "height": int}
    supplement = {"id": str, "label": str, "sourceURL": str, "license": str,
                  "sha256": str, "status": str, "format": str}
    declaration = {"url": str, "scope": str, "status": str, "sectionID": str, "sourceURL": str}
    schemas = {
        "article": {"status": str, "license": str, "licenseURL": str, "version": str,
                    "xmlURL": str, "figures": [figure], "supplements": [supplement],
                    "codeURLs": [str], "codeURLEvidence": [declaration]},
        "repository": {"status": str, "url": str, "commit": str, "license": str,
                       "scripts": [script], "inputs": [input_file]},
        "ai": {"status": str, "title_zh": str, "summary_zh": str, "learning_points": [str],
               "limitations": [str], "evidence_ids": [str], "generated_at": str, "model": str,
               "source_fingerprint": str, "summary_fingerprint": str},
        "metrics": {"status": str, "if": str, "jcr": str, "cas": str, "source": str,
                    "retrieved_at": str, "metric_year": str, "query_name": str},
        "mappingReview": {"approved": bool, "reviewer": str, "reviewedAt": str,
                          "inputSchemaVerified": bool, "commit": str, "scriptSHA256": str,
                          "figureSHA256": str, "inputSHA256": str, "scriptPath": str,
                          "figureId": str, "inputPath": str},
    }

    def project(value, schema, key=""):
        if isinstance(schema, dict):
            if not isinstance(value, dict):
                return None
            output = {}
            for name, child_schema in schema.items():
                if name in value:
                    child = project(value[name], child_schema, name)
                    if child is not None:
                        output[name] = child
            return output
        if isinstance(schema, list):
            if not isinstance(value, list):
                return None
            return [child for item in value[:200] if (child := project(item, schema[0], key)) is not None]
        if schema is int:
            return value if type(value) is int and 0 <= value <= 1000000000 else None
        if schema is bool:
            return value if type(value) is bool else None
        if not isinstance(value, str):
            return None
        if key == "status":
            return _safe_status(value)
        if key == "asset":
            return _asset(value)
        if key.lower().endswith("url") or key == "codeURLs":
            return _public_url(value)
        if key.lower().endswith("sha256") or key.endswith("fingerprint"):
            return value if re.fullmatch(r"[a-fA-F0-9]{64}", value) else None
        if key == "commit":
            return value if re.fullmatch(r"(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})", value) else None
        return value[:3000]

    result = {"version": 1, "doi": doi, "reason": archive["reason"], "archivedAt": archive["archivedAt"]}
    for name, schema in schemas.items():
        if name in archive:
            result[name] = project(archive[name], schema)
    return result


def archive_sources(record, reason, checked_at):
    """One recoverable DOI-bound source snapshot, never a public bypass/cache."""
    doi = normalize_doi(record.get("doi"))
    if not doi or doi != record.get("doi"):
        return
    existing = sanitized_archive(record)
    if existing is not None and (existing.get("article") or existing.get("repository")):
        record["retentionArchive"] = existing
        return  # Do not overwrite the original last-good snapshot with empty quarantine.
    archive = {"version": 1, "doi": doi, "archivedAt": checked_at, "reason": reason}
    for field in ("article", "repository", "ai", "metrics", "mappingReview"):
        if isinstance(record.get(field), dict):
            archive[field] = deepcopy(record[field])
    # catalog.json is committed to GitHub. Keep recoverable links/hashes and
    # structured facts, never cached code excerpts or newly unlicensed captions.
    for script in archive.get("repository", {}).get("scripts", []):
        for evidence in script.get("evidence", []):
            evidence.pop("text", None)
    for evidence in archive.get("article", {}).get("codeURLEvidence", []):
        evidence.pop("text", None)
    for figure in archive.get("article", {}).get("figures", []):
        figure.pop("caption", None)
    record["retentionArchive"] = archive
