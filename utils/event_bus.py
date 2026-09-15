"""
Jarvis Integration Gateway (Event Bus)
======================================

Normalizes every inbound channel — dashboard, Telegram text/voice/
photo, HTTP webhooks, paired mobile devices — into ONE internal event
shape before it reaches the agent core:

    InternalEvent {
        id, source ('dashboard'|'telegram'|'webhook'|'mobile'|'voice'),
        kind   ('text'|'voice_transcript'|'photo'|'json'),
        text, meta {…}, reply(callable), created
    }

A single registered handler (the standard think→execute pipeline)
consumes events; channels only translate transport → event and deliver
the handler's return value back over their own protocol.

HTTP gateway: POST /webhook?key=SECRET  {"text": "...", "wait": true}
    wait=true  → synchronous JSON reply {"ok":true,"result":"…"}
    otherwise  → 202 Accepted, processed in background thread
"""

import os
import json
import time
import uuid
import logging
import threading

logger = logging.getLogger("Jarvis.EventBus")


class InternalEvent:
    __slots__ = ('id', 'source', 'kind', 'text', 'meta', 'reply',
                 'created')

    def __init__(self, source, kind='text', text='', meta=None,
                 reply=None):
        self.id = uuid.uuid4().hex[:10]
        self.source = str(source or 'unknown')
        self.kind = str(kind or 'text')
        self.text = str(text or '')
        self.meta = dict(meta or {})
        self.reply = reply                 # callable(str) for responses
        self.created = time.time()

    def respond(self, message):
        """Deliver the pipeline result through the origin channel."""
        if callable(self.reply):
            try:
                self.reply(message)
            except Exception as e:
                logger.warning(f"reply failed for {self.id}: {e}")

    def to_dict(self):
        return {'id': self.id, 'source': self.source, 'kind': self.kind,
                'text': self.text[:200], 'meta': self.meta,
                'created': self.created}


class EventBus:
    """
    Fan-in point for all input channels. Exactly one main ``handler``
    is expected (set by the server); extra observers get notifications
    without consuming.
    """

    def __init__(self):
        self.handler = None            # fn(InternalEvent) -> str|None
        self.observers = []            # fn(InternalEvent) -> None
        self._recent = []              # small ring for diagnostics
        self.webhook_key = os.getenv('JARVIS_WEBHOOK_KEY', '')
        self._lock = threading.Lock()
        # Cap concurrent background handler threads so a flood of
        # wait=false events (webhook/voice/telegram) can't spawn an
        # unbounded number of LLM-driving threads (memory/CPU/cost
        # exhaustion).  Saturated events are answered "busy" and dropped.
        self._bg_slots = threading.BoundedSemaphore(8)

    # ------------------------------------------------------------------ #
    # Normalizers
    # ------------------------------------------------------------------ #
    @staticmethod
    def from_dashboard(text, reply=None):
        return InternalEvent('dashboard', 'text', text,
                             meta={'ui': True}, reply=reply)

    @staticmethod
    def from_telegram_text(text, chat_id=None, reply=None):
        return InternalEvent('telegram', 'text', text,
                             meta={'chat_id': chat_id}, reply=reply)

    @staticmethod
    def from_telegram_voice(transcript, chat_id=None, reply=None):
        return InternalEvent('telegram', 'voice_transcript', transcript,
                             meta={'chat_id': chat_id}, reply=reply)

    @staticmethod
    def from_telegram_photo(caption, image_path=None, chat_id=None,
                            reply=None):
        return InternalEvent('telegram', 'photo', caption or '',
                             meta={'chat_id': chat_id,
                                   'image_path': image_path},
                             reply=reply)

    @staticmethod
    def from_mobile_text(text, device_id=None, reply=None):
        # Paired phone (Jarvis Lite).  Untrusted origin: the server
        # pipeline stamps _origin='mobile:<device>' so destructive
        # actions always hold for a human on the dashboard.
        return InternalEvent('mobile', 'text', text,
                             meta={'device_id': device_id}, reply=reply)

    @staticmethod
    def from_webhook(payload, reply=None):
        if isinstance(payload, bytes):
            try:
                payload = json.loads(payload.decode('utf-8'))
            except Exception:
                payload = {}
        if not isinstance(payload, dict):
            payload = {'text': str(payload)}
        return InternalEvent('webhook', 'json', payload.get('text', ''),
                             meta={k: v for k, v in payload.items()
                                   if k != 'text'},
                             reply=reply)

    # ------------------------------------------------------------------ #
    # Dispatch
    # ------------------------------------------------------------------ #
    def publish(self, event, background=False):
        """
        Route an event through the main handler.

        Returns the handler result when synchronous, else None.
        Never raises outward — failures answer via event.respond().
        """
        if not isinstance(event, InternalEvent):
            event = self.from_webhook(event)
        if not event.text.strip() and event.kind != 'photo':
            logger.info(f"event {event.id}: empty payload ignored")
            return None

        with self._lock:
            self._recent.append(event.to_dict())
            del self._recent[:-50]

        for obs in list(self.observers):
            try:
                obs(event)
            except Exception as e:
                logger.warning(f"observer error: {e}")

        if self.handler is None:
            logger.error("no handler registered — event dropped")
            event.respond("Jarvis core is not ready to process events.")
            return None

        def _run():
            result = None
            try:
                result = self.handler(event)
            except Exception as e:
                logger.error(f"handler failed on {event.id}: {e}")
                result = f"Processing failed: {e}"
            if result is not None:
                event.respond(str(result))
            return result

        if background:
            if not self._bg_slots.acquire(blocking=False):
                logger.warning("event %s: background pipeline saturated — "
                               "dropping (busy reply sent)", event.id)
                event.respond("Jarvis is handling several requests at once — "
                              "please try again in a moment.")
                return None

            def _bg():
                try:
                    _run()
                finally:
                    self._bg_slots.release()

            threading.Thread(target=_bg, daemon=True,
                             name=f"evt-{event.source}").start()
            return None
        return _run()

    def recent_events(self, limit=20):
        with self._lock:
            return list(self._recent[-limit:])


# Shared bus (server registers the single main handler)
_shared = None


def get_bus():
    global _shared
    if _shared is None:
        _shared = EventBus()
    return _shared
