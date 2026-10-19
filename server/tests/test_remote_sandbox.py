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

    def test_connection_cap(self):
        upstream = socket.create_server(("127.0.0.1", 0))
        relay = TcpRelay("127.0.0.1", 0, upstream.getsockname()[:2], max_connections=1).start()
        try:
            a = socket.create_connection(relay.address, timeout=5)
            time.sleep(0.2)
            b = socket.create_connection(relay.address, timeout=5)
            try:
                closed = b.recv(10) == b""                    # 超过上限：直接断开
            except ConnectionResetError:                       # Windows 上是 RST
                closed = True
            self.assertTrue(closed)
            a.close()
            b.close()
        finally:
            relay.close()
            upstream.close()


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
        for bad in ({"agent_bind": "0.0.0.0:8790"}, {"agent_bind": "[::]:8790"}, {"agent_bind": "10.77.0.2"},
                    {"agent_bind": ""}, {"remote_agent_secret": "x" * 31}, {"agent_upstream_gateway": "10.77.0.1"}):
            with self.subTest(bad=bad):
                cfg = Config(**{**ok.__dict__, **bad})
                with self.assertRaises(ValueError):
                    cfg.validate_agent()

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
