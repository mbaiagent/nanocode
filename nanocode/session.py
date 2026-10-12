"""把所有部件组装成一个可用的会话，并处理配置、斜杠命令、会话保存。

CLI 和 Web 界面都只和 Session 打交道。看这个文件可以了解各模块是如何拼起来的：

    配置 → Provider + 工具(内置 + MCP) + 权限 + Hooks + 技能 → Agent
"""

from __future__ import annotations

import datetime
import json
import os
from dataclasses import dataclass, field

from . import __version__
from .agent import Agent
from .context import HOME_DIR, MEMORY_FILE, build_system_prompt, context_tokens, discover_commands, discover_skills
from .events import EventBus, TraceWriter
from .hooks import HookRunner
from .llm import PRESETS, make_provider
from .mcp import connect_servers
from .permissions import MODES, PermissionManager
from .tools import ToolRegistry, default_tools
from .tools.planning import STATUS_ICON


def load_settings(cwd: str) -> dict:
    """依次合并：~/.nanocode/settings.json → 项目 .nanocode/settings.json → .nanocode/settings.local.json。
    越靠后优先级越高；列表（allow/deny）和字典（hooks/mcpServers）会合并而不是覆盖。"""
    merged: dict = {}
    for path in (os.path.join(HOME_DIR, "settings.json"), os.path.join(cwd, ".nanocode", "settings.json"),
                 os.path.join(cwd, ".nanocode", "settings.local.json")):
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                _deep_merge(merged, json.load(f))
    return merged


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


class Session:
    def __init__(self, opts: Options, log=print):
        self.cwd = os.path.abspath(opts.cwd)
        self.settings = load_settings(self.cwd)
        s = self.settings
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
        self.data_dir = os.path.join(self.cwd, ".nanocode")
        if opts.trace:
            os.makedirs(os.path.join(self.data_dir, "traces"), exist_ok=True)
            self.trace_path = os.path.join(self.data_dir, "traces", f"{self.session_id}.jsonl")
            self.bus.subscribe(TraceWriter(self.trace_path))

        tools = default_tools()
        self.mcp_clients, mcp_tools = connect_servers(s.get("mcpServers", {}), self.cwd, log=log)
        self.registry = ToolRegistry(tools + mcp_tools)

        perms = s.get("permissions", {})
        self.permissions = PermissionManager(self.cwd, mode=opts.mode or s.get("mode", "default"),
                                             allow=list(perms.get("allow", [])), deny=list(perms.get("deny", [])))
        self.hooks = HookRunner(s.get("hooks"), self.cwd)
        self.skills = discover_skills(self.cwd)
        self.commands = discover_commands(self.cwd)
        self.agent = Agent(self.provider, self.registry, self.permissions, self.hooks, self.bus, self.cwd,
                           system_prompt=lambda: build_system_prompt(self.cwd, self.skills),
                           max_tokens=int(s.get("max_tokens", 8192)),
                           context_window=int(s.get("context_window", 128000)), skills=self.skills)
        self.agent.state["rag_config"] = s.get("rag", {})

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
        try:
            d = os.path.join(self.data_dir, "sessions")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, f"{self.session_id}.json"), "w", encoding="utf-8") as f:
                json.dump({"messages": self.agent.messages, "todos": self.agent.state.get("todos", []),
                           "usage": self.agent.usage.to_dict()}, f, ensure_ascii=False, indent=1)
        except OSError:
            pass

    def resume_latest(self) -> str:
        d = os.path.join(self.data_dir, "sessions")
        files = sorted(f for f in os.listdir(d) if f.endswith(".json")) if os.path.isdir(d) else []
        if not files:
            return "没有可恢复的会话"
        with open(os.path.join(d, files[-1]), encoding="utf-8") as f:
            data = json.load(f)
        self.agent.messages = data.get("messages", [])
        self.agent.state["todos"] = data.get("todos", [])
        self.session_id = files[-1][:-5]
        return f"已恢复会话 {self.session_id}（{len(self.agent.messages)} 条消息）"

    def close(self) -> None:
        for c in self.mcp_clients:
            c.close()

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
                     "  /memory            查看已加载的 NANOCODE.md",
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
            from .context import load_memory
            mems = load_memory(self.cwd)
            return "\n\n".join(f"── {p}\n{c}" for p, c in mems) or f"还没有 {MEMORY_FILE}。用 /init 生成，或用 # 追加一条。", None
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

    def add_memory(self, text: str) -> str:
        path = os.path.join(self.cwd, MEMORY_FILE)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n- {text.strip()}\n")
        return f"已记录到 {path}"
