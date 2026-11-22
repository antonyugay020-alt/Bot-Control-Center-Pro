"""Offline security tests for Codespaces controller (stdlib only)."""
from pathlib import Path
import unittest
from unittest.mock import patch

from tools.bcc_cloud_control import (APPROVED_PATHS, BRANCH, REPOSITORY,
                                     Blocked, inspect, run_graph,
                                     task_manifest, validate_origin)


class CloudGuardTests(unittest.TestCase):
    SHA = "1" * 40

    def test_valid_pinned_scope_cannot_spend_or_merge(self):
        result = task_manifest(self.SHA)
        self.assertEqual(result["target_sha"], self.SHA)
        for key in ("github_write", "kie_api_calls", "openai_api_calls", "live_trading", "merge"):
            self.assertEqual(result[key], "DISABLED")

    def test_unknown_or_duplicate_scope_rejected(self):
        for paths in (["bcc/trading.py"],
                      [APPROVED_PATHS[0], APPROVED_PATHS[0]],
                      ["bcc/../secrets.py"], [], [".github/workflows/bcc-ci.yml"]):
            with self.subTest(paths=paths), self.assertRaises(Blocked):
                task_manifest(self.SHA, paths)

    def test_invalid_shas_rejected(self):
        for sha in (None, "x" * 40, "1" * 39, "1" * 41, "a" * 39 + "\n"):
            with self.subTest(sha=sha), self.assertRaises(Blocked):
                task_manifest(sha)

    def test_only_exact_origin_allowed(self):
        self.assertTrue(validate_origin("https://github.com/" + REPOSITORY + ".git"))
        for remote in ("https://github.com/attacker/repo.git",
                       "https://token@github.com/" + REPOSITORY,
                       "file:///tmp/repo",
                       "https://github.com/" + REPOSITORY + "/evil"):
            self.assertFalse(validate_origin(remote))

    def test_inspect_wrong_branch_fails_closed(self):
        with patch("tools.bcc_cloud_control.git", side_effect=[
                str(Path.cwd()), "https://github.com/" + REPOSITORY + ".git", "main"]):
            with self.assertRaises(Blocked):
                inspect(Path.cwd())

    def test_inspect_modified_code_rejected(self):
        with patch("tools.bcc_cloud_control.git", side_effect=[
                str(Path.cwd()), "https://github.com/" + REPOSITORY + ".git",
                BRANCH, " M app.py"]):
            with self.assertRaises(Blocked):
                inspect(Path.cwd())

    def test_inspect_is_read_only(self):
        with patch("tools.bcc_cloud_control.git", side_effect=[
                str(Path.cwd()), "https://github.com/" + REPOSITORY + ".git",
                BRANCH, "", self.SHA]):
            result = inspect(Path.cwd())
        self.assertEqual(result["status"], "OFFLINE_CONTROL_READY")
        self.assertEqual(result["paid_operations"], "DISABLED")

    def test_graph_disables_provider_environment(self):
        fake = type("Result", (), {"returncode": 0})()
        with patch("tools.bcc_cloud_control.inspect"), patch(
                "tools.bcc_cloud_control.subprocess.run", return_value=fake) as run, patch.dict(
                "os.environ", {"OPENAI_API_KEY": "secret", "KIE_API_KEY": "secret",
                               "OLLAMA_BASE_URL": "http://x"}):
            report = run_graph(Path.cwd(), graphify="graphify")
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["graphify", "extract", ".", "--code-only"])
        self.assertEqual(kwargs["env"]["GRAPHIFY_BACKEND"], "none")
        for name in ("OPENAI_API_KEY", "KIE_API_KEY", "OLLAMA_BASE_URL"):
            self.assertNotIn(name, kwargs["env"])
        self.assertEqual(report["api_calls_authorized"], "false")

    def test_graph_fails_closed(self):
        fake = type("Result", (), {"returncode": 1})()
        with patch("tools.bcc_cloud_control.inspect"), patch(
                "tools.bcc_cloud_control.subprocess.run", return_value=fake):
            with self.assertRaises(Blocked):
                run_graph(Path.cwd(), graphify="graphify")

    def test_no_provider_calls_present(self):
        import tools.bcc_cloud_control as module
        source = Path(module.__file__).read_text(encoding="utf-8")
        for fragment in ("import openai", "import requests", "import bybit",
                         "requests.post(", "urllib.request.urlopen"):
            self.assertNotIn(fragment, source)


if __name__ == "__main__":
    unittest.main()
