"""Agent 核心循环 —— 整个项目最重要的文件。

一个编程 Agent 的本质只有十几行：

    messages = [用户的请求]
    while True:
        回复 = 模型(system, messages, tools)
        messages.append(回复)
        if 回复里没有工具调用:
            return 回复的文字                      # 模型认为任务完成了
        结果 = [执行(工具调用) for 工具调用 in 回复]
        messages.append(结果)                      # 把"环境反馈"交给模型，进入下一轮

"决定下一步做什么"完全交给模型；程序只负责执行、反馈、以及加上各种安全措施。
下面的 Agent 类就是这个循环，外加：
  - 流式输出、事件通知（给界面用）
  - 权限检查 + hooks（安全带）
  - 只读工具并行执行（提速）
  - 上下文快满时自动压缩（续航）
  - 用户中断、步数上限（刹车）
  - 子 Agent（分工）
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from .context import BASE_PROMPT, compact_messages, context_tokens, find_compact_split
from .events import EventBus
from .hooks import HookRunner
from .llm.base import LLMError, Provider
from .messages import (Usage, assistant_message, text_block, text_of, tool_result_block, tool_uses)
from .permissions import PermissionManager
from .tools.base import ToolContext, ToolRegistry, truncate


class Cancelled(Exception):
    pass


SUBAGENT_PROMPT = BASE_PROMPT + """

# 你的身份
你是被主 Agent 派出去完成一项子任务的子 Agent。你看不到主对话，只能看到下面的任务说明。
完成后用一段简洁的文字汇报结论（这是主 Agent 唯一能看到的内容），包含关键的文件路径和事实，不要寒暄。"""


class Agent:
    def __init__(self, provider: Provider, tools: ToolRegistry, permissions: PermissionManager,
                 hooks: HookRunner, bus: EventBus, cwd: str, system_prompt: Callable[[], str],
                 name: str = "main", depth: int = 0, max_steps: int = 60, max_tokens: int = 8192,
                 context_window: int = 128000, compact_ratio: float = 0.8, skills: dict | None = None,
                 cancel: threading.Event | None = None):
        self.provider = provider
        self.tools = tools
        self.permissions = permissions
        self.hooks = hooks
        self.bus = bus
        self.cwd = cwd
        self.system_prompt = system_prompt
        self.name = name
        self.depth = depth
        self.max_steps = max_steps
        self.max_tokens = max_tokens
        self.context_window = context_window
        self.compact_ratio = compact_ratio
        self.messages: list[dict] = []
        self.usage = Usage()
        self.state: dict = {"todos": [], "read_files": {}}
        self.cancel = cancel or threading.Event()
        self.busy = False
        self._compacted_at: int | None = None
        self.ctx = ToolContext(cwd=cwd, state=self.state, emit=self.emit, cancel=self.cancel,
                               run_subagent=self._run_subagent if depth == 0 else None, skills=skills or {})

    def emit(self, type: str, **data) -> None:
        self.bus.emit(type, agent=self.name, **data)

    # =================================================================== 主循环

    def run(self, prompt: str) -> str:
        self.cancel.clear()
        self.busy = True
        try:
            return self._run(prompt)
        finally:
            self.busy = False

    def _run(self, prompt: str) -> str:
        self.emit("user", text=prompt)
        blocks = [text_block(prompt)]

        # Hook：UserPromptSubmit —— 可以拦截请求，或附加额外上下文
        if self.depth == 0 and self.hooks.has("UserPromptSubmit"):
            out = self.hooks.run("UserPromptSubmit", {"prompt": prompt})
            self._report_hook("UserPromptSubmit", out)
            if out.blocked:
                self.emit("turn_end", final="", blocked=True, usage=self.usage.to_dict())
                return f"请求被 hook 拦截：{out.message}"
            if out.message:
                blocks.append(text_block(f"<hook-context>\n{out.message}\n</hook-context>"))
        self._append_user(blocks)

        final = ""
        for step in range(1, self.max_steps + 1):
            if self.cancel.is_set():
                final = "(已中断)"
                break
            self._maybe_compact()

            # ① 调用模型
            system, tools = self.system_prompt(), self.tools.schemas()
            self.emit("llm_request", step=step, messages=len(self.messages),
                      context_tokens=context_tokens(system, self.messages, tools), context_window=self.context_window)
            started = time.time()
            try:
                resp = self.provider.complete(system, self.messages, tools, self.max_tokens, on_text=self._on_text)
            except Cancelled:
                final = "(已中断)"
                break
            except LLMError as e:
                self.emit("error", message=str(e))
                final = f"模型调用失败：{e}"
                break
            self.usage.add(resp.usage)
            content = resp.content or [text_block("(空回复)")]
            self.messages.append(assistant_message(content))
            self.emit("llm_response", step=step, content=content, stop_reason=resp.stop_reason,
                      usage=resp.usage.to_dict(), total_usage=self.usage.to_dict(),
                      seconds=round(time.time() - started, 2))

            # ② 没有工具调用 → 模型认为完成了
            calls = tool_uses(self.messages[-1])
            if not calls:
                final = text_of(self.messages[-1])
                if resp.stop_reason == "max_tokens":
                    final += "\n\n(回复因达到 max_tokens 被截断)"
                # Hook：Stop —— 可以不让它停，比如"测试没过不许结束"
                if self.depth == 0 and self.hooks.has("Stop"):
                    out = self.hooks.run("Stop", {"final": final})
                    self._report_hook("Stop", out)
                    if out.blocked:
                        self._append_user([text_block(f"<hook-feedback>\n{out.message}\n</hook-feedback>")])
                        continue
                break

            # ③ 执行工具，把结果作为 user 消息追加 → 下一轮
            results = self._run_tools(calls)
            self.messages.append({"role": "user", "content": results})
        else:
            final = f"(达到最大步数 {self.max_steps}，已停止)"

        self.emit("turn_end", final=final, usage=self.usage.to_dict())
        return final

    def _on_text(self, delta: str) -> None:
        if self.cancel.is_set():
            raise Cancelled()
        self.emit("text_delta", text=delta)

    def _append_user(self, blocks: list[dict]) -> None:
        """追加 user 消息；如果上一条也是 user，就合并，保证 user/assistant 交替。"""
        if self.messages and self.messages[-1]["role"] == "user":
            self.messages[-1]["content"].extend(blocks)
        else:
            self.messages.append({"role": "user", "content": blocks})

    # =================================================================== 工具执行

    def _run_tools(self, calls: list[dict]) -> list[dict]:
        # 先依次过权限（可能要问用户，必须串行），再执行
        plans = [self._prepare(call) for call in calls]
        runnable = [p for p in plans if p["result"] is None]
        if len(runnable) > 1 and all(p["tool"].is_read_only for p in runnable):
            # 多个只读工具互不影响 → 并行执行
            with ThreadPoolExecutor(max_workers=min(8, len(runnable))) as pool:
                list(pool.map(self._execute, runnable))
        else:
            for p in runnable:
                self._execute(p)
        return [p["result"] for p in plans]

    def _prepare(self, call: dict) -> dict:
        plan = {"call": call, "tool": None, "result": None}
        self.emit("tool_call", id=call["id"], name=call["name"], input=call["input"])
        if self.cancel.is_set():
            plan["result"] = self._finish(call, "用户中断了操作，工具未执行", True)
            return plan
        tool = self.tools.get(call["name"])
        if tool is None:
            plan["result"] = self._finish(call, f"未知工具 {call['name']}，可用工具：{', '.join(self.tools.names())}", True)
            return plan
        plan["tool"] = tool
        decision = self.permissions.resolve(tool, call["input"])
        self.emit("permission", id=call["id"], name=call["name"], behavior=decision.behavior, reason=decision.reason)
        if decision.behavior == "deny":
            plan["result"] = self._finish(call, f"权限被拒绝：{decision.reason}", True)
            return plan
        if self.hooks.has("PreToolUse"):
            out = self.hooks.run("PreToolUse", {"tool_name": tool.name, "tool_input": call["input"]}, tool.name)
            self._report_hook("PreToolUse", out)
            if out.blocked:
                plan["result"] = self._finish(call, f"被 PreToolUse hook 拦截：{out.message}", True)
        return plan

    def _execute(self, plan: dict) -> None:
        call, tool = plan["call"], plan["tool"]
        started = time.time()
        if self.cancel.is_set():
            plan["result"] = self._finish(call, "用户中断了操作，工具未执行", True)
            return
        result = tool.execute(call["input"], self.ctx)
        content = truncate(result.content)
        if self.hooks.has("PostToolUse"):
            out = self.hooks.run("PostToolUse", {"tool_name": tool.name, "tool_input": call["input"],
                                                 "tool_result": content, "is_error": result.is_error}, tool.name)
            self._report_hook("PostToolUse", out)
            if out.blocked:
                content += f"\n\n[PostToolUse hook 反馈]\n{out.message}"
        plan["result"] = self._finish(call, content, result.is_error, time.time() - started)

    def _finish(self, call: dict, content: str, is_error: bool, seconds: float = 0.0) -> dict:
        self.emit("tool_result", id=call["id"], name=call["name"], content=content, is_error=is_error,
                  seconds=round(seconds, 3))
        return tool_result_block(call["id"], content, is_error)

    def _report_hook(self, event: str, out) -> None:
        if out.warnings or out.message:
            self.emit("hook", hook=event, blocked=out.blocked, message=out.message, warnings=out.warnings)

    # =================================================================== 上下文压缩

    def _maybe_compact(self) -> None:
        used = context_tokens(self.system_prompt(), self.messages, self.tools.schemas())
        if used <= self.context_window * self.compact_ratio or len(self.messages) <= 2:
            return
        # 刚压缩过，而之后上下文几乎没增长：说明大头在"最近一轮"本身（例如一个巨大的工具结果），
        # 再压缩也压不下去，只会每一步都白白调用一次模型做总结。等它再增长一截再说。
        if self._compacted_at is not None and used < self._compacted_at + self.context_window * 0.1:
            return
        self.compact(auto=True)

    def compact(self, focus: str = "", auto: bool = False) -> str:
        if len(self.messages) < 2:
            return "对话太短，不需要压缩"
        system, tools = self.system_prompt(), self.tools.schemas()
        before = context_tokens(system, self.messages, tools)
        limit = self.context_window * self.compact_ratio
        fixed = context_tokens(system, [], tools)
        if auto and fixed > limit * 0.9:
            # 系统提示词 + 工具定义本身就快占满窗口了，压缩对话无济于事
            self._compacted_at = before
            self.emit("hook", hook="compact", blocked=False, warnings=[],
                      message=f"系统提示词和工具定义约 {fixed} tokens，已接近上限 {int(limit)}，跳过压缩")
            return ""
        mid_turn = self.messages[-1]["role"] == "user"
        keep = 1
        split = find_compact_split(self.messages, 1)
        if mid_turn and split and context_tokens(system, self.messages[split:], tools) > limit * 0.9:
            # 太大的是"当前这一轮"本身（比如刚读了一个巨大的文件）。只总结更早的轮次压不下来，
            # 连当前这一轮一起总结，再让模型在摘要基础上继续。
            keep = 0
        self.messages, summary = compact_messages(self.provider, self.messages, keep_turns=keep, focus=focus,
                                                  mid_turn=mid_turn)
        after = context_tokens(self.system_prompt(), self.messages, self.tools.schemas())
        self._compacted_at = after
        self.emit("compact", auto=auto, before=before, after=after, summary=summary)
        return summary

    # =================================================================== 子 Agent

    def _run_subagent(self, description: str, prompt: str) -> str:
        sub = Agent(self.provider, self.tools.subset({"task", "todo_write"}), self.permissions, self.hooks,
                    self.bus, self.cwd, system_prompt=lambda: SUBAGENT_PROMPT, name=f"sub:{description}",
                    depth=self.depth + 1, max_steps=30, max_tokens=self.max_tokens,
                    context_window=self.context_window, cancel=self.cancel)
        self.emit("subagent_start", description=description, prompt=prompt)
        result = sub._run(prompt)  # 用 _run：不能 clear 共享的 cancel 事件
        self.usage.add(sub.usage)
        self.emit("subagent_end", description=description, result=result, usage=sub.usage.to_dict())
        return result or "(子 Agent 没有返回内容)"

    def reset(self) -> None:
        self.messages.clear()
        self.state["todos"] = []
        self.state["read_files"] = {}
        self.usage = Usage()
        self._compacted_at = None
