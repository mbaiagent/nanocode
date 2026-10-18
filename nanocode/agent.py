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

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from .context import BASE_PROMPT, compact_messages, context_tokens, find_compact_split
from .events import EventBus
from .hooks import HookRunner
from .llm.base import LLMError, Provider
from .messages import (Usage, assistant_message, text_block, text_of, tool_result_block, tool_uses)
from .permissions import EDIT_TOOLS, PermissionManager
from . import lessons, review, subcheck
from . import verify as verify_mod
from .tools.base import ToolContext, ToolRegistry, truncate


LLM_FAILED_PREFIX = "模型调用失败："   # 模型接口出错（配额、网络、服务故障）导致任务中止时，最终回答的开头；评测靠它区分"模型没做好"和"根本没调通"

class Cancelled(Exception):
    pass


SUBAGENT_PROMPT = BASE_PROMPT + """

# 你的身份
你是被主 Agent 派出去完成一项子任务的子 Agent。你看不到主对话，只能看到下面的任务说明。
完成后用一段简洁的文字汇报结论（这是主 Agent 唯一能看到的内容），包含关键的文件路径和事实，不要寒暄。
汇报里要分清两类内容：**已验证**的（你亲眼读到的代码、实际运行过的命令和结果）和**未验证**的（推测、印象、没跑过的东西），
不要把推测写成事实。任务如果限定了可以修改的范围或要求通过某条检查，系统会在你汇报后核对，不通过会让你重做。"""


class Agent:
    def __init__(self, provider: Provider, tools: ToolRegistry, permissions: PermissionManager,
                 hooks: HookRunner, bus: EventBus, cwd: str, system_prompt: Callable[[], str],
                 name: str = "main", depth: int = 0, max_steps: int = 60, max_tokens: int = 8192,
                 context_window: int = 128000, compact_ratio: float = 0.8, skills: dict | None = None,
                 cancel: threading.Event | None = None, verify: verify_mod.VerifyConfig | None = None):
        self.provider = provider
        self.tools = tools
        self.permissions = permissions
        self.hooks = hooks
        self.bus = bus
        # 与 ToolContext.__post_init__ / resolve_path / PermissionManager 保持同一套（realpath）基准。
        # 否则在 macOS（/var → /private/var）等带符号链接的工作目录下，os.path.relpath 会算成
        # ../../../../private/...，导致子任务越界误判、review 把同一文件列成两条。
        self.cwd = os.path.realpath(cwd)
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
        # 自动验证（只对主 Agent 生效，见 verify.py）
        self.verify = verify if depth == 0 else None
        self._dirty = False            # 本轮是否做过会改动东西的操作（检查之后清零）
        self._verify_failures = 0      # 连续失败次数
        self._verify_last: str | None = None   # 上一次失败的指纹
        self.write_scope: list[str] | None = None   # 子 Agent 允许修改的路径范围（None = 不限制），见 subcheck.py
        self._review_before: dict | None = None   # 终审模式：本轮开始时的文件快照（None = 本轮不做终审）
        self._review_originals: dict = {}          # 终审模式：写文件工具改动之前记下的原文
        self._verify_status: str | None = None     # 本轮最后一次自动验证的结果（pass / fail）
        self._lesson: dict | None = None           # 本轮第一次被验证打回时的证据，修好并通过后用来生成经验候选（见 lessons.py）
        self.verify_failed = False     # 上一轮是否因验证没通过而被迫停下（单次模式据此返回非零退出码）
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
        self._dirty, self._verify_failures, self._verify_last = False, 0, None
        self.verify_failed = False
        if self.depth == 0:   # 子 Agent 共用主 Agent 的终审记录，不能在这里清掉
            self._verify_status = None
            self._lesson = None
            self._review_before = subcheck.snapshot(self.cwd) if self.permissions.mode == "review" else None
            self._review_originals = {}

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
            try:
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
                    final = f"{LLM_FAILED_PREFIX}{e}"
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
                    # 自动验证 —— 做过改动的话，先过一遍检查命令；不通过就把输出交还给模型
                    if self.verify and self._dirty and not self.cancel.is_set():
                        verdict = self._verify()
                        if verdict is None:          # 未通过，反馈已追加，继续循环
                            continue
                        if verdict:                  # 超过重试上限 / 没有进展：停下来，把失败交给用户
                            final += verdict
                            self.verify_failed = True
                            self._finish_review()
                            self.emit("turn_end", final=final, usage=self.usage.to_dict(), verify_failed=True)
                            return final
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
            except Cancelled:
                final = "(已中断)"
                break
            except Exception as e:  # noqa: BLE001
                # 主循环的异常边界（#31）：走到这里说明是没预料到的内部错误。
                # 此刻 messages 很可能是「assistant 发了 tool_use、但下一条没有对应 tool_result」
                # 的非法状态 —— 模型接口对它是直接 400，这一段会话会永久废掉，只能 /clear。
                # 所以先补结果把对话修回合法，再走正常的收尾（turn_end / 终审汇总），
                # 让用户看到出错原因而不是一个卡在"等待模型"的界面。
                final = self._recover(step, e)
                break
        else:
            final = f"(达到最大步数 {self.max_steps}，已停止)"

        self._finish_review()
        self.emit("turn_end", final=final, usage=self.usage.to_dict())
        return final

    def _recover(self, step: int, exc: BaseException) -> str:
        """主循环异常后的兜底：补齐未执行的工具调用，报告原因。返回给调用方的最终回答。"""
        repaired = self._repair_unanswered()
        detail = f"{type(exc).__name__}: {exc}"
        if repaired:
            detail += (f"；已为 {len(repaired)} 个未执行的工具调用补上错误结果"
                       f"（{', '.join(repaired)}），对话可以继续")
        self.emit("error", message=f"第 {step} 步内部错误：{detail}", retryable=False)
        return f"(内部错误，第 {step} 步未能完成：{detail})"

    def _repair_unanswered(self) -> list[str]:
        """给最后一条 assistant 消息里还没拿到结果的 tool_use 补一条 is_error 的 tool_result。

        返回补齐的 id 列表；对话本来就合法时返回空列表、不改动任何东西。
        """
        if not self.messages or self.messages[-1].get("role") != "assistant":
            return []
        calls = tool_uses(self.messages[-1])
        if not calls:
            return []
        answered = {b.get("tool_use_id")
                    for m in self.messages if m.get("role") == "user"
                    for b in m.get("content", [])
                    if isinstance(b, dict) and b.get("type") == "tool_result"}
        missing = [c["id"] for c in calls if c["id"] not in answered]
        if not missing:
            return []
        self.messages.append({"role": "user",
                              "content": [tool_result_block(i, "[nanocode] 这一步因内部错误没能执行完，工具未执行", True)
                                          for i in missing]})
        return missing

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
        if self.write_scope is not None and tool.name in EDIT_TOOLS:
            rel = os.path.relpath(self.ctx.resolve(call["input"].get("path", "")), self.cwd)
            if not subcheck.in_scope(rel, self.write_scope):
                plan["result"] = self._finish(call, f"{rel} 不在这个子任务允许修改的范围内：{self.write_scope or '(只读，不许改任何文件)'}", True)
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
        if self._review_before is not None and tool.name in EDIT_TOOLS:
            review.remember(self._review_originals, self.cwd, self.ctx.resolve(str(call["input"].get("path", ""))))
        result = tool.execute(call["input"], self.ctx)
        if tool.changes_workspace and not result.is_error:
            self._dirty = True
            if self._lesson is not None and tool.name in EDIT_TOOLS:   # 打回之后改了哪些文件，是候选里的证据
                self._lesson["files"].add(os.path.relpath(self.ctx.resolve(str(call["input"].get("path", ""))), self.cwd))
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

    # =================================================================== 终审汇总

    def _finish_review(self) -> None:
        """终审模式：本轮结束时汇总改动（见 review.py）。没有改动就什么都不发。"""
        if self.depth != 0:   # 子 Agent 结束时不汇总，等主 Agent 整轮结束再一起汇总
            return
        before, self._review_before = self._review_before, None
        if before is None:
            return
        changed = subcheck.changed(before, subcheck.snapshot(self.cwd, before))
        files, hidden = review.build(self._review_originals, changed, self.cwd)
        self._review_originals = {}
        if files or hidden or changed is None:
            self.emit("review", files=files, hidden=hidden, verify=self._verify_status, uncounted=changed is None)

    # =================================================================== 自动验证

    def _verify(self) -> str | None:
        """跑一次检查。返回 None = 未通过且已把反馈追加进对话（应继续循环）；
        "" = 通过（可以结束）；非空字符串 = 失败且不再重试，内容是附加到最终回答后面的说明。"""
        cfg = self.verify
        attempt = self._verify_failures + 1
        self.emit("verify", status="running", attempt=attempt, command=cfg.command)
        result = verify_mod.run(cfg, self.cwd)
        self.emit("verify", status="pass" if result.passed else "fail", attempt=attempt, command=cfg.command,
                  exit_code=result.exit_code, output=verify_mod.tail(result.output), seconds=round(result.seconds, 2))
        self._verify_status = "pass" if result.passed else "fail"
        if not result.passed and self._lesson is None:
            self._lesson = {"command": cfg.command, "output": result.output, "files": set()}
        if result.passed:
            if self._lesson is not None:   # 被打回过、后来修好了：这是唯一会沉淀经验的情形
                lesson = lessons.build(self._lesson["command"], self._lesson["output"], sorted(self._lesson["files"]))
                if lesson:
                    self.emit("lesson", **lesson)
                self._lesson = None
            self._dirty, self._verify_failures, self._verify_last = False, 0, None
            return ""
        self._verify_failures += 1
        sig = verify_mod.signature(result)
        if sig == self._verify_last:
            reason = "同一个错误连续出现两次，模型没有取得进展"
        elif self._verify_failures > cfg.max_retries:
            reason = f"已重试 {cfg.max_retries} 次仍未通过"
        else:
            self._verify_last = sig
            self._append_user([text_block(f"<verify-feedback>\n{verify_mod.feedback(cfg, result, self._verify_failures)}\n</verify-feedback>")])
            return None
        return (f"\n\n(自动验证未通过，已停止：{reason}。检查命令：{cfg.command}，退出码 "
                f"{result.exit_code if result.exit_code is not None else '超时'}。最后一次输出：)\n{verify_mod.tail(result.output)}")

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

    def _run_subagent(self, description: str, prompt: str, check: str = "", allowed_paths=None) -> str:
        """派出子 Agent。返回它的结论 + 系统生成的验收报告（改了什么、有没有越界、检查过没过）。"""
        scope = subcheck.parse_scope(allowed_paths)   # 写错了抛 ScopeError，task 工具会把它变成给模型的错误
        sub = Agent(self.provider, self.tools.subset({"task", "todo_write"}), self.permissions, self.hooks,
                    self.bus, self.cwd, system_prompt=lambda: SUBAGENT_PROMPT, name=f"sub:{description}",
                    depth=self.depth + 1, max_steps=30, max_tokens=self.max_tokens,
                    context_window=self.context_window, cancel=self.cancel)
        sub.write_scope = scope
        sub._review_before, sub._review_originals = self._review_before, self._review_originals   # 子 Agent 的改动也进终审汇总
        self.emit("subagent_start", description=description, prompt=prompt, check=check, allowed_paths=scope)
        before = subcheck.snapshot(self.cwd)
        report = subcheck.Report(scope=scope, check=check.strip())
        result = sub._run(prompt)  # 用 _run：不能 clear 共享的 cancel 事件
        for attempt in range(1, subcheck.RETRIES + 2):
            report.changed = subcheck.changed(before, subcheck.snapshot(self.cwd, before))
            report.out_of_scope = [p for p in (report.changed or []) if scope is not None and not subcheck.in_scope(p, scope)]
            output = self._run_sub_check(report, attempt) if report.check else ""
            if not report.needs_redo or self.cancel.is_set():
                break
            if attempt > subcheck.RETRIES:
                report.gave_up = True
                break
            result = sub._run(subcheck.feedback(report, output))   # 子 Agent 保留上下文，接着修
        self._dirty = self._dirty or sub._dirty   # 子 Agent 改了东西，也算本轮有改动
        self.usage.add(sub.usage)
        self.emit("subagent_end", description=description, result=result, usage=sub.usage.to_dict(), report=report.to_dict())
        return f"{result or '(子 Agent 没有返回内容)'}\n\n{report.render()}"

    def _run_sub_check(self, report: "subcheck.Report", attempt: int) -> str:
        """跑子任务的检查命令。命令是模型传进来的，所以和 bash 一样要过权限，不能绕过确认。"""
        bash = self.tools.get("bash")
        if bash is None:
            report.check_status, report.check_note = "skipped", "没有 bash 工具"
            return ""
        decision = self.permissions.resolve(bash, {"command": report.check})
        if decision.behavior != "allow":
            report.check_status, report.check_note = "skipped", f"权限未放行：{decision.reason}"
            return ""
        report.check_attempts = attempt
        result = verify_mod.run(verify_mod.VerifyConfig(report.check, 0, 300), self.cwd)
        report.check_status = "pass" if result.passed else "fail"
        report.check_note = "" if result.passed else verify_mod.tail(result.output, 1500)
        return verify_mod.tail(result.output)

    def reset(self) -> None:
        self.messages.clear()
        self.state["todos"] = []
        self.state["read_files"] = {}
        self.usage = Usage()
        self._compacted_at = None
