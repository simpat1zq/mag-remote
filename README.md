# MAG Remote

A self-hosted web remote for **Infomir MAG** Linux set-top boxes (MAG250 through MAG544).
Open it on any phone, tablet or computer on your network, add your boxes once, and
control them from the browser — or from Home Assistant through its small HTTP API.

- Finds boxes on your network automatically, or add one by IP address
- Any number of boxes, each with its own name
- Works with both remote protocols MAG firmware uses: the older one that uses
  the Remote control **password**, and the newer one that **pairs with a code**
  shown on the TV
- Phone-first layout with a full remote: D-pad, volume, channels, numbers,
  color keys, playback; hold to repeat; keyboard shortcuts on desktop
- Compact layout (menu › Compact layout) that fits the whole remote on one
  phone screen
- Installs to your home screen (PWA), works offline on your LAN, no cloud,
  no telemetry; nothing is configured in the image — everything is set up in the UI

## Before you start

On each box, turn on the remote option and (for older firmware) set a numeric
password: **Settings › System settings › Remote control**.

## Run it

### Docker Compose

```yaml
services:
  mag-remote:
    image: ghcr.io/simpat1zq/mag-remote:latest
    container_name: mag-remote
    network_mode: host
    environment:
      PORT: "8080"
    volumes:
      - mag-remote-data:/data
    restart: unless-stopped

volumes:
  mag-remote-data:
```

Then open `http://<docker-host>:8080`.

### Docker

```sh
docker run -d --name mag-remote --network host \
  -v mag-remote-data:/data --restart unless-stopped \
  ghcr.io/simpat1zq/mag-remote:latest
```

### Komodo

Create a **Stack** from this Git repository (or paste the compose file above
into a UI-defined stack) and deploy. The repository's `compose.yaml` also has
`build: .`, so Komodo can build the image itself if you'd rather not pull.

### Why host networking?

Finding boxes uses a UDP broadcast and mDNS, which don't cross Docker's bridge
network. If you can't use host networking, publish the port instead
(`-p 8080:8080`) and add boxes by IP address — sending keys works fine over a
bridge network; only the network scan needs host mode.

## Settings

All optional, set as environment variables:

| Variable | Default | What it does |
| --- | --- | --- |
| `PORT` | `8080` | Port for the web UI |
| `BIND` | `0.0.0.0` | Address to listen on |
| `BASIC_AUTH` | — | `user:password` to require a login for the UI and API |
| `DATA_DIR` | `/data` | Where boxes are saved (`boxes.json`) |
| `LOG_LEVEL` | `INFO` | `DEBUG` for more detail |

Box passwords and pairing codes are stored in `boxes.json` in the data volume.
There's no login by default: keep it on your LAN, set `BASIC_AUTH`, or put it
behind your reverse proxy's authentication.

### Using a host folder instead of a named volume

A named volume (as in the examples above) works as-is. If you bind-mount a
host folder for `/data`, e.g. `/srv/mag-remote:/data`, the container's user
(UID 10001) must be able to write to it, or it stops at startup with
`PermissionError: ... /data/boxes.json.tmp`. Either:

- give the folder to the container user: `sudo chown -R 10001:10001 /srv/mag-remote`, or
- run the container as the user that owns the folder, by adding
  `user: "1000:1000"` (your `id -u`:`id -g`) to the service.

## Install it on your phone

The page installs as an app (its own icon, opens full screen, no browser bar).
It must be served over **HTTPS**, e.g. behind your reverse proxy; if that uses
a private CA, the phone has to trust that CA.

- **Android (Chrome, Edge, Samsung Internet):** menu (☰) › **Install app**,
  or the browser menu › *Install app* / *Add to Home screen*.
- **iPhone/iPad (Safari):** Share › *Add to Home Screen*.

## Adding a box

1. Open the menu (☰) › **Add a box**.
2. **Scan network**, or type the box's IP address and choose **Find**.
3. Give it a name, then either:
   - enter the **Remote control password** you set on the box, or
   - choose **Show code on TV** and type the code that appears.

Rename, test, change the password or remove a box with the gear next to it in
the menu.

## Home Assistant

Each box has an ID (shown by `GET /api/boxes`). Send keys with a `rest_command`:

```yaml
rest_command:
  mag_key:
    url: "http://<docker-host>:8080/api/boxes/{{ box }}/keys"
    method: post
    content_type: "application/json"
    payload: '{"keys": {{ keys | tojson }}}'
```

```yaml
action: rest_command.mag_key
data:
  box: 3f9c2a1b7e
  keys: ["power"]
```

Key names: `power`, `ok`, `up`, `down`, `left`, `right`, `back`, `home`,
`menu`, `info`, `mute`, `vol+`, `vol-`, `ch+`, `ch-`, `play`, `next`, `prev`,
`f1`–`f4` (red, green, yellow, blue), `0`–`9`. Boxes that pair with a code
also accept `stop`, `ff`, `rew`, `epg`, `tv`, `settings`, `pageup`,
`pagedown`, `record` and more — each box's list is in `GET /api/boxes`.

### API

| Method | Path | Body |
| --- | --- | --- |
| `GET` | `/api/boxes` | — |
| `POST` | `/api/discover` | `{"host": "192.168.1.50"}` (optional) |
| `POST` | `/api/boxes` | `{"host", "port", "password", "name"}` — password boxes |
| `POST` | `/api/pair/start` | `{"host", "port", "name"}` → `{"token"}` |
| `POST` | `/api/pair/finish` | `{"token", "code"}` |
| `PATCH` | `/api/boxes/<id>` | `{"name", "password"}` |
| `DELETE` | `/api/boxes/<id>` | — |
| `POST` | `/api/boxes/<id>/keys` | `{"keys": ["vol+", "vol+"]}` |
| `POST` | `/api/boxes/<id>/test` | — |
| `GET` | `/healthz` | — |

## Troubleshooting

- **Scan finds nothing** — check the remote option is on, the container uses
  host networking, and the box is on the same subnet. Add it by IP instead.
- **"No answer from the box"** — usually a wrong password. Re-enter it on the
  box (Settings › System settings › Remote control) and in the box's settings
  here.
- **"The box rejected this device"** — the box forgot the pairing (e.g. after
  a factory reset). Remove the box and add it again.

## How it works

The protocols were worked out from Infomir's MAGic Remote Android app.

- **Password boxes:** the box answers a UDP broadcast on port 6000 with its
  command port. Commands are JSON key events, AES-256-CBC encrypted with the
  zero-padded password.
- **Pairing boxes:** advertised over mDNS as `_infomir_mobile_rc_service._tcp`.
  A TCP session with a 38-byte header per message and a JSON body, AES-128-CFB
  encrypted with a key derived from the pairing code.

See `magremote/protocol.py` for details.

## Development

```sh
pip install -r requirements.txt pytest
python -m tests.fakebox          # simulated boxes: password 1234 on UDP 7666,
                                 # pairing code 4821 on TCP 7777
DATA_DIR=./data PORT=8080 python -m magremote
python -m pytest -q
```

## Disclaimer

Not affiliated with or endorsed by Infomir. MAG and MAGic Remote are trademarks
of their respective owners. Barlow fonts are bundled under the SIL Open Font
License (`magremote/static/fonts/OFL.txt`).
