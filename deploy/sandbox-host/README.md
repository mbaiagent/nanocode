# 沙箱放在另一台机器上（NCS_LAUNCHER=remote）

平台机器（控制面 + 模型网关 + PostgreSQL + Redis，`deploy/compose`）和沙箱主机（只跑沙箱）分开。适合平台机器内存小、沙箱想单独扩容的情况。

```
浏览器 ─HTTPS─▶ 平台机器 nginx/Caddy ─▶ 控制面 :8700 ──WireGuard──▶ 沙箱主机 代理 10.77.0.2:8790 ─▶ 沙箱容器 :8765
                                    模型网关 10.77.0.1:8701 ◀──WireGuard── 代理的网关中继 172.30.250.1:8701 ◀── 沙箱容器
                                    公网 80/443 ◀── 出站代理 172.30.250.1:3128（可选，默认关）◀──HTTP(S)_PROXY── 沙箱容器
```

- **沙箱代理**（`python -m nanocode_server sandbox-agent`）在沙箱主机上用 Docker 启动器起停容器，管理接口要带共用密钥 `NCS_REMOTE_AGENT_SECRET`（常量时间比较）。
- 控制面的 `/s/<id>/` 转发到代理的 `/sbx/<id>/...`，代理再转给容器。凭的是每个沙箱自己的 web_token（和本机 Docker 时一样），代理密钥不能用来访问沙箱。
- 沙箱只能连沙箱主机网桥上的一个端口（网关中继），中继转到平台机器的模型网关。**沙箱到不了 PostgreSQL / Redis / 控制面**，也出不了外网。
- 可选的**出站代理**（`NCS_EGRESS_PROXY=1`，默认关）：沙箱经它访问公网的 HTTP/HTTPS，内网、元数据和本机地址仍然到不了。见下面的「出站代理」一节。生产打开前，先确认镜像已经包含 [#134](https://github.com/mbaiagent/nanocode/issues/134) 的修复（PR #146）。

## 安全要点

| 风险 | 怎么挡 |
|---|---|
| Docker API 暴露 | 不暴露。Docker 只听本机 socket，远程只能调代理那几个接口 |
| 代理接口暴露 | 只监听 WireGuard 地址（`NCS_AGENT_BIND`，配置检查拒绝 0.0.0.0），firewalld 只允许平台的隧道地址访问 |
| 沙箱访问宿主机 / 隧道 | `deploy/firewall/ncs-sandbox-firewalld.sh`：沙箱网段只放行中继端口（#123）。代理接口 8790、sshd 都连不上 |
| 沙箱访问数据库 | PG / Redis 只听平台机器的 127.0.0.1；沙箱的流量只能经中继到网关端口 |
| 密钥在公网明文传输 | 代理接口是明文 HTTP，**只走 WireGuard**；公网上只多开一个 WireGuard 的 UDP 端口，只允许对端 IP |
| 沙箱出外网 | internal 网络，同单机；没有 DNS。打开出站代理后只能经代理访问 80/443，代理自己解析域名并拦内网 |
| 经出站代理打内网（SSRF）/ DNS 重绑定 | 解析出的每个地址都要是公网，私网、CGNAT、本机、链路本地（含 `169.254.169.254`）、ULA、沙箱网段、`NCS_EGRESS_BLOCK_NETWORKS`、本机自己的地址一律拒绝；检查过哪个 IP 就连哪个，连上后再复核 |
| 一个沙箱占满出站代理 | 每个沙箱 `NCS_EGRESS_PER_SANDBOX`（8），全局 `NCS_EGRESS_MAX_CONNECTIONS`（64），空闲 `NCS_EGRESS_IDLE_TIMEOUT`（120 秒），请求头 10 秒内要发完 |
| 能上网后把沙箱里的令牌外传 | #134（PR #146）：工具子进程拿不到令牌，Web 进程不可 dump。残留风险是同进程的 Python 工具和读得到的文件，所以出站代理仍然默认关。相关 #135、#136（PR #147） |
| 一个沙箱占满网关中继 | 中继每条连接有空闲超时（`NCS_AGENT_RELAY_IDLE_TIMEOUT`，默认 300 秒）和半关闭时限；全局上限 `NCS_AGENT_RELAY_MAX_CONNECTIONS`（128），每个沙箱上限 `NCS_AGENT_RELAY_PER_SANDBOX`（16） |

## 资源（1 vCPU / 1 GB 的沙箱主机）

Docker 守护进程约 80–100 MB，代理约 30–40 MB（出站代理在同一个进程里，空闲时不额外占内存，64 条并发隧道约多 5 MB）。每个沙箱 `NCS_SANDBOX_MEMORY=384Mi`、`NCS_SANDBOX_TMPFS=96m`（/tmp 算在沙箱内存里），能同时跑 1–2 个；平台那边设 `NCS_MAX_SANDBOXES=2`。建议开 1 GB swap。延迟：两台机器之间每个页面请求多一个来回，模型调用也多一个来回（几十毫秒），事件流不受影响。

## 步骤

下面用 `<平台IP>` / `<沙箱主机IP>` 表示两台机器的公网 IP，隧道地址平台 `10.77.0.1`、沙箱主机 `10.77.0.2`，WireGuard 端口 `51820/udp`。**只有沙箱主机开 WireGuard 端口**；平台机器主动连出，靠 `PersistentKeepalive` 维持隧道，不用开端口（和 [docs/distributed-deploy.md 3.1](../../docs/distributed-deploy.md#31-wireguard) 一致，#132 第 4 点）。

### 1. 沙箱主机装 Docker CE（AlmaLinux / Rocky 8、9）

```bash
dnf -y install dnf-plugins-core
dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
# Alma 8 自带的 podman / buildah / runc 和 containerd.io 冲突时加 --allowerasing
dnf -y install --allowerasing docker-ce docker-ce-cli containerd.io docker-compose-plugin
systemctl enable --now docker
```

### 2. 两台都装 WireGuard

```bash
dnf -y install wireguard-tools      # Alma 8 在 EPEL；Alma 9 在 AppStream
modprobe wireguard && echo ok       # Alma 9 内核自带
```

Alma 8 上 `modprobe wireguard` 失败时：先装和当前内核同版本的 `kernel-modules-extra`（`dnf -y install kernel-modules-extra-$(uname -r)`）再试；还不行就用 ELRepo 的 `kmod-wireguard`。实在装不上，可以退回用 autossh 建 SSH 隧道（把代理端口和网关端口互相转发），但 WireGuard 更省事也更稳。

各自生成密钥（私钥不要贴到任何地方）：

```bash
umask 077 && wg genkey | tee /etc/wireguard/private.key | wg pubkey > /etc/wireguard/public.key
```

平台机器 `/etc/wireguard/wg0.conf`：

```ini
[Interface]
Address = 10.77.0.1/24
PostUp = wg set %i private-key /etc/wireguard/private.key

[Peer]
PublicKey = <沙箱主机 public.key 的内容>
AllowedIPs = 10.77.0.2/32
Endpoint = <沙箱主机IP>:51820
# 平台机器不开 WireGuard 端口：靠它每 25 秒主动发包，维持隧道和防火墙的连接状态
PersistentKeepalive = 25
```

沙箱主机 `/etc/wireguard/wg0.conf`：

```ini
[Interface]
Address = 10.77.0.2/24
ListenPort = 51820
PostUp = wg set %i private-key /etc/wireguard/private.key

[Peer]
PublicKey = <平台机器 public.key 的内容>
AllowedIPs = 10.77.0.1/32
# 不写 Endpoint / PersistentKeepalive：平台机器没有监听端口，由它主动连过来
```

两台都 `systemctl enable --now wg-quick@wg0`，然后 `ping 10.77.0.2` / `ping 10.77.0.1` 互通。

### 3. firewalld

**沙箱主机的公网 zone**：只允许平台机器的 IP 连 WireGuard 端口（平台机器不开端口，不用加这条）。

```bash
# 只在沙箱主机上执行
firewall-cmd --permanent --zone=public --add-rich-rule='rule family="ipv4" source address="<平台IP>/32" port port="51820" protocol="udp" accept'
```

**隧道 zone**：按来源地址绑定（同 #123 的思路，来源优先于网卡），target=DROP，只开需要的端口。

```bash
# 沙箱主机：只允许平台 10.77.0.1 访问代理 8790
firewall-cmd --permanent --new-zone=ncs-wg
firewall-cmd --permanent --zone=ncs-wg --set-target=DROP
firewall-cmd --permanent --zone=ncs-wg --add-source=10.77.0.1/32
firewall-cmd --permanent --zone=ncs-wg --add-port=8790/tcp

# 平台机器：只允许沙箱主机 10.77.0.2 访问网关 8701
firewall-cmd --permanent --new-zone=ncs-wg
firewall-cmd --permanent --zone=ncs-wg --set-target=DROP
firewall-cmd --permanent --zone=ncs-wg --add-source=10.77.0.2/32
firewall-cmd --permanent --zone=ncs-wg --add-port=8701/tcp

firewall-cmd --reload
```

注意：`firewall-cmd --list-all` 里 `public` zone 的 `interfaces:` 为空时，eth0 走的是默认 zone（`firewall-cmd --get-default-zone`），确认它是 `public`，上面的 rich rule 才在 eth0 上生效。

**沙箱网段**（沙箱主机上，代理第一次启动、建好沙箱网络之后）：

```bash
cd deploy/firewall
NCS_DOCKER_SUBNET=172.30.250.0/24 NCS_GATEWAY_PORT=8701 ./ncs-sandbox-firewalld.sh
```

### 4. 共用密钥

在沙箱主机上生成，直接写进两边的 `.env`，不要在终端或聊天里显示：

```bash
cd deploy/sandbox-host && cp .env.example .env && chmod 600 .env
secret=$(openssl rand -hex 32)
sed -i "s/^NCS_REMOTE_AGENT_SECRET=.*/NCS_REMOTE_AGENT_SECRET=${secret}/" .env
# 平台机器的 deploy/compose/.env 里写同一个值（经 SSH 管道，不落终端）：
printf '%s\n' "${secret}" | ssh root@<平台IP> 'read -r s; cd /path/to/nanocode/deploy/compose && sed -i "s/^NCS_REMOTE_AGENT_SECRET=.*/NCS_REMOTE_AGENT_SECRET=${s}/" .env'
unset secret
```

### 5. 启动沙箱代理（沙箱主机）

```bash
cd deploy/sandbox-host
# 编辑 .env：NCS_AGENT_BIND、NCS_AGENT_UPSTREAM_GATEWAY、资源设置
mkdir -p /srv/nanocode
docker compose up -d --build
docker compose ps                     # agent 应变为 healthy
# 验证沙箱网段只能到中继端口（中继在跑，应显示 8701 open，其它 timeout）
../firewall/check-sandbox-isolation.sh
```

### 6. 平台切到远程沙箱（平台机器的 `deploy/compose/.env`）

```bash
NCS_LAUNCHER=remote
NCS_REMOTE_AGENT_URL=http://10.77.0.2:8790
NCS_REMOTE_AGENT_SECRET=<第 4 步写进去的值>
NCS_REMOTE_GATEWAY_URL=http://172.30.250.1:8701   # 可省略：省略时启动时向代理查询；写上则代理暂时不在也能启动
NCS_GATEWAY_HOST=10.77.0.1                       # 网关只监听隧道地址
NCS_MAX_SANDBOXES=2
```

然后 `docker compose up -d`。验证：

```bash
curl -s http://127.0.0.1:8700/healthz                       # {"ok": true, "launcher": "remote", "isolated": true}
curl -s http://10.77.0.2:8790/healthz                       # 平台机器上能连到代理
```

登录面板新建会话，能打开、能对话、换成自己的 Key 后能调用模型，就通了。平台机器上不再跑沙箱，`deploy/firewall` 那一步在平台机器上可以不做。目前 compose 在 remote 模式下仍会在平台机器上构建一次沙箱镜像（不会运行），并且仍挂载 `docker.sock`（[#132](https://github.com/mbaiagent/nanocode/issues/132)），可以用本机 override 去掉，见 [docs/deployment-topology.md](../../docs/deployment-topology.md#托管-postgres--redis)。

## 出站代理（可选，默认关）

沙箱默认完全断网，`web_search` 和 `web_fetch` 会报 `Could not resolve host`。打开出站代理后，沙箱里的 `web_fetch`、`web_search`、`pip`、`git`、`curl` 都经它访问公网 HTTP/HTTPS，模型调用仍然走网关中继（`NO_PROXY`）。设计、SSRF 拦截表、全部变量和威胁说明见 [docs/distributed-deploy.md 3.5](../../docs/distributed-deploy.md#35-出站代理沙箱上网默认关)。

> 默认关。生产打开前，先确认沙箱镜像已经包含 #134 的修复（PR #146，工具子进程拿不到令牌），并接受残留风险，见 distributed-deploy.md 3.5。

在测试环境打开：

```bash
cd deploy/sandbox-host
# .env 里改：
#   NCS_EGRESS_PROXY=1
#   NCS_EGRESS_BLOCK_NETWORKS=10.77.0.0/24      # WireGuard 网段；经 NAT 进来、网卡上看不到的公网 IP 也写上
#   NCS_EGRESS_ALLOW_DOMAINS=                   # 想收紧时写，例如 pypi.org,files.pythonhosted.org,github.com
cd ../firewall
NCS_DOCKER_SUBNET=172.30.250.0/24 NCS_GATEWAY_PORT=8701 NCS_EGRESS_PORT=3128 ./ncs-sandbox-firewalld.sh
cd ../sandbox-host
docker compose up -d --build agent
docker compose logs agent | grep 出站代理      # 应显示 出站代理 http://172.30.250.1:3128
NCS_GATEWAY_PORT=8701 NCS_EGRESS_PORT=3128 ../firewall/check-sandbox-isolation.sh
```

打开前已经在跑的沙箱没有代理环境变量，要停掉会话重开。平台机器这边不用改。访问日志在 `docker compose logs agent` 里，每条连接一行 JSON（`{"egress": "<会话>", "host": …, "up": …, "down": …}`），不含路径和请求头。要关掉，把 `NCS_EGRESS_PROXY=0` 改回去，`docker compose up -d agent`，再不带 `NCS_EGRESS_PORT` 重跑防火墙脚本，把端口关上。

## 运维

- **升级代理**：配好 CD 后由 [docs/cd.md](../../docs/cd.md) 的流程自动部署，手动升级只作兜底：`docker compose up -d --build agent`。代理重启不停沙箱；重启后转发会按容器名找回正在跑的沙箱（令牌由沙箱自己核对）。
- **平台重启**：控制面退出时让代理停掉全部沙箱，启动时对账，清掉数据库里已经没有的会话的沙箱。
- **工作区**在沙箱主机的 `/srv/nanocode/sandboxes/<会话>/`。平台开了工作区快照（`NCS_OBJECT_STORE=s3`，[distributed-deploy.md 第十节](../../docs/distributed-deploy.md#十工作区快照s3-兼容对象存储)）时，代理在停止 / 回收 / 删除后把它打包上传，本机没有时恢复：**这台机器上不放对象存储密钥**，代理只用控制面给的单个对象的预签名地址（代理是 host 网络，这台机器本身要能出站访问对象存储的 https 地址；沙箱容器仍然没有外网）。没开快照时，备份要备这台机器。
- **升级顺序**（快照接口是新加的）：先 `docker compose up -d --build agent`，再升级平台。
- **代理连不上**：控制面把会话标成已停止，用户刷新时重试启动；`docker compose logs agent` 看原因。
