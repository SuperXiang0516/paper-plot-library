import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper_plot_library.__main__ import main
from paper_plot_library.config import Settings


class CliTests(unittest.TestCase):
    def test_explicit_doi_repair_does_not_also_discover_new_papers(self):
        with tempfile.TemporaryDirectory() as folder, patch("paper_plot_library.__main__.load_settings", return_value=Settings()), patch("paper_plot_library.__main__.run_pipeline", return_value={}) as run, contextlib.redirect_stdout(io.StringIO()):
            main(["--root", folder, "update", "--doi", "10.1234/fixture"])
            self.assertEqual(run.call_args.kwargs["only_dois"], ["10.1234/fixture"])
            self.assertFalse(run.call_args.kwargs["discover_new"])
            self.assertEqual(run.call_args.args[0], Path(folder).resolve())

    def test_invalid_explicit_doi_fails_before_any_update(self):
        with patch("paper_plot_library.__main__.run_pipeline") as run, contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            main(["update", "--doi", "not-a-doi"])
        self.assertEqual(caught.exception.code, 2)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
