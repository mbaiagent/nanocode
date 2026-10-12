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

规则写法：`工具名` 或 `工具名(模式)`，模式用 * 通配，匹配工具的"主参数"
（bash 的命令、文件工具的路径）。例如：

    "allow": ["bash(git status*)", "bash(python -m pytest*)", "read_file"]
    "deny":  ["bash(rm -rf*)", "bash(*sudo*)", "write_file(*.env)"]
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from .tools.base import is_inside, resolve_path

MODES = ("default", "accept_edits", "plan", "bypass")
EDIT_TOOLS = {"write_file", "edit_file"}
_RULE = re.compile(r"^([\w\-]+)(?:\((.*)\))?$")

# 询问回调：(工具名, 参数, 主参数) -> "yes" | "no" | "always"
AskFn = Callable[[str, dict, str], str]


@dataclass
class Decision:
    behavior: str  # allow | deny | ask
    reason: str = ""


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
        if _matches_any(self.deny, tool.name, *deny_forms):
            return Decision("deny", "命中 deny 规则")
        if self.mode == "plan" and not tool.is_read_only:
            return Decision("deny", "当前处于 plan（只读规划）模式，不能执行修改类操作。请先给出方案，等用户切换模式后再执行。")
        if _matches_any(self.allow + self.session_allow, tool.name, *allow_forms):
            return Decision("allow", "命中 allow 规则")
        if self.mode == "bypass":
            return Decision("allow", "bypass 模式")
        if tool.is_read_only:
            return Decision("allow", "只读工具") if inside else Decision("ask", "访问工作目录以外的路径")
        if self.mode == "accept_edits" and tool.name in EDIT_TOOLS and inside:
            return Decision("allow", "accept_edits 模式")
        return Decision("ask", "需要用户确认")

    def resolve(self, tool, input: dict) -> Decision:
        """check + 必要时询问用户，返回最终的 allow / deny。"""
        decision = self.check(tool, input)
        if decision.behavior != "ask":
            return decision
        if not self.ask_fn:
            return Decision("deny", "需要确认，但当前没有可以询问的用户（非交互模式）。可用 --mode 或 allow 规则放行。")
        target = tool.permission_target(input) or ""
        answer = self.ask_fn(tool.name, input, target)
        if answer == "always":
            rule = f"bash({target})" if tool.name == "bash" else tool.name
            self.session_allow.append(rule)
            return Decision("allow", f"用户选择总是允许（已添加规则 {rule}）")
        if answer == "yes":
            return Decision("allow", "用户允许")
        return Decision("deny", "用户拒绝了这次操作。请询问用户希望怎么做，不要换个方式绕过。")


def _matches_any(rules: list[str], tool_name: str, *targets: str) -> bool:
    for rule in rules:
        m = _RULE.match(rule.strip())
        if not m or m.group(1) != tool_name:
            continue
        pattern = m.group(2)
        if pattern is None or any(fnmatch.fnmatchcase(t, pattern) for t in targets):
            return True
    return False
