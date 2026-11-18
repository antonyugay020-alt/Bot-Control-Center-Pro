"""Offline regression for BCC model task isolation and independent review.

No provider calls, repository file reads, GitHub writes or actual approvals.
"""
import builtins
import unittest
from unittest.mock import patch

from tools.bcc_kie_task_router import (
    REPO, RouteBlocked, dispatch_paid, pair_for_independent_review,
    prepare_handoff,
)

BASE = "7bba4909235cc56e0777075422c705a948eb44c2"
CANDIDATE = "e" * 40  # Synthetic commit ID: not claimed to exist on GitHub.
FILES = ("bcc/trading.py", "bcc/config.py", "tests/test_trading.py")


def task(tag="develop_core", model="terra", stage="implementation",
         source_sha=BASE, context_paths=FILES):
    return prepare_handoff(
        task_tag=tag,
        task_text="Review market book freshness and risk fencing without live orders",
        model=model, stage=stage, source_sha=source_sha,
        context_paths=context_paths, max_output_tokens=1024,
    )


class RoutingTests(unittest.TestCase):
    def test_terra_implementation_handoff_is_offline_only(self):
        item = task()
        data = item.safe_dict()
        self.assertEqual(data["repo"], REPO)
        self.assertEqual(data["source_sha"], BASE)
        self.assertEqual(data["model"], "terra")
        self.assertEqual(data["stage"], "implementation")
        self.assertGreater(data["historical_price_micro_usd"], 0)
        self.assertTrue(data["quote_conflict"])
        self.assertFalse(data["provider_contract_verified"])
        self.assertEqual(data["paid_calls"], "DISABLED")
        self.assertEqual(data["authorization"], "NONE")
        self.assertFalse(data["work_graphify_connected"])
        self.assertNotIn("Review market book", str(data))

    def test_only_explicit_model_and_stage_accepted(self):
        with self.assertRaises(TypeError):
            prepare_handoff(task_tag="task1", task_text="x",
                            stage="implementation", source_sha=BASE,
                            context_paths=FILES)
        for model, stage in [
            ("astra", "implementation"),
            ("astra", "independent_review"),
            ("claude", "implementation"),
            ("kimi", "delete_user_files"),
            ([], "implementation"),
            ("terra", {}),
        ]:
            with self.subTest(model=model, stage=stage):
                with self.assertRaises(RouteBlocked):
                    task(model=model, stage=stage)

    def test_kimi_review_stays_independent(self):
        implementation = task()
        review = task(tag="review_core", model="kimi",
                      stage="independent_review", source_sha=CANDIDATE,
                      context_paths=("bcc/trading.py",))
        link = pair_for_independent_review(
            implementation, review, candidate_sha=CANDIDATE)
        self.assertEqual(link["base_sha"], BASE)
        self.assertEqual(link["candidate_sha"], CANDIDATE)
        self.assertFalse(link["git_ancestry_verified"])
        self.assertFalse(link["ready_for_external_api"])
        self.assertTrue(link["independent_models"])

    def test_review_of_old_base_is_blocked(self):
        implementation = task()
        review = task(tag="review_base", model="kimi",
                      stage="independent_review", source_sha=BASE)
        with self.assertRaises(RouteBlocked):
            pair_for_independent_review(implementation, review,
                                        candidate_sha=CANDIDATE)

    def test_review_same_model_blocked(self):
        implementation = task()
        review = task(tag="review_core", model="terra",
                      stage="independent_review", source_sha=CANDIDATE)
        with self.assertRaises(RouteBlocked):
            pair_for_independent_review(implementation, review,
                                        candidate_sha=CANDIDATE)

    def test_review_out_of_scope_blocked(self):
        implementation = task()
        review = task(tag="review_extra", model="kimi",
                      stage="independent_review", source_sha=CANDIDATE,
                      context_paths=("bcc/risk_external.py",))
        with self.assertRaises(RouteBlocked):
            pair_for_independent_review(implementation, review,
                                        candidate_sha=CANDIDATE)

    def test_missing_candidate_and_fabricated_commit_blocked(self):
        implementation = task()
        review = task(tag="review_core", model="kimi",
                      stage="independent_review", source_sha=CANDIDATE)
        for invalid in (BASE, "NOT_A_SHA", "", 777):
            with self.subTest(invalid=invalid):
                with self.assertRaises(RouteBlocked):
                    pair_for_independent_review(implementation, review,
                                                candidate_sha=invalid)

    def test_astra_only_explicit_architecture_and_unquoted(self):
        item = task(tag="risk_architecture", model="astra", stage="architecture")
        data = item.safe_dict()
        self.assertIsNone(data["historical_price_micro_usd"])
        self.assertEqual(data["model"], "astra")
        self.assertEqual(data["paid_calls"], "DISABLED")

    def test_untrusted_paths_rejected(self):
        invalid = [
            "../secrets.key", "bcc/../secrets.py", "bcc\\config.py",
            "bcc/.env", "bcc/keys/secret.py", "MY_BOTS/trading.py",
            "REPORTS/log.txt", "docs/../../app.py", "docs/token/secret.md",
            "/tmp/bcc/config.py", "tools/foobar.exe", "bcc/./config.py",
            "bcc/config.py\nGET-KEY", "bcc/private/notes.md",
        ]
        for path in invalid:
            with self.subTest(path=path):
                with self.assertRaises(RouteBlocked):
                    task(context_paths=(path,))

    def test_context_duplicate_or_empty_denied(self):
        for paths in [(), ("bcc/config.py", "bcc/config.py"), 4,
                      ("bcc/config.py",) * 33]:
            with self.subTest(paths=str(paths)[:45]):
                with self.assertRaises(RouteBlocked):
                    task(context_paths=paths)

    def test_input_and_git_tag_bounds(self):
        for sha in ("f" * 39, "F" * 40, "q" * 40, ""):
            with self.assertRaises(RouteBlocked):
                task(source_sha=sha)
        with self.assertRaises(RouteBlocked):
            task(tag="a")
        with self.assertRaises(RouteBlocked):
            prepare_handoff(task_tag="large_task", task_text="a" * 24_001,
                            model="terra", stage="implementation",
                            source_sha=BASE, context_paths=FILES)

    def test_router_never_reads_repository_source(self):
        with patch.object(builtins, "open", side_effect=AssertionError("unexpected file access")):
            item = task()
        self.assertEqual(item.source_sha, BASE)

    def test_paid_dispatch_always_blocked(self):
        with self.assertRaises(RouteBlocked):
            dispatch_paid(model="terra", task="anything", approved=True)


if __name__ == "__main__":
    unittest.main()
