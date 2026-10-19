"""S3 兼容的对象存储客户端（工作区快照用），只用标准库。

为什么不用 boto3：项目约定零第三方依赖（AGENTS.md），而快照只需要五个操作——
预签名 PUT/GET（给真正搬数据的那一方）、列目录、删对象、HEAD。SigV4 签名本身只有几十行，
自己实现比引入 boto3 + botocore（几十 MB）划算；单机模式不开对象存储时这个模块根本不会被用到。

只用 path-style 地址（<endpoint>/<bucket>/<key>）：Backblaze B2、MinIO、Cloudflare R2、
AWS S3 都支持，不用为每家配虚拟主机域名。

安全：
  - 访问密钥只在控制面进程里（NCS_S3_ACCESS_KEY_ID / NCS_S3_SECRET_ACCESS_KEY），
    沙箱主机和沙箱容器拿到的永远只是某一个对象、有效期很短的预签名地址；
  - 报错信息里不带预签名地址的查询串（里面有签名），见 redact()。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import quote, urlparse

UNSIGNED = "UNSIGNED-PAYLOAD"
_EMPTY_SHA = hashlib.sha256(b"").hexdigest()


class ObjectStoreError(Exception):
    pass


def redact(url: str) -> str:
    """去掉预签名地址的查询串：日志和报错里只留 scheme://host/path。"""
    return url.split("?", 1)[0]


def _uri_encode(s: str, slash: bool) -> str:
    return quote(s, safe="-_.~" + ("/" if not slash else ""))


@dataclass
class S3Object:
    key: str
    size: int
    last_modified: str


class S3Client:
    def __init__(self, endpoint: str, region: str, bucket: str, access_key: str, secret_key: str,
                 timeout: float = 30):
        u = urlparse(endpoint.rstrip("/"))
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError("NCS_S3_ENDPOINT 必须是 http(s)://主机[:端口]")
        self.scheme = u.scheme
        self.host = u.netloc                     # 签名用的 Host 要和实际请求头一致（带非默认端口）
        self.base_path = u.path.rstrip("/")
        self.region = region or "us-east-1"
        self.bucket = bucket
        self.access_key = access_key
        self.secret_key = secret_key
        self.timeout = timeout

    # ------------------------------------------------------------------ SigV4

    def _path(self, key: str) -> str:
        return f"{self.base_path}/{self.bucket}" + (f"/{key}" if key else "/")

    def _scope(self, now: _dt.datetime) -> tuple[str, str, str]:
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        day = amz_date[:8]
        return amz_date, day, f"{day}/{self.region}/s3/aws4_request"

    def _signing_key(self, day: str) -> bytes:
        k = hmac.new(("AWS4" + self.secret_key).encode(), day.encode(), hashlib.sha256).digest()
        for part in (self.region, "s3", "aws4_request"):
            k = hmac.new(k, part.encode(), hashlib.sha256).digest()
        return k

    def signature(self, method: str, path: str, query: dict[str, str], headers: dict[str, str],
                  payload_hash: str, amz_date: str) -> tuple[str, str]:
        """返回 (签名, SignedHeaders)。headers 的名字要小写，必须含 host。"""
        day = amz_date[:8]
        scope = f"{day}/{self.region}/s3/aws4_request"
        canonical_query = "&".join(f"{_uri_encode(k, True)}={_uri_encode(v, True)}"
                                   for k, v in sorted(query.items()))
        names = sorted(headers)
        canonical_headers = "".join(f"{n}:{' '.join(str(headers[n]).split())}\n" for n in names)
        signed = ";".join(names)
        canonical = "\n".join([method, _uri_encode(path, False), canonical_query, canonical_headers, signed,
                               payload_hash])
        to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
        return hmac.new(self._signing_key(day), to_sign.encode(), hashlib.sha256).hexdigest(), signed

    def presign(self, method: str, key: str, expires: int = 3600, now: _dt.datetime | None = None) -> str:
        """某一个对象的预签名地址（只签 host，载荷不签）：拿到它的一方只能对这一个 key 做这一种操作。"""
        now = now or _dt.datetime.now(_dt.timezone.utc)
        amz_date, _, scope = self._scope(now)
        path = self._path(key)
        query = {"X-Amz-Algorithm": "AWS4-HMAC-SHA256", "X-Amz-Credential": f"{self.access_key}/{scope}",
                 "X-Amz-Date": amz_date, "X-Amz-Expires": str(int(expires)), "X-Amz-SignedHeaders": "host"}
        sig, _ = self.signature(method, path, query, {"host": self.host}, UNSIGNED, amz_date)
        qs = "&".join(f"{_uri_encode(k, True)}={_uri_encode(v, True)}" for k, v in sorted(query.items()))
        return f"{self.scheme}://{self.host}{_uri_encode(path, False)}?{qs}&X-Amz-Signature={sig}"

    def _request(self, method: str, key: str = "", query: dict[str, str] | None = None,
                 body: bytes = b"") -> tuple[int, bytes]:
        query = query or {}
        amz_date, _, scope = self._scope(_dt.datetime.now(_dt.timezone.utc))
        payload_hash = hashlib.sha256(body).hexdigest() if body else _EMPTY_SHA
        headers = {"host": self.host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date}
        path = self._path(key)
        sig, signed = self.signature(method, path, query, headers, payload_hash, amz_date)
        qs = "&".join(f"{_uri_encode(k, True)}={_uri_encode(v, True)}" for k, v in sorted(query.items()))
        url = f"{self.scheme}://{self.host}{_uri_encode(path, False)}" + (f"?{qs}" if qs else "")
        req = urllib.request.Request(url, data=body if method in ("PUT", "POST") else None, method=method, headers={
            "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date,
            "authorization": f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, "
                             f"SignedHeaders={signed}, Signature={sig}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except (urllib.error.URLError, OSError) as e:
            raise ObjectStoreError(f"连不上对象存储 {self.scheme}://{self.host}：{getattr(e, 'reason', e)}") from None

    @staticmethod
    def _error(status: int, raw: bytes) -> str:
        try:
            root = ET.fromstring(raw)
            code = next((el.text for el in root.iter() if el.tag.endswith("Code")), "")
            return f"HTTP {status} {code or ''}".strip()
        except ET.ParseError:
            return f"HTTP {status}"

    # ------------------------------------------------------------------ 操作

    def put(self, key: str, body: bytes) -> None:
        status, raw = self._request("PUT", key, body=body)
        if status not in (200, 201):
            raise ObjectStoreError(f"上传 {key} 失败：{self._error(status, raw)}")

    def get(self, key: str) -> bytes:
        status, raw = self._request("GET", key)
        if status != 200:
            raise ObjectStoreError(f"下载 {key} 失败：{self._error(status, raw)}")
        return raw

    def delete(self, key: str) -> None:
        status, raw = self._request("DELETE", key)
        if status not in (200, 204, 404):
            raise ObjectStoreError(f"删除 {key} 失败：{self._error(status, raw)}")

    def list(self, prefix: str, limit: int = 10000) -> list[S3Object]:
        """ListObjectsV2，自动翻页。"""
        out: list[S3Object] = []
        token = ""
        while len(out) < limit:
            query = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
            if token:
                query["continuation-token"] = token
            status, raw = self._request("GET", "", query)
            if status != 200:
                raise ObjectStoreError(f"列出 {prefix} 失败：{self._error(status, raw)}")
            try:
                root = ET.fromstring(raw)
            except ET.ParseError:
                raise ObjectStoreError("对象存储的列表响应不是 XML") from None
            ns = root.tag[:root.tag.index("}") + 1] if root.tag.startswith("{") else ""
            for c in root.findall(f"{ns}Contents"):
                out.append(S3Object(c.findtext(f"{ns}Key", ""), int(c.findtext(f"{ns}Size", "0") or 0),
                                    c.findtext(f"{ns}LastModified", "")))
            if (root.findtext(f"{ns}IsTruncated", "false") or "").lower() != "true":
                break
            token = root.findtext(f"{ns}NextContinuationToken", "") or ""
            if not token:
                break
        return out


def make_object_store(cfg) -> S3Client | None:
    """NCS_OBJECT_STORE=off（默认）→ None；s3 → S3Client。"""
    if cfg.object_store != "s3":
        return None
    return S3Client(cfg.s3_endpoint, cfg.s3_region, cfg.s3_bucket, cfg.s3_access_key_id, cfg.s3_secret_access_key)


__all__ = ["S3Client", "S3Object", "ObjectStoreError", "make_object_store", "redact", "UNSIGNED"]
