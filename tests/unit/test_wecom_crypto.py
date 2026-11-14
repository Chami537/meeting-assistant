"""WeCom callback crypto, checked against the vectors of WeCom's official WXBizMsgCrypt sample."""

import pytest
from app.integrations.wecom.crypto import CallbackCrypto, WeComCryptoError, parse_xml, signature

TOKEN = "QDG6eK"
AES_KEY = "jWmYm7qr5nMoAUwZRjGtBxmz3KA1tkAj3ykkR6q2B2C"
CORP_ID = "wx5823bf96d3bd56c7"

# GET URL verification sample
VERIFY_SIG = "5c45ff5e21c57e6ad56bac8758b79b1d9ac89fd3"
VERIFY_TS = "1409659589"
VERIFY_NONCE = "263014780"
VERIFY_ECHOSTR = "P9nAzCzyDtyTWESHep1vC5X9xho/qYX3Zpb4yKa9SKld1DsH3Iyt3tP3zNdtp+4RPcs8TgAE7OaBO+FZXvnaqQ=="

# POST message sample
POST_SIG = "477715d11cdb4164915debcba66cb864d751f3e6"
POST_TS = "1409659813"
POST_NONCE = "1372623149"
POST_BODY = (
    b"<xml><ToUserName><![CDATA[wx5823bf96d3bd56c7]]></ToUserName><Encrypt><![CDATA["
    b"RypEvHKD8QQKFhvQ6QleEB4J58tiPdvo+rtK1I9qca6aM/wvqnLSV5zEPeusUiX5L5X/0lWfrf0QADHHhGd3QczcdCUpj911L3vg3W"
    b"/sYYvuJTs3TUUkSUXxaccAS0qhxchrRYt66wiSpGLYL42aM6A8dTT+6k4aSknmPj48kzJs8qLjvd4Xgpue06DOdnLxAUHzM6+kDZ+H"
    b"MZfJYuR+LtwGc2hgf5gsijff0ekUNXZiqATP7PF5mZxZ3Izoun1s4zG4LUMnvw2r+KqCKIw+3IQH03v+BCA9nMELNqbSf6tiWSrXJB"
    b"3LAVGUcallcrw8V2t9EL4EhzJWrQUax5wLVMNS0+rUPA3k22Ncx4XXZS9o0MBH27Bo6BpNelZpS+/uh9KsNlY6bHCmJU9p8g7m3fV"
    b"Kn28H3KDYA5Pl/T8Z1ptDAVe0lXdQ2YoyyH2uyPIGHBZZIs2pDBS8R07+qN+E7Q==]]></Encrypt>"
    b"<AgentID><![CDATA[218]]></AgentID></xml>"
)


@pytest.fixture
def crypto():
    return CallbackCrypto(token=TOKEN, encoding_aes_key=AES_KEY, receive_id=CORP_ID)


def test_signature_matches_official_sample():
    assert signature(TOKEN, VERIFY_TS, VERIFY_NONCE, VERIFY_ECHOSTR) == VERIFY_SIG


def test_url_verification_official_sample(crypto):
    assert crypto.verify_url(VERIFY_SIG, VERIFY_TS, VERIFY_NONCE, VERIFY_ECHOSTR) == "1616140317555161061"


def test_post_callback_official_sample(crypto):
    msg = crypto.decrypt_callback(POST_SIG, POST_TS, POST_NONCE, POST_BODY, now=float(POST_TS))
    assert msg["MsgType"] == "text"
    assert msg["Content"] == "hello"
    assert msg["MsgId"] == "4561255354251345929"


def test_round_trip(crypto):
    encrypted = crypto.encrypt("<xml><Event>kf_msg_or_event</Event></xml>")
    assert crypto.decrypt(encrypted) == "<xml><Event>kf_msg_or_event</Event></xml>"


def test_round_trip_unicode_and_block_boundaries(crypto):
    for n in range(0, 70):
        message = "会" * n
        assert crypto.decrypt(crypto.encrypt(message)) == message


def test_wrong_signature_rejected(crypto):
    with pytest.raises(WeComCryptoError):
        crypto.verify_url("0" * 40, VERIFY_TS, VERIFY_NONCE, VERIFY_ECHOSTR)


def test_tampered_ciphertext_rejected(crypto):
    tampered = "A" + VERIFY_ECHOSTR[1:]
    with pytest.raises(WeComCryptoError):
        crypto.decrypt(tampered)


def test_other_corp_rejected():
    other = CallbackCrypto(token=TOKEN, encoding_aes_key=AES_KEY, receive_id="ww_other_corp")
    with pytest.raises(WeComCryptoError, match="receive id"):
        other.decrypt(VERIFY_ECHOSTR)


def test_stale_timestamp_rejected(crypto):
    with pytest.raises(WeComCryptoError, match="timestamp"):
        crypto.decrypt_callback(POST_SIG, POST_TS, POST_NONCE, POST_BODY, now=float(POST_TS) + 3600)


@pytest.mark.parametrize("bad", ["not base64!!", "", "QUJD"])
def test_malformed_ciphertext_rejected(crypto, bad):
    with pytest.raises(WeComCryptoError):
        crypto.decrypt(bad)


def test_bad_key_length_rejected():
    with pytest.raises(WeComCryptoError):
        CallbackCrypto(token=TOKEN, encoding_aes_key="short", receive_id=CORP_ID)


def test_entity_expansion_is_refused():
    bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]><xml><Encrypt>&b;</Encrypt></xml>'
    with pytest.raises(WeComCryptoError):
        parse_xml(bomb)


def test_missing_encrypt_element(crypto):
    with pytest.raises(WeComCryptoError, match="Encrypt"):
        crypto.decrypt_callback(POST_SIG, POST_TS, POST_NONCE, b"<xml><ToUserName>x</ToUserName></xml>")


@pytest.mark.parametrize("sig", ["é" * 40, "z" * 40, "abc", ""])
def test_malformed_signature_is_rejected_not_crashing(crypto, sig):
    with pytest.raises(WeComCryptoError):
        crypto.verify_url(sig, VERIFY_TS, VERIFY_NONCE, VERIFY_ECHOSTR)


def test_uppercase_signature_accepted(crypto):
    assert (
        crypto.verify_url(VERIFY_SIG.upper(), VERIFY_TS, VERIFY_NONCE, VERIFY_ECHOSTR)
        == "1616140317555161061"
    )


def test_post_callback_with_wrong_signature_is_rejected(crypto):
    with pytest.raises(WeComCryptoError, match="signature"):
        crypto.decrypt_callback("0" * 40, POST_TS, POST_NONCE, POST_BODY, now=float(POST_TS))


def test_replay_with_fresh_timestamp_is_rejected(crypto):
    """The timestamp is signed: moving an old callback to "now" breaks its signature."""
    fresh = str(int(POST_TS) + 86400)
    with pytest.raises(WeComCryptoError, match="signature"):
        crypto.decrypt_callback(POST_SIG, fresh, POST_NONCE, POST_BODY, now=float(fresh))


@pytest.mark.parametrize(("skew", "ok"), [(599, True), (-599, True), (601, False), (-601, False)])
def test_timestamp_window(crypto, skew, ok):
    ts = 1_700_000_000
    if ok:
        crypto.check_timestamp(str(ts), now=ts + skew)
    else:
        with pytest.raises(WeComCryptoError, match="timestamp"):
            crypto.check_timestamp(str(ts), now=ts + skew)
