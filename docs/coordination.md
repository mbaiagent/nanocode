# 跨 Agent 协作协议（nanocode）

> 参与方：Claude Code、Codex CLI、WorkBuddy、DeepSeek harness、Doubao
> 仓库：mbaiagent/nanocode
> 本文件是**单一事实来源**，所有 agent 开工前必须读，收工后必须更新。

---

## 1. 核心原则

1. **Git 是唯一协调通道**：不依赖 IM、群聊或外部工具。分工、进度、冲突全在这个文件 + issue/PR 里。
2. **分支隔离**：每个 agent 用独立 worktree + 分支，**绝不碰主工作区**（clone 仓库的那个目录）。
3. **issue 认领即锁**：在本文件「认领表」写下 issue 号 + 分支名 + 预计完成时间，别人看到就不碰。
4. **开工前先同步**：每次开始工作前，`git pull` 本文件，确认 issue 状态没变。
5. **收工后更新**：提 PR 后立即在本文件标记完成，不留悬空状态。

---

## 2. Worktree 约定

| Agent | Worktree 路径 | 分支命名 |
|---|---|---|
| Claude Code | `/tmp/nc-claude` | `fix/claude-<topic>` |
| Codex CLI | `/tmp/nc-codex` | `fix/codex-<topic>` |
| WorkBuddy | `/tmp/nc-win45`、`/tmp/nc-launch` | `fix/wb-<topic>` |
| DeepSeek harness | `/tmp/nc-ds` | `fix/ds-<topic>` |
| Doubao | `/tmp/nc-main-agent` | `fix/doubao-<topic>` |

**开工前模板**（每个 agent 复制执行）：
```bash
cd <自己的 worktree>
git fetch origin && git rebase origin/main
# 读本文件，确认 issue 没被别人认领
# 如果别人已经在做，换一个或先评论问
git checkout -b fix/<自己>-<topic> origin/main
```

---

## 3. 冲突矩阵（改同一文件前先看这里）

| 目录 | 当前负责 | 谁能碰 |
|---|---|---|
| `nanocode/web/**` | WorkBuddy（#78 #5） | 其他人需先评论沟通 |
| `nanocode/tools/**`、`verify.py` | WorkBuddy（#45） | 同上 |
| `server/nanocode_server/security/**` | Doubao（#62 已完成） | 空闲 |
| `server/nanocode_server/storage/`、`config.py` | 无主 | 先到先得，认领后填表 |
| `server/tests/**`、`docs/**`、CI | 无主 | 同上 |
| `nanocode/agent.py`、`context.py`、`permissions.py` | 无主 | 同上 |

---

## 4. 认领表（实时更新）

| Issue | 认领人 | 分支 | 状态 | 预计完成 | 备注 |
|---|---|---|---|---|---|
| #52 | Doubao | `fix/docs-ci-52` | PR #81 待合并 | 已完成 | CI + 文档 |
| #62 | Doubao | `fix/secret-min-perm-62` | PR #82 待合并 | 已完成 | SecretBox AAD |
| #45 | WorkBuddy | `fix/windows-batch2` | 进行中 | 今天 | Windows 第二批 |
| #78 #5 | WorkBuddy | 待定 | 未开始 | 明天 | Web 权限模式 |
| #56 #65 | Doubao | — | 已评论分析 | — | 待后续增强 |
| #36 #58 #59 #60 #64 #70 | WorkBuddy | `fix/launcher-v3` | PR #80 待合并 | — | |
| #89 #90 | DeepSeek harness | `fix/markdown-xss` | **已合并（#91）** | 已完成 | Markdown 链接 XSS + 本地化 marked；CSP 收回 self |
| （无 issue，用户直派） | Muse | `fix/muse-web-login` | **已合并到 main（043e5e8）** | 已完成 | 自带登录页替代 nginx Basic Auth 原生弹窗；动了 `nanocode/web/server.py` + 新增 `static/login.html`，WorkBuddy 如动 web/ 请先同步 |

| （无 issue，用户直派） | Muse | `fix/muse-md-spacing` | **已合并到 main（4ab5434）** | 已完成 | 回复 markdown 排版收紧：标题/列表纵向 margin 太大显空行，h3 字号太小无层级 |

| （无 issue，用户直派） | Muse | `fix/muse-tool-history` | **已合并到 main（d51a05e）** | 已完成 | 刷新后恢复工具卡片：loadHistory 按块顺序交错渲染文本+tool_use 卡片（tool_result 按 id 配对）；权限卡片不恢复（无落盘数据） |

| #100 /model 运行时切换模型 | Muse | `fix/muse-model-cmd` | **已合并到 main** | 已完成 | 实现 /model 斜杠命令：set_model() 重建 provider（忙时/策略锁定拒绝、失败保留原 provider、用量重置） |

| 斜杠命令显示两次 | Muse | `fix/muse-dup-cmd` | **已合并到 main（af7d363）** | 已完成 | POST 的 r.output 和 SSE command 事件各渲染一次；改为只由 SSE 统一渲染 |

| .gitignore 补全 | Muse | `fix/muse-gitignore` | **已合并到 main** | 已完成 | 忽略根目录 /.nanocode/ 和 /NANOCODE.md（examples/ 示例不受影响） |
| #145 轮询缩容 | Muse | `fix/muse-poll-idle-145` | PR #156 待合并 | 已完成 | 自适应退避 10s→60s，Copilot review 已请求 |
| #129 中继 bind 校验 | Muse | `fix/muse-relay-bind-129` | PR #155 待合并 | 已完成 | NCS_AGENT_RELAY_BIND 拒绝 0.0.0.0/::，Copilot review 已请求 |
| #115 审计日志设计 | Muse | — | 已评论分析 | 已完成 | 设计方案+待决策问题已贴，issue 保持打开等 owner |


**空 issue 认领流程**：
1. 先看 issue 是否被 main 覆盖（`git log --oneline | grep issue号`）
2. 在本表加一行，写明分支和预计完成时间
3. 开工
4. 完成后把状态改成「PR #xx 待合并」或「已关闭」

---

## 5. 自动检查机制

每个 agent **每次启动新任务时**（不是每次工具调用）执行：

```bash
# 1. 拉最新协作文件
git fetch origin main
git show origin/main:docs/coordination.md > /tmp/nc-coord-latest.md

# 2. 对比自己上次记录的状态
#    - 如果自己认领的 issue 已经被别人合了，就换一个
#    - 如果别人认领的 issue 超时（超过预计完成时间 2 小时），评论问一下
#    - 如果有新 issue 没人认领，自己挑一个
```

**超时规则**：
- 认领后 **2 小时**没更新状态 → 别人可以接手，在本文件注明「接手自 xxx」
- PR 提了但 **24 小时**没人 review/合并 → 在对应 PR 评论 ping 一下
- 每天结束时（PDT 18:00）各 agent 自查一次本文件，清理过期认领

---

## 6. 提 PR 规范

1. **标题**：`fix(scope): 一句话说明 (#issue号)`
2. **描述**：写清「改了什么行为、为什么、测试结果」，不要只写 `fix #xx`
3. **测试**：必须跑 `tests/` 和 `server/tests/` 两条，分开跑（同名 test_security.py 会冲突）
4. **关联 issue**：在 PR 描述写 `Fixes #xx`，合并后自动关 issue
5. **合并方式**：squash merge，保持 main 历史干净

---

## 6.5 互相 Review 机制

每个 agent 不是写完 PR 就完事，还要 review 别人的 PR。这是分布式 code review，不需要人工。

### Review 轮值表（按 agent 轮换）

| 轮到谁 review | 规则 |
|---|---|
| **Claude Code** | review WorkBuddy 和 DeepSeek harness 的 PR |
| **Codex CLI** | review Doubao 和 Claude Code 的 PR |
| **WorkBuddy** | review Codex CLI 和 DeepSeek harness 的 PR |
| **DeepSeek harness** | review Doubao 和 WorkBuddy 的 PR |
| **Doubao** | review Claude Code 和 Codex CLI 的 PR |

每个人 review 另外两个人的 PR，形成闭环，没有人只写不审。

### Review 清单（必须逐项过）

1. **测试跑了没**：本地拉分支跑 `tests/` + `server/tests/`，不看作者说跑了就算过
2. **安全边界**：有没有新的注入点、权限绕过、路径遍历、密钥泄露
3. **breaking change**：有没有改 API、数据库 schema、加密格式——改了要写迁移说明
4. **注释风格**：这个仓库注释要解释「为什么」和对应 issue 号，不是「做了什么」
5. **回退成本**：出问题能不能一键 revert，有没有副作用

### Review 动作

- 通过：`gh pr review <n> --approve --body "本地验证：测试全过，xxx 点 OK"`
- 不通过：`gh pr review <n> --request-changes --body "问题：xxx，建议改成 xxx"`
- 评论讨论：直接 `gh pr comment <n>`

### 自动 Review 检查（每次开工前跑）

```bash
# 1. 看有没有需要自己 review 的 open PR
gh pr list --search "review-requested:@me" --state open

# 2. 如果有，先 review 完再开始自己的新 issue
# 3. 如果自己的 PR 24 小时没人 review，在轮值人对应的 PR 评论里 ping
```

**优先级**：review 别人的 PR 优先于开新 issue。代码审查是协作的一部分，不是额外负担。

---

## 7. 出问题怎么办

| 情况 | 处理 |
|---|---|
| 发现别人改了自己正在改的文件 | 不要 force push，在 PR 评论说明，rebase 解决冲突 |
| 自己的 worktree 被 reset 冲掉了 | 从 `git reflog` 找回，或从远程分支恢复 |
| issue 状态和代码不一致 | 以代码为准，在 issue 评论说明 |
| CI 红了 | 谁的 PR 谁修，不要推给别人 |
| 不确定 issue 该不该自己做 | 在 issue 下评论问，不要猜 |

---

## 8. 更新日志

- 2026-10-18：Doubao 完成 #52（PR #81）、#62（PR #82），评论 #56 #65
- 2026-10-18：WorkBuddy 提 PR #80（#36 #58 #59 #60 #64 #70）
- 2026-10-19：Muse 认领 #145、#129，评估 #115；#129→PR #155，#145→PR #156，#115 评论设计方案后保持打开
