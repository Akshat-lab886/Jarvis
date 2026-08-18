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
                # Nothing queued -> nothing playing
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
                # If more items are queued, keep is_speaking True; else reset
                with self._lock:
                    self.is_speaking = not self._play_queue.empty()

    def speak(self, text):
        if not text:
            return

        try:
            from utils.server import send_to_ui
            send_to_ui('status', {'message': 'Speaking...'})
        except Exception:
            pass

        logger.info(f"Speaking: {text}")

        if self.suppress:
            # Remote channel: acknowledge but stay silent locally
            return

        if not self.audio_available:
            return

        self._ensure_worker()
        self._play_queue.put(text)

    def _generate_and_play(self, text):
        output_file = "response.mp3"
        voice = "en-US-ChristopherNeural"

        import edge_tts

        async def _generate_tts():
            communicate = edge_tts.Communicate(text, voice)
            await communicate.save(output_file)

        try:
            # Never use asyncio.run() here: it crashes with "cannot be called
            # from a running event loop" when speak() is triggered from a thread
            # that already owns a loop (e.g. the Telegram bot thread). A fresh
            # loop is always safe, even nested inside another running loop.
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(_generate_tts())
            finally:
                loop.close()
            if not os.path.exists(output_file):
                logger.error("TTS Error: Output file not created.")
                return
            logger.info(f"TTS generated: {output_file} ({os.path.getsize(output_file)} bytes)")
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
        except Exception:
            pass
