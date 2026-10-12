"""一个最小的 MCP 服务器（stdio 传输），只用标准库，用来理解 MCP 协议的服务端。

提供两个工具：
    word_count(text)   统计字数（只读）
    save_note(text)    把一条笔记追加到 notes.txt（有副作用）

在 .nanocode/settings.json 里这样接入：
    {"mcpServers": {"demo": {"command": "python3", "args": ["examples/mcp_server_demo.py"]}}}

接入后模型会看到两个新工具：mcp__demo__word_count 和 mcp__demo__save_note。
"""

import json
import sys

TOOLS = [
    {
        "name": "word_count",
        "description": "统计一段文本的字符数、行数和英文单词数",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "save_note",
        "description": "把一条笔记追加保存到当前目录的 notes.txt",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    },
]


def call_tool(name, args):
    if name == "word_count":
        text = args.get("text", "")
        return f"字符数 {len(text)}，行数 {text.count(chr(10)) + 1}，英文单词数 {len(text.split())}", False
    if name == "save_note":
        with open("notes.txt", "a", encoding="utf-8") as f:
            f.write(args.get("text", "") + "\n")
        return "已保存到 notes.txt", False
    return f"未知工具 {name}", True


def handle(msg):
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:  # 通知（例如 notifications/initialized），不需要回复
        return None
    if method == "initialize":
        result = {"protocolVersion": msg["params"].get("protocolVersion", "2025-06-18"),
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "demo", "version": "0.1.0"}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        text, is_error = call_tool(msg["params"]["name"], msg["params"].get("arguments", {}))
        result = {"content": [{"type": "text", "text": text}], "isError": is_error}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"不支持的方法 {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    reply = handle(json.loads(line))
    if reply is not None:
        sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
        sys.stdout.flush()
