"""Cloud entrypoint routing/save checks only: no Git push or live APIs."""
import json
import io
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper_plot_library.config import Settings
from paper_plot_library.storage import write_json
from scripts.cloud_collect import collect, git_checkpoint, main, write_daily_step_summary


def daily_summary(reached=False, reason="no_progress"):
    return {"date": "2026-10-15", "status": "finished" if reached else "paused",
        "endReason": "target_reached" if reached else reason,
        "dailyNewCount": 5 if reached else 2, "targetCount": 5,
        "batchCount": 3, "maxBatches": 12, "requests": 40, "maxRequests": 1800,
        "aiAttempts": 7, "maxAiAttempts": 165, "uncertainRequests": 150,
        "uncertainAiAttempts": 15, "inFlightCount": 0, "localCheckpointSaved": True}


class CloudCollectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ppl-cloud-offline-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        summary_env = patch.dict("os.environ", {"GITHUB_STEP_SUMMARY": ""})
        summary_env.start()
        self.addCleanup(summary_env.stop)
        (self.root / "data").mkdir()
        write_json(self.root / "data" / "catalog.json", {"schemaVersion": 4,
            "authorCodeExecuted": False, "papers": [], "inputSpecs": {}})
        write_json(self.root / "data" / "ledger.json", {"days": {}, "candidates": {}})
        write_json(self.root / "data" / "updates.json", [])

    def test_pending_existing_pool_never_redirects_auto_to_backfill(self):
        write_json(self.root / "data" / "ledger.json", {"days": {}, "candidates": {},
            "bootstrap": {"status": "needs_attention"},
            "backfillSession": {"status": "finished", "endReason": "no_eligible_cases"}})
        with patch("scripts.cloud_collect.load_settings", return_value=Settings()), \
                patch("scripts.cloud_collect.run_backfill_session") as backfill, \
                patch("scripts.cloud_collect.run_daily_session", return_value={"endReason": "target_reached"}) as daily:
            saves = []
            collect(self.root, "auto", lambda *args: saves.append(args))
            daily.assert_called_once()
            backfill.assert_not_called()
            self.assertEqual(len(saves), 1)  # final checkpoint, even if no work

    def test_explicit_daily_ignores_unresolved_existing_materials(self):
        write_json(self.root / "data" / "catalog.json", {"schemaVersion": 4,
            "authorCodeExecuted": False, "papers": [{"id": "unresolved-case", "doi": "10.1234/unresolved", "runPerformed": False}],
            "inputSpecs": {}})
        with patch("scripts.cloud_collect.load_settings", return_value=Settings()), \
                patch("scripts.cloud_collect.run_backfill_session") as backfill, \
                patch("scripts.cloud_collect.run_daily_session", return_value={"endReason": "target_reached"}) as daily:
            collect(self.root, "daily", lambda *args: None)
        daily.assert_called_once()
        backfill.assert_not_called()

    def test_completed_pool_routes_to_five_success_daily_session(self):
        with patch("scripts.cloud_collect.load_settings", return_value=Settings()), \
                patch("scripts.cloud_collect.run_backfill_session") as backfill, \
                patch("scripts.cloud_collect.run_daily_session", return_value={"endReason": "target_reached"}) as daily:
            collect(self.root, "auto", lambda *args: None)
            daily.assert_called_once()
            backfill.assert_not_called()

    def test_later_unfinished_case_outside_frozen_cohort_does_not_block_auto(self):
        write_json(self.root / "data" / "catalog.json", {"schemaVersion": 4,
            "authorCodeExecuted": False, "papers": [{"id": "later-case", "doi": "10.1234/later-case", "runPerformed": False}], "inputSpecs": {}})
        with patch("scripts.cloud_collect.load_settings", return_value=Settings()), \
                patch("scripts.cloud_collect.run_backfill_session") as backfill, \
                patch("scripts.cloud_collect.run_daily_session", return_value={"endReason": "target_reached"}) as daily:
            collect(self.root, "auto", lambda *args: None)
            daily.assert_called_once()
            backfill.assert_not_called()

    def test_backfill_is_only_started_by_explicit_maintenance_mode(self):
        with patch("scripts.cloud_collect.load_settings", return_value=Settings()), \
                patch("scripts.cloud_collect.run_backfill_session", return_value={"endReason": "cohort_traversed"}) as backfill, \
                patch("scripts.cloud_collect.run_daily_session") as daily:
            collect(self.root, "backfill", lambda *args: None)
        backfill.assert_called_once()
        daily.assert_not_called()

    def test_previous_day_exhausted_pass_can_continue_with_a_new_bounded_pass(self):
        write_json(self.root / "data" / "ledger.json", {"days": {}, "candidates": {},
            "backfillSession": {"status": "paused", "endReason": "time_limit_reached", "startedAt": "2026-10-10T12:00:00Z"}})
        with patch("scripts.cloud_collect.stamp", return_value="2026-10-11T12:00:00Z"), \
                patch("scripts.cloud_collect.load_settings", return_value=Settings()), \
                patch("scripts.cloud_collect.run_backfill_session", return_value={"endReason": "cohort_traversed"}) as backfill:
            collect(self.root, "backfill", lambda *args: None)
        self.assertTrue(backfill.call_args.kwargs["new_session"])

    def test_same_day_exhausted_pass_does_not_reset_paid_allowances(self):
        write_json(self.root / "data" / "ledger.json", {"days": {}, "candidates": {},
            "backfillSession": {"status": "paused", "endReason": "ai_budget_exhausted", "startedAt": "2026-10-11T01:00:00Z"}})
        with patch("scripts.cloud_collect.stamp", return_value="2026-10-11T12:00:00Z"), \
                patch("scripts.cloud_collect.load_settings", return_value=Settings()), \
                patch("scripts.cloud_collect.run_backfill_session", return_value={"endReason": "ai_budget_exhausted"}) as backfill:
            collect(self.root, "backfill", lambda *args: None)
        self.assertFalse(backfill.call_args.kwargs["new_session"])

    def test_repeated_auto_and_daily_defer_same_day_state_to_daily_session(self):
        saved = {"date": "2026-10-11", "status": "finished", "endReason": "target_reached", "dailyNewCount": 5,
            "requests": 120, "aiAttempts": 7}
        ledger = {"days": {}, "candidates": {}, "dailyCollectionSession": saved}
        write_json(self.root / "data" / "ledger.json", ledger)
        with patch("scripts.cloud_collect.load_settings", return_value=Settings()), \
                patch("scripts.cloud_collect.run_backfill_session") as backfill, \
                patch("scripts.cloud_collect.run_daily_session", return_value=saved) as daily:
            for mode in ("auto", "daily", "daily"):
                collect(self.root, mode, lambda *args: None)
        self.assertEqual(daily.call_count, 3)
        self.assertTrue(all("new_session" not in call.kwargs for call in daily.call_args_list))
        backfill.assert_not_called()
        # Routing must not reset or replace durable quota/paid allowance data.
        self.assertEqual(json.loads((self.root / "data" / "ledger.json").read_text()), ledger)

    def test_unknown_mode_rejected_before_loading_settings_or_collection(self):
        for mode in ("reset-daily-budget", [], {}, None, 1):
            with self.subTest(mode=mode), patch("scripts.cloud_collect.load_settings") as settings, \
                    patch("scripts.cloud_collect.run_daily_session") as daily, self.assertRaises(ValueError):
                collect(self.root, mode, lambda *args: None)
            settings.assert_not_called()
            daily.assert_not_called()

    def test_summary_shows_true_success_and_confirmed_plus_uncertain_budgets(self):
        path = self.root / "actions-summary.md"
        with patch.dict("os.environ", {"GITHUB_STEP_SUMMARY": str(path)}):
            write_daily_step_summary(daily_summary(reached=True))
        content = path.read_text(encoding="utf-8")
        self.assertIn("今日成功新增：5 / 5 篇 · 已达标", content)
        self.assertIn("当天累计 HTTP：190 / 1800 次", content)
        self.assertIn("当天累计 AI HTTP：22 / 165 次", content)
        self.assertIn("中断后保守计入 150 次", content)
        self.assertIn("旧文献修补、失败候选", content)

    def test_summary_marks_shortfall_with_controlled_discovery_failure_reason(self):
        path = self.root / "actions-summary.md"
        with patch.dict("os.environ", {"GITHUB_STEP_SUMMARY": str(path)}):
            write_daily_step_summary(daily_summary(reason="discovery_failed"))
        content = path.read_text(encoding="utf-8")
        self.assertIn("今日成功新增：2 / 5 篇 · 未达标", content)
        self.assertIn("文献发现服务暂时失败", content)

    def test_summary_only_writes_public_projection_and_rejects_arbitrary_reason(self):
        path = self.root / "actions-summary.md"
        value = dict(daily_summary(), providerResponse="DO_NOT_LEAK", error="SECRET_RESPONSE")
        with patch.dict("os.environ", {"GITHUB_STEP_SUMMARY": str(path)}):
            write_daily_step_summary(value)
            write_daily_step_summary(dict(value, endReason="DO_NOT_LEAK\n::error::SECRET_RESPONSE"))
        content = path.read_text(encoding="utf-8")
        self.assertIn("日更摘要未通过校验", content)
        self.assertNotIn("DO_NOT_LEAK", content)
        self.assertNotIn("SECRET_RESPONSE", content)
        self.assertNotIn("::error::", content)

    def test_summary_never_interpolates_unvalidated_date_or_numeric_field(self):
        path = self.root / "actions-summary.md"
        with patch.dict("os.environ", {"GITHUB_STEP_SUMMARY": str(path)}):
            for changed in ({"date": "2026-10-15\nDO_NOT_LEAK"}, {"dailyNewCount": "DO_NOT_LEAK"},
                            {"requests": True}, {"maxRequests": 99999999}):
                write_daily_step_summary(dict(daily_summary(), **changed))
        content = path.read_text(encoding="utf-8")
        self.assertEqual(content.count("日更摘要未通过校验"), 4)
        self.assertNotIn("DO_NOT_LEAK", content)

    def test_summary_missing_environment_does_not_open_any_file(self):
        with patch("scripts.cloud_collect.Path.open") as opening:
            write_daily_step_summary(daily_summary())
        opening.assert_not_called()

    def test_summary_write_failure_logs_only_fixed_warning(self):
        output = io.StringIO()
        with patch.dict("os.environ", {"GITHUB_STEP_SUMMARY": str(self.root / "summary.md")}), \
                patch("scripts.cloud_collect.Path.open", side_effect=OSError("DO_NOT_LEAK")), redirect_stdout(output):
            write_daily_step_summary(daily_summary())
        self.assertIn("::warning::无法写入 Actions 日更摘要", output.getvalue())
        self.assertNotIn("DO_NOT_LEAK", output.getvalue())

    def test_daily_collect_writes_shortfall_summary_and_warning_after_checkpoint(self):
        path = self.root / "actions-summary.md"
        summary = daily_summary()
        output = io.StringIO()
        saves = []
        with patch.dict("os.environ", {"GITHUB_STEP_SUMMARY": str(path)}), \
                patch("scripts.cloud_collect.load_settings", return_value=Settings()), \
                patch("scripts.cloud_collect.run_daily_session", return_value=summary), redirect_stdout(output):
            returned = collect(self.root, "auto", lambda *args: saves.append(args))
        self.assertEqual(returned, summary)
        self.assertEqual(len(saves), 1)
        self.assertIn("今日成功新增尚未达标", output.getvalue())
        self.assertIn("今日成功新增：2 / 5 篇 · 未达标", path.read_text(encoding="utf-8"))

    def test_daily_collect_reached_target_has_no_shortfall_warning(self):
        output = io.StringIO()
        with patch("scripts.cloud_collect.load_settings", return_value=Settings()), \
                patch("scripts.cloud_collect.run_daily_session", return_value=daily_summary(reached=True)), redirect_stdout(output):
            collect(self.root, "daily", lambda *args: None)
        self.assertNotIn("::warning::", output.getvalue())

    def test_git_checkpoint_only_stages_allowed_paths_and_pushes_without_force(self):
        (self.root / "data" / "assets").mkdir()
        with patch("scripts.cloud_collect.subprocess.run") as run:
            run.return_value.returncode = 1
            git_checkpoint(self.root)
            commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[0], ["git", "add", "--", "data/catalog.json", "data/ledger.json", "data/updates.json", "data/assets"])
        self.assertEqual(commands[-1], ["git", "push", "origin", "HEAD:main"])
        self.assertTrue(any("[skip ci]" in part for command in commands for part in command))
        self.assertNotIn("--force", [part for command in commands for part in command])
        self.assertFalse(any(".ppl" in part or ".env" in part for command in commands for part in command))

    def test_retry_push_even_if_previous_commit_already_made_and_no_new_diff(self):
        with patch("scripts.cloud_collect.subprocess.run") as run:
            run.return_value.returncode = 0
            git_checkpoint(self.root)
        self.assertEqual(run.call_args.args[0], ["git", "push", "origin", "HEAD:main"])

    def test_local_entry_cannot_upload_or_start_collection_accidentally(self):
        with patch.dict("os.environ", {"GITHUB_ACTIONS": "false"}), \
                patch("scripts.cloud_collect.collect") as collection, self.assertRaises(RuntimeError):
            main()
        collection.assert_not_called()


if __name__ == "__main__":
    unittest.main()
