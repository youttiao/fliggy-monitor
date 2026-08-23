"""Fliggy 监控主循环 — Phase 3 增强版。

相比 vps-handover 模板，关键变化：
1. **写 SQLite**：每轮把 cells → cells_snapshot（trigger 自动 upsert sellers）
2. **生成 alerts**：diff 当前轮 vs 上一轮 → 非自营新增 / 价格异动 / 自营缺位
3. **推 webhook**：feishu / dingtalk 通过 web.notifier.WebhookSender
4. **booktips 增量**：只 hit cache miss 的 sellerId
5. **结构化 logging**：所有 print 走 logging，便于 journalctl 收

调度：
- 由 systemd timer 触发（每 30 min 一次）→ 跑完即退出
- 单进程单轮；并发由 timer 频率决定

调用：
    python3 -m code.fliggy_monitor             # 跑一轮然后退出
    python3 -m code.fliggy_monitor --loop      # 循环模式（调试用）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# 允许从项目根运行
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "code"))

from fliggy_selectors import SELF_SELLER_ID  # noqa: E402
from mtop_client import MtopClient, parse_seller_info, parse_ticket_cells  # noqa: E402

from web import notifier  # noqa: E402
from web.db import connect, execute, query, set_config, transaction  # noqa: E402

COOKIE_PATH = os.getenv("FLIGGY_COOKIES", "/etc/fliggy-monitor/cookies.json")
DB_PATH = os.getenv("FLIGGY_DB", "/opt/fliggy-monitor/data/monitor.db")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("fliggy_monitor")


# ── 数据加载 ────────────────────────────────────────────────────────


def load_cookies(path: str) -> dict[str, str]:
    """从 /etc/fliggy-monitor/cookies.json 读 cookies。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"cookies.json 不存在：{p}\n"
            "→ 用 scripts/refresh_cookies.py 刷新，或从浏览器 DevTools 抓"
        )
    raw = json.loads(p.read_text(encoding="utf-8"))
    # 支持两种格式：[{name,value}] 或 {name:value}
    if isinstance(raw, list):
        return {c["name"]: c["value"] for c in raw}
    return raw


def load_pois(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return query(conn, "SELECT * FROM pois WHERE enabled = 1 ORDER BY poi_id") or []


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def round_id_now() -> str:
    # round_id 是给人看的标识，用 Asia/Shanghai 让它和 dashboard 上显示的时间对得上人眼
    from zoneinfo import ZoneInfo
    return "r" + datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d%H%M")


# ── 一轮扫描 ────────────────────────────────────────────────────────


def scan_poi(
    conn: sqlite3.Connection, client: MtopClient, poi: sqlite3.Row, round_db_id: int
) -> tuple[list[sqlite3.Row], int]:
    """对一个 POI 跑 shelf，写 cells_snapshot；返回 (新插入的 cells, booktips_hits)。

    注意：触发器 trg_seller_upsert 会在 INSERT 后自动维护 sellers 表。
    """
    poi_id = poi["poi_id"]
    poi_name = poi["name"]
    started = now_utc_iso()

    raw = client.shelf(poi_id)
    cells = parse_ticket_cells(raw)
    log.info("poi=%s (%s) → %d cells", poi_name, poi_id, len(cells))

    new_rows: list[sqlite3.Row] = []
    with transaction(conn):
        for c in cells:
            sid = c["sellerId"]
            is_self = 1 if sid == SELF_SELLER_ID else 0

            # 拆分价格
            full_price = c["price"]  # e.g. "¥58起"
            price_int = ""
            price_dec = ""
            price_suffix = ""
            for ch in full_price:
                if ch.isdigit():
                    price_int += ch
                elif ch == "." or (ch.isdigit() is False and price_dec == "" and price_int):
                    price_dec += ch
            # 更稳：用正则
            import re
            m = re.search(r"(\d+)(?:\.(\d+))?(.*)$", full_price.replace("¥", ""))
            if m:
                price_int = m.group(1) or ""
                price_dec = "." + m.group(2) if m.group(2) else ""
                price_suffix = m.group(3) or ""

            cur = execute(
                conn,
                """
                INSERT INTO cells_snapshot
                    (round_id, poi_id, poi_name, item_id, sku_id, cell_type, sku_name,
                     price_int, price_dec, price_suffix, sold, seller_id, is_self,
                     raw_shelf, first_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(round_id, poi_id, item_id, sku_id) DO UPDATE SET
                    sku_name = excluded.sku_name,
                    price_int = excluded.price_int,
                    price_dec = excluded.price_dec,
                    price_suffix = excluded.price_suffix,
                    sold = excluded.sold,
                    seller_id = excluded.seller_id,
                    is_self = excluded.is_self
                """,
                (
                    round_db_id, poi_id, poi_name,
                    c["itemId"], c["skuId"], c.get("cellType"),
                    c["name"], price_int, price_dec, price_suffix,
                    c.get("sold"), sid, is_self,
                    json.dumps(raw.get("data", {}).get("result", {}).get("data", {}))[:8000] or None,
                    started,
                ),
            )
            # 是否本 cell 在本轮首次出现（first_seen_at == started）
            row = query(
                conn,
                "SELECT * FROM cells_snapshot WHERE id = ?",
                (cur.lastrowid,),
                one=True,
            )
            if row and row["first_seen_at"] == started:
                new_rows.append(row)

        # 更新 poi 元数据
        execute(
            conn,
            "UPDATE pois SET last_scanned_at = ?, last_status = 'success', last_error = NULL WHERE poi_id = ?",
            (started, poi_id),
        )

    return new_rows, 0


def backfill_sellers(conn: sqlite3.Connection, client: MtopClient,
                     cells: list[sqlite3.Row]) -> int:
    """对 cache miss 的 sellerId 拉一次 booktips → 更新 sellers 表。"""
    miss = query(
        conn,
        """
        SELECT s.seller_id, c.item_id, c.sku_id, c.poi_id
        FROM sellers s
        JOIN cells_snapshot c ON c.seller_id = s.seller_id
        WHERE s.seller_name IS NULL OR s.seller_name = ''
        GROUP BY s.seller_id
        ORDER BY c.first_seen_at DESC
        LIMIT 50
        """,
    ) or []
    hits = 0
    for m in miss:
        try:
            raw = client.booktips(m["item_id"], m["sku_id"], m["poi_id"])
            info = parse_seller_info(raw)
            if not info:
                continue
            with transaction(conn):
                execute(
                    conn,
                    """
                    UPDATE sellers
                    SET seller_name = ?, seller_icon = ?, shop_jump_url = ?,
                        service_stats = ?, booktips_refreshed_at = ?, booktips_raw = ?
                    WHERE seller_id = ?
                    """,
                    (
                        info["sellerName"], info["sellerIcon"], info["shopJumpUrl"],
                        json.dumps(info["serviceStats"], ensure_ascii=False),
                        now_utc_iso(),
                        json.dumps(info, ensure_ascii=False)[:8000] or None,
                        m["seller_id"],
                    ),
                )
            hits += 1
            log.info("booktips hit sid=%s → %s", m["seller_id"], info["sellerName"])
        except Exception as e:
            log.warning("booktips fail sid=%s: %s", m["seller_id"], e)
            continue
    return hits


# ── 告警生成 ────────────────────────────────────────────────────────


def dedup_key(alert_type: str, *parts: str, window_hour: int = 1) -> str:
    bucket = int(time.time()) // (window_hour * 3600)
    raw = f"{alert_type}|" + "|".join(parts) + f"|{bucket}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def detect_alerts(conn: sqlite3.Connection, cur_round_id: int,
                  prev_round_id: int | None) -> list[dict]:
    """diff 本轮 vs 上一轮 → 3 类告警。"""
    alerts: list[dict] = []

    # 1. non_self_new：本轮首次出现 + 非自营 + 在最近 30 天内从未出现
    rows = query(
        conn,
        """
        SELECT c.*, s.seller_name, e.display_name AS enrichment_name,
               e.is_watched AS watched
        FROM cells_snapshot c
        LEFT JOIN sellers s ON s.seller_id = c.seller_id
        LEFT JOIN seller_enrichment e ON e.seller_id = c.seller_id
        WHERE c.round_id = ?
          AND c.is_self = 0
          AND c.first_seen_at = (SELECT started_at FROM rounds WHERE id = ?)
        """,
        (cur_round_id, cur_round_id),
    ) or []
    for r in rows:
        seller_display = r["enrichment_name"] or r["seller_name"] or (r["seller_id"][:6] + "…")
        key = dedup_key("non_self_new", r["poi_id"], r["item_id"], r["sku_id"], r["seller_id"])
        severity = "warning" if r["watched"] else "info"
        alerts.append({
            "type": "non_self_new",
            "severity": severity,
            "ts": now_utc_iso(),
            "round_id": cur_round_id,
            "poi_id": r["poi_id"],
            "poi_name": r["poi_name"],
            "item_id": r["item_id"],
            "sku_id": r["sku_id"],
            "seller_id": r["seller_id"],
            "seller_display": seller_display,
            "watched": bool(r["watched"]),
            "price_int": r["price_int"],
            "price_dec": r["price_dec"],
            "price_suffix": r["price_suffix"],
            "dedup_key": key,
            "payload": {
                "type": "non_self_new", "severity": severity,
                "poi_id": r["poi_id"], "poi_name": r["poi_name"],
                "sku_name": r["sku_name"],
                "seller_id": r["seller_id"], "seller_display": seller_display,
                "price_int": r["price_int"], "price_dec": r["price_dec"],
                "price_suffix": r["price_suffix"], "watched": bool(r["watched"]),
            },
        })

    # 2. price_alert：同 (poi, item, sku)，本轮与上轮价格不同
    if prev_round_id:
        rows = query(
            conn,
            """
            SELECT cur.*, prev.price_int AS prev_int, prev.price_dec AS prev_dec,
                   s.seller_name, e.display_name AS enrichment_name,
                   e.is_watched AS watched
            FROM cells_snapshot cur
            JOIN cells_snapshot prev
              ON prev.poi_id = cur.poi_id AND prev.item_id = cur.item_id
             AND prev.sku_id = cur.sku_id AND prev.round_id = ?
            LEFT JOIN sellers s ON s.seller_id = cur.seller_id
            LEFT JOIN seller_enrichment e ON e.seller_id = cur.seller_id
            WHERE cur.round_id = ?
              AND (cur.price_int != prev.price_int OR cur.price_dec != prev.price_dec)
            """,
            (prev_round_id, cur_round_id),
        ) or []
        for r in rows:
            seller_display = r["enrichment_name"] or r["seller_name"] or (r["seller_id"][:6] + "…")
            key = dedup_key("price_alert", r["poi_id"], r["item_id"], r["sku_id"],
                            r["price_int"], r["price_dec"])
            alerts.append({
                "type": "price_alert",
                "severity": "warning" if r["watched"] else "info",
                "ts": now_utc_iso(),
                "round_id": cur_round_id,
                "poi_id": r["poi_id"],
                "poi_name": r["poi_name"],
                "item_id": r["item_id"],
                "sku_id": r["sku_id"],
                "seller_id": r["seller_id"],
                "seller_display": seller_display,
                "watched": bool(r["watched"]),
                "price_int": r["price_int"],
                "price_dec": r["price_dec"],
                "price_suffix": r["price_suffix"],
                "dedup_key": key,
                "payload": {
                    "type": "price_alert", "severity": "warning" if r["watched"] else "info",
                    "poi_id": r["poi_id"], "poi_name": r["poi_name"],
                    "sku_name": r["sku_name"],
                    "seller_id": r["seller_id"], "seller_display": seller_display,
                    "price_int": r["price_int"], "price_dec": r["price_dec"],
                    "price_suffix": r["price_suffix"],
                    "prev_price": f"{r['prev_int']}{r['prev_dec']}",
                    "watched": bool(r["watched"]),
                },
            })

    # 3. self_missing：上一轮是自营，本轮不是自营 / 没了
    if prev_round_id:
        rows = query(
            conn,
            """
            SELECT prev.poi_id, prev.poi_name, prev.item_id, prev.sku_id,
                   cur.is_self AS cur_is_self, cur.seller_id AS cur_seller
            FROM cells_snapshot prev
            LEFT JOIN cells_snapshot cur
              ON cur.poi_id = prev.poi_id AND cur.item_id = prev.item_id
             AND cur.sku_id = prev.sku_id AND cur.round_id = ?
            WHERE prev.round_id = ? AND prev.is_self = 1
              AND (cur.id IS NULL OR cur.is_self = 0)
            """,
            (cur_round_id, prev_round_id),
        ) or []
        for r in rows:
            key = dedup_key("self_missing", r["poi_id"], r["item_id"], r["sku_id"])
            alerts.append({
                "type": "self_missing",
                "severity": "critical",
                "ts": now_utc_iso(),
                "round_id": cur_round_id,
                "poi_id": r["poi_id"],
                "poi_name": r["poi_name"],
                "item_id": r["item_id"],
                "sku_id": r["sku_id"],
                "seller_id": r["cur_seller"],
                "seller_display": None,
                "watched": False,
                "dedup_key": key,
                "payload": {
                    "type": "self_missing", "severity": "critical",
                    "poi_id": r["poi_id"], "poi_name": r["poi_name"],
                    "item_id": r["item_id"], "sku_id": r["sku_id"],
                    "replaced_by": r["cur_seller"],
                },
            })

    return alerts


def persist_alerts(conn: sqlite3.Connection, alerts: list[dict]) -> list[dict]:
    """把 alerts 写入 alerts 表；返回真插入的（去重后）。"""
    inserted = []
    with transaction(conn):
        for a in alerts:
            cur = execute(
                conn,
                """
                INSERT INTO alerts
                    (ts, round_id, type, severity, poi_id, poi_name,
                     item_id, sku_id, seller_id, payload, dedup_key, webhook_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                ON CONFLICT(dedup_key) DO NOTHING
                """,
                (a["ts"], a["round_id"], a["type"], a["severity"],
                 a["poi_id"], a["poi_name"], a["item_id"], a["sku_id"], a["seller_id"],
                 json.dumps(a["payload"], ensure_ascii=False), a["dedup_key"]),
            )
            if cur.rowcount > 0:
                inserted.append(a)
    return inserted


def dispatch_webhooks(conn: sqlite3.Connection, alerts: list[dict],
                      dashboard_base: str | None) -> None:
    """按 ``webhook_mode`` config 派发。

    ``shelf_report``（默认）：每轮聚合一条报告；只在有关注 POI 出现非自营货架时推送；
    没有 watched shelf → 完全不推送。

    ``per_alert``（旧模式）：每条 alert 一条消息，受 ``webhook_rules`` + shelf 关注门控。
    """
    mode = _get_config(conn, "webhook_mode", "shelf_report")
    if mode == "per_alert":
        _dispatch_per_alert(conn, alerts, dashboard_base)
    else:
        _dispatch_shelf_report(conn, dashboard_base)


def _dispatch_per_alert(conn: sqlite3.Connection, alerts: list[dict],
                        dashboard_base: str | None) -> None:
    """旧行为：按 alert 推送。货架过滤：v1 默认行为是「所有告警按规则推」。从这一版起增加**货架级 gate**：
    只对 `shelf_watch.is_watched=1` 的 cell 推 webhook；
    且仅在该 cell 的当前供应商不是自营时推（self_missing 例外：货架消失/被替换即为非自营）。
    没标的货架继续入库 + 写 dashboard，但不骚扰 IM。
    """
    url = get_config(conn, "webhook_url")
    if not url or url == "null":
        log.debug("webhook 未配置，跳过推送")
        return
    secret = get_config(conn, "webhook_secret")
    rules = get_config(conn, "webhook_rules", {})
    sender = notifier.WebhookSender(
        url,
        secret=secret if secret and secret != "null" else None,
        platform=get_config(conn, "webhook_platform", "auto"),
    )

    cur_round = query(conn, "SELECT id FROM rounds ORDER BY id DESC LIMIT 1", one=True)
    cur_round_id = cur_round["id"] if cur_round else None

    for a in alerts:
        if not rules.get(a["type"], False):
            log.debug("规则关闭：type=%s", a["type"])
            continue

        # ── 货架级过滤 ──
        watched = query(
            conn,
            "SELECT 1 FROM shelf_watch WHERE poi_id=? AND item_id=? AND sku_id=? AND is_watched=1",
            (a["poi_id"], a["item_id"], a["sku_id"]),
            one=True,
        )
        if not watched:
            log.debug("货架未关注：poi=%s item=%s sku=%s",
                      a["poi_id"], a["item_id"][:8], a["sku_id"][:8])
            continue

        # self_missing 永远满足「供应商非自营」（已下 / 被换），直接推
        if a["type"] != "self_missing":
            cur_cell = query(
                conn,
                "SELECT is_self FROM cells_snapshot WHERE round_id=? AND poi_id=? AND item_id=? AND sku_id=?",
                (cur_round_id, a["poi_id"], a["item_id"], a["sku_id"]),
                one=True,
            )
            if cur_cell and cur_cell["is_self"]:
                log.debug("货架关注但供应商为自营，跳过：%s", a["type"])
                continue

        rendered = notifier.render_alert(
            alert_type=a["type"],
            severity=a["severity"],
            poi_id=a["poi_id"], poi_name=a["poi_name"],
            sku_name=a.get("payload", {}).get("sku_name"),
            seller_id=a["seller_id"], seller_display=a.get("seller_display"),
            watched=a.get("watched", False),
            price_int=a.get("price_int"), price_dec=a.get("price_dec"),
            price_suffix=a.get("price_suffix"),
            ts=a["ts"],
            dashboard_url=f"{dashboard_base}/poi/{a['poi_id']}" if dashboard_base else None,
        )
        result = sender.send(rendered)
        status = "sent" if result.ok else "failed"
        with transaction(conn):
            execute(
                conn,
                "UPDATE alerts SET webhook_status = ?, webhook_sent_at = ?, "
                "webhook_resp = ?, webhook_retry = webhook_retry + 1 "
                "WHERE dedup_key = ?",
                (status, now_utc_iso(), result.response[:500] or result.error, a["dedup_key"]),
            )
        log.info("webhook %s type=%s status=%s code=%s", status, a["type"],
                 result.status_code, result.response[:80])


# ── 货架聚合报告（每轮一条，节省 webhook 调用） ────────────────


def build_shelf_report(conn: sqlite3.Connection, round_id: int) -> dict | None:
    """聚合 round 里所有 watched + non-self cell，按 POI 分组；返回 report dict 或 None（无 watched）。"""
    rows = query(
        conn,
        """
        SELECT c.poi_id, c.poi_name, c.item_id, c.sku_id, c.sku_name,
               c.price_int, c.price_dec, c.price_suffix,
               c.is_self, c.seller_id, s.seller_name,
               e.display_name AS enrichment_name
        FROM cells_snapshot c
        JOIN shelf_watch w
          ON w.poi_id = c.poi_id AND w.item_id = c.item_id
         AND w.sku_id = c.sku_id AND w.is_watched = 1
        LEFT JOIN sellers s ON s.seller_id = c.seller_id
        LEFT JOIN seller_enrichment e ON e.seller_id = c.seller_id
        WHERE c.round_id = ?
        ORDER BY c.poi_id, c.is_self DESC
        """,
        (round_id,),
    ) or []
    if not rows:
        return None

    # 按 POI 分组
    by_poi: dict[str, dict] = {}
    for r in rows:
        d = dict(r)
        g = by_poi.setdefault(d["poi_id"], {
            "poi_id": d["poi_id"],
            "poi_name": d["poi_name"],
            "cells": [],
        })
        g["cells"].append(d)

    groups: list[dict] = []
    non_self_pois = 0
    for pid, info in by_poi.items():
        non_self = [c for c in info["cells"] if not c["is_self"]]
        self_cells = [c for c in info["cells"] if c["is_self"]]
        # 同一 (item, sku) 在一个 round 里只有一行（schema UNIQUE）
        # 但 cell_type 不同的话仍是同一行；直接以行为准
        shelves = []
        for c in non_self:
            shelves.append({
                "sku_name": c["sku_name"] or "—",
                "item_id": c["item_id"],
                "sku_id": c["sku_id"],
                "price_int": c["price_int"],
                "price_dec": c["price_dec"],
                "price_suffix": c["price_suffix"],
                "watched": True,
            })
        if non_self:
            non_self_pois += 1
            groups.append({
                "poi_id": pid,
                "poi_name": info["poi_name"],
                "has_non_self": True,
                "non_self_count": len(non_self),
                "self_count": len(self_cells),
                "shelves": shelves,
            })
        else:
            groups.append({
                "poi_id": pid,
                "poi_name": info["poi_name"],
                "has_non_self": False,
                "non_self_count": 0,
                "self_count": len(self_cells),
                "shelves": [],
            })

    # 非自营 POI 排前面
    groups.sort(key=lambda g: (not g["has_non_self"], g["poi_name"]))

    round_row = query(conn, "SELECT round_id, started_at FROM rounds WHERE id = ?",
                      (round_id,), one=True)
    return {
        "round_id": round_row["round_id"] if round_row else "",
        "ts": round_row["started_at"] if round_row else now_utc_iso(),
        "groups": groups,
        "total_pois": len(by_poi),
        "non_self_pois": non_self_pois,
    }


def _dispatch_shelf_report(conn: sqlite3.Connection, dashboard_base: str | None) -> None:
    """按 round 聚合一条报告并推送；只在 report 里有 non_self 时实际推送。"""
    url = _get_config(conn, "webhook_url", None)
    if not url or url == "null":
        log.debug("webhook 未配置，跳过报告推送")
        return

    cur_round = query(conn, "SELECT id, round_id, started_at FROM rounds ORDER BY id DESC LIMIT 1", one=True)
    if not cur_round:
        log.warning("无 rounds 记录，跳过报告")
        return

    report = build_shelf_report(conn, cur_round["id"])
    if not report:
        log.info("round=%s 没有任何 watched 货架，跳过报告推送", cur_round["round_id"])
        return
    if report["non_self_pois"] == 0:
        log.info("round=%s 关注的 %d 个 POI 全是自营，跳过报告推送",
                 cur_round["round_id"], report["total_pois"])
        return

    report["dashboard_url"] = dashboard_base.rstrip("/") if dashboard_base else None

    sender = notifier.WebhookSender(
        url,
        secret=_get_config(conn, "webhook_secret", None),
        platform=_get_config(conn, "webhook_platform", "auto"),
    )
    result = sender.send_report(report)
    status = "sent" if result.ok else "failed"
    dedup_key = f"shelf_report|{cur_round['round_id']}"
    resp_text = (result.response[:500] if result.response else None) or result.error
    sent_at = now_utc_iso()

    # 写一条聚合 alert 进 alerts 表（按 round 去重）
    payload = {
        "type": "shelf_report",
        "round_id": cur_round["round_id"],
        "total_pois": report["total_pois"],
        "non_self_pois": report["non_self_pois"],
        "groups": [
            {
                "poi_id": g["poi_id"], "poi_name": g["poi_name"],
                "has_non_self": g["has_non_self"],
                "non_self_count": g["non_self_count"],
                "self_count": g["self_count"],
                "shelves": g["shelves"],
            }
            for g in report["groups"]
        ],
    }
    with transaction(conn):
        execute(
            conn,
            """
            INSERT INTO alerts
                (ts, round_id, type, severity, poi_id, poi_name,
                 item_id, sku_id, seller_id, payload, dedup_key,
                 webhook_status, webhook_sent_at, webhook_resp, webhook_retry)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(dedup_key) DO UPDATE SET
                webhook_status = excluded.webhook_status,
                webhook_sent_at = excluded.webhook_sent_at,
                webhook_resp = excluded.webhook_resp,
                webhook_retry = alerts.webhook_retry + 1
            """,
            (
                report["ts"], cur_round["id"], "shelf_report",
                "warning" if report["non_self_pois"] else "info",
                None, None, None, None, None,
                json.dumps(payload, ensure_ascii=False),
                dedup_key,
                status, sent_at, resp_text,
            ),
        )
    log.info("shelf_report sent=%s code=%s total_pois=%d non_self_pois=%d",
             status, result.status_code, report["total_pois"], report["non_self_pois"])


def get_config(conn, key, default=None):
    return _get_config(conn, key, default)


# 内部 copy（避免循环 import）
def _get_config(conn, key, default):
    import json as _json
    row = query(conn, "SELECT value FROM config WHERE key = ?", (key,), one=True)
    if not row:
        return default
    try:
        return _json.loads(row["value"])
    except (_json.JSONDecodeError, TypeError):
        return default


# ── 主循环 ──────────────────────────────────────────────────────────


def run_one_round(conn: sqlite3.Connection, client: MtopClient,
                  dashboard_base: str | None = None) -> dict:
    """跑一轮：扫描全部 POI → 写 DB → 告警 → 推 webhook。返回统计。"""
    pois = load_pois(conn)
    if not pois:
        log.warning("pois 表为空，请先跑 scripts/init_db.py")
        return {"status": "failed", "reason": "no pois"}

    started = now_utc_iso()
    rid = round_id_now()
    cur = execute(
        conn,
        "INSERT INTO rounds (round_id, started_at, status) VALUES (?, ?, 'running')",
        (rid, started),
    )
    round_db_id = cur.lastrowid
    log.info("round start rid=%s db_id=%d pois=%d", rid, round_db_id, len(pois))

    prev = query(conn, "SELECT id FROM rounds WHERE id < ? ORDER BY id DESC LIMIT 1",
                 (round_db_id,), one=True)
    prev_id = prev["id"] if prev else None

    cells_total = 0
    cells_self = 0
    new_sellers = 0
    booktips_hits = 0
    error_summary: list[str] = []

    for poi in pois:
        try:
            new_rows, _ = scan_poi(conn, client, poi, round_db_id)
            new_sellers += len({r["seller_id"] for r in new_rows})
        except Exception as e:
            log.exception("poi %s failed: %s", poi["poi_id"], e)
            error_summary.append(f"{poi['poi_id']}: {type(e).__name__}")
            execute(
                conn,
                "UPDATE pois SET last_scanned_at = ?, last_status = 'failed', last_error = ? "
                "WHERE poi_id = ?",
                (now_utc_iso(), str(e)[:300], poi["poi_id"]),
            )
            continue

        # 抖动 200-800ms 防反爬
        time.sleep(0.2 + random.random() * 0.6)

    # 统计
    stats = query(
        conn,
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN is_self=1 THEN 1 ELSE 0 END) AS self_n, "
        "SUM(CASE WHEN is_self=0 THEN 1 ELSE 0 END) AS nonself_n "
        "FROM cells_snapshot WHERE round_id = ?",
        (round_db_id,),
        one=True,
    )
    if stats:
        cells_total = stats["total"] or 0
        cells_self = stats["self_n"] or 0

    # booktips 增量
    try:
        booktips_hits = backfill_sellers(conn, client, [])
    except Exception as e:
        log.warning("booktips pass failed: %s", e)

    # 告警
    webhook_mode = get_config(conn, "webhook_mode", "shelf_report")
    inserted: list = []  # shelf_report 分支不写 inserted，关 round 时用得到
    if webhook_mode == "per_alert":
        alerts = detect_alerts(conn, round_db_id, prev_id)
        inserted = persist_alerts(conn, alerts)
        log.info("alerts: %d generated, %d new (after dedup)", len(alerts), len(inserted))
        dispatch_webhooks(conn, inserted, dashboard_base)
    else:
        # shelf_report 模式：跳过 per-alert 检测与入库，只聚合推送
        log.info("webhook_mode=shelf_report，跳过 per-alert detect/persist")
        dispatch_webhooks(conn, [], dashboard_base)

    # 关 round
    finished = now_utc_iso()
    duration_ms = int((datetime.fromisoformat(finished) -
                       datetime.fromisoformat(started)).total_seconds() * 1000)
    status = "success" if not error_summary else ("partial" if cells_total else "failed")
    err_msg = "; ".join(error_summary)[:500] if error_summary else None
    execute(
        conn,
        """UPDATE rounds SET finished_at = ?, status = ?, cells_total = ?, cells_self = ?,
           cells_non_self = ?, new_sellers = ?, booktips_hits = ?, error_msg = ?, duration_ms = ?
           WHERE id = ?""",
        (finished, status, cells_total, cells_self,
         (cells_total - cells_self), new_sellers, booktips_hits, err_msg, duration_ms,
         round_db_id),
    )
    set_config(conn, "last_global_scan", finished)
    log.info("round done status=%s cells=%d self=%d alerts_new=%d duration=%dms",
             status, cells_total, cells_self, len(inserted), duration_ms)
    return {"status": status, "cells_total": cells_total, "cells_self": cells_self,
            "alerts_new": len(inserted), "duration_ms": duration_ms}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true", help="循环模式（调试用）")
    parser.add_argument("--interval", type=int, default=1800, help="--loop 模式间隔（秒）")
    parser.add_argument("--dashboard-base", default=os.getenv("DASHBOARD_BASE_URL"),
                        help="webhook 推送时附带的 Dashboard URL base")
    args = parser.parse_args()

    cookies = load_cookies(COOKIE_PATH)
    client = MtopClient(cookies=cookies)
    conn = connect(DB_PATH)
    try:
        if args.loop:
            while True:
                run_one_round(conn, client, args.dashboard_base)
                log.info("sleep %ds…", args.interval)
                time.sleep(args.interval)
        else:
            stats = run_one_round(conn, client, args.dashboard_base)
            return 0 if stats.get("status") != "failed" else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())