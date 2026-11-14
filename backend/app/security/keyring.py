"""Application-level encryption for *_ciphertext columns (design doc §10).

Ciphertext layout: version (1 byte) || nonce (12 bytes) || AES-256-GCM ciphertext+tag. The version is
also stored in the matching *_key_version column so rows can be found for re-encryption. Keys come from
DATA_ENCRYPTION_KEYS="1:<base64 32 bytes>,2:<...>" and DATA_ENCRYPTION_ACTIVE_KEY=2: new data uses the
active key, older versions stay readable until rotated out.
"""

import base64
import binascii
import hashlib
import hmac
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_LEN = 12


class KeyringError(Exception):
    pass


@dataclass(frozen=True)
class Sealed:
    ciphertext: bytes
    key_version: int


class Keyring:
    def __init__(self, keys: dict[int, bytes], active: int):
        if active not in keys:
            raise KeyringError(f"active key {active} is not in the keyring")
        for version, key in keys.items():
            if not 1 <= version <= 255:
                raise KeyringError(f"key version {version} must be 1..255")
            if len(key) != 32:
                raise KeyringError(f"key {version} must be 32 bytes, got {len(key)}")
        self._keys = {v: AESGCM(k) for v, k in keys.items()}
        self._mac_key = hmac.new(keys[active], b"app-mac-v1", hashlib.sha256).digest()
        self.active = active

    @classmethod
    def from_env(cls, keys_spec: str, active: str | int) -> "Keyring":
        keys: dict[int, bytes] = {}
        for item in filter(None, (part.strip() for part in keys_spec.split(","))):
            version, sep, encoded = item.partition(":")
            if not sep:
                raise KeyringError("DATA_ENCRYPTION_KEYS entries look like <version>:<base64 key>")
            try:
                number, key = int(version), base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                # Never quote the entry: with its halves swapped, `version` is the key itself.
                name = f"key {version}" if version.strip().isdigit() else "an entry"
                raise KeyringError(f"{name} is not <version>:<base64 key>") from None
            if number in keys:
                raise KeyringError(f"duplicate key version {number}")
            if key in keys.values():
                raise KeyringError(f"key {number} repeats another version's key")
            keys[number] = key
        return cls(keys, int(active))

    def encrypt(self, plaintext: bytes, *, aad: bytes = b"") -> Sealed:
        """`aad` binds the ciphertext to its context (e.g. table, column and row id) so it cannot be
        copied to another row unnoticed."""
        nonce = os.urandom(_NONCE_LEN)
        body = self._keys[self.active].encrypt(nonce, plaintext, aad)
        return Sealed(bytes([self.active]) + nonce + body, self.active)

    def decrypt(self, ciphertext: bytes, *, aad: bytes = b"") -> bytes:
        if len(ciphertext) < 1 + _NONCE_LEN + 16:
            raise KeyringError("ciphertext too short")
        version = ciphertext[0]
        key = self._keys.get(version)
        if key is None:
            raise KeyringError(f"no key for version {version}")
        nonce, body = ciphertext[1 : 1 + _NONCE_LEN], ciphertext[1 + _NONCE_LEN :]
        try:
            return key.decrypt(nonce, body, aad)
        except InvalidTag as exc:
            raise KeyringError("ciphertext failed authentication") from exc

    def mac(self, data: bytes, *, label: str) -> str:
        """Keyed hash for pseudonymous identifiers (e.g. client IPs in access logs). Derived from the active
        key, so values change when the active key rotates."""
        return hmac.new(self._mac_key, label.encode() + b"\0" + data, hashlib.sha256).hexdigest()

    def needs_rotation(self, ciphertext: bytes) -> bool:
        return bool(ciphertext) and ciphertext[0] != self.active

    def encrypt_text(self, text: str, *, aad: bytes = b"") -> Sealed:
        return self.encrypt(text.encode("utf-8"), aad=aad)

    def decrypt_text(self, ciphertext: bytes, *, aad: bytes = b"") -> str:
        return self.decrypt(ciphertext, aad=aad).decode("utf-8")


def new_key_b64() -> str:
    """For operators: python -c 'from app.security.keyring import new_key_b64; print(new_key_b64())'"""
    return base64.b64encode(os.urandom(32)).decode()
