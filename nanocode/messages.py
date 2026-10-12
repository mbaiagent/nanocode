"""统一消息格式。

Agent 的全部"记忆"就是一个 messages 列表。nanocode 内部统一使用
"内容块"（content block）格式，它和 Anthropic Messages API 的格式几乎一样，
因为这种格式能把"文本"、"工具调用"、"工具结果"放在同一种结构里，最容易理解：

    {"role": "user",      "content": [{"type": "text", "text": "帮我修 bug"}]}
    {"role": "assistant", "content": [{"type": "text", "text": "我先看看文件"},
                                      {"type": "tool_use", "id": "t1",
                                       "name": "read_file", "input": {"path": "a.py"}}]}
    {"role": "user",      "content": [{"type": "tool_result", "tool_use_id": "t1",
                                       "content": "1\tprint('hi')", "is_error": False}]}

关键规则（所有厂商 API 都有类似约束）：
  1. user / assistant 交替出现；
  2. 每个 tool_use 后面，下一条 user 消息里必须有对应 id 的 tool_result；
  3. 工具结果是以 "user" 角色发回去的 —— 对模型而言，工具输出就是"环境的反馈"。

各家 provider（llm/ 目录）负责把这个格式翻译成自己 API 的格式。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

Block = dict[str, Any]
Message = dict[str, Any]


def text_block(text: str) -> Block:
    return {"type": "text", "text": text}


def tool_use_block(id: str, name: str, input: dict) -> Block:
    return {"type": "tool_use", "id": id, "name": name, "input": input}


def tool_result_block(tool_use_id: str, content: str, is_error: bool = False) -> Block:
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": content, "is_error": is_error}


def user_message(text: str) -> Message:
    return {"role": "user", "content": [text_block(text)]}


def assistant_message(blocks: list[Block]) -> Message:
    return {"role": "assistant", "content": blocks}


def text_of(message: Message) -> str:
    """取出一条消息里所有文本块拼起来的内容。"""
    return "".join(b.get("text", "") for b in message.get("content", []) if b.get("type") == "text")


def tool_uses(message: Message) -> list[Block]:
    return [b for b in message.get("content", []) if b.get("type") == "tool_use"]


def is_tool_result_message(message: Message) -> bool:
    """只包含工具结果的 user 消息 —— 它是一轮循环的中间步骤，而不是人说的话。"""
    content = message.get("content", [])
    return message["role"] == "user" and bool(content) and all(b.get("type") == "tool_result" for b in content)


def estimate_tokens(obj: Any) -> int:
    """粗略估算 token 数。

    真实 token 数要用各家的 tokenizer；这里用一个够用的近似：
    英文约 4 字符 / token，中文约 1~1.5 字 / token。
    """
    s = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    ascii_chars = sum(1 for c in s if ord(c) < 128)
    other = len(s) - ascii_chars
    return ascii_chars // 4 + other + 1


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    estimated: bool = False  # provider 没返回 usage 时用估算值

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.estimated = self.estimated or other.estimated

    def to_dict(self) -> dict:
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens, "estimated": self.estimated}


@dataclass
class LLMResponse:
    """一次模型调用的结果，已经被 provider 翻译成统一格式。"""

    content: list[Block] = field(default_factory=list)
    stop_reason: str = "end_turn"  # end_turn | tool_use | max_tokens
    usage: Usage = field(default_factory=Usage)
