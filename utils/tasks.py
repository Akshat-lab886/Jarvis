"""
Jarvis Tasks & Notes

Small persistent stores for the two classic assistant features:

- TodoList: "add buy milk to my todo list", "what's on my todo list",
  "mark X as done", "clear my todos". Persisted to todo.json.
- NotePad: "take a note: <text>", "show my notes", "delete note about X".
  Persisted to notes.json.

Both are thread-safe and survive restarts.
"""

import os
import json
import threading
import datetime


class _JsonStore:
    """Base class: thread-safe JSON list store."""

    def __init__(self, filename):
        self.base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.file_path = os.path.join(self.base_dir, filename)
        # RLock so save() can safely be invoked while the lock is held
        self._lock = threading.RLock()
        self._items = []
        self._load()

    def _load(self):
        # Always reset first: a missing/unreadable file must not leave stale
        # in-memory items behind (they'd surface as phantom todos/notes).
        self._items = []
        try:
            if os.path.exists(self.file_path):
                with open(self.file_path, 'r') as f:
                    data = json.load(f)
                if isinstance(data, list):
                    self._items = data
        except Exception as e:
            print(f"{type(self).__name__}: Failed to load {self.file_path}: {e}")

    def _save(self):
        try:
            with self._lock:
                data = list(self._items)
            with open(self.file_path, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"{type(self).__name__}: Failed to save: {e}")


class TodoList(_JsonStore):
    def __init__(self):
        super().__init__('todo.json')

    def add(self, text):
        text = text.strip().strip('"').strip("'")
        if not text:
            return "What should I add to your todo list, Sir?"
        with self._lock:
            self._items.append({
                "id": len(self._items) + 1,
                "text": text,
                "done": False,
                "created": datetime.datetime.now().isoformat(timespec="seconds"),
            })
        self._save()
        return f"Added to your todo list: {text}"

    def list(self):
        with self._lock:
            items = list(self._items)
        if not items:
            return "Your todo list is empty."
        lines = []
        for it in items:
            status = "✅" if it.get("done") else "⬜"
            lines.append(f"{status} #{it['id']}: {it['text']}")
        return "Your todo list:\n" + "\n".join(lines)

    def items_json(self):
        """Dashboard-friendly snapshot."""
        with self._lock:
            return list(self._items)

    def _find(self, keyword):
        kw = keyword.lower().strip()
        with self._lock:
            for it in self._items:
                if str(it['id']) == kw or kw in it['text'].lower():
                    return it
        return None

    def done(self, keyword):
        it = self._find(keyword)
        if not it:
            return f"I couldn't find a todo matching '{keyword}'."
        it["done"] = True
        self._save()
        return f"Marked done: {it['text']}"

    def remove(self, keyword):
        it = self._find(keyword)
        if not it:
            return f"I couldn't find a todo matching '{keyword}'."
        with self._lock:
            self._items.remove(it)
        self._save()
        return f"Removed from your todo list: {it['text']}"

    def clear(self):
        with self._lock:
            self._items = []
        self._save()
        return "Todo list cleared."


class NotePad(_JsonStore):
    def __init__(self):
        super().__init__('notes.json')

    def save(self, text):
        text = text.strip().strip('"').strip("'")
        if not text:
            return "What should I note down, Sir?"
        with self._lock:
            self._items.append({
                "id": len(self._items) + 1,
                "text": text,
                "created": datetime.datetime.now().isoformat(timespec="seconds"),
            })
        self._save()
        return f"Note saved: {text[:60]}{'...' if len(text) > 60 else ''}"

    def list(self):
        with self._lock:
            items = list(self._items)
        if not items:
            return "You have no notes yet."
        lines = []
        for it in items[-10:][::-1]:  # most recent first, max 10
            created = it.get("created", "")
            stamp = created[5:16] if created else ""
            lines.append(f"#{it['id']} [{stamp}] {it['text']}")
        return "Your recent notes:\n" + "\n".join(lines)

    def items_json(self, limit=50):
        """Dashboard-friendly snapshot (most recent first)."""
        with self._lock:
            items = list(self._items)
        return items[-limit:][::-1]

    def remove(self, keyword):
        kw = keyword.lower().strip()
        removed = None
        with self._lock:
            for it in self._items:
                if str(it['id']) == kw or kw in it['text'].lower():
                    self._items.remove(it)
                    removed = it
                    break
        if removed is None:
            return f"I couldn't find a note matching '{keyword}'."
        self._save()
        return f"Deleted note: {removed['text'][:60]}"
