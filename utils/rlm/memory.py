"""
J.A.R.V.I.S. — RLM: the Recursive Language Model core (memory half).

A recursive language model augments a base LM with a MEMORY that
recursively abstracts its own inputs into levels of increasing
compression:

    L0  events        raw observations (one per conversation exchange)
    L1  summaries     session-sized folds of ~12 events
    L2  abstractions  period-sized folds of sessions (themes, arcs)
    L3  world model   ONE evolving model of the user + a playbook of
                      distilled insights

Retrieval is recursive too: a query fans out to every level, hits at
low levels summon their parents (an event recalls its session), and
the world model always anchors the block.  The result is unbounded
effective context and continual learning on a fixed prompt budget.

PrimeAgent-level upgrades on top of the pure hierarchy:

    * TEMPORAL SCORING   retrieval ranks relevance × recency decay ×
                         importance × reuse (Generative-Agents style)
    * SUPERSESSION       a new insight that contradicts an older one
                         REPLACES it (ADD/UPDATE semantics, Mem0 style)
                         instead of piling up stale duplicates
    * ENTITY GRAPH       who/what the memory is about, with counts,
                         last-seen and co-occurrence edges, queryable
                         by subject (``recall_about``)
    * CONTINUITY         after an idle gap the next turn re-injects
                         where the last session left off

Consolidation (the "sleep" pass) and reflection (insight extraction)
run in background threads through ``Brain.complete()`` — never on the
chat path — and every operation degrades to a silent no-op on any
failure.  Kill switch: ``JARVIS_RLM=0``.
"""

import os
import re
import json
import math
import time
import threading
import datetime
import logging

from utils.rlm.entities import (EntityGraph, extract as _extract_entities,
                                enabled as _entities_enabled)

logger = logging.getLogger("Jarvis.RLM")

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
DEFAULT_DATA_FILE = os.path.join(_BASE_DIR, 'brain', 'data',
                                 'rlm_memory.json')


def _data_file():
    """Active RLM store path (env-overridable for tests/multi-tenant)."""
    return os.getenv('JARVIS_RLM_DATA_FILE', '').strip() or DEFAULT_DATA_FILE

_VECTOR_MODEL = "all-MiniLM-L6-v2"     # shared with episodic/librarian


# --------------------------------------------------------------------- #
# Tunables (env-overridable, read per-call so tests can flip them)
# --------------------------------------------------------------------- #

def _env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def enabled():
    """Master switch — JARVIS_RLM=0 disables the whole subsystem."""
    return os.getenv('JARVIS_RLM', '1') != '0'


def _l0_batch():
    return max(2, _env_int('JARVIS_RLM_L0_BATCH', 12))


def _l1_batch():
    return max(2, _env_int('JARVIS_RLM_L1_BATCH', 6))


def _world_ttl_s():
    return max(0.0, _env_int('JARVIS_RLM_WORLD_TTL_H', 12) * 3600)


def _reflect_every():
    return max(1, _env_int('JARVIS_RLM_REFLECT_EVERY', 3))


def _max_chars():
    return max(400, _env_int('JARVIS_RLM_MAX_CHARS', 1400))


def _deep_chars():
    return max(1000, _env_int('JARVIS_RLM_DEEP_CHARS', 2200))


def _max_l0():
    return max(50, _env_int('JARVIS_RLM_MAX_L0', 400))


def _tick_s():
    return max(30.0, _env_int('JARVIS_RLM_TICK_S', 300))


def _env_float(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _decay_tau_days():
    """Recency-decay horizon in days (bigger = memory stays 'fresh'
    longer; the floor keeps durable knowledge from ever vanishing)."""
    return max(1.0, _env_float('JARVIS_RLM_DECAY_DAYS', 21))


def _continuity_gap_s():
    """Idle time after which the next turn counts as a NEW session."""
    return max(60.0, _env_float('JARVIS_RLM_CONTINUITY_GAP_S', 1800))


def _w_rel():
    return max(0.0, _env_float('JARVIS_RLM_W_REL', 1.0))


def _w_rec():
    return max(0.0, _env_float('JARVIS_RLM_W_REC', 0.5))


def _w_imp():
    return max(0.0, _env_float('JARVIS_RLM_W_IMP', 0.5))


# --------------------------------------------------------------------- #
# Small text helpers
# --------------------------------------------------------------------- #

_STOPWORDS = {
    'the', 'a', 'an', 'is', 'are', 'was', 'were', 'what', 'do', 'does',
    'did', 'about', 'tell', 'me', 'my', 'your', 'that', 'this', 'with',
    'for', 'from', 'and', 'or', 'of', 'to', 'in', 'on', 'at', 'i', 'you',
    'it', 'be', 'we', 'can', 'could', 'would', 'should', 'have', 'has',
}

_TRIVIAL_RE = re.compile(
    r'^(hi|hey|hello|yo|sup|thanks|thank you|thx|ok|okay|cool|nice|good'
    r'( morning| evening| afternoon| night)?|jarvis)[\s!.,?]*$',
    re.IGNORECASE)

_SIGNAL_WORDS = (
    'remember', 'my ', 'i am', "i'm", 'goal', 'deadline', 'birthday',
    'allerg', 'prefer', 'always', 'never', 'project', 'meeting', 'doctor',
    'flight', 'interview', 'anniversary', 'exam', 'salary', 'password',
)

# A new insight containing one of these probably REPLACES an older
# statement about the same subject ("user now lives in Tokyo" vs the
# old "user lives in Berlin").
_CHANGE_RE = re.compile(
    r'\b(now|no longer|not anymore|moved|switched|changed|updated|'
    r'instead of|replaced|current|used to|formerly|as of|new)\b',
    re.IGNORECASE)


def _norm(text):
    """Lowercase, punctuation-stripped, whitespace-collapsed text."""
    return re.sub(r'\s+', ' ', re.sub(r'[^\w\s]', '', str(text or '').lower())).strip()


def _tokens(text):
    return [w for w in _norm(text).split() if w and w not in _STOPWORDS]


def _token_overlap(query, text):
    """0..1 relevance of *text* to *query* (keyword fallback scorer)."""
    q = _tokens(query)
    if not q:
        return 0.0
    t = set(_tokens(text))
    if not t:
        return 0.0
    full = sum(1 for w in q if w in t)
    partial = sum(1 for w in q
                  if w not in t and any(w in tw for tw in t))
    return (full + 0.5 * partial) / len(q)


def _jaccard(a, b):
    ta, tb = set(_tokens(a)), set(_tokens(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _trivial(user_text):
    u = (user_text or '').strip()
    return len(u) < 25 or bool(_TRIVIAL_RE.match(u))


def _exchange_importance(user_text):
    t = (user_text or '').lower()
    score = 3
    if len(t) > 120:
        score += 2
    elif len(t) > 40:
        score += 1
    if any(w in t for w in _SIGNAL_WORDS):
        score += 2
    return max(1, min(9, score))


def _now_iso():
    return datetime.datetime.now().isoformat()


def _parse_iso(s):
    try:
        return datetime.datetime.fromisoformat(str(s)).timestamp()
    except Exception:
        return 0.0


# --------------------------------------------------------------------- #
# Optional semantic index (ChromaDB, mirrors episodic_memory's pattern:
# lazy build, watchdog-guarded, keyword fallback when unavailable)
# --------------------------------------------------------------------- #

_INDEX_CACHE = {}
_INDEX_BUILD_TIMEOUT = 30


class _RLMVectorIndex:
    """ChromaDB-backed semantic index over RLM notes, one collection."""

    def __init__(self, persist_dir):
        import chromadb
        from chromadb.utils import embedding_functions
        os.makedirs(persist_dir, exist_ok=True)
        self.client = chromadb.PersistentClient(path=persist_dir)
        self.embedding_fn = \
            embedding_functions.SentenceTransformerEmbeddingFunction(
                model_name=_VECTOR_MODEL)
        self.collection = self.client.get_or_create_collection(
            name="jarvis_rlm",
            embedding_function=self.embedding_fn,
        )

    def upsert(self, note_id, text, metadata):
        try:
            self.collection.upsert(
                documents=[text or " "],
                ids=[f"rlm_{int(note_id)}"],
                metadatas=[metadata or {}],
            )
        except Exception as e:
            logger.debug("vector upsert failed: %s", e)

    def delete_ids(self, note_ids):
        if not note_ids:
            return
        try:
            self.collection.delete(
                ids=[f"rlm_{int(i)}" for i in note_ids])
        except Exception as e:
            logger.debug("vector delete failed: %s", e)

    def query_ids(self, text, level, n=8):
        """Ranked note-ids at *level* most similar to *text*."""
        if not text or not text.strip():
            return []
        try:
            res = self.collection.query(query_texts=[text], n_results=n,
                                        where={"level": int(level)})
            out = []
            for raw in (res.get('ids') or [[]])[0]:
                m = re.match(r'rlm_(\d+)$', str(raw))
                if m:
                    out.append(int(m.group(1)))
            return out
        except Exception as e:
            logger.debug("vector query failed: %s", e)
            return []


def _get_index_for(vector_dir):
    """Get-or-build the shared index (None when unavailable/hung)."""
    key = os.path.abspath(vector_dir)
    if key in _INDEX_CACHE:
        return _INDEX_CACHE[key]

    if os.getenv('JARVIS_DISABLE_VECTOR') == '1':
        _INDEX_CACHE[key] = None
        return None

    outcome = {}

    def _build():
        try:
            outcome['index'] = _RLMVectorIndex(key)
        except Exception as e:
            outcome['error'] = str(e)

    builder = threading.Thread(target=_build, daemon=True,
                               name="rlm-vector-init")
    builder.start()
    builder.join(timeout=_INDEX_BUILD_TIMEOUT)

    if builder.is_alive():
        logger.warning("RLM vector index build TIMED OUT — keyword mode")
        index = None
    elif 'error' in outcome:
        logger.info("RLM vector index unavailable (%s) — keyword mode",
                    outcome['error'])
        index = None
    else:
        index = outcome.get('index')
        if index is not None:
            logger.info("RLM semantic index online (%s)", _VECTOR_MODEL)

    _INDEX_CACHE[key] = index
    return index


# --------------------------------------------------------------------- #
# The store
# --------------------------------------------------------------------- #

LEVEL_EVENT = 0
LEVEL_SUMMARY = 1
LEVEL_ABSTRACTION = 2
LEVEL_INSIGHT = 3

_LEVEL_NAMES = {0: 'event', 1: 'summary', 2: 'abstraction', 3: 'insight'}


class RecursiveMemory:
    """
    Hierarchical, recursively-consolidated long-term memory.

    Thread-safe; JSON persistence (source of truth) + optional
    ChromaDB index (derived, rebuildable).  Every LLM-touching
    operation takes a ``brain`` (anything with ``complete(prompt,
    agent=...)``) and no-ops gracefully when it returns None.
    """

    MAX_INSIGHTS = 120
    MAX_L1 = 150
    MAX_L2 = 80

    def __init__(self, data_file=None, use_vector=True, vector_dir=None):
        self.file_path = data_file or _data_file()
        self._lock = threading.RLock()
        self.use_vector = use_vector
        self._vector_dir = vector_dir or os.path.join(_BASE_DIR,
                                                      'knowledge_vault')
        self._notes = []
        self._next_id = 1
        self._world_model = ""
        self._world_model_updated = ""
        self.last_consolidation = ""
        self.entities = EntityGraph()      # temporal entity index
        self._unreflected = []       # [(user, ai)] awaiting reflection
        self._reflecting = False
        self._consolidating = False
        self._maint_thread = None
        self._suppress_save = True     # attribute writes must not clobber disk
        self._load()                   # while loading the persisted state
        self._suppress_save = False

    # ---- world model: persisted on write (source of truth = JSON) ---- #

    @property
    def world_model(self):
        return self._world_model

    @world_model.setter
    def world_model(self, value):
        self._world_model = value or ''
        if not getattr(self, '_suppress_save', True):
            self._save()

    @property
    def world_model_updated(self):
        return self._world_model_updated

    @world_model_updated.setter
    def world_model_updated(self, value):
        self._world_model_updated = value or ''
        if not getattr(self, '_suppress_save', True):
            self._save()

    # ---------------- persistence ---------------- #

    def _load(self):
        try:
            if os.path.exists(self.file_path):
                with open(self.file_path, 'r') as f:
                    data = json.load(f)
                # One non-dict entry would otherwise take down every
                # downstream pass (fold/prune/recall all index n['id'] /
                # n.get(...)).  Filter here at the single choke point so
                # a corrupt-but-parseable file degrades instead of
                # crashing the maintenance loop.
                self._notes = [n for n in data.get('notes', [])
                               if isinstance(n, dict)]
                self._next_id = int(data.get('next_id',
                                             len(self._notes) + 1))
                self.world_model = data.get('world_model', '') or ''
                self.world_model_updated = data.get('world_model_updated',
                                                     '') or ''
                self.last_consolidation = data.get('last_consolidation',
                                                    '') or ''
                self._unreflected = [
                    (str(p[0]), str(p[1]))
                    for p in (data.get('unreflected') or [])
                    if isinstance(p, (list, tuple)) and len(p) == 2
                ][-_reflect_every() * 4:]
                self.entities = EntityGraph.from_dict(data.get('entities'))
        except Exception as e:
            logger.warning("RLM load failed (starting fresh): %s", e)
            self._notes = []
            self._next_id = 1

    def _save(self):
        try:
            with self._lock:
                data = {
                    "version": 1,
                    "next_id": self._next_id,
                    "notes": self._notes,
                    "world_model": self.world_model,
                    "world_model_updated": self.world_model_updated,
                    "last_consolidation": self.last_consolidation,
                    "unreflected": [list(p) for p in self._unreflected],
                    "entities": self.entities.to_dict(),
                }
            os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
            with open(self.file_path, 'w') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning("RLM save failed: %s", e)

    # ---------------- vector helpers ---------------- #

    def _index(self):
        if not getattr(self, 'use_vector', True):
            return None
        return _get_index_for(self._vector_dir)

    def _vector_upsert(self, note):
        idx = self._index()
        if idx is not None and note is not None:
            idx.upsert(note.get('id'), note.get('text', ''),
                       {"level": int(note.get('level', 0)),
                        "kind": note.get('kind', ''),
                        "created": note.get('created', '')})

    def _vector_delete(self, ids):
        idx = self._index()
        if idx is not None:
            idx.delete_ids(ids)

    # ------------------------------------------------------------------ #
    # Observation (L0 writes)
    # ------------------------------------------------------------------ #

    def observe(self, text, source='chat', importance=4, kind='exchange',
                tags=None):
        """Append one raw L0 note. Returns the note or None when skipped."""
        if not enabled():
            return None
        text = str(text or '').strip()
        if not text:
            return None
        norm = _norm(text)
        with self._lock:
            for n in reversed(self._notes):
                if n.get('level') == LEVEL_EVENT:
                    if _norm(n.get('text', '')) == norm:
                        n['access_count'] = n.get('access_count', 0) + 1
                        return None          # duplicate — already seen
                    break
            note = {
                "id": self._next_id,
                "level": LEVEL_EVENT,
                "kind": kind,
                "text": text[:2000],
                "created": _now_iso(),
                "source": source,
                "importance": max(1, min(10, int(importance))),
                "tags": list(tags or []),
                "children": [],
                "folded": False,
                "access_count": 0,
            }
            self._notes.append(note)
            self._next_id += 1
        self.entities.record(note['id'], note.get('text', ''),
                             note['created'])
        self._save()
        self._vector_upsert(note)
        return note

    def observe_exchange(self, user_text, ai_text, brain=None):
        """
        Record one conversation exchange and, when thresholds are met,
        hand work to the background reflector/consolidator.  Never
        raises, never blocks on the LLM (threads only).
        """
        if not enabled():
            return None
        note = self.observe(
            f"User: {str(user_text or '')[:600]}\n"
            f"Jarvis: {str(ai_text or '')[:600]}",
            source='chat', kind='exchange',
            importance=_exchange_importance(user_text))
        with self._lock:
            self._unreflected.append((str(user_text or ''),
                                      str(ai_text or '')))
            if len(self._unreflected) > _reflect_every() * 6:
                self._unreflected.pop(0)
            need_reflect = len(self._unreflected) >= _reflect_every()
            pending_l0 = sum(
                1 for n in self._notes
                if n.get('level') == LEVEL_EVENT and not n.get('folded'))
        if brain is None:
            return note
        if need_reflect:
            self._spawn(self._reflect_worker, brain)
        if pending_l0 >= _l0_batch():
            self._spawn(self._consolidate_worker, brain)
        return note

    def _spawn(self, fn, *args):
        """Daemon-thread dispatcher (tests monkeypatch to run inline)."""
        threading.Thread(target=fn, args=args, daemon=True,
                         name="rlm-worker").start()

    def _reflect_worker(self, brain):
        try:
            self.reflect(brain)
        except Exception as e:
            logger.debug("RLM background reflection failed: %s", e)

    def _consolidate_worker(self, brain):
        try:
            self.consolidate(brain)
        except Exception as e:
            logger.debug("RLM background consolidation failed: %s", e)

    # ------------------------------------------------------------------ #
    # Reflection (exchanges → L3 insights)
    # ------------------------------------------------------------------ #

    _REFLECT_SYSTEM = (
        "You are Jarvis's REFLECTOR. You extract only durable, "
        "months-scale knowledge — never transient chatter."
    )

    def reflect(self, brain, exchanges=None):
        """
        Extract durable insights from recent exchanges (blocking).
        Returns the list of stored insight notes ([] on skip/failure).
        """
        if not enabled() or brain is None:
            return []
        with self._lock:
            if self._reflecting:
                return []
            self._reflecting = True
            batch = list(self._unreflected) if exchanges is None \
                else [(str(u), str(a)) for u, a in exchanges]
            self._unreflected = []

        try:
            batch = [b for b in batch if not _trivial(b[0])]
            if not batch:
                return []
            transcript = "\n".join(
                f"User: {u[:300]}\nJarvis: {a[:300]}"
                for u, a in batch[-_reflect_every() * 2:])
            prompt = (
                "Reflect on these recent exchanges between Jarvis and "
                f"the user.\n\nTRANSCRIPT:\n{transcript[:4000]}\n\n"
                "Extract 0-3 DURABLE insights worth remembering for "
                "months: user-model facts, preferences, goals, "
                "constraints, or actionable lessons for Jarvis. "
                "Skip anything transient, obvious, or trivial.\n"
                'Output ONLY raw JSON: {"insights": [{"text": "<one '
                'sentence>", "kind": "user_model|preference|goal|'
                'constraint|lesson", "importance": <1-10>}]}'
            )
            raw = None
            try:
                raw = brain.complete(prompt, agent='reflector',
                                     timeout=45, max_tokens=500)
            except Exception as e:
                logger.debug("reflection call failed: %s", e)
            insights = self._parse_insights(raw)
            stored = []
            for ins in insights[:4]:
                note = self._store_insight(
                    ins.get('text', ''), ins.get('kind', 'fact'),
                    ins.get('importance', 6))
                if note is not None:
                    stored.append(note)
                # Guardrails self-update: preferences/styles/rules land
                # in USER.md, durable facts in MEMORY.md (Hermes parity).
                try:
                    from utils.guardrails import get_guardrails
                    get_guardrails().capture_insight(
                        ins.get('text', ''), ins.get('kind', ''))
                except Exception:
                    pass
                if ins.get('kind') == 'lesson' and ins.get('text'):
                    try:
                        from utils import self_learning
                        self_learning.add_lesson(
                            str(ins['text']), scope='rlm-reflection')
                    except Exception:
                        pass
            if stored:
                logger.info("RLM reflection stored %d insight(s)",
                            len(stored))
            return stored
        finally:
            with self._lock:
                self._reflecting = False

    @staticmethod
    def _parse_insights(raw):
        if not raw:
            return []
        text = str(raw).strip()
        if text.startswith('```'):
            text = re.sub(r'^```(json)?|```$', '', text).strip()
        try:
            match = re.search(r'\{.*\}', text, re.DOTALL)
            data = json.loads(match.group(0)) if match else None
        except (json.JSONDecodeError, AttributeError):
            return []
        items = data.get('insights') if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []
        out = []
        for it in items:
            if not isinstance(it, dict):
                continue
            txt = str(it.get('text', '')).strip()
            if len(txt) < 8:
                continue
            try:
                imp = int(it.get('importance', 6))
            except (TypeError, ValueError):
                imp = 6
            out.append({'text': txt[:400],
                        'kind': str(it.get('kind', 'fact'))[:24],
                        'importance': max(1, min(10, imp))})
        return out[:4]

    def _supersede_candidates(self, text, kind):
        """
        Older insights that *text* contradicts or replaces (Mem0-style
        UPDATE targets): similar subject (mid jaccard band — not a
        near-dupe), same kind or a shared entity, and the new text
        carries a change marker ('now', 'moved', 'no longer', …).
        Bounded to 3; deterministic (no LLM call, fully testable).
        """
        try:
            if not _CHANGE_RE.search(text):
                return []
            new_ents = {e.lower() for e in _extract_entities(text)}
            out = []
            for n in self._notes:
                if n.get('level') != LEVEL_INSIGHT or n.get('superseded_by'):
                    continue
                # A revision ("now keeps widget 1 on the shelf") does not
                # replace ANOTHER revision ("now keeps widget 0 on the
                # shelf") — only baseline facts get superseded.
                if _CHANGE_RE.search(n.get('text', '')):
                    continue
                sim = _jaccard(n.get('text', ''), text)
                if sim <= 0.30 or sim > 0.75:
                    continue          # unrelated, or near-dupe (reinforced)
                old_ents = {e.lower() for e in
                            _extract_entities(n.get('text', ''))}
                same_kind = str(kind).lower() in \
                    (t.lower() for t in n.get('tags', []))
                if sim > 0.45 or same_kind or (new_ents & old_ents):
                    out.append(n)
                if len(out) >= 3:
                    break
            return out
        except Exception:
            return []

    def _store_insight(self, text, kind='fact', importance=6):
        text = str(text or '').strip()
        if not text or not enabled():
            return None
        with self._lock:
            # Reinforce a near-identical insight instead of duplicating
            for n in self._notes:
                if n.get('level') == LEVEL_INSIGHT and \
                        _jaccard(n.get('text', ''), text) > 0.75:
                    n['access_count'] = n.get('access_count', 0) + 1
                    n['importance'] = max(n.get('importance', 5),
                                          int(importance))
                    self._save()
                    return None
            # Contradiction handling: the new insight may REPLACE older
            # ones about the same subject instead of coexisting with
            # stale facts.
            superseded = self._supersede_candidates(text, kind)
            note = {
                "id": self._next_id,
                "level": LEVEL_INSIGHT,
                "kind": "insight",
                "text": text[:500],
                "created": _now_iso(),
                "source": f"reflection:{kind}",
                "importance": max(1, min(10, int(importance))),
                "tags": [str(kind)],
                "children": [],
                "folded": False,
                "access_count": 0,
                "supersedes": [n['id'] for n in superseded],
            }
            for old in superseded:
                old['superseded_by'] = note['id']
                old['superseded_at'] = _now_iso()
            self._notes.append(note)
            self._next_id += 1
        self.entities.record(note['id'], note.get('text', ''),
                             note['created'])
        self._save()
        self._vector_upsert(note)
        return note

    # ------------------------------------------------------------------ #
    # Consolidation ("sleep"): L0→L1→L2→world model
    # ------------------------------------------------------------------ #

    def consolidate(self, brain):
        """
        One maintenance pass.  Returns a dict describing the work done;
        every step is independently failure-tolerant (unconsumed batches
        simply retry on the next pass).
        """
        if not enabled() or brain is None:
            return {}
        with self._lock:
            if self._consolidating:
                return {}
            self._consolidating = True
        try:
            # L2 folding only ever folds summaries that existed at the
            # START of this pass — a summary freshly created below must
            # not be immediately collapsed into an abstraction in the
            # same pass (the cascade advances one level per pass).
            with self._lock:
                prior_ids = {n['id'] for n in self._notes}
            did = {
                'l1': self._fold(brain, LEVEL_EVENT, _l0_batch(),
                                 LEVEL_SUMMARY, 'summary',
                                 "Fold the following conversation events "
                                 "into ONE dense session summary.\n"
                                 "Preserve every fact, decision, name, "
                                 "number, goal and open thread; compress "
                                 "pleasantries away. Under 180 words.\n\n"
                                 "EVENTS:\n{body}"),
                'l2': self._fold(brain, LEVEL_SUMMARY, _l1_batch(),
                                 LEVEL_ABSTRACTION, 'abstraction',
                                 "Synthesize these session summaries into "
                                 "ONE higher-level abstraction: recurring "
                                 "themes, ongoing arcs, progress toward "
                                 "goals, and patterns in how the user "
                                 "works. Under 200 words.\n\n"
                                 "SUMMARIES:\n{body}",
                                 only_ids=prior_ids),
                'world': self._refresh_world_model(brain),
                'pruned': self._prune(),
            }
            if any(did.values()):
                with self._lock:
                    self.last_consolidation = _now_iso()
                self._save()
                logger.info("RLM consolidated: %s", did)
            return did
        finally:
            with self._lock:
                self._consolidating = False

    def _fold(self, brain, level, batch_size, target_level, kind,
              prompt_template, only_ids=None):
        """Fold one batch of *level* notes into a single parent note."""
        with self._lock:
            pending = [n for n in self._notes
                       if n.get('level') == level and not n.get('folded')
                       and (only_ids is None or n.get('id') in only_ids)]
            if len(pending) < batch_size:
                return 0
            batch = pending[:batch_size * 2]
            body = "\n".join(f"• {n.get('text', '')[:400]}" for n in batch)
        prompt = prompt_template.format(body=body[:8000])
        summary = None
        try:
            summary = brain.complete(prompt, agent='archivist',
                                     timeout=60, max_tokens=700)
        except Exception as e:
            logger.debug("fold L%d→L%d failed: %s", level, target_level, e)
        if not summary or len(summary.strip()) < 20:
            return 0                      # keep batch pending; retry later
        summary = summary.strip()[:2500]
        with self._lock:
            note = {
                "id": self._next_id,
                "level": target_level,
                "kind": kind,
                "text": summary,
                "created": _now_iso(),
                "source": "consolidation",
                "importance": 6,
                "tags": [],
                "children": [n['id'] for n in batch],
                "folded": False,
                "access_count": 0,
            }
            for n in batch:
                n['folded'] = True
            self._notes.append(note)
            self._next_id += 1
        self.entities.record(note['id'], summary, note['created'])
        self._save()
        self._vector_upsert(note)
        return 1

    def _refresh_world_model(self, brain):
        with self._lock:
            wm = self.world_model or ''
            wm_ts = _parse_iso(self.world_model_updated)
            notes = list(self._notes)
        fresh = [n for n in notes
                 if n.get('level') in (LEVEL_ABSTRACTION, LEVEL_INSIGHT)
                 and _parse_iso(n.get('created', '')) > wm_ts]
        if not fresh:
            return 0
        if wm and (time.time() - wm_ts) < _world_ttl_s():
            return 0                        # rate-limited refresh
        recent_l2 = [n['text'] for n in fresh
                     if n.get('level') == LEVEL_ABSTRACTION][:6]
        recent_ins = [n['text'] for n in fresh
                      if n.get('level') == LEVEL_INSIGHT][:10]
        parts = ["Update Jarvis's WORLD MODEL of the user.", "",
                 f"CURRENT MODEL:\n{wm or '(none yet)'}"]
        if recent_l2:
            parts.append("\nRECENT THEMES (from long-term abstraction):\n"
                         + "\n".join(f"- {t[:400]}" for t in recent_l2))
        if recent_ins:
            parts.append("\nRECENT INSIGHTS:\n"
                         + "\n".join(f"- {t[:300]}" for t in recent_ins))
        parts.append(
            "\nProduce the UPDATED model in under 300 words: who the "
            "user is, what they are working toward, their rhythms and "
            "routines, constraints, standing preferences, and how "
            "Jarvis can serve them best. Merge with the current model — "
            "do not lose stable facts. Output ONLY the model text.")
        new_wm = None
        try:
            new_wm = brain.complete("\n".join(parts), agent='archivist',
                                    timeout=60, max_tokens=700)
        except Exception as e:
            logger.debug("world-model refresh failed: %s", e)
        if not new_wm or len(new_wm.strip()) < 40:
            return 0
        with self._lock:
            self.world_model = new_wm.strip()[:2500]
            self.world_model_updated = _now_iso()
        self._save()
        return 1

    def _prune(self):
        """
        Keep the hierarchy bounded; drop oldest *folded* material first.

        A folded note (an event that was summarised, a summary folded
        into an abstraction) has already handed its content up to a
        parent note, so when its level outgrows its cap it is the first
        candidate for removal.  Fresh (unfolded) notes are only shed
        when a level still exceeds its cap after every folded note there
        is gone — boundedness wins over keeping raw observations, but a
        consumed summary is always cheaper to drop than the live event
        that still awaits folding.
        """
        removed = []
        with self._lock:
            for level, cap in ((LEVEL_EVENT, _max_l0()),
                               (LEVEL_SUMMARY, self.MAX_L1),
                               (LEVEL_ABSTRACTION, self.MAX_L2)):
                at_level = [n for n in self._notes
                            if n.get('level') == level]
                excess = len(at_level) - cap
                if excess <= 0:
                    continue
                # Folded (consumed) notes first, each tier oldest first;
                # the newest `cap` notes at the level survive.
                at_level.sort(key=lambda x: (not bool(x.get('folded')),
                                             x.get('created', '')))
                doomed_ids = {n['id'] for n in at_level[:excess]}
                removed += doomed_ids
                self._notes = [n for n in self._notes
                               if n.get('id') not in doomed_ids]
            insights = [n for n in self._notes
                        if n.get('level') == LEVEL_INSIGHT]
            if len(insights) > self.MAX_INSIGHTS:
                # Superseded (stale) insights go first, then lowest
                # importance, then oldest.
                insights.sort(key=lambda x: (not bool(x.get('superseded_by')),
                                             x.get('importance', 5),
                                             x.get('created', '')))
                doomed = {n['id'] for n in
                          insights[:len(insights) - self.MAX_INSIGHTS]}
                removed += doomed
                self._notes = [n for n in self._notes
                               if n.get('id') not in doomed]
        if removed:
            self._save()
            self._vector_delete(removed)
        return len(removed)

    # ------------------------------------------------------------------ #
    # Recursive retrieval
    # ------------------------------------------------------------------ #

    def _temporal_mult(self, note):
        """
        Generative-Agents-style retrieval multiplier: recency decay ×
        importance × reuse.  Applied ON TOP of relevance so a month-old
        critical fact still outranks a fresh pleasantry — and the decay
        floor (0.35) means durable knowledge never vanishes.
        """
        try:
            age_days = max(
                0.0, (time.time() - _parse_iso(note.get('created', '')))
                / 86400.0)
            recency = 0.35 + 0.65 * math.exp(-age_days / _decay_tau_days())
            imp = 0.6 + 0.08 * float(note.get('importance', 5))
            reuse = min(1.25, 1.0 + 0.05 * int(note.get('access_count', 0)))
            return (recency ** _w_rec()) * (imp ** _w_imp()) * reuse
        except Exception:
            return 1.0

    def _rank_notes(self, notes, query):
        """[(score, note)] — temporal-weighted, per-level quotas."""
        q = (query or '').lower().strip()
        if not q:
            return []
        # Superseded insights are stale by definition — never recalled.
        notes = [n for n in notes if not n.get('superseded_by')]
        quotas = {LEVEL_INSIGHT: 4, LEVEL_ABSTRACTION: 3,
                  LEVEL_SUMMARY: 4, LEVEL_EVENT: 5}
        hits = {}
        idx = self._index()
        if idx is not None:
            for lvl, k in quotas.items():
                for rank, nid in enumerate(idx.query_ids(q, level=lvl,
                                                         n=k * 2)):
                    hits[nid] = max(hits.get(nid, 0.0),
                                    0.9 - rank * 0.08)
        if not hits:
            for n in notes:
                s = _token_overlap(
                    q, f"{n.get('text', '')} "
                       f"{' '.join(n.get('tags', []))}")
                if s > 0:
                    hits[n['id']] = s
        # Subject boost: query names a tracked entity → notes about it
        # rank higher (the entity graph steering retrieval).
        subject = self.entities.match(q)
        subject_low = subject.lower() if subject else ''
        by_id = {n['id']: n for n in notes}
        scored = []
        for i, rel in hits.items():
            n = by_id.get(i)
            if n is None:
                continue
            score = (rel ** _w_rel()) * self._temporal_mult(n)
            if subject_low and subject_low in \
                    str(n.get('text', '')).lower():
                score *= 1.2
            scored.append((score, n))
        scored.sort(key=lambda x: x[0], reverse=True)
        out, used = [], {lvl: 0 for lvl in quotas}
        for s, n in scored:
            lvl = n.get('level', LEVEL_EVENT)
            if used.get(lvl, 0) >= quotas.get(lvl, 4):
                continue
            used[lvl] = used.get(lvl, 0) + 1
            out.append((s, n))
        return out

    def _parents_of(self, note, notes):
        nid = note.get('id')
        return [p['id'] for p in notes
                if nid in (p.get('children') or [])]

    def recall(self, query, max_chars=None):
        """
        Query-time recursive retrieval across every level, with parent
        expansion (an event hit summons its session summary).  Returns a
        budgeted text block ('' when nothing relevant exists).
        """
        if not enabled():
            return ""
        budget = max_chars or _max_chars()
        with self._lock:
            wm = self.world_model or ''
            notes = [n for n in self._notes if not n.get('superseded_by')]
        if not wm and not notes:
            return ""

        ranked = self._rank_notes(notes, query)
        by_id = {n['id']: n for n in notes}
        # Parent expansion: pull in summaries/themes that contain hits
        extra_ids = set()
        ranked_ids = {n['id'] for _s, n in ranked}
        for _s, n in ranked:
            if n.get('level') in (LEVEL_EVENT, LEVEL_SUMMARY):
                for pid in self._parents_of(n, notes):
                    if pid not in ranked_ids:
                        extra_ids.add(pid)
        extra = [(0.55, by_id[pid]) for pid in extra_ids if pid in by_id]

        def _texts(items, cap):
            # *items* are note dicts (already unpacked from (score, note))
            return [str(n.get('text', ''))[:cap] for n in items]

        sections = []
        if wm:
            sections.append(("WORLD MODEL — stable picture of the user",
                             [wm[:700]]))
        insights = [n for _s, n in ranked if n.get('level') == LEVEL_INSIGHT]
        if insights:
            sections.append(("PLAYBOOK — distilled insights",
                             _texts(insights, 300)))
        l2 = [n for _s, n in ranked
              if n.get('level') == LEVEL_ABSTRACTION] + \
             [n for _s, n in extra if n.get('level') == LEVEL_ABSTRACTION]
        if l2:
            sections.append(("LONG-TERM THEMES", _texts(l2, 340)))
        l1 = [n for _s, n in ranked if n.get('level') == LEVEL_SUMMARY] + \
             [n for _s, n in extra if n.get('level') == LEVEL_SUMMARY]
        if l1:
            sections.append(("SESSION MEMORIES", _texts(l1, 320)))
        l0 = [n for _s, n in ranked if n.get('level') == LEVEL_EVENT]
        if l0:
            sections.append(("RELEVANT EVENTS", _texts(l0, 260)))

        out, remaining = [], budget
        for title, entries in sections:
            if remaining <= 80:
                break
            header = title + ":"
            lines, took = [], 0
            for text in entries:
                piece = f"  • {text}"
                if remaining - len(piece) - len(header) < 0:
                    break
                lines.append(piece)
                remaining -= len(piece) + 1
                took += 1
            if lines:
                out.append(header)
                out.extend(lines)
                remaining -= len(header) + 1
            if not took:
                continue
        return "\n".join(out)

    def recall_block(self, query, max_chars=None):
        """
        System-prompt-ready block ('' when nothing to inject).  The
        always-anchored world model alone is not "a match" — the block
        is only emitted when the query actually surfaces notes.
        """
        body = self.recall(query, max_chars=max_chars)
        if not body:
            return ""
        with self._lock:
            notes = [n for n in self._notes if not n.get('superseded_by')]
        if not self._rank_notes(notes, query):
            return ""
        return ("RLM RECURSIVE MEMORY (recalled across abstraction "
                f"levels):\n{body}")

    def recall_deep(self, query):
        """Richer recall for the agent's ``recall_deep`` tool."""
        return self.recall_block(query, max_chars=_deep_chars())

    # ------------------------------------------------------------------ #
    # Entity-graph retrieval (subject queries + continuity)
    # ------------------------------------------------------------------ #

    def recall_about(self, entity, max_chars=1400):
        """
        Everything the memory holds about ONE subject (person, project,
        topic): the graph query path behind "what do I know about X".
        Value-ranked (importance × recency); '' when the subject is
        unknown.
        """
        if not enabled():
            return ""
        name = self.entities.match(entity) or str(entity or '').strip()
        if not name:
            return ""
        low = name.lower()
        with self._lock:
            ent = self.entities.about(name)
            note_ids = set(ent.get('note_ids') or [])
            notes = [n for n in self._notes
                     if not n.get('superseded_by')
                     and (low in str(n.get('text', '')).lower()
                          or n.get('id') in note_ids)]
        if not notes and not ent:
            return ""
        notes.sort(key=lambda n: (self._temporal_mult(n),
                                  n.get('created', '')), reverse=True)

        sections = []
        if ent:
            last = str(ent.get('last_seen') or '')[:10]
            related = self.entities.related(name, 3)
            head = (f"{name} — {ent.get('count', 0)} mention(s)"
                    + (f", last {last}" if last else "")
                    + (f"; relates to {', '.join(related)}" if related
                       else ""))
            sections.append(("SUBJECT", [head]))
        for lvl, title, cap in ((LEVEL_INSIGHT, "PLAYBOOK", 300),
                                (LEVEL_ABSTRACTION, "LONG-TERM THEMES", 340),
                                (LEVEL_SUMMARY, "SESSION MEMORIES", 320),
                                (LEVEL_EVENT, "EVENTS", 260)):
            texts = [str(n.get('text', ''))[:cap]
                     for n in notes if n.get('level') == lvl][:6]
            if texts:
                sections.append((title, texts))

        budget = max_chars
        out = []
        for title, entries in sections:
            if budget <= 80:
                break
            header = title + ":"
            lines = []
            for text in entries:
                piece = f"  • {text}"
                if budget - len(piece) < 0:
                    break
                lines.append(piece)
                budget -= len(piece) + 1
            if not lines:
                continue
            out.append(header)
            out.extend(lines)
            budget -= len(header) + 1
        if not out:
            return ""
        return f"MEMORY ABOUT {name}:\n" + "\n".join(out)

    def entity_digest(self, max_chars=1200):
        """
        Compact digest of tracked subjects (people, projects, topics)
        for prompts and dashboards.  '' when the graph is empty.
        """
        if not enabled() or not _entities_enabled():
            return ""
        top = self.entities.top(12)
        if not top:
            return ""
        lines = ["KNOWN SUBJECTS (people, projects, topics in memory):"]
        for display, count, last in top:
            rel = self.entities.related(display, 3)
            lines.append(f"- {display} — {count} mention(s)"
                         + (f", last {str(last)[:10]}" if last else "")
                         + (f" (relates to {', '.join(rel)})" if rel
                            else ""))
        return "\n".join(lines)[:max(200, max_chars)]

    def continuity_block(self, max_chars=900):
        """
        Cross-session continuity: after an idle gap
        (``JARVIS_RLM_CONTINUITY_GAP_S``, default 30 min) the next turn
        is a NEW session — this block re-injects where the last one left
        off (latest session summary + final exchanges) so the
        conversation PICKS UP instead of restarting from zero.
        '' while the session is still warm (nothing to bridge).
        """
        if not enabled():
            return ""
        with self._lock:
            notes = [n for n in self._notes if not n.get('superseded_by')]
        if not notes:
            return ""
        last_ts = max((_parse_iso(n.get('created', '')) for n in notes),
                      default=0.0)
        if last_ts <= 0 or (time.time() - last_ts) < _continuity_gap_s():
            return ""
        summaries = [n for n in notes if n.get('level') == LEVEL_SUMMARY]
        latest = (max(summaries, key=lambda n: n.get('created', ''))
                  if summaries else None)
        events = sorted((n for n in notes
                         if n.get('level') == LEVEL_EVENT),
                        key=lambda n: n.get('created', ''))
        tail = events[-3:]
        lines = []
        if latest is not None:
            lines.append("LAST SESSION SUMMARY:\n  "
                         + str(latest.get('text', ''))[:420])
        if tail:
            lines.append("LAST EXCHANGES:")
            for n in tail:
                lines.append(f"  • {str(n.get('text', ''))[:200]}")
        if not lines:
            return ""
        gap_h = (time.time() - last_ts) / 3600.0
        head = (f"SESSION CONTINUITY — you and the user last spoke "
                f"{gap_h:.1f}h ago; pick up where you left off:")
        return (head + "\n" + "\n".join(lines))[:max(200, max_chars)]

    # ------------------------------------------------------------------ #
    # Maintenance daemon + introspection
    # ------------------------------------------------------------------ #

    def maintenance_tick(self, brain):
        """Backstop pass for the daemon thread (consolidate + reflect)."""
        out = {'consolidated': self.consolidate(brain)}
        with self._lock:
            backlog = len(self._unreflected)
        if backlog >= _reflect_every():
            out['reflected'] = len(self.reflect(brain))
        return out

    def start_maintenance(self, brain=None):
        """Start the periodic 'sleep' daemon (idempotent)."""
        if self._maint_thread is not None and \
                self._maint_thread.is_alive():
            return False

        def _loop():
            while True:
                time.sleep(_tick_s())
                try:
                    from utils.hibernation import get_manager
                    if getattr(get_manager(), 'hibernating', False):
                        continue          # sleep while the host sleeps
                except Exception:
                    pass
                try:
                    self.maintenance_tick(brain)
                except Exception as e:
                    logger.debug("RLM maintenance tick failed: %s", e)

        self._maint_thread = threading.Thread(
            target=_loop, daemon=True, name="rlm-maintenance")
        self._maint_thread.start()
        logger.info("RLM maintenance daemon online (%.0fs cadence)",
                    _tick_s())
        return True

    def stats(self):
        with self._lock:
            counts = {}
            for n in self._notes:
                counts[n.get('level', 0)] = counts.get(n.get('level', 0),
                                                       0) + 1
            return {
                'enabled': enabled(),
                'notes': len(self._notes),
                'by_level': {(_LEVEL_NAMES.get(k) or f'L{k}'): v
                             for k, v in sorted(counts.items())},
                'pending_events': sum(
                    1 for n in self._notes
                    if n.get('level') == LEVEL_EVENT
                    and not n.get('folded')),
                'unreflected': len(self._unreflected),
                'superseded': sum(
                    1 for n in self._notes if n.get('superseded_by')),
                'entities': len(self.entities),
                'decay_days': _decay_tau_days(),
                'continuity_gap_s': _continuity_gap_s(),
                'world_model_chars': len(self.world_model or ''),
                'world_model_updated': self.world_model_updated,
                'last_consolidation': self.last_consolidation,
                'vector': self._index() is not None,
            }

    def dashboard_snapshot(self, limit=30):
        """JSON snapshot for /api/rlm and diagnostics."""
        snap = self.stats()
        with self._lock:
            notes = sorted(self._notes,
                           key=lambda x: x.get('created', ''),
                           reverse=True)[:limit]
        snap['world_model'] = self.world_model
        snap['entities'] = [
            {'name': d, 'mentions': c, 'last_seen': l}
            for d, c, l in self.entities.top(10)]
        snap['recent'] = [
            {'id': n['id'], 'level': n.get('level'),
             'kind': n.get('kind'), 'text': str(n.get('text', ''))[:220],
             'created': n.get('created', ''),
             'importance': n.get('importance', 5),
             'superseded': bool(n.get('superseded_by'))}
            for n in notes]
        return snap

    def rebuild_vector_index(self):
        """Rebuild the semantic index from the JSON source of truth."""
        idx = self._index()
        if idx is None:
            return False
        with self._lock:
            snapshot = list(self._notes)
        try:
            existing = set()
            res = idx.collection.get()
            existing = {str(i) for i in (res.get('ids') or [])}
            want = {f"rlm_{n['id']}" for n in snapshot}
            stale = existing - want
            if stale:
                idx.collection.delete(ids=list(stale))
            for n in snapshot:
                if f"rlm_{n['id']}" in existing:
                    continue
                idx.upsert(n['id'], n.get('text', ''),
                           {"level": int(n.get('level', 0)),
                            "kind": n.get('kind', ''),
                            "created": n.get('created', '')})
            logger.info("RLM vector index rebuilt (%d notes)",
                        len(snapshot))
            return True
        except Exception as e:
            logger.warning("RLM index rebuild failed: %s", e)
            return False


# --------------------------------------------------------------------- #
# Singleton
# --------------------------------------------------------------------- #

_singleton = None
_singleton_lock = threading.Lock()


def get_rlm(data_file=None):
    """Process-wide RecursiveMemory (default data file)."""
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = RecursiveMemory(data_file=data_file)
        return _singleton


def _reset_singleton():
    """Test helper — drop the singleton so a fresh one is built."""
    global _singleton
    with _singleton_lock:
        _singleton = None
