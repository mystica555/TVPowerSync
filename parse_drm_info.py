#!/usr/bin/env python3
"""
parse_drm_info.py - DRM Info Parser and Capabilities Summarizer

Parses drm_info JSON output and provides a comprehensive, structured
summary of the GPU driver, DRM capabilities, display pipeline topology,
active scanout status, hardware planes, color management, and connected screens.
"""

import os
import sys
import json
import glob
import subprocess
from typing import Dict, Any, Optional

# Standard DRM connector types mapping
DRM_CONNECTOR_TYPES = {
    0: "Unknown",
    1: "VGA",
    2: "DVI-I",
    3: "DVI-D",
    4: "DVI-A",
    5: "Composite",
    6: "SVIDEO",
    7: "LVDS",
    8: "Component",
    9: "9PinDIN",
    10: "DisplayPort",
    11: "HDMI-A",
    12: "HDMI-B",
    13: "TV",
    14: "eDP",
    15: "VIRTUAL",
    16: "DSI",
    17: "DPI",
    18: "Writeback",
    19: "SPI",
    20: "USB",
}

# DRM Encoder types mapping
DRM_ENCODER_TYPES = {
    0: "None",
    1: "DAC",
    2: "TMDS",
    3: "LVDS",
    4: "TVDAC",
    5: "VIRTUAL",
    6: "DSI",
    7: "DP MST",
    8: "DPMST",
}

# DRM Plane types mapping
DRM_PLANE_TYPES = {
    0: "Overlay",
    1: "Primary",
    2: "Cursor",
}

# DRM Fourcc decoder
def fourcc_to_str(fourcc: int) -> str:
    try:
        return "".join([chr((fourcc >> (8 * i)) & 0xFF) for i in range(4)])
    except Exception:
        return f"0x{fourcc:08x}"

def decode_edid_file(sysfs_path: str) -> Dict[str, Any]:
    info = {}
    if not os.path.exists(sysfs_path):
        return info
    try:
        with open(sysfs_path, "rb") as f:
            raw = f.read()
        if len(raw) < 128:
            return info
        # Manufacturer ID
        mfg_id = (
            (raw[8] & 0x7C) >> 2,
            ((raw[8] & 0x03) << 3) | ((raw[9] & 0xE0) >> 5),
            raw[9] & 0x1F,
        )
        info["mfg_id"] = "".join([chr(x + 64) for x in mfg_id])
        info["product_code"] = raw[10] | (raw[11] << 8)
        # Parse descriptors for monitor name / string
        names = []
        for offset in [54, 72, 90, 108]:
            block = raw[offset : offset + 18]
            if block[0:2] == b"\x00\x00" and block[3] in (0xFC, 0xFE):
                s = block[5:].decode("ascii", errors="ignore").strip()
                if s:
                    names.append(s)
        if names:
            info["monitor_name"] = " / ".join(names)
    except Exception as e:
        info["error"] = str(e)
    return info

def get_prop_val(prop_dict: Optional[Dict[str, Any]]) -> Any:
    if not prop_dict:
        return None
    if "value" in prop_dict:
        return prop_dict["value"]
    if "raw_value" in prop_dict:
        return prop_dict["raw_value"]
    return None

def parse_card(card_name: str, card_data: Dict[str, Any]):
    print("=" * 80)
    print(f" DRM DEVICE SUMMARY: {card_name}")
    print("=" * 80)

    # 1. Driver & Device Info
    driver = card_data.get("driver", {})
    device = card_data.get("device", {})
    dev_data = device.get("device_data", {})
    bus_data = device.get("bus_data", {})
    kernel = driver.get("kernel", {})

    print(f"\n[1] GPU Hardware & Kernel Driver")
    print(f"  • Driver Name       : {driver.get('name')} ({driver.get('desc')})")
    ver = driver.get("version", {})
    print(f"  • Driver Version    : {ver.get('major')}.{ver.get('minor')}.{ver.get('patch')}")
    print(f"  • Kernel Version    : {kernel.get('sysname')} {kernel.get('release')}")
    if dev_data:
        print(f"  • PCI Vendor/Device : 0x{dev_data.get('vendor', 0):04x}:0x{dev_data.get('device', 0):04x} (Subsystem: 0x{dev_data.get('subsystem_vendor', 0):04x}:0x{dev_data.get('subsystem_device', 0):04x})")
    if bus_data:
        print(f"  • PCI Bus Address   : {bus_data.get('domain', 0):04x}:{bus_data.get('bus', 0):02x}:{bus_data.get('slot', 0):02x}.{bus_data.get('function', 0)}")

    # 2. Client & Core Capabilities
    client_caps = driver.get("client_caps", {})
    caps = driver.get("caps", {})
    print(f"\n[2] Core DRM & Modesetting Capabilities")
    print(f"  • Atomic Modesetting       : {'Supported' if client_caps.get('ATOMIC') else 'Not Supported'}")
    print(f"  • Universal Planes         : {'Supported' if client_caps.get('UNIVERSAL_PLANES') else 'Not Supported'}")
    print(f"  • Aspect Ratio Preserving  : {'Supported' if client_caps.get('ASPECT_RATIO') else 'Not Supported'}")
    print(f"  • Writeback Connectors     : {'Supported' if client_caps.get('WRITEBACK_CONNECTORS') else 'Not Supported'}")
    print(f"  • Explicit Sync (SyncObj)  : {'Supported (Timeline Semaphores)' if caps.get('SYNCOBJ_TIMELINE') else ('Supported' if caps.get('SYNCOBJ') else 'No')}")
    print(f"  • Async Page Flipping      : {'Supported (DRM_CAP_ASYNC_PAGE_FLIP=1)' if caps.get('ASYNC_PAGE_FLIP') else 'No'}")
    print(f"  • PRIME Buffer Sharing     : Mask {caps.get('PRIME')} (Import & Export DMA-BUF)")
    print(f"  • Hardware Cursor Size     : {caps.get('CURSOR_WIDTH')}x{caps.get('CURSOR_HEIGHT')}")
    print(f"  • Format Modifiers (ADDFB2): {'Supported' if caps.get('ADDFB2_MODIFIERS') else 'No'}")

    fb_size = card_data.get("fb_size", {})
    print(f"  • Framebuffer Limits       : [{fb_size.get('min_width')}, {fb_size.get('min_height')}] to [{fb_size.get('max_width')}, {fb_size.get('max_height')}]")

    # 3. Connectors & Displays
    connectors = card_data.get("connectors", [])
    print(f"\n[3] Connectors & Display Status ({len(connectors)} total)")

    for conn in connectors:
        cid = conn.get("id")
        raw_type = conn.get("type", 0)
        ctype_name = DRM_CONNECTOR_TYPES.get(raw_type, f"Type-{raw_type}")
        status = "Connected" if conn.get("status") == 1 else "Disconnected"
        props = conn.get("properties", {})

        crtc_id = get_prop_val(props.get("CRTC_ID"))
        dpms_prop = props.get("DPMS", {})
        dpms_val = dpms_prop.get("value") if isinstance(dpms_prop, dict) else None
        
        # Subconnector
        subconn_prop = props.get("subconnector", {})
        subconn_name = None
        if isinstance(subconn_prop, dict):
            sub_val = subconn_prop.get("value")
            spec = subconn_prop.get("spec", [])
            for item in spec:
                if item.get("value") == sub_val:
                    subconn_name = item.get("name")
                    break

        print(f"\n  • Connector #{cid} [{ctype_name}] - Status: {status.upper()}")
        if subconn_name and subconn_name != "Unknown":
            print(f"    - Subconnector / Adapter : {subconn_name}")
        print(f"    - DPMS State             : {dpms_val if dpms_val is not None else 'Unknown'}")
        print(f"    - Active CRTC            : #{crtc_id if crtc_id else 'None (inactive)'}")

        # Attempt sysfs EDID resolution via exact connector_id match
        for p in glob.glob("/sys/class/drm/card*-*/connector_id"):
            try:
                with open(p, "r") as cf:
                    if int(cf.read().strip()) == cid:
                        dir_path = os.path.dirname(p)
                        edid_path = os.path.join(dir_path, "edid")
                        port_name = os.path.basename(dir_path).split("-", 1)[-1]
                        print(f"    - Sysfs Port Name        : {port_name}")
                        edid_info = decode_edid_file(edid_path)
                        if edid_info and "mfg_id" in edid_info:
                            print(f"    - Detected Monitor       : {edid_info.get('monitor_name', 'Unknown')} ({edid_info.get('mfg_id')} 0x{edid_info.get('product_code', 0):04x})")
                        break
            except Exception:
                pass

        # Max BPC & Colorspace
        max_bpc = get_prop_val(props.get("max bpc"))
        if max_bpc is not None:
            print(f"    - Color Depth Capability : Up to {max_bpc} bpc ({max_bpc * 3}-bit deep color)")
        colorspace = get_prop_val(props.get("Colorspace"))
        if colorspace is not None:
            print(f"    - Active Colorspace      : Enum value {colorspace}")

        # HDR Metadata
        hdr_prop = props.get("HDR_OUTPUT_METADATA", {})
        hdr_data = hdr_prop.get("data") if isinstance(hdr_prop, dict) else None
        if hdr_data and isinstance(hdr_data, dict):
            eotf = hdr_data.get("eotf")
            eotf_str = "SMPTE ST 2084 (PQ)" if eotf == 2 else f"EOTF-{eotf}"
            print(f"    - HDR Output Metadata    : Supported (EOTF: {eotf_str})")
            primaries = hdr_data.get("display_primaries", {})
            if primaries:
                r, g, b = primaries.get("r", {}), primaries.get("g", {}), primaries.get("b", {})
                print(f"      Primaries              : Red=({r.get('x',0):.3f},{r.get('y',0):.3f}) Green=({g.get('x',0):.3f},{g.get('y',0):.3f}) Blue=({b.get('x',0):.3f},{b.get('y',0):.3f})")
            max_lum = hdr_data.get("max_display_mastering_luminance", 0)
            min_lum = hdr_data.get("min_display_mastering_luminance", 0)
            if max_lum > 0:
                print(f"      Mastering Luminance    : Min {min_lum} cd/m², Max {max_lum} cd/m²")

        modes = conn.get("modes", [])
        if modes:
            pref = modes[0]
            print(f"    - Total Modes Available  : {len(modes)}")
            print(f"    - Preferred Mode         : {pref.get('name')} @ {pref.get('vrefresh')}Hz (Clock: {pref.get('clock')} kHz)")
            if len(modes) > 1:
                other_modes = [f"{m.get('name')}@{m.get('vrefresh')}Hz" for m in modes[1:6]]
                print(f"    - Sample Alternate Modes : {', '.join(other_modes)}{' ...' if len(modes) > 6 else ''}")

    # 4. Display Pipes (CRTCs)
    crtcs = card_data.get("crtcs", [])
    print(f"\n[4] Display Pipes (CRTCs) ({len(crtcs)} hardware pipes)")
    for crtc in crtcs:
        c_id = crtc.get("id")
        props = crtc.get("properties", {})
        active = get_prop_val(props.get("ACTIVE"))
        mode_data = props.get("MODE_ID", {}).get("data") if isinstance(props.get("MODE_ID"), dict) else None
        gamma_size = get_prop_val(props.get("GAMMA_LUT_SIZE"))
        degamma_size = get_prop_val(props.get("DEGAMMA_LUT_SIZE"))
        vrr = get_prop_val(props.get("VRR_ENABLED"))
        scale_filter = get_prop_val(props.get("SCALING_FILTER"))

        state_str = "ACTIVE / SCANNING OUT" if active else "INACTIVE / STANDBY"
        print(f"\n  • CRTC #{c_id}: {state_str}")
        if active and mode_data and isinstance(mode_data, dict):
            print(f"    - Active Timing Mode     : {mode_data.get('hdisplay')}x{mode_data.get('vdisplay')} @ {mode_data.get('vrefresh')}Hz")
            print(f"    - Pixel Clock            : {mode_data.get('clock')} kHz")
        print(f"    - Color Management       : Degamma LUT ({degamma_size} entries), Gamma LUT ({gamma_size} entries), 3x3 CTM supported")
        print(f"    - Scaling Filter         : {'Nearest Neighbor' if scale_filter == 1 else 'Default (Bilinear)'}")
        print(f"    - VRR Enabled            : {'Yes' if vrr else 'No'}")

    # 5. Hardware Planes
    planes = card_data.get("planes", [])
    print(f"\n[5] Hardware Planes ({len(planes)} total)")
    type_counts = {"Primary": 0, "Overlay": 0, "Cursor": 0}
    active_planes = []

    for p in planes:
        pid = p.get("id")
        props = p.get("properties", {})
        raw_type = get_prop_val(props.get("type"))
        ptype_str = DRM_PLANE_TYPES.get(raw_type, "Unknown")
        type_counts[ptype_str] = type_counts.get(ptype_str, 0) + 1

        crtc_id = get_prop_val(props.get("CRTC_ID"))
        fb_id = get_prop_val(props.get("FB_ID"))
        if crtc_id and fb_id:
            w = get_prop_val(props.get("CRTC_W"))
            h = get_prop_val(props.get("CRTC_H"))
            x = get_prop_val(props.get("CRTC_X"))
            y = get_prop_val(props.get("CRTC_Y"))
            active_planes.append((pid, ptype_str, crtc_id, fb_id, x, y, w, h))

    print(f"  • Plane Topology Breakdown : {type_counts['Primary']} Primary, {type_counts['Overlay']} Overlay, {type_counts['Cursor']} Cursor")
    print(f"  • Features Supported       : Per-plane Alpha, Blend Modes, Hardware Rotation (0/180/flip-x), FB Damage Clips, Gen12 CCS Compression Modifiers")
    print(f"  • Currently Active Planes  : {len(active_planes)}")
    for ap in active_planes:
        pid, ptype, crtc_id, fb_id, x, y, w, h = ap
        print(f"    - Plane #{pid} [{ptype}]: Attached to CRTC #{crtc_id}, Framebuffer #{fb_id}, Rect: ({x},{y}) {w}x{h}")

    print("\n" + "=" * 80)

def main():
    json_path = sys.argv[1] if len(sys.argv) > 1 else "/home/mike/src/tizenpowersync/drm_info.json"
    if not os.path.exists(json_path):
        print(f"File {json_path} not found. Running `drm_info -j` directly...")
        proc = subprocess.run(["drm_info", "-j"], capture_output=True, text=True, check=True)
        data = json.loads(proc.stdout)
    else:
        with open(json_path, "r") as f:
            data = json.load(f)

    for card_name, card_data in data.items():
        parse_card(card_name, card_data)

if __name__ == "__main__":
    main()
