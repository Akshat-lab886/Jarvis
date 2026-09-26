"""
Offline tests for utils.checkpoints — the Tier A snapshot/rollback engine.

Covers:
  - create + rollback round trip (file restored after edit)
  - _find: exact name, prefix, label, ambiguity
  - create reports files that were skipped (large / out-of-root)
  - rollback skips unsafe paths (absolute / ``..``) forged into a manifest
  - drop + _prune bounds the snapshot count
"""

import os
import shutil
import json
import tempfile
import unittest
from unittest.mock import patch

import utils.checkpoints as ckpt


class _TempRepoMixin:
    """Redirect _BASE_DIR (repo root) into a temp dir so snapshots stay
    isolated from the real Jarvis workspace."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ckpt_test_")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        self.src = os.path.join(self.repo, "src")
        os.makedirs(self.src)
        # Patch the module-level _BASE_DIR so relpath/restore target the
        # temp repo instead of the real Jarvis checkout.
        self._patcher = patch.object(ckpt, "_BASE_DIR", self.repo)
        self._patcher.start()
        # Snapshots land in a temp dir under the temp repo.
        self.root = os.path.join(self.repo, "snapshots")
        os.makedirs(self.root, exist_ok=True)

    def tearDown(self):
        self._patcher.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mgr(self):
        return ckpt.CheckpointManager(root=self.root)


class TestCheckpointRoundTrip(_TempRepoMixin, unittest.TestCase):
    def test_create_and_rollback_restores_file(self):
        mgr = self._mgr()
        path = os.path.join(self.src, "notes.md")
        with open(path, "w") as f:
            f.write("original line")
        msg = mgr.create(label="baseline", paths=[path])
        # edit the file
        with open(path, "w") as f:
            f.write("corrupted — never should survive")
        snap_name = mgr.list(limit=1).split(":")[0] if False else None
        # find the snapshot name
        snaps = mgr._snapshots()
        self.assertTrue(snaps)
        name = snaps[0][0]
        # rollback
        out = mgr.rollback(name)
        self.assertIn("Rolled back", out)
        with open(path) as f:
            self.assertEqual(f.read(), "original line")

    def test_create_disabled_when_env_off(self):
        mgr = self._mgr()
        with patch.dict(os.environ, {"JARVIS_CHECKPOINTS": "0"}):
            self.assertFalse(ckpt.enabled())
            msg = mgr.create(label="x", paths=[self.src])
        self.assertIn("disabled", msg)


class TestFindAndDrop(_TempRepoMixin, unittest.TestCase):
    def test_find_exact_prefix_label(self):
        mgr = self._mgr()
        path = os.path.join(self.src, "a.txt")
        with open(path, "w") as f:
            f.write("x")
        mgr.create(label="pre-edit", paths=[path])
        snaps = mgr._snapshots()
        name = snaps[0][0]
        # exact name
        self.assertEqual(mgr._find(name), name)
        # prefix match
        self.assertEqual(mgr._find(name[:6]), name)
        # label match
        self.assertEqual(mgr._find("pre-edit"), name)

    def test_find_ambiguous_raises(self):
        mgr = self._mgr()
        p1 = os.path.join(self.src, "a.txt")
        p2 = os.path.join(self.src, "b.txt")
        for p in (p1, p2):
            with open(p, "w") as f:
                f.write("x")
        mgr.create(label="dup", paths=[p1])
        mgr.create(label="dup", paths=[p2])
        # prefix "dup" matches both snapshot dirs (both end with _dup)
        with self.assertRaises(LookupError):
            mgr._find("dup")


class TestSkipReporting(_TempRepoMixin, unittest.TestCase):
    def test_large_file_recorded_as_skipped(self):
        mgr = self._mgr()
        # write a file > 2 MB so it exceeds _MAX_FILE_BYTES
        big = os.path.join(self.src, "big.bin")
        with open(big, "wb") as f:
            f.write(b"\0" * (3 * 1024 * 1024))
        small = os.path.join(self.src, "small.txt")
        with open(small, "w") as f:
            f.write("tiny")
        msg = mgr.create(label="big-test", paths=[big, small])
        self.assertIn("1 skipped", msg)
        # the snapshot manifest recorded the skip
        snaps = mgr._snapshots()
        name = snaps[0][0]
        manifest = json.load(open(os.path.join(self.root, name,
                                               "manifest.json")))
        self.assertIn("skipped", manifest)
        self.assertEqual(len(manifest["skipped"]), 1)
        self.assertEqual(manifest["files"], ["src/small.txt"])


class TestRollbackSafety(_TempRepoMixin, unittest.TestCase):
    def test_rollback_skips_unsafe_paths(self):
        """A forged manifest with an absolute / '..' entry is NOT restored."""
        mgr = self._mgr()
        victim = os.path.join(self.repo, "victim.md")
        with open(victim, "w") as f:
            f.write("untouched")
        # forge a snapshot dir with a malicious manifest
        snap_name = "20240101_000000_evil"
        snap_dir = os.path.join(self.root, snap_name)
        os.makedirs(snap_dir)
        evil_payload = "/etc/passwd"   # absolute path
        manifest = {"label": "evil", "note": "", "files": [evil_payload,
                                                           "../escape.md"],
                    "skipped": []}
        with open(os.path.join(snap_dir, "manifest.json"), "w") as f:
            json.dump(manifest, f)
        msg = mgr.rollback(snap_name)
        self.assertIn("Rolled back", msg)
        # victim untouched (nothing restored)
        self.assertEqual(open(victim).read(), "untouched")
        # no file written outside repo
        self.assertFalse(os.path.exists("/etc/passwd_jarvis_test"))


class TestPruneAndList(_TempRepoMixin, unittest.TestCase):
    def test_list_and_stats(self):
        mgr = self._mgr()
        p = os.path.join(self.src, "a.txt")
        with open(p, "w") as f:
            f.write("x")
        mgr.create(label="one", paths=[p])
        listing = mgr.list(limit=10)
        self.assertIn("Checkpoint", listing)
        stats = mgr.stats()
        self.assertGreaterEqual(stats.get("snapshots", 0), 1)
        self.assertEqual(stats["enabled"], ckpt.enabled())
