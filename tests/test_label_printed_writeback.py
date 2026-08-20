# -*- coding: utf-8 -*-
"""库存标签打印状态回写（label_printed）修复自测。

覆盖场景（对应详设第 5 节）：
1. 单品打印成功 → 自动回写 label_printed=1（mock 打印客户端）
2. 打印失败/异常（打印机离线等）→ 500 且不标记
3. 商品未分配箱子 → 打印成功但不标记
4. 移箱保留 label_printed 标记（且幂等）
5. 批量补标记接口（item_ids 自动反查箱 / pairs 显式指定 两种模式）
6. 回归：GET 箱内商品、发货清单的 label_printed 随数据变化

mock 方式：patch label_print_client.get_client 返回 Mock 客户端，
wait_print_done 返回 True/False 或抛异常模拟打印成功/失败/离线。
数据库使用临时文件（DB_PATH 环境变量），不触碰真实数据。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

# 必须在导入 reply_server 之前设置，使 db_manager 单例使用临时数据库
_TMP_DIR = tempfile.mkdtemp(prefix="label_printed_test_")
os.environ["DB_PATH"] = os.path.join(_TMP_DIR, "test_label_printed.db")
os.environ["SQL_LOG_ENABLED"] = "false"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

import reply_server  # noqa: E402
from db_manager import db_manager  # noqa: E402


def _fake_print_client(print_ok=True, raise_exc=None):
    """构造 mock 标签打印客户端。"""
    fake = mock.Mock()
    fake.print_single_product_label.return_value = "task-mock-1"
    if raise_exc is not None:
        fake.wait_print_done.side_effect = raise_exc
    else:
        fake.wait_print_done.return_value = print_ok
    return fake


class LabelPrintedWritebackTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 绕过登录认证
        reply_server.app.dependency_overrides[reply_server.require_auth] = (
            lambda: {"user_id": 1, "username": "tester"}
        )
        cls.client = TestClient(reply_server.app)

        # 准备数据：两个箱子 + 三个商品分配到箱 A
        cls.box_a = db_manager.add_box(label="测试箱A")
        cls.box_b = db_manager.add_box(label="测试箱B")
        assert cls.box_a and cls.box_b
        db_manager.assign_item_to_box("ITEM-001", cls.box_a)
        db_manager.assign_item_to_box("ITEM-002", cls.box_a)
        db_manager.assign_item_to_box("ITEM-003", cls.box_a)

        # item_info + 待发货订单（用于发货清单回归）
        cur = db_manager.conn.cursor()
        cur.execute(
            "INSERT OR IGNORE INTO item_info (cookie_id, item_id, item_title, item_price) VALUES (?,?,?,?)",
            ("cookie-test", "ITEM-001", "测试商品1", "10"),
        )
        cur.execute(
            "INSERT OR IGNORE INTO orders (order_id, item_id, order_status, amount) VALUES (?,?,?,?)",
            ("ORD-001", "ITEM-001", "pending_ship", "10"),
        )
        db_manager.conn.commit()

    @classmethod
    def tearDownClass(cls):
        reply_server.app.dependency_overrides.clear()

    # ---- 工具 ----

    def _get_product(self, box_id, item_id):
        resp = self.client.get(f"/api/inventory/boxes/{box_id}/products")
        self.assertEqual(resp.status_code, 200)
        for p in resp.json().get("products", []):
            if p["item_id"] == item_id:
                return p
        return None

    def _print(self, item_id, fake_client):
        with mock.patch("label_print_client.get_client", return_value=fake_client):
            return self.client.post(
                f"/api/inventory/products/{item_id}/print-label",
                json={"box_label": "测试箱A", "item_name": "测试商品"},
            )

    # ---- 用例（按编号顺序执行）----

    def test_1_print_success_marks_label_printed(self):
        """打印成功 → 回写 label_printed=1，箱内商品视图可见。"""
        resp = self._print("ITEM-001", _fake_print_client(print_ok=True))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "done")
        self.assertTrue(data.get("marked"))

        p = self._get_product(self.box_a, "ITEM-001")
        self.assertIsNotNone(p)
        self.assertTrue(p["label_printed"])

    def test_2_print_failed_not_marked(self):
        """wait_print_done 返回 False（打印失败/取消/超时）→ 500 且不标记。"""
        resp = self._print("ITEM-002", _fake_print_client(print_ok=False))
        self.assertEqual(resp.status_code, 500)
        p = self._get_product(self.box_a, "ITEM-002")
        self.assertFalse(p["label_printed"])

    def test_3_print_exception_not_marked(self):
        """打印服务离线（抛 ConnectionError）→ 500 且不标记。"""
        resp = self._print("ITEM-002", _fake_print_client(raise_exc=ConnectionError("无法连接到标签打印服务")))
        self.assertEqual(resp.status_code, 500)
        p = self._get_product(self.box_a, "ITEM-002")
        self.assertFalse(p["label_printed"])

    def test_4_print_unboxed_item_not_marked(self):
        """商品未分配箱子 → 打印照常成功，但不标记。"""
        resp = self._print("ITEM-NOBOX", _fake_print_client(print_ok=True))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json().get("marked"))

    def test_5_move_preserves_label_printed(self):
        """移箱后 label_printed 标记保留。"""
        resp = self.client.post(
            f"/api/inventory/boxes/{self.box_a}/products/ITEM-001/move",
            json={"to_box_id": self.box_b},
        )
        self.assertEqual(resp.status_code, 200)
        # 旧箱已无该商品
        self.assertIsNone(self._get_product(self.box_a, "ITEM-001"))
        # 新箱中标记仍为已打印
        p = self._get_product(self.box_b, "ITEM-001")
        self.assertIsNotNone(p)
        self.assertTrue(p["label_printed"])

    def test_6_move_idempotent(self):
        """重复移箱（商品已在目标箱）→ 幂等成功。"""
        resp = self.client.post(
            f"/api/inventory/boxes/{self.box_a}/products/ITEM-001/move",
            json={"to_box_id": self.box_b},
        )
        self.assertEqual(resp.status_code, 200)
        p = self._get_product(self.box_b, "ITEM-001")
        self.assertTrue(p["label_printed"])

    def test_6b_assign_box_preserves_label_printed(self):
        """手动分配接口（PUT box）同样保留 label_printed 标记。"""
        # ITEM-001 当前在 box_b 且已标记，重新分配到 box_a
        resp = self.client.put(
            "/api/inventory/products/ITEM-001/box",
            json={"box_id": self.box_a},
        )
        self.assertEqual(resp.status_code, 200)
        p = self._get_product(self.box_a, "ITEM-001")
        self.assertIsNotNone(p)
        self.assertTrue(p["label_printed"])
        # 移回 box_b，恢复后续用例依赖的状态
        resp = self.client.put(
            "/api/inventory/products/ITEM-001/box",
            json={"box_id": self.box_b},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(self._get_product(self.box_b, "ITEM-001")["label_printed"])

    def test_7_batch_mark_printed(self):
        """批量补标记：item_ids 自动反查箱 + pairs 显式指定；未分配箱商品进 skipped。"""
        resp = self.client.post(
            "/api/inventory/products/mark-printed",
            json={
                "item_ids": ["ITEM-002", "ITEM-NOBOX"],
                "pairs": [{"item_id": "ITEM-003", "box_id": self.box_a}],
            },
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["marked"], 2)
        self.assertEqual(data["total"], 2)
        self.assertEqual([s["item_id"] for s in data["skipped"]], ["ITEM-NOBOX"])

        self.assertTrue(self._get_product(self.box_a, "ITEM-002")["label_printed"])
        self.assertTrue(self._get_product(self.box_a, "ITEM-003")["label_printed"])

        # 空参数 → 400
        resp = self.client.post("/api/inventory/products/mark-printed", json={})
        self.assertEqual(resp.status_code, 400)

    def test_8_shipping_list_reflects_label_printed(self):
        """回归：发货清单订单视图的已打/未打徽章随数据变化。"""
        resp = self.client.get("/api/inventory/shipping-list")
        self.assertEqual(resp.status_code, 200)
        orders = resp.json().get("orders", [])
        target = [o for o in orders if o["item_id"] == "ITEM-001"]
        self.assertEqual(len(target), 1)
        self.assertTrue(target[0]["label_printed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
