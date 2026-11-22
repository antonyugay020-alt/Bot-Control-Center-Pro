"""Review-only patch guard regression; Git commands are non-mutating."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools.bcc_patch_gate import (
    PINNED_SHA, PINNED_PROMPT_SHA256, JobBlocked, inspect_patch,
)


class PatchGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "bcc").mkdir()
        (self.root / "bcc/scheduler.py").write_text("first\nold\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.patch_source = ("diff --git a/bcc/scheduler.py b/bcc/scheduler.py\n"
                             "--- a/bcc/scheduler.py\n"
                             "+++ b/bcc/scheduler.py\n"
                             "@@ -1,2 +1,2 @@\n"
                             " first\n-old\n+new\n")
        self.gitcheck = patch("tools.bcc_patch_gate.check_snapshot", return_value=self.root)
        self.gitcheck.start()
        self.addCleanup(self.gitcheck.stop)

    def validate(self, text=None, sha=PINNED_SHA, prompt=PINNED_PROMPT_SHA256):
        return inspect_patch(self.root, sha, prompt,
                             (self.patch_source if text is None else text).encode("utf-8"))

    def test_real_git_check_accepts_but_does_not_apply(self):
        before = (self.root / "bcc/scheduler.py").read_text()
        report = self.validate()
        self.assertEqual(report["status"], "PATCH_PROPOSAL_VALIDATED")
        self.assertFalse(report["patch_applied"])
        self.assertFalse(report["tests_run"])
        self.assertEqual((self.root / "bcc/scheduler.py").read_text(), before)

    def test_wrong_source_sha_fails(self):
        with self.assertRaises(JobBlocked):
            self.validate(sha="b" * 40)

    def test_wrong_prompt_sha_fails(self):
        with self.assertRaises(JobBlocked):
            self.validate(prompt="b" * 64)

    def test_unapproved_margin_change_fails(self):
        with self.assertRaises(JobBlocked):
            self.validate(self.patch_source.replace("bcc/scheduler.py", "bcc/margin.py"))

    def test_wrong_current_content_fails_git_check(self):
        with self.assertRaises(JobBlocked):
            self.validate(self.patch_source.replace("-old", "-does_not_exist"))

    def test_creation_and_deletion_blocked(self):
        with self.assertRaises(JobBlocked):
            self.validate(self.patch_source.replace("--- a/bcc/scheduler.py",
                         "new file mode 100644\n--- a/bcc/scheduler.py"))

    def test_binary_patches_blocked(self):
        with self.assertRaises(JobBlocked):
            self.validate(self.patch_source + "GIT binary patch\n")

    def test_secret_blocked(self):
        with self.assertRaises(JobBlocked):
            self.validate(self.patch_source.replace("+new", "+sk-123456789012345678901234"))

    def test_duplicate_file_header_blocked(self):
        with self.assertRaises(JobBlocked):
            self.validate(self.patch_source + self.patch_source)

    def test_empty_patch_blocked(self):
        with self.assertRaises(JobBlocked):
            inspect_patch(self.root, PINNED_SHA, PINNED_PROMPT_SHA256, b"")


if __name__ == "__main__":
    unittest.main()
