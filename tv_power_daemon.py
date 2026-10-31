#!/usr/bin/env python3
"""
tv_power_daemon.py - Synchronize Samsung TV power with laptop display state
Event-Driven Architecture:
  1. Kernel Netlink KObject UEvent socket (zero-polling dock / display hotplug)
  2. D-Bus systemd login1 PropertiesChanged (zero-polling hardware lid events)
  3. D-Bus KDE ScreenSaver ActiveChanged signal (screen lock/unlock)
  4. D-Bus systemd login1 PrepareForSleep signal (system suspend/resume)
  5. Adaptive hardware DPMS tracking (fast 250ms when locked, slow 2s when working)
  6. Lightweight socket-based watchdog (zero subprocess forks while TV is streaming)

Actions:
  - When laptop display turns OFF:
      1. Transmit WebSocket KEY_POWER keystroke packet to TV over Wi-Fi
      2. Issue HDMI-CEC Standby command so TV doesn't idle on 'No Signal'
  - When laptop display turns ON:
      1. Broadcast Wake-on-LAN Magic Packet to TV MAC (40:16:3b:d0:4f:8b)
      2. Send discrete HDMI-CEC Image View On & Active Source (HDMI 1)
      3. Re-assert native 4K60 video mode via kscreen-doctor once TV is ready
"""

import sys
import os
# Add venv to sys.path so we can import samsungtvws without breaking system dbus
sys.path.append("/home/mike/src/drmstuff/venv/lib/python3.14/site-packages")

# Ensure Wayland environment variables are set so kscreen-doctor works from systemd user service
os.environ.setdefault("WAYLAND_DISPLAY", "wayland-0")
os.environ.setdefault("QT_QPA_PLATFORM", "wayland")

import glob
import signal
import socket
import json
import base64
import subprocess
import threading
import time
from datetime import datetime

try:
    import dbus
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib
except ImportError:
    print("Error: dbus and GLib Python bindings are required.")
    sys.exit(1)

try:
    from websockets.sync.client import connect as ws_connect
except ImportError:
    ws_connect = None

TV_IP = "192.168.0.2"
TV_MAC = "40:16:3b:d0:4f:8b"
CEC_DEVICE = "/dev/cec0"

def log(msg: str):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}", flush=True)

def send_wol(mac=TV_MAC, bursts=3, delay_between=0.05):
    """Broadcast Wake-on-LAN magic packets across all active network interfaces."""
    mac_clean = mac.replace(":", "").replace("-", "")
    if len(mac_clean) != 12:
        log(f"Error: Invalid MAC address {mac}")
        return False
    mac_bytes = bytes.fromhex(mac_clean)
    packet = b"\xff" * 6 + mac_bytes * 16

    targets = {"255.255.255.255", "192.168.0.255", TV_IP}
    local_ips = []
    try:
        out = subprocess.run(["ip", "-o", "-4", "addr", "show"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            parts = line.split()
            if "inet" in parts:
                ip_addr = parts[parts.index("inet") + 1].split("/")[0]
                if ip_addr.startswith("192.168.") or ip_addr.startswith("10."):
                    local_ips.append(ip_addr)
            if "brd" in parts:
                brd = parts[parts.index("brd") + 1]
                if brd.startswith("192.168.") or brd.startswith("10.") or brd == "255.255.255.255":
                    targets.add(brd)
    except Exception:
        pass

    for burst_idx in range(bursts):
        # 1. Global / subnet broadcasts
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            for target in targets:
                for port in (9, 7):
                    try:
                        s.sendto(packet, (target, port))
                    except Exception:
                        pass

        # 2. Bound to each local NIC (Ethernet and Wi-Fi)
        for local_ip in local_ips:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                    s.bind((local_ip, 0))
                    for port in (9, 7):
                        s.sendto(packet, ("255.255.255.255", port))
                        s.sendto(packet, ("192.168.0.255", port))
            except Exception:
                pass

        if burst_idx < bursts - 1:
            time.sleep(delay_between)

    log(f"Wake-on-LAN packet broadcasted across all network interfaces to {mac} ({bursts} burst{'s' if bursts > 1 else ''})")
    return True

def send_tv_websocket_key(key="KEY_POWEROFF"):
    """Send remote control key over Samsung TV WebSocket interface using samsungtvws."""
    try:
        from samsungtvws import SamsungTVWS
        tv = SamsungTVWS(TV_IP, timeout=1.5)
        tv.shortcuts().remote.send_key(key)
        log(f"WebSocket remote keystroke '{key}' sent via samsungtvws.")
        return True
    except Exception as e:
        log(f"WebSocket send ({key}) error: {e}")
        return False

def run_cmd(cmd: str):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)

def run_cmd_async(cmd: str, desc: str = ""):
    """Run command asynchronously in a daemon thread so the GLib event loop is never blocked."""
    def _worker():
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if desc:
            if res.returncode == 0:
                log(f"{desc} executed successfully.")
            else:
                log(f"{desc} completed with code {res.returncode}")
    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return t

def is_tv_network_awake(timeout=0.2) -> bool:
    """Non-blocking TCP socket check to see if TV network port 8001 is listening.
    Takes < 1ms on LAN and generates zero subprocess forks.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            return s.connect_ex((TV_IP, 8001)) == 0
    except Exception:
        return False

def get_tv_power_state(use_cec: bool = True) -> str:
    """Uses cec-ctl to get the current power state of the TV, falling back to network reachability.
    Returns 'on', 'standby', 'to-on', 'to-standby', or 'offline'.
    """
    if use_cec:
        try:
            output = subprocess.check_output(
                ["cec-ctl", "-d", "/dev/cec0", "--playback", "--to", "0", "--give-device-power-status"],
                stderr=subprocess.STDOUT, timeout=1.0
            ).decode()
            for line in output.splitlines():
                if "pwr-state:" in line:
                    return line.split(":")[1].split("(")[0].strip()
        except Exception:
            pass

    # If CEC did not return a decisive state (bus collision, packet drop, or undocked),
    # check if the TV network interface (port 8001) is active.
    if is_tv_network_awake(timeout=0.4):
        return "on"

    return "offline"

class TVPowerSyncDaemon:

    def __init__(self):
        # Locate laptop internal display dpms sysfs node
        candidates = glob.glob("/sys/class/drm/*-eDP-*/dpms")
        self.edp_dpms_path = candidates[0] if candidates else "/sys/class/drm/card1-eDP-1/dpms"
        log(f"Monitoring hardware display power at: {self.edp_dpms_path}")

        self.last_dp_status = self.read_external_display_status()
        self.last_lid_state = self.read_initial_lid_state()
        self.wake_in_progress = False
        self.abort_wake = False
        self.wake_backoff_until = 0.0
        self.offline_streak = 0
        self.tv_commanded_off = False
        self.last_off_time = 0.0

        # Initial state
        initial_hw = self.read_hardware_dpms()
        self.last_hw_dpms = initial_hw
        self.is_locked = False
        self.dpms_timer_id = 0
        log(f"Initial display power state: (hardware: {initial_hw}, external DP/HDMI: {self.last_dp_status}, lid: {self.last_lid_state})")

        # Setup kernel netlink uevent socket for zero-polling hotplug monitoring
        self.uevent_socket = None
        self.setup_uevent_listener()

        if initial_hw == "On":
            self.state = "awake"
            # If external display is connected at startup, decisively ensure TV is awake
            if self.last_dp_status == "connected":
                tv_state = get_tv_power_state()
                log(f"Initial TV power state: {tv_state}")
                if tv_state in ("standby", "to-standby", "offline"):
                    log("[*] Laptop is ON and dock is connected, but TV is not awake at startup. Triggering decisive wake...")
                    self.trigger_decisive_wake("Startup initial sync")
        else:
            self.state = "asleep"

        # Start adaptive DPMS timer (2000ms while unlocked; switches to 250ms when locked)
        self.update_dpms_timer(interval_ms=2000)

        # Start lightweight 5-second watchdog timer
        GLib.timeout_add_seconds(5, self.on_watchdog_tick)

    def setup_uevent_listener(self):
        """Bind to Linux kernel Netlink KObject UEvent socket for push hotplug events."""
        try:
            self.uevent_socket = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 15) # NETLINK_KOBJECT_UEVENT = 15
            self.uevent_socket.bind((0, 1)) # Multicast group 1 (kernel uevents)
            self.uevent_socket.setblocking(False)
            GLib.io_add_watch(self.uevent_socket.fileno(), GLib.IO_IN, self.on_uevent)
            log("Kernel Netlink UEvent listener registered for event-driven hotplug detection.")
        except Exception as e:
            log(f"Warning: Could not bind Netlink socket ({e}); falling back to periodic display check.")

    def on_uevent(self, source, condition):
        """Callback fired by GLib event loop when the Linux kernel emits a hardware uevent."""
        try:
            data = self.uevent_socket.recv(4096)
            # Filter for DRM or USB device change / add / remove events
            if b"SUBSYSTEM=drm" in data or b"SUBSYSTEM=usb" in data:
                self.check_external_display_transition("Kernel uevent")
        except Exception:
            pass
        return True # Keep watch active

    def read_initial_lid_state(self) -> str:
        """Read initial lid state via D-Bus login1, falling back to /proc/acpi once at startup."""
        try:
            bus = dbus.SystemBus()
            manager = bus.get_object("org.freedesktop.login1", "/org/freedesktop/login1")
            props = dbus.Interface(manager, "org.freedesktop.DBus.Properties")
            is_closed = bool(props.Get("org.freedesktop.login1.Manager", "LidClosed"))
            return "closed" if is_closed else "open"
        except Exception:
            for p in glob.glob("/proc/acpi/button/lid/*/state"):
                try:
                    with open(p, "r") as f:
                        if "closed" in f.read().lower():
                            return "closed"
                except Exception:
                    pass
            return "open"

    def read_hardware_dpms(self) -> str:
        """Fast 5-microsecond read of the laptop panel's DRM sysfs dpms state."""
        try:
            with open(self.edp_dpms_path, "r") as f:
                return f.read().strip()
        except Exception:
            return "Unknown"

    def get_external_connector_name(self) -> str:
        """Find the active external connector name (e.g. 'DP-2', 'DP-1', 'HDMI-A-1')."""
        for p in glob.glob("/sys/class/drm/*-DP-*/status") + glob.glob("/sys/class/drm/*-HDMI-*/status"):
            try:
                with open(p, "r") as f:
                    if f.read().strip() == "connected":
                        parts = os.path.basename(os.path.dirname(p)).split("-", 1)
                        if len(parts) == 2:
                            return parts[1]
            except Exception:
                pass
        return "DP-2"

    def read_external_display_status(self) -> str:
        """Check all DisplayPort and HDMI connectors for an active connection."""
        for p in glob.glob("/sys/class/drm/*-DP-*/status") + glob.glob("/sys/class/drm/*-HDMI-*/status"):
            try:
                with open(p, "r") as f:
                    if f.read().strip() == "connected":
                        return "connected"
            except Exception:
                pass
        return "disconnected"

    def check_external_display_transition(self, trigger: str = ""):
        """Evaluates external display status and fires connect/disconnect actions only on state change."""
        current_dp = self.read_external_display_status()
        if current_dp != self.last_dp_status:
            log(f"External display transition ({trigger}): {self.last_dp_status} -> {current_dp}")
            old_dp = self.last_dp_status
            self.last_dp_status = current_dp
            self.wake_backoff_until = 0.0
            self.offline_streak = 0

            if current_dp == "connected":
                log("[*] External TV plugged in (dock connected). Decisively waking TV...")
                self.trigger_decisive_wake("Dock connected")
            elif current_dp == "disconnected" and old_dp == "connected":
                log("[*] External TV unplugged (undocked). Sending sleep command to TV via Wi-Fi...")
                self.send_tv_sleep_wifi("Dock disconnected", is_undock=True)

    def on_login1_properties_changed(self, interface_name, changed_props, invalidated_props):
        """Event-driven callback for systemd logind hardware switch changes (zero ACPI polling)."""
        if "LidClosed" in changed_props:
            is_closed = bool(changed_props["LidClosed"])
            new_lid_state = "closed" if is_closed else "open"
            if new_lid_state != self.last_lid_state:
                log(f"Laptop lid transition (D-Bus login1): {self.last_lid_state} -> {new_lid_state}")
                self.last_lid_state = new_lid_state
                current_dp = self.read_external_display_status()
                if new_lid_state == "closed":
                    log("[*] Laptop lid closed. Putting TV to sleep...")
                    if current_dp == "connected":
                        run_cmd_async("cec-ctl -d /dev/cec0 --playback --to 0 --standby")
                    self.send_tv_sleep_wifi("Lid closed", is_undock=(current_dp != "connected"))
                    self.state = "asleep"
                elif new_lid_state == "open":
                    log("[*] Laptop lid opened. Ensuring displays are awake...")
                    if current_dp == "connected":
                        self.trigger_decisive_wake("Lid opened")

    def update_dpms_timer(self, interval_ms: int):
        """Switches DPMS polling interval: fast (250ms) when locked, slow (2000ms) when unlocked."""
        if self.dpms_timer_id:
            GLib.source_remove(self.dpms_timer_id)
        self.dpms_timer_id = GLib.timeout_add(interval_ms, self.on_dpms_poll)

    def on_dpms_poll(self) -> bool:
        """Minimalist 5-microsecond poll of hardware DPMS node."""
        current_hw = self.read_hardware_dpms()
        if current_hw != self.last_hw_dpms:
            log(f"Hardware DPMS transition: {self.last_hw_dpms} -> {current_hw}")
            self.last_hw_dpms = current_hw
            if current_hw == "Off":
                self.handle_screen_off("Laptop display DPMS went Off (hardware)")
            elif current_hw == "On":
                self.handle_screen_on("Laptop display DPMS went On (hardware)")
        return True

    def on_watchdog_tick(self) -> bool:
        """Periodic lightweight watchdog (every 5 seconds) to ensure TV stays awake while working."""
        if (self.state == "awake" and
            self.last_hw_dpms == "On" and
            self.last_dp_status == "connected" and
            self.last_lid_state == "open" and
            not self.wake_in_progress and
            time.time() >= self.wake_backoff_until):

            # Fast path: check port 8001 (takes 0.4ms, ZERO subprocesses while TV is streaming)
            if is_tv_network_awake(timeout=0.2):
                self.offline_streak = 0
                return True

            # Port 8001 was unreachable; check CEC power state to determine if TV turned off
            tv_state = get_tv_power_state()
            if self.read_external_display_status() != "connected":
                self.offline_streak = 0
            elif tv_state in ("standby", "to-standby"):
                log(f"[!] Watchdog check: Logged in and dock connected, but TV is in '{tv_state}'. Decisively waking TV...")
                self.offline_streak = 0
                self.trigger_decisive_wake("Watchdog - TV asleep while dock connected and logged in")
            elif tv_state == "offline":
                self.offline_streak += 1
                if self.offline_streak >= 2:
                    log(f"[!] Watchdog check: Logged in and dock connected, but TV is 'offline' ({self.offline_streak} consecutive checks). Decisively waking TV...")
                    self.offline_streak = 0
                    self.trigger_decisive_wake("Watchdog - TV offline while dock connected and logged in")
            else:
                self.offline_streak = 0

        return True

    def send_tv_sleep_wifi(self, reason: str, is_undock: bool = False):
        """Put TV to sleep via Wi-Fi/WebSocket (e.g. when undocked or lid closed).
        Includes automatic retry across network interface failover (dock Ethernet -> Wi-Fi).
        """
        self.abort_wake = True

        def _worker():
            log(f"[*] Sending Wi-Fi sleep command to TV ({reason})...")
            elapsed_since_off = time.time() - self.last_off_time
            if self.tv_commanded_off and elapsed_since_off < 12.0:
                log(f"[*] TV was already commanded off {elapsed_since_off:.1f}s ago. Skipping duplicate KEY_POWER toggle.")
                return

            if not is_undock and self.read_external_display_status() == "connected":
                tv_state = get_tv_power_state(use_cec=True)
                if tv_state in ("standby", "to-standby"):
                    log(f"[*] TV is already in {tv_state}. Skipping Wi-Fi sleep.")
                    self.tv_commanded_off = True
                    self.last_off_time = time.time()
                    return

            max_attempts = 10 if is_undock else 4
            for attempt in range(1, max_attempts + 1):
                if is_undock and self.read_external_display_status() == "connected":
                    log("[*] Dock reconnected during Wi-Fi sleep retry; aborting sleep.")
                    return

                if is_tv_network_awake(timeout=0.6):
                    if send_tv_websocket_key("KEY_POWER"):
                        self.tv_commanded_off = True
                        self.last_off_time = time.time()
                        log(f"[*] TV sleep command delivered over Wi-Fi on attempt {attempt}/{max_attempts} ({reason}).")
                        return
                if attempt < max_attempts:
                    time.sleep(0.5)

            log(f"[*] Could not reach TV over Wi-Fi after {max_attempts} attempts ({reason}) - TV may already be off.")

        threading.Thread(target=_worker, daemon=True).start()

    def trigger_decisive_wake(self, reason: str):
        """Spawns non-blocking worker thread to decisively wake the TV."""
        if self.wake_in_progress:
            log(f"[*] Wake sequence already running; ignoring duplicate trigger ({reason}).")
            return
        if time.time() < self.wake_backoff_until:
            log(f"[*] Wake backed off for another {int(self.wake_backoff_until - time.time())}s ({reason}).")
            return

        self.abort_wake = False
        self.wake_in_progress = True
        threading.Thread(target=self._decisive_wake_worker, args=(reason,), daemon=True).start()

    def _decisive_wake_worker(self, reason: str):
        try:
            self._do_decisive_wake(reason)
        except Exception as e:
            log(f"Error during decisive wake: {e}")
        finally:
            self.wake_in_progress = False

    def _do_decisive_wake(self, reason: str):
        if self.abort_wake or self.read_external_display_status() != "connected":
            log(f"[*] Aborting wake ({reason}) because external display is not connected.")
            return

        log(f"--> [DECISIVE WAKE] ({reason})")
        self.state = "awake"
        self.tv_commanded_off = False
        self.offline_streak = 0

        # 1. Respect shutdown settling buffer if recently put to sleep
        elapsed = time.time() - self.last_off_time
        if elapsed < 3.0:
            wait_time = 3.0 - elapsed
            log(f"[*] Wake requested {elapsed:.2f}s after sleep. Waiting {wait_time:.2f}s for TV shutdown cycle...")
            time.sleep(wait_time)

        if self.abort_wake or self.read_external_display_status() != "connected":
            log("[*] Aborting wake after settle wait because external display disconnected.")
            return

        # 2. Turn on KDE DPMS
        run_cmd_async("kscreen-doctor --dpms on")
        ext_conn = self.get_external_connector_name()

        # 3. Check if TV is already confirmed awake
        tv_state = get_tv_power_state()
        if tv_state in ("on", "to-on"):
            log(f"[*] TV is already '{tv_state}'. Re-asserting HDMI active source and display mode...")
            run_cmd_async(f"kscreen-doctor output.{ext_conn}.mode.1")
            run_cmd_async("cec-ctl -d /dev/cec0 --playback && cec-ctl -d /dev/cec0 --from 4 --to 0 --image-view-on && cec-ctl -d /dev/cec0 --from 4 --active-source phys-addr=1.0.0.0")
            return

        # 4. Broadcast rapid Wake-on-LAN bursts across all NICs (5 bursts for deep standby wake)
        log("Broadcasting rapid Wake-on-LAN burst (5 packets) across all NICs...")
        send_wol(TV_MAC, bursts=5, delay_between=0.05)

        # 5. Transmit HDMI-CEC IMAGE_VIEW_ON + ACTIVE_SOURCE (phys-addr=1.0.0.0)
        run_cmd_async("cec-ctl -d /dev/cec0 --playback && cec-ctl -d /dev/cec0 --from 4 --to 0 --image-view-on && cec-ctl -d /dev/cec0 --from 4 --active-source phys-addr=1.0.0.0")

        # 6. Verification and retry loop for deep standby (waiting for WoL / hardware boot)
        for attempt in range(1, 11):
            if self.abort_wake or self.read_external_display_status() != "connected":
                log("[*] External display disconnected during wake loop; aborting wake.")
                return
            log(f"Wake attempt {attempt}/10: Waiting for TV to wake via WoL/CEC...")
            time.sleep(1.5)
            if self.abort_wake or self.read_external_display_status() != "connected":
                log("[*] External display disconnected during wake loop; aborting wake.")
                return
            current_state = get_tv_power_state()
            if current_state in ("on", "to-on"):
                log(f"[*] TV confirmed awake ({current_state}) on attempt {attempt}. Syncing video stream and active source...")
                run_cmd_async(f"kscreen-doctor output.{ext_conn}.mode.1")
                run_cmd_async("cec-ctl -d /dev/cec0 --playback && cec-ctl -d /dev/cec0 --from 4 --to 0 --image-view-on && cec-ctl -d /dev/cec0 --from 4 --active-source phys-addr=1.0.0.0")
                return
            send_wol(TV_MAC, bursts=4, delay_between=0.05)
            run_cmd_async("cec-ctl -d /dev/cec0 --playback && cec-ctl -d /dev/cec0 --from 4 --to 0 --image-view-on && cec-ctl -d /dev/cec0 --from 4 --active-source phys-addr=1.0.0.0")

        log("[!] Decisive wake sequence did not detect TV awake after 10 attempts. Backing off for 60s.")
        self.wake_backoff_until = time.time() + 60.0

    def handle_screen_off(self, reason: str):
        if self.state == "asleep":
            return
        log(f"--> [OFF DETECTED] ({reason})")
        self.state = "asleep"
        self.abort_wake = True

        # If displays are turning off due to idle DPMS, automatically lock session
        if "DPMS" in reason:
            log("[*] Locking session for idle DPMS display turn-off (autolock).")
            run_cmd_async("loginctl lock-session")

        elapsed_since_off = time.time() - self.last_off_time
        if self.tv_commanded_off and elapsed_since_off < 12.0:
            log(f"[*] TV was already commanded off {elapsed_since_off:.1f}s ago. Skipping power toggle.")
            return

        is_connected = (self.read_external_display_status() == "connected")

        # Check if TV is already in standby before sending toggle key.
        tv_state = get_tv_power_state(use_cec=is_connected)
        if tv_state in ("standby", "to-standby"):
            log(f"[*] TV is already in {tv_state}. Skipping power toggle.")
            self.tv_commanded_off = True
            self.last_off_time = time.time()
            return

        # If external display is connected, send CEC standby command
        if is_connected:
            run_cmd_async("cec-ctl -d /dev/cec0 --playback --to 0 --standby")

        # Send WebSocket sleep over Wi-Fi
        if is_tv_network_awake(timeout=0.6):
            if send_tv_websocket_key("KEY_POWER"):
                self.tv_commanded_off = True
        self.last_off_time = time.time()

    def handle_screen_on(self, reason: str):
        if self.state == "awake":
            return

        log(f"--> [ON DETECTED] ({reason})")
        self.state = "awake"

        # Always ensure internal DPMS is on
        run_cmd_async("kscreen-doctor --dpms on")

        # If external display is connected, decisively ensure the TV wakes up
        if self.read_external_display_status() == "connected":
            self.trigger_decisive_wake(reason)
        else:
            log("[*] External display not connected; skipping TV wake.")

    def on_screensaver_active_changed(self, active: bool):
        if active:
            log("KDE ScreenSaver ActiveChanged: TRUE (locked)")
            self.is_locked = True
            # Fast DPMS polling (250ms) to detect Escape key or prompt sleep
            self.update_dpms_timer(interval_ms=250)
        else:
            log("KDE ScreenSaver ActiveChanged: FALSE (unlocked)")
            # Slow DPMS polling (2000ms) while user is working
            self.update_dpms_timer(interval_ms=2000)
            if getattr(self, "is_locked", False):
                self.is_locked = False
                self.handle_screen_on("KDE ScreenSaver Unlocked")
            else:
                log("[*] Session already unlocked; ignoring duplicate ActiveChanged: FALSE.")

    def on_prepare_for_sleep(self, sleeping: bool):
        if sleeping:
            self.handle_screen_off("systemd PrepareForSleep: suspending")
        else:
            # Re-check external display transition immediately on resume
            self.check_external_display_transition("Resume from sleep")
            self.handle_screen_on("systemd PrepareForSleep: resumed")

def main():
    log("Starting Samsung TV Power Synchronization Daemon (Event-Driven)...")

    DBusGMainLoop(set_as_default=True)
    daemon = TVPowerSyncDaemon()

    # Register D-Bus signal listeners
    try:
        session_bus = dbus.SessionBus()
        session_bus.add_signal_receiver(
            daemon.on_screensaver_active_changed,
            signal_name="ActiveChanged",
            dbus_interface="org.freedesktop.ScreenSaver",
            path="/org/freedesktop/ScreenSaver"
        )
        log("D-Bus Session Bus signal listener registered.")
    except Exception as e:
        log(f"Notice: SessionBus listener: {e}")

    try:
        system_bus = dbus.SystemBus()
        system_bus.add_signal_receiver(
            daemon.on_prepare_for_sleep,
            signal_name="PrepareForSleep",
            dbus_interface="org.freedesktop.login1.Manager",
            bus_name="org.freedesktop.login1"
        )
        # Register listener for login1 hardware switch properties (LidClosed)
        system_bus.add_signal_receiver(
            daemon.on_login1_properties_changed,
            signal_name="PropertiesChanged",
            dbus_interface="org.freedesktop.DBus.Properties",
            path="/org/freedesktop/login1"
        )
        log("D-Bus System Bus signal listeners registered (PrepareForSleep, LidClosed).")
    except Exception as e:
        log(f"Notice: SystemBus listener: {e}")

    loop = GLib.MainLoop()

    def sig_handler(sig, frame):
        log("Termination signal received. Exiting daemon.")
        loop.quit()

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    log("Daemon is active. Monitoring hardware DPMS, kernel uevents, and D-Bus signals.")
    loop.run()

if __name__ == "__main__":
    main()
