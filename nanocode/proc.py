"""跨平台子进程辅助：起进程、杀进程树、找 bash。

Windows 上 `os.killpg` / `signal.SIGKILL` 根本不存在（AttributeError），
`start_new_session=True` 会被静默忽略 —— 直接照 POSIX 写会 ①抛异常打断主循环
②超时后只杀掉 shell 自己，把 `pytest` / dev server 这些孙进程留在后面继续跑（#45）。
平台差异收在这个模块里，别处不要再写 `os.name == "nt"`。
"""

from __future__ import annotations

import ntpath
import os
import shutil
import subprocess
import sys

WINDOWS = os.name == "nt"


def force_utf8_stdio() -> None:
    """把本进程的 stdout / stderr 切成 UTF-8。

    Windows 上输出被重定向（管道、文件、CI 日志）时默认用本机代码页 cp1252，
    打印中文或 `⚠` / `✗` 这类符号会直接 UnicodeEncodeError 把整个进程打断 ——
    CLI、评测脚本都踩得到。`errors="replace"` 是最后一道保险：真遇到编不出的字符
    也只掉一个字符，不至于让一轮跑完的评测在最后打印时崩掉。

    stdout 被替换成 StringIO 之类（测试里很常见）时没有 reconfigure，忽略即可。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def utf8_env(base: dict | None = None) -> dict:
    """给子进程的环境变量：让 Python 子进程按 UTF-8 读写 stdio。

    nanocode 一律按 UTF-8 解码子进程输出（`encoding="utf-8"`），但 Windows 上
    Python 子进程的 stdout 默认还是本机代码页：子进程只要打印中文（mock 生成的
    示例脚本、hook 脚本、verify 命令、MCP 服务器、评测的 check.py）就会
    UnicodeEncodeError 崩掉，看起来就像"命令没输出 / 检查没通过"。

    只影响 Python 写的子进程，别的程序无感；显式设了 PYTHONIOENCODING 的以用户为准。
    """
    env = dict(os.environ if base is None else base)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def popen_kwargs() -> dict:
    """Popen 的平台无关参数。

    POSIX 起一个新会话：沙箱独占一个进程组，超时时能把整棵进程树一起带走。
    Windows 没有进程组的概念，给了也是被忽略（还会让人误以为杀得到子树）。
    """
    return {} if WINDOWS else {"start_new_session": True}


def kill_tree(proc: subprocess.Popen) -> None:
    """让 proc 连同它启动的子进程一起结束。

    正常情况发 SIGTERM 让它自己收尾（沙箱要停沙箱里的子进程、刷日志；直接 SIGKILL
    会留下半个工作区），5 秒还没退出才升级成 SIGKILL。
    Windows 用 `taskkill /T /F` 杀整棵树：`terminate()` 只杀直接子进程，孙进程照样留下。
    """
    if proc.poll() is not None:
        return
    if WINDOWS:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], timeout=15,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            proc.wait(timeout=5)
            return
        except (OSError, subprocess.SubprocessError):
            pass                      # taskkill 不在 PATH 上（精简版 Windows / 沙箱里）：退回下面
        try:
            proc.kill()
        except OSError:
            pass
        return
    import signal
    for sig, grace in ((signal.SIGTERM, 5), (signal.SIGKILL, None)):
        try:
            os.killpg(os.getpgid(proc.pid), sig)
            proc.wait(timeout=grace) if grace else proc.wait()
            return
        except (ProcessLookupError, PermissionError, AttributeError):
            break                     # 进程组已经没了：下面直接杀它自己
        except subprocess.TimeoutExpired:
            continue                  # 还活着：升级信号
    try:
        proc.kill()
    except OSError:
        pass


def _is_wsl_stub(exe: str) -> bool:
    """`C:\\Windows\\System32\\bash.exe` 是 WSL 启动器，不是我们要的 bash。

    它的命令跑在 WSL 视图里：`pwd`/`cwd` 语义和 Windows 不一致（工具记工作目录靠 `pwd`），
    输出还是 UTF-16LE，按 cp936/utf-8 解码出来是 `w\\x00s\\x00l\\x00...` 这样的乱码（#45）。
    """
    # 用 ntpath 而不是 os.path：判断的永远是 Windows 路径，而这段代码本身可能在
    # POSIX 上被单元测试跑到（os.path 在 POSIX 上会把 C:\... 当成相对路径）。
    if ntpath.normcase(ntpath.basename(exe)) != "bash.exe":
        return False                            # 只有 bash.exe 有 WSL 启动器这个化身
    sysroot = os.environ.get("SystemRoot", r"C:\Windows")
    prefix = ntpath.join(sysroot, "system32") + ntpath.sep
    return ntpath.normcase(exe).startswith(ntpath.normcase(prefix))


def find_bash() -> str | None:
    """返回一个真 bash 的路径；没有就返回 None（调用方要给出明确错误，不要静默将就）。

    Windows 上先找 Git for Windows（开发者机器上最常见），再退回 PATH —— 但要跳过
    System32 下的 WSL stub，它会在最意想不到的时候把命令送进另一个文件系统视图。
    """
    if not WINDOWS:
        return shutil.which("bash")
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        os.path.join(pf, "Git", "bin", "bash.exe"),
        os.path.join(pf, "Git", "usr", "bin", "bash.exe"),
        os.path.join(pf86, "Git", "bin", "bash.exe"),
        os.path.join(local, "Programs", "Git", "bin", "bash.exe") if local else "",
        shutil.which("bash") or "",
    ]
    for cand in candidates:
        if cand and os.path.isfile(cand) and not _is_wsl_stub(cand):
            return cand
    return None
