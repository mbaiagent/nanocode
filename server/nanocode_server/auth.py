"""用户身份：控制面自己不做登录，信任前面的认证代理。

生产部署：浏览器 → 入口（TLS）→ 认证代理（oauth2-proxy 之类，对接企业 SSO / OIDC）→ 控制面。
认证代理登录成功后把用户邮箱放进请求头（默认 X-Auth-Request-Email），控制面直接用它。

风险在于：如果有人能绕过认证代理直接访问控制面，就能随便写这个请求头冒充任何人。所以：
  1. 网络上只允许认证代理访问控制面（K8s NetworkPolicy，见 deploy/）；
  2. 认证代理还要带上一个共享密钥 X-NCS-Proxy-Secret，控制面逐个请求校验。没配密钥控制面拒绝启动。

本机开发时可以设 NCS_DEV_USER：只接受来自本机的请求，并且都当成这个用户。
"""

from __future__ import annotations

import hmac
import ipaddress
import re

from .config import Config

PROXY_SECRET_HEADER = "X-NCS-Proxy-Secret"
_USER = re.compile(r"^[A-Za-z0-9._@+\-]{1,128}$")


class AuthError(Exception):
    pass


def _is_loopback(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


def authenticate(headers, client_ip: str, cfg: Config) -> str:
    """返回用户名（小写），失败抛 AuthError。headers 需要支持不区分大小写的 get（http.server 的 headers 就是）。"""
    if cfg.proxy_secret:
        given = headers.get(PROXY_SECRET_HEADER) or ""
        if not hmac.compare_digest(given.encode(), cfg.proxy_secret.encode()):
            raise AuthError("请求没有经过认证代理")
        user = (headers.get(cfg.auth_header) or "").strip()
    elif cfg.dev_user:
        if not _is_loopback(client_ip):
            raise AuthError("开发模式只接受本机请求")
        user = cfg.dev_user
    else:
        raise AuthError("控制面没有配置认证方式")
    if not user:
        raise AuthError("没有登录")
    if not _USER.match(user):
        raise AuthError("用户名格式不对")
    return user.lower()


__all__ = ["authenticate", "AuthError", "PROXY_SECRET_HEADER"]
