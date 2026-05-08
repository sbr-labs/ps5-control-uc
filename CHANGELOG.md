# Changelog

All notable changes to this project will be documented in this file. The
format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.20] - 2026-05-08

### Added
- **Default home / fallback image bundled in the Docker image.** The
  daemon's `/home_image` endpoint previously returned nothing if the
  user didn't set `HOME_IMAGE_FILE` or `HOME_IMAGE_URL`, so the
  Remote 3 media-player widget showed a blank box when the PS5 was
  on the home screen. v0.4.20 ships a generic gamepad SVG at
  `/app/default-home-image.svg` and `docker-compose.yml` defaults
  `HOME_IMAGE_FILE` to that path. The art is original (no Sony
  trademarks) so it's safe to redistribute.
- Override knobs in `.env` for users who want their own image:
  - `HOME_IMAGE_URL=https://example.com/my.png` — daemon serves that URL as the media image
  - `HOME_IMAGE_FILE=/path/in/container/my.png` — daemon reads from disk (mount via `volumes:`)

## [0.4.19] - 2026-05-08

### Added
- README troubleshooting entry for cross-subnet / VLAN setups. PS5
  and daemon-host on different subnets is a common cause of silent
  "connection refused" — `ping` works, but TCP/UDP between subnets
  is blocked by router/firewall rules (typical "guest network
  isolation" or VLAN ACL behaviour). Documented the
  `ping`+`nc` diagnostic and recommended the same-subnet fix.

## [0.4.18] - 2026-05-08

### Fixed
- **Connection refused from the Remote 3 when the daemon runs on
  Docker Desktop for Mac.** Cause: the docker-compose.yml used
  `network_mode: host`, which on Linux and OrbStack means "the host's
  real network", but on Docker Desktop for Mac means "the internal
  Linux VM Docker Desktop runs Docker in" — NOT the Mac's actual
  LAN. The daemon was invisible to anything on the LAN regardless of
  which Mac IP (LAN or Wi-Fi) the user typed into the Remote 3
  integration setup, and turning off the firewall didn't help
  because there was nothing listening at the Mac's LAN IP at all.
  Switched to `ports: ["8456:8456"]` (Docker bridge networking with
  a published port). Same effect on Linux/OrbStack as before, and
  finally works on Docker Desktop Mac. Verified locally: daemon
  reachable on both `localhost:8456` and `<mac-lan-ip>:8456` (HTTP
  200 from both), and outbound connections to the PS5 still work
  fine through the bridge NAT.

## [0.4.17] - 2026-05-08

### Added
- Daemon now serves a small status + quick-start page at `GET /` —
  visible in any browser pointed at the daemon's URL. Shows live
  status (host:port the user reached the daemon at, PS5 IP it's
  configured for), a one-click download link for the UC Remote 3
  integration tarball, and a prefilled `<host>:8456` daemon URL the
  user copies into the Remote 3 setup. Saves manual googling of the
  GitHub release URL.
- Used by the HA add-on's "Open Web UI" button (HA reads the
  `webui:` directive in `config.yaml` and surfaces a button in the
  add-on UI that opens this page in a new tab).

## [0.4.16] - 2026-05-08

### Added
- `install.sh` now **verifies the daemon is actually responding** on
  `:8456` (via `curl http://localhost:8456/health`) before declaring
  success. Previously the container could start, the Python daemon
  inside could crash on bad credentials / unreachable PS5 / port
  collision, and `install.sh` would print "Done!" while the Remote 3
  saw connection refused. Now the script polls health for up to 15s
  and aborts with a clear "check `docker compose logs ps5-control`"
  pointer if the daemon never comes up.
- README troubleshooting block for "Remote 3 says connection refused"
  with a single one-liner that prints all five common-cause
  diagnostics (container state, daemon log, local curl, listen-IP,
  daemon-host LAN IP) so users can paste output and see exactly which
  layer is broken.

## [0.4.15] - 2026-05-08

### Fixed
- `install.sh` failed at the final `docker compose up -d` step with
  `Error response from daemon: Conflict. The container name
  "/ps5-control" is already in use by container "<id>"` when a previous
  install attempt had left a stopped container behind (Ctrl+C, crash
  loop, prior failed install, etc.). `update.sh` already used
  `--force-recreate` to handle this case but `install.sh` didn't. Added
  `docker compose down --remove-orphans` (idempotent — no-op when
  nothing is up) plus `docker compose up -d --force-recreate` to the
  install end-of-flow.

## [0.4.14] - 2026-05-08

### Fixed
- `install.sh` crashed with `bad substitution` on macOS at the OAuth
  prompt: `if [[ "${RUN_OAUTH,,}" == "y" ]]`. The `${VAR,,}` (lowercase
  conversion) is bash 4+ syntax, but **macOS ships with bash 3.2** by
  default (Apple stopped updating bash in 2007 due to GPL3). Replaced
  with a portable `case` glob (`[Yy]|[Yy][Ee][Ss]`) that works
  identically on bash 3.2 (macOS) and bash 4+/5 (Linux). Verified on
  macOS arm64 + OrbStack, Linux arm64, Linux x86_64, plus a deliberate
  broken-state recovery test (root-owned files + stale
  `credentials.json/` directory) on Linux arm64.

## [0.4.13] - 2026-05-08

### Added
- `install.sh`, `pair.sh`, and `update.sh` now **self-heal root-owned
  files** left over from previous Docker bind-mount activity. Docker
  writes inside containers as root, and bind-mount sources on the host
  end up root-owned — which then blocks subsequent `git pull` / `git
  reset` / `chmod` / `rm` operations for non-root users. The tester hit
  this between v0.4.11 and v0.4.12: `git reset --hard origin/main`
  failed with `error: unable to create file daemon/Dockerfile:
  Permission denied` because Docker had previously written into
  `daemon/` as root. All three scripts now detect any non-current-user-
  owned file under their working directory and `sudo chown -R` it back
  to the current user before continuing. Idempotent and quiet when
  nothing needs fixing.

## [0.4.12] - 2026-05-08

### Changed
- `pair.sh`'s final `chmod 600 credentials.json` is now best-effort
  with sudo fallback and a friendly explanation if it can't run.
  Previously it printed `chmod: changing permissions of
  'credentials.json': Operation not permitted` after a *successful*
  pairing because Docker creates the file as root (via the bind
  mount in the registration container) and the host's shell user
  can't `chmod` a root-owned file. The chmod is purely
  defence-in-depth — the daemon reads the file as root regardless,
  so functionality is unaffected — but the error looked alarming
  next to the success message. Now it tries `chmod`, falls back to
  `sudo chmod`, and otherwise prints a one-line note instead of an
  error.

## [0.4.11] - 2026-05-08

### Fixed
- v0.4.10's host-side cleanup of the stale `credentials.json/`
  directory wasn't enough on every setup: even with unconditional
  `docker compose down --remove-orphans` and `sudo rm -rf` fallback,
  some users still hit `IsADirectoryError` at the
  `with open('credentials.json', 'w')` line. Cause is environment-
  specific (race with restart loop, network mount, permission edge
  case, or user not actually on v0.4.10). Added a last-line-of-
  defence cleanup *inside* the registration container's Python
  script — `if os.path.isdir(...): shutil.rmtree(...)` immediately
  before the file open. The container runs as root with write access
  to the mounted volume, so this removes any directory the host-side
  step missed.

## [0.4.10] - 2026-05-08

### Fixed
- v0.4.9's `credentials.json`-directory cleanup didn't always work.
  Two failures uncovered by the tester:
  1. `docker compose ps --status=running --quiet` misses containers
     in `restarting` state — when the daemon is in a crash loop
     (`restart: unless-stopped` keeps respawning it after each
     `IsADirectoryError`), it spends most of its time in the
     "restarting" phase, not "running", so the conditional skipped
     `docker compose down` and the restart loop re-mounted the
     directory between cleanup attempts.
  2. Docker on some setups (e.g. rootful daemon) creates the
     bind-mount source as root-owned — a regular user's `rm -rf`
     failed silently with a permission error.
- `pair.sh` now: unconditionally `docker compose down --remove-orphans`
  (idempotent — no-op if nothing is up), tries `rm -rf`, falls back to
  `sudo rm -rf` if Docker made the dir root-owned, and *positively
  verifies* `credentials.json` no longer exists before continuing.
  If it still can't be removed, prints the exact 3-line manual
  recovery instead of failing later with the same opaque error.

## [0.4.9] - 2026-05-08

### Fixed
- Pairing crashed with `IsADirectoryError: [Errno 21] Is a directory:
  'credentials.json'` after entering the PIN. Cause: Docker Compose's
  `./credentials.json:/data/credentials.json:ro` bind-mount auto-creates
  the source as an **empty directory** if the file doesn't exist when
  `docker compose up` runs. If a previous (failed) install attempt
  brought the daemon up before pairing succeeded — or the user ran
  `docker compose up` manually — the host ended up with
  `credentials.json` as a directory, and Python's
  `open('credentials.json', 'w')` then refused to write through it.
  `pair.sh` now detects this stale state at startup, brings the daemon
  down if it's running (so the bind-mount releases the directory),
  and `rm -rf`s the empty directory before running registration.

## [0.4.8] - 2026-05-08

### Fixed
- Integration tarball was rejected by the UC Remote 3 firmware with
  *"Invalid archive, missing data in archive, or contained metadata
  cannot be read"* (German: *"Ungültiges Archiv, fehlende Daten im
  Archiv oder enthaltene Metadaten können nicht gelesen werden"*).
  Cause: `driver.json` shipped with `"home_page": ""` (empty string),
  which fails the firmware's URL-format validation on the integration
  manifest. Set to `https://github.com/sbr-labs/ps5-control-uc`.
- `driver.json` `version` and `release_date` were stuck at `0.4.0` /
  `2026-05-03` from the initial release; now match the package
  version + ship date.

### Added
- German translations (`"de"` entries) for `name`, `description`,
  `setup_data_schema.title`, and the setup info label. The UC Remote 3
  firmware picks the user's locale automatically — German users now
  see the integration with localised strings instead of falling back
  to the English ones.

## [0.4.7] - 2026-05-08

### Fixed
- `pair.sh` `docker run` was missing the `-i` flag. After v0.4.6 split
  the install step into a separate `docker build` and the registration
  step into a `docker run ... ps5-control-pairing python -`, the
  heredoc-piped script never reached the container's `python` because
  `docker run` without `-i` doesn't forward stdin. Result: container
  read empty stdin, exited 0 silently, no `credentials.json` written,
  user got the inscrutable "Pairing failed — credentials.json not
  created" with no upstream error to debug from. Verified by repro
  with `python -` reading sys.exit(2) — without `-i` exits 0, with
  `-i` exits 2 as expected.

## [0.4.6] - 2026-05-08

### Fixed
- **PIN-expiration race on Raspberry Pi armv7l.** The previous flow asked
  for the 8-digit pairing PIN, *then* spent 5–10 minutes installing
  Python deps inside the container (pip compiling
  `netifaces`/`cffi`/`pycryptodomex` from source — no armv7l wheels on
  PyPI), *then* tried to register with the PS5. PINs are valid ~5 min,
  so the PIN often expired mid-flight even when the user waited
  patiently. The Pi tester also Ctrl+C'd thinking it was hung, because
  `pip install --quiet` produces several minutes of silent output.
  `pair.sh` now installs everything via `docker build` *before* asking
  for the PIN: the build is slow once (5–10 min on Pi), cached on every
  retry (<30s), and the post-PIN registration step runs in seconds
  while the PIN is still fresh.
- `pair.sh` user-facing messaging now explicitly tells the user the
  build phase is the slow one and warns them not to Ctrl+C, plus
  explains *why* deps are installed before the PIN prompt (so the PIN
  doesn't expire). Status output uses coloured `==>` / `✓` / `!`
  markers so progress is visible.

## [0.4.5] - 2026-05-08

### Fixed
- `get-account-id.py` was printing the **decimal** form of the PSN
  Account ID (e.g. `7067298559098XXXXXX`) instead of the **base64**
  form (`aBc1dEfg23h=`). Sony's OAuth response carries the same value
  in both representations — `user_id` is decimal, `user_rpid` is
  base64 — and `pyremoteplay.register()` only accepts the base64 form.
  The script was reading `account_id` / `user_id` first; now it reads
  `user_rpid` first, with a fallback that converts decimal → base64
  locally (`base64.b64encode(int(decimal).to_bytes(8, "little"))`) if
  `user_rpid` is missing for any reason.
- `pair.sh` is now forgiving: if the user pastes the 19-digit decimal
  form of the Account ID (e.g. from an older `get-account-id.sh`
  run, or from a Sony page that shows it that way), it auto-converts
  to base64 before calling `pyremoteplay.register()` instead of
  failing on the inscrutable error pyremoteplay would otherwise throw.

## [0.4.4] - 2026-05-08

### Fixed
- Pairing crashed with `TypeError: Profiles.new_user() got multiple values
  for argument 'save'` immediately after entering the PIN, on every host
  (not arch-specific). `pair.sh` was written against an older
  `pyremoteplay` API. The current `Profiles.new_user(redirect_url, save)`
  expects a PSN OAuth redirect URL and runs the OAuth flow internally —
  but we already have the Account ID, so that path doesn't apply.
  `pair.sh` now constructs a `UserProfile` directly with the supplied
  Account ID, registers via `device.register(name, pin, profiles=...)`,
  and dumps the resulting profiles dict (`dict(profiles)`) as
  `credentials.json`. Reproduced + verified against pyremoteplay 0.7.6.

### Changed
- Daemon's `build_profiles_from_creds()` now accepts the native
  pyremoteplay format that `pair.sh` writes (`{username: {"id": ...,
  "hosts": {...}}}`) in addition to the legacy ps5-mqtt format
  (`{key: {"accountId": ..., "registration": {"PS5-...": ...}}}`).
  Existing ps5-mqtt-imported credentials still work unchanged.

## [0.4.3] - 2026-05-08

### Changed
- README install section rewritten as a beginner-friendly step-by-step
  walkthrough. Calls out the slow first-time `pip install` on 32-bit
  Raspberry Pi explicitly (5–10 min while pip compiles
  netifaces/cffi/pycryptodomex from source) so users don't think the
  installer is hung.
- New troubleshooting entry for "install stuck on `pip install`" and
  for `update.sh` failing to fast-forward after a remote force-push.

### Fixed
- `update.sh` now detects when the remote `main` history has been
  rewritten (force-push) and resets the local branch to `origin/main`
  automatically instead of bailing on `git pull --ff-only`. Local
  edits were already stashed earlier in the script, and
  `credentials.json` + `.env` are gitignored, so the reset is safe.

## [0.4.2] - 2026-05-08

### Fixed
- Pairing and OAuth helper failed on Raspberry Pi OS Bullseye (armv7l, 32-bit
  ARM) with `error: command 'gcc' failed: No such file or directory`. The
  one-shot `python:3.12-slim` containers used by `pair.sh` and
  `get-account-id.sh` had no compiler, and PyPI ships no armv7l wheels for
  `netifaces` / `cffi` / `pycryptodomex`, so pip fell back to source builds
  that immediately broke. Both scripts now `apt-get install gcc libc6-dev
  libffi-dev libssl-dev` inside the container before `pip install`.
- `pair.sh` and `get-account-id.sh` now also install `async-timeout>=4.0`
  alongside `pyremoteplay`. It's a transitive runtime dep (via
  `pyps4_2ndscreen`) that pip's resolver doesn't pull automatically, so
  `from pyremoteplay import RPDevice` raised `ModuleNotFoundError: No
  module named 'async_timeout'` on every host once the gcc error was past.
- `daemon/Dockerfile` now also installs `libffi-dev` + `libssl-dev` so the
  daemon image builds cleanly on armv7l (cffi needs `ffi.h`).

## [0.4.1] - 2026-05-06

### Added
- `get-account-id.sh` + `get-account-id.py` — Sony OAuth-based PSN
  Account ID lookup. Works for **private PSN profiles** too (the
  flipscreen.games tool only works for public profiles). Runs inside a
  one-shot Docker container, no Python install needed locally.
- `install.sh` now offers to run the OAuth helper at the pairing step
  when you don't already have your Account ID.
- README updated with both Account ID lookup paths.

## [0.4.0] - 2026-05-06

Initial public release.

### Added
- Python daemon (`daemon/`) wraps `pyremoteplay` and exposes a REST API on
  port 8456 for button presses, wake/standby, app launch, and state.
- Unfolded Circle Remote 3 integration (`integration/`) forwards button
  presses to the daemon over HTTP.
- One-shot installer (`install.sh`) handles Docker check, PS5 IP collection,
  Remote Play pairing (via `pair.sh`), build, and start.
- One-shot updater (`update.sh`) pulls from GitHub, rebuilds the daemon,
  and tells you if the integration tarball needs re-uploading to the
  Remote 3.
- Integration build script (`integration/build.sh`) for those who want
  to verify or modify the source.
