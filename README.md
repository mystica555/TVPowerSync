# TVPowerSync

Automated power synchronization, dock/lid state tracking, hardware DPMS monitoring, and DRM capabilities analysis for a TV used as an external monitor on Linux / KDE Plasma 6 (Wayland). Samsung Tizen is the reference backend; generic HDMI-CEC, Roku ECP, Android TV Remote v2, Fire TV ADB, LG webOS, and Vizio SmartCast backends are included for other models.

> For an in-depth technical explanation of the hardware topology, protocols, state machine, and edge-case mitigations, see **[ARCHITECTURE.md](ARCHITECTURE.md)**.

---

## Full Feature Set

1. **Real-Time Hardware DPMS Synchronization ([`tv_power_daemon.py`](ARCHITECTURE.md#21-tv_power_daemonpy-tv-power-syncservice))**:
   - Adaptive DPMS tracking: polls the internal panel at 250ms only while the session is locked, and idles at 2000ms when unlocked.
   - **Screen Off**: locks the session (`loginctl lock-session`) and asks the configured backend for a decisive standby. The Samsung backend uses discrete HDMI-CEC `STANDBY` when docked and falls back to WebSocket `KEY_POWER` only after a NACKed or ignored command is proven not to have landed. Roku, Android TV, Fire TV, LG webOS and Vizio SmartCast use their own API after CEC, and every path verifies delivery instead of trusting an exit status.
   - **Screen On**: wakes through the backend (multi-NIC Wake-on-LAN plus discrete CEC/API wake), then re-asserts the native mode via `kscreen-doctor output.<connector>.mode.<TV_MODE>` once the TV confirms it is awake.
   - **Non-blocking event loop**: every CEC/WoL/WebSocket/kscreen call runs in a worker thread. The GLib loop only reads sysfs and dispatches intents.
   - **Startup reconciliation**: a daemon start while the panel is already off commands the TV off; a start while the panel is on and docked wakes the TV if it is in standby.

2. **Event-Driven Dock Hotplug & Undock Wi-Fi Sleep**:
   - **Kernel Netlink UEvents**: DRM hotplug and USB dock attach/detach are pushed into the GLib loop. Events are coalesced with a 400ms debounce because USB-C/PD renegotiation emits bursts.
   - **Real fallback**: if the netlink bind fails, the daemon polls external display state every 2s instead of silently losing hotplug detection.
   - **Dock Plug-In (`disconnected -> connected`)**: clears wake backoff and runs the decisive wake sequence.
   - **Dock Unplug (`connected -> disconnected`)**: cancels any in-flight wake, skips the now-dead CEC adapter, and retries the backend's Wi-Fi path across the NetworkManager route failover window.

3. **Event-Driven Laptop Lid Synchronization**:
   - Listens on the System D-Bus for `PropertiesChanged` on `org.freedesktop.login1.Manager` (`LidClosed`); zero ACPI polling.
   - Closing the lid creates a sleep intent. Opening it clears backoff and wakes the TV if the dock is connected.

4. **Intent Model, Serialized CEC & Exponential Backoff**:
   - Every ON/OFF request is a monotonically numbered intent with its own cancellation event. A new intent cancels the previous one, and workers check cancellation before each side effect — a slow sleep retry can no longer deliver `KEY_POWER` after a wake has started.
   - HDMI-CEC access is serialized: only one `cec-ctl` process can touch `/dev/cec0` at a time, and a cancelled intent's command is dropped inside that lock. Wake, standby and query are each a single `cec-ctl` invocation.
   - The 5-second watchdog runs in a worker thread, probes the selected backend's control port (no forks while streaming; port `8001` for Samsung), falls back to CEC when the port is closed, and requires two consecutive `offline` readings.
   - Failed wakes back off exponentially (60s → 120s → ... → 15min) instead of retrying every ~75s forever. A dock connect, DPMS-on, lid-open or unlock resets the backoff.
   - 12-second duplicate-off cooldown, shared by CEC and WebSocket paths.

5. **Singleton Early-Boot Wake ([`boot_wol.py`](ARCHITECTURE.md#22-boot_wolpy-tv-boot-wolservice) & `tv-boot-wol.service`)**:
   - systemd oneshot (`RemainAfterExit=yes`) ordered after `network-online.target` and before `sddm.service`.
   - Wakes the TV once per boot if the dock/display or home network is present and the panel is not in DPMS Off.

6. **Manual CLI Control ([`control_tv.py`](ARCHITECTURE.md#23-control_tvpy)) & DRM Analyzer ([`parse_drm_info.py`](ARCHITECTURE.md#24-parse_drm_infopy))**
   - `control_tv`: inspect display/CEC/screensaver/network status or manually trigger wake, standby, or WoL.
   - `parse_drm_info`: summarize GPU capabilities, CRTC pipes, planes, HDR metadata, and connector modes.

---

## Backends

`TV_BACKEND` selects how power commands reach the TV. HDMI-CEC is tried first
when an adapter is configured; the vendor API is the fallback, and is the
primary path after the dock (and therefore CEC) disappears. The deployed
service reads the value baked in by `TV_BACKEND=roku ./deploy.sh`; a permanent
change can also be made with `systemctl --user edit tv-power-sync.service`.

| Backend | Best for | Requirements |
| :--- | :--- | :--- |
| `cec` | Any CEC-capable TV | `cec-utils`, `/dev/cec0` (udev rule included) |
| `samsung` | Samsung Tizen TVs | `samsungtvws`, TV network control on port 8001 |
| `roku` | Roku TVs | ECP enabled (`Settings > System > Advanced system settings > Control by mobile apps`) |
| `androidtv` | Android TV / Google TV devices | `androidtvremote2`, run `pair_android_tv.py` once |
| `firetv` | Fire TV / Fire TV Edition televisions | `adb`, ADB debugging enabled and authorized |
| `lg` | LG webOS TVs | `aiowebostv`, run `pair_lg_tv.py` once; network standby and WoL enabled |
| `vizio` | Vizio SmartCast TVs | `vizaio`, run `pair_vizio_tv.py` once; port 7345 (9000 on older firmware) |

Set `TV_IP` to the device and `TV_MAC` for Wake-on-LAN. LG and Vizio use
their local API for state and standby, but wake a fully sleeping TV through
WoL or CEC, so `TV_MAC` is required for reliable power-on. For a streaming box
rather than a TV, set `CEC_DEVICE=` so the backend tracks the box's own power
instead of the TV it is plugged into.

---

## Configuration

All scripts read the same environment variables (defaults match the original setup):

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `TV_IP` | `192.168.0.2` | TV address |
| `TV_MAC` | `40:16:3b:d0:4f:8b` | Wake-on-LAN target (leave unset for non-Samsung to disable WoL) |
| `TV_BACKEND` | `samsung` | `cec`, `samsung`, `roku`, `androidtv`, `firetv`, `lg` or `vizio` |
| `CEC_DEVICE` | `/dev/cec0` | HDMI-CEC adapter (empty disables CEC) |
| `CEC_PHYS_ADDR` | `1.0.0.0` | CEC physical address advertised as the laptop's source |
| `TV_ECP_PORT` | `8060` | Roku ECP port |
| `TV_ADB_PORT` | `5555` | Fire TV ADB port |
| `ADB_BIN` | `adb` | ADB executable |
| `ANDROIDTV_CERT` | `~/.local/state/tvpowersync/androidtv_cert.pem` | Android TV client certificate |
| `ANDROIDTV_KEY` | `~/.local/state/tvpowersync/androidtv_key.pem` | Android TV client key |
| `ANDROIDTV_CLIENT_NAME` | `TVPowerSync` | Name shown on the TV while pairing |
| `LG_CLIENT_KEY_FILE` | `~/.local/state/tvpowersync/lg_client_key.txt` | LG webOS client key written by `pair_lg_tv.py` |
| `VIZIO_TOKEN_FILE` | `~/.local/state/tvpowersync/vizio_token.txt` | Vizio SmartCast token written by `pair_vizio_tv.py` |
| `TV_VIZIO_PORT` | `7345` | Vizio SmartCast HTTPS port (9000 on older firmware) |
| `TV_MODE` | `3840x2160@60` | Mode re-asserted via `kscreen-doctor` |
| `TV_LOG_LEVEL` | `INFO` | Logging level (`DEBUG` is useful for CEC failures) |
| `TV_PYTHON` | `$XDG_DATA_HOME/tvpowersync/venv/bin/python` | Interpreter substituted into the systemd unit by `deploy.sh` |
| `TV_TOKEN_FILE` | `~/.local/state/tvpowersync/tv_token.txt` | Pairing token path |
| `TV_DRM_INFO` | `drm_info.json` (working directory) | JSON snapshot read by `parse_drm_info` |

Override persistent values in the systemd unit (for example
`systemctl --user edit tv-power-sync.service`), not in the scripts.

---

## Directory Structure & XDG Compliance

- **Source Repository**: `~/src/TVPowerSync/` (any location works)
- **Application Data**: `~/.local/share/tvpowersync/` (venv; honours `$XDG_DATA_HOME`)
- **Shared Helpers**: [`tvlib.py`](ARCHITECTURE.md#27-tvlibpy) (config, logging, WoL, CEC, sysfs, backoff) and [`tv_backends.py`](ARCHITECTURE.md#28-tv_backendspy) (vendor power paths)
- **User Executables**: `~/.local/bin/` (`control_tv`, `tv_power_daemon`, `boot_wol`, `parse_drm_info`, `pair_samsung_tv`, `pair_android_tv`, `pair_lg_tv`, `pair_vizio_tv`, `tvlib.py`, `tv_backends.py`)
- **Systemd User Service**: `~/.config/systemd/user/tv-power-sync.service`
- **Systemd Boot Service**: `/etc/systemd/system/tv-boot-wol.service`
- **HDMI-CEC Udev Rule**: `/etc/udev/rules.d/70-cec-uaccess.rules`

---

## Deployment

The daemon needs `dbus` and `gi` (system packages) plus the vendor libraries
`samsungtvws`, `androidtvremote2`, `aiowebostv`, and `vizaio` (pip; the latter
three are only needed for their respective backends). `deploy.sh` provisions
them:

```bash
./deploy.sh
```

If no usable venv exists, the script creates one at
`$XDG_DATA_HOME/tvpowersync/venv` (default `~/.local/share/tvpowersync/venv`)
with `python3 -m venv --system-site-packages`, so the system `dbus`/`gi` bindings
stay importable, and installs `samsungtvws`, `androidtvremote2`, `aiowebostv`,
and `vizaio` into it. The clone is only the source: after deployment it can be
moved or deleted, because the executables live in `~/.local/bin` and the
services point at the venv in the data directory.

The script then installs `tvlib.py` alongside the scripts, substitutes the
selected interpreter into `tv-power-sync.service` and the boot unit
(`TV_PYTHON` overrides the choice), rewrites the installed shebangs to it, and
restarts the user service.

An existing venv that cannot see `dbus`/`gi` is repaired by adding a
version-matched `000-system-bindings.pth` (equivalent to
`--system-site-packages`) instead of being re-created. The script fails with an
explicit message if no interpreter can provide the system bindings, and warns if
any vendor library is missing (the corresponding backend is disabled).

---

## CLI Usage

```bash
control_tv status    # KDE DPMS, CEC power state, D-Bus lock state, backend control port
control_tv wake      # Decisive wake (5x WoL + CEC HDMI 1 + mode sync)
control_tv standby   # DPMS off + CEC standby, then the backend's network fallback
control_tv wol       # Broadcast multi-NIC Wake-on-LAN magic packets only

pair_android_tv      # One-time Android TV Remote pairing (certificate + on-screen code)
pair_lg_tv           # One-time LG webOS pairing (client key + on-screen prompt)
pair_vizio_tv        # One-time Vizio SmartCast pairing (auth token + on-screen PIN)
parse_drm_info       # Summarize DRM GPU pipes, planes, and connector capabilities
```

---

## Caveats

- The watchdog's TCP fallback assumes the TV closes the backend's control port
  in standby (for example port `8001` on Samsung). If a firmware update keeps it
  open, CEC remains the authoritative state source (the daemon queries CEC when
  the port is closed, and reports `on` otherwise).
- A system suspend can begin before the asynchronous sleep worker finishes. The
  daemon still issues the command as early as possible, but logind does not wait
  for `PrepareForSleep` handlers.
- Roku ECP `PowerOff` only works on Roku TVs, not Roku streaming players. Fire TV
  control needs ADB debugging to stay authorized; some devices drop ADB in deep
  standby, in which case CEC remains the wake path.
- Roku wake needs Wake-on-LAN (`TV_MAC`) or Fast TV Start keeping ECP reachable
  in standby. Android TV, LG webOS and Vizio SmartCast wake through WoL/CEC; none
  of their local APIs can wake a fully sleeping TV.
- LG webOS needs network standby / “Mobile TV On” enabled for WoL. Vizio
  SmartCast usually closes its HTTPS API in standby, and Eco mode disables WoL;
  re-run `pair_vizio_tv.py` after a factory reset or token invalidation.
