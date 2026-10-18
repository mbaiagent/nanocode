"""终审模式（--mode review）用的风险分类：哪些命令可以免确认，哪些无论如何都要确认。

设计原则是"失败安全"：
  - 自动放行只给一份很窄的白名单 —— 跑测试 / lint / 只读的 git 和文件查看，而且命令里不能有任何 shell 拼接符
    （; & | ` $ < > ( ) { } 反斜杠 换行），也不能引用项目外的路径。认不出来的命令一律当成"需要确认"，
    不是"放行"。绕过白名单的写法（比如把命令藏进 python -c）只会落到"需要确认"这一档，不会被放行。
  - 高风险命令（删除、推送、改权限、提权、杀进程……）和高风险路径（项目外、配置和密钥文件）单独识别：
    终审模式下它们**无论 allow 规则怎么写**都要确认，也不能点"总是允许"。这是为了避免
    allow 里一条 bash(*) 就把删除也一起放开。
  - 这是启发式规则，不是沙箱。已知局限见 README：测试命令会执行项目里的代码，包括 Agent 刚写的；
    真正需要隔离时请用容器 / 虚拟机。
"""

from __future__ import annotations

import fnmatch
import ntpath
import os
import re
import shlex

# ---------------------------------------------------------------- 敏感路径

PROTECTED = (".git/*", ".nanocode/*", ".github/*", ".ssh/*", ".env", ".env.*", "*.pem", "*.key", "*id_rsa*",
             ".npmrc", ".pypirc", ".netrc")


def is_protected_path(rel: str) -> bool:
    """配置、密钥、版本库内部：改动它们属于高风险。rel 是相对项目根的路径。

    Windows 上大小写不敏感：磁盘上的 `.ENV` 就是 `.env`，大小写敏感匹配等于放过它（#43）。
    """
    rel = rel.replace(os.sep, "/")
    if rel.startswith("./"):
        rel = rel[2:]
    parts = rel.split("/")
    for i in range(len(parts)):
        sub = "/".join(parts[i:])
        if os.name == "nt":
            sub = sub.lower()
        if any(fnmatch.fnmatchcase(sub, p) for p in PROTECTED):
            return True
    return False


# ---------------------------------------------------------------- bash 命令

_RISKY = [
    (r"(^|[\s;&|(`])(rm|rmdir|unlink|shred|dd|mkfs\S*|truncate)\b", "删除或覆盖文件"),
    (r"(^|[\s;&|(`])(sudo|su|doas)\b", "提权"),
    (r"(^|[\s;&|(`])(chmod|chown|chgrp|chattr)\b", "改权限或属主"),
    (r"(^|[\s;&|(`])(kill|killall|pkill)\b", "结束进程"),
    (r"\bgit\s+(\S+\s+)*?(push|reset\s+--hard|clean|rebase|filter-branch|update-ref|gc\s+--prune)\b", "不可轻易撤销的 git 操作"),
    (r"\bgit\s+(\S+\s+)*?(checkout|restore)\s+(\S+\s+)*?--(\s|$)", "丢弃工作区改动"),
    (r"\bgit\s+(\S+\s+)*?branch\s+(\S+\s+)*?-D\b", "强制删除分支"),
    (r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(sh|bash|zsh|python3?)\b", "下载后直接执行"),
    (r"(>|>>)\s*/(etc|usr|bin|sbin|boot|dev|var|root)\b", "写系统目录"),
    (r"(^|\s)~/\.|\s/etc/|\s/root\b", "访问用户配置或系统目录"),
    (r"(^|[\s/])\.(git|nanocode|ssh)/|(^|[\s/])\.env\b", "访问版本库内部、配置或密钥"),
    (r"\b(pip3?|npm|yarn|pnpm|gem|cargo)\s+(install|add|uninstall|remove|publish)\b", "安装 / 卸载 / 发布依赖"),
]
_RISKY = [(re.compile(p), why) for p, why in _RISKY]

_META = re.compile(r"[;&|`$<>(){}\\\n\r]")

# 程序 -> 允许的子命令（None = 不限制，只看参数）
_TEST_RUNNERS = {
    "pytest": None, "py.test": None, "mypy": None, "flake8": None, "pylint": None, "tsc": None,
    "ruff": {"check"}, "cargo": {"test", "check", "clippy"}, "go": {"test", "vet"},
    "make": {"test", "lint", "check"},
}
_PY_MODULES = {"pytest", "unittest", "mypy", "flake8", "pylint", "pyflakes"}
_READERS = {"ls", "pwd", "cat", "head", "tail", "wc", "grep", "rg", "which", "date", "echo", "true"}
_GIT_READ = {"status", "diff", "log", "show"}
_BAD_FLAGS = ("--output", "--ext-diff", "--textconv", "--exec", "--fix", "--write", "--unsafe-fixes")


def _is_absolute(p: str) -> bool:
    """绝对路径（POSIX / Windows 盘符 / UNC）或家目录。

    分类跑在任意平台上，但命令最终是在用户的机器上执行的：Windows 上
    `cat C:/Users/me/.aws/credentials` 完全合法，只认 `/` 开头就等于放行（#43）。
    ntpath 在 POSIX 上也认得盘符和 UNC，所以不需要按平台分支。
    """
    return p.startswith(("/", "~", "\\")) or ntpath.isabs(p) or bool(ntpath.splitdrive(p)[0])


def _arg_is_unsafe(arg: str) -> bool:
    """参数里出现项目外路径、上级目录、家目录、敏感文件，都不能自动放行。"""
    # 按 = 拆开：`--rootdir=C:/Windows` 整体不以 / 或盘符开头，只看整个参数会漏掉（#43）
    if any(_is_absolute(p) for p in arg.split("=")) or ".." in re.split(r"[/=\\]", arg):
        return True        # 绝对路径、家目录、上级目录（注意 HEAD~1 和 ./... 是正常写法）
    if any(arg == f or arg.startswith(f + "=") for f in _BAD_FLAGS):
        return True
    return not arg.startswith("-") and is_protected_path(arg)


def classify_bash(command: str) -> str:
    """返回 "risky"（高风险，必须确认）/ "safe"（白名单内，可免确认）/ "unknown"（认不出，需要确认）。"""
    command = command.strip()
    if not command:
        return "unknown"
    for pattern, _why in _RISKY:
        if pattern.search(command):
            return "risky"
    if _META.search(command):
        return "unknown"
    try:
        tokens = shlex.split(command)
    except ValueError:
        return "unknown"
    if not tokens or any(_arg_is_unsafe(t) for t in tokens[1:]):
        return "unknown"
    prog, args = tokens[0], tokens[1:]
    if prog in ("python", "python3"):
        ok = len(args) >= 2 and args[0] == "-m" and args[1] in _PY_MODULES
        return "safe" if ok else "unknown"
    if prog == "git":
        ok = bool(args) and args[0] in _GIT_READ
        return "safe" if ok else "unknown"
    if prog == "npm":
        ok = args[:1] == ["test"] or (args[:1] == ["run"] and args[1:2] in (["test"], ["lint"]))
        return "safe" if ok else "unknown"
    if prog in _TEST_RUNNERS:
        allowed = _TEST_RUNNERS[prog]
        if allowed is None:
            return "safe"
        return "safe" if args and args[0] in allowed else "unknown"
    if prog in _READERS:
        return "safe"
    return "unknown"


def risk_reason(command: str) -> str:
    for pattern, why in _RISKY:
        if pattern.search(command):
            return why
    return ""
