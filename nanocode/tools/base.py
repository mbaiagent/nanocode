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


def norm_rel(path: str) -> str:
    """规则匹配用的规范化路径：统一分隔符 + Windows 上大小写不敏感。

    没有这个函数，Windows 上 `os.path.relpath` 给出 `secrets\\a.txt`，而规则一般写成
    `secrets/*`，fnmatchcase 匹配不上 —— 模型只要把 / 换成 \\ 就能绕过 deny，
    而 bypass 模式下 deny 是唯一防线（#35）。POSIX 上 normcase 是恒等变换。

    放在 tools/base.py 是因为 permissions / subcheck / review / agent 都要用它，
    而这几层之间互相引用会成环。
    """
    return os.path.normcase(path).replace(os.sep, "/")


def safe_relpath(path: str, start: str) -> str:
    """os.path.relpath 的兜底版本：算不出相对路径时返回绝对路径，绝不抛异常。

    Windows 上目标和 cwd 不在同一个盘（`D:` 上的文件 + `C:` 上的项目）时，relpath 抛
    `ValueError: path is on mount 'D:'`。这个异常会从 review / subcheck / 权限检查一路
    穿到主循环，直接打断整轮对话（#45）。相对路径只是给人和规则看的，算不出来就用
    绝对路径——同一份清单里两种形式混着出现，也比崩掉好。
    """
    try:
        return os.path.relpath(path, start)
    except ValueError:
        return os.path.abspath(path)


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
    """path 是否在 root 目录内（含 root 本身）。两侧都先 realpath，消掉 `..` 和软链接。

    Windows 上还要 normcase：盘符和路径大小写不敏感（`c:\\x` 和 `C:\\x` 是同一个地方），
    不做的话同一条路径会被判成"项目外"，触发大量无谓确认（#35）。
    """
    root = os.path.realpath(root)
    if os.name == "nt":
        root, path = os.path.normcase(root), os.path.normcase(os.path.realpath(path))
    else:
        path = os.path.realpath(path)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def symlink_escapes(root: str, path: str) -> bool:
    """path 是一个指向 root 之外的软链接吗。

    项目里的 `NANOCODE.md -> ~/.ssh/id_rsa`：os.path.isfile 和 open 都会跟随软链接，
    不检查的话，顺着它就能把项目外的文件读进上下文 —— 记忆文件会进入每一次请求的
    system prompt，grep / search_docs 会把内容搜出来，全程不需要任何工具调用、
    不需要用户批准，打开陌生仓库就可能把本机私钥发给模型服务商（#38）。
    硬链接在路径层识别不了，这个检查防不住它，只能靠"不打开陌生仓库"兜底。
    """
    return os.path.islink(path) and not is_inside(root, path)


@dataclass
class ToolContext:
    """工具运行时能拿到的环境。"""

    cwd: str
    state: dict = field(default_factory=dict)            # 会话级状态：已读文件、待办……
    emit: Callable[..., None] = lambda *a, **k: None      # 发事件（给 UI / trace）
    cancel: threading.Event = field(default_factory=threading.Event)
    run_subagent: Optional[Callable[..., str]] = None   # (description, prompt, check="", allowed_paths=None) -> 结论 + 验收报告
    skills: dict = field(default_factory=dict)
    # 权限判定时解析出的真实路径（用户批准的就是它）。Agent 每次调用路径类工具时
    # 用一份带上它的副本，工具执行前用 resolve_checked() 复核（#42）。空 = 不复核。
    approved_path: str = ""

    def __post_init__(self):
        self.cwd = os.path.realpath(self.cwd)

    def resolve(self, path: str) -> str:
        return resolve_path(self.cwd, path)

    def resolve_checked(self, path: str) -> str:
        """解析路径，并确认它仍是权限判定时的那个文件。

        check 和 open 之间隔着用户确认（可能好几分钟）、PreToolUse hook、并行的子 Agent，
        这期间把软链接换个指向，工具实际操作的就成了没被批准的文件（TOCTOU，#42）。
        """
        full = self.resolve(path)
        if self.approved_path and not _same_path(full, self.approved_path):
            raise ToolError(f"路径在权限确认之后变了：确认时是 {self.approved_path}，现在指向 {full}。"
                            "为安全起见没有执行，请重新发起这次操作。")
        return full


def _same_path(a: str, b: str) -> bool:
    if os.name == "nt":
        a, b = os.path.normcase(a), os.path.normcase(b)
    return a == b


_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def open_nofollow(path: str, mode: str = "rb", **kw):
    """打开 resolve_checked() 给出的真实路径，最后一级不跟随软链接。

    真实路径里本不该有软链接；复核之后到 open 之前要是有人把文件换成了软链接，
    O_NOFOLLOW 会让 open 直接失败（ELOOP），而不是顺着链接去读写别处。
    Windows 没有 O_NOFOLLOW，只能靠前面的复核。
    """
    flags = {"rb": os.O_RDONLY, "w": os.O_WRONLY | os.O_CREAT | os.O_TRUNC}[mode]
    flags |= getattr(os, "O_BINARY", 0) | _NOFOLLOW
    try:
        fd = os.open(path, flags, 0o666)
    except OSError:
        if _NOFOLLOW and os.path.islink(path):
            raise ToolError(f"{path} 在权限确认之后被换成了软链接，为安全起见没有执行。") from None
        raise
    return os.fdopen(fd, mode, **kw)


class Tool:
    name: str = ""
    description: str = ""
    input_schema: dict = {"type": "object", "properties": {}}
    is_read_only: bool = False
    path_target: bool = False   # permission_target() 返回的是文件路径（权限系统据此判断是否在项目内）

    @property
    def parallel_safe(self) -> bool:
        """可以和同一回复里的其它工具调用并行执行吗。默认只读的才可以。

        注意"可以免确认"（is_read_only）和"可以并行"不是一回事：
          - write_file 不免确认，也当然不能并行 —— 它和 read_file 并行时，
            read_file 还没把"已读过"登记上，write_file 就会以"修改前必须先读"拒绝；
          - task 名义上只读（子 Agent 的每个调用仍单独走权限），实际却会在工作线程里
            读写父 Agent 的 usage / _dirty / _review_originals，并对整棵工作树做快照，
            两个 task 并行会互相把对方的改动算成自己的（#46）。

        所以默认值跟着 is_read_only 走，需要例外的子类用类属性覆盖
        （`parallel_safe = False` 会遮住这个 property）。
        """
        return self.is_read_only

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
