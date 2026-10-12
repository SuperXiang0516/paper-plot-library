"""Fixed-cohort, evidence-gated initialization before daily discovery.

No author code is executed. Complete means the required static materials exist,
not that a person approved exact figure/script/input binding or reproduced a plot.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import io
from pathlib import Path, PurePosixPath
import re
from urllib.parse import unquote, urlsplit

from .figures import figure_reference
from .sources import normalize_doi


BOOTSTRAP_REASON_CODES = frozenset({
    "metadata_unchecked", "source_refresh_failed", "article_unchecked", "no_final_version",
    "selected_figure_missing", "figure_reuse_blocked", "figure_asset_unverified", "code_unchecked",
    "code_not_pinned", "script_integrity_missing", "author_code_link_unverified", "input_undocumented",
    "ai_missing", "ai_stale", "ai_configuration_missing", "ai_authentication_failed", "ai_payment_required",
    "ai_transient_failure", "ai_response_invalid", "metrics_unchecked", "metrics_configuration_missing",
    "metrics_query_failed", "source_transient_failure", "case_excluded", "cohort_record_missing",
    "request_budget_deferred",
})
REMOVAL_REASON_CODES = frozenset({
    "formal_version_unavailable", "selected_figure_unavailable", "figure_reuse_unavailable",
    "author_scripts_unavailable", "input_basis_unavailable",
})
_HUMAN_ACTION = {"ai_configuration_missing", "ai_authentication_failed", "ai_payment_required",
                 "metrics_configuration_missing", "case_excluded"}
_TRANSIENT = {"error", "unavailable", "metadata_unavailable", "network_error", "rate_limited",
              "service_unavailable", "other_http_error", "partial_failure"}
_DEFERRED = {"budget_exhausted", "deferred", "deferred_until_selection", "not_checked"}
_COUNT_LIMIT = 10000


def _object(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def _timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|\+00:00)", value):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _now(value=None):
    result = _timestamp(value) if value is not None else datetime.now(timezone.utc).replace(microsecond=0)
    if result is None:
        raise ValueError("初始化时间必须是有效 UTC 时间")
    return result.isoformat().replace("+00:00", "Z")


def _hash(value, lengths=(64,)):
    return isinstance(value, str) and len(value) in lengths and re.fullmatch(r"[a-fA-F0-9]+", value) is not None


def _url(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port or parsed.query or parsed.fragment:
            return None
        return parsed
    except ValueError:
        return None


def _safe_record_state(record):
    raw = _object(_object(record.get("automation")).get("bootstrap"))
    result = {}
    for field in ("attempts", "unsuccessfulScans"):
        value = raw.get(field)
        result[field] = value if type(value) is int and 0 <= value <= _COUNT_LIMIT else 0
    status = raw.get("status")
    if isinstance(status, str) and status in {"pending", "complete", "needs_attention"}:
        result["status"] = status
    last = raw.get("lastAttemptAt")
    if _timestamp(last):
        result["lastAttemptAt"] = last
    upcoming = raw.get("nextAttemptAt")
    if _timestamp(last) and _timestamp(upcoming) and timedelta(0) <= _timestamp(upcoming) - _timestamp(last) <= timedelta(hours=24):
        result["nextAttemptAt"] = upcoming
    result["reasonCodes"] = [code for code in _list(raw.get("reasonCodes"))[:30]
                             if isinstance(code, str) and code in BOOTSTRAP_REASON_CODES][:20]
    source_scans = _object(raw.get("sourceGapScans"))
    result["sourceGapScans"] = {code: value for code, value in source_scans.items()
                                if isinstance(code, str) and code in REMOVAL_REASON_CODES
                                and type(value) is int and 0 <= value <= _COUNT_LIMIT}
    return result


def safe_removed_dois(ledger):
    """Validate minimal public tombstones; never infer removal from a missing record."""
    if not isinstance(ledger, dict):
        raise ValueError("自动移除记录格式无效；不能忽略或重建移除记录")
    removed = ledger.get("removedPapers", {})
    if not isinstance(removed, dict) or len(removed) > _COUNT_LIMIT:
        raise ValueError("自动移除记录格式无效；不能忽略或重建移除记录")
    for doi, item in removed.items():
        if (not isinstance(doi, str) or not normalize_doi(doi) or normalize_doi(doi) != doi
                or not isinstance(item, dict) or set(item) != {"reasonCode", "removedAt"}
                or not isinstance(item.get("reasonCode"), str) or item["reasonCode"] not in REMOVAL_REASON_CODES
                or not _timestamp(item.get("removedAt"))):
            raise ValueError("自动移除记录格式无效；只允许规范 DOI、受控原因和 UTC 时间")
    return frozenset(removed)


def ensure_bootstrap(catalog, ledger, now):
    """Capture the cohort once; never silently add later daily admissions."""
    now = _now(now)
    papers = _list(catalog.get("papers"))
    removed = safe_removed_dois(ledger)
    if removed & {_object(paper).get("doi") for paper in papers if isinstance(_object(paper).get("doi"), str)}:
        raise ValueError("已移除 DOI 与现有案例冲突；恢复案例时请同时恢复其账本记录")
    if len(papers) > _COUNT_LIMIT:
        raise ValueError("初始化案例数超过安全上限")
    raw = ledger.get("bootstrap")
    if raw is None:
        if "bootstrap" in catalog or "bootstrap" in ledger:
            raise ValueError("初始化进度已存在但固定队列记录缺失；请恢复 data/ledger.json，不能自动重建或扩大队列")
        cohort = [normalize_doi(_object(paper).get("doi")) for paper in papers]
        if any(not doi for doi in cohort) or len(set(cohort)) != len(cohort):
            raise ValueError("初始化案例 DOI 无效或重复；未建立队列")
        state = {"version": 1, "cohortDOIs": cohort, "startedAt": now,
                 "status": "active" if cohort else "complete"}
    else:
        if not isinstance(raw, dict) or type(raw.get("version")) is not int or raw["version"] != 1:
            raise ValueError("初始化队列记录无效；不能自动重置固定队列")
        cohort = raw.get("cohortDOIs")
        status = raw.get("status")
        if (not isinstance(cohort, list) or len(cohort) > _COUNT_LIMIT
                or any(not isinstance(doi, str) or not normalize_doi(doi) or normalize_doi(doi) != doi for doi in cohort)
                or len(set(cohort)) != len(cohort) or not _timestamp(raw.get("startedAt"))
                or not isinstance(status, str) or status not in {"active", "needs_attention", "complete"}):
            raise ValueError("初始化队列记录无效；不能自动重置固定队列")
        state = {"version": 1, "cohortDOIs": list(cohort), "startedAt": raw["startedAt"], "status": status}
        removed_cohort = raw.get("removedDOIs", [])
        if (not isinstance(removed_cohort, list) or len(removed_cohort) > len(cohort)
                or any(not isinstance(doi, str) or doi not in cohort or doi not in removed for doi in removed_cohort)
                or len(set(removed_cohort)) != len(removed_cohort)):
            raise ValueError("初始化移除记录无效；移除 DOI 必须属于固定队列并有合法墓碑")
        if removed & set(cohort) != set(removed_cohort):
            raise ValueError("初始化移除记录不一致；不能把缺失案例自动视为合法移除")
        if removed_cohort:
            state["removedDOIs"] = list(removed_cohort)
    ledger["bootstrap"] = state
    return state


def _selected_figures(record):
    figures = [figure for figure in _list(_object(record.get("article")).get("figures")) if isinstance(figure, dict)]
    requested = record.get("figure")
    if requested is not None and not isinstance(requested, str):
        return []
    selected = figure_reference(requested)
    if selected:
        matches = []
        for figure in figures:
            reference = _object(figure.get("reference"))
            if reference.get("scope") not in {"main", "extended", "supplementary"} or not re.fullmatch(r"\d+", str(reference.get("number", ""))):
                reference = figure_reference(figure.get("label")) or {}
            if reference.get("scope") == selected["scope"] and str(reference.get("number", "")).lstrip("0") == str(selected["number"]).lstrip("0"):
                matches.append(figure)
        return matches
    return [] if isinstance(requested, str) and requested.strip() else figures


def _verified_asset(figure, root):
    asset = figure.get("asset")
    digest = figure.get("assetSHA256") if "assetSHA256" in figure else figure.get("sha256")
    if (figure.get("reuseStatus") != "allowed" or not isinstance(asset, str)
            or not re.fullmatch(r"assets/[A-Za-z0-9][A-Za-z0-9._/-]*", asset) or "//" in asset
            or any(part in {".", ".."} for part in asset.split("/"))
            or PurePosixPath(asset).suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".webp"}
            or not _hash(digest) or root is None):
        return False
    base = Path(root) / "data" / "assets"
    source = Path(root) / "data" / asset
    try:
        if base.is_symlink() or source.is_symlink() or not source.is_file() or not 0 < source.stat().st_size <= 24000000:
            return False
        source.resolve().relative_to(base.resolve())
        for parent in source.parents:
            if parent == base.parent:
                break
            if parent.is_symlink():
                return False
        raw = source.read_bytes()
        raster = (raw.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a"))
                  or raw.startswith(b"RIFF") and raw[8:12] == b"WEBP")
        if not raster or hashlib.sha256(raw).hexdigest() != digest.lower():
            return False
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as image:
            if image.width <= 0 or image.height <= 0 or image.width * image.height > 64000000:
                return False
            image.verify()
        return True
    except Exception:
        # Raster decoders may raise format-specific exceptions, including
        # decompression-bomb protection. Any unresolved asset stays incomplete.
        return False


def _fixed_scripts(repository):
    commit, repo_url = repository.get("commit"), _url(repository.get("url"))
    if (not _hash(commit, (40, 64)) or not repo_url or repo_url.hostname != "github.com"
            or not re.fullmatch(r"/[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}/?", repo_url.path)):
        return []
    result = []
    for script in _list(repository.get("scripts")):
        script = _object(script)
        parsed = _url(script.get("url"))
        path = script.get("path")
        if (parsed and parsed.hostname == "github.com" and isinstance(path, str) and path
                and not path.startswith("/") and not any(part in {".", ".."} for part in path.split("/"))
                and not any(ord(char) < 32 for char in path)
                and _hash(script.get("sha256"))
                and unquote(parsed.path) == repo_url.path.rstrip("/") + "/blob/" + commit + "/" + path):
            result.append(script)
    return result


def _documented_inputs(repository, spec):
    def documented_text(value):
        if not isinstance(value, str) or not 0 < len(value.strip()) <= 600:
            return False
        normalized = re.sub(r"[\s\[\](){}<>【】（）:_/\-]+", "", value).casefold()
        return bool(normalized and not re.fullmatch(
            r"(?:(?:列名|字段名?|输入|文件|数据|来源|column|field|source|provenance|name))?"
            r"(?:未知|待核|待核实|待确认|待定位|unknown|unverified|pending|tbd|todo|na|\?+)", normalized))
    for item in _list(repository.get("inputs")):
        item = _object(item)
        if (item.get("status") == "header_observed" and _hash(item.get("headerSHA256"))
                and any(isinstance(field, dict) and field.get("kind") == "actual_file_header"
                        and isinstance(field.get("name"), str) and field["name"].strip()
                        for field in _list(item.get("fields")))):
            return True
    spec = _object(spec)
    for item in _list(spec.get("files")) + _list(_object(spec.get("automaticInputs")).get("files")):
        for field in _list(_object(item).get("fields")):
            if (isinstance(field, list) and len(field) >= 3
                    and documented_text(field[0]) and documented_text(field[2])):
                return True  # Documented selected input notes, explicitly not an observed data table.
    return False


def _validated_current_ai(record):
    """Reuse the provider/cache validator, not a generated flag or prose presence."""
    from .enrichment import _SUMMARY_FIELDS, _validate_summary
    from .pipeline import evidence_for  # Runtime import avoids initialization cycle.
    ai = _object(record.get("ai"))
    if ai.get("author_code_executed") is True:
        return False
    current = ai.get("current_source_fingerprint")
    if current is not None and (not _hash(current) or current != ai.get("source_fingerprint")):
        return False
    try:
        allowed = {item["id"] for item in evidence_for(record) if isinstance(item, dict) and isinstance(item.get("id"), str)}
        return _validate_summary({key: ai.get(key) for key in _SUMMARY_FIELDS}, allowed) is not None
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def bootstrap_assessment(record, spec=None, root=None):
    """Assess real static components; bare curated links cannot finish the cohort."""
    record = _object(record)
    automation = _object(record.get("automation"))
    stages = {key: value if isinstance(value, str) else "unknown"
              for key, value in _object(automation.get("stages")).items()}
    article, repository = _object(record.get("article")), _object(record.get("repository"))
    ai, metrics = _object(record.get("ai")), _object(record.get("metrics"))
    gaps = []
    def gap(code):
        if code not in gaps:
            gaps.append(code)
    def effective(value):
        status = value.get("last_attempt_status") or value.get("status")
        return status if isinstance(status, str) else "unknown"
    if automation.get("status") == "excluded" or record.get("runPerformed") is True:
        gap("case_excluded")
    if stages.get("metadata") != "matched" or not _timestamp(automation.get("lastCheckedAt")):
        gap("metadata_unchecked")
    if any(stages.get(name) in _TRANSIENT for name in ("metadata", "article", "repository")):
        gap("source_transient_failure")
    if any(value.get("retained_from_previous") is True or effective(value) != "inspected" for value in (article, repository)):
        gap("source_refresh_failed")
    if stages.get("article") != "inspected" or article.get("status") != "inspected":
        gap("no_final_version" if article.get("status") == "no_final_version" else "article_unchecked")
    figures = _selected_figures(record)
    if not figures:
        gap("selected_figure_missing")
    elif not any(f.get("reuseStatus") == "allowed" for f in figures):
        gap("figure_reuse_blocked")
    elif not any(_verified_asset(f, root) for f in figures):
        gap("figure_asset_unverified")
    if stages.get("repository") != "inspected" or repository.get("status") != "inspected":
        gap("code_unchecked")
    if not _hash(repository.get("commit"), (40, 64)):
        gap("code_not_pinned")
    if not _fixed_scripts(repository):
        gap("script_integrity_missing")
    repo_url = repository.get("url")
    declared = [item.get("url") for item in _list(article.get("codeURLEvidence")) if isinstance(item, dict)]
    # Curated author-repository attribution may be retained, but never replaces
    # fixed-commit source inspection or claims precise figure/script binding.
    if not isinstance(repo_url, str) or repo_url.rstrip("/") not in {
            value.rstrip("/") for value in declared + [record.get("codeURL")] if isinstance(value, str) and _url(value)}:
        gap("author_code_link_unverified")
    if not _documented_inputs(repository, spec):
        gap("input_undocumented")
    ai_status = effective(ai)
    if stages.get("ai") in {None, "not_checked", "missing_evidence", "deferred", "deferred_until_selection"}:
        ai_status = "missing_evidence"
    if ai_status in {"missing_key", "missing_model", "missing_endpoint", "invalid_endpoint"}:
        gap("ai_configuration_missing")
    elif ai_status in {"authentication_error", "permission_denied"}:
        gap("ai_authentication_failed")
    elif ai_status == "payment_required":
        gap("ai_payment_required")
    elif ai_status == "invalid_response":
        gap("ai_response_invalid")
    elif isinstance(ai_status, str) and ai_status in _TRANSIENT:
        gap("ai_transient_failure")
    elif ai.get("status") == "stale":
        gap("ai_stale")
    elif (ai.get("status") != "generated" or stages.get("ai") != "generated"
          or not isinstance(ai.get("summary_zh"), str) or not ai["summary_zh"].strip()):
        gap("ai_missing")
    elif not _validated_current_ai(record):
        gap("ai_response_invalid")
    metric_status = effective(metrics)
    if stages.get("metrics") in {None, "not_checked", "deferred", "deferred_until_selection"}:
        metric_status = "not_checked"
    if metric_status == "missing_key":
        gap("metrics_configuration_missing")
    elif (metric_status not in {"matched", "not_found", "not_applicable"}
          or stages.get("metrics") not in {"matched", "not_found", "not_applicable"}
          or not _timestamp(metrics.get("retrieved_at")) or metrics.get("source") not in {"EasyScholar", "preprint"}
          or metric_status == "matched" and not any(isinstance(metrics.get(key), str) and metrics[key].strip()
                                                    for key in ("if", "jcr", "cas"))):
        gap("metrics_query_failed" if isinstance(metric_status, str) and metric_status in _TRANSIENT else "metrics_unchecked")
    # Only stages actually attempted this round count as a budget deferral.
    # Retained prior AI/metric diagnostics must not erase today's source failures.
    budget_deferred = any(value == "budget_exhausted" for value in stages.values())
    if budget_deferred:
        gap("request_budget_deferred")
    previous = _safe_record_state(record)
    status = "complete" if not gaps else "needs_attention" if previous.get("status") == "needs_attention" or "case_excluded" in gaps else "pending"
    completed_scan = (stages.get("metadata") == "matched"
                      and stages.get("article") in {"inspected", "no_final_version", "not_found", "excluded", "retracted", "doi_mismatch"}
                      and stages.get("repository") in {"inspected", "no_scripts_found", "not_found", "link_only_private"})
    attempted = any(isinstance(stages.get(name), str) and stages[name] not in _DEFERRED
                    for name in ("metadata", "article", "repository"))
    return {"status": status, "reasonCodes": gaps[:20], "sourceScanCompleted": completed_scan,
            "budgetDeferred": budget_deferred, "attemptPerformed": bool(attempted), "bindingVerified": False}


def note_bootstrap_attempt(record, spec, now, root=None, attempt_status=None):
    """Record bounded outcomes; skipped budgets never exhaust the attempt allowance."""
    now = _now(now)
    previous = _safe_record_state(record)
    assessment = bootstrap_assessment(record, spec, root)
    budget = assessment["budgetDeferred"] or attempt_status == "budget_exhausted"
    attempts, scans = previous["attempts"], previous["unsuccessfulScans"]
    source_gap_scans = dict(previous.get("sourceGapScans", {}))
    if previous.get("status") == "complete":
        # A later regression is a new unresolved cycle. Historical attempts
        # before successful collection must not exhaust its retry allowance.
        attempts, scans = 0, 0
        source_gap_scans = {}
    if not budget and (assessment["attemptPerformed"] or attempt_status == "error"):
        attempts = min(_COUNT_LIMIT, attempts + 1)
        if assessment["status"] != "complete" and assessment["sourceScanCompleted"]:
            scans = min(_COUNT_LIMIT, scans + 1)
    if not budget and assessment["sourceScanCompleted"]:
        # AI/metric failures alone cannot authorize a later material deletion.
        # Count each current material gap only after a clean completed source scan.
        from .pruning import uncollectable_reasons
        current_reasons = uncollectable_reasons(record, spec, root)
        if current_reasons:
            source_gap_scans = {code: min(_COUNT_LIMIT, source_gap_scans.get(code, 0) + 1)
                                for code in current_reasons}
        elif "source_transient_failure" not in assessment["reasonCodes"]:
            source_gap_scans = {}
    status = assessment["status"]
    reasons = assessment["reasonCodes"]
    if budget and "request_budget_deferred" not in reasons:
        reasons = (reasons + ["request_budget_deferred"])[:20]
    if budget:
        status = "pending"
    elif status != "complete" and (set(reasons) & _HUMAN_ACTION or scans >= 3 or attempts >= 3):
        status = "needs_attention"
    state = {"attempts": attempts, "unsuccessfulScans": scans, "lastAttemptAt": now,
             "status": status, "reasonCodes": reasons, "sourceGapScans": source_gap_scans}
    if status == "pending" and not budget and attempts:
        state["nextAttemptAt"] = (_timestamp(now) + timedelta(hours=min(24, 2 ** min(4, attempts - 1)))).isoformat().replace("+00:00", "Z")
    if not isinstance(record.get("automation"), dict):
        record["automation"] = {}
    record["automation"]["bootstrap"] = state
    return state


def bootstrap_report(catalog, ledger, root=None, now=None):
    """Refresh a safe fixed-cohort summary; unresolved attention never means done."""
    state = ensure_bootstrap(catalog, ledger, _now(now))
    papers = {paper.get("doi"): paper for paper in _list(catalog.get("papers")) if isinstance(paper, dict)}
    specs = _object(catalog.get("inputSpecs"))
    counts, issues = Counter(), []
    removed_cohort = set(state.get("removedDOIs", []))
    for doi in state["cohortDOIs"]:
        if doi in removed_cohort:
            counts["removed"] += 1
            continue
        paper = papers.get(doi)
        assessment = (bootstrap_assessment(paper, specs.get(paper.get("id")), root) if paper
                      else {"status": "needs_attention", "reasonCodes": ["cohort_record_missing"]})
        counts[assessment["status"]] += 1
        if assessment["status"] != "complete" and len(issues) < 100:
            issues.append({"doi": doi, "reasonCodes": assessment["reasonCodes"]})
    status = "needs_attention" if counts["needs_attention"] else "active" if counts["pending"] else "complete"
    summary = {"status": status, "totalCount": len(state["cohortDOIs"]), "completeCount": counts["complete"],
               "pendingCount": counts["pending"], "attentionCount": counts["needs_attention"], "removedCount": counts["removed"],
               "startedAt": state["startedAt"], "issues": issues}
    state.update({key: value for key, value in summary.items() if key != "issues"})
    return summary


def bootstrap_plan(catalog, ledger, limit, now, root=None, retry_attention=False):
    """Use all repair slots for this cohort; unattempted cases cannot starve."""
    now = _now(now)
    state = ensure_bootstrap(catalog, ledger, now)
    cohort = set(state["cohortDOIs"])
    specs = _object(catalog.get("inputSpecs"))
    planned = []
    for record in _list(catalog.get("papers")):
        if not isinstance(record, dict) or record.get("doi") not in cohort:
            continue
        assessment = bootstrap_assessment(record, specs.get(record.get("id")), root)
        previous = _safe_record_state(record)
        if assessment["status"] == "complete" or assessment["status"] == "needs_attention" and not retry_attention:
            continue
        last, upcoming = _timestamp(previous.get("lastAttemptAt")), _timestamp(previous.get("nextAttemptAt"))
        if not retry_attention and last and upcoming and last <= _timestamp(now) < upcoming:
            continue
        planned.append(record)
    planned.sort(key=lambda record: (_safe_record_state(record)["attempts"] > 0,
        _safe_record_state(record).get("lastAttemptAt", ""), record.get("id", "")))
    return planned[:max(0, min(50, limit))] if type(limit) is int else []
