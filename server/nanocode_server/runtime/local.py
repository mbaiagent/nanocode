"""本机子进程：开发和测试用。**没有任何隔离**，Agent 执行的命令直接跑在这台机器上。"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

from .base import Launcher, LaunchError, SandboxSpec, wait_ready
from .snapshot import LocalSnapshots

# 不要漏给沙箱的环境变量：控制面自己的配置、各家模型的 Key
_SECRET_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _terminate(proc: subprocess.Popen) -> None:
    """停止沙箱进程。POSIX 杀整个进程组，Windows 只能 terminate/kill（#75）。"""
    if os.name == "nt":
        # Windows 没有 SIGKILL / killpg：terminate() 是 TerminateProcess，kill() 兜底
        for attempt in (proc.terminate, proc.kill):
            try:
                attempt()
                proc.wait(timeout=5)
                return
            except (OSError, subprocess.TimeoutExpired):
                continue
        return
    # 沙箱是 start_new_session=True 起的：只杀直接子进程会把 Agent 拉起的
    # pytest / dev server 留下来，继续占端口、写已经删除的工作区。
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(proc.pid), sig)
            proc.wait(timeout=5)
            return
        except (ProcessLookupError, PermissionError):
            break
        except subprocess.TimeoutExpired:
            continue
    try:
        proc.kill()
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    """这个 pid 还活着吗。

    POSIX 用 `kill(pid, 0)`：不投递信号，只做存在性检查。
    Windows 上 `os.kill(pid, 0)` **不是**存在性检查 —— signal.CTRL_C_EVENT 的值就是 0，
    它会往目标进程所在的进程组广播 Ctrl+C；沙箱通常和控制面共享同一个 console，
    于是控制面自己收到 KeyboardInterrupt，整轮测试/服务被中断（#92）。
    Windows 改用 OpenProcess + GetExitCodeProcess 查 STILL_ACTIVE，只查不发信号。
    """
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return False
        return True
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False                      # 进程不存在（或者无权访问：那也不是我们能杀的了）
    try:
        code = wintypes.DWORD()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _wait_gone(pid: int, timeout: float) -> bool:
    """等 pid 消失。不属于我们（或者已经退出）也返回 True。

    注意 POSIX 的 kill(pid, 0) 对僵尸也返回成功：如果 pid 恰好是当前进程没回收的子进程，
    这里会等满宽限期、再给一具尸体补一发 SIGKILL。真沙箱不会这样（控制面重启后旧沙箱被
    init/launchd 收养并回收），代价只是多等几秒；不要用 waitpid 去"确认"——那会把别人的
    子进程回收掉，别人的 wait() 就永远拿不到退出码了。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.1)
    return False


def _kill_pid(pid: int) -> None:
    """杀掉一个不是我们 Popen 出来的沙箱进程（控制面重启前留下的，#60）。

    和 _terminate 的区别是没有 Popen 对象：进程组只能现查（查不到就退回到只杀它自己），
    而 pid 有可能已经被系统复用——所以调用前必须由 _alive_pid 用端口 + 令牌确认过
    它确实是这个会话的沙箱，绝不能拿一个来路不明的 pid 到这里来杀。
    """
    if os.name == "nt":
        try:
            os.kill(pid, signal.SIGTERM)            # Windows 上是 TerminateProcess
        except (OSError, ValueError):
            pass
        _wait_gone(pid, 5)
        return
    # 沙箱正常情况下是 start_new_session 起的、独占一个进程组。万一它和控制面同一个组
    # （比如被别的工具重新拉起过），killpg 会把控制面自己也杀了——那就只杀它一个。
    group = None
    try:
        group = os.getpgid(pid)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    if group is not None and group == os.getpgrp():
        group = None
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if group is None:
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                return
        else:
            try:
                os.killpg(group, sig)               # 带上沙箱自己拉起的 pytest / dev server
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    os.kill(pid, sig)
                except (ProcessLookupError, PermissionError):
                    return
        if _wait_gone(pid, 5):
            return


class LocalLauncher(LocalSnapshots, Launcher):
    name = "local"
    isolated = False

    def __init__(self, data_dir: str, ready_timeout: float = 30):
        self.root = os.path.join(os.path.abspath(data_dir), "sandboxes")
        self.ready_timeout = ready_timeout
        self.procs: dict[str, subprocess.Popen] = {}

    def _dirs(self, sid: str) -> tuple[str, str, str]:
        base = os.path.join(self.root, sid)
        return base, os.path.join(base, "workspace"), os.path.join(base, "home")

    # ------------------------------------------------------------------ 跨重启的进程跟踪
    #
    # 内存字典在控制面重启后就空了，但沙箱进程还活着（start_new_session=True）。
    # 以前 is_alive 只查字典，把"重启前就在跑"的进程判成死，下次访问再起一个，
    # 旧进程成了没人管的孤儿（#60）。现在每个沙箱在 base/pid 里记下
    # pid + 端口 + Web 令牌，重启后靠它找回进程。

    def _pid_file(self, sid: str) -> str:
        return os.path.join(self._dirs(sid)[0], "pid")

    def _write_pid(self, sid: str, pid: int, port: int, token: str) -> None:
        os.makedirs(self._dirs(sid)[0], exist_ok=True)      # 目录可能还没建（沙箱在别处被重新拉起过）
        with open(self._pid_file(sid), "w", encoding="utf-8") as f:
            json.dump({"pid": pid, "port": port, "token": token}, f)

    def _read_pid(self, sid: str) -> tuple[int, int, str] | None:
        try:
            with open(self._pid_file(sid), encoding="utf-8") as f:
                d = json.load(f)
            return int(d["pid"]), int(d["port"]), str(d["token"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _remove_pid(self, sid: str) -> None:
        try:
            os.remove(self._pid_file(sid))
        except OSError:
            pass

    def _alive_pid(self, sid: str) -> tuple[int, int, str] | None:
        """pid 文件里记的沙箱是否还活着、并且真的是这个会话的。

        只认"端口还在、令牌还对得上"的：pid 可能已经被系统复用给别的进程，
        那时当它已经死了——记录作废，但绝不能去动那个无辜的进程（#60）。
        """
        info = self._read_pid(sid)
        if info is None:
            return None
        _, port, token = info
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/state",
                                         headers={"x-nanocode-token": token, "Host": "localhost"})
            with urllib.request.urlopen(req, timeout=2) as r:
                if r.status == 200:
                    return info
        except (urllib.error.URLError, OSError, ValueError):
            pass
        self._remove_pid(sid)
        return None

    def _known_sids(self) -> set[str]:
        """内存里的 + 磁盘上留下 pid 文件的。"""
        sids = set(self.procs)
        if os.path.isdir(self.root):
            sids |= {n for n in os.listdir(self.root)
                     if os.path.isfile(os.path.join(self.root, n, "pid"))}
        return sids

    def start(self, spec: SandboxSpec) -> str:
        if "bypass" in spec.policy.get("allowed_modes", []):
            raise LaunchError("本机启动器没有隔离，不能允许 bypass 模式")
        self.stop(spec.session_id)
        base, workspace, home = self._dirs(spec.session_id)
        os.makedirs(workspace, exist_ok=True)
        os.makedirs(home, exist_ok=True)
        policy_path = os.path.join(base, "policy.json")
        with open(policy_path, "w", encoding="utf-8") as f:
            json.dump({**spec.policy, "audit_log": os.path.join(base, "audit.jsonl")}, f, ensure_ascii=False, indent=1)
        port = _free_port()
        import nanocode
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("NCS_", "NANOCODE_")) and not k.endswith(_SECRET_SUFFIXES)}
        env.update(spec.env(home, policy_path))
        env["PYTHONPATH"] = os.path.dirname(os.path.dirname(os.path.abspath(nanocode.__file__)))
        cmd = [sys.executable, "-m", "nanocode", *spec.args("127.0.0.1", port, workspace)]
        with open(os.path.join(base, "sandbox.log"), "ab") as log:
            proc = subprocess.Popen(cmd, cwd=workspace, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                    start_new_session=True)
        self.procs[spec.session_id] = proc
        self._write_pid(spec.session_id, proc.pid, port, spec.web_token)
        endpoint = f"http://127.0.0.1:{port}"
        log_path = os.path.join(base, "sandbox.log")
        try:
            wait_ready(endpoint, spec.web_token, self.ready_timeout, alive=lambda: proc.poll() is None,
                       log_path=log_path)
        except LaunchError as e:
            self.stop(spec.session_id)
            raise LaunchError(f"{e}（日志：{log_path}）") from None
        return endpoint

    def stop(self, session_id: str) -> None:
        proc = self.procs.pop(session_id, None)
        if proc is not None and proc.poll() is None:
            # 内存里的 Popen 还在：按平台杀——POSIX 杀整个进程组，Windows 用 TerminateProcess（#75）
            _terminate(proc)
            self._remove_pid(session_id)
            return
        info = self._alive_pid(session_id)          # 重启前留下的：确认还是我们的才杀（#60）
        if info is not None:
            _kill_pid(info[0])
        self._remove_pid(session_id)

    def delete(self, session_id: str) -> None:
        self.stop(session_id)
        shutil.rmtree(self._dirs(session_id)[0], ignore_errors=True)

    def is_alive(self, session_id: str) -> bool:
        proc = self.procs.get(session_id)
        if proc is not None and proc.poll() is None:
            return True
        return self._alive_pid(session_id) is not None

    def workspace(self, session_id: str) -> str:
        return self._dirs(session_id)[1]

    def snapshot_root(self, session_id: str) -> str:
        return self._dirs(session_id)[0]          # 下面是 workspace/ 和 home/

    def reconcile(self, keep: set[str]) -> None:
        """停掉 keep 之外的沙箱进程：控制面重启后数据库里已经没有会话的那些（#60）。"""
        for sid in self._known_sids() - keep:
            self.stop(sid)

    def shutdown(self) -> None:
        for sid in self._known_sids():
            self.stop(sid)


__all__ = ["LocalLauncher"]
