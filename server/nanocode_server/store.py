"""持久化：会话表（控制面）和用量表（模型网关），SQLite 实现。

为什么是 SQLite：标准库自带，单个控制面副本、千人规模的读写量完全够用，测试也不需要外部服务。
它的局限也要说清楚：
  - 只能有一个控制面副本（多副本要共享数据库，需要换成 PostgreSQL，接口就是这两个类的方法）；
  - 数据文件要放在持久卷上，并定期备份。
docs/12 里"状态外置到 PostgreSQL"是下一步，这里先把接口收拢，换实现时调用方不用改。
"""

from __future__ import annotations

import datetime
import os
import sqlite3
import threading
import time
import uuid

ACTIVE = ("starting", "running")


def _connect(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


class SessionStore:
    """会话：谁的、什么状态、沙箱地址、沙箱的 Web 令牌、最近活动时间。"""

    def __init__(self, path: str):
        self._db = _connect(path)
        self._lock = threading.Lock()
        self._db.execute("""CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL, endpoint TEXT NOT NULL DEFAULT '', web_token TEXT NOT NULL DEFAULT '',
            created REAL NOT NULL, last_active REAL NOT NULL, started REAL NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '')""")
        self._db.execute("CREATE INDEX IF NOT EXISTS sessions_owner ON sessions(owner)")

    def create(self, owner: str, title: str) -> dict:
        sid = uuid.uuid4().hex[:16]
        now = time.time()
        with self._lock:
            self._db.execute("INSERT INTO sessions (id, owner, title, status, created, last_active) VALUES (?,?,?,?,?,?)",
                             (sid, owner, title[:100], "stopped", now, now))
        return self.get(sid)

    def get(self, sid: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM sessions WHERE id=? AND status != 'deleted'", (sid,)).fetchone()
        return dict(row) if row else None

    def list(self, owner: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM sessions WHERE owner=? AND status != 'deleted' ORDER BY created DESC",
                                    (owner,)).fetchall()
        return [dict(r) for r in rows]

    def update(self, sid: str, **fields) -> None:
        allowed = {"title", "status", "endpoint", "web_token", "last_active", "started", "error"}
        assert set(fields) <= allowed, fields
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._db.execute(f"UPDATE sessions SET {cols} WHERE id=?", (*fields.values(), sid))

    def touch(self, sid: str) -> None:
        self.update(sid, last_active=time.time())

    def count_active(self, owner: str | None = None) -> int:
        q = "SELECT COUNT(*) FROM sessions WHERE status IN ('starting','running')"
        args: tuple = ()
        if owner is not None:
            q += " AND owner=?"
            args = (owner,)
        with self._lock:
            return self._db.execute(q, args).fetchone()[0]

    def active(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM sessions WHERE status IN ('starting','running')").fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        self._db.close()


def today() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


class UsageStore:
    """模型网关的用量：按用户、按天（UTC）累计请求数和 token。"""

    def __init__(self, path: str):
        self._db = _connect(path)
        self._lock = threading.Lock()
        self._db.execute("""CREATE TABLE IF NOT EXISTS usage (
            owner TEXT NOT NULL, day TEXT NOT NULL, requests INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (owner, day))""")

    def add(self, owner: str, requests: int = 0, input_tokens: int = 0, output_tokens: int = 0, day: str | None = None) -> None:
        day = day or today()
        with self._lock:
            self._db.execute("""INSERT INTO usage (owner, day, requests, input_tokens, output_tokens) VALUES (?,?,?,?,?)
                ON CONFLICT(owner, day) DO UPDATE SET requests=requests+excluded.requests,
                input_tokens=input_tokens+excluded.input_tokens, output_tokens=output_tokens+excluded.output_tokens""",
                             (owner, day, requests, input_tokens, output_tokens))

    def get(self, owner: str, day: str | None = None) -> dict:
        day = day or today()
        with self._lock:
            row = self._db.execute("SELECT requests, input_tokens, output_tokens FROM usage WHERE owner=? AND day=?",
                                   (owner, day)).fetchone()
        r = dict(row) if row else {"requests": 0, "input_tokens": 0, "output_tokens": 0}
        r["total_tokens"] = r["input_tokens"] + r["output_tokens"]
        r["day"] = day
        return r

    def close(self) -> None:
        self._db.close()


__all__ = ["SessionStore", "UsageStore", "today", "ACTIVE"]
