# 07 · MCP：让任何人都能给 Agent 加工具

> 对应源码：`nanocode/mcp.py`、`examples/mcp_server_demo.py`

## 问题

你想让 Agent 能查 Jira、读数据库、发 Slack 消息。按第 03 章的方法，要给每个 Agent 各写一遍工具。N 个 Agent × M 个服务 = N×M 份代码。

## MCP 的思路

MCP（Model Context Protocol）定义了一个标准协议：

```
Agent（MCP 客户端） ◀──── 标准协议 ────▶ 服务（MCP 服务器）
Claude Code                              GitHub MCP 服务器
nanocode                                 数据库 MCP 服务器
其他 Agent                                你自己写的服务器
```

服务方写一次 MCP 服务器，所有支持 MCP 的 Agent 都能用。N×M 变成 N+M。

## 协议本质

MCP 基于 JSON-RPC 2.0。最简单的传输方式是 **stdio**：Agent 把服务器作为子进程启动，通过它的 stdin/stdout 一行一条 JSON 通信。

一次完整的交互：

```
→ {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"nanocode"}}}
← {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18","capabilities":{"tools":{}},"serverInfo":{"name":"demo"}}}
→ {"jsonrpc":"2.0","method":"notifications/initialized"}
→ {"jsonrpc":"2.0","id":2,"method":"tools/list"}
← {"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"word_count","description":"统计……","inputSchema":{...}}]}}
→ {"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"word_count","arguments":{"text":"hello world"}}}
← {"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text","text":"字符数 11……"}],"isError":false}}
```

三个概念：
- **请求**有 `id`，必须回复，回复带同样的 `id`
- **通知**没有 `id`，不需要回复
- **握手**：先 `initialize`，再发 `notifications/initialized`，之后才能正常使用

## 客户端实现

`MCPClient` 的结构：

- 启动子进程，开一个**后台线程**持续读 stdout，按 `id` 把响应放进对应请求的队列；
- `request()` 发送请求，在自己的队列上等待（带超时）；
- 不是合法 JSON 的行直接忽略（有些服务器会往 stdout 打日志）；
- stderr 也开一个线程读，只保留最后 20 行：服务器起不来（缺依赖、参数错）时，这几行就是原因。

`connect_servers` 逐个连接，一个失败不影响其他，并给每个服务器记一条 `ServerStatus`（成功与否、错误原因、服务器自报的名字版本、工具列表）。
`/mcp` 命令和 Web 界面的「扩展」面板（`GET /api/extensions`）都读这份记录——装了什么、哪个没连上、为什么，不用翻启动日志。
每个服务器可以单独设 `"timeout"`（秒，默认 60）：`npx -y` / `uvx` 第一次启动要下载包，可能比较慢。

为什么需要后台线程？因为服务器可能在响应之间插入通知，也可能乱序返回。按 id 分发是 JSON-RPC 客户端的标准做法。

## 远程工具 → 本地工具

`MCPTool` 把每个远程工具包装成普通的 `Tool`：

| MCP 字段 | Tool 字段 |
|---|---|
| `name` | `mcp__<服务器名>__<工具名>`（和 Claude Code 的命名方式一致） |
| `description` | `description` |
| `inputSchema` | `input_schema` |
| `annotations.readOnlyHint` | `is_read_only` |

包装之后，对 Agent 循环来说 MCP 工具和内置工具**没有任何区别**：同样出现在 tools 列表里、同样经过权限检查和 hooks、同样可能并行。这就是好的抽象。

名字加前缀有两个作用：避免和内置工具重名；权限规则可以按服务器配置，例如 `"allow": ["mcp__demo__word_count"]`。

## 动手

1. 在项目里创建 `.nanocode/settings.json`：
   ```json
   {"mcpServers": {"demo": {"command": "python3", "args": ["examples/mcp_server_demo.py"]}}}
   ```
2. 启动 nanocode，会看到"MCP 服务器 demo 已连接，提供 2 个工具"。
3. 在 Web 面板「工具定义」里能看到 `mcp__demo__word_count` 和 `mcp__demo__save_note`。
4. 对真实模型说"统计一下 README.md 有多少字"，观察它会不会用 MCP 工具。

5. 想看真实生态的服务器，进 `examples/extensions/` 启动：那里配好了官方的 filesystem、memory（Node.js）和 fetch（uv）服务器，外加几个技能，用 `/mcp` 和 `/skills` 查看。

`examples/mcp_server_demo.py` 只有 80 行，是理解服务器端的最好材料。读完它，试着写一个你自己的服务器，比如查询本地 SQLite 数据库。

## nanocode 没实现的部分

完整的 MCP 还包括：
- **HTTP 传输**（远程服务器、OAuth 认证）
- **Resources**（服务器提供可读取的数据，如文件、数据库表）
- **Prompts**（服务器提供提示词模板）
- 工具列表变更通知、进度通知、取消请求等

原理都一样：JSON-RPC 请求/响应 + 能力协商。
