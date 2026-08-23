"""
Jarvis Transcript Archive (FTS5)
================================

Layer-2 memory: a fast full-text index over every conversation exchange,
complementing the L1 rolling digest (recent context) and L3 vector /
episodic stores (semantic recall).

    brain/data/transcripts.db   (SQLite, FTS5 virtual table)

Indexed from Brain._append_history; backfilled once from the persisted
conversation_history.json on first boot so past sessions are searchable
immediately.
"""

import os
import json
import sqlite3
import logging
import threading
import datetime

logger = logging.getLogger("Jarvis.Transcripts")

_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS transcripts USING fts5(
    user_text, assistant_text, source UNINDEXED, created UNINDEXED
);
"""


class TranscriptArchive:
    def __init__(self, db_path=None):
        self.base_dir = os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))
        self.db_path = db_path or os.path.join(
            self.base_dir, 'brain', 'data', 'transcripts.db')
        self._lock = threading.Lock()
        self._init_db()

    # ------------------------------------------------------------------ #
    def _connect(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self):
        try:
            with self._lock:
                conn = self._connect()
                try:
                    conn.executescript(_SCHEMA)
                    conn.commit()
                finally:
                    conn.close()
            self._backfill_history()
        except Exception as e:
            logger.error(f"Transcript archive init failed: {e}")

    def _backfill_history(self):
        """One-time import of the legacy conversation history JSON."""
        if self.count() > 0:
            return
        # Look NEXT TO THE DB so tests with temp dbs stay isolated —
        # in production the db lives in brain/data/ beside the history.
        hist_file = os.path.join(os.path.dirname(self.db_path),
                                 'conversation_history.json')
        if not os.path.exists(hist_file):
            return
        try:
            with open(hist_file, 'r') as f:
                entries = json.load(f)
            rows = [(e.get('user', ''), e.get('assistant', ''),
                     'backfill',
                     (e.get('created') or
                      datetime.datetime.now().isoformat()))
                    for e in entries if isinstance(e, dict)]
            if rows:
                with self._lock:
                    conn = self._connect()
                    try:
                        conn.executemany(
                            "INSERT INTO transcripts (user_text, "
                            "assistant_text, source, created) "
                            "VALUES (?, ?, ?, ?)", rows)
                        conn.commit()
                    finally:
                        conn.close()
                logger.info(f"Backfilled {len(rows)} exchanges into "
                            f"transcript archive")
        except Exception as e:
            logger.warning(f"Backfill skipped: {e}")

    # ------------------------------------------------------------------ #
    def index_exchange(self, user_text, assistant_text, source='chat'):
        """Persist one exchange into the FTS index (fire-safe)."""
        try:
            with self._lock:
                conn = self._connect()
                try:
                    conn.execute(
                        "INSERT INTO transcripts (user_text, "
                        "assistant_text, source, created) "
                        "VALUES (?, ?, ?, ?)",
                        (str(user_text or '')[:8000],
                         str(assistant_text or '')[:8000],
                         str(source or 'chat'),
                         datetime.datetime.now().isoformat()))
                    conn.commit()
                finally:
                    conn.close()
        except Exception as e:
            logger.debug(f"Transcript index failed: {e}")

    def search(self, query, limit=5):
        """
        Full-text search across all past conversations.

        Returns [{'user','assistant','source','created','snippet'}, …]
        ordered by relevance.  Empty list on no match / any failure.
        """
        query = str(query or '').strip()
        if not query:
            return []
        # FTS5 query syntax: quote terms to avoid operator injection
        safe = " ".join(
            '"' + term.replace('"', '""') + '"'
            for term in query.split()[:8])
        if not safe:
            return []
        try:
            with self._lock:
                conn = self._connect()
                try:
                    cur = conn.execute(
                        "SELECT user_text, assistant_text, source, "
                        "created, snippet(transcripts, 0, '»', '«', "
                        "' … ', 12) AS snip "
                        "FROM transcripts WHERE transcripts MATCH ? "
                        "ORDER BY rank LIMIT ?", (safe, int(limit)))
                    rows = cur.fetchall()
                finally:
                    conn.close()
            out = []
            for user_t, ai_t, src, created, snip in rows:
                out.append({
                    'user': user_t[:300],
                    'assistant': ai_t[:300],
                    'source': src,
                    'created': created,
                    'snippet': snip,
                })
            return out
        except Exception as e:
            logger.debug(f"Transcript search failed: {e}")
            return []

    def count(self):
        try:
            with self._lock:
                conn = self._connect()
                try:
                    cur = conn.execute(
                        "SELECT count(*) FROM transcripts")
                    row = cur.fetchone()
                finally:
                    conn.close()
            return int(row[0]) if row else 0
        except Exception:
            return 0


_shared = None


def get_archive():
    global _shared
    if _shared is None:
        _shared = TranscriptArchive()
    return _shared
