#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""
pair_vizio_tv.py - Pair with a Vizio SmartCast TV.

The Vizio SmartCast API requires a durable auth token. Start this script while
the TV is on and on the same network; it displays a PIN on the TV. The token is
written to VIZIO_TOKEN_FILE for tv_backends.VizioBackend.

Older Vizio firmware listens on port 9000 instead of 7345. Set
TV_VIZIO_PORT=9000 if the default port cannot be reached.

Configuration: TV_IP, TV_VIZIO_PORT, VIZIO_TOKEN_FILE.
"""

import asyncio
import os
import sys

import tvlib


def _require_library():
    try:
        import vizaio
    except ImportError:
        print("Error: vizaio is not installed.")
        print("Install it with: pip install vizaio")
        return None
    return vizaio


def _save_token(path: str, token: str) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(token.strip() + "\n")
    os.chmod(path, 0o600)


async def pair(config: tvlib.Config, module) -> bool:
    host = f"{config.tv_ip}:{config.vizio_port}"
    print(f"[*] Connecting to Vizio SmartCast TV at {host}...")

    try:
        async with module.Vizio(
            host,
            device_type=module.DeviceType.TV,
            timeout=10.0,
            command_timeout=15.0,
        ) as device:
            try:
                challenge = await device.begin_pair(
                    device_id=tvlib.VIZIO_DEVICE_ID,
                    device_name="TVPowerSync",
                )
            except module.VizioConnectionError as exc:
                print(f"[!] Could not reach the SmartCast API: {exc}")
                if config.vizio_port != 9000:
                    print("    Older firmware may use port 9000; retry with:")
                    print("    TV_VIZIO_PORT=9000 ./pair_vizio_tv.py")
                return False

            print("[*] LOOK AT THE TV SCREEN for the pairing PIN.")
            token = None
            for attempt in range(1, 4):
                pin = input("Enter the PIN shown on the TV: ").strip()
                try:
                    token = await device.finish_pair(
                        device_id=tvlib.VIZIO_DEVICE_ID,
                        challenge=challenge,
                        pin=pin,
                    )
                    break
                except module.VizioAuthError:
                    print(f"[!] Invalid PIN (attempt {attempt}/3).")
            if not token:
                print("[!] Pairing failed.")
                return False

            _save_token(config.vizio_token_file, token)
            print("[+] Paired successfully.")
            print(f"    Auth token: {config.vizio_token_file}")
            return True
    except Exception as exc:
        print(f"[!] Pairing failed: {type(exc).__name__}: {exc}")
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
