# 06 · 待办、子 Agent、Skills、自定义命令

> 对应源码：`nanocode/tools/planning.py`、`agent.py` 中的 `_run_subagent`、`subcheck.py`、`context.py` 中的 `discover_*`

这几个功能不直接改变外部世界，它们改变的是 Agent **怎么组织工作**。

## 待办清单：todo_write

长任务里，模型容易"走着走着忘了还剩什么"。尤其是做了很多步、上下文里塞满了工具输出之后，最初的计划被埋在很前面。

`todo_write` 让模型把计划写成清单：

```json
{"todos": [
  {"content": "定位登录失败的原因", "status": "completed"},
  {"content": "修复 token 过期判断", "status": "in_progress"},
  {"content": "补充单元测试", "status": "pending"},
  {"content": "运行全部测试", "status": "pending"}
]}
```

它的巧妙之处：

1. **清单会回到上下文**：工具结果里包含最新清单，所以清单总是出现在上下文的较新位置，模型每次更新都等于"复习"了一遍计划。
2. **每次传完整清单**：不需要"添加/删除/修改某项"这样复杂的接口，模型直接重写整个列表，简单可靠。
3. **用户能看到进度**：Web 面板「待办」标签实时显示。

它本质上是给模型一块"外部草稿纸"。实现只有二十几行，但对长任务的完成率影响很大。

## 子 Agent：task

### 问题：上下文污染

"这个项目里所有调用 `send_email` 的地方，哪些没有做异常处理？"

要回答这个问题，Agent 可能要 grep 一次、读 15 个文件。这 15 个文件的内容全部进入上下文，但主任务真正需要的只是最后那句结论。

### 解决：开一个新的上下文

```
主 Agent 上下文：
  user: 帮我给所有 send_email 调用加上重试
  assistant: tool_use(task, prompt="找出所有调用 send_email 且没有异常处理的位置，返回文件和行号")
  user: tool_result("共 4 处：a.py:12, b.py:40, c.py:7, d.py:88")   ← 只有结论进来了
  assistant: 好的，我逐个修改……

子 Agent 上下文（用完即丢）：
  user: 找出所有调用 send_email 且没有异常处理的位置……
  assistant: tool_use(grep) → 结果 → tool_use(read_file a.py) → ... 15 次读文件 ...
  assistant: 共 4 处：a.py:12, ...
```

看 `_run_subagent()` 的实现：

- 新建一个 `Agent`，**messages 是空的**，只放入任务说明；
- 工具集去掉 `task`（防止无限套娃）和 `todo_write`；
- 系统提示词告诉它"你是子 Agent，你看不到主对话，最终回复是主 Agent 唯一能看到的内容"；
- **共用同一个权限管理器**：子 Agent 的危险操作同样需要确认；
- 共用同一个取消信号：用户按 Ctrl-C，子 Agent 也停；
- 子 Agent 的 token 用量计入总用量。

### `task` 工具的参数

| 参数 | 必填 | 说明 |
|---|---|---|
| `description` | 是 | 3~8 个字的任务简述 |
| `prompt` | 是 | 交给子 Agent 的完整任务说明 |
| `check` | 否 | 检查命令。子 Agent 汇报后由系统运行，不通过就让它接着修（最多重做 2 次）；和 `bash` 一样要过权限 |
| `allowed_paths` | 否 | 允许修改的路径（相对工作目录，支持目录前缀和 `*`）；空列表 = 只读任务 |

### 验收：不要只信子 Agent 的自述

子 Agent 的结论后面总会附一份**系统生成**的验收报告：改了哪些文件、有没有超出 `allowed_paths`、`check` 有没有通过（没配则标明"未经验证"）。写文件工具越界会被直接拒绝；`bash` 造成的越界靠前后文件快照对比发现，并让子 Agent 还原。这样主 Agent 能区分"核对过的事实"和"子 Agent 的一面之词"。原理、局限和测试方法见第 11 章。

### 写好任务说明

子 Agent 看不到主对话，所以 `task` 工具的说明里强调："prompt 要写清楚任务目标和希望返回什么"。这和你给同事派活一样：上下文给够、交付物说清楚。

### 并行子 Agent

模型在一次回复里发出多个 `task` 调用时，因为 `task` 被标记为只读，它们会**并行**执行。可以让多个子 Agent 同时调研不同模块。（思考：子 Agent 内部可能执行写操作，标成只读安全吗？这里依赖的是子 Agent 内部每个工具调用仍然单独走权限检查。）

## Skills：按需加载的说明书

### 问题

你有很多领域知识想教给 Agent：怎么写这个项目的数据库迁移、怎么生成 PDF 报告、公司的代码审查清单……全放进系统提示词？每次请求都带着几万字，又贵又干扰注意力。

### 渐进式披露

```
系统提示词里只放目录（几十个字）：
  - db-migration：编写和执行数据库迁移
  - pdf-report：生成带图表的 PDF 报告

模型判断当前任务需要 → 调用 skill(name="db-migration") → 完整说明（可能几千字）进入上下文
```

一个 Skill 就是一个目录：

```
.nanocode/skills/db-migration/
├── SKILL.md          ← frontmatter 写 name 和 description，正文是完整说明
├── template.sql      ← 说明里可以引用目录下的其他文件，模型需要时再 read_file
└── check.py
```

```markdown
---
name: db-migration
description: 编写和执行数据库迁移
---
# 数据库迁移流程
1. 在 migrations/ 下新建文件，命名为 YYYYMMDD_描述.sql，参考同目录的 template.sql
2. ……
```

这是三层披露：目录（总是在）→ SKILL.md（需要时加载）→ 附属文件（更需要时再读）。

## 自定义斜杠命令

把常用的提示词存成文件，用 `/名字 参数` 调用：

```markdown
<!-- .nanocode/commands/review.md -->
---
description: 审查指定文件的代码
---
请审查 $ARGUMENTS 的代码，重点关注：错误处理、边界条件、命名、是否有重复代码。
按严重程度列出问题，给出具体的修改建议。
```

`/review src/auth.py` → 替换 `$ARGUMENTS` → 作为普通消息发给 Agent。

Skill 和命令的区别：**命令是人主动触发的**（你知道要做什么，懒得每次打字）；**Skill 是模型自己判断后加载的**（模型知道有这项能力，需要时才学）。

`examples/project/.nanocode/` 下有一个可用的命令和技能示例。
