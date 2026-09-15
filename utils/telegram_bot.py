import asyncio
import os
import glob
import logging
import subprocess
import tempfile
import threading
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
from utils.tools import Tools
from utils.brain import Brain
from utils.executor import JarvisExecutor

from config import Config


def _transcribe_audio_file(audio_path):
    """
    Speech-to-text for Telegram voice notes (OGG/Opus).

    Converts to 16kHz mono WAV via ffmpeg, then uses the same Google
    Web Speech backend as local mic input.  Raises on any failure.
    """
    import speech_recognition as sr

    wav_fd, wav_path = tempfile.mkstemp(suffix='.wav')
    os.close(wav_fd)
    try:
        conv = subprocess.run(
            ['ffmpeg', '-y', '-loglevel', 'error',
             '-i', audio_path, '-ar', '16000', '-ac', '1', wav_path],
            capture_output=True, timeout=30,
        )
        if conv.returncode != 0:
            raise RuntimeError(
                f"ffmpeg conversion failed: {conv.stderr[:200]}")

        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_path) as source:
            audio = recognizer.record(source)
        return recognizer.recognize_google(audio)
    finally:
        try:
            if os.path.exists(wav_path):
                os.remove(wav_path)
        except OSError:
            pass


class JarvisTeleBot:
    def __init__(self, executor, brain=None):
        self.token = Config.TELEGRAM_TOKEN
        self.executor = executor
        # Share the main Brain instance (single history, single set of API
        # clients) instead of spinning up an independent one per bot.
        self.brain = brain or getattr(executor, 'brain', None) or Brain()
        self.tools = executor.tools
        self.application = None
        self.logger = logging.getLogger("Jarvis.Telegram")
        self._notify_chat_id = None
        # Event loop the polling runs on (captured in run_bot).  PTB v20's
        # bot.* methods are coroutines; reminder sends originate on the
        # scheduler thread, so they hop here thread-safely.
        self._bot_loop = None
        self.logger.info("Initializing Telegram Bot")

    # ------------------------------------------------------------------ #
    # Authorization
    # ------------------------------------------------------------------ #
    def _is_allowed(self, user_id):
        """
        Only configured user IDs may control Jarvis remotely.
        Empty allowlist = locked down (secure default).
        """
        return Config.TELEGRAM_ALLOWED_IDS and user_id in Config.TELEGRAM_ALLOWED_IDS

    async def _check_access(self, update: Update) -> bool:
        user = update.effective_user
        if self._is_allowed(user.id):
            return True
        self.logger.warning(
            f"Blocked unauthorized Telegram user: id={user.id} username={user.username or '?'}"
        )
        await update.message.reply_text(
            "Access denied. Your Telegram user ID is not authorized.\n"
            "Add your ID to TELEGRAM_ALLOWED_IDS in the .env file."
        )
        return False

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self.logger.info("Received /start command")
        if not await self._check_access(update):
            return
        self._notify_chat_id = update.effective_chat.id
        await update.message.reply_text("Jarvis Remote Uplink Established.")

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self.logger.info("Received /status command")
        if not await self._check_access(update):
            return
        self._notify_chat_id = update.effective_chat.id
        vitals = self.tools.get_system_vitals()
        cpu = vitals.get('cpu', 'N/A')
        battery = vitals.get('battery', 'N/A')
        response = f"Systems Operational. CPU: {cpu}%, Battery: {battery}%."
        await update.message.reply_text(response)

    async def reminders_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self.logger.info("Received /reminders command")
        if not await self._check_access(update):
            return
        self._notify_chat_id = update.effective_chat.id
        await update.message.reply_text(self.executor.scheduler.list_reminders())

    async def _process_and_reply(self, update: Update,
                                 context: ContextTypes.DEFAULT_TYPE, text: str,
                                 kind: str = 'text', image_path=None):
        """
        Shared pipeline: publish a normalized event onto the integration
        bus; the server-side handler thinks/executes (host-silent for
        Telegram) and the reply lands back here.
        """
        self.logger.info(f"Telegram Message received ({kind}): {text[:80]}")
        loop = asyncio.get_event_loop()
        chat_id = update.effective_chat.id

        def _reply(message):
            asyncio.run_coroutine_threadsafe(
                update.message.reply_text(str(message)[:4000]), loop)

        from utils.event_bus import get_bus
        bus = get_bus()

        if kind == 'photo':
            event = bus.from_telegram_photo(text, image_path=image_path,
                                            chat_id=chat_id, reply=_reply)
        elif kind == 'voice':
            event = bus.from_telegram_voice(text, chat_id=chat_id,
                                            reply=_reply)
        else:
            event = bus.from_telegram_text(text, chat_id=chat_id,
                                           reply=_reply)

        await loop.run_in_executor(None, lambda: bus.publish(event))

    async def handle_message(self, update: Update,
                             context: ContextTypes.DEFAULT_TYPE):
        """Processes regular text messages from Telegram."""
        if not await self._check_access(update):
            return
        self._notify_chat_id = update.effective_chat.id
        await self._process_and_reply(update, context, update.message.text)

    # ------------------------------------------------------------------ #
    # Multimodal: voice notes + photos
    # ------------------------------------------------------------------ #
    async def handle_voice(self, update: Update,
                           context: ContextTypes.DEFAULT_TYPE):
        """Transcribes voice notes and runs them through the brain."""
        if not await self._check_access(update):
            return
        self._notify_chat_id = update.effective_chat.id

        clip = update.message.voice or update.message.audio
        if not clip:
            return
        await context.bot.send_chat_action(chat_id=update.effective_chat.id,
                                           action="typing")
        fd, ogg_path = tempfile.mkstemp(suffix='.ogg')
        os.close(fd)
        try:
            tg_file = await context.bot.get_file(clip.file_id)
            await tg_file.download_to_drive(ogg_path)
            text = await asyncio.get_event_loop().run_in_executor(
                None, _transcribe_audio_file, ogg_path)
        except Exception as e:
            self.logger.error(f"Voice transcription failed: {e}")
            await update.message.reply_text(
                "I couldn't transcribe that voice note, Sir.")
            return
        finally:
            for stale in [ogg_path] + glob.glob(ogg_path.replace(
                    '.ogg', '*.wav')):
                try:
                    if os.path.exists(stale):
                        os.remove(stale)
                except OSError:
                    pass

        if not text or not text.strip():
            await update.message.reply_text("The voice note came back empty.")
            return
        await self._process_and_reply(update, context, text, kind='voice')

    async def handle_photo(self, update: Update,
                           context: ContextTypes.DEFAULT_TYPE):
        """Analyzes photo messages through the vision pipeline."""
        if not await self._check_access(update):
            return
        self._notify_chat_id = update.effective_chat.id

        photo = update.message.photo[-1] if update.message.photo else None
        if not photo:
            return
        caption = (update.message.caption or
                   "What do you see in this image? Describe it concisely.")

        await context.bot.send_chat_action(chat_id=update.effective_chat.id,
                                           action="upload_photo")
        fd, img_path = tempfile.mkstemp(suffix='.jpg')
        os.close(fd)
        try:
            tg_file = await context.bot.get_file(photo.file_id)
            await tg_file.download_to_drive(img_path)
            await self._process_and_reply(update, context, caption,
                                          kind='photo',
                                          image_path=img_path)
        except Exception as e:
            self.logger.error(f"Photo analysis failed: {e}")
            await update.message.reply_text(
                "I couldn't analyze that image, Sir.")
        finally:
            try:
                if os.path.exists(img_path):
                    os.remove(img_path)
            except OSError:
                pass

    # ------------------------------------------------------------------ #
    # Reminder notifications (called from the scheduler thread)
    # ------------------------------------------------------------------ #
    def notify_reminder(self, reminder):
        """Push a fired reminder to the authorized chat."""
        if not self.application:
            return
        chat_id = self._notify_chat_id
        if not chat_id and Config.TELEGRAM_ALLOWED_IDS:
            chat_id = Config.TELEGRAM_ALLOWED_IDS[0]
        if not chat_id:
            return
        try:
            text = f"⏰ Reminder: {reminder['text']}"
            # bot.send_message is a coroutine in PTB v20; from this
            # non-async scheduler thread it must be awaited on the polling
            # loop or the message is silently dropped.
            self._send(self.application.bot.send_message(
                chat_id=chat_id, text=text))
            self.logger.info(f"Reminder notification sent to {chat_id}")
        except Exception as e:
            self.logger.error(f"Reminder notify failed: {e}")

    def _send(self, coro, timeout=30):
        """Run one async bot call from a non-async thread.

        Returns True when the call completed; logs and returns False on
        failure.  Uses the polling loop (captured in run_bot) when it is
        live — the bot's httpx client is bound to that loop — and only
        falls back to a private loop when no polling loop is reachable.
        """
        loop = self._bot_loop
        if loop is not None and loop.is_running():
            try:
                future = asyncio.run_coroutine_threadsafe(coro, loop)
                future.result(timeout=timeout)
                return True
            except Exception as e:
                self.logger.error(f"Bot async call failed: {e}")
                return False
        try:
            asyncio.run(coro)
            return True
        except Exception as e:
            self.logger.error(f"Bot async call failed: {e}")
            return False

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def run_bot(self):
        """Runs the bot polling logic in the current thread (blocking)."""
        try:
            self.logger.info("Starting bot event loop")

            self.application = ApplicationBuilder().token(self.token).build()

            # Command Handlers
            self.application.add_handler(CommandHandler("start", self.start_command))
            self.application.add_handler(CommandHandler("status", self.status_command))
            self.application.add_handler(CommandHandler("reminders", self.reminders_command))

            # Message Handler for Remote Control
            self.application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), self.handle_message))

            # Multimodal handlers: voice notes + photos
            self.application.add_handler(MessageHandler(
                (filters.VOICE | filters.AUDIO) & (~filters.COMMAND),
                self.handle_voice))
            self.application.add_handler(MessageHandler(
                filters.PHOTO & (~filters.COMMAND),
                self.handle_photo))

            self.logger.info("Telegram Bot polling starting...")
            # Drive polling on a loop we retain, so the reminder scheduler
            # can hop onto it (see _send).  Older PTB without the
            # custom_run_coroutine hook falls back to run_polling's own loop.
            try:
                self._bot_loop = asyncio.new_event_loop()
                self.application.run_polling(
                    stop_signals=False,
                    custom_run_coroutine=lambda coro:
                        self._bot_loop.run_until_complete(coro))
            except TypeError:
                self._bot_loop = None
                self.application.run_polling(stop_signals=False)
        except Exception as e:
            self.logger.error(f"Error in Telegram bot: {e}")

    def start_polling(self):
        """Starts the bot in a separate thread."""
        if not self.token:
            self.logger.warning("Telegram Token not found. Bot will not start.")
            return
        if not Config.TELEGRAM_ALLOWED_IDS:
            self.logger.warning(
                "TELEGRAM_ALLOWED_IDS is empty — the bot is running in LOCKED-DOWN mode "
                "(all remote commands are denied). Add your Telegram user ID to .env to enable control."
            )

        self.logger.info("Starting Telegram bot thread")
        bot_thread = threading.Thread(target=self.run_bot, daemon=True)
        bot_thread.start()
