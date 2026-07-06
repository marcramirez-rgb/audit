#!/usr/bin/env python3
"""Debug runner: execute project scripts with enhanced logging for troubleshooting.

Usage examples:
  python debug_run.py --script run_audit --csv path/to/file.csv
  python debug_run.py --script run_audit --ip 1.2.3.4 --manufacturer lvt
  python debug_run.py --script combined
  python debug_run.py --script custom --path ./some_script.py --extra "--flag val"

This script runs the target script in a subprocess with `PYTHONFAULTHANDLER=1`,
captures stdout/stderr, and writes a timestamped log file under ./debug_logs/.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
import json
import os


ROOT = Path(__file__).resolve().parent
DEBUG_DIR = ROOT / "debug_logs"
DEBUG_DIR.mkdir(exist_ok=True)


def _script_path(name: str) -> Path:
    if name == "run_audit":
        # Prefer the packaged/dist copy, fall back to the skill script if not present
        dist_path = ROOT / "dist" / "camera-audit" / "scripts" / "run_audit.py"
        if dist_path.exists():
            return dist_path
        return ROOT / ".claude" / "skills" / "camera-audit" / "scripts" / "run_audit.py"
    if name == "combined":
        return ROOT / "combined.py"
    raise FileNotFoundError(f"Unknown script name: {name}")


def build_cmd_for_args(args: argparse.Namespace) -> (list[str], Path):
    if args.script == "custom":
        if not args.path:
            raise SystemExit("--path is required for --script custom")
        script = Path(args.path).expanduser()
        cmd = [sys.executable, str(script)]
        if args.extra:
            cmd.extend(args.extra.split())
        return cmd, script

    script = _script_path(args.script)
    cmd = [sys.executable, str(script)]
    if args.script == "run_audit":
        if args.csv:
            cmd.extend(["--csv", str(Path(args.csv).expanduser())])
        else:
            if not args.ip or not args.manufacturer:
                raise SystemExit("--ip and --manufacturer are required for single-run run_audit mode")
            cmd.extend(["--ip", args.ip, "--manufacturer", args.manufacturer])
            if args.client:
                cmd.extend(["--client", args.client])
            if args.location:
                cmd.extend(["--location", args.location])
            if args.serial:
                cmd.extend(["--serial", args.serial])
    return cmd, script


def run_and_log(cmd: list[str], cwd: Path, env_extra: dict | None = None, timeout: int | None = None) -> Path:
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    short = Path(cmd[1]).stem if len(cmd) > 1 else "cmd"
    log_path = DEBUG_DIR / f"debug_{short}_{ts}.log"

    env = os.environ.copy()
    # Enable faulthandler to get better tracebacks on interpreter crashes
    env["PYTHONFAULTHANDLER"] = "1"
    env["PYTHONWARNINGS"] = env.get("PYTHONWARNINGS", "default")
    if env_extra:
        env.update(env_extra)

    header = {
        "timestamp": ts,
        "cwd": str(cwd),
        "command": cmd,
        "env_snapshot": {k: env.get(k) for k in ("PYTHONPATH", "PYTHONFAULTHANDLER", "PYTHONWARNINGS")},
        "python": sys.executable,
    }

    with open(log_path, "w", encoding="utf-8") as fh:
        fh.write("# Debug run log\n")
        fh.write(json.dumps(header, indent=2))
        fh.write("\n\n--- STDOUT / STDERR ---\n\n")

        print(f"[debug] Running: {' '.join(cmd)}")
        try:
            proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
            fh.write("--- STDOUT ---\n")
            fh.write(proc.stdout or "(no stdout)\n")
            fh.write("\n--- STDERR ---\n")
            fh.write(proc.stderr or "(no stderr)\n")
            fh.write(f"\n--- RETURN CODE: {proc.returncode} ---\n")
        except subprocess.TimeoutExpired as e:
            fh.write("(PROCESS TIMED OUT)\n")
            fh.write(str(e))
            print("[debug] Process timed out; see log for details.")
        except Exception as e:
            fh.write("(PROCESS RAISED EXCEPTION)\n")
            fh.write(repr(e))
            print("[debug] Failed to start process; see log for details.")

    print(f"[debug] Log written to: {log_path}")
    return log_path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Run project scripts with enhanced debug logging.")
    p.add_argument("--script", choices=["run_audit", "combined", "custom"], required=True)
    # run_audit options
    p.add_argument("--csv", help="Path to CSV for run_audit mode")
    p.add_argument("--ip", help="IP for single-camera run_audit mode")
    p.add_argument("--manufacturer", help="Manufacturer for single-camera run_audit mode (axis,hikvision,lvt,mixed)")
    p.add_argument("--client")
    p.add_argument("--location")
    p.add_argument("--serial")
    # custom
    p.add_argument("--path", help="Path to custom script when --script custom")
    p.add_argument("--extra", help="Extra args string passed to custom script")
    p.add_argument("--timeout", type=int, help="Timeout seconds for the subprocess")
    # optional mock credentials to avoid interactive prompts during debugging
    p.add_argument("--huser", help="Hikvision username to set in subprocess env (for debugging)")
    p.add_argument("--hpass", help="Hikvision password to set in subprocess env (for debugging)")
    p.add_argument("--auser", help="Axis username to set in subprocess env (for debugging)")
    p.add_argument("--apass", help="Axis password to set in subprocess env (for debugging)")
    p.add_argument("--debug-overlay", action="store_true", help="Enable saving of overlay debug images (sets DEBUG_OVERLAY=1 in subprocess)")

    args = p.parse_args(argv)
    try:
        cmd, script_path = build_cmd_for_args(args)
    except Exception as e:
        print(f"Error building command: {e}")
        return 2

    env_extra = {}
    if args.debug_overlay:
        env_extra["DEBUG_OVERLAY"] = "1"
    if args.huser:
        env_extra["HIK_USER"] = args.huser
    if args.hpass:
        env_extra["HIK_PASSWORD"] = args.hpass
    if args.auser:
        env_extra["AXIS_USER"] = args.auser
    if args.apass:
        env_extra["AXIS_PASSWORD"] = args.apass

    return_code = 0
    log = run_and_log(cmd, cwd=ROOT, env_extra=env_extra, timeout=args.timeout)
    # Optionally, we could parse log to surface errors; for now just return 0
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
