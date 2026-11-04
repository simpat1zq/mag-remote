"""Container health check: exit 0 if the web server answers /healthz.

Uses the same BIND/PORT as the server, so it works when the server listens
on a specific address (e.g. a Docker network gateway) instead of 0.0.0.0.
"""
import os
import sys
import urllib.request


def main():
    bind = os.environ.get("BIND", "0.0.0.0").strip()
    host = {"": "127.0.0.1", "0.0.0.0": "127.0.0.1", "::": "::1"}.get(bind, bind)
    if ":" in host:
        host = f"[{host}]"
    url = f"http://{host}:{os.environ.get('PORT', '8080')}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            sys.exit(0 if r.status == 200 else 1)
    except Exception as e:  # noqa: BLE001
        print(f"health check failed: {url}: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
