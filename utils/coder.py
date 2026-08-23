import os
import sys
import uuid
import shutil
import subprocess
import time
import logging

logger = logging.getLogger("Jarvis.Coder")

# Sandbox mode: 'auto' (docker when available, else local),
# 'docker' (require container) or 'local' (never containerize).
try:
    from config import Config
    _SANDBOX_MODE = getattr(Config, 'CODE_SANDBOX', 'auto')
except Exception:
    _SANDBOX_MODE = os.getenv('CODE_SANDBOX', 'auto')

SANDBOX_IMAGE = os.getenv('SANDBOX_IMAGE', 'python:3.11-slim')


class Coder:
    """
    The Architect's Hand — Advanced Code Execution Engine.
    Allows Jarvis to write and execute Python code dynamically.
    Supports iterative refinement: if code fails, can retry with fixes.

    Execution engines:
      - docker: generated code runs in an isolated container
        (--network none, CPU/memory caps, read-only workspace mount)
      - local: direct subprocess (legacy behavior)

    Selection via CODE_SANDBOX env ('auto'|'docker'|'local', default
    auto).  In auto mode a missing daemon or missing image silently
    falls back to local execution.
    """

    def __init__(self, workspace_dir="workspace"):
        self.workspace_dir = workspace_dir
        if not os.path.exists(self.workspace_dir):
            os.makedirs(self.workspace_dir)
        self._docker_ok = None   # lazy tri-state probe cache

    # ------------------------------------------------------------------ #
    # Safety
    # ------------------------------------------------------------------ #
    def validate_safety(self, code_string):
        """Filter to prevent catastrophic or destructive commands."""
        forbidden_terms = [
            "rm -rf /",
            "rm -rf ~",
            "shutil.rmtree('/",
            "shutil.rmtree(\"/",
            "os.system('format",
            "os.system(\"format",
            "mkfs",
            ":(){ :|:& };:",  # Fork bomb
            "os.system('rm",
            "os.system(\"rm",
            "subprocess.run(['rm'",
            "subprocess.call(['rm'",
            "os.remove('/')",
            "os.rmdir('/')",
        ]

        for term in forbidden_terms:
            if term in code_string:
                return False, f"Safety Protocol: Forbidden command '{term}' detected."
        return True, "Safe"

    # ------------------------------------------------------------------ #
    # Engine selection
    # ------------------------------------------------------------------ #
    def _use_docker(self):
        """Decide whether to run this execution inside Docker."""
        mode = (_SANDBOX_MODE or 'auto').lower()
        if mode == 'local':
            return False
        available = self._docker_available()
        if mode == 'docker':
            return available
        # auto: prefer container whenever the daemon answers
        return available

    def _docker_available(self):
        """Probe the Docker daemon once per process."""
        if self._docker_ok is not None:
            return self._docker_ok
        if not shutil.which('docker'):
            self._docker_ok = False
            return False
        try:
            probe = subprocess.run(
                ['docker', 'info', '--format', '{{.ServerVersion}}'],
                capture_output=True, text=True, timeout=8,
            )
            self._docker_ok = (probe.returncode == 0)
        except Exception:
            self._docker_ok = False
        if not self._docker_ok:
            logger.info("Coder: Docker not usable — using local engine.")
        return self._docker_ok

    # ------------------------------------------------------------------ #
    # Execution
    # ------------------------------------------------------------------ #
    def _run_local(self, filename, timeout):
        result = subprocess.run(
            [sys.executable, filename],
            cwd=self.workspace_dir,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        return {
            'success': (result.returncode == 0),
            'returncode': result.returncode,
            'stdout': result.stdout or '',
            'stderr': result.stderr or '',
            'engine': 'local',
        }

    def _run_docker(self, filename, timeout):
        ws_abs = os.path.abspath(self.workspace_dir)
        # Docker Desktop on macOS only shares /Users, /tmp and /private/tmp by default.
        # Workspaces under /var/folders (mkdtemp) are not mountable — fall back to local.
        if _SANDBOX_MODE == 'auto':
            shared_prefixes = ('/Users/', '/tmp/', '/private/tmp/', '/private/var/folders/')
            # /var/folders is symlink to /private/var/folders
            real_ws = os.path.realpath(ws_abs)
            if not any(real_ws.startswith(p) or ws_abs.startswith(p) for p in shared_prefixes):
                # Check if Docker would actually share this path — be conservative
                # for temp dirs outside /tmp
                if '/var/folders/' in real_ws and not real_ws.startswith('/private/tmp'):
                    logger.info(f"Coder: workspace {real_ws} not Docker-shareable — using local engine.")
                    return self._run_local(filename, timeout)

        cmd = [
            'docker', 'run', '--rm',
            # Namespace isolation: no network, no capabilities, no
            # privilege escalation, read-only root filesystem.
            '--network', 'none',
            '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges',
            '--read-only',
            '--tmpfs', '/tmp:rw,size=32m,noexec,nosuid',
            '--user', '65534:65534',          # nobody:nogroup
            '--cpus', '1',
            '--memory', '256m',
            '--pids-limit', '128',
            '-v', f'{ws_abs}:/sandbox:ro',
            '-w', '/sandbox',
            SANDBOX_IMAGE,
            'python', f'/sandbox/{filename}',
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        stderr = result.stderr or ''
        success = (result.returncode == 0)

        # Auto mode: any mount/image failure → transparent local fallback
        if not success and _SANDBOX_MODE == 'auto':
            lower_err = stderr.lower()
            if ('unable to find image' in stderr
                or 'pull access denied' in lower_err
                or "can't open file" in lower_err
                or 'no such file' in lower_err):
                logger.warning(f"Coder: docker run failed ({stderr[:120]}) — falling back to local engine.")
                res = self._run_local(filename, timeout)
                # Preserve original docker error for debugging if local also fails
                if not res['success']:
                    res['stderr'] = (res['stderr'] + f"\n[docker fallback from: {stderr[:200]}]").strip()
                return res

        return {
            'success': success,
            'returncode': result.returncode,
            'stdout': result.stdout or '',
            'stderr': stderr,
            'engine': 'docker',
        }

    # ------------------------------------------------------------------ #
    # AST pre-execution scanner (defense in depth)
    # ------------------------------------------------------------------ #
    _SCAN_FORBIDDEN_MODULES = frozenset({
        'subprocess', 'socket', 'ctypes', 'multiprocessing',
        'http.client', 'urllib.request', 'ftplib', 'telnetlib',
        'smtplib', 'pickle', 'dill', 'shutil',
    })
    _SCAN_DANGEROUS_ATTRS = frozenset({
        'system', 'popen', 'execv', 'execve', 'spawn', 'fork',
        'setuid', 'setgid', 'chown', 'chmod',
    })

    @classmethod
    def ast_scan(cls, code_string):
        """
        Static analysis of generated code before execution.

        Blocks network/process/persistence primitives outright.  File
        WRITES are allowed only to /tmp when running unsandboxed; inside
        the docker sandbox writes are harmless anyway but we stay strict
        for defense in depth.

        Returns list of violation strings (empty = clean).
        Set JARVIS_STRICT_SCAN=0 to disable (not recommended).
        """
        if os.getenv('JARVIS_STRICT_SCAN', '1') == '0':
            return []
        import ast as _ast
        violations = []
        try:
            tree = _ast.parse(code_string)
        except SyntaxError as e:
            return [f"syntax error: {e}"]     # let real exec surface it

        for node in _ast.walk(tree):
            # import subprocess / from socket import ...
            if isinstance(node, _ast.Import):
                for alias in node.names:
                    root = alias.name.split('.')[0]
                    if alias.name in cls._SCAN_FORBIDDEN_MODULES \
                            or root in cls._SCAN_FORBIDDEN_MODULES:
                        violations.append(
                            f"forbidden import: {alias.name}")
            elif isinstance(node, _ast.ImportFrom):
                mod = node.module or ''
                root = mod.split('.')[0]
                if mod in cls._SCAN_FORBIDDEN_MODULES \
                        or root in cls._SCAN_FORBIDDEN_MODULES:
                    violations.append(f"forbidden import: {mod}")

            # os.system / os.popen / eval-of-input style calls
            elif isinstance(node, _ast.Call):
                fn = node.func
                if isinstance(fn, _ast.Attribute) \
                        and fn.attr in cls._SCAN_DANGEROUS_ATTRS:
                    violations.append(
                        f"forbidden call: ...{fn.attr}()")
                elif isinstance(fn, _ast.Name) \
                        and fn.id in ('eval', 'exec', 'compile'):
                    violations.append(f"forbidden builtin: {fn.id}()")

            # dunder abuse (__import__, __builtins__, __subclasses__…)
            elif isinstance(node, _ast.Attribute) \
                    and node.attr.startswith('__') \
                    and node.attr.endswith('__'):
                violations.append(f"dunder access: {node.attr}")

        # dedupe preserving order
        seen, out = set(), []
        for v in violations:
            if v not in seen:
                seen.add(v)
                out.append(v)
        return out[:10]

    def _run_python(self, code_string, timeout=30):
        """
        Core runner: writes code to the workspace and executes it.

        Returns a structured dict — success is determined by the process
        return code, NOT by sniffing output text (stderr warnings or
        legitimately printing "Error..." no longer count as failure).
        """
        # 1. Safety Check (keyword blocklist)
        is_safe, message = self.validate_safety(code_string)
        if not is_safe:
            return {
                'success': False,
                'returncode': -1,
                'stdout': '',
                'stderr': message,
            }

        # 1b. AST pre-execution scan (defense in depth — runs even when
        # the docker sandbox will isolate the code afterwards).
        violations = self.ast_scan(code_string)
        if violations:
            return {
                'success': False,
                'returncode': -1,
                'stdout': '',
                'stderr': ("Safety Protocol: AST scanner blocked this "
                           "script:\n- " + "\n- ".join(violations)),
            }

        # 2. Prepare Workspace
        # Unique name: parallel steps may execute concurrently, so a
        # second-resolution timestamp alone can collide.
        filename = f"dynamic_task_{uuid.uuid4().hex[:12]}.py"
        file_path = os.path.join(self.workspace_dir, filename)

        try:
            # 3. Write Code
            with open(file_path, "w") as f:
                f.write(code_string)
            # Ensure Docker's nobody user can read temp workspaces
            try:
                os.chmod(file_path, 0o644)
                os.chmod(self.workspace_dir, 0o755)
            except Exception:
                pass

            # 4. Execute (containerized when possible)
            use_docker = self._use_docker()
            runner = self._run_docker if use_docker else self._run_local
            res = runner(filename, timeout)

            if not res['stdout'].strip() and not res['stderr'].strip():
                res['stderr'] = ''  # clean no-output run
            return res

        except subprocess.TimeoutExpired:
            return {
                'success': False,
                'returncode': -1,
                'stdout': '',
                'stderr': f"Code execution timed out (limit: {timeout}s).",
            }
        except Exception as e:
            return {
                'success': False,
                'returncode': -1,
                'stdout': '',
                'stderr': f"Execution Error: {str(e)}",
            }
        finally:
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
            except OSError:
                pass

    @staticmethod
    def _format_result(res):
        """Render a structured run result as the legacy string format."""
        parts = []
        if res['stdout']:
            parts.append(f"Output:\n{res['stdout']}")
        if res['stderr']:
            parts.append(f"Errors:\n{res['stderr']}")
        if not parts:
            return "Code executed successfully (No Output)."
        return "\n".join(parts).strip()

    def execute_python(self, code_string, timeout=30):
        """Writes code to a file and executes it.

        Returns a human-readable string combining stdout/stderr.
        Success/failure should be checked via execute_with_retry().
        """
        res = self._run_python(code_string, timeout=timeout)
        return self._format_result(res)

    def execute_with_retry(self, code_string, max_retries=2, timeout=30):
        """Execute code and report structured success based on exit code.

        Returns {'success': bool, 'output': str, 'stdout', 'stderr',
                 'retries_remaining'}.
        """
        res = self._run_python(code_string, timeout=timeout)
        output = self._format_result(res)
        return {
            'success': res['success'],
            'output': output,
            'stdout': res['stdout'],
            'stderr': res['stderr'],
            'returncode': res['returncode'],
            'retries_remaining': max_retries,
        }


if __name__ == "__main__":
    # Test
    coder = Coder()
    print(coder.execute_python("print('Hello from The Architect')"))
    print(coder.execute_python("print(1/0)"))  # Should show error
    print(coder.execute_with_retry("x = [1,2,3]; print(x[5])"))  # IndexError
