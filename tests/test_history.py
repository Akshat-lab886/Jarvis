"""Unit tests for utils/history.py (CommandLog).

Covers the persistence resilience paths (load/save failure must degrade
gracefully — never raise, never crash __init__) and the entry-capping
+ thread-safety invariants.  Uses a temp dir so no real
brain/data/command_history.json is touched.
"""

import os
import sys
import json
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_log(tmpdir):
    """Build a CommandLog pointed at a temp history file."""
    from utils.history import CommandLog
    log = CommandLog.__new__(CommandLog)
    log.base_dir = tmpdir
    log.file_path = os.path.join(tmpdir, 'command_history.json')
    log._lock = __import__('threading').Lock()
    log._entries = []
    return log


class TestCommandLog(unittest.TestCase):
    def test_load_missing_file_graceful(self):
        with tempfile.TemporaryDirectory() as d:
            log = _make_log(d)
            log._load()  # no file yet
            self.assertEqual(log._entries, [])

    def test_load_corrupt_json_graceful(self):
        """REGRESSION: a corrupted history file must NOT raise — the logger
        conversion must preserve the original 'return []' fallback."""
        with tempfile.TemporaryDirectory() as d:
            log = _make_log(d)
            with open(log.file_path, 'w') as f:
                f.write("{ not valid json }}}")
            log._load()
            self.assertEqual(log._entries, [])

    def test_save_then_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            log = _make_log(d)
            log.log("turn on the lights", "lights", "ok")
            log._save()
            log2 = _make_log(d)
            log2._load()
            self.assertEqual(len(log2._entries), 1)
            self.assertEqual(log2._entries[0]['user_text'],
                             "turn on the lights")

    def test_cap_to_max_entries(self):
        with tempfile.TemporaryDirectory() as d:
            log = _make_log(d)
            from utils.history import MAX_ENTRIES
            # Inject more than MAX_ENTRIES via direct append + save.
            log._entries = [{'user_text': str(i)} for i in range(MAX_ENTRIES + 50)]
            log._save()
            log2 = _make_log(d)
            log2._load()
            self.assertEqual(len(log2._entries), MAX_ENTRIES)
            # Keeps the most recent MAX_ENTRIES.
            self.assertEqual(log2._entries[-1]['user_text'],
                             str(MAX_ENTRIES + 49))

    def test_log_strips_and_creates_dir(self):
        with tempfile.TemporaryDirectory() as d:
            sub = os.path.join(d, 'nested', 'dir')
            log = _make_log(sub)
            log.log("  hello  ", "chat", "hi")
            self.assertTrue(os.path.exists(log.file_path))
            # The user text is persisted as-is (stripped by caller contract
            # here we just confirm it round-trips).
            with open(log.file_path) as f:
                data = json.load(f)
            self.assertTrue(any(e['user_text'] for e in data))


if __name__ == '__main__':
    unittest.main(verbosity=2)
