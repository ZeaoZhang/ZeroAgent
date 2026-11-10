"""ZeroAgent Streamlit frontend — minimal web UI.

Usage:
    streamlit run zero_agent/frontends/stapp.py
"""

import streamlit as st
from pathlib import Path

from zero_agent.core.agent import ZeroAgent
from zero_agent.core.config import load_default_config
from zero_agent.core.types import TerminalEvent, TerminalStatus
from zero_agent.runners.agent_runner import _consume_agent_run
from zero_agent.bots.common import resolve_output_files, strip_files
from zero_agent.bots.common import runner_workspace_dir
from zero_agent.core.localization import PROMPT_CAPABILITY_FILE_DELIVERY


_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif"}


def _output_files(agent: ZeroAgent, text: str) -> list[str]:
    workspace = Path(runner_workspace_dir(agent)).resolve()
    files = []
    for raw_path in resolve_output_files(text, workspace_dir=workspace):
        try:
            path = Path(raw_path).resolve(strict=True)
            path.relative_to(workspace)
        except (OSError, RuntimeError, ValueError):
            continue
        if path.is_file():
            files.append(str(path))
    return files


def _render_output_files(files: list[str], key_prefix: str) -> None:
    for index, raw_path in enumerate(files):
        path = Path(raw_path)
        try:
            content = path.read_bytes()
        except OSError:
            st.caption(f"File is no longer available: {path.name}")
            continue
        if path.suffix.lower() in _IMAGE_EXTS:
            st.image(content, caption=path.name, use_container_width=True)
        st.download_button(
            f"Download {path.name}",
            data=content,
            file_name=path.name,
            key=f"{key_prefix}-file-{index}",
        )


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
            if msg.get("files"):
                _render_output_files(msg["files"], f"history-{msg.get('id', 'message')}")

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
                gen = agent.run(
                    prompt,
                    prompt_capabilities=(PROMPT_CAPABILITY_FILE_DELIVERY,),
                )
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
                answer = terminal.text or full_text
                files = _output_files(agent, answer)
                visible_answer = strip_files(answer) or answer
                placeholder.markdown(visible_answer)
                _render_output_files(files, f"turn-{len(st.session_state.messages)}")
                if visible_answer or files:
                    st.session_state.messages.append({
                        "role": "assistant",
                        "content": visible_answer,
                        "files": files,
                        "id": len(st.session_state.messages),
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
