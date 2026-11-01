#!/usr/bin/env python3
"""
tv_power_daemon.py - Synchronize Samsung TV power with laptop display state.

Event sources:
  1. Kernel Netlink KObject UEvent socket (dock / display hotplug)
  2. D-Bus systemd login1 PropertiesChanged (hardware lid events)
  3. D-Bus KDE ScreenSaver ActiveChanged (screen lock/unlock)
  4. D-Bus systemd login1 PrepareForSleep (system suspend/resume)
  5. Adaptive hardware DPMS tracking (250ms while locked, 2s while working)
  6. Socket-based watchdog, run in a worker thread (no forks while streaming)

Power intent model:
  Every ON/OFF request creates a new Intent with its own cancellation event.
  A new intent cancels the previous one, and workers check cancellation before
  each side effect. This is what prevents a slow sleep retry from delivering
  KEY_POWER after a wake has already started.

Actions:
  - Screen off: lock the session, then discrete HDMI-CEC standby when docked,
    falling back to a WebSocket KEY_POWER only if CEC is unavailable.
  - Screen on: WoL + discrete CEC IMAGE_VIEW_ON/ACTIVE_SOURCE, then re-assert
    the native video mode once the TV confirms it is awake.
"""

import glob
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time

import dbus
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

import tvlib

log = logging.getLogger("tv.daemon")

NETLINK_KOBJECT_UEVENT = getattr(socket, "NETLINK_KOBJECT_UEVENT", 15)
DISPLAY_DEBOUNCE_MS = 400
FALLBACK_POLL_SECONDS = 2.0
SETTLE_SECONDS = 3.0


class Intent:
    """One power request. Superseding an intent sets its cancel event."""

    __slots__ = ("seq", "kind", "cancel")

    def __init__(self, seq: int, kind: str) -> None:
        self.seq = seq
        self.kind = kind
        self.cancel = threading.Event()


def run_cmd_async(cmd: list[str], desc: str = "") -> None:
    """Run a command in a daemon thread so the GLib loop is never blocked."""

    def _worker() -> None:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("%s failed: %s", desc or cmd[0], exc)
            return
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            log.warning(
                "%s exited %s: %s",
                desc or cmd[0],
                result.returncode,
                detail[-1] if detail else "no output",
            )
        elif desc:
            log.debug("%s succeeded.", desc)

    threading.Thread(target=_worker, daemon=True).start()


class TVPowerSyncDaemon:
    def __init__(self, config: tvlib.Config) -> None:
        self.config = config
        self.cec = tvlib.CecController(config.cec_device)

        self._panel_lock = threading.Lock()
        self._last_panel_warning = 0.0
        self.panel_dpms_path = tvlib.panel_dpms_path()
        if self.panel_dpms_path:
            log.info("Monitoring hardware display power at: %s", self.panel_dpms_path)
        else:
            log.warning(
                "No internal panel DPMS node found under /sys/class/drm; "
                "display tracking degraded."
            )

        self.last_dp_status = self.read_external_display_status()
        self.last_lid_state = self.read_initial_lid_state()
        self.last_hw_dpms = self.read_hardware_dpms()
        # Unknown (no panel node) must not be treated as "screen off":
        # that would turn the TV off while the user is working.
        self.state = "awake" if self.last_hw_dpms != "Off" else "asleep"
        self.is_locked = False

        # Power intents (written by the main loop, read by workers).
        self._intent_lock = threading.Lock()
        self._intent_seq = 0
        self._active_intent: Intent | None = None

        # Shared counters/flags.
        self._state_lock = threading.Lock()
        self._commanded_off = False
        self._last_commanded_off = 0.0
        self._offline_streak = 0
        self._watchdog_running = False

        self.backoff = tvlib.Backoff(initial=60.0, maximum=900.0)
        self._last_fallback_check = 0.0
        self._display_debounce_id = 0
        self._pending_trigger = ""

        self.uevent_socket = None
        self.setup_uevent_listener()

        log.info(
            "Initial display power state: (hardware: %s, external DP/HDMI: %s, lid: %s)",
            self.last_hw_dpms,
            self.last_dp_status,
            self.last_lid_state,
        )

        self.session_signals_registered = False
        self.system_signals_registered = False

        # Startup reconciliation runs off the main loop: a CEC query can take
        # up to a second and must not delay early D-Bus/uevent handling.
        GLib.timeout_add(500, self._initial_sync)
        self.update_dpms_timer(interval_ms=2000)
        GLib.timeout_add_seconds(5, self.on_watchdog_tick)

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def _initial_sync(self) -> bool:
        threading.Thread(target=self._initial_sync_worker, daemon=True).start()
        return False

    def _initial_sync_worker(self) -> None:
        try:
            if self.state == "awake":
                if self.read_external_display_status() != "connected":
                    return
                tv_state = tvlib.get_tv_power_state(self.cec, self.config.tv_ip)
                log.info("Initial TV power state: %s", tv_state)
                if tv_state in ("standby", "to-standby", "offline"):
                    log.info(
                        "[*] Laptop is ON and dock is connected, but TV is not awake "
                        "at startup. Triggering decisive wake..."
                    )
                    self.request_power_on("Startup initial sync")
            else:
                log.info(
                    "[*] Laptop panel is already off at startup; "
                    "reconciling TV power."
                )
                self.request_power_off("Startup: panel already off")
        except Exception:
            log.exception("Startup reconciliation failed")

    # ------------------------------------------------------------------
    # Kernel hotplug events
    # ------------------------------------------------------------------

    def setup_uevent_listener(self) -> None:
        """Bind to the kernel Netlink KObject UEvent socket."""
        sock = None
        try:
            sock = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, NETLINK_KOBJECT_UEVENT)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
            except OSError:
                pass
            sock.bind((0, 1))  # multicast group 1 (kernel uevents)
            sock.setblocking(False)
            self.uevent_socket = sock
            GLib.io_add_watch(
                sock.fileno(),
                GLib.IO_IN | GLib.IO_ERR | GLib.IO_HUP,
                self.on_uevent,
            )
            log.info(
                "Kernel Netlink UEvent listener registered for event-driven "
                "hotplug detection."
            )
        except Exception as exc:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            self.uevent_socket = None
            log.warning(
                "Could not bind Netlink socket (%s); falling back to %.0fs "
                "display polling.",
                exc,
                FALLBACK_POLL_SECONDS,
            )

    def on_uevent(self, source, condition) -> bool:
        """Drain available uevents and request a debounced display check."""
        if self.uevent_socket is None:
            return False
        if condition & (GLib.IO_ERR | GLib.IO_HUP | GLib.IO_NVAL):
            log.warning(
                "Netlink uevent socket closed or errored; switching to "
                "periodic display polling."
            )
            try:
                self.uevent_socket.close()
            except OSError:
                pass
            self.uevent_socket = None
            return False
        while True:
            try:
                data = self.uevent_socket.recv(4096)
            except BlockingIOError:
                break
            except OSError as exc:
                log.warning("Netlink uevent read failed: %s", exc)
                break
            if not data:
                break
            if self._uevent_is_relevant(data):
                self._schedule_display_check("Kernel uevent")
        return True

    @staticmethod
    def _uevent_is_relevant(data: bytes) -> bool:
        fields: dict[bytes, bytes] = {}
        for chunk in data.split(b"\0"):
            if b"=" in chunk:
                key, value = chunk.split(b"=", 1)
                fields[key] = value
        subsystem = fields.get(b"SUBSYSTEM", b"")
        action = fields.get(b"ACTION", b"")
        if subsystem == b"drm":
            # Real hotplug events carry HOTPLUG=1; add/remove covers connector
            # creation and teardown. Plain DPMS change events are ignored.
            return action in (b"add", b"remove") or fields.get(b"HOTPLUG") == b"1"
        if subsystem == b"usb":
            # Only attach/detach; HID and hub "change" events are noise here.
            return action in (b"add", b"remove")
        return False

    def _schedule_display_check(self, trigger: str) -> None:
        """Coalesce a burst of dock/PD renegotiation uevents into one check."""
        self._pending_trigger = trigger
        if self._display_debounce_id:
            return
        self._display_debounce_id = GLib.timeout_add(
            DISPLAY_DEBOUNCE_MS, self._on_display_settle
        )

    def _on_display_settle(self) -> bool:
        self._display_debounce_id = 0
        self.check_external_display_transition(self._pending_trigger)
        return False

    # ------------------------------------------------------------------
    # State readers
    # ------------------------------------------------------------------

    def read_initial_lid_state(self) -> str:
        """Read the lid state once via login1, falling back to /proc/acpi."""
        try:
            bus = dbus.SystemBus()
            manager = bus.get_object("org.freedesktop.login1", "/org/freedesktop/login1")
            props = dbus.Interface(manager, "org.freedesktop.DBus.Properties")
            is_closed = bool(props.Get("org.freedesktop.login1.Manager", "LidClosed"))
            return "closed" if is_closed else "open"
        except Exception as exc:
            log.debug("login1 lid query failed (%s); falling back to /proc/acpi", exc)
            for path in sorted(glob_lid_states()):
                try:
                    with open(path, "r") as handle:
                        if "closed" in handle.read().lower():
                            return "closed"
                except OSError:
                    pass
            return "open"

    def _resolve_panel(self) -> None:
        path = tvlib.panel_dpms_path()
        if path == self.panel_dpms_path:
            return
        if path:
            log.warning(
                "Internal panel DPMS node changed: %s -> %s",
                self.panel_dpms_path,
                path,
            )
        elif self.panel_dpms_path:
            log.warning(
                "Internal panel DPMS node disappeared: %s", self.panel_dpms_path
            )
        self.panel_dpms_path = path

    def read_hardware_dpms(self) -> str:
        """Read the internal panel's DRM DPMS state, re-resolving stale paths."""
        with self._panel_lock:
            path = self.panel_dpms_path
            if not path or not os.path.exists(path):
                self._resolve_panel()
                path = self.panel_dpms_path
            if not path:
                now = time.monotonic()
                if now - self._last_panel_warning > 60.0:
                    log.warning(
                        "Internal panel DPMS node unavailable; "
                        "cannot track display power."
                    )
                    self._last_panel_warning = now
                return "Unknown"
            try:
                with open(path, "r") as handle:
                    return handle.read().strip()
            except OSError as exc:
                log.debug("Cannot read %s: %s", path, exc)
                self._resolve_panel()
                return "Unknown"

    def read_external_display_status(self) -> str:
        return "connected" if tvlib.external_display_connected() else "disconnected"

    # ------------------------------------------------------------------
    # Display transitions
    # ------------------------------------------------------------------

    def check_external_display_transition(self, trigger: str = "") -> None:
        """Fire connect/disconnect actions only on an actual state change."""
        current = self.read_external_display_status()
        if current == self.last_dp_status:
            return
        old = self.last_dp_status
        self.last_dp_status = current
        self._set_offline_streak(0)
        log.info("External display transition (%s): %s -> %s", trigger, old, current)

        if current == "connected":
            self.backoff.reset()
            log.info("[*] External TV plugged in (dock connected). Waking TV...")
            self.request_power_on("Dock connected")
        else:
            log.info("[*] External TV unplugged (undocked). Sending sleep command via Wi-Fi...")
            self.request_power_off("Dock disconnected", undock=True)

    def on_login1_properties_changed(
        self, interface_name, changed_props, invalidated_props
    ) -> None:
        """Event-driven lid handling from systemd-logind."""
        if interface_name and interface_name != "org.freedesktop.login1.Manager":
            return
        if "LidClosed" not in changed_props:
            return
        new_lid_state = "closed" if bool(changed_props["LidClosed"]) else "open"
        if new_lid_state == self.last_lid_state:
            return
        log.info(
            "Laptop lid transition (D-Bus login1): %s -> %s",
            self.last_lid_state,
            new_lid_state,
        )
        self.last_lid_state = new_lid_state

        if new_lid_state == "closed":
            log.info("[*] Laptop lid closed. Putting TV to sleep...")
            self.state = "asleep"
            self.request_power_off("Lid closed")
        else:
            log.info("[*] Laptop lid opened. Ensuring displays are awake...")
            self.state = "awake"
            self.backoff.reset()
            if self.read_external_display_status() == "connected":
                self.request_power_on("Lid opened")

    # ------------------------------------------------------------------
    # DPMS polling and watchdog
    # ------------------------------------------------------------------

    def update_dpms_timer(self, interval_ms: int) -> None:
        """Use fast polling while locked, slow polling while working."""
        if getattr(self, "dpms_timer_id", 0):
            GLib.source_remove(self.dpms_timer_id)
        self.dpms_timer_id = GLib.timeout_add(interval_ms, self.on_dpms_poll)
        log.debug("DPMS poll interval set to %dms", interval_ms)

    def on_dpms_poll(self) -> bool:
        """Cheap sysfs read; no subprocesses and no network here."""
        current = self.read_hardware_dpms()
        if current != self.last_hw_dpms:
            log.info("Hardware DPMS transition: %s -> %s", self.last_hw_dpms, current)
            self.last_hw_dpms = current
            if current == "Off":
                self.handle_screen_off("Laptop display DPMS went Off (hardware)")
            elif current == "On":
                self.handle_screen_on("Laptop display DPMS went On (hardware)")

        if self.uevent_socket is None:
            now = time.monotonic()
            if now - self._last_fallback_check >= FALLBACK_POLL_SECONDS:
                self._last_fallback_check = now
                self.check_external_display_transition("Periodic fallback poll")
        return True

    def on_watchdog_tick(self) -> bool:
        """Queue a watchdog check without ever blocking the event loop."""
        if (
            self.state != "awake"
            or self.last_hw_dpms != "On"
            or self.last_dp_status != "connected"
            or self.last_lid_state != "open"
        ):
            return True

        with self._intent_lock:
            active = self._active_intent
            wake_running = (
                active is not None
                and active.kind == "on"
                and not active.cancel.is_set()
            )
        if wake_running or not self.backoff.ready():
            return True

        with self._state_lock:
            if self._watchdog_running:
                return True
            self._watchdog_running = True

        threading.Thread(target=self._watchdog_worker, daemon=True).start()
        return True

    def _watchdog_worker(self) -> None:
        try:
            if tvlib.is_tv_network_awake(self.config.tv_ip, timeout=0.2):
                self._set_offline_streak(0)
                return
            if self.read_external_display_status() != "connected":
                self._set_offline_streak(0)
                return

            tv_state = tvlib.get_tv_power_state(
                self.cec, self.config.tv_ip, use_cec=True
            )
            # The dock may have been unplugged while the CEC query was running.
            if self.read_external_display_status() != "connected":
                self._set_offline_streak(0)
                return

            if tv_state in ("standby", "to-standby"):
                log.warning(
                    "[!] Watchdog: dock connected and logged in, but TV is in "
                    "'%s'. Waking TV...",
                    tv_state,
                )
                self._set_offline_streak(0)
                self.request_power_on("Watchdog - TV asleep while docked and logged in")
            elif tv_state == "offline":
                streak = self._bump_offline_streak()
                if streak >= 2:
                    log.warning(
                        "[!] Watchdog: TV offline for %d consecutive checks while "
                        "docked and logged in. Waking TV...",
                        streak,
                    )
                    self._set_offline_streak(0)
                    self.request_power_on(
                        "Watchdog - TV offline while docked and logged in"
                    )
                else:
                    log.debug("Watchdog: TV offline (streak %d/2)", streak)
            else:
                self._set_offline_streak(0)
        except Exception:
            log.exception("Watchdog check failed")
        finally:
            with self._state_lock:
                self._watchdog_running = False

    def _set_offline_streak(self, value: int) -> None:
        with self._state_lock:
            self._offline_streak = value

    def _bump_offline_streak(self) -> int:
        with self._state_lock:
            self._offline_streak += 1
            return self._offline_streak

    # ------------------------------------------------------------------
    # Power intent bookkeeping
    # ------------------------------------------------------------------

    def _new_intent_locked(self, kind: str) -> Intent:
        self._intent_seq += 1
        if self._active_intent is not None:
            self._active_intent.cancel.set()
        intent = Intent(self._intent_seq, kind)
        self._active_intent = intent
        return intent

    def _finish_intent(self, intent: Intent) -> None:
        """Clear a completed intent so it cannot suppress future requests."""
        with self._intent_lock:
            if self._active_intent is intent:
                self._active_intent = None

    def _mark_commanded_off(self) -> None:
        with self._state_lock:
            self._commanded_off = True
            self._last_commanded_off = time.monotonic()

    def _clear_commanded_off(self) -> None:
        with self._state_lock:
            self._commanded_off = False

    def _commanded_off_recently(self, window: float = 12.0) -> bool:
        with self._state_lock:
            return (
                self._commanded_off
                and (time.monotonic() - self._last_commanded_off) < window
            )

    def _recent_off_elapsed(self) -> float | None:
        with self._state_lock:
            if not self._commanded_off:
                return None
            return time.monotonic() - self._last_commanded_off

    def request_power_off(self, reason: str, undock: bool | None = None) -> None:
        """Cancel any in-flight wake and put the TV to sleep."""
        with self._intent_lock:
            intent = self._new_intent_locked("off")
        if undock is None:
            undock = self.read_external_display_status() != "connected"
        threading.Thread(
            target=self._power_off_worker,
            args=(intent, reason, undock),
            daemon=True,
        ).start()

    def request_power_on(self, reason: str) -> None:
        """Start a decisive wake unless one is already running or backed off."""
        with self._intent_lock:
            active = self._active_intent
            if (
                active is not None
                and active.kind == "on"
                and not active.cancel.is_set()
            ):
                log.info(
                    "[*] Wake already in progress; ignoring duplicate trigger (%s).",
                    reason,
                )
                return
            if not self.backoff.ready():
                log.info(
                    "[*] Wake backed off for another %ds (%s)",
                    int(self.backoff.remaining()),
                    reason,
                )
                return
            intent = self._new_intent_locked("on")

        threading.Thread(
            target=self._wake_worker, args=(intent, reason), daemon=True
        ).start()

    # ------------------------------------------------------------------
    # Sleep path
    # ------------------------------------------------------------------

    def _power_off_worker(self, intent: Intent, reason: str, undock: bool) -> None:
        cancel = intent.cancel
        try:
            if self._commanded_off_recently():
                log.info(
                    "[*] TV was already commanded off recently; skipping "
                    "duplicate sleep command (%s).",
                    reason,
                )
                return
            if cancel.is_set():
                return

            connected = self.read_external_display_status() == "connected"
            tv_state = tvlib.get_tv_power_state(
                self.cec, self.config.tv_ip, use_cec=connected, cancel=cancel
            )
            if cancel.is_set():
                return
            if tv_state in ("standby", "to-standby"):
                log.info("[*] TV is already in %s. Skipping power toggle.", tv_state)
                self._mark_commanded_off()
                return

            # Prefer the discrete CEC path; never send both a standby and a
            # toggle, because KEY_POWER would undo the standby.
            if connected and not undock:
                log.info("[*] Sending HDMI-CEC standby to TV (%s)...", reason)
                if self.cec.standby(cancel):
                    self._mark_commanded_off()
                    log.info(
                        "[*] TV sleep command delivered via HDMI-CEC (%s).", reason
                    )
                    return
                if cancel.is_set():
                    return
                log.warning(
                    "HDMI-CEC standby did not confirm; re-checking power state "
                    "before the Wi-Fi fallback."
                )
                state_after = tvlib.get_tv_power_state(
                    self.cec, self.config.tv_ip, use_cec=True, cancel=cancel
                )
                if cancel.is_set():
                    return
                if state_after in ("standby", "to-standby"):
                    self._mark_commanded_off()
                    log.info(
                        "[*] TV reached %s despite the CEC error; skipping the "
                        "WebSocket toggle.",
                        state_after,
                    )
                    return
                log.warning("Falling back to Wi-Fi WebSocket for TV standby.")

            attempts = 10 if undock else 4
            for attempt in range(1, attempts + 1):
                if cancel.is_set():
                    return
                if undock and self.read_external_display_status() == "connected":
                    log.info(
                        "[*] Dock reconnected during Wi-Fi sleep retry; "
                        "aborting sleep."
                    )
                    return
                if tvlib.is_tv_network_awake(self.config.tv_ip, timeout=0.6):
                    if tvlib.send_key(
                        self.config.tv_ip,
                        "KEY_POWER",
                        timeout=3.0,
                        cancel=cancel,
                    ):
                        self._mark_commanded_off()
                        log.info(
                            "[*] TV sleep command delivered over Wi-Fi on "
                            "attempt %d/%d (%s).",
                            attempt,
                            attempts,
                            reason,
                        )
                        return
                if attempt < attempts and cancel.wait(0.5):
                    return

            log.info(
                "[*] Could not reach TV over Wi-Fi after %d attempts (%s); "
                "TV may already be off.",
                attempts,
                reason,
            )
        except Exception:
            log.exception("Error while sending TV sleep command (%s)", reason)
        finally:
            self._finish_intent(intent)

    # ------------------------------------------------------------------
    # Wake path
    # ------------------------------------------------------------------

    def _wake_worker(self, intent: Intent, reason: str) -> None:
        try:
            try:
                succeeded = self._do_decisive_wake(intent, reason)
            except Exception:
                log.exception("Error during decisive wake")
                succeeded = False

            with self._intent_lock:
                is_current = self._active_intent is intent
            if not is_current or intent.cancel.is_set():
                log.debug("Wake intent %d superseded; not applying backoff", intent.seq)
                return

            if succeeded:
                self.backoff.reset()
                log.info("[*] Wake sequence completed successfully.")
            else:
                delay = self.backoff.fail()
                log.warning(
                    "[!] Wake failed; next automatic attempt in %ds.", int(delay)
                )
        finally:
            self._finish_intent(intent)

    def _do_decisive_wake(self, intent: Intent, reason: str) -> bool:
        cancel = intent.cancel
        if cancel.is_set() or self.read_external_display_status() != "connected":
            log.info(
                "[*] Aborting wake (%s) because external display is not connected.",
                reason,
            )
            return False

        log.info("--> [DECISIVE WAKE] (%s)", reason)

        # Respect the TV's shutdown settling window. A reported "on" is not
        # trustworthy while Tizen is still shutting down (port 8001 can linger
        # for several seconds), so a recent off forces the full wake sequence.
        elapsed = self._recent_off_elapsed()
        recent_off = elapsed is not None and elapsed < SETTLE_SECONDS
        if recent_off:
            wait_time = SETTLE_SECONDS - elapsed
            log.info(
                "[*] Wake requested %.2fs after sleep. Waiting %.2fs for TV "
                "shutdown cycle...",
                elapsed,
                wait_time,
            )
            if cancel.wait(wait_time):
                log.info("[*] Wake cancelled during settle wait.")
                return False
        if cancel.is_set() or self.read_external_display_status() != "connected":
            log.info("[*] Aborting wake after settle wait; external display disconnected.")
            return False

        self._clear_commanded_off()
        self.ensure_dpms_on()
        ext_conn = tvlib.external_connector_name()

        if recent_off:
            # We already know the TV was commanded off; skip the pre-wake CEC
            # query and start the discrete wake immediately.
            log.info(
                "[*] TV was commanded off %.1fs ago; starting the full wake "
                "sequence.",
                elapsed,
            )
        else:
            tv_state = tvlib.get_tv_power_state(
                self.cec, self.config.tv_ip, use_cec=True, cancel=cancel
            )
            if cancel.is_set():
                return False
            if tv_state in ("on", "to-on"):
                log.info(
                    "[*] TV is already '%s'. Re-asserting HDMI active source "
                    "and display mode...",
                    tv_state,
                )
                self._assert_video(ext_conn, cancel)
                return True

        log.info("Broadcasting rapid Wake-on-LAN burst (5 packets) across all NICs...")
        tvlib.send_wol(
            self.config.tv_mac,
            self.config.tv_ip,
            bursts=5,
            delay_between=0.05,
            cancel=cancel,
        )
        if cancel.is_set():
            return False

        if not self.cec.wake(cancel):
            log.warning(
                "CEC wake command did not succeed; continuing with verification."
            )
        if cancel.is_set():
            return False

        for attempt in range(1, 11):
            # Poll quickly at first (Tizen boots in a few seconds), then relax.
            wait_time = 0.75 if attempt <= 4 else 1.5
            if cancel.wait(wait_time):
                log.info("[*] Wake cancelled during verification loop.")
                return False
            if self.read_external_display_status() != "connected":
                log.info("[*] External display disconnected during wake loop; aborting wake.")
                return False

            log.info("Wake attempt %d/10: waiting for TV...", attempt)
            current_state = tvlib.get_tv_power_state(
                self.cec,
                self.config.tv_ip,
                use_cec=True,
                cancel=cancel,
                # The discrete wake has already been sent; the TCP probe is a
                # valid fast confirmation at this point.
                allow_network_fallback=True,
            )
            if cancel.is_set():
                return False
            if current_state in ("on", "to-on"):
                log.info(
                    "[*] TV confirmed awake (%s) on attempt %d. Syncing video "
                    "stream and active source...",
                    current_state,
                    attempt,
                )
                self._assert_video(ext_conn, cancel)
                return True

            tvlib.send_wol(
                self.config.tv_mac,
                self.config.tv_ip,
                bursts=4,
                delay_between=0.05,
                cancel=cancel,
            )
            if cancel.is_set():
                return False
            # Re-sending the full CEC wake blocks this loop for ~3s; the TV
            # usually needs only the initial command, so retry every third
            # attempt and keep polling in between.
            if attempt % 3 == 0 and not self.cec.wake(cancel):
                log.debug("CEC wake retry %d did not succeed.", attempt)

        log.warning("[!] Decisive wake did not detect the TV awake after 10 attempts.")
        return False

    def _assert_video(self, connector: str | None, cancel: threading.Event) -> None:
        if cancel.is_set():
            return
        if connector:
            run_cmd_async(
                [
                    "kscreen-doctor",
                    f"output.{connector}.mode.{self.config.tv_mode}",
                ],
                "Video mode re-assert",
            )
        else:
            log.warning(
                "No active external connector found; skipping kscreen-doctor "
                "mode re-assert."
            )
        if not self.cec.wake(cancel):
            log.warning("CEC wake/active-source command did not succeed.")

    def ensure_dpms_on(self) -> None:
        current = self.read_hardware_dpms()
        if current == "On":
            return
        log.info("Requesting KDE DPMS on (panel currently %s)", current)
        run_cmd_async(["kscreen-doctor", "--dpms", "on"], "DPMS on")

    # ------------------------------------------------------------------
    # Screen / session events
    # ------------------------------------------------------------------

    def handle_screen_off(self, reason: str) -> None:
        log.info("--> [OFF DETECTED] (%s)", reason)
        self.state = "asleep"

        if "DPMS" in reason:
            log.info("[*] Locking session for idle DPMS display turn-off (autolock).")
            session_id = os.environ.get("XDG_SESSION_ID")
            cmd = ["loginctl", "lock-session"]
            if session_id:
                cmd.append(session_id)
            run_cmd_async(cmd, "Lock session")

        self.request_power_off(reason)

    def handle_screen_on(self, reason: str) -> None:
        log.info("--> [ON DETECTED] (%s)", reason)
        self.state = "awake"
        self.backoff.reset()

        if self.read_external_display_status() == "connected":
            self.ensure_dpms_on()
            self.request_power_on(reason)
        else:
            log.info("[*] External display not connected; skipping TV wake.")

    def on_screensaver_active_changed(self, active: bool) -> None:
        if active:
            log.info("KDE ScreenSaver ActiveChanged: TRUE (locked)")
            self.is_locked = True
            self.update_dpms_timer(interval_ms=250)
        else:
            log.info("KDE ScreenSaver ActiveChanged: FALSE (unlocked)")
            self.update_dpms_timer(interval_ms=2000)
            if self.is_locked:
                self.is_locked = False
                self.handle_screen_on("KDE ScreenSaver Unlocked")
            else:
                log.info(
                    "[*] Session already unlocked; ignoring duplicate "
                    "ActiveChanged: FALSE."
                )

    def on_prepare_for_sleep(self, sleeping: bool) -> None:
        if sleeping:
            log.info("systemd PrepareForSleep: suspending")
            self.handle_screen_off("systemd PrepareForSleep: suspending")
        else:
            log.info("systemd PrepareForSleep: resumed")
            # KWin restores DPMS asynchronously; reconcile after it settles and
            # never force the panel on with the lid shut.
            GLib.timeout_add_seconds(2, self._resume_reconcile)

    def _resume_reconcile(self) -> bool:
        if self.last_lid_state != "open":
            log.info("[*] Resume with lid closed; leaving TV power unchanged.")
            return False

        self.check_external_display_transition("Resume from sleep")
        hw_dpms = self.read_hardware_dpms()
        if hw_dpms == "On":
            self.handle_screen_on("Resume from sleep")
        else:
            log.info(
                "[*] Panel is %s after resume; leaving TV power unchanged until "
                "DPMS changes.",
                hw_dpms,
            )
        return False


def glob_lid_states() -> list[str]:
    return glob.glob("/proc/acpi/button/lid/*/state")


def register_signal_handlers(loop: GLib.MainLoop) -> None:
    def _handler(signum, frame):
        log.info("Termination signal received. Exiting daemon.")
        loop.quit()

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


def main() -> int:
    tvlib.setup_logging()
    try:
        config = tvlib.Config.from_env()
    except ValueError as exc:
        log.error("Invalid configuration: %s", exc)
        return 2

    log.info("Starting Samsung TV Power Synchronization Daemon (event-driven)...")

    DBusGMainLoop(set_as_default=True)
    daemon = TVPowerSyncDaemon(config)

    def register_session_signals() -> bool:
        try:
            bus = dbus.SessionBus()
            bus.add_signal_receiver(
                daemon.on_screensaver_active_changed,
                signal_name="ActiveChanged",
                dbus_interface="org.freedesktop.ScreenSaver",
                path="/org/freedesktop/ScreenSaver",
            )
            daemon.session_signals_registered = True
            log.info("D-Bus Session Bus signal listener registered.")
            return True
        except Exception as exc:
            log.warning(
                "Session bus listener unavailable (%s); retrying in 5s.", exc
            )
            return False

    def schedule_session_registration() -> bool:
        if daemon.session_signals_registered:
            return False
        if not register_session_signals():
            GLib.timeout_add_seconds(5, schedule_session_registration)
        return False

    def register_system_signals() -> bool:
        try:
            bus = dbus.SystemBus()
            bus.add_signal_receiver(
                daemon.on_prepare_for_sleep,
                signal_name="PrepareForSleep",
                dbus_interface="org.freedesktop.login1.Manager",
                bus_name="org.freedesktop.login1",
            )
            bus.add_signal_receiver(
                daemon.on_login1_properties_changed,
                signal_name="PropertiesChanged",
                dbus_interface="org.freedesktop.DBus.Properties",
                path="/org/freedesktop/login1",
            )
            daemon.system_signals_registered = True
            log.info(
                "D-Bus System Bus signal listeners registered "
                "(PrepareForSleep, LidClosed)."
            )
            return True
        except Exception as exc:
            log.warning(
                "System bus listener registration failed (%s); retrying in 5s.",
                exc,
            )
            return False

    def schedule_system_registration() -> bool:
        if daemon.system_signals_registered:
            return False
        if not register_system_signals():
            GLib.timeout_add_seconds(5, schedule_system_registration)
        return False

    register_session_signals()
    if not daemon.session_signals_registered:
        GLib.timeout_add_seconds(5, schedule_session_registration)
    register_system_signals()
    if not daemon.system_signals_registered:
        GLib.timeout_add_seconds(5, schedule_system_registration)

    loop = GLib.MainLoop()
    register_signal_handlers(loop)

    log.info(
        "Daemon is active. Monitoring hardware DPMS, kernel uevents, and "
        "D-Bus signals."
    )
    loop.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
