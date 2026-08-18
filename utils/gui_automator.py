import pyautogui
import time
import subprocess
import platform

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
        try:
            if system == "Darwin": # macOS
                subprocess.run(["open", "-a", app_name])
                return f"Opening {app_name} on macOS."
            elif system == "Windows":
                subprocess.run(["start", app_name], shell=True)
                return f"Opening {app_name} on Windows."
            else: # Linux
                subprocess.run([app_name], shell=True) # Simple attempt
                return f"Attempting to open {app_name} on Linux."
        except Exception as e:
            return f"Failed to open {app_name}: {e}"

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
