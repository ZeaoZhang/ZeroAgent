"""ZeroAgent Theme System.

Provides CSS loading for Streamlit apps and color definitions for Qt apps.

Usage (Streamlit):
    from zero_agent.frontends.themes import load_theme_css, theme_toggle_js

    css = load_theme_css("light", "stapp")
    st.markdown(css, unsafe_allow_html=True)
    st.markdown(theme_toggle_js(), unsafe_allow_html=True)

Usage (Qt):
    from zero_agent.frontends.themes import C_DARK, C_LIGHT, QSS_TEMPLATE, MD_CSS
"""

from zero_agent.frontends.themes._loader import load_theme_css, theme_toggle_js

__all__ = ["load_theme_css", "theme_toggle_js"]
