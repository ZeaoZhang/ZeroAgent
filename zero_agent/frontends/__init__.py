"""ZeroAgent 前端模块.

提供多种用户界面入口:
    - stapp.py: Streamlit Web 聊天界面
    - conductor.py: 多 Agent 编排器 Web UI (FastAPI + WebSocket)
    - acp_bridge.py: Agent Communication Protocol v1 JSON-RPC bridge
    - desktop_bridge.py: HTTP/WS session management server
    - launch.pyw: 桌面启动器 (pywebview)
    - hub.pyw: 服务发现面板
    - desktop_pet.pyw: Desktop Pet 主版本
    - desktop_pet_basic.pyw: Desktop Pet 精简版
    - launcher.py: 命令行启动器
    - skins/: Desktop Pet 皮肤资源
    - desktop/: Tauri v2 桌面应用
"""
