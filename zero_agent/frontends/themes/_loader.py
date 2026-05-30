"""Theme CSS loader — reads .css files at import time with caching."""

import os
from functools import lru_cache

_THEMES_DIR = os.path.dirname(os.path.abspath(__file__))


@lru_cache(maxsize=8)
def _read_css(filename: str) -> str:
    """Read a .css file from the themes directory, cached in memory."""
    path = os.path.join(_THEMES_DIR, filename)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def load_theme_css(theme: str = "light", app: str = "stapp") -> str:
    """Compose a full <style> block from theme vars + shared UI + app overrides.

    For "light" and "dark" themes, the correct :root variables are loaded directly
    and applied via st.rerun() — no JS attribute toggling needed.
    For "auto", light vars are the default and JS sets [data-theme="dark"] on the
    iframe's <html> when prefers-color-scheme is dark.

    Args:
        theme: "light", "dark", or "auto"
        app: "stapp" or "stapp2"

    Returns:
        Complete <style>...</style> string for Streamlit injection.
    """
    if theme == "dark":
        vars_file = "dark_vars.css"
    else:
        vars_file = "light_vars.css"

    overrides_file = f"{app}_overrides.css"

    css_parts = [
        _read_css(vars_file),
        _read_css("chat_ui.css"),
        _read_css(overrides_file),
    ]
    return f"<style>\n{''.join(css_parts)}</style>"


def theme_toggle_js() -> str:
    """JavaScript for theme toggle via iframe's own DOM + localStorage.

    Must target document.documentElement (the iframe's <html>), NOT
    window.parent.document — CSS [data-theme] selectors can't cross iframe boundaries.

    Supports three modes: "light", "dark", "auto" (follows prefers-color-scheme).
    """
    return """<script>
(function(){
    var doc = document;
    function apply(t) {
        if (t === 'auto') {
            t = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
        }
        doc.documentElement.setAttribute('data-theme', t);
    }
    function saved() {
        try { return localStorage.getItem('za-theme'); } catch(e) { return null; }
    }
    function save(t) {
        try { localStorage.setItem('za-theme', t); } catch(e) {}
    }
    var current = saved() || 'light';
    apply(current);
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function(e) {
        if (saved() === 'auto') apply(e.matches ? 'dark' : 'light');
    });
    window.__zaSetTheme = function(t) { save(t); apply(t); };
})();
</script>"""
