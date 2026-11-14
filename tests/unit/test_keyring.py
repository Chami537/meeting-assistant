import base64
import os

import pytest
from app.security.keyring import Keyring, KeyringError, new_key_b64


def _spec(*versions: int) -> tuple[str, dict[int, str]]:
    keys = {v: new_key_b64() for v in versions}
    return ",".join(f"{v}:{k}" for v, k in keys.items()), keys


def test_round_trip_and_version_prefix():
    spec, _ = _spec(1)
    ring = Keyring.from_env(spec, "1")
    sealed = ring.encrypt_text("会议密码 1234", aad=b"meeting_tasks.meeting_password_ciphertext:t1")
    assert sealed.key_version == 1 and sealed.ciphertext[0] == 1
    assert (
        ring.decrypt_text(sealed.ciphertext, aad=b"meeting_tasks.meeting_password_ciphertext:t1")
        == "会议密码 1234"
    )


def test_rotation_keeps_old_data_readable():
    spec, keys = _spec(1, 2)
    old = Keyring({1: base64.b64decode(keys[1])}, 1).encrypt(b"old")
    ring = Keyring.from_env(spec, 2)
    new = ring.encrypt(b"new")
    assert (ring.decrypt(old.ciphertext), ring.decrypt(new.ciphertext)) == (b"old", b"new")
    assert ring.needs_rotation(old.ciphertext) and not ring.needs_rotation(new.ciphertext)


def test_aad_binds_ciphertext_to_its_row():
    spec, _ = _spec(1)
    ring = Keyring.from_env(spec, 1)
    sealed = ring.encrypt(b"secret", aad=b"row-1")
    with pytest.raises(KeyringError):
        ring.decrypt(sealed.ciphertext, aad=b"row-2")


def test_tampering_detected():
    spec, _ = _spec(1)
    ring = Keyring.from_env(spec, 1)
    ct = bytearray(ring.encrypt(b"secret").ciphertext)
    ct[-1] ^= 1
    with pytest.raises(KeyringError):
        ring.decrypt(bytes(ct))


def test_unknown_version_rejected():
    spec, _ = _spec(1)
    ring = Keyring.from_env(spec, 1)
    ct = bytes([9]) + ring.encrypt(b"x").ciphertext[1:]
    with pytest.raises(KeyringError, match="no key"):
        ring.decrypt(ct)


@pytest.mark.parametrize(
    ("spec", "active"),
    [
        ("1:" + base64.b64encode(os.urandom(16)).decode(), "1"),  # 16-byte key
        ("1:not-base64", "1"),
        ("nocolon", "1"),
        ("1:" + base64.b64encode(os.urandom(32)).decode(), "2"),  # active key missing
        ("300:" + base64.b64encode(os.urandom(32)).decode(), "300"),  # version must fit in one byte
    ],
)
def test_bad_configuration_rejected(spec, active):
    with pytest.raises(KeyringError):
        Keyring.from_env(spec, active)


def test_short_ciphertext_rejected():
    spec, _ = _spec(1)
    with pytest.raises(KeyringError):
        Keyring.from_env(spec, 1).decrypt(b"\x01abc")


def test_duplicate_version_rejected():
    a, b = new_key_b64(), new_key_b64()
    with pytest.raises(KeyringError, match="duplicate"):
        Keyring.from_env(f"1:{a},1:{b}", 1)


def test_same_key_under_two_versions_rejected():
    a = new_key_b64()
    with pytest.raises(KeyringError, match="repeats"):
        Keyring.from_env(f"1:{a},2:{a}", 2)


def test_nonces_never_repeat():
    spec, _ = _spec(1)
    ring = Keyring.from_env(spec, 1)
    a, b = ring.encrypt(b"same"), ring.encrypt(b"same")
    assert a.ciphertext[1:13] != b.ciphertext[1:13] and a.ciphertext != b.ciphertext


def test_rotation_writes_the_active_version():
    spec, _ = _spec(1, 2)
    sealed = Keyring.from_env(spec, 2).encrypt(b"x")
    assert sealed.key_version == 2 == sealed.ciphertext[0]


def test_a_swapped_entry_is_refused_without_quoting_the_key():
    key = base64.b64encode(os.urandom(32)).decode()
    with pytest.raises(KeyringError) as exc:
        Keyring.from_env(f"{key}:1", 1)
    assert key not in str(exc.value) and key[:8] not in str(exc.value)
