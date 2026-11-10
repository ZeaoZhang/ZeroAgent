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
import shutil
import threading
import warnings
from pathlib import Path
from typing import Any, Optional

from zero_agent.core.types import TaskMode, TerminalEvent, TerminalStatus
from zero_agent.runners.agent_runner import _consume_agent_run


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
def _waiting_text(data: Any, fallback: str) -> str:
    payload = data if isinstance(data, dict) else {}
    nested = payload.get("data")
    if isinstance(nested, dict):
        payload = nested
    question = str(payload.get("question") or fallback)
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return question
    options = "\n".join(f"- {candidate}" for candidate in candidates)
    return f"{question}\n\n{options}"


def _terminal_view(event: dict[str, Any]) -> tuple[str, str, bool]:
    """Return (ui status, message, is_error) for a terminal queue item."""
    status = event.get("status")
    reason = event.get("reason") or status or "unknown"
    text = event.get("text") or ""
    if status == TerminalStatus.COMPLETED.value:
        return "Ready", text, False
    if status == TerminalStatus.WAITING.value:
        return "Waiting for input", _waiting_text(event.get("data"), text or reason), False
    if status == TerminalStatus.CANCELLED.value:
        return "Cancelled", text or reason, False
    if status == TerminalStatus.BUDGET_EXHAUSTED.value:
        message = text or f"Reached the turn/retry budget; task not completed ({reason})"
        return "Error", message, True
    return "Error", text or reason, True





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
            self._plan_ready_notified = False
            self._run_reservation_lock = threading.Lock()
            self._run_reserved = False

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
            if self._try_dispatch_plan(raw):
                return

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

        def _try_dispatch_plan(self, raw: str) -> bool:
            """Handle ``/plan <task>`` / ``/plan execute``; True if consumed."""
            stripped = raw.strip()
            if not stripped.startswith("/"):
                return False
            parts = stripped[1:].split(maxsplit=1)
            if not parts or parts[0].lower() != "plan":
                return False

            from zero_agent.frontends.commands.slash_commands import (
                has_active_plan,
                resolve_pending_plan,
                resolve_ready_plan,
            )
            from zero_agent.frontends.plan_command import create_plan_workspace

            arg = parts[1].strip() if len(parts) > 1 else ""

            if not arg:
                self._append_message("user", raw)
                self._append_message(
                    "system",
                    "用法: /plan <task> 创建计划工作区；/plan execute 执行已就绪的计划",
                )
                return True

            if not self._reserve_run():
                self._append_message("user", raw)
                self._append_message("system", "当前有任务正在运行，请先等待完成", error=True)
                return True

            try:
                if arg.lower() == "execute":
                    pending = resolve_pending_plan(self.agent)
                    if pending is not None:
                        task, mode, plan_path = pending
                    else:
                        ready = resolve_ready_plan(self.agent)
                        if ready is None:
                            self._append_message("user", raw)
                            self._append_message("system", "没有就绪的计划可执行", error=True)
                            self._release_run_reservation()
                            return True
                        task, plan_path = ready
                        mode = TaskMode.EXECUTING
                    self._append_message("user", raw)
                    self._run_prompt(
                        task,
                        initial_mode=mode,
                        plan_path=plan_path,
                        display_user=False,
                        reserved=True,
                    )
                    return True

                if has_active_plan(self.agent):
                    self._append_message("user", raw)
                    self._append_message("system", "已存在活动计划，请先 /plan execute", error=True)
                    self._release_run_reservation()
                    return True
            except Exception:
                self._release_run_reservation()
                raise

            try:
                workspace = create_plan_workspace(self.agent.config.workspace_dir, arg)
                self._append_message("user", raw)
                self._run_prompt(
                    arg,
                    initial_mode=TaskMode.PLAN,
                    plan_path=workspace.path,
                    workspace_directory=workspace.directory,
                    display_user=False,
                    reserved=True,
                )
            except Exception:
                self._release_run_reservation()
                raise
            return True

        def _reserve_run(self) -> bool:
            """Atomically reserve the single Agent.run slot until worker exit."""
            lock = getattr(self, "_run_reservation_lock", None)
            if lock is None:
                lock = threading.Lock()
                self._run_reservation_lock = lock
            with lock:
                if getattr(self, "_run_reserved", False) or getattr(self.agent, "_is_running_task", False):
                    return False
                self._run_reserved = True
                return True

        def _release_run_reservation(self) -> None:
            """Release a prompt reservation acquired before the worker starts."""
            lock = getattr(self, "_run_reservation_lock", None)
            if lock is None:
                self._run_reserved = False
                return
            with lock:
                self._run_reserved = False

        # ── Agent prompt runner (threaded) ──────────────────────

        def _run_prompt(
            self,
            prompt: str,
            *,
            initial_mode: TaskMode = TaskMode.OPEN,
            plan_path: Optional[str] = None,
            workspace_directory: Optional[str] = None,
            display_user: bool = True,
            reserved: bool = False,
        ) -> None:
            """Start an agent run in a background thread."""
            if not reserved and not self._reserve_run():
                if display_user:
                    self._append_message("user", prompt)
                self._append_message("system", "当前有任务正在运行，请先等待完成", error=True)
                return

            if display_user:
                self._append_message("user", prompt)

            # Clear streaming state
            self._streaming_text = ""
            self._streaming_markdown = None
            self._abort_flag.clear()

            # Launch worker
            try:
                threading.Thread(
                    target=self._worker_run,
                    args=(prompt,),
                    kwargs={
                        "initial_mode": initial_mode,
                        "plan_path": plan_path,
                        "workspace_directory": workspace_directory,
                    },
                    daemon=True,
                ).start()
            except Exception:
                self._release_run_reservation()
                raise

        def _worker_run(
            self,
            prompt: str,
            *,
            initial_mode: TaskMode = TaskMode.OPEN,
            plan_path: Optional[str] = None,
            workspace_directory: Optional[str] = None,
        ) -> None:
            """Background thread: run agent.run() and retain its terminal return."""
            agent = self.agent
            baseline_handler = getattr(agent, "handler", None)
            baseline_contract = getattr(baseline_handler, "task_contract", None)
            baseline_task_id = getattr(baseline_contract, "task_id", None)
            baseline_plan_path = getattr(baseline_contract, "plan_path", None)
            adopted_plan_path = False

            def observe_plan_adoption(*, permit_replaced_handler: bool = True) -> None:
                nonlocal adopted_plan_path
                if adopted_plan_path or not plan_path:
                    return
                if self.agent is not agent:
                    return
                handler = getattr(agent, "handler", None)
                contract = getattr(handler, "task_contract", None)
                if contract is None or getattr(contract, "plan_path", None) != plan_path:
                    return
                if handler is not baseline_handler and not permit_replaced_handler:
                    return
                if (
                    handler is not baseline_handler
                    or getattr(contract, "task_id", None) != baseline_task_id
                    or baseline_plan_path == plan_path
                ):
                    adopted_plan_path = True

            def tracked_gen(gen):
                yielded_chunk = False
                try:
                    while True:
                        try:
                            chunk = next(gen)
                        except StopIteration as stop:
                            observe_plan_adoption(
                                permit_replaced_handler=not yielded_chunk
                            )
                            return stop.value
                        observe_plan_adoption()
                        yielded_chunk = True
                        yield chunk
                except Exception:
                    observe_plan_adoption()
                    raise

            try:
                try:
                    gen = agent.run(
                        prompt,
                        initial_mode=initial_mode,
                        plan_path=plan_path,
                    )
                except Exception as exc:
                    terminal = TerminalEvent(
                        status=TerminalStatus.FAILED,
                        reason=type(exc).__name__,
                        text=str(exc),
                    )
                else:
                    terminal = _consume_agent_run(
                        tracked_gen(gen),
                        self._chunk_queue.put,
                        cancel_event=self._abort_flag,
                    )
                cleanup_warning = self._cleanup_unadopted_workspace(
                    workspace_directory,
                    plan_path,
                    adopted_plan_path,
                )
                payload = terminal.to_dict()
                if cleanup_warning:
                    payload["data"] = self._terminal_data_with_cleanup_warning(
                        payload.get("data"), cleanup_warning
                    )
                self._chunk_queue.put(payload)
            finally:
                self._release_run_reservation()

        def _cleanup_unadopted_workspace(
            self,
            workspace_directory: Optional[str],
            plan_path: Optional[str],
            adopted_plan_path: bool,
        ) -> Optional[str]:
            """Delete a workspace created by this run until this run adopts it.

            Cleanup is limited to the directory created by this call. Adoption
            is a run-local fact captured while consuming this run's generator,
            so a later handler replacement cannot make cleanup delete an
            adopted workspace or preserve an unadopted one by coincidence.
            """
            if not workspace_directory or not plan_path or adopted_plan_path:
                return None
            try:
                shutil.rmtree(workspace_directory)
            except Exception as exc:
                message = (
                    f"failed to clean unadopted plan workspace "
                    f"{workspace_directory!r}: {exc}"
                )
                try:
                    warnings.warn(message, RuntimeWarning, stacklevel=2)
                except Exception:
                    return message
            return None

        @staticmethod
        def _terminal_data_with_cleanup_warning(data: Any, warning: str) -> Any:
            if data is None:
                return {"cleanup_warning": warning}
            if isinstance(data, dict):
                enriched = dict(data)
                enriched["cleanup_warning"] = warning
                return enriched
            return {"value": data, "cleanup_warning": warning}


        # ── Chunk polling ──────────────────────────────────────

        def _poll_chunks(self) -> None:
            """Called by set_interval to drain the chunk queue on the main thread."""
            while not self._chunk_queue.empty():
                chunk = self._chunk_queue.get_nowait()

                if isinstance(chunk, dict):
                    kind = chunk.get("type")
                    if kind == "terminal":
                        status = chunk.get("status")
                        ui_status, message, is_error = _terminal_view(chunk)
                        if status == TerminalStatus.COMPLETED.value:
                            if message and not self._streaming_text:
                                self._handle_text_chunk(message)
                            self._finalize_streaming()
                        elif status == TerminalStatus.WAITING.value:
                            self._finalize_streaming()
                            self._append_message("system", message)
                            self._update_status(ui_status)
                        elif status == TerminalStatus.CANCELLED.value:
                            self._append_message("system", "[Interrupted by user]")
                            if self._streaming_markdown:
                                self._streaming_markdown.remove()
                                self._streaming_markdown = None
                            self._streaming_text = ""
                            self._update_status(ui_status)
                        else:
                            self._append_message("system", message, error=is_error)
                            self._finalize_streaming()
                            self._update_status(ui_status)
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
            self._maybe_notify_plan_ready()



            plan_content.remove_children()

            try:
                active = plan_state.is_active(self.agent)
            except Exception:
                active = False

            if not active:
                plan_content.mount(Static("No active plan", classes="error-text"))
                return

            # Read the path from the current PLAN TaskContract.
            plan_path = plan_state.resolve_path(self.agent)
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
            step = plan_state.current_step(getattr(getattr(self.agent, "client", None), "history", None) or [])
            if step:
                plan_content.mount(Static(f"Step: {step}"))

            # Items
            for content, status in items:
                glyph = "✓" if status == "done" else "○"
                plan_content.mount(Static(f"  {glyph} {content[:80]}"))

        def _maybe_notify_plan_ready(self) -> None:
            """Notify once when the plan becomes ready; never auto-execute.

            A plan is ready only when the live TaskContract is still in PLAN
            mode and the completion evaluator has certified it. The user must
            still run ``/plan execute`` themselves.
            """
            if self.agent is None:
                return
            try:
                from zero_agent.frontends.commands.slash_commands import (
                    resolve_ready_plan,
                )
                ready = resolve_ready_plan(self.agent)
            except Exception:
                return
            if ready is None:
                self._plan_ready_notified = False
                return
            if self._plan_ready_notified:
                return
            self._plan_ready_notified = True
            self._append_message(
                "system",
                "计划已就绪并已通过验证。请输入 /plan execute 开始执行（不会自动执行）。",
            )

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
