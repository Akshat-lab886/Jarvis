import asyncio
import os
from dotenv import load_dotenv
from telegram.ext import ApplicationBuilder, CommandHandler

load_dotenv()
token = os.getenv('TELEGRAM_TOKEN')

async def start(update, context):
    await update.message.reply_text("Test bot started!")

async def main():
    if not token:
        print("TELEGRAM_TOKEN not found!")
        return
    print(f"Starting test bot with token: {token[:10]}...")
    application = ApplicationBuilder().token(token).build()
    application.add_handler(CommandHandler("start", start))
    
    # We use build() and then start/stop or run_polling
    # run_polling is easier for tests
    await application.initialize()
    await application.start()
    print("Bot is running. Send /start on Telegram.")
    await application.updater.start_polling()
    
    # Keep it running for 30 seconds
    await asyncio.sleep(30)
    
    await application.updater.stop()
    await application.stop()
    await application.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
