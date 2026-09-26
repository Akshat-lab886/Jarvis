"""
Configuration sanity checks surfaced at startup via the dashboard socket.

Catches silent contradictions and locked-down features that would otherwise
only appear in log files — the user discovers them the hard way.
"""

import os
import logging

logger = logging.getLogger("Jarvis.ConfigDiagnostics")


def run_diagnostics():
    """
    Run all config sanity checks.  Returns a list of warning/notice lines
    to emit to the dashboard ai_text socket.  Never raises — any check
    that throws is skipped silently (logged at debug level).
    """
    lines = []
    try:
        lines.extend(_check_auto_upgrade_contradiction())
    except Exception as e:
        logger.debug("auto-upgrade contradiction check skipped: %s", e)

    try:
        lines.extend(_check_telegram_locked_down())
    except Exception as e:
        logger.debug("telegram locked-down check skipped: %s", e)

    try:
        lines.extend(_check_browser_driver())
    except Exception as e:
        logger.debug("browser driver check skipped: %s", e)

    try:
        lines.extend(_check_ollama_url_misuse())
    except Exception as e:
        logger.debug("ollama URL misuse check skipped: %s", e)

    return lines


def _check_auto_upgrade_contradiction():
    """
    JARVIS_AUTO_UPGRADE=1 triggers pip-install of capability deps on demand,
    but JARVIS_CAPABILITY_CHECK=0 disables the capability system entirely.
    The user sees no capability panel, the startup report is silenced, and
    the LLM never receives capability hints — the auto-install runs but the
    benefit is invisible.  Flag this contradiction.
    """
    auto_upgrade = os.getenv('JARVIS_AUTO_UPGRADE', '0') == '1'
    cap_check = os.getenv('JARVIS_CAPABILITY_CHECK', '1') != '0'
    if auto_upgrade and not cap_check:
        return [
            "⚠️  Configuration contradiction: JARVIS_AUTO_UPGRADE=1 is set "
            "but JARVIS_CAPABILITY_CHECK=0 disables the capability system. "
            "Packages install silently but the dashboard shows no capabilities "
            "and the LLM gets no hints.  Set JARVIS_CAPABILITY_CHECK=1 "
            "(or remove it) to see the capability panel and startup report."
        ]
    return []


def _check_telegram_locked_down():
    """
    Telegram bot is initialized but all remote commands will be denied
    because TELEGRAM_ALLOWED_IDS is empty.  Warn so the user doesn't
    spend time trying to control Jarvis via Telegram with no response.
    """
    from config import Config
    has_token = bool(getattr(Config, 'TELEGRAM_TOKEN', '') or
                      os.getenv('TELEGRAM_TOKEN', ''))
    allowed_ids = getattr(Config, 'TELEGRAM_ALLOWED_IDS', []) or []
    if has_token and not allowed_ids:
        return [
            "⚠️  Telegram bot is running in LOCKED-DOWN mode — "
            "all remote commands are denied because TELEGRAM_ALLOWED_IDS "
            "is empty.  Add your numeric Telegram user ID to .env to "
            "enable control.  Example: TELEGRAM_ALLOWED_IDS=123456789"
        ]
    return []


def _check_browser_driver():
    """
    The browser automation capability probe only checks 'import playwright'.
    The actual browser session needs the playwright node driver binary
    (playwright install chromium).  If the driver is missing, the user
    sees 'browser: ready' but any actual browser task crashes silently.
    Warn proactively when the node binary is absent.
    """
    try:
        import playwright
        driver_path = os.path.join(
            os.path.dirname(playwright.__file__),
            'driver', 'node')
        if not os.path.exists(driver_path):
            return [
                "⚠️  Playwright Python package installed but the browser "
                "driver binary is missing — run: playwright install chromium. "
                "Browser automation will fail until the Chromium browser is "
                "downloaded.  Web search and static extraction still work fine."
            ]
    except ImportError:
        pass  # playwright not installed at all — handled by capability system
    return []


def _check_ollama_url_misuse():
    """
    OLLAMA_BASE_URL must NOT include /v1/chat/completions — it is the base
    URL only (http://localhost:11434).  The OpenAI compat layer appends
    /v1/chat/completions automatically.  If the user accidentally appended
    /v1 or /api, the provider will appear online but every request 404s.
    Catch this common mistake.
    """
    url = (os.getenv('OLLAMA_BASE_URL') or '').strip()
    if not url or url.lower() in ('off', 'none', 'disabled'):
        return []
    bad_suffixes = ('/v1', '/v1/chat/completions', '/api', '/chat/completions')
    if any(url.rstrip('/').endswith(s) for s in bad_suffixes):
        return [
            f"⚠️  OLLAMA_BASE_URL='{url}' should be the base URL only "
            f"(e.g. http://localhost:11434).  The /v1/chat/completions "
            f"path is added automatically.  Fix this to use local models."
        ]
    return []
