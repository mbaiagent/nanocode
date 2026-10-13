"""Anthropic Messages API（Claude 原生协议）。

nanocode 的统一格式本来就是按这个协议设计的，所以翻译几乎为零。
值得学习的是它的流式事件，它把一次回复拆成若干"内容块"：

    message_start          → 输入 token 数
    content_block_start    → 开始一个块（text 或 tool_use，tool_use 带 id 和 name）
    content_block_delta    → text_delta（文字片段）或 input_json_delta（工具参数 JSON 片段）
    content_block_stop     → 块结束
    message_delta          → stop_reason、输出 token 数
    message_stop

另外演示了 Prompt Caching：给 system 提示词打上 cache_control，
多轮对话中这部分前缀会被缓存，大幅降低延迟和费用 —— Agent 每一步都要
重发整个上下文，所以缓存对 Agent 特别重要。
"""

from __future__ import annotations

import json

from ..messages import LLMResponse, Message, Usage
from .base import LLMError, Provider, TextCallback, finalize_text_blocks, parse_tool_input


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, *args, prompt_cache: bool = True, **kwargs):
        super().__init__(*args, **kwargs)
        self.prompt_cache = prompt_cache

    @staticmethod
    def to_anthropic_messages(messages: list[Message]) -> list[dict]:
        out = []
        for msg in messages:
            blocks = []
            for b in msg["content"]:
                if b["type"] == "tool_result":
                    blocks.append({"type": "tool_result", "tool_use_id": b["tool_use_id"],
                                   "content": b["content"] or "(空)", "is_error": bool(b.get("is_error"))})
                elif b["type"] == "text":
                    if b["text"]:
                        blocks.append({"type": "text", "text": b["text"]})
                else:
                    blocks.append(b)
            out.append({"role": msg["role"], "content": blocks or [{"type": "text", "text": "(空)"}]})
        return out

    def complete(self, system, messages, tools, max_tokens=4096, on_text: TextCallback = None) -> LLMResponse:
        body: dict = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": self.to_anthropic_messages(messages),
            "stream": True,
        }
        if system:
            sys_block = {"type": "text", "text": system}
            if self.prompt_cache:
                sys_block["cache_control"] = {"type": "ephemeral"}
            body["system"] = [sys_block]
        if tools:
            body["tools"] = [dict(t) for t in tools]
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}

        blocks: list[dict] = []
        json_buf: dict[int, str] = {}
        stop = "end_turn"
        usage = Usage()
        for _event, data in self._post_stream(f"{self.base_url}/v1/messages", headers, body):
            ev = json.loads(data)
            t = ev.get("type")
            if t == "message_start":
                # 兼容端点（如 OpenRouter）会显式返回 null；dict.get 的默认值只在"键缺失"时生效，所以全部用 `or 0`
                u = (ev.get("message") or {}).get("usage") or {}
                usage.input_tokens = ((u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
                                      + (u.get("cache_creation_input_tokens") or 0))
            elif t == "content_block_start":
                cb = ev["content_block"]
                if cb["type"] == "text":
                    blocks.append({"type": "text", "text": cb.get("text") or ""})
                elif cb["type"] == "tool_use":
                    blocks.append({"type": "tool_use", "id": cb["id"], "name": cb["name"], "input": {}})
                    json_buf[len(blocks) - 1] = ""
                else:
                    blocks.append({"type": "_ignored"})  # thinking 等其它块，本项目不处理
            elif t == "content_block_delta":
                d, idx = ev["delta"], len(blocks) - 1
                if d["type"] == "text_delta":
                    piece = d.get("text") or ""
                    blocks[idx]["text"] += piece
                    if on_text and piece:
                        on_text(piece)
                elif d["type"] == "input_json_delta":
                    json_buf[idx] += d.get("partial_json") or ""
            elif t == "content_block_stop":
                idx = len(blocks) - 1
                if idx in json_buf:
                    blocks[idx]["input"] = parse_tool_input(json_buf.pop(idx))
            elif t == "message_delta":
                stop = ev["delta"].get("stop_reason") or stop
                out = (ev.get("usage") or {}).get("output_tokens")
                if out is not None:
                    usage.output_tokens = out
            elif t == "error":
                raise LLMError(str(ev.get("error")))

        blocks = finalize_text_blocks([b for b in blocks if b["type"] != "_ignored"])
        if any(b["type"] == "tool_use" for b in blocks):
            stop = "tool_use"
        elif stop not in ("max_tokens",):
            stop = "end_turn"
        return LLMResponse(content=blocks, stop_reason=stop, usage=usage)
