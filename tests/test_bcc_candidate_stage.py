"""Local-only staging tests with real Git worktree and no model calls."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools.bcc_candidate_stage import BRANCH_PREFIX, stage_proposal
from tools.bcc_openai_job import JobBlocked

PROMPT = "a" * 64


class CandidateStageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "source"
        self.root.mkdir()
        (self.root / "bcc").mkdir()
        (self.root / "bcc/scheduler.py").write_text("first\nold\n", encoding="utf-8")
        self.git("init", "-q")
        self.git("config", "user.name", "QA")
        self.git("config", "user.email", "qa@example.invalid")
        self.git("add", "bcc/scheduler.py")
        self.git("commit", "-q", "-m", "seed")
        self.git("remote", "add", "origin",
                 "https://github.com/antonyugay020-alt/Bot-Control-Center-Pro.git")
        self.sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("checkout", "-q", "--detach", self.sha)
        # Tests simulate a pinned policy on a fresh disposable Git repository.
        # No production SHA or policy file is changed.
        self.source_pin = patch("tools.bcc_patch_gate.PINNED_SHA", self.sha)
        self.source_pin.start()
        self.addCleanup(self.source_pin.stop)
        self.prompt_pin = patch("tools.bcc_patch_gate.PINNED_PROMPT_SHA256", PROMPT)
        self.prompt_pin.start()
        self.addCleanup(self.prompt_pin.stop)
        self.candidate = (
            "diff --git a/bcc/scheduler.py b/bcc/scheduler.py\n"
            "--- a/bcc/scheduler.py\n"
            "+++ b/bcc/scheduler.py\n"
            "@@ -1,2 +1,2 @@\n"
            " first\n-old\n+new\n"
        ).encode("utf-8")

    def git(self, *args, check=True):
        return subprocess.run(["git", "-C", str(self.root), *args],
                              capture_output=True, text=True, check=check)

    def stage(self, candidate=None, *, sha=None, prompt_sha=None):
        return stage_proposal(self.root, sha or self.sha,
                              prompt_sha or PROMPT,
                              self.candidate if candidate is None else candidate)

    def cleanup_stage(self, report):
        self.git("worktree", "remove", "--force", report["candidate_worktree"])
        self.git("branch", "-D", report["candidate_branch"])

    def test_real_local_branch_stage_preserves_original(self):
        original = (self.root / "bcc/scheduler.py").read_text()
        report = self.stage()
        worktree = Path(report["candidate_worktree"])
        self.assertEqual((worktree / "bcc/scheduler.py").read_text(), "first\nnew\n")
        self.assertEqual((self.root / "bcc/scheduler.py").read_text(), original)
        self.assertTrue(report["candidate_branch"].startswith(BRANCH_PREFIX))
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), self.sha)
        self.assertFalse(report["tests_run"])
        self.assertFalse(report["executable_code_run"])
        self.assertFalse(report["git_commit_created"])
        self.assertEqual(report["github_writes"], "DISABLED")
        self.assertEqual(report["status"], "LOCAL_CANDIDATE_STAGED_FOR_REVIEW")
        self.cleanup_stage(report)

    def test_existing_candidate_branch_is_never_overwritten(self):
        first = self.stage()
        with self.assertRaises(JobBlocked):
            self.stage()
        self.cleanup_stage(first)

    def test_wrong_source_sha_refused(self):
        with self.assertRaises(JobBlocked):
            self.stage(sha="1" * 40)

    def test_wrong_prompt_hash_refused(self):
        with self.assertRaises(JobBlocked):
            self.stage(prompt_sha="2" * 64)

    def test_patch_with_wrong_hunk_is_refused(self):
        with self.assertRaises(JobBlocked):
            self.stage(self.candidate.replace(b"-old", b"-wrong"))

    def test_dirty_baseline_refused(self):
        (self.root / "bcc/scheduler.py").write_text("changed\n", encoding="utf-8")
        with self.assertRaises(JobBlocked):
            self.stage()

    def test_configured_git_filter_refused_before_checkout(self):
        self.git("config", "filter.example.process", "untrusted-command")
        with self.assertRaisesRegex(JobBlocked, "filters"):
            self.stage()

    def test_failure_rolls_back_new_branch_without_touching_source(self):
        original = (self.root / "bcc/scheduler.py").read_text()
        with patch("tools.bcc_candidate_stage._review_stage",
                   side_effect=JobBlocked("synthetic")):
            with self.assertRaises(JobBlocked):
                self.stage()
        self.assertEqual(
            self.git("branch", "--list", BRANCH_PREFIX + "*").stdout.strip(), "")
        self.assertEqual((self.root / "bcc/scheduler.py").read_text(), original)

    def test_secret_in_proposed_diff_is_refused(self):
        with self.assertRaises(JobBlocked):
            self.stage(self.candidate.replace(
                b"+new", b"+sk-123456789012345678901234"))

    def test_source_with_wrong_remote_is_refused(self):
        self.git("remote", "set-url", "origin", "https://github.com/attacker/fork.git")
        with self.assertRaises(JobBlocked):
            self.stage()


if __name__ == "__main__":
    unittest.main()
