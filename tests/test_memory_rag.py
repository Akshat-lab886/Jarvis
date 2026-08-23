"""
Phase B tests: vector episodic memory (fallback + semantic), rolling
conversation digest, and knowledge-vault auto-RAG injection.

Vector-dependent tests auto-skip when the embedding stack is broken in
the environment (e.g., missing torch libs) — fallback behavior is
always tested.
"""

import os
import sys
import json
import time
import shutil
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.episodic_memory import EpisodicMemory

# Hard-block any real provider calls from unit tests (.env may carry keys)
from config import Config
Config.GOOGLE_API_KEY = None

# Semantic (embedding) tests are OPT-IN: building a real ChromaDB index
# can hang on machines with broken native ML libs, so we never probe at
# import time.  Run them explicitly:
#   JARVIS_TEST_SEMANTIC=1 python tests/test_memory_rag.py
SEMANTIC_ENABLED = os.getenv('JARVIS_TEST_SEMANTIC') == '1'


def _make_tmp():
    return tempfile.mkdtemp(prefix="jarvis_phaseB_")


def _new_brain():
    """
    Bare Brain instance (no __init__) with only what memory/digest/vault
    helpers need.  Avoids touching real user data files.
    """
    from utils.brain import Brain
    b = Brain.__new__(Brain)
    b.active = True
    b.clients = []          # no providers reachable
    b.models = []
    b.history = []
    import threading as _t
    b.history_lock = _t.Lock()
    b.MAX_HISTORY = 4
    b.total_exchanges = 0
    b._digest_lock = _t.Lock()
    b._evicted_buffer = []
    b.digest_text = ""
    b.digest_upto = 0
    tmp = _make_tmp()
    b._test_tmp = tmp
    b.digest_file = os.path.join(tmp, 'digest.json')
    b._vault_query = None
    b._VAULT_TIMEOUT = 1.0
    # Never touch the real conversation history file in tests
    b._save_history = lambda: None
    return b


class FakeCompleteBrain:
    """Mixin providing a scriptable complete() on bare brains."""

    def attach_complete(self, target, responses=None, default="DIGEST-TEXT"):
        """Attach a recording fake complete() to *target* (the brain)."""
        target.complete_calls = []

        def fake_complete(prompt, system=None, timeout=None, **kw):
            target.complete_calls.append({"prompt": prompt, **kw})
            if callable(responses):
                return responses(prompt)
            return default

        target.complete = fake_complete
        return target


# ====================================================================== #
# Episodic memory — fallback (keyword/fuzzy) mode
# ====================================================================== #

class TestEpisodicFallback(unittest.TestCase):
    def setUp(self):
        self.tmp = _make_tmp()
        self.em = EpisodicMemory(
            filename=os.path.join(self.tmp, 'epi.json'),
            vector_dir=os.path.join(self.tmp, 'vectors'),
            use_vector=False,
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self):
        self.em.remember("User's daughter Aria is allergic to peanuts",
                         category='relationship', importance=9)
        self.em.remember("Prefers Italian food over fast food",
                         category='preference', importance=6)
        self.em.remember("Project Jarvis deadline is October 15",
                         category='context', importance=8)

    def test_remember_and_keyword_search(self):
        self._seed()
        hits = self.em.search("peanut allergy", limit=3)
        self.assertTrue(hits)
        self.assertIn("allergic", hits[0]['text'])

    def test_semantic_search_falls_back_to_fuzzy(self):
        self._seed()
        hits = self.em.semantic_search("child peanut safety", limit=3)
        self.assertTrue(hits)
        self.assertIn("allergic", hits[0]['text'])

    def test_forget_removes_and_persists(self):
        self._seed()
        self.assertEqual(self.em.forget(keyword='deadline'), 1)
        self.assertEqual(self.em.count(), 2)
        em2 = EpisodicMemory(filename=os.path.join(self.tmp, 'epi.json'),
                             use_vector=False)
        self.assertEqual(em2.count(), 2)

    def test_reinforce_dedupes(self):
        self._seed()
        self.em.remember("User's daughter Aria is allergic to peanuts",
                         category='relationship', importance=10)
        self.assertEqual(self.em.count(), 3)
        entry = self.em.search("peanut", limit=1)[0]
        self.assertGreaterEqual(entry['access_count'], 1)

    def test_context_for_prompt_with_query(self):
        self._seed()
        ctx = self.em.get_context_for_prompt(query="what is aria allergic to")
        self.assertIn("allergic", ctx)


# ====================================================================== #
# Episodic memory — semantic mode (skipped if vector stack is broken)
# ====================================================================== #

class TestEpisodicSemantic(unittest.TestCase):
    def setUp(self):
        self.tmp = _make_tmp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @unittest.skipUnless(SEMANTIC_ENABLED,
                         "set JARVIS_TEST_SEMANTIC=1 to run embedding tests")
    def test_semantic_recall_without_shared_words(self):
        em = EpisodicMemory(filename=os.path.join(self.tmp, 'epi.json'),
                            vector_dir=os.path.join(self.tmp, 'vectors'))
        em.remember("My daughter takes the school bus every morning "
                    "at seven thirty", category='routine', importance=7)
        hits = em.semantic_search("kid transportation schedule", limit=2)
        self.assertTrue(hits, "embedding recall should bridge vocabulary")
        self.assertIn("bus", hits[0]['text'])

    @unittest.skipUnless(SEMANTIC_ENABLED,
                         "set JARVIS_TEST_SEMANTIC=1 to run embedding tests")
    def test_hybrid_search_surfaces_semantic_only_hits(self):
        em = EpisodicMemory(filename=os.path.join(self.tmp, 'epi.json'),
                            vector_dir=os.path.join(self.tmp, 'vectors'))
        em.remember("The wifi password for the guest network is falcon99",
                    category='fact', importance=8)
        # No keyword overlap with the query below except none at all
        hits = em.search("internet access code visitors", limit=3)
        self.assertTrue(hits)


# ====================================================================== #
# Rolling conversation digest
# ====================================================================== #

class TestRollingDigest(unittest.TestCase, FakeCompleteBrain):
    def setUp(self):
        self.brain = _new_brain()
        self.attach_complete(self.brain, default="UPDATED-DIGEST-CONTENT")

    def tearDown(self):
        shutil.rmtree(self.brain._test_tmp, ignore_errors=True)

    def test_no_compression_below_threshold(self):
        self.brain._compress_history_if_needed()
        self.assertEqual(self.brain.complete_calls, [])
        self.assertEqual(self.brain.digest_text, "")

    def test_compression_folds_evicted_exchanges(self):
        for i in range(10):
            self.brain._evicted_buffer.append((f"question {i}", f"answer {i}"))
        self.brain._compress_history_if_needed()

        self.assertEqual(len(self.brain.complete_calls), 1)
        prompt = self.brain.complete_calls[0]['prompt']
        self.assertIn("question 9", prompt)
        self.assertEqual(self.brain.digest_text, "UPDATED-DIGEST-CONTENT")
        self.assertEqual(self.brain.digest_upto, 10)
        self.assertEqual(self.brain._evicted_buffer, [])

    def test_llm_failure_keeps_buffer(self):
        self.brain.complete = lambda *a, **k: None
        for i in range(8):
            self.brain._evicted_buffer.append((f"q{i}", f"a{i}"))
        self.brain._compress_history_if_needed()
        self.assertEqual(len(self.brain._evicted_buffer), 8)
        self.assertEqual(self.brain.digest_text, "")

    def test_digest_persists_and_reloads(self):
        for i in range(8):
            self.brain._evicted_buffer.append((f"q{i}", f"a{i}"))
        self.brain._compress_history_if_needed()
        self.assertTrue(os.path.exists(self.brain.digest_file))

        from utils.brain import Brain
        b2 = Brain.__new__(Brain)
        b2._digest_lock = threading.Lock()
        b2.digest_file = self.brain.digest_file
        b2.digest_text = ""
        b2.digest_upto = 0
        b2._evicted_buffer = []
        b2._load_digest()
        self.assertEqual(b2.digest_text, "UPDATED-DIGEST-CONTENT")

    def test_clear_resets_everything(self):
        self.brain.digest_text = "old"
        self.brain.digest_upto = 42
        self.brain._evicted_buffer = [("a", "b")]
        self.brain.clear_digest()
        self.assertEqual(self.brain.digest_text, "")
        self.assertEqual(self.brain._evicted_buffer, [])

    def test_append_history_buffers_evictions(self):
        b = self.brain
        for i in range(10):           # MAX_HISTORY = 4
            b._append_history(f"u{i}", f"j{i}")
        self.assertEqual(len(b.history), 4)
        self.assertEqual(b.total_exchanges, 10)
        self.assertEqual(len(b._evicted_buffer), 6)
        self.assertEqual(b._evicted_buffer[0], ("u0", "j0"))

    def test_pending_survives_restart(self):
        b = self.brain
        for i in range(6):
            b._append_history(f"u{i}", f"j{i}")
        pending_snapshot = list(b._evicted_buffer)
        self.assertTrue(pending_snapshot)

        from utils.brain import Brain
        b2 = Brain.__new__(Brain)
        b2._digest_lock = threading.Lock()
        b2.digest_file = b.digest_file
        b2.digest_text = ""
        b2.digest_upto = 0
        b2._evicted_buffer = []
        b2._load_digest()
        self.assertEqual([tuple(p) for p in b2._evicted_buffer],
                         [tuple(p) for p in pending_snapshot])


# ====================================================================== #
# Knowledge-vault auto-RAG block
# ====================================================================== #

class TestVaultRAG(unittest.TestCase, FakeCompleteBrain):
    def setUp(self):
        self.brain = _new_brain()
        self.attach_complete(self.brain)

    def tearDown(self):
        shutil.rmtree(self.brain._test_tmp, ignore_errors=True)

    def test_no_vault_wired_returns_empty(self):
        self.assertEqual(self.brain._vault_block("anything"), "")

    def test_valid_excerpt_formatted(self):
        self.brain.set_knowledge_vault(lambda q, n=3: (
            "[Source: notes.txt] The launch code is 4471 and the server "
            "room door code is 9982. This text is long enough to pass the "
            "minimum-length filter for inclusion."))
        block = self.brain._vault_block("what is the launch code?")
        self.assertIn("KNOWLEDGE VAULT EXCERPTS", block)
        self.assertIn("4471", block)

    def test_short_or_empty_results_skipped(self):
        self.brain.set_knowledge_vault(lambda q, n=3: "too short")
        self.assertEqual(self.brain._vault_block("q"), "")
        self.brain.set_knowledge_vault(lambda q, n=3: None)
        self.assertEqual(self.brain._vault_block("q"), "")
        self.brain.set_knowledge_vault(lambda q, n=3: "x" * 200)
        self.assertNotEqual(self.brain._vault_block("q"), "")

    def test_vault_exception_is_swallowed(self):
        def boom(q, n=3):
            raise RuntimeError("vault exploded")
        self.brain.set_knowledge_vault(boom)
        self.assertEqual(self.brain._vault_block("q"), "")

    def test_hung_vault_times_out(self):
        gate = threading.Event()

        def hang(q, n=3):
            gate.wait(timeout=30)     # far beyond _VAULT_TIMEOUT
            return "late answer " * 50
        self.brain._VAULT_TIMEOUT = 0.3
        self.brain.set_knowledge_vault(hang)
        start = time.time()
        result = self.brain._vault_block("q")
        elapsed = time.time() - start
        self.assertEqual(result, "")
        self.assertLess(elapsed, 2.0)
        gate.set()                    # release abandoned thread


# ====================================================================== #

if __name__ == '__main__':
    print("=" * 60)
    print("JARVIS PHASE B — MEMORY & CONTEXT TESTS")
    print("=" * 60)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(unittest.TestLoader().loadTestsFromModule(
        sys.modules[__name__]))
    print("\n" + "=" * 60)
    if result.wasSuccessful():
        print(f"ALL {result.testsRun} TESTS PASSED ✅")
    else:
        for test, trace in result.failures + result.errors:
            print(f"  ❌ {test}\n{trace[:300]}")
    print("=" * 60)
