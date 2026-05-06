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

## Quick start

```bash
git clone <this-repo>
cd <this-repo>
./install.sh
```

`install.sh` will:
1. Verify Docker is installed
2. Ask for your PS5's local IP
3. Walk you through Remote Play pairing (your PSN Account ID + 8-digit PIN
   from the PS5 screen)
4. Build the daemon container and start it
5. Print the daemon's URL — you'll paste this into the Remote 3 setup

Then upload `integration/ps5-uc-integration.tar.gz` to your Remote 3
(Settings → Integrations → Upload custom integration) and run setup,
entering the daemon URL when prompted.

## Detailed setup walkthrough

### 1. Enable Remote Play on the PS5

- **Settings → System → Remote Play → Enable Remote Play**
- **Settings → Users and Accounts → Other → Console Sharing and Offline
  Play → Enable**
- **Settings → System → Power Saving → Features Available in Rest Mode
  → Stay Connected to the Internet** + **Enable Turning On PS5 from
  Network** — both ON

### 2. Find your PSN Account ID

You'll need this for pairing — a short Base64 string like `aBc1dEfg23h=`.

The easiest source is **https://psn.flipscreen.games** — type your PSN
online ID, copy the **Base64 Account ID**.

### 3. Find your PS5's local IP

PS5: **Settings → Network → View Connection Status → IP Address**.

### 4. Run the installer

```bash
./install.sh
```

When the script asks for the pairing PIN, generate one on your PS5 at
**Settings → System → Remote Play → Link Device**. The PIN is valid for
~5 minutes.

When the installer finishes it prints the daemon URL — note this down.

### 5. Upload the integration

In the Remote 3 web configurator: **Settings → Integrations → Upload
custom integration** → pick `integration/ps5-uc-integration.tar.gz`.

### 6. Configure the integration

Run setup for the new "PS5 Control" integration. When asked for the
daemon URL, paste the one from step 4 (e.g. `10.0.0.5:8456`). Done.

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

**Pairing fails with "Could not reach PS5".** PS5 must be powered on (not
in rest mode) for the initial pairing. After pairing succeeds, the daemon
can wake it from rest.

**PIN keeps expiring.** Generate a fresh one on the PS5 immediately
before running `install.sh` — PINs are valid ~5 minutes.

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
