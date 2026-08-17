"""AXIS Perimeter Defender recon + config backup (fixed thermal units).

The Perimeter Defender sibling of probe_aoa.py. Use it on an Axis camera whose
Object Analytics endpoint 404s -- typically a fixed THERMAL unit such as the
AXIS Q1971-E, which runs Perimeter Defender instead.

READ-ONLY by default and safe against production cameras:

    python probe_pd.py --ip 10.23.34.243 --port 5015
    python probe_pd.py --ip 10.23.34.243 --port 5015 --backup

--backup additionally downloads context.knp (the camera's analytics configuration)
into ./pd_backups/. That blob is ENCRYPTED, so it cannot be inspected or edited --
but it is restorable byte-for-byte to the SAME camera, which makes it the only
rollback that exists for a Perimeter Defender unit. Take one before letting anyone
reconfigure a thermal with AXIS Perimeter Defender Setup.

Restoring is deliberately NOT exposed as a flag here: it replaces live analytics
config and restarts the ACAP. Use pd_config.PDClient.restore_context() knowingly.
"""

import argparse
import getpass
from pathlib import Path

import pd_config

DEFAULT_BACKUP_DIR = "pd_backups"


def probe(ip, port, user, password, backup_dir=None):
    client = pd_config.PDClient(ip, user, password, port)
    print(f"[*] Perimeter Defender at {client.base}{pd_config.API}")

    running = pd_config.detect(client.session, ip, port, client.auth_strategies)
    if running is None:
        print("    [!] Could not read the application list (auth or network).")
    else:
        print(f"    running apps: {', '.join(running) or '(none)'}")
        if not pd_config.is_perimeter_defender(running):
            print("    [!] Perimeter Defender is NOT running here -- this probe "
                  "expects a fixed thermal. Try probe_aoa.py instead.")

    for label, fn in [("version", client.about), ("status", client.application_status)]:
        try:
            print(f"    {label}: {fn()}")
        except pd_config.PDError as e:
            print(f"    [!] {label}: {e}")

    try:
        zones = client.get_zones()
    except pd_config.PDError as e:
        print(f"    [!] zones: {e}")
        zones = []

    try:
        scenarios = pd_config.parse_scenarios_xml(client.get_scenarios_xml())
    except pd_config.PDError as e:
        print(f"    [!] scenarios.xml: {e}")
        scenarios = []

    print(f"\n[*] {len(scenarios)} scenario(s), {len(zones)} zone(s):")
    for rule in pd_config.merge(zones, scenarios):
        dur = f", dwell {rule['duration']}s" if rule["duration"] else ""
        print(f"    '{rule['name']}'  type={rule['pd_type']}{dur}, "
              f"{len(rule['points'])} vertices")
        preview = ", ".join(f"({x:.3f},{y:.3f})" for x, y in rule["points"][:4])
        print(f"        {preview}{' ...' if len(rule['points']) > 4 else ''}")
    if not zones:
        print("    (no alarm zones configured)")

    # The tuning params are the WRITABLE half of a fixed thermal's analytics --
    # notably the AI classifier, which is what makes the camera classify
    # human/vehicle rather than just detect motion. It drifts per camera.
    try:
        c = client.classification()
        print("\n[*] Detection tuning (writable over VAPIX -- no Axis UI needed):")
        state = "ON" if c["classifier_enabled"] else "OFF  <-- NOT CLASSIFYING"
        print(f"    AI classifier (human/vehicle) : {state}")
        print(f"    DNN sensitivity               : {c['dnn_sensitivity']}")
        print(f"    detection sensitivity         : {c['sensitivity']}")
        print(f"    long-range mode               : {c['long_range_mode']}")
        print(f"    out-of-field filter           : {c['oof_filter']}")
        print(f"    min object (w%,h%)            : {c['min_object']}")
        print(f"    max object (w%,h%)            : {c['max_object']}")
    except pd_config.PDError as e:
        print(f"    [!] tuning params: {e}")

    print("\n[*] Files the camera will hand over:")
    for token, filename in sorted(pd_config.FILE_TOKENS.items()):
        try:
            name, modified, size = client.file_info(token)
            print(f"    {token:<12} -> {name:<16} {size:>10,} bytes   modified {modified}")
        except pd_config.PDError as e:
            print(f"    {token:<12} -> [!] {e}")

    if backup_dir:
        path, size = client.backup_context(backup_dir)
        print(f"\n[+] Backed up analytics config: {path} ({size:,} bytes)")
        print("    Encrypted blob -- restorable to THIS camera only, not editable.")
    return True


def main():
    ap = argparse.ArgumentParser(
        description="Probe an Axis fixed thermal's Perimeter Defender config (read-only).")
    ap.add_argument("--ip", required=True)
    ap.add_argument("--port", default="5015", help="LVT unit camera port (5010/5015/5020) or 80")
    ap.add_argument("--user", default=None)
    ap.add_argument("--password", default=None)
    ap.add_argument("--backup", action="store_true",
                    help=f"also download context.knp into ./{DEFAULT_BACKUP_DIR}/")
    ap.add_argument("--out", default=DEFAULT_BACKUP_DIR)
    args = ap.parse_args()

    user = args.user or input("Axis username: ").strip()
    password = args.password or getpass.getpass("Axis password: ")

    try:
        probe(args.ip, args.port, user, password,
              backup_dir=Path(args.out) if args.backup else None)
    except pd_config.PDAuthError as e:
        print(f"[!] AUTH REJECTED -- {e}")
        return 1
    except pd_config.PDError as e:
        print(f"[!] {e}")
        return 1
    print("\n[+] Probe complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
