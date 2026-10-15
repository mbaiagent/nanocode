"""平台策略：多用户部署时，由平台决定、用户和项目都不能覆盖的配置。

单机使用时没有这一层。部署成多人服务（见 docs/12）时，每个会话跑在一个沙箱里，
沙箱里的配置文件用户和 Agent 都能改：Agent 执行一句
`echo '{"base_url": "https://evil.example"}' > ~/.nanocode/settings.json`，
下次启动就会把请求发到别处。所以平台把策略文件以只读方式挂进沙箱，用环境变量
NANOCODE_POLICY 指向它。策略的优先级高于命令行参数、环境变量、用户配置和项目配置。

策略文件就是一份 settings.json，外加三个只在策略里有意义的键：

    {
      "provider": "custom", "protocol": "openai",
      "base_url": "http://gateway:8080/v1", "model": "deepseek-chat",   ← 锁定：命令行和配置都改不了
      "mode": "review",                                                 ← 启动时的权限模式
      "allowed_modes": ["default", "review", "bypass"],                 ← 只能在这些模式之间切换
      "forbid": ["hooks", "mcpServers", "permissions.allow"],           ← 用户和项目配置里的这些键一律删掉
      "permissions": {"deny": ["bash(curl *)"]},                        ← 和其他配置一样合并（deny 只会更严）
      "audit_log": "/var/log/nanocode/audit.jsonl"                      ← 审计日志写到哪里
    }

策略配置了却读不出来（文件不存在、不是合法 JSON）时直接报错拒绝启动，
而不是悄悄当作没有策略：那样等于把所有限制都关掉了。
"""

from __future__ import annotations

import dataclasses
import json
import os

POLICY_ENV = "NANOCODE_POLICY"
LOCKED_OPTIONS = ("provider", "model", "base_url", "protocol", "mode")   # 也是命令行参数，策略要盖过它们
META_KEYS = ("allowed_modes", "forbid", "audit_log")                    # 只在策略里有意义，不合并进配置


class PolicyError(Exception):
    pass


def load_policy(path: str | None = None) -> dict:
    """读策略文件。没配置（环境变量为空）返回 {}；配置了但读不出来就报错。"""
    path = path if path is not None else os.environ.get(POLICY_ENV, "")
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            policy = json.load(f)
    except (OSError, ValueError) as e:
        raise PolicyError(f"平台策略 {path} 读取失败，拒绝启动：{type(e).__name__}: {e}") from None
    if not isinstance(policy, dict):
        raise PolicyError(f"平台策略 {path} 必须是一个 JSON 对象")
    modes = policy.get("allowed_modes")
    if modes is not None and (not isinstance(modes, list) or not modes):
        raise PolicyError("allowed_modes 必须是非空列表")
    if modes and policy.get("mode") and policy["mode"] not in modes:
        raise PolicyError(f"策略里的 mode={policy['mode']} 不在 allowed_modes 里")
    return policy


def apply_policy(settings: dict, policy: dict, merge) -> dict:
    """先删掉 forbid 列出的键（支持 "permissions.allow" 这种两级写法），再把策略合并进去。

    merge 是 session._deep_merge：标量覆盖、字典递归合并、列表追加去重。
    """
    if not policy:
        return settings
    for key in policy.get("forbid") or []:
        parent, _, child = key.partition(".")
        if child:
            if isinstance(settings.get(parent), dict):
                settings[parent].pop(child, None)
        else:
            settings.pop(parent, None)
    merge(settings, {k: v for k, v in policy.items() if k not in META_KEYS})
    return settings


def lock_options(opts, policy: dict):
    """策略里写了的 provider / model / base_url / protocol / mode，覆盖命令行参数。"""
    locked = {k: policy[k] for k in LOCKED_OPTIONS if policy.get(k)}
    return dataclasses.replace(opts, **locked) if locked else opts


__all__ = ["POLICY_ENV", "PolicyError", "load_policy", "apply_policy", "lock_options"]
