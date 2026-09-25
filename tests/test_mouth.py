"""
Tests for utils.mouth (TTS pipeline).

Covers (edge_tts + pygame stubbed):
  - _emit_tt never raises even if send_to_ui is missing
  - _generate_and_play emits a tts_chunk UI event PER audio fragment
    (the streaming improvement — clients get audio progressively)
  - _generate_and_play degrades gracefully on TTS error
  - _generate_and_play degrades gracefully on empty chunk stream
"""

import os
import sys
import asyncio
import unittest
from unittest.mock import patch, MagicMock, call

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _FakeStream:
    """Mimics async edge_tts chunk stream: a few audio chunks then a final."""
    def __init__(self, chunks):
        self._chunks = chunks

    async def __aiter__(self):
        for c in self._chunks:
            yield c


class TestEmitTt(unittest.TestCase):
    """_emit_tt is a safe no-raise UI emitter."""

    def test_emit_success(self):
        from unittest.mock import patch
        m = MagicMock()
        with patch("utils.server.send_to_ui", m):
            from utils.mouth import Mouth
            Mouth()._emit_tt("tts_chunk", {"data": b"x", "len": 1})
        m.assert_called_once_with("tts_chunk", {"data": b"x", "len": 1})

    def test_emit_no_server(self):
        """If utils.server.send_to_ui can't be imported, _emit_tt swallows."""
        from utils.mouth import Mouth
        with patch("builtins.__import__", side_effect=ImportError):
            # _emit_tt catches its own exceptions, so this must not raise.
            Mouth()._emit_tt("tts_chunk", {"data": b"x"})


class TestGenerateAndPlay(unittest.TestCase):
    """_generate_and_play streaming chunk emission (edge_tts stubbed)."""

    def setUp(self):
        self.sent = []

        # Capture send_to_ui calls (replaces the socket emit).
        import utils.server as _srv
        self._orig_send = getattr(_srv, "send_to_ui", None)
        def _capture(event, data):
            self.sent.append((event, data))
        _srv.send_to_ui = _capture
        self._srv = _srv

        # Stub pygame at import time so Mouth.__init__ doesn't touch audio.
        # get_busy() MUST return False, else the playback loop (while
        # get_busy(): tick) spins forever in tests.
        self._pygame_stub = MagicMock()
        self._pygame_stub.mixer.music.get_busy.return_value = False
        self._mod_patch = patch.dict("sys.modules", {"pygame": self._pygame_stub})
        self._mod_patch.start()

    def tearDown(self):
        self._srv.send_to_ui = self._orig_send
        self._mod_patch.stop()

    def _make_comm(self, chunks):
        """Mock edge_tts.Communicate.stream() to yield `chunks` (list of dicts)."""
        comm = MagicMock()
        comm.stream.return_value = _FakeStream(chunks=chunks)
        self._comm_patch = patch("edge_tts.Communicate", return_value=comm)
        self._comm_patch.start()
        self.addCleanup(self._comm_patch.stop)
        return comm

    def test_emits_one_chunk_event_per_audio_fragment(self):
        from utils.mouth import Mouth
        self._make_comm([
            {"type": "audio", "data": b"frame1"},
            {"type": "audio", "data": b"frame2"},
            {"type": "audio", "data": b"frame3"},
        ])
        Mouth()._generate_and_play("hello world")
        tt_events = [(e, d) for e, d in self.sent if e == "tts_chunk"]
        self.assertEqual(len(tt_events), 3)
        self.assertEqual(tt_events[0][1]["data"], b"frame1")
        self.assertEqual(tt_events[1][1]["data"], b"frame2")
        self.assertEqual(tt_events[2][1]["data"], b"frame3")

    def test_degrades_on_tts_error(self):
        """An exception in the stream is caught -> no raise, no tts_chunk."""
        from utils.mouth import Mouth
        comm = self._make_comm([])
        comm.stream.side_effect = RuntimeError("tts cloud down")
        Mouth()._generate_and_play("hi")
        tt_events = [(e, d) for e, d in self.sent if e == "tts_chunk"]
        self.assertEqual(len(tt_events), 0)

    def test_degrades_on_empty_stream(self):
        """No audio chunks -> logs + returns, no crash, no tts_chunk."""
        from utils.mouth import Mouth
        self._make_comm([])
        Mouth()._generate_and_play("nothing")
        tt_events = [(e, d) for e, d in self.sent if e == "tts_chunk"]
        self.assertEqual(len(tt_events), 0)


if __name__ == "__main__":
    unittest.main()
