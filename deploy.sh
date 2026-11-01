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

# 0. Pick the interpreter that can import both the system bindings (dbus/gi)
#    and samsungtvws. A venv that only contains samsungtvws is repaired by
#    adding a .pth exposing the system dist-packages (the same effect as
#    creating it with --system-site-packages), so both sets remain available.
repair_system_bindings() {
    local venv_py="$1"
    if "$venv_py" -c 'import dbus, gi' >/dev/null 2>&1; then
        return 0
    fi

    local system_py
    system_py="/usr/bin/python3"
    [[ -x "$system_py" ]] || system_py="$(command -v python3 || true)"
    [[ -n "$system_py" ]] || return 1
    "$system_py" -c 'import dbus, gi' >/dev/null 2>&1 || return 1

    local venv_ver system_ver
    venv_ver="$("$venv_py" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || true)"
    system_ver="$("$system_py" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || true)"
    if [[ -z "$venv_ver" || "$venv_ver" != "$system_ver" ]]; then
        echo "  Cannot borrow system bindings: venv Python ${venv_ver:-?} != system Python ${system_ver:-?}."
        return 1
    fi

    local site_dir paths
    site_dir="$("$venv_py" -c 'import sysconfig; print(sysconfig.get_path("purelib"))' 2>/dev/null || true)"
    [[ -n "$site_dir" && -d "$site_dir" ]] || return 1

    paths="$("$system_py" - <<'PY' 2>/dev/null || true
import dbus, gi, sys
seen = set()
for path in sys.path:
    if path and path.startswith("/usr") and path not in seen:
        if "dist-packages" in path or "site-packages" in path:
            seen.add(path)
            print(path)
PY
)"
    [[ -n "$paths" ]] || return 1

    : > "$site_dir/000-system-bindings.pth"
    while IFS= read -r path; do
        if [[ -d "$path" ]]; then
            printf '%s\n' "$path" >> "$site_dir/000-system-bindings.pth"
        fi
    done <<< "$paths"

    echo "  Added system bindings to $site_dir/000-system-bindings.pth"
    "$venv_py" -c 'import dbus, gi' >/dev/null 2>&1
}

PYTHON_BIN="${TV_PYTHON:-}"
if [[ -z "$PYTHON_BIN" ]]; then
    for candidate in "$REPO_DIR/venv/bin/python" "$REPO_DIR/.venv/bin/python"; do
        [[ -x "$candidate" ]] || continue
        if repair_system_bindings "$candidate"; then
            PYTHON_BIN="$candidate"
            break
        fi
        echo "  Skipping $candidate: missing dbus/gi and unable to borrow system bindings."
    done
fi
if [[ -z "$PYTHON_BIN" ]]; then
    PYTHON_BIN="/usr/bin/python3"
    [[ -x "$PYTHON_BIN" ]] || PYTHON_BIN="$(command -v python3 || true)"
    echo "  Warning: no usable repo venv found; using system ${PYTHON_BIN:-python3}."
fi
if [[ -z "$PYTHON_BIN" || ! -x "$PYTHON_BIN" ]]; then
    echo "Error: no usable Python interpreter found (set TV_PYTHON)." >&2
    exit 1
fi
# Make the path absolute without resolving the venv's python symlink (resolving
# it would bypass the venv's site-packages entirely).
PYTHON_BIN="$(cd "$(dirname "$PYTHON_BIN")" && pwd)/$(basename "$PYTHON_BIN")"
echo "Python interpreter : $PYTHON_BIN"

if ! "$PYTHON_BIN" -c 'import dbus, gi' >/dev/null 2>&1; then
    echo "Error: $PYTHON_BIN cannot import dbus/gi." >&2
    echo "       Install python3-dbus/python3-gi, or create the venv with --system-site-packages." >&2
    exit 1
fi
"$PYTHON_BIN" -c 'import samsungtvws' >/dev/null 2>&1 \
    || echo "  Warning: $PYTHON_BIN cannot import samsungtvws; Wi-Fi TV control will be disabled."

# 1. Ensure target directories exist
mkdir -p "$XDG_BIN_HOME"
mkdir -p "$SYSTEMD_USER_DIR"

# 2. Deploy user executables and the shared helper module
echo -e "\n[*] Installing user executables to $XDG_BIN_HOME..."
SCRIPTS=(control_tv.py tv_power_daemon.py boot_wol.py parse_drm_info.py pair_samsung_tv.py)

for script in "${SCRIPTS[@]}"; do
    install -m 755 "$REPO_DIR/$script" "$XDG_BIN_HOME/$script"
    # Pin the installed entry point to the chosen interpreter so manual CLI
    # use also gets samsungtvws/dbus from the same environment.
    sed -i "1s|^#!.*|#!$PYTHON_BIN|" "$XDG_BIN_HOME/$script"
    base_name="${script%.py}"
    ln -sf "$XDG_BIN_HOME/$script" "$XDG_BIN_HOME/$base_name"
    echo "  Installed: $XDG_BIN_HOME/$script (and alias $base_name)"
done
install -m 644 "$REPO_DIR/tvlib.py" "$XDG_BIN_HOME/tvlib.py"
echo "  Installed: $XDG_BIN_HOME/tvlib.py"

# 3. Deploy systemd user service, substituting the interpreter path
echo -e "\n[*] Deploying systemd user service..."
sed "s|@PYTHON@|$PYTHON_BIN|g" \
    "$REPO_DIR/systemd/tv-power-sync.service" \
    > "$SYSTEMD_USER_DIR/tv-power-sync.service"
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

    # Ensure no legacy NetworkManager dispatcher scripts remain (all boot wake
    # is handled natively by systemd).
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
systemctl --user status tv-power-sync.service --no-pager || true
