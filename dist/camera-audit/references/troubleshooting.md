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
If several audits were run back-to-back against overlapping cameras (e.g.
retrying a unit that just failed), treat the repeated 401s as one lockout
incident, not N separate broken cameras -- re-running immediately only makes
the lockout window longer.

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
the script. The fix is `pip install -r requirements.txt` run from inside
*this skill's folder* (`camera-audit/`, where `requirements.txt` ships), not
a code change. See the Prerequisites section in `SKILL.md` -- this is a
one-time per-machine setup step, most likely to bite the first time this
skill runs on a new coworker's machine.

## `run_audit.py: No such file or directory`, or the console window never opens at all

This means the skill package itself is incomplete on this machine -- the
`scripts/run_audit.py` (and/or `scripts/camera_engine.py`) files that ship
with this skill aren't present at the expected path. This is a packaging/
distribution problem, not something to work around by guessing at a
different path or trying to reconstruct the script inline. Tell the user
plainly that their local copy of the skill is missing files and needs to be
reinstalled/updated from the source package, rather than reporting a vague
"audit failed."

## PowerShell doesn't open, or opens and closes immediately

Two different failure modes look similar here -- don't conflate them:

- **The `Start-Process` call itself errors** (e.g. execution policy blocking
  script launches, `powershell`/`python` not resolvable on PATH from a fresh
  shell). This surfaces as an error from the tool that ran `Start-Process`,
  before any camera window exists. Report that exact error to the user
  rather than assuming a credentials issue.
- **The window opens and closes instantly.** This usually means `python`
  itself failed fast -- most commonly the `ModuleNotFoundError` case above,
  or the missing-file case above. Since `run_audit.py` normally pauses on
  "Press Enter to close this window..." after any exception, a window that
  disappears without that pause suggests `python` isn't launching at all
  (wrong/missing interpreter on PATH), not that the script ran and failed.
