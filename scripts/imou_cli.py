#!/usr/bin/env python3
"""
Imou / Dahua IP Camera Manager CLI & Core Library (SmartPSS Replacement for macOS).
Provides full LAN discovery (ARP + TCP 37777 DVRIP probe), macOS Keychain credential management,
persistent camera registry (~/.config/imou-camera-studio/cameras.json),
OSD (Logo/Timestamp) customization, Image/Brightness/Color/WDR/IR tuning, Stream Encoding,
RTSP Snapshot, and PTZ control.
"""
import argparse
import concurrent.futures
import json
import os
import re
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

LOGIN_ERROR_MESSAGES = {
    1: "Sai mật khẩu (Safety Code). Vui lòng kiểm tra lại mã Safety Code 8 ký tự dưới đáy camera hoặc mật khẩu trong ứng dụng Imou Life.",
    2: "Tài khoản (Username) không tồn tại trên camera.",
    3: "Quá thời gian chờ đăng nhập (Timeout) tới cổng 37777 của camera.",
    4: "Tài khoản đang được đăng nhập trùng lặp.",
    5: "Camera đang tạm khoá đăng nhập (Account Locked) do nhập sai mật khẩu nhiều lần trước đó. Vui lòng chờ 3–5 phút hoặc khởi động lại nguồn camera rồi thử lại.",
    6: "Tài khoản bị đưa vào danh sách đen (Blacklist) của thiết bị.",
    7: "Thiết bị đang quá tải / bận xử lý.",
    8: "Lỗi kết nối phụ trợ SDK.",
    9: "Không thể kết nối mạng tới cổng TCP 37777 của camera (Kiểm tra IP hoặc kết nối Wi-Fi/LAN).",
    10: " vượt quá số kết nối tối đa của camera.",
}


def format_login_error(code: int) -> str:
    detail = LOGIN_ERROR_MESSAGES.get(
        code,
        "Sai mật khẩu hoặc thiết bị từ chối kết nối. Hãy kiểm tra lại mật khẩu / Safety Code.",
    )
    return f"Đăng nhập thất bại (Mã lỗi SDK {code}): {detail}"


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
        # Delete any existing item with the same service name first to avoid duplicate account conflicts
        subprocess.run(
            ["/usr/bin/security", "delete-generic-password", "-s", service],
            capture_output=True,
            text=True,
            timeout=5,
        )
        res = subprocess.run(
            [
                "/usr/bin/security",
                "add-generic-password",
                "-U",
                "-s",
                service,
                "-a",
                account or "admin",
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


def keychain_delete(service: str) -> bool:
    try:
        res = subprocess.run(
            ["/usr/bin/security", "delete-generic-password", "-s", service],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return res.returncode == 0
    except Exception:
        return False


def get_camera_password(ip: str) -> Optional[str]:
    """Return ONLY the password explicitly stored for this camera IP in Keychain."""
    return keychain_get(f"{KEYCHAIN_PREFIX}{ip}")


def get_candidate_passwords(ip: str) -> List[str]:
    """Never try other cameras' passwords on an IP to avoid locking out the camera's admin account."""
    pwd = get_camera_password(ip)
    return [pwd] if pwd else []


def load_arp_mac_map() -> Dict[str, str]:
    """Read macOS ARP cache (`arp -an`) to map IP -> MAC address."""
    mac_map: Dict[str, str] = {}
    try:
        res = subprocess.run(["/usr/sbin/arp", "-an"], capture_output=True, text=True, timeout=4)
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                m = re.search(r"\((\d+\.\d+\.\d+\.\d+)\)\s+at\s+([0-9a-fA-F:]{11,17})", line)
                if m:
                    ip = m.group(1)
                    raw_mac = m.group(2)
                    if raw_mac and "incomplete" not in raw_mac.lower():
                        parts = [p.zfill(2).upper() for p in raw_mac.split(":")]
                        if len(parts) == 6:
                            mac_map[ip] = ":".join(parts)
    except Exception:
        pass
    return mac_map


def load_cameras_registry() -> List[Dict[str, Any]]:
    """
    Load user camera registry from ~/.config/imou-camera-studio/cameras.json.
    If not created yet, bootstrap it from the bundled default assets/cameras.json.
    """
    if not USER_CAMERAS_FILE.exists():
        default_cams: List[Dict[str, Any]] = []
        if DEFAULT_CAMERAS_FILE.exists():
            try:
                data = json.loads(DEFAULT_CAMERAS_FILE.read_text(encoding="utf-8"))
                default_cams = data.get("cameras", [])
            except Exception:
                default_cams = []
        save_cameras_registry(default_cams)
        return default_cams

    try:
        data = json.loads(USER_CAMERAS_FILE.read_text(encoding="utf-8"))
        user_cams = data.get("cameras", [])
        deleted_ips = set(data.get("deleted_ips", []))
        # Merge any newly bundled default cameras that weren't in user_cams or deleted_ips
        if DEFAULT_CAMERAS_FILE.exists():
            try:
                def_data = json.loads(DEFAULT_CAMERAS_FILE.read_text(encoding="utf-8"))
                existing_ips = {c.get("ip") for c in user_cams if c.get("ip")}
                changed = False
                for dc in def_data.get("cameras", []):
                    dip = dc.get("ip")
                    if dip and dip not in existing_ips and dip not in deleted_ips:
                        user_cams.append(dc)
                        existing_ips.add(dip)
                        changed = True
                if changed:
                    save_cameras_registry(user_cams, list(deleted_ips))
            except Exception:
                pass
        return user_cams
    except Exception:
        return []


def _load_deleted_ips() -> List[str]:
    if USER_CAMERAS_FILE.exists():
        try:
            data = json.loads(USER_CAMERAS_FILE.read_text(encoding="utf-8"))
            return list(data.get("deleted_ips", []))
        except Exception:
            pass
    return []


def save_cameras_registry(cameras: List[Dict[str, Any]], deleted_ips: Optional[List[str]] = None) -> None:
    if deleted_ips is None:
        deleted_ips = _load_deleted_ips()
    USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    clean_cams = []
    for c in cameras:
        item = {
            "ip": c.get("ip", ""),
            "name": c.get("name", ""),
            "model": c.get("model", "Imou/Dahua IPC"),
            "serial_no": c.get("serial_no", ""),
            "firmware": c.get("firmware", ""),
            "mac": c.get("mac", ""),
            "port": int(c.get("port") or 37777),
            "rtsp_port": int(c.get("rtsp_port") or 554),
            "username": c.get("username") or "admin",
            "channel": int(c.get("channel") or 0),
            "notes": c.get("notes", ""),
            "name_source": c.get("name_source", "custom"),
        }
        clean_cams.append(item)
    payload = {"cameras": clean_cams, "deleted_ips": sorted(set(deleted_ips))}
    USER_CAMERAS_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def upsert_camera_registry(
    ip: str,
    name: Optional[str] = None,
    model: Optional[str] = None,
    serial_no: Optional[str] = None,
    firmware: Optional[str] = None,
    mac: Optional[str] = None,
    port: Optional[int] = None,
    rtsp_port: Optional[int] = None,
    username: Optional[str] = None,
    notes: Optional[str] = None,
    name_source: Optional[str] = None,
) -> Dict[str, Any]:
    ip = ip.strip()
    cams = load_cameras_registry()
    deleted_ips = [d for d in _load_deleted_ips() if d != ip]
    found = None
    for c in cams:
        if c.get("ip") == ip:
            found = c
            break
    if not found:
        arp_mac = load_arp_mac_map().get(ip, "")
        found = {
            "ip": ip,
            "name": name or f"Imou Camera ({ip})",
            "model": model or "Imou/Dahua IPC",
            "serial_no": serial_no or "",
            "firmware": firmware or "",
            "mac": mac if mac is not None else arp_mac,
            "port": int(port or 37777),
            "rtsp_port": int(rtsp_port or 554),
            "username": username or "admin",
            "channel": 0,
            "notes": notes if notes is not None else "",
            "name_source": name_source or ("user" if name else "auto"),
        }
        cams.append(found)
    else:
        if name is not None and name.strip():
            found["name"] = name.strip()
        if model is not None and model.strip():
            found["model"] = model.strip()
        if serial_no is not None and serial_no.strip():
            found["serial_no"] = serial_no.strip()
        if firmware is not None and firmware.strip():
            found["firmware"] = firmware.strip()
        if mac is not None and mac.strip():
            found["mac"] = mac.strip()
        if port is not None:
            found["port"] = int(port)
        if rtsp_port is not None:
            found["rtsp_port"] = int(rtsp_port)
        if username is not None and username.strip():
            found["username"] = username.strip()
        if notes is not None:
            found["notes"] = notes
        if name_source is not None:
            found["name_source"] = name_source
    save_cameras_registry(cams, deleted_ips)
    return found


def delete_camera_registry(ip: str, remove_keychain: bool = False) -> bool:
    ip = ip.strip()
    cams = load_cameras_registry()
    new_cams = [c for c in cams if c.get("ip") != ip]
    deleted_ips = _load_deleted_ips()
    if ip not in deleted_ips:
        deleted_ips.append(ip)
    save_cameras_registry(new_cams, deleted_ips)
    if remove_keychain:
        keychain_delete(f"{KEYCHAIN_PREFIX}{ip}")
    return len(new_cams) != len(cams)


def probe_dhip_alive(ip: str, port: int = 37777, timeout: float = 1.4) -> bool:
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


def detect_local_subnet_prefixes() -> List[str]:
    prefixes: List[str] = []
    # 1. Check primary outbound interface
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        parts = local_ip.split(".")
        if len(parts) == 4:
            p = f"{parts[0]}.{parts[1]}.{parts[2]}."
            prefixes.append(p)
    except Exception:
        pass
    finally:
        s.close()

    # 2. Check all active IPv4 interfaces via ifconfig
    try:
        res = subprocess.run(["/sbin/ifconfig"], capture_output=True, text=True, timeout=3)
        if res.returncode == 0:
            for m in re.finditer(r"inet\s+(\d+\.\d+\.\d+)\.\d+\s+netmask", res.stdout):
                sub = m.group(1) + "."
                if not sub.startswith("127.") and not sub.startswith("169.254.") and sub not in prefixes:
                    prefixes.append(sub)
    except Exception:
        pass

    if not prefixes:
        prefixes.append("172.16.0.")
    return prefixes


def discover_lan_cameras(subnet_prefix: Optional[str] = None, scan_all: bool = False) -> List[Dict[str, Any]]:
    prefixes = [subnet_prefix] if subnet_prefix else detect_local_subnet_prefixes()
    arp_macs = load_arp_mac_map()
    cams_list = load_cameras_registry()
    registry = {c["ip"]: dict(c) for c in cams_list if c.get("ip")}

    ips_to_check = list(registry.keys())
    if scan_all:
        # Prioritise ARP known hosts first, then full /24 on the primary subnet
        for arp_ip in arp_macs:
            if any(arp_ip.startswith(pref) for pref in prefixes) and arp_ip not in ips_to_check:
                ips_to_check.append(arp_ip)
        primary_prefix = prefixes[0]
        for i in range(1, 255):
            ip = f"{primary_prefix}{i}"
            if ip not in ips_to_check:
                ips_to_check.append(ip)

    online_map: Dict[str, bool] = {}
    probe_timeout = 1.5 if scan_all else 1.2
    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as ex:
        fut_to_ip = {
            ex.submit(probe_dhip_alive, ip, int(registry.get(ip, {}).get("port", 37777)), probe_timeout): ip
            for ip in ips_to_check
        }
        for fut in concurrent.futures.as_completed(fut_to_ip):
            ip = fut_to_ip[fut]
            try:
                online_map[ip] = bool(fut.result())
            except Exception:
                online_map[ip] = False

    results = []
    newly_discovered = False

    # Sort so registered cameras stay in order, followed by newly discovered IPs sorted numerically
    for ip in ips_to_check:
        is_online = online_map.get(ip, False)
        if ip not in registry and not is_online:
            continue

        if ip not in registry and is_online:
            new_cam = {
                "ip": ip,
                "name": f"Imou Camera ({ip})",
                "model": "Imou/Dahua IPC",
                "serial_no": "",
                "firmware": "",
                "mac": arp_macs.get(ip, ""),
                "port": 37777,
                "rtsp_port": 554,
                "username": "admin",
                "channel": 0,
                "notes": "Phát hiện qua quét mạng LAN",
                "name_source": "auto",
            }
            registry[ip] = new_cam
            cams_list.append(new_cam)
            newly_discovered = True

        item = dict(registry[ip])
        if not item.get("mac") and arp_macs.get(ip):
            item["mac"] = arp_macs[ip]
            registry[ip]["mac"] = arp_macs[ip]
            newly_discovered = True

        item["online"] = is_online
        item["has_saved_password"] = get_camera_password(ip) is not None
        results.append(item)

    if newly_discovered:
        save_cameras_registry(list(registry.values()))

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
        return (
            False,
            [],
            "Camera này chưa có mật khẩu trong macOS Keychain. Vui lòng nhập mật khẩu (hoặc mã Safety Code 8 ký tự dưới đáy camera) để kết nối.",
        )

    stdin_payload = "\n".join(json.dumps(r, separators=(",", ":")) for r in requests) + "\n"
    last_err = "Đăng nhập thất bại"

    for cand in candidates:
        env = os.environ.copy()
        env["IMOU_CAM_PASSWORD"] = cand
        try:
            res = subprocess.run(
                [str(BRIDGE_BIN), "rpc", ip, str(port), username],
                input=stdin_payload,
                capture_output=True,
                text=True,
                env=env,
                timeout=18,
            )
        except subprocess.TimeoutExpired:
            last_err = "Quá thời gian kết nối tới camera (Timeout 18s)."
            continue

        out_line = res.stdout.strip()
        try:
            parsed = json.loads(out_line)
        except Exception:
            last_err = out_line or res.stderr.strip() or "Lỗi giao tiếp SDK Bridge"
            continue

        if parsed.get("ok"):
            keychain_set(f"{KEYCHAIN_PREFIX}{ip}", username, cand)
            return True, parsed.get("responses", []), ""
        else:
            code = int(parsed.get("login_error", -1))
            last_err = format_login_error(code)

    return False, [], last_err


def inspect_camera(
    ip: str,
    port: Optional[int] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> Dict[str, Any]:
    reg_map = {c.get("ip"): c for c in load_cameras_registry() if c.get("ip")}
    reg_cam = reg_map.get(ip, {})
    use_port = int(port or reg_cam.get("port") or 37777)
    use_user = str(username or reg_cam.get("username") or "admin")

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
        {"method": "configManager.getConfig", "params": {"channel": 0, "name": "Encode", "onlyLocal": True}},
        {"method": "configManager.getConfig", "params": {"name": "Locales", "onlyLocal": True}},
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
        "Encode",
        "Locales",
    ]
    ok, responses, err = run_rpc_batch(ip, reqs, port=use_port, username=use_user, password=password)
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

    device_type = (raw_tables.get("DeviceType") or {}).get("type", "")
    serial_no = (raw_tables.get("SerialNo") or {}).get("sn", "")
    firmware = ((raw_tables.get("SoftwareVersion") or {}).get("version") or {}).get("Version", "")
    build_date = ((raw_tables.get("SoftwareVersion") or {}).get("version") or {}).get("BuildDate", "")

    # Auto-persist hardware metadata & newly authenticated camera in USER_CAMERAS_FILE
    arp_mac = load_arp_mac_map().get(ip)
    current_name = reg_cam.get("name", "")
    auto_name = None
    if not current_name or current_name.startswith("Imou Camera ("):
        clean_osd = channel_name.replace("ㅤ", "").strip()
        if clean_osd and clean_osd.lower() not in ("ipc", "imou", "camera"):
            auto_name = clean_osd
        elif device_type:
            auto_name = f"{device_type} ({ip.split('.')[-1]})"

    updated_cam = upsert_camera_registry(
        ip=ip,
        name=auto_name if auto_name else None,
        model=device_type if device_type else None,
        serial_no=serial_no if serial_no else None,
        firmware=firmware if firmware else None,
        mac=arp_mac if arp_mac else None,
        port=use_port,
        username=use_user,
    )
    updated_cam["online"] = True
    updated_cam["has_saved_password"] = True

    summary = {
        "device_type": device_type,
        "serial_no": serial_no,
        "firmware": firmware,
        "build_date": build_date,
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
    return {"ok": True, "ip": ip, "camera_meta": updated_cam, "summary": summary, "raw": raw_tables}


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
        # Many Dahua/Imou firmwares reject empty string "" in ChannelTitle with error 268959743;
        # use a single space " " when the user clears the channel name.
        safe_title = channel_name if channel_name != "" else " "
        if isinstance(ch_table, list) and ch_table and isinstance(ch_table[0], dict):
            ch_table[0]["Name"] = safe_title
        elif isinstance(ch_table, dict):
            ch_table["Name"] = safe_title
        else:
            ch_table = {"Name": safe_title}
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

    results = [r.get("data") for r in resps2]
    # Check VideoWidget setConfig (last request in set_reqs)
    if results and isinstance(results[-1], dict) and not results[-1].get("result", False):
        return {"ok": False, "error": "Camera từ chối lưu cấu hình VideoWidget.", "results": results}
    return {"ok": True, "results": results}


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
                if "Sharpness" in entry:
                    entry["Sharpness"] = val
                if "Level" in entry:
                    entry["Level"] = val
                if "Sharpness" not in entry and "Level" not in entry:
                    entry["Sharpness"] = val
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
            if infrared is not None and "InfraRed" in t:
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
    rtsp_port: Optional[int] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> Dict[str, Any]:
    reg_map = {c.get("ip"): c for c in load_cameras_registry() if c.get("ip")}
    reg_cam = reg_map.get(ip, {})
    use_rtsp = int(rtsp_port or reg_cam.get("rtsp_port") or 554)
    use_user = str(username or reg_cam.get("username") or "admin")

    pwd = password or get_camera_password(ip)
    if not pwd:
        return {"ok": False, "error": "Chưa có mật khẩu camera trong Keychain để lấy luồng RTSP."}

    output_path.parent.mkdir(parents=True, exist_ok=True)
    rtsp_url = f"rtsp://{use_user}:{pwd}@{ip}:{use_rtsp}/cam/realmonitor?channel=1&subtype={subtype}"
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

    p_disc = sub.add_parser("discover", help="Danh sách và kiểm tra trạng thái online của các camera Imou")
    p_disc.add_argument("--scan-all", action="store_true", help="Quét toàn bộ mạng LAN trên cổng 37777")

    p_pwd = sub.add_parser("set-password", help="Lưu mật khẩu (hoặc Safety Code) cho camera vào macOS Keychain")
    p_pwd.add_argument("--ip", required=True, help="Địa chỉ IP của camera")
    p_pwd.add_argument("--user", default="admin", help="Username (mặc định: admin)")
    p_pwd.add_argument("--password", help="Mật khẩu camera")
    p_pwd.add_argument("--stdin", action="store_true", help="Đọc mật khẩu từ stdin")

    p_ren = sub.add_parser("rename", help="Đổi tên quản lý của camera (và tuỳ chọn đồng bộ lên OSD ChannelTitle)")
    p_ren.add_argument("--ip", required=True, help="Địa chỉ IP của camera")
    p_ren.add_argument("--name", required=True, help="Tên mới của camera")
    p_ren.add_argument("--notes", help="Ghi chú vị trí / mô tả")
    p_ren.add_argument("--sync-osd", action="store_true", help="Đồng bộ tên này lên OSD ChannelTitle của camera")

    p_insp = sub.add_parser("inspect", help="Đọc toàn bộ thông số OSD, Độ sáng, Màu sắc, Luồng Video của camera")
    p_insp.add_argument("--ip", required=True, help="Địa chỉ IP của camera")

    p_osd = sub.add_parser("set-osd", help="Tuỳ chỉnh tắt/bật Logo Imou, Tên kênh, Ngày tháng năm (OSD)")
    p_osd.add_argument("--ip", required=True, help="Địa chỉ IP của camera")
    p_osd.add_argument("--logo", choices=["on", "off"], help="Bật/tắt Logo / Tên kênh trên góc video")
    p_osd.add_argument("--show-logo", choices=["true", "false"], help="Alias bật/tắt Logo")
    p_osd.add_argument("--time", choices=["on", "off"], help="Bật/tắt Ngày tháng năm trên video")
    p_osd.add_argument("--show-time", choices=["true", "false"], help="Alias bật/tắt Ngày tháng")
    p_osd.add_argument("--show-title", choices=["true", "false"], help="Alias bật/tắt Tên kênh")
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
    p_img.add_argument("--mirror", choices=["on", "off", "true", "false"], help="Đối xứng gương trái-phải (Mirror)")
    p_img.add_argument("--flip", choices=["on", "off", "true", "false"], help="Lật ngược trên-dưới (Flip)")
    p_img.add_argument("--daynight", choices=["auto", "color", "bw"], help="Chế độ Ngày/Đêm (1=Auto, 0=Color, 2=B&W)")
    p_img.add_argument("--ir", choices=["on", "off", "true", "false"], help="Đèn hồng ngoại (InfraRed)")
    p_img.add_argument("--wdr", help="Mức chống ngược sáng WDR (0=Tắt, 1-100 hoặc on/off)")
    p_img.add_argument("--wdr-level", type=int, help="Mức WDR (0-100)")

    p_enc = sub.add_parser("set-encode", help="Chỉnh luồng Video/Audio (H.264/H.265, FPS, Bitrate, Micro)")
    p_enc.add_argument("--ip", required=True, help="Địa chỉ IP của camera")
    p_enc.add_argument("--stream", choices=["main", "sub"], default="main")
    p_enc.add_argument("--compression", choices=["H.264", "H.265"])
    p_enc.add_argument("--main-codec", choices=["H.264", "H.265"])
    p_enc.add_argument("--fps", type=int)
    p_enc.add_argument("--main-fps", type=int)
    p_enc.add_argument("--bitrate", type=int)
    p_enc.add_argument("--main-bitrate", type=int)
    p_enc.add_argument("--audio", choices=["on", "off", "true", "false"])
    p_enc.add_argument("--main-audio", choices=["on", "off", "true", "false"])
    p_enc.add_argument("--sub-audio", choices=["on", "off", "true", "false"])

    p_snap = sub.add_parser("snapshot", help="Chụp ảnh trực tiếp từ camera qua RTSP")
    p_snap.add_argument("--ip", required=True, help="Địa chỉ IP của camera")
    p_snap.add_argument("--output", "--out", dest="output", default="/tmp/imou_snapshot.jpg", help="Đường dẫn file ảnh JPEG")
    p_snap.add_argument("--sub", action="store_true", help="Lấy từ luồng phụ (nhanh hơn)")

    p_ptz = sub.add_parser("ptz", help="Điều khiển quay quét PTZ")
    p_ptz.add_argument("--ip", required=True, help="Địa chỉ IP của camera")
    p_ptz.add_argument("--action", required=True, choices=list(PTZ_COMMANDS.keys()))
    p_ptz.add_argument("--speed", type=int, default=4)
    p_ptz.add_argument("--duration-ms", "--duration", dest="duration_ms", type=int, default=400)

    p_reb = sub.add_parser("reboot", help="Khởi động lại camera")
    p_reb.add_argument("--ip", required=True, help="Địa chỉ IP của camera")

    args = parser.parse_args()

    if args.cmd in ("discover", "status"):
        cams = discover_lan_cameras(scan_all=getattr(args, "scan_all", False))
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

    if args.cmd == "rename":
        cam = upsert_camera_registry(ip=args.ip, name=args.name, notes=args.notes, name_source="user")
        osd_res = None
        if args.sync_osd:
            osd_res = update_osd(args.ip, channel_name=args.name)
        print(json.dumps({"ok": True, "camera": cam, "osd_sync": osd_res}, indent=2, ensure_ascii=False))
        return 0

    if args.cmd == "inspect":
        info = inspect_camera(args.ip)
        print(json.dumps(info, indent=2, ensure_ascii=False))
        return 0 if info.get("ok") else 1

    if args.cmd == "set-osd":
        title = " " if args.clear_title else args.title
        logo_val = None
        if args.logo:
            logo_val = args.logo == "on"
        elif args.show_logo:
            logo_val = args.show_logo == "true"
        elif args.show_title:
            logo_val = args.show_title == "true"

        time_val = None
        if args.time:
            time_val = args.time == "on"
        elif args.show_time:
            time_val = args.show_time == "true"

        res = update_osd(
            args.ip,
            logo_enabled=logo_val,
            time_enabled=time_val,
            show_week=(args.week == "on") if args.week else None,
            channel_name=title,
            font_size=args.font_size,
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("ok") else 1

    if args.cmd == "set-image":
        dn_map = {"color": 0, "auto": 1, "bw": 2}
        wdr_val = args.wdr_level
        if wdr_val is None and args.wdr is not None:
            if str(args.wdr).lower() == "on":
                wdr_val = 50
            elif str(args.wdr).lower() == "off":
                wdr_val = 0
            else:
                wdr_val = int(args.wdr)
        res = update_image(
            args.ip,
            brightness=args.brightness,
            contrast=args.contrast,
            saturation=args.saturation,
            gamma=args.gamma,
            sharpness=args.sharpness,
            style=args.style,
            mirror=(args.mirror in ("on", "true")) if args.mirror else None,
            flip=(args.flip in ("on", "true")) if args.flip else None,
            day_night_color=dn_map[args.daynight] if args.daynight else None,
            infrared=(args.ir in ("on", "true")) if args.ir else None,
            wdr=wdr_val,
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("ok") else 1

    if args.cmd == "set-encode":
        codec = args.main_codec or args.compression
        fps = args.main_fps or args.fps
        bitrate = args.main_bitrate or args.bitrate
        audio = args.main_audio or args.audio
        res = update_encode(
            args.ip,
            main_codec=codec if args.stream == "main" else None,
            main_fps=fps if args.stream == "main" else None,
            main_bitrate=bitrate if args.stream == "main" else None,
            main_audio=(audio in ("on", "true")) if (audio and args.stream == "main") else None,
            sub_codec=codec if args.stream == "sub" else None,
            sub_fps=fps if args.stream == "sub" else None,
            sub_bitrate=bitrate if args.stream == "sub" else None,
            sub_audio=(args.sub_audio in ("on", "true")) if args.sub_audio else None,
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
