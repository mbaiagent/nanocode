# 分布式部署：平台机 + 沙箱机 + 托管数据库

> 状态：**规划 / 进行中**。本文描述的远程沙箱（`NCS_LAUNCHER=remote` + 沙箱代理）还在开发，代码以合并后的 PR 为准。
> 前置的宿主机隔离修复见 [#123](https://github.com/mbaiagent/nanocode/issues/123) / [PR #124](https://github.com/mbaiagent/nanocode/pull/124)；远程沙箱的 PR 叠在 #124 之上。
> 相关：#5、#106、#119；单机部署见 [deploy/compose/README.md](../deploy/compose/README.md)，总体设计见 [multiuser-1000-plan.md](multiuser-1000-plan.md)、[12-distributed-deployment.md](12-distributed-deployment.md)。

本文所有地址、域名、账号都是**示例占位符**：

| 占位符 | 含义 |
|---|---|
| `nanocode.example.com` | 对外域名 |
| `203.0.113.10` | 平台机公网 IP（RFC 5737 文档网段） |
| `198.51.100.20` | 沙箱机公网 IP（RFC 5737 文档网段） |
| `10.77.0.1` / `10.77.0.2` | WireGuard 内网地址：平台机 / 沙箱机 |
| `<endpoint-id>`、`<db-host>`、`<redis-host>` 等 | 托管服务控制台里给的值，**不要提交到仓库** |

---

## 一、为什么拆

单机 compose 把 nginx/TLS、控制面、网关、PostgreSQL、Redis、所有沙箱容器都放在一台机器上。小 VPS（1 GB 内存）上，数据库和沙箱会互相挤内存，同时只能跑 1 个会话。

拆开之后：

| 组件 | 放在哪 | 说明 |
|---|---|---|
| nginx + TLS、控制面、模型网关 | **平台机**（一台小 VPS） | 对外只开 80/443 和 SSH |
| 每个会话的沙箱容器 + 沙箱代理 | **沙箱机**（另一台小 VPS） | 只跑沙箱；代理只听 WireGuard 地址 |
| PostgreSQL | **托管 Postgres**（例：Neon 免费版） | 用户、会话、邀请码等持久数据 |
| Redis 兼容 | **托管 Valkey/Redis**（例：Aiven Valkey 免费版） | 限流计数、跨副本事件 |

平台机不再跑数据库和沙箱，内存宽松很多；沙箱机就算被打穿，也碰不到数据库和平台机上的密钥。

## 二、拓扑

```mermaid
flowchart LR
    U[浏览器] -->|HTTPS 443| N

    subgraph P[平台机 203.0.113.10]
        N[nginx + TLS] --> CP[控制面 :8700<br/>127.0.0.1]
        CP --- GW[模型网关 / relay]
        WGP[wg0 10.77.0.1]
    end

    subgraph S[沙箱机 198.51.100.20]
        WGS[wg0 10.77.0.2]
        AG[沙箱代理<br/>只听 10.77.0.2]
        AG -->|unix socket| D[(Docker 引擎)]
        D --> SB1[沙箱 A]
        D --> SB2[沙箱 B]
    end

    WGP <-->|WireGuard UDP<br/>只允许对端| WGS
    CP -->|起停沙箱 / 转发 /s/&lt;id&gt;/<br/>共享密钥认证| AG
    SB1 -.->|只能连 relay 端口<br/>和出站代理端口| AG
    SB2 -.->|只能连 relay 端口<br/>和出站代理端口| AG
    AG -.->|经 wg 转发到网关| GW
    AG -.->|出站代理（可选，默认关）<br/>只放 HTTP/HTTPS，拦内网| NET[公网网站 / 包仓库]

    CP -->|TLS, pooled| PG[(托管 Postgres<br/>Neon)]
    CP -->|rediss://| R[(托管 Valkey<br/>Aiven)]
    GW -->|HTTPS| LLM[用户自带 Key 的模型 API]
```

请求路径：

1. 浏览器 → 平台机 nginx（TLS 终止）→ 控制面（只听 `127.0.0.1:8700`）。
2. 新建会话：控制面经 WireGuard 调沙箱代理 `http://10.77.0.2:<agent-port>`，带共享密钥；代理在本机 Docker 里起沙箱容器。
3. 打开会话页面 `/s/<id>/`：控制面经 WireGuard 把请求转发给代理，代理再转给沙箱容器。
4. 沙箱里的 Agent 调模型：沙箱只能连代理暴露在沙箱网桥上的 relay 端口，代理经 WireGuard 转到平台机网关，网关再带上用户自己的 Key 去调模型 API。模型调用**不经过**出站代理（`NO_PROXY` 里有 relay 地址）。
5. 沙箱上网（可选，`NCS_EGRESS_PROXY=1`，默认关）：沙箱里的 `web_fetch`、`web_search`、`pip`、`git`、`curl` 经 `HTTP(S)_PROXY` 连沙箱代理里的出站代理，由它解析域名、拦内网后再连公网。见 [3.5 出站代理](#35-出站代理沙箱上网默认关)。

## 三、安全边界

### 3.1 WireGuard

- 两台机器各一个 `wg0`，私网 `10.77.0.0/24`（示例），`AllowedIPs` 只写对端的 `/32`。
- 只有沙箱机需要对外多开一个 WireGuard UDP 端口，并且在防火墙里只允许平台机公网 IP 访问；平台机主动连出即可，不用开端口。
- 平台机不开端口的前提是**隧道一直由平台机维持**：沙箱机发往平台机的流量（relay → 网关）只能沿着平台机连出时留下的 conntrack 状态回程。所以平台机的 `[Peer]` 里必须写 `PersistentKeepalive = 25`，让平台机每 25 秒发一个保活包，NAT / 防火墙状态不会过期。不写的话，隧道空闲一段时间后，沙箱机这一侧先发起的包会被平台机防火墙丢掉，表现为模型调用偶发超时。沙箱机一侧不需要这个参数。
- 私钥只放在各自机器的 `/etc/wireguard/`（`chmod 600`），不进仓库，不进聊天记录。

平台机 `/etc/wireguard/wg0.conf` 示例（密钥是占位符）：

```ini
[Interface]
Address = 10.77.0.1/24
PrivateKey = <平台机私钥>

[Peer]
PublicKey = <沙箱机公钥>
Endpoint = 198.51.100.20:<wg-port>
AllowedIPs = 10.77.0.2/32
# 平台机不开 WireGuard 端口，靠它每 25 秒主动发包维持隧道和 conntrack 状态
PersistentKeepalive = 25
```

沙箱机 `/etc/wireguard/wg0.conf` 示例：

```ini
[Interface]
Address = 10.77.0.2/24
ListenPort = <wg-port>
PrivateKey = <沙箱机私钥>

[Peer]
PublicKey = <平台机公钥>
AllowedIPs = 10.77.0.1/32
```

### 3.2 沙箱代理

- **只监听 WireGuard 地址**（`10.77.0.2`），不监听 `0.0.0.0`、不监听公网 IP。
- 平台 → 代理的每个请求都带共享密钥（不少于 32 字符，例如 `openssl rand -hex 32`），代理用常量时间比较校验；密钥两边各存在 root 只读的 env 文件里。
- 代理通过本机 unix socket 操作 Docker。**Docker API 永远不对外暴露**（不开 2375/2376，也不经 WireGuard 暴露）——拿到 Docker API 等于拿到沙箱机 root。
- 代理只提供有限的操作（起、停、查状态、转发），不提供"执行任意 docker 命令"。

### 3.3 沙箱网络（依赖 #123 / PR #124）

- 沙箱网络是 Docker `internal` 网络，固定网桥名和网段（PR #124：`NCS_DOCKER_BRIDGE`，默认 `ncs-sbx0`；`NCS_DOCKER_SUBNET`，默认 `172.30.250.0/24`），防火墙规则按它们写。
- 沙箱**只能**连网关 relay 端口（打开出站代理时再加上代理端口）；宿主机上的 sshd、代理管理端口、其它服务一律不通。
- 沙箱里没有 DNS，也没有直连公网的路由。
- 用 `deploy/firewall/check-sandbox-isolation.sh`（PR #124）在真机上验证：relay 端口 open，其它端口 timeout；带 `NCS_EGRESS_PORT` 时还验证出站代理（见 3.5）。

### 3.4 firewalld zone（AlmaLinux / RHEL 系）

| zone | 绑定 | target | 放行 |
|---|---|---|---|
| `public` | 公网网卡 | default | SSH；沙箱机另加 WireGuard UDP 端口（rich rule 限定来源为平台机公网 IP）；平台机加 80/443 |
| `wg` | `wg0` | DROP | 沙箱机：只允许来源 `10.77.0.1` 访问代理端口；平台机：只允许来源 `10.77.0.2` 访问网关端口 |
| `ncs-sandbox` | 来源 = 沙箱网段 | DROP | 只开 relay 端口；打开出站代理时再开代理端口（`ncs-sandbox-firewalld.sh`，带 `NCS_EGRESS_PORT`） |

注意：不要把沙箱网段或 `wg0` 放进 `trusted` zone；Docker 默认的 `docker` zone 是 `ACCEPT`，正是 #123 的成因。

### 3.5 出站代理（沙箱上网，默认关）

> **生产环境暂时不要打开。** 打开的前提是 [#134](https://github.com/mbaiagent/nanocode/issues/134)：现在沙箱的环境变量里还有网关令牌（`NANOCODE_API_KEY`）和 `NANOCODE_WEB_TOKEN`，任何 bash 子进程都读得到。沙箱断网时这是主要的缓解手段；能上网之后，一次提示词注入（`env` + `curl`）就能把它们带出去。#134 落地（沙箱里只有短期、只能访问中继的令牌）之前，`NCS_EGRESS_PROXY` 保持 `0`，只在测试环境打开。相关：[#135](https://github.com/mbaiagent/nanocode/issues/135)（沙箱里的 agent 能改审计日志，出了事查不清）、[#136](https://github.com/mbaiagent/nanocode/issues/136)（读 `.env`、`*.key`、`id_*` 这类敏感文件默认应该先确认）。

**架构**：

```
沙箱容器 ──HTTP(S)_PROXY──▶ 出站代理 172.30.250.1:3128（沙箱代理进程里，沙箱机网桥地址）──▶ 公网 80/443
沙箱容器 ──NO_PROXY──────▶ 网关 relay 172.30.250.1:8701 ──WireGuard──▶ 平台机网关 ──▶ 模型 API
```

- 代理是沙箱代理进程里的一个线程（`server/nanocode_server/egress.py`），只用标准库，和 relay 共用连接上限和超时的逻辑。不选 tinyproxy，是因为它只能按域名和客户端做 ACL，解析完之后没法按目标 IP 拦，做不到下面的 SSRF 和重绑定检查。squid 倒是能做（dst ACL），但常驻就要几十 MB，在 1 GB 的沙箱机上不划算，而且多一个要单独升级、单独配防火墙的组件。
- **只做两件事**：`CONNECT host:port`（HTTPS 隧道，代理看不到内容，证书仍由沙箱里的客户端按域名校验），以及绝对地址的明文 HTTP 转发（`GET http://…`）。目标端口必须在 `NCS_EGRESS_ALLOWED_PORTS` 里（默认 `80,443`），其它协议和端口一律 403。
- **DNS 由代理解析**，沙箱里仍然没有 DNS，也就没法把 DNS 当隧道用。沙箱代理会往沙箱里注入 `NANOCODE_EGRESS_PROXY=1`，告诉 `web_fetch` 和 `web_search` 不要先在本地解析域名，交给代理去检查。URL 里直接写 IP 的，沙箱里会先拦一次。
- 沙箱代理会往每个沙箱容器注入这些环境变量：`HTTP_PROXY`、`HTTPS_PROXY`、`http_proxy`、`https_proxy`（大小写都给，curl 只认小写），以及 `NO_PROXY`/`no_proxy`，内容是 `localhost`、`127.0.0.1`、`::1`、relay 地址，再加上沙箱策略里网关地址的主机名。

**SSRF 拦截表**：域名解析出的**每一个**地址都要过检查，有一个不合格就整个拒绝。混合应答是重绑定的常见手法。

| 拦住的目标 | 网段 |
|---|---|
| 私网（RFC 1918） | `10.0.0.0/8`、`172.16.0.0/12`、`192.168.0.0/16` |
| CGNAT | `100.64.0.0/10` |
| 本机 | `127.0.0.0/8`、`::1`、`0.0.0.0/8`、`::` |
| 链路本地，含云元数据 `169.254.169.254` | `169.254.0.0/16`、`fe80::/10` |
| IPv6 ULA | `fc00::/7` |
| 组播、保留、基准测试网段等 | `224.0.0.0/3`、`ff00::/8`、`192.0.0.0/24`、`198.18.0.0/15`，以及其它所有 `is_global` 为假的地址 |
| 嵌着 IPv4 的 IPv6 | `::ffff:a.b.c.d`、NAT64 `64:ff9b::/96`、6to4 `2002::/16`，按里面的 IPv4 判断 |
| 沙箱网段 | `NCS_DOCKER_SUBNET`（自动加入） |
| WireGuard 网段等 | `NCS_EGRESS_BLOCK_NETWORKS`（建议写上 `10.77.0.0/24`，私网本来就拦，写上是防以后换成别的网段） |
| 沙箱机自己的地址，含公网 IP | 从 `/proc/net/fib_trie` 和 `/proc/net/if_inet6` 读，每分钟刷新一次。经 NAT 或浮动 IP 进来、本机网卡上看不到的公网 IP，要写进 `NCS_EGRESS_BLOCK_NETWORKS` |

**防 DNS 重绑定**：每个请求只解析一次，检查过哪个 IP 就连哪个 IP，不交给系统再解析一次。连上之后再按对端地址复核一遍。明文 HTTP 的 `Host` 头由代理按 URL 里的域名重写，同时去掉 `Proxy-Authorization` 等逐跳头，并改成 `Connection: close`，一条连接只做一个请求。

**名额和时限**，按 #126 审查时 relay 的做法：

| 变量 | 默认 | 作用 |
|---|---|---|
| `NCS_EGRESS_MAX_CONNECTIONS` | `64` | 全局并发连接上限 |
| `NCS_EGRESS_PER_SANDBOX` | `8` | 每个沙箱（按来源 IP）的上限，超出直接回 503，一个沙箱占不满别人的名额 |
| `NCS_EGRESS_IDLE_TIMEOUT` | `120` | 两个方向都没有数据超过这么多秒就断开；写不出去也按这个超时 |
| （固定） | 10 秒 / 16 KB | 请求头读取有总时限（防慢速请求头），请求头最大 16 KB；连接上游超时 10 秒；一头 EOF 后另一头停顿 30 秒断开 |

**全部变量**（沙箱机 `deploy/sandbox-host/.env`）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `NCS_EGRESS_PROXY` | `0` | 开关。打开前先放行防火墙端口；生产环境等 #134 |
| `NCS_EGRESS_BIND` | 空 | 监听地址，空就和 relay 用同一个（沙箱网桥地址）。不能是 `0.0.0.0` / `::` |
| `NCS_EGRESS_PORT` | `3128` | 监听端口，不能和 `NCS_GATEWAY_PORT` 相同 |
| `NCS_EGRESS_ALLOWED_PORTS` | `80,443` | 允许的目标端口，CONNECT 和明文 HTTP 共用 |
| `NCS_EGRESS_ALLOW_DOMAINS` | 空 | 非空时只放行这些域名，按后缀匹配（`example.com` 也匹配 `a.example.com`，`*.` 前缀可写可不写）。直接写 IP 的一律拒绝 |
| `NCS_EGRESS_DENY_DOMAINS` | 空 | 一律拒绝这些域名，先于 allow 判断 |
| `NCS_EGRESS_BLOCK_NETWORKS` | 空 | 额外拦住的网段，逗号分隔 |
| `NCS_EGRESS_MAX_CONNECTIONS` / `NCS_EGRESS_PER_SANDBOX` / `NCS_EGRESS_IDLE_TIMEOUT` | `64` / `8` / `120` | 见上表 |

沙箱里由沙箱代理注入、不用手配的变量：`HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY`（大小写两套），以及 `NANOCODE_EGRESS_PROXY=1`。

**访问日志**：每条连接写一行 JSON 到沙箱代理的标准输出（`docker compose logs agent`；compose 里给日志限了 10 MB × 3），字段有 `egress`（会话编号）、`src`、`method`（`CONNECT` / `HTTP`）、`host`、`port`、`ip`、`result`（`ok` / `denied:…` / `error:…`）、`up`/`down`（字节数）和 `ms`。**URL 路径、查询串、请求头、请求体和响应都不记**，因为路径和头里常带令牌。

**打开**（沙箱机，在测试环境；生产环境等 #134）：

```bash
cd deploy/sandbox-host
# .env：NCS_EGRESS_PROXY=1，NCS_EGRESS_BLOCK_NETWORKS=10.77.0.0/24，按需填 NCS_EGRESS_ALLOW_DOMAINS
cd ../firewall
NCS_DOCKER_SUBNET=172.30.250.0/24 NCS_GATEWAY_PORT=8701 NCS_EGRESS_PORT=3128 ./ncs-sandbox-firewalld.sh
#   没有 firewalld：NCS_DOCKER_BRIDGE=ncs-sbx0 ... NCS_EGRESS_PORT=3128 ./ncs-sandbox-nftables.sh
cd ../sandbox-host && docker compose up -d --build agent
docker compose logs agent | grep 出站代理            # 启动行里应显示 出站代理 http://172.30.250.1:3128
```

已经在跑的沙箱拿不到新的环境变量，要停掉会话再开，或者重启平台。平台机这边不用改配置。

**验证**：

```bash
cd deploy/firewall
NCS_GATEWAY_PORT=8701 NCS_EGRESS_PORT=3128 ./check-sandbox-isolation.sh
```

要看到这几项：relay 和代理端口 `open`；其它宿主机端口 `timeout`；沙箱里 DNS 解析不了；直连公网连不上；经代理能取到 `PROBE_URL`（默认 `https://example.com/`）；`CONNECT 10.0.0.1:443`、`169.254.169.254:80`、`127.0.0.1:443`、`<网桥地址>:22`、`example.com:22` 和明文 `GET http://169.254.169.254/` 都回 `403`。最后在面板里让 Agent 调一次 `web_fetch`，再看 `docker compose logs agent` 里有对应的一行。

**威胁说明**（打开之后还剩下的风险）：

- **数据外传**：能上网就能往外发数据，这是允许上网的代价，域名白名单也只能缩小范围。所以前提是沙箱里没有值钱的东西（#134）。日志能事后追查发到了哪个域名、发了多少字节，但看不到内容。
- **滥用出口 IP**：别的用户可以拿沙箱机的公网 IP 去爬站、刷接口。只放 80/443、每个沙箱 8 条连接，挡得住扫端口和发邮件（25），挡不住 HTTP 层的滥用。必要时用 `NCS_EGRESS_ALLOW_DOMAINS` 收紧成包仓库加代码托管平台。
- **沙箱机自己的 DNS**：代理用的是系统解析器。宿主机要是装了 fake-ip 之类的透明代理（解析结果落在 `198.18.0.0/15`），所有请求都会被当作内网拒绝，这是安全的那一侧。
- **CONNECT 里跑的不一定是 TLS**：到 443 的隧道里装什么协议，代理不检查。端口限制和地址检查照样生效。
- 单机 Docker 和 Kubernetes 模式还没有这个代理，那里的沙箱仍然完全断网。

## 四、托管 PostgreSQL（以 Neon 免费版为例）

- **用 pooled 连接地址**（主机名里带 `-pooler` 的那个），控制面连接数多时不会顶满。
- **缩到零**：闲置约 5 分钟后计算节点自动休眠，下一次请求有冷启动（通常零点几秒到一两秒）。数据不会丢，只是第一个请求慢一点。
- **不要做保活 ping**：免费版每月计算时长有限（约 100 CU·小时），每几分钟 ping 一次会让节点一直醒着，几天就用完额度，额度用完库就停了。接受冷启动更划算。
- 平台连接数据库要能扛住冷启动：连接超时设宽一点、首次失败重试。（接入时要在代码里核实现有的重试/连接池行为，不够就补。）
- **老版本 libpq（< 14，例如 AlmaLinux 8 自带的 psql 13）**：
  - 不支持 SNI 方式识别 endpoint，要在连接串里加 `options=endpoint%3D<endpoint-id>`；
  - 不支持 `channel_binding=require`，要去掉这个参数（保留 `sslmode=require`）。
  - 平台容器里用 psycopg 自带的较新 libpq 时一般没这个问题；主要影响在宿主机上用 `psql` 手动排查的时候。

示例（全部是占位符）：

```bash
# 新版 libpq / psycopg
NCS_DATABASE_URL=postgresql://<user>:<password>@<db-host>-pooler.<region>.<provider-domain>/<dbname>?sslmode=require&channel_binding=require
# 老版 psql 13 手动排查
psql "postgresql://<user>:<password>@<db-host>-pooler.<region>.<provider-domain>/<dbname>?sslmode=require&options=endpoint%3D<endpoint-id>"
```

## 五、托管 Redis 兼容服务（以 Aiven Valkey 免费版为例）

- 只提供 TLS：连接串是 `rediss://`（两个 s）。
- 免费版**不能选机房**。如果分到的机房离平台机远，每次 Redis 往返都多几十毫秒；限流计数这类用法能接受，不要在热路径上做大量串行调用。
- **长时间不活跃会被关机**（关机前会发邮件通知），关了要去控制台手动开。所以它**需要保活**：平台机上一个 systemd timer，每 6 小时 `PING` 一次；失败时除了写日志，还要通过 `OnFailure=` **触发告警**，否则保活坏了要等 Redis 被关机才会发现。

> systemd unit 文件只支持整行注释（行首 `#` 或 `;`），**不支持行尾注释**：`#` 后面的内容会被当成值的一部分。下面的示例注释都单独占一行，照抄时不要挪到行尾。

```ini
# /etc/systemd/system/ncs-redis-keepalive.service
[Unit]
Description=Keep managed Redis/Valkey awake
# 失败（PING 不通、env 文件缺失等）时触发告警
OnFailure=ncs-redis-keepalive-alert.service

[Service]
Type=oneshot
# 内含 NCS_REDIS_URL，root 只读 (chmod 600)
EnvironmentFile=/etc/nanocode/keepalive.env
ExecStart=/bin/sh -c 'redis-cli -u "$NCS_REDIS_URL" --tls PING | grep -q PONG'
```

```ini
# /etc/systemd/system/ncs-redis-keepalive.timer
[Unit]
Description=Ping managed Redis/Valkey every 6h

[Timer]
OnBootSec=10min
OnUnitActiveSec=6h
Persistent=true

[Install]
WantedBy=timers.target
```

```ini
# /etc/systemd/system/ncs-redis-keepalive-alert.service
[Unit]
Description=Alert: managed Redis/Valkey keepalive failed

[Service]
Type=oneshot
# 内含 NCS_ALERT_WEBHOOK_URL（你的告警 webhook 地址），root 只读 (chmod 600)
EnvironmentFile=/etc/nanocode/alert.env
ExecStart=/bin/sh -c 'curl -fsS -m 10 -H "Content-Type: application/json" -d "{\"text\":\"ncs-redis-keepalive failed on %H\"}" "$NCS_ALERT_WEBHOOK_URL"'
```

告警渠道可以换成邮件（如 `mail -s ... <收件人>`）或任何 webhook；地址、收件人都放在 root 只读的 env 文件里，不写进 unit。

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now ncs-redis-keepalive.timer
sudo systemd-analyze verify /etc/systemd/system/ncs-redis-keepalive*.service   # 检查 unit 语法
sudo systemctl start ncs-redis-keepalive-alert.service                         # 手动发一条测试告警
journalctl -u ncs-redis-keepalive.service --since -1d   # 看最近的结果
```

> 不要把带密码的连接串写在 unit 文件或命令行参数里（`ps` 能看到），放在 root 只读的 EnvironmentFile 里。

## 六、和单机 compose 的取舍

| | 单机 compose | 分布式 |
|---|---|---|
| 部署复杂度 | 一条 `docker compose up` | 两台机器 + WireGuard + 两个托管账号 |
| 内存 | 数据库、平台、沙箱挤一台 | 平台机、沙箱机各自宽松 |
| 隔离 | 沙箱和数据库同机 | 沙箱机被打穿也碰不到数据库和平台密钥 |
| 延迟 | 本机 | 页面请求经 WireGuard 多几十毫秒；模型调用多一次转发；Redis 视机房而定 |
| 故障点 | 一台机器 | WireGuard、沙箱机、两个托管服务 |
| 扩展 | 加不了机器 | 可加沙箱机、平台多副本（`NCS_REPLICAS` 需要 PG + Redis） |

**单机 compose 保留**：本地开发、演示、分布式出问题时的回退方案都用它。

## 七、故障模式

| 现象 | 原因 | 影响 | 处理 |
|---|---|---|---|
| 新建会话失败 / 会话页打不开 | WireGuard 断了，或沙箱机 / 代理挂了；模型调用偶发超时多半是平台机 `[Peer]` 没配 `PersistentKeepalive` | 起不了新沙箱，已有会话的页面和模型调用都断 | `wg show` 看最近握手；`systemctl status` 看代理；控制面应返回明确的错误，而不是一直转圈 |
| 第一个请求慢 1～2 秒 | Neon 缩到零后冷启动 | 只慢一下 | 预期行为，不处理 |
| 数据库连不上（持续） | 免费额度用完、凭据轮换、服务端故障 | 登录、建会话都失败 | 看托管控制台；必要时临时切回单机 compose 自带的 PostgreSQL |
| 限流 / 事件异常，Redis 报连接错误 | Aiven 因不活跃关机，或保活 timer 停了 | 限流计数失效 | 保活失败应先收到 `OnFailure` 告警；控制台开机；查 `ncs-redis-keepalive` 日志 |
| 沙箱能连到宿主机其它端口 | 防火墙没配或被 Docker 重置 | 安全事故 | 重跑 `ncs-sandbox-firewalld.sh` 和 `check-sandbox-isolation.sh` |
| `web_fetch` 报 `Tunnel connection failed: 403 …` / `Could not resolve host` | 前者：代理拒绝了，原因写在状态行里（内网地址、端口、名单）。后者：出站代理没开（`NCS_EGRESS_PROXY=0`），或者沙箱是开代理之前启动的 | 只影响上网 | 看 `docker compose logs agent` 里的 `egress` 行；开代理后重启会话 |

## 八、1 GB 小机器的内存预算（估算）

以下是量级估算，上线前用 `free -m`、`docker stats` 实测替换。两台都建议开约 1～2 GB swap 兜底。

**平台机（1 GB）**

| 项 | 约占用 |
|---|---|
| 系统 + sshd + nginx | 150～250 MB |
| 控制面 + 网关（一个 Python 进程） | 100～200 MB |
| 其它已有服务 | 视情况 |
| 余量 | ≥ 200 MB |

数据库和 Redis 托管出去后，平台机不再需要 PostgreSQL（数百 MB）和 Redis 的内存。

**沙箱机（1 GB）**

| 项 | 约占用 |
|---|---|
| 系统 + sshd + Docker 引擎 | 200～300 MB |
| 沙箱代理（含 relay；出站代理空闲时不额外占内存，64 条并发隧道约多 5 MB，本地实测） | 50～100 MB |
| 每个沙箱容器（含 Agent 进程，`--memory` 限制） | 250～400 MB |

→ 同时 **1～2 个会话**。用并发上限（全局 / 每用户）卡住，超出排队或报"满了"，不要让 OOM killer 随机杀进程。可调的资源参数见 #119。

## 九、上线清单

- [ ] 1. PR #124（#123 宿主机隔离）合并；远程沙箱 PR 合并。
- [ ] 2. 两台机器：系统更新、SSH 只允许密钥登录、开 swap。
- [ ] 3. 沙箱机：装 Docker，构建沙箱镜像；跑 `ncs-sandbox-firewalld.sh`。
- [ ] 4. 两台机器装 WireGuard，生成各自密钥对，配 `wg0`（`10.77.0.1` / `10.77.0.2`），`AllowedIPs` 只写对端 `/32`，平台机 `[Peer]` 加 `PersistentKeepalive = 25`；沙箱机防火墙只对平台机 IP 开 WireGuard 端口；`wg show` 确认握手。
- [ ] 5. 配 firewalld `wg` zone（DROP，只放行对端访问指定端口）。
- [ ] 6. 生成共享密钥（`openssl rand -hex 32`），分别写进两台机器 root 只读的 env 文件。
- [ ] 7. 沙箱机启动沙箱代理（只听 `10.77.0.2`），用 systemd 管理、开机自启。
- [ ] 8. 托管 Postgres：建项目和数据库，拿 pooled 连接串；托管 Valkey：建服务，拿 `rediss://` 连接串。都只写进平台机 root 只读的 `.env`，不进仓库、不进聊天、不进日志。
- [ ] 9. 平台机 `.env`：`NCS_DATABASE_URL`、`NCS_REDIS_URL`、`NCS_LAUNCHER=remote` 以及代理地址和共享密钥（具体变量名以远程沙箱 PR 为准）；启动平台。
- [ ] 10. 配 Redis 保活 timer 和 `OnFailure=` 告警 unit；`systemd-analyze verify` 通过，手动触发一次告警**确认能收到**；**不要**给 Postgres 配保活。
- [ ] 11. 验证：`curl http://127.0.0.1:8700/healthz`；登录、建会话、打开 `/s/<id>/`、调一次模型；沙箱里跑 `check-sandbox-isolation.sh`；从公网扫沙箱机，确认只开 SSH 和 WireGuard。
- [ ] 12. （可选，**等 #134 落地**）打开出站代理：`NCS_EGRESS_PROXY=1`，防火墙脚本带 `NCS_EGRESS_PORT`，用 `check-sandbox-isolation.sh` 验证，见 3.5。
- [ ] 13. 演练回退：停掉 WireGuard，确认控制面报错清楚；恢复后新会话正常。

## 十、隐私提醒（本仓库是公开的）

提交文档、配置、日志之前检查：没有真实 IP、域名、托管服务的主机名 / endpoint ID / 项目 ID、账号邮箱、密码和令牌。示例一律用本文开头的占位符（RFC 5737 文档网段、`example.com`）。提交前可以跑 `gitleaks` 扫一遍。
