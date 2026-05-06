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
  echo "     1. Your PSN account (logged in at https://www.playstation.com)"
  echo "     2. The 8-digit pairing PIN from your PS5"
  echo "        (PS5 → Settings → System → Remote Play → Link Device)"
  echo
  read -r -p "Press Enter when ready (or Ctrl+C to cancel)..."
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

say "Starting daemon..."
docker compose up -d
ok "Daemon running."

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
