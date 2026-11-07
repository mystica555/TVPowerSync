#!/usr/bin/env python3
"""
control_tv.py - Manual power control and diagnostics for the configured TV.

Usage:
    ./control_tv.py status      # Check DPMS, backend power state, lock state, reachability
    ./control_tv.py wake        # Wake TV through the selected backend
    ./control_tv.py standby     # Put TV into standby through the selected backend
    ./control_tv.py wol         # Send Wake-on-LAN magic packets only

Backend and addresses are read from TV_BACKEND / TV_IP / TV_MAC / CEC_DEVICE /
TV_MODE / ... (see tvlib.py).
"""

import subprocess
import sys
import time

try:
    import dbus
except ImportError:
    dbus = None

import tv_backends
import tvlib


def run(cmd: list[str], timeout: float = 15.0):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[!] {' '.join(cmd)} failed: {exc}")
        return None


def show_status(config: tvlib.Config, backend: tv_backends.TvBackend) -> None:
    print("=== TV Backend ===")
    print(f"{backend.name}: {backend.description}")

    print("\n=== Display Power Management (DPMS) ===")
    result = run(["kscreen-doctor", "--dpms", "show"])
    if result is not None and result.stdout.strip():
        print(result.stdout.strip())
    else:
        print("DPMS query returned no output.")

    print("\n=== TV Power State ===")
    state = backend.power_state()
    print(f"Backend power state: {state}")
    if backend.cec is not None:
        cec_state = backend.cec.power_state()
        print(f"CEC pwr-state: {cec_state or 'no response'}")

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
    if backend.probe_port:
        if tvlib.is_tv_network_awake(
            config.tv_ip, timeout=0.4, port=backend.probe_port
        ):
            print(
                f"TV control port {backend.probe_port} on {config.tv_ip} is "
                "AWAKE and responding."
            )
        else:
            print(
                f"TV control port {backend.probe_port} on {config.tv_ip} is "
                "ASLEEP / unreachable."
            )
    else:
        print("This backend has no network control port.")


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


def wake_tv(config: tvlib.Config, backend: tv_backends.TvBackend) -> None:
    print("[*] Initiating TV Wake Sequence...")
    if tvlib.read_panel_dpms() != "On":
        run(["kscreen-doctor", "--dpms", "on"])

    tv_state = backend.power_state()
    if tv_state in ("on", "to-on"):
        print(f"[*] TV is already '{tv_state}'. Re-asserting active source and mode...")
        backend.active_source()
        assert_mode(config)
        print("[*] TV wake sequence completed.\n")
        show_status(config, backend)
        return

    print(f"[*] Sending wake sequence via the {backend.name} backend...")
    backend.wake()

    for attempt in range(1, 11):
        print(f"[*] Wake attempt {attempt}/10: waiting for TV to wake...")
        time.sleep(1.5)
        current_state = backend.power_state()
        if current_state in ("on", "to-on"):
            print(
                f"[*] TV confirmed awake ({current_state}) on attempt {attempt}. "
                "Syncing video stream..."
            )
            assert_mode(config)
            backend.active_source()
            break
        backend.nudge()
    else:
        print("[!] TV failed to wake after 10 attempts.")

    print("[*] TV wake sequence completed.\n")
    show_status(config, backend)


def standby_tv(config: tvlib.Config, backend: tv_backends.TvBackend) -> None:
    print("[*] Initiating TV Standby Sequence...")
    run(["kscreen-doctor", "--dpms", "off"])

    state = backend.power_state()
    if state in ("standby", "to-standby"):
        print(f"[*] TV is already in {state}. Skipping power command.")
    elif backend.standby(reason="CLI standby"):
        print(f"[*] TV standby delivered via the {backend.name} backend.")
    else:
        print("[!] Could not deliver standby via the selected backend.")

    print("[*] Waiting 4 seconds for the TV network stack to spin down...")
    time.sleep(4)
    print("[*] TV standby commands completed.\n")
    show_status(config, backend)


def main() -> int:
    tvlib.setup_logging()
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__.strip())
        return 0

    try:
        config = tvlib.Config.from_env()
        backend = tv_backends.create_backend(config)
    except ValueError as exc:
        print(f"[!] Invalid configuration: {exc}")
        return 2

    action = sys.argv[1].lower()
    try:
        if action == "status":
            show_status(config, backend)
        elif action in ("wake", "on"):
            wake_tv(config, backend)
        elif action in ("standby", "off", "sleep"):
            standby_tv(config, backend)
        elif action == "wol":
            tvlib.send_wol(config.tv_mac, config.tv_ip)
        else:
            print(f"Unknown action: {action}")
            print(__doc__.strip())
            return 1
    finally:
        backend.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
