"""Compatibility alias for legacy frontend bare imports."""

from zero_agent.frontends.chatapp_common import *  # noqa: F401,F403
from zero_agent.frontends.chatapp_common import (  # noqa: F401
    _handle_continue_frontend,
    _reset_conversation,
    _restore_native_history,
    _restore_text_pairs,
)
