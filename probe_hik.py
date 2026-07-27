"""Step 0 (Hikvision) -- ISAPI analytics schema probe.

The Hikvision counterpart to probe_aoa.py. READ-ONLY: dumps a camera's VCA rule
config + capabilities so we template the write path (PUT) against ACTUAL firmware
XML instead of guessing. Hik ISAPI varies a lot across models/firmware, so this is
mandatory before writing any rule builder.

Run:
    python probe_hik.py --ip 10.23.27.20 --channel 1 --user admin --password ****
    python probe_hik.py --ip 10.23.27.20                # prompts for creds

Dumps into ./hik_probes/ (gitignored) for each endpoint that answers 200:
    <ip>_ch<N>_behaviorRule.xml           the per-channel rule document (read path uses this)
    <ip>_ch<N>_behaviorRule_caps.xml      supported rule types / limits
    <ip>_ch<N>_FieldDetection.xml         /ISAPI/Smart intrusion resource (if present)
    <ip>_ch<N>_LineDetection.xml          /ISAPI/Smart line-crossing resource (if present)

What to look at:
    * Which family the camera actually uses -- /ISAPI/Intelligent/.../behaviorRule
      vs the /ISAPI/Smart/* per-rule resources. That decides the write endpoint.
    * The exact element names/namespace, coordinate encoding (positionX/Y 0..1000),
      duration/target tags, and whether a single rule or the whole channel is the
      write unit. Compare against camera_engine.HikvisionHandler.parse_analytics.
"""

import argparse
import getpass
from pathlib import Path

import requests
from requests.auth import HTTPBasicAuth, HTTPDigestAuth

import camera_engine  # reuse STRICT_TIMEOUT + auth conventions

NET_TIMEOUT = getattr(camera_engine, "STRICT_TIMEOUT", (3.05, 5.0))


def _endpoints(ip, port, ch):
    base = f"http://{ip}:{port}"
    return [
        ("behaviorRule", f"{base}/ISAPI/Intelligent/channels/{ch}/behaviorRule"),
        ("behaviorRule_caps", f"{base}/ISAPI/Intelligent/channels/{ch}/behaviorRule/capabilities"),
        ("FieldDetection", f"{base}/ISAPI/Smart/FieldDetection/{ch}"),
        ("FieldDetection_caps", f"{base}/ISAPI/Smart/FieldDetection/{ch}/capabilities"),
        ("LineDetection", f"{base}/ISAPI/Smart/LineDetection/{ch}"),
        ("LineDetection_caps", f"{base}/ISAPI/Smart/LineDetection/{ch}/capabilities"),
    ]


import re

# The VCA detections this writer cares about, and the SmartCap flag that gates each.
WRITABLE_DETECTIONS = {
    "isSupportFieldDetection": "intrusion (FieldDetection)",
    "isSupportLineDetection": "line crossing (LineDetection)",
    "isSupportRegionEntrance": "region entrance",
    "isSupportRegionExiting": "region exiting",
    "isSupportLoitering": "loitering",
}


def _get(session, url, auth_strategies):
    """GET with digest->basic fallback. Returns (status, body) with body always set
    (ISAPI puts a useful subStatusCode in non-200 XML)."""
    status, body = None, None
    for auth in auth_strategies:
        try:
            r = session.get(url, auth=auth, timeout=NET_TIMEOUT, verify=False)
        except requests.exceptions.RequestException as e:
            status = f"ERR {type(e).__name__}"
            continue
        status, body = r.status_code, r.text
        if r.status_code != 401:
            break  # digest worked (or a definitive non-auth answer); don't retry
    return status, body


def _verdict(session, ip, port, auth_strategies, out_dir):
    """Screen the device: model, and whether it exposes configurable rules. The
    authoritative signal is the Intelligent/behaviorRule/1 endpoint per channel (the
    one the audit read path uses) -- Smart/capabilities describes only the newer
    /ISAPI/Smart/* family and reads false on cameras that use the older behaviorRule
    family, so it must NOT be the sole basis for a verdict."""
    base = f"http://{ip}:{port}"
    _, dev = _get(session, f"{base}/ISAPI/System/deviceInfo", auth_strategies)
    model = re.search(r"<model>([^<]+)", dev or "")
    name = re.search(r"<deviceName>([^<]+)", dev or "")
    print(f"    device: {name.group(1) if name else '?'} / model {model.group(1) if model else '?'}")

    # Real signal: behaviorRule rules per channel.
    found_any = False
    for ch in ("1", "2"):
        status, body = _get(session, f"{base}/ISAPI/Intelligent/channels/{ch}/behaviorRule/1", auth_strategies)
        if status == 200 and body and "RuleInfo" in body:
            found_any = True
            path = Path(out_dir) / f"{ip.replace('.', '_')}_ch{ch}_behaviorRule.xml"
            path.write_text(body, encoding="utf-8")
            rules = re.findall(r"<ruleName>([^<]*)</ruleName>", body)
            types = sorted(set(re.findall(r"<eventType>([^<]*)</eventType>", body)))
            print(f"    ch{ch} behaviorRule: {len(rules)} rule(s) {rules} types={types} -> {path.name}")
    if found_any:
        print("    WRITABLE VCA: yes (behaviorRule family) -- valid target.")
    else:
        # Fall back to SmartCap as a secondary hint.
        status, caps = _get(session, f"{base}/ISAPI/Smart/capabilities", auth_strategies)
        supported = [desc for flag, desc in WRITABLE_DETECTIONS.items()
                     if caps and re.search(rf"<{flag}>true</", caps)] if status == 200 else []
        print(f"    WRITABLE VCA: {'Smart family: ' + ', '.join(supported) if supported else 'NONE found via behaviorRule or Smart -- not a target.'}")
    return found_any


def probe(ip, port, ch, user, password, out_dir):
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    auth_strategies = [HTTPDigestAuth(user, password), HTTPBasicAuth(user, password)]
    session = requests.Session()

    print(f"[*] Probing Hikvision ISAPI at {ip}:{port} channel {ch}")
    _verdict(session, ip, port, auth_strategies, out_dir)

    any_ok = False
    for label, url in _endpoints(ip, port, ch):
        status, body = _get(session, url, auth_strategies)
        sub = re.search(r"<subStatusCode>([^<]+)", body or "")
        if status == 200 and body:
            any_ok = True
            path = Path(out_dir) / f"{ip.replace('.', '_')}_ch{ch}_{label}.xml"
            path.write_text(body, encoding="utf-8")
            print(f"    [200] {label:22} -> {path.name}")
        else:
            print(f"    [{status}] {label:22} {('(' + sub.group(1) + ')') if sub else ''}")
    return any_ok


def main():
    ap = argparse.ArgumentParser(description="Probe a Hikvision camera's VCA analytics schema (read-only).")
    ap.add_argument("--ip", required=True)
    ap.add_argument("--port", default="80", help="ISAPI port (LVT units may port-forward, e.g. 5010/5015/5020)")
    ap.add_argument("--channel", default="1", help="channel to probe (1 or 2; 201 is thermal snapshot only)")
    ap.add_argument("--user", default=None)
    ap.add_argument("--password", default=None)
    ap.add_argument("--out", default="hik_probes")
    args = ap.parse_args()

    user = args.user or input("Hikvision username: ").strip()
    password = args.password or getpass.getpass("Hikvision password: ")

    ok = probe(args.ip, args.port, args.channel, user, password, args.out)
    print("[+] Probe complete." if ok else "[!] No endpoint answered 200 -- check IP/port/channel/creds.")


if __name__ == "__main__":
    main()
