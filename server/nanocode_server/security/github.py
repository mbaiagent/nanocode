"""GitHub OAuth（OAuth App，授权码 + PKCE）。只用标准库。

流程：
    1. authorize_url(state, challenge)   浏览器跳到 GitHub 授权页
    2. GitHub 带着 code 和 state 回到 /auth/github/callback
    3. exchange(code, verifier)          用 code 换 access_token（带 client_secret 和 PKCE verifier）
    4. profile(token)                    读 /user 和 /user/emails，拿到数字 id、login、主邮箱

只申请 read:user 和 user:email：不碰用户的仓库。access_token 用完即丢，不保存。
地址可配置（NCS_GITHUB_OAUTH_URL / NCS_GITHUB_API_URL），测试和冒烟时指向假的 GitHub。
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import urllib.error
import urllib.request
from urllib.parse import urlencode

SCOPES = "read:user user:email"
TIMEOUT = 15


class GitHubError(Exception):
    pass


def pkce_pair() -> tuple[str, str]:
    """(verifier, S256 challenge)"""
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class GitHubOAuth:
    def __init__(self, client_id: str, client_secret: str, redirect_uri: str,
                 oauth_url: str = "https://github.com", api_url: str = "https://api.github.com"):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.oauth_url = oauth_url.rstrip("/")
        self.api_url = api_url.rstrip("/")

    def authorize_url(self, state: str, challenge: str) -> str:
        q = urlencode({"client_id": self.client_id, "redirect_uri": self.redirect_uri, "scope": SCOPES,
                       "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
                       "allow_signup": "false"})
        return f"{self.oauth_url}/login/oauth/authorize?{q}"

    def _call(self, req: urllib.request.Request):
        req.add_header("accept", req.get_header("Accept") or "application/json")
        req.add_header("user-agent", "nanocode-server")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            raise GitHubError(f"GitHub 返回 HTTP {e.code}") from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise GitHubError(f"连不上 GitHub：{e}") from None

    def exchange(self, code: str, verifier: str) -> str:
        body = urlencode({"client_id": self.client_id, "client_secret": self.client_secret, "code": code,
                          "redirect_uri": self.redirect_uri, "code_verifier": verifier}).encode()
        data = self._call(urllib.request.Request(
            f"{self.oauth_url}/login/oauth/access_token", data=body, method="POST",
            headers={"content-type": "application/x-www-form-urlencoded", "Accept": "application/json"}))
        if not isinstance(data, dict) or not data.get("access_token"):
            err = (data or {}).get("error", "没有 access_token") if isinstance(data, dict) else "响应格式不对"
            raise GitHubError(f"换取令牌失败：{err}")
        return str(data["access_token"])

    def _get(self, token: str, path: str):
        return self._call(urllib.request.Request(f"{self.api_url}{path}", headers={
            "authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "x-github-api-version": "2022-11-28"}))

    def profile(self, token: str) -> dict:
        u = self._get(token, "/user")
        if not isinstance(u, dict) or not isinstance(u.get("id"), int) or not u.get("login"):
            raise GitHubError("GitHub 用户信息格式不对")
        email = ""
        try:
            emails = self._get(token, "/user/emails")
            if isinstance(emails, list):
                email = next((e.get("email", "") for e in emails
                              if isinstance(e, dict) and e.get("primary") and e.get("verified")), "")
        except GitHubError:
            pass                    # 邮箱拿不到不影响登录
        return {"id": u["id"], "login": str(u["login"]), "name": str(u.get("name") or ""),
                "avatar_url": str(u.get("avatar_url") or ""), "email": str(email or "")}


__all__ = ["GitHubOAuth", "GitHubError", "pkce_pair", "SCOPES"]
