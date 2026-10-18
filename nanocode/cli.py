"""命令行界面。

用法：
    nanocode                         交互模式（默认使用 mock 模型，无需 API Key）
    nanocode -p "解释一下 main.py"     单次模式：执行完就退出（适合脚本 / CI）
    nanocode --web                   启动本地 Web 界面（带 Agent 内部状态调试面板）

    nanocode --provider deepseek --model deepseek-chat
    nanocode --provider custom --protocol openai --base-url https://你的中转/v1 --model xxx

CLI 本身只是事件总线的一个订阅者：它把事件渲染成彩色文字。
Agent 在后台线程里运行，主线程负责读键盘（权限确认、Ctrl-C 中断）。
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading

from . import review as review_mod
from .llm import LLMError
from .session import Options, Session

C = {"dim": "\033[2m", "bold": "\033[1m", "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
     "blue": "\033[34m", "magenta": "\033[35m", "cyan": "\033[36m", "reset": "\033[0m"}


def paint(text: str, *styles: str) -> str:
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return text
    return "".join(C[s] for s in styles) + text + C["reset"]


def summarize_input(name: str, input: dict) -> str:
    for key in ("command", "path", "pattern", "description", "name"):
        if key in input:
            return str(input[key])[:120]
    if name == "todo_write":
        return f"{len(input.get('todos', []))} 项"
    return json.dumps(input, ensure_ascii=False)[:120]


class Renderer:
    """把事件渲染到终端。"""

    def __init__(self, out=sys.stdout, quiet: bool = False):
        self.out = out
        self.quiet = quiet  # -p 模式：工具过程写到 stderr，stdout 只留最终文字
        self.in_text = False

    def w(self, s: str, err: bool = False) -> None:
        stream = sys.stderr if (err and self.quiet) else self.out
        stream.write(s)
        stream.flush()

    def __call__(self, e: dict) -> None:
        t = e["type"]
        sub = e.get("agent", "main") != "main"
        indent = "    " if sub else ""
        if t != "text_delta" and self.in_text:
            self.w("\n")
            self.in_text = False
        if t == "text_delta":
            if self.quiet and sub:
                return
            self.w(paint(e["text"], "dim") if sub else e["text"], err=sub)
            self.in_text = True
        elif t == "tool_call":
            self.w(f"{indent}{paint('●', 'cyan')} {paint(e['name'], 'bold')}({summarize_input(e['name'], e['input'])})\n", err=True)
        elif t == "tool_result":
            lines = e["content"].splitlines() or [""]
            color = "red" if e["is_error"] else "dim"
            preview = "\n".join(f"{indent}  {'⎿' if i == 0 else ' '} {l[:160]}" for i, l in enumerate(lines[:4]))
            more = f"\n{indent}    … 共 {len(lines)} 行" if len(lines) > 4 else ""
            self.w(paint(preview + more, color) + "\n", err=True)
        elif t == "permission" and e["behavior"] == "deny":
            self.w(paint(f"{indent}  ✗ {e['reason']}", "red") + "\n", err=True)
        elif t == "subagent_start":
            self.w(paint(f"── 子 Agent 启动：{e['description']} ──", "magenta") + "\n", err=True)
        elif t == "subagent_end":
            r = e.get("report") or {}
            n = "未统计" if r.get("changed") is None else f"改动 {len(r['changed'])} 个文件"
            check = {"pass": "检查通过", "fail": "检查未通过", "skipped": "检查未执行", "none": "未验证"}.get(r.get("check_status"), "")
            scope = f" · 越界 {len(r['out_of_scope'])} 个" if r.get("out_of_scope") else ""
            scope += f" · 统计范围外改动 {len(r['external'])} 处" if r.get("external") else ""
            bad = bool(r) and not r.get("ok", True)
            self.w(paint(f"── 子 Agent 完成：{e['description']}（{n} · {check}{scope}）──", "red" if bad else "magenta") + "\n", err=True)
        elif t == "compact":
            self.w(paint(f"↻ 上下文已{'自动' if e['auto'] else ''}压缩：{e['before']} → {e['after']} tokens", "yellow") + "\n", err=True)
        elif t == "hook":
            msg = e["message"] or "\n".join(e["warnings"])
            self.w(paint(f"⚓ hook[{e['hook']}] {msg[:300]}", "yellow") + "\n", err=True)
        elif t == "memory_pending":
            self.w(paint(f"💡 经验候选 #{e['index']}（来自被自动验证打回又修好的回合，确认后才会写入项目记忆）\n    {e['text']}\n"
                         f"    写入：/memory accept {e['index']}　丢弃：/memory reject {e['index']}", "yellow") + "\n", err=True)
        elif t == "review":
            ok = e["verify"] == "pass"
            head = review_mod.headline(e["files"], e["hidden"], e["verify"]).replace("**", "")
            self.w(paint(f"═ {head}", "green" if ok else "yellow") + "\n", err=True)
            for f in e["files"]:
                stat = f" (+{f['added']} -{f['removed']})" if f["added"] is not None else f"（{f['note']}）"
                self.w(f"  {f['status']}  {f['path']}{stat}\n", err=True)
                for line in (f["diff"] or "").splitlines()[:40]:
                    color = "green" if line.startswith("+") and not line.startswith("+++") else "red" if line.startswith("-") and not line.startswith("---") else "dim"
                    self.w(paint("    " + line[:160], color) + "\n", err=True)
            if e["hidden"]:
                self.w(f"  … 还有 {e['hidden']} 个文件未列出\n", err=True)
            self.w(paint("  提交、推送等最后一步仍需你确认。", "dim") + "\n", err=True)
        elif t == "verify":
            if e["status"] == "running":
                self.w(paint(f"⏳ 自动验证（第 {e['attempt']} 次）：{e['command']}", "yellow") + "\n", err=True)
            elif e["status"] == "pass":
                self.w(paint(f"✓ 自动验证通过（第 {e['attempt']} 次，{e['seconds']}s）", "green") + "\n", err=True)
            else:
                tail = "\n".join(f"    {l[:160]}" for l in e["output"].splitlines()[-6:])
                self.w(paint(f"✗ 自动验证未通过（第 {e['attempt']} 次，退出码 {e['exit_code']}）\n{tail}", "red") + "\n", err=True)
        elif t == "error":
            self.w(paint(f"✗ {e['message']}", "red") + "\n", err=True)
        elif t == "warning":
            self.w(paint(f"⚠ {e['message']}", "yellow") + "\n", err=True)
        elif t == "turn_end" and not sub:
            u = e["usage"]
            self.w(paint(f"[tokens 输入 {u['input_tokens']} / 输出 {u['output_tokens']}{' 估算' if u['estimated'] else ''}]", "dim") + "\n", err=True)


def interactive(session: Session) -> None:
    asks: queue.Queue = queue.Queue()

    def ask_fn(name, input, target):
        """在 Agent 线程里被调用：把问题交给主线程，等主线程回答。"""
        reply: queue.Queue = queue.Queue()
        asks.put((name, input, target, reply))
        return reply.get()

    session.permissions.ask_fn = ask_fn
    session.bus.subscribe(Renderer())
    banner = (f"nanocode · {session.provider_name}/{session.provider.model} · 权限模式 {session.permissions.mode}\n"
              f"工作目录 {session.cwd}\n输入 /help 查看命令，Ctrl-C 中断，Ctrl-D 退出")
    print(paint(banner, "dim"))
    if session.provider_name == "mock":
        print(paint("（当前是 Mock 模型，不需要 API Key，会按固定剧本演示 Agent 循环。随便输入一句话试试。）", "yellow"))
    try:
        import readline  # noqa: F401 —— 让 input() 支持方向键和历史
    except ImportError:
        pass

    while True:
        try:
            line = input(paint("\n> ", "green", "bold")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        prompt = line
        if line.startswith("#"):
            print(session.add_memory(line.lstrip("#")))
            continue
        if line.startswith("/"):
            if line.strip() in ("/exit", "/quit"):
                break
            text, prompt = session.handle_command(line)
            if text:
                print(text)
            if not prompt:
                continue

        worker = threading.Thread(target=_safe_send, args=(session, prompt), daemon=True)
        worker.start()
        while worker.is_alive():
            try:
                name, input_, target, reply = asks.get(timeout=0.1)
            except queue.Empty:
                continue
            except KeyboardInterrupt:
                session.agent.cancel.set()
                print(paint("\n正在中断……", "yellow"))
                continue
            print(paint(f"\n? 允许 {name} 吗？", "yellow", "bold"))
            print(paint(f"  {target or json.dumps(input_, ensure_ascii=False)[:500]}", "yellow"))
            try:
                ans = input(paint("  [y] 允许  [a] 总是允许  [n] 拒绝 > ", "yellow")).strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = "n"
                session.agent.cancel.set()
            reply.put({"y": "yes", "a": "always"}.get(ans[:1], "no"))
    session.close()


def ask_trust(cwd: str, items: list[str]) -> bool:
    print(paint(f"\n这个项目（{cwd}）的配置要求以下权限：", "yellow", "bold"), file=sys.stderr)
    for line in items:
        print(paint(f"  • {line}", "yellow"), file=sys.stderr)
    print(paint("只有在你信任这个项目的来源时才应同意。不同意的话这些设置会被忽略，其余功能照常可用。", "dim"), file=sys.stderr)
    try:
        return input(paint("信任这个项目吗？[y/N] ", "yellow")).strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
        return False


def _safe_send(session: Session, prompt: str) -> None:
    try:
        session.send(prompt)
    except Exception as e:  # noqa: BLE001
        print(paint(f"\n✗ 出错：{type(e).__name__}: {e}", "red"))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="nanocode", description="一个用来学习 Agent 架构的迷你编程 Agent")
    p.add_argument("-p", "--print", dest="prompt", help="单次模式：执行这条指令后退出")
    p.add_argument("--provider", help="厂商预设：mock / anthropic / openai / deepseek / qwen / kimi / glm / "
                                      "siliconflow / openrouter / ollama / custom")
    p.add_argument("--model", help="模型名")
    p.add_argument("--base-url", help="自定义 API 地址")
    p.add_argument("--protocol", choices=["openai", "anthropic"], help="自定义 provider 使用的协议")
    p.add_argument("--api-key", help="API Key（更推荐用环境变量）")
    p.add_argument("--mode", choices=["default", "accept_edits", "plan", "review", "bypass"],
                   help="权限模式；review = 终审：测试和项目内编辑自动放行，高风险操作必须确认，结束时汇总 diff")
    p.add_argument("--cwd", default=os.getcwd(), help="工作目录")
    p.add_argument("--trace", action="store_true", help="把所有事件记录到 ~/.nanocode/projects/<项目>/traces/*.jsonl")
    p.add_argument("--resume", action="store_true", help="恢复上一次会话")
    p.add_argument("--trust", action="store_true", help="信任当前项目的配置（hooks、MCP、allow 规则等），不再询问")
    p.add_argument("--verify", metavar="CMD", default="", help="自动验证命令：Agent 改动后想结束时先运行，不通过就让它继续修（如 \"pytest -q\"）")
    p.add_argument("--web", action="store_true", help="启动 Web 界面")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--public-host", action="append", default=[], metavar="HOST",
                   help="反向代理部署：信任这个对外的主机名/IP（可重复，或逗号分隔；也可用环境变量 NANOCODE_PUBLIC_HOST）")
    p.add_argument("--public-origin", action="append", default=[], metavar="ORIGIN",
                   help="反向代理部署：信任这个完整的 Origin，如 https://example.com:8443（可重复；"
                        "也可用环境变量 NANOCODE_PUBLIC_ORIGIN）")
    args = p.parse_args(argv)

    public_hosts, public_origins = [], []
    if args.web:
        from .web.server import normalize_host, normalize_origin, public_settings
        try:
            public_hosts, public_origins = public_settings(args.public_host, args.public_origin)
            for h in public_hosts:
                normalize_host(h)
            for o in public_origins:
                normalize_origin(o)
        except ValueError as e:
            print(paint(f"配置错误：{e}", "red"), file=sys.stderr)
            return 2

    opts = Options(provider=args.provider or "", model=args.model or "", base_url=args.base_url or "",
                   api_key=args.api_key or "", protocol=args.protocol or "", mode=args.mode or "",
                   cwd=args.cwd, trace=args.trace, mock_delay=0.0 if args.prompt else 0.02, trust=args.trust, verify=args.verify)
    try:
        trust_fn = ask_trust if sys.stdin.isatty() and not args.prompt else None
        session = Session(opts, log=lambda m: print(paint(m, "yellow"), file=sys.stderr), trust_fn=trust_fn)
    except LLMError as e:
        print(paint(f"配置错误：{e}", "red"), file=sys.stderr)
        return 2
    if args.resume:
        print(session.resume_latest(), file=sys.stderr)

    if args.web:
        from .web.server import serve
        serve(session, args.host, args.port, public_hosts, public_origins)
        return 0
    if args.prompt:
        session.bus.subscribe(Renderer(quiet=True))
        try:
            result = session.send(args.prompt)
            failed = session.agent.verify_failed
        finally:
            session.close()
        print()
        if failed:   # 检查没过：让脚本 / CI 能从退出码看出来
            print(paint(result[result.rfind("(自动验证未通过"):], "red"), file=sys.stderr)
            return 1
        return 0
    interactive(session)
    return 0


if __name__ == "__main__":
    sys.exit(main())
