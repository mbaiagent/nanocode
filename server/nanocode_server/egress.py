"""出站代理（egress proxy）：沙箱上网的唯一出口，跑在沙箱代理进程里（NCS_EGRESS_PROXY=1）。

沙箱在 internal 网络里，没有 DNS、没有外网，宿主机防火墙只放行网关中继端口和这个代理端口。
沙箱里的 HTTP_PROXY / HTTPS_PROXY 指向这里，web_fetch、web_search、pip、git、curl 都经过它：

  - 只做两件事：CONNECT host:port（HTTPS 隧道）和绝对地址的明文 HTTP 转发（GET http://...）；
    目标端口必须在 NCS_EGRESS_ALLOWED_PORTS 里（默认 80,443），其它协议、端口一律 403。
  - DNS 由代理自己解析：沙箱拿不到任何解析结果，也就没法拿 DNS 当隧道。
  - 防 SSRF：解析出的**每一个**地址都要是公网地址，不能在内网 / 本机 / 链路本地（含云元数据
    169.254.169.254）/ CGNAT / ULA / 组播 / 保留地址里，也不能是 WireGuard 网段、沙箱网段
    （NCS_EGRESS_BLOCK_NETWORKS、NCS_DOCKER_SUBNET）或这台机器自己的地址。有一个不合格就拒绝。
  - 防 DNS 重绑定：检查过哪个地址就连哪个地址（不交给系统再解析一次），连上之后再按对端地址
    复核一遍；HTTP 转发时 Host 头由代理按地址里的域名重写。
  - 域名名单：NCS_EGRESS_DENY_DOMAINS 先拒；NCS_EGRESS_ALLOW_DOMAINS 非空时只放行名单里的
    （都按后缀匹配：example.com 也匹配 a.example.com）。
  - 名额和时限和网关中继同样处理（#126 审查）：全局上限 + 每个沙箱（按来源 IP）上限，
    请求头读取有总时限（防慢速请求头占名额），连接两个方向都空闲超过 idle_timeout 就断开，
    一头 EOF 后另一头停顿超过 half_close_timeout 就断开。
  - 访问日志一条连接一行 JSON：沙箱、方法、域名、端口、连到的地址、结果、上下行字节数、耗时。
    **不记 URL 路径、请求头、请求体、响应**——路径和头里常有令牌。

为什么不是 tinyproxy / squid：tinyproxy 只能按域名/客户端做 ACL，不能在解析之后按目标地址拦，
做不到上面的 SSRF / 重绑定检查；squid 能做（dst ACL），但一个常驻进程就要几十 MB，
在 1 GB 内存、还要跑沙箱的机器上不划算，而且多一个要单独升级、单独配防火墙的组件。
这里用标准库（项目零第三方依赖），和网关中继共用名额 / 超时逻辑，空闲时几乎不占内存。
"""

from __future__ import annotations

import ipaddress
import json
import re
import select
import socket
import threading
import time
from urllib.parse import urlsplit

from nanocode.tools.web import is_public_ip

# 显式列出要求拦住的网段：不全靠 ipaddress 的 is_global（各 Python 版本对个别网段的判断不完全一样）
ALWAYS_BLOCKED = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
    "192.0.0.0/24", "192.168.0.0/16", "198.18.0.0/15", "224.0.0.0/3",
    "::/128", "::1/128", "fc00::/7", "fe80::/10", "ff00::/8"))

HOSTNAME = re.compile(r"(?=.{1,253}$)[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?(\.[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?)*")
METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
HOP_BY_HOP = {"connection", "proxy-connection", "keep-alive", "proxy-authorization", "proxy-authenticate",
              "te", "trailer", "upgrade", "host"}


class Denied(Exception):
    """拒绝这条请求：status 回给客户端，reason 是短的 ASCII 说明（会出现在状态行和日志里）。"""

    def __init__(self, status: int, reason: str, ip: str = ""):
        super().__init__(reason)
        self.status, self.reason, self.ip = status, reason, ip


def _unwrap(addr):
    """IPv4 映射 / NAT64 / 6to4 里嵌着的 IPv4 按 IPv4 判断（和 web_fetch 的 is_public_ip 一致）。"""
    if addr.version == 6:
        if addr.ipv4_mapped:
            return addr.ipv4_mapped
        if addr in ipaddress.ip_network("64:ff9b::/96"):
            return ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
        if addr in ipaddress.ip_network("2002::/16"):
            return ipaddress.IPv4Address((int(addr) >> 80) & 0xFFFFFFFF)
    return addr


def host_addresses() -> set[str]:
    """这台机器自己的地址（含公网地址）：沙箱不能借代理绕回宿主机（sshd、面板……）。

    只用标准库：Linux 上读 /proc/net/fib_trie（IPv4 本地地址）和 /proc/net/if_inet6；
    其它系统读不到就是空集，靠 ALWAYS_BLOCKED 和 NCS_EGRESS_BLOCK_NETWORKS 兜住。
    """
    found: set[str] = set()
    try:
        with open("/proc/net/fib_trie", encoding="ascii", errors="replace") as f:
            prev = ""
            for line in f:
                if "/32 host LOCAL" in line:
                    ip = prev.strip().lstrip("|-+` ").strip()
                    try:
                        found.add(str(ipaddress.IPv4Address(ip)))
                    except ValueError:
                        pass
                prev = line
    except OSError:
        pass
    try:
        with open("/proc/net/if_inet6", encoding="ascii", errors="replace") as f:
            for line in f:
                raw = line.split()[0] if line.split() else ""
                if len(raw) == 32:
                    found.add(str(ipaddress.IPv6Address(int(raw, 16))))
    except (OSError, ValueError):
        pass
    return found


def _norm_domains(items) -> tuple[str, ...]:
    out = []
    for d in items or ():
        d = d.strip().lower().rstrip(".")
        while d.startswith(("*.", ".")):
            d = d[1:] if d.startswith(".") else d[2:]
        if d:
            out.append(d)
    return tuple(out)


def _matches(host: str, domains: tuple[str, ...]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def pump(a: socket.socket, b: socket.socket, idle_timeout: float, half_close_timeout: float,
         stop: threading.Event) -> tuple[int, int]:
    """双向转发到两边都结束为止，返回 (a→b, b→a) 字节数。

    网关中继和出站代理共用（#126 审查）：两个方向都没数据超过 idle_timeout 就断开；
    一头 EOF 后另一头停顿超过 half_close_timeout 就断开；写不出去由调用方设的 socket 超时兜住。
    """
    peer = {a: b, b: a}
    sent = {a: 0, b: 0}
    reading = {a, b}
    last = time.monotonic()
    eof_at = None
    while reading and not stop.is_set():
        now = time.monotonic()
        deadline = last + idle_timeout
        if eof_at is not None:
            deadline = min(deadline, max(eof_at, last) + half_close_timeout)
        wait = deadline - now
        if wait <= 0:
            break
        try:
            ready, _, _ = select.select(list(reading), [], [], min(wait, 1.0))
        except (OSError, ValueError):
            break
        failed = False
        for sk in ready:
            try:
                data = sk.recv(65536)
                if data:
                    peer[sk].sendall(data)
                    sent[sk] += len(data)
                    last = time.monotonic()
                    continue
            except OSError:
                failed = True
                break
            reading.discard(sk)                     # 这一头发完了：把 EOF 传给另一头（半关闭）
            if eof_at is None:
                eof_at = time.monotonic()
            try:
                peer[sk].shutdown(socket.SHUT_WR)
            except OSError:
                pass
        if failed:
            break
    return sent[a], sent[b]


def _close(*socks) -> None:
    for sk in socks:
        if sk is not None:
            try:
                sk.close()
            except OSError:
                pass


class EgressProxy:
    """HTTP/HTTPS 正向代理，带 SSRF 防护、名额和时限。见模块说明。

    resolver(host, port) -> [ip, ...] 和 allow_networks 只给测试用（不走真实 DNS、连本机测试服务）；
    部署时不开任何例外。
    """

    def __init__(self, host: str, port: int, allowed_ports=(80, 443), block_networks=(), allow_domains=(),
                 deny_domains=(), max_connections: int = 64, per_source: int = 8, idle_timeout: float = 120,
                 half_close_timeout: float = 30, header_timeout: float = 10, connect_timeout: float = 10,
                 max_header_bytes: int = 16384, who=None, log=print, resolver=None, allow_networks=(),
                 local_addresses=None):
        self.allowed_ports = frozenset(int(p) for p in allowed_ports)
        self.block_networks = tuple(ipaddress.ip_network(n, strict=False) for n in block_networks)
        self.allow_networks = tuple(ipaddress.ip_network(n, strict=False) for n in allow_networks)
        self.allow_domains = _norm_domains(allow_domains)
        self.deny_domains = _norm_domains(deny_domains)
        self.max_connections = max_connections
        self.per_source = per_source
        self.idle_timeout = idle_timeout
        self.half_close_timeout = half_close_timeout
        self.header_timeout = header_timeout
        self.connect_timeout = connect_timeout
        self.max_header_bytes = max_header_bytes
        self.who = who or (lambda ip: "")
        self.log = log
        self.resolver = resolver or self._resolve
        self._local_fn = local_addresses or host_addresses
        self._local: set[str] = set()
        self._local_at = 0.0
        self._count = 0
        self._by_source: dict[str, int] = {}
        self._guard = threading.Lock()
        self._stop = threading.Event()
        self._sock = socket.create_server((host, port))
        self.address = self._sock.getsockname()[:2]

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> "EgressProxy":
        threading.Thread(target=self._accept, daemon=True, name="出站代理").start()
        return self

    def close(self) -> None:
        self._stop.set()
        _close(self._sock)

    def active(self) -> int:
        with self._guard:
            return self._count

    @property
    def url(self) -> str:
        host, port = self.address
        return f"http://{'[' + host + ']' if ':' in host else host}:{port}"

    # ------------------------------------------------------------------ 名额

    def _admit(self, source: str) -> bool:
        with self._guard:
            if self._count >= self.max_connections or self._by_source.get(source, 0) >= self.per_source:
                return False
            self._count += 1
            self._by_source[source] = self._by_source.get(source, 0) + 1
            return True

    def _release(self, source: str) -> None:
        with self._guard:
            self._count -= 1
            n = self._by_source.get(source, 0) - 1
            if n > 0:
                self._by_source[source] = n
            else:
                self._by_source.pop(source, None)

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                client, addr = self._sock.accept()
            except OSError:
                return
            source = addr[0]
            if not self._admit(source):
                try:                                    # 不阻塞 accept 线程：发得出去就告诉它，发不出去就算了
                    client.setblocking(False)
                    client.send(self._error_bytes(503, "Too many connections from this sandbox"))
                except OSError:
                    pass
                _close(client)
                self._write_log(source, "-", "", 0, "", "denied:too-many-connections", 0, 0, time.monotonic())
                continue
            threading.Thread(target=self._serve, args=(client, source), daemon=True).start()

    # ------------------------------------------------------------------ 地址检查

    def _host_ips(self) -> set[str]:
        now = time.monotonic()
        if now - self._local_at > 60:                   # 地址会变（DHCP、加网卡）：一分钟刷新一次
            try:
                self._local = set(self._local_fn())
            except Exception:  # noqa: BLE001 —— 读不到就保持上一次的
                pass
            self._local_at = now
        return self._local

    def blocked(self, ip: str) -> str:
        """不允许连的地址返回原因（ASCII），允许返回空串。"""
        try:
            addr = ipaddress.ip_address(ip.split("%")[0])
        except ValueError:
            return "not an IP address"
        if any(addr in n for n in self.allow_networks):
            return ""
        inner = _unwrap(addr)
        for a in {addr, inner}:
            if any(a.version == n.version and a in n for n in ALWAYS_BLOCKED + self.block_networks):
                return "private or internal address"
        if not is_public_ip(str(addr)):
            return "private or internal address"
        local = self._host_ips()
        if str(addr) in local or str(inner) in local:
            return "address of the sandbox host"
        return ""

    @staticmethod
    def _resolve(host: str, port: int) -> list[str]:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        return list(dict.fromkeys(i[4][0] for i in infos))

    def _check_host(self, host: str) -> str:
        """规范化并检查目标主机名（域名名单）。返回小写的主机名 / IP 字面量。"""
        host = host.strip().lower()
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        host = host.rstrip(".")
        try:
            ipaddress.ip_address(host)
            is_ip = True
        except ValueError:
            is_ip = False
            if not HOSTNAME.fullmatch(host):
                raise Denied(400, "Invalid host name")
        if self.deny_domains and _matches(host, self.deny_domains):
            raise Denied(403, "Domain is on the denylist")
        if self.allow_domains and (is_ip or not _matches(host, self.allow_domains)):
            raise Denied(403, "Domain is not on the allowlist")
        return host

    def _connect(self, host: str, port: int) -> tuple[socket.socket, str]:
        if port not in self.allowed_ports:
            raise Denied(403, f"Port {port} is not allowed")
        try:
            ips = list(self.resolver(host, port))
        except (OSError, UnicodeError):
            raise Denied(502, "DNS lookup failed") from None
        if not ips:
            raise Denied(502, "DNS lookup failed")
        for ip in ips:                                  # 有一个不合格就整个拒绝（混合应答是重绑定的常见手法）
            why = self.blocked(ip)
            if why:
                raise Denied(403, f"Destination is a {why}" if why.startswith("private") else f"Destination is the {why}",
                             ip)
        last = "connect failed"
        for ip in ips[:4]:
            try:
                # 连的是检查过的那个 IP 字面量：create_connection 不会再查一次 DNS
                upstream = socket.create_connection((ip, port), timeout=self.connect_timeout)
            except socket.timeout:
                last = "connect timed out"
                continue
            except OSError:
                continue
            try:
                peer = upstream.getpeername()[0]
            except OSError:
                _close(upstream)
                continue
            if self.blocked(peer):                      # 复核真正连上的对端
                _close(upstream)
                raise Denied(403, "Destination is a private or internal address", peer)
            return upstream, peer
        raise Denied(504 if "timed out" in last else 502, last.capitalize())

    # ------------------------------------------------------------------ 一条连接

    def _read_head(self, client: socket.socket) -> tuple[bytes, bytes]:
        deadline = time.monotonic() + self.header_timeout
        buf = b""
        while True:
            end = buf.find(b"\r\n\r\n")
            if end >= 0:
                return buf[:end], buf[end + 4:]
            if len(buf) > self.max_header_bytes:
                raise Denied(431, "Request header too large")
            left = deadline - time.monotonic()
            if left <= 0:
                raise Denied(408, "Request header timeout")
            client.settimeout(left)
            try:
                chunk = client.recv(4096)
            except socket.timeout:
                raise Denied(408, "Request header timeout") from None
            if not chunk:
                raise Denied(400, "Connection closed before request")
            buf += chunk

    @staticmethod
    def _error_bytes(status: int, reason: str) -> bytes:
        body = f"{status} {reason}\n".encode()
        return (f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/plain; charset=utf-8\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\nX-Nanocode-Egress: denied\r\n\r\n").encode() + body

    def _serve(self, client: socket.socket, source: str) -> None:
        started = time.monotonic()
        method, host, port, ip, result, up, down = "-", "", 0, "", "ok", 0, 0
        upstream = None
        try:
            try:
                head, rest = self._read_head(client)
                lines = head.decode("latin-1").split("\r\n")
                parts = lines[0].split(" ")
                if len(parts) != 3 or parts[2] not in ("HTTP/1.1", "HTTP/1.0"):
                    raise Denied(400, "Malformed request line")
                method, target, version = parts
                if method == "CONNECT":
                    host, port = self._authority(target)
                    host = self._check_host(host)
                    upstream, ip = self._connect(host, port)
                    client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                    first = rest
                else:
                    if method not in METHODS:
                        raise Denied(405, "Method not allowed")
                    method = "HTTP"                     # 日志里不记具体方法以外的东西（路径、头都不记）
                    host, port, first = self._rewrite(parts[0], target, version, lines[1:], rest)
                    host = self._check_host(host)
                    upstream, ip = self._connect(host, port)
            except Denied as e:
                result = f"{'denied' if e.status < 500 else 'error'}:{e.reason}"
                ip = e.ip or ip
                try:
                    client.settimeout(5)
                    client.sendall(self._error_bytes(e.status, e.reason))
                    # 先半关闭再把没读完的请求读掉一点：直接 close 会回 RST，客户端可能看不到这条错误
                    client.shutdown(socket.SHUT_WR)
                    client.settimeout(0.2)
                    for _ in range(16):
                        if not client.recv(65536):
                            break
                except OSError:
                    pass
                return
            except (OSError, UnicodeError):
                result = "error:client"
                return
            for sk in (client, upstream):
                sk.settimeout(self.idle_timeout)       # sendall 发不出去时也按空闲超时断开
            try:
                if first:
                    upstream.sendall(first)
                    up += len(first)
            except OSError:
                result = "error:upstream"
                return
            a, b = pump(client, upstream, self.idle_timeout, self.half_close_timeout, self._stop)
            up, down = up + a, b
        finally:
            _close(client, upstream)
            self._release(source)
            self._write_log(source, method, host, port, ip, result, up, down, started)

    @staticmethod
    def _authority(target: str) -> tuple[str, int]:
        m = re.fullmatch(r"(\[[0-9A-Fa-f:.]+\]|[^\[\]:/@\s]+):(\d{1,5})", target)
        if not m or not 0 < int(m.group(2)) < 65536:
            raise Denied(400, "CONNECT target must be host:port")
        return m.group(1), int(m.group(2))

    @staticmethod
    def _rewrite(method: str, target: str, version: str, header_lines: list[str], rest: bytes):
        """明文 HTTP：绝对地址改成 origin-form，去掉逐跳头，Host 按地址重写，一条连接只做一个请求。"""
        try:
            u = urlsplit(target)
            port = u.port or 80
        except ValueError:
            raise Denied(400, "Invalid URL") from None
        if u.scheme.lower() != "http" or not u.hostname:
            raise Denied(400, "Only absolute http:// URLs (use CONNECT for https)")
        if u.username is not None or u.password is not None or "@" in u.netloc:
            raise Denied(400, "Credentials in URL are not allowed")
        headers = []
        drop = set(HOP_BY_HOP)
        for line in header_lines:
            if "\r" in line or "\n" in line:          # 单独的 CR / LF：请求走私的常见手法
                raise Denied(400, "Malformed header")
            name, sep, value = line.partition(":")
            if not sep or not name or name != name.strip():
                raise Denied(400, "Malformed header")
            if name.lower() == "connection":
                drop.update(t.strip().lower() for t in value.split(","))
        for line in header_lines:
            name, _, value = line.partition(":")
            if name.lower() not in drop:
                headers.append(f"{name}:{value}")
        host_header = u.hostname if ":" not in u.hostname else f"[{u.hostname}]"
        if u.port:
            host_header += f":{u.port}"
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        head = [f"{method} {path} {version}", f"Host: {host_header}", *headers, "Connection: close"]
        return u.hostname, port, ("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + rest

    def _write_log(self, source, method, host, port, ip, result, up, down, started) -> None:
        try:
            self.log(json.dumps({"egress": self.who(source) or "?", "src": source, "method": method,
                                 "host": host, "port": port, "ip": ip, "result": result, "up": up, "down": down,
                                 "ms": int((time.monotonic() - started) * 1000)}, ensure_ascii=False))
        except Exception:  # noqa: BLE001 —— 日志失败不影响转发
            pass


def proxy_env(proxy_url: str, no_proxy_hosts) -> dict:
    """注入沙箱容器的代理环境变量：大小写两种都给（curl 只认小写 http_proxy，有的工具只认大写）。

    NO_PROXY 必须包含网关（中继）地址：模型调用不能绕到出站代理上去——那边会因为是内网地址拒绝。
    NANOCODE_EGRESS_PROXY=1 告诉沙箱里的 web_fetch / web_search：代理会自己解析并做内网检查，
    沙箱里没有 DNS，不要先在本地解析。
    """
    hosts = list(dict.fromkeys(h for h in ("localhost", "127.0.0.1", "::1", *no_proxy_hosts) if h))
    no_proxy = ",".join(hosts)
    return {"HTTP_PROXY": proxy_url, "HTTPS_PROXY": proxy_url, "http_proxy": proxy_url, "https_proxy": proxy_url,
            "NO_PROXY": no_proxy, "no_proxy": no_proxy, "NANOCODE_EGRESS_PROXY": "1"}


__all__ = ["EgressProxy", "Denied", "pump", "proxy_env", "host_addresses", "ALWAYS_BLOCKED"]
