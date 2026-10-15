# nanocode-server：多用户部署的控制面和模型网关

把单用户的 nanocode 变成给上千人用的服务。设计见 [第 12 章](../docs/12-distributed-deployment.md)：
**每个会话一个沙箱**，沙箱里跑几乎不改的 `nanocode --web`；这个包负责沙箱外面的一切。

```
浏览器 ──HTTPS──▶ 入口 ──▶ 认证代理（oauth2-proxy，对接 SSO）──▶ 控制面 ──转发──▶ 用户自己的沙箱（nanocode --web）
                                                                │                    │
                                                         启动 / 回收沙箱          调模型（只带网关令牌）
                                                         （K8s + gVisor）             ▼
                                                                              模型网关（真实 Key 在这里）──▶ 模型厂商
```

和 nanocode 一样只用标准库。

## 组件

| 模块 | 作用 |
|---|---|
| `app.py` | 控制面：会话管理、按用户授权（别人的会话一律 404）、总容量和每人上限、把请求和事件流转发到沙箱、空闲回收 |
| `auth.py` | 用户身份：信任认证代理传来的请求头，并校验共享密钥 `X-NCS-Proxy-Secret`，防止绕过代理伪造身份 |
| `launcher.py` | 沙箱启动器：`LocalLauncher`（本机子进程，**没有隔离**，只用于开发和测试）、`KubernetesLauncher`（每会话一个 Pod，gVisor、非 root、只读根文件系统、不挂载集群凭证、每会话一个持久卷） |
| `gateway.py` | 模型网关：校验网关令牌、强制模型和输出上限、每人每分钟限流、每人每日 token 限额、流式转发并计量 |
| `tokens.py` | 网关令牌：HMAC 签名、带过期时间，网关不查数据库就能验证 |
| `store.py` | SQLite 存储：会话表（控制面）、用量表（网关） |
| `config.py` | 全部配置来自 `NCS_*` 环境变量，启动前做安全检查（配错了宁可起不来） |

nanocode 本体为此做的改动（都在 `nanocode/` 里，单机使用不受影响）：

- `policy.py`：平台策略。沙箱里的配置文件 Agent 能改，所以平台把策略只读挂进去（`NANOCODE_POLICY`），锁定模型去向、限制权限模式、删除危险配置，优先级高于一切；
- `session.py`：主 Agent 每一步调用模型前自动保存，沙箱被回收或节点故障最多丢一步；
- `web/server.py`：令牌可由控制面注入（`NANOCODE_WEB_TOKEN`）；事件流带 `id`，重连时按 `Last-Event-ID` 只补发没收到的；
- `web/static/index.html`：接口改用相对路径，页面挂在 `/s/<会话>/` 下也能用；
- `events.py`：审计日志（`AuditWriter`），只记录谁让 Agent 做了什么，不记录模型输出全文。

## 本机试用

```bash
# 不需要任何 Key：沙箱用 mock 模型
PYTHONPATH=server:. python3 -m nanocode_server dev
# 打开 http://127.0.0.1:8700 ，新建会话

# 接真实模型：网关和控制面在同一个进程里
NCS_UPSTREAM_BASE_URL=https://api.deepseek.com/v1 NCS_UPSTREAM_API_KEY=sk-... NCS_MODEL=deepseek-chat \
  PYTHONPATH=server:. python3 -m nanocode_server dev
```

`dev` 模式用本机子进程当沙箱，**Agent 执行的命令直接跑在你的电脑上**，只能自己开发用。

## 测试

```bash
python3 -m unittest discover -s server/tests
```

包括：令牌和认证、网关（含真实模型 API 常见的 HTTP/1.1 分块 + 保持连接）、K8s 启动器（假的 API 服务器）、
控制面端到端（真的启动 nanocode 沙箱，两个用户互相访问、审批经代理转发、停止后恢复对话、空闲回收），
以及部署清单和代码的一致性。

## 生产部署（Kubernetes）

清单在 [`deploy/k8s/`](../deploy/k8s/)，镜像在 [`deploy/`](../deploy/)：

1. 构建并推送两个镜像：`deploy/Dockerfile.sandbox`（沙箱）、`deploy/Dockerfile.server`（控制面和网关共用）；
2. 节点装好 gVisor（`runsc`），集群网络插件支持 NetworkPolicy；
3. 按 `40-secrets.example.yaml` 创建密钥（不要把真实值提交进仓库）；
4. 改 `50-control-plane.yaml`、`60-gateway.yaml` 里的镜像、模型、额度；改了网关地址、模型或权限模式，
   用 `python -m nanocode_server policy` 重新生成 `20-sandbox-policy.yaml`（`server/tests/test_deploy.py` 会检查两边一致）；
5. 按你的 SSO 改 `70-oauth2-proxy-ingress.example.yaml`；
6. `kubectl apply -f deploy/k8s/`。

## 主要配置

| 变量 | 说明 | 默认 |
|---|---|---|
| `NCS_TOKEN_SECRET` | 控制面和网关共用，签发 / 校验网关令牌，至少 32 个字符 | 必填 |
| `NCS_PROXY_SECRET` | 认证代理必须带上的共享密钥（生产必填） | — |
| `NCS_DEV_USER` | 仅本机开发：本机请求都当成这个用户 | — |
| `NCS_AUTH_HEADER` | 认证代理放用户身份的请求头 | `X-Auth-Request-Email` |
| `NCS_LAUNCHER` | `local` / `k8s` | `local` |
| `NCS_MAX_SANDBOXES` / `NCS_MAX_SESSIONS_PER_USER` | 总容量 / 每人同时运行的会话数 | 200 / 3 |
| `NCS_IDLE_TIMEOUT` | 空闲多少秒回收沙箱（工作区保留） | 1800 |
| `NCS_GATEWAY_URL` / `NCS_MODEL` | 沙箱访问网关的地址 / 使用的模型 | — |
| `NCS_SANDBOX_MODES` / `NCS_SANDBOX_MODE` | 沙箱里允许的权限模式 / 启动模式；`local` 启动器不允许 `bypass` | 除 bypass 外全部 / `default` |
| `NCS_TOKEN_TTL` | 网关令牌有效期，也是沙箱最长存活时间（秒） | 43200 |
| `NCS_UPSTREAM_BASE_URL` / `NCS_UPSTREAM_API_KEY` / `NCS_UPSTREAM_PROTOCOL` | 网关的上游模型 API | — / — / `openai` |
| `NCS_DAILY_TOKENS` / `NCS_RPM` / `NCS_MAX_OUTPUT_TOKENS` | 每人每日 token 上限 / 每分钟请求上限 / 单次输出上限 | 2000000 / 30 / 8192 |
| `NCS_K8S_*` | 命名空间、镜像、运行时、CPU / 内存、工作区大小、策略 ConfigMap，见 `config.py` | — |

完整列表和说明见 `config.py`。

## 局限（如实说明）

这是第 12 章阶段 1"多用户最小可用版"的实现，下面这些**还没做**：

- **单副本**：会话表和用量表在 SQLite 里，控制面和网关都只能跑一个副本，重启期间服务中断。
  多副本需要换成 PostgreSQL（会话、用量）和 Redis（限流计数），接口已经收拢在 `store.py`；
- **容量满时直接拒绝**，没有排队和排队位置显示；
- **审计日志写在沙箱里**（`/data/home/audit.jsonl`），沙箱里的 Agent 能改它。可信的审计需要从沙箱外收集
  （例如把事件推到日志系统）；控制面和网关目前只记录会话和用量；
- **没有指标、链路追踪和告警**，没有压测数据；
- **K8s 清单没有在真实集群里跑过**：YAML 能解析、和代码一致性有测试，Pod 创建流程用假的 API 服务器测过，
  但 gVisor、NetworkPolicy、oauth2-proxy 的实际效果需要在你的集群里验证；
- 沙箱镜像没有装包镜像代理，出口白名单默认只放行 DNS 和网关，Agent 装不了依赖，需要按你的环境加规则；
- 没有文件上传下载、没有 git 凭证管理、没有计费和管理后台。
