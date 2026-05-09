# PS5 Control for Unfolded Circle Remote 3

Control a PlayStation 5 from an Unfolded Circle Remote 3 — full d-pad, all
face buttons, L1/R1/L2/R2/L3/R3, OPTIONS, SHARE, PS button, touchpad, plus
wake-from-rest, send-to-rest, and best-effort app launching.

## How it works

Sony doesn't publish a public PS5 control API and the console won't accept
BLE keyboards. The only reliable software path is **Remote Play** — Sony's
own protocol for streaming the PS5 to a phone or PC. This project consists
of two parts:

- **`daemon/`** — a small Python service that runs in Docker on any
  always-on Linux box. It maintains a Remote Play session with the PS5
  and exposes a tiny HTTP API on port 8456. Built on
  [`pyremoteplay`](https://github.com/ktnrg45/pyremoteplay).
- **`integration/`** — a custom integration for the Unfolded Circle
  Remote 3 firmware. Forwards every button press as an HTTP call to the
  daemon.

```
[ Remote 3 ] --HTTP--> [ Linux box w/ daemon ] --Remote Play--> [ PS5 ]
```

## Requirements

| | |
|---|---|
| PlayStation 5 | Powered on, Remote Play enabled |
| PSN account | Already signed in on the PS5 |
| Linux box | Always-on, with Docker + Docker Compose v2 |
| Unfolded Circle Remote 3 | And access to its web configurator |

The daemon host can be any Linux machine with Docker — Raspberry Pi, NAS,
old laptop, mini-PC. It just needs to stay on while you want PS5 control
from the remote.

## Network model — LAN-only, no port-forwards needed

The daemon binds an HTTP API on `8456` and is designed to be reached **only on your LAN** by the Remote 3 (or by other LAN-side scripts / Home Assistant). It does **not** need an internet-facing port-forward, does **not** use MQTT, and shouldn't be opened to the WAN.

If you previously ran the older community `ps5-mqtt` Home Assistant add-on and you've now switched to this project, tidy up the leftover state from that setup:

1. **Uninstall the `ps5-mqtt` add-on** in HA → Settings → Add-ons (stopping it isn't enough — uninstall it).
2. **Delete any router port-forwards** you added for that add-on, typically `1883/tcp` and/or `8883/tcp`. This project doesn't need them.
3. If you don't use MQTT for anything else (e.g. Zigbee2MQTT), you can also uninstall the **Mosquitto broker** add-on. If you do use it for Zigbee2MQTT etc., just leave it on the LAN.

## Install — step by step

> **How long it takes:** ~5 minutes on a Mac / Intel Linux box / 64-bit
> ARM. **10–15 minutes on a 32-bit Raspberry Pi** (Pi 3, Pi 4 with the
> 32-bit Bullseye/Bookworm image, or any other `armv7l` host). The Pi
> has to compile a few Python packages from source — that's expected,
> just leave it running.

### Before you start — gather these 3 things

**1. Your PS5's local IP address.**
On the PS5: **Settings → Network → View Connection Status → IP Address**.
Looks like `192.168.x.x`.

**2. Your PSN Account ID** (a short Base64 string like `aBc1dEfg23h=`).
- **If your PSN profile is public** — paste your PSN online ID into
  [psn.flipscreen.games](https://psn.flipscreen.games) and copy the
  *Base64 Account ID* it shows.
- **If your PSN profile is private** — the installer can fetch it for
  you via Sony's OAuth (it'll ask). Or run `./get-account-id.sh` first
  to grab it on its own.

**3. A Linux/Mac box with Docker** that stays on (Pi, NAS, mini-PC, old
laptop — anything). Test by running `docker --version` and
`docker compose version` — both should print a version. If not, follow
[Docker's install guide](https://docs.docker.com/engine/install/) first.

### One-time PS5 setup (do these once, before installing)

Enable Remote Play — that's the protocol the daemon talks to the PS5 over:

- **Settings → System → Remote Play → Enable Remote Play** → ON
- **Settings → Users and Accounts → Other → Console Sharing and Offline
  Play** → Enable
- **Settings → System → Power Saving → Features Available in Rest Mode**
  → turn on **both** *Stay Connected to the Internet* and *Enable
  Turning On PS5 from Network*

### Install steps

**1. Clone the repo and enter the folder.**
```bash
git clone https://github.com/sbr-labs/ps5-control-uc.git
cd ps5-control-uc
```

**2. Generate an 8-digit pairing PIN on the PS5 — *right before* step 3.**
On the PS5: **Settings → System → Remote Play → Link Device**. The PIN
is valid for ~5 minutes only, so don't do this hours ahead.

**3. Run the installer.**
```bash
./install.sh
```

**4. Answer the prompts (in order):**

| Prompt | What to type |
|---|---|
| `PS5 IP:` | The IP from "Before you start" step 1 |
| `Need to look up your Account ID via OAuth first? (y/N):` | `y` if your PSN profile is private; `N` if you already have your Account ID |
| `Press Enter when ready to pair...` | Press Enter |
| `PSN Account ID (Base64):` | Paste the Base64 string |
| `8-digit pairing PIN from PS5:` | Type the digits showing on the PS5 screen |

**5. Wait. Here's what each stage looks like:**

- `apt-get install ... gcc libc6-dev ...` — installing build tools
  inside the pairing container (~30 seconds).
- **`pip install --quiet pyremoteplay...`** — looks like nothing's
  happening for several minutes. **It is working** — pip is compiling
  Python packages (`netifaces`, `cffi`, `pycryptodomex`) from source.
  - On Mac / Intel Linux / 64-bit ARM: ~30 seconds.
  - On a 32-bit Raspberry Pi: **5–10 minutes**. Don't kill it.
- `credentials.json written.` ✅ pairing succeeded.
- `Building daemon container...` — building the daemon Docker image
  (another ~1–3 minutes; ~5 minutes on a Pi).
- `Daemon running.` ✅ done.

At the end the script prints:

```
Daemon is listening on:    192.168.1.50:8456
```

**Write that address down** — you'll paste it into the Remote 3 in the
next section.

### Hook it up to the Remote 3

1. Open the Remote 3 web configurator in a browser (the address is on
   the Remote: **Settings → About → Web configurator**).
2. **Settings → Integrations → Upload custom integration** → pick
   `integration/ps5-uc-integration.tar.gz` from the repo you cloned.
3. After the upload completes, run setup for the new "PS5 Control"
   integration. When it asks for the **daemon URL / host**, paste the
   address from the install step (e.g. `192.168.1.50:8456`).
4. Done — press a button on the remote and the PS5 should respond.

### Quick sanity check

From the daemon machine:
```bash
curl http://localhost:8456/health
```
A JSON response means the daemon is live. The only thing left at that
point is the Remote 3 upload above.

## Use your own picture on the media-player widget (optional)

By default, the Remote 3 shows a PS5 wordmark on the media-player widget when the PS5 is on the home screen (no game running). Easy to swap for any picture you like — a screenshot, a piece of art, your dog, anything.

**Option A — link to a picture on the internet (easiest)**

1. **Find a PNG or JPG.** Must be a real image file (not SVG, not a webpage).
2. **Get the direct image URL.** Right-click the image in your browser → **"Copy image address"** (Chrome/Edge) or **"Copy image link"** (Safari/Firefox). URL should end in `.png`, `.jpg`, or `.jpeg`. If it ends in `.html` or anything else, that's the webpage, not the image.
3. **Edit `daemon/.env`** in your cloned repo and add (or change) the line:
   ```
   HOME_IMAGE_URL=https://example.com/my-art.png
   ```
4. **Restart the daemon:**
   ```bash
   cd daemon
   docker compose up -d --force-recreate
   ```
5. New image shows on the Remote 3 within ~10 seconds.

To go back to the default PS5 wordmark, set `HOME_IMAGE_URL=` (empty) in `.env` and restart.

**Option B — use a local file (no internet host needed)**

If you want to use a picture stored on your daemon machine, mount it into the container and point at it:

1. Save your PNG/JPG somewhere persistent on the host, e.g. `~/ps5-art/my.png`.
2. Edit `daemon/docker-compose.yml`. Under `volumes:` add:
   ```yaml
       - ~/ps5-art/my.png:/data/my.png:ro
   ```
3. In the same file, under `environment:`, set:
   ```yaml
       HOME_IMAGE_URL: ""
       HOME_IMAGE_FILE: "/data/my.png"
   ```
4. `docker compose up -d --force-recreate`.

**Common gotchas**

- URL ends in `.svg` → won't work. The Remote 3 only renders PNG/JPG.
- Pasted a Wikipedia / Pinterest / Imgur *page* URL → won't work. You need the direct *image* URL (right-click → Copy image address).
- URL works in your browser but Remote 3 shows nothing → some hosts block hot-linking. Try Imgur, Cloudinary, a GitHub Gist raw URL, or your own server.
- Pictures you upload to Imgur should use the `i.imgur.com/<id>.png` form, not the `imgur.com/<id>` page form.

## Day-to-day controls

| Remote 3 button | PS5 input |
|---|---|
| D-pad | Up / Down / Left / Right |
| Centre / OK | X (CROSS) |
| Back | O (CIRCLE) |
| Yellow | Triangle |
| Blue | Square |
| Home | PS button |
| Power | Wake / send to rest |

Plus L1, R1, L2, R2, L3, R3, OPTIONS, SHARE, TOUCHPAD as
`simple_commands` you can bind to any button or activity.

## Daemon REST API

Useful if you want to drive the PS5 from anywhere else (Home Assistant,
custom scripts, Apple Shortcuts, etc.):

```
POST /button     {"button": "<name>", "action": "tap|press|release"}
POST /wakeup
POST /standby
POST /launch     {"title_id": "<PPSAxxxxx>"}
GET  /state      -> {"power": "on|off", "session": bool, "app": "<name>"}
```

Default port is `8456`.

## Building the integration from source

The repo includes a pre-built `integration/ps5-uc-integration.tar.gz`.
If you want to rebuild it (e.g. after modifying `integration/source/`),
run:

```bash
./integration/build.sh
```

Requires Docker + buildx. Output is the Linux ARM64 binary suitable for
the Remote 3.

## Troubleshooting

**PS5 and the daemon-host are on different subnets / VLANs.** Two cross-subnet hops are involved: *daemon → PS5* (outbound to control the PS5) and *Remote 3 → daemon* (inbound for button presses). Both can be silently blocked by router or firewall rules even when `ping` works for normal browsing — typical "guest network isolation" or VLAN ACLs do exactly this. Diagnose with `ping <ps5-ip>` and `nc -zv <ps5-ip> 9295` from the daemon host; if `ping` works but `nc` fails, the router is allowing ICMP but blocking TCP/UDP between the subnets. **Simplest fix: put the daemon-host on the same subnet as the PS5.** Either move the PS5 to the daemon's subnet, install the daemon on a Pi/HA already on the PS5's subnet, or open TCP 9295, UDP 9296, UDP 9302, and TCP 8456 between the two subnets in your router/firewall.

**Remote 3 says "connection refused" / "host not reachable" pointing at the daemon.** Run this one-liner on the daemon host to diagnose all five common causes at once:
```bash
echo "===container===" && docker ps -a --filter name=ps5-control \
  && echo "===log===" && cd ~/ps5-control-uc/daemon && docker compose logs --tail 30 ps5-control \
  && echo "===curl===" && curl -v http://localhost:8456/health 2>&1 | tail -10 \
  && echo "===listening===" && (ss -tlnp 2>/dev/null || netstat -tlnp 2>/dev/null) | grep 8456 \
  && echo "===lan ip===" && hostname -I
```
What each section tells you:
- **container**: shows `Up X minutes` (good) or `Exited` (daemon crashed — check log)
- **log**: any Python traceback explains why the daemon stopped
- **curl**: HTTP 200 = daemon process is alive and serving locally; "Connection refused" here = daemon process died inside container
- **listening**: should show `0.0.0.0:8456` (good) or `::1:8456` (only loopback, fix `LISTEN_HOST=0.0.0.0` in `daemon/.env`)
- **lan ip**: confirms the IP you typed into the Remote 3 setup matches what the daemon's host actually has

If `curl` from the daemon host works but the Remote 3 still gets refused, it's a network problem (firewall on daemon host, different VLAN, etc.) — `sudo ufw allow 8456/tcp` if `ufw` is active on a Pi.

**Install seems stuck on `pip install --quiet pyremoteplay...`.** It
isn't — pip is compiling C-extension packages from source because there
are no prebuilt wheels for your CPU architecture (this is the normal
case on a 32-bit Raspberry Pi). Expect 5–10 minutes on Pi, 30s on
x86_64 / 64-bit ARM. Don't kill it. If it's been more than 15 minutes,
check `top` — if `cc1` or `gcc` is still using CPU, leave it.

**Pairing fails with "Could not reach PS5".** PS5 must be powered on (not
in rest mode) for the initial pairing. After pairing succeeds, the daemon
can wake it from rest.

**PIN keeps expiring.** Generate a fresh one on the PS5 immediately
before running `install.sh` — PINs are valid ~5 minutes.

**`./update.sh` says "Not possible to fast-forward" or refuses to pull.**
The remote `main` branch was force-pushed (e.g. to clean up history).
`update.sh` v0.4.3+ recovers automatically. If you're on an older
checkout, run:
```bash
git fetch origin
git reset --hard origin/main
./install.sh
```
Your `daemon/credentials.json` and `daemon/.env` are gitignored and
won't be touched.

**"Driver not connected" after upload.** Restart the Remote 3 once after
the first upload (Settings → Power → Restart). UC's integration list
caches aggressively.

**Buttons feel laggy.** Wi-Fi RTT to the daemon dominates. Wired
Ethernet on the daemon box helps significantly.

**Daemon can't reach PS5 after a reboot.** Some routers re-issue DHCP
addresses. Set a static IP / DHCP reservation for the PS5.

## Updating

When the project owner publishes a new version, pull and rebuild in one step:

```bash
cd ps5-control
./update.sh
```

`update.sh` will:
1. `git pull` the latest source
2. Stash any local edits you made (you can `git stash pop` after if you want them back)
3. Rebuild the daemon Docker container
4. Restart the daemon
5. Tell you whether the Remote 3 integration tarball changed — if it did,
   re-upload `integration/ps5-uc-integration.tar.gz` via the Remote 3 web
   configurator. If it didn't, you're done.

Your `credentials.json` and `.env` are preserved (they're gitignored — never
touched by updates).

> **Note:** `update.sh` only works if you originally `git clone`d the repo.
> If you downloaded a zip, re-clone fresh, copy your `credentials.json` and
> `.env` over from your old install, then run `./install.sh`.

## Stopping / restarting the daemon

```bash
cd daemon
docker compose down       # stop
docker compose up -d      # start
docker compose logs -f    # tail logs
```

> **Keep it LAN-only.** The daemon has no auth — anyone who can reach `:8456` can press buttons on your PS5. Don't port-forward `8456` to the WAN; access remotely via Tailscale / WireGuard / your existing HA remote-access setup instead. From a phone on 4G, `nc -zv <your-public-ip> 8456` should time out.

## Privacy / what stays local

- Your `daemon/credentials.json` (Remote Play registration) is generated
  on first install and stays on your daemon host. **Never share it** —
  it's tied to your PSN account.
- Your PS5 IP is stored in `daemon/.env` (also local).
- Both files are listed in `.gitignore` — they won't accidentally end up
  in commits if you fork this.

## License

MIT — see [LICENSE](LICENSE).

## Credits

- [`pyremoteplay`](https://github.com/ktnrg45/pyremoteplay) by
  ktnrg45 — does the heavy lifting on the Remote Play protocol.
- [`ucapi`](https://github.com/unfoldedcircle/integration-python-library)
  — Unfolded Circle's Python integration library.
