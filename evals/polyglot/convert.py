"""把 Aider Polyglot 基准（Exercism 习题）转成 nanocode 评测框架的任务格式。

    # 1. 取题库（公开仓库，题目内容版权归 Exercism，不随本仓库分发）
    git clone --depth 1 https://github.com/Aider-AI/polyglot-benchmark /tmp/polyglot-benchmark

    # 2. 转换（默认输出到 evals/polyglot/tasks/，已加入 .gitignore）
    python3 evals/polyglot/convert.py --src /tmp/polyglot-benchmark --language python --limit 10 --seed 0

    # 3. 用现有运行器评测（先 --reference 自检，再用真实模型）
    python3 evals/agent_tasks/run.py --tasks-dir evals/polyglot/tasks --reference
    python3 evals/agent_tasks/run.py --tasks-dir evals/polyglot/tasks --provider deepseek --model deepseek-chat --repeat 3

生成的每个任务和 evals/agent_tasks/tasks/ 里的一样：
    task.json   提示词（含题目说明）
    files/      初始文件：桩代码 + 可见的测试（Agent 可以自己跑测试验证）
    hidden/     判定用的测试（判定时覆盖回工作目录，Agent 改了可见测试也没用）
    check.py    判定脚本：python3 check.py <工作目录> <最终回答文件>，退出码 0 = 通过
    solution/   参考答案（Exercism 的 .meta/example，只给 --reference 用）

默认会逐题自检：参考答案必须通过、桩代码原样必须失败。自检不过的题会被跳过并说明原因，
这样评测出来的分数才有意义。

目前只支持 python。其他语言需要对应的工具链和测试命令，加到 LANGUAGES 里即可。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(HERE, "tasks")

# 判定脚本模板：自包含，不依赖仓库里的 checklib，所以任务目录放哪儿都能跑
CHECK_PY = '''\
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = {tests!r}          # 判定用的测试文件（相对路径）
TIMEOUT = 120

work = sys.argv[1]
# 把测试文件还原成 hidden/ 里的原版：Agent 改了可见的测试也没用
for rel in TESTS:
    dst = os.path.join(work, rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy(os.path.join(HERE, "hidden", rel), dst)

total = 0
for rel in TESTS:
    try:
        p = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", os.path.join(work, os.path.dirname(rel)),
                            "-t", work, "-p", os.path.basename(rel)],
                           cwd=work, capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        print("FAIL: 测试超时（可能有死循环）")
        sys.exit(1)
    out = p.stdout + p.stderr
    m = re.search(r"Ran (\\d+) tests?", out)
    ran = int(m.group(1)) if m else 0
    if ran == 0:
        continue    # 比如 test_utils.py 这种只放辅助代码的文件，没有测试可跑
    if p.returncode != 0:
        lines = [l for l in out.strip().splitlines() if l.strip()]
        print("FAIL: " + (lines[-1][:200] if lines else "没有输出"))
        sys.exit(1)
    total += ran
if total == 0:
    print("FAIL: 没有运行任何测试")
    sys.exit(1)
print(f"PASS: {{total}} 个测试通过")
'''


class Skip(Exception):
    """这道题转不了（缺文件等），带上原因。"""


def read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def load_python(src: str, name: str) -> dict:
    d = os.path.join(src, "python", "exercises", "practice", name)
    cfg_path = os.path.join(d, ".meta", "config.json")
    if not os.path.isfile(cfg_path):
        raise Skip("没有 .meta/config.json")
    files = json.loads(read(cfg_path)).get("files", {})
    solution, tests, example = files.get("solution", []), files.get("test", []), files.get("example", [])
    if not solution or not tests or len(example) != len(solution):
        raise Skip("config.json 里的 solution / test / example 不完整或数量对不上")
    for rel in solution + tests:
        if not os.path.isfile(os.path.join(d, rel)):
            raise Skip(f"缺少文件 {rel}")
    for rel in example:
        if not os.path.isfile(os.path.join(d, rel)):
            raise Skip(f"缺少参考答案 {rel}")
    instr = os.path.join(d, ".docs", "instructions.md")
    if not os.path.isfile(instr):
        raise Skip("没有 .docs/instructions.md")
    text = read(instr).strip()
    append = os.path.join(d, ".docs", "instructions.append.md")
    if os.path.isfile(append):
        text += "\n\n" + read(append).strip()
    return {"dir": d, "solution": solution, "tests": tests, "example": example, "instructions": text}


LANGUAGES = {"python": {"prefix": "py", "list_dir": ("python", "exercises", "practice"), "load": load_python}}


def list_exercises(src: str, language: str) -> list[str]:
    root = os.path.join(src, *LANGUAGES[language]["list_dir"])
    if not os.path.isdir(root):
        raise SystemExit(f"找不到题库目录 {root}（--src 要指向 polyglot-benchmark 仓库的根目录）")
    return sorted(n for n in os.listdir(root) if os.path.isdir(os.path.join(root, n)))


def build_prompt(ex: dict) -> str:
    stubs = "、".join(f"`{s}`" for s in ex["solution"])
    tests = "、".join(f"`{t}`" for t in ex["tests"])
    return (f"在当前目录里实现 {stubs}，让测试 {tests} 全部通过。不要修改测试文件。\n\n"
            f"题目说明如下：\n\n{ex['instructions']}")


def copy_tree(src: str, dst: str) -> None:
    skip = {".meta", ".docs", "__pycache__", ".git"}
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [x for x in dirnames if x not in skip]
        rel = os.path.relpath(dirpath, src)
        os.makedirs(os.path.join(dst, rel) if rel != "." else dst, exist_ok=True)
        for fn in filenames:
            shutil.copy(os.path.join(dirpath, fn), os.path.join(dst, rel, fn) if rel != "." else os.path.join(dst, fn))


def write_task(out: str, language: str, name: str, ex: dict) -> str:
    task_id = f"PG-{LANGUAGES[language]['prefix']}-{name}"
    t = os.path.join(out, task_id)
    if os.path.exists(t):
        shutil.rmtree(t)
    os.makedirs(os.path.join(t, "hidden"))
    os.makedirs(os.path.join(t, "solution"))
    copy_tree(ex["dir"], os.path.join(t, "files"))
    for rel in ex["tests"]:
        dst = os.path.join(t, "hidden", rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy(os.path.join(ex["dir"], rel), dst)
    for stub, example in zip(ex["solution"], ex["example"]):
        dst = os.path.join(t, "solution", stub)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy(os.path.join(ex["dir"], example), dst)
    with open(os.path.join(t, "task.json"), "w", encoding="utf-8") as f:
        json.dump({"id": task_id, "title": f"Polyglot {language}: {name}", "kind": "code",
                   "source": "Aider polyglot-benchmark / Exercism", "prompt": build_prompt(ex)},
                  f, ensure_ascii=False, indent=2)
    with open(os.path.join(t, "check.py"), "w", encoding="utf-8") as f:
        f.write(CHECK_PY.format(tests=list(ex["tests"])))
    return t


def run_check(task_dir: str, work: str) -> tuple[bool, str]:
    fd, answer = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    try:
        p = subprocess.run([sys.executable, os.path.join(task_dir, "check.py"), work, answer],
                           capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return False, "判定超时"
    finally:
        os.remove(answer)
    lines = (p.stdout + p.stderr).strip().splitlines()
    return p.returncode == 0, lines[-1][:200] if lines else "判定脚本无输出"


def self_check(task_dir: str) -> str | None:
    """参考答案必须通过、桩代码原样必须失败。返回 None 表示通过，否则是原因。"""
    work = tempfile.mkdtemp(prefix="pg-check-")
    try:
        shutil.copytree(os.path.join(task_dir, "files"), work, dirs_exist_ok=True)
        ok, why = run_check(task_dir, work)
        if ok:
            return "桩代码不做任何修改就通过了测试，判定没有意义"
        shutil.copytree(os.path.join(task_dir, "solution"), work, dirs_exist_ok=True)
        ok, why = run_check(task_dir, work)
        if not ok:
            return f"参考答案没能通过测试：{why}"
        return None
    finally:
        shutil.rmtree(work, ignore_errors=True)


def select(names: list[str], exercises: str, limit: int, seed: int) -> list[str]:
    if exercises:
        wanted = [x.strip() for x in exercises.split(",") if x.strip()]
        missing = [x for x in wanted if x not in names]
        if missing:
            raise SystemExit(f"题库里没有这些题：{', '.join(missing)}（用 --list 看全部）")
        return wanted
    if limit and limit < len(names):
        picked = random.Random(seed).sample(names, limit)   # 固定种子：前后对比时用的是同一批题
        return sorted(picked)
    return names


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="把 Aider Polyglot 习题转成 nanocode 评测任务")
    ap.add_argument("--src", required=True, help="polyglot-benchmark 仓库的根目录")
    ap.add_argument("--language", default="python", choices=sorted(LANGUAGES))
    ap.add_argument("--exercises", default="", help="指定题目，逗号分隔，如 two-bucket,wordy")
    ap.add_argument("--limit", type=int, default=0, help="随机抽 N 题（配合 --seed，结果可复现）")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--no-verify", action="store_true", help="跳过逐题自检（不建议）")
    ap.add_argument("--list", action="store_true", help="只列出题库里有哪些题")
    args = ap.parse_args(argv)

    names = list_exercises(args.src, args.language)
    if args.list:
        print("\n".join(names))
        return 0
    chosen = select(names, args.exercises, args.limit, args.seed)
    os.makedirs(args.out, exist_ok=True)

    made, skipped = [], []
    for name in chosen:
        try:
            ex = LANGUAGES[args.language]["load"](args.src, name)
            t = write_task(args.out, args.language, name, ex)
        except Skip as e:
            skipped.append((name, str(e)))
            continue
        if not args.no_verify:
            problem = self_check(t)
            if problem:
                shutil.rmtree(t, ignore_errors=True)
                skipped.append((name, problem))
                continue
        made.append(os.path.basename(t))
    print(f"生成 {len(made)} 个任务 → {args.out}")
    for tid in made:
        print(f"  ✓ {tid}")
    for name, why in skipped:
        print(f"  ✗ 跳过 {name}：{why}", file=sys.stderr)
    return 0 if made else 1


if __name__ == "__main__":
    sys.exit(main())
