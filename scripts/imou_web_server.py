#!/usr/bin/env python3
"""
Local Web Studio Server for Imou / Dahua IP Camera Manager (SmartPSS Replacement).
Runs on http://127.0.0.1:8766 using Python's built-in ThreadingHTTPServer with per-camera locking.
"""
import argparse
import json
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict
from urllib.parse import parse_qs, urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

import imou_cli  # noqa: E402

_LOCKS_GUARD = threading.Lock()
_CAM_LOCKS: Dict[str, threading.Lock] = {}


def get_cam_lock(ip: str) -> threading.Lock:
    with _LOCKS_GUARD:
        if ip not in _CAM_LOCKS:
            _CAM_LOCKS[ip] = threading.Lock()
        return _CAM_LOCKS[ip]


class ImouStudioHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        pass

    def _send_json(self, data: dict, status: int = 200) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw) if raw else {}

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        try:
            if path in ("/", "/index.html"):
                html_file = SKILL_DIR / "assets" / "index.html"
                raw = html_file.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(raw)
                return

            if path == "/api/cameras":
                scan_all = qs.get("scan_all", ["0"])[0] == "1"
                cams = imou_cli.discover_lan_cameras(scan_all=scan_all)
                self._send_json({"ok": True, "cameras": cams, "subnets": imou_cli.detect_local_subnet_prefixes()})
                return

            if path == "/api/camera/inspect":
                ip = qs.get("ip", [""])[0].strip()
                if not ip:
                    self._send_json({"ok": False, "error": "Missing ip"}, 400)
                    return
                with get_cam_lock(ip):
                    res = imou_cli.inspect_camera(ip)
                self._send_json(res)
                return

            if path == "/api/camera/snapshot":
                ip = qs.get("ip", [""])[0].strip()
                sub = 1 if qs.get("sub", ["0"])[0] == "1" else 0
                if not ip:
                    self._send_json({"ok": False, "error": "Missing ip"}, 400)
                    return
                safe_ip = ip.replace(".", "_")
                snap_path = Path(f"/tmp/imou_studio_snap_{safe_ip}.jpg")
                res = imou_cli.capture_snapshot(ip, snap_path, subtype=sub)
                if not res.get("ok") or not snap_path.exists():
                    self._send_json(res, 502)
                    return
                img_bytes = snap_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(img_bytes)))
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
                self.end_headers()
                self.wfile.write(img_bytes)
                return

            self._send_json({"ok": False, "error": "Not found"}, 404)
        except Exception as e:
            self._send_json({"ok": False, "error": str(e), "trace": traceback.format_exc()}, 500)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        try:
            body = self._read_json_body()

            if path in ("/api/camera/add", "/api/camera/update"):
                ip = str(body.get("ip", "")).strip()
                if not ip:
                    self._send_json({"ok": False, "error": "Vui lòng nhập địa chỉ IP của camera"}, 400)
                    return
                old_ip = str(body.get("old_ip", "")).strip()
                if old_ip and old_ip != ip:
                    imou_cli.delete_camera_registry(old_ip, remove_keychain=False)

                name = str(body.get("name") or "").strip() or None
                model = str(body.get("model") or "").strip() or None
                port = int(body.get("port") or 37777)
                rtsp_port = int(body.get("rtsp_port") or 554)
                username = str(body.get("username") or "admin").strip()
                notes = body.get("notes")
                pwd = str(body.get("password") or "").strip()
                sync_osd = bool(body.get("sync_osd_title", False))

                with get_cam_lock(ip):
                    cam = imou_cli.upsert_camera_registry(
                        ip=ip,
                        name=name,
                        model=model,
                        port=port,
                        rtsp_port=rtsp_port,
                        username=username,
                        notes=str(notes) if notes is not None else None,
                        name_source="user" if name else None,
                    )
                    if pwd:
                        imou_cli.keychain_set(f"{imou_cli.KEYCHAIN_PREFIX}{ip}", username, pwd)

                    osd_sync_res = None
                    if sync_osd and name and imou_cli.get_camera_password(ip):
                        osd_sync_res = imou_cli.update_osd(ip=ip, channel_name=name, port=port, username=username)

                    inspected = None
                    if imou_cli.get_camera_password(ip):
                        inspected = imou_cli.inspect_camera(ip, port=port, username=username, password=pwd or None)
                        if inspected.get("ok") and inspected.get("camera_meta"):
                            cam = inspected["camera_meta"]

                    cam["online"] = imou_cli.probe_dhip_alive(ip, port, timeout=1.0)
                    cam["has_saved_password"] = imou_cli.get_camera_password(ip) is not None

                self._send_json(
                    {
                        "ok": True,
                        "camera": cam,
                        "inspect": inspected,
                        "osd_sync": osd_sync_res,
                        "cameras": imou_cli.discover_lan_cameras(scan_all=False),
                    }
                )
                return

            if path == "/api/camera/delete":
                ip = str(body.get("ip", "")).strip()
                remove_key = bool(body.get("remove_keychain", False))
                if not ip:
                    self._send_json({"ok": False, "error": "Thiếu địa chỉ IP"}, 400)
                    return
                with get_cam_lock(ip):
                    imou_cli.delete_camera_registry(ip, remove_keychain=remove_key)
                self._send_json({"ok": True, "cameras": imou_cli.discover_lan_cameras(scan_all=False)})
                return

            if path == "/api/camera/password":
                ip = str(body.get("ip", "")).strip()
                user = str(body.get("username") or "admin").strip()
                pwd = str(body.get("password") or "").strip()
                if not ip or not pwd:
                    self._send_json({"ok": False, "error": "Vui lòng nhập đầy đủ IP và mật khẩu"}, 400)
                    return
                with get_cam_lock(ip):
                    # Save password to Keychain and ensure camera is registered in cameras.json
                    imou_cli.keychain_set(f"{imou_cli.KEYCHAIN_PREFIX}{ip}", user, pwd)
                    imou_cli.upsert_camera_registry(ip=ip, username=user)
                    res = imou_cli.inspect_camera(ip, username=user, password=pwd)
                res["cameras"] = imou_cli.discover_lan_cameras(scan_all=False)
                self._send_json(res)
                return

            if path == "/api/camera/osd":
                ip = str(body.get("ip", "")).strip()
                with get_cam_lock(ip):
                    res = imou_cli.update_osd(
                        ip=ip,
                        logo_enabled=body.get("logo_enabled"),
                        common_osd_enabled=body.get("common_osd_enabled"),
                        time_enabled=body.get("time_enabled"),
                        show_week=body.get("show_week"),
                        channel_name=body.get("channel_name"),
                        font_size=body.get("font_size"),
                        custom_text=body.get("custom_text"),
                        custom_enabled=body.get("custom_enabled"),
                        logo_rect=body.get("logo_rect"),
                        time_rect=body.get("time_rect"),
                    )
                    if not res.get("ok"):
                        self._send_json(res)
                        return
                    inspected = imou_cli.inspect_camera(ip)
                self._send_json({"ok": True, "update": res, "camera": inspected})
                return

            if path == "/api/camera/image":
                ip = str(body.get("ip", "")).strip()
                with get_cam_lock(ip):
                    res = imou_cli.update_image(
                        ip=ip,
                        brightness=body.get("brightness"),
                        contrast=body.get("contrast"),
                        saturation=body.get("saturation"),
                        gamma=body.get("gamma"),
                        hue=body.get("hue"),
                        chroma_suppress=body.get("chroma_suppress"),
                        style=body.get("style"),
                        sharpness=body.get("sharpness"),
                        mirror=body.get("mirror"),
                        flip=body.get("flip"),
                        rotate90=body.get("rotate90"),
                        day_night_color=body.get("day_night_color"),
                        infrared=body.get("infrared"),
                        wdr=body.get("wdr"),
                        anti_flicker=body.get("anti_flicker"),
                        exposure_compensation=body.get("exposure_compensation"),
                    )
                    if not res.get("ok"):
                        self._send_json(res)
                        return
                    inspected = imou_cli.inspect_camera(ip)
                self._send_json({"ok": True, "update": res, "camera": inspected})
                return

            if path == "/api/camera/encode":
                ip = str(body.get("ip", "")).strip()
                with get_cam_lock(ip):
                    res = imou_cli.update_encode(
                        ip=ip,
                        main_codec=body.get("main_codec"),
                        main_fps=body.get("main_fps"),
                        main_bitrate=body.get("main_bitrate"),
                        main_bitrate_ctrl=body.get("main_bitrate_ctrl"),
                        main_audio=body.get("main_audio"),
                        sub_codec=body.get("sub_codec"),
                        sub_fps=body.get("sub_fps"),
                        sub_bitrate=body.get("sub_bitrate"),
                        sub_audio=body.get("sub_audio"),
                    )
                    if not res.get("ok"):
                        self._send_json(res)
                        return
                    inspected = imou_cli.inspect_camera(ip)
                self._send_json({"ok": True, "update": res, "camera": inspected})
                return

            if path == "/api/camera/ptz":
                ip = str(body.get("ip", "")).strip()
                action = str(body.get("action", "")).strip()
                speed = int(body.get("speed") or 4)
                duration_ms = int(body.get("duration_ms") or 350)
                with get_cam_lock(ip):
                    res = imou_cli.control_ptz(ip, action, speed=speed, duration_ms=duration_ms)
                self._send_json(res)
                return

            if path == "/api/camera/reboot":
                ip = str(body.get("ip", "")).strip()
                with get_cam_lock(ip):
                    res = imou_cli.reboot_camera(ip)
                self._send_json(res)
                return

            self._send_json({"ok": False, "error": "Not found"}, 404)
        except Exception as e:
            self._send_json({"ok": False, "error": str(e), "trace": traceback.format_exc()}, 500)


def main() -> int:
    parser = argparse.ArgumentParser(description="Imou Camera Studio Web Server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()

    # Ensure user camera registry is initialized on startup
    imou_cli.load_cameras_registry()
    server = ThreadingHTTPServer((args.host, args.port), ImouStudioHandler)
    print(f"Imou Camera Studio listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
