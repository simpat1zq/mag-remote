"""Crypto vectors were produced by running the app's own Java classes."""
from magremote import protocol as p


def test_pairing_crypto_matches_app():
    ct = p.pairing_crypt("4821", b'{"dev_id":"abc","rc_code":140}', True)
    assert ct.hex() == "531fa6a1f0da70b3f8a9a86700fe19f23c452ddfa2ca340f378140c0deb2"
    assert p.pairing_crypt("4821", ct, False) == b'{"dev_id":"abc","rc_code":140}'


def test_legacy_crypto_matches_app():
    ct = p.legacy_encrypt("1234", b'{"msgType":"pingRequest"}')
    assert ct.hex() == "fbdc0602b417bbe89f28a9bf04f45e984b9e577b2049c8409853ad9916f56276"
    assert p.legacy_decrypt("1234", ct) == b'{"msgType":"pingRequest"}'


def test_frame_header():
    f = p.frame("PING_REQ", b"x" * 300)
    assert len(f) == 338 and f[3] == 1
    assert int.from_bytes(bytes([f[5], f[4]]), "big") == 338  # how the app reads it
    assert f[6:38] == b"ping-reqping-reqping-reqping-req"


def test_common_keys_exist_in_both():
    common = ["power", "ok", "up", "down", "left", "right", "back", "home", "menu",
              "info", "mute", "vol+", "vol-", "ch+", "ch-", "play", "next", "prev",
              "f1", "f2", "f3", "f4"] + [str(d) for d in range(10)]
    for k in common:
        assert k in p.LEGACY_KEYS and k in p.PAIRING_KEYS, k
