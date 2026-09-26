#!/usr/bin/env python3
"""
control_tv.py - Comprehensive power control for Samsung TV (2016 KU7000 / Tizen)
Combines Wake-on-LAN (WoL), HDMI-CEC, and KDE KWin DPMS.

Usage:
    ./control_tv.py status      # Check DPMS mode and CEC TV power state
    ./control_tv.py wake        # Wake TV (WoL + CEC Image View On + DPMS On)
    ./control_tv.py standby     # Turn off TV (CEC Standby + DPMS Off)
    ./control_tv.py wol         # Send Wake-on-LAN magic packet only
"""

import sys
import os
import time
import socket
import subprocess

try:
    import dbus
except ImportError:
    dbus = None

TV_IP = "192.168.0.2"
TV_MAC = "40:16:3b:d0:4f:8b"
CEC_DEVICE = "/dev/cec0"

def send_wol(mac=TV_MAC, bursts=3, delay_between=0.05):
    """Broadcast Wake-on-LAN magic packets across all active network interfaces."""
    mac_clean = mac.replace(":", "").replace("-", "")
    if len(mac_clean) != 12:
        print(f"Error: Invalid MAC address {mac}")
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

    print(f"[*] Wake-on-LAN Magic Packet sent across all NICs to {mac} ({bursts} burst{'s' if bursts > 1 else ''})")
    return True

def run_cmd(cmd, check=False):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, check=check)

def get_status():
    print("=== Display Power Management (DPMS) ===")
    p = run_cmd("kscreen-doctor --dpms show")
    if p.stdout.strip():
        print(p.stdout.strip())
    else:
        print("DPMS query returned no output.")

    print("\n=== HDMI-CEC Power Status ===")
    p = run_cmd(f"cec-ctl -d {CEC_DEVICE} --playback --to 0 --give-device-power-status")
    for line in p.stdout.split("\n"):
        if "pwr-state:" in line or "Received from TV" in line:
            print(line.strip())

    print("\n=== KDE Screensaver Status (via Python D-Bus) ===")
    if dbus is not None:
        try:
            session_bus = dbus.SessionBus()
            ss_obj = session_bus.get_object("org.freedesktop.ScreenSaver", "/org/freedesktop/ScreenSaver")
            ss_iface = dbus.Interface(ss_obj, "org.freedesktop.ScreenSaver")
            is_active = bool(ss_iface.GetActive())
            state_str = "Active / Locked (Screen Sleeping)" if is_active else "Inactive / Unlocked (Screen Awake)"
            print(f"Screensaver: {state_str} (GetActive = {is_active})")
        except Exception as e:
            print(f"Screensaver: Query error via D-Bus: {e}")
    else:
        print("Screensaver: python-dbus module not available.")

    print("\n=== Network Reachability ===")
    # Quick probe to port 8001
    p = run_cmd(f"nc -zv -w 1 {TV_IP} 8001 2>&1")
    if "succeeded" in p.stdout:
        print(f"TV network interface ({TV_IP}:8001) is AWAKE and responding.")
    else:
        print(f"TV network interface ({TV_IP}:8001) is ASLEEP / unreachable.")

def get_tv_power_state() -> str:
    """Uses cec-ctl to get the current power state of the TV."""
    try:
        output = subprocess.check_output(
            ["cec-ctl", "-d", "/dev/cec0", "--playback", "--to", "0", "--give-device-power-status"],
            stderr=subprocess.STDOUT, timeout=3.0
        ).decode()
        for line in output.splitlines():
            if "pwr-state:" in line:
                return line.split(":")[1].split("(")[0].strip()
        return "offline"
    except Exception:
        return "offline"


def wake_tv():
    print("[*] Initiating TV Wake Sequence...")
    run_cmd("kscreen-doctor --dpms on")
    
    print("[*] Checking TV power state via CEC to prevent toggling it off...")
    if get_tv_power_state() in ("on", "to-on"):
        print("[*] TV is already awake (via CEC). Skipping WebSocket power toggle.")
        print("[*] TV Wake sequence completed.\n")
        get_status()
        return

    print("[*] Broadcasting rapid Wake-on-LAN burst (3 packets) across all NICs...")
    send_wol(TV_MAC, bursts=3, delay_between=0.05)

    from tv_power_daemon import send_tv_websocket_key
    
    if send_tv_websocket_key("KEY_HDMI"):
        print("[*] TV woken via WebSocket KEY_POWER (fast path).")
    else:
        for attempt in range(1, 11):
            print(f"[*] Wake attempt {attempt}/10: Checking WebSocket...")
            time.sleep(2.0)
            if get_tv_power_state() in ("on", "to-on"):
                print(f"[*] TV confirmed awake via CEC on attempt {attempt}.")
                break
            if send_tv_websocket_key("KEY_HDMI"):
                print(f"[*] TV woken via WebSocket KEY_POWER on attempt {attempt}.")
                break
            send_wol(TV_MAC, bursts=3, delay_between=0.05)
        else:
            print("[!] TV failed to wake after 10 WoL attempts.")

    print("[*] TV Wake sequence completed.\n")
    get_status()

def standby_tv():
    print("[*] Initiating TV Standby Sequence...")
    from tv_power_daemon import send_tv_websocket_key

    # Turn off DPMS (daemon will handle the KEY_POWER securely using CEC checks)
    run_cmd("kscreen-doctor --dpms off")
    
    print("[*] Waiting 4 seconds for TV network stack to spin down...")
    time.sleep(4)
    print("[*] TV Standby commands completed.\n")
    get_status()

def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__.strip())
        sys.exit(0)

    action = sys.argv[1].lower()
    if action == "status":
        get_status()
    elif action in ("wake", "on"):
        wake_tv()
    elif action in ("standby", "off", "sleep"):
        standby_tv()
    elif action == "wol":
        send_wol()
    else:
        print(f"Unknown action: {action}")
        print(__doc__.strip())
        sys.exit(1)

if __name__ == "__main__":
    main()
