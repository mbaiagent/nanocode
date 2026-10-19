"""持久化和限流。open_store / make_rate_limiter 按配置选择实现。"""

from __future__ import annotations

from ..config import Config
from .ratelimit import MemoryRateLimiter, RateLimiter, RedisRateLimiter
from .sql import Database, StorageBusy, Store


def open_store(cfg: Config) -> Store:
    return Store(Database.open(cfg.database_url, cfg.data_dir))


def make_rate_limiter(cfg: Config) -> RateLimiter:
    if cfg.redis_url:
        return RedisRateLimiter(cfg.redis_url, cfg.requests_per_minute)
    return MemoryRateLimiter(cfg.requests_per_minute)


__all__ = ["open_store", "make_rate_limiter", "Store", "Database", "StorageBusy", "RateLimiter"]
