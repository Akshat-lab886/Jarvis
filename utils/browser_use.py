"""
J.A.R.V.I.S. — Browser Use facade (Hermes parity)
=================================================

Unified, tiered web automation behind one entry point:

    tier 1  static extract  — requests + BeautifulSoup (web_reader):
            instant, no browser, works headless anywhere
    tier 2  live browser    — the Playwright WebAgent (executor.agent):
            real clicks/typing, DOM reading, SCREENSHOTS, Chrome CDP
            with a persistent profile
    tier 3  remote gateway  — a configured tool gateway (see
            utils/tool_gateway.py) for cloud browsers / scraping APIs

The executor action ``browser_use`` dispatches here; the legacy
agent_* actions keep working unchanged.  ``JARVIS_BROWSER_USE=0``
forces tier 1 only.
"""

import os
import logging

logger = logging.getLogger("Jarvis.BrowserUse")

_TIMEOUT = 25


def enabled():
    return os.getenv('JARVIS_BROWSER_USE', '1') != '0'


def _extract_static(url):
    """Tier 1: fetch + strip to readable text (no browser launched)."""
    try:
        from utils.web_reader import extract_page_content
        return extract_page_content(url)
    except Exception as e:
        return f"[static extract failed: {e}]"


def _agent():
    """The live browser agent (executor's shared WebAgent instance)."""
    try:
        from utils.server import executor
        return executor.agent
    except Exception:
        return None


def browse(url, mode='auto'):
    """
    Navigate to *url* and return readable content.
    mode: 'auto' (static first, browser on failure/emptiness),
          'static', or 'browser' (forces the live agent).
    """
    url = str(url or '').strip()
    if not url:
        return "No URL given."
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url

    if mode == 'static' or not enabled():
        return _extract_static(url)

    if mode == 'browser':
        agent = _agent()
        if agent is None:
            return "Live browser agent unavailable — use mode 'static'."
        try:
            agent.start_browser()
            agent.go_to(url)
            return agent.get_page_text(max_chars=6000) or "(empty page)"
        except Exception as e:
            return f"[browser navigation failed: {e}]"

    # auto: static first (fast + cheap), escalate when it starves
    text = _extract_static(url)
    if text and len(text) > 400 and 'failed' not in text[:60].lower():
        return text
    logger.info("static extract thin for %s — escalating to browser", url)
    return browse(url, mode='browser')


def screenshot(url=None, out_path=None):
    """
    Screenshot the current browser page, or navigate to *url* first.
    Returns the saved image path (the vision stack can analyze it).
    """
    agent = _agent()
    if agent is None or not enabled():
        if url:
            return "Screenshots need the live browser agent " \
                   "(Playwright). Static text only: " + \
                   _extract_static(url)[:500]
        return "No browser agent available."
    try:
        agent.start_browser()
        if url:
            agent.go_to(str(url))
        path = agent.screenshot_page()
        return path or "Screenshot failed."
    except Exception as e:
        return f"Screenshot failed: {e}"


def interact(op, **kwargs):
    """
    Dispatch a live-browser interaction:
      click(selector) | click_index(i) | type(selector, text) |
      key(k) | read_dom() | text() | google(q)
    """
    agent = _agent()
    if agent is None or not enabled():
        return "Live browser agent unavailable."
    try:
        agent.start_browser()
        if op == 'click':
            return agent.click_element(kwargs.get('selector', ''))
        if op == 'click_index':
            return agent.click_element_at(int(kwargs.get('index', 0)))
        if op == 'type':
            return agent.type_text(kwargs.get('selector', ''),
                                   kwargs.get('text', ''))
        if op == 'key':
            return agent.press_key(kwargs.get('key', 'Enter'))
        if op == 'read_dom':
            return agent.read_dom(max_elements=kwargs.get('max', 40))
        if op == 'text':
            return agent.get_page_text(max_chars=kwargs.get('max_chars',
                                                            6000))
        if op == 'google':
            agent.google_search(kwargs.get('query', ''))
            return agent.get_page_text(max_chars=4000)
        return f"Unknown browser op {op!r}."
    except Exception as e:
        return f"Browser op failed: {e}"


def status():
    try:
        agent = _agent()
        # Both the running flag AND the worker thread must be live.
        # On a crash the flag is flipped to False, but we also guard
        # thread liveness defensively (a dead thread never services cmds).
        running = bool(agent and getattr(agent, 'running', False)
                       and getattr(getattr(agent, 'thread', None),
                                   'is_alive', lambda: False)())
    except Exception:
        running = False
    return {'browser_agent': running, 'enabled': enabled(),
            'tiers': ['static', 'browser', 'gateway']}
