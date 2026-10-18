# Dahua / Imou Port 37777 RPC2 Configuration Reference

Imou consumer IP cameras (`IPC-A26HS`, `IPC-C22C`, `IPC-F22-D`, `IPC-S42F-D`, `IPC-DK2`, `IPC-TA32CP`) disable the legacy HTTP `/cgi-bin/` web UI on port 80, but expose the full Dahua binary protocol (`DVRIP` + `ROAE-128` encrypted `RPC2`) on **TCP port 37777**.

## Core RPC2 Config Tables (`configManager.getConfig` / `configManager.setConfig`)

### 1. `ChannelTitle` (channel: 0)
- Firmware 2.x returns a list: `[{"Name": "Imou"}]`
- Firmware 3.x returns a dict: `{"Name": ""}`
- Setting `"Name": " "` clears the default `"Imou"` label.

### 2. `VideoWidget` (channel: 0)
Controls OSD overlays on the encoded video stream:
- `ChannelTitle`:
  - `EncodeBlend` (`bool`): Burn channel title / logo into recorded & RTSP stream.
  - `PreviewBlend` (`bool`): Show channel title in preview.
  - `Rect` (`[x1, y1, x2, y2]` in `0..8191` normalized coordinates).
- `CommonOsdEnable` (`bool`, firmware 3.x+): Controls the Imou brand watermark overlay.
- `TimeTitle`:
  - `EncodeBlend` (`bool`): Burn date/time into recorded & RTSP stream.
  - `PreviewBlend` (`bool`): Show date/time in preview.
  - `ShowWeek` (`bool`): Show day-of-week next to timestamp.
  - `Rect` (`[x1, y1, x2, y2]` in `0..8191` coordinates).
- `CustomTitle`: Array of custom text overlays (`EncodeBlend`, `Text`, `Rect`).
- `FontSize` (`int`): OSD font size (`16`, `24`, `32`, `48`).

### 3. `VideoColor` (channel: 0)
Array of 3 time-section color profiles (`0..100`, default `50`):
- `Brightness`, `Contrast`, `Saturation`, `Gamma`, `Hue`, `ChromaSuppress`
- `Style`: `"Standard"`, `"Soft"`, `"Vivid"`

### 4. `VideoInSharpness` (channel: 0)
- `Sharpness` (`0..100`), `Level` (`0..100`), `Mode` (`1`)

### 5. `VideoInOptions` (channel: 0)
Sensor & optics parameters (mirrored in root, `NormalOptions`, and `NightOptions`):
- `Mirror` (`bool`): Horizontal mirror.
- `Flip` (`bool`): Vertical flip (ceiling mount).
- `Rotate90` (`0`, `1`, `2`, `3`): 90-degree rotation mode.
- `DayNightColor` (`0` = Color, `1` = Auto, `2` = B&W).
- `InfraRed` (`bool`): IR LED control.
- `WideDynamicRange` (`0..100`) & `WideDynamicRangeMode` (`0` or `1`).
- `AntiFlicker` (`0` = Outdoor, `1` = 50Hz, `2` = 60Hz).
- `ExposureCompensation` (`0..100`, default `50`).

### 6. `Encode` (channel: 0)
- `MainFormat[0]` (Main high-res stream, RTSP `subtype=0`):
  - `AudioEnable` (`bool`)
  - `Video`: `Compression` (`"H.264"`, `"H.265"`), `Width`, `Height`, `FPS`, `BitRate`, `BitRateControl` (`"CBR"`, `"VBR"`), `GOP`.
- `ExtraFormat[0]` (Sub stream, RTSP `subtype=1`):
  - `AudioEnable` (`bool`)
  - `Video`: `Compression`, `Width`, `Height`, `FPS`, `BitRate`, `BitRateControl`.
