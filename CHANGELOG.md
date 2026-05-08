# Changelog

All notable changes to this project will be documented in this file. The
format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
