# nanocode #7 修复方案：`/api/chat` 限流 + 预算闸门

> 基于 `mbaiagent/nanocode` 默认分支 HEAD `3a883f0`（2026-10-15 只读检查）。本文件只是方案，没有改仓库。

## 0. 先看清现状（与 issue 描述的差异）

| 项 | 实际代码 | 出处 |
|---|---|---|
| 服务框架 | 纯标准库 `http.server.ThreadingHTTPServer` + `BaseHTTPRequestHandler`，不是 Flask/FastAPI | `nanocode/web/server.py` L37, `make_server` L514 |
| 启动方式 | `nanocode --web [--host 127.0.0.1 --port 8765 --public-host ...]` | `nanocode/cli.py` L226-262 |
| 请求链路 | `do_POST` → `/api/chat` → `WebApp.chat()` → 后台线程 `_run()` → `session.send(prompt)` → `Agent` 循环（`max_steps=60`，子 agent 30）→ `nanocode/llm/*` Provider | `server.py` L407-416, L249-287；`nanocode/agent.py` L61/L133/L378 |
| 鉴权 | 已有：Host/Origin 白名单 + `x-nanocode-token`（`NANOCODE_WEB_TOKEN`，否则每次启动随机）。令牌在页面加载时注入，**拿到页面就拿到令牌** | `server.py` `_guard` L342-377, `web_token` L135 |
| busy 锁 | `session.agent.busy` 全局单任务；**忙时不再拒绝，而是 append 到 `self.queue`（无上限）**，`_run` 做完一个自动取下一个 | `server.py` L174, L261-287 |
| 已有 429 | 只有 `/api/ticket`（`MAX_TICKETS`） | `server.py` L427-430 |
| 部署 | GitHub Pages（`gh-pages` 分支）只有一个静态介绍页 `index.html`，**不调用任何 API**；真正的服务跑在用户自己机器上（默认只绑 127.0.0.1），或反代后面（`--public-host`），或多用户平台 `server/`（控制面 + 模型网关 + 沙箱，`deploy/k8s/*`） | `gh-pages:index.html` L378 |
| 多用户平台 | `server/nanocode_server/web/gateway.py` 已经做了**每人每分钟限流 + 平台付费模型每日 token 额度**（`storage/ratelimit.py` 的 Memory/Redis 实现） | gateway.py L10-13, L79-83 |

**结论：**
1. #5 的描述已部分过时：现在不是“来一个拒一个”，而是**无界排队**。这正是 #7 的放大器：连点 100 次 = 入队 100 个 prompt，每个都跑完整 agent 循环（最多 60 步 × N 次模型调用），`/api/cancel` 只中断当前那个，队列还在。
2. 真正暴露风险的场景是 `nanocode --web` 直接对公网（`--host 0.0.0.0` / 反代），而 `server/` 平台路径已有网关限流。所以修复点放在 **`nanocode/web/server.py`**，平台路径只需确认不冲突。
3. 因为 busy 锁本身已经保证“同时最多 1 个 agent 在跑”，全局并发上限天然=1，**不需要再造并发信号量**；要限的是：入口频率、队列长度、单日总预算。

## 1. 推荐方案（按优先级）

1. **队列上限**（最重要、最便宜）：`NANOCODE_WEB_MAX_QUEUE`，默认 3，超了 429。
2. **按客户端的令牌桶**：每个 key（IP，或反代给的真实 IP）`burst=5`、`每分钟补 10 个`。只对“会启动模型的请求”扣（`#记忆` 和不产生 prompt 的 `/命令` 不扣，见下）。
3. **全局每日预算**：`NANOCODE_WEB_DAILY_RUNS`（每天最多启动多少轮 agent，默认 200）；可选 `NANOCODE_WEB_DAILY_LLM_CALLS`（按 Provider 实际调用次数，默认 0=不限），在 Provider 层计数，到了直接抛 `QuotaExhausted` 让当前循环停下——这才是真正“封顶花钱”的那道闸。
4. **访问令牌**：已存在。只补一条：监听非本机地址且没设 `NANOCODE_WEB_TOKEN` 时启动打印警告（随机令牌嵌在页面里，对公网等于没有）；文档强调公网必须前面挂登录（oauth2-proxy / basic auth）。
5. 与 #1（429 熔断）配合：`nanocode/llm/base.py` L133 已有 `QuotaExhausted`，L134 对 429 仍会重试；每日预算闸用 `QuotaExhausted` 抛出即可复用“不再重试”的路径。

## 2. 配置项（环境变量，`0` = 关闭）

| 变量 | 默认 | 含义 |
|---|---|---|
| `NANOCODE_WEB_RATE_PER_MIN` | `10` | 每个客户端每分钟补充的令牌数 |
| `NANOCODE_WEB_RATE_BURST` | `5` | 令牌桶容量（允许的瞬时连发） |
| `NANOCODE_WEB_MAX_QUEUE` | `3` | busy 时最多排队几条 |
| `NANOCODE_WEB_DAILY_RUNS` | `200` | 每个自然日（本地时区）最多启动的 agent 轮数（含出队执行的） |
| `NANOCODE_WEB_DAILY_LLM_CALLS` | `0` | 每日模型 API 调用上限（Provider 层计数）；OpenRouter 免费 key 建议设 `45` |
| `NANOCODE_TRUST_PROXY` | `0` | `1` 时用 `X-Forwarded-For` 最左侧地址做限流 key；只在确实挂了反代时开，否则可被伪造 |

本机默认（127.0.0.1）下这些默认值对单人使用无感：10 次/分钟远高于人手发送频率。

## 3. 429 响应格式

沿用现有 `_reject` 的 `{"ok": false, "error": ...}`，加机器可读字段和 `Retry-After` 头：

```http
HTTP/1.1 429 Too Many Requests
Content-Type: application/json; charset=utf-8
Retry-After: 6

{"ok": false, "error": "发送太频繁，请 6 秒后再试", "code": "rate_limited", "retry_after": 6}
```

`code` 取值：`rate_limited`（令牌桶）、`queue_full`（队列满，`retry_after` 不给或给 5）、`daily_budget`（`retry_after` = 到次日 0 点的秒数）。

## 4. 代码改动

### 4.1 新文件 `nanocode/web/limits.py`

```python
"""Web 面板的限流与预算（issue #7）。纯内存、线程安全；单进程足够（nanocode --web 本来就是单进程单会话）。"""
from __future__ import annotations
import os, threading, time, datetime as dt

def env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, "") or default))
    except ValueError:
        return default

class TokenBucket:
    """每个 key 一个桶：容量 burst，每秒补 rate_per_min/60。返回 0 表示放行，否则返回建议等待秒数。"""
    MAX_KEYS = 10_000
    def __init__(self, rate_per_min: int, burst: int):
        self.rate = rate_per_min / 60.0
        self.burst = max(1, burst)
        self._b: dict[str, tuple[float, float]] = {}   # key -> (tokens, last)
        self._lock = threading.Lock()
    def take(self, key: str, now: float | None = None) -> float:
        if self.rate <= 0:
            return 0
        now = time.monotonic() if now is None else now
        with self._lock:
            if len(self._b) > self.MAX_KEYS:   # 防 key 爆炸：清掉已经补满的
                self._b = {k: v for k, v in self._b.items()
                           if v[0] + (now - v[1]) * self.rate < self.burst}
            tokens, last = self._b.get(key, (float(self.burst), now))
            tokens = min(self.burst, tokens + (now - last) * self.rate)
            if tokens >= 1:
                self._b[key] = (tokens - 1, now)
                return 0
            self._b[key] = (tokens, now)
            return (1 - tokens) / self.rate

class DailyCounter:
    """按本地自然日计数，跨天自动清零。limit=0 不限。"""
    def __init__(self, limit: int):
        self.limit, self.day, self.used = limit, dt.date.today(), 0
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
        return int((dt.datetime.combine(now.date() + dt.timedelta(days=1), dt.time()) - now).total_seconds()) + 1

class Limits:
    def __init__(self):
        self.bucket = TokenBucket(env_int("NANOCODE_WEB_RATE_PER_MIN", 10), env_int("NANOCODE_WEB_RATE_BURST", 5))
        self.max_queue = env_int("NANOCODE_WEB_MAX_QUEUE", 3)
        self.daily_runs = DailyCounter(env_int("NANOCODE_WEB_DAILY_RUNS", 200))
        self.trust_proxy = os.environ.get("NANOCODE_TRUST_PROXY", "") == "1"

class RateLimited(Exception):
    def __init__(self, code: str, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.code, self.message, self.retry_after = code, message, retry_after
```

### 4.2 `nanocode/web/server.py`

**WebApp.__init__**（L154 附近）：

```diff
+from .limits import DailyCounter, Limits, RateLimited
 ...
         self.queue: list[str] = []      # 忙时排队的消息，按先进先出执行
+        self.limits = Limits()
         session.permissions.ask_fn = self.ask
```

**WebApp.chat** 增加 `client` 参数；只在真正要跑模型时扣额度（`#` 记忆、纯本地 `/命令` 不扣）：

```diff
-    def chat(self, message: str) -> dict:
+    def chat(self, message: str, client: str = "local") -> dict:
         s = self.session
         with self.lock:
             message = message.strip()
             if message.startswith("#"):
                 return {"ok": True, "output": s.add_memory(message.lstrip("#"))}
             prompt = message
             if message.startswith("/"):
                 output, prompt = s.handle_command(message)
                 s.bus.emit("command", input=message, output=output)
                 if not prompt:
                     return {"ok": True, "output": output}
+            wait = self.limits.bucket.take(client)
+            if wait:
+                w = max(1, int(wait + 0.999))
+                raise RateLimited("rate_limited", f"发送太频繁，请 {w} 秒后再试", w)
             if s.agent.busy:
+                if self.limits.max_queue and len(self.queue) >= self.limits.max_queue:
+                    raise RateLimited("queue_full", f"排队已满（{self.limits.max_queue} 条），等当前任务做完或先中断", 5)
                 self.queue.append(prompt)
                 ...
                 return {"ok": True, "queued": True, "position": len(self.queue)}
+            if not self.limits.daily_runs.take():
+                raise RateLimited("daily_budget", "今天的调用预算已用完，明天再来（或调大 NANOCODE_WEB_DAILY_RUNS）",
+                                  DailyCounter.seconds_to_midnight())
             s.agent.busy = True
```

注意：`/命令` 的副作用发生在限流检查之前（和现状一致，命令本身不花模型钱）；若想更严，可把令牌桶检查挪到最前面。

**_run 出队时也要扣每日预算**（否则排队的那几条绕过预算）：

```diff
             with self.lock:
                 if not self.queue:
                     ...
                     return
+                if not self.limits.daily_runs.take():
+                    dropped = len(self.queue)
+                    self.queue.clear()
+                    self.session.agent.busy = False
+                    self.session.bus.emit("error", agent="main", retryable=False,
+                                          message=f"今天的调用预算已用完，已丢弃排队中的 {dropped} 条消息")
+                    self.session.bus.emit("idle")
+                    return
                 prompt = self.queue.pop(0)
```

**/api/cancel 顺带清队列**（否则“停止”只停当前那条，队列里的继续烧）：

```diff
             if path == "/api/cancel":
+                with app.lock:
+                    app.queue.clear()
                 app.session.agent.cancel.set()
```
（需确认前端 `index.html` 的队列条 `updateQueuebar()` 能靠 `/api/state` 的 `queue` 字段自愈；它在 L366 已经读 `s.queue`。如果产品上希望“停止”保留队列，就改成单独的 `/api/queue/clear`。）

**Handler：取客户端 key + 返回 429**

```diff
+        def _client_key(self) -> str:
+            if app.limits.trust_proxy:
+                xff = (self.headers.get("x-forwarded-for") or "").split(",")[0].strip()
+                if xff:
+                    return xff
+            return self.client_address[0]
 ...
             if path == "/api/chat":
-                return self._json(app.chat(str(data.get("message", ""))))
+                try:
+                    return self._json(app.chat(str(data.get("message", "")), self._client_key()))
+                except RateLimited as e:
+                    return self._limited(e)
+
+        def _limited(self, e: "RateLimited") -> None:
+            body = json.dumps({"ok": False, "error": e.message, "code": e.code,
+                               "retry_after": e.retry_after}, ensure_ascii=False).encode()
+            self.send_response(429)
+            self.send_header("Content-Type", "application/json; charset=utf-8")
+            if e.retry_after:
+                self.send_header("Retry-After", str(e.retry_after))
+            self.send_header("Content-Length", str(len(body)))
+            self.end_headers()
+            self.wfile.write(body)
```
（`_json` 目前不支持自定义头，所以单独写 `_limited`；也可以给 `_json` 加 `headers=()` 参数，改动更小更统一。）

**serve()**（L524）：非本机监听且未设 `NANOCODE_WEB_TOKEN` 时打印警告，并打印当前限流配置一行，方便排查。

### 4.3 （可选，第二步）Provider 层每日调用上限 `nanocode/llm/base.py`

在 `Provider` 发请求的循环（L95-135 一带，真正 `urlopen` 之前）加一个模块级 `DailyCounter(env_int("NANOCODE_WEB_DAILY_LLM_CALLS", 0))`，用完时 `raise QuotaExhausted("本地每日模型调用上限已到", status=429)`。这样即使单条 prompt 内部跑了 60 步也会被截断；`friendly_error`（server.py L107）已经认识 `QuotaExhausted`，前端会显示友好提示，且 L133 的路径不会重试。CLI 不设这个变量就不受影响。

### 4.4 前端 `nanocode/web/static/index.html`

发送处理 `/api/chat` 响应：`status===429` 时显示 `error`，并在 `retry_after` 秒内禁用发送按钮（注意 `setBusy` L203 现在会把 `#send.disabled=false`，需单独加一个 `cooldownUntil` 判断）。

### 4.5 多用户平台 `server/`

不改。网关已有每人每分钟 + 每日 token 额度（`gateway.py` L79-83）。沙箱里的 `nanocode --web` 读不到上面的新环境变量时用默认值，是“单人会话”，默认值不会误伤；如需，在 `server/nanocode_server/runtime/*` 启动沙箱时透传。

## 5. 与 #5（全局 busy 锁）的关系

- 现在 busy 锁 = 全局并发 1，本方案**不改变**它，只给它前面加闸、给队列加上限。
- 限流 key 按 IP，而 Session 是全员共享的：这正是 #5 的问题，本方案不解决隔离，只防止“一个人刷爆所有人的额度”。
- 将来 #5 做成 per-user session + worker 池时：`TokenBucket` 的 key 换成 user/session id；`max_queue` 变 per-session；需要新增**全局并发信号量**（`NANOCODE_WEB_MAX_CONCURRENT`），因为那时 busy 锁不再是全局的。`limits.py` 已按可扩展的方式写。
- #5 正文里“直接拒绝”的描述已过时（现为无界排队），建议修 #7 时在 #5 留言更新现状（需你同意后再发）。

## 6. 测试（新增 `tests/test_web_ratelimit.py`，沿用 `tests/test_web_busy.py` 的 mock provider + `make_server(..., 0)` 骨架）

1. `TokenBucket` 单元测试：注入 `now`，burst 次放行、第 burst+1 次返回 >0 的等待秒数、过 60/rate 秒后恢复；`rate=0` 永远放行；key 超过 `MAX_KEYS` 时会清理。
2. `DailyCounter`：limit 次后拒绝；mock `dt.date.today` 跨天清零；`limit=0` 不限。
3. HTTP：设 `NANOCODE_WEB_RATE_BURST=2`，连发 3 次 `/api/chat` → 第 3 次 429，含 `Retry-After` 头和 `code=rate_limited`。
4. 队列上限：`MAX_QUEUE=1`，让 mock 第一轮卡在权限询问（test_web_busy 已有这个手法），再发 2 条 → 第 1 条 `queued`，第 2 条 429 `queue_full`。
5. 每日预算：`DAILY_RUNS=1`，第二轮（包括出队路径）被拒并发出 `error` 事件，`busy` 回到 False。
6. `#记忆` 与不产生 prompt 的 `/命令` 不扣令牌。
7. `TRUST_PROXY`：不开时伪造 `X-Forwarded-For` 不影响 key；开了按 XFF 区分。
8. `/api/cancel` 清空队列。
9. （若做 4.3）Provider 计数到上限抛 `QuotaExhausted`，且不重试（可放进 `tests/test_quota_retry.py`）。
10. 回归：`tests/test_web_busy.py`、`test_web_hardening.py`、`test_web_public_host.py` 全绿。测试里 setUp 时用 `mock.patch.dict(os.environ, ...)`，避免默认限流影响旧用例（默认 burst=5 应该够，但要确认旧用例没有一口气发 >5 条）。

运行：`python3 -m unittest discover -s tests`。

## 7. 上线步骤

1. PR 1（低风险）：`limits.py` + 队列上限 + 令牌桶 + 429 + cancel 清队列 + 测试 + README 环境变量表（README L242-247 那张表）补 5 行。
2. PR 2：每日预算（run 级）+ Provider 级 LLM 调用上限 + 前端冷却提示。
3. 现有公网部署：先用宽松值观察（`RATE_PER_MIN=30, MAX_QUEUE=5`），看日志里 429 次数，再收紧到默认值。挂反代的同时设 `NANOCODE_TRUST_PROXY=1`，否则所有人共用反代 IP 一个桶。
4. 合并后关闭 #7，并在 #1、#5 交叉引用。
5. 回滚：把对应变量设为 `0` 即关闭，无需回滚代码。

## 8. 不在本次范围

- 用量统计/预算告警面板（#7 建议 3）：平台路径已有 `UsageMeter`；单机版可后续把 `DailyCounter` 状态暴露到 `/api/state`。
- 持久化计数（重启清零是可接受的；要防重启绕过，需要落盘到 `NANOCODE_HOME`）。
