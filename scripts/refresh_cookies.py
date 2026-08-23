#!/usr/bin/env python3
"""Cookie 续期：用 Playwright 打开飞猪 H5 → 等 token 刷新 → 把 cookies 写到 /etc/fliggy-monitor/cookies.json。

设计要点：
- 后台跑（headed=False）；目标 URL 不需要用户交互
- 等待 `_m_h5_tk` 出现且包含有效 `_expiry`（> now+1h 才算成功）
- 失败重试 3 次，每次退避 5/15/30s
- 写到 `/etc/fliggy-monitor/cookies.json`（与代码 / 部署约定一致）；失败时回退到当前工作目录
- 记录历史到 `cookies_history` 表（前缀 + 时间，**不存完整 token**）

调用：
    python3 scripts/refresh_cookies.py                 # 默认路径
    python3 scripts/refresh_cookies.py --target /path  # 自定义 cookies 输出路径
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from web.db import connect  # noqa: E402

DEFAULT_COOKIE_PATH = os.getenv("FLIGGY_COOKIES", "/etc/fliggy-monitor/cookies.json")
DEFAULT_DB_PATH = os.getenv("FLIGGY_DB", "/opt/fliggy-monitor/data/monitor.db")
DEFAULT_TARGET_URL = os.getenv(
    "FLIGGY_TARGET_URL",
    "https://h5api.m.taobao.com/h5/mtop.trip.serverless.api.gateway/2.0/?jsv=2.5.1&appKey=12574478&t=1700000000000&sign=xxx&v=1.0&type=originaljson&dataType=jsonp&data=%7B%22serverless%22%3A%22tripsv2%22%2C%22feature%22%3A%22%22%7D"
)

EXPIRY_RE = re.compile(r"^([^_]+)_([^&]+)&")


def extract_m_h5_tk(cookies: list[dict]) -> tuple[str | None, str | None]:
    """从 Playwright 返回的 cookies 找 _m_h5_tk + 解析 _expiry。

    token 形如 `abc123_1700000000000&...` → expiry 是中间那段毫秒时间戳。
    """
    for c in cookies:
        if c["name"] == "_m_h5_tk":
            token = c["value"]
            m = EXPIRY_RE.match(token)
            if m:
                return token, m.group(2)
            return token, None
    return None, None


def parse_expiry(ms_str: str | None) -> datetime | None:
    if not ms_str:
        return None
    try:
        return datetime.fromtimestamp(int(ms_str) / 1000, tz=timezone.utc)
    except (ValueError, TypeError):
        return None


def write_cookies(path: Path, cookies: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 转 mtop client 期待的 dict 形态
    serialized = [
        {"name": c["name"], "value": c["value"], "domain": c.get("domain", ".taobao.com"),
         "path": c.get("path", "/")}
        for c in cookies
    ]
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(serialized, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o644)
    tmp.replace(path)
    try:
        # 如果是 /etc/fliggy-monitor，尝试保留 root:monitor 属主
        import subprocess
        subprocess.run(["chown", "root:monitor", str(path)], check=False)
    except Exception:
        pass


def record_history(db_path: Path, *, success: bool, token_prefix: str | None,
                   expiry_ts: str | None, error: str | None) -> None:
    try:
        conn = connect(db_path)
    except sqlite3.OperationalError as e:
        print(f"[refresh] 无法记录 history（DB 不可用）：{e}")
        return
    try:
        conn.execute(
            "INSERT INTO cookies_history (ts, token_prefix, expiry_ts, source, success, error_msg) "
            "VALUES (?, ?, ?, 'playwright', ?, ?)",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"),
             token_prefix or "NONE", expiry_ts, 1 if success else 0, error),
        )
        conn.commit()
    finally:
        conn.close()


def do_refresh(target_url: str) -> list[dict]:
    """打开 Playwright → 跳到目标 URL → 拿 cookies。"""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"),
            viewport={"width": 390, "height": 844},
            device_scale_factor=3,
            is_mobile=True,
            has_touch=True,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        page = ctx.new_page()
        # 等待 _m_h5_tk cookie 出现
        deadline = time.time() + 45
        token_cookie_value: str | None = None
        try:
            page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            while time.time() < deadline:
                cookies = ctx.cookies()
                token, _ = extract_m_h5_tk(cookies)
                if token:
                    token_cookie_value = token
                    break
                time.sleep(1)
        finally:
            cookies = ctx.cookies()
            browser.close()

        if not token_cookie_value:
            raise RuntimeError("未能在 45s 内拿到 _m_h5_tk cookie（可能目标 URL 失效或反爬）")
        return cookies


def main() -> int:
    parser = argparse.ArgumentParser(description="刷新飞猪 mtop cookies")
    parser.add_argument("--target", default=DEFAULT_COOKIE_PATH, help="cookies.json 输出路径")
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="SQLite 路径（记录 history）")
    parser.add_argument("--url", default=DEFAULT_TARGET_URL, help="用于触发 cookie 颁发的 URL")
    parser.add_argument("--max-retries", type=int, default=3)
    args = parser.parse_args()

    target = Path(args.target)
    db_path = Path(args.db)

    last_err: str | None = None
    for attempt in range(1, args.max_retries + 1):
        try:
            print(f"[refresh] 尝试 {attempt}/{args.max_retries} → {args.url[:60]}…")
            cookies = do_refresh(args.url)
            token, expiry_ms = extract_m_h5_tk(cookies)
            expiry_dt = parse_expiry(expiry_ms)
            expiry_iso = expiry_dt.isoformat(timespec="seconds") if expiry_dt else None

            # 校验 expiry > now + 1h
            if expiry_dt and (expiry_dt - datetime.now(timezone.utc)).total_seconds() < 3600:
                raise RuntimeError(f"拿到的 _m_h5_tk 过期时间太近：{expiry_iso}")

            write_cookies(target, cookies)
            print(f"[refresh] 写入 {target}（共 {len(cookies)} 个 cookie）")
            print(f"[refresh] _m_h5_tk prefix={token[:8] if token else '?'}… expiry={expiry_iso}")

            record_history(db_path, success=True,
                           token_prefix=token[:8] if token else None,
                           expiry_ts=expiry_iso, error=None)
            return 0
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            print(f"[refresh] 第 {attempt} 次失败：{last_err}")
            record_history(db_path, success=False, token_prefix=None, expiry_ts=None, error=last_err)
            if attempt < args.max_retries:
                backoff = 5 * (3 ** (attempt - 1))  # 5/15/45
                print(f"[refresh] {backoff}s 后重试…")
                time.sleep(backoff)

    print(f"[refresh] 全部重试失败：{last_err}")
    return 1


if __name__ == "__main__":
    sys.exit(main())