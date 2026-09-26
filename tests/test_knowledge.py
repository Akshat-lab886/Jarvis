"""Unit tests for utils.knowledge.Librarian — the Tier A RAG engine.

Covers the text chunking + vector-ingestion logic that previously had 0%
coverage. Construction is env-gated (JARVIS_DISABLE_VECTOR=1 skips the
heavy chromadb/torch init so these tests stay offline), and the ChromaDB
collection is stubbed so no real vector store is touched.

Key regression: memorized chunk IDs must be globally unique across rapid
ingest calls that share a source name — the old int(time.time())_i
scheme collides within the same second, causing ChromaDB to either
raise on duplicate IDs or silently overwrite prior chunks (data loss).
"""

import os
import sys
import unittest

# Disable the vector backend BEFORE importing the module so __init__
# never touches chromadb / torch / SentenceTransformer.
os.environ.setdefault('JARVIS_DISABLE_VECTOR', '1')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.knowledge import Librarian


class _FakeCollection:
    """Minimal stand-in for the ChromaDB collection.add interface."""

    def __init__(self):
        self.calls = []
        self._seen_ids = set()

    def add(self, documents=None, ids=None, metadatas=None):
        for i in ids:
            if i in self._seen_ids:
                raise RuntimeError(f"duplicate id in collection: {i}")
            self._seen_ids.add(i)
        self.calls.append({
            'documents': documents, 'ids': ids, 'metadatas': metadatas,
        })

    def count(self):
        return len(self._seen_ids)


class TestChunkText(unittest.TestCase):
    def setUp(self):
        self.lib = Librarian()

    def test_chunks_respect_size(self):
        """No chunk (minus trailing space) exceeds chunk_size words-chars."""
        text = "word " * 200  # 1000 chars
        chunks = self.lib._chunk_text(text, chunk_size=200)
        self.assertTrue(len(chunks) >= 2)
        for c in chunks:
            # current_length is word-char count; chunk content ~ size
            self.assertLessEqual(len(c), 210)

    def test_short_text_is_one_chunk(self):
        chunks = self.lib._chunk_text("hello world", chunk_size=500)
        self.assertEqual(chunks, ["hello world"])

    def test_empty_text_returns_empty_list(self):
        self.assertEqual(self.lib._chunk_text(""), [])
        self.assertEqual(self.lib._chunk_text("   "), [])

    def test_no_word_lost(self):
        text = "the quick brown fox jumps over the lazy dog"
        joined = " ".join(self.lib._chunk_text(text, chunk_size=10))
        self.assertEqual(joined, text)


class TestMemorizeText(unittest.TestCase):
    def setUp(self):
        self.lib = Librarian()
        self.lib.collection = _FakeCollection()

    def test_no_chunks_when_empty(self):
        result = self.lib.memorize_text("")
        self.assertEqual(result, "Stored 0 chunks.")
        self.assertEqual(self.lib.collection.calls, [])

    def test_vault_not_initialized(self):
        """If the collection never came up (torch/chroma broken), we degrade
        gracefully instead of crashing."""
        self.lib.collection = None
        self.assertEqual(self.lib.memorize_text("some text"),
                         "Error: Vault not initialized.")

    def test_ids_unique_within_call(self):
        # Long enough to split into multiple 500-char chunks.
        text = "word " * 300
        self.lib.memorize_text(text)
        ids = self.lib.collection.calls[0]['ids']
        self.assertGreater(len(ids), 1)
        self.assertEqual(len(ids), len(set(ids)))

    def test_ids_unique_across_rapid_calls(self):
        """THE regression: two same-source ingest calls in the same second
        must not produce colliding IDs. Previously both emitted
        'source_<epoch>_<0>' and ChromaDB raised / clobbered chunks."""
        text = "word " * 300
        self.lib.memorize_text(text)
        self.lib.memorize_text(text)
        seen = set()
        for call in self.lib.collection.calls:
            for i in call['ids']:
                self.assertNotIn(i, seen, f"duplicate id across calls: {i}")
                seen.add(i)

    def test_metadata_source_preserved(self):
        # Short text -> one chunk, but still validates metadata wiring.
        self.lib.memorize_text("alpha beta gamma",
                               source_name="my_doc")
        for m in self.lib.collection.calls[0]['metadatas']:
            self.assertEqual(m['source'], "my_doc")


class TestIndexEntry(unittest.TestCase):
    """Unit-test the _index_entry metadata builder in isolation.

    Covers the regression: a corrupted/migrated entry whose 'importance'
    is a non-numeric string (e.g. "high") previously crashed int() and
    silently killed the watchdog rebuild thread.
    """

    def _entry(self):
        from utils.episodic_memory import EpisodicMemory
        # Bypass __init__ (which loads/persists) — _index_entry is pure.
        obj = EpisodicMemory.__new__(EpisodicMemory)
        return obj._index_entry

    def test_normal_int_importance(self):
        out = self._entry()({"importance": 7, "category": "fact"})
        self.assertEqual(out["importance"], 7)

    def test_string_importance_is_coerced(self):
        """REGRESSION: importance='high' (or any non-numeric) must not
        raise — falls back to the default 5 instead of crashing the
        rebuild watchdog."""
        out = self._entry()({"importance": "high"})
        self.assertEqual(out["importance"], 5)

    def test_importance_clamped_to_range(self):
        out = self._entry()({"importance": 999})
        self.assertEqual(out["importance"], 10)
        out = self._entry()({"importance": -5})
        self.assertEqual(out["importance"], 1)

    def test_defaults_when_missing(self):
        out = self._entry()({})
        self.assertEqual(out["importance"], 5)
        self.assertEqual(out["category"], "fact")


if __name__ == "__main__":
    unittest.main(verbosity=2)
