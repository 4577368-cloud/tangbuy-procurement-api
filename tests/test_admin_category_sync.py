"""Admin 品类同步单元测试。"""

from __future__ import annotations

import unittest

from app.services.category_mapping.admin_sync import (
    hs_from_admin_entry,
    is_admin_category_trusted,
    is_placeholder_category,
)


class AdminCategorySyncTest(unittest.TestCase):
    def test_placeholder_other(self) -> None:
        self.assertTrue(is_placeholder_category("其它"))
        self.assertTrue(is_placeholder_category("其他"))
        self.assertTrue(is_placeholder_category("待映射"))
        self.assertFalse(is_placeholder_category("卫衣"))

    def test_hs_from_admin_dto(self) -> None:
        entry = {
            "goodsId": "683451269189",
            "categoryId": 50010159,
            "hsCodeDTO": {
                "cid": 50010159,
                "cnName": "卫衣",
                "enName": "Sweater",
                "decCnName": "卫衣1",
                "decEnName": "Sweater1",
                "hsCode": "6110200090",
                "needConfirm": 0,
            },
        }
        hs = hs_from_admin_entry(entry)
        self.assertIsNotNone(hs)
        assert hs is not None
        self.assertEqual(hs["category_id"], 50010159)
        self.assertEqual(hs["category_cn_name"], "卫衣")
        self.assertEqual(hs["hs_code"], "6110200090")
        self.assertEqual(hs["declare_cn_name"], "卫衣1")

    def test_hs_from_placeholder_rejected(self) -> None:
        entry = {
            "categoryId": 123,
            "hsCodeDTO": {"cid": 123, "cnName": "其它", "hsCode": "6110200090"},
        }
        self.assertIsNone(hs_from_admin_entry(entry))

    def test_trusted_requires_no_need_confirm(self) -> None:
        entry = {
            "categoryId": 50010159,
            "hsCodeDTO": {
                "cid": 50010159,
                "cnName": "卫衣",
                "hsCode": "6110200090",
                "decCnName": "卫衣",
                "needConfirm": 0,
            },
        }
        self.assertTrue(is_admin_category_trusted(entry))
        self.assertFalse(
            is_admin_category_trusted(entry, ord_row={"is_need_cfm": 1}),
        )


if __name__ == "__main__":
    unittest.main()
