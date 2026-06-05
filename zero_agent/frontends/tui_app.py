"""
Textual TUI — ZeroAgent 终端交互界面.

基于 Textual 框架的终端 UI，提供 Markdown 渲染的聊天区域、
流式响应、斜杠命令和侧边栏设置.

用法:
    python -m zero_agent.frontends.tui_app
"""

from __future__ import annotations

import os
import queue
import sys
import threading
from datetime import datetime
from typing import Any, Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    Markdown,
    Select,
    Static,
)

from zero_agent.core.agent import ZeroAgent
from zero_agent.core.config import AgentConfig

# ═══════════════════════════════════════════════════════════════
# Agent Bridge
# ═══════════════════════════════════════════════════════════════


class AgentBridge:
    """在后台线程运行 ZeroAgent，通过 Queue 与 UI 线程通信."""

    def __init__(self, agent: ZeroAgent):
        self.agent = agent
        self._queue: queue.Queue = queue.Queue()
        self._running = False
        self._stop = False

    @property
    def output_queue(self) -> queue.Queue:
        return self._queue

    def start_task(self, prompt: str) -> None:
        """在后台线程启动 agent 任务."""
        self._stop = False
        self._running = True

        def _worker() -> None:
            try:
                gen = self.agent.run(prompt)
                for chunk in gen:
                    if self._stop:
                        break
                    if isinstance(chunk, str):
                        self._queue.put({"type": "chunk", "text": chunk})
                    elif isinstance(chunk, dict):
                        if "turn" in chunk:
                            self._queue.put({"type": "turn", "n": chunk["turn"]})
                        if "error" in chunk:
                            self._queue.put({"type": "error", "text": str(chunk["error"])})
            except Exception as e:
                self._queue.put({"type": "error", "text": str(e)})
            finally:
                self._queue.put({"type": "done"})
                self._running = False

        threading.Thread(target=_worker, daemon=True).start()

    def stop_task(self) -> None:
        """发送停止信号."""
        self._stop = True
        self.agent.abort()

    def new_session(self) -> None:
        """重置会话."""
        self.agent.client.history = []
        self.agent.client.system = ""
        self.agent.handler.working = {}
        self.agent.handler.history_info = []
        self.agent.handler._empty_ct = 0

    @property
    def model_name(self) -> str:
        try:
            return self.agent.client.name
        except Exception:
            return "unknown"


# ═══════════════════════════════════════════════════════════════
# Help Screen
# ═══════════════════════════════════════════════════════════════


HELP_TEXT = """
# ZeroAgent TUI — Help

## Slash Commands

| Command | Description |
|---------|-------------|
| `/help` | Show this help |
| `/new` | Start a new session |
| `/stop` | Stop current generation |
| `/status` | Show agent status |
| `/session.<key>=<value>` | Set session parameter |
| `/resume` | List recoverable sessions |
| `/continue` | List recoverable sessions |
| `/continue N` | Restore session N with message replay |
| `/export` | Export chat history |

## Key Bindings

| Key | Action |
|-----|--------|
| `Ctrl+Q` | Quit |
| `Ctrl+S` | Focus sidebar |
| `Ctrl+I` | Focus input |
| `?` | Toggle help |
"""


# ═══════════════════════════════════════════════════════════════
# Help Modal
# ═══════════════════════════════════════════════════════════════


class HelpModal(ModalScreen[None]):
    """Help overlay modal."""

    def compose(self) -> ComposeResult:
        yield Markdown(HELP_TEXT)

    def on_key(self) -> None:
        self.dismiss()


# ═══════════════════════════════════════════════════════════════
# Main App
# ═══════════════════════════════════════════════════════════════


class ZeroAgentTUI(App):
    """ZeroAgent Textual 终端界面."""

    CSS = """
    Horizontal { height: 1fr; }
    VerticalScroll#chat-view {
        width: 1fr;
        height: 1fr;
        border: solid $primary;
        padding: 1;
        overflow-y: auto;
    }
    Vertical#sidebar {
        width: 30;
        height: 1fr;
        border: solid $primary;
        padding: 1;
        background: $surface;
    }
    Vertical#sidebar Label {
        margin-bottom: 1;
    }
    #status-bar {
        dock: bottom;
        height: 1;
        background: $primary;
        color: $text;
        padding: 0 1;
    }
    #status-bar Label {
        width: 1fr;
    }
    Static#stream-area {
        color: $text-muted;
        min-height: 1;
    }
    Input#chat-input {
        dock: bottom;
        margin-top: 1;
    }
    Button {
        margin-right: 1;
    }
    """

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("ctrl+s", "focus_sidebar", "Sidebar"),
        Binding("ctrl+i", "focus_input", "Input"),
        Binding("question_mark", "toggle_help", "Help"),
        Binding("ctrl+n", "new_session", "New Session"),
    ]

    def __init__(self):
        super().__init__()
        self._bridge: Optional[AgentBridge] = None
        self._messages: list[dict] = []
        self._streaming = False
        self._accumulated = ""
        self._turn = 0

    def on_mount(self) -> None:
        """初始化 agent 并显示欢迎消息."""
        try:
            config = self._load_config()
            agent = ZeroAgent(config=config)
            agent.handler.max_turns = agent.config.max_turns
            self._bridge = AgentBridge(agent)
        except Exception as e:
            self._bridge = None
            self._add_message("system", f"Failed to create agent: {e}")

        if self._bridge:
            self._add_message(
                "assistant",
                "Welcome to ZeroAgent TUI~  Type `/help` for commands.",
            )
        self._update_status()
        self.set_interval(0.1, self._poll_output)

    def compose(self) -> ComposeResult:
        """构建 UI 布局."""
        with Horizontal():
            yield VerticalScroll(id="chat-view")
            with Vertical(id="sidebar"):
                yield Label("Settings")
                yield Button("New Session", id="btn-new", variant="default")
                yield Button("Export", id="btn-export", variant="default")
                yield Button("Help", id="btn-help", variant="default")
                yield Label("")
                yield Label("Backend:", id="lbl-backend")
                yield Label("Model:", id="lbl-model")
                yield Label("Turns:", id="lbl-turns")
        yield Static("Ready.", id="status-bar")
        yield Input(placeholder="Enter your instruction...  (/help for commands)", id="chat-input")

    # ── Config ──────────────────────────────────────────────

    @staticmethod
    def _load_config() -> AgentConfig:
        config_path = os.path.join(
            os.path.expanduser("~"), ".zero_agent", "config.yaml"
        )
        if os.path.isfile(config_path):
            return AgentConfig.from_yaml(config_path)
        return AgentConfig.from_env()

    # ── Messages ────────────────────────────────────────────

    def _add_message(self, role: str, content: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self._messages.append({"role": role, "content": content, "time": ts})
        self._render_messages()

    def _render_messages(self) -> None:
        chat = self.query_one("#chat-view", VerticalScroll)
        chat.remove_children()

        for msg in self._messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            ts = msg.get("time", "")

            prefix = {"user": "You", "assistant": "ZA", "system": "SYS"}.get(role, role)
            header = f"**{prefix}**  _{ts}_\n\n"
            chat.mount(Markdown(header + content))
            chat.mount(Static(""))

        chat.scroll_end(animate=False)

    # ── Output Polling ──────────────────────────────────────

    def _poll_output(self) -> None:
        if not self._bridge or not self._bridge._running:
            return

        try:
            while True:
                item = self._bridge.output_queue.get_nowait()
                t = item.get("type", "")

                if t == "chunk":
                    self._accumulated += item["text"]
                    self._update_stream()
                elif t == "turn":
                    self._turn = item["n"]
                    self._update_status()
                elif t == "error":
                    self._add_message("system", f"Error: {item['text']}")
                elif t == "done":
                    self._finish_stream()
                    return
        except queue.Empty:
            pass

    def _update_stream(self) -> None:
        chat = self.query_one("#chat-view", VerticalScroll)
        # Remove previous streaming content (last Static)
        children = list(chat.children)
        if children and isinstance(children[-1], Static):
            children[-1].remove()

        chat.mount(Markdown(f"**ZA**  _streaming..._\n\n{self._accumulated}▌"))
        chat.scroll_end(animate=False)

    def _finish_stream(self) -> None:
        chat = self.query_one("#chat-view", VerticalScroll)
        # Remove streaming placeholder
        children = list(chat.children)
        if children and isinstance(children[-1], Static):
            pass  # already handled in _update_stream

        if self._accumulated:
            self._add_message("assistant", self._accumulated)
        self._accumulated = ""
        self._streaming = False
        self._update_status()
        self.query_one("#chat-input", Input).disabled = False

    # ── Status ──────────────────────────────────────────────

    def _update_status(self) -> None:
        status = self.query_one("#status-bar", Static)
        if not self._bridge:
            status.update("No agent configured")
            return

        bridge = self._bridge
        try:
            model = bridge.model_name
        except Exception:
            model = "?"

        running = "RUNNING" if bridge._running else "IDLE"
        status.update(
            f" Model: {model} | Turn: {self._turn} | Status: {running} "
            f"| Ctrl+Q Quit | ? Help"
        )

        # Sidebar labels
        try:
            self.query_one("#lbl-model", Label).update(f"Model: {model}")
        except Exception:
            pass

    # ── Input Handler ───────────────────────────────────────

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""

        if text.startswith("/"):
            self._handle_command(text)
        elif self._bridge and not self._bridge._running:
            self._add_message("user", text)
            self._bridge.start_task(text)
            self._streaming = True
            self._turn += 1
            self._update_status()
            event.input.disabled = True

    def _handle_command(self, text: str) -> None:
        cmd = text.lower()
        bridge = self._bridge

        if cmd == "/help" or cmd == "/?":
            self._add_message("system", HELP_TEXT)
        elif cmd == "/new":
            if bridge:
                bridge.new_session()
                self._messages = []
                self._turn = 0
                self.call_from_thread(
                    self._add_message, "system", "Session reset."
                )
                self._update_status()
        elif cmd == "/stop":
            if bridge and bridge._running:
                bridge.stop_task()
                self._add_message("system", "Stop signal sent.")
        elif cmd == "/status":
            status = f"Model: {bridge.model_name if bridge else 'none'} | Turn: {self._turn} | Streaming: {self._streaming}"
            self._add_message("system", status)
        elif cmd.startswith("/session."):
            if bridge:
                try:
                    parts = cmd[9:].split("=", 1)
                    if len(parts) == 2:
                        key, value = parts[0].strip(), parts[1].strip()
                        setattr(bridge.agent.client, key, value)
                        self._add_message("system", f"session.{key} = {value}")
                except Exception as e:
                    self._add_message("system", f"Error: {e}")
        elif cmd == "/resume" or cmd.startswith("/continue"):
            self._handle_continue(cmd)
        elif cmd == "/export":
            self._export_chat()
        else:
            self._add_message("system", f"Unknown command: {text}. Type /help.")

    def _export_chat(self) -> None:
        md = ["# ZeroAgent Chat Export\n"]
        for msg in self._messages:
            md.append(f"## {msg['role'].title()} ({msg['time']})\n\n{msg['content']}\n")

        out_path = os.path.join(
            os.path.expanduser("~"), ".zero_agent",
            f"chat_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md",
        )
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(md))
        self._add_message("system", f"Exported to {out_path}")

    def _handle_continue(self, cmd: str) -> None:
        """处理 /continue 或 /continue N 命令，回放历史会话.

        /continue 列出可恢复会话；/continue N 恢复第 N 个会话，
        使用 extract_ui_messages 解析原始日志并回放到 TUI 消息区.

        Args:
            cmd: 原始命令字符串.
        """
        from zero_agent.bots.shared.continue_cmd import (
            list_sessions,
            handle_frontend_command,
            extract_ui_messages,
        )
        import os as _os

        bridge = self._bridge
        if not bridge:
            self._add_message("system", "Agent not available.")
            return

        runner = bridge.agent
        cmd_stripped = cmd.strip()
        m = re.match(r"(/continue|/resume)\s+(\d+)\s*$", cmd_stripped)
        sessions = list_sessions(exclude_pid=_os.getpid())

        if not m:
            # /continue 或 /resume — 列出可用会话
            from zero_agent.bots.shared.continue_cmd import format_list as _fmt
            formatted = _fmt(sessions)
            self._add_message("system", formatted)
            return

        idx = int(m.group(2)) - 1
        if not (0 <= idx < len(sessions)):
            self._add_message("system", f"索引越界（有效范围 1-{len(sessions)}）")
            return

        target = sessions[idx][0]
        result = handle_frontend_command(runner, cmd_stripped)
        self._add_message("system", result)

        if result.startswith("✅"):
            history = extract_ui_messages(target)
            if history:
                # 清除当前消息并用历史会话替换
                self._messages = []
                for msg in history:
                    role = msg.get("role", "assistant")
                    content = msg.get("content", "").strip()
                    if content:
                        self._add_message(role, content)

    # ── Actions ─────────────────────────────────────────────

    def action_toggle_help(self) -> None:
        self.push_screen(HelpModal())

    def action_focus_sidebar(self) -> None:
        sidebar = self.query_one("#sidebar", Vertical)
        sidebar.focus()

    def action_focus_input(self) -> None:
        self.query_one("#chat-input", Input).focus()

    def action_new_session(self) -> None:
        self._handle_command("/new")

    # ── Button Handlers ─────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id
        if btn_id == "btn-new":
            self.action_new_session()
        elif btn_id == "btn-export":
            self._handle_command("/export")
        elif btn_id == "btn-help":
            self.action_toggle_help()


# ═══════════════════════════════════════════════════════════════
# Entry Point
# ═══════════════════════════════════════════════════════════════


def main() -> None:
    """启动 Textual TUI."""
    app = ZeroAgentTUI()
    app.run()


if __name__ == "__main__":
    main()
