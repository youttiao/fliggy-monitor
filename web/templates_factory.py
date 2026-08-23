"""统一的 Jinja2Templates 工厂：所有路由模块用同一个实例，过滤器 / globals 全局一致。

每个路由文件自己再 `Jinja2Templates(...)` 会导致 filters 不互通，
比如 `web.server` 注册了 `price` 但 `web.routes.pages` 看不到。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.templating import Jinja2Templates

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def _fmt_price_cents(price_int: Any, price_dec: Any = "") -> str:
    """渲染精确到分的价格（丢掉「起」后缀）。

    例：("58", ".5") → "58.50"；("128", "") → "128.00"；None → "?"。
    """
    if not price_int:
        return "?"
    raw = str(price_dec or "").replace(".", "").strip()
    cents = (raw + "00")[:2]
    return f"{price_int}.{cents}"


def make_templates() -> Jinja2Templates:
    t = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    t.env.globals["app_name"] = "飞猪哨兵"
    t.env.globals["self_seller_id"] = "2217592322543"
    t.env.filters["price"] = _fmt_price_cents
    return t