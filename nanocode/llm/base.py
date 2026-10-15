"""Provider 抽象 + HTTP/SSE 基础设施。

一个 Provider 只需要做一件事：

    complete(system, messages, tools, max_tokens, on_text) -> LLMResponse

  - 输入：统一格式的 system 提示词、消息列表、工具定义
  - 输出：统一格式的 LLMResponse（内容块 + 停止原因 + token 用量）
  - on_text：流式回调，模型每吐出一段文字就调用一次，让界面能"打字机"式显示

只要实现这个接口，任何厂商的模型都能接进 Agent —— Agent 循环完全不知道
背后是 Claude、GPT、DeepSeek 还是本地 Ollama。这就是"可接入任何一家 API"的关键。
"""

from __future__ import annotations

import datetime
import json
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Callable, Iterator, Optional

from ..messages import Block, LLMResponse, Message

TextCallback = Optional[Callable[[str], None]]


class LLMError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class QuotaExhausted(LLMError):
    """配额用尽（比如免费档每日限额）。重试没有意义：要等到重置时间之后才会恢复。"""


# 429 分两种：瞬时限流（每分钟 N 次，几秒后重试就行）和配额用尽（每日限额，要等几小时）。
# 服务端告诉我们的重置时间离得比这个远，就当作配额用尽。
QUOTA_WAIT_SECONDS = 120
QUOTA_KEYWORDS = ("per-day", "per day", "daily", "quota")


def parse_reset(headers, now: float | None = None) -> float | None:
    """从响应头算出"配额什么时候恢复"（Unix 时间戳，秒），拿不到返回 None。

    X-RateLimit-Reset 在不同服务里写法不一样：毫秒时间戳、秒时间戳、或者"还有多少秒"，按数值大小区分。
    Retry-After 是"还有多少秒"。
    """
    now = time.time() if now is None else now
    if headers is None:
        return None
    reset = headers.get("X-RateLimit-Reset")
    if reset:
        try:
            v = float(reset)
        except ValueError:
            v = None
        if v is not None and v > 0:
            if v > 1e11:
                return v / 1000      # 毫秒时间戳
            if v > 1e8:
                return v             # 秒时间戳
            return now + v           # 相对秒数
    retry_after = headers.get("Retry-After")
    if retry_after:
        try:
            return now + float(retry_after)
        except ValueError:
            pass
    return None


def quota_exhausted_message(headers, detail: str, now: float | None = None) -> str | None:
    """判断一个 429 是不是配额用尽。是就返回给用户看的说明（含本地时间的恢复时间），不是返回 None。"""
    now = time.time() if now is None else now
    reset_at = parse_reset(headers, now)
    wait = reset_at - now if reset_at is not None else None
    by_header = wait is not None and wait > QUOTA_WAIT_SECONDS
    by_text = any(k in detail.lower() for k in QUOTA_KEYWORDS)
    if not (by_header or by_text):
        return None
    msg = "HTTP 429：配额已用完，不再重试。"
    if reset_at is not None and wait > 0:
        local = datetime.datetime.fromtimestamp(reset_at).astimezone()
        hours, minutes = divmod(int(wait) // 60, 60)
        msg += f"预计 {local:%Y-%m-%d %H:%M %Z} 恢复（约 {hours} 小时 {minutes} 分钟后）。"
    else:
        msg += "服务端没有说明什么时候恢复。"
    return msg + f"\n服务端返回：{detail[:300]}"


_daily_calls = None


def take_daily_llm_call() -> None:
    """本地每日模型调用上限（环境变量 NANOCODE_WEB_DAILY_LLM_CALLS，默认 0 = 不限，issue #7）。

    在真正发 HTTP 请求前计数，一条 prompt 内部跑了 60 步也会在上限处截断。
    超限抛 QuotaExhausted：复用"配额用尽不再重试"的路径，Web 面板也会显示友好提示。
    """
    global _daily_calls
    from ..web.limits import DailyCounter, env_int   # 延迟导入，避免 llm 依赖 web 的导入顺序
    if _daily_calls is None:
        _daily_calls = DailyCounter(env_int("NANOCODE_WEB_DAILY_LLM_CALLS", 0))
    if not _daily_calls.take():
        raise QuotaExhausted(f"本地每日模型调用上限已到（NANOCODE_WEB_DAILY_LLM_CALLS={_daily_calls.limit}），"
                             "明天再来或调大这个值。", status=429)


class Provider(ABC):
    name = "base"

    def __init__(self, model: str, api_key: str = "", base_url: str = "", timeout: float = 300.0,
                 max_retries: int = 3, extra_headers: dict | None = None):
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.extra_headers = extra_headers or {}

    @abstractmethod
    def complete(self, system: str, messages: list[Message], tools: list[dict],
                 max_tokens: int = 4096, on_text: TextCallback = None) -> LLMResponse:
        """调用模型。tools 是统一格式：[{name, description, input_schema}]。"""

    # ------------------------------------------------------------------ HTTP

    def _post_stream(self, url: str, headers: dict, body: dict) -> Iterator[tuple[str, str]]:
        """POST 一个流式请求，逐个产出 (event, data)。带指数退避重试。

        只在"还没开始收到数据"时重试：流已经开始后失败就直接抛错，
        否则用户会看到重复的输出。
        """
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        all_headers = {"content-type": "application/json", **headers, **self.extra_headers}
        take_daily_llm_call()   # 重试不重复计数
        attempt = 0
        while True:
            req = urllib.request.Request(url, data=data, headers=all_headers, method="POST")
            try:
                resp = urllib.request.urlopen(req, timeout=self.timeout)
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:2000]
                if e.code == 429:
                    quota_msg = quota_exhausted_message(e.headers, detail)
                    if quota_msg:
                        # 配额类 429 在重置时间之前 100% 失败，重试只会白等、还可能多占请求次数
                        raise QuotaExhausted(quota_msg, status=429) from None
                retryable = e.code == 429 or e.code >= 500
                if retryable and attempt < self.max_retries:
                    attempt += 1
                    time.sleep(min(2 ** attempt, 20))
                    continue
                raise LLMError(f"HTTP {e.code}: {detail}", status=e.code) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt < self.max_retries:
                    attempt += 1
                    time.sleep(min(2 ** attempt, 20))
                    continue
                raise LLMError(f"网络错误：{e}") from None
            with resp:
                yield from iter_sse(resp)
            return


def iter_sse(lines) -> Iterator[tuple[str, str]]:
    """解析 Server-Sent Events。

    SSE 是纯文本协议，每个事件由若干行组成，空行表示一个事件结束：

        event: content_block_delta
        data: {"type": "content_block_delta", ...}

    OpenAI 风格的流只有 data 行，最后以 `data: [DONE]` 结束。
    """
    event, data_lines = "message", []
    for raw in lines:
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        line = line.rstrip("\r\n")
        if not line:
            if data_lines:
                yield event, "\n".join(data_lines)
            event, data_lines = "message", []
            continue
        if line.startswith(":"):
            continue  # 注释 / 心跳
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            event = value
        elif field == "data":
            data_lines.append(value)
    if data_lines:
        yield event, "\n".join(data_lines)


def parse_tool_input(raw: str) -> dict:
    """模型生成的工具参数是 JSON 字符串，可能不合法 —— 交给工具层报错，让模型自己修正。"""
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {"__invalid_json__": raw}
    except json.JSONDecodeError:
        return {"__invalid_json__": raw}


def finalize_text_blocks(blocks: list[Block]) -> list[Block]:
    """去掉空文本块（很多 API 不接受空文本）。"""
    return [b for b in blocks if not (b["type"] == "text" and not b["text"])]
