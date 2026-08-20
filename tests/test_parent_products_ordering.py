# -*- coding: utf-8 -*-
"""商品列表排序规则自测：已归档置底 + updated_at 降序（最新在上）。

覆盖：
1. 未归档商品按 updated_at 降序（最新在上）
2. 已归档商品全部置底（内部仍按时间降序）
3. touch_parents_updated_at 刷新后该商品置顶
4. 搜索时排序同样生效

数据库使用临时文件（DB_PATH 环境变量），不触碰真实数据。
"""
import os
import sys
import tempfile
import unittest

# 必须在导入 reply_server 之前设置，使 db_manager 单例使用临时数据库
_TMP_DIR = tempfile.mkdtemp(prefix="parent_ordering_test_")
os.environ["DB_PATH"] = os.path.join(_TMP_DIR, "test_ordering.db")
os.environ["SQL_LOG_ENABLED"] = "false"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

import reply_server  # noqa: E402
from db_manager import db_manager  # noqa: E402


class ParentProductsOrderingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reply_server.app.dependency_overrides[reply_server.require_auth] = (
            lambda: {"user_id": 1, "username": "tester"}
        )
        cls.client = TestClient(reply_server.app)

        cls.box_a = db_manager.add_box(label="排序箱A")
        cls.box_archive = db_manager.get_archive_box()
        assert cls.box_archive, "系统归档箱应存在"

        cur = db_manager.conn.cursor()
        # 四个商品，updated_at 由旧到新
        data = [
            ("O-001", "最早商品", "2026-01-01 00:00:00"),
            ("O-002", "中间商品", "2026-06-01 00:00:00"),
            ("O-003", "最新商品", "2026-08-01 00:00:00"),
            ("O-004", "归档商品", "2026-08-15 00:00:00"),
        ]
        for iid, title, ts in data:
            cur.execute(
                "INSERT INTO item_parents (item_id, title, status, cookie_id, updated_at) VALUES (?,?,?,?,?)",
                (iid, title, 'active', 'cookie-ord', ts),
            )
        db_manager.conn.commit()

        # 全部放入普通箱；O-004 归档（archived=1）
        for iid in ["O-001", "O-002", "O-003", "O-004"]:
            db_manager.assign_item_to_box(iid, cls.box_a)
        cur = db_manager.conn.cursor()
        cur.execute(
            "UPDATE inventory_product_box SET archived=1, original_box_id=? WHERE item_id='O-004'",
            (cls.box_a,),
        )
        db_manager.conn.commit()

    @classmethod
    def tearDownClass(cls):
        reply_server.app.dependency_overrides.clear()

    def test_order_newest_first_archived_last(self):
        """未归档按更新时间降序，已归档置底。"""
        products = db_manager.get_parent_products()
        order = [p["item_id"] for p in products if p["item_id"].startswith("O-")]
        # 期望：O-003(最新,未归档) -> O-002 -> O-001(最早) -> O-004(归档,置底)
        self.assertEqual(order, ["O-003", "O-002", "O-001", "O-004"])
        # 归档商品确实排最后且 is_archived=True（不依赖完整列表索引，兼容全量测试环境）
        self.assertEqual(order[-1], "O-004")
        p004 = next(p for p in products if p["item_id"] == "O-004")
        self.assertTrue(p004["is_archived"])

    def test_touch_parents_updated_at_moves_to_top(self):
        """touch 后该商品排最前（模拟"刚同步的最上面"）。"""
        db_manager.touch_parents_updated_at("cookie-ord", ["O-002"])
        products = db_manager.get_parent_products()
        order = [p["item_id"] for p in products if p["item_id"].startswith("O-")]
        # O-002 被 touch 后置顶（其余未归档仍按时间，归档仍置底）
        self.assertEqual(order[0], "O-002")
        self.assertEqual(order[-1], "O-004")
        self.assertIn("O-003", order[1:3])

    def test_search_still_applies_ordering(self):
        """搜索"商品"时排序仍生效。"""
        products = db_manager.get_parent_products(search_text="商品")
        # 只校验本模块 O-xxx 商品的相对顺序（全量测试环境下库中可能混入其他模块数据）
        order = [p["item_id"] for p in products if p["item_id"].startswith("O-")]
        # 未归档按更新时间降序（O-003 最新），归档商品 O-004 置底
        self.assertEqual(order, ["O-003", "O-002", "O-001", "O-004"])
        self.assertEqual(len(order), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
