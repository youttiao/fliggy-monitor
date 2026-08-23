# 04 · Webhook 通知规范

> 当监控脚本检测到「非自营 SKU」/「价格异动」/「自营缺位」等事件时，把消息 POST 到用户配置的 webhook URL（钉钉/企微/飞书/Bark/Telegram/自定义）。

---

## 4.1 协议总览

| 项 | 规格 |
|---|---|
| 方法 | `POST` |
| 编码 | `Content-Type: application/json; charset=utf-8` |
| 鉴权 | 自定义 header：`X-Sentinel-Signature: sha256=<hex>`（HMAC-SHA256, key=用户 webhook_secret） |
| 幂等 | 自定义 header：`X-Sentinel-Dedup-Key: <16-char hex>` |
| 重试 | 最多 3 次，指数退避 2s/8s/30s |
| 超时 | 单次请求 10s |
| 限频 | 同 `dedup_key` 1 小时内最多 1 次 |

---

## 4.2 告警类型 & Payload

### 4.2.1 `non_self_new` — 非自营 SKU 出现

**触发**：本轮扫描中某 cell `seller_id != self_seller_id`（无论之前是否出现过——用 `dedup_key` 去重窗口控制频率）。

**Payload**：
```json
{
  "event": "non_self_new",
  "ts": "2026-08-23T14:00:00+08:00",
  "round_id": "r202608231400",
  "severity": "warning",
  "poi": {
    "id": "1345",
    "name": "圆明园"
  },
  "sku": {
    "item_id": "1065739764221",
    "sku_id": "6276363111198",
    "name": "大门门门票+西洋楼遗址+沙盘全景模型展+电子语音讲解",
    "cell_type": "门票套餐",
    "price": "¥58",
    "price_decimal": ".0",
    "sold": "1234"
  },
  "seller": {
    "id": "2215602156137",
    "name": "宫足迹旅行社旗舰店",
    "shop_url": "https://shop409282745.m.taobao.com",
    "service_stat": "服务人数 17w+",
    "is_self": false
  },
  "deep_link": "https://feizhu.19880913.xyz/poi/1345?focus=1065739764221-6276363111198",
  "context": "本 POI 共 6 个非自营 cell，本次为第 3 个"
}
```

### 4.2.2 `price_alert` — 价格异动 ±20%

**触发**：同 `(poi_id, item_id, sku_id)` 的 `price_int/price_dec` 与上轮比变化 > 20%。

```json
{
  "event": "price_alert",
  "ts": "2026-08-23T14:00:00+08:00",
  "round_id": "r202608231400",
  "severity": "info",
  "poi": { "id": "1345", "name": "圆明园" },
  "sku": { "item_id": "...", "sku_id": "...", "name": "..." },
  "price": {
    "current":  "¥37.61",
    "previous": "¥31.36",
    "delta_pct": "+19.99%",
    "direction": "up"
  },
  "deep_link": "https://feizhu.19880913.xyz/poi/1345?focus=..."
}
```

### 4.2.3 `self_missing` — 自营 cell 突然消失（高严重）

**触发**：本轮某 POI 的 cells 全为非自营，但上轮存在自营 cell。

```json
{
  "event": "self_missing",
  "ts": "2026-08-23T14:00:00+08:00",
  "round_id": "r202608231400",
  "severity": "critical",
  "poi": { "id": "1345", "name": "圆明园" },
  "context": "上一轮自营 seller 2217592322543 在此 POI 有 0 个 cell，本轮亦为 0（已 3 轮无自营）",
  "deep_link": "https://feizhu.19880913.xyz/poi/1345"
}
```

> ⚠️ 注意：圆明园/天坛/恭王府历史上就没有自营（见 `seller_baseline.json` stability_notes），所以本规则的「持续 3 轮无自营」是必要阈值。

### 4.2.4 `first_seller` — 新卖家首次出现

**触发**：shelf 中出现一个 `seller_id` 之前从未在 seller_cache 见过。

```json
{
  "event": "first_seller",
  "ts": "2026-08-23T14:00:00+08:00",
  "round_id": "r202608231400",
  "severity": "info",
  "poi": { "id": "1544", "name": "恭王府" },
  "seller": {
    "id": "2219xxxxxxxx",
    "name": "新出现旅行社专营店",
    "shop_url": "https://shopXXXXXX.m.taobao.com",
    "first_seen_at": "2026-08-23T14:00:00+08:00"
  },
  "deep_link": "https://feizhu.19880913.xyz/poi/1544"
}
```

### 4.2.5 `shelf_error` — 扫描异常（高严重）

**触发**：某 POI 连续 3 轮 `shelf` 失败（HTTP 非 200 / FAIL_SYS_* / 超时）。

```json
{
  "event": "shelf_error",
  "ts": "2026-08-23T14:00:00+08:00",
  "round_id": "r202608231400",
  "severity": "critical",
  "poi": { "id": "1345", "name": "圆明园" },
  "error": {
    "ret": "FAIL_SYS_SESSION_EXPIRED::Session过期",
    "consecutive_failures": 3,
    "first_failure_at": "2026-08-23T13:30:00+08:00"
  },
  "deep_link": "https://feizhu.19880913.xyz/alerts"
}
```

### 4.2.6 `cookie_refresh_failed` — Cookie 续期失败（critical）

```json
{
  "event": "cookie_refresh_failed",
  "ts": "2026-08-23T13:00:00+08:00",
  "severity": "critical",
  "error": {
    "msg": "playwright 启动失败: chromium binary not found",
    "consecutive_failures": 2
  },
  "deep_link": "https://feizhu.19880913.xyz/settings"
}
```

---

## 4.3 HTTP Headers

```
POST <user-webhook-url> HTTP/1.1
Host: <target>
Content-Type: application/json; charset=utf-8
User-Agent: FliggySentinel/1.0 (+https://feizhu.19880913.xyz)
X-Sentinel-Event: non_self_new
X-Sentinel-Ts: 2026-08-23T14:00:00+08:00
X-Sentinel-Dedup-Key: 7a3b9c1f4e8d2a6b
X-Sentinel-Signature: sha256=<hex>
Content-Length: <...>

{ ...payload... }
```

### 签名算法（HMAC-SHA256）

```
sig = hex( HMAC_SHA256(key = webhook_secret, msg = raw_body) )
```

`webhook_secret` 是用户在设置页配置的密钥（默认与登录密码相同，可单独修改）。

**验证脚本（用户侧）**：
```python
import hmac, hashlib
def verify(body: bytes, sig_header: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig_header)
```

---

## 4.4 钉钉 / 企微 / 飞书 / Bark / Telegram 适配示例

### 4.4.1 钉钉（自定义机器人）

钉钉 webhook 接收 `markdown` 类型最易读。在监控发送前，加一层 wrapper：

```python
def to_dingtalk(payload: dict) -> dict:
    return {
        "msgtype": "markdown",
        "markdown": {
            "title": f"[飞猪哨兵] {payload['event']} · {payload['poi']['name']}",
            "text": format_md(payload)   # 见下
        },
        "at": {"isAtAll": False}
    }

def format_md(p: dict) -> str:
    e = p["event"]
    poi = p["poi"]["name"]
    if e == "non_self_new":
        return (f"### ⚠ 非自营 SKU 出现\n\n"
                f"**景区**: {poi}\n\n"
                f"**票**: {p['sku']['name']}\n\n"
                f"**卖家**: {p['seller']['name']}  \n"
                f"服务: {p['seller'].get('service_stat', '-')}\n\n"
                f"**价格**: {p['sku']['price']}  销量: {p['sku']['sold']}\n\n"
                f"[详情]({p['deep_link']})")
    # ... 其他 event 类似
```

> 钉钉的 webhook URL 自带 `access_token=xxx`，所以**用户的 webhook URL 直接填钉钉给的 URL 即可**——上面的 `payload` 是我们内部的 canonical 格式，到 webhook 发送时按目标平台 wrap。

### 4.4.2 企微

```python
def to_wechat_work(payload: dict) -> dict:
    return {
        "msgtype": "markdown",
        "markdown": {"content": format_md(payload)}
    }
```

### 4.4.3 飞书

```python
def to_feishu(payload: dict) -> dict:
    return {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text",
                                  "content": f"[飞猪哨兵] {payload['event']}"}},
            "elements": [
                {"tag": "markdown", "content": format_md(payload)}
            ]
        }
    }
```

### 4.4.4 Bark（iOS）

```python
def to_bark(payload: dict) -> str:
    # Bark: GET https://api.day.app/<key>/<title>/<body>?url=<deep_link>
    title = f"[{payload['event']}] {payload['poi']['name']}"
    body  = format_md(payload).replace("###", "").replace("\n\n", "\n")[:200]
    return f"https://api.day.app/<key>/{quote(title)}/{quote(body)}?url={quote(payload['deep_link'])}"
```

### 4.4.5 Telegram Bot

```python
def to_telegram(payload: dict) -> dict:
    return {
        "method": "sendMessage",
        "chat_id": "<chat_id>",
        "text": format_md(payload),
        "parse_mode": "Markdown",
        "reply_markup": {
            "inline_keyboard": [[{"text": "查看详情", "url": payload["deep_link"]}]]
        }
    }
```

### 4.4.6 通用 webhook（自定义接收方）

直接发 canonical JSON 格式（`Content-Type: application/json`），不 wrap。

**平台探测**（自动适配）：根据用户在设置页选择的「平台」字段决定走哪个 wrapper。
- `dingtalk` / `wechat_work` / `feishu` / `telegram` / `bark` / `custom`

---

## 4.5 发送端实现（`web/notifier.py`）

```python
"""Webhook sender — 异步发送 + 重试 + 去重。"""
from __future__ import annotations
import asyncio
import hmac
import hashlib
import json
import time
import logging
from typing import Any
import httpx

logger = logging.getLogger(__name__)

RETRY_DELAYS = [2, 8, 30]  # 指数退避

class WebhookSender:
    def __init__(self, url: str, secret: str | None = None,
                 platform: str = "custom", timeout: float = 10.0):
        self.url = url
        self.secret = secret
        self.platform = platform
        self.timeout = timeout

    def sign(self, body: bytes) -> str:
        if not self.secret:
            return ""
        return "sha256=" + hmac.new(self.secret.encode(), body, hashlib.sha256).hexdigest()

    def wrap(self, payload: dict) -> tuple[bytes, dict[str, str]]:
        """按 platform 包装；返回 (body_bytes, headers)"""
        if self.platform == "dingtalk":
            body = json.dumps(to_dingtalk(payload), ensure_ascii=False).encode()
        elif self.platform == "wechat_work":
            body = json.dumps(to_wechat_work(payload), ensure_ascii=False).encode()
        # ... 其他平台
        else:
            body = json.dumps(payload, ensure_ascii=False).encode()
        return body, {"Content-Type": "application/json; charset=utf-8"}

    async def send(self, payload: dict, dedup_key: str) -> tuple[bool, str]:
        body, headers = self.wrap(payload)
        headers.update({
            "User-Agent": "FliggySentinel/1.0",
            "X-Sentinel-Event": payload.get("event", ""),
            "X-Sentinel-Ts": payload.get("ts", ""),
            "X-Sentinel-Dedup-Key": dedup_key,
            "X-Sentinel-Signature": self.sign(body),
        })
        last_err = ""
        for attempt, delay in enumerate([0] + RETRY_DELAYS):
            if delay:
                await asyncio.sleep(delay)
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as cli:
                    r = await cli.post(self.url, content=body, headers=headers)
                if 200 <= r.status_code < 300:
                    return True, f"HTTP {r.status_code}: {r.text[:200]}"
                last_err = f"HTTP {r.status_code}: {r.text[:200]}"
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
            logger.warning(f"[webhook] attempt {attempt+1} failed: {last_err}")
        return False, last_err
```

---

## 4.6 发送流程集成到监控

```
for each round:
    for each alert in this_round_alerts:
        dedup_key = compute_dedup_key(alert.type, alert.context)
        if not exists_in_alerts_table(dedup_key, within_1h):
            INSERT INTO alerts (..., dedup_key, webhook_status='pending')
            enqueue_webhook_task(alert_id)
```

`enqueue_webhook_task` 把发送动作塞到 `asyncio.Queue`，监控主线程不等结果，写一行到 alerts 后继续扫下一 POI。

后台 webhook worker：
```
async def webhook_worker(q: asyncio.Queue):
    sender = WebhookSender(url, secret, platform)  # 从 config 读
    while True:
        alert_id = await q.get()
        payload = load_alert_payload(alert_id)
        dedup = load_dedup_key(alert_id)
        ok, resp = await sender.send(payload, dedup)
        update_alert_status(alert_id, "sent" if ok else "failed", resp)
```

---

## 4.7 失败处理

| 情况 | 处理 |
|---|---|
| webhook URL 未配置 | 跳过，alert.status 仍记为 `pending`（可在前端查看"待推送"队列） |
| HTTP 5xx | 重试 3 次，最终失败 alert.status = `failed`，告警界面标红 |
| HTTP 4xx（除 429） | 不重试，alert.status = `failed`，告警界面标红 + 显示响应体 |
| HTTP 429（限流） | 按 `Retry-After` header 退避；最多重试 3 次 |
| 网络超时 | 重试 3 次 |
| 签名验证失败（用户侧反馈） | 用户在设置页重置 webhook_secret |

**关键**：alert 表记录所有告警（含失败），用户可手动重发（前端按钮）。

---

## 4.8 配置项（来自 `config` 表）

```jsonc
{
  "webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=xxx",
  "webhook_secret": "f4k3-secr3t-k3y-1234",          // 可选；用于 HMAC 签名
  "webhook_platform": "dingtalk",                    // dingtalk / wechat_work / feishu / telegram / bark / custom
  "webhook_rules": {
    "non_self_new":     true,    // ⚠ 默认开
    "price_alert":      true,    // 默认开
    "self_missing":     true,    // 默认开
    "first_seller":     false,   // 默认关（刷屏）
    "shelf_error":      true,    // 默认开
    "cookie_refresh_failed": true  // 默认开
  },
  "webhook_dedup_window_hour": 1   // 同 dedup_key 1h 内只发 1 次
}
```

---

## 4.9 用户接收示例（钉钉群机器人配置）

1. 钉钉群 → 群设置 → 智能群助手 → 添加机器人 → 自定义
2. 安全设置：勾选「加签」（推荐）或「自定义关键词」（关键词：`飞猪哨兵` 或 `Fliggy`）
3. 复制 webhook URL（含 access_token）→ 填入 feizhu.19880913.xyz/settings 的 Webhook URL
4. 选择平台 = `dingtalk`
5. 点「测试推送」→ 钉钉群应收到一条测试消息
6. 确认 OK → 开始监控

---

## 4.10 安全检查清单

- [ ] Webhook URL 仅在 `config` 表加密存储？**No**——URL 本身是 secret，但 SQLite 文件就是 root-only（chmod 600），无需额外加密
- [ ] HTTPS Only？**Yes**——拒绝 `http://`（webhook 发送前校验）
- [ ] 签名校验开关？**Yes**——如果用户设了 secret，则发送时附带签名
- [ ] 重放攻击？**Mitigated**——dedup_key + 时间戳双重控制
- [ ] Body 大小限制？**Yes**——payload JSON 通常 < 2KB
- [ ] 用户敏感信息（cookie）泄漏到 webhook？**No**——payload 只含业务字段

---

## 4.11 接下来

- 实现 `web/notifier.py`（`WebhookSender` 类）
- 实现 `web/db.py` 的 `insert_alert()` + `update_alert_status()` + `dedup_exists()`
- 在 `code/fliggy_monitor.py` 加 alert 生成 + 推入队列
- 前端 `/settings` 加 webhook 配置 UI + 「测试推送」按钮
- 前端 `/alerts` 历史页 + 失败手动重发