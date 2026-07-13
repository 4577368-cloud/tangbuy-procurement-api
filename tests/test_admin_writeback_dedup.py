"""Admin 品类回写去重。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.services.category_mapping import admin_writeback as wb


class AdminWritebackDedupTests(unittest.TestCase):
    def test_local_writeback_covers_same_cid_and_items(self) -> None:
        prev = {
            "status": "ok",
            "cid": 50003509,
            "item_nos": ["TO26070000100", "TO26070000101"],
        }
        self.assertTrue(
            wb.local_writeback_covers(
                prev,
                cid=50003509,
                item_nos=["TO26070000100"],
            )
        )

    def test_local_writeback_not_cover_new_item(self) -> None:
        prev = {
            "status": "ok",
            "cid": 50003509,
            "item_nos": ["TO26070000100"],
        }
        self.assertFalse(
            wb.local_writeback_covers(
                prev,
                cid=50003509,
                item_nos=["TO26070000100", "TO26070000102"],
            )
        )

    def test_should_skip_when_admin_already_has_target_cid(self) -> None:
        product = {
            "source_product_id": "12345",
            "linked_ord_lines": ["TO26070000100"],
            "mapping_record": {},
        }
        hs = {"category_id": 50003509, "category_cn_name": "测试类目"}
        with patch.object(wb, "_fetch_admin_from_category", return_value=(50003509, "测试类目")):
            skip, reason = wb.should_skip_admin_writeback(product, hs)
        self.assertTrue(skip)
        self.assertEqual(reason, "admin_already")

    def test_should_skip_when_writeback_in_flight(self) -> None:
        product = {
            "source_product_id": "12345",
            "linked_ord_lines": ["TO26070000100"],
            "mapping_record": {
                "admin_writeback": {
                    "status": "writing",
                    "to_cid": 50003509,
                }
            },
        }
        hs = {"category_id": 50003509}
        with patch.object(wb, "_fetch_admin_from_category", return_value=(0, "")):
            skip, reason = wb.should_skip_admin_writeback(product, hs)
        self.assertTrue(skip)
        self.assertEqual(reason, "in_flight")

    def test_resolve_placeholder_skips_without_linked_lines(self) -> None:
        product = {
            "tangbuy_product_id": "TB001",
            "source_product_id": "12345",
            "linked_ord_lines": [],
            "mapping_record": {},
        }
        hs = {"category_id": 50003509, "category_cn_name": "女鞋"}
        with patch.object(wb, "_fetch_admin_from_category", return_value=(0, "")):
            result = wb.resolve_admin_writeback_placeholder(
                product,
                hs,
                at="2026-07-13T00:00:00.000Z",
            )
        self.assertEqual(result.get("status"), "skipped")
        self.assertEqual(result.get("skip_reason"), "no_items")

    def test_reconcile_stale_writing_without_lines(self) -> None:
        product = {
            "tangbuy_product_id": "TB001",
            "linked_ord_lines": [],
            "mapping_record": {
                "admin_writeback": {
                    "status": "writing",
                    "at": "2026-07-13T00:00:00.000Z",
                }
            },
        }
        fixed, changed = wb.reconcile_stale_admin_writeback(product)
        self.assertTrue(changed)
        self.assertEqual(
            fixed["mapping_record"]["admin_writeback"]["status"],
            "skipped",
        )

    def test_push_category_skips_duplicate_without_api_call(self) -> None:
        product = {
            "source_product_id": "12345",
            "linked_ord_lines": ["TO26070000100"],
            "mapping_record": {
                "admin_writeback": {
                    "status": "ok",
                    "cid": 50003509,
                    "item_nos": ["TO26070000100"],
                    "to_category": "测试类目",
                }
            },
        }
        hs = {"category_id": 50003509, "category_cn_name": "测试类目"}
        with patch.object(wb, "change_item_category") as mock_change:
            result = wb.push_category_to_admin(product=product, hs=hs)
        self.assertEqual(result.get("status"), "ok")
        self.assertTrue(result.get("skipped_duplicate"))
        mock_change.assert_not_called()


if __name__ == "__main__":
    unittest.main()
