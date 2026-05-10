#!/usr/bin/env bash
# PS5 Control — update to the latest version from GitHub.
# Pulls source, rebuilds the daemon container, and prints any post-update
# steps (e.g. re-uploading the integration tarball if it changed).

set -euo pipefail

cd "$(dirname "$0")"

CYAN="\033[1;36m"; GRN="\033[1;32m"; YEL="\033[1;33m"; RED="\033[1;31m"; OFF="\033[0m"
say() { printf "${CYAN}==>${OFF} %s\n" "$1"; }
ok()  { printf "${GRN}✓${OFF}  %s\n" "$1"; }
warn() { printf "${YEL}!${OFF}  %s\n" "$1"; }
err()  { printf "${RED}✗${OFF}  %s\n" "$1" >&2; }

# --- 1. Sanity ----------------------------------------------------------------
if [[ ! -d .git ]]; then
  err "Not inside a git checkout. Updates require 'git clone'-ing the repo,"
  err "not extracting a downloaded zip."
  err "Workaround: re-clone the repo, run install.sh fresh, your"
  err "credentials.json + .env will need to be re-created."
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  err "Docker required."; exit 1
fi

# --- 1b. Self-heal root-owned files left over by Docker ---------------------
# Docker bind-mounts can write files owned by root, which then block
# `git pull` / `git reset` for non-root users. Detect and chown back before
# attempting the pull. Same defensive pattern install.sh and pair.sh use.
if [[ -n "${USER:-}" ]] && find . -mindepth 1 -not -path './.git/*' -not -user "$USER" -print -quit 2>/dev/null | grep -q .; then
  say "Found root-owned files (left over from a previous Docker run). Fixing ownership..."
  if command -v sudo >/dev/null 2>&1 && sudo chown -R "$USER":"$(id -gn)" . 2>/dev/null; then
    ok "Ownership reset to $USER."
  else
    warn "Could not chown (no sudo or chown failed). git pull may fail."
  fi
fi

# --- 2. Track current version ------------------------------------------------
PREV_REV=$(git rev-parse HEAD 2>/dev/null || echo "unknown")
PREV_TARBALL_HASH=""
if [[ -f integration/ps5-uc-integration.tar.gz ]]; then
  PREV_TARBALL_HASH=$(shasum -a 256 integration/ps5-uc-integration.tar.gz 2>/dev/null | awk '{print $1}' || sha256sum integration/ps5-uc-integration.tar.gz | awk '{print $1}')
fi

# --- 3. Stash local edits (if any) -------------------------------------------
if ! git diff --quiet || ! git diff --cached --quiet; then
  warn "You have local changes — stashing before pull (you can 'git stash pop' after)."
  git stash push -m "pre-update auto-stash $(date -u +%Y-%m-%dT%H:%M:%SZ)" || true
fi

# --- 4. Pull -----------------------------------------------------------------
say "Pulling latest from GitHub..."
git fetch origin
DEFAULT_BRANCH=$(git remote show origin | sed -n 's/.*HEAD branch: //p')
DEFAULT_BRANCH=${DEFAULT_BRANCH:-main}

# If origin's history was rewritten (force-push) the local branch can't
# fast-forward. Detect by checking whether the local HEAD is still an
# ancestor of origin/<default>; if not, reset hard. Local edits were
# already stashed in step 3, and credentials.json + .env are gitignored,
# so reset --hard is safe.
if ! git merge-base --is-ancestor HEAD "origin/${DEFAULT_BRANCH}"; then
  warn "Remote history was rewritten — resetting local branch to origin/${DEFAULT_BRANCH}."
  git checkout "$DEFAULT_BRANCH" 2>/dev/null || git checkout -B "$DEFAULT_BRANCH" "origin/${DEFAULT_BRANCH}"
  git reset --hard "origin/${DEFAULT_BRANCH}"
else
  git pull --ff-only origin "$DEFAULT_BRANCH"
fi

NEW_REV=$(git rev-parse HEAD)
if [[ "$PREV_REV" == "$NEW_REV" ]]; then
  ok "Already up to date ($NEW_REV)."
else
  ok "Updated $PREV_REV -> $NEW_REV"
fi

# --- 5. Rebuild + restart daemon ---------------------------------------------
cd daemon
# Pre-create psn_tokens.json as a real file (not a Docker-created directory).
# Bind-mounting a non-existent file path makes Docker create it as a dir;
# the daemon then errors with IsADirectoryError on first start. Existing
# users who hit this in v0.5.0–v0.5.3 get a one-time fix here.
if [[ -d psn_tokens.json ]]; then
  warn "Removing stray psn_tokens.json directory created by Docker bind mount..."
  rmdir psn_tokens.json 2>/dev/null || sudo rmdir psn_tokens.json
fi
if [[ ! -f psn_tokens.json ]]; then
  echo '{}' > psn_tokens.json
fi
say "Rebuilding daemon container..."
docker compose build
say "Restarting daemon..."
docker compose up -d --force-recreate
ok "Daemon running."
cd ..

# --- 6. Did the integration tarball change? ----------------------------------
NEW_TARBALL_HASH=""
if [[ -f integration/ps5-uc-integration.tar.gz ]]; then
  NEW_TARBALL_HASH=$(shasum -a 256 integration/ps5-uc-integration.tar.gz 2>/dev/null | awk '{print $1}' || sha256sum integration/ps5-uc-integration.tar.gz | awk '{print $1}')
fi

echo
echo "============================================================"
ok "Update complete."
if [[ -n "$NEW_TARBALL_HASH" && "$PREV_TARBALL_HASH" != "$NEW_TARBALL_HASH" ]]; then
  warn "The Remote 3 integration tarball changed in this update."
  echo "    -> Re-upload integration/ps5-uc-integration.tar.gz to your Remote 3:"
  echo "       (Settings -> Integrations -> Custom -> Upload)"
else
  ok "Remote 3 integration unchanged — no upload needed."
fi
echo "============================================================"
