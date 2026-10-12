"""OpenAI 兼容协议（Chat Completions）。

国内外绝大多数厂商都提供这种协议：OpenAI、DeepSeek、通义千问（DashScope 兼容模式）、
Kimi（Moonshot）、智谱 GLM、硅基流动、OpenRouter、本地 Ollama / vLLM ……
所以实现这一个 provider，就能接入几十家模型。

这个文件的核心是"翻译"：

  统一格式（内容块）                    OpenAI 格式
  ─────────────────────────────────────────────────────────────────
  system 参数                     →   {"role": "system", "content": ...}
  assistant 的 tool_use 块         →   assistant 消息的 tool_calls 字段（arguments 是 JSON 字符串）
  user 消息里的 tool_result 块      →   单独的 {"role": "tool", "tool_call_id": ...} 消息
  tools: {name, input_schema}      →   {"type": "function", "function": {name, parameters}}
"""

from __future__ import annotations

import json

from ..messages import LLMResponse, Message, Usage, estimate_tokens, text_block, tool_use_block
from .base import LLMError, Provider, TextCallback, finalize_text_blocks, parse_tool_input


class OpenAICompatProvider(Provider):
    name = "openai"

    def __init__(self, *args, stream_usage: bool = True, **kwargs):
        super().__init__(*args, **kwargs)
        self.stream_usage = stream_usage

    # ------------------------------------------------------------ 翻译：请求

    @staticmethod
    def to_openai_messages(system: str, messages: list[Message]) -> list[dict]:
        out: list[dict] = [{"role": "system", "content": system}] if system else []
        for msg in messages:
            if msg["role"] == "assistant":
                text = "".join(b["text"] for b in msg["content"] if b["type"] == "text")
                calls = [
                    {"id": b["id"], "type": "function",
                     "function": {"name": b["name"], "arguments": json.dumps(b["input"], ensure_ascii=False)}}
                    for b in msg["content"] if b["type"] == "tool_use"
                ]
                m: dict = {"role": "assistant", "content": text or None}
                if calls:
                    m["tool_calls"] = calls
                out.append(m)
            else:
                # user 消息：工具结果要拆成独立的 tool 消息，并且必须排在普通文本前面
                texts = []
                for b in msg["content"]:
                    if b["type"] == "tool_result":
                        content = b["content"]
                        if b.get("is_error"):
                            content = f"[ERROR] {content}"
                        out.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": content})
                    elif b["type"] == "text":
                        texts.append(b["text"])
                if texts:
                    out.append({"role": "user", "content": "\n\n".join(texts)})
        return out

    @staticmethod
    def to_openai_tools(tools: list[dict]) -> list[dict]:
        return [{"type": "function",
                 "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
                for t in tools]

    # ------------------------------------------------------------ 调用 + 翻译：响应

    def complete(self, system, messages, tools, max_tokens=4096, on_text: TextCallback = None) -> LLMResponse:
        body: dict = {
            "model": self.model,
            "messages": self.to_openai_messages(system, messages),
            "max_tokens": max_tokens,
            "stream": True,
        }
        if tools:
            body["tools"] = self.to_openai_tools(tools)
        if self.stream_usage:
            body["stream_options"] = {"include_usage": True}
        headers = {"authorization": f"Bearer {self.api_key}"} if self.api_key else {}

        text = ""
        calls: dict[int, dict] = {}  # 流式时，工具调用按 index 分片到达，要拼起来
        finish = None
        usage = None
        for _event, data in self._post_stream(f"{self.base_url}/chat/completions", headers, body):
            if data.strip() == "[DONE]":
                break
            chunk = json.loads(data)
            if chunk.get("error"):
                raise LLMError(str(chunk["error"]))
            if chunk.get("usage"):
                u = chunk["usage"]
                usage = Usage(u.get("prompt_tokens", 0) or 0, u.get("completion_tokens", 0) or 0)
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    text += delta["content"]
                    if on_text:
                        on_text(delta["content"])
                for tc in delta.get("tool_calls") or []:
                    slot = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                    slot["id"] = tc.get("id") or slot["id"]
                    fn = tc.get("function") or {}
                    slot["name"] += fn.get("name") or ""
                    slot["args"] += fn.get("arguments") or ""
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]

        blocks = [text_block(text)]
        for i, c in enumerate(calls[k] for k in sorted(calls)):
            blocks.append(tool_use_block(c["id"] or f"call_{i}", c["name"], parse_tool_input(c["args"])))
        blocks = finalize_text_blocks(blocks)

        if calls:
            stop = "tool_use"
        elif finish == "length":
            stop = "max_tokens"
        else:
            stop = "end_turn"
        if usage is None:  # 有些厂商流式不返回 usage，用估算值
            usage = Usage(estimate_tokens(system) + estimate_tokens(messages), estimate_tokens(blocks), estimated=True)
        return LLMResponse(content=blocks, stop_reason=stop, usage=usage)
