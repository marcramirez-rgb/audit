"""LiveView Technologies -- Axis Analytics Writer.

The write-side sibling of audit_gui.py (the audit report GUI). Point at one Axis camera,
pull a live snapshot, draw/edit AXIS Object Analytics scenarios (trigger + exclusion
areas) on it, and push them to the camera -- no camera web UI required.

Run with: python analytics_writer_gui.py

Capabilities (schema + write path validated live against AOA API 1.6):
    * Connect + fetch snapshot, read current scenarios (read-only)
    * Overlay existing scenarios on the snapshot in amber
    * Draw intrusion / line-crossing / loitering scenarios + exclusion areas
    * Edit an existing scenario in place (preserves perspective/presets/filters)
    * Auto-backup before every write; one-click restore from backup
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
import vendor_adapter

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# --- LiveView Technologies brand palette (lifted from audit_gui.py) ---
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
ZONE_FILL = "#009CA1"
EXCL_OUTLINE = "#FF6B6B"  # exclusion zones drawn in red to read as "ignore here"
EXCL_FILL = "#E03131"
EXISTING_OUTLINE = "#F7B500"  # scenarios already on the camera, shown as amber reference
SIZE_OUTLINE = "#B197FC"      # min/max object-size boxes (purple)
PERSP_OUTLINE = "#51CF66"     # Axis perspective calibration bars (green)

# Write path validated live against 10.23.164.21:5010 (AOA 1.6): admin write confirmed,
# setConfiguration round-trip + add/verify/restore all proven. Gate is open.
WRITE_ENABLED = True

CANVAS_W, CANVAS_H = 900, 506  # 16:9 drawing surface
BACKUP_DIR = Path(__file__).with_name("aoa_backups")
NEW_SCENARIO = "(new scenario)"
DIR_L2R = "Left → Right"
DIR_R2L = "Right → Left"
DIR_TO_API = {DIR_L2R: "leftToRight", DIR_R2L: "rightToLeft"}
API_TO_DIR = {v: k for k, v in DIR_TO_API.items()}

# UI label <-> neutral Scenario.kind
LABEL_TO_KIND = {"Intrusion": "intrusion", "Line Crossing": "line", "Loitering": "loiter"}
KIND_TO_LABEL = {v: k for k, v in LABEL_TO_KIND.items()}

ctk.set_appearance_mode("light")


class WriterApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("LiveView Technologies -- Analytics Writer")
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
        self.existing_overlays = []    # {name, kind, verts(frac), exclusions} for the canvas
        self.existing_scenarios = []   # neutral vendor_adapter.Scenario list from last read
        self.editing = None            # native_id being edited, or None = new
        self.adapter = None            # current vendor adapter
        self.edit_size = []            # [(frac_rect, label)] editable min/max size boxes
        self.size_mode = None          # None | 'min' | 'max' -- clicks draw that size box
        self.size_first = None         # first corner (canvas) while drawing a size box
        self.perspective_bars = []     # editable calibration bars: [{"height":cm, "points":[canvas pts]}]
        self.current_bar = []          # in-progress bar (canvas pts; 2 clicks = 1 bar)
        self.bar_mode = False          # canvas clicks place calibration bars instead of zones

        self._build_header()
        self._build_body()
        self._prefill_credentials()
        self._on_mfg_change()          # set initial capability gating + channel visibility
        self.after(100, self._poll_queue)

    # -------------------------------------------------------------- vendor
    def _caps(self):
        return vendor_adapter.capabilities_for(self.mfg_var.get())

    def _on_mfg_change(self, _value=None):
        mfg = self.mfg_var.get()
        # Hik needs a channel; Axis doesn't.
        if vendor_adapter.camera_engine.classify_manufacturer(mfg) == "HIKVISION":
            self.channel_frame.pack(fill="x", padx=12, pady=4, after=self.ip_entry)
        else:
            self.channel_frame.pack_forget()
        self.adapter = None  # force rebuild on next action
        self._prefill_credentials()
        self._apply_capabilities()

    def _apply_capabilities(self):
        """Enable/disable UI to match what the selected vendor can actually do."""
        caps = self._caps()
        if caps is None:
            return
        # Scenario types restricted to what the vendor supports.
        labels = [KIND_TO_LABEL[k] for k in caps.kinds if k in KIND_TO_LABEL]
        self.rule_menu.configure(values=labels)
        if self.rule_var.get() not in labels and labels:
            self.rule_var.set(labels[0])
        # Vehicle class: only if the vendor supports it.
        self.class_vehicle.configure(state="normal" if "vehicle" in caps.classes else "disabled")
        # Exclusion controls.
        excl_state = "normal" if caps.exclusions else "disabled"
        self.mode_toggle.configure(state=excl_state)
        self.finish_excl_button.configure(state=excl_state)
        if not caps.exclusions:
            self.mode_var.set("Include")
        # Restore only where the API can truly roll back (delete-capable vendors).
        self.restore_button.configure(state="normal" if caps.can_delete else "disabled")
        # Perspective calibration section (Axis only).
        if caps.perspective:
            self.persp_frame.pack(fill="x", padx=12, pady=4, before=self.push_button)
        else:
            self.persp_frame.pack_forget()
            self.bar_mode = False
        # Object-size box editor (positioned min/max -- Hik only).
        if caps.size_boxes:
            self.size_frame.pack(fill="x", padx=12, pady=4, before=self.push_button)
        else:
            self.size_frame.pack_forget()
            self.size_mode = None
        self._on_rule_change()  # refresh loiter/fence frame visibility under new type list

    # -------------------------------------------------------------- layout
    def _build_header(self):
        header = ctk.CTkFrame(self, fg_color=LVT_DARK_TEAL, corner_radius=0, height=72)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)
        ctk.CTkLabel(header, text="Analytics Writer", font=ctk.CTkFont(size=22, weight="bold"),
                     text_color=LVT_WHITE).pack(side="left", padx=24, pady=10)
        ctk.CTkLabel(header, text="Configure Axis and Hikvision analytics -- one place, no web UI",
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
        self.mfg_var = tk.StringVar(value="Axis")
        ctk.CTkOptionMenu(side, values=["Axis", "Hikvision"], variable=self.mfg_var,
                          command=self._on_mfg_change, fg_color=LVT_TEAL, button_color=LVT_DARK_TEAL,
                          button_hover_color=LVT_DARK_TEAL_HOVER).pack(fill="x", padx=12, pady=4)

        self.ip_entry = ctk.CTkEntry(side, placeholder_text="Camera IP e.g. 10.23.66.205")
        self.ip_entry.pack(fill="x", padx=12, pady=4)

        # Hik-only: which channel the VCA lives on (optical=1, thermal=2).
        self.channel_frame = ctk.CTkFrame(side, fg_color="transparent")
        ctk.CTkLabel(self.channel_frame, text="Channel (Hik: 1=optical, 2=thermal)",
                     text_color=LVT_TEXT_MUTED, font=ctk.CTkFont(size=10)).pack(anchor="w")
        self.channel_var = tk.StringVar(value="2")
        ctk.CTkOptionMenu(self.channel_frame, values=["1", "2"], variable=self.channel_var,
                          fg_color=LVT_TEAL, button_color=LVT_DARK_TEAL,
                          button_hover_color=LVT_DARK_TEAL_HOVER).pack(fill="x")
        # packed/unpacked by _on_mfg_change

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
        ctk.CTkButton(side, text="Clear Overlays", command=self._clear_overlays,
                      fg_color=LVT_TEAL, hover_color=LVT_TEAL_HOVER).pack(fill="x", padx=12, pady=(0, 4))

        ctk.CTkLabel(side, text="Edit existing (or create new)", text_color=LVT_TEXT_MUTED,
                     font=ctk.CTkFont(size=10)).pack(anchor="w", padx=12, pady=(6, 0))
        self.edit_var = tk.StringVar(value=NEW_SCENARIO)
        self.edit_menu = ctk.CTkOptionMenu(side, values=[NEW_SCENARIO], variable=self.edit_var,
                                           command=self._on_edit_select, fg_color=LVT_TEAL,
                                           button_color=LVT_DARK_TEAL, button_hover_color=LVT_DARK_TEAL_HOVER)
        self.edit_menu.pack(fill="x", padx=12, pady=4)

        label("Scenario type")
        self.rule_var = tk.StringVar(value="Intrusion")
        self.rule_menu = ctk.CTkOptionMenu(side, values=["Intrusion", "Line Crossing", "Loitering"], variable=self.rule_var,
                          command=self._on_rule_change, fg_color=LVT_TEAL, button_color=LVT_DARK_TEAL,
                          button_hover_color=LVT_DARK_TEAL_HOVER)
        self.rule_menu.pack(fill="x", padx=12, pady=4)

        # AOA caps scenario names at 15 chars (maxLengthName from capabilities) -- enforce
        # visibly here instead of letting the write fail or silently truncate.
        self.name_var = tk.StringVar()
        self.name_var.trace_add("write", self._limit_name_len)
        self.name_entry = ctk.CTkEntry(side, placeholder_text="Scenario name (max 15)", textvariable=self.name_var)
        self.name_entry.pack(fill="x", padx=12, pady=4)

        label("Detection type")
        ctk.CTkLabel(side, text="Tick one, or both for a combined scenario.",
                     text_color=LVT_TEXT_MUTED, font=ctk.CTkFont(size=10)).pack(anchor="w", padx=12)
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

        # Crossing direction (fence). Shown only for Line Crossing. The arrow on the
        # canvas points the way an object must cross to trigger.
        self.fence_frame = ctk.CTkFrame(side, fg_color="transparent")
        ctk.CTkLabel(self.fence_frame, text="Crossing direction", text_color=LVT_TEXT_MUTED,
                     font=ctk.CTkFont(size=10)).pack(anchor="w")
        self.dir_var = tk.StringVar(value=DIR_L2R)
        ctk.CTkSegmentedButton(self.fence_frame, values=[DIR_L2R, DIR_R2L], variable=self.dir_var,
                               command=lambda _v: self._redraw(), selected_color=LVT_DARK_TEAL,
                               selected_hover_color=LVT_DARK_TEAL_HOVER, unselected_color=LVT_TEAL,
                               unselected_hover_color=LVT_TEAL_HOVER).pack(fill="x")
        # hidden unless Line Crossing selected

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

        # Perspective calibration (Axis only). Draw 2-3 vertical bars of known real-world
        # height so the camera can size objects in the scene.
        self.persp_frame = ctk.CTkFrame(side, fg_color="transparent")
        ctk.CTkLabel(self.persp_frame, text="Perspective calibration (green bars)",
                     text_color=LVT_TEXT_DARK, font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w", pady=(10, 2))
        hrow = ctk.CTkFrame(self.persp_frame, fg_color="transparent")
        hrow.pack(fill="x")
        ctk.CTkLabel(hrow, text="Bar height (cm)", text_color=LVT_TEXT_MUTED,
                     font=ctk.CTkFont(size=10)).pack(side="left")
        self.bar_height_var = tk.StringVar(value="180")
        ctk.CTkEntry(hrow, textvariable=self.bar_height_var, width=70).pack(side="right")
        self.bar_button = ctk.CTkButton(self.persp_frame, text="Draw Bar: OFF", command=self._toggle_bar_mode,
                                        fg_color=LVT_TEAL, hover_color=LVT_TEAL_HOVER)
        self.bar_button.pack(fill="x", pady=4)
        ctk.CTkButton(self.persp_frame, text="Clear Bars", command=self._clear_bars,
                      fg_color=LVT_TEAL, hover_color=LVT_TEAL_HOVER).pack(fill="x")
        ctk.CTkLabel(self.persp_frame, text="Set height, click 2 points per bar (2-3 bars).",
                     text_color=LVT_TEXT_MUTED, font=ctk.CTkFont(size=10), justify="left").pack(anchor="w")

        # Object-size filter (Hikvision). Draw min + max object-size boxes (purple).
        self.size_frame = ctk.CTkFrame(side, fg_color="transparent")
        ctk.CTkLabel(self.size_frame, text="Object size filter (min/max boxes)",
                     text_color=LVT_TEXT_DARK, font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w", pady=(10, 2))
        srow = ctk.CTkFrame(self.size_frame, fg_color="transparent")
        srow.pack(fill="x")
        self.min_size_button = ctk.CTkButton(srow, text="Draw Min", width=120, command=lambda: self._toggle_size_mode("min"),
                                             fg_color=LVT_TEAL, hover_color=LVT_TEAL_HOVER)
        self.min_size_button.pack(side="left", padx=(0, 4))
        self.max_size_button = ctk.CTkButton(srow, text="Draw Max", width=120, command=lambda: self._toggle_size_mode("max"),
                                             fg_color=LVT_TEAL, hover_color=LVT_TEAL_HOVER)
        self.max_size_button.pack(side="left")
        ctk.CTkButton(self.size_frame, text="Clear Sizes", command=self._clear_sizes,
                      fg_color=LVT_TEAL, hover_color=LVT_TEAL_HOVER).pack(fill="x", pady=4)
        ctk.CTkLabel(self.size_frame, text="Click 2 opposite corners per box. Set both min and max.",
                     text_color=LVT_TEXT_MUTED, font=ctk.CTkFont(size=10), justify="left").pack(anchor="w")
        # packed/unpacked by _apply_capabilities

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
        rule = self.rule_var.get()
        if rule == "Loitering":
            self.loiter_frame.pack(fill="x", padx=12, pady=4)
        else:
            self.loiter_frame.pack_forget()
        if rule == "Line Crossing":
            self.mode_var.set("Include")  # no exclusion for line crossing
            self.fence_frame.pack(fill="x", padx=12, pady=4)
        else:
            self.fence_frame.pack_forget()
        self._clear_points()

    def _alarm_direction(self):
        """The selected crossing direction as the AOA API value (leftToRight/rightToLeft)."""
        return DIR_TO_API.get(self.dir_var.get(), "leftToRight")

    def _set_classes(self, types):
        (self.class_human.select() if "human" in types else self.class_human.deselect())
        (self.class_vehicle.select() if "vehicle" in types else self.class_vehicle.deselect())

    def _on_edit_select(self, choice):
        if choice == NEW_SCENARIO:
            self.editing = None
            self.name_var.set("")
            self.points, self.exclude_zones, self.current_exclude = [], [], []
            self.edit_size = []
            self.size_mode = self.size_first = None
            self.perspective_bars, self.current_bar = [], []
            self._redraw()
            self._log("[.] New scenario mode.")
            return
        sc = next((s for s in self.existing_scenarios if s.name == choice), None)
        if sc is None:
            return
        if not self.tk_image:
            self._log("[!] Read Current Config first so the snapshot is loaded.")
            return

        self.editing = sc.native_id
        self.name_var.set(sc.name)
        self._set_classes(sc.classes)
        self.rule_var.set(KIND_TO_LABEL.get(sc.kind, "Intrusion"))

        # Show/hide the type-specific fields WITHOUT _on_rule_change (which clears geometry).
        if sc.kind == "loiter":
            self.loiter_frame.pack(fill="x", padx=12, pady=4)
            self.loiter_entry.delete(0, "end")
            self.loiter_entry.insert(0, str(sc.duration or ""))
        else:
            self.loiter_frame.pack_forget()

        if sc.kind == "line":
            self.dir_var.set(API_TO_DIR.get(sc.direction or "leftToRight", DIR_L2R))
            self.fence_frame.pack(fill="x", padx=12, pady=4)
        else:
            self.fence_frame.pack_forget()

        self.points = [self._frac_to_canvas(fx, fy) for (fx, fy) in sc.points]
        self.exclude_zones = [[self._frac_to_canvas(fx, fy) for (fx, fy) in z] for z in sc.exclusions]
        self.current_exclude = []
        self.edit_size = [(r, lbl) for r, lbl in ((sc.min_size, "min"), (sc.max_size, "max")) if r]
        self.perspective_bars = [{"height": b.get("height"),
                                  "points": [self._frac_to_canvas(fx, fy) for (fx, fy) in b.get("points", [])]}
                                 for b in (sc.perspective or [])]
        self.current_bar = []
        self.bar_mode = False
        self.mode_var.set("Include")
        self._redraw()
        extras = []
        if self.edit_size:
            extras.append("min/max size")
        if self.perspective_bars:
            extras.append(f"{len(self.perspective_bars)} perspective bar(s)")
        msg = (" Showing " + ", ".join(extras) + ".") if extras else ""
        self._log(f"[.] Editing '{choice}'. Adjust and Push to update it in place.{msg}")

    def _make_adapter(self):
        ip = self.ip_entry.get().strip()
        user = self.user_entry.get().strip()
        password = self.pass_entry.get().strip()
        if not (ip and user and password):
            self._log("[!] Enter IP, username, and password first.")
            return None
        try:
            self.adapter = vendor_adapter.make_adapter(
                self.mfg_var.get(), ip, self.port_var.get(), user, password,
                channel=self.channel_var.get())
        except Exception as e:
            self._log(f"[!] {e}")
            return None
        return self.adapter

    def _run_bg(self, fn):
        if self.worker and self.worker.is_alive():
            self._log("[!] A request is already running.")
            return
        self.worker = threading.Thread(target=fn, daemon=True)
        self.worker.start()

    def _fetch_snapshot(self):
        adapter = self._make_adapter()
        if not adapter:
            return
        self._log(f"[*] Fetching snapshot from {adapter.vendor} {self.ip_entry.get().strip()} ...")

        def work():
            try:
                self.msg_queue.put(("snapshot", adapter.fetch_snapshot()))
            except Exception as e:
                self.msg_queue.put(("log", f"[!] Snapshot failed: {e}"))
        self._run_bg(work)

    def _read_config(self):
        adapter = self._make_adapter()
        if not adapter:
            return
        need_snap = self.tk_image is None
        self._log(f"[*] Reading current {adapter.vendor} scenarios ...")

        def work():
            try:
                if need_snap:
                    self.msg_queue.put(("snapshot", adapter.fetch_snapshot()))
                scenarios = adapter.read_scenarios()
                overlays = []
                lines = [f"[+] {len(scenarios)} scenario(s) configured:"]
                for s in scenarios:
                    ov_kind = "fence" if s.kind == "line" else "area"
                    overlays.append({"name": s.name, "kind": ov_kind, "verts": s.points})
                    for ex in s.exclusions:
                        overlays.append({"name": f"{s.name} (exclude)", "kind": "exclude", "verts": ex})
                    if s.min_size:
                        overlays.append({"name": f"{s.name} min", "kind": "size", "rect": s.min_size})
                    if s.max_size:
                        overlays.append({"name": f"{s.name} max", "kind": "size", "rect": s.max_size})
                    for bar in (s.perspective or []):
                        overlays.append({"name": f"{bar.get('height')}cm", "kind": "bar",
                                         "verts": bar.get("points", [])})
                    size_txt = ""
                    if s.min_size or s.max_size:
                        size_txt = " size[min/max]"
                    lines.append(f"    '{s.name}' {s.kind} classes={s.classes}"
                                 + (f" dur={s.duration}" if s.duration else "")
                                 + (f" excl={len(s.exclusions)}" if s.exclusions else "") + size_txt)
                self.msg_queue.put(("overlays", overlays, lines, scenarios))
            except Exception as e:
                self.msg_queue.put(("log", f"[!] Read failed: {e}"))
        self._run_bg(work)

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
        self.edit_size = []
        self.size_mode = self.size_first = None
        self.perspective_bars, self.current_bar = [], []
        self.editing = None
        if hasattr(self, "edit_var"):
            self.edit_var.set(NEW_SCENARIO)
        self._redraw()

    def _frac_to_canvas(self, fx, fy):
        """Neutral [0,1] top-left fraction -> canvas pixel."""
        ix, iy = fx * self.img_w, fy * self.img_h
        return (ix * self.scale + self.offset_x, iy * self.scale + self.offset_y)

    def _canvas_to_frac(self, cx, cy):
        """Canvas pixel -> neutral [0,1] top-left fraction (clamped)."""
        ix, iy = self._canvas_to_image_px(cx, cy)
        return (max(0.0, min(1.0, ix / self.img_w)), max(0.0, min(1.0, iy / self.img_h)))

    def _draw_fence_arrow(self):
        """Perpendicular arrow at the line midpoint showing the crossing direction an
        object must travel to trigger. Computed in AOA normalized space (the authority
        for leftToRight/rightToLeft, which is relative to vertex[0]->vertex[1]), then
        mapped to the canvas so it stays correct through the Y-flip."""
        # Compute in AOA y-up space (the authority for leftToRight/rightToLeft, verified
        # against the Axis UI) then map back through fractions. Direction is Axis-only.
        def frac_to_aoa(fx, fy):
            return (2 * fx - 1, 1 - 2 * fy)

        def aoa_to_canvas(ax, ay):
            return self._frac_to_canvas((ax + 1) / 2.0, (1 - ay) / 2.0)

        n0 = frac_to_aoa(*self._canvas_to_frac(*self.points[0]))
        n1 = frac_to_aoa(*self._canvas_to_frac(*self.points[1]))
        dx, dy = n1[0] - n0[0], n1[1] - n0[1]
        length = (dx * dx + dy * dy) ** 0.5 or 1.0
        if self._alarm_direction() == "leftToRight":
            px, py = dy / length, -dx / length
        else:
            px, py = -dy / length, dx / length
        mid = ((n0[0] + n1[0]) / 2.0, (n0[1] + n1[1]) / 2.0)
        tip = (mid[0] + px * 0.18, mid[1] + py * 0.18)
        mcx, mcy = aoa_to_canvas(*mid)
        tcx, tcy = aoa_to_canvas(*tip)
        self.canvas.create_line(mcx, mcy, tcx, tcy, fill=ZONE_OUTLINE, width=3,
                                arrow="last", arrowshape=(14, 16, 6))

    def _redraw(self):
        self.canvas.delete("all")
        if self.tk_image:
            self.canvas.create_image(self.offset_x, self.offset_y, anchor="nw", image=self.tk_image)

        # Existing camera scenarios (amber reference), drawn under the active drawing.
        for ov in self.existing_overlays:
            if ov["kind"] == "size":
                fx, fy, fw, fh = ov["rect"]
                x0, y0 = self._frac_to_canvas(fx, fy)
                x1, y1 = self._frac_to_canvas(fx + fw, fy + fh)
                self.canvas.create_rectangle(x0, y0, x1, y1, outline=SIZE_OUTLINE, width=2)
                self.canvas.create_text(x0 + 2, y0 - 7, text=ov["name"], fill=SIZE_OUTLINE,
                                        anchor="sw", font=("Consolas", 8))
                continue
            if ov["kind"] == "bar":
                self._draw_bar(ov["verts"], ov["name"])
                continue
            pts = [self._frac_to_canvas(fx, fy) for (fx, fy) in ov["verts"]]
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
                self._draw_fence_arrow()
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

        # Min/max object-size boxes for the scenario being edited (purple), so selecting
        # a rule shows its sizing even when the full read-overlays aren't present.
        for (fx, fy, fw, fh), lbl in self.edit_size:
            x0, y0 = self._frac_to_canvas(fx, fy)
            x1, y1 = self._frac_to_canvas(fx + fw, fy + fh)
            self.canvas.create_rectangle(x0, y0, x1, y1, outline=SIZE_OUTLINE, width=2)
            self.canvas.create_text(x0 + 2, y0 - 7, text=lbl, fill=SIZE_OUTLINE,
                                    anchor="sw", font=("Consolas", 8, "bold"))

        # Editable perspective calibration bars (green) + any in-progress bar.
        for bar in self.perspective_bars:
            self._draw_bar_canvas(bar["points"], f"{bar['height']}cm")
        if len(self.current_bar) == 1:
            x, y = self.current_bar[0]
            self.canvas.create_oval(x - 4, y - 4, x + 4, y + 4, fill=PERSP_OUTLINE, outline=LVT_WHITE)

    def _draw_bar(self, verts_frac, label):
        """Draw a calibration bar from fraction points (used by read-overlays)."""
        self._draw_bar_canvas([self._frac_to_canvas(fx, fy) for (fx, fy) in verts_frac], label)

    def _draw_bar_canvas(self, pts, label):
        """Draw a calibration bar from canvas points: a line with end ticks + height label."""
        if len(pts) < 2:
            return
        self.canvas.create_line(*self._flat(pts[:2]), fill=PERSP_OUTLINE, width=3)
        for (x, y) in pts[:2]:
            self.canvas.create_line(x - 5, y, x + 5, y, fill=PERSP_OUTLINE, width=2)
        self.canvas.create_text(pts[0][0] + 6, pts[0][1], text=label, fill=PERSP_OUTLINE,
                                anchor="w", font=("Consolas", 8, "bold"))

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
        if self.size_mode:
            if self.size_first is None:
                self.size_first = (event.x, event.y)
            else:
                f0 = self._canvas_to_frac(*self.size_first)
                f1 = self._canvas_to_frac(event.x, event.y)
                rect = (min(f0[0], f1[0]), min(f0[1], f1[1]),
                        abs(f1[0] - f0[0]), abs(f1[1] - f0[1]))
                self.edit_size = [(r, l) for (r, l) in self.edit_size if l != self.size_mode]
                self.edit_size.append((rect, self.size_mode))
                self._log(f"[.] {self.size_mode.capitalize()} size box set.")
                self._toggle_size_mode(self.size_mode)  # turn the mode off
            self._redraw()
            return
        if self.bar_mode:
            self.current_bar.append((event.x, event.y))
            if len(self.current_bar) == 2:
                try:
                    h = int(self.bar_height_var.get().strip())
                except ValueError:
                    h = 180
                if len(self.perspective_bars) >= aoa_config.PERSP_MAX_BARS:
                    self._log(f"[!] Max {aoa_config.PERSP_MAX_BARS} calibration bars.")
                    self.current_bar = []
                else:
                    self.perspective_bars.append({"height": h, "points": list(self.current_bar)})
                    self.current_bar = []
                    self._log(f"[.] Bar {len(self.perspective_bars)} added ({h}cm).")
            self._redraw()
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
            fx, fy = self._canvas_to_frac(event.x, event.y)
            self.coord_label.configure(
                text=f"cursor: px({int(ix)},{int(iy)})  ->  frac({fx:.3f}, {fy:.3f})   |   "
                     f"points: {len(self.points)}")

    def _update_coord_label(self):
        fracs = [self._canvas_to_frac(x, y) for (x, y) in self.points]
        self._log(f"[.] {len(self.points)} point(s): " +
                  ", ".join(f"({fx:.3f},{fy:.3f})" for fx, fy in fracs))

    def _on_mode_change(self, _value=None):
        # Exclusion applies to area scenarios; line crossing has no include area to
        # exclude from, so keep it include-only.
        if self.rule_var.get() == "Line Crossing" and self.mode_var.get() == "Exclude":
            self._log("[!] Exclusion zones aren't used with Line Crossing. Staying in Include mode.")
            self.mode_var.set("Include")

    def _toggle_bar_mode(self):
        self.bar_mode = not self.bar_mode
        self.bar_button.configure(text=f"Draw Bar: {'ON' if self.bar_mode else 'OFF'}",
                                  fg_color=PERSP_OUTLINE if self.bar_mode else LVT_TEAL)
        self.current_bar = []
        if self.bar_mode:
            self._log("[.] Bar mode ON: set the height, then click 2 points for each bar (2-3 bars).")

    def _clear_bars(self):
        self.perspective_bars = []
        self.current_bar = []
        self._redraw()
        self._log("[.] Calibration bars cleared.")

    def _toggle_size_mode(self, which):
        self.size_mode = None if self.size_mode == which else which
        self.size_first = None
        for w, btn in (("min", self.min_size_button), ("max", self.max_size_button)):
            btn.configure(fg_color=SIZE_OUTLINE if self.size_mode == w else LVT_TEAL)
        if self.size_mode:
            # leaving bar mode if it was on -- one draw mode at a time
            self.bar_mode = False
            self._log(f"[.] {which.capitalize()} size box: click 2 opposite corners.")

    def _clear_sizes(self):
        self.edit_size = []
        self.size_mode = None
        self.size_first = None
        for btn in (self.min_size_button, self.max_size_button):
            btn.configure(fg_color=LVT_TEAL)
        self._redraw()
        self._log("[.] Size boxes cleared.")

    def _clear_overlays(self):
        """Remove the reference overlays drawn by Read Current Config (existing
        scenarios, size boxes, perspective bars) for a blank snapshot to draw on.
        Display-only -- does NOT change anything on the camera."""
        if not self.existing_overlays:
            self._log("[.] No config overlays to clear.")
            return
        self.existing_overlays = []
        self._redraw()
        self._log("[.] Config overlays cleared (camera unchanged). Blank slate to draw on.")

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
        """Turn the current drawing + form into a vendor-neutral Scenario, or return
        (None, reason) if the inputs aren't usable."""
        name = self.name_var.get().strip()
        if not name:
            return None, "Enter a scenario name."
        if not self.tk_image:
            return None, "Fetch a snapshot and draw a zone first."
        classes = self._selected_classes()
        rule = self.rule_var.get()
        kind = LABEL_TO_KIND.get(rule, "intrusion")

        points = [self._canvas_to_frac(x, y) for (x, y) in self.points]

        if kind == "line":
            if len(points) != 2:
                return None, "Line crossing needs exactly 2 points."
        elif len(points) < 3:
            return None, f"{rule} needs an area of at least 3 points."

        # Exclusion zones (area scenarios only). Auto-finish an in-progress one.
        exclusions = []
        if kind != "line":
            zones = list(self.exclude_zones)
            if len(self.current_exclude) >= 3:
                zones.append(list(self.current_exclude))
            exclusions = [[self._canvas_to_frac(x, y) for (x, y) in z] for z in zones]

        duration = 0
        if kind == "loiter":
            try:
                duration = int(self.loiter_entry.get().strip())
            except ValueError:
                return None, "Enter loiter seconds as a whole number."

        # Perspective calibration bars (Axis only). Include an in-progress complete bar.
        bars = list(self.perspective_bars)
        if len(self.current_bar) == 2:
            try:
                bars.append({"height": int(self.bar_height_var.get().strip()), "points": list(self.current_bar)})
            except ValueError:
                pass
        perspective = None
        if bars:
            if not (aoa_config.PERSP_MIN_BARS <= len(bars) <= aoa_config.PERSP_MAX_BARS):
                return None, f"Perspective needs {aoa_config.PERSP_MIN_BARS}-{aoa_config.PERSP_MAX_BARS} bars, got {len(bars)}."
            perspective = [{"height": b["height"],
                            "points": [self._canvas_to_frac(x, y) for (x, y) in b["points"]]} for b in bars]

        # Min/max object-size boxes (positioned -- Hik). Both must be set to apply a filter.
        min_size = max_size = None
        if self._caps().size_boxes:
            min_size = next((r for (r, l) in self.edit_size if l == "min"), None)
            max_size = next((r for (r, l) in self.edit_size if l == "max"), None)
            if bool(min_size) != bool(max_size):
                return None, "Set BOTH a min and a max size box (or clear sizes)."

        sc = vendor_adapter.Scenario(
            name=name, kind=kind, points=points, classes=classes, duration=duration,
            direction=self._alarm_direction() if kind == "line" else None,
            exclusions=exclusions, native_id=self.editing, perspective=perspective,
            min_size=min_size, max_size=max_size)
        return sc, None

    def _push(self):
        adapter = self._make_adapter()
        if not adapter:
            return
        sc, reason = self._build_scenario()
        if sc is None:
            self._log(f"[!] {reason}")
            messagebox.showwarning("Can't push yet", reason)
            return

        ip, port = self.ip_entry.get().strip(), self.port_var.get()
        excl_txt = f"\n{len(sc.exclusions)} exclusion zone(s) included." if sc.exclusions else ""
        verb = "Update existing scenario" if self.editing is not None else "Push new scenario"
        if not messagebox.askyesno(
            "Confirm live write",
            f"{verb} '{sc.name}' ({sc.kind}) on {adapter.vendor}\n{ip}:{port}?{excl_txt}\n\n"
            f"The current config is backed up first, and other scenarios are preserved. "
            f"This changes live camera analytics."):
            self._log("[.] Push cancelled.")
            return

        self.push_button.configure(state="disabled", text="Pushing...")
        self._log(f"[*] Backing up + pushing '{sc.name}' to {adapter.vendor} {ip}:{port} ...")
        backup_dir = "hik_backups" if adapter.vendor == "Hikvision" else BACKUP_DIR

        def work():
            try:
                backup_path, _verify = adapter.apply_scenario(sc, backup_dir)
                names = [s.name for s in adapter.read_scenarios()]
                self.msg_queue.put(("push_done", Path(backup_path).name, names))
            except Exception as e:
                self.msg_queue.put(("push_err", str(e)))
        self._run_bg(work)

    def _restore_backup(self):
        if not self._caps().can_delete:
            messagebox.showinfo("Restore not available",
                                f"{self.mfg_var.get()} on this camera can't roll back via API "
                                f"(PUT is upsert-only). Remove/adjust rules in the camera web UI.")
            return
        adapter = self._make_adapter()
        if not adapter:
            return
        client = adapter.client  # Axis-only path (guarded above)
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
                    names = [NEW_SCENARIO] + [s.name for s in scenarios]
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
        """Seed user/pass for the selected vendor from env or access.env. Silent if
        nothing is found. Re-run on manufacturer change."""
        prefix = "HIK" if vendor_adapter.camera_engine.classify_manufacturer(self.mfg_var.get()) == "HIKVISION" else "AXIS"
        user = os.environ.get(f"{prefix}_USER")
        password = os.environ.get(f"{prefix}_PASS") or os.environ.get(f"{prefix}_PASSWORD")
        env_file = Path(__file__).with_name("access.env")
        if (not user or not password) and env_file.exists():
            try:
                env = dict(re.findall(r"^([A-Z_]+)=(.*)$", env_file.read_text(), re.M))
                user = user or env.get(f"{prefix}_USER")
                password = password or env.get(f"{prefix}_PASSWORD") or env.get(f"{prefix}_PASS")
            except OSError:
                pass
        self.user_entry.delete(0, "end")
        self.pass_entry.delete(0, "end")
        if user:
            self.user_entry.insert(0, user)
        if password:
            self.pass_entry.insert(0, password)


if __name__ == "__main__":
    WriterApp().mainloop()
