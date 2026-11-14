# Detailed Architecture & Technical Reference

This document explains the hardware topology, protocols, event sources, state
machine and edge-case mitigations of **TVPowerSync**.

---

## 1. System Overview & Hardware Topology

The suite synchronizes power, input selection and the video link of a TV used
as a **4K60 UHD** external monitor for a Linux laptop running **KDE Plasma 6
(Wayland)**. Samsung Tizen is the reference backend; the same daemon also
drives generic HDMI-CEC, Roku ECP, Android TV Remote v2, Fire TV ADB, LG webOS
and Vizio SmartCast backends.

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
  `cancel` event. Sleep intents (and daemon shutdown) also cancel pending
  panel-on wake hints/debounce work and any scheduled post-wake source-reassert
  or DP-retrain thread.
- Workers check `cancel` before each side effect: before a CEC query, before
  each WoL burst, before a WebSocket key, between wake-verification attempts,
  and before post-wake routing or link-retrain retries.
- The CEC controller re-checks cancellation *inside* its process lock, so a
  superseded intent cannot transmit a stale command after waiting for the
  adapter.
- Duplicate ON requests are ignored while an ON intent is active; duplicate OFF
  requests are absorbed by the 12-second cooldown (cleared by any wake request or
  early `IMAGE_VIEW_ON` pre-wake, and re-armed only by uncancelled sleeps) and
  the pre-flight CEC state query.

This replaces the previous `abort_wake` boolean, which conflated “cancel the
wake” with “sleep now” and could not stop an in-flight sleep retry.

#### Monitored inputs

Lid position, dock/display hotplug, KDE screen-lock state and suspend/resume are
event-driven. The internal panel's DPMS state is the only routinely polled
input; external-display state falls back to a 2-second poll only if the netlink
listener is unavailable. The lid is read once at startup and never polled.

1. **Kernel Netlink KObject UEvent socket**
   - Binds multicast group 1 (`NETLINK_KOBJECT_UEVENT`), raises `SO_RCVBUF` to
     1 MiB, and attaches to `GLib.io_add_watch`.
   - Only `SUBSYSTEM=drm` events with `HOTPLUG=1`/`add`/`remove`, and
     `SUBSYSTEM=usb` `add`/`remove` events are considered. USB HID/hub `change`
     traffic and the daemon's own DPMS changes are ignored.
   - Events are coalesced by a 400ms one-shot timer, because USB-C/PD
     renegotiation and MST setup emit bursts and transient `connected` reads.
   - If the netlink listener is unavailable (bind failure or socket error),
     `on_dpms_poll` calls `check_external_display_transition()` every 2 seconds.
     This is the fallback the previous version only logged about.

2. **Laptop lid (`login1.PropertiesChanged`, `LidClosed`)**
   - The state is read **once at startup** via the system D-Bus. If that query
     fails, `/proc/acpi/button/lid/*/state` is read once as a bootstrap
     fallback; it is not used again.
   - Afterwards, transitions arrive as `PropertiesChanged` signals on
     `org.freedesktop.login1.Manager`. The handler filters on that interface
     and `LidClosed`, then updates the cached state.
   - There is no periodic lid polling. If the system-bus signal receiver cannot
     be registered, the daemon retries registration every 5s; it does not fall
     back to ACPI polling, so lid events are unavailable until registration
     succeeds.

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
   - Probes the selected backend's control port first (no subprocess). Only
     when the port is closed does it query CEC.
   - A CEC `standby`/`to-standby` reading wakes immediately; `offline` requires
     two consecutive checks. After the blocking query returns, both the watchdog
     and startup reconciliation re-check the intent sequence and wake
     preconditions (awake intent, panel DPMS `On`, dock connected, lid open) so a
     sleep, screen-off, lid close or unplug during the check cannot trigger a
     stale wake.
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
single-path rule as the daemon: CEC standby when docked, then the selected
backend's network fallback (WebSocket, ECP, ADB, or vendor API). All commands
use list-form `subprocess` calls (no shell).

### 2.4 `parse_drm_info.py`

Formats `drm_info -j` output: driver/caps, connectors and EDID, bpc/HDR
metadata, CRTC timings, and plane usage. It reads `argv[1]` when given, otherwise
`TV_DRM_INFO` (default `drm_info.json` in the working directory) and falls back
to running `drm_info -j` when the file is missing.

### 2.5 `pair_samsung_tv.py`

One-off WebSocket pairing helper (ports 8002/8001). Uses `TV_IP` and writes the
token to `TV_TOKEN_FILE` (default `~/.local/state/tvpowersync/tv_token.txt`).

### 2.6 `udev/70-cec-uaccess.rules`

```
KERNEL=="cec*", SUBSYSTEM=="cec", GROUP="video", MODE="0660", TAG+="uaccess"
```

Grants the active desktop user access to `/dev/cec0` without root.

### 2.7 `tvlib.py`

Shared config, logging, WoL, CEC, Samsung remote transports, DRM-sysfs and
backoff helpers. The daemon, CLI, boot service and backends all import it, so
`send_wol`, power-state parsing and connector discovery exist in exactly one
place. `TvLink` holds the optional kept-warm Tizen WebSocket, `LegacyLink` the
pre-2016 TCP session (adapted from `samsungctl`; see
`THIRD_PARTY_NOTICES.md`), and `send_key` remains the one-shot WebSocket
fallback.

### 2.8 `tv_backends.py`

Vendor power policy behind a single interface:

```
power_state(cancel, allow_network_fallback) -> on | standby | to-on | to-standby | offline
prewake(cancel) / cec_prewake(cancel) -> early WoL / IMAGE_VIEW_ON hint
wake(cancel) / nudge(cancel) -> one full attempt / a cheap repeat
standby(cancel, undock, reason, should_abort) -> delivered?
active_source(cancel) / keepalive() / close()
```

`active_source(cancel)` delegates to `CecController.active_source(cancel)`
(`SET_STREAM_PATH` + `ACTIVE_SOURCE` only, without `IMAGE_VIEW_ON`), so a
routing call never powers on a TV that is entering standby.

`TvBackend` supplies the CEC + WoL baseline; `CecBackend` is the vendor-neutral
choice. `SamsungBackend` adds the Samsung remote API — the Tizen WebSocket, or
the pre-2016 TCP protocol on port 55000 (or `TV_SAMSUNG_PORT`) when
`TV_SAMSUNG_PROTOCOL=legacy` — and the proven-toggle fallback. `RokuBackend`
uses ECP (`/query/device-info`, `PowerOn`, `PowerOff`).
`AndroidTvBackend` uses Android TV Remote v2 and expects `pair_android_tv.py` to
have saved a client certificate. `FireTvBackend` uses ADB `dumpsys power` and
key codes 223/224. `WebOsBackend` uses `aiowebostv`/SSAP and expects the client
key saved by `pair_lg_tv.py`; `VizioBackend` uses `vizaio` and expects the auth
token saved by `pair_vizio_tv.py`. Both use the vendor API for state and
standby, and WoL/CEC for wake. All backends try CEC first when an adapter is
configured; set `CEC_DEVICE=` to control a streaming box's own power instead of
the TV it is connected to.

### 2.9 `pair_lg_tv.py`

One-off LG webOS pairing helper. Connects to `TV_IP` with `aiowebostv`, waits
for the on-screen allow prompt, and writes the returned client key to
`LG_CLIENT_KEY_FILE` (default
`~/.local/state/tvpowersync/lg_client_key.txt`, mode `0600`).

### 2.10 `pair_vizio_tv.py`

One-off Vizio SmartCast pairing helper. Connects to `TV_IP:TV_VIZIO_PORT`,
starts a pairing session, asks for the PIN shown on the TV, and writes the
durable auth token to `VIZIO_TOKEN_FILE` (default
`~/.local/state/tvpowersync/vizio_token.txt`, mode `0600`). Use
`TV_VIZIO_PORT=9000` on older firmware.

---

## 3. Operational Workflows

### 3.1 Decisive Wake (`_do_decisive_wake`)

Triggered by dock connect, DPMS Off→On, lid open (docked), resume (docked,
lid open) and the watchdog. The protocol steps below describe the Samsung
backend; the daemon contract is identical for every backend, which substitutes
its own wake command and readiness probe.

1. **Cancellation / connection guard** — abort if the intent was superseded or
   the external display is disconnected.
2. **Settle window** — if the TV was commanded off less than 3s ago, wait out the
   remainder. The wait is interruptible by a new intent. A reported `on`
   during this window is ignored (Tizen can keep port 8001 open while shutting
   down): the pre-wake CEC query is skipped, and the early WoL hint plus full
   WoL + CEC sequence run immediately after the settle. If a sleep worker is
   still on the wire, the wake waits for it (up to 3s) and re-checks the off
   timestamp after the blocking state query, so a wake can no longer declare
   success while the TV is still shutting down.
3. **Opportunistic wake hints** — the moment the panel reports `On`, before the
   800ms debounce and the settle window, send three magic-packet bursts. When
   the daemon itself commanded standby and no sleep worker is still on the
   wire, also send `IMAGE_VIEW_ON` right there: the magic packet alone does not
   boot this set, and otherwise the debounce sits between the mouse and the red
   light. Both this hint and the debounce share the panel-on transition's
   cancellation event, so a DPMS `Off` bounce or sleep intent aborts pending
   hints instead of letting them land after standby starts; transmitting that
   early `IMAGE_VIEW_ON` also marks a wake attempt since the last sleep so a
   quick bounce back off still transmits standby. Every other trigger waits out
   the debounce, so a transient DPMS bounce cannot boot the TV for nothing. The
   full wake sequence repeats both, which is harmless.
4. **Clear the off cooldown** (both when a wake intent is created and before
   any early `IMAGE_VIEW_ON` pre-wake), then ensure KDE DPMS is on *only if the
   panel is currently off* (avoids spawning a Qt process on every wake).
5. **Fast path** — send a bare CEC `IMAGE_VIEW_ON` (no `ACTIVE_SOURCE`, which
   a booting set would drop), then query CEC when the TV was not recently
   commanded off. The image-view-on is harmless on an awake set and is what
   starts a sleeping one booting, so the query's 1-3s overlaps the boot
   instead of delaying it. Only a settled `on` (with EDID) re-asserts the video
   mode and CEC active source and returns. A set that was commanded off moments
   ago skips both the query and the extra image-view-on: it
   costs 2-3s and can only answer standby, so the full sequence starts at once.
   The query never uses the network fallback, because the control port stays
   open in standby and would report `on`. A `to-on`/`to-standby` set is still
   booting: it acknowledges CEC power-on but drops an `ACTIVE_SOURCE` sent in
   that window, so it takes the full sequence instead.
6. **Wake** — 5 WoL bursts (one `ip` enumeration cached for the whole sequence)
   plus a single `cec-ctl --playback --to 0 --image-view-on --set-stream-path ... --active-source ...`
   invocation.
7. **Verification loop** — up to 10 attempts, polling TCP every 0.5s for the
   first four attempts and every 1.5s afterwards. WoL is re-sent each round; the
   blocking CEC wake is repeated every second round so it does not delay the
   polls. The loop exits immediately if the intent is cancelled or the dock is
   unplugged.
8. **Video mode re-assert** — `kscreen-doctor output.<connector>.mode.<TV_MODE>`,
   followed by `SET_STREAM_PATH` + `ACTIVE_SOURCE` so a TV left on another
   input switches back. Using the mode string rather than a positional index
   survives mode-table changes. While the TV reports on without EDID (awake on
   another input), the verification loop re-sends the active source every round
   instead of only broadcasting WoL. Every successful wake also schedules the
   same routing again 1s, 2.5s and 4s later, because a set that has just powered
   on accepts CEC frames before its inputs are ready and would otherwise settle
   on "No Signal" with the first, dropped `ACTIVE_SOURCE`. The first retry is
   deliberately early: the control port opens while the set is still booting, so
   a 3s first retry made the picture itself wait ~3s. The last repeat is
   preceded by a DP link retrain (disable the output, re-enable it, then
   re-assert the native mode), which recovers a TV whose HDMI receiver stayed
   out of sync after a fast standby/wake cycle even though the GPU still
   reports an active link with EDID. The retrain only runs when that wake had
   to wait for an in-flight sleep command (the race that produces the desync),
   and at most once a minute, so ordinary cold wakes never blank the screen.
   The entire post-wake retry and retrain sequence runs under its own
   cancellation event, which is cleared immediately by any sleep request or
   daemon shutdown, re-checks intent state before retraining or re-asserting,
   and sends only `SET_STREAM_PATH` + `ACTIVE_SOURCE` (never `IMAGE_VIEW_ON`) so
   it can never wake a TV back out of standby. A `to-on` reading in the
   verification loop is not treated as awake; only a settled `on` (or a silent
   CEC with a reachable control port) ends the wake.
9. **[Backoff](#35-watchdog-backoff)** — a failed, still-current wake doubles the delay (max 15min).
   Superseded wakes do not apply backoff.

`KEY_POWER` is never sent during a wake: Samsung treats it as a toggle, so wake
uses only discrete commands.

### 3.2 Sleep (`_power_off_worker`)

Triggered by DPMS off, lid close, dock unplug and suspend. The delivery-proof
steps below are the Samsung backend's; Roku ECP, Android TV Remote, Fire TV
ADB, LG webOS and Vizio SmartCast substitute their own switch-off command and
confirmation.

1. **Cancel any in-flight wake** via the new intent, also cancelling any
   pending DPMS-on hint/settle work and any scheduled post-wake source-reassert
   or DP-retrain thread.
2. **Cooldown** — skip if an uncancelled sleep command was delivered less than
   12s ago and no wake request or early `IMAGE_VIEW_ON` pre-wake has occurred
   since then. A cancelled sleep does not preserve cooldown suppression across an
   intervening wake.
3. **Pre-flight state** — CEC query; if already `standby`/`to-standby`, mark the
   cooldown and stop.
4. **Single path with delivery proof** — when docked, send one CEC `STANDBY`.
   `cec-ctl` exits 0 even when the frame is not acknowledged (`Tx, Not
   Acknowledged`), and the TV does not act on a NACKed frame. A missing ACK is
   therefore treated as failure: the daemon re-checks the CEC power state, then
   falls back to WebSocket `KEY_POWER`. After an acknowledged standby it also
   watches CEC power status for a few seconds, re-sending the standby if the TV
   still reports `on`. The previous implementation trusted the exit status, so
   a NACKed standby was logged as delivered and the TV stayed on. The toggle is
   sent only after the standby has been proven not to have landed, so it cannot
   undo a successful standby.
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
The TV is kept a mirror of the panel: a panel `On` at the lock screen wakes it
so the password entry box is visible, and a panel `Off` puts it back to sleep.
The intent queue handles duplicate OFF requests; there is no longer a
“state is already asleep” early return that could permanently miss a command.

At startup, if the panel is already `Off`, the daemon creates a sleep intent
after 500ms instead of assuming the TV is already off.

A panel `On` transition is debounced for 800ms before it wakes the TV. KDE's
locker re-asserts DPMS around a blank/restore, and waking on that bounce meant
the next sleep request was cancelled by the wake it had just started. Both the
debounce timer and any pre-debounce early wake hint share a per-transition
cancellation event that is set as soon as the panel goes `Off` again (or when a
sleep starts), so a bounce aborts pending hints and a sleep request is never
superseded, while a genuine pointer or keyboard wake at the lock screen still
starts the normal wake path. Startup reconciliation skips the wake while the
session is locked and re-checks wake preconditions after probing TV power
state, so a daemon restart or concurrent lock cannot toggle the TV.

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
| Lid state at startup | one-time `login1` query (`/proc/acpi` fallback) | seed cached state; no polling |
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
| `TV_MAC` | `40:16:3b:d0:4f:8b` | WoL (default only applies to the Samsung backend) |
| `TV_BACKEND` | `samsung` | backend selection |
| `CEC_DEVICE` | `/dev/cec0` | daemon, CLI, boot (empty disables CEC) |
| `CEC_PHYS_ADDR` | `1.0.0.0` | CEC active-source address |
| `TV_ECP_PORT` | `8060` | Roku ECP |
| `TV_ADB_PORT` | `5555` | Fire TV ADB |
| `ADB_BIN` | `adb` | Fire TV ADB executable |
| `ANDROIDTV_CERT` / `ANDROIDTV_KEY` | `~/.local/state/tvpowersync/androidtv_*.pem` | Android TV pairing |
| `ANDROIDTV_CLIENT_NAME` | `TVPowerSync` | Android TV pairing |
| `LG_CLIENT_KEY_FILE` | `~/.local/state/tvpowersync/lg_client_key.txt` | LG webOS pairing |
| `VIZIO_TOKEN_FILE` | `~/.local/state/tvpowersync/vizio_token.txt` | Vizio SmartCast pairing |
| `TV_VIZIO_PORT` | `7345` | Vizio SmartCast HTTPS port |
| `TV_MODE` | `3840x2160@60` | mode re-assert |
| `TV_LOG_LEVEL` | `INFO` | all scripts |
| `TV_PYTHON` | `$XDG_DATA_HOME/tvpowersync/venv/bin/python` | `deploy.sh` interpreter override |
| `TV_TOKEN_FILE` | `~/.local/state/tvpowersync/tv_token.txt` | pairing helper |
| `TV_DRM_INFO` | `drm_info.json` (cwd) | `parse_drm_info` snapshot |

[`deploy.sh`](README.md#deployment) keeps the venv outside the clone, at
`$XDG_DATA_HOME/tvpowersync/venv` (default
`~/.local/share/tvpowersync/venv`), so the checkout can be moved or deleted
after deployment. When the venv is missing the script creates it with
`--system-site-packages` and installs the vendor libraries (`samsungtvws`,
`androidtvremote2`, `aiowebostv`, `vizaio`); `TV_PYTHON` overrides the
interpreter. If an existing venv cannot see the system bindings it writes a
version-matched `000-system-bindings.pth` into the venv (the same effect as the
flag) rather than re-creating it. It substitutes the selected interpreter into
the user and boot units (`@PYTHON@`, `@BIN_DIR@`), rewrites the installed script
shebangs to match, fails if no interpreter can import `dbus`/`gi`, and warns if
any vendor library is missing.
