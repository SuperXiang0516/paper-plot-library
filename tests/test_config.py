"""Explicit configuration isolation and bounded local settings."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper_plot_library.config import load_settings


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def write_config(self, payload):
        (self.root / "config.json").write_text(json.dumps(payload))

    def test_defaults_and_zero_caps(self):
        result = load_settings(self.root)
        self.assertEqual(result.max_new_per_day, 5)
        self.assertEqual(result.llm_model, "")
        self.assertEqual(result.llm_api_key, "")
        self.assertFalse(result.initialize_existing_first)
        self.assertEqual(result.bootstrap_batch_limit, 20)
        self.write_config({"max_new_per_day": 0, "max_ai_calls": 0, "repair_limit": 0, "candidate_checks": 0,
                           "max_images": 0, "max_supplements": 0})
        result = load_settings(self.root)
        self.assertEqual((result.max_new_per_day, result.max_ai_calls, result.max_images), (0, 0, 0))

    def test_secret_fields_forbidden_in_public_config(self):
        for field in ("llm_api_key", "easyscholar_key", "ncbi_api_key", "github_token", "openalex_key"):
            self.write_config({field: "secret"})
            with self.subTest(field=field), self.assertRaises(ValueError):
                load_settings(self.root)

    def test_unknown_fields_and_wrong_types_rejected(self):
        for value in ({"typo": 1}, {"max_new_per_day": 6}, {"max_new_per_day": True}, {"max_ai_calls": -1},
                      {"include_supplements": "true"}, {"max_requests": 0}, {"discovery_limit": 101}, {"max_file_bytes": 10000001}):
            self.write_config(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                load_settings(self.root)

    def test_initialization_settings_are_explicit_and_bounded(self):
        self.write_config({"initialize_existing_first": True, "bootstrap_batch_limit": 50})
        result = load_settings(self.root)
        self.assertTrue(result.initialize_existing_first)
        self.assertEqual(result.bootstrap_batch_limit, 50)
        self.assertEqual((result.repair_limit, result.max_ai_calls, result.max_requests), (5, 5, 150))
        for value in ({"initialize_existing_first": "true"}, {"initialize_existing_first": 1},
                      {"bootstrap_batch_limit": True}, {"bootstrap_batch_limit": 0},
                      {"bootstrap_batch_limit": 51}, {"bootstrap_batch_limit": "20"}):
            self.write_config(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                load_settings(self.root)

    def test_only_current_project_env_is_loaded(self):
        (Path(self.temp.name) / ".env").write_text("LLM_API_KEY=unrelated-secret\n")
        (self.root / "config.env").write_text("LLM_API_KEY=aging-secret\n")
        self.assertEqual(load_settings(self.root).llm_api_key, "")
        (self.root / ".env").write_text('LLM_API_KEY="project-secret"\nLLM_MODEL=specified-model\nEASYSCHOLAR_SECRET_KEY=test\n')
        result = load_settings(self.root)
        self.assertEqual(result.llm_api_key, "project-secret")
        self.assertEqual(result.llm_model, "specified-model")

    def test_real_environment_overrides_local_file_without_mutating_environment(self):
        (self.root / ".env").write_text("LLM_API_KEY=local-secret\nLLM_MODEL=local-model\n")
        os.environ["LLM_API_KEY"] = "environment-secret"
        result = load_settings(self.root)
        self.assertEqual(result.llm_api_key, "environment-secret")
        self.assertNotIn("LLM_MODEL", os.environ)

    def test_blank_environment_suppresses_local_secret(self):
        (self.root / ".env").write_text("LLM_API_KEY=local-secret\n")
        os.environ["LLM_API_KEY"] = ""
        self.assertEqual(load_settings(self.root).llm_api_key, "")

    def test_discovery_query_requires_bounded_text(self):
        for value in (None, 123, [], "", "x" * 20001):
            self.write_config({"discovery_query": value})
            with self.subTest(value=type(value).__name__), self.assertRaises(ValueError):
                load_settings(self.root)

    def test_setting_repr_does_not_dump_secrets(self):
        os.environ.update({"LLM_API_KEY": "DO_NOT_LEAK_AI", "EASYSCHOLAR_SECRET_KEY": "DO_NOT_LEAK_ES",
                           "NCBI_API_KEY": "DO_NOT_LEAK_NCBI", "GH_TOKEN": "DO_NOT_LEAK_GH", "OPENALEX_API_KEY": "DO_NOT_LEAK_OA"})
        self.assertNotIn("DO_NOT_LEAK", repr(load_settings(self.root)))


if __name__ == "__main__":
    unittest.main()
