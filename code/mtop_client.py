"""Fliggy mtop client — 纯 stdlib + curl，VPS 上零依赖就能跑。

用法：
    from mtop_client import MtopClient
    c = MtopClient(cookies={...})  # cookies 包含 _m_h5_tk / _m_h5_tk_enc / cookie2 / t
    raw = c.shelf(poi_id="1345")
    raw = c.booktips(item_id="...", sku_id="...", poi_id="...")
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import time
from selectors import (
    APP_KEY,
    BOOKTIPS_API,
    BOOKTIPS_DEFAULT_DATA,
    BOOKTIPS_VERSION,
    REQUIRED_HEADERS,
    SHELF_API,
    SHELF_DATA_TYPE_SHELF,
    SHELF_FC_GROUP,
    SHELF_FC_NAME,
    SHELF_VERSION,
    TTID,
)
from typing import Any
from urllib.parse import quote


class MtopError(Exception):
    pass


class MtopClient:
    """Fliggy mtop HTTP client. thread-unsafe; create per thread or wrap with lock."""

    CURL_BIN = "/usr/bin/curl"

    def __init__(self, cookies: dict[str, str], timeout: int = 15):
        self.cookies = dict(cookies)
        self.timeout = timeout
        tk = self.cookies.get("_m_h5_tk", "")
        if "_" not in tk:
            raise MtopError(f"_m_h5_tk format invalid: {tk!r}")
        self._token = tk.split("_", 1)[0]
        self._missing = [k for k in ("_m_h5_tk", "_m_h5_tk_enc", "cookie2", "t") if k not in cookies]
        if self._missing:
            raise MtopError(f"missing required cookies: {self._missing}")

    @staticmethod
    def _sign(token: str, t_ms: str, app_key: str, data_str: str) -> str:
        """MD5(token & t & appKey & data).hexdigest()  ←  raw data 字符串，**不要 sort_keys**"""
        return hashlib.md5(f"{token}&{t_ms}&{app_key}&{data_str}".encode()).hexdigest()

    def _request(self, url_base: str, data_obj: dict[str, Any], version: str) -> dict:
        data_str = json.dumps(data_obj, separators=(",", ":"), ensure_ascii=False)
        t_ms = str(int(time.time() * 1000))
        sig = self._sign(self._token, t_ms, APP_KEY, data_str)

        url = (f"{url_base}?type=originaljson"
               f"&data={quote(data_str, safe='')}"
               f"&ttid={quote(TTID)}"
               f"&appKey={APP_KEY}"
               f"&t={t_ms}"
               f"&sign={sig}")

        cookie_h = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        cmd = [
            self.CURL_BIN, "-sS",
            "--max-time", str(self.timeout),
            "-H", f"referer: {REQUIRED_HEADERS['referer']}",
            "-H", f"origin:  {REQUIRED_HEADERS['origin']}",
            "-H", f"user-agent: {REQUIRED_HEADERS['user-agent']}",
            "-H", f"cookie: {cookie_h}",
            "-w", "\n%{http_code}",
            url,
        ]
        out = subprocess.check_output(cmd).decode()
        body, _, code = out.rpartition("\n")
        code = code.strip()
        if code != "200":
            raise MtopError(f"HTTP {code}: {body[:500]}")
        try:
            raw = json.loads(body)
        except json.JSONDecodeError as e:
            raise MtopError(f"JSON decode failed: {e}; body[:200]={body[:200]!r}")
        ret = raw.get("ret", [])
        if not ret or "SUCCESS" not in ret:
            raise MtopError(f"mtop ret != SUCCESS: {ret}; body[:300]={body[:300]!r}")
        return raw

    def shelf(self, poi_id: str) -> dict:
        data = {
            "fcGroup": SHELF_FC_GROUP,
            "fcName": SHELF_FC_NAME,
            "fcData": {
                "dataType": SHELF_DATA_TYPE_SHELF,
                "poiId": str(poi_id),
            },
            "source": "standard_shelf",
            "pageSource": "standard_shelf",
            "h5Version": "1.0.26",
        }
        return self._request(SHELF_API, data, SHELF_VERSION)

    def booktips(self, item_id: str, sku_id: str, poi_id: str) -> dict:
        client_ts = time.strftime("%Y-%m-%d %H:%M:%S.000", time.gmtime())
        data = {
            "itemId": str(item_id),
            "skuId":  str(sku_id),
            "poiId":  str(poi_id),
            **BOOKTIPS_DEFAULT_DATA,
            "userFlowParam": json.dumps(
                {"spmDetail": "", "clientReqTime": client_ts},
                separators=(",", ":"),
            ),
        }
        return self._request(BOOKTIPS_API, data, BOOKTIPS_VERSION)


# ── parse helpers ──────────────────────────────────────────────────
def parse_ticket_cells(shelf_raw: dict) -> list[dict]:
    """从 shelf raw 抽所有「门票」cell + sellerId + 价格/库存。

    Returns: [{itemId, skuId, poiId, poiName, name, price, priceDecimal, sold, cellType, sellerId}, ...]
    """
    try:
        shelves = shelf_raw["data"]["result"]["data"]["shelf"]["shelves"]
    except (KeyError, TypeError):
        return []
    out = []
    for shelf in shelves:
        if shelf.get("type") != "ScenicTicketType":
            continue
        cells = list(shelf.get("cells", []))
        for tab in shelf.get("tabs", []):
            cells.extend(tab.get("cells", []))
        for cell in cells:
            if not cell.get("bookingTipsJumpInfo"):
                continue  # 免费票 / 无 tips，跳过
            ps = cell.get("priceStruct", {}) or {}
            out.append({
                "itemId":      str(cell.get("itemId", "")),
                "skuId":       str(cell.get("skuId", "")),
                "poiId":       str(cell.get("poiId", "")),
                "poiName":     str(cell.get("poiName", "")),
                "name":        str(cell.get("name", "")),
                "price":       f"{ps.get('pricePrefix', '¥')}{ps.get('integerPrice', '')}{ps.get('priceSuffix', '')}",
                "priceDecimal": ps.get("decimalPrice"),
                "sold":        str(cell.get("soldStr", "")),
                "cellType":    shelf.get("name", "ScenicTicketType"),
                "sellerId":    str(cell.get("sellerId", "")),
            })
    return out


def parse_seller_info(booktips_raw: dict) -> dict | None:
    """从 booktips raw 抽 sellerName / icon / shopUrl / 服务人数。

    ⚠️ canonical sellerId 是 shelf cell 的（13 位 `2217xxx`），booktips 返回的
    sellerIcon URL 里含另一套 ID（OSS 路径 `6xxx/8xxx`），不要拿来当 sellerId。
    """
    try:
        info = booktips_raw["data"]["sellerInfo"]["data"]
    except (KeyError, TypeError):
        return None
    if not info.get("sellerName"):
        return None
    return {
        "sellerName":   info.get("sellerName"),
        "sellerIcon":   info.get("sellerIcon"),
        "icon":         info.get("icon"),
        "shopJumpUrl":  (info.get("jumpInfo") or {}).get("shopJumpUrl"),
        "serviceStats": info.get("sellerPropList", []),
    }