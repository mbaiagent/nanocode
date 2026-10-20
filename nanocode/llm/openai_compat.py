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
  assistant 的 reasoning 块         ↔   assistant 消息的 reasoning_content（只发回给产出它的同一个端点 + 模型，#99）
"""

from __future__ import annotations

import json
from urllib.parse import urlsplit

from ..messages import (LLMResponse, Message, Usage, estimate_tokens, reasoning_block, reasoning_of, text_block,
                        tool_use_block)
from .base import LLMError, Provider, TextCallback, finalize_text_blocks, parse_tool_input


class OpenAICompatProvider(Provider):
    name = "openai"

    def __init__(self, *args, stream_usage: bool = True, **kwargs):
        super().__init__(*args, **kwargs)
        self.stream_usage = stream_usage

    @property
    def source(self) -> str:
        """这个端点的身份，记在 reasoning 块上：协议 + 规范化后的 base_url + 模型名。

        要带模型名：多用户平台里所有 OpenAI 协议的模型共用同一个网关地址（http://网关/v1），
        用户中途从 DeepSeek 换成 OpenAI 时 base_url 不变 —— 只看地址的话，DeepSeek 的思考内容
        就会被转发给不认识这个字段的厂商。
        """
        u = urlsplit(self.base_url)
        return f"openai:{u.scheme.lower()}://{u.netloc.lower()}{u.path.rstrip('/')}#{self.model}"

    # ------------------------------------------------------------ 翻译：请求

    @staticmethod
    def to_openai_messages(system: str, messages: list[Message], reasoning_source: str | None = None) -> list[dict]:
        """reasoning_source：当前端点的身份（见 source）。

        历史里有这个端点产出的 reasoning 块时，每条 assistant 消息都带上 reasoning_content：
        DeepSeek 思考模式下带 tools 的请求要求回传**所有**历史轮次的 reasoning_content（没产出
        思考的轮次，比如压缩后补的确认消息，带空字符串），否则 400；不带 tools 时服务端会忽略它。
        别的端点（OpenAI、千问、Kimi、GLM、OpenRouter……）产出的历史、或者不传 reasoning_source，
        请求和以前完全一样，不会多出一个它们不认识的字段。
        """
        out: list[dict] = [{"role": "system", "content": system}] if system else []
        send_reasoning = bool(reasoning_source) and any(
            reasoning_of(m, reasoning_source) is not None for m in messages if m["role"] == "assistant")
        for msg in messages:
            if msg["role"] == "assistant":
                text = "".join(b["text"] for b in msg["content"] if b["type"] == "text")
                calls = [
                    {"id": b["id"], "type": "function",
                     "function": {"name": b["name"], "arguments": json.dumps(b["input"], ensure_ascii=False)},
                     # Gemini 的 thinking 模型要求把 extra_content.google.thought_signature 原样带回，否则 400；
                     # 纯 OpenAI 的服务端不会返回它，所以没有这个键时请求和以前完全一样
                     **({"extra_content": b["extra_content"]} if b.get("extra_content") else {})}
                    for b in msg["content"] if b["type"] == "tool_use"
                ]
                m: dict = {"role": "assistant", "content": text or None}
                if send_reasoning:
                    m["reasoning_content"] = reasoning_of(msg, reasoning_source) or ""
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
            "messages": self.to_openai_messages(system, messages, self.source),
            "max_tokens": max_tokens,
            "stream": True,
        }
        if tools:
            body["tools"] = self.to_openai_tools(tools)
        if self.stream_usage:
            body["stream_options"] = {"include_usage": True}
        headers = {"authorization": f"Bearer {self.api_key}"} if self.api_key else {}

        text = ""
        reasoning = ""   # DeepSeek 等思考模式的 reasoning_content：存下来，下一轮要原样回传（#99）
        calls: dict[int, dict] = {}  # 流式时，工具调用按 index 分片到达，要拼起来
        finish = None
        usage = None
        for _event, data in self._post_stream(f"{self.base_url}/chat/completions", headers, body):
            if data.strip() == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                # 代理/网关插入的非 JSON 行（如 keep-alive）：跟 Anthropic 那边 #20 一样跳过，
                # 不能让整轮对话崩溃（#139）
                continue
            if not isinstance(chunk, dict):
                continue
            if chunk.get("error"):
                raise LLMError(str(chunk["error"]))
            if chunk.get("usage"):
                u = chunk["usage"]
                details = u.get("completion_tokens_details") or {}
                usage = Usage(u.get("prompt_tokens", 0) or 0, u.get("completion_tokens", 0) or 0,
                              reasoning_tokens=(details.get("reasoning_tokens") or 0) if isinstance(details, dict) else 0)
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("reasoning_content"):
                    reasoning += delta["reasoning_content"]      # 不走 on_text：思考过程不是回复正文
                if delta.get("content"):
                    text += delta["content"]
                    if on_text:
                        on_text(delta["content"])
                for tc in delta.get("tool_calls") or []:
                    slot = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                    slot["id"] = tc.get("id") or slot["id"]
                    if tc.get("extra_content"):                 # 可能在任意一个分片里到达
                        slot["extra"] = tc["extra_content"]
                    fn = tc.get("function") or {}
                    slot["name"] += fn.get("name") or ""
                    slot["args"] += fn.get("arguments") or ""
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]

        blocks = [reasoning_block(reasoning, self.source)] if reasoning else []
        blocks.append(text_block(text))
        for i, c in enumerate(calls[k] for k in sorted(calls)):
            blocks.append(tool_use_block(c["id"] or f"call_{i}", c["name"], parse_tool_input(c["args"]), c.get("extra")))
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
