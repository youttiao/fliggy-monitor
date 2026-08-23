"""Seller 管理路由：列表 + 详情 + 编辑 + watch toggle（页面版）。"""

from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import auth as authmod
from .. import db as dbmod

router = APIRouter()
_TEMPLATES_DIR = __import__("pathlib").Path(__file__).resolve().parents[1] / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _conn(request: Request):
    return request.app.state.db


def _ctx(request: Request, **extra):
    conn = _conn(request)
    return {
        "request": request,
        "site": {
            "site_name": dbmod.get_config(conn, "site_name", "飞猪哨兵"),
            "self_seller_id": dbmod.get_config(conn, "self_seller_id", "2217592322543"),
        },
        "current_path": request.url.path,
        **extra,
    }


@router.get("/sellers", response_class=HTMLResponse)
async def sellers_list(
    request: Request,
    watched: Optional[str] = None,
    unidentified: Optional[str] = None,
    q: Optional[str] = None,
    _=Depends(authmod.require_login),
):
    conn = _conn(request)
    rows = dbmod.list_sellers(
        conn,
        watched_only=(watched == "1"),
        unidentified_only=(unidentified == "1"),
        search=q or None,
    )
    return templates.TemplateResponse(
        request,
        "sellers_list.html",
        _ctx(request, sellers=rows, watched=watched, unidentified=unidentified, q=q or ""),
    )


@router.get("/sellers/{seller_id}", response_class=HTMLResponse)
async def seller_detail_page(
    request: Request,
    seller_id: str,
    _=Depends(authmod.require_login),
):
    conn = _conn(request)
    seller = dbmod.seller_detail(conn, seller_id)
    if not seller:
        return templates.TemplateResponse(
            request,
            "error.html",
            _ctx(request, status_code=404, detail=f"卖家 {seller_id} 未在数据库中"),
            status_code=404,
        )
    pois = dbmod.seller_pois(conn, seller_id)
    recent = dbmod.seller_recent_cells(conn, seller_id, limit=30)
    return templates.TemplateResponse(
        request,
        "seller_detail.html",
        _ctx(request, seller=seller, pois=pois, recent=recent),
    )


@router.post("/sellers/{seller_id}")
async def seller_edit_submit(
    request: Request,
    seller_id: str,
    display_name: str = Form(""),
    is_watched: str | None = Form(None),
    notes: str = Form(""),
    tags: str = Form(""),
    priority: int = Form(0),
    _=Depends(authmod.require_login),
):
    conn = _conn(request)
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    dbmod.upsert_seller_enrichment(
        conn,
        seller_id=seller_id,
        display_name=display_name.strip() or None,
        is_watched=bool(is_watched),
        notes=notes.strip() or None,
        tags=tag_list,
        priority=max(0, min(3, priority)),
    )
    return RedirectResponse(url=f"/sellers/{seller_id}?saved=1", status_code=303)