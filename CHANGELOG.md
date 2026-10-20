# 更新日志

按时间记录合并到 `main` 的主要变化；版本号见 `pyproject.toml`。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。
细节以对应 PR / issue 为准；**「进行中 / 计划中」的条目还没合并，不代表已经可用。**

## [0.2.0] 2026-10-20

### 工程 / CI

- **版本号 0.1.0 → 0.2.0**：涵盖下面「未发布」里已合并的全部改动。
- **CI 加 Python 3.10**：核心和平台测试都跑 3.10（与 `requires-python = ">=3.10"` 一致）；main 上 3 OS × 3 Python，PR 上 ubuntu 跑 3.10/3.11/3.12、macOS / Windows 跑 3.12。
- **CI 加 ruff lint**：`ruff==0.7.4`，只开 `E9,F`；顺带清掉 9 处未使用的导入 / 变量。
- **删除 `[tool.pytest.ini_options]`**：CI 用的是 `unittest`，这段配置不生效。
- **README 数字统一**：测试数只在一处写（900+，核心 600+、平台 300+），核心代码行数更新为约 8600 行，注明平台在 `server/` 且可选；补充评测局限（13 个任务、单次噪声大，比较用 `--repeat 3`+）。

## [未发布] 2026-10

### 多用户平台（nanocode-server）

- **文档 生产架构图**：[docs/deployment-topology.md](docs/deployment-topology.md#生产架构图分布式形态) 新增 mermaid 架构图（平台机 / 沙箱机 / WireGuard / 出站代理 / 快照 / CD）和安全要点。
- **新增 多用户 P0**（[#117](https://github.com/mbaiagent/nanocode/pull/117)）：`NCS_REPLICAS > 1` 时强制 PostgreSQL + Redis，否则拒绝启动；Redis 默认 RESP2，兼容老版本；新增单机 `deploy/compose`（控制面 + 网关 + PostgreSQL 16 + Redis 7 + Docker 沙箱）。
- **新增 多用户 P1**（[#118](https://github.com/mbaiagent/nanocode/pull/118)）：`NCS_AUTH=github`，用 GitHub OAuth 登录（state + PKCE，按 GitHub 数字 id 绑定）；邀请码注册；服务端登录会话（库里只存哈希，可立即吊销）；`/admin` 管理页（邀请码、账号停用 / 启用）；引导管理员 `NCS_ADMIN_GITHUB_LOGINS`；可选 Caddy 自动 HTTPS。模型 Key 由用户自带（BYO），加密存库、只在网关解密。见 [docs/auth-and-admin.md](docs/auth-and-admin.md)。
- **安全 沙箱宿主机隔离**（[#124](https://github.com/mbaiagent/nanocode/pull/124)，Fixes [#123](https://github.com/mbaiagent/nanocode/issues/123)）：沙箱网桥名 / 网段固定（`NCS_DOCKER_BRIDGE`、`NCS_DOCKER_SUBNET`），新增 `deploy/firewall/` 的 firewalld / nftables 脚本和 `check-sandbox-isolation.sh`，沙箱只能连宿主机的网关端口。
- **新增 远程沙箱**（[#126](https://github.com/mbaiagent/nanocode/pull/126)）：`NCS_LAUNCHER=remote`，沙箱跑在另一台机器上，经 WireGuard 等内网隧道连接；新增 `python -m nanocode_server sandbox-agent`（只听内网地址、共享密钥认证、不暴露 Docker API）和网关中继（全局 / 每沙箱连接上限、空闲超时，`NCS_AGENT_RELAY_*`）；新增 `deploy/sandbox-host/`。
- **新增 沙箱出站代理**（[#137](https://github.com/mbaiagent/nanocode/pull/137)，**默认关**）：`NCS_EGRESS_PROXY=1` 时，沙箱代理里会起一个只放行 HTTP/HTTPS 的正向代理。DNS 由代理解析；拒绝内网、元数据、本机、沙箱网段和 WireGuard 网段；防 DNS 重绑定；有每沙箱连接上限和空闲超时，可选域名白名单 / 黑名单；访问日志只记域名和字节数。防火墙脚本新增 `NCS_EGRESS_PORT`，`check-sandbox-isolation.sh` 会验证代理。前置条件 [#134](https://github.com/mbaiagent/nanocode/issues/134) 已由 #146 修复；生产打开前确认镜像版本。相关 [#135](https://github.com/mbaiagent/nanocode/issues/135)、[#136](https://github.com/mbaiagent/nanocode/issues/136)。见 [docs/distributed-deploy.md](docs/distributed-deploy.md#35-出站代理沙箱上网默认关)。
- **修复 老版本 Docker 网络没有 Gateway 字段**（[#130](https://github.com/mbaiagent/nanocode/pull/130)）：建网络时显式写网关，已有网络按网段第一个地址兜底；Docker 26 上沙箱代理不再反复重启。
- **新增 工作区快照**（[#144](https://github.com/mbaiagent/nanocode/pull/144)）：`NCS_OBJECT_STORE=s3`（默认 off），沙箱停止 / 空闲回收时把工作区存进 S3 兼容对象存储（B2、MinIO、R2…），每个会话保留最近 `NCS_SNAPSHOT_KEEP` 份，本地没有时自动恢复，删除会话默认清理云端快照；密钥只在控制面，沙箱代理只拿预签名地址；恢复前流式校验（含链式符号链接）；纯标准库实现，不依赖 boto3。顺带修复 Docker 启动器 chown 跟随符号链接。见 [docs/distributed-deploy.md 第十节](docs/distributed-deploy.md#十工作区快照s3-兼容对象存储)。
- **安全 中继监听地址拒绝 0.0.0.0**（Fixes [#129](https://github.com/mbaiagent/nanocode/issues/129)）：沙箱代理启动时 `NCS_AGENT_RELAY_BIND` 非空且为 `0.0.0.0` / `::` / `*` 直接拒绝启动，和 `NCS_AGENT_BIND` 同一规则；配错不再静默把网关中继暴露到公网。
- **性能 面板轮询退避**（[#145](https://github.com/mbaiagent/nanocode/issues/145)）：面板轮询 `/api/me` + `/api/sessions` 从固定 10 秒改成自适应——无变化时间隔 10s→60s 退避，有变化或回到前台立刻重置；隐藏页签本来就不轮询。闲置页签不再每 10 秒唤醒托管 Postgres。
- **安全 平台页面加 CSP / 防点击劫持**（Fixes #113）：控制面页面默认带 CSP、`frame-ancestors 'none'`、`X-Frame-Options: DENY`；`/s/<id>/` 转发的沙箱面板由平台统一加 CSP（`frame-ancestors 'self'`）和 `X-Frame-Options: SAMEORIGIN`，不再依赖沙箱自己发的头。
- **调参 小机器资源**（#119）：`NCS_SANDBOX_TMPFS`、`NCS_SANDBOX_PIDS`、PG / Redis / 容器内存上限；大小同时认 `384Mi` 和 `384m` 写法。

### 单机 nanocode / Web 面板

- **修复 自动压缩失败时异常逃出主循环**（Fixes [#140](https://github.com/mbaiagent/nanocode/issues/140)）：压缩用的模型调用抛 `LLMError`（限流、配额、网络错误）时直接逃出 `run()`——不发 `turn_end`、终审快照还残留。现在 `_maybe_compact()` 挪进主循环 try，压缩失败发 warning 并带着原上下文继续（真超窗的话紧接着的模型调用会走正常收尾）。
- **修复 write_file / edit_file 非原子写入**（Fixes [#142](https://github.com/mbaiagent/nanocode/issues/142)）：以前直接 `"w"` 打开目标文件，先截断再写，中途失败原内容就丢了；现在先写同目录临时文件、落盘，再 `os.replace` 原子替换，失败时原文件完好。过期检查从 float mtime 改为 `(mtime_ns, size)`，并在写入前紧挨着再校验一次，同一秒内的外部修改也能拦住。
- **修复 429 重试不看 Retry-After**（Fixes [#141](https://github.com/mbaiagent/nanocode/issues/141)）：客户端遇到可重试的 429 时固定按 2/4/8 秒退避，服务端要求等更久时 3 次全白发。现在优先按 `Retry-After` / `X-RateLimit-Reset` 给的时间等（封顶 120 秒），没有这个头才指数退避；平台网关的 429 也不再固定回 `retry-after: 10`，按限流窗口的真实剩余时间返回。
- **修复 最终回答里的代码块显示成 `&quot;`**（Fixes [#138](https://github.com/mbaiagent/nanocode/issues/138)）：Markdown 只转义一次。不再先 `esc()` 再交给 marked，改为让 marked 把原始 HTML 当文本转义输出（`markdown-safe.js` 的 `markdownToHtml`），之后仍经 `sanitizeHtml` 过滤链接协议，XSS 防护不变。
- **安全 根目录 `.gitignore` 忽略密钥文件**（Fixes [#122](https://github.com/mbaiagent/nanocode/issues/122)）：`.env`、`.env.*`、`*.env`、`*.pem`、`*.key`、`*.p12`、`*.pfx`、`id_rsa*` / `id_ed25519*` / `id_ecdsa*` 在任何子目录都不会被 `git add -A` 带进提交；`*.env.example` 和 `*.pub` 照常提交。`tests/test_gitignore.py` 用 `git check-ignore` 验证。
- **变化 `web_fetch` / `web_search` 遵守 `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY`**（[#137](https://github.com/mbaiagent/nanocode/pull/137)）：以前一律直连，现在设了代理就走代理（https 走 CONNECT，证书仍按域名校验）。本地仍会先解析并拦住内网地址；不过经你自己配的上游代理时，代理会再解析一次域名，防 DNS 重绑定就取决于那个代理。设了 `NANOCODE_EGRESS_PROXY=1`（平台沙箱）时，域名直接交给代理解析。
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
- **修复 CD 丢 `.env`**：版本目录被手动换成普通目录后，新版本拿不到沙箱机的 `.env`，部署连续失败。现在 `CARRY` 文件会回退到健康历史里去找，找不到就在切换前拒绝部署。见 [docs/cd.md](docs/cd.md#回滚)。
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
- 计划中：工作区快照到 S3 兼容对象存储（B2）。

### 已知问题

- [#129](https://github.com/mbaiagent/nanocode/issues/129) 中继监听地址没有拒绝 `0.0.0.0`；[#132](https://github.com/mbaiagent/nanocode/issues/132) compose 不支持外部库、remote 模式仍挂 `docker.sock`、运行中连接不重试。
- 安全：[#134](https://github.com/mbaiagent/nanocode/issues/134) 沙箱环境变量里的令牌对子进程可见；[#135](https://github.com/mbaiagent/nanocode/issues/135) 审计日志可被沙箱内的 agent 篡改；[#136](https://github.com/mbaiagent/nanocode/issues/136) 读取敏感文件默认不确认。见 [docs/security-model.md](docs/security-model.md)。
