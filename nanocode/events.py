"""事件总线：Agent 在循环中的每一个动作都会发出一个事件。

界面（CLI、Web）和日志（trace 文件）都只是事件的订阅者。
这样 Agent 核心不依赖任何界面代码 —— 想换一个界面，只需要写一个新的订阅者。

主要事件类型（按一轮对话中出现的顺序）：
    user           用户输入
    llm_request    即将调用模型（第几步、上下文大小）
    text_delta     模型流式输出的文字片段
    llm_response   模型这一步的完整输出（内容块、停止原因、token 用量）
    tool_call      模型要调用某个工具
    permission     权限决策（allow / deny 以及原因）
    permission_request  需要问用户：请求 id、工具、参数（Web 界面据此弹出确认框）
    permission_answer   用户回答了（或超时 / 断开按拒绝处理）
    command        执行了一条 / 斜杠命令（命令原文和输出）
    tool_result    工具执行结果
    todos          待办清单更新
    compact        上下文被压缩
    hook           hook 输出了警告/反馈
    verify         自动验证（status: running / pass / fail；见 verify.py）
    review         终审汇总：本轮改动的文件和 diff（review 模式，见 review.py）
    lesson         被自动验证打回又修好的回合，附证据（见 lessons.py；Session 据此生成 memory_pending）
    memory_pending 待用户确认的经验候选（/memory accept 写入，/memory reject 丢弃）
    subagent_start / subagent_end  子 Agent 启动 / 结束（结束时带验收报告 report，见 subcheck.py）
    turn_end       本轮结束（最终回答、累计用量）
    error          出错（retryable 为真时前端给重试按钮，prompt 是原文）
    queued         消息在忙时被收下排队（position：第几位）
    dequeue        排队的消息开始执行（remaining：还剩几条）
    idle           队列清空、Agent 空闲
    session_switched  切换了会话（前端重载页面）
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from typing import Callable

Listener = Callable[[dict], None]


class EventBus:
    def __init__(self, history: int = 2000):
        self._listeners: list[Listener] = []
        self._lock = threading.Lock()
        self.history: deque = deque(maxlen=history)
        self._seq = 0

    def subscribe(self, fn: Listener) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(fn)
        return lambda: self._listeners.remove(fn) if fn in self._listeners else None

    def emit(self, type: str, **data) -> dict:
        with self._lock:
            self._seq += 1
            event = {"seq": self._seq, "type": type, "ts": time.time(), **data}
            if type != "text_delta":
                self.history.append(event)
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn(event)
            except Exception:  # noqa: BLE001 —— 界面出错不能影响 Agent
                pass
        return event


class TraceWriter:
    """把事件写成 JSONL 文件，方便事后逐行分析一次任务的完整过程。"""

    def __init__(self, path: str):
        self.path = path
        self._f = open(path, "a", encoding="utf-8")

    def __call__(self, event: dict) -> None:
        if event["type"] == "text_delta":
            return
        if self._f.closed:
            return
        self._f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        self._f.flush()

    def close(self) -> None:
        self._f.close()


class AuditWriter:
    """审计日志：只记录"谁让 Agent 做了什么、结果如何"，不记录模型输出的全文。

    多用户部署时由平台策略的 "audit_log" 打开（见 policy.py）。和 trace 的区别：
      - trace 记录全部事件，用于调试，内容很大；
      - 审计只记录用户输入、工具调用、权限决策、命令、错误和每轮用量，长字段截断，适合长期保存、
        交给日志系统收集（例如沙箱里写到一个由平台收集的文件）。
    每行带上会话 id，日志系统可以按会话汇总。
    """

    TYPES = {"user", "tool_call", "permission", "permission_request", "permission_answer", "tool_result",
             "command", "error", "turn_end", "session_switched"}
    MAX_FIELD = 2000

    def __init__(self, path: str, session: Callable[[], str] = lambda: ""):
        self.path = path
        self.session = session
        self._lock = threading.Lock()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self._f = open(path, "a", encoding="utf-8")

    def __call__(self, event: dict) -> None:
        if event["type"] not in self.TYPES or self._f.closed:
            return
        record = {"session": self.session(), **{k: self._short(v) for k, v in event.items()}}
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            self._f.write(line + "\n")
            self._f.flush()

    def _short(self, value):
        if isinstance(value, str) and len(value) > self.MAX_FIELD:
            return value[:self.MAX_FIELD] + f"…（截断，原长 {len(value)}）"
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False, default=str)
            return value if len(text) <= self.MAX_FIELD else text[:self.MAX_FIELD] + f"…（截断，原长 {len(text)}）"
        return value

    def close(self) -> None:
        with self._lock:
            self._f.close()
