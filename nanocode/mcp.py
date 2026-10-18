"""MCP（Model Context Protocol）客户端 —— stdio 传输。

MCP 是一个开放协议，让"工具提供方"和"Agent"解耦：任何人写一个 MCP 服务器，
所有支持 MCP 的 Agent（Claude Code、nanocode……）都能直接使用它的工具。

协议本质是 JSON-RPC 2.0，stdio 传输下每行一条 JSON 消息：

    客户端 → {"jsonrpc":"2.0","id":1,"method":"initialize","params":{...}}
    服务器 → {"jsonrpc":"2.0","id":1,"result":{"capabilities":{...},"serverInfo":{...}}}
    客户端 → {"jsonrpc":"2.0","method":"notifications/initialized"}       （通知，没有 id）
    客户端 → {"jsonrpc":"2.0","id":2,"method":"tools/list"}
    服务器 → {"jsonrpc":"2.0","id":2,"result":{"tools":[{name, description, inputSchema}]}}
    客户端 → {"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":..., "arguments":{...}}}
    服务器 → {"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text","text":"..."}],"isError":false}}

拿到 tools/list 的结果后，每个远程工具被包装成一个普通的 Tool，
名字是 mcp__<服务器名>__<工具名>（和 Claude Code 的命名方式一样），
对 Agent 循环来说它和内置工具没有任何区别。

配置（.nanocode/settings.json）：
    "mcpServers": {"demo": {"command": "python3", "args": ["examples/mcp_server_demo.py"], "env": {}}}
"""

from __future__ import annotations

import collections
import json
import os
import queue
import subprocess
import threading
from dataclasses import dataclass, field

from .tools.base import Tool, ToolError

PROTOCOL_VERSION = "2025-06-18"


class MCPError(Exception):
    pass


class MCPClient:
    def __init__(self, name: str, command: str, args: list[str] | None = None, env: dict | None = None,
                 cwd: str | None = None, timeout: float = 60):
        self.name = name
        self.timeout = timeout
        self._id = 0
        self._lock = threading.Lock()
        self._responses: dict[int, queue.Queue] = {}
        self.proc = subprocess.Popen([command, *(args or [])], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, cwd=cwd, bufsize=1,
                                     env={**os.environ, **(env or {})})
        # 服务器的错误输出只保留最后几行：启动失败时拿来告诉用户原因（缺依赖、参数不对……）
        self.stderr_tail: collections.deque = collections.deque(maxlen=20)
        threading.Thread(target=self._read_loop, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        self.server_info: dict = {}

    def _read_loop(self) -> None:
        """后台线程持续读取服务器输出，按 id 把响应分发给等待中的请求；没有 id 的是通知，忽略。"""
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # 服务器往 stdout 打了日志，不是协议消息
            q = self._responses.get(msg.get("id")) if "id" in msg else None
            if q:
                q.put(msg)
        for q in list(self._responses.values()):
            q.put({"error": {"message": "MCP 服务器进程已退出"}})

    def _send(self, msg: dict) -> None:
        if self.proc.poll() is not None:
            raise MCPError(f"MCP 服务器 {self.name} 已退出")
        self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def _read_stderr(self) -> None:
        for line in self.proc.stderr:
            if line.strip():
                self.stderr_tail.append(line.rstrip())

    def request(self, method: str, params: dict | None = None) -> dict:
        with self._lock:
            self._id += 1
            rid = self._id
            q: queue.Queue = queue.Queue()
            self._responses[rid] = q
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        try:
            msg = q.get(timeout=self.timeout)
        except queue.Empty:
            raise MCPError(f"{self.name}.{method} 超时") from None
        finally:
            self._responses.pop(rid, None)
        if "error" in msg:
            raise MCPError(f"{self.name}.{method} 出错：{msg['error'].get('message')}")
        return msg.get("result", {})

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def initialize(self) -> None:
        result = self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "nanocode", "version": "0.1.0"},
        })
        self.server_info = result.get("serverInfo", {})
        self.notify("notifications/initialized")

    def list_tools(self) -> list[dict]:
        return self.request("tools/list").get("tools", [])

    def call_tool(self, name: str, arguments: dict) -> tuple[str, bool]:
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        parts = []
        for c in result.get("content", []):
            if c.get("type") == "text":
                parts.append(c["text"])
            else:
                parts.append(f"[{c.get('type')} 内容，nanocode 只显示文本]")
        return "\n".join(parts) or "(无输出)", bool(result.get("isError"))

    def close(self) -> None:
        try:
            self.proc.stdin.close()  # 关闭 stdin 是通知 stdio 服务器退出的标准方式
            self.proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            self.proc.kill()
        finally:
            for stream in (self.proc.stdout, self.proc.stderr):
                if stream:
                    stream.close()


class MCPTool(Tool):
    """把一个远程 MCP 工具包装成本地 Tool。"""

    def __init__(self, client: MCPClient, spec: dict):
        self.client = client
        self.remote_name = spec["name"]
        self.name = f"mcp__{client.name}__{spec['name']}"
        self.description = spec.get("description", "") or f"MCP 工具 {spec['name']}"
        self.input_schema = spec.get("inputSchema") or {"type": "object", "properties": {}}
        # MCP 规范里有 annotations.readOnlyHint，服务器声明只读就当只读工具处理
        self.is_read_only = bool((spec.get("annotations") or {}).get("readOnlyHint"))
        # 但 readOnlyHint 是服务器**自报**的，不等于真的只读（#41 讲的就是它绕过 plan 模式）。
        # 并行执行会打乱多个工具的相对顺序、让共享状态的读写交错，所以 MCP 工具一律串行 ——
        # 代价只是同一条回复里的多个 MCP 调用排队执行，可以忽略。
        self._parallel_safe = False

    @property
    def parallel_safe(self) -> bool:
        return self._parallel_safe

    def permission_target(self, input):
        return json.dumps(input, ensure_ascii=False)

    def run(self, input, ctx):
        try:
            text, is_error = self.client.call_tool(self.remote_name, input)
        except MCPError as e:
            raise ToolError(str(e)) from None
        if is_error:
            raise ToolError(text)
        return text


@dataclass
class ServerStatus:
    """一个 MCP 服务器的连接结果，用于 /mcp 命令和 Web 界面的"扩展"面板。"""
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    ok: bool = False
    error: str = ""
    server: str = ""                 # 服务器自报的名字和版本
    tools: list[str] = field(default_factory=list)


def connect_servers(config: dict, cwd: str, log=print) -> tuple[list[MCPClient], list[Tool], list[ServerStatus]]:
    """连接配置里的所有 MCP 服务器。一个失败不影响别的；每个服务器的结果都记下来，事后能查。"""
    clients, tools, statuses = [], [], []
    for name, spec in (config or {}).items():
        st = ServerStatus(name, str(spec.get("command", "")), [str(a) for a in spec.get("args") or []])
        statuses.append(st)
        client = None
        try:
            client = MCPClient(name, spec["command"], spec.get("args"), spec.get("env"), cwd=cwd,
                               timeout=float(spec.get("timeout", 60)))
            client.initialize()
            specs = client.list_tools()
        except Exception as e:  # noqa: BLE001
            detail = f"\n{chr(10).join(list(client.stderr_tail)[-5:])}" if client and client.stderr_tail else ""
            st.error = f"{type(e).__name__}: {e}{detail}"
            if client:
                client.close()
            log(f"MCP 服务器 {name} 连接失败：{st.error}")
            continue
        clients.append(client)
        tools.extend(MCPTool(client, s) for s in specs)
        info = client.server_info
        st.ok, st.tools = True, [s["name"] for s in specs]
        st.server = " ".join(x for x in (info.get("name", ""), info.get("version", "")) if x)
        log(f"MCP 服务器 {name} 已连接，提供 {len(specs)} 个工具")
    return clients, tools, statuses
