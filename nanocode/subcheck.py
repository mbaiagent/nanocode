"""子 Agent 的验收：它说了不算，由系统去核对。

主 Agent 派出子任务时可以带两个可选条件（见 tools/planning.py 的 task 工具）：
  check          一条检查命令（如 pytest -q tests/test_x.py）。子 Agent 想交差前先跑，不过就让它继续修。
  allowed_paths  允许修改的路径范围（相对工作目录，如 ["src/parser/*", "tests/test_parser.py"]）。
                 写文件工具越界会被直接拒绝；bash 等绕不开的改动，靠前后快照对比发现。

不管有没有带条件，子 Agent 返回的结论后面都会附一份"验收报告"：改了哪些文件、有没有越界、
检查有没有通过。报告是系统生成的，不是子 Agent 自述 —— 主 Agent 靠它区分"确认过的事实"和"子 Agent 的一面之词"。

快照比较的是内容摘要：改了又还原、只 touch 没改内容，都不算改动。超过 1MB 的文件只比较大小和修改时间。
文件数超过上限时放弃统计，报告必须写"未能统计、未经验证"，ok 为 False —— 一份什么都没核对的报告，
不能看起来像核对过了（#39）。bash 里改的东西只能靠快照发现，所以是事后发现而不是事前阻止。

快照有两块看不见的地方，报告不许假装看得见（#39）：
  1. 统计失败（文件数超过上限）—— changed 为 None；
  2. 统计盲区 —— cwd 里被 SKIP_DIRS 跳过的目录，和 cwd 之外的整个文件系统
     （子 Agent 用一条已放行的 bash 就能写 ~/.bashrc、.git/hooks/pre-commit）。
     对其中最危险的几个"会被自动执行"的落点有定向监测，见 external_snapshot。
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
from dataclasses import dataclass, field

from .tools.base import norm_rel

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
    rel = norm_rel(rel)
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


# ---------------------------------------------------------------- 统计盲区的定向监测
#
# snapshot 只覆盖 cwd 内、且跳过 .git / .nanocode 等目录；cwd 之外（家目录、/tmp、别处）
# 整个文件系统都不在统计内。bash 写得进其中任何地方，而"改动文件：无"只意味着项目树里没变 ——
# 把这两件事混为一谈，就是给子 Agent 的自述发假印章（#39）。
#
# 全盘监测不可行（家目录又大又杂，mtime 天天在变，全是无关噪音），只盯最危险的几个落点：
# 它们有一个共同点 —— **都会被自动执行**，改一次，一次普通的登录 / git 操作 / 启动 nanocode
# 就能把后门跑起来，而且没有一个在 allowed_paths 的管辖范围里：
#   ~/.bashrc 等 shell 启动文件   下次登录 shell 时执行
#   ~/.nanocode/settings.json     下次启动 nanocode 时执行（用户级 hooks/MCP/verify）
#   <项目>/.nanocode/**           下次启动 nanocode 时执行（项目级 hooks/MCP/verify 及脚本）
#   <项目>/.git/hooks/*           下次 git 操作时执行（pre-commit 等）
HOME_WATCH = (".bashrc", ".zshrc", ".bash_profile", ".zprofile", ".profile")
PROJECT_WATCH_DIRS = ((".nanocode",), (".git", "hooks"))


def external_snapshot(root: str, home: str | None = None) -> dict[str, tuple]:
    """对统计盲区做定向监测，返回 {展示名: (大小, mtime_ns) 或 None（不存在）}。

    只记录大小和修改时间，不读内容：这些位置小、敏感，但不属于任务产出，
    为它们全文建索引的代价和噪音都不值得；mtime + 大小足以发现"被改过"。
    home 参数只是为测试可注入；真实 home 用 expanduser（不用 home_dir()，
    那个是 nanocode 自己的配置目录，另外单独监测）。
    """
    home = os.path.expanduser("~") if home is None else home
    # home_dir() 放函数里 import：subcheck 被 tools/planning.py 引用，而 context 又依赖
    # tools.base，模块级 import 会成环（tools → planning → subcheck → context → tools）。
    from .context import home_dir
    out: dict[str, tuple | None] = {}

    def watch(display: str, path: str) -> None:
        try:
            st = os.lstat(path)
        except OSError:
            out[display] = None      # 不存在：之后"出现"（新建文件）也算变化
        else:
            out[display] = (st.st_size, st.st_mtime_ns)

    for name in HOME_WATCH:
        watch(f"~/{name}", os.path.join(home, name))
    watch("~/.nanocode/settings.json", os.path.join(home_dir(), "settings.json"))
    for parts in PROJECT_WATCH_DIRS:
        d = os.path.join(root, *parts)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            path = os.path.join(d, name)
            if os.path.isfile(path):     # 只监测文件：目录整体的增删由 listdir 的差异体现
                watch(f"{'/'.join(parts)}/{name}", path)
    return out


def external_changed(before: dict | None, after: dict | None) -> list[str]:
    """两次定向监测之间，盲区里哪些位置变了（含新建 / 删除）。"""
    if before is None or after is None:
        return []
    return sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))


def external_violations(before: dict, after: dict, scope: list[str] | None) -> list[str]:
    """盲区里被改、且不在 allowed_paths 明确放行范围内的位置。

    项目内被监测的位置（.nanocode/、.git/hooks/）如果本来就在允许范围内，是任务授权
    的改动，不上报；家目录等永远在范围外，改一丝丝也要说。scope 为 None（不限范围）时
    全部上报 —— 不改什么不等于可以改哪儿，报告至少要让主 Agent 知道这些改动没被统计。
    """
    hits = external_changed(before, after)
    if scope is None:
        return hits
    return [h for h in hits if h.startswith("~") or not in_scope(h, scope)]


@dataclass
class Report:
    changed: list[str] | None = None            # None = 没法统计
    out_of_scope: list[str] = field(default_factory=list)
    scope: list[str] | None = None
    external: list[str] = field(default_factory=list)   # 统计盲区里被改的位置（#39）
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
        """验收通过。统计失败（changed is None）或盲区里发现改动时不算通过：
        报告的承诺是"系统核对过"，核对不了就不能算过 —— 宁可让用户多确认一次，
        也不要一份看起来像验证过了的报告（#39）。"""
        return (not self.out_of_scope and self.check_status in ("none", "pass") and not self.gave_up
                and self.changed is not None and not self.external)

    def to_dict(self) -> dict:
        return {"changed": self.changed, "out_of_scope": self.out_of_scope, "scope": self.scope, "external": self.external,
                "check": self.check, "check_status": self.check_status, "check_attempts": self.check_attempts,
                "ok": self.ok}

    def render(self) -> str:
        lines = ["---", "[验收报告：由系统核对生成，不是子 Agent 自述]"]
        if self.changed is None:
            lines.append(f"- 改动文件：未能统计（文件超过 {MAX_FILES} 个），**未经验证**")
        elif not self.changed:
            lines.append("- 改动文件：无")
        else:
            shown = ", ".join(self.changed[:MAX_LISTED])
            more = f" 等共 {len(self.changed)} 个" if len(self.changed) > MAX_LISTED else ""
            lines.append(f"- 改动文件：{shown}{more}")
        if self.scope is not None:
            if self.changed is None:
                # 统计失败时 out_of_scope 必然是空的，以前照样打印"均在允许范围内" ——
                # 一份什么都没核对的报告看起来却像核对过了（#39）
                lines.append("- 范围：**未能核实**（改动没能统计，范围检查没有执行）")
            elif self.out_of_scope:
                lines.append(f"- 范围：超出允许范围 {self.scope or '(只读，不许改任何文件)'} → {', '.join(self.out_of_scope[:MAX_LISTED])}"
                             f"{'（已要求子 Agent 还原，仍未解决）' if self.gave_up else ''}")
            else:
                lines.append("- 范围：均在允许范围内")
        if self.external:
            shown = ", ".join(self.external[:MAX_LISTED])
            more = f" 等共 {len(self.external)} 处" if len(self.external) > MAX_LISTED else ""
            lines.append(f"- 统计范围外的改动：{shown}{more}（不在快照和 allowed_paths 的覆盖范围内，请人工确认）")
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
