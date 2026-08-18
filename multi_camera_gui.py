"""Multi-Camera Dry Run -- plan one analytics change across many cameras.

A double-clickable front end for dry_run.py, in the same shape as the other two
tools (Run Multi-Camera Dry Run.bat). It shares dry_run's planner and its
renderer, so the report here is byte-identical to the CLI's -- two renderers
would drift, and the report IS the product.

READ-ONLY BY CONSTRUCTION. This window has no Apply button because there is no
apply path yet: dry_run.py contains no write call and a test asserts that by
scanning its source. When an apply step exists it should be a deliberate,
separate action -- a dry run must never be one click away from a fleet write.

Scope is non-geometric on purpose: dwell, classes, names. Those are safe to fan
out because they carry no scene correspondence (20 seconds means the same thing
on every camera; the same polygon does not). Copying geometry between cameras
needs a per-camera preview or a confidence gate, and is not this tool.
"""

import queue
import threading
import tkinter as tk

import customtkinter as ctk

import dry_run
import ui_theme

LVT_LIGHT = ui_theme.LVT_LIGHT
LVT_TEAL = ui_theme.LVT_TEAL
LVT_TEAL_HOVER = ui_theme.LVT_TEAL_HOVER
LVT_DARK_TEAL = ui_theme.LVT_DARK_TEAL
LVT_DARK_TEAL_HOVER = ui_theme.LVT_DARK_TEAL_HOVER
LVT_TEXT_DARK = ui_theme.LVT_TEXT_DARK
LVT_TEXT_MUTED = ui_theme.LVT_TEXT_MUTED
LVT_SURFACE = ui_theme.LVT_SURFACE
LVT_LOG_BG = ui_theme.LVT_LOG_BG
LVT_LOG_TEXT = ui_theme.LVT_LOG_TEXT
LVT_ON_TEAL = ui_theme.LVT_ON_TEAL

PORTS = {"Center (5010)": "5010", "Left (5015)": "5015", "Right (5020)": "5020"}
ANY = "(any)"

STARTUP_APPEARANCE = ui_theme.init_appearance()


class MultiCameraApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Multi-Camera Dry Run")
        self.configure(fg_color=LVT_SURFACE)
        self.msg_queue = queue.Queue()
        self._build()
        self._apply_startup_geometry(1180, 760)
        ui_theme.on_appearance_change(lambda _m: None, self)
        self.after(100, self._poll)

    # ------------------------------------------------------------------ layout

    def _apply_startup_geometry(self, want_w, want_h):
        """Open at a size the screen can actually show. winfo_screen* report
        DPI-VIRTUALISED units while CustomTkinter scales whatever geometry() is
        handed, so budget in physical pixels then divide back out -- the same trap
        that once produced a window taller than the display."""
        try:
            sf = ctk.ScalingTracker.get_window_scaling(self) or 1.0
        except Exception:                                          # noqa: BLE001
            sf = 1.0
        max_w = int(self.winfo_screenwidth() * sf * 0.92)
        max_h = int(self.winfo_screenheight() * sf * 0.88)
        w, h = min(int(want_w * sf), max_w), min(int(want_h * sf), max_h)
        self.geometry(f"{int(w / sf)}x{int(h / sf)}+40+30")
        self.minsize(int(min(900, w) / sf), int(min(560, h) / sf))

    def _build(self):
        header = ctk.CTkFrame(self, fg_color=LVT_DARK_TEAL, corner_radius=0)
        header.pack(fill="x")
        ctk.CTkLabel(header, text="Multi-Camera Dry Run", text_color=LVT_ON_TEAL,
                     font=ctk.CTkFont(size=20, weight="bold")).pack(
            side="left", padx=16, pady=12)
        ctk.CTkLabel(header, text="Plan one change across many cameras -- nothing is written",
                     text_color=LVT_ON_TEAL, font=ctk.CTkFont(size=12)).pack(
            side="left", padx=4)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=12, pady=12)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        side = ctk.CTkScrollableFrame(body, fg_color=LVT_LIGHT, width=340)
        side.grid(row=0, column=0, sticky="nsw", padx=(0, 12))
        self._build_side(side)

        right = ctk.CTkFrame(body, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_rowconfigure(1, weight=1)
        right.grid_columnconfigure(0, weight=1)
        self.status = ctk.CTkLabel(right, text="Enter cameras, choose a change, then Run Dry Run.",
                                   text_color=LVT_TEXT_MUTED, anchor="w")
        self.status.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self.out = ctk.CTkTextbox(right, fg_color=LVT_LOG_BG, text_color=LVT_LOG_TEXT,
                                  font=ctk.CTkFont(family="Consolas", size=12),
                                  wrap="none")
        self.out.grid(row=1, column=0, sticky="nsew")
        self.out.configure(state="disabled")

    def _section(self, parent, text):
        ctk.CTkLabel(parent, text=text, text_color=LVT_TEXT_DARK,
                     font=ctk.CTkFont(size=13, weight="bold")).pack(
            anchor="w", padx=12, pady=(12, 2))

    def _build_side(self, side):
        self._section(side, "Cameras")
        ctk.CTkLabel(side, text="One IP per line. Add :port to override the boxes below.",
                     text_color=LVT_TEXT_MUTED, font=ctk.CTkFont(size=10),
                     justify="left", wraplength=300).pack(anchor="w", padx=12)
        self.ips = ctk.CTkTextbox(side, height=110, font=ctk.CTkFont(family="Consolas", size=12))
        self.ips.pack(fill="x", padx=12, pady=(4, 6))

        self.port_vars = {}
        row = ctk.CTkFrame(side, fg_color="transparent")
        row.pack(fill="x", padx=12)
        for label in PORTS:
            v = tk.BooleanVar(value=True)
            self.port_vars[label] = v
            ctk.CTkCheckBox(row, text=label, variable=v, fg_color=LVT_TEAL,
                            hover_color=LVT_TEAL_HOVER,
                            font=ctk.CTkFont(size=11)).pack(anchor="w", pady=1)

        self._section(side, "Credentials")
        self.vendor_var = tk.StringVar(value="Axis")
        ctk.CTkOptionMenu(side, values=["Axis", "Hikvision"], variable=self.vendor_var,
                          fg_color=LVT_TEAL, button_color=LVT_TEAL,
                          button_hover_color=LVT_TEAL_HOVER).pack(fill="x", padx=12, pady=2)
        self.user = ctk.CTkEntry(side, placeholder_text="username")
        self.user.pack(fill="x", padx=12, pady=2)
        self.password = ctk.CTkEntry(side, placeholder_text="password", show="*")
        self.password.pack(fill="x", padx=12, pady=2)
        ctk.CTkLabel(side, text="Left blank, access.env is used (same as the other tools).",
                     text_color=LVT_TEXT_MUTED, font=ctk.CTkFont(size=10),
                     justify="left", wraplength=300).pack(anchor="w", padx=12)

        self._section(side, "Which rules to match")
        self.kind_var = tk.StringVar(value=ANY)
        ctk.CTkOptionMenu(side, values=[ANY, "intrusion", "line", "loiter"],
                          variable=self.kind_var, fg_color=LVT_TEAL, button_color=LVT_TEAL,
                          button_hover_color=LVT_TEAL_HOVER).pack(fill="x", padx=12, pady=2)
        self.class_var = tk.StringVar(value=ANY)
        ctk.CTkOptionMenu(side, values=[ANY, "human", "vehicle"], variable=self.class_var,
                          fg_color=LVT_TEAL, button_color=LVT_TEAL,
                          button_hover_color=LVT_TEAL_HOVER).pack(fill="x", padx=12, pady=2)
        self.name_re = ctk.CTkEntry(side, placeholder_text="name matches regex (optional)")
        self.name_re.pack(fill="x", padx=12, pady=2)

        self._section(side, "What to change")
        self.duration = ctk.CTkEntry(side, placeholder_text="set dwell seconds (optional)")
        self.duration.pack(fill="x", padx=12, pady=2)
        self.classes = ctk.CTkEntry(side, placeholder_text="set classes, e.g. human,vehicle")
        self.classes.pack(fill="x", padx=12, pady=2)
        self.rename_from = ctk.CTkEntry(side, placeholder_text="rename: find (regex)")
        self.rename_from.pack(fill="x", padx=12, pady=2)
        self.rename_to = ctk.CTkEntry(side, placeholder_text="rename: replace with")
        self.rename_to.pack(fill="x", padx=12, pady=2)
        ctk.CTkLabel(side, text="Geometry is never changed by this tool -- only dwell, "
                               "classes and names.",
                     text_color=LVT_TEXT_MUTED, font=ctk.CTkFont(size=10),
                     justify="left", wraplength=300).pack(anchor="w", padx=12, pady=(4, 0))

        self.run_button = ctk.CTkButton(side, text="Run Dry Run", command=self._run,
                                        fg_color=LVT_DARK_TEAL, hover_color=LVT_DARK_TEAL_HOVER,
                                        height=40, font=ctk.CTkFont(size=13, weight="bold"))
        self.run_button.pack(fill="x", padx=12, pady=(14, 4))
        ctk.CTkLabel(side, text="Read-only: this window cannot write to a camera.",
                     text_color=LVT_TEXT_MUTED, font=ctk.CTkFont(size=10),
                     justify="left", wraplength=300).pack(anchor="w", padx=12, pady=(0, 12))

    # ------------------------------------------------------------------ run

    def _targets(self):
        """[(ip, port)] from the IP box crossed with the ticked position boxes.
        An explicit ip:port in the box wins, so a one-off camera needs no ticking."""
        ports = [PORTS[k] for k, v in self.port_vars.items() if v.get()]
        out = []
        for raw in self.ips.get("1.0", "end").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            if ":" in raw:
                ip, _, port = raw.partition(":")
                if ip.strip() and port.strip():
                    out.append((ip.strip(), port.strip()))
                    continue
            for p in ports:
                out.append((raw, p))
        return out

    def _spec(self):
        dur = self.duration.get().strip()
        classes = self.classes.get().strip()
        rf = self.rename_from.get().strip()
        return {
            "vendor": self.vendor_var.get(),
            "kind": None if self.kind_var.get() == ANY else self.kind_var.get(),
            "cls": None if self.class_var.get() == ANY else self.class_var.get(),
            "name_re": self.name_re.get().strip() or None,
            "duration": int(dur) if dur.lstrip("-").isdigit() else None,
            "classes": [c.strip() for c in classes.split(",")] if classes else None,
            "rename_from": rf or None,
            "rename_to": self.rename_to.get(),
        }

    def _write(self, lines):
        self.out.configure(state="normal")
        self.out.delete("1.0", "end")
        self.out.insert("end", "\n".join(lines))
        self.out.configure(state="disabled")

    def _run(self):
        targets = self._targets()
        if not targets:
            self._write(["Enter at least one camera IP, and tick at least one position."])
            return
        spec = self._spec()
        dur_raw = self.duration.get().strip()
        if dur_raw and spec["duration"] is None:
            self._write([f"'{dur_raw}' is not a whole number of seconds."])
            return

        user, password = dry_run.credentials(spec["vendor"], self.user.get().strip() or None,
                                             self.password.get().strip() or None)
        if not (user and password):
            self._write(["No credentials: fill them in, or add them to access.env."])
            return

        self.run_button.configure(state="disabled", text="Planning...")
        self.status.configure(text=f"Reading {len(targets)} camera(s) -- nothing is being written.")

        def work():
            try:
                # Sequential on purpose: these cameras lock an account out after a
                # handful of bad auths, and a burst of parallel reads across a
                # fleet is exactly how one wrong password becomes a lockout.
                plans = [dry_run.plan_camera(ip, port, user, password, spec)
                         for ip, port in targets]
                self.msg_queue.put(("done", dry_run.render_lines(plans, spec)))
            except Exception as e:                                # noqa: BLE001
                self.msg_queue.put(("err", f"{type(e).__name__}: {e}"))

        threading.Thread(target=work, daemon=True).start()

    def _poll(self):
        try:
            while True:
                kind, payload = self.msg_queue.get_nowait()
                self.run_button.configure(state="normal", text="Run Dry Run")
                if kind == "done":
                    lines, blocked = payload
                    self._write(lines)
                    self.status.configure(
                        text=("Plan ready -- nothing was written."
                              + (f"  {blocked} rule(s) BLOCKED." if blocked else "")))
                else:
                    self._write([payload])
                    self.status.configure(text="Failed.")
        except queue.Empty:
            pass
        self.after(120, self._poll)


if __name__ == "__main__":
    MultiCameraApp().mainloop()
