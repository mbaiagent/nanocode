"""终审汇总（--mode review）：任务结束时，把本轮所有改动、diff 和检查结果摆到你面前。

终审模式的想法是：中间那些无害的步骤（读文件、改项目内的文件、跑测试）不逐条打断你，
但是**结束时**你要能一眼看清到底改了什么 —— 然后由你决定最后一步（提交、推送、合并）。
提交 / 推送本来就不在免确认的白名单里，所以仍然会问你。

diff 的来源：写文件工具（write_file / edit_file）动手之前，先记下这个文件的原文；结束时拿现在的内容比。
bash 造成的改动（比如 sed -i）没有原文，只能列出文件名，并标明"经 bash 改动，无 diff"。
太大（超过 200KB）或不是文本的文件同样只列文件名。已知局限：这是汇总，不是回滚 —— 看完觉得不对，
需要你自己用 git 之类的工具还原。
"""

from __future__ import annotations

import difflib
import os

from .tools.base import norm_rel, safe_relpath

MAX_ORIGINAL_BYTES = 200_000
MAX_FILES = 30
MAX_DIFF_CHARS = 6000        # 单个文件的 diff 最多保留多少字符
UNRECORDED = object()        # 原文太大或不是文本：只列文件名


def read_text(path: str):
    """读出文本内容；不存在返回 None；太大或不是文本返回 UNRECORDED。"""
    try:
        if os.path.getsize(path) > MAX_ORIGINAL_BYTES:
            return UNRECORDED
        with open(path, "rb") as f:
            return f.read().decode("utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError):
        return UNRECORDED


def remember(originals: dict, cwd: str, path: str) -> None:
    """写文件工具动手之前调用：这个文件本轮第一次被改，就记下原文。"""
    # 必须用和 subcheck.snapshot 相同的规范化（/ 分隔）：Windows 上 relpath 给
    # `pkg\\a.txt`，快照给 `pkg/a.txt`，同一个文件会在终审里出现两次（#45）
    rel = norm_rel(safe_relpath(path, cwd))
    if rel not in originals:
        originals[rel] = read_text(path)


def build(originals: dict, changed: list[str] | None, cwd: str) -> tuple[list[dict], int]:
    """返回 (文件清单, 因数量上限没列出的个数)。"""
    rels = sorted(set(originals) | set(changed or []))
    entries = []
    for rel in rels:
        now = read_text(os.path.join(cwd, rel))
        if rel in originals:
            old = originals[rel]
            if old is UNRECORDED or now is UNRECORDED:
                entries.append({"path": rel, "status": "修改", "diff": None, "added": None, "removed": None,
                                "note": "文件较大或不是文本，不显示 diff"})
                continue
            if old == now:
                continue                       # 改了又改回来，净改动为零
            status = "新增" if old is None else ("删除" if now is None else "修改")
            lines = list(difflib.unified_diff((old or "").splitlines(), (now or "").splitlines(), f"a/{rel}", f"b/{rel}",
                                              lineterm="", n=2))
            added = sum(1 for l in lines if l.startswith("+") and not l.startswith("+++"))
            removed = sum(1 for l in lines if l.startswith("-") and not l.startswith("---"))
            diff = "\n".join(lines)
            if len(diff) > MAX_DIFF_CHARS:
                diff = diff[:MAX_DIFF_CHARS] + f"\n… [diff 过长，已截断，共 {added} 行新增 {removed} 行删除]"
            entries.append({"path": rel, "status": status, "diff": diff, "added": added, "removed": removed, "note": ""})
        else:
            entries.append({"path": rel, "status": "删除" if now is None else "改动", "diff": None, "added": None,
                            "removed": None, "note": "经 bash 改动，无 diff"})
    hidden = max(0, len(entries) - MAX_FILES)
    return entries[:MAX_FILES], hidden


def headline(entries: list[dict], hidden: int, verify: str | None) -> str:
    total = len(entries) + hidden
    check = {"pass": "自动验证：通过", "fail": "自动验证：**未通过**", None: "自动验证：未配置或未运行"}.get(verify, "")
    return f"终审：本轮改动 {total} 个文件 · {check}"
