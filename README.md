# LiveView Technologies Camera Analytics

Pulls live snapshot images and configured analytics rules (intrusion zones, line
crossing, loitering, etc.) from Axis and Hikvision (including "LVT"-branded
OEM units, which run Hikvision firmware) cameras, and generates an Excel
report with a photo + overlay of each configured rule per camera, plus an
audit log of anything that failed.

## ALWAYS TRUST BUT VERIFY THE DETECTION SHAPE - if a detection zone looks incorrect log into the camera and verify 

## Prerequisites

- Python 3.11+ (tested on 3.11.9)
- Network access to the target cameras (same LAN or VPN)
- Valid admin/operator credentials for the cameras you're testing

## Setup — the easy way (recommended, especially for coworkers)

You do **not** need to touch a terminal or know any pip commands.

1. Install **Python 3.11 or newer** from <https://www.python.org/downloads/>.
   On the **first** installer screen, tick **"Add python.exe to PATH"** before
   clicking Install. (If you skipped that box, `setup.bat` will still try to
   find Python for you.)
2. Double-click **`setup.bat`**. It builds a private environment in `.venv`
   and installs everything. Wait for **"Setup complete!"**.
3. Double-click **`Run Analytics Writer.bat`** or **`Run Audit Report.bat`**.

That's it — no `.env` file is required. See **Credentials** below.

> **On an LVT-managed laptop the normal download will fail** with an SSL /
> "certificate verify failed" error. That's the corporate network (Zscaler /
> Netskope) inspecting traffic. `setup.bat` handles this automatically: it
> retries using the Windows certificate store (where IT has installed the
> company root CA) and, if needed, a trusted-host fallback. You don't have to
> do anything — just let it run. If it still fails after all three attempts,
> the window stays open so you can copy the error and send it to Marc.

> **"The app opens a black window that disappears instantly."** That means it
> crashed on startup — almost always because setup wasn't run (or didn't
> finish) on that machine. Run `setup.bat` first. The `Run *.bat` launchers now
> keep the window open on any crash so the error is readable.

## Setup — the manual way (if you prefer the terminal)

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

If `pip` fails with an SSL / certificate error on a managed laptop, use the
Windows cert store:

```
pip install --use-feature=truststore -r requirements.txt
```

## Running it

Once set up, the double-click launchers are the simplest way to run either GUI.
From an activated venv you can also run them directly:

**GUI (recommended for most people):**
```
python audit_gui.py            # audit report  (Run Audit Report.bat)
python analytics_writer_gui.py # analytics writer (Run Analytics Writer.bat)
```

**Command-line (same engine, terminal-driven):**
```
python combined.py
```

Both GUIs call into the same underlying engine (`camera_engine.py`) and produce
identical reports — pick whichever fits how you work. The GUIs are the ones to
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
| `MANUFACTURER` | Must contain "axis", "hik", or "lvt" (case-insensitive). Rows with `LVT` are treated as Hikvision-compatible. | Row is flagged as unrecognized and skipped (see below) |
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

### Debug artifacts

Temporary debug outputs and diagnostics are written to `debug_overlay_tests/`.
Keep the folder, and use it as the canonical place for overlay/debug artifacts
from test runs and troubleshooting scripts.

Example:

```bash
python overlay_debug.py --ip 10.23.19.107 --manufacturer lvt --username admin --password secret
```

Adjust the arguments to match the target camera and credentials for your
environment.

## Known limitations

- **TLS verification is intentionally disabled** (`verify=False`) — these
  cameras use self-signed certificates over the local network. This is a
  reasonable tradeoff on a trusted internal network, not something to change
  without also fixing certificate management on the cameras themselves.
- **AXIS Perimeter Defender** (used on fixed thermal cameras and some center pano cameras
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
| `ModuleNotFoundError` on launch | Setup hasn't run on this machine — double-click `setup.bat` and wait for "Setup complete!" |
| `'python' is not recognized` | Python isn't on PATH. Either reinstall Python with "Add python.exe to PATH" ticked, or just run `setup.bat` — it finds Python without PATH |
| `pip` fails with SSL / "certificate verify failed" | Corporate network inspection. `setup.bat` handles it automatically (truststore + trusted-host fallback). Manual: `pip install --use-feature=truststore -r requirements.txt` |
| Fleet Picker: `snowflake-connector-python is not installed` | Driver didn't install. Run `".venv\Scripts\python.exe" -m pip install --use-feature=truststore "snowflake-connector-python[secure-local-storage]==4.7.1"`. See **Fleet Picker (Snowflake)** |
| Fleet Picker: `Couldn't auto-detect your LVT login` | Rare (non-Entra-joined machine). Set env var `SNOWFLAKE_USER=your.name@lvt.com` and retry |
| Fleet Picker: browser login works but queries fail on permissions | Okta role not granted — request `APP-SNOWFLAKE-PRODUCT_ANALYST` |
| Terminal window flashes and vanishes when launching the GUI | Startup crash — run `setup.bat` first. The `Run *.bat` launchers keep the window open on crash so you can read the error |
| GUI window won't open / import error | Make sure `audit_gui.py` and `camera_engine.py` are in the same folder — the GUI imports the engine module directly |
| Verify the status of true 401 errors in VMS. If the unit is online, but errored out, try running a single unit audit on that unit. It could be the unit is in an area with poor connectivity


## File overview

| File | Purpose |
|---|---|
| `setup.bat` | One-click first-time setup (finds Python, builds `.venv`, installs deps, handles corporate SSL). Double-click this first. |
| `Run Analytics Writer.bat` | One-click launcher for the analytics writer GUI |
| `Run Audit Report.bat` | One-click launcher for the audit report GUI |
| `audit_gui.py` | Desktop GUI (CustomTkinter) — the primary way to run this |
| `combined.py` | Terminal/CLI version, same engine |
| `camera_engine.py` | All camera-fetching, parsing, and report-generation logic. No UI code. |
| `requirements.txt` | Pinned dependency versions |
| `.env.example` | Template for an optional `.env`. Not needed normally — camera creds are prompted fresh, and Snowflake config is baked in + auto-detected. Only for overrides (e.g. a non-Entra machine that needs `SNOWFLAKE_USER`) |
| `fleet_catalog.py` | Snowflake/offline-CSV data layer behind the Fleet Picker (Client → Location → TDC → camera rows) |


## Fleet Picker (Snowflake) — pick a unit instead of typing IPs

Both GUIs can pull the fleet straight from Snowflake so you select
**Client → Location → TDC** from dropdowns instead of hand-typing IPs:

- **Audit Report GUI** — the **Fleet Picker** tab (build a multi-client batch, no CSV).
- **Analytics Writer GUI** — the **Pick from fleet…** button (fill IP + vendor for one camera).

**There's nothing to configure — no `.env` file.** LVT's Snowflake
account/warehouse/role are built into the app, and your login is auto-detected
from your Windows (Entra) account. Auth is **SSO / externalbrowser**: the first
query opens your browser for the LVT (Okta) login. **No password or key is
stored** — the browser handles it, and the login token is cached so you're not
prompted every launch.

So on a normal LVT laptop: run `setup.bat` once, open the picker, log in through
the browser when it pops up. That's it.

### The only requirement: the Snowflake driver

`setup.bat` installs it automatically (with the corporate-SSL fallback). When
setup finishes, the summary line should read:

```
Snowflake driver: installed - live Fleet Picker ready
```

If it says **`MISSING`**, or the picker shows **"snowflake-connector-python is
not installed"**, install just the driver with:

```
".venv\Scripts\python.exe" -m pip install --use-feature=truststore "snowflake-connector-python[secure-local-storage]==4.7.1"
```

### Requesting access

Access is requested through **Okta** — the `APP-SNOWFLAKE-PRODUCT_ANALYST` role.
Until your account has that role the browser login will succeed but queries will
fail with a permissions error.

### If it won't connect

| Message in the picker | Fix |
|---|---|
| `snowflake-connector-python is not installed` | Run the driver install command above. |
| `Couldn't auto-detect your LVT login` | Rare (non-Entra-joined machine). Set an env var `SNOWFLAKE_USER=your.name@lvt.com` (or put it in a `.env`) and retry. |
| Browser login works but queries fail with a permission error | Your Okta role isn't granted yet — request `APP-SNOWFLAKE-PRODUCT_ANALYST`. |
| Auto-detect picked the wrong email | Override it: set `SNOWFLAKE_USER=your.name@lvt.com`. |

### Getting the data as a CSV (manual, no picker)

The Fleet Picker runs the query below for you. To build a bulk-upload CSV by hand
instead, run it in Snowflake — the CSV **must** have these 6 columns:

```sql
SELECT
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
WHERE lu.live_unit_serial_nr IN ('TDC14016')
GROUP BY lu.CLIENT_NM, lu.LOCATION_NM, nc.PUBLIC_IP, lu.LIVE_UNIT_SERIAL_NR, cm.MANUFACTURER, cm.MODEL
ORDER BY lu.LOCATION_NM
```

Use whatever filter you want. For bulk collection, query on `CLIENT_NM`; for
accounts with a flattened hierarchy, also add a `LOCATION_NM` filter for sub-clients. 