#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""
tv_backends.py - Vendor-specific TV power backends.

The daemon, CLI and boot helper speak one small interface:

    backend = create_backend(config)
    backend.power_state(cancel=..., allow_network_fallback=...)
    backend.wake(cancel=...)
    backend.nudge(cancel=...)
    backend.standby(cancel=..., undock=..., reason=..., should_abort=...)
    backend.active_source(cancel=...)
    backend.keepalive()
    backend.close()

HDMI-CEC is the common baseline: every backend uses it for power state and
standby when an adapter is available. Vendor APIs are layered on top:

    cec       Generic HDMI-CEC + Wake-on-LAN.
    samsung   CEC + Samsung Tizen WebSocket (legacy default).
    roku      CEC + Roku ECP on port 8060.
    androidtv CEC + Android TV Remote protocol v2 (optional dependency).
    firetv    CEC + ADB (Fire TV / Fire TV Edition televisions).
    lg        CEC + LG webOS WebSocket/SSAP (optional dependency).
    vizio     CEC + Vizio SmartCast HTTPS API (optional dependency).

Backends are selected with TV_BACKEND. Samsung is the default so an existing
installation keeps its current behaviour.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from typing import Callable, Optional

import tvlib

log = logging.getLogger("tv.backends")

SleepCheck = Optional[Callable[[], bool]]

# Android TV Remote protocol v2 ports.
ANDROIDTV_API_PORT = 6466
ANDROIDTV_PAIR_PORT = 6467

# Roku ECP power-mode values mapped onto the daemon's power-state vocabulary.
_ROKU_POWER_MODES = {
    "PowerOn": "on",
    "Standby": "standby",     # normal Roku TV standby
    "Ready": "standby",       # networked standby / Fast TV Start
    "DisplayOff": "standby",  # panel off, device awake
    "Headless": "offline",
    "PowerOff": "offline",
}


def _wait_or_cancel(cancel: Optional[threading.Event], seconds: float) -> bool:
    """Wait, returning True when the intent was cancelled."""
    if cancel is None:
        time.sleep(seconds)
        return False
    return cancel.wait(seconds)


def _read_secret(path: str) -> Optional[str]:
    """Read a one-line pairing secret, returning None if it is absent."""
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip() or None
    except OSError:
        return None


def _parse_android_power_status(output: str) -> Optional[str]:
    """Parse `dumpsys power` output from an Android TV / Fire TV device."""
    if "mWakefulness=Awake" in output:
        return "on"
    if "mWakefulness=Asleep" in output or "mWakefulness=Dozing" in output:
        return "standby"
    if "mScreenOn=true" in output or "Display Power: state=ON" in output:
        return "on"
    if "mScreenOn=false" in output or "Display Power: state=OFF" in output:
        return "standby"
    return None


class TvBackend:
    """Common CEC + Wake-on-LAN baseline for a TV power backend."""

    name = "base"
    description = "HDMI-CEC + Wake-on-LAN"
    supports_keepalive = False
    probe_port: Optional[int] = None

    def __init__(self, config: tvlib.Config) -> None:
        self.config = config
        self.cec: Optional[tvlib.CecController] = None
        if config.cec_device:
            self.cec = tvlib.CecController(
                config.cec_device, phys_addr=config.cec_phys_addr
            )
        self._warm = False

    @property
    def warm(self) -> bool:
        return self._warm

    # -- shared helpers --------------------------------------------------

    def _cec_wake(self, cancel: Optional[threading.Event]) -> bool:
        return self.cec.wake(cancel) if self.cec is not None else False

    def _confirm_cec_standby(
        self, cancel: Optional[threading.Event], attempts: int = 3
    ) -> bool:
        """Confirm an acknowledged CEC standby actually reaches standby.

        cec-ctl can return success while the TV ignores the message, so watch
        the CEC power state briefly. A missing reply is normal once the TV
        powers down; a persistent 'on' is not.
        """
        if self.cec is None:
            return False
        for attempt in range(1, attempts + 1):
            if _wait_or_cancel(cancel, 1.0):
                return False
            state = self.cec.power_state(cancel)
            if cancel is not None and cancel.is_set():
                return False
            if state in ("standby", "to-standby"):
                log.info("[*] TV confirmed '%s' via HDMI-CEC.", state)
                return True
            if state not in ("on", "to-on"):
                log.debug(
                    "No CEC power state after standby; assuming the TV is "
                    "powering down."
                )
                return True
            log.warning(
                "[!] TV still reports '%s' after standby (attempt %d/%d).",
                state,
                attempt,
                attempts,
            )
            if attempt < attempts and self.cec.standby(cancel):
                log.info("[*] Re-sent HDMI-CEC standby.")
        return False

    # -- backend interface -----------------------------------------------

    def power_state(
        self,
        cancel: Optional[threading.Event] = None,
        allow_network_fallback: bool = True,
    ) -> str:
        """Return 'on', 'standby', 'to-on', 'to-standby' or 'offline'."""
        if self.cec is not None:
            state = self.cec.power_state(cancel)
            if state in tvlib.KNOWN_POWER_STATES:
                return state
        if cancel is not None and cancel.is_set():
            return "offline"
        if (
            allow_network_fallback
            and self.probe_port
            and tvlib.is_tv_network_awake(
                self.config.tv_ip, timeout=0.4, port=self.probe_port
            )
        ):
            return "on"
        return "offline"

    def wake(self, cancel: Optional[threading.Event] = None) -> bool:
        """One full wake attempt: Wake-on-LAN plus a discrete CEC wake."""
        sent = False
        if self.config.tv_mac:
            sent = tvlib.send_wol(
                self.config.tv_mac,
                self.config.tv_ip,
                bursts=5,
                delay_between=0.05,
                cancel=cancel,
            )
        if cancel is not None and cancel.is_set():
            return False
        return self._cec_wake(cancel) or sent

    def nudge(self, cancel: Optional[threading.Event] = None) -> None:
        """Cheap repeat used between verification polls."""
        if self.config.tv_mac and (cancel is None or not cancel.is_set()):
            tvlib.send_wol(
                self.config.tv_mac,
                self.config.tv_ip,
                bursts=4,
                delay_between=0.05,
                cancel=cancel,
            )

    def standby(
        self,
        cancel: Optional[threading.Event] = None,
        undock: bool = False,
        reason: str = "",
        should_abort: SleepCheck = None,
    ) -> bool:
        """Put the TV into standby and report whether it was delivered."""
        if undock:
            log.warning(
                "%s backend cannot reach the TV after the CEC adapter was "
                "removed (undock).",
                self.name,
            )
            return False
        if self.cec is None:
            log.warning("%s backend has no CEC device configured.", self.name)
            return False
        log.info("[*] Sending HDMI-CEC standby to TV (%s)...", reason)
        if not self.cec.standby(cancel):
            return False
        log.info("[*] TV acknowledged HDMI-CEC standby (%s).", reason)
        return self._confirm_cec_standby(cancel)

    def active_source(self, cancel: Optional[threading.Event] = None) -> bool:
        """Re-assert this source (discrete IMAGE_VIEW_ON + ACTIVE_SOURCE)."""
        if self.cec is None:
            return True
        return self.cec.wake(cancel)

    def keepalive(self) -> bool:
        self._warm = True
        return True

    def close_keepalive(self) -> None:
        self._warm = False

    def close(self) -> None:
        self.close_keepalive()


class CecBackend(TvBackend):
    """Generic HDMI-CEC backend; no vendor network API."""

    name = "cec"
    description = "generic HDMI-CEC + Wake-on-LAN"


class SamsungBackend(TvBackend):
    """Samsung Tizen: CEC plus the WebSocket remote-control API."""

    name = "samsung"
    description = "Samsung Tizen WebSocket + HDMI-CEC"
    supports_keepalive = True
    probe_port = tvlib.TV_WS_PORT

    def __init__(self, config: tvlib.Config) -> None:
        super().__init__(config)
        self.tv_link = tvlib.TvLink(config.tv_ip)

    def standby(
        self,
        cancel: Optional[threading.Event] = None,
        undock: bool = False,
        reason: str = "",
        should_abort: SleepCheck = None,
    ) -> bool:
        """CEC standby first, with a proven Wi-Fi toggle fallback.

        Tizen exits cec-ctl with status 0 even for an unacknowledged frame, so
        a NACK is treated as failure: re-check the power state, then fall back
        to the WebSocket KEY_POWER toggle only if standby did not land. The
        toggle can undo a successful standby, hence the proof step first.
        """
        if not undock and self.cec is not None:
            log.info("[*] Sending HDMI-CEC standby to TV (%s)...", reason)
            if self.cec.standby(cancel):
                log.info("[*] TV acknowledged HDMI-CEC standby (%s).", reason)
                if self._confirm_cec_standby(cancel):
                    return True
                if cancel is not None and cancel.is_set():
                    return False
                log.warning(
                    "[!] TV still reports on after repeated CEC standby; "
                    "falling back to Wi-Fi WebSocket."
                )
            else:
                if cancel is not None and cancel.is_set():
                    return False
                log.warning(
                    "HDMI-CEC standby was not acknowledged; re-checking "
                    "power state before the Wi-Fi fallback."
                )
                state_after = self.power_state(cancel=cancel)
                if cancel is not None and cancel.is_set():
                    return False
                if state_after in ("standby", "to-standby"):
                    log.info(
                        "[*] TV reached %s despite the missing CEC "
                        "acknowledgment; skipping the WebSocket toggle.",
                        state_after,
                    )
                    return True
                log.warning("Falling back to Wi-Fi WebSocket for TV standby.")

        # The warm socket is sourced from Wi-Fi, so it outlives the dock
        # removal. Use it before any probe or reconnect work.
        if self.tv_link.warm and self.tv_link.send_key(
            "KEY_POWER", timeout=2.5, cancel=cancel
        ):
            log.info("[*] TV sleep command delivered on the warm channel (%s).", reason)
            return True
        if cancel is not None and cancel.is_set():
            return False

        attempts = 20 if undock else 4
        for attempt in range(1, attempts + 1):
            if cancel is not None and cancel.is_set():
                return False
            if should_abort is not None and should_abort():
                log.info(
                    "[*] Dock reconnected during Wi-Fi sleep retry; "
                    "aborting sleep."
                )
                return False
            # A short probe keeps failed attempts cheap while Wi-Fi route
            # failover settles; a reachable LAN answers in a few ms.
            if tvlib.is_tv_network_awake(self.config.tv_ip, timeout=0.25):
                if self.tv_link.send_key(
                    "KEY_POWER", timeout=2.5, cancel=cancel
                ):
                    log.info(
                        "[*] TV sleep command delivered over Wi-Fi on "
                        "attempt %d/%d (%s).",
                        attempt,
                        attempts,
                        reason,
                    )
                    return True
            if attempt < attempts and _wait_or_cancel(cancel, 0.25):
                return False

        log.info(
            "[*] Could not reach TV over Wi-Fi after %d attempts (%s); "
            "TV may already be off.",
            attempts,
            reason,
        )
        return False

    def keepalive(self) -> bool:
        ok = self.tv_link.keepalive()
        self._warm = ok
        return ok

    def close_keepalive(self) -> None:
        self.tv_link.close()
        self._warm = False

    def close(self) -> None:
        self.tv_link.close()
        super().close()


class RokuBackend(TvBackend):
    """Roku TV control through the External Control Protocol (ECP)."""

    name = "roku"
    description = "Roku ECP + HDMI-CEC"

    def __init__(self, config: tvlib.Config) -> None:
        super().__init__(config)
        self.ecp_port = config.ecp_port
        self.probe_port = config.ecp_port
        self.base_url = f"http://{config.tv_ip}:{config.ecp_port}"

    def _request(self, path: str, method: str = "GET", timeout: float = 1.0):
        data = b"" if method == "POST" else None
        request = urllib.request.Request(
            self.base_url + path, data=data, method=method
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()

    def _key(self, key: str, cancel: Optional[threading.Event] = None) -> bool:
        if cancel is not None and cancel.is_set():
            return False
        try:
            status, _body = self._request(f"/keypress/{key}", method="POST")
        except urllib.error.HTTPError as exc:
            log.warning("Roku ECP key %s rejected: HTTP %s", key, exc.code)
            return False
        except (urllib.error.URLError, OSError) as exc:
            log.debug("Roku ECP key %s failed: %s", key, exc)
            return False
        return 200 <= status < 300

    def _power_mode(self, timeout: float = 1.0) -> Optional[str]:
        try:
            _status, body = self._request("/query/device-info", timeout=timeout)
            root = ET.fromstring(body)
        except (urllib.error.URLError, OSError, ET.ParseError) as exc:
            log.debug("Roku ECP device-info failed: %s", exc)
            return None
        return root.findtext("power-mode") or ""

    def power_state(
        self,
        cancel: Optional[threading.Event] = None,
        allow_network_fallback: bool = True,
    ) -> str:
        if self.cec is not None:
            state = self.cec.power_state(cancel)
            if state in tvlib.KNOWN_POWER_STATES:
                return state
        if cancel is not None and cancel.is_set():
            return "offline"
        # Roku's device-info power-mode is authoritative, not a bare TCP
        # probe, so it is used even during the post-sleep wake guard.
        mode = self._power_mode()
        if mode is None:
            return "offline"
        return _ROKU_POWER_MODES.get(mode, "on")

    def wake(self, cancel: Optional[threading.Event] = None) -> bool:
        sent = False
        if self.config.tv_mac:
            sent = tvlib.send_wol(
                self.config.tv_mac,
                self.config.tv_ip,
                bursts=5,
                delay_between=0.05,
                cancel=cancel,
            )
        if cancel is not None and cancel.is_set():
            return False
        cec_ok = self._cec_wake(cancel)
        power_on = self._key("PowerOn", cancel)
        return sent or cec_ok or power_on

    def nudge(self, cancel: Optional[threading.Event] = None) -> None:
        if cancel is not None and cancel.is_set():
            return
        if self.config.tv_mac:
            tvlib.send_wol(
                self.config.tv_mac,
                self.config.tv_ip,
                bursts=4,
                delay_between=0.05,
                cancel=cancel,
            )
        # Fast TV Start keeps ECP reachable in standby; PowerOn is discrete.
        self._key("PowerOn", cancel)

    def standby(
        self,
        cancel: Optional[threading.Event] = None,
        undock: bool = False,
        reason: str = "",
        should_abort: SleepCheck = None,
    ) -> bool:
        if not undock and self.cec is not None:
            log.info("[*] Sending HDMI-CEC standby to TV (%s)...", reason)
            if self.cec.standby(cancel):
                log.info("[*] TV acknowledged HDMI-CEC standby (%s).", reason)
                if self._confirm_cec_standby(cancel):
                    return True
                if cancel is not None and cancel.is_set():
                    return False
                log.warning(
                    "CEC standby did not land; falling back to Roku ECP "
                    "PowerOff."
                )
            else:
                log.warning(
                    "HDMI-CEC standby was not acknowledged; falling back to "
                    "Roku ECP PowerOff."
                )

        attempts = 10 if undock else 3
        for attempt in range(1, attempts + 1):
            if cancel is not None and cancel.is_set():
                return False
            if should_abort is not None and should_abort():
                log.info(
                    "[*] Dock reconnected during Roku sleep retry; "
                    "aborting sleep."
                )
                return False
            if not self._key("PowerOff", cancel):
                log.warning(
                    "Roku ECP PowerOff failed. A Roku streaming player has no "
                    "standby; TV power control requires a Roku TV."
                )
                return False
            for _ in range(3):
                if _wait_or_cancel(cancel, 0.6):
                    return False
                state = self.power_state(cancel=cancel)
                if state in ("standby", "offline"):
                    log.info("[*] TV confirmed '%s' via Roku ECP.", state)
                    return True
            log.warning(
                "[!] Roku still reports on after PowerOff (attempt %d/%d).",
                attempt,
                attempts,
            )
        return False


class AndroidTvBackend(TvBackend):
    """Android TV Remote protocol v2 (Google TV / Android TV devices)."""

    name = "androidtv"
    description = "Android TV Remote v2 + HDMI-CEC"
    probe_port = ANDROIDTV_API_PORT

    def __init__(self, config: tvlib.Config) -> None:
        super().__init__(config)
        self._lock = threading.Lock()
        self._missing_logged = False

    def _remote_module(self):
        try:
            import androidtvremote2
        except ImportError:
            if not self._missing_logged:
                log.warning(
                    "androidtvremote2 is not installed; install it to use the "
                    "Android TV backend (pip install androidtvremote2)."
                )
                self._missing_logged = True
            return None
        if not (
            os.path.exists(self.config.androidtv_cert)
            and os.path.exists(self.config.androidtv_key)
        ):
            if not self._missing_logged:
                log.warning(
                    "Android TV client certificate/key not found. Run "
                    "pair_android_tv.py first (looked for %s and %s).",
                    self.config.androidtv_cert,
                    self.config.androidtv_key,
                )
                self._missing_logged = True
            return None
        return androidtvremote2

    def _run(self, action: str):
        module = self._remote_module()
        if module is None:
            return None

        async def _worker():
            remote = module.AndroidTVRemote(
                self.config.androidtv_client_name,
                self.config.androidtv_cert,
                self.config.androidtv_key,
                self.config.tv_ip,
                api_port=ANDROIDTV_API_PORT,
                pair_port=ANDROIDTV_PAIR_PORT,
            )
            try:
                await remote.async_connect()
                if action == "state":
                    return remote.is_on
                remote.send_key_command("POWER")
                await asyncio.sleep(0.25)
                return True
            finally:
                try:
                    remote.disconnect()
                except Exception:
                    pass

        with self._lock:
            try:
                return asyncio.run(_worker())
            except Exception as exc:
                log.warning("Android TV %s command failed: %s", action, exc)
                return None

    def _android_awake(self) -> bool:
        return tvlib.is_tv_network_awake(
            self.config.tv_ip, timeout=0.3, port=ANDROIDTV_API_PORT
        )

    def wake(self, cancel: Optional[threading.Event] = None) -> bool:
        sent = False
        if self.config.tv_mac:
            sent = tvlib.send_wol(
                self.config.tv_mac,
                self.config.tv_ip,
                bursts=5,
                delay_between=0.05,
                cancel=cancel,
            )
        if cancel is not None and cancel.is_set():
            return False
        return self._cec_wake(cancel) or sent

    def nudge(self, cancel: Optional[threading.Event] = None) -> None:
        if self.config.tv_mac and (cancel is None or not cancel.is_set()):
            tvlib.send_wol(
                self.config.tv_mac,
                self.config.tv_ip,
                bursts=4,
                delay_between=0.05,
                cancel=cancel,
            )

    def standby(
        self,
        cancel: Optional[threading.Event] = None,
        undock: bool = False,
        reason: str = "",
        should_abort: SleepCheck = None,
    ) -> bool:
        if not undock and self.cec is not None:
            log.info("[*] Sending HDMI-CEC standby to TV (%s)...", reason)
            if self.cec.standby(cancel):
                log.info("[*] TV acknowledged HDMI-CEC standby (%s).", reason)
                if self._confirm_cec_standby(cancel):
                    return True
                if cancel is not None and cancel.is_set():
                    return False
                log.warning(
                    "CEC standby did not land; falling back to the Android TV "
                    "remote POWER key."
                )
            else:
                log.warning(
                    "HDMI-CEC standby was not acknowledged; falling back to "
                    "the Android TV remote POWER key."
                )

        if self._run("power") is None:
            return False
        # Confirm against the device's own API port, not the TV's CEC state:
        # for a box, the TV can stay on while the box sleeps.
        for attempt in range(1, 4):
            if _wait_or_cancel(cancel, 0.8):
                return False
            if not self._android_awake():
                log.info("[*] TV confirmed standby via Android TV API port.")
                return True
            state = self._run("state")
            if state is False:
                log.info("[*] TV confirmed standby via Android TV remote.")
                return True
            # Re-send only when the device explicitly reports it is still on;
            # a failed state read must not toggle a device that is already off.
            if state is True and attempt < 3:
                self._run("power")
        log.warning("[!] Android TV still reports on after POWER.")
        return False


class WebOsBackend(TvBackend):
    """LG webOS: CEC plus the WebSocket/SSAP remote-control API."""

    name = "lg"
    description = "LG webOS WebSocket + HDMI-CEC"
    probe_port = 3000

    def __init__(self, config: tvlib.Config) -> None:
        super().__init__(config)
        self._lock = threading.Lock()
        self._missing_logged = False

    def _module(self):
        try:
            import aiowebostv
        except ImportError:
            if not self._missing_logged:
                log.warning(
                    "aiowebostv is not installed; install it to use the LG "
                    "webOS backend (pip install aiowebostv)."
                )
                self._missing_logged = True
            return None
        return aiowebostv

    def _client_key(self) -> Optional[str]:
        key = _read_secret(self.config.lg_client_key_file)
        if key is None and not self._missing_logged:
            log.warning(
                "LG webOS client key not found. Run pair_lg_tv.py first "
                "(looked for %s).",
                self.config.lg_client_key_file,
            )
            self._missing_logged = True
        return key

    def _run(self, action: str, should_abort: SleepCheck = None) -> Optional[bool]:
        module = self._module()
        key = self._client_key()
        if module is None or key is None:
            return None

        async def _worker():
            client = module.WebOsClient(
                self.config.tv_ip, key, connect_timeout=3.0
            )
            try:
                if not await client.connect():
                    return None
                if action == "state":
                    return bool(client.tv_state.is_on)
                if action == "off":
                    if should_abort is not None and should_abort():
                        return False
                    if not client.tv_state.is_on:
                        return True
                    await client.power_off()
                    return True
                return None
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass
                try:
                    await client.close_client_session()
                except Exception:
                    pass

        with self._lock:
            try:
                return asyncio.run(_worker())
            except Exception as exc:
                log.debug("LG webOS %s command failed: %s", action, exc)
                return None

    def power_state(
        self,
        cancel: Optional[threading.Event] = None,
        allow_network_fallback: bool = True,
    ) -> str:
        if self.cec is not None:
            state = self.cec.power_state(cancel)
            if state in tvlib.KNOWN_POWER_STATES:
                return state
        if cancel is not None and cancel.is_set():
            return "offline"
        state = self._run("state")
        if state is None:
            return "offline"
        return "on" if state else "standby"

    def standby(
        self,
        cancel: Optional[threading.Event] = None,
        undock: bool = False,
        reason: str = "",
        should_abort: SleepCheck = None,
    ) -> bool:
        if not undock and self.cec is not None:
            log.info("[*] Sending HDMI-CEC standby to TV (%s)...", reason)
            if self.cec.standby(cancel):
                log.info("[*] TV acknowledged HDMI-CEC standby (%s).", reason)
                if self._confirm_cec_standby(cancel):
                    return True
                if cancel is not None and cancel.is_set():
                    return False
                log.warning(
                    "CEC standby did not land; falling back to LG webOS."
                )
            else:
                log.warning(
                    "HDMI-CEC standby was not acknowledged; falling back to "
                    "LG webOS."
                )

        if cancel is not None and cancel.is_set():
            return False
        if should_abort is not None and should_abort():
            log.info("[*] Dock reconnected during LG sleep; aborting sleep.")
            return False
        if self._run("off", should_abort):
            log.info("[*] TV sleep command delivered via LG webOS (%s).", reason)
            return True
        log.info("[*] Could not reach LG webOS to deliver standby (%s).", reason)
        return False


class VizioBackend(TvBackend):
    """Vizio SmartCast: CEC plus the local HTTPS control API."""

    name = "vizio"
    description = "Vizio SmartCast + HDMI-CEC"

    def __init__(self, config: tvlib.Config) -> None:
        super().__init__(config)
        self.probe_port = config.vizio_port
        self._lock = threading.Lock()
        self._missing_logged = False

    def _module(self):
        try:
            import vizaio
        except ImportError:
            if not self._missing_logged:
                log.warning(
                    "vizaio is not installed; install it to use the Vizio "
                    "SmartCast backend (pip install vizaio)."
                )
                self._missing_logged = True
            return None
        return vizaio

    def _token(self) -> Optional[str]:
        token = _read_secret(self.config.vizio_token_file)
        if token is None and not self._missing_logged:
            log.warning(
                "Vizio SmartCast auth token not found. Run "
                "pair_vizio_tv.py first (looked for %s).",
                self.config.vizio_token_file,
            )
            self._missing_logged = True
        return token

    def _run(self, action: str, should_abort: SleepCheck = None) -> Optional[bool]:
        module = self._module()
        token = self._token()
        if module is None or token is None:
            return None

        host = f"{self.config.tv_ip}:{self.config.vizio_port}"

        async def _worker():
            device = module.Vizio(
                host,
                device_type=module.DeviceType.TV,
                auth_token=token,
                timeout=2.0,
                command_timeout=4.0,
            )
            try:
                if action == "state":
                    return bool(await device.get_power_state())
                if action == "off":
                    if should_abort is not None and should_abort():
                        return False
                    await device.power_off()
                    return True
                return None
            finally:
                try:
                    await device.aclose()
                except Exception:
                    pass

        with self._lock:
            try:
                return asyncio.run(_worker())
            except Exception as exc:
                log.debug("Vizio SmartCast %s command failed: %s", action, exc)
                return None

    def power_state(
        self,
        cancel: Optional[threading.Event] = None,
        allow_network_fallback: bool = True,
    ) -> str:
        if self.cec is not None:
            state = self.cec.power_state(cancel)
            if state in tvlib.KNOWN_POWER_STATES:
                return state
        if cancel is not None and cancel.is_set():
            return "offline"
        state = self._run("state")
        if state is None:
            return "offline"
        return "on" if state else "standby"

    def standby(
        self,
        cancel: Optional[threading.Event] = None,
        undock: bool = False,
        reason: str = "",
        should_abort: SleepCheck = None,
    ) -> bool:
        if not undock and self.cec is not None:
            log.info("[*] Sending HDMI-CEC standby to TV (%s)...", reason)
            if self.cec.standby(cancel):
                log.info("[*] TV acknowledged HDMI-CEC standby (%s).", reason)
                if self._confirm_cec_standby(cancel):
                    return True
                if cancel is not None and cancel.is_set():
                    return False
                log.warning(
                    "CEC standby did not land; falling back to Vizio "
                    "SmartCast."
                )
            else:
                log.warning(
                    "HDMI-CEC standby was not acknowledged; falling back to "
                    "Vizio SmartCast."
                )

        if cancel is not None and cancel.is_set():
            return False
        if should_abort is not None and should_abort():
            log.info("[*] Dock reconnected during Vizio sleep; aborting sleep.")
            return False
        if self._run("off", should_abort):
            log.info(
                "[*] TV sleep command delivered via Vizio SmartCast (%s).",
                reason,
            )
            return True
        log.info("[*] Could not reach Vizio SmartCast to deliver standby (%s).", reason)
        return False


class FireTvBackend(TvBackend):
    """Fire TV / Fire TV Edition control over ADB."""

    name = "firetv"
    description = "Fire TV ADB + HDMI-CEC"

    def __init__(self, config: tvlib.Config) -> None:
        super().__init__(config)
        self.serial = f"{config.tv_ip}:{config.adb_port}"
        self.probe_port = config.adb_port
        self._lock = threading.RLock()
        self._adb_ready = False
        self._missing_logged = False

    def _adb(
        self, args: list[str], timeout: float = 5.0
    ) -> Optional[subprocess.CompletedProcess]:
        cmd = [self.config.adb_bin, "-s", self.serial, *args]
        try:
            return subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            log.debug("ADB command timed out: %s", " ".join(args))
            return None
        except OSError as exc:
            if not self._missing_logged:
                log.warning("ADB is unavailable (%s); Fire TV control disabled.", exc)
                self._missing_logged = True
            return None

    def _ensure_adb_locked(self) -> bool:
        if self._adb_ready:
            result = self._adb(["get-state"], timeout=1.5)
            if (
                result is not None
                and result.returncode == 0
                and result.stdout.strip() == "device"
            ):
                return True
            self._adb_ready = False

        result = self._adb(["connect", self.serial], timeout=4.0)
        if result is None:
            return False
        if result.returncode != 0 and "connected" not in (
            result.stdout + result.stderr
        ).lower():
            log.debug(
                "ADB connect to %s failed: %s",
                self.serial,
                (result.stderr or result.stdout).strip(),
            )
            return False
        result = self._adb(["get-state"], timeout=2.0)
        self._adb_ready = (
            result is not None
            and result.returncode == 0
            and result.stdout.strip() == "device"
        )
        if not self._adb_ready:
            log.debug("ADB device %s is not ready.", self.serial)
        return self._adb_ready

    def _adb_power_state(self) -> Optional[str]:
        with self._lock:
            if not self._ensure_adb_locked():
                return None
            result = self._adb(["shell", "dumpsys", "power"], timeout=4.0)
            if result is None or result.returncode != 0:
                self._adb_ready = False
                return None
            state = _parse_android_power_status(result.stdout)
            if state is None:
                log.debug("Could not parse Fire TV dumpsys power output.")
            return state

    def _adb_key(self, keycode: str) -> bool:
        with self._lock:
            if not self._ensure_adb_locked():
                return False
            result = self._adb(
                ["shell", "input", "keyevent", keycode], timeout=4.0
            )
            return result is not None and result.returncode == 0

    def power_state(
        self,
        cancel: Optional[threading.Event] = None,
        allow_network_fallback: bool = True,
    ) -> str:
        if self.cec is not None:
            state = self.cec.power_state(cancel)
            if state in tvlib.KNOWN_POWER_STATES:
                return state
        # ADB reports the device's actual wakefulness; unlike a bare TCP
        # probe this is safe to trust during the post-sleep wake guard.
        state = self._adb_power_state()
        if state in tvlib.KNOWN_POWER_STATES:
            return state
        return "offline"

    def wake(self, cancel: Optional[threading.Event] = None) -> bool:
        sent = False
        if self.config.tv_mac:
            sent = tvlib.send_wol(
                self.config.tv_mac,
                self.config.tv_ip,
                bursts=5,
                delay_between=0.05,
                cancel=cancel,
            )
        if cancel is not None and cancel.is_set():
            return False
        cec_ok = self._cec_wake(cancel)
        adb_ok = self._adb_key("224")  # KEYCODE_WAKEUP
        return sent or cec_ok or adb_ok

    def nudge(self, cancel: Optional[threading.Event] = None) -> None:
        if cancel is not None and cancel.is_set():
            return
        if self.config.tv_mac:
            tvlib.send_wol(
                self.config.tv_mac,
                self.config.tv_ip,
                bursts=4,
                delay_between=0.05,
                cancel=cancel,
            )
        self._adb_key("224")

    def standby(
        self,
        cancel: Optional[threading.Event] = None,
        undock: bool = False,
        reason: str = "",
        should_abort: SleepCheck = None,
    ) -> bool:
        if not undock and self.cec is not None:
            log.info("[*] Sending HDMI-CEC standby to TV (%s)...", reason)
            if self.cec.standby(cancel):
                log.info("[*] TV acknowledged HDMI-CEC standby (%s).", reason)
                if self._confirm_cec_standby(cancel):
                    return True
                if cancel is not None and cancel.is_set():
                    return False
                log.warning(
                    "CEC standby did not land; falling back to Fire TV ADB."
                )
            else:
                log.warning(
                    "HDMI-CEC standby was not acknowledged; falling back to "
                    "Fire TV ADB."
                )

        for attempt in range(1, 4):
            if cancel is not None and cancel.is_set():
                return False
            if not self._adb_key("223"):  # KEYCODE_SLEEP
                log.warning("Fire TV ADB sleep key failed.")
                return False
            for _ in range(3):
                if _wait_or_cancel(cancel, 0.6):
                    return False
                state = self._adb_power_state()
                if state in ("standby", "offline"):
                    log.info("[*] TV confirmed '%s' via Fire TV ADB.", state)
                    return True
            log.warning(
                "[!] Fire TV still reports on after sleep key (attempt %d/3).",
                attempt,
            )
        return False


_BACKENDS = {
    "cec": CecBackend,
    "samsung": SamsungBackend,
    "roku": RokuBackend,
    "androidtv": AndroidTvBackend,
    "firetv": FireTvBackend,
    "lg": WebOsBackend,
    "vizio": VizioBackend,
}

_ALIASES = {
    "android": "androidtv",
    "fire": "firetv",
    "fire_tv": "firetv",
    "webos": "lg",
    "smartcast": "vizio",
}


def create_backend(config: tvlib.Config) -> TvBackend:
    """Build the backend selected by config.backend."""
    key = config.backend.strip().lower()
    key = _ALIASES.get(key, key)
    backend_class = _BACKENDS.get(key)
    if backend_class is None:
        raise ValueError(
            f"Unsupported TV_BACKEND {config.backend!r}; choose one of "
            f"{', '.join(sorted(_BACKENDS))}."
        )
    backend = backend_class(config)
    log.info("Using TV backend: %s (%s)", backend.name, backend.description)
    if key != "samsung" and not config.tv_mac:
        log.info(
            "Wake-on-LAN is disabled because TV_MAC is not set for this "
            "backend."
        )
    return backend
