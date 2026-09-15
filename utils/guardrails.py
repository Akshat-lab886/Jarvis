"""
J.A.R.V.I.S. — User & Workspace Guardrails (Hermes parity)
==========================================================

Continuously self-updating localized markdown configurations that keep
persistent rules, project boundaries and user styles in every prompt:

    USER.md      (repo root)  — who the user is: preferences, style,
                                standing rules.  Written by Jarvis as it
                                learns (RLM reflector feeds preferences
                                here) and by the user by hand.
    MEMORY.md    (repo root)  — durable facts worth never re-deriving
                                (addresses, accounts, ongoing situations).
    .jarvis.md   (per project)— project boundaries: what this codebase
                                is, conventions, what NOT to touch.

All three are plain markdown with ``## <Section>`` headers so the user
can edit them freely and the model can read them natively.  Jarvis only
ever APPENDS lines under a section (deduplicated), never rewrites —
hand-written content is sacred.

Injection: ``Guardrails.load_block()`` renders a compact prompt block;
Brain.think() includes it on every turn (cached 60s like the other
context blocks).  ``JARVIS_GUARDRAILS=0`` disables everything.
"""

import os
import re
import threading
import logging

logger = logging.getLogger("Jarvis.Guardrails")

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

USER_MD = os.path.join(_BASE_DIR, 'USER.md')
MEMORY_MD = os.path.join(_BASE_DIR, 'MEMORY.md')

# Which RLM insight kinds land in which file/section
# (covers both legacy kinds and the reflector persona's
# user_model|preference|goal|constraint|lesson taxonomy — 'lesson'
# is intentionally unmapped here: reflect() routes lessons to
# brain/data/lessons.md via self_learning instead of USER/MEMORY.md)
_KIND_DESTINATIONS = {
    'preference': ('user', 'Preferences'),
    'style': ('user', 'Style'),
    'rule': ('user', 'Rules'),
    'fact': ('memory', 'Facts'),
    'goal': ('memory', 'Facts'),
    'boundary': ('project', 'Boundaries'),
    'user_model': ('user', 'Facts'),
    'constraint': ('user', 'Rules'),
}

_MAX_FILE_CHARS = 6000     # per-file read cap for prompt injection
_CACHE_TTL = 60            # seconds


def enabled():
    return os.getenv('JARVIS_GUARDRAILS', '1') != '0'


def _read(path, max_chars=_MAX_FILE_CHARS):
    try:
        if not os.path.exists(path):
            return ""
        with open(path, 'r', encoding='utf-8',
                  errors='replace') as f:
            text = f.read()
        return text.strip()[:max_chars]
    except Exception as e:
        logger.debug("guardrail read failed (%s): %s", path, e)
        return ""


def _normalize(line):
    """Collapse whitespace so near-duplicates match."""
    return re.sub(r'\s+', ' ', str(line or '').strip().lower()).rstrip('.')


def _bound_md(content, max_lines):
    """
    Shrink *content* (a ``## ``-sectioned markdown doc) to at most
    *max_lines* bullet lines, evicting the OLDEST bullets first.

    The title and every section header are kept as long as any bullet
    under them survives; a header whose bullets were all evicted is
    dropped along with them.  A naive whole-file tail-cut can instead
    land above an older section's header while the tail of that
    section's bullets survives below it — orphaning those bullets under
    whatever follows and silently losing the file title.  Because the
    user's markdown is treated as sacred (we only ever append), the
    bounding pass must never rearrange or strand existing lines.
    """
    lines = content.split('\n')
    bullets = [i for i, ln in enumerate(lines) if ln.startswith('- ')]
    if len(bullets) <= max_lines:
        return content
    # owning section-header index for each bullet (nearest preceding '## ')
    owned_by = {}
    cur = None
    for i, ln in enumerate(lines):
        if ln.startswith('## '):
            cur = i
        elif ln.startswith('- ') and cur is not None:
            owned_by.setdefault(cur, []).append(i)
    excess = len(bullets) - max_lines
    doomed = set(bullets[:excess])
    kept = []
    for i, ln in enumerate(lines):
        if i in doomed:
            continue
        if ln.startswith('## ') and owned_by.get(i) and \
                all(b in doomed for b in owned_by[i]):
            continue                 # emptied section: drop its header too
        kept.append(ln)
    return re.sub(r'\n{3,}', '\n\n', '\n'.join(kept)).strip() + '\n'


def _update_md(path, section, line, max_lines=120):
    """
    Append *line* under ``## <section>`` in the markdown file at *path*,
    creating the file/section if needed.  Deduplicated and bounded.

    Returns a human-readable status string (never raises).
    """
    line = re.sub(r'\s+', ' ', str(line or '').strip())
    if not line or not section:
        return "Nothing to update."
    line = line.rstrip('.') + '.'
    try:
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        with _WRITE_LOCK:
            section = re.sub(r'[^A-Za-z0-9 &-]', '', str(section)) or 'Notes'
            header = f"## {section}"

            # Read under the lock so a read-modify-write from a concurrent
            # updater can never lose an append (single atomic turn).
            body = _read(path, max_chars=10 ** 9)   # full text for editing

            if _normalize(line) and _normalize(line) in _normalize(body):
                return f"Already recorded in {os.path.basename(path)}."

            if not body:
                content = (f"# {os.path.basename(path)}\n\n"
                           f"{header}\n- {line}\n")
            elif header in body:
                # Append under the section — before the next header if
                # one follows, else at the end of the file.
                idx = body.index(header) + len(header)
                nxt = re.search(r'\n## ', body[idx:])
                if nxt:
                    insert_at = idx + nxt.start()
                    content = (body[:insert_at].rstrip()
                               + f"\n- {line}\n" + body[insert_at:])
                else:
                    content = body.rstrip() + f"\n- {line}\n"
            else:
                content = body.rstrip() + f"\n\n{header}\n- {line}\n"

            # Bound the file: evict the OLDEST bullets when it grows
            # huge.  Section headers (and the title) survive only while
            # bullets under them do, so no surviving bullet can end up
            # orphaned under a dropped header (see _bound_md).
            content = _bound_md(content, max_lines)

            with open(path, 'w', encoding='utf-8') as f:
                f.write(content)
        return f"Recorded in {os.path.basename(path)} → {section}: {line}"
    except Exception as e:
        logger.warning("guardrail update failed (%s): %s", path, e)
        return f"Could not update {os.path.basename(path)}: {e}"


_WRITE_LOCK = threading.RLock()   # one lock guards all three files


class Guardrails:
    """Loader + self-updater for USER.md / MEMORY.md / .jarvis.md."""

    def __init__(self, user_md=None, memory_md=None):
        self.user_md = user_md or USER_MD
        self.memory_md = memory_md or MEMORY_MD
        self._cache = None
        self._cache_ts = 0.0
        self._lock = threading.RLock()

    # ------------------------------------------------------------- #
    # Prompt injection
    # ------------------------------------------------------------- #
    def load_block(self, project_dir=None, max_chars=2400):
        """
        Render the guardrail block for the system prompt.  Empty string
        when disabled or when no file exists yet.
        """
        if not enabled():
            return ""
        with self._lock:
            import time as _time
            if self._cache is not None and \
                    _time.time() - self._cache_ts < _CACHE_TTL:
                cached = self._cache
            else:
                cached = None
        if cached is not None and project_dir is None:
            return cached[:max_chars]

        parts = []
        user = _read(self.user_md)
        if user:
            parts.append(f"USER PROFILE & RULES (USER.md — persistent, "
                         f"user-editable):\n{user}")
        memory = _read(self.memory_md)
        if memory:
            parts.append(f"PERSISTENT FACTS (MEMORY.md):\n{memory}")
        project_md = self.project_md_path(project_dir)
        project = _read(project_md) if project_md else ""
        if project:
            parts.append(f"PROJECT BOUNDARIES ({os.path.basename(project_md)} "
                         f"— obey these for this project):\n{project}")

        if not parts:
            block = ""
        else:
            block = "GUARDRAILS — standing instructions that override "
            block += "ad-hoc requests where they conflict:\n\n"
            block += "\n\n".join(parts)

        if project_dir is None:
            with self._lock:
                self._cache = block
                import time as _time
                self._cache_ts = _time.time()
        return block[:max_chars]

    @staticmethod
    def project_md_path(project_dir):
        """Resolve the .jarvis.md for a project dir (or None)."""
        if not project_dir:
            return None
        path = os.path.join(str(project_dir), '.jarvis.md')
        return path if os.path.isdir(str(project_dir)) else None

    def invalidate(self):
        with self._lock:
            self._cache = None
            self._cache_ts = 0.0

    # ------------------------------------------------------------- #
    # Self-update API (called by the RLM reflector, the agent, or the
    # user via the guardrails_update action)
    # ------------------------------------------------------------- #
    def update(self, target, section, line):
        """
        target: 'user' | 'memory' | 'project' (project uses the
        workspace root or JARVIS_PROJECT_DIR).
        """
        if not enabled():
            return "Guardrails are disabled (JARVIS_GUARDRAILS=0)."
        target = str(target or '').lower()
        if target == 'user':
            out = _update_md(self.user_md, section, line)
        elif target == 'memory':
            out = _update_md(self.memory_md, section, line)
        elif target == 'project':
            project_dir = os.getenv('JARVIS_PROJECT_DIR') or \
                os.path.join(_BASE_DIR, 'workspace')
            out = _update_md(os.path.join(project_dir, '.jarvis.md'),
                             section, line)
        else:
            return "target must be 'user', 'memory' or 'project'."
        self.invalidate()
        return out

    def capture_insight(self, text, kind):
        """
        RLM reflector hook: route an extracted insight to its natural
        home file.  Unknown kinds are ignored.
        """
        dest = _KIND_DESTINATIONS.get(str(kind or '').lower())
        if not dest or not text:
            return None
        target, section = dest
        return self.update(target, section, text)


# --------------------------------------------------------------------- #
# Singleton
# --------------------------------------------------------------------- #

_singleton = None
_singleton_lock = threading.Lock()


def get_guardrails():
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = Guardrails()
        return _singleton


def _reset_singleton():
    """Test helper."""
    global _singleton
    with _singleton_lock:
        _singleton = None
