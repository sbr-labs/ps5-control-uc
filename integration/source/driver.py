"""Unfolded Circle integration for PS5 (via ps5-control daemon).

Runs on the Remote 3. Forwards every button press to a ps5-control daemon
running somewhere on the same LAN (default port 8456). The daemon maintains
a Remote Play session with the PS5 and translates HTTP calls into pyremoteplay
controller events, which is the only software-only way to drive PS5 navigation
since the PS5 doesn't accept BLE keyboards or expose a public control API.

Setup flow asks for the daemon's host:port.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from pathlib import Path
from typing import Any, Optional

import aiohttp
import ucapi
from ucapi import media_player as mp
from ucapi.api_definitions import (
    AbortDriverSetup,
    DriverSetupRequest,
    IntegrationSetupError,
    RequestUserInput,
    SetupAction,
    SetupComplete,
    SetupDriver,
    SetupError,
    UserDataResponse,
)

logging.basicConfig(
    level=os.environ.get("PS5_CTRL_LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
_LOG = logging.getLogger("ps5_ctrl")

CONFIG_FILENAME = "ps5_control_config.json"
HTTP_TIMEOUT_S = aiohttp.ClientTimeout(total=30)


# ---------- UC command -> daemon button name ----------

_CMD_KEY: dict[str, str] = {
    mp.Commands.CURSOR_UP:    "UP",
    mp.Commands.CURSOR_DOWN:  "DOWN",
    mp.Commands.CURSOR_LEFT:  "LEFT",
    mp.Commands.CURSOR_RIGHT: "RIGHT",
    mp.Commands.CURSOR_ENTER: "CROSS",      # OK = cross on PS5
    mp.Commands.BACK:         "CIRCLE",     # back = circle on PS5
    mp.Commands.HOME:         "PS",         # home = PS button
    mp.Commands.MENU:         "OPTIONS",
    mp.Commands.INFO:         "TOUCHPAD",
    mp.Commands.PLAY_PAUSE:   "CROSS",      # best-effort in media UIs
}

SIMPLE_COMMANDS = [
    "CROSS", "CIRCLE", "TRIANGLE", "SQUARE",
    "UP", "DOWN", "LEFT", "RIGHT",
    "L1", "R1", "L2", "R2",
    "L3", "R3",
    "PS", "OPTIONS", "SHARE", "TOUCHPAD",
    # Session control — drop the Remote Play stream so PS5 settings
    # (Screen and Video etc.) are accessible. RECONNECT to resume control.
    "SESSION_DISCONNECT",
    "SESSION_RECONNECT",
]

FEATURES = [
    mp.Features.ON_OFF,
    mp.Features.DPAD,
    mp.Features.HOME,
    mp.Features.MENU,
    mp.Features.INFO,
    mp.Features.PLAY_PAUSE,
]


# ---------- Config ----------

def _config_path(api: ucapi.IntegrationAPI) -> Path:
    return Path(api.config_dir_path) / CONFIG_FILENAME


def _load_config(api: ucapi.IntegrationAPI) -> dict[str, Any]:
    p = _config_path(api)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def _save_config(api: ucapi.IntegrationAPI, cfg: dict[str, Any]) -> None:
    p = _config_path(api)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, indent=2))


# ---------- Daemon HTTP client ----------

class DaemonClient:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.base = f"http://{host}:{port}"
        self._session: Optional[aiohttp.ClientSession] = None

    async def _ensure(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(family=socket.AF_INET, ssl=False)
            self._session = aiohttp.ClientSession(connector=connector, timeout=HTTP_TIMEOUT_S)
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            try:
                await self._session.close()
            except Exception as exc:
                _LOG.debug("session close: %s", exc)
        self._session = None

    async def _post(self, path: str, body: Optional[dict] = None) -> bool:
        try:
            session = await self._ensure()
            async with session.post(f"{self.base}{path}", json=body or {}) as r:
                ok = r.status == 200
                if not ok:
                    _LOG.warning("daemon %s -> HTTP %s", path, r.status)
                return ok
        except Exception as exc:
            _LOG.warning("daemon %s -> %s: %s", path, type(exc).__name__, exc)
            return False

    async def _get(self, path: str) -> Optional[dict]:
        try:
            session = await self._ensure()
            async with session.get(f"{self.base}{path}") as r:
                if r.status != 200:
                    return None
                return await r.json(content_type=None)
        except Exception as exc:
            _LOG.debug("daemon GET %s -> %s", path, exc)
            return None

    async def health(self) -> bool:
        return await self._get("/health") is not None

    async def state(self) -> dict:
        return await self._get("/state") or {}

    async def wakeup(self) -> bool:
        return await self._post("/wakeup")

    async def standby(self) -> bool:
        return await self._post("/standby")

    async def button(self, name: str, action: str = "tap") -> bool:
        return await self._post("/button", {"button": name.upper(), "action": action})

    async def disconnect_session(self, pause_s: int = 120) -> bool:
        try:
            session = await self._ensure()
            url = f"{self.base}/disconnect?pause={pause_s}"
            async with session.post(url) as r:
                return r.status == 200
        except Exception as exc:
            _LOG.warning("disconnect: %s", exc)
            return False

    async def reconnect_session(self) -> bool:
        return await self._post("/reconnect")


# ---------- PS5 device entity ----------

class PS5Device:
    def __init__(self, api: ucapi.IntegrationAPI, client: DaemonClient) -> None:
        self._uc = api
        self._client = client
        self._poll_task: Optional[asyncio.Task] = None
        self._awake = False
        self._app: Optional[str] = None
        self._image_url: str = ""

        self.media_player = mp.MediaPlayer(
            identifier="ps5",
            name="PlayStation 5",
            features=FEATURES,
            attributes={mp.Attributes.STATE: mp.States.UNKNOWN},
            options={mp.Options.SIMPLE_COMMANDS: SIMPLE_COMMANDS},
            device_class=mp.DeviceClasses.STREAMING_BOX,
            cmd_handler=self._on_cmd,
        )

    async def _on_cmd(self, entity, cmd_id, params=None) -> ucapi.StatusCodes:
        params = params or {}
        _LOG.debug("cmd: %s params=%s", cmd_id, params)
        try:
            if cmd_id == mp.Commands.ON:
                ok = await self._client.wakeup()
                if ok:
                    self._awake = True
                    self._push()
                return ucapi.StatusCodes.OK if ok else ucapi.StatusCodes.SERVER_ERROR

            if cmd_id == mp.Commands.OFF:
                ok = await self._client.standby()
                if ok:
                    self._awake = False
                    self._app = None
                    self._push()
                return ucapi.StatusCodes.OK if ok else ucapi.StatusCodes.SERVER_ERROR

            key = _CMD_KEY.get(cmd_id)
            if key:
                ok = await self._client.button(key)
                return ucapi.StatusCodes.OK if ok else ucapi.StatusCodes.SERVER_ERROR

            # simple_commands: cmd_id is the button name itself
            raw = (cmd_id or "").upper()
            if raw == "SESSION_DISCONNECT":
                ok = await self._client.disconnect_session(pause_s=120)
                return ucapi.StatusCodes.OK if ok else ucapi.StatusCodes.SERVER_ERROR
            if raw == "SESSION_RECONNECT":
                ok = await self._client.reconnect_session()
                return ucapi.StatusCodes.OK if ok else ucapi.StatusCodes.SERVER_ERROR
            if raw in {b.upper() for b in SIMPLE_COMMANDS}:
                ok = await self._client.button(raw)
                return ucapi.StatusCodes.OK if ok else ucapi.StatusCodes.SERVER_ERROR

        except Exception:
            _LOG.exception("cmd error")
            return ucapi.StatusCodes.SERVER_ERROR
        return ucapi.StatusCodes.NOT_IMPLEMENTED

    def _push(self) -> None:
        state = mp.States.ON if self._awake else mp.States.OFF
        self._uc.configured_entities.update_attributes(
            self.media_player.id,
            {
                mp.Attributes.STATE:           state,
                mp.Attributes.MEDIA_TITLE:     self._app or "",
                mp.Attributes.MEDIA_IMAGE_URL: self._image_url or "",
            },
        )

    async def start_polling(self, interval_s: float = 10.0) -> None:
        async def _loop():
            while True:
                try:
                    s = await self._client.state()
                    self._awake = s.get("power") == "on"
                    self._app = s.get("app") or None
                    self._image_url = s.get("image_url") or ""
                    self._push()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _LOG.exception("poll error")
                await asyncio.sleep(interval_s)
        self._poll_task = asyncio.create_task(_loop())

    def stop_polling(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            self._poll_task = None


# ---------- Setup ----------

async def _validate_daemon(host: str, port: int) -> bool:
    client = DaemonClient(host, port)
    try:
        return await client.health()
    finally:
        await client.close()


def _setup_handler(state: dict[str, Any]):
    api: ucapi.IntegrationAPI = state["api"]

    async def _finish(host: str, port: int) -> SetupAction:
        if not await _validate_daemon(host, port):
            _LOG.warning("daemon not reachable at %s:%s", host, port)
            return SetupError(IntegrationSetupError.CONNECTION_REFUSED)
        cfg = {"host": host, "port": port}
        _save_config(api, cfg)
        await _configure(state, host, port)
        return SetupComplete()

    def _ask() -> RequestUserInput:
        existing = _load_config(api)
        return RequestUserInput(
            title={"en": "PS5 Control daemon"},
            settings=[
                {
                    "id": "host",
                    "label": {"en": "Daemon host (IP)"},
                    "field": {"text": {"value": existing.get("host", "")}},
                },
                {
                    "id": "port",
                    "label": {"en": "Daemon port"},
                    "field": {"text": {"value": str(existing.get("port", 8456))}},
                },
            ],
        )

    async def handle(msg: SetupDriver) -> SetupAction:
        try:
            if isinstance(msg, AbortDriverSetup):
                return SetupError(IntegrationSetupError.OTHER)

            if isinstance(msg, DriverSetupRequest):
                data = msg.setup_data or {}
                host = (data.get("host") or "").strip()
                port_s = (data.get("port") or "").strip()
                if host:
                    return await _finish(host, int(port_s) if port_s else 8456)
                cfg = _load_config(api)
                if cfg.get("host"):
                    await _configure(state, cfg["host"], int(cfg.get("port", 8456)))
                    return SetupComplete()
                return _ask()

            if isinstance(msg, UserDataResponse):
                vals = msg.input_values or {}
                host = (vals.get("host") or "").strip()
                port_s = (vals.get("port") or "").strip() or "8456"
                if not host:
                    return _ask()
                try:
                    port = int(port_s)
                except ValueError:
                    return _ask()
                return await _finish(host, port)

            return SetupError(IntegrationSetupError.OTHER)
        except Exception:
            _LOG.exception("setup error")
            return SetupError(IntegrationSetupError.OTHER)

    return handle


async def _configure(state: dict[str, Any], host: str, port: int) -> None:
    api: ucapi.IntegrationAPI = state["api"]

    old: Optional[PS5Device] = state.get("ps5")
    if old:
        try:
            old.stop_polling()
        except Exception as exc:
            _LOG.debug("stop_polling: %s", exc)
        try:
            await old._client.close()
        except Exception as exc:
            _LOG.debug("client close: %s", exc)

    client = DaemonClient(host, port)
    ps5 = PS5Device(api, client)
    # Adding the same identifier twice can raise — be defensive.
    try:
        api.available_entities.add(ps5.media_player)
    except Exception as exc:
        _LOG.debug("available_entities.add: %s", exc)
    try:
        api.configured_entities.add(ps5.media_player)
    except Exception as exc:
        _LOG.debug("configured_entities.add: %s", exc)

    await ps5.start_polling()
    state["ps5"] = ps5
    _LOG.info("PS5 daemon configured: %s:%s", host, port)


async def main() -> None:
    loop = asyncio.get_event_loop()
    api = ucapi.IntegrationAPI(loop)
    state: dict[str, Any] = {"api": api, "ps5": None}

    cfg = _load_config(api)
    host = cfg.get("host") or os.environ.get("PS5_DAEMON_HOST", "")
    port = int(cfg.get("port") or os.environ.get("PS5_DAEMON_PORT", "8456"))
    if host:
        _LOG.info("bootstrap daemon: %s:%s", host, port)
        try:
            await _configure(state, host, port)
        except Exception:
            _LOG.exception("bootstrap failed (continuing)")

    @api.listens_to(ucapi.Events.CONNECT)
    async def _on_connect():
        await api.set_device_state(ucapi.DeviceStates.CONNECTED)

    await api.init("driver.json", _setup_handler(state))
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
