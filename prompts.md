We are building Version 18.0: The Desktop Agent.

1. **Update `utils/desktop_agent.py`:**
   - Import `pyautogui`, `base64`, `io`, `PIL.Image`.
   - Create class `DesktopAgent`.
   
   - Add method `capture_screen(self)`:
     - `screenshot = pyautogui.screenshot()`
     - Resize it to something manageable (e.g., 1024x768) to save API tokens, but keep the aspect ratio.
     - Convert to Base64 string.
     - Return the base64 string AND the original screen size (width, height) for coordinate scaling.

   - Add method `perform_action(self, action_json, screen_size)`:
     - `action_type = action_json['type']` (click, type, key, scroll).
     - **Coordinate Scaling:** Gemini sees the 1024px image. Your Mac is likely 2560px (Retina). You MUST scale the X/Y coordinates up.
     - `scale_x = real_width / 1024`, `scale_y = real_height / 768`.
     - `real_x = action_json['x'] * scale_x`.
     - Execute:
       - If `click`: `pyautogui.moveTo(real_x, real_y); pyautogui.click()`.
       - If `type`: `pyautogui.write(action_json['text'])`.
       - If `key`: `pyautogui.press(action_json['key'])`.

2. **Update `utils/brain.py`:**
   - Update System Prompt for Desktop Mode:
     """
     DESKTOP AGENT MODE:
     - You will receive a SCREENSHOT of the user's computer.
     - User Request: [Goal].
     - Based on the image, decide the next mouse/keyboard action.
     - Output JSON: {"type": "click", "x": [0-1024], "y": [0-768], "reason": "Clicking the File menu"} 
     - OR {"type": "type", "text": "Hello"}
     - OR {"type": "done"} if task is complete.
     """

Goal: I say "Jarvis, empty my trash," and he looks at the screen, finds the trash icon, and clicks it.

