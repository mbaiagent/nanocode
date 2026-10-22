# 02 · 消息格式与模型适配

> 对应源码：`nanocode/messages.py`、`nanocode/llm/`

## 为什么要有"统一格式"

各家 API 表达同一件事的方式不同。Agent 循环如果直接写死某一家的格式，换模型就要改核心代码。所以 nanocode 分了两层：

```
Agent 循环（只认统一格式）
      │
      ▼
Provider.complete(system, messages, tools) -> LLMResponse
      │            ↑ 统一格式进            ↑ 统一格式出
      ▼
各家 API（各自的格式）
```

新增一家模型 = 写一个新的 Provider（或者在预设表里加一行），Agent 核心一行不改。这就是"可接入任何一家 API"的全部秘密。

## 统一格式

nanocode 选用"内容块"格式（和 Anthropic 的格式基本一致），因为它把文本、工具调用、工具结果放进同一种结构：

```python
{"role": "assistant", "content": [
    {"type": "text", "text": "我先看看"},
    {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "a.py"}}
]}
{"role": "user", "content": [
    {"type": "tool_result", "tool_use_id": "t1", "content": "1\tprint(1)", "is_error": False}
]}
```

模型调用的返回值也被统一成：

```python
LLMResponse(content=[...内容块...], stop_reason="tool_use" | "end_turn" | "max_tokens", usage=Usage(...))
```

## 同一段对话，两种协议

**Anthropic Messages API**（`llm/anthropic.py`）几乎原样发送：

```json
{
  "system": [{"type": "text", "text": "你是 nanocode...", "cache_control": {"type": "ephemeral"}}],
  "messages": [
    {"role": "user", "content": [{"type": "text", "text": "读一下 a.py"}]},
    {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "a.py"}}]},
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "1\tprint(1)"}]}
  ],
  "tools": [{"name": "read_file", "description": "...", "input_schema": {...}}]
}
```

**OpenAI Chat Completions**（`llm/openai_compat.py`）需要翻译：

```json
{
  "messages": [
    {"role": "system", "content": "你是 nanocode..."},
    {"role": "user", "content": "读一下 a.py"},
    {"role": "assistant", "content": null,
     "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "read_file", "arguments": "{\"path\": \"a.py\"}"}}]},
    {"role": "tool", "tool_call_id": "t1", "content": "1\tprint(1)"}
  ],
  "tools": [{"type": "function", "function": {"name": "read_file", "description": "...", "parameters": {...}}}]
}
```

翻译要点：

| 统一格式 | OpenAI 格式 | 注意 |
|---|---|---|
| `system` 参数 | 第一条 `role: system` 消息 | |
| `tool_use` 块 | assistant 消息的 `tool_calls` | `arguments` 是 **JSON 字符串**，不是对象 |
| `tool_use` 块的 `extra_content` | tool_call 上同名字段 | 服务端附在调用上、下一轮必须原样带回的数据（Gemini thinking 模型的 `thought_signature`，缺了会 400）。Agent 不解读它，只随消息保存；发给 Anthropic 时会去掉 |
| `tool_result` 块 | 独立的 `role: tool` 消息 | 每个结果一条；必须紧跟在 assistant 之后、普通 user 文本之前 |
| `input_schema` | `function.parameters` | 内容相同，换个位置 |
| `stop_reason: tool_use` | `finish_reason: tool_calls` | 有些厂商不规范，nanocode 以"是否真有工具调用"为准 |

## 流式输出

两种协议都用 SSE（Server-Sent Events）：一个持续的 HTTP 响应，每个事件是几行 `data: {...}`，空行分隔。`llm/base.py` 里的 `iter_sse()` 十几行就能解析。

**难点在工具调用的流式拼接**。工具参数也是一点一点到达的：

OpenAI：
```
data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"read_file","arguments":"{\"pa"}}]}}]}
data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"th\": \"b.py\"}"}}]}}]}
```
按 `index` 把 `arguments` 片段拼起来，最后再 `json.loads`。

Anthropic：
```
event: content_block_start   {"content_block": {"type": "tool_use", "id": "toolu_1", "name": "read_file"}}
event: content_block_delta   {"delta": {"type": "input_json_delta", "partial_json": "{\"path\":"}}
event: content_block_delta   {"delta": {"type": "input_json_delta", "partial_json": " \"c.py\"}"}}
event: content_block_stop
```
按块拼接 `partial_json`，在 `content_block_stop` 时解析。

拼出来的 JSON 可能不合法（模型偶尔会犯错）。nanocode 不会崩溃，而是把原始字符串放进 `{"__invalid_json__": ...}`，由工具层返回"参数不是合法的 JSON"，让模型自己重试。

`tests/test_providers.py` 用一个假的 HTTP 服务器回放上面这些事件流，验证解析结果，很适合对照着读。

## 重试

`_post_stream` 对 429（限流）和 5xx 做指数退避重试，但**只在流开始之前**重试：一旦已经开始输出文字，失败就直接报错，否则用户会看到重复内容。

## Prompt Caching

Agent 每走一步都要把整个上下文重新发一遍。第 20 步时，前面 19 步的内容都是重复的。Anthropic 的 Prompt Caching 允许给前缀打标记（`cache_control`），命中缓存的部分计费便宜得多、处理也更快。nanocode 在系统提示词上打了标记。OpenAI、DeepSeek 等厂商对较长的重复前缀也有自动缓存机制。

这也解释了为什么 Agent 的上下文要**只追加、不修改**：改动历史消息会让缓存失效。

## token 用量

Anthropic 在 `message_start` 和 `message_delta` 里返回用量；OpenAI 协议需要请求 `stream_options.include_usage`，在最后一个 chunk 里返回。有些厂商不支持，nanocode 会退回估算（`messages.estimate_tokens`），并在界面上标"估算"。

## 接入一家新厂商

1. 如果它兼容 OpenAI 协议（绝大多数都是）：在 `llm/__init__.py` 的 `PRESETS` 里加一行，或者直接用 `--provider custom --protocol openai --base-url ...`。
2. 如果是全新的协议：仿照 `openai_compat.py` 写一个 `Provider` 子类，实现 `complete()`，在 `make_provider` 里注册。需要写的只有两个翻译：请求翻译和流式响应解析。

## 图片输入（#110）

消息内容块支持 `type: image`（`source.type=base64`，与 Anthropic 对齐）。

- **CLI**：在输入里写 `@/path/to.png`（或相对路径），会随文本一起发给模型。
- **OpenAI 兼容**：转成 `image_url`（data URI）；**Anthropic**：转成 `source.base64`。
- 不支持视觉的模型（`Provider.supports_images = False`，如 mock）会提前报错「当前模型不支持图片输入」，不会等到 API 400。
- 压缩摘要时旧图变成占位文字；最近保留的回合里的图片仍在。
- 单张上限 4 MiB；会话 JSON 里仍含 base64（大图会胀）。更彻底的「只存附件路径」可后续做。

