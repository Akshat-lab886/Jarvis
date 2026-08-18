import asyncio
import threading
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
from utils.tools import Tools
from utils.brain import Brain
from utils.executor import JarvisExecutor

from config import Config


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

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Processes regular text messages from Telegram."""
        if not await self._check_access(update):
            return
        self._notify_chat_id = update.effective_chat.id

        text = update.message.text
        self.logger.info(f"Telegram Message received: {text}")

        # 1. Think
        command = self.brain.think(text)

        # 2. Execute remotely: don't speak aloud on the host machine
        try:
            self.executor.mouth.suppress = True
            result = self.executor.execute_command(command, self.brain, original_text=text)
        finally:
            self.executor.mouth.suppress = False

        # 3. Reply back to Telegram
        await update.message.reply_text(result)

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
            self.application.bot.send_message(chat_id=chat_id, text=text)
            self.logger.info(f"Reminder notification sent to {chat_id}")
        except Exception as e:
            self.logger.error(f"Reminder notify failed: {e}")

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def run_bot(self):
        """Runs the bot polling logic in the current thread (blocking)."""
        try:
            self.logger.info("Starting bot event loop")
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            self.application = ApplicationBuilder().token(self.token).build()

            # Command Handlers
            self.application.add_handler(CommandHandler("start", self.start_command))
            self.application.add_handler(CommandHandler("status", self.status_command))
            self.application.add_handler(CommandHandler("reminders", self.reminders_command))

            # Message Handler for Remote Control
            self.application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), self.handle_message))

            self.logger.info("Telegram Bot polling starting...")
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
