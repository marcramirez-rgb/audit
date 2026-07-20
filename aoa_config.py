"""AXIS Object Analytics (AOA) configuration layer -- the WRITE-side sibling of the
read paths in camera_engine.py.

This module talks to the same endpoint the audit engine reads from
(`/local/objectanalytics/control.cgi`), using the same digest->basic auth fallback,
but adds the verbs needed to *change* analytics config:

    getSupportedVersions -> get_supported_versions()
    getConfigurationCapabilities -> get_capabilities()
    getConfiguration -> get_config()
    setConfiguration -> set_config(full_config)

CRITICAL SEMANTIC: setConfiguration REPLACES the entire configuration object. There
is no per-scenario patch. Every write must be read-modify-write of the *whole* config.
Never build a config from empty and push it, or you wipe every other scenario plus
device-specific defaults. `apply_scenario()` enforces this pattern and always backs
up the current config to disk first.

Nothing in here is GUI-aware; it is importable and unit-testable without a camera.
"""

import json
from datetime import datetime
from io import BytesIO
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from requests.auth import HTTPBasicAuth, HTTPDigestAuth
from urllib3.util.retry import Retry

# Reuse the audit engine's proven constants/handlers so the writer never drifts
# from what already works in the field.
import camera_engine

# Matches camera_engine.STRICT_TIMEOUT; kept as a local alias so this module can be
# imported/tested even if that constant is ever renamed there.
CONTROL_PATH = "/local/objectanalytics/control.cgi"
DEFAULT_API_VERSION = "1.2"  # what the audit read path uses today; probe confirms per-firmware
NET_TIMEOUT = getattr(camera_engine, "STRICT_TIMEOUT", (3.05, 5.0))


class AOAError(RuntimeError):
    """Raised when the camera answers but reports a logical failure.

    AOA's local CGI returns HTTP 200 with an {"error": {...}} body on failure
    (unlicensed/stopped app, unsupported apiVersion, invalid config, ...) instead
    of a non-200 status -- the same trap camera_engine.py:514 documents on read.
    """


class AOAAuthError(AOAError):
    """Every auth strategy was rejected (401). Usually a read-only account trying
    to write, or wrong credentials."""


class AOAClient:
    """One camera + one port. Cheap to construct; opens a session lazily."""

    def __init__(self, ip, user, password, port, api_version=DEFAULT_API_VERSION,
                 use_https=False, session=None):
        self.ip = ip
        self.port = str(port)
        self.user = user
        self.api_version = api_version
        self.scheme = "https" if use_https else "http"
        # Same order the audit engine uses: digest first, basic fallback.
        self.auth_strategies = [HTTPDigestAuth(user, password), HTTPBasicAuth(user, password)]
        self._context = 0  # JSON-RPC-style request id, incremented per call
        self.session = session or self._build_session()
        # Reuse the audit handler purely for its snapshot logic -- one source of truth.
        self._axis = camera_engine.AxisHandler(ip, user, password)

    @staticmethod
    def _build_session():
        s = requests.Session()
        retries = Retry(total=2, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retries)
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        return s

    @property
    def control_url(self):
        return f"{self.scheme}://{self.ip}:{self.port}{CONTROL_PATH}"

    # ------------------------------------------------------------------ transport

    def _call(self, method, params=None):
        """POST one AOA control method. Returns the full decoded JSON response
        (the caller pulls `.data` out). Raises AOAAuthError / AOAError on failure."""
        self._context += 1
        payload = {
            "apiVersion": self.api_version,
            "context": str(self._context),
            "method": method,
        }
        if params is not None:
            payload["params"] = params

        last_err = "All authentication attempts failed"
        saw_401 = False
        saw_other = False

        for auth in self.auth_strategies:
            try:
                resp = self.session.post(self.control_url, auth=auth, json=payload,
                                         timeout=NET_TIMEOUT, verify=False)
            except requests.exceptions.RequestException as e:
                saw_other = True
                last_err = f"{type(e).__name__}: {e}"
                continue

            if resp.status_code == 401:
                saw_401 = True
                last_err = "HTTP 401 (auth rejected)"
                continue
            if resp.status_code != 200:
                saw_other = True
                last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
                continue

            try:
                data = resp.json()
            except ValueError:
                saw_other = True
                last_err = f"Non-JSON 200 body: {resp.text[:200]}"
                continue

            # HTTP 200 does NOT mean success for these CGIs.
            if isinstance(data, dict) and "error" in data:
                err = data["error"]
                if isinstance(err, dict):
                    raise AOAError(f"AOA error {err.get('code', '?')}: {err.get('message', err)}")
                raise AOAError(f"AOA error: {err}")
            return data

        if saw_401 and not saw_other:
            raise AOAAuthError(
                "AOA rejected every credential (401). If reads work but writes fail, "
                "the account is likely read-only -- writing scenarios needs an admin account."
            )
        raise AOAError(last_err)

    # ------------------------------------------------------------------ read verbs

    def get_supported_versions(self):
        return self._call("getSupportedVersions")

    def get_capabilities(self):
        """What this firmware actually supports (scenario types, object classes,
        filters). MUST gate the UI -- older cameras won't offer 'fence' or 'vehicle'."""
        return self._call("getConfigurationCapabilities")

    def get_config(self):
        """Full current configuration. This is the object you mutate and send back."""
        return self._call("getConfiguration")

    def fetch_snapshot(self):
        """PIL image for the drawing surface. Delegates to the audit engine's Axis
        snapshot logic (GET->POST fallback, both auth strategies)."""
        img, url, err, auth_rejected = self._axis.fetch_snapshot(self.session, self.port)
        if img is None:
            if auth_rejected:
                raise AOAAuthError(f"Snapshot auth rejected at {url}")
            raise AOAError(f"Snapshot failed at {url}: {err}")
        return img

    # ------------------------------------------------------------------ write verbs

    def set_config(self, full_config):
        """Push a COMPLETE configuration object. Prefer apply_scenario() which
        backs up first and preserves everything else."""
        params = full_config.get("data", full_config) if isinstance(full_config, dict) else full_config
        return self._call("setConfiguration", params=params)

    def backup_config(self, backup_dir):
        """Dump the current full config to a timestamped JSON. Returns the path.
        This is both the safety net and the undo file."""
        backup_dir = Path(backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        # No Date.now-style call restriction here (that's the workflow sandbox); real
        # datetime is fine in normal Python.
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        cfg = self.get_config()
        path = backup_dir / f"aoa_backup_{self.ip.replace('.', '_')}_{self.port}_{stamp}.json"
        path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        return path, cfg

    def apply_scenario(self, scenario, backup_dir, replace_by_name=True):
        """The safe write path: back up current config -> insert/replace one scenario
        in the FULL config -> push the whole thing back -> re-read for verification.

        Returns (backup_path, verify_config).
        """
        validate_scenario(scenario)
        backup_path, current = self.backup_config(backup_dir)
        new_config = insert_or_replace_scenario(current, scenario, replace_by_name=replace_by_name)
        self.set_config(new_config)
        verify = self.get_config()
        return backup_path, verify


# ---------------------------------------------------------------- coordinate math

def pixel_to_norm(px, py, img_w, img_h):
    """Screen pixel -> AOA normalized coordinate. Inverse of camera_engine.py:603.

    AOA space: x in [-1, 1] left->right, y in [-1, 1] bottom->top (Y is UP).
    """
    nx = (px / img_w) * 2.0 - 1.0
    ny = 1.0 - (py / img_h) * 2.0
    return (round(nx, 6), round(ny, 6))


def norm_to_pixel(nx, ny, img_w, img_h):
    """AOA normalized coordinate -> screen pixel. Matches camera_engine.py:603
    exactly so drawn zones and read-back zones land on the same pixels."""
    px = int(((nx + 1.0) / 2.0) * img_w)
    py = int(((1.0 - ny) / 2.0) * img_h)
    return (px, py)


# ---------------------------------------------------- scenario builders
#
# Corrected against REAL getConfiguration + getConfigurationCapabilities output from
# 10.23.164.21:5010 (AOA API 1.6). See aoa_probes/. Constants below are the actual
# firmware constraints; validate_scenario() enforces them before any write.
#
# Confirmed constraints:
#   supportedScenarios: motion | fence | crosslinecounting
#   trigger includeArea: 3..20 vertices, condition individualTimeInArea (loiter)
#   trigger fence:       2..10 vertices, no conditions
#   scenario name:       1..15 chars (maxLengthName=15)
#   objectClassifications: human | vehicle only
#   REQUIRED scenario keys observed: devices, perspectives, presets (empty lists ok)

VALID_CLASSES = ("human", "vehicle")
MAX_NAME_LEN = 15
AREA_MIN_VERTS, AREA_MAX_VERTS = 3, 20
FENCE_MIN_VERTS, FENCE_MAX_VERTS = 2, 10
MAX_SCENARIOS = 10


def _next_scenario_id(config):
    scenarios = config.get("data", {}).get("scenarios", []) if isinstance(config, dict) else []
    used = [s.get("id") for s in scenarios if isinstance(s.get("id"), int)]
    return (max(used) + 1) if used else 1


def _classifications(classes):
    return [{"type": c} for c in classes if c in VALID_CLASSES]


def _scenario_skeleton(name, scenario_type, classes, scenario_id, device_id):
    """Common scenario shell matching the real config shape (devices/perspectives/
    presets are required keys even when empty)."""
    return {
        "id": scenario_id,
        "name": name,  # NOT truncated -- validate_scenario rejects >15 so nothing is silently lost
        "type": scenario_type,
        "devices": [{"id": device_id}],
        "objectClassifications": _classifications(classes),
        "perspectives": [],
        "presets": [],
        "filters": [],
        "triggers": [],
    }


def build_intrusion(name, vertices_norm, classes=("human",), scenario_id=None, device_id=1):
    """Object-in-area (intrusion). type=motion, trigger=includeArea (3..20 vertices)."""
    s = _scenario_skeleton(name, "motion", classes, scenario_id, device_id)
    s["triggers"] = [{"type": "includeArea", "vertices": [[x, y] for (x, y) in vertices_norm]}]
    return s


def build_line_crossing(name, line_norm, classes=("human",), scenario_id=None, device_id=1):
    """Virtual fence / line crossing. type=fence, trigger=fence (2..10 vertices)."""
    s = _scenario_skeleton(name, "fence", classes, scenario_id, device_id)
    s["triggers"] = [{"type": "fence", "vertices": [[x, y] for (x, y) in line_norm]}]
    return s


def build_loiter(name, vertices_norm, seconds, classes=("human",), scenario_id=None, device_id=1):
    """Time-in-area (loitering) = a motion/includeArea scenario carrying an
    individualTimeInArea condition.

    Condition shape CONFIRMED against a real UI-configured loiter scenario on
    10.23.164.21:5015 ("Scene1_H_20s"): the per-class list key is "data", not
    "classifications" (capabilities was misleading here). Matches what
    camera_engine.parse_analytics reads (condition["data"][i]["time"]).
    """
    s = build_intrusion(name, vertices_norm, classes=classes, scenario_id=scenario_id, device_id=device_id)
    s["triggers"][0]["conditions"] = [{
        "type": "individualTimeInArea",
        "data": [{"type": c, "time": int(seconds), "alarmTime": 1}
                 for c in classes if c in VALID_CLASSES],
    }]
    return s


EXCLUDE_MIN_VERTS, EXCLUDE_MAX_VERTS = 3, 10
MAX_EXCLUDE_ZONES = 5


def build_exclude_area(vertices_norm):
    """One exclusion polygon -- an area within the scenario where detections are
    ignored. Shape mirrors the (confirmed) includeArea trigger convention: a bare
    'vertices' list. Attach with add_exclude_zones()."""
    return {"type": "excludeArea", "vertices": [[x, y] for (x, y) in vertices_norm]}


def add_exclude_zones(scenario, zones_norm):
    """Append excludeArea filters to a scenario. `zones_norm` is a list of vertex
    lists (each [(x, y), ...]). Returns the scenario (mutated). Enforces the AOA
    caps: <=5 zones, 3..10 vertices each."""
    existing = [f for f in scenario.get("filters", []) if f.get("type") == "excludeArea"]
    if len(existing) + len(zones_norm) > MAX_EXCLUDE_ZONES:
        raise ValueError(f"at most {MAX_EXCLUDE_ZONES} exclusion zones per scenario")
    filters = scenario.setdefault("filters", [])
    for verts in zones_norm:
        n = len(verts)
        if not (EXCLUDE_MIN_VERTS <= n <= EXCLUDE_MAX_VERTS):
            raise ValueError(f"exclusion zone needs {EXCLUDE_MIN_VERTS}..{EXCLUDE_MAX_VERTS} vertices, got {n}")
        filters.append(build_exclude_area(verts))
    return scenario


def validate_scenario(scenario):
    """Raise ValueError if the scenario violates the firmware constraints we captured.
    Cheap insurance against a rejected (or worse, silently wrong) write."""
    name = scenario.get("name", "")
    if not (1 <= len(name) <= MAX_NAME_LEN):
        raise ValueError(f"name must be 1..{MAX_NAME_LEN} chars, got {len(name)}: {name!r}")
    stype = scenario.get("type")
    if stype not in ("motion", "fence", "crosslinecounting"):
        raise ValueError(f"unsupported scenario type: {stype!r}")
    if not scenario.get("objectClassifications"):
        raise ValueError("at least one object classification (human/vehicle) required")
    trig = (scenario.get("triggers") or [{}])[0]
    n = len(trig.get("vertices", []))
    if trig.get("type") == "includeArea" and not (AREA_MIN_VERTS <= n <= AREA_MAX_VERTS):
        raise ValueError(f"area needs {AREA_MIN_VERTS}..{AREA_MAX_VERTS} vertices, got {n}")
    if trig.get("type") == "fence" and not (FENCE_MIN_VERTS <= n <= FENCE_MAX_VERTS):
        raise ValueError(f"fence needs {FENCE_MIN_VERTS}..{FENCE_MAX_VERTS} vertices, got {n}")
    excludes = [f for f in scenario.get("filters", []) if f.get("type") == "excludeArea"]
    if len(excludes) > MAX_EXCLUDE_ZONES:
        raise ValueError(f"at most {MAX_EXCLUDE_ZONES} exclusion zones, got {len(excludes)}")
    for ex in excludes:
        en = len(ex.get("vertices", []))
        if not (EXCLUDE_MIN_VERTS <= en <= EXCLUDE_MAX_VERTS):
            raise ValueError(f"exclusion zone needs {EXCLUDE_MIN_VERTS}..{EXCLUDE_MAX_VERTS} vertices, got {en}")
    return True


def insert_or_replace_scenario(config, scenario, replace_by_name=True):
    """Return a NEW full config with `scenario` inserted or replacing an existing one.
    Does not mutate the input. Match order: by id (edit-in-place), then optionally by
    name, else append (assigning the next free id if none)."""
    if not isinstance(config, dict):
        raise ValueError("config must be the dict returned by get_config()")
    new_config = json.loads(json.dumps(config))  # deep copy
    data = new_config.setdefault("data", {})
    scenarios = data.setdefault("scenarios", [])

    sid = scenario.get("id")
    # Edit-in-place: a scenario carrying an existing id replaces that entry outright,
    # so renames still target the right scenario.
    if sid is not None:
        for i, existing in enumerate(scenarios):
            if existing.get("id") == sid:
                scenarios[i] = scenario
                return new_config

    if sid is None:
        scenario = {**scenario, "id": _next_scenario_id(new_config)}

    if replace_by_name:
        for i, existing in enumerate(scenarios):
            if existing.get("name") == scenario.get("name"):
                scenario = {**scenario, "id": existing.get("id", scenario["id"])}
                scenarios[i] = scenario
                return new_config

    scenarios.append(scenario)
    return new_config


def update_scenario_geometry(scenario, include_verts_norm, classes,
                             exclude_zones_norm=None, loiter_seconds=None):
    """Patch an EXISTING scenario in place (returns a copy) changing only the
    user-editable fields -- object classes, the trigger polygon/line, exclusion
    zones, and loiter time -- while preserving id, type, perspectives, presets,
    metadataOverlay, and any non-exclusion filters (size/distance/etc.). This is
    what 'edit an existing scenario' must use so rich UI-configured settings aren't
    silently discarded by rebuilding from scratch."""
    s = json.loads(json.dumps(scenario))  # deep copy
    s["objectClassifications"] = _classifications(classes)
    if s.get("triggers"):
        s["triggers"][0]["vertices"] = [[x, y] for (x, y) in include_verts_norm]
        conds = s["triggers"][0].get("conditions")
        if conds and loiter_seconds is not None:
            for c in conds:
                if c.get("type") == "individualTimeInArea":
                    c["data"] = [{"type": cl, "time": int(loiter_seconds), "alarmTime": 1}
                                 for cl in classes if cl in VALID_CLASSES]
    # Preserve non-exclusion filters; replace the exclusion set with what was drawn.
    s["filters"] = [f for f in s.get("filters", []) if f.get("type") != "excludeArea"]
    if exclude_zones_norm:
        add_exclude_zones(s, exclude_zones_norm)
    return s
