"""
JARVIS Ear — phone/desktop microphone capture → speech-to-text.

Tier A voice path (capture side): mic → `speech_recognition` → text →
`_send_to_ui('user_text')` → the agent loop.  Designed to NEVER raise:
every failure path returns None and surfaces a status update so the UI
stays responsive even with no mic, no network, or a flaky STT backend.
"""

import logging

import speech_recognition as sr

logger = logging.getLogger("Jarvis")


def _send_to_ui(event, data):
    """Lazy import to avoid any module-load order issues with utils.server."""
    try:
        from utils.server import send_to_ui
        send_to_ui(event, data)
    except Exception:
        pass


class Ear:
    def __init__(self):
        self.recognizer = sr.Recognizer()
        self.mic_available = False
        try:
            import pyaudio  # noqa: F401 — presence check only
            self.mic_available = True
        except ImportError:
            self.mic_available = False
            logger.warning("PyAudio not installed. Voice features disabled.")
            _send_to_ui('status', {'message': 'Error: PyAudio not installed (No Mic)'})

    def listen(self):
        """Capture one utterance and return its text.

        Returns None on any failure (no mic, STT error, network error)
        — the caller treats a None result as 'no voice input this turn'.
        Never raises.
        """
        if not self.mic_available:
            return None

        try:
            with sr.Microphone() as source:
                logger.info("Ear: listening")
                _send_to_ui('status', {'message': 'Listening...'})
                self.recognizer.adjust_for_ambient_noise(source)
                audio = self.recognizer.listen(source)

                logger.info("Ear: processing")
                _send_to_ui('status', {'message': 'Processing...'})

                text = self._recognize(audio)
                if text:
                    logger.info("Ear: user said: %s", text)
                    _send_to_ui('user_text', {'text': text})
                return text
        except sr.UnknownValueError:
            logger.warning("Ear: could not understand audio")
            _send_to_ui('status', {'message': 'Could not understand audio'})
            return None
        except sr.RequestError as e:
            logger.error("Ear: STT request error: %s", e)
            _send_to_ui('status', {'message': f"API Error: {e}"})
            return None
        except Exception as e:
            logger.error("Ear: unexpected error: %s", e, exc_info=True)
            _send_to_ui('status', {'message': f"Error: {e}"})
            return None

    def _recognize(self, audio):
        """Speech-to-text backend.

        Tries Google's free STT first (works offline? no — needs network),
        then degrades gracefully.  Returns str or None.  Subclasses or a
        future local recognizer (Vosk/pocketsphinx) can override this hook
        to make the Tier A stack fully offline.
        """
        try:
            return self.recognizer.recognize_google(audio)
        except sr.UnknownValueError:
            raise
        except sr.RequestError:
            raise
        except Exception as e:
            logger.error("Ear: STT backend failed: %s", e)
            return None
