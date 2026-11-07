#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""
tvlib.py - Shared TV / HDMI-CEC / Wake-on-LAN helpers.

Used by tv_power_daemon.py, control_tv.py, boot_wol.py and the vendor
backends so configuration, network, CEC and sysfs logic exists in exactly
one place.

Configuration is read from the environment (all optional):
  TV_IP         TV address                (default 192.168.0.2)
  TV_MAC        TV MAC address            (default 40:16:3b:d0:4f:8b)
  TV_BACKEND    Power backend             (samsung, cec, roku, androidtv, firetv, lg, vizio)
  CEC_DEVICE    HDMI-CEC adapter          (default /dev/cec0; empty disables CEC)
  CEC_PHYS_ADDR CEC physical address      (default 1.0.0.0)
  TV_MODE       Native mode for kscreen   (default 3840x2160@60)
  TV_LOG_LEVEL  Logging level             (default INFO)
"""

from __future__ import annotations

import base64
import fcntl
import glob
import ipaddress
import json
import logging
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Iterable, Optional

DEFAULT_TV_IP = "192.168.0.2"
DEFAULT_TV_MAC = "40:16:3b:d0:4f:8b"
DEFAULT_CEC_DEVICE = "/dev/cec0"
DEFAULT_CEC_PHYS_ADDR = "1.0.0.0"
DEFAULT_TV_MODE = "3840x2160@60"
DEFAULT_TV_BACKEND = "samsung"
DEFAULT_ECP_PORT = 8060
DEFAULT_ADB_PORT = 5555
DEFAULT_VIZIO_PORT = 7345
TV_BACKENDS = frozenset(
    {"cec", "samsung", "roku", "androidtv", "firetv", "lg", "vizio"}
)
TV_WS_PORT = 8001
_WS_NAME_B64 = base64.b64encode(b"SamsungTvRemote").decode("ascii")
_STATE_HOME = os.environ.get(
    "XDG_STATE_HOME", os.path.expanduser("~/.local/state")
)
DEFAULT_ANDROIDTV_CERT = os.path.join(
    _STATE_HOME, "tvpowersync", "androidtv_cert.pem"
)
DEFAULT_ANDROIDTV_KEY = os.path.join(
    _STATE_HOME, "tvpowersync", "androidtv_key.pem"
)
DEFAULT_LG_CLIENT_KEY = os.path.join(
    _STATE_HOME, "tvpowersync", "lg_client_key.txt"
)
DEFAULT_VIZIO_TOKEN = os.path.join(
    _STATE_HOME, "tvpowersync", "vizio_token.txt"
)
VIZIO_DEVICE_ID = "tvpowersync"

KNOWN_POWER_STATES = frozenset({"on", "standby", "to-on", "to-standby"})

log = logging.getLogger("tv")


def _read_version() -> str:
    """Read the release version shipped next to this module."""
    try:
        with open(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION"),
            encoding="utf-8",
        ) as handle:
            return handle.read().strip() or "unknown"
    except OSError:
        return "unknown"


__version__ = _read_version()


def setup_logging() -> None:
    """Configure stdout logging once, honouring TV_LOG_LEVEL."""
    level_name = os.environ.get("TV_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=level,
            format="[%(asctime)s] %(levelname)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            stream=sys.stdout,
        )
    root.setLevel(level)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def normalize_mac(mac: str) -> str:
    """Return a validated, colon-free MAC address string."""
    cleaned = mac.replace(":", "").replace("-", "").strip()
    if len(cleaned) != 12 or any(c not in "0123456789abcdefABCDEF" for c in cleaned):
        raise ValueError(f"Invalid MAC address: {mac!r}")
    return cleaned.upper()


def build_magic_packet(mac: str) -> bytes:
    """Build a 102-byte Wake-on-LAN magic packet."""
    cleaned = normalize_mac(mac)
    return b"\xff" * 6 + bytes.fromhex(cleaned) * 16


_MODE_RE = re.compile(r"^\d+x\d+(?:@\d+(?:\.\d+)?)?$")


def _env_port(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid {name} {raw!r}") from exc
    if not 1 <= value <= 65535:
        raise ValueError(f"Invalid {name} {raw!r} (expected 1-65535)")
    return value


_PHYS_ADDR_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


@dataclass(frozen=True)
class Config:
    tv_ip: str = DEFAULT_TV_IP
    tv_mac: str = DEFAULT_TV_MAC
    cec_device: str = DEFAULT_CEC_DEVICE
    tv_mode: str = DEFAULT_TV_MODE
    backend: str = DEFAULT_TV_BACKEND
    cec_phys_addr: str = DEFAULT_CEC_PHYS_ADDR
    ecp_port: int = DEFAULT_ECP_PORT
    adb_port: int = DEFAULT_ADB_PORT
    adb_bin: str = "adb"
    androidtv_cert: str = DEFAULT_ANDROIDTV_CERT
    androidtv_key: str = DEFAULT_ANDROIDTV_KEY
    androidtv_client_name: str = "TVPowerSync"
    lg_client_key_file: str = DEFAULT_LG_CLIENT_KEY
    vizio_token_file: str = DEFAULT_VIZIO_TOKEN
    vizio_port: int = DEFAULT_VIZIO_PORT

    @classmethod
    def from_env(cls) -> "Config":
        tv_ip = os.environ.get("TV_IP", DEFAULT_TV_IP).strip()
        try:
            ipaddress.ip_address(tv_ip)
        except ValueError as exc:
            raise ValueError(f"Invalid TV_IP {tv_ip!r}") from exc

        backend = os.environ.get("TV_BACKEND", DEFAULT_TV_BACKEND).strip().lower()
        if backend not in TV_BACKENDS:
            raise ValueError(
                f"Invalid TV_BACKEND {backend!r} (choose from "
                f"{', '.join(sorted(TV_BACKENDS))})"
            )

        # The default MAC belongs to the Samsung installation. For another
        # backend, an unset TV_MAC must disable WoL rather than wake the wrong
        # device on the LAN.
        raw_mac = os.environ.get("TV_MAC")
        if raw_mac is None and backend != "samsung":
            tv_mac = ""
        else:
            tv_mac = (raw_mac if raw_mac is not None else DEFAULT_TV_MAC).strip()
            normalize_mac(tv_mac)  # validate eagerly

        tv_mode = os.environ.get("TV_MODE", DEFAULT_TV_MODE).strip()
        if not _MODE_RE.match(tv_mode):
            raise ValueError(f"Invalid TV_MODE {tv_mode!r} (expected e.g. 3840x2160@60)")

        cec_phys_addr = os.environ.get(
            "CEC_PHYS_ADDR", DEFAULT_CEC_PHYS_ADDR
        ).strip()
        if not _PHYS_ADDR_RE.match(cec_phys_addr):
            raise ValueError(
                f"Invalid CEC_PHYS_ADDR {cec_phys_addr!r} "
                "(expected e.g. 1.0.0.0)"
            )

        return cls(
            tv_ip=tv_ip,
            tv_mac=tv_mac,
            cec_device=os.environ.get("CEC_DEVICE", DEFAULT_CEC_DEVICE).strip(),
            tv_mode=tv_mode,
            backend=backend,
            cec_phys_addr=cec_phys_addr,
            ecp_port=_env_port("TV_ECP_PORT", DEFAULT_ECP_PORT),
            adb_port=_env_port("TV_ADB_PORT", DEFAULT_ADB_PORT),
            adb_bin=os.environ.get("ADB_BIN", "adb").strip() or "adb",
            androidtv_cert=os.environ.get(
                "ANDROIDTV_CERT", DEFAULT_ANDROIDTV_CERT
            ).strip(),
            androidtv_key=os.environ.get(
                "ANDROIDTV_KEY", DEFAULT_ANDROIDTV_KEY
            ).strip(),
            androidtv_client_name=os.environ.get(
                "ANDROIDTV_CLIENT_NAME", "TVPowerSync"
            ).strip()
            or "TVPowerSync",
            lg_client_key_file=os.environ.get(
                "LG_CLIENT_KEY_FILE", DEFAULT_LG_CLIENT_KEY
            ).strip()
            or DEFAULT_LG_CLIENT_KEY,
            vizio_token_file=os.environ.get(
                "VIZIO_TOKEN_FILE", DEFAULT_VIZIO_TOKEN
            ).strip()
            or DEFAULT_VIZIO_TOKEN,
            vizio_port=_env_port("TV_VIZIO_PORT", DEFAULT_VIZIO_PORT),
        )


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

_iface_lock = threading.Lock()
_iface_cache: list[tuple[str, Optional[str]]] = []
_iface_cache_at = 0.0
# The interface set rarely changes outside a dock transition, and enumerating
# while the dock is re-enumerating can block on the kernel's rtnl lock. Keep a
# last-known-good set long enough that a wake never has to refresh mid-transition.
_IFACE_CACHE_TTL = 300.0

_SIOCGIFADDR = 0x8915
_SIOCGIFBRDADDR = 0x8919
_SIOCGIFFLAGS = 0x8913
_IFF_UP = 0x1


def _enumerate_ipv4_interfaces() -> list[tuple[str, Optional[str]]]:
    """Enumerate IPv4 addresses with ioctl; no subprocess, no rtnl stalls.

    A forked `ip` process can block in the kernel for several seconds while a
    USB-C dock re-enumerates. That delay lands directly in the WoL/wake path,
    so addresses are read straight from the kernel instead.
    """
    result: list[tuple[str, Optional[str]]] = []
    try:
        interfaces = socket.if_nameindex()
    except OSError as exc:
        log.debug("if_nameindex failed: %s", exc)
        return result

    for _index, name in interfaces:
        if name == "lo":
            continue
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                ifreq = struct.pack("256s", name.encode()[:15])
                flags = struct.unpack(
                    "H", fcntl.ioctl(sock.fileno(), _SIOCGIFFLAGS, ifreq)[16:18]
                )[0]
                if not flags & _IFF_UP:
                    continue
                address = socket.inet_ntoa(
                    fcntl.ioctl(sock.fileno(), _SIOCGIFADDR, ifreq)[20:24]
                )
                try:
                    broadcast = socket.inet_ntoa(
                        fcntl.ioctl(sock.fileno(), _SIOCGIFBRDADDR, ifreq)[20:24]
                    )
                    if broadcast == "0.0.0.0":  # point-to-point, no broadcast
                        broadcast = None
                except OSError:
                    broadcast = None
                result.append((address, broadcast))
        except OSError:
            continue
    return result


def _interface_ipv4(name: str) -> Optional[str]:
    """Return the current IPv4 address of one interface, or None."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            ifreq = struct.pack("256s", name.encode()[:15])
            flags = struct.unpack(
                "H", fcntl.ioctl(sock.fileno(), _SIOCGIFFLAGS, ifreq)[16:18]
            )[0]
            if not flags & _IFF_UP:
                return None
            return socket.inet_ntoa(
                fcntl.ioctl(sock.fileno(), _SIOCGIFADDR, ifreq)[20:24]
            )
    except OSError:
        return None


def wireless_ipv4_address() -> Optional[str]:
    """Return the IPv4 address of an up Wi-Fi interface, if any.

    The TV control channel is pinned to Wi-Fi because a socket bound to the
    dock's Ethernet address dies the instant the dock is unplugged -- exactly
    when the sleep key has to go out.
    """
    for _index, name in socket.if_nameindex():
        if name == "lo" or not os.path.isdir(f"/sys/class/net/{name}/wireless"):
            continue
        address = _interface_ipv4(name)
        if address:
            return address
    return None


def local_ipv4_interfaces(force: bool = False) -> list[tuple[str, Optional[str]]]:
    """Return (address, broadcast) pairs for non-loopback IPv4 interfaces.

    A 30s cache keeps a wake sequence from re-enumerating on every WoL burst;
    an empty result never replaces the last known good set, because a dock
    transition briefly removes every address from the table.
    """
    global _iface_cache, _iface_cache_at
    now = time.monotonic()
    with _iface_lock:
        if not force and _iface_cache and now - _iface_cache_at < _IFACE_CACHE_TTL:
            return list(_iface_cache)

    result = _enumerate_ipv4_interfaces()
    with _iface_lock:
        if result:
            _iface_cache = result
            _iface_cache_at = time.monotonic()
        elif _iface_cache:
            log.debug("Interface enumeration empty; using the last known good set")
            return list(_iface_cache)
        else:
            _iface_cache = []
            _iface_cache_at = time.monotonic()
    return list(result)


def send_wol(
    mac: str,
    tv_ip: Optional[str],
    bursts: int = 3,
    delay_between: float = 0.05,
    cancel: Optional[threading.Event] = None,
) -> bool:
    """Broadcast Wake-on-LAN magic packets across all active IPv4 NICs."""
    if not mac:
        log.debug("Wake-on-LAN skipped: no TV_MAC is configured")
        return False
    try:
        packet = build_magic_packet(mac)
    except ValueError as exc:
        log.error("%s", exc)
        return False

    interfaces = local_ipv4_interfaces()
    if not interfaces:
        log.warning("No non-loopback IPv4 interfaces found; sending unbound broadcast only")

    targets = {"255.255.255.255"}
    if tv_ip:
        targets.add(tv_ip)
    for _ip, brd in interfaces:
        if brd:
            targets.add(brd)

    for burst in range(max(1, bursts)):
        if cancel is not None and cancel.is_set():
            return False

        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                for target in targets:
                    for port in (9, 7):
                        try:
                            sock.sendto(packet, (target, port))
                        except OSError:
                            pass
        except OSError as exc:
            log.debug("Unbound WoL send failed: %s", exc)

        for ip_addr, brd in interfaces:
            dests = {"255.255.255.255"}
            if brd:
                dests.add(brd)
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                    sock.bind((ip_addr, 0))
                    for dest in dests:
                        for port in (9, 7):
                            try:
                                sock.sendto(packet, (dest, port))
                            except OSError:
                                pass
            except OSError as exc:
                log.debug("WoL send via %s failed: %s", ip_addr, exc)

        if burst < bursts - 1:
            if cancel is not None:
                if cancel.wait(delay_between):
                    return False
            else:
                time.sleep(delay_between)

    log.info("Wake-on-LAN sent to %s (%d burst%s)", mac, bursts, "" if bursts == 1 else "s")
    return True


def is_tv_network_awake(
    tv_ip: str, timeout: float = 0.2, port: int = TV_WS_PORT
) -> bool:
    """Non-blocking TCP probe of the TV's WebSocket port."""
    if not tv_ip:
        return False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            return sock.connect_ex((tv_ip, port)) == 0
    except OSError:
        return False


# ---------------------------------------------------------------------------
# HDMI-CEC
# ---------------------------------------------------------------------------


def parse_power_state(output: str) -> Optional[str]:
    """Extract the pwr-state value from cec-ctl output."""
    for line in output.splitlines():
        if "pwr-state:" in line:
            return line.split("pwr-state:", 1)[1].split("(")[0].strip()
    return None


def cec_transmit_acknowledged(
    result: Optional[subprocess.CompletedProcess],
) -> bool:
    """Return True only when cec-ctl transmitted and the peer acknowledged.

    cec-ctl exits 0 even when no device acknowledges the CEC frame (it prints
    "Tx, Not Acknowledged"), so its exit status alone does not prove that the
    TV received a command.
    """
    if result is None or result.returncode != 0:
        return False
    output = (result.stdout or "") + (result.stderr or "")
    return "Not Acknowledged" not in output


class CecController:
    """Serialized access to a single HDMI-CEC adapter.

    CEC adapters allow one controlling process at a time, and every cec-ctl
    invocation here is a separate process. The lock guarantees that two
    workers can never overlap them, and the cancellation check inside the
    lock prevents a superseded intent from transmitting a stale command.
    """

    def __init__(
        self,
        device: str = DEFAULT_CEC_DEVICE,
        query_timeout: float = 1.0,
        action_timeout: float = 4.0,
        phys_addr: str = DEFAULT_CEC_PHYS_ADDR,
    ) -> None:
        self.device = device
        self.query_timeout = query_timeout
        self.action_timeout = action_timeout
        self.phys_addr = phys_addr
        self._lock = threading.Lock()

    def _run(
        self,
        args: Iterable[str],
        timeout: float,
        cancel: Optional[threading.Event] = None,
    ) -> Optional[subprocess.CompletedProcess]:
        with self._lock:
            if cancel is not None and cancel.is_set():
                return None
            if not self.device or not os.path.exists(self.device):
                return None
            cmd = ["cec-ctl", "-d", self.device, *args]
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=timeout
                )
            except subprocess.TimeoutExpired:
                log.debug("cec-ctl timed out: %s", " ".join(args))
                return None
            except OSError as exc:
                log.warning("cec-ctl unavailable: %s", exc)
                return None
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip().splitlines()
                log.debug(
                    "cec-ctl exited %s for %s: %s",
                    result.returncode,
                    " ".join(args),
                    detail[-1] if detail else "no output",
                )
            return result

    def power_state(self, cancel: Optional[threading.Event] = None) -> Optional[str]:
        result = self._run(
            ["--playback", "--to", "0", "--give-device-power-status"],
            self.query_timeout,
            cancel,
        )
        if result is None:
            return None
        return parse_power_state((result.stdout or "") + (result.stderr or ""))

    def wake(self, cancel: Optional[threading.Event] = None) -> bool:
        """Discrete IMAGE_VIEW_ON + ACTIVE_SOURCE (never a power toggle)."""
        result = self._run(
            [
                "--playback",
                "--to",
                "0",
                "--image-view-on",
                "--active-source",
                f"phys-addr={self.phys_addr}",
            ],
            self.action_timeout,
            cancel,
        )
        return cec_transmit_acknowledged(result)

    def active_source(self, cancel: Optional[threading.Event] = None) -> bool:
        """Discrete ACTIVE_SOURCE, without another power-on request."""
        result = self._run(
            [
                "--playback",
                "--to",
                "0",
                "--active-source",
                f"phys-addr={self.phys_addr}",
            ],
            self.action_timeout,
            cancel,
        )
        return cec_transmit_acknowledged(result)

    def standby(self, cancel: Optional[threading.Event] = None) -> bool:
        result = self._run(
            ["--playback", "--to", "0", "--standby"],
            self.action_timeout,
            cancel,
        )
        if not cec_transmit_acknowledged(result):
            log.warning(
                "HDMI-CEC standby was not acknowledged; the TV did not "
                "confirm receipt."
            )
            return False
        return True


def get_tv_power_state(
    cec: Optional[CecController],
    tv_ip: str,
    use_cec: bool = True,
    cancel: Optional[threading.Event] = None,
    allow_network_fallback: bool = True,
) -> str:
    """Return 'on', 'standby', 'to-on', 'to-standby' or 'offline'.

    CEC is authoritative when it answers; the TCP probe is only a fallback.
    Set allow_network_fallback=False for a short window after commanding the
    TV off: Tizen can keep port 8001 open while shutting down, which would
    otherwise look like a healthy "on" state.
    """
    if use_cec and cec is not None:
        state = cec.power_state(cancel)
        if state in KNOWN_POWER_STATES:
            return state
    if cancel is not None and cancel.is_set():
        return "offline"
    if allow_network_fallback and is_tv_network_awake(tv_ip, timeout=0.4):
        return "on"
    return "offline"


# ---------------------------------------------------------------------------
# Samsung WebSocket remote
# ---------------------------------------------------------------------------

_ws_import_failed = False


def send_key(
    tv_ip: str,
    key: str = "KEY_POWER",
    timeout: float = 3.0,
    cancel: Optional[threading.Event] = None,
) -> bool:
    """Send a remote key over the Samsung WebSocket API.

    A fresh connection is used per key (the TV drops idle sessions), but it is
    always closed and every call must happen off the event-loop thread.
    """
    global _ws_import_failed
    if cancel is not None and cancel.is_set():
        return False
    try:
        from samsungtvws import SamsungTVWS
    except ImportError as exc:
        if not _ws_import_failed:
            log.error(
                "samsungtvws is not importable (%s); WebSocket control disabled", exc
            )
            _ws_import_failed = True
        return False

    tv = None
    try:
        tv = SamsungTVWS(tv_ip, timeout=timeout)
        tv.shortcuts().remote.send_key(key)
        return True
    except Exception as exc:  # library raises a variety of connection errors
        log.warning("WebSocket %s failed: %s", key, exc)
        return False
    finally:
        if tv is not None:
            try:
                tv.close()
            except Exception:
                pass


class TvLink:
    """A single, kept-warm WebSocket to the TV's remote-control endpoint.

    Tizen stops answering on port 8001 after a few idle minutes. When that
    happened, the undock path spent 6-8s in TCP retries before KEY_POWER could
    be delivered. Holding one session open and pinging it keeps the TV's
    network control service warm, and reusing the socket removes both the
    connection handshake and the library's 1s key-press delay from the sleep
    path.

    The session is opened from the Wi-Fi source address: a socket bound to the
    dock's Ethernet address is torn down the moment the dock disappears, which
    was why the first warm-channel attempt still took 8s.

    All calls are serialized: the keepalive thread and a power worker may race
    for the link, and the TV should only see one session from us.
    """

    def __init__(self, tv_ip: str, timeout: float = 1.5) -> None:
        self.tv_ip = tv_ip
        self.timeout = timeout
        self._lock = threading.Lock()
        self._ws = None
        self._warm = False

    @property
    def warm(self) -> bool:
        return self._warm

    # -- internals (call with the lock held) -----------------------------

    def _open_locked(self, timeout: float, bind_wifi: bool = True):
        """Open a session, optionally sourced from the Wi-Fi address.

        The TCP socket is created and bound here because the SamsungTVWS
        wrapper always lets the kernel choose the source address. It is handed
        to websocket-client, which performs the HTTP upgrade and framing.
        """
        import websocket

        sock = None
        source = wireless_ipv4_address() if bind_wifi else None
        if source:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            try:
                sock.bind((source, 0))
                sock.connect((self.tv_ip, TV_WS_PORT))
            except OSError:
                sock.close()
                raise
        url = (
            f"ws://{self.tv_ip}:{TV_WS_PORT}"
            f"/api/v2/channels/samsung.remote.control?name={_WS_NAME_B64}"
        )
        ws = websocket.WebSocket()
        try:
            ws.connect(url, timeout=timeout, socket=sock)
            ws.settimeout(timeout)
            while True:
                raw = ws.recv()
                if not raw:
                    raise ConnectionError("TV closed the socket during handshake")
                event = json.loads(raw).get("event")
                if event == "ms.channel.connect":
                    return ws
                if event == "ms.channel.unauthorized":
                    raise PermissionError("TV rejected the WebSocket session")
        except Exception:
            try:
                ws.shutdown()
            except Exception:
                pass
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            raise

    def _close_locked(self) -> None:
        ws, self._ws = self._ws, None
        self._warm = False
        if ws is None:
            return
        try:
            ws.close()
        except Exception:
            # websocket-client skips shutdown() when sending the close frame
            # fails, which would leave the socket in CLOSE-WAIT forever.
            pass
        finally:
            try:
                ws.shutdown()
            except Exception:
                pass

    def _ping_locked(self) -> bool:
        """Ping the open session so the TV sees traffic on the link.

        A missing pong only means the TV is busy; a closed or dead socket is
        what matters, and pinging it surfaces that immediately.
        """
        ws = self._ws
        if ws is None or not ws.connected:
            return False
        try:
            from websocket import WebSocketTimeoutException
        except ImportError:  # pragma: no cover - websocket-client ships with samsungtvws
            WebSocketTimeoutException = TimeoutError  # type: ignore[assignment,misc]
        try:
            ws.settimeout(0.25)
            ws.ping()
            try:
                ws.recv()  # pong, or an unrelated event; proves peer is alive
            except WebSocketTimeoutException:
                pass
            return True
        except Exception as exc:
            log.debug("TV control channel ping failed: %s", exc)
            return False
        finally:
            try:
                ws.settimeout(self.timeout)
            except Exception:
                pass

    # -- public API ------------------------------------------------------

    def keepalive(self) -> bool:
        """Keep the channel warm, opening (or re-opening) it if needed."""
        with self._lock:
            if self._ping_locked():
                self._warm = True
                return True
            self._close_locked()
            try:
                self._ws = self._open_locked(self.timeout)
            except Exception as exc:
                log.debug("TV control channel unavailable: %s", exc)
                return False
            log.debug("TV control channel established.")
            self._warm = True
            return True

    def send_key(
        self,
        key: str,
        timeout: float = 2.5,
        cancel: Optional[threading.Event] = None,
    ) -> bool:
        """Send a key over the warm link, reconnecting once on failure."""
        if cancel is not None and cancel.is_set():
            return False
        payload = json.dumps(
            {
                "method": "ms.remote.control",
                "params": {
                    "Cmd": "Click",
                    "DataOfCmd": key,
                    "Option": "false",
                    "TypeOfRemote": "SendRemoteKey",
                },
            }
        )
        with self._lock:
            for attempt in (1, 2):
                if cancel is not None and cancel.is_set():
                    return False
                try:
                    if self._ws is None:
                        # Prefer Wi-Fi so the session survives the undock;
                        # the retry uses the default route (Ethernet).
                        self._ws = self._open_locked(
                            timeout, bind_wifi=attempt == 1
                        )
                    self._ws.send(payload)
                    self._warm = True
                    return True
                except Exception as exc:
                    log.warning(
                        "Warm WebSocket %s failed (attempt %d/2): %s",
                        key,
                        attempt,
                        exc,
                    )
                    self._close_locked()
            return False

    def close(self) -> None:
        with self._lock:
            self._close_locked()


# ---------------------------------------------------------------------------
# DRM sysfs helpers
# ---------------------------------------------------------------------------


def panel_dpms_path() -> Optional[str]:
    """Locate the internal panel's DRM DPMS node (eDP, LVDS or DSI)."""
    for pattern in ("*-eDP-*/dpms", "*-LVDS-*/dpms", "*-DSI-*/dpms"):
        matches = sorted(glob.glob("/sys/class/drm/" + pattern))
        if matches:
            return matches[0]
    return None


def read_panel_dpms(path: Optional[str] = None) -> str:
    path = path or panel_dpms_path()
    if not path:
        return "Unknown"
    try:
        with open(path, "r") as handle:
            return handle.read().strip()
    except OSError:
        return "Unknown"


def external_display_connected() -> bool:
    """True when any DisplayPort or HDMI connector reports connected."""
    for path in sorted(
        glob.glob("/sys/class/drm/*-DP-*/status")
        + glob.glob("/sys/class/drm/*-HDMI-*/status")
    ):
        try:
            with open(path, "r") as handle:
                if handle.read().strip() == "connected":
                    return True
        except OSError:
            pass
    return False


def external_connector_name() -> Optional[str]:
    """Return the active external connector name (e.g. 'DP-2', 'HDMI-A-1')."""
    for path in sorted(
        glob.glob("/sys/class/drm/*-DP-*/status")
        + glob.glob("/sys/class/drm/*-HDMI-*/status")
    ):
        try:
            with open(path, "r") as handle:
                if handle.read().strip() != "connected":
                    continue
            name = os.path.basename(os.path.dirname(path)).split("-", 1)
            if len(name) == 2:
                return name[1]
        except OSError:
            pass
    return None


def external_edid_present() -> bool:
    """True when a connected DP/HDMI connector exposes a non-empty EDID.

    A TCP-only "on" is not enough to know the TV is usable: Tizen can keep
    port 8001 open while shutting down, and a TV on another input may not
    expose EDID to the GPU. This is the cheapest cross-check.
    """
    for status_path in sorted(
        glob.glob("/sys/class/drm/*-DP-*/status")
        + glob.glob("/sys/class/drm/*-HDMI-*/status")
    ):
        try:
            with open(status_path, "r") as handle:
                if handle.read().strip() != "connected":
                    continue
        except OSError:
            continue
        edid_path = os.path.join(os.path.dirname(status_path), "edid")
        try:
            # sysfs attributes report st_size 0 even when they have content,
            # so probe by reading one byte instead of using getsize().
            with open(edid_path, "rb") as handle:
                if handle.read(1):
                    return True
        except OSError:
            pass
    return False


# ---------------------------------------------------------------------------
# Wake retry backoff
# ---------------------------------------------------------------------------


class Backoff:
    """Exponential backoff for repeated failed wake attempts."""

    def __init__(self, initial: float = 60.0, maximum: float = 900.0) -> None:
        self._initial = initial
        self._maximum = maximum
        self._current = initial
        self._until = 0.0
        self._lock = threading.Lock()

    def ready(self) -> bool:
        with self._lock:
            return time.monotonic() >= self._until

    def remaining(self) -> float:
        with self._lock:
            return max(0.0, self._until - time.monotonic())

    def fail(self) -> float:
        """Record a failure and return the delay applied before the next try."""
        with self._lock:
            delay = self._current
            self._until = time.monotonic() + delay
            self._current = min(self._current * 2, self._maximum)
            return delay

    def reset(self) -> None:
        with self._lock:
            self._current = self._initial
            self._until = 0.0
