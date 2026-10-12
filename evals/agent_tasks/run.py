"""Agent 任务评测：让 nanocode 用真实模型完成 10 个小型编程任务，自动判定成功与否。

    python3 evals/agent_tasks/run.py --provider deepseek --model deepseek-chat
    python3 evals/agent_tasks/run.py --provider openrouter --model openrouter/free --tasks T01,T04 --repeat 3
    python3 evals/agent_tasks/run.py --reference        # 不调模型，用参考答案验证评测框架本身

每个任务：
    tasks/<id>/task.json   提示词
    tasks/<id>/files/      初始文件（复制到一个全新的临时目录，Agent 在那里工作）
    tasks/<id>/hidden/     隐藏测试（判定时才复制进去，Agent 看不到也改不了）
    tasks/<id>/check.py    判定脚本：python3 check.py <工作目录> <最终回答文件>，退出码 0 = 通过
    tasks/<id>/solution/   参考答案（只给 --reference 和单元测试用）

记录的指标：是否通过、模型调用次数（步数）、工具调用次数、工具报错次数、token、耗时。
结果写到 results/ 下的 JSON（完整数据）和 Markdown（汇总表）。

安全提示：评测默认用 bypass 模式（不逐条确认），Agent 会在临时目录里执行模型生成的命令。
任务本身是无害的，但模型的行为无法保证。条件允许的话请在容器或虚拟机里运行；
也可以用 --mode accept_edits，只自动允许改文件，命令仍会因为没人确认而被拒绝（成功率会下降）。
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
TASKS = os.path.join(HERE, "tasks")
sys.path.insert(0, ROOT)


def load_tasks(selected: str = "") -> list[dict]:
    wanted = {s.strip() for s in selected.split(",") if s.strip()}
    tasks = []
    for name in sorted(os.listdir(TASKS)):
        d = os.path.join(TASKS, name)
        if not os.path.isfile(os.path.join(d, "task.json")):
            continue
        if wanted and not any(name == w or name.startswith(w + "-") for w in wanted):
            continue
        with open(os.path.join(d, "task.json"), encoding="utf-8") as f:
            tasks.append({**json.load(f), "dir": d})
    return tasks


def prepare(task: dict) -> str:
    work = tempfile.mkdtemp(prefix=f"nc-eval-{task['id'][:3]}-")
    shutil.copytree(os.path.join(task["dir"], "files"), work, dirs_exist_ok=True)
    return work


def apply_reference(task: dict, work: str) -> str:
    """把参考答案放进工作目录，返回参考回答文本。"""
    answer = ""
    sol = os.path.join(task["dir"], "solution")
    for name in os.listdir(sol):
        if name == "__answer__":
            with open(os.path.join(sol, name), encoding="utf-8") as f:
                answer = f.read()
        else:
            shutil.copy(os.path.join(sol, name), os.path.join(work, name))
    return answer


def run_check(task: dict, work: str, answer: str) -> tuple[bool, str]:
    fd, answer_file = tempfile.mkstemp(suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(answer or "")
    try:
        p = subprocess.run([sys.executable, os.path.join(task["dir"], "check.py"), work, answer_file],
                           capture_output=True, text=True, timeout=300)
        lines = (p.stdout + p.stderr).strip().splitlines()
        return p.returncode == 0, (lines[-1] if lines else f"判定脚本无输出（退出码 {p.returncode}）")[:300]
    except subprocess.TimeoutExpired:
        return False, "判定超时"
    finally:
        os.remove(answer_file)


def run_agent(task: dict, work: str, args) -> dict:
    from nanocode.session import Options, Session
    events: list[dict] = []
    logs: list[str] = []
    opts = Options(provider=args.provider, model=args.model, base_url=args.base_url, protocol=args.protocol,
                   mode=args.mode, cwd=work, mock_delay=0)
    started = time.time()
    session = Session(opts, log=logs.append)
    session.bus.subscribe(lambda e: e["type"] != "text_delta" and events.append(e))
    session.agent.max_steps = args.max_steps
    timer = threading.Timer(args.timeout, session.agent.cancel.set)
    timer.start()
    final, error = "", ""
    try:
        final = session.send(task["prompt"])
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"
    finally:
        timer.cancel()
        session.close()
    errors = [e for e in events if e["type"] == "error"]
    return {
        "final": final,
        "error": error or (errors[-1]["message"] if errors else ""),
        "timed_out": session.agent.cancel.is_set(),
        "steps": sum(1 for e in events if e["type"] == "llm_request"),
        "tool_calls": sum(1 for e in events if e["type"] == "tool_call"),
        "tool_errors": sum(1 for e in events if e["type"] == "tool_result" and e["is_error"]),
        "denied": sum(1 for e in events if e["type"] == "permission" and e["behavior"] == "deny"),
        "tools_used": sorted({e["name"] for e in events if e["type"] == "tool_call"}),
        "input_tokens": session.agent.usage.input_tokens,
        "output_tokens": session.agent.usage.output_tokens,
        "usage_estimated": session.agent.usage.estimated,
        "seconds": round(time.time() - started, 1),
    }


def git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
                              text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def summarize(runs: list[dict]) -> list[dict]:
    rows = []
    for tid in sorted({r["task"] for r in runs}):
        rs = [r for r in runs if r["task"] == tid]
        avg = lambda k: round(sum(r.get(k, 0) for r in rs) / len(rs), 1)  # noqa: E731
        fails = [r["check_reason"] for r in rs if not r["passed"]]
        rows.append({"task": tid, "passed": sum(r["passed"] for r in rs), "runs": len(rs),
                     "steps": avg("steps"), "tool_calls": avg("tool_calls"), "tool_errors": avg("tool_errors"),
                     "input_tokens": avg("input_tokens"), "output_tokens": avg("output_tokens"),
                     "seconds": avg("seconds"), "last_failure": fails[-1] if fails else ""})
    return rows


def markdown(meta: dict, rows: list[dict]) -> str:
    total = sum(r["runs"] for r in rows)
    passed = sum(r["passed"] for r in rows)
    lines = [f"# Agent 任务评测：{meta['provider']}/{meta['model']}", "",
             f"- 时间：{meta['date']}　nanocode 提交：{meta['commit'] or '未知'}　Python {meta['python']}　{meta['platform']}",
             f"- 权限模式：{meta['mode']}　最大步数：{meta['max_steps']}　单任务超时：{meta['timeout']}s　每题重复：{meta['repeat']}",
             f"- **通过率：{passed}/{total}（{passed / total:.0%}）**" if total else "- 没有运行任何任务",
             "", "| 任务 | 通过 | 平均步数 | 工具调用 | 工具报错 | 输入 tokens | 输出 tokens | 耗时(s) | 最近一次失败原因 |",
             "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['task']} | {r['passed']}/{r['runs']} | {r['steps']} | {r['tool_calls']} | {r['tool_errors']} | "
                     f"{r['input_tokens']:.0f} | {r['output_tokens']:.0f} | {r['seconds']} | {r['last_failure'].replace('|', '/')} |")
    if meta.get("usage_estimated"):
        lines += ["", "注：该 provider 没有返回 token 用量，表中 token 为估算值。"]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="nanocode Agent 任务评测")
    ap.add_argument("--provider", default=os.environ.get("NANOCODE_PROVIDER", "mock"))
    ap.add_argument("--model", default=os.environ.get("NANOCODE_MODEL", ""))
    ap.add_argument("--base-url", default="")
    ap.add_argument("--protocol", default="")
    ap.add_argument("--mode", default="bypass", choices=["bypass", "accept_edits", "default"])
    ap.add_argument("--tasks", default="", help="只跑部分任务，如 T01,T04")
    ap.add_argument("--repeat", type=int, default=1, help="每个任务跑几次（模型有随机性，建议 ≥3）")
    ap.add_argument("--max-steps", type=int, default=30)
    ap.add_argument("--timeout", type=int, default=600, help="单个任务的超时秒数")
    ap.add_argument("--reference", action="store_true", help="不调模型，用参考答案验证评测框架")
    ap.add_argument("--keep", action="store_true", help="保留每个任务的工作目录，便于排查")
    ap.add_argument("--out", default=os.path.join(HERE, "results"))
    args = ap.parse_args(argv)

    if not args.reference and args.mode == "bypass":
        print("提示：bypass 模式下 Agent 会直接执行模型生成的命令（在临时目录里）。建议在容器或虚拟机中运行。\n",
              file=sys.stderr)
    # 评测不读写你自己的 ~/.nanocode（会话记录、信任记录）
    os.environ.setdefault("NANOCODE_HOME", tempfile.mkdtemp(prefix="nc-eval-home-"))

    tasks = load_tasks(args.tasks)
    if not tasks:
        print("没有匹配的任务", file=sys.stderr)
        return 2
    runs = []
    for task in tasks:
        for i in range(args.repeat):
            work = prepare(task)
            if args.reference:
                answer, stats = apply_reference(task, work), {"steps": 0, "tool_calls": 0, "tool_errors": 0,
                                                              "input_tokens": 0, "output_tokens": 0, "seconds": 0}
            else:
                stats = run_agent(task, work, args)
                answer = stats["final"]
            passed, reason = run_check(task, work, answer)
            runs.append({"task": task["id"], "attempt": i + 1, "passed": passed, "check_reason": reason,
                         "workdir": work if args.keep else "", **stats})
            print(f"{'✓' if passed else '✗'} {task['id']} #{i + 1}  步数 {stats['steps']}  {reason}", flush=True)
            if not args.keep:
                shutil.rmtree(work, ignore_errors=True)

    meta = {"provider": "reference" if args.reference else args.provider,
            "model": "-" if args.reference else (args.model or "default"),
            "date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "commit": git_commit(),
            "python": platform.python_version(), "platform": f"{platform.system()} {platform.machine()}",
            "mode": args.mode, "max_steps": args.max_steps, "timeout": args.timeout, "repeat": args.repeat,
            "usage_estimated": any(r.get("usage_estimated") for r in runs)}
    rows = summarize(runs)
    os.makedirs(args.out, exist_ok=True)
    stem = f"{datetime.datetime.now():%Y%m%d-%H%M}-{re.sub(r'[^A-Za-z0-9_.-]+', '_', meta['provider'] + '-' + meta['model'])}"
    with open(os.path.join(args.out, stem + ".json"), "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "summary": rows, "runs": runs}, f, ensure_ascii=False, indent=1)
    report = markdown(meta, rows)
    with open(os.path.join(args.out, stem + ".md"), "w", encoding="utf-8") as f:
        f.write(report)
    print("\n" + report)
    print(f"结果已写入 {os.path.join(args.out, stem)}.json / .md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
