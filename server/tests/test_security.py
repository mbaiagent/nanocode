"""签名令牌、API Key 加密、身份认证。"""

import base64
import socket
import unittest
from unittest import mock

import support  # noqa: F401

from nanocode_server.security.auth import COOKIE, Authenticator, AuthError, host_name
from nanocode_server.models import Upstream
from nanocode_server.security.secretbox import SecretBox, SecretBoxError, hint
from nanocode_server.services.upstream import UpstreamError, check_base_url, open_request
from nanocode_server.security.tokens import Signer
from support import FakeServer, SECRET, make_config


class SignerTest(unittest.TestCase):
    def test_round_trip_and_expiry(self):
        s = Signer(SECRET, "gateway")
        t = s.sign({"u": "alice", "s": "x"}, ttl=60, now=1000)
        self.assertEqual(s.verify(t, now=1059)["u"], "alice")
        self.assertIsNone(s.verify(t, now=1061))

    def test_purposes_do_not_mix(self):
        """登录 Cookie 不能拿去当网关令牌用，反之亦然。"""
        login = Signer(SECRET, "login").sign({"u": "alice"}, 60)
        self.assertIsNone(Signer(SECRET, "gateway").verify(login))

    def test_tampering(self):
        s = Signer(SECRET, "login")
        t = s.sign({"u": "alice"}, 60)
        forged = Signer("x" * 48, "login").sign({"u": "admin"}, 60).split(".")[1]
        v, _, sig = t.split(".")
        self.assertIsNone(s.verify(f"{v}.{forged}.{sig}"))
        for bad in ("", "garbage", "v1.a.b", None):
            self.assertIsNone(s.verify(bad))


class SecretBoxTest(unittest.TestCase):
    def test_round_trip_and_randomness(self):
        box = SecretBox(SECRET)
        a, b = box.encrypt("sk-secret-1234"), box.encrypt("sk-secret-1234")
        self.assertNotEqual(a, b)                      # 每次都用新的随机数
        self.assertNotIn("sk-secret", a)
        self.assertEqual(box.decrypt(a), "sk-secret-1234")
        self.assertEqual(box.decrypt(box.encrypt("长" * 100)), "长" * 100)

    def test_tampering_and_wrong_key_rejected(self):
        box = SecretBox(SECRET)
        ct = bytearray(base64.b64decode(box.encrypt("sk-1234567890")))
        ct[-1] ^= 1
        with self.assertRaises(SecretBoxError):
            box.decrypt(base64.b64encode(bytes(ct)).decode())
        with self.assertRaises(SecretBoxError):
            SecretBox("y" * 48).decrypt(box.encrypt("sk-1234567890"))
        with self.assertRaises(SecretBoxError):
            box.decrypt("not base64!")

    def test_hint(self):
        self.assertEqual(hint("sk-abcdefgh1234"), "…1234")
        self.assertEqual(hint("short"), "…")


class AuthTest(unittest.TestCase):
    def test_dev_cookie_login(self):
        auth = Authenticator(make_config())
        cookie = auth.login_cookie("Alice")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        token = cookie.split(";")[0].split("=", 1)[1]
        self.assertEqual(auth.identify({}, {COOKIE: token}), "alice")
        self.assertIsNone(auth.identify({}, {}))
        self.assertIsNone(auth.identify({}, {COOKIE: "forged"}))
        with self.assertRaises(AuthError):
            auth.login_cookie("bad name/../x")

    def test_dev_mode_rejects_rebinding_hosts(self):
        auth = Authenticator(make_config())
        auth.check_host("127.0.0.1:8700")
        auth.check_host("localhost:8700")
        auth.check_host("[::1]:8700")
        with self.assertRaises(AuthError) as cm:
            auth.check_host("evil.example:8700")
        self.assertEqual(cm.exception.status, 403)

    def test_proxy_mode(self):
        auth = Authenticator(make_config(auth="proxy", proxy_secret="p" * 20))
        with self.assertRaises(AuthError):        # 没经过认证代理
            auth.identify({"X-Auth-Request-Email": "alice@example.com"}, {})
        self.assertEqual(auth.identify({"X-Auth-Request-Email": "Alice@Example.com", "X-NCS-Proxy-Secret": "p" * 20}, {}),
                         "alice@example.com")
        self.assertIsNone(auth.identify({"X-NCS-Proxy-Secret": "p" * 20}, {}))
        with self.assertRaises(AuthError):        # 代理模式不能用登录接口
            auth.login_cookie("alice")
        auth.check_host("nanocode.example.com")   # 代理模式不限制 Host

    def test_host_name(self):
        self.assertEqual(host_name("[::1]:80"), "::1")
        self.assertEqual(host_name("Example.com:443"), "example.com")
        self.assertEqual(host_name("example.com"), "example.com")


class ConfigTest(unittest.TestCase):
    def test_unsafe_configs_refused(self):
        for kw in ({"secret_key": "short"},
                   {"auth": "proxy", "proxy_secret": ""},           # 谁都能伪造身份请求头
                   {"auth": "dev", "host": "0.0.0.0"},             # 开发模式登录不能对外
                   {"auth": "proxy", "proxy_secret": "p" * 20, "allow_private_upstreams": True},
                   {"sandbox_mode": "bypass"},                      # 不在允许的模式里
                   {"auth": "nope"},
                   {"launcher": "nope"},
                   # 生产模式没有隔离：每个用户的 Agent 都以控制面同一个系统用户运行（#36）
                   {"auth": "proxy", "proxy_secret": "p" * 20, "launcher": "local"}):
            with self.subTest(**kw), self.assertRaises(ValueError):
                make_config(**kw).validate()
        make_config().validate()
        make_config(auth="proxy", proxy_secret="p" * 20, host="0.0.0.0", launcher="docker").validate()
        # 显式承认无隔离风险后才放行（#36）
        make_config(auth="proxy", proxy_secret="p" * 20, launcher="local", allow_unisolated=True).validate()
        # 只启动网关的角色不创建沙箱，不该被隔离检查拦住（#36）
        make_config(auth="proxy", proxy_secret="p" * 20, launcher="local").validate(launcher_needed=False)

    def test_dev_secret_is_generated_once_and_kept(self):
        cfg = make_config(secret_key="")
        cfg.ensure_secret()
        first = cfg.secret_key
        again = make_config(secret_key="", data_dir=cfg.data_dir)
        again.ensure_secret()
        self.assertEqual(again.secret_key, first)     # 重启后还能解密用户的 Key
        with self.assertRaises(ValueError):
            make_config(secret_key="", auth="proxy", proxy_secret="p" * 20).ensure_secret()


class UpstreamSecurityTest(unittest.TestCase):
    def test_private_mode_rejects_link_local_metadata(self):
        with self.assertRaises(UpstreamError):
            check_base_url("http://169.254.169.254/latest/meta-data/", allow_private=True)

    def test_private_mode_resolves_and_pins_private_https(self):
        def private_dns(host, port, **kw):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port))]

        with mock.patch("socket.getaddrinfo", side_effect=private_dns):
            self.assertEqual(check_base_url("https://model.internal/v1", allow_private=True), "10.0.0.5")

    def test_private_http_request_uses_resolved_ip(self):
        server = FakeServer(lambda method, path, headers, body: (200, {}, b"ok"))
        connected_to = []
        original_create_connection = socket.create_connection

        def private_dns(host, port, *args, **kw):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

        def record_connection(address, *args, **kw):
            connected_to.append(address)
            return original_create_connection(address, *args, **kw)

        try:
            upstream = Upstream("openai", f"http://model.internal:{server.port}/v1", "key", "model", billable=False)
            with mock.patch("socket.getaddrinfo", side_effect=private_dns), \
                    mock.patch("socket.create_connection", side_effect=record_connection):
                conn, response = open_request(upstream, {}, allow_private=True, timeout=5)
                try:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), b"ok")
                finally:
                    conn.close()
            self.assertEqual(connected_to[0][0], "127.0.0.1")
            self.assertEqual(server.requests[0]["headers"]["Host"], f"model.internal:{server.port}")
        finally:
            server.close()


if __name__ == "__main__":
    unittest.main()
