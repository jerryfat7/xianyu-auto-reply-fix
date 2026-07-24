"""
自动分箱引擎 — Phase 4

规则内嵌到箱子的匹配逻辑:
- 遍历 item_info 中所有未分配商品
- 按箱子 priority DESC + is_full=0 遍历
- ip_tags AND cat_tags 同时匹配 → 分配
- 所有匹配箱子满 → 标记 overflow（不自动兜底）
- 无任何匹配 → 标记 unmatched
"""

import uuid
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger


@dataclass
class BoxingResult:
    """单次分箱结果"""
    total: int = 0
    assigned: int = 0
    unmatched: int = 0
    overflow: int = 0
    overflow_detail: list[dict] = field(default_factory=list)
    unmatched_items: list[dict] = field(default_factory=list)


def _match_item(item_title: str, ip_tags: str, cat_tags: str) -> bool:
    """检查商品标题是否匹配箱子规则。"""
    if not item_title:
        return False
    title_lower = item_title.lower()

    # IP 匹配: '*' 通配，或任一关键词在标题中
    if ip_tags == '*':
        ip_ok = True
    else:
        ip_ok = any(
            tag.strip().lower() in title_lower
            for tag in ip_tags.split(',')
            if tag.strip()
        )

    # 品类匹配
    if cat_tags == '*':
        cat_ok = True
    else:
        cat_ok = any(
            tag.strip().lower() in title_lower
            for tag in cat_tags.split(',')
            if tag.strip()
        )

    return ip_ok and cat_ok


def auto_box(db_manager, only_new: bool = True, operation: str = 'auto_box') -> BoxingResult:
    """
    执行自动分箱。
    """
    result = BoxingResult()
    batch_id = str(uuid.uuid4())[:8]

    if only_new:
        items = db_manager.get_unboxed_items()
    else:
        # 全部重新处理：在一个事务内完成清空+分配
        from db_manager import db_manager as dbm
        with dbm.lock:
            cursor = dbm.conn.cursor()
            # 清空旧映射
            cursor.execute("DELETE FROM inventory_product_box")
            # 重置箱子满状态
            cursor.execute("UPDATE inventory_boxes SET is_full = 0")
            # 获取所有商品
            cursor.execute("""
                SELECT ii.item_id, ii.item_title
                FROM item_info ii
                ORDER BY ii.item_title
            """)
            items = [{'item_id': r[0], 'item_title': r[1] or ''} for r in cursor.fetchall()]

    result.total = len(items)
    if not items:
        logger.info("[自动分箱] 没有待分配商品")
        return result

    # 获取所有箱子（按 priority 降序）
    boxes = db_manager.get_boxes()
    available_boxes = [b for b in boxes if not b.get('is_full')]
    logger.info(f"[自动分箱] 箱子总数 {len(boxes)}, 可用 {len(available_boxes)}")

    if not available_boxes:
        result.unmatched = len(items)
        return result

    sample_title = items[0].get('item_title', '') if items else ''
    sample_matched = [b['label'] for b in available_boxes
                      if _match_item(sample_title, b.get('ip_tags', '*'), b.get('cat_tags', '*'))]
    logger.info(f"[自动分箱] 采样: '{sample_title}' → {sample_matched}")

    for item in items:
        item_id = item['item_id']
        title = item.get('item_title') or ''

        assigned = False
        matched_full_boxes = []

        for box in available_boxes:
            if box.get('is_full'):
                continue
            if not _match_item(title, box.get('ip_tags', '*'), box.get('cat_tags', '*')):
                continue

            box_id = box['id']
            # 直接用 raw SQL 分配（避免 ORM 锁问题）
            try:
                from db_manager import db_manager as dbm2
                with dbm2.lock:
                    cur = dbm2.conn.cursor()
                    cur.execute(
                        "INSERT OR IGNORE INTO inventory_product_box (item_id, box_id) VALUES (?, ?)",
                        (item_id, box_id)
                    )
                    if cur.rowcount > 0:
                        # 检查箱子是否已满
                        cur.execute(
                            "SELECT COUNT(*) FROM inventory_product_box WHERE box_id = ?", (box_id,)
                        )
                        cnt = cur.fetchone()[0]
                        cap = box.get('capacity')
                        if cap and cnt >= cap:
                            cur.execute("UPDATE inventory_boxes SET is_full = 1 WHERE id = ?", (box_id,))
                            box['is_full'] = True
                        dbm2.conn.commit()
                        result.assigned += 1
                        assigned = True
                        # 记录分箱日志
                        try:
                            import json
                            images_json = ''
                            cur.execute("SELECT images FROM item_parents WHERE item_id = ?", (item_id,))
                            img_row = cur.fetchone()
                            if img_row and img_row[0]:
                                images_json = img_row[0]
                            cur.execute("SELECT item_price FROM item_info WHERE item_id = ?", (item_id,))
                            price_row = cur.fetchone()
                            item_price = price_row[0] if price_row else ''
                            db_manager.add_auto_box_log(
                                batch_id, item_id, title, item_price or '',
                                box['label'] or '', images_json, operation
                            )
                        except Exception as log_e:
                            logger.warning(f"[自动分箱] 记录日志失败: {log_e}")
                        break
            except Exception as e:
                logger.warning(f"[自动分箱] 分配异常: {title} → {box['label']}: {e}")

        if assigned:
            continue
        if matched_full_boxes:
            result.overflow += 1
            continue
        result.unmatched += 1
        result.unmatched_items.append({'item_id': item_id, 'item_title': title})

    logger.info(f"[自动分箱] 完成: 总计 {result.total}, 已分配 {result.assigned}, 溢出 {result.overflow}, 未匹配 {result.unmatched}")
    return result


def rebox_all(db_manager, confirm_text: str) -> Optional[BoxingResult]:
    """
    重新分箱: 清空所有映射 → 全量重新匹配。

    Args:
        db_manager: DBManager 实例
        confirm_text: 确认文本，必须等于 "我确认删除所有映射"

    Returns:
        BoxingResult 或 None (确认失败)
    """
    if confirm_text != "我确认删除所有映射":
        logger.warning("[重新分箱] 确认文本不匹配，操作取消")
        return None

    count = db_manager.clear_all_mappings()
    logger.info(f"[重新分箱] 已清空 {count} 条映射")

    return auto_box(db_manager, only_new=False, operation='rebox')
