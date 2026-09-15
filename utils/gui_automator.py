import re
import pyautogui
import time
import subprocess
import platform

_APP_NAME_UNSAFE = re.compile(r'[^A-Za-z0-9 .\-_/]')


def _safe_app_name(name):
    """Strip shell metacharacters from a launcher name.

    On Windows/Linux the app name becomes part of a shell command line
    (``start <name>``), so a name supplied by the LLM must never be able
    to smuggle extra commands — ``foo; rm -rf ~`` would otherwise run
    verbatim.  macOS uses argv-only ``open -a`` and needs no filtering.
    """
    return _APP_NAME_UNSAFE.sub('', str(name or ''))

class Operator:
    def __init__(self):
        # Safety Feature: Failsafe triggered by moving mouse to top-left corner
        pyautogui.FAILSAFE = True
        print("Operator Module Initialized. PyAutoGUI Failsafe is ON.")

    def open_app(self, app_name):
        """
        Opens an application based on the OS.
        """
        system = platform.system()
        # Validate app name: alphanumeric, spaces, hyphens, dots only
        safe = _safe_app_name(app_name)
        if not safe:
            return "Refusing unsafe app name."
        try:
            if system == "Darwin": # macOS
                subprocess.run(["open", "-a", safe], timeout=10)
                return f"Opening {safe} on macOS."
            elif system == "Windows":
                # 'start' is a cmd builtin, so the name still reaches a
                # shell — keep only launcher-safe characters.
                subprocess.run(["start", safe], shell=True, timeout=10)
                return f"Opening {safe} on Windows."
            else: # Linux
                # No shell: subprocess resolves the command via PATH on
                # its own, so a hostile name can't inject extra commands.
                subprocess.run([safe], timeout=10)
                return f"Attempting to open {safe} on Linux."
        except Exception as e:
            return f"Failed to open {safe}: {e}"

    def write_text(self, text):
        """
        Types text with a small delay to simulate human typing.
        """
        try:
            pyautogui.write(text, interval=0.05)
            return "Typed text."
        except Exception as e:
            return f"Typing error: {e}"

    def press_key(self, key):
        """
        Presses a specific key (e.g., 'enter', 'space', 'esc').
        """
        try:
            pyautogui.press(key)
            return f"Pressed {key}."
        except Exception as e:
            return f"Key press error: {e}"
