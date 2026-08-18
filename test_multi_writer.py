#!/usr/bin/env python3
"""Tests for the multi-camera writer. No camera needed.

Two things are worth pinning here, and both bit during development:

1. SUBCLASS SIGNATURE COMPATIBILITY. MultiWriterApp overrides WriterApp methods
   that Tk calls as callbacks -- bound to <KeyRelease> and to option menus, which
   hand over an event or the chosen value. An override that narrows the signature
   compiles, imports, constructs and only fails when a human types in the IP box:
   "TypeError: _check_target_change() takes 1 positional argument but 2 were
   given". A test that only constructs the window cannot see it.

2. PER-CAMERA STATE ISOLATION. The whole feature is that each camera keeps its
   own zone. If a switch leaks state between sessions, the tool silently pushes
   one camera's geometry to another -- exactly the failure the design exists to
   avoid.

Needs a display. Reports SKIP rather than failing if Tk cannot start.

Run:  .venv\\Scripts\\python.exe test_multi_writer.py
"""

from __future__ import annotations

import inspect
import sys

from PIL import Image

import analytics_writer_gui as awg
import multi_writer_gui as mw


def _session(app, target, ip, port, colour):
    s = mw.CameraSession(ip, port, "Axis")
    s.loaded = True
    s.pil_image = Image.new("RGB", (640, 480), colour)
    app.sessions[target] = s
    return s


def _reset(app):
    app.sessions.clear()
    app.active = None
    app._switching = False
    app.points, app.exclude_zones, app.current_exclude = [], [], []
    app.perspective_bars, app.current_bar = [], []
    app.existing_scenarios, app.existing_overlays = [], []
    app.edit_label_map = {}
    app.name_var.set("")
    app._refresh_tabs()
    return app


# --------------------------------------------------------------------------- #

def test_overrides_keep_the_base_callback_signatures(app):
    """THE REGRESSION. Any override of a WriterApp method must still accept what
    Tk hands it. Checked for every override, not just the one that broke."""
    problems = []
    for name, sub in vars(mw.MultiWriterApp).items():
        if name.startswith("__") or not callable(sub):
            continue
        base = getattr(awg.WriterApp, name, None)
        if base is None or not callable(base):
            continue
        sp = inspect.signature(sub).parameters
        bp = inspect.signature(base).parameters
        base_var = any(p.kind is p.VAR_POSITIONAL for p in bp.values())
        sub_var = any(p.kind is p.VAR_POSITIONAL for p in sp.values())
        if base_var and not sub_var:
            problems.append(f"{name}: base takes *args, override does not")
        elif len(sp) < len(bp):
            problems.append(f"{name}: base{inspect.signature(base)} "
                            f"vs override{inspect.signature(sub)}")
    assert not problems, "; ".join(problems)
    return f"{len([n for n in vars(mw.MultiWriterApp) if getattr(awg.WriterApp, n, None)])} override(s) compatible"


def test_target_change_survives_the_callbacks_tk_actually_makes(app):
    """Bound to <KeyRelease> (an event) and to option menus (a string). Both
    shapes have to work -- this is the exact call that raised in the field."""
    app._check_target_change()                 # direct call
    app._check_target_change("Left")           # option menu -> selected value
    app._check_target_change(object())         # <KeyRelease> -> event object
    return "callable with 0, 1 value and 1 event argument"


def test_switching_keeps_each_camera_geometry_separate(app):
    _reset(app)
    _session(app, "1.1.1.1:5015", "1.1.1.1", "5015", "#304050")
    _session(app, "1.1.1.1:5020", "1.1.1.1", "5020", "#503040")

    app.switch_to("1.1.1.1:5015")
    app.name_var.set("ZONE-A")
    a = [(0.1, 0.1), (0.5, 0.1), (0.5, 0.5)]
    app.points = [app._frac_to_canvas(*p) for p in a]

    app.switch_to("1.1.1.1:5020")
    app.name_var.set("ZONE-B")
    b = [(0.6, 0.6), (0.9, 0.6), (0.9, 0.9), (0.6, 0.9)]
    app.points = [app._frac_to_canvas(*p) for p in b]

    app.switch_to("1.1.1.1:5015")
    assert app.name_var.get() == "ZONE-A", app.name_var.get()
    back = [app._canvas_to_frac(x, y) for (x, y) in app.points]
    assert len(back) == 3, back
    worst = max(max(abs(p[0] - q[0]), abs(p[1] - q[1])) for p, q in zip(back, a))
    assert worst < 0.005, f"geometry drifted by {worst} across switches"
    app.capture_active()
    assert [len(s.points) for s in app.sessions.values()] == [3, 4], app.sessions
    return f"two cameras hold 3 and 4 points independently; drift {worst:.5f}"


def test_only_named_and_drawn_cameras_are_pushable(app):
    """dirty gates the push. A loaded-but-untouched camera must never be written."""
    _reset(app)
    _session(app, "2.2.2.2:5010", "2.2.2.2", "5010", "#222")
    s2 = _session(app, "2.2.2.2:5015", "2.2.2.2", "5015", "#333")
    app.switch_to("2.2.2.2:5015")
    app.points = [app._frac_to_canvas(*p) for p in [(0.2, 0.2), (0.7, 0.2), (0.7, 0.7)]]
    app.name_var.set("ONLY-THIS")
    app.capture_active()
    pushable = [s.target for s in app.sessions.values() if s.dirty]
    assert pushable == ["2.2.2.2:5015"], pushable
    assert s2.name == "ONLY-THIS"
    return "untouched camera excluded; only the edited one is pushable"


def test_a_zone_without_a_name_is_not_pushable(app):
    _reset(app)
    _session(app, "3.3.3.3:5010", "3.3.3.3", "5010", "#444")
    app.switch_to("3.3.3.3:5010")
    app.points = [app._frac_to_canvas(*p) for p in [(0.2, 0.2), (0.7, 0.2), (0.7, 0.7)]]
    app.name_var.set("   ")
    app.capture_active()
    assert not app.sessions["3.3.3.3:5010"].dirty
    return "geometry with a blank name is not queued for push"


def test_a_failed_camera_is_marked_and_never_pushed(app):
    _reset(app)
    s = _session(app, "4.4.4.4:5010", "4.4.4.4", "5010", "#555")
    s.error, s.loaded, s.pil_image = "AOAError: HTTP 401", False, None
    assert s.status() == "error", s.status()
    assert not s.dirty
    app._refresh_tabs()
    return "unreadable camera shows as error and cannot be pushed"


def test_switching_to_a_camera_with_no_snapshot_clears_the_canvas(app):
    """SAFETY. If a camera failed to load, its tab must not leave the PREVIOUS
    camera's image on the canvas -- that is how someone draws a zone on the wrong
    scene and pushes it to the wrong camera."""
    _reset(app)
    good = _session(app, "6.6.6.6:5015", "6.6.6.6", "5015", "#777")
    bad = _session(app, "6.6.6.6:5010", "6.6.6.6", "5010", "#888")
    bad.pil_image, bad.loaded, bad.error = None, False, "ReadTimeout"

    app.switch_to("6.6.6.6:5015")
    assert app.tk_image is not None and app.img_w == 640, (app.img_w, app.img_h)

    app.switch_to("6.6.6.6:5010")
    assert app.tk_image is None, "a failed camera left an image on the canvas"
    assert (app.img_w, app.img_h) == (0, 0), (app.img_w, app.img_h)
    assert "no snapshot" in app.coord_label.cget("text"), app.coord_label.cget("text")
    return "failed camera clears the canvas instead of showing the last one"


def test_retry_failed_requeues_only_the_broken_cameras(app):
    _reset(app)
    ok = _session(app, "7.7.7.7:5015", "7.7.7.7", "5015", "#999")
    bad = _session(app, "7.7.7.7:5010", "7.7.7.7", "5010", "#aaa")
    bad.error, bad.loaded, bad.pil_image = "ReadTimeout", False, None
    captured = {}
    real = app._load_targets
    app._load_targets = lambda targets: captured.setdefault("targets", targets)
    try:
        app.retry_failed()
    finally:
        app._load_targets = real
    assert captured["targets"] == [("7.7.7.7", "5010")], captured
    assert "7.7.7.7:5015" in app.sessions, "the healthy camera was dropped"
    return "retry re-queues only the failed camera, keeps the loaded one"


def test_session_geometry_is_fractions_not_pixels(app):
    """Sessions must survive a resize, so nothing may be stored in canvas pixels."""
    _reset(app)
    _session(app, "5.5.5.5:5010", "5.5.5.5", "5010", "#666")
    app.switch_to("5.5.5.5:5010")
    app.points = [app._frac_to_canvas(*p) for p in [(0.25, 0.25), (0.75, 0.25), (0.75, 0.75)]]
    app.name_var.set("FRAC")
    app.capture_active()
    pts = app.sessions["5.5.5.5:5010"].points
    assert all(0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 for x, y in pts), pts
    assert abs(pts[0][0] - 0.25) < 0.005 and abs(pts[2][1] - 0.75) < 0.005, pts
    return "captured geometry is [0,1] fractions"


# --------------------------------------------------------------------------- #

def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    try:
        app = mw.MultiWriterApp()
        app.withdraw()
        app.update()
    except Exception as exc:                                      # noqa: BLE001
        print(f"\nSKIP multi-writer tests -- no usable display "
              f"({type(exc).__name__}: {exc})\n")
        return 0

    width = max(len(n) for n, _ in tests)
    failures = []
    print(f"\nmulti-camera writer -- {len(tests)} tests\n" + "=" * (width + 58))
    try:
        for name, fn in tests:
            try:
                print(f"PASS  {name:<{width}}  {fn(app) or ''}")
            except AssertionError as exc:
                failures.append(name)
                print(f"FAIL  {name:<{width}}  {exc}")
            except Exception as exc:                              # noqa: BLE001
                failures.append(name)
                print(f"ERROR {name:<{width}}  {type(exc).__name__}: {exc}")
    finally:
        try:
            for job in app.tk.call("after", "info"):
                try:
                    app.after_cancel(job)
                except Exception:                                 # noqa: BLE001
                    pass
            app.destroy()
        except Exception:                                         # noqa: BLE001
            pass

    print("=" * (width + 58))
    print(f"{len(tests) - len(failures)}/{len(tests)} passed\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
