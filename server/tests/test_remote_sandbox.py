"""远程沙箱（NCS_LAUNCHER=remote）：控制面 → 沙箱代理 → 沙箱，沙箱 → 代理的网关中继 → 模型网关。

端到端用真的控制面、模型网关和沙箱代理（代理里用本机启动器代替 Docker），全部在 127.0.0.1 上。
"""

import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

import support  # noqa: F401

from nanocode_server.config import Config, parse_size, split_hostport
from nanocode_server.demo import FakeModel
from nanocode_server.platform import Platform
from nanocode_server.runtime import DockerLauncher, LaunchError, LocalLauncher, RemoteLauncher, SandboxSpec
from nanocode_server.sandbox_agent import SandboxAgent, TcpRelay, build_agent
from nanocode_server.web.framework import make_server
from support import Client, make_config

SECRET = "agent-secret-" + "x" * 32


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def call(url, method="GET", body=None, token=SECRET, headers=None):
    h = {"content-type": "application/json", **(headers or {})}
    if token:
        h["authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


class RemoteEndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = FakeModel()
        gateway_port = free_port()
        # 沙箱主机：代理 + 网关中继（中继转到控制面那边的网关）
        agent_cfg = Config(data_dir=tempfile.mkdtemp(prefix="ncs-agent-"), remote_agent_secret=SECRET,
                           gateway_port=0, agent_upstream_gateway=f"127.0.0.1:{gateway_port}")
        cls.sandboxes = LocalLauncher(agent_cfg.data_dir)
        cls.agent, cls.relay = build_agent(agent_cfg, launcher=cls.sandboxes, log=lambda m: None)
        cls.agent_server = make_server(cls.agent, "127.0.0.1", 0)
        threading.Thread(target=cls.agent_server.serve_forever, daemon=True).start()
        cls.agent_url = f"http://127.0.0.1:{cls.agent_server.server_address[1]}"
        # 平台：控制面 + 网关，沙箱交给远程启动器
        cls.cfg = make_config(allow_private_upstreams=True, gateway_port=gateway_port, max_sessions_per_user=3,
                              launcher="remote", remote_agent_url=cls.agent_url, remote_agent_secret=SECRET)
        cls.launcher = RemoteLauncher(cls.agent_url, SECRET)
        cls.platform = Platform(cls.cfg, launcher=cls.launcher, log=lambda m: None)
        cls.platform.start()
        cls.port = cls.platform.control_port

    @classmethod
    def tearDownClass(cls):
        cls.platform.close()
        cls.agent_server.shutdown()
        cls.agent_server.server_close()
        cls.relay.close()
        cls.sandboxes.shutdown()
        cls.model.close()

    def create(self, c):
        status, data, _ = c.request("POST", "/api/sessions", {"title": "t"})
        self.assertEqual(status, 201, data)
        sid = data["session"]["id"]
        self.addCleanup(c.request, "POST", f"/api/sessions/{sid}/delete", {})
        return sid

    def test_sandbox_gateway_is_the_relay_control_plane_talks_to_gateway_directly(self):
        self.assertEqual(self.platform.sessions.gateway_url, self.agent.gateway_url)
        self.assertEqual(self.platform.sessions.control_gateway_url, f"http://127.0.0.1:{self.cfg.gateway_port}")

    def test_full_flow_through_agent(self):
        c = Client(self.port, "rita")
        sid = self.create(c)
        status, page, _ = c.request("GET", f"/s/{sid}/?embed=1")
        self.assertEqual(status, 200)
        self.assertEqual(c.request("GET", f"/s/{sid}/api/state")[1]["provider"], "mock")
        self.assertTrue(self.sandboxes.is_alive(sid))                       # 沙箱真的跑在"沙箱主机"上
        self.assertTrue(self.launcher.is_alive(sid))
        c.request("POST", f"/s/{sid}/api/chat", {"message": "看看这个项目"})
        events = c.events_until(sid, "turn_end")                             # 事件流经两跳转发
        self.assertTrue(any(e["type"] == "permission_answer" for e in events))

    def test_model_calls_go_through_relay_with_users_key(self):
        c = Client(self.port, "sam")
        status, data, _ = c.request("POST", "/api/credentials/test", {   # 控制面直连网关测试 Key
            "provider": "custom", "base_url": self.model.url, "api_key": "sk-sam-123456", "models": "fake-model"})
        self.assertEqual((status, data.get("ok")), (200, True), data)
        status, data, _ = c.request("POST", "/api/credentials", {"provider": "custom", "base_url": self.model.url,
                                                                 "api_key": "sk-sam-123456", "models": "fake-model"})
        self.assertEqual(status, 201, data)
        ref = f"c:{data['credential']['id']}:fake-model"
        sid = self.create(c)
        self.assertEqual(c.request("POST", f"/api/sessions/{sid}/model", {"model": ref})[0], 200)
        calls = len(self.model.calls)
        c.request("POST", f"/s/{sid}/api/chat", {"message": "暗号：r1"})
        c.events_until(sid, "turn_end")
        self.assertEqual({k["key"] for k in self.model.calls[calls:]}, {"sk-sam-123456"})

    def test_sandbox_killed_on_host_is_restarted(self):
        c = Client(self.port, "tom")
        sid = self.create(c)
        self.assertEqual(c.request("GET", f"/s/{sid}/api/state")[0], 200)
        self.sandboxes.stop(sid)                                            # 在沙箱主机上直接杀掉
        self.assertEqual(c.request("GET", f"/s/{sid}/api/state")[0], 502)
        self.assertEqual(c.request("GET", f"/s/{sid}/api/state")[0], 200)

    def test_stop_and_delete_reach_the_agent(self):
        c = Client(self.port, "uma")
        sid = self.create(c)
        c.request("GET", f"/s/{sid}/api/state")
        self.assertEqual(c.request("POST", f"/api/sessions/{sid}/stop", {})[0], 200)
        self.assertFalse(self.sandboxes.is_alive(sid))

    # ------------------------------------------------------------------ 代理本身的安全

    def test_management_api_needs_secret(self):
        for token in ("", "wrong", SECRET[:-1], SECRET + "x"):
            with self.subTest(token=token):
                self.assertEqual(call(self.agent_url + "/v1/info", token=token)[0], 401)
                self.assertEqual(call(self.agent_url + f"/v1/sandboxes/{'a' * 16}/stop", "POST", token=token)[0], 401)
        status, info = call(self.agent_url + "/v1/info")
        self.assertEqual((status, info["gateway_url"]), (200, self.agent.gateway_url))
        self.assertEqual(call(self.agent_url + "/healthz", token="")[0], 200)

    def test_proxy_needs_the_sandboxs_own_token(self):
        c = Client(self.port, "vic")
        sid = self.create(c)
        c.request("GET", f"/s/{sid}/api/state")
        url = f"{self.agent_url}/sbx/{sid}/api/state"
        self.assertEqual(call(url, token="", headers={"x-nanocode-token": "w" * 32})[0], 403)
        self.assertEqual(call(url, token="")[0], 403)
        self.assertEqual(call(url, token=SECRET)[0], 403)                   # 代理密钥也不行：只认沙箱令牌
        self.assertEqual(call(f"{self.agent_url}/sbx/..%2f..%2fetc/api/state", token="")[0], 404)
        self.assertEqual(call(f"{self.agent_url}/sbx/{'z' * 16}/api/state", token="")[0], 403)

    def test_bad_requests_are_rejected(self):
        self.assertEqual(call(self.agent_url + "/v1/sandboxes/short/start", "POST", {"web_token": "w" * 32,
                                                                                     "policy": {}})[0], 404)
        self.assertEqual(call(self.agent_url + f"/v1/sandboxes/{'b' * 16}/start", "POST", {"policy": {}})[0], 400)
        self.assertEqual(call(self.agent_url + "/v1/reconcile", "POST", {"keep": "all"})[0], 400)


class RemoteLauncherErrorsTest(unittest.TestCase):
    def test_agent_down(self):
        r = RemoteLauncher(f"http://127.0.0.1:{free_port()}", SECRET, timeout=2)
        self.assertFalse(r.is_alive("a" * 16))
        with self.assertRaisesRegex(LaunchError, "连不上沙箱代理"):
            r.start(SandboxSpec("a" * 16, "u", "w" * 32, "", {}))
        with self.assertRaisesRegex(LaunchError, "NCS_REMOTE_GATEWAY_URL"):
            r.gateway_url_for_sandbox(8701)
        r.reconcile(set())          # 代理不在不能让控制面起不来
        r.shutdown()

    def test_explicit_gateway_url_skips_agent(self):
        r = RemoteLauncher("http://127.0.0.1:1", SECRET, gateway_url="http://172.30.250.1:8701/")
        self.assertEqual(r.gateway_url_for_sandbox(8701), "http://172.30.250.1:8701")

    def test_launch_error_from_agent_is_reported(self):
        class Failing(LocalLauncher):
            def start(self, spec):
                raise LaunchError("镜像不存在")
        relay = TcpRelay("127.0.0.1", 0, ("127.0.0.1", free_port()))
        agent = SandboxAgent(Failing(tempfile.mkdtemp()), SECRET, "http://127.0.0.1:1")
        server = make_server(agent, "127.0.0.1", 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            r = RemoteLauncher(f"http://127.0.0.1:{server.server_address[1]}", SECRET)
            with self.assertRaisesRegex(LaunchError, "镜像不存在"):
                r.start(SandboxSpec("c" * 16, "u", "w" * 32, "", {"mode": "default"}))
        finally:
            server.shutdown()
            server.server_close()
            relay.close()

    def test_agent_refuses_short_secret(self):
        with self.assertRaises(ValueError):
            SandboxAgent(LocalLauncher(tempfile.mkdtemp()), "short", "http://x")


class RelayTest(unittest.TestCase):
    def test_bytes_flow_both_ways_and_eof_propagates(self):
        upstream = socket.create_server(("127.0.0.1", 0))
        got = []

        def serve():
            conn, _ = upstream.accept()
            data = b""
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
            got.append(data)
            conn.sendall(data.upper())
            conn.close()
        threading.Thread(target=serve, daemon=True).start()
        relay = TcpRelay("127.0.0.1", 0, upstream.getsockname()[:2]).start()
        try:
            c = socket.create_connection(relay.address, timeout=5)
            payload = b"hello " * 50000
            c.sendall(payload)
            c.shutdown(socket.SHUT_WR)
            back = b""
            while True:
                chunk = c.recv(65536)
                if not chunk:
                    break
                back += chunk
            c.close()
            self.assertEqual((got[0], back), (payload, payload.upper()))
        finally:
            relay.close()
            upstream.close()

    @staticmethod
    def closed_by_peer(sock, timeout=5.0) -> bool:
        sock.settimeout(timeout)
        try:
            return sock.recv(10) == b""
        except ConnectionResetError:                           # Windows 上是 RST
            return True
        except socket.timeout:
            return False

    @staticmethod
    def echo_server():
        """回显上游：每条连接读到什么回什么，直到对方关闭。可以挂住不读（hang=True）。"""
        srv = socket.create_server(("127.0.0.1", 0))

        def handle(conn):
            try:
                while True:
                    data = conn.recv(65536)
                    if not data:
                        break
                    conn.sendall(data)
            except OSError:
                pass
            finally:
                conn.close()

        def loop():
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                threading.Thread(target=handle, args=(conn,), daemon=True).start()
        threading.Thread(target=loop, daemon=True).start()
        return srv

    def roundtrip(self, address, payload=b"ping"):
        c = socket.create_connection(address, timeout=5)
        try:
            c.sendall(payload)
            return c.recv(65536)
        finally:
            c.close()

    def test_connection_cap(self):
        upstream = self.echo_server()
        relay = TcpRelay("127.0.0.1", 0, upstream.getsockname()[:2], max_connections=1, per_source=1).start()
        try:
            a = socket.create_connection(relay.address, timeout=5)
            time.sleep(0.2)
            b = socket.create_connection(relay.address, timeout=5)
            self.assertTrue(self.closed_by_peer(b))                # 超过上限：直接断开
            a.close()
            b.close()
        finally:
            relay.close()
            upstream.close()

    def test_idle_connections_do_not_block_new_ones(self):
        """回归（#126 审查）：空闲连接占着名额不放，别的沙箱就调不了模型。空闲超时后名额要还回来。"""
        upstream = self.echo_server()
        relay = TcpRelay("127.0.0.1", 0, upstream.getsockname()[:2], max_connections=2, per_source=2,
                         idle_timeout=0.5).start()
        try:
            idle = [socket.create_connection(relay.address, timeout=5) for _ in range(2)]
            time.sleep(0.2)
            self.assertEqual(relay.active(), 2)
            blocked = socket.create_connection(relay.address, timeout=5)
            self.assertTrue(self.closed_by_peer(blocked))          # 名额满了
            blocked.close()
            for c in idle:
                self.assertTrue(self.closed_by_peer(c))            # 空闲超时被断开
                c.close()
            deadline = time.time() + 5
            while relay.active() and time.time() < deadline:
                time.sleep(0.05)
            self.assertEqual(relay.active(), 0)
            self.assertEqual(self.roundtrip(relay.address), b"ping")   # 新连接能用了
        finally:
            relay.close()
            upstream.close()

    def test_active_connection_is_not_cut_by_idle_timeout(self):
        upstream = self.echo_server()
        relay = TcpRelay("127.0.0.1", 0, upstream.getsockname()[:2], idle_timeout=0.6).start()
        try:
            c = socket.create_connection(relay.address, timeout=5)
            for _ in range(5):                                     # 总时长超过空闲超时，但一直有数据
                time.sleep(0.3)
                c.sendall(b"x")
                self.assertEqual(c.recv(10), b"x")
            c.close()
        finally:
            relay.close()
            upstream.close()

    def test_one_sandbox_cannot_take_all_slots(self):
        upstream = self.echo_server()
        relay = TcpRelay("127.0.0.1", 0, upstream.getsockname()[:2], max_connections=10, per_source=2).start()
        try:
            held = [socket.create_connection(relay.address, timeout=5) for _ in range(2)]
            time.sleep(0.2)
            third = socket.create_connection(relay.address, timeout=5)
            self.assertTrue(self.closed_by_peer(third))            # 同一来源超过 per_source
            third.close()
            self.assertEqual(relay.active(), 2)
            for c in held:
                c.close()
        finally:
            relay.close()
            upstream.close()

    def test_half_close_is_bounded(self):
        """一头发了 EOF、另一头既不回也不关：最多等 half_close_timeout。"""
        silent = socket.create_server(("127.0.0.1", 0))
        conns = []
        threading.Thread(target=lambda: conns.append(silent.accept()[0]), daemon=True).start()
        relay = TcpRelay("127.0.0.1", 0, silent.getsockname()[:2], idle_timeout=30, half_close_timeout=0.5).start()
        try:
            c = socket.create_connection(relay.address, timeout=5)
            c.shutdown(socket.SHUT_WR)
            started = time.time()
            self.assertTrue(self.closed_by_peer(c, timeout=5))
            self.assertLess(time.time() - started, 4)
            c.close()
        finally:
            relay.close()
            silent.close()
            for x in conns:
                x.close()


class AgentLauncherFailureTest(unittest.TestCase):
    def agent(self, launcher):
        agent = SandboxAgent(launcher, SECRET, "http://127.0.0.1:1")
        server = make_server(agent, "127.0.0.1", 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return agent, f"http://127.0.0.1:{server.server_address[1]}"

    def test_stop_delete_failures_are_502_with_reason(self):
        class Broken(LocalLauncher):
            def stop(self, sid):
                raise LaunchError("连不上 Docker（/var/run/docker.sock）")

            def delete(self, sid):
                raise LaunchError("连不上 Docker（/var/run/docker.sock）")

            def is_alive(self, sid):
                raise LaunchError("连不上 Docker")
        _, url = self.agent(Broken(tempfile.mkdtemp()))
        for path, method in ((f"/v1/sandboxes/{'a' * 16}/stop", "POST"), (f"/v1/sandboxes/{'a' * 16}/delete", "POST"),
                             (f"/v1/sandboxes/{'a' * 16}", "GET")):
            with self.subTest(path=path):
                status, body = call(url + path, method)
                self.assertEqual(status, 502)
                self.assertIn("Docker", body.get("error", ""))

    def test_rediscovered_address_is_cached(self):
        """代理重启后按容器找回沙箱地址，找回一次就缓存，不用每个请求都查 Docker。"""
        sandbox = FakeSandbox()
        lookups = []

        class Rediscover(LocalLauncher):
            def endpoint_of(self, sid):
                lookups.append(sid)
                return sandbox.url
        _, url = self.agent(Rediscover(tempfile.mkdtemp()))
        try:
            for _ in range(3):
                self.assertEqual(call(f"{url}/sbx/{'d' * 16}/api/state", token="",
                                      headers={"x-nanocode-token": "t" * 32})[0], 200)
            self.assertEqual(lookups, ["d" * 16])
            # 令牌仍由沙箱自己核对：错的令牌被沙箱拒绝 → 代理回 403 并丢掉缓存
            self.assertEqual(call(f"{url}/sbx/{'d' * 16}/api/state", token="",
                                  headers={"x-nanocode-token": "bad"})[0], 403)
            call(f"{url}/sbx/{'d' * 16}/api/state", token="", headers={"x-nanocode-token": "t" * 32})
            self.assertEqual(len(lookups), 2)
        finally:
            sandbox.close()


class FakeSandbox:
    """只认令牌 ttt… 的假沙箱。"""

    def __init__(self):
        from http.server import BaseHTTPRequestHandler

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                ok = self.headers.get("x-nanocode-token") == "t" * 32
                body = b'{"ok": true}' if ok else b"{}"
                self.send_response(200 if ok else 403)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        self.server = support.LocalHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class RemoteConfigTest(unittest.TestCase):
    def test_remote_launcher_config(self):
        ok = dict(launcher="remote", remote_agent_url="http://10.77.0.2:8790", remote_agent_secret=SECRET)
        make_config(**ok).validate()
        make_config(**ok, remote_gateway_url="http://172.30.250.1:8701").validate()
        for bad in ({"remote_agent_url": ""}, {"remote_agent_url": "ftp://x"}, {"remote_agent_url": "http://h/path"},
                    {"remote_agent_secret": "short"}, {"remote_gateway_url": "10.0.0.1:8701"}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    make_config(**{**ok, **bad}).validate()
        make_config(**{**ok, "remote_agent_secret": ""}).validate(launcher_needed=False)   # 只跑网关的角色不管

    def test_agent_config(self):
        ok = Config(remote_agent_secret=SECRET, agent_bind="10.77.0.2:8790", agent_upstream_gateway="10.77.0.1:8701")
        ok.validate_agent()
        for bad in ({"agent_relay_per_sandbox": 0}, {"agent_relay_per_sandbox": 200}, {"agent_relay_idle_timeout": 1},
                    {"agent_bind": "0.0.0.0:8790"}, {"agent_bind": "[::]:8790"}, {"agent_bind": "10.77.0.2"},
                    {"agent_bind": ""}, {"remote_agent_secret": "x" * 31}, {"agent_upstream_gateway": "10.77.0.1"},
                    # 中继监听地址和代理一样，不能开在所有地址上（#129）：配错会把网关中继暴露到公网
                    {"agent_relay_bind": "0.0.0.0"}, {"agent_relay_bind": "::"},
                    {"agent_relay_bind": "*"}, {"agent_relay_bind": "[::]"}):
            with self.subTest(bad=bad):
                cfg = Config(**{**ok.__dict__, **bad})
                with self.assertRaises(ValueError):
                    cfg.validate_agent()
        for good in ({"agent_relay_bind": ""}, {"agent_relay_bind": "10.77.0.2"}):  # 空 = 沙箱网桥地址，照常允许
            with self.subTest(good=good):
                Config(**{**ok.__dict__, **good}).validate_agent()

    def test_split_hostport(self):
        self.assertEqual(split_hostport("10.77.0.1:8701"), ("10.77.0.1", 8701))
        self.assertEqual(split_hostport("http://10.77.0.1:8701"), ("10.77.0.1", 8701))
        self.assertEqual(split_hostport("[fd00::1]:8701"), ("fd00::1", 8701))
        for bad in ("10.77.0.1", ":8701", "h:0", "h:70000", "http://h"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                split_hostport(bad)

    def test_from_env(self):
        env = {"NCS_LAUNCHER": "remote", "NCS_REMOTE_AGENT_URL": "http://10.77.0.2:8790",
               "NCS_REMOTE_AGENT_SECRET": SECRET, "NCS_REMOTE_GATEWAY_URL": "http://172.30.250.1:8701",
               "NCS_AGENT_BIND": "10.77.0.2:8790", "NCS_AGENT_UPSTREAM_GATEWAY": "10.77.0.1:8701"}
        from unittest import mock
        with mock.patch.dict(os.environ, env):
            cfg = Config.from_env()
        self.assertEqual((cfg.launcher, cfg.remote_agent_url, cfg.remote_gateway_url, cfg.agent_bind,
                          cfg.agent_upstream_gateway), ("remote", "http://10.77.0.2:8790", "http://172.30.250.1:8701",
                                                        "10.77.0.2:8790", "10.77.0.1:8701"))


class ResourceKnobsTest(unittest.TestCase):
    """#119：沙箱大小写法、/tmp 和进程数可调。"""

    def test_parse_size(self):
        for text, want in (("384Mi", 384 * 2**20), ("384m", 384 * 2**20), ("384M", 384 * 2**20), ("1g", 2**30),
                           ("1Gi", 2**30), ("512k", 512 * 2**10), ("1.5Gi", 3 * 2**29), ("1048576", 2**20)):
            with self.subTest(text=text):
                self.assertEqual(parse_size(text), want)
        for bad in ("", "lots", "1T", "-1m"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_size(bad)

    def test_tmpfs_and_pids(self):
        d = DockerLauncher.__new__(DockerLauncher)
        d.image, d.network, d.runtime, d.cpu, d.memory = "img", "ncs", "", "1", "384Mi"
        spec = SandboxSpec("0123456789abcdef", "a", "w" * 32, "", {})
        h = d.container_config(spec, "/d", "/p")["HostConfig"]
        self.assertEqual((h["Tmpfs"]["/tmp"], h["PidsLimit"]), (f"rw,size={192 * 2**20}", 512))   # 默认内存一半
        d.memory = "2Gi"
        self.assertEqual(d.container_config(spec, "/d", "/p")["HostConfig"]["Tmpfs"]["/tmp"], f"rw,size={512 * 2**20}")
        d.tmpfs, d.pids_limit = "96m", 128
        h = d.container_config(spec, "/d", "/p")["HostConfig"]
        self.assertEqual((h["Tmpfs"]["/tmp"], h["PidsLimit"]), (f"rw,size={96 * 2**20}", 128))

    def test_config_validation(self):
        make_config(sandbox_memory="384m", sandbox_tmpfs="96m").validate()
        for bad in ({"sandbox_memory": "lots"}, {"sandbox_tmpfs": "x"}, {"sandbox_pids": 4}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                make_config(**bad).validate()


class SandboxHostComposeTest(unittest.TestCase):
    """deploy/sandbox-host 的 compose 和 .env.example 与代码一致。"""

    def test_variables_are_known_and_used(self):
        import re
        repo = support.REPO

        def read(*parts):
            with open(os.path.join(*parts), encoding="utf-8") as f:
                return f.read()
        known = set(re.findall(r'"(NCS_[A-Z0-9_]+)"', read(repo, "server", "nanocode_server", "config.py")))
        d = os.path.join(repo, "deploy", "sandbox-host")
        compose = read(d, "docker-compose.yml")
        env = dict(re.findall(r"^([A-Z0-9_]+)=(.*)$", read(d, ".env.example"), re.M))
        used = set(re.findall(r"\b(NCS_[A-Z0-9_]+)", compose)) | set(env)
        self.assertEqual(used - known - {"NCS_HOST_DATA_DIR", "NCS_AGENT_MEM_LIMIT"}, set())
        self.assertEqual(set(env) - set(re.findall(r"\$\{([A-Z0-9_]+)", compose)), set())
        self.assertIn('command: ["sandbox-agent"]', compose)
        self.assertNotIn("0.0.0.0", env.get("NCS_AGENT_BIND", ""))
        self.assertNotIn("ports:", compose)                   # 不发布端口：代理只听 WireGuard 地址


if __name__ == "__main__":
    unittest.main()
