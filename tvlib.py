#!/usr/bin/env python3
"""
tvlib.py - Shared Samsung TV / HDMI-CEC / Wake-on-LAN helpers.

Used by tv_power_daemon.py, control_tv.py and boot_wol.py so the network,
CEC and sysfs logic exists in exactly one place.

Configuration is read from the environment (all optional):
  TV_IP       TV address                  (default 192.168.0.2)
  TV_MAC      TV MAC address              (default 40:16:3b:d0:4f:8b)
  CEC_DEVICE  HDMI-CEC adapter            (default /dev/cec0)
  TV_MODE     Native mode for kscreen     (default 3840x2160@60)
  TV_LOG_LEVEL  Logging level             (default INFO)
"""

from __future__ import annotations

import fcntl
import glob
import ipaddress
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
DEFAULT_TV_MODE = "3840x2160@60"
TV_WS_PORT = 8001

KNOWN_POWER_STATES = frozenset({"on", "standby", "to-on", "to-standby"})

log = logging.getLogger("tv")


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


@dataclass(frozen=True)
class Config:
    tv_ip: str = DEFAULT_TV_IP
    tv_mac: str = DEFAULT_TV_MAC
    cec_device: str = DEFAULT_CEC_DEVICE
    tv_mode: str = DEFAULT_TV_MODE

    @classmethod
    def from_env(cls) -> "Config":
        tv_ip = os.environ.get("TV_IP", DEFAULT_TV_IP).strip()
        try:
            ipaddress.ip_address(tv_ip)
        except ValueError as exc:
            raise ValueError(f"Invalid TV_IP {tv_ip!r}") from exc

        tv_mac = os.environ.get("TV_MAC", DEFAULT_TV_MAC).strip()
        normalize_mac(tv_mac)  # validate eagerly

        tv_mode = os.environ.get("TV_MODE", DEFAULT_TV_MODE).strip()
        if not _MODE_RE.match(tv_mode):
            raise ValueError(f"Invalid TV_MODE {tv_mode!r} (expected e.g. 3840x2160@60)")

        return cls(
            tv_ip=tv_ip,
            tv_mac=tv_mac,
            cec_device=os.environ.get("CEC_DEVICE", DEFAULT_CEC_DEVICE).strip(),
            tv_mode=tv_mode,
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
    ) -> None:
        self.device = device
        self.query_timeout = query_timeout
        self.action_timeout = action_timeout
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
                "phys-addr=1.0.0.0",
            ],
            self.action_timeout,
            cancel,
        )
        return result is not None and result.returncode == 0

    def standby(self, cancel: Optional[threading.Event] = None) -> bool:
        result = self._run(
            ["--playback", "--to", "0", "--standby"],
            self.action_timeout,
            cancel,
        )
        return result is not None and result.returncode == 0


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
