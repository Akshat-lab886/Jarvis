"""
Jarvis Session Budget Guard
===========================

Hard limits on token spend and estimated USD cost per process session.
Prevents runaway compute from infinite loops, hallucination spirals, or
reflection storms.

Limits (env-configurable):
    JARVIS_BUDGET_USD      default 5.00   — estimated dollar cap
    JARVIS_BUDGET_TOKENS   default 500000 — total token cap
    JARVIS_WARN_AT_PCT     default 80     — warn once past this %

Token counting: provider usage fields when available, else a chars/4
heuristic.  Pricing is a coarse per-model table (USD per 1K tokens,
blended in/out) — deliberately conservative; override via
JARVIS_PRICE_JSON='{"model-substring": price}'.

Brain.complete()/think() call budget.guard() before every provider call
and budget.record() after; when exceeded, guard() raises
BudgetExceededError which callers translate into a polite halt.
"""

import os
import json
import time
import threading

# Rough blended USD per 1K tokens (in+out averaged). Conservative.
_DEFAULT_PRICES = {
    'gpt-oss-120b': 0.60,
    'gpt-oss-20b': 0.10,
    'compound': 0.20,
    'qwen': 0.30,
    'gemini': 0.30,
    'llama': 0.20,
    'gemma': 0.10,
}


class BudgetExceededError(RuntimeError):
    pass


class Budget:
    def __init__(self):
        self.limit_usd = float(os.getenv('JARVIS_BUDGET_USD', '5') or 5)
        self.limit_tokens = int(os.getenv('JARVIS_BUDGET_TOKENS',
                                          '500000') or 500000)
        self.warn_pct = float(os.getenv('JARVIS_WARN_AT_PCT', '80'))
        self._custom_prices = {}
        raw = os.getenv('JARVIS_PRICE_JSON')
        if raw:
            try:
                self._custom_prices = {
                    str(k).lower(): float(v)
                    for k, v in json.loads(raw).items()
                }
            except Exception:
                pass

        self.spent_usd = 0.0
        self.spent_tokens = 0
        self.calls = 0
        self.warned = False
        self.halted = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    def _price_for(self, model):
        m = str(model or '').lower()
        for key, price in self._custom_prices.items():
            if key in m:
                return price
        for key, price in _DEFAULT_PRICES.items():
            if key in m:
                return price
        return 0.50   # unknown model — assume mid-tier

    @staticmethod
    def _est_tokens(text):
        return max(1, int(len(text or '') / 4))

    # ------------------------------------------------------------------ #
    def _pct_used(self):
        with_lock_held = True  # informational; callers hold lock or accept race
        usd_pct = (self.spent_usd / self.limit_usd * 100) \
            if self.limit_usd > 0 else 0
        tok_pct = (self.spent_tokens / self.limit_tokens * 100) \
            if self.limit_tokens > 0 else 0
        return max(usd_pct, tok_pct)

    def guard(self):
        """
        Raise BudgetExceededError when either hard limit is hit.
        Called BEFORE each provider call.
        """
        with self._lock:
            if self.halted:
                raise BudgetExceededError("session budget already exhausted")
            self._warn_if_needed()
            if ((self.limit_usd > 0 and
                 self.spent_usd >= self.limit_usd) or
                (self.limit_tokens > 0 and
                 self.spent_tokens >= self.limit_tokens)):
                self.halted = True
            if self.halted:
                print(f"🛑 SESSION BUDGET EXHAUSTED — halting LLM calls "
                      f"(${self.spent_usd:.3f}/${self.limit_usd:.2f}, "
                      f"{self.spent_tokens} tokens). "
                      f"Restart Jarvis or raise JARVIS_BUDGET_USD.")
                raise BudgetExceededError(
                    f"Session budget exhausted "
                    f"(${self.spent_usd:.2f}/${self.limit_usd:.2f})")

    def _warn_if_needed(self):
        """One-time heads-up once past JARVIS_WARN_AT_PCT of the budget."""
        if self.warned:
            return
        worst = self._pct_used()
        if worst >= self.warn_pct:
            self.warned = True
            print(f"⚠️  Budget warning: {worst:.0f}% of session "
                  f"budget used (${self.spent_usd:.3f}/"
                  f"${self.limit_usd:.2f}, "
                  f"{self.spent_tokens}/{self.limit_tokens} tokens)")

    def record(self, model=None, input_text='', output_text='',
               usage=None):
        """
        Account one provider call. ``usage`` may carry OpenAI-style
        prompt_tokens/completion_tokens; else heuristic estimation.
        """
        with self._lock:
            self.calls += 1
            if usage and getattr(usage, 'prompt_tokens', None) is not None:
                in_tok = int(usage.prompt_tokens or 0)
                out_tok = int(getattr(usage, 'completion_tokens', 0) or 0)
            else:
                in_tok = self._est_tokens(input_text)
                out_tok = self._est_tokens(output_text)

            total = in_tok + out_tok
            price = self._price_for(model)
            cost = total / 1000.0 * price
            self.spent_tokens += total
            self.spent_usd += cost

            # Warn + halt checks inline so long-running loops stop
            # promptly at spend time, not only before the next call.
            self._warn_if_needed()
            if ((self.limit_usd > 0 and
                 self.spent_usd >= self.limit_usd) or
                (self.limit_tokens > 0 and
                 self.spent_tokens >= self.limit_tokens)):
                self.halted = True

    # ------------------------------------------------------------------ #
    def status(self):
        with self._lock:
            return {
                'spent_usd': round(self.spent_usd, 4),
                'limit_usd': self.limit_usd,
                'spent_tokens': self.spent_tokens,
                'limit_tokens': self.limit_tokens,
                'calls': self.calls,
                'halted': self.halted,
                'pct': round(max(
                    self.spent_usd / self.limit_usd * 100
                    if self.limit_usd else 0,
                    self.spent_tokens / self.limit_tokens * 100
                    if self.limit_tokens else 0), 1),
            }

    def reset(self):
        with self._lock:
            self.spent_usd = 0.0
            self.spent_tokens = 0
            self.calls = 0
            self.warned = False
            self.halted = False


# Shared session budget (one per process)
_shared = None


def get_budget():
    global _shared
    if _shared is None:
        _shared = Budget()
    return _shared
