"""SQL 存储：同一套 SQL 跑在 SQLite（默认，单机）和 PostgreSQL（多副本共享）上。

控制面和模型网关读写同一个数据库：网关要知道某个会话选了哪个模型、用户的 API Key 是什么、
今天用了多少。单机时是同一个 SQLite 文件，分布式部署时是同一个 PostgreSQL。

为了两边都能跑，SQL 只用两者共有的写法；占位符统一写成 ?，PostgreSQL 执行前换成 %s
（只换 SQL 代码里的，字符串字面量里的 ? 不动）。

schema 迁移：启动时把数据库升级到代码知道的版本（schema_version 表 + 顺序迁移）。
多副本滚动升级时新旧副本会同时连同一个 PostgreSQL，所以每条迁移都必须幂等；
数据库版本比代码还新时拒绝启动——那份数据是更新的代码写的，旧代码看不懂。
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

SCHEMA_VERSION = 1

# 顺序迁移：结构每次变化在这里追加一条，全部幂等（重跑不能出错），
# 因为多副本滚动升级时新旧副本会同时对同一个 PostgreSQL 跑迁移（#64）。
# 第 N 条把数据库从 N-1 升级到 N。
MIGRATIONS: list[list[str]] = [
    # v1：初始结构。更早的版本没有 schema_version 表，老库会被 CREATE IF NOT EXISTS 原样补上版本号。
    ["""CREATE TABLE IF NOT EXISTS sessions (
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
        billable_tokens BIGINT NOT NULL DEFAULT 0, PRIMARY KEY (owner, day))"""],
]


class StorageBusy(RuntimeError):
    """数据库连接池忙（拿不到连接）。调用方应返回 503 让客户端重试，不要卡死或断连接（#60/#64）。"""


def _pg_placeholders(sql: str) -> str:
    """把参数占位符 ? 换成 PostgreSQL 的 %s——只换 SQL 代码里的，字符串字面量里的 ? 不动。

    整条 sql.replace("?", "%s") 太脆：以后 SQL 里出现 LIKE '%?%'、JSON 操作符之类的字面量
    就会被改错（#64）。这里按单引号字面量切分，'' 是转义的单引号。
    """
    out: list[str] = []
    quoted = False
    i = 0
    while i < len(sql):
        c = sql[i]
        if quoted:
            if c == "'":
                if sql[i + 1:i + 2] == "'":       # 字面量里转义的单引号，两个字符一起跳过
                    out.append("''")
                    i += 2
                    continue
                quoted = False
        elif c == "'":
            quoted = True
        elif c == "?":
            out.append("%s")
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


class ConnectionPool:
    """很浅的连接池：按需创建、上限 size；坏了就关掉重建；拿不到就明确报 StorageBusy。

    单独成类是为了不装 psycopg 也能测（PostgreSQL 本身是可选依赖）。
    """

    def __init__(self, connect, size: int = 8, timeout: float = 30):
        self._connect = connect
        self._size = size
        self._timeout = timeout
        self._pool: queue.LifoQueue = queue.LifoQueue()
        self._created = 0
        self._lock = threading.Lock()

    def _acquire(self):
        try:
            return self._pool.get_nowait()
        except queue.Empty:
            pass
        with self._lock:
            if self._created < self._size:
                self._created += 1
                try:
                    return self._connect()
                except BaseException:
                    # 释放预留的池容量；否则连接失败会永久占住一个槽位。
                    self._created -= 1
                    raise
        try:
            return self._pool.get(timeout=self._timeout)
        except queue.Empty:
            # 宁可明确失败：调用方会把它变成 503，客户端重试即可（#60）。
            raise StorageBusy(f"{self._timeout:.0f} 秒内没等到数据库连接") from None

    def _release(self, conn) -> None:
        self._pool.put(conn)

    @contextmanager
    def connection(self):
        conn = self._acquire()
        try:
            yield conn
        except Exception:
            if getattr(conn, "closed", False) or getattr(conn, "broken", False):
                # 坏掉的连接必须显式关掉再放掉槽位：只把计数减掉不 close 会泄漏
                # socket 和服务端连接，直到垃圾回收（#64）。
                self._discard(conn)
            else:
                self._release(conn)
            raise
        else:
            self._release(conn)

    def _discard(self, conn) -> None:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 —— 关一个已经坏了的连接，失败也只能忽略
            pass
        with self._lock:
            self._created -= 1

    def close(self) -> None:
        while True:
            try:
                conn = self._pool.get_nowait()
            except queue.Empty:
                return
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            with self._lock:
                self._created -= 1


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
        """把数据库升级到代码知道的版本。版本比代码新：拒绝启动（#64）。"""
        self.execute("CREATE TABLE IF NOT EXISTS schema_version (id INTEGER PRIMARY KEY, version INTEGER NOT NULL)")
        rows = self.execute("SELECT version FROM schema_version WHERE id = 1")
        current = rows[0]["version"] if rows else 0
        if current > SCHEMA_VERSION:
            raise RuntimeError(f"数据库 schema 版本是 {current}，比这个代码支持的 {SCHEMA_VERSION} 新："
                               "请升级 nanocode-server（ downgrade 数据请先用旧版本导出）")
        # 逐条应用、每条都幂等：多副本同时迁移或迁移中途失败都不会把库弄坏（#64）。
        for version, stmts in enumerate(MIGRATIONS[current:], start=current + 1):
            for stmt in stmts:
                self.execute(stmt)
            self.execute("INSERT INTO schema_version (id, version) VALUES (1, ?) "
                         "ON CONFLICT (id) DO UPDATE SET version = EXCLUDED.version", (version,))

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

        self._pool = ConnectionPool(lambda: psycopg.connect(url, autocommit=True, row_factory=dict_row), pool_size)
        with self._pool.connection():
            pass        # 配了就要连得上，连不上直接报错

    def execute(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._pool.connection() as conn:
            cur = conn.execute(_pg_placeholders(sql), params)
            return list(cur.fetchall()) if cur.description else []

    def update(self, sql: str, params: tuple = ()) -> int:
        with self._pool.connection() as conn:
            return conn.execute(_pg_placeholders(sql), params).rowcount

    def close(self) -> None:
        self._pool.close()


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

    def claim_start(self, sid: str, web_token: str, stale_after: float = 300, now: float | None = None) -> bool:
        """原子地把会话从"没在运行"改成"启动中"，成功返回 True。

        控制面可以有多个副本：同一个会话同时被两个副本访问时，只有抢到的那个去启动沙箱。
        启动中的副本挂了，状态会卡在 starting：超过 stale_after 秒没有进展就允许别人重新抢。

        stale_after 必须明显大于最慢的启动超时（K8s 是 120 秒），否则启动途中会被别人
        重抢、起出第二个沙箱来（#59）。
        """
        now = time.time() if now is None else now
        return self.db.update(
            "UPDATE sessions SET status = 'starting', web_token = ?, error = '', last_active = ? WHERE id = ? AND "
            "(status IN ('stopped', 'failed') OR (status = 'starting' AND last_active < ?))",
            (web_token, now, sid, now - stale_after)) == 1

    def finish_start(self, sid: str, web_token: str, endpoint: str, now: float) -> bool:
        """启动完成：写成 running。只有还持着这次启动（状态没变、令牌没被重抢换成新的）才成功。

        没有这道校验，启动期间被删除的会话会被无条件写回 running"复活"，
        endpoint 指向一个工作区已经没了的沙箱（#59）。web_token 就是租约：
        claim_start 原子地换了它，谁还拿着旧值就说明已经失去启动权。
        """
        return self.db.update(
            "UPDATE sessions SET status = 'running', endpoint = ?, started = ?, last_active = ? "
            "WHERE id = ? AND status = 'starting' AND web_token = ?",
            (endpoint, now, now, sid, web_token)) == 1

    def fail_start(self, sid: str, web_token: str, error: str) -> bool:
        """启动失败：写成 failed。同样只有还持着启动权时才写，不覆盖别人的删除/停止（#59）。"""
        return self.db.update(
            "UPDATE sessions SET status = 'failed', endpoint = '', error = ? "
            "WHERE id = ? AND status = 'starting' AND web_token = ?",
            (error[:500], sid, web_token)) == 1

    def update_session(self, sid: str, **fields) -> None:
        unknown = set(fields) - self.SESSION_FIELDS
        if unknown:
            raise ValueError(f"不能更新这些字段：{unknown}")
        # deleted 是终态：除"改成 deleted"本身，任何更新都不能把已删除的会话写回可见（#59）。
        guard = "" if fields.get("status") == "deleted" else " AND status <> 'deleted'"
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE sessions SET {cols} WHERE id = ?{guard}", (*fields.values(), sid))

    def stop_session(self, sid: str) -> bool:
        """停止。同样受终态保护：已删除的会话不允许被 stop 写活（#59）。"""
        return self.db.update("UPDATE sessions SET status = 'stopped', endpoint = '' "
                              "WHERE id = ? AND status <> 'deleted'", (sid,)) == 1

    def mark_dead(self, sid: str) -> bool:
        """转发失败：沙箱不在了。只有还在 running 才标记，避免和停止/删除互相覆盖（#59）。"""
        return self.db.update("UPDATE sessions SET status = 'stopped', endpoint = '' "
                              "WHERE id = ? AND status = 'running'", (sid,)) == 1

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
        # 一条带 owner 条件的 DELETE：不用先 SELECT 再 DELETE 两个来回（#64）。
        return self.db.update("DELETE FROM credentials WHERE id = ? AND owner = ?", (cid, owner)) == 1

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

    def reserve_quota(self, owner: str, tokens: int, limit: int, day: str | None = None) -> bool:
        """原子地「检查 + 预留」每日额度：今天的 billable_tokens 加上 tokens 仍不超过
        limit，才把 tokens 记进去并返回 True；放不下返回 False。

        以前网关是「请求前读一次用量 → 放行 → 结束后再记账」，并发的请求都读到同一个
        旧值、全部通过检查，合力可以把额度超支几倍（#54）。这里把查和留放进同一条
        UPDATE 的 WHERE 里，靠数据库行锁串行化：只有真正放得下的请求能预留成功。
        """
        if limit <= 0:
            return True                      # 0 = 不限制
        day = day or utc_day()
        # 先保证当天有行（并发下只有一个能插入，另一个 DO NOTHING），再原子地占额度。
        self.db.execute("INSERT INTO usage (owner, day, requests, input_tokens, output_tokens, billable_tokens) "
                        "VALUES (?, ?, 0, 0, 0, 0) ON CONFLICT (owner, day) DO NOTHING", (owner, day))
        return self.db.update(
            "UPDATE usage SET billable_tokens = billable_tokens + ? WHERE owner = ? AND day = ? AND "
            "billable_tokens + ? <= ?", (tokens, owner, day, tokens, limit)) == 1

    def settle_usage(self, owner: str, requests: int, input_tokens: int, output_tokens: int,
                     billable_delta: int, day: str | None = None) -> None:
        """结算一次模型请求：补记真实的 input/output，billable 按「实际 - 预留」多退
        少补（delta 可为负，退款；调用方保证 delta <= 0，见 gateway.record）。"""
        self.db.execute("INSERT INTO usage (owner, day, requests, input_tokens, output_tokens, billable_tokens) "
                        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (owner, day) DO UPDATE SET "
                        "requests = usage.requests + EXCLUDED.requests, "
                        "input_tokens = usage.input_tokens + EXCLUDED.input_tokens, "
                        "output_tokens = usage.output_tokens + EXCLUDED.output_tokens, "
                        "billable_tokens = usage.billable_tokens + EXCLUDED.billable_tokens",
                        (owner, day or utc_day(), requests, input_tokens, output_tokens, billable_delta))

    def get_usage(self, owner: str, day: str | None = None) -> dict:
        day = day or utc_day()
        rows = self.db.execute("SELECT requests, input_tokens, output_tokens, billable_tokens FROM usage "
                               "WHERE owner = ? AND day = ?", (owner, day))
        u = ({k: int(v) for k, v in rows[0].items()} if rows else
             {"requests": 0, "input_tokens": 0, "output_tokens": 0, "billable_tokens": 0})
        return {**u, "total_tokens": u["input_tokens"] + u["output_tokens"], "day": day}

    def close(self) -> None:
        self.db.close()


__all__ = ["Database", "Store", "StorageBusy", "utc_day"]
