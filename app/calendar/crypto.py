"""凭据与令牌密码学：AES-GCM 加密、学号 HMAC 索引、订阅令牌与短时验证令牌。

约束（项目硬规则）：
- 学号与密码只以密文落盘；索引使用 HMAC，静态泄露不暴露学号
- 订阅令牌只存哈希 + 加密副本（加密副本用于已授权回显）
- 任何密钥/明文都不得进入日志
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from typing import Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class CalendarCryptoError(RuntimeError):
    """密钥或密文非法。"""


def _decode_key(master_key: str) -> bytes:
    raw = (master_key or "").strip()
    if not raw:
        raise CalendarCryptoError("CALENDAR_MASTER_KEY 未配置")
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            key = decoder(raw + "=" * (-len(raw) % 4))
        except Exception:
            continue
        if len(key) == 32:
            return key
    raise CalendarCryptoError("CALENDAR_MASTER_KEY 必须是 32 字节的 base64（建议 urlsafe）")


class CalendarCrypto:
    """AEAD 加解密 + HMAC 索引 + 令牌工具。"""

    def __init__(self, master_key: str, verify_ttl: int = 600) -> None:
        self._key = _decode_key(master_key)
        self._aead = AESGCM(self._key)
        self.verify_ttl = max(60, int(verify_ttl))

    # ---- 索引 ----
    def owner_hash(self, username: str) -> str:
        """学号的不可逆索引（HMAC-SHA256），用于唯一约束与 O(1) 精确查询。"""
        return hmac.new(self._key, b"owner|" + str(username or "").strip().encode("utf-8"),
                        hashlib.sha256).hexdigest()

    def stable_id(self, *parts: object) -> str:
        payload = "|".join(str(p) for p in parts).encode("utf-8")
        return hmac.new(self._key, b"uid|" + payload, hashlib.sha256).hexdigest()

    # ---- AEAD ----
    def encrypt(self, plaintext: str, aad: str) -> bytes:
        nonce = secrets.token_bytes(12)
        data = self._aead.encrypt(nonce, str(plaintext).encode("utf-8"),
                                  ("v1|" + aad).encode("utf-8"))
        return nonce + data

    def decrypt(self, blob: bytes, aad: str) -> str:
        if not blob or len(blob) < 13:
            raise CalendarCryptoError("密文长度非法")
        try:
            return self._aead.decrypt(blob[:12], blob[12:], ("v1|" + aad).encode("utf-8")).decode("utf-8")
        except InvalidTag as exc:
            raise CalendarCryptoError("密文校验失败（密钥不匹配或数据被篡改）") from exc

    # ---- 订阅令牌 ----
    @staticmethod
    def new_token() -> str:
        """256-bit 随机订阅令牌（base64url，约 43 字符）。"""
        return secrets.token_urlsafe(32)

    @staticmethod
    def token_hash(token: str) -> str:
        return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()

    @staticmethod
    def compare(left: str, right: str) -> bool:
        return hmac.compare_digest(str(left or ""), str(right or ""))

    # ---- 短时验证令牌（查询成功后签发，证明当前密码刚被学校接受）----
    def issue_verify_token(self, owner_hash: str, now: Optional[float] = None) -> str:
        expires = int((now or time.time()) + self.verify_ttl)
        signature = hmac.new(self._key, b"verify|%s|%d" % (owner_hash.encode("utf-8"), expires),
                             hashlib.sha256).hexdigest()
        return "%d.%s" % (expires, signature)

    def check_verify_token(self, token: str, owner_hash: str,
                           now: Optional[float] = None) -> bool:
        raw = str(token or "").strip()
        if "." not in raw:
            return False
        expires_text, _, signature = raw.partition(".")
        if not expires_text.isdigit() or not signature:
            return False
        if int(expires_text) < int(now or time.time()):
            return False
        expected = hmac.new(self._key, b"verify|%s|%s" % (owner_hash.encode("utf-8"),
                                                           expires_text.encode("ascii")),
                            hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)
