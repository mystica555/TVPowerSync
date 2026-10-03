#!/usr/bin/env python3
"""
tv_power_daemon.py - Synchronize Samsung TV power with laptop display state
Monitors:
  1. Real-time hardware DPMS of internal display (/sys/class/drm/*-eDP-*/dpms)
     Detects hitting Escape at login prompt, idle display sleep, etc.
  2. D-Bus KDE ScreenSaver ActiveChanged signal (screen lock/unlock)
  3. D-Bus systemd login1 PrepareForSleep signal (system suspend/resume)

Actions:
  - When laptop display turns OFF:
      1. Transmit WebSocket KEY_POWEROFF / KEY_POWER keystroke packet to TV
      2. Issue HDMI-CEC Standby command so TV doesn't idle on 'No Signal'
  - When laptop display turns ON:
      1. Broadcast Wake-on-LAN Magic Packet to TV MAC (40:16:3b:d0:4f:8b)
      2. Send HDMI-CEC Image View On & Active Source
      3. Enable DPMS via kscreen-doctor
"""

import sys
import os
# Add venv to sys.path so we can import samsungtvws without breaking system dbus
sys.path.append("/home/mike/src/drmstuff/venv/lib/python3.14/site-packages")
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
        # Initialize with 2.0s timeout (uses port 8001 by default)
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


def is_tv_network_awake(timeout=0.4) -> bool:
    """Check if TV network port 8001 is open (listening)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            return s.connect_ex((TV_IP, 8001)) == 0
    except Exception:
        return False

def send_tv_sleep_wifi(reason: str):
    """Put TV to sleep via Wi-Fi/WebSocket (e.g. when undocked or lid closed)."""
    def _worker():
        log(f"[*] Sending Wi-Fi sleep command to TV ({reason})...")
        tv_state = get_tv_power_state()
        if tv_state in ("standby", "to-standby"):
            log(f"[*] TV is already in {tv_state}. Skipping Wi-Fi sleep.")
            return
        if is_tv_network_awake(timeout=0.4):
            send_tv_websocket_key("KEY_POWER")
    threading.Thread(target=_worker, daemon=True).start()

def get_tv_power_state() -> str:
    """Uses cec-ctl to get the current power state of the TV, falling back to network reachability.
    Returns 'on', 'standby', 'to-on', 'to-standby', or 'offline'.
    """
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
    # When the TV is ON, port 8001 responds immediately (< 1ms).
    # When the TV is in standby, port 8001 is closed / unreachable.
    if is_tv_network_awake(timeout=0.3):
        return "on"

    return "offline"

class TVPowerSyncDaemon:

    def __init__(self):
        # Locate laptop internal display dpms sysfs node
        candidates = glob.glob("/sys/class/drm/*-eDP-*/dpms")
        self.edp_dpms_path = candidates[0] if candidates else "/sys/class/drm/card1-eDP-1/dpms"
        log(f"Monitoring hardware display power at: {self.edp_dpms_path}")

        self.last_dp_status = self.read_external_display_status()
        self.last_lid_state = self.read_lid_state()
        self.wake_in_progress = False
        self.wake_backoff_until = 0.0
        self.poll_counter = 0

        # Initial state
        initial_hw = self.read_hardware_dpms()
        self.last_hw_dpms = initial_hw
        self.is_locked = False
        log(f"Initial display power state: (hardware: {initial_hw}, external DP/HDMI: {self.last_dp_status}, lid: {self.last_lid_state})")

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

    def read_hardware_dpms(self) -> str:
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

    def read_lid_state(self) -> str:
        """Check if laptop lid is physically closed or open."""
        for p in glob.glob("/proc/acpi/button/lid/*/state"):
            try:
                with open(p, "r") as f:
                    if "closed" in f.read().lower():
                        return "closed"
            except Exception:
                pass
        return "open"

    def trigger_decisive_wake(self, reason: str):
        """Spawns non-blocking worker thread to decisively wake the TV."""
        if self.wake_in_progress:
            log(f"[*] Wake sequence already running; ignoring duplicate trigger ({reason}).")
            return
        if time.time() < self.wake_backoff_until:
            log(f"[*] Wake backed off for another {int(self.wake_backoff_until - time.time())}s ({reason}).")
            return

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
        log(f"--> [DECISIVE WAKE] ({reason})")
        self.state = "awake"

        # 1. Respect shutdown settling buffer if recently put to sleep
        if hasattr(self, "last_off_time"):
            elapsed = time.time() - self.last_off_time
            if elapsed < 3.0:
                wait_time = 3.0 - elapsed
                log(f"[*] Wake requested {elapsed:.2f}s after sleep. Waiting {wait_time:.2f}s for TV shutdown cycle...")
                time.sleep(wait_time)

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
        # Note: Both WoL and IMAGE_VIEW_ON are completely discrete commands.
        # We do NOT send WebSocket KEY_POWER in the wake sequence to prevent accidental power inversion.
        run_cmd_async("cec-ctl -d /dev/cec0 --playback && cec-ctl -d /dev/cec0 --from 4 --to 0 --image-view-on && cec-ctl -d /dev/cec0 --from 4 --active-source phys-addr=1.0.0.0")

        # 6. Verification and retry loop for deep standby (waiting for WoL / hardware boot)
        for attempt in range(1, 11):
            log(f"Wake attempt {attempt}/10: Waiting for TV to wake via WoL/CEC...")
            time.sleep(1.5)
            current_state = get_tv_power_state()
            if current_state in ("on", "to-on"):
                log(f"[*] TV confirmed awake ({current_state}) on attempt {attempt}. Syncing video stream and active source...")
                # Re-assert mode on external display NOW that the TV HDMI sink is awake and ready
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

        # If displays are turning off due to idle DPMS, automatically lock session
        if "DPMS" in reason:
            log("[*] Locking session for idle DPMS display turn-off (autolock).")
            run_cmd_async("loginctl lock-session")

        # Check if TV is already in standby before sending toggle key.
        tv_state = get_tv_power_state()
        if tv_state in ("standby", "to-standby"):
            log(f"[*] TV is already in {tv_state}. Skipping power toggle.")
            self.last_off_time = time.time()
            return

        # If external display is connected, send CEC standby command
        if self.read_external_display_status() == "connected":
            run_cmd_async("cec-ctl -d /dev/cec0 --playback --to 0 --standby")

        # Send WebSocket sleep over Wi-Fi
        if is_tv_network_awake(timeout=0.3):
            send_tv_websocket_key("KEY_POWER")
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

    def poll_hardware_dpms(self) -> bool:
        """Periodic fast poll of hardware DPMS, external display, lid, and TV state (every 150ms)."""
        # 1. Hardware DPMS of laptop internal panel
        current_hw = self.read_hardware_dpms()
        if current_hw != self.last_hw_dpms:
            log(f"Hardware DPMS transition: {self.last_hw_dpms} -> {current_hw}")
            self.last_hw_dpms = current_hw
            if current_hw == "Off":
                self.handle_screen_off("Laptop display DPMS went Off (hardware)")
            elif current_hw == "On":
                self.handle_screen_on("Laptop display DPMS went On (hardware)")

        # 2. External display status (dock connection)
        current_dp = self.read_external_display_status()
        if current_dp != self.last_dp_status:
            log(f"External display transition: {self.last_dp_status} -> {current_dp}")
            old_dp = self.last_dp_status
            self.last_dp_status = current_dp
            self.wake_backoff_until = 0.0  # Clear backoff on hardware change

            if current_dp == "connected":
                log("[*] External TV plugged in (dock connected). Decisively waking TV...")
                self.trigger_decisive_wake("Dock connected")
            elif current_dp == "disconnected" and old_dp == "connected":
                log("[*] External TV unplugged (undocked). Sending sleep command to TV via Wi-Fi...")
                send_tv_sleep_wifi("Dock disconnected")

        # 3. Laptop lid state
        current_lid = self.read_lid_state()
        if current_lid != self.last_lid_state:
            log(f"Laptop lid transition: {self.last_lid_state} -> {current_lid}")
            self.last_lid_state = current_lid
            if current_lid == "closed":
                log("[*] Laptop lid closed. Putting TV to sleep...")
                if current_dp == "connected":
                    run_cmd_async("cec-ctl -d /dev/cec0 --playback --to 0 --standby")
                send_tv_sleep_wifi("Lid closed")
                self.state = "asleep"
            elif current_lid == "open":
                log("[*] Laptop lid opened. Ensuring displays are awake...")
                if current_dp == "connected":
                    self.trigger_decisive_wake("Lid opened")

        # 4. Periodic Watchdog (every ~3 seconds / 20 ticks):
        # "make certain the tv is awake if im logged in and the dp>hdmi is connected"
        self.poll_counter += 1
        if self.poll_counter >= 20:
            self.poll_counter = 0
            if (self.state == "awake" and
                current_hw == "On" and
                current_dp == "connected" and
                current_lid == "open" and
                not self.wake_in_progress and
                time.time() >= self.wake_backoff_until):
                tv_state = get_tv_power_state()
                if tv_state in ("standby", "to-standby", "offline"):
                    log(f"[!] Watchdog check: Logged in and dock connected, but TV is in '{tv_state}'. Decisively waking TV...")
                    self.trigger_decisive_wake("Watchdog - TV asleep while dock connected and logged in")

        return True  # Keep GLib timer active

    def on_screensaver_active_changed(self, active: bool):
        if active:
            log("KDE ScreenSaver ActiveChanged: TRUE (locked)")
            self.is_locked = True
        else:
            log("KDE ScreenSaver ActiveChanged: FALSE (unlocked)")
            if getattr(self, "is_locked", False):
                self.is_locked = False
                self.handle_screen_on("KDE ScreenSaver Unlocked")
            else:
                log("[*] Session already unlocked; ignoring duplicate ActiveChanged: FALSE.")

    def on_prepare_for_sleep(self, sleeping: bool):
        if sleeping:
            self.handle_screen_off("systemd PrepareForSleep: suspending")
        else:
            self.handle_screen_on("systemd PrepareForSleep: resumed")

def main():
    log("Starting Samsung TV Power Synchronization Daemon...")

    DBusGMainLoop(set_as_default=True)
    daemon = TVPowerSyncDaemon()

    # Register fast GLib timer (150ms) to track hardware display DPMS
    GLib.timeout_add(150, daemon.poll_hardware_dpms)

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
        log("D-Bus System Bus signal listener registered.")
    except Exception as e:
        log(f"Notice: SystemBus listener: {e}")

    loop = GLib.MainLoop()

    def sig_handler(sig, frame):
        log("Termination signal received. Exiting daemon.")
        loop.quit()

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    log("Daemon is active. Monitoring hardware DPMS and D-Bus signals.")
    loop.run()

if __name__ == "__main__":
    main()
