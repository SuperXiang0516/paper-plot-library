"""Historical, offline-only scope migration from exact-DOI Europe PMC caches.

This does not fetch articles, execute code, refresh withdrawal checks, or count
an admission. Only a canonical title and bounded classification are changed.
"""
from __future__ import annotations

import base64
import binascii
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from urllib.parse import urlencode

from .pipeline import persistent_catalog, validate_catalog
from .scope import POLICY, POLICIES, assess_scope
from .sources import EPMC, _paper, _published_not_future, _usable, normalize_doi
from .storage import project_lock, read_json, write_json


MAX_FILES = 512
MAX_CACHE_BYTES = 6000000
MAX_TOTAL_BYTES = 32000000


def _strict_json(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = item
        return result
    def constant(_):
        raise ValueError("non-finite JSON number")
    return json.loads(value, object_pairs_hook=pairs, parse_constant=constant)


def _doi_query(query):
    if not isinstance(query, str) or not 1 <= len(query) <= 30000:
        return None
    requested = []
    for part in query.split(" OR "):
        match = re.fullmatch(r'DOI:"(10\.\d{4,9}/[^"\\\s<>]{1,512})"', part)
        doi = match.group(1) if match else ""
        if not doi or normalize_doi(doi) != doi or doi in requested:
            return None
        requested.append(doi)
    return set(requested) if 1 <= len(requested) <= 50 else None


def _cached_metadata(path, *, now):
    """Validate the cache identity against this exact fixed HTTPS core request."""
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_CACHE_BYTES:
        return None
    outer = _strict_json(path.read_bytes())
    if not isinstance(outer, dict) or set(outer) != {"at", "data"}:
        return None
    at = outer["at"]
    if (type(at) not in (int, float) or not math.isfinite(at) or not 0 <= at <= now + 300
            or not isinstance(outer["data"], str) or len(outer["data"]) > 5400000):
        return None
    decoded = base64.b64decode(outer["data"], validate=True)
    if len(decoded) > 4000000:
        return None
    response = _strict_json(decoded)
    if not isinstance(response, dict) or not isinstance(response.get("request"), dict):
        return None
    request = response["request"]
    requested = _doi_query(request.get("queryString"))
    page_size = request.get("pageSize")
    if (not requested or request.get("resultType") != "core"
            or type(page_size) is not int or not 1 <= page_size <= 50
            or request.get("cursorMark", "*") != "*"):
        return None
    params = {"query": request["queryString"], "format": "json", "resultType": "core", "pageSize": page_size}
    url = EPMC + "?" + urlencode(params)
    identity = json.dumps([url, {}, hashlib.sha256(b"").hexdigest()], sort_keys=True)
    expected_name = "http-" + hashlib.sha256(identity.encode()).hexdigest() + ".json"
    if path.name != expected_name:
        return None
    rows = response.get("resultList", {}).get("result") if isinstance(response.get("resultList"), dict) else None
    if (not isinstance(rows, list) or len(rows) > page_size
            or type(response.get("hitCount")) is not int or not 0 <= response["hitCount"] <= 50):
        return None
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get("doi"), str)
                or row["doi"] != normalize_doi(row["doi"]) or row["doi"] not in requested):
            return None
    return rows


def read_offline_scope_metadata(root, *, cache_paths=None, now=None):
    """Read bounded ordinary caches, never construct an HTTP client.

    ``cache_paths`` is a fixture seam, not an arbitrary-path escape: supplied
    paths must still be regular direct children of this root's private cache.
    Cached withdrawal status is historical, not a current safety recheck.
    """
    root = Path(root).resolve()
    cache = root / ".ppl" / "cache"
    report = {"cacheFilesExamined": 0, "cacheFilesAccepted": 0, "cacheLimitReached": False}
    if cache.is_symlink() or cache.parent.is_symlink() or not cache.is_dir():
        return {}, {}, report
    # Normalize parent aliases (for example macOS /var -> /private/var), but do
    # not resolve the leaf: a symlinked cache file must remain rejectable.
    paths = sorted(cache.glob("http-*.json")) if cache_paths is None else sorted(
        Path(p).parent.resolve() / Path(p).name for p in cache_paths)
    metadata, blocked, consumed = {}, {}, 0
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
        raise ValueError("invalid offline cache clock")
    today = datetime.fromtimestamp(now, timezone.utc).date()
    for path in paths:
        if path.parent != cache or not re.fullmatch(r"http-[0-9a-f]{64}\.json", path.name):
            continue
        if report["cacheFilesExamined"] >= MAX_FILES or consumed >= MAX_TOTAL_BYTES:
            report["cacheLimitReached"] = True
            break
        if path.is_symlink() or not path.is_file():
            continue
        size = path.stat().st_size
        report["cacheFilesExamined"] += 1
        if size > MAX_CACHE_BYTES or size > MAX_TOTAL_BYTES - consumed:
            continue
        consumed += size
        try:
            rows = _cached_metadata(path, now=now)
        except (OSError, ValueError, TypeError, KeyError, binascii.Error, UnicodeError):
            continue
        if rows is None:
            continue
        report["cacheFilesAccepted"] += 1
        for row in rows:
            doi = row["doi"]
            if doi in blocked:
                continue
            try:
                if (row.get("source") not in {"MED", "PMC"} or not isinstance(row.get("title"), str)
                        or not 1 <= len(row["title"]) <= 2000
                        or not isinstance(row.get("abstractText", ""), str)
                        or len(row.get("abstractText", "")) > 100000):
                    raise ValueError("invalid formal metadata")
                record = _paper(row)
                record["paper"] = " ".join(record["paper"].split())
                if not _usable(record):
                    blocked[doi] = "nonfinal_or_retracted"
                elif not _published_not_future(record, today):
                    blocked[doi] = "invalid_or_future_date"
                elif not record["paper"]:
                    blocked[doi] = "invalid_metadata"
                else:
                    previous = metadata.get(doi)
                    identity = lambda value: (value["paper"].casefold(), value["abstract"])
                    if previous is not None and identity(previous) != identity(record):
                        blocked[doi] = "conflicting_metadata"
                    else:
                        record["status"] = "matched"
                        metadata.setdefault(doi, record)
            except (ValueError, TypeError, AttributeError):
                blocked[doi] = "invalid_metadata"
            if doi in blocked:
                metadata.pop(doi, None)
    return metadata, blocked, report


def refresh_scope_offline(root, *, cache_paths=None):
    """Atomically migrate only existing titles and scope tags, under one lock."""
    root = Path(root).resolve()
    target = root / "data" / "catalog.json"
    if target.is_symlink() or target.parent.is_symlink():
        raise ValueError("案例库路径不能是符号链接；未进行离线迁移")
    config = read_json(root / "config.json", {})
    policy = config.get("research_scope", POLICY) if isinstance(config, dict) else None
    if policy not in POLICIES or policy == "unrestricted":
        raise ValueError("离线领域复核需要启用明确的研究领域策略；未修改案例库")
    with project_lock(root):
        catalog = validate_catalog(read_json(target))
        original = deepcopy(catalog)
        metadata, blocked, cache_report = read_offline_scope_metadata(root, cache_paths=cache_paths)
        checked, updated, missing, attention = 0, 0, 0, []
        for record in catalog["papers"]:
            doi = normalize_doi(record["doi"])
            if doi in blocked:
                attention.append({"doi": doi, "reason": blocked[doi]})
                continue
            formal = metadata.get(doi)
            if formal is None:
                missing += 1
                continue
            checked += 1
            proof = {"doi": record["doi"], "paper": formal["paper"], "abstract": formal["abstract"]}
            decision = assess_scope(proof, policy, allow_saved=False, source="verified-metadata")
            if record.get("paper") != formal["paper"] or record.get("researchScope") != decision:
                record["paper"] = formal["paper"]
                record["researchScope"] = decision
                updated += 1
        if updated:
            persisted = persistent_catalog(validate_catalog(catalog))
            # Enforce the mutation contract independently of helper internals.
            expected = deepcopy(original)
            for old, new in zip(expected["papers"], catalog["papers"]):
                for field in ("paper", "researchScope"):
                    if field in new:
                        old[field] = deepcopy(new[field])
            if expected != persisted:
                raise ValueError("离线领域迁移超出允许字段；未修改案例库")
            write_json(target, persisted)
        return {"mode": "scope-refresh-offline", "historicalMetadataOnly": True,
                "currentSourceSafetyRechecked": False, "httpRequests": 0, "aiAttempts": 0,
                "authorCodeExecuted": False, **cache_report, "checkedCount": checked,
                "updatedCount": updated, "unchangedCount": checked - updated, "missingCount": missing,
                "reviewCount": len(attention), "review": attention[:100], "saved": bool(updated)}
