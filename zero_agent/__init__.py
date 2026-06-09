"""ZeroAgent - A clean, reusable autonomous agent framework."""

from __future__ import annotations

import os


def _configure_litellm_defaults() -> None:
    """Set quiet LiteLLM defaults before any litellm import."""
    os.environ.setdefault("LITELLM_LOG", "ERROR")


_configure_litellm_defaults()

__version__ = "0.1.0"
