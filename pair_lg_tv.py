#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""
pair_lg_tv.py - Pair with an LG webOS TV.

The LG webOS WebSocket API requires a one-time client key. Run this script
while the TV is on and on the same network, then accept the prompt on the TV.
The key is written to LG_CLIENT_KEY_FILE for tv_backends.WebOsBackend.

Configuration: TV_IP, LG_CLIENT_KEY_FILE.
"""

import asyncio
import os
import sys

import tvlib


def _require_library():
    try:
        import aiowebostv
    except ImportError:
        print("Error: aiowebostv is not installed.")
        print("Install it with: pip install aiowebostv")
        return None
    return aiowebostv


def _save_key(path: str, key: str) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(key.strip() + "\n")
    os.chmod(path, 0o600)


async def pair(config: tvlib.Config, module) -> bool:
    print(f"[*] Connecting to LG webOS TV at {config.tv_ip}...")
    print("[*] LOOK AT THE TV SCREEN and allow the TVPowerSync connection.")

    client = module.WebOsClient(config.tv_ip, None, connect_timeout=30)
    try:
        if not await client.connect():
            print("[!] Could not connect to the LG TV.")
            return False
        key = client.client_key
        if not key:
            print("[!] The TV did not return a client key.")
            return False
        _save_key(config.lg_client_key_file, key)
        print("[+] Paired successfully.")
        print(f"    Client key: {config.lg_client_key_file}")
        return True
    except Exception as exc:
        print(f"[!] Pairing failed: {type(exc).__name__}: {exc}")
        print("    Make sure the TV is on and the on-screen prompt was accepted.")
        return False
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
        try:
            await client.close_client_session()
        except Exception:
            pass


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
