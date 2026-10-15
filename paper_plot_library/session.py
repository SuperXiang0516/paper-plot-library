"""Finite, resumable sessions over existing papers only; never daily discovery.

A traversal is not a claim of complete materials or figure reproduction. Each
batch has its own five-paper/paid/network budgets; the persisted session applies
additional cumulative ceilings, including conservative unknown interrupted use.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import math
from pathlib import Path
import re
import time

from .bootstrap import bootstrap_assessment, bootstrap_plan, _safe_record_state, _timestamp
from .config import load_settings
from .http import HttpClient
from .sources import normalize_doi
from .storage import project_lock, read_json, write_json


SESSION_STATUSES = frozenset({"active", "paused", "finished"})
SESSION_END_REASONS = frozenset({"cohort_traversed", "no_eligible_cases", "batch_limit_reached",
    "ai_budget_exhausted", "http_budget_exhausted", "time_limit_reached", "ai_configuration_blocked",
    "checkpoint_callback_failed", "batch_failed", "interrupted"})
SESSION_COUNTERS = frozenset({"targetCount", "plannedCount", "processedCount", "remainingCount",
    "batchCount", "checkpointCount", "requests", "aiAttempts", "uncertainRequests", "uncertainAiAttempts",
    "inFlightCount", "maxBatches", "maxRequests", "maxAiAttempts", "completeCount", "pendingCount", "attentionCount", "removedCount"})
_LIMITS = {"session_max_batches": (1, 36), "session_max_ai_attempts": (0, 165),
           "session_max_requests": (1, 1800), "session_max_seconds": (60, 3300)}
_FATAL = {"missing_key", "missing_model", "missing_endpoint", "invalid_endpoint", "authentication_error",
          "payment_required", "permission_denied", "invalid_request", "endpoint_not_found"}


def stamp():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _limits(settings, *, daily=False):
    values = {}
    for name, (low, high) in _LIMITS.items():
        value = getattr(settings, name)
        if type(value) is not int or not low <= value <= high:
            raise ValueError("连续补库预算无效；未开始请求")
        values[name] = value
    # The shared project setting may give new-paper discovery more small
    # batches. Explicit old-library passes retain their established ceiling.
    if not daily:
        values["session_max_batches"] = min(12, values["session_max_batches"])
    return values


def _dois(value, cohort=None, maximum=10000):
    if (not isinstance(value, list) or len(value) > maximum
            or any(not isinstance(doi, str) or normalize_doi(doi) != doi or not doi
                   or cohort is not None and doi not in cohort for doi in value)
            or len(set(value)) != len(value)):
        raise ValueError("连续补库固定队列或游标无效；请恢复 ledger，不能自动重置")
    return list(value)


def _state(raw):
    """Fail closed on corrupt progress, rather than resetting paid allowances."""
    if (not isinstance(raw, dict) or type(raw.get("version")) is not int or raw["version"] != 1
            or not isinstance(raw.get("id"), str) or not re.fullmatch(r"[a-f0-9]{32}", raw["id"])
            or not _timestamp(raw.get("startedAt")) or not _timestamp(raw.get("updatedAt"))
            or not isinstance(raw.get("status"), str) or raw["status"] not in SESSION_STATUSES):
        raise ValueError("连续补库记录无效；请恢复 ledger，不能自动重置")
    cohort = _dois(raw.get("cohortDOIs"))
    planned = _dois(raw.get("plannedDOIs"), cohort)
    processed = _dois(raw.get("processedDOIs"), planned)
    reason = raw.get("endReason", "")
    if not isinstance(reason, str) or reason and reason not in SESSION_END_REASONS:
        raise ValueError("连续补库结束状态无效")
    values = {"version": 1, "id": raw["id"], "startedAt": raw["startedAt"], "updatedAt": raw["updatedAt"],
              "status": raw["status"], "endReason": reason, "cohortDOIs": cohort,
              "plannedDOIs": planned, "processedDOIs": processed}
    for name, low, high in (("batchCount", 0, 12), ("checkpointCount", 0, 10000),
            ("requests", 0, 1800), ("aiAttempts", 0, 165), ("uncertainRequests", 0, 1800),
            ("uncertainAiAttempts", 0, 165), ("maxBatches", 1, 12), ("maxRequests", 1, 1800),
            ("maxAiAttempts", 0, 165), ("maxSeconds", 60, 3300), ("elapsedSeconds", 0, 3300)):
        value = raw.get(name)
        if type(value) is not int or not low <= value <= high:
            raise ValueError("连续补库用量或预算无效；不能重新开始计数")
        values[name] = value
    if values["requests"] + values["uncertainRequests"] > 1800 or values["aiAttempts"] + values["uncertainAiAttempts"] > 165:
        raise ValueError("连续补库累计用量超出安全上限")
    inflight = raw.get("inFlight")
    if inflight is not None:
        if not isinstance(inflight, dict) or not _timestamp(inflight.get("startedAt")):
            raise ValueError("连续补库批次检查点无效")
        chosen = _dois(inflight.get("dois"), planned, 5)
        requests, ai = inflight.get("reservedRequests"), inflight.get("reservedAiAttempts")
        if (not chosen or type(requests) is not int or not 1 <= requests <= 150
                or type(ai) is not int or not 0 <= ai <= 15
                or values["requests"] + values["uncertainRequests"] + requests > 1800
                or values["aiAttempts"] + values["uncertainAiAttempts"] + ai > 165):
            raise ValueError("连续补库批次保留预算无效")
        before = inflight.get("beforeCheckedAt")
        if not isinstance(before, dict) or set(before) != set(chosen) or any(not isinstance(value, str) for value in before.values()):
            raise ValueError("连续补库来源检查游标无效")
        values["inFlight"] = {"dois": chosen, "startedAt": inflight["startedAt"],
            "reservedRequests": requests, "reservedAiAttempts": ai,
            "beforeCheckedAt": {doi: value if _timestamp(value) else "" for doi, value in before.items()}}
    return values


def _new_state(catalog, settings, now, previous=None):
    limits = _limits(settings)
    cohort = _dois([normalize_doi(paper.get("doi")) for paper in catalog["papers"]])
    identity = hashlib.sha256(json.dumps([now, cohort, previous.get("id") if previous else ""], sort_keys=True).encode()).hexdigest()[:32]
    return {"version": 1, "id": identity, "startedAt": now, "updatedAt": now, "status": "active", "endReason": "",
        "cohortDOIs": cohort, "plannedDOIs": [], "processedDOIs": [], "batchCount": 0, "checkpointCount": 0,
        "requests": 0, "aiAttempts": 0, "uncertainRequests": 0, "uncertainAiAttempts": 0,
        "maxBatches": limits["session_max_batches"], "maxRequests": limits["session_max_requests"],
        "maxAiAttempts": limits["session_max_ai_attempts"], "maxSeconds": limits["session_max_seconds"], "elapsedSeconds": 0}


def session_summary(catalog, state, root=None, ledger=None):
    state = _state(state)
    from .bootstrap import safe_removed_dois
    if ledger is None:
        ledger = read_json(Path(root) / "data" / "ledger.json", {"days": {}, "candidates": {}}) if root else {}
    removed = safe_removed_dois(ledger)
    papers = {paper["doi"]: paper for paper in catalog["papers"]}
    specs = catalog.get("inputSpecs", {})
    counts = Counter()
    complete_unprocessed = 0
    for doi in state["cohortDOIs"]:
        paper = papers.get(doi)
        if paper is None and doi in removed:
            counts["removed"] += 1
            continue
        assessment = bootstrap_assessment(paper, specs.get(paper.get("id")), root) if paper else {"status": "needs_attention"}
        counts[assessment["status"]] += 1
        complete_unprocessed += int(assessment["status"] == "complete" and doi not in state["processedDOIs"])
    material_status = ("needs_attention" if counts["needs_attention"] else "active" if counts["pending"]
                       else "complete_with_removals" if counts["removed"] else "complete")
    result = {key: state[key] for key in ("version", "id", "startedAt", "updatedAt", "status", "endReason",
        "batchCount", "checkpointCount", "requests", "aiAttempts", "uncertainRequests", "uncertainAiAttempts",
        "maxBatches", "maxRequests", "maxAiAttempts")}
    result.update(targetCount=len(state["cohortDOIs"]), plannedCount=len(state["plannedDOIs"]),
        processedCount=len(state["processedDOIs"]), remainingCount=max(0, len(state["cohortDOIs"]) - len(state["processedDOIs"]) - complete_unprocessed
            - sum(doi in removed and doi not in papers and doi not in state["processedDOIs"] for doi in state["cohortDOIs"])),
        inFlightCount=len(state.get("inFlight", {}).get("dois", [])), completeCount=counts["complete"],
        pendingCount=counts["pending"], attentionCount=counts["needs_attention"], removedCount=counts["removed"], materialsStatus=material_status,
        localCheckpointSaved=True)
    return result


@contextmanager
def _session_lock(root):
    path = root / ".ppl" / "session.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("已有连续补库正在运行；不重复启动") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _read(root, now):
    from .pipeline import validate_catalog, validate_ledger
    from .bootstrap import safe_removed_dois
    catalog = validate_catalog(read_json(root / "data" / "catalog.json"))
    ledger = validate_ledger(read_json(root / "data" / "ledger.json", {"days": {}, "candidates": {}}), now[:10])
    if safe_removed_dois(ledger) & {paper["doi"] for paper in catalog["papers"]}:
        raise ValueError("已移除 DOI 与当前案例冲突；请恢复其对应 ledger 记录")
    return catalog, ledger


def _save(root, state, now, batch_report=None):
    from .pipeline import persistent_catalog
    from .site import build_site
    state["updatedAt"] = now
    state["checkpointCount"] = min(10000, state["checkpointCount"] + 1)
    state = _state(state)
    with project_lock(root):
        catalog, ledger = _read(root, now)
        summary = session_summary(catalog, state, root, ledger)
        ledger["backfillSession"] = state
        catalog["session"] = summary
        # Fixed session queue is durable before its projected catalog summary.
        write_json(root / "data" / "ledger.json", ledger)
        write_json(root / "data" / "catalog.json", persistent_catalog(catalog))
        reports = read_json(root / "data" / "updates.json", [])
        if not isinstance(reports, list) or any(not isinstance(item, dict) for item in reports):
            raise ValueError("更新历史无效；不能覆盖已有记录")
        if batch_report and reports and reports[0].get("startedAt") == batch_report.get("startedAt"):
            reports[0]["session"] = summary
            write_json(root / "data" / "updates.json", reports)
        build_site(root, catalog, reports)
    return state, summary


def _recover_inflight(state, catalog):
    inflight = state.pop("inFlight", None)
    if not inflight:
        return
    papers = {paper["doi"]: paper for paper in catalog["papers"]}
    for doi in inflight["dois"]:
        paper = papers.get(doi, {})
        checked = paper.get("automation", {}).get("lastCheckedAt", "")
        if (_timestamp(checked) and checked != inflight["beforeCheckedAt"][doi]
                and _timestamp(checked) >= _timestamp(inflight["startedAt"]) and doi not in state["processedDOIs"]):
            state["processedDOIs"].append(doi)
    # Without a final batch report its actual usage is unknown, never zero and
    # never misreported as confirmed HTTP traffic. Do not revisit its claims.
    state["uncertainRequests"] += inflight["reservedRequests"]
    state["uncertainAiAttempts"] += inflight["reservedAiAttempts"]


def _eligible(catalog, state, now, root, auto_remove=False):
    from .pipeline import _safe_repair_schedule
    from .pruning import uncollectable_reasons
    cohort = set(state["cohortDOIs"])
    claimed = set(state["plannedDOIs"])
    specs = catalog.get("inputSpecs", {})
    chosen = []
    moment = _timestamp(now)
    blockers = {"ai_configuration_missing", "ai_authentication_failed", "ai_payment_required",
                "metrics_configuration_missing", "metrics_query_failed", "case_excluded", "source_transient_failure"}
    for paper in catalog["papers"]:
        if paper["doi"] not in cohort or paper["doi"] in claimed:
            continue
        spec = specs.get(paper.get("id"))
        assessment = bootstrap_assessment(paper, spec, root)
        previous = _safe_record_state(paper)
        attention = assessment["status"] == "needs_attention"
        if assessment["status"] == "complete":
            continue
        if attention:
            if auto_remove is not True or blockers & set(assessment["reasonCodes"]):
                continue
            checked = _timestamp(paper.get("automation", {}).get("lastCheckedAt"))
            gaps = uncollectable_reasons(paper, spec, root)
            if (not checked or not gaps or not any(previous.get("sourceGapScans", {}).get(code, 0) < 3 for code in gaps)):
                continue
        # retry_attention=True is used only after our independent filtering. It
        # must never bypass the normal cooldown or immediately rescan a legacy
        # attention record whose old AI attempts exhausted the generic allowance.
        schedules = [previous]
        if attention:
            schedules.append(_safe_repair_schedule(paper))
        cooling = False
        for schedule in schedules:
            last, upcoming = _timestamp(schedule.get("lastAttemptAt")), _timestamp(schedule.get("nextAttemptAt"))
            if last and upcoming and last <= moment < upcoming:
                cooling = True
        if attention:
            last = _timestamp(previous.get("lastAttemptAt")) or checked
            # Attention states do not get a bootstrap nextAttemptAt. Preserve a
            # conservative daily source-review interval rather than making an
            # explicit next session a repeated same-minute absence scan.
            if last and last <= moment < last + timedelta(hours=24):
                cooling = True
        if not cooling:
            chosen.append(paper)
    selected = dict(catalog, papers=chosen)
    ledger = {"bootstrap": {"version": 1, "cohortDOIs": state["cohortDOIs"], "startedAt": state["startedAt"], "status": "active"}}
    return bootstrap_plan(selected, ledger, 5, now, root=root, retry_attention=True)


def run_backfill_session(root, settings=None, *, batch_callback=None, batch_start_callback=None, client_factory=None,
                         adapters=None, batch_runner=None, new_session=False, now=stamp, clock=time.monotonic):
    """Continue the persisted finite pass; completed/claimed cases are never repeated.

    ``batch_start_callback(summary)`` runs after durable in-flight reservation
    and before any collection/AI transport. ``batch_callback(report, summary)``
    runs after durable local batch save. Callback failures stop without deleting
    saved material or silently releasing a potentially cloud-saved reservation.
    Starting a new pass after a finished one requires explicit ``new_session``.
    """
    root = Path(root)
    settings = settings or load_settings(root)
    limits = _limits(settings)
    from .pipeline import run_pipeline
    runner = batch_runner or run_pipeline
    started = clock()
    if not isinstance(started, (int, float)) or not math.isfinite(started):
        raise ValueError("连续补库计时器无效")
    with _session_lock(root):
        moment = now()
        if not _timestamp(moment):
            raise ValueError("连续补库时间无效")
        with project_lock(root):
            catalog, ledger = _read(root, moment)
            raw = ledger.get("backfillSession")
            if raw is None and ("backfillSession" in ledger or "session" in catalog):
                raise ValueError("连续补库游标缺失；请恢复 ledger，不能自动重新付费")
            state = _state(raw) if raw is not None else None
            if new_session and state and state.get("inFlight"):
                raise ValueError("中断批次尚未恢复；请先继续现有session，不能重置未知用量")
            if not state or new_session:
                state = _new_state(catalog, settings, moment, state)
            elif state["status"] == "finished":
                return session_summary(catalog, state, root)
            _recover_inflight(state, catalog)
        elapsed_before = state["elapsedSeconds"]
        deadline = started + min(state["maxSeconds"], limits["session_max_seconds"]) - elapsed_before
        state.update(status="active", endReason="")
        state, summary = _save(root, state, moment)

        def stop(reason, finished=False, report=None):
            elapsed = max(0, clock() - started)
            state["elapsedSeconds"] = min(3300, elapsed_before + math.ceil(elapsed))
            state.update(status="finished" if finished else "paused", endReason=reason)
            return _save(root, state, now(), report)[1]

        while True:
            moment = now()
            with project_lock(root):
                catalog, _ = _read(root, moment)
                chosen = _eligible(catalog, state, moment, root, settings.auto_remove_uncollectable)
            if not chosen:
                remaining = session_summary(catalog, state, root)["remainingCount"]
                return stop("cohort_traversed" if remaining == 0 else "no_eligible_cases", finished=True)
            if state["batchCount"] >= min(state["maxBatches"], limits["session_max_batches"]):
                return stop("batch_limit_reached")
            elapsed = elapsed_before + max(0, clock() - started)
            remaining_seconds = min(state["maxSeconds"], limits["session_max_seconds"]) - elapsed
            if remaining_seconds <= 0:
                return stop("time_limit_reached")
            requests_left = min(state["maxRequests"], limits["session_max_requests"]) - state["requests"] - state["uncertainRequests"]
            ai_left = min(state["maxAiAttempts"], limits["session_max_ai_attempts"]) - state["aiAttempts"] - state["uncertainAiAttempts"]
            if requests_left <= 0:
                return stop("http_budget_exhausted")
            if ai_left <= 0 and settings.llm_api_key and settings.llm_model:
                return stop("ai_budget_exhausted")
            batch_settings = replace(settings, initialize_existing_first=True,
                max_papers_per_run=min(5, settings.max_papers_per_run), bootstrap_batch_limit=min(5, settings.bootstrap_batch_limit),
                max_ai_calls=min(15, settings.max_ai_calls, max(0, ai_left)), max_ai_attempts_per_paper=min(3, settings.max_ai_attempts_per_paper),
                max_requests=min(150, settings.max_requests, requests_left))
            chosen = chosen[:min(batch_settings.max_papers_per_run, batch_settings.bootstrap_batch_limit)]
            chosen_dois = [paper["doi"] for paper in chosen]
            state["batchCount"] += 1
            state["plannedDOIs"].extend(chosen_dois)
            state["inFlight"] = {"dois": chosen_dois, "startedAt": moment,
                "reservedRequests": batch_settings.max_requests, "reservedAiAttempts": batch_settings.max_ai_calls,
                "beforeCheckedAt": {paper["doi"]: paper.get("automation", {}).get("lastCheckedAt", "") for paper in chosen}}
            state["elapsedSeconds"] = min(3300, math.ceil(elapsed))
            state, summary = _save(root, state, moment)
            if batch_start_callback:
                try:
                    batch_start_callback(summary)
                except Exception:
                    # Preserve inFlight: a remote checkpoint may have been
                    # pushed before its callback failed. No confirmed HTTP/AI
                    # usage is invented for the unstarted collection batch.
                    return stop("checkpoint_callback_failed")
            if clock() >= deadline:
                return stop("time_limit_reached")
            client = (client_factory(batch_settings) if client_factory else HttpClient(root / ".ppl" / "cache",
                max_requests=batch_settings.max_requests, max_post_attempts=batch_settings.max_ai_calls))
            client.session_deadline = deadline
            client.deadline_clock = clock
            try:
                report = runner(root, batch_settings, client=client, adapters=adapters,
                    only_dois=chosen_dois, discover_new=False, mode="backfill", session_dois=chosen_dois)
            except KeyboardInterrupt:
                with project_lock(root):
                    catalog, _ = _read(root, now())
                    _recover_inflight(state, catalog)
                return stop("interrupted")
            except Exception:
                with project_lock(root):
                    catalog, _ = _read(root, now())
                    _recover_inflight(state, catalog)
                return stop("time_limit_reached" if getattr(client, "time_budget_exhausted", False) is True else "batch_failed")
            if (not isinstance(report, dict) or type(report.get("requests")) is not int
                    or not 0 <= report["requests"] <= batch_settings.max_requests
                    or type(report.get("aiAttempts")) is not int or not 0 <= report["aiAttempts"] <= batch_settings.max_ai_calls
                    or report["aiAttempts"] > report["requests"]):
                with project_lock(root):
                    catalog, _ = _read(root, now())
                    _recover_inflight(state, catalog)
                return stop("batch_failed")
            try:
                checked = _dois(report.get("checkedDOIs"), chosen_dois, 5)
            except ValueError:
                with project_lock(root):
                    catalog, _ = _read(root, now())
                    _recover_inflight(state, catalog)
                return stop("batch_failed")
            state.pop("inFlight")
            state["requests"] += report["requests"]
            state["aiAttempts"] += report["aiAttempts"]
            state["processedDOIs"].extend(checked)
            # A known, unstarted case can be selected in a later batch; an
            # interrupted unknown claim above cannot safely be repeated.
            state["plannedDOIs"] = [doi for doi in state["plannedDOIs"] if doi not in chosen_dois or doi in checked]
            state["elapsedSeconds"] = min(3300, elapsed_before + math.ceil(max(0, clock() - started)))
            # Determine a normal terminal outcome before saving and invoking the
            # final batch callback. Its Git checkpoint sees the finished state,
            # not an active state followed by an unpublished local-only stop.
            terminal, finished = "", False
            fatal = report.get("aiStoppedStatus")
            with project_lock(root):
                current, current_ledger = _read(root, now())
                next_choices = _eligible(current, state, now(), root, settings.auto_remove_uncollectable)
                remaining = session_summary(current, state, root, current_ledger)["remainingCount"]
            if isinstance(fatal, str) and fatal in _FATAL:
                terminal = "ai_configuration_blocked"
            elif getattr(client, "time_budget_exhausted", False) is True:
                terminal = "time_limit_reached"
            elif not checked:
                terminal = "http_budget_exhausted" if report.get("collectionBudgetExhausted") else "no_eligible_cases"
                finished = not report.get("collectionBudgetExhausted")
            elif not next_choices:
                terminal, finished = ("cohort_traversed" if remaining == 0 else "no_eligible_cases"), True
            elif state["batchCount"] >= min(state["maxBatches"], limits["session_max_batches"]):
                terminal = "batch_limit_reached"
            elif state["requests"] + state["uncertainRequests"] >= min(state["maxRequests"], limits["session_max_requests"]):
                terminal = "http_budget_exhausted"
            elif (settings.llm_api_key and settings.llm_model and state["aiAttempts"] + state["uncertainAiAttempts"]
                  >= min(state["maxAiAttempts"], limits["session_max_ai_attempts"])):
                terminal = "ai_budget_exhausted"
            elif state["elapsedSeconds"] >= min(state["maxSeconds"], limits["session_max_seconds"]):
                terminal = "time_limit_reached"
            if terminal:
                state.update(status="finished" if finished else "paused", endReason=terminal)
            state, summary = _save(root, state, now(), report)
            if batch_callback:
                try:
                    batch_callback(report, summary)
                except Exception:
                    return stop("checkpoint_callback_failed", report=report)
            if terminal:
                return summary
