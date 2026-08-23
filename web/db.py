"""SQLite 封装。WAL 模式，row_factory=Row，连接级 busy_timeout。

调用方：
    from web.db import connect, transaction, query, execute

设计要点：
- web 模块（多读）和 monitor（单写）共用同一 DB 文件；WAL 让两边不互锁
- 写路径显式开事务（`with transaction(db):`），短路后 ROLLBACK
- 读路径直接 `query(db, sql, params)`
- 时间戳用 UTC ISO 8601 字符串，便于跨时区排查；前端展示时按 config.site_timezone 渲染
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

# 默认 DB 路径：与 05-deployment-vps.md 对齐（/opt/fliggy-monitor/data/monitor.db）
DEFAULT_DB_PATH = os.getenv("FLIGGY_DB", "/opt/fliggy-monitor/data/monitor.db")

# 默认 cookie 文件路径：与 cookie_sync.py / fliggy_monitor.py 保持一致
DEFAULT_COOKIE_PATH = os.getenv("FLIGGY_COOKIES", "/etc/fliggy-monitor/cookies.json")

# cookie 同步状态的告警阈值（秒）。12h 内同步 = 绿；12h-3d = 黄；>3d = 红。
# 这些值先用经验值占位，跑几天看真实 cookie mtime 再调。
COOKIE_OK_MAX_AGE_S = 12 * 3600          # 12 hours
COOKIE_STALE_MAX_AGE_S = 3 * 24 * 3600   # 3 days


def _resolve_db_path(path: str | os.PathLike[str] | None = None) -> Path:
    p = Path(path) if path else Path(DEFAULT_DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def connect(path: str | os.PathLike[str] | None = None, *, read_only: bool = False) -> sqlite3.Connection:
    """打开一个 SQLite 连接。

    - WAL：读写并发
    - foreign_keys=ON：FK 约束生效
    - busy_timeout=5s：避免 monitor 写时 web 短暂 BUSY
    - row_factory=Row：dict-like 行
    """
    p = _resolve_db_path(path)
    if read_only:
        # Read-only URI 模式（VPS 上 web 进程可走这个，强制只读）
        uri = f"file:{p}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    else:
        conn = sqlite3.connect(p, timeout=5.0, isolation_level=None)  # autocommit; 我们用显式事务
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA temp_store = MEMORY")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式事务上下文。退出时若未异常 → COMMIT，否则 ROLLBACK。"""
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def query(
    conn: sqlite3.Connection,
    sql: str,
    params: Iterable[Any] = (),
    *,
    one: bool = False,
) -> list[sqlite3.Row] | sqlite3.Row | None:
    """读路径。无结果返回 []；one=True 且无结果返回 None。"""
    cur = conn.execute(sql, tuple(params))
    rows = cur.fetchall()
    cur.close()
    if one:
        return rows[0] if rows else None
    return rows


def execute(
    conn: sqlite3.Connection,
    sql: str,
    params: Iterable[Any] = (),
) -> sqlite3.Cursor:
    """写路径。单条 SQL；复杂多语句请用 transaction(conn) 包裹。"""
    return conn.execute(sql, tuple(params))


def executemany(
    conn: sqlite3.Connection,
    sql: str,
    seq_of_params: Iterable[Iterable[Any]],
) -> sqlite3.Cursor:
    return conn.executemany(sql, [tuple(p) for p in seq_of_params])


def get_config(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    """读 config 表的 JSON-decoded value。"""
    import json
    row = query(conn, "SELECT value FROM config WHERE key = ?", (key,), one=True)
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return default


def set_config(conn: sqlite3.Connection, key: str, value: Any) -> None:
    """写 config。value 会被 JSON 序列化。"""
    import json
    from datetime import datetime, timezone
    payload = json.dumps(value, ensure_ascii=False)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with transaction(conn):
        execute(
            conn,
            "INSERT INTO config(key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, payload, now),
        )


# ──────────────────────────────────────────────────────────────
# 高层只读查询（路由直接用）
# ──────────────────────────────────────────────────────────────


def list_rounds(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return query(
        conn,
        "SELECT * FROM rounds ORDER BY started_at DESC LIMIT ?",
        (limit,),
    ) or []


def latest_round(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return query(conn, "SELECT * FROM rounds ORDER BY started_at DESC LIMIT 1", one=True)


def latest_successful_round(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """最新一轮『有可用数据』的 round。

    判定：status='success'，或 status='partial' 且至少有一些 cell。
    用于 dashboard 在最新一轮失败时回退到上一个有用的 round。
    """
    return query(
        conn,
        """SELECT * FROM rounds
           WHERE (status = 'success' OR (status = 'partial' AND cells_total > 0))
           ORDER BY id DESC LIMIT 1""",
        one=True,
    )


def cookie_metadata(path: str | None = None) -> dict[str, Any]:
    """cookies.json 文件健康度：是否存在、mtime、age_seconds、status。

    status 取值：
    - 'ok'：< COOKIE_OK_MAX_AGE_S（默认 12h）
    - 'stale'：12h ~ COOKIE_STALE_MAX_AGE_S（默认 3d）
    - 'expired'：> 3d
    - 'missing'：文件不存在
    """
    from datetime import datetime, timezone
    p = Path(path) if path else Path(DEFAULT_COOKIE_PATH)
    if not p.exists():
        return {
            "exists": False,
            "path": str(p),
            "mtime": None,
            "age_seconds": None,
            "status": "missing",
        }
    stat = p.stat()
    mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
    now = datetime.now(timezone.utc)
    age = max(0, int((now - mtime).total_seconds()))

    if age < COOKIE_OK_MAX_AGE_S:
        status = "ok"
    elif age < COOKIE_STALE_MAX_AGE_S:
        status = "stale"
    else:
        status = "expired"

    return {
        "exists": True,
        "path": str(p),
        "mtime": mtime.isoformat(timespec="seconds"),
        "age_seconds": age,
        "status": status,
    }


def poi_summary(conn: sqlite3.Connection, latest_round_id: int) -> list[sqlite3.Row]:
    """每个 POI 的本轮统计：total / self / non_self / non_self_sellers。"""
    return query(
        conn,
        """
        SELECT
            p.poi_id, p.name, p.enabled, p.last_scanned_at, p.last_status,
            COUNT(c.id) AS cells_total,
            SUM(CASE WHEN c.is_self=1 THEN 1 ELSE 0 END) AS cells_self,
            SUM(CASE WHEN c.is_self=0 THEN 1 ELSE 0 END) AS cells_non_self,
            COUNT(DISTINCT CASE WHEN c.is_self=0 THEN c.seller_id END) AS non_self_sellers
        FROM pois p
        LEFT JOIN cells_snapshot c ON c.poi_id = p.poi_id AND c.round_id = ?
        GROUP BY p.poi_id
        ORDER BY p.poi_id
        """,
        (latest_round_id,),
    ) or []


def poi_cells(
    conn: sqlite3.Connection,
    poi_id: str,
    round_id: int,
    *,
    cell_type: str | None = None,
    seller_id: str | None = None,
    only_non_self: bool = False,
) -> list[sqlite3.Row]:
    """某 POI 在某 round 的所有 cell；可按 cell_type / seller_id / 非自营 过滤。"""
    where = ["c.round_id = ?", "c.poi_id = ?"]
    params: list[Any] = [round_id, poi_id]
    if cell_type:
        where.append("c.cell_type = ?")
        params.append(cell_type)
    if seller_id:
        where.append("c.seller_id = ?")
        params.append(seller_id)
    if only_non_self:
        where.append("c.is_self = 0")
    sql = f"""
        SELECT c.*, s.seller_name, s.shop_jump_url, s.service_stats,
               e.display_name AS enrichment_name, e.is_watched AS watched,
               e.notes AS enrichment_notes, e.priority AS enrichment_priority,
               w.is_watched AS shelf_watched,
               w.notes AS shelf_notes
        FROM cells_snapshot c
        LEFT JOIN sellers s ON s.seller_id = c.seller_id
        LEFT JOIN seller_enrichment e ON e.seller_id = c.seller_id
        LEFT JOIN shelf_watch w
               ON w.poi_id = c.poi_id
              AND w.item_id = c.item_id
              AND w.sku_id  = c.sku_id
              AND w.is_watched = 1
        WHERE {' AND '.join(where)}
        ORDER BY
            c.is_self DESC,                       -- 自营先行
            COALESCE(w.is_watched, 0) DESC,       -- 货架关注次之
            COALESCE(e.is_watched, 0) DESC,       -- 卖家关注的次之
            COALESCE(e.priority, 0) DESC,
            c.cell_type,
            c.price_int,
            c.sku_name
    """
    return query(conn, sql, params) or []


def sku_history(conn: sqlite3.Connection, poi_id: str, item_id: str, sku_id: str, limit: int = 30) -> list[sqlite3.Row]:
    return query(
        conn,
        """
        SELECT r.started_at, c.price_int, c.price_dec, c.price_suffix,
               c.sold, c.seller_id, c.is_self, s.seller_name
        FROM cells_snapshot c
        JOIN rounds r ON r.id = c.round_id
        LEFT JOIN sellers s ON s.seller_id = c.seller_id
        WHERE c.poi_id = ? AND c.item_id = ? AND c.sku_id = ?
        ORDER BY r.started_at DESC
        LIMIT ?
        """,
        (poi_id, item_id, sku_id, limit),
    ) or []


def list_alerts(conn: sqlite3.Connection, limit: int = 100, *, only_pending_webhook: bool = False) -> list[sqlite3.Row]:
    if only_pending_webhook:
        return query(
            conn,
            "SELECT * FROM alerts WHERE webhook_status = 'pending' ORDER BY ts ASC LIMIT ?",
            (limit,),
        ) or []
    return query(conn, "SELECT * FROM alerts ORDER BY ts DESC LIMIT ?", (limit,)) or []


def list_sellers(conn: sqlite3.Connection, *, watched_only: bool = False, unidentified_only: bool = False,
                 search: str | None = None) -> list[sqlite3.Row]:
    """JOIN sellers + enrichment。unidentified = 没 display_name 也没 seller_name。"""
    where: list[str] = []
    params: list[Any] = []
    if watched_only:
        where.append("COALESCE(e.is_watched, 0) = 1")
    if unidentified_only:
        where.append("(e.display_name IS NULL OR e.display_name = '') AND (s.seller_name IS NULL OR s.seller_name = '')")
    if search:
        where.append("(s.seller_id LIKE ? OR s.seller_name LIKE ? OR e.display_name LIKE ? OR e.notes LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like, like, like])
    sql = f"""
        SELECT s.seller_id, s.seller_name, s.seller_icon, s.shop_jump_url,
               s.first_seen_at, s.last_seen_at, s.total_cells, s.is_self,
               e.display_name, e.is_watched, e.notes, e.tags, e.priority,
               e.updated_at AS enrichment_updated_at
        FROM sellers s
        LEFT JOIN seller_enrichment e ON e.seller_id = s.seller_id
        {'WHERE ' + ' AND '.join(where) if where else ''}
        ORDER BY COALESCE(e.is_watched, 0) DESC,
                 COALESCE(e.priority, 0) DESC,
                 s.last_seen_at DESC
    """
    return query(conn, sql, params) or []


def seller_detail(conn: sqlite3.Connection, seller_id: str) -> sqlite3.Row | None:
    return query(
        conn,
        """
        SELECT s.*, e.display_name, e.is_watched, e.notes, e.tags, e.priority,
               e.updated_at AS enrichment_updated_at
        FROM sellers s
        LEFT JOIN seller_enrichment e ON e.seller_id = s.seller_id
        WHERE s.seller_id = ?
        """,
        (seller_id,),
        one=True,
    )


def seller_recent_cells(conn: sqlite3.Connection, seller_id: str, limit: int = 30) -> list[sqlite3.Row]:
    return query(
        conn,
        """
        SELECT c.poi_id, c.poi_name, c.item_id, c.sku_id, c.sku_name,
               c.price_int, c.price_dec, c.price_suffix, c.is_self,
               c.first_seen_at, r.started_at AS round_started_at
        FROM cells_snapshot c
        JOIN rounds r ON r.id = c.round_id
        WHERE c.seller_id = ?
        ORDER BY r.started_at DESC
        LIMIT ?
        """,
        (seller_id, limit),
    ) or []


def seller_pois(conn: sqlite3.Connection, seller_id: str) -> list[sqlite3.Row]:
    return query(
        conn,
        """
        SELECT DISTINCT c.poi_id, c.poi_name, COUNT(*) AS cell_count
        FROM cells_snapshot c
        WHERE c.seller_id = ?
        GROUP BY c.poi_id
        ORDER BY cell_count DESC
        """,
        (seller_id,),
    ) or []


def upsert_seller_enrichment(
    conn: sqlite3.Connection,
    *,
    seller_id: str,
    display_name: str | None,
    is_watched: bool,
    notes: str | None,
    tags: list[str] | None,
    priority: int,
    created_by: str = "admin",
) -> None:
    import json
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with transaction(conn):
        execute(
            conn,
            """
            INSERT INTO seller_enrichment
                (seller_id, display_name, is_watched, notes, tags, priority,
                 created_at, updated_at, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(seller_id) DO UPDATE SET
                display_name = excluded.display_name,
                is_watched   = excluded.is_watched,
                notes        = excluded.notes,
                tags         = excluded.tags,
                priority     = excluded.priority,
                updated_at   = excluded.updated_at
            """,
            (
                seller_id,
                display_name,
                1 if is_watched else 0,
                notes,
                json.dumps(tags, ensure_ascii=False) if tags else "[]",
                priority,
                now,
                now,
                created_by,
            ),
        )


def is_shelf_watched(conn: sqlite3.Connection, poi_id: str, item_id: str, sku_id: str) -> bool:
    """查 (poi, item, sku) 是否在 shelf_watch 表里被关注。"""
    row = query(
        conn,
        "SELECT 1 FROM shelf_watch WHERE poi_id=? AND item_id=? AND sku_id=? AND is_watched=1",
        (poi_id, item_id, sku_id),
        one=True,
    )
    return bool(row)


def upsert_shelf_watch(
    conn: sqlite3.Connection,
    *,
    poi_id: str,
    item_id: str,
    sku_id: str,
    is_watched: bool,
    notes: str | None = None,
    created_by: str = "admin",
) -> None:
    """写入 / 更新 (poi, item, sku) 的关注标记。"""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with transaction(conn):
        execute(
            conn,
            """
            INSERT INTO shelf_watch
                (poi_id, item_id, sku_id, is_watched, notes,
                 created_at, updated_at, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(poi_id, item_id, sku_id) DO UPDATE SET
                is_watched = excluded.is_watched,
                notes      = excluded.notes,
                updated_at = excluded.updated_at
            """,
            (
                poi_id, item_id, sku_id,
                1 if is_watched else 0,
                notes,
                now, now, created_by,
            ),
        )


# ──────────────────────────────────────────────────────────────
# 健康检查 / 系统自检
# ──────────────────────────────────────────────────────────────


def healthz(conn: sqlite3.Connection) -> dict[str, Any]:
    """给 /healthz 端点用：DB 可读 + 最新 round + 配置项是否齐。"""
    latest = latest_round(conn)
    webhook_url = get_config(conn, "webhook_url")
    webhook_secret = get_config(conn, "webhook_secret")
    return {
        "ok": True,
        "db": "ok",
        "latest_round": dict(latest) if latest else None,
        "webhook_configured": bool(webhook_url and webhook_url != "null"),
        "webhook_signed": bool(webhook_secret and webhook_secret != "null"),
    }