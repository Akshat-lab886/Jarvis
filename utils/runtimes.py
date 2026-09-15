"""
J.A.R.V.I.S. — Multi-Backend Runtime Execution (Hermes parity)
==============================================================

One API, seven execution environments:

    local        direct subprocess on this machine (always available)
    docker       containerized execution (network-off, resource-capped)
    ssh          remote host over `ssh` (BatchMode, key auth)
    daytona      Daytona sandbox CLI (`daytona exec`)
    singularity  Apptainer/Singularity container (`apptainer exec`)
    vercel       Vercel sandbox CLI (`vercel sandbox`)
    modal        serverless Modal job (`modal run`)

Every non-local backend is activated purely by configuration — an env
var (or CLI presence) and nothing else.  No new dependencies: each
adapter shells out to the official CLI, and an unconfigured backend
fails fast with a message that says exactly what to set.  ``auto``
picks the safest available: docker for code, local for shell.

    from utils.runtimes import run
    run(code="print('hi')", backend='docker')
    run(command="df -h", backend='ssh')

``JARVIS_RUNTIMES=0`` disables everything but local.
"""

import os
import shlex
import shutil
import logging
import tempfile
import subprocess

logger = logging.getLogger("Jarvis.Runtimes")

_TIMEOUT_DEFAULT = 60
_CODE_IMAGE = os.getenv('JARVIS_SANDBOX_IMAGE', 'python:3.11-slim')


def enabled():
    return os.getenv('JARVIS_RUNTIMES', '1') != '0'


# --------------------------------------------------------------------- #
# Backend registry
# --------------------------------------------------------------------- #

def _cli_available(cmd):
    return shutil.which(cmd) is not None


def backend_configured(backend):
    """Is this backend usable right now?  (cheap probe, no execution)"""
    if not enabled():
        return backend in ('local', 'auto')
    if backend in ('local', 'auto'):
        return True
    if backend == 'docker':
        return _cli_available('docker')
    if backend == 'ssh':
        return bool(os.getenv('JARVIS_SSH_HOST')) and \
            _cli_available('ssh')
    if backend == 'daytona':
        return _cli_available('daytona')
    if backend == 'singularity':
        return _cli_available('apptainer') or \
            _cli_available('singularity')
    if backend == 'vercel':
        return _cli_available('vercel')
    if backend == 'modal':
        return _cli_available('modal')
    return False


def available_backends():
    """Names of every usable backend, local first."""
    names = ['local', 'docker', 'ssh', 'daytona', 'singularity',
             'vercel', 'modal']
    return [n for n in names if backend_configured(n)]


def _wrap_timeout(timeout):
    try:
        return max(5, int(timeout or _TIMEOUT_DEFAULT))
    except (TypeError, ValueError):
        return _TIMEOUT_DEFAULT


def _result(proc, backend, timeout):
    return {
        'success': proc.returncode == 0,
        'returncode': proc.returncode,
        'stdout': (proc.stdout or '')[:20000],
        'stderr': (proc.stderr or '')[:8000],
        'backend': backend,
        'timed_out': False,
    }


def _run_argv(argv, backend, timeout, cwd=None, env=None):
    """Shared subprocess runner with timeout → structured result."""
    timeout = _wrap_timeout(timeout)
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
            cwd=cwd, env=env)
        return _result(proc, backend, timeout)
    except subprocess.TimeoutExpired:
        return {'success': False, 'returncode': -1, 'stdout': '',
                'stderr': f'timed out after {timeout}s',
                'backend': backend, 'timed_out': True}
    except FileNotFoundError:
        return {'success': False, 'returncode': -1, 'stdout': '',
                'stderr': f'backend CLI not found for {backend}',
                'backend': backend, 'timed_out': False}
    except Exception as e:
        return {'success': False, 'returncode': -1, 'stdout': '',
                'stderr': str(e)[:500], 'backend': backend,
                'timed_out': False}


# --------------------------------------------------------------------- #
# Command execution per backend
# --------------------------------------------------------------------- #

def _ssh_argv(command):
    host = os.environ['JARVIS_SSH_HOST']
    user = os.getenv('JARVIS_SSH_USER')
    target = f"{user}@{host}" if user else host
    argv = ['ssh', '-o', 'BatchMode=yes', '-o',
            'ConnectTimeout=10']
    port = os.getenv('JARVIS_SSH_PORT')
    if port:
        argv += ['-p', str(port)]
    argv += [target, command]
    return argv


def run_command(command, backend='auto', timeout=_TIMEOUT_DEFAULT):
    """
    Execute a SHELL command on the chosen backend.
    Returns {'success', 'stdout', 'stderr', 'backend', ...}.
    """
    command = str(command or '').strip()
    if not command:
        return {'success': False, 'stdout': '', 'stderr': 'no command',
                'backend': backend, 'timed_out': False}
    if not enabled():
        backend = 'local'

    if backend in ('auto', 'docker', 'local'):
        # Shell commands stay local unless docker is explicitly chosen
        if backend == 'auto':
            backend = 'local'
        if backend == 'docker':
            if not backend_configured('docker'):
                return {'success': False, 'stdout': '',
                        'stderr': 'docker CLI/daemon not available',
                        'backend': 'docker', 'timed_out': False}
            return _run_argv(
                ['docker', 'run', '--rm', '--network', 'none',
                 '--cpus', '1', '--memory', '256m',
                 _CODE_IMAGE, 'sh', '-c', command],
                'docker', timeout)
        # local: a direct shell on this machine (always available)
        return _run_argv(['sh', '-c', command], 'local', timeout)

    if backend == 'ssh':
        if not os.getenv('JARVIS_SSH_HOST'):
            return {'success': False, 'stdout': '',
                    'stderr': 'JARVIS_SSH_HOST not set — configure the '
                              'remote endpoint in .env',
                    'backend': 'ssh', 'timed_out': False}
        return _run_argv(_ssh_argv(command), 'ssh', timeout)

    if backend == 'daytona':
        if not backend_configured('daytona'):
            return {'success': False, 'stdout': '',
                    'stderr': 'daytona CLI not installed',
                    'backend': 'daytona', 'timed_out': False}
        workspace = os.getenv('JARVIS_DAYTONA_WS', 'default')
        return _run_argv(['daytona', 'exec', '-w', workspace, command],
                         'daytona', timeout)

    if backend == 'singularity':
        cli = 'apptainer' if _cli_available('apptainer') else 'singularity'
        if not _cli_available(cli):
            return {'success': False, 'stdout': '',
                    'stderr': 'apptainer/singularity not installed',
                    'backend': 'singularity', 'timed_out': False}
        img = os.getenv('JARVIS_SINGULARITY_IMAGE', 'docker://python:3.11-slim')
        return _run_argv([cli, 'exec', img, 'sh', '-c', command],
                         'singularity', timeout)

    if backend == 'vercel':
        if not backend_configured('vercel'):
            return {'success': False, 'stdout': '',
                    'stderr': 'vercel CLI not installed',
                    'backend': 'vercel', 'timed_out': False}
        return _run_argv(['vercel', 'sandbox', 'exec', '--', command],
                         'vercel', timeout)

    if backend == 'modal':
        if not backend_configured('modal'):
            return {'success': False, 'stdout': '',
                    'stderr': 'modal CLI not installed '
                              '(pip install modal)',
                    'backend': 'modal', 'timed_out': False}
        entry = os.getenv('JARVIS_MODAL_ENTRY')
        if not entry:
            return {'success': False, 'stdout': '',
                    'stderr': 'JARVIS_MODAL_ENTRY not set — point it at '
                              'your modal entrypoint module',
                    'backend': 'modal', 'timed_out': False}
        try:
            args = shlex.split(command)
        except ValueError as e:
            # A heredoc-wrapped code snippet can carry an unmatched quote
            # that shlex cannot parse — fail with a structured result
            # instead of letting ValueError escape the public API.
            return {'success': False, 'stdout': '',
                    'stderr': f'modal command not shell-parseable: {e}',
                    'backend': 'modal', 'timed_out': False}
        return _run_argv(['modal', 'run', entry, '--'] + args,
                         'modal', timeout)

    return {'success': False, 'stdout': '',
            'stderr': f'unknown backend {backend!r} — use one of '
                      f'{available_backends()}',
            'backend': backend, 'timed_out': False}


# --------------------------------------------------------------------- #
# Code execution per backend
# --------------------------------------------------------------------- #

def run_code(code, backend='auto', timeout=_TIMEOUT_DEFAULT):
    """
    Execute a PYTHON snippet on the chosen backend.  The snippet is
    written to a temp file and run as `python <file>` (or its
    container/remote equivalent).  Local code execution reuses the
    Coder engine's safety scanner.
    """
    code = str(code or '').strip()
    if not code:
        return {'success': False, 'stdout': '', 'stderr': 'no code',
                'backend': backend, 'timed_out': False}

    if backend == 'auto':
        backend = 'docker' if backend_configured('docker') else 'local'
    if not enabled():
        backend = 'local'

    if backend == 'local':
        from utils.coder import Coder
        res = Coder().execute_with_retry(code, timeout=timeout)
        return {'success': res.get('success', False),
                'stdout': res.get('stdout', ''),
                'stderr': res.get('stderr', ''),
                'backend': 'local', 'timed_out': False}

    fd, path = tempfile.mkstemp(suffix='.py', prefix='jarvis_rt_')
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(code)
        os.chmod(path, 0o644)

        if backend == 'docker':
            if not backend_configured('docker'):
                return {'success': False, 'stdout': '',
                        'stderr': 'docker CLI/daemon not available',
                        'backend': 'docker', 'timed_out': False}
            dirname = os.path.dirname(path)
            return _run_argv(
                ['docker', 'run', '--rm', '--network', 'none',
                 '--cap-drop', 'ALL', '--read-only',
                 '--tmpfs', '/tmp:rw,size=32m,noexec,nosuid',
                 '--user', '65534:65534', '--cpus', '1',
                 '--memory', '256m',
                 '-v', f'{dirname}:/sandbox:ro', '-w', '/sandbox',
                 _CODE_IMAGE, 'python', f'/sandbox/{os.path.basename(path)}'],
                'docker', timeout)

        if backend == 'ssh':
            if not os.getenv('JARVIS_SSH_HOST'):
                return {'success': False, 'stdout': '',
                        'stderr': 'JARVIS_SSH_HOST not set',
                        'backend': 'ssh', 'timed_out': False}
            # Stream the code over stdin to avoid remote temp files
            timeout_ = _wrap_timeout(timeout)
            try:
                proc = subprocess.run(
                    _ssh_argv('python3 -'), input=code,
                    capture_output=True, text=True, timeout=timeout_)
                return _result(proc, 'ssh', timeout_)
            except subprocess.TimeoutExpired:
                return {'success': False, 'returncode': -1, 'stdout': '',
                        'stderr': f'timed out after {timeout_}s',
                        'backend': 'ssh', 'timed_out': True}

        if backend in ('daytona', 'singularity', 'vercel', 'modal'):
            # Embed the script in a heredoc on the backend's shell.  Use
            # a per-call random delimiter so code that happens to contain
            # a fixed marker (e.g. "JARVIS_EOF") can never truncate the
            # script or smuggle shell into the command.
            import uuid
            delim = f"JARVIS_EOF_{uuid.uuid4().hex[:10]}"
            heredoc = f"python3 - <<'{delim}'\n{code}\n{delim}"
            return run_command(heredoc, backend=backend, timeout=timeout)

        return {'success': False, 'stdout': '',
                'stderr': f'unknown backend {backend!r}',
                'backend': backend, 'timed_out': False}
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def describe():
    """Status line for the dashboard / TUI."""
    backends = available_backends()
    ssh = ' (JARVIS_SSH_HOST set)' if os.getenv('JARVIS_SSH_HOST') else ''
    return (f"runtimes: {', '.join(backends)}{ssh} — "
            f"auto={'docker' if 'docker' in backends else 'local'}")
