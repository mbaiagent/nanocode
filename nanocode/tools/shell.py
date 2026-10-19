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
import re
import subprocess
import threading
import time

from .base import Tool, ToolError, truncate
from ..proc import WINDOWS, find_bash, kill_tree, popen_kwargs, utf8_env

_MARK = "__NANOCODE_PWD__"
_MSYS_PATH = re.compile(r"^/([A-Za-z])/(.*)$")


def _native_path(path: str, windows: bool = WINDOWS) -> str:
    """把 Git Bash 的 MSYS 路径（/c/Users/x）转成 Windows 路径（C:\\Users\\x）。

    工具靠输出里的 `pwd` 记住 `cd` 之后的位置，但 Windows 的 os.path.isdir 不认 MSYS
    路径 —— 结果 `cd sub` 下一条命令就"忘了"自己在哪。POSIX 上原样返回。
    """
    m = _MSYS_PATH.match(path)
    if windows and m:
        return m.group(1).upper() + ":\\" + m.group(2).replace("/", "\\")
    return path


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
        bash = find_bash()
        if bash is None:
            # Windows 上 bash 可能只有 System32 下的 WSL stub：跑在 WSL 视图里、pwd 语义不对、
            # 输出还是 UTF-16LE 的乱码。与其给出一堆假结果，不如直接说清楚（#45）。
            raise ToolError("没有找到可用的 bash。Windows 请安装 Git for Windows（自带 Git Bash）；"
                            "系统自带的 C:\\Windows\\System32\\bash.exe 是 WSL 启动器，它的工作目录和输出编码"
                            "与 Windows 不一致，这里不使用。")
        script = f"{input['command']}\n__nc=$?\necho; echo {_MARK}; pwd; exit $__nc"
        proc = subprocess.Popen([bash, "-c", script], cwd=cwd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                encoding="utf-8", errors="replace", env=utf8_env(), **popen_kwargs())
        # 必须边运行边读输出：如果等进程结束再读，输出超过管道缓冲区（约 64KB）时进程会卡死
        chunks: list[str] = []
        reader = threading.Thread(target=lambda: chunks.append(proc.stdout.read()), daemon=True)
        reader.start()
        deadline = time.time() + timeout
        try:
            while proc.poll() is None:
                if ctx.cancel.is_set():
                    kill_tree(proc)
                    raise ToolError("命令被用户中断")
                if time.time() > deadline:
                    kill_tree(proc)
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
            new_cwd = _native_path(new_cwd)
            if new_cwd and os.path.isdir(new_cwd):
                ctx.state["shell_cwd"] = new_cwd
            out = out.rstrip("\n")
        out = truncate(out.strip(), 20000) or "(无输出)"
        if proc.returncode != 0:
            return f"{out}\n\n[退出码 {proc.returncode}]"
        return out

