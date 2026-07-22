"""
标签打印客户端 —— 电脑A → 电脑B HTTP 调用。

封装对 dtp-webapp (电脑B:4050) 的库存标签打印 API 调用。
使用 httpx 同步客户端，在 async 上下文中需用 run_in_executor 包裹。
"""

import os
import sys
import json
from typing import Optional

import httpx
import yaml
from loguru import logger


# ---- 配置加载 ----

def _load_config() -> dict:
    """从 global_config.yml 加载 label_printer 配置"""
    config_path = os.path.join(os.path.dirname(__file__), 'global_config.yml')
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"配置文件不存在: {config_path}")
    with open(config_path, 'r', encoding='utf-8') as f:
        cfg = yaml.safe_load(f)
    return cfg.get('label_printer', {})


_label_cfg = _load_config()
BASE_URL = f"http://{_label_cfg.get('host', '127.0.0.1')}:{_label_cfg.get('port', 4050)}"


# ---- 客户端 ----

class LabelPrintClient:
    """电脑B 标签打印 HTTP 客户端"""

    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or BASE_URL).rstrip('/')
        self._client: Optional[httpx.Client] = None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=httpx.Timeout(30.0))
        return self._client

    def close(self):
        if self._client:
            self._client.close()
            self._client = None

    # ----- 通用请求 -----

    def _post(self, path: str, body: dict) -> dict:
        url = f"{self.base_url}{path}"
        try:
            resp = self.client.post(url, json=body)
            resp.raise_for_status()
            return resp.json()
        except httpx.ConnectError:
            logger.error(f"[标签打印] 无法连接电脑B: {url}")
            raise ConnectionError(f"无法连接到标签打印服务 {self.base_url}，请确认电脑B已启动")
        except httpx.HTTPStatusError as e:
            logger.error(f"[标签打印] 请求失败 {url}: {e.response.status_code} {e.response.text}")
            raise

    def _get(self, path: str) -> dict:
        url = f"{self.base_url}{path}"
        try:
            resp = self.client.get(url)
            resp.raise_for_status()
            return resp.json()
        except httpx.ConnectError:
            logger.error(f"[标签打印] 无法连接电脑B: {url}")
            raise ConnectionError(f"无法连接到标签打印服务 {self.base_url}，请确认电脑B已启动")

    def _get_sse(self, path: str):
        """SSE 流式读取（同步阻塞），返回生成器"""
        url = f"{self.base_url}{path}"
        try:
            with self.client.stream("GET", url) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if line.startswith("data: "):
                        yield json.loads(line[6:])
        except httpx.ConnectError:
            raise ConnectionError(f"无法连接到标签打印服务 {self.base_url}")

    # ----- 标签打印 -----

    def print_box_label(self, box_name: str, box_barcode: str = "",
                        box_description: str = "", copies: int = 1) -> str:
        """
        打印箱子标签。

        Args:
            box_name: 箱子名称 "小盒A-RE0吧唧"
            box_barcode: 箱子条码
            box_description: 箱子描述

        Returns:
            taskId，可传给 wait_print_done() 等待完成
        """
        result = self._post("/api/inventory-label/print", {
            "items": [{
                "name": box_name,
                "phone": box_barcode,
                "address": box_description,
            }],
            "labelType": "box",
            "printerName": "",  # 空字符串 = 用电脑B的第一个打印机
            "copies": copies,
        })
        task_id = result.get("taskId", "")
        logger.info(f"[标签打印] 箱子标签已提交: {box_name}, taskId={task_id}")
        return task_id

    def print_product_labels(self, products: list[dict], copies: int = 1) -> str:
        """
        批量打印商品标签。

        Args:
            products: [
                {"name": "雷姆 吧唧", "phone": "SKU-001", "address": "小盒A-RE0吧唧"},
                {"name": "拉姆 吧唧", "phone": "SKU-002", "address": "小盒A-RE0吧唧"},
            ]
            copies: 每张打印份数

        Returns:
            taskId
        """
        result = self._post("/api/inventory-label/print", {
            "items": products,
            "labelType": "product",
            "printerName": "",
            "copies": copies,
        })
        task_id = result.get("taskId", "")
        logger.info(f"[标签打印] 商品标签已提交: {len(products)} 件, taskId={task_id}")
        return task_id

    def print_single_product_label(self, item_name: str, item_id: str,
                                   box_label: str, copies: int = 1) -> str:
        """
        打印单个商品标签。

        Args:
            item_name: 商品名称
            item_id: 商品ID
            box_label: 所在箱子名称
            copies: 打印份数

        Returns:
            taskId
        """
        return self.print_product_labels([{
            "name": item_name,
            "phone": item_id,
            "address": box_label,
        }], copies=copies)

    def print_batch(self, items: list[dict], label_type: str = "product",
                    copies: int = 1) -> str:
        """
        通用批量打印（直接透传 items）。

        Args:
            items: 标签项列表
            label_type: "box" | "product"
            copies: 打印份数

        Returns:
            taskId
        """
        result = self._post("/api/inventory-label/print", {
            "items": items,
            "labelType": label_type,
            "printerName": "",
            "copies": copies,
        })
        return result.get("taskId", "")

    def wait_print_done(self, task_id: str) -> bool:
        """
        同步等待打印任务完成（阻塞）。

        Returns:
            True=全部成功, False=有失败或取消
        """
        try:
            for msg in self._get_sse(f"/api/inventory-label/print/{task_id}/status"):
                status = msg.get("status", "")
                if status == "printing":
                    item_name = (msg.get("item") or {}).get("name", "")
                    logger.debug(f"[标签打印] 正在打印: {item_name}")
                elif status == "error":
                    logger.error(f"[标签打印] 打印失败: {msg.get('error', '未知错误')}")
                    return False
                elif status == "cancelled":
                    logger.warning("[标签打印] 已取消")
                    return False
                elif status == "finished":
                    total = msg.get("total", 0)
                    logger.info(f"[标签打印] 全部完成, 共 {total} 张")
                    return True
                elif status == "timeout":
                    logger.warning("[标签打印] SSE 超时")
                    return False
        except Exception as e:
            logger.error(f"[标签打印] SSE 异常: {e}")
            return False
        return False

    # ----- 工具 -----

    def health_check(self) -> dict:
        """检查电脑B 打印服务是否在线"""
        return self._get("/api/health")

    def get_printers(self) -> list[dict]:
        """获取电脑B 的可用打印机列表"""
        result = self._get("/api/printers")
        return result.get("printers", [])


# ---- 模块级快捷函数（单例） ----

_client: Optional[LabelPrintClient] = None


def get_client() -> LabelPrintClient:
    global _client
    if _client is None:
        _client = LabelPrintClient()
    return _client


def print_box(box_name: str, box_barcode: str = "",
              box_description: str = "") -> str:
    """快捷函数：打印箱子标签"""
    return get_client().print_box_label(box_name, box_barcode, box_description)


def print_products(products: list[dict]) -> str:
    """快捷函数：批量打印商品标签"""
    return get_client().print_product_labels(products)


def wait_until_done(task_id: str) -> bool:
    """快捷函数：等待打印完成"""
    return get_client().wait_print_done(task_id)


# ---- 独立测试入口 ----

if __name__ == "__main__":
    print(f"=== 标签打印客户端测试 ===")
    print(f"  目标地址: {BASE_URL}")
    print()

    client = LabelPrintClient()

    # 1. 健康检查
    try:
        health = client.health_check()
        print(f"✅ 电脑B 在线: {health}")
    except Exception as e:
        print(f"❌ 电脑B 不在线: {e}")
        sys.exit(1)

    # 2. 获取打印机
    printers = client.get_printers()
    if printers:
        print(f"✅ 可用打印机: {len(printers)} 台")
        for p in printers:
            print(f"   - {p.get('name', '?')} ({p.get('type', '?')})")
    else:
        print("⚠️ 未检测到打印机，但仍尝试打印")

    # 3. 打印测试标签
    print()
    print("发送测试标签...")
    try:
        task_id = client.print_batch(
            items=[{
                "name": "测试标签-Phase1",
                "phone": "TEST-001",
                "address": "电脑A→电脑B 链路测试",
            }],
            label_type="product",
        )
        print(f"✅ 任务已提交, taskId={task_id}")

        # 等待打印完成
        print("等待打印完成...")
        ok = client.wait_print_done(task_id)
        if ok:
            print("✅ 打印成功！请检查打印机出纸。")
        else:
            print("❌ 打印未成功完成，请查看电脑B 日志。")
    except Exception as e:
        print(f"❌ 打印失败: {e}")

    client.close()
