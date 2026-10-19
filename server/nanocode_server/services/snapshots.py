"""工作区快照：沙箱停止 / 空闲回收 / 删除时把工作区存进 S3 兼容的对象存储，换了机器或磁盘丢了还能恢复。

谁上传（#134 之后沙箱里不该再有任何长期凭证，这里同样）：
  - 对象存储的访问密钥只在控制面（NCS_S3_ACCESS_KEY_ID / NCS_S3_SECRET_ACCESS_KEY）。
  - 真正搬数据的是工作区所在的那台机器：单机 / Docker 是控制面自己，远程沙箱是沙箱主机上的沙箱代理。
    控制面只给它一个**某一个对象、一种操作（PUT 或 GET）、一小时内有效**的预签名地址。
  - 为什么不把（哪怕只限一个桶的）密钥放到沙箱主机上：沙箱主机跑的是不可信代码，是两台机器里
    更容易被打穿的那台。它拿到桶密钥 = 能读、改、删**所有用户**的快照；只拿预签名地址 = 最多
    读写它本来就有本地副本的那几个工作区，而且一小时后作废。列目录、按保留份数删旧快照这些
    需要桶级权限的操作只在控制面做。
  - 沙箱容器什么都拿不到：预签名地址只在控制面 → 代理的管理接口里传，打包和上传都在容器外面、
    容器停掉之后进行。

生命周期：
  stop / 空闲回收      先停容器（不等快照），再在后台打包上传；失败按退避重试几次，只记日志和计数，
                       从不阻塞停止。
  delete               停容器、标记删除，后台快照完成（或最终失败）后才删本地工作区。
  再次启动             本机没有工作区（换了沙箱主机、磁盘清过）且会话以前启动过 → 下载最近一份恢复；
                       最近一份坏了就试更早的；全都恢复不了就让这次启动失败（不能拿空工作区启动，
                       否则下次停止时空快照会把好快照挤出保留名额）。
                       同一个会话还有快照在后台跑时，启动先等它结束（不会一边打包一边写）。
  手动                 POST /api/sessions/<id>/snapshot（会话主人），沙箱可以在运行。
  保留                 上传成功后列出这个工作区的快照，只留最近 NCS_SNAPSHOT_KEEP 份。

对象 key：<NCS_S3_PREFIX>/<用户>/<会话>/<UTC 时间，到毫秒>.tar.gz，按字典序就是时间序。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import re
import threading
import time

from ..runtime import Launcher
from ..runtime.snapshot import DEFAULT_IGNORE, SnapshotError
from ..storage.objectstore import ObjectStoreError, S3Client

_NAME = re.compile(r"\d{8}T\d{6}\d{3}Z\.tar\.gz")
_SAFE = re.compile(r"[^A-Za-z0-9._-]")
PRESIGN_TTL = 3600


def safe_segment(value: str) -> str:
    """用户 id 放进对象 key：只留安全字符；改动过的加一段哈希，避免 a@b 和 a_b 撞到同一个目录。"""
    clean = _SAFE.sub("_", value)[:80] or "_"
    if clean != value:
        clean += "-" + hashlib.sha256(value.encode()).hexdigest()[:8]
    return clean


class SnapshotManager:
    def __init__(self, cfg, launcher: Launcher, s3: S3Client, log=print,
                 retries: int = 3, backoff: tuple[float, ...] = (5, 30), concurrency: int = 2):
        if not getattr(launcher, "supports_snapshots", False):
            raise ValueError(f"NCS_OBJECT_STORE=s3：{launcher.name} 启动器不支持工作区快照"
                             "（只支持 local / docker / remote；K8s 请用存储类自己的卷快照）")
        self.cfg, self.launcher, self.s3, self.log = cfg, launcher, s3, log
        self.prefix = cfg.s3_prefix.strip("/")
        self.keep = cfg.snapshot_keep
        self.max_bytes = cfg.snapshot_max
        self.ignore = list(DEFAULT_IGNORE) + list(cfg.snapshot_ignore)
        self.retries, self.backoff = max(1, retries), backoff
        self._slots = threading.Semaphore(concurrency)    # 同时打包上传的数量：别把沙箱主机的磁盘和带宽占满
        self._guard = threading.Lock()
        self._pending: dict[str, threading.Event] = {}    # 会话 → 后台快照完成的信号
        self._closing = threading.Event()
        self.metrics = {"uploaded": 0, "upload_failed": 0, "upload_retries": 0, "bytes_uploaded": 0,
                        "restored": 0, "restore_failed": 0, "pruned": 0, "prune_failed": 0, "last_error": ""}

    # ------------------------------------------------------------------ key

    def workspace_prefix(self, owner: str, sid: str) -> str:
        return f"{self.prefix}/{safe_segment(owner)}/{sid}/"

    def new_key(self, owner: str, sid: str) -> str:
        now = _dt.datetime.now(_dt.timezone.utc)
        return self.workspace_prefix(owner, sid) + now.strftime("%Y%m%dT%H%M%S") + f"{now.microsecond // 1000:03d}Z.tar.gz"

    def list(self, owner: str, sid: str) -> list[dict]:
        """这个工作区的快照，新的在前。"""
        prefix = self.workspace_prefix(owner, sid)
        objs = [o for o in self.s3.list(prefix) if _NAME.fullmatch(o.key[len(prefix):])]
        objs.sort(key=lambda o: o.key, reverse=True)
        return [{"key": o.key, "name": o.key[len(prefix):], "size": o.size, "last_modified": o.last_modified}
                for o in objs]

    # ------------------------------------------------------------------ 指标 / 日志

    def _count(self, name: str, n: int = 1) -> None:
        with self._guard:
            self.metrics[name] += n

    def _fail(self, msg: str) -> None:
        with self._guard:
            self.metrics["last_error"] = msg[:300]
        self.log(f"[snapshot] {msg}")

    def stats(self) -> dict:
        with self._guard:
            return {**self.metrics, "pending": len(self._pending), "keep": self.keep, "max_bytes": self.max_bytes}

    # ------------------------------------------------------------------ 上传

    def snapshot_now(self, owner: str, sid: str) -> dict:
        """打一份快照并上传（一次，不重试），成功后按保留份数清理。失败抛 SnapshotError / ObjectStoreError。"""
        key = self.new_key(owner, sid)
        with self._slots:
            stats = self.launcher.snapshot(sid, self.s3.presign("PUT", key, PRESIGN_TTL), self.max_bytes, self.ignore)
        self._count("uploaded")
        self._count("bytes_uploaded", int(stats.get("bytes") or 0))
        self.log(f"[snapshot] uploaded session={sid} bytes={stats.get('bytes')} raw={stats.get('raw_bytes')} "
                 f"files={stats.get('files')} skipped={stats.get('skipped')} seconds={stats.get('seconds')}")
        self.prune(owner, sid)
        return {"key": key, **stats}

    def prune(self, owner: str, sid: str) -> int:
        try:
            old = self.list(owner, sid)[self.keep:]
            for o in old:
                self.s3.delete(o["key"])
            self._count("pruned", len(old))
            return len(old)
        except ObjectStoreError as e:
            self._count("prune_failed")
            self._fail(f"prune failed session={sid}: {e}")       # 下次上传成功时会再清理
            return 0

    def _attempt_all(self, owner: str, sid: str) -> bool:
        for attempt in range(1, self.retries + 1):
            if attempt > 1:
                self._count("upload_retries")
                if self._closing.wait(self.backoff[min(attempt - 2, len(self.backoff) - 1)]):
                    break
            try:
                self.snapshot_now(owner, sid)
                return True
            except (SnapshotError, ObjectStoreError) as e:
                permanent = getattr(e, "permanent", False)
                self._fail(f"upload failed session={sid} attempt={attempt}/{self.retries}"
                           f"{' permanent' if permanent else ''}: {e}")
                if permanent:
                    break
            except Exception as e:  # noqa: BLE001 —— 后台线程不能把异常抛丢
                self._fail(f"upload failed session={sid} attempt={attempt}/{self.retries}: {type(e).__name__}: {e}")
        self._count("upload_failed")
        return False

    def schedule(self, owner: str, sid: str, then=None) -> threading.Event:
        """后台打包上传（带重试），立即返回。then：快照结束后（无论成败）要做的事，例如删除本地工作区。"""
        with self._guard:
            prev = self._pending.get(sid)
            done = threading.Event()
            self._pending[sid] = done

        def run():
            try:
                if prev is not None:
                    prev.wait(900)                        # 同一个会话的快照按顺序来
                self._attempt_all(owner, sid)
            finally:
                try:
                    if then is not None:
                        then()
                except Exception as e:  # noqa: BLE001
                    self.log(f"[snapshot] after-snapshot step failed session={sid}: {type(e).__name__}: {e}")
                with self._guard:
                    if self._pending.get(sid) is done:
                        self._pending.pop(sid, None)
                done.set()

        threading.Thread(target=run, daemon=True, name=f"snapshot-{sid}").start()
        return done

    def settle(self, sid: str, timeout: float = 300) -> bool:
        """等这个会话的后台快照结束（启动前调用）。超时返回 False，调用方照常继续。"""
        with self._guard:
            ev = self._pending.get(sid)
        if ev is None:
            return True
        ok = ev.wait(timeout)
        if not ok:
            self.log(f"[snapshot] still running after {timeout:.0f}s session={sid}, starting anyway")
        return ok

    # ------------------------------------------------------------------ 恢复

    def restore_if_missing(self, owner: str, sid: str) -> dict | None:
        """本机没有工作区时恢复最近一份能用的快照；没有快照返回 None。都恢复不了抛 SnapshotError。"""
        if self.launcher.has_workspace(sid):
            return None
        try:
            snaps = self.list(owner, sid)
        except ObjectStoreError as e:
            self._count("restore_failed")
            self._fail(f"restore failed session={sid}: {e}")
            raise SnapshotError(f"列不出工作区快照：{e}") from None
        if not snaps:
            return None
        errors = []
        for snap in snaps:
            for attempt in (1, 2):                        # 网络抖动重试一次；归档本身坏了就换更早的一份
                try:
                    stats = self.launcher.restore(sid, self.s3.presign("GET", snap["key"], PRESIGN_TTL), self.max_bytes)
                    self._count("restored")
                    self.log(f"[snapshot] restored session={sid} from={snap['name']} files={stats.get('files')} "
                             f"bytes={stats.get('bytes')}")
                    return {"key": snap["key"], **stats}
                except SnapshotError as e:
                    errors.append(f"{snap['name']}: {e}")
                    self._fail(f"restore failed session={sid} from={snap['name']} attempt={attempt}: {e}")
                    if e.permanent:
                        break
        self._count("restore_failed")
        raise SnapshotError("工作区快照都恢复不了：" + "；".join(errors[-3:]))

    def close(self, timeout: float = 30) -> None:
        """控制面退出：不再等重试的退避；正在上传的最多等 timeout 秒。"""
        self._closing.set()
        deadline = time.time() + timeout
        with self._guard:
            events = list(self._pending.values())
        for ev in events:
            ev.wait(max(0.0, deadline - time.time()))


__all__ = ["SnapshotManager", "safe_segment"]
