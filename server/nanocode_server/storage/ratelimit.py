"""每人每分钟的请求数限制。

    MemoryRateLimiter   进程内存里的滑动窗口：单副本足够；多副本时每个副本各算各的
    RedisRateLimiter    Redis 固定窗口计数：多副本共享同一个计数
"""

from __future__ import annotations

import collections
import threading
import time
from abc import ABC, abstractmethod
from urllib.parse import parse_qs, urlparse


def redis_options(url: str, redis_py_version: str) -> dict:
    """redis.Redis.from_url 的参数：URL 里没指定 protocol 时用 RESP2（#72）。

    redis-py 5.0 之前没有 protocol 参数（本来就只说 RESP2），传了反而报错，所以只在 5.0+ 时加。
    """
    opts: dict = {"socket_timeout": 2}
    if "protocol" in parse_qs(urlparse(url).query):
        return opts
    try:
        major = int(redis_py_version.split(".")[0])
    except ValueError:
        major = 0
    if major >= 5:
        opts["protocol"] = 2
    return opts


class RateLimiter(ABC):
    backend = ""

    @abstractmethod
    def allow(self, key: str) -> bool:
        """这次请求是否允许。允许时同时计数。"""


class MemoryRateLimiter(RateLimiter):
    backend = "memory"

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self._hits: dict[str, collections.deque] = collections.defaultdict(collections.deque)
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        if self.per_minute <= 0:
            return True
        now = time.time() if now is None else now
        with self._lock:
            q = self._hits[key]
            while q and q[0] <= now - 60:
                q.popleft()
            if len(q) >= self.per_minute:
                return False
            q.append(now)
            return True


class RedisRateLimiter(RateLimiter):
    backend = "redis"

    def __init__(self, url: str, per_minute: int, prefix: str = "ncs:rl:"):
        try:
            import redis
        except ImportError:
            raise RuntimeError("配置了 NCS_REDIS_URL，但没有安装 redis 包：pip install redis") from None
        # redis-py 8.x 默认用 RESP3，连上就发 HELLO；老版本 Redis（如 Windows 移植版 3.0.504）
        # 不认这个命令，直接报 unknown command 'HELLO'（#72）。URL 没写 protocol 时默认 RESP2，
        # 新旧 Redis 都能用；需要 RESP3 时在 URL 里显式加 ?protocol=3。
        self.client = redis.Redis.from_url(url, **redis_options(url, getattr(redis, "__version__", "")))
        self.client.ping()          # 配了就要连得上，连不上直接报错
        self.per_minute = per_minute
        self.prefix = prefix

    def allow(self, key: str, now: float | None = None) -> bool:
        if self.per_minute <= 0:
            return True
        now = time.time() if now is None else now
        bucket = f"{self.prefix}{key}:{int(now // 60)}"
        pipe = self.client.pipeline()
        pipe.incr(bucket)
        pipe.expire(bucket, 120)
        count, _ = pipe.execute()
        return int(count) <= self.per_minute


__all__ = ["RateLimiter", "MemoryRateLimiter", "RedisRateLimiter", "redis_options"]
