"""Step 0 -- AOA schema probe.

Dumps a camera's real AXIS Object Analytics surface to JSON so we template the
write path (setConfiguration) against ACTUAL firmware output instead of a guessed
schema. This is READ-ONLY and safe to run against production cameras.

Run it against 2-3 cameras on different firmware before trusting any write:

    python probe_aoa.py --ip 10.23.66.205 --port 5015 --user root --password ****
    python probe_aoa.py --ip 10.23.66.205 --port 5015           # prompts for creds

Outputs (into ./aoa_probes/ by default):
    <ip>_<port>_supported_versions.json
    <ip>_<port>_capabilities.json      <- what scenario types / classes / filters exist
    <ip>_<port>_configuration.json     <- the exact object shape you mutate + push back

What to look at afterward:
    * capabilities -> which scenario `type` values and objectClassification types are
      allowed on THIS firmware (older units won't have 'fence' or 'vehicle').
    * configuration -> the real keys inside scenarios/triggers/filters. Compare against
      the PROVISIONAL builders in aoa_config.py and correct them.
"""

import argparse
import getpass
import json
from pathlib import Path

import aoa_config


def _dump(out_dir, ip, port, label, payload):
    path = Path(out_dir) / f"{ip.replace('.', '_')}_{port}_{label}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"    wrote {path}")
    return path


def probe(ip, port, user, password, out_dir, api_version):
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    client = aoa_config.AOAClient(ip, user, password, port, api_version=api_version)

    print(f"[*] Probing AOA at {client.control_url} (apiVersion {api_version})")
    steps = [
        ("supported_versions", client.get_supported_versions),
        ("capabilities", client.get_capabilities),
        ("configuration", client.get_config),
    ]
    ok = True
    for label, fn in steps:
        try:
            payload = fn()
            _dump(out_dir, ip, port, label, payload)
        except aoa_config.AOAAuthError as e:
            print(f"    [!] {label}: AUTH REJECTED -- {e}")
            ok = False
            break  # no point continuing if auth is the problem
        except aoa_config.AOAError as e:
            # Keep going: e.g. capabilities may need a newer apiVersion than config.
            print(f"    [!] {label}: {e}")
            ok = False
    return ok


def main():
    ap = argparse.ArgumentParser(description="Probe an Axis camera's Object Analytics schema (read-only).")
    ap.add_argument("--ip", required=True)
    ap.add_argument("--port", default="5015", help="LVT unit camera port (5010/5015/5020) or 80")
    ap.add_argument("--user", default=None)
    ap.add_argument("--password", default=None)
    ap.add_argument("--api-version", default=aoa_config.DEFAULT_API_VERSION)
    ap.add_argument("--out", default="aoa_probes")
    args = ap.parse_args()

    user = args.user or input("Axis username: ").strip()
    password = args.password or getpass.getpass("Axis password: ")

    ok = probe(args.ip, args.port, user, password, args.out, args.api_version)
    print("[+] Probe complete." if ok else "[!] Probe finished with errors (see above).")


if __name__ == "__main__":
    main()
