"""FastAPI app：lifespan + middleware + 路由挂载 + 静态文件 + 模板。

启动：
    uvicorn web.server:app --host 127.0.0.1 --port 8080
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import auth as authmod
from . import db as dbmod
from .routes import api_router, cookie_sync_router, extensions_router, pages_router, sellers_router
from .templates_factory import make_templates

ROOT = Path(__file__).resolve().parent
TEMPLATES_DIR = ROOT / "templates"
STATIC_DIR = ROOT / "static"
DB_PATH = os.getenv("FLIGGY_DB", "/opt/fliggy-monitor/data/monitor.db")

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("web.server")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """开 DB → 健康检查 → yield → 关 DB。"""
    log.info("lifespan.start db=%s", DB_PATH)
    conn = dbmod.connect(DB_PATH)
    app.state.db = conn
    try:
        h = dbmod.healthz(conn)
        log.info("lifespan.healthz %s", h)
    except Exception as e:
        log.exception("lifespan.healthz.fail: %s", e)
    yield
    log.info("lifespan.stop")
    try:
        conn.close()
    except Exception:
        pass


app = FastAPI(
    title="飞猪哨兵 · Fliggy Sentinel",
    description="内部 dashboard + 告警工具",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None,  # 内部工具，禁 swagger
    redoc_url=None,
    openapi_url=None,
)


# 静态文件
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# 模板（单一实例：所有路由模块共享过滤器 / globals）
templates = make_templates()


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    """基础安全头 + 全局 X-Content-Type-Options / X-Frame-Options。"""
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    return response


@app.exception_handler(404)
async def not_found(request: Request, exc):
    if request.url.path.startswith("/api/") or request.url.path.startswith("/static/"):
        return JSONResponse({"error": "not_found"}, status_code=404)
    return templates.TemplateResponse(
        request,
        "error.html",
        {"status_code": 404, "detail": "页面没找到",
         "current_path": request.url.path, "site": {"site_name": "飞猪哨兵"}},
        status_code=404,
    )


@app.exception_handler(500)
async def server_error(request: Request, exc):
    log.exception("unhandled error on %s: %s", request.url.path, exc)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "internal"}, status_code=500)
    return templates.TemplateResponse(
        request,
        "error.html",
        {"status_code": 500, "detail": "服务器开了小差",
         "current_path": request.url.path, "site": {"site_name": "飞猪哨兵"}},
        status_code=500,
    )


@app.exception_handler(authmod._RedirectToLogin)
async def _redirect_to_login(request: Request, exc: authmod._RedirectToLogin):
    """require_login 抛 _RedirectToLogin → 302 到 /login。"""
    from fastapi.responses import RedirectResponse
    nxt = request.url.path
    if request.url.query:
        nxt += "?" + request.url.query
    return RedirectResponse(url=f"/login?next={nxt}", status_code=302)


# 路由挂载（顺序无关）
app.include_router(pages_router)
app.include_router(api_router)
app.include_router(sellers_router)
app.include_router(cookie_sync_router)
app.include_router(extensions_router)