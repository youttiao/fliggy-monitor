#!/usr/bin/env python3
"""回填 cells_snapshot 的 price_int / price_dec / price_suffix。

背景：早期 `fliggy_monitor.scan_poi` 从字符串 ``c["price"]`` 正则解析价格，
而 ``c["price"]`` 是 ``{prefix}{integerPrice}{suffix}`` 的拼接，**漏掉了
decimalPrice** —— 所以所有历史行的 ``price_dec`` 都是空，价格永远显示成整数。

raw_shelf 字段（cells_snapshot.raw_shelf）存了完整的 ``data.result.data``，
里面 ``shelves[*].cells[*].priceStruct`` 三个字段都齐，足以还原。

用法：
    python3 scripts/backfill_price_dec.py                      # 默认 DB
    python3 scripts/backfill_price_dec.py /tmp/test_monitor.db # 显式路径
    python3 scripts/backfill_price_dec.py --dry-run            # 只统计，不写
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from web.db import connect, transaction  # noqa: E402

log = logging.getLogger("backfill_price_dec")


def find_price_in_shelf(raw: dict, poi_id: str, item_id: str, sku_id: str) -> dict | None:
    """从 raw_shelf (data.result.data) 里找对应 cell 的 priceStruct。

    返回 ``{"price_int", "price_dec", "price_suffix"}`` 或 None。
    """
    try:
        shelves = raw["shelf"]["shelves"]
    except (KeyError, TypeError):
        return None
    for shelf in shelves:
        if shelf.get("type") != "ScenicTicketType":
            continue
        cells = list(shelf.get("cells", []))
        for tab in shelf.get("tabs", []):
            cells.extend(tab.get("cells", []))
        for cell in cells:
            if (str(cell.get("itemId", "")) == item_id
                    and str(cell.get("skuId", "")) == sku_id
                    and str(cell.get("poiId", "")) == poi_id):
                ps = cell.get("priceStruct") or {}
                return {
                    "price_int": str(ps.get("integerPrice", "")),
                    "price_dec": str(ps.get("decimalPrice", "")),
                    "price_suffix": str(ps.get("priceSuffix", "")),
                }
    return None


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("db", nargs="?", default=None,
                   help="SQLite 路径；省略时用 web.db.DEFAULT_DB_PATH")
    p.add_argument("--dry-run", action="store_true", help="只统计，不写")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    conn = connect(args.db)
    # 找出 price_dec 为空的所有行（最严格的过滤；只要缺失就重写）
    rows = conn.execute(
        """
        SELECT id, poi_id, item_id, sku_id, raw_shelf,
               price_int AS old_int, price_dec AS old_dec, price_suffix AS old_suffix
        FROM cells_snapshot
        WHERE raw_shelf IS NOT NULL AND raw_shelf != ''
          AND (price_dec IS NULL OR price_dec = '')
        """
    ).fetchall()
    log.info("需要回填的行：%d", len(rows))

    fixed = 0
    unresolved = 0
    same_as_old = 0
    samples: list[tuple[int, str, str, str, str]] = []

    with transaction(conn):
        for r in rows:
            try:
                raw = json.loads(r["raw_shelf"])
            except (json.JSONDecodeError, TypeError):
                unresolved += 1
                continue
            hit = find_price_in_shelf(raw, r["poi_id"], r["item_id"], r["sku_id"])
            if not hit:
                unresolved += 1
                continue

            # price_dec 缺失就更新（保留可能已正确但缺的小数）；如果整数部分也对得上
            # 且原本就只是缺小数，就补 .00 也算修复（避免 .0 vs .00 不一致争议）
            new_dec = hit["price_dec"] or ""
            new_int = hit["price_int"] or r["old_int"] or ""
            new_suffix = hit["price_suffix"] or r["old_suffix"] or ""

            same = (new_int == (r["old_int"] or "")
                    and new_dec == (r["old_dec"] or "")
                    and new_suffix == (r["old_suffix"] or ""))
            if same:
                same_as_old += 1
                continue

            if args.dry_run:
                log.debug("DRY id=%s %s/%s → int=%r dec=%r suf=%r",
                          r["id"], r["item_id"][:8], r["sku_id"][:8],
                          new_int, new_dec, new_suffix)
            else:
                conn.execute(
                    """
                    UPDATE cells_snapshot
                    SET price_int = ?, price_dec = ?, price_suffix = ?
                    WHERE id = ?
                    """,
                    (new_int, new_dec, new_suffix, r["id"]),
                )
                fixed += 1
                if len(samples) < 5:
                    samples.append((r["id"], r["item_id"], r["sku_id"],
                                    f"{new_int}{new_dec}{new_suffix}",
                                    f"{r['old_int'] or ''}{r['old_dec'] or ''}{r['old_suffix'] or ''}"))

    log.info("修复：%d  已经是新值：%d  无法定位：%d",
             fixed, same_as_old, unresolved)
    if samples:
        log.info("示例：")
        for sid, iid, skid, nw, old in samples:
            log.info("  id=%s %s/%s  %r → %r", sid, iid[:8], skid[:8], old, nw)

    return 0


if __name__ == "__main__":
    sys.exit(main())