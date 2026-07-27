"""Vendor abstraction for the analytics writer -- one interface, both vendors.

The GUI works against a VendorAdapter and a vendor-neutral Scenario, so it never has
to branch on Axis-vs-Hik. Each adapter translates the neutral form to/from its native
config (AOA JSON via aoa_config, Hik ISAPI XML via hik_config).

Neutral coordinate space: (x, y) fractions in [0,1], ORIGIN TOP-LEFT (like image
pixels / img_w, img_h). Each adapter converts to its native space:
    Axis AOA:  ax = 2*fx - 1,          ay = 1 - 2*fy        ([-1,1], y-up)
    Hik ISAPI: hx = round(fx*1000),    hy = round((1-fy)*1000)   (0..1000, y-up + flip)

Capabilities differ by vendor and are advertised so the GUI can enable/disable
features (e.g. Axis supports exclusion zones + delete; the tested Hik thermal firmware
supports neither).
"""

from dataclasses import dataclass, field

import aoa_config
import camera_engine
import hik_config


@dataclass
class Scenario:
    """Vendor-neutral analytics scenario. Points are [0,1] top-left fractions."""
    name: str
    kind: str                       # "intrusion" | "line" | "loiter"
    points: list                    # [(fx, fy), ...]
    classes: tuple = ("human",)     # subset of ("human", "vehicle")
    duration: int = 0               # seconds (loiter / dwell)
    direction: str = None           # line only: "leftToRight" | "rightToLeft" (Axis)
    exclusions: list = field(default_factory=list)  # [[(fx,fy),...], ...] (Axis)
    native_id: object = None        # vendor id (AOA scenario id / Hik ruleName), for edit
    min_size: tuple = None          # (fx, fy, fw, fh) fraction rect -- smallest object
    max_size: tuple = None          # (fx, fy, fw, fh) fraction rect -- largest object
    perspective: list = None        # Axis calibration bars: [{"height": cm, "points": [(fx,fy),(fx,fy)]}]


@dataclass
class Capabilities:
    kinds: tuple                    # which Scenario.kind values are writable
    classes: tuple                  # supported detection classes
    multi_class: bool               # can one scenario target human AND vehicle
    exclusions: bool                # supports exclusion zones
    direction: bool                 # supports line-crossing direction
    can_delete: bool                # can remove a scenario via API
    perspective: bool = False       # supports perspective calibration bars
    size_boxes: bool = False        # supports positioned min/max object-size boxes
    notes: str = ""


# ---------------------------------------------------------------- Axis

class AxisAdapter:
    vendor = "Axis"
    capabilities = Capabilities(
        kinds=("intrusion", "line", "loiter"), classes=("human", "vehicle"),
        multi_class=True, exclusions=True, direction=True, can_delete=True, perspective=True,
        notes="AOA: full config replace -- add/edit/delete all supported.")

    def __init__(self, ip, port, user, password, **_):
        self.client = aoa_config.AOAClient(ip, user, password, port)

    def fetch_snapshot(self):
        return self.client.fetch_snapshot()

    def read_scenarios(self):
        cfg = self.client.get_config()
        persp_defs = {p.get("id"): p for p in cfg.get("data", {}).get("perspectives", [])}
        out = []
        for s in cfg.get("data", {}).get("scenarios", []):
            trig = (s.get("triggers") or [{}])[0]
            verts = [(( ax + 1) / 2.0, (1 - ay) / 2.0) for ax, ay in trig.get("vertices", [])]
            conds = trig.get("conditions") or []
            is_loiter = any(c.get("type") == "individualTimeInArea" for c in conds)
            kind = "line" if s.get("type") == "fence" else ("loiter" if is_loiter else "intrusion")
            classes = tuple(c.get("type") for c in s.get("objectClassifications", []))
            excl = [[((ax + 1) / 2.0, (1 - ay) / 2.0) for ax, ay in f.get("vertices", [])]
                    for f in s.get("filters", []) if f.get("type") == "excludeArea"]
            dur = 0
            for c in conds:
                for d in c.get("data", []):
                    if d.get("time"):
                        dur = int(d["time"])
                        break
            direction = trig.get("alarmDirection") if s.get("type") == "fence" else None
            min_size = self._axis_min_size(s.get("filters", []))
            persp = None
            pids = s.get("perspectives") or []
            if pids and pids[0] in persp_defs:
                persp = [{"height": b.get("height"),
                          "points": [((ax + 1) / 2.0, (1 - ay) / 2.0) for ax, ay in b.get("points", [])]}
                         for b in persp_defs[pids[0]].get("bars", [])]
            out.append(Scenario(name=s.get("name", ""), kind=kind, points=verts,
                                classes=classes or ("human",), duration=dur, direction=direction,
                                exclusions=excl, native_id=s.get("id"), min_size=min_size,
                                perspective=persp))
        return out

    @staticmethod
    def _axis_min_size(filters):
        """AOA minimum object size as a reference box. sizePercentage is width%/height%
        with NO position (unlike Hik's positioned boxes), so it's anchored bottom-left
        purely as a size legend. sizePerspective (real-world cm) can't be scaled without
        perspective calibration, so it's skipped here."""
        for f in filters:
            if f.get("type") == "sizePercentage":
                w, h = f.get("width", 0) / 100.0, f.get("height", 0) / 100.0
                if w > 0 and h > 0:
                    return (0.02, max(0.0, 0.97 - h), w, h)
        return None

    @staticmethod
    def _to_aoa(points):
        return [(2 * fx - 1, 1 - 2 * fy) for (fx, fy) in points]

    def apply_scenario(self, sc, backup_dir):
        """Create or edit-in-place. If sc.native_id matches an existing scenario, patch
        it (preserving perspective/presets/filters); otherwise build a new one. If
        sc.perspective is set, its calibration bars are written to the top-level
        perspectives and linked to the scenario (updating the existing one when editing)."""
        _require(self.capabilities, sc)
        verts = self._to_aoa(sc.points)
        excl = [self._to_aoa(z) for z in sc.exclusions] or None

        orig = None
        if sc.native_id is not None:
            current = self.client.get_config()
            orig = next((s for s in current.get("data", {}).get("scenarios", [])
                         if s.get("id") == sc.native_id), None)

        if orig is not None:
            loiter = sc.duration if sc.kind == "loiter" else None
            direction = sc.direction if sc.kind == "line" else None
            scenario = aoa_config.update_scenario_geometry(orig, verts, sc.classes, excl, loiter, direction)
            scenario["name"] = sc.name
        elif sc.kind == "line":
            scenario = aoa_config.build_line_crossing(sc.name, verts, classes=sc.classes,
                                                      alarm_direction=sc.direction or "leftToRight")
        elif sc.kind == "loiter":
            scenario = aoa_config.build_loiter(sc.name, verts, sc.duration or 1, classes=sc.classes)
        else:
            scenario = aoa_config.build_intrusion(sc.name, verts, classes=sc.classes)
        if excl and sc.kind != "line":
            aoa_config.add_exclude_zones(scenario, excl)

        if not sc.perspective:
            return self.client.apply_scenario(scenario, backup_dir)

        # Perspective bars: write to top-level data.perspectives + link the scenario.
        # Reuse the scenario's existing perspective id on edit so we don't pile up dupes.
        bars_norm = [{"height": b["height"], "points": self._to_aoa(b["points"])}
                     for b in sc.perspective]
        pid = (orig.get("perspectives") or [None])[0] if orig else None
        persp = aoa_config.build_perspective(bars_norm, perspective_id=pid)
        backup_path, current = self.client.backup_config(backup_dir)
        cfg, pid = aoa_config.insert_or_replace_perspective(current, persp)
        scenario["perspectives"] = [pid]
        cfg = aoa_config.insert_or_replace_scenario(cfg, scenario)
        self.client.set_config(cfg)
        return backup_path, self.client.get_config()


# ---------------------------------------------------------------- Hik

class HikAdapter:
    vendor = "Hikvision"
    capabilities = Capabilities(
        kinds=("intrusion", "line"), classes=("human", "vehicle"),
        multi_class=True, exclusions=False, direction=True, can_delete=False,
        size_boxes=True,
        notes="behaviorRule PUT is upsert-only on tested DS-2TD firmware: add/edit "
              "yes, delete no (use web UI). Intrusion (fieldDetection) + line crossing "
              "(lineDetection) supported; loiter/region not yet templated.")

    def __init__(self, ip, port, user, password, channel=2, **_):
        self.client = hik_config.HikClient(ip, user, password, port, channel=channel)
        self._axis_snap = None  # Hik snapshots come from camera_engine; see fetch_snapshot

    def fetch_snapshot(self):
        import camera_engine
        h = camera_engine.HikvisionHandler(self.client.ip, "", "")
        h.auth_strategies = self.client.auth_strategies
        img, url, err, auth_rej = h.fetch_snapshot(self.client.session, self.client.port)
        if img is None:
            raise hik_config.HikError(f"snapshot failed at {url}: {err}")
        return img

    def read_scenarios(self):
        import xml.etree.ElementTree as ET
        xml = self.client.get_behavior_rule()
        root = ET.fromstring(xml)
        out = []
        for ri in root.findall(".//ns:RuleInfo", hik_config.NS):
            name = _text(ri, "ns:ruleName")
            etype = _text(ri, "ns:eventType") or ""
            kind = "line" if "line" in etype.lower() else "intrusion"
            pts = [(int(_text(c, "ns:positionX")) / 1000.0, 1 - int(_text(c, "ns:positionY")) / 1000.0)
                   for c in ri.findall(".//ns:RegionCoordinates", hik_config.NS)]
            target = _text(ri, ".//ns:detectionTarget") or "human"
            dur = _text(ri, ".//ns:durationTime") or "0"
            direction = _HIK_DIR_TO_NEUTRAL.get(_text(ri, ".//ns:directionSensitivity")) if kind == "line" else None
            out.append(Scenario(name=name, kind=kind, points=pts,
                                classes=_hik_target_to_classes(target), direction=direction,
                                duration=int(dur) if dur.isdigit() else 0, native_id=name,
                                min_size=_size_rect(ri, "MinObjectSize"),
                                max_size=_size_rect(ri, "MaxObjectSize")))
        return out

    def apply_scenario(self, sc, backup_dir):
        """Add or edit-in-place (upsert by ruleName). Cannot delete on this firmware.
        Editing patches the existing rule in place so its SizeFilter (min/max object
        size) and other settings are preserved, not rebuilt away."""
        _require(self.capabilities, sc)
        region_hik = [(round(fx * 1000), round((1 - fy) * 1000)) for (fx, fy) in sc.points]
        target = _classes_to_hik_target(sc.classes)
        direction = _NEUTRAL_DIR_TO_HIK.get(sc.direction, "left-right")
        # Min/max size boxes: neutral fraction rect -> Hik 0..1000 (x,y,w,h) with the
        # region Y-flip (inverse of _size_rect). Both must be set to write a SizeFilter.
        min_box = self._frac_rect_to_hik(sc.min_size)
        max_box = self._frac_rect_to_hik(sc.max_size)

        backup_path, current = self.client.backup_behavior_rule(backup_dir)
        patched = hik_config.patch_rule_geometry(
            current, sc.name, region_hik, target=target, duration=sc.duration or None,
            min_box=min_box, max_box=max_box,
            direction=direction if sc.kind == "line" else None)
        if patched is not None:                       # edit-in-place: preserves other settings
            self.client.put_behavior_rule(patched)
            return backup_path, self.client.get_behavior_rule()

        if sc.kind == "line":
            rule = hik_config.build_line_crossing_rule(sc.name, region_hik, target=target,
                                                       direction=direction,
                                                       min_box=min_box, max_box=max_box)
        else:
            rule = hik_config.build_intrusion_rule(sc.name, region_hik, target=target,
                                                   duration=sc.duration or 1,
                                                   min_box=min_box, max_box=max_box)
        new_xml = hik_config.insert_or_replace_rule(current, rule, replace_by_name=True)
        self.client.put_behavior_rule(new_xml)
        return backup_path, self.client.get_behavior_rule()

    @staticmethod
    def _frac_rect_to_hik(rect):
        """Neutral (fx,fy,fw,fh) top-left fraction -> Hik (x,y,w,h) in 0..1000 with the
        region Y-flip. Inverse of _size_rect. None -> None."""
        if not rect:
            return None
        fx, fy, fw, fh = rect
        return (round(fx * 1000), round((1 - fy - fh) * 1000), round(fw * 1000), round(fh * 1000))


# Neutral direction (Axis API values) <-> Hik lineDetection directionSensitivity.
_NEUTRAL_DIR_TO_HIK = {"leftToRight": "left-right", "rightToLeft": "right-left"}
_HIK_DIR_TO_NEUTRAL = {"left-right": "leftToRight", "right-left": "rightToLeft"}


def _classes_to_hik_target(classes):
    s = set(classes or ())
    if "human" in s and "vehicle" in s:
        return "human_vehicle"
    if "vehicle" in s:
        return "vehicle"
    return "human"


def _hik_target_to_classes(target):
    if target == "human_vehicle":
        return ("human", "vehicle")
    return (target,) if target in ("human", "vehicle") else ("human",)


def _text(el, path):
    node = el.find(path, hik_config.NS)
    return node.text if node is not None and node.text else ""


def _size_rect(rule_el, box_tag):
    """Parse a Hik SizeFilter box (MinObjectSize/MaxObjectSize) into a neutral
    (fx, fy, fw, fh) top-left fraction rect, or None. positionX/Y/width/height are in
    the same 0..1000 grid as RuleRegion, so we apply the same vertical flip the region
    coords use (camera_engine.py:306) -- the box spans rawY..rawY+h, which flips to a
    top-left corner at (x/1000, 1-(y+h)/1000). Confirmed against a real min+max
    SizeFilter (TESTZONE1 on 10.23.5.188:5020 ch2)."""
    box = rule_el.find(f".//ns:SizeFilter/ns:{box_tag}", hik_config.NS)
    if box is None:
        return None
    def g(tag):
        n = box.find(f"ns:{tag}", hik_config.NS)
        return int(n.text) if n is not None and (n.text or "").isdigit() else None
    x, y, w, h = g("positionX"), g("positionY"), g("width"), g("height")
    if None in (x, y, w, h):
        return None
    return (x / 1000.0, 1 - (y + h) / 1000.0, w / 1000.0, h / 1000.0)


def _require(caps, sc):
    """Guard a scenario against a vendor's capabilities before we try to write it."""
    if sc.kind not in caps.kinds:
        raise ValueError(f"{sc.kind!r} scenarios aren't supported on this camera "
                         f"(supported: {', '.join(caps.kinds)})")
    if len(sc.classes) > 1 and not caps.multi_class:
        raise ValueError("this camera can't target human AND vehicle in one scenario")
    if sc.exclusions and not caps.exclusions:
        raise ValueError("this camera doesn't support exclusion zones")
    bad = [c for c in sc.classes if c not in caps.classes]
    if bad:
        raise ValueError(f"unsupported detection class(es): {bad}")


# ---------------------------------------------------------------- factory

def make_adapter(vendor, ip, port, user, password, channel=None):
    import camera_engine
    v = camera_engine.classify_manufacturer(vendor)
    if v == "AXIS":
        return AxisAdapter(ip, port, user, password)
    if v == "HIKVISION":
        return HikAdapter(ip, port, user, password, channel=int(channel) if channel else 2)
    raise ValueError(f"unsupported vendor: {vendor!r}")


def capabilities_for(vendor):
    """Capabilities for a vendor WITHOUT connecting -- lets the UI gate features as
    soon as the user picks a manufacturer."""
    import camera_engine
    v = camera_engine.classify_manufacturer(vendor)
    if v == "AXIS":
        return AxisAdapter.capabilities
    if v == "HIKVISION":
        return HikAdapter.capabilities
    return None
