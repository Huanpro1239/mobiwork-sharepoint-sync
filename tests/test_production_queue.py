from __future__ import annotations

import unittest

from scoring.production_queue import (
    advance_retry_attempts,
    legacy_requires_rescore,
    select_pending_url_groups,
)


class ProductionQueueTests(unittest.TestCase):
    def test_unresolved_legacy_states_require_current_model_rescore(self):
        cases = [
            {"Phân Loại AI": "Can_duyet", "Trạng Thái Quyết Định": "LEGACY_REVIEW"},
            {"Phân Loại AI": "Khong_the_cham", "Trạng Thái Quyết Định": "TECHNICAL_FAILURE"},
            {"Phân Loại AI": "", "Trạng Thái Quyết Định": "PENDING_SCORE"},
            {"Phân Loại AI": "Bien_hieu", "Trạng Thái Quyết Định": "REVIEW_SCENE"},
        ]

        self.assertTrue(all(legacy_requires_rescore(row) for row in cases))

    def test_legacy_auto_final_labels_can_be_reused_during_migration(self):
        for label in ("Bien_hieu", "Trung_bay", "Khong_dat"):
            with self.subTest(label=label):
                self.assertFalse(
                    legacy_requires_rescore(
                        {
                            "Phân Loại AI": label,
                            "Trạng Thái Quyết Định": "LEGACY_AUTO_REUSED",
                        }
                    )
                )

    def test_duplicate_urls_consume_one_batch_slot_and_keep_all_row_indexes(self):
        rows = [
            {"hinh_anh": "https://example/a.jpg", "ngay": "2026-08-01"},
            {"hinh_anh": "https://example/a.jpg", "ngay": "2026-08-02"},
            {"hinh_anh": "https://example/b.jpg", "ngay": "2026-08-03"},
        ]

        selection = select_pending_url_groups(
            rows,
            candidate_indices=[0, 1, 2],
            attempts_by_url={},
            limit=1,
            max_attempts=3,
            selection="oldest",
        )

        self.assertEqual([group.url for group in selection.selected], ["https://example/a.jpg"])
        self.assertEqual(selection.selected[0].indices, (0, 1))
        self.assertEqual([group.url for group in selection.deferred], ["https://example/b.jpg"])

    def test_batch_limit_counts_unique_urls(self):
        rows = [
            {"hinh_anh": f"https://example/{index}.jpg", "ngay": f"2026-08-0{index + 1}"}
            for index in range(5)
        ]

        selection = select_pending_url_groups(
            rows,
            candidate_indices=list(range(5)),
            attempts_by_url={},
            limit=2,
            max_attempts=3,
            selection="oldest",
        )

        self.assertEqual(len(selection.selected), 2)
        self.assertEqual(len(selection.deferred), 3)

    def test_new_urls_are_prioritized_before_retrying_failures(self):
        rows = [
            {"hinh_anh": "https://example/retry.jpg", "ngay": "2026-08-01"},
            {"hinh_anh": "https://example/new.jpg", "ngay": "2026-08-31"},
        ]

        selection = select_pending_url_groups(
            rows,
            candidate_indices=[0, 1],
            attempts_by_url={"https://example/retry.jpg": 1},
            limit=1,
            max_attempts=3,
            selection="oldest",
        )

        self.assertEqual(selection.selected[0].url, "https://example/new.jpg")
        self.assertEqual(selection.deferred[0].url, "https://example/retry.jpg")

    def test_retry_limit_blocks_url_without_consuming_a_batch_slot(self):
        rows = [
            {"hinh_anh": "https://example/blocked.jpg", "ngay": "2026-08-01"},
            {"hinh_anh": "https://example/new.jpg", "ngay": "2026-08-02"},
        ]

        selection = select_pending_url_groups(
            rows,
            candidate_indices=[0, 1],
            attempts_by_url={"https://example/blocked.jpg": 3},
            limit=1,
            max_attempts=3,
            selection="oldest",
        )

        self.assertEqual([group.url for group in selection.blocked], ["https://example/blocked.jpg"])
        self.assertEqual([group.url for group in selection.selected], ["https://example/new.jpg"])

    def test_retry_state_clears_successes_and_separates_retryable_from_blocked(self):
        first = advance_retry_attempts(
            attempts_by_url={"https://example/a.jpg": 1, "https://example/success.jpg": 2},
            succeeded_urls={"https://example/success.jpg"},
            failed_urls={"https://example/a.jpg", "https://example/b.jpg"},
            max_attempts=3,
        )
        self.assertEqual(
            first.attempts_by_url,
            {"https://example/a.jpg": 2, "https://example/b.jpg": 1},
        )
        self.assertEqual(first.retryable_urls, frozenset({"https://example/a.jpg", "https://example/b.jpg"}))
        self.assertEqual(first.blocked_urls, frozenset())

        second = advance_retry_attempts(
            attempts_by_url=first.attempts_by_url,
            succeeded_urls=set(),
            failed_urls={"https://example/a.jpg"},
            max_attempts=3,
        )
        self.assertEqual(second.attempts_by_url["https://example/a.jpg"], 3)
        self.assertEqual(second.blocked_urls, frozenset({"https://example/a.jpg"}))


if __name__ == "__main__":
    unittest.main()
