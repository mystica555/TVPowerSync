# Detailed Architecture & Technical Reference

This document provides an in-depth technical reference for the **DRM & Samsung TV Power Management Suite**, explaining the complete feature set, hardware topology, protocol interactions, state machine design, and edge-case mitigations.

---

## 1. System Overview & Hardware Topology

The software synchronizes the power state, input selection, and video link of a **Samsung Tizen Smart TV (`UN40KU7000`)** used as a primary **4K60 UHD (`3840x2160 @ 60Hz`)** external monitor for a Linux laptop running **KDE Plasma 6 (Wayland)**.

### Hardware & Interface Stack
- **Internal Display (`eDP-1`)**: Laptop panel (`2880x1800 @ 60Hz`, scale `1.6`, positioned at `(3840, 1284)`).
- **External Display (`DP-2`)**: Connected via a USB-C dock featuring:
  - **DisplayPort-to-HDMI 2.0 Active Protocol Converter** (Parade `PS186` / `186GA1`) exposing DRM connector `/sys/class/drm/card1-DP-2` and kernel HDMI-CEC adapter `/dev/cec0` (`i915` driver).
  - **USB Wired Ethernet Controller** (`enx50a0300cc53a`) alongside the laptop's internal Wi-Fi interface (`wlo1`).
- **Samsung Smart TV (`UN40KU7000`)**:
  - **IP Address**: `192.168.0.2`
  - **MAC Address**: `40:16:3b:d0:4f:8b`
  - **Physical HDMI Port**: HDMI 1 / DVI (CEC Physical Address `1.0.0.0`, TV Logical Address `0`).
  - **Control Channels**:
    1. **Layer 2 Wake-on-LAN (WoL)**: UDP Magic Packets on ports `9` and `7` (wakes the TV mainboard and network controller from deep standby).
    2. **HDMI-CEC (`/dev/cec0`)**: Single-wire serial bus on HDMI pin 13. Used for discrete wake (`IMAGE_VIEW_ON` `0x04`), direct input switching (`ACTIVE_SOURCE` `0x82` -> `1.0.0.0`), standby (`STANDBY` `0x36`), and power state queries (`GIVE_DEVICE_POWER_STATUS` `0x8f`).
    3. **Samsung Tizen WebSocket API (`port 8001`)**: Local LAN/Wi-Fi remote control via `samsungtvws`. Used for turning the TV off (`KEY_POWER`) over Wi-Fi when HDMI-CEC is unavailable (e.g., when undocked) and for fast TCP socket reachability probes (`192.168.0.2:8001`).

---

## 2. Core Components

### 2.1 `tv_power_daemon.py` (`tv-power-sync.service`)
A long-running user-session daemon managed by `systemd --user` (`~/.config/systemd/user/tv-power-sync.service`). It runs an event-driven `GLib.MainLoop` combining **D-Bus signal listeners** with a **150ms hardware sysfs polling timer**.

#### Monitored Inputs & Signals
1. **Internal Display Hardware DPMS (`/sys/class/drm/*-eDP-*/dpms`)**:
   - Polled every `150ms` via `read_hardware_dpms()`.
   - Detects instant hardware display power transitions (`On` <-> `Off`), such as pressing `Escape` on the KDE lock screen, idle DPMS sleep, or waking the screen via mouse/keyboard input.
2. **External Display / Dock Connection (`/sys/class/drm/*-DP-*/status` & `*-HDMI-*/status`)**:
   - Polled every `150ms` via `read_external_display_status()`.
   - Dynamically scans all DisplayPort and HDMI connectors so the dock works regardless of which USB-C port it is plugged into (`DP-1`, `DP-2`, etc.).
3. **Laptop Lid State (`/proc/acpi/button/lid/*/state`)**:
   - Polled every `150ms` via `read_lid_state()`.
   - Detects physical lid close (`open -> closed`) and lid open (`closed -> open`) transitions.
4. **KDE ScreenSaver D-Bus Signal (`org.freedesktop.ScreenSaver.ActiveChanged`)**:
   - Listens on the Session D-Bus for lock (`TRUE`) and unlock (`FALSE`) events.
5. **Systemd Login1 Suspend/Resume Signal (`org.freedesktop.login1.Manager.PrepareForSleep`)**:
   - Listens on the System D-Bus for system suspend (`TRUE`) and resume (`FALSE`) transitions.
6. **Active Connection Watchdog (Every ~3 Seconds / 20 Ticks)**:
   - Continuously verifies that whenever you are logged in (`state == "awake"`), the laptop screen is `On`, the lid is `open`, and the dock is `connected`, the TV remains awake.

---

### 2.2 `boot_wol.py` (`tv-boot-wol.service`)
A system-level oneshot service (`/etc/systemd/system/tv-boot-wol.service`) that runs **once per boot** after `network-online.target` and before `sddm.service` / `display-manager.service`.
- **Singleton Execution**: Configured with `Type=oneshot` and `RemainAfterExit=yes` so it executes once when the network comes up at boot and never re-triggers on subsequent DHCP lease renewals.
- **Safety Checks**:
  1. Verifies the laptop internal panel is not in DPMS `Off`.
  2. Checks if a DRM external connector is `connected`, a known USB-C dock vendor ID (`2109` VIA Labs / `0bda` Realtek) is attached, or the system is on the `192.168.0.x` home subnet.
- **Actions**: Broadcasts a 3-burst Wake-on-LAN magic packet across all NICs and transmits HDMI-CEC `IMAGE_VIEW_ON` + `ACTIVE_SOURCE (1.0.0.0)` so the TV is awake and displaying the login screen by the time SDDM starts.

---

### 2.3 `control_tv.py` (`control_tv`)
An interactive CLI tool installed to `~/.local/bin/control_tv` that provides manual control and diagnostics:
- `control_tv status`: Queries and prints KDE DPMS status (`kscreen-doctor --dpms show`), HDMI-CEC power state (`cec-ctl`), KDE ScreenSaver lock status via D-Bus, and TCP reachability of `192.168.0.2:8001`.
- `control_tv wake`: Executes the full discrete wake sequence (DPMS on, 5-burst multi-NIC WoL, CEC `IMAGE_VIEW_ON` + `ACTIVE_SOURCE`, and `kscreen-doctor` mode re-assertion).
- `control_tv standby`: Cleanly puts the TV into standby (DPMS off, CEC `--standby`, and WebSocket `KEY_POWER` if not already in standby).
- `control_tv wol`: Broadcasts Wake-on-LAN magic packets across all network interfaces.

---

### 2.4 `parse_drm_info.py` (`parse_drm_info`)
A diagnostic utility that runs `drm_info -j` and formats a comprehensive human-readable report of the Intel `i915` GPU, atomic modesetting capabilities, connectors (EDID, bpc, HDR metadata, subconnectors), active CRTC pipes, pixel clocks, and hardware planes.

---

### 2.5 `pair_samsung_tv.py` (`pair_samsung_tv`)
A helper script for initial WebSocket pairing with the Samsung TV (ports `8001` / `8002`) if a new token is ever required.

---

### 2.6 `udev/70-cec-uaccess.rules`
A udev rule deployed to `/etc/udev/rules.d/70-cec-uaccess.rules`:
```udev
KERNEL=="cec*", SUBSYSTEM=="cec", GROUP="video", MODE="0660", TAG+="uaccess"
```
Grants the active desktop user session direct read/write permissions to `/dev/cec0` without requiring `sudo`.

---

## 3. Detailed Operational Workflows & Edge-Case Mitigations

### 3.1 Decisive Wake Sequence (`_do_decisive_wake`)
Triggered when:
- The USB-C dock is plugged in (`disconnected -> connected`).
- The laptop display wakes (`DPMS Off -> On`) while docked.
- The laptop lid opens (`closed -> open`) while docked.
- The system resumes from suspend or unlocks while docked.
- The 3-second watchdog detects the TV is asleep while docked and logged in.

#### Step-by-Step Execution:
1. **Abort / Connection Guard**: Verifies `self.abort_wake` is `False` and `read_external_display_status() == "connected"`.
2. **Shutdown Settling Buffer (3.0s)**:
   - Samsung Tizen TVs take ~3.0 seconds after receiving a power-off command to finish spinning down their internal state. If a wake is triggered within 3.0s of `self.last_off_time`, the worker waits out the remainder of the 3.0s window so the TV microcontroller does not drop the wake request.
3. **Enable KDE DPMS**:
   - Runs `kscreen-doctor --dpms on` asynchronously (with `WAYLAND_DISPLAY=wayland-0` and `QT_QPA_PLATFORM=wayland` guaranteed in the environment).
4. **Fast Exit if Already Awake**:
   - Calls `get_tv_power_state()`. If the TV is already `on` or `to-on`, re-asserts `kscreen-doctor output.<conn>.mode.1` and HDMI-CEC `IMAGE_VIEW_ON` + `ACTIVE_SOURCE` and returns immediately.
5. **Multi-NIC Wake-on-LAN Burst (5 Bursts)**:
   - Sends 5 rapid bursts (`50ms` interval) of the UDP magic packet to `255.255.255.255`, `192.168.0.255`, and `192.168.0.2` on ports `9` and `7`, explicitly binding to every local IPv4 interface (both wired Ethernet and Wi-Fi).
   - **Why WoL is required**: When the Samsung TV enters deep standby, its HDMI-CEC receiver stops responding until a WoL magic packet wakes the mainboard.
6. **Strictly Discrete Wake Commands (Zero Power Inversion)**:
   - **Crucial Design Rule**: WebSocket `KEY_POWER` is a *toggle* key on Samsung Tizen and is **never** sent during the wake sequence. Waking uses *only* discrete, idempotent commands (`WoL` + CEC `IMAGE_VIEW_ON` `0x04` + `ACTIVE_SOURCE` `0x82`), making it physically impossible for a wake routine to accidentally toggle an already-on TV off.
7. **CEC Logical Address Allocation & Active Source**:
   - Hotplugging the dock causes the kernel to drop the CEC logical address (`Not Allocated`). Every CEC transmission explicitly chains `cec-ctl -d /dev/cec0 --playback` first to claim Logical Address `4` (`Playback Device 1`) before sending `--from 4 --to 0 --image-view-on` and `--from 4 --active-source phys-addr=1.0.0.0`.
8. **Verification Loop & Post-Wake Video Mode Re-assertion (`kscreen-doctor output.<conn>.mode.1`)**:
   - Polls `get_tv_power_state()` every `1.5s` for up to 10 attempts, re-sending WoL bursts and CEC opcodes until the TV confirms `on` or `to-on` (or aborts immediately if the dock is unplugged mid-loop).
   - **Why Post-Wake Mode Re-assertion is required**: While the laptop internal panel wakes in milliseconds, the TV takes ~4–6 seconds to boot from standby. When KWin initially enables DPMS at `t = 0s`, the TV's HDMI receiver is still unpowered, leaving the dock's Parade PS186 DP-to-HDMI converter without an active TMDS clock handshake. As soon as the TV confirms awake (`on`), the daemon runs `kscreen-doctor output.<conn>.mode.1` to force KWin to push the native `3840x2160@60Hz` stream to the now-ready HDMI sink, preventing a "No Signal" screen.
9. **60-Second Backoff**:
   - If the TV does not wake after 10 attempts (e.g., unplugged from AC wall power), the daemon backs off for `60s` (`self.wake_backoff_until`). Any hardware connection transition immediately clears this backoff.

---

### 3.2 Wi-Fi Sleep on Undock & Route-Failover Retry (`send_tv_sleep_wifi`)
Triggered when:
- The USB-C dock cable is unplugged (`connected -> disconnected`).
- The laptop lid is closed (`open -> closed`).

#### Why Undocking Requires Special Handling:
1. **Severed HDMI-CEC**: Unplugging the dock physically disconnects `/dev/cec0`, so HDMI-CEC `STANDBY` cannot reach the TV. Without a Wi-Fi sleep command, the TV stays powered on displaying a floating "Input Disconnected / Check signal cable" screen.
2. **Simultaneous Ethernet-to-Wi-Fi Route Failover**:
   - Because the USB-C dock contains the wired Ethernet NIC (`enx50a0300cc53a`), unplugging the dock tears down the wired interface at the exact same millisecond the display disconnects.
   - NetworkManager takes `~0.2s – 1.5s` to cancel the DHCP lease on the removed USB NIC and switch the default IPv4 route over to Wi-Fi (`wlo1`).

#### Step-by-Step Execution:
1. **Immediate Wake Cancellation**: Sets `self.abort_wake = True` so any in-flight wake thread terminates immediately.
2. **Duplicate Toggle Cooldown Guard (`self.tv_commanded_off`)**:
   - Checks if `self.tv_commanded_off` is `True` within the last `12.0s`.
   - When the Samsung TV receives `KEY_POWER` to turn off, its TCP port `8001` remains open for ~4–8 seconds while Tizen shuts down. If you unplug the dock (which sends `KEY_POWER`) and then 2 seconds later close the laptop lid or lock the screen, this 12-second guard prevents sending a second `KEY_POWER` toggle that would turn the TV back on.
3. **Skip Dead CEC on Undock**:
   - When `is_undock=True` (or the external connector is `disconnected`), the worker bypasses `cec-ctl` completely so it doesn't waste 1 second timing out on a disconnected cable.
4. **Multi-Attempt Wi-Fi Retry Loop**:
   - Retries up to **10 times** (`0.5s` interval, up to 5 seconds total):
     - Checks if the dock was reconnected (`abort` sleep if re-docked).
     - Probes `is_tv_network_awake(timeout=0.6)` on `192.168.0.2:8001`.
     - As soon as NetworkManager finishes failing over to `wlo1`, transmits `send_tv_websocket_key("KEY_POWER")`, records `self.tv_commanded_off = True` and `self.last_off_time = time.time()`, and exits cleanly.

---

### 3.3 Idle / Lock Screen Sleep (`handle_screen_off`)
Triggered when:
- Laptop internal display hardware DPMS transitions `On -> Off`.
- System prepares to suspend (`PrepareForSleep(True)`).

#### Step-by-Step Execution:
1. Sets `self.state = "asleep"` and `self.abort_wake = True`.
2. **Automatic Session Lock**: If triggered by hardware DPMS going `Off`, invokes `loginctl lock-session` asynchronously.
3. **12-Second Cooldown & Standby Check**:
   - Skips if `self.tv_commanded_off` was already set within the last `12.0s`.
   - Queries `get_tv_power_state(use_cec=is_connected)`. If already in `standby` or `to-standby`, skips power toggle.
4. **Dual-Path Standby**:
   - If docked, sends `cec-ctl -d /dev/cec0 --playback --to 0 --standby`.
   - If `is_tv_network_awake(timeout=0.6)` is `True`, sends WebSocket `KEY_POWER` and sets `self.tv_commanded_off = True`.

---

### 3.4 Resilient Power State Detection & Watchdog (`get_tv_power_state`)
HDMI-CEC is a slow (`~400 bps`) single-wire bus that occasionally experiences bus contention (`Tx, Aborted, Max Retries`).
- **Two-Tier Power State Check**:
  1. **Primary (HDMI-CEC)**: Runs `cec-ctl -d /dev/cec0 --playback --to 0 --give-device-power-status`. If the TV responds with `pwr-state: on`, `standby`, `to-on`, or `to-standby`, that authoritative state is returned.
  2. **Fallback (TCP Port `8001` Probe)**: If CEC fails or times out, `is_tv_network_awake(timeout=0.6)` performs a non-blocking TCP `connect_ex` to `192.168.0.2:8001`. When the TV is on, port `8001` responds in `< 1ms` on Ethernet / `< 20ms` on Wi-Fi and returns `"on"`. When the TV is in standby, port `8001` is closed and returns `"offline"`.
- **Watchdog Safeguards**:
  - Runs every 20 poll ticks (`~3.0s`) when `state == "awake"`, `eDP-1 == "On"`, `dock == "connected"`, and `lid == "open"`.
  - Re-verifies `read_external_display_status() == "connected"` *after* `get_tv_power_state()` returns so unplugging the dock mid-query never triggers a false wake.
  - Triggers an immediate wake if CEC explicitly reports `standby` or `to-standby`, or after **2 consecutive** `"offline"` readings (`self.offline_streak >= 2`).

---

## 4. State & Event Flow Summary

| Event / Trigger | Detection Mechanism | Daemon Action |
| :--- | :--- | :--- |
| **System Boot (Docked)** | `tv-boot-wol.service` (`oneshot` after network online, before SDDM) | Broadcasts 3x WoL across all NICs + CEC `IMAGE_VIEW_ON` & `ACTIVE_SOURCE (1.0.0.0)`. |
| **Dock Plugged In** | `/sys/class/drm/*-DP-*/status` (`disconnected -> connected`) | Clears backoff, runs `_do_decisive_wake` (DPMS on, 5x WoL, CEC HDMI 1, re-asserts `4K60` mode). |
| **Dock Unplugged** | `/sys/class/drm/*-DP-*/status` (`connected -> disconnected`) | Sets `abort_wake`, skips dead CEC, retries WebSocket `KEY_POWER` over Wi-Fi across route failover. |
| **Laptop Lid Closed** | `/proc/acpi/button/lid/*/state` (`open -> closed`) | Sends CEC `--standby` (if docked) + Wi-Fi `KEY_POWER` sleep; sets state to `asleep`. |
| **Laptop Lid Opened** | `/proc/acpi/button/lid/*/state` (`closed -> open`) | If docked, runs `_do_decisive_wake`. |
| **Idle DPMS / Esc on Lock** | `/sys/class/drm/*-eDP-*/dpms` (`On -> Off`) | Locks session (`loginctl lock-session`), sends CEC `--standby` + WebSocket `KEY_POWER`. |
| **Mouse / Key Wake** | `/sys/class/drm/*-eDP-*/dpms` (`Off -> On`) | Enables KDE DPMS; if docked, runs `_do_decisive_wake` and syncs `4K60` video stream once TV wakes. |
| **TV Turned Off While Working** | 3-second Watchdog (`poll_counter >= 20`) | Detects `standby` or `2x offline` while logged in & docked; runs `_do_decisive_wake`. |
