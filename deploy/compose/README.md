# 单机部署（docker compose）

一台 Linux 服务器上跑起整个平台：控制面 + 模型网关（一个进程）、PostgreSQL 16、Redis 7，沙箱用宿主机的 Docker（每个会话一个容器）。

只支持 **Linux 宿主机**。平台容器用 `network_mode: host`：它要把模型网关监听在沙箱内部网络的网桥地址上，并直接访问沙箱容器的 IP，这两件事只能在宿主机网络里做。macOS / Windows 的 Docker Desktop 没有 host 网络的这些语义，请用 `python -m nanocode_server` 直接跑（见 [server/README.md](../../server/README.md)）。

## 启动

```bash
cd deploy/compose
cp .env.example .env
# 三个密钥都换成随机值（十六进制，放进连接串不用转义）
sed -i "s/替换成-openssl-rand-hex-32-的输出/$(openssl rand -hex 32)/" .env
sed -i "s/替换成-openssl-rand-hex-16-的输出/$(openssl rand -hex 16)/g" .env
sudo mkdir -p /srv/nanocode   # 或改 .env 里的 NCS_HOST_DATA_DIR
docker compose up -d --build
docker compose ps        # platform 的 health 应变为 healthy
curl -s http://127.0.0.1:8700/healthz          # {"ok": true, ...}
```

打开 `http://127.0.0.1:8700`，登录页输入任意用户名（`NCS_AUTH=dev`，只限本机），在左下角"模型与密钥"里添加自己的 API Key。

远程访问用 SSH 隧道：`ssh -L 8700:127.0.0.1:8700 <服务器>`。对公网开放要等 P1 的 GitHub 登录；在那之前也可以前面挂认证代理，设 `NCS_AUTH=proxy` 和 `NCS_PROXY_SECRET`（见 `server/README.md` 的配置表）。

## 组成

| 服务 | 说明 |
|---|---|
| `platform` | `nanocode-server:latest`，`standalone` 模式。以 root 运行（要操作 docker.sock，并把工作区 chown 给沙箱里的 uid 10001）；沙箱容器本身仍是非 root、只读根、无 capabilities、内部网络 |
| `sandbox-image` | 只负责构建 `nanocode-sandbox:latest`，容器立即退出 |
| `postgres` | 端口只发布到 `127.0.0.1:15432`，数据在 `pgdata` 卷 |
| `redis` | 端口只发布到 `127.0.0.1:16379`，带密码、不持久化（限流计数可以丢） |

`NCS_REPLICAS=1`：单机一个进程。要多副本必须用 `deploy/k8s/`，启动检查会拒绝"SQLite + 多副本"和"没配 Redis 的多副本"（#72）。

## 停止与数据

```bash
docker compose down            # 保留 PostgreSQL 数据和 /srv/nanocode 里的工作区
docker compose down -v         # 连 PostgreSQL 数据一起删（工作区在 NCS_HOST_DATA_DIR，不受影响）
```

备份数据库：`docker compose exec postgres pg_dump -U ncs ncs > ncs.sql`。
