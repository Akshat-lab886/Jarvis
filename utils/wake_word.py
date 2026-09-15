"""
J.A.R.V.I.S. — Wake Word (Hermes parity)
========================================

Hands-free activation — "Hey Jarvis" — in front of the normal voice
loop.  Two detection tiers, best available first:

    tier 1  a real wake-word engine when installed
            (openwakeword / pocketsphinx — pip install either)
    tier 2  the dependency-free tier: speech_recognition listens in
            short bursts and gates on the phrase appearing in the
            transcript.  Works everywhere SpeechRecognition does.

The phrase is configurable (``JARVIS_WAKE_WORD``; any substring match,
case-insensitive).  Wake-gating itself is opt-in so existing voice
users see zero behaviour change: set ``JARVIS_WAKE_WORD_ENABLE=1``
(then only "…Jarvis…" utterances reach the brain), or run the CLI
probe:

    python -m utils.wake_word
"""

import os
import logging

logger = logging.getLogger("Jarvis.WakeWord")

_DEFAULT_PHRASE = 'jarvis'
_BURST_S = 3          # tier-2 listen burst length


def wake_enabled():
    return os.getenv('JARVIS_WAKE_WORD_ENABLE', '0') == '1'


def phrase():
    return os.getenv('JARVIS_WAKE_WORD', _DEFAULT_PHRASE).strip().lower() \
        or _DEFAULT_PHRASE


def matches(text):
    """Does this transcript contain the wake phrase?"""
    return phrase() in str(text or '').lower()


def strip_wake_word(text):
    """Remove the wake phrase (and polite fillers around it)."""
    t = str(text or '')
    low = t.lower()
    idx = low.find(phrase())
    if idx == -1:
        return t.strip()
    # Drop the phrase plus any "hey/ok/hi" directly before it
    start = idx
    for prefix in ('hey ', 'ok ', 'okay ', 'hi ', 'yo ', 'hello '):
        if low[max(0, idx - len(prefix)):idx] == prefix:
            start = idx - len(prefix)
            break
    cleaned = t[:start] + ' ' + t[idx + len(phrase()):]
    return ' '.join(cleaned.split()).strip(" ,.!:;-")


class WakeWordListener:
    """Continuous listener that fires a callback on each wake hit."""

    def __init__(self, on_wake=None):
        self.on_wake = on_wake          # fn(transcript_after_phrase)
        self._running = False
        self._engine = None             # tier-1 engine when available

    # ------------------------------------------------------------------ #
    # Tier 1: dedicated wake-word engines
    # ------------------------------------------------------------------ #
    def _try_engine(self):
        """Build a tier-1 detector, or None when not installed."""
        try:
            import openwakeword
            from openwakeword.model import Model as _OWWModel
            openwakeword.utils.download_models()
            model = _OWWModel()
            return ('openwakeword', model)
        except Exception:
            pass
        try:
            from pocketsphinx import LiveSpeech
            speech = LiveSpeech(keyphrase=phrase(), kws_threshold=1e-20)
            return ('pocketsphinx', speech)
        except Exception:
            return None

    def listen_loop(self, recognizer, microphone_factory):
        """
        Tier-2 loop: short listen bursts; when the transcript contains
        the phrase, hand the REMAINDER of the utterance to on_wake.
        Runs until stop().
        """
        self._running = True
        logger.info("wake-word loop active (phrase: %r)", phrase())
        while self._running:
            try:
                with microphone_factory() as source:
                    # A finite timeout keeps the loop responsive to
                    # stop(): with timeout=None a silent room blocks the
                    # listen forever and "Runs until stop()" never
                    # happens.  A burst of silence raises WaitTimeoutError
                    # (~_BURST_S), which just re-loops.
                    audio = recognizer.listen(
                        source, phrase_time_limit=_BURST_S + 2,
                        timeout=_BURST_S + 1)
                try:
                    text = recognizer.recognize_google(audio)
                except Exception:
                    continue
                if text and matches(text):
                    command = strip_wake_word(text)
                    logger.info("wake word hit → %r", command[:80])
                    if self.on_wake and command:
                        self.on_wake(command)
                    elif self.on_wake:
                        self.on_wake('')      # wake with no command
            except Exception as e:
                logger.debug("wake loop hiccup: %s", e)
        logger.info("wake-word loop stopped")

    def start(self, on_wake=None):
        """
        Start listening on a daemon thread (tier 2).  Returns True when
        a microphone + recognizer could be assembled.
        """
        if self._running:
            return True
        if on_wake is not None:
            self.on_wake = on_wake
        try:
            import speech_recognition as sr
        except ImportError:
            logger.warning("speech_recognition unavailable — no wake word")
            return False
        try:
            recognizer = sr.Recognizer()
            microphone_factory = sr.Microphone
        except Exception as e:
            logger.warning("no microphone for wake word: %s", e)
            return False
        import threading
        threading.Thread(target=self.listen_loop,
                         args=(recognizer, microphone_factory),
                         daemon=True, name="WakeWord").start()
        return True

    def stop(self):
        self._running = False


# --------------------------------------------------------------------- #
# Singleton
# --------------------------------------------------------------------- #

_singleton = None


def get_listener():
    global _singleton
    if _singleton is None:
        _singleton = WakeWordListener()
    return _singleton


def _reset_singleton():
    global _singleton
    _singleton = None


# --------------------------------------------------------------------- #
# CLI probe: prints a line every time the phrase is heard
# --------------------------------------------------------------------- #

if __name__ == '__main__':
    print(f"Say '{phrase()}' … (Ctrl+C to stop)")
    get_listener().start(on_wake=lambda cmd: print(
        f"⏰ WAKE → {cmd!r}"))
    import time
    while True:
        time.sleep(1)
