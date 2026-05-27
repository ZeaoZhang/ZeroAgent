"""

Streamlit Web 前端 — ZeroAgent 可视化聊天界面.

提供侧边栏（后端选择、模型信息、max_turns 滑块）、
主聊天区域（消息流、工具调用展开面板）和任务提交输入框。

依赖: pip install streamlit

用法:
    streamlit run zero_agent/frontends/stapp.py
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any, Dict, List, Optional

import streamlit as st

st.set_page_config(page_title="ZeroAgent", page_icon="", layout="wide")

# 标题
st.title("ZeroAgent")
st.caption("Clean & reusable autonomous agent framework")


@st.cache_resource
def create_agent() -> Any:
    """创建并缓存 ZeroAgent 实例."""
    from zero_agent.core.agent import ZeroAgent
    from zero_agent.core.config import AgentConfig

    config_path = os.path.join(
        os.path.expanduser("~"), ".zero_agent", "config.yaml"
    )
    if os.path.isfile(config_path):
        config = AgentConfig.from_yaml(config_path)
    else:
        config = AgentConfig.from_env()

    return ZeroAgent(config=config)


def _render_sidebar(agent: Any) -> None:
    """渲染侧边栏控件.

    Args:
        agent: ZeroAgent 实例.
    """
    with st.sidebar:
        st.header("Settings")

        # 后端选择器
        try:
            backends = agent.list_backends()
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
                agent.switch_backend(selected)
                st.rerun()
        except Exception:
            st.text("No backends available")

        # 模型信息
        try:
            st.text(f"Model: {agent.client.name}")
        except Exception:
            pass

        # Max turns
        max_turns = st.slider("Max Turns", 10, 200, 80)
        agent.handler.max_turns = max_turns

        st.divider()

        # 操作按钮
        if st.button(" New Session", use_container_width=True):
            _new_session(agent)
        if st.button(" Export", use_container_width=True):
            _export_chat()

        st.divider()
        st.caption(f"Tools: {len(agent.registry.list_all())}")


def _new_session(agent: Any) -> None:
    """清空会话状态."""
    agent.client.history = []
    agent.client.system = ""
    agent.handler.working = {}
    agent.handler.history_info = []
    agent.handler._empty_ct = 0
    st.session_state.messages = []
    st.rerun()


def _export_chat() -> None:
    """导出聊天记录为 markdown."""
    if "messages" not in st.session_state:
        return

    md_lines: list[str] = ["# ZeroAgent Chat Export\n"]
    for msg in st.session_state.messages:
        role = msg["role"]
        content = msg.get("content", "")
        if role == "user":
            md_lines.append(f"## User\n\n{content}\n")
        elif role == "assistant":
            md_lines.append(f"## Agent\n\n{content}\n")
        elif role == "tool":
            tool_name = msg.get("tool_name", "tool")
            md_lines.append(f"### Tool: {tool_name}\n\n```\n{content}\n```\n")

    import datetime
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    st.download_button(
        "Download",
        "\n".join(md_lines),
        f"zeroagent_chat_{timestamp}.md",
        mime="text/markdown",
    )


def _render_chat(agent: Any) -> None:
    """渲染主聊天区域并处理用户输入.

    Args:
        agent: ZeroAgent 实例.
    """
    # 初始化消息历史
    if "messages" not in st.session_state:
        st.session_state.messages = []

    # 渲染历史消息
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            if msg["role"] == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    with st.expander(
                        f" {tc.get('tool_name', 'tool')}",
                        expanded=False,
                    ):
                        st.json(tc.get("args", {}))
                        if tc.get("result"):
                            st.code(str(tc["result"])[:2000])
            st.markdown(msg.get("content", ""))

    # 用户输入
    user_input = st.chat_input("输入任务描述...")
    if not user_input:
        return

    # 显示用户消息
    st.session_state.messages.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)

    # 执行 agent
    with st.chat_message("assistant"):
        status_text = st.empty()
        content_placeholder = st.empty()
        collected: list[str] = []
        tool_calls: list[dict] = []

        try:
            gen = agent.run(user_input)
            for chunk in gen:
                if isinstance(chunk, str):
                    collected.append(chunk)
                    content_placeholder.markdown("".join(collected))
                elif isinstance(chunk, dict) and "turn" in chunk:
                    status_text.caption(f"Turn {chunk['turn']}")

            # 保存助手消息
            full_response = "".join(collected)
            st.session_state.messages.append({
                "role": "assistant",
                "content": full_response,
                "tool_calls": tool_calls,
            })
        except Exception as e:
            st.error(f"Error: {e}")


def main() -> None:
    """Streamlit 应用主入口."""
    try:
        agent = create_agent()
    except Exception as e:
        st.error(f"Failed to create agent: {e}")
        st.info(
            "Run `python -m zero_agent.utils.configure` "
            "to set up your configuration."
        )
        return

    _render_sidebar(agent)
    _render_chat(agent)


if __name__ == "__main__":
    main()
