"""ZeroAgent TUI — multi-panel Textual interface.

Usage:
    zero-agent-tui
    python -m zero_agent.frontends.tui

Requires zero-agent[ui] extras (textual, rich).

Layout
------
Left sidebar (file browser) | Center (chat + input) | Right (plan + log)
Toggleable panels via keyboard shortcuts.
"""

from __future__ import annotations

import os
import queue
import threading
from pathlib import Path
from typing import Any, Generator

# ── Optional-dependency guards ──────────────────────────────────────

_TEXTUAL_AVAILABLE = True
_MARKDOWN_AVAILABLE = True
_RICH_LOG_AVAILABLE = True
_DIR_TREE_AVAILABLE = True

try:
    import textual
except ImportError:
    _TEXTUAL_AVAILABLE = False

if _TEXTUAL_AVAILABLE:
    try:
        from textual.app import App, ComposeResult
        from textual.binding import Binding
        from textual.containers import (
            Vertical,
            VerticalScroll,
            Container,
        )
        from textual.screen import ModalScreen
        from textual.widgets import (
            Header,
            Input,
            Label,
            ListItem,
            ListView,
            Static,
        )
    except ImportError:
        _TEXTUAL_AVAILABLE = False

    try:
        from textual.widgets import Markdown
    except ImportError:
        _MARKDOWN_AVAILABLE = False

    try:
        from textual.widgets import RichLog
    except ImportError:
        _RICH_LOG_AVAILABLE = False

    try:
        from textual.widgets import DirectoryTree
    except ImportError:
        _DIR_TREE_AVAILABLE = False



if _TEXTUAL_AVAILABLE:

    class CommandPalette(ModalScreen[str | None]):
        """Slash-command palette overlay.

        Press `/` or `Ctrl+P` to open. Lists all registered slash commands.
        Selecting a command fills the input box with that command prefix.
        """

        DEFAULT_CSS = """
        CommandPalette {
            align: center middle;
            background: $panel 90%;
        }
        CommandPalette Vertical {
            width: 50;
            max-height: 70%;
            border: thick $primary;
            background: $surface;
            padding: 1;
        }
        CommandPalette ListView {
            height: 1fr;
        }
        CommandPalette Label {
            dock: top;
            padding: 1 2;
            text-style: bold;
            background: $primary;
            color: $text;
        }
        """

        BINDINGS = [
            Binding("escape", "dismiss(None)", "Cancel"),
            Binding("enter", "select", "Select"),
        ]

        def __init__(self, commands: list[Any]) -> None:
            super().__init__()
            self._commands = commands

        def compose(self) -> ComposeResult:
            with Vertical():
                yield Label("Commands (press Enter to select, Esc to cancel)")
                list_view = ListView(id="palette-list")
                for cmd in self._commands:
                    hint = f"  {cmd.arg_hint}" if cmd.arg_hint else ""
                    desc = f"  — {cmd.description}" if cmd.description else ""
                    label = f"/{cmd.name}{hint}{desc}"
                    list_view.append(ListItem(Label(label), name=cmd.name))
                yield list_view

        def on_list_view_selected(self, event: ListView.Selected) -> None:
            if event.item.name is not None:
                self.dismiss(f"/{event.item.name}")

        def action_select(self) -> None:
            list_view = self.query_one(ListView)
            if list_view.index is not None:
                item = list_view.children[list_view.index]
                name = getattr(item, "name", None)
                if name:
                    self.dismiss(f"/{name.name}" if hasattr(name, "name") else f"/{name}")
                else:
                    self.dismiss(None)
            else:
                self.dismiss(None)


# ── File Preview ────────────────────────────────────────────────────


if _TEXTUAL_AVAILABLE:

    class FilePreview(ModalScreen[None]):
        """Modal showing file content."""

        DEFAULT_CSS = """
        FilePreview {
            align: center middle;
            background: $panel 85%;
        }
        FilePreview Vertical {
            width: 70%;
            max-height: 80%;
            border: thick $secondary;
            background: $surface;
            padding: 1;
        }
        FilePreview Label#preview-title {
            dock: top;
            padding: 0 2;
            text-style: bold;
            color: $secondary;
        }
        FilePreview VerticalScroll {
            height: 1fr;
            border: solid $primary-background;
        }
        FilePreview Static#preview-body {
            padding: 1;
        }
        """

        BINDINGS = [
            Binding("escape,q", "dismiss", "Close"),
        ]

        def __init__(self, path: str, content: str) -> None:
            super().__init__()
            self._path = path
            self._content = content

        def compose(self) -> ComposeResult:
            with Vertical():
                yield Label(f"📄 {self._path}", id="preview-title")
                with VerticalScroll():
                    yield Static(self._content, id="preview-body")

        def action_dismiss(self) -> None:
            self.dismiss(None)


# ── Main TUI App ────────────────────────────────────────────────────


if _TEXTUAL_AVAILABLE:

    class ZeroAgentTui(App):
        """ZeroAgent multi-panel Textual terminal interface.

        Three panels:
          - Left:  file browser (toggle with Ctrl+F)
          - Center: chat + input
          - Right: plan sidebar + log footer
        """

        CSS = """
        Screen {
            layout: grid;
            grid-size: 3;
            grid-columns: auto 1fr auto;
            grid-rows: 1fr;
        }

        #left-sidebar {
            width: 28;
            border-right: solid $panel;
            background: $surface;
            display: none;
        }
        #left-sidebar.visible {
            display: block;
        }

        #main-panel {
            height: 100%;
            layout: grid;
            grid-size: 1;
            grid-rows: 1fr auto;
        }

        #chat-container {
            height: 1fr;
            overflow-y: auto;
            padding: 0 1;
        }

        #chat-container Markdown {
            margin: 1 0;
        }

        #input-area {
            dock: bottom;
            height: auto;
            border-top: solid $panel;
            background: $surface;
        }

        #input-area Input {
            width: 100%;
        }

        #status-bar {
            height: 1;
            background: $primary;
            color: $text;
            padding: 0 1;
        }

        #right-panel {
            width: 32;
            border-left: solid $panel;
            background: $surface;
            layout: grid;
            grid-size: 1;
            grid-rows: 1fr auto;
        }

        #plan-panel {
            height: 1fr;
            border-bottom: solid $panel;
        }

        #plan-panel Label#plan-header {
            dock: top;
            height: 1;
            padding: 0 1;
            background: $panel;
            text-style: bold;
        }

        #plan-panel VerticalScroll {
            height: 1fr;
            padding: 0 1;
        }

        #log-panel {
            height: 12;
        }
        #log-panel.hidden {
            height: 0;
            display: none;
        }

        #log-panel Label#log-header {
            dock: top;
            height: 1;
            padding: 0 1;
            background: $panel;
            text-style: bold;
        }

        #left-header {
            dock: top;
            height: 1;
            padding: 0 1;
            background: $panel;
            text-style: bold;
        }

        DirectoryTree {
            height: 1fr;
        }

        .error-text {
            color: $error;
        }

        .user-label {
            color: $secondary;
            text-style: bold;
        }

        .assistant-label {
            color: $primary;
            text-style: bold;
        }
        """

        BINDINGS = [
            Binding("ctrl+q", "quit", "Quit"),
            Binding("ctrl+p", "command_palette", "Commands"),
            Binding("slash", "command_palette", ""),
            Binding("ctrl+l", "toggle_log", "Toggle Log"),
            Binding("ctrl+f", "toggle_files", "Toggle Files"),
        ]

        # ── Init ────────────────────────────────────────────────

        def __init__(self) -> None:
            super().__init__()
            from zero_agent.core.agent import ZeroAgent
            from zero_agent.core.config import load_default_config

            self._za = ZeroAgent  # cached class ref
            self._load_config = load_default_config
            self.agent: Any | None = None
            self._show_files = False
            self._show_log = True
            self._chunk_queue: queue.Queue[Any] = queue.Queue()
            self._streaming_text: str = ""
            self._streaming_markdown: Any | None = None
            self._abort_flag = threading.Event()
            self._command_defs: list[Any] = []
            self._message_log: list[Any] = []

        # ── Compose ─────────────────────────────────────────────

        def compose(self) -> ComposeResult:
            # Left sidebar: file browser
            with Vertical(id="left-sidebar"):
                yield Label("Files", id="left-header")
                if _DIR_TREE_AVAILABLE:
                    yield DirectoryTree(".", id="file-tree")
                else:
                    yield Static("(directory-tree widget unavailable)", classes="error-text")

            # Main panel: chat + input
            with Vertical(id="main-panel"):
                yield Header(show_clock=True)
                yield VerticalScroll(id="chat-container", can_focus=False)
                with Container(id="input-area"):
                    yield Input(placeholder="Type a message or /command...", id="chat-input")
                    yield Static("Ready", id="status-bar")

            # Right panel: plan + log
            with Vertical(id="right-panel"):
                with Vertical(id="plan-panel"):
                    yield Label("Plan", id="plan-header")
                    yield VerticalScroll(id="plan-content", can_focus=False)
                with Vertical(id="log-panel"):
                    yield Label("Log", id="log-header")
                    if _RICH_LOG_AVAILABLE:
                        yield RichLog(id="log-output", highlight=True, markup=True)
                    else:
                        yield Static("(RichLog unavailable)", id="log-output")

        # ── Mount ────────────────────────────────────────────────

        def on_mount(self) -> None:
            """Initialize agent and refresh intervals."""
            config = self._load_config()
            self.agent = self._za(config=config)

            # Load command definitions
            self._load_commands()

            # Periodic polling of chunk queue
            self.set_interval(0.05, self._poll_chunks)

            # Periodic plan refresh
            self.set_interval(1.0, self._refresh_plan)

            # Focus input on start
            self.query_one("#chat-input", Input).focus()

        # ── Keyboard actions ────────────────────────────────────

        def action_command_palette(self) -> None:
            """Open the slash-command palette."""
            if not self._command_defs:
                self.notify("No commands available", severity="warning")
                return
            self.push_screen(
                CommandPalette(self._command_defs),
                self._on_command_selected,
            )

        def _on_command_selected(self, result: str | None) -> None:
            if result is not None:
                inp = self.query_one("#chat-input", Input)
                inp.value = result + " "
                inp.focus()

        def action_toggle_log(self) -> None:
            """Toggle log-panel visibility."""
            log_panel = self.query_one("#log-panel")
            self._show_log = not self._show_log
            if self._show_log:
                log_panel.remove_class("hidden")
            else:
                log_panel.add_class("hidden")

        def action_toggle_files(self) -> None:
            """Toggle file-browser visibility."""
            sidebar = self.query_one("#left-sidebar")
            self._show_files = not self._show_files
            if self._show_files:
                sidebar.add_class("visible")
                self._refresh_file_tree()
            else:
                sidebar.remove_class("visible")

        # ── Chat input ──────────────────────────────────────────

        def on_input_submitted(self, event: Input.Submitted) -> None:
            value = event.value.strip()
            if not value:
                return

            inp = self.query_one("#chat-input", Input)
            inp.clear()

            # Handle slash commands
            if value.startswith("/"):
                self._dispatch_command(value)
                return

            # Regular prompt
            self._run_prompt(value)

        # ── Slash command dispatch ──────────────────────────────

        def _dispatch_command(self, raw: str) -> None:
            """Dispatch a /slash command and show result in chat."""
            try:
                from zero_agent.frontends.commands import handle_command, is_exit_command
            except ImportError:
                self._append_message("system", "Command system unavailable", error=True)
                return

            if is_exit_command(raw):
                self.action_quit()
                return

            self._append_message("user", raw)
            try:
                result = handle_command(raw, self.agent)
            except Exception as exc:
                result = f"Error: {exc}"

            if result and result.strip():
                self._append_message("system", result)

        # ── Agent prompt runner (threaded) ──────────────────────

        def _run_prompt(self, prompt: str) -> None:
            """Start an agent run in a background thread."""
            self._append_message("user", prompt)

            # Clear streaming state
            self._streaming_text = ""
            self._streaming_markdown = None
            self._abort_flag.clear()

            # Launch worker
            threading.Thread(
                target=self._worker_run,
                args=(prompt,),
                daemon=True,
            ).start()

        def _worker_run(self, prompt: str) -> None:
            """Background thread: run agent.run() generator, push chunks to queue."""
            try:
                gen: Generator[Any, None, dict] = self.agent.run(prompt)
                for chunk in gen:
                    if self._abort_flag.is_set():
                        self._chunk_queue.put({"type": "aborted"})
                        try:
                            gen.close()
                        except Exception:
                            pass
                        return
                    self._chunk_queue.put(chunk)
                self._chunk_queue.put({"type": "done"})
            except Exception as exc:
                self._chunk_queue.put({"type": "error", "text": str(exc)})

        # ── Chunk polling ──────────────────────────────────────

        def _poll_chunks(self) -> None:
            """Called by set_interval to drain the chunk queue on the main thread."""
            while not self._chunk_queue.empty():
                chunk = self._chunk_queue.get_nowait()

                if isinstance(chunk, dict):
                    kind = chunk.get("type")
                    if kind == "done":
                        self._finalize_streaming()
                    elif kind == "error":
                        self._append_message("system", chunk.get("text", "Unknown error"), error=True)
                        self._finalize_streaming()
                    elif kind == "aborted":
                        self._append_message("system", "[Interrupted by user]")
                        if self._streaming_markdown:
                            self._streaming_markdown.remove()
                            self._streaming_markdown = None
                    elif kind == "turn":
                        self._update_status(f"Turn {chunk.get('turn', '?')}")
                    # silently skip unknown dicts
                elif isinstance(chunk, str):
                    self._handle_text_chunk(chunk)

        def _handle_text_chunk(self, text: str) -> None:
            """Accumulate streaming text and update visible markdown widget."""
            container = self.query_one("#chat-container", VerticalScroll)

            if self._streaming_markdown is None:
                # Start new streaming message
                self._streaming_text = text
                self._streaming_markdown = Markdown(text, id="streaming-msg")
                container.mount(self._streaming_markdown)
            else:
                self._streaming_text += text
                self._streaming_markdown.update(self._streaming_text)

            # Also feed log
            self._log(text)

            # Auto-scroll
            container.scroll_end(animate=False)

        def _finalize_streaming(self) -> None:
            """Finalize the current streaming message."""
            if self._streaming_markdown is not None:
                self._streaming_markdown.id = ""  # remove streaming marker
                self._streaming_markdown = None
                self._streaming_text = ""
            self._update_status("Ready")

        # ── Message helpers ────────────────────────────────────

        def _append_message(self, role: str, text: str, *, error: bool = False) -> None:
            """Append a non-streaming message to the chat container."""
            container = self.query_one("#chat-container", VerticalScroll)
            if role == "user":
                label = "You"
                cls = "user-label"
                widget = Markdown(f"**{label}:** {text}")
            elif role == "assistant":
                label = "Agent"
                cls = "assistant-label"
                widget = Markdown(f"**{label}:** {text}")
            else:
                cls = "error-text" if error else ""
                widget = Markdown(text)
                if error and cls:
                    widget.add_class(cls)

            container.mount(widget)
            container.scroll_end(animate=False)

        def _log(self, text: str) -> None:
            """Append a snippet to the RichLog panel."""
            if not self._show_log:
                return
            try:
                log = self.query_one("#log-output", RichLog if _RICH_LOG_AVAILABLE else Static)
                if _RICH_LOG_AVAILABLE:
                    log.write(text)  # type: ignore[union-attr]
            except Exception:
                pass

        def _update_status(self, text: str) -> None:
            """Update the status bar."""
            try:
                status = self.query_one("#status-bar", Static)
                status.update(text)
            except Exception:
                pass

        # ── Plan refresh ────────────────────────────────────────

        def _refresh_plan(self) -> None:
            """Periodically refresh the plan sidebar from agent state."""
            try:
                plan_content = self.query_one("#plan-content", VerticalScroll)
            except Exception:
                return

            try:
                from zero_agent.frontends import plan_state
                _PLAN_STATE_AVAILABLE = True
            except ImportError:
                _PLAN_STATE_AVAILABLE = False

            if not _PLAN_STATE_AVAILABLE or self.agent is None:
                return

            # Get message history for plan state analysis
            client = getattr(self.agent, "client", None)
            msgs = getattr(client, "history", None) or []

            plan_content.remove_children()

            try:
                active = plan_state.is_active(self.agent, messages=msgs)
            except Exception:
                active = False

            if not active:
                plan_content.mount(Static("No active plan", classes="error-text"))
                return

            # Try to get plan text from stashed path or messages
            plan_path = plan_state.resolve_path(self.agent, messages=msgs)
            plan_text = ""
            if plan_path and os.path.isfile(plan_path):
                try:
                    with open(plan_path, encoding="utf-8") as f:
                        plan_text = f.read()
                except Exception:
                    pass

            if plan_text:
                items = plan_state.extract(plan_text)
                done_count, total = plan_state.summary(items)
            else:
                items = []
                done_count, total = 0, 0

            # Show summary line
            plan_content.mount(
                Static(f"[{done_count}/{total}] done" if total else "No items")
            )

            # Current step from message history
            step = plan_state.current_step(msgs) if msgs else ""
            if step:
                plan_content.mount(Static(f"Step: {step}"))

            # Items
            for content, status in items:
                glyph = "✓" if status == "done" else "○"
                plan_content.mount(Static(f"  {glyph} {content[:80]}"))

        def _refresh_file_tree(self) -> None:
            """Point the directory tree at the workspace."""
            if not _DIR_TREE_AVAILABLE or not self.agent:
                return
            try:
                tree = self.query_one("#file-tree", DirectoryTree)
                ws = self.agent.config.workspace_dir
                tree.path = str(Path(ws).resolve())
                tree.reload()
            except Exception:
                pass

        def on_directory_tree_file_selected(
            self, event: Any
        ) -> None:
            """Show file content when a file is clicked."""
            path = getattr(event, "path", None)
            if not path or not os.path.isfile(path):
                return
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    content = f.read(20000)
            except Exception as exc:
                content = f"Error reading file: {exc}"

            self.push_screen(FilePreview(str(path), content))

        # ── Command loading ─────────────────────────────────────

        def _load_commands(self) -> None:
            """Load command definitions from the command system."""
            try:
                from zero_agent.frontends.commands import COMMANDS
                self._command_defs = list(COMMANDS)
            except ImportError:
                self._command_defs = []

        # ── Quit handling ──────────────────────────────────────

        def action_quit(self) -> None:
            """Abort any running job and exit."""
            self._abort_flag.set()
            if self.agent is not None:
                try:
                    self.agent.abort()
                except Exception:
                    pass
            self.exit()


# ── Entry point ────────────────────────────────────────────────────


def main() -> None:
    """Start the ZeroAgent TUI, or print install hint if deps missing."""
    if not _TEXTUAL_AVAILABLE:
        print("Install zero-agent[ui] to use the TUI: textual")
        import sys
        sys.exit(1)

    try:
        from zero_agent.core.agent import ZeroAgent
        from zero_agent.core.config import load_default_config
    except ImportError as e:
        print(f"Missing dependency: {e}")
        print("Install zero-agent[ui] to use the TUI")
        import sys
        sys.exit(1)

    app = ZeroAgentTui()
    app.run()


if __name__ == "__main__":
    main()
