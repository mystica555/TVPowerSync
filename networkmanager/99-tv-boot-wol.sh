#!/bin/sh
# NetworkManager dispatcher hook: fires on network connection events.
# Executes as a strict singleton once per boot session when home network address is acquired.

BOOT_FLAG_RUN="/run/tv_boot_wol.done"
BOOT_FLAG_TMP="/tmp/tv_boot_wol.done"

# 1. If already executed during this boot session, exit immediately
if [ -f "$BOOT_FLAG_RUN" ] || [ -f "$BOOT_FLAG_TMP" ]; then
    exit 0
fi

INTERFACE="$1"
ACTION="$2"

# 2. Only trigger when an interface comes up or acquires a DHCPv4 address
if [ "$ACTION" != "up" ] && [ "$ACTION" != "dhcp4-change" ]; then
    exit 0
fi

# 3. Check if we actually acquired a 192.168.0.x IP address
if ip -4 addr show dev "$INTERFACE" 2>/dev/null | grep -q 'inet 192.168.0.'; then
    # 4. Verify laptop display is not in hardware DPMS Off (sleeping)
    for p in /sys/class/drm/*-eDP-*/dpms; do
        if [ -f "$p" ] && [ "$(cat "$p" 2>/dev/null)" = "Off" ]; then
            exit 0
        fi
    done

    # 5. Mark singleton flags immediately so concurrent events don't race
    touch "$BOOT_FLAG_RUN" 2>/dev/null || true
    touch "$BOOT_FLAG_TMP" 2>/dev/null || true
    chmod 644 "$BOOT_FLAG_TMP" "$BOOT_FLAG_RUN" 2>/dev/null || true

    # 6. Execute boot wake sequence in background
    /home/mike/.local/bin/boot_wol.py >/dev/null 2>&1 &
fi
