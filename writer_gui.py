"""LiveView Technologies -- Axis Analytics Writer (working concept).

The write-side sibling of gui_app.py. Point at one Axis camera, pull a live snapshot,
DRAW an analytics zone/line on it, and (once Step 0 schema validation is done) push it
to AXIS Object Analytics -- no camera web UI required.

Run with: python writer_gui.py

Status of the concept:
    * Connect + fetch snapshot ......... working (read-only, safe)
    * Draw zone/line -> normalized coords working
    * Read current AOA scenarios ....... working (read-only, safe)
    * Push scenario to camera .......... GATED. Set WRITE_ENABLED=True only after
      probe_aoa.py confirms the setConfiguration schema on your firmware (Step 0/2).
"""

import os
import queue
import re
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk
from PIL import Image, ImageTk

import aoa_config

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# --- LiveView Technologies brand palette (lifted from gui_app.py) ---
LVT_LIGHT = "#E5F5F5"
LVT_TEAL = "#00A19A"
LVT_TEAL_HOVER = "#008680"
LVT_DARK_TEAL = "#00726E"
LVT_DARK_TEAL_HOVER = "#005B58"
LVT_TEXT_DARK = "#1A1D27"
LVT_TEXT_MUTED = "#6B7A79"
LVT_WHITE = "#FFFFFF"
LVT_LOG_BG = "#0F1117"
LVT_LOG_TEXT = "#D6EFEF"
ZONE_OUTLINE = "#00E5DA"
ZONE_FILL = "#00A19A"
EXCL_OUTLINE = "#FF6B6B"  # exclusion zones drawn in red to read as "ignore here"
EXCL_FILL = "#E03131"
EXISTING_OUTLINE = "#F7B500"  # scenarios already on the camera, shown as amber reference

# Write path validated live against 10.23.164.21:5010 (AOA 1.6): admin write confirmed,
# setConfiguration round-trip + add/verify/restore all proven. Gate is open.
WRITE_ENABLED = True

CANVAS_W, CANVAS_H = 900, 506  # 16:9 drawing surface
BACKUP_DIR = Path(__file__).with_name("aoa_backups")
NEW_SCENARIO = "(new scenario)"

ctk.set_appearance_mode("light")


class WriterApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("LiveView Technologies -- Axis Analytics Writer")
        self.geometry("1180x860")
        self.minsize(1040, 760)
        self.configure(fg_color=LVT_WHITE)

        self.msg_queue = queue.Queue()
        self.worker = None

        # Drawing / image state
        self.pil_image = None          # original full-res PIL image
        self.tk_image = None           # scaled ImageTk for display
        self.img_w = self.img_h = 0    # original dims (coords normalize against these)
        self.scale = 1.0               # display scale factor
        self.offset_x = self.offset_y = 0
        self.points = []               # canvas-pixel points for the INCLUDE zone/line
        self.exclude_zones = []        # list of finished exclusion polygons (canvas px)
        self.current_exclude = []      # exclusion polygon currently being drawn
        self.existing_overlays = []    # scenarios already on the camera (normalized verts)
        self.existing_scenarios = []   # full scenario dicts from the last Read Current Config
        self.editing = None            # original scenario dict being edited, or None = new

        self._build_header()
        self._build_body()
        self._prefill_credentials()
        self.after(100, self._poll_queue)

    # -------------------------------------------------------------- layout
    def _build_header(self):
        header = ctk.CTkFrame(self, fg_color=LVT_DARK_TEAL, corner_radius=0, height=72)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)
        ctk.CTkLabel(header, text="Axis Analytics Writer", font=ctk.CTkFont(size=22, weight="bold"),
                     text_color=LVT_WHITE).pack(side="left", padx=24, pady=10)
        ctk.CTkLabel(header, text="Draw analytics scenarios and push them to the camera -- no web UI",
                     font=ctk.CTkFont(size=12), text_color=LVT_LIGHT).pack(side="left", padx=(0, 24), pady=(20, 10))

    def _build_body(self):
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=16, pady=12)
        body.grid_columnconfigure(0, weight=0)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        self._build_sidebar(body)
        self._build_canvas_area(body)

    def _build_sidebar(self, parent):
        side = ctk.CTkScrollableFrame(parent, fg_color=LVT_LIGHT, corner_radius=10, width=300)
        side.grid(row=0, column=0, sticky="nsew", padx=(0, 12))

        def label(txt, pad=(10, 2)):
            ctk.CTkLabel(side, text=txt, text_color=LVT_TEXT_DARK,
                         font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w", padx=12, pady=pad)

        label("Camera", (12, 2))
        self.ip_entry = ctk.CTkEntry(side, placeholder_text="Camera IP e.g. 10.23.66.205")
        self.ip_entry.pack(fill="x", padx=12, pady=4)

        self.port_var = tk.StringVar(value="5015")
        ctk.CTkLabel(side, text="Port (LVT: 5010=Center, 5015=Left, 5020=Right)",
                     text_color=LVT_TEXT_MUTED, font=ctk.CTkFont(size=10)).pack(anchor="w", padx=12)
        ctk.CTkOptionMenu(side, values=["5010", "5015", "5020", "80"], variable=self.port_var,
                          fg_color=LVT_TEAL, button_color=LVT_DARK_TEAL,
                          button_hover_color=LVT_DARK_TEAL_HOVER).pack(fill="x", padx=12, pady=4)

        self.user_entry = ctk.CTkEntry(side, placeholder_text="Axis username")
        self.user_entry.pack(fill="x", padx=12, pady=4)
        self.pass_entry = ctk.CTkEntry(side, placeholder_text="Axis password", show="*")
        self.pass_entry.pack(fill="x", padx=12, pady=4)

        ctk.CTkButton(side, text="Fetch Snapshot", command=self._fetch_snapshot,
                      fg_color=LVT_DARK_TEAL, hover_color=LVT_DARK_TEAL_HOVER).pack(fill="x", padx=12, pady=(8, 4))
        ctk.CTkButton(side, text="Read Current Config", command=self._read_config,
                      fg_color=LVT_TEAL, hover_color=LVT_TEAL_HOVER).pack(fill="x", padx=12, pady=4)

        ctk.CTkLabel(side, text="Edit existing (or create new)", text_color=LVT_TEXT_MUTED,
                     font=ctk.CTkFont(size=10)).pack(anchor="w", padx=12, pady=(6, 0))
        self.edit_var = tk.StringVar(value=NEW_SCENARIO)
        self.edit_menu = ctk.CTkOptionMenu(side, values=[NEW_SCENARIO], variable=self.edit_var,
                                           command=self._on_edit_select, fg_color=LVT_TEAL,
                                           button_color=LVT_DARK_TEAL, button_hover_color=LVT_DARK_TEAL_HOVER)
        self.edit_menu.pack(fill="x", padx=12, pady=4)

        label("Scenario type")
        self.rule_var = tk.StringVar(value="Intrusion")
        ctk.CTkOptionMenu(side, values=["Intrusion", "Line Crossing", "Loitering"], variable=self.rule_var,
                          command=self._on_rule_change, fg_color=LVT_TEAL, button_color=LVT_DARK_TEAL,
                          button_hover_color=LVT_DARK_TEAL_HOVER).pack(fill="x", padx=12, pady=4)

        # AOA caps scenario names at 15 chars (maxLengthName from capabilities) -- enforce
        # visibly here instead of letting the write fail or silently truncate.
        self.name_var = tk.StringVar()
        self.name_var.trace_add("write", self._limit_name_len)
        self.name_entry = ctk.CTkEntry(side, placeholder_text="Scenario name (max 15)", textvariable=self.name_var)
        self.name_entry.pack(fill="x", padx=12, pady=4)

        self.class_human = ctk.CTkCheckBox(side, text="Human", fg_color=LVT_TEAL, hover_color=LVT_TEAL_HOVER)
        self.class_human.select()
        self.class_human.pack(anchor="w", padx=12, pady=2)
        self.class_vehicle = ctk.CTkCheckBox(side, text="Vehicle", fg_color=LVT_TEAL, hover_color=LVT_TEAL_HOVER)
        self.class_vehicle.pack(anchor="w", padx=12, pady=2)

        self.loiter_frame = ctk.CTkFrame(side, fg_color="transparent")
        ctk.CTkLabel(self.loiter_frame, text="Loiter seconds", text_color=LVT_TEXT_MUTED,
                     font=ctk.CTkFont(size=10)).pack(anchor="w")
        self.loiter_entry = ctk.CTkEntry(self.loiter_frame, placeholder_text="e.g. 10")
        self.loiter_entry.pack(fill="x")
        # hidden unless Loitering selected

        label("Drawing")
        self.mode_var = tk.StringVar(value="Include")
        self.mode_toggle = ctk.CTkSegmentedButton(
            side, values=["Include", "Exclude"], variable=self.mode_var, command=self._on_mode_change,
            selected_color=LVT_DARK_TEAL, selected_hover_color=LVT_DARK_TEAL_HOVER,
            unselected_color=LVT_TEAL, unselected_hover_color=LVT_TEAL_HOVER)
        self.mode_toggle.pack(fill="x", padx=12, pady=4)
        self.mode_hint = ctk.CTkLabel(side, text="Include = detect here (teal). Exclude = ignore here (red).",
                                      text_color=LVT_TEXT_MUTED, font=ctk.CTkFont(size=10), justify="left")
        self.mode_hint.pack(anchor="w", padx=12)

        self.finish_excl_button = ctk.CTkButton(side, text="Finish Exclusion Zone", command=self._finish_exclude,
                                                fg_color=EXCL_FILL, hover_color="#B02525")
        self.finish_excl_button.pack(fill="x", padx=12, pady=4)

        row = ctk.CTkFrame(side, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=4)
        ctk.CTkButton(row, text="Undo Point", width=120, command=self._undo_point,
                      fg_color=LVT_TEAL, hover_color=LVT_TEAL_HOVER).pack(side="left", padx=(0, 4))
        ctk.CTkButton(row, text="Clear All", width=120, command=self._clear_points,
                      fg_color=LVT_TEAL, hover_color=LVT_TEAL_HOVER).pack(side="left")

        self.push_button = ctk.CTkButton(side, text="Push to Camera", command=self._push,
                                          fg_color=LVT_DARK_TEAL, hover_color=LVT_DARK_TEAL_HOVER,
                                          font=ctk.CTkFont(size=13, weight="bold"), height=40)
        self.push_button.pack(fill="x", padx=12, pady=(12, 4))

        self.restore_button = ctk.CTkButton(side, text="Restore from Backup...", command=self._restore_backup,
                                             fg_color=LVT_TEAL, hover_color=LVT_TEAL_HOVER)
        self.restore_button.pack(fill="x", padx=12, pady=4)

        if not WRITE_ENABLED:
            self.push_button.configure(state="disabled", text="Push to Camera (locked)")
            ctk.CTkLabel(side, text="Writing is locked until probe_aoa.py confirms the\n"
                                    "setConfiguration schema on this firmware (Step 0).",
                         text_color=LVT_TEXT_MUTED, font=ctk.CTkFont(size=10), justify="left").pack(
                anchor="w", padx=12, pady=(0, 8))

    def _build_canvas_area(self, parent):
        right = ctk.CTkFrame(parent, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_rowconfigure(0, weight=0)
        right.grid_rowconfigure(1, weight=1)
        right.grid_columnconfigure(0, weight=1)

        self.coord_label = ctk.CTkLabel(right, text="No snapshot loaded. Enter camera details and Fetch Snapshot.",
                                        text_color=LVT_TEXT_MUTED, anchor="w")
        self.coord_label.grid(row=0, column=0, sticky="ew", pady=(0, 6))

        self.canvas = tk.Canvas(right, width=CANVAS_W, height=CANVAS_H, bg="#0B0D12",
                                highlightthickness=1, highlightbackground=LVT_DARK_TEAL)
        self.canvas.grid(row=1, column=0, sticky="nw")
        self.canvas.bind("<Button-1>", self._on_canvas_click)
        self.canvas.bind("<Motion>", self._on_canvas_motion)

        self.log_box = ctk.CTkTextbox(right, fg_color=LVT_LOG_BG, text_color=LVT_LOG_TEXT,
                                      font=ctk.CTkFont(family="Consolas", size=11), wrap="word", height=150)
        self.log_box.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self.log_box.configure(state="disabled")

    # -------------------------------------------------------------- behavior
    def _log(self, text):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _limit_name_len(self, *_):
        v = self.name_var.get()
        if len(v) > aoa_config.MAX_NAME_LEN:
            self.name_var.set(v[:aoa_config.MAX_NAME_LEN])

    def _on_rule_change(self, _value=None):
        if self.rule_var.get() == "Loitering":
            self.loiter_frame.pack(fill="x", padx=12, pady=4)
        else:
            self.loiter_frame.pack_forget()
        if self.rule_var.get() == "Line Crossing":
            self.mode_var.set("Include")  # no exclusion for line crossing
        self._clear_points()

    def _set_classes(self, types):
        (self.class_human.select() if "human" in types else self.class_human.deselect())
        (self.class_vehicle.select() if "vehicle" in types else self.class_vehicle.deselect())

    def _on_edit_select(self, choice):
        if choice == NEW_SCENARIO:
            self.editing = None
            self.name_var.set("")
            self.points, self.exclude_zones, self.current_exclude = [], [], []
            self._redraw()
            self._log("[.] New scenario mode.")
            return
        scenario = next((s for s in self.existing_scenarios if s.get("name") == choice), None)
        if scenario is None:
            return
        if not self.tk_image:
            self._log("[!] Read Current Config first so the snapshot is loaded.")
            return

        self.editing = scenario
        self.name_var.set(scenario.get("name", ""))
        self._set_classes([c.get("type") for c in scenario.get("objectClassifications", [])])

        trig = (scenario.get("triggers") or [{}])[0]
        ttype = trig.get("type")
        conds = trig.get("conditions") or []
        is_loiter = any(c.get("type") == "individualTimeInArea" for c in conds)
        if ttype in ("fence", "countingLine"):
            self.rule_var.set("Line Crossing")
        elif is_loiter:
            self.rule_var.set("Loitering")
        else:
            self.rule_var.set("Intrusion")

        # Show/hide loiter field WITHOUT _on_rule_change (which would clear the geometry).
        if self.rule_var.get() == "Loitering":
            self.loiter_frame.pack(fill="x", padx=12, pady=4)
            secs = ""
            for c in conds:
                for d in c.get("data", []):
                    if d.get("time"):
                        secs = str(d["time"])
                        break
            self.loiter_entry.delete(0, "end")
            self.loiter_entry.insert(0, secs)
        else:
            self.loiter_frame.pack_forget()

        self.points = [self._norm_to_canvas(float(x), float(y)) for x, y in trig.get("vertices", [])]
        self.exclude_zones = [[self._norm_to_canvas(float(x), float(y)) for x, y in f.get("vertices", [])]
                              for f in scenario.get("filters", []) if f.get("type") == "excludeArea"]
        self.current_exclude = []
        self.mode_var.set("Include")
        self._redraw()
        self._log(f"[.] Editing '{choice}' (id {scenario.get('id')}). Non-editable settings "
                  f"(perspective, size filters) are preserved. Adjust and Push to update.")

    def _make_client(self):
        ip = self.ip_entry.get().strip()
        user = self.user_entry.get().strip()
        password = self.pass_entry.get().strip()
        if not (ip and user and password):
            self._log("[!] Enter IP, username, and password first.")
            return None
        return aoa_config.AOAClient(ip, user, password, self.port_var.get())

    def _run_bg(self, fn):
        if self.worker and self.worker.is_alive():
            self._log("[!] A request is already running.")
            return
        self.worker = threading.Thread(target=fn, daemon=True)
        self.worker.start()

    def _fetch_snapshot(self):
        client = self._make_client()
        if not client:
            return
        self._log(f"[*] Fetching snapshot from {client.control_url.rsplit('/local', 1)[0]} ...")

        def work():
            try:
                img = client.fetch_snapshot()
                self.msg_queue.put(("snapshot", img))
            except Exception as e:
                self.msg_queue.put(("log", f"[!] Snapshot failed: {e}"))
        self._run_bg(work)

    def _read_config(self):
        client = self._make_client()
        if not client:
            return
        need_snap = self.tk_image is None
        self._log("[*] Reading current AOA configuration ...")

        def work():
            try:
                if need_snap:
                    # Need the snapshot to place the overlays; fetch it first so this
                    # is one click even before a snapshot is loaded.
                    self.msg_queue.put(("snapshot", client.fetch_snapshot()))
                cfg = client.get_config()
                scenarios = cfg.get("data", {}).get("scenarios", [])
                overlays = self._parse_overlays(scenarios)
                lines = [f"[+] {len(scenarios)} scenario(s) configured:"]
                for s in scenarios:
                    classes = ", ".join(c.get("type", "?") for c in s.get("objectClassifications", [])) or "any"
                    n_excl = len([f for f in s.get("filters", []) if f.get("type") == "excludeArea"])
                    excl = f", {n_excl} exclusion(s)" if n_excl else ""
                    lines.append(f"    #{s.get('id')} '{s.get('name')}' type={s.get('type')} classes={classes}{excl}")
                self.msg_queue.put(("overlays", overlays, lines, scenarios))
            except Exception as e:
                self.msg_queue.put(("log", f"[!] Read failed: {e}"))
        self._run_bg(work)

    @staticmethod
    def _parse_overlays(scenarios):
        """Flatten scenarios into drawable overlays (normalized verts + kind + label).
        kind: 'area' (includeArea), 'fence' (fence/countingLine), 'exclude'."""
        overlays = []
        for s in scenarios:
            name = s.get("name", f"#{s.get('id')}")
            trig = (s.get("triggers") or [{}])[0]
            ttype = trig.get("type")
            verts = [(float(x), float(y)) for x, y in trig.get("vertices", [])]
            if ttype in ("fence", "countingLine"):
                overlays.append({"name": name, "kind": "fence", "verts": verts})
            elif verts:
                overlays.append({"name": name, "kind": "area", "verts": verts})
            for f in s.get("filters", []):
                if f.get("type") == "excludeArea":
                    ev = [(float(x), float(y)) for x, y in f.get("vertices", [])]
                    overlays.append({"name": f"{name} (exclude)", "kind": "exclude", "verts": ev})
        return overlays

    # ---- canvas drawing
    def _display_image(self, pil_img):
        self.pil_image = pil_img
        self.img_w, self.img_h = pil_img.size
        self.scale = min(CANVAS_W / self.img_w, CANVAS_H / self.img_h)
        disp_w, disp_h = int(self.img_w * self.scale), int(self.img_h * self.scale)
        self.offset_x = (CANVAS_W - disp_w) // 2
        self.offset_y = (CANVAS_H - disp_h) // 2
        resized = pil_img.resize((disp_w, disp_h), Image.LANCZOS)
        self.tk_image = ImageTk.PhotoImage(resized)
        self.points = []
        self.exclude_zones = []
        self.current_exclude = []
        self.existing_overlays = []
        self.editing = None
        if hasattr(self, "edit_var"):
            self.edit_var.set(NEW_SCENARIO)
        self._redraw()

    def _norm_to_canvas(self, nx, ny):
        ix, iy = aoa_config.norm_to_pixel(nx, ny, self.img_w, self.img_h)
        return (ix * self.scale + self.offset_x, iy * self.scale + self.offset_y)

    def _redraw(self):
        self.canvas.delete("all")
        if self.tk_image:
            self.canvas.create_image(self.offset_x, self.offset_y, anchor="nw", image=self.tk_image)

        # Existing camera scenarios (amber reference), drawn under the active drawing.
        for ov in self.existing_overlays:
            pts = [self._norm_to_canvas(nx, ny) for (nx, ny) in ov["verts"]]
            if ov["kind"] == "fence" and len(pts) >= 2:
                self.canvas.create_line(*self._flat(pts), fill=EXISTING_OUTLINE, width=3, dash=(6, 3))
            elif ov["kind"] == "exclude" and len(pts) >= 3:
                self.canvas.create_polygon(*self._flat(pts), outline=EXISTING_OUTLINE, fill="",
                                           width=2, dash=(2, 2))
            elif len(pts) >= 3:
                self.canvas.create_polygon(*self._flat(pts), outline=EXISTING_OUTLINE, fill="",
                                           width=2, dash=(6, 3))
            if pts:
                lx, ly = pts[0]
                self.canvas.create_text(lx + 4, ly - 8, text=ov["name"], fill=EXISTING_OUTLINE,
                                        anchor="sw", font=("Consolas", 9, "bold"))

        is_line = self.rule_var.get() == "Line Crossing"

        # Include zone/line (teal)
        if len(self.points) >= 2:
            if is_line:
                self.canvas.create_line(*self._flat(self.points[:2]), fill=ZONE_OUTLINE, width=3)
            else:
                self.canvas.create_polygon(*self._flat(self.points), outline=ZONE_OUTLINE,
                                           fill=ZONE_FILL, stipple="gray25", width=2)
        for (x, y) in self.points:
            self.canvas.create_oval(x - 4, y - 4, x + 4, y + 4, fill=ZONE_OUTLINE, outline=LVT_WHITE)

        # Finished exclusion zones (red)
        for zone in self.exclude_zones:
            if len(zone) >= 3:
                self.canvas.create_polygon(*self._flat(zone), outline=EXCL_OUTLINE,
                                           fill=EXCL_FILL, stipple="gray50", width=2)
        # In-progress exclusion zone (red dots/line)
        if len(self.current_exclude) >= 2:
            self.canvas.create_line(*self._flat(self.current_exclude), fill=EXCL_OUTLINE, width=2, dash=(4, 3))
        for (x, y) in self.current_exclude:
            self.canvas.create_oval(x - 4, y - 4, x + 4, y + 4, fill=EXCL_OUTLINE, outline=LVT_WHITE)

    @staticmethod
    def _flat(points):
        return [c for p in points for c in p]

    def _canvas_to_image_px(self, cx, cy):
        """Canvas pixel -> ORIGINAL image pixel (coords must normalize against full res)."""
        ix = (cx - self.offset_x) / self.scale
        iy = (cy - self.offset_y) / self.scale
        return ix, iy

    def _on_canvas_click(self, event):
        if not self.tk_image:
            return
        # ignore clicks outside the image area
        ix, iy = self._canvas_to_image_px(event.x, event.y)
        if not (0 <= ix <= self.img_w and 0 <= iy <= self.img_h):
            return
        if self.mode_var.get() == "Exclude":
            if len(self.exclude_zones) >= aoa_config.MAX_EXCLUDE_ZONES:
                self._log(f"[!] Max {aoa_config.MAX_EXCLUDE_ZONES} exclusion zones per scenario.")
                return
            self.current_exclude.append((event.x, event.y))
        else:
            is_line = self.rule_var.get() == "Line Crossing"
            if is_line and len(self.points) >= 2:
                self.points = []  # start a new line
            self.points.append((event.x, event.y))
        self._redraw()
        self._update_coord_label()

    def _on_canvas_motion(self, event):
        if not self.tk_image:
            return
        ix, iy = self._canvas_to_image_px(event.x, event.y)
        if 0 <= ix <= self.img_w and 0 <= iy <= self.img_h:
            nx, ny = aoa_config.pixel_to_norm(ix, iy, self.img_w, self.img_h)
            self.coord_label.configure(
                text=f"cursor: px({int(ix)},{int(iy)})  ->  AOA norm({nx:+.3f}, {ny:+.3f})   |   "
                     f"points: {len(self.points)}")

    def _update_coord_label(self):
        norms = [aoa_config.pixel_to_norm(*self._canvas_to_image_px(x, y), self.img_w, self.img_h)
                 for (x, y) in self.points]
        self._log(f"[.] {len(self.points)} point(s): " +
                  ", ".join(f"({nx:+.3f},{ny:+.3f})" for nx, ny in norms))

    def _on_mode_change(self, _value=None):
        # Exclusion applies to area scenarios; line crossing has no include area to
        # exclude from, so keep it include-only.
        if self.rule_var.get() == "Line Crossing" and self.mode_var.get() == "Exclude":
            self._log("[!] Exclusion zones aren't used with Line Crossing. Staying in Include mode.")
            self.mode_var.set("Include")

    def _finish_exclude(self):
        if len(self.current_exclude) < 3:
            self._log("[!] An exclusion zone needs at least 3 points before finishing.")
            return
        self.exclude_zones.append(list(self.current_exclude))
        self.current_exclude = []
        self._log(f"[.] Exclusion zone #{len(self.exclude_zones)} added.")
        self._redraw()

    def _undo_point(self):
        if self.mode_var.get() == "Exclude":
            if self.current_exclude:
                self.current_exclude.pop()
            elif self.exclude_zones:
                # pop the last finished zone back into the in-progress buffer to edit
                self.current_exclude = self.exclude_zones.pop()
                if self.current_exclude:
                    self.current_exclude.pop()
        elif self.points:
            self.points.pop()
        self._redraw()

    def _clear_points(self):
        self.points = []
        self.exclude_zones = []
        self.current_exclude = []
        self._redraw()

    def _selected_classes(self):
        classes = []
        if self.class_human.get():
            classes.append("human")
        if self.class_vehicle.get():
            classes.append("vehicle")
        return tuple(classes) or ("human",)

    def _build_scenario(self):
        """Turn the current drawing + form into a validated AOA scenario, or return
        (None, reason) if the inputs aren't usable."""
        name = self.name_var.get().strip()
        if not name:
            return None, "Enter a scenario name."
        if not self.tk_image:
            return None, "Fetch a snapshot and draw a zone first."
        classes = self._selected_classes()
        rule = self.rule_var.get()

        norm = [aoa_config.pixel_to_norm(*self._canvas_to_image_px(x, y), self.img_w, self.img_h)
                for (x, y) in self.points]

        # Exclusion zones (area scenarios only). Auto-finish an in-progress one.
        exclude_norm = None
        if rule != "Line Crossing":
            zones = list(self.exclude_zones)
            if len(self.current_exclude) >= 3:
                zones.append(list(self.current_exclude))
            exclude_norm = [[aoa_config.pixel_to_norm(*self._canvas_to_image_px(x, y), self.img_w, self.img_h)
                             for (x, y) in zone] for zone in zones] or None

        if self.editing is not None:
            # Modify-in-place: patch only geometry/classes/exclusions/loiter time,
            # preserving all other settings on the original scenario.
            loiter_seconds = None
            if rule == "Loitering":
                try:
                    loiter_seconds = int(self.loiter_entry.get().strip())
                except ValueError:
                    return None, "Enter loiter seconds as a whole number."
            scenario = aoa_config.update_scenario_geometry(
                self.editing, norm, classes, exclude_norm, loiter_seconds)
            scenario["name"] = name  # allow rename (replace-by-id keeps the right target)
        elif rule == "Line Crossing":
            if len(norm) != 2:
                return None, "Line crossing needs exactly 2 points."
            scenario = aoa_config.build_line_crossing(name, norm, classes=classes)
        elif rule == "Loitering":
            if len(norm) < 3:
                return None, "Loitering needs an area of at least 3 points."
            try:
                seconds = int(self.loiter_entry.get().strip())
            except ValueError:
                return None, "Enter loiter seconds as a whole number."
            scenario = aoa_config.build_loiter(name, norm, seconds, classes=classes)
            if exclude_norm:
                aoa_config.add_exclude_zones(scenario, exclude_norm)
        else:  # Intrusion
            if len(norm) < 3:
                return None, "Intrusion needs an area of at least 3 points."
            scenario = aoa_config.build_intrusion(name, norm, classes=classes)
            if exclude_norm:
                aoa_config.add_exclude_zones(scenario, exclude_norm)

        try:
            aoa_config.validate_scenario(scenario)
        except ValueError as e:
            return None, str(e)
        return scenario, None

    def _push(self):
        client = self._make_client()
        if not client:
            return
        scenario, reason = self._build_scenario()
        if scenario is None:
            self._log(f"[!] {reason}")
            messagebox.showwarning("Can't push yet", reason)
            return

        n_excl = len([f for f in scenario.get("filters", []) if f.get("type") == "excludeArea"])
        excl_txt = f"\n{n_excl} exclusion zone(s) included." if n_excl else ""
        verb = "Update existing scenario" if self.editing is not None else "Push new scenario"
        if not messagebox.askyesno(
            "Confirm live write",
            f"{verb} '{scenario['name']}' ({scenario['type']}) on\n"
            f"{client.ip}:{client.port}?{excl_txt}\n\n"
            f"The current config is backed up to aoa_backups\\ first, and every other "
            f"scenario is preserved. This changes live camera analytics."):
            self._log("[.] Push cancelled.")
            return

        self.push_button.configure(state="disabled", text="Pushing...")
        self._log(f"[*] Backing up + pushing '{scenario['name']}' to {client.ip}:{client.port} ...")

        def work():
            try:
                backup_path, verify = client.apply_scenario(scenario, BACKUP_DIR)
                names = [s.get("name") for s in verify.get("data", {}).get("scenarios", [])]
                self.msg_queue.put(("push_done", backup_path.name, names))
            except Exception as e:
                self.msg_queue.put(("push_err", str(e)))
        self._run_bg(work)

    def _restore_backup(self):
        client = self._make_client()
        if not client:
            return
        import json
        selected = filedialog.askopenfilename(
            initialdir=BACKUP_DIR if BACKUP_DIR.exists() else Path.home(),
            title="Select an AOA backup JSON to restore",
            filetypes=(("Backup JSON", "*.json"), ("All Files", "*.*")))
        if not selected:
            return

        try:
            loaded = json.loads(Path(selected).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            messagebox.showerror("Can't read backup", str(e))
            return

        # Guard: it must look like a getConfiguration dump, not a probe/capabilities file.
        scenarios = loaded.get("data", {}).get("scenarios") if isinstance(loaded, dict) else None
        if scenarios is None:
            messagebox.showerror("Not a config backup",
                                 "That file has no data.scenarios -- it's not a getConfiguration backup.")
            return

        # Guard: backup filenames encode <ip>_<port>. Warn on a camera mismatch so we
        # never restore one camera's zones onto a different camera.
        fname = Path(selected).name
        m = re.search(r"aoa_backup_(\d+_\d+_\d+_\d+)_(\d+)_", fname)
        if m:
            file_ip = m.group(1).replace("_", ".")
            file_port = m.group(2)
            if (file_ip, file_port) != (client.ip, client.port):
                if not messagebox.askyesno(
                    "Camera mismatch",
                    f"This backup is from {file_ip}:{file_port}, but the form targets "
                    f"{client.ip}:{client.port}.\n\nRestore it onto {client.ip}:{client.port} anyway?"):
                    self._log("[.] Restore cancelled (camera mismatch).")
                    return

        names = [s.get("name") for s in scenarios]
        if not messagebox.askyesno(
            "Confirm restore",
            f"Overwrite ALL analytics on {client.ip}:{client.port} with this backup?\n\n"
            f"Backup has {len(scenarios)} scenario(s): {names}\n\n"
            f"This replaces the camera's current scenarios entirely."):
            self._log("[.] Restore cancelled.")
            return

        self.restore_button.configure(state="disabled", text="Restoring...")
        self._log(f"[*] Restoring {fname} to {client.ip}:{client.port} ...")

        def work():
            try:
                client.set_config(loaded)
                verify = client.get_config()
                got = [s.get("name") for s in verify.get("data", {}).get("scenarios", [])]
                self.msg_queue.put(("restore_done", got))
            except Exception as e:
                self.msg_queue.put(("restore_err", str(e)))
        self._run_bg(work)

    def _poll_queue(self):
        try:
            while True:
                msg = self.msg_queue.get_nowait()
                if msg[0] == "log":
                    self._log(msg[1])
                elif msg[0] == "snapshot":
                    self._display_image(msg[1])
                    self._log(f"[+] Snapshot loaded ({self.img_w}x{self.img_h}). Click to draw the zone.")
                elif msg[0] == "overlays":
                    _, overlays, lines, scenarios = msg
                    self.existing_overlays = overlays
                    self.existing_scenarios = scenarios
                    names = [NEW_SCENARIO] + [s.get("name", f"#{s.get('id')}") for s in scenarios]
                    self.edit_menu.configure(values=names)
                    self._redraw()
                    self._log("\n".join(lines))
                    if overlays:
                        self._log(f"[+] Overlaid {len(overlays)} existing zone(s) in amber. "
                                  f"Pick one from 'Edit existing' to modify it, or draw a new one.")
                elif msg[0] == "push_done":
                    _, backup_name, names = msg
                    self.push_button.configure(state="normal", text="Push to Camera")
                    self._log(f"[+] Pushed OK. Backup: {backup_name}. Scenarios now: {names}")
                elif msg[0] == "push_err":
                    self.push_button.configure(state="normal", text="Push to Camera")
                    self._log(f"[!] Push failed: {msg[1]}")
                    messagebox.showerror("Push failed", msg[1])
                elif msg[0] == "restore_done":
                    self.restore_button.configure(state="normal", text="Restore from Backup...")
                    self._log(f"[+] Restore complete. Scenarios now: {msg[1]}")
                elif msg[0] == "restore_err":
                    self.restore_button.configure(state="normal", text="Restore from Backup...")
                    self._log(f"[!] Restore failed: {msg[1]}")
                    messagebox.showerror("Restore failed", msg[1])
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _prefill_credentials(self):
        """Convenience: seed user/pass from env or access.env so testing doesn't
        require retyping. Silent if nothing is found."""
        user = os.environ.get("AXIS_USER")
        password = os.environ.get("AXIS_PASS") or os.environ.get("AXIS_PASSWORD")
        env_file = Path(__file__).with_name("access.env")
        if (not user or not password) and env_file.exists():
            try:
                env = dict(re.findall(r"^([A-Z_]+)=(.*)$", env_file.read_text(), re.M))
                user = user or env.get("AXIS_USER")
                password = password or env.get("AXIS_PASSWORD") or env.get("AXIS_PASS")
            except OSError:
                pass
        if user:
            self.user_entry.insert(0, user)
        if password:
            self.pass_entry.insert(0, password)


if __name__ == "__main__":
    WriterApp().mainloop()
