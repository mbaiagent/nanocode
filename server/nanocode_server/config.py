"""配置：全部来自 NCS_* 环境变量。

可选组件按"配了就用、没配就用内置实现"的原则选择：

    数据库      NCS_DATABASE_URL=postgresql://...  → PostgreSQL（需要 psycopg）；不配 → SQLite 文件
    限流计数    NCS_REDIS_URL=redis://...          → Redis（需要 redis 包）；不配 → 进程内存
    工作区快照  NCS_OBJECT_STORE=s3 + NCS_S3_*        → 沙箱停止 / 回收 / 删除时打包上传到 S3 兼容存储（B2、MinIO、R2…）；
                                                     默认 off：不需要任何云服务（纯标准库实现，不依赖 boto3）
    沙箱        NCS_LAUNCHER=auto                  → 在 K8s 里就用 K8s；本机有 Docker 且有沙箱镜像就用 Docker；
                                                     否则本机子进程（没有隔离，只适合开发）。
                                                     生产模式（NCS_AUTH != dev）拒绝这种静默退化，也拒绝显式
                                                     NCS_LAUNCHER=local，除非显式 NCS_ALLOW_UNISOLATED=1

配了却用不了（比如配了 PostgreSQL 地址但没装驱动）会直接报错，不会悄悄退回内置实现：
那样一个以为跑在 PostgreSQL 上的集群，其实每个副本各写各的 SQLite。
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
from dataclasses import dataclass, field
from urllib.parse import urlparse

ALL_MODES = ["default", "accept_edits", "plan", "review", "bypass"]
AUTH_MODES = ("github", "proxy", "dev")
AUTH_REQUIRED = ("请设置 NCS_AUTH：github（GitHub 登录，生产）/ proxy（认证代理）/ "
                 "dev（输入任意用户名，只限本机开发）")
SAFE_MODES = [m for m in ALL_MODES if m != "bypass"]
LOOPBACK = ("127.0.0.1", "localhost", "::1")


_SIZE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:([KkMmGg])i?)?[Bb]?")


def parse_size(size: str) -> int:
    """容器大小写法转字节数：K8s 的 384Mi / 1Gi 和 Docker 的 384m / 1g / 512M 都认（都按 1024 进位，#119）。"""
    m = _SIZE.fullmatch(str(size).strip())
    if not m:
        raise ValueError(f"看不懂的大小：{size!r}（例如 384Mi、512m、1Gi）")
    mult = {"k": 2**10, "m": 2**20, "g": 2**30}.get((m.group(2) or "").lower(), 1)
    return int(float(m.group(1)) * mult)


def split_hostport(value: str) -> tuple[str, int]:
    """'10.77.0.1:8701'、'http://10.77.0.1:8701'、'[fd00::1]:8701' → (主机, 端口)；不合法抛 ValueError。"""
    v = value.strip()
    if "://" in v:
        u = urlparse(v)
        if not u.hostname or not u.port:
            raise ValueError(f"地址要带主机和端口：{value!r}")
        return u.hostname, u.port
    host, _, port = v.rpartition(":")
    host = host.strip("[]")
    if not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(f"地址要写成 主机:端口：{value!r}")
    return host, int(port)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    raw = _env(name)
    return int(raw) if raw else default


def _list(name: str, default: list[str]) -> list[str]:
    raw = _env(name)
    return [x.strip() for x in raw.split(",") if x.strip()] if raw else list(default)


def _ports(name: str, default: list[int]) -> list[int]:
    raw = _list(name, [str(p) for p in default])
    try:
        ports = [int(p) for p in raw]
    except ValueError:
        raise ValueError(f"{name} 必须是逗号分隔的端口号，例如 80,443：{_env(name)!r}") from None
    if not ports or not all(0 < p < 65536 for p in ports):
        raise ValueError(f"{name} 必须是 1-65535 的端口号，例如 80,443：{_env(name)!r}")
    return ports


def _json_dict(name: str) -> dict:
    """JSON 对象：直接写在环境变量里，或者写一个文件路径（适合挂载 ConfigMap / Secret）。"""
    raw = _env(name)
    if not raw:
        return {}
    try:
        if raw.startswith("{"):
            return json.loads(raw)
        with open(raw, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        raise ValueError(f"{name} 不是合法的 JSON 对象或 JSON 文件：{e}") from None


def _bool(name: str, default: bool = False) -> bool:
    raw = _env(name).lower()
    return default if not raw else raw in ("1", "true", "yes", "on")


@dataclass
class Config:
    # ------------------------------------------------------------ 基础
    data_dir: str = "./ncs-data"
    secret_key: str = ""                    # 主密钥：签名令牌、加密用户的 API Key 都由它派生，至少 32 个字符
    database_url: str = ""                  # 空 = data_dir 下的 SQLite
    redis_url: str = ""                     # 空 = 进程内存限流

    # ------------------------------------------------------------ 控制面
    host: str = "127.0.0.1"
    port: int = 8700
    public_url: str = ""                    # 对外地址（用于 Cookie 的 Secure 标记），如 https://nanocode.example.com
    auth: str = "dev"                       # github：GitHub 登录（生产）；proxy：信任认证代理；dev：输入任意用户名（只限本机）
                                            # 从环境变量读取时没有默认值，必须显式设置 NCS_AUTH
    auth_header: str = "X-Auth-Request-Email"
    proxy_secret: str = ""
    max_sandboxes: int = 200
    max_sessions_per_user: int = 3
    idle_timeout: int = 1800
    reap_interval: int = 60                 # 回收线程多久醒一次；没有运行中的沙箱时醒了也不查库（#131）
    token_ttl: int = 12 * 3600              # 网关令牌有效期 = 沙箱最长存活时间
    login_ttl: int = 7 * 24 * 3600          # 开发模式登录 Cookie 的有效期

    # ------------------------------------------------------------ 账号（NCS_AUTH=github）
    github_client_id: str = ""
    github_client_secret: str = ""
    github_oauth_url: str = "https://github.com"         # 授权页和换令牌接口；测试 / 冒烟时指向假的 GitHub
    github_api_url: str = "https://api.github.com"
    admin_github_logins: list[str] = field(default_factory=list)   # 引导管理员：首次登录不需要邀请码；也可写 id:<数字 id>
    session_ttl: int = 30 * 24 * 3600       # 登录会话的绝对有效期
    session_idle: int = 7 * 24 * 3600       # 多久没用就失效
    login_per_minute: int = 10              # 每个 IP 每分钟最多发起几次登录（含 OAuth 回调）
    trust_forwarded: bool = False           # 前面有反向代理（Caddy）时用 X-Forwarded-For 最后一项作客户端 IP

    # ------------------------------------------------------------ 沙箱
    launcher: str = "auto"                  # auto / local / docker / k8s / remote
    allow_unisolated: bool = False          # 显式承认"生产模式也没有隔离"的风险（NCS_ALLOW_UNISOLATED=1）
    sandbox_modes: list[str] = field(default_factory=lambda: list(SAFE_MODES))
    sandbox_mode: str = "default"
    sandbox_image: str = "nanocode-sandbox:latest"   # Docker 和 K8s 启动器用的沙箱镜像
    docker_runtime: str = ""                # 例如 runsc（gVisor）；空 = Docker 默认运行时
    docker_network: str = "ncs-sandboxes"
    # #123：沙箱网桥的固定名字和网段。宿主机防火墙按它们写规则（deploy/firewall/），
    # 只放行沙箱到模型网关端口的连接；空 = 交给 Docker 自动分配（防火墙规则就没法写死）
    docker_bridge: str = "ncs-sbx0"
    docker_subnet: str = "172.30.250.0/24"
    sandbox_cpu: str = "1"
    sandbox_memory: str = "2Gi"
    sandbox_tmpfs: str = ""                 # Docker 沙箱 /tmp 的大小；空 = 512m 和沙箱内存一半里较小的（tmpfs 算在容器内存里，#119）
    sandbox_pids: int = 512                 # Docker 沙箱的进程数上限
    sandbox_mcp_servers: dict = field(default_factory=dict)   # 平台给每个沙箱配的 MCP 服务器（用户和项目自己配的一律不生效）
    k8s_api: str = "https://kubernetes.default.svc"
    k8s_namespace: str = "nanocode-sandboxes"
    k8s_runtime_class: str = "gvisor"
    k8s_storage_class: str = ""
    k8s_workspace_size: str = "10Gi"

    # ------------------------------------------------------------ 远程沙箱（NCS_LAUNCHER=remote）
    # 控制面在一台机器、沙箱在另一台：沙箱主机上跑 `python -m nanocode_server sandbox-agent`，
    # 两台之间走 WireGuard 之类的内网隧道（见 deploy/sandbox-host/）。
    remote_agent_url: str = ""              # 控制面用：沙箱代理地址，例如 http://10.77.0.2:8790
    remote_agent_secret: str = ""           # 两边共用的密钥（至少 32 个字符），控制面调用代理的接口时带上
    remote_gateway_url: str = ""            # 沙箱访问网关用的地址；空 = 问沙箱代理（它在沙箱网桥上中继到网关）
    agent_bind: str = ""                    # 沙箱代理用：监听地址，例如 10.77.0.2:8790（只能是内网地址，不能是 0.0.0.0）
    agent_upstream_gateway: str = ""        # 沙箱代理用：真正的模型网关，例如 10.77.0.1:8701
    agent_relay_bind: str = ""              # 沙箱代理用：中继监听的地址；空 = 沙箱网桥的地址（不能是 0.0.0.0，见 validate_agent，#129）
    agent_relay_max_connections: int = 128  # 中继全局连接上限（所有沙箱共用）
    agent_relay_per_sandbox: int = 16       # 每个沙箱（按来源 IP）最多几条中继连接：一个沙箱占不满全局名额
    agent_relay_idle_timeout: int = 300     # 中继连接两个方向都没数据超过这么多秒就断开
    # 出站代理（沙箱代理里，egress.py）：沙箱经它访问公网 HTTP/HTTPS。默认关：打开前先配好防火墙
    egress_proxy: bool = False
    egress_bind: str = ""                   # 监听地址；空 = 和网关中继同一个（沙箱网桥地址）
    egress_port: int = 3128
    egress_allowed_ports: list[int] = field(default_factory=lambda: [80, 443])
    egress_max_connections: int = 64        # 全局上限
    egress_per_sandbox: int = 8             # 每个沙箱（按来源 IP）上限
    egress_idle_timeout: int = 120          # 两个方向都没数据超过这么多秒就断开
    egress_allow_domains: list[str] = field(default_factory=list)   # 非空 = 只放行这些域名（含子域名）
    egress_deny_domains: list[str] = field(default_factory=list)    # 先于 allow 生效
    egress_block_networks: list[str] = field(default_factory=list)  # 额外拦住的网段（WireGuard 网段等）

    # ------------------------------------------------------------ 工作区快照（services/snapshots.py）
    # 只有控制面需要这些：它持有密钥、只给工作区所在的机器（单机的自己 / 沙箱代理）发某一个对象的预签名地址
    object_store: str = "off"               # off（默认，单机不需要任何云服务）/ s3
    s3_endpoint: str = ""                   # 例如 https://s3.<region>.backblazeb2.com、http://127.0.0.1:9000（MinIO）
    s3_region: str = "us-east-1"
    s3_bucket: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_prefix: str = "snapshots"            # 对象 key：<前缀>/<用户>/<会话>/<UTC 时间>.tar.gz
    snapshot_keep: int = 3                  # 每个工作区保留最近几份
    snapshot_max_bytes: str = "1Gi"         # 工作区未压缩大小上限，超了不打包（恢复时同样按它限制解压大小）
    snapshot_ignore: list[str] = field(default_factory=list)  # 追加的忽略规则（文件名通配），默认规则见 runtime/snapshot.py
    snapshot_purge_on_delete: bool = True   # 删除会话时连桶里这个会话的快照一起删（默认，隐私优先）
    snapshot_on_delete: bool = False        # 只在不 purge 时有意义：删除前先存最后一份，误删能找回

    gateway_host: str = "127.0.0.1"
    gateway_port: int = 8701
    gateway_url: str = ""                   # 沙箱访问网关的地址；空 = http://<gateway_host>:<gateway_port>
    platform_base_url: str = ""             # 平台提供的模型（平台付费）：上游地址
    platform_api_key: str = ""
    platform_protocol: str = "openai"
    platform_models: list[str] = field(default_factory=list)
    platform_label: str = "平台模型"
    enable_mock_model: bool = True          # 提供不需要 Key 的演示模型
    allow_private_upstreams: bool = False   # 只允许 dev 模式下把用户自己的 Key 指向内网 / 本机地址（如本机 Ollama）
    daily_tokens: int = 2_000_000           # 每人每天平台模型的 token 上限；用户自己的 Key 不受限
    requests_per_minute: int = 30
    max_output_tokens: int = 8192
    max_connections: int = 256              # HTTP 服务并发连接上限（线程数），超限直接 503：防慢连接/半开连接占满资源
    replicas: int = 1                       # 控制面 + 网关的副本数；>1 时必须用 PostgreSQL，SQLite 不能跨副本共享（#72）

    @classmethod
    def from_env(cls) -> "Config":
        d = cls()
        return cls(
            data_dir=_env("NCS_DATA_DIR", d.data_dir),
            secret_key=_env("NCS_SECRET_KEY"),
            database_url=_env("NCS_DATABASE_URL"),
            redis_url=_env("NCS_REDIS_URL"),
            host=_env("NCS_HOST", d.host),
            port=_int("NCS_PORT", d.port),
            public_url=_env("NCS_PUBLIC_URL"),
            auth=_env("NCS_AUTH"),
            auth_header=_env("NCS_AUTH_HEADER", d.auth_header),
            proxy_secret=_env("NCS_PROXY_SECRET"),
            max_sandboxes=_int("NCS_MAX_SANDBOXES", d.max_sandboxes),
            max_sessions_per_user=_int("NCS_MAX_SESSIONS_PER_USER", d.max_sessions_per_user),
            idle_timeout=_int("NCS_IDLE_TIMEOUT", d.idle_timeout),
            reap_interval=_int("NCS_REAP_INTERVAL", d.reap_interval),
            token_ttl=_int("NCS_TOKEN_TTL", d.token_ttl),
            login_ttl=_int("NCS_LOGIN_TTL", d.login_ttl),
            github_client_id=_env("NCS_GITHUB_CLIENT_ID"),
            github_client_secret=_env("NCS_GITHUB_CLIENT_SECRET"),
            github_oauth_url=_env("NCS_GITHUB_OAUTH_URL", d.github_oauth_url),
            github_api_url=_env("NCS_GITHUB_API_URL", d.github_api_url),
            admin_github_logins=[x.lower() for x in _list("NCS_ADMIN_GITHUB_LOGINS", [])],
            session_ttl=_int("NCS_SESSION_TTL", d.session_ttl),
            session_idle=_int("NCS_SESSION_IDLE", d.session_idle),
            login_per_minute=_int("NCS_LOGIN_PER_MINUTE", d.login_per_minute),
            trust_forwarded=_bool("NCS_TRUST_FORWARDED", False),
            launcher=_env("NCS_LAUNCHER", d.launcher),
            allow_unisolated=_bool("NCS_ALLOW_UNISOLATED", False),
            sandbox_modes=_list("NCS_SANDBOX_MODES", SAFE_MODES),
            sandbox_mode=_env("NCS_SANDBOX_MODE", d.sandbox_mode),
            sandbox_image=_env("NCS_SANDBOX_IMAGE", d.sandbox_image),
            docker_runtime=_env("NCS_DOCKER_RUNTIME"),
            docker_network=_env("NCS_DOCKER_NETWORK", d.docker_network),
            docker_bridge=_env("NCS_DOCKER_BRIDGE", d.docker_bridge),
            docker_subnet=_env("NCS_DOCKER_SUBNET", d.docker_subnet),
            sandbox_cpu=_env("NCS_SANDBOX_CPU", d.sandbox_cpu),
            sandbox_memory=_env("NCS_SANDBOX_MEMORY", d.sandbox_memory),
            sandbox_tmpfs=_env("NCS_SANDBOX_TMPFS"),
            sandbox_pids=_int("NCS_SANDBOX_PIDS", d.sandbox_pids),
            remote_agent_url=_env("NCS_REMOTE_AGENT_URL"),
            remote_agent_secret=_env("NCS_REMOTE_AGENT_SECRET"),
            remote_gateway_url=_env("NCS_REMOTE_GATEWAY_URL"),
            agent_bind=_env("NCS_AGENT_BIND"),
            agent_upstream_gateway=_env("NCS_AGENT_UPSTREAM_GATEWAY"),
            agent_relay_bind=_env("NCS_AGENT_RELAY_BIND"),
            agent_relay_max_connections=_int("NCS_AGENT_RELAY_MAX_CONNECTIONS", d.agent_relay_max_connections),
            agent_relay_per_sandbox=_int("NCS_AGENT_RELAY_PER_SANDBOX", d.agent_relay_per_sandbox),
            agent_relay_idle_timeout=_int("NCS_AGENT_RELAY_IDLE_TIMEOUT", d.agent_relay_idle_timeout),
            egress_proxy=_bool("NCS_EGRESS_PROXY", False),
            egress_bind=_env("NCS_EGRESS_BIND"),
            egress_port=_int("NCS_EGRESS_PORT", d.egress_port),
            egress_allowed_ports=_ports("NCS_EGRESS_ALLOWED_PORTS", d.egress_allowed_ports),
            egress_max_connections=_int("NCS_EGRESS_MAX_CONNECTIONS", d.egress_max_connections),
            egress_per_sandbox=_int("NCS_EGRESS_PER_SANDBOX", d.egress_per_sandbox),
            egress_idle_timeout=_int("NCS_EGRESS_IDLE_TIMEOUT", d.egress_idle_timeout),
            egress_allow_domains=_list("NCS_EGRESS_ALLOW_DOMAINS", []),
            egress_deny_domains=_list("NCS_EGRESS_DENY_DOMAINS", []),
            egress_block_networks=_list("NCS_EGRESS_BLOCK_NETWORKS", []),
            sandbox_mcp_servers=_json_dict("NCS_SANDBOX_MCP_SERVERS"),
            object_store=_env("NCS_OBJECT_STORE", d.object_store).lower(),
            s3_endpoint=_env("NCS_S3_ENDPOINT"),
            s3_region=_env("NCS_S3_REGION", d.s3_region),
            s3_bucket=_env("NCS_S3_BUCKET"),
            s3_access_key_id=_env("NCS_S3_ACCESS_KEY_ID"),
            s3_secret_access_key=_env("NCS_S3_SECRET_ACCESS_KEY"),
            s3_prefix=_env("NCS_S3_PREFIX", d.s3_prefix),
            snapshot_keep=_int("NCS_SNAPSHOT_KEEP", d.snapshot_keep),
            snapshot_max_bytes=_env("NCS_SNAPSHOT_MAX_BYTES", d.snapshot_max_bytes),
            snapshot_ignore=_list("NCS_SNAPSHOT_IGNORE", []),
            snapshot_purge_on_delete=_bool("NCS_SNAPSHOT_PURGE_ON_DELETE", True),
            snapshot_on_delete=_bool("NCS_SNAPSHOT_ON_DELETE", False),
            k8s_api=_env("NCS_K8S_API", d.k8s_api),
            k8s_namespace=_env("NCS_K8S_NAMESPACE", d.k8s_namespace),
            k8s_runtime_class=_env("NCS_K8S_RUNTIME_CLASS", d.k8s_runtime_class),
            k8s_storage_class=_env("NCS_K8S_STORAGE_CLASS"),
            k8s_workspace_size=_env("NCS_K8S_WORKSPACE_SIZE", d.k8s_workspace_size),
            gateway_host=_env("NCS_GATEWAY_HOST", d.gateway_host),
            gateway_port=_int("NCS_GATEWAY_PORT", d.gateway_port),
            gateway_url=_env("NCS_GATEWAY_URL"),
            platform_base_url=_env("NCS_PLATFORM_BASE_URL"),
            platform_api_key=_env("NCS_PLATFORM_API_KEY"),
            platform_protocol=_env("NCS_PLATFORM_PROTOCOL", d.platform_protocol),
            platform_models=_list("NCS_PLATFORM_MODELS", []),
            platform_label=_env("NCS_PLATFORM_LABEL", d.platform_label),
            enable_mock_model=_bool("NCS_ENABLE_MOCK_MODEL", True),
            allow_private_upstreams=_bool("NCS_ALLOW_PRIVATE_UPSTREAMS", False),
            daily_tokens=_int("NCS_DAILY_TOKENS", d.daily_tokens),
            requests_per_minute=_int("NCS_RPM", d.requests_per_minute),
            max_output_tokens=_int("NCS_MAX_OUTPUT_TOKENS", d.max_output_tokens),
            max_connections=_int("NCS_MAX_CONNECTIONS", d.max_connections),
            replicas=_int("NCS_REPLICAS", d.replicas),
        )

    # ------------------------------------------------------------ 派生值

    @property
    def github_callback_url(self) -> str:
        return self.public_url.rstrip("/") + "/auth/github/callback"

    @property
    def https(self) -> bool:
        return self.public_url.startswith("https://")

    def _validate_github(self) -> None:
        if not (self.github_client_id and self.github_client_secret):
            raise ValueError("NCS_AUTH=github 需要 NCS_GITHUB_CLIENT_ID 和 NCS_GITHUB_CLIENT_SECRET（GitHub OAuth App）")
        pub = urlparse(self.public_url)
        if pub.scheme not in ("http", "https") or not pub.hostname or pub.path not in ("", "/"):
            raise ValueError("NCS_AUTH=github 需要 NCS_PUBLIC_URL，例如 https://nanocode.example.com（OAuth 回调地址由它拼出）")
        if pub.scheme != "https" and pub.hostname not in LOOPBACK:
            # 明文 http 上登录 Cookie 会被中间人拿走；只有本机调试可以用 http
            raise ValueError("NCS_PUBLIC_URL 必须是 https://（只有本机地址可以用 http）")
        for name, url in (("NCS_GITHUB_OAUTH_URL", self.github_oauth_url), ("NCS_GITHUB_API_URL", self.github_api_url)):
            u = urlparse(url)
            if not u.hostname or (u.scheme != "https" and u.hostname not in LOOPBACK):
                raise ValueError(f"{name} 必须是 https://（只有本机地址可以用 http，用于测试）")
        if self.session_ttl <= 0 or self.session_idle <= 0:
            raise ValueError("NCS_SESSION_TTL / NCS_SESSION_IDLE 必须大于 0")

    def _validate_remote(self) -> None:
        u = urlparse(self.remote_agent_url)
        if u.scheme not in ("http", "https") or not u.hostname or u.path not in ("", "/"):
            raise ValueError("NCS_LAUNCHER=remote 需要 NCS_REMOTE_AGENT_URL，例如 http://10.77.0.2:8790（走内网隧道）")
        if len(self.remote_agent_secret) < 32:
            raise ValueError("NCS_LAUNCHER=remote 需要 NCS_REMOTE_AGENT_SECRET（至少 32 个字符，和沙箱代理那边一致）")
        if self.remote_gateway_url and urlparse(self.remote_gateway_url).scheme not in ("http", "https"):
            raise ValueError("NCS_REMOTE_GATEWAY_URL 必须是 http(s):// 地址")

    @property
    def snapshot_max(self) -> int:
        return parse_size(self.snapshot_max_bytes)

    def _validate_object_store(self) -> None:
        if self.object_store not in ("off", "s3"):
            raise ValueError("NCS_OBJECT_STORE 只能是 off 或 s3")
        if self.object_store != "s3":
            return
        u = urlparse(self.s3_endpoint)
        if u.scheme not in ("http", "https") or not u.hostname or u.query:
            raise ValueError("NCS_OBJECT_STORE=s3 需要 NCS_S3_ENDPOINT，例如 https://s3.<region>.backblazeb2.com")
        if u.scheme == "http" and u.hostname not in LOOPBACK:
            raise ValueError("NCS_S3_ENDPOINT 必须是 https://（只有本机测试用的 MinIO 可以用 http）")
        missing = [n for n, v in (("NCS_S3_BUCKET", self.s3_bucket), ("NCS_S3_ACCESS_KEY_ID", self.s3_access_key_id),
                                  ("NCS_S3_SECRET_ACCESS_KEY", self.s3_secret_access_key)) if not v]
        if missing:
            raise ValueError(f"NCS_OBJECT_STORE=s3 还需要：{', '.join(missing)}")
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", self.s3_bucket):
            raise ValueError("NCS_S3_BUCKET 不是合法的桶名（3-63 个小写字母、数字、. -）")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,200}", self.s3_prefix.strip("/") or "x") or ".." in self.s3_prefix:
            raise ValueError("NCS_S3_PREFIX 只能包含字母、数字和 . _ - /")
        if not 1 <= self.snapshot_keep <= 100:
            raise ValueError("NCS_SNAPSHOT_KEEP 要在 1 到 100 之间")
        try:
            size = parse_size(self.snapshot_max_bytes)
        except ValueError as e:
            raise ValueError(f"NCS_SNAPSHOT_MAX_BYTES：{e}") from None
        if not (1 << 20) <= size <= (5 << 30):
            raise ValueError("NCS_SNAPSHOT_MAX_BYTES 要在 1Mi 到 5Gi 之间（单次 PUT 的上限是 5 GB）")

    def validate_agent(self) -> None:
        """沙箱代理（sandbox-agent）的启动检查：它能起停容器，接口只能开在内网地址上。"""
        if len(self.remote_agent_secret) < 32:
            raise ValueError("沙箱代理需要 NCS_REMOTE_AGENT_SECRET（至少 32 个字符，和控制面一致）")
        try:
            host, _ = split_hostport(self.agent_bind)
        except ValueError:
            raise ValueError("沙箱代理需要 NCS_AGENT_BIND=<内网地址>:<端口>，例如 10.77.0.2:8790") from None
        if host in ("0.0.0.0", "::", "*", ""):
            raise ValueError("NCS_AGENT_BIND 不能监听所有地址：沙箱代理能起停容器，只能开在内网隧道地址上（如 WireGuard 的 10.77.0.2）")
        try:
            split_hostport(self.agent_upstream_gateway)
        except ValueError:
            raise ValueError("沙箱代理需要 NCS_AGENT_UPSTREAM_GATEWAY=<网关地址>:<端口>，例如 10.77.0.1:8701") from None
        # 网关中继和代理监听地址一样，只能开在内网地址上（#129）：配成 0.0.0.0 会把中继暴露到公网
        if self.agent_relay_bind and self.agent_relay_bind.strip("[]") in ("0.0.0.0", "::", "*"):
            raise ValueError("NCS_AGENT_RELAY_BIND 不能监听所有地址：网关中继只给沙箱用，留空就是沙箱网桥地址")
        if not (1 <= self.agent_relay_per_sandbox <= self.agent_relay_max_connections):
            raise ValueError("要求 1 <= NCS_AGENT_RELAY_PER_SANDBOX <= NCS_AGENT_RELAY_MAX_CONNECTIONS")
        if self.agent_relay_idle_timeout < 10:
            raise ValueError("NCS_AGENT_RELAY_IDLE_TIMEOUT 至少 10 秒（模型首个 token 前可能有停顿）")
        if self.egress_proxy:
            self._validate_egress()

    def _validate_egress(self) -> None:
        if self.egress_bind.strip("[]") in ("0.0.0.0", "::", "*"):
            raise ValueError("NCS_EGRESS_BIND 不能监听所有地址：出站代理只给沙箱用，留空就是沙箱网桥地址")
        if not 0 < self.egress_port < 65536:
            raise ValueError("NCS_EGRESS_PORT 必须是 1-65535 的端口号")
        if self.egress_port == self.gateway_port:
            raise ValueError("NCS_EGRESS_PORT 不能和 NCS_GATEWAY_PORT 相同")
        if not self.egress_allowed_ports or not all(0 < p < 65536 for p in self.egress_allowed_ports):
            raise ValueError("NCS_EGRESS_ALLOWED_PORTS 必须是 1-65535 的端口号，例如 80,443")
        if not (1 <= self.egress_per_sandbox <= self.egress_max_connections):
            raise ValueError("要求 1 <= NCS_EGRESS_PER_SANDBOX <= NCS_EGRESS_MAX_CONNECTIONS")
        if self.egress_idle_timeout < 10:
            raise ValueError("NCS_EGRESS_IDLE_TIMEOUT 至少 10 秒")
        for n in self.egress_block_networks:
            try:
                ipaddress.ip_network(n, strict=False)
            except ValueError:
                raise ValueError(f"NCS_EGRESS_BLOCK_NETWORKS 里有不合法的网段：{n!r}") from None

    @property
    def sandbox_gateway_url(self) -> str:
        return (self.gateway_url or f"http://{self.gateway_host}:{self.gateway_port}").rstrip("/")

    @property
    def has_platform_models(self) -> bool:
        return bool(self.platform_base_url and self.platform_api_key and self.platform_models)

    def ensure_secret(self) -> None:
        """没配主密钥时：开发模式生成一个并保存在数据目录里（重启后用户的 API Key 还能解密）；生产模式报错。"""
        if not self.auth:
            raise ValueError(AUTH_REQUIRED)
        if self.secret_key:
            return
        if self.auth != "dev":
            raise ValueError("生产模式必须设置 NCS_SECRET_KEY（至少 32 个字符）")
        path = os.path.join(self.data_dir, "secret.key")
        os.makedirs(self.data_dir, exist_ok=True)
        os.chmod(self.data_dir, 0o700)  # #62：数据目录含 secret.key 和 SQLite，只允许 owner 读写
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                self.secret_key = f.read().strip()
            return
        self.secret_key = secrets.token_urlsafe(48)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(self.secret_key)

    def validate(self, launcher_needed: bool = True) -> None:
        """启动前的安全检查：配错了宁可起不来，也不要带着漏洞运行。

        launcher_needed=False 给"只启动模型网关"的角色用：它不创建沙箱，隔离与否与它无关。
        """
        if len(self.secret_key) < 32:
            raise ValueError("NCS_SECRET_KEY 至少 32 个字符")
        if not self.auth:
            raise ValueError(AUTH_REQUIRED)
        if self.auth not in AUTH_MODES:
            raise ValueError("NCS_AUTH 只能是 github / proxy / dev")
        if self.auth == "github":
            self._validate_github()
        if self.auth == "proxy" and len(self.proxy_secret) < 16:
            raise ValueError("proxy 认证必须设置 NCS_PROXY_SECRET（至少 16 个字符），否则谁都能伪造身份请求头")
        if self.allow_private_upstreams and self.auth != "dev":
            raise ValueError("NCS_ALLOW_PRIVATE_UPSTREAMS 仅限 dev 认证模式")
        if self.auth == "dev" and self.host not in LOOPBACK:
            raise ValueError("dev 认证（输入任意用户名登录）只能在只监听本机时使用，对外服务请用 NCS_AUTH=github 或 proxy")
        if self.launcher not in ("auto", "local", "docker", "k8s", "remote"):
            raise ValueError("NCS_LAUNCHER 只能是 auto / local / docker / k8s / remote")
        if launcher_needed and self.launcher == "remote":
            self._validate_remote()
        self._validate_object_store()
        if self.reap_interval < 1:
            raise ValueError("NCS_REAP_INTERVAL 至少 1 秒")
        if self.replicas < 1:
            raise ValueError("NCS_REPLICAS 至少为 1")
        # SQLite 是本地文件，多副本各写各的：登录写在这个副本、下一个请求落到那个副本，
        # 就会"登录成功后被踢回登录页"（#72 跨机器实测复现）。宁可拒绝启动。
        if self.replicas > 1 and not self.database_url.startswith(("postgres://", "postgresql://")):
            raise ValueError(
                f"NCS_REPLICAS={self.replicas} 但数据库是 SQLite（NCS_DATABASE_URL 未配或不是 postgresql://）："
                "SQLite 不能跨副本共享，多副本会脑裂。请设置 NCS_DATABASE_URL=postgresql://...，"
                "或把 NCS_REPLICAS 设回 1")
        if self.replicas > 1 and not self.redis_url:
            # 进程内存限流每个副本各算各的，N 个副本就是 N 倍额度（#56）
            raise ValueError(f"NCS_REPLICAS={self.replicas} 需要共享的限流计数：请设置 NCS_REDIS_URL=redis://...")
        if launcher_needed and self.auth != "dev" and self.launcher == "local" and not self.allow_unisolated:
            # 没有隔离时，"每个用户一个沙箱"的所有保证都不成立（#36）：Agent 会以控制面同一个
            # 系统用户运行，读得到主密钥、数据库里所有会话的令牌和别人的工作区。/api/me 里的
            # isolated:false 要登录后才看得到，阻止不了事故——所以在启动前就拒绝。
            raise ValueError(
                "生产模式（NCS_AUTH != dev）不能用没有隔离的本机启动器：请构建沙箱镜像让 NCS_LAUNCHER=auto "
                "选到 Docker/K8s，或显式设置 NCS_LAUNCHER=docker/k8s；确实要在生产里无隔离运行，"
                "请显式设置 NCS_ALLOW_UNISOLATED=1 承认这个风险")
        if self.docker_bridge and not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", self.docker_bridge):
            raise ValueError("NCS_DOCKER_BRIDGE 是 Linux 网卡名：1-15 个字母、数字或 _ . -")
        if self.docker_subnet:
            try:
                net = ipaddress.ip_network(self.docker_subnet, strict=True)
            except ValueError:
                raise ValueError(f"NCS_DOCKER_SUBNET 不是合法网段：{self.docker_subnet!r}（例如 172.30.250.0/24）") from None
            if net.version != 4 or net.prefixlen > 28:
                raise ValueError("NCS_DOCKER_SUBNET 必须是 IPv4 网段，且至少 /28")
        try:
            parse_size(self.sandbox_memory)
            if self.sandbox_tmpfs:
                parse_size(self.sandbox_tmpfs)
        except ValueError as e:
            raise ValueError(f"NCS_SANDBOX_MEMORY / NCS_SANDBOX_TMPFS：{e}") from None
        if self.sandbox_pids < 16:
            raise ValueError("NCS_SANDBOX_PIDS 至少 16")
        if self.sandbox_mode not in self.sandbox_modes:
            raise ValueError(f"NCS_SANDBOX_MODE={self.sandbox_mode} 不在 NCS_SANDBOX_MODES 里")
        servers = self.sandbox_mcp_servers
        if not isinstance(servers, dict) or not all(
                isinstance(v, dict) and isinstance(v.get("command"), str) and v["command"] for v in servers.values()):
            raise ValueError('NCS_SANDBOX_MCP_SERVERS 格式：{"名字": {"command": "...", "args": [...]}, ...}')
        if self.platform_protocol not in ("openai", "anthropic"):
            raise ValueError("NCS_PLATFORM_PROTOCOL 只能是 openai 或 anthropic")


__all__ = ["Config", "parse_size", "split_hostport", "ALL_MODES", "SAFE_MODES", "LOOPBACK", "AUTH_MODES"]
