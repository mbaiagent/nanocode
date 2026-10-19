"""P0（#72）：多副本必须用 PostgreSQL + Redis；Redis 默认 RESP2；单机 compose 与代码一致。"""

import os
import re
import sys
import types
import unittest
from unittest import mock

import support  # noqa: F401

from support import REPO, make_config

from nanocode_server.config import Config
from nanocode_server.storage.ratelimit import RedisRateLimiter, redis_options

COMPOSE = os.path.join(REPO, "deploy", "compose")
PG = "postgresql://ncs:pw@db:5432/ncs"


def read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


class ReplicaCheckTest(unittest.TestCase):
    def test_single_replica_sqlite_is_fine(self):
        make_config().validate()
        make_config(database_url="sqlite:///tmp/x.db").validate()

    def test_multiple_replicas_refuse_default_sqlite(self):
        with self.assertRaisesRegex(ValueError, "SQLite"):
            make_config(replicas=2, redis_url="redis://r").validate()

    def test_multiple_replicas_refuse_explicit_sqlite_url(self):
        with self.assertRaisesRegex(ValueError, "SQLite"):
            make_config(replicas=3, database_url="sqlite:///data/p.db", redis_url="redis://r").validate()

    def test_multiple_replicas_need_redis(self):
        with self.assertRaisesRegex(ValueError, "NCS_REDIS_URL"):
            make_config(replicas=2, database_url=PG).validate()

    def test_multiple_replicas_with_postgres_and_redis_ok(self):
        make_config(replicas=2, database_url=PG, redis_url="redis://r").validate()
        make_config(replicas=2, database_url="postgres://a@b/c", redis_url="redis://r").validate(launcher_needed=False)

    def test_gateway_role_is_checked_too(self):
        with self.assertRaisesRegex(ValueError, "SQLite"):
            make_config(replicas=2, redis_url="redis://r").validate(launcher_needed=False)

    def test_replicas_must_be_positive(self):
        with self.assertRaisesRegex(ValueError, "NCS_REPLICAS"):
            make_config(replicas=0).validate()

    def test_from_env(self):
        with mock.patch.dict(os.environ, {"NCS_REPLICAS": "4"}):
            self.assertEqual(Config.from_env().replicas, 4)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NCS_REPLICAS", None)
            self.assertEqual(Config.from_env().replicas, 1)


class RedisProtocolTest(unittest.TestCase):
    def test_defaults_to_resp2_on_new_redis_py(self):
        self.assertEqual(redis_options("redis://h:6379/0", "8.0.1"), {"socket_timeout": 2, "protocol": 2})
        self.assertEqual(redis_options("redis://h/0", "5.0.0")["protocol"], 2)

    def test_explicit_protocol_in_url_wins(self):
        self.assertNotIn("protocol", redis_options("redis://h/0?protocol=3", "8.0.1"))
        self.assertNotIn("protocol", redis_options("rediss://:pw@h/0?ssl_cert_reqs=none&protocol=2", "8.0.1"))

    def test_old_redis_py_gets_no_protocol_kwarg(self):
        self.assertNotIn("protocol", redis_options("redis://h/0", "4.6.0"))
        self.assertNotIn("protocol", redis_options("redis://h/0", ""))

    def test_limiter_passes_resp2_to_client(self):
        calls = {}

        class FakeClient:
            def ping(self):
                return True

        class FakeRedis:
            @staticmethod
            def from_url(url, **kw):
                calls.update(kw, url=url)
                return FakeClient()

        fake = types.SimpleNamespace(Redis=FakeRedis, __version__="8.2.0")
        with mock.patch.dict(sys.modules, {"redis": fake}):
            RedisRateLimiter("redis://:pw@old-redis:6379/0", 10)
        self.assertEqual(calls["protocol"], 2)
        self.assertEqual(calls["url"], "redis://:pw@old-redis:6379/0")


class ComposeConsistencyTest(unittest.TestCase):
    KNOWN = set(re.findall(r'"(NCS_[A-Z0-9_]+)"', read(REPO, "server", "nanocode_server", "config.py")))
    # 只在 compose 里用的变量（不是平台配置项）
    COMPOSE_ONLY = {"NCS_HOST_DATA_DIR", "NCS_PG_PORT", "NCS_REDIS_PORT"}

    def setUp(self):
        self.compose = read(COMPOSE, "docker-compose.yml")
        self.env = dict(re.findall(r"^([A-Z0-9_]+)=(.*)$", read(COMPOSE, ".env.example"), re.M))

    def test_platform_variables_are_known(self):
        used = set(re.findall(r"\b(NCS_[A-Z0-9_]+)", self.compose)) | {k for k in self.env if k.startswith("NCS_")}
        self.assertEqual(used - self.KNOWN - self.COMPOSE_ONLY, set())

    def test_env_example_covers_required_and_is_used(self):
        required = set(re.findall(r"\$\{([A-Z0-9_]+):\?", self.compose))
        self.assertEqual(required, {"NCS_SECRET_KEY", "POSTGRES_PASSWORD", "REDIS_PASSWORD"})
        self.assertLessEqual(required, set(self.env))
        referenced = set(re.findall(r"\$\{([A-Z0-9_]+)", self.compose))
        self.assertEqual(set(self.env) - referenced, set(), ".env.example 里有 compose 没用到的变量")

    def test_production_backends_and_single_replica(self):
        self.assertIn("NCS_DATABASE_URL: postgresql://", self.compose)
        self.assertIn("NCS_REDIS_URL: redis://", self.compose)
        self.assertIn('NCS_REPLICAS: "1"', self.compose)
        self.assertIn("NCS_LAUNCHER: docker", self.compose)

    def test_data_dir_is_mounted_at_same_path(self):
        # 沙箱容器由宿主机 Docker 创建，绑定挂载用宿主机路径：容器内外必须同一个路径
        d = "${NCS_HOST_DATA_DIR:-/srv/nanocode}"
        self.assertIn(f"- {d}:{d}", self.compose)
        self.assertIn(f"NCS_DATA_DIR: {d}", self.compose)

    def test_databases_only_on_loopback(self):
        for port in re.findall(r'^\s+- "([^"]+:\d+)"$', self.compose, re.M):
            self.assertTrue(port.startswith("127.0.0.1:"), port)

    def test_k8s_replicas_env_matches_deployment(self):
        k8s = os.path.join(REPO, "deploy", "k8s")
        for name in ("50-control-plane.yaml", "60-gateway.yaml"):
            text = read(k8s, name)
            with self.subTest(file=name):
                replicas = re.search(r"^\s+replicas: (\d+)", text, re.M).group(1)
                self.assertIn(f'{{name: NCS_REPLICAS, value: "{replicas}"}}', text)


if __name__ == "__main__":
    unittest.main()
