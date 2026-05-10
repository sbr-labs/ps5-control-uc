# PSN Presence — design notes (v0.5.0 candidate, NOT yet shipped)

## Problem
PS5 firmware 13.x removed `running-app-name` and `running-app-titleid` from the local DDP broadcast. The daemon's `RPDevice.app_name` / `app_id` come back empty regardless of what's on screen. As a result:
- Media-player widget shows the home image instead of game cover art
- DRM auto-disconnect can't fire (no app name to match)
- `/state` always reports `app: ""`

## Solution
Query Sony's PSN REST API for "currently playing" instead of relying on local DDP. Same data the official PS App uses on a friend's profile.

## Pieces shipped on this branch (`feat/psn-presence`)

1. **`psn_presence.py`** — standalone module, hand-rolled HTTP client (no new dep). Handles npsso → tokens → presence.
2. **`test_psn_presence.py`** — manual smoke harness to verify the module works against the user's account before integrating.

## Validation status (2026-05-10)

- ✅ Module verified end-to-end on user's account: auth flow, /me decimal-id resolution, basicPresences, catalog cover-art fallback all working. Returned `{"app_name": "Call of Duty®", "app_id": "PPSA07950_00", "image_url": "https://image.api.playstation.com/vulcan/.../...png"}` for an active game.
- ⏳ Streaming-app detection unvalidated — needs test pass while Sky Go / Netflix / etc. are foreground. Decides whether PSN presence reports app titles for streaming apps (it should, but Sony's behaviour for streaming is less consistent than for games).

## DRM auto-disconnect — **decided MANUAL ONLY** (2026-05-10)

User confirmed keeping DRM session-teardown as a manual action, not automatic. Reason: when the daemon tears down the Remote Play session for DRM apps (Sky Go, HBO Max, etc.), the streaming plays — but the Remote 3 buttons stop working entirely (no RP session = no path for buttons). That trade-off is too surprising as a default; user would experience "remote stopped working when I opened Sky Go." Manual gives the user full control over when remote-control is sacrificed for playback.

**v0.5.0 plan for DRM:**
- **Remove** the existing `drm_watcher_loop()` in `daemon.py` (currently dead code on firmware 13 anyway, since DDP doesn't report streaming-app names; would become live with PSN presence and we don't want that surprise).
- **Keep** `/disconnect?pause=N` and `/reconnect` HTTP endpoints (already implemented in daemon). These are the manual tools.
- **Document** the manual workflow in the README:
  - Build a Remote 3 activity called "Sky Go" / "HBO Max" / etc. that:
    1. Calls `POST /disconnect?pause=300` (5-minute pause prevents auto-reconnect)
    2. Opens the streaming app via the regular activity flow
  - Build a "Resume PS5" activity that calls `POST /reconnect` to restore Remote 3 control
- **HA equivalent**: existing `rest_command:` block in the README already maps these. Add explicit examples for "disconnect for Sky Go" and "reconnect when done".

The PSN presence module still benefits the cover-art use case independently of DRM. v0.5.0 = cover art works correctly; DRM remains a manual workflow.

## Pieces NOT yet shipped (to-do before v0.5.0 release)

3. **Daemon integration** — wire `PsnPresence` into `daemon.py`:
   - Read `enable_psn_presence` + `psn_npsso_token` from env / addon config
   - At startup, instantiate `PsnPresence(token_path="/data/psn_tokens.json", account_id=ACCOUNT_ID)`
   - Call `psn.start(npsso=PSN_NPSSO_TOKEN)` once
   - Background task polling `psn.fetch_presence()` every 30 s
   - Cache result on the controller; have `app_name` / `app_id` / `app_image` properties prefer PSN cache over DDP

4. **HA addon plumbing**:
   - `config.yaml`: add `enable_psn_presence: false`, `psn_npsso_token: ""` options
   - `translations/en.yaml`: descriptions for both new fields with link to ssocookie page
   - `run.sh`: pass through `PSN_PRESENCE_ENABLED` and `PSN_NPSSO_TOKEN` env vars

5. **Standalone Docker plumbing**:
   - `daemon/.env.example`: document `PSN_PRESENCE_ENABLED` and `PSN_NPSSO_TOKEN`
   - `daemon/docker-compose.yml`: pass them through

6. **Docs** — README sections for both repos, framed as opt-in:
   > **Want game cover art on the media-player widget?** Optional one-time setup: paste your PSN npsso cookie into the addon config / `.env`. Daemon then queries PSN for "currently playing" every 30s and the cover art appears on the widget. Without this, the widget shows your home image.

7. **Test plan** (to run with the user when they're available):
   - [ ] Test harness passes: `NPSSO=... ACCOUNT_ID=... python3 test_psn_presence.py`
   - [ ] Returns real presence when a game is running on the PS5
   - [ ] Returns `{}` when PS5 is off / streaming app foreground
   - [ ] Force-expire access_token → refresh succeeds, presence still works
   - [ ] Daemon log shows `psn:` lines without errors over a 5-minute run
   - [ ] `/state` now returns populated `app` and `image_url` when a game is running
   - [ ] Existing-user migration: enable flag + paste npsso, addon picks it up on restart
   - [ ] Failure path: invalid npsso → daemon logs clear error, doesn't crash, falls back to DDP/home-image

## Design decisions / why

- **Hand-rolled, no `psnawp_api` dep.** Sony's API is plain HTTP/JSON; we only need two endpoints (token + presence). `psnawp_api` pulls in `pyrate_limiter`, `requests`, etc. — image growth + risk of upstream breakage. ~200 lines vs 1 dep.
- **NPSSO not OAuth-redirect.** Originally considered reusing the existing OAuth-code from pairing, but pyremoteplay uses a different OAuth `client_id` than the PSN-API one — auth codes aren't interchangeable across OAuth clients. NPSSO is the one-shot bootstrap path used by every community PSN library; user pastes once, daemon handles refresh forever.
- **30 s poll interval.** Conservative under Sony's rate limits (community libraries comfortably do ~10s). Fast enough that cover art lag is tolerable; slow enough to never get throttled.
- **Tokens persisted at `/data/psn_tokens.json`.** HA addon's persistent storage; survives addon updates and HAOS reboots. Same volume as `credentials.json`.
- **Atomic write (`tmp` + `os.replace`).** Survives a crash mid-write — never leaves `psn_tokens.json` half-written and corrupt.
- **Opt-in feature flag.** Default `enable_psn_presence: false`. Existing users who don't care don't pay the auth-ceremony cost; users who want it flip the flag and paste npsso once.

## Constants verified at design time (2026-05-10)

- `client_id` = `09515159-7237-4370-9b40-3806e67c0891` (Sony PS App Android)
- `client_secret` = `ucPjka5tntB2KqsP` (decoded from PSNAWP's `AUTH_BASIC`)
- `redirect_uri` = `com.scee.psxandroid.scecompcall://redirect`
- `scope` = `psn:mobile.v2.core psn:clientapp`
- Token endpoint: `https://ca.account.sony.com/api/authz/v3/oauth/token`
- Authorize endpoint: `https://ca.account.sony.com/api/authz/v3/oauth/authorize`
- Presence endpoint: `https://m.np.playstation.com/api/userProfile/v2/internal/users/basicPresences`

If any of these stop working, Sony has rotated something — update the constants and re-test.
