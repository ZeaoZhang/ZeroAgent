"""ZeroAgent Streamlit frontend — minimal web UI.

Usage:
    streamlit run zero_agent/frontends/stapp.py
"""

import streamlit as st

from zero_agent.core.agent import ZeroAgent
from zero_agent.core.config import load_default_config


def main():
    st.set_page_config(page_title="ZeroAgent Chat", layout="wide")
    st.title("ZeroAgent Chat")

    # Initialize session state
    if "agent" not in st.session_state:
        config = load_default_config()
        st.session_state.agent = ZeroAgent(config=config)
        st.session_state.messages = []
        st.session_state.running = False

    agent: ZeroAgent = st.session_state.agent

    # Sidebar: model selection and backend info
    with st.sidebar:
        st.header("Model")
        llms = agent.list_llms()
        llm_labels = [f"{i}: {name} ({model})" for i, name, model in llms]
        selected = st.selectbox("Backend", range(len(llms)), format_func=lambda i: llm_labels[i] if i < len(llm_labels) else "")
        if st.button("Switch"):
            agent.next_llm(selected)
            st.success(f"Switched to backend {selected}")

        st.header("Backends")
        for i, name, model in llms:
            st.text(f"[{i}] {name}: {model}")

        if st.button("Stop", key="stop_btn"):
            agent.abort()
            st.session_state.running = False

    # Chat history
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # Chat input
    if prompt := st.chat_input("Type your message..."):
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        st.session_state.running = True
        with st.chat_message("assistant"):
            placeholder = st.empty()
            full_text = ""
            gen = agent.run(prompt)
            try:
                for chunk in gen:
                    if isinstance(chunk, str):
                        full_text += chunk
                        placeholder.markdown(full_text)
            except Exception as e:
                placeholder.error(f"Error: {e}")
            finally:
                placeholder.markdown(full_text)
                st.session_state.messages.append({"role": "assistant", "content": full_text})
                st.session_state.running = False


if __name__ == "__main__":
    main()
