"""
J.A.R.V.I.S. — Sandboxed Python RPC Runtime (Hermes parity)
============================================================

Executes complex tasks by writing isolated Python on the fly and
calling it over lightweight RPC, collapsing multi-step execution
cycles into single token steps.

Instead of spawning a fresh interpreter per snippet (the Coder
engine's model), ``PythonRPC`` keeps ONE worker process alive for the
whole session and speaks newline-delimited JSON to it:

    Jarvis → worker:  {"id": 7, "code": "x = 41; x += 1"}
    worker → Jarvis:  {"id": 7, "ok": true, "stdout": "", "repr": "42"}

The worker's namespace PERSISTS between calls — the agent can grow a
computation across many tool steps (load data once, then query it
repeatedly) without re-paying setup.  Each call still passes the
Coder's keyword + AST safety scanners, is timeout-guarded, and the
whole session can be reset or killed at any time.

``JARVIS_PYTHON_RPC=0`` disables the RPC layer (falls back to the
one-shot Coder engine).
"""

import os
import sys
import json
import uuid
import threading
import subprocess
import logging

logger = logging.getLogger("Jarvis.PythonRPC")

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The worker script the RPC process runs.  Pure stdlib; reads JSON
# lines from stdin, executes code in a persistent namespace, replies
# with a JSON line.  Never exits on errors — reports them.
_WORKER_SOURCE = r'''
import sys, json, io, traceback

NS = {"__name__": "__jarvis_rpc__"}

while True:
    line = sys.stdin.readline()
    if not line:
        break
    try:
        req = json.loads(line)
    except ValueError:
        continue
    rid = req.get("id")
    code = req.get("code") or ""
    buf = io.StringIO()
    ok, rep = True, ""
    real_stdout = sys.stdout
    sys.stdout = buf
    try:
        compiled = compile(code, "<rpc>", "exec")
        exec(compiled, NS)
        for name in ("result", "_", "out"):
            if name in NS:
                rep = repr(NS[name])[:2000]
                break
    except SystemExit as e:
        ok, rep = True, "exited(%s)" % (e.code,)
    except BaseException:
        ok = False
        rep = traceback.format_exc(limit=6)[:4000]
    finally:
        sys.stdout = real_stdout
    reply = {"id": rid, "ok": ok,
             "stdout": buf.getvalue()[:20000], "repr": rep}
    sys.stdout.write(json.dumps(reply) + "\n")
    sys.stdout.flush()
'''


def enabled():
    return os.getenv('JARVIS_PYTHON_RPC', '1') != '0'


def _safety_ok(code):
    """Reuse the Coder engine's scanners (keyword blocklist + AST)."""
    try:
        from utils.coder import Coder
        ok, msg = Coder().validate_safety(code)
        if not ok:
            return False, msg
        violations = Coder.ast_scan(code)
        if violations:
            return False, ("AST scanner blocked this code:\n- "
                           + "\n- ".join(violations))
    except Exception:
        pass
    return True, ''


class PythonRPC:
    """One persistent sandboxed Python worker process."""

    def __init__(self):
        self._proc = None
        self._lock = threading.RLock()
        self._calls = 0
        self._worker_path = None

    # ------------------------------------------------------------------ #
    # Process lifecycle
    # ------------------------------------------------------------------ #
    def _ensure_worker(self):
        if self._proc is not None and self._proc.poll() is None:
            return
        if self._worker_path is None:
            import tempfile
            fd, self._worker_path = tempfile.mkstemp(
                suffix='.py', prefix='jarvis_rpc_')
            with os.fdopen(fd, 'w') as f:
                f.write(_WORKER_SOURCE)
        self._proc = subprocess.Popen(
            [sys.executable, '-u', self._worker_path],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1)
        logger.info("python RPC worker started (pid %d)", self._proc.pid)

    def close(self):
        """
        Terminate the worker (called on shutdown / reset).  The process
        is always reaped (no zombie) and hard-killed if it ignores
        SIGTERM — a runaway sandbox must never be left behind.
        """
        with self._lock:
            proc, self._proc = self._proc, None
            if proc is None:
                return
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                except Exception:
                    pass

    def reset(self):
        """Fresh namespace — restart the worker process."""
        self.close()
        with self._lock:
            self._ensure_worker()
        return "RPC session reset — namespace cleared."

    # ------------------------------------------------------------------ #
    # RPC call
    # ------------------------------------------------------------------ #
    def call(self, code, timeout=30):
        """
        Execute *code* in the persistent namespace.  Returns
        {'success', 'stdout', 'repr', 'error'} — never raises.
        """
        code = str(code or '').strip()
        if not code:
            return {'success': False, 'stdout': '', 'repr': '',
                    'error': 'no code'}
        if not enabled():
            # Degraded mode: one-shot execution via the Coder engine
            from utils.coder import Coder
            res = Coder().execute_with_retry(code, timeout=timeout)
            return {'success': res.get('success', False),
                    'stdout': res.get('stdout', ''),
                    'repr': '', 'error': res.get('stderr', '')}

        ok, msg = _safety_ok(code)
        if not ok:
            return {'success': False, 'stdout': '', 'repr': '',
                    'error': msg}

        rid = uuid.uuid4().hex[:10]
        with self._lock:
            try:
                self._ensure_worker()
                self._proc.stdin.write(
                    json.dumps({'id': rid, 'code': code}) + "\n")
                self._proc.stdin.flush()
            except Exception as e:
                self.close()
                return {'success': False, 'stdout': '', 'repr': '',
                        'error': f'worker pipe broken: {e}'}

            # Read the matching reply under a watchdog: a hung snippet
            # must not wedge Jarvis.  The reader runs in a helper thread
            # so the main thread can enforce the timeout.  It reads the
            # pipe captured NOW — a reader left over from a timed-out
            # call keeps reading the OLD pipe, which close() closes when
            # it reaps the worker, so it can never swallow replies from
            # a recycled worker.
            proc = self._proc
            pipe = proc.stdout
            reply_box = {}

            def _read():
                try:
                    while True:
                        line = pipe.readline()
                        if not line:
                            reply_box['error'] = 'worker died'
                            return
                        data = json.loads(line)
                        if data.get('id') == rid:
                            reply_box['data'] = data
                            return
                        # stale reply from a timed-out call — skip it
                except Exception as e:
                    reply_box['error'] = str(e)

            reader = threading.Thread(target=_read, daemon=True,
                                      name="rpc-reader")
            reader.start()
            reader.join(timeout=max(5, timeout))
            self._calls += 1

        if 'data' not in reply_box:
            # Timed out or died: the worker is now untrusted — recycle
            # it so the next call starts clean.
            self.close()
            return {'success': False, 'stdout': '', 'repr': '',
                    'error': f'RPC call timed out or crashed after '
                             f'{timeout}s — session recycled'}

        data = reply_box['data']
        out = {'success': bool(data.get('ok')),
               'stdout': data.get('stdout', ''),
               'repr': data.get('repr', ''),
               'error': '' if data.get('ok') else data.get('repr', '')}
        return out

    def format(self, code, timeout=30):
        """Human-readable rendering of a call (for executor actions)."""
        res = self.call(code, timeout=timeout)
        parts = []
        if res.get('stdout'):
            parts.append(res['stdout'].rstrip())
        if res.get('repr'):
            parts.append(f"→ {res['repr']}")
        if res.get('error'):
            parts.append(f"ERROR:\n{res['error']}")
        if not parts:
            parts.append('(no output)')
        return "\n".join(parts)

    def stats(self):
        return {'calls': self._calls,
                'alive': bool(self._proc is not None
                              and self._proc.poll() is None),
                'enabled': enabled()}


# --------------------------------------------------------------------- #
# Singleton — one worker per Jarvis process
# --------------------------------------------------------------------- #

_singleton = None
_singleton_lock = threading.Lock()


def get_rpc():
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = PythonRPC()
        return _singleton


def _reset_singleton():
    """Test helper — also kills any live worker."""
    global _singleton
    with _singleton_lock:
        if _singleton is not None:
            try:
                _singleton.close()
            except Exception:
                pass
        _singleton = None


# --------------------------------------------------------------------- #
# CLI self-test
# --------------------------------------------------------------------- #

if __name__ == '__main__':
    rpc = PythonRPC()
    print(rpc.format("data = [3, 1, 2]; data.sort(); data"))
    print(rpc.format("sum(data)"))          # namespace persists: 6
    print(rpc.format("1 / 0"))              # reported, not fatal
    print(rpc.format("import subprocess"))  # blocked by AST scanner
    rpc.reset()
    print(rpc.format("'data' in dir() or 'namespace cleared'"))
    rpc.close()
