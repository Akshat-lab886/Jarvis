"""
JARVIS Computer-Use Eval Suite (Phase 0 measurement harness)
=============================================================

Measures the desktop vision loop with numbers, before/after every change.

Two modes
---------
``--probe``  (permission-independent — always runnable)
    Times exactly what Phase 1 touches, on this machine:
      * frame pipeline  — ``DesktopAgent._prepare`` (scale + JPEG) on
        synthetic1920x1080 and 3840x2160 (Retina) frames, so no Screen
        Recording permission is needed;
      * fixed overhead  — reads the loop's constants (POST_ACTION_SLEEP_S,
        pyautogui.PAUSE, CLICK_MOVE_DURATION_S) and computes the exact
        per-click / per-type step cost Phase 1 removes;
      * model latency   — real Gemini call (GOOGLE_API_KEY) with a
        synthetic frame (3 calls). Unchanged by Phase 1, but dominates
        the budget, so it belongs in the table;
      * Jev gate        — real screen_step() call, timed. Fail-open:
        reports "unreachable" when the local gateway is down.

``--live``  (needs Screen Recording + Automation permission for Terminal)
    Deterministic desktop tasks with setup/verify/cleanup. Reports
    pass/fail + wall time + per-phase timing from DesktopAgent.last_run:

      open_calc     activate a neutral app -> open Calculator (frontmost)
      calc_sum      compute 7+8 in Calculator -> display reads 15
      type_marker   type the run nonce into a new TextEdit doc -> text matches
      erase_doc     delete3 short letters -> document empty
      hotkey_gap    press cmd+shift+3 -> KNOWN ACTION-SPACE GAP
                    (the loop has no chord/hotkey op — reported as a gap
                    marker, not scored; it is the motivation for Phase 3)

Exit codes: 0 ok · 1 live task failure · 2 environment blocked (permissions).

Usage:
    ./venv/bin/python -m utils.desktop_eval --probe-only --label baseline
    ./venv/bin/python -m utils.desktop_eval --tasks calc_sum,type_marker
    ./venv/bin/python -m utils.desktop_eval --label after --json /tmp/after.json
"""

import argparse
import json
import os
import platform
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from PIL import Image

from utils.computer_use import _as_quote, _osascript, get_driver


# --------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------- #
def _iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _frontmost():
    """Name of the frontmost process, or None on permission timeout."""
    try:
        proc = subprocess.run(
            ['osascript', '-e',
             'tell application "System Events" to name of first process '
             'whose frontmost is true'],
            capture_output=True, text=True, timeout=8)
        if proc.returncode == 0:
            return (proc.stdout or '').strip()
    except Exception:
        return None
    return None


def _screenshot_capability():
    """(ok, detail) for one cheap capture attempt."""
    try:
        import pyautogui
        img = pyautogui.screenshot()
        return True, f"{img.size[0]}x{img.size[1]}"
    except Exception as e:
        return False, str(e)[:300]


# --------------------------------------------------------------------- #
# TextEdit / Calculator helpers (setup / verify / cleanup)
# --------------------------------------------------------------------- #
NONCE = "JARVIS_EVAL_" + uuid.uuid4().hex[:6].upper()


def _te_new_doc():
    _osascript('tell application "TextEdit" to activate')
    _osascript('tell application "TextEdit"\n  make new document\n'
               '  set text of front document to ""\nend tell')


def _te_keystroke(text):
    _osascript('tell application "System Events" to keystroke '
               f'"{_as_quote(text)}"')


def _te_text():
    ok, out = _osascript('tell application "TextEdit" to text of '
                         'front document')
    return out if ok else None


def _te_close_front():
    return _osascript('tell application "TextEdit" to close '
                      'front document saving no')


def _calc_display_values():
    ok, out = _osascript(
        'tell application "System Events" to tell process "Calculator"\n'
        'set out to ""\n'
        'repeat with w in windows\n'
        '  repeat with e in (every static text of w)\n'
        '    set out to out & (value of e) & "|"\n'
        '  end repeat\n'
        'end repeat\n'
        'return out\n'
        'end tell')
    return [v.strip() for v in out.split('|')] if ok else None


_ST = {"calc_pre_running": False}


def _calc_pre_running():
    ok, out = _osascript('tell application "System Events" to count '
                         'processes whose name is "Calculator"')
    return ok and out.strip() == '1'


# --------------------------------------------------------------------- #
# Task registry
# --------------------------------------------------------------------- #
@dataclass
class Task:
    name: str
    prompt: str
    verify: Callable[[], Tuple[Optional[bool], str]]
    setup: Optional[Callable[[], None]] = None
    cleanup: Optional[Callable[[], None]] = None
    max_steps: int = 8
    capability_gap: bool = False   # known gap: reported, not scored


def build_tasks():
    """Fresh task list per run (nonce is run-scoped)."""
    return [
        Task(
            name="open_calc",
            prompt="Open the Calculator application.",
            setup=lambda: get_driver().activate("TextEdit"),
            verify=lambda: (
                _frontmost() == "Calculator",
                f"frontmost={_frontmost()}"),
            max_steps=8,
        ),
        Task(
            name="calc_sum",
            prompt="Using the Calculator app that is open, compute 7 + 8 "
                   "so the result is showing.",
            setup=lambda: get_driver().activate("Calculator"),
            verify=lambda: _verify_calc15(),
            cleanup=lambda: _calc_cleanup(),
            max_steps=10,
        ),
        Task(
            name="type_marker",
            prompt=f"Type this exact text into the document: {NONCE}",
            setup=lambda: _te_new_doc(),
            verify=lambda: _verify_contains(NONCE),
            cleanup=lambda: _te_close_front(),
            max_steps=6,
        ),
        Task(
            name="erase_doc",
            prompt="Delete all text in the front TextEdit document.",
            setup=lambda: (_te_new_doc(), _te_keystroke("XYZ")),
            verify=lambda: _verify_empty(),
            cleanup=lambda: _te_close_front(),
            max_steps=8,
        ),
        Task(
            name="hotkey_gap",
            prompt="Press cmd+shift+3 to take a screenshot.",
            verify=lambda: (None, "not scored — capability gap marker"),
            capability_gap=True,
            max_steps=4,
        ),
    ]


def _verify_calc15():
    vals = _calc_display_values()
    if vals is None:
        return None, "calculator display unreadable (Automation permission?)"
    hit = any(v == "15" or v.endswith("15") for v in vals)
    return hit, f"display values={vals[:8]}"


def _verify_contains(needle):
    txt = _te_text()
    if txt is None:
        return None, "TextEdit document unreadable (Automation permission?)"
    return needle in txt, f"doc={txt[:80]!r}"


def _verify_empty():
    txt = _te_text()
    if txt is None:
        return None, "TextEdit document unreadable (Automation permission?)"
    return txt.strip() == "", f"doc={txt[:80]!r}"


def _calc_cleanup():
    if not _ST["calc_pre_running"]:
        _osascript('tell application "Calculator" to quit')


# --------------------------------------------------------------------- #
# Probe mode (permission-independent)
# --------------------------------------------------------------------- #
def run_probe(label, gemini_calls=3):
    import pyautogui
    from config import Config
    from utils import desktop_agent as da
    from utils import jev

    out = {
        "label": label, "ts": _iso(),
        "platform": f"{platform.system()} {platform.release()} "
                    f"{platform.machine()}",
        "python": platform.python_version(),
    }

    # --- A) frame pipeline ------------------------------------------- #
    frames = {}
    for w, h in ((1920, 1080), (3840, 2160)):
        img = Image.new("RGB", (w, h), (40, 40, 48))
        times, b64 = [], None
        for _ in range(5):
            t0 = time.perf_counter()
            b64, *_ = da.DesktopAgent._prepare(img, da.FRAME_MAX_WIDTH)
            times.append((time.perf_counter() - t0) * 1000)
        frames[f"{w}x{h}"] = {
            "avg_ms": round(sum(times) / len(times), 1),
            "min_ms": round(min(times), 1),
            "jpeg_kb": max(1, (len(b64) * 3) // 4 // 1024),
        }
    out["frame_prepare"] = frames

    # --- B) fixed per-step overhead (from live constants) ------------ #
    # Instantiate the agent FIRST: pyautogui's module default PAUSE (0.1)
    # differs from what DesktopAgent configures — the report must show
    # what the loop actually pays (baseline PAUSE=0.2, Phase 1 -> 0.0).
    da.DesktopAgent()
    sleep_s = getattr(da, "POST_ACTION_SLEEP_S", 0.0)
    pause_s = float(pyautogui.PAUSE)
    move_s = getattr(da, "CLICK_MOVE_DURATION_S", 0.0)
    per_click = sleep_s + pause_s * 2 + move_s   # moveTo + click API calls
    per_key = sleep_s + pause_s * 1
    adaptive = getattr(da, "SETTLE_MAX_MS", None)
    out["fixed_overhead"] = {
        "post_action_sleep_s": sleep_s,
        "pyautogui_pause_s": pause_s,
        "click_move_duration_s": move_s,
        "per_click_step_s": round(per_click, 2),
        "per_key_step_s": round(per_key, 2),
        "per8step_task_s": round(per_click * 8, 1),
        "settle_mode": ("adaptive" if adaptive else "fixed-sleep"),
        "settle_max_ms": adaptive,
        "adaptive_settle_floor_s": (
            round(getattr(da, "SETTLE_INTERVAL_S", 0.06)
                  * getattr(da, "SETTLE_MIN_STABLE", 2), 2)
            if adaptive else None),
    }

    # --- C) real vision-model call (Gemini) --------------------------- #
    probe_b64 = da.DesktopAgent._prepare(
        Image.new("RGB", (1920, 1080), (40, 40, 48)),
        da.FRAME_MAX_WIDTH)[0]
    if Config.GOOGLE_API_KEY:
        try:
            agent = da.DesktopAgent()
            times, raw = [], None
            for k in range(gemini_calls):
                t0 = time.perf_counter()
                raw = agent._ask(
                    "TASK: probe only. This is screenshot "
                    f"{k + 1} of up to {gemini_calls}. "
                    "Decide the next single action.", probe_b64)
                times.append(round((time.perf_counter() - t0) * 1000))
            out["model"] = {
                "provider": "gemini", "model": Config.GEMINI_MODEL,
                "calls_ms": times,
                "avg_ms": round(sum(times) / len(times)),
                "sample": str(raw)[:100],
            }
        except Exception as e:
            out["model"] = {"provider": "gemini", "error": str(e)[:300]}
    else:
        out["model"] = {"provider": "gemini", "error": "no GOOGLE_API_KEY"}

    # --- D) real Jev gate call (fail-open) ---------------------------- #
    try:
        t0 = time.perf_counter()
        verdict = jev.screen_step(
            "eval probe", {"type": "key", "key": "enter"})
        ms = round((time.perf_counter() - t0) * 1000)
        out["jev"] = {
            "enabled": jev.enabled(), "ms": ms,
            "reachable": verdict is not None,
            "verdict": verdict if verdict else "unreachable (fail-open)",
        }
    except Exception as e:
        out["jev"] = {"enabled": jev.enabled(), "error": str(e)[:200]}

    return out


def _print_probe(p):
    print(f"\n=== PROBE [{p['label']}] {p['ts']}  {p['platform']} ===")
    for size, d in p.get("frame_prepare", {}).items():
        print(f"  frame {size:>9}: prepare avg {d['avg_ms']:>6.1f}ms "
              f"(min {d['min_ms']:.1f}) -> jpeg {d['jpeg_kb']}KB")
    f = p.get("fixed_overhead", {})
    print(f"  fixed overhead [{f.get('settle_mode')}]: "
          f"sleep={f.get('post_action_sleep_s')}s "
          f"PAUSE={f.get('pyautogui_pause_s')}s "
          f"move={f.get('click_move_duration_s')}s")
    print(f"    -> per click step {f.get('per_click_step_s')}s, "
          f"per key step {f.get('per_key_step_s')}s, "
          f"8-step task floor {f.get('per8step_task_task_s', f.get('per8step_task_s'))}s")
    m = p.get("model", {})
    if "error" in m:
        print(f"  model: ERROR {m['error'][:120]}")
    else:
        print(f"  model ({m['provider']}/{m['model']}): "
              f"calls {m['calls_ms']}ms avg {m['avg_ms']}ms")
    j = p.get("jev", {})
    if "error" in j:
        print(f"  jev: ERROR {j['error'][:120]}")
    else:
        print(f"  jev: {j['ms']}ms reachable={j['reachable']} "
              f"enabled={j['enabled']}")


# --------------------------------------------------------------------- #
# Live mode
# --------------------------------------------------------------------- #
def run_live(task_names, max_steps_override, label, out):
    """Run tasks on the real desktop. Returns (exit_code, section)."""
    section = {"label": label, "ts": _iso(), "tasks": [], "blocked": None}

    ok, detail = _screenshot_capability()
    if not ok:
        section["blocked"] = {
            "what": "screen capture",
            "detail": detail,
            "fix": "System Settings > Privacy & Security > Screen "
                   "Recording -> enable for Terminal, then restart the run.",
        }
        return 2, section

    if _frontmost() is None:
        section["blocked"] = {
            "what": "System Events (Automation)",
            "detail": "osascript timed out — permission prompt pending?",
            "fix": "System Settings > Privacy & Security > Automation "
                   "-> allow Terminal to control System Events / "
                   "TextEdit / Calculator.",
        }
        return 2, section

    from utils.desktop_agent import DesktopAgent

    all_tasks = {t.name: t for t in build_tasks()}
    names = task_names or list(all_tasks)
    _ST["calc_pre_running"] = _calc_pre_running()

    agent = DesktopAgent()
    score = {"pass": 0, "fail": 0, "verify_error": 0, "gap": 0}

    for name in names:
        task = all_tasks.get(name)
        if task is None:
            print(f"  ?? unknown task {name!r}")
            continue
        steps = max_steps_override or task.max_steps
        rec = {"name": name, "capability_gap": task.capability_gap}
        t_wall = time.perf_counter()
        try:
            if task.setup:
                try:
                    task.setup()
                except Exception as e:
                    rec["error"] = f"setup failed: {e}"
            if "error" not in rec:
                result = agent.run_task(task.prompt, max_steps=steps)
                rec["agent"] = str(result)[:600]
                rec["timing"] = (agent.last_run or {}).get("summary")
                rec["steps_used"] = len((agent.last_run or {}).get("steps", []))
                try:
                    verdict, vdetail = task.verify()
                    rec["verify"] = vdetail
                    if verdict is None:
                        rec["status"] = "verify_error"
                    else:
                        rec["status"] = "pass" if verdict else "fail"
                except Exception as e:
                    rec["status"] = "verify_error"
                    rec["verify"] = str(e)[:200]
        except Exception as e:
            rec["status"] = "error"
            rec["error"] = str(e)[:300]
        finally:
            if task.cleanup:
                try:
                    task.cleanup()
                except Exception as e:
                    rec["cleanup_error"] = str(e)[:200]
        rec["wall_s"] = round(time.perf_counter() - t_wall, 2)
        status = rec.get("status", "error")
        if task.capability_gap:
            score["gap"] += 1
            print(f"  [GAP]    {name:<12} {rec['wall_s']:>5.1f}s  "
                  f"{rec.get('verify', '')}")
        else:
            score[status if status in score else "fail"] += 1
            mark = {"pass": "PASS", "fail": "FAIL"}.get(status, "VERR")
            print(f"  [{mark:^4}]  {name:<12} {rec['wall_s']:>5.1f}s  "
                  f"{rec.get('timing', '')}  {rec.get('verify', '')}")
        section["tasks"].append(rec)

    scored = score["pass"] + score["fail"] + score["verify_error"]
    section["score"] = {**score, "scored": scored}
    print(f"\n  score: {score['pass']}/{scored} pass"
          f"  (verify_errors={score['verify_error']}, "
          f"gap_markers={score['gap']})")
    return (0 if score["fail"] == 0 and score["verify_error"] == 0 else 1), section


# --------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(prog="desktop_eval",
                                 description="JARVIS computer-use eval suite")
    ap.add_argument("--label", default="run",
                    help="tag for this run (baseline / after / ...)")
    ap.add_argument("--tasks", default="",
                    help="comma list of task names (default: all)")
    ap.add_argument("--max-steps", type=int, default=None,
                    help="override every task's step cap")
    ap.add_argument("--probe-only", action="store_true")
    ap.add_argument("--live-only", action="store_true")
    ap.add_argument("--json", default="/tmp/jarvis_desktop_eval.json",
                    help="report output path")
    args = ap.parse_args(argv)

    report = {"label": args.label, "ts": _iso()}
    code = 0

    if not args.live_only:
        try:
            report["probe"] = run_probe(args.label)
            _print_probe(report["probe"])
        except Exception as e:
            report["probe"] = {"error": str(e)[:400]}
            print(f"  probe ERROR: {e}")
            code = max(code, 1)

    if not args.probe_only:
        names = [n.strip() for n in args.tasks.split(",") if n.strip()] or None
        live_code, section = run_live(names, args.max_steps, args.label,
                                      report)
        report["live"] = section
        if section.get("blocked"):
            b = section["blocked"]
            print(f"\n  LIVE BLOCKED [{b['what']}]: {b['detail']}\n"
                  f"  fix: {b['fix']}")
        code = max(code, live_code)

    with open(args.json, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\nreport -> {args.json}")
    return code


if __name__ == "__main__":
    sys.exit(main())
