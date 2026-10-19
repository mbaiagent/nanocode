"""工作区快照（NCS_OBJECT_STORE=s3）：SigV4 签名、打包 / 恢复的安全校验、保留份数、失败重试、
单机和远程沙箱（沙箱代理）两条链路，以及密钥不进沙箱。

对象存储用一个会校验 SigV4 签名的假 S3（path-style，只认对的密钥）；装了 moto 时
再对 moto 的 S3 服务器跑一遍（可选，CI 里没有就跳过）。
"""

import datetime as dt
import hashlib
import io
import json
import os
import re
import shutil
import stat
import tarfile
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qsl, unquote, urlparse

import support  # noqa: F401

from nanocode_server.config import Config
from nanocode_server.platform import Platform
from nanocode_server.runtime import Launcher, LocalLauncher, RemoteLauncher
from nanocode_server.runtime.snapshot import (DEFAULT_IGNORE, LocalSnapshots, SnapshotError, extract, pack,
                                              validate_members)
from nanocode_server.sandbox_agent import build_agent
from nanocode_server.services.snapshots import SnapshotManager, safe_segment
from nanocode_server.storage.objectstore import UNSIGNED, ObjectStoreError, S3Client
from nanocode_server.web.framework import make_server
from support import Client, LocalHTTPServer, make_config

POSIX = os.name != "nt"
AK, SK, BUCKET = "test-access-key", "test-secret-key-" + "z" * 24, "ws-bucket"
NAME = re.compile(r"snapshots/[A-Za-z0-9._-]+/[A-Za-z0-9]{16}/\d{8}T\d{9}Z\.tar\.gz")


# ---------------------------------------------------------------------- 假 S3（校验签名）


class FakeS3:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.fail_puts = 0                       # 接下来几个 PUT 回 500
        self.fail_gets = 0
        self.requests = []
        self.verifier = S3Client("http://x", "us-east-1", BUCKET, AK, SK)
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _reply(self, status, body=b"", ctype="application/xml"):
                self.send_response(status)
                self.send_header("content-type", ctype)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _handle(self):
                n = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(n) if n else b""
                u = urlparse(self.path)
                path = unquote(u.path)
                query = dict(parse_qsl(u.query, keep_blank_values=True))
                outer.requests.append((self.command, path, sorted(query)))
                if not outer._authorized(self.command, path, query, self.headers, body):
                    return self._reply(403, b"<Error><Code>SignatureDoesNotMatch</Code></Error>")
                parts = path.lstrip("/").split("/", 1)
                if parts[0] != BUCKET:
                    return self._reply(404, b"<Error><Code>NoSuchBucket</Code></Error>")
                key = parts[1] if len(parts) > 1 else ""
                if self.command == "PUT":
                    if outer.fail_puts > 0:
                        outer.fail_puts -= 1
                        return self._reply(500, b"<Error><Code>InternalError</Code></Error>")
                    outer.objects[key] = body
                    return self._reply(200)
                if self.command == "DELETE":
                    outer.objects.pop(key, None)
                    return self._reply(204)
                if self.command == "GET" and not key and query.get("list-type") == "2":
                    return self._reply(200, outer._list(query))
                if self.command == "GET":
                    if outer.fail_gets > 0:
                        outer.fail_gets -= 1
                        return self._reply(500, b"<Error><Code>InternalError</Code></Error>")
                    if key not in outer.objects:
                        return self._reply(404, b"<Error><Code>NoSuchKey</Code></Error>")
                    return self._reply(200, outer.objects[key], "application/octet-stream")
                return self._reply(400)

            do_GET = do_PUT = do_DELETE = do_HEAD = _handle

        self.server = LocalHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.endpoint = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _authorized(self, method, path, query, headers, body) -> bool:
        host = headers.get("host", "")
        if "X-Amz-Signature" in query:                               # 预签名
            q = dict(query)
            sig = q.pop("X-Amz-Signature")
            if not q.get("X-Amz-Credential", "").startswith(AK + "/"):
                return False
            when = dt.datetime.strptime(q["X-Amz-Date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc)
            if (dt.datetime.now(dt.timezone.utc) - when).total_seconds() > int(q["X-Amz-Expires"]):
                return False
            want, _ = self.verifier.signature(method, path, q, {"host": host}, UNSIGNED, q["X-Amz-Date"])
            return want == sig
        auth = headers.get("authorization", "")
        m = re.fullmatch(r"AWS4-HMAC-SHA256 Credential=([^/]+)/[^,]+, SignedHeaders=([^,]+), Signature=(\w+)", auth)
        if not m or m.group(1) != AK:
            return False
        payload = headers.get("x-amz-content-sha256", "")
        if payload != hashlib.sha256(body).hexdigest():
            return False
        signed = {n: (host if n == "host" else headers.get(n, "")) for n in m.group(2).split(";")}
        want, _ = self.verifier.signature(method, path, query, signed, payload, headers.get("x-amz-date", ""))
        return want == m.group(3)

    def _list(self, query) -> bytes:
        prefix = query.get("prefix", "")
        keys = sorted(k for k in self.objects if k.startswith(prefix))
        start = int(query.get("continuation-token") or 0)
        page = keys[start:start + int(query.get("max-keys", 1000))]
        more = start + len(page) < len(keys)
        items = "".join(f"<Contents><Key>{k}</Key><Size>{len(self.objects[k])}</Size>"
                        f"<LastModified>2026-01-01T00:00:00.000Z</LastModified></Contents>" for k in page)
        token = f"<NextContinuationToken>{start + len(page)}</NextContinuationToken>" if more else ""
        return (f'<?xml version="1.0"?><ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                f"<IsTruncated>{'true' if more else 'false'}</IsTruncated>{token}{items}</ListBucketResult>").encode()

    def client(self, secret=SK) -> S3Client:
        return S3Client(self.endpoint, "us-east-1", BUCKET, AK, secret)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def s3_config(fake: FakeS3, **kw) -> Config:
    return make_config(object_store="s3", s3_endpoint=fake.endpoint, s3_bucket=BUCKET, s3_access_key_id=AK,
                       s3_secret_access_key=SK, **kw)


# ---------------------------------------------------------------------- 签名


class SigV4Test(unittest.TestCase):
    """AWS 文档（Signature Version 4 for S3）里的官方示例。"""

    def setUp(self):
        self.c = S3Client("https://examplebucket.s3.amazonaws.com", "us-east-1", "", "AKIAIOSFODNN7EXAMPLE",
                          "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
        self.c._path = lambda key: "/" + key        # 文档示例是 virtual-hosted 风格

    def test_presigned_url_example(self):
        url = self.c.presign("GET", "test.txt", 86400, dt.datetime(2013, 5, 24, tzinfo=dt.timezone.utc))
        self.assertTrue(url.endswith("X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404"))
        self.assertIn("X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20130524%2Fus-east-1%2Fs3%2Faws4_request", url)

    def test_list_objects_example(self):
        empty = hashlib.sha256(b"").hexdigest()
        sig, signed = self.c.signature("GET", "/", {"max-keys": "2", "prefix": "J"}, {
            "host": "examplebucket.s3.amazonaws.com", "x-amz-content-sha256": empty,
            "x-amz-date": "20130524T000000Z"}, empty, "20130524T000000Z")
        self.assertEqual(sig, "34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7")
        self.assertEqual(signed, "host;x-amz-content-sha256;x-amz-date")


class ObjectStoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = FakeS3()

    @classmethod
    def tearDownClass(cls):
        cls.fake.close()

    def test_put_get_list_delete_with_pagination(self):
        c = self.fake.client()
        for i in range(5):
            c.put(f"p/a/{i}.bin", b"x" * i)
        c.put("p/b/other", b"y")
        listing = []
        orig = c._request

        def small_pages(method, key="", query=None, body=b""):
            if query and "max-keys" in query:
                query = {**query, "max-keys": "2"}
                listing.append(query.get("continuation-token", ""))
            return orig(method, key, query, body)

        c._request = small_pages
        objs = c.list("p/a/")
        self.assertEqual([o.key for o in objs], [f"p/a/{i}.bin" for i in range(5)])
        self.assertEqual(len(listing), 3)                  # 翻了三页
        self.assertEqual(c.get("p/a/3.bin"), b"xxx")
        c.delete("p/a/3.bin")
        self.assertNotIn("p/a/3.bin", self.fake.objects)

    def test_wrong_secret_is_rejected_and_error_has_no_secret(self):
        with self.assertRaises(ObjectStoreError) as cm:
            self.fake.client("wrong-secret-" + "q" * 20).put("k", b"v")
        self.assertIn("403", str(cm.exception))
        self.assertNotIn("wrong-secret", str(cm.exception))

    def test_presigned_put_and_get_only_work_for_that_key_and_method(self):
        c = self.fake.client()
        url = c.presign("PUT", "only/this", 60)
        urllib.request.urlopen(urllib.request.Request(url, data=b"data", method="PUT")).read()
        self.assertEqual(self.fake.objects["only/this"], b"data")
        other = url.replace("only/this", "only/that")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(urllib.request.Request(other, data=b"x", method="PUT"))
        self.assertEqual(cm.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError):    # PUT 的地址不能拿来 DELETE
            urllib.request.urlopen(urllib.request.Request(url, method="DELETE"))
        self.assertEqual(urllib.request.urlopen(c.presign("GET", "only/this", 60)).read(), b"data")


@unittest.skipUnless(__import__("importlib").util.find_spec("moto"), "没装 moto（可选）")
class MotoTest(unittest.TestCase):
    def test_against_moto_server(self):
        from moto.server import ThreadedMotoServer
        server = ThreadedMotoServer(ip_address="127.0.0.1", port=0)
        server.start()
        self.addCleanup(server.stop)
        host, port = server.get_host_and_port()
        c = S3Client(f"http://{host}:{port}", "us-east-1", "moto-bucket", "testing", "testing")
        c._request("PUT", "")                                 # 建桶
        c.put("a/1", b"one")
        urllib.request.urlopen(urllib.request.Request(c.presign("PUT", "a/2", 60), data=b"two", method="PUT",
                                                      headers={"content-type": "application/octet-stream"})).read()
        self.assertEqual(sorted(o.key for o in c.list("a/")), ["a/1", "a/2"])
        self.assertEqual(urllib.request.urlopen(c.presign("GET", "a/2", 60)).read(), b"two")
        c.delete("a/1")
        self.assertEqual([o.key for o in c.list("a/")], ["a/2"])
        # 完整的快照 → 保留 → 恢复
        root = tempfile.mkdtemp(prefix="ncs-moto-")
        self.addCleanup(shutil.rmtree, root, True)
        cfg = make_config(object_store="s3", s3_endpoint=f"http://{host}:{port}", s3_bucket="moto-bucket",
                          s3_access_key_id="testing", s3_secret_access_key="testing", snapshot_keep=2)
        mgr = SnapshotManager(cfg, DirLauncher(root), c, lambda m: None)
        for i in range(3):
            write(os.path.join(root, SID, "workspace", "f"), f"v{i}".encode())
            mgr.snapshot_now("u", SID)
            time.sleep(0.003)
        self.assertEqual(len(mgr.list("u", SID)), 2)
        shutil.rmtree(os.path.join(root, SID))
        mgr.restore_if_missing("u", SID)
        with open(os.path.join(root, SID, "workspace", "f"), "rb") as f:
            self.assertEqual(f.read(), b"v2")


# ---------------------------------------------------------------------- 配置


class ConfigTest(unittest.TestCase):
    def test_default_off_and_needs_nothing(self):
        cfg = make_config()
        self.assertEqual(cfg.object_store, "off")
        cfg.validate()

    def test_s3_requires_fields(self):
        for kw, msg in (({}, "NCS_S3_ENDPOINT"),
                        ({"s3_endpoint": "https://s3.example.com"}, "NCS_S3_BUCKET"),
                        ({"s3_endpoint": "http://s3.example.com", "s3_bucket": "b-1", "s3_access_key_id": "a",
                          "s3_secret_access_key": "s"}, "https"),
                        ({"s3_endpoint": "https://s3.example.com", "s3_bucket": "Bad_Bucket", "s3_access_key_id": "a",
                          "s3_secret_access_key": "s"}, "桶名"),
                        ({"s3_endpoint": "https://s3.example.com", "s3_bucket": "b-1", "s3_access_key_id": "a",
                          "s3_secret_access_key": "s", "snapshot_keep": 0}, "KEEP"),
                        ({"s3_endpoint": "https://s3.example.com", "s3_bucket": "b-1", "s3_access_key_id": "a",
                          "s3_secret_access_key": "s", "s3_prefix": "a/../b"}, "PREFIX")):
            with self.subTest(kw=kw), self.assertRaisesRegex(ValueError, msg):
                make_config(object_store="s3", **kw).validate()
        with self.assertRaisesRegex(ValueError, "off 或 s3"):
            make_config(object_store="gcs").validate()

    def test_from_env(self):
        env = {"NCS_OBJECT_STORE": "S3", "NCS_S3_ENDPOINT": "https://s3.example.com", "NCS_S3_REGION": "eu-x",
               "NCS_S3_BUCKET": "my-bucket", "NCS_S3_ACCESS_KEY_ID": "kid", "NCS_S3_SECRET_ACCESS_KEY": "sec",
               "NCS_S3_PREFIX": "ws", "NCS_SNAPSHOT_KEEP": "5", "NCS_SNAPSHOT_MAX_BYTES": "200Mi",
               "NCS_SNAPSHOT_IGNORE": "*.log, dist", "NCS_SNAPSHOT_ON_DELETE": "1",
               "NCS_SNAPSHOT_PURGE_ON_DELETE": "0"}
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            cfg = Config.from_env()
        finally:
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.assertEqual((cfg.object_store, cfg.s3_region, cfg.s3_bucket, cfg.s3_prefix, cfg.snapshot_keep),
                         ("s3", "eu-x", "my-bucket", "ws", 5))
        self.assertEqual(cfg.snapshot_max, 200 << 20)
        self.assertEqual(cfg.snapshot_ignore, ["*.log", "dist"])
        self.assertTrue(cfg.snapshot_on_delete)
        self.assertFalse(cfg.snapshot_purge_on_delete)
        d = make_config()
        self.assertEqual((d.snapshot_purge_on_delete, d.snapshot_on_delete), (True, False))   # 默认：删除即清理云端

    def test_k8s_launcher_is_refused(self):
        class K8sLike(Launcher):
            name = "k8s"
            start = stop = delete = is_alive = lambda *a: None
        fake = FakeS3()
        self.addCleanup(fake.close)
        with self.assertRaisesRegex(ValueError, "不支持工作区快照"):
            SnapshotManager(s3_config(fake), K8sLike(), fake.client())


# ---------------------------------------------------------------------- 打包 / 恢复


def write(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


class PackRestoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ncs-snap-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.src = os.path.join(self.tmp, "src")
        self.dst = os.path.join(self.tmp, "dst")
        self.archive = os.path.join(self.tmp, "a.tar.gz")

    def test_roundtrip_with_ignore_list(self):
        ws = os.path.join(self.src, "workspace")
        write(os.path.join(ws, "main.py"), b"print('hi')\n")
        write(os.path.join(ws, "pkg", "deep", "data.bin"), os.urandom(4096))
        write(os.path.join(ws, "node_modules", "left-pad", "index.js"))
        write(os.path.join(ws, "pkg", "__pycache__", "m.cpython-312.pyc"))
        write(os.path.join(ws, "stray.pyc"))
        write(os.path.join(ws, "build.log"))
        write(os.path.join(self.src, "home", "sessions", "s1.json"), b"{}")
        write(os.path.join(self.src, "home", ".cache", "big"), b"c" * 1000)
        os.chmod(os.path.join(ws, "main.py"), 0o755)
        stats = pack(self.src, self.archive, 1 << 20, list(DEFAULT_IGNORE) + ["*.log"])
        self.assertGreater(stats["files"], 0)
        extract(self.archive, self.dst, 1 << 20)
        got = sorted(os.path.relpath(os.path.join(r, f), self.dst).replace(os.sep, "/")
                     for r, _, fs in os.walk(self.dst) for f in fs)
        self.assertEqual(got, ["home/sessions/s1.json", "workspace/main.py", "workspace/pkg/deep/data.bin"])
        with open(os.path.join(self.dst, "workspace", "pkg", "deep", "data.bin"), "rb") as a, \
                open(os.path.join(ws, "pkg", "deep", "data.bin"), "rb") as b:
            self.assertEqual(a.read(), b.read())
        if POSIX:
            self.assertTrue(os.stat(os.path.join(self.dst, "workspace", "main.py")).st_mode & stat.S_IXUSR)
        self.assertFalse([n for n in os.listdir(self.dst) if n.startswith(".restore-")])

    @unittest.skipUnless(POSIX, "符号链接 / FIFO")
    def test_pack_never_follows_links_out_of_the_workspace(self):
        secret = os.path.join(self.tmp, "host-secret")
        write(secret, b"TOP-SECRET")
        ws = os.path.join(self.src, "workspace")
        write(os.path.join(ws, "real.txt"), b"ok")
        os.symlink("real.txt", os.path.join(ws, "inside-link"))
        os.symlink(secret, os.path.join(ws, "abs-link"))
        os.symlink("../../host-secret", os.path.join(ws, "rel-escape"))
        os.makedirs(os.path.join(self.tmp, "outdir"))
        write(os.path.join(self.tmp, "outdir", "x"), b"TOP-SECRET")
        os.symlink(os.path.join(self.tmp, "outdir"), os.path.join(ws, "dir-link"))
        os.mkfifo(os.path.join(ws, "fifo"))
        stats = pack(self.src, self.archive, 1 << 20)
        with tarfile.open(self.archive) as t:
            names = {m.name: m for m in t.getmembers()}
            blob = b"".join(t.extractfile(m).read() for m in t.getmembers() if m.isreg())
        self.assertNotIn(b"TOP-SECRET", blob)
        self.assertEqual(names["workspace/inside-link"].linkname, "real.txt")
        for n in ("workspace/abs-link", "workspace/rel-escape", "workspace/dir-link", "workspace/fifo"):
            self.assertNotIn(n, names)
        self.assertGreaterEqual(stats["skipped"], 4)
        extract(self.archive, self.dst, 1 << 20)
        with open(os.path.join(self.dst, "workspace", "inside-link"), "rb") as f:
            self.assertEqual(f.read(), b"ok")

    @unittest.skipUnless(POSIX, "符号链接")
    def test_pack_drops_chained_links_that_escape(self):
        os.makedirs(os.path.join(self.src, "home", "a", "b"))
        os.makedirs(os.path.join(self.src, "workspace"))
        os.symlink("../../../workspace", os.path.join(self.src, "home", "a", "b", "k"))
        os.symlink("../home/a/b/k/../../..", os.path.join(self.src, "workspace", "l"))
        pack(self.src, self.archive, 1 << 20)
        with tarfile.open(self.archive) as t:
            names = t.getnames()
        self.assertIn("home/a/b/k", names)
        self.assertNotIn("workspace/l", names)
        extract(self.archive, self.dst, 1 << 20)

    @unittest.skipUnless(POSIX, "符号链接")
    def test_workspace_itself_replaced_by_a_link_is_not_followed(self):
        os.makedirs(self.src)
        os.makedirs(os.path.join(self.tmp, "elsewhere"))
        write(os.path.join(self.tmp, "elsewhere", "secret"), b"TOP-SECRET")
        os.symlink(os.path.join(self.tmp, "elsewhere"), os.path.join(self.src, "workspace"))
        pack(self.src, self.archive, 1 << 20)
        with tarfile.open(self.archive) as t:
            self.assertEqual(t.getnames(), [])

    def test_size_cap(self):
        write(os.path.join(self.src, "workspace", "big"), b"b" * 5000)
        with self.assertRaises(SnapshotError) as cm:
            pack(self.src, self.archive, 4000)
        self.assertTrue(cm.exception.permanent)

    def test_refuses_to_overwrite_existing_workspace(self):
        write(os.path.join(self.src, "workspace", "a"), b"new")
        pack(self.src, self.archive, 1 << 20)
        write(os.path.join(self.dst, "workspace", "a"), b"local")
        with self.assertRaises(SnapshotError):
            extract(self.archive, self.dst, 1 << 20)
        with open(os.path.join(self.dst, "workspace", "a"), "rb") as f:
            self.assertEqual(f.read(), b"local")


class TarTraversalTest(unittest.TestCase):
    """恢复不可信归档：每一种越界都要整体拒绝，而且目标目录外、甚至目标目录里都不能留下任何东西。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ncs-evil-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "sandbox", "data")
        os.makedirs(self.root)

    def make(self, entries) -> str:
        path = os.path.join(self.tmp, f"evil-{len(os.listdir(self.tmp))}.tar.gz")
        with tarfile.open(path, "w:gz", format=tarfile.PAX_FORMAT) as t:
            for name, kind, extra in entries:
                ti = tarfile.TarInfo(name)
                ti.type = kind
                data = b""
                if kind == tarfile.REGTYPE:
                    data = extra if isinstance(extra, bytes) else b"pwned"
                    ti.size = len(data)
                elif kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                    ti.linkname = extra
                elif kind in (tarfile.CHRTYPE, tarfile.BLKTYPE):
                    ti.devmajor, ti.devminor = 1, 3
                t.addfile(ti, io.BytesIO(data) if kind == tarfile.REGTYPE else None)
        return path

    def assert_rejected(self, entries, max_bytes=1 << 20):
        archive = self.make(entries)
        before = set(os.listdir(self.tmp))
        with self.assertRaises(SnapshotError) as cm:
            extract(archive, self.root, max_bytes)
        self.assertTrue(cm.exception.permanent)
        self.assertEqual(set(os.listdir(self.tmp)), before)                  # 外面没多东西
        self.assertEqual(os.listdir(self.root), [])                          # 里面也没有（含临时目录）
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "pwned")))

    def test_absolute_path(self):
        self.assert_rejected([("/tmp/pwned", tarfile.REGTYPE, None)])

    def test_dotdot(self):
        self.assert_rejected([("../pwned", tarfile.REGTYPE, None)])
        self.assert_rejected([("workspace/../../pwned", tarfile.REGTYPE, None)])
        self.assert_rejected([("workspace/a/../../../../pwned", tarfile.REGTYPE, None)])

    def test_backslash_and_drive(self):
        self.assert_rejected([("workspace\\..\\..\\pwned", tarfile.REGTYPE, None)])
        self.assert_rejected([("C:/pwned", tarfile.REGTYPE, None)])

    def test_unknown_top_level(self):
        self.assert_rejected([("policy/policy.json", tarfile.REGTYPE, None)])

    def test_symlink_escapes(self):
        self.assert_rejected([("workspace/l", tarfile.SYMTYPE, "/etc/passwd")])
        self.assert_rejected([("workspace/l", tarfile.SYMTYPE, "../../..")])
        self.assert_rejected([("workspace/a/l", tarfile.SYMTYPE, "../../../pwned")])

    def test_write_through_symlink(self):
        # 链接本身指向工作区里面，但随后的文件想顺着链接写：一律拒绝
        self.assert_rejected([("workspace/sub", tarfile.DIRTYPE, None),
                              ("workspace/l", tarfile.SYMTYPE, "sub"),
                              ("workspace/l/x", tarfile.REGTYPE, None)])
        self.assert_rejected([("workspace/l/x", tarfile.REGTYPE, None),
                              ("workspace/l", tarfile.SYMTYPE, "home")])

    def test_chained_symlinks_escape(self):
        # PR #144 review 的反例：每个链接按字符串规范化都在根目录里，物理解析却走到根目录上两级
        self.assert_rejected([("home/a/b", tarfile.DIRTYPE, None),
                              ("home/a/b/k", tarfile.SYMTYPE, "../../../workspace"),
                              ("workspace/l", tarfile.SYMTYPE, "../home/a/b/k/../../..")])
        # 顺序反过来、嵌套更深也一样
        self.assert_rejected([("workspace/l", tarfile.SYMTYPE, "../home/x/y/z/k/../../../../.."),
                              ("home/x/y/z/k", tarfile.SYMTYPE, "../../../../workspace")])

    def test_symlink_loop(self):
        self.assert_rejected([("workspace/a", tarfile.SYMTYPE, "b"), ("workspace/b", tarfile.SYMTYPE, "a")])

    def test_link_to_root_itself(self):
        self.assert_rejected([("workspace/up", tarfile.SYMTYPE, "..")])

    def test_safe_chained_links_are_kept(self):
        archive = self.make([("workspace/sub", tarfile.DIRTYPE, None),
                             ("workspace/sub/f", tarfile.REGTYPE, b"ok"),
                             ("home/k", tarfile.SYMTYPE, "../workspace/sub"),
                             ("workspace/l", tarfile.SYMTYPE, "../home/k/f")])
        extract(archive, self.root, 1 << 20)
        if POSIX:
            with open(os.path.join(self.root, "workspace", "l"), "rb") as f:
                self.assertEqual(f.read(), b"ok")

    def test_member_count_cap_streaming(self):
        from nanocode_server.runtime import snapshot as mod
        old = mod.MAX_MEMBERS
        mod.MAX_MEMBERS = 5
        self.addCleanup(setattr, mod, "MAX_MEMBERS", old)
        self.assert_rejected([(f"workspace/e{i}", tarfile.DIRTYPE, None) for i in range(50)])

    def test_hardlink(self):
        self.assert_rejected([("workspace/h", tarfile.LNKTYPE, "/etc/shadow")])
        self.assert_rejected([("workspace/a", tarfile.REGTYPE, None), ("workspace/h", tarfile.LNKTYPE, "workspace/a")])

    def test_devices_and_fifos(self):
        self.assert_rejected([("workspace/null", tarfile.CHRTYPE, None)])
        self.assert_rejected([("workspace/sda", tarfile.BLKTYPE, None)])
        self.assert_rejected([("workspace/fifo", tarfile.FIFOTYPE, None)])

    def test_duplicates(self):
        self.assert_rejected([("workspace/a", tarfile.REGTYPE, None), ("workspace/./a", tarfile.REGTYPE, None)])

    def test_decompression_bomb(self):
        self.assert_rejected([("workspace/a", tarfile.REGTYPE, b"a" * 3000),
                              ("workspace/b", tarfile.REGTYPE, b"b" * 3000)], max_bytes=4000)

    def test_not_a_tarball(self):
        bad = os.path.join(self.tmp, "bad.tar.gz")
        write(bad, b"definitely not gzip")
        with self.assertRaises(SnapshotError):
            extract(bad, self.root, 1 << 20)
        self.assertEqual(os.listdir(self.root), [])

    def test_setuid_bits_are_dropped(self):
        archive = os.path.join(self.tmp, "suid.tar.gz")
        with tarfile.open(archive, "w:gz") as t:
            ti = tarfile.TarInfo("workspace/suid")
            ti.size, ti.mode = 1, 0o4755
            t.addfile(ti, io.BytesIO(b"x"))
        extract(archive, self.root, 1 << 20)
        if POSIX:
            self.assertFalse(os.stat(os.path.join(self.root, "workspace", "suid")).st_mode & stat.S_ISUID)

    def test_validate_members_accepts_safe_layout(self):
        ok = [tarfile.TarInfo(n) for n in ("workspace", "workspace/a", "home")]
        ok[0].type = ok[2].type = tarfile.DIRTYPE
        link = tarfile.TarInfo("workspace/l")
        link.type, link.linkname = tarfile.SYMTYPE, "../home"
        self.assertEqual(len(validate_members(ok + [link], 10)), 4)


# ---------------------------------------------------------------------- SnapshotManager


class DirLauncher(LocalSnapshots, Launcher):
    """只有工作区目录、没有进程的启动器：测快照逻辑用。"""
    name = "dirs"

    def __init__(self, root):
        self.root = root

    def snapshot_root(self, sid):
        return os.path.join(self.root, sid)

    def start(self, spec):
        return "http://unused"

    def stop(self, sid):
        pass

    def delete(self, sid):
        shutil.rmtree(self.snapshot_root(sid), ignore_errors=True)

    def is_alive(self, sid):
        return False


SID = "A" * 16


class ManagerTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeS3()
        self.addCleanup(self.fake.close)
        self.root = tempfile.mkdtemp(prefix="ncs-mgr-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.launcher = DirLauncher(self.root)
        self.logs = []
        self.mgr = SnapshotManager(s3_config(self.fake), self.launcher, self.fake.client(), self.logs.append,
                                   backoff=(0,))
        write(os.path.join(self.root, SID, "workspace", "f.txt"), b"v1")

    def test_key_layout_and_user_segment(self):
        info = self.mgr.snapshot_now("gh-123", SID)
        self.assertRegex(info["key"], NAME)
        self.assertTrue(info["key"].startswith(f"snapshots/gh-123/{SID}/"))
        self.assertEqual(safe_segment("gh-123"), "gh-123")
        self.assertNotEqual(safe_segment("a@b"), safe_segment("a_b"))
        self.assertRegex(safe_segment("../../x"), r"^[A-Za-z0-9._-]+$")
        self.assertNotIn("/", safe_segment("../../x"))

    def test_keeps_latest_n(self):
        keys = []
        for i in range(5):
            write(os.path.join(self.root, SID, "workspace", "f.txt"), f"v{i}".encode())
            keys.append(self.mgr.snapshot_now("u", SID)["key"])
            time.sleep(0.003)
        left = sorted(k for k in self.fake.objects if k.startswith("snapshots/u/"))
        self.assertEqual(left, sorted(keys)[-3:])
        self.assertEqual(self.mgr.stats()["pruned"], 2)

    def test_restore_only_when_missing_and_falls_back_to_older(self):
        self.mgr.snapshot_now("u", SID)
        self.assertIsNone(self.mgr.restore_if_missing("u", SID))          # 本地还在：不动
        time.sleep(0.003)
        write(os.path.join(self.root, SID, "workspace", "f.txt"), b"v2")
        newest = self.mgr.snapshot_now("u", SID)["key"]
        self.fake.objects[newest] = b"corrupted"                          # 最新那份坏了
        shutil.rmtree(os.path.join(self.root, SID))
        info = self.mgr.restore_if_missing("u", SID)
        self.assertNotEqual(info["key"], newest)
        with open(os.path.join(self.root, SID, "workspace", "f.txt"), "rb") as f:
            self.assertEqual(f.read(), b"v1")
        self.assertEqual(self.mgr.stats()["restored"], 1)

    def test_nothing_to_restore(self):
        self.assertIsNone(self.mgr.restore_if_missing("u", "B" * 16))

    def test_all_snapshots_broken_fails_loudly(self):
        key = self.mgr.snapshot_now("u", SID)["key"]
        self.fake.objects[key] = b"junk"
        shutil.rmtree(os.path.join(self.root, SID))
        with self.assertRaises(SnapshotError):
            self.mgr.restore_if_missing("u", SID)
        self.assertEqual(self.mgr.stats()["restore_failed"], 1)

    def test_upload_failures_retry_in_background_and_never_block(self):
        self.fake.fail_puts = 2
        t0 = time.monotonic()
        done = self.mgr.schedule("u", SID)
        self.assertLess(time.monotonic() - t0, 0.5)                         # 立即返回
        self.assertTrue(done.wait(20))
        s = self.mgr.stats()
        self.assertEqual((s["uploaded"], s["upload_retries"], s["upload_failed"]), (1, 2, 0))
        self.fake.fail_puts = 10
        self.assertTrue(self.mgr.schedule("u", SID).wait(20))
        self.assertEqual(self.mgr.stats()["upload_failed"], 1)
        self.assertTrue(any("upload failed" in m and "attempt=3/3" in m for m in self.logs))
        self.assertFalse(any("X-Amz-Signature" in m or SK in m for m in self.logs))   # 日志里没有签名和密钥

    def test_too_large_is_not_retried(self):
        self.mgr.max_bytes = 1
        self.assertTrue(self.mgr.schedule("u", SID).wait(20))
        s = self.mgr.stats()
        self.assertEqual((s["upload_failed"], s["upload_retries"]), (1, 0))

    def test_then_runs_after_snapshot_even_on_failure(self):
        self.fake.fail_puts = 10
        ran = []
        self.assertTrue(self.mgr.schedule("u", SID, then=lambda: ran.append(1)).wait(20))
        self.assertEqual(ran, [1])

    def test_manual_snapshots_have_own_slot_and_do_not_queue(self):
        from nanocode_server.services.snapshots import SnapshotBusy
        gate = threading.Event()
        orig = self.launcher.snapshot
        self.launcher.snapshot = lambda *a: (gate.wait(10), orig(*a))[1]
        t = threading.Thread(target=self.mgr.snapshot_now, args=("u", SID), kwargs={"manual": True})
        t.start()
        time.sleep(0.2)
        with self.assertRaises(SnapshotBusy):                               # 第二个手动快照：立即拒绝，不排队
            self.mgr.snapshot_now("u", SID, manual=True)
        self.launcher.snapshot = orig
        self.assertTrue(self.mgr.schedule("u", SID).wait(10))               # 后台快照不被手动快照挡住
        gate.set()
        t.join(10)
        self.assertEqual(self.mgr.stats()["manual_busy"], 1)

    def test_purge_deletes_all_snapshots_of_the_session(self):
        self.mgr.snapshot_now("u", SID)
        write(os.path.join(self.root, "C" * 16, "workspace", "x"))
        other = self.mgr.snapshot_now("u", "C" * 16)["key"]
        self.assertTrue(self.mgr.purge("u", SID))
        self.assertEqual(sorted(self.fake.objects), [other])

    def test_download_does_not_follow_redirects(self):
        from nanocode_server.runtime.snapshot import download
        from support import FakeServer
        target = FakeServer(lambda m, p, h, b: (200, {}, b"evil"))
        redirect = FakeServer(lambda m, p, h, b: (302, {"location": target.url + "/x", "content-length": "0"}, b""))
        self.addCleanup(target.close)
        self.addCleanup(redirect.close)
        with self.assertRaises(SnapshotError):
            download(redirect.url + "/snap", os.path.join(self.root, "dl"), 1 << 20)
        self.assertEqual(target.requests, [])

    def test_settle_waits_for_pending(self):
        gate = threading.Event()
        orig = self.launcher.snapshot
        self.launcher.snapshot = lambda *a: (gate.wait(5), orig(*a))[1]
        self.mgr.schedule("u", SID)
        self.assertFalse(self.mgr.settle(SID, timeout=0.2))
        gate.set()
        self.assertTrue(self.mgr.settle(SID, timeout=10))


# ---------------------------------------------------------------------- 端到端：单机（本机启动器）


class LocalEndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = FakeS3()
        cls.cfg = s3_config(cls.fake)
        cls.platform = Platform(cls.cfg, log=lambda m: None)
        cls.platform.snapshots.backoff = (0,)
        cls.platform.start()
        cls.port = cls.platform.control_port
        cls.launcher = cls.platform.launcher

    @classmethod
    def tearDownClass(cls):
        cls.platform.close()
        cls.fake.close()

    def objects(self, sid):
        return sorted(k for k in self.fake.objects if f"/{sid}/" in k)

    def test_stop_uploads_missing_workspace_is_restored_on_next_open(self):
        c = Client(self.port, "una")
        status, data, _ = c.request("POST", "/api/sessions", {"title": "t"})
        self.assertEqual(status, 201, data)
        sid = data["session"]["id"]
        ws = self.launcher.workspace(sid)
        write(os.path.join(ws, "notes.md"), b"keep me")
        write(os.path.join(ws, "node_modules", "x.js"), b"skip me")
        self.assertEqual(c.request("POST", f"/api/sessions/{sid}/stop", {})[0], 200)
        self.assertTrue(self.platform.snapshots.settle(sid, 30))
        keys = self.objects(sid)
        self.assertEqual(len(keys), 1)
        self.assertRegex(keys[0], NAME)
        # 本机工作区没了（换机器 / 磁盘清了）：再打开会话时从快照恢复
        shutil.rmtree(self.launcher.snapshot_root(sid))
        self.assertEqual(c.request("GET", f"/s/{sid}/api/state")[0], 200)
        with open(os.path.join(ws, "notes.md"), "rb") as f:
            self.assertEqual(f.read(), b"keep me")
        self.assertFalse(os.path.exists(os.path.join(ws, "node_modules")))
        # 策略文件和沙箱环境里都没有对象存储的密钥
        with open(os.path.join(self.launcher.snapshot_root(sid), "policy.json"), encoding="utf-8") as f:
            self.assertNotIn(SK, f.read())
        # 手动快照 + 列表
        status, data, _ = c.request("POST", f"/api/sessions/{sid}/snapshot", {})
        self.assertEqual(status, 201, data)
        self.assertNotIn("key", data["snapshot"])
        status, data, _ = c.request("GET", f"/api/sessions/{sid}/snapshots")
        self.assertEqual((status, len(data["snapshots"])), (200, 2))
        # 别人看不到、也拍不了
        other = Client(self.port, "vic")
        self.assertEqual(other.request("POST", f"/api/sessions/{sid}/snapshot", {})[0], 404)
        self.assertEqual(other.request("GET", f"/api/sessions/{sid}/snapshots")[0], 404)
        # 删除（默认 NCS_SNAPSHOT_PURGE_ON_DELETE=1）：云端快照和本地工作区都删掉
        self.assertEqual(c.request("POST", f"/api/sessions/{sid}/delete", {})[0], 200)
        self.assertTrue(self.platform.snapshots.settle(sid, 30))
        self.assertEqual(self.objects(sid), [])
        self.assertFalse(os.path.exists(self.launcher.snapshot_root(sid)))

    def test_delete_keeps_last_snapshot_when_purge_is_off(self):
        self.cfg.snapshot_purge_on_delete, self.cfg.snapshot_on_delete = False, True
        self.addCleanup(setattr, self.cfg, "snapshot_purge_on_delete", True)
        self.addCleanup(setattr, self.cfg, "snapshot_on_delete", False)
        c = Client(self.port, "zed")
        sid = c.request("POST", "/api/sessions", {"title": "t"})[1]["session"]["id"]
        write(os.path.join(self.launcher.workspace(sid), "a.txt"), b"a")
        self.assertEqual(c.request("POST", f"/api/sessions/{sid}/delete", {})[0], 200)
        self.assertTrue(self.platform.snapshots.settle(sid, 30))
        self.assertEqual(len(self.objects(sid)), 1)
        self.assertFalse(os.path.exists(self.launcher.snapshot_root(sid)))

    def test_idle_reaping_triggers_snapshot(self):
        c = Client(self.port, "ivy")
        sid = c.request("POST", "/api/sessions", {"title": "t"})[1]["session"]["id"]
        self.addCleanup(c.request, "POST", f"/api/sessions/{sid}/delete", {})
        write(os.path.join(self.launcher.workspace(sid), "idle.txt"), b"idle")
        reaped = self.platform.sessions.reap_once(now=time.time() + self.cfg.idle_timeout + 60)
        self.assertIn(sid, reaped)
        self.assertTrue(self.platform.snapshots.settle(sid, 30))
        self.assertEqual(len(self.objects(sid)), 1)

    def test_admin_stats_requires_admin(self):
        self.assertEqual(Client(self.port, "wes").request("GET", "/api/admin/snapshots")[0], 403)

    def test_spec_never_carries_object_store_credentials(self):
        seen = []
        orig = self.launcher.start
        self.launcher.start = lambda spec: (seen.append(spec), orig(spec))[1]
        self.addCleanup(setattr, self.launcher, "start", orig)
        c = Client(self.port, "xia")
        status, data, _ = c.request("POST", "/api/sessions", {"title": "t"})
        self.assertEqual(status, 201)
        self.addCleanup(c.request, "POST", f"/api/sessions/{data['session']['id']}/delete", {})
        blob = json.dumps([seen[0].env("/h", "/p"), seen[0].policy, seen[0].api_key])
        for secret in (SK, AK, BUCKET, self.fake.endpoint):
            self.assertNotIn(secret, blob)


class DisabledTest(unittest.TestCase):
    def test_off_by_default_endpoints(self):
        platform = Platform(make_config(), log=lambda m: None)
        platform.start()
        self.addCleanup(platform.close)
        self.assertIsNone(platform.snapshots)
        c = Client(platform.control_port, "yan")
        status, data, _ = c.request("POST", "/api/sessions", {"title": "t"})
        sid = data["session"]["id"]
        self.addCleanup(c.request, "POST", f"/api/sessions/{sid}/delete", {})
        self.assertEqual(c.request("POST", f"/api/sessions/{sid}/snapshot", {})[0], 404)
        status, data, _ = c.request("GET", f"/api/sessions/{sid}/snapshots")
        self.assertEqual((status, data["enabled"], data["snapshots"]), (200, False, []))


# ---------------------------------------------------------------------- 远程沙箱：沙箱代理打包上传


AGENT_SECRET = "agent-secret-" + "y" * 32


class RemoteAgentSnapshotTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = FakeS3()
        agent_cfg = Config(data_dir=tempfile.mkdtemp(prefix="ncs-agent-"), remote_agent_secret=AGENT_SECRET,
                           gateway_port=0, agent_upstream_gateway="127.0.0.1:9")
        cls.host_launcher = LocalLauncher(agent_cfg.data_dir)            # 沙箱主机上的启动器
        cls.agent, cls.relay = build_agent(agent_cfg, launcher=cls.host_launcher, log=lambda m: None)
        cls.server = make_server(cls.agent, "127.0.0.1", 0)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.remote = RemoteLauncher(f"http://127.0.0.1:{cls.server.server_address[1]}", AGENT_SECRET)
        cls.mgr = SnapshotManager(s3_config(cls.fake), cls.remote, cls.fake.client(), lambda m: None, backoff=(0,))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.relay.close()
        cls.fake.close()

    def test_agent_uploads_and_restores_with_presigned_urls_only(self):
        sid = "R" * 16
        write(os.path.join(self.host_launcher.workspace(sid), "app.py"), b"remote")
        self.assertTrue(self.remote.has_workspace(sid))
        info = self.mgr.snapshot_now("gh-9", sid)
        self.assertIn(info["key"], self.fake.objects)
        # 只有预签名请求来自代理那一侧；带签名头（Authorization）的只有控制面的列表 / 删除
        puts = [r for r in self.fake.requests if r[0] == "PUT"]
        self.assertTrue(all("X-Amz-Signature" in q for _, _, q in puts))
        shutil.rmtree(self.host_launcher.snapshot_root(sid))
        self.assertFalse(self.remote.has_workspace(sid))
        self.mgr.restore_if_missing("gh-9", sid)
        with open(os.path.join(self.host_launcher.workspace(sid), "app.py"), "rb") as f:
            self.assertEqual(f.read(), b"remote")

    def test_agent_rejects_bad_requests(self):
        from test_remote_sandbox import call
        base = self.remote.agent_url
        sid = "Q" * 16
        self.assertEqual(call(f"{base}/v1/sandboxes/{sid}/snapshot", "POST", {"url": "file:///etc/passwd",
                                                                               "max_bytes": 10, "ignore": []},
                              token=AGENT_SECRET)[0], 400)
        self.assertEqual(call(f"{base}/v1/sandboxes/{sid}/snapshot", "POST", {"url": "https://x", "max_bytes": 10,
                                                                               "ignore": []}, token="wrong")[0], 401)
        self.assertEqual(call(f"{base}/v1/sandboxes/{sid}/restore", "POST", {"url": "https://x", "max_bytes": -1},
                              token=AGENT_SECRET)[0], 400)

    def test_permanent_errors_map_to_422(self):
        sid = "P" * 16
        write(os.path.join(self.host_launcher.workspace(sid), "big"), b"b" * 5000)
        with self.assertRaises(SnapshotError) as cm:
            self.remote.snapshot(sid, self.fake.client().presign("PUT", "x/y", 60), 1000, [])
        self.assertTrue(cm.exception.permanent)


if __name__ == "__main__":
    unittest.main()
