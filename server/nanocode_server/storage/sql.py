"""SQL 存储：同一套 SQL 跑在 SQLite（默认，单机）和 PostgreSQL（多副本共享）上。

控制面和模型网关读写同一个数据库：网关要知道某个会话选了哪个模型、用户的 API Key 是什么、
今天用了多少。单机时是同一个 SQLite 文件，分布式部署时是同一个 PostgreSQL。

为了两边都能跑，SQL 只用两者共有的写法；占位符统一写成 ?，PostgreSQL 执行前换成 %s。
"""

from __future__ import annotations

import json
import os
import queue
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager

from ..models import ACTIVE_STATUSES, Credential, Session

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY, owner TEXT NOT NULL, title TEXT NOT NULL, model_ref TEXT NOT NULL,
        status TEXT NOT NULL, endpoint TEXT NOT NULL DEFAULT '', web_token TEXT NOT NULL DEFAULT '',
        created DOUBLE PRECISION NOT NULL, last_active DOUBLE PRECISION NOT NULL,
        started DOUBLE PRECISION NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '')""",
    "CREATE INDEX IF NOT EXISTS sessions_owner ON sessions (owner)",
    "CREATE INDEX IF NOT EXISTS sessions_status ON sessions (status)",
    """CREATE TABLE IF NOT EXISTS credentials (
        id TEXT PRIMARY KEY, owner TEXT NOT NULL, provider TEXT NOT NULL, label TEXT NOT NULL,
        protocol TEXT NOT NULL, base_url TEXT NOT NULL, models TEXT NOT NULL, secret TEXT NOT NULL,
        key_hint TEXT NOT NULL, created DOUBLE PRECISION NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS credentials_owner ON credentials (owner)",
    """CREATE TABLE IF NOT EXISTS usage (
        owner TEXT NOT NULL, day TEXT NOT NULL, requests BIGINT NOT NULL DEFAULT 0,
        input_tokens BIGINT NOT NULL DEFAULT 0, output_tokens BIGINT NOT NULL DEFAULT 0,
        billable_tokens BIGINT NOT NULL DEFAULT 0, PRIMARY KEY (owner, day))""",
]


class Database:
    """很薄的一层：执行 SQL，返回字典列表。SQLite 一个连接加锁；PostgreSQL 一个小连接池。"""

    backend = ""

    @staticmethod
    def open(url: str, data_dir: str) -> "Database":
        if url.startswith(("postgres://", "postgresql://")):
            return PostgresDatabase(url)
        if url and not url.startswith("sqlite:///"):
            raise ValueError(f"不支持的 NCS_DATABASE_URL：{url.split(':', 1)[0]}（支持 postgresql:// 和 sqlite:///）")
        path = url[len("sqlite:///"):] if url else os.path.join(data_dir, "platform.db")
        return SqliteDatabase(path)

    def migrate(self) -> None:
        for stmt in SCHEMA:
            self.execute(stmt)

    def execute(self, sql: str, params: tuple = ()) -> list[dict]:
        raise NotImplementedError

    def update(self, sql: str, params: tuple = ()) -> int:
        """执行写操作，返回影响的行数。"""
        raise NotImplementedError

    def close(self) -> None:
        pass


class SqliteDatabase(Database):
    backend = "sqlite"

    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")       # 控制面和网关两个进程同时读写同一个文件
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._lock = threading.Lock()

    def execute(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return [dict(r) for r in cur.fetchall()] if cur.description else []

    def update(self, sql: str, params: tuple = ()) -> int:
        with self._lock:
            return self._conn.execute(sql, params).rowcount

    def close(self) -> None:
        self._conn.close()


class PostgresDatabase(Database):
    backend = "postgres"

    def __init__(self, url: str, pool_size: int = 8):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError:
            raise RuntimeError("配置了 PostgreSQL，但没有安装驱动：pip install 'psycopg[binary]'") from None
        self._connect = lambda: psycopg.connect(url, autocommit=True, row_factory=dict_row)
        self._pool: queue.LifoQueue = queue.LifoQueue()
        self._size = pool_size
        self._created = 0
        self._lock = threading.Lock()
        self._release(self._acquire())      # 配了就要连得上，连不上直接报错

    def _acquire(self):
        try:
            return self._pool.get_nowait()
        except queue.Empty:
            pass
        with self._lock:
            if self._created < self._size:
                self._created += 1
                return self._connect()
        return self._pool.get(timeout=30)

    def _release(self, conn) -> None:
        self._pool.put(conn)

    @contextmanager
    def _connection(self):
        conn = self._acquire()
        try:
            yield conn
        except Exception:
            if conn.closed or conn.broken:     # 坏掉的连接不放回池子，换一个新的
                with self._lock:
                    self._created -= 1
                raise
            self._release(conn)
            raise
        else:
            self._release(conn)

    def execute(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._connection() as conn:
            cur = conn.execute(sql.replace("?", "%s"), params)
            return list(cur.fetchall()) if cur.description else []

    def update(self, sql: str, params: tuple = ()) -> int:
        with self._connection() as conn:
            return conn.execute(sql.replace("?", "%s"), params).rowcount

    def close(self) -> None:
        while not self._pool.empty():
            self._pool.get_nowait().close()


def _new_id() -> str:
    return uuid.uuid4().hex[:16]


def utc_day(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() if now is None else now))


class Store:
    """三张表的读写：会话、凭证、用量。"""

    SESSION_FIELDS = {"title", "model_ref", "status", "endpoint", "web_token", "last_active", "started", "error"}

    def __init__(self, db: Database):
        self.db = db
        db.migrate()

    @property
    def backend(self) -> str:
        return self.db.backend

    # ------------------------------------------------------------------ 会话

    def create_session(self, owner: str, title: str, model_ref: str) -> Session:
        now = time.time()
        s = Session(id=_new_id(), owner=owner, title=title[:100], model_ref=model_ref,
                    created=now, last_active=now)
        self.db.execute("INSERT INTO sessions (id, owner, title, model_ref, status, created, last_active) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)", (s.id, owner, s.title, model_ref, s.status, now, now))
        return s

    def get_session(self, sid: str) -> Session | None:
        rows = self.db.execute("SELECT * FROM sessions WHERE id = ? AND status <> 'deleted'", (sid,))
        return Session(**rows[0]) if rows else None

    def list_sessions(self, owner: str) -> list[Session]:
        rows = self.db.execute("SELECT * FROM sessions WHERE owner = ? AND status <> 'deleted' "
                               "ORDER BY last_active DESC", (owner,))
        return [Session(**r) for r in rows]

    def active_sessions(self) -> list[Session]:
        rows = self.db.execute("SELECT * FROM sessions WHERE status IN (?, ?)", ACTIVE_STATUSES)
        return [Session(**r) for r in rows]

    def claim_start(self, sid: str, web_token: str, stale_after: float = 120, now: float | None = None) -> bool:
        """原子地把会话从"没在运行"改成"启动中"，成功返回 True。

        控制面可以有多个副本：同一个会话同时被两个副本访问时，只有抢到的那个去启动沙箱。
        启动中的副本挂了，状态会卡在 starting：超过 stale_after 秒没有进展就允许别人重新抢。
        """
        now = time.time() if now is None else now
        return self.db.update(
            "UPDATE sessions SET status = 'starting', web_token = ?, error = '', last_active = ? WHERE id = ? AND "
            "(status IN ('stopped', 'failed') OR (status = 'starting' AND last_active < ?))",
            (web_token, now, sid, now - stale_after)) == 1

    def update_session(self, sid: str, **fields) -> None:
        unknown = set(fields) - self.SESSION_FIELDS
        if unknown:
            raise ValueError(f"不能更新这些字段：{unknown}")
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE sessions SET {cols} WHERE id = ?", (*fields.values(), sid))

    # ------------------------------------------------------------------ 凭证

    def add_credential(self, c: Credential) -> Credential:
        c.id = c.id or _new_id()
        c.created = c.created or time.time()
        self.db.execute("INSERT INTO credentials (id, owner, provider, label, protocol, base_url, models, secret, "
                        "key_hint, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (c.id, c.owner, c.provider, c.label, c.protocol, c.base_url, json.dumps(c.models),
                         c.secret, c.key_hint, c.created))
        return c

    def _credential(self, row: dict) -> Credential:
        return Credential(**{**row, "models": json.loads(row["models"])})

    def list_credentials(self, owner: str) -> list[Credential]:
        rows = self.db.execute("SELECT * FROM credentials WHERE owner = ? ORDER BY created", (owner,))
        return [self._credential(r) for r in rows]

    def get_credential(self, cid: str) -> Credential | None:
        rows = self.db.execute("SELECT * FROM credentials WHERE id = ?", (cid,))
        return self._credential(rows[0]) if rows else None

    def delete_credential(self, owner: str, cid: str) -> bool:
        if not self.db.execute("SELECT id FROM credentials WHERE id = ? AND owner = ?", (cid, owner)):
            return False
        self.db.execute("DELETE FROM credentials WHERE id = ? AND owner = ?", (cid, owner))
        return True

    # ------------------------------------------------------------------ 用量

    def add_usage(self, owner: str, requests: int, input_tokens: int, output_tokens: int, billable: bool,
                  day: str | None = None) -> None:
        """billable：平台付费的模型（计入每日额度）；用户自己的 Key 只计量、不计入额度。"""
        billed = input_tokens + output_tokens if billable else 0
        self.db.execute("INSERT INTO usage (owner, day, requests, input_tokens, output_tokens, billable_tokens) "
                        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (owner, day) DO UPDATE SET "
                        "requests = usage.requests + EXCLUDED.requests, "
                        "input_tokens = usage.input_tokens + EXCLUDED.input_tokens, "
                        "output_tokens = usage.output_tokens + EXCLUDED.output_tokens, "
                        "billable_tokens = usage.billable_tokens + EXCLUDED.billable_tokens",
                        (owner, day or utc_day(), requests, input_tokens, output_tokens, billed))

    def get_usage(self, owner: str, day: str | None = None) -> dict:
        day = day or utc_day()
        rows = self.db.execute("SELECT requests, input_tokens, output_tokens, billable_tokens FROM usage "
                               "WHERE owner = ? AND day = ?", (owner, day))
        u = ({k: int(v) for k, v in rows[0].items()} if rows else
             {"requests": 0, "input_tokens": 0, "output_tokens": 0, "billable_tokens": 0})
        return {**u, "total_tokens": u["input_tokens"] + u["output_tokens"], "day": day}

    def close(self) -> None:
        self.db.close()


__all__ = ["Database", "Store", "utc_day"]
