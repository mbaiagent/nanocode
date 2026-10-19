"""内置自动验证：Agent 想结束时，先跑一遍你配置的检查命令，不通过就把输出交还给模型继续修。

这是"产出 → 检查 → 纠正 → 重复"里的"检查"：一个模型自己说了不算、能让工作失败的检查。
和 Stop hook 的区别：Stop hook 要你自己写脚本，也没有次数上限；这里有现成的重试上限，
并且同一个错误连续出现两次（说明模型没有进展）就停下来，把失败信息交给你。

配置（.nanocode/settings.json，或命令行 --verify "pytest -q"）：
    "verify": "pytest -q"
    "verify": {"command": "pytest -q", "max_retries": 3, "timeout": 300}

什么时候检查：本轮用过会改动东西的工具（写文件、bash 等非只读工具）之后，模型准备结束时。
纯问答、只读调查不会触发。检查命令来自项目配置时，和 hooks 一样需要先信任这个项目（见 trust.py）。
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass

from .proc import kill_tree, popen_kwargs

DEFAULT_RETRIES = 3
DEFAULT_TIMEOUT = 300.0
FEEDBACK_LIMIT = 4000   # 喂回模型的检查输出最多多少字符（保留末尾，测试报错通常在最后）


@dataclass
class VerifyConfig:
    command: str
    max_retries: int = DEFAULT_RETRIES
    timeout: float = DEFAULT_TIMEOUT


def parse_config(raw, override_command: str = "") -> VerifyConfig | None:
    """把 settings 里的 verify（字符串或字典）整理成 VerifyConfig；没配置返回 None。

    配置写错会抛 ValueError，由调用方告诉用户 —— 不要悄悄当作"没配置"，否则用户以为有检查其实没有。
    """
    if raw in (None, "", {}) and not override_command:
        return None
    if isinstance(raw, str) or raw in (None, "", {}):
        raw = {"command": raw or ""}
    if not isinstance(raw, dict):
        raise ValueError("verify 必须是字符串（命令）或对象")
    command = (override_command or raw.get("command") or "")
    if not isinstance(command, str) or not command.strip():
        raise ValueError("verify.command 不能为空")
    retries = raw.get("max_retries", DEFAULT_RETRIES)
    timeout = raw.get("timeout", DEFAULT_TIMEOUT)
    if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
        raise ValueError("verify.max_retries 必须是不小于 0 的整数")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError("verify.timeout 必须是大于 0 的秒数")
    return VerifyConfig(command.strip(), retries, float(timeout))


@dataclass
class VerifyResult:
    passed: bool
    exit_code: int | None   # 超时为 None
    output: str
    seconds: float
    timed_out: bool = False


def run(cfg: VerifyConfig, cwd: str) -> VerifyResult:
    """在工作目录里跑检查命令。超时会连同它启动的子进程一起杀掉。

    超时分支按平台走（proc.kill_tree）：原来这里直接 os.killpg —— Windows 上那是
    AttributeError，会穿出 verify → 主循环，把一次普通的"检查超时"变成崩溃（#45）。
    """
    started = time.time()
    p = subprocess.Popen(cfg.command, shell=True, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, encoding="utf-8", errors="replace", **popen_kwargs())
    try:
        out, _ = p.communicate(timeout=cfg.timeout)
    except subprocess.TimeoutExpired:
        kill_tree(p)
        out, _ = p.communicate()
        return VerifyResult(False, None, (out or "") + f"\n[检查命令超过 {cfg.timeout:g} 秒仍未结束，已终止]",
                            time.time() - started, timed_out=True)
    return VerifyResult(p.returncode == 0, p.returncode, out or "", time.time() - started)


def tail(text: str, limit: int = FEEDBACK_LIMIT) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return f"[前面省略 {len(text) - limit} 个字符]\n{text[-limit:]}"


_DURATION = re.compile(r"\b\d+(?:\.\d+)?\s*(?:ms|s|sec|seconds)\b")


def signature(result: VerifyResult) -> str:
    """失败的"指纹"：去掉耗时之后输出一样，就认为是同一个错误（模型没有取得进展）。"""
    return f"{result.exit_code}\n{_DURATION.sub('', result.output).strip()}"


def feedback(cfg: VerifyConfig, result: VerifyResult, attempt: int) -> str:
    left = cfg.max_retries - attempt + 1
    return (f"自动验证未通过（命令：{cfg.command}，退出码 {result.exit_code if result.exit_code is not None else '超时'}，"
            f"第 {attempt} 次检查，还可以再修 {left} 次）。请根据下面的输出修正，修好后再结束；不要通过改检查本身或删测试来让它通过。\n\n"
            f"{tail(result.output) or '(没有输出)'}")
