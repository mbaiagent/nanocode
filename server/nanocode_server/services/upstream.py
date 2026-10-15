"""和真实模型 API 打交道：拼地址和请求头、安全检查、流式读取、从响应里读 token 用量。

只有模型网关调用这里，控制面不直接访问任何模型厂商。
"""

from __future__ import annotations

import http.client
import json
import socket
from urllib.parse import urlparse

from nanocode.tools.web import is_public_ip

from ..models import Upstream


class UpstreamError(Exception):
    pass


def check_base_url(url: str, allow_private: bool) -> str | None:
    """用户填写的上游地址必须指向公网（除非明确允许内网，例如本机开发时用 Ollama）。

    网关在内网里，如果让用户把地址指向 http://内网服务/ 或云厂商元数据地址，网关就会替他去访问，
    并把结果流式返回——这就是 SSRF。所以保存时检查一次，每次请求前再检查一次（防 DNS 记录被换掉）。
    """
    u = urlparse(url)
    if u.scheme not in ("https", "http") or not u.hostname or u.username or u.password:
        raise UpstreamError("地址必须是 http(s)://主机名/路径，不能带用户名密码")
    if allow_private:
        return None
    if u.scheme != "https":
        raise UpstreamError("只允许 https 地址")
    try:
        ips = list(dict.fromkeys(i[4][0] for i in socket.getaddrinfo(
            u.hostname, u.port or 443, type=socket.SOCK_STREAM)))
    except socket.gaierror as e:
        raise UpstreamError(f"无法解析域名 {u.hostname}：{e}") from None
    if not ips or not all(is_public_ip(ip) for ip in ips):
        raise UpstreamError(f"{u.hostname} 解析到了内网或保留地址，不允许")
    # 把本次校验得到的 IP 返回给连接层固定使用，避免连接时再次解析域名。
    return ips[0]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """连接到已校验的 IP，同时按原始域名发送 Host 并校验 TLS 证书。"""

    def __init__(self, host: str, port: int | None, ip: str, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self._pinned_ip = ip

    def connect(self) -> None:
        sock = socket.create_connection((self._pinned_ip, self.port), self.timeout, self.source_address)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def endpoint_path(up: Upstream) -> str:
    base = urlparse(up.base_url).path.rstrip("/")
    return base + ("/chat/completions" if up.protocol == "openai" else "/v1/messages")


def request_headers(up: Upstream, incoming=None) -> dict:
    incoming = incoming or {}
    h = {"content-type": "application/json", "accept": incoming.get("accept") or "application/json",
         **up.extra_headers}
    if up.protocol == "openai":
        h["authorization"] = f"Bearer {up.api_key}"
    else:
        h["x-api-key"] = up.api_key
        h["anthropic-version"] = incoming.get("anthropic-version") or "2023-06-01"
        if incoming.get("anthropic-beta"):
            h["anthropic-beta"] = incoming["anthropic-beta"]
    return h


def open_request(up: Upstream, body: dict, incoming=None, allow_private: bool = False, timeout: float = 300):
    """发出请求，返回 (连接, 响应)。调用方负责关闭连接。"""
    pinned_ip = check_base_url(up.base_url, allow_private)
    u = urlparse(up.base_url)
    if pinned_ip:
        conn = _PinnedHTTPSConnection(u.hostname, u.port, pinned_ip, timeout)
    else:
        cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        conn = cls(u.hostname, u.port, timeout=timeout)
    try:
        conn.request("POST", endpoint_path(up), body=json.dumps(body).encode(), headers=request_headers(up, incoming))
        return conn, conn.getresponse()
    except (OSError, http.client.HTTPException) as e:
        conn.close()
        raise UpstreamError(f"连不上上游：{type(e).__name__}: {e}") from None


def probe(up: Upstream, allow_private: bool = False) -> dict:
    """用最小的请求试一下 Key 和模型能不能用。"""
    body = {"model": up.model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}   # 两种协议都接受
    try:
        conn, resp = open_request(up, body, allow_private=allow_private, timeout=30)
    except UpstreamError as e:
        return {"ok": False, "error": str(e)}
    try:
        data = resp.read()[:2000].decode("utf-8", "replace")
    finally:
        conn.close()
    if resp.status == 200:
        return {"ok": True}
    return {"ok": False, "error": f"HTTP {resp.status}：{data[:300]}"}


class UsageMeter:
    """从上游响应里读出 token 用量：流式和非流式、OpenAI 和 Anthropic 两种协议。"""

    def __init__(self, protocol: str):
        self.protocol = protocol
        self.input_tokens = 0
        self.output_tokens = 0

    def feed_sse_line(self, line: bytes) -> None:
        line = line.strip()
        if not line.startswith(b"data:"):
            return
        payload = line[5:].strip()
        if not payload or payload == b"[DONE]":
            return
        try:
            self.feed_json(json.loads(payload))
        except ValueError:
            pass

    def feed_json(self, data) -> None:
        if not isinstance(data, dict):
            return
        if self.protocol == "openai":
            u = data.get("usage") or {}
            if u:
                self.input_tokens = u.get("prompt_tokens") or self.input_tokens
                self.output_tokens = u.get("completion_tokens") or self.output_tokens
            return
        u = (data.get("message") or {}).get("usage") or data.get("usage") or {}
        if u.get("input_tokens") is not None:
            cached = (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
            self.input_tokens = (u.get("input_tokens") or 0) + cached
        if u.get("output_tokens") is not None:
            self.output_tokens = u.get("output_tokens") or 0


__all__ = ["UpstreamError", "check_base_url", "open_request", "probe", "UsageMeter", "endpoint_path"]
