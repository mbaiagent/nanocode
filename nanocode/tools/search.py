"""搜索工具：glob（按文件名找）和 grep（按内容找）。

为什么不直接让模型用 bash 跑 find / grep？
  - 专用工具是只读的，可以免确认、可以并行；
  - 输出格式稳定、自动忽略 .git / node_modules 等目录、自动截断；
  - 结果按修改时间排序，最近改过的文件往往最相关。
"""

from __future__ import annotations

import fnmatch
import os
import re

from .base import Tool, ToolError, symlink_escapes

IGNORED_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache",
                "dist", "build", ".idea", ".nanocode"}


def walk_files(root: str):
    """遍历 root 下的文件，跳过忽略目录和指向项目外的软链接。

    权限检查只认工具参数里的"根路径"，树里每个文件不再过一遍；`evil.md -> ~/.ssh/id_rsa`
    这样的软链接会被 os.walk 列出来、被 open 跟随，把私钥内容搜进上下文（#38）。
    指向项目内的软链接照常遍历。硬链接在路径层识别不了，防不住。
    """
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
        for name in filenames:
            full = os.path.join(dirpath, name)
            if symlink_escapes(root, full):
                continue
            yield full


def glob_to_regex(pattern: str) -> re.Pattern:
    """把 glob 翻译成正则：`**/` = 任意层目录（可以是零层），`*` = 不跨目录的任意字符。"""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + r"\Z")


def _match(rel: str, pattern: str) -> bool:
    return bool(glob_to_regex(pattern).match(rel.replace(os.sep, "/")))


class Glob(Tool):
    name = "glob"
    path_target = True
    is_read_only = True
    description = ("按文件名模式查找文件，例如 `**/*.py`（任意层级）、`src/*.ts`、`*`（当前层）。"
                   "结果按修改时间从新到旧排序，自动忽略 .git、node_modules 等目录。")
    input_schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "glob 模式"},
            "path": {"type": "string", "description": "搜索的根目录，默认为工作目录"},
        },
        "required": ["pattern"],
    }

    def permission_target(self, input):
        return input.get("path", ".")

    def run(self, input, ctx):
        root = ctx.resolve(input.get("path") or ".")
        if not os.path.isdir(root):
            raise ToolError(f"目录不存在：{root}")
        pattern = input["pattern"]
        hits = []
        if "/" not in pattern and "**" not in pattern:  # 当前层：文件和目录都列出来
            for name in os.listdir(root):
                if fnmatch.fnmatch(name, pattern) and name not in IGNORED_DIRS:
                    full = os.path.join(root, name)
                    hits.append(full + ("/" if os.path.isdir(full) else ""))
        else:
            hits = [p for p in walk_files(root) if _match(os.path.relpath(p, root), pattern)]
        hits.sort(key=lambda p: os.path.getmtime(p.rstrip("/")), reverse=True)
        if not hits:
            return "没有匹配的文件"
        shown = [os.path.relpath(p, ctx.cwd) + ("/" if p.endswith("/") else "") for p in hits[:200]]
        more = f"\n... 还有 {len(hits) - 200} 个结果" if len(hits) > 200 else ""
        return "\n".join(shown) + more


class Grep(Tool):
    name = "grep"
    path_target = True
    is_read_only = True
    description = ("用正则表达式搜索文件内容，返回 `文件:行号: 内容`。"
                   "可以用 glob 参数限定文件类型（如 `*.py`）。适合找函数定义、调用点、报错信息。")
    input_schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Python 正则表达式"},
            "path": {"type": "string", "description": "搜索的目录或文件，默认工作目录"},
            "glob": {"type": "string", "description": "只搜索匹配该文件名模式的文件，如 *.py"},
            "ignore_case": {"type": "boolean"},
        },
        "required": ["pattern"],
    }

    def permission_target(self, input):
        return input.get("path", ".")

    def run(self, input, ctx):
        try:
            rx = re.compile(input["pattern"], re.IGNORECASE if input.get("ignore_case") else 0)
        except re.error as e:
            raise ToolError(f"正则表达式错误：{e}") from None
        root = ctx.resolve(input.get("path") or ".")
        files = [root] if os.path.isfile(root) else walk_files(root)
        name_glob = input.get("glob")
        out, total = [], 0
        for path in files:
            if name_glob and not fnmatch.fnmatch(os.path.basename(path), name_glob):
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    for no, line in enumerate(f, 1):
                        if rx.search(line):
                            total += 1
                            if len(out) < 300:
                                out.append(f"{os.path.relpath(path, ctx.cwd)}:{no}: {line.rstrip()[:300]}")
            except (UnicodeDecodeError, OSError):
                continue  # 跳过二进制和无权限文件
        if not out:
            return "没有匹配"
        return "\n".join(out) + (f"\n... 共 {total} 处匹配，只显示前 300 处" if total > 300 else "")
