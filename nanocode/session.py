"""把所有部件组装成一个可用的会话，并处理配置、斜杠命令、会话保存。

CLI 和 Web 界面都只和 Session 打交道。看这个文件可以了解各模块是如何拼起来的：

    配置 → Provider + 工具(内置 + MCP) + 权限 + Hooks + 技能 → Agent
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
from dataclasses import dataclass, field

from . import __version__
from .agent import Agent
from .context import MEMORY_FILE, build_system_prompt, context_tokens, discover_commands, discover_skills, home_dir
from .events import AuditWriter, EventBus, TraceWriter
from . import policy as policy_mod
from . import verify as verify_mod
from .hooks import HookRunner
from .llm import PRESETS, make_provider
from .mcp import connect_servers
from .permissions import MODES, PermissionManager
from .tools import ToolRegistry, default_tools
from .tools.planning import STATUS_ICON
from .trust import TrustFn, TrustStore, apply_trust


def _read_json(path: str) -> dict:
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_settings(cwd: str, trust_fn: TrustFn | None = None) -> tuple[dict, str]:
    """合并配置：~/.nanocode/settings.json（你自己的，直接生效）
              → 项目 .nanocode/settings.json → .nanocode/settings.local.json（项目的，敏感项需信任）。

    越靠后优先级越高；列表（allow/deny）和字典（hooks/mcpServers）会合并而不是覆盖。
    返回 (配置, 提示信息)。项目配置中能执行命令、放宽权限、改变请求去向的字段，
    在用户信任这个目录之前会被忽略，见 trust.py。
    """
    merged = _read_json(os.path.join(home_dir(), "settings.json"))
    project: dict = {}
    for name in ("settings.json", "settings.local.json"):
        _deep_merge(project, _read_json(os.path.join(cwd, ".nanocode", name)))
    effective, notice = apply_trust(cwd, project, TrustStore(os.path.join(home_dir(), "trusted.json")), trust_fn)
    _deep_merge(merged, effective)
    return merged, notice


def project_data_dir(cwd: str) -> str:
    """会话记录、trace 存在 ~/.nanocode/projects/<目录名>-<哈希>/，而不是写进用户的项目里。"""
    real = os.path.realpath(cwd)
    slug = re.sub(r"[^\w.-]", "_", os.path.basename(real) or "root")
    return os.path.join(home_dir(), "projects", f"{slug}-{hashlib.sha1(real.encode()).hexdigest()[:8]}")


def _deep_merge(base: dict, new: dict) -> None:
    for k, v in new.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        elif isinstance(v, list) and isinstance(base.get(k), list):
            base[k] = base[k] + [x for x in v if x not in base[k]]
        else:
            base[k] = v


@dataclass
class Options:
    provider: str = ""
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    protocol: str = ""
    mode: str = ""
    cwd: str = field(default_factory=os.getcwd)
    trace: bool = False
    mock_delay: float = 0.02
    trust: bool = False   # True = 不询问，直接信任项目配置（--trust）
    verify: str = ""       # 自动验证命令（--verify），优先于 settings.json 里的 verify


class Session:
    def __init__(self, opts: Options, log=print, trust_fn: TrustFn | None = None):
        self.cwd = os.path.realpath(opts.cwd)
        # 平台策略（多用户部署时由平台下发，见 policy.py）：优先级最高，盖过命令行参数和所有配置文件
        self.policy = policy_mod.load_policy()
        opts = policy_mod.lock_options(opts, self.policy)
        if opts.trust:
            trust_fn = lambda cwd, items: True  # noqa: E731
        self.settings, notice = load_settings(self.cwd, trust_fn)
        if notice:
            log(notice)
        s = self.settings = policy_mod.apply_policy(self.settings, self.policy, _deep_merge)
        provider_name = opts.provider or os.environ.get("NANOCODE_PROVIDER") or s.get("provider") or "mock"
        self.provider = make_provider(
            provider_name,
            model=opts.model or os.environ.get("NANOCODE_MODEL") or s.get("model", ""),
            base_url=opts.base_url or os.environ.get("NANOCODE_BASE_URL") or s.get("base_url", ""),
            api_key=opts.api_key,
            protocol=opts.protocol or s.get("protocol", ""),
            delay=opts.mock_delay,
            extra_headers=s.get("headers"),
        )
        self.provider_name = provider_name
        self.bus = EventBus()
        self.session_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.data_dir = project_data_dir(self.cwd)
        if self.policy.get("audit_log"):
            self._audit = AuditWriter(self.policy["audit_log"], session=lambda: self.session_id)
            self.bus.subscribe(self._audit)
        if opts.trace:
            os.makedirs(os.path.join(self.data_dir, "traces"), exist_ok=True)
            self.trace_path = os.path.join(self.data_dir, "traces", f"{self.session_id}.jsonl")
            self._trace = TraceWriter(self.trace_path)
            self.bus.subscribe(self._trace)

        tools = default_tools()
        self.mcp_clients, mcp_tools = connect_servers(s.get("mcpServers", {}), self.cwd, log=log)
        self.registry = ToolRegistry(tools + mcp_tools)

        perms = s.get("permissions", {})
        mode = opts.mode or s.get("mode", "default")
        allowed_modes = self.policy.get("allowed_modes") or ()
        if allowed_modes and mode not in allowed_modes:
            log(f"权限模式 {mode} 不在平台允许的范围内，改用 {allowed_modes[0]}")
            mode = allowed_modes[0]
        self.permissions = PermissionManager(self.cwd, mode=mode,
                                             allow=list(perms.get("allow", [])), deny=list(perms.get("deny", [])),
                                             allowed_modes=tuple(allowed_modes))
        self.hooks = HookRunner(s.get("hooks"), self.cwd)
        try:
            verify_cfg = verify_mod.parse_config(s.get("verify"), opts.verify)
        except ValueError as e:   # 写错了要明说，不能悄悄当作没配置
            log(f"verify 配置无效，自动验证未启用：{e}")
            verify_cfg = None
        self.skills = discover_skills(self.cwd)
        self.commands = discover_commands(self.cwd)
        self.agent = Agent(self.provider, self.registry, self.permissions, self.hooks, self.bus, self.cwd,
                           system_prompt=lambda: build_system_prompt(self.cwd, self.skills, model=self.provider.model,
                                                                     provider=self.provider_name, protocol=self.provider.name),
                           max_tokens=int(s.get("max_tokens", 8192)),
                           context_window=int(s.get("context_window", 128000)), skills=self.skills,
                           verify=verify_cfg)
        self.agent.state["rag_config"] = s.get("rag", {})
        # 经验沉淀：自动验证打回又修好的回合会产生候选，等用户用 /memory accept 确认（见 lessons.py）
        self.suggest_memory = bool(s.get("suggest_memory", True))
        self.pending_memory: list[dict] = []
        self._declined_memory: set[str] = set()
        self.bus.subscribe(self._on_lesson)
        self.bus.subscribe(self._autosave)

    def _autosave(self, e: dict) -> None:
        """主 Agent 每一步调用模型前保存一次会话。

        只在整轮结束时保存的话，一个 30 步的任务做到第 29 步时进程被杀（沙箱回收、节点故障），整轮全丢。
        选在"调用模型前"保存，是因为这时上一步的工具结果已经写进消息，对话是完整的；
        如果在工具执行到一半时保存，会存下一个没有结果的 tool_use，恢复后模型接口会拒绝。
        """
        if e["type"] == "llm_request" and e.get("agent") == "main":
            self.save()

    # ------------------------------------------------------------------ 运行

    def send(self, prompt: str) -> str:
        result = self.agent.run(prompt)
        self.save()
        return result

    def info(self) -> dict:
        a = self.agent
        system = a.system_prompt()
        return {
            "version": __version__, "provider": self.provider_name, "model": self.provider.model,
            "cwd": self.cwd, "mode": self.permissions.mode, "busy": a.busy,
            "usage": a.usage.to_dict(), "todos": a.state.get("todos", []),
            "context_tokens": context_tokens(system, a.messages, self.registry.schemas()),
            "context_window": a.context_window,
            "tools": [{"name": t.name, "read_only": t.is_read_only} for t in self.registry.all()],
            "skills": list(self.skills), "commands": list(self.commands),
        }

    # ------------------------------------------------------------------ 保存 / 恢复

    def save(self) -> None:
        """保存会话。先写临时文件、落盘，再用 os.replace 原子替换。

        直接覆盖写的话，写到一半崩溃 / 断电 / 磁盘满，旧的好文件已经被截断，整个会话就丢了。
        这样做即使中途失败，旧文件也原封不动。失败不会让这一轮崩溃，但会通过事件报告出来，
        不再静默吞掉（否则用户以为保存成功，实际什么都没存）。
        """
        d = os.path.join(self.data_dir, "sessions")
        path = os.path.join(d, f"{self.session_id}.json")
        tmp = path + ".tmp"
        try:
            os.makedirs(d, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"messages": self.agent.messages, "todos": self.agent.state.get("todos", []),
                           "usage": self.agent.usage.to_dict()}, f, ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except (OSError, TypeError, ValueError) as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            self.bus.emit("error", agent="main", message=f"会话保存失败（上一次保存的内容不受影响）：{type(e).__name__}: {e}")

    def list_sessions(self) -> list[dict]:
        """列出本项目的所有已保存会话：id、消息数、最后修改时间、第一条用户消息做标题。"""
        d = os.path.join(self.data_dir, "sessions")
        out = []
        if not os.path.isdir(d):
            return out
        for name in sorted((f for f in os.listdir(d) if f.endswith(".json")),
                           key=lambda f: os.path.getmtime(os.path.join(d, f)), reverse=True):
            path = os.path.join(d, name)
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                messages = data.get("messages", []) if isinstance(data, dict) else []
            except (OSError, ValueError):
                continue
            title = ""
            for m in messages:
                if m.get("role") == "user":
                    c = m.get("content")
                    title = (c if isinstance(c, str) else str(c))[:30]
                    break
            out.append({"id": name[:-5], "messages": len(messages),
                        "mtime": os.path.getmtime(path), "title": title or "（空会话）"})
        return out

    def resume(self, session_id: str) -> str:
        """切换到指定会话：先保存当前，再载入目标。"""
        self.save()
        d = os.path.join(self.data_dir, "sessions")
        path = os.path.join(d, f"{session_id}.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("messages", []), list):
            raise ValueError("会话文件格式不对")
        self.agent.messages = data.get("messages", [])
        self.agent.state["todos"] = data.get("todos", [])
        u = data.get("usage", {}) or {}
        self.agent.usage.input_tokens = u.get("input_tokens", 0)
        self.agent.usage.output_tokens = u.get("output_tokens", 0)
        self.agent.usage.estimated = u.get("estimated", False)
        self.session_id = session_id
        # 切会话时清空事件历史，前端重连后只重放新会话的内容
        self.bus.history.clear()
        return f"已切换到会话 {session_id}（{len(self.agent.messages)} 条消息）"

    def new_session(self) -> str:
        """开新会话：先保存当前，再清空。"""
        self.save()
        sid = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        d = os.path.join(self.data_dir, "sessions")
        # 极小概率撞上已有的 id（比如 1 秒内开了两次），往后挪直到不重
        while os.path.exists(os.path.join(d, f"{sid}.json")):
            sid += "_1"
        self.session_id = sid
        self.agent.messages = []
        self.agent.state["todos"] = []
        self.bus.history.clear()
        return f"已开始新会话 {self.session_id}"

    def resume_latest(self) -> str:
        """恢复最近一次会话。读到损坏的文件就改名为 .corrupt 备份、给出提示，再试更早的一个。"""
        d = os.path.join(self.data_dir, "sessions")
        files = sorted(f for f in os.listdir(d) if f.endswith(".json")) if os.path.isdir(d) else []
        notes = []
        for name in reversed(files):
            path = os.path.join(d, name)
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict) or not isinstance(data.get("messages", []), list):
                    raise ValueError("格式不对")
            except (OSError, ValueError) as e:   # JSONDecodeError、UnicodeDecodeError 都是 ValueError
                try:
                    os.replace(path, path + ".corrupt")
                    notes.append(f"会话文件 {name} 已损坏（{type(e).__name__}），已改名为 {name}.corrupt 备份")
                except OSError:
                    notes.append(f"会话文件 {name} 无法读取（{type(e).__name__}），已跳过")
                continue
            self.agent.messages = data.get("messages", [])
            self.agent.state["todos"] = data.get("todos", [])
            self.session_id = name[:-5]
            msg = f"已恢复会话 {self.session_id}（{len(self.agent.messages)} 条消息）"
            return "\n".join(notes + [msg])
        return "\n".join(notes + ["没有可恢复的会话"])

    def close(self) -> None:
        for c in self.mcp_clients:
            c.close()
        if getattr(self, "_trace", None):
            self._trace.close()
        if getattr(self, "_audit", None):
            self._audit.close()

    # ------------------------------------------------------------------ 斜杠命令

    def handle_command(self, line: str) -> tuple[str, str | None]:
        """处理 / 开头的输入。返回 (要显示的文字, 要发给模型的提示词或 None)。"""
        name, _, arg = line[1:].strip().partition(" ")
        arg = arg.strip()
        a = self.agent
        if name in ("help", "?"):
            lines = ["内置命令：",
                     "  /help              显示帮助",
                     "  /clear             清空对话（开始新任务）",
                     "  /compact [重点]    手动压缩上下文",
                     "  /context           查看上下文占用",
                     "  /cost              查看 token 用量",
                     "  /mode [模式]       查看/切换权限模式：" + " | ".join(MODES),
                     "  /tools             列出可用工具",
                     "  /todos             查看待办清单",
                     "  /memory [accept|reject [编号|all]]  查看已加载的 NANOCODE.md 和待确认的经验候选；确认 / 丢弃候选",
                     "  /init              让 Agent 分析项目并生成 NANOCODE.md",
                     "  /resume            恢复上一次会话",
                     "  /providers         列出内置的厂商预设",
                     "  # 内容             把一句话追加到项目 NANOCODE.md（记忆）"]
            if self.commands:
                lines.append("自定义命令：")
                lines += [f"  /{k:<17} {v['description']}" for k, v in self.commands.items()]
            return "\n".join(lines), None
        if name == "clear":
            a.reset()
            self.session_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            return "对话已清空", None
        if name == "compact":
            summary = a.compact(focus=arg)
            self.save()
            return f"已压缩。摘要：\n{summary}", None
        if name == "context":
            i = self.info()
            pct = i["context_tokens"] / i["context_window"] * 100
            return (f"上下文约 {i['context_tokens']} tokens / {i['context_window']}（{pct:.1f}%），"
                    f"{len(a.messages)} 条消息。超过 {int(a.compact_ratio * 100)}% 会自动压缩。"), None
        if name == "cost":
            u = a.usage
            return f"输入 {u.input_tokens} tokens，输出 {u.output_tokens} tokens{'（估算）' if u.estimated else ''}", None
        if name == "mode":
            if arg:
                try:
                    self.permissions.set_mode(arg)
                except ValueError as e:
                    return str(e), None
            return f"当前权限模式：{self.permissions.mode}", None
        if name == "tools":
            return "\n".join(f"  {t.name}{'  (只读)' if t.is_read_only else ''}" for t in self.registry.all()), None
        if name == "todos":
            todos = a.state.get("todos") or []
            return "\n".join(f"{STATUS_ICON[t['status']]} {t['content']}" for t in todos) or "暂无待办", None
        if name == "memory":
            return self.memory_command(arg), None
        if name == "init":
            return "", ("请分析这个项目（目录结构、技术栈、如何构建/运行/测试、代码风格约定），"
                        f"然后在项目根目录创建 {MEMORY_FILE}，内容要简洁实用，供以后的 AI 助手快速了解这个项目。")
        if name == "resume":
            return self.resume_latest(), None
        if name == "providers":
            return "\n".join(f"  {k:<12} {v['protocol']:<10} {v['base_url']}" for k, v in PRESETS.items()), None
        if name in self.commands:
            prompt = self.commands[name]["template"].replace("$ARGUMENTS", arg)
            return "", prompt
        return f"未知命令 /{name}，输入 /help 查看帮助", None

    def _on_lesson(self, e: dict) -> None:
        if e["type"] != "lesson" or not self.suggest_memory:
            return
        from .context import load_memory
        known = "\n".join(c for _, c in load_memory(self.cwd))
        if e["key"] in known or e["key"] in self._declined_memory or any(p["key"] == e["key"] for p in self.pending_memory):
            return   # 同一条约束已经记过 / 被拒绝过 / 已在待确认列表里
        self.pending_memory.append({k: e[k] for k in ("text", "key", "command", "evidence", "files")})
        self.bus.emit("memory_pending", index=len(self.pending_memory), text=e["text"], evidence=e["evidence"],
                      files=e["files"], command=e["command"])

    def memory_command(self, arg: str) -> str:
        """/memory [accept|reject [编号|all]]：查看已加载的记忆和待确认的经验候选，确认或丢弃候选。"""
        from .context import load_memory
        action, _, which = arg.partition(" ")
        which = which.strip() or ("all" if action in ("accept", "reject") else "")
        if action in ("accept", "reject"):
            if not self.pending_memory:
                return "没有待确认的经验候选"
            if which == "all":
                picked = list(range(len(self.pending_memory)))
            elif which.isdigit() and 1 <= int(which) <= len(self.pending_memory):
                picked = [int(which) - 1]
            else:
                return f"编号无效：{which}。当前有 {len(self.pending_memory)} 条候选，用 /memory 查看"
            chosen = [self.pending_memory[i] for i in picked]
            self.pending_memory = [p for i, p in enumerate(self.pending_memory) if i not in picked]
            if action == "accept":
                for p in chosen:
                    self.add_memory(p["text"])
                return f"已写入 {MEMORY_FILE}：{len(chosen)} 条"
            self._declined_memory.update(p["key"] for p in chosen)
            return f"已丢弃 {len(chosen)} 条（本次会话不再提示同样的约束）"
        if action:
            return "用法：/memory                    查看记忆和待确认的候选\n      /memory accept [编号|all]  把候选写入记忆\n      /memory reject [编号|all]  丢弃候选"
        mems = load_memory(self.cwd)
        out = "\n\n".join(f"── {p}\n{c}" for p, c in mems) or f"还没有 {MEMORY_FILE}。用 /init 生成，或用 # 追加一条。"
        if self.pending_memory:
            out += "\n\n── 待确认的经验候选（来自被自动验证打回又修好的回合；确认后才会写入）\n" + "\n".join(
                f"  {i}. {p['text']}" for i, p in enumerate(self.pending_memory, 1)) + "\n  → /memory accept [编号|all] 写入，/memory reject [编号|all] 丢弃"
        return out

    def add_memory(self, text: str) -> str:
        path = os.path.join(self.cwd, MEMORY_FILE)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n- {text.strip()}\n")
        return f"已记录到 {path}"
