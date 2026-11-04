"""HTTP server: a JSON API plus the single-page web remote."""
from __future__ import annotations

import base64
import hmac
import ipaddress
import json
import logging
import mimetypes
import os
import re
import signal
import socket
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from . import protocol as p
from .store import Store

log = logging.getLogger("mag-remote")
STATIC = Path(__file__).with_name("static")
PAIR_TTL = 300  # seconds a pairing request stays open


class App:
    def __init__(self, data_dir, auth=None):
        self.store = Store(os.path.join(data_dir, "boxes.json"))
        self.sessions = p.PairingSessions()
        self.pending = {}  # token -> (PairingConnection, box draft, started)
        self.pending_lock = threading.Lock()
        self.auth = auth
        self.dev_descr = f"MAG Remote web ({socket.gethostname()})"[:60]

    # ------------------------------------------------------------ helpers --
    def box_view(self, b):
        v = Store.public(b)
        v["keys"] = p.keys_for(b["protocol"])
        return v

    def _reap_pending(self):
        now = time.time()
        with self.pending_lock:
            for t in [t for t, (_, _, s) in self.pending.items() if now - s > PAIR_TTL]:
                self.pending.pop(t)[0].close()

    # ---------------------------------------------------------------- API --
    def list_boxes(self, _):
        return {"boxes": [self.box_view(b) for b in self.store.boxes()], "version": __version__}

    def discover(self, body):
        hosts = [h for h in [body.get("host")] if h]
        for h in hosts:
            _check_host(h)
        added = {(b["host"], b["port"]) for b in self.store.boxes()}
        found = p.discover(wait=float(body.get("wait", 3)), hosts=hosts)
        for b in found:
            b["added"] = (b["host"], b["port"]) in added
        return {"found": found}

    def add_legacy(self, body):
        host, port = _check_host(body.get("host")), _port(body.get("port"))
        password = str(body.get("password") or "").strip()
        if not password:
            raise p.RemoteError("Enter the password from the box's Remote control settings.")
        p.legacy_ping(host, port, password)
        box = self.store.add({
            "name": _name(body, host), "host": host, "port": port, "protocol": p.LEGACY,
            "password": password, "model": body.get("model", ""), "serial": body.get("serial", ""),
        })
        return {"box": self.box_view(box)}

    def pair_start(self, body):
        self._reap_pending()
        host, port = _check_host(body.get("host")), _port(body.get("port"))
        conn = p.PairingConnection(host, port, self.store.dev_id, self.dev_descr)
        try:
            conn.request_pairing()
        except OSError as e:
            conn.close()
            raise p.RemoteError(f"Couldn't start pairing: {e}")
        token = uuid.uuid4().hex
        draft = {"name": _name(body, host), "host": host, "port": port, "protocol": p.PAIRING,
                 "model": body.get("model", ""), "serial": body.get("serial", "")}
        with self.pending_lock:
            self.pending[token] = (conn, draft, time.time())
        return {"token": token}

    def pair_finish(self, body):
        with self.pending_lock:
            item = self.pending.pop(str(body.get("token")), None)
        if not item:
            raise p.RemoteError("That pairing request expired. Start again.")
        conn, draft, _ = item
        code = str(body.get("code") or "").strip()
        try:
            conn.complete_pairing(code)
        except p.RemoteError:
            with self.pending_lock:  # let them retry the code on the same request
                self.pending[body["token"]] = item
            raise
        conn.close()
        box = self.store.add({**draft, "code": code})
        return {"box": self.box_view(box)}

    def pair_cancel(self, body):
        with self.pending_lock:
            item = self.pending.pop(str(body.get("token")), None)
        if item:
            item[0].close()
        return {}

    def update_box(self, box_id, body):
        fields = {"name": (body.get("name") or "").strip()[:60] or None}
        if body.get("password"):
            fields["password"] = str(body["password"]).strip()
        box = self.store.update(box_id, **fields)
        if not box:
            raise NotFound()
        self.sessions.drop(box_id)
        return {"box": self.box_view(box)}

    def delete_box(self, box_id, _):
        self.sessions.drop(box_id)
        if not self.store.remove(box_id):
            raise NotFound()
        return {}

    def send_keys(self, box_id, body):
        box = self.store.get(box_id)
        if not box:
            raise NotFound()
        keys = body.get("keys") or [body.get("key")]
        keys = [str(k).lower() for k in keys if k][:20]
        if not keys:
            raise p.RemoteError("No key given.")
        if box["protocol"] == p.LEGACY:
            p.legacy_send(box["host"], box["port"], box["password"], keys)
        else:
            self.sessions.send(box, self.store.dev_id, self.dev_descr, keys)
        return {"sent": keys}

    def test_box(self, box_id, _):
        box = self.store.get(box_id)
        if not box:
            raise NotFound()
        t0 = time.time()
        if box["protocol"] == p.LEGACY:
            p.legacy_ping(box["host"], box["port"], box["password"])
        else:
            self.sessions.drop(box_id)
            c = p.PairingConnection(box["host"], box["port"], self.store.dev_id, self.dev_descr)
            try:
                c.connect(box["code"])
            finally:
                c.close()
        return {"ok": True, "ms": round((time.time() - t0) * 1000)}


class NotFound(Exception):
    pass


def _check_host(h):
    h = str(h or "").strip()
    try:
        ipaddress.ip_address(h)
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", h):
            raise p.RemoteError("Enter a valid IP address or hostname.")
    return h


def _port(v):
    try:
        v = int(v)
        if 0 < v < 65536:
            return v
    except (TypeError, ValueError):
        pass
    raise p.RemoteError("Enter a valid port (use Scan to find it).")


def _name(body, host):
    return (str(body.get("name") or "").strip() or host)[:60]


ROUTES = [
    ("GET", r"/api/boxes", "list_boxes"),
    ("POST", r"/api/discover", "discover"),
    ("POST", r"/api/boxes", "add_legacy"),
    ("POST", r"/api/pair/start", "pair_start"),
    ("POST", r"/api/pair/finish", "pair_finish"),
    ("POST", r"/api/pair/cancel", "pair_cancel"),
    ("PATCH", r"/api/boxes/(\w+)", "update_box"),
    ("DELETE", r"/api/boxes/(\w+)", "delete_box"),
    ("POST", r"/api/boxes/(\w+)/keys", "send_keys"),
    ("POST", r"/api/boxes/(\w+)/test", "test_box"),
]


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"mag-remote/{__version__}"

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.address_string(), fmt % args)

        def _json(self, status, obj):
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self):
            if not app.auth:
                return True
            hdr = self.headers.get("Authorization", "")
            if hdr.startswith("Basic "):
                try:
                    given = base64.b64decode(hdr[6:]).decode()
                except ValueError:
                    given = ""
                if hmac.compare_digest(given, app.auth):
                    return True
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.send_header("WWW-Authenticate", 'Basic realm="MAG Remote"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        def _dispatch(self, method):
            path = self.path.split("?", 1)[0]
            if path == "/healthz":
                return self._json(200, {"ok": True})
            if not self._authorized():
                return
            if method == "GET" and not path.startswith("/api/"):
                return self._static(path)
            for m, pattern, name in ROUTES:
                mt = re.fullmatch(pattern, path)
                if m == method and mt:
                    break
            else:
                return self._json(404, {"error": "Not found"})
            body = {}
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                if n > 65536:
                    return self._json(413, {"error": "Request too large"})
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    return self._json(400, {"error": "Invalid JSON"})
                if not isinstance(body, dict):
                    return self._json(400, {"error": "Invalid JSON"})
            try:
                return self._json(200, getattr(app, name)(*mt.groups(), body))
            except p.RemoteError as e:
                return self._json(502, {"error": str(e)})
            except NotFound:
                return self._json(404, {"error": "Box not found"})
            except Exception:  # pragma: no cover
                log.exception("request failed")
                return self._json(500, {"error": "Internal error - see container logs"})

        def _static(self, path):
            rel = "index.html" if path in ("/", "") else path.lstrip("/")
            f = (STATIC / rel).resolve()
            if STATIC.resolve() not in f.parents or not f.is_file():
                return self._json(404, {"error": "Not found"})
            data = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(f.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PATCH(self):
            self._dispatch("PATCH")

        def do_DELETE(self):
            self._dispatch("DELETE")

    return Handler


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(message)s")
    mimetypes.add_type("application/manifest+json", ".webmanifest")
    port = int(os.environ.get("PORT", "8080"))
    bind = os.environ.get("BIND", "0.0.0.0")
    auth = os.environ.get("BASIC_AUTH") or None  # "user:password", optional
    app = App(os.environ.get("DATA_DIR", "/data"), auth)
    srv = ThreadingHTTPServer((bind, port), make_handler(app))
    srv.daemon_threads = True
    log.info("MAG Remote %s listening on http://%s:%d%s", __version__, bind, port,
             " (basic auth on)" if auth else "")

    # As a container's PID 1, Python ignores SIGTERM unless a handler is set,
    # so `docker stop` would wait its full 10 s timeout before killing us.
    def _stop(signum, _frame):
        log.info("Stopping (signal %d)", signum)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _stop)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
