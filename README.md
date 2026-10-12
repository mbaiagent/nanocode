# nanocode

一个用来**看懂 Agent、看懂 Claude Code 核心架构**的迷你编程 Agent。

- **纯 Python 标准库**，零依赖，核心代码约 2800 行（含大量中文注释），每个文件开头都有讲解
- **可接入任何一家模型**：Claude、GPT、DeepSeek、通义千问、Kimi、智谱 GLM、硅基流动、OpenRouter、本地 Ollama，或任何 OpenAI / Anthropic 兼容的中转服务
- **实现了 Claude Code 的核心机制**：Agent 循环、流式输出、文件/命令/搜索工具、权限模式与规则、Hooks、项目记忆、上下文压缩、待办清单、子 Agent、Skills、自定义斜杠命令、MCP、会话保存
- **自带 Web 调试面板**：实时看到每一圈循环、每次发给模型的完整上下文、系统提示词、工具定义
- **自带 Mock 模型**：不需要 API Key，就能把整套流程跑起来、看清楚
- **内置小而完整的 RAG**：切块、中文 BM25、可选向量检索、RRF 混合、增量索引，外加一个评测脚本对比 grep 与检索
- **51 个单元测试**，覆盖循环的每个分支、两种协议的流式解析和检索管线

> nanocode 是依据 Claude Code 的公开文档和可观察行为，从零写的教学实现，不包含也不来自 Claude Code 的源代码。它追求的是"把原理讲清楚"，不是"功能一样多"。

---

## 30 秒跑起来（不需要 API Key）

```bash
cd nanocode
python3 -m nanocode --web          # 打开 http://127.0.0.1:8765
# 或者终端版
python3 -m nanocode
```

默认使用 Mock 模型。随便输入一句话，它会按剧本演示一遍完整的 Agent 循环：列待办 → 看目录 → 写文件（请求权限）→ 运行（请求权限）→ 汇报。
在 Web 界面右侧切到「循环时间线」和「上下文」，看循环每转一圈 messages 是怎么增长的。

想安装成命令：`pip install -e .`，之后直接用 `nanocode`。要求 Python ≥ 3.10。

## 接入真实模型

| 厂商 | 命令 | API Key 环境变量 |
|---|---|---|
| Anthropic Claude | `nanocode --provider anthropic --model claude-sonnet-5-5` | `ANTHROPIC_API_KEY` |
| OpenAI | `nanocode --provider openai --model <模型名>` | `OPENAI_API_KEY` |
| DeepSeek | `nanocode --provider deepseek --model deepseek-chat` | `DEEPSEEK_API_KEY` |
| 通义千问 | `nanocode --provider qwen --model qwen-plus` | `DASHSCOPE_API_KEY` |
| Kimi | `nanocode --provider kimi --model <模型名>` | `MOONSHOT_API_KEY` |
| 智谱 GLM | `nanocode --provider glm --model <模型名>` | `ZHIPUAI_API_KEY` |
| 硅基流动 | `nanocode --provider siliconflow --model <模型名>` | `SILICONFLOW_API_KEY` |
| OpenRouter | `nanocode --provider openrouter --model <模型名>` | `OPENROUTER_API_KEY` |
| 本地 Ollama | `nanocode --provider ollama --model qwen2.5-coder` | 不需要 |
| 任意兼容服务 | `nanocode --provider custom --protocol openai --base-url https://xxx/v1 --model <模型名>` | `NANOCODE_API_KEY` |

模型名经常更新，请以各家官方文档为准。也可以把配置写进环境变量（`NANOCODE_PROVIDER`、`NANOCODE_MODEL`、`NANOCODE_BASE_URL`、`NANOCODE_API_KEY`）或 `.nanocode/settings.json`。

**选模型的提醒**：Agent 非常依赖模型的工具调用能力。小模型（尤其是 7B 以下的本地模型）经常不会调用工具或参数格式出错，这时你会在调试面板里看到大量报错 —— 这本身也是很好的学习材料。

## 常用操作

```bash
nanocode -p "解释一下 main.py"                # 单次模式，跑完就退出（适合脚本、CI）
nanocode --mode plan                          # 只读规划模式：只能看，不能改
nanocode --mode accept_edits                  # 自动允许改文件，命令仍需确认
nanocode --trace                              # 把所有事件记录到 .nanocode/traces/*.jsonl
nanocode --resume                             # 恢复上一次会话
```

交互模式里：`/help` 查看命令，`/compact` 压缩上下文，`/context` 看上下文占用，`/mode plan` 切换模式，`/init` 生成项目记忆，`# 一句话` 追加记忆，`Ctrl-C` 中断。

## 目录结构

```
nanocode/
├── messages.py        统一消息格式 —— Agent 的"内存"长什么样
├── llm/               模型适配层
│   ├── base.py          Provider 接口、HTTP 重试、SSE 解析
│   ├── openai_compat.py OpenAI 兼容协议（覆盖国内外绝大多数厂商）
│   ├── anthropic.py     Claude 原生协议（含 Prompt Caching）
│   ├── mock.py          离线 Mock 模型
│   └── __init__.py      厂商预设 + 工厂函数
├── tools/             工具
│   ├── base.py          Tool 抽象、参数校验、输出截断
│   ├── files.py         read_file / write_file / edit_file
│   ├── shell.py         bash
│   ├── search.py        glob / grep
│   ├── rag_tool.py      search_docs（按含义检索文档）
│   └── planning.py      todo_write / task（子 Agent）/ skill
├── agent.py           ★ 核心循环
├── rag.py             RAG：切块、BM25、向量、混合检索、增量索引
├── permissions.py     权限模式与规则
├── hooks.py           生命周期钩子
├── context.py         系统提示词、项目记忆、技能/命令发现、上下文压缩
├── mcp.py             MCP 客户端（stdio）
├── events.py          事件总线 + trace
├── session.py         组装各部件、斜杠命令、会话保存
├── cli.py             终端界面
└── web/               Web 界面（调试面板 + 架构学习页）
docs/                  分章节的架构讲解（建议按顺序读）
examples/              MCP 服务器示例、hooks/skills/commands 配置示例、RAG 评测（rag_eval/）
tests/                 单元测试
```

## 怎么学

建议顺序：先跑起来 → 读 [docs/00-学习路线.md](docs/00-学习路线.md) → 按章节读文档，同时打开对应源码 → 做 [docs/09-练习.md](docs/09-练习.md)。

| 章节 | 内容 |
|---|---|
| [01 Agent 循环](docs/01-agent-loop.md) | 一个 Agent 的本质就是一个 while 循环 |
| [02 消息与模型适配](docs/02-messages-and-providers.md) | 统一消息格式，如何接入任何一家模型，流式输出 |
| [03 工具](docs/03-tools.md) | 工具怎么描述给模型，为什么编辑要用字符串替换 |
| [04 权限与 Hooks](docs/04-permissions-and-hooks.md) | 给 Agent 系上安全带 |
| [05 上下文工程](docs/05-context-engineering.md) | 系统提示词、项目记忆、压缩、缓存 |
| [06 待办、子 Agent、Skills](docs/06-subagents-todos-skills.md) | 让 Agent 会规划、会分工、会按需学习 |
| [07 MCP](docs/07-mcp.md) | 让任何人都能给 Agent 加工具 |
| [08 与 Claude Code 对照](docs/08-claude-code-mapping.md) | 每个模块对应 Claude Code 的哪个功能，生产级还差什么 |
| [09 练习](docs/09-练习.md) | 动手改代码，检验是否真的懂了 |
| [10 RAG](docs/10-rag.md) | 什么时候用检索而不是 grep，管线怎么搭，怎么评测 |

## 运行测试

```bash
python3 -m unittest discover -s tests
```

## 安全提示

nanocode 会在你的电脑上执行模型生成的命令。默认模式下每条命令都需要你确认；`bypass` 模式请只在容器或虚拟机里使用。

MIT License
