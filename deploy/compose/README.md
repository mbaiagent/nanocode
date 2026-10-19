# 单机部署（docker compose）

一台 Linux 服务器上跑起整个平台：Caddy（自动 HTTPS）、控制面 + 模型网关（一个进程）、PostgreSQL 16、Redis 7，沙箱用宿主机的 Docker（每个会话一个容器）。登录用 GitHub OAuth，注册要邀请码，模型 Key 由用户自带。

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
docker compose up -d --build
docker compose ps                                  # platform 的 health 应变为 healthy
curl -s http://127.0.0.1:8700/healthz              # {"ok": true, "launcher": "docker", "isolated": true}
curl -sI https://nanocode.aipulse.lol/login        # 证书申请成功后 200
```

## 第一次登录

1. 管理员（`NCS_ADMIN_GITHUB_LOGINS` 里的 GitHub 账号）打开 `https://<域名>`，点"使用 GitHub 登录"，不用邀请码。
2. 左下角钥匙图标进 `/admin`，生成邀请码（可设次数和有效天数，明文只显示一次），发给要邀请的人。
3. 被邀请的人在登录页填邀请码，再点"使用 GitHub 登录"。以后再登录不用邀请码。
4. 每个人在"模型与密钥"里添加自己的 API Key。

## 组成

| 服务 | 说明 |
|---|---|
| `caddy` | 自动申请 / 续期 Let's Encrypt 证书，证书在 `caddy_data` 卷。HSTS；SSE 不缓冲 |
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
