# 03 · 工具

> 对应源码：`nanocode/tools/`

## 工具对模型来说是什么

模型看不到你的 Python 代码，它看到的只是这样一段 JSON：

```json
{
  "name": "edit_file",
  "description": "在文件中把 old_string 精确替换为 new_string。old_string 必须与文件内容逐字符一致（包括缩进），并且在文件中唯一……编辑前必须先 read_file。",
  "input_schema": {
    "type": "object",
    "properties": {
      "path": {"type": "string"},
      "old_string": {"type": "string", "description": "要被替换的原文"},
      "new_string": {"type": "string", "description": "替换后的新文本"},
      "replace_all": {"type": "boolean"}
    },
    "required": ["path", "old_string", "new_string"]
  }
}
```

模型根据 `description` 决定**什么时候**用，根据 `input_schema` 决定**怎么**传参。所以：

> 工具的 description 是 Agent 最重要的提示词之一。它不是给人看的文档，是给模型看的使用说明。

好的工具说明会写：做什么、什么时候该用、什么时候不该用（应该用哪个别的工具）、有哪些前置条件、常见的坑。对比 `bash` 的说明里那句"查找文件请优先用 glob / grep，读文件用 read_file"，它在引导模型选更合适的工具。

## 工具对程序来说是什么

```python
class Tool:
    name: str
    description: str
    input_schema: dict
    is_read_only: bool                      # 只读：免确认、可并行
    def permission_target(self, input): ...  # 权限规则匹配的"主参数"
    def run(self, input, ctx) -> str: ...    # 干活，返回给模型看的文字
```

`execute()` 包了一层：参数校验 → `run()` → 把所有异常变成 `ToolResult(..., is_error=True)`。

## nanocode 的 10 个工具

| 工具 | 只读 | 作用 | 设计要点 |
|---|---|---|---|
| `read_file` | ✓ | 读文件 | 带行号；支持 offset/limit 分段；拒绝二进制 |
| `write_file` | | 创建/覆盖 | 覆盖已有文件前必须先读过 |
| `edit_file` | | 精确替换 | old_string 必须唯一；必须先读 |
| `bash` | | 执行命令 | 超时、可中断、记住 cd 后的目录、输出截断 |
| `glob` | ✓ | 按文件名找 | 支持 `**`；按修改时间排序；忽略 .git 等 |
| `grep` | ✓ | 按内容找 | 正则；`文件:行号: 内容` 格式 |
| `search_docs` | ✓ | 按含义检索文档 | BM25 + 可选向量，见第 10 章；已知确切名字时用 grep |
| `todo_write` | ✓ | 待办清单 | 见第 06 章 |
| `task` | ✓ | 子 Agent | 见第 06 章 |
| `skill` | ✓ | 加载技能说明 | 见第 06 章 |

## 三个关键设计

### 1. 读文件带行号

```
1	def add(a, b):
2	    return a - b
```

行号给了模型一个"坐标系"：它可以说"第 2 行有 bug"，用户也能快速定位。编辑时模型需要去掉行号前缀，所以 `edit_file` 的说明里专门提醒了这一点。

### 2. 编辑用"精确字符串替换"

让模型改文件有几种做法：

| 做法 | 问题 |
|---|---|
| 输出整个新文件 | 大文件浪费大量 token；模型容易"顺手"改掉不该改的地方或漏掉内容 |
| 输出 diff / patch | 模型很难算对行号和上下文格式，补丁经常打不上 |
| 按行号替换 | 文件一改行号就变了，多次编辑互相干扰 |
| **精确字符串替换** | 只输出要改的那一小段；找不到或不唯一就报错，绝不改错地方 |

"必须唯一"这条规则很关键：如果 `old_string` 出现了 3 次，宁可报错让模型多带几行上下文，也不要猜它想改哪一处。

### 3. 先读后改

`files.py` 在 `ctx.state["read_files"]` 里记录每个文件被读取时的修改时间。编辑或覆盖时检查：

- 没读过 → 拒绝："修改之前必须先用 read_file 读取它"
- 读过之后文件又被改了（比如用户在编辑器里改了，或者 bash 里的脚本改了）→ 拒绝："请重新 read_file"

这逼着模型基于文件的**真实当前内容**修改，而不是基于它的记忆或猜测。

## bash 工具的细节

- **工作目录保持**：在命令末尾追加 `pwd`，读回来保存。模型执行 `cd src` 之后，下一条命令就在 src 里。
- **必须边跑边读输出**：如果等进程结束再读，输出超过管道缓冲区（约 64KB）时子进程会阻塞，永远不会结束。`test_bash_large_output_no_deadlock` 就是测这个的。
- **进程组**：用 `start_new_session=True` 启动，超时或中断时杀掉整个进程组，避免留下孤儿进程。
- **退出码**：非 0 退出码不算"工具错误"，而是作为信息告诉模型（测试失败是有用的反馈，不是异常）。

## 输出截断

一次 `cat` 一个日志文件可能有几 MB，直接塞进上下文会立刻撑爆。`truncate()` 保留头尾、截掉中间，并明确告诉模型"省略了多少字符"，模型就知道可以用 offset/limit 或 grep 去看具体部分。

## 只读工具并行

模型经常一次发出多个调用："读 a.py、读 b.py、grep 一下 foo"。这些互不影响，`agent.py` 用线程池并行执行。只要有一个是写操作，就全部串行，保证顺序语义。结果始终按调用顺序返回。

## 加一个新工具

```python
class WebFetch(Tool):
    name = "web_fetch"
    is_read_only = True
    description = "获取一个网页的文本内容。适合查阅在线文档。"
    input_schema = {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}

    def permission_target(self, input):
        return input.get("url", "")

    def run(self, input, ctx):
        import urllib.request
        with urllib.request.urlopen(input["url"], timeout=30) as r:
            return r.read().decode("utf-8", "replace")[:50000]
```

然后在 `tools/__init__.py` 的 `default_tools()` 里加上 `WebFetch()`。就这样，模型下一次请求就能看到并使用它。（练习：这个实现有什么安全问题？参考第 04 章。）
