#!/usr/bin/env python3
"""
boot_wol.py - Gratuitous Wake-on-LAN and backend wake at boot.

Runs once per boot (systemd oneshot). If an external display, a USB-C dock or
the home network is present and the laptop panel is not in DPMS Off, it wakes
the TV before the display manager starts.
"""

import glob
import logging
import sys

import tv_backends
import tvlib

log = logging.getLogger("tv.boot")

DOCK_USB_VENDORS = {"2109", "0bda"}  # VIA Labs hub/billboard, Realtek dock NIC


def dock_usb_present() -> str | None:
    for path in sorted(glob.glob("/sys/bus/usb/devices/*/idVendor")):
        try:
            with open(path, "r") as handle:
                vendor = handle.read().strip().lower()
        except OSError:
            continue
        if vendor in DOCK_USB_VENDORS:
            return vendor
    return None


def home_network_present() -> bool:
    return any(ip.startswith("192.168.0.") for ip, _brd in tvlib.local_ipv4_interfaces())


def is_tv_connected() -> tuple[bool, str]:
    if tvlib.external_display_connected():
        return True, "DRM external connector is connected"

    vendor = dock_usb_present()
    if vendor:
        return True, f"USB dock/adapter detected (vendor ID {vendor})"

    if home_network_present():
        return True, "Home network detected (192.168.0.x)"

    return False, "No external display, dock, or home network detected"


def main() -> int:
    tvlib.setup_logging()
    try:
        config = tvlib.Config.from_env()
        backend = tv_backends.create_backend(config)
    except ValueError as exc:
        log.error("Invalid configuration: %s", exc)
        return 2

    try:
        if tvlib.read_panel_dpms() == "Off":
            log.info(
                "Laptop display is in hardware DPMS Off (sleeping). "
                "Skipping boot wake."
            )
            return 0

        connected, reason = is_tv_connected()
        if not connected:
            log.info("Skipping boot wake: %s", reason)
            return 0

        log.info("Boot wake triggered: %s", reason)
        log.info("Sending wake sequence via the %s backend...", backend.name)
        if backend.wake():
            log.info("Wake sequence sent via %s.", backend.name)
        else:
            log.warning("[!] %s wake command failed.", backend.name)

        log.info("Boot wake sequence completed.")
    finally:
        backend.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
