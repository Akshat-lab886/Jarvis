import os
import queue
import threading
import asyncio
import logging

logger = logging.getLogger('Jarvis')

class Mouth:
    """
    Speech output module.

    - Generates TTS with edge-tts and plays it with pygame.
    - Speaking happens on a background worker thread so Jarvis never blocks
      while talking (responses, missions and the dashboard stay responsive).
    - Safe to use on machines with no audio device (falls back to UI-only).
    - `suppress` flag lets remote channels (e.g. Telegram) run commands
      without making the local machine speak.
    """

    def __init__(self):
        logger.info("Mouth initialized")
        self.is_speaking = False
        self.suppress = False
        self.audio_available = True
        self._play_queue = queue.Queue()
        self._worker = None
        self._lock = threading.Lock()
        try:
            import pygame
            pygame.mixer.init()
        except Exception as e:
            self.audio_available = False
            logger.warning(f"Audio device unavailable, speech will be UI-only: {e}")

    def _ensure_worker(self):
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._worker_loop, daemon=True, name="MouthSpeaker")
            self._worker.start()

    def _worker_loop(self):
        """Sequentially generates and plays TTS from the queue."""
        while True:
            try:
                text = self._play_queue.get(timeout=1)
            except queue.Empty:
                with self._lock:
                    self.is_speaking = False
                continue

            with self._lock:
                self.is_speaking = True
            try:
                self._generate_and_play(text)
            except Exception as e:
                logger.error(f"Speech playback error: {e}")
            finally:
                self._play_queue.task_done()
                with self._lock:
                    self.is_speaking = not self._play_queue.empty()

    # Maximum characters to speak — long responses are truncated so the
    # voice finishes quickly.  The full text is already visible on the
    # dashboard.
    MAX_SPEECH_CHARS = 200

    def speak(self, text):
        if not text:
            return

        try:
            from utils.server import send_to_ui
            send_to_ui('status', {'message': 'Speaking...'})
        except Exception:
            pass

        logger.info(f"Speaking: {text[:80]}...")

        if self.suppress:
            return

        if not self.audio_available:
            return

        # Truncate long responses for faster speech
        speak_text = text[:self.MAX_SPEECH_CHARS]
        if len(text) > self.MAX_SPEECH_CHARS:
            speak_text += '...'

        self._ensure_worker()
        self._play_queue.put(speak_text)

    def _emit_tt(self, event, data):
        """Emit a TTS/SocketIO event (never raises — UI-only channel)."""
        try:
            from utils.server import send_to_ui
            send_to_ui(event, data)
        except Exception:
            pass

    def _generate_and_play(self, text):
        import edge_tts
        voice = "en-US-ChristopherNeural"
        output_file = os.path.join(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))), 'response.mp3')

        # --- Stream TTS: collect audio chunks then write once ---
        try:
            loop = asyncio.new_event_loop()
            try:
                async def _collect_chunks():
                    chunks = []
                    communicate = edge_tts.Communicate(text, voice)
                    async for chunk in communicate.stream():
                        if chunk["type"] == "audio" and "data" in chunk:
                            data = chunk["data"]
                            chunks.append(data)
                            # Emit progressive TTS chunks so any SocketIO
                            # client (desktop web UI, mobile relay) can
                            # stream-play progressively instead of waiting
                            # for the whole utterance. Each chunk is a raw
                            # MP3 frame fragment — clients buffer until the
                            # next chunk arrives; playback stays gapless
                            # because edge-tts emits contiguous chunks.
                            self._emit_tt('tts_chunk',
                                          {'data': data, 'len': len(data)})
                    return chunks
                audio_chunks = loop.run_until_complete(_collect_chunks())
            finally:
                loop.close()

            if not audio_chunks:
                logger.error("TTS: No audio chunks received.")
                return

            with open(output_file, 'wb') as f:
                for c in audio_chunks:
                    f.write(c)

            logger.info(f"TTS: {os.path.getsize(output_file)} bytes")
        except Exception as e:
            logger.error(f"TTS Generation Error: {e}")
            return

        try:
            import pygame
            pygame.mixer.music.load(output_file)
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy():
                pygame.time.Clock().tick(10)
            logger.info("Audio finished playing.")
        except Exception as e:
            logger.error(f"Pygame Error: {e}")

        try:
            from utils.server import send_to_ui
            send_to_ui('status', {'message': 'Online'})
        except Exception as e:
            pass
