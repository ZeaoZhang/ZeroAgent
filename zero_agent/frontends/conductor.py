"""ZeroAgent Conductor — multi-subagent orchestration UI backend.

FastAPI backend serving conductor.html. Manages subagent lifecycle via
the --task file I/O protocol, with websocket broadcast for real-time updates.

Usage:
    python -m zero_agent.frontends.conductor --port 8900
    zero-agent-conductor --host 127.0.0.1 --port 8900
"""

import asyncio
import json
import os
import queue
import re
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "conductor.html")

HOST = "127.0.0.1"
PORT = 8900


# ---- data models ----

class ChatIn(BaseModel):
    msg: str
    role: str = "conductor"


class StartSubagentIn(BaseModel):
    prompt: str


class ApprovalIn(BaseModel):
    prompt: str
    source: str = ""


class SubagentActionIn(BaseModel):
    action: str = "intervene"  # intervene | abort | kill
    msg: str = ""


@dataclass
class SubAgentState:
    id: str
    prompt: str
    status: str = "running"  # running | done | stopped | error
    partial: str = ""
    result: str = ""
    error: str = ""
    task_dir: str = ""
    created_at: int = field(default_factory=lambda: int(time.time()))
    updated_at: int = field(default_factory=lambda: int(time.time()))


# ---- state ----

ws_clients: Set[WebSocket] = set()
main_loop: Optional[asyncio.AbstractEventLoop] = None
chat_messages: List[Dict[str, Any]] = []
subagent_counter: int = 0


def now_ms() -> int:
    return int(time.time() * 1000)


def short_id() -> str:
    return uuid.uuid4().hex[:8]


_TURN_SPLIT_RE = re.compile(r"\**LLM Running \(Turn \d+\) \.\.\.\**")
_SUMMARY_RE = re.compile(r"<summary>(.*?)</summary>\s*", re.DOTALL)


def extract_last_summary(full: str) -> str:
    """Extract the latest <summary> content for in-progress display."""
    if not full:
        return ""
    summaries = _SUMMARY_RE.findall(full)
    if summaries:
        s = summaries[-1].strip()
        return s[-1000:] if len(s) > 1000 else s
    s = full.strip()
    return s[-1000:] if len(s) > 1000 else s
def clean_log_text(s: str) -> str:
    if not s:
        return s
    s = _TURN_SPLIT_RE.sub("", s)
    s = re.sub(r"```[\s\S]*?```", "", s)
    return s.strip()


def schedule_broadcast(payload: dict):
    if main_loop and main_loop.is_running():
        asyncio.run_coroutine_threadsafe(broadcast(payload), main_loop)


async def broadcast(payload: dict):
    dead = []
    for ws in ws_clients:
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        ws_clients.discard(ws)


def add_chat(msg: str, role: str = "conductor") -> dict:
    item = {"id": short_id(), "role": role, "msg": msg, "ts": now_ms()}
    chat_messages.append(item)
    if len(chat_messages) > 200:
        chat_messages[:] = chat_messages[-200:]
    schedule_broadcast({"type": "chat", "items": chat_messages[-20:]})
    return item


# ---- subagent lifecycle ----

class SubagentPool:
    def __init__(self):
        self.items: Dict[str, SubAgentState] = {}
        self._lock = threading.Lock()

    def snapshot(self) -> List[dict]:
        with self._lock:
            return [
                {
                    "id": s.id,
                    "prompt": s.prompt[:500],
                    "status": s.status,
                    "partial": s.partial[-2000:],
                    "result": s.result[-2000:],
                    "error": s.error,
                    "created_at": s.created_at,
                    "updated_at": s.updated_at,
                }
                for s in self.items.values()
            ]

    def start_subagent(self, prompt: str) -> dict:
        global subagent_counter
        subagent_counter += 1
        sid = f"sa_{subagent_counter}"
        task_dir = os.path.join(
            ROOT, "workspace", "conductor_tasks", sid
        )
        os.makedirs(task_dir, exist_ok=True)

        state = SubAgentState(
            id=sid,
            prompt=prompt,
            task_dir=task_dir,
        )
        with self._lock:
            self.items[sid] = state

        # Write input.txt for the subagent
        input_path = os.path.join(task_dir, "input.txt")
        with open(input_path, "w", encoding="utf-8") as f:
            f.write(prompt)

        # Launch subagent process
        import subprocess

        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "zero_agent.runners.cli",
                "--task", task_dir,
                "--nobg",
                "-q",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # Background monitor thread
        t = threading.Thread(
            target=self._monitor, args=(sid, task_dir, proc), daemon=True
        )
        t.start()

        schedule_broadcast({"type": "subagents", "items": self.snapshot()})
        return {"id": sid, "status": "started"}

    def _monitor(self, sid, task_dir, proc):
        """Monitor subagent process, collect output, and update state."""
        try:
            stdout, stderr = proc.communicate(timeout=3600)
            exit_code = proc.returncode
        except Exception:
            exit_code = -1
            stdout = ""
            stderr = str(sys.exc_info()[1])

        with self._lock:
            state = self.items.get(sid)
            if state:
                # Read output
                out_path = os.path.join(task_dir, "output.txt")
                if os.path.isfile(out_path):
                    with open(out_path, "r", encoding="utf-8") as f:
                        state.result = f.read()

                if exit_code == 0 and state.result:
                    state.status = "done"
                else:
                    state.status = "error"
                    state.error = stderr[:2000] if stderr else f"exit code {exit_code}"

                state.updated_at = int(time.time())

        schedule_broadcast({"type": "subagents", "items": self.snapshot()})

    def get(self, sid: str) -> Optional[SubAgentState]:
        with self._lock:
            return self.items.get(sid)

    def intervene(self, sid: str, msg: str) -> dict:
        state = self.get(sid)
        if not state:
            return {"error": f"unknown subagent: {sid}"}
        interv_path = os.path.join(state.task_dir, "_intervene")
        with open(interv_path, "w", encoding="utf-8") as f:
            f.write(msg)
        return {"id": sid, "status": "keyinfo_injected"}

    def abort(self, sid: str) -> dict:
        state = self.get(sid)
        if not state:
            return {"error": f"unknown subagent: {sid}"}
        stop_path = os.path.join(state.task_dir, "_stop")
        with open(stop_path, "w", encoding="utf-8") as f:
            f.write("abort")
        return {"id": sid, "status": "stop_signal_written"}


pool = SubagentPool()


# ---- Conductor orchestration ----

INSTR_DISPATCHED = (
    "Task received. I'll handle THIS TASK from here. You MUST do other task or end your reply."
)


class Conductor:
    def __init__(self):
        self.running = False

    def start(self):
        self.running = True

    def notify(self, event: dict):
        schedule_broadcast({"type": "conductor_event", "event": event})


conductor = Conductor()


# ---- lifespan ----

@asynccontextmanager
async def lifespan(app: FastAPI):
    global main_loop
    main_loop = asyncio.get_running_loop()
    conductor.start()
    yield
    conductor.running = False


# ---- FastAPI app ----

app = FastAPI(title="ZeroAgent Conductor", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def index():
    return FileResponse(HTML_PATH)


@app.get("/chat")
def api_get_chat():
    return {"items": chat_messages[-20:]}


@app.post("/chat")
def api_chat(body: ChatIn):
    return add_chat(body.msg, role=body.role)


@app.get("/subagent")
def list_subagents():
    return {"items": pool.snapshot()}


@app.get("/subagent/{sid}")
def get_subagent(sid: str):
    s = pool.get(sid)
    if not s:
        return JSONResponse({"error": f"unknown subagent: {sid}"}, status_code=404)
    return {
        "id": s.id,
        "prompt": s.prompt[:500],
        "status": s.status,
        "partial": s.partial[-2000:],
        "result": s.result[-2000:],
        "error": s.error,
        "created_at": s.created_at,
        "updated_at": s.updated_at,
    }


@app.post("/subagent/start")
def api_start_subagent(body: StartSubagentIn):
    result = pool.start_subagent(body.prompt)
    result["instruction"] = INSTR_DISPATCHED
    return result


@app.post("/subagent/{sid}/action")
def api_subagent_action(sid: str, body: SubagentActionIn):
    s = pool.get(sid)
    if not s:
        return JSONResponse({"error": f"unknown subagent: {sid}"}, status_code=404)

    if body.action == "intervene":
        return pool.intervene(sid, body.msg)
    elif body.action == "abort":
        return pool.abort(sid)
    elif body.action == "stop":
        return pool.abort(sid)
    else:
        return JSONResponse({"error": f"unknown action: {body.action}"}, status_code=400)


@app.websocket("/ws")
async def websocket(ws: WebSocket):
    await ws.accept()
    ws_clients.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        ws_clients.discard(ws)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="ZeroAgent Conductor")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()

    import uvicorn

    uvicorn.run("zero_agent.frontends.conductor:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
