"""Web 面板的限流与预算（issue #7）。

nanocode --web 本来就是单进程单会话，所以全部放内存、加锁即可，重启清零。
每一项都能用环境变量设成 0 关掉，回滚不用改代码：

    NANOCODE_WEB_RATE_PER_MIN   每个客户端每分钟补充的令牌数（默认 10）
    NANOCODE_WEB_RATE_BURST     令牌桶容量，允许的瞬时连发（默认 5）
    NANOCODE_WEB_MAX_QUEUE      Agent 忙时最多排队几条（默认 3）
    NANOCODE_WEB_DAILY_RUNS     每个自然日最多启动几轮 Agent，含出队执行的（默认 200）
    NANOCODE_TRUST_PROXY        设为 1 时用 X-Forwarded-For 最左侧地址区分客户端（只在挂了反代时开）

模型 API 调用次数的每日上限 NANOCODE_WEB_DAILY_LLM_CALLS 在 Provider 层计数，见 llm/base.py。
"""

from __future__ import annotations

import datetime as dt
import os
import threading
import time


def env_int(name: str, default: int) -> int:
    """读非负整数环境变量；没设或写错就用默认值。"""
    raw = os.environ.get(name, "")
    if raw.strip() == "":
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


class RateLimited(Exception):
    """超限。code 给前端判断：rate_limited / queue_full / daily_budget。"""

    def __init__(self, code: str, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.code, self.message, self.retry_after = code, message, retry_after


class TokenBucket:
    """每个 key 一个桶：容量 burst，每秒补 rate_per_min/60 个。

    take() 返回 0 表示放行，否则返回建议等待的秒数。rate_per_min 或 burst 为 0 表示不限。
    """

    MAX_KEYS = 10_000   # 防止伪造大量 key 撑爆内存

    def __init__(self, rate_per_min: int, burst: int):
        self.rate = rate_per_min / 60.0
        self.burst = burst
        self._b: dict[str, tuple[float, float]] = {}   # key -> (剩余令牌, 上次更新时间)
        self._lock = threading.Lock()

    def take(self, key: str, now: float | None = None) -> float:
        if self.rate <= 0 or self.burst <= 0:
            return 0
        now = time.monotonic() if now is None else now
        with self._lock:
            if len(self._b) >= self.MAX_KEYS and key not in self._b:
                # 已经补满的桶等于没记录，删掉不影响结果
                self._b = {k: v for k, v in self._b.items()
                           if v[0] + (now - v[1]) * self.rate < self.burst}
            tokens, last = self._b.get(key, (float(self.burst), now))
            tokens = min(float(self.burst), tokens + (now - last) * self.rate)
            if tokens >= 1:
                self._b[key] = (tokens - 1, now)
                return 0
            self._b[key] = (tokens, now)
            return (1 - tokens) / self.rate


class DailyCounter:
    """按本地自然日计数，跨天自动清零。limit=0 不限。"""

    def __init__(self, limit: int):
        self.limit = limit
        self.day = dt.date.today()
        self.used = 0
        self._lock = threading.Lock()

    def take(self) -> bool:
        if self.limit <= 0:
            return True
        with self._lock:
            today = dt.date.today()
            if today != self.day:
                self.day, self.used = today, 0
            if self.used >= self.limit:
                return False
            self.used += 1
            return True

    @staticmethod
    def seconds_to_midnight() -> int:
        now = dt.datetime.now()
        tomorrow = dt.datetime.combine(now.date() + dt.timedelta(days=1), dt.time())
        return int((tomorrow - now).total_seconds()) + 1


class Limits:
    """WebApp 用到的全部限制，启动时从环境变量读一次。"""

    def __init__(self):
        self.rate_per_min = env_int("NANOCODE_WEB_RATE_PER_MIN", 10)
        self.burst = env_int("NANOCODE_WEB_RATE_BURST", 5)
        self.bucket = TokenBucket(self.rate_per_min, self.burst)
        self.max_queue = env_int("NANOCODE_WEB_MAX_QUEUE", 3)
        self.daily_runs = DailyCounter(env_int("NANOCODE_WEB_DAILY_RUNS", 200))
        self.trust_proxy = os.environ.get("NANOCODE_TRUST_PROXY", "").strip() == "1"

    def describe(self) -> str:
        def show(v):
            return str(v) if v else "不限"
        rate = f"{self.rate_per_min}/分钟（连发 {self.burst}）" if self.rate_per_min and self.burst else "不限"
        return (f"限流：每个客户端 {rate} · 排队上限 {show(self.max_queue)} · "
                f"每日 Agent 轮数 {show(self.daily_runs.limit)}"
                + (" · 按 X-Forwarded-For 区分客户端" if self.trust_proxy else ""))
