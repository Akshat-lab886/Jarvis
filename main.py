from utils.server import start_server, executor, brain, send_to_ui
from utils.ear import Ear
from config import Config
import argparse
import threading
import time
import signal
import sys
import os
from utils.diagnostics import run_diagnostics

def start_listening_loop():
    """Background thread for local voice commands."""
    ear = Ear()
    if not ear.mic_available:
        print("Microphone not available via PyAudio. Local listening disabled.")
        return

    # Optional wake-word gate ("Hey Jarvis") in front of the loop —
    # opt-in via JARVIS_WAKE_WORD_ENABLE=1 so existing voice users see
    # no behaviour change.
    from utils import wake_word
    if wake_word.wake_enabled():
        armed = wake_word.get_listener().start(
            on_wake=lambda cmd: _process_voice(cmd or "I'm listening, Sir."))
        if armed:
            print(f"Wake word active: say '{wake_word.phrase()}' …")
            return    # the wake listener owns the mic

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
                _process_voice(text)

        except Exception as e:
            print(f"Error in listener loop: {e}")
            time.sleep(1)


def _process_voice(text):
    """Shared voice→pipeline path (used directly and by the wake word)."""
    # Quick actions (missions, auto-build, dev studio, git, email,
    # GUI type/press) — shared with the web dashboard so voice and
    # chat behave identically.
    from utils.quick_actions import handle_quick_actions
    if handle_quick_actions(text, executor, brain, ui_callback=send_to_ui):
        return

    # Normal Brain Processing
    command = brain.think(text)
    executor.execute_command(command, brain, original_text=text, ui_callback=send_to_ui)

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
    parser = argparse.ArgumentParser(description="JARVIS Assistant")
    parser.add_argument('--headless', action='store_true',
                        help='Run without the local voice listener '
                             '(dashboard/Telegram only — daemon friendly)')
    parser.add_argument('--tui', action='store_true',
                        help='Terminal UI: multiline editing, slash '
                             'commands, live tool feed — no browser '
                             'needed (implies --headless)')
    parser.add_argument('--hud', action='store_true',
                        help='Floating HUD composer bar on top of any '
                             'window (implies --headless)')
    args, _unknown = parser.parse_known_args()
    headless = args.headless or args.tui or args.hud or \
        os.getenv('JARVIS_HEADLESS') == '1'

    # Run System Diagnostics (voice deps only matter in voice mode)
    if not run_diagnostics():
        print("System Diagnostics Failed. Aborting startup.")
        sys.exit(1)

    # Register Signal Handler
    signal.signal(signal.SIGINT, shutdown_system)
    signal.signal(signal.SIGTERM, shutdown_system)

    # HUD mode: floating composer only — no server, no voice
    if args.hud:
        from utils.hud import run_hud
        run_hud()
        sys.exit(0)

    # TUI mode: full terminal client (brain + executor, no Flask)
    if args.tui:
        from utils.tui import run_tui
        run_tui()
        sys.exit(0)

    if headless:
        print("Starting JARVIS in HEADLESS daemon mode "
              "(no microphone; dashboard + Telegram active).")
    else:
        # Start Listener Thread
        listener_thread = threading.Thread(target=start_listening_loop,
                                           daemon=True)
        listener_thread.start()

    # Telegram Bot is initialized within start_server to manage process lifecycle

    # Start Flask server
    start_server()
