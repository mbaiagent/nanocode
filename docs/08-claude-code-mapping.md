# 08 · 与 Claude Code 对照

nanocode 是依据 Claude Code 的**公开文档和可观察行为**写的教学实现，不来自其源代码。下表说明每个功能在两边分别是什么样子。Claude Code 的功能在持续更新，具体细节请以官方文档为准。

## 功能对照

| 概念 | Claude Code | nanocode | 源码位置 |
|---|---|---|---|
| 核心循环 | 模型 → 工具 → 结果 → 模型，直到不再调用工具 | 同 | `agent.py` |
| 读文件 | Read | `read_file`（带行号、分段） | `tools/files.py` |
| 写文件 | Write | `write_file` | `tools/files.py` |
| 编辑文件 | Edit（精确字符串替换，要求先读） | `edit_file`（同样的规则） | `tools/files.py` |
| 执行命令 | Bash | `bash` | `tools/shell.py` |
| 搜索 | Glob、Grep | `glob`、`grep` | `tools/search.py` |
| 待办清单 | TodoWrite | `todo_write` | `tools/planning.py` |
| 子 Agent | Task / Agent 工具，可在 `.claude/agents/` 自定义子 Agent | `task`（单一通用子 Agent） | `agent.py: _run_subagent` |
| 项目记忆 | `CLAUDE.md`（用户级 + 项目级，支持多层目录） | `NANOCODE.md` | `context.py: load_memory` |
| 初始化记忆 | `/init` | `/init` | `session.py` |
| 权限模式 | default / acceptEdits / plan / bypassPermissions | default / accept_edits / plan / bypass | `permissions.py` |
| 权限规则 | `settings.json` 中的 allow / deny，如 `Bash(npm run test:*)` | 同样的思路，`bash(npm run test*)` | `permissions.py` |
| 配置分层 | 用户 / 项目 / 本地 settings | 同 | `session.py: load_settings` |
| Hooks | PreToolUse、PostToolUse、UserPromptSubmit、Stop 等，退出码 2 阻断 | 这四个，同样的退出码约定 | `hooks.py` |
| 上下文压缩 | `/compact`，以及接近上限时自动压缩 | `/compact` 与自动压缩 | `context.py`、`agent.py` |
| 自定义命令 | `.claude/commands/*.md`，支持 `$ARGUMENTS` | `.nanocode/commands/*.md` | `context.py: discover_commands` |
| Skills | `SKILL.md`（name + description），按需加载 | 同样的结构 | `context.py`、`tools/planning.py` |
| MCP | stdio / HTTP，工具名 `mcp__服务器__工具` | 仅 stdio，同样的命名 | `mcp.py` |
| 非交互模式 | `claude -p "..."` | `nanocode -p "..."` | `cli.py` |
| 会话恢复 | `--resume` / `--continue` | `--resume`（恢复最近一次） | `session.py` |
| 中断 | Esc | Ctrl-C / Web 上的"中断"按钮 | `agent.py: cancel` |
| 多厂商模型 | 主要面向 Claude | 任意 OpenAI / Anthropic 兼容 API | `llm/` |

## nanocode 刻意省略的（生产级还需要什么）

这些是让 Agent 从"能跑"变成"好用、可靠、安全"的部分。每一项都值得单独研究：

**安全**
- **沙箱**：真正的命令隔离（文件系统、网络），而不是靠字符串匹配规则
- 更细的 bash 命令解析（识别管道、子命令、重定向），让权限规则不容易被绕过
- 敏感文件保护、网络访问控制

**能力**
- WebFetch / WebSearch 等联网工具
- 图片输入（截图、设计稿）
- Notebook 编辑、IDE 集成（读取诊断信息、显示 diff）
- 扩展思考（thinking）内容的处理
- 后台运行的长命令、多个并行会话

**体验**
- 差异预览：编辑前给用户看 diff
- 检查点与撤销：一键回滚 Agent 做的修改
- 更精确的 token 计数与费用统计
- 富文本终端界面（Markdown 渲染、语法高亮）

**工程**
- 遥测与错误上报
- 更完整的配置体系（企业策略、托管设置）
- 插件系统（把命令、技能、hooks、MCP 打包分发）

## 从 nanocode 的角度看 Claude Code 的设计取舍

读完 nanocode，再用 Claude Code，你会注意到很多设计背后的原因：

- **为什么 Edit 必须先 Read？** 防止基于过时内容的修改（第 03 章）。
- **为什么有 plan 模式？** 把"想"和"做"分开，让人在中间把关（第 04 章）。
- **为什么 CLAUDE.md 要分层？** 个人习惯、项目约定、子模块细节各有归属（第 05 章）。
- **为什么大搜索交给子 Agent？** 保护主上下文（第 06 章）。
- **为什么 Skills 只在提示词里放描述？** 渐进式披露，省上下文（第 06 章）。
- **为什么上下文接近满时会自动压缩，而不是报错？** 长任务的续航（第 05 章）。
- **为什么 Hook 用退出码 2 而不是 1？** 1 是很多命令的普通失败码，用 2 专门表示"有意阻断"，避免误伤。
