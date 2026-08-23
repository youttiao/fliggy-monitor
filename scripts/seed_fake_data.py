#!/usr/bin/env python3
"""为本地验证 UI 生成假数据：1 轮 cells + 几条 alerts。

不影响真实监控逻辑——只在 /tmp/test_monitor.db 里写。
"""
from __future__ import annotations

import json
import random
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB = sys.argv[1] if len(sys.argv) > 1 else "/tmp/test_monitor.db"
SELF_SELLER_ID = "2217592322543"

# 从 DB 拉所有 seller + POI
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA foreign_keys = ON")
sellers = [dict(r) for r in conn.execute("SELECT * FROM sellers").fetchall()]
pois = [dict(r) for r in conn.execute("SELECT * FROM pois WHERE enabled = 1").fetchall()]
print(f"[seed] {len(sellers)} sellers, {len(pois)} POIs")

CELL_TYPES = ["门票套餐", "景点门票", "园内项目", "景区联票", "周边景区门票", "周边景区套票"]
SKU_NAMES = {
    "门票套餐": ["成人电子票（含讲解）", "亲子套票（1大1小）", "家庭套票（2大1小）", "学生套票"],
    "景点门票": ["成人票", "学生票", "老人票", "儿童票", "军人优待票"],
    "园内项目": ["船票", "电瓶车票", "语音讲解", "全景游"],
    "景区联票": ["门票+讲解联票", "门票+船票联票", "双园联票"],
    "周边景区门票": ["联游门票", "一日游门票"],
    "周边景区套票": ["套票（含交通）", "二日联游套票"],
}

# ── 把 2 个非自营 seller 标为关注（演示 cyan 高亮）
watched = [s["seller_id"] for s in sellers if s["is_self"] == 0][:2]
for sid in watched:
    conn.execute(
        "INSERT INTO seller_enrichment (seller_id, display_name, is_watched, notes, tags, priority, created_at, updated_at, created_by) "
        "VALUES (?, ?, 1, ?, ?, 2, ?, ?, 'admin') "
        "ON CONFLICT(seller_id) DO UPDATE SET is_watched=1, priority=2, updated_at=excluded.updated_at",
        (sid, sellers[[s["seller_id"] for s in sellers].index(sid)]["seller_name"] or "(未命名)",
         "竞品重点观察", json.dumps(["competitor", "vip"]),
         datetime.now(timezone.utc).isoformat(timespec="seconds"),
         datetime.now(timezone.utc).isoformat(timespec="seconds"))
    )

# 给 1 个非自营 seller 加 display_name 覆盖（演示三档优先级）
if len(sellers) > 2:
    sid = sellers[2]["seller_id"]
    conn.execute(
        "INSERT INTO seller_enrichment (seller_id, display_name, is_watched, notes, tags, priority, created_at, updated_at, created_by) "
        "VALUES (?, ?, 0, ?, '?', 0, ?, ?, 'admin') "
        "ON CONFLICT(seller_id) DO UPDATE SET display_name=excluded.display_name, updated_at=excluded.updated_at",
        (sid, "【演示】用户自定义名", "这个 seller 的备注信息",
         datetime.now(timezone.utc).isoformat(timespec="seconds"),
         datetime.now(timezone.utc).isoformat(timespec="seconds"))
    )

# ── Round 1 (30 min ago)
round1_start = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(timespec="seconds")
round1_id = "r" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M") + "a"
cur = conn.execute(
    "INSERT INTO rounds (round_id, started_at, finished_at, status, cells_total, cells_self, cells_non_self) "
    "VALUES (?, ?, ?, 'success', 0, 0, 0)",
    (round1_id, round1_start, round1_start),
)
round1_db_id = cur.lastrowid

# ── Round 2 (now)
round2_start = datetime.now(timezone.utc).isoformat(timespec="seconds")
round2_id = "r" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M") + "b"
cur = conn.execute(
    "INSERT INTO rounds (round_id, started_at, finished_at, status, cells_total, cells_self, cells_non_self) "
    "VALUES (?, ?, ?, 'success', 0, 0, 0)",
    (round2_id, round2_start, round2_start),
)
round2_db_id = cur.lastrowid

# ── 写 cells：每个 POI 6-10 个 cell，混入自营 / 关注 / 普通
total = 0
self_count = 0
nonself_count = 0
cell_seed_round1 = []  # (poi_id, item_id, sku_id, seller_id, is_self, price_int, price_dec)
rng = random.Random(42)
watched_dicts = [s for s in sellers if s["seller_id"] in watched]
nonself_dicts = [s for s in sellers if not s["is_self"]]

for poi in pois:
    n_cells = random.randint(6, 10)
    for i in range(n_cells):
        ct = random.choice(CELL_TYPES)
        sku_name = random.choice(SKU_NAMES[ct])
        seller = random.choice(sellers)
        sid = seller["seller_id"]
        is_self = 1 if sid == SELF_SELLER_ID else 0
        price_int = str(random.choice([18, 25, 30, 35, 45, 58, 68, 88, 99, 128, 158, 198, 268]))
        price_dec = random.choice([".0", ".5", ".9", ""])
        item_id = str(random.randint(10**12, 10**13 - 1))
        sku_id = str(random.randint(10**12, 10**13 - 1))

        cell_seed_round1.append((poi["poi_id"], item_id, sku_id, sid, is_self, price_int, price_dec, ct, sku_name))

# Round 1
for poi_id, item_id, sku_id, sid, is_self, price_int, price_dec, ct, sku_name in cell_seed_round1:
    conn.execute(
        "INSERT INTO cells_snapshot (round_id, poi_id, poi_name, item_id, sku_id, cell_type, sku_name, "
        "price_int, price_dec, price_suffix, sold, seller_id, is_self, raw_shelf, first_seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
        (round1_db_id, poi_id, next(p["name"] for p in pois if p["poi_id"] == poi_id),
         item_id, sku_id, ct, sku_name, price_int, price_dec, "起", random.choice(["已售100+", "已售500+", "已售1000+", "1.2w+"]),
         sid, is_self, round1_start),
    )
    total += 1
    if is_self: self_count += 1
    else: nonself_count += 1

# Round 2：80% 沿用 round1（同 (poi,item,sku)），20% 改价格；5 个全新 (poi,item,sku) 触发 non_self_new
existing_item_ids = {item_id for _, item_id, _, _, _, _, _, _, _ in cell_seed_round1}
existing_sku_ids = {sku_id for _, _, sku_id, _, _, _, _, _, _ in cell_seed_round1}

def gen_unique_id():
    while True:
        x = str(rng.randint(10**12, 10**13 - 1))
        if x not in existing_item_ids and x not in existing_sku_ids:
            return x

for seed in cell_seed_round1:
    poi_id, item_id, sku_id, sid, is_self, price_int, price_dec, ct, sku_name = seed
    if rng.random() < 0.20:
        new_price_int = str(max(1, int(price_int) + rng.randint(-10, 10)))
    else:
        new_price_int = price_int
    conn.execute(
        "INSERT INTO cells_snapshot (round_id, poi_id, poi_name, item_id, sku_id, cell_type, sku_name, "
        "price_int, price_dec, price_suffix, sold, seller_id, is_self, raw_shelf, first_seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
        (round2_db_id, poi_id, next(p["name"] for p in pois if p["poi_id"] == poi_id),
         item_id, sku_id, ct, sku_name, new_price_int, price_dec, "起", "已售200+", sid, is_self,
         round2_start),
    )
    total += 1
    if is_self: self_count += 1
    else: nonself_count += 1

# 5 个新 cell（全新 item_id/sku_id）
new_cells_added = []
for i in range(5):
    poi = rng.choice(pois)
    ct = rng.choice(CELL_TYPES[:3])
    sku_name = rng.choice(SKU_NAMES[ct])
    pool = watched_dicts if rng.random() < 0.4 else nonself_dicts
    seller = rng.choice(pool)
    sid = seller["seller_id"]
    item_id = gen_unique_id()
    sku_id = gen_unique_id()
    existing_item_ids.add(item_id)
    existing_sku_ids.add(sku_id)
    conn.execute(
        "INSERT INTO cells_snapshot (round_id, poi_id, poi_name, item_id, sku_id, cell_type, sku_name, "
        "price_int, price_dec, price_suffix, sold, seller_id, is_self, raw_shelf, first_seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
        (round2_db_id, poi["poi_id"], poi["name"], item_id, sku_id, ct, sku_name,
         "58", ".0", "起", "新上线", sid, 0, round2_start),
    )
    total += 1
    nonself_count += 1
    new_cells_added.append((poi, sid, sku_name))

# 1 个自营缺位：把 round1 里某个 self cell 在 round2 UPDATE 为另一个非自营 seller
# （不能 INSERT 因为 (round,poi,item,sku) 已经写过；用 UPDATE 改 seller_id）
if cell_seed_round1:
    for seed in cell_seed_round1:
        poi_id, item_id, sku_id, sid, is_self, price_int, price_dec, ct, sku_name = seed
        if is_self and rng.random() < 0.5:
            other = rng.choice([s for s in sellers if not s["is_self"]])
            conn.execute(
                "UPDATE cells_snapshot SET seller_id = ?, is_self = 0, sold = ? "
                "WHERE round_id = ? AND poi_id = ? AND item_id = ? AND sku_id = ?",
                (other["seller_id"], "已售50+", round2_db_id, poi_id, item_id, sku_id),
            )
            break

# 写 alerts：非自营新增 + 价格异动 + 自营缺位
import hashlib


def dkey(*parts):
    bucket = int(datetime.now(timezone.utc).timestamp()) // 3600
    return hashlib.sha1(f"{'|'.join(parts)}|{bucket}".encode()).hexdigest()[:16]

# non_self_new (round2 新 cell)
for poi, sid, sku_name in new_cells_added:
    is_watched = sid in watched
    conn.execute(
        "INSERT INTO alerts (ts, round_id, type, severity, poi_id, poi_name, seller_id, payload, dedup_key, webhook_status) "
        "VALUES (?, ?, 'non_self_new', ?, ?, ?, ?, ?, ?, 'sent')",
        (round2_start, round2_db_id, "warning" if is_watched else "info",
         poi["poi_id"], poi["name"], sid,
         json.dumps({"sku_name": sku_name, "seller_id": sid, "watched": is_watched}, ensure_ascii=False),
         dkey("non_self_new", poi["poi_id"], sid)),
    )

# price_alert (round1 → round2 价格变的)
for seed in cell_seed_round1:
    if rng.random() < 0.10:
        poi_id, item_id, sku_id, sid, is_self, price_int, price_dec, ct, sku_name = seed
        conn.execute(
            "INSERT INTO alerts (ts, round_id, type, severity, poi_id, poi_name, seller_id, payload, dedup_key, webhook_status) "
            "VALUES (?, ?, 'price_alert', 'info', ?, ?, ?, ?, ?, 'sent')",
            (round2_start, round2_db_id, poi_id, next(p["name"] for p in pois if p["poi_id"] == poi_id),
             sid, json.dumps({"sku_name": sku_name, "old": price_int, "new": "???"}, ensure_ascii=False),
             dkey("price_alert", poi_id, item_id, sku_id)),
        )

# self_missing (1 条)
conn.execute(
    "INSERT INTO alerts (ts, round_id, type, severity, poi_id, poi_name, payload, dedup_key, webhook_status) "
    "VALUES (?, ?, 'self_missing', 'critical', '1345', '圆明园', ?, ?, 'pending')",
    (round2_start, round2_db_id,
     json.dumps({"item_id": "1065739764221", "msg": "自营被替换为外部商家"}, ensure_ascii=False),
     dkey("self_missing", "1345", "1065739764221")),
)

# 更新 round 统计
for rid, t, s, n in [(round1_db_id, total//2, self_count//2, nonself_count//2),
                     (round2_db_id, total - total//2, self_count - self_count//2, nonself_count - nonself_count//2)]:
    conn.execute("UPDATE rounds SET cells_total=?, cells_self=?, cells_non_self=?, duration_ms=? WHERE id=?",
                 (t, s, n, random.randint(800, 2400), rid))

# 更新 pois.last_scanned_at
for poi in pois:
    conn.execute("UPDATE pois SET last_scanned_at=?, last_status='success' WHERE poi_id=?",
                 (round2_start, poi["poi_id"]))

# 更新 config.last_global_scan
conn.execute("UPDATE config SET value=? WHERE key='last_global_scan'", (json.dumps(round2_start),))

conn.commit()
print(f"[seed] round1={round1_db_id} round2={round2_db_id}")
print(f"[seed] total cells={total} self={self_count} non_self={nonself_count}")
print(f"[seed] alerts: {len(new_cells_added)} non_self_new + 若干 price_alert + 1 self_missing")
conn.close()
print("[seed] done. 启动 uvicorn 后浏览器访问 http://127.0.0.1:8080/")