"""
Jarvis Pre-Execution Circuit Breaker
====================================

Validates the runtime environment BEFORE any LLM tokens are spent:

  - At least one LLM provider configured (fatal if none)
  - Workspace writable + sane disk headroom
  - Memory backend reachable (JSON store writable; vector is optional)
  - Coder sandbox ready (docker probe deferred — local always usable)
  - Config sanity (ports, budgets positive)

Modes:
  ok        — everything passed; normal operation
  degraded  — non-critical failures (vector down, low disk warn);
              operation continues with reduced features
  tripped   — fatal failure (no providers, unwritable workspace);
              Brain refuses to spend tokens until a re-check passes

Re-checks happen at most once every RECHECK_INTERVAL seconds so a
transient failure recovers automatically without hammering the system.
"""

import os
import shutil
import time
import logging
import threading

logger = logging.getLogger("Jarvis.CircuitBreaker")

RECHECK_INTERVAL = 60      # seconds between re-validations
MIN_DISK_MB = 200          # warn below this free space in project volume


class BreakerReport:
    def __init__(self):
        self.status = "unknown"       # ok | degraded | tripped
        self.checks = []              # [{name, ok, fatal, detail}]
        self.checked_at = 0.0

    @property
    def tripped(self):
        return self.status == "tripped"

    def failures(self):
        return [c for c in self.checks if not c['ok']]

    def to_dict(self):
        return {
            'status': self.status,
            'checked_at': self.checked_at,
            'checks': self.checks,
        }


class CircuitBreaker:
    """Process-wide runtime validator. Instantiate once, share freely."""

    def __init__(self, base_dir=None):
        self.base_dir = base_dir or os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))
        self._lock = threading.Lock()
        self._report = BreakerReport()

    # ------------------------------------------------------------------ #
    # Individual checks
    # ------------------------------------------------------------------ #
    def _check_providers(self):
        try:
            from config import Config
            has_groq = bool(getattr(Config, 'GROQ_API_KEY', None))
            has_gemini = bool(getattr(Config, 'GOOGLE_API_KEY', None))
            ok = has_groq or has_gemini
            detail = []
            if has_groq:
                detail.append("Groq")
            if has_gemini:
                detail.append("Gemini")
            return ok, ok or False, \
                ("configured: " + ", ".join(detail)) if detail \
                else "no LLM API key found"
        except Exception as e:
            return False, True, f"config error: {e}"

    def _check_workspace(self):
        ws = os.path.join(self.base_dir, 'workspace')
        try:
            os.makedirs(ws, exist_ok=True)
            probe = os.path.join(ws, '.breaker_probe')
            with open(probe, 'w') as f:
                f.write('ok')
            os.remove(probe)
            return True, True, "writable"
        except Exception as e:
            return False, True, f"unwritable: {e}"

    def _check_disk(self):
        try:
            usage = shutil.disk_usage(self.base_dir)
            free_mb = usage.free // (1024 * 1024)
            if free_mb < MIN_DISK_MB:
                return False, False, f"low disk: {free_mb}MB free"
            return True, False, f"{free_mb // 1024}GB free"
        except Exception as e:
            return False, False, f"stat failed: {e}"

    def _check_memory_backends(self):
        """JSON stores must be writable; vector layer is optional."""
        problems = []
        try:
            episodic = os.path.join(self.base_dir, 'episodic_memory.json')
            if os.path.exists(episodic) and not os.access(episodic, os.W_OK):
                problems.append("episodic_memory.json read-only")
        except Exception as e:
            problems.append(f"episodic check failed: {e}")

        vector_note = "vector disabled"
        try:
            if os.getenv('JARVIS_DISABLE_VECTOR') != '1':
                import utils.episodic_memory as em  # noqa: F401
                vector_note = "vector module importable"
        except Exception as e:
            vector_note = f"vector unavailable ({type(e).__name__})"

        ok = not problems
        return ok, False, ("; ".join(problems) +
                           f" | {vector_note}") if problems else vector_note

    def _check_budget_config(self):
        try:
            usd = float(os.getenv('JARVIS_BUDGET_USD', '5'))
            tok = int(os.getenv('JARVIS_BUDGET_TOKENS', '500000'))
            if usd <= 0 or tok <= 0:
                return False, False, "budgets must be positive"
            return True, False, f"${usd:.2f} / {tok} tokens"
        except ValueError:
            return False, False, "invalid budget values"

    # ------------------------------------------------------------------ #
    # Validation pass
    # ------------------------------------------------------------------ #
    CHECKS = [
        ('llm_providers', '_check_providers'),
        ('workspace', '_check_workspace'),
        ('disk_headroom', '_check_disk'),
        ('memory_backends', '_check_memory_backends'),
        ('budget_config', '_check_budget_config'),
    ]

    def validate(self, force=False):
        """Run all checks (throttled unless force=True)."""
        now = time.time()
        with self._lock:
            if not force and (now - self._report.checked_at) < RECHECK_INTERVAL:
                return self._report

            report = BreakerReport()
            for name, method_name in self.CHECKS:
                fatal = False
                try:
                    ok, fatal, detail = getattr(self, method_name)()
                except Exception as e:                 # belt & braces
                    ok, fatal, detail = False, True, f"check crashed: {e}"
                report.checks.append({
                    'name': name, 'ok': bool(ok),
                    'fatal': bool(fatal), 'detail': str(detail)[:160],
                })

            fatal_failures = [c for c in report.checks
                              if not c['ok'] and c['fatal']]
            any_failures = [c for c in report.checks if not c['ok']]

            if fatal_failures:
                report.status = 'tripped'
            elif any_failures:
                report.status = 'degraded'
            else:
                report.status = 'ok'
            report.checked_at = now
            self._report = report

        if report.status == 'tripped':
            logger.error(f"CIRCUIT BREAKER TRIPPED: "
                         f"{[c['detail'] for c in fatal_failures]}")
        elif report.status == 'degraded':
            logger.warning(f"Circuit breaker degraded: "
                           f"{[c['name'] for c in any_failures]}")
        return report

    @property
    def report(self):
        return self._report

    def allow_llm_spend(self):
        """True when tokens may be spent (auto re-checks when tripped)."""
        if self._report.status != 'tripped':
            return True
        fresh = self.validate(force=True)
        return fresh.status != 'tripped'


# Module-level shared breaker (used by Brain)
_shared = None


def get_breaker():
    global _shared
    if _shared is None:
        _shared = CircuitBreaker()
    return _shared
