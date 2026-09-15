"""
J.A.R.V.I.S. — Post-Edit Semantic Diagnostics (Hermes parity)
=============================================================

Hooks language-tooling checks in IMMEDIATELY after file manipulation
so the agent learns its edit broke something before the user does:

    tier 1  native checkers — python: compile()/AST (instant, local);
            js/mjs: node --check; ts: tsc --noEmit when present
    tier 2  a real LSP — set JARVIS_LSP_CMD to a command template that
            takes {file} (e.g. "pylsp --check {file}") and it wins over
            tier 1 whenever the binary exists

Results carry line numbers where the checker provides them, so the
agent's next turn can fix the exact spot.  ``check_code`` exists for
snippets that were never written to disk.

``JARVIS_LSP_DIAGNOSTICS=0`` disables all post-edit checking.
"""

import os
import re
import json
import logging
import shutil
import subprocess

logger = logging.getLogger("Jarvis.LSP")

_TIMEOUT = 20


def enabled():
    return os.getenv('JARVIS_LSP_DIAGNOSTICS', '1') != '0'


def _parse_py_error(text):
    """Extract line/col + message from a python traceback line."""
    m = re.search(r'line (\d+)', text)
    line = int(m.group(1)) if m else None
    msg = text.strip().splitlines()[-1] if text.strip() else 'syntax error'
    return line, msg[:300]


def _check_python(source, filename='<code>'):
    issues = []
    try:
        compile(source, filename, 'exec')
    except SyntaxError as e:
        issues.append({'line': e.lineno, 'message': e.msg or str(e),
                       'severity': 'error'})
    except ValueError as e:
        issues.append({'line': None, 'message': str(e), 'severity': 'error'})
    return issues


def _check_via_cli(argv):
    """Run an external checker; non-zero exit → one issue."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=_TIMEOUT)
    except FileNotFoundError:
        return None                    # checker not installed
    except subprocess.TimeoutExpired:
        return [{'line': None, 'message': 'checker timed out',
                 'severity': 'warning'}]
    if proc.returncode == 0:
        return []
    msg = (proc.stderr or proc.stdout or 'check failed').strip()
    line = None
    m = re.search(r':(\d+):', msg)
    if m:
        line = int(m.group(1))
    return [{'line': line, 'message': msg.splitlines()[-1][:400]
             if msg else 'check failed', 'severity': 'error'}]


def _lsp_command(path):
    """Resolve the JARVIS_LSP_CMD template for this file, or None."""
    template = os.getenv('JARVIS_LSP_CMD')
    if not template:
        return None
    import shlex
    try:
        argv = [tok.replace('{file}', path)
                for tok in shlex.split(template)]
    except ValueError:
        return None
    return argv if argv and shutil.which(argv[0]) else None


def check_code(source, language='python'):
    """Check an in-memory snippet (no file needed)."""
    if not enabled():
        return {'ok': True, 'issues': [], 'checker': 'disabled'}
    language = (language or 'python').lower()
    if language in ('python', 'py', ''):
        issues = _check_python(source)
        return {'ok': not issues, 'issues': issues, 'checker': 'compile'}
    return {'ok': True, 'issues': [], 'checker': 'none'}


def check_file(path):
    """
    Diagnose one file after an edit.
    Returns {'ok', 'issues': [{'line', 'message', 'severity'}],
             'checker'} — never raises.
    """
    path = str(path or '')
    if not enabled() or not os.path.isfile(path):
        return {'ok': True, 'issues': [], 'checker': 'skipped'}
    ext = os.path.splitext(path)[1].lower()
    try:
        # tier 2: configured LSP command wins when its binary exists
        lsp = _lsp_command(path)
        if lsp:
            issues = _check_via_cli(lsp)
            if issues is not None:
                return {'ok': not issues, 'issues': issues,
                        'checker': lsp[0]}

        # tier 1: language-native checkers
        if ext == '.py':
            with open(path, 'r', encoding='utf-8',
                      errors='replace') as f:
                issues = _check_python(f.read(), path)
            return {'ok': not issues, 'issues': issues,
                    'checker': 'compile'}
        if ext in ('.js', '.mjs', '.cjs'):
            if shutil.which('node'):
                issues = _check_via_cli(['node', '--check', path])
                if issues is not None:
                    return {'ok': not issues, 'issues': issues,
                            'checker': 'node'}
            # No node → the file was NOT validated.  Report that instead
            # of a silent all-clear, so a broken JS file isn't confirmed
            # "clean" by the agent (the module's whole purpose).
            return {'ok': True, 'issues': [{
                'line': None, 'severity': 'warning',
                'message': (f"{os.path.basename(path)} was NOT validated: "
                            "no 'node' binary available for JS syntax "
                            "checks.")}], 'checker': 'none'}
        elif ext in ('.ts', '.tsx'):
            if shutil.which('tsc'):
                issues = _check_via_cli(['tsc', '--noEmit', path])
                if issues is not None:
                    return {'ok': not issues, 'issues': issues,
                            'checker': 'tsc'}
            return {'ok': True, 'issues': [{
                'line': None, 'severity': 'warning',
                'message': (f"{os.path.basename(path)} was NOT validated: "
                            "no 'tsc' binary available for TypeScript "
                            "checks.")}], 'checker': 'none'}
        elif ext == '.json':
            with open(path, 'r', encoding='utf-8',
                      errors='replace') as f:
                json.load(f)
            return {'ok': True, 'issues': [], 'checker': 'json'}
        # Unrecognised extension: no checker in our set applies — treat
        # as clean-by-design (render → '').
        return {'ok': True, 'issues': [], 'checker': 'none'}
    except json.JSONDecodeError as e:
        return {'ok': False,
                'issues': [{'line': e.lineno, 'message': str(e),
                            'severity': 'error'}], 'checker': 'json'}
    except Exception as e:
        logger.debug("diagnostics failed for %s: %s", path, e)
        # Operational failure (unreadable file, decoder/checker blew up):
        # the file was NOT validated, so never report it clean.
        return {'ok': True, 'issues': [{
            'line': None, 'severity': 'warning',
            'message': (f"{os.path.basename(path)} could not be validated "
                        f"({e.__class__.__name__}).")}], 'checker': 'error'}


def render(result):
    """
    Render a check result for the agent's tool output: '' when clean, a
    DIAGNOSTICS block when the edit broke something, and an explicit
    NOTICE when validation could NOT run (absent checker / internal
    failure) — a broken-but-unchecked file is never reported clean.
    """
    if not result:
        return ''
    issues = result.get('issues') or []
    if result.get('ok'):
        if not issues:
            return ''
        # Non-blocking notes only (e.g. "no checker available"): the
        # edit is not claimed broken, but it must not be confirmed clean
        # either.
        notes = [i.get('message', '') for i in issues[:3] if i.get('message')]
        return "NOTICE: " + " | ".join(notes) if notes else ''
    lines = ["DIAGNOSTICS — your edit broke this file. Fix it now:"]
    for issue in issues[:5]:
        where = f"line {issue['line']}: " if issue.get('line') else ''
        lines.append(f"- {where}{issue.get('message', '')}")
    return "\n".join(lines)


def check_and_render(path):
    """check_file + render in one call (the executor's hot path)."""
    return render(check_file(path))
