import os
import csv
import getpass
from pathlib import Path
from datetime import datetime
import platform
import subprocess

# --- GUI & Environment Imports ---
import tkinter as tk
from tkinter import filedialog
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import camera_engine

# --- 1. SET UP INFRASTRUCTURE PATHING & CREDENTIALS ---
# No default usernames: "admin"/"root" are well-known factory defaults for
# these vendors, so credentials must come from .env or an interactive prompt.
USER_HIK = os.getenv("HIK_USER")
PASS_HIK = os.getenv("HIK_PASSWORD")
USER_AXIS = os.getenv("AXIS_USER")
PASS_AXIS = os.getenv("AXIS_PASSWORD")

output_dir = camera_engine.default_output_dir()

# --- 2. DYNAMIC MODE & SELECTION RUNTIME PROMPTS ---
print("=========================================")
print("  UNIFIED MASTER REPORT GENERATOR (PRO)  ")
print("=========================================")
print("1. Process a bulk batch via CSV file")
print("2. Test a single camera / IP address")
print("-----------------------------------------")
run_mode = input("Select an option (1 or 2) [Default 1]: ").strip()
while run_mode not in ("", "1", "2"):
    run_mode = input("[!] Invalid selection. Enter 1 or 2 [Default 1]: ").strip()
print(f"[+] Mode selected: {'Single Camera Test' if run_mode == '2' else 'CSV Bulk Batch'}")

camera_rows = []
base_filename = ""

if run_mode == "2":
    print("\n[ Single Camera Mode Selected ]")
    single_ip = input("Enter the Camera IP Address: ").strip()
    while not single_ip:
        single_ip = input("[!] IP Address cannot be blank. Enter Camera IP: ").strip()

    print("Select Camera Manufacturer:")
    print("1. Hikvision")
    print("2. Axis")
    print("3. LVT (Hikvision-compatible)")
    mfg_choice = input("Enter 1, 2, or 3 [Default 1]: ").strip()
    while mfg_choice not in ("", "1", "2", "3"):
        mfg_choice = input("[!] Invalid selection. Enter 1, 2, or 3 [Default 1]: ").strip()
    if mfg_choice == "2":
        single_mfg = "Axis"
    elif mfg_choice == "3":
        single_mfg = "LVT"
    else:
        single_mfg = "Hikvision"
    print(f"[+] Manufacturer selected: {single_mfg} (LVT is treated as Hikvision-compatible)")

    camera_rows.append({
        "CLIENT_NM": input("\nEnter Client Name (Optional): ").strip() or "Single Test",
        "LOCATION_NM": input("Enter Location Name (Optional): ").strip() or "Diagnostic",
        "LIVE_UNIT_SERIAL_NM": input("Enter Unit Serial (Optional): ").strip() or "N/A",
        "IP": single_ip,
        "MANUFACTURER": single_mfg
    })
    base_filename = f"Diagnostic_Test_{single_ip.replace('.', '_')}"
else:
    print("\n[ CSV Bulk Mode Selected ]")
    print("Select your input CSV file via the window popup...")

    root = tk.Tk()
    root.withdraw()
    root.attributes('-topmost', True)

    selected_file = filedialog.askopenfilename(
        initialdir=Path.home() / "Downloads",
        title="Select Camera Layout CSV File",
        filetypes=(("CSV Files", "*.csv"), ("All Files", "*.*"))
    )

    if selected_file:
        csv_input_path = Path(selected_file)
    else:
        print("[-] Window closed without selection. Falling back to manual name lookups.")
        csv_input_path = Path.home() / "Downloads" / (input("Enter the input CSV file name: ").strip() or "fullTesting.csv")

    if not csv_input_path.exists():
        print(f"[!] Error: Missing input file profile target: {csv_input_path}")
        exit()

    print(f"Reading target camera layouts from: {csv_input_path}...")
    with open(csv_input_path, mode='r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        camera_rows = list(reader)

    # INFRASTRUCTURE CHECK: fail fast on a malformed/empty CSV instead of
    # silently producing a report with zero rows.
    if not camera_rows:
        print(f"[!] Error: CSV file '{csv_input_path.name}' has no data rows. Exiting.")
        exit()
    if "IP" not in reader.fieldnames:
        print(f"[!] Error: CSV file '{csv_input_path.name}' is missing a required 'IP' column. "
              f"Found columns: {reader.fieldnames}")
        exit()
    rows_with_ip = sum(1 for r in camera_rows if r.get("IP", "").strip())
    if rows_with_ip == 0:
        print(f"[!] Error: CSV file '{csv_input_path.name}' has an 'IP' column but every value is blank. Exiting.")
        exit()
    if rows_with_ip < len(camera_rows):
        print(f"[!] Warning: {len(camera_rows) - rows_with_ip} of {len(camera_rows)} row(s) are missing an IP "
              f"and will be skipped during processing.")
    deduped_rows = camera_engine.dedupe_camera_rows(camera_rows)
    if len(deduped_rows) < len(camera_rows):
        print(f"[!] Note: Collapsed {len(camera_rows) - len(deduped_rows)} duplicate-IP row(s) into {len(deduped_rows)} unique device(s).")
    camera_rows = deduped_rows
    custom_tag = input("\nEnter an optional custom tag/job name (or press Enter to use CSV name): ").strip()
    base_filename = custom_tag if custom_tag else csv_input_path.stem

# --- 3. PRE-FLIGHT CREDENTIAL CHECK (Prompts JIT based on what's in the run payload) ---
mfg_classes = [camera_engine.classify_manufacturer(r.get("MANUFACTURER", "")) for r in camera_rows if r.get("IP", "").strip()]
unrecognized_mfg_rows = [r for r in camera_rows
                         if r.get("IP", "").strip() and camera_engine.classify_manufacturer(r.get("MANUFACTURER", "")) is None]
if unrecognized_mfg_rows:
    print(f"\n[!] Warning: {len(unrecognized_mfg_rows)} row(s) have a MANUFACTURER value that isn't recognized as "
          f"Axis or Hikvision and will be skipped (logged to Missed Cameras):")
    for r in unrecognized_mfg_rows:
        print(f"    -> IP {r.get('IP', '?')}: MANUFACTURER='{r.get('MANUFACTURER', '')}'")
    if len(unrecognized_mfg_rows) == len(camera_rows):
        print("[!] Every row failed to classify -- check that the CSV's MANUFACTURER column header "
              "is spelled/capitalized exactly as expected.")

needs_axis = "AXIS" in mfg_classes or "MIXED" in mfg_classes
needs_hik = "HIKVISION" in mfg_classes or "MIXED" in mfg_classes

# Every run (single-test or batch) always asks for credentials fresh instead of
# silently reusing whatever is cached in .env, so a stale/unexpected .env value
# never causes a manufacturer's prompt to be silently skipped.
if needs_axis:
    USER_AXIS = input("\n[!] Enter Axis camera username: ").strip()
    if not USER_AXIS:
        print("[-] Error: Axis username cannot be blank. Exiting.")
        exit()
    PASS_AXIS = getpass.getpass("[!] Enter Axis camera password: ").strip()
    if not PASS_AXIS:
        print("[-] Error: Axis password cannot be blank. Exiting.")
        exit()
if needs_hik:
    USER_HIK = input("\n[!] Enter Hikvision/LVT camera username: ").strip()
    if not USER_HIK:
        print("[-] Error: Hikvision/LVT username cannot be blank. Exiting.")
        exit()
    PASS_HIK = getpass.getpass("[!] Enter Hikvision/LVT camera password: ").strip()
    if not PASS_HIK:
        print("[-] Error: Hikvision/LVT password cannot be blank. Exiting.")
        exit()

credentials = {
    "AXIS_USER": USER_AXIS, "AXIS_PASS": PASS_AXIS,
    "HIK_USER": USER_HIK, "HIK_PASS": PASS_HIK,
}

# --- 4. RUN THE ENGINE ---
final_output_path = camera_engine.run_batch(
    camera_rows, credentials, output_dir, base_filename,
    log_cb=print,
)

try:
    print("[*] Opening results folder...")
    folder_to_open = final_output_path.parent
    if platform.system() == "Windows":
        os.startfile(folder_to_open)
    elif platform.system() == "Darwin":
        subprocess.Popen(["open", folder_to_open])
    else:
        subprocess.Popen(["xdg-open", folder_to_open])
except Exception:
    pass
