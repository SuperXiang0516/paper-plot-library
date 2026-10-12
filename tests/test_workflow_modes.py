"""Offline initialization mode and workflow execution boundary checks."""
import json
from pathlib import Path
import re
import unittest


PROJECT = Path(__file__).resolve().parents[1]


class WorkflowModeTests(unittest.TestCase):
    def setUp(self):
        self.workflow = (PROJECT / ".github" / "workflows" / "site.yml").read_text(encoding="utf-8")

    def test_project_uses_five_paper_batches_and_fifteen_bounded_ai_attempts(self):
        config = json.loads((PROJECT / "config.json").read_text(encoding="utf-8"))
        self.assertTrue(config["initialize_existing_first"])
        self.assertEqual(config["bootstrap_batch_limit"], 5)
        self.assertEqual(config["max_papers_per_run"], 5)
        self.assertEqual(config["max_ai_attempts_per_paper"], 3)
        self.assertEqual((config["repair_limit"], config["max_ai_calls"], config["max_requests"],
                          config["max_new_per_day"]), (5, 15, 150, 5))

    def test_dispatch_preserves_collect_switch_and_exposes_only_known_modes(self):
        inputs = self.workflow.split("    inputs:\n", 1)[1].split("  schedule:\n", 1)[0]
        collect, modes = inputs.split("      mode:\n", 1)
        self.assertIn("      collect:\n", collect)
        self.assertIn("        type: boolean\n", collect)
        self.assertIn("        default: true\n", collect)
        self.assertIn("        type: choice\n", modes)
        self.assertIn("        default: auto\n", modes)
        self.assertEqual(re.findall(r"^          - (\S+)$", modes, re.MULTILINE), ["auto", "backfill", "daily"])

    def test_mode_is_passed_as_quoted_environment_not_inline_shell_expression(self):
        self.assertIn("UPDATE_MODE: ${{ github.event_name == 'workflow_dispatch' && inputs.mode || 'auto' }}", self.workflow)
        command = next(line.strip() for line in self.workflow.splitlines() if "run: python -m paper_plot_library update" in line)
        self.assertEqual(command, 'run: python -m paper_plot_library update --mode "$UPDATE_MODE"')
        self.assertNotIn("${{", command)

    def test_push_builds_without_collection_and_keeps_one_daily_schedule(self):
        condition = "if: github.event_name == 'schedule' || (github.event_name == 'workflow_dispatch' && inputs.collect)"
        self.assertEqual(self.workflow.count(condition), 2)
        self.assertIn("  push:\n    branches: [main]\n", self.workflow)
        self.assertEqual(re.findall(r"^    - cron: (.+)$", self.workflow, re.MULTILINE), ["'47 1 * * *'"])


if __name__ == "__main__":
    unittest.main()
