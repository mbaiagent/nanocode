# 配置参考：NCS_* 环境变量

> 适用于多用户平台 `nanocode-server`（`server/`）、沙箱代理（`sandbox-agent`）和 `deploy/` 下的 compose / 防火墙脚本。
> 单机 `nanocode` / `nanocode --web` **不读任何 `NCS_*` 变量**，它的 `NANOCODE_*` 变量见 [README「配置参考」](../README.md#配置参考)。
>
> 本表按代码整理（`server/nanocode_server/config.py` 的 `Config.from_env()`、`deploy/**` 的 compose 和脚本、`server/tests/`）。
> 以代码为准：表和代码对不上时请提 issue。自己核对可以跑：
>
> ```bash
> rg -o --no-filename 'NCS_[A-Z0-9_]+' server nanocode deploy | sort -u
> ```

布尔值写 `1` / `true` / `yes` / `on` 为真，其它为假；列表用逗号分隔；空字符串等于没配。
「谁读」一列：**平台** = 控制面 + 模型网关（`python -m nanocode_server`、`control`、`gateway`）；**代理** = 沙箱主机上的 `sandbox-agent`；**compose** = 只在 `deploy/*/docker-compose.yml` 里展开，代码不读。

启动前 `Config.validate()` / `validate_agent()` 会检查组合是否安全，配错了**宁可起不来**（例如生产模式没有主密钥、`dev` 认证却监听公网、多副本却用 SQLite）。

## 1. 基础与存储

| 变量 | 默认 | 谁读 | 说明 |
|---|---|---|---|
| `NCS_DATA_DIR` | `./ncs-data`（镜像里 `/data`；compose 里等于 `NCS_HOST_DATA_DIR`） | 平台、代理 | 数据目录：SQLite 文件、dev 模式生成的 `secret.key`、本机 / Docker 沙箱的工作区。权限收紧为 0700 |
| `NCS_SECRET_KEY` | 空 | 平台 | 主密钥（≥32 字符）：签名网关令牌、加密用户的 API Key。控制面和网关必须一致。生产（`NCS_AUTH` ≠ `dev`）必填；`dev` 不配会生成并保存在数据目录。**换了它，库里已有的 Key 都解不开** |
| `NCS_DATABASE_URL` | 空 = 数据目录下的 SQLite | 平台 | `postgresql://...`（需要 `psycopg`）或 `sqlite:///路径`。托管 Postgres 的注意事项见 [distributed-deploy.md 第四节](distributed-deploy.md#四托管-postgresql以-neon-免费版为例) |
| `NCS_REDIS_URL` | 空 = 进程内存 | 平台 | `redis://` / `rediss://`（需要 `redis` 包）。默认 RESP2，要 RESP3 加 `?protocol=3` |
| `NCS_REPLICAS` | `1` | 平台 | 控制面 + 网关的副本数。>1 时必须同时配 PostgreSQL 和 Redis，否则拒绝启动（#72） |

## 2. 控制面与登录

| 变量 | 默认 | 谁读 | 说明 |
|---|---|---|---|
| `NCS_AUTH` | **无默认，必须设置**（compose 里默认 `github`） | 平台 | `github`（GitHub OAuth + 邀请码，生产）/ `proxy`（信任前面的认证代理）/ `dev`（登录页填任意用户名，只允许监听本机）。见 [auth-and-admin.md](auth-and-admin.md) |
| `NCS_HOST` / `NCS_PORT` | `127.0.0.1` / `8700` | 平台 | 控制面监听地址。`dev` 认证只允许本机地址 |
| `NCS_PUBLIC_URL` | 空（compose 里 = `https://${NCS_DOMAIN}`） | 平台 | 对外地址。https 时 Cookie 加 `Secure` 和 `__Host-` 前缀；`github` 模式必填，OAuth 回调是 `<NCS_PUBLIC_URL>/auth/github/callback`，必须 https（本机地址除外） |
| `NCS_GITHUB_CLIENT_ID` / `NCS_GITHUB_CLIENT_SECRET` | 空 | 平台 | GitHub OAuth App 的凭据，`github` 模式必填。Secret 只放进服务器上 root 只读的 `.env` |
| `NCS_GITHUB_OAUTH_URL` / `NCS_GITHUB_API_URL` | `https://github.com` / `https://api.github.com` | 平台 | 只在测试 / 冒烟时指向假的 GitHub（`server/tests/fake_github.py`）。必须 https，本机地址除外 |
| `NCS_ADMIN_GITHUB_LOGINS` | 空 | 平台 | 引导管理员，逗号分隔：GitHub login 或 `id:<数字 id>`（更稳）。首次登录不需要邀请码并成为管理员。`dev` / `proxy` 模式下按用户名 / 代理给的身份匹配 |
| `NCS_SESSION_TTL` / `NCS_SESSION_IDLE` | `2592000`（30 天）/ `604800`（7 天） | 平台 | 登录会话（`github` 模式）的绝对有效期 / 闲置有效期，秒，必须 >0 |
| `NCS_LOGIN_TTL` | `604800`（7 天） | 平台 | `dev` 模式登录 Cookie 的有效期 |
| `NCS_LOGIN_PER_MINUTE` | `10` | 平台 | 每个 IP 每分钟最多几次登录（含 OAuth 回调、邀请码尝试）。多副本时用 Redis 计数 |
| `NCS_TRUST_FORWARDED` | `false`（compose 里固定 `1`） | 平台 | 前面有反向代理时用 `X-Forwarded-For` **最后一项**作客户端 IP。没有代理时别开，否则可伪造 |
| `NCS_AUTH_HEADER` | `X-Auth-Request-Email` | 平台 | `proxy` 模式：认证代理传身份的请求头 |
| `NCS_PROXY_SECRET` | 空 | 平台 | `proxy` 模式必填（≥16 字符）：证明请求经过了认证代理 |
| `NCS_MAX_CONNECTIONS` | `256`（沙箱主机 compose 里 `64`） | 平台、代理 | HTTP 服务并发连接（线程）上限，超限直接 503 |

## 3. 会话与沙箱

| 变量 | 默认 | 谁读 | 说明 |
|---|---|---|---|
| `NCS_LAUNCHER` | `auto`（compose 里 `docker`） | 平台 | `auto` / `local` / `docker` / `k8s` / `remote`。`auto`：在 K8s 里用 K8s，本机有 Docker 和沙箱镜像用 Docker，否则本机子进程（**没有隔离**）。生产模式拒绝静默退化成 `local` |
| `NCS_ALLOW_UNISOLATED` | `false` | 平台 | 显式承认"生产模式也没有隔离"的风险，才允许生产里用 `local`。不建议 |
| `NCS_MAX_SANDBOXES` | `200`（compose 里 `50`） | 平台 | 同时运行的沙箱总数上限，满了直接拒绝（没有排队） |
| `NCS_MAX_SESSIONS_PER_USER` | `3` | 平台 | 每人同时运行的会话数 |
| `NCS_IDLE_TIMEOUT` | `1800` | 平台 | 沙箱空闲多少秒被回收（工作区保留，下次打开恢复对话） |
| `NCS_TOKEN_TTL` | `43200`（12 小时） | 平台、代理 | 网关令牌有效期 = 沙箱最长存活时间。两边要一致 |
| `NCS_SANDBOX_MODES` | 除 `bypass` 外全部 | 平台 | 沙箱里允许的权限模式。`local` 启动器不允许包含 `bypass` |
| `NCS_SANDBOX_MODE` | `default` | 平台 | 沙箱启动时的权限模式，必须在 `NCS_SANDBOX_MODES` 里 |
| `NCS_SANDBOX_IMAGE` | `nanocode-sandbox:latest` | 平台、代理 | Docker / K8s 沙箱镜像 |
| `NCS_SANDBOX_CPU` / `NCS_SANDBOX_MEMORY` | `1` / `2Gi`（沙箱主机 compose 里 `384Mi`） | 平台、代理 | 每个沙箱的 CPU / 内存上限。大小认 `384Mi`、`1Gi`，也认 Docker 写法 `384m`、`1g`（#119） |
| `NCS_SANDBOX_TMPFS` | 空 = `512m` 与内存一半取较小 | 平台、代理 | Docker 沙箱 `/tmp`（tmpfs）大小，算在容器内存里 |
| `NCS_SANDBOX_PIDS` | `512` | 平台、代理 | Docker 沙箱进程数上限，至少 16 |
| `NCS_SANDBOX_MCP_SERVERS` | 空 | 平台 | 平台给每个沙箱配的 MCP 服务器：JSON 对象（格式同 `mcpServers`）或 JSON 文件路径。用户和项目自己配的 MCP 一律不生效 |
| `NCS_DOCKER_RUNTIME` | 空 = Docker 默认 | 平台、代理 | 例如 `runsc`（gVisor） |
| `NCS_DOCKER_NETWORK` | `ncs-sandboxes` | 平台、代理 | 沙箱网络名。复用已有网络时会校验 internal、关闭容器间通信、网桥名和网段，不符就拒绝 |
| `NCS_DOCKER_BRIDGE` | `ncs-sbx0` | 平台、代理、防火墙脚本 | 沙箱网桥网卡名（1–15 个字符）。宿主机防火墙按它写规则（#123） |
| `NCS_DOCKER_SUBNET` | `172.30.250.0/24` | 平台、代理、防火墙脚本 | 沙箱网段（IPv4，至少 /28），网桥地址是第一个地址（`.1`）。老版本 Docker 不返回 Gateway 字段时按第一个地址兜底（#130） |
| `NCS_K8S_API` | `https://kubernetes.default.svc` | 平台 | K8s 启动器：API 地址 |
| `NCS_K8S_NAMESPACE` | `nanocode-sandboxes` | 平台 | 沙箱 Pod 的命名空间 |
| `NCS_K8S_RUNTIME_CLASS` | `gvisor` | 平台 | 沙箱 Pod 的 RuntimeClass |
| `NCS_K8S_STORAGE_CLASS` | 空 = 集群默认 | 平台 | 工作区 PVC 的 StorageClass |
| `NCS_K8S_WORKSPACE_SIZE` | `10Gi` | 平台 | 工作区 PVC 大小 |

## 4. 远程沙箱（`NCS_LAUNCHER=remote`，#126）

拓扑见 [deployment-topology.md](deployment-topology.md) 和 [deploy/sandbox-host/README.md](../deploy/sandbox-host/README.md)。

| 变量 | 默认 | 谁读 | 说明 |
|---|---|---|---|
| `NCS_REMOTE_AGENT_URL` | 空 | 平台 | 沙箱代理地址，走内网隧道，例如 `http://10.77.0.2:8790`。`remote` 模式必填 |
| `NCS_REMOTE_AGENT_SECRET` | 空 | 平台、代理 | 两边共用的密钥（≥32 字符，`openssl rand -hex 32`），常量时间比较 |
| `NCS_REMOTE_GATEWAY_URL` | 空 = 启动时向代理查询 | 平台 | 沙箱访问网关的地址（沙箱主机网桥上的中继），例如 `http://172.30.250.1:8701`。写上则代理暂时不在也能启动 |
| `NCS_AGENT_BIND` | 空（**必填**） | 代理 | 代理监听的内网地址:端口，例如 `10.77.0.2:8790`。拒绝 `0.0.0.0` / `::` |
| `NCS_AGENT_UPSTREAM_GATEWAY` | 空（**必填**） | 代理 | 平台的模型网关，例如 `10.77.0.1:8701` |
| `NCS_AGENT_RELAY_BIND` | 空 = 沙箱网桥地址 | 代理 | 网关中继的监听地址。⚠️ 目前**没有**拒绝 `0.0.0.0` 的校验，见 [#129](https://github.com/mbaiagent/nanocode/issues/129)；请保持为空 |
| `NCS_AGENT_RELAY_MAX_CONNECTIONS` | `128` | 代理 | 中继全局连接上限 |
| `NCS_AGENT_RELAY_PER_SANDBOX` | `16` | 代理 | 每个沙箱（按来源 IP）的中继连接上限，要求 1 ≤ 它 ≤ 全局上限 |
| `NCS_AGENT_RELAY_IDLE_TIMEOUT` | `300` | 代理 | 中继连接两个方向都没数据超过这么多秒就断开，至少 10 |

## 4a. 工作区快照（`NCS_OBJECT_STORE=s3`，PR #144）

设计、生命周期、恢复时的安全校验、B2 配置见 [distributed-deploy.md 第十节](distributed-deploy.md#十工作区快照s3-兼容对象存储)。**只有平台需要这些变量**：沙箱代理没有对象存储密钥，只用控制面给的单对象预签名地址。local / docker / remote 启动器支持，k8s 配了会拒绝启动。

| 变量 | 默认 | 谁读 | 说明 |
|---|---|---|---|
| `NCS_OBJECT_STORE` | `off` | 平台 | `off` / `s3`。`off` 时工作区只在本机 / 沙箱机磁盘上，不需要任何云服务 |
| `NCS_S3_ENDPOINT` | 空 | 平台 | S3 API 地址，必须 `https://`（只有本机 MinIO 可以 `http://127.0.0.1:…`），path-style。B2 形如 `https://s3.<region>.backblazeb2.com` |
| `NCS_S3_REGION` | `us-east-1` | 平台 | 签名区域，B2 是 endpoint 里那段 `<region>` |
| `NCS_S3_BUCKET` | 空 | 平台 | 私有桶名 |
| `NCS_S3_ACCESS_KEY_ID` / `NCS_S3_SECRET_ACCESS_KEY` | 空 | 平台 | 只限这个桶的读写密钥，放 root 只读的 env 文件 |
| `NCS_S3_PREFIX` | `snapshots` | 平台 | 对象 key：`<前缀>/<用户>/<会话>/<UTC 时间>Z.tar.gz` |
| `NCS_SNAPSHOT_KEEP` | `3` | 平台 | 每个会话保留最近几份（1–100） |
| `NCS_SNAPSHOT_MAX_BYTES` | `1Gi` | 平台 | 工作区未压缩大小上限（1Mi–5Gi）；恢复时同样限制解压大小 |
| `NCS_SNAPSHOT_IGNORE` | 空 | 平台 | 追加的忽略规则（逗号分隔的文件名通配）；默认已忽略 `node_modules`、`.venv`、`__pycache__`、`.cache` 等 |
| `NCS_SNAPSHOT_PURGE_ON_DELETE` | `true` | 平台 | 删除会话时删掉桶里这个会话的全部快照 |
| `NCS_SNAPSHOT_ON_DELETE` | `false` | 平台 | 只在 purge 关掉时生效：删除前先存最后一份 |

## 5. 模型网关与模型

| 变量 | 默认 | 谁读 | 说明 |
|---|---|---|---|
| `NCS_GATEWAY_HOST` / `NCS_GATEWAY_PORT` | `127.0.0.1` / `8701` | 平台（端口也被代理和防火墙脚本读） | 网关监听地址。一体启动 + Docker 启动器时 `127.0.0.1` 会自动换成沙箱网桥地址；远程沙箱时设成平台机的隧道地址 |
| `NCS_GATEWAY_URL` | 空 = `http://<HOST>:<PORT>` | 平台 | 沙箱访问网关的地址；控制面和网关分开部署（K8s）时必填 |
| `NCS_PLATFORM_BASE_URL` / `NCS_PLATFORM_API_KEY` / `NCS_PLATFORM_MODELS` | 空 | 平台 | 平台提供、平台付费的模型（三个都配才生效）。不配则只有演示模型和用户自带 Key |
| `NCS_PLATFORM_PROTOCOL` | `openai` | 平台 | `openai` / `anthropic` |
| `NCS_PLATFORM_LABEL` | `平台模型` | 平台 | 页面上显示的名字 |
| `NCS_ENABLE_MOCK_MODEL` | `true` | 平台 | 提供不需要 Key 的演示模型 |
| `NCS_ALLOW_PRIVATE_UPSTREAMS` | `false` | 平台 | 允许用户的 Key 指向内网 / 本机（如本机 Ollama）。**只允许 `dev` 模式** |
| `NCS_DAILY_TOKENS` | `2000000` | 平台 | 每人每天平台模型的 token 上限；用户自带的 Key 只计量不限额 |
| `NCS_RPM` | `30` | 平台 | 每人每分钟模型请求上限 |
| `NCS_MAX_OUTPUT_TOKENS` | `8192` | 平台 | 单次请求输出上限 |

## 6. 只在 compose / 脚本里用的变量（代码不读）

| 变量 | 默认 | 在哪 | 说明 |
|---|---|---|---|
| `NCS_DOMAIN` | 无（必填） | `deploy/compose` | 对外域名，Caddy 用它申请证书，`NCS_PUBLIC_URL` 由它拼出 |
| `NCS_HOST_DATA_DIR` | `/srv/nanocode` | 两个 compose | 宿主机数据目录，宿主机和容器内用同一路径（沙箱的绑定挂载用宿主机路径） |
| `POSTGRES_PASSWORD` / `REDIS_PASSWORD` | 无（必填） | `deploy/compose` | 本机 PostgreSQL / Redis 容器的密码 |
| `NCS_PG_PORT` / `NCS_REDIS_PORT` | `15432` / `16379` | `deploy/compose` | 本机 PG / Redis 发布到宿主机 `127.0.0.1` 的端口 |
| `NCS_PG_SHARED_BUFFERS` / `NCS_PG_MAX_CONNECTIONS` | `128MB` / `100` | `deploy/compose` | PostgreSQL 调参（#119） |
| `NCS_REDIS_MAXMEMORY` | `0`（不限） | `deploy/compose` | Redis maxmemory，满了 LRU 淘汰 |
| `NCS_PG_MEM_LIMIT` / `NCS_REDIS_MEM_LIMIT` / `NCS_PLATFORM_MEM_LIMIT` | `0`（不限） | `deploy/compose` | 容器内存上限 |
| `NCS_AGENT_MEM_LIMIT` | `128m` | `deploy/sandbox-host` | 沙箱代理容器的内存上限 |
| `NCS_FIREWALL_ZONE` | `ncs-sandbox` | `deploy/firewall/ncs-sandbox-firewalld.sh` | 给沙箱网段建的 firewalld zone 名 |

> ⚠️ `deploy/compose/docker-compose.yml` 只把**列出来的**变量传进平台容器。`NCS_PLATFORM_*`、`NCS_SANDBOX_MODES` / `NCS_SANDBOX_MODE`、`NCS_SANDBOX_MCP_SERVERS`、`NCS_SESSION_TTL` / `NCS_SESSION_IDLE`、`NCS_TOKEN_TTL`、`NCS_DOCKER_NETWORK`、`NCS_AUTH_HEADER` 等写进 `.env` **不会生效**，要在本机 `docker-compose.override.yml` 里加到 `platform.environment`。数据库和 Redis 地址也写死成本机容器，用托管库的写法见 [deployment-topology.md](deployment-topology.md#托管-postgres--redis)（#132）。

## 7. 测试用

| 变量 | 说明 |
|---|---|
| `NCS_TEST_DATABASE_URL` | 设了才在 PostgreSQL 上再跑一遍存储 / 账号测试 |
| `NCS_TEST_REDIS_URL` | 设了才跑 Redis 限流测试 |
| `NCS_TEST_DOCKER` | `1` 时真的启动 Docker 沙箱（需要已构建沙箱镜像） |

## 8. 兼容与计划中的变量

| 变量 | 状态 | 说明 |
|---|---|---|
| `NCS_SEARCH_*` | 兼容旧名 | `web_search` 的旧前缀，新名是 `NANOCODE_SEARCH_*`（见 README） |
| `NCS_REAP_INTERVAL` | **进行中**：[PR #133](https://github.com/mbaiagent/nanocode/pull/133)（未合并） | 回收线程间隔（默认 60 秒）；空闲时不查数据库，让托管 Postgres 能缩到零（[#131](https://github.com/mbaiagent/nanocode/issues/131)） |
| 出站代理相关（issue 里暂称 `NCS_EGRESS_PROXY`） | **计划中**，变量名以 PR 为准 | 沙箱只放行 HTTP/HTTPS 的出站代理，默认关闭；**在 [#134](https://github.com/mbaiagent/nanocode/issues/134) 修完前不应在生产打开** |
