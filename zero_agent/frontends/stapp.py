"""
Streamlit Web 前端 — ZeroAgent 可视化聊天界面.

完全对齐 GenericAgent stapp.py v1 + stapp2.py UX，包括:
- Anthropic 风格主题 (stapp2.py CSS)
- Turn 折叠 (stapp.py fold_turns + st.expander)
- 桌面宠物启动
- 空闲自主行动模式
- 滚动幽灵修复 + IME 组合输入修复
- /new /continue /btw /export 斜杠命令
- 浮动停止按钮
- 字体缩放

用法:
    streamlit run zero_agent/frontends/stapp.py
"""

from __future__ import annotations

import html
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from typing import Any
from urllib.parse import quote
from urllib.request import urlopen

import streamlit as st

try:
    from streamlit import iframe as _st_iframe
    _embed_html = lambda html_content, **kw: _st_iframe(html_content, **{k: max(v, 1) if isinstance(v, int) else v for k, v in kw.items()})
except (ImportError, AttributeError):
    from streamlit.components.v1 import html as _embed_html

st.set_page_config(page_title="ZeroAgent", layout="wide")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ═══════════════════════════════════════════════════════════════
# I18n
# ═══════════════════════════════════════════════════════════════

LANG = os.environ.get("ZA_LANG", "zh")
if LANG not in ("zh", "en"):
    LANG = "zh"
I18N = {
    "zh": {
        "force_stop": "强行停止任务",
        "reinject_tools": "重新注入工具",
        "desktop_pet": "🐱 桌面宠物",
        "find_work": "🎯 给我找点事做",
        "start_auto": "开始空闲自主行动",
        "enable_auto": "▶️ 允许自主行动",
        "disable_auto": "⏸️ 禁止自主行动",
        "auto_running": "🟢 自主行动运行中，会在你离开它30分钟后自动进行",
        "auto_stopped": "🔴 自主行动已停止",
        "stop_gen": "停止生成",
        "new_session": " New Session",
        "export_chat": " Export",
        "settings": "Settings",
        "max_turns": "Max Turns",
        "font_size": "Font Size",
        "llm_backend": "LLM Backend",
        "chat_placeholder": "请输入指令",
        "welcome": "欢迎使用 ZeroAgent~",
        "config_error": "创建 ~/.zero_agent/config.yaml 或设置环境变量 ZA_LLM_PROVIDER / ZA_LLM_API_KEY / ZA_LLM_API_BASE / ZA_LLM_MODEL",
    },
    "en": {
        "force_stop": "Force Stop",
        "reinject_tools": "Reinject Tools",
        "desktop_pet": "🐱 Desktop Pet",
        "find_work": "🎯 Find me something to do",
        "start_auto": "Start Idle Autonomous",
        "enable_auto": "▶️ Enable Autonomous",
        "disable_auto": "⏸️ Disable Autonomous",
        "auto_running": "🟢 Autonomous mode active — triggers after 30min idle",
        "auto_stopped": "🔴 Autonomous mode stopped",
        "stop_gen": "Stop Generation",
        "new_session": " New Session",
        "export_chat": " Export",
        "settings": "Settings",
        "max_turns": "Max Turns",
        "font_size": "Font Size",
        "llm_backend": "LLM Backend",
        "chat_placeholder": "Enter your instruction...",
        "welcome": "Welcome to ZeroAgent~",
        "config_error": "Create ~/.zero_agent/config.yaml or set ZA_LLM_PROVIDER / ZA_LLM_API_KEY / ZA_LLM_API_BASE / ZA_LLM_MODEL env vars",
    },
}


def T(key: str) -> str:
    return I18N.get(LANG, I18N["zh"]).get(key, key)


# ═══════════════════════════════════════════════════════════════
# Anthropic Light Theme CSS
# ═══════════════════════════════════════════════════════════════

THEME_CSS = """
<style>
:root {
    --za-primary: #D4A27F;
    --za-primary-hover: #C4895F;
    --za-bg: #FAF9F6;
    --za-bg-secondary: #EEECE2;
    --za-code-bg: #F4F1EB;
    --za-text: #1A1714;
    --za-text-secondary: #6B6560;
    --za-border: #D5CEC5;
    --za-sidebar-bg: #F0EDE4;
    --za-accent: #CC785C;
}

body, [data-testid="stAppViewContainer"] {
    background-color: var(--za-bg) !important;
    color: var(--za-text) !important;
}
.stApp { background-color: var(--za-bg) !important; }

/* Tight default spacing */
p, li, div[data-testid="stMarkdownContainer"] p {
    line-height: 1.45 !important;
    margin-bottom: 0.35rem !important;
}

[data-testid="stHeader"], header[data-testid="stHeader"] {
    background-color: var(--za-bg) !important;
    border-bottom: 1px solid var(--za-border) !important;
}
[data-testid="stToolbar"] { visibility: hidden !important; }
[data-testid="stDecoration"], #MainMenu { display: none !important; visibility: hidden !important; }
[data-testid="stExpandSidebarButton"], [data-testid="stExpandSidebarButton"] * {
    visibility: visible !important;
}
[data-testid="stToolbar"] div:has([data-testid="stExpandSidebarButton"]) {
    visibility: visible !important;
}
button[data-testid="stExpandSidebarButton"] {
    visibility: visible !important;
    background: #F4F1EA !important; border: none !important;
    color: #3B2F2A !important; border-radius: 10px !important; box-shadow: none !important;
}
button[data-testid="stExpandSidebarButton"]:hover {
    background: #EAE4D9 !important;
}
button[data-testid="stExpandSidebarButton"],
button[data-testid="stExpandSidebarButton"] *,
button[data-testid="stExpandSidebarButton"] [data-testid="stIconMaterial"] {
    color: #3B2F2A !important; fill: #3B2F2A !important; stroke: #3B2F2A !important;
}

[data-testid="stSidebar"], section[data-testid="stSidebar"] {
    background-color: var(--za-sidebar-bg) !important;
    border-right: 1px solid var(--za-border) !important;
}
[data-testid="stSidebar"] .stMarkdown, [data-testid="stSidebar"] p,
[data-testid="stSidebar"] span, [data-testid="stSidebar"] label {
    color: var(--za-text) !important;
}
[data-testid="stSidebar"] hr { border-color: var(--za-border) !important; }

h1, .stTitle, [data-testid="stHeading"] h1 {
    color: var(--za-text) !important; font-weight: 600 !important; letter-spacing: -0.02em !important;
}

.stButton > button {
    background-color: var(--za-bg-secondary) !important;
    color: var(--za-text) !important; border: 1px solid var(--za-border) !important;
    border-radius: 8px !important; padding: 0.4rem 1rem !important;
    font-weight: 500 !important; transition: all 0.2s ease !important;
}
.stButton > button:hover {
    background-color: var(--za-primary) !important; color: white !important;
    border-color: var(--za-primary) !important;
}
.stButton > button[kind="primary"] {
    background-color: var(--za-primary) !important; color: white !important;
    border-color: var(--za-primary) !important;
}
.stButton > button[kind="primary"]:hover {
    background-color: var(--za-primary-hover) !important;
    border-color: var(--za-primary-hover) !important;
}

[data-testid="stChatInput"], [data-testid="stChatInput"] > div {
    background-color: var(--za-bg) !important; border-color: var(--za-border) !important;
}
[data-testid="stChatInput"] { margin-bottom: 12px !important; }
[data-testid="stChatInput"] textarea, [data-testid="stChatInputTextArea"] {
    color: var(--za-text) !important; background-color: var(--za-bg) !important;
    caret-color: #1A1714 !important;
}
[data-testid="stChatInput"] textarea::placeholder {
    color: var(--za-text-secondary) !important; opacity: 0.7 !important;
}
[data-testid="stChatInput"] > div {
    border: 1px solid var(--za-border) !important; border-radius: 12px !important;
    min-height: 48px !important; padding: 0.25rem 0.4rem 0.25rem 0.7rem !important;
    align-items: center !important; gap: 0.4rem !important;
}
[data-testid="stChatInput"] > div:focus-within {
    border-color: var(--za-primary) !important;
    box-shadow: 0 0 0 2px rgba(212, 162, 127, 0.2) !important;
}
[data-testid="stChatInput"] textarea, [data-testid="stChatInputTextArea"] {
    min-height: 1.5rem !important; padding: 0.35rem 0 !important; line-height: 1.5 !important;
}
[data-testid="stChatInput"] button, [data-testid="stChatInputSubmitButton"] {
    background-color: var(--za-primary) !important; color: white !important;
    border-radius: 10px !important; width: 48px !important; height: 48px !important;
    min-width: 48px !important; min-height: 48px !important; padding: 0 !important;
    display: inline-flex !important; align-items: center !important;
    justify-content: center !important; flex-shrink: 0 !important;
}
[data-testid="stChatInput"] button:hover { background-color: var(--za-primary-hover) !important; }

/* ===== Floating stop button ===== */
.stop-btn-anchor { display: none !important; }
[data-testid="stElementContainer"]:has(.stop-btn-anchor) {
    height: 0 !important; min-height: 0 !important; margin: 0 !important;
    padding: 0 !important; overflow: visible !important;
}
[data-testid="stVerticalBlock"]:has(.stop-btn-anchor):not(:has([data-testid="stChatMessage"])) {
    position: fixed !important; bottom: 5.75rem !important; left: 50% !important;
    transform: translateX(-50%) !important; z-index: 1000 !important;
    width: auto !important; background: transparent !important;
    pointer-events: none !important; gap: 0 !important;
}
[data-testid="stVerticalBlock"]:has(.stop-btn-anchor):not(:has([data-testid="stChatMessage"])) > * {
    pointer-events: auto !important;
}
[data-testid="stVerticalBlock"]:has(.stop-btn-anchor):not(:has([data-testid="stChatMessage"])) [data-testid="stButton"] > button {
    border-radius: 999px !important; padding: 0.35rem 1.1rem !important;
    min-height: 2rem !important; font-size: 0.84rem !important;
    font-weight: 500 !important; line-height: 1 !important;
    box-shadow: 0 2px 8px rgba(0,0,0,0.12) !important; white-space: nowrap !important;
}
[data-testid="stVerticalBlock"]:has(.stop-btn-anchor):not(:has([data-testid="stChatMessage"])) [data-testid="stButton"] > button[kind="primary"] {
    background-color: rgba(212, 162, 127, 0.95) !important;
    border-color: rgba(212, 162, 127, 0.95) !important;
}
[data-testid="stVerticalBlock"]:has(.stop-btn-anchor):not(:has([data-testid="stChatMessage"])) [data-testid="stButton"] > button:hover {
    transform: translateY(-1px) !important; box-shadow: 0 3px 12px rgba(0,0,0,0.15) !important;
}

/* ===== Chat messages ===== */
[data-testid="stChatMessage"] {
    background-color: var(--za-bg) !important; border: none !important;
    border-radius: 12px !important; padding: 0.5rem 1rem !important; margin-bottom: 0.3rem !important;
}
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarAssistant"]) {
    background-color: var(--za-bg) !important;
}
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) {
    background-color: var(--za-bg) !important; border: 1px solid var(--za-border) !important;
    border-radius: 12px !important; box-shadow: 0 1px 3px rgba(0, 0, 0, 0.04) !important;
}
[data-testid="stChatMessage"] p, [data-testid="stChatMessage"] .stMarkdown {
    color: var(--za-text) !important; line-height: 1.6 !important;
}

/* ===== Timestamp ===== */
.msg-timestamp {
    text-align: left; font-size: 0.73rem; color: var(--za-text-secondary);
    margin-top: -0.3rem; margin-bottom: 0.2rem; opacity: 0.55;
    font-family: monospace; letter-spacing: 0.02em;
}

/* ===== Avatars ===== */
[data-testid="stChatMessageAvatarContainer"] { width: 36px !important; height: 36px !important; }
[data-testid="stChatMessageAvatarContainer"] > div,
[data-testid*="stChatMessageAvatar"],[data-testid*="chatAvatar"] {
    width: 36px !important; height: 36px !important; border-radius: 50% !important;
    display: flex !important; align-items: center !important;
    justify-content: center !important; overflow: hidden !important;
}
[data-testid*="stChatMessageAvatar"]:has(svg),
[data-testid*="chatAvatar"][data-testid*="user"],
[data-testid*="stChatMessageAvatar"][data-testid*="User"],
[data-testid*="stChatMessageAvatar"][data-testid*="user"] {
    background: linear-gradient(145deg, #D8B08A 0%, #B98259 100%) !important;
    border: 1px solid rgba(150, 102, 67, 0.22) !important;
    box-shadow: inset 0 1px 0 rgba(255,255,255,0.34), 0 2px 6px rgba(104, 76, 54, 0.10) !important;
}
[data-testid*="chatAvatar"][data-testid*="assistant"],
[data-testid*="stChatMessageAvatar"][data-testid*="Assistant"],
[data-testid*="stChatMessageAvatar"][data-testid*="assistant"],
[data-testid="stChatMessageAvatarContainer"] > div {
    background: linear-gradient(145deg, #F6F1E9 0%, #E5D7C7 100%) !important;
    border: 1px solid rgba(187, 165, 141, 0.50) !important;
    box-shadow: inset 0 1px 0 rgba(255,255,255,0.72), 0 2px 6px rgba(104, 76, 54, 0.08) !important;
}

:not(pre) > code {
    background-color: var(--za-code-bg) !important; border: 1px solid var(--za-border) !important;
    border-radius: 4px !important; padding: 0.15em 0.4em !important;
    font-size: 0.9em !important; color: var(--za-text) !important;
}
pre, .stCodeBlock, .stCodeBlock pre {
    background-color: var(--za-code-bg) !important; border: 1px solid var(--za-border) !important;
    border-radius: 8px !important;
}
pre code, .stCodeBlock code, [data-testid="stChatMessage"] pre code,
[data-testid="stChatMessage"] .stCodeBlock code {
    background-color: transparent !important; border: none !important;
    padding: 0 !important; font-size: inherit !important; color: var(--za-text) !important;
}

[data-testid="stToast"] {
    background-color: var(--za-bg-secondary) !important; border: 1px solid var(--za-border) !important;
    border-radius: 8px !important; color: var(--za-text) !important;
    z-index: 999999 !important;
}
/* Prevent toast from being clipped by bottom elements */
[data-testid="stBottomBlockContainer"] {
    background-color: var(--za-bg) !important;
    z-index: 1 !important;
}
[data-testid="stToastContainer"] {
    z-index: 999999 !important;
    bottom: 80px !important;
}
[data-testid="stAlert"] { border-radius: 8px !important; }
.stCaption, [data-testid="stCaptionContainer"] { color: var(--za-text-secondary) !important; }
hr { border-color: var(--za-border) !important; }
a { color: var(--za-accent) !important; }
a:hover { color: var(--za-primary-hover) !important; }
[data-testid="stBottomBlockContainer"] { background-color: var(--za-bg) !important; }

::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: var(--za-bg); }
::-webkit-scrollbar-thumb { background: var(--za-border); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: var(--za-text-secondary); }
</style>
"""

# ═══════════════════════════════════════════════════════════════
# JS Scripts
# ═══════════════════════════════════════════════════════════════

# Auto-scroll: keeps chat scrolled to bottom as new content arrives
AUTOSCROLL_JS = """<script>
(function(){
var p=window.parent,d=p.document,ticking=false;
function scroll(){var c=d.querySelector('[data-testid="stAppViewContainer"]')||d.querySelector('.stApp');if(c)c.scrollTop=c.scrollHeight;}
var s=d.createElement('style');s.textContent='img,video,audio,iframe,canvas,object,embed{max-height:none!important;height:auto!important;min-height:auto!important}';d.head.appendChild(s);
new MutationObserver(function(){if(!ticking){ticking=true;requestAnimationFrame(function(){scroll();ticking=false;})}}).observe(d.querySelector('#root')||d.querySelector('.stApp')||d.body,{childList:true,subtree:true,characterData:true});
setTimeout(scroll,300);setTimeout(scroll,800);
})();
</script>"""

# Scroll-height ghost fix: prevents phantom scroll from expander open/close animations
SCROLL_GHOST_FIX_JS = """<script>
!function(){var p=window.parent;if(p.__sfx2)return;p.__sfx2=1;var d=p.document;
var pending=0;
function f(){pending=0;var m=d.querySelector('section.main');if(!m)return;
var s=m.scrollTop,h=m.scrollHeight;
m.style.minHeight=h+1+'px';void m.offsetHeight;
m.style.minHeight='';void m.offsetHeight;
m.scrollTop=s}
function schedule(){if(!pending){pending=1;requestAnimationFrame(f)}}
d.addEventListener('transitionend',function(e){
e.target.closest&&e.target.closest('details')&&setTimeout(schedule,60)},!0);
new MutationObserver(function(){setTimeout(schedule,80)})
.observe(d.body,{subtree:1,attributes:1,attributeFilter:['open']})}()
</script>"""

# IME fix: prevents Enter from submitting during CJK IME composition (macOS only)
IME_FIX_JS = "" if sys.platform == "win32" else """<script>
!function(){if(window.parent.__imeFix)return;window.parent.__imeFix=1;
var d=window.parent.document,c=0;
d.addEventListener('compositionstart',function(){c=1},!0);
d.addEventListener('compositionend',function(){c=0},!0);
function f(){d.querySelectorAll('textarea[data-testid=stChatInputTextArea]')
.forEach(function(t){t.__imeFix||(t.__imeFix=1,t.addEventListener('keydown',function(e){
e.key==='Enter'&&!e.shiftKey&&(e.isComposing||c||e.keyCode===229)&&
(e.stopImmediatePropagation(),e.preventDefault())},!0))})}
f();new MutationObserver(f).observe(d.body,{childList:1,subtree:1})}()
</script>"""


# ═══════════════════════════════════════════════════════════════
# Agent Factory
# ═══════════════════════════════════════════════════════════════


def _create_agent() -> Any:
    """创建 ZeroAgent + AgentRunner 实例."""
    from zero_agent.adapters.agent_runner import AgentRunner
    from zero_agent.core.agent import ZeroAgent
    from zero_agent.core.config import AgentConfig
    from zero_agent.bots.shared.continue_cmd import set_sessions_dir

    config_path = os.path.join(os.path.expanduser("~"), ".zero_agent", "config.yaml")
    if os.path.isfile(config_path):
        config = AgentConfig.from_yaml(config_path)
    else:
        config = AgentConfig.from_env()

    set_sessions_dir(os.path.abspath(config.sessions_dir))

    za = ZeroAgent(config=config)
    za.handler.max_turns = config.max_turns
    return AgentRunner(za)


@st.cache_resource
def _get_runner() -> Any:
    return _create_agent()


# ═══════════════════════════════════════════════════════════════
# Session State
# ═══════════════════════════════════════════════════════════════


def _init_session() -> None:
    defaults: dict = {
        "messages": [],
        "streaming": False,
        "display_queue": None,
        "partial_response": "",
        "reply_ts": "",
        "current_prompt": "",
        "font_scale": 100.0,
        "last_reply_time": 0,
        "autonomous_enabled": False,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


# ═══════════════════════════════════════════════════════════════
# Font Scale
# ═══════════════════════════════════════════════════════════════


def _build_font_css(scale: float) -> str:
    pct = max(100.0, min(200.0, float(scale)))
    rem = pct / 100.0
    return f"""
<style id="za-font-scale">
:root,html,body,[data-testid="stAppViewContainer"],.stApp{{font-size:{pct:.1f}%!important}}
body,[data-testid="stAppViewContainer"],.stApp{{--za-font-scale:{rem:.3f}}}
[data-testid="stAppViewContainer"],.stApp,.stApp p,.stApp li,.stApp label,
.stApp div[data-testid="stMarkdownContainer"],.stApp textarea,.stApp input,
.stApp button,.stApp [data-testid="stChatMessageContent"],.stApp .stCaption{{
font-size:calc(1rem*var(--za-font-scale,1))!important}}
</style>
"""


# ═══════════════════════════════════════════════════════════════
# Turn Folding (aligned with GenericAgent stapp.py)
# ═══════════════════════════════════════════════════════════════

_SUMMARY_TAG_RE = re.compile(r"<summary>.*?</summary>\s*", re.DOTALL)


def fold_turns(text: str) -> list[dict]:
    """按 Turn 标记拆分响应，旧 turn 折叠为 expander 标题.

    Returns:
        [{"type":"text","content":...}, {"type":"fold","title":...,"content":...}]
    """
    # 保护 4+ 反引号块（子 agent 嵌套的 LLM Running 标记）
    _ph: list[str] = []
    safe = re.sub(
        r"`{4,}.*?`{4,}",
        lambda m: (_ph.append(m.group(0)), f"\x00PH{len(_ph)-1}\x00")[1],
        text, flags=re.DOTALL,
    )
    safe = re.sub(
        r"`{4,}[^`].*$",
        lambda m: (_ph.append(m.group(0)), f"\x00PH{len(_ph)-1}\x00")[1],
        safe, flags=re.DOTALL,
    )

    parts = re.split(r"(\**LLM Running \(Turn \d+\) \.\.\.\*\**)", safe)
    parts = [re.sub(r"\x00PH(\d+)\x00", lambda m: _ph[int(m.group(1))], p) for p in parts]

    if len(parts) < 4:
        return [{"type": "text", "content": text}]

    segments: list[dict] = []
    if parts[0].strip():
        segments.append({"type": "text", "content": parts[0]})

    turns: list[tuple[str, str]] = []
    for i in range(1, len(parts), 2):
        marker = parts[i]
        content = parts[i + 1] if i + 1 < len(parts) else ""
        turns.append((marker, content))

    for idx, (marker, content) in enumerate(turns):
        if idx < len(turns) - 1:
            # Earlier turns: collapse into expander
            _c = re.sub(
                r"`{3,}.*?`{3,}|<thinking>.*?</thinking>",
                "", content, flags=re.DOTALL,
            )
            matches = re.findall(
                r"<summary>\s*((?:(?!<summary>).)*?)\s*</summary>",
                _c, re.DOTALL,
            )
            if matches:
                title = matches[0].strip().split("\n")[0]
                if len(title) > 50:
                    title = title[:50] + "..."
            else:
                _plain = _c.strip().split("\n", 1)[0]
                title = (_plain[:50] + "...") if len(_plain) > 50 else (_plain or marker.strip("*"))
            segments.append({"type": "fold", "title": title, "content": content})
        else:
            # Latest turn: expanded
            segments.append({"type": "text", "content": marker + content})

    return segments


def render_segments(segments: list[dict], suffix: str = "") -> None:
    """渲染 turn 分段：已完成的折叠，最新的展开."""
    for seg in segments:
        if seg["type"] == "fold":
            with st.expander(seg["title"], expanded=False):
                st.markdown(seg["content"])
        else:
            st.markdown(seg["content"] + suffix)


# ═══════════════════════════════════════════════════════════════
# Streaming Engine (generator-based, aligned with GenericAgent)
# ═══════════════════════════════════════════════════════════════


def agent_backend_stream(prompt: str | None = None):
    """Generator: drains display_queue, yielding accumulated full response text.

    - prompt given: starts a new task via runner.put_task()
    - prompt None: resumes draining the existing display_queue in session_state
    """
    runner = _get_runner()
    if prompt is not None:
        st.session_state.display_queue = runner.put_task(prompt, source="user")
        st.session_state.partial_response = ""
    dq = st.session_state.get("display_queue")
    if dq is None:
        return

    response = re.sub(
        r"\**LLM Running \(Turn \d+\) \.\.\.\**\s*$",
        "", st.session_state.get("partial_response", ""),
    ).rstrip()

    try:
        while True:
            try:
                item = dq.get(timeout=1)
            except queue.Empty:
                yield response  # heartbeat
                continue
            if "next" in item:
                response = item["next"]
                st.session_state.partial_response = response
                yield response
            if "done" in item:
                st.session_state.display_queue = None
                st.session_state.partial_response = ""
                yield item["done"]
                break
    finally:
        runner.abort()
        try:
            st.session_state.display_queue = None
            st.session_state.partial_response = ""
        except BaseException:
            pass


def render_main_stream(prompt: str | None = None) -> None:
    """Render the assistant bubble for the main task with turn folding."""
    with st.chat_message("assistant"):
        frozen = 0
        live = st.empty()
        response = ""
        CURSOR = " ▌"
        for response in agent_backend_stream(prompt):
            segs = fold_turns(response)
            n_done = max(0, len(segs) - 1)
            while frozen < n_done:
                with live.container():
                    render_segments([segs[frozen]])
                live = st.empty()
                frozen += 1
            with live.container():
                render_segments([segs[-1]], suffix=CURSOR)

        # Final render without cursor
        segs = fold_turns(response)
        for i in range(frozen, len(segs)):
            with live.container():
                render_segments([segs[i]])
            if i < len(segs) - 1:
                live = st.empty()

    if response:
        st.session_state.messages.append({"role": "assistant", "content": response})
        st.session_state.last_reply_time = int(time.time())


# ═══════════════════════════════════════════════════════════════
# Slash Commands
# ═══════════════════════════════════════════════════════════════


def _handle_slash_cmd(runner: Any, cmd: str, ts: str) -> bool:
    """处理斜杠命令，返回 True 表示已处理并需要 rerun."""
    cmd = cmd.strip()

    if cmd == "/new":
        from zero_agent.bots.shared.continue_cmd import reset_conversation
        runner.za.client.history = []
        runner.za.client.system = ""
        runner.za.handler.working = {}
        runner.za.handler.history_info = []
        runner.za.handler._empty_ct = 0
        st.session_state.messages = [
            {"role": "assistant", "content": reset_conversation(runner.za), "time": ts}
        ]
        _reset_and_rerun()
        return True

    if cmd.startswith("/continue"):
        from zero_agent.bots.shared.continue_cmd import (
            list_sessions, handle_frontend_command, extract_ui_messages,
        )
        m = re.match(r"/continue\s+(\d+)\s*$", cmd)
        sessions = list_sessions(exclude_pid=os.getpid()) if m else []
        idx = int(m.group(1)) - 1 if m else -1
        target = sessions[idx][0] if 0 <= idx < len(sessions) else None
        result = handle_frontend_command(runner.za, cmd)
        history = extract_ui_messages(target) if target and "成功" in result else None
        tail = [{"role": "assistant", "content": result, "time": ts}]
        if history:
            st.session_state.messages = history + tail
        else:
            st.session_state.messages = list(st.session_state.messages) + [
                {"role": "user", "content": cmd, "time": ts},
            ] + tail
        _reset_and_rerun()
        return True

    if cmd.startswith("/btw"):
        from zero_agent.bots.shared.btw_cmd import handle_frontend_command as btw_handle
        answer = btw_handle(runner.za, cmd)
        st.session_state.messages = list(st.session_state.messages) + [
            {"role": "user", "content": cmd, "time": ts},
            {"role": "assistant", "content": answer, "time": ts},
        ]
        st.rerun()
        return True

    if cmd.startswith("/export"):
        from zero_agent.bots.shared.export_cmd import (
            last_assistant_text, export_to_temp, wrap_for_clipboard,
        )
        parts = cmd.split(maxsplit=1)
        sub = parts[1].strip() if len(parts) > 1 else ""
        sub_lower = sub.lower()
        if not sub:
            result = (
                "**选择导出方式：**\n\n"
                "- `/export clip` — 整理到代码块中\n"
                "- `/export <文件名>` — 导出到 `temp/<文件名>`（默认 .md 后缀）\n"
                "- `/export all` — 显示完整对话日志路径"
            )
        elif sub_lower == "all":
            log_dir = getattr(runner.za.config, "log_dir", None)
            result = f"📂 日志目录: `{log_dir}`" if log_dir else "❌ 未配置日志目录"
        else:
            text = last_assistant_text(runner.za)
            if not text:
                result = "❌ 还没有模型回复可导出"
            elif sub_lower in ("clip", "copy"):
                result = f"📋 最后一轮回复（点代码块右上角 📋 复制）:\n\n{wrap_for_clipboard(text)}"
            else:
                try:
                    path = export_to_temp(text, sub)
                    result = f"✅ 已导出:\n\n`{path}`"
                except Exception as e:
                    result = f"❌ 导出失败: {e}"
        st.session_state.messages = list(st.session_state.messages) + [
            {"role": "user", "content": cmd, "time": ts},
            {"role": "assistant", "content": result, "time": ts},
        ]
        _reset_and_rerun()
        return True

    return False


def _reset_and_rerun() -> None:
    """清理流式状态并 rerun."""
    st.session_state.streaming = False
    st.session_state.display_queue = None
    st.session_state.partial_response = ""
    st.session_state.reply_ts = ""
    st.session_state.current_prompt = ""
    st.session_state.last_reply_time = int(time.time())
    st.rerun()


# ═══════════════════════════════════════════════════════════════
# Session History (backed by sessions_dir model_responses logs)
# ═══════════════════════════════════════════════════════════════


def _render_session_history(runner) -> None:
    """Render session history browser in the sidebar."""
    from zero_agent.bots.shared.continue_cmd import (
        list_sessions, _rel_time, extract_ui_messages, handle_frontend_command,
    )

    st.caption("Session History")

    sessions = list_sessions(exclude_pid=os.getpid())
    if not sessions:
        st.caption("No historical sessions yet")
        return

    if st.button("Refresh", key="refresh_sessions", use_container_width=True):
        st.rerun()

    for i, (path, mtime, preview, n_rounds) in enumerate(sessions[:20]):
        rel = _rel_time(mtime)
        preview_text = (preview or "(empty)").replace("\n", " ")[:50]
        label = f"{i+1}. {preview_text} · {n_rounds}轮 · {rel}"

        if st.button(label, key=f"load_session_{i}", use_container_width=True):
            result = handle_frontend_command(runner, f"/continue {i + 1}")
            ui_msgs = extract_ui_messages(path) if "成功" in result else None
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            if ui_msgs:
                st.session_state.messages = ui_msgs + [
                    {"role": "assistant", "content": result, "time": ts}
                ]
            else:
                st.session_state.messages = list(st.session_state.messages) + [
                    {"role": "assistant", "content": result, "time": ts}
                ]
            st.session_state.streaming = False
            st.session_state.display_queue = None
            st.session_state.partial_response = ""
            st.rerun()


# ═══════════════════════════════════════════════════════════════
# Sidebar
# ═══════════════════════════════════════════════════════════════


@st.fragment
def _render_sidebar(runner: Any) -> None:
    """渲染侧边栏."""
    st.header(T("settings"))

    # LLM Backend selector
    try:
        backends = runner.list_backends()
        backend_names = [b[0] for b in backends]
        active_idx = next((i for i, b in enumerate(backends) if b[2]), 0)
        st.caption(f"Model: {runner.get_model_name()}")
        selected = st.selectbox(
            T("llm_backend"),
            backend_names,
            index=active_idx,
        )
        if selected != backend_names[active_idx]:
            runner.switch_backend(selected)
            st.rerun()
    except Exception:
        st.text("No backends available")

    st.divider()

    # Force Stop
    if st.button(T("force_stop")):
        runner.abort()
        st.toast("Stop signal sent")
        st.rerun()

    # Reinject Tools
    if st.button(T("reinject_tools")):
        runner.za.client.last_tools = ""
        st.toast("Tools will be re-injected next turn")

    # Desktop Pet
    if st.button(T("desktop_pet")):
        kwargs = {"creationflags": 0x08} if sys.platform == "win32" else {}
        pet_script = os.path.join(SCRIPT_DIR, "desktop_pet.pyw")
        if not os.path.exists(pet_script):
            pet_script = os.path.join(SCRIPT_DIR, "desktop_pet_basic.pyw")
        subprocess.Popen([sys.executable, pet_script], **kwargs)

        def _pet_req(q: str) -> None:
            def _do() -> None:
                try:
                    urlopen(f"http://127.0.0.1:41983/?{q}", timeout=2)
                except Exception:
                    pass
            threading.Thread(target=_do, daemon=True).start()

        runner._pet_req = _pet_req
        if not hasattr(runner, "_turn_end_hooks"):
            runner._turn_end_hooks = {}

        def _pet_hook(ctx: dict) -> None:
            parts = [f"Turn {ctx.get('turn','?')}"]
            if ctx.get("summary"):
                parts.append(ctx["summary"])
            if ctx.get("exit_reason"):
                parts.append("DONE")
            _pet_req(f"msg={quote(chr(10).join(parts))}")
            if ctx.get("exit_reason"):
                _pet_req("state=idle")
        runner._turn_end_hooks["pet"] = _pet_hook
        st.toast("Desktop pet started")

    st.divider()

    # Session controls
    col1, col2 = st.columns(2)
    with col1:
        if st.button(T("new_session"), use_container_width=True):
            from zero_agent.bots.shared.continue_cmd import _snapshot_current_log
            _snapshot_current_log()
            runner.za.client.history = []
            runner.za.client.system = ""
            runner.za.handler.working = {}
            runner.za.handler.history_info = []
            runner.za.handler._empty_ct = 0
            st.session_state.messages = []
            st.session_state.streaming = False
            st.session_state.display_queue = None
            st.session_state.partial_response = ""
            st.rerun()
    with col2:
        if st.button(T("export_chat"), use_container_width=True):
            _render_export()

    st.divider()

    # Session History
    _render_session_history(runner)

    st.divider()
    st.caption(f"Tools: {len(runner.za.registry.list_all())}")

    # ——— Autonomous Mode ———
    if LANG == "zh":
        if st.button(T("find_work")):
            st.session_state["_inject_prompt"] = (
                "按照自主行动的规划部分，充分分析我的情况，给我生成一批TODO，务必让我感兴趣"
            )
            st.rerun()

        st.divider()

        if st.button(T("start_auto")):
            st.session_state.last_reply_time = int(time.time()) - 1800
            st.session_state.autonomous_enabled = True
            st.toast("已将上次回复时间设为1800秒前，自主行动已激活")
            st.rerun()

        if st.session_state.autonomous_enabled:
            if st.button(T("disable_auto")):
                st.session_state.autonomous_enabled = False
                st.toast("⏸️ 已禁止自主行动")
                st.rerun()
            st.caption(T("auto_running"))
        else:
            if st.button(T("enable_auto"), type="primary"):
                st.session_state.autonomous_enabled = True
                st.toast("✅ 已允许自主行动")
                st.rerun()
            st.caption(T("auto_stopped"))


def _render_export() -> None:
    """导出聊天历史为 Markdown 下载."""
    lines: list[str] = []
    for msg in st.session_state.messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        ts = msg.get("time", "")
        lines.append(f"### [{role}] {ts}")
        lines.append("")
        lines.append(content)
        lines.append("")
    md = "\n".join(lines)
    st.download_button(
        "Download Chat",
        data=md,
        file_name=f"zero-agent-chat-{datetime.now().strftime('%Y%m%d_%H%M%S')}.md",
        mime="text/markdown",
        use_container_width=True,
    )


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════


def main() -> None:
    _init_session()

    # Inject theme + JS
    st.markdown(THEME_CSS, unsafe_allow_html=True)
    st.markdown(_build_font_css(st.session_state.font_scale), unsafe_allow_html=True)
    _embed_html(AUTOSCROLL_JS, height=0, width=0)
    _embed_html(SCROLL_GHOST_FIX_JS, height=0, width=0)
    _embed_html(IME_FIX_JS, height=0, width=0)

    # Header
    st.title("ZeroAgent")
    st.caption("Clean & reusable autonomous agent framework")

    try:
        runner = _get_runner()
    except Exception as e:
        st.error(f"Failed to create agent: {e}")
        st.info(T("config_error"))
        return

    # Sidebar
    with st.sidebar:
        _render_sidebar(runner)

    # Welcome
    if not st.session_state.messages:
        with st.chat_message("assistant"):
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            st.markdown(f'<div class="msg-timestamp">{ts}</div>', unsafe_allow_html=True)
            st.write(T("welcome"))

    # Render message history with turn folding
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            slot = st.empty()
            with slot.container():
                if msg["role"] == "assistant":
                    render_segments(fold_turns(msg["content"]))
                else:
                    st.markdown(msg["content"])

    # Handle injected prompts
    _injected = st.session_state.pop("_inject_prompt", None)
    if _injected:
        prompt = _injected
    else:
        prompt = st.chat_input(T("chat_placeholder"), disabled=st.session_state.streaming)

    if prompt:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        cmd = (prompt or "").strip()

        # Slash commands
        if cmd.startswith("/"):
            if _handle_slash_cmd(runner, cmd, ts):
                return  # handler already triggered rerun

        # Regular prompt
        st.session_state.messages.append({"role": "user", "content": prompt})
        if hasattr(runner, "_pet_req"):
            runner._pet_req("state=walk")

        with st.chat_message("user"):
            st.markdown(prompt)

        render_main_stream(prompt)

    elif st.session_state.get("display_queue") is not None:
        # No new prompt, but mid-flight task (e.g. after /btw rerun) — resume drain
        render_main_stream()

    # Hidden element for idle monitor (launch.pyw reads this)
    if st.session_state.autonomous_enabled:
        st.markdown(
            f"""<div id="last-reply-time" style="display:none">{st.session_state.get('last_reply_time', int(time.time()))}</div>""",
            unsafe_allow_html=True,
        )


if __name__ == "__main__":
    main()
