"""网关令牌、身份认证、配置的安全检查。"""

import unittest

import support  # noqa: F401  —— 设置 import 路径

from nanocode_server import tokens
from nanocode_server.auth import AuthError, authenticate
from support import SECRET, make_config


class TokenTest(unittest.TestCase):
    def test_round_trip(self):
        t = tokens.sign(SECRET, "alice@example.com", "abc", ttl=60, now=1000)
        claims = tokens.verify(SECRET, t, now=1010)
        self.assertEqual((claims["u"], claims["s"]), ("alice@example.com", "abc"))

    def test_expired(self):
        t = tokens.sign(SECRET, "alice", "abc", ttl=60, now=1000)
        self.assertIsNone(tokens.verify(SECRET, t, now=1061))

    def test_wrong_secret_and_tampering(self):
        t = tokens.sign(SECRET, "alice", "abc", ttl=60)
        self.assertIsNone(tokens.verify("x" * 40, t))
        prefix, payload, sig = t.split(".")
        forged = tokens.sign("x" * 40, "bob", "abc", ttl=60).split(".")[1]   # 换成别人的载荷
        self.assertIsNone(tokens.verify(SECRET, f"{prefix}.{forged}.{sig}"))
        self.assertIsNone(tokens.verify(SECRET, "garbage"))
        self.assertIsNone(tokens.verify(SECRET, ""))


class AuthTest(unittest.TestCase):
    def test_requires_proxy_secret(self):
        cfg = make_config(proxy_secret="p" * 20)
        with self.assertRaises(AuthError):     # 绕过认证代理、自己写身份头
            authenticate({"X-Auth-Request-Email": "alice@example.com"}, "10.0.0.1", cfg)
        with self.assertRaises(AuthError):
            authenticate({"X-Auth-Request-Email": "alice@example.com", "X-NCS-Proxy-Secret": "wrong"}, "10.0.0.1", cfg)
        user = authenticate({"X-Auth-Request-Email": "Alice@Example.com", "X-NCS-Proxy-Secret": "p" * 20}, "10.0.0.1", cfg)
        self.assertEqual(user, "alice@example.com")

    def test_missing_or_malformed_user(self):
        cfg = make_config(proxy_secret="p" * 20)
        with self.assertRaises(AuthError):
            authenticate({"X-NCS-Proxy-Secret": "p" * 20}, "10.0.0.1", cfg)
        with self.assertRaises(AuthError):
            authenticate({"X-NCS-Proxy-Secret": "p" * 20, "X-Auth-Request-Email": "a b/../c"}, "10.0.0.1", cfg)

    def test_dev_user_only_from_loopback(self):
        cfg = make_config(dev_user="dev@localhost")
        self.assertEqual(authenticate({}, "127.0.0.1", cfg), "dev@localhost")
        with self.assertRaises(AuthError):
            authenticate({}, "192.168.1.5", cfg)


class ConfigCheckTest(unittest.TestCase):
    def test_refuses_unsafe_configs(self):
        with self.assertRaises(ValueError):
            make_config(token_secret="short", dev_user="d").check_control()
        with self.assertRaises(ValueError):          # 既没有代理密钥也不是开发模式：谁都能伪造身份
            make_config().check_control()
        with self.assertRaises(ValueError):          # 开发模式不能监听外网
            make_config(dev_user="d", host="0.0.0.0").check_control()
        with self.assertRaises(ValueError):          # 本机启动器没有隔离，不能放开 bypass
            make_config(dev_user="d", sandbox_modes=["default", "bypass"]).check_control()
        make_config(dev_user="d").check_control()
        make_config(proxy_secret="p" * 20, launcher="k8s", sandbox_modes=["default", "bypass"]).check_control()

    def test_sandbox_policy_locks_model_to_gateway(self):
        cfg = make_config(gateway_url="http://gw:8080/v1", model="deepseek-chat")
        p = cfg.sandbox_policy("/data/home/audit.jsonl")
        self.assertEqual((p["provider"], p["protocol"], p["base_url"], p["model"]),
                         ("custom", "openai", "http://gw:8080/v1", "deepseek-chat"))
        for key in ("hooks", "mcpServers", "verify", "permissions.allow", "headers"):
            self.assertIn(key, p["forbid"])
        self.assertNotIn("bypass", p["allowed_modes"])
        cfg.upstream_protocol = "anthropic"
        self.assertEqual(cfg.sandbox_policy("x")["base_url"], "http://gw:8080")


if __name__ == "__main__":
    unittest.main()
