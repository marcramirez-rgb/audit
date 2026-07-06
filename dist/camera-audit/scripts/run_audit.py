"""Non-interactive-mode entry point into camera_engine.run_batch(), for use by the
camera-audit skill. Unlike combined.py, this takes its inputs as CLI arguments
instead of interactive menus/tkinter dialogs, so a caller can construct the exact
invocation ahead of time. Credential prompts (input()/getpass()) are intentionally
kept interactive, asked fresh every run, and only for whichever vendor(s) this
run's rows actually need -- same rationale as combined.py: never let a stale or
wrong cached password make a run silently look like it worked when it didn't.

This script is meant to be launched in its own real console window (see the
skill's SKILL.md for why) so those prompts reach an actual keyboard.
"""
import argparse
import csv
import getpass
import os
import platform
import subprocess
import sys
import traceback
from pathlib import Path

# camera_engine.py ships alongside this script inside the plugin package
# (camera-audit/scripts/camera_engine.py) so this skill has no dependency on
# an external project directory being present on the machine it runs on.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import camera_engine


def load_csv_rows(csv_path):
    with open(csv_path, mode="r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames or []

    if not rows:
        print(f"[!] Error: CSV file '{csv_path}' has no data rows.")
        sys.exit(1)
    if "IP" not in fieldnames:
        print(f"[!] Error: CSV file '{csv_path}' is missing a required 'IP' column. Found columns: {fieldnames}")
        sys.exit(1)

    rows_with_ip = sum(1 for r in rows if r.get("IP", "").strip())
    if rows_with_ip == 0:
        print(f"[!] Error: CSV file '{csv_path}' has an 'IP' column but every value is blank.")
        sys.exit(1)
    if rows_with_ip < len(rows):
        skipped = [r for r in rows if not r.get("IP", "").strip()]
        print(f"[!] Warning: {len(skipped)} of {len(rows)} row(s) are missing an IP and will be automatically skipped:")
        for r in skipped:
            label = r.get("LIVE_UNIT_SERIAL_NM") or r.get("LOCATION_NM") or r.get("CLIENT_NM") or "(no identifying columns)"
            print(f"    -> {label}")

    return dedupe_by_ip(rows)


def dedupe_by_ip(rows):
    """The documented Snowflake export (references/snowflake_query.md) joins in
    MODEL, so a unit with more than one camera model on record comes back as
    multiple rows sharing the same IP -- one per manufacturer/model combination,
    not one per physical unit. camera_engine.py tests an IP as a single 3-port
    unit, so running each raw row separately would dial the same device multiple
    times in one batch: wasted time, and real risk of tripping its account
    lockout. Collapse each IP to one row, using MANUFACTURER='mixed' when its
    rows span more than one recognized vendor."""
    grouped = {}
    order = []
    passthrough = []  # blank-IP rows: harmless no-ops downstream, left as-is
    for row in rows:
        ip = row.get("IP", "").strip()
        if not ip:
            passthrough.append(row)
            continue
        if ip not in grouped:
            grouped[ip] = []
            order.append(ip)
        grouped[ip].append(row)

    deduped = []
    collapsed_ips = []
    for ip in order:
        group = grouped[ip]
        if len(group) == 1:
            deduped.append(group[0])
            continue

        collapsed_ips.append(ip)
        classes = {camera_engine.classify_manufacturer(r.get("MANUFACTURER", "")) for r in group}
        classes.discard(None)
        base = dict(group[0])
        if len(classes) > 1:
            base["MANUFACTURER"] = "mixed"
        deduped.append(base)

    if collapsed_ips:
        print(f"[!] Note: {len(collapsed_ips)} IP(s) appeared as multiple CSV rows (one per camera "
              f"model) and were collapsed to a single test per IP. IPs spanning more than one "
              f"recognized vendor were marked 'mixed':")
        for ip in collapsed_ips:
            print(f"    -> {ip}")

    return deduped + passthrough


def build_single_row(args):
    # Normalize known vendor aliases: treat 'lvt' as Hikvision-compatible
    mfg = (args.manufacturer or "").strip().lower()
    if mfg == "lvt":
        mfg = "hikvision"
    return [{
        "CLIENT_NM": args.client or "Single Test",
        "LOCATION_NM": args.location or "Diagnostic",
        "LIVE_UNIT_SERIAL_NM": args.serial or "N/A",
        "IP": args.ip,
        "MANUFACTURER": mfg,
    }]


def prompt_credentials(needs_axis, needs_hik):
    credentials = {"AXIS_USER": None, "AXIS_PASS": None, "HIK_USER": None, "HIK_PASS": None}

    # Allow non-interactive debugging via environment variables
    if needs_axis:
        credentials["AXIS_USER"] = os.getenv("AXIS_USER") or input("Enter Axis camera username: ").strip()
        credentials["AXIS_PASS"] = os.getenv("AXIS_PASSWORD") or getpass.getpass("Enter Axis camera password: ").strip()
        if not credentials["AXIS_USER"] or not credentials["AXIS_PASS"]:
            print("[-] Error: Axis username/password cannot be blank.")
            sys.exit(1)

    if needs_hik:
        credentials["HIK_USER"] = os.getenv("HIK_USER") or input("Enter Hikvision camera username: ").strip()
        credentials["HIK_PASS"] = os.getenv("HIK_PASSWORD") or getpass.getpass("Enter Hikvision camera password: ").strip()
        if not credentials["HIK_USER"] or not credentials["HIK_PASS"]:
            print("[-] Error: Hikvision username/password cannot be blank.")
            sys.exit(1)

    return credentials


def open_folder(folder):
    """Best-effort open of the report's containing folder, mirroring combined.py."""
    try:
        if platform.system() == "Windows":
            os.startfile(folder)
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", folder])
        else:
            subprocess.Popen(["xdg-open", folder])
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(
        description="Run a camera analytics audit (Axis/Hikvision) and produce an Excel report. "
                     "Prompts for camera credentials interactively; never pass them as arguments."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--csv", help="Path to a CSV of cameras (columns: IP, MANUFACTURER, CLIENT_NM, LOCATION_NM, LIVE_UNIT_SERIAL_NM).")
    mode.add_argument("--ip", help="Single camera IP to test.")
    parser.add_argument("--manufacturer", choices=["axis", "hikvision", "lvt", "mixed"],
                        help="Required with --ip. Use 'mixed' when the unit's three camera "
                             "positions aren't all the same vendor -- this asks for both "
                             "credential sets and figures out each port's vendor individually. "
                             "Note: 'lvt' is treated as Hikvision-compatible.")
    parser.add_argument("--client", help="Client name (single-camera mode only).")
    parser.add_argument("--location", help="Location name (single-camera mode only).")
    parser.add_argument("--serial", help="Unit serial (single-camera mode only).")
    parser.add_argument("--tag", help="Custom tag/job name used in the output filename.")
    args = parser.parse_args()

    if args.ip and not args.manufacturer:
        parser.error("--manufacturer is required with --ip")

    try:
        _run(args)
    except SystemExit as e:
        if e.code not in (0, None):
            input("\nPress Enter to close this window...")
        raise
    except Exception:
        traceback.print_exc()
        input("\nPress Enter to close this window...")
        sys.exit(1)


def _run(args):
    if args.csv:
        csv_path = Path(args.csv).expanduser()
        if not csv_path.exists():
            print(f"[!] Error: CSV file not found: {csv_path}")
            sys.exit(1)
        camera_rows = load_csv_rows(csv_path)
        base_filename = args.tag or csv_path.stem
    else:
        camera_rows = build_single_row(args)
        base_filename = args.tag or f"Diagnostic_Test_{args.ip.replace('.', '_')}"

    mfg_classes = [camera_engine.classify_manufacturer(r.get("MANUFACTURER", ""))
                   for r in camera_rows if r.get("IP", "").strip()]
    unrecognized_count = sum(1 for c in mfg_classes if c is None)
    if unrecognized_count:
        print(f"[!] Warning: {unrecognized_count} row(s) have an unrecognized MANUFACTURER value "
              f"and will be flagged in the Missed Cameras sheet rather than guessed.")

    has_mixed = "MIXED" in mfg_classes
    needs_axis = "AXIS" in mfg_classes or has_mixed
    needs_hik = "HIKVISION" in mfg_classes or has_mixed
    if not needs_axis and not needs_hik:
        print("[!] Error: no row classified as Axis, Hikvision, or Mixed -- nothing to run. "
              "Check the MANUFACTURER column values and header spelling.")
        sys.exit(1)

    print("=========================================")
    print("  CAMERA AUDIT - CREDENTIAL PROMPT")
    print("=========================================")
    credentials = prompt_credentials(needs_axis, needs_hik)

    output_dir = camera_engine.default_output_dir()
    final_path = camera_engine.run_batch(camera_rows, credentials, output_dir, base_filename, log_cb=print)

    print(f"\n[DONE] Report written to: {final_path}")
    print("[*] Opening results folder...")
    open_folder(final_path.parent)


if __name__ == "__main__":
    main()
