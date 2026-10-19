"""配置：全部来自 NCS_* 环境变量。

可选组件按"配了就用、没配就用内置实现"的原则选择：

    数据库      NCS_DATABASE_URL=postgresql://...  → PostgreSQL（需要 psycopg）；不配 → SQLite 文件
    限流计数    NCS_REDIS_URL=redis://...          → Redis（需要 redis 包）；不配 → 进程内存
    沙箱        NCS_LAUNCHER=auto                  → 在 K8s 里就用 K8s；本机有 Docker 且有沙箱镜像就用 Docker；
                                                     否则本机子进程（没有隔离，只适合开发）。
                                                     生产模式（NCS_AUTH != dev）拒绝这种静默退化，也拒绝显式
                                                     NCS_LAUNCHER=local，除非显式 NCS_ALLOW_UNISOLATED=1

配了却用不了（比如配了 PostgreSQL 地址但没装驱动）会直接报错，不会悄悄退回内置实现：
那样一个以为跑在 PostgreSQL 上的集群，其实每个副本各写各的 SQLite。
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, field

ALL_MODES = ["default", "accept_edits", "plan", "review", "bypass"]
SAFE_MODES = [m for m in ALL_MODES if m != "bypass"]
LOOPBACK = ("127.0.0.1", "localhost", "::1")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    raw = _env(name)
    return int(raw) if raw else default


def _list(name: str, default: list[str]) -> list[str]:
    raw = _env(name)
    return [x.strip() for x in raw.split(",") if x.strip()] if raw else list(default)


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
    auth: str = "dev"                       # dev：登录页输入任意用户名（只限本机）；proxy：信任认证代理
    auth_header: str = "X-Auth-Request-Email"
    proxy_secret: str = ""
    max_sandboxes: int = 200
    max_sessions_per_user: int = 3
    idle_timeout: int = 1800
    token_ttl: int = 12 * 3600              # 网关令牌有效期 = 沙箱最长存活时间
    login_ttl: int = 7 * 24 * 3600          # 开发模式登录 Cookie 的有效期

    # ------------------------------------------------------------ 沙箱
    launcher: str = "auto"                  # auto / local / docker / k8s
    allow_unisolated: bool = False          # 显式承认"生产模式也没有隔离"的风险（NCS_ALLOW_UNISOLATED=1）
    sandbox_modes: list[str] = field(default_factory=lambda: list(SAFE_MODES))
    sandbox_mode: str = "default"
    sandbox_image: str = "nanocode-sandbox:latest"   # Docker 和 K8s 启动器用的沙箱镜像
    docker_runtime: str = ""                # 例如 runsc（gVisor）；空 = Docker 默认运行时
    docker_network: str = "ncs-sandboxes"
    sandbox_cpu: str = "1"
    sandbox_memory: str = "2Gi"
    sandbox_mcp_servers: dict = field(default_factory=dict)   # 平台给每个沙箱配的 MCP 服务器（用户和项目自己配的一律不生效）
    k8s_api: str = "https://kubernetes.default.svc"
    k8s_namespace: str = "nanocode-sandboxes"
    k8s_runtime_class: str = "gvisor"
    k8s_storage_class: str = ""
    k8s_workspace_size: str = "10Gi"

    # ------------------------------------------------------------ 模型网关
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
            auth=_env("NCS_AUTH", d.auth),
            auth_header=_env("NCS_AUTH_HEADER", d.auth_header),
            proxy_secret=_env("NCS_PROXY_SECRET"),
            max_sandboxes=_int("NCS_MAX_SANDBOXES", d.max_sandboxes),
            max_sessions_per_user=_int("NCS_MAX_SESSIONS_PER_USER", d.max_sessions_per_user),
            idle_timeout=_int("NCS_IDLE_TIMEOUT", d.idle_timeout),
            token_ttl=_int("NCS_TOKEN_TTL", d.token_ttl),
            login_ttl=_int("NCS_LOGIN_TTL", d.login_ttl),
            launcher=_env("NCS_LAUNCHER", d.launcher),
            allow_unisolated=_bool("NCS_ALLOW_UNISOLATED", False),
            sandbox_modes=_list("NCS_SANDBOX_MODES", SAFE_MODES),
            sandbox_mode=_env("NCS_SANDBOX_MODE", d.sandbox_mode),
            sandbox_image=_env("NCS_SANDBOX_IMAGE", d.sandbox_image),
            docker_runtime=_env("NCS_DOCKER_RUNTIME"),
            docker_network=_env("NCS_DOCKER_NETWORK", d.docker_network),
            sandbox_cpu=_env("NCS_SANDBOX_CPU", d.sandbox_cpu),
            sandbox_memory=_env("NCS_SANDBOX_MEMORY", d.sandbox_memory),
            sandbox_mcp_servers=_json_dict("NCS_SANDBOX_MCP_SERVERS"),
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
    def sandbox_gateway_url(self) -> str:
        return (self.gateway_url or f"http://{self.gateway_host}:{self.gateway_port}").rstrip("/")

    @property
    def has_platform_models(self) -> bool:
        return bool(self.platform_base_url and self.platform_api_key and self.platform_models)

    def ensure_secret(self) -> None:
        """没配主密钥时：开发模式生成一个并保存在数据目录里（重启后用户的 API Key 还能解密）；生产模式报错。"""
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
        if self.auth not in ("dev", "proxy"):
            raise ValueError("NCS_AUTH 只能是 dev 或 proxy")
        if self.auth == "proxy" and len(self.proxy_secret) < 16:
            raise ValueError("proxy 认证必须设置 NCS_PROXY_SECRET（至少 16 个字符），否则谁都能伪造身份请求头")
        if self.allow_private_upstreams and self.auth != "dev":
            raise ValueError("NCS_ALLOW_PRIVATE_UPSTREAMS 仅限 dev 认证模式")
        if self.auth == "dev" and self.host not in LOOPBACK:
            raise ValueError("dev 认证（输入任意用户名登录）只能在只监听本机时使用，对外服务请用 NCS_AUTH=proxy")
        if self.launcher not in ("auto", "local", "docker", "k8s"):
            raise ValueError("NCS_LAUNCHER 只能是 auto / local / docker / k8s")
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
        if self.sandbox_mode not in self.sandbox_modes:
            raise ValueError(f"NCS_SANDBOX_MODE={self.sandbox_mode} 不在 NCS_SANDBOX_MODES 里")
        servers = self.sandbox_mcp_servers
        if not isinstance(servers, dict) or not all(
                isinstance(v, dict) and isinstance(v.get("command"), str) and v["command"] for v in servers.values()):
            raise ValueError('NCS_SANDBOX_MCP_SERVERS 格式：{"名字": {"command": "...", "args": [...]}, ...}')
        if self.platform_protocol not in ("openai", "anthropic"):
            raise ValueError("NCS_PLATFORM_PROTOCOL 只能是 openai 或 anthropic")


__all__ = ["Config", "ALL_MODES", "SAFE_MODES", "LOOPBACK"]
