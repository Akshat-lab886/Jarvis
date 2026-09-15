"""
J.A.R.V.I.S. — Automatic Workspace Checkpoints (Hermes parity)
==============================================================

Takes localized snapshots of the active working directory BEFORE
initiating code edits, enabling full recovery with a single
``/rollback`` if an automated run breaks something.

How it works:
    * ``create(label, paths)`` — copies the CURRENT content of the
      given files (or every text file under a project dir, bounded)
      into ``brain/data/checkpoints/<ts>_<label>/`` with a manifest.
    * The executor calls ``before_edit(...)`` automatically before
      every dev_write / write_code edit — the user never asks for a
      checkpoint by hand.
    * ``rollback(id_or_label)`` restores the manifest's files exactly
      as they were (overwriting whatever the automated run produced).
    * Snapshots are bounded: last N kept, per-file size cap, text
      files only, generated artifacts excluded.

``JARVIS_CHECKPOINTS=0`` disables snapshotting entirely.
"""

import os
import json
import shutil
import logging
import threading
import datetime

logger = logging.getLogger("Jarvis.Checkpoints")

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ROOT = os.path.join(_BASE_DIR, 'brain', 'data', 'checkpoints')

_MAX_KEEP = 20              # snapshots retained
_MAX_FILE_BYTES = 2 * 1024 * 1024   # per file in a snapshot
_MAX_FILES_PER_SNAPSHOT = 400
_SKIP_DIRS = {'.git', '__pycache__', 'node_modules', '.venv', 'venv',
              'dist', 'build', '.next', '.mypy_cache'}
_TEXT_EXTS = {'.txt', '.md', '.py', '.js', '.ts', '.tsx', '.jsx', '.json',
              '.yaml', '.yml', '.toml', '.ini', '.cfg', '.csv', '.html',
              '.css', '.sh', '.env', '.xml', '.sql', '.rs', '.go',
              '.java', '.c', '.cpp', '.h', '.swift', '.kt', '.rb',
              '.php', '.vue', '.svelte'}


def enabled():
    return os.getenv('JARVIS_CHECKPOINTS', '1') != '0'


def _is_text_file(path):
    if os.path.splitext(path)[1].lower() in _TEXT_EXTS:
        return True
    try:
        with open(path, 'rb') as f:
            return b'\x00' not in f.read(1024)
    except Exception:
        return False


class CheckpointManager:
    """Snapshot / list / rollback store."""

    def __init__(self, root=None):
        self.root = root or DEFAULT_ROOT
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    # Creating snapshots
    # ------------------------------------------------------------------ #
    def _collect_files(self, paths):
        """Expand the requested paths into a concrete file list."""
        files = []
        for p in paths or []:
            p = os.path.abspath(str(p))
            if os.path.isfile(p):
                files.append(p)
            elif os.path.isdir(p):
                for dirpath, dirnames, filenames in os.walk(p):
                    dirnames[:] = [d for d in dirnames
                                   if d not in _SKIP_DIRS]
                    for name in filenames:
                        fp = os.path.join(dirpath, name)
                        if _is_text_file(fp):
                            files.append(fp)
                        if len(files) >= _MAX_FILES_PER_SNAPSHOT:
                            return files
        return files

    def create(self, label='edit', paths=None, note=''):
        """
        Snapshot the given files (or the default workspace when none).
        Returns a status message.  Never raises.
        """
        if not enabled():
            return "Checkpoints disabled (JARVIS_CHECKPOINTS=0)."
        try:
            if not paths:
                paths = [os.path.join(_BASE_DIR, 'workspace')]
                if not os.path.isdir(paths[0]):
                    return "Nothing to snapshot — no paths given."
            files = self._collect_files(paths)
            if not files:
                return "No text files found to checkpoint."

            stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')
            slug = ''.join(c if c.isalnum() or c in '-_' else '_'
                           for c in str(label))[:40] or 'edit'
            snap_dir = os.path.join(self.root, f"{stamp}_{slug}")
            os.makedirs(snap_dir, exist_ok=True)

            manifest = {'label': str(label), 'note': str(note or '')[:300],
                        'created': datetime.datetime.now().isoformat(),
                        'files': []}
            copied = 0
            for src in files:
                try:
                    if os.path.getsize(src) > _MAX_FILE_BYTES:
                        continue
                    rel = os.path.relpath(src, _BASE_DIR)
                    # Symmetry with rollback's guard: a file outside the
                    # repo root would be stored under ``..`` components
                    # that rollback refuses to restore.  Skip it rather
                    # than write an unrecoverable entry (which would also
                    # physically escape the snapshot dir on disk).
                    if (rel in ('.', '..') or rel.startswith('..')
                            or os.path.isabs(rel)):
                        continue
                    dst = os.path.join(snap_dir, rel)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(src, dst)
                    manifest['files'].append(rel)
                    copied += 1
                except Exception as e:
                    logger.debug("checkpoint copy skipped (%s): %s", src, e)

            with open(os.path.join(snap_dir, 'manifest.json'),
                      'w', encoding='utf-8') as f:
                json.dump(manifest, f, indent=2)

            self._prune()
            logger.info("checkpoint '%s' saved: %d file(s)", label, copied)
            return (f"Checkpoint saved: {slug} ({copied} file(s)). "
                    f"Rollback anytime with /rollback {slug}.")
        except Exception as e:
            logger.warning("checkpoint failed: %s", e)
            return f"Checkpoint failed: {e}"

    def before_edit(self, file_paths, note=''):
        """Executor hook — snapshot right before an automated edit."""
        paths = [p for p in (file_paths or []) if p]
        if not paths:
            return None
        return self.create(label='pre-edit', paths=paths, note=note)

    # ------------------------------------------------------------------ #
    # Listing / rollback
    # ------------------------------------------------------------------ #
    def _snapshots(self):
        """[(dir, manifest)] newest-first; corrupt entries skipped."""
        out = []
        if not os.path.isdir(self.root):
            return out
        for name in sorted(os.listdir(self.root), reverse=True):
            mpath = os.path.join(self.root, name, 'manifest.json')
            if not os.path.exists(mpath):
                continue
            try:
                with open(mpath, encoding='utf-8') as f:
                    manifest = json.load(f)
                out.append((name, manifest))
            except Exception:
                continue
        return out

    def list(self, limit=10):
        """Human-readable listing for the dashboard / TUI."""
        snaps = self._snapshots()[:limit]
        if not snaps:
            return "No checkpoints yet."
        lines = ["Checkpoints (newest first):"]
        for name, m in snaps:
            files = m.get('files')
            if not isinstance(files, list):
                files = []          # corrupt-but-valid manifest: degrade
            note = f" — {m['note']}" if m.get('note') else ''
            lines.append(f"- {name}: {len(files)} file(s), "
                         f"{m.get('label', '?')}{note}")
        return "\n".join(lines)

    def _find(self, selector):
        """Resolve a snapshot dir by exact name, unique prefix, or
        label.  Returns the dir name or None (ambiguous → error msg)."""
        if not selector:
            return None
        snaps = self._snapshots()
        names = [n for n, _ in snaps]
        selector = str(selector)
        if selector in names:
            return selector
        prefixes = [n for n in names if n.startswith(selector)
                    or selector in n.split('_', 1)[-1]]
        if len(prefixes) == 1:
            return prefixes[0]
        labels = [n for n, m in snaps if m.get('label') == selector]
        if len(labels) == 1:
            return labels[0]
        if len(prefixes) > 1 or len(labels) > 1:
            raise LookupError(f"ambiguous checkpoint '{selector}' — "
                              f"be more specific")
        return None

    def rollback(self, selector=None):
        """
        Restore a snapshot (defaults to the newest).  Returns a status
        message.  Files are copied back over whatever broke.
        """
        if not enabled():
            return "Checkpoints disabled."
        try:
            if selector is None:
                snaps = self._snapshots()
                if not snaps:
                    return "No checkpoint to roll back to."
                name = snaps[0][0]
            else:
                name = self._find(selector)
                if name is None:
                    return (f"No checkpoint matches '{selector}'. "
                            f"Use checkpoint list to see them.")
            snap_dir = os.path.join(self.root, name)
            mpath = os.path.join(snap_dir, 'manifest.json')
            with open(mpath, encoding='utf-8') as f:
                manifest = json.load(f)

            restored, missing = 0, 0
            for raw in manifest.get('files', []):
                # Defense-in-depth: never restore outside the repo root.
                # A forged/corrupt manifest (or a snapshot taken from an
                # out-of-repo path) must not be able to overwrite
                # arbitrary locations via ``..`` or an absolute path.
                rel = os.path.normpath(str(raw))
                if (not rel or rel in ('.', '..') or rel.startswith('..')
                        or os.path.isabs(rel)):
                    missing += 1
                    logger.warning("rollback skipped unsafe path: %s", raw)
                    continue
                src = os.path.join(snap_dir, rel)
                dst = os.path.join(_BASE_DIR, rel)
                try:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(src, dst)
                    restored += 1
                except Exception as e:
                    missing += 1
                    logger.debug("restore failed (%s): %s", rel, e)
            logger.info("rolled back to checkpoint %s (%d files)",
                        name, restored)
            msg = (f"Rolled back to {name}: {restored} file(s) restored"
                   f"{' , ' + str(missing) + ' failed' if missing else ''}"
                   ".")
            return msg.replace(' , ', ', ')
        except LookupError as e:
            return str(e)
        except Exception as e:
            return f"Rollback failed: {e}"

    def drop(self, selector):
        """Delete a snapshot."""
        try:
            name = self._find(selector)
        except LookupError as e:
            return str(e)
        if name is None:
            return f"No checkpoint matches '{selector}'."
        shutil.rmtree(os.path.join(self.root, name), ignore_errors=True)
        return f"Checkpoint {name} deleted."

    def _prune(self):
        """Keep only the newest _MAX_KEEP snapshots."""
        snaps = self._snapshots()
        for name, _ in snaps[_MAX_KEEP:]:
            shutil.rmtree(os.path.join(self.root, name),
                          ignore_errors=True)

    def stats(self):
        snaps = self._snapshots()
        return {'snapshots': len(snaps),
                'latest': snaps[0][0] if snaps else None,
                'enabled': enabled()}


# --------------------------------------------------------------------- #
# Singleton
# --------------------------------------------------------------------- #

_singleton = None
_singleton_lock = threading.Lock()


def get_checkpoints():
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = CheckpointManager()
        return _singleton


def _reset_singleton():
    """Test helper."""
    global _singleton
    with _singleton_lock:
        _singleton = None
