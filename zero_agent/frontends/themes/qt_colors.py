"""Qt color palettes + SVG icons + QSS templates for ZeroAgent desktop app."""

from PySide6.QtGui import QColor

# ═══════════════════════════════════════════════════════
# Dark Theme (current default)
# ═══════════════════════════════════════════════════════

C_DARK = {
    "bg":       QColor(14, 14, 18),
    "panel":    QColor(20, 20, 24, 248),
    "border":   QColor(45, 45, 50),
    "accent":   "#7c3aed",
    "text":     "#e4e4e7",
    "muted":    "#71717a",
    "user_g0":  QColor(99, 84, 234),
    "user_g1":  QColor(139, 92, 246),
    "asst_bg":  QColor(39, 39, 42, 210),
    "asst_bdr": QColor(63, 63, 70),
    "send_g0":  QColor(124, 58, 237),
    "send_g1":  QColor(139, 92, 246),
    "green":    "#22c55e",
    "hover_bg": "rgba(63,63,70,0.6)",
    "accent_bg":"rgba(124,58,237,0.25)",
    "accent_bdr":"rgba(124,58,237,0.5)",
    "svg_color": "#a78bfa",
}

# ═══════════════════════════════════════════════════════
# Light Theme (warm Anthropic-inspired)
# ═══════════════════════════════════════════════════════

C_LIGHT = {
    "bg":       QColor(250, 249, 246),
    "panel":    QColor(255, 255, 255, 248),
    "border":   QColor(213, 206, 197),
    "accent":   "#D4A27F",
    "text":     "#1A1714",
    "muted":    "#6B6560",
    "user_g0":  QColor(212, 162, 127),
    "user_g1":  QColor(196, 137, 95),
    "asst_bg":  QColor(244, 241, 235, 210),
    "asst_bdr": QColor(213, 206, 197),
    "send_g0":  QColor(220, 120, 92),
    "send_g1":  QColor(239, 140, 108),
    "green":    "#5A8A5E",
    "hover_bg": "rgba(213,206,197,0.4)",
    "accent_bg":"rgba(212,162,127,0.18)",
    "accent_bdr":"rgba(212,162,127,0.45)",
    "svg_color": "#CC785C",
}

# ═══════════════════════════════════════════════════════
# SVG Icons (parameterized by {c} color)
# ═══════════════════════════════════════════════════════

SVG_COPY = '<svg viewBox="0 0 24 24" fill="none" stroke="{c}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>'
SVG_REGEN = '<svg viewBox="0 0 24 24" fill="none" stroke="{c}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/></svg>'
SVG_CHAT = '<svg viewBox="0 0 24 24" fill="none" stroke="{c}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>'
SVG_CLOCK = '<svg viewBox="0 0 24 24" fill="none" stroke="{c}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>'
SVG_SEARCH = '<svg viewBox="0 0 24 24" fill="none" stroke="{c}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>'
SVG_BOOK = '<svg viewBox="0 0 24 24" fill="none" stroke="{c}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H20v20H6.5a2.5 2.5 0 0 1 0-5H20"/></svg>'
SVG_GEAR = '<svg viewBox="0 0 24 24" fill="none" stroke="{c}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/></svg>'
SVG_PLUS = '<svg viewBox="0 0 24 24" fill="none" stroke="{c}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>'
SVG_STOP = '<svg viewBox="0 0 24 24" fill="{c}" stroke="none"><rect width="10" height="10" x="7" y="7" rx="1.5" ry="1.5"/></svg>'
SVG_SAVE = '<svg viewBox="0 0 24 24" fill="none" stroke="{c}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>'
SVG_TRASH = '<svg viewBox="0 0 24 24" fill="none" stroke="{c}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/><line x1="10" x2="10" y1="11" y2="17"/><line x1="14" x2="14" y1="11" y2="17"/></svg>'
SVG_BOLT = '<svg viewBox="0 0 24 24" fill="none" stroke="{c}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>'
SVG_PLAY = '<svg viewBox="0 0 24 24" fill="{c}" stroke="none"><polygon points="6 3 20 12 6 21 6 3"/></svg>'
SVG_FILE = '<svg viewBox="0 0 24 24" fill="none" stroke="{c}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5L14.5 2z"/><polyline points="14 2 14 8 20 8"/><line x1="16" x2="8" y1="13" y2="13"/><line x1="16" x2="8" y1="17" y2="17"/><line x1="10" x2="8" y1="9" y2="9"/></svg>'
SVG_USER = '<svg viewBox="0 0 24 24" fill="none" stroke="{c}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>'
SVG_BOT = '<svg viewBox="0 0 24 24" fill="none" stroke="{c}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m12 3-1.912 5.813a2 2 0 0 1-1.275 1.275L3 12l5.813 1.912a2 2 0 0 1 1.275 1.275L12 21l1.912-5.813a2 2 0 0 1 1.275-1.275L21 12l-5.813-1.912a2 2 0 0 1-1.275-1.275L12 3Z"/><path d="M5 3v4"/><path d="M7 5H3"/></svg>'
SVG_SEND = '<svg viewBox="0 0 24 24" fill="none" stroke="{c}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2 11 13"/><path d="m22 2-7 20-4-9-9-4Z"/></svg>'
SVG_MOON = '<svg viewBox="0 0 24 24" fill="none" stroke="{c}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>'
SVG_SUN = '<svg viewBox="0 0 24 24" fill="none" stroke="{c}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>'

# Aliases
# Window control icons
SVG_MINIMIZE = '<svg viewBox="0 0 24 24" fill="none" stroke="{c}" stroke-width="2" stroke-linecap="round"><line x1="5" y1="12" x2="19" y2="12"/></svg>'
SVG_MAXIMIZE = '<svg viewBox="0 0 24 24" fill="none" stroke="{c}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="16" height="16" rx="2"/></svg>'
SVG_RESTORE  = '<svg viewBox="0 0 24 24" fill="none" stroke="{c}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="8" width="12" height="12" rx="2"/><rect x="8" y="4" width="12" height="12" rx="2"/></svg>'
SVG_CLOSE    = '<svg viewBox="0 0 24 24" fill="none" stroke="{c}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="6" y1="6" x2="18" y2="18"/><line x1="6" y1="18" x2="18" y2="6"/></svg>'

# Aliases
SVG_CLIP = SVG_PLUS
SVG_RESET = SVG_REGEN

# ═══════════════════════════════════════════════════════
# QSS Templates
# ═══════════════════════════════════════════════════════

SCROLLBAR_STYLE = """
QScrollBar:vertical { width: 5px; background: transparent; border: none; }
QScrollBar::handle:vertical {
    background: rgba(255,255,255,0.12); border-radius: 2px; min-height: 20px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
"""


def build_action_btn_style(c: dict) -> str:
    """Build action button QSS for a color palette."""
    return f"""
    QPushButton {{
        background: {c['hover_bg']}; border: none; border-radius: 4px;
        padding: 2px 4px; color: {c['text']}; font-size: 11px;
    }}
    QPushButton:hover {{ background: {c['accent_bg']}; }}
    """


def build_tab_button_style(c: dict) -> str:
    """Build tab button QSS for a color palette."""
    return f"""
    QPushButton {{
        background: transparent; border: none; color: {c['muted']};
        padding: 6px 0; font-size: 13px; font-weight: 500;
        border-bottom: 2px solid transparent;
    }}
    QPushButton:hover {{ color: {c['text']}; }}
    QPushButton[active="true"] {{
        color: {c['accent']}; border-bottom-color: {c['accent']};
    }}
    """


def build_model_row_style(c: dict) -> str:
    """Build model row QSS for a color palette."""
    return f"""
    QFrame#modelRow {{
        background: {c['hover_bg']}; border: 1px solid {c['accent_bdr']};
        border-radius: 6px; padding: 6px 8px;
    }}
    QFrame#modelRow:hover {{ background: {c['accent_bg']}; }}
    """


def build_send_btn_style(c: dict) -> str:
    """Build send button QSS for a color palette."""
    g0 = c['send_g0'].name() if isinstance(c['send_g0'], QColor) else c['send_g0']
    g1 = c['send_g1'].name() if isinstance(c['send_g1'], QColor) else c['send_g1']
    return f"""
    QPushButton {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {g0}, stop:1 {g1});
        border: none; border-radius: 10px; color: white; font-weight: 600;
        padding: 8px 16px; font-size: 13px;
    }}
    QPushButton:hover {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {g1}, stop:1 {g0});
    }}
    QPushButton:disabled {{ background: {c['hover_bg']}; color: {c['muted']}; }}
    """


def build_stop_btn_style(c: dict) -> str:
    """Build stop button QSS for a color palette."""
    return f"""
    QPushButton {{
        background: {c['hover_bg']}; border: 1px solid {c['border']};
        border-radius: 10px; color: {c['text']}; padding: 8px 16px; font-size: 13px;
    }}
    QPushButton:hover {{ background: rgba(220,38,38,0.3); border-color: rgba(220,38,38,0.6); }}
    """


def build_titlebar_btn_style(c: dict, danger: bool = False) -> str:
    """Build titlebar window-control button QSS for a color palette."""
    hover_bg = "rgba(220,38,38,0.85)" if danger else c["hover_bg"]
    return f"""
    QPushButton {{
        background: {c['hover_bg']}; color: {c['muted']};
        border: none; border-radius: 13px;
    }}
    QPushButton:hover {{ background: {hover_bg}; color: white; }}
    """
