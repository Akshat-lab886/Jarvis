"""
Jarvis Desktop Agent (v20)

Gives Jarvis eyes and hands on the user's desktop:

- Captures the screen with pyautogui and downscales it for the vision model.
- Asks the vision model (Gemini, with Groq vision fallback) for the
  next single action as JSON: click / type / key / done.
- Scales model coordinates back to the real screen resolution and executes
  with pyautogui.
- Loops until the task is done or a step limit is reached.

v19 (Phase 0 · instrumentation)
--------------------------------
Per-phase timing for every step — capture / model / Jev gate / execute /
settle — is recorded in ``self.last_run`` and appended to the result as a
``[timing]`` line; ``utils/desktop_eval.py`` reads it for baselines.

v20 (Phase 1 · speed)
----------------------
Tuned against the Phase 0 baseline probe, which showed the frame pipeline
is cheap (13 ms) while the FIXED SLEEPS are the bottleneck (~1.6 s of dead
time per click step, ~13 s floor on an 8-step task):

* ``POST_ACTION_SLEEP_S (1.0)`` + ``CLICK_MOVE_DURATION_S (0.2)`` and the
  global ``pyautogui.PAUSE (0.2)`` are gone.  PAUSE=0; clicks are one
  ``pyautogui.click(x, y)`` call with no cursor animation.
* **Adaptive settle** replaces the fixed sleep: frame-diff polling
  (160px grayscale thumbs, ≥2 consecutive quiet frames, capped at
  ``SETTLE_MAX_MS``) returns as soon as the screen holds still — and a
  frame is only reused for the next step's model call when the action
  VISIBLY took effect (otherwise the loop forces a fresh capture, so a
  slow/deferred render can never poison the next decision).
* Capture: BILINEAR downscale (was LANCZOS) + JPEG q62 (was q70) — the
  probe showed even LANCZOS is13 ms, so this is a small win, taken
  because it is free.
* **Bounded rolling context**: the model now sees its last3 actions
  (70 chars each — constant bound) instead of nothing, so it stops
  repeating itself; prompt size cannot grow with task length (counters
  the measured "later steps are3x slower" CUA pattern).
* Jev step-gate timeout is explicit: ``JEV_STEP_TIMEOUT_S`` defaults to
  2.0 s — NOT tighter.  The Phase 0 probe measured the real gate at
  1563 ms; a1.2 s cap would fail-open every call and silently disable
  the gate.  Fail-open semantics unchanged.

Safety: pyautogui fail-safe is ON (moving the mouse to a screen corner
aborts execution).  Jev per-step gate and HITL approvals untouched.
"""

import io
import os
import re
import json
import time
import base64
import logging

import pyautogui
from PIL import Image, ImageChops

from config import Config

logger = logging.getLogger("Jarvis.DesktopAgent")

# --------------------------------------------------------------------- #
# Phase 1 · speed constants (see docstring; values chosen from the
# Phase 0 baseline probe on this machine).
# --------------------------------------------------------------------- #
SETTLE_MAX_MS = int(os.getenv("SETTLE_MAX_MS", "600"))        # was sleep(1.0)
SETTLE_INTERVAL_S = float(os.getenv("SETTLE_INTERVAL_S", "0.06"))
SETTLE_DIFF_MAX = float(os.getenv("SETTLE_DIFF_MAX", "0.002"))  # changed-pixel fraction (of thumb)
SETTLE_MIN_STABLE = 2             # consecutive quiet frames before we trust
SETTLE_WIDTH = 160                # frame-diff thumb width (cheap)
TYPE_INTERVAL_S = 0.02            # unchanged: faster drops keys on some apps
PYAUTOGUI_PAUSE = 0.0             # was 0.2 — paid on EVERY pyautogui call
FRAME_QUALITY = 62                # was 70 — UI text stays legible at 1024w
FRAME_MAX_WIDTH = 1024            # model coordinate space
FRAME_RESAMPLE = Image.BILINEAR   # was LANCZOS
HISTORY_STEPS = 3                 # bounded rolling context (was: none)
HISTORY_CHARS = 70
JEV_STEP_TIMEOUT_S = float(os.getenv("JEV_STEP_TIMEOUT_S", "2.0"))
#   ^ Phase 0 measured the Jev gate at 1563 ms: a tighter cap (the
#     originally-planned 1.2 s) would fail-open EVERY call and silently
#     disable the gate.  Keep 2.0 s — speed never outranks safety.

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
    def __init__(self, max_width=FRAME_MAX_WIDTH):
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = PYAUTOGUI_PAUSE
        self.max_width = max_width
        self.screen_w, self.screen_h = pyautogui.size()
        self.last_run = None          # Phase 0: per-run timing record
        self._last_img = None         # Phase 1: frame the model last saw

    # ------------------------------------------------------------------ #
    # Screen capture
    # ------------------------------------------------------------------ #
    @staticmethod
    def _prepare(img, max_width=FRAME_MAX_WIDTH):
        """
        Scale + JPEG-encode a captured frame for the vision model.
        Pure function (no screen access) so the eval harness can time the
        exact pipeline on a synthetic frame without Screen Recording
        permission.
        Returns (base64_jpeg, real_w, real_h, disp_w, disp_h).
        """
        real_w, real_h = img.size
        scale = max_width / real_w
        disp_w = max_width
        disp_h = max(1, int(real_h * scale))
        img = img.convert("RGB").resize((disp_w, disp_h), FRAME_RESAMPLE)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=FRAME_QUALITY)
        return base64.b64encode(buf.getvalue()).decode(), real_w, real_h, disp_w, disp_h

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
        self._last_img = img
        return self._prepare(img, self.max_width)

    # ------------------------------------------------------------------ #
    # Phase 1 · adaptive settle (frame-diff) — replaces the fixed sleep
    # ------------------------------------------------------------------ #
    @staticmethod
    def _thumb(img):
        """Cheap grayscale thumbnail for frame-diffing."""
        w = SETTLE_WIDTH
        h = max(1, int(img.height * w / img.width))
        return img.convert("L").resize((w, h), Image.BILINEAR)

    @staticmethod
    def _changed_frac(a, b):
        """Fraction of thumb pixels that moved ≥19 gray levels."""
        if a is None or b is None or a.size != b.size:
            return 1.0
        hist = ImageChops.difference(a, b).histogram()
        return sum(hist[19:]) / float(a.width * a.height)

    def _settle(self, seen_img):
        """Poll until the screen holds still.

        Returns ``(img, ms, saw_change)``:
          * stops after SETTLE_MIN_STABLE consecutive frames that differ
            ≤ SETTLE_DIFF_MAX from the previous frame, or at SETTLE_MAX_MS;
          * ``saw_change`` — at least one post-action frame differed from
            the frame the model saw, i.e. the action visibly took effect.
            When False the returned frame may be a pre-render/stale view,
            so the caller must NOT reuse it (fresh capture instead).

        Replaces the old fixed ``time.sleep(1.0)`` — fast actions (~120 ms)
        return in a couple of polls, slow animations keep polling to the
        cap, and nothing waits blindly.
        """
        t0 = time.time()
        deadline = t0 + SETTLE_MAX_MS / 1000.0
        pre = self._thumb(seen_img) if seen_img is not None else None
        prev = pre
        quiet = 0
        saw_change = False
        img = seen_img
        while time.time() < deadline:
            time.sleep(SETTLE_INTERVAL_S)
            try:
                img = pyautogui.screenshot()
            except Exception:
                break
            t = self._thumb(img)
            if not saw_change and self._changed_frac(pre, t) > SETTLE_DIFF_MAX:
                saw_change = True
            if self._changed_frac(prev, t) <= SETTLE_DIFF_MAX:
                quiet += 1
                if quiet >= SETTLE_MIN_STABLE:
                    break
            else:
                quiet = 0
            prev = t
        return img, (time.time() - t0) * 1000, saw_change

    # ------------------------------------------------------------------ #
    # Vision model calls
    # ------------------------------------------------------------------ #
    def _ask_gemini(self, prompt, image):
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=Config.GOOGLE_API_KEY)
        resp = client.models.generate_content(
            model=Config.GEMINI_MODEL,
            contents=[prompt, image],
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
            ),
        )
        return resp.text

    def _ask_groq(self, prompt, image_b64):
        from openai import OpenAI
        client = OpenAI(base_url="https://api.groq.com/openai/v1",
                        api_key=Config.GROQ_API_KEY)
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
        return self._ask_groq(prompt, image_b64)

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
            # Phase 1: one call, no cursor animation (PAUSE=0) — was
            # moveTo(duration=0.2) + click(), ~0.4 s of pure overhead.
            pyautogui.click(x, y)
            return f"Clicked ({x}, {y})"
        if atype == "type":
            text = action.get("text", "")
            if text:
                pyautogui.write(text, interval=TYPE_INTERVAL_S)
            return f"Typed: {text[:40]!r}"
        if atype == "key":
            key = action.get("key", "")
            if key:
                pyautogui.press(key)
            return f"Pressed: {key}"
        return f"Unknown action type: {atype}"

    # ------------------------------------------------------------------ #
    # Phase 0 · timing bookkeeping
    # ------------------------------------------------------------------ #
    @staticmethod
    def _timings(run, msg):
        """Attach a compact [timing] line to a result string + summarise.

        Stores everything in ``run`` (published as ``self.last_run``) for
        the eval harness.  Never raises — timing must not break results.
        """
        try:
            steps = run.setdefault("steps", [])
            n = len(steps)
            wall = time.time() - run.get("t0", time.time())
            run["wall_s"] = round(wall, 2)

            def avg(key):
                vals = [s[key] for s in steps if s.get(key) is not None]
                return (sum(vals) / len(vals)) if vals else None

            parts = [f"steps={n}", f"wall={wall:.1f}s"]
            for label, key in (("capture", "capture_ms"),
                               ("model", "model_ms"),
                               ("gate", "gate_ms"),
                               ("exec", "execute_ms"),
                               ("settle", "settle_ms")):
                v = avg(key)
                if v is not None:
                    parts.append(f"{label}={v:.0f}ms")
            parts.append(f"model_calls={run.get('model_calls', 0)}")
            run["summary"] = " | ".join(parts)
            logger.info("desktop_task %s", run["summary"])
        except Exception:
            run.setdefault("summary", "timing unavailable")
        return f"{msg}\n[timing] {run.get('summary', '?')}"

    # ------------------------------------------------------------------ #
    # Main loop
    # ------------------------------------------------------------------ #
    def run_task(self, task, max_steps=None):
        run = {"task": str(task)[:200], "steps": [], "model_calls": 0,
               "gate_calls": 0, "t0": time.time()}
        self.last_run = run

        if not Config.GOOGLE_API_KEY and not Config.GROQ_API_KEY:
            return self._timings(
                run, "Error: No vision model configured "
                     "(need GOOGLE_API_KEY or GROQ_API_KEY).")

        max_steps = max_steps or Config.DESKTOP_AGENT_MAX_STEPS
        log = []

        # Jev per-step safety gate — fail-open (see utils/jev.py).
        # The loop gets ONE approval up front, then runs blind; this adds
        # a sub-second typed check before each step touches the desktop.
        # No key / any error → runs exactly as it does today.
        try:
            from utils import jev as _jev
            jev_ok = _jev.enabled()
        except Exception:
            jev_ok = False

        pending = None   # settled frame reused as next capture (Phase 1)

        for i in range(max_steps):
            step = {"i": i + 1}
            t = time.time()
            try:
                if pending is not None:
                    # Reuse the settled post-action frame — it IS the
                    # current state (saw_change verified the action took
                    # effect), so skip one full capture.
                    self._last_img = pending
                    b64, real_w, real_h, disp_w, disp_h = self._prepare(
                        pending, self.max_width)
                    pending = None
                    step["frame_reused"] = True
                else:
                    b64, real_w, real_h, disp_w, disp_h = \
                        self.capture_screen()
                    step["frame_reused"] = False
            except Exception as e:
                step["capture_ms"] = (time.time() - t) * 1000
                run["steps"].append(step)
                return self._timings(run, f"Error: {e}")
            step["capture_ms"] = (time.time() - t) * 1000

            # Bounded rolling context (Phase 1): last HISTORY_STEPS
            # actions, each capped — constant prompt size, so late steps
            # don't get slower and the model stops repeating itself.
            recent = [x[:HISTORY_CHARS] for x in log[-HISTORY_STEPS:]]
            hist = "; ".join(recent) if recent else "none"
            prompt = (f"TASK: {task}\n"
                      f"This is screenshot {i + 1} of up to {max_steps}. "
                      f"Recent actions: {hist}. "
                      f"Decide the next single action.")
            t = time.time()
            try:
                raw = self._ask(prompt, b64)
            except Exception as e:
                step["model_ms"] = (time.time() - t) * 1000
                run["steps"].append(step)
                return self._timings(
                    run, f"Error talking to vision model: {e}\n"
                         f"Progress so far: {log}")
            step["model_ms"] = (time.time() - t) * 1000
            run["model_calls"] += 1

            action = self._parse_action(raw)
            if not action:
                run["steps"].append(step)
                return self._timings(
                    run, f"I couldn't understand the vision model's response "
                         f"({raw[:200]}). Stopping.\nProgress: {log}")

            step["action"] = action.get("type")

            if action.get("type") == "done":
                run["steps"].append(step)
                return self._timings(
                    run, f"Task complete: {action.get('summary', 'done')}\n"
                         f"Actions: {log}")

            # Per-step Jev gate: abort on a high-confidence hazard
            # (credential / delete / spend / outbound send), skip
            # high-confidence off-task drift. Fail-open on any error.
            #
            # Only CONTENT-BEARING steps are screened. A bare coordinate
            # click ({type,x,y}) gives the (text-only) model nothing to
            # judge — it can't see the screen — so gating it would skip
            # valid clicks on an uncertain score. Clicks carry no
            # injectable text anyway; type/key steps are where the
            # off-task / credential / outbound risk actually lives.
            _is_text_step = (action.get("type") in ("type", "key")
                             or bool(action.get("text"))
                             or bool(action.get("key")))
            if jev_ok and _is_text_step:
                t = time.time()
                try:
                    verdict = _jev.screen_step(task, action,
                                               timeout=JEV_STEP_TIMEOUT_S)
                except Exception:
                    verdict = None
                step["gate_ms"] = (time.time() - t) * 1000
                run["gate_calls"] += 1
                if verdict is not None:
                    if _jev.step_should_abort(verdict):
                        run["steps"].append(step)
                        return self._timings(
                            run, f"Safety gate (Jev) stopped step {i + 1}: "
                                 f"proposed action flagged hazardous "
                                 f"(p={verdict.get('hazard'):.2f}).\n"
                                 f"Progress: {log}")
                    if _jev.step_off_task(verdict):
                        run["steps"].append(step)
                        log.append(f"[gate] skipped off-task step {i + 1} "
                                   f"(p_on_task="
                                   f"{verdict.get('on_task'):.2f})")
                        continue

            t = time.time()
            try:
                result = self._execute(action, real_w, real_h, disp_w, disp_h)
                log.append(result)
            except Exception as e:
                step["execute_ms"] = (time.time() - t) * 1000
                run["steps"].append(step)
                return self._timings(run,
                                     f"Action failed: {e}\nProgress: {log}")
            step["execute_ms"] = (time.time() - t) * 1000

            # Phase 1 · adaptive settle: frame-diff until the screen holds
            # still (cap SETTLE_MAX_MS) — replaces fixed sleep(1.0).
            # Reuse the frame for the next step ONLY if the action visibly
            # took effect; otherwise force a fresh capture next step so a
            # deferred render can never feed the model a stale screen.
            settled, settle_ms, saw_change = self._settle(self._last_img)
            step["settle_ms"] = settle_ms
            self._last_img = settled
            pending = settled if saw_change else None

            run["steps"].append(step)

        return self._timings(
            run, f"Reached the step limit ({max_steps}) without finishing. "
                 f"Progress: {log}")
