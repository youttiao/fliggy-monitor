"""登录鉴权：硬编码密码 + 服务端 session store + IP /24 失败锁定。

设计要点（用户明确要求 v1 简化）：
- 密码后端硬编码 `xuran888`（可被 `FLIGGY_ADMIN_PASSWORD` 环境变量覆盖）
- 不用 bcrypt（v1 单密码，没必要哈希成本）
- 仍然走 session cookie + 服务端 web_sessions 表，便于未来无痛升级
- IP /24 失败计数：5 次 / 10 分钟 → 锁定 10 分钟
- 滑动续期：每次请求若 last_seen_at 距今 > 1h → 重置 expires_at = now + 7d
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import sqlite3
from fastapi import Request, Response

from . import db as dbmod

# v1 硬编码；将来上 bcrypt 时改为读 config 表
ADMIN_PASSWORD = os.getenv("FLIGGY_ADMIN_PASSWORD", "xuran888")
SESSION_COOKIE = "fliggy_sid"
SESSION_TTL = timedelta(days=7)
LOCKOUT_THRESHOLD = 5
LOCKOUT_WINDOW = timedelta(minutes=10)


def verify_password(input_pwd: str) -> bool:
    """constant-time 比较。"""
    if not input_pwd:
        return False
    a = input_pwd.encode("utf-8")
    b = ADMIN_PASSWORD.encode("utf-8")
    return hmac.compare_digest(a, b)


def client_ip(request: Request) -> str:
    """优先 X-Forwarded-For（Caddy 后），fallback 直接对端。

    生产环境强制 Caddy 设 X-Forwarded-For；这里用第一段。
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "0.0.0.0"


def ip_prefix_24(ip: str) -> str:
    """取 IPv4 /24 前缀。IPv6 暂取前 4 段（粗粒度即可）。"""
    if not ip:
        return "unknown"
    if ":" in ip:  # IPv6
        parts = ip.split(":")
        return ":".join(parts[:4]) or "ipv6"
    parts = ip.split(".")
    if len(parts) == 4:
        return ".".join(parts[:3])
    return ip


def ua_family(ua: str | None) -> str:
    """只记浏览器 family，不存完整 UA（隐私）。"""
    if not ua:
        return "unknown"
    ua = ua.lower()
    for token in ("edg/", "opr/", "chrome/", "safari/", "firefox/", "curl/", "wget/", "python"):
        idx = ua.find(token)
        if idx >= 0:
            return token.rstrip("/")
    return "other"


# ──────────────────────────────────────────────────────────────
# Session CRUD
# ──────────────────────────────────────────────────────────────


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def create_session(conn: sqlite3.Connection, *, ip: str, ua: str) -> str:
    sid = secrets.token_urlsafe(32)
    now = _utcnow()
    expires = now + SESSION_TTL
    with dbmod.transaction(conn):
        dbmod.execute(
            conn,
            """
            INSERT INTO web_sessions
                (sid, created_at, last_seen_at, expires_at, user_agent, ip_prefix, is_active)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            """,
            (sid, now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds"),
             expires.isoformat(timespec="seconds"), ua, ip_prefix_24(ip)),
        )
    return sid


def get_session(conn: sqlite3.Connection, sid: str) -> Optional[sqlite3.Row]:
    if not sid:
        return None
    row = dbmod.query(
        conn,
        "SELECT * FROM web_sessions WHERE sid = ? AND is_active = 1",
        (sid,),
        one=True,
    )
    if not row:
        return None
    expires = datetime.fromisoformat(row["expires_at"])
    if expires < _utcnow():
        destroy_session(conn, sid)
        return None
    return row


def touch_session(conn: sqlite3.Connection, sid: str) -> None:
    """滑动续期：距离 last_seen_at > 1h 才动 expires_at，避免 DB 频繁写。"""
    now = _utcnow()
    row = dbmod.query(conn, "SELECT last_seen_at FROM web_sessions WHERE sid = ?", (sid,), one=True)
    if not row:
        return
    last = datetime.fromisoformat(row["last_seen_at"])
    new_expires = now + SESSION_TTL
    if now - last > timedelta(hours=1):
        dbmod.execute(
            conn,
            "UPDATE web_sessions SET last_seen_at = ?, expires_at = ? WHERE sid = ?",
            (now.isoformat(timespec="seconds"), new_expires.isoformat(timespec="seconds"), sid),
        )
    else:
        dbmod.execute(
            conn,
            "UPDATE web_sessions SET last_seen_at = ? WHERE sid = ?",
            (now.isoformat(timespec="seconds"), sid),
        )


def destroy_session(conn: sqlite3.Connection, sid: str) -> None:
    dbmod.execute(conn, "UPDATE web_sessions SET is_active = 0 WHERE sid = ?", (sid,))


# ──────────────────────────────────────────────────────────────
# 失败计数 / 锁定
# ──────────────────────────────────────────────────────────────


def record_failure(conn: sqlite3.Connection, *, ip: str, ua: str, reason: str) -> None:
    now = _utcnow()
    dbmod.execute(
        conn,
        "INSERT INTO login_failures (ip_prefix, ts, user_agent_family, reason) VALUES (?, ?, ?, ?)",
        (ip_prefix_24(ip), now.isoformat(timespec="seconds"), ua_family(ua), reason),
    )


def is_locked_out(conn: sqlite3.Connection, ip: str) -> bool:
    cutoff = (_utcnow() - LOCKOUT_WINDOW).isoformat(timespec="seconds")
    row = dbmod.query(
        conn,
        "SELECT COUNT(*) AS n FROM login_failures WHERE ip_prefix = ? AND ts > ?",
        (ip_prefix_24(ip), cutoff),
        one=True,
    )
    return bool(row and row["n"] >= LOCKOUT_THRESHOLD)


def clear_failures(conn: sqlite3.Connection, ip: str) -> None:
    dbmod.execute(conn, "DELETE FROM login_failures WHERE ip_prefix = ?", (ip_prefix_24(ip),))


def remaining_lockout_seconds(conn: sqlite3.Connection, ip: str) -> int:
    """返回还需要等多久解锁（秒）。给前端展示。"""
    cutoff = (_utcnow() - LOCKOUT_WINDOW).isoformat(timespec="seconds")
    row = dbmod.query(
        conn,
        "SELECT ts FROM login_failures WHERE ip_prefix = ? AND ts > ? ORDER BY ts ASC LIMIT 1",
        (ip_prefix_24(ip), cutoff),
        one=True,
    )
    if not row:
        return 0
    first = datetime.fromisoformat(row["ts"])
    unlock_at = first + LOCKOUT_WINDOW
    delta = (unlock_at - _utcnow()).total_seconds()
    return max(0, int(delta))


# ──────────────────────────────────────────────────────────────
# Cookie 工具
# ──────────────────────────────────────────────────────────────


def set_session_cookie(response: Response, sid: str, *, secure: bool) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=sid,
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,
        secure=secure,
        samesite="strict",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def read_session_cookie(request: Request) -> str:
    return request.cookies.get(SESSION_COOKIE, "")


def session_fingerprint_hash(sid: str) -> str:
    """调试用：截断 sid 的 sha256。不暴露 sid 给前端。"""
    return hashlib.sha256(sid.encode()).hexdigest()[:12]


# ──────────────────────────────────────────────────────────────
# 验证装饰器（FastAPI 依赖）
# ──────────────────────────────────────────────────────────────


async def require_login(request: Request) -> sqlite3.Row:
    """FastAPI Depends：返回有效 session row，否则 302 到 /login。"""
    from fastapi.responses import RedirectResponse
    conn = request.app.state.db
    sid = read_session_cookie(request)
    sess = get_session(conn, sid) if sid else None
    if not sess:
        raise _RedirectToLogin()
    touch_session(conn, sid)
    # 把 sid 写到 request.state 供日志使用
    request.state.sid_prefix = session_fingerprint_hash(sid)
    return sess


class _RedirectToLogin(Exception):
    pass


def redirect_to_login() -> "RedirectResponse":  # type: ignore[name-defined]
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/login?next=/", status_code=302)