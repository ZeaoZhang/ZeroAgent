"""ZeroAgent Streamlit frontend — minimal web UI.

Usage:
    streamlit run zero_agent/frontends/stapp.py
"""

import streamlit as st

from zero_agent.core.agent import ZeroAgent
from zero_agent.core.config import load_default_config
from zero_agent.core.types import TerminalEvent, TerminalStatus
from zero_agent.runners.agent_runner import _consume_agent_run


def _waiting_text(terminal: TerminalEvent) -> str:
    payload = terminal.data if isinstance(terminal.data, dict) else {}
    nested = payload.get("data")
    if isinstance(nested, dict):
        payload = nested
    fallback = terminal.text or terminal.reason or "Waiting for user input"
    question = str(payload.get("question") or fallback)
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return question
    options = "\n".join(f"- {candidate}" for candidate in candidates)
    return f"{question}\n\n{options}"


def _error_text(terminal: TerminalEvent) -> str:
    if terminal.status == TerminalStatus.BUDGET_EXHAUSTED and not terminal.text:
        suffix = f" ({terminal.reason})" if terminal.reason else ""
        return f"Reached the turn/retry budget; task not completed{suffix}"
    return terminal.text or terminal.reason or terminal.status.value


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
            try:
                gen = agent.run(prompt)
            except Exception as exc:
                terminal = TerminalEvent(
                    status=TerminalStatus.FAILED,
                    reason=type(exc).__name__,
                    text=str(exc),
                )
            else:
                def on_chunk(chunk):
                    nonlocal full_text
                    if isinstance(chunk, str):
                        full_text += chunk
                        placeholder.markdown(full_text)

                terminal = _consume_agent_run(gen, on_chunk)
            if terminal.status == TerminalStatus.COMPLETED:
                placeholder.markdown(full_text or terminal.text)
                if full_text or terminal.text:
                    st.session_state.messages.append({
                        "role": "assistant",
                        "content": full_text or terminal.text,
                    })
            elif terminal.status == TerminalStatus.WAITING:
                message = _waiting_text(terminal)
                placeholder.info(message)
                st.session_state.messages.append({"role": "system", "content": message})
            elif terminal.status == TerminalStatus.CANCELLED:
                placeholder.warning(terminal.reason or "Cancelled")
            else:
                placeholder.error(_error_text(terminal))
            st.session_state.running = False


if __name__ == "__main__":
    main()
