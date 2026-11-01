# TizenPowerSync

Automated power synchronization, dock/lid state tracking, hardware DPMS monitoring, and DRM capabilities analysis for a Samsung Tizen Smart TV (`UN40KU7000`, `4K60 UHD`) connected as an external monitor on Linux / KDE Plasma 6 (Wayland).

> For an in-depth technical explanation of the hardware topology, protocols, state machine, and edge-case mitigations, see **[ARCHITECTURE.md](ARCHITECTURE.md)**.

---

## Full Feature Set

1. **Real-Time Hardware DPMS Synchronization (`tv_power_daemon.py`)**:
   - Adaptive DPMS tracking: polls the internal panel at 250ms only while the session is locked, and idles at 2000ms when unlocked.
   - **Screen Off**: locks the session (`loginctl lock-session`) and then uses **one** power path — discrete HDMI-CEC `STANDBY` when docked, with WebSocket `KEY_POWER` only as a fallback if CEC fails. The two are never sent together, so a toggle can't undo the standby.
   - **Screen On**: wakes the TV with multi-NIC Wake-on-LAN bursts and discrete HDMI-CEC (`IMAGE_VIEW_ON` + `ACTIVE_SOURCE`), then re-asserts the native mode via `kscreen-doctor output.<connector>.mode.<TV_MODE>` once the TV confirms it is awake.
   - **Non-blocking event loop**: every CEC/WoL/WebSocket/kscreen call runs in a worker thread. The GLib loop only reads sysfs and dispatches intents.
   - **Startup reconciliation**: a daemon start while the panel is already off commands the TV off; a start while the panel is on and docked wakes the TV if it is in standby.

2. **Event-Driven Dock Hotplug & Undock Wi-Fi Sleep**:
   - **Kernel Netlink UEvents**: DRM hotplug and USB dock attach/detach are pushed into the GLib loop. Events are coalesced with a 400ms debounce because USB-C/PD renegotiation emits bursts.
   - **Real fallback**: if the netlink bind fails, the daemon polls external display state every 2s instead of silently losing hotplug detection.
   - **Dock Plug-In (`disconnected -> connected`)**: clears wake backoff and runs the decisive wake sequence.
   - **Dock Unplug (`connected -> disconnected`)**: cancels any in-flight wake, skips the now-dead CEC adapter, and retries WebSocket `KEY_POWER` over Wi-Fi across the NetworkManager route failover window.

3. **Event-Driven Laptop Lid Synchronization**:
   - Listens on the System D-Bus for `PropertiesChanged` on `org.freedesktop.login1.Manager` (`LidClosed`); zero ACPI polling.
   - Closing the lid creates a sleep intent. Opening it clears backoff and wakes the TV if the dock is connected.

4. **Intent Model, Serialized CEC & Exponential Backoff**:
   - Every ON/OFF request is a monotonically numbered intent with its own cancellation event. A new intent cancels the previous one, and workers check cancellation before each side effect — a slow sleep retry can no longer deliver `KEY_POWER` after a wake has started.
   - HDMI-CEC access is serialized: only one `cec-ctl` process can touch `/dev/cec0` at a time, and a cancelled intent's command is dropped inside that lock. Wake, standby and query are each a single `cec-ctl` invocation.
   - The 5-second watchdog runs in a worker thread, probes TCP port `8001` (no forks while streaming), falls back to CEC when the port is closed, and requires two consecutive `offline` readings.
   - Failed wakes back off exponentially (60s → 120s → ... → 15min) instead of retrying every ~75s forever. A dock connect, DPMS-on, lid-open or unlock resets the backoff.
   - 12-second duplicate-off cooldown, shared by CEC and WebSocket paths.

5. **Singleton Early-Boot Wake (`boot_wol.py` & `tv-boot-wol.service`)**:
   - systemd oneshot (`RemainAfterExit=yes`) ordered after `network-online.target` and before `sddm.service`.
   - Wakes the TV once per boot if the dock/display or home network is present and the panel is not in DPMS Off.

6. **Manual CLI Control (`control_tv.py`) & DRM Analyzer (`parse_drm_info.py`)**
   - `control_tv`: inspect display/CEC/screensaver/network status or manually trigger wake, standby, or WoL.
   - `parse_drm_info`: summarize GPU capabilities, CRTC pipes, planes, HDR metadata, and connector modes.

---

## Configuration

All scripts read the same environment variables (defaults match the original setup):

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `TV_IP` | `192.168.0.2` | TV address |
| `TV_MAC` | `40:16:3b:d0:4f:8b` | Wake-on-LAN target |
| `CEC_DEVICE` | `/dev/cec0` | HDMI-CEC adapter |
| `TV_MODE` | `3840x2160@60` | Mode re-asserted via `kscreen-doctor` |
| `TV_LOG_LEVEL` | `INFO` | Logging level (`DEBUG` is useful for CEC failures) |
| `TV_PYTHON` | repo `venv/bin/python` | Interpreter substituted into the systemd unit by `deploy.sh` |
| `TV_TOKEN_FILE` | `~/.local/state/drmstuff/tv_token.txt` | Pairing token path |

Override persistent values in the systemd unit (for example
`systemctl --user edit tv-power-sync.service`), not in the scripts.

---

## Directory Structure & XDG Compliance

- **Source Repository**: `~/src/drmstuff/`
- **Shared Helpers**: `tvlib.py` (config, logging, WoL, CEC, sysfs, backoff)
- **User Executables**: `~/.local/bin/` (`control_tv`, `tv_power_daemon`, `boot_wol`, `parse_drm_info`, `pair_samsung_tv`, `tvlib.py`)
- **Systemd User Service**: `~/.config/systemd/user/tv-power-sync.service`
- **Systemd Boot Service**: `/etc/systemd/system/tv-boot-wol.service`
- **HDMI-CEC Udev Rule**: `/etc/udev/rules.d/70-cec-uaccess.rules`

---

## Deployment

The daemon needs `dbus` and `gi` (system packages) plus `samsungtvws` (pip). The
recommended layout is a venv that can still see the system bindings:

```bash
python3 -m venv --system-site-packages venv
./venv/bin/pip install samsungtvws
./deploy.sh
```

`deploy.sh` installs `tvlib.py` alongside the scripts, selects an interpreter
that can import `dbus` and `gi` (`TV_PYTHON` overrides it), substitutes it into
`tv-power-sync.service`, rewrites the installed shebangs to it, and restarts the
user service. If an existing venv cannot see `dbus`/`gi`, the script adds a
version-matched `000-system-bindings.pth` to that venv (equivalent to
`--system-site-packages`) instead of re-creating it. It fails with an explicit
message if no interpreter can provide the system bindings, and warns if
`samsungtvws` is missing (Wi-Fi control disabled).

---

## CLI Usage

```bash
control_tv status    # KDE DPMS, CEC power state, D-Bus lock state, TV port 8001
control_tv wake      # Decisive wake (5x WoL + CEC HDMI 1 + mode sync)
control_tv standby   # DPMS off + CEC standby (Wi-Fi toggle only if CEC fails)
control_tv wol       # Broadcast multi-NIC Wake-on-LAN magic packets only

parse_drm_info       # Summarize DRM GPU pipes, planes, and connector capabilities
```

---

## Caveats

- The watchdog's TCP fallback assumes the TV closes port `8001` in standby. If a
  firmware update keeps it open, CEC remains the authoritative state source (the
  daemon queries CEC when the port is closed, and reports `on` otherwise).
- A system suspend can begin before the asynchronous sleep worker finishes. The
  daemon still issues the command as early as possible, but logind does not wait
  for `PrepareForSleep` handlers.
