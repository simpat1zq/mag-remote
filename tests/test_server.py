"""End-to-end API test against the simulated boxes in tests/fakebox.py."""
import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from magremote.server import App, make_handler
from tests import fakebox


@pytest.fixture(scope="module")
def api(tmp_path_factory):
    os.environ["FAKE_LOG"] = str(tmp_path_factory.mktemp("fb") / "fakebox.log")
    fakebox.LOG = os.environ["FAKE_LOG"]
    for fn in (fakebox.legacy, fakebox.pairing):
        threading.Thread(target=fn, daemon=True).start()
    app = App(str(tmp_path_factory.mktemp("data")))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.3)
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def call(method, path, body=None):
        req = urllib.request.Request(base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    yield call
    srv.shutdown()


def log():
    with open(fakebox.LOG) as f:
        return f.read()


def test_legacy_box(api):
    code, r = api("POST", "/api/boxes", {"host": "127.0.0.1", "port": 7666, "password": "0000"})
    assert code == 502 and "password" in r["error"].lower()
    code, r = api("POST", "/api/boxes", {"host": "127.0.0.1", "port": 7666, "password": "1234", "name": "Den"})
    assert code == 200 and r["box"]["name"] == "Den" and "password" not in r["box"]
    box = r["box"]["id"]
    assert api("POST", f"/api/boxes/{box}/keys", {"keys": ["power", "vol+"]})[0] == 200
    time.sleep(0.2)
    assert "legacy: key power" in log() and "legacy: key vol+" in log()
    assert api("POST", f"/api/boxes/{box}/test")[1]["ok"]
    assert api("POST", f"/api/boxes/{box}/keys", {"key": "nope"})[0] == 502


def test_pairing_box(api):
    _, r = api("POST", "/api/pair/start", {"host": "127.0.0.1", "port": 7777, "name": "Bedroom"})
    token = r["token"]
    code, r = api("POST", "/api/pair/finish", {"token": token, "code": "0000"})
    assert code == 502 and "Incorrect" in r["error"]
    code, r = api("POST", "/api/pair/finish", {"token": token, "code": "4821"})
    assert code == 200 and "code" not in r["box"]
    box = r["box"]["id"]
    for k in ("ok", "down"):
        assert api("POST", f"/api/boxes/{box}/keys", {"key": k})[0] == 200
    time.sleep(0.2)
    assert "pairing: key ok" in log() and "pairing: key down" in log()


def test_list_rename_delete(api):
    _, r = api("GET", "/api/boxes")
    assert len(r["boxes"]) == 2
    box = r["boxes"][0]["id"]
    assert api("PATCH", f"/api/boxes/{box}", {"name": "Kitchen"})[1]["box"]["name"] == "Kitchen"
    assert api("DELETE", f"/api/boxes/{box}")[0] == 200
    assert len(api("GET", "/api/boxes")[1]["boxes"]) == 1
    assert api("DELETE", f"/api/boxes/{box}")[0] == 404
