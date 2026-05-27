"""ZeroAgent 桌面启动器 — pywebview 本地 app 包装.

将 Streamlit 前端包装为独立桌面窗口，提供原生 app 体验.

依赖: pip install pywebview streamlit

用法:
    zero-agent-launcher                 # 桌面窗口模式
    python zero_agent/frontends/launcher.py
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time


def _find_free_port(start: int = 8501) -> int:
    """查找可用端口.

    Args:
        start: 起始端口号.

    Returns:
        可用端口号.
    """
    import socket

    for port in range(start, start + 100):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    return start


def _run_streamlit(port: int) -> None:
    """在后台线程启动 Streamlit 服务.

    Args:
        port: Streamlit 监听端口.
    """
    stapp_path = os.path.join(os.path.dirname(__file__), "stapp.py")

    # 设置 Streamlit 命令行参数
    sys.argv = [
        "streamlit", "run", stapp_path,
        "--server.headless", "true",
        "--server.port", str(port),
        "--browser.serverAddress", "127.0.0.1",
        "--server.enableCORS", "false",
        "--server.enableXsrfProtection", "false",
    ]

    try:
        import streamlit.web.bootstrap as st_bootstrap
        st_bootstrap.run(stapp_path, "", [], flag_options={})
    except ImportError:
        print("[Error] Streamlit not installed. Install with: pip install streamlit")
        sys.exit(1)


def main() -> None:
    """启动 pywebview 桌面窗口."""
    try:
        import webview
    except ImportError:
        print("[Error] pywebview not installed. Install with: pip install pywebview")
        print("Alternatively, run the Streamlit app directly:")
        print("  streamlit run zero_agent/frontends/stapp.py")
        sys.exit(1)

    port = _find_free_port()

    # 后台启动 Streamlit
    streamlit_thread = threading.Thread(
        target=_run_streamlit, args=(port,), daemon=True
    )
    streamlit_thread.start()

    # 等待 Streamlit 就绪
    url = f"http://127.0.0.1:{port}"
    print(f"Starting ZeroAgent desktop app on {url}...")

    for _ in range(30):
        try:
            import urllib.request

            urllib.request.urlopen(f"{url}/_stcore/health", timeout=1)
            break
        except Exception:
            time.sleep(0.5)

    # 创建 pywebview 窗口
    webview.create_window(
        title="ZeroAgent",
        url=url,
        width=1200,
        height=800,
        min_size=(800, 600),
    )

    print(f"ZeroAgent desktop app ready at {url}")
    webview.start()

    # 窗口关闭后清理
    os.kill(os.getpid(), signal.SIGINT)


if __name__ == "__main__":
    main()
