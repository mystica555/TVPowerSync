#!/bin/bash
set -euo pipefail

# XDG Base Directory specification compliance
XDG_BIN_HOME="${XDG_BIN_HOME:-$HOME/.local/bin}"
XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
SYSTEMD_USER_DIR="$XDG_CONFIG_HOME/systemd/user"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Deploying Samsung TV & DRM Utilities (XDG Compliant) ==="
echo "Target Bin Dir     : $XDG_BIN_HOME"
echo "Target Config Dir  : $XDG_CONFIG_HOME"
echo "Systemd User Dir   : $SYSTEMD_USER_DIR"

# 1. Ensure target directories exist
mkdir -p "$XDG_BIN_HOME"
mkdir -p "$SYSTEMD_USER_DIR"

# 2. Deploy user executables to ~/.local/bin
echo -e "\n[*] Installing user executables to $XDG_BIN_HOME..."
SCRIPTS=(control_tv.py tv_power_daemon.py boot_wol.py parse_drm_info.py pair_samsung_tv.py)

for script in "${SCRIPTS[@]}"; do
    install -m 755 "$REPO_DIR/$script" "$XDG_BIN_HOME/$script"
    # Create extension-less alias symlinks for convenience
    base_name="${script%.py}"
    ln -sf "$XDG_BIN_HOME/$script" "$XDG_BIN_HOME/$base_name"
    echo "  Installed: $XDG_BIN_HOME/$script (and alias $base_name)"
done

# 3. Deploy systemd user service
echo -e "\n[*] Deploying systemd user service..."
cp "$REPO_DIR/systemd/tv-power-sync.service" "$SYSTEMD_USER_DIR/tv-power-sync.service"
systemctl --user daemon-reload
systemctl --user enable tv-power-sync.service
systemctl --user restart tv-power-sync.service
echo "  User service tv-power-sync.service reloaded and restarted."

# 4. Deploy system boot service and udev rules (system-level)
echo -e "\n[*] Deploying system boot service and udev rules..."
if command -v sudo >/dev/null 2>&1; then
    sudo cp "$REPO_DIR/systemd/tv-boot-wol.service" /etc/systemd/system/tv-boot-wol.service
    sudo systemctl daemon-reload
    sudo systemctl enable tv-boot-wol.service
    echo "  System service tv-boot-wol.service updated."

    # Ensure no legacy NetworkManager dispatcher scripts remain (all boot wake handled natively by systemd)
    if [ -f /etc/NetworkManager/dispatcher.d/99-tv-boot-wol.sh ]; then
        sudo rm -f /etc/NetworkManager/dispatcher.d/99-tv-boot-wol.sh
        echo "  Removed legacy NetworkManager dispatcher hook."
    fi

    if [ -d "$REPO_DIR/udev" ]; then
        sudo cp "$REPO_DIR"/udev/*.rules /etc/udev/rules.d/
        sudo udevadm control --reload-rules
        sudo udevadm trigger --subsystem-match=cec
        echo "  Udev rules for HDMI-CEC deployed and triggered."
    fi
else
    echo "  Notice: sudo not available, skipping system service copy."
fi

echo -e "\n=== Deployment Completed Successfully ==="
systemctl --user status tv-power-sync.service --no-pager
