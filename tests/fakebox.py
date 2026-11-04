"""
Simulated MAG boxes for development and tests - no hardware needed.

  python -m tests.fakebox            # legacy box (password 1234) + pairing box (code 4821)

Legacy:  answers discovery on UDP 6000, commands on UDP 7666.
Pairing: TCP 7777; shows (prints) pairing code 4821.
Every received key is printed and appended to fakebox.log.
"""
import json
import os
import socket
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from magremote import protocol as p  # noqa: E402

PASSWORD, CODE = os.environ.get("FAKE_PASSWORD", "1234"), os.environ.get("FAKE_CODE", "4821")
BIND = os.environ.get("FAKE_BIND", "0.0.0.0")
LEGACY_PORT, PAIRING_PORT = 7666, 7777
LOG = os.environ.get("FAKE_LOG", "fakebox.log")
_lock = threading.Lock()


def record(line):
    print(line, flush=True)
    with _lock, open(LOG, "a") as f:
        f.write(line + "\n")


def discovery():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((BIND, p.DISCOVERY_PORT))
    while True:
        data, addr = s.recvfrom(2048)
        try:
            req = json.loads(data)
        except ValueError:
            continue
        if req.get("protocol") == "remote_stb_1.0":
            reply = {"type": "MAG544", "name": "fake-legacy", "port": str(LEGACY_PORT),
                     "serialNumber": "FAKE0001"}
            s.sendto(json.dumps(reply).encode(), (addr[0], int(req.get("port", p.REPLY_PORT))))


def legacy():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((BIND, LEGACY_PORT))
    while True:
        data, addr = s.recvfrom(2048)
        try:
            msg = json.loads(p.legacy_decrypt(PASSWORD, data))
        except ValueError:
            record("legacy: undecryptable packet (wrong password) - ignored")
            continue
        if msg.get("msgType") == "pingRequest":
            s.sendto(p.legacy_encrypt(PASSWORD, b'{"msgType":"pingResponse"}'), addr)
        elif msg.get("msgType") == "keyboardKey":
            name = next((k for k, v in p.LEGACY_KEYS.items()
                         if v == (msg["metaState"], msg["unicode"], msg["keycode"])), "?")
            record(f"legacy: key {name}")


def pairing_client(c):
    paired = pairing_client.paired
    conn = p.PairingConnection.__new__(p.PairingConnection)
    conn.s = c
    try:
        while True:
            name, body = conn.recv()
            if name in ("PAIRING_REQ", "CONNECT_REQ"):
                j = json.loads(body)
                if name == "PAIRING_REQ":
                    record(f"pairing: request from {j.get('dev_descr')} -> TV shows {CODE}")
                    continue
                ok = j["dev_id"] in paired
                resp = "CONNECT_OK_RESP" if ok else "CONNECT_ERROR_RESP"
                c.sendall(p.frame(resp, p.pairing_crypt(CODE, json.dumps({"dev_id": j["dev_id"]}).encode(), True)))
                continue
            try:
                j = json.loads(p.pairing_crypt(CODE, body, False))
            except ValueError:
                c.sendall(p.frame("PAIRING_ERROR_RESP", b""))
                continue
            if name == "PAIRING_COMPLETE_REQ":
                paired.add(j["dev_id"])
                record("pairing: paired")
                c.sendall(p.frame("PAIRING_OK_RESP", p.pairing_crypt(CODE, json.dumps({"dev_id": j["dev_id"]}).encode(), True)))
            elif name == "RC_CODE_REQ":
                key = next((k for k, v in p.PAIRING_KEYS.items() if v == j["rc_code"]), "?")
                record(f"pairing: key {key}")
    except (p.RemoteError, OSError):
        c.close()


pairing_client.paired = set()


def pairing():
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((BIND, PAIRING_PORT))
    srv.listen()
    while True:
        c, _ = srv.accept()
        threading.Thread(target=pairing_client, args=(c,), daemon=True).start()


if __name__ == "__main__":
    for fn in (discovery, legacy, pairing):
        threading.Thread(target=fn, daemon=True).start()
    record(f"fakebox up: legacy udp {LEGACY_PORT} pw {PASSWORD}, pairing tcp {PAIRING_PORT} code {CODE}")
    threading.Event().wait()
