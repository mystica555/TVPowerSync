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
import glob
import signal
import socket
import json
import base64
import subprocess
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

def send_wol(mac=TV_MAC):
    mac_clean = mac.replace(":", "").replace("-", "")
    mac_bytes = bytes.fromhex(mac_clean)
    packet = b"\xff" * 6 + mac_bytes * 16

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for target in ("255.255.255.255", "192.168.0.255", TV_IP):
            try:
                s.sendto(packet, (target, 9))
                s.sendto(packet, (target, 7))
            except Exception:
                pass
    log(f"Wake-on-LAN packet broadcasted to {mac}")

def send_tv_websocket_key(key="KEY_POWEROFF"):
    """Send remote control key over Samsung TV WebSocket interface."""
    if ws_connect is None:
        log("Notice: websockets library not available for WebSocket key transmission.")
        return False
    name_b64 = base64.b64encode(b"Laptop-KDE").decode()
    url = f"ws://{TV_IP}:8001/api/v2/channels/samsung.remote.control?name={name_b64}"
    payload = json.dumps({
        "method": "ms.remote.control",
        "params": {
            "Cmd": "Click",
            "DataOfCmd": key,
            "Option": "false",
            "TypeOfRemote": "SendRemoteKey"
        }
    })
    try:
        with ws_connect(url, open_timeout=1.5, close_timeout=1.0) as ws:
            ws.send(payload)
            log(f"WebSocket remote keystroke '{key}' sent to {url}")
            return True
    except Exception as e:
        log(f"WebSocket send ({key}) error: {e}")
        return False

def run_cmd(cmd: str):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)

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

        # 1. Send WebSocket keystroke packet to TV interface
        send_tv_websocket_key("KEY_POWEROFF")
        send_tv_websocket_key("KEY_POWER")

        # 2. Send HDMI-CEC Standby command so TV doesn't hang on 'No Signal'
        res = run_cmd(f"sudo cec-ctl -d {CEC_DEVICE} --playback --to 0 --standby")
        if res.returncode == 0:
            log("HDMI-CEC Standby command sent successfully.")
        else:
            log(f"HDMI-CEC Standby notice: code {res.returncode}")

    def handle_screen_on(self, reason: str):
        if self.state == "awake":
            return
        log(f"--> [ON DETECTED] ({reason})")
        self.state = "awake"

        # 1. Turn on KDE DPMS so video signal is clocked
        run_cmd("kscreen-doctor --dpms on")

        # 2. Send Wake-on-LAN Magic Packet to power on the TV
        send_wol(TV_MAC)

        # 3. Send HDMI-CEC Image View On & Active Source
        run_cmd(f"sudo cec-ctl -d {CEC_DEVICE} --playback --to 0 --image-view-on")
        run_cmd(f"sudo cec-ctl -d {CEC_DEVICE} --playback --active-source phys-addr=1.0.0.0")
        log("HDMI-CEC Image View On & Active Source sent.")

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
