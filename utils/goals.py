"""
J.A.R.V.I.S. — Goal Engine (autonomy layer)
===========================================

Turns "do X by Friday" into a TRACKED, SELF-DRIVING objective instead of a
forgotten chat line:

    goal_set       "finish the report by Friday" (+ optional deadline,
                   priority, auto-start flag)
    goal_list      open goals with progress + deadline countdown
    goal_done      mark complete (or auto-completes when its tasks finish)
    goal_drop      abandon a goal

How it works
------------
* Goals persist in ``brain/data/goals.json`` (JSON, thread-safe).
* A goal owns background ComplexTaskManager tasks.  When the agent loop
  or the user creates a task FOR a goal, the task is linked; when every
  linked task completes, the goal auto-completes and announces itself.
* Deadline watchdog: a daemon tick escalates goals due within
  ``JARVIS_GOALS_ESCALATE_H`` (default 24h) and overdue goals — once per
  goal per arming — through the notifier (dashboard feed + optional
  voice/Telegram, see ``utils/notify.py``).
* ``JARVIS_GOALS=0`` disables everything (fail-soft no-ops).
* ``JARVIS_GOALS_AUTOSTART=1`` lets a goal spawn its own planned task
  via the planner persona WITHOUT the user saying "start"; default 0
  (suggest-only) keeps full autonomy opt-in.

Design constraints (Jarvis house style):
* stdlib only, bounded store, never raises into the caller
* destructive actions still pass through executor approvals — goals
  NEVER bypass the HITL checkpoint
* all LLM calls go through brain.complete (budget + breaker guarded)
"""

import os
import re
import json
import time
import uuid
import threading
import logging
import datetime

logger = logging.getLogger("Jarvis.Goals")

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_GOALS_FILE = os.path.join(_BASE_DIR, 'brain', 'data', 'goals.json')

STATUSES = ('open', 'in_progress', 'done', 'dropped', 'overdue')

# "by Friday", "due tomorrow", "by 2026-09-30", "in 3 days", "EOD Friday"
_DEADLINE_RE = re.compile(
    r'\b(?:by|due|before|deadline:?)\s+'
    r'(monday|tuesday|wednesday|thursday|friday|saturday|sunday|'
    r'today|tomorrow|tonight|eod|end\s+of\s+day|'
    r'\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?|'
    r'in\s+\d+\s+(?:days?|hours?|weeks?))',
    re.IGNORECASE)

_IN_DAYS_RE = re.compile(r'in\s+(\d+)\s+(days?|hours?|weeks?)', re.IGNORECASE)

_WEEKDAYS = {
    'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3,
    'friday': 4, 'saturday': 5, 'sunday': 6,
}


def enabled():
    return os.getenv('JARVIS_GOALS', '1') != '0'


def autostart_enabled():
    """JARVIS_GOALS_AUTOSTART=1 lets goals spawn their own tasks."""
    return os.getenv('JARVIS_GOALS_AUTOSTART', '0') == '1'


def _escalate_hours():
    try:
        return max(1.0, float(os.getenv('JARVIS_GOALS_ESCALATE_H', '24')))
    except (TypeError, ValueError):
        return 24.0


def _watchdog_tick_s():
    try:
        return max(60.0, float(os.getenv('JARVIS_GOALS_TICK_S', '600')))
    except (TypeError, ValueError):
        return 600.0


def parse_deadline(text, now=None):
    """
    Best-effort deadline extraction from free text.
    Returns an ISO datetime string or '' (never raises).
    """
    try:
        now = now or datetime.datetime.now()
        m = _DEADLINE_RE.search(str(text or ''))
        if not m:
            return ''
        frag = m.group(0).lower()

        in_m = _IN_DAYS_RE.search(frag)
        if in_m:
            n = int(in_m.group(1))
            unit = in_m.group(2).lower()
            if unit.startswith('hour'):
                return (now + datetime.timedelta(hours=n)).isoformat()
            if unit.startswith('week'):
                return (now + datetime.timedelta(weeks=n)).isoformat()
            return (now + datetime.timedelta(days=n)).isoformat()

        date_m = re.search(r'(\d{4})-(\d{1,2})-(\d{1,2})', frag)
        if date_m:
            return datetime.datetime(
                int(date_m.group(1)), int(date_m.group(2)),
                int(date_m.group(3)), 18, 0).isoformat()
        slash_m = re.search(r'(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?', frag)
        if slash_m:
            yy = slash_m.group(3)
            year = now.year if not yy else (
                int(yy) if len(yy) == 4 else 2000 + int(yy))
            return datetime.datetime(
                year, int(slash_m.group(1)),
                int(slash_m.group(2)), 18, 0).isoformat()

        if 'tomorrow' in frag or 'eod' in frag or 'end of day' in frag:
            base = now + datetime.timedelta(days=1)
            return base.replace(hour=18, minute=0,
                                second=0, microsecond=0).isoformat()
        if 'today' in frag or 'tonight' in frag:
            return now.replace(hour=23, minute=0,
                               second=0, microsecond=0).isoformat()

        for name, idx in _WEEKDAYS.items():
            if name in frag:
                days_ahead = (idx - now.weekday()) % 7 or 7
                base = (now + datetime.timedelta(days=days_ahead))
                return base.replace(hour=18, minute=0,
                                    second=0, microsecond=0).isoformat()
        return ''
    except Exception:
        return ''


def _parse_iso(s):
    try:
        return datetime.datetime.fromisoformat(str(s))
    except Exception:
        return None


def _countdown(deadline_iso, now=None):
    """Human countdown ('in 2d 3h', 'overdue by 5h', 'no deadline')."""
    try:
        now = now or datetime.datetime.now()
        dt = _parse_iso(deadline_iso)
        if dt is None:
            return 'no deadline'
        delta = dt - now
        secs = int(delta.total_seconds())
        if secs < 0:
            over = -secs
            if over < 3600:
                return f"overdue by {over // 60}m"
            if over < 86400:
                return f"overdue by {over // 3600}h"
            return f"overdue by {over // 86400}d"
        if secs < 3600:
            return f"in {max(1, secs // 60)}m"
        if secs < 86400:
            return f"in {secs // 3600}h"
        return f"in {secs // 86400}d"
    except Exception:
        return ''


class GoalStore:
    """Thread-safe JSON goal store.  Fire-and-forget writes."""

    def __init__(self, file_path=None):
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        default = os.path.join(base, 'brain', 'data', 'goals.json')
        override = os.getenv('JARVIS_GOALS_FILE', '').strip()
        self.file_path = file_path or override or default
        self._lock = threading.RLock()
        self._goals = []
        self._load()

    # ---------------- persistence ---------------- #

    def _load(self):
        try:
            if os.path.exists(self.file_path):
                with open(self.file_path, 'r') as f:
                    data = json.load(f)
                goals = data.get('goals', data) if isinstance(
                    data, dict) else data
                self._goals = [g for g in (goals or [])
                               if isinstance(g, dict) and g.get('title')]
        except Exception as e:
            logger.warning("goals load failed (starting fresh): %s", e)
            self._goals = []

    def _save(self):
        try:
            with self._lock:
                data = {"goals": self._goals}
            os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
            with open(self.file_path, 'w') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning("goals save failed: %s", e)

    # ---------------- CRUD ---------------- #

    def set(self, title, deadline='', priority='normal'):
        """Create a goal.  Returns the goal dict (or None when off)."""
        if not enabled():
            return None
        title = str(title or '').strip()[:300]
        if not title:
            return None
        if not deadline:
            deadline = parse_deadline(title)
        priority = str(priority or 'normal').lower()
        if priority not in ('low', 'normal', 'high'):
            priority = 'normal'
        now = datetime.datetime.now().isoformat()
        goal = {
            'id': uuid.uuid4().hex[:8],
            'title': title,
            'deadline': deadline or '',
            'priority': priority,
            'status': 'open',
            'progress': 0,
            'task_ids': [],
            'created': now,
            'updated': now,
            'escalated': '',      # last escalation marker sent
            'announced': False,   # completion announced
        }
        with self._lock:
            self._goals.append(goal)
            if len(self._goals) > 50:      # bounded store
                self._goals = self._goals[-50:]
        self._save()
        logger.info("goal set: %s (due %s)", title[:60],
                    deadline or 'no deadline')
        return dict(goal)

    def get(self, goal_id):
        with self._lock:
            for g in self._goals:
                if g.get('id') == goal_id:
                    return dict(g)
        return None

    def list(self, include_done=False):
        with self._lock:
            goals = [dict(g) for g in self._goals
                     if include_done or g.get('status') in
                     ('open', 'in_progress', 'overdue')]
        goals.sort(key=lambda g: (g.get('deadline') or '~~~~',
                                  g.get('created', '')))
        return goals

    def link_task(self, goal_id, task_id):
        """Attach a background task id to a goal."""
        with self._lock:
            for g in self._goals:
                if g.get('id') == goal_id:
                    if task_id not in g.setdefault('task_ids', []):
                        g['task_ids'].append(task_id)
                    if g.get('status') == 'open':
                        g['status'] = 'in_progress'
                    g['updated'] = datetime.datetime.now().isoformat()
                    self._save()
                    return True
        return False

    def update_progress(self, goal_id, progress):
        try:
            progress = max(0, min(100, int(progress)))
        except (TypeError, ValueError):
            return False
        with self._lock:
            for g in self._goals:
                if g.get('id') == goal_id:
                    g['progress'] = progress
                    g['updated'] = datetime.datetime.now().isoformat()
                    self._save()
                    return True
        return False

    def complete(self, goal_id):
        with self._lock:
            for g in self._goals:
                if g.get('id') == goal_id:
                    g['status'] = 'done'
                    g['progress'] = 100
                    g['updated'] = datetime.datetime.now().isoformat()
                    self._save()
                    return dict(g)
        return None

    def drop(self, goal_id):
        with self._lock:
            for g in self._goals:
                if g.get('id') == goal_id:
                    g['status'] = 'dropped'
                    g['updated'] = datetime.datetime.now().isoformat()
                    self._save()
                    return True
        return False

    def render(self, include_done=False):
        """Compact human-readable goal list."""
        goals = self.list(include_done=include_done)
        if not goals:
            return "No open goals. Say 'my goal is to … by Friday'."
        lines = ["OPEN GOALS:"]
        for g in goals:
            cd = _countdown(g.get('deadline', ''))
            bar = f"{g.get('progress', 0)}%"
            tasks = len(g.get('task_ids', []))
            flag = ""
            if g.get('status') == 'overdue':
                flag = " ⏰ OVERDUE"
            elif g.get('priority') == 'high':
                flag = " 🔥"
            lines.append(
                f"- [{g['id']}] {g['title']} — {bar}, "
                f"{cd}"
                + (f", {tasks} task(s)" if tasks else "")
                + flag)
        return "\n".join(lines)

    def stats(self):
        with self._lock:
            open_g = [g for g in self._goals
                      if g.get('status') in
                      ('open', 'in_progress', 'overdue')]
            return {
                'open': len(open_g),
                'done': sum(1 for g in self._goals
                            if g.get('status') == 'done'),
                'overdue': sum(1 for g in self._goals
                               if g.get('status') == 'overdue'),
                'total': len(self._goals),
            }


# --------------------------------------------------------------------- #
# Singleton
# --------------------------------------------------------------------- #

_singleton = None
_singleton_lock = threading.Lock()


def get_goals(file_path=None):
    """Process-wide GoalStore."""
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = GoalStore(file_path=file_path)
        return _singleton


def _reset_singleton():
    """Test helper."""
    global _singleton
    with _singleton_lock:
        _singleton = None


# --------------------------------------------------------------------- #
# Deadline watchdog — escalation pass over open goals
# --------------------------------------------------------------------- #

def watchdog_tick(store=None, brain=None, notifier=None):
    """
    One escalation pass.  For each open goal due within the escalation
    window (or already overdue) that hasn't been escalated for this
    state, mark it and notify.  Returns the list of escalated goals.
    Never raises; LLM-free (pure clock math) unless autostart fires.
    """
    if not enabled():
        return []
    store = store or get_goals()
    out = []
    try:
        now = datetime.datetime.now()
        window_h = _escalate_hours()
        for g in store.list():
            dl = _parse_iso(g.get('deadline', ''))
            if dl is None:
                continue
            hours_left = (dl - now).total_seconds() / 3600.0
            if hours_left <= 0:
                marker = 'overdue'
                with store._lock:
                    for _g in store._goals:
                        if _g.get('id') == g.get('id'):
                            _g['status'] = 'overdue'
                            _g['updated'] = now.isoformat()
                store._save()
            elif hours_left <= window_h:
                marker = 'due-soon'
            else:
                continue
            if g.get('escalated') == marker:
                continue          # already announced for this state
            with store._lock:
                for _g in store._goals:
                    if _g.get('id') == g.get('id'):
                        _g['escalated'] = marker
                        _g['updated'] = now.isoformat()
            store._save()
            freshened = store.get(g['id']) or g
            out.append(freshened)
            if notifier is not None:
                try:
                    if marker == 'overdue':
                        notifier.announce(
                            f"⏰ Goal overdue: {g['title']}",
                            priority='high')
                    else:
                        notifier.announce(
                            f"⏳ Goal due {_countdown(g.get('deadline', ''))}: "
                            f"{g['title']}",
                            priority='normal')
                except Exception:
                    pass
        # Autostart: open goals with no tasks get a planned task spawned
        # from the planner persona (opt-in via JARVIS_GOALS_AUTOSTART=1).
        if autostart_enabled() and brain is not None:
            for g in store.list():
                if g.get('task_ids'):
                    continue
                try:
                    _autostart_goal(store, brain, g, notifier)
                except Exception as e:
                    logger.debug("goal autostart skipped (%s): %s",
                                 g.get('id'), e)
    except Exception as e:
        logger.debug("goals watchdog tick failed: %s", e)
    return out


def _autostart_goal(store, brain, goal, notifier=None):
    """Plan a goal into steps and launch it as a background task."""
    from utils.agents import get_profile  # noqa: F401 (validates registry)
    raw = None
    try:
        raw = brain.complete(
            f"Break this goal into 2-5 concrete, self-contained steps.\n\n"
            f"GOAL: {goal.get('title', '')[:300]}\n\n"
            'Output ONLY raw JSON: {"steps": [{"text": "<step>"}]}',
            agent='planner', timeout=45, max_tokens=600)
    except Exception as e:
        logger.debug("goal autostart plan failed: %s", e)
        return None
    specs = _extract_steps(raw)
    if not specs:
        return None
    try:
        tm = _task_manager_for(brain)
        if tm is None:
            return None
        task = tm.create_task(f"[Goal] {goal.get('title', '')}", specs)
        ok, _msg = tm.start_task(task.id)
        if ok:
            store.link_task(goal['id'], task.id)
            if notifier is not None:
                try:
                    notifier.announce(
                        f"🚀 Auto-started goal '{goal.get('title', '')[:60]}' "
                        f"({len(specs)} steps, background task {task.id}).",
                        priority='low')
                except Exception:
                    pass
            return task.id
    except Exception as e:
        logger.debug("goal autostart launch failed: %s", e)
    return None


def _extract_steps(raw):
    """Best-effort step list from planner JSON (bounded, never raises)."""
    try:
        if not raw:
            return []
        m = re.search(r'\{.*\}', str(raw), re.DOTALL)
        data = json.loads(m.group(0)) if m else None
        items = data.get('steps') if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []
        out = []
        for it in items[:5]:
            text = it.get('text') if isinstance(it, dict) else it
            text = str(text or '').strip()
            if text:
                out.append(text[:300])
        return out
    except Exception:
        return []


def _task_manager_for(brain):
    """ComplexTaskManager bound to *brain* (executor-local, lazy)."""
    try:
        from utils.server import executor as _server_executor
        _server_executor.task_manager.brain = brain
        return _server_executor.task_manager
    except Exception:
        pass
    try:
        from utils.executor import JarvisExecutor
        ex = JarvisExecutor()
        ex.task_manager.brain = brain
        return ex.task_manager
    except Exception as e:
        logger.debug("no task manager available: %s", e)
        return None


def check_goal_completion(store=None, task_manager=None, brain=None,
                          notifier=None):
    """
    Link check: any goal whose linked tasks are ALL terminal-complete
    auto-completes (with announcement).  Call after task finishes or on
    the watchdog tick.  Returns completed goal ids.  Never raises.
    """
    if not enabled():
        return []
    done_ids = []
    try:
        store = store or get_goals()
        if task_manager is None:
            tm = _task_manager_for(brain) if brain is not None else None
            if tm is None:
                return []
            task_manager = tm
        for g in store.list():
            tids = g.get('task_ids') or []
            if not tids:
                continue
            states = []
            for tid in tids:
                try:
                    t = task_manager.get_task(tid)
                    states.append(getattr(t, 'status', None)
                                  and str(t.status.value
                                          if hasattr(t.status, 'value')
                                          else t.status))
                except Exception:
                    states.append(None)
            if states and all(s == 'completed' for s in states):
                store.complete(g['id'])
                done_ids.append(g['id'])
                if notifier is not None:
                    try:
                        notifier.announce(
                            f"✅ Goal complete: {g['title']}",
                            priority='normal')
                    except Exception:
                        pass
    except Exception as e:
        logger.debug("goal completion check failed: %s", e)
    return done_ids


# --------------------------------------------------------------------- #
# Watchdog daemon
# --------------------------------------------------------------------- #

_watch_thread = None
_watch_stop = threading.Event()


def start_watchdog(brain=None, notifier=None):
    """Start the periodic deadline watchdog (idempotent, daemon)."""
    global _watch_thread
    if _watch_thread is not None and _watch_thread.is_alive():
        return False
    if not enabled():
        return False
    _watch_stop.clear()

    def _loop():
        # Lazy import: hibernation may not exist in bare test envs.
        while not _watch_stop.wait(timeout=_watchdog_tick_s()):
            try:
                from utils.hibernation import get_manager
                if getattr(get_manager(), 'hibernating', False):
                    continue
            except Exception:
                pass
            try:
                from utils.notify import get_notifier
                nb = notifier or get_notifier()
                watchdog_tick(brain=brain, notifier=nb)
                check_goal_completion(brain=brain, notifier=nb)
            except Exception as e:
                logger.debug("goals watchdog loop failed: %s", e)

    _watch_thread = threading.Thread(target=_loop, daemon=True,
                                     name="goals-watchdog")
    _watch_thread.start()
    logger.info("Goals watchdog online (%.0fs cadence)",
                _watchdog_tick_s())
    return True


def stop_watchdog():
    _watch_stop.set()
