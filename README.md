# LiveView Technologies Camera Analytics

Pulls live snapshot images and configured analytics rules (intrusion zones, line
crossing, loitering, etc.) from Axis and Hikvision (including "LVT"-branded
OEM units, which run Hikvision firmware) cameras, and generates an Excel
report with a photo + overlay of each configured rule per camera, plus an
audit log of anything that failed.

## Prerequisites

- Python 3.11+ (tested on 3.11.9)
- Network access to the target cameras (same LAN or VPN)
- Valid admin/operator credentials for the cameras you're testing

## Setup

```
pip install -r requirements.txt
```

That's it — no `.env` file is required. See **Credentials** below.

## Running it

**GUI (recommended for most people):**
```
python gui_app.py
```

**Command-line (same engine, terminal-driven):**
```
python combined.py
```

Both call into the same underlying engine (`camera_engine.py`) and produce
identical reports — pick whichever fits how you work. The GUI is the one to
point non-technical coworkers at.

## How it works

Every camera IP is checked on **3 fixed ports** (`5010`/`5015`/`5020`, mapped
to CENTER/LEFT/RIGHT positions) — this matches the standard 3-camera unit
layout. If your setup doesn't follow that convention, this tool isn't
going to be useful without code changes to `CAMERA_CONFIGS` in
`camera_engine.py`.

For each port, it:
1. Fetches a live snapshot image.
2. Fetches the camera's configured analytics rules (Axis Object Analytics API,
   or Hikvision's Intelligent/behaviorRule API).
3. Draws the rule's zone/line over the snapshot and embeds it as a thumbnail
   in the report.

### Two modes

- **Single Camera Test** — check one IP, useful for verifying a single unit.
- **CSV Batch** — process a whole fleet from a CSV file.

### CSV format

Required column: `IP`. Recognized optional columns:

| Column | Purpose | Default if blank |
|---|---|---|
| `MANUFACTURER` | Must contain "axis", "hik", or "lvt" (case-insensitive) | Row is flagged as unrecognized and skipped (see below) |
| `CLIENT_NM` | Client/customer name, shown in the report | blank |
| `LOCATION_NM` | Site/location name | blank |
| `LIVE_UNIT_SERIAL_NM` | Unit serial number | blank |

Rows with a blank `IP` are skipped with a warning. Rows with a `MANUFACTURER`
value that doesn't clearly match a known vendor are **not silently guessed** —
they get flagged in the report's "Missed Cameras" tab, and the tool still
attempts a snapshot using whatever vendor API formats it has credentials for,
so you at least get a photo instead of nothing.

## Credentials

You'll be prompted for Axis and/or Hikvision credentials at the start of every
run, but **only for whichever vendor(s) are actually present** in that run's
cameras. This is deliberate: it always asks fresh rather than silently reusing
a cached value, so a stale or wrong password can't cause a run to look like it
worked when it didn't.

**Do not put real credentials in a committed file.** There's an `access.env`
file that may exist locally from earlier testing — it is *not* meant to be
shared or committed, and as of this version it has no effect anyway (see
`.env.example` for why). If you're setting this up in a shared/git location,
make sure `.gitignore` is respected and no `.env`/`access.env` file with real
values goes with it.

## Output

Reports land in `~/Downloads/Camera_Reports_Master/`, named
`<tag or CSV filename>_Master_<timestamp>.xlsx`, with two sheets:

- **Camera Analytics** — one row per configured rule (or a placeholder row if
  a camera has no rules), with the rule's zone drawn over a live snapshot.
- **Missed Cameras** — every failure: connection errors, timeouts, rejected
  credentials, or a camera running an analytics engine this tool doesn't
  support (see Known Limitations).

## Known limitations

- **TLS verification is intentionally disabled** (`verify=False`) — these
  cameras use self-signed certificates over the local network. This is a
  reasonable tradeoff on a trusted internal network, not something to change
  without also fixing certificate management on the cameras themselves.
- **AXIS Perimeter Defender** (used on fixed thermal cameras instead of the
  standard AXIS Object Analytics app) is **detected but not supported** — the
  tool flags these as `[SPECIAL CASE]` in Missed Cameras rather than pulling
  real rule data, because Perimeter Defender's configuration is stored in an
  opaque, undocumented binary format. This is confirmed against a real device.
- **Hikvision thermal detection is a best-effort heuristic**, not confirmed
  against a real failing device (unlike the Axis case above). It flags model
  numbers containing `2TD` as likely thermal/specialty units. If you hit a
  Hikvision thermal camera that isn't flagged, or a false positive, that
  heuristic in `camera_engine.py` (`THERMAL_MODEL_MARKERS`) is the place to
  adjust it.
- **Account lockouts**: most cameras lock an account after repeated failed
  logins (Hikvision defaults to ~5 attempts). If you see widespread `401`
  errors across many cameras that you've confirmed have the right password,
  it's very likely a lockout from an earlier bad run, not a current bug —
  wait for the lockout window to clear rather than repeatedly retrying. The
  tool already limits how many failed attempts one bad password can generate
  per device (see `auth_rejected` handling in `camera_engine.py`), but it
  can't undo a lockout that already happened.
- **"LVT" manufacturer values are assumed to be Hikvision-compatible OEM
  units** (same credentials, same API). If that's ever not true for some
  device, it'll show up as authentication failures for that specific unit.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Every camera shows a gray/black thumbnail | Snapshot fetch is failing — check the Missed Cameras tab for the actual error (timeout, 401, etc.), not just the main sheet |
| Widespread `401 Unauthorized` across many devices | Check you can log into one device's web UI manually with the same credentials. If that works, it's likely an account lockout from a prior bad run, not this run's password |
| A camera hits the wrong API (e.g. Axis URL for a Hikvision unit) | Check the exact `MANUFACTURER` value in the CSV, and the column header spelling — must be exactly `MANUFACTURER` |
| `ModuleNotFoundError` on launch | Run `pip install -r requirements.txt` |
| GUI window won't open / import error | Make sure `gui_app.py` and `camera_engine.py` are in the same folder — the GUI imports the engine module directly |

## File overview

| File | Purpose |
|---|---|
| `gui_app.py` | Desktop GUI (CustomTkinter) — the primary way to run this |
| `combined.py` | Terminal/CLI version, same engine |
| `camera_engine.py` | All camera-fetching, parsing, and report-generation logic. No UI code. |
| `requirements.txt` | Pinned dependency versions |
| `.env.example` | Template for local credential env vars (currently unused by either entry point — see note in the file) |


## Getting data
| In snowflake you will want to use the below query:
|SELECT
    nc.PUBLIC_IP AS IP, 
    lu.LIVE_UNIT_SERIAL_NR AS LIVE_UNIT_SERIAL_NM,
    lu.LOCATION_NM,
    lu.CLIENT_NM,
    cm.MANUFACTURER,
    cm.MODEL
FROM STAGING.HORUS.NETWORK_CAMERAS nc
JOIN EDW.DM_PRODUCT.LIVE_UNIT lu
    ON nc.LIVE_UNIT_ID = lu.LIVE_UNIT_ID
LEFT JOIN STAGING.HORUS.CAMERA_MFTRS cm
    ON nc.CAMERA_MFTRS_ID = cm.ID
WHERE lu.live_unit_serial_nr in ('TDC14016')
GROUP BY lu.CLIENT_NM, lu.LOCATION_NM, nc.PUBLIC_IP, lu.LIVE_UNIT_SERIAL_NR, cm.MANUFACTURER, cm.MODEL
ORDER BY lu.LOCATION_NM
|Use whatever filtering clause you want to retrieve the data but it the csv file for bulk upload most have those 6 columns