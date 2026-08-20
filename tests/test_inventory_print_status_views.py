# -*- coding: utf-8 -*-
"""库存打印状态标识（箱子页 printed_count / 商品列表页 label_printed）自测。

覆盖验收标准：
1. GET /api/inventory/boxes 返回 printed_count（全部/部分/未打/空箱/归档箱聚合正确）
2. GET /api/inventory/parent-products 返回 label_printed
   - 未入箱商品返回 false
   - 多箱时任一为 1 即 true
   - 下架/归档商品照常返回该字段
3. 打印一件后箱子进度与商品列表状态同步（依赖已修复的回写）

数据库使用临时文件（DB_PATH 环境变量），不触碰真实数据。
"""
import os
import sys
import tempfile
import unittest

# 必须在导入 reply_server 之前设置，使 db_manager 单例使用临时数据库
_TMP_DIR = tempfile.mkdtemp(prefix="print_status_view_test_")
os.environ["DB_PATH"] = os.path.join(_TMP_DIR, "test_print_status_view.db")
os.environ["SQL_LOG_ENABLED"] = "false"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

import reply_server  # noqa: E402
from db_manager import db_manager  # noqa: E402


class InventoryPrintStatusViewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reply_server.app.dependency_overrides[reply_server.require_auth] = (
            lambda: {"user_id": 1, "username": "tester"}
        )
        cls.client = TestClient(reply_server.app)

        cls.box_a = db_manager.add_box(label="状态箱A", capacity=10)
        cls.box_b = db_manager.add_box(label="状态箱B", capacity=10)

        # 插入父商品
        cur = db_manager.conn.cursor()
        for iid, title in [
            ("P-001", "已打商品"), ("P-002", "未打商品"), ("P-003", "未入箱商品"),
            ("P-004", "多箱商品"), ("P-005", "归档商品"),
        ]:
            cur.execute(
                "INSERT OR IGNORE INTO item_parents (item_id, title, status, cookie_id) VALUES (?,?,?,?)",
                (iid, title, 'active', 'cookie-status'),
            )
        db_manager.conn.commit()

        # P-001 → box_a 已打；P-002 → box_a 未打；P-004 → box_a(已打)+box_b(未打)；P-005 → box_a 已打后归档
        db_manager.assign_item_to_box("P-001", cls.box_a)
        db_manager.assign_item_to_box("P-002", cls.box_a)
        db_manager.assign_item_to_box("P-004", cls.box_a)
        db_manager.assign_item_to_box("P-004", cls.box_b)
        db_manager.assign_item_to_box("P-005", cls.box_a)
        db_manager.mark_label_printed("P-001", cls.box_a)
        db_manager.mark_label_printed("P-004", cls.box_a)
        db_manager.mark_label_printed("P-005", cls.box_a)
        # P-005 归档（手动置位 archived 模拟归档商品）
        cur = db_manager.conn.cursor()
        cur.execute(
            "UPDATE inventory_product_box SET archived=1, original_box_id=? WHERE item_id=? AND box_id=?",
            (cls.box_a, "P-005", cls.box_a),
        )
        db_manager.conn.commit()

    @classmethod
    def tearDownClass(cls):
        reply_server.app.dependency_overrides.clear()

    # ---- 箱子页 printed_count ----

    def test_boxes_return_printed_count_full(self):
        """箱子页：已打商品聚合到 printed_count，三态数值正确。"""
        resp = self.client.get("/api/inventory/boxes")
        self.assertEqual(resp.status_code, 200)
        boxes = {b["id"]: b for b in resp.json().get("boxes", [])}
        self.assertIn("printed_count", boxes[self.box_a])
        # box_a: P-001(已打) P-002(未打) P-004(已打) P-005(已打/归档) → 3/4
        self.assertEqual(boxes[self.box_a]["printed_count"], 3)
        self.assertEqual(boxes[self.box_a]["product_count"], 4)
        # box_b: 仅 P-004(未打) → 0/1
        self.assertEqual(boxes[self.box_b]["printed_count"], 0)
        self.assertEqual(boxes[self.box_b]["product_count"], 1)

    def test_boxes_empty_archive_zero(self):
        """箱子页：无已打商品的箱子 printed_count 为 0。"""
        resp = self.client.get("/api/inventory/boxes")
        boxes = {b["id"]: b for b in resp.json().get("boxes", [])}
        self.assertEqual(boxes[self.box_b]["printed_count"], 0)

    # ---- 商品列表页 label_printed ----

    def test_parent_products_label_printed(self):
        """商品列表页：已打/未打/未入箱/多箱/归档均返回 label_printed。"""
        resp = self.client.get("/api/inventory/parent-products")
        self.assertEqual(resp.status_code, 200)
        products = {p["item_id"]: p for p in resp.json().get("products", [])}

        # 已打
        self.assertTrue(products["P-001"]["label_printed"])
        # 未打
        self.assertFalse(products["P-002"]["label_printed"])
        # 未入箱 → false
        self.assertFalse(products["P-003"]["label_printed"])
        self.assertIsNone(products["P-003"]["box_id"])
        # 多箱任一为 1 → true
        self.assertTrue(products["P-004"]["label_printed"])
        # 归档商品照常返回字段
        self.assertTrue(products["P-005"]["label_printed"])
        self.assertTrue(products["P-005"]["is_archived"])

    def test_parent_products_search_still_works(self):
        """商品列表页：搜索过滤逻辑不受影响。"""
        resp = self.client.get("/api/inventory/parent-products?search=多箱")
        self.assertEqual(resp.status_code, 200)
        products = resp.json().get("products", [])
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0]["item_id"], "P-004")
        self.assertTrue(products[0]["label_printed"])

    def test_print_then_status_updates(self):
        """打印一件未打商品后，箱子进度与商品状态同步变已打。"""
        # P-002 当前未打
        before = self.client.get("/api/inventory/parent-products").json()
        p_before = [p for p in before["products"] if p["item_id"] == "P-002"][0]
        self.assertFalse(p_before["label_printed"])

        # mock 打印成功
        from unittest import mock
        fake = mock.Mock()
        fake.print_single_product_label.return_value = "task-mock"
        fake.wait_print_done.return_value = True
        with mock.patch("label_print_client.get_client", return_value=fake):
            resp = self.client.post(
                "/api/inventory/products/P-002/print-label",
                json={"box_label": "状态箱A", "item_name": "未打商品"},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json().get("marked"))

        # 商品列表同步
        after = self.client.get("/api/inventory/parent-products").json()
        p_after = [p for p in after["products"] if p["item_id"] == "P-002"][0]
        self.assertTrue(p_after["label_printed"])

        # 箱子进度同步：box_a 变 4/4
        boxes = {b["id"]: b for b in self.client.get("/api/inventory/boxes").json()["boxes"]}
        self.assertEqual(boxes[self.box_a]["printed_count"], 4)
        self.assertEqual(boxes[self.box_a]["product_count"], 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
