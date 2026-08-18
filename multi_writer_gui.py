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

import tkinter as tk

import customtkinter as ctk

import analytics_writer_gui as awg
import vendor_adapter
from analytics_writer_gui import (LVT_DARK_TEAL, LVT_DARK_TEAL_HOVER, LVT_LIGHT,
                                  LVT_TEAL, LVT_TEAL_HOVER, LVT_TEXT_DARK,
                                  LVT_TEXT_MUTED, WriterApp)


class CameraSession:
    """One camera's editing state, in vendor-neutral terms.

    Everything geometric is a [0,1] fraction so it is independent of canvas size
    and directly pushable; nothing here references a Tk widget."""

    def __init__(self, ip, port, vendor, sensor=None, channel=None):
        self.ip, self.port, self.vendor = ip, str(port), vendor
        self.sensor, self.channel = sensor, channel
        self.loaded = False          # snapshot + existing rules fetched
        self.error = None
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
        return f"{self.ip}:{self.port}"

    @property
    def dirty(self):
        """Has anything been drawn that could be pushed?"""
        return bool(self.points) and bool(self.name.strip())

    def status(self):
        if self.error:
            return "error"
        if not self.loaded:
            return "loading"
        return "edited" if self.dirty else "loaded"


class MultiWriterApp(WriterApp):
    def __init__(self):
        self.sessions = {}           # target -> CameraSession
        self.active = None           # target currently on the canvas
        self._switching = False      # suppress the base class's camera-change reset
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
        ctk.CTkButton(top, text="Retry failed", width=110, command=self.retry_failed,
                      fg_color=LVT_TEAL, hover_color=LVT_TEAL_HOVER).pack(side="right", padx=6)
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
        marks = {"loading": "...", "error": "!", "edited": "*", "loaded": ""}
        for target, s in self.sessions.items():
            mark = marks[s.status()]
            label = f"{target}{('  ' + mark) if mark else ''}"
            active = target == self.active
            ctk.CTkButton(
                self.tabs, text=label, width=170, height=28,
                command=lambda t=target: self.switch_to(t),
                fg_color=LVT_DARK_TEAL if active else LVT_TEAL,
                hover_color=LVT_DARK_TEAL_HOVER if active else LVT_TEAL_HOVER,
                font=ctk.CTkFont(size=11, weight="bold" if active else "normal"),
            ).pack(side="left", padx=(0, 6))
        edited = [t for t, s in self.sessions.items() if s.dirty]
        ctk.CTkLabel(self.tabs,
                     text=f"   * = edited ({len(edited)} ready to push)",
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

    def _load_targets(self, targets):
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
            self.sessions[t] = CameraSession(ip, port, vendor, sensor, channel)
            new.append(t)
        self._refresh_tabs()
        if not new:
            self._log("[.] Those cameras are already loaded.")
            return
        self._log(f"[*] Loading {len(new)} camera(s): {', '.join(new)}")

        def work():
            # Sequential: these cameras lock an account after a few bad auths, and a
            # parallel burst across a fleet is how one wrong password locks it out.
            for t in new:
                s = self.sessions[t]
                try:
                    adapter = vendor_adapter.make_adapter(
                        s.vendor, s.ip, s.port, user, password,
                        channel=s.sensor if s.vendor.lower().startswith("axis") else s.channel)
                    img = adapter.fetch_snapshot(log=lambda m: self.msg_queue.put(("log", m)))
                    scenarios = adapter.read_scenarios()
                    self.msg_queue.put(("cam_loaded", (t, img, scenarios)))
                except Exception as e:                            # noqa: BLE001
                    self.msg_queue.put(("cam_error", (t, f"{type(e).__name__}: {e}")))
            self.msg_queue.put(("bg_idle", None))

        # Through _run_bg, NOT a raw thread. The base class serialises all camera
        # work behind one worker, and bypassing it let a manual Fetch Snapshot run
        # at the same time as a multi-load: two PTZ preset moves against one unit
        # at once, and the snapshot read timed out.
        self._set_busy(True)
        self._run_bg(work)

    def _set_busy(self, busy):
        """Grey the multi-camera buttons while any camera work is in flight, so a
        second click cannot queue work the base class will just refuse."""
        state = "disabled" if busy else "normal"
        for b in (getattr(self, "push_all_button", None), getattr(self, "load_button", None)):
            if b is not None:
                b.configure(state=state)

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
                self.coord_label.configure(
                    text=f"{s.target}: no snapshot ({s.error or 'not loaded yet'}). "
                         f"Use Retry failed, or Fetch Snapshot.")

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
        self._log(f"[*] Now editing {target}"
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
            lines.append(f"  {s.target}: {verb} '{s.name}' "
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

        user = self.user_entry.get().strip()
        password = self.pass_entry.get().strip()
        self.push_all_button.configure(state="disabled", text="Pushing...")
        self._log(f"[*] Pushing {len(pending)} camera(s), one at a time ...")

        def work():
            results = []
            for s in pending:
                try:
                    adapter = vendor_adapter.make_adapter(
                        s.vendor, s.ip, s.port, user, password,
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
        self._set_busy(True)
        self._run_bg(work)

    # ------------------------------------------------------------------ queue

    def _poll_queue(self):
        try:
            while True:
                msg = self.msg_queue.get_nowait()
                kind = msg[0]
                if kind == "cam_loaded":
                    target, img, scenarios = msg[1]
                    s = self.sessions[target]
                    s.pil_image, s.existing_scenarios, s.loaded = img, scenarios, True
                    s.existing_overlays = [
                        {"name": x.name, "kind": "fence" if x.kind == "line" else "area",
                         "verts": x.points,
                         "direction": x.direction if x.kind == "line" else None}
                        for x in scenarios]
                    self._log(f"[+] {target}: snapshot + {len(scenarios)} rule(s).")
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
