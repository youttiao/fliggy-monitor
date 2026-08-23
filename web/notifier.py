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
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

import httpx

PLATFORM_DINGTALK = "dingtalk"
PLATFORM_FEISHU = "feishu"
PLATFORM_CUSTOM = "custom"

_BJ = ZoneInfo("Asia/Shanghai")


def _fmt_bj_ts(iso_str: Any) -> str:
    """UTC ISO → 'YYYY-MM-DD HH:MM:SS' Asia/Shanghai。

    alert['ts'] 是 DB 存的 UTC ISO（`...+00:00`），直接进 IM 卡片会让人误判时间。
    IM 卡片上的时间按 Beijing 渲染，运维手机上看到的就是 Beijing 时间。
    空值 / 解析失败时回退原值（不抛错，避免一次告警被渲染失败拖死）。
    """
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(str(iso_str).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return str(iso_str)
        return dt.astimezone(_BJ).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(iso_str)


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
        text_parts.append(f"**价格**：{_fmt_price(alert['price_int'], alert.get('price_dec'), alert.get('price_suffix'))}")
    if alert.get("url"):
        text_parts.append(f"[打开 Dashboard]({alert['url']})")

    text = "\n\n".join(text_parts)
    return {"msgtype": "markdown", "markdown": {"title": title, "text": text}}


def _build_feishu_body(alert: dict[str, Any]) -> dict[str, Any]:
    """飞书 interactive card（schema 2.0）。

    关键约束：
    - ``elements`` 必须放在 ``body.elements`` 下，不能直接挂 ``card.elements``
      （旧 schema 1.0 写法会被飞书 7.20+ 拒收，返回 ``unknown property, property: elements``）
    - 按钮不再用 ``{"tag": "action", "actions": [...]}`` 包裹，直接放 ``button``
    - ``note`` 组件已废弃；时间戳直接用 plain text 跟在元素列表末尾
    """
    title = f"飞猪哨兵 · {alert.get('type_label', alert.get('type', ''))}"[:256]
    severity = alert.get("severity", "info").upper()
    poi = alert.get("poi_name") or alert.get("poi_id") or "—"
    sku = alert.get("sku_name") or "—"
    seller = alert.get("seller_display") or alert.get("seller_id") or "—"
    color_map = {"INFO": "blue", "WARNING": "orange", "CRITICAL": "red"}
    template = color_map.get(severity, "blue")

    body_elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "fields": [
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**POI**\n{poi}"}},
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**等级**\n{severity}"}},
                {"is_short": False, "text": {"tag": "lark_md", "content": f"**SKU**\n{sku}"}},
                {"is_short": False, "text": {"tag": "lark_md", "content": f"**卖家**\n{seller}"}},
            ],
        },
    ]
    if alert.get("watched"):
        body_elements.append({"tag": "tag", "text": "⭐ 关注"})
    if alert.get("price_int"):
        body_elements.append({"tag": "markdown", "content": f"**价格** {_fmt_price(alert['price_int'], alert.get('price_dec'), alert.get('price_suffix'))}"})
    if alert.get("url"):
        body_elements.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "打开 Dashboard"},
            "type": "primary",
            "url": alert["url"],
        })
    if alert.get("ts"):
        body_elements.append({
            "tag": "markdown",
            "content": f"<font color='grey'>{_fmt_bj_ts(alert['ts'])}</font>",
        })

    return {
        "msg_type": "interactive",
        "card": {
            "schema": "2.0",
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": template,
            },
            "body": {"elements": body_elements},
        },
    }


def _sign_body(secret: str, body_bytes: bytes) -> str:
    """自定义 HMAC-SHA256 签名头。"""
    h = hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
    return f"sha256={h}"


# ──────────────────────────────────────────────────────────────
# 货架聚合报告（每轮一条，节省 webhook 调用次数）
# ──────────────────────────────────────────────────────────────


def _fmt_price(price_int: Optional[str], price_dec: Optional[str], suffix: Optional[str]) -> str:
    """把 price_int / price_dec / suffix 拼成「¥58.00 起」。

    price_dec 的真实形态上游不一定：API 文档写的是 ``".0"`` / ``".5"``，但实测
    偶尔会回 ``"00"`` / ``None`` / ``"0"``。如果直接 ``f"{price_int}{price_dec}"``，
    后者会把 ``"30" + "00"`` 拼成 ``"¥3000"``（整数被吞进小数再乘 100），看似
    价格暴涨但其实是缺小数点。

    归一化：剥掉已有的小数点，把剩下的数字 padding 到 2 位（不够右补 0），
    再和整数部分用 ``.`` 拼回去。与 ``web/templates_factory._fmt_price_cents``
    是同一份逻辑，保证 webhook / Dashboard 渲染一致。
    """
    if not price_int:
        return "—"
    raw = str(price_dec or "").replace(".", "").strip()
    cents = (raw + "00")[:2]
    s = f"¥{price_int}.{cents}"
    if suffix:
        s += f" {suffix}"
    return s


def _truncate(text: str, n: int = 36) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= n else text[: n - 1] + "…"


def _render_shelf_report_markdown(report: dict[str, Any]) -> str:
    """把聚合报告渲染成 markdown 文本（飞书 / 钉钉共用）。"""
    lines: list[str] = []
    title_suffix = "（有非自营）" if report["non_self_pois"] else ""
    lines.append(f"**飞猪哨兵 · 货架报告**{title_suffix} · 轮次 `{report['round_id']}`")
    lines.append("")

    for g in report["groups"]:
        poi_line = f"📍 **{g['poi_name']}**"
        if g["has_non_self"]:
            poi_line += f" — {g['non_self_count']} 个非自营货架"
        else:
            poi_line += f" — ✓ 全部自营（{g['self_count']} 个货架）"
        lines.append(poi_line)
        if g["has_non_self"]:
            for sh in g["shelves"]:
                watch_mark = " ⭐" if sh.get("watched") else ""
                lines.append(f"· {_truncate(sh['sku_name'])} · {_fmt_price(sh['price_int'], sh['price_dec'], sh['price_suffix'])}{watch_mark}")
        lines.append("")

    lines.append("---")
    lines.append(
        f"关注 POI **{report['total_pois']}** · 出现非自营 **{report['non_self_pois']}** · {report['ts']}"
    )
    if report.get("dashboard_url"):
        lines.append(f"[打开 Dashboard]({report['dashboard_url']})")

    return "\n".join(lines)


def build_shelf_report_feishu(report: dict[str, Any]) -> dict[str, Any]:
    """飞书 interactive card（schema 2.0 聚合报告）。"""
    title = "飞猪哨兵 · 货架报告"
    template = "red" if report["non_self_pois"] else "blue"

    content = _render_shelf_report_markdown(report)

    body_elements: list[dict[str, Any]] = [
        {"tag": "markdown", "content": content},
    ]
    if report.get("dashboard_url"):
        body_elements.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "打开 Dashboard"},
            "type": "primary",
            "url": report["dashboard_url"],
        })
    if report.get("ts"):
        body_elements.append({
            "tag": "markdown",
            "content": f"<font color='grey'>{report['ts']}</font>",
        })

    return {
        "msg_type": "interactive",
        "card": {
            "schema": "2.0",
            "header": {
                "title": {"tag": "plain_text", "content": title[:256]},
                "template": template,
            },
            "body": {"elements": body_elements},
        },
    }


def build_shelf_report_dingtalk(report: dict[str, Any]) -> dict[str, Any]:
    """钉钉 markdown（聚合报告）。"""
    content = _render_shelf_report_markdown(report)
    title = "飞猪哨兵 · 货架报告"
    return {
        "msgtype": "markdown",
        "markdown": {"title": title[:128], "text": content},
        "at": {"isAtAll": False},
    }


def build_shelf_report_body(report: dict[str, Any], platform: str) -> dict[str, Any]:
    """按平台选报告渲染器（仅 payload，不含签名 / URL 处理）。"""
    if platform == PLATFORM_FEISHU:
        return build_shelf_report_feishu(report)
    return build_shelf_report_dingtalk(report)


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
        if self.platform == PLATFORM_FEISHU:
            body = _build_feishu_body(alert)
        elif self.platform == PLATFORM_DINGTALK:
            body = _build_dingtalk_body(alert)
        else:
            # auto / custom：按 URL 解析
            if detect_platform(self.url) == PLATFORM_FEISHU:
                body = _build_feishu_body(alert)
            else:
                body = _build_dingtalk_body(alert)
        return self._post(body)

    def send_report(self, report: dict[str, Any]) -> WebhookResult:
        """发送货架聚合报告（每轮一条，节省 quota）。"""
        # auto / custom 也要按 URL 选 renderer，否则 auto 会落到钉钉 body
        platform = self.platform
        if platform == "auto" or not platform:
            platform = detect_platform(self.url)
        body = build_shelf_report_body(report, platform)
        return self._post(body)

    def _post(self, body: dict[str, Any]) -> WebhookResult:
        """序列化 + 签名 + POST。"""
        # 序列化
        body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")

        # headers
        headers = {"Content-Type": "application/json; charset=utf-8"}

        # 用于签名 + OK 校验：auto → 按 URL 解析
        effective_platform = self.platform
        if effective_platform == "auto" or not effective_platform:
            effective_platform = detect_platform(self.url)

        # 签名
        url_with_sign = self.url
        if effective_platform == PLATFORM_DINGTALK and self.secret:
            ts, sign = _dingtalk_sign(self.secret)
            url_with_sign = f"{self.url}&timestamp={ts}&sign={sign}"
        if self.secret and effective_platform != PLATFORM_DINGTALK:
            headers["X-Signature"] = _sign_body(self.secret, body_bytes)

        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(url_with_sign, content=body_bytes, headers=headers)
            ok = 200 <= resp.status_code < 300
            # 钉钉成功响应里 errcode=0；飞书 StatusCode=0
            snippet = (resp.text or "")[:300]
            try:
                j = resp.json()
                if effective_platform == PLATFORM_DINGTALK and j.get("errcode", 0) != 0:
                    ok = False
                    snippet = f"errcode={j.get('errcode')} errmsg={j.get('errmsg')}"
                elif effective_platform == PLATFORM_FEISHU and j.get("StatusCode", j.get("code", 0)) not in (0, 200):
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