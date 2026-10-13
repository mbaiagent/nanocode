"""子 Agent 的验收：它说了不算，由系统去核对。

主 Agent 派出子任务时可以带两个可选条件（见 tools/planning.py 的 task 工具）：
  check          一条检查命令（如 pytest -q tests/test_x.py）。子 Agent 想交差前先跑，不过就让它继续修。
  allowed_paths  允许修改的路径范围（相对工作目录，如 ["src/parser/*", "tests/test_parser.py"]）。
                 写文件工具越界会被直接拒绝；bash 等绕不开的改动，靠前后快照对比发现。

不管有没有带条件，子 Agent 返回的结论后面都会附一份"验收报告"：改了哪些文件、有没有越界、
检查有没有通过。报告是系统生成的，不是子 Agent 自述 —— 主 Agent 靠它区分"确认过的事实"和"子 Agent 的一面之词"。

快照比较的是内容摘要：改了又还原、只 touch 没改内容，都不算改动。超过 1MB 的文件只比较大小和修改时间。
文件数超过上限时放弃统计并在报告里写明，不会假装检查过。bash 里改的东西只能靠快照发现，所以是事后发现而不是事前阻止。
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
from dataclasses import dataclass, field

SKIP_DIRS = {".git", ".nanocode", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache", ".pytest_cache",
             ".ruff_cache", ".tox"}
MAX_FILES = 20000
MAX_HASH_BYTES = 1_000_000   # 超过这个大小的文件不读内容，只比较大小和修改时间
MAX_LISTED = 8
RETRIES = 2   # 验收没过时，最多让子 Agent 重做几次


class ScopeError(ValueError):
    pass


def parse_scope(patterns) -> list[str] | None:
    """整理 allowed_paths。None = 没有限制；空列表 = 一个文件都不许改（只读任务）。"""
    if patterns is None:
        return None
    if not isinstance(patterns, list) or not all(isinstance(p, str) and p.strip() for p in patterns):
        raise ScopeError("allowed_paths 必须是非空字符串组成的列表")
    out = []
    for p in patterns:
        p = p.strip().replace("\\", "/")
        if os.path.isabs(p) or ".." in p.split("/"):
            raise ScopeError(f"allowed_paths 只能写相对工作目录的路径，不能含 ..：{p}")
        out.append(p.removeprefix("./"))
    return out


def in_scope(rel: str, patterns: list[str]) -> bool:
    """rel 是否在允许范围内。模式可以是文件、目录（前缀）或通配符（* 可以跨目录）。"""
    rel = rel.replace(os.sep, "/")
    for p in patterns:
        base = p.rstrip("/")
        if rel == base or rel.startswith(base + "/") or fnmatch.fnmatchcase(rel, p):
            return True
    return False


def _digest(path: str, size: int) -> str | None:
    if size > MAX_HASH_BYTES:
        return None
    try:
        with open(path, "rb") as f:
            return hashlib.sha1(f.read()).hexdigest()
    except OSError:
        return None


def snapshot(root: str, prev: dict | None = None) -> dict[str, tuple] | None:
    """{相对路径: (大小, 修改时间, 内容摘要)}。文件太多就返回 None（表示没法统计）。
    传入上一次的快照 prev 时，大小和修改时间都没变的文件直接沿用旧摘要，只重新读变过的文件。"""
    snap: dict[str, tuple] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            path = os.path.join(dirpath, name)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            old = (prev or {}).get(rel)
            if old and old[:2] == (st.st_size, st.st_mtime_ns):
                snap[rel] = old
            else:
                snap[rel] = (st.st_size, st.st_mtime_ns, _digest(path, st.st_size))
            if len(snap) > MAX_FILES:
                return None
    return snap


def _differs(b: tuple | None, a: tuple | None) -> bool:
    if b is None or a is None:
        return b is not a            # 新增或删除
    if b[:2] == a[:2]:
        return False
    if b[2] is not None and a[2] is not None:
        return b[2] != a[2]          # 时间变了但内容一样（touch、改了又还原）不算
    return True


def changed(before: dict | None, after: dict | None) -> list[str] | None:
    if before is None or after is None:
        return None
    return sorted(p for p in set(before) | set(after) if _differs(before.get(p), after.get(p)))


@dataclass
class Report:
    changed: list[str] | None = None            # None = 没法统计
    out_of_scope: list[str] = field(default_factory=list)
    scope: list[str] | None = None
    check: str = ""
    check_status: str = "none"                  # none 没配置 / pass / fail / skipped（没能执行）
    check_attempts: int = 0
    check_note: str = ""                        # fail 时是输出末尾，skipped 时是原因
    gave_up: bool = False                       # 重试用完仍未通过

    @property
    def needs_redo(self) -> bool:
        """有明确的问题（越界 / 检查没过），值得让子 Agent 重做。检查没能执行不算：重做也解决不了。"""
        return bool(self.out_of_scope) or self.check_status == "fail"

    @property
    def ok(self) -> bool:
        return not self.out_of_scope and self.check_status in ("none", "pass") and not self.gave_up

    def to_dict(self) -> dict:
        return {"changed": self.changed, "out_of_scope": self.out_of_scope, "scope": self.scope, "check": self.check,
                "check_status": self.check_status, "check_attempts": self.check_attempts, "ok": self.ok}

    def render(self) -> str:
        lines = ["---", "[验收报告：由系统核对生成，不是子 Agent 自述]"]
        if self.changed is None:
            lines.append(f"- 改动文件：未能统计（文件超过 {MAX_FILES} 个）")
        elif not self.changed:
            lines.append("- 改动文件：无")
        else:
            shown = ", ".join(self.changed[:MAX_LISTED])
            more = f" 等共 {len(self.changed)} 个" if len(self.changed) > MAX_LISTED else ""
            lines.append(f"- 改动文件：{shown}{more}")
        if self.scope is not None:
            if self.out_of_scope:
                lines.append(f"- 范围：超出允许范围 {self.scope or '(只读，不许改任何文件)'} → {', '.join(self.out_of_scope[:MAX_LISTED])}"
                             f"{'（已要求子 Agent 还原，仍未解决）' if self.gave_up else ''}")
            else:
                lines.append("- 范围：均在允许范围内")
        if self.check_status == "pass":
            lines.append(f"- 检查：`{self.check}` 通过（第 {self.check_attempts} 次）")
        elif self.check_status == "fail":
            lines.append(f"- 检查：`{self.check}` **未通过**（已尝试 {self.check_attempts} 次），子 Agent 的结论不可信。最后输出：\n{self.check_note}")
        elif self.check_status == "skipped":
            lines.append(f"- 检查：`{self.check}` 没有执行（{self.check_note}），**未验证**")
        else:
            lines.append("- 检查：未配置，以上结论**未经验证**")
        return "\n".join(lines)


def feedback(report: Report, check_output: str = "") -> str:
    """验收没通过时，交还给子 Agent 的说明。"""
    parts = ["你的子任务没有通过验收，请修正后再汇报："]
    if report.out_of_scope:
        parts.append(f"- 你改动了不允许修改的文件：{', '.join(report.out_of_scope[:MAX_LISTED])}。"
                     f"允许范围：{report.scope or '(只读，不许改任何文件)'}。请把它们还原成原来的样子。")
    if report.check_status == "fail":
        parts.append(f"- 检查命令 `{report.check}` 未通过，输出如下：\n{check_output}")
    return "\n".join(parts)
