# nanocode 多用户登录方案（按 1000+ 注册用户设计）

> 只是方案，没有改代码、没有提交。基于 `mbaiagent/nanocode` main `421986a`（2026-10-19 只读克隆），参考：`server/` 源码与 README、`docs/web-multisession-plan.md`、`docs/issue-7-rate-limit.md`、`docs/12-distributed-deployment.md`、`verification/results/perf-loadtest.md`，以及 issue #5、#106、#72、#66、#68。
> 文中凡标 **[估]** 的数字都是估算，不是实测；标 **[测]** 的来自仓库里已有的压测记录。

## 0. 结论先说

1. **1000+ 用户走 `server/` 平台，不走单进程 Web 面板。** 面板是"一个进程、一个工作目录、一个系统用户"，三期做完也只是"几个互相信任的人共用"（`web-multisession-plan.md` 自己也这么写）。`server/` 已经是"控制面 + 模型网关 + 每会话一个沙箱 + SQLite/PG + 内存/Redis 限流 + Docker/K8s"，#36–#65 的安全和可靠性问题已全部关闭。缺的主要是**原生账号体系**、**配额套餐和排队**、**可观测和审计**，以及**真实规模的容量验证**。
2. **UI 已经是复用关系，不需要合并。** 平台页面（`server/.../static`，约 800 行）只管登录、会话列表、模型与密钥；对话界面就是沙箱里原样运行的 `nanocode --web`，通过 `/s/<id>/` 转发。面板的多会话一期、二期照常做，本地单人用得上。**面板三期（按用户归属）建议不做**，或者只做最小版本，标明"仅限可信小团队"。在平台里一个会话就是一个沙箱，沙箱内保持单会话即可。
3. 当前部署：用户盒子 `104.194.92.198` 上跑的是单用户 `nanocode --web` 面板（看截图得知，我没有访问）。它不能直接拿来当多用户入口，可以留作个人面板，或者在 P0 改成 docker-compose 单机试点。

## 1. 现状核对（和 1000 人目标的差距）

| 方面 | 现在 `server/` 有的 | 缺的 |
|---|---|---|
| 登录 | `NCS_AUTH=dev`：输入任意用户名，只限本机。`proxy`：前面挂 oauth2-proxy，再用共享密钥证明请求经过了代理 | 没有用户表、注册、邀请、密码、邮箱验证、找回、管理员、登录会话吊销，也没有防爆破 |
| 身份 | `owner` 只是一个字符串（用户名或邮箱），存在 sessions、credentials、usage 三张表 | 没有 `users` 表，没有角色和状态（停用、封禁） |
| 会话与沙箱 | 每会话一个沙箱；数据库原子抢占启动；空闲回收（1800 秒）；`NCS_MAX_SANDBOXES=200`，`NCS_MAX_SESSIONS_PER_USER=3` | 容量满了**直接 503 拒绝，不排队**；没有预热池；K8s 每会话一个 10Gi PVC，没有生命周期管理 |
| 模型与 Key | 用户自带 Key（BYO）加密存库，只有网关解密；平台 Key 有每日 token 额度（预留后只退不补，#54）；每人 RPM 限制 | 没有套餐和分级额度，没有计费事件和账单钩子；SecretBox 主密钥一换，旧 Key 就全部解不开（没有密钥版本） |
| 存储 | SQLite / PG 用同一套 SQL，schema 迁移幂等（#64） | **#72 的两条建议没落地**：Redis 没默认 RESP2（老 Redis 会报 `unknown command 'HELLO'`）；SQLite 加非本机监听或多副本时不拒绝启动（#72 已复现"登录后被踢回登录页"的脑裂） |
| HTTP | 标准库线程服务器，有并发上限 `NCS_MAX_CONNECTIONS=256` 和请求超时（#55） | 每条 SSE 长连接占一个线程，所以 300 人同时在线要么开多副本，要么调大上限 |
| 观测 | — | README 自己写了：没有指标、追踪、告警、计费和管理后台；审计日志在沙箱里，Agent 能改 |
| 验证 | [测] 单机 4 vCPU / 16 GB 上，100 个用户同时开会话全部成功。用的是假模型，每个沙箱约 20 MB。建会话耗时随并发线性增长，每多一个约 +0.24 秒（100 并发时 p95 27 秒），瓶颈在 Docker 守护进程，约每秒创建 10 个容器 | K8s 清单没在真实集群跑过，没测过 gVisor、真实模型和小时级稳定性 |

## 2. 目标架构

```
浏览器 ─TLS─▶ 入口（Caddy/nginx，SSE 关缓冲）
              ├─▶ 控制面 ×N（无状态）：账号/登录、会话、配额、排队、管理后台、/s/<id>/ 转发
              │        │                              │
              │        ▼                              ▼
              │   PostgreSQL（唯一权威状态）      沙箱池（Docker → K8s+gVisor）
              │   Redis（限流/登录节流/排队/      每会话一个 nanocode --web
              │          在线状态，可丢）         无外网，只能访问网关
              │        ▲                              │
              └────────┴──── 模型网关 ×N ◀─────────────┘ ──▶ 模型厂商（平台 Key / 用户 Key）
              对象存储（可选）：工作区快照、导出
```

- **沙箱内容换到单独的源上（重要）。** 现在 `/s/<id>/` 和控制面同源。一旦面板渲染模型输出时出现 XSS，就能用受害者的身份同源调用 `/api/credentials`、`/api/sessions`。转发时已经剥掉 Cookie，但同源 fetch 照样会带上。多人之后这就成了跨用户风险。建议沙箱界面放在 `https://<sid>.sbx.example.com`，或者至少统一用 `sbx.example.com`，用一次性票据换沙箱专用 Cookie，再配上严格的 CSP。
- **SSE 不需要 Redis 扇出。** 现在每个浏览器的事件流由控制面直接转发到它会话的沙箱，任何副本都能转发，所以不需要粘性会话。Redis 只用于计数、节流、排队和在线状态。断线续传（Redis Streams）留到以后。

## 3. 认证

| 项 | 方案 |
|---|---|
| 模式 | 新增 `NCS_AUTH=builtin`（原生账号），保留 `dev` 和 `proxy`（企业 SSO 继续用 oauth2-proxy）。三种模式都映射到同一张 `users` 表；proxy 模式下首次出现的身份自动建账号 |
| 登录方式（建议优先级） | ① **GitHub OAuth**（用户群是开发者，零密码，用 state + PKCE）；② **邮箱魔法链接**（一次性令牌，15 分钟有效，只存哈希，用一次就失效）；③ 密码，可选，默认关闭。通用 OIDC（Google、企业 IdP）放在后面，或者继续走 proxy |
| 密码哈希 | 优先 argon2id（`argon2-cffi`，可选依赖）；不装就用标准库 `hashlib.scrypt`（n=2^15, r=8, p=1）。参数写进哈希串，以后可以升级。用 HIBP 前缀查询拒绝弱密码，可选 |
| 登录会话 | **不用 JWT，用服务端会话**：Cookie 里放 32 字节随机令牌，库里只存 SHA-256。好处是能立即吊销（退出、改密、封禁、"登出所有设备"）。Cookie 用 `__Host-` 前缀，加 HttpOnly、Secure、SameSite=Lax；空闲 7 天、绝对 30 天过期；Redis 缓存 60 秒，减少 PG 查询。网关令牌继续用现有的 HMAC 签名令牌（沙箱用，和登录无关） |
| CSRF | 沿用现有做法：写接口只收 JSON，Origin 必须是本站，SameSite。OAuth 回调校验 state。魔法链接点开后要再点一次按钮，用 POST 确认，防止邮件扫描器预取导致误登录 |
| 防爆破 | Redis 计数：每 IP 每分钟 10 次；每账号或邮箱每 15 分钟 5 次失败后渐进延迟，再锁定 15 分钟；魔法链接每个邮箱每小时 3 封。触发阈值后才要人机验证（Turnstile/hCaptcha，可选）。报错信息不区分"账号不存在"和"密码错误" |
| 注册 | `NCS_SIGNUP=invite`（默认，邀请码，可设次数和过期）/ `domain`（允许的邮箱域名）/ `open`（开放注册，配合邮箱验证和等待名单）。另有 GitHub 组织白名单，可选 |
| 管理员 | `python -m nanocode_server admin create <email>` 创建第一个管理员。角色分 `user`、`admin`。管理后台能做：用户列表、停用或封禁（同时吊销登录会话、停掉它的沙箱）、改套餐和额度、发邀请、看用量。管理员操作全部写审计日志 |

## 4. 数据模型（PostgreSQL，迁移 v2 起）

| 表 | 关键字段 | 说明 |
|---|---|---|
| `users` | id (uuid), email (唯一, 小写), display_name, role, status(active/disabled/banned), plan_id, created, last_login | 新增 |
| `identities` | user_id, provider(github/email/oidc/proxy), subject, 唯一(provider, subject) | 一个用户可以绑多个登录方式 |
| `password_credentials` | user_id, hash, updated | 可选 |
| `auth_sessions` | token_hash, user_id, created, last_seen, expires, ip, ua, revoked | 登录会话 |
| `login_tokens` | token_hash, email, purpose(magic/verify/reset), expires, used | 魔法链接、邮箱验证 |
| `invites` | code_hash, created_by, max_uses, used, expires | |
| `plans` | id, daily_tokens, monthly_tokens, max_running_sessions, max_sessions, storage_gb, allow_platform_models | 套餐。现在的 `NCS_DAILY_TOKENS` 等变量作为默认套餐 |
| `sessions` | 现有表；`owner` 改为存 user_id | 迁移时按 owner 字符串回填 |
| `credentials` | 现有表；`secret` 加密钥版本前缀 | BYO Key |
| `usage` | 现有的按天汇总表 | 额度判断 |
| `usage_events` | ts, user_id, session_id, model, billable, input/output/reasoning_tokens, cost_estimate | 账单明细，按月分区 |
| `run_queue` | session_id, user_id, enqueued, priority | 容量满时排队，也可以只放 Redis |
| `audit_log` | ts, actor, action, target, ip, detail(jsonb) | 只追加；应用账号没有 UPDATE/DELETE 权限 |
| `api_tokens` | user_id, name, token_hash, scopes, last_used, expires | 可选，给 CLI 或脚本访问平台用 |

- **对话内容**：继续存在沙箱工作区里（Docker 存数据目录，K8s 存 PVC），控制面只存索引。以后（P4）在会话停止时把工作区打包快照到对象存储，回收 PVC。
- **静态加密**：
  - 现在的 SecretBox 是"HMAC-SHA256 流加密 + 认证标签，绑定 AAD"，只用了标准库，可以继续用。
  - 生产建议改成 AES-256-GCM（`cryptography`，可选依赖）加信封加密。数据密钥放在库里，主密钥放在 KMS 或 K8s Secret。密文加上 `key_id`，这样**可以轮换主密钥**；现在换主密钥会让所有用户的 Key 失效。
  - 磁盘层面靠云盘或 PG 自带的加密。
- **BYO Key 和平台 Key**：两种代码里都已经支持，决策见开放问题 2。建议默认是"平台给少量免费额度做试用，长期使用让用户自带 Key"。平台 Key 按套餐限额；BYO Key 只计量、不限额，但仍受 RPM 和并发限制，防止拖垮沙箱资源。

## 5. 隔离

- **沙箱粒度**：保持**每会话一个沙箱**（现有方案 A），不改成每用户一个，因为每会话的隔离边界最清楚。一个用户的多个会话之间也互相隔离。
- **资源**：默认 `limits` 设 1 vCPU / 2 GiB（现状），`requests` 降到 0.25 vCPU / 512 MiB，靠超卖。理由是 Agent 大部分时间在等模型 [估]。另外限制 pids、只读根文件系统、去掉 capabilities、用 gVisor（`runsc`）。
- **存储**：
  - K8s 现在每会话一个 10Gi PVC，1000 个用户、每人几个会话会让存储失控。
  - 改法：默认 2–5Gi；每用户总配额由套餐决定；停止超过 N 天的会话快照到对象存储，然后删掉 PVC；删除会话立即删除数据。
- **网络**：沙箱默认无外网，只能访问网关（已实测）。需要 pip、npm、git clone 时，加一个**只读包镜像或白名单出口代理**，平台层面配置（开放问题 5）。
- **单机 Docker 阶段**：控制面持有 docker.sock，等于宿主 root。所以控制面本身必须当作可信组件，单独部署，不跑任何用户代码。

## 6. 容量与成本估算

### 6.1 负载假设（沿用 `docs/12` 第二节的假设，全部是 [估]）

| 指标 | 取值 |
|---|---|
| 注册用户 | 1000 |
| 高峰同时在线（页面开着） | 30% ≈ **300**，每人一条 SSE |
| 高峰同时在跑任务的会话 | 5–10% ≈ **50–100**。`docs/12` 写的是 10–20%（100–200），这里按任务给的 50–100 做基线，200 当压力档 |
| 每步模型调用 | 输入 5k–60k tokens，输出 0.2k–2k |
| 每个任务 | 5–30 步 |

### 6.2 推出来的规模（[估]）

| 资源 | 50 并发运行 | 100 并发运行 | 200 并发运行 | 依据 |
|---|---|---|---|---|
| 存活沙箱（运行中 + 空闲未回收） | ~100 | ~200 | ~400 | 假设空闲 1800 秒回收前，存活数 ≈ 运行数 × 2 [估] |
| 沙箱 CPU requests / 内存 requests | ~25 vCPU / 50 GiB | ~50 / 100 | ~100 / 200 | 每个存活沙箱 0.25 vCPU / 512 MiB；跑测试、装依赖时会冲到 limit。[测] 的 20 MB 是假模型下的空载值，不能拿来算 |
| 平台本身（控制面、网关、PG、Redis） | ~4–8 vCPU / 16 GiB | 同左 | ~8–16 vCPU | [测] 登录、发消息 p95 在 100 并发时 <1 秒，平台开销小 |
| 控制面副本 | 2 | 2–3 | 3–4 | 300 条 SSE ÷ 每副本 256 线程，再留余量 |
| LLM 调用速率 | 2–5 次/秒 | 3–10 次/秒 | 7–20 次/秒 | 每个会话每 10–30 秒调用一次（`docs/12`） |
| LLM 输入 token | ~0.9–9M/分钟 | ~1.8–18M/分钟 | 13–36M/分钟（`docs/12` 原文） | 速率 × 平均输入 5k–30k |
| 冷启动 | 50 个同时建：约 12 秒 [测] | 100 个：p95 约 27 秒 [测] | 线性外推约 50 秒以上 [估] | 所以 P4 需要预热池 |

### 6.3 算力成本（[估]，数量级，要按实际云厂商报价核对）

- 按通用型云主机按需价约 $0.04–0.05 / vCPU·小时（含 4 GiB 内存）的量级 [估，未核实具体厂商]：
  - 50 并发：单台 32 vCPU / 128 GiB 约 $1,000–1,200/月。预留实例或包年可以再低 30–50%。
  - 100 并发：约 64 vCPU，相当于 2 台，约 $2,000–2,500/月。
- **模型费用通常远大于算力费用。** 日成本 ≈ 日活任务数 × 平均步数 × 每步 token × 单价。
  - 举例 [估]：每天 2000 个任务 × 15 步 × 20k 输入 token = 6 亿输入 token/天，按所用模型的单价乘出来。
  - 所以"谁付模型费"（开放问题 2）是第一决策。平台付费的话，必须有套餐额度和单任务预算熔断。
- 厂商限流：100 并发时每分钟上千万输入 token，多数默认账号的 TPM 上限扛不住（`docs/12` 的判断）。要么提前申请提额，要么在网关上做多 Key、多厂商池。

## 7. 配额、排队与计费钩子

- **限制层次**：每用户 RPM（已有）→ 每用户同时运行会话数（套餐）→ 全局沙箱数（已有）→ 每日和每月 token（平台模型）→ 单任务 token 预算（复用面板 #7 的 `QuotaExhausted` 路径）。
- **满了排队，不拒绝**：现在是 503"平台容量已满"。改成先进 `run_queue`，页面显示"排队第 n 位"，按公平规则出队（每用户轮转），排队超过 N 分钟才失败。
- **计费钩子**：网关结算时写 `usage_events`，再按周期聚合，通过 webhook 推给外部计费系统（Stripe 或自建）。平台本身不做支付。额度用到 80% 和 100% 时发站内或邮件通知。

## 8. 可观测与审计

- `/metrics`（Prometheus 格式）指标：
  - 请求量、延迟、状态码；
  - 沙箱运行中和启动中的数量、启动耗时直方图、回收数；
  - 排队长度和等待时间；
  - 网关的上游延迟和错误率、429 次数；
  - 按模型统计的 token 用量；
  - PG 连接池占用、Redis 延迟。
- 结构化 JSON 日志，带 request_id、user_id、session_id；不记录 Key，也不记录对话正文。
- 告警示例：沙箱启动 p95 超过 30 秒、5xx 比例超过 1%、上游 429 激增、单用户 token 突增、PG 连接池用满。
- **可信审计**：写进 PG 的 `audit_log`（控制面写，沙箱碰不到），解决 README"审计在沙箱里、Agent 能改"的问题。记录这些事件：登录成功和失败、退出、改密、绑定或解绑身份、添加或删除 Key、建或删会话、换模型、管理员操作、额度变更。保留 180 天 [估，按合规要求定]。
- 链路追踪（OpenTelemetry）放到 K8s 阶段再做。

## 9. 安全要点

| 风险 | 处理 |
|---|---|
| 工具结果里的提示注入（网页、仓库文件、MCP 输出） | 影响面限制在自己的沙箱：没有外网、拿不到真 Key、不开 bypass 模式。权限确认框显示真实的命令和路径，不显示模型的转述。网页、文件内容加上"不可信内容"标记。MCP 只允许平台配置的（已有） |
| 跨用户 XSS | 沙箱换到单独的源，加 CSP（见第 2 节）。面板渲染 Markdown 的 XSS 修复（#29–#36 那批）要持续有回归测试 |
| SSRF | 网关上游检查已有（只允许 https 公网地址，并固定已检查的解析 IP）；面板 `web.py` 的数字 IP 写法也修过（PR #98） |
| 密钥 | `NCS_SECRET_KEY` 放在 Secret 或 KMS 里，控制面和网关分开派生子密钥（#62 已做）。加密钥版本，支持轮换。OAuth client secret、SMTP 密码只放在 Secret 里 |
| 账号接管 | 邮箱必须验证才能绑定。改密或改邮箱时吊销所有登录会话并发通知邮件。GitHub 登录按 GitHub 的用户 id 绑定，不按 login 名（login 名可以改） |
| 滥用（拿平台当免费算力或代理） | 邀请制或域名白名单；新账号额度低；沙箱 CPU 和时长有上限（`NCS_TOKEN_TTL` 已限制最长 12 小时）；管理员一键封禁 |

## 10. 部署拓扑

1. **起步（P0–P3）：单台 VM + docker-compose**
   - Caddy（自动 TLS）、控制面 ×2、网关 ×1、PG 16、Redis 7（要 ≥6；如果只能用老版本 Redis，见 #72 的 `?protocol=2`）、Docker 启动器加 gVisor。
   - 沙箱和平台在同一台机器上。建议 32 vCPU / 128 GiB 起步，可以承载约 50 个并发运行 [估]。
   - 每日 PG 备份（`pg_dump` 到对象存储）。
2. **扩展（P5）：托管 K8s**
   - 用现有的 `deploy/k8s/` 清单；托管 PG 和 Redis；沙箱节点池 + Cluster Autoscaler + gVisor RuntimeClass；平台组件 HPA。
   - 清单先在真实集群里验证（README 写明还没跑过）。
3. **`104.194.92.198`**：目前跑的是单用户面板。可以选择作为单机试点机（要先确认规格），或者保持个人面板，另开一台。

## 11. 从现状迁移

1. 面板不动，单人本地用法不受影响。
2. `server/` 加迁移 v2，回填用户：把 sessions、credentials、usage 里出现过的 `owner` 字符串去重后建 `users` 行（像邮箱的就当 email，否则当用户名），然后把 `owner` 换成 user_id。迁移要幂等，和 #64 的约定一致。
3. `dev` 和 `proxy` 模式照常工作：proxy 首次见到一个身份就自动建用户。`builtin` 是新增模式，不影响现有部署。
4. 启动检查补上 #72：监听非本机地址或声明了多副本、而数据库还是 SQLite 时拒绝启动；Redis 默认用 RESP2，或者文档注明要 ≥6。

## 12. 分期计划（规模和时间都是 [估]，按一个人全职算）

| 期 | 内容 | 主要文件 | 规模 | 测试 | 时间 |
|---|---|---|---|---|---|
| **P0 生产基线** | #72 两条（SQLite 拒绝多副本，Redis RESP2）；docker-compose（Caddy + PG + Redis + 平台 + 沙箱镜像）；健康检查；`admin` 命令框架 | `config.py`、`ratelimit.py`、`deploy/compose/*`、`__main__.py` | ~300 行 + compose | ~10 | 2–3 天 |
| **P1 原生账号** | `builtin` 认证：用户、身份、登录会话表；GitHub OAuth；邮箱魔法链接（SMTP）；可选密码（scrypt/argon2id）；邀请和注册策略；登录节流；登出所有设备；登录和注册页面；owner 迁移 | 新增 `security/accounts.py`、`security/passwords.py`、`services/users.py`、`services/mailer.py`、`web/auth_routes.py`；改 `auth.py`、`sql.py`（迁移 v2）、`static/login.html` | ~1800 行 | ~60（OAuth 用假的 GitHub 服务，邮件用假的 SMTP） | 1.5–2 周 |
| **P2 套餐、排队、管理后台** | plans 表；每用户并发数；容量满时排队加公平出队；`usage_events`；计费 webhook；管理后台（用户、额度、邀请、封禁） | `services/quotas.py`、`services/queue.py`、`web/admin.py`、`static/admin.*`、`gateway.py` | ~1400 行 | ~45 | 1.5 周 |
| **P3 可观测与审计** | `/metrics`、JSON 日志、`audit_log`、告警规则示例、Grafana 面板示例 | `observability.py`、各服务埋点、`deploy/monitoring/*` | ~700 行 | ~20 | 1 周 |
| **P4 安全与容量** | 沙箱换到独立源加 CSP；预热池（需要沙箱支持启动后再注入策略，这是难点）；工作区配额、快照和回收；用接近真实的模型（固定延迟加真实长度）压 50、100、200 并发；AES-GCM 信封加密和密钥轮换 | `proxy.py`、`runtime/*`、`nanocode/policy.py`、`loadtest.py`、`secretbox.py` | ~1200 行 | ~35 | 2–2.5 周 |
| **P5 K8s 上线** | 真实集群验证清单；HPA 和 Autoscaler；托管 PG 和 Redis；演练（杀节点、滚动升级）；评估要不要把线程服务器换成异步（看 P4 压测结果再定） | `deploy/k8s/*`、文档 | 以清单和文档为主 | 部署测试 | 1–2 周 |

合计约 **5.5k 行、170 个测试、7–9 周** [估]。`docs/12` 估的是"多用户最小可用版 4–6 周 + 可靠性 3–4 周"，但其中控制面、网关、沙箱已经做完，所以这里主要是账号、配额和运维这部分。**P0 + P1 做完（约 2.5 周）就可以邀请内部 20–50 人试用**；P2、P3 做完再开放到几百人；P4 的压测通过后再承诺 1000 人。

## 13. 风险

1. **模型费用失控**：平台付费时，少数长任务就能烧掉预算。要靠套餐额度、单任务熔断和告警三道闸。
2. **冷启动**：[测] 冷启动随并发线性增长，早上集中开工会排队几十秒。预热池和"策略后注入"的改动面比较大。
3. **存储膨胀**：每会话一个 PVC 不回收，成本会持续上涨。
4. **K8s、gVisor 没有实测**，真实集群上可能踩到 NetworkPolicy、存储类的坑。
5. **同源 XSS**：现在的同源结构在多人场景下是放大器，P4 之前开放注册要慎重。
6. **线程服务器**：300 条以上 SSE 靠多副本能撑住，到几千在线就要换异步。不过 1000 注册用户的规模下，这一点暂时不卡脖子 [估]。
7. 邮件送达率（魔法链接进垃圾箱）和 GitHub OAuth 应用审核都依赖外部服务。

## 14. 需要你定的问题

1. **登录方式**：GitHub OAuth + 邮箱魔法链接（我的建议），还是要密码登录，或者对接公司 SSO（那就继续走 proxy）？
2. **谁付模型费**：只让用户自带 Key、平台全包（要定套餐和预算），还是"平台给少量试用额度，长期自带 Key"（我的建议）？要不要接收费？
3. **产品形态**：自托管（给别人部署用，重点在文档和 compose）还是你自己运营的 SaaS（重点在注册、风控和计费）？开放注册，还是邀请制？
4. **部署目标**：先在单台 VM 上用 docker-compose 起步（`104.194.92.198` 的规格够吗，还是另开一台？），还是直接上云厂商的托管 K8s？预算上限是多少？
5. **沙箱要不要外网**：用户需要 `git clone` GitHub 私有仓库、`pip` / `npm install` 吗？要的话得做包镜像或白名单出口代理，还要设计怎么把用户的 GitHub 授权安全地给到沙箱。
