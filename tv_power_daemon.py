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


def get_tv_power_state() -> str:
    """Uses cec-ctl to get the current power state of the TV.
    Returns 'on', 'standby', 'to-on', 'to-standby', or 'offline'.
    """
    try:
        import subprocess
        output = subprocess.check_output(
            ["cec-ctl", "-d", "/dev/cec0", "--playback", "--to", "0", "--give-device-power-status"],
            stderr=subprocess.STDOUT, timeout=1.0
        ).decode()
        for line in output.splitlines():
            if "pwr-state:" in line:
                return line.split(":")[1].split("(")[0].strip()
        return "offline"
    except Exception:
        return "offline"

class UserActivityTracker:
    """Monitors Linux input subsystem (/dev/input/mice and keyboards) in real time
    to detect genuine physical user interactions vs synthetic KDE display events.
    """
    def __init__(self):
        self.last_input_time = 0.0
        self._running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        fds = {}
        try:
            fd = os.open("/dev/input/mice", os.O_RDONLY | os.O_NONBLOCK)
            fds[fd] = "mice"
        except Exception:
            pass

        for p in glob.glob("/dev/input/by-path/*-kbd"):
            try:
                fd = os.open(p, os.O_RDONLY | os.O_NONBLOCK)
                fds[fd] = p
            except Exception:
                pass

        while self._running:
            try:
                if not fds:
                    time.sleep(1.0)
                    continue
                r, _, _ = select.select(list(fds.keys()), [], [], 0.5)
                now = time.time()
                for fd in r:
                    try:
                        os.read(fd, 64)
                        self.last_input_time = now
                    except Exception:
                        pass
            except Exception:
                time.sleep(0.5)


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
        self.is_locked = False
        self.tracker = UserActivityTracker()
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

        # If displays are turning off due to idle DPMS, automatically lock session
        if "DPMS" in reason:
            log("[*] Locking session for idle DPMS display turn-off (autolock).")
            run_cmd_async("loginctl lock-session")

        # Check if TV is already in standby before sending toggle key.
        # This prevents accidental wake if the TV is already powered off.
        tv_state = get_tv_power_state()
        if tv_state in ("standby", "to-standby"):
            log(f"[*] TV is already in {tv_state}. Skipping WebSocket power toggle.")
            self.last_off_time = time.time()
            return

        send_tv_websocket_key("KEY_POWER")
        # Record last_off_time AFTER transmission so shutdown settling is accurately measured
        self.last_off_time = time.time()

    def handle_screen_on(self, reason: str):
        if self.state == "awake":
            return

        log(f"--> [ON DETECTED] ({reason})")
        self.state = "awake"

        # 1. The Samsung TV takes ~3.0s from command delivery to complete its internal shutdown.
        # If the user wakes the screen too soon, wait out the remaining shutdown window
        # so the wake keystroke is not dropped by the busy TV micro-controller.
        if hasattr(self, "last_off_time"):
            elapsed = time.time() - self.last_off_time
            if elapsed < 3.0:
                wait_time = 3.0 - elapsed
                log(f"[*] Wake requested {elapsed:.2f}s after sleep. Waiting {wait_time:.2f}s for TV shutdown cycle to complete...")
                time.sleep(wait_time)

        # 2. Turn on KDE DPMS asynchronously so it doesn't delay network commands
        run_cmd_async("kscreen-doctor --dpms on")

        # 3. Transmit HDMI-CEC IMAGE_VIEW_ON in parallel to trigger direct hardware display wake
        run_cmd_async("cec-ctl -d /dev/cec0 --playback --to 0 --image-view-on")

        # 4. Instantly send WoL across all NICs (non-blocking, prepares deep standby wake)
        log("Broadcasting rapid Wake-on-LAN burst (3 packets) across all NICs...")
        send_wol(TV_MAC, bursts=3, delay_between=0.05)

        # 3. Try websocket KEY_POWER immediately (instant wake from shallow standby)
        if send_tv_websocket_key("KEY_POWER"):
            log("[*] TV woken via WebSocket KEY_POWER (fast path).")
            return

        # 5. Fallback loop for deep standby (waiting for WoL to wake it)
        # We do NOT send KEY_POWER in this loop to avoid toggling off the waking TV!
        for attempt in range(1, 11):
            log(f"Wake attempt {attempt}/10: Waiting for TV to wake via WoL...")
            time.sleep(1.5)
            if get_tv_power_state() in ("on", "to-on"):
                log(f"[*] TV confirmed awake via CEC on attempt {attempt}.")
                return
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
                # Check for spurious TV-disconnect HDMI bounce (occurs within 6s of turning off TV)
                if self.state == "asleep" and hasattr(self, "last_off_time") and (time.time() - self.last_off_time < 6.0):
                    # Check if genuine user mouse/keyboard input was detected
                    time_since_input = time.time() - self.tracker.last_input_time
                    if time_since_input > 1.0:
                        log(f"[*] Spurious DPMS On without user input ({time_since_input:.1f}s since last input). Forcing display back to sleep.")
                        run_cmd_async("kscreen-doctor --dpms off")
                        return True
                self.handle_screen_on("Laptop display DPMS went On (hardware)")
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
