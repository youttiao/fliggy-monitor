"""Webhook 推送器。

支持的 IM 平台（v1 实现）：
- 钉钉（DingTalk）：通过机器人 webhook，markdown 卡片 + 可选签名
- 飞书（Feishu）：通过机器人 webhook，interactive card

URL 自动识别：
- 含 `oapi.dingtalk.com` → 钉钉
- 含 `open.feishu.cn`     → 飞书
- 其他                    → 自定义（按钉钉格式 POST，由调用方保证）

签名（钉钉官方算法）：
    string_to_sign = f"{timestamp}\n{sec}"
    sign = base64(hmac_sha256(secret.encode(), string_to_sign.encode()))

HMAC 签名（自定义头 X-Signature）：
    sha256(secret, body_bytes).hex() → "sha256=<hex>"
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Optional

import httpx


PLATFORM_DINGTALK = "dingtalk"
PLATFORM_FEISHU = "feishu"
PLATFORM_CUSTOM = "custom"


@dataclass
class WebhookResult:
    ok: bool
    status_code: int
    response: str
    error: str | None = None


def detect_platform(url: str) -> str:
    """按 URL 域名判定平台。"""
    if not url:
        return PLATFORM_CUSTOM
    lower = url.lower()
    if "oapi.dingtalk.com" in lower:
        return PLATFORM_DINGTALK
    if "open.feishu.cn" in lower:
        return PLATFORM_FEISHU
    return PLATFORM_CUSTOM


def _dingtalk_sign(secret: str) -> tuple[int, str]:
    """钉钉加签。返回 (timestamp, sign)。"""
    ts = int(time.time() * 1000)
    s = f"{ts}\n{secret}"
    h = hmac.new(secret.encode("utf-8"), s.encode("utf-8"), hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(h).decode("utf-8"))
    return ts, sign


def _build_dingtalk_body(alert: dict[str, Any]) -> dict[str, Any]:
    """钉钉 markdown 消息。

    字段约束：title ≤ 128 字符；text 是个 markdown 子集。
    """
    title = f"[飞猪哨兵] {alert.get('type_label', alert.get('type', ''))}".strip()[:128]
    severity = alert.get("severity", "info").upper()
    poi = alert.get("poi_name") or alert.get("poi_id") or "—"
    sku = alert.get("sku_name") or "—"
    seller = alert.get("seller_display") or alert.get("seller_id") or "—"
    color_map = {"INFO": "#5EAEFF", "WARNING": "#E5A847", "CRITICAL": "#FF6B6B"}
    color = color_map.get(severity, "#5EAEFF")
    watched = " ⭐ **关注卖家**" if alert.get("watched") else ""

    text_parts = [
        f"**POI**：{poi}",
        f"**SKU**：{sku}",
        f"**卖家**：{seller}{watched}",
        f"**等级**：<font color=\"{color}\">{severity}</font>",
    ]
    if alert.get("price_int"):
        text_parts.append(f"**价格**：{alert['price_int']}{alert.get('price_dec', '')}{alert.get('price_suffix', '')}")
    if alert.get("url"):
        text_parts.append(f"[打开 Dashboard]({alert['url']})")

    text = "\n\n".join(text_parts)
    return {"msgtype": "markdown", "markdown": {"title": title, "text": text}}


def _build_feishu_body(alert: dict[str, Any]) -> dict[str, Any]:
    """飞书 interactive card。

    字段约束：title.content ≤ 256 字符；元素数 ≤ 50。
    """
    title = f"飞猪哨兵 · {alert.get('type_label', alert.get('type', ''))}"[:256]
    severity = alert.get("severity", "info").upper()
    poi = alert.get("poi_name") or alert.get("poi_id") or "—"
    sku = alert.get("sku_name") or "—"
    seller = alert.get("seller_display") or alert.get("seller_id") or "—"
    color_map = {"INFO": "blue", "WARNING": "orange", "CRITICAL": "red"}
    template = color_map.get(severity, "blue")
    watched_tag = {"tag": "tag", "text": "⭐ 关注"} if alert.get("watched") else None

    fields = [
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**POI**\n{poi}"}},
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**等级**\n{severity}"}},
        {"is_short": False, "text": {"tag": "lark_md", "content": f"**SKU**\n{sku}"}},
        {"is_short": False, "text": {"tag": "lark_md", "content": f"**卖家**\n{seller}"}},
    ]

    elements: list[dict[str, Any]] = fields
    if watched_tag:
        elements.append(watched_tag)
    if alert.get("url"):
        elements.append({
            "tag": "action",
            "actions": [{
                "tag": "button",
                "text": {"tag": "plain_text", "content": "打开 Dashboard"},
                "type": "primary",
                "url": alert["url"],
            }],
        })
    elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": f"{alert.get('ts', '')}"}]})

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": template,
            },
            "elements": elements,
        },
    }


def _sign_body(secret: str, body_bytes: bytes) -> str:
    """自定义 HMAC-SHA256 签名头。"""
    h = hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
    return f"sha256={h}"


class WebhookSender:
    """一个 sender 对应一个 webhook URL（同一平台可能多个 URL → 多个 sender 实例）。"""

    def __init__(self, url: str, *, secret: Optional[str] = None, platform: Optional[str] = None,
                 timeout: float = 5.0):
        if not url:
            raise ValueError("webhook url is required")
        self.url = url
        self.secret = secret or ""
        self.platform = platform or detect_platform(url)
        self.timeout = timeout

    def send(self, alert: dict[str, Any]) -> WebhookResult:
        if self.platform == PLATFORM_DINGTALK:
            body = _build_dingtalk_body(alert)
        elif self.platform == PLATFORM_FEISHU:
            body = _build_feishu_body(alert)
        else:
            body = _build_dingtalk_body(alert)  # custom 按钉钉兼容格式

        # 序列化
        body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")

        # headers
        headers = {"Content-Type": "application/json; charset=utf-8"}

        # 签名
        url_with_sign = self.url
        if self.platform == PLATFORM_DINGTALK and self.secret:
            ts, sign = _dingtalk_sign(self.secret)
            url_with_sign = f"{self.url}&timestamp={ts}&sign={sign}"
        if self.secret and self.platform != PLATFORM_DINGTALK:
            headers["X-Signature"] = _sign_body(self.secret, body_bytes)

        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(url_with_sign, content=body_bytes, headers=headers)
            ok = 200 <= resp.status_code < 300
            # 钉钉成功响应里 errcode=0；飞书 StatusCode=0
            snippet = (resp.text or "")[:300]
            try:
                j = resp.json()
                if self.platform == PLATFORM_DINGTALK and j.get("errcode", 0) != 0:
                    ok = False
                    snippet = f"errcode={j.get('errcode')} errmsg={j.get('errmsg')}"
                elif self.platform == PLATFORM_FEISHU and j.get("StatusCode", j.get("code", 0)) not in (0, 200):
                    ok = False
                    snippet = f"code={j.get('code')} msg={j.get('msg')}"
            except (json.JSONDecodeError, ValueError):
                pass
            return WebhookResult(ok=ok, status_code=resp.status_code, response=snippet)
        except httpx.HTTPError as e:
            return WebhookResult(ok=False, status_code=0, response="", error=f"{type(e).__name__}: {e}")


def render_alert(
    *,
    alert_type: str,
    severity: str,
    poi_id: Optional[str],
    poi_name: Optional[str],
    sku_name: Optional[str],
    seller_id: Optional[str],
    seller_display: Optional[str],
    watched: bool = False,
    price_int: Optional[str] = None,
    price_dec: Optional[str] = None,
    price_suffix: Optional[str] = None,
    ts: Optional[str] = None,
    dashboard_url: Optional[str] = None,
) -> dict[str, Any]:
    """把一条告警归一化成 dict 给 sender 用。type_label 给中文。"""
    label_map = {
        "non_self_new": "出现新非自营 SKU",
        "price_alert": "价格异动",
        "self_missing": "自营缺位",
        "first_seller": "首次出现新卖家",
        "shelf_error": "采集失败",
        "cookie_refresh_failed": "Cookie 续期失败",
    }
    return {
        "type": alert_type,
        "type_label": label_map.get(alert_type, alert_type),
        "severity": severity,
        "poi_id": poi_id,
        "poi_name": poi_name,
        "sku_name": sku_name,
        "seller_id": seller_id,
        "seller_display": seller_display,
        "watched": watched,
        "price_int": price_int,
        "price_dec": price_dec,
        "price_suffix": price_suffix,
        "ts": ts,
        "url": dashboard_url,
    }