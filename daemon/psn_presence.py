"""PSN presence client — fills in the running-app metadata that Sony stripped
out of the PS5's DDP broadcast in firmware 13.x.

PS5 firmware 13+ no longer reports `running-app-name` / `running-app-titleid`
over the local DDP discovery protocol, so pyremoteplay's `RPDevice.app_name`
and `app_id` come back empty regardless of what's on screen. This module
queries Sony's PSN REST API instead — the same endpoint the official
PlayStation mobile app uses to show "currently playing" on a friend's
profile.

Auth model:
- User pastes their **NPSSO** cookie (a long-lived PSN session cookie they
  fetch from https://ca.account.sony.com/api/v1/ssocookie while signed in).
- This module exchanges npsso → authorization_code → access_token + refresh_token.
- access_token (~1 h) is auto-refreshed via refresh_token (~60 d rolling).
- Tokens persisted to disk; user never re-auths unless the daemon is offline
  for ~2 months straight (Sony invalidates idle refresh chains).

References (all verified against PSNAWP 3.0.3 source 2026-05-10):
- OAuth client = Sony's official PS App Android client (client_id below).
- Token endpoint = ca.account.sony.com/api/authz/v3/oauth/token.
- Presence endpoint = m.np.playstation.com/api/userProfile/v2/internal/users/basicPresences.

This is a hand-rolled minimal client (no psnawp_api dep) — Sony's REST API
is plain HTTP/JSON and we only need two endpoints. Less surface area, no
extra runtime deps, no upstream-breakage risk.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import aiohttp

log = logging.getLogger("ps5ctrl.psn")

# Sony's official "PS App" mobile client. Stable for years; PSNAWP and every
# other community library uses this same value.
CLIENT_ID = "09515159-7237-4370-9b40-3806e67c0891"
SCOPE = "psn:mobile.v2.core psn:clientapp"
REDIRECT_URI = "com.scee.psxandroid.scecompcall://redirect"
# Pre-encoded HTTP Basic for CLIENT_ID:CLIENT_SECRET. The decoded secret
# is the Sony-published value for this OAuth client (used by PSNAWP and
# every other community PSN library) — not a credential of ours.
# Decoded: 09515159-7237-4370-9b40-3806e67c0891:ucPjka5tntB2KqsP
AUTH_BASIC = "Basic MDk1MTUxNTktNzIzNy00MzcwLTliNDAtMzgwNmU2N2MwODkxOnVjUGprYTV0bnRCMktxc1A="

AUTHORIZE_URL = "https://ca.account.sony.com/api/authz/v3/oauth/authorize"
TOKEN_URL = "https://ca.account.sony.com/api/authz/v3/oauth/token"
ME_URL = "https://dms.api.playstation.com/api/v1/devices/accounts/me"
PRESENCE_URL = (
    "https://m.np.playstation.com/api/userProfile/v2/internal/users"
    "/basicPresences"
)
# Public catalog — no auth needed. Used to look up cover art when
# basicPresences returns conceptIconUrl="" (Sony doesn't always populate it).
CATALOG_URL_TEMPLATE = (
    "https://m.np.playstation.com/api/catalog/v2/titles/{title_id}/concepts"
)
# media.images[].type ranked best→worst for the Remote 3's media-player
# widget. The widget appears closer to square than 16:9 in practice, so
# MASTER (1024×1024 square key art) usually fills better than the
# widescreen banners which letterbox top+bottom. Override via the
# PSN_COVER_ART_PREFERENCE env var if your widget is configured wider.
_DEFAULT_COVER_ART_PREFERENCE = (
    "SIXTEEN_BY_NINE_BANNER",  # 3840×2160, widest aspect Sony offers
    "GAMEHUB_COVER_ART",       # 3840×2160 alternative 16:9
    "FOUR_BY_THREE_BANNER",
    "MASTER",
    "PORTRAIT_BANNER",
)
COVER_ART_PREFERENCE = tuple(
    a.strip()
    for a in (os.environ.get("PSN_COVER_ART_PREFERENCE")
              or ",".join(_DEFAULT_COVER_ART_PREFERENCE)).split(",")
    if a.strip()
)

# Refresh access token this many seconds before its actual expiry to avoid
# a stampede right at the boundary.
REFRESH_LEEWAY_S = 120

# Hard floor between fetch_presence() calls. Even if the daemon tries to
# fetch 100 times in 10 seconds (e.g. a flurry of button presses), Sony
# only sees one call every PSN_MIN_FETCH_INTERVAL_S. Caller gets the
# cached result during the cooldown.
PSN_MIN_FETCH_INTERVAL_S = float(os.environ.get("PSN_MIN_FETCH_INTERVAL_S", "5"))

# Backoff schedule (seconds) when Sony returns 429. Each successive 429
# advances one rung; first non-429 success resets back to 0.
BACKOFF_SCHEDULE_S = (30, 60, 120, 300, 300)


class PsnPresenceError(Exception):
    """PSN presence couldn't be fetched. Caller should fall back to DDP."""


class PsnPresence:
    """Manages PSN OAuth tokens + presence polling.

    Lifecycle:
        psn = PsnPresence(token_path="/data/psn_tokens.json", account_id="aBc...=")
        await psn.start(npsso="<user-pasted-cookie-or-None>")
        # Then call psn.fetch_presence() periodically. It returns
        # {"app_name": "...", "app_id": "...", "image_url": "..."} or {}.

    Rate-limit safety:
    - PSN_MIN_FETCH_INTERVAL_S enforces a hard floor between calls so a
      flurry of button presses can't hammer Sony.
    - On 429 (Too Many Requests) we back off exponentially up to 5 min
      and return the cached presence to the caller during the cooldown,
      so the widget stays populated instead of going blank.
    """

    def __init__(
        self,
        token_path: str,
        account_id: Optional[str] = None,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> None:
        """account_id is optional — if not given (or given in base64 form),
        the canonical decimal account ID is fetched from /me on first use.
        Sony's presence endpoint requires the decimal form."""
        self.token_path = token_path
        # Stored either as decimal (preferred) or base64 (will be replaced
        # by decimal on first /me call). None = look up from /me.
        self.account_id: Optional[str] = account_id
        self._tokens: dict[str, Any] = {}
        self._session = session  # injected for tests; otherwise per-call
        self._lock = asyncio.Lock()  # serialise refreshes
        # Rate-limit + backoff state.
        self._last_presence_at: float = 0.0
        self._cooldown_until: float = 0.0     # set when Sony 429s us
        self._backoff_step: int = 0            # index into BACKOFF_SCHEDULE_S
        self._cached_presence: dict[str, str] = {}  # last successful result

    # ---------- token lifecycle ----------

    async def start(self, npsso: Optional[str] = None) -> bool:
        """Load saved tokens, OR bootstrap from npsso if provided.

        Returns True if presence is ready to fetch, False if user needs to
        provide a fresh npsso (saved tokens missing or invalidated).
        """
        if self._load_tokens_from_disk():
            log.info("psn: loaded saved tokens from %s", self.token_path)
            return True
        if npsso:
            try:
                await self._bootstrap_from_npsso(npsso.strip())
                self._save_tokens()
                log.info("psn: bootstrapped tokens from npsso, persisted to %s", self.token_path)
                return True
            except Exception as exc:
                log.error("psn: npsso bootstrap failed: %s", exc)
                return False
        log.warning(
            "psn: no saved tokens at %s and no npsso provided — presence "
            "disabled. Get an npsso from https://ca.account.sony.com/api/v1/"
            "ssocookie (signed in) and paste it into the addon config to enable.",
            self.token_path,
        )
        return False

    # Bump this when the cover-art preference order changes, to force a
    # one-time refetch of cached covers under the new ordering.
    COVER_CACHE_SCHEMA_VERSION = 2

    def _load_tokens_from_disk(self) -> bool:
        if not os.path.exists(self.token_path):
            return False
        try:
            with open(self.token_path, "r") as f:
                self._tokens = json.load(f)
            if not self._tokens.get("refresh_token"):
                return False
            # Pick up cached decimal account_id (skips the /me lookup next time).
            cached = self._tokens.get("account_id")
            if cached and (not self.account_id or not self.account_id.isdigit()):
                self.account_id = str(cached)
            # Invalidate cover_cache if it was populated under an older
            # preference order — forces a refetch using the current ranking.
            saved_schema = self._tokens.get("cover_cache_schema_version", 0)
            if saved_schema < self.COVER_CACHE_SCHEMA_VERSION:
                if self._tokens.get("cover_cache"):
                    log.info("psn: cover_cache schema bump %s → %s, clearing cached covers",
                             saved_schema, self.COVER_CACHE_SCHEMA_VERSION)
                    self._tokens["cover_cache"] = {}
                self._tokens["cover_cache_schema_version"] = self.COVER_CACHE_SCHEMA_VERSION
                self._save_tokens()
            return True
        except Exception:
            log.exception("psn: failed reading %s — treating as missing", self.token_path)
            return False

    def _save_tokens(self) -> None:
        os.makedirs(os.path.dirname(self.token_path) or ".", exist_ok=True)
        # Persist to a temp file then rename — survives a crash mid-write.
        tmp = self.token_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self._tokens, f)
        os.replace(tmp, self.token_path)

    async def _bootstrap_from_npsso(self, npsso: str) -> None:
        """npsso → authorization_code → access_token + refresh_token."""
        async with self._http() as sess:
            # Step 1: npsso → authorization_code
            params = {
                "access_type": "offline",
                "client_id": CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
                "response_type": "code",
                "scope": SCOPE,
            }
            headers = {
                "Cookie": f"npsso={npsso}",
                "Content-Type": "application/x-www-form-urlencoded",
            }
            async with sess.get(AUTHORIZE_URL, params=params, headers=headers, allow_redirects=False) as resp:
                loc = resp.headers.get("Location") or ""
                code = parse_qs(urlparse(loc).query).get("code", [None])[0]
                if not code:
                    raise PsnPresenceError(
                        f"npsso → code exchange failed: status={resp.status} "
                        f"location={loc!r}. Most common cause: npsso expired "
                        "(re-fetch from https://ca.account.sony.com/api/v1/ssocookie)."
                    )
            # Step 2: code → tokens
            await self._exchange_code(sess, code)

    async def _exchange_code(self, sess: aiohttp.ClientSession, code: str) -> None:
        data = {
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPE,
            "token_format": "jwt",
        }
        headers = {
            "Authorization": AUTH_BASIC,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        async with sess.post(TOKEN_URL, data=data, headers=headers) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise PsnPresenceError(f"token exchange failed: {resp.status} {body[:200]}")
            payload = await resp.json()
        self._tokens = {
            "access_token": payload["access_token"],
            "refresh_token": payload["refresh_token"],
            "access_expires_at": time.time() + int(payload.get("expires_in", 3600)),
            "refresh_expires_at": time.time() + int(payload.get("refresh_token_expires_in", 60 * 24 * 3600)),
        }

    async def _refresh_access(self) -> None:
        """Use refresh_token to get a new access_token. Rolls refresh_token."""
        rt = self._tokens.get("refresh_token")
        if not rt:
            raise PsnPresenceError("no refresh_token — needs fresh npsso")
        data = {
            "refresh_token": rt,
            "grant_type": "refresh_token",
            "scope": SCOPE,
            "token_format": "jwt",
        }
        headers = {
            "Authorization": AUTH_BASIC,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        async with self._http() as sess:
            async with sess.post(TOKEN_URL, data=data, headers=headers) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise PsnPresenceError(
                        f"refresh failed: {resp.status} {body[:200]} — "
                        "refresh_token likely expired, needs fresh npsso."
                    )
                payload = await resp.json()
        self._tokens["access_token"] = payload["access_token"]
        self._tokens["access_expires_at"] = time.time() + int(payload.get("expires_in", 3600))
        # Sony rolls the refresh_token on each refresh — replace ours.
        if "refresh_token" in payload:
            self._tokens["refresh_token"] = payload["refresh_token"]
            self._tokens["refresh_expires_at"] = time.time() + int(
                payload.get("refresh_token_expires_in", 60 * 24 * 3600)
            )
        self._save_tokens()
        log.debug("psn: access token refreshed, expires in %ss", int(payload.get("expires_in", 3600)))

    async def _ensure_access_token(self) -> str:
        async with self._lock:
            exp = self._tokens.get("access_expires_at", 0)
            if not self._tokens.get("access_token") or exp - REFRESH_LEEWAY_S < time.time():
                await self._refresh_access()
            return self._tokens["access_token"]

    async def _ensure_account_id(self, token: str) -> str:
        """Look up the canonical decimal account ID via /me if we don't
        have it, or if the stored value isn't all-digits (i.e. is base64)."""
        if self.account_id and self.account_id.isdigit():
            return self.account_id
        headers = {"Authorization": f"Bearer {token}"}
        async with self._http() as sess:
            async with sess.get(ME_URL, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise PsnPresenceError(f"/me lookup failed: {resp.status} {body[:200]}")
                data = await resp.json()
        decimal = str(data.get("accountId") or "")
        if not decimal.isdigit():
            raise PsnPresenceError(f"/me returned no usable accountId: {data!r}")
        self.account_id = decimal
        # Persist alongside tokens so we don't re-look-it-up next start.
        self._tokens["account_id"] = decimal
        self._save_tokens()
        log.info("psn: resolved decimal account_id from /me")
        return decimal

    # ---------- presence query ----------

    async def fetch_presence(self) -> dict[str, str]:
        """Returns {app_name, app_id, image_url} for the currently-playing
        title on this account, or {} if not playing / not online / failed.

        Never raises — caller should treat {} as "fall back to DDP".

        Rate-limit safety: enforces PSN_MIN_FETCH_INTERVAL_S between actual
        Sony calls. During a 429 backoff window, returns the last cached
        presence so the widget stays populated.
        """
        now = time.time()
        # Honour the active backoff window (set on 429).
        if now < self._cooldown_until:
            return dict(self._cached_presence)
        # Honour the per-call min-interval.
        if (now - self._last_presence_at) < PSN_MIN_FETCH_INTERVAL_S:
            return dict(self._cached_presence)
        self._last_presence_at = now

        try:
            token = await self._ensure_access_token()
            account_id = await self._ensure_account_id(token)
        except Exception as exc:
            log.warning("psn: setup failed: %s", exc)
            return dict(self._cached_presence)
        params = {
            "type": "primary",
            "accountIds": account_id,
            "platforms": "PS4,PS5,MOBILE_APP,PSPC",
            "withOwnGameTitleInfo": "true",
        }
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with self._http() as sess:
                async with sess.get(PRESENCE_URL, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    status = resp.status
                    if status == 429:
                        # Sony rate-limited us. Set an exponential backoff
                        # and return the cached presence so the widget
                        # doesn't go blank during the cooldown.
                        wait_s = BACKOFF_SCHEDULE_S[min(self._backoff_step, len(BACKOFF_SCHEDULE_S) - 1)]
                        self._cooldown_until = time.time() + wait_s
                        self._backoff_step = min(self._backoff_step + 1, len(BACKOFF_SCHEDULE_S) - 1)
                        log.warning(
                            "psn: rate-limited by Sony — backing off %ss before next fetch "
                            "(serving cached presence in the meantime)", wait_s)
                        return dict(self._cached_presence)
                    if status != 200:
                        body = await resp.text()
                        log.warning("psn: presence fetch %s: %s", status, body[:200])
                        return dict(self._cached_presence)
                    # Successful call — reset backoff progression.
                    self._backoff_step = 0
                    data = await resp.json()
        except Exception as exc:
            log.warning("psn: presence request failed: %s", exc)
            return dict(self._cached_presence)
        result = _parse_presence(data, self.account_id)
        # Sony's basicPresences sometimes returns image_url=""; fall back to
        # the catalog endpoint when we have a title_id but no image. Cached
        # per title so it's only one extra HTTP call when the title changes.
        if result.get("app_id") and not result.get("image_url"):
            cover = await self._fetch_cover_art(result["app_id"])
            if cover:
                result["image_url"] = cover
        # Cache the latest successful result so it can be served during
        # backoff windows / inter-call cooldowns.
        self._cached_presence = dict(result)
        return result

    async def _fetch_cover_art(self, title_id: str) -> str:
        """Look up cover art for a PSN title via the catalog endpoint.
        No auth required. Returns "" if not found / network error."""
        # Cache hit?
        cache = self._tokens.setdefault("cover_cache", {})
        if title_id in cache:
            return str(cache[title_id])
        url = CATALOG_URL_TEMPLATE.format(title_id=title_id)
        params = {"age": 99, "country": "GB", "language": "en-GB"}
        try:
            async with self._http() as sess:
                async with sess.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return ""
                    payload = await resp.json()
        except Exception as exc:
            log.debug("psn: catalog lookup failed for %s: %s", title_id, exc)
            return ""
        item = payload[0] if isinstance(payload, list) and payload else payload
        images = (item or {}).get("media", {}).get("images", []) or []
        # Pick the first image whose type is in our preference list.
        for preferred in COVER_ART_PREFERENCE:
            for img in images:
                if img.get("type") == preferred and img.get("url"):
                    cover = _hint_size(str(img["url"]))
                    cache[title_id] = cover
                    self._save_tokens()  # persist cache for next run
                    log.info("psn: catalog cover %s → %s", preferred, cover)
                    return cover
        # Fall back to ANY image so we get something rather than nothing.
        for img in images:
            if img.get("url"):
                cover = _hint_size(str(img["url"]))
                cache[title_id] = cover
                self._save_tokens()
                return cover
        return ""

    # ---------- helpers ----------

    def _http(self) -> aiohttp.ClientSession:
        """Per-call session unless one was injected (for tests)."""
        if self._session is not None:
            # Async context manager wrapper that yields the injected session
            # without closing it. Trivial implementation:
            return _NoCloseSession(self._session)
        return aiohttp.ClientSession()


def _hint_size(url: str) -> str:
    """Append Sony's CDN size hint so we hand the Remote 3 a 1920×1080
    image instead of the raw 4K source. The Remote 3 firmware seems to
    render the 4K versions at a smaller fixed size — asking for 1080p
    explicitly tends to produce a sharper, larger-looking widget."""
    if "?" in url or "image.api.playstation.com" not in url:
        return url
    return f"{url}?w=1920"


class _NoCloseSession:
    """`async with` wrapper around an existing ClientSession that doesn't close it."""
    def __init__(self, sess: aiohttp.ClientSession) -> None:
        self._sess = sess
    async def __aenter__(self) -> aiohttp.ClientSession:
        return self._sess
    async def __aexit__(self, *exc: Any) -> None:
        return None


def _parse_presence(data: dict[str, Any], account_id: str) -> dict[str, str]:
    """Pull app_name / app_id / image_url out of the presence payload.

    Sony's response shape (verified against psnawp_api docstring + PS app):
      {"basicPresences":[{"accountId":"...","primaryPlatformInfo":{...},
                          "gameTitleInfoList":[{"npTitleId":"...","titleName":"...",
                                                "format":"PS5","launchPlatform":"PS5",
                                                "conceptIconUrl":"https://..."}]}]}
    """
    presences = data.get("basicPresences") or []
    if not presences:
        return {}
    me = next((p for p in presences if str(p.get("accountId") or "") == str(account_id)), presences[0])
    titles = me.get("gameTitleInfoList") or []
    if not titles:
        return {}
    t = titles[0]
    return {
        "app_name": t.get("titleName") or "",
        "app_id": t.get("npTitleId") or "",
        "image_url": t.get("conceptIconUrl") or "",
    }
