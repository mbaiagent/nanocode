"""Hooks：在 Agent 生命周期的固定时刻运行你自己的 shell 命令。

权限系统回答"能不能做"，hooks 则让你插入任意确定性的逻辑：
自动格式化、跑 lint、记审计日志、拦截危险命令、往对话里注入上下文……
模型是概率性的，hooks 是确定性的 —— "每次改完文件都要格式化" 这种规则，
写在提示词里模型可能忘，写成 hook 就一定会执行。

支持的事件：
  UserPromptSubmit  用户提交消息后、发给模型前。stdout 会作为额外上下文附加给模型。
  PreToolUse        工具执行前。退出码 2 = 拦截，stderr 作为原因反馈给模型。
  PostToolUse       工具执行后。退出码 2 = 把 stderr 作为反馈追加给模型（如 lint 报错）。
  Stop              Agent 准备结束本轮回答时。退出码 2 = 不许停，stderr 作为新指令让它继续。

hook 命令从 stdin 收到一个 JSON（事件名、工具名、参数、结果……）。
退出码 0 = 正常；2 = 阻断/反馈；其它 = 只打印警告，不影响流程。

配置（.nanocode/settings.json）：
    "hooks": {
      "PostToolUse": [{"matcher": "write_file|edit_file", "command": "ruff format ."}],
      "PreToolUse":  [{"matcher": "bash", "command": "python3 .nanocode/hooks/guard.py"}]
    }
matcher 是匹配工具名的正则，省略表示匹配全部。
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field

from .proc import utf8_env

EVENTS = ("UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop")


@dataclass
class HookOutcome:
    blocked: bool = False
    message: str = ""   # 阻断原因 / 反馈 / 附加上下文
    warnings: list[str] = field(default_factory=list)


class HookRunner:
    def __init__(self, config: dict | None, cwd: str, timeout: float = 60):
        self.config = config or {}
        self.cwd = cwd
        self.timeout = timeout

    def has(self, event: str) -> bool:
        return bool(self.config.get(event))

    def run(self, event: str, payload: dict, tool_name: str = "") -> HookOutcome:
        outcome = HookOutcome()
        messages = []
        for hook in self.config.get(event, []):
            matcher = hook.get("matcher", "")
            if matcher and tool_name and not re.fullmatch(matcher, tool_name):
                continue
            data = json.dumps({"event": event, "cwd": self.cwd, **payload}, ensure_ascii=False)
            try:
                # encoding 必须显式指定：text=True 在 Windows 上按本机 locale（cp936 等）解码，
                # 而 cmd / node / bash 常输出 UTF-8，解码失败发生在 subprocess 的 reader 线程里，
                # 异常只进 stderr，返回的 CompletedProcess.stdout 会变成 None，后面 .strip() 直接
                # AttributeError —— 一个 hook 打崩整轮对话（#32）。
                p = subprocess.run(hook["command"], shell=True, input=data, capture_output=True,
                                   text=True, encoding="utf-8", errors="replace",
                                   cwd=self.cwd, timeout=self.timeout, env=utf8_env())
            except (subprocess.TimeoutExpired, OSError) as e:
                outcome.warnings.append(f"hook 没能运行（{type(e).__name__}）：{hook['command']}")
                continue
            # 兜底：p.stdout / p.stderr 理论上不会是 None，但不让它变成 AttributeError 打穿 _prepare
            stdout = (p.stdout or "") if p.stdout is not None else ""
            stderr = (p.stderr or "") if p.stderr is not None else ""
            if p.returncode == 2:
                outcome.blocked = True
                messages.append(stderr.strip() or f"被 hook 拦截：{hook['command']}")
            elif p.returncode == 0:
                if event == "UserPromptSubmit" and stdout.strip():
                    messages.append(stdout.strip())
            else:
                outcome.warnings.append(f"hook 失败（退出码 {p.returncode}）：{hook['command']}\n{stderr.strip()}")
        outcome.message = "\n".join(messages)
        return outcome
