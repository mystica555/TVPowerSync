#!/usr/bin/env python3
"""
pair_android_tv.py - Pair with an Android TV / Google TV device.

The Android TV Remote protocol v2 requires a client certificate and a one-time
on-screen pairing code. This script generates the certificate if needed, asks
the device to start pairing, and saves the certificate/key so
tv_backends.AndroidTvBackend can connect.

Steps:
1. Power on the Android TV and connect it to the same network.
2. Run this script.
3. Enter the 6-digit code shown on the TV.

Configuration: TV_IP, ANDROIDTV_CERT, ANDROIDTV_KEY, ANDROIDTV_CLIENT_NAME.
"""

import asyncio
import os
import sys

import tvlib


def _require_library():
    try:
        import androidtvremote2
    except ImportError:
        print("Error: androidtvremote2 is not installed.")
        print("Install it with: pip install androidtvremote2")
        return None
    return androidtvremote2


async def pair(config: tvlib.Config, module) -> bool:
    for path in (config.androidtv_cert, config.androidtv_key):
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)

    remote = module.AndroidTVRemote(
        config.androidtv_client_name,
        config.androidtv_cert,
        config.androidtv_key,
        config.tv_ip,
    )
    if await remote.async_generate_cert_if_missing():
        print(f"[*] Generated client certificate at {config.androidtv_cert}")

    print(f"[*] Starting pairing with {config.tv_ip}...")
    print("[*] LOOK AT THE TV SCREEN for the pairing code.")
    try:
        await remote.async_start_pairing()
    except module.CannotConnect as exc:
        print(f"[!] Could not reach Android TV at {config.tv_ip}: {exc}")
        return False
    except module.ConnectionClosed as exc:
        print(f"[!] Pairing connection closed: {exc}")
        return False

    for attempt in range(1, 4):
        code = input("Enter the 6-digit code shown on the TV: ").strip()
        try:
            await remote.async_finish_pairing(code)
        except module.InvalidAuth:
            print(f"[!] Invalid pairing code (attempt {attempt}/3).")
            continue
        except module.ConnectionClosed:
            print("[!] Pairing connection closed; run the script again.")
            return False
        print("[+] Paired successfully.")
        print(f"    Certificate: {config.androidtv_cert}")
        print(f"    Key:         {config.androidtv_key}")
        return True
    return False


def main() -> int:
    tvlib.setup_logging()
    try:
        config = tvlib.Config.from_env()
    except ValueError as exc:
        print(f"[!] Invalid configuration: {exc}")
        return 2

    module = _require_library()
    if module is None:
        return 1

    print(__doc__.strip())
    print("-" * 60)
    return 0 if asyncio.run(pair(config, module)) else 1


if __name__ == "__main__":
    sys.exit(main())
