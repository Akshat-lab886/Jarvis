"""
J.A.R.V.I.S. — RLM: persistent agent task plan (Hermes parity).

Frontier agents own a visible, durable plan: they decompose a goal
into steps, work them across MANY turns (and restarts), and mark
progress as they go.  This module is that plan store; the agent loop
exposes it through the ``task_plan`` tool and injects the current
state into every system prompt so "continue where we left off" works.

JSON persistence, thread-safe, tiny — a plan is one goal plus an
ordered list of step objects.  ``JARVIS_AGENT_PLAN_STORE=0`` disables
persistence (plans then live only in memory).
"""

import os
import json
import threading
import datetime
import logging

logger = logging.getLogger("Jarvis.RLM.PlanState")

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
DEFAULT_PLAN_FILE = os.path.join(_BASE_DIR, 'brain', 'data',
                                 'agent_plan.json')


def _data_file():
    """Active plan store path (env-overridable for tests/multi-tenant)."""
    return os.getenv('JARVIS_AGENT_PLAN_FILE', '').strip() or DEFAULT_PLAN_FILE

STATUSES = ('pending', 'in_progress', 'done', 'skipped')


def _persist_enabled():
    return os.getenv('JARVIS_AGENT_PLAN_STORE', '1') != '0'


class PlanState:
    """The agent's own cross-session task plan."""

    def __init__(self, file_path=None):
        self.file_path = file_path or _data_file()
        self._lock = threading.RLock()
        self.goal = ""
        self.steps = []           # [{"text", "status", "updated"}]
        self.updated = ""
        self._load()

    # ---------------- persistence ---------------- #

    def _load(self):
        if not _persist_enabled():
            return
        try:
            if os.path.exists(self.file_path):
                with open(self.file_path, 'r') as f:
                    data = json.load(f)
                with self._lock:
                    self.goal = str(data.get('goal', '') or '')
                    steps = data.get('steps') or []
                    self.steps = [
                        {"text": str(s.get('text', ''))[:400],
                         "status": s.get('status', 'pending')
                         if s.get('status') in STATUSES else 'pending',
                         "updated": str(s.get('updated', ''))}
                        for s in steps if isinstance(s, dict)
                    ]
                    self.updated = str(data.get('updated', '') or '')
        except Exception as e:
            logger.warning("plan load failed (starting fresh): %s", e)

    def _save(self):
        if not _persist_enabled():
            return
        try:
            with self._lock:
                data = {"goal": self.goal, "steps": self.steps,
                        "updated": self.updated}
            os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
            with open(self.file_path, 'w') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning("plan save failed: %s", e)

    # ---------------- operations ---------------- #

    def set(self, goal, steps):
        """Replace the plan. Returns the rendered state."""
        goal = str(goal or '').strip()
        steps = [str(s).strip()[:400] for s in (steps or [])
                 if str(s).strip()]
        if not goal or not steps:
            return "ERROR: a plan needs a goal and at least one step."
        now = datetime.datetime.now().isoformat()
        with self._lock:
            self.goal = goal[:500]
            self.steps = [{"text": s, "status": 'pending',
                           "updated": now} for s in steps[:20]]
            self.updated = now
        self._save()
        logger.info("agent plan set: %s (%d steps)", goal[:60], len(steps))
        return self.render()

    def update(self, step_index, status):
        """Mark a step's status by 1-based index. Returns rendered state."""
        try:
            idx = int(step_index) - 1
        except (TypeError, ValueError):
            return "ERROR: step_index must be a number (1-based)."
        if status not in STATUSES:
            return (f"ERROR: status must be one of "
                    f"{', '.join(STATUSES)}.")
        with self._lock:
            if not (0 <= idx < len(self.steps)):
                return (f"ERROR: no step {step_index} — the plan has "
                        f"{len(self.steps)} step(s). Use task_plan get "
                        f"to see them.")
            self.steps[idx]['status'] = status
            self.steps[idx]['updated'] = \
                datetime.datetime.now().isoformat()
            self.updated = self.steps[idx]['updated']
        self._save()
        return self.render()

    def clear(self):
        with self._lock:
            had = bool(self.goal or self.steps)
            self.goal = ""
            self.steps = []
            self.updated = ""
        self._save()
        return "Plan cleared." if had else "No plan to clear."

    def get(self):
        with self._lock:
            if not self.goal:
                return "No active plan."
            return self.render()

    # ---------------- rendering ---------------- #

    def render(self):
        """Compact prompt-ready rendering of the current plan."""
        with self._lock:
            if not self.goal:
                return ""
            lines = [f"ACTIVE TASK PLAN — {self.goal}"]
            for i, s in enumerate(self.steps, 1):
                mark = {'done': '[x]', 'in_progress': '[>]',
                        'skipped': '[-]', 'pending': '[ ]'}.get(
                            s['status'], '[ ]')
                lines.append(f"  {i}. {mark} {s['text']}")
            done = sum(1 for s in self.steps if s['status'] == 'done')
            lines.append(f"  progress: {done}/{len(self.steps)} steps "
                         "complete")
        return "\n".join(lines)

    def stats(self):
        with self._lock:
            return {
                'goal': self.goal,
                'steps': len(self.steps),
                'done': sum(1 for s in self.steps
                            if s['status'] == 'done'),
                'updated': self.updated,
            }


# --------------------------------------------------------------------- #
# Singleton
# --------------------------------------------------------------------- #

_singleton = None
_singleton_lock = threading.Lock()


def get_plan_state(file_path=None):
    """Process-wide PlanState (default file)."""
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = PlanState(file_path=file_path)
        return _singleton


def _reset_singleton():
    """Test helper — drop the singleton so a fresh one is built."""
    global _singleton
    with _singleton_lock:
        _singleton = None
