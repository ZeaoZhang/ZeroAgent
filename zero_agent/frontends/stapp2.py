import os, sys
import html
if sys.stdout is None: sys.stdout = open(os.devnull, "w")
if sys.stderr is None: sys.stderr = open(os.devnull, "w")
try: sys.stdout.reconfigure(errors='replace')
except: pass
try: sys.stderr.reconfigure(errors='replace')
except: pass
import streamlit as st
try:
    from streamlit import iframe as _st_iframe  # 1.56+
    _embed_html = lambda html, **kw: _st_iframe(html, **{k: max(v, 1) if isinstance(v, int) else v for k, v in kw.items()})
except (ImportError, AttributeError):
    from streamlit.components.v1 import html as _embed_html  # ≤1.55
import time, json, re, threading, queue
from datetime import datetime
from zero_agent.core.agent import ZeroAgent
from zero_agent.adapters.agent_runner import AgentRunner

st.set_page_config(page_title="Cowork", layout="wide")

# ─── Anthropic Light Theme CSS ───
from zero_agent.frontends.themes import load_theme_css, theme_toggle_js

ANTHROPIC_SELECTBOX_SCRIPT = """
<div></div>
<script>
(function() {
    const hostWin = window.parent;
    const doc = hostWin.document;
    const LABEL_TEXT = '备用链路';
    const EXTRA_WIDTH = 56;
    const TIMER_KEY = '__anthropicSelectboxFixedWidthTimer';
    const FONT_LABELS = {
        '100': '标准（100%）',
        '112.5': '偏大（112.5%）',
        '125': '更大（125%）',
        '137.5': '超大（137.5%）'
    };

    function measureTextWidth(text, sourceEl) {
        const canvas = hostWin.__anthropicSelectboxMeasureCanvas || (hostWin.__anthropicSelectboxMeasureCanvas = doc.createElement('canvas'));
        const ctx = canvas.getContext('2d');
        const style = sourceEl ? hostWin.getComputedStyle(sourceEl) : null;
        const font = style ? `${style.fontWeight} ${style.fontSize} ${style.fontFamily}` : '400 14px sans-serif';
        ctx.font = font;
        return Math.ceil(ctx.measureText(text || '').width);
    }

    function ensureSidebarSettingsTitle() {
        const sidebar = doc.querySelector('[data-testid="stSidebar"]');
        if (!sidebar) return;
        const collapseBtn = sidebar.querySelector('button[kind="header"], [data-testid="stSidebarCollapseButton"] button, [data-testid="stSidebarCollapseButton"]');
        if (!collapseBtn || !collapseBtn.parentElement) return;
        let title = doc.getElementById('custom-sidebar-settings-title');
        if (!title) {
            title = doc.createElement('span');
            title.id = 'custom-sidebar-settings-title';
            title.textContent = '设置';
            title.style.cssText = 'font-size:14px;font-weight:600;color:rgb(38,39,48);margin-right:8px;line-height:1;display:inline-flex;align-items:center;white-space:nowrap;';
        }
        if (collapseBtn.previousElementSibling !== title) {
            collapseBtn.parentElement.insertBefore(title, collapseBtn);
        }
    }

    function applyLiveFontPreview() {
        const sidebar = doc.querySelector('[data-testid="stSidebar"]');
        if (!sidebar) return;
        const sliderLabel = Array.from(sidebar.querySelectorAll('label, p')).find((el) => el.textContent && el.textContent.trim() === '字体大小');
        if (!sliderLabel) return;
        const container = sliderLabel.closest('[data-testid="stWidgetLabel"]')?.parentElement?.parentElement || sliderLabel.closest('[data-testid="stSlider"]') || sliderLabel.closest('div');
        if (!container) return;
        const input = container.querySelector('input[type="range"]');
        if (!input) return;
        const caption = container.querySelector('[data-testid="stCaptionContainer"] p, p');

        const updateFont = () => {
            const raw = parseFloat(input.value);
            if (!Number.isFinite(raw)) return;
            doc.documentElement.style.setProperty('font-size', raw + '%', 'important');
            if (caption) {
                const key = String(raw % 1 === 0 ? raw.toFixed(0) : raw);
                caption.textContent = FONT_LABELS[key] || `${raw.toFixed(1)}%`;
            }
        };

        if (input.dataset.liveFontBound !== '1') {
            input.addEventListener('input', updateFont);
            input.addEventListener('change', updateFont);
            input.dataset.liveFontBound = '1';
        }
        updateFont();
    }

    function applyFixedWidth() {
        const sidebar = doc.querySelector('[data-testid="stSidebar"]');
        if (!sidebar) return;
        const boxes = sidebar.querySelectorAll('[data-testid="stSelectbox"]');
        boxes.forEach((box) => {
            const labelNode = box.querySelector('label [data-testid="stMarkdownContainer"] p, label p');
            if (!labelNode || labelNode.textContent.trim() !== LABEL_TEXT) return;
            const selectRoot = box.querySelector('[data-baseweb="select"]');
            const trigger = selectRoot && selectRoot.firstElementChild;
            const maxLabelNode = box.querySelector('[data-testid="sidebar-llm-max-label"]');
            const text = ((maxLabelNode && maxLabelNode.textContent) || '').trim();
            if (!selectRoot || !trigger || !text) return;

            const textWidth = measureTextWidth(text, trigger);
            const targetWidth = Math.min(sidebar.clientWidth - 32, Math.max(96, textWidth + EXTRA_WIDTH));
            const valueWrap = trigger.firstElementChild;
            const arrowWrap = valueWrap && valueWrap.nextElementSibling;
            const valueNode = valueWrap && valueWrap.querySelector('[value]');

            box.style.setProperty('width', targetWidth + 'px', 'important');
            box.style.setProperty('max-width', targetWidth + 'px', 'important');
            box.style.setProperty('flex', '0 0 ' + targetWidth + 'px', 'important');

            selectRoot.style.setProperty('width', targetWidth + 'px', 'important');
            selectRoot.style.setProperty('min-width', targetWidth + 'px', 'important');
            selectRoot.style.setProperty('max-width', targetWidth + 'px', 'important');

            trigger.style.setProperty('width', targetWidth + 'px', 'important');
            trigger.style.setProperty('min-width', targetWidth + 'px', 'important');
            trigger.style.setProperty('max-width', targetWidth + 'px', 'important');
            trigger.style.setProperty('padding-right', '0px', 'important');
            trigger.style.setProperty('justify-content', 'flex-start', 'important');
            trigger.style.setProperty('box-sizing', 'border-box', 'important');

            if (valueWrap) {
                valueWrap.style.setProperty('flex', '1 1 auto', 'important');
                valueWrap.style.setProperty('min-width', '0px', 'important');
                valueWrap.style.setProperty('max-width', 'calc(100% - 24px)', 'important');
                valueWrap.style.setProperty('padding-right', '4px', 'important');
            }
            if (valueNode) {
                valueNode.style.setProperty('max-width', '100%', 'important');
            }
            if (arrowWrap) {
                arrowWrap.style.setProperty('margin-left', 'auto', 'important');
                arrowWrap.style.setProperty('padding-right', '0px', 'important');
                arrowWrap.style.setProperty('width', '24px', 'important');
                arrowWrap.style.setProperty('min-width', '24px', 'important');
                arrowWrap.style.setProperty('display', 'flex', 'important');
                arrowWrap.style.setProperty('justify-content', 'flex-end', 'important');
                arrowWrap.style.setProperty('align-items', 'center', 'important');
                arrowWrap.style.setProperty('overflow', 'visible', 'important');
            }
        });
        ensureSidebarSettingsTitle();
        applyLiveFontPreview();
    }

    if (hostWin[TIMER_KEY]) {
        hostWin.clearInterval(hostWin[TIMER_KEY]);
    }
    hostWin[TIMER_KEY] = hostWin.setInterval(applyFixedWidth, 300);
    hostWin.setTimeout(applyFixedWidth, 60);
    hostWin.setTimeout(applyFixedWidth, 300);
    hostWin.setTimeout(applyFixedWidth, 1000);
    applyFixedWidth();
})();
</script>
"""

@st.cache_resource
def init():
    za = ZeroAgent()
    runner = AgentRunner(za)
    if runner.llmclient is None:
        st.error("⚠️ 未配置任何可用的 LLM 接口，请检查 ~/.zero_agent/config.yaml 或环境变量后重启。")
        st.stop()
    return runner


def build_dynamic_font_css(scale_percent: float) -> str:
    root_percent = max(100.0, min(200.0, float(scale_percent)))
    rem_scale = root_percent / 100.0
    return f"""
<style id="dynamic-font-scale-style">
:root, html, body, [data-testid="stAppViewContainer"], .stApp {{
    font-size: {root_percent:.1f}% !important;
}}
body, [data-testid="stAppViewContainer"], .stApp {{
    --app-font-scale: {rem_scale:.3f};
}}
[data-testid="stAppViewContainer"], .stApp, .stApp p, .stApp li, .stApp label,
.stApp div[data-testid="stMarkdownContainer"], .stApp textarea, .stApp input,
.stApp button, .stApp [data-testid="stChatMessageContent"], .stApp .stCaption {{
    font-size: calc(1rem * var(--app-font-scale, 1)) !important;
}}
</style>
"""


def build_dynamic_font_update_script(scale_percent: float) -> str:
    css = json.dumps(build_dynamic_font_css(scale_percent))
    return f"""
<script>
(() => {{
    const cssText = {css};
    const parser = new DOMParser();
    const parsed = parser.parseFromString(cssText, 'text/html');
    const nextStyle = parsed.querySelector('#dynamic-font-scale-style');
    if (!nextStyle) return;
    const hostDoc = window.parent && window.parent.document ? window.parent.document : document;
    const existing = hostDoc.querySelector('#dynamic-font-scale-style');
    if (existing) {{
        existing.textContent = nextStyle.textContent;
    }} else {{
        hostDoc.head.appendChild(nextStyle);
    }}
}})();
</script>
"""


def build_header_agent_badge_script() -> str:
    return """
<script>
(() => {
    const hostWin = window.parent || window;
    const hostDoc = hostWin.document || document;
    const BADGE_ID = 'generic-agent-header-badge';
    const STYLE_ID = 'generic-agent-header-badge-style';

    const ensureStyle = () => {
        if (hostDoc.getElementById(STYLE_ID)) return;
        const style = hostDoc.createElement('style');
        style.id = STYLE_ID;
        style.textContent = `
            #${BADGE_ID} {
                position: absolute;
                left: 50%;
                top: 50%;
                transform: translate(-50%, -50%);
                display: inline-flex;
                align-items: center;
                justify-content: center;
                white-space: nowrap;
                font-size: 2.75rem;
                font-weight: 600;
                line-height: 1.2;
                color: #000000;
                padding: 0;
                border-radius: 0;
                background: transparent;
                border: none;
                box-shadow: none;
                pointer-events: none;
                z-index: 20;
            }
        `;
        hostDoc.head.appendChild(style);
    };

    const findHeaderRoot = () => {
        const candidates = [
            'header[data-testid="stHeader"]',
            '[data-testid="stHeader"]',
            'header',
        ];
        for (const selector of candidates) {
            const root = hostDoc.querySelector(selector);
            if (root) return root;
        }
        return null;
    };

    const ensureBadge = () => {
        ensureStyle();
        const headerRoot = findHeaderRoot();
        if (!headerRoot) return;
        headerRoot.style.position = 'relative';

        let badge = hostDoc.getElementById(BADGE_ID);
        if (!badge) {
            badge = hostDoc.createElement('div');
            badge.id = BADGE_ID;
            badge.textContent = 'Generic Agent';
        }
        if (badge.parentElement !== headerRoot) {
            headerRoot.appendChild(badge);
        }

        const titleEl = hostDoc.querySelector('h1');
        if (titleEl) {
            const titleStyle = hostWin.getComputedStyle(titleEl);
            badge.style.fontSize = titleStyle.fontSize;
            badge.style.fontWeight = titleStyle.fontWeight;
            badge.style.lineHeight = titleStyle.lineHeight;
            badge.style.fontFamily = titleStyle.fontFamily;
            badge.style.letterSpacing = titleStyle.letterSpacing;
            badge.style.color = '#000000';
        }
    };

    if (hostWin.__genericAgentHeaderBadgeTimer) {
        hostWin.clearInterval(hostWin.__genericAgentHeaderBadgeTimer);
    }
    hostWin.__genericAgentHeaderBadgeTimer = hostWin.setInterval(ensureBadge, 500);
    hostWin.setTimeout(ensureBadge, 80);
    hostWin.setTimeout(ensureBadge, 400);
    ensureBadge();
})();
</script>
"""

agent = init()

def init_session_state():
    for key, value in {
        'agent_name': 'GenericAgent', 'streaming': False, 'stopping': False, 'display_queue': None,
        'partial_response': '', 'reply_ts': '', 'current_prompt': '', 'selected_llm_idx': agent.llm_no,
        'autonomous_enabled': False, 'messages': [], 'theme': 'light',
    }.items(): st.session_state.setdefault(key, value)

init_session_state()

# Inject theme CSS + toggle JS
st.markdown(load_theme_css(st.session_state.get("theme", "light"), "stapp2"), unsafe_allow_html=True)
st.markdown(build_dynamic_font_css(110.0), unsafe_allow_html=True)
_embed_html(theme_toggle_js(), height=0, width=0)
_embed_html(ANTHROPIC_SELECTBOX_SCRIPT, height=0, width=0)
_embed_html(build_header_agent_badge_script(), height=0, width=0)

st.session_state.agent_name = 'Generic Agent'
with st.chat_message("assistant"):
    st.markdown(f'<div class="msg-timestamp">{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</div>', unsafe_allow_html=True)
    st.write("欢迎使用GenericAgent~")


@st.fragment
def render_sidebar():
    llm_options, current_idx = agent.list_llms(), agent.llm_no
    st.session_state.selected_llm_idx = current_idx
    llm_labels = {idx: f"{idx}: {(name or '').strip()}" for idx, name, _ in llm_options}
    st.caption(f"当前使用的LLM为：{current_idx}: {agent.get_llm_name()}", help="可在下方选择链路")
    st.markdown(f'<div data-testid="sidebar-llm-max-label" style="display:none">{html.escape(max(llm_labels.values(), key=len, default=""))}</div>', unsafe_allow_html=True)
    selected_idx = st.selectbox("选择链路：", [idx for idx, _, _ in llm_options], index=next((i for i, (idx, _, _) in enumerate(llm_options) if idx == current_idx), 0), format_func=llm_labels.get, key="sidebar_llm_select")
    if selected_idx != current_idx:
        agent.next_llm(selected_idx)
        st.session_state.selected_llm_idx = selected_idx
        st.toast(f"已切换到备用链路：{llm_labels[selected_idx]}")
        st.rerun()
    st.divider()
    if st.button("重新注入System Prompt"):
        if hasattr(agent, 'za'):
            agent.za.handler._last_tool_schemas_hash = None
        st.toast("下次将重新注入System Prompt")

    st.divider()

    # Theme toggle (at bottom)
    theme_labels = {"light": "☀️ Light", "dark": "🌙 Dark", "auto": "🔄 Auto"}
    current_theme = st.session_state.get("theme", "light")
    theme_keys = list(theme_labels.keys())
    theme_idx = theme_keys.index(current_theme) if current_theme in theme_keys else 0
    selected_theme = st.selectbox(
        "Theme",
        theme_keys,
        index=theme_idx,
        format_func=lambda k: theme_labels[k],
    )
    if selected_theme != current_theme:
        st.session_state.theme = selected_theme
        st.rerun()

with st.sidebar: render_sidebar()


def start_agent_task(prompt):
    st.session_state.display_queue = agent.put_task(prompt, source="user")
    st.session_state.streaming, st.session_state.stopping, st.session_state.partial_response = True, False, ''
    st.session_state.reply_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    st.session_state.current_prompt = prompt


def poll_agent_output(max_items=20):
    q = st.session_state.display_queue
    if q is None:
        st.session_state.streaming = False
        return False
    done = False
    for _ in range(max_items):
        try:
            item = q.get_nowait()
        except queue.Empty:
            break
        if 'next' in item: st.session_state.partial_response = item['next']
        if 'done' in item:
            st.session_state.partial_response = item['done']
            done = True
            break
    if done: st.session_state.streaming = st.session_state.stopping = False; st.session_state.display_queue = None
    return done


def _get_response_segments(text):
    return [p for p in re.split(r'(?=\*\*LLM Running \(Turn \d+\) \.\.\.\*\*)', text) if p.strip()] or [text]

def render_message(role, content, ts='', unsafe_allow_html=True):
    with st.chat_message(role):
        if ts: st.markdown(f'<div class="msg-timestamp">{ts}</div>', unsafe_allow_html=True)
        st.markdown(content, unsafe_allow_html=unsafe_allow_html)

def finish_streaming_message():
    reply_ts = st.session_state.reply_ts
    st.session_state.messages.extend({"role": "assistant", "content": seg, "time": reply_ts} for seg in _get_response_segments(st.session_state.partial_response))
    st.session_state.last_reply_time = int(time.time())
    st.session_state.partial_response = st.session_state.reply_ts = st.session_state.current_prompt = ''

def render_streaming_area():
    if not st.session_state.streaming: return
    with st.container():
        st.markdown('<span class="stop-btn-anchor"></span>', unsafe_allow_html=True)
        if st.button("⏹️ 停止生成", type="primary"):
            agent.abort(); st.session_state.stopping = True; st.toast("已发送停止信号"); st.rerun()
    reply_ts = st.session_state.reply_ts
    with st.empty().container():
        segments = _get_response_segments(st.session_state.partial_response)
        for i, seg in enumerate(segments): render_message("assistant", seg + ("" if i < len(segments) - 1 else "▌"), ts=reply_ts, unsafe_allow_html=False)
    if poll_agent_output(): finish_streaming_message()
    else: time.sleep(0.2)
    st.rerun()

for msg in st.session_state.messages: render_message(msg["role"], msg["content"], ts=msg.get("time", ""), unsafe_allow_html=True)
if st.session_state.streaming: render_streaming_area()
if prompt := st.chat_input("请输入指令", disabled=st.session_state.streaming):
    st.session_state.messages.append({"role": "user", "content": prompt, "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    start_agent_task(prompt)
    st.rerun()

