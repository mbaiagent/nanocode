# 更新日志

本项目还没有打版本号，按时间记录合并到 `main` 的主要变化。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。
细节以对应 PR / issue 为准；**「进行中 / 计划中」的条目还没合并，不代表已经可用。**

## [未发布] 2026-10

### 多用户平台（nanocode-server）

- **新增 多用户 P0**（[#117](https://github.com/mbaiagent/nanocode/pull/117)）：`NCS_REPLICAS > 1` 时强制 PostgreSQL + Redis，否则拒绝启动；Redis 默认 RESP2，兼容老版本；新增单机 `deploy/compose`（控制面 + 网关 + PostgreSQL 16 + Redis 7 + Docker 沙箱）。
- **新增 多用户 P1**（[#118](https://github.com/mbaiagent/nanocode/pull/118)）：`NCS_AUTH=github`，用 GitHub OAuth 登录（state + PKCE，按 GitHub 数字 id 绑定）；邀请码注册；服务端登录会话（库里只存哈希，可立即吊销）；`/admin` 管理页（邀请码、账号停用 / 启用）；引导管理员 `NCS_ADMIN_GITHUB_LOGINS`；可选 Caddy 自动 HTTPS。模型 Key 由用户自带（BYO），加密存库、只在网关解密。见 [docs/auth-and-admin.md](docs/auth-and-admin.md)。
- **安全 沙箱宿主机隔离**（[#124](https://github.com/mbaiagent/nanocode/pull/124)，Fixes [#123](https://github.com/mbaiagent/nanocode/issues/123)）：沙箱网桥名 / 网段固定（`NCS_DOCKER_BRIDGE`、`NCS_DOCKER_SUBNET`），新增 `deploy/firewall/` 的 firewalld / nftables 脚本和 `check-sandbox-isolation.sh`，沙箱只能连宿主机的网关端口。
- **新增 远程沙箱**（[#126](https://github.com/mbaiagent/nanocode/pull/126)）：`NCS_LAUNCHER=remote`，沙箱跑在另一台机器上，经 WireGuard 等内网隧道连接；新增 `python -m nanocode_server sandbox-agent`（只听内网地址、共享密钥认证、不暴露 Docker API）和网关中继（全局 / 每沙箱连接上限、空闲超时，`NCS_AGENT_RELAY_*`）；新增 `deploy/sandbox-host/`。
- **修复 老版本 Docker 网络没有 Gateway 字段**（[#130](https://github.com/mbaiagent/nanocode/pull/130)）：建网络时显式写网关，已有网络按网段第一个地址兜底；Docker 26 上沙箱代理不再反复重启。
- **安全 平台页面加 CSP / 防点击劫持**（Fixes #113）：控制面页面默认带 CSP、`frame-ancestors 'none'`、`X-Frame-Options: DENY`；`/s/<id>/` 转发的沙箱面板由平台统一加 CSP（`frame-ancestors 'self'`）和 `X-Frame-Options: SAMEORIGIN`，不再依赖沙箱自己发的头。
- **调参 小机器资源**（#119）：`NCS_SANDBOX_TMPFS`、`NCS_SANDBOX_PIDS`、PG / Redis / 容器内存上限；大小同时认 `384Mi` 和 `384m` 写法。

### 单机 nanocode / Web 面板

- **安全 读取敏感文件默认需要确认**（[#147](https://github.com/mbaiagent/nanocode/pull/147)，Fixes [#136](https://github.com/mbaiagent/nanocode/issues/136)）：`.env`、私钥、`.netrc`、云凭据等读取时询问，非交互时拒绝；宽泛 allow 规则不放行；bash 常见读法启发式识别。新配置 `permissions.sensitive_read` / `permissions.sensitive_read_exclude`。
- **安全 工具子进程不继承凭据**（[#146](https://github.com/mbaiagent/nanocode/pull/146)，Fixes [#134](https://github.com/mbaiagent/nanocode/issues/134)）：bash / verify / hook 子进程拿不到 `NANOCODE_*`、`NCS_*`（行为变化：本地模式下 `NANOCODE_API_KEY` 也不再传给子进程，厂商 Key 照常传）；平台模式下另外去掉厂商 Key 和像密钥的变量，Web 进程设为不可 dump。新增 `NANOCODE_TOOL_ENV_KEEP` 放行。
- **新增 多会话并行运行**（[#112](https://github.com/mbaiagent/nanocode/pull/112)）：`NANOCODE_WEB_MAX_RUNS`、`NANOCODE_WEB_MAX_RUNS_PER_CLIENT`、`NANOCODE_WEB_MAX_LIVE_SESSIONS`。
- **新增** 自带登录页（`NANOCODE_HTPASSWD`）代替 Basic Auth 弹窗；`/model` 运行时切换模型；`web_search` 支持多个搜索源，默认 DuckDuckGo（[#94](https://github.com/mbaiagent/nanocode/pull/94)）。
- **修复** 刷新 / 重启后恢复聊天记录和工具卡片（[#88](https://github.com/mbaiagent/nanocode/pull/88)）；聊天中切换权限模式（[#87](https://github.com/mbaiagent/nanocode/pull/87)）；本地加载 marked 并过滤 `javascript:` 链接（[#91](https://github.com/mbaiagent/nanocode/pull/91)）；斜杠命令不再显示两次；macOS 本机绑定跳过反向 DNS（[#97](https://github.com/mbaiagent/nanocode/pull/97)）。
- **修复 模型适配**：DeepSeek 思考模式回传 `reasoning_content`（[#101](https://github.com/mbaiagent/nanocode/pull/101)）；思考把 `max_tokens` 用光时加大预算重试一次（[#103](https://github.com/mbaiagent/nanocode/pull/103)，`NANOCODE_MAX_TOKENS_CEILING`）；DeepSeek 搜索改用官方端点（[#107](https://github.com/mbaiagent/nanocode/pull/107)）。
- **安全** 执行前复核权限判定时的真实路径，堵住检查和打开之间换软链接（[#108](https://github.com/mbaiagent/nanocode/pull/108)，#42）。
- **修复** 技能 / 自定义命令的 frontmatter 支持 YAML 块标量（`|`、`|-`、`>`、`>-` 等）和缩进续行，官方技能的 description 不再被解析成 `>` / `|-`（#109）。
- **新增** `examples/minimal_agent.py`：约 260 行、零依赖的最小 Agent（[#116](https://github.com/mbaiagent/nanocode/pull/116)）。

### CI

- **新增 CD**：`.github/workflows/deploy.yml`，main 上 CI 成功后经 SSH 先部署沙箱机再部署平台机；服务器端 `deploy/cd/`（强制命令 + 受限 sudo + 版本目录 + 健康检查失败自动回滚，保留 3 个版本）。见 [docs/cd.md](docs/cd.md)。
- 修掉 Windows / macOS 上长期失败的测试（[#93](https://github.com/mbaiagent/nanocode/pull/93)）；CI 加 concurrency、超时，PR 上缩减矩阵（[#98](https://github.com/mbaiagent/nanocode/pull/98)）。

### 文档

- **新增 维护交接手册** `docs/maintenance/agent-playbook.md`（附英文摘要）：别的 agent 或人可以直接接手审 PR、处理 issue、巡检和部署；`AGENTS.md` 加了入口。
- 新增 [docs/distributed-deploy.md](docs/distributed-deploy.md)（[#125](https://github.com/mbaiagent/nanocode/pull/125)）：平台机 + 沙箱机 + 托管 Postgres / Valkey，含 Neon 冷启动 / libpq 版本坑、Valkey 保活与告警。
- 新增 [docs/cloud-agent-architecture.md](docs/cloud-agent-architecture.md)（[#127](https://github.com/mbaiagent/nanocode/pull/127)）：主流云端编程 Agent 架构调研。
- 新增 [文档索引](docs/README.md)、[配置参考](docs/configuration.md)、[部署拓扑](docs/deployment-topology.md)、[登录与管理](docs/auth-and-admin.md)、[安全模型](docs/security-model.md) 和本文件。
- README 新增「在线使用」（托管实例链接、GitHub 登录 + 邀请码 + 自带 Key）和平台截图（`docs/images/platform/`）。

### 进行中（未合并）

- [PR #128](https://github.com/mbaiagent/nanocode/pull/128)：本地单机模式和平台部署分成两个入口，测试保证本地零依赖。
- [PR #133](https://github.com/mbaiagent/nanocode/pull/133)（Fixes [#131](https://github.com/mbaiagent/nanocode/issues/131)）：回收线程空闲时不查数据库，托管 Postgres 能缩到零；`NCS_REAP_INTERVAL`；启动连库退避重试。
- 计划中：沙箱出站代理（只放行 HTTP/HTTPS，默认关闭，依赖 [#134](https://github.com/mbaiagent/nanocode/issues/134)）；工作区快照到 S3 兼容对象存储（B2）。

### 已知问题

- [#129](https://github.com/mbaiagent/nanocode/issues/129) 中继监听地址没有拒绝 `0.0.0.0`；[#132](https://github.com/mbaiagent/nanocode/issues/132) compose 不支持外部库、remote 模式仍挂 `docker.sock`、运行中连接不重试。
- 安全：[#134](https://github.com/mbaiagent/nanocode/issues/134) 沙箱环境变量里的令牌对子进程可见；[#135](https://github.com/mbaiagent/nanocode/issues/135) 审计日志可被沙箱内的 agent 篡改；[#136](https://github.com/mbaiagent/nanocode/issues/136) 读取敏感文件默认不确认。见 [docs/security-model.md](docs/security-model.md)。
