"""WeCom callback signature and encryption (the WXBizMsgCrypt scheme, official doc 90930 / 90968).

- signature = sha1 of the lexicographically sorted [token, timestamp, nonce, encrypted] joined together
- AES key = base64decode(EncodingAESKey + "="), 32 bytes; IV = first 16 bytes of the key; AES-256-CBC
- padding is PKCS#7 with a 32-byte block
- plaintext = 16 random bytes || 4-byte big-endian length || message || receive_id (the corp id)
"""

import base64
import binascii
import hashlib
import hmac
import os
import re
import struct
import time
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from defusedxml import ElementTree

_BLOCK = 32
_HEX40 = re.compile(r"[0-9a-fA-F]{40}")
# Requests older or newer than this are rejected to blunt replays (timestamp is part of the signature).
MAX_CLOCK_SKEW_SECONDS = 600


class WeComCryptoError(Exception):
    pass


def signature(token: str, timestamp: str, nonce: str, encrypted: str) -> str:
    return hashlib.sha1("".join(sorted([token, timestamp, nonce, encrypted])).encode()).hexdigest()


def _pad(data: bytes) -> bytes:
    n = _BLOCK - len(data) % _BLOCK
    return data + bytes([n]) * n


def _unpad(data: bytes) -> bytes:
    if not data:
        raise WeComCryptoError("empty plaintext")
    n = data[-1]
    if not 1 <= n <= _BLOCK or data[-n:] != bytes([n]) * n:
        raise WeComCryptoError("bad padding")
    return data[:-n]


@dataclass(frozen=True)
class CallbackCrypto:
    token: str
    encoding_aes_key: str
    receive_id: str  # corp id

    def __post_init__(self):
        if len(self.encoding_aes_key) != 43:
            raise WeComCryptoError("EncodingAESKey must be 43 characters")

    @property
    def _key(self) -> bytes:
        try:
            key = base64.b64decode(self.encoding_aes_key + "=", validate=True)
        except binascii.Error as exc:
            raise WeComCryptoError("EncodingAESKey is not base64") from exc
        if len(key) != 32:
            raise WeComCryptoError("EncodingAESKey must decode to 32 bytes")
        return key

    def _cipher(self) -> Cipher:
        key = self._key
        return Cipher(algorithms.AES(key), modes.CBC(key[:16]))

    def verify_signature(self, msg_signature: str, timestamp: str, nonce: str, encrypted: str) -> None:
        if not _HEX40.fullmatch(msg_signature):
            raise WeComCryptoError("malformed signature")
        expected = signature(self.token, timestamp, nonce, encrypted)
        if not hmac.compare_digest(expected, msg_signature.lower()):
            raise WeComCryptoError("signature mismatch")

    def check_timestamp(self, timestamp: str, now: float | None = None) -> None:
        try:
            ts = int(timestamp)
        except ValueError as exc:
            raise WeComCryptoError("timestamp is not an integer") from exc
        if abs((time.time() if now is None else now) - ts) > MAX_CLOCK_SKEW_SECONDS:
            raise WeComCryptoError("timestamp outside the allowed window")

    def decrypt(self, encrypted: str) -> str:
        try:
            raw = base64.b64decode(encrypted, validate=True)
        except binascii.Error as exc:
            raise WeComCryptoError("ciphertext is not base64") from exc
        if not raw or len(raw) % 16:
            raise WeComCryptoError("ciphertext length is not a multiple of the AES block")
        decryptor = self._cipher().decryptor()
        plain = _unpad(decryptor.update(raw) + decryptor.finalize())
        if len(plain) < 20:
            raise WeComCryptoError("plaintext too short")
        (length,) = struct.unpack(">I", plain[16:20])
        message, receive_id = plain[20 : 20 + length], plain[20 + length :]
        if len(message) != length:
            raise WeComCryptoError("declared length exceeds plaintext")
        if receive_id.decode("utf-8", "replace") != self.receive_id:
            raise WeComCryptoError("receive id mismatch")
        return message.decode("utf-8")

    def encrypt(self, message: str, *, random16: bytes | None = None) -> str:
        body = message.encode("utf-8")
        plain = (random16 or os.urandom(16)) + struct.pack(">I", len(body)) + body + self.receive_id.encode()
        encryptor = self._cipher().encryptor()
        return base64.b64encode(encryptor.update(_pad(plain)) + encryptor.finalize()).decode()

    # ------------------------------------------------------------ callback helpers

    def verify_url(self, msg_signature: str, timestamp: str, nonce: str, echostr: str) -> str:
        """GET URL verification: returns the plaintext to echo back within 1 second."""
        self.verify_signature(msg_signature, timestamp, nonce, echostr)
        return self.decrypt(echostr)

    def decrypt_callback(
        self, msg_signature: str, timestamp: str, nonce: str, body: bytes, *, now: float | None = None
    ) -> dict[str, str]:
        """POST callback: returns the decrypted inner XML as a flat dict of its child elements."""
        envelope = parse_xml(body)
        encrypted = envelope.get("Encrypt")
        if not encrypted:
            raise WeComCryptoError("callback has no Encrypt element")
        self.verify_signature(msg_signature, timestamp, nonce, encrypted)
        self.check_timestamp(timestamp, now)
        return parse_xml(self.decrypt(encrypted).encode("utf-8"))


def parse_xml(body: bytes) -> dict[str, str]:
    try:
        root = ElementTree.fromstring(body)
    except Exception as exc:  # defusedxml raises its own subclasses for entity attacks
        raise WeComCryptoError("malformed XML") from exc
    return {child.tag: (child.text or "") for child in root}
