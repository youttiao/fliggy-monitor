"""页面路由：HTML（Jinja2）。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import auth as authmod
from .. import db as dbmod

router = APIRouter()
_TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _conn(request: Request):
    return request.app.state.db


def _site_config(conn) -> dict[str, Any]:
    return {
        "site_name": dbmod.get_config(conn, "site_name", "飞猪哨兵"),
        "site_timezone": dbmod.get_config(conn, "site_timezone", "Asia/Shanghai"),
        "self_seller_id": dbmod.get_config(conn, "self_seller_id", "2217592322543"),
        "self_seller_name": dbmod.get_config(conn, "self_seller_name", ""),
        "webhook_url": dbmod.get_config(conn, "webhook_url"),
        "webhook_rules": dbmod.get_config(conn, "webhook_rules", {}),
    }


def _ctx(request: Request, **extra: Any) -> dict[str, Any]:
    conn = _conn(request)
    return {
        "request": request,
        "site": _site_config(conn),
        "current_path": request.url.path,
        **extra,
    }


# ──────────────────────────────────────────────────────────────
# Auth
# ──────────────────────────────────────────────────────────────


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    # 已登录则跳走
    sid = authmod.read_session_cookie(request)
    if authmod.get_session(_conn(request), sid):
        return RedirectResponse(url=next or "/", status_code=302)
    locked_for = authmod.remaining_lockout_seconds(_conn(request), authmod.client_ip(request))
    return templates.TemplateResponse(
        request,
        "login.html",
        _ctx(request, next=next, locked_for=locked_for),
    )


@router.post("/login")
async def login_submit(
    request: Request,
    password: str = Form(""),
    next: str = Form("/"),
):
    conn = _conn(request)
    ip = authmod.client_ip(request)
    ua = request.headers.get("user-agent", "")

    if authmod.is_locked_out(conn, ip):
        authmod.record_failure(conn, ip=ip, ua=ua, reason="locked")
        locked_for = authmod.remaining_lockout_seconds(conn, ip)
        return templates.TemplateResponse(
            request,
            "login.html",
            _ctx(request, error=f"锁定中，请 {locked_for}s 后重试", next=next, locked_for=locked_for),
            status_code=429,
        )

    if not authmod.verify_password(password):
        authmod.record_failure(conn, ip=ip, ua=ua, reason="wrong_password")
        locked_for = authmod.remaining_lockout_seconds(conn, ip)
        err = "密码错误"
        if locked_for > 0:
            err += f"（{locked_for}s 内重试 5 次会被锁定）"
        return templates.TemplateResponse(
            request,
            "login.html",
            _ctx(request, error=err, next=next, locked_for=locked_for),
            status_code=401,
        )

    authmod.clear_failures(conn, ip)
    sid = authmod.create_session(conn, ip=ip, ua=ua)
    target = next if next.startswith("/") else "/"
    resp = RedirectResponse(url=target, status_code=303)
    authmod.set_session_cookie(resp, sid, secure=request.url.scheme == "https")
    return resp


@router.post("/logout")
async def logout(request: Request):
    sid = authmod.read_session_cookie(request)
    if sid:
        authmod.destroy_session(_conn(request), sid)
    resp = RedirectResponse(url="/login", status_code=303)
    authmod.clear_session_cookie(resp)
    return resp


# ──────────────────────────────────────────────────────────────
# Dashboard
# ──────────────────────────────────────────────────────────────


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, _=Depends(authmod.require_login)):
    conn = _conn(request)
    latest = dbmod.latest_round(conn)
    if not latest:
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            _ctx(request, no_data=True, latest=None, summary=[]),
        )
    summary = dbmod.poi_summary(conn, latest["id"])
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        _ctx(request, no_data=False, latest=latest, summary=summary),
    )


# ──────────────────────────────────────────────────────────────
# POI detail
# ──────────────────────────────────────────────────────────────


@router.get("/poi/{poi_id}", response_class=HTMLResponse)
async def poi_detail(
    request: Request,
    poi_id: str,
    cell_type: str | None = None,
    seller_id: str | None = None,
    focus: str | None = None,
    _=Depends(authmod.require_login),
):
    conn = _conn(request)
    latest = dbmod.latest_round(conn)
    if not latest:
        return RedirectResponse(url="/", status_code=303)

    poi_row = dbmod.query(conn, "SELECT * FROM pois WHERE poi_id = ?", (poi_id,), one=True)
    if not poi_row:
        return templates.TemplateResponse(
            request,
            "error.html",
            _ctx(request, status_code=404, detail=f"POI {poi_id} 不在监控列表"),
            status_code=404,
        )

    cells = dbmod.poi_cells(
        conn, poi_id, latest["id"],
        cell_type=cell_type, seller_id=seller_id,
    )
    # 分组：cell_type → list[cells]
    grouped: dict[str, list[dict]] = {}
    for c in cells:
        ct = c["cell_type"] or "未分类"
        grouped.setdefault(ct, []).append(dict(c))

    # cell_type 列表（按出现顺序，但自营 > 联票 > 周边放前面）
    type_priority = ["门票套餐", "景点门票", "园内项目", "景区联票", "周边景区门票", "周边景区套票", "未分类"]
    ordered_types = sorted(grouped.keys(), key=lambda t: (type_priority.index(t) if t in type_priority else 99, t))

    return templates.TemplateResponse(
        request,
        "poi_detail.html",
        _ctx(
            request,
            poi=poi_row,
            latest=latest,
            grouped=grouped,
            ordered_types=ordered_types,
            cell_type=cell_type,
            seller_id=seller_id,
            focus=focus,
        ),
    )


# ──────────────────────────────────────────────────────────────
# SKU detail
# ──────────────────────────────────────────────────────────────


@router.get("/poi/{poi_id}/sku/{item_id}/{sku_id}", response_class=HTMLResponse)
async def sku_detail(
    request: Request,
    poi_id: str,
    item_id: str,
    sku_id: str,
    _=Depends(authmod.require_login),
):
    conn = _conn(request)
    latest = dbmod.latest_round(conn)
    if not latest:
        return RedirectResponse(url="/", status_code=303)

    cur = dbmod.query(
        conn,
        """
        SELECT c.*, s.seller_name, s.shop_jump_url, s.service_stats,
               e.display_name AS enrichment_name, e.is_watched AS watched,
               e.notes AS enrichment_notes
        FROM cells_snapshot c
        LEFT JOIN sellers s ON s.seller_id = c.seller_id
        LEFT JOIN seller_enrichment e ON e.seller_id = c.seller_id
        WHERE c.round_id = ? AND c.poi_id = ? AND c.item_id = ? AND c.sku_id = ?
        """,
        (latest["id"], poi_id, item_id, sku_id),
        one=True,
    )
    if not cur:
        return templates.TemplateResponse(
            request,
            "error.html",
            _ctx(request, status_code=404, detail="本轮未抓到该 SKU"),
            status_code=404,
        )

    history = dbmod.sku_history(conn, poi_id, item_id, sku_id, limit=30)
    return templates.TemplateResponse(
        request,
        "sku_detail.html",
        _ctx(request, cell=cur, history=history, latest=latest),
    )


# ──────────────────────────────────────────────────────────────
# Alerts
# ──────────────────────────────────────────────────────────────


@router.get("/alerts", response_class=HTMLResponse)
async def alerts_page(
    request: Request,
    type: str | None = None,
    severity: str | None = None,
    _=Depends(authmod.require_login),
):
    conn = _conn(request)
    where = []
    params: list[Any] = []
    if type:
        where.append("type = ?")
        params.append(type)
    if severity:
        where.append("severity = ?")
        params.append(severity)
    sql = f"SELECT * FROM alerts {'WHERE ' + ' AND '.join(where) if where else ''} ORDER BY ts DESC LIMIT 200"
    rows = dbmod.query(conn, sql, params) or []
    return templates.TemplateResponse(
        request,
        "alerts.html",
        _ctx(request, alerts=rows, type=type, severity=severity),
    )


# ──────────────────────────────────────────────────────────────
# Settings
# ──────────────────────────────────────────────────────────────


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, _=Depends(authmod.require_login)):
    conn = _conn(request)
    cfg = {
        "webhook_url": dbmod.get_config(conn, "webhook_url"),
        "webhook_secret": dbmod.get_config(conn, "webhook_secret"),
        "webhook_platform": dbmod.get_config(conn, "webhook_platform", "auto"),
        "webhook_rules": dbmod.get_config(conn, "webhook_rules", {}),
        "webhook_recent": dbmod.get_config(conn, "webhook_recent", []),
        "self_seller_id": dbmod.get_config(conn, "self_seller_id"),
        "self_seller_name": dbmod.get_config(conn, "self_seller_name"),
        "polling_sec": dbmod.get_config(conn, "polling_sec", 1800),
        "site_name": dbmod.get_config(conn, "site_name", "飞猪哨兵"),
    }
    return templates.TemplateResponse(request, "settings.html", _ctx(request, cfg=cfg))


@router.post("/settings")
async def settings_submit(
    request: Request,
    webhook_url: str = Form(""),
    webhook_secret: str = Form(""),
    webhook_platform: str = Form("auto"),
    self_seller_id: str = Form(""),
    self_seller_name: str = Form(""),
    polling_sec: int = Form(1800),
    rule_non_self_new: str | None = Form(None),
    rule_price_alert: str | None = Form(None),
    rule_self_missing: str | None = Form(None),
    rule_first_seller: str | None = Form(None),
    rule_shelf_error: str | None = Form(None),
    rule_cookie_refresh_failed: str | None = Form(None),
    _=Depends(authmod.require_login),
):
    conn = _conn(request)
    dbmod.set_config(conn, "webhook_url", webhook_url or None)
    dbmod.set_config(conn, "webhook_secret", webhook_secret or None)
    dbmod.set_config(conn, "webhook_platform", webhook_platform)
    dbmod.set_config(conn, "self_seller_id", self_seller_id)
    dbmod.set_config(conn, "self_seller_name", self_seller_name)
    dbmod.set_config(conn, "polling_sec", max(300, int(polling_sec)))
    dbmod.set_config(conn, "webhook_rules", {
        "non_self_new": bool(rule_non_self_new),
        "price_alert": bool(rule_price_alert),
        "self_missing": bool(rule_self_missing),
        "first_seller": bool(rule_first_seller),
        "shelf_error": bool(rule_shelf_error),
        "cookie_refresh_failed": bool(rule_cookie_refresh_failed),
    })
    return RedirectResponse(url="/settings?saved=1", status_code=303)


# ──────────────────────────────────────────────────────────────
# Healthz
# ──────────────────────────────────────────────────────────────


@router.get("/healthz")
async def healthz(request: Request):
    conn = _conn(request)
    try:
        return dbmod.healthz(conn)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)