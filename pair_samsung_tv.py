#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""
pair_samsung_tv.py - Initiate WebSocket pairing with Samsung Smart TV (2016 KU7000)

Steps before running:
1. Make sure the TV is powered ON and on the local network (TV_IP).
2. On your TV, navigate to:
   Settings -> General -> External Device Manager -> Device Connection Manager
   - Access Notification: Set to 'First Time Only' or 'Always On'
   - Device List: Check and clear any old/denied entries if necessary
3. Run this script. An on-screen prompt "Allow connection from ...?" should
   appear on the TV.
4. Press 'Allow' using your TV remote.
"""

import base64
import asyncio
import json
import os
import ssl
import sys

try:
    import websockets
except ImportError:
    print("Error: websockets library not found in current environment.")
    sys.exit(1)

import tvlib

TOKEN_FILE = os.environ.get(
    "TV_TOKEN_FILE",
    os.path.expanduser("~/.local/state/tvpowersync/tv_token.txt"),
)


async def attempt_pairing(tv_ip: str, port: int = 8002) -> bool:
    use_ssl = port == 8002
    name_b64 = base64.b64encode(b"Laptop-KDE").decode("utf-8")
    scheme = "wss" if use_ssl else "ws"
    url = (
        f"{scheme}://{tv_ip}:{port}/api/v2/channels/samsung.remote.control"
        f"?name={name_b64}"
    )

    ssl_ctx = None
    if use_ssl:
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

    print(f"\n[*] Connecting to TV at {url}...")
    print("[*] LOOK AT YOUR TV SCREEN NOW for an 'Allow' permission prompt!")

    try:
        async with websockets.connect(url, ssl=ssl_ctx, open_timeout=15) as ws:
            print("[*] WebSocket connected! Waiting for TV response...")
            while True:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=30)
                    print(f"[TV Event] {msg}")
                    data = json.loads(msg)
                    if data.get("event") == "ms.channel.connect":
                        token = data.get("data", {}).get("token")
                        if token:
                            print(f"\n[+] SUCCESS! Received authentication token: {token}")
                            token_dir = os.path.dirname(TOKEN_FILE)
                            if token_dir:
                                os.makedirs(token_dir, exist_ok=True)
                            with open(TOKEN_FILE, "w") as handle:
                                handle.write(token)
                            print(f"[+] Token saved to {TOKEN_FILE}")
                            return True
                    elif data.get("event") == "ms.channel.unauthorized":
                        print("[-] Received ms.channel.unauthorized from TV.")
                        print("    If a prompt appeared on the TV, click 'Allow'.")
                        print("    If no prompt appeared, check Device Connection Manager.")
                except asyncio.TimeoutError:
                    print("[-] Timed out waiting for message.")
                    break
    except Exception as exc:
        print(f"[-] Connection error: {type(exc).__name__}: {exc}")
    return False


def main() -> int:
    tvlib.setup_logging()
    try:
        config = tvlib.Config.from_env()
    except ValueError as exc:
        print(f"[!] Invalid configuration: {exc}")
        return 2

    print(__doc__.strip())
    print("-" * 60)

    if asyncio.run(attempt_pairing(config.tv_ip, 8002)):
        return 0
    print("\nRetrying on port 8001 (unencrypted websocket)...")
    return 0 if asyncio.run(attempt_pairing(config.tv_ip, 8001)) else 1


if __name__ == "__main__":
    sys.exit(main())
