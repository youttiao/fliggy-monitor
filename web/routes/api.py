"""JSON API：dashboard 数据、Seller watch toggle、test webhook、manual round trigger。"""

from __future__ import annotations

import json
import subprocess
import time
from typing import Any

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse

from .. import auth as authmod
from .. import db as dbmod
from .. import notifier as notif

router = APIRouter(prefix="/api")


def _conn(request: Request):
    return request.app.state.db


# 手动触发 round 的 rate-limit：进程内 60s 一次。多次刷新页面/点击也防爆。
_last_trigger: dict[str, float] = {"ts": 0.0}
_MANUAL_COOLDOWN_S = 60


@router.get("/dashboard/summary")
async def dashboard_summary(request: Request, _=Depends(authmod.require_login)):
    conn = _conn(request)
    latest = dbmod.latest_round(conn)
    if not latest:
        return {"latest": None, "pois": [], "kpis": {}}
    summary = dbmod.poi_summary(conn, latest["id"])
    return {
        "latest": dict(latest),
        "pois": [dict(r) for r in summary],
        "kpis": {
            "cells_total": sum((r["cells_total"] or 0) for r in summary),
            "cells_self": sum((r["cells_self"] or 0) for r in summary),
            "cells_non_self": sum((r["cells_non_self"] or 0) for r in summary),
            "non_self_sellers": sum((r["non_self_sellers"] or 0) for r in summary),
        },
    }


@router.post("/sellers/{seller_id}/watch")
async def toggle_watch(
    request: Request,
    seller_id: str,
    body: dict[str, Any] = Body(...),
    _=Depends(authmod.require_login),
):
    conn = _conn(request)
    watched = bool(body.get("watched", False))
    # 读取已有 enrichment（保留 display_name / notes / tags / priority）
    existing = dbmod.query(
        conn, "SELECT * FROM seller_enrichment WHERE seller_id = ?", (seller_id,), one=True,
    )
    dbmod.upsert_seller_enrichment(
        conn,
        seller_id=seller_id,
        display_name=existing["display_name"] if existing else None,
        is_watched=watched,
        notes=existing["notes"] if existing else None,
        tags=json.loads(existing["tags"]) if existing and existing["tags"] else None,
        priority=(existing["priority"] if existing else 0) or 0,
    )
    return {"ok": True, "seller_id": seller_id, "watched": watched}


@router.post("/shelves/watch")
async def toggle_shelf_watch(
    request: Request,
    body: dict[str, Any] = Body(...),
    _=Depends(authmod.require_login),
):
    """货架级别关注 toggle —— 只对关注的 (poi,item,sku) 推 webhook。

    body: {poi_id: str, item_id: str, sku_id: str, watched: bool, notes?: str}
    """
    conn = _conn(request)
    poi_id = (body.get("poi_id") or "").strip()
    item_id = (body.get("item_id") or "").strip()
    sku_id = (body.get("sku_id") or "").strip()
    watched = bool(body.get("watched", False))
    notes = (body.get("notes") or None)
    if not (poi_id and item_id and sku_id):
        return JSONResponse(
            {"ok": False, "error": "poi_id / item_id / sku_id 必填"},
            status_code=400,
        )
    dbmod.upsert_shelf_watch(
        conn,
        poi_id=poi_id, item_id=item_id, sku_id=sku_id,
        is_watched=watched, notes=notes,
    )
    return {"ok": True, "poi_id": poi_id, "item_id": item_id,
            "sku_id": sku_id, "watched": watched}


@router.get("/shelves/watched")
async def list_watched_shelves(
    request: Request,
    poi_id: str | None = None,
    _=Depends(authmod.require_login),
):
    """列出所有被关注的货架（可按 POI 过滤）。"""
    conn = _conn(request)
    where = ["w.is_watched = 1"]
    params: list[Any] = []
    if poi_id:
        where.append("w.poi_id = ?")
        params.append(poi_id)
    sql = f"""
        SELECT w.poi_id, w.item_id, w.sku_id, w.notes,
               w.created_at, w.updated_at,
               c.sku_name, c.price_int, c.price_dec, c.seller_id, c.is_self
        FROM shelf_watch w
        LEFT JOIN cells_snapshot c
               ON c.poi_id = w.poi_id AND c.item_id = w.item_id AND c.sku_id = w.sku_id
              AND c.round_id = (SELECT id FROM rounds ORDER BY started_at DESC LIMIT 1)
        {'WHERE ' + ' AND '.join(where) if where else ''}
        ORDER BY w.updated_at DESC
    """
    rows = dbmod.query(conn, sql, params) or []
    return [dict(r) for r in rows]


@router.post("/webhook/test")
async def webhook_test(request: Request, body: dict[str, Any] = Body(default=None), _=Depends(authmod.require_login)):
    conn = _conn(request)
    url = dbmod.get_config(conn, "webhook_url")
    if not url or url == "null":
        return JSONResponse({"ok": False, "error": "未配置 webhook_url"}, status_code=400)
    secret = dbmod.get_config(conn, "webhook_secret")
    platform = dbmod.get_config(conn, "webhook_platform", "auto")
    mode = dbmod.get_config(conn, "webhook_mode", "shelf_report")
    kind = (body or {}).get("kind") or ("report" if mode == "shelf_report" else "single")

    sender = notif.WebhookSender(url, secret=secret if secret and secret != "null" else None,
                                  platform=None if platform == "auto" else platform)

    if kind == "report":
        report = {
            "round_id": "rDEMO000000",
            "ts": "2026-08-23 14:00",
            "dashboard_url": str(request.base_url).rstrip("/") + "/poi/1345",
            "total_pois": 3,
            "non_self_pois": 2,
            "groups": [
                {
                    "poi_id": "1345", "poi_name": "圆明园",
                    "has_non_self": True, "non_self_count": 2, "self_count": 1,
                    "shelves": [
                        {"sku_name": "大门门票+讲解", "item_id": "1065739764221",
                         "sku_id": "6276363111198", "price_int": "58",
                         "price_dec": ".00", "price_suffix": "起", "watched": True},
                        {"sku_name": "圆明园+颐和园联票", "item_id": "1065739764221",
                         "sku_id": "6276363111200", "price_int": "88",
                         "price_dec": ".50", "price_suffix": "起", "watched": True},
                    ],
                },
                {
                    "poi_id": "1544", "poi_name": "天坛",
                    "has_non_self": False, "non_self_count": 0, "self_count": 5,
                    "shelves": [],
                },
                {
                    "poi_id": "2301", "poi_name": "颐和园",
                    "has_non_self": True, "non_self_count": 1, "self_count": 0,
                    "shelves": [
                        {"sku_name": "大门门票", "item_id": "1065000111001",
                         "sku_id": "6276000222002", "price_int": "30",
                         "price_dec": ".00", "price_suffix": "起", "watched": True},
                    ],
                },
            ],
        }
        result = sender.send_report(report)
    else:
        payload = notif.render_alert(
            alert_type="non_self_new",
            severity="warning",
            poi_id="1345",
            poi_name="圆明园",
            sku_name="成人票（含讲解）",
            seller_id="2217592322543",
            seller_display="测试商家",
            watched=False,
            price_int="58",
            price_dec=".00",
            price_suffix="起",
            ts="now",
            dashboard_url=str(request.base_url).rstrip("/") + "/poi/1345",
        )
        result = sender.send(payload)

    return {"ok": result.ok, "status_code": result.status_code,
            "response": result.response, "error": result.error, "kind": kind}


@router.get("/rounds")
async def list_rounds(request: Request, limit: int = 20, _=Depends(authmod.require_login)):
    conn = _conn(request)
    rows = dbmod.list_rounds(conn, limit=limit)
    return [dict(r) for r in rows]


@router.post("/rounds/trigger")
async def trigger_round_now(_=Depends(authmod.require_login)):
    """手动立即触发一轮抓取。

    通过 `systemctl start fliggy-monitor.service` 启动 oneshot 服务。
    防滥用：60s cooldown + 服务已在跑时拒绝。
    """
    now = time.time()
    since = now - _last_trigger["ts"]
    if since < _MANUAL_COOLDOWN_S:
        wait = int(_MANUAL_COOLDOWN_S - since)
        return JSONResponse(
            {"ok": False, "error": f"刚已触发（{wait}s 前），请稍等再试"},
            status_code=429,
        )

    try:
        active = subprocess.run(
            ["/usr/bin/systemctl", "is-active", "fliggy-monitor.service"],  # noqa: S607
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except Exception as e:
        active = f"unknown ({e})"
    if active == "active":
        return JSONResponse(
            {"ok": False, "error": "fliggy-monitor 正在运行，无需再触发"},
            status_code=409,
        )

    try:
        # Type=oneshot 服务 start 默认会等到命令跑完（~40s），用 --no-block 立刻返回
        subprocess.run(
            ["/usr/bin/systemctl", "start", "--no-block", "fliggy-monitor.service"],  # noqa: S607
            check=True, timeout=5, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as e:
        return JSONResponse(
            {"ok": False, "error": f"systemctl start 失败：{e.stderr or e}"},
            status_code=500,
        )
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    _last_trigger["ts"] = now
    return {
        "ok": True,
        "started_at": int(now),
        "expected_duration_s": 40,
        "message": "已触发，等约 40s 后 dashboard 刷新即可看到新数据",
    }