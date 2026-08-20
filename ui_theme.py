"""Shared look-and-feel for the two desktop GUIs (audit_gui.py, analytics_writer_gui.py):
the LiveView brand palette, light/dark appearance handling, and the header toggle both
tools drop in.

Every palette entry is a ``(light, dark)`` pair. CustomTkinter resolves a pair against
the current appearance mode on its own, so any widget built with these constants
follows the theme with no extra code -- which is why the two GUIs could gain dark mode
without touching their ~110 individual widget definitions.

Two exceptions to that rule, both deliberate:

  * ``LVT_WHITE`` / ``LVT_ON_TEAL`` are single colors, not pairs. They mark text and
    glyphs that sit ON a teal button or the teal header bar, which stays teal in both
    modes -- flipping them would put dark text on a dark-teal button.
  * RAW tkinter widgets (tk.Canvas, tk.PanedWindow) reject a pair. Pass those through
    ``resolve()`` and re-apply them from an ``on_appearance_change`` callback.
"""

import json
from pathlib import Path

import customtkinter as ctk

# --- LiveView Technologies brand palette, as (light mode, dark mode) pairs ---------
LVT_LIGHT = ("#E5F5F5", "#1E2A31")            # section / card background
LVT_TEAL = ("#00A19A", "#00B0A8")             # secondary buttons, accents
LVT_TEAL_HOVER = ("#008680", "#00958E")
LVT_DARK_TEAL = ("#00726E", "#00807A")        # header bar, primary buttons
LVT_DARK_TEAL_HOVER = ("#005B58", "#006E69")
LVT_TEXT_DARK = ("#1A1D27", "#E6EDF0")        # primary body text
LVT_TEXT_MUTED = ("#6B7A79", "#93A9A7")       # hints, secondary labels
LVT_SURFACE = ("#FFFFFF", "#12181D")          # window + panel background
LVT_LOG_BG = ("#0F1117", "#0A0D12")           # log pane (dark in both modes)
LVT_LOG_TEXT = ("#D6EFEF", "#D6EFEF")
LVT_ERROR = ("#B00020", "#FF7A85")            # failure text

# Single colors: these sit on teal chrome, which does not flip between modes.
LVT_WHITE = "#FFFFFF"
LVT_ON_TEAL = "#E5F5F5"

APPEARANCE_MODES = ("Light", "Dark", "System")
DEFAULT_APPEARANCE = "Light"

# --- Audit report layout (audit_gui.py) -------------------------------------------
# Not look-and-feel, but it is an operator preference that has to survive a relaunch,
# and this module already owns the prefs file -- so it lives here rather than opening
# a second store. The strings double as the segmented button's labels.
LAYOUT_TABS = "Tab per location"
LAYOUT_SINGLE = "One sheet"
REPORT_LAYOUTS = (LAYOUT_TABS, LAYOUT_SINGLE)
DEFAULT_REPORT_LAYOUT = LAYOUT_TABS

# Remembers the operator's Light/Dark choice between launches. Machine-local
# convenience only -- no fleet data in it, but there's no reason to commit it either.
UI_PREFS = Path(__file__).with_name("ui_prefs.json")


def resolve(color):
    """Pick the current mode's half of a ``(light, dark)`` pair.

    For raw tkinter widgets only -- CTk widgets take the pair itself. Single colors
    pass straight through, so this is always safe to wrap around a palette constant.
    """
    if isinstance(color, (tuple, list)):
        return color[1] if ctk.get_appearance_mode().lower() == "dark" else color[0]
    return color


def load_prefs():
    """Every stored UI preference as a dict, or an empty one when nothing is saved
    yet. Best-effort: a missing or corrupt prefs file must never stop the GUI
    opening, so a bad read reads as "no preferences" rather than raising."""
    try:
        prefs = json.loads(UI_PREFS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return prefs if isinstance(prefs, dict) else {}


def save_pref(key, value):
    """Persist one preference, leaving the others alone. Read-modify-write rather
    than a plain overwrite: the file holds several unrelated keys now, and writing
    just the one being changed would silently drop the rest."""
    prefs = load_prefs()
    prefs[key] = value
    try:
        UI_PREFS.write_text(json.dumps(prefs, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        pass


def load_appearance():
    """The saved appearance choice, or the default when nothing is stored yet."""
    saved = load_prefs().get("appearance")
    return saved if saved in APPEARANCE_MODES else DEFAULT_APPEARANCE


def save_appearance(mode):
    """Persist the appearance choice for the next launch (best-effort)."""
    save_pref("appearance", mode)


def load_report_layout():
    """The saved audit-report layout, or the default when nothing is stored yet.
    Anything unrecognized falls back to the default rather than being trusted --
    a hand-edited prefs file must not be able to pick a layout that doesn't exist."""
    saved = load_prefs().get("report_layout")
    return saved if saved in REPORT_LAYOUTS else DEFAULT_REPORT_LAYOUT


def save_report_layout(layout):
    """Persist the audit-report layout choice for the next launch (best-effort)."""
    save_pref("report_layout", layout)


def init_appearance():
    """Apply the saved appearance at startup. Returns the mode that was applied, so
    the toggle can show it. Call this BEFORE building any widgets."""
    mode = load_appearance()
    ctk.set_appearance_mode(mode)
    return mode


def on_appearance_change(callback, widget):
    """Run ``callback(mode_string)`` whenever the appearance mode changes -- including
    an OS-level light/dark switch while "System" is selected, which no toggle press
    would tell us about.

    Only raw tkinter widgets need this; CTk widgets are already on this same tracker.
    The tracker is CustomTkinter internals, so a version that moves it degrades to
    "themed on the next toggle press" rather than failing to start."""
    try:
        ctk.AppearanceModeTracker.add(callback, widget)
    except Exception:
        pass


class AppearanceToggle(ctk.CTkFrame):
    """Light / Dark / System switch for a GUI header bar.

    Sits on the teal header, so its own colors are the fixed on-teal ones rather than
    palette pairs. Saves the choice on every press; ``on_change`` (optional) fires
    after the mode is applied for anything that has to be recolored by hand.
    """

    def __init__(self, master, initial, on_change=None, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self._on_change = on_change
        self.var = ctk.StringVar(value=initial if initial in APPEARANCE_MODES else DEFAULT_APPEARANCE)
        ctk.CTkLabel(self, text="Appearance", text_color=LVT_ON_TEAL,
                     font=ctk.CTkFont(size=10)).pack(anchor="e")
        ctk.CTkSegmentedButton(
            self, values=list(APPEARANCE_MODES), variable=self.var, command=self._apply,
            height=24, font=ctk.CTkFont(size=11),
            selected_color="#00514E", selected_hover_color="#003F3D",
            unselected_color="#00918B", unselected_hover_color="#00A19A",
            text_color=LVT_WHITE, fg_color="#005B58",
        ).pack(anchor="e")

    def _apply(self, mode):
        ctk.set_appearance_mode(mode)
        save_appearance(mode)
        if self._on_change:
            self._on_change(mode)
