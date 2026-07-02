---
name: camera-audit
description: Runs a quick single-camera Axis/Hikvision analytics check (camera_engine.py) to generate an Excel report of configured analytics rules (intrusion zones, line crossing, loitering) with photo overlays for one camera IP. Also supports a CSV batch mode for a whole fleet, if the user already has a CSV file on disk with camera IPs (this tool can't accept a file upload/attachment directly). Use this whenever the user wants to check, test, audit, or generate a report for a security camera from this project -- phrases like "check this camera," "run a camera audit," "test this camera IP," "pull analytics rules," or "generate the camera report" should all trigger it, even if they don't name the skill directly. Also use it when the user asks why a prior camera audit run showed failures/401s.
---
##This tool assumes you have access to snowflake and can provide unit/camera IP address. This is built to handle Axis and Hikvision camera. Dahua implemention to be added in later. This will not take a snapshot of quad view cameras 


# Camera Audit

Kicks off `camera_engine.run_batch()` (the same engine behind `gui_app.py` and
`combined.py`) through a script built for Claude to drive: `scripts/run_audit.py`
takes plain CLI arguments instead of interactive menus or a Tkinter file picker.

## Why credential handling looks the way it does

This tool logs into live security cameras with admin/operator credentials.
Those credentials must never be typed into this conversation or passed as a
CLI argument (they'd end up in shell history, logs, or the chat transcript).
They also need to be asked fresh every run rather than cached, so a stale or
wrong password can't make a run silently look like it worked when it didn't
(same reasoning the underlying tool already uses).

Claude Code's own command-running tools don't give the user a real keyboard
to type into -- a `getpass()` prompt run through them just hangs forever with
no way to respond. So `run_audit.py` must be launched in a **separate, real
console window** the user can see and type into, not captured inline. Claude
then finds out the run finished by watching for the output file to appear,
not by reading the launched window's output.

## Step 1: Get the inputs

**Default to single-camera mode.** Most requests are "check this one camera" --
ask for the IP and manufacturer (`axis` or `hikvision`; "LVT"-branded units
also count as `hikvision` -- see `references/troubleshooting.md`).
Client/location/serial are optional. This tool has no way to accept a file
upload/attachment, so don't ask the user to "upload a CSV" for a one-off
check -- just get the two required values directly in conversation.

**Only reach for CSV batch mode if the user says they have multiple cameras**
(a fleet, a site, "all our cameras at X") **and already has a CSV file sitting
on disk** -- Claude can read a CSV that exists as a real file and pass its
path to `run_audit.py --csv`, but can't accept one uploaded through chat.
Required column: `IP`. Optional: `MANUFACTURER` (must contain "axis", "hik",
or "lvt" -- case-insensitive; other values get flagged in the report instead
of guessed), `CLIENT_NM`, `LOCATION_NM`, `LIVE_UNIT_SERIAL_NM`. If the user
wants to build this CSV from Snowflake, see `references/snowflake_query.md`.
If a row has a blank `IP`, say plainly that it's automatically skipped --
`camera_engine.py` returns early on an empty IP before it ever opens a
connection, so this is a safe no-op, not something that risks failing
partway through or needs the user to edit the CSV first. Still worth naming
which row it is (by serial/location if present) so they can add the IP later
if that camera was supposed to be included.

Don't guess a manufacturer from an ambiguous value -- ask the user rather than
picking one, since sending an Axis request to a Hikvision unit (or vice versa)
just produces a confusing failure instead of a clean error.

**Mixed-vendor units.** A physical 3-camera unit's CENTER/LEFT/RIGHT
positions (ports 5010/5015/5020) are sometimes not all the same vendor --
e.g. two Hikvision cameras and one Axis. If the user says a unit is mixed,
use `--manufacturer mixed`. This asks for **both** credential sets up front
(you don't need to ask the user which port is which vendor -- `camera_engine.py`
probes each port itself: cheap snapshot check with Axis first, then Hikvision
if Axis doesn't answer, and only the vendor that actually responds on a given
port gets the full analytics fetch there) and produces one merged report.
Each port's row in "Camera Position" is labeled with which vendor resolved it
(e.g. `CENTER [Axis]`), so the user can see the split at a glance. A port
where neither vendor responds shows up in Missed Cameras tagged `[MIXED]`
with both vendors' errors, rather than silently picking one.

This was validated with mocked responses covering "resolves to vendor A",
"falls back to vendor B after a non-auth failure on A", and "both vendors
cleanly reject credentials" -- not yet against a real mixed-vendor device, so
treat the first live run as a check, not a guarantee, and flag anything that
looks off (e.g. a port's data looking wrong for the vendor label it got).

## Step 2: Launch in a separate console window

Build the command from `scripts/run_audit.py` in this skill's folder:

```
python <skill_dir>/scripts/run_audit.py --ip <ip> --manufacturer <axis|hikvision|mixed> [--client "..."] [--location "..."] [--serial "..."] [--tag "<job name>"]
```

or, for CSV batch mode:

```
python <skill_dir>/scripts/run_audit.py --csv "<path/to/file.csv>" [--tag "<job name>"]
```

Launch it in a new window so the credential prompts are genuinely interactive.
On Windows, from PowerShell:

```powershell
Start-Process powershell -ArgumentList "-Command", "python '<skill_dir>/scripts/run_audit.py' --ip <ip> --manufacturer <axis|hikvision>"
```

Tell the user a window just opened and to enter their camera credentials
there when prompted -- Claude cannot see or fill that prompt.

## Step 3: Wait for the report, then summarize it

Reports land in `~/Downloads/Camera_Reports_Master/`, named
`<tag or CSV filename>_Master_<timestamp>.xlsx`. Since the console window's
output isn't visible to Claude, poll that folder for a new file matching the
expected `<base_filename>_Master_*.xlsx` pattern rather than trying to read
the window's stdout. A run over more than a handful of cameras can take
several minutes -- check back periodically rather than assuming failure if
the file isn't there after a few seconds.

Once the file appears, don't parse the whole workbook into the conversation
(reports embed a thumbnail image per rule and can be large). Instead:

1. Report the file path to the user.
2. Optionally open it with `openpyxl` (`load_workbook(path, read_only=True)`)
   just to count data rows in the "Missed Cameras" sheet (data starts at row
   3) and mention that count -- e.g. "report saved to X; 4 cameras had
   failures, see the Missed Cameras tab for details."

## If something looks wrong

Check `references/troubleshooting.md` before assuming it's a bug -- several
things that look like failures (widespread 401s, thermal cameras flagged as
special cases, disabled TLS verification) are documented, expected behavior
of this tool.
