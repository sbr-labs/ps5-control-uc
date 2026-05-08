# Changelog

All notable changes to this project will be documented in this file. The
format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
