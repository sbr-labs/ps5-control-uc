#!/usr/bin/env bash
# Remote Play pairing — runs pyremoteplay's pairing flow in a one-shot Docker
# container so users don't need Python installed on the host. Produces
# daemon/credentials.json which the daemon mounts read-only.

set -euo pipefail

PS5_HOST="${1:-}"
if [[ -z "$PS5_HOST" ]]; then
  echo "Usage: pair.sh <PS5_IP>" >&2
  exit 1
fi

cd "$(dirname "$0")/daemon"

cat <<EOF
------------------------------------------------------------
Remote Play pairing — what happens next:

1. The script asks for your PSN Account ID. You can get it via
   https://psn.flipscreen.games or 'psn-account-id' tools — it's a
   short Base64 string like 'aBc1dEfg23h='.

2. Then it asks for an 8-digit pairing PIN. Generate one on the
   PS5: Settings → System → Remote Play → Link Device. The PIN is
   shown on screen for ~5 minutes.

3. The script connects to your PS5 at ${PS5_HOST}, registers, and
   writes credentials.json next to the daemon files.
------------------------------------------------------------

EOF

read -r -p "PSN Account ID (Base64): " ACCOUNT_ID
read -r -p "8-digit pairing PIN from PS5: " PIN

if [[ -z "$ACCOUNT_ID" || -z "$PIN" ]]; then
  echo "Both Account ID and PIN are required." >&2
  exit 1
fi

# Run pyremoteplay's pairing flow in a one-shot container, write
# credentials.json into the daemon dir.
docker run --rm \
  --network host \
  -v "$(pwd):/work" \
  -w /work \
  python:3.12-slim bash -lc "
    set -e
    # pyremoteplay deps (netifaces, cffi, pycryptodomex) ship no prebuilt
    # wheels for armv7l (Raspberry Pi 32-bit), so pip falls back to building
    # from source. python:3.12-slim has no compiler — install the toolchain
    # before pip. On x86_64/arm64 wheels exist and the compile step is
    # skipped; the apt step is then wasted ~30s but harmless.
    apt-get update -qq
    apt-get install -y --no-install-recommends \
      gcc libc6-dev libffi-dev libssl-dev >/dev/null
    pip install --quiet pyremoteplay 'pyee<12' 'async-timeout>=4.0'
    python - <<PYEOF
import json, sys
from pyremoteplay import RPDevice
from pyremoteplay.profile import Profiles, UserProfile

USERNAME = 'shared-user'

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
  "

if [[ ! -f credentials.json ]]; then
  echo "Pairing failed — credentials.json not created." >&2
  exit 1
fi

chmod 600 credentials.json
echo "✓ credentials.json saved."
