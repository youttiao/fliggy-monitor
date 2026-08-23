"""统一的 Jinja2Templates 工厂：所有路由模块用同一个实例，过滤器 / globals 全局一致。

每个路由文件自己再 `Jinja2Templates(...)` 会导致 filters 不互通，
比如 `web.server` 注册了 `price` 但 `web.routes.pages` 看不到。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi.templating import Jinja2Templates

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

_BJ = ZoneInfo("Asia/Shanghai")


def _fmt_price_cents(price_int: Any, price_dec: Any = "") -> str:
    """渲染精确到分的价格（丢掉「起」后缀）。

    例：("58", ".5") → "58.50"；("128", "") → "128.00"；None → "?"。
    """
    if not price_int:
        return "?"
    raw = str(price_dec or "").replace(".", "").strip()
    cents = (raw + "00")[:2]
    return f"{price_int}.{cents}"


def _fmt_bj(iso_str: Any, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """UTC ISO → Asia/Shanghai 渲染。

    DB 内部统一存 UTC ISO `2026-08-23T17:10:14+00:00`，前端模板调用 `{{ x.ts | bj }}`
    拿到 `2026-08-24 01:10:14`。空值原样返回 `""`；解析失败 / 裸字符串无 tz 信息
    时回退原值，不瞎猜（避免对历史脏数据制造误导）。
    """
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(str(iso_str).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return str(iso_str)
        return dt.astimezone(_BJ).strftime(fmt)
    except Exception:
        return str(iso_str)


def _fmt_bj_date(iso_str: Any) -> str:
    return _fmt_bj(iso_str, fmt="%Y-%m-%d")


def make_templates() -> Jinja2Templates:
    t = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    t.env.globals["app_name"] = "飞猪哨兵"
    t.env.globals["self_seller_id"] = "2217592322543"
    t.env.filters["price"] = _fmt_price_cents
    t.env.filters["bj"] = _fmt_bj
    t.env.filters["bjdate"] = _fmt_bj_date
    return t