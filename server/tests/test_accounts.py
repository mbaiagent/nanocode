"""P1：原生账号——GitHub OAuth 登录（假 GitHub）、服务端登录会话、邀请码、引导管理员、老数据迁移。"""

import http.client
import os
import tempfile
import threading
import time
import unittest
from urllib.parse import parse_qs, urlparse

import support  # noqa: F401

from fake_github import CLIENT_ID, CLIENT_SECRET, FakeGitHub
from nanocode_server.config import Config
from nanocode_server.platform import Platform
from nanocode_server.security.accounts import AccountError, Accounts, token_hash
from nanocode_server.security.auth import Authenticator
from nanocode_server.security.github import GitHubError, GitHubOAuth, pkce_pair
from nanocode_server.storage import Database, Store
from support import PG_URL, Client, make_config

ADMIN = {"id": 1000001, "login": "octo-admin", "name": "MB", "avatar_url": "https://avatars.example/x.png"}


def profile(i, login):
    return {"id": i, "login": login, "name": login.title(), "avatar_url": "", "email": f"{login}@example.com"}


def github_config(**kw):
    base = dict(auth="github", github_client_id=CLIENT_ID, github_client_secret=CLIENT_SECRET,
                public_url="http://127.0.0.1:8700", admin_github_logins=["octo-admin"], allow_unisolated=True)
    return make_config(**{**base, **kw})


def new_store():
    return Store(Database.open("", tempfile.mkdtemp()))


class ConfigTest(unittest.TestCase):
    def test_auth_must_be_explicit_from_env(self):
        old = os.environ.pop("NCS_AUTH", None)
        try:
            cfg = Config.from_env()
            cfg.secret_key = "s" * 40
            with self.assertRaisesRegex(ValueError, "NCS_AUTH"):
                cfg.validate()
            with self.assertRaisesRegex(ValueError, "NCS_AUTH"):
                cfg.ensure_secret()
        finally:
            if old is not None:
                os.environ["NCS_AUTH"] = old

    def test_github_needs_client_and_public_url(self):
        github_config().validate()
        github_config(public_url="https://nanocode.example.com", host="127.0.0.1", launcher="docker").validate()
        for kw, msg in [({"github_client_secret": ""}, "CLIENT_SECRET"),
                        ({"public_url": ""}, "NCS_PUBLIC_URL"),
                        ({"public_url": "http://nanocode.example.com"}, "https"),
                        ({"public_url": "https://x.example/sub"}, "NCS_PUBLIC_URL"),
                        ({"github_oauth_url": "http://evil.example"}, "NCS_GITHUB_OAUTH_URL"),
                        ({"session_idle": 0}, "NCS_SESSION")]:
            with self.subTest(kw=kw), self.assertRaisesRegex(ValueError, msg):
                github_config(**kw).validate()

    def test_dev_login_still_refused_when_bound_publicly(self):
        with self.assertRaisesRegex(ValueError, "dev"):
            make_config(auth="dev", host="0.0.0.0").validate()

    def test_https_cookies_use_host_prefix_and_secure(self):
        auth = Authenticator(github_config(public_url="https://nanocode.example.com"))
        c = auth.session_cookie_for("tok")
        self.assertTrue(c.startswith("__Host-ncs_sid=tok; Path=/; HttpOnly; SameSite=Lax"), c)
        self.assertIn("; Secure", c)
        self.assertNotIn("Domain", c)
        self.assertTrue(auth.oauth_cookie_for("s", "v", "").startswith("__Host-ncs_oauth="))
        plain = Authenticator(github_config()).session_cookie_for("tok")
        self.assertTrue(plain.startswith("ncs_sid=") and "Secure" not in plain)


class MigrationTest(unittest.TestCase):
    def test_existing_owner_strings_become_legacy_accounts(self):
        """v1 的库：owner 只是字符串。升级后每个出现过的 owner 都有账号，数据原样归属。"""
        path = os.path.join(tempfile.mkdtemp(), "platform.db")
        db = Database.open(f"sqlite:///{path}", "")
        db.execute("CREATE TABLE schema_version (id INTEGER PRIMARY KEY, version INTEGER NOT NULL)")
        from nanocode_server.storage.sql import MIGRATIONS
        for stmt in MIGRATIONS[0]:
            db.execute(stmt)
        db.execute("INSERT INTO schema_version (id, version) VALUES (1, 1)")
        db.execute("INSERT INTO sessions (id, owner, title, model_ref, status, created, last_active) "
                   "VALUES ('s1', 'alice', 't', 'mock', 'stopped', 1, 1)")
        db.execute("INSERT INTO credentials (id, owner, provider, label, protocol, base_url, models, secret, key_hint, "
                   "created) VALUES ('c1', 'bob@example.com', 'deepseek', 'l', 'openai', 'u', '[]', 'x', 'h', 1)")
        db.execute("INSERT INTO usage (owner, day, requests) VALUES ('alice', '2026-10-19', 3)")
        db.close()
        store = Store(Database.open(f"sqlite:///{path}", ""))
        self.addCleanup(store.close)
        self.assertEqual({u.id for u in store.list_users()}, {"alice", "bob@example.com"})
        self.assertEqual(store.user_for_identity("legacy", "alice").id, "alice")
        self.assertEqual([s.id for s in store.list_sessions("alice")], ["s1"])
        store.db.migrate()                                         # 幂等
        self.assertEqual(len(store.list_users()), 2)


class AccountsTest(unittest.TestCase):
    def make_store(self):
        return new_store()

    def setUp(self):
        self.cfg = github_config()
        self.store = self.make_store()
        self.addCleanup(self.store.close)
        self.acc = Accounts(self.cfg, self.store)

    def admin(self):
        return self.acc.github_login(ADMIN)

    def test_bootstrap_admin_needs_no_invite(self):
        u = self.admin()
        self.assertEqual((u.id, u.role, u.login), ("gh-1000001", "admin", "octo-admin"))

    def test_bootstrap_by_numeric_id(self):
        self.cfg.admin_github_logins = ["id:42"]
        self.assertTrue(self.acc.github_login(profile(42, "renamed")).is_admin)

    def test_first_login_without_invite_is_rejected(self):
        with self.assertRaises(AccountError) as e:
            self.acc.github_login(profile(1, "alice"))
        self.assertEqual(e.exception.code, "invite_required")
        with self.assertRaises(AccountError) as e:
            self.acc.github_login(profile(1, "alice"), "AAAA-BBBB-CCCC-DDDD")
        self.assertEqual(e.exception.code, "invite_invalid")
        self.assertEqual(len(self.store.list_users()), 0)

    def test_invite_is_single_use_by_default_and_hashed(self):
        inv, code = self.acc.create_invite(self.admin(), "for alice")
        u = self.acc.github_login(profile(1, "alice"), code.lower())          # 大小写、分隔符都不敏感
        self.assertEqual((u.role, u.invite_id), ("user", inv.id))
        with self.assertRaises(AccountError):
            self.acc.github_login(profile(2, "bob"), code)
        raw = self.store.db.execute("SELECT * FROM invites")[0]
        self.assertNotIn(code.replace("-", ""), str(raw))                   # 明文不进库
        self.assertEqual(raw["uses"], 1)

    def test_returning_user_needs_no_invite_and_profile_is_refreshed(self):
        _, code = self.acc.create_invite(self.admin())
        self.acc.github_login(profile(1, "alice"), code)
        u = self.acc.github_login({**profile(1, "alice2"), "name": "Alice New"})       # 改了名，按数字 id 认
        self.assertEqual((u.id, u.login, u.name), ("gh-1", "alice2", "Alice New"))

    def test_expired_and_revoked_invites(self):
        admin = self.admin()
        inv, code = self.acc.create_invite(admin, expires_days=1, now=time.time() - 2 * 86400)
        with self.assertRaises(AccountError):
            self.acc.github_login(profile(1, "a"), code)
        inv2, code2 = self.acc.create_invite(admin, max_uses=5)
        self.assertTrue(self.store.revoke_invite(inv2.id))
        with self.assertRaises(AccountError):
            self.acc.github_login(profile(1, "a"), code2)
        states = {i.id: i.state(time.time()) for i in self.store.list_invites()}
        self.assertEqual(states, {inv.id: "expired", inv2.id: "revoked"})

    def test_concurrent_redeem_of_single_use_invite(self):
        _, code = self.acc.create_invite(self.admin())
        ok, errors = [], []

        def go(i):
            try:
                ok.append(self.acc.github_login(profile(100 + i, f"u{i}"), code))
            except AccountError:
                errors.append(i)
        threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual((len(ok), len(errors)), (1, 7))

    def test_admin_login_name_squatting_is_not_promoted(self):
        """原管理员改名后，别人注册了 octo-admin 这个名字：不能因为名字对上就成为管理员。"""
        self.admin()
        with self.assertRaises(AccountError):
            self.acc.github_login(profile(999, "octo-admin"))       # 没邀请码 → 拒绝（不是引导管理员）

    def test_disabled_user_cannot_log_in(self):
        _, code = self.acc.create_invite(self.admin())
        u = self.acc.github_login(profile(1, "alice"), code)
        self.store.update_user(u.id, status="disabled")
        with self.assertRaises(AccountError) as e:
            self.acc.github_login(profile(1, "alice"))
        self.assertEqual(e.exception.code, "disabled")

    def test_sessions_are_hashed_revocable_and_expire(self):
        u = self.admin()
        now = time.time()
        token = self.acc.start_session(u, "1.2.3.4", "ua", now)
        self.assertEqual(self.acc.resolve(token, now).id, u.id)
        self.assertFalse(self.store.db.execute("SELECT 1 FROM auth_sessions WHERE token_hash = ?", (token,)))
        self.assertTrue(self.store.db.execute("SELECT 1 FROM auth_sessions WHERE token_hash = ?", (token_hash(token),)))
        self.assertIsNone(self.acc.resolve(token, now + self.cfg.session_idle + 1))        # 闲置太久
        self.assertIsNone(self.acc.resolve(token, now + self.cfg.session_ttl + 1))         # 绝对过期
        self.assertIsNone(self.acc.resolve("x" * 500, now))
        self.acc.end_session(token)
        self.assertIsNone(self.acc.resolve(token, now))
        t2 = self.acc.start_session(u, "", "", now)
        self.store.update_user(u.id, status="disabled")
        self.assertIsNone(self.acc.resolve(t2, now))

    def test_invite_parameter_checks(self):
        admin = self.admin()
        for kw in ({"max_uses": 0}, {"max_uses": 5000}, {"expires_days": -1}, {"expires_days": 1000}):
            with self.subTest(kw=kw), self.assertRaises(AccountError):
                self.acc.create_invite(admin, **kw)

    def test_dev_and_proxy_users_get_accounts(self):
        self.cfg.admin_github_logins = ["root"]
        self.assertEqual(self.acc.ensure_user("alice", "dev").role, "user")
        self.assertEqual(self.acc.ensure_user("root", "dev").role, "admin")
        self.assertEqual(self.acc.ensure_user("alice", "dev").id, "alice")
        self.assertEqual(len(self.store.list_users()), 2)


@unittest.skipUnless(PG_URL, "设置 NCS_TEST_DATABASE_URL 才测 PostgreSQL")
class PostgresAccountsTest(AccountsTest):
    """同一套账号测试跑在 PostgreSQL 上（含并发抢单次邀请码）。"""

    def make_store(self):
        store = Store(Database.open(PG_URL, ""))
        for t in ("auth_sessions", "invites", "identities", "users"):
            store.db.execute(f"DELETE FROM {t}")
        return store


class GitHubClientTest(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        self.addCleanup(self.gh.close)
        self.oauth = GitHubOAuth(CLIENT_ID, CLIENT_SECRET, "http://127.0.0.1:8700/auth/github/callback",
                                 self.gh.url, self.gh.url)

    def test_authorize_url_has_minimal_scopes_and_pkce(self):
        _, challenge = pkce_pair()
        q = parse_qs(urlparse(self.oauth.authorize_url("st", challenge)).query)
        self.assertEqual(q["scope"], ["read:user user:email"])
        self.assertEqual((q["state"], q["code_challenge"], q["code_challenge_method"]), (["st"], [challenge], ["S256"]))
        self.assertEqual(q["redirect_uri"], ["http://127.0.0.1:8700/auth/github/callback"])

    def test_exchange_and_profile(self):
        verifier, challenge = pkce_pair()
        cb = self.gh.authorize_redirect(self.oauth.authorize_url("st", challenge),
                                        {**profile(7, "carol"), "emails": [
                                            {"email": "old@x", "primary": False, "verified": True},
                                            {"email": "carol@x", "primary": True, "verified": True}]})
        code = parse_qs(urlparse(cb).query)["code"][0]
        token = self.oauth.exchange(code, verifier)
        p = self.oauth.profile(token)
        self.assertEqual((p["id"], p["login"], p["email"]), (7, "carol", "carol@x"))
        self.assertTrue(self.gh.requests[-1]["headers"].get("Authorization", "").startswith("Bearer "))

    def test_wrong_verifier_or_code_fails(self):
        _, challenge = pkce_pair()
        cb = self.gh.authorize_redirect(self.oauth.authorize_url("st", challenge), profile(7, "carol"))
        code = parse_qs(urlparse(cb).query)["code"][0]
        with self.assertRaises(GitHubError):
            self.oauth.exchange(code, "not-the-verifier")
        with self.assertRaises(GitHubError):
            self.oauth.profile("bogus-token")


class Browser:
    """最小的浏览器：记 Cookie、不自动跟随跳转。"""

    def __init__(self, port):
        self.port = port
        self.cookies: dict[str, str] = {}

    def request(self, method, path, body=None, headers=None):
        import json as _json
        h = {"Host": f"127.0.0.1:{self.port}"}
        if self.cookies:
            h["cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if body is not None:
            h["content-type"] = "application/json"
        h.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.request(method, path, _json.dumps(body) if body is not None else None, h)
        r = conn.getresponse()
        data = r.read()
        for k, v in r.getheaders():
            if k.lower() == "set-cookie":
                name, _, rest = v.partition("=")
                value = rest.split(";", 1)[0]
                if "Max-Age=0" in v:
                    self.cookies.pop(name, None)
                else:
                    self.cookies[name] = value
        loc = r.getheader("location") or ""
        conn.close()
        try:
            data = _json.loads(data)
        except ValueError:
            pass
        return r.status, data, loc


class GitHubLoginEndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gh = FakeGitHub()
        cls.cfg = github_config(github_oauth_url=cls.gh.url, github_api_url=cls.gh.url, login_per_minute=0)
        cls.platform = Platform(cls.cfg, log=lambda m: None)
        cls.platform.start()
        cls.port = cls.platform.control_port

    @classmethod
    def tearDownClass(cls):
        cls.platform.close()
        cls.gh.close()

    def login(self, user, invite="", browser=None):
        b = browser or Browser(self.port)
        path = "/auth/github" + (f"?invite={invite}" if invite else "")
        status, _, loc = b.request("GET", path)
        self.assertEqual(status, 302)
        self.assertTrue(loc.startswith(self.gh.url + "/login/oauth/authorize?"), loc)
        cb = urlparse(self.gh.authorize_redirect(loc, user))
        status, _, loc = b.request("GET", f"{cb.path}?{cb.query}")
        return b, status, loc

    def test_full_flow_admin_invites_user(self):
        admin, status, loc = self.login({**ADMIN, "emails": []})
        self.assertEqual((status, loc), (302, "/"))
        self.assertIn("ncs_sid", admin.cookies)
        self.assertNotIn("ncs_oauth", admin.cookies)                 # 用完就清
        me = admin.request("GET", "/api/me")[1]
        self.assertEqual((me["user"], me["login"], me["role"], me["auth"]), ("gh-1000001", "octo-admin", "admin", "github"))
        self.assertEqual(admin.request("GET", "/admin")[0], 200)

        # 没邀请码的新人被拒
        _, status, loc = self.login(profile(2, "alice"))
        self.assertEqual(loc, "/login?error=invite_required")

        # 管理员发邀请码 → 新人用它注册
        status, data, _ = admin.request("POST", "/api/admin/invites", {"note": "alice", "max_uses": 1, "expires_days": 3})
        self.assertEqual(status, 201, data)
        code = data["code"]
        alice, status, loc = self.login(profile(2, "alice"), invite=code)
        self.assertEqual(loc, "/")
        self.assertEqual(alice.request("GET", "/api/me")[1]["role"], "user")
        self.assertEqual(alice.request("GET", "/api/admin/invites")[0], 403)
        self.assertEqual(alice.request("GET", "/admin")[0], 403)
        status, data, _ = alice.request("POST", "/api/sessions", {"title": "mine"})
        self.assertEqual(status, 201, data)

        listed = admin.request("GET", "/api/admin/invites")[1]["invites"]
        self.assertEqual([(i["state"], i["uses"]) for i in listed if i["note"] == "alice"], [("used", 1)])
        self.assertNotIn("code", listed[0])
        users = {u["id"]: u for u in admin.request("GET", "/api/admin/users")[1]["users"]}
        self.assertEqual(users["gh-2"]["login"], "alice")

        # 停用 → 立即失效；不能停用自己，也不能停用别的管理员
        self.assertEqual(admin.request("POST", "/api/admin/users/gh-1000001/status", {"status": "disabled"})[0], 400)
        self.platform.store.update_user("gh-2", role="admin")
        self.assertEqual(admin.request("POST", "/api/admin/users/gh-2/status", {"status": "disabled"})[0], 400)
        self.platform.store.update_user("gh-2", role="user")
        self.assertEqual(admin.request("POST", "/api/admin/users/gh-2/status", {"status": "disabled"})[0], 200)
        self.assertEqual(alice.request("GET", "/api/me")[0], 401)
        _, _, loc = self.login(profile(2, "alice"))
        self.assertEqual(loc, "/login?error=disabled")

    def test_logout_revokes_server_side_session(self):
        b, _, _ = self.login({**ADMIN, "emails": []})
        stolen = dict(b.cookies)
        self.assertEqual(b.request("POST", "/api/logout", {})[0], 200)
        thief = Browser(self.port)
        thief.cookies = stolen
        self.assertEqual(thief.request("GET", "/api/me")[0], 401)

    def test_state_mismatch_and_missing_cookie_are_rejected(self):
        b = Browser(self.port)
        _, _, loc = b.request("GET", "/auth/github")
        cb = urlparse(self.gh.authorize_redirect(loc, {**ADMIN, "emails": []}))
        q = parse_qs(cb.query)
        status, _, loc = b.request("GET", f"/auth/github/callback?code={q['code'][0]}&state=forged")
        self.assertEqual(loc, "/login?error=state")
        status, _, loc = Browser(self.port).request("GET", f"/auth/github/callback?{cb.query}")   # 别人的回调链接
        self.assertEqual(loc, "/login?error=state")
        _, _, loc = b.request("GET", "/auth/github")
        state = parse_qs(urlparse(loc).query)["state"][0]
        self.assertEqual(b.request("GET", f"/auth/github/callback?error=access_denied&state={state}")[2],
                         "/login?error=denied")

    def test_github_failure_and_dev_login_disabled(self):
        self.gh.fail_token = True
        try:
            _, _, loc = self.login({**ADMIN, "emails": []})
        finally:
            self.gh.fail_token = False
        self.assertEqual(loc, "/login?error=github")
        b = Browser(self.port)
        self.assertEqual(b.request("POST", "/api/login", {"user": "octo-admin"})[0], 404)
        self.assertEqual(b.request("GET", "/api/auth/info")[1]["mode"], "github")
        self.assertEqual(b.request("GET", "/api/me")[0], 401)
        self.assertEqual(b.request("GET", "/admin")[2], "/login")

    def test_cross_site_post_rejected(self):
        b, _, _ = self.login({**ADMIN, "emails": []})
        status, _, _ = b.request("POST", "/api/admin/invites", {}, headers={"origin": "https://evil.example"})
        self.assertEqual(status, 403)


class LogFailureTest(unittest.TestCase):
    def test_logging_never_breaks_the_request(self):
        """stderr 不是 UTF-8（Windows cp1252）或日志本身出错：登录失败照样跳回登录页。"""
        import logging
        from nanocode_server.web import control

        class Boom(logging.Handler):
            def emit(self, record):
                raise UnicodeEncodeError("cp1252", "换取", 0, 1, "nope")
        h = Boom()
        h.handleError = lambda record: (_ for _ in ()).throw(RuntimeError("handler failed"))
        control._auth_log.addHandler(h)
        try:
            control._log_failure("rid", "github", "换取令牌失败：bad_verification_code")
        finally:
            control._auth_log.removeHandler(h)


class DevAndProxyStatusTest(unittest.TestCase):
    """dev / proxy 模式下停用账号也必须立即生效（不能只靠 github 模式的登录会话检查）。"""

    def test_disabled_dev_user_is_locked_out(self):
        platform = Platform(make_config(admin_github_logins=["root"]), log=lambda m: None)
        platform.start()
        try:
            port = platform.control_port
            root, bob = Client(port, "root"), Client(port, "bob")
            self.assertEqual(bob.request("GET", "/api/me")[0], 200)
            self.assertEqual(root.request("POST", "/api/admin/users/bob/status", {"status": "disabled"})[0], 200)
            self.assertEqual(bob.request("GET", "/api/me")[0], 403)                       # 旧 Cookie 失效
            self.assertEqual(bob.request("POST", "/api/sessions", {"title": "x"})[0], 403)
            again = Client(port)
            self.assertEqual(again.request("POST", "/api/login", {"user": "bob"})[0], 403)  # 重新登录也不行
            self.assertEqual(root.request("POST", "/api/admin/users/bob/status", {"status": "active"})[0], 200)
            self.assertEqual(bob.request("GET", "/api/me")[0], 200)
        finally:
            platform.close()

    def test_disabled_proxy_user_is_rejected_even_with_valid_proxy_headers(self):
        secret = "p" * 24
        cfg = make_config(auth="proxy", proxy_secret=secret, admin_github_logins=["boss@example.com"],
                          allow_unisolated=True)
        platform = Platform(cfg, log=lambda m: None)
        platform.start()
        try:
            port = platform.control_port
            boss = Client(port, "boss@example.com", proxy_secret=secret)
            eve = Client(port, "eve@example.com", proxy_secret=secret)
            self.assertEqual(eve.request("GET", "/api/me")[0], 200)
            self.assertEqual(boss.request("GET", "/api/me")[1]["role"], "admin")
            status, data, _ = boss.request("POST", "/api/admin/users/eve@example.com/status", {"status": "disabled"})
            self.assertEqual(status, 200, data)
            self.assertEqual(eve.request("GET", "/api/me")[0], 403)
            self.assertEqual(eve.request("POST", "/api/sessions", {"title": "x"})[0], 403)
        finally:
            platform.close()


class LoginRateLimitTest(unittest.TestCase):
    def test_login_attempts_are_limited_per_ip(self):
        gh = FakeGitHub()
        cfg = github_config(github_oauth_url=gh.url, github_api_url=gh.url, login_per_minute=3, trust_forwarded=True)
        platform = Platform(cfg, log=lambda m: None)
        platform.start()
        try:
            b = Browser(platform.control_port)
            for _ in range(3):
                self.assertTrue(b.request("GET", "/auth/github", headers={"x-forwarded-for": "9.9.9.9, 1.1.1.1"})[2]
                                .startswith(gh.url))
            self.assertEqual(b.request("GET", "/auth/github", headers={"x-forwarded-for": "8.8.8.8, 1.1.1.1"})[2],
                             "/login?error=rate")                # 按代理追加的最后一项计，伪造前缀没用
            self.assertTrue(b.request("GET", "/auth/github", headers={"x-forwarded-for": "2.2.2.2"})[2].startswith(gh.url))
        finally:
            platform.close()
            gh.close()


if __name__ == "__main__":
    unittest.main()
