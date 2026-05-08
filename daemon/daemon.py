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

    def refresh_status(self) -> dict:
        return self.device.get_status() or {}

    @property
    def is_on(self) -> bool:
        return bool(self.device.is_on)

    @property
    def app_name(self) -> Optional[str]:
        s = self.device.status or {}
        return s.get("running-app-name") or None

    @property
    def app_id(self) -> Optional[str]:
        s = self.device.status or {}
        return s.get("running-app-titleid") or None

    @property
    def app_image(self) -> Optional[str]:
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
        # RPDevice.wakeup is synchronous; run in executor to avoid blocking loop
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.device.wakeup, self.user, self.profiles)
        return await self.device.async_wait_for_wakeup(timeout=60)

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

    app = web.Application()
    app.on_response_prepare.append(_on_response_prepare)
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

    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    log.info("Shutting down")
    keepalive_task.cancel()
    await controller.disconnect()
    await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass
