# Troubleshooting a camera audit run

These are documented, expected behaviors of `camera_engine.py` -- check here
before treating something as a bug.

## Widespread 401 Unauthorized across many cameras

Most likely an **account lockout**, not a wrong password. These cameras lock
an account after repeated failed logins (Hikvision defaults to ~5 attempts).
If the user has confirmed they can log into one device's web UI manually with
the same credentials, this is almost certainly a lockout from an earlier bad
run -- the fix is waiting for the lockout window to clear, not retrying
immediately or trying more password variations (which extends the lockout).

## Every camera shows a gray/black thumbnail

The snapshot fetch is failing. Point the user at the "Missed Cameras" sheet
for the actual error (timeout, 401, connection refused, etc.) rather than
guessing from the main sheet alone.

## A camera hits the wrong API (Axis URL sent to a Hikvision unit, or vice versa)

Check the exact `MANUFACTURER` value in the CSV and the column header
spelling -- it must be exactly `MANUFACTURER`. "LVT"-branded units are
intentionally treated as Hikvision-compatible (same ISAPI endpoints, same
credentials) since they run Hikvision firmware under an OEM rebrand -- that's
correct, not a misclassification.

## TLS verification is disabled (`verify=False`)

Intentional. These cameras use self-signed certificates on a trusted internal
network/VPN. Don't suggest re-enabling it without also fixing certificate
management on the cameras themselves -- that's a larger, separate change.

## AXIS Perimeter Defender cameras flagged as `[SPECIAL CASE]`

Expected. Perimeter Defender (used on fixed thermal cameras instead of
standard AXIS Object Analytics) stores its configuration in an opaque,
undocumented binary format this tool can't parse. It's detected and flagged
in Missed Cameras rather than silently skipped or misreported -- this is
confirmed against a real device, not a guess.

## Hikvision camera model containing "2TD" flagged as thermal/specialty

This is a best-effort heuristic (`THERMAL_MODEL_MARKERS` in
`camera_engine.py`), not confirmed against a real failing device the way the
Axis case above is. If a real Hikvision thermal camera isn't flagged, or a
non-thermal camera is flagged incorrectly, that's the heuristic to adjust --
mention it to the user rather than silently working around it, since editing
`camera_engine.py` is out of scope for this skill.

## `ModuleNotFoundError` when running `run_audit.py`

Dependencies aren't installed in the Python environment being used to launch
the script. The fix is `pip install -r requirements.txt` from the project
root (`axis_api_testing/`), not a code change.
