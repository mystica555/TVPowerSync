#!/usr/bin/env python3
"""
control_tv.py - Manual power control and diagnostics for the Samsung TV.

Usage:
    ./control_tv.py status      # Check DPMS, CEC power state, lock state, reachability
    ./control_tv.py wake        # Wake TV (WoL + CEC Image View On + DPMS/mode sync)
    ./control_tv.py standby     # Put TV into standby (CEC first, Wi-Fi fallback)
    ./control_tv.py wol         # Send Wake-on-LAN magic packets only

Configuration is read from TV_IP / TV_MAC / CEC_DEVICE / TV_MODE (see tvlib.py).
"""

import subprocess
import sys
import time

try:
    import dbus
except ImportError:
    dbus = None

import tvlib


def run(cmd: list[str], timeout: float = 15.0):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[!] {' '.join(cmd)} failed: {exc}")
        return None


def show_status(config: tvlib.Config, cec: tvlib.CecController) -> None:
    print("=== Display Power Management (DPMS) ===")
    result = run(["kscreen-doctor", "--dpms", "show"])
    if result is not None and result.stdout.strip():
        print(result.stdout.strip())
    else:
        print("DPMS query returned no output.")

    print("\n=== HDMI-CEC Power Status ===")
    state = cec.power_state()
    if state:
        print(f"CEC pwr-state: {state}")
    else:
        print("CEC query returned no power state.")

    print("\n=== KDE Screensaver Status (via Python D-Bus) ===")
    if dbus is not None:
        try:
            bus = dbus.SessionBus()
            obj = bus.get_object(
                "org.freedesktop.ScreenSaver", "/org/freedesktop/ScreenSaver"
            )
            iface = dbus.Interface(obj, "org.freedesktop.ScreenSaver")
            is_active = bool(iface.GetActive())
            label = "Active / Locked (Screen Sleeping)" if is_active else "Inactive / Unlocked"
            print(f"Screensaver: {label} (GetActive = {is_active})")
        except Exception as exc:
            print(f"Screensaver: Query error via D-Bus: {exc}")
    else:
        print("Screensaver: python-dbus module not available.")

    print("\n=== Network Reachability ===")
    if tvlib.is_tv_network_awake(config.tv_ip, timeout=0.4):
        print(f"TV network interface ({config.tv_ip}:8001) is AWAKE and responding.")
    else:
        print(f"TV network interface ({config.tv_ip}:8001) is ASLEEP / unreachable.")


def assert_mode(config: tvlib.Config) -> None:
    connector = tvlib.external_connector_name()
    if not connector:
        print("[!] No active external connector found; skipping mode re-assert.")
        return
    result = run(["kscreen-doctor", f"output.{connector}.mode.{config.tv_mode}"])
    if result is not None and result.returncode == 0:
        print(f"[*] Re-asserted {connector} mode {config.tv_mode}.")
    elif result is not None:
        print(f"[!] kscreen-doctor mode re-assert exited {result.returncode}.")


def wake_tv(config: tvlib.Config, cec: tvlib.CecController) -> None:
    print("[*] Initiating TV Wake Sequence...")
    if tvlib.read_panel_dpms() != "On":
        run(["kscreen-doctor", "--dpms", "on"])

    tv_state = tvlib.get_tv_power_state(cec, config.tv_ip)
    if tv_state in ("on", "to-on"):
        print(f"[*] TV is already '{tv_state}'. Re-asserting active source and mode...")
        assert_mode(config)
        cec.wake()
        print("[*] TV wake sequence completed.\n")
        show_status(config, cec)
        return

    print("[*] Broadcasting rapid Wake-on-LAN burst (5 packets) across all NICs...")
    tvlib.send_wol(config.tv_mac, config.tv_ip, bursts=5, delay_between=0.05)

    # Discrete wake only: never send the KEY_POWER toggle here.
    cec.wake()

    for attempt in range(1, 11):
        print(f"[*] Wake attempt {attempt}/10: waiting for TV to wake via WoL/CEC...")
        time.sleep(1.5)
        current_state = tvlib.get_tv_power_state(cec, config.tv_ip)
        if current_state in ("on", "to-on"):
            print(
                f"[*] TV confirmed awake ({current_state}) on attempt {attempt}. "
                "Syncing video stream..."
            )
            assert_mode(config)
            cec.wake()
            break
        tvlib.send_wol(config.tv_mac, config.tv_ip, bursts=4, delay_between=0.05)
        cec.wake()
    else:
        print("[!] TV failed to wake after 10 attempts.")

    print("[*] TV wake sequence completed.\n")
    show_status(config, cec)


def standby_tv(config: tvlib.Config, cec: tvlib.CecController) -> None:
    print("[*] Initiating TV Standby Sequence...")
    run(["kscreen-doctor", "--dpms", "off"])

    state = tvlib.get_tv_power_state(cec, config.tv_ip)
    if state in ("standby", "to-standby"):
        print(f"[*] TV is already in {state}. Skipping power command.")
    elif tvlib.external_display_connected() and cec.standby():
        # Only fall back to a toggle when CEC did not confirm delivery; the
        # toggle would otherwise undo the standby.
        print("[*] TV standby delivered via HDMI-CEC.")
    elif tvlib.is_tv_network_awake(config.tv_ip, timeout=0.6) and tvlib.send_key(
        config.tv_ip
    ):
        print("[*] TV standby delivered via Wi-Fi WebSocket.")
    else:
        print("[!] Could not deliver standby over CEC or Wi-Fi.")

    print("[*] Waiting 4 seconds for the TV network stack to spin down...")
    time.sleep(4)
    print("[*] TV standby commands completed.\n")
    show_status(config, cec)


def main() -> int:
    tvlib.setup_logging()
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__.strip())
        return 0

    try:
        config = tvlib.Config.from_env()
    except ValueError as exc:
        print(f"[!] Invalid configuration: {exc}")
        return 2

    cec = tvlib.CecController(config.cec_device)
    action = sys.argv[1].lower()

    if action == "status":
        show_status(config, cec)
    elif action in ("wake", "on"):
        wake_tv(config, cec)
    elif action in ("standby", "off", "sleep"):
        standby_tv(config, cec)
    elif action == "wol":
        tvlib.send_wol(config.tv_mac, config.tv_ip)
    else:
        print(f"Unknown action: {action}")
        print(__doc__.strip())
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
