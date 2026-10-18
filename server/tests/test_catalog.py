"""模型目录：一个用户能用哪些模型、会话的模型转发到哪里、添加 Key 的校验（含 SSRF 防护）。"""

import unittest
from unittest import mock

import support  # noqa: F401

from nanocode_server.security.secretbox import SecretBox
from nanocode_server.services.catalog import MOCK_REF, CatalogError, ModelCatalog
from nanocode_server.storage import open_store
from support import SECRET, make_config


def public_dns(host, port, **kw):
    return [(2, 1, 6, "", ("93.184.216.34", port))]


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config(platform_base_url="https://platform.example/v1", platform_api_key="PLATFORM-KEY",
                               platform_models=["big", "small"])
        self.store = open_store(self.cfg)
        self.cat = ModelCatalog(self.cfg, self.store, SecretBox(SECRET))

    def tearDown(self):
        self.store.close()

    def add(self, owner="alice", **kw):
        args = dict(provider="deepseek", api_key="sk-alice-123456", models=["deepseek-chat"])
        args.update(kw)
        with mock.patch("socket.getaddrinfo", public_dns):
            return self.cat.add_credential(owner, **args)

    def test_options_by_source(self):
        refs = [o.ref for o in self.cat.options("alice")]
        self.assertEqual(refs, [MOCK_REF, "p:big", "p:small"])
        c = self.add()
        self.assertIn(f"c:{c.id}:deepseek-chat", [o.ref for o in self.cat.options("alice")])
        self.assertNotIn(f"c:{c.id}:deepseek-chat", [o.ref for o in self.cat.options("bob")])   # 别人看不到
        self.assertEqual(self.cat.default_ref("alice"), f"c:{c.id}:deepseek-chat")             # 优先用自己的 Key
        self.assertEqual(self.cat.default_ref("bob"), "p:big")

    def test_resolve(self):
        c = self.add()
        up = self.cat.resolve("alice", f"c:{c.id}:deepseek-chat")
        self.assertEqual((up.api_key, up.model, up.base_url, up.billable),
                         ("sk-alice-123456", "deepseek-chat", "https://api.deepseek.com/v1", False))
        self.assertTrue(self.cat.resolve("bob", "p:big").billable)
        with self.assertRaises(CatalogError):          # 拿着别人的凭证 id 也解析不出来
            self.cat.resolve("bob", f"c:{c.id}:deepseek-chat")
        with self.assertRaises(CatalogError):
            self.cat.resolve("alice", f"c:{c.id}:not-listed")
        with self.assertRaises(CatalogError):
            self.cat.resolve("alice", "p:unknown")

    def test_key_is_stored_encrypted(self):
        c = self.add()
        raw = self.store.db.execute("SELECT secret, key_hint FROM credentials WHERE id = ?", (c.id,))[0]
        self.assertNotIn("sk-alice", raw["secret"])
        self.assertEqual(raw["key_hint"], "…3456")
        self.assertNotIn("secret", c.public())

    def test_preset_url_cannot_be_overridden(self):
        c = self.add(base_url="https://evil.example/v1")
        self.assertEqual(c.base_url, "https://api.deepseek.com/v1")

    def test_custom_url_must_be_public_https(self):
        for url in ("http://127.0.0.1:8080/v1", "https://user:pw@x.example/v1", "ftp://x"):
            with self.subTest(url=url), self.assertRaises(CatalogError):
                self.add(provider="custom", base_url=url)
        private = lambda host, port, **kw: [(2, 1, 6, "", ("10.0.0.5", port))]   # noqa: E731
        with mock.patch("socket.getaddrinfo", private), self.assertRaises(CatalogError):
            self.cat.add_credential("alice", "custom", "sk-x-123456", ["m"], base_url="https://internal.example/v1")
        c = self.add(provider="custom", base_url="https://llm.example/v1", protocol="anthropic")
        self.assertEqual((c.protocol, c.base_url), ("anthropic", "https://llm.example/v1"))

    def test_private_allowed_when_configured(self):
        self.cfg.allow_private_upstreams = True
        loopback_dns = lambda host, port, **kw: [(2, 1, 6, "", ("127.0.0.1", port))]  # noqa: E731
        with mock.patch("socket.getaddrinfo", loopback_dns):
            c = self.cat.add_credential("alice", "custom", "sk-alice-123456", ["deepseek-chat"],
                                        base_url="http://127.0.0.1:11434/v1")
        self.assertEqual(c.base_url, "http://127.0.0.1:11434/v1")

    def test_private_mode_rejects_link_local_metadata_target(self):
        self.cfg.allow_private_upstreams = True
        with self.assertRaises(CatalogError):
            self.cat.add_credential("alice", "custom", "sk-x-123456", ["m"],
                                    base_url="http://169.254.169.254/latest/meta-data/")

    def test_private_mode_rejects_cleartext_http_to_public_ip(self):
        self.cfg.allow_private_upstreams = True
        with mock.patch("socket.getaddrinfo", public_dns), self.assertRaises(CatalogError):
            self.cat.add_credential("alice", "custom", "sk-x-123456", ["m"],
                                    base_url="http://api.example/v1")

    def test_input_validation(self):
        for kw in ({"api_key": ""}, {"api_key": "has space"}, {"models": []}, {"models": ["bad model!"]},
                   {"provider": "nope"}, {"provider": "custom", "base_url": "https://x.example", "protocol": "grpc"}):
            with self.subTest(**kw), self.assertRaises(CatalogError):
                self.add(**kw)


if __name__ == "__main__":
    unittest.main()
