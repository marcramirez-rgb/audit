"""Multi-Camera Analytics Writer -- load several cameras, edit each on its own
snapshot, push everything in one go.

This is the ordinary writer with a camera bar bolted on: it SUBCLASSES WriterApp,
so the canvas, vertex dragging, exclusion zones, size boxes, perspective bars,
gold reference overlays and the per-vendor push logic are the same code, not a
reimplementation that would drift from it.

WHAT THIS IS NOT
----------------
It does not broadcast one zone to many cameras. Each camera keeps its OWN
geometry, drawn on its OWN snapshot, and pushing N cameras makes N independent
API calls. That is the whole point: a polygon means a place in a specific
scene, so copying one between cameras with different mounting, lens or aim puts
the zone somewhere nobody looked at. Batching the WORKFLOW is safe; batching the
GEOMETRY is not, and this tool deliberately only does the former.

HOW PER-CAMERA STATE IS KEPT
----------------------------
WriterApp holds exactly one camera's state in instance attributes. Switching
cameras here captures the active camera's edits into a session dict and restores
the target's. Geometry is stored as [0,1] FRACTIONS rather than canvas pixels,
so a session survives a window resize and can be pushed without depending on
whatever the canvas happens to look like at that moment.
"""

import queue
import threading
import time
from concurrent import futures

import tkinter as tk

import customtkinter as ctk

import analytics_writer_gui as awg
import camera_engine
import vendor_adapter
from analytics_writer_gui import (LVT_DARK_TEAL, LVT_DARK_TEAL_HOVER, LVT_LIGHT,
                                  LVT_TEAL, LVT_TEAL_HOVER, LVT_TEXT_DARK,
                                  LVT_TEXT_MUTED, WriterApp)


#: Snapshot timeouts for a BATCH read. MEASURED, not guessed: AxisHandler tries
#: GET then POST for EACH of two auth strategies, so an unreachable snapshot costs
#: FOUR timeouts, not one. A generous 25s read therefore turned a ~20s failure
#: into ~124s and one bad camera swallowed the whole batch. Keep this modest --
#: a camera that needs longer is better served by the Retry button than by making
#: every failure four times as expensive.
BATCH_SNAPSHOT_TIMEOUT = (4.0, 10.0)

#: Cameras contacted at once. These are DIFFERENT cameras behind one NAT, so
#: there is no same-device contention -- the collision that caused trouble before
#: was a manual fetch racing the batch on ONE camera, which the single-flight
#: worker still prevents. Kept small so a unit's uplink is not saturated.
BATCH_CONCURRENCY = 3


class CameraSession:
    """One camera's editing state, in vendor-neutral terms.

    Everything geometric is a [0,1] fraction so it is independent of canvas size
    and directly pushable; nothing here references a Tk widget."""

    def __init__(self, ip, port, vendor, sensor=None, channel=None, fleet=None):
        self.ip, self.port, self.vendor = ip, str(port), vendor
        # Catalog details for this camera's unit, when it came from the Fleet
        # Picker. None for a hand-typed IP.
        self.fleet = fleet or {}
        self.sensor, self.channel = sensor, channel
        self.loaded = False          # usable: we have a snapshot, rules or not
        self.error = None            # nothing at all could be read
        self.analytics_error = None  # snapshot fine, rules could not be read
        self.pil_image = None
        self.existing_scenarios = []
        self.existing_overlays = []
        # --- the edit in progress
        self.points = []             # [(fx, fy)]
        self.exclusions = []         # [[(fx, fy), ...]]
        self.name = ""
        self.kind_label = "Intrusion"
        self.classes = ("human",)
        self.duration = ""
        self.direction = None
        self.editing = None          # native_id when editing an existing rule
        self.perspective = []
        self.sizes = []

    @property
    def target(self):
        """Stable key and the address anything network-facing needs."""
        return f"{self.ip}:{self.port}"

    @property
    def position(self):
        """Center / Left / Right -- how the three cameras on a unit are known."""
        return awg.PORT_VALUE_TO_LABEL.get(self.port, f"port {self.port}")

    @property
    def label(self):
        """Operator-facing name: the unit (TDC) and which camera on it.

        An IP is how the tool reaches a camera; a TDC and a position are how a
        person refers to one. Falls back to the address when the camera did not
        come from the Fleet Picker, because a hand-typed IP has no catalog entry
        and a blank label would be worse than a technical one."""
        tdc = (self.fleet or {}).get("tdc")
        return f"{tdc} - {self.position}" if tdc else f"{self.target}"

    @property
    def dirty(self):
        """Has anything been drawn that could be pushed?"""
        return bool(self.points) and bool(self.name.strip())

    def status(self):
        if self.error:
            return "error"
        if not self.loaded:
            return "loading"
        if self.dirty:
            return "edited"
        return "warn" if (self.analytics_error or self.pil_image is None) else "loaded"


class MultiWriterApp(WriterApp):
    def __init__(self):
        self.sessions = {}           # target -> CameraSession
        self.active = None           # target currently on the canvas
        self._switching = False      # suppress the base class's camera-change reset
        # Cooperative cancel. An HTTP request already in flight cannot be killed,
        # so this is checked between cameras and during the retry wait: the camera
        # being contacted finishes, then the batch stops.
        self._cancel = threading.Event()
        # Cameras picked while a batch is still running. Loaded automatically when
        # the worker frees up; dropped by Cancel and Clear.
        self._pending_targets = []
        super().__init__()
        self.title("Multi-Camera Analytics Writer")
        self._build_camera_bar()

    # ------------------------------------------------------------------ base hooks

    def _check_target_change(self, *args):
        """The base class wipes the canvas when the IP/port fields change, which is
        right for a single-camera tool. Here a switch is a deliberate, state-saving
        operation, so the reset is suppressed for the duration of one.

        Keeps the base's *args: this is bound to <KeyRelease> on the IP entry and
        to two option menus, which hand it an event / the chosen value. Narrowing
        the signature made every keystroke in the IP box raise TypeError."""
        if self._switching:
            return
        super()._check_target_change(*args)

    # ------------------------------------------------------------------ camera bar

    def _build_camera_bar(self):
        bar = ctk.CTkFrame(self, fg_color=LVT_LIGHT, corner_radius=8)
        bar.pack(fill="x", side="top", padx=16, pady=(0, 8), before=self.winfo_children()[1])
        top = ctk.CTkFrame(bar, fg_color="transparent")
        top.pack(fill="x", padx=10, pady=(8, 2))
        ctk.CTkLabel(top, text="Cameras in this session", text_color=LVT_TEXT_DARK,
                     font=ctk.CTkFont(size=13, weight="bold")).pack(side="left")
        self.load_button = ctk.CTkButton(top, text="Load cameras...", width=130,
                                         command=self._open_loader, fg_color=LVT_TEAL,
                                         hover_color=LVT_TEAL_HOVER)
        self.load_button.pack(side="right")
        self.retry_button = ctk.CTkButton(top, text="Retry failed", width=110,
                                          command=self.retry_failed, fg_color=LVT_TEAL,
                                          hover_color=LVT_TEAL_HOVER)
        self.retry_button.pack(side="right", padx=6)
        self.clear_button = ctk.CTkButton(top, text="Clear", width=70,
                                          command=self.clear_all, fg_color=LVT_TEAL,
                                          hover_color=LVT_TEAL_HOVER)
        self.clear_button.pack(side="right", padx=(0, 6))
        # Stays enabled while work runs -- having no way out was the whole problem.
        self.cancel_button = ctk.CTkButton(top, text="Cancel", width=80,
                                           command=self.cancel_batch, fg_color=LVT_TEAL,
                                           hover_color=LVT_TEAL_HOVER)
        self.cancel_button.pack(side="right", padx=(0, 6))
        self.push_all_button = ctk.CTkButton(
            top, text="Push ALL edited cameras", width=200, command=self._push_all,
            fg_color=LVT_DARK_TEAL, hover_color=LVT_DARK_TEAL_HOVER,
            font=ctk.CTkFont(size=12, weight="bold"))
        self.push_all_button.pack(side="right", padx=8)

        self.tabs = ctk.CTkFrame(bar, fg_color="transparent")
        self.tabs.pack(fill="x", padx=10, pady=(2, 8))
        self.tab_hint = ctk.CTkLabel(
            self.tabs, text="No cameras loaded. Use \"Load cameras...\" to add some.",
            text_color=LVT_TEXT_MUTED, font=ctk.CTkFont(size=11))
        self.tab_hint.pack(anchor="w")

    def _refresh_tabs(self):
        for w in self.tabs.winfo_children():
            w.destroy()
        if not self.sessions:
            self.tab_hint = ctk.CTkLabel(
                self.tabs, text="No cameras loaded. Use \"Load cameras...\" to add some.",
                text_color=LVT_TEXT_MUTED, font=ctk.CTkFont(size=11))
            self.tab_hint.pack(anchor="w")
            return
        marks = {"loading": "...", "error": "!", "edited": "*",
                 "warn": "~", "loaded": ""}
        widest = max((len(s.label) for s in self.sessions.values()), default=12)
        for target, s in self.sessions.items():
            mark = marks[s.status()]
            label = f"{s.label}{('  ' + mark) if mark else ''}"
            active = target == self.active
            ctk.CTkButton(
                self.tabs, text=label, width=max(150, min(230, widest * 9 + 34)), height=28,
                command=lambda t=target: self.switch_to(t),
                fg_color=LVT_DARK_TEAL if active else LVT_TEAL,
                hover_color=LVT_DARK_TEAL_HOVER if active else LVT_TEAL_HOVER,
                font=ctk.CTkFont(size=11, weight="bold" if active else "normal"),
            ).pack(side="left", padx=(0, 6))
        edited = [t for t, s in self.sessions.items() if s.dirty]
        ctk.CTkLabel(self.tabs,
                     text=f"   * edited ({len(edited)} ready to push)   ~ partial   ! failed",
                     text_color=LVT_TEXT_MUTED, font=ctk.CTkFont(size=11)).pack(side="left")

    # ------------------------------------------------------------------ loader

    def _open_loader(self):
        dlg = ctk.CTkToplevel(self)
        dlg.title("Load cameras")
        dlg.geometry("460x360")
        dlg.transient(self)
        ctk.CTkLabel(dlg, text="One camera per line as ip or ip:port.\n"
                              "A bare IP loads the ports ticked below.",
                     justify="left", text_color=LVT_TEXT_MUTED).pack(anchor="w", padx=14, pady=(12, 4))
        box = ctk.CTkTextbox(dlg, height=130, font=ctk.CTkFont(family="Consolas", size=12))
        box.pack(fill="x", padx=14)
        current = self.ip_entry.get().strip()
        if current:
            box.insert("1.0", current)
        port_vars = {}
        row = ctk.CTkFrame(dlg, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=8)
        for label, value in awg.PORT_LABEL_TO_VALUE.items():
            if value == "80":
                continue
            v = tk.BooleanVar(value=True)
            port_vars[value] = v
            ctk.CTkCheckBox(row, text=label, variable=v, fg_color=LVT_TEAL,
                            hover_color=LVT_TEAL_HOVER).pack(side="left", padx=(0, 10))

        def go():
            targets, ports = [], [p for p, v in port_vars.items() if v.get()]
            for raw in box.get("1.0", "end").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                if ":" in raw:
                    ip, _, port = raw.partition(":")
                    if ip.strip() and port.strip():
                        targets.append((ip.strip(), port.strip()))
                        continue
                targets.extend((raw, p) for p in ports)
            dlg.destroy()
            self._load_targets(targets)

        ctk.CTkButton(dlg, text="Load", command=go, fg_color=LVT_DARK_TEAL,
                      hover_color=LVT_DARK_TEAL_HOVER, height=38).pack(fill="x", padx=14, pady=12)

    def _apply_fleet_pick(self, ip, mfg_label, row=None):
        """One Fleet Picker pick loads the WHOLE unit.

        A TDC is one IP with its cameras behind the fixed position ports, so in a
        multi-camera tool the useful unit of loading is the unit -- picking
        Center, then Left, then Right one at a time is three trips through the
        dialog for one physical box.

        Deliberately does NOT call the base handler: that one retargets the
        editor (new IP in the sidebar, canvas wiped, 'set the port then Fetch
        Snapshot' guidance), which is right for the single-camera tool and wrong
        here -- the canvas must stay on the tab being edited while the picked
        unit's cameras arrive as new tabs, or queue behind a batch in flight."""
        # Fold the active tab's edits into its session first: switching the
        # vendor below re-gates the UI, and unsaved state must survive a pick.
        self.capture_active()
        self._record_fleet_row(ip, row)
        if mfg_label and mfg_label != self.mfg_var.get():
            # The picked vendor decides which API and which prefilled credentials
            # the new batch loads with. _switching suppresses the base class's
            # target-change reset -- nothing about the CURRENT tab has changed.
            self._switching = True
            try:
                self.mfg_var.set(mfg_label)
                self._on_mfg_change()
            finally:
                self._switching = False
        ports = [p for p in awg.PORT_LABEL_TO_VALUE.values() if p != "80"]
        tdc = (self.fleet_info.get(ip) or {}).get("tdc") or ip
        self._log(f"[*] Fleet pick: loading all {len(ports)} cameras of {tdc}.")
        self._load_targets([(ip, p) for p in ports])

    def _load_targets(self, targets):
        # A batch already in flight does not REFUSE new cameras -- it QUEUES them.
        # Refusing broke the natural rhythm of the persistent Fleet Picker (pick a
        # unit, pick the next, pick the next...): the second pick landed while the
        # first unit's slowest camera was still timing out, and simply vanished.
        # Sessions are still only created when their batch actually starts, so a
        # queued camera can never strand as a "loading" tab with no work underway.
        if self.worker and self.worker.is_alive():
            fresh = [(ip, p) for ip, p in targets
                     if f"{ip}:{p}" not in self.sessions
                     and (ip, p) not in self._pending_targets]
            self._pending_targets.extend(fresh)
            if fresh:
                self._log(f"[*] Batch in progress -- queued {len(fresh)} camera(s); "
                          f"they load automatically when it finishes.")
            else:
                self._log("[.] Those cameras are already loaded or queued.")
            return
        user = self.user_entry.get().strip()
        password = self.pass_entry.get().strip()
        if not (user and password):
            self._log("[!] Enter username and password first.")
            return
        vendor = self.mfg_var.get()
        sensor = self._axis_sensor_or_none()
        channel = self.channel_var.get() if hasattr(self, "channel_var") else None

        new = []
        for ip, port in targets:
            t = f"{ip}:{port}"
            if t in self.sessions:
                continue
            self.sessions[t] = CameraSession(ip, port, vendor, sensor, channel,
                                             fleet=self.fleet_info.get(ip))
            new.append(t)
        self._refresh_tabs()
        if not new:
            self._log("[.] Those cameras are already loaded.")
            return
        self._log("[*] Loading " + str(len(new)) + " camera(s): "
                  + ", ".join(self.sessions[t].label for t in new))

        def work():
            # Concurrent across cameras, because they are separate devices and a
            # single slow or unreachable one used to stall every camera behind it.
            # Still inside ONE _run_bg worker, so a manual Fetch Snapshot cannot
            # race the batch on the same camera.
            def load_one(t):
                if self._cancel.is_set():
                    return
                s = self.sessions[t]
                channel = s.sensor if s.vendor.lower().startswith("axis") else s.channel
                try:
                    adapter = vendor_adapter.make_adapter(
                        s.vendor, s.ip, s.port, user, password, channel=channel)
                except Exception as e:                            # noqa: BLE001
                    self.msg_queue.put(("cam_error", (t, f"{type(e).__name__}: {e}")))
                    return

                # Snapshot and analytics are fetched INDEPENDENTLY: a camera with
                # no analytics app still has a picture worth drawing on, and one
                # whose snapshot fails may still have rules worth showing.
                img, scenarios, notes = None, [], []
                try:
                    # attempts=1 on the first pass. Retrying here multiplies an
                    # already four-timeout failure; "Retry failed" exists for that.
                    img = self._fetch_snapshot_with_retry(adapter, t, attempts=1)
                except Exception as e:                            # noqa: BLE001
                    notes.append(f"snapshot failed: {type(e).__name__}: {e}")
                try:
                    scenarios = adapter.read_scenarios()
                except Exception as e:                            # noqa: BLE001
                    notes.append(f"analytics unreadable: {type(e).__name__}: {e}")

                if img is None and not scenarios:
                    if self._try_other_vendor(t, s, notes):
                        return
                    self.msg_queue.put(("cam_error", (t, "; ".join(notes) or "nothing readable")))
                else:
                    self.msg_queue.put(("cam_loaded", (t, img, scenarios, notes)))

            with futures.ThreadPoolExecutor(max_workers=BATCH_CONCURRENCY) as pool:
                list(pool.map(load_one, new))
            self.msg_queue.put(("bg_idle", None))

        # Through _run_bg, NOT a raw thread. The base class serialises all camera
        # work behind one worker, and bypassing it let a manual Fetch Snapshot run
        # at the same time as a multi-load: two PTZ preset moves against one unit
        # at once, and the snapshot read timed out.
        self._cancel.clear()
        self._set_busy(True)
        self._run_bg(self._with_batch_timeout(work))

    def _try_other_vendor(self, t, s, notes):
        """Mixed units: the catalog knows a unit's VENDORS but not which PORT each
        one sits on, so a whole-unit fleet pick can assign the wrong API to one
        camera -- an Axis at 5010 asked for /ISAPI/... 404s on everything (field
        report, 10.23.101.156). When a camera fails COMPLETELY under its assigned
        vendor, try the other one -- with that vendor's own credentials -- before
        declaring it dead. Runs on the worker thread: no Tk access in here."""
        other = "Hikvision" if s.vendor.lower().startswith("axis") else "Axis"
        user, password = self._env_credentials(other)
        if not (user and password):
            notes.append(f"{other} not tried (no credentials for it in access.env)")
            return False
        try:
            adapter = vendor_adapter.make_adapter(
                other, s.ip, s.port, user, password,
                log=lambda m: self.msg_queue.put(("log", m)))
        except Exception as e:                                    # noqa: BLE001
            notes.append(f"{other}: {type(e).__name__}")
            return False
        img, scenarios = None, []
        try:
            img = self._fetch_snapshot_with_retry(adapter, t, attempts=1)
        except Exception as e:                                    # noqa: BLE001
            notes.append(f"{other} snapshot: {type(e).__name__}")
        try:
            scenarios = adapter.read_scenarios()
        except Exception as e:                                    # noqa: BLE001
            notes.append(f"{other} analytics: {type(e).__name__}")
        if img is None and not scenarios:
            return False
        s.vendor = other
        self.msg_queue.put(("log", f"[*] {s.label}: vendor auto-corrected to {other} "
                                   f"-- the picked vendor got no answer, {other} did "
                                   f"(mixed unit). Pushes to it will use {other}."))
        self.msg_queue.put(("cam_loaded", (t, img, scenarios, [])))
        return True

    @staticmethod
    def _with_batch_timeout(fn):
        """Run fn with the snapshot timeout widened, then restore it.

        camera_engine.STRICT_TIMEOUT is read at call time inside fetch_snapshot,
        so raising it here reaches the snapshot without touching the audit tool's
        own reads. Safe because every camera operation is serialised behind one
        worker -- nothing else is mid-request while this is swapped."""
        def wrapped():
            original = camera_engine.STRICT_TIMEOUT
            camera_engine.STRICT_TIMEOUT = BATCH_SNAPSHOT_TIMEOUT
            try:
                fn()
            finally:
                camera_engine.STRICT_TIMEOUT = original
        return wrapped

    def _drain_pending(self):
        """Start the queued cameras once the worker is truly free.

        bg_idle is posted as the batch thread's LAST act, so the thread can still
        be alive for a moment after the message is handled -- starting the next
        batch in that window would be refused by _run_bg and the queue would sit
        forever (no further bg_idle is coming to retry it). Poll briefly instead."""
        if not self._pending_targets:
            return
        if self.worker and self.worker.is_alive():
            self.after(200, self._drain_pending)
            return
        nxt, self._pending_targets = self._pending_targets, []
        self._log(f"[*] Starting the queued batch: {len(nxt)} camera(s).")
        self._load_targets(nxt)

    def cancel_batch(self):
        """Stop after the camera currently being contacted."""
        if not (self.worker and self.worker.is_alive()):
            self._log("[.] Nothing running to cancel.")
            return
        self._cancel.set()
        self._log("[*] Cancelling -- finishing the camera in flight, then stopping. "
                  "A request already sent cannot be interrupted.")

    def clear_all(self):
        """Empty the session and blank the canvas, so a bad batch can be abandoned
        without restarting the whole app."""
        if self.worker and self.worker.is_alive():
            self._log("[!] Still working -- press Cancel first, then Clear.")
            return
        self.sessions.clear()
        self._pending_targets = []
        self.active = None
        self.points, self.exclude_zones, self.current_exclude = [], [], []
        self.perspective_bars, self.current_bar = [], []
        self.existing_scenarios, self.existing_overlays = [], []
        self.edit_label_map = {}
        self.edit_menu.configure(values=[awg.NEW_SCENARIO])
        self.edit_var.set(awg.NEW_SCENARIO)
        self.name_var.set("")
        self.pil_image = self.tk_image = None
        self.img_w = self.img_h = 0
        self.canvas.delete("all")
        self.coord_label.configure(text="Cleared. Load cameras to start again.")
        self._refresh_tabs()
        self._log("[*] Session cleared.")

    def _fetch_snapshot_with_retry(self, adapter, target, attempts=2):
        """One retry on a timeout. A PTZ dome is sent to its analytics preset
        before the capture, and a lens still settling can blow the read timeout
        without anything actually being wrong."""
        last = None
        for attempt in range(attempts):
            try:
                return adapter.fetch_snapshot(log=lambda m: self.msg_queue.put(("log", m)))
            except Exception as e:                                # noqa: BLE001
                last = e
                if "timeout" not in f"{type(e).__name__}{e}".lower() or attempt == attempts - 1:
                    raise
                self.msg_queue.put(("log", f"[.] {target}: snapshot timed out, retrying once "
                                           f"(the lens may still be settling)."))
                if self._cancel.wait(2.0):
                    raise
        raise last

    def _run_bg(self, fn):
        """Same single-flight guard as the base, but the refusal explains the
        situation instead of the bare 'A request is already running.' -- which,
        right after the queue message, read as though the fixes were not in."""
        if self.worker and self.worker.is_alive():
            self._log("[!] Busy: a batch is loading/pushing cameras. Snapshots for "
                      "queued tabs arrive automatically -- wait for the tabs to "
                      "settle, or press Cancel.")
            return
        super()._run_bg(fn)

    def _set_busy(self, busy):
        """Grey every button that would only be refused while camera work is in
        flight. Includes the SIDEBAR's Fetch Snapshot / Read Config / Push: they
        share the single-flight worker with the batch, and the field report was an
        operator following the 'no snapshot yet' hint straight into a refusal."""
        state = "disabled" if busy else "normal"
        for name in ("push_all_button", "load_button", "retry_button", "clear_button",
                     "fetch_button", "read_button", "push_button"):
            b = getattr(self, name, None)
            if b is not None:
                b.configure(state=state)
        # Cancel is the one control that must stay live while work is running.
        if getattr(self, "cancel_button", None) is not None:
            self.cancel_button.configure(state="normal")
        if not busy:
            # Blanket re-enable is wrong for Push: on a read-only camera (fixed
            # thermal) capabilities keep it disabled. Re-gate rather than assume.
            self._apply_capabilities()

    def retry_failed(self):
        """Re-load every camera that errored. A ReadTimeout on a PTZ dome that was
        still settling is worth one more try, and re-typing the whole list is not."""
        failed = [t for t, s in self.sessions.items() if s.error]
        if not failed:
            self._log("[.] No failed cameras to retry.")
            return
        for t in failed:
            self.sessions[t].error = None
            self.sessions[t].loaded = False
        targets = [(self.sessions[t].ip, self.sessions[t].port) for t in failed]
        for t in failed:
            del self.sessions[t]
        self._refresh_tabs()
        self._load_targets(targets)

    # ------------------------------------------------------------------ switching

    def capture_active(self):
        """Fold the canvas's current edit back into the active session."""
        s = self.sessions.get(self.active)
        if s is None or not self.tk_image:
            return
        s.points = [self._canvas_to_frac(x, y) for (x, y) in self.points]
        s.exclusions = [[self._canvas_to_frac(x, y) for (x, y) in z]
                        for z in self.exclude_zones]
        s.name = self.name_var.get()
        s.kind_label = self.rule_var.get()
        s.classes = self._selected_classes()
        s.duration = self.loiter_entry.get().strip()
        s.direction = self._alarm_direction() if s.kind_label == "Line Crossing" else None
        s.editing = self.editing
        s.perspective = [{"height": b.get("height"),
                          "points": [self._canvas_to_frac(x, y) for (x, y) in b.get("points", [])]}
                         for b in self.perspective_bars]

    def switch_to(self, target):
        s = self.sessions.get(target)
        if s is None or target == self.active:
            return
        self.capture_active()
        self._switching = True
        try:
            self.active = target
            # Reflect the new camera in the sidebar without tripping the base
            # class's "camera changed -> wipe everything" guard.
            self.ip_entry.delete(0, "end")
            self.ip_entry.insert(0, s.ip)
            if s.port in awg.PORT_VALUE_TO_LABEL:
                self.port_var.set(awg.PORT_VALUE_TO_LABEL[s.port])
            self.adapter = None
            self._loaded_target = self._form_target()

            # Put the snapshot up FIRST. _display_image deliberately wipes every
            # edit field (right for a fresh capture in the single-camera tool) and
            # it establishes the canvas scale/offset that fraction -> pixel
            # conversion depends on. Restoring before it would be erased by it,
            # and would convert against the previous camera's geometry.
            if s.pil_image is not None:
                self._display_image(s.pil_image)
            else:
                # No snapshot for this camera. The canvas MUST be cleared: leaving
                # the previous camera's image under this tab's name is how someone
                # draws a zone on the wrong scene.
                self.pil_image = self.tk_image = None
                self.img_w = self.img_h = 0
                self.canvas.delete("all")
                # Only point at the buttons once there is actually something for
                # them to do -- a camera still in the batch queue needs patience,
                # not a click that the single-flight worker will refuse.
                if s.error:
                    hint = (f"{s.label}: no snapshot ({s.error}). "
                            f"Use Retry failed, or Fetch Snapshot.")
                elif not s.loaded:
                    hint = (f"{s.label}: still loading -- its snapshot appears "
                            f"when the batch reaches it.")
                else:
                    hint = (f"{s.label}: loaded without a snapshot. "
                            f"Use Fetch Snapshot to try again.")
                self.coord_label.configure(text=hint)

            self.existing_scenarios = s.existing_scenarios
            self.existing_overlays = s.existing_overlays
            self.edit_label_map = self._build_edit_labels(s.existing_scenarios)
            self.edit_menu.configure(values=[awg.NEW_SCENARIO] + list(self.edit_label_map.keys()))
            self.edit_var.set(awg.NEW_SCENARIO)
            self.editing = s.editing

            self.name_var.set(s.name)
            if s.kind_label in (self.rule_menu.cget("values") or []):
                self.rule_var.set(s.kind_label)
            self._refresh_type_frames()
            self._set_classes(s.classes)
            if s.duration:
                self.loiter_entry.delete(0, "end")
                self.loiter_entry.insert(0, s.duration)
            self.points = [self._frac_to_canvas(fx, fy) for (fx, fy) in s.points]
            self.exclude_zones = [[self._frac_to_canvas(fx, fy) for (fx, fy) in z]
                                  for z in s.exclusions]
            self.perspective_bars = [
                {"height": b.get("height"),
                 "points": [self._frac_to_canvas(fx, fy) for (fx, fy) in b.get("points", [])]}
                for b in s.perspective]
            self._redraw()
        finally:
            self._switching = False
        self._refresh_tabs()
        self._log(f"[*] Now editing {s.label} ({target})"
                  + (f" -- {len(s.existing_scenarios)} existing rule(s)." if s.loaded else "."))

    # ------------------------------------------------------------------ push all

    def _push_all(self):
        self.capture_active()
        pending = [s for s in self.sessions.values() if s.dirty]
        if not pending:
            self._log("[!] Nothing to push -- draw a zone and name it on at least one camera.")
            return

        lines = []
        for s in pending:
            verb = "update" if s.editing is not None else "create"
            lines.append(f"  {s.label} [{s.target}]: {verb} '{s.name}' "
                         f"({awg.LABEL_TO_KIND.get(s.kind_label, 'intrusion')}, "
                         f"{len(s.points)} points)")
        if not awg.messagebox.askyesno(
            "Confirm multi-camera write",
            f"Push {len(pending)} change(s) -- one API call per camera:\n\n"
            + "\n".join(lines)
            + "\n\nEach camera is backed up first, and each zone was drawn on that "
              "camera's own snapshot. Cameras not listed are untouched."):
            self._log("[.] Multi-camera push cancelled.")
            return

        # Credentials are resolved per CAMERA VENDOR, on the main thread (mfg_var
        # is Tk state). The entry boxes hold one vendor's pair; on a mixed session
        # -- possible via vendor auto-correct -- the other vendor's cameras must
        # be pushed with their own credentials or every one of them 401s.
        user = self.user_entry.get().strip()
        password = self.pass_entry.get().strip()
        selected = vendor_adapter.camera_engine.classify_manufacturer(self.mfg_var.get())
        creds = {}
        for s in pending:
            if vendor_adapter.camera_engine.classify_manufacturer(s.vendor) == selected \
                    and user and password:
                creds[s.target] = (user, password)
            else:
                eu, ep = self._env_credentials(s.vendor)
                creds[s.target] = (eu or user, ep or password)

        self.push_all_button.configure(state="disabled", text="Pushing...")
        self._log(f"[*] Pushing {len(pending)} camera(s), one at a time ...")

        def work():
            results = []
            for s in pending:
                if self._cancel.is_set():
                    results.append((s.target, False, "cancelled before this camera"))
                    continue
                try:
                    c_user, c_pass = creds[s.target]
                    adapter = vendor_adapter.make_adapter(
                        s.vendor, s.ip, s.port, c_user, c_pass,
                        channel=s.sensor if s.vendor.lower().startswith("axis") else s.channel)
                    sc = vendor_adapter.Scenario(
                        name=s.name.strip(),
                        kind=awg.LABEL_TO_KIND.get(s.kind_label, "intrusion"),
                        points=list(s.points), classes=s.classes,
                        duration=int(s.duration) if str(s.duration).isdigit() else 0,
                        direction=s.direction, exclusions=[list(z) for z in s.exclusions],
                        native_id=s.editing,
                        perspective=s.perspective or None)
                    backup, _verify = adapter.apply_scenario(sc, awg.BACKUP_DIR)
                    results.append((s.target, True, f"OK (backup {backup.name})"))
                except Exception as e:                            # noqa: BLE001
                    results.append((s.target, False, f"{type(e).__name__}: {e}"))
            self.msg_queue.put(("push_all_done", results))
        self._cancel.clear()
        self._set_busy(True)
        self._run_bg(self._with_batch_timeout(work))

    # ------------------------------------------------------------------ queue

    def _poll_queue(self):
        try:
            while True:
                msg = self.msg_queue.get_nowait()
                kind = msg[0]
                if kind == "cam_loaded":
                    target, img, scenarios, notes = msg[1]
                    s = self.sessions[target]
                    s.pil_image, s.existing_scenarios, s.loaded = img, scenarios, True
                    s.analytics_error = next((n for n in notes if n.startswith("analytics")), None)
                    for n in notes:
                        self._log(f"[!] {target}: {n}")
                    s.existing_overlays = [
                        {"name": x.name, "kind": "fence" if x.kind == "line" else "area",
                         "verts": x.points,
                         "direction": x.direction if x.kind == "line" else None}
                        for x in scenarios]
                    self._log(f"[+] {target}: "
                              + ("snapshot " if img is not None else "NO snapshot ")
                              + f"+ {len(scenarios)} rule(s)."
                              + ("  Draw and push still work." if img is not None
                                 and s.analytics_error else ""))
                    if self.active is None:
                        self.switch_to(target)
                    self._refresh_tabs()
                elif kind == "cam_error":
                    target, err = msg[1]
                    self.sessions[target].error = err
                    self._log(f"[!] {target}: {err}")
                    self._refresh_tabs()
                elif kind == "bg_idle":
                    self._set_busy(False)
                    if self._cancel.is_set():
                        # Cancel means STOP -- auto-starting the queue right after
                        # would un-cancel the operator's decision.
                        dropped = len(self._pending_targets)
                        self._pending_targets = []
                        self._log("[*] Batch cancelled."
                                  + (f" {dropped} queued camera(s) dropped." if dropped else "")
                                  + " Clear, or Load cameras again.")
                        self._cancel.clear()
                    elif self._pending_targets:
                        self._drain_pending()
                elif kind == "log":
                    self._log(msg[1])
                elif kind == "push_all_done":
                    self._set_busy(False)
                    self.push_all_button.configure(text="Push ALL edited cameras")
                    ok = [r for r in msg[1] if r[1]]
                    bad = [r for r in msg[1] if not r[1]]
                    self._log(f"[+] Pushed {len(ok)}/{len(msg[1])} camera(s):")
                    for target, good, detail in msg[1]:
                        self._log(f"    {'[+]' if good else '[!]'} {target}: {detail}")
                    if bad:
                        awg.messagebox.showwarning(
                            "Some cameras failed",
                            "\n".join(f"{t}: {d}" for t, _g, d in bad))
                    self._refresh_tabs()
                else:
                    self.msg_queue.put(msg)
                    break
        except queue.Empty:
            pass
        # Hand anything not handled here to the single-camera writer's own poller,
        # which owns snapshot/read/push/restore messages.
        super()._poll_queue()


if __name__ == "__main__":
    MultiWriterApp().mainloop()
