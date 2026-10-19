# 单机部署（docker compose）

一台 Linux 服务器上跑起整个平台：HTTPS 反向代理（可选自带的 Caddy，或机器上已有的 nginx）、控制面 + 模型网关（一个进程）、PostgreSQL 16、Redis 7，沙箱用宿主机的 Docker（每个会话一个容器）。登录用 GitHub OAuth，注册要邀请码，模型 Key 由用户自带。

只支持 **Linux 宿主机**。平台和 Caddy 用 `network_mode: host`：平台要把模型网关监听在沙箱内部网络的网桥地址上、直接访问沙箱容器的 IP；Caddy 直接占用宿主机 80/443，转发到只听 127.0.0.1 的控制面。macOS / Windows 请用 `NCS_AUTH=dev python -m nanocode_server` 直接跑（见 [server/README.md](../../server/README.md)）。

## 准备

1. **域名**：A 记录指向服务器公网 IP（例：`nanocode.aipulse.lol → 104.194.92.198`），防火墙放行 80 和 443。
2. **GitHub OAuth App**：GitHub → Settings → Developer settings → OAuth Apps → New OAuth App
   - Homepage URL：`https://nanocode.aipulse.lol`
   - Authorization callback URL：`https://nanocode.aipulse.lol/auth/github/callback`
   - 建好后复制 Client ID，点 "Generate a new client secret" 生成 Secret，填进 `.env`（不要提交到仓库）。
3. **Docker**：Docker Engine 24+ 和 Compose v2。

## 启动

```bash
cd deploy/compose
cp .env.example .env
# 三个密钥都换成随机值（十六进制，放进连接串不用转义）
sed -i "s/替换成-openssl-rand-hex-32-的输出/$(openssl rand -hex 32)/" .env
sed -i "s/替换成-openssl-rand-hex-16-的输出/$(openssl rand -hex 16)/g" .env
chmod 600 .env
# 编辑 .env：NCS_DOMAIN、NCS_GITHUB_CLIENT_ID、NCS_GITHUB_CLIENT_SECRET、NCS_ADMIN_GITHUB_LOGINS
sudo mkdir -p /srv/nanocode   # 或改 .env 里的 NCS_HOST_DATA_DIR
# 二选一：
docker compose --profile caddy up -d --build     # 用自带的 Caddy 占 80/443、自动申请证书
docker compose up -d --build                     # 机器上已有 nginx：见下文"用已有的 nginx"
docker compose ps                                  # platform 的 health 应变为 healthy
curl -s http://127.0.0.1:8700/healthz              # {"ok": true, "launcher": "docker", "isolated": true}
curl -sI https://nanocode.aipulse.lol/login        # 证书申请成功后 200
```

### 宿主机防火墙（必做，#123）

沙箱的 internal 网络挡得住外网，挡不住"沙箱 → 宿主机网桥地址上的 sshd / nginx 等端口"。平台第一次启动、建好沙箱网络后，在宿主机上执行：

```bash
cd deploy/firewall
sudo NCS_DOCKER_SUBNET=172.30.250.0/24 NCS_GATEWAY_PORT=8701 ./ncs-sandbox-firewalld.sh   # 没有 firewalld：./ncs-sandbox-nftables.sh
./check-sandbox-isolation.sh                                                             # 网关端口 open，其它 timeout 才算通过
```

细节（为什么按来源网段、默认 zone / eth0 的坑）见 [deploy/firewall/README.md](../firewall/README.md)。

## 低配单机（1 GB 内存，#119）

`.env` 里改这些（默认值保持原来的行为）：

```bash
NCS_PG_SHARED_BUFFERS=64MB
NCS_PG_MAX_CONNECTIONS=20        # 单副本平台连接池是 8
NCS_REDIS_MAXMEMORY=64mb
NCS_PG_MEM_LIMIT=256m
NCS_REDIS_MEM_LIMIT=96m
NCS_PLATFORM_MEM_LIMIT=256m
NCS_SANDBOX_MEMORY=384Mi         # 也认 Docker 写法 384m / 1g
NCS_SANDBOX_TMPFS=96m            # /tmp 算在沙箱内存里；不配时取 512m 和内存一半里较小的
NCS_MAX_SANDBOXES=2
NCS_IDLE_TIMEOUT=600             # 空闲 10 分钟回收沙箱
```

沙箱想放到另一台机器上（平台机器只跑控制面、数据库和 Redis），见 [deploy/sandbox-host/](../sandbox-host/README.md)。

## 用已有的 nginx（代替 Caddy）

机器上 80/443 已经被 nginx 占着时，不启用 `caddy` profile，用 [nginx.example.conf](nginx.example.conf)：

```bash
sudo cp nginx.example.conf /etc/nginx/sites-available/nanocode
sudo ln -s /etc/nginx/sites-available/nanocode /etc/nginx/sites-enabled/nanocode
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d nanocode.aipulse.lol      # 申请证书，自动加 443 和 http→https 跳转
```

要点：`proxy_buffering off`（事件流）、`X-Forwarded-For $proxy_add_x_forwarded_for`（登录限流按真实 IP）、`Host $host`。

## 第一次登录

1. 管理员（`NCS_ADMIN_GITHUB_LOGINS` 里的 GitHub 账号）打开 `https://<域名>`，点"使用 GitHub 登录"，不用邀请码。
2. 左下角钥匙图标进 `/admin`，生成邀请码（可设次数和有效天数，明文只显示一次），发给要邀请的人。
3. 被邀请的人在登录页填邀请码，再点"使用 GitHub 登录"。以后再登录不用邀请码。
4. 每个人在"模型与密钥"里添加自己的 API Key。

## 组成

| 服务 | 说明 |
|---|---|
| `caddy` | 可选（`--profile caddy`）。自动申请 / 续期 Let's Encrypt 证书，证书在 `caddy_data` 卷。HSTS；SSE 不缓冲；管理 API 关闭 |
| `platform` | `nanocode-server:latest`，`standalone` 模式，只听 `127.0.0.1:8700`。以 root 运行（要操作 docker.sock，并把工作区 chown 给沙箱里的 uid 10001）；沙箱容器本身仍是非 root、只读根、无 capabilities、内部网络 |
| `sandbox-image` | 只负责构建 `nanocode-sandbox:latest`，容器立即退出 |
| `postgres` | 端口只发布到 `127.0.0.1:15432`，数据在 `pgdata` 卷 |
| `redis` | 端口只发布到 `127.0.0.1:16379`，带密码、不持久化（限流计数可以丢） |

`NCS_REPLICAS=1`：单机一个进程。要多副本必须用 `deploy/k8s/`，启动检查会拒绝"SQLite + 多副本"和"没配 Redis 的多副本"（#72）。

## 本机试用（没有域名）

把 `.env` 里 `NCS_DOMAIN=localhost`：Caddy 用自签的本地 CA 发证书（浏览器会提示不受信任）。GitHub OAuth App 要另建一个，回调填 `https://localhost/auth/github/callback`。完全离线的冒烟可以用假的 GitHub：`python3 server/tests/fake_github.py 9999`，并设 `NCS_GITHUB_OAUTH_URL=NCS_GITHUB_API_URL=http://127.0.0.1:9999`、`NCS_GITHUB_CLIENT_ID=test-client-id`、`NCS_GITHUB_CLIENT_SECRET=test-client-secret`（授权页地址加 `&login=<名字>` 指定身份）。

## 停止与数据

```bash
docker compose down            # 保留 PostgreSQL 数据、证书和 /srv/nanocode 里的工作区
docker compose down -v         # 连 PostgreSQL 数据和证书一起删（工作区在 NCS_HOST_DATA_DIR，不受影响）
```

备份数据库：`docker compose exec postgres pg_dump -U ncs ncs > ncs.sql`。

工作区可以自动备份到 S3 兼容对象存储（`NCS_OBJECT_STORE=s3`，见 `.env.example` 最后一段和 [distributed-deploy.md 第十节](../../docs/distributed-deploy.md#十工作区快照s3-兼容对象存储)）：沙箱停止 / 回收 / 删除时存一份，本地工作区没了时打开会话会自动恢复。
