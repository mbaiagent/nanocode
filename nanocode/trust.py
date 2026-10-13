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

不敏感、照常生效的：permissions.deny（只会更安全）、context_window、max_tokens、rag.include 等。
NANOCODE.md、skills、commands 只是文字，会进入提示词，但不会自己执行
（它们可能包含提示词注入，这由权限系统兜底）。
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
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


def describe(sensitive: dict) -> list[str]:
    """给人看的说明：这份配置到底要做什么。"""
    lines = []
    for event, hooks in (sensitive.get("hooks") or {}).items():
        for h in hooks:
            lines.append(f"hook {event}{'（' + h['matcher'] + '）' if h.get('matcher') else ''} 会执行：{h.get('command')}")
    for name, spec in (sensitive.get("mcpServers") or {}).items():
        lines.append(f"MCP 服务器 {name} 会执行：{' '.join([spec.get('command', '')] + list(spec.get('args', [])))}")
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
    return lines


def fingerprint(cwd: str, sensitive: dict) -> str:
    raw = os.path.realpath(cwd) + "\n" + json.dumps(sensitive, sort_keys=True, ensure_ascii=False)
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
    if trust_fn and trust_fn(cwd, describe(sensitive)):
        store.trust(cwd, fp)
        return project_cfg, ""
    return safe, ("项目配置里有需要信任才能生效的设置，已忽略：\n  " + "\n  ".join(describe(sensitive)) +
                  "\n确认安全后可用 --trust 启动（或在交互模式下按提示同意）。")


__all__ = ["split_sensitive", "restore", "describe", "fingerprint", "TrustStore", "apply_trust", "TrustFn"]
