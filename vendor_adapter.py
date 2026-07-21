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


@dataclass
class Capabilities:
    kinds: tuple                    # which Scenario.kind values are writable
    classes: tuple                  # supported detection classes
    multi_class: bool               # can one scenario target human AND vehicle
    exclusions: bool                # supports exclusion zones
    direction: bool                 # supports line-crossing direction
    can_delete: bool                # can remove a scenario via API
    notes: str = ""


# ---------------------------------------------------------------- Axis

class AxisAdapter:
    vendor = "Axis"
    capabilities = Capabilities(
        kinds=("intrusion", "line", "loiter"), classes=("human", "vehicle"),
        multi_class=True, exclusions=True, direction=True, can_delete=True,
        notes="AOA: full config replace -- add/edit/delete all supported.")

    def __init__(self, ip, port, user, password, **_):
        self.client = aoa_config.AOAClient(ip, user, password, port)

    def fetch_snapshot(self):
        return self.client.fetch_snapshot()

    def read_scenarios(self):
        cfg = self.client.get_config()
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
            out.append(Scenario(name=s.get("name", ""), kind=kind, points=verts,
                                classes=classes or ("human",), duration=dur, direction=direction,
                                exclusions=excl, native_id=s.get("id")))
        return out

    @staticmethod
    def _to_aoa(points):
        return [(2 * fx - 1, 1 - 2 * fy) for (fx, fy) in points]

    def apply_scenario(self, sc, backup_dir):
        """Create or edit-in-place. If sc.native_id matches an existing scenario, patch
        it (preserving perspective/presets/filters); otherwise build a new one."""
        _require(self.capabilities, sc)
        verts = self._to_aoa(sc.points)
        excl = [self._to_aoa(z) for z in sc.exclusions] or None

        if sc.native_id is not None:
            current = self.client.get_config()
            orig = next((s for s in current.get("data", {}).get("scenarios", [])
                         if s.get("id") == sc.native_id), None)
            if orig is not None:
                loiter = sc.duration if sc.kind == "loiter" else None
                direction = sc.direction if sc.kind == "line" else None
                scenario = aoa_config.update_scenario_geometry(orig, verts, sc.classes, excl, loiter, direction)
                scenario["name"] = sc.name
                return self.client.apply_scenario(scenario, backup_dir)

        if sc.kind == "line":
            scenario = aoa_config.build_line_crossing(sc.name, verts, classes=sc.classes,
                                                      alarm_direction=sc.direction or "leftToRight")
        elif sc.kind == "loiter":
            scenario = aoa_config.build_loiter(sc.name, verts, sc.duration or 1, classes=sc.classes)
        else:
            scenario = aoa_config.build_intrusion(sc.name, verts, classes=sc.classes)
        if excl and sc.kind != "line":
            aoa_config.add_exclude_zones(scenario, excl)
        return self.client.apply_scenario(scenario, backup_dir)


# ---------------------------------------------------------------- Hik

class HikAdapter:
    vendor = "Hikvision"
    capabilities = Capabilities(
        kinds=("intrusion",), classes=("human", "vehicle"),
        multi_class=False, exclusions=False, direction=False, can_delete=False,
        notes="behaviorRule PUT is upsert-only on tested DS-2TD firmware: add/edit "
              "yes, delete no (use web UI). Line/loiter not yet templated.")

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
            out.append(Scenario(name=name, kind=kind, points=pts,
                                classes=(target,) if target in ("human", "vehicle") else ("human",),
                                duration=int(dur) if dur.isdigit() else 0, native_id=name))
        return out

    def apply_scenario(self, sc, backup_dir):
        """Add or edit-in-place (upsert by ruleName). Cannot delete on this firmware."""
        _require(self.capabilities, sc)
        region_hik = [(round(fx * 1000), round((1 - fy) * 1000)) for (fx, fy) in sc.points]
        target = sc.classes[0] if sc.classes else "human"
        rule = hik_config.build_intrusion_rule(sc.name, region_hik, target=target,
                                               duration=sc.duration or 1)
        return self.client.apply_rule(rule, backup_dir, replace_by_name=True)


def _text(el, path):
    node = el.find(path, hik_config.NS)
    return node.text if node is not None and node.text else ""


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
