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
    app._pending_targets = []
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


def test_clear_all_empties_the_session_and_canvas(app):
    """An escape hatch. Without it a bad batch could only be abandoned by closing
    the app, which is what an operator hit in the field."""
    _reset(app)
    _session(app, "8.8.8.8:5015", "8.8.8.8", "5015", "#bbb")
    app.switch_to("8.8.8.8:5015")
    app.name_var.set("GONE")
    app.points = [app._frac_to_canvas(*p) for p in [(0.2, 0.2), (0.7, 0.2), (0.7, 0.7)]]
    app.clear_all()
    assert app.sessions == {} and app.active is None, (app.sessions, app.active)
    assert app.points == [] and app.tk_image is None
    assert app.name_var.get() == ""
    return "clear empties sessions, drawing and canvas"


def test_clear_refuses_while_work_is_running(app):
    """Clearing mid-batch would leave the worker writing into sessions that no
    longer exist; it says press Cancel first instead."""
    _reset(app)
    _session(app, "9.9.9.9:5015", "9.9.9.9", "5015", "#ccc")

    class _Busy:
        @staticmethod
        def is_alive():
            return True

    real, app.worker = app.worker, _Busy()
    try:
        app.clear_all()
    finally:
        app.worker = real
    assert "9.9.9.9:5015" in app.sessions, "cleared while busy"
    return "clear refused while a batch is in flight"


def test_cancel_sets_the_flag_only_when_work_is_running(app):
    _reset(app)
    app._cancel.clear()
    app.cancel_batch()                     # nothing running
    assert not app._cancel.is_set(), "cancelled with no work in flight"

    class _Busy:
        @staticmethod
        def is_alive():
            return True

    real, app.worker = app.worker, _Busy()
    try:
        app.cancel_batch()
        assert app._cancel.is_set(), "cancel did not set the flag"
    finally:
        app.worker = real
        app._cancel.clear()
    return "cancel is a no-op when idle, sets the flag when busy"


def test_batch_timeout_is_widened_then_restored(app):
    """Snapshots in a batch get a longer timeout than the interactive default,
    and the original MUST come back even if the work raises."""
    import camera_engine
    original = camera_engine.STRICT_TIMEOUT
    seen = {}

    def boom():
        seen["during"] = camera_engine.STRICT_TIMEOUT
        raise RuntimeError("work failed")

    try:
        mw.MultiWriterApp._with_batch_timeout(boom)()
    except RuntimeError:
        pass
    assert seen["during"] == mw.BATCH_SNAPSHOT_TIMEOUT, seen
    assert camera_engine.STRICT_TIMEOUT == original, "timeout not restored"
    assert mw.BATCH_SNAPSHOT_TIMEOUT[1] > original[1], "batch read timeout must be longer"
    return f"{original} -> {mw.BATCH_SNAPSHOT_TIMEOUT} during batch, restored after"


def test_tab_label_uses_tdc_and_position_when_the_fleet_picker_supplied_it(app):
    """Operators think in units and positions, not addresses. But a hand-typed IP
    has no catalog row, so the address has to remain the fallback -- a blank or
    half-built label would be worse than a technical one."""
    from_fleet = mw.CameraSession("10.0.0.5", "5015", "Axis",
                                  fleet={"tdc": "TDC12345", "client": "ACME",
                                         "location": "Yard 3"})
    assert from_fleet.position == "Left", from_fleet.position
    assert from_fleet.label == "TDC12345 - Left", from_fleet.label

    for port, expect in (("5010", "Center"), ("5015", "Left"), ("5020", "Right")):
        s = mw.CameraSession("10.0.0.5", port, "Axis", fleet={"tdc": "TDC1"})
        assert s.label == f"TDC1 - {expect}", s.label

    typed = mw.CameraSession("10.0.0.9", "5020", "Axis")
    assert typed.label == "10.0.0.9:5020", typed.label

    blank = mw.CameraSession("10.0.0.9", "5020", "Axis", fleet={"tdc": ""})
    assert blank.label == "10.0.0.9:5020", "an empty TDC must fall back, not show nothing"

    odd = mw.CameraSession("10.0.0.9", "8080", "Axis", fleet={"tdc": "TDC7"})
    assert odd.label == "TDC7 - port 8080", odd.label

    # target stays the address -- it is the key and what the network needs.
    assert from_fleet.target == "10.0.0.5:5015", from_fleet.target
    return "TDC + position when known, address when not; target unchanged"


def test_fleet_pick_records_the_catalog_row_against_the_ip(app):
    """The picker had the row all along and threw it away. Verify it lands in
    fleet_info, and that a pick without a row does not explode."""
    app.fleet_info.clear()
    app._apply_fleet_pick("10.1.2.3", "Axis", {
        "LIVE_UNIT_SERIAL_NM": " TDC99887 ", "CLIENT_NM": "ACME",
        "LOCATION_NM": "North Lot", "MODEL": "Q6135-LE"})
    info = app.fleet_info["10.1.2.3"]
    assert info["tdc"] == "TDC99887", info          # whitespace stripped
    assert info["location"] == "North Lot", info
    app._apply_fleet_pick("10.1.2.4", "Axis")       # hand-typed / no row
    assert "10.1.2.4" not in app.fleet_info
    return "catalog row stored per IP; a row-less pick is harmless"


def test_fleet_pick_loads_all_three_cameras_of_the_unit(app):
    """A TDC is one IP with three cameras behind fixed ports; picking a unit in
    the Fleet Picker must load the whole unit, tabs named by TDC + position."""
    _reset(app)
    captured = {}
    real = app._load_targets
    app._load_targets = lambda targets: captured.setdefault("targets", targets)
    try:
        app._apply_fleet_pick("10.5.5.5", "Axis",
                              {"LIVE_UNIT_SERIAL_NM": "TDC55555", "CLIENT_NM": "ACME",
                               "LOCATION_NM": "Yard 9"})
    finally:
        app._load_targets = real
    assert captured["targets"] == [("10.5.5.5", "5010"), ("10.5.5.5", "5015"),
                                   ("10.5.5.5", "5020")], captured
    assert app.fleet_info["10.5.5.5"]["tdc"] == "TDC55555"
    # And the sessions those targets create would carry the TDC label.
    s = mw.CameraSession("10.5.5.5", "5015", "Axis", fleet=app.fleet_info["10.5.5.5"])
    assert s.label == "TDC55555 - Left", s.label
    return "one pick -> Center + Left + Right, labelled by TDC"


def test_fleet_pick_saves_the_active_edit_before_the_form_changes(app):
    """The base pick handler changes the form target, which wipes the canvas. An
    unsaved zone on the active tab must be captured into its session first."""
    _reset(app)
    _session(app, "10.6.6.6:5015", "10.6.6.6", "5015", "#ddd")
    app.switch_to("10.6.6.6:5015")
    app.name_var.set("KEEP-ME")
    app.points = [app._frac_to_canvas(*p) for p in [(0.2, 0.2), (0.7, 0.2), (0.7, 0.7)]]
    real = app._load_targets
    app._load_targets = lambda targets: None
    try:
        app._apply_fleet_pick("10.7.7.7", "Axis", {"LIVE_UNIT_SERIAL_NM": "TDC77777"})
    finally:
        app._load_targets = real
    kept = app.sessions["10.6.6.6:5015"]
    assert kept.name == "KEEP-ME" and len(kept.points) == 3, (kept.name, kept.points)
    return "active tab's unsaved zone survives picking the next unit"


def test_a_pick_mid_batch_queues_instead_of_vanishing(app):
    """THE FIELD REPORT. Pick unit A, then pick unit B while A's slowest camera
    is still timing out: B used to be refused with a log line and silently lost,
    which broke the pick-a-whole-location rhythm. It must queue -- without
    creating sessions (a session with no batch underway strands as 'loading'
    forever), without duplicates, and it must start once the worker frees."""
    _reset(app)

    class _Busy:
        @staticmethod
        def is_alive():
            return True

    real, app.worker = app.worker, _Busy()
    try:
        app._load_targets([("10.8.8.8", "5010"), ("10.8.8.8", "5015")])
        app._load_targets([("10.8.8.8", "5010")])      # picked again -> no dupe
    finally:
        app.worker = real
    assert "10.8.8.8:5010" not in app.sessions, "session created for a queued batch"
    assert app._pending_targets == [("10.8.8.8", "5010"), ("10.8.8.8", "5015")]

    captured = {}
    real_load = app._load_targets
    app._load_targets = lambda targets: captured.setdefault("targets", targets)
    try:
        app._drain_pending()                           # worker is free again
    finally:
        app._load_targets = real_load
    assert captured["targets"] == [("10.8.8.8", "5010"), ("10.8.8.8", "5015")]
    assert app._pending_targets == []
    return "mid-batch picks queue (deduped) and start when the worker frees"


def test_cancel_drops_the_queue_instead_of_resurrecting_it(app):
    """Cancel means STOP: auto-starting queued cameras right after a cancel would
    quietly overrule the operator."""
    _reset(app)
    app._pending_targets = [("10.9.9.9", "5010")]
    app._cancel.set()
    app.msg_queue.put(("bg_idle", None))
    app._poll_queue()
    assert app._pending_targets == [], "queued cameras survived a cancel"
    assert not app._cancel.is_set(), "cancel flag must reset for the next batch"
    return "cancel clears the queue; the flag resets"


def test_sidebar_actions_grey_out_during_a_batch(app):
    """FIELD REPORT, round two. The camera-bar buttons greyed during a batch but
    the sidebar's Fetch Snapshot / Read Config / Push did not -- and the canvas
    hint even pointed at Fetch Snapshot -- so the operator's click was met with
    'A request is already running.' and read as the fixes missing."""
    _reset(app)
    app._set_busy(True)
    for name in ("fetch_button", "read_button", "push_button", "load_button"):
        assert str(getattr(app, name).cget("state")) == "disabled", name
    assert str(app.cancel_button.cget("state")) == "normal", "Cancel must stay live"
    app._set_busy(False)
    for name in ("fetch_button", "read_button", "load_button"):
        assert str(getattr(app, name).cget("state")) == "normal", name
    return "sidebar + bar buttons grey while busy; Cancel never does"


def test_busy_refusal_message_explains_the_batch(app):
    """If a click does slip through, the refusal must say what is happening and
    what to do -- not the bare 'A request is already running.'"""
    _reset(app)

    class _Busy:
        @staticmethod
        def is_alive():
            return True

    real, app.worker = app.worker, _Busy()
    try:
        app._run_bg(lambda: None)
    finally:
        app.worker = real
    tail = app.log_box.get("1.0", "end").strip().splitlines()[-1]
    assert "batch" in tail and "automatically" in tail, tail
    return "refusal names the batch and says snapshots arrive on their own"


def test_queued_camera_hint_says_wait_not_click(app):
    """The canvas hint for a still-loading camera must counsel patience -- telling
    the operator to press a button the worker will refuse caused this loop."""
    _reset(app)
    _session(app, "11.1.1.1:5015", "11.1.1.1", "5015", "#eee")
    waiting = _session(app, "11.1.1.1:5010", "11.1.1.1", "5010", "#fff")
    waiting.pil_image, waiting.loaded, waiting.error = None, False, None
    app.switch_to("11.1.1.1:5015")
    app.switch_to("11.1.1.1:5010")
    text = app.coord_label.cget("text")
    assert "still loading" in text, text
    assert "Fetch Snapshot" not in text, f"hint points at a refused button: {text}"
    return "loading tab says the snapshot is on its way"


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
