# DRM & Samsung TV Power Management Suite

Automated power synchronization, hardware DPMS tracking, and DRM capabilities analysis for Samsung Smart TVs (Tizen OS / 2016+ KU-series) connected as PC monitors on Linux/KDE Plasma.

## Architecture & Features

1. **Real-time Hardware DPMS Sync (`tv_power_daemon.py`)**:
   - Directly monitors `/sys/class/drm/*-eDP-*/dpms` in real time.
   - Detects when the laptop screen turns off (e.g. pressing `Escape` at the KDE lock screen or idle power management timeout) and immediately powers off the TV via WebSocket and HDMI-CEC Standby.
   - Detects when the laptop screen turns on (pressing any key or unlocking) and immediately broadcasts a Wake-on-LAN Magic Packet and HDMI-CEC Wake command.
2. **Early Boot Wake (`boot_wol.py`)**:
   - Systemd unit `tv-boot-wol.service` and NetworkManager dispatcher hook.
   - Checks if the TV is connected via DRM sysfs (`card*-DP-*/status`).
   - If connected, broadcasts gratuitous WoL packets and sends HDMI-CEC wake as soon as DHCP succeeds, before SDDM (graphical login) starts.
3. **Manual CLI Management (`control_tv.py`)**:
   - Simple CLI utility available on `$PATH` (`control_tv [status|wake|standby|wol]`).
   - Queries DPMS, HDMI-CEC state, and KDE screensaver state via pure Python D-Bus.
4. **DRM Analyzer (`parse_drm_info.py`)**:
   - Formats and summarizes DRM GPU capabilities, hardware planes, CRTC pipes, color management, format modifiers, and connected displays.

## Directory Structure & XDG Compliance

- Source repository: `~/src/drmstuff/`
- User executables: `~/.local/bin/` (`$XDG_BIN_HOME`)
- Systemd user service: `~/.config/systemd/user/tv-power-sync.service` (`$XDG_CONFIG_HOME`)
- System boot service: `/etc/systemd/system/tv-boot-wol.service`
- NetworkManager hook: `/etc/NetworkManager/dispatcher.d/99-tv-boot-wol.sh`

## Deployment

To deploy or update files into `~/.local/bin` and reload all systemd services:

```bash
./deploy.sh
```

## CLI Usage

Since `~/.local/bin` is in your `$PATH`, you can run the commands directly:

```bash
control_tv status    # Check DPMS, HDMI-CEC power state, and KDE screensaver
control_tv wake      # Wake TV (WoL + CEC wake + DPMS on)
control_tv standby   # Turn off TV (CEC standby + DPMS off)
control_tv wol       # Broadcast WoL magic packet only

parse_drm_info       # Parse and summarize DRM capabilities
```
