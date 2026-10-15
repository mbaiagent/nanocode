"""配置：全部来自环境变量（容器里最常用的方式），每一项都有说明和默认值。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

ALL_MODES = ["default", "accept_edits", "plan", "review", "bypass"]


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    return int(raw) if raw.strip() else default


def _list(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name, "")
    return [x.strip() for x in raw.split(",") if x.strip()] if raw.strip() else list(default)


@dataclass
class Config:
    # ------------------------------------------------------------ 通用
    data_dir: str = "./ncs-data"          # NCS_DATA_DIR：数据库、本地沙箱的工作区
    token_secret: str = ""                # NCS_TOKEN_SECRET：签发给沙箱的网关令牌用它签名（控制面和网关共用）

    # ------------------------------------------------------------ 控制面
    host: str = "127.0.0.1"               # NCS_HOST
    port: int = 8700                      # NCS_PORT
    auth_header: str = "X-Auth-Request-Email"   # NCS_AUTH_HEADER：认证代理（如 oauth2-proxy）放用户身份的请求头
    proxy_secret: str = ""                # NCS_PROXY_SECRET：认证代理必须带上 X-NCS-Proxy-Secret，防止绕过代理伪造身份
    dev_user: str = ""                    # NCS_DEV_USER：仅本机开发——来自本机的请求一律当成这个用户
    launcher: str = "local"               # NCS_LAUNCHER：local（开发，本机子进程，没有隔离）/ k8s（生产）
    max_sandboxes: int = 200              # NCS_MAX_SANDBOXES：同时运行的沙箱上限（总容量）
    max_sessions_per_user: int = 3        # NCS_MAX_SESSIONS_PER_USER：每人同时运行的会话上限
    idle_timeout: int = 1800              # NCS_IDLE_TIMEOUT：空闲多少秒（没有请求、Agent 不在工作）回收沙箱
    gateway_url: str = "http://127.0.0.1:8701/v1"   # NCS_GATEWAY_URL：沙箱访问模型网关的地址
    gateway_admin_url: str = ""           # NCS_GATEWAY_ADMIN_URL：控制面查询用量时访问网关的地址（默认同上）
    model: str = "deepseek-chat"          # NCS_MODEL：沙箱使用的模型（网关也会强制这个模型）
    sandbox_provider: str = "custom"      # NCS_SANDBOX_PROVIDER：沙箱里的 provider；测试和离线开发可设为 mock
    sandbox_modes: list[str] = field(default_factory=lambda: [m for m in ALL_MODES if m != "bypass"])
                                          # NCS_SANDBOX_MODES：沙箱里允许的权限模式；k8s 强隔离时可以加上 bypass
    sandbox_mode: str = "default"         # NCS_SANDBOX_MODE：沙箱启动时的权限模式
    token_ttl: int = 12 * 3600            # NCS_TOKEN_TTL：网关令牌有效期，也是沙箱的最长存活时间

    # ------------------------------------------------------------ Kubernetes 启动器
    k8s_api: str = "https://kubernetes.default.svc"   # NCS_K8S_API
    k8s_namespace: str = "nanocode-sandboxes"          # NCS_K8S_NAMESPACE
    k8s_image: str = "nanocode-sandbox:latest"         # NCS_K8S_IMAGE
    k8s_runtime_class: str = "gvisor"                  # NCS_K8S_RUNTIME_CLASS：强隔离运行时，留空 = 不用（不推荐）
    k8s_storage_class: str = ""                        # NCS_K8S_STORAGE_CLASS：工作区持久卷的存储类，留空用集群默认
    k8s_workspace_size: str = "10Gi"                   # NCS_K8S_WORKSPACE_SIZE
    k8s_cpu: str = "1"                                 # NCS_K8S_CPU：每个沙箱的 CPU 上限
    k8s_memory: str = "2Gi"                            # NCS_K8S_MEMORY：每个沙箱的内存上限
    k8s_policy_configmap: str = "ncs-sandbox-policy"   # NCS_K8S_POLICY_CONFIGMAP：沙箱策略文件所在的 ConfigMap

    # ------------------------------------------------------------ 模型网关
    gateway_host: str = "127.0.0.1"       # NCS_GATEWAY_HOST
    gateway_port: int = 8701              # NCS_GATEWAY_PORT
    upstream_base_url: str = ""           # NCS_UPSTREAM_BASE_URL：真实的模型 API，如 https://api.deepseek.com/v1
    upstream_api_key: str = ""            # NCS_UPSTREAM_API_KEY：真实的 Key，只在网关进程里
    upstream_protocol: str = "openai"     # NCS_UPSTREAM_PROTOCOL：openai / anthropic
    daily_tokens: int = 2_000_000         # NCS_DAILY_TOKENS：每人每天的 token 上限（输入 + 输出），0 = 不限
    requests_per_minute: int = 30         # NCS_RPM：每人每分钟的请求上限，0 = 不限
    max_output_tokens: int = 8192         # NCS_MAX_OUTPUT_TOKENS：单次请求的输出上限，超过的请求会被压到这个值

    @classmethod
    def from_env(cls) -> "Config":
        c = cls(
            data_dir=_env("NCS_DATA_DIR", cls.data_dir),
            token_secret=_env("NCS_TOKEN_SECRET"),
            host=_env("NCS_HOST", cls.host),
            port=_int("NCS_PORT", cls.port),
            auth_header=_env("NCS_AUTH_HEADER", cls.auth_header),
            proxy_secret=_env("NCS_PROXY_SECRET"),
            dev_user=_env("NCS_DEV_USER"),
            launcher=_env("NCS_LAUNCHER", cls.launcher),
            max_sandboxes=_int("NCS_MAX_SANDBOXES", cls.max_sandboxes),
            max_sessions_per_user=_int("NCS_MAX_SESSIONS_PER_USER", cls.max_sessions_per_user),
            idle_timeout=_int("NCS_IDLE_TIMEOUT", cls.idle_timeout),
            gateway_url=_env("NCS_GATEWAY_URL", cls.gateway_url),
            gateway_admin_url=_env("NCS_GATEWAY_ADMIN_URL"),
            model=_env("NCS_MODEL", cls.model),
            sandbox_provider=_env("NCS_SANDBOX_PROVIDER", cls.sandbox_provider),
            sandbox_modes=_list("NCS_SANDBOX_MODES", [m for m in ALL_MODES if m != "bypass"]),
            sandbox_mode=_env("NCS_SANDBOX_MODE", cls.sandbox_mode),
            token_ttl=_int("NCS_TOKEN_TTL", cls.token_ttl),
            k8s_api=_env("NCS_K8S_API", cls.k8s_api),
            k8s_namespace=_env("NCS_K8S_NAMESPACE", cls.k8s_namespace),
            k8s_image=_env("NCS_K8S_IMAGE", cls.k8s_image),
            k8s_runtime_class=_env("NCS_K8S_RUNTIME_CLASS", cls.k8s_runtime_class),
            k8s_storage_class=_env("NCS_K8S_STORAGE_CLASS"),
            k8s_workspace_size=_env("NCS_K8S_WORKSPACE_SIZE", cls.k8s_workspace_size),
            k8s_cpu=_env("NCS_K8S_CPU", cls.k8s_cpu),
            k8s_memory=_env("NCS_K8S_MEMORY", cls.k8s_memory),
            k8s_policy_configmap=_env("NCS_K8S_POLICY_CONFIGMAP", cls.k8s_policy_configmap),
            gateway_host=_env("NCS_GATEWAY_HOST", cls.gateway_host),
            gateway_port=_int("NCS_GATEWAY_PORT", cls.gateway_port),
            upstream_base_url=_env("NCS_UPSTREAM_BASE_URL"),
            upstream_api_key=_env("NCS_UPSTREAM_API_KEY"),
            upstream_protocol=_env("NCS_UPSTREAM_PROTOCOL", cls.upstream_protocol),
            daily_tokens=_int("NCS_DAILY_TOKENS", cls.daily_tokens),
            requests_per_minute=_int("NCS_RPM", cls.requests_per_minute),
            max_output_tokens=_int("NCS_MAX_OUTPUT_TOKENS", cls.max_output_tokens),
        )
        return c

    def check_control(self) -> None:
        """控制面启动前的安全检查：配置错了宁可起不来，也不要带着漏洞运行。"""
        if len(self.token_secret) < 32:
            raise ValueError("NCS_TOKEN_SECRET 至少 32 个字符（网关令牌靠它签名）")
        if not self.proxy_secret and not self.dev_user:
            raise ValueError("必须设置 NCS_PROXY_SECRET（生产：认证代理带上它）或 NCS_DEV_USER（仅本机开发）。"
                             "否则任何人都能伪造身份请求头")
        if self.dev_user and self.host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("NCS_DEV_USER 只能在只监听本机（NCS_HOST=127.0.0.1）时使用")
        if self.sandbox_mode not in self.sandbox_modes:
            raise ValueError(f"NCS_SANDBOX_MODE={self.sandbox_mode} 不在 NCS_SANDBOX_MODES 里")
        if self.launcher == "local" and "bypass" in self.sandbox_modes:
            raise ValueError("local 启动器没有任何隔离，不能允许 bypass 模式")

    def check_gateway(self) -> None:
        if len(self.token_secret) < 32:
            raise ValueError("NCS_TOKEN_SECRET 至少 32 个字符")
        if not self.upstream_base_url or not self.upstream_api_key:
            raise ValueError("网关需要 NCS_UPSTREAM_BASE_URL 和 NCS_UPSTREAM_API_KEY")
        if self.upstream_protocol not in ("openai", "anthropic"):
            raise ValueError("NCS_UPSTREAM_PROTOCOL 只能是 openai 或 anthropic")

    def sandbox_policy(self, audit_log: str) -> dict:
        """写进每个沙箱的平台策略（nanocode/policy.py）。k8s 部署时同一份内容放进 ConfigMap。

        - 模型去向锁死在网关：沙箱里的 Agent 改配置、改启动参数都绕不过去；
        - 用户和项目配置里能执行命令（hooks、mcpServers、verify）、放宽权限（permissions.allow）、
          改请求去向（headers、rag 的 embedding 地址）的键一律删掉。
        """
        policy = {
            "provider": self.sandbox_provider,
            "mode": self.sandbox_mode,
            "allowed_modes": list(self.sandbox_modes),
            "forbid": ["hooks", "mcpServers", "verify", "permissions.allow", "headers", "rag"],
            "audit_log": audit_log,
        }
        if self.sandbox_provider != "mock":
            base = self.gateway_url.rstrip("/")
            if self.upstream_protocol == "anthropic":      # Anthropic 客户端自己拼 /v1/messages
                base = base[:-3] if base.endswith("/v1") else base
            policy.update(protocol=self.upstream_protocol, base_url=base, model=self.model)
        return policy


__all__ = ["Config", "ALL_MODES"]
