#!/usr/bin/env python3
"""
boot_wol.py - Gratuitous Wake-on-LAN and HDMI-CEC wake on boot.
Checks if external TV/display is connected (/sys/class/drm/*-DP-*/status or HDMI).
If connected, sends gratuitous WoL packets and HDMI-CEC wake before graphical login.
"""

import sys
import os
import glob
import time
import socket
import subprocess

TV_IP = "192.168.0.2"
TV_MAC = "40:16:3b:d0:4f:8b"
CEC_DEVICE = "/dev/cec0"

def is_tv_connected() -> tuple[bool, str]:
    """Check if TV/adapter is physically plugged in or on home network."""
    # 1. Check if DRM reports connected
    for p in glob.glob("/sys/class/drm/*-DP-*/status") + glob.glob("/sys/class/drm/*-HDMI-*/status"):
        try:
            with open(p, "r") as f:
                if f.read().strip() == "connected":
                    return True, f"DRM port connected ({p})"
        except Exception:
            pass

    # 2. Check if USB-C dock / DisplayPort adapter or hub is present
    for p in glob.glob("/sys/bus/usb/devices/*/idVendor"):
        try:
            with open(p, "r") as f:
                vid = f.read().strip().lower()
                # 2109 = VIA Labs (dock/hub/billboard), 0bda = Realtek dock ethernet
                if vid in ("2109", "0bda"):
                    return True, f"USB display adapter/dock detected (vendor ID {vid})"
        except Exception:
            pass

    # 3. Check if local network is on 192.168.0.x (home network)
    try:
        out = subprocess.run(["ip", "-o", "-4", "addr", "show"], capture_output=True, text=True).stdout
        if "192.168.0." in out:
            return True, "Home network detected (192.168.0.x)"
    except Exception:
        pass

    return False, "No external display, dock, or home network detected"

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

    print(f"[*] Gratuitous Wake-on-LAN packets sent across all NICs to {mac} ({bursts} bursts)")
    return True

def is_laptop_display_off() -> bool:
    """Check if laptop screen is in hardware DPMS Off state (sleeping/locked)."""
    for p in glob.glob("/sys/class/drm/*-eDP-*/dpms"):
        try:
            with open(p, "r") as f:
                if f.read().strip() == "Off":
                    return True
        except Exception:
            pass
    return False

def main():
    if is_laptop_display_off():
        print("[*] Laptop display is in hardware DPMS Off (sleeping). Skipping boot wake.")
        return

    connected, reason = is_tv_connected()
    if not connected:
        print(f"[*] Skipping boot wake: {reason}")
        return

    print(f"[*] Boot wake triggered: {reason}")
    print("[*] Sending rapid Wake-on-LAN burst (3 packets, 50ms interval) across all NICs...")
    send_wol(TV_MAC, bursts=3, delay_between=0.05)

    if os.path.exists(CEC_DEVICE):
        res = subprocess.run(f"cec-ctl -d {CEC_DEVICE} --playback --to 0 --image-view-on --active-source phys-addr=1.0.0.0", shell=True, capture_output=True, text=True)
        if res.returncode == 0:
            print("[*] Sent HDMI-CEC IMAGE_VIEW_ON + ACTIVE_SOURCE to TV.")
        else:
            print(f"[!] CEC transmission returned code {res.returncode}")

    print("[*] Boot wake sequence completed.")

if __name__ == "__main__":
    main()
