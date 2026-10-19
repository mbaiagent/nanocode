"""出站代理（egress.py）：只放 HTTP/HTTPS、DNS 由代理解析、拦内网（SSRF）、防 DNS 重绑定、名额和时限、日志不带敏感内容。

全部在 127.0.0.1 上：测试代理用 allow_networks 放行 127.0.0.1/32（代替"公网"），resolver 注入假的 DNS。
"""

import os
import socket
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler

import support  # noqa: F401

from nanocode_server.config import Config
from nanocode_server.egress import EgressProxy, proxy_env
from nanocode_server.runtime import DockerLauncher, LocalLauncher, SandboxSpec
from nanocode_server.sandbox_agent import build_agent
from support import LocalHTTPServer

SECRET = "agent-secret-" + "x" * 32


class Echo(BaseHTTPRequestHandler):
    def do_GET(self):
        body = (f"path={self.path}\nhost={self.headers.get('Host')}\n" +
                "".join(f"h:{k.lower()}\n" for k in self.headers.keys())).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


FAKE_DNS = {"public.test": ["127.0.0.1"], "internal.test": ["10.0.0.5"], "meta.test": ["169.254.169.254"],
            "mixed.test": ["127.0.0.1", "169.254.169.254"], "v6local.test": ["::1"], "ula.test": ["fd00::1"],
            "mapped.test": ["::ffff:10.0.0.1"], "cgnat.test": ["100.64.1.1"], "a.allowed.test": ["127.0.0.1"],
            "bad.allowed.test": ["127.0.0.1"]}


def fake_resolver(host, port):
    if host in FAKE_DNS:
        return FAKE_DNS[host]
    if host.replace(".", "").isdigit() or ":" in host:
        return [host]
    raise socket.gaierror("no such host")


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = LocalHTTPServer(("127.0.0.1", 0), Echo)
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()
        cls.up_port = cls.upstream.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.upstream.shutdown()
        cls.upstream.server_close()

    def proxy(self, **kw):
        self.logs = []
        opts = dict(allowed_ports=(80, 443, self.up_port), resolver=fake_resolver, allow_networks=["127.0.0.1/32"],
                    local_addresses=lambda: set(), log=self.logs.append)
        opts.update(kw)
        p = EgressProxy("127.0.0.1", 0, **opts).start()
        self.addCleanup(p.close)
        return p

    def raw(self, p, data: bytes, read=True, timeout=5) -> bytes:
        s = socket.create_connection(p.address, timeout=timeout)
        try:
            try:
                s.sendall(data)
            except ConnectionError:                     # 被拒的连接可能已经关了（Windows 上是 10053）
                return b""
            if not read:
                return b""
            out = b""
            while True:
                try:
                    chunk = s.recv(65536)
                except (socket.timeout, ConnectionError):
                    break
                if not chunk:
                    break
                out += chunk
            return out
        finally:
            s.close()

    def connect(self, p, target: str):
        s = socket.create_connection(p.address, timeout=5)
        s.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = s.recv(4096)
            if not chunk:
                break
            head += chunk
        return s, head

    def status(self, head: bytes) -> int:
        return int(head.split(b" ", 2)[1]) if head.startswith(b"HTTP/") else 0

    def wait_idle(self, p, timeout=5):
        deadline = time.time() + timeout
        while p.active() and time.time() < deadline:
            time.sleep(0.05)
        return p.active()


class AddressPolicyTest(Base):
    def test_private_internal_and_metadata_addresses_are_blocked(self):
        p = self.proxy(allow_networks=(), block_networks=["192.0.2.0/24"], local_addresses=lambda: {"198.51.100.20"})
        for ip in ("10.1.2.3", "172.16.0.1", "172.31.255.255", "192.168.1.1", "100.64.0.1", "100.127.255.254",
                   "127.0.0.1", "127.8.9.10", "169.254.169.254", "169.254.0.1", "0.0.0.0", "224.0.0.1",
                   "255.255.255.255", "::1", "::", "fc00::1", "fd12:3456::1", "fe80::1", "ff02::1",
                   "::ffff:127.0.0.1", "::ffff:169.254.169.254", "64:ff9b::a9fe:a9fe", "2002:a00:1::",
                   "192.0.2.10",                     # NCS_EGRESS_BLOCK_NETWORKS（例如 WireGuard 网段）
                   "198.51.100.20"):                 # 沙箱主机自己的地址
            with self.subTest(ip=ip):
                self.assertTrue(p.blocked(ip), ip)
        for ip in ("1.1.1.1", "8.8.8.8", "2606:4700:4700::1111"):
            with self.subTest(ip=ip):
                self.assertEqual(p.blocked(ip), "")

    def test_docker_subnet_and_extra_networks_come_from_config(self):
        cfg = Config(docker_subnet="172.30.250.0/24", egress_block_networks=["10.77.0.0/24", "8.8.8.0/24"])
        from nanocode_server.sandbox_agent import build_egress
        p = build_egress(cfg, "127.0.0.1", log=lambda m: None, local_addresses=lambda: set())
        self.addCleanup(p.close)
        self.assertTrue(p.blocked("172.30.250.7"))
        self.assertTrue(p.blocked("10.77.0.1"))
        self.assertTrue(p.blocked("8.8.8.8"))
        self.assertEqual(p.blocked("1.1.1.1"), "")

    def test_host_addresses_are_read_on_linux(self):
        from nanocode_server.egress import host_addresses
        addrs = host_addresses()
        if os.path.exists("/proc/net/fib_trie"):
            self.assertIn("127.0.0.1", addrs)


class ForwardingTest(Base):
    def test_connect_tunnel_to_a_public_address(self):
        p = self.proxy()
        s, head = self.connect(p, f"public.test:{self.up_port}")
        try:
            self.assertEqual(self.status(head), 200, head)
            s.sendall(b"GET /x HTTP/1.1\r\nHost: public.test\r\nConnection: close\r\n\r\n")
            out = b""
            while chunk := s.recv(65536):
                out += chunk
            self.assertIn(b"path=/x", out)
        finally:
            s.close()

    def test_plain_http_is_rewritten_and_proxy_headers_are_dropped(self):
        p = self.proxy()
        out = self.raw(p, (f"GET http://public.test:{self.up_port}/a/b?q=1 HTTP/1.1\r\nHost: evil.example\r\n"
                           "Proxy-Authorization: Basic c2VjcmV0\r\nProxy-Connection: keep-alive\r\n"
                           "X-Keep: 1\r\n\r\n").encode())
        self.assertIn(b"200", out.split(b"\r\n", 1)[0])
        self.assertIn(b"path=/a/b?q=1", out)
        self.assertIn(f"host=public.test:{self.up_port}".encode(), out)      # Host 按地址重写，不信客户端的
        self.assertIn(b"h:x-keep", out)
        self.assertNotIn(b"h:proxy-authorization", out)
        self.assertNotIn(b"h:proxy-connection", out)

    def test_dns_is_resolved_by_the_proxy_and_failures_are_502(self):
        p = self.proxy()
        s, head = self.connect(p, "nosuch.test:443")
        s.close()
        self.assertEqual(self.status(head), 502)
        self.assertIn(b"DNS", head)


class SsrfTest(Base):
    def test_internal_destinations_are_refused(self):
        p = self.proxy()
        for target in ("internal.test:443", "meta.test:443", "mixed.test:443", "v6local.test:443", "ula.test:443",
                       "mapped.test:443", "cgnat.test:443", "10.0.0.1:443", "169.254.169.254:80",
                       "[::1]:443", "[fd00::1]:443"):
            with self.subTest(target=target):
                s, head = self.connect(p, target)
                s.close()
                self.assertEqual(self.status(head), 403, head)

    def test_plain_http_to_metadata_is_refused(self):
        p = self.proxy()
        for url in ("http://169.254.169.254/latest/meta-data/", "http://meta.test/", "http://10.0.0.1/",
                    "http://2130706433/", "http://127.0.0.1:22/"):
            with self.subTest(url=url):
                out = self.raw(p, f"GET {url} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
                self.assertEqual(self.status(out), 403, out)

    def test_loopback_is_refused_without_the_test_exemption(self):
        p = self.proxy(allow_networks=())
        s, head = self.connect(p, f"public.test:{self.up_port}")
        s.close()
        self.assertEqual(self.status(head), 403)

    def test_only_allowed_ports(self):
        p = self.proxy(allowed_ports=(80, 443))
        for target in ("public.test:22", "public.test:25", "public.test:6379", f"public.test:{self.up_port}"):
            with self.subTest(target=target):
                s, head = self.connect(p, target)
                s.close()
                self.assertEqual(self.status(head), 403)
                self.assertIn(b"Port", head)

    def test_dns_rebinding_resolves_once_and_connects_to_the_checked_address(self):
        """第一次解析是公网、之后变成内网：代理只解析一次，连的就是检查过的地址。"""
        calls = []

        def rebinding(host, port):
            calls.append(host)
            return ["127.0.0.1"] if len(calls) == 1 else ["10.0.0.9"]

        p = self.proxy(resolver=rebinding)
        connected = []
        real = socket.create_connection

        def spy(addr, *a, **kw):
            if addr[1] == self.up_port:                         # 只记代理连上游的那一下（测试客户端也用它）
                connected.append(addr[0])
            return real(addr, *a, **kw)

        import nanocode_server.egress as egress
        egress.socket.create_connection = spy
        self.addCleanup(setattr, egress.socket, "create_connection", real)
        out = self.raw(p, f"GET http://rebind.test:{self.up_port}/ HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        self.assertIn(b"200", out.split(b"\r\n", 1)[0])
        self.assertEqual(calls, ["rebind.test"])                  # 只解析了一次
        self.assertEqual(connected, ["127.0.0.1"])                # 连的就是检查过的那个 IP
        out = self.raw(p, f"GET http://rebind.test:{self.up_port}/ HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        self.assertEqual(self.status(out), 403)                   # 第二次解析到内网：拒绝，没去连
        self.assertEqual(connected, ["127.0.0.1"])

    def test_connected_peer_is_checked_again(self):
        p = self.proxy()
        seen = []
        original = p.blocked

        def blocked(ip):
            seen.append(ip)
            return "" if len(seen) == 1 else "private or internal address"     # 连上之后的复核才拦

        p.blocked = blocked
        s, head = self.connect(p, f"public.test:{self.up_port}")
        s.close()
        self.assertEqual(self.status(head), 403)
        self.assertEqual(len(seen), 2)
        p.blocked = original

    def test_malformed_requests(self):
        p = self.proxy()
        for req in (b"GET / HTTP/1.1\r\nHost: public.test\r\n\r\n",               # 不是绝对地址
                    b"GET https://public.test/ HTTP/1.1\r\n\r\n",                  # https 要用 CONNECT
                    b"GET ftp://public.test/ HTTP/1.1\r\n\r\n",
                    b"GET http://user:pw@public.test/ HTTP/1.1\r\n\r\n",
                    b"CONNECT public.test HTTP/1.1\r\n\r\n",
                    b"CONNECT bad_host!:443 HTTP/1.1\r\n\r\n",
                    b"GET http://public.test/ HTTP/1.1\r\nX: a\nInjected: b\r\n\r\n",
                    b"TRACE http://public.test/ HTTP/1.1\r\n\r\n",
                    b"garbage\r\n\r\n"):
            with self.subTest(req=req):
                out = self.raw(p, req)
                self.assertIn(self.status(out), (400, 405), out)


class DomainListTest(Base):
    def test_denylist_and_allowlist(self):
        p = self.proxy(deny_domains=["bad.allowed.test"], allow_domains=["*.allowed.test"])
        s, head = self.connect(p, f"a.allowed.test:{self.up_port}")
        s.close()
        self.assertEqual(self.status(head), 200)
        for target, why in (("bad.allowed.test", b"denylist"), ("public.test", b"allowlist"),
                            ("127.0.0.1", b"allowlist")):
            with self.subTest(target=target):
                s, head = self.connect(p, f"{target}:{self.up_port}")
                s.close()
                self.assertEqual(self.status(head), 403)
                self.assertIn(why, head)


class LimitsTest(Base):
    def test_per_sandbox_limit_and_release(self):
        p = self.proxy(per_source=1, max_connections=4)
        s1, head = self.connect(p, f"public.test:{self.up_port}")
        self.assertEqual(self.status(head), 200)
        out = self.raw(p, f"CONNECT public.test:{self.up_port} HTTP/1.1\r\n\r\n".encode())
        self.assertIn(out[:12], (b"", b"HTTP/1.1 503"))           # 第二条直接被拒
        s1.close()
        self.assertEqual(self.wait_idle(p), 0)
        s2, head = self.connect(p, f"public.test:{self.up_port}")
        s2.close()
        self.assertEqual(self.status(head), 200)

    def test_slow_request_header_is_cut_off(self):
        p = self.proxy(header_timeout=0.5)
        s = socket.create_connection(p.address, timeout=5)
        try:
            s.sendall(b"CONNECT public.test:443 HTTP/1.1\r\n")   # 不发完
            started = time.time()
            out = s.recv(4096)
            self.assertLess(time.time() - started, 4)
            self.assertIn(b"408", out)
        finally:
            s.close()
        self.assertEqual(self.wait_idle(p), 0)

    def test_oversized_header_is_refused(self):
        p = self.proxy(max_header_bytes=1024)
        out = self.raw(p, b"GET http://public.test/ HTTP/1.1\r\nX: " + b"a" * 4096 + b"\r\n\r\n")
        self.assertEqual(self.status(out), 431)

    def test_idle_tunnel_is_closed(self):
        p = self.proxy(idle_timeout=0.5)
        s, head = self.connect(p, f"public.test:{self.up_port}")
        try:
            self.assertEqual(self.status(head), 200)
            started = time.time()
            self.assertEqual(s.recv(10), b"")                      # 什么都不发：被断开
            self.assertLess(time.time() - started, 4)
        finally:
            s.close()
        self.assertEqual(self.wait_idle(p), 0)


class LogTest(Base):
    def test_log_has_domain_and_bytes_but_no_path_or_headers(self):
        import json
        p = self.proxy(who=lambda ip: "sbx-abc")
        self.raw(p, (f"GET http://public.test:{self.up_port}/secret-path?token=t0psecret HTTP/1.1\r\n"
                     "Authorization: Bearer h3adersecret\r\nCookie: c00kie\r\n\r\n").encode())
        s, _ = self.connect(p, "meta.test:443")
        s.close()
        deadline = time.time() + 5
        while len(self.logs) < 2 and time.time() < deadline:
            time.sleep(0.05)
        text = "\n".join(self.logs)
        for secret in ("secret-path", "t0psecret", "h3adersecret", "c00kie", "Authorization"):
            self.assertNotIn(secret, text)
        entries = [json.loads(line) for line in self.logs]
        ok = next(e for e in entries if e["host"] == "public.test")
        self.assertEqual((ok["egress"], ok["method"], ok["result"]), ("sbx-abc", "HTTP", "ok"))
        self.assertGreater(ok["up"], 0)
        self.assertGreater(ok["down"], 0)
        denied = next(e for e in entries if e["host"] == "meta.test")
        self.assertTrue(denied["result"].startswith("denied:"))


class WiringTest(unittest.TestCase):
    def agent(self, **kw):
        cfg = Config(data_dir=tempfile.mkdtemp(prefix="ncs-egress-"), remote_agent_secret=SECRET, gateway_port=0,
                     agent_upstream_gateway="127.0.0.1:9", egress_port=0, **kw)
        launcher = LocalLauncher(cfg.data_dir)
        agent, relay = build_agent(cfg, launcher=launcher, log=lambda m: None)
        self.addCleanup(relay.close)
        if agent.egress:
            self.addCleanup(agent.egress.close)
        return agent, relay, launcher

    def test_off_by_default(self):
        self.assertFalse(Config().egress_proxy)
        agent, _, launcher = self.agent()
        self.assertIsNone(agent.egress)
        self.assertEqual(launcher.extra_env, {})

    def test_enabled_injects_proxy_env_and_keeps_gateway_off_the_proxy(self):
        agent, relay, launcher = self.agent(egress_proxy=True)
        self.assertIsNotNone(agent.egress)
        env = launcher.extra_env
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            self.assertEqual(env[k], agent.egress.url)
        self.assertIn(relay.address[0], env["NO_PROXY"].split(","))
        self.assertEqual(env["NO_PROXY"], env["no_proxy"])
        self.assertEqual(env["NANOCODE_EGRESS_PROXY"], "1")
        # 控制面另指了网关地址：那个主机名也补进 NO_PROXY
        spec = SandboxSpec("a" * 16, "u", "t" * 16, "", {"base_url": "http://gw.internal.test:8701/v1"})
        extra = launcher.sandbox_extra_env(spec)
        self.assertIn("gw.internal.test", extra["NO_PROXY"].split(","))
        self.assertNotIn("gw.internal.test", launcher.extra_env["NO_PROXY"])      # 不改共享的那份

    def test_docker_container_gets_proxy_env_but_spec_env_wins(self):
        d = DockerLauncher.__new__(DockerLauncher)
        d.network, d.runtime, d.memory, d.cpu, d.tmpfs, d.pids_limit, d.image = \
            "ncs-sandboxes", "", "384Mi", "1", "", 256, "img"
        d.extra_env = {**proxy_env("http://172.30.250.1:3128", ["172.30.250.1"]), "NANOCODE_WEB_TOKEN": "evil"}
        spec = SandboxSpec("a" * 16, "u", "t" * 16, "", {"base_url": "http://172.30.250.1:8701/v1"})
        env = dict(e.split("=", 1) for e in d.container_config(spec, "/d", "/p")["Env"])
        self.assertEqual(env["HTTPS_PROXY"], "http://172.30.250.1:3128")
        self.assertIn("172.30.250.1", env["NO_PROXY"].split(","))
        self.assertEqual(env["NANOCODE_WEB_TOKEN"], "t" * 16)

    def test_config_from_env_and_validation(self):
        env = {"NCS_EGRESS_PROXY": "1", "NCS_EGRESS_ALLOWED_PORTS": "443, 80", "NCS_EGRESS_DENY_DOMAINS": "a.test,b.test",
               "NCS_EGRESS_BLOCK_NETWORKS": "10.77.0.0/24", "NCS_EGRESS_PER_SANDBOX": "4"}
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            cfg = Config.from_env()
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertTrue(cfg.egress_proxy)
        self.assertEqual(cfg.egress_allowed_ports, [443, 80])
        self.assertEqual(cfg.egress_deny_domains, ["a.test", "b.test"])
        self.assertEqual(cfg.egress_per_sandbox, 4)
        base = dict(remote_agent_secret=SECRET, agent_bind="10.77.0.2:8790", agent_upstream_gateway="10.77.0.1:8701",
                    egress_proxy=True)
        Config(**base).validate_agent()
        for kw in ({"egress_bind": "0.0.0.0"}, {"egress_port": 8701}, {"egress_allowed_ports": []},
                   {"egress_allowed_ports": [0]}, {"egress_per_sandbox": 100}, {"egress_idle_timeout": 1},
                   {"egress_block_networks": ["nope"]}):
            with self.subTest(kw=kw):
                with self.assertRaises(ValueError):
                    Config(**{**base, **kw}).validate_agent()
        os.environ["NCS_EGRESS_ALLOWED_PORTS"] = "80,x"
        try:
            with self.assertRaises(ValueError):
                Config.from_env()
        finally:
            os.environ.pop("NCS_EGRESS_ALLOWED_PORTS")


if __name__ == "__main__":
    unittest.main()
