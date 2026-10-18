# AGENTS.md

给所有 AI agent（和人类贡献者）的项目指引。开工前读这个文件。

## 项目是什么

nanocode 是一个**零第三方依赖的 Python 教学型 Coding Agent**。目标是用纯标准库把 Coding Agent 的核心闭环跑通，作为理解 Claude Code 等工具底层逻辑的入门样板。

- **不是生产工具**：不要加重型依赖（httpx、pydantic、tiktoken、tree-sitter）。要加依赖先开 issue 讨论。
- **代码即文档**：每个模块顶部有注释解释设计决策，注释要写「为什么」和对应 issue 号，不是「做了什么」。
- **测试必须全过**：改任何代码后跑 `python -m unittest discover -s tests` 和 `python -m unittest discover -s server/tests`，两条都要过。

## 快速开始

```bash
# 不需要装任何依赖，Python 3.10+ 直接跑
PYTHONPATH=. python3 -m nanocode --help

# 跑测试
PYTHONPATH=. python3 -m unittest discover -s tests
PYTHONPATH=server:server/tests:. python3 -m unittest discover -s server/tests

# 启动 web 面板
PYTHONPATH=. python3 -m nanocode --web --port 8765
```

## 代码结构

```
nanocode/           核心 Agent 库
├── agent.py        主循环（消息循环、工具派发、压缩、权限拦截）
├── context.py      上下文管理（token 估算、压缩）
├── permissions.py  权限模型（default/accept_edits/plan/review/bypass）
├── tools/          工具实现（read/write/edit/bash/task/web_fetch 等）
├── llm/            OpenAI / Anthropic 协议适配
├── mcp.py          MCP 客户端
├── rag.py          本地检索
└── web/            单用户 web 面板

server/             多用户平台（nanocode_server）
├── nanocode_server/
│   ├── web/        HTTP 框架、控制面、模型网关
│   ├── services/   业务逻辑（catalog、sessions、upstream）
│   ├── storage/    SQLite / PostgreSQL、限流（内存 / Redis）
│   ├── runtime/     沙箱启动器（local / docker / k8s）
│   └── security/   令牌签名、SecretBox 加密
└── tests/          平台测试

docs/               教学文档（每章对应一个模块）
```

## 贡献规则

### 1. Worktree 隔离

**不要在主工作区 `/Users/mibao/dev/nanocode` 直接改代码**。每个 agent 用独立 worktree：

```bash
git worktree add /tmp/nc-<your-name> -b fix/<topic> origin/main
cd /tmp/nc-<your-name>
# 在这里改代码
```

### 2. 协作流程

- 开工前读 `docs/coordination.md`，确认 issue 没被别人认领
- 认领 issue 后在协作文件里登记
- 提 PR 后在对应 issue 下评论
- 互相 review：每个 agent 要 review 别人的 PR（轮值表见 coordination.md）

### 3. 提交规范

- 标题：`fix(scope): 一句话说明 (#issue号)`
- 描述：写清「改了什么行为、为什么、测试结果」
- Squash merge，保持 main 历史干净
- Breaking change 要在 PR 描述里写明迁移方式

### 4. 测试约定

- 核心测试在 `tests/`，平台测试在 `server/tests/`
- **分开跑**（两个目录有同名 `test_security.py`，一起跑会冲突）
- Windows 不支持的功能（如 `os.killpg`、`signal.SIGKILL`）要加 `os.name != "nt"` 判断，不要直接抛
- 安全相关改动必须加对抗测试（权限绕过、注入、路径遍历）

### 5. 安全红线

- 不要在代码里硬编码 API Key
- 用户 API Key 必须加密存储（`security/secretbox.py`）
- 权限规则是字符串匹配，不是沙箱——不要在文档里承诺「安全隔离」
- 云元数据地址（169.254.169.254）和内网地址要在 upstream 校验里拦截

## 不要做的事

- ❌ 加第三方依赖（先开 issue 讨论）
- ❌ 在主工作区改代码
- ❌ Force push 到 main
- ❌ 跳过测试直接提 PR
- ❌ 把教学项目当生产系统加 K8s/服务网格那套

## 更多

- 协作协议：`docs/coordination.md`
- 教学文档目录：`docs/`
- 架构总览：`docs/00-overview.md`（如果存在）
