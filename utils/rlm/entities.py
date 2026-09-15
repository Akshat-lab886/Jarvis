"""
RLM — Temporal Entity Graph (PrimeAgent-level memory, part 1)
=============================================================

A lightweight, dependency-free knowledge-graph layer over the RLM
hierarchy: WHO/WHAT the memory is about, how often, and when last.

    "Talked with Akshat Pratap about the Jarvis refactor"
      → entities: [Akshat Pratap, Jarvis]
      → edges:    (Akshat Pratap) — co-occurs-with — (Jarvis)

This is what turns a pile of notes into a memory you can *query by
subject* ("what do I know about Akshat") and what lets recall filter,
rank and connect instead of only keyword-match.

Design constraints (Jarvis house style):

* pure stdlib, heuristic extraction — no spaCy/model download
* bounded: capped entities, capped per-entity note ids, capped edges
* persisted inside the RLM JSON (``entities`` key), rebuildable
* thread-safe, never raises into the caller
* ``JARVIS_RLM_ENTITIES=0`` switches the whole layer off
"""

import os
import re
import itertools
import threading

# --------------------------------------------------------------------- #
# Switches + tunables
# --------------------------------------------------------------------- #

_MAX_PER_ENTITY_NOTES = 24        # provenance ids kept per entity
_MAX_EDGES = 1200
_MAX_NAMES_PER_NOTE = 8           # entities recorded per note


def _max_entities():
    """Max distinct subjects tracked (per-call so tests/env flips apply)."""
    try:
        return max(10, int(os.getenv('JARVIS_RLM_ENTITY_MAX', '500')))
    except (TypeError, ValueError):
        return 500


# Backwards-compat alias (imported nowhere internally anymore).
try:
    _MAX_ENTITIES = _max_entities()
except Exception:
    _MAX_ENTITIES = 500

# Words that look like entities but aren't (greetings, roles, calendar
# words, fillers that start sentences).
_BLOCK = {
    'jarvis', 'user', 'sir', 'hey', 'hi', 'hello', 'ok', 'okay', 'yes',
    'no', 'please', 'thanks', 'thank', 'you', 'and', 'but', 'the',
    'this', 'that', 'these', 'those', 'what', 'when', 'where', 'who',
    'why', 'how', 'monday', 'tuesday', 'wednesday', 'thursday',
    'friday', 'saturday', 'sunday', 'today', 'tomorrow', 'yesterday',
    'tonight', 'morning', 'afternoon', 'evening', 'night', 'week',
    'weeks', 'month', 'months', 'year', 'january', 'february', 'march',
    'april', 'may', 'june', 'july', 'august', 'september', 'october',
    'november', 'december', 'user', 'assistant', 'api', 'http', 'https',
    'json', 'python', 'code', 'file', 'files', 'folder', 'project',
}

# Multi-word capitalized spans: "Akshat Pratap", "Blue Bottle Coffee"
_CAP_SEQ_RE = re.compile(
    r'\b([A-Z][a-zA-Z0-9&\'-]{1,}(?:\s+(?:of|the|de|van|von|del)?\s*'
    r'[A-Z][a-zA-Z0-9&\'-]{1,})+)\b')

# Single capitalized words (accepted only mid-sentence — the sentence
# starter is almost always just a capitalized normal word)
_CAP_WORD_RE = re.compile(r'\b([A-Z][a-zA-Z0-9&\'-]{2,})\b')

# Path-like / code-like identifiers: utils/executor.py, SKILL.md,
# docker-compose.yml, snake_case_id
_PATHISH_RE = re.compile(
    r'\b([\w.-]+(?:/[\w.-]+)+)\b'                 # a/b[/c...]
    r'|\b([A-Za-z][\w-]*\.(?:py|js|ts|tsx|jsx|json|md|yaml|yml|toml|'
    r'sql|csv|txt|html|css|sh|env))\b'             # name.ext
    r'|\b([a-z][a-z0-9]*(?:_[a-z0-9]+){2,})\b'    # snake_case_long
)

# Sentence starts (to exclude their leading capitalized word)
_SENT_SPLIT_RE = re.compile(r'(?<=[.!?])\s+|\n')


def enabled():
    return os.getenv('JARVIS_RLM_ENTITIES', '1') != '0'


def extract(text):
    """
    Heuristic entity extraction.  Returns a de-duplicated list of names
    (display casing preserved, first occurrence wins).  Never raises.
    """
    try:
        text = str(text or '')
        if not text.strip():
            return []
        found, seen = [], set()

        def _add(name):
            name = name.strip(' .,;:!?()"\'')
            if len(name) < 3 or len(name) > 60:
                return
            key = name.lower()
            if key in seen or key in _BLOCK:
                return
            if not any(c.isalnum() for c in name):
                return
            seen.add(key)
            found.append(name)

        # Sentence-initial tokens are excluded from single-word matches
        sents = _SENT_SPLIT_RE.split(text)
        starts = set()
        for sent in sents:
            first = re.match(r'\s*([A-Za-z]+)', sent)
            if first:
                starts.add(first.group(1).lower())

        # 1) path-like identifiers (highest precision)
        for m in _PATHISH_RE.finditer(text):
            for g in m.groups():
                if g:
                    _add(g)

        # 2) multi-word capitalized spans (people, products, companies)
        for m in _CAP_SEQ_RE.finditer(text):
            _add(re.sub(r'\s+', ' ', m.group(1)))

        # 3) single capitalized words mid-sentence — except a sentence
        #    opener, UNLESS its sentence also names a multi-word entity
        #    ("Atlas planning with Priya Sharma." → Atlas is a project).
        def _opens_named_sentence(w):
            for sent in sents:
                if _CAP_SEQ_RE.search(sent) and \
                        re.search(r'\b' + re.escape(w) + r'\b', sent):
                    return True
            return False

        for m in _CAP_WORD_RE.finditer(text):
            w = m.group(1)
            if w.lower() not in starts or _opens_named_sentence(w):
                _add(w)

        # 4) the subject named after "User:" is a likely entity even when
        #    lowercase ("User: atlas checkpoint one" → tracks 'Atlas').
        _u = re.search(r'\bUser:\s*([A-Za-z][\w-]{2,})', text)
        if _u and _u.group(1).lower() not in _BLOCK:
            _add(_u.group(1)[:1].upper() + _u.group(1)[1:].lower())

        return found[:_MAX_NAMES_PER_NOTE]
    except Exception:
        return []


class EntityGraph:
    """
    Bounded temporal entity index: mentions, last-seen, provenance and
    co-occurrence edges.  ``record()`` is the only write API; everything
    else is read-only.  Thread-safe; JSON-serialisable via
    ``to_dict``/``from_dict``.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._ents = {}        # lower-key → {display, count, last_seen, note_ids}
        self._edges = {}       # "a|b" (sorted, lowered) → count

    # ------------------------------ writes -------------------------- #

    def record(self, note_id, text, ts=None):
        """
        Index every entity found in *text* as mentioned by note *note_id*.
        Fire-and-forget: never raises, returns the names recorded.
        """
        if not enabled():
            return []
        try:
            names = extract(text)
            if not names:
                return []
            ts = str(ts or '')
            with self._lock:
                for name in names:
                    self._touch(name, note_id, ts)
                # co-occurrence edges (bounded fan-out per note)
                keys = sorted(n.lower() for n in names)[:6]
                for a, b in itertools.combinations(keys, 2):
                    ekey = f"{a}|{b}"
                    self._edges[ekey] = self._edges.get(ekey, 0) + 1
                if len(self._edges) > _MAX_EDGES:
                    keep = sorted(self._edges.items(),
                                  key=lambda kv: kv[1], reverse=True)
                    self._edges = dict(keep[:_MAX_EDGES])
            return names
        except Exception:
            return []

    def _touch(self, name, note_id=None, ts=None):
        key = name.lower()
        ent = self._ents.get(key)
        if ent is None:
            if len(self._ents) >= _max_entities():
                # Evict the least-relevant entity (lowest count, oldest)
                victim = min(self._ents.items(),
                             key=lambda kv: (kv[1]['count'],
                                             kv[1].get('last_seen', '')))
                del self._ents[victim[0]]
            ent = {'display': name, 'count': 0, 'last_seen': '',
                   'note_ids': []}
            self._ents[key] = ent
        ent['count'] += 1
        if ts and ts > ent.get('last_seen', ''):
            ent['last_seen'] = ts
        if note_id is not None:
            ids = ent['note_ids']
            if note_id not in ids:
                ids.append(note_id)
                del ids[:-_MAX_PER_ENTITY_NOTES]

    # ------------------------------ reads --------------------------- #

    def __len__(self):
        with self._lock:
            return len(self._ents)

    def match(self, query):
        """
        Best entity for a free-text *query*: the longest tracked entity
        whose name appears in the query (case-insensitive), else ''.
        """
        try:
            q = str(query or '').lower().strip()
            if not q:
                return ''
            with self._lock:
                keys = list(self._ents.keys())
            best = ''
            for key in keys:
                if key in q and len(key) > len(best):
                    best = key
            if best:
                with self._lock:
                    return self._ents[best]['display']
            return ''
        except Exception:
            return ''

    def about(self, name):
        """Entity record (display/count/last_seen/note_ids) or None."""
        with self._lock:
            return dict(self._ents.get(str(name or '').lower()) or {})

    def top(self, n=12):
        """Most salient entities: [(display, count, last_seen)]."""
        with self._lock:
            ents = sorted(self._ents.values(),
                          key=lambda e: (e['count'], e.get('last_seen', '')),
                          reverse=True)
            return [(e['display'], e['count'], e.get('last_seen', ''))
                    for e in ents[:max(1, n)]]

    def related(self, name, n=6):
        """Entities that co-occur most with *name* (the graph edges)."""
        key = str(name or '').lower()
        if not key:
            return []
        with self._lock:
            scored = []
            for ekey, cnt in self._edges.items():
                a, b = ekey.split('|', 1)
                if a == key:
                    scored.append((b, cnt))
                elif b == key:
                    scored.append((a, cnt))
            scored.sort(key=lambda kv: kv[1], reverse=True)
            out = []
            for k, _cnt in scored[:n]:
                ent = self._ents.get(k)
                if ent:
                    out.append(ent['display'])
            return out

    # --------------------------- persistence ------------------------ #

    def to_dict(self):
        with self._lock:
            return {'entities': {k: dict(v) for k, v in self._ents.items()},
                    'edges': dict(self._edges)}

    @classmethod
    def from_dict(cls, data):
        graph = cls()
        try:
            data = data or {}
            with graph._lock:
                for k, v in (data.get('entities') or {}).items():
                    if isinstance(v, dict):
                        graph._ents[str(k)] = {
                            'display': str(v.get('display', k)),
                            'count': int(v.get('count', 0)),
                            'last_seen': str(v.get('last_seen', '')),
                            'note_ids': list(v.get('note_ids') or [])
                        }
                for k, v in (data.get('edges') or {}).items():
                    try:
                        graph._edges[str(k)] = int(v)
                    except (TypeError, ValueError):
                        continue
        except Exception:
            pass
        return graph
