"""Hikvision ISAPI analytics WRITE layer -- the Hik counterpart to aoa_config.py.

Where Axis AOA is JSON over one control.cgi, Hikvision is XML over ISAPI. VCA rules
live in a per-channel document at:

    GET/PUT  /ISAPI/Intelligent/channels/{ch}/behaviorRule/1

The document is <BehaviorRule><RuleInfoList><RuleInfo>...</RuleInfo>...</RuleInfoList>.

IMPORTANT -- confirmed live on 10.23.5.188:5020 ch2 (DS-2TD4237-10/V2 thermal): on this
firmware PUT is an UPSERT, not a replace. It adds/updates RuleInfo by ruleId but does
NOT delete rules absent from the body; DELETE and collection PUT return notSupport. So
this API can ADD and EDIT-in-place rules but CANNOT delete them (at least on this thermal
model -- standard optical Hik cameras may replace on PUT; verify before relying on it).
Consequence: restore()/apply_rule cannot roll back an add or remove a rule here.

Coordinates are positionX/positionY in a 0..1000 space; the read path applies a
vertical flip to match the snapshot (camera_engine.py:306), mirrored here.

This module deliberately mirrors aoa_config.py's shape (client + builders + backup +
read-modify-write) so the GUI can drive either vendor behind a common interface.
"""

from datetime import datetime
from pathlib import Path
import xml.etree.ElementTree as ET

import requests
from requests.adapters import HTTPAdapter
from requests.auth import HTTPBasicAuth, HTTPDigestAuth
from urllib3.util.retry import Retry

import camera_engine

NS_URI = "http://www.std-cgi.com/ver20/XMLSchema"
NS = {"ns": NS_URI}
NET_TIMEOUT = getattr(camera_engine, "STRICT_TIMEOUT", (3.05, 5.0))

# Emit the default namespace with no prefix so PUT bodies match the camera's own format
# (otherwise ElementTree writes ns0: prefixes the firmware may reject).
ET.register_namespace("", NS_URI)

VALID_TARGETS = ("human", "vehicle")  # detectionTarget; sample uses a single value
REGION_MIN_VERTS = 3  # intrusion region polygon
LINE_VERTS = 2        # line-crossing tripwire


class HikError(RuntimeError):
    """Camera reached but reported a logical failure (ISAPI ResponseStatus)."""


class HikAuthError(HikError):
    """Every auth strategy rejected (401)."""


class HikClient:
    """One camera + one channel. Mirrors aoa_config.AOAClient."""

    def __init__(self, ip, user, password, port, channel=1, session=None):
        self.ip = ip
        self.port = str(port)
        self.channel = str(channel)
        self.auth_strategies = [HTTPDigestAuth(user, password), HTTPBasicAuth(user, password)]
        self.session = session or self._build_session()

    @staticmethod
    def _build_session():
        s = requests.Session()
        retries = Retry(total=2, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retries)
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        return s

    @property
    def behavior_url(self):
        return f"http://{self.ip}:{self.port}/ISAPI/Intelligent/channels/{self.channel}/behaviorRule/1"

    # ------------------------------------------------------------------ transport
    def _request(self, method, body=None):
        """digest->basic fallback. Returns response text on success; raises on failure.
        ISAPI reports errors in a ResponseStatus XML body even on some non-2xx codes."""
        headers = {"Content-Type": "application/xml"} if body is not None else {}
        saw_401 = False
        last = "no response"
        for auth in self.auth_strategies:
            try:
                r = self.session.request(method, self.behavior_url, auth=auth, data=body,
                                         headers=headers, timeout=NET_TIMEOUT, verify=False)
            except requests.exceptions.RequestException as e:
                last = f"{type(e).__name__}: {e}"
                continue
            if r.status_code == 401:
                saw_401 = True
                last = "HTTP 401"
                continue
            # Inspect ResponseStatus for a logical error even on 200.
            sub = _status_of(r.text)
            if r.status_code == 200 and (sub is None or sub in ("ok", "1")):
                return r.text
            last = f"HTTP {r.status_code}" + (f" ({sub})" if sub else f": {r.text[:200]}")
            if r.status_code != 401:
                raise HikError(last)
        if saw_401:
            raise HikAuthError("Hikvision rejected every credential (401). Writing VCA rules "
                               "needs an admin/operator account with config rights.")
        raise HikError(last)

    def get_behavior_rule(self):
        return self._request("GET")

    def put_behavior_rule(self, xml_text):
        return self._request("PUT", body=xml_text.encode("utf-8"))

    def backup_behavior_rule(self, backup_dir):
        backup_dir = Path(backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        xml = self.get_behavior_rule()
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        path = backup_dir / f"hik_behaviorRule_{self.ip.replace('.', '_')}_{self.port}_ch{self.channel}_{stamp}.xml"
        path.write_text(xml, encoding="utf-8")
        return path, xml

    def apply_rule(self, rule_el, backup_dir, replace_by_name=True):
        """Add or edit-in-place one RuleInfo: back up the channel's behaviorRule ->
        insert/update the rule in the RuleInfoList -> PUT -> re-read to verify. Returns
        (backup_path, verify_xml).

        NOTE: PUT is upsert on the tested firmware -- this ADDS a new rule or UPDATES an
        existing one (matched by name/id), but it does NOT delete other rules. It cannot
        be used to remove a rule."""
        backup_path, current = self.backup_behavior_rule(backup_dir)
        new_xml = insert_or_replace_rule(current, rule_el, replace_by_name=replace_by_name)
        self.put_behavior_rule(new_xml)
        verify = self.get_behavior_rule()
        return backup_path, verify

    def restore(self, xml_text):
        """PUT a previously backed-up behaviorRule document. WARNING: because PUT is
        upsert (not replace) on the tested firmware, this re-adds/updates the rules in
        the backup but does NOT remove rules created after the backup was taken -- it is
        not a true rollback for additions. Deleting a rule needs the camera web UI."""
        self.put_behavior_rule(xml_text)
        return self.get_behavior_rule()


# ---------------------------------------------------------------- rule builders
# Mirror aoa_config's builders (build_intrusion / build_line_crossing) but emit a
# Hik <RuleInfo> ElementTree node instead of a JSON scenario. Region coords are in
# 0..1000 (use pixel_to_hik to convert from canvas pixels). Templated on the real
# fieldDetection rule captured from 10.23.5.188:5020 ch2.


def _sub(parent, tag, text=None):
    e = ET.SubElement(parent, f"{{{NS_URI}}}{tag}")
    if text is not None:
        e.text = str(text)
    return e


def _region(rule_el, coords_hik):
    rr = _sub(rule_el, "RuleRegion")
    rcl = _sub(rr, "RegionCoordinatesList")
    for (hx, hy) in coords_hik:
        rc = _sub(rcl, "RegionCoordinates")
        _sub(rc, "positionX", int(hx))
        _sub(rc, "positionY", int(hy))


def build_intrusion_rule(name, region_hik, target="human", duration=1, sensitivity=50):
    """Intrusion (eventType=fieldDetection, ruleType=region). region_hik is a list of
    (x, y) in 0..1000, >=3 points. ruleId is assigned at insert time."""
    if target not in VALID_TARGETS:
        raise ValueError(f"target must be one of {VALID_TARGETS}, got {target!r}")
    if len(region_hik) < REGION_MIN_VERTS:
        raise ValueError(f"intrusion region needs >= {REGION_MIN_VERTS} points, got {len(region_hik)}")
    ri = ET.Element(f"{{{NS_URI}}}RuleInfo")
    _sub(ri, "ruleId", 0)  # placeholder; insert_or_replace_rule assigns the real id
    _sub(ri, "ruleName", name)
    _sub(ri, "enabled", "true")
    _sub(ri, "eventType", "fieldDetection")
    _sub(ri, "ruleType", "region")
    fdp = _sub(ri, "FieldDetectionParam")
    _sub(fdp, "durationTime", int(duration))
    _sub(fdp, "sensitivity", int(sensitivity))
    _sub(fdp, "detectionTarget", target)
    _region(ri, region_hik)
    return ri


def _rule_infolist(root):
    rilist = root.find("ns:RuleInfoList", NS)
    if rilist is None:
        raise HikError("behaviorRule document has no RuleInfoList")
    return rilist


def next_rule_id(root):
    ids = [int(r.find("ns:ruleId", NS).text) for r in root.findall(".//ns:RuleInfo", NS)
           if r.find("ns:ruleId", NS) is not None and (r.find("ns:ruleId", NS).text or "").isdigit()]
    return (max(ids) + 1) if ids else 1


def insert_or_replace_rule(xml_text, rule_el, replace_by_name=True):
    """Return a full behaviorRule XML string with `rule_el` inserted (or replacing a
    same-named RuleInfo). Assigns ruleId. Does not mutate inputs. Mirrors
    aoa_config.insert_or_replace_scenario."""
    root = ET.fromstring(xml_text)
    rilist = _rule_infolist(root)
    name_el = rule_el.find(f"{{{NS_URI}}}ruleName")
    new_name = name_el.text if name_el is not None else None

    if replace_by_name and new_name is not None:
        for existing in list(rilist.findall("ns:RuleInfo", NS)):
            ename = existing.find("ns:ruleName", NS)
            if ename is not None and ename.text == new_name:
                # keep the existing ruleId, drop the old node, insert the new one
                eid = existing.find("ns:ruleId", NS)
                rid = eid.text if eid is not None else str(next_rule_id(root))
                _set_rule_id(rule_el, rid)
                idx = list(rilist).index(existing)
                rilist.remove(existing)
                rilist.insert(idx, rule_el)
                return _serialize(root)

    _set_rule_id(rule_el, str(next_rule_id(root)))
    rilist.append(rule_el)
    return _serialize(root)


def _set_rule_id(rule_el, rid):
    el = rule_el.find(f"{{{NS_URI}}}ruleId")
    if el is None:
        el = ET.Element(f"{{{NS_URI}}}ruleId")
        rule_el.insert(0, el)
    el.text = str(rid)


def _serialize(root):
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")


# ---------------------------------------------------------------- helpers

def _status_of(xml_text):
    """Return the ISAPI subStatusCode (or statusCode) from a ResponseStatus body, or
    None if the body isn't a ResponseStatus."""
    if not xml_text or "ResponseStatus" not in xml_text:
        return None
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    for tag in ("subStatusCode", "statusCode"):
        el = root.find(f"ns:{tag}", NS)
        if el is not None and el.text:
            return el.text.strip()
    return None


def rule_summary(xml_text):
    """(count, [ruleName], [eventType]) for a BehaviorRule document -- for logging/verify."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return 0, [], []
    infos = root.findall(".//ns:RuleInfo", NS)
    names = [(i.find("ns:ruleName", NS).text if i.find("ns:ruleName", NS) is not None else "?") for i in infos]
    types = sorted({i.find("ns:eventType", NS).text for i in infos if i.find("ns:eventType", NS) is not None})
    return len(infos), names, types


def normalize(xml_text):
    """Whitespace-insensitive canonical form for comparing before/after a write."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return xml_text
    for el in root.iter():
        if el.text:
            el.text = el.text.strip()
        if el.tail:
            el.tail = el.tail.strip()
    return ET.tostring(root, encoding="unicode")


# ---------------------------------------------------------------- coordinates
# 0..1000 space with a vertical flip to match the snapshot (inverse of
# camera_engine.py:306). Kept here so the GUI's Hik path mirrors AOA's transforms.

def hik_to_pixel(hx, hy, img_w, img_h):
    px = int((hx / 1000.0) * img_w)
    py = int((hy / 1000.0) * img_h)
    return (px, img_h - 1 - py)


def pixel_to_hik(px, py, img_w, img_h):
    hx = round((px / img_w) * 1000.0)
    hy = round(((img_h - 1 - py) / img_h) * 1000.0)
    return (max(0, min(1000, hx)), max(0, min(1000, hy)))
