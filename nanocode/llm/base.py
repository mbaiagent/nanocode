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
        attempt = 0
        while True:
            req = urllib.request.Request(url, data=data, headers=all_headers, method="POST")
            try:
                resp = urllib.request.urlopen(req, timeout=self.timeout)
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:2000]
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
