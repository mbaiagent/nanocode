#!/usr/bin/env python3
"""
最小 Agent（零第三方依赖，约 260 行）

Agent 的全部秘密只有一个循环：

    while 模型还想调工具:
        执行工具，把结果放回对话
    否则返回最终回答

运行方式：
    python3 minimal_agent.py --mock "看看当前目录"        # 无需网络，演示循环
    python3 minimal_agent.py "统计 README 有多少行"       # 需 API Key
    python3 minimal_agent.py --base-url https://api.deepseek.com/v1 \
        --model deepseek-chat "看看项目结构"

环境变量：NANOCODE_API_KEY / NANOCODE_BASE_URL / NANOCODE_MODEL
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

MAX_STEPS = 8

SYSTEM_PROMPT = """你是一个运行在用户终端里的编程助手。
需要事实时先调用工具，不要猜测；得到结果后再回答。回答保持简洁。"""


# ---------------------------------------------------------------------------
# 工具层：每个工具 = 给模型看的 JSON Schema + 真正执行的 Python 函数
# ---------------------------------------------------------------------------

def tool_list_dir(path: str = ".") -> str:
    p = Path(path).expanduser()
    if not p.is_dir():
        return f"错误：{path} 不是目录"
    items = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name))
    return "\n".join(("📁 " if i.is_dir() else "📄 ") + i.name for i in items) or "（空目录）"


def tool_read_file(path: str, offset: int = 1, limit: int = 200) -> str:
    p = Path(path).expanduser()
    if not p.is_file():
        return f"错误：找不到文件 {path}"
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(0, offset - 1)
    chunk = lines[start:start + limit]
    return "\n".join(f"{start + i + 1}\t{line}" for i, line in enumerate(chunk)) or "（空文件）"


def tool_bash(command: str) -> str:
    try:
        r = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=30
        )
        out = (r.stdout + r.stderr).strip()
        return out[:8000] or f"（无输出，退出码 {r.returncode}）"
    except subprocess.TimeoutExpired:
        return "错误：命令超时（30 秒）"


TOOLS: dict[str, dict] = {
    "list_dir": {
        "schema": {
            "type": "function",
            "function": {
                "name": "list_dir",
                "description": "列出目录下的文件和子目录",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "目录路径，默认当前目录"}
                    },
                },
            },
        },
        "func": tool_list_dir,
    },
    "read_file": {
        "schema": {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "读取文本文件，返回带行号的内容",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "文件路径"},
                        "offset": {"type": "integer", "description": "起始行，从 1 开始"},
                        "limit": {"type": "integer", "description": "最多读取行数"},
                    },
                    "required": ["path"],
                },
            },
        },
        "func": tool_read_file,
    },
    "bash": {
        "schema": {
            "type": "function",
            "function": {
                "name": "bash",
                "description": "在 shell 中执行命令，返回 stdout 和 stderr",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "要执行的命令"}
                    },
                    "required": ["command"],
                },
            },
        },
        "func": tool_bash,
    },
}

TOOL_SPECS = [t["schema"] for t in TOOLS.values()]


def dispatch(name: str, args: dict, auto_approve: bool) -> str:
    """执行一次工具调用。工具出错不崩溃，把错误字符串交还给模型即可。"""
    if name not in TOOLS:
        return f"错误：未知工具 {name}"
    if name == "bash" and not auto_approve:
        print(f"\n即将执行：{args.get('command')}")
        try:
            ok = input("允许？[y/N] ").strip().lower() in ("y", "yes")
        except EOFError:
            ok = False
        if not ok:
            return "用户拒绝执行该命令"
    try:
        return TOOLS[name]["func"](**args)
    except Exception as e:  # 工具异常统一转成文本
        return f"工具执行出错：{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# 模型层：只要兼容 OpenAI 的 /chat/completions 协议，换 base_url 就能换厂商
# ---------------------------------------------------------------------------

def call_openai(messages: list[dict], model: str, base_url: str, api_key: str) -> dict:
    payload = {
        "model": model,
        "messages": messages,
        "tools": TOOL_SPECS,
        "tool_choice": "auto",
        "temperature": 0,
    }
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    msg = data["choices"][0]["message"]
    return {
        "role": "assistant",
        "content": msg.get("content") or "",
        "tool_calls": msg.get("tool_calls") or [],
    }


def mock_model(messages: list[dict]) -> dict:
    """离线演示：第一次要求列目录，拿到结果后给最终回答。"""
    if messages[-1]["role"] == "tool":
        return {
            "role": "assistant",
            "content": "工具返回：\n" + messages[-1]["content"][:800]
            + "\n\n（--mock 演示；换成真实 API 后走的是同一个循环。）",
        }
    return {
        "role": "assistant",
        "content": "我先看看当前目录。",
        "tool_calls": [{
            "id": "call_mock_1",
            "type": "function",
            "function": {"name": "list_dir", "arguments": '{"path": "."}'},
        }],
    }


# ---------------------------------------------------------------------------
# Agent 循环：整个程序的核心
# ---------------------------------------------------------------------------

def run(task: str, model_fn, auto_approve: bool) -> str:
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": task},
    ]

    for step in range(1, MAX_STEPS + 1):
        msg = model_fn(messages)
        messages.append(msg)

        calls = msg.get("tool_calls") or []
        if not calls:                      # 模型不再调工具 → 结束
            return msg.get("content", "")

        if msg.get("content"):             # 只打印中间过程，最终回答由 main 输出
            print(f"\n\033[2m[step {step}] {msg['content']}\033[0m")

        for call in calls:                 # 执行工具，把结果塞回对话
            fn = call["function"]
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            print(f"\n\033[36m[tool] {fn['name']}({json.dumps(args, ensure_ascii=False)})\033[0m")
            result = dispatch(fn["name"], args, auto_approve)
            preview = result if len(result) <= 300 else result[:300] + "…"
            print(f"\033[2m[result] {preview}\033[0m")
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": result,
            })

    return f"达到最大步数 {MAX_STEPS}，已停止。"


def main() -> None:
    ap = argparse.ArgumentParser(description="最小的 Agent 实现")
    ap.add_argument("task", nargs="+", help="交给 Agent 的任务")
    ap.add_argument("--mock", action="store_true", help="离线演示，不调用真实模型")
    ap.add_argument("--model", default=os.getenv("NANOCODE_MODEL", "gpt-4o"))
    ap.add_argument(
        "--base-url",
        default=os.getenv("NANOCODE_BASE_URL", "https://api.openai.com/v1"),
    )
    ap.add_argument("--yes", action="store_true", help="自动批准 bash 命令")
    args = ap.parse_args()
    task = " ".join(args.task)

    if args.mock:
        model_fn = mock_model
    else:
        api_key = os.getenv("NANOCODE_API_KEY") or os.getenv("OPENAI_API_KEY")
        if not api_key:
            sys.exit("缺少 API Key：设置 NANOCODE_API_KEY，或加 --mock 离线体验。")
        model_fn = lambda msgs: call_openai(msgs, args.model, args.base_url, api_key)

    print(f"任务：{task}\n{'─' * 50}")
    print(run(task, model_fn, args.yes))


if __name__ == "__main__":
    main()
