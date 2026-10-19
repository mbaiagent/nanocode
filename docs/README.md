# 文档索引

nanocode 的文档分三类：**学习章节**（看懂一个编程 Agent 怎么做）、**平台与部署**（多用户平台怎么配、怎么部署、安全边界在哪）、**方案、调研与记录**。
项目总览和快速上手见仓库根目录的 [README.md](../README.md)（[English](../README.en.md)），版本变化见 [CHANGELOG.md](../CHANGELOG.md)。

## 学习章节（建议按顺序读）

| 章节 | 内容 |
|---|---|
| [00 学习路线](00-学习路线.md) | 怎么读这套文档和源码 |
| [01 Agent 循环](01-agent-loop.md) | 一个 Agent 的本质就是一个 while 循环 |
| [02 消息与模型适配](02-messages-and-providers.md) | 统一消息格式、接入任何一家模型、流式输出 |
| [03 工具](03-tools.md) | 工具怎么描述给模型，为什么编辑要用字符串替换 |
| [04 权限与 Hooks](04-permissions-and-hooks.md) | 权限模式、规则、hooks、项目信任 |
| [05 上下文工程](05-context-engineering.md) | 系统提示词、项目记忆、压缩、缓存 |
| [06 待办、子 Agent、Skills](06-subagents-todos-skills.md) | 规划、分工、按需加载 |
| [07 MCP](07-mcp.md) | 让任何人都能给 Agent 加工具 |
| [08 与 Claude Code 对照](08-claude-code-mapping.md) | 每个模块对应哪个功能，生产级还差什么 |
| [09 练习](09-练习.md) | 动手改代码 |
| [10 RAG](10-rag.md) | 什么时候用检索而不是 grep |
| [11 验证循环](11-verification-loop.md) | 自动验证、子 Agent 验收、终审模式、经验沉淀 |
| [12 分布式部署（设计）](12-distributed-deployment.md) | 给 1000+ 用户用还差什么：隔离、多租户、状态外置、网关、路线图 |
| [13 模型视角](13-model-perspective.md) | Agent 的行为从哪里来 |

## 平台与部署（多用户平台 nanocode-server）

| 文档 | 内容 |
|---|---|
| [server/README.md](../server/README.md) | 平台是什么、一分钟跑起来、组件可选、代码结构、测试、压测 |
| [deployment-topology.md](deployment-topology.md) | **先读这个**：本地单机 vs 平台；开发 / 单机平台 / 分布式三种形态怎么选；托管 Postgres / Redis；计划中的事项 |
| [configuration.md](configuration.md) | 全部 `NCS_*` 环境变量：默认值、谁读、含义 |
| [auth-and-admin.md](auth-and-admin.md) | GitHub 登录、邀请码注册、管理员和 `/admin`、用户自带 Key |
| [security-model.md](security-model.md) | 信任边界、实测状态、已知弱点和在修的安全 issue、出站代理的前置条件 |
| [distributed-deploy.md](distributed-deploy.md) | 分布式部署实操：平台机 + 沙箱机（WireGuard）+ 托管 Postgres / Valkey、故障模式、上线清单 |
| [deploy/compose/README.md](../deploy/compose/README.md) | 单机部署（docker compose + Caddy / nginx） |
| [deploy/sandbox-host/README.md](../deploy/sandbox-host/README.md) | 沙箱放到另一台机器（`NCS_LAUNCHER=remote`）的逐步操作 |
| [deploy/firewall/README.md](../deploy/firewall/README.md) | 宿主机防火墙：沙箱只能连网关端口（#123） |
| [cloud-agent-architecture.md](cloud-agent-architecture.md) | 调研：主流云端编程 Agent 怎么搭，nanocode 在哪一步 |

## 方案、调研与记录

| 文档 | 内容 |
|---|---|
| [multiuser-1000-plan.md](multiuser-1000-plan.md) | 千人级多用户登录方案和已定决策（GitHub OAuth / BYO Key / 单机 compose） |
| [web-multisession-plan.md](web-multisession-plan.md) | 单机 Web 面板多会话并行方案 |
| [issue-7-rate-limit.md](issue-7-rate-limit.md) | #7 `/api/chat` 限流方案 |
| [platform/README.md](platform/README.md) | Agent 平台设计文档集（企业级 / 通用 Agent，千到十亿用户的愿景与架构） |
| [maintenance/agent-playbook.md](maintenance/agent-playbook.md) | **维护交接手册**：权限边界、巡检 / issue 处理 / PR 审查合并 / 保活四个自动化、修复 PR 流程、部署与 CD（[English](maintenance/agent-playbook.en.md)） |
| [workflow.md](workflow.md) | 开发流程与测试流程 |
| [journey.md](journey.md) | 开发历程与踩过的坑 |
| [resume.md](resume.md) | 把这个项目写进简历 |
| [coordination.md](coordination.md) | 多个编程 Agent 协作的约定（历史记录，认领表可能过时） |
| [../verification/results/SUMMARY.md](../verification/results/SUMMARY.md) | 真实环境验证结果汇总 |
| [../evals/agent_tasks/README.md](../evals/agent_tasks/README.md) | Agent 任务评测 |

## 写文档的约定

- 本仓库公开：只用占位符（`nanocode.example.com`、`203.0.113.10` / `198.51.100.20`、`10.77.0.x`、`<db-host>`），不写真实 IP、域名、托管服务的主机名 / ID、邮箱、密钥。
- 还没合并的功能写成"计划中 / 进行中"并附 PR 或 issue 链接，不写成已完成。
- 加了新的环境变量就同步 [configuration.md](configuration.md)；单机的 `NANOCODE_*` 变量写进根 README（`tests/test_docs_consistency.py` 会检查）。
