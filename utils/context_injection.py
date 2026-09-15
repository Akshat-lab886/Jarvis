"""
J.A.R.V.I.S. — Dynamic Context Injection (Hermes parity)
========================================================

``@`` markers natively inside any interface auto-expand and inject
files, folders, git diffs or URLs straight into the context prompt:

    "review @src/app.py and check the diff @git"
    "summarize @https://example.com/article"
    "clean up @~/Downloads a bit"
    "@brain/data/ what's growing in there?"

Marker grammar (deliberately strict so emails and social handles are
never touched): ``@token`` must start at a word boundary, and the token
may contain path characters.  A token resolves to the FIRST of:

    an existing file        → its content (capped)
    an existing directory   → a shallow tree + head of each text file
    'git' | 'diff'          → ``git diff`` (plus status) of the repo
    'status'                → ``git status --short``
    a URL (http/s)          → fetched page text
    nothing                 → left untouched

The expansion is idempotent (a second pass sees its own header and
stops) and budget-capped.  ``JARVIS_CONTEXT_INJECTION=0`` disables it.
"""

import os
import re
import logging

logger = logging.getLogger("Jarvis.ContextInjection")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Marker: start-of-string or whitespace, '@', then path/URL-ish chars.
# ':' is included so URLs ('@https://…') tokenize whole; emails stay
# safe because '@' must still follow start-or-whitespace.
_MARKER_RE = re.compile(r'(?:^|(?<=\s))@([\w.:/~+-]+)')

_HEADER = "[CONTEXT EXPANSION]"

_PER_FILE_CAP = 3000        # chars per injected file
_DIR_FILES = 8              # files previewed per directory
_TOTAL_CAP = 9000           # overall expansion budget
_TEXT_EXTS = {'.txt', '.md', '.py', '.js', '.ts', '.tsx', '.jsx', '.json',
              '.yaml', '.yml', '.toml', '.ini', '.cfg', '.csv', '.html',
              '.css', '.sh', '.env', '.log', '.xml', '.sql', '.rs',
              '.go', '.java', '.c', '.cpp', '.h'}


def enabled():
    return os.getenv('JARVIS_CONTEXT_INJECTION', '1') != '0'


def _resolve_path(token):
    path = os.path.expanduser(token)
    if not os.path.isabs(path):
        # Relative to the repo root first, then to CWD
        candidate = os.path.join(BASE_DIR, path)
        if os.path.exists(candidate):
            return candidate
        if os.path.exists(path):
            return os.path.abspath(path)
    return path if os.path.exists(path) else None


def _read_file(path):
    """Capped read of a text file; binary files report metadata only."""
    try:
        size = os.path.getsize(path)
        ext = os.path.splitext(path)[1].lower()
        with open(path, 'rb') as f:
            head = f.read(2048)
        if b'\x00' in head:
            return f"[binary file, {size} bytes]"
        text = head.decode('utf-8', errors='replace')
        if ext not in _TEXT_EXTS and len(text.split()) < 5:
            return f"[non-text file, {size} bytes]"
        with open(path, 'r', encoding='utf-8',
                  errors='replace') as f:
            text = f.read(_PER_FILE_CAP + 1)
        if len(text) > _PER_FILE_CAP:
            text = text[:_PER_FILE_CAP] + "\n…[truncated]"
        return text
    except Exception as e:
        return f"[unreadable: {e}]"


def _read_dir(path):
    """Shallow tree + a small head of each text file."""
    try:
        entries = sorted(os.listdir(path))
    except Exception as e:
        return f"[unreadable directory: {e}]"
    tree = [f"{os.path.basename(path)}/ (" + ", ".join(entries[:24])
            + (", …" if len(entries) > 24 else "") + ")"]
    shown = 0
    for name in entries:
        if shown >= _DIR_FILES:
            tree.append(f"… and {len(entries) - shown} more entries")
            break
        fp = os.path.join(path, name)
        if not os.path.isfile(fp):
            continue
        if os.path.splitext(name)[1].lower() not in _TEXT_EXTS:
            continue
        tree.append(f"--- {name} ---\n" + _read_file(fp)[:600])
        shown += 1
    return "\n".join(tree)


def _git_block(subcommand):
    import subprocess
    try:
        res = subprocess.run(['git'] + subcommand, cwd=BASE_DIR,
                             capture_output=True, text=True, timeout=15)
        out = (res.stdout or '').strip()
        if res.returncode != 0 and not out:
            return (f"[git {subcommand[0]} failed: "
                    f"{(res.stderr or '').strip()[:150]}]")
        return out[:_PER_FILE_CAP] or "(no output — clean tree)"
    except FileNotFoundError:
        return "[git not installed]"
    except Exception as e:
        return f"[git failed: {e}]"


def _fetch_url(url):
    try:
        from utils.web_reader import extract_page_content
        text = extract_page_content(url)
        if text and len(text) > 80:
            return text[:_PER_FILE_CAP]
    except Exception as e:
        logger.debug("url fetch via web_reader failed: %s", e)
    return f"[could not fetch {url}]"


def _resolve(token):
    """Return (label, content) or None for one @token."""
    low = token.lower()
    if low in ('git', 'diff'):
        block = _git_block(['diff'])
        status = _git_block(['status', '--short'])
        return token, f"git status:\n{status}\n\ngit diff:\n{block}"
    if low == 'status':
        return token, _git_block(['status', '--short'])
    if low.startswith(('http://', 'https://')):
        return token, _fetch_url(token)
    path = _resolve_path(token)
    if path is None:
        return None
    if os.path.isdir(path):
        return token, _read_dir(path)
    if os.path.isfile(path):
        return token, _read_file(path)
    return None


def expand(text, total_cap=_TOTAL_CAP):
    """
    Expand every @marker in *text*.  Returns the text with a
    ``[CONTEXT EXPANSION]`` appendix (original text is preserved
    verbatim at the top).  Idempotent: already-expanded text passes
    through unchanged.
    """
    text = str(text or '')
    if not enabled() or '@' not in text or _HEADER in text:
        return text

    blocks, used, budget = [], set(), total_cap
    for match in _MARKER_RE.finditer(text):
        # Trailing sentence punctuation is not part of the reference:
        # "review @notes.md." must resolve to 'notes.md'.
        token = match.group(1).rstrip('.,;:!?\'"')
        if token in used or len(token) < 2:
            continue
        resolved = _resolve(token)
        if resolved is None:
            continue
        label, content = resolved
        used.add(token)
        if budget <= 200:
            blocks.append(f"### @{token}\n[budget exhausted]")
            break
        content = str(content)[:budget]
        budget -= len(content) + 40
        blocks.append(f"### @{label}\n{content}")

    if not blocks:
        return text
    appendix = (_HEADER + " — referenced material, injected verbatim:\n\n"
                + "\n\n".join(blocks))
    logger.info("context injection: expanded %d marker(s): %s",
                len(blocks), ", ".join(sorted(used)))
    return f"{text}\n\n{appendix}"


def notes(text):
    """Which markers would expand — for UI hints (no file reads).

    Mirrors ``expand``'s token normalization (trailing sentence
    punctuation stripped) so a hinted marker always expands.
    """
    text = str(text or '')
    if not enabled() or _HEADER in text:
        return []
    return [t.rstrip('.,;:!?\'"')
            for t in _MARKER_RE.findall(text) if len(t) >= 2]
