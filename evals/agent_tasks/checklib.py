"""各任务 check.py 共用的小工具。

check.py 的约定：
    python3 check.py <工作目录> <最终回答文件>
    打印一行 "PASS: 原因" 或 "FAIL: 原因"，退出码 0 = 通过，1 = 未通过。

判定只依赖 hidden/ 里的测试（运行时才复制进工作目录），Agent 改了自己能看到的测试也没用。
"""

import ast
import os
import re
import shutil
import subprocess
import sys


def task_dir() -> str:
    return os.path.dirname(os.path.abspath(sys.argv[0]))


def args() -> tuple[str, str]:
    work, answer_file = sys.argv[1], sys.argv[2]
    answer = ""
    if os.path.exists(answer_file):
        with open(answer_file, encoding="utf-8") as f:
            answer = f.read()
    return work, answer


def copy_hidden(work: str) -> None:
    src = os.path.join(task_dir(), "hidden")
    for name in os.listdir(src):
        shutil.copy(os.path.join(src, name), os.path.join(work, name))


def run_tests(work: str, pattern: str = "test_hidden*.py", timeout: int = 60) -> tuple[bool, str, int]:
    """返回 (是否全部通过, 输出末尾, 运行的测试数)。"""
    try:
        p = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", work, "-t", work, "-p", pattern],
                           cwd=work, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace",
                           # 自包含：不 import 仓库里的 nanocode（隐藏测试可能在任何目录里跑，#92）
                           env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    except subprocess.TimeoutExpired:
        return False, "测试超时（可能有死循环）", 0
    out = p.stdout + p.stderr
    m = re.search(r"Ran (\d+) tests?", out)
    return p.returncode == 0, out[-1500:], int(m.group(1)) if m else 0


def last_line(text: str) -> str:
    lines = [l for l in text.strip().splitlines() if l.strip()]
    return lines[-1][:200] if lines else ""


def functions_calling(path: str, callee: str) -> set[str]:
    """返回文件里调用了 callee 的顶层函数名。"""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    out = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and getattr(sub.func, "id", None) == callee:
                    out.add(node.name)
    return out


def finish(ok: bool, reason: str) -> None:
    print(("PASS: " if ok else "FAIL: ") + reason)
    sys.exit(0 if ok else 1)


def standard_check() -> None:
    """最常见的判定：复制隐藏测试，全部通过即 PASS。"""
    work, _ = args()
    copy_hidden(work)
    ok, out, n = run_tests(work)
    finish(ok and n > 0, f"隐藏测试 {n} 个，{'全部通过' if ok else '有失败：' + last_line(out)}")
