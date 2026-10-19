"""加密保存用户的 API Key（数据库泄露时拿不到明文）。

只用标准库：没有 AES，所以用 HMAC-SHA256 作伪随机函数构造流密码（计数器模式），再用另一把密钥
对密文做 HMAC 认证（先加密后认证）。这是教科书式的构造：
    密钥流第 i 块 = HMAC(加密密钥, 随机数 ‖ i)
    密文          = 明文 XOR 密钥流
    认证标签      = HMAC(认证密钥, 版本 ‖ 随机数 ‖ 密文)
每条记录用新的 16 字节随机数，解密前先在常数时间内校验标签，任何篡改都会被拒绝。

主密钥换了（NCS_SECRET_KEY 变了），以前存的 Key 就解不开了：要么保持主密钥不变，要么让用户重新填写。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

from .keys import derive

VERSION = b"\x01"


class SecretBoxError(Exception):
    pass


class SecretBox:
    def __init__(self, master: str):
        self.enc_key = derive(master, "secretbox/encrypt")
        self.mac_key = derive(master, "secretbox/mac")

    def _keystream(self, nonce: bytes, n: int) -> bytes:
        out = bytearray()
        counter = 0
        while len(out) < n:
            out += hmac.new(self.enc_key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
            counter += 1
        return bytes(out[:n])

    def encrypt(self, plaintext: str, aad: bytes = b"") -> str:
        data = plaintext.encode()
        nonce = os.urandom(16)
        ct = bytes(a ^ b for a, b in zip(data, self._keystream(nonce, len(data))))
        # AAD 绑定 credential_id/owner：DB 写权限者把密文搬到别的行也解不开（#62）
        tag = hmac.new(self.mac_key, VERSION + nonce + aad + ct, hashlib.sha256).digest()
        return base64.b64encode(VERSION + nonce + tag + ct).decode()

    def decrypt(self, token: str, aad: bytes = b"") -> str:
        try:
            raw = base64.b64decode(token.encode(), validate=True)
        except ValueError:
            raise SecretBoxError("密文格式不对") from None
        if len(raw) < 1 + 16 + 32 or raw[:1] != VERSION:
            raise SecretBoxError("密文格式不对")
        nonce, tag, ct = raw[1:17], raw[17:49], raw[49:]
        expected = hmac.new(self.mac_key, VERSION + nonce + aad + ct, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expected):
            raise SecretBoxError("密文被篡改，或者主密钥变了")
        return bytes(a ^ b for a, b in zip(ct, self._keystream(nonce, len(ct)))).decode()


def hint(secret: str) -> str:
    """给人看的提示：只露最后 4 位。"""
    return f"…{secret[-4:]}" if len(secret) > 8 else "…"


__all__ = ["SecretBox", "SecretBoxError", "hint"]
