#!/usr/bin/env bash
# PS5 Control — one-shot installer for Linux + Docker.
# Walks through Remote Play pairing, builds the daemon container, and prints
# the URL to paste into the Unfolded Circle Remote 3 integration setup.

set -euo pipefail

cd "$(dirname "$0")/daemon"

CYAN="\033[1;36m"; GRN="\033[1;32m"; YEL="\033[1;33m"; RED="\033[1;31m"; OFF="\033[0m"
say() { printf "${CYAN}==>${OFF} %s\n" "$1"; }
ok()  { printf "${GRN}✓${OFF}  %s\n" "$1"; }
warn() { printf "${YEL}!${OFF}  %s\n" "$1"; }
err()  { printf "${RED}✗${OFF}  %s\n" "$1" >&2; }

# --- 1. Docker check ----------------------------------------------------------
say "Checking Docker..."
if ! command -v docker >/dev/null 2>&1; then
  err "Docker not found."
  echo "   Install: https://docs.docker.com/engine/install/"
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  err "Docker Compose v2 not found ('docker compose' command)."
  echo "   On Debian/Ubuntu: sudo apt install docker-compose-plugin"
  exit 1
fi
ok "Docker $(docker --version | awk '{print $3}' | tr -d ',') ready."

# --- 1b. Self-heal root-owned files in daemon/ -------------------------------
# Docker bind-mounts can leave root-owned files behind (e.g. credentials.json
# or its parent directory created during a failed install attempt with an
# older version of this script). On a `git pull` to a fixed version those
# root-owned files would block git from updating the working tree
# ("error: unable to create file ...: Permission denied"), and the user gets
# stuck. Detect any non-current-user-owned regular files / dirs under here
# and chown them back, so subsequent `git pull` / `docker compose` work.
if [[ -n "${USER:-}" ]] && find . -mindepth 1 -not -user "$USER" -print -quit 2>/dev/null | grep -q .; then
  say "Found root-owned files in daemon/ (left over from a previous Docker run). Fixing ownership..."
  if command -v sudo >/dev/null 2>&1 && sudo chown -R "$USER":"$(id -gn)" . 2>/dev/null; then
    ok "Ownership of daemon/ reset to $USER."
  else
    warn "Could not chown daemon/ (no sudo or chown failed). Continuing — may fail later."
  fi
fi

# --- 2. PS5 IP ----------------------------------------------------------------
ENV_FILE=".env"
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
fi
if [[ -z "${PS5_HOST:-}" ]]; then
  echo
  say "Enter your PS5's local IP address."
  echo "   (PS5 → Settings → Network → Connection Status → IP Address)"
  read -r -p "PS5 IP: " PS5_HOST
  if [[ -z "$PS5_HOST" ]]; then
    err "PS5 IP is required."; exit 1
  fi
  echo "PS5_HOST=${PS5_HOST}" > "$ENV_FILE"
  ok "Saved to daemon/.env"
else
  ok "Using existing PS5_HOST=${PS5_HOST} from .env"
fi

# --- 3. Remote Play pairing ---------------------------------------------------
if [[ ! -f "credentials.json" ]]; then
  echo
  say "Pairing with PS5 Remote Play..."
  echo "   This opens the pyremoteplay pairing flow inside a one-shot Docker"
  echo "   container. You'll need:"
  echo "     1. Your PSN Account ID (Base64 string, e.g. 'aBc1dEfg23h=')"
  echo "        - Public PSN profile: https://psn.flipscreen.games"
  echo "        - Private profile:    run ./get-account-id.sh first"
  echo "          (uses Sony OAuth, works regardless of privacy settings)"
  echo "     2. The 8-digit pairing PIN from your PS5"
  echo "        (PS5 → Settings → System → Remote Play → Link Device)"
  echo
  read -r -p "Need to look up your Account ID via OAuth first? (y/N): " RUN_OAUTH
  # `${VAR,,}` (lowercase) is bash-4-only — macOS ships bash 3.2 and would
  # bail with `bad substitution` here, killing the whole installer. Use a
  # portable case-glob to match Y / y / YES / yes / etc.
  case "$RUN_OAUTH" in
    [Yy]|[Yy][Ee][Ss])
      ( cd .. && bash get-account-id.sh ) || warn "OAuth lookup failed — try https://psn.flipscreen.games instead"
      echo
      ;;
  esac
  read -r -p "Press Enter when ready to pair (or Ctrl+C to cancel)..."
  bash ../pair.sh "$PS5_HOST"
  if [[ ! -f "credentials.json" ]]; then
    err "Pairing did not produce credentials.json — aborting."
    exit 1
  fi
  ok "Pairing complete."
else
  ok "credentials.json already present, skipping pairing."
fi

# --- 4. Build + start ---------------------------------------------------------
echo
say "Building daemon container..."
docker compose build
ok "Build OK."

# Pre-create psn_tokens.json as an empty JSON file. If it doesn't exist
# on the host before `docker compose up`, Docker's bind mount creates it
# as a *directory* (known gotcha) which then breaks the daemon when it
# tries to read/write it as a file. Pre-creating an empty {} fixes this.
if [[ ! -f psn_tokens.json ]]; then
  if [[ -d psn_tokens.json ]]; then
    say "Removing stray psn_tokens.json directory left by Docker bind mount..."
    rmdir psn_tokens.json 2>/dev/null || sudo rmdir psn_tokens.json
  fi
  echo '{}' > psn_tokens.json
fi

say "Starting daemon..."
# Bring down any stale container first. If a previous install attempt left
# `ps5-control` in stopped/exited state (Ctrl+C, crash loop, etc.), the
# next `docker compose up -d` would fail with:
#   "Conflict. The container name '/ps5-control' is already in use"
# `docker compose down --remove-orphans` is idempotent — no-op when nothing
# is up — so always safe.
docker compose down --remove-orphans >/dev/null 2>&1 || true
docker compose up -d --force-recreate
ok "Container started."

# Verify the daemon is *actually* responding on :8456 — not just that the
# container is up. The container can start successfully and then the
# Python process inside crashes (bad credentials.json, PS5 unreachable
# at boot, etc.) — without this check, install.sh would print "Done!"
# while the Remote 3 sees connection refused.
say "Waiting for daemon HTTP API to come up on :8456..."
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
  if curl -fsS -m 2 -o /dev/null http://localhost:8456/health 2>/dev/null; then
    ok "Daemon responding on :8456 (after ${i}s)."
    break
  fi
  if [[ $i -eq 15 ]]; then
    err "Daemon container is up but NOT responding on :8456 after 15s."
    err "Likely the Python daemon crashed inside the container. Check:"
    err "    cd daemon && docker compose logs ps5-control"
    err "Common causes: invalid credentials.json, PS5 unreachable at boot,"
    err "or another process already on :8456. Fix and run ./install.sh again."
    exit 1
  fi
  sleep 1
done

# --- 5. Print summary ---------------------------------------------------------
HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "<this-machine-IP>")
PORT="${LISTEN_PORT:-8456}"
echo
echo "============================================================"
ok "Done."
echo
echo "Daemon is listening on:    ${HOST_IP}:${PORT}"
echo "View logs:                 cd daemon && docker compose logs -f"
echo "Stop daemon:               cd daemon && docker compose down"
echo
say "Next: install the Remote 3 integration"
echo "   1. Copy 'integration/ps5-uc-integration.tar.gz' to your phone or laptop."
echo "   2. Open the Remote 3 web configurator (Settings → Integrations)."
echo "   3. Click 'Upload custom integration', select the tarball."
echo "   4. Run setup. When asked for daemon IP:port, enter:"
echo "        ${HOST_IP}:${PORT}"
echo "============================================================"
