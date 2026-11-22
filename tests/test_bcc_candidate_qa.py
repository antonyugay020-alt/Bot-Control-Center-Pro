"""Offline Docker QA gate tests: no Docker daemon or paid APIs are invoked."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import bcc_candidate_qa as qa
from tools.bcc_openai_job import JobBlocked


class OfflineQATests(unittest.TestCase):
    IMAGE = "sha256:" + "a" * 64
    SHA = "1" * 40
    PROMPT = "2" * 64
    PATCH = b"safe diff fixture\n"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.candidate = self.base / "candidate"
        self.candidate.mkdir()

    def argv(self, image=None, name="bcc-qa-" + "1" * 16):
        return qa.docker_args(self.candidate, self.IMAGE if image is None else image,
                              name, uid=1000, gid=1000)

    def test_network_and_capability_prohibitions(self):
        cmd = self.argv()
        for item in ("--network=none", "--read-only", "--cap-drop=ALL",
                     "--security-opt=no-new-privileges", "--pids-limit=256",
                     "--pull=never", "--memory=3g", "--cpus=2"):
            self.assertIn(item, cmd)
        self.assertIn("type=bind,source=" + str(self.candidate.resolve())
                      + ",target=/src,readonly", cmd)
        self.assertIn("PYTEST_DISABLE_PLUGIN_AUTOLOAD=1", cmd)
        self.assertIn("QT_QPA_PLATFORM=offscreen", cmd)
        self.assertEqual(cmd[-6:], ["-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"])
        for key in ("OPENAI_API_KEY", "KIE_API_KEY", "GH_TOKEN", "GITHUB_TOKEN"):
            self.assertNotIn(key, " ".join(cmd))

    def test_invalid_image_refused(self):
        for value in ("python:3.12", "latest", self.IMAGE + " ",
                      "sha256:" + "z" * 64, ""):
            with self.subTest(value=value), self.assertRaises(JobBlocked):
                self.argv(image=value)

    def test_invalid_container_name_refused(self):
        with self.assertRaises(JobBlocked):
            self.argv(name="other-container")
        with self.assertRaises(JobBlocked):
            self.argv(name="bcc-qa-" + "a" * 100)

    def test_root_user_refused(self):
        with self.assertRaises(JobBlocked):
            qa.docker_args(self.candidate, self.IMAGE,
                           "bcc-qa-" + "1" * 16, uid=0, gid=0)

    def test_exact_local_image_check_denies_when_docker_missing(self):
        with patch("tools.bcc_candidate_qa.shutil.which", return_value=None):
            with self.assertRaisesRegex(JobBlocked, "no host-Python fallback"):
                qa._existing_image(self.IMAGE)

    def test_inspection_requires_exact_local_id(self):
        fake = type("Result", (), {"returncode": 0,
                                   "stdout": (self.IMAGE + "\n").encode()})()
        with patch("tools.bcc_candidate_qa.shutil.which", return_value="/usr/bin/docker"):
            with patch("tools.bcc_candidate_qa.subprocess.run", return_value=fake) as run:
                qa._existing_image(self.IMAGE)
        argv = run.call_args.args[0]
        self.assertEqual(argv[:3], ["docker", "image", "inspect"])
        self.assertNotIn("pull", argv)
        self.assertNotIn("build", argv)

    def test_wrong_local_image_id_denied(self):
        fake = type("Result", (), {"returncode": 0,
                                   "stdout": ("sha256:" + "b" * 64 + "\n").encode()})()
        with patch("tools.bcc_candidate_qa.shutil.which", return_value="/usr/bin/docker"):
            with patch("tools.bcc_candidate_qa.subprocess.run", return_value=fake):
                with self.assertRaises(JobBlocked):
                    qa._existing_image(self.IMAGE)

    def test_plan_does_not_start_docker(self):
        fake_report = {"candidate": str(self.candidate),
                       "source_sha": self.SHA, "prompt_sha256": self.PROMPT,
                       "diff_sha256": hashlib.sha256(self.PATCH).hexdigest(),
                       "files": ["bcc/scheduler.py"]}
        with patch("tools.bcc_candidate_qa.review_candidate", return_value=fake_report):
            with patch("tools.bcc_candidate_qa.docker_args",
                       return_value=["docker", "run", "-m", "pytest", "-q", "-p",
                                     "no:cacheprovider", "tests"]):
                with patch("tools.bcc_candidate_qa.subprocess.run",
                           side_effect=AssertionError("Docker invoked")):
                    result = qa.plan(self.candidate, self.candidate, self.PATCH,
                                     self.SHA, self.PROMPT, self.IMAGE)
        self.assertFalse(result["qa_executed"])
        self.assertEqual(result["status"], "OFFLINE_DOCKER_QA_PLAN")
        self.assertEqual(result["sandbox_network"], "NONE")
        self.assertEqual(result["github_writes"], "DISABLED")

    def test_candidate_must_be_exact_temporary_sibling(self):
        # Source check and patch validation are mocked, but path policy is real.
        cand = Path(tempfile.mkdtemp(prefix="bcc-pr12-review-"))
        self.addCleanup(lambda: cand.rmdir() if cand.exists() else None)
        result = {"diff_sha256": hashlib.sha256(self.PATCH).hexdigest(),
                  "files": {"bcc/scheduler.py": {"added": 1, "removed": 1}}}
        with patch("tools.bcc_candidate_qa.check_snapshot", return_value=self.base):
            with patch("tools.bcc_candidate_qa.inspect_patch", return_value=result):
                with patch("tools.bcc_candidate_qa._git_common",
                           return_value=self.base):
                    with patch("tools.bcc_candidate_qa._review_stage") as check:
                        stage = qa.review_candidate(self.base, cand, self.PATCH,
                                                    self.SHA, self.PROMPT)
        self.assertEqual(stage["status"], "CANDIDATE_REVIEWED_FOR_OFFLINE_QA")
        self.assertFalse(stage["candidate_executed"])
        check.assert_called_once()

    def test_outside_stage_is_rejected(self):
        result = {"diff_sha256": hashlib.sha256(self.PATCH).hexdigest(),
                  "files": {"bcc/scheduler.py": {"added": 1, "removed": 1}}}
        with patch("tools.bcc_candidate_qa.check_snapshot", return_value=self.base):
            with patch("tools.bcc_candidate_qa.inspect_patch", return_value=result):
                with self.assertRaises(JobBlocked):
                    qa.review_candidate(self.base, self.candidate, self.PATCH,
                                        self.SHA, self.PROMPT)


if __name__ == "__main__":
    unittest.main()
