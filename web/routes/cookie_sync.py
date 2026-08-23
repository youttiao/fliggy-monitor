"""Cookie sync endpoint — 供 Chrome 扩展 POST mtop cookies 上传到 VPS。

Auth: `X-Sync-Secret` header 必须等于 env var `COOKIE_SYNC_SECRET`。
不依赖 dashboard 的 session login，扩展不需要先登录飞猪哨兵。

收到后：
1. 验证 secret
2. 验证 body 含 4 个必需 cookie
3. 写到 /etc/fliggy-monitor/cookies.json（chmod 644，覆盖现有）

成功返回 {ok: true, saved: N, path: ...}；失败 401/400。
"""

from __future__ import annotations

import hmac
import json
import os
from pathlib import Path

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

REQUIRED_COOKIES = ("_m_h5_tk", "_m_h5_tk_enc", "cookie2", "t")
COOKIE_PATH = Path(
    os.getenv("FLIGGY_COOKIES", "/etc/fliggy-monitor/cookies.json")
)

router = APIRouter(prefix="/api/cookies", tags=["cookies"])


def _expected_secret() -> str:
    return os.getenv("COOKIE_SYNC_SECRET", "")


def _verify_secret(provided: str | None) -> bool:
    expected = _expected_secret()
    if not expected:
        return False
    if not provided:
        return False
    return hmac.compare_digest(provided.encode(), expected.encode())


def _write_cookies(cookies: dict[str, str]) -> tuple[int, str]:
    """写 cookies.json，chmod 644。返回 (写出的 cookie 数, 绝对路径)。"""
    COOKIE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(cookies, ensure_ascii=False, indent=2) + "\n"
    COOKIE_PATH.write_text(payload, encoding="utf-8")
    os.chmod(COOKIE_PATH, 0o644)
    return len(cookies), str(COOKIE_PATH)


@router.post("/sync")
async def sync_cookies(
    request: Request,
    x_sync_secret: str | None = Header(default=None, alias="X-Sync-Secret"),
):
    if not _verify_secret(x_sync_secret):
        return JSONResponse(
            {"ok": False, "error": "invalid or missing X-Sync-Secret"},
            status_code=401,
        )

    try:
        body = await request.json()
    except json.JSONDecodeError as e:
        return JSONResponse(
            {"ok": False, "error": f"invalid JSON body: {e}"},
            status_code=400,
        )

    if not isinstance(body, dict):
        return JSONResponse(
            {"ok": False, "error": "body must be a JSON object"},
            status_code=400,
        )

    cookies = body.get("cookies")
    if not isinstance(cookies, dict):
        return JSONResponse(
            {"ok": False, "error": "body.cookies must be a JSON object"},
            status_code=400,
        )

    missing = [k for k in REQUIRED_COOKIES if not cookies.get(k)]
    if missing:
        return JSONResponse(
            {"ok": False, "error": f"missing cookies: {missing}",
             "required": list(REQUIRED_COOKIES)},
            status_code=400,
        )

    # 保留扩展可能附带的其他 cookie（tfstk、isg 等风控字段），但只取 string 值
    clean = {k: str(v) for k, v in cookies.items() if isinstance(v, (str, int, float))}
    n, path = _write_cookies(clean)

    return {"ok": True, "saved": n, "path": path, "required": list(REQUIRED_COOKIES)}


@router.get("/health")
async def cookie_health(x_sync_secret: str | None = Header(default=None, alias="X-Sync-Secret")):
    """扩展可调：探测 secret 是否配好 + 当前 cookies.json 是否存在 + mtime。"""
    if not _verify_secret(x_sync_secret):
        return JSONResponse({"ok": False, "error": "auth"}, status_code=401)

    exists = COOKIE_PATH.exists()
    mtime = COOKIE_PATH.stat().st_mtime if exists else None
    return {
        "ok": True,
        "secret_configured": bool(_expected_secret()),
        "cookies_file": str(COOKIE_PATH),
        "exists": exists,
        "mtime": mtime,
    }