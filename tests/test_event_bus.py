"""
Offline tests for utils.event_bus — the Tier A event-normalization and
dispatch hub. Every inbound channel (dashboard/Telegram/mobile/webhook)
is normalized into one InternalEvent here; the mobile photo path is the
entry point for phone-camera -> SigLIP vision relay.

Covers:
  - InternalEvent slots + to_dict truncation + respond reply channel
  - all normalizers set source/kind/meta correctly
  - publish dispatch: sync handler result -> respond; empty-text filtered
  - observer errors never propagate (isolated)
  - background publish: bounded semaphore + busy-reply saturation
  - handler failure -> event.respond('Processing failed'); never raises
  - no-handler registered -> graceful 'core not ready' reply
  - publish accepts a raw dict (auto-webhook normalization)
  - recent_events capped at 50, ordered newest-first
  - from_webhook strips the 'text' key from meta
"""

import os
import sys
import time
import threading
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestInternalEvent(unittest.TestCase):
    def test_slots_and_defaults(self):
        from utils.event_bus import InternalEvent
        ev = InternalEvent('mobile', 'text', 'hi', meta={'device_id': 'd1'})
        self.assertEqual(ev.source, 'mobile')
        self.assertEqual(ev.kind, 'text')
        self.assertEqual(ev.text, 'hi')
        self.assertEqual(ev.meta, {'device_id': 'd1'})
        self.assertIsNone(ev.reply)

    def test_defaults(self):
        from utils.event_bus import InternalEvent
        ev = InternalEvent('dashboard')
        self.assertEqual(ev.kind, 'text')
        self.assertEqual(ev.text, '')
        self.assertEqual(ev.meta, {})

    def test_to_dict_truncates_text(self):
        from utils.event_bus import InternalEvent
        ev = InternalEvent('t', 'text', 'A' * 500)
        d = ev.to_dict()
        self.assertEqual(d['source'], 't')
        self.assertEqual(d['kind'], 'text')
        self.assertEqual(len(d['text']), 200)
        self.assertEqual(len(d['text']), 200)

    def test_respond_invokes_reply(self):
        from utils.event_bus import InternalEvent
        reply = MagicMock()
        ev = InternalEvent('t', 'text', 'hi', reply=reply)
        ev.respond("got it")
        reply.assert_called_once_with("got it")

    def test_respond_no_callback_is_noop(self):
        from utils.event_bus import InternalEvent
        ev = InternalEvent('t', 'text', 'hi', reply=None)
        ev.respond("hi")  # must not raise


class TestNormalizers(unittest.TestCase):
    def test_from_dashboard(self):
        from utils.event_bus import EventBus
        ev = EventBus.from_dashboard("hello", reply=lambda m: m)
        self.assertEqual(ev.source, 'dashboard')
        self.assertEqual(ev.kind, 'text')
        self.assertEqual(ev.text, 'hello')
        self.assertTrue(ev.meta.get('ui'))

    def test_from_telegram_text(self):
        from utils.event_bus import EventBus
        ev = EventBus.from_telegram_text("hi", chat_id=42)
        self.assertEqual(ev.source, 'telegram')
        self.assertEqual(ev.kind, 'text')
        self.assertEqual(ev.meta.get('chat_id'), 42)

    def test_from_telegram_voice(self):
        from utils.event_bus import EventBus
        ev = EventBus.from_telegram_voice("spoken words", chat_id=7)
        self.assertEqual(ev.kind, 'voice_transcript')
        self.assertEqual(ev.text, 'spoken words')

    def test_from_telegram_photo(self):
        from utils.event_bus import EventBus
        ev = EventBus.from_telegram_photo("caption", image_path='/img/x',
                                          chat_id=7)
        self.assertEqual(ev.source, 'telegram')
        self.assertEqual(ev.kind, 'photo')
        self.assertEqual(ev.text, 'caption')
        self.assertEqual(ev.meta.get('image_path'), '/img/x')

    def test_from_mobile_text(self):
        from utils.event_bus import EventBus
        ev = EventBus.from_mobile_text("remind me at 5pm", device_id="dev_abc")
        self.assertEqual(ev.source, 'mobile')
        self.assertEqual(ev.kind, 'text')
        self.assertEqual(ev.text, 'remind me at 5pm')
        self.assertEqual(ev.meta.get('device_id'), 'dev_abc')

    def test_from_mobile_photo_carries_metadata_not_pixels(self):
        from utils.event_bus import EventBus
        ev = EventBus.from_mobile_photo("what's this", device_id="dev_x",
                                        image_meta={"w": 1080, "h": 1920})
        self.assertEqual(ev.source, 'mobile')
        self.assertEqual(ev.kind, 'photo')
        self.assertEqual(ev.text, "what's this")
        self.assertEqual(ev.meta.get('device_id'), 'dev_x')
        self.assertEqual(ev.meta.get('image_meta'),
                         {"w": 1080, "h": 1920})

    def test_from_webhook_strips_text_from_meta(self):
        from utils.event_bus import EventBus
        ev = EventBus.from_webhook({"text": "hi there", "secret": "k1",
                                    "extra": "x"})
        self.assertEqual(ev.source, 'webhook')
        self.assertEqual(ev.kind, 'json')
        self.assertEqual(ev.text, 'hi there')
        self.assertNotIn('text', ev.meta)
        self.assertEqual(ev.meta, {'secret': 'k1', 'extra': 'x'})


class TestPublishDispatch(unittest.TestCase):
    def _bus(self, handler=None):
        from utils.event_bus import EventBus
        bus = EventBus()
        if handler:
            bus.handler = handler
        return bus

    def test_sync_handler_result_responds(self):
        from utils.event_bus import InternalEvent
        seen = []
        def handler(ev):
            seen.append(ev.text)
            return "acknowledged"
        bus = self._bus(handler)
        reply = MagicMock()
        ev = InternalEvent('dashboard', 'text', 'do thing', reply=reply)
        result = bus.publish(ev)
        self.assertEqual(result, "acknowledged")
        reply.assert_called_once_with("acknowledged")

    def test_empty_text_filtered(self):
        from utils.event_bus import InternalEvent
        bus = self._bus(handler=lambda ev: "x")
        reply = MagicMock()
        ev = InternalEvent('dashboard', 'text', '   ', reply=reply)
        self.assertIsNone(bus.publish(ev))
        reply.assert_not_called()

    def test_photo_event_with_empty_text_passes(self):
        """A photo with blank caption must NOT be filtered (image is the
        payload, per the publish() guard at line 157)."""
        from utils.event_bus import InternalEvent
        bus = self._bus(handler=lambda ev: "seen")
        ev = InternalEvent('mobile', 'photo', '', meta={'device_id': 'd'})
        result = bus.publish(ev)
        self.assertEqual(result, "seen")

    def test_handler_exception_is_swallowed(self):
        from utils.event_bus import InternalEvent
        reply = MagicMock()
        def boom(ev):
            raise RuntimeError("kaboom")
        bus = self._bus(boom)
        ev = InternalEvent('dashboard', 'text', 'x', reply=reply)
        result = bus.publish(ev)  # must not raise
        # publish returns the failure string (event.respond fires the
        # 'Processing failed: …' reply via the reply callback).
        self.assertIn("failed", str(result).lower())
        reply.assert_called_once()
        msg = reply.call_args[0][0]
        self.assertIn("failed", msg.lower())

    def test_no_handler_graceful_reply(self):
        from utils.event_bus import InternalEvent
        bus = self._bus(handler=None)
        reply = MagicMock()
        ev = InternalEvent('dashboard', 'text', 'x', reply=reply)
        self.assertIsNone(bus.publish(ev))
        reply.assert_called_once_with("Jarvis core is not ready to process events.")

    def test_observer_error_does_not_break_publish(self):
        from utils.event_bus import InternalEvent
        obs = MagicMock(side_effect=RuntimeError("observer boom"))
        bus = self._bus(handler=lambda ev: "ok")
        bus.observers.append(obs)
        ev = InternalEvent('dashboard', 'text', 'x', reply=MagicMock())
        result = bus.publish(ev)
        self.assertEqual(result, "ok")

    def test_raw_dict_normalized_as_webhook(self):
        from utils import event_bus as eb
        bus = eb.EventBus()
        seen = []
        bus.handler = lambda ev: (seen.append(ev.source), "done")[1]
        result = bus.publish({"text": "web event", "k": "v"})
        self.assertEqual(result, "done")
        self.assertEqual(seen[0], 'webhook')

    def test_background_busy_reply_on_saturation(self):
        """When all 8 bg slots are busy, a background event gets a
        'handling several requests' reply and is dropped."""
        from utils.event_bus import InternalEvent
        bus = self._bus(handler=lambda ev: time.sleep(0.2))
        # Exhaust the semaphore (8 slots).
        block = threading.Event()
        holders = []
        for _ in range(8):
            e = threading.Event()
            holders.append(e)
            # Acquire each slot manually.
            bus._bg_slots.acquire(blocking=False)
        reply = MagicMock()
        ev = InternalEvent('webhook', 'text', 'late event', reply=reply)
        result = bus.publish(ev, background=True)
        self.assertIsNone(result)
        reply.assert_called_once_with(
            "Jarvis is handling several requests at once — "
            "please try again in a moment.")


class TestRecentEvents(unittest.TestCase):
    def test_recent_capped_and_ordered(self):
        from utils.event_bus import InternalEvent
        from utils.event_bus import EventBus
        bus = EventBus()
        handler = MagicMock(return_value="x")
        bus.handler = handler
        for i in range(60):
            ev = InternalEvent('dashboard', 'text', f"msg{i}")
            bus.publish(ev)
        recent = bus.recent_events(limit=50)
        # Capped at 50 (older 10 evicted by the del self._recent[:-50]).
        self.assertEqual(len(recent), 50)
        # Stored oldest->newest; [-50:] window is msg10..msg59.
        self.assertEqual(recent[0]['text'], "msg10")
        self.assertEqual(recent[-1]['text'], "msg59")


class TestGetBus(unittest.TestCase):
    def test_singleton(self):
        from utils.event_bus import get_bus
        b1 = get_bus()
        b2 = get_bus()
        self.assertIs(b1, b2)


if __name__ == "__main__":
    unittest.main()
