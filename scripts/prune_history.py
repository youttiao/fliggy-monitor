#!/usr/bin/env python3
"""清理 N 天前的历史数据，避免 DB 无限膨胀。

保留策略（默认，可在 config 表改 `retention_days`）：
- cells_snapshot / rounds / alerts / cookies_history: 14 天
- login_failures: 30 天（安全审计需要更长）
- web_sessions: 只清过期的（expires_at < now）；活跃会话不动

运行时机：systemd timer ``fliggy-prune.timer`` 每天 04:07 触发。
手动运行::

    python3 scripts/prune_history.py
    python3 scripts/prune_history.py --retention-days 30
    python3 scripts/prune_history.py --dry-run

为什么用 VACUUM 而不是 incremental：14 天体量下 < 100MB，VACUUM < 1s；
incremental 需要先 ``PRAGMA auto_vacuum = INCREMENTAL`` 才会生效，没启用时静默 no-op。
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 允许从项目根或 web/ 任意位置运行
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from web.db import connect, execute, get_config, transaction  # noqa: E402

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("prune_history")

# 默认保留期；config 表里有 ``retention_days`` 键时可在线调整，无需重启服务。
DEFAULT_RETENTION_DAYS = 14
LOGIN_FAILURES_RETENTION_DAYS = 30


def cutoff_iso(days: int) -> str:
    """N 天前的 UTC ISO 8601 时间戳，与 ``rounds.started_at`` / ``alerts.ts`` 同格式。"""
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def prune_rounds_and_cells(conn: sqlite3.Connection, cutoff: str) -> tuple[int, int]:
    """清 cells_snapshot + rounds 老于 cutoff 的行。

    cells_snapshot 有 FK → rounds(id) ON DELETE CASCADE，但我们手动双向删：
    1) 先删 cells（叶子表，无 cascade 计算开销）
    2) 再删 rounds（外键 targets，单独删会触发 alert.round_id 孤儿行）
    """
    with transaction(conn):
        n_cells = execute(
            conn,
            "DELETE FROM cells_snapshot WHERE round_id IN "
            "(SELECT id FROM rounds WHERE started_at < ?)",
            (cutoff,),
        ).rowcount
        n_rounds = execute(
            conn,
            "DELETE FROM rounds WHERE started_at < ?",
            (cutoff,),
        ).rowcount
    return n_cells, n_rounds


def prune_alerts(conn: sqlite3.Connection, cutoff: str) -> int:
    return execute(conn, "DELETE FROM alerts WHERE ts < ?", (cutoff,)).rowcount


def prune_cookies_history(conn: sqlite3.Connection, cutoff: str) -> int:
    return execute(conn, "DELETE FROM cookies_history WHERE ts < ?", (cutoff,)).rowcount


def prune_web_sessions(conn: sqlite3.Connection) -> int:
    """过期即删；活跃会话（expires_at > now）不动。"""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return execute(conn, "DELETE FROM web_sessions WHERE expires_at < ?", (now,)).rowcount


def prune_login_failures(conn: sqlite3.Connection, cutoff: str) -> int:
    return execute(conn, "DELETE FROM login_failures WHERE ts < ?", (cutoff,)).rowcount


def vacuum(conn: sqlite3.Connection) -> None:
    log.info("VACUUM 开始")
    t0 = datetime.now()
    conn.execute("VACUUM")
    log.info("VACUUM 完成，耗时 %.1fs", (datetime.now() - t0).total_seconds())


def report_sizes(conn: sqlite3.Connection) -> dict[str, int]:
    """清理前后所有表的行数 + DB 文件字节数。"""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    out: dict[str, int] = {}
    for r in rows:
        out[r["name"]] = conn.execute(f"SELECT COUNT(*) FROM {r['name']}").fetchone()[0]
    # 取 DB 主文件大小（WAL/SHM 不算，是 transient 的）
    db_row = conn.execute("PRAGMA database_list").fetchone()
    if db_row and db_row["file"] and os.path.exists(db_row["file"]):
        out["__db_bytes__"] = os.path.getsize(db_row["file"])
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="清理 fliggy-monitor 历史数据")
    parser.add_argument(
        "--retention-days",
        type=int,
        default=None,
        help=f"覆盖 config.retention_days（默认读 config；缺省 {DEFAULT_RETENTION_DAYS}）",
    )
    parser.add_argument(
        "--db",
        default=os.getenv("FLIGGY_DB", "/opt/fliggy-monitor/data/monitor.db"),
        help="DB 文件路径（默认 /opt/fliggy-monitor/data/monitor.db）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印计划，不实际删 / 不 VACUUM",
    )
    parser.add_argument(
        "--skip-vacuum",
        action="store_true",
        help="跳过最后的 VACUUM（清理照做，但文件大小不会立即回收）",
    )
    args = parser.parse_args()

    log.info("DB: %s", args.db)
    conn = connect(args.db)
    try:
        # 保留期：CLI > config > 默认
        if args.retention_days is not None:
            retention_days = args.retention_days
            log.info("retention_days 来自 CLI 参数: %d", retention_days)
        else:
            retention_days = int(get_config(conn, "retention_days", DEFAULT_RETENTION_DAYS))
            log.info("retention_days 来自 config 表: %d", retention_days)

        cutoff = cutoff_iso(retention_days)
        login_cutoff = cutoff_iso(LOGIN_FAILURES_RETENTION_DAYS)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")

        log.info(
            "cutoff: rounds/cells/alerts/cookies < %s; login_failures < %s; sessions expires_at < %s",
            cutoff, login_cutoff, now,
        )

        before = report_sizes(conn)
        log.info("清理前: %s", before)

        if args.dry_run:
            log.info("[dry-run] 跳过所有 DELETE / VACUUM")
            return 0

        # 顺序：先删 children，再删 parents；sellers/seller_enrichment/pois/shelf_watch 是 cumulative，不动
        n_cells, n_rounds = prune_rounds_and_cells(conn, cutoff)
        log.info("rounds: 删 %d 行（cascade cells_snapshot %d 行）", n_rounds, n_cells)

        n_alerts = prune_alerts(conn, cutoff)
        log.info("alerts: 删 %d 行", n_alerts)

        n_cookies = prune_cookies_history(conn, cutoff)
        log.info("cookies_history: 删 %d 行", n_cookies)

        n_sessions = prune_web_sessions(conn)
        log.info("web_sessions: 删 %d 行（仅过期）", n_sessions)

        n_login = prune_login_failures(conn, login_cutoff)
        log.info("login_failures: 删 %d 行（保留 %d 天）", n_login, LOGIN_FAILURES_RETENTION_DAYS)

        if not args.skip_vacuum:
            vacuum(conn)

        after = report_sizes(conn)
        log.info("清理后: %s", after)
        if "__db_bytes__" in before and "__db_bytes__" in after:
            before_mb = before["__db_bytes__"] / 1024 / 1024
            after_mb = after["__db_bytes__"] / 1024 / 1024
            log.info(
                "DB 文件: %.2f MB → %.2f MB（回收 %.2f MB）",
                before_mb, after_mb, before_mb - after_mb,
            )
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())