"""假的 GitHub（OAuth 授权页 + 换令牌 + /user + /user/emails），测试和冒烟共用。

    FakeGitHub()                 起在随机端口
    gh.users["code-x"] = {...}   授权码 → 用户资料；换令牌时要带对 client_secret 和 PKCE verifier
    gh.authorize_redirect(url)   模拟用户在授权页点"同意"：返回 GitHub 会跳回的回调地址

也可以单独跑：python3 server/tests/fake_github.py 9999 （冒烟用：授权页直接带 ?login= 指定身份）
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

CLIENT_ID = "test-client-id"
CLIENT_SECRET = "test-client-secret"


class FakeGitHub:
    def __init__(self, port: int = 0, host: str = "127.0.0.1"):
        self.users: dict[str, dict] = {}          # code → 用户资料
        self.pending: dict[str, dict] = {}        # code → {challenge, redirect_uri}
        self.tokens: dict[str, dict] = {}         # access_token → 用户资料
        self.requests: list[dict] = []
        self.fail_token = False
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, data, headers=None):
                body = json.dumps(data).encode() if not isinstance(data, bytes) else data
                self.send_response(status)
                for k, v in (headers or {"content-type": "application/json"}).items():
                    self.send_header(k, v)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                outer.requests.append({"method": "GET", "path": u.path, "query": q, "headers": dict(self.headers)})
                if u.path == "/login/oauth/authorize":
                    # 独立运行（冒烟）时：?login=xxx 直接"同意"并跳回回调
                    login = q.get("login") or "octocat"
                    uid = int(hashlib.sha256(login.encode()).hexdigest()[:7], 16)
                    code = outer.grant(q, {"id": uid, "login": login, "name": login.title(),
                                           "avatar_url": "", "emails": [{"email": f"{login}@example.com",
                                                                         "primary": True, "verified": True}]})
                    loc = q["redirect_uri"] + "?" + urlencode({"code": code, "state": q.get("state", "")})
                    return self._send(302, b"", {"location": loc})
                token = (self.headers.get("authorization") or "")[7:]
                user = outer.tokens.get(token)
                if not user:
                    return self._send(401, {"message": "Bad credentials"})
                if u.path == "/user":
                    return self._send(200, {k: user[k] for k in ("id", "login", "name", "avatar_url")} | {"email": None})
                if u.path == "/user/emails":
                    return self._send(200, user.get("emails", []))
                return self._send(404, {"message": "Not Found"})

            def do_POST(self):
                n = int(self.headers.get("content-length") or 0)
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode()).items()}
                outer.requests.append({"method": "POST", "path": self.path, "form": form, "headers": dict(self.headers)})
                if self.path != "/login/oauth/access_token":
                    return self._send(404, {"message": "Not Found"})
                if outer.fail_token:
                    return self._send(200, {"error": "bad_verification_code"})
                code = form.get("code", "")
                p = outer.pending.pop(code, None)
                if not p or form.get("client_id") != CLIENT_ID or form.get("client_secret") != CLIENT_SECRET:
                    return self._send(200, {"error": "bad_verification_code"})
                verifier = form.get("code_verifier", "")
                challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                if challenge != p["challenge"] or form.get("redirect_uri") != p["redirect_uri"]:
                    return self._send(200, {"error": "bad_verification_code"})
                token = f"gho_{code}"
                outer.tokens[token] = outer.users.pop(code)
                return self._send(200, {"access_token": token, "token_type": "bearer", "scope": "read:user,user:email"})

        self.server = ThreadingHTTPServer((host, port), H)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def grant(self, query: dict, user: dict) -> str:
        code = f"code{len(self.requests)}x{len(self.pending)}x{user['id']}"
        self.users[code] = user
        self.pending[code] = {"challenge": query.get("code_challenge", ""), "redirect_uri": query.get("redirect_uri", "")}
        return code

    def authorize_redirect(self, authorize_url: str, user: dict) -> str:
        q = {k: v[0] for k, v in parse_qs(urlparse(authorize_url).query).items()}
        code = self.grant(q, user)
        return q["redirect_uri"] + "?" + urlencode({"code": code, "state": q.get("state", "")})

    def close(self):
        self.server.shutdown()
        self.server.server_close()


if __name__ == "__main__":
    gh = FakeGitHub(int(sys.argv[1]) if len(sys.argv) > 1 else 9999, "127.0.0.1")
    print(f"fake GitHub on {gh.url}", flush=True)
    threading.Event().wait()
