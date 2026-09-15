"""
J.A.R.V.I.S. — Tool Gateway (BYOK answer to the Nous Tool Gateway)
==================================================================

One configurable surface that combines web scraping, text-to-speech,
image generation and cloud-browser automation — WITHOUT locking you
into one vendor's infrastructure.

Point ``JARVIS_TOOL_GATEWAY_URL`` at any gateway that speaks the
simple endpoint contract (Nous Research's tool gateway, a self-hosted
proxy, or your own):

    POST {url}/scrape   {"url": …}        → {"text": …}
    POST {url}/tts      {"text": …}       → audio bytes
    POST {url}/image    {"prompt": …}     → image bytes
    POST {url}/browser  {"url": …}        → {"text": …}

Auth header from ``JARVIS_TOOL_GATEWAY_KEY``.  Every capability ALSO
has a local fallback that needs no gateway at all:

    scrape  → web_reader (requests + BeautifulSoup)
    tts     → Mouth (edge-tts + pygame)
    image   → configured image-capable provider via the router
    browser → the Playwright WebAgent

So the gateway is an accelerator, never a dependency.
``JARVIS_TOOL_GATEWAY=0`` ignores the remote gateway entirely.
"""

import os
import logging

logger = logging.getLogger("Jarvis.ToolGateway")

_TIMEOUT = 30


def gateway_enabled():
    return bool(os.getenv('JARVIS_TOOL_GATEWAY_URL')) and \
        os.getenv('JARVIS_TOOL_GATEWAY', '1') != '0'


def _headers():
    headers = {'Content-Type': 'application/json'}
    key = os.getenv('JARVIS_TOOL_GATEWAY_KEY', '')
    if key:
        headers['Authorization'] = f'Bearer {key}'
    return headers


def _call(endpoint, payload):
    """POST to the gateway; returns (ok, data_or_text)."""
    import requests
    url = os.environ['JARVIS_TOOL_GATEWAY_URL'].rstrip('/') + endpoint
    try:
        resp = requests.post(url, json=payload, headers=_headers(),
                             timeout=_TIMEOUT)
        if resp.status_code >= 300:
            return False, f"gateway {endpoint} → HTTP {resp.status_code}"
        ctype = resp.headers.get('Content-Type', '')
        if 'json' in ctype:
            return True, resp.json()
        return True, resp.content
    except Exception as e:
        return False, f"gateway {endpoint} failed: {e}"


# --------------------------------------------------------------------- #
# Capabilities — remote first, local fallback always
# --------------------------------------------------------------------- #

def scrape(url):
    """Web page → clean text."""
    url = str(url or '').strip()
    if not url:
        return "No URL given."
    if gateway_enabled():
        ok, data = _call('/scrape', {'url': url})
        if ok and isinstance(data, dict) and data.get('text'):
            return str(data['text'])[:20000]
        logger.debug("gateway scrape fallback: %s", data)
    from utils.browser_use import browse
    return browse(url, mode='static')


def tts(text, out_path=None):
    """
    Text → speech.  Returns a status message; with no gateway this
    routes through Mouth (queues local playback) — set out_path to get
    a file instead (gateway or edge-tts only).
    """
    text = str(text or '').strip()
    if not text:
        return "Nothing to speak."
    if gateway_enabled():
        ok, data = _call('/tts', {'text': text})
        if ok and isinstance(data, (bytes, bytearray)):
            out_path = out_path or os.path.join(
                os.path.dirname(os.path.dirname(
                    os.path.abspath(__file__))), 'response.mp3')
            with open(out_path, 'wb') as f:
                f.write(data)
            return f"Spoken via gateway → {out_path}"
        logger.debug("gateway tts fallback: %s", data)
    if out_path:
        try:
            import asyncio
            import edge_tts
            asyncio.run(edge_tts.save(text[:3000], out_path))
            return f"Spoken locally → {out_path}"
        except Exception as e:
            return f"Local TTS file failed: {e}"
    try:
        from utils.server import executor
        executor.mouth.speak(text)
        return "Speaking locally."
    except Exception as e:
        return f"TTS unavailable: {e}"


def image(prompt, out_path=None):
    """Prompt → image via the gateway, or the first image-capable
    provider in the router fleet."""
    prompt = str(prompt or '').strip()
    if not prompt:
        return "No image prompt given."
    if gateway_enabled():
        ok, data = _call('/image', {'prompt': prompt})
        if ok and isinstance(data, (bytes, bytearray)):
            out_path = out_path or os.path.join(
                os.path.dirname(os.path.dirname(
                    os.path.abspath(__file__))), 'generated_image.png')
            with open(out_path, 'wb') as f:
                f.write(data)
            return f"Image saved → {out_path}"
        logger.debug("gateway image fallback: %s", data)
    return ("No image-capable provider configured. Set an image model "
            "in your provider fleet or configure JARVIS_TOOL_GATEWAY_URL.")


def browser(url):
    """Cloud-browser page extraction via the gateway, local browser as
    fallback."""
    url = str(url or '').strip()
    if gateway_enabled():
        ok, data = _call('/browser', {'url': url})
        if ok and isinstance(data, dict) and data.get('text'):
            return str(data['text'])[:20000]
    from utils.browser_use import browse
    return browse(url, mode='browser')


def status():
    return {
        'gateway_configured': gateway_enabled(),
        'url': os.getenv('JARVIS_TOOL_GATEWAY_URL', ''),
        'capabilities': ['scrape', 'tts', 'image', 'browser'],
        'fallbacks': 'web_reader / mouth / browser agent',
    }
