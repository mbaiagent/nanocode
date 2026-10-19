"""领域对象：会话、用户的模型凭证、可选的模型、解析后的上游。各层之间传的都是这些，而不是裸字典。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

ACTIVE_STATUSES = ("starting", "running")


@dataclass
class Session:
    id: str
    owner: str
    title: str
    model_ref: str
    status: str = "stopped"            # starting / running / stopped / failed / deleted
    endpoint: str = ""                 # 沙箱地址，只在控制面内部使用
    web_token: str = ""                # 沙箱的 Web 令牌，只在控制面内部使用
    created: float = 0.0
    last_active: float = 0.0
    started: float = 0.0               # 上次启动时间；非零说明启动过，再启动时要恢复对话
    error: str = ""

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    def public(self) -> dict:
        """给浏览器看的：不包含沙箱地址和令牌。"""
        return {"id": self.id, "title": self.title, "model": self.model_ref, "status": self.status,
                "created": self.created, "last_active": self.last_active, "error": self.error,
                "url": f"/s/{self.id}/"}


@dataclass
class Credential:
    """用户自己的模型 API Key（加密保存），以及用它可以调用的模型。"""
    id: str
    owner: str
    provider: str                      # 预设名（deepseek、openai……）或 custom
    label: str
    protocol: str                      # openai / anthropic
    base_url: str
    models: list[str]
    secret: str = ""                   # 加密后的 API Key
    key_hint: str = ""                 # …1234
    created: float = 0.0

    def public(self) -> dict:
        d = asdict(self)
        d.pop("secret")
        return d


@dataclass
class ModelOption:
    """会话可以选择的一个模型。ref 是稳定的标识，保存在会话上。"""
    ref: str                           # mock / p:<模型> / c:<凭证 id>:<模型>
    model: str                         # 真实模型名，沙箱里的 Agent 会被告知它
    label: str                         # 给人看的名字
    group: str                         # 演示 / 平台模型 / 我的模型
    provider: str
    protocol: str = ""                 # openai / anthropic；演示模型为空
    billable: bool = False             # 平台付费：受每日额度限制


@dataclass
class Upstream:
    """网关要把请求转发到哪里：解析会话选的模型得到。"""
    protocol: str
    base_url: str
    api_key: str
    model: str
    billable: bool
    extra_headers: dict = field(default_factory=dict)


@dataclass
class User:
    """平台账号。id 就是其他表里的 owner：GitHub 用户是 gh-<数字 id>，迁移来的老数据是原来的用户名。"""
    id: str
    login: str                         # 给人看的名字：GitHub login / 开发模式用户名 / 代理给的邮箱
    name: str = ""
    email: str = ""
    avatar_url: str = ""
    role: str = "user"                 # user / admin
    status: str = "active"             # active / disabled
    invite_id: str = ""
    created: float = 0.0
    last_login: float = 0.0

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def public(self) -> dict:
        return asdict(self)


@dataclass
class Invite:
    """邀请码。库里只存哈希，明文只在创建时返回一次。"""
    id: str
    code_hint: str                     # 末 4 位，方便管理员认
    note: str = ""
    created_by: str = ""
    created: float = 0.0
    expires: float = 0.0               # 0 = 不过期
    max_uses: int = 1
    uses: int = 0
    revoked: int = 0

    def state(self, now: float) -> str:
        if self.revoked:
            return "revoked"
        if self.expires and self.expires <= now:
            return "expired"
        if self.uses >= self.max_uses:
            return "used"
        return "active"

    def public(self, now: float) -> dict:
        return {**asdict(self), "revoked": bool(self.revoked), "state": self.state(now)}


__all__ = ["Session", "Credential", "ModelOption", "Upstream", "User", "Invite", "ACTIVE_STATUSES"]
