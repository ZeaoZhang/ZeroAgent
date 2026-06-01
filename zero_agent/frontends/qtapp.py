"""
桌面前端单文件版 – PySide6 聊天面板 + 悬浮按钮   thanks to GaoZhiCheng
依赖: pip install PySide6
可选: pip install markdown  (Markdown 渲染)
用法: python frontends/qtapp.py 
"""
from __future__ import annotations

import math, os, sys, json, glob, re, base64, time, threading
import queue as _queue
from datetime import datetime
from typing import Optional

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QFrame, QTextEdit, QStackedWidget,
    QListWidget, QListWidgetItem, QSizePolicy, QFileDialog,
    QSplitter, QTextBrowser, QApplication, QMessageBox,
    QMenu, QLineEdit,
)
from PySide6.QtCore import (
    Qt, QTimer, QPoint, QPointF, QByteArray, QSize,
    Signal, QMetaObject, Q_ARG, QObject, QDateTime, QEvent,
)
from PySide6.QtGui import (
    QPainter, QColor, QLinearGradient, QRadialGradient,
    QPen, QPainterPath, QCursor, QFont, QIcon, QPixmap, QRegion,
)

from zero_agent.frontends.themes.qt_colors import (
    C_DARK, C_LIGHT,
    SVG_COPY, SVG_REGEN, SVG_CHAT, SVG_CLOCK, SVG_SEARCH, SVG_BOOK, SVG_GEAR,
    SVG_PLUS, SVG_STOP, SVG_SAVE, SVG_TRASH, SVG_BOLT, SVG_PLAY, SVG_FILE,
    SVG_USER, SVG_BOT, SVG_SEND, SVG_CLIP, SVG_RESET,
    SVG_MOON, SVG_SUN,
    SVG_MINIMIZE, SVG_MAXIMIZE, SVG_RESTORE, SVG_CLOSE,
    SCROLLBAR_STYLE,
    build_action_btn_style, build_tab_button_style, build_model_row_style,
    build_send_btn_style, build_stop_btn_style, build_titlebar_btn_style,
)

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from zero_agent.core.agent import ZeroAgent
from zero_agent.core.config import AgentConfig
from zero_agent.adapters.agent_runner import AgentRunner
from zero_agent.bots.common import FILE_HINT, HELP_TEXT, clean_reply, build_done_text, format_restore


# ══════════════════════════════════════════════════════════════════════
# FloatingButton
# ══════════════════════════════════════════════════════════════════════

class FloatingButton(QWidget):
    SIZE = 60       # circle diameter
    MARGIN = 14     # extra space for glow
    TOTAL = SIZE + MARGIN * 2

    def __init__(self, chat_panel: QWidget):
        super().__init__()
        self.chat_panel = chat_panel
        self._drag_origin_global: QPoint | None = None
        self._drag_origin_win: QPoint | None = None
        self._dragged = False
        self._glow = 0.5
        self._glow_dir = 1
        self._hovering = False
        self._hover_clock = 0.0
        self._hover_strength = 0.0
        self._flow_phase = 0.0
        self._running = False
        self._last_toggle_ms = 0  # debounce timestamp

        # Window flags: frameless, always on top, no taskbar entry
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(self.TOTAL, self.TOTAL)
        self.setCursor(QCursor(Qt.PointingHandCursor))

        # Smooth animation (~30 fps)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(33)

        # Default position: bottom-right of the work area
        scr = QApplication.primaryScreen().availableGeometry()
        self.move(scr.right() - self.TOTAL - 20, scr.bottom() - self.TOTAL - 20)

    # ── Animation ────────────────────────────────────────
    def _tick(self):
        # running status: green when model is actively responding
        self._running = bool(
            getattr(self.chat_panel, "_is_streaming", False)
            or getattr(getattr(self.chat_panel, "agent", None), "is_running", False)
        )

        self._glow += self._glow_dir * 0.04
        if self._glow >= 1.0:
            self._glow, self._glow_dir = 1.0, -1
        elif self._glow <= 0.0:
            self._glow, self._glow_dir = 0.0, 1

        target = 1.0 if self._hovering else 0.0
        self._hover_strength += (target - self._hover_strength) * 0.20
        self._hover_clock += 0.033
        self._flow_phase += 0.16 + (0.06 if self._running else 0.0) + (0.05 if self._hovering else 0.0)
        self.update()

    # ── Painting ──────────────────────────────────────────
    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        m = self.MARGIN
        r = self.SIZE // 2
        cx = m + r
        # Rhythmic spring bounce: one main hop + one lighter rebound per beat.
        beat_t = self._hover_clock % 1.18
        spring = 0.0
        if beat_t < 0.70:
            spring += max(0.0, math.exp(-5.2 * beat_t) * math.sin(15.5 * beat_t))
        if beat_t > 0.20:
            rt = beat_t - 0.20
            spring += 0.52 * max(0.0, math.exp(-7.0 * rt) * math.sin(21.0 * rt))
        idle_sway = 0.20 * math.sin(self._hover_clock * 2.1)
        bounce = int(round((spring * 7.2 + idle_sway) * self._hover_strength))
        cy = m + r - bounce

        if self._running:
            # running: #2DFFF5 -> #FFF878
            g0 = QColor(45, 255, 245, 195)
            g1 = QColor(255, 248, 120, 195)
            glow_rgb = (96, 255, 216)
        else:
            # idle: #103CE7 -> #64E9FF
            g0 = QColor(16, 60, 231, 195)
            g1 = QColor(100, 233, 255, 195)
            glow_rgb = (74, 170, 255)

        # --- Outer glow rings (3 layers) ---
        base_alpha = int(45 + 25 * self._glow)
        for i, gr in enumerate([r + 10, r + 6, r + 2]):
            g = QRadialGradient(QPointF(cx, cy), gr)
            g.setColorAt(0.0, QColor(glow_rgb[0], glow_rgb[1], glow_rgb[2], max(0, base_alpha - i * 14)))
            g.setColorAt(1.0, QColor(glow_rgb[0], glow_rgb[1], glow_rgb[2], 0))
            p.setBrush(g)
            p.setPen(Qt.NoPen)
            p.drawEllipse(int(cx - gr), int(cy - gr), int(gr * 2), int(gr * 2))

        # --- Frosted glass disc behind main circle ---
        frost = QRadialGradient(QPointF(cx, cy), r)
        frost.setColorAt(0.0, QColor(30, 30, 45, 140))
        frost.setColorAt(0.85, QColor(20, 20, 32, 160))
        frost.setColorAt(1.0, QColor(14, 14, 20, 100))
        p.setBrush(frost)
        p.setPen(Qt.NoPen)
        p.drawEllipse(cx - r, cy - r, r * 2, r * 2)

        # --- Main circle (flowing state gradient) ---
        spin = self._flow_phase
        dx = math.cos(spin) * r
        dy = math.sin(spin) * r
        grad = QLinearGradient(cx - dx, cy - dy, cx + dx, cy + dy)
        grad.setColorAt(0.0, g0)
        grad.setColorAt(1.0, g1)
        p.setBrush(grad)
        p.setPen(QPen(QColor(255, 255, 255, 50), 1.5))
        p.drawEllipse(cx - r, cy - r, r * 2, r * 2)

        # --- Flowing glass streaks ---
        clip = QPainterPath()
        clip.addEllipse(float(cx - r), float(cy - r), float(r * 2), float(r * 2))
        p.setClipPath(clip)

        flow_shift = math.sin(self._flow_phase * 0.85) * (r * 0.7)
        streak1 = QLinearGradient(cx - r + flow_shift, cy - r, cx + r + flow_shift, cy + r)
        streak1.setColorAt(0.00, QColor(255, 255, 255, 0))
        streak1.setColorAt(0.45, QColor(255, 255, 255, 42))
        streak1.setColorAt(0.52, QColor(255, 255, 255, 78))
        streak1.setColorAt(0.60, QColor(255, 255, 255, 24))
        streak1.setColorAt(1.00, QColor(255, 255, 255, 0))
        p.setBrush(streak1)
        p.setPen(Qt.NoPen)
        p.drawEllipse(cx - r, cy - r, r * 2, r * 2)

        flow_shift_2 = math.cos(self._flow_phase * 1.2) * (r * 0.5)
        streak2 = QLinearGradient(cx - r, cy + flow_shift_2, cx + r, cy - flow_shift_2)
        streak2.setColorAt(0.00, QColor(255, 255, 255, 0))
        streak2.setColorAt(0.35, QColor(255, 255, 255, 16))
        streak2.setColorAt(0.50, QColor(255, 255, 255, 46))
        streak2.setColorAt(0.65, QColor(255, 255, 255, 16))
        streak2.setColorAt(1.00, QColor(255, 255, 255, 0))
        p.setBrush(streak2)
        p.drawEllipse(cx - r, cy - r, r * 2, r * 2)

        # --- Top highlight ---
        hl = QLinearGradient(cx, cy - r, cx, cy)
        hl.setColorAt(0.0, QColor(255, 255, 255, 72))
        hl.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setBrush(hl)
        p.drawRect(cx - r, cy - r, r * 2, r)
        p.setClipping(False)

        # --- Bot icon ---
        p.setPen(QPen(QColor(255, 255, 255, 220), 1.8))
        p.setBrush(Qt.NoBrush)
        # Head
        p.drawRoundedRect(cx - 9, cy - 6, 18, 12, 2, 2)
        # Eyes
        p.setBrush(QColor(255, 255, 255, 220))
        p.setPen(Qt.NoPen)
        p.drawEllipse(cx - 6, cy - 3, 4, 4)
        p.drawEllipse(cx + 2, cy - 3, 4, 4)
        # Antenna stem
        p.setPen(QPen(QColor(255, 255, 255, 220), 1.8))
        p.drawLine(cx, cy - 6, cx, cy - 10)
        # Antenna tip
        p.setBrush(QColor(255, 255, 255, 190))
        p.setPen(Qt.NoPen)
        p.drawEllipse(cx - 2, cy - 13, 4, 4)

    def enterEvent(self, event):
        self._hovering = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovering = False
        self.update()
        super().leaveEvent(event)

    # ── Mouse events (drag + click) ───────────────────────
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_origin_global = event.globalPosition().toPoint()
            self._drag_origin_win = self.pos()
            self._dragged = False

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.LeftButton and self._drag_origin_global:
            delta = event.globalPosition().toPoint() - self._drag_origin_global
            if abs(delta.x()) > 5 or abs(delta.y()) > 5:
                self._dragged = True
            if self._dragged:
                new = self._drag_origin_win + delta
                scr = QApplication.primaryScreen().availableGeometry()
                new.setX(max(scr.left(), min(new.x(), scr.right() - self.width())))
                new.setY(max(scr.top(), min(new.y(), scr.bottom() - self.height())))
                self.move(new)

    def mouseDoubleClickEvent(self, event):
        # Qt sends Press→Release→DoubleClick→Release on double-click.
        # The first Release already toggled the panel; swallow the DoubleClick
        # so the second Release does NOT trigger a second toggle.
        self._dragged = True   # mark as "dragged" → Release will be ignored
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            if not self._dragged:
                self._toggle()
            self._dragged = False
        self._drag_origin_global = None

    # ── Toggle panel ──────────────────────────────────────
    def _toggle(self):
        now = QDateTime.currentMSecsSinceEpoch()
        if now - self._last_toggle_ms < 500:   # 500 ms debounce
            return
        self._last_toggle_ms = now

        if self.chat_panel.isVisible():
            self.chat_panel.hide()
        else:
            self._position_panel()
            self.chat_panel.show()
            self.chat_panel.raise_()
            self.chat_panel.activateWindow()

    def _position_panel(self):
        scr = QApplication.primaryScreen().availableGeometry()
        btn = self.geometry()
        pw = self.chat_panel.width()
        ph = self.chat_panel.height()
        # Prefer left of button, bottom-aligned
        x = btn.left() - pw - 12
        y = btn.bottom() - ph
        x = max(scr.left() + 10, min(x, scr.right() - pw - 10))
        y = max(scr.top() + 10, min(y, scr.bottom() - ph - 10))
        self.chat_panel.move(x, y)


# ══════════════════════════════════════════════════════════════════════
# ChatPanel
# ══════════════════════════════════════════════════════════════════════

# ── constants ─────────────────────────────────────────────────────────────────
HISTORY_FILE = "memory/chat_history.json"
TEXT_FILE_EXTS = {
    ".txt", ".md", ".py", ".json", ".csv", ".yaml", ".yml",
    ".log", ".ini", ".toml", ".xml", ".html", ".js", ".ts", ".sql",
}
MAX_INLINE_CHARS = 6000
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB
AUTO_IDLE_THRESHOLD = 1800  # seconds before autonomous trigger
AUTO_COOLDOWN = 120         # seconds between triggers

C = C_DARK  # active palette, reassigned by set_theme()

_SVG_COPY = SVG_COPY
_SVG_REGEN = SVG_REGEN
_SVG_CHAT = SVG_CHAT
_SVG_CLOCK = SVG_CLOCK
_SVG_SEARCH = SVG_SEARCH
_SVG_BOOK = SVG_BOOK
_SVG_GEAR = SVG_GEAR
_SVG_PLUS = SVG_PLUS
_SVG_CLIP = SVG_CLIP
_SVG_STOP = SVG_STOP
_SVG_RESET = SVG_RESET
_SVG_SAVE = SVG_SAVE
_SVG_TRASH = SVG_TRASH
_SVG_BOLT = SVG_BOLT
_SVG_PLAY = SVG_PLAY
_SVG_FILE = SVG_FILE
_SVG_USER = SVG_USER
_SVG_BOT = SVG_BOT
_SVG_SEND = SVG_SEND

_SCROLLBAR_STYLE = SCROLLBAR_STYLE

_MD_CSS = """
body { color: #e4e4e7; font-family: "Arial", "Microsoft YaHei", sans-serif; font-size: 13px; line-height: 1.6; font-weight: 400; }
h1 { color: #f4f4f5; font-size: 20px; font-weight: 700; border-bottom: 1px solid #3f3f46; padding-bottom: 4px; margin-top: 16px; }
h2 { color: #f4f4f5; font-size: 17px; font-weight: 700; border-bottom: 1px solid #3f3f46; padding-bottom: 3px; margin-top: 14px; }
h3 { color: #f4f4f5; font-size: 15px; font-weight: 600; margin-top: 12px; }
h4,h5,h6 { color: #d4d4d8; font-size: 13px; font-weight: 600; margin-top: 10px; }
code { background: rgba(63,63,70,0.6); color: #c4b5fd; padding: 1px 4px; border-radius: 3px;
       font-family: Consolas, "Courier New", monospace; font-size: 12px; }
pre  { background: rgba(24,24,30,0.95); border: 1px solid #3f3f46; border-radius: 6px;
       padding: 10px 12px; margin: 8px 0; }
pre code { background: transparent; padding: 0; color: #d4d4d8; }
a { color: #818cf8; text-decoration: none; }
a:hover { text-decoration: underline; }
blockquote { border-left: 3px solid #7c3aed; margin: 8px 0 8px 0; padding: 4px 0 4px 12px; color: #a1a1aa; }
table { border-collapse: collapse; margin: 8px 0; }
th, td { border: 1px solid #3f3f46; padding: 5px 10px; }
th { background: rgba(63,63,70,0.35); color: #d4d4d8; font-weight: 700; }
hr { border: none; border-top: 1px solid #3f3f46; margin: 12px 0; }
ul, ol { padding-left: 22px; margin: 4px 0; }
li { margin: 2px 0; }
p { margin: 6px 0; }
"""


def _md_to_html(text: str) -> str:
    try:
        import markdown
        return markdown.markdown(
            text, extensions=["fenced_code", "tables", "nl2br", "sane_lists"]
        )
    except ImportError:
        pass
    html, in_code, in_ul = [], False, False
    for raw in text.split("\n"):
        if raw.strip().startswith("```"):
            if in_code:
                html.append("</code></pre>")
            else:
                html.append("<pre><code>")
            in_code = not in_code
            continue
        if in_code:
            html.append(raw.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
            continue
        line = raw
        line = re.sub(r"`([^`]+)`", r"<code>\1</code>", line)
        line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
        line = re.sub(r"\*(.+?)\*", r"<i>\1</i>", line)
        line = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', line)
        if re.match(r"^#{1,6}\s", line):
            lvl = len(line.split()[0])
            line = f"<h{lvl}>{line[lvl:].strip()}</h{lvl}>"
        elif re.match(r"^-{3,}$|^_{3,}$|^\*{3,}$", line.strip()):
            line = "<hr>"
        elif re.match(r"^\s*[-*+]\s", line):
            content = re.sub(r"^\s*[-*+]\s", "", line)
            if not in_ul:
                html.append("<ul>")
                in_ul = True
            line = f"<li>{content}</li>"
        else:
            if in_ul:
                html.append("</ul>")
                in_ul = False
            line = f"<p>{line}</p>" if line.strip() else ""
        html.append(line)
    if in_code:
        html.append("</code></pre>")
    if in_ul:
        html.append("</ul>")
    return "\n".join(html)


_icon_cache: dict[str, QIcon] = {}

def _svg_icon(key: str, svg_template: str, color: str = "#a1a1aa",
              size: int = 16) -> QIcon:
    cache_key = f"{key}_{color}_{size}"
    if cache_key not in _icon_cache:
        try:
            from PySide6.QtSvg import QSvgRenderer
        except ImportError:
            return QIcon()
        data = QByteArray(svg_template.format(c=color).encode("utf-8"))
        renderer = QSvgRenderer(data)
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        renderer.render(painter)
        painter.end()
        _icon_cache[cache_key] = QIcon(pixmap)
    return _icon_cache[cache_key]


# ── utilities ─────────────────────────────────────────────────────────────────
def _make_session_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def _load_history() -> list:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []


def _save_history(history: list):
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def _build_prompt_with_uploads(prompt: str, files: list) -> tuple:
    """
    files: list of {'name': str, 'type': str, 'raw': bytes}
    returns (full_prompt, display_prompt, display_attachments)
    """
    if not files:
        return prompt, prompt, []

    os.makedirs("temp/uploaded", exist_ok=True)
    attachment_chunks = ["\n\n[用户上传附件 — 文件已保存到本地磁盘，可用 file_read 工具读取]"]
    display_attachments = []
    img_count, file_names = 0, []

    for f in files:
        raw, name, mime = f["raw"], f["name"], f.get("type", "")
        size = len(raw)
        ext = os.path.splitext(name)[1].lower()
        safe = re.sub(r"[^A-Za-z0-9._\-]", "_", name)
        saved = os.path.join(
            "temp", "uploaded",
            f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{safe}",
        )
        try:
            with open(saved, "wb") as out:
                out.write(raw)
        except Exception:
            saved = "(保存失败)"

        if mime.startswith("image/"):
            b64 = base64.b64encode(raw).decode()
            attachment_chunks.append(
                f"\n- [图片附件] {name} ({size} bytes)\n  磁盘路径: {saved}"
                f"\n  data:{mime};base64,{b64}"
            )
            display_attachments.append({"type": "image", "name": name})
            img_count += 1
        elif ext in TEXT_FILE_EXTS:
            text = raw.decode("utf-8", errors="replace")
            attachment_chunks.append(
                f"\n--- 文本文件: {name} ({size} bytes) ---\n磁盘路径: {saved}\n{text[:MAX_INLINE_CHARS]}"
                + ("\n[内容已截断，请用 file_read 读取完整内容]" if len(text) > MAX_INLINE_CHARS else "")
            )
            display_attachments.append({"type": "file", "name": name})
            file_names.append(name)
        else:
            attachment_chunks.append(
                f"\n- 文件: {name} ({size} bytes)\n  磁盘路径: {saved}"
            )
            display_attachments.append({"type": "file", "name": name})
            file_names.append(name)

    parts = []
    if img_count:
        parts.append(f"{img_count} 张图片")
    if file_names:
        parts.append(f"{len(file_names)} 个文件（{'、'.join(file_names)}）")
    display_prompt = f"{prompt}\n\n📎 已附带：{'，'.join(parts)}" if parts else prompt
    return prompt + "\n".join(attachment_chunks), display_prompt, display_attachments


# ── small reusable widgets ────────────────────────────────────────────────────
class _Separator(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(1)
        self.apply_theme()

    def apply_theme(self):
        self.setStyleSheet(f"background: {C['border'].name()};")


class _Badge(QLabel):
    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.apply_theme()

    def apply_theme(self):
        self.setStyleSheet(
            f"QLabel {{ background: {C['hover_bg']}; color: {C['muted']};"
            f" border: 1px solid {C['border']}; border-radius: 9px;"
            f" padding: 1px 8px; font-size: 11px; }}"
        )


class _StreamingBadge(QLabel):
    def __init__(self, parent=None):
        super().__init__("处理中…", parent)
        self.apply_theme()
        self.hide()

    def apply_theme(self):
        self.setStyleSheet(
            f"QLabel {{ background: {C['accent_bg']}; color: {C['svg_color']};"
            f" border: 1px solid {C['accent_bdr']}; border-radius: 9px;"
            f" padding: 1px 8px; font-size: 11px; }}"
        )


class _FoldableTextBrowser(QTextBrowser):
    """QTextBrowser subclass that reliably detects clicks on fold anchors."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.viewport().installEventFilter(self)

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent
        if obj is self.viewport() and event.type() == QEvent.MouseButtonRelease:
            href = self.anchorAt(event.pos())
            if href and href.startswith("#fold_"):
                from urllib.parse import unquote
                title = unquote(href[6:])
                p = self.parent()
                while p and not isinstance(p, _MsgRow):
                    p = p.parent()
                if p and hasattr(p, '_toggle_fold'):
                    p._toggle_fold(title)
                    return True
        return super().eventFilter(obj, event)


class _MsgRow(QWidget):
    """A single message row – flat layout with avatar, inspired by ChatGPT / Qwen."""

    @staticmethod
    def _action_btn_style() -> str:
        return f"""
        QPushButton {{
            background: transparent; border: none; border-radius: 4px; padding: 3px;
        }}
        QPushButton:hover {{ background: {C['hover_bg']}; }}
        """

    def _apply_avatar_style(self):
        is_dark = C is C_DARK
        if is_dark:
            self._avatar.setStyleSheet(
                "QLabel { background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.10);"
                " border-radius: 15px; }"
            )
        else:
            self._avatar.setStyleSheet(
                f"QLabel {{ background: rgba(0,0,0,0.04); border: 1px solid {C['border'].name()};"
                f" border-radius: 15px; }}"
            )

    def _apply_role_label_style(self):
        self._role_lbl.setStyleSheet(
            f"color: {C['text']}; font-size: 12px; font-weight: 700; background: transparent;"
        )

    def _apply_bubble_style(self):
        self._bubble.setStyleSheet(
            f"background: {C['hover_bg']}; border-radius: 12px;"
        )

    def _apply_msg_label_style(self):
        self._msg_label.setStyleSheet(
            f"QLabel {{ background: transparent; color: {C['text']};"
            f" padding: 0; font-size: 14px; line-height: 1.6; }}"
        )

    def apply_theme(self):
        """Reapply styles after theme change."""
        if hasattr(self, '_avatar'):
            self._apply_avatar_style()
        if hasattr(self, '_role_lbl'):
            self._apply_role_label_style()
        if hasattr(self, '_bubble'):
            self._apply_bubble_style()
        if hasattr(self, '_msg_label'):
            self._apply_msg_label_style()
        # Update action buttons
        if self._action_row:
            for btn in self._action_row.findChildren(QPushButton):
                btn.setStyleSheet(self._action_btn_style())
    def __init__(self, text: str, role: str, parent=None, on_resend=None, on_delete=None, on_rewrite=None, created_at: str = None):
        super().__init__(parent)
        self._text = text
        self._role = role
        self._on_resend = on_resend
        self._on_delete = on_delete
        self._on_rewrite = on_rewrite
        self._created_at = created_at
        self._action_row = None
        self._finished = True

        is_user = role == "user"
        self.setStyleSheet("background: transparent;")
        self._is_user = is_user

        outer = QHBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(10)
        outer.setAlignment(Qt.AlignTop)

        # ── avatar ──
        avatar = QLabel()
        avatar.setFixedSize(30, 30)
        avatar.setAlignment(Qt.AlignCenter)
        svg_data = _SVG_USER if is_user else _SVG_BOT
        avatar_color = "#c8c8d0" if is_user else "#9eb4d0"
        pm = QPixmap(30, 30)
        pm.fill(QColor(0, 0, 0, 0))
        from PySide6.QtSvg import QSvgRenderer
        renderer = QSvgRenderer(QByteArray(svg_data.replace("{c}", avatar_color).encode()))
        p = QPainter(pm)
        renderer.render(p)
        p.end()
        avatar.setPixmap(pm)
        self._avatar = avatar
        self._apply_avatar_style()

        # ── content column ──
        content_col = QVBoxLayout()
        content_col.setContentsMargins(0, 0, 0, 0)
        content_col.setSpacing(2)

        role_lbl = QLabel("你" if is_user else "助手")
        self._role_lbl = role_lbl
        self._apply_role_label_style()
        if is_user:
            role_lbl.setAlignment(Qt.AlignRight)
        content_col.addWidget(role_lbl)

        if is_user:
            # ── user: right-aligned bubble ──
            bubble = QWidget()
            self._bubble = bubble
            self._apply_bubble_style()
            bubble_ly = QVBoxLayout(bubble)
            bubble_ly.setContentsMargins(12, 8, 12, 8)
            bubble_ly.setSpacing(0)

            label = QLabel(text)
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
            self._msg_label = label
            self._apply_msg_label_style()
            bubble_ly.addWidget(label)
            self._label = label

            # Size bubble to text: measure longest line, cap at 420
            fm = label.fontMetrics()
            text_w = max((fm.horizontalAdvance(ln) for ln in text.split('\n')), default=0)
            bubble.setMinimumWidth(min(text_w + 24, 420))
            bubble.setMaximumWidth(420)
            content_col.addWidget(bubble, 0, Qt.AlignRight)

            # ── user message action row ──
            self._action_row = QWidget()
            self._action_row.setStyleSheet("background: transparent;")
            alayout = QHBoxLayout(self._action_row)
            alayout.setContentsMargins(0, 4, 0, 0)
            alayout.setSpacing(4)
            alayout.setAlignment(Qt.AlignRight)

            icon_sz = QSize(15, 15)

            copy_btn = QPushButton()
            copy_btn.setIcon(_svg_icon("copy", _SVG_COPY))
            copy_btn.setIconSize(icon_sz)
            copy_btn.setFixedSize(26, 24)
            copy_btn.setStyleSheet(self._action_btn_style())
            copy_btn.setToolTip("复制")
            copy_btn.setCursor(QCursor(Qt.PointingHandCursor))
            copy_btn.clicked.connect(self._copy_text)
            alayout.addWidget(copy_btn)

            if on_delete:
                delete_btn = QPushButton()
                delete_btn.setIcon(_svg_icon("delete", _SVG_TRASH))
                delete_btn.setIconSize(icon_sz)
                delete_btn.setFixedSize(26, 24)
                delete_btn.setStyleSheet(self._action_btn_style())
                delete_btn.setToolTip("删除")
                delete_btn.setCursor(QCursor(Qt.PointingHandCursor))
                delete_btn.clicked.connect(self._do_delete)
                alayout.addWidget(delete_btn)

            if on_rewrite:
                rewrite_btn = QPushButton()
                rewrite_btn.setIcon(_svg_icon("rewrite", _SVG_RESET))
                rewrite_btn.setIconSize(icon_sz)
                rewrite_btn.setFixedSize(26, 24)
                rewrite_btn.setStyleSheet(self._action_btn_style())
                rewrite_btn.setToolTip("重写")
                rewrite_btn.setCursor(QCursor(Qt.PointingHandCursor))
                rewrite_btn.clicked.connect(self._do_rewrite)
                alayout.addWidget(rewrite_btn)

            alayout.addStretch()

            if created_at:
                from datetime import datetime
                try:
                    dt = datetime.fromisoformat(created_at)
                    time_lbl = QLabel(dt.strftime("%Y-%m-%d %H:%M"))
                    time_lbl.setStyleSheet(f"color: {C['muted']}; font-size: 11px; background: transparent;")
                    alayout.addWidget(time_lbl)
                except:
                    pass

            self._action_row.hide()
            content_col.addWidget(self._action_row, 0, Qt.AlignRight)
        else:
            # ── assistant: left-aligned, no bubble ──
            browser = _FoldableTextBrowser()
            browser.setReadOnly(True)
            browser.setOpenExternalLinks(True)
            browser.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            browser.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            browser.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
            browser.document().setDefaultStyleSheet(_MD_CSS)
            browser.setStyleSheet(
                "QTextBrowser { background: transparent; color: #e4e4e7;"
                " border: none; padding: 0; font-size: 14px; }"
            )
            self._folded_ids = set()  # 记录被折叠的块
            self._auto_fold_new_blocks(text)
            browser.setHtml(self._render_with_folds(text))
            self._label = browser
            content_col.addWidget(browser)
            self._adjust_browser_height()

            self._action_row = QWidget()
            self._action_row.setStyleSheet("background: transparent;")
            alayout = QHBoxLayout(self._action_row)
            alayout.setContentsMargins(0, 4, 0, 0)
            alayout.setSpacing(4)

            icon_sz = QSize(15, 15)

            copy_btn = QPushButton()
            copy_btn.setIcon(_svg_icon("copy", _SVG_COPY))
            copy_btn.setIconSize(icon_sz)
            copy_btn.setFixedSize(26, 24)
            copy_btn.setStyleSheet(self._action_btn_style())
            copy_btn.setToolTip("复制")
            copy_btn.setCursor(QCursor(Qt.PointingHandCursor))
            copy_btn.clicked.connect(self._copy_text)
            alayout.addWidget(copy_btn)

            if on_delete:
                delete_btn = QPushButton()
                delete_btn.setIcon(_svg_icon("delete", _SVG_TRASH))
                delete_btn.setIconSize(icon_sz)
                delete_btn.setFixedSize(26, 24)
                delete_btn.setStyleSheet(self._action_btn_style())
                delete_btn.setToolTip("删除")
                delete_btn.setCursor(QCursor(Qt.PointingHandCursor))
                delete_btn.clicked.connect(self._do_delete)
                alayout.addWidget(delete_btn)

            if on_resend:
                regen_btn = QPushButton()
                regen_btn.setIcon(_svg_icon("regen", _SVG_REGEN))
                regen_btn.setIconSize(icon_sz)
                regen_btn.setFixedSize(26, 24)
                regen_btn.setStyleSheet(self._action_btn_style())
                regen_btn.setToolTip("重新生成")
                regen_btn.setCursor(QCursor(Qt.PointingHandCursor))
                regen_btn.clicked.connect(self._do_resend)
                alayout.addWidget(regen_btn)

            export_btn = QPushButton()
            export_btn.setIcon(_svg_icon("save", _SVG_SAVE))
            export_btn.setIconSize(icon_sz)
            export_btn.setFixedSize(26, 24)
            export_btn.setStyleSheet(self._action_btn_style())
            export_btn.setToolTip("导出为md")
            export_btn.setCursor(QCursor(Qt.PointingHandCursor))
            export_btn.clicked.connect(self._export_as_md)
            alayout.addWidget(export_btn)

            alayout.addStretch()

            if created_at:
                from datetime import datetime
                try:
                    dt = datetime.fromisoformat(created_at)
                    time_lbl = QLabel(dt.strftime("%Y-%m-%d %H:%M"))
                    time_lbl.setStyleSheet(f"color: {C['muted']}; font-size: 11px; background: transparent;")
                    alayout.addWidget(time_lbl)
                except:
                    pass

            self._action_row.hide()
            content_col.addWidget(self._action_row)

        # ── assemble: assistant left, user right ──
        if is_user:
            outer.addStretch(1)
            outer.addLayout(content_col, 0)
            outer.addWidget(avatar, 0, Qt.AlignTop)
        else:
            outer.addWidget(avatar, 0, Qt.AlignTop)
            outer.addLayout(content_col, 1)

    def _copy_text(self):
        QApplication.clipboard().setText(self._text)

    def _do_resend(self):
        if self._on_resend:
            self._on_resend()

    def _do_delete(self):
        if self._on_delete:
            self._on_delete()

    def _do_rewrite(self):
        if self._on_rewrite:
            self._on_rewrite()

    def _export_as_md(self):
        from PySide6.QtWidgets import QFileDialog
        import os
        from datetime import datetime
        default_name = f"msg_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        file_path, _ = QFileDialog.getSaveFileName(
            self, "导出为 Markdown", default_name, "Markdown 文件 (*.md);;所有文件 (*)"
        )
        if file_path:
            try:
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(self._text)
            except Exception as e:
                import traceback
                traceback.print_exc()

    def enterEvent(self, event):
        if self._action_row and self._finished:
            self._action_row.show()
        super().enterEvent(event)

    def leaveEvent(self, event):
        if self._action_row:
            self._action_row.hide()
        super().leaveEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._role != "user" and hasattr(self, '_label'):
            self._adjust_browser_height()

    def set_finished(self, done: bool):
        self._finished = done
        if not done and self._action_row:
            self._action_row.hide()

    def _adjust_browser_height(self):
        doc = self._label.document()
        w = self._label.width()
        if w < 50:
            w = 460
        doc.setTextWidth(w - 6)
        self._label.setFixedHeight(int(doc.size().height() + 8))

    def set_text(self, text: str):
        self._text = text
        if self._role == "user":
            self._label.setText(text)
            self._label.adjustSize()
        else:
            self._auto_fold_new_blocks(text)
            self._label.setHtml(self._render_with_folds(text))
            self._adjust_browser_height()

    def highlight(self, keyword: str):
        """Apply highlight and return keyword's y position in document, or None."""
        if not keyword or not self._text:
            return None
        kw_lower = keyword.lower()
        text_lower = self._text.lower()
        if kw_lower not in text_lower:
            return None
        if self._role == "user":
            escaped = self._text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            kw_esc = keyword.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            highlighted = escaped.replace(kw_esc, f'<span style="background: rgba(251,191,36,0.35); color: #fbbf24;">{kw_esc}</span>')
            self._label.setText(highlighted)
            self._label.adjustSize()
            return 0  # plain text, keyword at top
        else:
            from PySide6.QtGui import QTextDocument, QTextCursor, QTextCharFormat
            doc = self._label.document()
            cursor = QTextCursor(doc)
            flags = QTextDocument.FindFlags(0)
            fmt = QTextCharFormat()
            fmt.setBackground(QColor(251, 191, 36, 90))
            fmt.setForeground(QColor(251, 191, 36))
            keyword_y = None
            while True:
                cursor = doc.find(keyword, cursor, flags)
                if cursor.isNull():
                    break
                cursor.mergeCharFormat(fmt)
                if keyword_y is None:
                    keyword_y = self._label.cursorRect(cursor).y()
            self._adjust_browser_height()
            return keyword_y

    def clear_highlight(self):
        if self._role == "user":
            self._label.setText(self._text)
            self._label.adjustSize()
        else:
            self._label.setHtml(self._render_with_folds(self._text))
            self._adjust_browser_height()


    def _parse_foldable_blocks(self, text: str):
        """解析文本为可折叠块，返回 [(type, title_or_None, content), ...]"""
        import re
        lines = text.split('\n')
        blocks = []
        current_type = "normal"
        current_title = None
        current_lines = []

        for line in lines:
            # 检查是否是折叠块开始
            llm_match = re.match(r'^\s*\*\*LLM Running \(Turn \d+\) \.\.\.\*\*\s*$', line)
            tool_match = re.match(r'^\s*🛠️\s*Tool:', line)
            tool_compact_match = re.match(r'^\s*🛠️\s+\w+\(', line)

            is_foldable_start = llm_match or tool_match or tool_compact_match

            if is_foldable_start:
                if current_lines:
                    blocks.append((current_type, current_title, '\n'.join(current_lines)))

                title = line.strip()
                if llm_match:
                    title = line.strip().replace('**', '')
                current_type = "foldable"
                current_title = title
                current_lines = [line]
            else:
                current_lines.append(line)

        if current_lines:
            blocks.append((current_type, current_title, '\n'.join(current_lines)))

        return blocks

    def _auto_fold_new_blocks(self, text: str):
        """将新出现的折叠块加入 _folded_ids（仅在此处修改集合）"""
        for _, title, _ in self._parse_foldable_blocks(text):
            if title is not None and title not in self._folded_ids:
                self._folded_ids.add(title)

    def _render_with_folds(self, text: str) -> str:
        """渲染文本为带折叠的 HTML（纯渲染，不修改 _folded_ids）"""
        from urllib.parse import quote
        blocks = self._parse_foldable_blocks(text)
        html_parts = []

        for i, (block_type, title, content) in enumerate(blocks):
            if block_type == "normal":
                html_parts.append(f'<div>{_md_to_html(content)}</div>')
            else:
                safe_title = quote(title, safe='')
                display_title = title.replace('**', '')
                if title in self._folded_ids:
                    # 折叠状态：只显示标题 + 展开链接
                    html_parts.append(
                        f'<div><p><a href="#fold_{safe_title}" style="color: {C["muted"]}; text-decoration: none;">▶ {display_title} (点击展开)</a></p></div>'
                    )
                else:
                    # 展开状态：显示标题 + 折叠链接 + 内容
                    html_parts.append(
                        f'<div><p><a href="#fold_{safe_title}" style="color: {C["muted"]}; text-decoration: none;">▼ {display_title} (点击折叠)</a></p>{_md_to_html(content)}</div>'
                    )
        return '\n'.join(html_parts)

    def _toggle_fold(self, title):
        """折叠/展开切换"""
        if title in self._folded_ids:
            self._folded_ids.remove(title)
        else:
            self._folded_ids.add(title)
        self._label.setHtml(self._render_with_folds(self._text))
        self._adjust_browser_height()


class _TabButton(QPushButton):
    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setCheckable(True)
        self.setFixedHeight(30)
        self.apply_theme()

    def apply_theme(self):
        self.setStyleSheet(build_tab_button_style(C))


def _action_btn(label: str, color: str, icon: QIcon | None = None) -> QPushButton:
    btn = QPushButton(label)
    if icon and not icon.isNull():
        btn.setIcon(icon)
        btn.setIconSize(QSize(16, 16))
    btn.setFixedHeight(36)
    btn.setStyleSheet(f"""
        QPushButton {{
            background: {C['hover_bg']}; color: {C['text']};
            border: 1px solid {C['border'].name()};
            border-left: 3px solid {color};
            border-radius: 8px; padding: 0 14px;
            font-size: 13px; font-weight: 700; text-align: left;
        }}
        QPushButton:hover {{ background: {C['hover_bg']}; }}
        QPushButton:checked {{ color: {color}; background: {C['hover_bg']}; }}
    """)
    return btn


# ── Main panel ────────────────────────────────────────────────────────────────
class ChatPanel(QWidget):
    """Frameless always-on-top chat window."""

    def __init__(self, runner):
        super().__init__()
        self.runner = runner
        self.agent = runner  # compatibility alias for internal use

        # session state
        self._messages: list[dict] = []
        self._session = {"id": _make_session_id(), "title": "新对话", "messages": []}
        self._history: list[dict] = _load_history()
        self._pending_files: list[dict] = []  # {'name','type','raw'}
        self._settings_health_checked = False

        # streaming state
        self._display_queue: Optional[_queue.Queue] = None
        self._streaming_row: Optional[_MsgRow] = None
        self._streaming_text = ""
        self._user_scrolled_up = False
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_queue)

        # autonomous mode
        self.autonomous_enabled = False
        self.last_reply_time = time.time()

        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.resize(530, 700)

        # drag state (title bar)
        self._drag_pos: Optional[QPoint] = None
        self._current_theme = "dark"

        self._build_ui()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRect(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        grad = QLinearGradient(0, 0, 0, self.height())
        bg = C["bg"]
        if isinstance(bg, QColor):
            r, g, b, a = bg.red(), bg.green(), bg.blue(), bg.alpha()
        else:
            r, g, b, a = 14, 14, 18, 255
        grad.setColorAt(0.0, QColor(r, g, b, a))
        grad.setColorAt(1.0, QColor(max(0, r - 10), max(0, g - 10), max(0, b - 14), a))
        p.fillPath(path, grad)

    def resizeEvent(self, event):
        path = QPainterPath()
        path.addRect(0, 0, float(self.width()), float(self.height()))
        self.setMask(QRegion(path.toFillPolygon().toPolygon()))
        super().resizeEvent(event)

    # ── UI construction ───────────────────────────────────────────────────────
    def _build_ui(self):
        self._separators: list[_Separator] = []
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_titlebar())
        sep1 = _Separator()
        self._separators.append(sep1)
        root.addWidget(sep1)
        root.addWidget(self._build_tabbar())
        sep2 = _Separator()
        self._separators.append(sep2)
        root.addWidget(sep2)

        self._stack = QStackedWidget()
        self._stack.setStyleSheet("background: transparent;")
        self._stack.addWidget(self._build_chat_page())    # 0
        self._stack.addWidget(self._build_history_page()) # 1
        self._stack.addWidget(self._build_sop_page())     # 2
        self._stack.addWidget(self._build_settings_page())# 3
        root.addWidget(self._stack)
        root.addWidget(self._build_statusbar())

        # Now that _stack exists, activate the first tab
        self._switch_tab(0)

    # ── title bar ─────────────────────────────────────────────────────────────
    def _build_titlebar(self) -> QWidget:
        bar = QWidget()
        bar.setFixedHeight(48)
        bar.setStyleSheet("background: transparent;")
        bar.setCursor(QCursor(Qt.SizeAllCursor))

        ly = QHBoxLayout(bar)
        ly.setContentsMargins(16, 0, 10, 0)
        ly.setSpacing(8)

        # Search button
        search_btn = QPushButton()
        search_btn.setIcon(_svg_icon("search", _SVG_SEARCH, "#a1a1aa"))
        search_btn.setIconSize(QSize(16, 16))
        search_btn.setFixedSize(26, 26)
        search_btn.setCursor(QCursor(Qt.PointingHandCursor))
        search_btn.setStyleSheet("""
            QPushButton { background: transparent; border: none; border-radius: 13px; }
            QPushButton:hover { background: rgba(63,63,70,0.6); }
        """)
        search_btn.clicked.connect(self._toggle_search)
        self._search_btn = search_btn
        ly.addWidget(search_btn)

        # Search widget (hidden by default)
        self._search_widget = QWidget()
        self._search_widget.hide()
        sw_ly = QHBoxLayout(self._search_widget)
        sw_ly.setContentsMargins(0, 0, 0, 0)
        sw_ly.setSpacing(6)

        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("搜索当前对话和历史...")
        self._search_input.setFixedHeight(26)
        self._search_input.setStyleSheet(f"""
            QLineEdit {{
                background: rgba(32,32,38,0.9);
                border: 1px solid {C['border'].name()};
                border-radius: 13px;
                color: {C['text']};
                font-size: 13px;
                padding: 0 10px;
            }}
            QLineEdit::placeholder {{ color: {C['muted']}; }}
        """)
        self._search_input.setFixedWidth(200)
        self._search_input.textChanged.connect(self._on_search_changed)
        self._search_input.installEventFilter(self)
        sw_ly.addWidget(self._search_input)

        close_search = QPushButton("×")
        close_search.setFixedSize(26, 26)
        close_search.setCursor(QCursor(Qt.PointingHandCursor))
        close_search.setStyleSheet("""
            QPushButton { background: transparent; color: #71717a; border: none; font-size: 16px; }
            QPushButton:hover { color: #a1a1aa; }
        """)
        close_search.clicked.connect(self._hide_search)
        sw_ly.addWidget(close_search)
        ly.addWidget(self._search_widget)

        ly.addStretch()

        # Theme toggle button
        theme_btn = QPushButton()
        theme_btn.setIcon(_svg_icon("moon", SVG_MOON, C["svg_color"]))
        theme_btn.setIconSize(QSize(14, 14))
        theme_btn.setFixedSize(26, 26)
        theme_btn.setCursor(QCursor(Qt.PointingHandCursor))
        theme_btn.setToolTip("Toggle light/dark theme")
        theme_btn.setStyleSheet("""
            QPushButton { background: rgba(63,63,70,0.6); color: #a1a1aa;
                border: none; border-radius: 13px; }
            QPushButton:hover { background: rgba(63,63,70,0.9); color: white; }
        """)
        theme_btn.clicked.connect(self._toggle_theme)
        self._theme_btn = theme_btn
        ly.addWidget(theme_btn)

        # Minimize button
        mini = QPushButton()
        mini.setIcon(_svg_icon("minimize", SVG_MINIMIZE, C["svg_color"]))
        mini.setIconSize(QSize(14, 14))
        mini.setFixedSize(26, 26)
        mini.setCursor(QCursor(Qt.PointingHandCursor))
        mini.setToolTip(self.tr("\u6700\u5C0F\u5316"))
        mini.setStyleSheet(build_titlebar_btn_style(C))
        mini.clicked.connect(self.hide)
        self._mini_btn = mini
        ly.addWidget(mini)

        # Maximize button
        maxi = QPushButton()
        maxi.setIcon(_svg_icon("maximize", SVG_MAXIMIZE, C["svg_color"]))
        maxi.setIconSize(QSize(14, 14))
        maxi.setFixedSize(26, 26)
        maxi.setCursor(QCursor(Qt.PointingHandCursor))
        maxi.setToolTip(self.tr("\u6700\u5927\u5316"))
        maxi.setStyleSheet(build_titlebar_btn_style(C))
        maxi.clicked.connect(self._toggle_maximize)
        self._maxi_btn = maxi
        ly.addWidget(maxi)

        # Close button
        close = QPushButton()
        close.setIcon(_svg_icon("close", SVG_CLOSE, C["svg_color"]))
        close.setIconSize(QSize(14, 14))
        close.setFixedSize(26, 26)
        close.setCursor(QCursor(Qt.PointingHandCursor))
        close.setToolTip(self.tr("\u5173\u95ED"))
        close.setStyleSheet(build_titlebar_btn_style(C, danger=True))
        close.clicked.connect(lambda: (self.close(), QApplication.instance().quit()))
        self._close_btn = close
        ly.addWidget(close)

        # Drag
        bar.mousePressEvent   = self._tb_press
        bar.mouseMoveEvent    = self._tb_move
        bar.mouseReleaseEvent = self._tb_release
        return bar

    def _toggle_search(self):
        if hasattr(self, "_search_visible") and self._search_visible:
            self._hide_search()
        else:
            self._show_search()

    def _show_search(self):
        self._search_visible = True
        self._search_btn.setFixedSize(0, 0)
        self._search_widget.show()
        self._search_input.setFocus()
        self._search_input.selectAll()

    def _hide_search(self):
        self._search_visible = False
        self._search_btn.setFixedSize(26, 26)
        self._search_widget.hide()
        self._search_input.clear()
        self._clear_all_highlights()
        if self._stack.currentIndex() == 1:
            self._reset_history_items_style()

    def _hide_search_if_no_focus(self):
        if not self._search_input.hasFocus():
            self._hide_search()

    def _on_search_changed(self, text):
        if not text.strip():
            self._clear_all_highlights()
            return
        keyword = text.strip()
        current_tab = self._stack.currentIndex()

        if current_tab == 0:
            self._search_current_chat(keyword)
        elif current_tab == 1:
            self._search_history(keyword)

    def _clear_all_highlights(self):
        for i in range(self._msg_layout.count() - 1):
            w = self._msg_layout.itemAt(i).widget()
            if isinstance(w, _MsgRow):
                w.clear_highlight()

    def _search_current_chat(self, keyword: str):
        first_found = None
        first_keyword_y = None
        for i in range(self._msg_layout.count() - 1):
            w = self._msg_layout.itemAt(i).widget()
            if isinstance(w, _MsgRow):
                if keyword.lower() in w._text.lower():
                    kw_y = w.highlight(keyword)
                    if first_found is None:
                        first_found = w
                        first_keyword_y = kw_y
                else:
                    w.clear_highlight()
        # 滚动到第一个匹配项（使用关键词在文档内的实际位置）
        if first_found:
            self._scroll_to_widget(first_found, first_keyword_y or 0)

    def _scroll_to_widget(self, w, keyword_y=0):
        self._user_scrolled_up = True
        self._msg_container.layout().activate()
        QApplication.processEvents()

        sb = self._scroll.verticalScrollBar()
        vp_h = self._scroll.viewport().height()
        keyword_screen_y = w.y() + keyword_y
        target = keyword_screen_y - vp_h // 3
        target = max(0, min(target, sb.maximum()))
        sb.setValue(target)
        QApplication.processEvents()
        self._scroll.viewport().repaint()

    def _search_history(self, keyword: str):
        kw_lower = keyword.lower()
        for i in range(self._hist_list.count()):
            item = self._hist_list.item(i)
            session = item.data(Qt.UserRole)
            messages = session.get("messages", []) if session else []
            content_text = " ".join([m.get("content", "") for m in messages if isinstance(m.get("content"), str)])
            match = kw_lower in content_text.lower()
            item.setHidden(not match)
            if match:
                item.setBackground(QColor(251, 191, 36, 50))
                item.setForeground(QColor(251, 191, 36))
            else:
                item.setBackground(QColor(0, 0, 0, 0))
                item.setForeground(QColor(255, 255, 255))

    def _reset_history_items_style(self):
        for i in range(self._hist_list.count()):
            item = self._hist_list.item(i)
            item.setHidden(False)
            item.setBackground(QColor(0, 0, 0, 0))
            item.setForeground(QColor(255, 255, 255))
            w = self._hist_list.itemWidget(item)
            if w:
                w.setStyleSheet(
                    f"background: rgba(35,35,42,0.6); color: {C['text']};"
                    " border: 1px solid #3f3f46; border-radius: 8px;"
                    " padding: 8px 12px; margin: 2px 0;"
                )

    def _tb_press(self, e):
        if e.button() == Qt.LeftButton:
            self._drag_pos = e.globalPosition().toPoint() - self.pos()

    def _tb_move(self, e):
        if e.buttons() == Qt.LeftButton and self._drag_pos is not None:
            self.move(e.globalPosition().toPoint() - self._drag_pos)

    def _tb_release(self, _e):
        self._drag_pos = None

    def _toggle_maximize(self):
        if self.isMaximized():
            self.showNormal()
            self._maxi_btn.setIcon(_svg_icon("maximize", SVG_MAXIMIZE, C["svg_color"]))
            self._maxi_btn.setToolTip(self.tr("最大化"))
        else:
            self.showMaximized()
            self._maxi_btn.setIcon(_svg_icon("restore", SVG_RESTORE, C["svg_color"]))
            self._maxi_btn.setToolTip(self.tr("恢复"))

    def _toggle_theme(self):
        """Switch between dark and light theme."""
        global C
        new_theme = "light" if self._current_theme == "dark" else "dark"
        self._current_theme = new_theme
        if new_theme == "light":
            C = C_LIGHT
        else:
            C = C_DARK
        self._apply_theme()

    def _apply_theme(self):
        """Reapply styles after theme change."""
        svg_color = C.get("svg_color", "#a78bfa")

        # ── paint event ──
        self.update()

        # ── titlebar buttons ──
        self._theme_btn.setIcon(
            _svg_icon("moon", SVG_MOON, svg_color) if self._current_theme == "dark"
            else _svg_icon("sun", SVG_SUN, svg_color)
        )
        for btn, svg in [
            (self._mini_btn, SVG_MINIMIZE),
            (self._maxi_btn, SVG_MAXIMIZE if not self.isMaximized() else SVG_RESTORE),
            (self._close_btn, SVG_CLOSE),
        ]:
            btn.setIcon(_svg_icon("winctl", svg, svg_color))
            btn.setStyleSheet(build_titlebar_btn_style(C, danger=(btn is self._close_btn)))
        self._search_btn.setIcon(_svg_icon("search", SVG_SEARCH, C["muted"]))
        self._search_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; border: none; border-radius: 13px; }}
            QPushButton:hover {{ background: {C['hover_bg']}; }}
        """)
        self._theme_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; border: none; border-radius: 13px; }}
            QPushButton:hover {{ background: {C['hover_bg']}; }}
        """)

        # ── tab bar ──
        self._tabbar_widget.setStyleSheet(f"background: {C['panel'].name()};")
        for btn in self._tabs:
            btn.apply_theme()
        self._new_chat_btn.setStyleSheet(f"""
            QPushButton {{ background: {C['accent_bg']}; color: {C['svg_color']};
                border: 1px solid {C['accent_bdr']}; border-radius: 7px;
                padding: 0 10px; font-size: 12px; font-weight: 700; }}
            QPushButton:hover {{ background: {C['accent_bg']}; color: {C['text']}; }}
        """)

        # ── separators ──
        for sep in self._separators:
            sep.apply_theme()

        # ── markdown CSS & assistant browsers ──
        self._rebuild_md_css()

        # ── status bar ──
        self._status_dot.setStyleSheet(f"color: {C['green']}; font-size: 9px;")

        # ── message rows ──
        for i in range(self._msg_layout.count()):
            w = self._msg_layout.itemAt(i).widget()
            if isinstance(w, _MsgRow):
                w.apply_theme()
                # Update assistant browser stylesheet + document CSS
                if not w._is_user and hasattr(w, '_label') and isinstance(w._label, QTextBrowser):
                    w._label.setStyleSheet(
                        f"QTextBrowser {{ background: transparent; color: {C['text']};"
                        f" border: none; padding: 0; font-size: 14px; }}"
                    )
                    w._label.document().setDefaultStyleSheet(_MD_CSS)
            elif isinstance(w, QLabel):
                # System notice labels
                w.setStyleSheet(
                    f"QLabel {{ background: transparent; color: {C['muted']};"
                    f" border: none; padding: 6px 20px; font-size: 12px; }}"
                )

        # ── model rows ──
        if hasattr(self, '_model_row_widgets'):
            self._refresh_model_rows_style()

        # ── input area ──
        self._input.setStyleSheet(
            f"QTextEdit {{ background: transparent; color: {C['text']};"
            f" border: none; padding: 10px 12px; font-size: 14px; }}"
        )

    def _rebuild_md_css(self):
        """Rebuild _MD_CSS from current C palette."""
        global _MD_CSS
        _MD_CSS = f"""
        body {{ color: {C['text']}; font-family: "Arial", "Microsoft YaHei", sans-serif; font-size: 13px; line-height: 1.6; font-weight: 400; }}
        h1 {{ color: {C['text']}; font-size: 20px; font-weight: 700; border-bottom: 1px solid {C['border']}; padding-bottom: 4px; margin-top: 16px; }}
        h2 {{ color: {C['text']}; font-size: 17px; font-weight: 700; border-bottom: 1px solid {C['border']}; padding-bottom: 3px; margin-top: 14px; }}
        h3 {{ color: {C['text']}; font-size: 15px; font-weight: 600; margin-top: 12px; }}
        h4,h5,h6 {{ color: {C['muted']}; font-size: 13px; font-weight: 600; margin-top: 10px; }}
        code {{ background: {C['hover_bg']}; color: {C['svg_color']}; padding: 1px 4px; border-radius: 3px;
               font-family: Consolas, "Courier New", monospace; font-size: 12px; }}
        pre  {{ background: rgba(24,24,30,0.95); border: 1px solid {C['border']}; border-radius: 6px;
               padding: 10px 12px; margin: 8px 0; }}
        pre code {{ background: transparent; padding: 0; color: {C['text']}; }}
        a {{ color: {C['svg_color']}; text-decoration: none; }}
        a:hover {{ text-decoration: underline; }}
        blockquote {{ border-left: 3px solid {C['accent']}; margin: 8px 0 8px 0; padding: 4px 0 4px 12px; color: {C['muted']}; }}
        table {{ border-collapse: collapse; margin: 8px 0; }}
        th, td {{ border: 1px solid {C['border']}; padding: 5px 10px; }}
        th {{ background: {C['hover_bg']}; color: {C['text']}; font-weight: 700; }}
        hr {{ border: none; border-top: 1px solid {C['border']}; margin: 12px 0; }}
        ul, ol {{ padding-left: 22px; margin: 4px 0; }}
        li {{ margin: 2px 0; }}
        p {{ margin: 6px 0; }}
        """

    # ── status bar ─────────────────────────────────────────────────────────────
    def _build_statusbar(self) -> QWidget:
        bar = QWidget()
        bar.setFixedHeight(24)
        bar.setStyleSheet("background: transparent;")
        ly = QHBoxLayout(bar)
        ly.setContentsMargins(16, 0, 10, 0)
        ly.setSpacing(8)

        # Status dot
        dot = QLabel("●")
        dot.setStyleSheet(f"color: {C['green']}; font-size: 9px;")
        dot.setFixedWidth(12)
        self._status_dot = dot
        ly.addWidget(dot)

        # Model name (clickable to show model list)
        self._model_badge = QLabel(self._model_name())
        self._model_badge.setStyleSheet(f"color: {C['muted']}; font-size: 11px;")
        self._model_badge.setCursor(QCursor(Qt.PointingHandCursor))
        self._model_badge.mousePressEvent = lambda e: self._show_model_menu(e)
        ly.addWidget(self._model_badge)

        self._streaming_badge = _StreamingBadge()
        ly.addWidget(self._streaming_badge)

        ly.addStretch()
        return bar

    def _show_model_menu(self, _e):
        menu = QMenu(self._model_badge)
        menu.setStyleSheet(f"""
            QMenu {{
                background: {C['panel'].name()};
                border: 1px solid {C['border'].name()};
                padding: 4px 0;
            }}
            QMenu::item {{
                color: {C['text']};
                padding: 6px 20px 6px 12px;
                font-size: 12px;
            }}
            QMenu::item:selected {{
                background: {C['hover_bg']};
            }}
        """)
        backends = self.runner.list_backends()
        for i, (name, model, active) in enumerate(backends):
            display = f"{name}/{model}"
            act = menu.addAction(f"{display}  #{i + 1}")
            act.triggered.connect(lambda _, idx=i: self._do_switch_to(idx))
        menu.exec(QCursor.pos())

    # ── tab bar ───────────────────────────────────────────────────────────────
    def _build_tabbar(self) -> QWidget:
        bar = QWidget()
        bar.setFixedHeight(40)
        bar.setStyleSheet(f"background: {C['panel'].name()};")
        self._tabbar_widget = bar

        ly = QHBoxLayout(bar)
        ly.setContentsMargins(12, 5, 12, 5)
        ly.setSpacing(4)

        self._tabs: list[_TabButton] = []
        tab_defs = [
            (_SVG_CHAT,  "对话"),
            (_SVG_CLOCK, "历史"),
            (_SVG_BOOK,  "SOP"),
            (_SVG_GEAR,  "设置"),
        ]
        for i, (svg, text) in enumerate(tab_defs):
            btn = _TabButton(text)
            btn.setIcon(_svg_icon(text, svg, "#b0b0b8"))
            btn.setIconSize(QSize(14, 14))
            btn.clicked.connect(lambda _checked, idx=i: self._switch_tab(idx))
            ly.addWidget(btn)
            self._tabs.append(btn)

        ly.addStretch()

        new_btn = QPushButton("新对话")
        new_btn.setIcon(_svg_icon("plus", _SVG_PLUS, C["svg_color"]))
        new_btn.setIconSize(QSize(12, 12))
        new_btn.setFixedHeight(27)
        new_btn.setStyleSheet(f"""
            QPushButton {{ background: {C['accent_bg']}; color: {C['svg_color']};
                border: 1px solid {C['accent_bdr']}; border-radius: 7px;
                padding: 0 10px; font-size: 12px; font-weight: 700; }}
            QPushButton:hover {{ background: {C['accent_bg']}; color: {C['text']}; }}
        """)
        new_btn.clicked.connect(self._new_session)
        self._new_chat_btn = new_btn
        ly.addWidget(new_btn)

        # NOTE: _switch_tab(0) is called in _build_ui() after _stack is created
        return bar

    def _switch_tab(self, idx: int):
        self._stack.setCurrentIndex(idx)
        for i, btn in enumerate(self._tabs):
            btn.setChecked(i == idx)
        # 切换标签时关闭搜索框
        if hasattr(self, '_search_visible') and self._search_visible:
            self._hide_search()
        if idx == 1:
            self._refresh_history()
        if idx == 2:
            self._refresh_sop()
        if idx == 3:
            self._refresh_model_rows_style()
            if not self._settings_health_checked:
                self._start_health_checks()
                self._settings_health_checked = True

    # ── chat page ─────────────────────────────────────────────────────────────
    def _build_chat_page(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        ly = QVBoxLayout(page)
        ly.setContentsMargins(0, 0, 0, 0)
        ly.setSpacing(0)

        # ── message scroll area ──
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet(f"QScrollArea {{ background: transparent; border: none; }} {_SCROLLBAR_STYLE}")

        self._msg_container = QWidget()
        self._msg_container.setStyleSheet("background: transparent;")
        self._msg_layout = QVBoxLayout(self._msg_container)
        self._msg_layout.setContentsMargins(0, 12, 0, 12)
        self._msg_layout.setSpacing(4)
        self._msg_layout.addStretch()

        self._scroll.setWidget(self._msg_container)
        self._scroll.verticalScrollBar().valueChanged.connect(self._on_scroll)

        # ── scroll navigation buttons (centered at bottom of message area) ──
        scroll_wrapper = QWidget()
        scroll_wrapper.setStyleSheet("background: transparent;")
        wrap_ly = QVBoxLayout(scroll_wrapper)
        wrap_ly.setContentsMargins(0, 0, 0, 0)
        wrap_ly.setSpacing(0)
        wrap_ly.addWidget(self._scroll)

        self._nav_widget = QWidget()
        self._nav_widget.setFixedSize(68, 28)
        self._nav_widget.setStyleSheet("background: transparent; border: none;")
        nav_ly = QHBoxLayout(self._nav_widget)
        nav_ly.setContentsMargins(6, 2, 6, 2)
        nav_ly.setSpacing(8)

        self._nav_up = QPushButton("∧")
        self._nav_up.setFixedWidth(26)
        self._nav_up.setCursor(QCursor(Qt.PointingHandCursor))
        self._nav_up.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C['muted']}; border: none; font-size: 14px; }}
            QPushButton:hover {{ color: {C['text']}; }}
            QPushButton:disabled {{ color: {C['border'].name()}; }}
        """)
        self._nav_up.clicked.connect(self._scroll_to_top)

        self._nav_down = QPushButton("∨")
        self._nav_down.setFixedWidth(26)
        self._nav_down.setCursor(QCursor(Qt.PointingHandCursor))
        self._nav_down.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C['muted']}; border: none; font-size: 14px; }}
            QPushButton:hover {{ color: {C['text']}; }}
            QPushButton:disabled {{ color: {C['border'].name()}; }}
        """)
        self._nav_down.clicked.connect(self._scroll_to_bottom)

        nav_ly.addWidget(self._nav_up)
        nav_ly.addWidget(self._nav_down)

        wrap_ly.addWidget(self._nav_widget, 0, Qt.AlignHCenter | Qt.AlignBottom)
        self._nav_widget.setContentsMargins(0, 0, 0, 8)
        self._nav_widget.hide()

        ly.addWidget(scroll_wrapper, 1)

        ly.addWidget(_Separator())

        # ── input area ──
        ly.addWidget(self._build_input_area())

        QTimer.singleShot(200, self._update_nav_visibility)
        return page

    def _build_input_area(self) -> QWidget:
        wrap = QWidget()
        wrap.setStyleSheet("background: transparent;")
        ly = QVBoxLayout(wrap)
        ly.setContentsMargins(20, 6, 20, 0)
        ly.setSpacing(0)

        self._chips_row = QWidget()
        self._chips_row.setStyleSheet("background: transparent;")
        self._chips_ly = QHBoxLayout(self._chips_row)
        self._chips_ly.setContentsMargins(0, 0, 0, 6)
        self._chips_ly.setSpacing(6)
        self._chips_row.hide()
        ly.addWidget(self._chips_row)

        card = QWidget()
        card.setStyleSheet(f"""
            QWidget#inputCard {{
                background: rgba(32,32,38,0.85);
                border: 1px solid {C['border'].name()};
                border-radius: 16px;
            }}
            QWidget#inputCard:focus-within {{
                border-color: rgba(124,58,237,0.55);
            }}
        """)
        card.setObjectName("inputCard")
        card_ly = QVBoxLayout(card)
        card_ly.setContentsMargins(14, 10, 10, 10)
        card_ly.setSpacing(6)

        class _PlainTextEdit(QTextEdit):
            def insertFromMimeData(self, source):
                text = source.text() or source.data("text/plain")
                if text:
                    self.insertPlainText(text)

        self._input = _PlainTextEdit()
        self._input.setAutoFormatting(QTextEdit.AutoNone)
        self._input.setFixedHeight(64)
        self._input.setPlaceholderText("给助手发送消息... Enter发送，Shift+Enter换行")
        self._input.setStyleSheet(f"""
            QTextEdit {{
                background: transparent; color: {C['text']};
                border: none; padding: 0; font-size: 14px;
                selection-background-color: rgba(124,58,237,0.4);
            }}
        """)
        self._input.installEventFilter(self)
        self._input.textChanged.connect(self._on_text_changed)
        card_ly.addWidget(self._input)

        bottom = QHBoxLayout()
        bottom.setSpacing(6)

        attach = QPushButton()
        attach.setIcon(_svg_icon("clip", _SVG_CLIP, "#a1a1aa"))
        attach.setIconSize(QSize(17, 17))
        attach.setFixedSize(30, 30)
        attach.setToolTip("上传附件")
        attach.setCursor(QCursor(Qt.PointingHandCursor))
        attach.setStyleSheet("""
            QPushButton { background: transparent; border: none; border-radius: 15px; }
            QPushButton:hover { background: rgba(63,63,70,0.6); }
        """)
        attach.clicked.connect(self._attach_files)
        bottom.addWidget(attach)

        self._char_lbl = QLabel("0 / 2000")
        self._char_lbl.setStyleSheet(f"color: {C['muted']}; font-size: 11px;")
        bottom.addWidget(self._char_lbl)

        self._token_lbl = QLabel("")
        self._token_lbl.setStyleSheet(f"color: {C['muted']}; font-size: 11px; margin-left: 10px;")
        bottom.addWidget(self._token_lbl)

        bottom.addStretch()

        self._is_streaming = False
        self._send_btn = QPushButton()
        self._send_btn.setFixedSize(34, 34)
        self._send_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self._send_btn.clicked.connect(self._on_send_btn_click)
        self._set_send_mode()
        bottom.addWidget(self._send_btn)

        card_ly.addLayout(bottom)
        ly.addWidget(card)
        return wrap

    # ── history page ──────────────────────────────────────────────────────────
    def _build_history_page(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        ly = QVBoxLayout(page)
        ly.setContentsMargins(12, 12, 12, 12)
        ly.setSpacing(8)

        header = QHBoxLayout()
        lbl = QLabel("历史记录")
        lbl.setStyleSheet(f"color: {C['text']}; font-weight: 600; font-size: 14px;")
        header.addWidget(lbl)
        header.addStretch()

        restore_btn = QPushButton("恢复会话")
        restore_btn.setStyleSheet(self._small_btn_style(C["accent"]))
        restore_btn.clicked.connect(self._restore_selected)
        header.addWidget(restore_btn)

        del_btn = QPushButton("删除")
        del_btn.setStyleSheet(self._small_btn_style("#dc2626"))
        del_btn.clicked.connect(self._delete_selected)
        header.addWidget(del_btn)
        ly.addLayout(header)

        self._hist_list = QListWidget()
        self._hist_list.setStyleSheet(f"""
            QListWidget {{ background: transparent; border: none; outline: none; }}
            QListWidget::item {{
                background: rgba(35,35,42,0.6); color: {C['text']};
                border: 1px solid {C['border'].name()}; border-radius: 8px;
                padding: 8px 12px; margin: 2px 0;
            }}
            QListWidget::item:hover {{ background: rgba(55,55,65,0.8);
                border-color: rgba(124,58,237,0.4); }}
            QListWidget::item:selected {{ background: {C["accent_bg"]};
                border-color: rgba(124,58,237,0.6); }}
            {_SCROLLBAR_STYLE}
        """)
        self._hist_list.itemDoubleClicked.connect(self._restore_selected)
        ly.addWidget(self._hist_list)
        return page

    # ── SOP page ──────────────────────────────────────────────────────────────
    def _build_sop_page(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        ly = QVBoxLayout(page)
        ly.setContentsMargins(0, 0, 0, 0)

        splitter = QSplitter(Qt.Horizontal)

        self._sop_list = QListWidget()
        self._sop_list.setMaximumWidth(175)
        self._sop_list.setStyleSheet(f"""
            QListWidget {{ background: rgba(10,10,14,0.7); border: none;
                border-right: 1px solid {C['border'].name()}; outline: none; }}
            QListWidget::item {{ color: {C['muted']}; padding: 7px 10px;
                border-radius: 4px; margin: 1px 4px; }}
            QListWidget::item:hover {{ background: rgba(55,55,65,0.7); color: {C['text']}; }}
            QListWidget::item:selected {{ background: rgba(124,58,237,0.28); color: white; }}
            {_SCROLLBAR_STYLE}
        """)
        self._sop_list.currentItemChanged.connect(self._load_sop)
        splitter.addWidget(self._sop_list)

        self._sop_viewer = QTextBrowser()
        self._sop_viewer.setOpenExternalLinks(True)
        self._sop_viewer.document().setDefaultStyleSheet(_MD_CSS)
        self._sop_viewer.setStyleSheet(f"""
            QTextBrowser {{ background: transparent; color: {C['text']};
                border: none; padding: 10px 14px;
                font-family: "Arial", "Microsoft YaHei", sans-serif;
                font-size: 13px; }}
            {_SCROLLBAR_STYLE}
        """)
        splitter.addWidget(self._sop_viewer)
        splitter.setSizes([165, 340])
        ly.addWidget(splitter)
        return page

    # ── settings page ─────────────────────────────────────────────────────────
    def _build_settings_page(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        ly = QVBoxLayout(page)
        ly.setContentsMargins(16, 16, 16, 16)
        ly.setSpacing(8)

        lbl = QLabel("控制面板")
        lbl.setStyleSheet(f"color: {C['text']}; font-weight: 600; font-size: 14px;")
        ly.addWidget(lbl)

        self._model_info = QLabel(f"当前模型：{self._model_name()} (#{self.runner.llm_no})")
        self._model_info.setStyleSheet(f"color: {C['muted']}; font-size: 12px;")
        ly.addWidget(self._model_info)
        ly.addSpacing(4)

        model_hdr = QLabel("模型列表")
        model_hdr.setStyleSheet(f"color: {C['text']}; font-weight: 600; font-size: 13px;")
        ly.addWidget(model_hdr)

        self._model_rows_container = QWidget()
        self._model_rows_container.setStyleSheet("background: transparent;")
        self._model_rows_layout = QVBoxLayout(self._model_rows_container)
        self._model_rows_layout.setContentsMargins(0, 0, 0, 0)
        self._model_rows_layout.setSpacing(3)
        ly.addWidget(self._model_rows_container)

        self._model_row_widgets: list[dict] = []
        self._health_results: dict[int, bool | None] = {}
        self._build_model_rows()

        ly.addSpacing(6)

        for (lbl_text, color, handler, svg) in [
            ("重置提示词", "#059669", self._do_reset_prompt,  _SVG_RESET),
            ("保存当前会话","#0ea5e9", self._do_save,         _SVG_SAVE),
            ("清空对话",   "#78716c", self._do_clear,         _SVG_TRASH),
        ]:
            b = _action_btn(lbl_text, color, _svg_icon(lbl_text, svg))
            b.clicked.connect(handler)
            ly.addWidget(b)

        ly.addSpacing(10)
        sep = QLabel("自主行动")
        sep.setStyleSheet(f"color: {C['text']}; font-weight: 600; font-size: 13px;")
        ly.addWidget(sep)

        self._auto_btn = _action_btn(f"开启自主行动 (idle > {AUTO_IDLE_THRESHOLD // 60} min 自动触发)", "#f59e0b",
                                      _svg_icon("bolt", _SVG_BOLT))
        self._auto_btn.setCheckable(True)
        self._auto_btn.clicked.connect(self._do_toggle_auto)
        ly.addWidget(self._auto_btn)

        trigger_btn = _action_btn("立即触发一次", "#f59e0b",
                                  _svg_icon("play", _SVG_PLAY))
        trigger_btn.clicked.connect(self._do_trigger_auto)
        ly.addWidget(trigger_btn)

        ly.addStretch()
        return page

    # ── model list ────────────────────────────────────────────────────────────
    @staticmethod
    def _model_row_style() -> str:
        return (
            f"QPushButton {{ background: {C['hover_bg']}; color: {C['text']};"
            f" border: 1px solid {C['border']}; border-radius: 8px;"
            f" padding: 6px 10px; font-size: 12px; font-weight: 700; text-align: left; }}"
            f" QPushButton:hover {{ background: {C['hover_bg']}; }}"
        )

    @staticmethod
    def _model_row_active_style() -> str:
        return (
            f"QPushButton {{ background: {C['accent_bg']}; color: {C['svg_color']};"
            f" border: 1px solid {C['accent_bdr']}; border-radius: 8px;"
            f" padding: 6px 10px; font-size: 12px; font-weight: 700; text-align: left; }}"
            f" QPushButton:hover {{ background: {C['accent_bg']}; }}"
        )

    def _build_model_rows(self):
        while self._model_rows_layout.count():
            w = self._model_rows_layout.takeAt(0).widget()
            if w:
                w.deleteLater()
        self._model_row_widgets.clear()

        for idx, (name, model, active) in enumerate(self.runner.list_backends()):
            display = f"{name}/{model}"
            is_current = active

            row = QWidget()
            row.setStyleSheet("background: transparent;")
            rlay = QHBoxLayout(row)
            rlay.setContentsMargins(0, 0, 0, 0)
            rlay.setSpacing(6)

            dot = QLabel("●")
            dot.setFixedWidth(14)
            dot.setAlignment(Qt.AlignCenter)
            dot.setStyleSheet("color: #71717a; font-size: 11px;")
            rlay.addWidget(dot)

            btn = QPushButton(f"  #{idx}  {display}")
            btn.setCursor(QCursor(Qt.PointingHandCursor))
            btn.setStyleSheet(self._model_row_active_style() if is_current else self._model_row_style())
            btn.clicked.connect(lambda checked, i=idx: self._do_switch_to(i))
            rlay.addWidget(btn, 1)

            self._model_rows_layout.addWidget(row)
            self._model_row_widgets.append({"dot": dot, "btn": btn, "idx": idx})

    def _refresh_model_rows_style(self):
        active_idx = self.runner.llm_no
        for entry in self._model_row_widgets:
            is_current = entry["idx"] == active_idx
            entry["btn"].setStyleSheet(
                self._model_row_active_style() if is_current else self._model_row_style()
            )
            status = self._health_results.get(entry["idx"])
            if status is True:
                entry["dot"].setStyleSheet("color: #22c55e; font-size: 11px;")
            elif status is False:
                entry["dot"].setStyleSheet("color: #ef4444; font-size: 11px;")
            else:
                entry["dot"].setStyleSheet("color: #71717a; font-size: 11px;")

    def _do_switch_to(self, idx: int):
        if idx == self.runner.llm_no:
            return
        self.runner.next_llm(n=idx)
        name = self._model_name()
        self._model_badge.setText(name)
        self._model_info.setText(f"当前模型：{name} (#{self.runner.llm_no})")
        self._add_system_notice(f"已切换至 {name}，对话上下文已保留")
        self._refresh_model_rows_style()

    def _start_health_checks(self):
        self._health_results.clear()
        self._health_pending = 0
        self._health_result_queue = _queue.Queue()
        for entry in self._model_row_widgets:
            entry["dot"].setStyleSheet("color: #71717a; font-size: 11px;")
            entry["dot"].setText("◌")
        backends = self.runner.list_backends()
        for idx, (name, model, active) in enumerate(backends):
            self._health_pending += 1
            t = threading.Thread(target=self._check_backend, args=(idx, name, model), daemon=True)
            t.start()
        if not hasattr(self, '_health_poll_timer'):
            self._health_poll_timer = QTimer(self)
            self._health_poll_timer.timeout.connect(self._poll_health_results)
        self._health_poll_timer.start(500)

    def _poll_health_results(self):
        while True:
            try:
                idx, ok = self._health_result_queue.get_nowait()
                self._health_results[idx] = ok
            except _queue.Empty:
                break
        self._refresh_model_rows_style()
        if len(self._health_results) >= self._health_pending:
            self._health_poll_timer.stop()

    def _check_backend(self, idx: int, name: str, model: str):
        ok = False
        try:
            client = self.runner.za.client
            if client is not None:
                ok = True
            print(f"[HealthCheck] Backend #{idx} {name}/{model}: {'OK' if ok else 'UNKNOWN'}")
        except Exception as e:
            print(f"[HealthCheck] Backend #{idx} {name}/{model}: ERROR -> {e}")
            ok = False
        self._health_result_queue.put((idx, ok))

    # ── event filter (Enter key in text edit, Escape to close search) ──────────
    def eventFilter(self, obj, event):
        if event.type() == QEvent.KeyPress:
            if obj is self._search_input and event.key() == Qt.Key_Escape:
                self._hide_search()
                return True
            if obj is self._input and event.key() in (Qt.Key_Return, Qt.Key_Enter):
                if not (event.modifiers() & Qt.ShiftModifier):
                    self._handle_send()
                    return True
        # 搜索框失焦时关闭搜索
        if event.type() == QEvent.FocusOut and obj is self._search_input:
            # 延迟关闭，等待点击事件处理完毕
            QTimer.singleShot(50, self._hide_search_if_no_focus)
        return super().eventFilter(obj, event)

    def _on_text_changed(self):
        n = len(self._input.toPlainText())
        self._char_lbl.setText(f"{n} / 2000")

    # ── file attachment ────────────────────────────────────────────────────────
    def _attach_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择附件", "",
            "All Files (*);;"
            "Images (*.png *.jpg *.jpeg *.gif *.webp *.bmp);;"
            "Text (*.txt *.md *.py *.json *.csv *.yaml *.yml *.log *.js *.ts *.sql)",
        )
        for path in paths:
            name = os.path.basename(path)
            if any(f["name"] == name for f in self._pending_files):
                continue
            ext = os.path.splitext(path)[1].lower()
            img_exts = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
            mime = (f"image/{ext[1:]}" if ext in img_exts else
                    "text/plain" if ext in TEXT_FILE_EXTS else
                    "application/octet-stream")
            try:
                with open(path, "rb") as fh:
                    raw = fh.read()
                if len(raw) > MAX_UPLOAD_BYTES:
                    print(f"[Attach] 文件过大，已跳过: {name} ({len(raw)} bytes)")
                    continue
                self._pending_files.append({"name": name, "type": mime, "raw": raw})
            except Exception as e:
                print(f"[Attach] Failed to read {path}: {e}")
        self._refresh_chips()

    def _refresh_chips(self):
        while self._chips_ly.count():
            item = self._chips_ly.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not self._pending_files:
            self._chips_row.hide()
            return
        for f in self._pending_files:
            chip = QLabel(f['name'])
            chip.setStyleSheet(f"""
                QLabel {{ background: rgba(55,55,65,0.7); color: {C['text']};
                    border: 1px solid {C['border'].name()}; border-radius: 6px;
                    padding: 3px 8px; font-size: 11px; }}
            """)
            self._chips_ly.addWidget(chip)
        self._chips_ly.addStretch()
        self._chips_row.show()

    # ── send / streaming ───────────────────────────────────────────────────────
    @staticmethod
    def _send_btn_style() -> str:
        return f"""
        QPushButton {{ background: {C['text']}; border: none; border-radius: 17px; }}
        QPushButton:hover {{ background: {C['muted']}; }}
        QPushButton:pressed {{ background: {C['muted']}; }}
        """

    @staticmethod
    def _stop_btn_style() -> str:
        return """
        QPushButton { background: rgba(239,68,68,0.85); border: none; border-radius: 17px; }
        QPushButton:hover { background: rgba(248,113,113,0.9); }
        QPushButton:pressed { background: rgba(220,38,38,0.9); }
        """

    def _set_send_mode(self):
        self._is_streaming = False
        self._send_btn.setText("")
        self._send_btn.setIcon(_svg_icon("send_arrow", _SVG_SEND, "#18181b"))
        self._send_btn.setIconSize(QSize(18, 18))
        self._send_btn.setStyleSheet(self._send_btn_style())

    def _set_stop_mode(self):
        self._is_streaming = True
        self._send_btn.setText("")
        self._send_btn.setIcon(_svg_icon("stop_circle", _SVG_STOP, "#ffffff"))
        self._send_btn.setIconSize(QSize(16, 16))
        self._send_btn.setStyleSheet(self._stop_btn_style())

    def _on_send_btn_click(self):
        if self._is_streaming:
            self._do_stop()
        else:
            self._handle_send()

    def _handle_send(self):
        text = self._input.toPlainText().strip()
        files = self._pending_files.copy()
        if not text and not files:
            return

        if text.startswith("/"):
            self._input.clear()
            self._pending_files.clear()
            self._refresh_chips()
            self._handle_command(text)
            return

        prompt = text or "请分析我上传的附件。"
        full_prompt, display_prompt, _ = _build_prompt_with_uploads(prompt, files)

        # Clear input state
        self._input.clear()
        self._pending_files.clear()
        self._refresh_chips()

        # Update session title
        if self._session["title"] == "新对话" and prompt:
            self._session["title"] = prompt[:20] + ("..." if len(prompt) > 20 else "")

        from datetime import datetime
        now_iso = datetime.now().isoformat()
        user_idx = len(self._messages)
        self._messages.append({"role": "user", "content": display_prompt, "created_at": now_iso})
        self._add_msg_row(
            "user",
            display_prompt,
            created_at=now_iso,
            on_delete=lambda idx=user_idx: self._delete_message(idx),
            on_rewrite=lambda idx=user_idx: self._rewrite_message(idx)
        )
        self._update_token_usage()

        # Start streaming — reset scroll lock so new output auto-scrolls
        self._user_scrolled_up = False
        self._streaming_text = ""
        # The streaming row will be replaced when done, it doesn't need deletion/export
        self._streaming_row = self._add_msg_row("assistant", "▌")
        self._streaming_row.set_finished(False)
        self._set_stop_mode()
        self._streaming_badge.show()

        self._display_queue = self.runner.put_task(f"{FILE_HINT}\n\n{full_prompt}", source="user")
        self._poll_timer.start(40)

    def _handle_command(self, cmd: str):
        parts = cmd.split()
        op = parts[0].lower() if parts else ""
        if op == "/help":
            self._add_system_notice(HELP_TEXT)
        elif op == "/stop":
            self._do_stop()
            self._add_system_notice("⏹️ 已停止")
        elif op == "/status":
            llm = self._model_name()
            state = "🔴 运行中" if self.runner.is_running else "🟢 空闲"
            self._add_system_notice(f"状态: {state}\nLLM: [{self.runner.llm_no}] {llm}")
        elif op == "/llm":
            if not self.runner.llmclient:
                self._add_system_notice("❌ 当前没有可用的 LLM 配置")
            elif len(parts) > 1:
                try:
                    idx = int(parts[1])
                    self._do_switch_to(idx)
                except Exception:
                    self._add_system_notice(f"用法: /llm <0-{len(self.runner.list_backends()) - 1}>")
            else:
                lines = [f"{'→' if active else '  '} [{i}] {name}/{model}"
                         for i, (name, model, active) in enumerate(self.runner.list_backends())]
                self._add_system_notice("LLMs:\n" + "\n".join(lines))
        elif op == "/restore":
            restored_info, err = format_restore()
            if err:
                self._add_system_notice(err)
            else:
                restored, fname, count = restored_info
                self.runner.abort()
                self.runner.history.extend(restored)
                self._add_system_notice(f"✅ 已恢复 {count} 轮对话\n来源: {fname}")
        elif op == "/new":
            self._do_clear()
            self._add_system_notice("✅ 已开启新对话")
        else:
            self._add_system_notice(f"未知命令: {cmd}\n{HELP_TEXT}")

    def _poll_queue(self):
        if not self._display_queue:
            return
        try:
            while True:
                item = self._display_queue.get_nowait()
                if not isinstance(item, dict) or ("next" not in item and "done" not in item):
                    print(f"[Queue] 跳过异常项: {item}")
                    continue
                if "next" in item:
                    self._streaming_text = item["next"]
                    if self._streaming_row:
                        self._streaming_row.set_text(self._streaming_text + " ▌")
                    self._update_token_usage()
                    self._scroll_bottom()
                if "done" in item:
                    final = item["done"]
                    from datetime import datetime
                    now_iso = datetime.now().isoformat()
                    # Remove the temporary streaming row
                    if self._streaming_row:
                        # Find its position in the layout to replace it
                        idx = self._msg_layout.indexOf(self._streaming_row)
                        self._streaming_row.deleteLater()
                        self._streaming_row = None
                    # Add the final message with proper buttons
                    assist_idx = len(self._messages)
                    self._messages.append({"role": "assistant", "content": final, "created_at": now_iso})
                    # Insert at the same position where the streaming row was, or before the stretch
                    insert_pos = idx if idx >= 0 else self._msg_layout.count() - 1
                    row = _MsgRow(
                        final,
                        "assistant",
                        on_resend=self._regenerate_response,
                        on_delete=lambda idx=assist_idx: self._delete_message(idx),
                        on_rewrite=None,
                        created_at=now_iso
                    )
                    # 自动展开最后一个 LLM Running 块，方便用户直接看到结果
                    for _, title, _ in reversed(row._parse_foldable_blocks(final)):
                        if title is not None and title in row._folded_ids and 'LLM Running' in title:
                            row._folded_ids.remove(title)
                            row._label.setHtml(row._render_with_folds(final))
                            row._adjust_browser_height()
                            break
                    self._msg_layout.insertWidget(insert_pos, row)
                    self._poll_timer.stop()
                    self._set_send_mode()
                    self._streaming_badge.hide()
                    self.last_reply_time = time.time()
                    self._update_token_usage()
                    self._scroll_bottom()
                    self._auto_save()
                    break
        except _queue.Empty:
            pass

    def _add_msg_row(self, role: str, text: str, created_at: str = None, on_delete=None, on_rewrite=None) -> _MsgRow:
        row = _MsgRow(
            text,
            role,
            on_resend=self._regenerate_response if role != "user" else None,
            on_delete=on_delete,
            on_rewrite=on_rewrite,
            created_at=created_at
        )
        self._msg_layout.insertWidget(self._msg_layout.count() - 1, row)
        self._scroll_bottom()
        return row

    def _regenerate_response(self):
        """Resend the last user message to regenerate the assistant response."""
        if self._is_streaming:
            return
        for msg in reversed(self._messages):
            if msg["role"] == "user":
                self._input.setPlainText(msg["content"])
                self._handle_send()
                break

    def _delete_message(self, index: int):
        """Delete the message at the given index."""
        if index < 0 or index >= len(self._messages):
            return
        # Remove from data
        self._messages.pop(index)
        # Rebuild all rows to ensure on_delete indices are correct
        self._rebuild_messages()
        # Update
        self._update_token_usage()
        self._auto_save()

    def _rewrite_message(self, index: int):
        """Rewrite the user message at the given index."""
        if index < 0 or index >= len(self._messages):
            return
        if self._messages[index]["role"] != "user":
            return
        # Get the content and fill it into the input
        content = self._messages[index]["content"]
        self._input.setPlainText(content)
        # Remove this message and everything after it
        self._messages = self._messages[:index]
        # Rebuild UI
        self._rebuild_messages()
        self._update_token_usage()
        self._auto_save()

    def _on_scroll(self, value):
        sb = self._scroll.verticalScrollBar()
        self._user_scrolled_up = value < sb.maximum() - 30
        self._update_nav_visibility()

    def _update_nav_visibility(self):
        sb = self._scroll.verticalScrollBar()
        max_val = sb.maximum()
        vp_h = self._scroll.viewport().height()
        total_h = max_val + vp_h
        show_nav = max_val > 0 and total_h >= vp_h * 1.5

        if show_nav:
            self._nav_widget.show()
            self._nav_up.setEnabled(sb.value() > 2)
            self._nav_down.setEnabled(max_val > 0 and sb.value() < max_val - 2)
        else:
            self._nav_widget.hide()

    def _scroll_to_top(self):
        self._user_scrolled_up = True
        self._scroll.verticalScrollBar().setValue(0)

    def _scroll_to_bottom(self):
        self._user_scrolled_up = False
        QTimer.singleShot(60, lambda: self._scroll.verticalScrollBar().setValue(
            self._scroll.verticalScrollBar().maximum()
        ))

    def _scroll_bottom(self):
        if self._user_scrolled_up:
            return
        QTimer.singleShot(60, lambda: self._scroll.verticalScrollBar().setValue(
            self._scroll.verticalScrollBar().maximum()
        ))

    # ── inject (autonomous mode) ───────────────────────────────────────────────
    def inject_message(self, text: str):
        """Programmatically send a message (called by idle monitor)."""
        self._input.setPlainText(text)
        self._handle_send()

    # ── history ────────────────────────────────────────────────────────────────
    def _refresh_history(self):
        self._history = _load_history()
        self._hist_list.clear()
        for s in reversed(self._history[-20:]):
            n = len(s.get("messages", []))
            item = QListWidgetItem(f"  {s.get('title','未命名')}   ({n} 条)")
            item.setData(Qt.UserRole, s)
            self._hist_list.addItem(item)

    def _restore_selected(self, item=None):
        item = item or self._hist_list.currentItem()
        if not item:
            return
        s = item.data(Qt.UserRole)
        if s:
            self._session = s.copy()
            self._messages = s.get("messages", []).copy()
            self._rebuild_messages()
            self._switch_tab(0)
            self._update_token_usage()
            search_text = self._search_input.text().strip()
            if search_text:
                QTimer.singleShot(50, lambda: self._search_current_chat(search_text))

    def _delete_selected(self):
        item = self._hist_list.currentItem()
        if not item:
            return
        s = item.data(Qt.UserRole)
        if s:
            self._history = [h for h in self._history if h.get("id") != s.get("id")]
            _save_history(self._history)
            self._refresh_history()

    def _rebuild_messages(self):
        while self._msg_layout.count() > 1:
            it = self._msg_layout.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        for i, m in enumerate(self._messages):
            rewrite_cb = (lambda idx=i: self._rewrite_message(idx)) if m["role"] == "user" else None
            self._add_msg_row(
                m["role"],
                m["content"],
                created_at=m.get("created_at"),
                on_delete=lambda idx=i: self._delete_message(idx),
                on_rewrite=rewrite_cb
            )
        self._update_token_usage()

    def _update_token_usage(self):
        in_chars = sum(len(m.get("content", "")) for m in self._messages if m.get("role") == "user")
        out_chars = sum(len(m.get("content", "")) for m in self._messages if m.get("role") == "assistant")
        if getattr(self, "_is_streaming", False) and getattr(self, "_streaming_text", ""):
            out_chars += len(self._streaming_text)
        
        in_tokens = int(in_chars / 2.5)
        out_tokens = int(out_chars / 2.5)
        
        if in_tokens == 0 and out_tokens == 0:
            self._token_lbl.setText("")
        else:
            self._token_lbl.setText(f"|   会话上下文消耗: 入 {in_tokens}  出 {out_tokens} tokens")

    # ── SOP ────────────────────────────────────────────────────────────────────
    def _refresh_sop(self):
        self._sop_list.clear()
        file_icon = _svg_icon("sop_file_item", _SVG_FILE, C["muted"])
        for path in sorted(glob.glob(os.path.join(os.path.dirname(os.path.dirname(__file__)), "memory", "*.md"))):
            name = os.path.basename(path)
            size = os.path.getsize(path)
            it = QListWidgetItem(name)
            it.setIcon(file_icon)
            it.setData(Qt.UserRole, path)
            it.setToolTip(f"{size:,} 字节")
            self._sop_list.addItem(it)

    def _load_sop(self, item):
        if not item:
            return
        path = item.data(Qt.UserRole)
        try:
            with open(path, "r", encoding="utf-8") as f:
                self._sop_viewer.setHtml(_md_to_html(f.read()))
        except Exception as e:
            self._sop_viewer.setPlainText(f"读取失败: {e}")

    # ── settings actions ───────────────────────────────────────────────────────
    def _model_name(self) -> str:
        try:
            return self.runner.get_llm_name()
        except Exception:
            return "未知"

    def _add_system_notice(self, text: str):
        """Insert a small centered notice label (not tracked as a message)."""
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setStyleSheet(
            f"QLabel {{ background: transparent; color: {C['muted']};"
            f" border: none; padding: 6px 20px; font-size: 12px; }}"
        )
        self._msg_layout.insertWidget(self._msg_layout.count() - 1, lbl)
        self._scroll_bottom()

    def _do_stop(self):
        self.runner.abort()
        self._poll_timer.stop()
        self._set_send_mode()
        self._streaming_badge.hide()
        if self._streaming_row:
            self._streaming_row.set_text(self._streaming_text or "（已停止）")
            self._streaming_row.set_finished(True)
            self._streaming_row = None
        self._update_token_usage()

    def _do_reset_prompt(self):
        # ZeroAgent 的 system prompt 管理方式不同，清除缓存标记即可
        try:
            za = self.runner.za
            if hasattr(za.handler, '_last_tool_schemas_hash'):
                za.handler._last_tool_schemas_hash = None
        except Exception:
            pass

    def _auto_save(self):
        if not self._messages:
            return
        if self._session.get("title") == "新对话":
            first_user = next(
                (m["content"] for m in self._messages if m["role"] == "user"), ""
            )
            if first_user:
                self._session["title"] = first_user[:30].replace("\n", " ")
        self._do_save()

    def _do_save(self):
        if not self._messages:
            return
        self._session["messages"] = self._messages.copy()
        self._session["updatedAt"] = datetime.now().isoformat()
        self._history = _load_history()
        for i, s in enumerate(self._history):
            if s.get("id") == self._session["id"]:
                self._history[i] = self._session.copy()
                break
        else:
            self._history.append(self._session.copy())
        _save_history(self._history)

    def _do_clear(self):
        self._messages.clear()
        self._session = {"id": _make_session_id(), "title": "新对话", "messages": []}
        self._rebuild_messages()
        self._switch_tab(0)
        self._update_token_usage()

    def _new_session(self):
        if self._messages:
            self._do_save()
        self._do_clear()

    def _do_toggle_auto(self):
        self.autonomous_enabled = not self.autonomous_enabled
        self._auto_btn.setChecked(self.autonomous_enabled)
        lbl = "暂停自主行动" if self.autonomous_enabled else "开启自主行动 (idle > 30 min 自动触发)"
        self._auto_btn.setText(lbl)

    def _do_trigger_auto(self):
        self.inject_message(
            "[AUTO]🤖 用户触发了自主行动，请阅读自动化sop，选择并执行一项有价值的任务。"
        )

    # ── helpers ────────────────────────────────────────────────────────────────
    @staticmethod
    def _small_btn_style(color: str) -> str:
        return (
            f"QPushButton {{ background: {color}; color: white; border: none;"
            f" border-radius: 7px; padding: 4px 12px; font-size: 12px; font-weight: 600; }}"
            f"QPushButton:hover {{ opacity: 0.85; }}"
        )


# ══════════════════════════════════════════════════════════════════════
# Entry Point
# ══════════════════════════════════════════════════════════════════════

def main():
    # High-DPI support
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("ZeroAgent")

    # Font
    font = QFont()
    # Keep English glyphs in Arial; Chinese falls back to Microsoft YaHei.
    try:
        font.setFamilies(["Arial", "Microsoft YaHei"])
    except Exception:
        font.setFamily("Microsoft YaHei")
    font.setPointSize(10)
    app.setFont(font)

    # ── Config loading ───────────────────────────────────
    config: Optional[AgentConfig] = None
    from zero_agent.core.config import default_config_path, load_default_config
    config_path = str(default_config_path())
    try:
        config = load_default_config()
    except Exception:
        QMessageBox.critical(
            None,
            "未配置 LLM",
            f"未在项目配置文件中发现任何可用的 LLM 接口配置:\n{config_path}\n程序将在无 LLM 模式下运行。",
        )
        config = AgentConfig()

    # ── Agent initialisation ──────────────────────────────
    za = ZeroAgent(config=config)
    runner = AgentRunner(za)

    # ── Windows ───────────────────────────────────────────
    panel = ChatPanel(runner)
    button = FloatingButton(panel)
    button.show()

    # Position panel next to button and show it on first launch
    button._position_panel()
    panel.show()

    scr = QApplication.primaryScreen().availableGeometry()
    print(f"[ZeroAgent] 启动成功")
    print(f"  屏幕分辨率: {scr.width()}x{scr.height()}")
    print(f"  悬浮按钮: ({button.x()}, {button.y()})")
    print(f"  聊天面板: ({panel.x()}, {panel.y()})")
    print(f"  关闭面板后可点击右下角发光按钮重新打开")

    # ── Idle monitor (autonomous mode) ────────────────────
    _last_trigger = 0.0

    def idle_check():
        nonlocal _last_trigger
        if not panel.autonomous_enabled:
            return
        now = time.time()
        if now - _last_trigger < AUTO_COOLDOWN:
            return
        idle = now - panel.last_reply_time
        if idle > AUTO_IDLE_THRESHOLD:
            _last_trigger = now
            panel.inject_message(
                "[AUTO]🤖 用户已经离开超过30分钟，作为自主智能体，请阅读自动化sop，执行自动任务。"
            )

    idle_timer = QTimer()
    idle_timer.timeout.connect(idle_check)
    idle_timer.start(5000)  # check every 5 s

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
