"""工具的抽象。

对模型来说，一个工具只有三样东西：名字、说明、参数的 JSON Schema。
模型读说明来决定"什么时候用"，按 Schema 生成参数。所以工具的 description
写得好不好，直接决定 Agent 聪不聪明 —— 这是 Agent 开发里最容易被低估的部分。

对程序来说，工具还多两样东西：
  - is_read_only：只读工具可以免确认、可以并行执行
  - run(input, ctx)：真正干活，返回给模型看的文本
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class ToolResult:
    content: str
    is_error: bool = False


class ToolError(Exception):
    """工具执行失败。错误信息会原样交给模型 —— 写清楚原因，模型才能自己纠正。"""


def resolve_path(cwd: str, path: str) -> str:
    """把工具参数里的路径变成磁盘上真实的绝对路径。

    权限检查和工具执行必须用同一个函数解析路径。早期版本里两边各算各的：
    权限检查没展开 `~`、没跟随符号链接，于是 `~/.bashrc` 或指向项目外的软链接
    被当成"项目内文件"直接放行，而工具实际操作的却是项目外的文件。
    realpath 会展开所有符号链接，`..` 也一并消掉。
    """
    path = os.path.expanduser(path)
    full = path if os.path.isabs(path) else os.path.join(cwd, path)
    return os.path.realpath(full)


def is_inside(root: str, path: str) -> bool:
    root = os.path.realpath(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


@dataclass
class ToolContext:
    """工具运行时能拿到的环境。"""

    cwd: str
    state: dict = field(default_factory=dict)            # 会话级状态：已读文件、待办……
    emit: Callable[..., None] = lambda *a, **k: None      # 发事件（给 UI / trace）
    cancel: threading.Event = field(default_factory=threading.Event)
    run_subagent: Optional[Callable[..., str]] = None   # (description, prompt, check="", allowed_paths=None) -> 结论 + 验收报告
    skills: dict = field(default_factory=dict)

    def __post_init__(self):
        self.cwd = os.path.realpath(self.cwd)

    def resolve(self, path: str) -> str:
        return resolve_path(self.cwd, path)


class Tool:
    name: str = ""
    description: str = ""
    input_schema: dict = {"type": "object", "properties": {}}
    is_read_only: bool = False
    path_target: bool = False   # permission_target() 返回的是文件路径（权限系统据此判断是否在项目内）

    @property
    def changes_workspace(self) -> bool:
        """成功执行后，项目里的东西是否可能变了（决定"改完要验证"的闸门要不要关上）。默认：不是只读就算。
        联网这类"有副作用、但不改项目"的工具可以在子类里覆盖成 False。"""
        return not self.is_read_only

    def run(self, input: dict, ctx: ToolContext) -> str:
        raise NotImplementedError

    def permission_target(self, input: dict) -> str:
        """权限规则匹配用的"主参数"，例如 bash 的命令、文件工具的路径。"""
        return ""

    def schema(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}

    # 执行入口：校验参数 → 调用 run → 把异常变成"给模型看的错误"
    def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        if "__invalid_json__" in input:
            return ToolResult(f"参数不是合法的 JSON：{input['__invalid_json__'][:500]}", True)
        problem = validate(input, self.input_schema)
        if problem:
            return ToolResult(f"参数错误：{problem}", True)
        try:
            return ToolResult(self.run(input, ctx))
        except ToolError as e:
            return ToolResult(str(e), True)
        except Exception as e:  # noqa: BLE001 —— 任何异常都不能让 Agent 崩溃
            return ToolResult(f"{type(e).__name__}: {e}", True)


_TYPES: dict[str, Any] = {"string": str, "integer": int, "number": (int, float), "boolean": bool,
                          "array": list, "object": dict}


def validate(input: dict, schema: dict) -> str:
    """极简的 JSON Schema 校验：必填字段 + 基本类型 + enum。"""
    if not isinstance(input, dict):
        return "参数必须是对象"
    for key in schema.get("required", []):
        if key not in input:
            return f"缺少必填参数 {key!r}"
    for key, value in input.items():
        spec = schema.get("properties", {}).get(key)
        if not spec:
            continue
        expected = _TYPES.get(spec.get("type", ""))
        if expected and not isinstance(value, expected) or (spec.get("type") in ("integer", "number") and isinstance(value, bool)):
            return f"参数 {key!r} 应为 {spec['type']} 类型"
        if "enum" in spec and value not in spec["enum"]:
            return f"参数 {key!r} 必须是 {spec['enum']} 之一"
    return ""


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None):
        self._tools: dict[str, Tool] = {}
        for t in tools or []:
            self.register(t)

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def schemas(self) -> list[dict]:
        return [t.schema() for t in self._tools.values()]

    def subset(self, exclude: set[str]) -> "ToolRegistry":
        return ToolRegistry([t for t in self._tools.values() if t.name not in exclude])


def truncate(text: str, limit: int = 30000) -> str:
    """工具输出太长会撑爆上下文。保留头尾，中间截断，并告诉模型被截断了。"""
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n\n... [省略 {len(text) - limit} 个字符] ...\n\n{text[-half:]}"
