"""从一个主密钥派生出各种用途的子密钥：不同用途用不同的密钥，一处泄露不会波及别处。"""

from __future__ import annotations

import hashlib
import hmac


def derive(master: str, purpose: str) -> bytes:
    return hmac.new(master.encode(), f"nanocode-server/{purpose}".encode(), hashlib.sha256).digest()


def internal_key(master: str) -> str:
    """控制面调用网关内部接口用的密钥。沙箱拿不到主密钥，也就拿不到它。"""
    return derive(master, "gateway/internal").hex()


__all__ = ["derive", "internal_key"]
