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
        tv = SamsungTVWS(TV_IP, timeout=2.0)
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

class TVPowerSyncDaemon:
    def __init__(self):
        # Locate laptop internal display dpms sysfs node
        candidates = glob.glob("/sys/class/drm/*-eDP-*/dpms")
        self.edp_dpms_path = candidates[0] if candidates else "/sys/class/drm/card1-eDP-1/dpms"
        log(f"Monitoring hardware display power at: {self.edp_dpms_path}")

        # Initial state
        initial_hw = self.read_hardware_dpms()
        self.state = "awake" if initial_hw == "On" else "asleep"
        self.last_hw_dpms = initial_hw
        log(f"Initial display power state: {self.state.upper()} (hardware: {initial_hw})")

    def read_hardware_dpms(self) -> str:
        try:
            with open(self.edp_dpms_path, "r") as f:
                return f.read().strip()
        except Exception:
            return "Unknown"

    def handle_screen_off(self, reason: str):
        if self.state == "asleep":
            return
        log(f"--> [OFF DETECTED] ({reason})")
        self.state = "asleep"
        self.last_off_time = time.time()

        # Only send KEY_POWER via websocket after DPMS off.
        send_tv_websocket_key("KEY_POWER")

    def handle_screen_on(self, reason: str):
        if self.state == "awake":
            return

        # Ignore spurious wakes that happen within 10 seconds of turning off
        # (e.g. KDE reconfiguring screens when TV HDMI drops)
        if hasattr(self, "last_off_time") and time.time() - self.last_off_time < 10.0:
            log(f"--> [IGNORED WAKE] ({reason}) - Occurred within 10s of sleep")
            return

        log(f"--> [ON DETECTED] ({reason})")
        self.state = "awake"

        # 1. Turn on KDE DPMS so video signal is clocked immediately
        run_cmd("kscreen-doctor --dpms on")

        # 2. Check current power state via CEC to avoid toggling it OFF if it's already ON
        try:
            output = subprocess.check_output(
                ["cec-ctl", "-d", "/dev/cec0", "--playback", "--to", "0", "--give-device-power-status"],
                stderr=subprocess.STDOUT, timeout=2.0
            ).decode()
            for line in output.splitlines():
                if "pwr-state:" in line:
                    state = line.split(":")[1].split("(")[0].strip()
                    if state in ("on", "to-on"):
                        log("[*] TV is already awake (via CEC). Skipping WebSocket power toggle.")
                        return
                    break
        except Exception as e:
            log(f"CEC power state check failed: {e}")

        # 3. Instantly send WoL (non-blocking) in case it's in deep standby
        log("Broadcasting rapid Wake-on-LAN burst (3 packets) across all NICs...")
        send_wol(TV_MAC, bursts=3, delay_between=0.05)

        # 4. Try websocket KEY_POWER (instant wake if TV network is still up)
        if send_tv_websocket_key("KEY_POWER"):
            log("[*] TV woken via WebSocket KEY_POWER (fast path).")
            return

        # 5. Websocket failed — TV is in deep standby. WoL was already sent, just retry WebSocket to confirm.
        for attempt in range(1, 11):
            log(f"Wake attempt {attempt}/10: Checking WebSocket...")
            time.sleep(2.0)
            if send_tv_websocket_key("KEY_POWER"):
                log(f"[*] TV woken via WoL + WebSocket on attempt {attempt}.")
                return
            # Resend WoL just in case
            send_wol(TV_MAC, bursts=3, delay_between=0.05)
        log("[!] TV failed to wake after 10 WoL attempts.")

    def poll_hardware_dpms(self) -> bool:
        """Periodic fast poll of hardware DPMS sysfs node (every 150ms)."""
        current_hw = self.read_hardware_dpms()
        if current_hw != self.last_hw_dpms:
            log(f"Hardware DPMS transition: {self.last_hw_dpms} -> {current_hw}")
            self.last_hw_dpms = current_hw
            if current_hw == "Off":
                self.handle_screen_off("Laptop display DPMS went Off (hardware)")
            elif current_hw == "On":
                self.handle_screen_on("Laptop display DPMS went On (hardware)")
        return True  # Keep GLib timer active

    def on_screensaver_active_changed(self, active: bool):
        if active:
            # Screensaver locked/active; check if display is also turning off
            log("KDE ScreenSaver ActiveChanged: TRUE (locked)")
        else:
            log("KDE ScreenSaver ActiveChanged: FALSE (unlocked)")
            self.handle_screen_on("KDE ScreenSaver Unlocked")

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
