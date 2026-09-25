"""
Offline tests for utils.ear — the microphone capture side of the Tier A
voice path (mic -> speech_recognition -> text -> UI).

Ear previously had ZERO test coverage and 8 raw print() calls. Covers the
'never raises' contract with speech_recognition / pyaudio / Microphone
stubbed (no mic, no network, no pyaudio needed in the venv):

  - mic unavailable (no pyaudio) -> listen() returns None
  - listen() success -> returns recognized text
  - UnknownValueError -> None
  - RequestError (API/network) -> None
  - unexpected exception in listen() -> None, never propagates
  - _recognize: non-STT backend crash -> None (no user_text emitted)
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _stub_sr(recognize_result=None, recognize_error=None):
    """Return (fake_sr_module, recognizer_instance) with REAL exception
    classes so Ear's `except sr.UnknownValueError` clauses match."""
    fake_sr = MagicMock()
    fake_sr.UnknownValueError = type("UnknownValueError", (Exception,), {})
    fake_sr.RequestError = type("RequestError", (Exception,), {})

    class _FakeRecognizer:
        def adjust_for_ambient_noise(self, source):
            pass
        def listen(self, source):
            return "FAKE_AUDIO"

    class _FakeMicrophone:
        def __init__(self, *a, **k):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    recognizer_instance = _FakeRecognizer()
    if recognize_error is not None:
        recognizer_instance.recognize_google = MagicMock(
            side_effect=recognize_error)
    else:
        recognizer_instance.recognize_google = MagicMock(
            return_value=recognize_result)
    fake_sr.Recognizer.return_value = recognizer_instance
    fake_sr.Microphone.return_value = _FakeMicrophone()
    return fake_sr, recognizer_instance


class _StubPyaudio:
    pass


def _ctx(fake_sr, pyaudio_ok, send_ui):
    """Patches for Ear construction: sr + pyaudio import + _send_to_ui."""
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "pyaudio":
            if pyaudio_ok:
                return _StubPyaudio
            raise ImportError("no pyaudio in test")
        return real_import(name, *a, **k)

    patches = [patch("utils.ear.sr", fake_sr),
               patch("builtins.__import__", side_effect=fake_import)]
    if send_ui is not None:
        patches.append(patch("utils.ear._send_to_ui", send_ui))
    for p in patches:
        p.start()
    return patches


class _EarHarness(unittest.TestCase):
    """Base: builds an Ear under the stubbed context."""
    pyaudio_ok = True
    recognize_result = None
    recognize_error = None

    def _make(self, send_ui=None):
        from utils.ear import Ear
        patches = _ctx(_stub_sr(self.recognize_result,
                                self.recognize_error)[0],
                       pyaudio_ok=self.pyaudio_ok, send_ui=send_ui)
        self.addCleanup(lambda: [p.stop() for p in patches])
        return Ear()


class TestMicUnavailable(_EarHarness):
    pyaudio_ok = False  # force the ImportError path

    def test_no_pyaudio_returns_none(self):
        ear = self._make(send_ui=MagicMock())
        self.assertFalse(ear.mic_available)
        self.assertIsNone(ear.listen())


class TestListenSuccess(_EarHarness):
    recognize_result = "hello world"
    recognize_error = None

    def test_success_returns_text(self):
        ear = self._make(send_ui=MagicMock())
        self.assertTrue(ear.mic_available)
        self.assertEqual(ear.listen(), "hello world")

    def test_sends_ui_events(self):
        sent = []
        ear = self._make(send_ui=lambda ev, d: sent.append((ev, d)))
        ear.listen()
        kinds = [ev for ev, _ in sent]
        self.assertIn('status', kinds)
        self.assertIn('user_text', kinds)


class TestListenErrorRecovery(_EarHarness):
    recognize_error = None
    pyaudio_ok = True

    def test_unknown_value_error_returns_none(self):
        ue = _stub_sr()[0].UnknownValueError
        self.recognize_error = ue("could not parse")
        ear = self._make(send_ui=MagicMock())
        self.assertIsNone(ear.listen())

    def test_request_error_returns_none(self):
        re_err = _stub_sr()[0].RequestError
        self.recognize_error = re_err("network down")
        ear = self._make(send_ui=MagicMock())
        self.assertIsNone(ear.listen())

    def test_unexpected_exception_never_propagates(self):
        fake_sr, _ = _stub_sr()
        fake_sr.Microphone.side_effect = RuntimeError("no mic device")
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *a, **k):
            if name == "pyaudio":
                return _StubPyaudio
            return real_import(name, *a, **k)

        patches = [patch("utils.ear.sr", fake_sr),
                   patch("builtins.__import__", side_effect=fake_import)]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        from utils.ear import Ear
        ear = Ear()
        self.assertIsNone(ear.listen())


class TestRecognizeDegradation(_EarHarness):
    pyaudio_ok = True

    def test_backend_crash_returns_none(self):
        """Non-STT exception in recognize_google caught by _recognize."""
        fake_sr, rec = _stub_sr()
        rec.recognize_google.side_effect = ValueError("segfault")
        patches = _ctx(fake_sr, pyaudio_ok=True, send_ui=None)
        self.addCleanup(lambda: [p.stop() for p in patches])
        from utils.ear import Ear
        ear = Ear()
        sent = []
        with patch("utils.ear._send_to_ui",
                   side_effect=lambda ev, d: sent.append((ev, d))):
            self.assertIsNone(ear.listen())
        self.assertNotIn('user_text', [ev for ev, _ in sent])


if __name__ == "__main__":
    unittest.main()
