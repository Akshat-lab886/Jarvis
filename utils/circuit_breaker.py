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
            detail = []
            if bool(getattr(Config, 'GROQ_API_KEY', None)):
                detail.append("Groq")
            if bool(getattr(Config, 'GOOGLE_API_KEY', None)):
                detail.append("Gemini")
            # BYOK fleet: keystore overlay (dashboard-added keys) + env
            # for every other provider.  The old Groq/Gemini-only check
            # tripped the breaker for OpenAI/Anthropic-only users even
            # though Brain.complete serves them fine via the router —
            # which also starved the RLM reflector/archivist (they call
            # Brain.complete, which refuses to spend when tripped).
            try:
                from utils.llm.keystore import get_keystore
                ks = get_keystore()
                for _name, _label, _envs in (
                        ('openai', 'OpenAI', ('OPENAI_API_KEY',)),
                        ('anthropic', 'Anthropic',
                         ('ANTHROPIC_API_KEY',)),
                        ('openrouter', 'OpenRouter',
                         ('OPENROUTER_API_KEY',)),
                        ('deepseek', 'DeepSeek', ('DEEPSEEK_API_KEY',)),
                        ('custom', 'Custom',
                         ('CUSTOM_OPENAI_API_KEY',
                          'CUSTOM_OPENAI_BASE_URL',))):
                    try:
                        _has = bool(ks.get(_name))
                    except Exception:
                        _has = False
                    if not _has:
                        import os as _os
                        _has = any(bool(_os.getenv(_e, '').strip())
                                   for _e in _envs)
                    if _has and _label not in detail:
                        detail.append(_label)
            except Exception:
                pass
            # Offline-first: a reachable local server (Ollama/LM Studio,
            # no key needed) also lights the fleet.  Without this a
            # local-only user trips the breaker, and the tripped breaker
            # starves the RLM reflector/archivist (their consolidation
            # spend goes through Brain.complete, which refuses while
            # tripped) — the hierarchy would stall at L0 with no sleep
            # pass.  Reuses the provider's own probe (short timeout),
            # only when no key was found, so the boot path never hangs
            # on it.  Honors the local-disable switches in factory
            # config so users CAN opt out without tripping either: a
            # disabled endpoint must never count as "configured".
            if not detail:
                try:
                    import os as _os2
                    _local_off = (_os2.getenv('JARVIS_DISABLE_LOCAL',
                                              '') == '1')
                    _ollama_url = _os2.getenv(
                        'OLLAMA_BASE_URL',
                        'http://localhost:11434/v1').strip()
                    _lmstudio_url = _os2.getenv(
                        'LMSTUDIO_BASE_URL',
                        'http://localhost:1234/v1').strip()
                    if not _local_off and _ollama_url.lower() not in (
                            '', 'off', 'none', 'disabled'):
                        from utils.llm.providers.openai_compat import (
                            OpenAICompatProvider as _Compat)
                        if _Compat("ollama", _ollama_url,
                                   key_optional=True,
                                   dynamic_models=True).available():
                            detail.append('Ollama')
                    if not _local_off and _lmstudio_url.lower() not in (
                            '', 'off', 'none', 'disabled'):
                        from utils.llm.providers.openai_compat import (
                            OpenAICompatProvider as _Compat2)
                        if _Compat2("lmstudio", _lmstudio_url,
                                    key_optional=True,
                                    dynamic_models=True).available():
                            detail.append('LM Studio')
                except Exception:
                    pass
            ok = bool(detail)
            # Missing providers is FATAL (tripped, not degraded): with no
            # fleet Brain.complete refuses to spend, so the RLM
            # reflector/archivist sleep pass stalls cleanly instead of
            # burning failover attempts every tick.  (The old
            # `ok or False` returned fatal=False on the failure path,
            # downgrading "no keys at all" to degraded.)
            return ok, True, \
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
        """
        JSON stores must be writable; vector layer is optional.

        RLM-readiness: the recursive memory JSON
        (brain/data/rlm_memory.json) must be creatable alongside the
        legacy episodic store — if it is
        not, the L0 observe path degrades to in-memory-only and the
        hierarchy silently loses everything on restart.  A read-only or
        uncreatable RLM dir is therefore checked exactly like the
        episodic one (non-fatal: degraded, never tripped).
        """
        problems = []
        try:
            from utils.rlm.memory import DEFAULT_DATA_FILE
            _rlm_file = (os.getenv('JARVIS_RLM_DATA_FILE', '').strip()
                         or DEFAULT_DATA_FILE)
            _rlm_dir = os.path.dirname(_rlm_file)
            # No makedirs here: a check must never create real dirs as a
            # side effect (breaker runs at boot AND inside unit tests).
            # A missing dir is fine iff its parent is writable (the RLM
            # store creates it on first save); otherwise flag it.
            if os.path.isdir(_rlm_dir):
                if not os.access(_rlm_dir, os.W_OK):
                    problems.append(f"rlm dir read-only: {_rlm_dir}")
                elif os.path.exists(_rlm_file) and not os.access(
                        _rlm_file, os.W_OK):
                    problems.append(
                        f"rlm store read-only: "
                        f"{os.path.basename(_rlm_file)}")
            elif not os.access(os.path.dirname(_rlm_dir) or '.', os.W_OK):
                problems.append(f"rlm parent dir unwritable: {_rlm_dir}")
        except Exception as e:
            problems.append(f"rlm store check failed: {e}")
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
