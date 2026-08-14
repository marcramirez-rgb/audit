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

import requests

import aoa_config
import camera_engine
import hik_config


@dataclass(frozen=True)
class AxisLinePair:
    """native_id for a BIDIRECTIONAL Axis line: the ids of the two one-way fence
    scenarios that together make the rule.

    Deliberately not a tuple/namedtuple -- the GUI uses `isinstance(native_id, tuple)`
    to recognise a Hikvision (channel, sid, ruleId) address, and this must not answer
    to that test."""
    lr_id: int
    rl_id: int


@dataclass
class Scenario:
    """Vendor-neutral analytics scenario. Points are [0,1] top-left fractions."""
    name: str
    kind: str                       # "intrusion" | "line" | "loiter"
    points: list                    # [(fx, fy), ...]
    classes: tuple = ("human",)     # subset of ("human", "vehicle")
    duration: int = 0               # seconds (loiter / dwell)
    direction: str = None           # line only: "leftToRight" | "rightToLeft" | "both"
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
    intrusion_duration: bool = False  # intrusion rules carry a dwell/duration (Hik durationTime)
    # True when ONE rule can watch both ways across the line. False means the vendor
    # only has one-way fences and a bidirectional line costs two scenarios -- the GUI
    # says so, and caps the name to leave room for the -LR/-RL suffixes.
    native_bidirectional: bool = False
    notes: str = ""


# ---------------------------------------------------------------- Axis

class AxisAdapter:
    vendor = "Axis"
    capabilities = Capabilities(
        kinds=("intrusion", "line", "loiter"), classes=("human", "vehicle"),
        multi_class=True, exclusions=True, direction=True, can_delete=True, perspective=True,
        native_bidirectional=False,
        notes="AOA: full config replace -- add/edit/delete all supported. A fence is "
              "one-way, so a bidirectional line is written as a -LR/-RL scenario pair.")

    # Suffixes for the two halves of a bidirectional line. Kept short because AOA caps
    # scenario names at 15 chars, which is also why the base name is capped at 12.
    BIDIR_SUFFIX = {"leftToRight": "-LR", "rightToLeft": "-RL"}
    BIDIR_BASE_MAX = aoa_config.MAX_NAME_LEN - 3

    # Movement between the two readings bracketing a capture that means "the camera
    # moved on us": pan deg, tilt deg, zoom (Axis zoom is a 1..9999 lens position).
    PTZ_DRIFT_TOLERANCE = (1.0, 1.0, 50.0)

    def __init__(self, ip, port, user, password, channel=None, **_):
        # channel selects a physical sensor on a multi-sensor Axis unit (e.g. P3747,
        # verified against a real 10.23.135.24:5010). None (the default, ordinary
        # single-sensor cameras) preserves the exact prior single-image behavior.
        self.device_id = int(channel) if channel else None
        self.client = aoa_config.AOAClient(ip, user, password, port, channel_idx=self.device_id)

    def fetch_snapshot(self, log=None):
        """Snapshot at the PRESET the analytics are bound to -- not wherever the
        dome currently points. LVT's Axis units are Q6135-LE PTZ domes and their AOA
        scenarios bind to a saved preset (fleet-wide: 143 of 149 scenarios in local
        backups carry one, usually preset 2 "Scene1"). Zones are drawn on this frame
        and pushed as fractions of it, so a patrol-position frame would put the
        pushed polygon in the wrong place on the scene the rule actually evaluates:
        false alarms or missed intrusions. Goto the preset, wait for the lens to
        stop, capture, then confirm it didn't move during the capture. Fixed Axis
        models have no PTZ to query and flow through unchanged."""
        log = log or (lambda _m: None)
        handler = self.client._axis
        session, port = self.client.session, self.client.port
        camera = self.device_id or 1

        preset = self._analytics_preset()
        if preset is not None:
            if handler.goto_preset(session, port, preset, camera=camera):
                log(f"[*] PTZ moved to AOA preset {preset} and settled.")
            else:
                log(f"[!] Could not park the camera at AOA preset {preset} -- it may be "
                    f"mid-patrol. Zones drawn on this frame may not line up; re-fetch.")

        before = handler.ptz_position(session, port, camera=camera)
        img = self.client.fetch_snapshot()
        delta = camera_engine.ptz_delta(before, handler.ptz_position(session, port, camera=camera))
        if delta and any(d > t for d, t in zip(delta, self.PTZ_DRIFT_TOLERANCE)):
            log(f"[!] Camera MOVED during capture (pan {delta[0]:.1f}°, tilt {delta[1]:.1f}°, "
                f"zoom {delta[2]:.0f}) -- likely mid-patrol. Fetch again before drawing.")
        return img

    def _analytics_preset(self):
        """PTZ preset this camera's AOA scenarios are bound to, or None when the
        unit is fixed / nothing is bound. Warns nothing here -- callers log."""
        try:
            presets = camera_engine.aoa_scenario_presets(self.client.get_config())
        except Exception:
            return None
        return presets[0] if presets else None

    def read_scenarios(self):
        cfg = self.client.get_config()
        persp_defs = {p.get("id"): p for p in cfg.get("data", {}).get("perspectives", [])}
        out = []
        for s in cfg.get("data", {}).get("scenarios", []):
            # On a multi-sensor unit, only show/edit scenarios bound to the selected
            # sensor -- every scenario carries a "devices" list (required key, even on
            # single-sensor cameras where it's just [{"id": 1}]), so when no sensor is
            # selected (self.device_id is None) nothing is filtered here at all.
            if self.device_id is not None:
                devs = [d.get("id") for d in s.get("devices", []) if isinstance(d, dict)]
                if devs and self.device_id not in devs:
                    continue
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
        return self._fold_line_pairs(out)

    @classmethod
    def _fold_line_pairs(cls, scenarios):
        """Fold each -LR/-RL fence pair back into ONE bidirectional Scenario.

        A bidirectional line lives on the camera as two one-way scenarios (see
        _apply_line_pair). Handing those back as two separate rules is exactly the
        kind of thing that makes an operator misread what's configured, so they are
        rejoined here: the pair shows up as a single rule named after the shared base,
        with direction 'both', and edits/pushes go to both halves together.

        Pairing is deliberately strict -- matching base name, identical line, and each
        half's alarmDirection agreeing with its own suffix. Anything touched in the
        camera's web UI since (a moved endpoint, a flipped direction) falls out of the
        match and is shown as the two independent rules it has become, rather than
        being silently edited as a pair."""
        by_base = {}   # base name -> {"leftToRight": Scenario, "rightToLeft": Scenario}
        for s in scenarios:
            if s.kind != "line" or s.direction not in cls.BIDIR_SUFFIX:
                continue
            suffix = cls.BIDIR_SUFFIX[s.direction]
            if s.name.endswith(suffix) and len(s.name) > len(suffix):
                by_base.setdefault(s.name[:-len(suffix)], {})[s.direction] = s

        merged, consumed = {}, set()
        for base, halves in by_base.items():
            lr, rl = halves.get("leftToRight"), halves.get("rightToLeft")
            if lr is None or rl is None:
                continue
            if _points_equal(lr.points, rl.points):
                merged[id(lr)] = Scenario(
                    name=base, kind="line", points=lr.points, classes=lr.classes,
                    duration=lr.duration, direction="both",
                    native_id=AxisLinePair(lr.native_id, rl.native_id),
                    perspective=lr.perspective, min_size=lr.min_size, max_size=lr.max_size)
                consumed.add(id(rl))

        # Rebuild in read order, with each pair sitting where its -LR half was.
        return [merged.get(id(s), s) for s in scenarios if id(s) not in consumed]

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
        # Bidirectional lines (and any edit of one) need two scenarios written together,
        # so they take their own path.
        if sc.kind == "line" and (sc.direction == "both" or isinstance(sc.native_id, AxisLinePair)):
            return self._apply_line_pair(sc, backup_dir)
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
                                                      alarm_direction=sc.direction or "leftToRight",
                                                      device_id=self.device_id or 1)
        elif sc.kind == "loiter":
            scenario = aoa_config.build_loiter(sc.name, verts, sc.duration or 1, classes=sc.classes,
                                               device_id=self.device_id or 1)
        else:
            scenario = aoa_config.build_intrusion(sc.name, verts, classes=sc.classes,
                                                  device_id=self.device_id or 1)
        if excl and sc.kind != "line":
            aoa_config.add_exclude_zones(scenario, excl)

        if orig is None:
            # New scenario on a PTZ dome: bind it to the same preset the camera's
            # other scenarios use. A scenario with an empty `presets` list isn't tied
            # to the guard position, so on a patrolling unit it would be evaluated
            # against whatever the lens sees. Editing preserves the original binding
            # via update_scenario_geometry, so this only applies to creates.
            preset = self._analytics_preset()
            if preset is not None:
                scenario["presets"] = [preset]

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

    # ---- bidirectional line crossing (Axis has no both-ways fence)

    def _apply_line_pair(self, sc, backup_dir):
        """Write a bidirectional line as the PAIR of one-way fences AOA actually
        supports -- both halves in a single config write, so the camera never sits in a
        half-applied state.

        The halves are named <base>-LR and <base>-RL and share one line; read_scenarios
        folds them back into a single rule, so from the GUI a bidirectional line stays
        one thing. Both direction changes are handled here too:
          one-way -> both: the existing scenario becomes the -LR half (keeping its id,
              presets and filters) and a new -RL half is added;
          both -> one-way: the surviving half is renamed back to the plain base name
              and the other is deleted.
        """
        base = sc.name
        if len(base) > self.BIDIR_BASE_MAX:
            raise ValueError(
                f"a bidirectional line needs a name of at most {self.BIDIR_BASE_MAX} "
                f"characters -- it is written as two scenarios, '{base}-LR' and "
                f"'{base}-RL', and AOA caps a scenario name at {aoa_config.MAX_NAME_LEN}")

        verts = self._to_aoa(sc.points)
        backup_path, current = self.client.backup_config(backup_dir)
        existing = {s.get("id"): s for s in current.get("data", {}).get("scenarios", [])}

        # Which camera-side scenario (if any) already backs each direction.
        if isinstance(sc.native_id, AxisLinePair):
            ids = {"leftToRight": sc.native_id.lr_id, "rightToLeft": sc.native_id.rl_id}
        elif isinstance(sc.native_id, int):
            # Editing a one-way line into a bidirectional one: keep its id for the half
            # it already points the right way, so presets/filters survive.
            orig_dir = ((existing.get(sc.native_id, {}).get("triggers") or [{}])[0]
                        .get("alarmDirection") or "leftToRight")
            ids = {orig_dir: sc.native_id}
        else:
            ids = {}

        wanted = (["leftToRight", "rightToLeft"] if sc.direction == "both" else [sc.direction])
        drop = [ids[d] for d in ids if d not in wanted and ids[d] in existing]

        # A camera that is already full rejects the whole write, so check before adding.
        adds = len([d for d in wanted if ids.get(d) not in existing])
        if aoa_config.scenario_count(current) - len(drop) + adds > aoa_config.MAX_SCENARIOS:
            raise ValueError(
                f"this camera already holds {aoa_config.scenario_count(current)} of "
                f"{aoa_config.MAX_SCENARIOS} scenarios -- a bidirectional line needs two "
                f"free slots. Remove a scenario, or use a single-direction line.")

        cfg = current
        for direction in wanted:
            # A single-direction result drops the suffix entirely: the rule is no longer
            # half of a pair and shouldn't keep reading like one.
            name = f"{base}{self.BIDIR_SUFFIX[direction]}" if sc.direction == "both" else base
            orig = existing.get(ids.get(direction))
            if orig is not None:
                scenario = aoa_config.update_scenario_geometry(
                    orig, verts, sc.classes, None, None, direction)
                scenario["name"] = name
            else:
                scenario = aoa_config.build_line_crossing(
                    name, verts, classes=sc.classes, alarm_direction=direction,
                    device_id=self.device_id or 1)
                preset = self._analytics_preset()
                if preset is not None:
                    scenario["presets"] = [preset]
            aoa_config.validate_scenario(scenario)
            cfg = aoa_config.insert_or_replace_scenario(cfg, scenario, replace_by_name=True)

        for dead_id in drop:
            cfg = aoa_config.remove_scenario(cfg, dead_id)

        self.client.set_config(cfg)
        return backup_path, self.client.get_config()


# ---------------------------------------------------------------- Hik

class HikAdapter:
    vendor = "Hikvision"
    capabilities = Capabilities(
        kinds=("intrusion", "line"), classes=("human", "vehicle"),
        multi_class=True, exclusions=False, direction=True, can_delete=False,
        size_boxes=True, intrusion_duration=True, native_bidirectional=True,
        notes="behaviorRule PUT is upsert-only on tested DS-2TD firmware: add/edit "
              "yes, delete no (use web UI). Intrusion (fieldDetection) + line crossing "
              "(lineDetection) supported; loiter/region not yet templated.")

    def __init__(self, ip, port, user, password, channel=2, **_):
        self.client = hik_config.HikClient(ip, user, password, port, channel=channel)
        self._axis_snap = None  # Hik snapshots come from camera_engine; see fetch_snapshot

    def fetch_snapshot(self, log=None):
        """Snapshot of the ANALYTICS SCENE view -- not wherever the lens happens to
        be pointing. PTZ Hik domes patrol: zones are drawn on this frame and their
        coordinates bind to the rule's scene, so a mid-patrol frame would make the
        operator draw (and later push) a polygon that is misaligned on the real
        scene view -- mis-alerts / missed intrusions once the camera returns home.
        Mirrors the audit engine: goto the rule-bearing scene, let the PTZ settle,
        capture, then confirm the camera didn't move DURING capture (a patrol tick
        between goto and the image GET would silently poison the frame). Fixed
        cameras reject the goto and the position query, and flow through unchanged."""
        log = log or (lambda _m: None)
        h = camera_engine.HikvisionHandler(self.client.ip, "", "")
        h.auth_strategies = self.client.auth_strategies

        selected = self.client.channel
        target = (self._first_rule_doc([selected, "1" if selected == "2" else "2"])
                  or (selected, 1))
        ch, sid = target
        if h.reposition_for_scene(self.client.session, self.client.port, ch, sid):
            log(f"[*] PTZ repositioned to analytics scene {sid} (ch{ch}) and settled.")

        pos_before = self._ptz_status()
        img, url, err, auth_rej = h.fetch_snapshot(self.client.session, self.client.port)
        if img is None:
            raise hik_config.HikError(f"snapshot failed at {url}: {err}")
        drift = self._ptz_drift(pos_before, self._ptz_status())
        if drift:
            log(f"[!] Camera MOVED during capture ({drift}) -- likely mid-patrol. "
                f"Fetch the snapshot again before drawing, or zones will be misaligned.")
        return img

    def _ptz_status(self):
        """Current (pan, tilt, zoom) from the PTZ status endpoint, or None when the
        unit has no queryable PTZ (fixed cameras) or any value is missing. ISAPI
        reports azimuth/elevation/zoom as value x10."""
        url = f"http://{self.client.ip}:{self.client.port}/ISAPI/PTZCtrl/channels/1/status"
        for auth in self.client.auth_strategies:
            try:
                r = self.client.session.get(url, auth=auth, timeout=hik_config.NET_TIMEOUT,
                                            verify=False)
            except requests.exceptions.RequestException:
                return None
            if r.status_code == 401:
                continue
            if r.status_code != 200:
                return None
            import xml.etree.ElementTree as ET
            try:
                root = ET.fromstring(r.text)
            except ET.ParseError:
                return None
            vals = []
            for tag in ("azimuth", "elevation", "absoluteZoom"):
                el = root.find(f".//{{*}}{tag}")
                if el is None or not el.text:
                    return None
                vals.append(float(el.text) / 10.0)
            return tuple(vals)
        return None

    # Movement between the two readings bracketing a capture that means "the camera
    # moved on us": pan deg, tilt deg, zoom (Hik reports zoom as a ratio).
    PTZ_DRIFT_TOLERANCE = (1.0, 1.0, 0.5)

    @classmethod
    def _ptz_drift(cls, before, after):
        """Human-readable description of camera movement between two _ptz_status
        readings, or None when it held still (or either reading is unavailable)."""
        delta = camera_engine.ptz_delta(before, after)
        if delta and any(d > t for d, t in zip(delta, cls.PTZ_DRIFT_TOLERANCE)):
            return f"pan {delta[0]:.1f}°, tilt {delta[1]:.1f}°, zoom {delta[2]:.1f}"
        return None

    def read_scenarios(self):
        """Return every rule on the camera: scan BOTH channels and ALL scene docs.

        Two Hik realities force the wide scan (both confirmed live on an LVT-384-25MM
        thermal dome, 10.23.39.254:5015):
          - the optical(1)/thermal(2) split: rules can live on either channel, and the
            GUI's channel picker is just a guess;
          - the trailing number in .../behaviorRule/{sid} is a SCENE document id, not
            a constant -- that unit keeps its six real rules in ch2 doc 2 while doc 1
            is empty, so reading only doc 1 misses all of them.
        Each scenario's native_id is (channel, sid, ruleId) so an edit patches exactly
        the document+rule it came from -- vital when operators reuse one name
        ("RULE1" x6 on that unit) for many different zones. A channel whose first
        document errors (auth/network/notSupport) is skipped whole; if nothing is
        readable anywhere, the first error is raised."""
        out, first_err = [], None
        selected, original_sid = self.client.channel, self.client.sid
        try:
            for ch in (selected, "1" if selected == "2" else "2"):
                self.client.channel = ch
                for sid in range(1, hik_config.MAX_SCENE_DOCS + 1):
                    self.client.sid = sid
                    try:
                        xml = self.client.get_behavior_rule()
                    except Exception as e:
                        if first_err is None:
                            first_err = e
                        break  # auth/network/notSupport won't differ for higher docs
                    out.extend(self._parse_scenarios(xml, channel=ch, sid=sid))
        finally:
            self.client.channel, self.client.sid = selected, original_sid
        if not out and first_err is not None:
            raise first_err
        return out

    @staticmethod
    def _parse_scenarios(xml, channel, sid):
        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml)
        out = []
        for ri in root.findall(".//ns:RuleInfo", hik_config.NS):
            name = _text(ri, "ns:ruleName")
            # native_id is (channel, sid, ruleId): ruleId is the identity the firmware
            # upserts by, but it's only unique WITHIN one scene document, and many
            # rules can share a ruleName -- the full triple pins down exactly one rule
            # on the camera. Falls back to the name when ruleId is absent.
            rid = _text(ri, "ns:ruleId")
            native = (channel, sid, int(rid) if rid.isdigit() else name)
            etype = _text(ri, "ns:eventType") or ""
            kind = "line" if "line" in etype.lower() else "intrusion"
            pts = [(int(_text(c, "ns:positionX")) / 1000.0, 1 - int(_text(c, "ns:positionY")) / 1000.0)
                   for c in ri.findall(".//ns:RegionCoordinates", hik_config.NS)]
            target = _text(ri, ".//ns:detectionTarget") or "human"
            dur = _text(ri, ".//ns:durationTime") or "0"
            direction = _HIK_DIR_TO_NEUTRAL.get(_text(ri, ".//ns:directionSensitivity")) if kind == "line" else None
            out.append(Scenario(name=name, kind=kind, points=pts,
                                classes=_hik_target_to_classes(target), direction=direction,
                                duration=int(dur) if dur.isdigit() else 0, native_id=native,
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

        # Aim the client at the exact document the rule lives in. On edit,
        # native_id is (channel, sid, ruleId) from read_scenarios; the ruleId part
        # locates the rule so a duplicate ruleName can't send the patch to the wrong
        # one. On create (native_id None) the rule lands in the selected channel's
        # rule-bearing scene document (see _locate_create_sid); patch then finds no
        # match and falls through to insert_or_replace_rule below.
        edit_id = None
        if isinstance(sc.native_id, tuple):
            ch, sid, rid = sc.native_id
            self.client.channel, self.client.sid = str(ch), int(sid)
            if isinstance(rid, int):
                edit_id = rid
        else:
            self.client.sid = self._locate_create_sid()

        backup_path, current = self.client.backup_behavior_rule(backup_dir)
        patched = hik_config.patch_rule_geometry(
            current, sc.name, region_hik, target=target, duration=sc.duration or None,
            min_box=min_box, max_box=max_box,
            direction=direction if sc.kind == "line" else None, rule_id=edit_id)
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

    def _first_rule_doc(self, channels):
        """First (channel, sid) whose behaviorRule document already carries rules,
        scanning scene docs 1..MAX_SCENE_DOCS per given channel. None when every
        document is empty (or a channel is unreadable). Restores the client's
        channel/sid either way."""
        orig_ch, orig_sid = self.client.channel, self.client.sid
        try:
            for ch in channels:
                self.client.channel = ch
                for sid in range(1, hik_config.MAX_SCENE_DOCS + 1):
                    self.client.sid = sid
                    try:
                        count, _, _ = hik_config.rule_summary(self.client.get_behavior_rule())
                    except Exception:
                        break
                    if count:
                        return ch, sid
            return None
        finally:
            self.client.channel, self.client.sid = orig_ch, orig_sid

    def _locate_create_sid(self):
        """Scene document a NEW rule should land in on the currently selected channel:
        the first document that already carries rules -- rules in an inactive scene
        document do nothing, and where the existing rules sit is where the camera's
        active scene lives. Falls back to document 1 (the pre-scene-aware behavior)
        when the whole channel is empty."""
        found = self._first_rule_doc([self.client.channel])
        return found[1] if found else 1

    @staticmethod
    def _frac_rect_to_hik(rect):
        """Neutral (fx,fy,fw,fh) top-left fraction -> Hik (x,y,w,h) in 0..1000 with the
        region Y-flip. Inverse of _size_rect. None -> None."""
        if not rect:
            return None
        fx, fy, fw, fh = rect
        return (round(fx * 1000), round((1 - fy - fh) * 1000), round(fw * 1000), round(fh * 1000))


# Neutral direction (Axis API values, plus "both") <-> Hik directionSensitivity.
# Hikvision has a real both-ways setting ("any"); Axis does not -- see
# AxisAdapter._apply_line_pair for how "both" is written there.
_NEUTRAL_DIR_TO_HIK = {"leftToRight": "left-right", "rightToLeft": "right-left",
                       "both": "any"}
_HIK_DIR_TO_NEUTRAL = {"left-right": "leftToRight", "right-left": "rightToLeft",
                       "any": "both"}

VALID_DIRECTIONS = ("leftToRight", "rightToLeft", "both")


def _points_equal(a, b, tol=1e-4):
    """Same polyline, allowing for the rounding each vendor's coordinate space does."""
    if len(a) != len(b):
        return False
    return all(abs(ax - bx) <= tol and abs(ay - by) <= tol
               for (ax, ay), (bx, by) in zip(a, b))


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
    # Zero-area boxes (seen live on 10.23.39.254:5015) are firmware placeholders, not
    # a real size filter -- treat as absent so we don't draw a degenerate rect or
    # round-trip it through an edit (patch_rule_geometry then leaves the camera's
    # SizeFilter untouched).
    if not w or not h:
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
    if sc.kind == "line" and sc.direction and sc.direction not in VALID_DIRECTIONS:
        raise ValueError(f"unknown crossing direction {sc.direction!r} "
                         f"(expected one of {', '.join(VALID_DIRECTIONS)})")
    bad = [c for c in sc.classes if c not in caps.classes]
    if bad:
        raise ValueError(f"unsupported detection class(es): {bad}")


# ---------------------------------------------------------------- factory

def make_adapter(vendor, ip, port, user, password, channel=None):
    import camera_engine
    v = camera_engine.classify_manufacturer(vendor)
    if v == "AXIS":
        return AxisAdapter(ip, port, user, password, channel=channel)
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
