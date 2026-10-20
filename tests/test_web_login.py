"""自带登录页测试：会话门禁、密码校验、限流、登出。

每一条安全断言都对应一个真实攻击面：
- 没会话直接进 /、调 /api/* → 必须被拦（302 / 401）
- 登录页不能泄露 API 令牌（偷到令牌就能冒充用户发命令）
- 错误密码与不存在用户回同样的错（防用户名枚举）
- 会话不能伪造，登出后立即失效
- 登录接口限流防爆破
"""

import http.client
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.cookies import SimpleCookie

from helpers import Workspace
from nanocode.session import Options, Session
from nanocode.web import server as webserver
from nanocode.web.server import make_server, verify_password

TEST_USER = "admin"
TEST_PASS = "s3cret-pw-123"
TEST_SALT = "testsalt"


def openssl_available() -> bool:
    try:
        p = subprocess.run(["openssl", "version"], capture_output=True, timeout=10)
        return p.returncode == 0
    except OSError:
        return False


def make_htpasswd(path: str, username: str = TEST_USER, password: str = TEST_PASS) -> None:
    """用 openssl 生成确定性 apr1 哈希（固定盐），写入 htpasswd 文件。"""
    p = subprocess.run(
        ["openssl", "passwd", "-apr1", "-salt", TEST_SALT, "-stdin"],
        input=password.encode("utf-8"), capture_output=True, timeout=10)
    assert p.returncode == 0, "openssl passwd 失败"
    with open(path, "wb") as f:
        f.write(username.encode("utf-8") + b":" + p.stdout.strip() + b"\n")


@unittest.skipUnless(openssl_available(), "需要 openssl 可执行文件")
class VerifyPasswordTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.htpasswd = os.path.join(self.dir, ".htpasswd")
        make_htpasswd(self.htpasswd)
        self._old = os.environ.get("NANOCODE_HTPASSWD")
        os.environ["NANOCODE_HTPASSWD"] = self.htpasswd

    def tearDown(self):
        if self._old is None:
            os.environ.pop("NANOCODE_HTPASSWD", None)
        else:
            os.environ["NANOCODE_HTPASSWD"] = self._old
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_correct_password(self):
        self.assertTrue(verify_password(TEST_USER, TEST_PASS))

    def test_wrong_password(self):
        self.assertFalse(verify_password(TEST_USER, "wrong-pw"))

    def test_unknown_user(self):
        self.assertFalse(verify_password("nobody", TEST_PASS))

    def test_empty(self):
        self.assertFalse(verify_password("", ""))
        self.assertFalse(verify_password(TEST_USER, ""))

    def test_missing_file(self):
        os.environ["NANOCODE_HTPASSWD"] = os.path.join(self.dir, "不存在")
        self.assertFalse(verify_password(TEST_USER, TEST_PASS))

    def test_malformed_lines_ignored(self):
        with open(self.htpasswd, "ab") as f:
            f.write(b"no-colon-here\n")
            f.write(b"bob:$2y$10$xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n")
        # 坏行不影响正常用户；bob 的 bcrypt 行不被当成 apr1 认
        self.assertTrue(verify_password(TEST_USER, TEST_PASS))
        self.assertFalse(verify_password("bob", "whatever"))

    def test_password_not_in_argv(self):
        """密码只走 stdin：verify 过程中命令行里不能出现明文密码。"""
        seen = []

        orig_run = subprocess.run

        def spy(*a, **kw):
            seen.append(a)
            return orig_run(*a, **kw)

        import unittest.mock as mock
        with mock.patch.object(subprocess, "run", spy):
            verify_password(TEST_USER, TEST_PASS)
        for a in seen:
            cmdline = " ".join(str(x) for x in a[0]) if a else ""
            self.assertNotIn(TEST_PASS, cmdline)


@unittest.skipUnless(openssl_available(), "需要 openssl 可执行文件")
class WebLoginTest(unittest.TestCase):
    """端到端：真实 HTTP 服务 + 真实 Cookie。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.htpasswd = os.path.join(self.dir, ".htpasswd")
        make_htpasswd(self.htpasswd)
        self._old_env = dict(os.environ)
        os.environ["NANOCODE_HTPASSWD"] = self.htpasswd
        os.environ["NANOCODE_COOKIE_SECURE"] = "0"  # 测试走明文 HTTP
        self.ws = Workspace()
        self.session = Session(
            Options(provider="mock", cwd=self.ws.dir, mock_delay=0),
            log=lambda m: None)
        self.server, self.app = make_server(self.session, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.assertTrue(self.app.login_enabled)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.session.close()
        self.ws.cleanup()
        os.environ.clear()
        os.environ.update(self._old_env)
        shutil.rmtree(self.dir, ignore_errors=True)

    # -- 小工具 --
    def req(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": f"127.0.0.1:{self.port}"}
        h.update(headers or {})
        conn.request(method, path, body=body, headers=h)
        r = conn.getresponse()
        data = r.read()
        resp_headers = dict(r.getheaders())
        conn.close()
        return r.status, resp_headers, data

    def get_cookie(self, headers):
        c = SimpleCookie()
        c.load(headers.get("set-cookie", ""))
        return c.get(webserver.SESSION_COOKIE).value if webserver.SESSION_COOKIE in c else ""

    def login(self, username=TEST_USER, password=TEST_PASS):
        body = json.dumps({"username": username, "password": password}).encode()
        return self.req("POST", "/api/login", body=body,
                        headers={"content-type": "application/json"})

    def authed(self, cookie):
        return {"cookie": f"{webserver.SESSION_COOKIE}={cookie}"}

    # -- 测试 --
    def test_login_page_public(self):
        status, _, data = self.req("GET", "/login")
        self.assertEqual(status, 200)
        text = data.decode("utf-8")
        self.assertIn("登录", text)
        # 对抗：登录页不能泄露 API 令牌
        self.assertNotIn(self.app.token, text)
        self.assertNotIn("__NANOCODE_TOKEN__", text)

    def test_root_redirects_to_login_without_session(self):
        status, headers, _ = self.req("GET", "/")
        self.assertEqual(status, 302)
        self.assertEqual(headers.get("location"), "/login")

    def test_api_unauthorized_without_session(self):
        status, _, data = self.req(
            "GET", "/api/state",
            headers={"x-nanocode-token": self.app.token})
        self.assertEqual(status, 401)

    def test_static_requires_session(self):
        status, _, _ = self.req("GET", "/favicon.svg")
        self.assertEqual(status, 302)

    def test_login_wrong_password(self):
        status, _, data = self.req(
            "POST", "/api/login",
            body=json.dumps({"username": TEST_USER,
                             "password": "wrong"}).encode(),
            headers={"content-type": "application/json"})
        self.assertEqual(status, 401)
        self.assertIn("用户名或密码错误", data.decode("utf-8"))

    def test_login_unknown_user_same_error(self):
        """用户名枚举对抗：不存在用户与密码错误回同样的错。"""
        s1, _, d1 = self.login(username="nobody", password="x")
        s2, _, d2 = self.login(username=TEST_USER, password="x")
        self.assertEqual((s1, d1), (s2, d2))

    def test_login_non_json_rejected(self):
        status, _, _ = self.req(
            "POST", "/api/login", body=b"username=a",
            headers={"content-type": "text/plain"})
        self.assertEqual(status, 415)

    def test_full_flow(self):
        status, headers, data = self.login()
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(data)["ok"])
        cookie = self.get_cookie(headers)
        self.assertTrue(cookie)
        set_cookie = headers.get("set-cookie", "")
        self.assertIn("httponly", set_cookie.lower())
        self.assertIn("samesite=lax", set_cookie.lower())

        # 带会话进首页
        status, _, page = self.req("GET", "/", headers=self.authed(cookie))
        self.assertEqual(status, 200)
        self.assertIn(self.app.token, page.decode("utf-8"))

        # 带会话调 API（令牌层仍在）
        status, _, _ = self.req("GET", "/api/state",
                                headers={**self.authed(cookie),
                                         "x-nanocode-token": self.app.token})
        self.assertEqual(status, 200)

        # 伪造的会话进不去
        status, _, _ = self.req("GET", "/",
                                headers=self.authed("forged-token-xxx"))
        self.assertEqual(status, 302)

    def test_logout(self):
        _, headers, _ = self.login()
        cookie = self.get_cookie(headers)
        status, headers2, _ = self.req(
            "POST", "/api/logout", body=b"{}",
            headers={**self.authed(cookie),
                     "content-type": "application/json",
                     "x-nanocode-token": self.app.token})
        self.assertEqual(status, 200)
        # 登出后会话立即失效
        status, _, _ = self.req("GET", "/", headers=self.authed(cookie))
        self.assertEqual(status, 302)

    def test_login_rate_limit(self):
        for _ in range(5):
            status, _, _ = self.login(password="wrong")
            self.assertEqual(status, 401)
        status, _, data = self.login(password="wrong")
        self.assertEqual(status, 429)
        self.assertIn("频繁", data.decode("utf-8"))

    def test_login_fail_expired_keys_pruned(self):
        """对抗：伪造大量客户端 key 刷失败记录，过期条目要被清掉，不能无限增长。"""
        import time as _time
        old = _time.time() - webserver.LOGIN_FAIL_WINDOW - 1
        for i in range(webserver.LOGIN_FAIL_MAX_KEYS):
            self.app._login_fails[f"spoof-{i}"] = [old]
        self.app.login_failed("new-key")
        self.assertLess(len(self.app._login_fails), webserver.LOGIN_FAIL_MAX_KEYS)
        self.assertIn("new-key", self.app._login_fails)

    def test_login_page_redirects_when_logged_in(self):
        _, headers, _ = self.login()
        cookie = self.get_cookie(headers)
        status, h2, _ = self.req("GET", "/login", headers=self.authed(cookie))
        self.assertEqual(status, 302)
        self.assertEqual(h2.get("location"), "/")


@unittest.skipUnless(openssl_available(), "需要 openssl 可执行文件")
class WebLoginDisabledTest(unittest.TestCase):
    """不设 NANOCODE_HTPASSWD 时保持旧行为：无登录，直接进。"""

    def setUp(self):
        self._old_env = dict(os.environ)
        os.environ.pop("NANOCODE_HTPASSWD", None)
        self.ws = Workspace()
        self.session = Session(
            Options(provider="mock", cwd=self.ws.dir, mock_delay=0),
            log=lambda m: None)
        self.server, self.app = make_server(self.session, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.assertFalse(self.app.login_enabled)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.session.close()
        self.ws.cleanup()
        os.environ.clear()
        os.environ.update(self._old_env)

    def test_root_open_without_login(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/", headers={"Host": f"127.0.0.1:{self.port}"})
        r = conn.getresponse()
        r.read()
        conn.close()
        self.assertEqual(r.status, 200)

    def test_login_page_redirects_to_root(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/login", headers={"Host": f"127.0.0.1:{self.port}"})
        r = conn.getresponse()
        r.read()
        conn.close()
        self.assertEqual(r.status, 302)


if __name__ == "__main__":
    unittest.main()
