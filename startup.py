import os
import sys
import time
import subprocess

# Add current directory to path so imports work
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from utils.server import start_server
from config import Config
from utils.telegram_bot import JarvisTeleBot

def print_logo():
    logo = r"""
       _                      _     
      | | __ _ _ ____   _(_)___ 
   _  | |/ _` | '__\ \ / / / __|
  | |_| | (_| | |   \ V /| \__ \
   \___/ \__,_|_|    \_/ |_|___/
    Just A Rather Very Intelligent System
    """
    print("\033[96m" + logo + "\033[0m") # Cyan color

def play_boot_sound():
    try:
        # Mac system sound (Hero is a classic boot-like sound)
        # Using subprocess to play it in background so it doesn't block startup too long?
        # Actually, let's play it synchronously for dramatic effect, it's short.
        subprocess.run(["afplay", "/System/Library/Sounds/Hero.aiff"], stderr=subprocess.DEVNULL)
    except Exception:
        # Fallback sound or silence
        print("\a") # System beep

def main():
    os.system('clear') # Clear terminal
    print_logo()
    print("\033[90mInitializing systems...\033[0m")
    
    # Check critical files
    if not os.path.exists(".env"):
        print("\033[91mCRITICAL ERROR: .env file missing! Setup required.\033[0m")
        return
    
    play_boot_sound()
    
    print("\033[92mSystems Operational. Launching Core...\033[0m")
    time.sleep(0.5)
    
    # Start Telegram Bot
    if Config.TELEGRAM_TOKEN:
        print("\033[94mUplinking Telegram Remote...\033[0m")
        bot = JarvisTeleBot(Config.TELEGRAM_TOKEN)
        bot.start()

    # Detecting Local IP for Remote Access
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        print(f"\n\033[96mJarvis Remote Interface available at: https://{local_ip}:{Config.PORT}\033[0m")
        print("\033[93mNOTE: You must accept the 'Invalid Certificate' warning on your device.\033[0m")
    except Exception:
        print("\n\033[93mCould not determine local IP. Check network settings.\033[0m")

    try:
        start_server()
    except KeyboardInterrupt:
        print("\n\n\033[93mShutting down J.A.R.V.I.S. ... Goodbye, Sir.\033[0m")
        sys.exit(0)
    except Exception as e:
        print(f"\n\033[91mSystem Crash: {e}\033[0m")

if __name__ == "__main__":
    main()
