"""Cloud entrypoint routing/save checks only: no Git push or live APIs."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper_plot_library.config import Settings
from paper_plot_library.storage import write_json
from scripts.cloud_collect import collect, git_checkpoint, main


class CloudCollectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ppl-cloud-offline-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "data").mkdir()
        write_json(self.root / "data" / "catalog.json", {"schemaVersion": 4,
            "authorCodeExecuted": False, "papers": [], "inputSpecs": {}})
        write_json(self.root / "data" / "ledger.json", {"days": {}, "candidates": {}})
        write_json(self.root / "data" / "updates.json", [])

    def test_pending_existing_pool_routes_to_continuous_backfill_not_daily(self):
        with patch("scripts.cloud_collect.load_settings", return_value=Settings()), \
                patch("scripts.cloud_collect.bootstrap_report", return_value={"status": "active"}), \
                patch("scripts.cloud_collect.run_backfill_session", return_value={"endReason": "cohort_traversed"}) as backfill, \
                patch("scripts.cloud_collect.run_daily_session") as daily:
            saves = []
            collect(self.root, "auto", lambda *args: saves.append(args))
            backfill.assert_called_once()
            daily.assert_not_called()
            self.assertEqual(len(saves), 1)  # final checkpoint, even if no work

    def test_daily_cannot_bypass_unresolved_bootstrap(self):
        with patch("scripts.cloud_collect.load_settings", return_value=Settings()), \
                patch("scripts.cloud_collect.bootstrap_report", return_value={"status": "active"}), \
                patch("scripts.cloud_collect.run_daily_session") as daily, \
                self.assertRaises(ValueError):
            collect(self.root, "daily", lambda *args: None)
        daily.assert_not_called()

    def test_completed_pool_routes_to_five_success_daily_session(self):
        with patch("scripts.cloud_collect.load_settings", return_value=Settings()), \
                patch("scripts.cloud_collect.bootstrap_report", return_value={"status": "complete"}), \
                patch("scripts.cloud_collect.run_backfill_session") as backfill, \
                patch("scripts.cloud_collect.run_daily_session", return_value={"endReason": "target_reached"}) as daily:
            collect(self.root, "auto", lambda *args: None)
            daily.assert_called_once()
            backfill.assert_not_called()

    def test_later_unfinished_case_outside_frozen_cohort_still_routes_to_backfill(self):
        write_json(self.root / "data" / "catalog.json", {"schemaVersion": 4,
            "authorCodeExecuted": False, "papers": [{"id": "later-case", "doi": "10.1234/later-case", "runPerformed": False}], "inputSpecs": {}})
        with patch("scripts.cloud_collect.load_settings", return_value=Settings()), \
                patch("scripts.cloud_collect.bootstrap_report", return_value={"status": "complete"}), \
                patch("scripts.cloud_collect.run_backfill_session", return_value={"endReason": "cohort_traversed"}) as backfill, \
                patch("scripts.cloud_collect.run_daily_session") as daily:
            collect(self.root, "auto", lambda *args: None)
            backfill.assert_called_once()
            daily.assert_not_called()

    def test_previous_day_exhausted_pass_can_continue_with_a_new_bounded_pass(self):
        write_json(self.root / "data" / "ledger.json", {"days": {}, "candidates": {},
            "backfillSession": {"status": "paused", "endReason": "time_limit_reached", "startedAt": "2026-10-10T12:00:00Z"}})
        with patch("scripts.cloud_collect.stamp", return_value="2026-10-11T12:00:00Z"), \
                patch("scripts.cloud_collect.load_settings", return_value=Settings()), \
                patch("scripts.cloud_collect.bootstrap_report", return_value={"status": "active"}), \
                patch("scripts.cloud_collect.run_backfill_session", return_value={"endReason": "cohort_traversed"}) as backfill:
            collect(self.root, "auto", lambda *args: None)
        self.assertTrue(backfill.call_args.kwargs["new_session"])

    def test_same_day_exhausted_pass_does_not_reset_paid_allowances(self):
        write_json(self.root / "data" / "ledger.json", {"days": {}, "candidates": {},
            "backfillSession": {"status": "paused", "endReason": "ai_budget_exhausted", "startedAt": "2026-10-11T01:00:00Z"}})
        with patch("scripts.cloud_collect.stamp", return_value="2026-10-11T12:00:00Z"), \
                patch("scripts.cloud_collect.load_settings", return_value=Settings()), \
                patch("scripts.cloud_collect.bootstrap_report", return_value={"status": "active"}), \
                patch("scripts.cloud_collect.run_backfill_session", return_value={"endReason": "ai_budget_exhausted"}) as backfill:
            collect(self.root, "auto", lambda *args: None)
        self.assertFalse(backfill.call_args.kwargs["new_session"])

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
