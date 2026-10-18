#!/usr/bin/env python3
"""
Imou / Dahua IP Camera Manager CLI & Core Library (SmartPSS Replacement for macOS).
Provides full device discovery, macOS Keychain credential management, OSD (Logo/Timestamp)
customization, Image/Brightness/Color/WDR/IR tuning, Stream Encoding, RTSP Snapshot, and PTZ.
"""
import argparse
import concurrent.futures
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SKILL_DIR = Path(__file__).resolve().parent.parent
BRIDGE_BIN = SKILL_DIR / "scripts" / "bin" / "imou_sdk_bridge"
USER_CONFIG_DIR = Path.home() / ".config" / "imou-camera-studio"
USER_CAMERAS_FILE = USER_CONFIG_DIR / "cameras.json"
DEFAULT_CAMERAS_FILE = SKILL_DIR / "assets" / "cameras.json"
KEYCHAIN_PREFIX = "imou-camera:"

PTZ_COMMANDS = {
    "up": 0,
    "down": 1,
    "left": 2,
    "right": 3,
    "zoom_in": 4,
    "zoom_out": 5,
    "focus_near": 6,
    "focus_far": 7,
    "up_left": 32,
    "up_right": 33,
    "down_left": 34,
    "down_right": 35,
}


def keychain_get(service: str) -> Optional[str]:
    try:
        res = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", service, "-w"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode == 0:
            return res.stdout.rstrip("\r\n")
    except Exception:
        pass
    return None


def keychain_set(service: str, account: str, secret: str) -> bool:
    try:
        res = subprocess.run(
            [
                "/usr/bin/security",
                "add-generic-password",
                "-U",
                "-s",
                service,
                "-a",
                account,
                "-w",
                secret,
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return res.returncode == 0
    except Exception:
        return False


def get_camera_password(ip: str) -> Optional[str]:
    pwd = keychain_get(f"{KEYCHAIN_PREFIX}{ip}")
    if pwd:
        return pwd
    for fallback in ("default-1", "default-2"):
        pwd = keychain_get(f"{KEYCHAIN_PREFIX}{fallback}")
        if pwd:
            return pwd
    return None


def get_candidate_passwords(ip: str) -> List[str]:
    seen = []
    for key in (f"{KEYCHAIN_PREFIX}{ip}", f"{KEYCHAIN_PREFIX}default-1", f"{KEYCHAIN_PREFIX}default-2"):
        p = keychain_get(key)
        if p and p not in seen:
            seen.append(p)
    return seen


def import_smartpss_credentials() -> Dict[str, Any]:
    if not BRIDGE_BIN.exists():
        return {"ok": False, "error": f"Bridge binary missing at {BRIDGE_BIN}"}
    res = subprocess.run([str(BRIDGE_BIN), "decrypt-smartpss"], capture_output=True, text=True, timeout=10)
    if res.returncode != 0:
        return {"ok": False, "error": res.stderr.strip() or res.stdout.strip()}
    try:
        data = json.loads(res.stdout.strip())
    except Exception as e:
        return {"ok": False, "error": f"Invalid JSON: {e}"}

    if not data.get("ok"):
        return data

    imported = []
    for idx, dev in enumerate(data.get("devices", []), start=1):
        ip = dev.get("ip", "")
        user = dev.get("username") or "admin"
        pwd = dev.get("password") or ""
        if not pwd:
            continue
        keychain_set(f"{KEYCHAIN_PREFIX}default-{idx}", user, pwd)
        if ip:
            keychain_set(f"{KEYCHAIN_PREFIX}{ip}", user, pwd)
        imported.append({"idUnique": dev.get("idUnique"), "name": dev.get("name"), "ip": ip, "username": user})

    return {"ok": True, "imported": imported}


def load_cameras_registry() -> List[Dict[str, Any]]:
    target = USER_CAMERAS_FILE if USER_CAMERAS_FILE.exists() else DEFAULT_CAMERAS_FILE
    if not target.exists():
        return []
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        return data.get("cameras", [])
    except Exception:
        return []


def save_cameras_registry(cameras: List[Dict[str, Any]]) -> None:
    USER_CAMERAS_FILE.parent.mkdir(parents=True, exist_ok=True)
    USER_CAMERAS_FILE.write_text(json.dumps({"cameras": cameras}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def upsert_camera_registry(
    ip: str,
    name: Optional[str] = None,
    model: Optional[str] = None,
    port: int = 37777,
    rtsp_port: int = 554,
    username: str = "admin",
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    cams = load_cameras_registry()
    found = None
    for c in cams:
        if c.get("ip") == ip:
            found = c
            break
    if not found:
        found = {
            "ip": ip,
            "name": name or f"Imou Camera ({ip})",
            "model": model or "Imou/Dahua IPC",
            "mac": "",
            "port": port,
            "rtsp_port": rtsp_port,
            "username": username,
            "channel": 0,
            "notes": notes or "",
        }
        cams.append(found)
    else:
        if name:
            found["name"] = name
        if model:
            found["model"] = model
        if port:
            found["port"] = port
        if rtsp_port:
            found["rtsp_port"] = rtsp_port
        if username:
            found["username"] = username
        if notes is not None:
            found["notes"] = notes
    save_cameras_registry(cams)
    return found


def probe_dhip_alive(ip: str, port: int = 37777, timeout: float = 0.8) -> bool:
    pkt = bytearray(32)
    pkt[0] = 0xA0
    pkt[3] = 0x60
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((ip, port))
        s.sendall(bytes(pkt))
        resp = s.recv(64)
        return len(resp) >= 32 and resp[0] == 0xB0
    except Exception:
        return False
    finally:
        s.close()


def detect_local_subnet_prefix() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        parts = local_ip.split(".")
        if len(parts) == 4:
            return f"{parts[0]}.{parts[1]}.{parts[2]}."
    except Exception:
        pass
    finally:
        s.close()
    return "192.168.1."


def discover_lan_cameras(subnet_prefix: Optional[str] = None, scan_all: bool = False) -> List[Dict[str, Any]]:
    if not subnet_prefix:
        subnet_prefix = detect_local_subnet_prefix()
    registry = {c["ip"]: dict(c) for c in load_cameras_registry()}
    ips_to_check = list(registry.keys())
    if scan_all:
        for i in range(1, 255):
            ip = f"{subnet_prefix}{i}"
            if ip not in registry:
                ips_to_check.append(ip)

    online_map: Dict[str, bool] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=48) as ex:
        fut_to_ip = {ex.submit(probe_dhip_alive, ip, registry.get(ip, {}).get("port", 37777)): ip for ip in ips_to_check}
        for fut in concurrent.futures.as_completed(fut_to_ip):
            ip = fut_to_ip[fut]
            online_map[ip] = bool(fut.result())

    results = []
    for ip in ips_to_check:
        is_online = online_map.get(ip, False)
        if ip not in registry and not is_online:
            continue
        item = registry.get(
            ip,
            {
                "ip": ip,
                "name": f"Imou Camera ({ip})",
                "model": "Imou/Dahua IPC",
                "mac": "",
                "port": 37777,
                "rtsp_port": 554,
                "username": "admin",
                "channel": 0,
                "notes": "Phát hiện tự động trên LAN",
            },
        )
        item["online"] = is_online
        item["has_saved_password"] = keychain_get(f"{KEYCHAIN_PREFIX}{ip}") is not None
        results.append(item)
    return results


def run_rpc_batch(
    ip: str,
    requests: List[Dict[str, Any]],
    port: int = 37777,
    username: str = "admin",
    password: Optional[str] = None,
) -> Tuple[bool, List[Dict[str, Any]], str]:
    candidates = [password] if password else get_candidate_passwords(ip)
    if not candidates:
        # Auto-import from SmartPSS on first use if Keychain is empty
        import_smartpss_credentials()
        candidates = get_candidate_passwords(ip)
    if not candidates:
        return False, [], "Chưa có mật khẩu cho camera này. Vui lòng nhập mật khẩu (hoặc Safety Code dưới đáy camera)."

    stdin_payload = "\n".join(json.dumps(r, separators=(",", ":")) for r in requests) + "\n"
    last_err = "Login failed"

    for cand in candidates:
        env = os.environ.copy()
        env["IMOU_CAM_PASSWORD"] = cand
        res = subprocess.run(
            [str(BRIDGE_BIN), "rpc", ip, str(port), username],
            input=stdin_payload,
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )
        out_line = res.stdout.strip()
        try:
            parsed = json.loads(out_line)
        except Exception:
            last_err = out_line or res.stderr.strip() or "SDK Bridge error"
            continue

        if parsed.get("ok"):
            # Cache working password for this specific IP in Keychain
            keychain_set(f"{KEYCHAIN_PREFIX}{ip}", username, cand)
            return True, parsed.get("responses", []), ""
        else:
            code = parsed.get("login_error", -1)
            last_err = f"Đăng nhập thất bại (Mã lỗi SDK: {code} - Sai mật khẩu hoặc tài khoản bị khoá tạm thời)"

    return False, [], last_err


def inspect_camera(ip: str, port: int = 37777, username: str = "admin", password: Optional[str] = None) -> Dict[str, Any]:
    reqs = [
        {"method": "magicBox.getDeviceType", "params": None},
        {"method": "magicBox.getSerialNo", "params": None},
        {"method": "magicBox.getSoftwareVersion", "params": None},
        {"method": "configManager.getConfig", "params": {"channel": 0, "name": "ChannelTitle", "onlyLocal": True}},
        {"method": "configManager.getConfig", "params": {"channel": 0, "name": "VideoWidget", "onlyLocal": True}},
        {"method": "configManager.getConfig", "params": {"channel": 0, "name": "VideoColor", "onlyLocal": True}},
        {"method": "configManager.getConfig", "params": {"channel": 0, "name": "VideoInOptions", "onlyLocal": True}},
        {"method": "configManager.getConfig", "params": {"channel": 0, "name": "VideoInDayNight", "onlyLocal": True}},
        {"method": "configManager.getConfig", "params": {"channel": 0, "name": "VideoInSharpness", "onlyLocal": True}},
        {"method": "configManager.getConfig", "params": {"channel": 0, "name": "VideoInBacklight", "onlyLocal": True}},
        {"method": "configManager.getConfig", "params": {"channel": 0, "name": "VideoInExposure", "onlyLocal": True}},
        {"method": "configManager.getConfig", "params": {"channel": 0, "name": "VideoInWhiteBalance", "onlyLocal": True}},
        {"method": "configManager.getConfig", "params": {"channel": 0, "name": "Encode", "onlyLocal": True}},
        {"method": "configManager.getConfig", "params": {"name": "Locales", "onlyLocal": True}},
        {"method": "configManager.getConfig", "params": {"name": "NTP", "onlyLocal": True}},
    ]
    keys = [
        "DeviceType",
        "SerialNo",
        "SoftwareVersion",
        "ChannelTitle",
        "VideoWidget",
        "VideoColor",
        "VideoInOptions",
        "VideoInDayNight",
        "VideoInSharpness",
        "VideoInBacklight",
        "VideoInExposure",
        "VideoInWhiteBalance",
        "Encode",
        "Locales",
        "NTP",
    ]
    ok, responses, err = run_rpc_batch(ip, reqs, port=port, username=username, password=password)
    if not ok:
        return {"ok": False, "ip": ip, "error": err}

    raw_tables: Dict[str, Any] = {}
    for k, r in zip(keys, responses):
        data = r.get("data", {})
        if isinstance(data, dict) and data.get("result"):
            params = data.get("params", {})
            raw_tables[k] = params.get("table", params) if isinstance(params, dict) else params
        else:
            raw_tables[k] = None

    def first_dict(val: Any) -> Dict[str, Any]:
        if isinstance(val, dict):
            return val
        if isinstance(val, list) and val and isinstance(val[0], dict):
            return val[0]
        return {}

    ch_obj = first_dict(raw_tables.get("ChannelTitle"))
    channel_name = ch_obj.get("Name", "")

    vw = first_dict(raw_tables.get("VideoWidget"))
    ch_widget = vw.get("ChannelTitle", {}) if isinstance(vw.get("ChannelTitle"), dict) else {}
    time_widget = vw.get("TimeTitle", {}) if isinstance(vw.get("TimeTitle"), dict) else {}
    pic_widget = vw.get("PictureTitle", {}) if isinstance(vw.get("PictureTitle"), dict) else {}
    custom_titles = vw.get("CustomTitle", []) if isinstance(vw.get("CustomTitle"), list) else []

    vc = first_dict(raw_tables.get("VideoColor"))
    vio = first_dict(raw_tables.get("VideoInOptions"))
    sharp = first_dict(raw_tables.get("VideoInSharpness"))
    dn = first_dict(raw_tables.get("VideoInDayNight"))
    bl = first_dict(raw_tables.get("VideoInBacklight"))

    enc = raw_tables.get("Encode") or {}
    main_fmt = (enc.get("MainFormat") or [{}])[0] if isinstance(enc, dict) else {}
    sub_fmt = (enc.get("ExtraFormat") or [{}])[0] if isinstance(enc, dict) else {}

    has_common_osd = "CommonOsdEnable" in vw
    common_osd_enabled = bool(vw.get("CommonOsdEnable", False))

    summary = {
        "device_type": (raw_tables.get("DeviceType") or {}).get("type", ""),
        "serial_no": (raw_tables.get("SerialNo") or {}).get("sn", ""),
        "firmware": ((raw_tables.get("SoftwareVersion") or {}).get("version") or {}).get("Version", ""),
        "build_date": ((raw_tables.get("SoftwareVersion") or {}).get("version") or {}).get("BuildDate", ""),
        "osd": {
            "channel_name": channel_name,
            "logo_enabled": bool(ch_widget.get("EncodeBlend", False)),
            "logo_preview_enabled": bool(ch_widget.get("PreviewBlend", False)),
            "logo_rect": ch_widget.get("Rect", []),
            "has_common_osd": has_common_osd,
            "common_osd_enabled": common_osd_enabled,
            "picture_logo_enabled": bool(pic_widget.get("EncodeBlend", False)),
            "time_enabled": bool(time_widget.get("EncodeBlend", False)),
            "time_preview_enabled": bool(time_widget.get("PreviewBlend", False)),
            "show_week": bool(time_widget.get("ShowWeek", False)),
            "time_rect": time_widget.get("Rect", []),
            "font_size": vw.get("FontSize", 24),
            "custom_text": custom_titles[1].get("Text", "") if len(custom_titles) > 1 and isinstance(custom_titles[1], dict) else "",
            "custom_enabled": bool(custom_titles[1].get("EncodeBlend", False)) if len(custom_titles) > 1 and isinstance(custom_titles[1], dict) else False,
            "time_format": (raw_tables.get("Locales") or {}).get("TimeFormat", "yyyy-MM-dd HH:mm:ss") if isinstance(raw_tables.get("Locales"), dict) else "yyyy-MM-dd HH:mm:ss",
        },
        "image": {
            "brightness": vc.get("Brightness", 50),
            "contrast": vc.get("Contrast", 50),
            "saturation": vc.get("Saturation", 50),
            "gamma": vc.get("Gamma", 50),
            "hue": vc.get("Hue", 50),
            "chroma_suppress": vc.get("ChromaSuppress", 50),
            "style": vc.get("Style", "Standard"),
            "sharpness": sharp.get("Sharpness", sharp.get("Level", 50)),
            "mirror": bool(vio.get("Mirror", False)),
            "flip": bool(vio.get("Flip", False)),
            "rotate90": vio.get("Rotate90", 0),
            "day_night_color": vio.get("DayNightColor", 1),
            "day_night_mode": dn.get("Mode", "Brightness"),
            "infrared": bool(vio.get("InfraRed", False)),
            "wdr": vio.get("WideDynamicRange", 0),
            "backlight_mode": bl.get("Mode", "Off"),
            "anti_flicker": vio.get("AntiFlicker", 0),
            "exposure_compensation": vio.get("ExposureCompensation", 50),
        },
        "encode": {
            "main_codec": (main_fmt.get("Video") or {}).get("Compression", "H.264"),
            "main_width": (main_fmt.get("Video") or {}).get("Width", 1920),
            "main_height": (main_fmt.get("Video") or {}).get("Height", 1080),
            "main_fps": (main_fmt.get("Video") or {}).get("FPS", 25),
            "main_bitrate": (main_fmt.get("Video") or {}).get("BitRate", 2048),
            "main_bitrate_ctrl": (main_fmt.get("Video") or {}).get("BitRateControl", "CBR"),
            "main_audio": bool(main_fmt.get("AudioEnable", True)),
            "sub_codec": (sub_fmt.get("Video") or {}).get("Compression", "H.264"),
            "sub_width": (sub_fmt.get("Video") or {}).get("Width", 640),
            "sub_height": (sub_fmt.get("Video") or {}).get("Height", 480),
            "sub_fps": (sub_fmt.get("Video") or {}).get("FPS", 25),
            "sub_bitrate": (sub_fmt.get("Video") or {}).get("BitRate", 512),
            "sub_audio": bool(sub_fmt.get("AudioEnable", True)),
        },
    }
    return {"ok": True, "ip": ip, "summary": summary, "raw": raw_tables}


def update_osd(
    ip: str,
    logo_enabled: Optional[bool] = None,
    common_osd_enabled: Optional[bool] = None,
    time_enabled: Optional[bool] = None,
    show_week: Optional[bool] = None,
    channel_name: Optional[str] = None,
    font_size: Optional[int] = None,
    custom_text: Optional[str] = None,
    custom_enabled: Optional[bool] = None,
    logo_rect: Optional[List[int]] = None,
    time_rect: Optional[List[int]] = None,
    port: int = 37777,
    username: str = "admin",
) -> Dict[str, Any]:
    get_reqs = [
        {"method": "configManager.getConfig", "params": {"channel": 0, "name": "ChannelTitle", "onlyLocal": True}},
        {"method": "configManager.getConfig", "params": {"channel": 0, "name": "VideoWidget", "onlyLocal": True}},
    ]
    ok, resps, err = run_rpc_batch(ip, get_reqs, port=port, username=username)
    if not ok:
        return {"ok": False, "error": err}

    ch_table = ((resps[0].get("data") or {}).get("params") or {}).get("table")
    vw_table = ((resps[1].get("data") or {}).get("params") or {}).get("table")
    if vw_table is None:
        return {"ok": False, "error": "Không đọc được bảng cấu hình VideoWidget từ camera."}

    set_reqs = []
    if channel_name is not None:
        if isinstance(ch_table, list) and ch_table and isinstance(ch_table[0], dict):
            ch_table[0]["Name"] = channel_name
        elif isinstance(ch_table, dict):
            ch_table["Name"] = channel_name
        else:
            ch_table = [{"Name": channel_name}]
        set_reqs.append(
            {
                "method": "configManager.setConfig",
                "params": {"channel": 0, "name": "ChannelTitle", "table": ch_table},
            }
        )

    vw = vw_table[0] if isinstance(vw_table, list) and vw_table else vw_table
    if not isinstance(vw, dict):
        return {"ok": False, "error": "Định dạng VideoWidget không hợp lệ."}

    if logo_enabled is not None:
        if "ChannelTitle" not in vw or not isinstance(vw["ChannelTitle"], dict):
            vw["ChannelTitle"] = {}
        vw["ChannelTitle"]["EncodeBlend"] = bool(logo_enabled)
        vw["ChannelTitle"]["PreviewBlend"] = bool(logo_enabled)
        if "PictureTitle" in vw and isinstance(vw["PictureTitle"], dict) and vw["PictureTitle"]:
            vw["PictureTitle"]["EncodeBlend"] = bool(logo_enabled)
            vw["PictureTitle"]["PreviewBlend"] = bool(logo_enabled)
        if "CommonOsdEnable" in vw and common_osd_enabled is None:
            vw["CommonOsdEnable"] = bool(logo_enabled)

    if common_osd_enabled is not None and "CommonOsdEnable" in vw:
        vw["CommonOsdEnable"] = bool(common_osd_enabled)

    if logo_rect and len(logo_rect) == 4:
        vw.setdefault("ChannelTitle", {})["Rect"] = [int(x) for x in logo_rect]

    if time_enabled is not None:
        if "TimeTitle" not in vw or not isinstance(vw["TimeTitle"], dict):
            vw["TimeTitle"] = {}
        vw["TimeTitle"]["EncodeBlend"] = bool(time_enabled)
        vw["TimeTitle"]["PreviewBlend"] = bool(time_enabled)

    if show_week is not None:
        vw.setdefault("TimeTitle", {})["ShowWeek"] = bool(show_week)

    if time_rect and len(time_rect) == 4:
        vw.setdefault("TimeTitle", {})["Rect"] = [int(x) for x in time_rect]

    if font_size is not None:
        vw["FontSize"] = int(font_size)

    if custom_text is not None or custom_enabled is not None:
        ct = vw.get("CustomTitle")
        if isinstance(ct, list) and len(ct) > 1 and isinstance(ct[1], dict):
            if custom_text is not None:
                ct[1]["Text"] = custom_text
            if custom_enabled is not None:
                ct[1]["EncodeBlend"] = bool(custom_enabled)
                ct[1]["PreviewBlend"] = bool(custom_enabled)

    set_reqs.append(
        {
            "method": "configManager.setConfig",
            "params": {"channel": 0, "name": "VideoWidget", "table": vw_table},
        }
    )

    ok2, resps2, err2 = run_rpc_batch(ip, set_reqs, port=port, username=username)
    if not ok2:
        return {"ok": False, "error": err2}
    return {"ok": True, "results": [r.get("data") for r in resps2]}


def update_image(
    ip: str,
    brightness: Optional[int] = None,
    contrast: Optional[int] = None,
    saturation: Optional[int] = None,
    gamma: Optional[int] = None,
    hue: Optional[int] = None,
    chroma_suppress: Optional[int] = None,
    style: Optional[str] = None,
    sharpness: Optional[int] = None,
    mirror: Optional[bool] = None,
    flip: Optional[bool] = None,
    rotate90: Optional[int] = None,
    day_night_color: Optional[int] = None,
    infrared: Optional[bool] = None,
    wdr: Optional[int] = None,
    anti_flicker: Optional[int] = None,
    exposure_compensation: Optional[int] = None,
    port: int = 37777,
    username: str = "admin",
) -> Dict[str, Any]:
    get_reqs = [
        {"method": "configManager.getConfig", "params": {"channel": 0, "name": "VideoColor", "onlyLocal": True}},
        {"method": "configManager.getConfig", "params": {"channel": 0, "name": "VideoInOptions", "onlyLocal": True}},
        {"method": "configManager.getConfig", "params": {"channel": 0, "name": "VideoInSharpness", "onlyLocal": True}},
    ]
    ok, resps, err = run_rpc_batch(ip, get_reqs, port=port, username=username)
    if not ok:
        return {"ok": False, "error": err}

    vc_table = ((resps[0].get("data") or {}).get("params") or {}).get("table")
    vio_table = ((resps[1].get("data") or {}).get("params") or {}).get("table")
    sharp_table = ((resps[2].get("data") or {}).get("params") or {}).get("table")

    set_reqs = []

    if vc_table and any(
        x is not None for x in (brightness, contrast, saturation, gamma, hue, chroma_suppress, style)
    ):
        vc_entries = vc_table if isinstance(vc_table, list) else ([vc_table] if isinstance(vc_table, dict) else [])
        for entry in vc_entries:
            if not isinstance(entry, dict):
                continue
            if brightness is not None:
                entry["Brightness"] = max(0, min(100, int(brightness)))
            if contrast is not None:
                entry["Contrast"] = max(0, min(100, int(contrast)))
            if saturation is not None:
                entry["Saturation"] = max(0, min(100, int(saturation)))
            if gamma is not None:
                entry["Gamma"] = max(0, min(100, int(gamma)))
            if hue is not None:
                entry["Hue"] = max(0, min(100, int(hue)))
            if chroma_suppress is not None:
                entry["ChromaSuppress"] = max(0, min(100, int(chroma_suppress)))
            if style is not None:
                entry["Style"] = style
        set_reqs.append(
            {
                "method": "configManager.setConfig",
                "params": {"channel": 0, "name": "VideoColor", "table": vc_table},
            }
        )

    if sharp_table and sharpness is not None:
        val = max(0, min(100, int(sharpness)))
        sharp_entries = sharp_table if isinstance(sharp_table, list) else ([sharp_table] if isinstance(sharp_table, dict) else [])
        for entry in sharp_entries:
            if isinstance(entry, dict):
                entry["Sharpness"] = val
                entry["Level"] = val
        set_reqs.append(
            {
                "method": "configManager.setConfig",
                "params": {"channel": 0, "name": "VideoInSharpness", "table": sharp_table},
            }
        )

    if vio_table and isinstance(vio_table, dict) and any(
        x is not None for x in (mirror, flip, rotate90, day_night_color, infrared, wdr, anti_flicker, exposure_compensation)
    ):
        targets = [vio_table]
        for sub_key in ("NormalOptions", "NightOptions"):
            if isinstance(vio_table.get(sub_key), dict):
                targets.append(vio_table[sub_key])
        for t in targets:
            if mirror is not None:
                t["Mirror"] = bool(mirror)
            if flip is not None:
                t["Flip"] = bool(flip)
            if rotate90 is not None:
                t["Rotate90"] = int(rotate90)
            if day_night_color is not None:
                t["DayNightColor"] = int(day_night_color)
            if infrared is not None:
                t["InfraRed"] = bool(infrared)
            if wdr is not None:
                t["WideDynamicRange"] = int(wdr)
                t["WideDynamicRangeMode"] = 1 if int(wdr) > 0 else 0
            if anti_flicker is not None:
                t["AntiFlicker"] = int(anti_flicker)
            if exposure_compensation is not None:
                t["ExposureCompensation"] = max(0, min(100, int(exposure_compensation)))
        set_reqs.append(
            {
                "method": "configManager.setConfig",
                "params": {"channel": 0, "name": "VideoInOptions", "table": vio_table},
            }
        )

    if not set_reqs:
        return {"ok": True, "results": [], "message": "Không có thông số hình ảnh nào thay đổi."}

    ok2, resps2, err2 = run_rpc_batch(ip, set_reqs, port=port, username=username)
    if not ok2:
        return {"ok": False, "error": err2}
    return {"ok": True, "results": [r.get("data") for r in resps2]}


def update_encode(
    ip: str,
    main_codec: Optional[str] = None,
    main_fps: Optional[int] = None,
    main_bitrate: Optional[int] = None,
    main_bitrate_ctrl: Optional[str] = None,
    main_audio: Optional[bool] = None,
    sub_codec: Optional[str] = None,
    sub_fps: Optional[int] = None,
    sub_bitrate: Optional[int] = None,
    sub_audio: Optional[bool] = None,
    port: int = 37777,
    username: str = "admin",
) -> Dict[str, Any]:
    get_reqs = [{"method": "configManager.getConfig", "params": {"channel": 0, "name": "Encode", "onlyLocal": True}}]
    ok, resps, err = run_rpc_batch(ip, get_reqs, port=port, username=username)
    if not ok:
        return {"ok": False, "error": err}

    enc_table = ((resps[0].get("data") or {}).get("params") or {}).get("table")
    if not enc_table or not isinstance(enc_table, dict):
        return {"ok": False, "error": "Không đọc được bảng cấu hình Encode từ camera."}

    for fmt in enc_table.get("MainFormat", []):
        if not isinstance(fmt, dict):
            continue
        if main_audio is not None:
            fmt["AudioEnable"] = bool(main_audio)
        vid = fmt.get("Video", {})
        if main_codec:
            vid["Compression"] = main_codec
        if main_fps is not None:
            vid["FPS"] = int(main_fps)
        if main_bitrate is not None:
            vid["BitRate"] = int(main_bitrate)
        if main_bitrate_ctrl:
            vid["BitRateControl"] = main_bitrate_ctrl

    for fmt in enc_table.get("ExtraFormat", []):
        if not isinstance(fmt, dict):
            continue
        if sub_audio is not None:
            fmt["AudioEnable"] = bool(sub_audio)
        vid = fmt.get("Video", {})
        if sub_codec:
            vid["Compression"] = sub_codec
        if sub_fps is not None:
            vid["FPS"] = int(sub_fps)
        if sub_bitrate is not None:
            vid["BitRate"] = int(sub_bitrate)

    set_reqs = [{"method": "configManager.setConfig", "params": {"channel": 0, "name": "Encode", "table": enc_table}}]
    ok2, resps2, err2 = run_rpc_batch(ip, set_reqs, port=port, username=username)
    if not ok2:
        return {"ok": False, "error": err2}
    return {"ok": True, "results": [r.get("data") for r in resps2]}


def capture_snapshot(
    ip: str,
    output_path: Path,
    subtype: int = 0,
    rtsp_port: int = 554,
    username: str = "admin",
    password: Optional[str] = None,
) -> Dict[str, Any]:
    pwd = password or get_camera_password(ip)
    if not pwd:
        import_smartpss_credentials()
        pwd = get_camera_password(ip)
    if not pwd:
        return {"ok": False, "error": "Chưa có mật khẩu camera để lấy luồng RTSP."}

    output_path.parent.mkdir(parents=True, exist_ok=True)
    rtsp_url = f"rtsp://{username}:{pwd}@{ip}:{rtsp_port}/cam/realmonitor?channel=1&subtype={subtype}"
    res = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-rtsp_transport",
            "tcp",
            "-i",
            rtsp_url,
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(output_path),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if res.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
        return {"ok": True, "path": str(output_path), "size": output_path.stat().st_size}
    return {"ok": False, "error": "Không chụp được ảnh từ luồng RTSP (kiểm tra mật khẩu hoặc kết nối mạng)."}


def control_ptz(
    ip: str,
    action: str,
    speed: int = 4,
    duration_ms: int = 400,
    port: int = 37777,
    username: str = "admin",
) -> Dict[str, Any]:
    cmd_id = PTZ_COMMANDS.get(action.lower())
    if cmd_id is None:
        return {"ok": False, "error": f"Lệnh PTZ không hợp lệ: {action}"}
    pwd = get_camera_password(ip)
    if not pwd:
        return {"ok": False, "error": "Chưa có mật khẩu cho camera này."}
    env = os.environ.copy()
    env["IMOU_CAM_PASSWORD"] = pwd
    res = subprocess.run(
        [str(BRIDGE_BIN), "ptz", ip, str(port), username, str(cmd_id), str(speed), str(duration_ms)],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    try:
        return json.loads(res.stdout.strip())
    except Exception:
        return {"ok": False, "error": res.stderr.strip() or res.stdout.strip()}


def reboot_camera(ip: str, port: int = 37777, username: str = "admin") -> Dict[str, Any]:
    ok, resps, err = run_rpc_batch(ip, [{"method": "magicBox.reboot", "params": None}], port=port, username=username)
    if not ok:
        return {"ok": False, "error": err}
    return {"ok": True, "response": resps[0].get("data") if resps else None}


def main() -> int:
    parser = argparse.ArgumentParser(description="Imou / Dahua SmartPSS Camera Manager CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("import-smartpss", help="Đồng bộ mật khẩu camera đã lưu từ SmartPSS.app sang macOS Keychain")

    p_disc = sub.add_parser("discover", help="Danh sách và kiểm tra trạng thái online của các camera Imou")
    p_disc.add_argument("--scan-all", action="store_true", help="Quét toàn bộ dải 192.168.1.1-254 trên cổng 37777")

    p_pwd = sub.add_parser("set-password", help="Lưu mật khẩu (hoặc Safety Code) cho camera vào macOS Keychain")
    p_pwd.add_argument("--ip", required=True, help="Địa chỉ IP của camera")
    p_pwd.add_argument("--user", default="admin", help="Username (mặc định: admin)")
    p_pwd.add_argument("--password", help="Mật khẩu camera")
    p_pwd.add_argument("--stdin", action="store_true", help="Đọc mật khẩu từ stdin")

    p_insp = sub.add_parser("inspect", help="Đọc toàn bộ thông số OSD, Độ sáng, Màu sắc, Luồng Video của camera")
    p_insp.add_argument("--ip", required=True, help="Địa chỉ IP của camera")

    p_osd = sub.add_parser("set-osd", help="Tuỳ chỉnh tắt/bật Logo Imou, Tên kênh, Ngày tháng năm (OSD)")
    p_osd.add_argument("--ip", required=True, help="Địa chỉ IP của camera")
    p_osd.add_argument("--logo", choices=["on", "off"], help="Bật/tắt Logo / Tên kênh trên góc video")
    p_osd.add_argument("--time", choices=["on", "off"], help="Bật/tắt Ngày tháng năm trên video")
    p_osd.add_argument("--week", choices=["on", "off"], help="Bật/tắt hiển thị Thứ trong tuần")
    p_osd.add_argument("--title", help="Đặt tên kênh hiển thị (hoặc truyền chuỗi rỗng để xoá chữ Imou)")
    p_osd.add_argument("--clear-title", action="store_true", help="Xoá chữ 'Imou' về khoảng trắng")
    p_osd.add_argument("--font-size", type=int, help="Cỡ chữ OSD (ví dụ: 16, 24, 32, 48)")

    p_img = sub.add_parser("set-image", help="Căn chỉnh Độ sáng, Tương phản, Bão hoà, Sắc nét, Lật hình, Hồng ngoại, WDR")
    p_img.add_argument("--ip", required=True, help="Địa chỉ IP của camera")
    p_img.add_argument("--brightness", type=int, help="Độ sáng (0-100, mặc định 50)")
    p_img.add_argument("--contrast", type=int, help="Độ tương phản (0-100, mặc định 50)")
    p_img.add_argument("--saturation", type=int, help="Độ bão hoà màu (0-100, mặc định 50)")
    p_img.add_argument("--gamma", type=int, help="Gamma (0-100, mặc định 50)")
    p_img.add_argument("--sharpness", type=int, help="Độ sắc nét (0-100, mặc định 50)")
    p_img.add_argument("--style", choices=["Standard", "Soft", "Vivid"], help="Phong cách màu")
    p_img.add_argument("--mirror", choices=["on", "off"], help="Đối xứng gương trái-phải (Mirror)")
    p_img.add_argument("--flip", choices=["on", "off"], help="Lật ngược trên-dưới (Flip)")
    p_img.add_argument("--daynight", choices=["auto", "color", "bw"], help="Chế độ Ngày/Đêm (1=Auto, 0=Color, 2=B&W)")
    p_img.add_argument("--ir", choices=["on", "off"], help="Đèn hồng ngoại (InfraRed)")
    p_img.add_argument("--wdr", type=int, help="Mức chống ngược sáng WDR (0=Tắt, 1-100)")

    p_enc = sub.add_parser("set-encode", help="Chỉnh luồng Video/Audio (H.264/H.265, FPS, Bitrate, Micro)")
    p_enc.add_argument("--ip", required=True, help="Địa chỉ IP của camera")
    p_enc.add_argument("--main-codec", choices=["H.264", "H.265"])
    p_enc.add_argument("--main-fps", type=int)
    p_enc.add_argument("--main-bitrate", type=int)
    p_enc.add_argument("--main-audio", choices=["on", "off"])
    p_enc.add_argument("--sub-audio", choices=["on", "off"])

    p_snap = sub.add_parser("snapshot", help="Chụp ảnh trực tiếp từ camera qua RTSP")
    p_snap.add_argument("--ip", required=True, help="Địa chỉ IP của camera")
    p_snap.add_argument("--output", default="/tmp/imou_snapshot.jpg", help="Đường dẫn file ảnh JPEG")
    p_snap.add_argument("--sub", action="store_true", help="Lấy từ luồng phụ (nhanh hơn)")

    p_ptz = sub.add_parser("ptz", help="Điều khiển quay quét PTZ")
    p_ptz.add_argument("--ip", required=True, help="Địa chỉ IP của camera")
    p_ptz.add_argument("--action", required=True, choices=list(PTZ_COMMANDS.keys()))
    p_ptz.add_argument("--speed", type=int, default=4)
    p_ptz.add_argument("--duration-ms", type=int, default=400)

    p_reb = sub.add_parser("reboot", help="Khởi động lại camera")
    p_reb.add_argument("--ip", required=True, help="Địa chỉ IP của camera")

    args = parser.parse_args()

    if args.cmd == "import-smartpss":
        print(json.dumps(import_smartpss_credentials(), indent=2, ensure_ascii=False))
        return 0

    if args.cmd == "discover":
        import_smartpss_credentials()
        cams = discover_lan_cameras(scan_all=args.scan_all)
        print(json.dumps({"ok": True, "cameras": cams}, indent=2, ensure_ascii=False))
        return 0

    if args.cmd == "set-password":
        pwd = sys.stdin.read().strip() if args.stdin else (args.password or "")
        if not pwd:
            print(json.dumps({"ok": False, "error": "Mật khẩu trống"}, ensure_ascii=False))
            return 1
        keychain_set(f"{KEYCHAIN_PREFIX}{args.ip}", args.user, pwd)
        info = inspect_camera(args.ip, username=args.user, password=pwd)
        print(json.dumps(info, indent=2, ensure_ascii=False))
        return 0 if info.get("ok") else 1

    if args.cmd == "inspect":
        info = inspect_camera(args.ip)
        print(json.dumps(info, indent=2, ensure_ascii=False))
        return 0 if info.get("ok") else 1

    if args.cmd == "set-osd":
        title = " " if args.clear_title else args.title
        res = update_osd(
            args.ip,
            logo_enabled=(args.logo == "on") if args.logo else None,
            time_enabled=(args.time == "on") if args.time else None,
            show_week=(args.week == "on") if args.week else None,
            channel_name=title,
            font_size=args.font_size,
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("ok") else 1

    if args.cmd == "set-image":
        dn_map = {"color": 0, "auto": 1, "bw": 2}
        res = update_image(
            args.ip,
            brightness=args.brightness,
            contrast=args.contrast,
            saturation=args.saturation,
            gamma=args.gamma,
            sharpness=args.sharpness,
            style=args.style,
            mirror=(args.mirror == "on") if args.mirror else None,
            flip=(args.flip == "on") if args.flip else None,
            day_night_color=dn_map[args.daynight] if args.daynight else None,
            infrared=(args.ir == "on") if args.ir else None,
            wdr=args.wdr,
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("ok") else 1

    if args.cmd == "set-encode":
        res = update_encode(
            args.ip,
            main_codec=args.main_codec,
            main_fps=args.main_fps,
            main_bitrate=args.main_bitrate,
            main_audio=(args.main_audio == "on") if args.main_audio else None,
            sub_audio=(args.sub_audio == "on") if args.sub_audio else None,
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("ok") else 1

    if args.cmd == "snapshot":
        res = capture_snapshot(args.ip, Path(args.output), subtype=1 if args.sub else 0)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("ok") else 1

    if args.cmd == "ptz":
        res = control_ptz(args.ip, args.action, speed=args.speed, duration_ms=args.duration_ms)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("ok") else 1

    if args.cmd == "reboot":
        res = reboot_camera(args.ip)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("ok") else 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
