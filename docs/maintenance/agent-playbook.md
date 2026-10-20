# 维护交接手册（Agent Playbook）

> **English summary.** This is the operational handoff playbook for maintaining `mbaiagent/nanocode`. It is written for an AI agent or a human taking over. It covers: (1) permission boundaries: the agent may review and merge clean PRs, open and evaluate issues, and open fix PRs, but must ask the owner before force-pushing or rewriting history, deleting anything (except deleting a PR's head branch after merging it), changing repo settings, or merging anything with architecture risk; (2) four automations: a weekday code-quality patrol, new-issue triage and fixing, PR review and squash-merge, and a weekly managed-Redis keepalive check; (3) the fix-PR workflow, with exact test commands, CI matrix quirks and gh quirks; (4) deployment and CD (in progress, PR #148); (5) reporting conventions; (6) a dated snapshot of open work; (7) lessons learned. **The repo is public: use placeholders only, never print secrets, and commit with a noreply email. Docs change in the same PR as code.**

本仓库是**公开仓库**。本文只用占位符：`nanocode.example.com`、`<platform-host>`、`<sandbox-host>`、`10.77.0.x`、`<db-host>`。真实主机、凭据和账号信息只放在服务器上的运维笔记里（root-only），不进仓库。

---

## 1. 角色与权限边界

### 1.1 Agent 可以自己做的事
- 审查 PR，**干净的**直接 squash 合并（标准见 §2.3）。
- 新开 issue，评估已有的 issue。
- 开修复 PR（不自己合并自己刚开的修复 PR，见 1.3）。
- 在 issue / PR 下评论，请求 Copilot review。

### 1.2 必须先问 owner 的事
- 删除任何东西之前先问（仓库、其他分支、release、tag、secrets、环境等），唯一例外：合并 PR 后删除该 PR 的 head 分支。
- `git push --force`、`--force-with-lease`、rebase 已推送的提交、改写历史（包括为清理隐私去改写历史，#121）。
- 改仓库设置：分支保护、Actions 权限、environments、secrets、可见性、协作者。
- 合并带架构风险的 PR，或带「需要人工决策」PR 标签，或标了「不要合并 / WIP」的 PR。
- 在生产上打开有安全前置条件的开关（例如出站代理要等 #134）。
- 一切对外可见、不可逆的操作：发消息、对外发帖等。

### 1.3 不许绕过 CI 自己批准自己
- 合并前 CI 必须全绿，本地也要跑全量测试。不用 admin 权限跳过检查。
- 修复 PR 由「PR 审查」流程按同一标准审，不因为作者是自己就放松。作者和审查者是同一个账号时，GitHub 不允许 request changes，改用普通评论，但标准不变。

### 1.4 公开仓库的隐私规则
- 只写占位符，不写真实 IP、域名（README 里的在线使用链接除外）、主机名、托管服务的账号 / ID、邮箱、GitHub 数字 id。
- 不打印、不记日志、不提交任何密钥。密钥文件（如 `~/.secrets/*.env`）只能 `source`，不能 `cat`。
- 提交邮箱用 noreply：
  ```bash
  git config user.name  "<github-login>"
  git config user.email "<github-id>+<github-login>@users.noreply.github.com"
  ```
- 提交前自查：`git diff --cached | grep -nE '([0-9]{1,3}\.){3}[0-9]{1,3}|@[a-z0-9-]+\.(com|net|cn)'`，把命中的逐条看一遍（文档占位网段 `203.0.113.x`、`198.51.100.x`、`10.77.0.x` 可以留）。

### 1.5 文档同步（owner 的规定）
改了行为、配置、环境变量或部署方式，**必须在同一个 PR 里**更新文档：`docs/configuration.md`（`NCS_*`）、根 README（`NANOCODE_*`，`tests/test_docs_consistency.py` 会检查）、相关的部署文档和 `CHANGELOG.md`。缺文档按问题处理，不合并。

---

## 2. 四个自动化

先准备一个通用的全新克隆（每次运行都重新 clone，不复用旧目录）：
```bash
WORK=/tmp/nc-$(date +%s); gh repo clone mbaiagent/nanocode "$WORK" -- -q && cd "$WORK"
```

### 2.1 每日代码质量巡检（工作日 09:00 PT）
**只读，不改代码。**
1. 全新克隆 main。
2. 看最近 1–3 个工作日的新提交：`git log --since="3 days ago" --stat`，逐个看 diff。
3. 按轮换深入一个模块（按日期轮流，记下上次巡检到哪里）：
   1. `nanocode/agent.py` + `nanocode/llm/`
   2. `nanocode/tools/`
   3. `nanocode/web/`
   4. 会话 + CLI（`nanocode/session*`、`nanocode/cli*`、`__main__`）
   5. `server/` 控制面（`web/`、`services/`、`storage/`、`security/`）
   6. `server/` 沙箱运行时（`runtime/`、沙箱代理、中继）
   7. CI 与测试（`.github/workflows/`、`tests/`、`server/tests/`）
4. 工具只用来找线索，结论必须读代码确认，并给出 `文件:行号`：
   ```bash
   pipx run ruff check <module>
   pipx run bandit -q -r <module>
   pipx run radon cc -s -n C <module>
   ```
5. 去重：搜索已开和已关的 issue，以及 `docs/`：
   ```bash
   gh issue list --state all --search "<关键词>" --limit 30
   grep -rn "<关键词>" docs/
   ```
6. 每次最多开 **5** 个 issue。中文标题，正文包含：现象、位置（`文件:行号`）、影响、复现或证据、修复建议。打标签 `security` / `performance` / `tech-debt`：
   ```bash
   gh issue create --title "fix(llm): ……" --body-file body.md --label tech-debt
   ```
7. 有新发现才汇报，没有就不出声。

### 2.2 新 issue 处理（工作日 09:30–17:30 PT，每 2 小时一次）
1. 选还没处理的 open issue：没有 `🤖 评估` 评论，也没有关联 PR。跳过跟踪类、需求类（`enhancement`、`[Analysis]`、`[Capacity]`）。每次最多 **3** 个。
   ```bash
   gh api "repos/mbaiagent/nanocode/issues/<N>/comments" -q '.[].body' | grep -c '🤖 评估'
   gh api "repos/mbaiagent/nanocode/issues/<N>/timeline" -q '.[]|select(.event=="cross-referenced")|.source.issue.pull_request.html_url'
   ```
2. 在全新克隆上复现，最好先写一个在 main 上会失败的测试。
3. **不成立**：评论 `🤖 评估：` 加证据（命令、输出、`文件:行号`），issue 保持打开，留给 owner 决定。
4. **成立**：
   1. 评论 `🤖 评估：已确认，……`。
   2. `git checkout -b fix/<N>-<topic> origin/main`，修复并加测试。
   3. 跑全量 core + server 测试（§3.3）。
   4. 正常 push，开中文 PR，正文写 `Fixes #<N>`、改了什么、为什么、测试结果。
   5. 在 issue 下评论 PR 链接，请求 Copilot review（§3.6），记录 CI 结果。
5. 不合并，不关 issue。改动大的（跨模块、改协议 / 数据结构、改安全边界）给 PR 加上「需要人工决策」标签（这是仓库里的 PR label）。

### 2.3 PR 审查与合并（PR 打开时触发）
1. **跳过**：draft、已关闭、带「需要人工决策」PR 标签或标了「不要合并 / WIP」的 PR（这类只评论，不合并）。
2. **代码质量**：可读性、重复、错误处理、边界条件、测试覆盖、是否符合现有架构（零第三方依赖、注释写「为什么」）。
3. **安全**：命令 / SQL 注入、路径穿越和符号链接、SSRF、鉴权绕过、密钥泄露（日志、错误信息、子进程环境）、XSS / CSP、CSRF、不安全的默认值、新增依赖。
4. **文档同步**（缺文档算问题）和**公开仓库隐私**（§1.4）。
5. **测试**：本地跑全量（§3.3），并等 CI 全部完成。PR 上是 8 个 job：core / server 各 4 个（ubuntu 3.11/3.12、macOS 3.12、Windows 3.12）。
   ```bash
   gh pr checks <N> --watch
   ```
6. **有问题**：用中文 request changes，逐条写 `文件:行号`。作者和审查者是同一账号时改用普通评论：
   ```bash
   gh api repos/mbaiagent/nanocode/pulls/<N>/reviews -f event=REQUEST_CHANGES -f body="$(cat review.md)"
   # 同一账号：
   gh api repos/mbaiagent/nanocode/issues/<N>/comments -f body="$(cat review.md)"
   ```
7. **干净**：
   ```bash
   SHA=<审查时看到的 head sha>
   gh api repos/mbaiagent/nanocode/issues/<N>/comments -f body="审查结论：……（中文）"
   [ "$(gh api repos/mbaiagent/nanocode/pulls/<N> -q .head.sha)" = "$SHA" ] || echo "head 变了，重新审"
   gh api -X PUT repos/mbaiagent/nanocode/pulls/<N>/merge -f merge_method=squash -f sha="$SHA"
   # 只有没有别的 open PR 以它为 base 时才删分支
   BR=$(gh api repos/mbaiagent/nanocode/pulls/<N> -q .head.ref)
   [ -z "$(gh pr list --base "$BR" --json number -q '.[].number')" ] && gh api -X DELETE "repos/mbaiagent/nanocode/git/refs/heads/$BR"
   ```
   **教训**：删掉 #124 的分支时，以它为 base 的叠加 PR #126 被 GitHub 自动关闭了。
8. 作者推了修复以后要**再审一次**：触发器只在 PR 打开时触发，后续 push 不会自动重审。

### 2.4 每周托管 Redis 保活检查（周一）
托管 Redis（Valkey）免费档长期没有访问会被停用，所以服务器上有一个 systemd 保活定时器。检查方法：
```bash
ssh <platform-host> 'systemctl list-timers "*keepalive*"; journalctl -u "ncs-redis-keepalive.service" --since "8 days ago" --no-pager | tail -n 50'
```
最近 7 天每次都成功、没有失败就不出声；有失败或定时器没在跑，就汇报给 owner，附上日志摘要（去掉主机名和连接串）。托管 Postgres **不做**保活，它要能缩到零省额度。

---

## 3. 修复 PR 工作流

### 3.1 分支命名
`fix/<issue>-<topic>`、`feat/<topic>`、`docs/<topic>`（另有 `perf/`、`ci/`）。每次都从最新的 `origin/main` 拉。

### 3.2 提交风格
- 标题：`fix(scope): 一句话说明 (#issue)`，scope 如 `llm`、`tools`、`web`、`server`、`security`、`cd`、`docs`。
- 正文写清楚改了什么行为、为什么、测试结果。合并时 squash。

### 3.3 跑测试（与 CI 一致）
两个套件**分开跑**，因为两个目录里都有 `test_security.py`：
```bash
PYTHONPATH=. python3 -m unittest discover -s tests -v
PYTHONPATH=server:server/tests:. python3 -m unittest discover -s server/tests -v
```
Windows 上 `PYTHONPATH` 的分隔符是 `;`。

### 3.4 冲突
```bash
git fetch origin && git merge origin/main   # 解决冲突，跑测试
git push                                     # 普通 push，绝不 force
```

### 3.5 CI 矩阵与坑
- PR 上：ubuntu 3.11/3.12 + macOS 3.12 + Windows 3.12，两个套件共 8 个 job；push 到 main 时是 3 个系统 × 2 个 Python 版本的完整矩阵。
- **Windows 环境变量不区分大小写**：`os.environ` 里的 `Path` 和 `PATH` 是同一个键，按大小写区分去清理或比较的测试会在 Windows 上失败（#137、#146）。
- Windows 没有 `os.killpg` / `SIGKILL`，要加 `os.name != "nt"` 判断。
- **Python 3.11 的 `http.client`** 和 3.12 行为不同（例如代理 / CONNECT 隧道相关的用例在 3.11 上失败，#137），本地最好两个版本都跑。

### 3.6 gh 的坑
`gh pr edit`、`gh issue view` 会因为 Projects (classic) 报错退出，改用 `gh api`：
```bash
gh api repos/mbaiagent/nanocode/issues/<N> -q '.title,.state,.body'
gh api -X PATCH repos/mbaiagent/nanocode/pulls/<N> -f body="$(cat body.md)"
gh api repos/mbaiagent/nanocode/issues/<N>/labels -f 'labels[]=security'
gh api repos/mbaiagent/nanocode/pulls/<N>/requested_reviewers -f 'reviewers[]=copilot-pull-request-reviewer[bot]'
```
Copilot review 也可以在网页上请求；API 请求失败不影响流程，记一笔就行。

---

## 4. 部署与 CD

### 4.1 拓扑（占位）
```
用户 ──HTTPS──▶ <platform-host>（控制面 + 模型网关 + 中继，nginx/Caddy）
                  │  WireGuard 10.77.0.1 ⇄ 10.77.0.2
                  ▼
               <sandbox-host>（sandbox-agent 只听 wg 地址；Docker 沙箱只能连中继）
<platform-host> ──TLS──▶ 托管 Postgres（<db-host>，可缩到零）
                ──TLS──▶ 托管 Redis/Valkey（周保活）
```
单机 `deploy/compose` 保留，用作开发和备用方案。详见 [distributed-deploy.md](../distributed-deploy.md)、[deployment-topology.md](../deployment-topology.md)。

### 4.2 CD（进行中：[PR #148](https://github.com/mbaiagent/nanocode/pull/148)）
- 设计：main 上 CI 全绿后，GitHub Actions 经 SSH 部署，只在 `production` environment 里运行，只允许 main 触发。
- 服务器上用专门的非 root 部署用户，`authorized_keys` 用 forced command，只能执行部署、查看状态、回滚这三个命令。服务器指纹写死在配置里。两台机器各用一把密钥，泄露了就删掉对应那一行。
- 服务器端会再确认提交确实在 main 上，代码只从 GitHub 拉。
- 合并前先在两台机器上手动完整部署一遍。

### 4.3 部署顺序、健康检查、回滚
1. **先沙箱机，后平台机**（新平台可能依赖新的沙箱代理接口）。
2. 每台部署完都跑健康检查：平台 `/healthz`，沙箱代理健康接口，再跑 `deploy/firewall/check-sandbox-isolation.sh`。
3. 检查不过自动回滚到上一个版本目录，保留最近 3 个版本。
4. 改配置前先备份 `.env` 和 nginx 配置（`cp x x.bak.$(date +%F)`）。数据库在托管服务上，依赖服务商的备份。
5. 在 #148 合并前，部署都是手动的：按服务器上运维笔记里的步骤执行。

### 4.4 凭据
主机地址、SSH 密钥、数据库连接串、OAuth 密钥只放在服务器上 root-only 的 `.env` 和运维笔记里。搬运密钥不经过聊天、日志和命令行参数。

---

## 5. 汇报约定
- 用简洁的中文向 owner 汇报，带 PR / issue 链接和合并 SHA（短 SHA 即可）。
- 没有变化就不出声（巡检和保活检查尤其如此）。
- 时间一律用太平洋时间（PT）。
- 需要 owner 决定的事情单独列出来，说明选项和推荐。

---

## 6. 当前状态快照（2026-10-19，`gh` 实时拉取，会过时）

**Open PR**
| PR | 状态 |
|---|---|
| [#148](https://github.com/mbaiagent/nanocode/pull/148) CD 经 SSH 部署 | 审查要求修改失败处理（坏版本目录、临时文件）；合并前要手动试部署 |
| [#147](https://github.com/mbaiagent/nanocode/pull/147) 敏感文件读取需确认 (#136) | 已改成「只能收紧」，等重审 |
| [#146](https://github.com/mbaiagent/nanocode/pull/146) 子进程不继承令牌 (#134) | 已按意见修改（本地保留厂商 key），等重审 |
| [#144](https://github.com/mbaiagent/nanocode/pull/144) 工作区快照（S3 兼容） | 修符号链接链逃逸、解压炸弹等问题中 |
| [#137](https://github.com/mbaiagent/nanocode/pull/137) 沙箱出站代理 | 修 3.11 / Windows 测试；默认关，生产上要等 #134 |
| [#128](https://github.com/mbaiagent/nanocode/pull/128) 本地 / 平台入口分离 | 之前 CI 失败过，待重审 |

**Open issue**
| Issue | 状态 |
|---|---|
| #145 面板 10s 轮询让 Postgres 缩不到零 | 待修 |
| #142 写文件非原子 | 已确认，待修 |
| #141 429 忽略 Retry-After | 已确认，待修 |
| #140 压缩失败逃出主循环 | 已确认，待修 |
| #139 流式中断不转 LLMError | 已确认，待修 |
| #138 回答代码块被 HTML 重复转义 | 待修，修好后重截 README 首图 |
| #136 敏感文件读取 | PR #147 |
| #134 沙箱内令牌 | PR #146 |
| #132 compose 外部 DB / remote 仍挂 docker.sock | 部分修复（DB 重试已在 #133） |
| #129 中继绑定应禁止 0.0.0.0 | 待修 |
| #122 .gitignore 未忽略 .env | 待修 |
| #121 历史里有真实邮箱 | 需要改写历史，**等 owner 决定** |
| #120 文档里有真实 IP | 当前文件已在 #143 清理，历史里仍有，等 owner 决定 |
| #115 审计日志在沙箱内可篡改 | 待设计（#135 已作为重复关闭） |
| #114 沙箱面板与控制面同源 | 改动大，排在 #134/#136 之后 |
| #113 转发时丢 CSP | 同上 |
| #111 / #110 / #109 / #105 | 需求 / 小 bug，未排期 |
| #72 / #68 / #66 | 评估类跟踪 issue，不进新 issue 流程 |

刷新快照：
```bash
gh pr list --state open; gh issue list --state open --limit 50
```

---

## 7. 经验教训
- **Neon 空闲轮询**（#131 → #133）：回收线程每 60 秒查一次库，托管 Postgres 一直缩不到零。修法是没有沙箱时完全不查库，有沙箱时只在接近回收时间时才查。网页每 10 秒轮询同样会唤醒数据库（#145）。连接串不要带 `options=endpoint`，加上 `connect_timeout`，启动时连库要带退避重试。
- **Docker 26 网络没有 Gateway 字段**（#130）：老版本 Docker 建的网络 inspect 不出网关，沙箱代理会反复重启。建网络时显式写网关，读不到就按网段第一个地址兜底。
- **中继防 DoS**（#126）：网关中继要有全局和每沙箱的连接上限，再加空闲超时（`NCS_AGENT_RELAY_*`）。中继和沙箱代理只能绑内网地址（#129）。
- **安全检查只能收紧**（#147）：新加的敏感文件检查排在原有判定前面，结果把 plan 模式下更严格的拒绝盖掉了（`cat .env && rm -rf src` 从拒绝变成可以批准）。新检查的结果只能和原判定取更严格的一个。
- **tar 符号链接链**（#144）：恢复归档时，两层符号链接串起来就能指到工作区外。要逐个解析成真实路径并确认在工作区内，同时限制解压后的总大小和文件数。
- **删 base 分支会关掉叠加 PR**（#124/#126）：删分支前先查 `gh pr list --base <branch>`。
- **沙箱内令牌**（#134）：只要有出站网络，一条 `env` 加一次 `curl` 就能把令牌外传。出站代理上线必须排在「子进程拿不到令牌」之后。
- **跨平台测试**：Windows 环境变量不区分大小写，Python 3.11 的 `http.client` 和 3.12 行为不同（#137）。
- **防火墙重载**：`firewall-cmd --reload` 会清掉 fail2ban 加的封禁规则，重载后要重启 fail2ban。
- **公开仓库**：示例配置里的管理员名单默认值要留空，否则有人照抄示例，同名用户第一次登录就成了管理员（#143）。
- **别在线上目录手动 clone**（CD 连续失败，2026-10-19）：有人在沙箱机的 `/opt/nanocode` 上直接 `git clone`，把版本软链接换成了没有 `.env` 的普通目录。部署脚本把它当成当前版本，新版本于是拿不到 `.env`，回滚目标也没有。现在脚本会从健康历史里补 `CARRY` 文件，补不到就拒绝部署。排查部署失败时，先看 `readlink /opt/nanocode` 和 `nanocode-deploy status`。
