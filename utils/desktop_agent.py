"""
Jarvis Desktop Agent (v18)

Gives Jarvis eyes and hands on the user's desktop:

- Captures the screen with pyautogui and downscales it for the vision model.
- Asks the vision model (Gemini, with OpenRouter vision fallback) for the
  next single action as JSON: click / type / key / done.
- Scales model coordinates back to the real screen resolution and executes
  with pyautogui.
- Loops until the task is done or a step limit is reached.

Safety: pyautogui fail-safe is ON (moving the mouse to a screen corner
aborts execution).
"""

import io
import os
import re
import json
import time
import base64

import pyautogui
from PIL import Image

from config import Config

SYSTEM_PROMPT = """You are a desktop automation agent controlling the user's computer.
You receive a screenshot of the screen. Decide the NEXT SINGLE action that makes progress on the task.
Output STRICT JSON only, no markdown, no extra text. One of these forms:
{"type": "click", "x": <0-1024>, "y": <0-768>, "reason": "brief reason"}
{"type": "type", "text": "text to type"}
{"type": "key", "key": "enter"}            (any pyautogui key name: enter, tab, esc, backspace...)
{"type": "done", "summary": "Task complete: ..."}
Coordinates are relative to the 1024-wide screenshot you are shown.
Click only on elements you can actually see. When the task is finished, output the done action."""


class DesktopAgent:
    def __init__(self, max_width=1024):
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.2
        self.max_width = max_width
        self.screen_w, self.screen_h = pyautogui.size()

    # ------------------------------------------------------------------ #
    # Screen capture
    # ------------------------------------------------------------------ #
    def capture_screen(self):
        """
        Take a screenshot, downscale it for the vision model.
        Returns (base64_jpeg, real_w, real_h, display_w, display_h).
        """
        try:
            img = pyautogui.screenshot()
        except Exception as e:
            raise RuntimeError(
                f"Can't capture the screen ({e}). "
                "On macOS, grant Screen Recording permission to the terminal/IDE "
                "running Jarvis (System Settings > Privacy & Security > Screen Recording)."
            )
        real_w, real_h = img.size
        scale = self.max_width / real_w
        disp_w = self.max_width
        disp_h = max(1, int(real_h * scale))
        img = img.convert("RGB").resize((disp_w, disp_h), Image.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=70)
        return base64.b64encode(buf.getvalue()).decode(), real_w, real_h, disp_w, disp_h

    # ------------------------------------------------------------------ #
    # Vision model calls
    # ------------------------------------------------------------------ #
    def _ask_gemini(self, prompt, image):
        import google.generativeai as genai
        genai.configure(api_key=Config.GOOGLE_API_KEY)
        model = genai.GenerativeModel(Config.GEMINI_MODEL, system_instruction=SYSTEM_PROMPT)
        resp = model.generate_content([prompt, image])
        return resp.text

    def _ask_openrouter(self, prompt, image_b64):
        from openai import OpenAI
        client = OpenAI(base_url="https://openrouter.ai/api/v1",
                        api_key=Config.OPENROUTER_API_KEY)
        # Prefer a vision-capable model from the configured list
        vision = [m for m in Config.MODELS if any(v in m for v in ('vl', 'vision', 'gemini', 'dots', 'gpt-4o'))]
        model = (vision or Config.MODELS or ["nvidia/nemotron-nano-12b-v2-vl:free"])[0]
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {
                    "url": f"data:image/jpeg;base64,{image_b64}"}},
            ]},
        ]
        resp = client.chat.completions.create(model=model, messages=messages)
        return resp.choices[0].message.content

    def _ask(self, prompt, image_b64):
        if Config.GOOGLE_API_KEY:
            img = Image.open(io.BytesIO(base64.b64decode(image_b64)))
            return self._ask_gemini(prompt, img)
        return self._ask_openrouter(prompt, image_b64)

    # ------------------------------------------------------------------ #
    # Action parsing & execution
    # ------------------------------------------------------------------ #
    def _parse_action(self, text):
        text = (text or "").strip()
        text = re.sub(r"^```(?:json)?", "", text).rstrip("`").strip()
        try:
            return json.loads(text)
        except Exception:
            m = re.search(r"(\{.*\})", text, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group(1))
                except Exception:
                    return None
        return None

    def _execute(self, action, real_w, real_h, disp_w, disp_h):
        atype = action.get("type")
        if atype == "click":
            x = round(int(action.get("x", 0)) * real_w / disp_w)
            y = round(int(action.get("y", 0)) * real_h / disp_h)
            x = min(real_w - 1, max(0, x))
            y = min(real_h - 1, max(0, y))
            pyautogui.moveTo(x, y, duration=0.2)
            pyautogui.click()
            return f"Clicked ({x}, {y})"
        if atype == "type":
            text = action.get("text", "")
            if text:
                pyautogui.write(text, interval=0.02)
            return f"Typed: {text[:40]!r}"
        if atype == "key":
            key = action.get("key", "")
            if key:
                pyautogui.press(key)
            return f"Pressed: {key}"
        return f"Unknown action type: {atype}"

    # ------------------------------------------------------------------ #
    # Main loop
    # ------------------------------------------------------------------ #
    def run_task(self, task, max_steps=None):
        if not Config.GOOGLE_API_KEY and not Config.OPENROUTER_API_KEY:
            return "Error: No vision model configured (need GOOGLE_API_KEY or OPENROUTER_API_KEY)."

        max_steps = max_steps or Config.DESKTOP_AGENT_MAX_STEPS
        log = []

        for i in range(max_steps):
            try:
                b64, real_w, real_h, disp_w, disp_h = self.capture_screen()
            except Exception as e:
                return f"Error: {e}"
            prompt = (f"TASK: {task}\n"
                      f"This is screenshot {i + 1} of up to {max_steps}. "
                      f"Decide the next single action.")
            try:
                raw = self._ask(prompt, b64)
            except Exception as e:
                return (f"Error talking to vision model: {e}\n"
                        f"Progress so far: {log}")

            action = self._parse_action(raw)
            if not action:
                return (f"I couldn't understand the vision model's response "
                        f"({raw[:200]}). Stopping.\nProgress: {log}")

            if action.get("type") == "done":
                return f"Task complete: {action.get('summary', 'done')}\nActions: {log}"

            try:
                result = self._execute(action, real_w, real_h, disp_w, disp_h)
                log.append(result)
            except Exception as e:
                return f"Action failed: {e}\nProgress: {log}"

            time.sleep(1.0)

        return (f"Reached the step limit ({max_steps}) without finishing. "
                f"Progress: {log}")
