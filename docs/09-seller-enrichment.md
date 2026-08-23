# 09 · Seller 关注与人工补全（Enrichment）

> 把"哪些卖家是对手、谁是重点关注"从「监控发现的事实」升级成「用户主动维护的画像」。**seller_enrichment** 表存用户标记，前端 UI 让你 30 秒补全一个新出现的 sellerId。

---

## 9.1 业务背景

监控在飞猪上发现的所有 seller 都会进 `sellers` 表（系统自动），sellerName 由 booktips 拉。但：

1. **新出现的 sellerId**——booktips 可能拿不到名字（限流 / 字段缺失 / 临时下架）
2. **同名歧义**——"成都链上旅游专营店"在不同 itemId 下其实是同一组织，要不要归并？
3. **谁重要**——业务上关心的对手是谁？竞品 / VIP / 已知合作方 / 黑名单？

这些**不在监控职责内**，但对运营方极有价值。所以加一张 `seller_enrichment` 表，**用户手动维护**。

---

## 9.2 表结构（回顾）

详见 [03-database-schema.md §3.2.4](docs/03-database-schema.md)：

```sql
CREATE TABLE seller_enrichment (
    seller_id       TEXT PRIMARY KEY,
    display_name    TEXT,                  -- 用户覆盖名（优先于 booktips）
    is_watched      INTEGER NOT NULL DEFAULT 0,
    notes           TEXT,
    tags            TEXT,                  -- JSON array
    priority        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    created_by      TEXT,
    FOREIGN KEY (seller_id) REFERENCES sellers(seller_id) ON DELETE CASCADE
);
```

---

## 9.3 前端三个入口

#### A. 卖家管理列表（`/sellers`）

- **表格列**：sellerId / 显示名 / 来源 / 关注 / 货架数 / 首次 / 操作
- **筛选**：全部 / 只看关注 / 只看未识别 / 未填 display_name
- **搜索**：按 sellerId / 显示名 / notes 全文搜（SQLite 用 LIKE，v1 不上 FTS5）
- **行交互**：点「关注」列的 `☆` 立即切到 `★`（AJAX 乐观 UI，无需刷新）
- **行点击**：进 `/sellers/{id}` 编辑表单

#### B. 卖家详情 / 编辑（`/sellers/{seller_id}`）

- **顶部**：seller 显示名 + sellerId + 「★ 关注中 / ☆ 未关注」chip
- **概况区**：shopUrl / icon / 服务人数 / 首次 / 最近 / 跨 POI 数 / 跨 cell 数
- **涵盖的 POI**：chips，点击跳 `/poi/{poi_id}?focus=seller:{seller_id}`
- **最近 30 轮该 seller 的货架列表**（缩略 SKU 表）
- **编辑表单**：
  - `display_name` —— 用户覆盖名（最高优先级）
  - `is_watched` —— checkbox
  - `priority` —— 0-3 下拉，影响列表排序
  - `notes` —— 单行文本（"黑名单 / 待联系 / 已合作"）
  - `tags` —— 逗号分隔，存为 JSON array
  - 「保存修改」POST 整个表单 → 乐观 UI

#### C. SKU 行内快捷入口

SKU 主表的 seller 列，点 seller 名 → 跳 `/sellers/{seller_id}`。**Hover 显示**：
- 如果未识别：tooltip 显示 "未识别 sellerId，点此补全"
- 如果已关注：tooltip 显示 "★ 关注的卖家"

---

## 9.4 名称回退规则（核心）

前端展示一个 seller 的显示名时按以下优先级：

```python
def display_name(seller_id: str, sellers_row, enrichment_row) -> str:
    if enrichment_row and enrichment_row.display_name:
        return enrichment_row.display_name          # ① 用户手动（最高）
    if sellers_row and sellers_row.seller_name:
        return sellers_row.seller_name               # ② booktips 自动
    return f"{seller_id[:6]}…"                       # ③ 兜底："221759…"
```

**为什么 ③ 显示截断 ID 而不是空**：用户一眼看到「这里有个新 seller 需要补」+ 点击直接进编辑页。

---

## 9.5 数据流

```
[监控脚本]
  shelf 解析 → 新 sellerId 入 sellers 表（不带 name）
       ↓
[booktips 增量] （cache miss 时拉一次）
  拉回 sellerName → UPDATE sellers.seller_name
       ↓
[前端 /sellers 列表]
  JOIN sellers + seller_enrichment
  显示 display_name / 来源 / 关注 状态
       ↓
[用户编辑 /sellers/{id}]
  POST /sellers/{seller_id}
  → INSERT/UPSERT seller_enrichment
  → 立即生效到所有页面（cells_snapshot 不变，JOIN 实时算）
```

---

## 9.6 "未识别"批量处理工作流

监控发现 3-5 个未识别的 sellerId（兜底显示 "221759…"），运营方工作流：

1. 进 `/sellers`，筛选「只看未识别」
2. 一行一行点进去：
   - 看到该 seller 的 POI / SKU / 价格分布
   - 看 shopUrl（如 `https://shopNNNNNN.m.taobao.com`）→ 浏览器打开
   - 在飞猪上确认这家店是干啥的
3. 填 display_name + 是否关注 + notes + tags → 保存
4. 立即在 POI 详情 / SKU 行看到新名字（前端无缓存）

**预期节奏**：发现一批未识别 → 集中 10-20 分钟补完 → 之后每天仅 1-2 个新增（稳定后）。

---

## 9.7 与告警系统的交互

`is_watched=1` 时的额外行为：

| 事件 | 普通 seller | watched seller |
|---|---|---|
| 该 seller 新 cell 出现 | `non_self_new` 告警 | `non_self_new` 告警 + **优先级更高**（warn 而非 info） |
| 该 seller 某 cell 价格异动 | `price_alert` | `price_alert` 标记为 watched |
| 该 seller 消失（所有 cell 都没了） | 不告警 | `watched_seller_disappeared` 告警（v2 增量） |

**v1 简化**：仅 webhook payload 里加 `"watched": true` 字段（见 [04-webhook-spec.md §4.2.1](docs/04-webhook-spec.md)）。

---

## 9.8 导入现有数据

`data/seller_cache.json` 里的 16 个 seller 已经有 seller_name（来自抓包时的 booktips）。部署时：

```bash
python3 scripts/init_db.py
# 该脚本会：
# 1. 从 seller_cache.json 读 16 个 seller，INSERT INTO sellers
# 2. 不动 seller_enrichment（空表，由用户后续填）
```

**首次部署后** `/sellers` 看到 16 个 seller（都有显示名），用户可以从这 16 个里挑重点标 `is_watched`。

---

## 9.9 批量编辑 API（v1 不做，v2 预留）

未来如果需要：
- `POST /api/sellers/bulk` —— 一次标多个 seller 为关注
- `GET /api/sellers/export.csv` —— 导出全量
- `POST /api/sellers/import.csv` —— 从 CSV 批量导入

v1 只做单条编辑。

---

## 9.10 接下来

1. 实现 `web/routes/sellers.py`：list + detail + edit + watch toggle
2. 实现 `web/templates/sellers/{list, detail}.html` + `partials/_seller_row.html`
3. 在 POI 详情页把 seller 列改为「点击跳 /sellers/{id}」
4. 在 SKU 主表的 hover 显示 seller 详情 chip