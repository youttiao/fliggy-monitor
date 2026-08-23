#!/usr/bin/env python3
"""初始化数据库：建表 + 导入 baseline 数据 + 写默认 config。

幂等：可重复运行。已有数据保留，只补缺失的 baseline + seed。

用法：
    python3 scripts/init_db.py                    # 默认路径
    python3 scripts/init_db.py --db /tmp/foo.db    # 指定路径
    python3 scripts/init_db.py --import-baseline   # 从 data/*.json 导入
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# 允许从项目根或 web/ 任意位置运行
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from web.db import connect, execute, set_config, transaction  # noqa: E402

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
PRAGMA temp_store = MEMORY;

CREATE TABLE IF NOT EXISTS rounds (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id        TEXT UNIQUE NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'running',
    cells_total     INTEGER DEFAULT 0,
    cells_self      INTEGER DEFAULT 0,
    cells_non_self  INTEGER DEFAULT 0,
    new_sellers     INTEGER DEFAULT 0,
    booktips_hits   INTEGER DEFAULT 0,
    error_msg       TEXT,
    duration_ms     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_rounds_started ON rounds(started_at DESC);

CREATE TABLE IF NOT EXISTS cells_snapshot (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id        INTEGER NOT NULL,
    poi_id          TEXT NOT NULL,
    poi_name        TEXT NOT NULL,
    item_id         TEXT NOT NULL,
    sku_id          TEXT NOT NULL,
    cell_type       TEXT,
    sku_name        TEXT,
    price_int       TEXT,
    price_dec       TEXT,
    price_suffix    TEXT,
    sold            TEXT,
    seller_id       TEXT NOT NULL,
    is_self         INTEGER NOT NULL,
    raw_shelf       TEXT,
    first_seen_at   TEXT NOT NULL,
    FOREIGN KEY (round_id) REFERENCES rounds(id) ON DELETE CASCADE,
    UNIQUE(round_id, poi_id, item_id, sku_id)
);
CREATE INDEX IF NOT EXISTS idx_cells_round    ON cells_snapshot(round_id);
CREATE INDEX IF NOT EXISTS idx_cells_poi      ON cells_snapshot(poi_id);
CREATE INDEX IF NOT EXISTS idx_cells_seller   ON cells_snapshot(seller_id);
CREATE INDEX IF NOT EXISTS idx_cells_item     ON cells_snapshot(item_id);
CREATE INDEX IF NOT EXISTS idx_cells_first    ON cells_snapshot(first_seen_at);
CREATE INDEX IF NOT EXISTS idx_cells_is_self  ON cells_snapshot(is_self);

CREATE TABLE IF NOT EXISTS sellers (
    seller_id       TEXT PRIMARY KEY,
    seller_name     TEXT,
    seller_icon     TEXT,
    shop_jump_url   TEXT,
    service_stats   TEXT,
    is_self         INTEGER NOT NULL DEFAULT 0,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    total_cells     INTEGER DEFAULT 0,
    booktips_refreshed_at TEXT,
    booktips_raw    TEXT
);
CREATE INDEX IF NOT EXISTS idx_sellers_self   ON sellers(is_self);
CREATE INDEX IF NOT EXISTS idx_sellers_last   ON sellers(last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_sellers_name   ON sellers(seller_name);

CREATE TABLE IF NOT EXISTS seller_enrichment (
    seller_id       TEXT PRIMARY KEY,
    display_name    TEXT,
    is_watched      INTEGER NOT NULL DEFAULT 0,
    notes           TEXT,
    tags            TEXT,
    priority        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    created_by      TEXT,
    FOREIGN KEY (seller_id) REFERENCES sellers(seller_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_seller_enrich_watched ON seller_enrichment(is_watched);
CREATE INDEX IF NOT EXISTS idx_seller_enrich_priority ON seller_enrichment(priority DESC);

CREATE TABLE IF NOT EXISTS pois (
    poi_id          TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    tb_cn           TEXT,
    h5_url          TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1,
    polling_sec     INTEGER NOT NULL DEFAULT 1800,
    last_scanned_at TEXT,
    last_status     TEXT,
    last_error      TEXT,
    cells_avg       INTEGER,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS config (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS web_sessions (
    sid         TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    user_agent  TEXT,
    ip_prefix   TEXT NOT NULL,
    is_active   INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_web_sessions_expires ON web_sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_web_sessions_active  ON web_sessions(is_active);
CREATE INDEX IF NOT EXISTS idx_web_sessions_ip      ON web_sessions(ip_prefix, last_seen_at DESC);

CREATE TABLE IF NOT EXISTS login_failures (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ip_prefix   TEXT NOT NULL,
    ts          TEXT NOT NULL,
    user_agent_family TEXT,
    reason      TEXT
);
CREATE INDEX IF NOT EXISTS idx_login_fail_ip_ts ON login_failures(ip_prefix, ts DESC);

CREATE TABLE IF NOT EXISTS alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    round_id        INTEGER,
    type            TEXT NOT NULL,
    severity        TEXT NOT NULL,
    poi_id          TEXT,
    poi_name        TEXT,
    item_id         TEXT,
    sku_id          TEXT,
    seller_id       TEXT,
    payload         TEXT NOT NULL,
    dedup_key       TEXT NOT NULL,
    webhook_status  TEXT,
    webhook_sent_at TEXT,
    webhook_resp    TEXT,
    webhook_retry   INTEGER DEFAULT 0,
    UNIQUE(dedup_key)
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts      ON alerts(ts DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_type    ON alerts(type);
CREATE INDEX IF NOT EXISTS idx_alerts_poi     ON alerts(poi_id);
CREATE INDEX IF NOT EXISTS idx_alerts_severity ON alerts(severity);
CREATE INDEX IF NOT EXISTS idx_alerts_status  ON alerts(webhook_status);

CREATE TABLE IF NOT EXISTS cookies_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    token_prefix    TEXT NOT NULL,
    expiry_ts       TEXT,
    source          TEXT,
    success         INTEGER NOT NULL,
    error_msg       TEXT
);
CREATE INDEX IF NOT EXISTS idx_cookies_ts ON cookies_history(ts DESC);

-- 触发器：cells_snapshot 写入后自动 upsert sellers
CREATE TRIGGER IF NOT EXISTS trg_seller_upsert
AFTER INSERT ON cells_snapshot
BEGIN
    INSERT INTO sellers (seller_id, first_seen_at, last_seen_at, total_cells, is_self)
    VALUES (NEW.seller_id, NEW.first_seen_at, NEW.first_seen_at, 1, NEW.is_self)
    ON CONFLICT(seller_id) DO UPDATE SET
        last_seen_at = NEW.first_seen_at,
        total_cells  = total_cells + 1,
        is_self      = NEW.is_self;
END;
"""

DEFAULT_CONFIG = {
    "webhook_url": "null",  # 用户在 /settings 页面填
    "webhook_secret": "null",  # 钉钉加签 secret 或自定义 HMAC secret
    "webhook_platform": '"auto"',  # auto / dingtalk / feishu / custom
    "webhook_rules": json.dumps({
        "non_self_new": True,
        "price_alert": True,
        "self_missing": True,
        "first_seller": False,
        "shelf_error": True,
        "cookie_refresh_failed": True,
    }, ensure_ascii=False),
    "webhook_recent": "[]",
    "self_seller_id": '"2217592322543"',
    "self_seller_name": '"北京旭冉假期旅游专营店"',
    "site_name": '"飞猪哨兵"',
    "site_timezone": '"Asia/Shanghai"',
    "last_global_scan": "null",
    "polling_sec": "1800",  # 30 min
}


def seed_config(conn) -> int:
    """写入默认 config（如已有则保留，仅补缺失键）。"""
    n = 0
    for k, v in DEFAULT_CONFIG.items():
        row = execute(conn, "SELECT 1 FROM config WHERE key = ?", (k,)).fetchone()
        if not row:
            set_config(conn, k, json.loads(v) if v.startswith('"') or v.startswith('[') or v.startswith('{') else v)
            n += 1
    return n


def import_seller_baseline(conn, json_path: Path) -> int:
    """从 data/seller_cache.json 导入 baseline sellers。

    实际格式：{"meta": ..., "sellers": {sellerId: {sellerName, ...}, ...}}（dict-keyed）
    兼容旧版 list 格式。
    """
    if not json_path.exists():
        print(f"[skip] {json_path} 不存在，跳过 baseline 导入")
        return 0
    data = json.loads(json_path.read_text(encoding="utf-8"))
    sellers = data.get("sellers", [])
    if isinstance(sellers, dict):
        # 新格式：{seller_id: payload}；转 list
        sellers = list(sellers.values())
    if not isinstance(sellers, list) or not sellers:
        print(f"[skip] {json_path} 格式不像 seller 列表（type={type(sellers).__name__}）")
        return 0

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    n = 0
    with transaction(conn):
        for s in sellers:
            sid = s.get("seller_id") or s.get("sellerId")
            if not sid:
                continue
            stats = s.get("service_stats") or s.get("serviceStats")
            execute(
                conn,
                """
                INSERT INTO sellers
                    (seller_id, seller_name, seller_icon, shop_jump_url, service_stats,
                     is_self, first_seen_at, last_seen_at, total_cells, booktips_refreshed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(seller_id) DO UPDATE SET
                    seller_name  = excluded.seller_name,
                    seller_icon  = excluded.seller_icon,
                    shop_jump_url= excluded.shop_jump_url,
                    service_stats= excluded.service_stats,
                    booktips_refreshed_at = excluded.booktips_refreshed_at
                """,
                (
                    sid,
                    s.get("seller_name") or s.get("sellerName"),
                    s.get("seller_icon") or s.get("sellerIcon"),
                    s.get("shop_jump_url") or s.get("shopJumpUrl"),
                    json.dumps(stats, ensure_ascii=False) if stats else None,
                    1 if sid == "2217592322543" else 0,
                    now, now, 0, now,
                ),
            )
            n += 1
    return n


def import_poi_registry(conn, json_path: Path) -> int:
    """从 JSON 或 YAML 风格的 POI 注册表导入。

    兼容：JSON 列表、JSON {pois:[...]}、JSON {pool:[...]}、以及 handover 的 YAML 风格。
    YAML 解析失败时退化到硬编码 8 POI（与 docs/05 + fliggy-vps-handover/data/poi_registry.json 一致）。
    """
    rows: list[dict] = []

    if json_path.exists():
        raw = json_path.read_text(encoding="utf-8")
        try:
            data = json.loads(raw)
            rows = (data.get("pool") or data.get("pois") or
                    (data if isinstance(data, list) else []))
        except json.JSONDecodeError:
            # 简易 YAML 子集解析（front-matter + `- key: value` 列表 + 顶层 `key: value`）
            rows = _parse_simple_yaml_pois(raw)

    if not rows:
        # 兜底：硬编码 8 POI（与 docs/05-deployment-vps.md 对齐）
        rows = [
            {"poiId": "1345", "name": "圆明园", "tbCn": "h.8j2xUJ7"},
            {"poiId": "12726", "name": "藏文化博物院", "tbCn": "h.89rvDZJ"},
            {"poiId": "1350", "name": "天坛公园", "tbCn": "h.8j2yERj"},
            {"poiId": "1338", "name": "北海公园", "tbCn": "h.89rwfVr"},
            {"poiId": "1341", "name": "景山公园", "tbCn": "h.88dC3tP"},
            {"poiId": "1355", "name": "颐和园", "tbCn": "h.89ry9T3"},
            {"poiId": "1331", "name": "雍和宫", "tbCn": "h.88dC6u6"},
            {"poiId": "1544", "name": "恭王府", "tbCn": "h.88dC5uP"},
        ]
        print(f"[fallback] {json_path} 不可解析，使用硬编码 8 POI")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    n = 0
    with transaction(conn):
        for p in rows:
            pid = str(p.get("poi_id") or p.get("poiId"))
            if not pid:
                continue
            execute(
                conn,
                """
                INSERT INTO pois (poi_id, name, tb_cn, h5_url, enabled, polling_sec, created_at)
                VALUES (?, ?, ?, ?, 1, 1800, ?)
                ON CONFLICT(poi_id) DO UPDATE SET
                    name = excluded.name,
                    tb_cn = excluded.tb_cn,
                    h5_url = excluded.h5_url
                """,
                (
                    pid,
                    p.get("name") or p.get("poi_name") or "",
                    p.get("tb_cn") or p.get("tbCn"),
                    p.get("h5_url") or p.get("h5Url"),
                    now,
                ),
            )
            n += 1
    return n


def _parse_simple_yaml_pois(text: str) -> list[dict]:
    """只解析 POI 注册表这种简单 YAML：
    ---
    key: value
    pool:
      - poiId: "1345"
        name: 圆明园
        tb_cn: h.xxxx
    """
    rows: list[dict] = []
    in_pool = False
    cur: dict[str, str] | None = None
    for line in text.splitlines():
        s = line.rstrip()
        if not s or s.startswith("---"):
            continue
        if s.startswith("pool:"):
            in_pool = True
            continue
        if not in_pool:
            continue
        if s.startswith("  - "):
            # 新 entry
            if cur:
                rows.append(cur)
            cur = {}
            kv = s[4:]
            if ":" in kv:
                k, v = kv.split(":", 1)
                cur[k.strip()] = v.strip().strip('"')
        elif s.startswith("    ") and cur is not None:
            kv = s.strip()
            if ":" in kv:
                k, v = kv.split(":", 1)
                cur[k.strip()] = v.strip().strip('"')
        else:
            # 退出 pool（顶层 key 又开始）
            in_pool = False
    if cur:
        rows.append(cur)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="初始化飞猪哨兵数据库")
    parser.add_argument("--db", default=os.getenv("FLIGGY_DB", "/opt/fliggy-monitor/data/monitor.db"),
                        help="DB 文件路径")
    parser.add_argument("--no-baseline", action="store_true", help="不导入 baseline 数据")
    parser.add_argument("--data-dir", default=str(ROOT / "data"), help="data/ 目录路径")
    args = parser.parse_args()

    print(f"[init_db] DB: {args.db}")
    conn = connect(args.db)
    try:
        print("[init_db] 写入 schema…")
        conn.executescript(SCHEMA_SQL)

        print("[init_db] 写入默认 config…")
        n_cfg = seed_config(conn)
        print(f"  → 新增 {n_cfg} 个 config 项")

        if not args.no_baseline:
            data_dir = Path(args.data_dir)
            n_seller = import_seller_baseline(conn, data_dir / "seller_cache.json")
            n_poi = import_poi_registry(conn, data_dir / "poi_registry.json")
            print(f"  → baseline sellers: {n_seller}, POI: {n_poi}")

        # 列出所有表
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        print(f"[init_db] 完成。共 {len(rows)} 张表：{', '.join(r[0] for r in rows)}")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())