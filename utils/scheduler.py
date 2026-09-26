"""
Jarvis Reminder Scheduler

Lets Jarvis understand natural-language reminders ("remind me in 20 minutes to X",
"remind me at 6pm to X", "remind me every day at 9am to X") and fire them in the
background — speaking aloud, pushing to the dashboard, and optionally notifying
remote channels (Telegram) via the `on_fire` hook.

Reminders are persisted to reminders.json so they survive restarts.
"""

import os
import re
import json
import logging
import time

logger = logging.getLogger("Jarvis.Scheduler")
import threading
import datetime

import dateparser
from dateparser.search import search_dates

from config import Config

# Phrases to strip when extracting the "what" of a reminder
PREFIXES = [
    "remind me to ", "remind me that ", "remind me about ", "remind me in ",
    "remind me at ", "remind me ", "set a reminder to ", "set a reminder ",
    "set reminder to ", "set reminder ", "add a reminder to ", "add a reminder ",
    "add reminder to ", "add reminder ", "reminder: ", "reminder ",
]

DAILY_KEYWORDS = ["every day", "daily", "each day"]

WEEKDAYS = {
    'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3,
    'friday': 4, 'saturday': 5, 'sunday': 6,
}


class ReminderScheduler:
    def __init__(self, mouth=None, poll_interval=None):
        self.base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.file_path = os.path.join(self.base_dir, 'reminders.json')
        self.mouth = mouth
        self.poll_interval = poll_interval or Config.REMINDER_POLL_INTERVAL
        self.on_fire = None  # optional callback fired for remote notification

        self._lock = threading.Lock()
        self._reminders = []
        self._next_id = 1
        self._running = False
        self._thread = None
        # Recently fired reminders, keyed by id, so the dashboard's Snooze
        # button can reschedule them even after they were removed from the queue.
        self._last_fired = {}

        # Do NOT load persisted reminders — reminders should only fire when
        # the user explicitly tells Jarvis to set one.
        self._clear_persisted()

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    def _load(self):
        # Always reset first: if the file is missing, unreadable or invalid,
        # stale in-memory reminders must not survive (they'd fire anyway).
        self._reminders = []
        self._next_id = 1
        try:
            if os.path.exists(self.file_path):
                with open(self.file_path, 'r') as f:
                    data = json.load(f)
                self._reminders = data.get('reminders', [])
                self._next_id = data.get('next_id', 1)
        except Exception as e:
            logger.warning("Failed to load reminders: %s", e)

    def _clear_persisted(self):
        """Remove any saved reminders so old ones don't auto-fire on startup."""
        self._reminders = []
        self._next_id = 1
        try:
            if os.path.exists(self.file_path):
                os.remove(self.file_path)
        except Exception:
            pass

    def _save(self):
        try:
            with self._lock:
                data = {"next_id": self._next_id, "reminders": self._reminders}
            with open(self.file_path, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning("Failed to save reminders: %s", e)

    # ------------------------------------------------------------------ #
    # Parsing
    # ------------------------------------------------------------------ #
    def _strip_prefix(self, text):
        lower = text.lower().strip()
        for prefix in PREFIXES:
            if lower.startswith(prefix):
                return text.strip()[len(prefix):]
        return text.strip()

    def _extract_relative_delta(self, lower):
        """'in 20 minutes', 'in 2 hours', 'in 3 days' -> timedelta or None."""
        m = re.search(r'\bin\s+(\d+)\s*(minutes?|mins?|hours?|hrs?|days?|weeks?)\b', lower)
        if not m:
            return None
        n = int(m.group(1))
        unit = m.group(2)
        if unit.startswith(('h', 'hr')):
            return datetime.timedelta(hours=n)
        if unit.startswith(('d', 'day')):
            return datetime.timedelta(days=n)
        if unit.startswith('w'):
            return datetime.timedelta(weeks=n)
        return datetime.timedelta(minutes=n)

    def _extract_time(self, lower):
        """Absolute time-of-day -> (hour, minute) or None. Handles 6pm, 6:30pm, 18:30, 6 o'clock."""
        # Explicit am/pm / o'clock / 24h clock
        m = re.search(r'\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.|o\'?clock)\b', lower)
        if m:
            return self._normalize_time(int(m.group(1)), int(m.group(2) or 0), m.group(3).lower())
        # 'at 6' / 'at 18:30' without am/pm
        m = re.search(r'\bat\s+(\d{1,2})(?::(\d{2}))?\b', lower)
        if m:
            return self._normalize_time(int(m.group(1)), int(m.group(2) or 0), '')
        return None

    @staticmethod
    def _normalize_time(hour, minute, modifier):
        mod = (modifier or '').lower()
        if mod.startswith('p') and hour < 12:
            hour += 12
        elif mod.startswith('a') and hour == 12:
            hour = 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return (hour, minute)
        return None

    def _extract_day_offset(self, lower):
        """Day keyword -> offset from today, or None."""
        if 'the day after tomorrow' in lower:
            return 2
        if 'tomorrow' in lower or 'tmrw' in lower:
            return 1
        if 'next week' in lower:
            return 7
        if 'today' in lower or 'tonight' in lower:
            return 0
        for name, idx in WEEKDAYS.items():
            if name in lower:
                offset = (idx - datetime.datetime.now().weekday()) % 7
                return offset if offset > 0 else 7
        return None

    def _parse_due(self, text):
        """
        Parse the due datetime from reminder text.
        Returns (due_datetime, matched_substrings, repeat_daily) or (None, [], False).
        """
        lower = text.lower()
        repeat_daily = any(kw in lower for kw in DAILY_KEYWORDS)
        now = datetime.datetime.now()

        # 1. Relative ("in 20 minutes") — most reliable, wins immediately
        rel = self._extract_relative_delta(lower)
        if rel is not None:
            return now + rel, [], repeat_daily

        # 2. Day keyword + absolute time ("tomorrow at 9am", "next monday at 6pm")
        day_offset = self._extract_day_offset(lower)
        abs_time = self._extract_time(lower)
        if day_offset is not None or abs_time is not None:
            base = now + datetime.timedelta(days=day_offset or 0)
            if abs_time is not None:
                due = base.replace(hour=abs_time[0], minute=abs_time[1], second=0, microsecond=0)
            else:
                due = base.replace(hour=9, minute=0, second=0, microsecond=0)
            # Roll past absolute times forward (e.g. "at 6pm" said after 6pm)
            if day_offset is None and abs_time is not None and due <= now:
                due += datetime.timedelta(days=1)
            return due, [], repeat_daily

        # 3. Fallback: let search_dates find anything else (e.g. "next month", "on the 5th")
        try:
            found = search_dates(text, settings={
                'PREFER_DATES_FROM': 'future',
                'RELATIVE_BASE': now,
            })
        except Exception:
            found = None
        if found:
            best = max(found, key=lambda x: len(x[0]))
            due = best[1]
            if due <= now:
                due += datetime.timedelta(days=1)
            return due, [s for s, _ in found], repeat_daily

        return None, [], repeat_daily

    def _extract_what(self, text, matched):
        """Strip prefixes, parsed date phrases and connectors to get the reminder content."""
        lower = text.lower()
        cleaned = text

        for sub in sorted(matched, key=len, reverse=True):
            idx = lower.find(sub.lower())
            if idx != -1:
                cleaned = cleaned[:idx] + " " + cleaned[idx + len(sub):]
                lower = cleaned.lower()

        # Regex-strip time/date phrases ('at 6pm', 'in 20 minutes', 'tomorrow', weekdays...)
        cleaned = re.sub(r'\bin\s+\d+\s*(minutes?|mins?|hours?|hrs?|days?|weeks?)\b', ' ', cleaned, flags=re.I)
        cleaned = re.sub(r'\bat\s+\d{1,2}(?::\d{2})?\s*(?:am|pm|o\'?clock)?\b', ' ', cleaned, flags=re.I)
        cleaned = re.sub(r'\b\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.|o\'?clock)\b', ' ', cleaned, flags=re.I)
        cleaned = re.sub(r'\bnext\s+(?:mon|tue|wed|thu|fri|sat|sun)(?:day)?\b', ' ', cleaned, flags=re.I)
        for phrase in ['the day after tomorrow', 'tomorrow', 'tmrw', 'tonight', 'today', 'next week']:
            cleaned = re.sub(r'\b' + phrase + r'\b', ' ', cleaned, flags=re.I)
        for name in WEEKDAYS:
            cleaned = re.sub(r'\b' + name + r'\b', ' ', cleaned, flags=re.I)
        for kw in DAILY_KEYWORDS:
            cleaned = re.sub(r'\b' + kw + r'\b', ' ', cleaned, flags=re.I)

        cleaned = re.sub(r'^(?:to|that|about|me|the)\b', ' ', cleaned.strip(), flags=re.I)
        cleaned = cleaned.strip(" .,;:-")
        cleaned = re.sub(r'\s+', ' ', cleaned)
        return cleaned or "your reminder"

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def schedule(self, text):
        """
        Parse and store a reminder from natural language.
        Returns a human-readable confirmation string.
        """
        cleaned = self._strip_prefix(text)
        due, matched, repeat_daily = self._parse_due(cleaned)

        if due is None:
            return ("I couldn't figure out when to remind you. "
                    "Try something like 'remind me in 20 minutes to drink water'.")

        what = self._extract_what(cleaned, matched)

        with self._lock:
            reminder = {
                "id": self._next_id,
                "text": what,
                "due": due.isoformat(),
                "repeat_daily": repeat_daily,
                "created": datetime.datetime.now().isoformat(),
            }
            self._next_id += 1
            self._reminders.append(reminder)

        self._save()

        # Auto-start the background polling loop only when a reminder is
        # explicitly set — no polling until the user asks.
        self.start()

        when = due.strftime("%A, %I:%M %p")
        repeat_note = " (repeats daily)" if repeat_daily else ""
        return f"Reminder set: {what} at {when}{repeat_note}."

    def list_reminders(self):
        """Return a formatted summary of all pending reminders."""
        with self._lock:
            reminders = list(self._reminders)

        if not reminders:
            return "You have no pending reminders."

        now = datetime.datetime.now()
        lines = []
        for r in sorted(reminders, key=lambda x: x['due']):
            try:
                due = datetime.datetime.fromisoformat(r['due'])
            except Exception:
                continue
            delta = due - now
            if delta.total_seconds() <= 0:
                when = "due now"
            elif delta.total_seconds() < 3600:
                when = f"in {max(1, int(delta.total_seconds() // 60))} minutes"
            else:
                when = due.strftime("%A, %I:%M %p")
            repeat = " (daily)" if r.get('repeat_daily') else ""
            lines.append(f"#{r['id']}: {r['text']} — {when}{repeat}")

        return "Pending reminders:\n" + "\n".join(lines)

    def cancel(self, keyword):
        """Remove reminders whose text matches a keyword. Returns a message."""
        if not keyword:
            return "Which reminder should I cancel?"
        kw = keyword.lower()
        with self._lock:
            matched = [r for r in self._reminders
                       if kw in r['text'].lower() or str(r['id']) == kw]
            if matched:
                self._reminders = [r for r in self._reminders if r not in matched]
                names = ", ".join(f"#{r['id']} ({r['text']})" for r in matched)
            else:
                names = None

        if names:
            self._save()
            return f"Cancelled reminder(s): {names}."
        return f"I couldn't find a reminder matching '{keyword}'."

    def _fire(self, reminder):
        msg = f"⏰ Reminder: {reminder['text']}"
        logger.info(msg)
        # Keep a copy so the dashboard can snooze it after it fires
        with self._lock:
            self._last_fired[reminder['id']] = dict(reminder)
            if len(self._last_fired) > 20:
                oldest = min(self._last_fired)
                del self._last_fired[oldest]
        try:
            from utils.server import send_to_ui
            send_to_ui('ai_text', {'text': msg})
            # Dedicated notification event so the dashboard can show a
            # Snooze/Dismiss toast.
            send_to_ui('reminder_fired', {'id': reminder['id'], 'text': reminder['text']})
        except Exception:
            pass
        if self.mouth is not None:
            try:
                self.mouth.speak(f"Sir, reminder: {reminder['text']}")
            except Exception as e:
                logger.warning("Reminders: speak failed: %s", e)
        if self.on_fire is not None:
            try:
                self.on_fire(reminder)
            except Exception as e:
                logger.warning("Reminders: on_fire hook failed: %s", e)

    def snooze(self, reminder_id, minutes=10):
        """
        Reschedules a fired (or pending) reminder by id for `minutes` from now.
        """
        try:
            reminder_id = int(reminder_id)
        except (TypeError, ValueError):
            return "Invalid reminder id for snooze."
        minutes = max(1, int(minutes))

        reminder = None
        with self._lock:
            for r in self._reminders:
                if r['id'] == reminder_id:
                    reminder = r
                    break
            if reminder is None and reminder_id in self._last_fired:
                reminder = dict(self._last_fired[reminder_id])
                reminder.pop('due', None)
            if reminder is not None and reminder in self._reminders:
                self._reminders.remove(reminder)

        if reminder is None:
            return f"I couldn't find a reminder with id {reminder_id} to snooze."

        reminder['due'] = (datetime.datetime.now() + datetime.timedelta(minutes=minutes)).isoformat()
        with self._lock:
            self._reminders.append(reminder)
        self._save()
        return f"Snoozed '{reminder['text']}' for {minutes} minutes."

    # ------------------------------------------------------------------ #
    # Background loop
    # ------------------------------------------------------------------ #
    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="ReminderScheduler")
        self._thread.start()
        logger.info("Reminder scheduler started.")

    def stop(self):
        self._running = False

    def _loop(self):
        while self._running:
            try:
                self._check_due()
            except Exception as e:
                logger.warning("Scheduler loop error: %s", e)
            time.sleep(self.poll_interval)

    def _check_due(self):
        now = datetime.datetime.now()
        due_now = []
        with self._lock:
            for r in self._reminders:
                try:
                    due = datetime.datetime.fromisoformat(r['due'])
                except Exception:
                    continue
                if due <= now:
                    due_now.append(r)

        if not due_now:
            return

        for r in due_now:
            self._fire(r)
            with self._lock:
                if r in self._reminders:
                    self._reminders.remove(r)
                if r.get('repeat_daily'):
                    try:
                        next_due = datetime.datetime.fromisoformat(r['due']) + datetime.timedelta(days=1)
                        r['due'] = next_due.isoformat()
                        self._reminders.append(r)
                    except Exception:
                        pass
        self._save()


if __name__ == "__main__":
    # Quick self-test (uses a temp file so the real reminders.json is untouched)
    import tempfile
    s = ReminderScheduler(mouth=None, poll_interval=999)
    s.file_path = os.path.join(tempfile.gettempdir(), 'jarvis_scheduler_selftest.json')
    if os.path.exists(s.file_path):
        os.remove(s.file_path)  # start clean
    s._load()
    for t in [
        "remind me in 2 minutes to drink water",
        "remind me tomorrow at 9am to call mom",
        "set a reminder to submit the report at 6pm",
        "remind me every day at 8am to take vitamins",
        "remind me in 30 minutes to stand up and stretch",
        "remind me at 6pm to call dad",
        "remind me next monday at 9am to pay rent",
    ]:
        due, matched, rep = s._parse_due(s._strip_prefix(t))
        what = s._extract_what(s._strip_prefix(t), matched)
        print(f"{t!r}\n  due={due} repeat={rep}\n  what={what!r}\n  -> {s.schedule(t)}")
    print(s.list_reminders())
    s.stop()
