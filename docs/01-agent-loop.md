# 01 · Agent 循环

> 对应源码：`nanocode/agent.py`

## 聊天机器人 vs Agent

聊天机器人：你说一句，它回一句。模型只产生文字。

Agent：你给一个目标，它**自己决定**下一步做什么、调用什么工具、看到结果后再决定下一步，直到它认为目标完成。模型产生的不只是文字，还有"动作"。

两者用的是同一个模型、同一个 API。区别只在于外面有没有一个循环，以及有没有给模型工具。

## 最小的 Agent

去掉所有细节，`agent.py` 的核心是这样：

```python
messages = [{"role": "user", "content": "帮我修复测试"}]

while True:
    reply = model(system, messages, tools)       # ① 调用模型
    messages.append(reply)                       # ② 记下模型说了什么
    calls = [b for b in reply.content if b.type == "tool_use"]
    if not calls:                                # ③ 没有工具调用 = 模型认为完成了
        return reply.text
    results = [run(c) for c in calls]            # ④ 执行工具
    messages.append({"role": "user", "content": results})   # ⑤ 把结果交还模型
```

这十几行就是所有编程 Agent（包括 Claude Code）的心脏。其余一切都是围绕它加的：

| 附加能力 | 解决什么问题 | nanocode 中的位置 |
|---|---|---|
| 流式输出 | 用户不用干等，能看到模型边想边写 | `on_text` 回调 → `text_delta` 事件 |
| 权限检查 | 模型可能误操作或被注入攻击 | `_prepare` → `permissions.resolve` |
| Hooks | 插入确定性规则（格式化、审计、拦截） | `_prepare` / `_execute` / `_run` |
| 并行执行 | 多个只读操作同时跑，更快 | `_run_tools` 中的线程池 |
| 输出截断 | 防止一个大文件撑爆上下文 | `truncate()` |
| 自动压缩 | 长任务不会因上下文满了而中断 | `_maybe_compact` |
| 中断 | 用户发现方向不对可以随时叫停 | `self.cancel` 事件 |
| 步数上限 | 防止模型陷入死循环烧钱 | `max_steps` |
| 子 Agent | 把子任务放到独立上下文里做 | `_run_subagent` |

## 循环什么时候结束？

**由模型决定。** 当模型的回复里不再包含工具调用时，循环结束。在 API 层面，这表现为 `stop_reason` 是 `end_turn`（OpenAI 协议中是 `finish_reason: "stop"`），而不是 `tool_use`。

这是 Agent 设计里最重要的一个观念转变：程序不规定流程，不写"先搜索、再读文件、再修改"这样的步骤，而是把工具和目标交给模型，让模型自己规划。程序只负责：执行、反馈、兜底。

兜底的情况有三种：用户中断（`(已中断)`）、达到最大步数、模型调用失败。

## 工具结果为什么是 `user` 角色？

```json
{"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "bash", "input": {"command": "pytest"}}]}
{"role": "user",      "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "2 failed, 5 passed"}]}
```

对模型而言，对话只有两方：它自己，和"外部世界"。工具的输出是外部世界的反馈，所以归在 user 一侧。`tool_use_id` 把每个结果和对应的调用配对。

**这带来一个硬约束**：每个 `tool_use` 之后，下一条 user 消息里必须有同 id 的 `tool_result`，否则 API 会报错。nanocode 在各种异常路径上都维护了这个约束：

- 工具不存在 → 返回 `is_error: true` 的结果
- 参数不合法 → 返回错误结果
- 权限被拒 → 返回错误结果（"用户拒绝了这次操作……"）
- 用户中断 → 剩下的工具返回"用户中断了操作，工具未执行"

看 `test_agent.py` 里的 `test_cancel`，它专门验证中断后消息列表依然合法。

## 错误是给模型看的

注意 `Tool.execute()`：任何异常都被捕获，变成一段文字作为工具结果返回。Agent 永远不会因为工具出错而崩溃，模型会读到错误信息，然后自己修正。例如 `edit_file` 找不到要替换的文本时，返回：

> 在文件中找不到 old_string。请确认缩进和空白完全一致，必要时重新 read_file。

这句话是写给模型看的"操作指南"。**错误信息的质量直接影响 Agent 的自我纠错能力。**

## 一次完整运行的事件序列

打开 `--trace` 运行一次，然后看 `.nanocode/traces/*.jsonl`：

```
user            "把 README 里的错别字修一下"
llm_request     step=1  context_tokens=2100
llm_response    stop_reason=tool_use  content=[text, tool_use(read_file)]
tool_call       read_file {"path": "README.md"}
permission      allow  "只读工具"
tool_result     "1\t# 项目标题\n2\t这是一个示列项目..."
llm_request     step=2
llm_response    stop_reason=tool_use  content=[tool_use(edit_file)]
tool_call       edit_file {...}
permission      allow  "用户允许"
tool_result     "已修改 README.md（替换 1 处，约第 2 行）"
llm_request     step=3
llm_response    stop_reason=end_turn  content=[text("已把"示列"改为"示例"。")]
turn_end
```

三圈循环、两次工具调用、一次权限确认。

## 思考题

1. 如果模型在同一个回复里先输出文字、再调用工具、然后又输出文字，`text_of()` 会返回什么？这对 CLI 显示有什么影响？
2. `max_steps` 设成多少合适？太小和太大分别有什么问题？
3. 为什么 `_run_tools` 先依次做完所有权限检查，再统一执行，而不是检查一个执行一个？（提示：并行执行时，用户确认对话框怎么办？）
