"""Dry run a multi-camera analytics change: report exactly what WOULD happen.

Nothing here writes. There is no setConfiguration / apply_scenario call anywhere
in this module -- that is the safety property, not a promise in a comment. Run
it, read the plan, then decide whether to push.

    python dry_run.py --ip 10.23.87.55 --ports 5010,5015,5020 \
        --match-kind loiter --match-class human --set-duration 20

WHY THE DRY RUN IS THE FIRST PIECE OF MULTI-CAMERA WORK
--------------------------------------------------------
A bad fleet-wide change does not fail loudly; it succeeds, and fifty cameras
quietly end up watching the wrong thing. So the plan is produced, validated and
reviewed before anything is written -- and every camera that could NOT be read
is named in the summary rather than dropped in silence.

GEOMETRY IS NEVER TOUCHED HERE. Vertices are carried through unchanged and the
report says so per rule ("geometry unchanged (N verts)"). Bulk edits to
non-geometric properties -- dwell, classes, names -- are safe across cameras
precisely because they carry no scene correspondence: the same dwell time means
the same thing everywhere, while the same polygon does not. Copying a POLYGON
between cameras is a separate, much riskier feature and is deliberately absent.
"""

import argparse
import getpass
import os
import re
from pathlib import Path

import aoa_config
import vendor_adapter

CHANGE, BLOCK, SKIP = "CHANGE", "BLOCK ", "  --  "


class Plan:
    """What would happen to one camera."""

    def __init__(self, target):
        self.target = target
        self.error = None          # unreadable -> nothing is planned for it
        self.rows = []             # [(verdict, rule_name, detail)]

    @property
    def changes(self):
        return [r for r in self.rows if r[0] == CHANGE]

    @property
    def blocks(self):
        return [r for r in self.rows if r[0] == BLOCK]


def matches(sc, spec):
    """Is this rule inside the change's scope?"""
    if spec["kind"] and sc.kind != spec["kind"]:
        return False
    if spec["cls"] and spec["cls"] not in (sc.classes or ()):
        return False
    if spec["name_re"] and not re.search(spec["name_re"], sc.name):
        return False
    return True


def propose(sc, spec):
    """(name, duration, classes) this rule would end up with."""
    name = sc.name
    if spec["rename_from"] is not None:
        name = re.sub(spec["rename_from"], spec["rename_to"], name)
    duration = spec["duration"] if spec["duration"] is not None else sc.duration
    classes = tuple(spec["classes"]) if spec["classes"] else sc.classes
    return name, duration, classes


def plan_camera(ip, port, user, password, spec):
    plan = Plan(f"{ip}:{port}")
    try:
        adapter = vendor_adapter.make_adapter(spec["vendor"], ip, port, user, password)
        scenarios = adapter.read_scenarios()
    except Exception as e:                                        # noqa: BLE001
        # An unreadable camera is REPORTED, never quietly dropped: "we updated 12
        # of 15 cameras" has to be able to name the other three.
        plan.error = f"{type(e).__name__}: {e}"
        return plan

    caps = adapter.capabilities
    for sc in scenarios:
        if sc.read_only:
            plan.rows.append((SKIP, sc.name, "read-only -- this engine has no config API"))
            continue
        if not matches(sc, spec):
            plan.rows.append((SKIP, sc.name, "outside the match filter"))
            continue

        name, duration, classes = propose(sc, spec)
        deltas = []
        if name != sc.name:
            deltas.append(f"name {sc.name!r} -> {name!r}")
        if duration != sc.duration:
            deltas.append(f"dwell {sc.duration}s -> {duration}s")
        if classes != sc.classes:
            deltas.append(f"classes {sc.classes} -> {classes}")
        if not deltas:
            plan.rows.append((SKIP, sc.name, "already at the target values"))
            continue

        # Validate the PROPOSED rule now, so a 16-char name or an unsupported
        # class surfaces here rather than half way through a fleet push.
        problem = None
        if len(name) > aoa_config.MAX_NAME_LEN:
            problem = (f"name would be {len(name)} chars, camera caps at "
                       f"{aoa_config.MAX_NAME_LEN}")
        bad = [c for c in classes if caps.classes and c not in caps.classes]
        if bad:
            problem = f"unsupported class(es) {bad} on {adapter.vendor}"
        if sc.kind == "loiter" and duration is not None and duration < 1:
            problem = "loiter dwell must be at least 1s"

        detail = "; ".join(deltas) + f"  |  geometry unchanged ({len(sc.points)} verts)"
        plan.rows.append((BLOCK if problem else CHANGE, sc.name,
                          f"{problem}  |  {detail}" if problem else detail))
    return plan


def render_lines(plans, spec):
    """The plan as a list of text lines, plus the blocked-rule count.

    Split from printing so the GUI and the CLI show byte-identical reports --
    two renderers would drift, and the report is the whole product here."""
    out = []
    add = out.append
    add("=" * 78)
    add("DRY RUN -- no camera was written to")
    scope = []
    if spec["kind"]:
        scope.append(f"kind={spec['kind']}")
    if spec["cls"]:
        scope.append(f"class={spec['cls']}")
    if spec["name_re"]:
        scope.append("name~/" + spec["name_re"] + "/")
    change = []
    if spec["duration"] is not None:
        change.append(f"dwell -> {spec['duration']}s")
    if spec["rename_from"]:
        change.append("rename s/" + spec["rename_from"] + "/" + spec["rename_to"] + "/")
    if spec["classes"]:
        change.append(f"classes -> {tuple(spec['classes'])}")
    add("  match : " + (", ".join(scope) or "every writable rule"))
    add("  change: " + (", ".join(change) or "(nothing -- read-only preview)"))
    add("=" * 78)

    total_changes = total_blocks = 0
    for p in plans:
        add("")
        add(f"### {p.target}")
        if p.error:
            add(f"    SKIP   unreadable -- {p.error}")
            continue
        if not p.rows:
            add("    (no analytics rules configured)")
        for verdict, name, detail in p.rows:
            add(f"    {verdict} {name:<16} {detail}")
        total_changes += len(p.changes)
        total_blocks += len(p.blocks)

    unreadable = [p.target for p in plans if p.error]
    touched = len([p for p in plans if p.changes])
    add("")
    add("=" * 78)
    add(f"  {total_changes} rule(s) would change across {touched} camera(s)")
    if total_blocks:
        add(f"  {total_blocks} rule(s) BLOCKED -- fix these before pushing")
    if unreadable:
        add(f"  {len(unreadable)} camera(s) unreadable and excluded: "
            + ", ".join(unreadable))
    add("  nothing was written.")
    add("=" * 78)
    return out, total_blocks


def render(plans, spec):
    lines, blocked = render_lines(plans, spec)
    print()
    for line in lines:
        print(line)
    return blocked


def credentials(vendor, user, password):
    """CLI flag -> environment -> access.env -> prompt.

    Same order the GUIs use, so a fleet dry run can be scripted without putting a
    password in the shell history, and still works interactively."""
    prefix = "HIK" if str(vendor).upper().startswith("HIK") else "AXIS"
    user = user or os.environ.get(f"{prefix}_USER")
    password = password or os.environ.get(f"{prefix}_PASSWORD") or os.environ.get(f"{prefix}_PASS")
    env_file = Path(__file__).with_name("access.env")
    if (not user or not password) and env_file.exists():
        try:
            env = {}
            for line in env_file.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
            user = user or env.get(f"{prefix}_USER")
            password = password or env.get(f"{prefix}_PASSWORD") or env.get(f"{prefix}_PASS")
        except OSError:
            pass
    user = user or input("Username: ").strip()
    password = password or getpass.getpass("Password: ")
    return user, password


def main():
    ap = argparse.ArgumentParser(description="Dry run a multi-camera analytics change.")
    ap.add_argument("--ip", required=True)
    ap.add_argument("--ports", default="5010,5015,5020")
    ap.add_argument("--vendor", default="Axis")
    ap.add_argument("--user")
    ap.add_argument("--password")
    ap.add_argument("--match-kind", choices=["intrusion", "line", "loiter"])
    ap.add_argument("--match-class", choices=["human", "vehicle"])
    ap.add_argument("--match-name", help="regex matched against the rule name")
    ap.add_argument("--set-duration", type=int)
    ap.add_argument("--set-classes", help="comma list, e.g. human,vehicle")
    ap.add_argument("--rename-from", help="regex substituted in the rule name")
    ap.add_argument("--rename-to", default="", help="replacement for --rename-from")
    args = ap.parse_args()

    spec = {
        "vendor": args.vendor,
        "kind": args.match_kind,
        "cls": args.match_class,
        "name_re": args.match_name,
        "duration": args.set_duration,
        "classes": [c.strip() for c in args.set_classes.split(",")] if args.set_classes else None,
        "rename_from": args.rename_from,
        "rename_to": args.rename_to,
    }
    user, password = credentials(args.vendor, args.user, args.password)

    plans = [plan_camera(args.ip, p.strip(), user, password, spec)
             for p in args.ports.split(",") if p.strip()]
    return 1 if render(plans, spec) else 0


if __name__ == "__main__":
    raise SystemExit(main())
