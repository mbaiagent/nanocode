# 登录、邀请码与管理员（多用户平台）

> 适用于 `nanocode-server`（PR #118 起）。单机 `nanocode --web` 没有账号体系，对外暴露时用 `NANOCODE_HTPASSWD` 自带登录页或在反向代理上加认证，见 [README「安全」](../README.md#安全)。
> 相关变量见 [configuration.md 第 2 节](configuration.md#2-控制面与登录)；实现在 `server/nanocode_server/security/`（`auth.py`、`accounts.py`、`github.py`）和 `web/control.py`。

## 一、三种认证方式（`NCS_AUTH`，必须显式设置）

| 模式 | 适合 | 身份从哪来 | 限制 |
|---|---|---|---|
| `github` | 生产（单机 compose 默认） | GitHub OAuth，按 GitHub **数字 id** 绑定账号 | 需要 `NCS_GITHUB_CLIENT_ID` / `_SECRET` 和 https 的 `NCS_PUBLIC_URL`；第一次登录要邀请码 |
| `proxy` | 前面已有 SSO（oauth2-proxy 等，K8s 清单用它） | 认证代理传来的请求头（`NCS_AUTH_HEADER`，默认 `X-Auth-Request-Email`） | 必须配 `NCS_PROXY_SECRET`（≥16 字符），代理每个请求都要带上，否则谁都能伪造身份 |
| `dev` | 本机开发 | 登录页填任意用户名 | 只允许监听本机地址（`NCS_HOST` 是 `127.0.0.1` / `localhost` / `::1`），否则拒绝启动 |

没设 `NCS_AUTH`、或设了不认识的值，平台拒绝启动。

## 二、GitHub 登录（`NCS_AUTH=github`）

1. 在 GitHub 建 OAuth App：Settings → Developer settings → OAuth Apps → New OAuth App
   - Homepage URL：`https://nanocode.example.com`（你的 `NCS_PUBLIC_URL`）
   - Authorization callback URL：`https://nanocode.example.com/auth/github/callback`
2. 复制 Client ID，生成 Client Secret，**直接写进服务器上 root 只读（`chmod 600`）的 `.env`**：`NCS_GITHUB_CLIENT_ID`、`NCS_GITHUB_CLIENT_SECRET`。不要贴进聊天、终端回显或仓库。
3. `NCS_ADMIN_GITHUB_LOGINS` 填引导管理员（见下文）。

登录流程和安全设计：

- 只申请 `read:user user:email`，不读用户仓库；GitHub 的 access_token 换完资料就丢弃，不保存。
- OAuth 的 `state` 和 PKCE verifier 放在短命的签名 Cookie 里，回调时校验 `state`（防登录 CSRF）；邀请码也随这个 Cookie 带到回调。
- 账号 id 形如 `gh-<GitHub 数字 id>`，就是会话、Key、用量表里的 owner；用户在 GitHub 改 login 不影响。
- 登录成功后发服务端登录会话：Cookie 里是 32 字节随机令牌（HttpOnly、SameSite=Lax，https 下 `Secure` + `__Host-` 前缀），**库里只存它的 SHA-256**。有绝对有效期（`NCS_SESSION_TTL`，30 天）和闲置有效期（`NCS_SESSION_IDLE`，7 天）；退出或账号被停用立即失效。
- 登录相关接口（`/auth/github`、回调、`/api/login`）按 IP 限速：`NCS_LOGIN_PER_MINUTE`（默认 10 次 / 分钟），多副本时用 Redis 计数。前面有反向代理时要开 `NCS_TRUST_FORWARDED=1`（compose 已固定开启），否则所有人共用代理 IP 的额度。
- 失败会跳回 `/login?error=<代码>`：`state`（状态校验失败）、`denied`（用户在 GitHub 拒绝授权）、`github`（和 GitHub 通信失败）、`invite_required`、`invite_invalid`、`disabled`、`rate`。

离线冒烟可以用假的 GitHub：`python3 server/tests/fake_github.py 9999`，并把 `NCS_GITHUB_OAUTH_URL` / `NCS_GITHUB_API_URL` 指向 `http://127.0.0.1:9999`，见 [deploy/compose/README.md「本机试用」](../deploy/compose/README.md#本机试用没有域名)。

## 三、注册与邀请码

规则（`security/accounts.py`）：

1. 已经绑定过的 GitHub 账号直接登录（被停用的除外）。
2. 第一次登录的账号必须带**有效邀请码**（登录页填写），除非它在 `NCS_ADMIN_GITHUB_LOGINS` 里。
3. 邀请码无效、用完、过期、被撤销时拒绝，账号不会建出来。两个请求并发首次登录同一账号时，后到的那个会把邀请码次数退回去。

邀请码：

- 格式 `XXXX-XXXX-XXXX-XXXX`（16 位十六进制，约 64 位熵）。输入时忽略大小写和连字符。
- 库里只存哈希和末 4 位提示；**明文只在生成时显示一次**，丢了就撤销重发。
- 生成时可设：备注、可用次数（1–1000，默认 1）、有效天数（0–365，默认 7；0 = 不过期）。

## 四、管理员与 `/admin`

**引导管理员**：`NCS_ADMIN_GITHUB_LOGINS`（逗号分隔）里的账号第一次登录不需要邀请码，并直接成为管理员；已有账号后来被加进名单的，下次登录时升级为管理员。

- 推荐写 `id:<GitHub 数字 id>`。按 login 写时，如果这个 login 曾经属于另一个已注册的 GitHub 账号（原主人改名后被别人抢注），**不会**给管理员。
- `dev` / `proxy` 模式下，名单按用户名 / 代理给的身份（如邮箱）匹配。
- 目前没有在页面上把普通用户提升为管理员的功能，管理员只能通过这个名单产生。

**管理页 `/admin`**（主界面左下角钥匙图标），只有管理员能打开，对应接口都在 `/api/admin/*`：

| 操作 | 接口 |
|---|---|
| 列出邀请码（状态、已用次数、过期时间、备注） | `GET /api/admin/invites` |
| 生成邀请码（`note`、`max_uses`、`expires_days`） | `POST /api/admin/invites` |
| 撤销邀请码 | `POST /api/admin/invites/<id>/revoke` |
| 列出账号 | `GET /api/admin/users` |
| 停用 / 启用账号（`status: disabled / active`） | `POST /api/admin/users/<id>/status` |

停用账号会：立即作废他所有登录会话、停掉他正在运行的沙箱（网关令牌随之失效）。不能停用自己，也不能停用管理员。

**老数据**：schema v2 把之前 `dev` / `proxy` 模式下出现过的用户名回填成 `legacy` 账号，会话、Key、用量保留；它们**不会**和 GitHub 账号自动按名字合并（有冒领风险）。

## 五、模型与 Key：用户自带（BYO Key）

平台的模型目录有三种来源（`services/catalog.py`）：

| 来源 | 引用写法 | 谁付费 | 限额 |
|---|---|---|---|
| 演示模型 | `mock` | 不调外部 API | — （`NCS_ENABLE_MOCK_MODEL=false` 可关） |
| 平台模型 | `p:<模型>` | 平台（`NCS_PLATFORM_*`，可不配） | 每人每日 `NCS_DAILY_TOKENS`、每分钟 `NCS_RPM` |
| 用户自己的 Key | `c:<id>:<模型>` | 用户自己 | 只计量；每分钟 `NCS_RPM` 仍生效 |

用户在主界面"模型与密钥"里添加 Key：选厂商预设（DeepSeek、OpenAI、Anthropic、通义千问、Kimi、智谱、硅基流动、OpenRouter）或"自定义（OpenAI / Anthropic 兼容）"，可以先"测试"再保存；每个会话可以随时换模型（换模型会重写策略并重启沙箱，对话保留）。

- Key 用 `NCS_SECRET_KEY` 派生的密钥加密存库（绑定 owner，换了主密钥就解不开），页面上只显示末几位提示。
- **真实 Key 永远不进沙箱**：沙箱里拿到的是网关令牌（HMAC 签名、有过期时间、绑定会话和本次启动的代次），只能调平台的模型网关；网关按令牌查出会话、模型和该用哪把 Key，解密后注入上游请求。
- 自定义地址默认只允许 https 公网地址，并固定已检查过的解析 IP（防 SSRF / DNS 重绑定）；`NCS_ALLOW_PRIVATE_UPSTREAMS=1`（只限 `dev`）才允许本机 / 内网（如本机 Ollama）。
- 已知不足：沙箱里的网关令牌和 `NANOCODE_WEB_TOKEN` 在环境变量里，沙箱内任何子进程都能读到，见 [security-model.md](security-model.md) 和 [#134](https://github.com/mbaiagent/nanocode/issues/134)。

## 六、运维小抄

- 第一次上线：管理员登录 → `/admin` 生成邀请码 → 发给被邀请人 → 他在登录页填邀请码后点"使用 GitHub 登录"→ 在"模型与密钥"里加自己的 Key。
- 怀疑登录会话泄露：停用再启用该账号（作废全部登录会话）。
- 轮换 GitHub Client Secret：GitHub 上生成新 Secret → 写进服务器 `.env` → 重启平台 → GitHub 上删掉旧 Secret。已登录用户不受影响（登录会话在平台侧）。
- **不要轮换 `NCS_SECRET_KEY`**，除非接受所有用户重新填 Key（库里加密的 Key 会解不开，网关令牌也会全部失效）。
