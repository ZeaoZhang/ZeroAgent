"""ZeroAgent bot frontends — Telegram, Discord, WeChat, QQ, WeCom, Feishu, DingTalk.

Each bot wraps a ZeroAgent + AgentRunner and exposes a platform-specific
messaging interface (polling / webhook / websocket).

Shared infrastructure lives in common.py (AgentBotMixin) and the command
modules (continue_cmd, btw_cmd, review_cmd, export_cmd, session_names).
"""
