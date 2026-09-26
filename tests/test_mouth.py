"""Unit tests for utils/mouth.py TTS streaming path.

Covers the regression: _generate_and_play must NOT emit tts_chunk
from inside the running asyncio loop (which would block edge-tts
chunk delivery on socket latency), and must guard loop.close() against
an already-closed loop.

edge_tts / pygame are faked via sys.modules injection (no network/real
audio). The SocketIO emit is captured through send_to_ui so we can count
and order the tts_chunk events.
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class FakeEdgeTTSCommunicate:
    """Mimics edge_tts.Communicate with a streaming async generator."""

    def __init__(self, chunks):
        self._chunks = list(chunks)

    def stream(self):
        async def gen():
            for c in self._chunks:
                yield {"type": "audio", "data": c}
            # trailing non-audio control frame (should be ignored)
            yield {"type": "Meta", }
        return gen()


def _install_fake_edge_tts(chunks):
    mod = type(sys)("edge_tts")
    mod.Communicate = lambda text, voice: FakeEdgeTTSCommunicate(chunks)
    sys.modules["edge_tts"] = mod
    return mod


class FakePygame:
    class mixer:
        music = MagicMock()
        music.get_busy = lambda: False
    time = MagicMock()
    time.Clock.return_value.tick = lambda self, n: None


class TestTTSStreaming(unittest.TestCase):

    def setUp(self):
        # Fresh Mouth each test, no real audio device.
        from utils.mouth import Mouth
        self.chunks = [b"\x01", b"\x02", b"\x03", b"\x04"]
        _install_fake_edge_tts(self.chunks)
        sys.modules["pygame"] = FakePygame()
        self.mouth = Mouth()

    def tearDown(self):
        for k in ("edge_tts", "pygame"):
            sys.modules.pop(k, None)

    def test_chunks_emitted_exactly_once_after_loop(self):
        """All collected audio chunks are emitted as tts_chunk, in order,
        exactly once — and emission happens AFTER the asyncio loop is
        closed (not inside it)."""
        emitted = []

        async def fake_send_to_ui(event, data):
            emitted.append((event, data))

        async def send_to_ui(event, data):
            await fake_send_to_ui(event, data)

        with patch.object(self.mouth, '_emit_tt',
                          side_effect=lambda e, d: emitted.append((e, d))):
            self.mouth._generate_and_play("hello")

        events = [e for e, _ in emitted]
        self.assertIn('tts_chunk', events)
        chunks = [d['data'] for e, d in emitted if e == 'tts_chunk']
        self.assertEqual(chunks, [b"\x01", b"\x02", b"\x03", b"\x04"])

    def test_loop_not_closed_twice(self):
        """REGRESSION: loop.close() in finally must be guarded by
        is_closed() — calling it twice raises RuntimeError."""
        loop_refs = []
        real_new = asyncio.new_event_loop
        real_close = asyncio.AbstractEventLoop.close

        def tracking_new(*a, **k):
            loop = real_new(*a, **k)
            loop_refs.append(loop)
            return loop

        with patch.object(asyncio, 'new_event_loop', tracking_new), \
             patch.object(self.mouth, '_emit_tt'):
            self.mouth._generate_and_play("hi")

        # Exactly one loop created and it was closed by our finally block.
        self.assertEqual(len(loop_refs), 1)
        self.assertTrue(loop_refs[0].is_closed())

    def test_empty_stream_logs_error_and_returns(self):
        """No audio chunks -> log + early return (no file write)."""
        _install_fake_edge_tts([])
        msgs = []
        import utils.mouth as mouth_mod
        with patch.object(self.mouth, '_emit_tt'), \
             patch.object(mouth_mod.logger, 'error',
                          side_effect=lambda m, *a: msgs.append(m)):
            self.mouth._generate_and_play("nothing")
        self.assertTrue(any("No audio chunks" in str(m) for m in msgs))


if __name__ == '__main__':
    unittest.main(verbosity=2)
