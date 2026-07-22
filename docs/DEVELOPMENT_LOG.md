# 库存管理功能 — 开发日志

> 记录 2026-07-22 ~ 2026-07-23 开发过程中的关键决策、问题修复和功能讨论。

---

## 一、功能概述

为 `xianyu-auto-reply-fix` 新增完整的库存管理模块，包含：

- 箱子管理（创建/编辑/删除/复制/优先级排序）
- 自动分箱引擎（IP+品类关键词匹配，按优先级分配）
- 商品列表（卡片布局，显示图片/价格/规格/所属箱子）
- 箱内商品查看（缩略图/移出/移动到其他箱/单件打印）
- 兜底箱子系统化（不可删除/IP品类锁定/释放全部商品）
- 发货清单（订单视图+箱子核对视图）
- 标签打印（对接电脑B DTP打印服务）

---

## 二、架构设计

```
电脑A (xianyu-auto-reply-fix, Docker)       电脑B (webapp, FastAPI :4050)
┌──────────────────────────────┐         ┌──────────────────────────┐
│ reply_server.py (API)        │ ──HTTP→ │ label_service.py (模板)   │
│ label_print_client.py        │         │ printer_service.py (DTP)  │
│ auto_box_engine.py           │         │ 40×30mm 标签纸           │
│ db_manager.py (SQLite)       │         └──────────────────────────┘
│ static/js/app.js (前端)      │
└──────────────────────────────┘
```

**数据库新增表**：
- `inventory_boxes` — 箱子（含规则 ip_tags/cat_tags/priority/is_default）
- `inventory_product_box` — 商品→箱子映射
- `item_parents` / `item_skus` — SKU 拆分模型
- `box_templates` — 箱子创建模板

---

## 三、关键问题与修复

### 3.1 `ii.images` 列不存在
- **现象**：多处 SQL 查询 `SELECT images FROM item_info` 报错
- **原因**：`item_info` 表无 `images` 列，图片从 `item_detail` JSON 或 `item_parents.images` 获取
- **修复**：
  - `get_unboxed_items`: `ii.images` → `ii.item_detail` + JSON 解析
  - `auto_box_engine`: JOIN `item_parents.images`
  - `get_box_products`: COALESCE(`ip.images`, `ii.item_detail`)

### 3.2 自动分箱全部失败
- **现象**：rebox-all 后 155 个商品全部 unmatched
- **原因**：`assign_item_to_box()` 的锁机制与 `auto_box` 循环冲突，`INSERT OR IGNORE` 始终返回 0
- **修复**：重写 `auto_box` 用原始 SQL 直接操作，在一个事务内完成清空+分配

### 3.3 多规格未识别
- **现象**：861410184837 实际多规格（艾一对26/雷一对26/拉一对10），但显示单规格
- **原因**：`is_multi_spec` 从未被设置，之前用 `cardType==1` 判断无效
- **修复**：用 `detail_params.isSKU == "1"` 检测多规格；同步时更新 `is_multi_spec`

### 3.4 价格解析失败
- **现象**：迁移报 `could not convert string to float: '¥26'`
- **修复**：`float(price.replace('¥','').replace(',','').strip() or 0)`

### 3.5 图片不显示
- **原因**：`item_info.item_detail` 是纯文本（非JSON），无法提取图片
- **修复**：同步时从闲鱼 API 的 `picInfo.picUrl` + `detail_params.imageInfos` 提取图片，存入 `item_parents.images`

### 3.6 每次启动产生新兜底箱子
- **原因**：迁移只查 `is_default=0` 的 `*/*` 箱子，已标记的兜底箱被排除后触发"没有则创建"
- **修复**：改为查所有 `*/*` 箱子，已存在则跳过创建

### 3.7 barcode 空字符串唯一约束冲突
- **现象**：第二个箱子创建失败
- **原因**：`barcode TEXT UNIQUE`，空字符串 `''` 是非 NULL 值
- **修复**：`NULLIF(?, '')` 将空串转 NULL

### 3.8 移动后界面卡死
- **原因**：每次操作后 `new bootstrap.Modal()` 创建新 backdrop 叠加
- **修复**：复用 modal 实例，打开状态下只刷新内容

### 3.9 详情被覆盖
- **现象**：浏览器抓取纯文本覆盖了同步写入的 JSON
- **修复**：`save_item_detail_only` 检测已有 JSON 格式则跳过

### 3.10 Tab 状态不更新
- **原因**：HTML 缺少 `id="inventoryTabs"`，JS 选择器匹配为空
- **修复**：给 `<ul>` 添加 `id="inventoryTabs"`

---

## 四、功能设计决策

### 4.1 兜底箱子系统化
- `is_default=1`，IP/品类/容量/优先级锁定
- 不可删除（前端隐藏按钮 + 后端 400）
- 编辑弹窗内"释放全部商品"按钮
- 首次启动自动创建（幂等）

### 4.2 匹配规则
- `ip_tags` AND `cat_tags` 同时匹配（子串）
- `*` 表示通配
- 按 `priority DESC` 排序，优先匹配高优先级箱子

### 4.3 商品卡片布局
- `card h-100 d-flex flex-column` 等高弹性布局
- `aspect-ratio: 1/1` + `object-fit: contain` 正方形容器
- 标题两行截断 (`-webkit-line-clamp: 2`)
- 底部箱子归属行

### 4.4 箱内查看弹窗
- 箱子名可点击打开
- 缩略图(48×48) | 标题 | 价格 | 操作栏
- 移出 + 移动到（下拉，已满灰色标注）+ 打印标签

### 4.5 标签模板
- BOX: 标题"箱子" + 名称 + （无电话行）+ 描述
- PRODUCT: 商品名 + 商品ID + 箱名，`showPhoneIcon=False`

---

## 五、文件变更清单

| 文件 | 模块 |
|---|---|
| `db_manager.py` | 库存表/SKU迁移/箱子CRUD/商品映射/发货清单 |
| `reply_server.py` | REST API（箱子/商品/分箱/打印/移动） |
| `auto_box_engine.py` | IP+品类匹配/自动分箱/重新分箱 |
| `label_print_client.py` | 电脑B HTTP客户端/单件打印 |
| `XianyuAutoAsync.py` | 同步流程增强（图片/isSKU/JSON详情） |
| `static/index.html` | 库存管理UI/弹窗 |
| `static/js/app.js` | 前端交互逻辑 |
| `Dockerfile-cn` / `docker-compose-cn.yml` | 部署优化 |
| `webapp/label_service.py` | 标签模板（BOX/PRODUCT showPhoneIcon） |

---

## 六、后续待开发

- [ ] 发货清单增强（缩略图/拣货勾选/标记已打印）
- [ ] 详情 API 获取完整多规格数据（skuBase）
- [ ] 商品列表筛选/搜索
- [ ] 批量打印标签
