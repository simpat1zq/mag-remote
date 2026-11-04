"""
Network protocols spoken by Infomir MAG Linux set-top boxes to the
"MAGic Remote" mobile app, re-implemented from the app (v2.0.5).

Two generations exist; a box speaks one of them depending on firmware:

  legacy  UDP. Box answers a broadcast on UDP 6000 with its command port.
          Messages are JSON, AES-256-CBC, key = the numeric password set in
          Settings > System settings > Remote control, zero-padded to 32 bytes.

  pairing TCP, advertised over mDNS as _infomir_mobile_rc_service._tcp.
          One-time pairing: the box shows a code on the TV. Messages are a
          38-byte header + JSON body, AES-128-CFB, key = SHA1(code + salt)[:16].
"""
from __future__ import annotations

import hashlib
import json
import socket
import threading
import time

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

try:  # CFB moved to the "decrepit" module in newer cryptography releases
    from cryptography.hazmat.decrepit.ciphers.modes import CFB
except ImportError:  # pragma: no cover
    CFB = modes.CFB

LEGACY = "legacy"
PAIRING = "pairing"


class RemoteError(Exception):
    """Raised for any failure talking to a box; message is user-facing."""


# ============================================================ key tables ====
ALT = 0x08000000  # Qt::AltModifier

# legacy: name -> (metaState, unicode, Qt keycode)
LEGACY_KEYS = {
    "up": (0, 19, 0x01000013), "down": (0, 20, 0x01000015),
    "left": (0, 21, 0x01000012), "right": (0, 22, 0x01000014),
    "ok": (0, 66, 0x01000004), "back": (0, 4, 0x01000003),
    "home": (0, 3, 0x01000000), "menu": (0, 82, 0x0100003A),
    "info": (ALT, 121, 89), "power": (ALT, 0, 85), "mute": (ALT, 96, 96),
    "vol+": (0, 24, 43), "vol-": (0, 25, 45),
    "ch+": (0, 9, 0x01000016), "ch-": (0, 9, 0x01000017),
    "f1": (0, 131, 0x01000030), "f2": (0, 132, 0x01000031),
    "f3": (0, 133, 0x01000032), "f4": (0, 134, 0x01000033),
    "play": (ALT, 114, 82), "next": (ALT, 102, 70), "prev": (ALT, 98, 66),
    **{str(d): (0, 0, 48 + d) for d in range(10)},
}

# pairing protocol: name -> rc_code
PAIRING_KEYS = {
    **{str(d): 128 + d for d in range(10)},
    "ok": 172, "up": 189, "down": 190, "left": 191, "right": 171,
    "back": 143, "exit": 141, "menu": 138, "info": 157, "power": 140,
    "mute": 176, "vol+": 146, "vol-": 147, "ch+": 188, "ch-": 145,
    "f1": 178, "f2": 177, "f3": 185, "f4": 186, "play": 139, "stop": 153,
    "ff": 144, "rew": 150, "record": 158, "pageup": 148, "pagedown": 149,
    "settings": 179, "tv": 181, "web": 184, "epg": 182, "refresh": 174,
    "mode": 163, "pip": 168, "swap": 170, "favorite": 173, "aspect": 151,
    "keyboard": 152, "mic": 169,
}
# so the same UI buttons work on either generation
PAIRING_KEYS.setdefault("home", PAIRING_KEYS["exit"])
PAIRING_KEYS.setdefault("next", PAIRING_KEYS["ff"])
PAIRING_KEYS.setdefault("prev", PAIRING_KEYS["rew"])


def keys_for(protocol: str) -> list[str]:
    return sorted(LEGACY_KEYS if protocol == LEGACY else PAIRING_KEYS)


# ================================================================ legacy ====
LEGACY_IV = b"erghnlhbnmbnkghy"
DISCOVERY_PORT = 6000
REPLY_PORT = 8777


def _legacy_key(password: str) -> bytes:
    return password.encode()[:32].ljust(32, b"\0")


def legacy_encrypt(password: str, data: bytes) -> bytes:
    p = padding.PKCS7(128).padder()
    data = p.update(data) + p.finalize()
    e = Cipher(algorithms.AES(_legacy_key(password)), modes.CBC(LEGACY_IV)).encryptor()
    return e.update(data) + e.finalize()


def legacy_decrypt(password: str, data: bytes) -> bytes:
    d = Cipher(algorithms.AES(_legacy_key(password)), modes.CBC(LEGACY_IV)).decryptor()
    raw = d.update(data) + d.finalize()
    u = padding.PKCS7(128).unpadder()
    return u.update(raw) + u.finalize()


def _legacy_exchange(host, port, password, messages, timeout):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        def tx(obj):
            s.sendto(legacy_encrypt(password, json.dumps(obj, separators=(",", ":")).encode()),
                     (host, int(port)))

        tx({"msgType": "pingRequest"})
        try:
            data, _ = s.recvfrom(2048)
        except socket.timeout:
            raise RemoteError("No answer from the box. Check the password, and that "
                              "Remote control is on (Settings > System settings > Remote control).")
        try:
            reply = json.loads(legacy_decrypt(password, data))
        except ValueError:
            raise RemoteError("The box answered but the password is wrong.")
        if reply.get("msgType") != "pingResponse":
            raise RemoteError(f"Unexpected reply from the box: {reply}")
        for m in messages:
            tx(m)
    finally:
        s.close()


def legacy_ping(host, port, password, timeout=3.0):
    _legacy_exchange(host, port, password, [], timeout)


def legacy_send(host, port, password, keys, timeout=3.0):
    msgs = []
    for k in keys:
        if k not in LEGACY_KEYS:
            raise RemoteError(f"Key '{k}' isn't supported by this box.")
        meta, uni, code = LEGACY_KEYS[k]
        msgs.append({"metaState": meta, "unicode": uni, "msgType": "keyboardKey",
                     "keycode": code, "action": "press"})
    _legacy_exchange(host, port, password, msgs, timeout)


def legacy_discover(wait=3.0, hosts=(), broadcast=True):
    """Broadcast and/or unicast a discovery probe; return boxes found."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        s.bind(("", REPLY_PORT))
    except OSError:
        s.bind(("", 0))  # reply port busy: unicast replies may still reach us
    probe = json.dumps({"protocol": "remote_stb_1.0", "port": REPLY_PORT}).encode()
    found, end = {}, time.time() + wait
    try:
        while time.time() < end:
            for dst in list(hosts) + (["255.255.255.255"] if broadcast else []):
                try:
                    s.sendto(probe, (dst, DISCOVERY_PORT))
                except OSError:
                    pass
            s.settimeout(0.8)
            try:
                while True:
                    data, addr = s.recvfrom(2048)
                    try:
                        info = json.loads(data.decode())
                        found[addr[0]] = {
                            "host": addr[0], "port": int(info.get("port")),
                            "name": info.get("name") or addr[0],
                            "model": info.get("type") or "",
                            "serial": info.get("serialNumber") or "",
                            "protocol": LEGACY,
                        }
                    except (ValueError, TypeError):
                        pass
            except (socket.timeout, OSError):
                pass
    finally:
        s.close()
    return list(found.values())


# =============================================================== pairing ====
SALT = bytes([8, 56, 0x9A, 0x84, 29, 0xB5, 0xD3, 74])
PAIRING_IV = bytes([18, 111, 0xF1, 33, 102, 71, 0x90, 109,
                    0xC0, 0xE9, 6, 0x99, 0xB4, 99, 0xDE, 101])
HDR = 38
MSG = {
    "PAIRING_REQ":          "pairing-reqpairing-reqpairing-re",
    "PAIRING_COMPLETE_REQ": "pairing-complete-reqpairing-comp",
    "CONNECT_REQ":          "connect-reqconnect-reqconnect-re",
    "RC_CODE_REQ":          "rc-code-reqrc-code-reqrc-code-re",
    "PING_REQ":             "ping-reqping-reqping-reqping-req",
    "PAIRING_OK_RESP":      "pairing-ok-resppairing-ok-resppa",
    "CONNECT_OK_RESP":      "connect-ok-respconnect-ok-respco",
    "PING_RESP":            "ping-respping-respping-respping-",
    "PAIRING_ERROR_RESP":   "pairing-error-resppairing-error-",
    "CONNECT_ERROR_RESP":   "connect-error-respconnect-error-",
    "PROTOCOL_ERROR_RESP":  "protocol_error_respprotocol_erro",
    "BUSY_RESP":            "busy-respbusy-respbusy-respbusy-",
    "SHUTDOWN_RESP":        "shutdown-respshutdown-respshutdo",
    "INVALID_DATA_RESP":    "invalid-data-respinvalid-data-re",
}
MSG_BY_HDR = {v: k for k, v in MSG.items()}
SERVICE_TYPE = "_infomir_mobile_rc_service._tcp.local."


def pairing_crypt(code: str, data: bytes, encrypt: bool) -> bytes:
    key = hashlib.sha1(code.encode() + SALT).digest()[:16]
    c = Cipher(algorithms.AES(key), CFB(PAIRING_IV))
    op = c.encryptor() if encrypt else c.decryptor()
    return op.update(data) + op.finalize()


def frame(msg: str, body: bytes) -> bytes:
    total = HDR + len(body)
    hdr = bytearray(HDR)
    hdr[3] = 1
    hdr[4], hdr[5] = total & 0xFF, (total >> 8) & 0xFF
    t = MSG[msg].encode()
    hdr[6:6 + len(t)] = t
    return bytes(hdr) + body


class PairingConnection:
    """One TCP session with a box that speaks the pairing protocol."""

    def __init__(self, host, port, dev_id, dev_descr, timeout=8.0):
        self.dev_id, self.dev_descr = dev_id, dev_descr
        try:
            self.s = socket.create_connection((host, int(port)), timeout=timeout)
        except OSError as e:
            raise RemoteError(f"Can't reach {host}:{port} ({e.strerror or e}).")

    def _body(self, extra=None, descr=True):
        obj = {"dev_id": self.dev_id}
        if descr:
            obj["dev_descr"] = self.dev_descr
        obj.update(extra or {})
        return json.dumps(obj, separators=(",", ":")).encode()

    def send(self, msg, body, code=None):
        if code:
            body = pairing_crypt(code, body, True)
        self.s.sendall(frame(msg, body))

    def _read(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.s.recv(n - len(buf))
            if not chunk:
                raise RemoteError("The box closed the connection.")
            buf += chunk
        return buf

    def recv(self):
        try:
            hdr = self._read(HDR)
            total = hdr[4] | (hdr[5] << 8)
            body = self._read(max(total - HDR, 0))
        except socket.timeout:
            raise RemoteError("The box didn't answer in time.")
        return MSG_BY_HDR.get(hdr[6:38].rstrip(b"\0").decode(errors="replace"), "UNKNOWN"), body

    def request_pairing(self):
        self.send("PAIRING_REQ", self._body())

    def complete_pairing(self, code):
        self.send("PAIRING_COMPLETE_REQ", self._body(), code)
        name, _ = self.recv()
        if name != "PAIRING_OK_RESP":
            raise RemoteError("Incorrect pairing code." if name == "PAIRING_ERROR_RESP"
                              else f"Pairing failed ({name}).")

    def connect(self, code):
        self.send("CONNECT_REQ", self._body())
        name, _ = self.recv()
        if name != "CONNECT_OK_RESP":
            raise RemoteError("The box rejected this device - remove it and pair again."
                              if name == "CONNECT_ERROR_RESP" else f"Connect failed ({name}).")

    def key(self, code, name):
        if name not in PAIRING_KEYS:
            raise RemoteError(f"Key '{name}' isn't supported by this box.")
        self.send("RC_CODE_REQ", self._body({"rc_code": PAIRING_KEYS[name]}), code)

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass


class PairingSessions:
    """Keeps one live connection per paired box; reconnects when it drops."""

    def __init__(self):
        self._conns, self._lock = {}, threading.Lock()

    def send(self, box, dev_id, dev_descr, keys):
        with self._lock:
            for attempt in (1, 2):
                conn = self._conns.get(box["id"])
                try:
                    if conn is None:
                        conn = PairingConnection(box["host"], box["port"], dev_id, dev_descr)
                        conn.connect(box["code"])
                        self._conns[box["id"]] = conn
                    for k in keys:
                        conn.key(box["code"], k)
                    return
                except (OSError, RemoteError) as e:
                    if conn:
                        conn.close()
                    self._conns.pop(box["id"], None)
                    if attempt == 2 or (isinstance(e, RemoteError) and "rejected" in str(e)):
                        raise e if isinstance(e, RemoteError) else RemoteError(str(e))

    def drop(self, box_id):
        with self._lock:
            c = self._conns.pop(box_id, None)
            if c:
                c.close()


def pairing_discover(wait=3.0):
    try:
        from zeroconf import ServiceBrowser, Zeroconf
    except ImportError:
        return []
    found = {}

    class Listener:
        def add_service(self, zc, type_, name):
            info = zc.get_service_info(type_, name, 2000)
            if not info:
                return
            for addr in info.parsed_addresses():
                if ":" in addr:
                    continue
                label = name.split("." + type_.split(".")[0])[0]
                disp, _, serial = label.rpartition("-")
                found[addr] = {"host": addr, "port": info.port, "name": disp or label,
                               "model": "", "serial": serial, "protocol": PAIRING}

        def update_service(self, *a):
            pass

        def remove_service(self, *a):
            pass

    zc = Zeroconf()
    try:
        ServiceBrowser(zc, SERVICE_TYPE, Listener())
        time.sleep(wait)
    finally:
        zc.close()
    return list(found.values())


def discover(wait=3.0, hosts=()):
    """Find boxes of both generations. Runs both searches in parallel.

    With `hosts`, only those addresses are asked (no broadcast), so a typed-in
    address never resolves to some other box on the network."""
    hosts = list(hosts)
    if hosts:
        return [b for b in legacy_discover(min(wait, 2.0), hosts, broadcast=False)
                if b["host"] in hosts]
    results = {}

    def run(fn, *a):
        for b in fn(*a):
            results.setdefault(b["host"], b)

    t = threading.Thread(target=run, args=(pairing_discover, wait))
    t.start()
    run(legacy_discover, wait, hosts)
    t.join()
    # one box can answer from several addresses; keep one per serial,
    # preferring an address the user typed in
    by_serial = {}
    for b in sorted(results.values(), key=lambda b: (b["host"] not in hosts, b["host"])):
        key = (b["serial"], b["protocol"]) if b["serial"] else b["host"]
        by_serial.setdefault(key, b)
    return sorted(by_serial.values(), key=lambda b: b["host"])
