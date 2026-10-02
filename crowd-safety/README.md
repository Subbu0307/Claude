# CrowdWatch: crowd-density alerts from existing CCTV

CrowdWatch watches a temple's (or any venue's) existing CCTV cameras, estimates how many people are
standing in each marked area, converts that to **people per square metre**, and warns staff on
WhatsApp, a control-room dashboard, or any webhook *before* a crowd reaches crush density.

```
 CCTV / NVR (RTSP) ─► person detector ─► people per zone ─► density/m² ─► level + trend ─► alerts
   existing cameras      YOLOX, CPU         floor polygons     calibrated     hysteresis      WhatsApp
                                                                area           rate of rise    dashboard
                                                                                               webhook
```

## Why density

Crowd crushes happen when density climbs past roughly **4–5 people/m²**. At that point people
can no longer control their own movement, and a push or a stumble travels through the crowd as a
wave. Below ~2/m² people move freely. Density is the measure that predicts danger; a raw head count
doesn't.

| Level | Default (people/m²) | What staff see | Suggested action |
|---|---|---|---|
| 🟢 normal | < 2 | dashboard | — |
| 🟡 busy | 2–3 | dashboard | watch |
| 🟠 warning | 3–4 | WhatsApp + dashboard | slow inflow, open alternate routes |
| 🔴 critical | ≥ 4 | WhatsApp to everyone incl. police, repeated every 2 min, dashboard alarm | stop entry, open exits |
| 📈 rising fast | climbing ≥ 0.75/m² per minute | WhatsApp, with "critical in ~N min" | act early |

Thresholds are set per zone. Use lower ones where the crowd moves (stairs, ramps, corridors), because
moving crowds become dangerous at lower densities than standing ones.

## What it does

- **Works with existing cameras**: anything that gives an RTSP/HTTP stream (Hikvision, Dahua and
  CP Plus NVRs all do), a webcam, or a recorded video file.
- **Zones on the floor**: you outline each area (queue pen, hall, stairs) on the camera image.
  CrowdWatch works out its true floor area from 4 measured reference marks, correcting for
  perspective, or you can enter the area directly.
- **Stable, early alerts**: escalates immediately, but only steps down after density has stayed
  clearly lower for a minute, so staff aren't spammed by a crowd hovering at a threshold. It also
  predicts how many minutes remain until critical.
- **Fails loudly**: a frozen or disconnected camera raises a "Camera OFFLINE, post a steward" alert
  and its zones turn grey ("No live data"). They are never shown as safe. If the dashboard loses its
  connection to the server, it says so in red.
- **Routing by severity**: e.g. control room from *busy*, duty officer from *warning*, police from
  *critical*.
- **Drill mode**: simulated crowds ramp through every level so you can rehearse the response and
  confirm WhatsApp delivery, with no cameras needed.
- **Privacy by design**: frames are analysed in memory and never written to disk. No face
  recognition, no identification, only counts. The dashboard is password-protected.

## Quick start (5 minutes, no cameras)

```bash
cd crowd-safety
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# Person-detection model (YOLOX-S, Apache-2.0, 36 MB)
curl -L -o models/yolox_s.onnx \
  https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_s.onnx

cp site.example.yaml site.yaml
python -m crowdwatch.cli drill site.yaml --ramp 60
# open http://127.0.0.1:8080 and watch zones go 🟢 → 🔴 → 🟢; alerts print in the terminal
```

## Setting up a real site

1. **Pick cameras.** Prefer cameras mounted high and looking down at queue pens, gates, stairs and
   the area in front of the sanctum. Overhead views keep heads separate; low, horizontal views hide
   people behind each other. Use the NVR's sub-stream (~720p) to save CPU.
2. **Grab a frame** from each camera (VLC → Video → Take snapshot, or the NVR app).
3. **Draw zones** in `site.yaml`: list the pixel corners of each floor area (any image viewer or
   MS Paint shows pixel coordinates under the cursor).
4. **Calibrate the floor area.** Either measure each zone's area with a tape (`area_m2`), or mark 4
   points on the floor (tile corners, pillar bases), measure their real positions in metres, and add
   them as `ground_calibration`. The 4 points must not lie on one line.
5. **Check it**:
   ```bash
   python -m crowdwatch.cli check site.yaml --frame gate1.jpg --camera gate1
   ```
   This prints each zone's area and how many people make it critical, and writes `zones_preview.jpg`
   with zones, detected people and their foot positions. Make sure the zones cover the floor you meant.
6. **Validate counts at peak time (important).** In dense crowds, people hide behind each other and
   any camera-based detector **undercounts**. On a busy day, take 5–10 frames per camera, count heads
   by hand in each zone, and compare with what `check` reports. Set
   `count_multiplier = manual count ÷ detected count` for that zone (typically 1.1–1.6). Repeat after
   moving a camera. Until this is done, treat readings as a lower bound.
7. **Alerts on WhatsApp.** Set `WHATSAPP_ACCESS_TOKEN` and `WHATSAPP_PHONE_NUMBER_ID` (Meta WhatsApp
   Cloud API). WhatsApp only delivers free text to people who messaged you in the last 24h, so create
   and get approval for a **utility template** with one parameter, e.g. *"Crowd alert: {{1}}"*, and
   set `WHATSAPP_ALERT_TEMPLATE=<template name>`. Then run a drill and confirm every phone gets it.
8. **Run it**:
   ```bash
   export CROWDWATCH_DASHBOARD_PASSWORD='choose-a-strong-one'
   python -m crowdwatch.cli run site.yaml --host 0.0.0.0 --port 8080
   ```
   Run it on a PC on the same network as the NVR, ideally on a UPS. Put the dashboard on a big screen
   in the control room and press **Enable alarm sound**.

## Hardware

On a 4-core 2.8 GHz CPU, YOLOX-S takes about 0.2 s per frame, or ~0.4 s with 2×2 tiling (which finds
small, distant people in wide shots). At one analysis every 2 s per camera, an ordinary 4–8 core PC
handles roughly 4–8 cameras. For more cameras, use `yolox_tiny.onnx` (~5× faster, less accurate on
small people), run several instances, or add an NVIDIA GPU (`pip install onnxruntime-gpu`).

## Limits: read before relying on it

- **It supports people; it doesn't replace them.** The safety basics still matter most: holding
  areas that let people in in batches, one-way routes, no counter-flow, wide exits, trained stewards,
  and a clear chain of command for "stop entry". CrowdWatch tells them *where* and *when* to act sooner.
- **Undercounting in very dense crowds** (step 6). Above ~5/m² heads overlap heavily. A
  density-map model (e.g. CSRNet, DM-Count) estimates very dense crowds better than a detector; the
  `Detector` interface in `crowdwatch/detector.py` is where such a model would plug in.
- **Camera coverage is the ceiling.** Areas with no camera aren't monitored; list them in the safety
  plan and staff them.
- **Test before every big festival**: run a drill, check every camera shows LIVE, and confirm alerts
  reach every phone.
- **Alerts must never cause panic.** They go to staff, not to the public. Public announcements should
  be calm and specific ("please wait at the tank area, darshan resumes in 10 minutes").

## Development

```bash
python -m pytest
```

The tests cover geometry and calibration, config validation, level changes with hysteresis, trend
and ETA, alert routing/retries/repeats, camera-offline handling, dashboard auth, YOLOX decoding,
tiling, and drill isolation between cameras. They use fake detectors and clocks, so they need no
model or camera.

| File | Purpose |
|---|---|
| `crowdwatch/config.py` | site config, thresholds, validation |
| `crowdwatch/geometry.py` | polygon area, perspective calibration |
| `crowdwatch/detector.py` | YOLOX person detector (ONNX Runtime), tiling |
| `crowdwatch/density.py` | per-zone density, smoothing, levels, trend |
| `crowdwatch/alerts.py` | alert policy, WhatsApp/webhook/console delivery |
| `crowdwatch/monitor.py` | camera capture, reconnects, offline detection |
| `crowdwatch/server.py`, `static/dashboard.html` | control-room dashboard |
| `crowdwatch/cli.py` | `check`, `run`, `drill` |
