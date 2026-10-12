"""bash 工具：让 Agent 能运行任何命令 —— 测试、构建、git、安装依赖……

这是编程 Agent 能力最强、风险也最大的工具，所以：
  - 默认每次执行都要用户确认（见 permissions.py）；
  - 有超时；输出过长会截断；
  - 支持中途取消（用户按 Ctrl-C / 点"停止"）。

工作目录会被记住：模型执行 `cd sub` 之后，后续命令在 sub 里运行。
实现方式是在命令末尾追加 `pwd` 并读回来 —— 一个简单但实用的小技巧。
"""

from __future__ import annotations

import os
import subprocess
import threading
import time

from .base import Tool, ToolError, truncate

_MARK = "__NANOCODE_PWD__"


class Bash(Tool):
    name = "bash"
    description = ("在 shell 中执行命令并返回输出（stdout 和 stderr 合并）。适合运行测试、构建、git、"
                   "安装依赖等。工作目录会在多次调用间保持。查找文件请优先用 glob / grep，读文件用 read_file。"
                   "不要运行需要交互输入或永不退出的命令（如 vim、长期运行的服务）。")
    input_schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的命令"},
            "timeout": {"type": "integer", "description": "超时秒数，默认 120，最大 600"},
        },
        "required": ["command"],
    }

    def permission_target(self, input):
        return input.get("command", "")

    def run(self, input, ctx):
        cwd = ctx.state.get("shell_cwd") or ctx.cwd
        timeout = min(max(1, input.get("timeout", 120)), 600)
        script = f"{input['command']}\n__nc=$?\necho; echo {_MARK}; pwd; exit $__nc"
        proc = subprocess.Popen(["bash", "-c", script], cwd=cwd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
                                errors="replace", start_new_session=True)
        # 必须边运行边读输出：如果等进程结束再读，输出超过管道缓冲区（约 64KB）时进程会卡死
        chunks: list[str] = []
        reader = threading.Thread(target=lambda: chunks.append(proc.stdout.read()), daemon=True)
        reader.start()
        deadline = time.time() + timeout
        try:
            while proc.poll() is None:
                if ctx.cancel.is_set():
                    _kill(proc)
                    raise ToolError("命令被用户中断")
                if time.time() > deadline:
                    _kill(proc)
                    raise ToolError(f"命令超时（{timeout} 秒）被终止")
                time.sleep(0.05)
            reader.join(timeout=5)
        finally:
            if proc.poll() is None:
                proc.wait(timeout=5)
            reader.join(timeout=1)
            if not reader.is_alive():
                proc.stdout.close()
        out = "".join(chunks)
        if _MARK in out:
            out, _, tail = out.rpartition(_MARK)
            new_cwd = tail.strip().splitlines()[0] if tail.strip() else ""
            if new_cwd and os.path.isdir(new_cwd):
                ctx.state["shell_cwd"] = new_cwd
            out = out.rstrip("\n")
        out = truncate(out.strip(), 20000) or "(无输出)"
        if proc.returncode != 0:
            return f"{out}\n\n[退出码 {proc.returncode}]"
        return out


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, 9)
    except (ProcessLookupError, PermissionError, AttributeError):
        proc.kill()
