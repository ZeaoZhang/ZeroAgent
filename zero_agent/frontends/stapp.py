"""
Streamlit Web 前端 — ZeroAgent 可视化聊天界面.

基于 GenericAgent stapp2.py UX 设计，提供 Anthropic 风格主题、
流式响应渲染、停止生成按钮和 Turn 分段显示.

用法:
    streamlit run zero_agent/frontends/stapp.py
"""

from __future__ import annotations

import html
import json
import os
import queue
import re
import sys
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import streamlit as st

try:
    from streamlit.components.v1 import html as _embed_html
except ImportError:  # pragma: no cover
    _embed_html = None  # type: ignore

st.set_page_config(page_title="ZeroAgent", layout="wide")

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

[data-testid="stHeader"] {
    background-color: var(--za-bg) !important;
    border-bottom: 1px solid var(--za-border) !important;
}
[data-testid="stToolbar"] { visibility: hidden !important; }
#MainMenu, [data-testid="stDecoration"] { display: none !important; visibility: hidden !important; }

button[data-testid="stExpandSidebarButton"] {
    visibility: visible !important;
    background: #F4F1EA !important;
    border: none !important;
    color: #3B2F2A !important;
    border-radius: 10px !important;
    box-shadow: none !important;
}
button[data-testid="stExpandSidebarButton"]:hover {
    background: #EAE4D9 !important;
}

[data-testid="stSidebar"] {
    background-color: var(--za-sidebar-bg) !important;
    border-right: 1px solid var(--za-border) !important;
}
[data-testid="stSidebar"] .stMarkdown, [data-testid="stSidebar"] p,
[data-testid="stSidebar"] span, [data-testid="stSidebar"] label {
    color: var(--za-text) !important;
}

h1, [data-testid="stHeading"] h1 {
    color: var(--za-text) !important;
    font-weight: 600 !important;
    letter-spacing: -0.02em !important;
}

pre, .stCodeBlock, .stCodeBlock pre {
    background-color: var(--za-code-bg) !important;
    border: 1px solid var(--za-border) !important;
    border-radius: 8px !important;
}
.stCodeBlock code { background-color: transparent !important; border: none !important; }

[data-testid="stChatMessage"] pre code {
    background-color: transparent !important;
    border: none !important;
    padding: 0 !important;
}

a { color: var(--za-accent) !important; }
a:hover { color: var(--za-primary-hover) !important; }

::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: var(--za-bg); }
::-webkit-scrollbar-thumb { background: var(--za-border); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: var(--za-text-secondary); }

.msg-timestamp {
    font-size: 0.75rem;
    color: var(--za-text-secondary);
    margin-bottom: 0.2rem;
}

[data-testid="stBottomBlockContainer"] { background-color: var(--za-bg) !important; }
</style>
"""

AUTOSCROLL_JS = """
<script>
(function() {
    const hostWin = window.parent || window;
    const hostDoc = hostWin.document || document;
    const KEY = '__zaAutoScrollInstalled';
    if (hostWin[KEY]) return;
    hostWin[KEY] = true;

    function scrollToBottom() {
        const main = hostDoc.querySelector('[data-testid="stMainBlockContainer"]');
        if (main) {
            main.scrollTop = main.scrollHeight;
        }
    }

    const observer = new MutationObserver(function() {
        scrollToBottom();
    });

    function attach() {
        const main = hostDoc.querySelector('[data-testid="stMainBlockContainer"]');
        if (main) {
            observer.observe(main, { childList: true, subtree: true, characterData: true });
            scrollToBottom();
            return true;
        }
        return false;
    }

    if (!attach()) {
        var timer = hostWin.setInterval(function() {
            if (attach()) hostWin.clearInterval(timer);
        }, 200);
        hostWin.setTimeout(function() { hostWin.clearInterval(timer); }, 10000);
    }
})();
</script>
"""

# ═══════════════════════════════════════════════════════════════
# Agent Factory
# ═══════════════════════════════════════════════════════════════


@st.cache_resource
def create_agent() -> Any:
    """创建并缓存 ZeroAgent + AgentRunner 实例."""
    from zero_agent.adapters.agent_runner import AgentRunner
    from zero_agent.core.agent import ZeroAgent
    from zero_agent.core.config import AgentConfig

    config_path = os.path.join(
        os.path.expanduser("~"), ".zero_agent", "config.yaml"
    )
    if os.path.isfile(config_path):
        config = AgentConfig.from_yaml(config_path)
    else:
        config = AgentConfig.from_env()

    za = ZeroAgent(config=config)
    return AgentRunner(za)


def _get_agent() -> Any:
    """获取 AgentRunner 实例."""
    if "agent_runner" not in st.session_state:
        st.session_state.agent_runner = create_agent()
    return st.session_state.agent_runner


# ═══════════════════════════════════════════════════════════════
# Session State
# ═══════════════════════════════════════════════════════════════


def _init_session() -> None:
    """初始化 session state 默认值."""
    defaults: dict = {
        "messages": [],
        "streaming": False,
        "stop_sig": False,
        "display_queue": None,
        "partial_response": "",
        "reply_ts": "",
        "current_prompt": "",
        "font_scale": 110.0,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


# ═══════════════════════════════════════════════════════════════
# Sidebar
# ═══════════════════════════════════════════════════════════════


@st.fragment
def _render_sidebar(runner: Any) -> None:
    """渲染侧边栏 — 后端选择、max_turns、字体大小、操作."""
    with st.sidebar:
        st.header("Settings")

        # Backend selector
        try:
            backends = runner.list_backends()
            backend_names = [b[0] for b in backends]
            active_idx = next(
                (i for i, b in enumerate(backends) if b[2]), 0
            )
            selected = st.selectbox(
                "LLM Backend",
                backend_names,
                index=active_idx,
            )
            if selected != backend_names[active_idx]:
                runner.switch_backend(selected)
                st.rerun()
        except Exception:
            st.text("No backends available")

        # Model info
        try:
            st.caption(f"Model: {runner.get_model_name()}")
        except Exception:
            pass

        # Max turns
        max_turns = st.slider(
            "Max Turns",
            10, 200,
            min(max(runner.za.config.max_turns, 10), 200),
        )
        runner.za.config.max_turns = max_turns
        runner.za.handler.max_turns = max_turns

        # Font size
        font_scale = st.slider(
            "Font Size",
            100, 150,
            int(st.session_state.font_scale),
            step=13,
        )
        if font_scale != int(st.session_state.font_scale):
            st.session_state.font_scale = float(font_scale)
            st.rerun()

        st.divider()

        if st.button(" New Session", use_container_width=True):
            _new_session(runner)
        if st.button(" Export", use_container_width=True):
            _export_chat()

        st.divider()
        st.caption(f"Tools: {len(runner.za.registry.list_all())}")


def _new_session(runner: Any) -> None:
    """清空会话并重置 agent 状态."""
    runner.za.client.history = []
    runner.za.client.system = ""
    runner.za.handler.working = {}
    runner.za.handler.history_info = []
    runner.za.handler._empty_ct = 0
    st.session_state.messages = []
    st.session_state.streaming = False
    st.session_state.stop_sig = False
    st.session_state.partial_response = ""
    st.rerun()


def _export_chat() -> None:
    """导出聊天记录为 markdown 下载."""
    msgs = st.session_state.get("messages", [])
    if not msgs:
        return

    md_lines = ["# ZeroAgent Chat Export\n"]
    for msg in msgs:
        role = msg.get("role", "")
        content = msg.get("content", "")
        ts = msg.get("time", "")
        header = f"## {role.title()}"
        if ts:
            header += f" ({ts})"
        md_lines.append(f"{header}\n\n{content}\n")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    st.download_button(
        "Download",
        "\n".join(md_lines),
        f"zeroagent_chat_{timestamp}.md",
        mime="text/markdown",
    )


# ═══════════════════════════════════════════════════════════════
# Agent Task Runner
# ═══════════════════════════════════════════════════════════════


def _start_agent_task(runner: Any, prompt: str) -> None:
    """在后台线程启动 agent 并将输出推送到 display_queue."""
    q: queue.Queue = queue.Queue()
    st.session_state.display_queue = q
    st.session_state.streaming = True
    st.session_state.stop_sig = False
    st.session_state.partial_response = ""
    st.session_state.reply_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    st.session_state.current_prompt = prompt

    def _worker() -> None:
        try:
            gen = runner.taskloop(prompt)
            for chunk in gen:
                if st.session_state.stop_sig:
                    break
                if isinstance(chunk, str):
                    q.put({"next": chunk})
                elif isinstance(chunk, dict):
                    q.put(chunk)
        except Exception as e:
            q.put({"error": str(e)})
        finally:
            q.put({"done": True})

    threading.Thread(target=_worker, daemon=True).start()


def _poll_agent_output(max_items: int = 20) -> bool:
    """从 display_queue 拉取数据. 返回 True 表示完成."""
    q = st.session_state.display_queue
    if q is None:
        st.session_state.streaming = False
        return True
    done = False
    for _ in range(max_items):
        try:
            item = q.get_nowait()
        except queue.Empty:
            break
        if "error" in item:
            st.session_state.partial_response = f"[Error: {item['error']}]"
            done = True
            break
        if "next" in item:
            st.session_state.partial_response = item["next"]
        if "done" in item:
            done = True
            break
    if done:
        st.session_state.streaming = False
        st.session_state.stop_sig = False
        st.session_state.display_queue = None
    return done


def _get_response_segments(text: str) -> list[str]:
    """按 Turn 标记拆分响应为独立消息段."""
    segments = re.split(
        r'(?=\*\*LLM Running \(Turn \d+\) \.\.\.\*\*)', text
    )
    return [s for s in segments if s.strip()] or [text]


def _finish_streaming_message() -> None:
    """流式输出结束后，将完整响应保存到消息历史."""
    reply_ts = st.session_state.reply_ts
    full = st.session_state.partial_response
    for seg in _get_response_segments(full):
        st.session_state.messages.append({
            "role": "assistant",
            "content": seg,
            "time": reply_ts,
        })
    st.session_state.partial_response = ""
    st.session_state.reply_ts = ""
    st.session_state.current_prompt = ""


# ═══════════════════════════════════════════════════════════════
# Chat Rendering
# ═══════════════════════════════════════════════════════════════


def _render_message(role: str, content: str, ts: str = "") -> None:
    """渲染单条聊天消息."""
    with st.chat_message(role):
        if ts:
            st.markdown(
                f'<div class="msg-timestamp">{ts}</div>',
                unsafe_allow_html=True,
            )
        st.markdown(content)


def _render_streaming_area() -> None:
    """渲染流式输出区域 — 实时显示 agent 输出 + 停止按钮."""
    if not st.session_state.streaming:
        return

    # Stop button
    with st.container():
        if st.button("Stop Generation", type="primary"):
            st.session_state.stop_sig = True
            st.toast("Stop signal sent")
            st.rerun()

    # Streaming content
    reply_ts = st.session_state.reply_ts
    partial = st.session_state.partial_response
    segments = _get_response_segments(partial)
    with st.empty().container():
        for i, seg in enumerate(segments):
            cursor = "" if i < len(segments) - 1 else "▌"
            _render_message("assistant", seg + cursor, ts=reply_ts)

    if _poll_agent_output():
        _finish_streaming_message()
    else:
        time.sleep(0.15)
    st.rerun()


# ═══════════════════════════════════════════════════════════════
# Font Scale CSS
# ═══════════════════════════════════════════════════════════════


def _build_font_css(scale: float) -> str:
    """构建动态字体缩放 CSS."""
    pct = max(100.0, min(150.0, scale))
    rem = pct / 100.0
    return f"""
<style id="za-font-scale">
:root, html, body, [data-testid="stAppViewContainer"], .stApp {{
    font-size: {pct:.1f}% !important;
}}
body, [data-testid="stAppViewContainer"], .stApp {{
    --za-font-scale: {rem:.3f};
}}
[data-testid="stAppViewContainer"] p, .stApp p,
[data-testid="stAppViewContainer"] li, .stApp li,
[data-testid="stAppViewContainer"] label, .stApp label,
[data-testid="stChatMessageContent"], .stApp .stCaption {{
    font-size: calc(1rem * var(--za-font-scale, 1)) !important;
}}
</style>
"""


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════


def main() -> None:
    """Streamlit 应用主入口."""
    _init_session()

    # Inject theme + auto-scroll
    st.markdown(THEME_CSS, unsafe_allow_html=True)
    st.markdown(
        _build_font_css(st.session_state.font_scale),
        unsafe_allow_html=True,
    )
    if _embed_html:
        _embed_html(AUTOSCROLL_JS, height=0, width=0)

    # Header
    st.title("ZeroAgent")
    st.caption("Clean & reusable autonomous agent framework")

    try:
        runner = _get_agent()
    except Exception as e:
        st.error(f"Failed to create agent: {e}")
        st.info(
            "Run `python -m zero_agent.utils.configure` "
            "to set up your configuration."
        )
        return

    # Sidebar
    _render_sidebar(runner)

    # Welcome message
    if not st.session_state.messages:
        with st.chat_message("assistant"):
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            st.markdown(
                f'<div class="msg-timestamp">{ts}</div>',
                unsafe_allow_html=True,
            )
            st.write("Welcome to ZeroAgent~")

    # Render history
    for msg in st.session_state.messages:
        _render_message(
            msg["role"], msg["content"], ts=msg.get("time", "")
        )

    # Streaming area
    _render_streaming_area()

    # Chat input
    if prompt := st.chat_input(
        "Enter your instruction...",
        disabled=st.session_state.streaming,
    ):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        st.session_state.messages.append({
            "role": "user",
            "content": prompt,
            "time": ts,
        })
        _start_agent_task(runner, prompt)
        st.rerun()


if __name__ == "__main__":
    main()
