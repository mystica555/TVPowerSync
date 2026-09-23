#!/bin/sh
# NetworkManager dispatcher hook: fires as soon as DHCP succeeds
INTERFACE="$1"
ACTION="$2"

if [ "$ACTION" = "up" ] || [ "$ACTION" = "dhcp4-change" ]; then
    /home/mike/.local/bin/boot_wol.py >/dev/null 2>&1 &
fi
