# DRM & Samsung TV Power Management Suite

Automated power synchronization, dock/lid state tracking, hardware DPMS monitoring, and DRM capabilities analysis for a Samsung Tizen Smart TV (`UN40KU7000`, `4K60 UHD`) connected as an external monitor on Linux / KDE Plasma 6 (Wayland).

> For an in-depth technical explanation of the hardware topology, protocols, state machine, and edge-case mitigations, see **[ARCHITECTURE.md](ARCHITECTURE.md)**.

---

## Full Feature Set

1. **Real-Time Hardware DPMS Synchronization (`tv_power_daemon.py`)**:
   - Adaptive DPMS tracking: polls internal panel DPMS fast (250ms) only when locked (detecting `Escape` or prompt blanking in milliseconds), and idles at 2000ms when unlocked to conserve CPU.
   - **Screen Off**: When the laptop display turns off (e.g., idle timeout or pressing `Escape` on the lock screen), automatically locks the session (`loginctl lock-session`) and powers off the TV via HDMI-CEC Standby (`--standby`) and WebSocket (`KEY_POWER`).
   - **Screen On**: When the laptop display wakes, decisively wakes the TV via multi-NIC Wake-on-LAN bursts and discrete HDMI-CEC (`IMAGE_VIEW_ON` + `ACTIVE_SOURCE`), then re-asserts native `3840x2160@60Hz` output via `kscreen-doctor` once the TV's HDMI receiver comes online.

2. **Instant Event-Driven Dock Hotplug & Undock Wi-Fi Sleep**:
   - **Zero-Polling Hotplug**: Binds directly to the Linux kernel Netlink KObject UEvent socket. Display and USB changes push hardware notifications directly into the GLib event loop with zero timer polling.
   - **Dock Plug-In (`disconnected -> connected`)**: Immediately wakes the TV from standby/deep standby, claims CEC Logical Address 4 (`Playback 1`), switches the TV directly to HDMI 1 (`phys-addr=1.0.0.0`), and syncs the 4K60 video stream.
   - **Dock Unplug (`connected -> disconnected`)**: Because unplugging the USB-C dock severs HDMI-CEC *and* tears down the dock's wired USB Ethernet interface (`enx50a0300cc53a`), the daemon cancels any active wake worker, bypasses dead CEC queries, and retries sending WebSocket `KEY_POWER` over Wi-Fi (`wlo1`) across the NetworkManager route failover window so the TV immediately turns off instead of sitting on the "Input Disconnected" screen.

3. **Event-Driven Laptop Lid Synchronization**:
   - Listens on the System D-Bus for `PropertiesChanged` on `org.freedesktop.login1.Manager` (`LidClosed`).
   - Zero ACPI polling: completely eliminates the expensive 1.89 ms kernel ACPI AML queries (`/proc/acpi/button/lid/*/state`) while responding instantly to lid open/close events.
   - Closing the lid puts the TV to sleep via HDMI-CEC (if docked) and Wi-Fi WebSocket.
   - Opening the lid while docked triggers the decisive wake sequence.

4. **Zero-Fork Connection Watchdog & Power Inversion Protection**:
   - Every 5 seconds, verifies that if you are logged in, the laptop screen is on, the lid is open, and the dock is connected, the TV is awake.
   - **Zero Subprocess Forks While Streaming**: Probes the TV's open TCP port `8001` via non-blocking socket connect (< 0.4ms) rather than spawning `cec-ctl` processes every few seconds. Only falls back to querying `cec-ctl` if port 8001 is closed or unreachable.
   - **Zero Power Inversion**: Wake sequences use *only* discrete commands (`WoL` + CEC `IMAGE_VIEW_ON` + `ACTIVE_SOURCE`) and never send toggle keys (`KEY_POWER`), while sleep sequences enforce a 12-second duplicate toggle cooldown (`tv_commanded_off`).

5. **Singleton Early-Boot Wake (`boot_wol.py` & `tv-boot-wol.service`)**:
   - Pure systemd oneshot service (`RemainAfterExit=yes`) ordered after `network-online.target` and before `sddm.service`.
   - Wakes the TV once per boot (without firing on subsequent DHCP renewals) if the dock/display or home network (`192.168.0.x`) is present and the laptop screen is on.

6. **Manual CLI Control (`control_tv.py`) & DRM Analyzer (`parse_drm_info.py`)**:
   - `control_tv`: Command-line tool to inspect display/CEC/screensaver/network status or manually trigger wake, standby, or WoL.
   - `parse_drm_info`: Summarizes Intel `i915` GPU capabilities, CRTC pipes, hardware planes, HDR metadata, and connector modes.

---

## Directory Structure & XDG Compliance

- **Source Repository**: `~/src/drmstuff/`
- **Detailed Documentation**: [`~/src/drmstuff/ARCHITECTURE.md`](ARCHITECTURE.md)
- **User Executables**: `~/.local/bin/` (`control_tv`, `tv_power_daemon`, `boot_wol`, `parse_drm_info`, `pair_samsung_tv`)
- **Systemd User Service**: `~/.config/systemd/user/tv-power-sync.service`
- **Systemd Boot Service**: `/etc/systemd/system/tv-boot-wol.service`
- **HDMI-CEC Udev Rule**: `/etc/udev/rules.d/70-cec-uaccess.rules`

---

## Deployment

To install updated scripts into `~/.local/bin`, deploy systemd units and udev rules, and restart `tv-power-sync.service`:

```bash
./deploy.sh
```

---

## CLI Usage

```bash
control_tv status    # Check KDE DPMS, HDMI-CEC power state, D-Bus lock state, and TV network port
control_tv wake      # Decisively wake TV (5x WoL + CEC HDMI 1 active source + 4K60 mode sync)
control_tv standby   # Put TV into standby (DPMS off + CEC standby + WebSocket KEY_POWER)
control_tv wol       # Broadcast multi-NIC Wake-on-LAN magic packets only

parse_drm_info       # Parse and summarize DRM GPU pipes, planes, and connector capabilities
```
