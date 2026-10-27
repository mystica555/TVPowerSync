# DRM & Samsung TV Power Management Suite

Automated power synchronization, dock/lid state tracking, hardware DPMS monitoring, and DRM capabilities analysis for a Samsung Tizen Smart TV (`UN40KU7000`, `4K60 UHD`) connected as an external monitor on Linux / KDE Plasma 6 (Wayland).

> For an in-depth technical explanation of the hardware topology, protocols, state machine, and edge-case mitigations, see **[ARCHITECTURE.md](ARCHITECTURE.md)**.

---

## Full Feature Set

1. **Real-Time Hardware DPMS Synchronization (`tv_power_daemon.py`)**:
   - Polls `/sys/class/drm/*-eDP-*/dpms` every `150ms` alongside D-Bus listeners for `org.freedesktop.ScreenSaver` (lock/unlock) and `org.freedesktop.login1` (suspend/resume).
   - **Screen Off**: When the laptop display turns off (e.g., idle timeout or pressing `Escape` on the lock screen), automatically locks the session (`loginctl lock-session`) and powers off the TV via HDMI-CEC Standby (`--standby`) and WebSocket (`KEY_POWER`).
   - **Screen On**: When the laptop display wakes, decisively wakes the TV via multi-NIC Wake-on-LAN bursts and discrete HDMI-CEC (`IMAGE_VIEW_ON` + `ACTIVE_SOURCE`), then re-asserts native `3840x2160@60Hz` output via `kscreen-doctor` once the TV's HDMI receiver comes online.

2. **Instant Dock Hotplug Wake & Undock Wi-Fi Sleep**:
   - **Dock Plug-In (`disconnected -> connected`)**: Dynamically monitors all `/sys/class/drm/*-DP-*/status` and `*-HDMI-*/status` ports. Immediately wakes the TV from standby/deep standby, claims CEC Logical Address 4 (`Playback 1`), switches the TV directly to HDMI 1 (`phys-addr=1.0.0.0`), and syncs the 4K60 video stream.
   - **Dock Unplug (`connected -> disconnected`)**: Because unplugging the USB-C dock severs HDMI-CEC *and* tears down the dock's wired USB Ethernet interface (`enx50a0300cc53a`), the daemon cancels any active wake worker, bypasses dead CEC queries, and retries sending WebSocket `KEY_POWER` over Wi-Fi (`wlo1`) across the NetworkManager route failover window so the TV immediately turns off instead of sitting on the "Input Disconnected" screen.

3. **Laptop Lid Open / Close Synchronization**:
   - Monitors `/proc/acpi/button/lid/*/state`.
   - Closing the lid puts the TV to sleep via HDMI-CEC (if docked) and Wi-Fi WebSocket.
   - Opening the lid while docked triggers the decisive wake sequence.

4. **Active Connection Watchdog & Power Inversion Protection**:
   - Every ~3 seconds, verifies that if you are logged in, the laptop screen is on, the lid is open, and the dock is connected, the TV is awake.
   - Uses a two-tier status check (HDMI-CEC `GIVE_DEVICE_POWER_STATUS` with a fast TCP `192.168.0.2:8001` socket fallback) to prevent false alarms from single CEC bus collisions.
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
