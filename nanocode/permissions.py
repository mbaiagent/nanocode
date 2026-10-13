"""权限系统：在"模型想做"和"真正去做"之间加一道闸门。

模型可能误解需求、可能被文件里的恶意内容诱导（提示词注入），所以有副作用的
操作必须可控。决策顺序（先命中先生效）：

  1. deny 规则      —— 黑名单，永远拒绝，例如 bash(rm -rf *)
  2. plan 模式      —— 只读模式：所有非只读工具一律拒绝，让模型先出方案
  3. allow 规则     —— 白名单（配置文件 + 本次会话里用户点过"总是允许"的）
  4. bypass 模式    —— 全部放行（危险，只在沙箱/容器里用）
  5. 只读工具       —— 在工作目录内直接放行；读工作目录以外的文件要确认
  6. accept_edits   —— 工作目录内的文件编辑自动放行，bash 仍需确认
  7. 其余           —— 询问用户（ask）

review（终审）模式的顺序不同，见 check()：deny → plan 之外 → **高风险一律确认**（连 allow 规则也不能放开）→
allow 规则 / 项目内的编辑 / 白名单内的检查命令自动放行 → 其余询问。高风险和白名单的定义在 risk.py。

规则写法：`工具名` 或 `工具名(模式)`，模式用 * 通配，匹配工具的"主参数"
（bash 的命令、文件工具的路径）。例如：

    "allow": ["bash(git status*)", "bash(python -m pytest*)", "read_file"]
    "deny":  ["bash(rm -rf*)", "bash(*sudo*)", "write_file(*.env)"]
"""

from __future__ import annotations

import fnmatch
import os
import re
import shlex
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import risk
from .tools.base import is_inside, resolve_path

MODES = ("default", "accept_edits", "plan", "review", "bypass")
EDIT_TOOLS = {"write_file", "edit_file"}
_RULE = re.compile(r"^([\w\-]+)(?:\((.*)\))?$")

# 询问回调：(工具名, 参数, 主参数) -> "yes" | "no" | "always"
AskFn = Callable[[str, dict, str], str]


@dataclass
class Decision:
    behavior: str  # allow | deny | ask
    reason: str = ""
    high_risk: bool = False   # 终审模式下识别出的高风险操作：不能被 allow 规则放开，也不能"总是允许"


@dataclass
class PermissionManager:
    cwd: str
    mode: str = "default"
    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)
    ask_fn: Optional[AskFn] = None
    session_allow: list[str] = field(default_factory=list)

    def __post_init__(self):
        self.cwd = os.path.realpath(self.cwd)

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"未知模式 {mode}，可选：{', '.join(MODES)}")
        self.mode = mode

    # ------------------------------------------------------------------ 决策

    def check(self, tool, input: dict) -> Decision:
        target = tool.permission_target(input) or ""
        inside = True
        allow_forms = deny_forms = [target]
        if getattr(tool, "path_target", False):
            # 路径类工具：用真实路径判断，规则也匹配规范化后的路径，
            # 防止 `~/x`、软链接、`src/../../etc/x` 这类写法绕过检查
            full = resolve_path(self.cwd, target or ".")
            inside = is_inside(self.cwd, full)
            norm = os.path.relpath(full, os.path.realpath(self.cwd)) if inside else full
            allow_forms = [norm]                 # allow 只认规范化路径：宁可多问一次
            deny_forms = [target, norm, full]    # deny 任一写法命中都拒绝：宁可多拦一次
        if tool.name == "bash":
            if _bash_matches(self.deny, target, deny=True):
                return Decision("deny", "命中 deny 规则（复合命令里任何一段命中都算）")
        elif _matches_any(self.deny, tool.name, *deny_forms):
            return Decision("deny", "命中 deny 规则")
        if self.mode == "plan" and not tool.is_read_only:
            return Decision("deny", "当前处于 plan（只读规划）模式，不能执行修改类操作。请先给出方案，等用户切换模式后再执行。")
        if self.mode == "review":
            why = self._high_risk(tool, target, inside, allow_forms[0] if getattr(tool, "path_target", False) else "")
            if why:
                return Decision("ask", f"高风险操作（{why}），终审模式下无论 allow 规则如何都必须确认", high_risk=True)
        if tool.name == "bash":
            if _bash_matches(self.allow, target, cwd=self.cwd, exact=self.session_allow):
                return Decision("allow", "命中 allow 规则")
        elif _matches_any(self.allow + self.session_allow, tool.name, *allow_forms):
            return Decision("allow", "命中 allow 规则")
        if self.mode == "bypass":
            return Decision("allow", "bypass 模式")
        if tool.is_read_only:
            return Decision("allow", "只读工具") if inside else Decision("ask", "访问工作目录以外的路径")
        if self.mode == "accept_edits" and tool.name in EDIT_TOOLS and inside:
            return Decision("allow", "accept_edits 模式")
        if self.mode == "review":
            if tool.name in EDIT_TOOLS and inside:
                return Decision("allow", "终审模式：项目内的编辑自动放行，结束时汇总 diff 供你审查")
            if tool.name == "bash" and risk.classify_bash(target) == "safe":
                return Decision("allow", "终审模式：白名单内的测试 / 只读命令自动放行")
        return Decision("ask", "需要用户确认")

    def _high_risk(self, tool, target: str, inside: bool, rel: str) -> str:
        """终审模式下的高风险判断，返回原因；空串 = 不算高风险。"""
        if tool.name == "bash":
            return risk.risk_reason(target) if risk.classify_bash(target) == "risky" else ""
        if tool.is_read_only or not getattr(tool, "path_target", False):
            return ""
        if not inside:
            return "修改项目以外的路径"
        return "修改配置、密钥或版本库内部" if risk.is_protected_path(rel) else ""

    def resolve(self, tool, input: dict) -> Decision:
        """check + 必要时询问用户，返回最终的 allow / deny。"""
        decision = self.check(tool, input)
        if decision.behavior != "ask":
            return decision
        if not self.ask_fn:
            hint = ""
            if tool.name == "bash" and len(split_command(tool.permission_target(input) or "")[0]) > 1:
                hint = "这是复合命令（含 && ; | 等），allow 规则要每一段都命中才放行。"
            return Decision("deny", "需要确认，但当前没有可以询问的用户（非交互模式）。可用 --mode 或 allow 规则放行。" + hint)
        target = tool.permission_target(input) or ""
        answer = self.ask_fn(tool.name, input, target)
        if answer == "always" and decision.high_risk:
            return Decision("allow", "用户允许这一次（高风险操作不支持「总是允许」）")
        if answer == "always":
            rule = f"bash({target})" if tool.name == "bash" else tool.name
            self.session_allow.append(rule)
            return Decision("allow", f"用户选择总是允许（已添加规则 {rule}）")
        if answer == "yes":
            return Decision("allow", "用户允许")
        return Decision("deny", "用户拒绝了这次操作。请询问用户希望怎么做，不要换个方式绕过。")


# ---------------------------------------------------------------- bash 复合命令
#
# 规则是对"一条命令"写的，而模型常写 `cd dir && cmd`、`cmd1; cmd2`、`cmd | tail`。整串做 fnmatch 会两头出错：
# 该放行的不命中（模型自己加的 cd 前缀就让 allow 失效），不该放行的却命中（`unittest && rm -rf /` 被前缀规则放行；
# deny 也被 `cd x && rm -rf y` 绕过）。所以先拆成段，再逐段匹配：allow 要每一段都命中，deny 任一段命中就拒绝。
# 这仍然只是字符串层面的处理，不是沙箱：`bash -c "…"`、`xargs rm`、别名、变量展开等都看不穿。

_SEPARATORS = ("&&", "||", "|&")


def split_command(command: str, deep: bool = False) -> tuple[list[str], bool]:
    """按 && || ; | 换行 & 拆成段（引号里的不拆）。返回 (段列表, 是否有藏在段里的命令)。

    藏起来的命令指 $(…)、反引号、<(…)、(…) 子 shell、{ …; } 命令组：它们在一段之内再执行别的命令。
    deep=False（匹配 allow 用）：只标记出来，调用方应当拒绝放行；
    deep=True（匹配 deny 用）：把它们也当作分隔符展开，宁可多拦。
    """
    segs: list[str] = []
    cur: list[str] = []
    opaque = False
    quote = ""
    resume: list[str] = []       # deep 模式：进入 "…$(…)…" 里的命令替换前，记下外层的引号状态
    i, n = 0, len(command)

    def cut():
        text = "".join(cur).strip()
        if text:
            segs.append(text)
        cur.clear()

    def hidden(width: int = 1) -> int:
        nonlocal opaque
        nonlocal quote
        opaque = True
        if deep:
            cut()
            if quote == '"':
                resume.append(quote)
                quote = ""
        else:
            cur.append(command[i:i + width])
        return i + width

    while i < n:
        c, two = command[i], command[i:i + 2]
        if quote == "'":
            cur.append(c)
            quote = "" if c == "'" else quote
            i += 1
        elif c == "\\" and i + 1 < n:
            cur.append(two)
            i += 2
        elif quote == '"':
            if c == '"':
                quote = ""
                cur.append(c)
                i += 1
            elif c == "`":
                i = hidden()
            elif two == "$(":
                i = hidden(2)
            else:
                cur.append(c)
                i += 1
        elif c in "'\"":
            quote = c
            cur.append(c)
            i += 1
        elif c == "`":
            i = hidden()
        elif two in ("$(", "<(", ">("):
            i = hidden(2)
        elif c == ")" and deep and resume:
            cut()
            quote = resume.pop()
            i += 1
        elif c in "()":
            i = hidden()
        elif c in "{}" and (i == 0 or command[i - 1].isspace()) and (i + 1 >= n or command[i + 1].isspace()):
            i = hidden()                       # 独立的 { 或 }：命令组；`${X}` 和 `find -exec {} \;` 不算
        elif two in _SEPARATORS:
            cut()
            i += 2
        elif c in ";|\n":
            cut()
            i += 1
        elif c == "&" and not (i > 0 and command[i - 1] in "<>") and command[i + 1:i + 2] != ">":
            cut()                              # 单个 & 是后台运行；2>&1 和 &> 是重定向，不是分隔符
            i += 1
        else:
            cur.append(c)
            i += 1
    cut()
    return segs, opaque


def _is_harmless_cd(segment: str, cwd: str) -> bool:
    """`cd 项目内的目录` 只改变后面命令的工作目录，不算一个需要规则放行的动作。"""
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return False
    if len(tokens) != 2 or tokens[0] != "cd" or tokens[1].startswith(("-", "~", "$")):
        return False
    return is_inside(cwd, resolve_path(cwd, tokens[1]))


def _bash_matches(rules: list[str], command: str, deny: bool = False, cwd: str = "", exact: list[str] | None = None) -> bool:
    patterns: list[str | None] = []
    for rule in rules:
        m = _RULE.match(rule.strip())
        if m and m.group(1) == "bash":
            patterns.append(m.group(2))
    if deny:
        # 整串匹配保留原有行为（如 `bash(*sudo*)`）；再逐段匹配，堵住 `cd x && rm -rf y`
        segments, _ = split_command(command, deep=True)
        return any(p is None or fnmatch.fnmatchcase(t, p) for p in patterns for t in [command.strip(), *segments])
    # allow：「总是允许」记下的是整条命令的原文，只认逐字相同的那一条（不当作通配）
    if any(r.strip() == f"bash({command})" for r in exact or []):
        return True
    if any(p is None or p == "*" for p in patterns):
        return True                                           # 用户明确写了放行所有 bash
    segments, opaque = split_command(command)
    if opaque or not segments:
        return False
    real = [t for t in segments if not _is_harmless_cd(t, cwd)]
    return bool(real) and all(any(fnmatch.fnmatchcase(t, p) for p in patterns if p) for t in real)


def _matches_any(rules: list[str], tool_name: str, *targets: str) -> bool:
    for rule in rules:
        m = _RULE.match(rule.strip())
        if not m or m.group(1) != tool_name:
            continue
        pattern = m.group(2)
        if pattern is None or any(fnmatch.fnmatchcase(t, pattern) for t in targets):
            return True
    return False
