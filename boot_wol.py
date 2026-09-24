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

def is_tv_connected() -> bool:
    """Check if TV is physically plugged into a display port."""
    for p in glob.glob("/sys/class/drm/*-DP-*/status") + glob.glob("/sys/class/drm/*-HDMI-*/status"):
        try:
            with open(p, "r") as f:
                if f.read().strip() == "connected":
                    return True
        except Exception:
            pass
    return False

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

def main():
    if not is_tv_connected():
        print("[*] TV/external display is not connected. Skipping boot wake.")
        return

    print("[*] External display connection detected on DRM port.")
    print("[*] Sending rapid Wake-on-LAN burst (3 packets, 50ms interval) across all NICs...")
    send_wol(TV_MAC, bursts=3, delay_between=0.05)

    if os.path.exists(CEC_DEVICE):
        subprocess.run(f"cec-ctl -d {CEC_DEVICE} --playback --to 0 --image-view-on --active-source phys-addr=1.0.0.0", shell=True, capture_output=True)

    print("[*] Boot wake sequence completed.")

if __name__ == "__main__":
    main()
