"""汇总一次运行的 trace（.jsonl），给验证报告用。

    python3 verification/trace_stats.py              # 自动找 $NANOCODE_HOME 下最新的 trace
    python3 verification/trace_stats.py 某个.jsonl    # 指定文件

输出：事件类型计数、用过的工具、被拒绝的操作、子 Agent、压缩、错误、最终回答、token 用量。
只读取文件，不修改任何东西。
"""

import collections
import glob
import json
import os
import sys


def latest_trace() -> str:
    home = os.environ.get("NANOCODE_HOME") or os.path.expanduser("~/.nanocode")
    files = glob.glob(os.path.join(home, "projects", "*", "traces", "*.jsonl"))
    if not files:
        sys.exit(f"在 {home}/projects/*/traces/ 下没有找到 trace。运行 nanocode 时加 --trace，并确认 NANOCODE_HOME 一致。")
    return max(files, key=os.path.getmtime)


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else latest_trace()
    with open(path, encoding="utf-8") as f:
        events = [json.loads(line) for line in f if line.strip()]
    types = collections.Counter(e["type"] for e in events)
    tools = collections.Counter(e["name"] for e in events if e["type"] == "tool_call")
    print(f"trace：{path}")
    print(f"事件数：{len(events)}")
    print("事件类型：" + ", ".join(f"{k}={v}" for k, v in sorted(types.items())))
    print("工具调用：" + (", ".join(f"{k}×{v}" for k, v in tools.most_common()) or "无"))
    steps = [e for e in events if e["type"] == "llm_request"]
    print(f"模型调用次数：{len(steps)}（主 Agent {sum(e.get('agent') == 'main' for e in steps)}）")
    for e in events:
        if e["type"] == "permission" and e["behavior"] == "deny":
            print(f"被拒绝：{e['name']} —— {e['reason'][:120]}")
        elif e["type"] == "tool_result" and e["is_error"]:
            print(f"工具报错：{e['name']} —— {e['content'][:160]!r}")
        elif e["type"] in ("subagent_start", "subagent_end"):
            print(f"{e['type']}：{e['description']}" + (f" → {e['result'][:160]!r}" if e["type"] == "subagent_end" else ""))
        elif e["type"] == "compact":
            print(f"上下文压缩：{e['before']} → {e['after']}（{'自动' if e['auto'] else '手动'}）")
        elif e["type"] == "hook":
            print(f"hook[{e['hook']}]：{(e.get('message') or '; '.join(e.get('warnings', [])))[:160]}")
        elif e["type"] == "error":
            print(f"错误：{e['message'][:300]}")
    ends = [e for e in events if e["type"] == "turn_end" and e.get("agent") == "main"]
    if ends:
        last = ends[-1]
        u = last["usage"]
        print(f"token：输入 {u['input_tokens']}，输出 {u['output_tokens']}{'（估算）' if u.get('estimated') else ''}")
        print("最终回答：\n" + (last["final"] or "(空)")[:1500])


if __name__ == "__main__":
    main()
