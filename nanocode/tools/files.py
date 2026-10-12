"""文件工具：read_file / write_file / edit_file。

三个值得注意的设计（都是从编程 Agent 的实践里总结出来的）：

1. 读文件带行号（类似 `cat -n`）。模型引用代码、定位问题时有了"坐标"。
2. 编辑用"精确字符串替换"，而不是整文件重写或 diff：
   - 模型只需输出要改的那一小段，省 token、少出错；
   - old_string 必须在文件中唯一出现，否则拒绝 —— 防止改错地方。
3. "先读后改"：没读过（或读过之后被外部改动过）的文件不允许编辑/覆盖，
   逼模型基于文件的真实内容做修改，而不是凭记忆瞎改。
"""

from __future__ import annotations

import os

from .base import Tool, ToolContext, ToolError


def _mark_read(ctx: ToolContext, path: str) -> None:
    ctx.state.setdefault("read_files", {})[path] = os.path.getmtime(path)


def _check_fresh(ctx: ToolContext, path: str) -> None:
    seen = ctx.state.setdefault("read_files", {}).get(path)
    if seen is None:
        raise ToolError(f"修改 {path} 之前必须先用 read_file 读取它。")
    if os.path.getmtime(path) > seen + 1e-6:
        raise ToolError(f"{path} 在你读取之后被修改过了，请重新 read_file 再修改。")


class ReadFile(Tool):
    name = "read_file"
    path_target = True
    is_read_only = True
    description = ("读取一个文本文件，返回带行号的内容（格式：行号<TAB>内容）。"
                   "大文件可以用 offset / limit 分段读取。修改文件之前必须先读取它。")
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径，相对于工作目录或绝对路径"},
            "offset": {"type": "integer", "description": "从第几行开始（1 起），默认 1"},
            "limit": {"type": "integer", "description": "最多读多少行，默认 2000"},
        },
        "required": ["path"],
    }

    def permission_target(self, input):
        return input.get("path", "")

    def run(self, input, ctx):
        path = ctx.resolve(input["path"])
        if not os.path.exists(path):
            raise ToolError(f"文件不存在：{path}")
        if os.path.isdir(path):
            raise ToolError(f"{path} 是目录，请用 glob 查看目录内容")
        with open(path, "rb") as f:
            raw = f.read()
        if b"\x00" in raw[:8000]:
            raise ToolError(f"{path} 看起来是二进制文件，无法以文本读取")
        lines = raw.decode("utf-8", "replace").splitlines()
        offset = max(1, input.get("offset", 1))
        limit = input.get("limit", 2000)
        chunk = lines[offset - 1: offset - 1 + limit]
        _mark_read(ctx, path)
        if not lines:
            return "(空文件)"
        body = "\n".join(f"{i}\t{line[:2000]}" for i, line in enumerate(chunk, start=offset))
        if offset - 1 + limit < len(lines):
            body += f"\n... (共 {len(lines)} 行，用 offset={offset + limit} 继续读)"
        return body


class WriteFile(Tool):
    name = "write_file"
    path_target = True
    description = ("创建新文件，或完整覆盖已有文件（覆盖前必须先 read_file）。"
                   "修改已有文件的一部分时，优先使用 edit_file。会自动创建父目录。")
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径"},
            "content": {"type": "string", "description": "完整的文件内容"},
        },
        "required": ["path", "content"],
    }

    def permission_target(self, input):
        return input.get("path", "")

    def run(self, input, ctx):
        path = ctx.resolve(input["path"])
        existed = os.path.exists(path)
        if existed:
            _check_fresh(ctx, path)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(input["content"])
        _mark_read(ctx, path)
        n = input["content"].count("\n") + 1
        return f"{'已覆盖' if existed else '已创建'} {path}（{n} 行）"


class EditFile(Tool):
    name = "edit_file"
    path_target = True
    description = ("在文件中把 old_string 精确替换为 new_string。old_string 必须与文件内容逐字符一致"
                   "（包括缩进），并且在文件中唯一；不唯一时请多带几行上下文，或设置 replace_all=true。"
                   "编辑前必须先 read_file。注意不要把 read_file 输出里的行号前缀带进来。")
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_string": {"type": "string", "description": "要被替换的原文"},
            "new_string": {"type": "string", "description": "替换后的新文本"},
            "replace_all": {"type": "boolean", "description": "替换所有出现，默认 false"},
        },
        "required": ["path", "old_string", "new_string"],
    }

    def permission_target(self, input):
        return input.get("path", "")

    def run(self, input, ctx):
        path = ctx.resolve(input["path"])
        if not os.path.exists(path):
            raise ToolError(f"文件不存在：{path}（新建文件请用 write_file）")
        _check_fresh(ctx, path)
        old, new = input["old_string"], input["new_string"]
        if old == new:
            raise ToolError("old_string 和 new_string 相同，没有需要修改的内容")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        count = text.count(old)
        if count == 0:
            raise ToolError("在文件中找不到 old_string。请确认缩进和空白完全一致，必要时重新 read_file。")
        if count > 1 and not input.get("replace_all"):
            raise ToolError(f"old_string 在文件中出现了 {count} 次。请带上更多上下文让它唯一，或设置 replace_all=true。")
        text = text.replace(old, new) if input.get("replace_all") else text.replace(old, new, 1)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        _mark_read(ctx, path)
        line = text[: text.find(new)].count("\n") + 1 if new else 0
        return f"已修改 {path}（替换 {count if input.get('replace_all') else 1} 处，约第 {line} 行）"
