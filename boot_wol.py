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

def send_wol(mac=TV_MAC):
    mac_clean = mac.replace(":", "").replace("-", "")
    mac_bytes = bytes.fromhex(mac_clean)
    packet = b"\xff" * 6 + mac_bytes * 16

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        # Send a burst of gratuitous packets across broadcast targets
        for _ in range(3):
            for target in ("255.255.255.255", "192.168.0.255", TV_IP):
                try:
                    s.sendto(packet, (target, 9))
                    s.sendto(packet, (target, 7))
                except Exception:
                    pass
            time.sleep(0.05)
    print(f"[*] Gratuitous Wake-on-LAN packets broadcasted to {mac}")

def main():
    if not is_tv_connected():
        print("[*] TV/external display is not connected. Skipping boot wake.")
        return

    print("[*] External display connection detected on DRM port.")
    send_wol(TV_MAC)

    # If HDMI-CEC adapter exists, also issue CEC wake commands
    if os.path.exists(CEC_DEVICE):
        print(f"[*] Issuing HDMI-CEC wake commands via {CEC_DEVICE}...")
        subprocess.run(f"cec-ctl -d {CEC_DEVICE} --playback --to 0 --image-view-on", shell=True, capture_output=True)
        subprocess.run(f"cec-ctl -d {CEC_DEVICE} --playback --active-source phys-addr=1.0.0.0", shell=True, capture_output=True)

    print("[*] Boot wake sequence completed.")

if __name__ == "__main__":
    main()
