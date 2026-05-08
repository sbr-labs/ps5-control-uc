#!/usr/bin/env bash
# Remote Play pairing — runs pyremoteplay's pairing flow inside a Docker
# image we build once and cache. Produces daemon/credentials.json which
# the daemon mounts read-only.
#
# Why a built+cached image and not a one-shot bash-in-container? PS5
# pairing PINs expire after ~5 minutes. On a 32-bit Raspberry Pi the
# `pip install pyremoteplay` step takes 5–10 minutes (no armv7l wheels;
# pip compiles netifaces/cffi/pycryptodomex from source). If we read the
# PIN before that install runs, the PIN can expire mid-flight. So we
# build the pairing image FIRST (slow first time, cached on retries),
# THEN ask for a fresh PIN, THEN run registration (which is now fast).

set -euo pipefail

PS5_HOST="${1:-}"
if [[ -z "$PS5_HOST" ]]; then
  echo "Usage: pair.sh <PS5_IP>" >&2
  exit 1
fi

cd "$(dirname "$0")/daemon"

# Docker Compose's bind-mount (`./credentials.json:/data/credentials.json:ro`)
# auto-creates the source as a DIRECTORY if it doesn't exist when
# `docker compose up` runs. If a previous failed install ran the daemon
# before pairing succeeded, we end up with credentials.json as an empty
# directory — and Python's `open('credentials.json', 'w')` then crashes
# with `IsADirectoryError`. Detect and clean up before pairing.
if [[ -d credentials.json ]]; then
  echo "==> credentials.json is a directory (left over from a failed install)."

  # Always bring the daemon down — `docker compose ps --status=running`
  # misses containers in 'restarting' state (a crash loop with
  # restart: unless-stopped will re-mount the directory between every
  # crash, defeating cleanup). Unconditional `docker compose down` is
  # idempotent: no-op if nothing is up.
  echo "    Stopping daemon (releases the bind-mount)..."
  docker compose down --remove-orphans --timeout 5 >/dev/null 2>&1 || true

  # Try regular rm, fall back to sudo if Docker created the dir
  # root-owned (common on rootful Docker daemons running as root).
  echo "    Removing the empty directory..."
  if ! rm -rf credentials.json 2>/dev/null; then
    if command -v sudo >/dev/null 2>&1; then
      sudo rm -rf credentials.json
    fi
  fi

  # Verify cleanup actually worked. If it didn't, give the user the
  # exact one-liner to fix it manually instead of failing later in the
  # registration step with the same IsADirectoryError.
  if [[ -e credentials.json ]]; then
    echo "ERROR: could not remove credentials.json — still exists after cleanup." >&2
    echo "       Run this from the daemon dir, then re-run ./pair.sh:" >&2
    echo "         cd $(pwd)" >&2
    echo "         docker compose down" >&2
    echo "         sudo rm -rf credentials.json" >&2
    exit 1
  fi
  echo "    ✓ Cleaned."
fi

CYAN="\033[1;36m"; GRN="\033[1;32m"; YEL="\033[1;33m"; OFF="\033[0m"
say()  { printf "${CYAN}==>${OFF} %s\n" "$1"; }
ok()   { printf "${GRN}✓${OFF}  %s\n" "$1"; }
warn() { printf "${YEL}!${OFF}  %s\n" "$1"; }

cat <<EOF
------------------------------------------------------------
Remote Play pairing — three steps:

1. The script asks for your PSN Account ID. Short Base64 string ending
   in '=', e.g. 'aBc1dEfg23h='. (If you only have the long decimal form
   like '7067298559098XXXXXX', that's fine — we'll convert it.)

2. The script builds the pairing environment in Docker.
     - First time on a Raspberry Pi: 5–10 minutes (compiling Python
       packages from source — no precompiled wheels exist for 32-bit
       ARM). It is NOT stuck. Don't Ctrl+C.
     - Every later run: <30 seconds (Docker layer cache).

3. THEN the script asks for an 8-digit pairing PIN. Generate the PIN
   on the PS5 RIGHT BEFORE typing it: Settings → System → Remote Play
   → Link Device. PINs only live ~5 minutes — that's why we install
   everything before asking for the PIN, so it doesn't expire while
   pip is compiling.

The script will register with your PS5 at ${PS5_HOST} and write
credentials.json next to the daemon files.
------------------------------------------------------------

EOF

# --- Step 1: Account ID ------------------------------------------------------
read -r -p "PSN Account ID (Base64 or decimal): " ACCOUNT_ID
if [[ -z "$ACCOUNT_ID" ]]; then
  echo "Account ID is required." >&2
  exit 1
fi

# Forgiving input: if the user pasted the 19-ish-digit decimal form,
# convert to base64. pyremoteplay's register() only accepts base64.
if [[ "$ACCOUNT_ID" =~ ^[0-9]+$ ]]; then
  warn "Account ID is in decimal form — converting to base64..."
  ACCOUNT_ID=$(python3 -c "import sys, base64; print(base64.b64encode(int(sys.argv[1]).to_bytes(8,'little')).decode())" "$ACCOUNT_ID" 2>/dev/null \
            || docker run --rm python:3.12-slim python -c "import sys, base64; print(base64.b64encode(int(sys.argv[1]).to_bytes(8,'little')).decode())" "$ACCOUNT_ID")
  if [[ -z "$ACCOUNT_ID" ]]; then
    echo "Failed to convert decimal Account ID to base64." >&2
    exit 1
  fi
  ok "Using Account ID (base64): $ACCOUNT_ID"
fi

# --- Step 2: Build the pairing image (slow first time, cached after) --------
echo
say "Building pairing image (first time on Pi: 5–10 min; later: <30s)..."
echo "    If you see lines like 'Building wheel for cffi' and then a long"
echo "    pause, that's pip compiling C extensions — be patient."
echo

# Use a Dockerfile so the apt + pip layers cache between retries. Without
# this, every PIN retry would start the 10-minute install over again.
docker build -t ps5-control-pairing -f - . <<'DOCKERFILE'
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libc6-dev libffi-dev libssl-dev \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir \
    pyremoteplay 'pyee<12' 'async-timeout>=4.0'
WORKDIR /work
DOCKERFILE
ok "Pairing image ready."

# --- Step 3: PIN + register --------------------------------------------------
echo
say "Now generate a fresh 8-digit PIN on the PS5:"
echo "    Settings → System → Remote Play → Link Device"
echo "    The PIN is valid for ~5 minutes — type it as soon as it appears."
echo
read -r -p "8-digit pairing PIN from PS5: " PIN

if [[ -z "$PIN" ]]; then
  echo "PIN is required." >&2
  exit 1
fi

# Run registration — image is cached, so this is just the network step
# plus our small Python script. Should complete in seconds.
# `-i` is REQUIRED here: without it docker doesn't forward stdin to the
# container, so the heredoc-piped script never reaches python and the
# container silently exits 0 with no credentials.json written.
docker run --rm -i \
  --network host \
  -v "$(pwd):/work" \
  -w /work \
  ps5-control-pairing python - <<PYEOF
import json, os, shutil, sys
from pyremoteplay import RPDevice
from pyremoteplay.profile import Profiles, UserProfile

USERNAME = 'shared-user'

# Last-line-of-defence cleanup. If the host-side cleanup in pair.sh
# didn't manage to remove a stale credentials.json/ directory (e.g.
# permission edge case, race with the daemon's restart loop, user on
# an older pair.sh), do it here from inside the registration container
# right before we try to open the file for writing. The container
# runs as root and has write access to /work, so this removes any
# directory that the host-side step left behind.
if os.path.isdir('credentials.json'):
    print('Stale credentials.json/ directory detected inside container — removing.', file=sys.stderr)
    shutil.rmtree('credentials.json')

device = RPDevice('${PS5_HOST}')
if not device.get_status():
    print('ERROR: Could not reach PS5 at ${PS5_HOST}', file=sys.stderr)
    sys.exit(1)

# pyremoteplay 0.7.x: build the user profile manually (Profiles.new_user
# would do an OAuth round-trip from a redirect URL — we already have the
# Account ID, so skip that path).
profiles = Profiles()
profiles.update_user(UserProfile(USERNAME, {'id': '${ACCOUNT_ID}', 'hosts': {}}))

result = device.register(USERNAME, '${PIN}', profiles=profiles, save=False)
if not result:
    print('ERROR: Registration failed. Check the PIN is current and Account ID is correct.', file=sys.stderr)
    sys.exit(2)

# Dump pyremoteplay-native profiles JSON. The daemon's loader accepts
# this format directly (and falls back to ps5-mqtt format if needed).
# dict(profiles) returns the underlying nested-dict storage —
# get_user_profile() would wrap each value in a UserProfile object that
# json.dump can't serialize.
out = dict(profiles)
with open('credentials.json', 'w') as f:
    json.dump(out, f, indent=2)
print('credentials.json written.')
PYEOF

if [[ ! -f credentials.json ]]; then
  echo "Pairing failed — credentials.json not created." >&2
  exit 1
fi

chmod 600 credentials.json
ok "credentials.json saved."
