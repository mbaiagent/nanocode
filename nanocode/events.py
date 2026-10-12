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
    tool_result    工具执行结果
    todos          待办清单更新
    compact        上下文被压缩
    hook           hook 输出了警告/反馈
    turn_end       本轮结束（最终回答、累计用量）
    error          出错
"""

from __future__ import annotations

import json
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
