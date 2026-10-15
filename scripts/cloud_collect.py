"""Cloud-only orchestration with a Git checkpoint after every finite batch.

This script never reads or prints secret values. It pushes only the known public
data paths, never the private cache, and never uses force push.
"""
import json
import os
from pathlib import Path
import subprocess
from dataclasses import replace

from paper_plot_library.bootstrap import effective_collection_mode
from paper_plot_library.cloud_update import public_daily_summary, run_daily_session
from paper_plot_library.config import load_settings
from paper_plot_library.pipeline import stamp, validate_ledger
from paper_plot_library.session import run_backfill_session
from paper_plot_library.storage import read_json


_DAILY_REASON_LABELS = {
    "": "日更尚未结束",
    "target_reached": "已达到今日成功新增目标",
    "batch_limit_reached": "已达到当天累计批次上限",
    "ai_budget_exhausted": "已达到当天累计 AI 请求上限",
    "http_budget_exhausted": "已达到当天累计 HTTP 请求上限",
    "time_limit_reached": "已达到当天累计运行时间上限",
    "ai_configuration_blocked": "AI 配置、权限或余额需要检查",
    "checkpoint_callback_failed": "云端检查点保存未确认，需要检查保存步骤",
    "batch_failed": "本批未完成，已保守记录可能发生的请求",
    "interrupted": "运行中断，已保守记录可能发生的请求",
    "no_progress": "本轮没有新的可处理候选；后续计划会在剩余额度内继续补足",
    "discovery_failed": "文献发现服务暂时失败；后续计划会在剩余额度内重试",
    "initialization_pending": "旧版本初始化阻断，需要检查运行的代码版本",
    "day_changed": "运行跨越 UTC 日期，待下一次计划按新日期继续",
}


def _daily_target_reached(summary):
    return (summary is not None and summary["status"] == "finished"
            and summary["endReason"] == "target_reached"
            and summary["dailyNewCount"] >= summary["targetCount"])


def write_daily_step_summary(summary):
    """Append only the validated public daily projection, never provider text."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    value = public_daily_summary(summary)
    if value is None:
        content = "## 每日新增结果\n\n未达标：日更摘要未通过校验，不能确认成功新增数量。\n\n旧文献修补不计入新增。\n\n"
    else:
        outcome = "已达标" if _daily_target_reached(value) else "未达标"
        http_used = value["requests"] + value["uncertainRequests"]
        ai_used = value["aiAttempts"] + value["uncertainAiAttempts"]
        content = ("## 每日新增结果\n\n"
            f"**今日成功新增：{value['dailyNewCount']} / {value['targetCount']} 篇 · {outcome}**\n\n"
            f"计数日期：{value['date']}（UTC；同日所有计划与手动重跑共享计数和预算）。\n\n"
            f"结束原因：{_DAILY_REASON_LABELS.get(value['endReason'], '受控停止，请检查运行状态')}。\n\n"
            f"当天累计 HTTP：{http_used} / {value['maxRequests']} 次"
            f"（已确认 {value['requests']} 次；中断后保守计入 {value['uncertainRequests']} 次）。\n\n"
            f"当天累计 AI HTTP：{ai_used} / {value['maxAiAttempts']} 次"
            f"（已确认 {value['aiAttempts']} 次；中断后保守计入 {value['uncertainAiAttempts']} 次）。\n\n"
            f"当天累计批次：{value['batchCount']} / {value['maxBatches']}。\n\n"
            "只统计真正准入的新文献；旧文献修补、失败候选和 AI 请求次数均不计入新增。"
            "预算耗尽不会重置后继续收费，未达标不会显示为达标。\n\n")
    try:
        with Path(path).open("a", encoding="utf-8") as handle:
            handle.write(content)
    except OSError:
        # Keep completed data/checkpoints even if GitHub's summary file is not
        # writable. Neither a file path nor arbitrary exception text is logged.
        print("::warning::无法写入 Actions 日更摘要；已保存的数据不受影响。", flush=True)


def git_checkpoint(root, report=None, summary=None):
    paths = ["data/catalog.json", "data/ledger.json", "data/updates.json"]
    if (root / "data" / "assets").is_dir():
        paths.append("data/assets")
    subprocess.run(["git", "add", "--", *paths], cwd=root, check=True)
    changed = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=root)
    if changed.returncode not in (0, 1):
        raise RuntimeError("无法检查公开数据检查点")
    if changed.returncode:
        subprocess.run(["git", "-c", "user.name=github-actions[bot]", "-c",
            "user.email=41898282+github-actions[bot]@users.noreply.github.com", "commit",
            "-m", "data: save plotting-library batch checkpoint [skip ci]"], cwd=root, check=True)
    # Also retries a commit made before an interrupted/failed push. No force.
    subprocess.run(["git", "push", "origin", "HEAD:main"], cwd=root, check=True)
    print("GitHub 批次检查点已保存", flush=True)


def collect(root, mode, checkpoint=git_checkpoint):
    effective_mode = effective_collection_mode(mode)
    settings = load_settings(root)
    callback = lambda report, summary: checkpoint(root, report, summary)
    before_batch = lambda summary: checkpoint(root, None, summary)
    # New discoveries are the cloud default. Old material gaps remain visible
    # to explicit maintenance, but must never silently redirect a daily run.
    # run_daily_session owns the durable UTC-day quota and cumulative budgets;
    # every scheduled top-up and manual rerun uses that same saved state.
    if effective_mode == "backfill":
        moment = stamp()
        ledger = validate_ledger(read_json(root / "data" / "ledger.json", {"days": {}, "candidates": {}}), moment[:10])
        previous = ledger.get("backfillSession", {})
        # A finished pass can be followed by another bounded source review on
        # a later invocation; eligibility still respects per-case cooldowns.
        # Paused sessions retain their cumulative reservations and allowances.
        ceiling_reasons = {"batch_limit_reached", "ai_budget_exhausted", "http_budget_exhausted", "time_limit_reached"}
        later_day = (isinstance(previous, dict) and isinstance(previous.get("startedAt"), str)
                     and previous["startedAt"][:10] < moment[:10])
        exhausted_previous_day = (later_day and previous.get("status") == "paused"
                                  and previous.get("endReason") in ceiling_reasons)
        if exhausted_previous_day and previous.get("inFlight"):
            # Recover the already uploaded reservation without making another
            # paid request before opening the next day's bounded pass.
            run_backfill_session(root, replace(settings, session_max_ai_attempts=0, session_max_requests=1),
                batch_callback=callback, batch_start_callback=before_batch)
            checkpoint(root)
        new_session = isinstance(previous, dict) and (previous.get("status") == "finished" or exhausted_previous_day)
        summary = run_backfill_session(root, settings, batch_callback=callback,
                                       batch_start_callback=before_batch, new_session=new_session)
    else:
        summary = run_daily_session(root, settings, batch_callback=callback, batch_start_callback=before_batch)
    checkpoint(root, None, summary)  # Persist final pause/empty-pass state too.
    if effective_mode == "daily":
        safe_summary = public_daily_summary(summary)
        write_daily_step_summary(safe_summary)
        if not _daily_target_reached(safe_summary):
            print("::warning::今日成功新增尚未达标；请查看 Actions 摘要中的新增数量、结束原因和当天累计预算。已保存批次不会撤销。", flush=True)
        print(json.dumps(safe_summary if safe_summary is not None else {"status": "unverified"}, ensure_ascii=False, indent=2), flush=True)
    else:
        if summary.get("endReason") not in {"cohort_traversed", "target_reached"}:
            print("::warning::连续整理已安全停止；请查看摘要中的结束原因。未达到当日新增目标不会标为达标，旧材料缺口不会标为完成。", flush=True)
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return summary


def main():
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("此入口仅用于 GitHub Actions；本地请使用 backfill-session，避免意外上传")
    root = Path(__file__).resolve().parents[1]
    collect(root, os.environ.get("UPDATE_MODE", "auto"))


if __name__ == "__main__":
    main()
