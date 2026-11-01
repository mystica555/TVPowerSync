# Detailed Architecture & Technical Reference

This document explains the hardware topology, protocols, event sources, state
machine and edge-case mitigations of **TizenPowerSync**.

---

## 1. System Overview & Hardware Topology

The suite synchronizes power, input selection and the video link of a
**Samsung Tizen Smart TV (`UN40KU7000`)** used as a **4K60 UHD** external
monitor for a Linux laptop running **KDE Plasma 6 (Wayland)**.

### Hardware & Interface Stack
- **Internal Display (`eDP-1`)**: laptop panel (`2880x1800 @ 60Hz`, scale
  `1.6`, positioned at `(3840, 1284)`).
- **External Display (`DP-2`)**: connected through a USB-C dock featuring:
  - a **DisplayPort-to-HDMI 2.0 active converter** (Parade `PS186`) exposing
    DRM connector `/sys/class/drm/card1-DP-2` and HDMI-CEC adapter `/dev/cec0`
    (`i915` driver);
  - a **USB wired Ethernet controller** (`enx50a0300cc53a`) alongside the
    laptop's Wi-Fi interface (`wlo1`).
- **Samsung Smart TV (`UN40KU7000`)**:
  - IP `192.168.0.2`, MAC `40:16:3b:d0:4f:8b`
  - HDMI 1 / DVI, CEC physical address `1.0.0.0`, TV logical address `0`
  - Control channels: **WoL** (UDP 9/7), **HDMI-CEC** (`/dev/cec0`), and the
    **Tizen WebSocket API** (port `8001`).

All of these are [configurable](#5-configuration) via `TV_IP`, `TV_MAC`, `CEC_DEVICE`, `TV_MODE`
and `TV_LOG_LEVEL` (see [`tvlib.Config`](#27-tvlibpy)).

---

## 2. Core Components

### 2.1 `tv_power_daemon.py` (`tv-power-sync.service`)

A long-running user-session daemon driving a `GLib.MainLoop`. The main loop does
only cheap work — sysfs reads, socket reads and scheduling. Every CEC, WoL,
WebSocket and `kscreen-doctor` operation runs in a worker thread.

#### Power intent model

Every request to turn the TV on or off creates an `Intent`:

```
Intent(seq, kind, cancel_event)
```

- Creating a new intent increments `seq` and sets the previous intent's
  `cancel` event.
- Workers check `cancel` before each side effect: before a CEC query, before
  each WoL burst, before a WebSocket key, between wake-verification attempts.
- The CEC controller re-checks cancellation *inside* its process lock, so a
  superseded intent cannot transmit a stale command after waiting for the
  adapter.
- Duplicate ON requests are ignored while an ON intent is active; duplicate OFF
  requests are absorbed by the 12-second cooldown and the pre-flight CEC state
  query.

This replaces the previous `abort_wake` boolean, which conflated “cancel the
wake” with “sleep now” and could not stop an in-flight sleep retry.

#### Monitored inputs

1. **Kernel Netlink KObject UEvent socket**
   - Binds multicast group 1 (`NETLINK_KOBJECT_UEVENT`), raises `SO_RCVBUF` to
     1 MiB, and attaches to `GLib.io_add_watch`.
   - Only `SUBSYSTEM=drm` events with `HOTPLUG=1`/`add`/`remove`, and
     `SUBSYSTEM=usb` `add`/`remove` events are considered. USB HID/hub `change`
     traffic and the daemon's own DPMS changes are ignored.
   - Events are coalesced by a 400ms one-shot timer, because USB-C/PD
     renegotiation and MST setup emit bursts and transient `connected` reads.
   - If the netlink bind fails, `on_dpms_poll` calls
     `check_external_display_transition()` every 2 seconds. This is the fallback
     the previous version only logged about.

2. **Laptop lid (`login1.PropertiesChanged`, `LidClosed`)**
   - Read once at startup via D-Bus with a `/proc/acpi` fallback; afterwards
     purely event-driven. The handler filters on the `login1.Manager` interface.

3. **Adaptive hardware DPMS tracking**
   - Reads `/sys/class/drm/*-eDP-*/dpms` (falling back to LVDS/DSI).
   - 250ms while the session is locked, 2000ms while unlocked. sysfs has no
     inotify/poll support, so a timer is the correct mechanism; the cost is a
     few microseconds per read.
   - If the node disappears (GPU reset, connector rename), the path is
     re-resolved. `Unknown` is never treated as “screen off”.

4. **KDE ScreenSaver (`org.freedesktop.ScreenSaver.ActiveChanged`)**
   - Switches the DPMS polling interval and drives unlock-time wake.
   - Registration is retried every 5 seconds if the session bus is not ready
     when the user unit starts.

5. **`PrepareForSleep`**
   - Suspend creates sleep intent.
   - Resume waits 2 seconds for KWin to restore DPMS, then reconciles. It never
     forces `kscreen-doctor --dpms on`, and it does nothing while the lid is
     closed.

6. **Watchdog (5-second GLib timer, worker-thread check)**
   - Preconditions: state `awake`, panel DPMS `On`, dock `connected`, lid open,
     no active wake, backoff expired.
   - Probes TCP `8001` first (no subprocess). Only when the port is closed does
     it query CEC.
   - A CEC `standby`/`to-standby` reading wakes immediately; `offline` requires
     two consecutive checks. The dock state is re-read after the query so an
     unplug during the check cannot trigger a false wake.
   - Failed wake attempts apply [exponential backoff](#35-watchdog-backoff) (60s, 120s, 240s, ... up to
     900s). Backoff resets on dock connect, DPMS-on, lid-open or unlock.

### 2.2 `boot_wol.py` (`tv-boot-wol.service`)

A system-level oneshot (`RemainAfterExit=yes`) running once per boot after
`network-online.target` and before the display manager. It skips when the panel
is in DPMS Off, checks for a connected DRM connector, a known USB-C dock vendor
(`2109` VIA Labs / `0bda` Realtek), or the `192.168.0.x` home subnet, then sends
three WoL bursts plus CEC `IMAGE_VIEW_ON` + `ACTIVE_SOURCE`.

### 2.3 `control_tv.py`

Manual CLI for `status`, `wake`, `standby` and `wol`. Standby follows the same
single-path rule as the daemon: CEC standby when docked, WebSocket `KEY_POWER`
only as a fallback. All commands use list-form `subprocess` calls (no shell).

### 2.4 `parse_drm_info.py`

Formats `drm_info -j` output: driver/caps, connectors and EDID, bpc/HDR
metadata, CRTC timings, and plane usage. It reads `argv[1]` when given, otherwise
`TV_DRM_INFO` (default `drm_info.json` in the working directory) and falls back
to running `drm_info -j` when the file is missing.

### 2.5 `pair_samsung_tv.py`

One-off WebSocket pairing helper (ports 8002/8001). Uses `TV_IP` and writes the
token to `TV_TOKEN_FILE` (default `~/.local/state/tizenpowersync/tv_token.txt`).

### 2.6 `udev/70-cec-uaccess.rules`

```
KERNEL=="cec*", SUBSYSTEM=="cec", GROUP="video", MODE="0660", TAG+="uaccess"
```

Grants the active desktop user access to `/dev/cec0` without root.

### 2.7 `tvlib.py`

Shared config, logging, WoL, CEC, WebSocket, DRM-sysfs and backoff helpers. The
daemon, CLI and boot service all import it, so `send_wol`, power-state parsing
and connector discovery exist in exactly one place. `TvLink` holds the optional
kept-warm remote-control WebSocket; `send_key` remains the one-shot fallback.

---

## 3. Operational Workflows

### 3.1 Decisive Wake (`_do_decisive_wake`)

Triggered by dock connect, DPMS Off→On, lid open (docked), resume (docked,
lid open) and the watchdog.

1. **Cancellation / connection guard** — abort if the intent was superseded or
   the external display is disconnected.
2. **Settle window** — if the TV was commanded off less than 3s ago, wait out
   the remainder. The wait is interruptible by a new intent. A reported `on`
   during this window is ignored (Tizen can keep port 8001 open while shutting
   down): the pre-wake CEC query is skipped and the full WoL + CEC sequence runs
   immediately after the settle.
3. **Clear the off cooldown**, then ensure KDE DPMS is on *only if the panel is
   currently off* (avoids spawning a Qt process on every wake).
4. **Fast path** — query CEC. If already `on`/`to-on`, re-assert the video mode
   and CEC active source and return.
5. **Wake** — 5 WoL bursts (one `ip` enumeration cached for the whole sequence)
   plus a single `cec-ctl --playback --to 0 --image-view-on --active-source`
   invocation.
6. **Verification loop** — up to 10 attempts, polling TCP every 0.75s for the
   first four attempts and every 1.5s afterwards. WoL is re-sent each round; the
   blocking CEC wake is repeated only every third round so it does not delay the
   polls. The loop exits immediately if the intent is cancelled or the dock is
   unplugged.
7. **Video mode re-assert** — `kscreen-doctor output.<connector>.mode.<TV_MODE>`.
   Using the mode string rather than a positional index survives mode-table
   changes.
8. **[Backoff](#35-watchdog-backoff)** — a failed, still-current wake doubles the delay (max 15min).
   Superseded wakes do not apply backoff.

`KEY_POWER` is never sent during a wake: Samsung treats it as a toggle, so wake
uses only discrete commands.

### 3.2 Sleep (`_power_off_worker`)

Triggered by DPMS off, lid close, dock unplug and suspend.

1. **Cancel any in-flight wake** via the new intent.
2. **Cooldown** — skip if a sleep command was delivered less than 12s ago.
3. **Pre-flight state** — CEC query; if already `standby`/`to-standby`, mark the
   cooldown and stop.
4. **Single path** — when docked, send one CEC `STANDBY`. If and only if that
   fails, fall back to WebSocket `KEY_POWER`. The previous implementation sent
   both unconditionally, so the toggle could wake a TV the standby had just
   turned off.
5. **Warm control channel** — while docked and awake, the daemon holds one
   WebSocket to the TV (`tvlib.TvLink`) and pings it every 10s. Tizen otherwise
   stops answering on port 8001 after a few idle minutes, which made the undock
   path spend 6-8s in TCP retries before `KEY_POWER` could go out. The sleep key
   reuses this socket with `key_press_delay=0`, so only the server round trip
   remains. The session is sourced from the Wi-Fi address, because a socket
   bound to the dock's Ethernet address is destroyed at the moment of undock;
   the sleep worker tries the warm link before any probe. The link is closed
   while the TV is expected to be off, and reopened automatically after a wake.
6. **Undock retry** — after the dock is removed, CEC is gone and NetworkManager
   is failing over from the dock NIC to Wi-Fi. The worker probes reachability
   with a 0.25s TCP timeout and retries every 0.25s for up to 20 attempts
   (~10s), aborting if the dock returns or the intent is cancelled. Wake-on-LAN
   interface discovery uses ioctl rather than a forked `ip` process, so a dock
   re-enumeration cannot stall the wake path for seconds.

### 3.3 Screen Off / Lock

`handle_screen_off` sets state to `asleep`, runs `loginctl lock-session` with
`XDG_SESSION_ID` when the trigger was idle DPMS, and creates a sleep intent.
The intent queue handles duplicate OFF requests; there is no longer a
“state is already asleep” early return that could permanently miss a command.

At startup, if the panel is already `Off`, the daemon creates a sleep intent
after 500ms instead of assuming the TV is already off.

A panel `On` transition is debounced for 800ms before it wakes the TV. KDE's
locker re-asserts DPMS around a blank/restore, and waking on that bounce meant
the next sleep request was cancelled by the wake it had just started. The timer
is dropped when the panel goes `Off` again, so a sleep request is never
superseded by a bounce, while a genuine pointer or keyboard wake at the lock
screen still starts the normal wake path. Startup reconciliation skips the wake
while the session is locked, so a daemon restart cannot toggle the TV.

### 3.4 Suspend / Resume

`PrepareForSleep(true)` creates a sleep intent. systemd does not wait for
D-Bus signal handlers, so the command is best-effort (documented caveat).
`PrepareForSleep(false)` schedules a reconcile 2s later: if the lid is open and
the panel came back `On`, it runs the normal wake path; otherwise it leaves the
TV alone and waits for the DPMS poll.

### 3.5 Watchdog Backoff

| Consecutive failure | Next attempt |
| :--- | :--- |
| 1 | 60s |
| 2 | 120s |
| 3 | 240s |
| 4 | 480s |
| 5+ | 900s (cap) |

Any user-intent transition (dock connect, DPMS-on, lid open, unlock) resets the
delay to 60s. This prevents the old behavior of broadcasting WoL forever, once
per ~75s, when the TV is unplugged from AC or deliberately off.

---

## 4. Event Flow Summary

| Event | Detection | Action |
| :--- | :--- | :--- |
| Boot (docked) | `tv-boot-wol.service` after network-online | 3x WoL + CEC wake |
| Dock plugged | netlink uevent, 400ms debounce | reset backoff, decisive wake |
| Dock unplugged | netlink uevent, 400ms debounce | cancel wake, Wi-Fi sleep with route-failover retry |
| Lid closed | `login1` PropertiesChanged | sleep intent |
| Lid opened | `login1` PropertiesChanged | wake if docked |
| Idle DPMS / Esc | panel DPMS On→Off | lock session, sleep intent |
| Mouse/key wake | panel DPMS Off→On | DPMS on (if needed), wake |
| TV off while working | 5s watchdog (thread) | CEC state / 2× offline → wake, with backoff |
| Suspend | `PrepareForSleep(true)` | sleep intent |
| Resume | `PrepareForSleep(false)` + 2s | reconcile only if lid open and panel on |

---

## 5. Configuration

| Variable | Default | Used by |
| :--- | :--- | :--- |
| `TV_IP` | `192.168.0.2` | daemon, CLI, boot, pairing |
| `TV_MAC` | `40:16:3b:d0:4f:8b` | WoL |
| `CEC_DEVICE` | `/dev/cec0` | daemon, CLI, boot |
| `TV_MODE` | `3840x2160@60` | mode re-assert |
| `TV_LOG_LEVEL` | `INFO` | all scripts |
| `TV_PYTHON` | `$XDG_DATA_HOME/tizenpowersync/venv/bin/python` | `deploy.sh` interpreter override |
| `TV_TOKEN_FILE` | `~/.local/state/tizenpowersync/tv_token.txt` | pairing helper |
| `TV_DRM_INFO` | `drm_info.json` (cwd) | `parse_drm_info` snapshot |

[`deploy.sh`](README.md#deployment) keeps the venv outside the clone, at
`$XDG_DATA_HOME/tizenpowersync/venv` (default
`~/.local/share/tizenpowersync/venv`), so the checkout can be moved or deleted
after deployment. When the venv is missing the script creates it with
`--system-site-packages` and installs `samsungtvws`; `TV_PYTHON` overrides the
interpreter. If an existing venv cannot see the system bindings it writes a
version-matched `000-system-bindings.pth` into the venv (the same effect as the
flag) rather than re-creating it. It substitutes the selected interpreter into
the user and boot units (`@PYTHON@`, `@BIN_DIR@`), rewrites the installed script
shebangs to match, fails if no interpreter can import `dbus`/`gi`, and warns if
`samsungtvws` is missing.
