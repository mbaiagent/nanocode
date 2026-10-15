"""nanocode 多用户平台：控制面 + 模型网关（docs/12 的方案 A：每个会话一个沙箱）。

分层（上层只依赖下层）：

    web/        HTTP：一个很小的路由框架，控制面 / 网关 / 反向代理三组路由
    services/   业务：模型目录、会话生命周期、上游模型客户端
    runtime/    沙箱启动器：本机进程 / Docker / Kubernetes
    storage/    持久化（SQLite / PostgreSQL）和限流（内存 / Redis）
    security/   签名令牌、API Key 加密、身份认证
    config.py   配置

只用标准库；PostgreSQL、Redis 是可选的，配了才需要对应的驱动。
"""

__version__ = "0.2.0"
