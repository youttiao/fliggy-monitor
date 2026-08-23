# 03 · 数据库 Schema（SQLite）

> 单一数据库文件 `/opt/fliggy-monitor/data/monitor.db`。WAL 模式，单写多读，无需额外服务。

---

## 3.1 设计原则

1. **每轮扫描 = 一行 `rounds` + 多行 `cells_snapshot`**，便于"任意时间点回放"
2. **sellers 维度表**保存卖家基本信息，**cells_snapshot 通过 FK 引用**，避免每行重复 seller 元数据
3. **config 是 KV 表**，用 JSON 列存结构化值；单行小表，方便前端读
4. **alerts 表只追加**，从不 UPDATE——告警是事件流，不是状态
5. **cookies 表**只存当前最新一组 + 续期历史，不暴露 token 明文给前端

---

## 3.2 表结构

### 3.2.1 `rounds` — 每轮扫描一次

```sql
CREATE TABLE rounds (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id        TEXT UNIQUE NOT NULL,        -- e.g. 'r202608231400'
    started_at      TEXT NOT NULL,               -- ISO 8601 with TZ, UTC
    finished_at     TEXT,                        -- NULL 表示进行中
    status          TEXT NOT NULL DEFAULT 'running',  -- running / success / partial / failed
    cells_total     INTEGER DEFAULT 0,
    cells_self      INTEGER DEFAULT 0,
    cells_non_self  INTEGER DEFAULT 0,
    new_sellers     INTEGER DEFAULT 0,           -- 本轮首次出现的 sellerId 数
    booktips_hits   INTEGER DEFAULT 0,
    error_msg       TEXT,                        -- 失败的 summary
    duration_ms     INTEGER                      -- finished_at - started_at
);

CREATE INDEX idx_rounds_started ON rounds(started_at DESC);
```

#### 字段语义

- `round_id` 用 `rYYYYMMDDHHMM` 格式，每 30 min 一轮（如 `r202608231400`），便于日志和告警引用
- `status`: `success`=全 8 POI OK、`partial`=部分失败、`failed`=全失败
- `duration_ms` 用于发现慢轮（> 30s 应告警）

### 3.2.2 `cells_snapshot` — 每行一个 cell 在一轮扫描中的快照

```sql
CREATE TABLE cells_snapshot (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id        INTEGER NOT NULL,
    poi_id          TEXT NOT NULL,               -- cell.poiId（注意：可能 != 当前 POI，跨 POI 套票）
    poi_name        TEXT NOT NULL,
    item_id         TEXT NOT NULL,
    sku_id          TEXT NOT NULL,
    cell_type       TEXT,                        -- '门票套餐' / '景点门票' / '园内项目' / '景区联票' / '周边景区门票' / '周边景区套票'
    sku_name        TEXT,                        -- 完整票名
    price_int       TEXT,                        -- 整数部分（"58"），存 TEXT 是为了保留前导 0
    price_dec       TEXT,                        -- 小数部分（".5" / ".0" / ".89"），含前导点
    price_suffix    TEXT,                        -- "起" / ""
    sold            TEXT,                        -- "1234" / "1.2w+" / "已售100+"
    seller_id       TEXT NOT NULL,
    is_self         INTEGER NOT NULL,            -- 0 / 1
    raw_shelf       TEXT,                        -- 该 cell 的原始 JSON（用于回溯字段）
    first_seen_at   TEXT NOT NULL,               -- 该 (poi,item,sku) 首次入库时间
    FOREIGN KEY (round_id) REFERENCES rounds(id) ON DELETE CASCADE,
    UNIQUE(round_id, poi_id, item_id, sku_id)
);

CREATE INDEX idx_cells_round    ON cells_snapshot(round_id);
CREATE INDEX idx_cells_poi      ON cells_snapshot(poi_id);
CREATE INDEX idx_cells_seller   ON cells_snapshot(seller_id);
CREATE INDEX idx_cells_item     ON cells_snapshot(item_id);
CREATE INDEX idx_cells_first    ON cells_snapshot(first_seen_at);
CREATE INDEX idx_cells_is_self  ON cells_snapshot(is_self);
```

#### 为什么不存 `booktips_raw` 在 cell 上？

booktips 是 itemId 级共享（一个 itemId 一次），不应该在每行 cell 上重复。booktips 原始数据放 `booktips_cache` 表里（如下）。

#### `first_seen_at` 怎么算？

- 本轮新出现的 `(poi_id, item_id, sku_id)` 三元组 → first_seen_at = 本轮 started_at
- 已存在的 → first_seen_at 保持首次记录的值（INSERT OR IGNORE 时不覆盖）

### 3.2.3 `sellers` — 卖家主表（latest snapshot）

```sql
CREATE TABLE sellers (
    seller_id       TEXT PRIMARY KEY,
    seller_name     TEXT,                        -- NULL 表示还没拉过 booktips
    seller_icon     TEXT,
    shop_jump_url   TEXT,
    service_stats   TEXT,                        -- JSON: [{"propName":"服务人数","propValue":"17w+"}]
    is_self         INTEGER NOT NULL DEFAULT 0, -- 0 / 1
    first_seen_at   TEXT NOT NULL,               -- 卖家首次在 shelf 中出现
    last_seen_at    TEXT NOT NULL,               -- 最近一次出现
    total_cells     INTEGER DEFAULT 0,           -- 累计独立 cell 数
    booktips_refreshed_at TEXT,                  -- 最近一次 booktips 拉取时间
    booktips_raw    TEXT,                        -- 最近一次 booktips 响应的 sellerInfo 子树（用于审计）
    UNIQUE(seller_id)
);

CREATE INDEX idx_sellers_self   ON sellers(is_self);
CREATE INDEX idx_sellers_last   ON sellers(last_seen_at DESC);
CREATE INDEX idx_sellers_name   ON sellers(seller_name);
```

#### 触发器：自动更新 last_seen / total_cells

```sql
CREATE TRIGGER trg_seller_upsert
AFTER INSERT ON cells_snapshot
BEGIN
    INSERT INTO sellers (seller_id, first_seen_at, last_seen_at, total_cells, is_self)
    VALUES (NEW.seller_id, NEW.first_seen_at, NEW.first_seen_at, 1, NEW.is_self)
    ON CONFLICT(seller_id) DO UPDATE SET
        last_seen_at = NEW.first_seen_at,
        total_cells  = total_cells + 1,
        is_self      = NEW.is_self;  -- 保持与最新 cell 一致（虽然 seller_id 通常稳定）
END;
```

### 3.2.4 `seller_enrichment` — 用户管理的卖家元数据（display_name / 关注 / 备注）

> 用户手动维护的卖家画像。**与 `sellers` 表分离**——前者是系统从 booktips 自动拉的，后者是用户补充/打标/关注。两者通过 `seller_id` 关联。

```sql
CREATE TABLE seller_enrichment (
    seller_id       TEXT PRIMARY KEY,                   -- 与 sellers.seller_id 对齐
    display_name    TEXT,                                -- 用户给的友好名称（覆盖 booktips seller_name）
    is_watched      INTEGER NOT NULL DEFAULT 0,          -- 0 / 1：是否关注
    notes           TEXT,                                -- 自由备注（例："已合作 / 黑名单 / 待联系"）
    tags            TEXT,                                -- JSON array（例：["vip","competitor"]）
    priority        INTEGER NOT NULL DEFAULT 0,          -- 0-3：影响前端排序
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    created_by      TEXT,                                -- 'admin' / 'system'（v1 单用户，都是 admin）
    FOREIGN KEY (seller_id) REFERENCES sellers(seller_id) ON DELETE CASCADE
);

CREATE INDEX idx_seller_enrich_watched ON seller_enrichment(is_watched);
CREATE INDEX idx_seller_enrich_priority ON seller_enrichment(priority DESC);
```

#### 字段语义

- `display_name`：用户在 `/sellers/{id}` 编辑页输入；前端展示优先级 **最高**
- `is_watched`：标 1 → 前端该卖家的所有货架用 **cyan 左色条 + ★ 前缀**；监控告警里**也**额外推一条「关注卖家」事件
- `notes`：纯文本，单行；前端 hover tooltip 显示
- `tags`：JSON 数组，自由定义；v1 仅做文本展示 + 过滤（未来可聚合统计）
- `priority`：0=默认，3=最高；用于「关注的卖家」列表排序
- `created_at / updated_at`：审计用
- 唯一约束在 `seller_id`：一个 seller 一行

#### 与 sellers 表的关系

| 字段 | sellers 表（系统） | seller_enrichment 表（用户） |
|---|---|---|
| `seller_name` | ✓ booktips 自动 | — |
| `display_name` | — | ✓ 用户覆盖（前端展示优先） |
| `is_watched` | — | ✓ 用户标记 |
| `notes/tags` | — | ✓ |
| `is_self` | ✓ 派生自 SELF_SELLER_ID | — |

#### 前端名称回退规则（review）

```
1. seller_enrichment.display_name  (用户手动)   ← 最高优先
2. sellers.seller_name              (booktips)   ← 自动（已 16 个）
3. sellerId[:6] + "…"               (兜底)       ← 例："221759…"
```

#### 监控脚本与 enrichment 的关系

- 监控脚本**只读** `is_self`（= `seller_id == SELF_SELLER_ID`）
- `is_watched` 仅用于前端**展示**和 `non_self_watched` 类型的告警（v2 增量，本轮不开）
- 监控脚本**不**自动写 `seller_enrichment`——所有写入都来自用户手动编辑

### 3.2.5 `pois` — 监控 POI 配置

```sql
CREATE TABLE pois (
    poi_id          TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    tb_cn           TEXT,
    h5_url          TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1, -- 0 / 1；用户可关闭某 POI 的监控
    polling_sec     INTEGER NOT NULL DEFAULT 1800, -- 30 min
    last_scanned_at TEXT,
    last_status     TEXT,                        -- 'success' / 'failed' / 'skipped'
    last_error      TEXT,
    cells_avg       INTEGER,                     -- 历史平均 cells 数（健康基线）
    created_at      TEXT NOT NULL
);

-- 启动时从 data/poi_registry.json 导入
INSERT INTO pois (poi_id, name, tb_cn, h5_url, created_at) VALUES
    ('1345', '圆明园', 'h.8j2xUJ7', 'https://...', datetime('now')),
    ...
;
```

### 3.2.6 `config` — 键值配置

```sql
CREATE TABLE config (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,               -- JSON-encoded
    updated_at      TEXT NOT NULL
);

-- 初始化
INSERT INTO config VALUES
    ('admin_password_hash', '"$2b$12$...bcrypt...',   datetime('now')),  -- xuran888 的 bcrypt
    ('webhook_url',         'null',                   datetime('now')),
    ('webhook_secret',      'null',                   datetime('now')),  -- HMAC 签名用
    ('webhook_rules',       '{"non_self_new":true,"price_alert":true,"self_missing":true,"first_seller":false,"shelf_error":false}', datetime('now')),
    ('webhook_recent',      '[]',                     datetime('now')),  -- 最近 20 条推送结果
    ('login_attempts',      '0',                      datetime('now')),  -- 当前 IP 失败计数
    ('login_locked_until',  'null',                   datetime('now')),  -- ISO 时间戳
    ('self_seller_id',      '"2217592322543"',        datetime('now')),
    ('self_seller_name',    '"北京旭冉假期旅游专营店"', datetime('now')),
    ('site_name',           '"飞猪哨兵"',              datetime('now')),
    ('last_global_scan',    'null',                   datetime('now')),
    ('site_timezone',       '"Asia/Shanghai"',        datetime('now'));
```

### 3.2.7 `web_sessions` — Dashboard 登录 session（服务端 session store）

> Session-based auth 的服务端存储。详见 [01-architecture.md §1.7](01-architecture.md)。

```sql
CREATE TABLE web_sessions (
    sid         TEXT PRIMARY KEY,                -- secrets.token_urlsafe(32)，256 bit 熵
    created_at  TEXT NOT NULL,                   -- UTC ISO 8601
    last_seen_at TEXT NOT NULL,                  -- 滑动续期
    expires_at  TEXT NOT NULL,                   -- 默认 created_at + 7d
    user_agent  TEXT,                            -- 仅 family: "Chrome"/"Safari"/"Firefox"/"curl"（不存完整 UA）
    ip_prefix   TEXT NOT NULL,                   -- /24 段（例 "107.172.144"），不存完整 IP
    is_active   INTEGER NOT NULL DEFAULT 1      -- 0=已登出/作废；保留行 7d 后清理
);

CREATE INDEX idx_web_sessions_expires ON web_sessions(expires_at);
CREATE INDEX idx_web_sessions_active  ON web_sessions(is_active);
CREATE INDEX idx_web_sessions_ip      ON web_sessions(ip_prefix, last_seen_at DESC);
```

#### 字段语义

- `sid`：32 字符 URL-safe random（`secrets.token_urlsafe(32)`），256 bit 熵。浏览器 cookie 里只存 sid；查 DB 决定是否登录
- `ip_prefix`：IPv4 `/24` 前缀（前 3 段）。**不**存完整 IP——日志/备份泄露隐私
- `user_agent`：只记浏览器 family（解析 UA 第一段：`Mozilla/5.0 (...)` → 取括号里的 `.../X.Y`），不存完整 UA（隐私 + 日志最小化）
- `is_active=0`：登出、改密码触发、运维手动清除
- `expires_at`：硬过期；超过即拒绝（即便 `is_active=1`）

#### 滑动续期

每次成功请求若 `last_seen_at < now - 1h` → `last_seen_at=now, expires_at=now+7d`（避免长时间盯屏突然被踢）。

#### 清理

```sql
-- 每日 job：清过期 + 登出 7 天前的行
DELETE FROM web_sessions
WHERE (expires_at < datetime('now')) OR
      (is_active = 0 AND last_seen_at < datetime('now', '-7 days'));
```

### 3.2.8 `login_failures` — 登录失败计数（IP 维度，限流）

```sql
CREATE TABLE login_failures (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ip_prefix   TEXT NOT NULL,                   -- /24 段
    ts          TEXT NOT NULL,                   -- UTC ISO
    user_agent_family TEXT,                      -- 浏览器 family
    reason      TEXT                             -- 'wrong_password' / 'locked' / 'malformed'
);

CREATE INDEX idx_login_fail_ip_ts ON login_failures(ip_prefix, ts DESC);
```

#### 判定逻辑（每次 POST /login 入口）

```sql
SELECT COUNT(*) FROM login_failures
WHERE ip_prefix = ? AND ts > datetime('now', '-10 minutes');
```

- `>= 5` → 拒绝，返回 429 + 「请 10 min 后重试」
- 成功登录 → `DELETE FROM login_failures WHERE ip_prefix=?`

#### 清理

```sql
-- 24h 前的失败记录清掉（防止表无限增长）
DELETE FROM login_failures WHERE ts < datetime('now', '-24 hours');
```

### 3.2.9 `alerts` — 告警事件流（追加）

```sql
CREATE TABLE alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,               -- 告警触发时间
    round_id        INTEGER,                     -- 关联的 round
    type            TEXT NOT NULL,               -- 'non_self_new' / 'price_alert' / 'self_missing' / 'first_seller' / 'shelf_error' / 'cookie_refresh_failed'
    severity        TEXT NOT NULL,               -- 'info' / 'warning' / 'critical'
    poi_id          TEXT,
    poi_name        TEXT,
    item_id         TEXT,
    sku_id          TEXT,
    seller_id       TEXT,
    payload         TEXT NOT NULL,               -- JSON: 完整 payload
    dedup_key       TEXT NOT NULL,               -- sha1(type+poi+item+sku+seller+window)
    webhook_status  TEXT,                        -- NULL / 'pending' / 'sent' / 'failed'
    webhook_sent_at TEXT,
    webhook_resp    TEXT,                        -- 推送返回的 response 摘要
    webhook_retry   INTEGER DEFAULT 0,
    UNIQUE(dedup_key)                            -- 自然去重
);

CREATE INDEX idx_alerts_ts      ON alerts(ts DESC);
CREATE INDEX idx_alerts_type    ON alerts(type);
CREATE INDEX idx_alerts_poi     ON alerts(poi_id);
CREATE INDEX idx_alerts_severity ON alerts(severity);
CREATE INDEX idx_alerts_status  ON alerts(webhook_status);
```

#### 告警去重窗口（dedup_key 生成）

```python
import hashlib
def dedup_key(alert_type: str, poi_id: str, item_id: str = "", sku_id: str = "",
              seller_id: str = "", window_hour: int = 1) -> str:
    """window_hour = 去重窗口小时数；同 dedup_key 在窗口内只发一次"""
    bucket = int(time.time()) // (window_hour * 3600)
    raw = f"{alert_type}|{poi_id}|{item_id}|{sku_id}|{seller_id}|{bucket}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]
```

#### `webhook_status` 状态机

```
INSERT → 'pending'
   ↓
POST webhook 成功 (HTTP 2xx) → 'sent'
   ↓ 失败重试 ≤3 次
   ↓ 最终失败 → 'failed'
```

### 3.2.10 `cookies_history` — Cookie 续期记录（只追加，用于审计）

```sql
CREATE TABLE cookies_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    token_prefix    TEXT NOT NULL,               -- _m_h5_tk 前 8 字符（不存完整 token）
    expiry_ts       TEXT,                        -- _m_h5_tk 的 _expiry 部分
    source          TEXT,                        -- 'playwright' / 'manual'
    success         INTEGER NOT NULL,
    error_msg       TEXT
);

CREATE INDEX idx_cookies_ts ON cookies_history(ts DESC);
```

**为什么只存 prefix 不存完整**：cookie 是高度敏感凭据，DB 备份 / 文件传输都可能泄露。token 前 8 字符足够排重 / 审计。

---

## 3.3 启动 SQL（`scripts/init_db.py` 输出）

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
PRAGMA temp_store = MEMORY;
```

WAL 模式下，监控脚本（写）和 web（读）并发不互锁。

---

## 3.4 数据生命周期 / 保留策略

| 表 | 保留期 | 清理策略 |
|---|---|---|
| `rounds` | 永久 | 不删；用于趋势分析 |
| `cells_snapshot` | 90 天 | 90 天前每天 cron 删：`DELETE WHERE first_seen_at < datetime('now', '-90 days')` |
| `sellers` | 永久 | 不删（first_seen_at 用作"老卖家"vs"新卖家"判定） |
| `seller_enrichment` | 永久 | 用户手动管理；删除 = 清空关注标记 |
| `pois` | 永久 | 仅在用户禁用时改 `enabled=0` |
| `config` | 永久 | UPSERT |
| `web_sessions` | 7 天（过期/登出后） | 每日清过期 + 已登出 7 天前的行 |
| `login_failures` | 24 小时 | 滚动删除 |
| `alerts` | 永久 | 不删；超过 1 年归档到 `alerts_archive.jsonl` |
| `cookies_history` | 30 天 | 滚动删除 |

```sql
-- 每日清理（在 fliggy-monitor.service 里也加一份 idempotent）
DELETE FROM cells_snapshot
WHERE id IN (
  SELECT id FROM cells_snapshot
  WHERE first_seen_at < datetime('now', '-90 days')
  LIMIT 10000
);

DELETE FROM web_sessions
WHERE expires_at < datetime('now')
   OR (is_active = 0 AND last_seen_at < datetime('now', '-7 days'));

DELETE FROM login_failures WHERE ts < datetime('now', '-24 hours');

DELETE FROM cookies_history WHERE ts < datetime('now', '-30 days');
```

---

## 3.5 典型查询示例

#### 1. 当前所有非自营 cell（最新轮）

```sql
SELECT c.*, s.seller_name, s.shop_jump_url, s.service_stats
FROM cells_snapshot c
JOIN rounds r ON c.round_id = r.id
LEFT JOIN sellers s ON c.seller_id = s.seller_id
WHERE r.id = (SELECT id FROM rounds ORDER BY started_at DESC LIMIT 1)
  AND c.is_self = 0
ORDER BY c.poi_id, c.sku_id;
```

#### 2. 单 SKU 的历史价格

```sql
SELECT r.started_at, c.price_int, c.price_dec
FROM cells_snapshot c
JOIN rounds r ON c.round_id = r.id
WHERE c.poi_id = '1345' AND c.item_id = '1065739764221' AND c.sku_id = '6276363111198'
ORDER BY r.started_at DESC
LIMIT 30;
```

#### 3. 过去 7 天告警类型分布

```sql
SELECT type, severity, COUNT(*) AS cnt
FROM alerts
WHERE ts > datetime('now', '-7 days')
GROUP BY type, severity
ORDER BY cnt DESC;
```

#### 4. 卖家维度：哪个外部商家出现最频繁

```sql
SELECT seller_id, seller_name, COUNT(DISTINCT poi_id) AS poi_count,
       COUNT(*) AS cell_count, MAX(ts) AS last_seen
FROM cells_snapshot c
JOIN rounds r ON c.round_id = r.id
LEFT JOIN sellers s ON c.seller_id = s.seller_id
WHERE c.is_self = 0
  AND r.started_at > datetime('now', '-7 days')
GROUP BY seller_id
ORDER BY cell_count DESC
LIMIT 20;
```

#### 5. 每 POI 的非自营率

```sql
SELECT c.poi_id, c.poi_name,
       COUNT(*) AS total,
       SUM(CASE WHEN is_self=0 THEN 1 ELSE 0 END) AS non_self
FROM cells_snapshot c
JOIN rounds r ON c.round_id = r.id
WHERE r.id = (SELECT id FROM rounds ORDER BY started_at DESC LIMIT 1)
GROUP BY c.poi_id;
```

---

## 3.6 数据迁移 / 升级路径

```bash
# 第一次部署：建库
python3 scripts/init_db.py

# 升级：保留旧数据，加新列（SQLite ALTER TABLE 限制：用新表 rename swap）
python3 scripts/migrate_db.py --to v2

# 全量备份（每日 cron）
cp /opt/fliggy-monitor/data/monitor.db \
   /opt/fliggy-monitor/backups/monitor-$(date +%Y%m%d).db
sqlite3 /opt/fliggy-monitor/data/monitor.db ".backup /opt/fliggy-monitor/backups/monitor.db"
```

保留最近 30 天备份即可。

---

## 3.7 ER 关系（简图）

```
┌──────────┐ 1     N ┌──────────────────┐ N   1 ┌────────┐
│  rounds  │────────►│ cells_snapshot   │──────►│sellers │
└──────────┘         └──────────────────┘       └────────┘
                            │ N
                            │
                            ▼ 1
                       ┌──────────┐
                       │  alerts  │ (N:1 也可关联 rounds)
                       └──────────┘

┌──────────┐
│   pois   │ ←── 用户配置（哪些 POI 启用）
└──────────┘

┌──────────┐
│  config  │ ←── 全局配置（webhook / 密码 / 自营 ID）
└──────────┘
```

---

## 3.8 接下来

- 实现 `scripts/init_db.py`：建表 + 导入 `data/*.json`
- 实现 `web/db.py`：sqlite3 封装（连接池 / row_factory / 事务）
- 在 `code/fliggy_monitor.py` 加 SQLite writer：每 cell 写 `cells_snapshot` + `sellers` upsert
- 在 `code/fliggy_monitor.py` 加 alert generator：本轮 diff → `alerts` 表 → 触发 webhook