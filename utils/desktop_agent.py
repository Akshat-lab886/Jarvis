"""
Jarvis Desktop Agent (v21.1)

Gives Jarvis eyes and hands on the user's desktop:

- Captures the screen and downscales it for the vision model.
- Asks the vision model for a short PLAN of next actions (JSON).
- Executes them with per-action settle + per-action Jev safety gate,
  re-planning from a fresh frame after each chunk.
- Falls back to coordinate clicks when AX grounding is unavailable.

v19 (Phase 0 · instrumentation)
--------------------------------
Per-phase timing in ``self.last_run`` + ``[timing]`` line on results;
magic sleeps hoisted to module constants; ``utils/desktop_eval.py``
measures baselines.

v20 (Phase 1 · speed)
----------------------
Adaptive frame-diff settle (floor ~0.12 s, cap 0.6 s) replaced the fixed
sleep(1.0); PAUSE=0; direct ``click(x, y)``; BILINEAR + JPEG q62;
bounded rolling context (last 3 actions); ``JEV_STEP_TIMEOUT_S=2.0``
kept deliberately (measured gate latency ~1.5 s — a tighter cap would
fail-open every call and disable the gate).  Baseline dead time per
8-step task: ~12.8 s → ~1.1 s.

v21 (Phase 2 + Phase 3)
------------------------
Phase 2 — chunked planning:
  One model call returns ``{"plan": [2-4 actions], "done": null}``; the
  loop executes up to ``PLAN_CHUNK`` of them (settle + Jev gate AFTER
  EVERY action — chunking never skips a gate), then re-plans from a
  fresh/reused frame.  Model calls per task ≈ ceil(actions / chunk) + 1
  instead of one per action.  Legacy single-action replies
  ``{"type": ...}`` still parse (graceful degradation to v20 pacing).
  A call-budget guard (2×max_steps+4) stops a no-progress loop even if
  every step is gate-skipped.

Phase 3 — AX grounding + widened action space:
  ``computer_use.ax_tree()`` snapshots the frontmost app's accessibility
  tree (fail-open → ``[]`` → coords-only prompt) and the plan prompt
  carries bounded element refs: ``{"type":"click_el","ref":N}`` runs in
  BACKGROUND via ``get_driver().click_element`` (cursor never moves).
  New ops: ``hotkey`` chords, ``drag``, ``scroll``, right/double click,
  ``wait``.  ``hotkey`` is CONTENT-BEARING (⌘V pastes!) so it is
  Jev-gated like type/key; pointer ops stay ungated (same trust as
  today's clicks).

v21.1 (self-review fixes)
-------------------------
* **AX circuit breaker** — a missing Automation permission makes
  osascript BLOCK up to the driver's 12 s timeout; without a breaker
  every plan call would eat that stall for the life of the run.  Now a
  2 s-capped ping runs once per run (skips the tree entirely when
  denied) and ANY empty/failed tree latches ``_ax_off`` for the rest of
  the run (ref map cleared first — a stale ref must never click).
* **Key-name normalization** — measured against
  ``pyautogui.KEYBOARD_KEYS``: ``cmd``/``opt``/``control`` are NOT
  valid keys (``command``/``alt``/``ctrl`` are), and the eval prompt
  literally says "cmd+shift+3", so the model WOULD emit ``cmd`` →
  KeyError → "Action failed".  Aliases applied to both ``key`` and
  ``hotkey`` ops; single keys lowercased ("Enter" → "enter").

Safety unchanged: pyautogui corner failsafe, Jev per-step gate
fail-open, HITL approvals in the executor, bounded prompt/history.
"""

import io
import os
import re
import json
import time
import base64
import logging
import subprocess

import pyautogui
from PIL import Image, ImageChops

from config import Config

logger = logging.getLogger("Jarvis.DesktopAgent")

# --------------------------------------------------------------------- #
# Phase 1 · speed constants (chosen from the Phase 0 baseline probe:
# the frame pipeline measured only13 ms — NOT the bottleneck — while the
# fixed sleeps measured1.6 s PER CLICK STEP, so the sleeps are what go).
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
#   ^ Phase 0 measured the Jev gate at1563 ms: a tighter cap (the
#     originally-planned1.2 s) would fail-open EVERY call and silently
#     disable the gate.  Keep2.0 s — speed never outranks safety.

# Phase 2 · chunked planning: actions executed per model call.
# 1 = legacy pacing (one action per call).
PLAN_CHUNK = max(1, int(os.getenv("DESKTOP_PLAN_CHUNK", "3")))

# Phase 3 · AX grounding (fail-open to coords-only when unavailable).
AX_ENABLED = os.getenv("DESKTOP_AX", "1") != "0"
AX_MAX_ELEMS = int(os.getenv("DESKTOP_AX_MAX", "40"))
AX_LIST_MAX_CHARS = 900           # prompt budget for the element list
AX_PING_TIMEOUT_S = float(os.getenv("DESKTOP_AX_PING_S", "2.0"))

# v21.1 · aliases to names pyautogui actually accepts.  MEASURED against
# pyautogui.KEYBOARD_KEYS: 'cmd'/'opt'/'control' → False;
# 'command'/'alt'/'ctrl' → True.  The model (and our own eval prompt)
# says "cmd+shift+3" — without this map the hotkey op dies with KeyError.
_KEY_ALIASES = {"cmd": "command", "opt": "alt", "control": "ctrl"}

SYSTEM_PROMPT = """You are a desktop automation agent controlling the user's computer.
You receive a screenshot. Decide the NEXT FEW single actions (2-4; exactly 1 if unsure) that make progress on the task.
Output STRICT JSON only, no markdown. Either a plan:
{"plan":[{"type":"click","x":<0-1024>,"y":<0-768>,"reason":"..."},...],"done":null}
or, when the task is finished: {"plan":[],"done":"Task complete: ..."}
One action per plan item, in execution order. Forms:
{"type":"click","x":N,"y":N}          optional: "button":"right", "dbl":true
{"type":"click_el","ref":N}           N from the UI ELEMENTS list (runs in background; cursor does not move)
{"type":"type","text":"..."}
{"type":"key","key":"enter"}          enter tab esc backspace ...
{"type":"hotkey","keys":["command","shift","3"]}   key CHORD
{"type":"scroll","amount":N}          N>0 up, N<0 down
{"type":"drag","x":N,"y":N,"to_x":N,"to_y":N}
{"type":"wait"}                       pause; screen still catching up
Coordinates are relative to the 1024-wide screenshot shown; click only elements you can actually see; prefer click_el refs when a UI ELEMENTS list is given."""


def _norm_key(k):
    """Lowercase + alias a key name to something pyautogui accepts."""
    k = str(k if k is not None else "").strip().lower()
    return _KEY_ALIASES.get(k, k)


class DesktopAgent:
    def __init__(self, max_width=FRAME_MAX_WIDTH):
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = PYAUTOGUI_PAUSE
        self.max_width = max_width
        self.screen_w, self.screen_h = pyautogui.size()
        self.last_run = None          # Phase 0: per-run timing record
        self._last_img = None         # Phase 1: frame the model last saw
        self._ax_map = {}             # Phase 3: ref -> element dict
        self._ax_ping = None          # v21.1: None=unknown, True/False
        self._ax_off = False          # v21.1: circuit breaker (this run)

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
    # Phase 3 · AX grounding (fail-open → coords-only prompt)
    # ------------------------------------------------------------------ #
    def _ax_ping_ok(self):
        """2 s-capped Automation-permission probe.

        System Events answers this instantly when allowed and BLOCKS on
        the permission dialog when not — capping it turns "hang forever
        x N plan calls" into at most one short stall per run.
        """
        try:
            proc = subprocess.run(
                ['osascript', '-e',
                 'tell application "System Events" to name of first '
                 'process whose frontmost is true'],
                capture_output=True, text=True,
                timeout=AX_PING_TIMEOUT_S)
            return (proc.returncode == 0
                    and bool((proc.stdout or '').strip()))
        except Exception:
            return False

    def _ax_bundle(self):
        """(element-lines, ms) for the plan prompt — '' on any failure.

        Fail-open: AX unavailable/disabled → coords-only prompt, i.e.
        exactly the pre-Phase-3 behaviour.  The ref map is RESET first so
        a stale ref can never click the wrong element.

        Circuit breaker (v21.1): the driver's tree walk can block up to
        its osascript timeout when Automation is denied — so probe once
        per run with a 2 s-capped ping, and latch ``_ax_off`` after ANY
        empty/failed tree (denied, hung, or windowless frontmost app).
        Worst case cost per run: one ping stall — never N×12 s.
        """
        self._ax_map = {}
        if not AX_ENABLED or self._ax_off:
            return "", 0.0
        t0 = time.time()
        if self._ax_ping is None:
            if not self._ax_ping_ok():
                self._ax_ping = False
                self._ax_off = True
                return "", (time.time() - t0) * 1000
            self._ax_ping = True
        try:
            from utils.computer_use import get_driver
            els = get_driver().ax_tree(max_elements=AX_MAX_ELEMS)
        except Exception:
            els = []
        ms = (time.time() - t0) * 1000
        if not els:
            self._ax_off = True     # denied / hung / windowless app
            return "", ms
        lines, chars = [], 0
        for e in els:
            try:
                ref = int(e.get("ref") or (len(lines) + 1))
                role = str(e.get("role", ""))[:18]
                name = " ".join(str(e.get("name", "")).split())[:44]
                line = (f"[{ref}] {role} {name!r} @ "
                        f"({e['x']},{e['y']},{e['w']},{e['h']})")
            except Exception:
                continue
            if chars + len(line) > AX_LIST_MAX_CHARS:
                break
            self._ax_map[ref] = e
            chars += len(line)
            lines.append(line)
        return ("\n" + "\n".join(lines)) if lines else "", ms

    def _prompt(self, task, n, max_steps, hist, ax_list):
        parts = [f"TASK: {task}",
                 f"This is screenshot {n} of up to {max_steps}.",
                 f"Recent actions: {hist}."]
        if ax_list:
            parts.append("UI ELEMENTS (accessibility; prefer click_el "
                         "refs):\n" + ax_list)
        parts.append("Decide the next plan.")
        return "\n".join(parts)

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
    # Action parsing (plan + legacy) & execution
    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_plan(raw):
        """Parse a model reply into ``(actions, done)``.

        Accepts BOTH formats:
          * Phase 2 plan:  {"plan":[...],"done":null|"Task complete: ..."}
          * legacy single: {"type":"click", ...}   (→ one-action plan)
        Returns ``(None, None)`` on anything unparseable, ``( [], done)``
        when the task is finished, else ``(actions, None)``.
        """
        text = (raw or "").strip()
        text = re.sub(r"^```(?:json)?", "", text).rstrip("`").strip()
        obj = None
        try:
            obj = json.loads(text)
        except Exception:
            m = re.search(r"(\{.*\})", text, re.DOTALL)
            if m:
                try:
                    obj = json.loads(m.group(1))
                except Exception:
                    return None, None
        if not isinstance(obj, dict):
            return None, None
        if "plan" in obj:
            acts = obj.get("plan")
            if not isinstance(acts, list):
                return None, None
            acts = [a for a in acts
                    if isinstance(a, dict) and a.get("type")]
            if acts:
                return acts, None
            d = obj.get("done")
            if d not in (None, False, "", "null"):
                return [], str(d)
            return None, None
        if obj.get("type"):
            return [obj], None          # legacy single-action reply
        return None, None

    def _execute(self, action, real_w, real_h, disp_w, disp_h):
        def _pt(x, y):
            x = min(real_w - 1, max(0, int(x)))
            y = min(real_h - 1, max(0, int(y)))
            return x, y

        atype = action.get("type")
        if atype == "click":
            x, y = _pt(action.get("x", 0), action.get("y", 0))
            button = str(action.get("button", "left")).lower()
            dbl = bool(action.get("dbl"))
            # PAUSE=0 (Phase 1): one call, no cursor animation.
            if dbl and button == "right":
                pyautogui.doubleClick(x, y, button="right")
            elif dbl:
                pyautogui.doubleClick(x, y)
            elif button == "right":
                pyautogui.rightClick(x, y)
            else:
                pyautogui.click(x, y)
            tag = " [dbl]" if dbl else (" [right]" if button == "right" else "")
            return f"Clicked ({x}, {y}){tag}"
        if atype == "click_el":
            # Phase 3: background element click — cursor never moves.
            try:
                ref = int(action.get("ref", 0) or 0)
            except (TypeError, ValueError):
                return f"Bad element ref: {action.get('ref')!r}"
            el = (self._ax_map or {}).get(ref)
            if not el:
                return f"Unknown element ref {ref} (stale AX list?)"
            from utils.computer_use import get_driver
            return get_driver().click_element(el.get("app", ""),
                                              el.get("name", ""),
                                              el.get("role"))
        if atype == "hotkey":
            # Chords (⌘⇧3 etc) — the gap the eval hotkey marker proved.
            # CONTENT-BEARING (⌘V pastes!) → Jev-gated before we get here.
            keys = action.get("keys") or action.get("key")
            if isinstance(keys, str):
                keys = [k.strip() for k in re.split(r"[+\-,\s]+", keys)
                        if k.strip()]
            # v21.1: alias to names pyautogui accepts ('cmd' does NOT —
            # measured against KEYBOARD_KEYS; the model says "cmd").
            keys = [_norm_key(k) for k in (keys or [])][:6]
            if len(keys) < 2:
                return f"Hotkey needs ≥2 keys, got {keys!r}"
            pyautogui.hotkey(*keys)
            return f"Pressed: {'+'.join(keys)}"
        if atype == "scroll":
            try:
                amt = int(action.get("amount", 0) or 0)
            except (TypeError, ValueError):
                return f"Scroll bad amount: {action.get('amount')!r}"
            if not amt:
                return "Scroll: no amount"
            pyautogui.scroll(max(-20, min(20, amt)))
            return f"Scrolled {amt:+d}"
        if atype == "drag":
            x1, y1 = _pt(action.get("x", 0), action.get("y", 0))
            x2, y2 = _pt(action.get("to_x", 0), action.get("to_y", 0))
            pyautogui.moveTo(x1, y1)
            pyautogui.mouseDown()
            # small duration so target apps register the trajectory
            pyautogui.moveTo(x2, y2, duration=0.08)
            pyautogui.mouseUp()
            return f"Dragged ({x1},{y1})->({x2},{y2})"
        if atype == "wait":
            # the settle after execute does the actual waiting
            return "Waited."
        if atype == "type":
            text = action.get("text", "")
            if text:
                pyautogui.write(text, interval=TYPE_INTERVAL_S)
            return f"Typed: {text[:40]!r}"
        if atype == "key":
            # v21.1: lowercase + alias ("Enter"→"enter", "control"→"ctrl")
            key = _norm_key(action.get("key"))
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
                               ("ax", "ax_ms"),
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
    # Main loop (Phase 2: plan-chunked; gate AFTER EVERY action)
    # ------------------------------------------------------------------ #
    def run_task(self, task, max_steps=None):
        run = {"task": str(task)[:200], "steps": [], "model_calls": 0,
               "gate_calls": 0, "t0": time.time()}
        self.last_run = run
        self._ax_ping = None        # v21.1: fresh AX probe each run —
        self._ax_off = False        # permission may have been granted since

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

        chunk = max(1, PLAN_CHUNK)
        call_budget = max_steps * 2 + 4      # no-progress guard
        used = 0
        pending = None                       # settled frame for next plan

        while used < max_steps and run["model_calls"] <= call_budget:
            # ---- one capture (reuse last settle when it verifiably landed)
            t = time.time()
            try:
                if pending is not None:
                    self._last_img = pending
                    b64, real_w, real_h, disp_w, disp_h = self._prepare(
                        pending, self.max_width)
                    pending = None
                else:
                    b64, real_w, real_h, disp_w, disp_h = \
                        self.capture_screen()
            except Exception as e:
                run["steps"].append(
                    {"i": used + 1,
                     "capture_ms": (time.time() - t) * 1000})
                return self._timings(run, f"Error: {e}")
            capture_ms = (time.time() - t) * 1000

            # ---- AX bundle (Phase 3; fail-open → coords-only) --------
            ax_list, ax_ms = self._ax_bundle()

            # ---- bounded rolling context -----------------------------
            recent = [x[:HISTORY_CHARS] for x in log[-HISTORY_STEPS:]]
            hist = "; ".join(recent) if recent else "none"
            prompt = self._prompt(str(task), used + 1, max_steps,
                                  hist, ax_list)

            t = time.time()
            try:
                raw = self._ask(prompt, b64)
            except Exception as e:
                run["steps"].append(
                    {"i": used + 1, "capture_ms": capture_ms,
                     "model_ms": (time.time() - t) * 1000})
                return self._timings(
                    run, f"Error talking to vision model: {e}\n"
                         f"Progress so far: {log}")
            model_ms = (time.time() - t) * 1000
            run["model_calls"] += 1

            actions, done = self._parse_plan(raw)
            if actions is None:
                run["steps"].append(
                    {"i": used + 1, "capture_ms": capture_ms,
                     "model_ms": model_ms, "ax_ms": ax_ms})
                return self._timings(
                    run, f"I couldn't understand the vision model's response "
                         f"({raw[:200]}). Stopping.\nProgress: {log}")

            if not actions and done:
                run["steps"].append(
                    {"i": used + 1, "capture_ms": capture_ms,
                     "model_ms": model_ms, "ax_ms": ax_ms,
                     "action": "done"})
                return self._timings(
                    run, f"Task complete: {done}\nActions: {log}")

            # ---- execute up to chunk actions -------------------------
            first = True
            for a in actions[:chunk]:
                if used >= max_steps:
                    break
                step = {"i": used + 1, "action": a.get("type")}
                if first:
                    step["capture_ms"] = capture_ms
                    step["model_ms"] = model_ms
                    step["ax_ms"] = ax_ms
                    first = False

                if a.get("type") == "done":
                    run["steps"].append(step)
                    return self._timings(
                        run, f"Task complete: {a.get('summary', 'done')}\n"
                             f"Actions: {log}")

                # Per-step Jev gate — AFTER EVERY action, chunked or not:
                # abort on high-confidence hazard (credential / delete /
                # spend / outbound send), skip confident off-task drift.
                # Content-bearing steps only: type/key/hotkey (⌘V pastes!)
                # and anything with text — bare pointer ops carry no
                # injectable text (same trust as pre-Phase-2 clicks).
                _is_text_step = (a.get("type") in ("type", "key", "hotkey")
                                 or bool(a.get("text"))
                                 or bool(a.get("key")))
                if jev_ok and _is_text_step:
                    t = time.time()
                    try:
                        verdict = _jev.screen_step(
                            task, a, timeout=JEV_STEP_TIMEOUT_S)
                    except Exception:
                        verdict = None
                    step["gate_ms"] = (time.time() - t) * 1000
                    run["gate_calls"] += 1
                    if verdict is not None:
                        if _jev.step_should_abort(verdict):
                            run["steps"].append(step)
                            return self._timings(
                                run, f"Safety gate (Jev) stopped step "
                                     f"{used + 1}: proposed action flagged "
                                     f"hazardous "
                                     f"(p={verdict.get('hazard'):.2f}).\n"
                                     f"Progress: {log}")
                        if _jev.step_off_task(verdict):
                            run["steps"].append(step)
                            log.append(f"[gate] skipped off-task step "
                                       f"{used + 1} (p_on_task="
                                       f"{verdict.get('on_task'):.2f})")
                            break   # plan is wrong → re-plan NOW

                t = time.time()
                try:
                    result = self._execute(a, real_w, real_h,
                                           disp_w, disp_h)
                    log.append(result)
                except Exception as e:
                    step["execute_ms"] = (time.time() - t) * 1000
                    run["steps"].append(step)
                    return self._timings(
                        run, f"Action failed: {e}\nProgress: {log}")
                step["execute_ms"] = (time.time() - t) * 1000
                used += 1

                # Phase 1 · adaptive settle after EVERY executed action
                settled, settle_ms, saw_change = self._settle(
                    self._last_img)
                step["settle_ms"] = settle_ms
                self._last_img = settled
                pending = settled if (saw_change
                                      and settled is not None) else None
                run["steps"].append(step)

                if chunk <= 1:
                    break       # legacy pacing: one action per model call
            # chunk exhausted (or gate-skipped) → loop re-plans from a
            # fresh/reused frame

        if used >= max_steps:
            return self._timings(
                run, f"Reached the step limit ({max_steps}) without "
                     f"finishing. Progress: {log}")
        return self._timings(
            run, f"Planning made no progress after {run['model_calls']} "
                 f"model calls (every step skipped?). Progress: {log}")
