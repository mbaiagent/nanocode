# 把 nanocode 写进简历

这篇写给拿这个项目找工作的人：怎么描述、写哪些数字、面试会被追问什么。
原则只有一条：**每句话都能在仓库里找到证据，面试官顺着问三层也答得上来。**

## 先想清楚定位

nanocode 是一个**从零实现的编程 Agent**（对标 Claude Code 的核心机制），外加一个**按千人规模设计的多用户部署平台**。
它不是"调了个 API 的聊天机器人"，也不是"复刻了 Claude Code"——前者低估了，后者夸大了。

按你投的岗位挑重点：

| 岗位 | 重点写 | 少写 |
|---|---|---|
| AI 应用 / Agent 开发 | Agent 循环、工具设计、上下文工程、子 Agent、MCP、评测 | K8s 清单细节 |
| 后端 / 平台开发 | 控制面 + 网关拆分、存储和限流可替换、沙箱启动器、会话状态恢复 | 提示词细节 |
| 安全方向 | 权限模式、工作区信任、平台策略、密钥加密、SSRF、沙箱隔离 | 学习文档 |
| 算法 / 评测 | 评测框架、通过率、失败归因、基础设施错误和模型错误的区分 | 前端 |

## 可以写的数字（都能在仓库里核实）

| 数字 | 出处 |
|---|---|
| Agent 核心约 5500 行 Python，**只用标准库**，零第三方依赖 | `nanocode/` |
| 平台约 3100 行，同样只用标准库（PostgreSQL / Redis 驱动可选） | `server/nanocode_server/` |
| 核心 500+ 个单元测试，平台 120+ 个测试 | `tests/`、`server/tests/` |
| 两种协议（Anthropic、OpenAI 兼容）覆盖 9 家厂商预设，外加任意兼容服务 | `nanocode/llm/`、README「接入真实模型」 |
| 13 个编程任务 × 3 次重复，真实模型通过率 **37/39（95%）** | `evals/agent_tasks/results/` |
| 5 种权限模式；项目配置里能执行命令的项需要用户信任 | `nanocode/permissions.py`、`nanocode/trust.py` |
| 接入官方 MCP 服务器（filesystem、memory、fetch）实测可用 | `examples/extensions/` |
| 多用户模拟：10 个用户并发，隔离检查 51/51 通过；PostgreSQL + Redis + Docker 沙箱组合也跑通 | `server/README.md`、`python3 -m nanocode_server demo` |
| 14 章原理文档 + 14 篇平台设计文档 + ADR | `docs/`、`docs/platform/` |

> 测试数会随提交变化，写之前跑一次 `python3 -m unittest discover -s tests` 拿最新的数。

## 三种写法

### 一行版（放在"项目经历"列表里）

> **nanocode**：从零实现的编程 Agent（纯 Python 标准库），支持工具调用、权限控制、MCP、子 Agent，真实模型评测通过率 95%；并实现了按千人规模设计的多用户部署平台（控制面 / 模型网关 / 容器沙箱）。

### 标准版（5～6 条，最常用）

**nanocode — 编程 Agent 与多用户平台**　个人项目　Python（仅标准库）

- 实现 Agent 主循环：模型调用 → 工具执行 → 结果回填，支持流式输出、并行执行只读工具、中断，以及上下文超过 80% 时自动压缩；用统一消息格式适配 Anthropic 和 OpenAI 兼容两种协议，内置 9 家厂商预设，也能接任意兼容服务。
- 设计 11 个内置工具（文件读写、按字符串替换编辑、glob / grep 搜索、Shell、网页抓取、待办、子 Agent、技能加载、文档检索），以及 MCP stdio 客户端（JSON-RPC），可接入官方 filesystem / memory / fetch 等服务器，连接状态和失败原因可在 CLI 和 Web 面板查看。
- 构建安全机制：5 种权限模式 + allow/deny 规则 + 生命周期 hooks；项目配置里能执行命令、放宽权限、改变请求去向的字段，必须经用户信任（按配置指纹记录，配置变了重新询问）后才生效。
- 搭建评测框架：13 个真实编程任务 × 3 次重复，自动判定通过与否并记录步数、token、失败原因；模型接口出错（配额、网络、服务故障）的运行单独统计、不计入通过率；真实模型通过率 37/39（95%）。
- 实现多用户部署平台：控制面和模型网关可拆成独立进程；存储支持 SQLite / PostgreSQL，限流支持内存 / Redis，沙箱支持本机进程 / Docker / Kubernetes，依赖缺失时自动降级，单机即可跑通全部核心功能。
- 平台安全：用户的 API Key 加密存储且不进沙箱，沙箱只拿短期签名令牌访问网关；自定义上游地址做 SSRF 检查；每个会话下发只读策略，锁定模型去向、禁止危险配置；Docker 沙箱无外网。10 个用户并发模拟，隔离检查全部通过。

### 精简版（3 条，简历版面紧张时）

- 从零实现编程 Agent（纯标准库，5000+ 行，370+ 测试）：主循环、11 个内置工具、上下文压缩、子 Agent、MCP 客户端、权限与工作区信任。
- 搭建评测框架（13 任务 × 3 次），真实模型通过率 95%，并修正了评测把模型接口出错算成模型失败的问题。
- 实现千人规模架构的多用户平台：控制面 / 模型网关 / 沙箱启动器三层，存储、限流、沙箱均可替换，单机降级可运行；API Key 加密、令牌签名、沙箱断网。

### 英文版

- Built a coding agent from scratch in pure Python (stdlib only, 5k+ LOC, 370+ unit tests): agent loop with streaming and auto-compaction, 11 built-in tools, sub-agents, skills, an MCP stdio client, a permission system and workspace trust.
- Designed an evaluation harness (13 real coding tasks × 3 runs) reaching a 95% pass rate (37/39) with a production model; runs aborted by API errors are reported separately instead of counted as model failures.
- Implemented a multi-tenant platform designed for 1,000+ users: control plane, model gateway and pluggable sandbox launchers (process / Docker / Kubernetes), SQLite or PostgreSQL, in-memory or Redis rate limiting; encrypted API keys, signed short-lived gateway tokens, egress-free sandboxes.

## 面试会被追问什么

准备好这些问题，每个都要能说到代码层面：

1. **Agent 循环什么时候停？** 模型不再调用工具（`stop_reason` 是 `end_turn`）；另外有最大步数、用户中断兜底。→ `nanocode/agent.py`、docs/01
2. **编辑文件为什么用字符串替换，不用行号或整文件重写？** 行号在多次编辑后会漂移；整文件重写费 token 还容易丢内容；要求 `old_string` 唯一，匹配不上就报错让模型重读。→ docs/03
3. **上下文满了怎么办？** 超过窗口的 80% 时把旧消息压缩成摘要，切分点不能落在 tool_use 和 tool_result 中间。→ `nanocode/context.py`、docs/05
4. **子 Agent 解决什么问题？** 探索性任务产生大量中间结果，放进主上下文会污染；子 Agent 在新的上下文里做完只返回结论。→ docs/06
5. **MCP 是什么，你怎么实现的？** JSON-RPC over stdio；后台线程读输出、按 id 分发响应；远程工具包装成本地 Tool，对循环透明。→ `nanocode/mcp.py`、docs/07
6. **打开一个陌生仓库会不会被攻击？** 项目配置里的 hooks、mcpServers 等能执行命令，必须用户信任后才生效。→ `nanocode/trust.py`
7. **多用户时，用户的 API Key 怎么保护？** 加密落库，沙箱里只有网关签名令牌；Agent 能执行任意命令，所以沙箱里不能有任何真密钥。→ docs/12、`server/nanocode_server/security/`
8. **为什么控制面和网关要能拆开？** 模型调用是长连接、流量大，和控制面的扩缩容节奏不同；拆开后网关可以单独水平扩展。→ docs/platform/02
9. **评测通过率怎么算才可信？** 每题重复 3 次；模型接口出错（配额、网络、服务故障）的运行单独归类，不算模型失败。→ `evals/agent_tasks/run.py`
10. **哪里还不够好？** 见下一节，主动说比被问出来好。

## 诚实的边界

这些要说清楚，不要写成已经做到：

- 平台是**按千人规模设计**的，在**单机**上用多用户模拟、PostgreSQL + Redis + Docker 验证过，**没有在真实的千人生产环境运行过**。
- Kubernetes 启动器用假的 API 服务器测试了调用顺序和清单内容，没有在真实集群上压测。
- 评测是 13 个自建任务，不是 SWE-bench 这类公开基准；通过率只说明在这些任务上的表现。
- 这是学习型项目，代码追求可读而非性能极致。

面试官问"上线了吗、多少用户"时，直接说"没有上线，单机模拟了 N 个用户、验证了隔离和降级"，再讲你为千人规模做的设计取舍——这比含糊其辞得分高。

## 怎么证明给人看

- 简历上附仓库链接，README 第一屏就能 30 秒跑起来（不需要 API Key）。
- 准备一段 2 分钟的录屏：Web 面板里看循环时间线、上下文增长、权限审批、「扩展」面板里的 MCP 服务器。
- 平台部分准备 `server/docs/` 里的截图：登录、会话、模型设置。
