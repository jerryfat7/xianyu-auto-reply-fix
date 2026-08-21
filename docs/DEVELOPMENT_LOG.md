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

### 3.11 label_printed 不回写（死代码修复，2026-08-20）
- **现象**：前端点击「打印标签」后打印机出纸、接口返回成功，但 `inventory_product_box.label_printed` 永远为 0，发货清单/箱内商品视图始终显示「未打」
- **原因**：
  1. `POST /api/inventory/products/{item_id}/print-label` 只发送打印任务，从未调用 `db_manager.mark_label_printed()`（该方法此前为死代码）
  2. 移箱（move）与手动分配（PUT box）实现为 `DELETE + INSERT OR IGNORE`，会把已置位的标记重置为 0
  3. 原代码忽略 `wait_print_done()` 返回值，打印失败/离线时仍返回成功
- **修复**：
  - 单品打印成功后反查商品所在箱并回写 `label_printed=1`（try/except 包裹，失败仅记 warning 不影响打印响应，响应附加 `marked` 字段）
  - 检查 `wait_print_done()` 返回值，打印失败/取消/超时（含服务离线）返回 500 且不标记
  - `db_manager` 新增 `get_item_box_id` / `mark_labels_printed_batch` / `move_item_between_boxes`（保留标记、幂等）/ `refresh_box_full_status`
  - 移箱接口与手动分配接口改用 `move_item_between_boxes`，保留标记
  - 新增批量补标记接口 `POST /api/inventory/products/mark-printed`（支持 `item_ids` 自动反查箱 / `pairs` 显式指定两种模式，用于历史数据人工补录）
  - 箱级打印接口 `/api/inventory/print-labels/{box_id}` 维持现状（箱标签 ≠ 商品标签），docstring 写明语义
  - 前端 `printProductLabel` 打印成功后刷新箱内商品/发货清单/商品列表视图的徽章
- **自测**：`tests/test_label_printed_writeback.py`（9 用例，mock `label_print_client.get_client`，临时数据库隔离，全部通过）

### 3.12 箱子/商品页增加打印状态标识（2026-08-20）
- **需求**：此前打印状态仅在发货清单页展示，箱子管理页与商品列表页看不到打印进度
- **后端改动**：
  - `GET /api/inventory/boxes`：每个箱子新增 `printed_count`（`COALESCE(SUM(pb.label_printed),0)` 单条聚合 SQL，无 N+1）
  - `GET /api/inventory/parent-products`：每个商品新增 `label_printed: bool`（`SELECT MAX(label_printed)` 按 item_id 聚合，多箱任一为 1 即 true；未入箱返回 false）
- **前端改动**：
  - 箱子管理页：容量列后新增「打印进度」列，`已打 X/Y` 三态 badge（全部 `bg-success` / 部分 `bg-warning text-dark` / 未打 `bg-secondary`，空箱/归档箱显示 `—`），表头与 colspan 9→10
  - 商品列表页：卡片 header 追加「已打/未打」徽章（`bg-success` / `bg-warning text-dark`），可与已下架/已归档徽章并列
  - 筛选下拉新增「未打标签」「已打标签」两项（客户端过滤：在售 + 未归档 + 打印状态）
- **自测**：`tests/test_inventory_print_status_views.py`（5 用例，临时数据库隔离，全部通过）；另用临时服务 + 浏览器实机验证三态颜色、卡片徽章、筛选结果、打印后进度同步

### 3.13 箱内商品弹窗增加标签打印状态列（2026-08-20）
- **需求**：箱子管理页点开具体箱子后，弹窗内的商品列表不显示标签是否打印，需补充
- **方案**：后端 `GET /api/inventory/boxes/{box_id}/products` 已返回 `label_printed`（`get_box_products` SQL 含 `pb.label_printed`），仅前端未展示，纯前端改动
- **改动**：
  - `static/index.html`：弹窗表头在「价格」与「操作」间加「标签」列，初始占位 `colspan` 4→5
  - `static/js/app.js`：`viewBoxProducts` 每行加标签状态徽章（`已打 bg-success` / `未打 bg-warning text-dark`，与发货清单/商品列表样式统一），加载中/空态/错误态 `colspan` 4→5
  - 打印成功后 `printProductLabel` 已有的弹窗刷新逻辑使徽章实时更新（无需额外改动）
- **验证**：临时服务 + 浏览器实机验证表头/徽章/打印回写后徽章实时更新

### 3.14 商品列表排序：更新时间倒序 + 已归档置底（2026-08-20）
- **需求**：商品列表原按标题 `ORDER BY title`（Unicode 码点序，非拼音），改为按商品更新时间倒序（最新在上），已归档商品置底
- **改动**：
  - `db_manager.get_parent_products`：4 个分支 SQL 排序改为 `COALESCE(归档子查询,0) ASC, updated_at DESC`（用 `inventory_product_box` 聚合子查询判断是否归档，多箱任一归档即置底）
  - 新增 `db_manager.touch_parents_updated_at(cookie_id, item_ids)`：刷新一批在售商品 `updated_at`
  - `reply_server.inventory_sync_from_xianyu`：同步后刷新本次同步商品的 `updated_at`，使"最新同步"排最前（否则已存在商品同步后位置不变）
- **说明**：`updated_at` 语义 = 首次入库 / 状态切换 / 最近一次同步
- **自测**：`tests/test_parent_products_ordering.py`（3 用例：时间倒序+归档置底 / touch 置顶 / 搜索时排序仍生效）

### 3.14.1 修复：排序结果反了（新上架商品靠后）（2026-08-21）
- **现象**：上一版加了 `touch_parents_updated_at` 在同步时把**全部在售商品**的 `updated_at` 统一刷成同一时刻，导致排序时间戳同质化，SQLite 退化为按 `rowid`（插入顺序）排 —— 新上架商品（rowid 大）反而排到后面；归档置底仍生效，所以新商品正好排在归档商品前
- **修复**：移除 `db_manager.touch_parents_updated_at` 方法及 `reply_server.inventory_sync_from_xianyu` 中的调用
  - 恢复后 `updated_at` 语义 = 首次入库 / 状态切换（delisted↔active）
  - 新上架商品入库时 `updated_at` 为当前时刻（最大）→ 自然排最前
- **自测**：`test_parent_products_ordering.py` 改为验证「新入库商品排最前 + 归档置底」

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
