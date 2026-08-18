#!/usr/bin/env python3
"""Tests for the multi-camera dry run. No camera needed.

The dry run is what stands between one typo and fifty misconfigured cameras, so
the properties that matter are behavioural, not cosmetic: it must never alter
geometry, must refuse rather than guess on an unreadable camera, and must catch
a rule the camera would reject BEFORE anything is pushed.

Run:  .venv\\Scripts\\python.exe test_dry_run.py
"""

from __future__ import annotations

import sys

import dry_run
import vendor_adapter


def _spec(**over):
    base = {"vendor": "Axis", "kind": None, "cls": None, "name_re": None,
            "duration": None, "classes": None, "rename_from": None, "rename_to": ""}
    base.update(over)
    return base


def _sc(name, kind="loiter", classes=("human",), duration=15, points=None, **kw):
    return vendor_adapter.Scenario(
        name=name, kind=kind, classes=classes, duration=duration,
        points=points or [(0.1, 0.1), (0.9, 0.1), (0.9, 0.9)], **kw)


class _StubAdapter:
    vendor = "Axis"
    capabilities = vendor_adapter.AxisAdapter.capabilities

    def __init__(self, scenarios):
        self._scs = scenarios

    def read_scenarios(self):
        return self._scs


def _plan(scenarios, spec, monkey_error=None):
    """Run plan_camera against stub scenarios instead of a camera."""
    real = vendor_adapter.make_adapter

    def fake(*_a, **_kw):
        if monkey_error:
            raise monkey_error
        return _StubAdapter(scenarios)

    vendor_adapter.make_adapter = fake
    try:
        return dry_run.plan_camera("1.2.3.4", "5010", "u", "p", spec)
    finally:
        vendor_adapter.make_adapter = real


# --------------------------------------------------------------------------- #

def test_geometry_is_never_altered():
    """The whole premise of safe bulk editing: properties change, polygons don't."""
    pts = [(0.11, 0.22), (0.9, 0.2), (0.8, 0.77), (0.2, 0.7)]
    sc = _sc("R1", duration=5, points=list(pts))
    plan = _plan([sc], _spec(duration=30))
    assert len(plan.changes) == 1, plan.rows
    assert "geometry unchanged (4 verts)" in plan.changes[0][2], plan.changes[0]
    assert sc.points == pts, "the source scenario's points were mutated"
    return "dwell change reported with geometry explicitly untouched"


def test_match_filters_scope_the_change():
    scs = [_sc("H1", classes=("human",)), _sc("V1", classes=("vehicle",)),
           _sc("L1", kind="line", classes=("human",))]
    plan = _plan(scs, _spec(kind="loiter", cls="human", duration=30))
    changed = [r[1] for r in plan.changes]
    assert changed == ["H1"], changed
    assert all("outside the match filter" in r[2] for r in plan.rows if r[1] != "H1")
    return "kind + class filters select exactly one of three rules"


def test_name_regex_filter():
    scs = [_sc("Scene1_H_1s"), _sc("Gate_H_1s")]
    plan = _plan(scs, _spec(name_re="^Scene1_", duration=30))
    assert [r[1] for r in plan.changes] == ["Scene1_H_1s"], plan.rows
    return "name regex narrows the scope"


def test_rename_is_a_regex_substitution():
    plan = _plan([_sc("Scene_V_10s")], _spec(rename_from="^Scene_", rename_to="Scene1_"))
    assert len(plan.changes) == 1
    assert "'Scene_V_10s' -> 'Scene1_V_10s'" in plan.changes[0][2], plan.changes[0]
    return "drifted name normalised by substitution"


def test_a_rule_already_at_target_is_not_a_change():
    """Re-running a bulk change must be a no-op, not a second write to everything."""
    plan = _plan([_sc("R1", duration=20)], _spec(duration=20))
    assert plan.changes == [], plan.rows
    assert "already at the target values" in plan.rows[0][2]
    return "idempotent -- no change reported when already correct"


def test_overlong_name_is_blocked_not_pushed():
    """Caught in the plan, not half way through a fleet push."""
    plan = _plan([_sc("Scene1_H_15s")],
                 _spec(rename_from="^Scene1_", rename_to="PerimeterZone_"))
    assert len(plan.blocks) == 1 and plan.changes == [], plan.rows
    assert "caps at 15" in plan.blocks[0][2], plan.blocks[0]
    return "19-char name blocked against the camera's 15-char limit"


def test_unsupported_class_is_blocked():
    plan = _plan([_sc("R1")], _spec(classes=["unicorn"]))
    assert len(plan.blocks) == 1, plan.rows
    assert "unsupported class" in plan.blocks[0][2]
    return "a class the vendor does not support is refused"


def test_unreadable_camera_is_reported_not_skipped_silently():
    """'We updated 12 of 15' has to be able to name the other three."""
    plan = _plan([], _spec(duration=30), monkey_error=RuntimeError("HTTP 401"))
    assert plan.error and "HTTP 401" in plan.error, plan.error
    assert plan.rows == [] and plan.changes == []
    return "unreadable camera carries its error and plans nothing"


def test_read_only_rules_are_left_alone():
    """A Perimeter Defender zone has no config API -- it can never be in a plan."""
    plan = _plan([_sc("intrusion-1 / zone-1", read_only=True)], _spec(duration=30))
    assert plan.changes == [], plan.rows
    assert "read-only" in plan.rows[0][2]
    return "read-only thermal zones excluded from any bulk change"


def test_module_contains_no_write_calls():
    """The safety property is structural: if a write ever appears here, this fails."""
    import pathlib
    src = pathlib.Path(dry_run.__file__).read_text(encoding="utf-8")
    body = "\n".join(line for line in src.splitlines()
                     if not line.strip().startswith(("#", '"', "'")))
    for forbidden in ("set_config(", "apply_scenario(", "put_behavior_rule(",
                      "set_params(", "delete_scenario(", "restore_context("):
        assert forbidden not in body, f"dry_run.py must not call {forbidden}"
    return "no write call exists anywhere in dry_run.py"


# --------------------------------------------------------------------------- #

def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    width = max(len(n) for n, _ in tests)
    failures = []
    print(f"\nmulti-camera dry run -- {len(tests)} tests\n" + "=" * (width + 58))
    for name, fn in tests:
        try:
            print(f"PASS  {name:<{width}}  {fn() or ''}")
        except AssertionError as exc:
            failures.append(name)
            print(f"FAIL  {name:<{width}}  {exc}")
        except Exception as exc:                                  # noqa: BLE001
            failures.append(name)
            print(f"ERROR {name:<{width}}  {type(exc).__name__}: {exc}")
    print("=" * (width + 58))
    print(f"{len(tests) - len(failures)}/{len(tests)} passed\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
