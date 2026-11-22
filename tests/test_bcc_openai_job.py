"""No-network tests for pinned source review bundle generation."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.bcc_openai_job import (JobBlocked, SCOPE, TOKEN_PATTERN, _excerpt,
                                  check_snapshot, prepare, write_bundle)
from tools.bcc_cloud_control import REPOSITORY


class PinnedJobTests(unittest.TestCase):
    SHA = "a" * 40

    def create_tree(self, root):
        for path, ranges in SCOPE.items():
            output = root / path
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text("x = 1\n" * max(b for a, b in ranges), encoding="utf-8")

    def test_safe_pinned_bundle_and_no_paid_path(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.create_tree(root)
            with patch("tools.bcc_openai_job.check_snapshot", return_value=root):
                manifest, prompt = prepare(root, self.SHA)
            self.assertEqual(manifest["status"], "OFFLINE_REVIEW_BUNDLE")
            self.assertEqual(manifest["network_calls"], "DISABLED")
            self.assertEqual(manifest["approval"], "NOT_GRANTED")
            self.assertLess(len(prompt), 90_000)
            self.assertEqual(len(manifest["sources"]), len(SCOPE))
            self.assertIn(b"process_tick", prompt)

    def test_wrong_commit_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with patch("tools.bcc_openai_job._git", side_effect=[
                    str(root), "https://github.com/"+REPOSITORY+".git", "b"*40]):
                with self.assertRaises(JobBlocked):
                    check_snapshot(root, self.SHA)

    def test_source_branch_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with patch("tools.bcc_openai_job._git", side_effect=[
                    str(root), "https://github.com/"+REPOSITORY+".git", self.SHA, "main"]):
                with self.assertRaises(JobBlocked):
                    check_snapshot(root, self.SHA)

    def test_modified_source_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with patch("tools.bcc_openai_job._git", side_effect=[
                    str(root), "https://github.com/"+REPOSITORY+".git", self.SHA, "", " M bcc/runtime.py"]):
                with self.assertRaises(JobBlocked):
                    check_snapshot(root, self.SHA)

    def test_token_disclosure_blocked(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.create_tree(root)
            target = root / "bcc/runtime.py"
            text = target.read_text(encoding="utf-8")
            target.write_text(text.replace("x = 1\n" * 100, "x = 1\n" * 80 + "x = 'sk-AAAAAAAAAAAAAAAAAAAAAA'\n" + "x = 1\n" * 19, 1), encoding="utf-8")
            with self.assertRaises(JobBlocked):
                _excerpt(root, "bcc/runtime.py")

    def test_unapproved_path_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(JobBlocked):
                _excerpt(Path(d), "MY_BOTS/secret.db")

    def test_symlink_source_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.create_tree(root)
            target = root / "bcc/market.py"
            target.unlink()
            target.symlink_to(root / "bcc/runtime.py")
            with self.assertRaises(JobBlocked):
                _excerpt(root, "bcc/market.py")

    def test_exclusive_output(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "review.txt"
            write_bundle(target, b"safe")
            self.assertEqual(target.read_bytes(), b"safe")
            with self.assertRaises(JobBlocked):
                write_bundle(target, b"overwrite")
            self.assertEqual(target.read_bytes(), b"safe")

    def test_secret_pattern(self):
        self.assertIsNotNone(TOKEN_PATTERN.search("sk-12345678901234567890"))
        self.assertIsNone(TOKEN_PATTERN.search("dummy-key-placeholder"))


if __name__ == "__main__":
    unittest.main()
