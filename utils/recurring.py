"""
Jarvis Recurring Automations
============================

Cron-style background jobs expressed in natural language:

    "every day at 9am give me my morning briefing"
    "every weekday at 18:30 check my calendar"
    "every monday at 8am summarize my emails"

Unlike one-shot reminders, automations PERSIST across restarts (jobs.json)
and fire an arbitrary Jarvis *action* through a callback wired by the
server — usually brain.think() + executor.execute_command().

Each job fires at most once per matching day (last_fired marker).
"""

import os
import re
import json
import time
import threading
import logging
import datetime

logger = logging.getLogger("Jarvis.Recurring")

WEEKDAY_NAMES = {
    'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3,
    'friday': 4, 'saturday': 5, 'sunday': 6,
}

# every (day|weekday|<name>) at HH[:MM] [am|pm] <action>
_JOB_RE = re.compile(
    r'\bevery\s+(day|daily|weekday|weekdays|'
    r'mondays?|tuesdays?|wednesdays?|thursdays?|fridays?|saturdays?|sundays?)'
    r'\s+(?:at\s+)?'
    r'(\d{1,2})(?::(\d{1,2}))?\s*(am|pm|a\.m\.|p\.m\.)?\b'
    r'\s*,?\s*(.+)$',
    re.IGNORECASE,
)

# every N minutes|hours <action>  — interval schedules (backups,
# polling, digest loops) that fire many times per day.
_JOB_INTERVAL_RE = re.compile(
    r'\bevery\s+(\d{1,4})\s*(minutes?|mins?|hours?|hrs?)\b'
    r'[\s,]*(.*)$',
    re.IGNORECASE,
)


class RecurringAutomations:
    """Persistent cron-like automation store with a polling loop."""

    def __init__(self, file_path=None, mouth=None, poll_interval=None):
        self.base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.file_path = file_path or os.path.join(self.base_dir, 'jobs.json')
        self.mouth = mouth
        self.poll_interval = poll_interval or 20  # seconds
        self.on_fire = None   # fn(job) -> result text (wired by server)
        self.max_log_lines = 5

        self._lock = threading.Lock()
        self._jobs = []
        self._next_id = 1
        self._running = False
        self._thread = None

        # Automations are intentional long-lived rules — restore them.
        self._load()

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    def _load(self):
        try:
            if os.path.exists(self.file_path):
                with open(self.file_path, 'r') as f:
                    data = json.load(f)
                self._jobs = data.get('jobs', [])
                self._next_id = data.get('next_id', len(self._jobs) + 1)
                if self._jobs:
                    logger.info(f"Restored {len(self._jobs)} recurring "
                                f"automation(s)")
        except Exception as e:
            logger.error(f"Failed to load automations: {e}")
            self._jobs = []
            self._next_id = 1

    def _save(self):
        try:
            with self._lock:
                data = {"next_id": self._next_id, "jobs": self._jobs}
            with open(self.file_path, 'w') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save automations: {e}")

    # ------------------------------------------------------------------ #
    # Parsing
    # ------------------------------------------------------------------ #
    @staticmethod
    def _normalize_time(hour, minute, modifier):
        hour = int(hour)
        minute = int(minute or 0)
        mod = (modifier or '').lower().replace('.', '')
        if mod.startswith('p') and hour < 12:
            hour += 12
        elif mod.startswith('a') and hour == 12:
            hour = 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
        return None

    @staticmethod
    def _resolve_days(token):
        t = token.lower().rstrip('s')
        if t in ('day', 'daily'):
            return 'daily'
        if t == 'weekday':
            return 'weekdays'
        for name, idx in WEEKDAY_NAMES.items():
            if t.startswith(name[:3]) and name.startswith(t[:3]):
                return [idx]
        return None

    def parse(self, text):
        """
        Parse automation text into a job dict.
        Returns (job_dict, None) or (None, error_message).
        """
        text = str(text or '')

        # Interval schedules first ("every 30 minutes back up the db")
        m = _JOB_INTERVAL_RE.search(text)
        if m:
            n = int(m.group(1))
            unit = m.group(2).lower()
            action = m.group(3).strip().rstrip('.')
            if n < 1:
                return None, "The interval must be at least 1."
            if not action:
                return None, ("What should the automation do? e.g. "
                              "'every 30 minutes back up the database'.")
            seconds = n * 3600 if unit.startswith(('h', 'hr')) else n * 60
            return ({
                "id": None,
                "schedule": "interval",
                "interval_s": seconds,
                "hour": None, "minute": None,
                "action": action,
                "created": datetime.datetime.now().isoformat(),
                "last_fired": None,
                "last_fired_ts": 0.0,
            }, None)

        m = _JOB_RE.search(text)
        if not m:
            return None, (
                "I couldn't parse that schedule. Try: 'every day at 9am "
                "give me my morning briefing', 'every weekday at 18:30 "
                "check my email', or 'every 30 minutes back up the "
                "database'."
            )

        days = self._resolve_days(m.group(1))
        # A lone ":" right after the hour (no minutes) previously leaked
        # into the action while the two-digit-minute regex silently
        # scheduled HH:00 — e.g. "every day at 9: do yoga" became 9:00 AM
        # running ": do yoga".  Reject it with guidance instead.
        if m.group(3) is None and m.group(4) is None \
                and text[m.end(2):].lstrip().startswith(':'):
            return None, ("That time is missing the minutes after the "
                          "colon — e.g. 'every day at 9:30pm do the "
                          "thing'.")
        hhmm = self._normalize_time(m.group(2), m.group(3), m.group(4))
        if days is None or hhmm is None:
            return None, ("That time or day didn't make sense "
                          "(e.g. 'every friday at 9am').")

        action = m.group(5).strip().rstrip('.').lstrip(':').strip()
        if not action:
            return None, ("What should the automation do? e.g. "
                          "'every day at 9am give me my morning briefing'.")

        job = {
            "id": None,                      # assigned on add()
            "schedule": days,
            "hour": hhmm[0],
            "minute": hhmm[1],
            "action": action,
            "created": datetime.datetime.now().isoformat(),
            "last_fired": None,              # date string of last fire
        }
        return job, None

    # ------------------------------------------------------------------ #
    # CRUD
    # ------------------------------------------------------------------ #
    def add(self, text):
        job, err = self.parse(text)
        if err:
            return err
        with self._lock:
            job['id'] = self._next_id
            self._next_id += 1
            self._jobs.append(job)
        self._save()
        self.start()   # ensure loop running
        sched = self._describe(job)
        return (f"Automation #{job['id']} scheduled: {sched} — "
                f"I will: {job['action']}.")

    def remove(self, keyword):
        kw = str(keyword or '').lower().strip()
        if not kw:
            return "Which automation should I cancel?"
        with self._lock:
            matched = [j for j in self._jobs
                       if kw in j['action'].lower() or str(j['id']) == kw]
            if matched:
                self._jobs = [j for j in self._jobs if j not in matched]
                names = ", ".join(f"#{j['id']}" for j in matched)
            else:
                names = None
        if names:
            self._save()
            return f"Cancelled automation(s): {names}."
        return f"No automation matches '{keyword}'."

    def list_jobs(self):
        with self._lock:
            jobs = list(self._jobs)
        if not jobs:
            return ("You have no recurring automations. Try: 'every day "
                    "at 9am give me my morning briefing'.")
        lines = ["Recurring automations:"]
        for j in sorted(jobs, key=lambda x: x['id']):
            fired = f" (last: {j['last_fired']})" if j.get('last_fired') else ""
            lines.append(f"#{j['id']}: {self._describe(j)} — do: "
                         f"{j['action']}{fired}")
        return "\n".join(lines)

    def count(self):
        with self._lock:
            return len(self._jobs)

    @staticmethod
    def _describe(job):
        sched = job['schedule']
        if sched == 'interval':
            secs = int(job.get('interval_s') or 0)
            if secs >= 3600 and secs % 3600 == 0:
                return f"every {secs // 3600}h"
            return f"every {max(1, secs // 60)}m"
        hour12 = job['hour'] % 12 or 12
        ampm = 'AM' if job['hour'] < 12 else 'PM'
        if sched == 'daily':
            day_txt = "every day"
        elif sched == 'weekdays':
            day_txt = "every weekday"
        else:
            names = [n for n, i in WEEKDAY_NAMES.items()
                     if i in sched]
            day_txt = "every " + "/".join(n.capitalize() for n in names)
        return f"{day_txt} at {hour12}:{job['minute']:02d} {ampm}"

    # ------------------------------------------------------------------ #
    # Maturity: run-now, next-fire preview, run log
    # ------------------------------------------------------------------ #
    _FIRE_LOG_MAX = 3

    def run_now(self, keyword):
        """Immediately fire the matching job (bypasses schedule)."""
        kw = str(keyword or '').lower().strip()
        job = None
        with self._lock:
            for j in self._jobs:
                if kw in j['action'].lower() or str(j['id']) == kw:
                    job = j
                    break
        if not job:
            return f"No automation matches '{keyword}'."
        self._fire(job)
        return f"Automation #{job['id']} triggered."

    def _next_fire(self, job, now=None):
        """Datetime of this job's NEXT fire after *now*."""
        now = now or datetime.datetime.now()
        sched = job['schedule']
        if sched == 'interval':
            interval = int(job.get('interval_s') or 60)
            last_ts = float(job.get('last_fired_ts') or 0)
            if last_ts <= 0:
                last_ts = now.timestamp()
            return datetime.datetime.fromtimestamp(last_ts + interval)
        candidate = now.replace(hour=job['hour'], minute=job['minute'],
                                second=0, microsecond=0)

        def _day_ok(dt):
            if sched == 'daily':
                return True
            if sched == 'weekdays':
                return dt.weekday() < 5
            return dt.weekday() in sched

        for _ in range(9):   # bounded search across days
            already = (candidate.date() == now.date()
                       and job.get('last_fired') == now.strftime('%Y-%m-%d'))
            if candidate > now and _day_ok(candidate) and not already:
                return candidate
            candidate += datetime.timedelta(days=1)
        return None

    def describe_next(self, job):
        nxt = self._next_fire(job)
        if not nxt:
            return ''
        delta = nxt - datetime.datetime.now()
        hrs = int(delta.total_seconds() // 3600)
        mins = int(delta.total_seconds() % 3600 // 60)
        when = f"in {hrs}h {mins}m" if hrs else f"in {mins}m"
        return f"{nxt.strftime('%a %H:%M')} ({when})"

    # ------------------------------------------------------------------ #
    # Firing logic
    # ------------------------------------------------------------------ #
    def _is_due(self, job, now):
        sched = job['schedule']
        if sched == 'interval':
            interval = int(job.get('interval_s') or 60)
            last_ts = float(job.get('last_fired_ts') or 0)
            if last_ts <= 0:
                # First poll after a restart: arm the clock instead of
                # firing immediately.
                job['last_fired_ts'] = now.timestamp()
                return False
            return (now.timestamp() - last_ts) >= interval
        if sched == 'daily':
            day_ok = True
        elif sched == 'weekdays':
            day_ok = now.weekday() < 5
        else:
            day_ok = now.weekday() in sched
        if not day_ok:
            return False
        today = now.strftime('%Y-%m-%d')
        if job.get('last_fired') == today:
            return False
        return (now.hour, now.minute) >= (job['hour'], job['minute'])

    def _fire(self, job, now=None):
        # Use the SAME logical time as the due-check so tests (and any
        # clock drift between check & fire) stay consistent.
        now = now or datetime.datetime.now()
        today = now.strftime('%Y-%m-%d')
        # Mark FIRST so a slow callback can't double-fire on the next poll
        with self._lock:
            job['last_fired'] = today
            if job.get('schedule') == 'interval':
                job['last_fired_ts'] = now.timestamp()
        self._save()

        logger.info(f"Firing automation #{job['id']}: {job['action'][:60]}")
        result_text = None
        if self.on_fire is not None:
            try:
                result_text = self.on_fire(dict(job))
            except Exception as e:
                logger.error(f"Automation #{job['id']} callback failed: {e}")

        # Run log (last N outcomes, shown on dashboard)
        ok = not str(result_text or '').lower().startswith(
            ('processing failed', "i'm having trouble"))
        with self._lock:
            job.setdefault('history', [])
            job['history'].insert(0, {'at': today, 'ok': ok})
            del job['history'][self._FIRE_LOG_MAX:]

        # Chained follow-ups (full autonomy): when the result carries a
        # verdict the brain flagged for follow-up, spawn ONE bounded
        # background task from it — e.g. "build failed" → "investigate
        # the failure".  The brain opts in per-run (server tags the
        # command); this method stays inert otherwise, so legacy tests
        # and plain schedules never spawn surprise work.
        try:
            self._maybe_followup(job, result_text)
        except Exception as e:
            logger.debug(f"automation follow-up skipped: {e}")

        msg = f"🤖 Automation #{job['id']} ran: {job['action']}"
        try:
            from utils.server import send_to_ui
            send_to_ui('automation_fired', {
                'id': job['id'],
                'action': job['action'],
                'schedule': self._describe(job),
                'result': str(result_text)[:800] if result_text else '',
            })
            send_to_ui('ai_text', {
                'text': (msg + (f"\n\n{str(result_text)[:800]}"
                                if result_text else ""))})
        except Exception:
            pass
        # Voice for automations is opt-in (JARVIS_NOTIFY_VOICE=1):
        # unprompted speech is startling, and the notifier already
        # applies priority + rate limits.  The direct mouth call below
        # stays as the legacy fallback ONLY when the notifier is off.
        try:
            from utils.notify import get_notifier, enabled as _nb_on
            if _nb_on():
                get_notifier().announce(msg, priority='low')
                _spoken_via_notifier = True
            else:
                _spoken_via_notifier = False
        except Exception:
            _spoken_via_notifier = False
        if self.mouth is not None and result_text \
                and not _spoken_via_notifier:
            try:
                # Speak only a compact spoken summary, not raw payloads
                spoken = str(result_text).strip().splitlines()[0][:220]
                self.mouth.speak(spoken)
            except Exception as e:
                logger.error(f"Automation speak failed: {e}")

    def _maybe_followup(self, job, result_text):
        """
        Spawn at most ONE follow-up background task from an automation
        result.  Gated on ALL of: the server tagged this run with
        ``_allow_followup``, the result text carries a
        ``FOLLOWUP: <instruction>`` verdict line, and no follow-up was
        spawned for this job today.  Bounded and inert by default.
        """
        text = str(result_text or '')
        if 'FOLLOWUP:' not in text:
            return None
        if not job.get('_allow_followup'):
            return None
        today = datetime.datetime.now().strftime('%Y-%m-%d')
        if job.get('_followup_day') == today:
            return None
        line = text.split('FOLLOWUP:', 1)[1].strip().splitlines()[0][:300]
        if len(line) < 8:
            return None
        try:
            from utils.server import executor as _server_executor
            tm = _server_executor.task_manager
        except Exception:
            return None
        task = tm.create_task(f"[Follow-up] {line}", [line])
        ok, _msg = tm.start_task(task.id)
        if ok:
            with self._lock:
                job['_followup_day'] = today
            self._save()
            logger.info("automation #%s follow-up task %s: %s",
                        job.get('id'), task.id, line[:80])
            return task.id
        return None

    def _check_due(self, now=None):
        now = now or datetime.datetime.now()
        due = []
        with self._lock:
            for job in self._jobs:
                try:
                    if self._is_due(job, now):
                        due.append(job)
                except Exception as e:
                    logger.error(f"Sched check failed for job: {e}")
        for job in due:
            self._fire(job, now=now)
        return len(due)

    # ------------------------------------------------------------------ #
    # Loop
    # ------------------------------------------------------------------ #
    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="RecurringAutomations")
        self._thread.start()
        logger.info("Recurring automation loop started.")

    def stop(self):
        self._running = False

    def _loop(self):
        while self._running:
            try:
                self._check_due()
            except Exception as e:
                logger.error(f"Automation loop error: {e}")
            time.sleep(self.poll_interval)


if __name__ == "__main__":
    # Self-test with temp storage
    import tempfile
    path = os.path.join(tempfile.gettempdir(), 'jarvis_recurring_selftest.json')
    if os.path.exists(path):
        os.remove(path)
    ra = RecurringAutomations(file_path=path, poll_interval=999)
    print(ra.add("every day at 9am give me my morning briefing"))
    print(ra.add("every weekday at 18:30 check my calendar"))
    print(ra.add("every friday at 8pm order pizza"))
    print(ra.list_jobs())

    # Due-logic probes
    probes = [
        (datetime.datetime(2026, 8, 21, 8, 59), 0),   # Friday, before 9am
        (datetime.datetime(2026, 8, 21, 9, 0), 1),    # Friday 9:00 sharp-ish
        (datetime.datetime(2026, 8, 22, 9, 0), 1),    # Saturday: weekday-job off
        (datetime.datetime(2026, 8, 28, 20, 0), 1),   # Friday 8pm pizza
    ]
    for when, expected in probes:
        got = ra._check_due(when)
        status = "OK " if got == expected else "FAIL"
        print(f"[{status}] {when} -> fired {got} (expected {expected})")
    print(ra.remove("pizza"))
    ra.stop()
