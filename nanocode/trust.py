"""工作区信任：打开一个陌生项目，不能让它的配置文件替你做决定。

项目目录里的 .nanocode/settings.json 是"项目作者"写的，不是你写的。
你从网上 clone 一个仓库，然后在里面启动 nanocode —— 如果它的配置能：

  - hooks / mcpServers / verify → 在你电脑上执行任意命令（启动时、第一次提问时或任务结束时就执行）
  - permissions.allow     → 给危险操作开绿灯，例如 bash(*)
  - mode                  → 直接切到 bypass，所有操作免确认
  - provider / base_url / protocol / headers / rag.embedding
                          → 把请求（连同你环境变量里的 API Key）发到攻击者的服务器

那么"打开项目"本身就成了攻击。所以这些字段属于"敏感配置"：
第一次遇到时必须问你，你同意后记下"这个目录 + 这份配置"的指纹；
配置内容一旦变化（指纹不同），就要重新询问。

指纹不只覆盖配置文本：配置里引用的项目内脚本（hook / MCP / verify 指向的
.py、.sh 等）的内容也一起哈希（#40）—— 否则 git pull、切分支之后脚本被换成
别的内容，配置文本没变，下次启动照样静默执行。

不敏感、照常生效的：permissions.deny（只会更安全）、context_window、max_tokens、rag.include 等。
NANOCODE.md、skills、commands 只是文字，会进入提示词，但不会自己执行
（它们可能包含提示词注入，这由权限系统兜底）。
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shlex
from typing import Callable, Optional

TOP_LEVEL = ("hooks", "mcpServers", "mode", "provider", "base_url", "protocol", "headers", "verify")
TrustFn = Callable[[str, list[str]], bool]   # (项目目录, 敏感项说明) -> 是否信任


def split_sensitive(cfg: dict) -> tuple[dict, dict]:
    """把项目配置拆成 (安全部分, 敏感部分)。"""
    safe = copy.deepcopy(cfg)
    sensitive: dict = {}
    for key in TOP_LEVEL:
        if key in safe:
            sensitive[key] = safe.pop(key)
    allow = (safe.get("permissions") or {}).pop("allow", None)
    if allow:
        sensitive["permissions.allow"] = allow
    emb = (safe.get("rag") or {}).pop("embedding", None)
    if emb:
        sensitive["rag.embedding"] = emb
    return safe, sensitive


def restore(safe: dict, sensitive: dict) -> dict:
    cfg = copy.deepcopy(safe)
    for key, value in sensitive.items():
        if key == "permissions.allow":
            cfg.setdefault("permissions", {})["allow"] = value
        elif key == "rag.embedding":
            cfg.setdefault("rag", {})["embedding"] = value
        else:
            cfg[key] = value
    return cfg


def describe(sensitive: dict, cwd: str = "") -> list[str]:
    """给人看的说明：这份配置到底要做什么。传入 cwd 时附带项目内脚本的内容哈希（#40）。"""
    lines = []
    for event, hooks in (sensitive.get("hooks") or {}).items():
        for h in hooks:
            lines.append(f"hook {event}{'（' + h['matcher'] + '）' if h.get('matcher') else ''} 会执行：{h.get('command')}")
    for name, spec in (sensitive.get("mcpServers") or {}).items():
        lines.append(f"MCP 服务器 {name} 会执行：{' '.join([spec.get('command', '')] + list(spec.get('args', [])))}")
        # 子进程默认还会拿到 PATH / HOME 等基础变量；env 里显式写了什么也要说清楚，
        # 它足以覆盖 PATH、PYTHONPATH 把服务器换成别的程序（#41）
        if spec.get("env"):
            lines.append(f"  并覆盖环境变量：{', '.join(spec['env'])}")
        lines.append("  （子进程默认继承 PATH / HOME 等基础变量，不继承各家 API Key）")
    if "verify" in sensitive:
        v = sensitive["verify"]
        lines.append(f"Agent 结束前会自动执行检查命令：{v.get('command') if isinstance(v, dict) else v}")
    if "permissions.allow" in sensitive:
        lines.append(f"免确认规则：{', '.join(sensitive['permissions.allow'])}")
    if "mode" in sensitive:
        lines.append(f"权限模式改为：{sensitive['mode']}")
    for key in ("provider", "base_url", "protocol"):
        if key in sensitive:
            lines.append(f"模型配置 {key} = {sensitive[key]}（你的 API Key 会发往这里）")
    if "headers" in sensitive:
        lines.append(f"给模型请求附加请求头：{', '.join(sensitive['headers'])}")
    if "rag.embedding" in sensitive:
        lines.append(f"向量检索服务：{json.dumps(sensitive['rag.embedding'], ensure_ascii=False)}")
    if cwd:
        # 脚本内容也参与信任（#40）：只显示命令行的话，用户没处发现"配置没变、
        # 但脚本被换成了别的内容"。哈希前 8 位给人一个肉眼可比的线索。
        for tok, digest in _script_digests(cwd, sensitive):
            lines.append(f"  项目内脚本 {tok}（内容 sha256:{digest[:8]}）—— 脚本内容变化同样需要重新确认")
    return lines


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        return "!"          # 读不了（权限、被占用……）：固定标记。状态一变（变能读了、
                            # 内容变了）指纹就变，不会把"读不了"当成稳定状态放行
    return h.hexdigest()


def _command_tokens(sensitive: dict) -> list[str]:
    """敏感配置里所有命令行的 token：hook command、MCP command+args、verify command。

    shell 命令不是结构化数据，这里只做粗浅的切分，而且宁多勿少：切错的 token
    解析不成项目内文件，自然被丢掉；漏掉一个能解析的才会漏放。POSIX 和 Windows
    两种切法都试（后者不吞反斜杠），引号不配对就退化成空白切分。
    """
    commands: list[str] = []
    for hooks in (sensitive.get("hooks") or {}).values():
        for h in hooks:
            if isinstance(h, dict) and isinstance(h.get("command"), str):
                commands.append(h["command"])
    for spec in (sensitive.get("mcpServers") or {}).values():
        if not isinstance(spec, dict):
            continue
        if isinstance(spec.get("command"), str):
            commands.append(spec["command"])
        for a in spec.get("args") or []:
            if isinstance(a, str):
                commands.append(a)
    if "verify" in sensitive:
        v = sensitive["verify"]
        if isinstance(v, dict) and isinstance(v.get("command"), str):
            commands.append(v["command"])
        elif isinstance(v, str):
            commands.append(v)
    tokens: list[str] = []
    for cmd in commands:
        if not cmd:
            continue
        for posix in (True, False):
            try:
                parts = shlex.split(cmd, posix=posix)
            except ValueError:
                parts = cmd.split()
            tokens += [p.strip("\"'") for p in parts if p.strip("\"'")]
    return tokens


def _script_digests(cwd: str, sensitive: dict) -> list[tuple[str, str]]:
    """敏感配置引用的项目内脚本：[(命令里的写法, 内容 sha256)]。

    为什么脚本内容要进指纹：配置文本没变、脚本内容变了（git pull、切分支、
    Agent 自己在 accept_edits 下改写 .nanocode/hooks/guard.py），信任却还记着
    "这份配置我见过" —— 于是新内容静默执行，不再询问（#40）。
    按"写法"判断是否在项目内（不跟软链）：项目里的脚本哪怕是指向家目录的软链，
    内容也要进指纹；/usr/bin/env、python3 这类项目外的不跟。启发式解析防不全
    `bash -c "python3 x.py"` 这种嵌套，但 hook / MCP / verify 直接指向项目内
    脚本的主要路径都覆盖得到。
    """
    root = os.path.realpath(cwd)
    out: dict[str, str] = {}
    for tok in _command_tokens(sensitive):
        if not tok or tok.startswith("-"):
            continue
        lex = os.path.normpath(os.path.join(root, os.path.expanduser(tok)))
        if not (lex == root or lex.startswith(root + os.sep)):
            continue                      # 项目外的可执行文件（python3、/usr/bin/env）不跟
        real = os.path.realpath(lex)
        if not os.path.isfile(real):
            continue                      # 目录名、不存在的路径、纯命令名
        out[tok] = _sha256_file(real)
    return sorted(out.items())


def fingerprint(cwd: str, sensitive: dict) -> str:
    raw = os.path.realpath(cwd) + "\n" + json.dumps(sensitive, sort_keys=True, ensure_ascii=False)
    # 引用的项目内脚本内容也进指纹（#40）：用户信任的是"这份配置 + 这些脚本"，
    # 只哈希配置文本的话，脚本被换掉内容不会被发现，下次启动直接执行、不再询问。
    for tok, digest in _script_digests(cwd, sensitive):
        raw += f"\n{tok}\n{digest}"
    return hashlib.sha256(raw.encode()).hexdigest()


class TrustStore:
    def __init__(self, path: str):
        self.path = path

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def is_trusted(self, cwd: str, fp: str) -> bool:
        return self._load().get(os.path.realpath(cwd)) == fp

    def trust(self, cwd: str, fp: str) -> None:
        data = self._load()
        data[os.path.realpath(cwd)] = fp
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)


def apply_trust(cwd: str, project_cfg: dict, store: TrustStore, trust_fn: Optional[TrustFn]) -> tuple[dict, str]:
    """返回 (实际生效的项目配置, 给用户的提示)。"""
    safe, sensitive = split_sensitive(project_cfg)
    if not sensitive:
        return project_cfg, ""
    fp = fingerprint(cwd, sensitive)
    if store.is_trusted(cwd, fp):
        return project_cfg, ""
    if trust_fn and trust_fn(cwd, describe(sensitive, cwd)):
        store.trust(cwd, fp)
        return project_cfg, ""
    return safe, ("项目配置里有需要信任才能生效的设置，已忽略：\n  " + "\n  ".join(describe(sensitive, cwd)) +
                  "\n确认安全后可用 --trust 启动（或在交互模式下按提示同意）。")


__all__ = ["split_sensitive", "restore", "describe", "fingerprint", "TrustStore", "apply_trust", "TrustFn"]
