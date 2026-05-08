# Changelog

All notable changes to this project will be documented in this file. The
format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
