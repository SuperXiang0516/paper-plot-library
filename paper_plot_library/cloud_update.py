"""Finite daily collection toward five genuinely admitted cases, with durable budgets.

This module never loosens the pipeline's source/evidence admission gates. Each
batch is independent and bounded; the UTC-day ledger prevents a rerun from
resetting paid/network allowances. Interrupted unknown usage is charged at the
reserved ceiling, not falsely presented as confirmed HTTP requests.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, timezone
import fcntl
import math
from pathlib import Path
import time

from .bootstrap import _timestamp
from .config import load_settings
from .http import HttpClient
from .scope import eligible_daily_dois
from .session import _limits
from .storage import project_lock, read_json, write_json


DAILY_SESSION_STATUSES = frozenset({"active", "paused", "finished"})
DAILY_SESSION_REASONS = frozenset({"", "target_reached", "batch_limit_reached", "ai_budget_exhausted", "http_budget_exhausted",
            "time_limit_reached", "ai_configuration_blocked", "checkpoint_callback_failed", "batch_failed",
            "interrupted", "no_progress", "discovery_failed", "initialization_pending", "day_changed"})
_STATUS = DAILY_SESSION_STATUSES
_REASONS = DAILY_SESSION_REASONS
_FATAL = {"missing_key", "missing_model", "missing_endpoint", "invalid_endpoint", "authentication_error",
          "payment_required", "permission_denied", "invalid_request", "endpoint_not_found"}
_COUNTERS = {"batchCount": (0, 36), "requests": (0, 1800), "aiAttempts": (0, 165),
             "uncertainRequests": (0, 1800), "uncertainAiAttempts": (0, 165), "elapsedSeconds": (0, 3300),
             "maxBatches": (1, 36), "maxRequests": (1, 1800), "maxAiAttempts": (0, 165),
             "maxSeconds": (60, 3300), "dailyNewCount": (0, 5)}
_PUBLIC_BOUNDS = {key: bounds for key, bounds in _COUNTERS.items()
                  if key not in {"elapsedSeconds", "maxSeconds"}}
_PUBLIC_BOUNDS.update(targetCount=(0, 5), inFlightCount=(0, 1))
DAILY_SESSION_COUNTERS = frozenset(_PUBLIC_BOUNDS)


def stamp():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def public_daily_summary(value):
    """Strict public projection: controlled enums, bounded integers and one UTC date."""
    if (not isinstance(value, dict) or not isinstance(value.get("date"), str)
            or not isinstance(value.get("status"), str) or value["status"] not in DAILY_SESSION_STATUSES
            or not isinstance(value.get("endReason"), str) or value["endReason"] not in DAILY_SESSION_REASONS):
        return None
    try:
        if date.fromisoformat(value["date"]).isoformat() != value["date"]:
            return None
    except ValueError:
        return None
    result = {key: value[key] for key in ("date", "status", "endReason")}
    # Older published summaries did not include timestamps. When provided,
    # expose only controlled UTC values, never arbitrary status/provider text.
    for key in ("startedAt", "updatedAt"):
        if key in value:
            if not _timestamp(value[key]):
                return None
            result[key] = value[key]
    if ("startedAt" in result and result["startedAt"][:10] != result["date"]
            or "startedAt" in result and "updatedAt" in result
                and _timestamp(result["updatedAt"]) < _timestamp(result["startedAt"])):
        return None
    for key, (low, high) in _PUBLIC_BOUNDS.items():
        number = value.get(key)
        if type(number) is not int or not low <= number <= high:
            return None
        result[key] = number
    if (result["requests"] + result["uncertainRequests"] > result["maxRequests"]
            or result["aiAttempts"] + result["uncertainAiAttempts"] > result["maxAiAttempts"]
            or result["aiAttempts"] > result["requests"] or result["uncertainAiAttempts"] > result["uncertainRequests"]
            or result["batchCount"] > result["maxBatches"]
            or (result["status"] == "finished") != (result["endReason"] == "target_reached")
            or result["status"] == "finished" and result["dailyNewCount"] < result["targetCount"]
            or result["status"] == "active" and result["endReason"] != ""
            or result["status"] == "paused" and not result["endReason"]
            or result["status"] != "active" and result["inFlightCount"]
                and not (result["status"] == "paused" and result["endReason"] == "checkpoint_callback_failed")
            or type(value.get("localCheckpointSaved")) is not bool):
        return None
    result["localCheckpointSaved"] = value["localCheckpointSaved"]
    return result


def _state(raw):
    fields = {"version", "date", "startedAt", "updatedAt", "status", "endReason"} | set(_COUNTERS)
    if (not isinstance(raw, dict) or not fields <= set(raw) or set(raw) - fields - {"inFlight"}
            or type(raw.get("version")) is not int or raw["version"] != 1
            or not isinstance(raw.get("date"), str) or not _timestamp(raw.get("startedAt"))
            or not _timestamp(raw.get("updatedAt")) or not isinstance(raw.get("status"), str)
            or raw["status"] not in _STATUS or not isinstance(raw.get("endReason"), str)
            or raw["endReason"] not in _REASONS):
        raise ValueError("日更连续用量记录无效；不能自动重置付费预算")
    try:
        if date.fromisoformat(raw["date"]).isoformat() != raw["date"] or raw["startedAt"][:10] != raw["date"]:
            raise ValueError()
    except ValueError:
        raise ValueError("日更连续日期无效；不能自动重置付费预算") from None
    result = {key: raw[key] for key in fields}
    for key, (low, high) in _COUNTERS.items():
        if type(raw[key]) is not int or not low <= raw[key] <= high:
            raise ValueError("日更连续用量或上限无效；不能自动重置付费预算")
    if (result["requests"] + result["uncertainRequests"] > result["maxRequests"]
            or result["aiAttempts"] + result["uncertainAiAttempts"] > result["maxAiAttempts"]
            or result["aiAttempts"] > result["requests"] or result["uncertainAiAttempts"] > result["uncertainRequests"]
            or result["batchCount"] > result["maxBatches"]
            or (result["status"] == "finished") != (result["endReason"] == "target_reached")
            or result["status"] == "active" and result["endReason"] != ""
            or result["status"] == "paused" and not result["endReason"]):
        raise ValueError("日更连续累计用量超出已保存上限")
    inflight = raw.get("inFlight")
    if inflight is not None:
        if (not isinstance(inflight, dict) or set(inflight) != {"startedAt", "reservedRequests", "reservedAiAttempts"}
                or not _timestamp(inflight.get("startedAt")) or inflight["startedAt"][:10] != result["date"]
                or result["status"] != "active"
                    and not (result["status"] == "paused" and result["endReason"] == "checkpoint_callback_failed")
                or result["batchCount"] < 1 or type(inflight.get("reservedRequests")) is not int
                or not 1 <= inflight["reservedRequests"] <= 150
                or type(inflight.get("reservedAiAttempts")) is not int
                or not 0 <= inflight["reservedAiAttempts"] <= min(15, inflight["reservedRequests"])
                or result["requests"] + result["uncertainRequests"] + inflight["reservedRequests"] > result["maxRequests"]
                or result["aiAttempts"] + result["uncertainAiAttempts"] + inflight["reservedAiAttempts"] > result["maxAiAttempts"]):
            raise ValueError("日更连续批次保留预算无效")
        result["inFlight"] = dict(inflight)
    return result


def _read(root, moment):
    from .pipeline import validate_catalog, validate_ledger
    catalog = validate_catalog(read_json(root / "data" / "catalog.json"))
    ledger = validate_ledger(read_json(root / "data" / "ledger.json", {"days": {}, "candidates": {}}), moment[:10])
    return catalog, ledger


def _count(catalog, ledger, day, policy="unrestricted"):
    counted = eligible_daily_dois(catalog, ledger.get("days", {}).get(day, {}).get("newDOIs", []), day, policy)
    if len(counted) > 5:
        raise ValueError("日更新增记录超过五篇安全上限；不能继续准入")
    return len(counted)


def _refresh_count(state, catalog, ledger, day, policy):
    counted = _count(catalog, ledger, day, policy)
    # A newly enabled research policy cannot let an old off-topic admission
    # occupy a qualified slot. This changes no spent or reserved allowance.
    state["dailyNewCount"] = max(state["dailyNewCount"], counted) if policy == "unrestricted" else counted


def _summary(state, target):
    result = {key: state[key] for key in ("date", "startedAt", "updatedAt", "status", "endReason", "batchCount", "requests", "aiAttempts",
        "uncertainRequests", "uncertainAiAttempts", "dailyNewCount", "maxBatches", "maxRequests", "maxAiAttempts")}
    result.update(targetCount=target, inFlightCount=int("inFlight" in state), localCheckpointSaved=True)
    return result


def _save(root, state, moment, target):
    from .pipeline import persistent_catalog
    from .site import build_site
    state["updatedAt"] = moment
    state = _state(state)
    with project_lock(root):
        catalog, ledger = _read(root, moment)
        reports = read_json(root / "data" / "updates.json", [])
        if not isinstance(reports, list) or any(not isinstance(item, dict) for item in reports):
            raise ValueError("更新历史格式无效；未覆盖已有日更记录")
        summary = public_daily_summary(_summary(state, target))
        if summary is None:
            raise ValueError("日更公开摘要无效；未覆盖已有记录")
        ledger["dailyCollectionSession"] = state
        catalog["dailySession"] = summary
        write_json(root / "data" / "ledger.json", ledger)
        write_json(root / "data" / "catalog.json", persistent_catalog(catalog))
        if reports and reports[0].get("date") == state["date"]:
            reports[0]["dailySession"] = summary
            write_json(root / "data" / "updates.json", reports)
        build_site(root, catalog, reports)
    return state


def _recover(state):
    inflight = state.pop("inFlight", None)
    if inflight:
        state["uncertainRequests"] += inflight["reservedRequests"]
        state["uncertainAiAttempts"] += inflight["reservedAiAttempts"]


@contextmanager
def _daily_lock(root):
    path = root / ".ppl" / "daily-session.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("已有连续日更正在运行；不重复启动") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def run_daily_session(root, settings=None, batch_callback=None, client_factory=None, adapters=None,
                      now=stamp, clock=time.monotonic, batch_runner=None, batch_start_callback=None):
    """Collect until the true daily quota, a persisted ceiling, or no new candidate work."""
    root = Path(root)
    settings = settings or load_settings(root)
    limits = _limits(settings, daily=True)
    target = settings.max_new_per_day
    if type(target) is not int or not 0 <= target <= 5:
        raise ValueError("日更目标必须为零至五篇")
    from .pipeline import run_pipeline
    runner = batch_runner or run_pipeline
    started = clock()
    if type(started) not in (int, float) or not math.isfinite(started):
        raise ValueError("日更连续计时器无效")
    with _daily_lock(root):
        moment = now()
        if not _timestamp(moment):
            raise ValueError("日更连续时间无效")
        day = moment[:10]
        with project_lock(root):
            catalog, ledger = _read(root, moment)
            raw = ledger.get("dailyCollectionSession")
            if raw is None and "dailyCollectionSession" in ledger:
                raise ValueError("日更连续预算记录缺失；不能重新开始计数")
            state = _state(raw) if raw is not None else None
            if state and state["date"] > day:
                raise ValueError("日更连续日期在未来；不能重置预算")
            if state is None or state["date"] < day:
                state = {"version": 1, "date": day, "startedAt": moment, "updatedAt": moment,
                    "status": "active", "endReason": "", "batchCount": 0, "requests": 0, "aiAttempts": 0,
                    "uncertainRequests": 0, "uncertainAiAttempts": 0, "elapsedSeconds": 0,
                    "maxBatches": limits["session_max_batches"], "maxRequests": limits["session_max_requests"],
                    "maxAiAttempts": limits["session_max_ai_attempts"], "maxSeconds": limits["session_max_seconds"],
                    "dailyNewCount": _count(catalog, ledger, day, settings.research_scope)}
            else:
                _recover(state)
                _refresh_count(state, catalog, ledger, day, settings.research_scope)
                # Upgrade only the known legacy batch ceiling after the full
                # saved state has passed validation. Paid/network/time limits,
                # confirmed and unknown use, and admissions remain untouched.
                # Any lower configured ceiling still binds via min() below.
                if state["maxBatches"] == 12 and limits["session_max_batches"] > 12:
                    state["maxBatches"] = limits["session_max_batches"]
        elapsed_before = state["elapsedSeconds"]
        state.update(status="active", endReason="")
        state = _save(root, state, moment, target)

        def stop(reason, finished=False):
            state["elapsedSeconds"] = min(3300, elapsed_before + math.ceil(max(0, clock() - started)))
            state.update(status="finished" if finished else "paused", endReason=reason)
            return _summary(_save(root, state, now(), target), target)

        if state["dailyNewCount"] >= target:
            return stop("target_reached", finished=True)
        while True:
            moment = now()
            if not _timestamp(moment):
                raise ValueError("日更连续时间无效")
            if moment[:10] != day:
                return stop("day_changed")
            with project_lock(root):
                catalog, ledger = _read(root, moment)
                _refresh_count(state, catalog, ledger, day, settings.research_scope)
            if state["dailyNewCount"] >= target:
                return stop("target_reached", finished=True)
            if state["batchCount"] >= min(state["maxBatches"], limits["session_max_batches"]):
                return stop("batch_limit_reached")
            elapsed = elapsed_before + max(0, clock() - started)
            seconds_left = min(state["maxSeconds"], limits["session_max_seconds"]) - elapsed
            if seconds_left <= 0:
                return stop("time_limit_reached")
            requests_left = min(state["maxRequests"], limits["session_max_requests"]) - state["requests"] - state["uncertainRequests"]
            ai_left = min(state["maxAiAttempts"], limits["session_max_ai_attempts"]) - state["aiAttempts"] - state["uncertainAiAttempts"]
            if requests_left <= 0:
                return stop("http_budget_exhausted")
            if ai_left <= 0 and settings.llm_api_key and settings.llm_model:
                return stop("ai_budget_exhausted")
            # Daily collection has its own work and budget. Old material gaps
            # remain visible in bootstrap reports but cannot take new-paper
            # slots or paid attempts away from today's successful admissions.
            batch_settings = replace(settings,
                # Older callers leave daily_body_image_first unset.  Preserve
                # their strict synthetic/test mode; the project config opts
                # into image-first daily collection explicitly.
                require_complete_new_cases=(settings.require_complete_new_cases
                                            or not getattr(settings, "daily_body_image_first", False)),
                discovery_limit=100, repair_limit=0,
                max_papers_per_run=min(5, settings.max_papers_per_run),
                max_ai_attempts_per_paper=min(3, settings.max_ai_attempts_per_paper),
                max_requests=min(150, settings.max_requests, requests_left),
                max_ai_calls=min(15, settings.max_ai_calls, max(0, ai_left)))
            batch_settings.max_ai_calls = min(batch_settings.max_ai_calls, batch_settings.max_requests)
            state["batchCount"] += 1
            state["inFlight"] = {"startedAt": moment, "reservedRequests": batch_settings.max_requests,
                                 "reservedAiAttempts": batch_settings.max_ai_calls}
            state["elapsedSeconds"] = min(3300, math.ceil(elapsed))
            state = _save(root, state, moment, target)
            if batch_start_callback:
                try:
                    batch_start_callback(_summary(state, target))
                except Exception:
                    # The upstream durable checkpoint may or may not have
                    # succeeded. Keep the reservation for conservative resume;
                    # do not falsely present it as confirmed provider traffic.
                    return stop("checkpoint_callback_failed")
            client = (client_factory(batch_settings) if client_factory else HttpClient(root / ".ppl" / "cache",
                max_requests=batch_settings.max_requests, max_post_attempts=batch_settings.max_ai_calls))
            # Persist/checkpoint and callback time must not extend this deadline.
            client.session_deadline = started + min(state["maxSeconds"], limits["session_max_seconds"]) - elapsed_before
            client.deadline_clock = clock
            try:
                report = runner(root, batch_settings, client=client, adapters=adapters, discover_new=True, mode="daily")
            except KeyboardInterrupt:
                _recover(state)
                return stop("interrupted")
            except Exception:
                _recover(state)
                return stop("time_limit_reached" if getattr(client, "time_budget_exhausted", False) else "batch_failed")
            valid = isinstance(report, dict)
            for field, ceiling in (("requests", batch_settings.max_requests), ("aiAttempts", batch_settings.max_ai_calls),
                                   ("checkedCount", batch_settings.max_papers_per_run), ("repairCount", batch_settings.max_papers_per_run)):
                valid = valid and type(report.get(field)) is int and 0 <= report[field] <= ceiling
            if valid:
                valid = (report["aiAttempts"] <= report["requests"] and report["repairCount"] <= report["checkedCount"]
                         and report["requests"] == getattr(client, "requests", report["requests"])
                         and report["aiAttempts"] == getattr(client, "post_attempts", report["aiAttempts"]))
            if not valid:
                _recover(state)
                return stop("batch_failed")
            state.pop("inFlight")
            state["requests"] += report["requests"]
            state["aiAttempts"] += report["aiAttempts"]
            with project_lock(root):
                catalog, ledger = _read(root, now())
                _refresh_count(state, catalog, ledger, day, settings.research_scope)
            state["elapsedSeconds"] = min(3300, elapsed_before + math.ceil(max(0, clock() - started)))
            state = _save(root, state, now(), target)
            summary = _summary(state, target)
            if batch_callback:
                try:
                    batch_callback(report, summary)
                except Exception:
                    return stop("checkpoint_callback_failed")
            if getattr(client, "time_budget_exhausted", False):
                return stop("time_limit_reached")
            if isinstance(report.get("aiStoppedStatus"), str) and report["aiStoppedStatus"] in _FATAL:
                return stop("ai_configuration_blocked")
            if state["dailyNewCount"] >= target:
                return stop("target_reached", finished=True)
            if report["checkedCount"] <= report["repairCount"]:
                return stop("discovery_failed" if report.get("discoveryStatus") == "partial_failure" else "no_progress")
