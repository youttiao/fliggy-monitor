"""冒烟测试：1 个 POI（圆明园）端到端走一遍。

用法：
    python tests/smoke.py

预期：打印 6 个非自营 cell（圆明园里没有 SELF），不抛异常。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "code"))

from mtop_client import MtopClient, parse_ticket_cells
from selectors import SELF_SELLER_ID


def main():
    cookies_path = Path("/etc/fliggy-vps/cookies.json")
    if not cookies_path.exists():
        # demo 模式：硬编码示例 cookies（来自 mac Chrome 实抓，2026-08-23 有效）
        # 新项目里换成自己的 cookie 注入
        cookies_path = Path(__file__).resolve().parent / "demo_cookies.json"

    cookies = json.loads(cookies_path.read_text())
    client = MtopClient(cookies=cookies)

    # 1. shelf 单 POI
    raw = client.shelf("1345")  # 圆明园
    cells = parse_ticket_cells(raw)
    print(f"[shelf 1345 圆明园] {len(cells)} ticket cells")

    # 2. 列表
    self_count = 0
    for c in cells:
        is_self = (c["sellerId"] == SELF_SELLER_ID)
        if is_self:
            self_count += 1
        print(f"  {'★SELF' if is_self else '   '} "
              f"seller={c['sellerId']:13s}  item={c['itemId']:13s}  "
              f"{c['price']:8s}  {c['name'][:30]}")

    print(f"\n[SUMMARY] 圆明园: {len(cells)} cells, {self_count} 自营")

    # 3. booktips 单 seller（用第一个非自营 cell 验证）
    for c in cells:
        if c["sellerId"] != SELF_SELLER_ID:
            raw = client.booktips(c["itemId"], c["skuId"], c["poiId"])
            info = parse_seller_info(raw)
            print(f"\n[booktips] itemId={c['itemId']} → sellerName={info['sellerName']!r}")
            print(f"            shopJumpUrl={info['shopJumpUrl']}")
            print(f"            serviceStats={info['serviceStats']}")
            break

    print("\n✓ smoke ok")


if __name__ == "__main__":
    main()