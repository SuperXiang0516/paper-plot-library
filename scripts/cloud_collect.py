"""Cloud-only orchestration with a Git checkpoint after every finite batch.

This script never reads or prints secret values. It pushes only the known public
data paths, never the private cache, and never uses force push.
"""
import json
import os
from pathlib import Path
import subprocess
from dataclasses import replace

from paper_plot_library.bootstrap import bootstrap_assessment, bootstrap_report
from paper_plot_library.cloud_update import run_daily_session
from paper_plot_library.config import load_settings
from paper_plot_library.pipeline import stamp, validate_catalog, validate_ledger
from paper_plot_library.session import run_backfill_session
from paper_plot_library.storage import read_json


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
    if mode not in {"auto", "backfill", "daily"}:
        raise ValueError("未知采集模式；未开始请求")
    settings = load_settings(root)
    moment = stamp()
    catalog = validate_catalog(read_json(root / "data" / "catalog.json"))
    ledger = validate_ledger(read_json(root / "data" / "ledger.json", {"days": {}, "candidates": {}}), moment[:10])
    bootstrap = bootstrap_report(catalog, ledger, root, moment)
    specs = catalog.get("inputSpecs", {})
    existing_pending = any(bootstrap_assessment(paper, specs.get(paper["id"]), root)["status"] != "complete"
                           for paper in catalog["papers"])
    callback = lambda report, summary: checkpoint(root, report, summary)
    before_batch = lambda summary: checkpoint(root, None, summary)
    if mode == "daily" and (bootstrap["status"] != "complete" or existing_pending):
        raise ValueError("现有案例还未处理完；daily 不得跳过补库")
    if mode == "backfill" or bootstrap["status"] != "complete" or existing_pending:
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
    if summary.get("endReason") not in {"cohort_traversed", "target_reached"}:
        print("::warning::连续整理已安全停止；请查看摘要中的结束原因，未达到目标的材料不会标为完成。", flush=True)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return summary


def main():
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("此入口仅用于 GitHub Actions；本地请使用 backfill-session，避免意外上传")
    root = Path(__file__).resolve().parents[1]
    collect(root, os.environ.get("UPDATE_MODE", "auto"))


if __name__ == "__main__":
    main()
