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
        # 商品级类目虽已对齐，子单无本地/全局成功记录时仍应补写一次
        self.assertFalse(skip)

    def test_should_skip_when_all_items_globally_written(self) -> None:
        product = {
            "tangbuy_product_id": "TB002",
            "source_product_id": "99999",
            "linked_ord_lines": ["TO26070000100", "TO26070000101"],
            "mapping_record": {},
        }
        hs = {"category_id": 50003509, "category_cn_name": "测试类目"}
        with (
            patch.object(wb, "collect_item_nos", return_value=["TO26070000100", "TO26070000101"]),
            patch.object(
                wb,
                "collect_global_ok_item_nos_for_cid",
                return_value={"TO26070000100", "TO26070000101"},
            ),
        ):
            skip, reason = wb.should_skip_admin_writeback(product, hs)
        self.assertTrue(skip)
        self.assertEqual(reason, "already_ok")

    def test_push_category_writes_only_pending_items(self) -> None:
        product = {
            "source_product_id": "12345",
            "linked_ord_lines": ["TO26070000100", "TO26070000102"],
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
        with (
            patch.object(wb, "collect_item_nos", return_value=["TO26070000100", "TO26070000102"]),
            patch.object(wb, "change_item_category") as mock_change,
        ):
            result = wb.push_category_to_admin(product=product, hs=hs)
        self.assertEqual(result.get("status"), "ok")
        mock_change.assert_called_once()
        args, kwargs = mock_change.call_args
        self.assertEqual(kwargs.get("item_nos"), ["TO26070000102"])
        self.assertEqual(
            set(result.get("item_nos") or []),
            {"TO26070000100", "TO26070000102"},
        )

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

    def test_collect_item_nos_uses_linked_lines(self) -> None:
        product = {"linked_ord_lines": ["TI26060000099"]}
        self.assertEqual(wb.collect_item_nos(product), ["TI26060000099"])

    def test_persist_side_effects_schedules_without_writing_status(self) -> None:
        from app.services.products import store

        product = {
            "tangbuy_product_id": "TB001",
            "source_product_id": "12345",
            "linked_ord_lines": ["TI26060000062"],
            "hs_mapping": {"category_id": 50003509, "category_cn_name": "测试"},
            "mapping_record": {
                "admin_writeback": {"status": "skipped", "skip_reason": "no_items"},
            },
        }
        hs = {"category_id": 50003509, "category_cn_name": "测试"}
        with (
            patch.object(store, "upsert_local_mapping"),
            patch.object(wb, "schedule_admin_writeback") as mock_schedule,
            patch.object(wb, "should_skip_admin_writeback", return_value=(False, "")),
            patch.object(wb, "collect_item_nos", return_value=["TI26060000062"]),
        ):
            store.persist_product_mapping_side_effects(
                product,
                hs,
                manual=True,
                resolution="manual_correct",
            )
        mock_schedule.assert_called_once()

    def test_collect_item_nos_expands_to_main_order(self) -> None:
        from app.services.category_mapping import admin_writeback_collect as collect_mod

        with patch.object(collect_mod, "_expand_ord_line_ref", return_value=["TI26030000055"]):
            product = {"linked_ord_lines": ["TO26030000056"]}
            self.assertEqual(collect_mod.collect_item_nos(product), ["TI26030000055"])

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
        with (
            patch.object(wb, "collect_item_nos", return_value=["TO26070000100"]),
            patch.object(wb, "change_item_category") as mock_change,
        ):
            result = wb.push_category_to_admin(product=product, hs=hs)
        self.assertEqual(result.get("status"), "ok")
        self.assertTrue(result.get("skipped_duplicate"))
        mock_change.assert_not_called()


if __name__ == "__main__":
    unittest.main()
