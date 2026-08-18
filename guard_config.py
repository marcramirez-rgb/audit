"""AXIS Guard apps (Motion / Fence / Loitering Guard) -- the WRITABLE analytics
path on Axis FIXED THERMAL units.

WHY THIS EXISTS
---------------
Fixed thermals (AXIS Q1971-E) ship running AXIS Perimeter Defender, which has no
zone-configuration API at all and stores its zones in an encrypted blob -- see
pd_config.py for the evidence. So the writer could read those cameras but never
change them.

Axis PREINSTALLS Motion Guard, Fence Guard and Loitering Guard on these same
units (confirmed on 10.23.34.243:5015 and :5020), and each of those DOES expose a
documented control.cgi with getConfiguration/setConfiguration -- the same
JSON-RPC shape aoa_config.py already speaks. That is the write path.

CONFIRMED LIVE on 10.23.34.243:5020 (Q1971-E, firmware 11.11.116). The apps were
started, probed and stopped again; every constant below came from that camera's
own getConfigurationCapabilities, not from documentation:

    all three   profiles 0..10 per camera, name 1..15 chars, coords -1..1
    fenceguard      trigger 'fence'         2..10 vertices, 1 instance
                    filters sizePercentage 3..100%, timeShortLivedLimit 1..5s
                    NO exclusion zones
    motionguard     trigger 'includeArea'   3..10 vertices, 1 instance
                    filters sizePercentage, timeShortLivedLimit 1..180s,
                            distanceSwayingObject 3..20, excludeArea 0..3
    loiteringguard  trigger 'loiteringArea' 3..10 vertices, 1 instance
                    conditions individual/group 1..360 SECONDS
                    filters sizePercentage, distanceSwayingObject, excludeArea 0..3

THREE THINGS THAT DIFFER FROM AOA AND WILL BITE IF FORGOTTEN
------------------------------------------------------------
1. Trigger vertices live under "data", NOT "vertices"; the rule list is
   "profiles", not "scenarios"; the per-rule id is "uid", not "id".
2. ONE APP PER RULE TYPE. AOA puts every scenario type in one config; here an
   area rule and a line rule live in two different applications, each with its
   own config document and its own running/stopped state. A rule inside a
   STOPPED app does nothing at all, which is why apply_profile() starts it.
3. NO OBJECT CLASSIFICATION. These apps predate AOA's classifier -- there is no
   human/vehicle concept, only size and duration filters. Anything relying on
   classes must not silently pretend otherwise.

setConfiguration REPLACES the whole configuration for that app, exactly like AOA,
so every write is a read-modify-write of the full document.
"""

import json
from datetime import datetime
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from requests.auth import HTTPBasicAuth, HTTPDigestAuth
from urllib3.util.retry import Retry

import camera_engine

NET_TIMEOUT = getattr(camera_engine, "STRICT_TIMEOUT", (3.05, 5.0))
DEFAULT_API_VERSION = "1.0"  # every Guard app on the tested firmware accepts 1.0..1.4

#: Which application implements each of the writer's neutral rule kinds.
KIND_TO_APP = {"intrusion": "motionguard", "line": "fenceguard", "loiter": "loiteringguard"}
APP_TO_KIND = {v: k for k, v in KIND_TO_APP.items()}

#: The single trigger type each app accepts.
APP_TRIGGER = {"motionguard": "includeArea", "fenceguard": "fence",
               "loiteringguard": "loiteringArea"}

MAX_PROFILES = 10
MAX_NAME_LEN = 15
AREA_MIN_VERTS, AREA_MAX_VERTS = 3, 10
FENCE_MIN_VERTS, FENCE_MAX_VERTS = 2, 10
EXCLUDE_MIN_VERTS, EXCLUDE_MAX_VERTS = 3, 10
MAX_EXCLUDE_ZONES = 3
LOITER_MIN_SECONDS, LOITER_MAX_SECONDS = 1, 360
VALID_ALARM_DIRECTIONS = ("leftToRight", "rightToLeft", "both")

#: Apps that support exclusion zones (Fence Guard does not).
APPS_WITH_EXCLUSIONS = ("motionguard", "loiteringguard")


class GuardError(RuntimeError):
    """The app answered but reported a logical failure. Like AOA, these CGIs
    return HTTP 200 with an {"error": {...}} body instead of a non-200 status."""


class GuardAuthError(GuardError):
    """Every credential was rejected (401)."""


class GuardAppStopped(GuardError):
    """The application is installed but not running, so its control.cgi cannot
    answer. Raised instead of the bare HTTP 500 the camera actually returns."""


class GuardClient:
    """One camera + one port + ONE Guard application."""

    def __init__(self, ip, user, password, port, app, api_version=DEFAULT_API_VERSION,
                 use_https=False, session=None):
        if app not in APP_TRIGGER:
            raise ValueError(f"unknown Guard app {app!r} -- expected one of {sorted(APP_TRIGGER)}")
        self.ip = ip
        self.port = str(port)
        self.app = app
        self.api_version = api_version
        self.scheme = "https" if use_https else "http"
        self.auth_strategies = [HTTPDigestAuth(user, password), HTTPBasicAuth(user, password)]
        self._context = 0
        self.session = session or self._build_session()
        self._axis = camera_engine.AxisHandler(ip, user, password)

    @staticmethod
    def _build_session():
        s = requests.Session()
        retries = Retry(total=2, backoff_factor=0.5, status_forcelist=[502, 503, 504])
        adapter = HTTPAdapter(max_retries=retries)
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        return s

    @property
    def base(self):
        return f"{self.scheme}://{self.ip}:{self.port}"

    @property
    def control_url(self):
        return f"{self.base}/local/{self.app}/control.cgi"

    # ------------------------------------------------------------------ transport

    def _call(self, method, params=None):
        self._context += 1
        payload = {"apiVersion": self.api_version, "context": str(self._context),
                   "method": method}
        if params is not None:
            payload["params"] = params

        last_err = "all authentication attempts failed"
        saw_401 = saw_500 = saw_other = False

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
                continue
            if resp.status_code == 500:
                # The route stays registered while the app is stopped, so a stopped
                # app is a 500 rather than a 404. Distinguish it: "start the app" is
                # a very different fix from "this camera is broken".
                saw_500 = True
                last_err = f"HTTP 500 from {self.app}"
                continue
            if resp.status_code != 200:
                saw_other = True
                last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
                continue

            try:
                data = resp.json()
            except ValueError:
                saw_other = True
                last_err = f"non-JSON 200 body: {resp.text[:200]}"
                continue

            if isinstance(data, dict) and "error" in data:
                err = data["error"]
                if isinstance(err, dict):
                    raise GuardError(f"{self.app} error {err.get('code', '?')}: "
                                     f"{err.get('message', err)}")
                raise GuardError(f"{self.app} error: {err}")
            return data

        if saw_401 and not (saw_other or saw_500):
            raise GuardAuthError(
                f"{self.app} rejected every credential (401). Writing profiles needs "
                f"an administrator account.")
        if saw_500 and not saw_other:
            raise GuardAppStopped(
                f"AXIS {self.app} is installed but not running on {self.ip}:{self.port}, "
                f"so its configuration API returns HTTP 500. Start the application "
                f"first -- a profile inside a stopped app never fires.")
        raise GuardError(last_err)

    # ------------------------------------------------------------------ app lifecycle

    def app_status(self):
        """'Running' / 'Stopped' / None when the app isn't installed."""
        url = f"{self.base}/axis-cgi/applications/list.cgi"
        for auth in self.auth_strategies:
            try:
                resp = self.session.get(url, auth=auth, timeout=NET_TIMEOUT, verify=False)
            except requests.exceptions.RequestException:
                continue
            if resp.status_code != 200:
                continue
            import xml.etree.ElementTree as ET
            try:
                root = ET.fromstring(resp.text)
            except ET.ParseError:
                return None
            for el in root.findall(".//application"):
                if el.get("Name") == self.app:
                    return el.get("Status")
            return None
        return None

    def _app_action(self, action):
        url = (f"{self.base}/axis-cgi/applications/control.cgi"
               f"?action={action}&package={self.app}")
        for auth in self.auth_strategies:
            try:
                resp = self.session.get(url, auth=auth, timeout=(NET_TIMEOUT[0], 30),
                                        verify=False)
            except requests.exceptions.RequestException as e:
                raise GuardError(f"{action} {self.app}: {type(e).__name__}: {e}") from e
            if resp.status_code == 401:
                continue
            body = resp.text.strip()
            # VAPIX answers "OK", or "Error: <reason>"; already-in-that-state is fine.
            if resp.status_code == 200 and (body.upper().startswith("OK")
                                            or "already" in body.lower()):
                return body
            raise GuardError(f"{action} {self.app} failed: HTTP {resp.status_code} {body[:120]}")
        raise GuardAuthError(f"{action} {self.app}: every credential rejected (401)")

    def start_app(self, wait_seconds=30):
        """Start the application and wait for it to report Running.

        Confirmed live: starting a Guard app does NOT stop Perimeter Defender --
        they coexist on the tested firmware, so this does not take the camera's
        existing analytics offline."""
        import time
        if self.app_status() == "Running":
            return False
        self._app_action("start")
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            if self.app_status() == "Running":
                return True
            time.sleep(1.5)
        raise GuardError(f"{self.app} did not reach Running within {wait_seconds}s")

    def stop_app(self):
        self._app_action("stop")

    # ------------------------------------------------------------------ read verbs

    def get_supported_versions(self):
        return self._call("getSupportedVersions")

    def get_capabilities(self):
        return self._call("getConfigurationCapabilities")

    def get_config(self):
        return self._call("getConfiguration")

    def fetch_snapshot(self, channel_idx=None):
        img, url, err, auth_rejected = self._axis.fetch_snapshot(
            self.session, self.port, channel_idx=channel_idx)
        if img is None:
            if auth_rejected:
                raise GuardAuthError(f"snapshot auth rejected at {url}")
            raise GuardError(f"snapshot failed at {url}: {err}")
        return img

    # ------------------------------------------------------------------ write verbs

    def set_config(self, full_config):
        params = full_config.get("data", full_config) if isinstance(full_config, dict) else full_config
        return self._call("setConfiguration", params=params)

    def backup_config(self, backup_dir):
        backup_dir = Path(backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        cfg = self.get_config()
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        path = backup_dir / f"guard_{self.app}_{self.ip.replace('.', '_')}_{self.port}_{stamp}.json"
        path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        return path, cfg

    def apply_profile(self, profile, backup_dir, replace_by_name=True, autostart=True):
        """Safe write: ensure the app runs -> back up -> insert/replace ONE profile
        in the full config -> push it all back -> re-read to verify.

        Returns (backup_path, verify_config, started) where `started` records
        whether this call had to start the application."""
        validate_profile(profile, self.app)
        started = False
        if autostart:
            started = self.start_app()
        backup_path, current = self.backup_config(backup_dir)
        new_config = insert_or_replace_profile(current, profile, self.app,
                                               replace_by_name=replace_by_name)
        self.set_config(new_config)
        return backup_path, self.get_config(), started


# ---------------------------------------------------------------- coordinate math

def frac_to_guard(points):
    """Neutral top-left fraction [0,1] -> Guard normalized [-1,1] with Y up.
    Identical to the AOA mapping, so drawings transfer between the two."""
    return [[2.0 * fx - 1.0, 1.0 - 2.0 * fy] for (fx, fy) in points]


def guard_to_frac(data):
    """Guard normalized [-1,1] -> neutral top-left fraction [0,1]."""
    return [((gx + 1.0) / 2.0, (1.0 - gy) / 2.0) for gx, gy in data]


# ---------------------------------------------------------------- builders

def _profile_skeleton(name, uid, camera_id=1):
    return {"name": name, "uid": uid, "camera": camera_id, "filters": [], "triggers": []}


def _next_uid(config):
    profiles = config.get("data", {}).get("profiles", []) if isinstance(config, dict) else []
    used = [p.get("uid") for p in profiles if isinstance(p.get("uid"), int)]
    return (max(used) + 1) if used else 1


def build_area_profile(name, points_guard, uid=None, camera_id=1, exclusions=None):
    """Motion Guard: object in area."""
    p = _profile_skeleton(name, uid, camera_id)
    p["triggers"] = [{"type": "includeArea", "data": [list(v) for v in points_guard]}]
    if exclusions:
        add_exclude_zones(p, exclusions)
    return p


def build_fence_profile(name, points_guard, uid=None, camera_id=1,
                        alarm_direction="leftToRight"):
    """Fence Guard: line crossing.

    Unlike AOA -- whose fence is strictly one-way, forcing the -LR/-RL scenario
    pair in AxisAdapter -- Fence Guard accepts 'both' as a single profile."""
    if alarm_direction not in VALID_ALARM_DIRECTIONS:
        raise ValueError(f"alarm_direction must be one of {VALID_ALARM_DIRECTIONS}")
    p = _profile_skeleton(name, uid, camera_id)
    p["triggers"] = [{"type": "fence", "alarmDirection": alarm_direction,
                      "data": [list(v) for v in points_guard]}]
    return p


def build_loiter_profile(name, points_guard, seconds, uid=None, camera_id=1,
                         exclusions=None):
    """Loitering Guard: time in area. `seconds` goes straight into the
    'individual' condition (the app takes seconds, unlike AOA's per-class list)."""
    secs = int(seconds)
    if not (LOITER_MIN_SECONDS <= secs <= LOITER_MAX_SECONDS):
        raise ValueError(f"loiter time must be {LOITER_MIN_SECONDS}..{LOITER_MAX_SECONDS}s, "
                         f"got {secs}")
    p = _profile_skeleton(name, uid, camera_id)
    p["triggers"] = [{
        "type": "loiteringArea",
        "data": [list(v) for v in points_guard],
        "conditions": [{"type": "individual", "data": secs, "active": True}],
    }]
    if exclusions:
        add_exclude_zones(p, exclusions)
    return p


def add_exclude_zones(profile, zones_guard):
    """Exclusion zones are FILTERS here (type 'excludeArea'), not triggers.
    Motion/Loitering Guard allow up to 3; Fence Guard has none at all."""
    filters = profile.setdefault("filters", [])
    existing = [f for f in filters if f.get("type") == "excludeArea"]
    if len(existing) + len(zones_guard) > MAX_EXCLUDE_ZONES:
        raise ValueError(f"at most {MAX_EXCLUDE_ZONES} exclusion zones per profile")
    for verts in zones_guard:
        n = len(verts)
        if not (EXCLUDE_MIN_VERTS <= n <= EXCLUDE_MAX_VERTS):
            raise ValueError(f"exclusion zone needs {EXCLUDE_MIN_VERTS}..{EXCLUDE_MAX_VERTS} "
                             f"vertices, got {n}")
        filters.append({"type": "excludeArea", "active": True,
                        "data": [list(v) for v in verts]})
    return profile


# ---------------------------------------------------------------- factory defaults

def default_geometry(capabilities, trigger_type):
    """The out-of-box trigger geometry the app advertises for `trigger_type`."""
    for tr in (capabilities or {}).get("data", {}).get("triggers", []):
        if tr.get("type") == trigger_type:
            return tr.get("defaultInstance")
    return None


def _same_geometry(a, b, tol=1e-6):
    if not a or not b or len(a) != len(b):
        return False
    return all(abs(float(p[0]) - float(q[0])) <= tol and abs(float(p[1]) - float(q[1])) <= tol
               for p, q in zip(a, b))


def is_factory_default(profile, capabilities):
    """True when this profile is the app's UNTOUCHED out-of-box profile.

    Every Guard app ships with one profile (named "Profile 1") whose trigger is
    the capability's defaultInstance -- for Motion and Loitering Guard that is a
    rectangle spanning -0.97..0.97, i.e. 97% of the frame.

    This matters operationally, not just visually. Starting a Guard app to hold a
    carefully drawn zone ALSO activates that default, so the app keeps detecting
    across the whole image and the drawn zone stops meaning anything. Callers
    surface these so an operator can see -- and remove -- them.

    Matched on GEOMETRY, not on the name: someone who renamed the default but
    never moved it still has a full-frame profile, and someone who kept the name
    but redrew the area has a real zone that must not be offered for deletion."""
    trig = (profile.get("triggers") or [{}])[0]
    dflt = default_geometry(capabilities, trig.get("type"))
    if dflt is None:
        return False
    return _same_geometry(trig.get("data") or [], dflt)


# ---------------------------------------------------------------- validation

def validate_profile(profile, app):
    """Reject anything the camera's own capabilities say it will refuse."""
    name = profile.get("name", "")
    if not (1 <= len(name) <= MAX_NAME_LEN):
        raise ValueError(f"name must be 1..{MAX_NAME_LEN} chars, got {len(name)}: {name!r}")

    triggers = profile.get("triggers") or []
    if len(triggers) != 1:
        raise ValueError(f"a Guard profile carries exactly one trigger, got {len(triggers)}")
    trig = triggers[0]

    expected = APP_TRIGGER[app]
    if trig.get("type") != expected:
        raise ValueError(f"{app} only accepts a {expected!r} trigger, got {trig.get('type')!r}")

    verts = trig.get("data") or []
    n = len(verts)
    if expected == "fence":
        lo, hi = FENCE_MIN_VERTS, FENCE_MAX_VERTS
    else:
        lo, hi = AREA_MIN_VERTS, AREA_MAX_VERTS
    if not (lo <= n <= hi):
        raise ValueError(f"{expected} needs {lo}..{hi} vertices, got {n}")

    for v in verts:
        if len(v) != 2 or not all(-1.0 <= float(c) <= 1.0 for c in v):
            raise ValueError(f"vertices must be [x, y] within -1..1, got {v!r}")

    if expected == "fence":
        ad = trig.get("alarmDirection")
        if ad is not None and ad not in VALID_ALARM_DIRECTIONS:
            raise ValueError(f"alarmDirection must be one of {VALID_ALARM_DIRECTIONS}, got {ad!r}")

    if expected == "loiteringArea":
        conds = [c for c in (trig.get("conditions") or []) if c.get("active")]
        if not conds:
            raise ValueError("a loitering profile needs an active condition")
        for c in conds:
            secs = int(c.get("data", 0))
            if not (LOITER_MIN_SECONDS <= secs <= LOITER_MAX_SECONDS):
                raise ValueError(f"loiter time must be {LOITER_MIN_SECONDS}.."
                                 f"{LOITER_MAX_SECONDS}s, got {secs}")

    excludes = [f for f in profile.get("filters", []) if f.get("type") == "excludeArea"]
    if excludes and app not in APPS_WITH_EXCLUSIONS:
        raise ValueError(f"AXIS {app} does not support exclusion zones")
    if len(excludes) > MAX_EXCLUDE_ZONES:
        raise ValueError(f"at most {MAX_EXCLUDE_ZONES} exclusion zones, got {len(excludes)}")
    for ex in excludes:
        en = len(ex.get("data") or [])
        if not (EXCLUDE_MIN_VERTS <= en <= EXCLUDE_MAX_VERTS):
            raise ValueError(f"exclusion zone needs {EXCLUDE_MIN_VERTS}..{EXCLUDE_MAX_VERTS} "
                             f"vertices, got {en}")
    return True


# ---------------------------------------------------------------- config surgery

def profile_count(config):
    return len(config.get("data", {}).get("profiles", []) if isinstance(config, dict) else [])


def insert_or_replace_profile(config, profile, app, replace_by_name=True):
    """Return a NEW full config with `profile` inserted or replacing an existing
    one. Does not mutate the input. Match order: by uid (edit-in-place, so renames
    still target the right profile), then optionally by name, else append."""
    if not isinstance(config, dict):
        raise ValueError("config must be the dict returned by get_config()")
    new_config = json.loads(json.dumps(config))
    data = new_config.setdefault("data", {})
    profiles = data.setdefault("profiles", [])

    uid = profile.get("uid")
    if uid is not None:
        for i, existing in enumerate(profiles):
            if existing.get("uid") == uid:
                profiles[i] = profile
                return new_config

    if uid is None:
        profile = {**profile, "uid": _next_uid(new_config)}

    if replace_by_name:
        for i, existing in enumerate(profiles):
            if existing.get("name") == profile.get("name"):
                profile = {**profile, "uid": existing.get("uid", profile["uid"])}
                profiles[i] = profile
                return new_config

    if len(profiles) + 1 > MAX_PROFILES:
        raise ValueError(f"AXIS {app} holds at most {MAX_PROFILES} profiles per camera "
                         f"and already has {len(profiles)}")
    profiles.append(profile)
    return new_config


def remove_profile(config, uid):
    """Return a NEW full config without the profile carrying `uid`. A missing uid
    is a no-op -- the caller's intent ("this must not be on the camera") already
    holds."""
    if not isinstance(config, dict):
        raise ValueError("config must be the dict returned by get_config()")
    new_config = json.loads(json.dumps(config))
    data = new_config.setdefault("data", {})
    data["profiles"] = [p for p in data.get("profiles", []) if p.get("uid") != uid]
    return new_config


def parse_profiles(config, app, capabilities=None):
    """Full config -> [{name, uid, kind, points(frac), duration, direction,
    exclusions(frac), is_default}]. The inverse of the builders.

    `capabilities` (optional) enables is_default -- see is_factory_default."""
    out = []
    for p in config.get("data", {}).get("profiles", []):
        trig = (p.get("triggers") or [{}])[0]
        pts = guard_to_frac(trig.get("data") or [])
        duration = 0
        for c in trig.get("conditions") or []:
            if c.get("type") == "individual" and c.get("active"):
                try:
                    duration = int(c.get("data", 0))
                except (TypeError, ValueError):
                    duration = 0
        excl = [guard_to_frac(f.get("data") or [])
                for f in p.get("filters", []) if f.get("type") == "excludeArea"]
        out.append({
            "name": p.get("name", ""),
            "uid": p.get("uid"),
            "kind": APP_TO_KIND.get(app, "intrusion"),
            "points": pts,
            "duration": duration,
            "direction": trig.get("alarmDirection") if trig.get("type") == "fence" else None,
            "exclusions": excl,
            "is_default": is_factory_default(p, capabilities) if capabilities else False,
        })
    return out
