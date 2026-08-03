"""LiveView Technologies Camera Analytics -- desktop GUI.

Run with: python audit_gui.py
"""

import csv
import os
import platform
import queue
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

import camera_engine
import fleet_catalog

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# --- LiveView Technologies brand palette (from CAMERA_CONFIGS in camera_engine.py) ---
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

ctk.set_appearance_mode("light")


class CredentialBlock(ctk.CTkFrame):
    """Username/password entry pair for one vendor, with a show/hide toggle and a
    'required for this run' indicator that gets updated as inputs change."""

    def __init__(self, master, title):
        super().__init__(master, fg_color=LVT_LIGHT, corner_radius=10)
        self.title_text = title

        self.title_label = ctk.CTkLabel(self, text=title, font=ctk.CTkFont(size=14, weight="bold"), text_color=LVT_TEXT_DARK)
        self.title_label.grid(row=0, column=0, columnspan=2, sticky="w", padx=12, pady=(10, 4))

        ctk.CTkLabel(self, text="Username", text_color=LVT_TEXT_MUTED, font=ctk.CTkFont(size=11)).grid(row=1, column=0, sticky="w", padx=12)
        self.user_entry = ctk.CTkEntry(self, placeholder_text="username")
        self.user_entry.grid(row=2, column=0, columnspan=2, sticky="ew", padx=12, pady=(0, 8))

        ctk.CTkLabel(self, text="Password", text_color=LVT_TEXT_MUTED, font=ctk.CTkFont(size=11)).grid(row=3, column=0, sticky="w", padx=12)
        self.pass_entry = ctk.CTkEntry(self, placeholder_text="password", show="*")
        self.pass_entry.grid(row=4, column=0, sticky="ew", padx=(12, 4), pady=(0, 10))

        self.show_var = tk.BooleanVar(value=False)
        self.show_toggle = ctk.CTkCheckBox(self, text="Show", variable=self.show_var, command=self._toggle_show,
                                            width=18, checkbox_width=18, checkbox_height=18,
                                            fg_color=LVT_TEAL, hover_color=LVT_TEAL_HOVER)
        self.show_toggle.grid(row=4, column=1, sticky="w", padx=(4, 12), pady=(0, 10))

        self.grid_columnconfigure(0, weight=1)

    def _toggle_show(self):
        self.pass_entry.configure(show="" if self.show_var.get() else "*")

    def set_required(self, required):
        if required:
            self.title_label.configure(text=f"{self.title_text}  (required for this run)", text_color=LVT_DARK_TEAL)
        else:
            self.title_label.configure(text=f"{self.title_text}  (not needed for this run)", text_color=LVT_TEXT_MUTED)

    def username(self):
        return self.user_entry.get().strip()

    def password(self):
        return self.pass_entry.get().strip()


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("LiveView Technologies Camera Analytics")
        self.geometry("980x950")
        self.minsize(860, 760)
        self.configure(fg_color=LVT_WHITE)

        self.msg_queue = queue.Queue()
        self.csv_rows = None
        self.csv_path = None
        self.worker_thread = None
        self.run_start_time = None

        # --- Fleet Picker (Snowflake / cached catalog) state ---
        self.catalog_source = None          # a fleet_catalog.CatalogSource, built lazily
        self.picker_source_pref = "auto"    # "auto" | "live" | "cache"
        self.picker_basket = []             # resolved camera rows queued for audit
        self.basket_serials = set()         # TDC serials already in the basket (dedupe)
        self.unit_checkboxes = {}           # serial -> BooleanVar for the unit list
        self._clients_loaded = False

        self._build_header()
        self._build_tabs()
        self._build_credentials()
        self._build_controls()
        self._build_log()

        self._refresh_credential_requirements()
        self.after(100, self._poll_queue)

    # ---------------------------------------------------------------- layout

    def _build_header(self):
        header = ctk.CTkFrame(self, fg_color=LVT_DARK_TEAL, corner_radius=0, height=72)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        ctk.CTkLabel(header, text="LiveView Technologies Camera Analytics", font=ctk.CTkFont(size=22, weight="bold"),
                     text_color=LVT_WHITE).pack(side="left", padx=24, pady=10)
        ctk.CTkLabel(header, text="Camera Analytics Report Generator", font=ctk.CTkFont(size=12),
                     text_color=LVT_LIGHT).pack(side="left", padx=(0, 24), pady=(20, 10))

    def _build_tabs(self):
        self.tabview = ctk.CTkTabview(
            self, fg_color=LVT_LIGHT, segmented_button_fg_color=LVT_TEAL,
            segmented_button_selected_color=LVT_DARK_TEAL, segmented_button_selected_hover_color=LVT_DARK_TEAL_HOVER,
            segmented_button_unselected_color=LVT_TEAL, segmented_button_unselected_hover_color=LVT_TEAL_HOVER,
            text_color=LVT_WHITE, command=self._on_tab_changed,
        )
        self.tabview.pack(fill="x", padx=20, pady=(16, 8))
        self.tab_single = self.tabview.add("Single Camera Test")
        self.tab_csv = self.tabview.add("CSV Batch")
        self.tab_picker = self.tabview.add("Fleet Picker")
        self._build_single_tab(self.tab_single)
        self._build_csv_tab(self.tab_csv)
        self._build_picker_tab(self.tab_picker)

    def _build_single_tab(self, tab):
        tab.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(tab, text="Camera IP Address", text_color=LVT_TEXT_DARK).grid(row=0, column=0, sticky="w", padx=(4, 12), pady=8)
        self.single_ip_entry = ctk.CTkEntry(tab, placeholder_text="e.g. 10.23.66.205")
        self.single_ip_entry.grid(row=0, column=1, sticky="ew", pady=8)

        ctk.CTkLabel(tab, text="Manufacturer", text_color=LVT_TEXT_DARK).grid(row=1, column=0, sticky="w", padx=(4, 12), pady=8)
        self.single_mfg_var = tk.StringVar(value="Hikvision")
        self.single_mfg_menu = ctk.CTkOptionMenu(tab, values=["Hikvision", "Axis"], variable=self.single_mfg_var,
                                                  fg_color=LVT_TEAL, button_color=LVT_DARK_TEAL, button_hover_color=LVT_DARK_TEAL_HOVER,
                                                  command=lambda _v: self._refresh_credential_requirements())
        self.single_mfg_menu.grid(row=1, column=1, sticky="w", pady=8)

        ctk.CTkLabel(tab, text="Client Name (Optional)", text_color=LVT_TEXT_DARK).grid(row=2, column=0, sticky="w", padx=(4, 12), pady=8)
        self.single_client_entry = ctk.CTkEntry(tab, placeholder_text="Single Test")
        self.single_client_entry.grid(row=2, column=1, sticky="ew", pady=8)

        ctk.CTkLabel(tab, text="Location (Optional)", text_color=LVT_TEXT_DARK).grid(row=3, column=0, sticky="w", padx=(4, 12), pady=8)
        self.single_location_entry = ctk.CTkEntry(tab, placeholder_text="Diagnostic")
        self.single_location_entry.grid(row=3, column=1, sticky="ew", pady=8)

        ctk.CTkLabel(tab, text="Unit Serial (Optional)", text_color=LVT_TEXT_DARK).grid(row=4, column=0, sticky="w", padx=(4, 12), pady=8)
        self.single_serial_entry = ctk.CTkEntry(tab, placeholder_text="N/A")
        self.single_serial_entry.grid(row=4, column=1, sticky="ew", pady=(8, 14))

    def _build_csv_tab(self, tab):
        tab.grid_columnconfigure(1, weight=1)

        browse_btn = ctk.CTkButton(tab, text="Browse for CSV...", command=self._browse_csv,
                                    fg_color=LVT_TEAL, hover_color=LVT_TEAL_HOVER, text_color=LVT_WHITE)
        browse_btn.grid(row=0, column=0, sticky="w", padx=(4, 12), pady=10)
        self.csv_path_label = ctk.CTkLabel(tab, text="No file selected", text_color=LVT_TEXT_MUTED)
        self.csv_path_label.grid(row=0, column=1, sticky="w", pady=10)

        self.csv_status_label = ctk.CTkLabel(tab, text="", text_color=LVT_TEXT_DARK, justify="left", anchor="w")
        self.csv_status_label.grid(row=1, column=0, columnspan=2, sticky="ew", padx=4, pady=(0, 10))

        ctk.CTkLabel(tab, text="Custom Tag / Job Name (Optional)", text_color=LVT_TEXT_DARK).grid(row=2, column=0, sticky="w", padx=(4, 12), pady=8)
        self.csv_tag_entry = ctk.CTkEntry(tab, placeholder_text="uses CSV filename if left blank")
        self.csv_tag_entry.grid(row=2, column=1, sticky="ew", pady=(8, 14))

    def _build_picker_tab(self, tab):
        """Cascading Client -> Location -> TDC picker backed by fleet_catalog.

        Checked units get resolved to camera rows and dropped into a batch
        basket that persists across client/location changes -- so a TAM can
        assemble a multi-client audit without a CSV, or mix in units on top of
        one. Feeds run_batch identically to the CSV tab."""
        # The whole tab is ONE bounded scroll page, so it can never push the
        # credentials / Start controls (below the tabview) off-screen -- and the
        # inner lists are plain blocks, NOT their own scroll regions, so there is
        # a SINGLE scrollbar for the panel instead of several fighting ones.
        page = ctk.CTkScrollableFrame(tab, fg_color=LVT_LIGHT, height=430)
        page.pack(fill="x", expand=False)
        page.grid_columnconfigure(1, weight=1)
        self.picker_page = page

        # -- data source row --
        ctk.CTkLabel(page, text="Data source", text_color=LVT_TEXT_DARK).grid(row=0, column=0, sticky="w", padx=(4, 12), pady=(8, 4))
        source_row = ctk.CTkFrame(page, fg_color="transparent")
        source_row.grid(row=0, column=1, sticky="ew", pady=(8, 4))
        self.picker_source_var = tk.StringVar(value="Auto")
        self.picker_source_menu = ctk.CTkOptionMenu(
            source_row, values=["Auto", "Live Snowflake", "Cached catalog"], variable=self.picker_source_var,
            fg_color=LVT_TEAL, button_color=LVT_DARK_TEAL, button_hover_color=LVT_DARK_TEAL_HOVER,
            command=self._picker_set_source, width=170)
        self.picker_source_menu.pack(side="left")
        ctk.CTkButton(source_row, text="Reload", width=80, command=self._picker_reload,
                      fg_color=LVT_TEAL, hover_color=LVT_TEAL_HOVER, text_color=LVT_WHITE).pack(side="left", padx=8)
        self.picker_source_status = ctk.CTkLabel(page, text="Not loaded yet.", text_color=LVT_TEXT_MUTED, anchor="w")
        self.picker_source_status.grid(row=1, column=0, columnspan=2, sticky="ew", padx=4, pady=(0, 6))

        # -- client: type-to-filter (the live fleet has thousands of clients, so
        #    a plain dropdown is unusable). Typing filters a short results list;
        #    clicking a result selects the client and loads its locations. --
        self.picker_client_var = tk.StringVar(value="—")
        self._all_clients = []
        ctk.CTkLabel(page, text="Client", text_color=LVT_TEXT_DARK).grid(row=2, column=0, sticky="w", padx=(4, 12), pady=6)
        self.picker_client_search = ctk.CTkEntry(page, placeholder_text="Type to filter clients…")
        self.picker_client_search.grid(row=2, column=1, sticky="ew", pady=6)
        self.picker_client_search.bind("<KeyRelease>", self._filter_clients)
        self.picker_client_search.configure(state="disabled")

        self.picker_client_results = ctk.CTkFrame(page, fg_color=LVT_WHITE)
        self.picker_client_results.grid(row=3, column=0, columnspan=2, sticky="ew", padx=4, pady=(0, 6))
        self.picker_client_hint = ctk.CTkLabel(self.picker_client_results, text="Load a source to list clients.",
                                               text_color=LVT_TEXT_MUTED)
        self.picker_client_hint.pack(anchor="w", padx=6, pady=6)

        # -- location: same type-to-filter pattern as client --
        self.picker_location_var = tk.StringVar(value="—")
        self._all_locations = []
        ctk.CTkLabel(page, text="Location", text_color=LVT_TEXT_DARK).grid(row=4, column=0, sticky="w", padx=(4, 12), pady=6)
        self.picker_location_search = ctk.CTkEntry(page, placeholder_text="Filter locations, or scroll the list to browse…")
        self.picker_location_search.grid(row=4, column=1, sticky="ew", pady=6)
        self.picker_location_search.bind("<KeyRelease>", self._filter_locations)
        self.picker_location_search.configure(state="disabled")

        self.picker_location_results = ctk.CTkFrame(page, fg_color=LVT_WHITE)
        self.picker_location_results.grid(row=5, column=0, columnspan=2, sticky="ew", padx=4, pady=(0, 6))
        ctk.CTkLabel(self.picker_location_results, text="Pick a client first.",
                     text_color=LVT_TEXT_MUTED).pack(anchor="w", padx=6, pady=6)

        # -- unit (TDC) multi-select --
        unit_header = ctk.CTkFrame(page, fg_color="transparent")
        unit_header.grid(row=6, column=0, columnspan=2, sticky="ew", padx=4, pady=(8, 2))
        ctk.CTkLabel(unit_header, text="Units (TDC) — check to select", text_color=LVT_TEXT_DARK).pack(side="left")
        ctk.CTkButton(unit_header, text="Clear", width=60, command=lambda: self._picker_check_all(False),
                      fg_color=LVT_TEAL, hover_color=LVT_TEAL_HOVER, text_color=LVT_WHITE).pack(side="right", padx=(6, 0))
        ctk.CTkButton(unit_header, text="Select all", width=80, command=lambda: self._picker_check_all(True),
                      fg_color=LVT_TEAL, hover_color=LVT_TEAL_HOVER, text_color=LVT_WHITE).pack(side="right")

        self.picker_units_frame = ctk.CTkFrame(page, fg_color=LVT_WHITE)
        self.picker_units_frame.grid(row=7, column=0, columnspan=2, sticky="ew", padx=4, pady=(0, 6))
        self.picker_units_empty = ctk.CTkLabel(self.picker_units_frame, text="Pick a client and location to list units.",
                                               text_color=LVT_TEXT_MUTED)
        self.picker_units_empty.pack(anchor="w", padx=6, pady=6)

        self.picker_add_btn = ctk.CTkButton(page, text="Add checked units to batch  ↓", command=self._add_units_to_batch,
                                            fg_color=LVT_DARK_TEAL, hover_color=LVT_DARK_TEAL_HOVER, text_color=LVT_WHITE, state="disabled")
        self.picker_add_btn.grid(row=8, column=0, columnspan=2, sticky="ew", padx=4, pady=(0, 8))

        # -- batch basket (persists across client/location changes) --
        basket_header = ctk.CTkFrame(page, fg_color="transparent")
        basket_header.grid(row=9, column=0, columnspan=2, sticky="ew", padx=4, pady=(2, 2))
        self.basket_summary = ctk.CTkLabel(basket_header, text="Batch is empty.", text_color=LVT_TEXT_DARK,
                                           font=ctk.CTkFont(size=13, weight="bold"))
        self.basket_summary.pack(side="left")
        ctk.CTkButton(basket_header, text="Clear batch", width=90, command=self._clear_batch,
                      fg_color=LVT_TEAL, hover_color=LVT_TEAL_HOVER, text_color=LVT_WHITE).pack(side="right")

        self.basket_frame = ctk.CTkFrame(page, fg_color=LVT_WHITE)
        self.basket_frame.grid(row=10, column=0, columnspan=2, sticky="ew", padx=4, pady=(0, 10))
        self._refresh_basket_view()

    # --------------------------------------------------------- picker helpers

    def _bg(self, fn, tag, *args):
        """Run fn(*args) off the UI thread; post (tag, result) or a picker error
        back through the message queue."""
        def work():
            try:
                self.msg_queue.put((tag, fn(*args)))
            except Exception as e:  # CatalogError and anything the driver throws
                self.msg_queue.put(("picker_error", str(e)))
        threading.Thread(target=work, daemon=True).start()

    def _picker_pref_from_choice(self):
        return {"Auto": "auto", "Live Snowflake": "live", "Cached catalog": "cache"}[self.picker_source_var.get()]

    def _picker_set_source(self, _choice=None):
        self.picker_source_pref = self._picker_pref_from_choice()
        self._picker_reload()

    def _picker_reload(self):
        # Drop any existing connection and reload the client list from scratch.
        if self.catalog_source is not None:
            try:
                self.catalog_source.close()
            except Exception:
                pass
            self.catalog_source = None
        self._clients_loaded = False
        self._all_clients = []
        self.picker_client_search.delete(0, "end")
        self.picker_client_search.configure(state="disabled")
        self.picker_client_var.set("—")
        self._render_client_results([], "Connecting…")
        self._reset_locations("Pick a client first.")
        self._clear_unit_list("Loading…")
        self.picker_source_status.configure(text="Connecting…", text_color=LVT_TEXT_MUTED)
        self._bg(self._build_source_and_list_clients, "picker_clients")

    def _build_source_and_list_clients(self):
        src = fleet_catalog.build_source(prefer=self.picker_source_pref)
        clients = src.list_clients()
        return (src, clients)

    def _picker_ensure_loaded(self):
        """Called when the picker tab is first shown -- kick off the initial load."""
        if not self._clients_loaded and self.catalog_source is None:
            self.picker_source_pref = self._picker_pref_from_choice()
            self._picker_reload()

    _CLIENT_RESULT_CAP = 8  # keep the list SHORT (type to narrow); the fleet has thousands

    def _filter_clients(self, _event=None):
        if not self._all_clients:
            return
        typed = self.picker_client_search.get().strip().lower()
        matches = [c for c in self._all_clients if typed in c.lower()] if typed else self._all_clients
        shown = matches[:self._CLIENT_RESULT_CAP]
        self._render_client_results(
            shown,
            empty_msg="No clients match." if typed else "Type to filter clients…",
            more=len(matches) - len(shown),
        )

    def _render_client_results(self, clients, empty_msg=None, more=0):
        for child in self.picker_client_results.winfo_children():
            child.destroy()
        if not clients:
            ctk.CTkLabel(self.picker_client_results, text=empty_msg or "Type to filter clients…",
                         text_color=LVT_TEXT_MUTED).pack(anchor="w", padx=6, pady=6)
            return
        selected = self.picker_client_var.get()
        for c in clients:
            is_sel = (c == selected)
            ctk.CTkButton(
                self.picker_client_results, text=("✓  " + c) if is_sel else c, anchor="w", height=24,
                fg_color=LVT_DARK_TEAL if is_sel else "transparent",
                text_color=LVT_WHITE if is_sel else LVT_TEXT_DARK,
                hover_color=LVT_LIGHT, command=lambda cc=c: self._choose_client(cc),
            ).pack(fill="x", padx=4, pady=1)
        if more > 0:
            ctk.CTkLabel(self.picker_client_results, text=f"…and {more} more — keep typing to narrow.",
                         text_color=LVT_TEXT_MUTED).pack(anchor="w", padx=6, pady=(2, 4))
        self._enable_wheel_scroll(self.picker_page)

    def _choose_client(self, client):
        self.picker_client_var.set(client)
        # Reflect the pick in the search box and collapse the list to just it.
        self.picker_client_search.delete(0, "end")
        self.picker_client_search.insert(0, client)
        self._render_client_results([client])
        self._on_client_selected(client)

    def _enable_wheel_scroll(self, scroll_frame):
        """Forward mouse-wheel events from a CTkScrollableFrame and every child
        widget to the frame's canvas, so the wheel scrolls the list even when the
        pointer is over a button/checkbox inside it (those otherwise swallow the
        event, forcing you to click the scrollbar arrow)."""
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

    def _clear_unit_list(self, message):
        for child in self.picker_units_frame.winfo_children():
            child.destroy()
        self.unit_checkboxes = {}
        self.picker_units_empty = ctk.CTkLabel(self.picker_units_frame, text=message, text_color=LVT_TEXT_MUTED)
        self.picker_units_empty.pack(anchor="w", padx=6, pady=6)
        self.picker_add_btn.configure(state="disabled")

    def _on_client_selected(self, client):
        if not client or client == "—" or self.catalog_source is None:
            return
        self._reset_locations("Loading locations…")
        self._clear_unit_list("Loading locations…")
        self._bg(self.catalog_source.list_locations, "picker_locations", client)

    # -- location: mirrors the client type-to-filter list --

    def _reset_locations(self, message):
        self._all_locations = []
        self.picker_location_var.set("—")
        self.picker_location_search.delete(0, "end")
        self.picker_location_search.configure(state="disabled")
        self._render_location_results([], message)

    def _filter_locations(self, _event=None):
        if not self._all_locations:
            return
        typed = self.picker_location_search.get().strip().lower()
        # Show ALL locations (no cap) so you can scroll and browse when you don't
        # know the exact site name -- the filter box just narrows if you do.
        matches = [l for l in self._all_locations if typed in l.lower()] if typed else self._all_locations
        self._render_location_results(
            matches,
            empty_msg="No locations match." if typed else "No locations for this client.",
            more=0,
        )

    def _render_location_results(self, locations, empty_msg=None, more=0):
        for child in self.picker_location_results.winfo_children():
            child.destroy()
        if not locations:
            ctk.CTkLabel(self.picker_location_results, text=empty_msg or "Type to filter locations…",
                         text_color=LVT_TEXT_MUTED).pack(anchor="w", padx=6, pady=6)
            return
        selected = self.picker_location_var.get()
        for loc in locations:
            is_sel = (loc == selected)
            ctk.CTkButton(
                self.picker_location_results, text=("✓  " + loc) if is_sel else loc, anchor="w", height=24,
                fg_color=LVT_DARK_TEAL if is_sel else "transparent",
                text_color=LVT_WHITE if is_sel else LVT_TEXT_DARK,
                hover_color=LVT_LIGHT, command=lambda ll=loc: self._choose_location(ll),
            ).pack(fill="x", padx=4, pady=1)
        if more > 0:
            ctk.CTkLabel(self.picker_location_results, text=f"…and {more} more — keep typing to narrow.",
                         text_color=LVT_TEXT_MUTED).pack(anchor="w", padx=6, pady=(2, 4))
        self._enable_wheel_scroll(self.picker_page)

    def _choose_location(self, location):
        self.picker_location_var.set(location)
        self.picker_location_search.delete(0, "end")
        self.picker_location_search.insert(0, location)
        self._render_location_results([location])
        self._on_location_selected(location)

    def _on_location_selected(self, location):
        client = self.picker_client_var.get()
        if not location or location == "—" or client == "—" or self.catalog_source is None:
            return
        self._clear_unit_list("Loading units…")
        self._bg(self.catalog_source.list_units, "picker_units", client, location)

    def _populate_units(self, serials):
        for child in self.picker_units_frame.winfo_children():
            child.destroy()
        self.unit_checkboxes = {}
        if not serials:
            self.picker_units_empty = ctk.CTkLabel(self.picker_units_frame, text="No units at this location.",
                                                   text_color=LVT_TEXT_MUTED)
            self.picker_units_empty.pack(anchor="w", padx=6, pady=6)
            self.picker_add_btn.configure(state="disabled")
            return
        for serial in serials:
            var = tk.BooleanVar(value=False)
            already = serial in self.basket_serials
            text = f"{serial}   (already in batch)" if already else serial
            cb = ctk.CTkCheckBox(self.picker_units_frame, text=text, variable=var,
                                 fg_color=LVT_TEAL, hover_color=LVT_TEAL_HOVER,
                                 text_color=LVT_TEXT_MUTED if already else LVT_TEXT_DARK)
            cb.pack(anchor="w", padx=6, pady=2)
            self.unit_checkboxes[serial] = var
        self.picker_add_btn.configure(state="normal")
        self._enable_wheel_scroll(self.picker_page)

    def _picker_check_all(self, value):
        for var in self.unit_checkboxes.values():
            var.set(value)

    def _add_units_to_batch(self):
        chosen = [s for s, var in self.unit_checkboxes.items() if var.get() and s not in self.basket_serials]
        if not chosen:
            messagebox.showinfo("Nothing to add", "Check one or more units that aren't already in the batch.")
            return
        if self.catalog_source is None:
            return
        self.picker_add_btn.configure(state="disabled", text="Resolving cameras…")
        self.picker_source_status.configure(text=f"Resolving cameras for {len(chosen)} unit(s)…", text_color=LVT_TEXT_MUTED)
        self._bg(self.catalog_source.resolve_cameras, "picker_added", chosen)

    def _clear_batch(self):
        self.picker_basket = []
        self.basket_serials = set()
        self._refresh_basket_view()
        # Repaint the unit list so "already in batch" tags disappear.
        if self.unit_checkboxes:
            self._populate_units(list(self.unit_checkboxes.keys()))
        self._refresh_credential_requirements()

    def _refresh_basket_view(self):
        for child in self.basket_frame.winfo_children():
            child.destroy()
        if not self.picker_basket:
            self.basket_summary.configure(text="Batch is empty.")
            ctk.CTkLabel(self.basket_frame, text="Add units above to build a batch (units can span multiple clients).",
                         text_color=LVT_TEXT_MUTED).pack(anchor="w", padx=6, pady=6)
            return
        # Group basket rows by serial for a compact per-unit summary.
        by_serial = {}
        for row in self.picker_basket:
            by_serial.setdefault(row["LIVE_UNIT_SERIAL_NM"], []).append(row)
        clients = {r["CLIENT_NM"] for r in self.picker_basket}
        self.basket_summary.configure(
            text=f"Batch: {len(by_serial)} unit(s), {len(self.picker_basket)} camera(s), {len(clients)} client(s)")
        for serial, rows in by_serial.items():
            line = f"{serial}  —  {rows[0]['CLIENT_NM']} / {rows[0]['LOCATION_NM']}  ({len(rows)} cam)"
            ctk.CTkLabel(self.basket_frame, text=line, text_color=LVT_TEXT_DARK, anchor="w").pack(anchor="w", padx=6, pady=1)
        self._enable_wheel_scroll(self.picker_page)

    def _build_credentials(self):
        frame = ctk.CTkFrame(self, fg_color="transparent")
        frame.pack(fill="x", padx=20, pady=8)
        frame.grid_columnconfigure((0, 1), weight=1)

        self.axis_creds = CredentialBlock(frame, "Axis Credentials")
        self.axis_creds.grid(row=0, column=0, sticky="nsew", padx=(0, 8))

        self.hik_creds = CredentialBlock(frame, "Hikvision Credentials")
        self.hik_creds.grid(row=0, column=1, sticky="nsew", padx=(8, 0))

    def _build_controls(self):
        frame = ctk.CTkFrame(self, fg_color="transparent")
        frame.pack(fill="x", padx=20, pady=(4, 8))
        frame.grid_columnconfigure(0, weight=1)

        self.start_button = ctk.CTkButton(frame, text="Start Processing", command=self._start_processing,
                                           fg_color=LVT_DARK_TEAL, hover_color=LVT_DARK_TEAL_HOVER, text_color=LVT_WHITE,
                                           font=ctk.CTkFont(size=14, weight="bold"), height=40)
        self.start_button.grid(row=0, column=0, sticky="w")

        self.open_folder_button = ctk.CTkButton(frame, text="Open Report Folder", command=self._open_report_folder,
                                                 fg_color=LVT_TEAL, hover_color=LVT_TEAL_HOVER, text_color=LVT_WHITE,
                                                 state="disabled", height=40)
        self.open_folder_button.grid(row=0, column=1, sticky="e", padx=(8, 0))

        self.progress_bar = ctk.CTkProgressBar(frame, progress_color=LVT_TEAL)
        self.progress_bar.set(0)
        self.progress_bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 2))

        self.progress_label = ctk.CTkLabel(frame, text="Idle", text_color=LVT_TEXT_MUTED)
        self.progress_label.grid(row=2, column=0, columnspan=2, sticky="w")

        self.last_report_path = None

    def _build_log(self):
        frame = ctk.CTkFrame(self, fg_color="transparent")
        frame.pack(fill="both", expand=True, padx=20, pady=(0, 16))
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(0, weight=1)

        self.log_box = ctk.CTkTextbox(frame, fg_color=LVT_LOG_BG, text_color=LVT_LOG_TEXT,
                                       font=ctk.CTkFont(family="Consolas", size=12), wrap="word")
        self.log_box.grid(row=0, column=0, sticky="nsew")
        self.log_box.configure(state="disabled")

    # ------------------------------------------------------------- behavior

    def _on_tab_changed(self):
        if self.tabview.get() == "Fleet Picker":
            self._picker_ensure_loaded()
        self._refresh_credential_requirements()

    def _current_mfg_needs(self):
        """Returns (needs_axis, needs_hik) for whatever mode/rows are currently active."""
        active = self.tabview.get()
        if active == "Single Camera Test":
            mfg_class = camera_engine.classify_manufacturer(self.single_mfg_var.get())
            return mfg_class == "AXIS", mfg_class == "HIKVISION"
        rows = self.picker_basket if active == "Fleet Picker" else self.csv_rows
        if rows:
            classes = [camera_engine.classify_manufacturer(r.get("MANUFACTURER", "")) for r in rows if r.get("IP", "").strip()]
            return "AXIS" in classes or "MIXED" in classes, "HIKVISION" in classes or "MIXED" in classes
        return False, False

    def _refresh_credential_requirements(self):
        needs_axis, needs_hik = self._current_mfg_needs()
        self.axis_creds.set_required(needs_axis)
        self.hik_creds.set_required(needs_hik)

    def _browse_csv(self):
        selected = filedialog.askopenfilename(
            initialdir=Path.home() / "Downloads",
            title="Select Camera Layout CSV File",
            filetypes=(("CSV Files", "*.csv"), ("All Files", "*.*")),
        )
        if not selected:
            return

        csv_path = Path(selected)
        try:
            with open(csv_path, mode='r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                rows = list(reader)
                fieldnames = reader.fieldnames
        except Exception as e:
            messagebox.showerror("Could not read CSV", f"Failed to read '{csv_path.name}':\n{e}")
            return

        if not rows:
            messagebox.showerror("Empty CSV", f"'{csv_path.name}' has no data rows.")
            return
        if not fieldnames or "IP" not in fieldnames:
            messagebox.showerror("Missing IP column", f"'{csv_path.name}' is missing a required 'IP' column.\n\nFound columns: {fieldnames}")
            return
        rows_with_ip = sum(1 for r in rows if r.get("IP", "").strip())
        if rows_with_ip == 0:
            messagebox.showerror("No IP values", f"'{csv_path.name}' has an 'IP' column but every value is blank.")
            return

        self.csv_rows = rows
        self.csv_path = csv_path
        self.csv_path_label.configure(text=csv_path.name, text_color=LVT_TEXT_DARK)

        status_lines = [f"Loaded {len(rows)} row(s), {rows_with_ip} with a valid IP."]
        if rows_with_ip < len(rows):
            status_lines.append(f"{len(rows) - rows_with_ip} row(s) missing an IP will be skipped.")

        deduped_rows = camera_engine.dedupe_camera_rows(rows)
        if len(deduped_rows) < len(rows):
            status_lines.append(f"Collapsed {len(rows) - len(deduped_rows)} duplicate-IP row(s) into {len(deduped_rows)} unique device(s).")
        rows = deduped_rows
        self.csv_rows = rows

        classes = [camera_engine.classify_manufacturer(r.get("MANUFACTURER", "")) for r in rows if r.get("IP", "").strip()]
        unrecognized = sum(1 for c in classes if c is None)
        if unrecognized:
            status_lines.append(f"{unrecognized} row(s) have an unrecognized MANUFACTURER value and will be flagged, not processed.")
        axis_count = sum(1 for c in classes if c == "AXIS")
        hik_count = sum(1 for c in classes if c == "HIKVISION")
        mixed_count = sum(1 for c in classes if c == "MIXED")
        status_lines.append(f"Detected: {axis_count} Axis, {hik_count} Hikvision, {mixed_count} Mixed.")

        self.csv_status_label.configure(text="\n".join(status_lines))
        self._refresh_credential_requirements()

    def _append_log(self, text):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _gather_camera_rows(self):
        """Returns (camera_rows, base_filename) for whichever tab is active, or
        None if validation fails (an error dialog has already been shown)."""
        if self.tabview.get() == "Single Camera Test":
            ip = self.single_ip_entry.get().strip()
            if not ip:
                messagebox.showerror("Missing IP", "Enter a camera IP address before starting.")
                return None
            row = {
                "CLIENT_NM": self.single_client_entry.get().strip() or "Single Test",
                "LOCATION_NM": self.single_location_entry.get().strip() or "Diagnostic",
                "LIVE_UNIT_SERIAL_NM": self.single_serial_entry.get().strip() or "N/A",
                "IP": ip,
                "MANUFACTURER": self.single_mfg_var.get(),
            }
            base_filename = f"Diagnostic_Test_{ip.replace('.', '_')}"
            return [row], base_filename
        elif self.tabview.get() == "Fleet Picker":
            if not self.picker_basket:
                messagebox.showerror("Empty batch", "Add at least one unit to the batch before starting.")
                return None
            clients = sorted({r["CLIENT_NM"] for r in self.picker_basket if r.get("CLIENT_NM")})
            if len(clients) == 1:
                tag = clients[0]
            elif len(clients) > 1:
                tag = f"{clients[0]}_and_{len(clients) - 1}_more"
            else:
                tag = "Fleet_Picker"
            base_filename = f"FleetPicker_{tag}".replace(" ", "_").replace("/", "-")
            # Match the CSV path: collapse rows sharing an IP (a unit exposed on
            # one public IP across ports) into one device row before the engine
            # probes it, and let mixed-vendor units be flagged.
            return camera_engine.dedupe_camera_rows(list(self.picker_basket)), base_filename
        else:
            if not self.csv_rows:
                messagebox.showerror("No CSV loaded", "Browse for a CSV file before starting.")
                return None
            base_filename = self.csv_tag_entry.get().strip() or self.csv_path.stem
            return self.csv_rows, base_filename

    def _start_processing(self):
        if self.worker_thread and self.worker_thread.is_alive():
            return

        gathered = self._gather_camera_rows()
        if gathered is None:
            return
        camera_rows, base_filename = gathered

        needs_axis, needs_hik = self._current_mfg_needs()
        if needs_axis and not (self.axis_creds.username() and self.axis_creds.password()):
            messagebox.showerror("Missing Axis credentials", "This run needs Axis camera credentials -- fill in both fields.")
            return
        if needs_hik and not (self.hik_creds.username() and self.hik_creds.password()):
            messagebox.showerror("Missing Hikvision credentials", "This run needs Hikvision camera credentials -- fill in both fields.")
            return

        credentials = {
            "AXIS_USER": self.axis_creds.username() or None, "AXIS_PASS": self.axis_creds.password() or None,
            "HIK_USER": self.hik_creds.username() or None, "HIK_PASS": self.hik_creds.password() or None,
        }

        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")
        self.progress_bar.set(0)
        self.progress_label.configure(text="Starting...")
        self.start_button.configure(state="disabled", text="Processing...")
        self.open_folder_button.configure(state="disabled")

        self.worker_thread = threading.Thread(
            target=self._run_worker, args=(camera_rows, credentials, base_filename), daemon=True
        )
        self.worker_thread.start()

    def _run_worker(self, camera_rows, credentials, base_filename):
        try:
            output_dir = camera_engine.default_output_dir()
            path = camera_engine.run_batch(
                camera_rows, credentials, output_dir, base_filename,
                log_cb=lambda line: self.msg_queue.put(("log", line)),
                progress_cb=lambda done, total: self.msg_queue.put(("progress", done, total)),
            )
            self.msg_queue.put(("done", path))
        except Exception as e:
            self.msg_queue.put(("error", str(e)))

    def _poll_queue(self):
        try:
            while True:
                msg = self.msg_queue.get_nowait()
                self._handle_message(msg)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _handle_message(self, msg):
        kind = msg[0]
        if kind == "log":
            self._append_log(msg[1])
        elif kind == "progress":
            done, total = msg[1], msg[2]
            self.progress_bar.set(done / total if total else 0)
            self.progress_label.configure(text=f"{done} / {total} camera(s) processed")
        elif kind == "done":
            self._on_run_complete(msg[1])
        elif kind == "error":
            self._on_run_error(msg[1])
        elif kind == "picker_clients":
            self._on_clients_loaded(msg[1])
        elif kind == "picker_locations":
            self._on_locations_loaded(msg[1])
        elif kind == "picker_units":
            self._populate_units(msg[1])
        elif kind == "picker_added":
            self._on_cameras_resolved(msg[1])
        elif kind == "picker_error":
            self._on_picker_error(msg[1])

    def _on_clients_loaded(self, payload):
        source, clients = payload
        self.catalog_source = source
        self._clients_loaded = True
        self.picker_source_status.configure(
            text=f"{source.label} — {len(clients)} client(s).", text_color=LVT_DARK_TEAL)
        self._all_clients = clients
        if clients:
            self.picker_client_search.configure(state="normal")
            self._filter_clients()  # render the (unfiltered) initial list
            self._clear_unit_list("Pick a client and location to list units.")
        else:
            self.picker_client_search.configure(state="disabled")
            self._render_client_results([], "No clients found in this source.")
            self._clear_unit_list("No clients found in this source.")

    def _on_locations_loaded(self, locations):
        self._all_locations = locations
        if locations:
            self.picker_location_search.configure(state="normal")
            self._filter_locations()  # render the (unfiltered) initial list
            self._clear_unit_list("Pick a location to list units.")
        else:
            self.picker_location_search.configure(state="disabled")
            self._render_location_results([], "No locations for this client.")
            self._clear_unit_list("No locations for this client.")

    def _on_cameras_resolved(self, rows):
        self.picker_add_btn.configure(state="normal", text="Add checked units to batch  ↓")
        added_serials = {r["LIVE_UNIT_SERIAL_NM"] for r in rows if r.get("IP")}
        # Any checked serial that came back with zero cameras is worth flagging.
        requested = {s for s, var in self.unit_checkboxes.items() if var.get() and s not in self.basket_serials}
        empty = requested - added_serials
        for row in rows:
            if row.get("IP"):
                self.picker_basket.append(row)
        self.basket_serials |= added_serials
        # Repaint the unit list so newly-added units show their "already in batch" tag.
        if self.unit_checkboxes:
            self._populate_units(list(self.unit_checkboxes.keys()))
        self._refresh_basket_view()
        self._refresh_credential_requirements()
        status = f"Added {len(added_serials)} unit(s), {len(rows)} camera(s) to the batch."
        if empty:
            status += f"  ({len(empty)} unit(s) had no cameras in the source: {', '.join(sorted(empty))})"
        self.picker_source_status.configure(text=status, text_color=LVT_DARK_TEAL)

    def _on_picker_error(self, message):
        add_state = "normal" if self.unit_checkboxes else "disabled"
        self.picker_add_btn.configure(text="Add checked units to batch  ↓", state=add_state)
        self.picker_source_status.configure(text=message.split(chr(10))[0], text_color="#B00020")
        messagebox.showerror("Fleet Picker", message)

    def _on_run_complete(self, output_path):
        self.last_report_path = output_path
        self.start_button.configure(state="normal", text="Start Processing")
        self.open_folder_button.configure(state="normal")
        self.progress_label.configure(text=f"Done -- report saved as {output_path.name}")

    def _on_run_error(self, error_text):
        self.start_button.configure(state="normal", text="Start Processing")
        self.progress_label.configure(text="Failed -- see log for details")
        self._append_log(f"\n[!] FATAL ERROR: {error_text}")
        messagebox.showerror("Processing failed", f"An error stopped the run:\n\n{error_text}")

    def _open_report_folder(self):
        if not self.last_report_path:
            return
        folder = self.last_report_path.parent
        try:
            if platform.system() == "Windows":
                os.startfile(folder)
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception as e:
            messagebox.showerror("Could not open folder", str(e))


if __name__ == "__main__":
    app = App()
    app.mainloop()
