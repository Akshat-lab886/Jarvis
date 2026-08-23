"""
Jarvis Episodic & Adaptive Memory Engine

A persistent, structured memory system that:
- Stores personal preferences, ongoing health goals, context, and relationship data
- Uses categories and tags for intelligent retrieval
- Supports temporal decay (older memories rank lower unless reinforced)
- Provides search by keyword, category, recency, or relevance
- Automatically captures new memories from conversations
- Survives restarts via JSON persistence

Categories:
  - preference  : food, music, color, habits, likes/dislikes
  - health      : goals, medications, allergies, routines
  - context     : ongoing projects, life events, random facts
  - relationship: people, their details, important dates
  - goal        : short-term and long-term goals
  - fact        : personal facts the user shares
  - routine     : daily/weekly routines the user follows
"""

import os
import json
import threading
import datetime
import re
from collections import defaultdict


# ---- Category constants ----
CATEGORIES = [
    'preference', 'health', 'context', 'relationship',
    'goal', 'fact', 'routine', 'note'
]

# Decay half-life in days: memories older than this lose relevance
DECAY_HALF_LIFE_DAYS = 90

# Embedding model shared with the knowledge vault (Librarian)
_VECTOR_MODEL = "all-MiniLM-L6-v2"

# ---- Shared-per-directory vector index cache ------------------------- #
# Keyed by persist-dir abs path so multiple EpisodicMemory instances in
# one process share a single collection handle (and model load), while
# tests can isolate into temp dirs.
_INDEX_CACHE = {}


class _EpisodicVectorIndex:
    """ChromaDB-backed semantic index over episodic memories."""

    def __init__(self, persist_dir):
        import chromadb
        from chromadb.utils import embedding_functions
        os.makedirs(persist_dir, exist_ok=True)
        self.client = chromadb.PersistentClient(path=persist_dir)
        self.embedding_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name=_VECTOR_MODEL
        )
        self.collection = self.client.get_or_create_collection(
            name="jarvis_episodic",
            embedding_function=self.embedding_fn,
        )

    def upsert(self, memory_id, text, metadata):
        try:
            self.collection.upsert(
                documents=[text or " "],
                ids=[f"epi_{memory_id}"],
                metadatas=[metadata or {}],
            )
        except Exception as e:
            print(f"EpisodicMemory: vector upsert failed: {e}")

    def delete_ids(self, memory_ids):
        if not memory_ids:
            return
        try:
            self.collection.delete(ids=[f"epi_{int(i)}" for i in memory_ids])
        except Exception as e:
            print(f"EpisodicMemory: vector delete failed: {e}")

    def query_ids(self, text, n=10):
        """Return ranked integer memory IDs most similar to *text*."""
        if not text or not text.strip():
            return []
        try:
            res = self.collection.query(query_texts=[text], n_results=n)
            out = []
            for raw in (res.get('ids') or [[]])[0]:
                m = re.match(r'epi_(\d+)$', str(raw))
                if m:
                    out.append(int(m.group(1)))
            return out
        except Exception as e:
            print(f"EpisodicMemory: vector query failed: {e}")
            return []


_INDEX_BUILD_TIMEOUT = 30   # seconds; native libs can hang on broken setups


def _get_index_for(vector_dir):
    """
    Get-or-build the shared index for *vector_dir*.

    Returns None when unavailable (missing deps, model load failure, or
    a HUNG native library call — guarded via watchdog thread since a
    hang raises nothing).  Set JARVIS_DISABLE_VECTOR=1 to force
    keyword-only mode.
    """
    key = os.path.abspath(vector_dir)
    if key in _INDEX_CACHE:
        return _INDEX_CACHE[key]

    if os.getenv('JARVIS_DISABLE_VECTOR') == '1':
        _INDEX_CACHE[key] = None
        return None

    outcome = {}

    def _build():
        try:
            outcome['index'] = _EpisodicVectorIndex(key)
        except Exception as e:
            outcome['error'] = str(e)

    builder = threading.Thread(target=_build, daemon=True,
                               name="episodic-vector-init")
    builder.start()
    builder.join(timeout=_INDEX_BUILD_TIMEOUT)

    if builder.is_alive():
        print("EpisodicMemory: vector index build TIMED OUT "
              f"(>{_INDEX_BUILD_TIMEOUT}s) — keyword-only mode")
        index = None
    elif 'error' in outcome:
        print(f"EpisodicMemory: vector index unavailable "
              f"({outcome['error']}) — keyword-only mode")
        index = None
    else:
        index = outcome.get('index')
        if index is not None:
            print(f"EpisodicMemory: semantic index online ({_VECTOR_MODEL})")

    _INDEX_CACHE[key] = index
    return index


class EpisodicMemory:
    """
    Thread-safe structured memory store.

    Each memory entry:
    {
        "id": int,
        "category": str,
        "text": str,
        "tags": [str],
        "created": str (ISO),
        "last_accessed": str (ISO),
        "access_count": int,
        "source": str ("conversation" | "system" | "passive"),
        "importance": int (1-10, default 5),
        "metadata": dict (freeform)
    }
    """

    def __init__(self, filename='episodic_memory.json', use_vector=True,
                 vector_dir=None):
        self.base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.file_path = os.path.join(self.base_dir, filename)
        self._lock = threading.RLock()
        self._memories = []
        self._next_id = 1
        # Semantic layer (JSON stays the source of truth; ChromaDB is a
        # derived, rebuildable index).  Built lazily on first use so a
        # missing/failed model never blocks core memory operations.
        self.use_vector = use_vector
        self._vector_dir = vector_dir or os.path.join(self.base_dir,
                                                      'knowledge_vault')
        self._load()

    def _index(self):
        """Return the shared vector index for this store (or None)."""
        if not getattr(self, 'use_vector', True):
            return None
        return _get_index_for(self._vector_dir)

    def _index_entry(self, entry):
        return {
            "category": entry.get('category', 'fact'),
            "importance": int(entry.get('importance', 5)),
            "created": entry.get('created', ''),
        }

    def _vector_sync_new(self, entries):
        idx = self._index()
        if not idx:
            return
        for e in entries:
            idx.upsert(e.get('id'), e.get('text', ''), self._index_entry(e))

    def _vector_delete_ids(self, ids):
        idx = self._index()
        if idx:
            idx.delete_ids(ids)

    def rebuild_vector_index(self):
        """Rebuild the semantic index from the JSON source of truth."""
        idx = self._index()
        if not idx:
            print("EpisodicMemory: cannot rebuild — index unavailable.")
            return False
        with self._lock:
            snapshot = list(self._memories)
        try:
            existing = set()
            res = idx.collection.get()
            existing = {str(i) for i in (res.get('ids') or [])}
            want = {f"epi_{m['id']}" for m in snapshot}
            stale = existing - want
            if stale:
                idx.collection.delete(ids=list(stale))
            for m in snapshot:
                if f"epi_{m['id']}" in existing:
                    continue
                idx.upsert(m['id'], m.get('text', ''), self._index_entry(m))
            print(f"EpisodicMemory: vector index rebuilt "
                  f"({len(snapshot)} memories)")
            return True
        except Exception as e:
            print(f"EpisodicMemory: rebuild failed: {e}")
            return False

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    def _load(self):
        self._memories = []
        self._next_id = 1
        try:
            if os.path.exists(self.file_path):
                with open(self.file_path, 'r') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._memories = data.get('memories', [])
                    self._next_id = data.get('next_id', len(self._memories) + 1)
                elif isinstance(data, list):
                    self._memories = data
                    self._next_id = len(self._memories) + 1
        except Exception as e:
            print(f"EpisodicMemory: Failed to load: {e}")

    def _save(self):
        try:
            with self._lock:
                data = {
                    "next_id": self._next_id,
                    "memories": self._memories
                }
            with open(self.file_path, 'w') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"EpisodicMemory: Failed to save: {e}")

    # ------------------------------------------------------------------ #
    # Core CRUD
    # ------------------------------------------------------------------ #
    def remember(self, text, category='fact', tags=None, importance=5,
                 source='conversation', metadata=None):
        """
        Store a new memory.
        Returns the memory entry dict.
        """
        text = (text or '').strip()
        if not text:
            return None

        # Dedup: check if an almost identical memory exists
        with self._lock:
            for existing in self._memories:
                if (existing.get('category') == category and
                    self._similarity(existing.get('text', ''), text) > 0.85):
                    # Reinforce existing instead of duplicating
                    existing['access_count'] = existing.get('access_count', 0) + 1
                    existing['last_accessed'] = datetime.datetime.now().isoformat()
                    # If new one is more important, upgrade
                    if importance > existing.get('importance', 5):
                        existing['importance'] = importance
                    # Merge tags
                    if tags:
                        old_tags = set(existing.get('tags', []))
                        old_tags.update(tags)
                        existing['tags'] = sorted(old_tags)
                    self._save()
                    # Keep vector metadata fresh (importance/tags changed)
                    self._vector_sync_new([existing])
                    return existing

        now = datetime.datetime.now().isoformat()
        entry = {
            "id": self._next_id,
            "category": category,
            "text": text,
            "tags": tags or [],
            "created": now,
            "last_accessed": now,
            "access_count": 0,
            "source": source,
            "importance": max(1, min(10, importance)),
            "metadata": metadata or {}
        }

        with self._lock:
            self._memories.append(entry)
            self._next_id += 1
        self._save()
        self._vector_sync_new([entry])
        print(f"EpisodicMemory: Remembered [{category}] {text[:80]}")
        return entry

    def forget(self, memory_id=None, keyword=None, category=None):
        """
        Remove memories by id, keyword match, or category.
        Returns count of removed memories.  Also removes them from the
        semantic index so forgotten memories can't resurface.
        """
        removed = 0
        removed_ids = []
        with self._lock:
            before = len(self._memories)
            if memory_id is not None:
                removed_ids = [m.get('id') for m in self._memories
                               if m.get('id') == memory_id]
                self._memories = [m for m in self._memories if m.get('id') != memory_id]
            elif keyword:
                kw = keyword.lower()
                keep = []
                for m in self._memories:
                    hit = (kw in m.get('text', '').lower() or
                           any(kw in t for t in m.get('tags', [])))
                    if hit:
                        removed_ids.append(m.get('id'))
                    else:
                        keep.append(m)
                self._memories = keep
            elif category:
                removed_ids = [m.get('id') for m in self._memories
                               if m.get('category') == category]
                self._memories = [m for m in self._memories if m.get('category') != category]
            removed = before - len(self._memories)
        if removed:
            self._save()
            self._vector_delete_ids(removed_ids)
        return removed

    def get(self, memory_id):
        """Get a single memory by ID and update access stats."""
        with self._lock:
            for m in self._memories:
                if m.get('id') == memory_id:
                    m['last_accessed'] = datetime.datetime.now().isoformat()
                    m['access_count'] = m.get('access_count', 0) + 1
                    self._save()
                    return dict(m)
        return None

    # ------------------------------------------------------------------ #
    # Search & Retrieval
    # ------------------------------------------------------------------ #
    def _recency_score(self, memory, now=None):
        """Exponential recency decay (1.0 fresh → ~0 at half-life*many)."""
        now = now or datetime.datetime.now()
        try:
            created = datetime.datetime.fromisoformat(
                memory.get('created', now.isoformat()))
            age_days = (now - created).total_seconds() / 86400
            return 0.5 ** (age_days / DECAY_HALF_LIFE_DAYS)
        except Exception:
            return 0.5

    def semantic_search(self, query, limit=10, min_importance=1):
        """
        Embedding-based recall over episodic memories.

        Falls back to fuzzy_search when the vector layer is unavailable.
        Blends: semantic rank (65%) + recency (20%) + importance (15%).
        """
        idx = self._index()
        if idx is None:
            return [m for m in self.fuzzy_search(query, limit=limit)
                    if m.get('importance', 5) >= min_importance]

        id_hits = idx.query_ids(query, n=max(limit * 4, limit))
        if not id_hits:
            return []

        now = datetime.datetime.now()
        with self._lock:
            by_id = {m.get('id'): m for m in self._memories}

        scored = []
        for rank, mid in enumerate(id_hits):
            m = by_id.get(mid)
            if not m or m.get('importance', 5) < min_importance:
                continue
            rank_score = 1.0 / (rank + 1)
            combined = (rank_score * 0.65
                        + self._recency_score(m, now) * 0.20
                        + (m.get('importance', 5) / 10.0) * 0.15)
            scored.append((combined, m))

        scored.sort(key=lambda x: x[0], reverse=True)
        seen, out = set(), []
        for _, m in scored:
            if m['id'] in seen:
                continue
            seen.add(m['id'])
            out.append(m)
            if len(out) >= limit:
                break
        return out

    def search(self, query, category=None, tags=None, limit=10,
               min_importance=1):
        """
        Hybrid memory recall: keyword relevance ⊕ semantic similarity.

        Keyword results are normalized and fused with embedding hits
        (60/40 blend); memories found ONLY semantically still surface —
        that's the point of a vector layer.
        """
        query_lower = (query or '').lower()
        now = datetime.datetime.now()

        results = []
        with self._lock:
            for m in self._memories:
                if m.get('importance', 5) < min_importance:
                    continue
                if category and m.get('category') != category:
                    continue
                if tags:
                    m_tags = set(m.get('tags', []))
                    if not set(tags).intersection(m_tags):
                        continue

                # Score: text relevance + recency + importance
                text_score = self._text_relevance(query_lower, m)
                if text_score == 0:
                    continue

                recency_score = self._recency_score(m, now)
                importance_score = m.get('importance', 5) / 10.0
                combined = ((text_score * 0.6) + (recency_score * 0.2)
                            + (importance_score * 0.2))

                results.append((combined, m))

        # ---- Fuse with semantic hits when the vector layer is up ---- #
        idx = self._index() if query_lower else None
        if idx is not None:
            sem_ids = idx.query_ids(query_lower, n=max(limit * 3, 15))
            if sem_ids:
                with self._lock:
                    by_id = {m.get('id'): m for m in self._memories}
                max_kw = max((s for s, _m in results), default=0.0) or 1.0
                fused = {m['id']: (score / max_kw, m)
                         for score, m in results}
                for rank, mid in enumerate(sem_ids):
                    m = by_id.get(mid)
                    if not m:
                        continue
                    if m.get('importance', 5) < min_importance:
                        continue
                    if category and m.get('category') != category:
                        continue
                    if tags and not set(tags) & set(m.get('tags', [])):
                        continue
                    sem_score = 0.9 / (rank + 1)   # top hit ≈ 0.9
                    if m['id'] in fused:
                        kw, mm = fused[m['id']]
                        fused[m['id']] = (kw * 0.6 + sem_score * 0.4, mm)
                    else:
                        # Semantic-only discovery — recall beyond keywords
                        fused[m['id']] = (sem_score, m)
                results = sorted(fused.values(),
                                 key=lambda x: x[0], reverse=True)

        return [m for _, m in results[:limit]]

    def get_by_category(self, category, limit=50):
        """Get all memories of a given category, most recent first."""
        with self._lock:
            items = [m for m in self._memories if m.get('category') == category]
        items.sort(key=lambda x: x.get('created', ''), reverse=True)
        return items[:limit]

    def get_recent(self, limit=20):
        """Get most recently created memories."""
        with self._lock:
            items = list(self._memories)
        items.sort(key=lambda x: x.get('created', ''), reverse=True)
        return items[:limit]

    def get_all(self):
        """Get all memories (snapshot)."""
        with self._lock:
            return list(self._memories)

    def count(self):
        with self._lock:
            return len(self._memories)

    # ------------------------------------------------------------------ #
    # Contextual Injection (for Brain system prompt)
    # ------------------------------------------------------------------ #
    def get_context_for_prompt(self, query=None, max_tokens=2000):
        """
        Build a context block for the Brain's system prompt.
        Returns a formatted string with the most relevant memories.
        If query is None, returns a broad overview (for morning briefing, etc.)
        """
        lines = []

        if query:
            # Search for relevant memories
            results = self.search(query, limit=15)
            if results:
                lines.append(f"Relevant memories for '{query}':")
                for m in results:
                    tag_str = f" [{', '.join(m.get('tags', []))}]" if m.get('tags') else ""
                    lines.append(f"  - [{m['category']}]{tag_str} {m['text']}")
        else:
            # Broad overview: recent + high-importance memories
            high_imp = [m for m in self.get_all() if m.get('importance', 5) >= 7]
            recent = self.get_recent(10)
            seen_ids = set()

            lines.append("USER MEMORY OVERVIEW:")
            lines.append("High-importance facts:")
            for m in sorted(high_imp, key=lambda x: x.get('importance', 0), reverse=True)[:10]:
                if m['id'] not in seen_ids:
                    lines.append(f"  - [{m['category']}] {m['text']}")
                    seen_ids.add(m['id'])

            lines.append("Recent memories:")
            for m in recent[:8]:
                if m['id'] not in seen_ids:
                    lines.append(f"  - [{m['category']}] {m['text']}")
                    seen_ids.add(m['id'])

            # Relationships
            relationships = self.get_by_category('relationship', limit=10)
            if relationships:
                lines.append("People the user cares about:")
                for r in relationships:
                    lines.append(f"  - {r['text']}")

            # Goals
            goals = self.get_by_category('goal', limit=5)
            if goals:
                lines.append("Active goals:")
                for g in goals:
                    lines.append(f"  - {g['text']}")

            # Health
            health = self.get_by_category('health', limit=5)
            if health:
                lines.append("Health context:")
                for h in health:
                    lines.append(f"  - {h['text']}")

        context = "\n".join(lines)

        # Truncate if too long
        if len(context) > max_tokens * 4:  # rough chars-per-token estimate
            context = context[:max_tokens * 4] + "\n[...truncated...]"

        return context

    # ------------------------------------------------------------------ #
    # Auto-capture: Extract memories from conversation text
    # ------------------------------------------------------------------ #
    def auto_capture(self, user_text, response_text=""):
        """
        Analyze user text for new memories to capture.
        Looks for patterns like:
        - "my favorite X is Y"
        - "I love/hate X"
        - "remember that..."
        - "my friend/family X"
        - "I'm trying to..."
        - Health-related statements
        Returns list of captured memories.
        """
        captured = []
        text = user_text.strip()

        # ---- Preference patterns ----
        pref_patterns = [
            (r"my\s+(?:favorite|favourite)\s+(?:\w+\s+)?(?:is|are)\s+(.+)", "preference"),
            (r"i\s+(?:really\s+)?(?:love|like|prefer|enjoy)\s+(.+)", "preference"),
            (r"i\s+(?:really\s+)?(?:hate|dislike|can't stand|despise)\s+(.+)", "preference"),
            (r"i\s+(?:usually|always|often)\s+(.+)", "routine"),
            (r"(?:don't|do not)\s+(?:like|want)\s+(.+)", "preference"),
        ]

        for pattern, category in pref_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                mem = self.remember(
                    text=text,
                    category=category,
                    tags=[category, 'auto-captured'],
                    source='conversation',
                    importance=6
                )
                if mem:
                    captured.append(mem)
                break

        # ---- Relationship patterns ----
        rel_patterns = [
            r"(?:my|meet|call)\s+(friend|mom|dad|mother|father|brother|sister|"
            r"wife|husband|partner|girlfriend|boyfriend|colleague|boss|"
            r"teacher|doctor|dentist)\s+(\w+)",
            r"(\w+)\s+(?:is\s+my\s+)?(friend|mom|dad|mother|father|brother|sister|"
            r"wife|husband|partner|girlfriend|boyfriend)",
        ]

        for pattern in rel_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                groups = match.groups()
                # Normalize order: name first, then relationship
                if groups[0].lower() in ['my', 'meet', 'call']:
                    name = groups[1] if len(groups) > 1 else groups[0]
                    rel_type = groups[2] if len(groups) > 2 else groups[1]
                else:
                    name = groups[0]
                    rel_type = groups[1]

                detail = text
                mem = self.remember(
                    text=f"{name} — {rel_type}. {detail}",
                    category='relationship',
                    tags=['relationship', rel_type.lower(), name.lower()],
                    source='conversation',
                    importance=8
                )
                if mem:
                    captured.append(mem)
                break

        # ---- Relationship detail patterns (peanut allergy, etc.) ----
        detail_patterns = [
            (r"(\w+)(?:'s?)?\s+(?:kid|child|son|daughter)?\s*(?:is\s+)?allergic\s+to\s+(.+)",
             "relationship", 8),
            (r"(\w+)(?:'s?)?\s+(?:favorite|favourite)\s+(?:\w+\s+)?(?:is|are)\s+(.+)",
             "relationship", 7),
            (r"(\w+)\s+(?:loves?|likes?|enjoys?)\s+(.+)",
             "relationship", 7),
            (r"(\w+)(?:'s?)?\s+birthday\s+(?:is\s+)?(.+)",
             "relationship", 9),
        ]

        for pattern, category, importance in detail_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                mem = self.remember(
                    text=text,
                    category=category,
                    tags=['relationship-detail', 'auto-captured'],
                    source='conversation',
                    importance=importance
                )
                if mem:
                    captured.append(mem)
                break

        # ---- Health patterns ----
        health_patterns = [
            r"(?:i(?:'m| am)\s+)?(?:trying\s+to\s+)?(?:lose\s+weight|exercise|"
            r"eat\s+healthy|go\s+(?:to\s+the\s+)?gym|meditate|run|jog|walk|"
            r"quit\s+smoking|drink\s+more\s+water|sleep\s+better)",
            r"(?:my|the)\s+(?:health\s+)?goal\s+(?:is\s+)?(.+)",
            r"i(?:'m| am)\s+(?:on\s+)?(?:a\s+)?(?:diet|fast|keto|vegan|vegetarian)",
            r"i\s+(?:need|want)\s+to\s+(?:lose|gain)\s+\d+\s*(?:kg|lbs?|pounds?)",
        ]

        for pattern in health_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                mem = self.remember(
                    text=text,
                    category='health',
                    tags=['health', 'goal', 'auto-captured'],
                    source='conversation',
                    importance=7
                )
                if mem:
                    captured.append(mem)
                break

        # ---- Goal patterns ----
        goal_patterns = [
            r"(?:i(?:'m| am)\s+)?(?:trying|planning|going)\s+to\s+(.+)",
            r"(?:my|the)\s+(?:goal|target|plan)\s+(?:is|are|to)\s+(.+)",
        ]

        if not captured:  # Only if nothing else captured
            for pattern in goal_patterns:
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    mem = self.remember(
                        text=text,
                        category='goal',
                        tags=['goal', 'auto-captured'],
                        source='conversation',
                        importance=6
                    )
                    if mem:
                        captured.append(mem)
                    break

        # ---- Explicit "remember" command ----
        remember_match = re.search(
            r"(?:remember|note|save|store)\s+(?:that\s+)?(.+)", text, re.IGNORECASE
        )
        if remember_match and not captured:
            detail = remember_match.group(1).strip()
            mem = self.remember(
                text=detail,
                category='fact',
                tags=['explicit', 'user-requested'],
                source='conversation',
                importance=7
            )
            if mem:
                captured.append(mem)

        # ---- Deeper: Work/Project context ----
        work_patterns = [
            (r"(?:i(?:'m| am)\s+)?(?:working|building|developing|creating|designing)\s+(?:on\s+)?(.+)", 'context', 6),
            (r"(?:my|the)\s+(?:project|app|startup|company|business)\s+(?:is|named|called)\s+(.+)", 'context', 7),
            (r"(?:my|our)\s+(?:team|colleagues|coworkers?)\s+(?:are|is|were)\s+(.+)", 'context', 5),
            (r"(?:i|we)\s+(?:need|want|should)\s+to\s+(?:finish|complete|ship|launch|release)\s+(.+)", 'goal', 7),
        ]

        if not captured:
            for pattern, category, importance in work_patterns:
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    mem = self.remember(
                        text=text,
                        category=category,
                        tags=[category, 'auto-captured', 'deeper'],
                        source='conversation',
                        importance=importance
                    )
                    if mem:
                        captured.append(mem)
                    break

        # ---- Deeper: Emotional states / decisions ----
        if not captured:
            emotion_patterns = [
                (r"i(?:'m| am)\s+(?:so\s+)?(?:happy|excited|thrilled|glad)\s+(?:about|that|because)\s+(.+)", 'context', 5),
                (r"i(?:'m| am)\s+(?:so\s+)?(?:worried|stressed|anxious|nervous|frustrated)\s+(?:about|that|because)\s+(.+)", 'context', 5),
                (r"(?:i|we)\s+(?:decided|agreed|chose)\s+to\s+(.+)", 'context', 6),
                (r"(?:the|my)\s+(?:meeting|call|interview)\s+(?:went|was)\s+(.+)", 'context', 6),
            ]
            for pattern, category, importance in emotion_patterns:
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    mem = self.remember(
                        text=text,
                        category=category,
                        tags=[category, 'auto-captured', 'emotional'],
                        source='conversation',
                        importance=importance
                    )
                    if mem:
                        captured.append(mem)
                    break

        # ---- Deeper: People mentioned (without explicit relationship keywords) ----
        if not captured:
            people_match = re.search(
                r"(?:talked?|met|spoke|called|messaged|texted|contacted)\s+(?:with|to|about)\s+(\w+)",
                text, re.IGNORECASE
            )
            if people_match:
                name = people_match.group(1).title()
                mem = self.remember(
                    text=text,
                    category='relationship',
                    tags=['relationship', 'auto-captured', 'deeper', name.lower()],
                    source='conversation',
                    importance=5
                )
                if mem:
                    captured.append(mem)

        return captured

    # ------------------------------------------------------------------ #
    # Helper methods
    # ------------------------------------------------------------------ #
    def _text_relevance(self, query_lower, memory):
        """Score how relevant a memory is to a query (0.0 to 1.0)."""
        text = (memory.get('text', '') + ' ' + ' '.join(memory.get('tags', []))).lower()
        if not query_lower or not text:
            return 0.0

        query_words = query_lower.split()
        if not query_words:
            return 0.0

        # Exact match
        if query_lower in text:
            return 1.0

        # Word overlap
        text_words = set(text.split())
        matches = sum(1 for w in query_words if w in text_words)
        word_score = matches / len(query_words)

        # Partial word match
        partial = sum(1 for w in query_words if any(w in tw for tw in text_words))
        partial_score = partial / len(query_words)

        return max(word_score, partial_score * 0.8)

    def _similarity(self, text_a, text_b):
        """Simple word-overlap similarity (0.0 to 1.0)."""
        words_a = set(text_a.lower().split())
        words_b = set(text_b.lower().split())
        if not words_a or not words_b:
            return 0.0
        intersection = words_a.intersection(words_b)
        union = words_a.union(words_b)
        return len(intersection) / len(union)

    # ------------------------------------------------------------------ #
    # Advanced: Fuzzy / Semantic Search
    # ------------------------------------------------------------------ #
    def fuzzy_search(self, query, limit=10):
        """
        Enhanced search with fuzzy matching: handles typos, partial words,
        synonyms, and multi-word queries with better scoring.
        """
        query_lower = (query or '').lower().strip()
        if not query_lower:
            return []

        query_words = set(query_lower.split())
        # Remove stop words for better matching
        stop_words = {'the', 'a', 'an', 'is', 'are', 'was', 'were', 'what', 'do',
                      'does', 'did', 'about', 'tell', 'me', 'my', 'your', 'that',
                      'this', 'with', 'for', 'from', 'and', 'or', 'of', 'to', 'in'}
        meaningful_words = query_words - stop_words
        if not meaningful_words:
            meaningful_words = query_words

        now = datetime.datetime.now()
        results = []

        with self._lock:
            for m in self._memories:
                text = (m.get('text', '') + ' ' + ' '.join(m.get('tags', []))).lower()

                # Exact substring match (highest weight)
                exact_score = 1.0 if query_lower in text else 0.0

                # Word-level matching
                text_words = set(text.split())
                full_matches = sum(1 for w in meaningful_words if w in text_words)
                partial_matches = sum(1 for w in meaningful_words
                                      if any(w in tw for tw in text_words) and w not in text_words)
                # Prefix matching (e.g., 'comput' matches 'computer')
                prefix_matches = sum(1 for w in meaningful_words
                                     if any(tw.startswith(w) for tw in text_words))

                word_score = 0.0
                if meaningful_words:
                    full_ratio = full_matches / len(meaningful_words)
                    partial_ratio = partial_matches / len(meaningful_words)
                    prefix_ratio = prefix_matches / len(meaningful_words)
                    word_score = full_ratio * 0.7 + partial_ratio * 0.2 + prefix_ratio * 0.1

                text_score = max(exact_score, word_score)
                if text_score == 0:
                    continue

                # Recency score
                try:
                    created = datetime.datetime.fromisoformat(m.get('created', now.isoformat()))
                    age_days = (now - created).total_seconds() / 86400
                    recency_score = 0.5 ** (age_days / DECAY_HALF_LIFE_DAYS)
                except Exception:
                    recency_score = 0.5

                # Access frequency bonus
                access_bonus = min(m.get('access_count', 0) * 0.05, 0.3)

                # Importance score
                importance_score = m.get('importance', 5) / 10.0

                combined = (text_score * 0.5) + (recency_score * 0.2) + (importance_score * 0.15) + (access_bonus * 0.15)
                results.append((combined, m))

        results.sort(key=lambda x: x[0], reverse=True)
        return [m for _, m in results[:limit]]

    # ------------------------------------------------------------------ #
    # Advanced: Memory Consolidation
    # ------------------------------------------------------------------ #
    def consolidate(self, max_conversation_topics=50):
        """
        Consolidate memories:
        1. Prune old low-importance conversation topics (keep most recent N)
        2. Merge similar memories with combined tags
        3. Boost importance of frequently-accessed memories
        Returns count of memories cleaned up.
        """
        cleaned = 0
        removed_ids = set()
        with self._lock:
            # 1. Prune old conversation topics (keep most recent N)
            topics = [m for m in self._memories if m.get('category') == 'conversation_topic']
            if len(topics) > max_conversation_topics:
                topics.sort(key=lambda x: x.get('created', ''), reverse=True)
                to_remove_ids = {m['id'] for m in topics[max_conversation_topics:]}
                before = len(self._memories)
                self._memories = [m for m in self._memories if m.get('id') not in to_remove_ids]
                removed_ids |= to_remove_ids
                cleaned += before - len(self._memories)

            # 2. Merge near-duplicate memories (across all categories)
            merged_ids = set()
            for i, m1 in enumerate(self._memories):
                if m1.get('id') in merged_ids:
                    continue
                for j in range(i + 1, len(self._memories)):
                    m2 = self._memories[j]
                    if m2.get('id') in merged_ids:
                        continue
                    if (m1.get('category') == m2.get('category') and
                        m1.get('category') not in ('conversation_topic',) and
                        self._similarity(m1.get('text', ''), m2.get('text', '')) > 0.8):
                        # Merge: keep the newer one, boost its importance
                        if m2.get('created', '') > m1.get('created', ''):
                            m1, m2 = m2, m1
                        # Merge tags
                        old_tags = set(m1.get('tags', []))
                        old_tags.update(m2.get('tags', []))
                        m1['tags'] = sorted(old_tags)
                        m1['importance'] = min(10, max(m1.get('importance', 5), m2.get('importance', 5)) + 1)
                        m1['access_count'] = m1.get('access_count', 0) + m2.get('access_count', 0)
                        merged_ids.add(m2['id'])
                        cleaned += 1

            if merged_ids:
                self._memories = [m for m in self._memories if m.get('id') not in merged_ids]
            removed_ids |= merged_ids

            # 3. Boost importance of frequently-accessed memories
            boosted = []
            for m in self._memories:
                if m.get('access_count', 0) >= 3 and m.get('importance', 5) < 8:
                    m['importance'] = min(10, m.get('importance', 5) + 1)
                    boosted.append(m)

        if cleaned:
            self._save()
            self._vector_delete_ids(removed_ids)
            # Refresh metadata for boosted entries (importance changed)
            self._vector_sync_new(boosted)
            print(f"EpisodicMemory: Consolidated — cleaned {cleaned} memories")
        return cleaned

    # ------------------------------------------------------------------ #
    # Advanced: User Profile Building
    # ------------------------------------------------------------------ #
    def get_user_profile(self):
        """
        Build an aggregate user profile from all stored memories.
        Returns a structured dict with categorized facts about the user.
        """
        profile = {
            'preferences': [],
            'health': [],
            'goals': [],
            'relationships': [],
            'routines': [],
            'facts': [],
            'context': [],
        }

        with self._lock:
            for m in self._memories:
                cat = m.get('category', 'fact')
                text = m.get('text', '')
                if cat in profile:
                    profile[cat].append(text)
                elif cat == 'relationship':
                    profile['relationships'].append(text)
                else:
                    profile['facts'].append(text)

        # Deduplicate within each category
        for key in profile:
            seen = set()
            unique = []
            for text in profile[key]:
                if text not in seen:
                    seen.add(text)
                    unique.append(text)
            profile[key] = unique[:20]  # Cap each category

        return profile

    def get_profile_summary(self, max_chars=2000):
        """
        Get a human-readable summary of the user profile.
        Designed to be injected into the Brain's system prompt.
        """
        profile = self.get_user_profile()
        lines = ["USER PROFILE (aggregate from all conversations):"]

        section_map = {
            'preferences': 'Preferences & Likes',
            'health': 'Health & Wellness',
            'goals': 'Goals & Plans',
            'relationships': 'People',
            'routines': 'Daily Routines',
            'facts': 'Personal Facts',
            'context': 'Current Context',
        }

        for key, label in section_map.items():
            items = profile.get(key, [])
            if items:
                lines.append(f"\n{label}:")
                for item in items[:8]:
                    lines.append(f"  • {item[:100]}")

        summary = "\n".join(lines)
        if len(summary) > max_chars:
            summary = summary[:max_chars] + "\n[...truncated...]"
        return summary

    # ------------------------------------------------------------------ #
    # Advanced: Get daily digest of conversations
    # ------------------------------------------------------------------ #
    def get_daily_digest(self, date_str=None):
        """
        Get a summary of all conversations from a specific day.
        date_str: 'YYYY-MM-DD' format, defaults to today.
        """
        if date_str is None:
            date_str = datetime.datetime.now().strftime('%Y-%m-%d')

        topics = []
        with self._lock:
            for m in self._memories:
                created = m.get('created', '')
                if created.startswith(date_str):
                    topics.append(m)

        topics.sort(key=lambda x: x.get('created', ''), reverse=True)
        return topics

    # ------------------------------------------------------------------ #
    # Stats
    # ------------------------------------------------------------------ #
    def stats(self):
        """Return a summary of memory stats."""
        with self._lock:
            categories = defaultdict(int)
            for m in self._memories:
                categories[m.get('category', 'unknown')] += 1
            return {
                'total': len(self._memories),
                'categories': dict(categories),
                'avg_importance': (
                    sum(m.get('importance', 5) for m in self._memories) / max(1, len(self._memories))
                )
            }

    def items_json(self, limit=50):
        """Dashboard-friendly snapshot (most recent first)."""
        with self._lock:
            items = list(self._memories)
        items.sort(key=lambda x: x.get('created', ''), reverse=True)
        return items[:limit]


# ---- Legacy compatibility ----
class Memory:
    """
    Drop-in replacement for the old flat memory.py.
    Wraps EpisodicMemory for backward compat while adding structured storage.
    """
    def __init__(self, filename='memory.json'):
        self.filename = filename
        self.base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.file_path = os.path.join(self.base_dir, filename)
        self.memory = {}
        self._load_memory()

        # Also initialize episodic memory
        self.episodic = EpisodicMemory()

    def _load_memory(self):
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, 'r') as f:
                    self.memory = json.load(f)
            except json.JSONDecodeError:
                self.memory = {}
        else:
            self.memory = {}
            self._save_to_file()

    def _save_to_file(self):
        with open(self.file_path, 'w') as f:
            json.dump(self.memory, f, indent=4)

    def save_memory(self, key, value):
        self.memory[key] = value
        self._save_to_file()

        # Also store in episodic memory as a fact
        self.episodic.remember(
            text=f"{key}: {value}",
            category='fact',
            tags=[key.lower().replace(' ', '_')],
            source='conversation',
            importance=7
        )
        print(f"Memory saved: {key} = {value}")
        return True

    def get_memory(self, key):
        # Check flat memory first, then episodic
        val = self.memory.get(key, None)
        if val:
            return val

        # Search episodic memory for the key
        results = self.episodic.search(key, limit=1)
        if results:
            return results[0].get('text', None)
        return None

    def get_all_memories(self):
        return self.memory
