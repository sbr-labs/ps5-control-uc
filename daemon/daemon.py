"""PS5 Remote Play control daemon.

Maintains a Remote Play session with the PS5 and serves an HTTP REST API for
button commands. HA's `rest_command` integration calls this from scripts that
the UC Remote 3 fires as buttons.

REST endpoints
--------------
  POST /button     {"button": "<name>", "action": "tap|press|release"}
  POST /wakeup
  POST /standby
  POST /launch     {"title_id": "<PPSAxxxxx>"}     (best-effort)
  GET  /state      -> {"power": "on|off", "session": bool, "app": "<name>"}

Buttons
-------
  UP DOWN LEFT RIGHT  L1 R1 L2 R2  CROSS CIRCLE SQUARE TRIANGLE
  OPTIONS SHARE PS  L3 R3 TOUCHPAD
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from typing import Optional

from aiohttp import web

from pyremoteplay import RPDevice
from pyremoteplay.profile import Profiles

from psn_presence import PsnPresence

# ---------- Config from env ----------

PS5_HOST     = os.environ.get("PS5_HOST", "")
CREDS_PATH   = os.environ.get("PS5_CREDS", "/data/credentials.json")
LISTEN_HOST  = os.environ.get("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT  = int(os.environ.get("LISTEN_PORT", "8456"))
# Fallback artwork shown when PS5 is on but no app is detected (home screen).
# Set HOME_IMAGE_FILE to an SVG/PNG path mounted into the container, or
# HOME_IMAGE_URL to an external URL. Both optional — skipped if neither set.
HOME_IMAGE_FILE = os.environ.get("HOME_IMAGE_FILE", "")
HOME_IMAGE_URL_OVERRIDE = os.environ.get("HOME_IMAGE_URL", "")

if not PS5_HOST:
    raise SystemExit(
        "PS5_HOST environment variable is required. "
        "Set it to your PS5's local IP address (e.g. PS5_HOST=10.0.0.42)."
    )
# Set to 0 to disable idle teardown (session stays alive forever while PS5 awake)
SESSION_IDLE_TIMEOUT_S = int(os.environ.get("IDLE_TIMEOUT", "0"))
# Button tap delay (seconds between press + release). Default 0.1; we lower
# for snappier menu nav. Some apps reject very short taps — bump if needed.
BUTTON_TAP_DELAY = float(os.environ.get("BUTTON_TAP_DELAY", "0.03"))
# Buttons that get "rapid-tap amplification" when held: when the Remote 3
# fires repeated taps for one of these, the daemon fires its own additional
# taps every RAPID_TAP_S until the Remote stops. Bypasses both the Remote 3's
# ~100ms tap cadence AND the PS5's ~250ms auto-repeat warm-up.
RAPID_BUTTONS = set(
    (os.environ.get("RAPID_BUTTONS")
     or "UP,DOWN,LEFT,RIGHT,L1,R1,L2,R2").upper().split(",")
)
RAPID_TAP_S      = float(os.environ.get("RAPID_TAP_S",      "0.05"))   # 50ms = 20 taps/sec
RAPID_RELEASE_S  = float(os.environ.get("RAPID_RELEASE_S",  "0.18"))   # stop after this much idle
# Distinguishes "held" from "single click": rapid loop only kicks in once two
# taps arrive within RAPID_DETECT_S of each other (i.e. you're holding).
RAPID_DETECT_S   = float(os.environ.get("RAPID_DETECT_S",   "0.15"))
# Pre-warm session at startup if PS5 is awake, so first user press is instant.
PREWARM = os.environ.get("PREWARM", "1") == "1"
# Health check interval to keep session alive + detect PS5 going to rest.
KEEPALIVE_S = int(os.environ.get("KEEPALIVE", "30"))

# PSN presence — fills in the running-app metadata that Sony stripped from
# the PS5's DDP broadcast in firmware 13.x. When PSN_NPSSO_TOKEN is set
# (one-time paste from https://ca.account.sony.com/api/v1/ssocookie, signed
# in), the daemon queries Sony's PSN REST API every PSN_PRESENCE_POLL_S to
# get the currently-playing title + cover art. Tokens persisted under
# /data/psn_tokens.json so the user only pastes the npsso once.
PSN_NPSSO_TOKEN = os.environ.get("PSN_NPSSO_TOKEN", "").strip()
# Idle poll cadence (no recent button activity). 15s is well under Sony's
# rate limit and catches game switches within ~half a minute.
PSN_PRESENCE_POLL_S = int(os.environ.get("PSN_PRESENCE_POLL_S", "15"))
# Fast poll cadence when a button has been pressed in the last
# PSN_PRESENCE_ACTIVITY_WINDOW_S seconds — user is interacting, so we
# refresh more aggressively so cover art keeps up with menu nav.
PSN_PRESENCE_FAST_POLL_S = int(os.environ.get("PSN_PRESENCE_FAST_POLL_S", "5"))
PSN_PRESENCE_ACTIVITY_WINDOW_S = int(os.environ.get("PSN_PRESENCE_ACTIVITY_WINDOW_S", "60"))
PSN_TOKENS_PATH = os.environ.get("PSN_TOKENS_PATH", "/data/psn_tokens.json")

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("ps5ctrl")


# ---------- Profile loader ----------

def build_profiles_from_creds(creds_path: str) -> tuple[str, Profiles]:
    """Load credentials.json into a pyremoteplay Profiles dict.

    Accepts two formats:
    1. Native pyremoteplay (what pair.sh writes): {username: {"id": <id>,
       "hosts": {<mac>: {"type": "PS5", "data": {...}}}}}.
    2. Legacy ps5-mqtt: {<key>: {"accountId": ..., "registration":
       {"PS5-Mac": ..., ...}}}. Kept for users importing creds from an
       existing ps5-mqtt setup.
    """
    with open(creds_path) as f:
        data = json.load(f)
    if not data:
        raise ValueError(f"No credentials in {creds_path}")
    first_key, first_val = next(iter(data.items()))

    if isinstance(first_val, dict) and "id" in first_val and "hosts" in first_val:
        # Native pyremoteplay format
        return first_key, Profiles(data)

    if isinstance(first_val, dict) and "accountId" in first_val and "registration" in first_val:
        # Legacy ps5-mqtt format
        ps5 = first_val
        reg = ps5["registration"]
        host_data = {
            (k.split("-", 1)[1] if k.startswith("PS5-") else k): v
            for k, v in reg.items()
        }
        mac_upper = reg["PS5-Mac"].upper().replace(":", "")
        user_name = reg["PS5-Nickname"]
        profiles = Profiles({
            user_name: {
                "id":    ps5["accountId"],
                "hosts": {mac_upper: {"type": "PS5", "data": host_data}},
            }
        })
        return user_name, profiles

    raise ValueError(f"Unrecognised credentials format in {creds_path}")


# ---------- Session manager ----------

class PS5Controller:
    def __init__(self, host: str, user: str, profiles: Profiles) -> None:
        self.host = host
        self.user = user
        self.profiles = profiles
        self.device = RPDevice(host)
        self._lock = asyncio.Lock()
        self._last_activity = 0.0
        self._idle_task: Optional[asyncio.Task] = None
        # Rapid-tap state: button -> last_tap_monotonic; loop task per button
        self._rapid_last: dict[str, float] = {}
        self._rapid_tasks: dict[str, asyncio.Task] = {}
        # When > now, keepalive won't re-establish a torn-down session.
        # Used by /disconnect?pause=N so user can use PS5 settings.
        self._pause_until: float = 0.0
        # PSN presence cache — populated by psn_presence_loop() every
        # PSN_PRESENCE_POLL_S seconds. Empty dict when presence is
        # disabled or Sony reports nothing playing.
        self._psn_presence: dict[str, str] = {}

    def refresh_status(self) -> dict:
        return self.device.get_status() or {}

    @property
    def is_on(self) -> bool:
        return bool(self.device.is_on)

    @property
    def app_name(self) -> Optional[str]:
        # Prefer PSN presence when available — DDP returns empty for
        # third-party apps and most titles since firmware 13.x.
        psn = self._psn_presence.get("app_name")
        if psn:
            return psn
        s = self.device.status or {}
        return s.get("running-app-name") or None

    @property
    def app_id(self) -> Optional[str]:
        psn = self._psn_presence.get("app_id")
        if psn:
            return psn
        s = self.device.status or {}
        return s.get("running-app-titleid") or None

    @property
    def app_image(self) -> Optional[str]:
        # PSN cover art (from catalog endpoint) takes priority — works
        # even without an active Remote Play session.
        psn = self._psn_presence.get("image_url")
        if psn:
            return psn
        try:
            mi = self.device.media_info
            if mi and mi.cover_art:
                return mi.cover_art
        except Exception:
            pass
        return None

    async def refresh_media_info(self) -> None:
        """Look up PS Store cover art for the currently running app/game."""
        app = self.app_name
        title = self.app_id
        if not app or not title:
            return
        # Only refetch when the title changes
        if getattr(self, "_last_media_title", None) == title:
            return
        try:
            await self.device.async_get_ps_store_data(app, title, region="en/gb")
            self._last_media_title = title
            log.info("media_info: %s -> %s", app, self.app_image)
        except Exception as exc:
            log.debug("media_info fetch failed for %s: %s", title, exc)

    @property
    def has_session(self) -> bool:
        return self.device.connected

    async def ensure_session(self) -> bool:
        async with self._lock:
            self._last_activity = asyncio.get_event_loop().time()
            self.refresh_status()
            if not self.is_on:
                log.warning("PS5 not on — can't start session")
                return False
            if self.has_session and self.device.controller.running:
                return True

            try:
                self.device.disconnect()
            except Exception:
                pass

            log.info("Creating Remote Play session...")
            sess = self.device.create_session(
                user=self.user, profiles=self.profiles,
                resolution="360p", fps="low", quality="very_low", codec="h264",
            )
            if sess is None:
                log.error("Failed to create session")
                return False

            ok = await self.device.connect()
            if not ok:
                log.error("Failed to connect")
                return False

            ready = await self.device.async_wait_for_session(timeout=20)
            if not ready:
                log.error("Session not ready in 20s")
                self.device.disconnect()
                return False

            self.device.controller.start()
            await asyncio.sleep(0.5)
            log.info("Session ready")
            self._start_idle_watcher()
            return True

    def _start_idle_watcher(self) -> None:
        # Idle timeout 0 = never tear down (keep session alive while PS5 on).
        if SESSION_IDLE_TIMEOUT_S <= 0:
            return
        if self._idle_task is None or self._idle_task.done():
            self._idle_task = asyncio.create_task(self._idle_loop())

    async def _idle_loop(self) -> None:
        while self.has_session:
            await asyncio.sleep(15)
            idle = asyncio.get_event_loop().time() - self._last_activity
            if idle > SESSION_IDLE_TIMEOUT_S:
                log.info("Idle %ss — tearing down session", int(idle))
                try:
                    self.device.controller.stop()
                except Exception:
                    pass
                self.device.disconnect()
                return

    async def disconnect(self) -> None:
        if self.has_session:
            try:
                self.device.controller.stop()
            except Exception:
                pass
            self.device.disconnect()
            await asyncio.sleep(0.2)

    async def wakeup(self) -> bool:
        log.info("wakeup")
        # RPDevice.wakeup is synchronous; run in executor to avoid blocking loop.
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.device.wakeup, self.user, self.profiles)
        # Fire-and-forget warmup: cold-booting a PS5 from rest takes 25–45 s,
        # well past any reasonable HTTP timeout. Return immediately so the
        # caller (driver / activity) sees success as soon as the WoL packet
        # is sent. The 10-second /state poll picks up the actual "on" state
        # shortly after boot, and `ensure_session` here pre-warms the
        # Remote Play session so the first button after boot is instant.
        asyncio.create_task(self._post_wakeup_warmup())
        return True

    async def _post_wakeup_warmup(self) -> None:
        try:
            woke = await self.device.async_wait_for_wakeup(timeout=60)
            if woke:
                await self.ensure_session()
            else:
                log.warning("PS5 did not wake within 60s")
        except Exception:
            log.exception("post-wakeup warmup failed")

    async def standby(self) -> bool:
        log.info("standby")
        ok = await self.ensure_session()
        if not ok:
            return False
        try:
            # RPDevice.standby is async — await directly
            return bool(await self.device.standby(self.user, self.profiles))
        finally:
            await self.disconnect()

    async def button(self, name: str, action: str = "tap") -> bool:
        if not await self.ensure_session():
            return False
        name = name.upper()
        action = action.lower()
        try:
            self._last_activity = asyncio.get_event_loop().time()
            now = self._last_activity

            if action == "tap" and name in RAPID_BUTTONS:
                # Always fire one clean tap right now (instant response — no
                # delay penalty for single clicks).
                await self.device.controller.async_button(
                    name, "tap", delay=BUTTON_TAP_DELAY,
                )
                # If a previous tap landed close-by, treat as "held" and kick
                # off the rapid amplifier (or just refresh its keep-alive).
                last = self._rapid_last.get(name, 0.0)
                self._rapid_last[name] = now
                if (now - last) < RAPID_DETECT_S:
                    if name not in self._rapid_tasks or self._rapid_tasks[name].done():
                        self._rapid_tasks[name] = asyncio.create_task(self._rapid_loop(name))
                return True

            await self.device.controller.async_button(
                name, action, delay=BUTTON_TAP_DELAY,
            )
            return True
        except Exception as exc:
            log.warning("button %s/%s failed: %s", name, action, exc)
            return False

    async def _rapid_loop(self, name: str) -> None:
        """Once a hold is detected, fire taps at RAPID_TAP_S cadence to fill
        the gaps between the Remote 3's slower tap-when-held cadence. Stops
        when no Remote 3 tap arrives for RAPID_RELEASE_S."""
        try:
            while True:
                await asyncio.sleep(RAPID_TAP_S)
                last = self._rapid_last.get(name, 0.0)
                idle = asyncio.get_event_loop().time() - last
                if idle > RAPID_RELEASE_S:
                    return
                await self.device.controller.async_button(name, "tap", delay=BUTTON_TAP_DELAY)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("rapid loop %s: %s", name, exc)
        finally:
            self._rapid_tasks.pop(name, None)


# ---------- HTTP API ----------

VALID_BUTTONS = {
    "UP", "DOWN", "LEFT", "RIGHT",
    "L1", "R1", "L2", "R2",
    "CROSS", "CIRCLE", "SQUARE", "TRIANGLE",
    "OPTIONS", "SHARE", "PS",
    "L3", "R3", "TOUCHPAD",
}
VALID_ACTIONS = {"tap", "press", "release"}


async def _on_response_prepare(request, response):
    """Force TCP_NODELAY so button responses ship immediately, not after Nagle delay."""
    transport = request.transport
    if transport is not None:
        sock = transport.get_extra_info("socket")
        if sock is not None:
            try:
                import socket as _s
                sock.setsockopt(_s.IPPROTO_TCP, _s.TCP_NODELAY, 1)
            except Exception:
                pass


def make_app(controller: PS5Controller) -> web.Application:
    async def handle_button(req: web.Request) -> web.Response:
        try:
            data = await req.json()
        except Exception:
            data = {}
        btn = (data.get("button") or "").upper()
        action = (data.get("action") or "tap").lower()
        if btn not in VALID_BUTTONS:
            return web.json_response(
                {"ok": False, "error": f"invalid button '{btn}'", "valid": sorted(VALID_BUTTONS)},
                status=400,
            )
        if action not in VALID_ACTIONS:
            action = "tap"
        ok = await controller.button(btn, action)
        return web.json_response({"ok": ok, "button": btn, "action": action})

    async def handle_wakeup(req: web.Request) -> web.Response:
        ok = await controller.wakeup()
        return web.json_response({"ok": ok})

    async def handle_standby(req: web.Request) -> web.Response:
        ok = await controller.standby()
        return web.json_response({"ok": ok})

    async def handle_disconnect(req: web.Request) -> web.Response:
        """Tear down the Remote Play session. Use when you need to access
        PS5 settings (Screen and Video etc.) that lock out while RP active."""
        try:
            pause = int(req.query.get("pause", "120"))
        except ValueError:
            pause = 120
        # Set pause BEFORE disconnect so keepalive can't race in and reconnect.
        if pause > 0:
            controller._pause_until = asyncio.get_event_loop().time() + pause
        await controller.disconnect()
        return web.json_response({"ok": True, "pause": pause})

    async def handle_reconnect(req: web.Request) -> web.Response:
        controller._pause_until = 0
        ok = await controller.ensure_session()
        return web.json_response({"ok": ok})

    async def handle_state(req: web.Request) -> web.Response:
        controller.refresh_status()
        image = controller.app_image
        # Fall back to PS5 home-screen artwork when no app cover is known
        # but PS5 is on (idle on home screen, or in an app PSN doesn't track).
        if not image and controller.is_on:
            host_hdr = req.headers.get("Host", f"{LISTEN_HOST}:{LISTEN_PORT}")
            base = HOME_IMAGE_URL_OVERRIDE or f"http://{host_hdr}/home_image"
            # Cache-bust by file mtime so updates to the logo show up next poll
            try:
                ver = int(os.path.getmtime(HOME_IMAGE_FILE))
            except Exception:
                ver = 0
            image = f"{base}?v={ver}"
        return web.json_response({
            "power":     "on" if controller.is_on else "off",
            "session":   controller.has_session,
            "app":       controller.app_name or "",
            "app_id":    controller.app_id or "",
            "image_url": image or "",
        })

    async def handle_home_image(req: web.Request) -> web.Response:
        try:
            with open(HOME_IMAGE_FILE, "rb") as f:
                data = f.read()
            ct = "image/svg+xml" if HOME_IMAGE_FILE.endswith(".svg") else "image/png"
            return web.Response(body=data, content_type=ct,
                                # Short cache; mtime is also in URL so any
                                # update bypasses any client-side caching too.
                                headers={"Cache-Control": "public, max-age=60"})
        except FileNotFoundError:
            return web.Response(status=404, text="home image not configured")

    async def handle_health(req: web.Request) -> web.Response:
        return web.json_response({"ok": True, "host": controller.host})

    async def handle_index(req: web.Request) -> web.Response:
        """Small status + quick-start page.

        Useful for HA add-on users (HA renders this as the add-on's "Open Web UI"
        button target) and standalone Docker users who type the daemon URL into
        a browser to confirm it's alive.
        """
        host_header = req.headers.get("Host", f"{LISTEN_HOST}:{LISTEN_PORT}")
        ps5 = PS5_HOST or "&lt;not configured&gt;"
        # If this handler is responding, the daemon is alive — no need to probe.
        status_text = f"✅ Daemon running — listening on <code>{host_header}</code>, PS5 at <code>{ps5}</code>"
        html = (
            "<!DOCTYPE html>\n"
            "<html lang=\"en\"><head><meta charset=\"UTF-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">"
            "<title>PS5 Control daemon</title>"
            "<style>"
            "body{font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;"
            "max-width:720px;margin:2rem auto;padding:0 1rem;line-height:1.55;color:#1a1a1a}"
            "h1{font-size:1.7rem;margin-bottom:0.2rem}"
            "h2{font-size:1.15rem;margin-top:2rem}"
            ".status{padding:0.9rem 1rem;border-radius:8px;margin:1rem 0;background:#e8f6e8;border-left:4px solid #2e8b2e}"
            "code{background:#f1f1f1;padding:2px 6px;border-radius:4px;"
            "font-family:ui-monospace,Menlo,Consolas,monospace;font-size:0.92em}"
            ".dl{display:inline-block;background:#0066ff;color:#fff;padding:0.7rem 1.2rem;"
            "border-radius:6px;text-decoration:none;font-weight:600;margin:0.4rem 0}"
            ".dl:hover{background:#0050cc}"
            "ol{padding-left:1.4rem}li{margin:0.45rem 0}"
            "footer{margin-top:2.5rem;font-size:0.85em;color:#666;border-top:1px solid #eee;padding-top:1rem}"
            "</style></head><body>"
            "<h1>🎮 PS5 Control daemon</h1>"
            f"<div class=\"status\">{status_text}</div>"
            "<h2>Connect your Unfolded Circle Remote 3</h2>"
            "<ol>"
            "<li>Download the integration tarball: "
            "<a class=\"dl\" href=\"https://github.com/sbr-labs/ps5-control-uc/releases/latest/download/ps5-uc-integration.tar.gz\">"
            "📥 Download UC Remote 3 integration</a></li>"
            "<li>In the Remote 3 web configurator: <strong>Settings → Integrations → Upload custom integration</strong>, "
            "pick the tarball you just downloaded.</li>"
            "<li>Run setup for the new <strong>PS5 Control (SBR)</strong> integration. "
            f"When asked for the daemon host, type: <code>{host_header}</code></li>"
            "<li>Done — buttons on the Remote 3 should drive your PS5.</li>"
            "</ol>"
            "<h2>HTTP API (for HA scripts, voice intents, etc.)</h2>"
            "<ul>"
            "<li><code>POST /wakeup</code> — wake from rest</li>"
            "<li><code>POST /standby</code> — send to rest</li>"
            "<li><code>POST /button</code> with body <code>{\"button\":\"PS\",\"action\":\"tap\"}</code></li>"
            "<li><code>GET /state</code> — current power, app, session</li>"
            "<li><code>GET /health</code> — daemon liveness probe</li>"
            "</ul>"
            "<footer>Source: "
            "<a href=\"https://github.com/sbr-labs/ps5-control-uc\">sbr-labs/ps5-control-uc</a> · "
            "HA add-on: <a href=\"https://github.com/sbr-labs/ha-addons\">sbr-labs/ha-addons</a>"
            "</footer></body></html>"
        )
        return web.Response(text=html, content_type="text/html")

    app = web.Application()
    app.on_response_prepare.append(_on_response_prepare)
    app.router.add_get("/",            handle_index)
    app.router.add_post("/button",     handle_button)
    app.router.add_post("/wakeup",     handle_wakeup)
    app.router.add_post("/standby",    handle_standby)
    app.router.add_post("/disconnect", handle_disconnect)
    app.router.add_post("/reconnect",  handle_reconnect)
    app.router.add_get("/state",       handle_state)
    app.router.add_get("/health",      handle_health)
    app.router.add_get("/home_image",  handle_home_image)
    return app


# ---------- Main ----------

async def psn_presence_loop(controller: "PS5Controller", psn: "PsnPresence") -> None:
    """Background task that polls Sony's PSN basicPresences endpoint and
    caches the currently-playing title on the controller. Fills in the
    metadata that DDP no longer reports on firmware 13.x. Falls back
    gracefully if Sony returns nothing (controller's PSN cache emptied).

    Activity-aware: when a button has been pressed within
    PSN_PRESENCE_ACTIVITY_WINDOW_S, we poll on the FAST cadence so the
    cover art catches up quickly while the user is interacting. Otherwise
    we fall back to the slower idle cadence to spare Sony's API."""
    last_title: Optional[str] = None
    while True:
        try:
            result = await psn.fetch_presence()
            controller._psn_presence = result
            title = result.get("app_id") or None
            if title != last_title:
                if title:
                    log.info("psn_presence: %s (%s)", result.get("app_name"), title)
                else:
                    log.info("psn_presence: nothing playing")
                last_title = title
        except Exception:
            log.exception("psn_presence_loop error")
        now = asyncio.get_event_loop().time()
        recent_activity = (now - controller._last_activity) < PSN_PRESENCE_ACTIVITY_WINDOW_S
        await asyncio.sleep(PSN_PRESENCE_FAST_POLL_S if recent_activity else PSN_PRESENCE_POLL_S)


async def keepalive_loop(controller: "PS5Controller") -> None:
    """Background task that:
    - Refreshes DDP status (so /state is fresh and app sensor updates)
    - Fetches PS Store cover art when the running app changes
    - Tears down session if PS5 has entered rest mode
    NEVER auto-establishes a session — that's the user's job (button press
    or wake), so we don't interrupt a DualSense gameplay session.
    """
    while True:
        try:
            now = asyncio.get_event_loop().time()
            paused = now < controller._pause_until
            controller.refresh_status()
            if controller.is_on:
                if controller.has_session:
                    await controller.refresh_media_info()
            else:
                # PS5 went to rest — drop the session if any
                if controller.has_session and not paused:
                    log.info("keepalive: PS5 in rest mode — closing session")
                    await controller.disconnect()
        except Exception:
            log.exception("keepalive error")
        await asyncio.sleep(KEEPALIVE_S)


async def amain() -> None:
    log.info("PS5 control daemon starting")
    log.info(
        "PS5: %s   listen: %s:%s   prewarm=%s   idle=%ss   tap_delay=%.3fs",
        PS5_HOST, LISTEN_HOST, LISTEN_PORT, PREWARM,
        SESSION_IDLE_TIMEOUT_S, BUTTON_TAP_DELAY,
    )

    user, profiles = build_profiles_from_creds(CREDS_PATH)
    log.info("User profile: %s   accountId=%s", user, profiles[user].get("id"))

    controller = PS5Controller(PS5_HOST, user, profiles)
    app = make_app(controller)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, LISTEN_HOST, LISTEN_PORT)
    await site.start()
    log.info("HTTP API listening on %s:%s", LISTEN_HOST, LISTEN_PORT)

    # Pre-warm session ONLY if PS5 is in standby — that way we never grab
    # control while a DualSense session is in active gameplay use. If PS5 is
    # already on at startup, we leave it alone; first Remote 3 button press
    # establishes session on demand (~10 sec lag for that first press).
    if PREWARM:
        try:
            controller.refresh_status()
            if controller.is_on:
                log.info("PS5 already awake at startup — skipping pre-warm "
                         "(would interrupt active DualSense gameplay)")
            else:
                log.info("PS5 in standby — no pre-warm needed")
        except Exception:
            log.exception("prewarm check error")

    keepalive_task = asyncio.create_task(keepalive_loop(controller))

    # PSN presence — start ONLY if we either have a saved tokens file or
    # the user has supplied an npsso. If neither, leave it disabled; the
    # daemon still works (just without game cover art on the widget).
    psn: Optional["PsnPresence"] = None
    presence_task: Optional[asyncio.Task] = None
    if PSN_NPSSO_TOKEN or os.path.exists(PSN_TOKENS_PATH):
        psn = PsnPresence(token_path=PSN_TOKENS_PATH)
        if await psn.start(npsso=PSN_NPSSO_TOKEN or None):
            presence_task = asyncio.create_task(psn_presence_loop(controller, psn))
            log.info("psn_presence: enabled (poll every %ss)", PSN_PRESENCE_POLL_S)
        else:
            psn = None
            log.warning("psn_presence: disabled (start failed — see psn: lines above)")
    else:
        log.info("psn_presence: disabled (no PSN_NPSSO_TOKEN and no saved tokens at %s)", PSN_TOKENS_PATH)

    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    log.info("Shutting down")
    keepalive_task.cancel()
    if presence_task:
        presence_task.cancel()
    await controller.disconnect()
    await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass
