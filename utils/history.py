"""
Jarvis Command History

A lightweight, thread-safe log of every command the user sends (voice, web
dashboard or Telegram). Powers the "what did I do today?" daily-summary
feature. Persisted to brain/data/command_history.json and capped to the most
recent entries so it never grows unbounded.
"""

import os
import json
import threading
import datetime

MAX_ENTRIES = 1000


class CommandLog:
    def __init__(self):
        self.base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.file_path = os.path.join(self.base_dir, 'brain', 'data', 'command_history.json')
        self._lock = threading.Lock()
        self._entries = []
        self._load()

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    def _load(self):
        try:
            if os.path.exists(self.file_path):
                with open(self.file_path, 'r') as f:
                    data = json.load(f)
                if isinstance(data, list):
                    self._entries = data[-MAX_ENTRIES:]
        except Exception as e:
            print(f"CommandLog: Failed to load history: {e}")
            self._entries = []

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
            with self._lock:
                data = list(self._entries)
            with open(self.file_path, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"CommandLog: Failed to save history: {e}")

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def log(self, user_text, action, result=None):
        """Record one user command."""
        if not user_text:
            return
        entry = {
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "user_text": user_text,
            "action": action or "",
            "result": (result or "")[:200],
        }
        with self._lock:
            self._entries.append(entry)
            if len(self._entries) > MAX_ENTRIES:
                self._entries = self._entries[-MAX_ENTRIES:]
        self._save()

    def today(self, limit=50):
        """Return commands logged since local midnight, most recent last."""
        now = datetime.datetime.now()
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        with self._lock:
            entries = list(self._entries)
        today_entries = []
        for e in entries:
            try:
                ts = datetime.datetime.fromisoformat(e.get("ts", ""))
            except Exception:
                continue
            if ts >= start_of_day:
                today_entries.append(e)
        return today_entries[-limit:]

    def clear(self):
        with self._lock:
            self._entries = []
        self._save()
        return "Command history cleared."
