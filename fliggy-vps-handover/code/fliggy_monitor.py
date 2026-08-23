"""Fliggy H5 POI 监控 — 主循环模板。

新项目改这个：
  - cookies 来源（环境变量 / 配置文件 / 单独 fetcher）
  - 数据落盘（DB / OSS / webhook）
  - 告警通道（钉钉 / 企微 / 邮件）

设计原则：
  - shelf 每轮全量扫 8 POI（轻量，单 POI < 50KB）
  - booktips 增量：只在 cache miss 时 hit
  - seller 缓存启动时一次性加载到内存
  - 默认输出/告警只针对「非自营」cell
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Iterable

from mtop_client import MtopClient, parse_ticket_cells, parse_seller_info
from selectors import SELF_SELLER_ID

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


# ── 加载静态数据 ─────────────────────────────────────────────────────
def load_poi_registry() -> list[dict]:
    p = ROOT.parent / "data" / "poi_registry.json"
    return json.loads(p.read_text())["pool"]


def load_seller_cache() -> dict[str, dict]:
    """{sellerId: {sellerName, sellerIcon, shopJumpUrl, serviceStats, ...}, ...}"""
    p = ROOT.parent / "data" / "seller_cache.json"
    cache = json.loads(p.read_text())
    return cache["sellers"]


# ── 监控一轮 ────────────────────────────────────────────────────────
def monitor_one_poi(client: MtopClient, poi: dict, seller_cache: dict) -> list[dict]:
    """对一个 POI 跑一轮扫描，返回「非自营 cell」列表（含卖家信息）。

    单 cell 输出格式：
    {
      poiId, poiName, itemId, skuId, name, price, sold,
      sellerId, sellerName, shopJumpUrl, isSelf: False
    }
    """
    raw = client.shelf(poi["poiId"])
    cells = parse_ticket_cells(raw)
    out = []
    for c in cells:
        sid = c["sellerId"]
        is_self = (sid == SELF_SELLER_ID)
        if is_self:
            continue  # 跳过自营（监控目的是找非自营）
        seller = seller_cache.get(sid, {})
        out.append({
            **{k: c[k] for k in ("poiId", "poiName", "itemId", "skuId", "name", "price", "sold")},
            "sellerId":    sid,
            "sellerName":  seller.get("sellerName", "(unknown)"),
            "sellerIcon":  seller.get("sellerIcon"),
            "shopJumpUrl": seller.get("shopJumpUrl"),
            "isSelf":      False,
        })
    return out


def refill_seller_cache(client: MtopClient, seller_cache: dict, hit_log: list) -> int:
    """扫描一轮后，对 cache miss 的 sellerId 触发 booktips 增量补齐。返回新 hit 数。"""
    miss = [sid for sid in hit_log if sid not in seller_cache or not seller_cache[sid].get("sellerName")]
    new_hits = 0
    for sid in miss:
        # 需要一个 itemId 来 hit booktips；通常从 hit_log 里取
        item_id = hit_log.get(sid + ":itemId")  # 实际使用时改成 dict 维护 itemId 映射
        sku_id = hit_log.get(sid + ":skuId")
        poi_id = hit_log.get(sid + ":poiId")
        if not (item_id and sku_id and poi_id):
            continue
        try:
            raw = client.booktips(item_id, sku_id, poi_id)
        except Exception as e:
            print(f"[booktips FAIL] sid={sid}: {e}")
            continue
        info = parse_seller_info(raw)
        if info:
            seller_cache.setdefault(sid, {})
            seller_cache[sid].update({
                "sellerName":   info["sellerName"],
                "sellerIcon":   info["sellerIcon"],
                "icon":         info["icon"],
                "shopJumpUrl":  info["shopJumpUrl"],
                "serviceStats": info["serviceStats"],
            })
            new_hits += 1
            print(f"[booktips HIT] sid={sid} → {info['sellerName']!r}")
    return new_hits


# ── 主循环 ──────────────────────────────────────────────────────────
def main():
    # TODO: cookies 怎么注入 — 从 env / file / KMS
    # 这里直接给一个示例；新项目改成自己的获取逻辑
    cookies = json.loads(Path("/etc/fliggy-vps/cookies.json").read_text())

    client = MtopClient(cookies=cookies)
    pois = load_poi_registry()
    seller_cache = load_seller_cache()

    print(f"[init] {len(pois)} POI, {len(seller_cache)} sellers cached")
    print(f"[init] SELF_SELLER_ID = {SELF_SELLER_ID}")

    while True:
        round_results: list[dict] = []
        miss_sids: list[str] = []
        for poi in pois:
            try:
                cells = monitor_one_poi(client, poi, seller_cache)
                round_results.extend(cells)
            except Exception as e:
                print(f"[shelf FAIL] poi={poi['name']}: {e}")
                continue

            # 收集本轮新发现的 sellerId → booktips 缓存
            # (实际场景：调用 client.shelf 后，从 raw 抽所有 cell.sellerId)
            # 省略 — 完整逻辑在 build_seller_cache.py 里

        # 告警：非自营 cell 总数
        non_self = [c for c in round_results if not c.get("isSelf")]
        print(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
              f"non-self cells: {len(non_self)} across {len(pois)} POI")

        # TODO: 这里接 webhook / 写 DB / 发钉钉 / 写 OSS
        # 当前 demo：dump 到 stdout
        for c in non_self:
            print(f"  {c['poiName']:8s} | {c['name'][:30]:30s} | "
                  f"{c['price']:8s} | {c['sellerName']}")

        # 下一轮
        time.sleep(60 * 30)  # 30 min 一轮


if __name__ == "__main__":
    main()