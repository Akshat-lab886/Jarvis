from utils.server import start_server, executor, brain, send_to_ui
from utils.ear import Ear
from config import Config
import threading
import time
import signal
import sys
from utils.diagnostics import run_diagnostics

def start_listening_loop():
    """Background thread for local voice commands."""
    ear = Ear()
    if not ear.mic_available:
        print("Microphone not available via PyAudio. Local listening disabled.")
        return

    print("Requesting Microphone access... (Check OS permissions)")

    while True:
        try:
            # 1. Check if Jarvis is speaking (Prevent Self-Listening)
            if executor.mouth.is_speaking:
                time.sleep(0.1)
                continue

            # 2. Listen
            text = ear.listen()

            # 3. Process
            if text:
                # Double check if he started speaking during processing
                if executor.mouth.is_speaking:
                    continue

                print(f"Local User said: {text}")

                # Quick actions (missions, auto-build, dev studio, git, email,
                # GUI type/press) — shared with the web dashboard so voice and
                # chat behave identically.
                from utils.quick_actions import handle_quick_actions
                if handle_quick_actions(text, executor, brain, ui_callback=send_to_ui):
                    continue

                # Normal Brain Processing
                command = brain.think(text)
                executor.execute_command(command, brain, original_text=text, ui_callback=send_to_ui)

        except Exception as e:
            print(f"Error in listener loop: {e}")
            time.sleep(1)

def shutdown_system(signum, frame):
    print("\nShutting down Jarvis protocols...")

    # Check if agent is loaded (lazy load check) and close it
    # We access _agent directly to avoid triggering lazy load if not already loaded
    if hasattr(executor, '_agent') and executor._agent:
        print("Closing Browser Agent...")
        try:
            executor.agent.close_browser()
            time.sleep(1) # Give it a moment to close
        except Exception as e:
            print(f"Error closing agent: {e}")

    print("Goodnight, Sir.")
    sys.exit(0)

if __name__ == "__main__":
    # Run System Diagnostics
    if not run_diagnostics():
        print("System Diagnostics Failed. Aborting startup.")
        sys.exit(1)

    # Register Signal Handler
    signal.signal(signal.SIGINT, shutdown_system)

    # Start Listener Thread
    listener_thread = threading.Thread(target=start_listening_loop, daemon=True)
    listener_thread.start()

    # Telegram Bot is initialized within start_server to manage process lifecycle

    # Start Flask server
    start_server()
