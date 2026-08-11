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
import fleet_catalog

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

CANVAS_W, CANVAS_H = 900, 506      # 16:9 drawing surface (startup size; it now follows the window)
CANVAS_MIN_W, CANVAS_MIN_H = 360, 203  # floor the canvas may shrink to on a small screen
BACKUP_DIR = Path(__file__).with_name("aoa_backups")
HIK_BACKUP_DIR = Path(__file__).with_name("hik_backups")


def save_snapshot_pair(backup_path, pil_img):
    """Save the raw snapshot the zone was drawn on next to its config backup, using a
    matching filename stem (e.g. aoa_backup_..._{ts}.json -> aoa_backup_..._{ts}.jpg).

    This is what turns each write into a retrievable (image, geometry) pair for later
    ML/eval work -- the geometry already lives in the backup JSON/XML; this adds the
    frame it was drawn against, which was previously discarded. Best-effort: a save
    failure must never break a successful camera push, so the caller ignores errors.
    Returns the image Path on success, or None."""
    if pil_img is None:
        return None
    img_path = Path(backup_path).with_suffix(".jpg")
    img = pil_img if pil_img.mode in ("RGB", "L") else pil_img.convert("RGB")
    img.save(img_path, format="JPEG", quality=90)
    return img_path
NEW_SCENARIO = "(new scenario)"
DIR_L2R = "Left → Right"
DIR_R2L = "Right → Left"
DIR_TO_API = {DIR_L2R: "leftToRight", DIR_R2L: "rightToLeft"}
API_TO_DIR = {v: k for k, v in DIR_TO_API.items()}

# LVT camera position <-> underlying port. The dropdown shows the position;
# the actual port is resolved from it (5010=Center, 5015=Left, 5020=Right).
PORT_LABEL_TO_VALUE = {"Center": "5010", "Left": "5015", "Right": "5020", "Direct (80)": "80"}
PORT_VALUE_TO_LABEL = {v: k for k, v in PORT_LABEL_TO_VALUE.items()}

# UI label <-> neutral Scenario.kind
LABEL_TO_KIND = {"Intrusion": "intrusion", "Line Crossing": "line", "Loitering": "loiter"}
KIND_TO_LABEL = {v: k for k, v in LABEL_TO_KIND.items()}

ctk.set_appearance_mode("light")


def _points_bbox(points):
    """Bounding box (fx0, fy0, fx1, fy1) of a list of (fx, fy) fraction points."""
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def _rect_overlaps(bbox, rect):
    """True if fraction rect (fx, fy, fw, fh) intersects bbox (fx0, fy0, fx1, fy1).
    Hikvision rejects the whole push (400 badParameters) if a SizeFilter min/max box
    sits entirely outside the RuleRegion's own area, even though nothing in the wire
    format requires the two to relate -- confirmed live against 10.23.23.182:5010 ch2."""
    bx0, by0, bx1, by1 = bbox
    rx, ry, rw, rh = rect
    return bx0 < rx + rw and rx < bx1 and by0 < ry + rh and ry < by1


def _bind_wheel(scroll_frame):
    """Forward mouse-wheel events from a CTkScrollableFrame and every child to the
    frame's canvas, so the wheel scrolls even when the pointer is over a child
    widget (which otherwise swallows the event)."""
    canvas = getattr(scroll_frame, "_parent_canvas", None)
    if canvas is None:
        return

    def _on_wheel(event):
        canvas.yview_scroll(int(-event.delta / 120), "units")
        return "break"

    def _bind(widget):
        widget.bind("<MouseWheel>", _on_wheel)
        for child in widget.winfo_children():
            _bind(child)

    _bind(scroll_frame)


class _FilterList(ctk.CTkFrame):
    """Labelled type-to-filter search box + a SHORT results block.

    Replaces a dropdown whose native pop-out can't be wheel-scrolled and is
    unusable at fleet scale. The results block is a plain frame (not its own
    scroll region) so it lives inside the dialog's single scroll page -- it shows
    at most CAP matches and says "keep typing" beyond that, so it never balloons.
    ``on_choose(value)`` fires when a result is clicked; ``notify`` (if given) is
    called after every re-render so the page can re-bind wheel scrolling.

    ``cap`` limits how many matches are shown (with a "keep typing" hint beyond
    it) -- use it for huge lists like clients. Pass ``cap=None`` for lists you
    want to BROWSE by scrolling (locations, units) when you may not know the
    exact name.
    """

    def __init__(self, master, label, placeholder, on_choose, notify=None, cap=None):
        super().__init__(master, fg_color="transparent")
        self._on_choose = on_choose
        self._notify = notify
        self._cap = cap
        self._all = []
        self._selected = None
        ctk.CTkLabel(self, text=label, text_color=LVT_TEXT_DARK).pack(anchor="w")
        self._entry = ctk.CTkEntry(self, placeholder_text=placeholder)
        self._entry.pack(fill="x")
        self._entry.bind("<KeyRelease>", lambda e: self._render())
        self._results = ctk.CTkFrame(self, fg_color=LVT_WHITE)
        self._results.pack(fill="x", pady=(2, 0))
        self.reset("—")

    def reset(self, hint):
        self._all = []
        self._selected = None
        self._entry.delete(0, "end")
        self._entry.configure(state="disabled")
        self._placeholder(hint)

    def set_items(self, items, empty_hint="None."):
        self._all = list(items)
        self._selected = None
        self._entry.configure(state="normal")
        self._entry.delete(0, "end")
        if items:
            self._render()
        else:
            self._entry.configure(state="disabled")
            self._placeholder(empty_hint)

    def get(self):
        return self._selected

    def _placeholder(self, text):
        for c in self._results.winfo_children():
            c.destroy()
        ctk.CTkLabel(self._results, text=text, text_color=LVT_TEXT_MUTED).pack(anchor="w", padx=6, pady=6)
        if self._notify:
            self._notify()

    def _render(self):
        typed = self._entry.get().strip().lower()
        matches = [v for v in self._all if typed in v.lower()] if typed else self._all
        shown = matches[:self._cap] if self._cap else matches
        for c in self._results.winfo_children():
            c.destroy()
        if not shown:
            self._placeholder("No matches." if typed else "Type to filter…")
            return
        for v in shown:
            is_sel = (v == self._selected)
            ctk.CTkButton(self._results, text=("✓  " + v) if is_sel else v, anchor="w", height=24,
                          fg_color=LVT_DARK_TEAL if is_sel else "transparent",
                          text_color=LVT_WHITE if is_sel else LVT_TEXT_DARK, hover_color=LVT_LIGHT,
                          command=lambda vv=v: self._choose(vv)).pack(fill="x", padx=4, pady=1)
        if len(matches) > len(shown):
            ctk.CTkLabel(self._results, text=f"…and {len(matches) - len(shown)} more — keep typing to narrow.",
                         text_color=LVT_TEXT_MUTED).pack(anchor="w", padx=6, pady=(2, 4))
        if self._notify:
            self._notify()

    def _choose(self, v):
        self._selected = v
        self._entry.delete(0, "end")
        self._entry.insert(0, v)
        self._render()
        self._on_choose(v)


class FleetPickerDialog(ctk.CTkToplevel):
    """Modal Client -> Location -> TDC -> camera picker (v2.0).

    The writer targets one camera at a time, so this resolves a single selected
    camera and hands its IP + vendor back to the sidebar via ``on_select(ip,
    mfg_label)``. The LVT port (5010/5015/5020 = Center/Left/Right) stays on the
    sidebar dropdown -- the catalog can't know which position you're editing.

    Each cascade level is a type-to-filter list (client, location, TDC), so it
    scales to the live fleet and scrolls with the mouse wheel. Backed by
    ``fleet_catalog`` (live Snowflake via SSO, or the offline cached catalog).
    All lookups run off the UI thread and marshal back through a queue.
    """

    def __init__(self, master, on_select):
        super().__init__(master)
        self.on_select = on_select
        self.title("Fleet Picker")
        self.geometry("470x640")
        self.minsize(420, 480)
        self.configure(fg_color=LVT_WHITE)
        self.transient(master)

        self._q = queue.Queue()
        self.source = None
        self._cam_rows = []          # resolved camera rows for the chosen TDC
        self.cam_choice = tk.StringVar(value="")

        self._build()
        self.after(80, self._poll)
        self._reload()
        self.after(200, self.grab_set)  # after the window is mapped

    # -- layout --
    def _build(self):
        # Header (fixed, top).
        ctk.CTkLabel(self, text="Pick a camera from the fleet", font=ctk.CTkFont(size=16, weight="bold"),
                     text_color=LVT_TEXT_DARK).pack(anchor="w", padx=14, pady=(14, 2))
        src_row = ctk.CTkFrame(self, fg_color="transparent")
        src_row.pack(fill="x", padx=14, pady=4)
        self.source_var = tk.StringVar(value="Auto")
        ctk.CTkOptionMenu(src_row, values=["Auto", "Live Snowflake", "Cached catalog"], variable=self.source_var,
                          command=lambda _v: self._reload(), fg_color=LVT_TEAL, button_color=LVT_DARK_TEAL,
                          button_hover_color=LVT_DARK_TEAL_HOVER, width=160).pack(side="left")
        ctk.CTkButton(src_row, text="Reload", width=70, command=self._reload, fg_color=LVT_TEAL,
                      hover_color=LVT_TEAL_HOVER).pack(side="left", padx=8)
        self.status = ctk.CTkLabel(self, text="Connecting…", text_color=LVT_TEXT_MUTED, anchor="w", justify="left")
        self.status.pack(fill="x", padx=14, pady=(0, 6))

        # Action buttons pinned to the BOTTOM so they are ALWAYS visible, no
        # matter how long the lists get. Packed before the scroll page.
        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(side="bottom", fill="x", padx=14, pady=(6, 12))
        self.use_btn = ctk.CTkButton(btn_row, text="Use this camera", command=self._use, state="disabled",
                                     fg_color=LVT_DARK_TEAL, hover_color=LVT_DARK_TEAL_HOVER)
        self.use_btn.pack(side="left", expand=True, fill="x", padx=(0, 6))
        ctk.CTkButton(btn_row, text="Cancel", command=self.destroy, fg_color=LVT_TEAL,
                      hover_color=LVT_TEAL_HOVER, width=90).pack(side="right")

        # Single scroll region for everything between header and buttons.
        self.page = ctk.CTkScrollableFrame(self, fg_color=LVT_WHITE)
        self.page.pack(side="top", fill="both", expand=True, padx=8, pady=(0, 4))

        # Client is capped (2,000+ clients -> must type). Location/Unit show the
        # FULL list so you can scroll and browse when you don't know the exact
        # name; the filter box only narrows if you do.
        self.client_list = _FilterList(self.page, "Client", "Type to filter clients…", self._on_client,
                                       notify=self._rebind_wheel, cap=8)
        self.client_list.pack(fill="x", padx=8, pady=2)
        self.location_list = _FilterList(self.page, "Location", "Filter locations, or scroll to browse…", self._on_location,
                                         notify=self._rebind_wheel, cap=None)
        self.location_list.pack(fill="x", padx=8, pady=2)
        self.tdc_list = _FilterList(self.page, "Unit (TDC)", "Filter units, or scroll to browse…", self._on_tdc,
                                    notify=self._rebind_wheel, cap=None)
        self.tdc_list.pack(fill="x", padx=8, pady=2)

        ctk.CTkLabel(self.page, text="Camera on this unit", text_color=LVT_TEXT_DARK).pack(anchor="w", padx=8, pady=(6, 0))
        self.cam_frame = ctk.CTkFrame(self.page, fg_color=LVT_LIGHT)
        self.cam_frame.pack(fill="x", padx=8, pady=4)

        self._cam_placeholder("Pick a unit to list its cameras.")

    def _rebind_wheel(self):
        _bind_wheel(self.page)

    def _cam_placeholder(self, text):
        for c in self.cam_frame.winfo_children():
            c.destroy()
        ctk.CTkLabel(self.cam_frame, text=text, text_color=LVT_TEXT_MUTED).pack(anchor="w", padx=6, pady=6)
        self.use_btn.configure(state="disabled")
        self._rebind_wheel()

    # -- threaded loads --
    def _bg(self, fn, tag, *args):
        def work():
            try:
                self._q.put((tag, fn(*args)))
            except Exception as e:
                self._q.put(("error", str(e)))
        threading.Thread(target=work, daemon=True).start()

    def _poll(self):
        try:
            while True:
                tag, payload = self._q.get_nowait()
                self._handle(tag, payload)
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(80, self._poll)

    def _handle(self, tag, payload):
        if tag == "clients":
            self.source, clients = payload
            self.status.configure(text=f"{self.source.label} — {len(clients)} client(s).", text_color=LVT_DARK_TEAL)
            self.client_list.set_items(clients, "No clients in this source.")
        elif tag == "locations":
            self.location_list.set_items(payload, "No locations for this client.")
            self.tdc_list.reset("Pick a location first.")
            self._cam_placeholder("Pick a unit to list its cameras.")
        elif tag == "units":
            self.tdc_list.set_items(payload, "No units at this location.")
            self._cam_placeholder("Pick a unit to list its cameras.")
        elif tag == "cameras":
            self._show_cameras(payload)
        elif tag == "error":
            self.status.configure(text=payload.split(chr(10))[0], text_color="#B00020")
            messagebox.showerror("Fleet Picker", payload, parent=self)

    def _pref(self):
        return {"Auto": "auto", "Live Snowflake": "live", "Cached catalog": "cache"}[self.source_var.get()]

    def _reload(self):
        if self.source is not None:
            try:
                self.source.close()
            except Exception:
                pass
            self.source = None
        self.client_list.reset("Connecting…")
        self.location_list.reset("Pick a client first.")
        self.tdc_list.reset("Pick a location first.")
        self._cam_placeholder("Loading…")
        self.status.configure(text="Connecting…", text_color=LVT_TEXT_MUTED)
        self._bg(self._connect_and_clients, "clients")

    def _connect_and_clients(self):
        src = fleet_catalog.build_source(prefer=self._pref())
        return (src, src.list_clients())

    def _on_client(self, client):
        if not client or self.source is None:
            return
        self.location_list.reset("Loading locations…")
        self.tdc_list.reset("Pick a location first.")
        self._cam_placeholder("Loading locations…")
        self._bg(self.source.list_locations, "locations", client)

    def _on_location(self, location):
        client = self.client_list.get()
        if not location or not client or self.source is None:
            return
        self.tdc_list.reset("Loading units…")
        self._cam_placeholder("Loading units…")
        self._bg(self.source.list_units, "units", client, location)

    def _on_tdc(self, serial):
        if not serial or self.source is None:
            return
        self._cam_placeholder("Loading cameras…")
        self._bg(self.source.resolve_cameras, "cameras", [serial])

    def _show_cameras(self, rows):
        for c in self.cam_frame.winfo_children():
            c.destroy()
        self._cam_rows = [r for r in rows if r.get("IP")]
        if not self._cam_rows:
            self._cam_placeholder("No cameras for this unit in the source.")
            return
        self.cam_choice.set(self._cam_rows[0]["IP"])
        for r in self._cam_rows:
            vendor = r.get("MANUFACTURER") or "?"
            model = r.get("MODEL") or ""
            text = f"{r['IP']}   —   {vendor}" + (f" {model}" if model else "")
            ctk.CTkRadioButton(self.cam_frame, text=text, variable=self.cam_choice, value=r["IP"],
                               fg_color=LVT_TEAL, hover_color=LVT_TEAL_HOVER, text_color=LVT_TEXT_DARK).pack(anchor="w", padx=6, pady=2)
        self.use_btn.configure(state="normal")
        self._rebind_wheel()

    def _use(self):
        ip = self.cam_choice.get()
        row = next((r for r in self._cam_rows if r["IP"] == ip), None)
        if not row:
            return
        mfg_class = vendor_adapter.camera_engine.classify_manufacturer(row.get("MANUFACTURER", ""))
        label = {"AXIS": "Axis", "HIKVISION": "Hikvision"}.get(mfg_class)
        if label is None:
            if not messagebox.askyesno(
                "Unrecognized vendor",
                f"MANUFACTURER '{row.get('MANUFACTURER')}' for {ip} isn't clearly Axis or Hikvision.\n\n"
                "Use the IP anyway and set the vendor manually?", parent=self):
                return
        self.on_select(ip, label)
        self.destroy()


class WriterApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("LiveView Technologies -- Analytics Writer")
        # Size to the screen instead of demanding a fixed 1180x860: on a 1366x768 laptop
        # that pushed the canvas and log off the bottom with no way to scroll to them.
        self._apply_startup_geometry(1180, 860)
        self.minsize(880, 560)
        self.configure(fg_color=LVT_WHITE)

        self.msg_queue = queue.Queue()
        self.worker = None

        # Drawing / image state
        self.pil_image = None          # original full-res PIL image
        self.tk_image = None           # scaled ImageTk for display
        self.img_w = self.img_h = 0    # original dims (coords normalize against these)
        self.scale = 1.0               # display scale factor
        self.offset_x = self.offset_y = 0
        self.drag_target = None        # vertex being dragged (grab-and-drag), or None
        self.points = []               # canvas-pixel points for the INCLUDE zone/line
        self.exclude_zones = []        # list of finished exclusion polygons (canvas px)
        self.current_exclude = []      # exclusion polygon currently being drawn
        self.existing_overlays = []    # {name, kind, verts(frac), exclusions} for the canvas
        self.existing_scenarios = []   # neutral vendor_adapter.Scenario list from last read
        self.edit_label_map = {}       # dropdown display label -> Scenario (disambiguates dup names)
        self.editing = None            # native_id being edited, or None = new
        self.adapter = None            # current vendor adapter
        self.edit_size = []            # [(frac_rect, label)] editable min/max size boxes
        self.size_mode = None          # None | 'min' | 'max' -- clicks draw that size box
        self.size_first = None         # first corner (canvas) while drawing a size box
        self.perspective_bars = []     # editable calibration bars: [{"height":cm, "points":[canvas pts]}]
        self.current_bar = []          # in-progress bar (canvas pts; 2 clicks = 1 bar)
        self.bar_mode = False          # canvas clicks place calibration bars instead of zones
        # Live canvas size -- the drawing surface follows the window, so scale/offset and
        # every stored canvas-pixel point are recomputed whenever it changes.
        self.canvas_w, self.canvas_h = CANVAS_W, CANVAS_H
        self._resize_job = None
        self._pending_canvas_size = None

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
        # Hik needs a fixed channel; Axis instead gets an (optional) sensor picker
        # for multi-sensor units.
        if vendor_adapter.camera_engine.classify_manufacturer(mfg) == "HIKVISION":
            self.channel_frame.pack(fill="x", padx=12, pady=4, after=self.ip_entry)
            self.axis_sensor_frame.pack_forget()
        else:
            self.channel_frame.pack_forget()
            self.axis_sensor_frame.pack(fill="x", padx=12, pady=4, after=self.ip_entry)
        self.adapter = None  # force rebuild on next action
        self._prefill_credentials()
        self._apply_capabilities()

    def _on_axis_sensor_change(self):
        self.adapter = None  # force rebuild so the new sensor takes effect
        self.tk_image = None  # stale snapshot was from a different sensor

    def _axis_sensor_or_none(self):
        v = self.axis_sensor_var.get()
        return None if v == "Auto" else v

    def _open_fleet_picker(self):
        FleetPickerDialog(self, self._apply_fleet_pick)

    def _apply_fleet_pick(self, ip, mfg_label):
        """Callback from FleetPickerDialog: drop the chosen camera's IP + vendor
        into the sidebar. Port/channel stay as the operator set them."""
        if mfg_label and mfg_label != self.mfg_var.get():
            self.mfg_var.set(mfg_label)
            self._on_mfg_change()
        self.ip_entry.delete(0, "end")
        self.ip_entry.insert(0, ip)
        self.adapter = None  # force rebuild against the new IP on next action
        self._log(f"[*] Fleet Picker selected {mfg_label or 'camera'} at {ip} "
                  f"(set the port for Center/Left/Right, then Fetch Snapshot).")

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
    def _apply_startup_geometry(self, want_w, want_h):
        """Open at the preferred size, clamped to what the screen can actually show
        (and centred), so the tool is usable on a laptop without maximizing."""
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w = max(880, min(want_w, sw - 80))
        h = max(560, min(want_h, sh - 120))
        x, y = max(0, (sw - w) // 2), max(0, (sh - h) // 3)
        self.geometry(f"{w}x{h}+{x}+{y}")

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

        # v2.0: pick a camera from the fleet (Client -> Location -> TDC) instead
        # of typing the IP. Fills IP + vendor; port stays on the dropdown below.
        ctk.CTkButton(side, text="Pick from fleet…", command=self._open_fleet_picker,
                      fg_color=LVT_TEAL, hover_color=LVT_TEAL_HOVER).pack(fill="x", padx=12, pady=(0, 4))

        # Hik-only: which channel the VCA lives on (optical=1, thermal=2).
        self.channel_frame = ctk.CTkFrame(side, fg_color="transparent")
        ctk.CTkLabel(self.channel_frame, text="Channel (Hik: 1=optical, 2=thermal)",
                     text_color=LVT_TEXT_MUTED, font=ctk.CTkFont(size=10)).pack(anchor="w")
        self.channel_var = tk.StringVar(value="2")
        ctk.CTkOptionMenu(self.channel_frame, values=["1", "2"], variable=self.channel_var,
                          fg_color=LVT_TEAL, button_color=LVT_DARK_TEAL,
                          button_hover_color=LVT_DARK_TEAL_HOVER).pack(fill="x")
        # packed/unpacked by _on_mfg_change

        # Axis-only: which physical sensor on a multi-sensor unit (e.g. P3747, verified
        # against a real 10.23.135.24:5010). "Auto" (default) means an ordinary
        # single-sensor camera -- behaves exactly as before this was added.
        self.axis_sensor_frame = ctk.CTkFrame(side, fg_color="transparent")
        ctk.CTkLabel(self.axis_sensor_frame, text="Sensor (multi-sensor units only, e.g. P3747)",
                     text_color=LVT_TEXT_MUTED, font=ctk.CTkFont(size=10)).pack(anchor="w")
        self.axis_sensor_var = tk.StringVar(value="Auto")
        ctk.CTkOptionMenu(self.axis_sensor_frame, values=["Auto", "1", "2", "3", "4"],
                          variable=self.axis_sensor_var, command=lambda _v: self._on_axis_sensor_change(),
                          fg_color=LVT_TEAL, button_color=LVT_DARK_TEAL,
                          button_hover_color=LVT_DARK_TEAL_HOVER).pack(fill="x")
        # packed/unpacked by _on_mfg_change

        self.port_var = tk.StringVar(value="Left")
        ctk.CTkLabel(side, text="Camera position",
                     text_color=LVT_TEXT_MUTED, font=ctk.CTkFont(size=10)).pack(anchor="w", padx=12)
        ctk.CTkOptionMenu(side, values=list(PORT_LABEL_TO_VALUE.keys()), variable=self.port_var,
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
        ctk.CTkLabel(self.loiter_frame, text="Dwell / loiter time (sec)", text_color=LVT_TEXT_MUTED,
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

        # The sidebar is taller than most screens; without this the wheel does nothing
        # unless the pointer happens to sit on the frame's own background.
        _bind_wheel(side)

    def _build_canvas_area(self, parent):
        right = ctk.CTkFrame(parent, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_rowconfigure(0, weight=0)
        right.grid_rowconfigure(1, weight=1)
        right.grid_columnconfigure(0, weight=1)

        self.coord_label = ctk.CTkLabel(right, text="No snapshot loaded. Enter camera details and Fetch Snapshot.",
                                        text_color=LVT_TEXT_MUTED, anchor="w")
        self.coord_label.grid(row=0, column=0, sticky="ew", pady=(0, 6))

        # Canvas over log with a draggable sash: on a short screen the operator can hand
        # the drawing surface the room instead of losing it under a fixed-height log.
        split = tk.PanedWindow(right, orient="vertical", sashwidth=7, sashrelief="flat",
                               bg=LVT_WHITE, bd=0, showhandle=False)
        split.grid(row=1, column=0, sticky="nsew")

        # Requested size is the FLOOR the layout may shrink to, not the drawing size --
        # the old 900x506 request is what made the window unshrinkable. The pane hands
        # the canvas whatever space is actually left, and <Configure> rescales into it.
        self.canvas = tk.Canvas(split, width=CANVAS_MIN_W, height=CANVAS_MIN_H, bg="#0B0D12",
                                highlightthickness=1, highlightbackground=LVT_DARK_TEAL)
        split.add(self.canvas, minsize=CANVAS_MIN_H, stretch="always")
        self.canvas.bind("<Button-1>", self._on_canvas_click)
        self.canvas.bind("<B1-Motion>", self._on_canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_canvas_release)
        self.canvas.bind("<Motion>", self._on_canvas_motion)
        self.canvas.bind("<Configure>", self._on_canvas_configure)

        self.log_box = ctk.CTkTextbox(split, fg_color=LVT_LOG_BG, text_color=LVT_LOG_TEXT,
                                      font=ctk.CTkFont(family="Consolas", size=11), wrap="word", height=150)
        split.add(self.log_box, minsize=60, height=150, stretch="never")
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

    def _wants_duration(self, rule):
        """The dwell/loiter-time field applies to Axis Loitering and (Hik) intrusion,
        where fieldDetection carries a durationTime."""
        return rule == "Loitering" or (rule == "Intrusion" and self._caps().intrusion_duration)

    def _on_rule_change(self, _value=None):
        rule = self.rule_var.get()
        if self._wants_duration(rule):
            self.loiter_frame.pack(fill="x", padx=12, pady=4)
        else:
            self.loiter_frame.pack_forget()
        if rule == "Line Crossing":
            self.mode_var.set("Include")  # no exclusion for line crossing
            self.fence_frame.pack(fill="x", padx=12, pady=4)
        else:
            self.fence_frame.pack_forget()
        self._clear_points()

    def _port(self):
        """The underlying port for the selected camera position (Center/Left/Right)."""
        return PORT_LABEL_TO_VALUE.get(self.port_var.get(), self.port_var.get())

    def _alarm_direction(self):
        """The selected crossing direction as the AOA API value (leftToRight/rightToLeft)."""
        return DIR_TO_API.get(self.dir_var.get(), "leftToRight")

    def _set_classes(self, types):
        (self.class_human.select() if "human" in types else self.class_human.deselect())
        (self.class_vehicle.select() if "vehicle" in types else self.class_vehicle.deselect())

    @staticmethod
    def _build_edit_labels(scenarios):
        """Map each scenario to a UNIQUE dropdown label. Operators often reuse one
        name for many zones ('RULE1' x6 on a real thermal unit), so a bare name would
        collide and every entry would point at the first. Duplicates get '  #n' plus
        classes/duration. The rule's technical address (channel/scene/ruleId) is
        deliberately NOT shown -- it confused operators; it stays inside native_id so
        the push still targets the exact rule, and selecting an entry highlights its
        zone on the canvas, which is the real disambiguator. Display only: the pushed
        scenario keeps its real name. Preserves read order."""
        from collections import Counter
        counts = Counter(s.name for s in scenarios)
        seen, out = {}, {}
        for s in scenarios:
            if counts[s.name] > 1:
                seen[s.name] = seen.get(s.name, 0) + 1
                label = f"{s.name}  #{seen[s.name]} · {'+'.join(s.classes)}"
                if s.duration:
                    label += f" · {s.duration}s"
            else:
                label = s.name
            out[label] = s
        return out

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
        sc = self.edit_label_map.get(choice)
        if sc is None:
            # Fallback for a plain-name choice (e.g. programmatic set with no dup labels).
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
        if self._wants_duration(KIND_TO_LABEL.get(sc.kind, "Intrusion")):
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
        if ip and ":" in ip:
            # Port is its own field -- a stray "10.23.23.182:" or "10.23.23.182:5010"
            # in the IP box combines with that field into an invalid "ip::port" URL
            # (InvalidURL) instead of a clear error, so strip it here.
            stripped = ip.split(":", 1)[0].strip()
            self._log(f"[!] IP field had a ':' in it (port is its own field) -- using '{stripped}'.")
            ip = stripped
        if not (ip and user and password):
            self._log("[!] Enter IP, username, and password first.")
            return None
        is_axis = vendor_adapter.camera_engine.classify_manufacturer(self.mfg_var.get()) == "AXIS"
        channel = self._axis_sensor_or_none() if is_axis else self.channel_var.get()
        try:
            self.adapter = vendor_adapter.make_adapter(
                self.mfg_var.get(), ip, self._port(), user, password,
                channel=channel)
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
        if adapter.vendor == "Hikvision":
            self._log("[.] PTZ units are first sent to their analytics scene -- allow ~7s.")

        def work():
            try:
                snap_log = lambda m: self.msg_queue.put(("log", m))
                self.msg_queue.put(("snapshot", adapter.fetch_snapshot(log=snap_log)))
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
                    snap_log = lambda m: self.msg_queue.put(("log", m))
                    self.msg_queue.put(("snapshot", adapter.fetch_snapshot(log=snap_log)))
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
                    # Hik rules carry their address (channel/scene doc/ruleId) so six
                    # same-named rules stay tellable-apart in the log.
                    loc_txt = ""
                    if isinstance(s.native_id, tuple):
                        ch, sid, rid = s.native_id
                        loc_txt = f" @ch{ch}/s{sid}/r{rid}"
                    lines.append(f"    '{s.name}'{loc_txt} {s.kind} classes={s.classes}"
                                 + (f" dur={s.duration}" if s.duration else "")
                                 + (f" excl={len(s.exclusions)}" if s.exclusions else "") + size_txt)
                self.msg_queue.put(("overlays", overlays, lines, scenarios))
            except Exception as e:
                self.msg_queue.put(("log", f"[!] Read failed: {e}"))
        self._run_bg(work)

    # ---- canvas drawing
    def _fit_image(self):
        """(Re)fit the snapshot to the CURRENT canvas size: recompute scale/offset and
        rebuild the display bitmap. Coordinates always normalize against the original
        full-res image, so refitting never touches what gets written to the camera."""
        if self.pil_image is None or not self.img_w or not self.img_h:
            self.scale, self.offset_x, self.offset_y = 1.0, 0, 0
            self.tk_image = None
            return
        cw, ch = max(self.canvas_w, 1), max(self.canvas_h, 1)
        self.scale = min(cw / self.img_w, ch / self.img_h)
        disp_w = max(int(self.img_w * self.scale), 1)
        disp_h = max(int(self.img_h * self.scale), 1)
        self.offset_x = (cw - disp_w) // 2
        self.offset_y = (ch - disp_h) // 2
        self.tk_image = ImageTk.PhotoImage(self.pil_image.resize((disp_w, disp_h), Image.LANCZOS))

    def _on_canvas_configure(self, event):
        """The canvas now follows the window. Debounce the flood of Configure events a
        drag produces -- refitting does a LANCZOS resize of the full-res snapshot."""
        if abs(event.width - self.canvas_w) < 2 and abs(event.height - self.canvas_h) < 2:
            return
        self._pending_canvas_size = (event.width, event.height)
        if self._resize_job is not None:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(90, self._apply_canvas_size)

    def _apply_canvas_size(self):
        self._resize_job = None
        if self._pending_canvas_size is None:
            return
        old_scale, old_ox, old_oy = self.scale, self.offset_x, self.offset_y
        had_image = self.tk_image is not None
        self.canvas_w, self.canvas_h = self._pending_canvas_size
        self._pending_canvas_size = None
        self._fit_image()
        if had_image and old_scale:
            self._remap_points(old_scale, old_ox, old_oy)
        self._redraw()

    def _remap_points(self, old_scale, old_ox, old_oy):
        """In-progress geometry is stored in canvas pixels, so a resize has to move every
        stored point to the same spot on the newly scaled image. (existing_overlays and
        edit_size are kept as fractions and follow the new scale on their own.)"""
        def remap(p):
            fx = (p[0] - old_ox) / old_scale / self.img_w
            fy = (p[1] - old_oy) / old_scale / self.img_h
            return self._frac_to_canvas(fx, fy)

        self.points = [remap(p) for p in self.points]
        self.exclude_zones = [[remap(p) for p in zone] for zone in self.exclude_zones]
        self.current_exclude = [remap(p) for p in self.current_exclude]
        self.current_bar = [remap(p) for p in self.current_bar]
        for bar in self.perspective_bars:
            bar["points"] = [remap(p) for p in bar["points"]]
        if self.size_first is not None:
            self.size_first = remap(self.size_first)

    def _display_image(self, pil_img):
        self.pil_image = pil_img
        self.img_w, self.img_h = pil_img.size
        self._fit_image()
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

    DRAG_RADIUS = 9  # px hit-radius for grabbing a plotted vertex

    def _find_vertex(self, cx, cy):
        """Return a descriptor for the plotted vertex nearest to (cx, cy) within
        DRAG_RADIUS, or None. Searches include zone/line, exclusion polygons, and
        perspective-bar endpoints (all canvas-pixel points)."""
        r2 = self.DRAG_RADIUS ** 2
        def near(p):
            return (p[0] - cx) ** 2 + (p[1] - cy) ** 2 <= r2
        for i, p in enumerate(self.points):
            if near(p):
                return ("points", i)
        for zi, zone in enumerate(self.exclude_zones):
            for i, p in enumerate(zone):
                if near(p):
                    return ("exclude", zi, i)
        for i, p in enumerate(self.current_exclude):
            if near(p):
                return ("current_exclude", i)
        for bi, bar in enumerate(self.perspective_bars):
            for i, p in enumerate(bar["points"]):
                if near(p):
                    return ("bar", bi, i)
        for i, p in enumerate(self.current_bar):
            if near(p):
                return ("current_bar", i)
        # Min/max size-box corners (rects stored as fractions; derive canvas corners).
        for si, (rect, _lbl) in enumerate(self.edit_size):
            fx, fy, fw, fh = rect
            corners = ((fx, fy), (fx + fw, fy), (fx + fw, fy + fh), (fx, fy + fh))
            for ci, (cfx, cfy) in enumerate(corners):
                if near(self._frac_to_canvas(cfx, cfy)):
                    return ("size", si, ci)
        return None

    def _set_vertex(self, target, x, y):
        kind = target[0]
        if kind == "points":
            self.points[target[1]] = (x, y)
        elif kind == "exclude":
            self.exclude_zones[target[1]][target[2]] = (x, y)
        elif kind == "current_exclude":
            self.current_exclude[target[1]] = (x, y)
        elif kind == "bar":
            self.perspective_bars[target[1]]["points"][target[2]] = (x, y)
        elif kind == "current_bar":
            self.current_bar[target[1]] = (x, y)
        elif kind == "size":
            # target = ("size", box_index, (anchor_fx, anchor_fy)); resize the rect so the
            # grabbed corner follows the mouse while the opposite corner stays pinned.
            si, (ax, ay) = target[1], target[2]
            fx, fy = self._canvas_to_frac(x, y)
            rect = (min(ax, fx), min(ay, fy), abs(fx - ax), abs(fy - ay))
            _r, lbl = self.edit_size[si]
            self.edit_size[si] = (rect, lbl)

    def _on_canvas_drag(self, event):
        if self.drag_target is None:
            return
        # clamp to the displayed image so a vertex can't be dragged off-frame
        x = max(self.offset_x, min(self.offset_x + self.img_w * self.scale, event.x))
        y = max(self.offset_y, min(self.offset_y + self.img_h * self.scale, event.y))
        self._set_vertex(self.drag_target, x, y)
        self._redraw()

    def _on_canvas_release(self, _event):
        self.drag_target = None

    def _on_canvas_click(self, event):
        if not self.tk_image:
            return
        # Grab-and-drag: if the click lands on an existing plotted vertex, start dragging
        # it instead of adding a new point (works in any mode, like the Axis web UI).
        target = self._find_vertex(event.x, event.y)
        if target is not None:
            if target[0] == "size":
                # Pin the diagonally-opposite corner (in fractions) for the whole drag so
                # the box resizes cleanly and can't flip-flop as corners cross over.
                _, si, ci = target
                fx, fy, fw, fh = self.edit_size[si][0]
                corners = ((fx, fy), (fx + fw, fy), (fx + fw, fy + fh), (fx, fy + fh))
                self.drag_target = ("size", si, corners[(ci + 2) % 4])
            else:
                self.drag_target = target
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
        # cursor hint: show a grab cursor when hovering over a draggable vertex
        self.canvas.configure(cursor="hand2" if self._find_vertex(event.x, event.y) else "")
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
        """Return the canvas to a blank slate: remove the reference overlays drawn by
        Read Current Config (existing scenarios, size boxes, perspective bars) AND
        discard any scenario currently loaded for editing, so the view goes back to
        just the snapshot with the dropdown on New Scenario. Display-only -- does NOT
        change anything on the camera."""
        had_overlays = bool(self.existing_overlays)
        had_active = bool(self.points or self.exclude_zones or self.current_exclude
                          or self.edit_size or self.perspective_bars
                          or self.editing is not None)
        if not (had_overlays or had_active):
            self._log("[.] Nothing to clear.")
            return
        self.existing_overlays = []
        # Route the active-drawing reset through the New Scenario path so the editing
        # state, name, geometry and the scenario dropdown all return to blank together.
        # (Previously this only cleared the reference overlays, leaving a selected
        # scenario drawn on screen -- you had to pick "New Scenario" or re-fetch.)
        self.edit_var.set(NEW_SCENARIO)
        self._on_edit_select(NEW_SCENARIO)
        self._log("[.] Cleared to a blank snapshot (camera unchanged).")

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
        if self._wants_duration(rule):
            val = self.loiter_entry.get().strip()
            if kind == "loiter" and not val:
                return None, "Loitering needs a dwell time in seconds."
            if val:
                try:
                    duration = int(val)
                except ValueError:
                    return None, "Enter the dwell / loiter time as a whole number of seconds."

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
            if min_size and max_size:
                region_bbox = _points_bbox(points)
                if not _rect_overlaps(region_bbox, min_size):
                    return None, ("The min size box doesn't overlap the drawn zone -- this "
                                  "camera rejects the whole push if a size box sits outside "
                                  "the zone. Redraw the min box over the zone.")
                if not _rect_overlaps(region_bbox, max_size):
                    return None, ("The max size box doesn't overlap the drawn zone -- this "
                                  "camera rejects the whole push if a size box sits outside "
                                  "the zone. Redraw the max box over the zone.")

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

        ip, port = self.ip_entry.get().strip(), self._port()
        sensor_txt = ""
        if adapter.vendor == "Axis" and getattr(adapter, "device_id", None):
            sensor_txt = f", sensor {adapter.device_id}"
        elif isinstance(self.editing, tuple):
            # Hik edit: say exactly which rule gets patched -- with six same-named
            # rules on a camera, "RULE1" alone doesn't identify the write target.
            e_ch, e_sid, e_rid = self.editing
            sensor_txt = f", ch{e_ch} scene {e_sid} rule {e_rid}"
        excl_txt = f"\n{len(sc.exclusions)} exclusion zone(s) included." if sc.exclusions else ""
        verb = "Update existing scenario" if self.editing is not None else "Push new scenario"
        if not messagebox.askyesno(
            "Confirm live write",
            f"{verb} '{sc.name}' ({sc.kind}) on {adapter.vendor}\n{ip}:{port}{sensor_txt}?{excl_txt}\n\n"
            f"The current config is backed up first, and other scenarios are preserved. "
            f"This changes live camera analytics."):
            self._log("[.] Push cancelled.")
            return

        self.push_button.configure(state="disabled", text="Pushing...")
        self._log(f"[*] Backing up + pushing '{sc.name}' to {adapter.vendor} {ip}:{port} ...")
        backup_dir = HIK_BACKUP_DIR if adapter.vendor == "Hikvision" else BACKUP_DIR

        snapshot_img = self.pil_image  # frame the zone was drawn on; save it beside the backup

        def work():
            try:
                backup_path, _verify = adapter.apply_scenario(sc, backup_dir)
                try:
                    pair = save_snapshot_pair(backup_path, snapshot_img)
                except Exception as e:  # never let a snapshot-save failure fail the push
                    pair = None
                    self.msg_queue.put(("log", f"[!] Snapshot pair not saved: {e}"))
                names = [s.name for s in adapter.read_scenarios()]
                self.msg_queue.put(("push_done", Path(backup_path).name, names,
                                    Path(pair).name if pair else None))
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
                    self.edit_label_map = self._build_edit_labels(scenarios)
                    names = [NEW_SCENARIO] + list(self.edit_label_map.keys())
                    self.edit_menu.configure(values=names)
                    self._redraw()
                    self._log("\n".join(lines))
                    if overlays:
                        self._log(f"[+] Overlaid {len(overlays)} existing zone(s) in amber. "
                                  f"Pick one from 'Edit existing' to modify it, or draw a new one.")
                elif msg[0] == "push_done":
                    _, backup_name, names, pair_name = msg
                    self.push_button.configure(state="normal", text="Push to Camera")
                    self._log(f"[+] Pushed OK. Backup: {backup_name}. Scenarios now: {names}")
                    if pair_name:
                        self._log(f"[+] Snapshot saved for ML pair: {pair_name}")
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
                # Guard against a mangled append (PowerShell `>>` writes UTF-16 and a
                # file without a trailing newline glues the new line onto the last
                # value) -- that once corrupted HIK_PASSWORD and made every Hik read
                # in this GUI fail auth. Strip NULs, then truncate any value that has
                # another VAR= assignment fused onto it.
                text = env_file.read_text(errors="ignore").replace("\0", "")
                env = {}
                for k, v in re.findall(r"^([A-Z_]+)=(.*)$", text, re.M):
                    glued = re.search(r"[A-Z][A-Z_]{2,}=", v)
                    env[k] = (v[:glued.start()] if glued else v).strip()
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
