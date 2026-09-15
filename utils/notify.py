"""
J.A.R.V.I.S. — Proactive Notifier (autonomy layer)
===================================================

One fan-out point for everything Jarvis wants to say UNPROMPTED
(deadline escalations, goal completions, automation results,
proactive findings):

    get_notifier().announce(text, priority='normal'|'high'|'low')

Channels (each fail-soft, each independently gated):
  * dashboard feed — socketio ``proactive`` event + ai_text line
    (always on; the quiet default)
  * voice — mouth.speak, only when ``JARVIS_NOTIFY_VOICE=1``
    (default OFF — unprompted speech is startling)
  * telegram — bot push, only when ``JARVIS_NOTIFY_TELEGRAM=1``
    AND the bot is live with a known chat id (default OFF)

Priority mapping: high → all enabled channels; normal → dashboard +
voice-if-on (no Telegram ping); low → dashboard feed only.

Rate limits keep a noisy loop from spamming: at most
``JARVIS_NOTIFY_MAX_PER_H`` announcements per hour (default 6);
excess is collapsed into the dashboard feed silently.

Kill switch: ``JARVIS_NOTIFY=0`` turns the whole module into a no-op
(announce() returns False, nothing emitted).
"""

import os
import time
import threading
import logging
import collections

logger = logging.getLogger("Jarvis.Notify")

_MAX_PER_H_DEFAULT = 6


def enabled():
    return os.getenv('JARVIS_NOTIFY', '1') != '0'


def _voice_on():
    return os.getenv('JARVIS_NOTIFY_VOICE', '0') == '1'


def _telegram_on():
    return os.getenv('JARVIS_NOTIFY_TELEGRAM', '0') == '1'


def _max_per_h():
    try:
        return max(1, int(os.getenv('JARVIS_NOTIFY_MAX_PER_H',
                                    str(_MAX_PER_H_DEFAULT))))
    except (TypeError, ValueError):
        return _MAX_PER_H_DEFAULT


class Notifier:
    """Fan-out announcer.  Thread-safe, never raises."""

    def __init__(self):
        self._lock = threading.Lock()
        self._hits = collections.deque()   # timestamps of recent announces
        self._dropped = 0

    # ---------------- rate limit ---------------- #

    def _allow(self):
        now = time.time()
        with self._lock:
            while self._hits and now - self._hits[0] > 3600:
                self._hits.popleft()
            if len(self._hits) >= _max_per_h():
                self._dropped += 1
                return False
            self._hits.append(now)
            return True

    # ---------------- public ---------------- #

    def announce(self, text, priority='normal'):
        """
        Broadcast *text* across the enabled channels for *priority*.
        Returns True when emitted (at least the dashboard feed got it),
        False when disabled or invalid.  Never raises.
        """
        text = str(text or '').strip()
        if not text or not enabled():
            return False
        priority = str(priority or 'normal').lower()
        if priority not in ('low', 'normal', 'high'):
            priority = 'normal'
        if not self._allow():
            logger.info("notifier rate-limited, collapsing to feed: %s",
                        text[:80])
            # Still land in the feed — silence would lose the signal.
            self._feed(text, priority, collapsed=True)
            return True
        self._feed(text, priority)
        if priority == 'low':
            return True
        if _voice_on():
            self._speak(text)
        if priority == 'high' and _telegram_on():
            self._telegram(text)
        return True

    def stats(self):
        with self._lock:
            return {'sent_last_hour': len(self._hits),
                    'dropped': self._dropped}

    # ---------------- channels ---------------- #

    def _feed(self, text, priority, collapsed=False):
        try:
            from utils.server import send_to_ui
            send_to_ui('proactive', {'text': text[:800],
                                     'priority': priority,
                                     'collapsed': collapsed})
            send_to_ui('ai_text', {'text': text[:800]})
        except Exception as e:
            logger.debug("notifier feed failed: %s", e)

    def _speak(self, text):
        try:
            from utils.server import executor
            mouth = getattr(executor, 'mouth', None)
            if mouth is not None:
                mouth.speak(text[:220])
        except Exception as e:
            logger.debug("notifier voice failed: %s", e)

    def _telegram(self, text):
        # Server-owned bot instance: server.py keeps it local, so reach
        # it through a module-level hook it registers at startup
        # (avoids importing the bot before its deps exist).
        try:
            hook = globals().get('_TELEGRAM_HOOK')
            if hook is not None:
                hook(text[:1000])
        except Exception as e:
            logger.debug("notifier telegram failed: %s", e)


# --------------------------------------------------------------------- #
# Singleton + server hook
# --------------------------------------------------------------------- #

_singleton = None
_singleton_lock = threading.Lock()


def get_notifier():
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = Notifier()
        return _singleton


def register_telegram_hook(fn):
    """
    Server startup calls this with a ``fn(text)`` that pushes to the
    live Telegram bot (no-op when the bot is down — fn must swallow).
    """
    globals()['_TELEGRAM_HOOK'] = fn


def _reset_singleton():
    """Test helper."""
    global _singleton
    with _singleton_lock:
        _singleton = None
