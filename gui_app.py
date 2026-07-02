"""LiveView Technologies Camera Analytics -- desktop GUI.

Run with: python gui_app.py
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
        self.geometry("950x780")
        self.minsize(820, 650)
        self.configure(fg_color=LVT_WHITE)

        self.msg_queue = queue.Queue()
        self.csv_rows = None
        self.csv_path = None
        self.worker_thread = None
        self.run_start_time = None

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
        self._build_single_tab(self.tab_single)
        self._build_csv_tab(self.tab_csv)

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
        self._refresh_credential_requirements()

    def _current_mfg_needs(self):
        """Returns (needs_axis, needs_hik) for whatever mode/rows are currently active."""
        if self.tabview.get() == "Single Camera Test":
            mfg_class = camera_engine.classify_manufacturer(self.single_mfg_var.get())
            return mfg_class == "AXIS", mfg_class == "HIKVISION"
        if self.csv_rows:
            classes = [camera_engine.classify_manufacturer(r.get("MANUFACTURER", "")) for r in self.csv_rows if r.get("IP", "").strip()]
            return "AXIS" in classes, "HIKVISION" in classes
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

        classes = [camera_engine.classify_manufacturer(r.get("MANUFACTURER", "")) for r in rows if r.get("IP", "").strip()]
        unrecognized = sum(1 for c in classes if c is None)
        if unrecognized:
            status_lines.append(f"{unrecognized} row(s) have an unrecognized MANUFACTURER value and will be flagged, not processed.")
        axis_count = sum(1 for c in classes if c == "AXIS")
        hik_count = sum(1 for c in classes if c == "HIKVISION")
        status_lines.append(f"Detected: {axis_count} Axis, {hik_count} Hikvision.")

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
