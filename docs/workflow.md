# 开发流程与测试流程：用了哪些工具和命令，各自做什么，得到什么结果

这篇讲两件事：**一个改动从想法到合进主干经过哪些步骤**，以及**每一步用什么工具、什么命令验证，达到什么结果**。
写的是实际发生过的做法，不是理想化的流程。早期仓库里没有 CI，检查都靠下面列的命令手动跑。现在有 GitHub Actions（`.github/workflows/ci.yml`，#52 / #92 / #98）：每个 PR 和 push 到 main 都跑核心测试和平台测试两个套件（PR 上 ubuntu 3.10 / 3.11 / 3.12 + macOS 3.12 + Windows 3.12，main 上 3 个系统 × 3 个 Python 版本），另有一个 ruff lint job（`E9,F`）。真实模型评测和浏览器验证仍要手动跑，结果写进文档和提交信息。

## 一、总览

```
需求 ──▶ 开分支 ──▶ 改代码 ──▶ 单元测试 ──▶ 文档一致性测试 ──▶ 真实环境验证 ──▶ 提交 ──▶ 推送 ──▶ 合并主干
                      ▲                                              │
                      └──────────── 失败：先写能复现的测试，再修 ◀────┘
```

几条一直坚持的规矩：

1. **每个改动一个分支**（`feat/…`、`fix/…`、`docs/…`、`perf/…`、`eval/…`），不直接在 main 上提交。
2. **先复现，再修**：修 bug 时先写一个在旧代码上会失败的测试，确认它失败，修完确认它通过。
3. **合并要确认**：分支测试全绿后，等作者说"合并"再合进主干（没有说合并的分支保留在远端）。
4. **写出来的数字要能核实**：测试数、通过率、性能数据都要能在仓库里重跑出来。

## 二、开发流程的每一步

### 1. 理解和定位（只读）

| 工具 | 用途 | 说明 |
|---|---|---|
| `Grep` / `Glob` | 找到相关代码和文档 | 比如改 `/mcp` 命令前，先搜 `handle_command`、`connect_servers` 的所有调用方 |
| `Read` | 读完整文件 | 改之前必须读过；改了要确认改对了位置 |
| `git log --oneline`、`git show <提交>` | 看历史决策和修复理由 | 写开发历程文档时，就是靠提交信息还原每个问题 |

**结果**：改动前知道影响面。比如改 `connect_servers` 的返回值时，搜出两个调用方（`session.py`、`tests/test_agent.py`），一起改掉，没有漏。

### 2. 开分支

```bash
git checkout -b feat/extensions-view        # 从 main 开新分支
git push -u origin feat/extensions-view      # 第一次推送，建立跟踪
```

**目的**：main 始终是可以运行、测试全绿的版本；改动可以单独回退。

### 3. 修改代码和文档

| 工具 | 用途 |
|---|---|
| `Edit` | 对已有文件做精确的字符串替换（必须先 `Read` 过） |
| `Write` | 新建文件，或整体重写一个文件 |
| `Bash` + Python 脚本 | 多处机械替换时，用 `python3 - <<'EOF' … EOF` 做带断言的替换（`assert 旧文本 in s`），替换不到就报错，不会静默改错 |

**注意**：用 `sed` 做多行或带特殊字符的替换容易出错（比如把 base64 调用改坏过），所以多行改动优先用 `Edit` 或带断言的 Python 脚本。

### 4. 跑测试（见第三节的详细说明）

最常用的命令（平台测试已随平台移到私有仓库）：

```bash
python3 -m unittest discover -s tests          # 核心：约 370 个测试
```

两条都要通过才提交。

### 5. 提交

```bash
git add -A
git commit -q -F - <<'EOF'
feat: 一句话说明改了什么

- 要点一
- 要点二

Co-Authored-By: …
Claude-Session: …
EOF
```

**约定**：提交信息写"为什么改"和"验证了什么"，不只写"改了哪些文件"。Trailer 行按仓库要求保留。

### 6. 推送与合并

```bash
git push -u origin <分支>      # 推送分支（网络失败最多重试 4 次，间隔 2/4/8/16 秒）
git checkout main
git merge --no-ff <分支>        # 保留分支的合并记录
python3 -m unittest discover -s tests   # 合并后再跑一次
git push origin main
```

**目的**：`--no-ff` 让历史里能看出每个功能是哪个分支、什么时候合进来的；合并后再跑一次测试，防止分支之间互相影响。

**远端限制**：删除远端分支被权限拒绝（403），所以已合并的分支留在远端，只在本地清理。

## 三、测试流程

测试分五层，从快到慢、从单元到真实环境：

| 层级 | 做什么 | 命令 | 什么时候跑 |
|---|---|---|---|
| 1. 单元测试 | 逻辑分支、边界、安全回归 | `python3 -m unittest discover -s tests` | 每次提交前 |
| 2. 平台测试 | 控制面、网关、沙箱、存储的端到端（进程内启动真实服务） | 在私有仓库 | 平台改动时 |
| 3. 文档一致性 | 代码里的参数、配置项、环境变量、命令、模块，文档里都有；文档里的数字和代码一致 | 包含在第 1 层里（`tests/test_docs_consistency.py`） | 每次提交前 |
| 4. 评测 | 真实模型完成 13 个编程任务，隐藏测试判定 | `python3 evals/agent_tasks/run.py …` | 改了循环、提示词、工具之后 |
| 5. 真实环境验证 | 真实模型、真实浏览器、真实 Docker、真实数据库 | 按 `verification/TASKS.md` 逐项做，结果写进 `verification/results/` | 大功能合并前、发布前 |

### 3.1 单元测试：`unittest discover`

```bash
python3 -m unittest discover -s tests -p "test_agent.py"   # 只跑一个文件
python3 -m unittest discover -s tests -v                   # 逐个显示
```

- **为什么用标准库 `unittest`**：项目零依赖，测试也不引入 pytest。
- **测什么**：Agent 循环的每个分支（用脚本化的假模型精确控制每一轮）、两种协议的流式解析、工具的参数校验、权限规则（路径绕过、复合命令、软链接）、会话保存与恢复、斜杠命令、MCP 客户端（用真实的子进程服务器）。
- **结果**：核心 371 个测试全部通过（开发历程里的 22 个问题，大多数都有对应的回归测试）。

### 3.2 平台测试（已移到私有仓库，以下为拆分前的记录）

```bash
python3 -m unittest discover -s server/tests
python3 -m unittest discover -s server/tests -p test_platform.py -k ListenBacklog   # 只跑一个用例
```

- **怎么测**：测试里真的启动控制面、网关和沙箱（本机进程启动器），用 HTTP 客户端像浏览器一样登录、建会话、发消息、读事件流。
- **需要外部服务的测试**：PostgreSQL、Redis、Docker 相关的用例由环境变量打开（`NCS_TEST_*`），默认跳过，所以平台测试显示 `OK (skipped=9)`。开启后用真实的 PostgreSQL、Redis 和 Docker 跑过。
- **结果**：72 个测试通过，9 个按条件跳过。

### 3.3 文档一致性测试

`tests/test_docs_consistency.py` 检查这些内容：

- 每个命令行参数、`settings.json` 配置项、环境变量、事件类型、斜杠命令，都在文档里出现；
- 每个源文件都在 README 的目录结构里；
- 每个章节都在学习路线里；
- README 里写的代码行数、测试数量和实际相差不超过约 10%（否则测试失败，要求同步更新）。

**目的**：文档和代码不会悄悄脱节。新增一个 `/mcp` 命令，如果忘了写进 README，测试会直接失败。

### 3.4 评测：`evals/agent_tasks`

```bash
python3 evals/agent_tasks/run.py --reference                   # 框架自检：参考答案应 13/13
python3 evals/agent_tasks/run.py --provider openrouter \
    --model deepseek/deepseek-v4-flash-0731 --repeat 3         # 真实模型，每题 3 次
python3 evals/agent_tasks/run.py --tasks T10 --repeat 5 --keep # 只跑一题，保留工作目录方便看
```

| 参数 | 作用 |
|---|---|
| `--reference` | 用参考答案跑，检查评测本身有没有问题（框架自检） |
| `--provider` / `--model` | 选真实模型；密钥从环境变量读取，不写进命令 |
| `--repeat N` | 每题重复 N 次。模型有随机性，单次结果不可靠 |
| `--tasks` | 只跑部分任务 |
| `--max-infra-errors` | 连续遇到接口错误（配额、网络）超过阈值就停止，避免浪费额度 |
| `--keep` | 保留每次运行的工作目录，方便看模型做了什么 |

**每个任务的判定**：任务目录里有隐藏测试（评测时才复制进去），模型只看得到题目说明和可见文件。通过 = 隐藏测试全部通过。

**关键设计**：区分"模型没做好"和"接口出错了"。接口出错（配额用完、网络断开）的运行不计入通过率，报告里单独提示。这是修过的一个问题（开发历程第 14 条），修之前一次配额用完的运行被记成了模型得 0 分。

**结果**：
- 框架自检：13/13。
- DeepSeek V4 Flash，`--repeat 3`：37/39（95%）。两次失败都是 T10，模型漏掉了一个边界类的变异体，已分析为模型能力问题。

### 3.5 真实环境验证：`verification/`

`verification/TASKS.md` 定义了 V01–V13 共 13 项验证，每项都要求在真实环境里做，并把结果写进 `verification/results/Vxx.md`，同时更新 `SUMMARY.md` 里的一行。

| 项 | 内容 | 状态 |
|---|---|---|
| V01 | 环境、单元测试、评测框架自检 | PASS |
| V02 | Mock 模型端到端（命令行） | PASS |
| V03 | Web 界面浏览器实操（Chromium） | PASS 8/8 |
| V04 | Web 安全：跨站请求、DNS 重绑定、令牌 | PASS 8/8 |
| V05 | 工作区信任 | PASS |
| V06 | 路径权限：`~`、软链接、`..` | PASS |
| V07–V10 | 真实模型：基础对话、修 bug、Anthropic 协议、检索与子 Agent | 部分 PARTIAL（外部条件限制，已记录原因） |
| V11 | RAG 向量与混合检索评测 | PARTIAL（缺 embedding Key，只完成 BM25 基线） |
| V12 | Agent 任务评测 | PASS 37/39 |
| V13 | 验证闭环：`--verify`、终审、子任务验收、经验候选 | PASS 5/5 |

**这些验证的特点**：验证人员（另一个 Agent 或人）不修改源码，只写结果；发现的 bug 写清复现步骤、实际输出和预期输出，由开发者修。

### 3.6 其他用到的验证工具

| 工具 / 命令 | 用在哪里 | 目的和结果 |
|---|---|---|
| `Chromium` + Python `playwright`（`executable_path=/opt/pw-browsers/chromium`） | 打开 Web 界面，点按钮，截图 | 确认新的「扩展」面板渲染正确；发现并修了会话标题显示成列表 repr 的问题 |
| `mermaid`（npm 装到临时目录，在 Chromium 里渲染） | 检查 README 里的架构图语法和布局 | 渲染后看图，发现模块图太乱，改成分层图 |
| `docker`、`docker stats`、`docker inspect` | 真实沙箱的启动、隔离、内存统计 | 验证沙箱断网、只能访问网关；压测时统计内存 |
| `docker run` 原始计时 | 区分是 Docker 本身还是平台的开销 | 20 个并行 `docker run` 用 2.0 秒，说明守护进程吞吐有限 |
| `vmstat 1` | 压测时观察整机 CPU、IO | 50 用户压测期间 CPU 平均约 50%，没有长期打满 |
| `curl` | 快速检查 HTTP 接口（`/api/state` 等） | 排查沙箱是否就绪 |
| `script -qc "…"` | 在伪终端里运行 CLI，保留颜色输出 | 生成终端截图用的真实运行记录 |
| `npm view <包> versions` / `pip index versions` | 选择要固定的 MCP 服务器版本 | 避免示例配置使用刚发布的版本 |
| `npx` / `uvx` | 启动官方 MCP 服务器（filesystem、memory、fetch） | 实测 `/mcp` 能列出它们的工具；发现首次启动需要下载，所以给了 `timeout` 配置 |

### 3.7 压测：`nanocode_server loadtest`（私有仓库）

```bash
python3 -m nanocode_server loadtest --users 10 25 50 100
```

- **做什么**：每档 N 个用户同时登录、添加 Key、建会话（启动沙箱）、发消息、等结束，分步计时，并统计沙箱内存。
- **目的**：回答"同时多少人能用、哪一步慢"。
- **结果**：10/25/50/100 个用户全部成功；建会话是主要瓶颈，随并发线性增长（约每个沙箱 0.24 秒）。
- **过程中修的问题**：监听队列默认只有 5，50 个并发时 17 个连接被重置。修复后有回归测试。
- **没做到的**：Kubernetes、gVisor、真实模型的并发限流、长时间运行。详见 `verification/results/perf-loadtest.md`。

### 3.8 演示命令：`nanocode_server demo`（私有仓库）

```bash
python3 -m nanocode_server demo --users 10
```

模拟多个用户同时使用，逐项检查隔离：每个人只能看到自己的会话和 Key，访问别人的会话返回 404，沙箱工作区里只有自己的文件，网关用对了每个人的 Key。
**结果**：10 个用户 51/51 项检查通过。

## 四、Git 和协作工具

| 工具 | 用途 |
|---|---|
| `git` | 分支、提交、合并、推送、查历史 |
| GitHub issue 评论 | 把复现结果和修复证据写进 issue（每条评论带生成来源的页脚），修复验证后关闭 issue |
| GitHub 的 MCP 工具 | 在对话里读取和评论 issue、PR（需要时才用，不主动创建 PR） |

## 五、这套流程达到的结果

| 指标 | 结果 | 怎么核实 |
|---|---|---|
| 核心单元测试 | 371 个通过 | `python3 -m unittest discover -s tests` |
| 平台测试 | 72 个通过，9 个按条件跳过（拆分前） | 私有仓库 |
| 评测框架自检 | 13/13 | `run.py --reference` |
| 真实模型评测 | 37/39（95%） | `verification/results/V12.md` |
| 真实环境验证 | V01–V13 中 9 项 PASS，其余 PARTIAL 并写明原因 | `verification/results/SUMMARY.md` |
| 多用户隔离 | 10 个用户 51/51 | `nanocode_server demo` |
| 压测 | 100 个用户同时全部成功 | `nanocode_server loadtest` |
| 修过的问题 | 开发历程里记录了 22 个问题，每个都对应提交；其中有回归测试的占多数，部分只有验证记录（如 Web 界面的小问题） | `docs/journey.md` 与 `git log` |

## 六、局限

- **CI 只跑单元测试**：两条 `unittest` 命令已接进 GitHub Actions；需要 PostgreSQL / Redis / Docker 的可选测试在 CI 里跳过，真实模型评测和浏览器验证也不在 CI 里。
- **真实模型测试有成本和随机性**：评测要花 API 额度，每题要重复多次才有参考价值。
- **真实环境验证大多是手动的**：V03、V04 等需要浏览器，无法在无头环境里全部自动化。
- **压测只覆盖本机 Docker**：Kubernetes 和 gVisor 下的启动时间没有测。
- **部分验证因为缺少 Key 而 PARTIAL**：比如 RAG 的向量检索只做了 BM25 基线。

如果你想复现核心的流程，最少只需要一条命令：

```bash
python3 -m unittest discover -s tests
```

通过，就说明代码和文档在当前状态下是一致的。
