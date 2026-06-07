"""ZeroAgent desktop launcher wrapping the shared Web2 frontend with pywebview."""

from __future__ import annotations

import contextlib
import os
import signal
import sys
import threading
import time
import urllib.request


HOST = os.environ.get("BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("BRIDGE_PORT", "14168"))


def _bridge_url() -> str:
    return f"http://{HOST}:{PORT}/"


def _run_bridge() -> None:
    os.environ["ZA_DESKTOP_BRIDGE_NO_BROWSER"] = "1"
    from aiohttp import web
    from zero_agent.frontends.desktop_bridge import create_app

    web.run_app(create_app(), host=HOST, port=PORT, print=None, handle_signals=False)


def _is_bridge_ready(url: str) -> bool:
    try:
        urllib.request.urlopen(url, timeout=1).close()
        return True
    except Exception:
        return False


def _wait_for_bridge(url: str) -> None:
    for _ in range(60):
        if _is_bridge_ready(url):
            return
        time.sleep(0.5)


def main() -> None:
    try:
        import webview
    except ImportError:
        print("[Error] pywebview not installed. Install with: pip install 'zero-agent[ui]'")
        print("Alternatively run: python -m zero_agent.frontends.desktop_bridge")
        sys.exit(1)

    url = _bridge_url()
    if not _is_bridge_ready(url):
        threading.Thread(target=_run_bridge, daemon=True).start()
    _wait_for_bridge(url)

    webview.create_window(
        title="ZeroAgent",
        url=url,
        width=1200,
        height=800,
        min_size=(800, 600),
    )
    webview.start()

    with contextlib.suppress(Exception):
        os.kill(os.getpid(), signal.SIGINT)


if __name__ == "__main__":
    main()
