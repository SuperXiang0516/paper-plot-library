import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper_plot_library.__main__ import main
from paper_plot_library.config import Settings


class CliTests(unittest.TestCase):
    def test_bootstrap_batch_override_is_forwarded_separately_from_daily_repairs(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch("paper_plot_library.__main__.load_settings", return_value=Settings()) as load, \
                patch("paper_plot_library.__main__.run_pipeline", return_value={}), \
                contextlib.redirect_stdout(io.StringIO()):
            main(["--root", folder, "update", "--mode", "backfill", "--bootstrap-batch-limit", "1"])
            self.assertEqual(load.call_args.args[1], {"bootstrap_batch_limit": 1})

    def test_explicit_doi_repair_does_not_also_discover_new_papers(self):
        with tempfile.TemporaryDirectory() as folder, patch("paper_plot_library.__main__.load_settings", return_value=Settings()), patch("paper_plot_library.__main__.run_pipeline", return_value={}) as run, contextlib.redirect_stdout(io.StringIO()):
            main(["--root", folder, "update", "--doi", "10.1234/fixture"])
            self.assertEqual(run.call_args.kwargs["only_dois"], ["10.1234/fixture"])
            self.assertFalse(run.call_args.kwargs["discover_new"])
            self.assertEqual(run.call_args.kwargs["mode"], "auto")
            self.assertEqual(run.call_args.args[0], Path(folder).resolve())

    def test_invalid_explicit_doi_fails_before_any_update(self):
        with patch("paper_plot_library.__main__.run_pipeline") as run, contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            main(["update", "--doi", "not-a-doi"])
        self.assertEqual(caught.exception.code, 2)
        run.assert_not_called()

    def test_modes_are_forwarded_and_backfill_never_discovers_new_papers(self):
        for mode, discovery in (("auto", True), ("backfill", False), ("daily", True)):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as folder, \
                    patch("paper_plot_library.__main__.load_settings", return_value=Settings()) as load, \
                    patch("paper_plot_library.__main__.run_pipeline", return_value={}) as run, \
                    contextlib.redirect_stdout(io.StringIO()):
                main(["--root", folder, "update", "--mode", mode])
                self.assertEqual(run.call_args.kwargs["mode"], mode)
                self.assertEqual(run.call_args.kwargs["discover_new"], discovery)
                self.assertEqual(load.call_args.args[1], {})

    def test_unknown_mode_fails_before_loading_settings_or_running_updates(self):
        with patch("paper_plot_library.__main__.load_settings") as load, \
                patch("paper_plot_library.__main__.run_pipeline") as run, \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            main(["update", "--mode", "unchecked-shell-text"])
        self.assertEqual(caught.exception.code, 2)
        load.assert_not_called()
        run.assert_not_called()

    def test_no_discovery_still_applies_in_daily_mode(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch("paper_plot_library.__main__.load_settings", return_value=Settings()), \
                patch("paper_plot_library.__main__.run_pipeline", return_value={}) as run, \
                contextlib.redirect_stdout(io.StringIO()):
            main(["--root", folder, "update", "--mode", "daily", "--no-discovery"])
            self.assertEqual(run.call_args.kwargs["mode"], "daily")
            self.assertFalse(run.call_args.kwargs["discover_new"])


if __name__ == "__main__":
    unittest.main()
