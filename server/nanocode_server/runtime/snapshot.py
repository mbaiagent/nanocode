"""工作区快照的打包、校验和恢复：在工作区所在的那台机器上执行（单机是控制面，远程沙箱是沙箱代理）。

快照内容：沙箱数据目录下的 workspace/（用户的项目）和 home/（NANOCODE_HOME：对话记录、配置），
打成 tar.gz。不含平台策略、pid、日志——这些每次启动都会重新生成。

为什么这里要格外小心：打包和恢复都以沙箱主机上的高权限用户（通常是 root）运行，
而工作区内容完全由沙箱里的 Agent（也就是用户，或者被提示词注入的模型）控制。

  打包时（root 读用户可控的目录）：
    - 不跟随符号链接：沙箱里放一个 `ln -s /etc/shadow x`，跟随的话 root 会把宿主机文件
      打进快照，用户下次恢复就拿到了。POSIX 上用 os.fwalk + O_NOFOLLOW 按目录 fd 逐级打开，
      打包期间把文件换成链接也绕不过去；
    - 只收普通文件、目录和指向工作区内部的相对符号链接；设备、FIFO、socket 一律跳过；
    - 超过 max_bytes（NCS_SNAPSHOT_MAX_BYTES，按未压缩大小）直接失败，不上传半个快照。

  恢复时（root 往磁盘写一个从对象存储下载的、内容可能被篡改过的归档）：
    - 先完整校验再写盘：不能有绝对路径、`..`、反斜杠，顶层只能是 workspace/ 或 home/；
    - 只允许普通文件、目录、符号链接；硬链接、设备文件、FIFO 一律拒绝；
    - 符号链接只能是相对的、且解析后还在快照根目录里；路径的上级不能是归档里的符号链接
      （先建链接再顺着链接写文件是经典的 tar 穿越手法）；
    - 解压到一个全新的临时目录：文件用 O_CREAT|O_EXCL|O_NOFOLLOW 创建，符号链接最后才建，
      所以写文件时路径上不可能有链接；权限去掉 setuid/setgid/sticky；
    - 总大小、文件数都有上限（防解压炸弹）；校验通过、全部写完后才换进工作区位置。
"""

from __future__ import annotations

import fnmatch
import io
import os
import posixpath
import shutil
import stat
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from zlib import error as zlib_error

from .base import LaunchError

TOPS = ("workspace", "home")
MAX_MEMBERS = 200_000
# 默认不进快照的：依赖目录、构建缓存、包管理器缓存——都能重新生成，往往又占掉工作区大头
DEFAULT_IGNORE = (
    "node_modules", ".venv", "venv", "__pycache__", ".cache", ".npm", ".pnpm-store", ".yarn",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".nox", ".gradle", ".next", ".nuxt",
    ".turbo", ".parcel-cache", "*.pyc",
)
_HAS_FWALK = hasattr(os, "fwalk") and hasattr(os, "O_NOFOLLOW")


class SnapshotError(LaunchError):
    """快照打包 / 上传 / 下载 / 恢复失败。permanent=True 表示重试也没用（太大、归档不合法）。"""

    def __init__(self, message: str, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


def _ignored(name: str, patterns) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def _link_inside(member_path: str, target: str) -> bool:
    """快速的纯字符串预检：链接目标是相对的、不含反斜杠 / 盘符，按字符串规范化后仍在快照根目录里。

    只是第一道筛子——`..` 在物理上跟着上一级链接走，字符串规范化挡不住链式链接
    （PR #144 review 的反例：home/a/b/k -> ../../../workspace，workspace/l -> ../home/a/b/k/../../..）。
    恢复时真正的判定是 resolve_link()：按归档里的链接表逐段模拟解析。
    """
    if not target or target.startswith("/") or "\\" in target or "\0" in target or (len(target) > 1 and target[1] == ":"):
        return False
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(member_path), target))
    return resolved != ".." and not resolved.startswith("../") and resolved.split("/", 1)[0] in TOPS


MAX_LINK_HOPS = 40


def resolve_link(path: str, links: dict[str, str]) -> list[str] | None:
    """像内核一样逐段解析归档里的符号链接 path（links：链接路径 → 目标），返回最终路径的各段；
    任何时候走出快照根目录、链接成环或展开超过 MAX_LINK_HOPS 次，返回 None。

    遇到 `..` 弹出的是"当前物理位置"的上一级：先展开路径上已经走过的链接再弹，
    所以链式链接里的 `..` 会被正确地算到链接指向的位置上。
    """
    hops = 0

    def walk(base: list[str], target: str) -> list[str] | None:
        nonlocal hops
        if not target or target.startswith("/") or "\\" in target or "\0" in target or (len(target) > 1 and target[1] == ":"):
            return None
        cur = list(base)
        for seg in target.split("/"):
            if seg in ("", "."):
                continue
            if seg == "..":
                if not cur:
                    return None                       # 走出快照根目录
                cur.pop()
                continue
            cur.append(seg)
            key = "/".join(cur)
            if key in links:
                hops += 1
                if hops > MAX_LINK_HOPS:
                    return None
                cur = walk(cur[:-1], links[key])
                if cur is None:
                    return None
        return cur

    parts = path.split("/")
    final = walk(parts[:-1], links[path])
    if final is None or not final or final[0] not in TOPS:
        return None
    return final


class _Exact(io.RawIOBase):
    """恰好读出 size 字节：打包时文件被改短就补零、变长就截断（tarfile 要求和头里的大小一致）。"""

    def __init__(self, f, size: int):
        self.f, self.left = f, size

    def readable(self):
        return True

    def read(self, n=-1):
        if self.left <= 0:
            return b""
        n = self.left if n is None or n < 0 else min(n, self.left)
        data = self.f.read(n) or b""
        if not data:
            data = b"\0" * n
        self.left -= len(data)
        return data


class _Packer:
    def __init__(self, tar: tarfile.TarFile, max_bytes: int, ignore):
        self.tar, self.max_bytes, self.ignore = tar, max_bytes, tuple(ignore)
        self.total = self.files = self.skipped = 0

    def _info(self, name: str, st: os.stat_result, kind: bytes) -> tarfile.TarInfo:
        ti = tarfile.TarInfo(name)
        ti.type = kind
        ti.mode = stat.S_IMODE(st.st_mode) & 0o777          # 不带 setuid / setgid / sticky
        ti.mtime = int(st.st_mtime)
        ti.uid = ti.gid = 0
        ti.uname = ti.gname = ""
        return ti

    def _count(self, size: int) -> None:
        self.total += size
        self.files += 1
        if self.total > self.max_bytes:
            raise SnapshotError(f"工作区超过快照大小上限 {self.max_bytes} 字节（NCS_SNAPSHOT_MAX_BYTES）", permanent=True)
        if self.files > MAX_MEMBERS:
            raise SnapshotError(f"工作区文件数超过 {MAX_MEMBERS}", permanent=True)

    def add_file(self, arc: str, f, st: os.stat_result) -> None:
        self._count(st.st_size)
        ti = self._info(arc, st, tarfile.REGTYPE)
        ti.size = st.st_size
        self.tar.addfile(ti, _Exact(f, st.st_size))

    def add_dir(self, arc: str, st: os.stat_result) -> None:
        self._count(0)
        self.tar.addfile(self._info(arc, st, tarfile.DIRTYPE))

    def add_link(self, arc: str, target: str, st: os.stat_result) -> None:
        # 字符串预检 + 按磁盘上的真实解析（跟着链式链接走）确认还在快照根目录里；
        # 恢复时还会按归档内容再校验一次（resolve_link），这里只是不把坏链接打进去
        if not _link_inside(arc, target) or not self._really_inside(arc):
            self.skipped += 1                              # 指向工作区外面的链接不进快照
            return
        self._count(0)
        ti = self._info(arc, st, tarfile.SYMTYPE)
        ti.mode = 0o777
        ti.linkname = target
        self.tar.addfile(ti)

    root = ""

    def _really_inside(self, arc: str) -> bool:
        if not self.root:
            return True
        real = os.path.realpath(os.path.join(self.root, *arc.split("/")))
        base = os.path.realpath(self.root)
        tops = [os.path.join(base, t) for t in TOPS]
        return any(real == t or real.startswith(t + os.sep) for t in tops)

    # POSIX：按目录 fd 逐级走，不跟随任何链接
    def walk_fd(self, top: str, root: str) -> None:
        for dirpath, dirnames, filenames, dfd in os.fwalk(root, follow_symlinks=False):
            rel = os.path.relpath(dirpath, root)
            base = top if rel == "." else f"{top}/{rel.replace(os.sep, '/')}"
            self.add_dir(base, os.stat(dfd))
            keep = []
            for d in dirnames:
                st = os.stat(d, dir_fd=dfd, follow_symlinks=False)
                if _ignored(d, self.ignore):
                    continue
                if stat.S_ISLNK(st.st_mode):                # fwalk 把指向目录的链接也列在 dirnames 里
                    self.add_link(f"{base}/{d}", os.readlink(d, dir_fd=dfd), st)
                elif stat.S_ISDIR(st.st_mode):
                    keep.append(d)
            dirnames[:] = keep
            for name in filenames:
                if _ignored(name, self.ignore):
                    continue
                st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
                arc = f"{base}/{name}"
                if stat.S_ISLNK(st.st_mode):
                    self.add_link(arc, os.readlink(name, dir_fd=dfd), st)
                elif stat.S_ISREG(st.st_mode):
                    try:
                        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
                    except OSError:
                        self.skipped += 1                   # 打包期间被删 / 换成了链接
                        continue
                    with os.fdopen(fd, "rb") as f:
                        fst = os.fstat(f.fileno())
                        if not stat.S_ISREG(fst.st_mode):
                            self.skipped += 1
                            continue
                        self.add_file(arc, f, fst)
                else:
                    self.skipped += 1                       # 设备、FIFO、socket

    # Windows（只有单机本地模式会用到）：没有 fwalk / O_NOFOLLOW，用 lstat 判断
    def walk_plain(self, top: str, root: str) -> None:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            rel = os.path.relpath(dirpath, root)
            base = top if rel == "." else f"{top}/{rel.replace(os.sep, '/')}"
            self.add_dir(base, os.lstat(dirpath))
            dirnames[:] = [d for d in dirnames if not _ignored(d, self.ignore)
                           and not os.path.islink(os.path.join(dirpath, d))]
            for name in filenames:
                p = os.path.join(dirpath, name)
                st = os.lstat(p)
                if _ignored(name, self.ignore) or not stat.S_ISREG(st.st_mode):
                    self.skipped += 1
                    continue
                with open(p, "rb") as f:
                    self.add_file(f"{base}/{name}", f, os.fstat(f.fileno()))


def pack(root: str, out_path: str, max_bytes: int, ignore=DEFAULT_IGNORE) -> dict:
    """把 root 下的 workspace/ 和 home/ 打成 out_path（tar.gz）。返回统计。"""
    started = time.monotonic()
    with tarfile.open(out_path, "w:gz", compresslevel=6, format=tarfile.PAX_FORMAT) as tar:
        p = _Packer(tar, max_bytes, ignore)
        p.root = root
        for top in TOPS:
            d = os.path.join(root, top)
            if not os.path.isdir(d) or os.path.islink(d):
                continue
            (p.walk_fd if _HAS_FWALK else p.walk_plain)(top, d)
    return {"bytes": os.path.getsize(out_path), "raw_bytes": p.total, "files": p.files, "skipped": p.skipped,
            "seconds": round(time.monotonic() - started, 3)}


# ---------------------------------------------------------------------- 恢复


class _Checker:
    """逐个成员校验（边读边查，不先把全部成员头读进内存），最后再统一校验符号链接。"""

    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes
        self.seen: set[str] = set()
        self.links: dict[str, str] = {}
        self.total = 0
        self.count = 0

    def member(self, m: tarfile.TarInfo) -> str:
        self.count += 1
        if self.count > MAX_MEMBERS:
            raise SnapshotError(f"快照里的文件数超过 {MAX_MEMBERS}", permanent=True)
        name = m.name
        if not name or name.startswith("/") or "\\" in name or "\0" in name or (len(name) > 1 and name[1] == ":"):
            raise SnapshotError(f"快照里有不合法的路径：{name!r}", permanent=True)
        parts = [x for x in name.split("/") if x not in ("", ".")]
        if not parts or ".." in parts or parts[0] not in TOPS:
            raise SnapshotError(f"快照里有越界的路径：{name!r}", permanent=True)
        path = "/".join(parts)
        if not (m.isreg() or m.isdir() or m.issym()):
            raise SnapshotError(f"快照里有不允许的条目类型（硬链接 / 设备 / FIFO）：{name!r}", permanent=True)
        if path in self.seen:
            raise SnapshotError(f"快照里有重复的路径：{name!r}", permanent=True)
        self.seen.add(path)
        if m.issym():
            if not _link_inside(path, m.linkname):
                raise SnapshotError(f"快照里有指向外面的符号链接：{name!r} -> {m.linkname!r}", permanent=True)
            self.links[path] = m.linkname
        if m.isreg():
            self.total += max(0, m.size)
            if self.total > self.max_bytes:
                raise SnapshotError(f"快照解压后超过大小上限 {self.max_bytes} 字节", permanent=True)
        return path

    def finish(self) -> None:
        for path in self.seen:                          # 任何路径的上级都不能是归档里的链接
            parts = path.split("/")
            for i in range(1, len(parts)):
                if "/".join(parts[:i]) in self.links:
                    raise SnapshotError(f"快照里有经过符号链接的路径：{path!r}", permanent=True)
        for path, target in self.links.items():         # 链式链接逐段解析，必须仍在快照根目录里
            if resolve_link(path, self.links) is None:
                raise SnapshotError(f"快照里有（经链式解析后）指向外面的符号链接：{path!r} -> {target!r}",
                                    permanent=True)


def validate_members(members, max_bytes: int) -> list[tuple[str, tarfile.TarInfo]]:
    """完整校验归档成员，返回 (规范化路径, 成员)。任何一个不合法都整体拒绝。"""
    c = _Checker(max_bytes)
    out = [(c.member(m), m) for m in members]
    c.finish()
    return out


def extract(archive: str, root: str, max_bytes: int) -> dict:
    """把归档恢复到 root 下的 workspace/ 和 home/。root 下这两个目录必须不存在或为空。

    流式读（r|gz）：边读边校验、边把普通文件和目录写进全新的临时目录，文件数和总大小一超限立即停；
    符号链接先记下来，读完后按完整的链接表校验，全部合格才建链接、换进工作区位置。
    """
    for top in TOPS:
        d = os.path.join(root, top)
        if os.path.islink(d) or (os.path.isdir(d) and os.listdir(d)) or (os.path.exists(d) and not os.path.isdir(d)):
            raise SnapshotError(f"{top}/ 已经有内容，不覆盖本地工作区")
    os.makedirs(root, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix=".restore-", dir=root)
    try:
        try:
            tar = tarfile.open(archive, "r|gz")
        except (tarfile.TarError, OSError, EOFError) as e:
            raise SnapshotError(f"快照不是合法的 tar.gz：{e}", permanent=True) from None
        checker = _Checker(max_bytes)
        files = 0
        symlinks = []
        with tar:
            try:
                for m in tar:
                    path = checker.member(m)
                    dest = os.path.join(tmp, *path.split("/"))
                    if m.isdir():
                        os.makedirs(dest, mode=0o755, exist_ok=True)
                        continue
                    if m.issym():
                        symlinks.append((m.linkname, dest))
                        continue
                    # 上级目录如果是本归档里（稍后才建）的链接，finish() 会整份拒绝；这时文件只落在临时目录里
                    os.makedirs(os.path.dirname(dest), mode=0o755, exist_ok=True)
                    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_BINARY", 0))
                    fd = os.open(dest, flags, 0o600)
                    src = tar.extractfile(m)
                    with os.fdopen(fd, "wb") as out:
                        if src is not None:
                            shutil.copyfileobj(src, out, 1 << 20)
                    os.chmod(dest, (m.mode & 0o777) | 0o600)
                    files += 1
            except (tarfile.TarError, EOFError, zlib_error) as e:
                raise SnapshotError(f"快照损坏：{e}", permanent=True) from None
        checker.finish()
        for target, dest in symlinks:                    # 链接最后建：写文件时路径上不可能有链接
            os.makedirs(os.path.dirname(dest), mode=0o755, exist_ok=True)
            try:
                os.symlink(target, dest)
            except (OSError, NotImplementedError):
                pass                                     # Windows 没权限建链接：跳过
        for top in TOPS:
            d = os.path.join(root, top)
            if os.path.isdir(d):
                os.rmdir(d)                              # 上面确认过是空目录
            src = os.path.join(tmp, top)
            if os.path.isdir(src):
                os.replace(src, d)
            else:
                os.makedirs(d, exist_ok=True)
        return {"files": files}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------- 传输（预签名地址）


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """对象存储不该回 3xx：不跟随重定向，少一个被引到别处（SSRF）的口子。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _check_url(url: str) -> None:
    if not url.startswith(("https://", "http://")):
        raise SnapshotError("快照地址必须是 http(s)://", permanent=True)


def upload(url: str, path: str, timeout: float = 600) -> None:
    _check_url(url)
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        req = urllib.request.Request(url, data=f, method="PUT", headers={
            "content-length": str(size), "content-type": "application/gzip"})
        try:
            with _opener.open(req, timeout=timeout) as r:
                r.read()
        except urllib.error.HTTPError as e:
            raise SnapshotError(f"上传快照失败：对象存储返回 HTTP {e.code}") from None
        except (urllib.error.URLError, OSError) as e:
            raise SnapshotError(f"上传快照失败：{getattr(e, 'reason', e)}") from None


def download(url: str, path: str, limit: int, timeout: float = 600) -> int:
    _check_url(url)
    try:
        with _opener.open(urllib.request.Request(url), timeout=timeout) as r, open(path, "wb") as f:
            n = 0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                n += len(chunk)
                if n > limit:
                    raise SnapshotError(f"快照文件超过 {limit} 字节", permanent=True)
                f.write(chunk)
            return n
    except urllib.error.HTTPError as e:
        raise SnapshotError(f"下载快照失败：对象存储返回 HTTP {e.code}", permanent=e.code in (403, 404)) from None
    except (urllib.error.URLError, OSError) as e:
        raise SnapshotError(f"下载快照失败：{getattr(e, 'reason', e)}") from None


def archive_limit(max_bytes: int) -> int:
    """下载的压缩包最多多大：未压缩上限 + gzip/tar 头的余量（随机数据压不动，还会略大一点）。"""
    return max_bytes + max_bytes // 20 + (16 << 20)


class LocalSnapshots:
    """启动器混入：工作区就在本机磁盘上（本机子进程 / Docker，也就是沙箱代理那一端）。

    子类实现 snapshot_root(sid)：workspace/ 和 home/ 所在的目录。
    """

    supports_snapshots = True

    def snapshot_root(self, session_id: str) -> str:  # pragma: no cover - 子类实现
        raise NotImplementedError

    def _scratch(self) -> str:
        d = os.path.join(getattr(self, "root"), ".snapshots-tmp")       # 不在任何工作区里，沙箱看不到
        os.makedirs(d, mode=0o700, exist_ok=True)
        return d

    def has_workspace(self, session_id: str) -> bool:
        root = self.snapshot_root(session_id)
        for top in TOPS:
            d = os.path.join(root, top)
            if os.path.islink(d) or (os.path.isdir(d) and os.listdir(d)):
                return True
        return False

    def snapshot(self, session_id: str, upload_url: str, max_bytes: int, ignore=DEFAULT_IGNORE) -> dict:
        fd, tmp = tempfile.mkstemp(prefix=f"{session_id}-", suffix=".tar.gz", dir=self._scratch())
        os.close(fd)
        try:
            try:
                stats = pack(self.snapshot_root(session_id), tmp, max_bytes, ignore)
            except OSError as e:
                raise SnapshotError(f"打包工作区失败：{e}") from None
            upload(upload_url, tmp)
            return stats
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    def restore(self, session_id: str, download_url: str, max_bytes: int) -> dict:
        fd, tmp = tempfile.mkstemp(prefix=f"{session_id}-", suffix=".tar.gz", dir=self._scratch())
        os.close(fd)
        try:
            size = download(download_url, tmp, archive_limit(max_bytes))
            try:
                stats = extract(tmp, self.snapshot_root(session_id), max_bytes)
            except OSError as e:
                raise SnapshotError(f"恢复工作区失败：{e}") from None
            return {**stats, "bytes": size}
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass


__all__ = ["pack", "extract", "validate_members", "upload", "download", "archive_limit", "LocalSnapshots",
           "SnapshotError", "DEFAULT_IGNORE", "TOPS"]
