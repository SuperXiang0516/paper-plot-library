"""Evidence-gated removal of repeatedly uncollectable static source records.

No file deletion, transport, author execution or raw response persistence occurs
here. Git history can restore the removed record and its minimal public ledger.
"""
from __future__ import annotations

from .bootstrap import (REMOVAL_REASON_CODES, _object, _list, _now, _safe_record_state,
                        bootstrap_assessment, ensure_bootstrap, safe_removed_dois)
from .sources import normalize_doi


_TERMINAL_REPOSITORY = {"no_scripts_found", "not_found", "link_only_private"}
_TRANSIENT_SOURCE = {"error", "unavailable", "network_error", "rate_limited", "service_unavailable",
                     "other_http_error", "budget_exhausted", "authentication_error", "payment_required",
                     "permission_denied", "invalid_request", "endpoint_not_found", "deferred", "not_checked"}


def uncollectable_reasons(record, spec=None, root=None):
    """Current clean source gaps only; bounded/failed fetches are not proof of absence."""
    record = _object(record)
    automation = _object(record.get("automation"))
    stages = {key: value if isinstance(value, str) else "unknown"
              for key, value in _object(automation.get("stages")).items()}
    assessment = bootstrap_assessment(record, spec, root)
    if not assessment["sourceScanCompleted"] or assessment["budgetDeferred"]:
        return ()
    if any(stages.get(stage) in _TRANSIENT_SOURCE for stage in ("metadata", "article", "repository")
           if isinstance(stages.get(stage), str)):
        return ()
    if "source_transient_failure" in assessment["reasonCodes"]:
        return ()
    article, repository = _object(record.get("article")), _object(record.get("repository"))
    if (article.get("status") != stages.get("article") or repository.get("status") != stages.get("repository")
            or stages.get("article") not in {"inspected", "no_final_version", "not_found"}
            or stages.get("repository") not in {"inspected", *_TERMINAL_REPOSITORY}):
        return ()
    if "sourceScanLimited" in repository and type(repository["sourceScanLimited"]) is not bool:
        return ()
    if any(value.get("retained_from_previous") is True
           or isinstance(value.get("last_attempt_status"), str) and value["last_attempt_status"] in _TRANSIENT_SOURCE
           for value in (article, repository)):
        return ()
    warnings = [value for value in _list(repository.get("limitations")) if isinstance(value, str)]
    # Script fetch exceptions are swallowed by the bounded inspector; its
    # terminal-looking no_scripts_found must never turn those into deletion.
    if any(value.startswith("Source unavailable:") for value in warnings):
        return ()
    selected = _list(article.get("figures"))
    if any(_object(figure).get("downloadStatus") in {"failed", "pending"}
           for figure in selected if isinstance(_object(figure).get("downloadStatus", ""), str)):
        return ()
    inputs = [_object(value) for value in _list(repository.get("inputs"))]
    if any(value.get("status") == "header_fetch_failed" for value in inputs):
        return ()
    gaps = set(assessment["reasonCodes"])
    reasons = []
    if stages.get("article") in {"no_final_version", "not_found"}:
        reasons.append("formal_version_unavailable")
    if "selected_figure_missing" in gaps:
        reasons.append("selected_figure_unavailable")
    if "figure_reuse_blocked" in gaps:
        reasons.append("figure_reuse_unavailable")
    tree_limited = (repository.get("sourceScanLimited") is True
                    or any(value.startswith("GitHub tree truncated:") for value in warnings))
    if not tree_limited and (stages.get("repository") in _TERMINAL_REPOSITORY
                             or "author_code_link_unverified" in gaps):
        reasons.append("author_scripts_unavailable")
    # Known repository files awaiting unsupported/large/count-limited parsing
    # are a scanner limitation, not evidence that author input basis is absent.
    header_limited = any(value.get("status") == "repository_file_found" for value in inputs)
    if ("input_undocumented" in gaps and stages.get("repository") == "inspected"
            and "script_integrity_missing" not in gaps and not tree_limited and not header_limited):
        reasons.append("input_basis_unavailable")
    return tuple(reasons)


def prune_uncollectable(catalog, ledger, root=None, now=None):
    """Remove only gaps proven on three current clean source scans; return safe facts."""
    timestamp = _now(now)
    removed_before = safe_removed_dois(ledger)
    if not isinstance(catalog, dict) or not isinstance(catalog.get("papers"), list):
        raise ValueError("案例目录格式无效；未执行自动移除")
    specs = catalog.get("inputSpecs", {})
    if not isinstance(specs, dict):
        raise ValueError("输入目录格式无效；未执行自动移除")
    seen_dois, seen_ids = set(), set()
    for record in catalog["papers"]:
        if not isinstance(record, dict) or not isinstance(record.get("id"), str) or not record["id"]:
            raise ValueError("案例标识格式无效；未执行自动移除")
        doi = normalize_doi(record.get("doi"))
        if (not doi or doi != record.get("doi") or doi in removed_before
                or doi in seen_dois or record["id"] in seen_ids):
            raise ValueError("案例 DOI 或自动移除记录冲突；未执行自动移除")
        seen_dois.add(doi)
        seen_ids.add(record["id"])
    bootstrap = None
    if "bootstrap" in ledger or "bootstrap" in catalog:
        bootstrap = ensure_bootstrap(catalog, ledger, timestamp)
    decisions = []
    for record in catalog["papers"]:
        doi = record["doi"]
        previous = _safe_record_state(record)
        if previous["unsuccessfulScans"] < 3 or not previous.get("lastAttemptAt"):
            continue
        reasons = uncollectable_reasons(record, specs.get(record.get("id")), root)
        qualified = [code for code in reasons if previous.get("sourceGapScans", {}).get(code, 0) >= 3]
        if qualified:
            decisions.append({"doi": doi, "reasonCode": qualified[0], "removedAt": timestamp})
    if not decisions:
        return {"deletedDOIs": [], "reasons": {}, "removedCount": 0}
    if len(removed_before) + len(decisions) > 10000:
        raise ValueError("自动移除记录超过安全上限；未执行自动移除")
    deleted = {item["doi"] for item in decisions}
    deleted_ids = {record.get("id") for record in catalog["papers"] if record["doi"] in deleted}
    tombstones = dict(ledger.get("removedPapers", {}))
    for item in decisions:
        tombstones[item["doi"]] = {"reasonCode": item["reasonCode"], "removedAt": item["removedAt"]}
    ledger["removedPapers"] = tombstones
    if bootstrap is not None:
        cohort = set(bootstrap["cohortDOIs"])
        bootstrap["removedDOIs"] = [doi for doi in bootstrap["cohortDOIs"]
                                    if doi in tombstones and doi in cohort]
    catalog["papers"] = [record for record in catalog["papers"] if record["doi"] not in deleted]
    catalog["inputSpecs"] = {key: value for key, value in specs.items() if key not in deleted_ids}
    # Candidate ledger may contain detailed earlier admission facts. A removed
    # DOI retains only the controlled tombstone, never a candidate/full record.
    candidates = ledger.get("candidates")
    if isinstance(candidates, dict):
        for doi in deleted:
            candidates.pop(doi, None)
    return {"deletedDOIs": [item["doi"] for item in decisions],
            "reasons": {item["doi"]: item["reasonCode"] for item in decisions}, "removedCount": len(decisions)}
