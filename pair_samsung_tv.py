#!/usr/bin/env python3
"""
pair_samsung_tv.py - Initiate WebSocket pairing with Samsung Smart TV (2016 KU7000)

Steps before running:
1. Make sure the TV is powered ON and on the local network (192.168.0.2).
2. On your TV, navigate to:
   Settings -> General -> External Device Manager -> Device Connection Manager
   - Access Notification: Set to 'First Time Only' or 'Always On'
   - Device List: Check and clear any old/denied entries if necessary
3. Run this script. An on-screen prompt "Allow connection from Antigravity/Laptop?"
   should appear on the TV.
4. Press 'Allow' using your TV remote.
"""

import sys
import os
import ssl
import json
import base64
import asyncio

TV_IP = "192.168.0.2"
TOKEN_FILE = os.path.expanduser("~/src/drmstuff/tv_token.txt")

try:
    import websockets
except ImportError:
    print("Error: websockets library not found in current environment.")
    sys.exit(1)

async def attempt_pairing(port=8002):
    use_ssl = (port == 8002)
    name_b64 = base64.b64encode(b"Laptop-KDE").decode("utf-8")
    scheme = "wss" if use_ssl else "ws"
    url = f"{scheme}://{TV_IP}:{port}/api/v2/channels/samsung.remote.control?name={name_b64}"

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
                            with open(TOKEN_FILE, "w") as f:
                                f.write(token)
                            print(f"[+] Token saved to {TOKEN_FILE}")
                            return True
                    elif data.get("event") == "ms.channel.unauthorized":
                        print("[-] Received ms.channel.unauthorized from TV.")
                        print("    If a prompt appeared on the TV, click 'Allow'.")
                        print("    If no prompt appeared, check Device Connection Manager on the TV.")
                except asyncio.TimeoutError:
                    print("[-] Timed out waiting for message.")
                    break
    except Exception as e:
        print(f"[-] Connection error: {type(e).__name__}: {e}")
    return False

def main():
    print(__doc__.strip())
    print("-" * 60)
    input("Press Enter when ready to start pairing...")
    
    # Try port 8002 first (standard for Tizen secure websocket)
    success = asyncio.run(attempt_pairing(8002))
    if not success:
        print("\nRetrying on port 8001 (unencrypted websocket)...")
        asyncio.run(attempt_pairing(8001))

if __name__ == "__main__":
    main()
