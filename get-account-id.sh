#!/usr/bin/env bash
# Wrapper that runs get-account-id.py inside a one-shot Docker container.
# No need to install Python or pyremoteplay locally — Docker handles it.

set -euo pipefail

cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: Docker not found." >&2
  echo "       Install: https://docs.docker.com/engine/install/" >&2
  exit 1
fi

docker run --rm -it \
  --network host \
  -v "$(pwd)/get-account-id.py:/work/get-account-id.py:ro" \
  -w /work \
  python:3.12-slim \
  bash -lc '
    set -e
    # pyremoteplay needs a compile toolchain on armv7l (no PyPI wheels for
    # netifaces/cffi/pycryptodomex). Install build deps before pip.
    apt-get update -qq
    apt-get install -y --no-install-recommends \
      gcc libc6-dev libffi-dev libssl-dev >/dev/null
    pip install --quiet pyremoteplay "pyee<12" "async-timeout>=4.0" 2>&1 | tail -1
    python3 get-account-id.py
  '
