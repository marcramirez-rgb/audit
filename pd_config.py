"""AXIS Perimeter Defender (PD) access layer -- the third vendor path, alongside
aoa_config.py (Axis Object Analytics) and hik_config.py (Hikvision ISAPI).

WHY THIS MODULE EXISTS
----------------------
Axis FIXED THERMAL units (e.g. AXIS Q1971-E) do not run Object Analytics at all.
`/local/objectanalytics/control.cgi` 404s on them; the analytics engine is AXIS
Perimeter Defender, a separately licensed ACAP with a completely different API.
The audit engine already knew how to read one PD metadata frame; the writer knew
nothing about PD and simply failed to connect. This module is the shared,
testable home for "talk to Perimeter Defender".

WHAT PD ACTUALLY EXPOSES (complete -- not a guess)
--------------------------------------------------
The ACAP logs its own route table at startup, and the app's log is readable at
/v2/log. Captured live from 10.23.34.243:5015 (PD 3.7.0.7430, firmware 11.11.116),
these are ALL the endpoints it registers:

    get:    /local/AXISPerimeterDefender/v2/info/about
    get:    /local/AXISPerimeterDefender/v2/info/applicationStatus
    get:    /local/AXISPerimeterDefender/v2/info/numberOfCores
    get:    /local/AXISPerimeterDefender/v2/metadata/liveStream
    get:    /local/AXISPerimeterDefender/v2/files/([\\w-]*)
    get:    /local/AXISPerimeterDefender/v2/files/([\\w-]*)/info
    post:   /local/AXISPerimeterDefender/v2/files/([\\w-]*)
    delete: /local/AXISPerimeterDefender/v2/files/([\\w-]*)
    get:    /local/AXISPerimeterDefender/v2/log

plus static files from the app's html dir (log.html, scenarios.xml).

THERE IS NO ZONE/SCENARIO CONFIGURATION ENDPOINT. That is the whole reason the
writer cannot push a detection zone to a fixed thermal, and it is a property of
the vendor's app, not a gap in this code:

  * Zone geometry lives ONLY inside `context.knp`, reachable as the file token
    `contextFile`. A real AXIS Perimeter Defender Setup session on this camera is
    in the log doing exactly GET .../files/contextFile/info -> GET
    .../files/contextFile -> POST .../files/contextFile, after which the ACAP
    restarts itself to reload config.
  * `context.knp` is ENCRYPTED. Downloaded and measured: 175364 bytes of base64
    (72-char CRLF lines) decoding to 127968 bytes at 7.9987 bits/byte entropy,
    all 256 byte values present, no compression magic, and none of the known zone
    coordinates appear as int16/int32/float/double. It is ciphertext, so a new
    one cannot be authored without Axis's key.

So the honest capability split, which vendor_adapter advertises to the GUI:
    READ  zones + scenarios  -> yes (this module)
    BACK UP the config blob  -> yes (opaque but restorable byte-for-byte)
    WRITE a zone             -> no API exists; use AXIS Perimeter Defender Setup

The file-token allow-list is fixed and was recovered by brute force against the
403 ("not allowed") oracle -- the names are NOT the filenames, and the route regex
([\\w-]*) rejects dots, so "context.knp" 404s while "contextFile" works.
"""

import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import requests
from requests.auth import HTTPBasicAuth, HTTPDigestAuth

import camera_engine

APP_PATH = "/local/AXISPerimeterDefender"
API = f"{APP_PATH}/v2"
NET_TIMEOUT = getattr(camera_engine, "STRICT_TIMEOUT", (3.05, 5.0))

#: Tokens the ACAP's CHttpFileManager accepts, mapped to the file each returns.
#: Anything else answers 403 "not allowed". Confirmed live on PD 3.7.0.7430.
FILE_TOKENS = {
    "contextFile": "context.knp",     # the (encrypted) analytics configuration
    "supportFile": "supportLog.log",
    "logFile": "APD-log.txt",
    "licenseFile": "lic.xml",
}

#: Fallback reference frame if a metadata frame omits OSD_SIZE. PD reports zone
#: points as pixels in this space, not as normalized coordinates.
DEFAULT_OSD_SIZE = (384, 288)

#: VAPIX parameter group holding PD's tuning settings.
PARAM_GROUP = "AXISPerimeterDefender"

#: Tuning parameters this module will write, mapped to their allowed values --
#: None means "numeric, let the camera decide" (set_params verifies by read-back
#: rather than trusting a guessed range, since param.cgi answers 'OK' even for
#: values the application later clamps).
#:
#: UseDNNClassifier is the one that matters most: it is what makes a fixed
#: thermal CLASSIFY (human vs vehicle) instead of merely detecting motion.
#: Observed live at both settings on two identical Q1971-E units on one LVT unit.
PD_TUNING_PARAMS = {
    "UseDNNClassifier": ("yes", "no"),
    "DNNSensitivityLevel": None,
    "SensitivityLevel": None,
    "MinObjectWidthPercentage": None,
    "MinObjectHeightPercentage": None,
    "MaxObjectWidthPercentage": None,
    "MaxObjectHeightPercentage": None,
    "UseOOFFilter": ("yes", "no"),
    "OOFFilterValue": None,
    "LongRangeMode": ("yes", "no"),
    "ContrastEnhancement": ("yes", "no"),
    "PostAlarmTimeInSec": None,
    "ShowScenarioZoneOverlays": ("yes", "no"),
    "ShowAlarmStatusOverlay": ("yes", "no"),
}

#: Never read the multipart metadata stream forever waiting for a frame.
_STREAM_CAP_BYTES = 131072

#: PD scenario type -> the writer's vendor-neutral kind. PD's own vocabulary is
#: richer than the three kinds the GUI draws, so anything unmapped is surfaced as
#: an area rather than silently dropped.
PD_TYPE_TO_KIND = {
    "intrusion": "intrusion",
    "loitering": "loiter",
    "zone-crossing": "intrusion",
    "zonecrossing": "intrusion",
    "conditional": "intrusion",
}


class PDError(RuntimeError):
    """Perimeter Defender answered, but not with what we asked for."""


class PDAuthError(PDError):
    """Every credential was rejected (401)."""


class PDClient:
    """One camera + one port. Read-oriented: the only write verb is
    restore_context(), which pushes back a blob we previously downloaded."""

    def __init__(self, ip, user, password, port, use_https=False, session=None):
        self.ip = ip
        self.port = str(port)
        self.user = user
        self.scheme = "https" if use_https else "http"
        # Same order the audit engine uses: digest first, basic fallback.
        self.auth_strategies = [HTTPDigestAuth(user, password), HTTPBasicAuth(user, password)]
        self.session = session or requests.Session()
        # Reuse the audit handler purely for its snapshot logic -- one source of truth.
        self._axis = camera_engine.AxisHandler(ip, user, password)

    @property
    def base(self):
        return f"{self.scheme}://{self.ip}:{self.port}"

    # ------------------------------------------------------------------ transport

    def _get(self, path, stream=False):
        """GET with the digest->basic fallback. Returns the Response.
        Raises PDAuthError when every strategy saw 401."""
        saw_401 = False
        last_err = "all authentication attempts failed"
        for auth in self.auth_strategies:
            try:
                resp = self.session.get(self.base + path, auth=auth, timeout=NET_TIMEOUT,
                                        stream=stream, verify=False)
            except requests.exceptions.RequestException as e:
                last_err = f"{type(e).__name__}: {e}"
                continue
            if resp.status_code == 401:
                saw_401 = True
                continue
            return resp
        if saw_401:
            raise PDAuthError(f"Perimeter Defender rejected every credential (401) at {path}")
        raise PDError(f"{path}: {last_err}")

    # ------------------------------------------------------------------ info

    def about(self):
        """Version banner, e.g. 'APD 3.7.0.7430 built on Mar  6 2025, 10:49:16'."""
        r = self._get(f"{API}/info/about")
        if r.status_code != 200:
            raise PDError(f"info/about returned HTTP {r.status_code}")
        return r.text.strip()

    def application_status(self):
        """'RUNNING' when the analytics engine is actually up. A stopped PD still
        serves this endpoint, which is exactly how we tell 'no zones configured'
        apart from 'the app isn't running'."""
        r = self._get(f"{API}/info/applicationStatus")
        if r.status_code != 200:
            raise PDError(f"info/applicationStatus returned HTTP {r.status_code}")
        return r.text.strip()

    def fetch_snapshot(self, channel_idx=None):
        """PIL image for the drawing surface -- plain VAPIX, nothing PD-specific."""
        img, url, err, auth_rejected = self._axis.fetch_snapshot(
            self.session, self.port, channel_idx=channel_idx)
        if img is None:
            if auth_rejected:
                raise PDAuthError(f"Snapshot auth rejected at {url}")
            raise PDError(f"Snapshot failed at {url}: {err}")
        return img

    # ------------------------------------------------------------------ zones

    def fetch_metadata_frame(self):
        """One complete <NODE> frame from the multipart metadata stream.

        The endpoint is multipart/x-mixed-replace emitting ~9 frames/second; we
        read only until the first complete frame, then drop the connection."""
        resp = self._get(f"{API}/metadata/liveStream", stream=True)
        with resp:
            if resp.status_code != 200:
                raise PDError(f"metadata/liveStream returned HTTP {resp.status_code}")
            buf = ""
            for chunk in resp.iter_content(chunk_size=2048):
                if not chunk:
                    continue
                buf += chunk.decode("utf-8", errors="replace")
                end = buf.find("</NODE>")
                if end != -1:
                    start = buf.find("<NODE")
                    if start != -1:
                        return buf[start:end + len("</NODE>")]
                if len(buf) > _STREAM_CAP_BYTES:
                    break
        raise PDError("metadata stream produced no complete zone frame")

    def get_zones(self):
        """Configured alarm zones as vendor-neutral fractions. See parse_zones()."""
        return parse_zones(self.fetch_metadata_frame())

    def get_scenarios_xml(self):
        """The scenario definitions PD exports to its own html dir on every config
        load. Carries what the metadata frame does NOT: scenario name, type and
        per-zone dwell time. Returns '' when the app has never been configured."""
        r = self._get(f"{APP_PATH}/scenarios.xml")
        if r.status_code == 404:
            return ""
        if r.status_code != 200:
            raise PDError(f"scenarios.xml returned HTTP {r.status_code}")
        return r.text

    # ------------------------------------------------------------------ tuning params

    def get_params(self):
        """All root.AXISPerimeterDefender.* parameters as a {short_name: value} dict.

        PD's ZONES are locked inside the encrypted context.knp, but its DETECTION
        BEHAVIOUR -- including the AI classifier that produces human/vehicle
        classification -- lives in the ordinary VAPIX parameter tree, which is
        both readable and writable. This is the part of a thermal's analytics that
        can be managed without the Axis UI."""
        r = self._get(f"/axis-cgi/param.cgi?action=list&group={PARAM_GROUP}")
        if r.status_code != 200:
            raise PDError(f"param list returned HTTP {r.status_code}")
        out = {}
        for line in r.text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            prefix = f"root.{PARAM_GROUP}."
            if key.startswith(prefix):
                out[key[len(prefix):]] = value.strip()
        if not out:
            raise PDError("no Perimeter Defender parameters returned")
        return out

    def set_params(self, updates):
        """Write one or more PD tuning parameters. Returns {name: new_value}.

        Every write is VERIFIED BY READ-BACK rather than trusting the response:
        param.cgi answers a plain 'OK' and will happily accept a value the
        application then clamps or ignores, so 'OK' alone is not evidence the
        setting took. Raises PDError naming any parameter that did not stick."""
        if not updates:
            return {}
        for name, value in updates.items():
            if name not in PD_TUNING_PARAMS:
                raise ValueError(
                    f"unknown Perimeter Defender parameter {name!r} -- known tuning "
                    f"parameters: {sorted(PD_TUNING_PARAMS)}")
            allowed = PD_TUNING_PARAMS[name]
            if allowed is not None and str(value) not in allowed:
                raise ValueError(f"{name} must be one of {allowed}, got {value!r}")

        query = "&".join(f"root.{PARAM_GROUP}.{n}={v}" for n, v in updates.items())
        r = self._get(f"/axis-cgi/param.cgi?action=update&{query}")
        if r.status_code != 200:
            raise PDError(f"param update returned HTTP {r.status_code}: {r.text[:200]}")
        if "OK" not in r.text.upper():
            raise PDError(f"param update refused: {r.text.strip()[:200]}")

        after = self.get_params()
        stuck, missed = {}, []
        for name, value in updates.items():
            actual = after.get(name)
            if actual is None or actual.lower() != str(value).lower():
                missed.append(f"{name}: asked {value!r}, camera reports {actual!r}")
            else:
                stuck[name] = actual
        if missed:
            raise PDError("the camera did not accept: " + "; ".join(missed))
        return stuck

    def classification(self):
        """Summary of whether this thermal is actually classifying objects.

        The AI classifier is what turns PD's raw detections into human/vehicle
        classifications. It is per-camera and drifts: two identical Q1971-E units
        on the SAME LVT unit were found with it on and off (10.23.34.243 :5020 on,
        :5015 off), which is invisible unless something reads it."""
        params = self.get_params()
        enabled = params.get("UseDNNClassifier", "").lower() == "yes"
        return {
            "classifier_enabled": enabled,
            "dnn_sensitivity": params.get("DNNSensitivityLevel"),
            "sensitivity": params.get("SensitivityLevel"),
            "long_range_mode": params.get("LongRangeMode"),
            "oof_filter": params.get("UseOOFFilter"),
            "min_object": (params.get("MinObjectWidthPercentage"),
                           params.get("MinObjectHeightPercentage")),
            "max_object": (params.get("MaxObjectWidthPercentage"),
                           params.get("MaxObjectHeightPercentage")),
        }

    def enable_classifier(self, enabled=True, sensitivity=None):
        """Turn PD's human/vehicle AI classifier on or off.

        This is the single highest-value writable setting on a fixed thermal: with
        it off the camera still alarms, but without classifying what it saw."""
        updates = {"UseDNNClassifier": "yes" if enabled else "no"}
        if sensitivity is not None:
            updates["DNNSensitivityLevel"] = str(int(sensitivity))
        return self.set_params(updates)

    # ------------------------------------------------------------------ files

    def file_info(self, token):
        """(filename, modified, size_bytes) for an allow-listed file token."""
        _require_token(token)
        r = self._get(f"{API}/files/{token}/info")
        if r.status_code == 403:
            raise PDError(f"file token {token!r} refused by the camera (403)")
        if r.status_code != 200:
            raise PDError(f"files/{token}/info returned HTTP {r.status_code}")
        parts = [p.strip() for p in r.text.strip().split(",")]
        if len(parts) != 3:
            raise PDError(f"unexpected file info payload: {r.text.strip()!r}")
        name, modified, size = parts
        return name, modified, int(size)

    def download_file(self, token):
        """Raw bytes of an allow-listed file."""
        _require_token(token)
        r = self._get(f"{API}/files/{token}")
        if r.status_code == 403:
            raise PDError(f"file token {token!r} refused by the camera (403)")
        if r.status_code != 200:
            raise PDError(f"files/{token} returned HTTP {r.status_code}")
        return r.content

    def backup_context(self, backup_dir):
        """Save this camera's analytics configuration blob to a timestamped file.

        This is the ONLY capture of a PD camera's zone setup that can be put back:
        the payload is encrypted, so it is opaque to us, but it is restorable
        byte-for-byte. Returns (path, size_bytes)."""
        backup_dir = Path(backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        blob = self.download_file("contextFile")
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        path = backup_dir / f"pd_context_{self.ip.replace('.', '_')}_{self.port}_{stamp}.knp"
        path.write_bytes(blob)
        return path, len(blob)

    def restore_context(self, blob_path):
        """Push a previously downloaded context.knp back onto the camera.

        DESTRUCTIVE AND NOT UNDOABLE FROM HERE: this replaces the live analytics
        configuration and the ACAP restarts itself to reload it (observed in the
        app log as 'Signal 15 received. Exiting.' followed by a fresh START LOG).
        Only ever restore a blob taken from THIS camera -- context.knp carries the
        scene calibration, so another camera's file describes another scene.

        Callers must confirm with the operator first; nothing here asks."""
        blob_path = Path(blob_path)
        blob = blob_path.read_bytes()
        if not blob:
            raise PDError(f"{blob_path.name} is empty -- refusing to push it")
        url = f"{self.base}{API}/files/contextFile"
        files = {"file": ("context.knp", blob, "application/octet-stream")}
        saw_401 = False
        last_err = "all authentication attempts failed"
        for auth in self.auth_strategies:
            try:
                resp = self.session.post(url, auth=auth, files=files,
                                         timeout=(NET_TIMEOUT[0], 60), verify=False)
            except requests.exceptions.RequestException as e:
                last_err = f"{type(e).__name__}: {e}"
                continue
            if resp.status_code == 401:
                saw_401 = True
                continue
            if resp.status_code in (200, 201, 204):
                return len(blob)
            raise PDError(f"restore returned HTTP {resp.status_code}: {resp.text[:200]}")
        if saw_401:
            raise PDAuthError("Perimeter Defender rejected every credential (401) on restore")
        raise PDError(last_err)


# ---------------------------------------------------------------- parsing

def _require_token(token):
    if token not in FILE_TOKENS:
        raise ValueError(f"unknown file token {token!r} -- allowed: {sorted(FILE_TOKENS)}")


def osd_size(xml_frame_root):
    """Reference frame the zone pixels are expressed in. PD reports it on the root
    <NODE OSD_SIZE="384x288">; a malformed or missing value falls back rather than
    dividing by zero."""
    try:
        w, h = (int(v) for v in xml_frame_root.get("OSD_SIZE", "").lower().split("x"))
    except (ValueError, AttributeError):
        return DEFAULT_OSD_SIZE
    if w <= 0 or h <= 0:
        return DEFAULT_OSD_SIZE
    return w, h


def parse_zones(xml_frame):
    """Alarm zones from one metadata frame -> [{"name": str, "points": [(fx, fy)]}].

    Points come back as vendor-neutral TOP-LEFT fractions in [0, 1] -- the same
    space vendor_adapter.Scenario uses -- so a caller can overlay them on a
    snapshot of any resolution. PD's own X2D/Y2D are pixels in the OSD_SIZE frame
    and can sit exactly on the far edge (X2D=384 on a 384-wide frame was observed
    live), hence the clamp.

    Raises PDError on a frame that isn't parseable at all; an app with no zones
    configured is not an error and returns []."""
    try:
        root = ET.fromstring(xml_frame)
    except ET.ParseError as e:
        raise PDError(f"unparseable Perimeter Defender metadata frame: {e}") from e

    ref_w, ref_h = osd_size(root)
    zones = []
    for zone in root.findall(".//ALERT_ZONE_LIST/ZONE"):
        points = sorted(zone.findall("POINT"),
                        key=lambda p: int(p.get("NUMBER", "0") or "0"))
        verts = []
        for p in points:
            try:
                raw_x = float(p.get("X2D", "0"))
                raw_y = float(p.get("Y2D", "0"))
            except ValueError:
                continue
            verts.append((min(1.0, max(0.0, raw_x / ref_w)),
                          min(1.0, max(0.0, raw_y / ref_h))))
        if verts:
            zones.append({"name": zone.get("NAME", "zone"), "points": verts})
    return zones


def parse_scenarios_xml(xml_text):
    """PD scenario definitions -> [{"name", "type", "kind", "zones", "duration"}].

    `zones` are zone NAMES (the geometry for them comes from the metadata frame),
    `duration` is seconds converted from PD's min_duration milliseconds, and
    `kind` is the writer's neutral vocabulary. Unknown/absent input returns []."""
    if not (xml_text or "").strip():
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise PDError(f"unparseable scenarios.xml: {e}") from e

    out = []
    for sc in root.findall(".//SZE-scenario"):
        raw_type = (sc.get("type") or "").strip()
        zone_els = sc.findall("zone")
        duration_ms = 0
        for z in zone_els:
            try:
                duration_ms = max(duration_ms, int(z.get("min_duration", "0") or "0"))
            except ValueError:
                continue
        out.append({
            "name": sc.get("name", "scenario"),
            "type": raw_type,
            "kind": PD_TYPE_TO_KIND.get(raw_type.lower(), "intrusion"),
            "zones": [z.get("name", "") for z in zone_els],
            "duration": duration_ms // 1000,
            "analytics_mode": sc.get("analyticsMode", ""),
        })
    return out


def merge(zones, scenarios):
    """Join the two halves of PD's split config into one list of rules.

    Geometry lives in the metadata frame (keyed by zone name); the scenario's
    name, type and dwell time live in scenarios.xml. Neither alone is a complete
    picture, so this pairs them by zone name.

    Every zone is returned exactly once. A zone no scenario claims is still
    reported (PD keeps drawn-but-unused zones) rather than dropped, because an
    operator looking at the overlay needs to see what is actually on the camera."""
    by_name = {z["name"]: z for z in zones}
    rules, claimed = [], set()
    for sc in scenarios:
        for zone_name in sc["zones"]:
            zone = by_name.get(zone_name)
            if zone is None:
                continue
            claimed.add(zone_name)
            rules.append({
                "name": f"{sc['name']} / {zone_name}",
                "kind": sc["kind"],
                "pd_type": sc["type"],
                "points": zone["points"],
                "duration": sc["duration"],
            })
    for zone in zones:
        if zone["name"] not in claimed:
            rules.append({
                "name": zone["name"],
                "kind": "intrusion",
                "pd_type": "unassigned zone",
                "points": zone["points"],
                "duration": 0,
            })
    return rules


def detect(session, ip, port, auth_strategies):
    """Is Perimeter Defender the analytics engine on this camera?

    Returns the list of RUNNING application NiceNames, or None when the list
    couldn't be read. Callers pair this with an Object Analytics 404 -- that
    combination is what identifies a fixed thermal (see camera_engine's
    _detect_active_analytics_app, which does the same check on the audit side)."""
    url = f"http://{ip}:{port}/axis-cgi/applications/list.cgi"
    for auth in auth_strategies:
        try:
            resp = session.get(url, auth=auth, timeout=NET_TIMEOUT, verify=False)
        except requests.exceptions.RequestException:
            continue
        if resp.status_code != 200:
            continue
        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError:
            return None
        return [app.get("NiceName", app.get("Name", "Unknown App"))
                for app in root.findall(".//application")
                if app.get("Status") == "Running"]
    return None


def is_perimeter_defender(running_apps):
    return bool(running_apps) and any("perimeter" in a.lower() for a in running_apps)
