"""
The Router — Jarvis's provider-agnostic completion front door.

Given a message list it:
  1. builds a candidate chain across EVERY configured provider,
     filtered by required capabilities (vision / tools / long_context),
  2. walks the chain honouring per-model and per-provider cooldowns,
  3. enforces the session budget + circuit breaker exactly like the
     legacy brain loop did,
  4. records spend, and fails over on any single-provider failure.

Bring-your-own-key reality: whoever you are — Groq free tier, OpenAI,
Anthropic, DeepSeek, OpenRouter, or a local Ollama box — drop in keys
and the same brain lights up.
"""

import os
import time
import threading
import logging

logger = logging.getLogger("Jarvis.LLM.Router")

DEFAULT_ORDER = ["google", "groq", "anthropic", "openai", "deepseek",
                 "openrouter", "custom", "ollama", "lmstudio"]

# Cooldown seconds by failure kind.
COOLDOWN = {
    "quota": 900,        # daily/monthly quotas — back off long
    "rate_limit": 60,    # TPM bursts — recover quickly
    "auth": 3600,        # bad key — stop hammering, surface loudly
    "context": 0,        # not this model's fault alone — just skip
    "timeout": 30,
    "generic": 15,
    "no_stream": 0,      # provider can't stream this turn — skip, no penalty
}


class AllProvidersError(Exception):
    """Every candidate provider/model failed."""

    def __init__(self, attempts):
        self.attempts = attempts          # [(provider, model, error_str)]
        summary = "; ".join(f"{p}/{m}: {e}" for p, m, e in attempts[-3:])
        super().__init__(f"all providers failed — {summary}")

    @property
    def kinds(self):
        return {k for _, _, k in getattr(self, '_kinds', [])}


class Router:

    def __init__(self):
        from utils.llm.providers import build_providers
        from utils.llm.keystore import get_keystore
        self.keystore = get_keystore()
        self.providers = build_providers(self.keystore)
        # Ensure the on-device SigLIP vision provider is present.
        # build_providers() registers it, but some sandbox environments can't
        # import the (new) submodule the first time — so we re-assert it here
        # at class-construction time, which reliably imports sigclip_vision.
        # The router's vision-failover chain must always have an offline
        # "eyes" fallback for the desktop/mobile computer-use loop.
        if 'siglip' not in self.providers:
            try:
                from utils.llm.providers.sigclip_vision import SiglipVisionProvider
                _sp = SiglipVisionProvider()
                if _sp.available():
                    self.providers['siglip'] = _sp
            except Exception:
                pass
        self._lock = threading.RLock()
        # (provider, model_or_'*') -> available_again_ts
        self._cooldowns = {}

    # ---------------------------------------------------------------- #
    # Ordering & chain construction

    def _ordered_providers(self):
        env_order = [p.strip() for p in
                     os.getenv('JARVIS_PROVIDER_ORDER', '').split(',')
                     if p.strip()]
        prefer_local = os.getenv('JARVIS_PREFER_LOCAL', '') == '1'
        names = list(self.providers.keys())
        if env_order:
            ordered = [n for n in env_order if n in names]
            ordered += [n for n in names if n not in ordered]
            return ordered
        cloud = [n for n in DEFAULT_ORDER if n in names
                 and n not in ('ollama', 'lmstudio')]
        local = [n for n in ('ollama', 'lmstudio') if n in names]
        if prefer_local:
            return local + cloud
        # Preserve legacy behaviour: configured extras keep DEFAULT_ORDER
        # sequence, then anything unknown (future providers) appended.
        known = [n for n in cloud if n in DEFAULT_ORDER]
        extra_cloud = [n for n in cloud if n not in known]
        return known + extra_cloud + local

    def _cooling(self, provider, model):
        now = time.time()
        if self._cooldowns.get((provider, '*'), 0) > now:
            return True
        return self._cooldowns.get((provider, model), 0) > now

    def _chain(self, require=None, models=None):
        """
        Candidate [(provider_obj, model)] list.

        ``models`` accepts entries like ``"groq"`` (whole provider),
        ``"groq:model-slug"``, or a bare slug searched across providers.
        """
        chain = []
        if models:
            wanted = [models] if isinstance(models, str) else list(models)
        else:
            wanted = None

        for pname in self._ordered_providers():
            provider = self.providers.get(pname)
            if provider is None:
                continue
            pmodels = None
            if wanted:
                pmodels = []
                for w in wanted:
                    if ':' in w:
                        wp, wm = w.split(':', 1)
                        if wp == pname and (
                                wm in provider.models
                                or getattr(provider, 'dynamic_models',
                                           False)):
                            pmodels.append(wm)
                    elif w == pname:
                        pmodels = list(provider.models)
                        break
                    elif w in provider.models:
                        pmodels.append(w)
            else:
                pmodels = list(provider.list_models())
            for model in pmodels or []:
                from utils.llm.catalog import has_caps
                if not has_caps(pname, model, require):
                    continue
                if self._cooling(pname, model):
                    continue
                chain.append((provider, model))
        return chain

    # ---------------------------------------------------------------- #
    # Guardrails (identical semantics to the legacy brain loops)

    @staticmethod
    def _guard_ok():
        from utils.circuit_breaker import get_breaker
        from utils.budget import get_budget, BudgetExceededError
        try:
            if not get_breaker().allow_llm_spend():
                raise AllProvidersError([("system", "breaker", "tripped")])
            get_budget().guard()
        except BudgetExceededError as e:
            raise AllProvidersError([("system", "budget", str(e))])
        except AllProvidersError:
            raise
        except Exception:
            pass  # guardrail infrastructure failing must never brick chat

    @staticmethod
    def _record(provider, model, result, input_text=''):
        try:
            from utils.budget import get_budget
            get_budget().record(
                model=model,
                input_text=input_text,
                output_text=result.text or '',
                usage=result.usage)
        except Exception:
            pass

    @staticmethod
    def _est_input_tokens(messages):
        total = 0
        for msg in messages or []:
            content = msg.get('content', '') if isinstance(msg, dict) else ''
            if isinstance(content, str):
                total += len(content)
            else:
                for part in content or []:
                    total += len(str(part.get('text', '')))
        return total // 4

    def _cooldown_set(self, provider, model, kind):
        secs = COOLDOWN.get(kind, 15)
        if not secs:
            return
        with self._lock:
            if kind in ('quota', 'auth'):
                self._cooldowns[(provider, '*')] = time.time() + secs
            else:
                self._cooldowns[(provider, model)] = time.time() + secs
        logger.info("Cooldown %ss → %s/%s (%s)", secs, provider, model,
                    kind)

    # ---------------------------------------------------------------- #
    # Public API

    def chat(self, messages, *, require=None, models=None, tools=None,
             max_tokens=None,
             temperature=0.2, timeout=45, stream=False, purpose="chat"):
        """
        Run one completion across the fleet.  Returns ChatResult (or a
        chunk iterator when ``stream=True``).

        :param require: capability set, e.g. {'vision'} / {'tools'}
        :param models: preferred chain, most-preferred first
        """
        chain = self._chain(require=require, models=models)
        if not chain:
            raise AllProvidersError([
                ("router", "-", "no configured provider satisfies "
                 f"{sorted(require) if require else 'any'} requirement")])

        attempts = []
        input_est = self._est_input_tokens(messages)
        for provider, model in chain:
            try:
                self._guard_ok()
            except AllProvidersError as e:
                raise e

            logger.info("LLM %s via %s/%s", purpose, provider.name, model)
            try:
                result = provider.chat(
                    messages, model=model, tools=tools,
                    max_tokens=max_tokens,
                    temperature=temperature, timeout=timeout,
                    stream=stream)
            except Exception as err:
                kind = getattr(err, 'kind', 'generic')
                attempts.append((provider.name, model,
                                 f"[{kind}] {err}"))
                self._cooldown_set(provider.name, model, kind)
                continue

            if stream:
                return self._guarded_stream(provider, model, result,
                                            attempts)

            # Budget estimation counts len(text)//4, so hand it a string
            # sized to reproduce our own token estimate exactly.
            self._record(provider.name, model, result,
                         input_text=' ' * (input_est * 4))
            return result

        err = AllProvidersError(attempts)
        logger.warning("Router exhausted chain (%d attempt(s)): %s",
                       len(attempts), err)

        # Streaming requested but nobody in the chain could serve it —
        # degrade transparently to a blocking call rather than failing.
        if stream and attempts and all(
                '[no_stream]' in a[2] or '[generic]' in a[2]
                for a in attempts):
            logger.info("No provider streams; retrying chain blocking")
            return self.chat(messages, require=require, models=models,
                             max_tokens=max_tokens, temperature=temperature,
                             timeout=timeout, stream=False,
                             purpose=purpose + '-blocking')
        raise err

    def _guarded_stream(self, provider, model, gen, attempts):
        """
        Return a generator that fails over to the next candidate only if
        the stream errors BEFORE its first chunk; once bytes flow we own
        the response.
        """
        first = {}
        try:
            first['chunk'] = next(gen)
        except StopIteration:
            return iter([])          # empty stream is a valid outcome
        except Exception as err:
            kind = getattr(err, 'kind', 'generic')
            attempts.append((provider.name, model, f"[{kind}] {err}"))
            self._cooldown_set(provider.name, model, kind)
            raise AllProvidersError(attempts)

        def _wrap():
            yield first['chunk']
            got_error = None
            try:
                for chunk in gen:
                    yield chunk
            except Exception as err:
                got_error = err
            finally:
                if got_error is not None:
                    # A mid-stream failure: do NOT credit the partial output
                    # to the session budget (the user got an incomplete
                    # answer), but surface the error so the caller can react
                    # instead of silently truncating their response.
                    kind = getattr(got_error, 'kind', 'generic')
                    attempts.append((provider.name, model,
                                     f"[{kind}] {got_error}"))
                    self._cooldown_set(provider.name, model, kind)
                    logger.warning("stream failed mid-chunk via %s/%s: %s",
                                   provider.name, model, got_error)
                    raise AllProvidersError(attempts)
                else:
                    probe = first.get('chunk')
                    if probe is not None:
                        self._record(provider.name, model, probe)

        return _wrap()

    # ---------------------------------------------------------------- #

    def refresh(self):
        """Rebuild providers (after keys were added at runtime)."""
        from utils.llm.providers import build_providers
        with self._lock:
            self.providers = build_providers(self.keystore)
            self._cooldowns.clear()
        return self.describe()

    def describe(self):
        """Status snapshot for dashboards / diagnostics."""
        with self._lock:
            cool_now = {f"{p}/{m}": int(remaining)
                        for (p, m), ts in self._cooldowns.items()
                        if (remaining := ts - time.time()) > 0}
        return {
            "order": self._ordered_providers(),
            "providers": [
                {"name": name,
                 "models": self.providers[name].list_models()[:8],
                 "dynamic": getattr(self.providers[name],
                                    'dynamic_models', False)}
                for name in sorted(self.providers)
            ],
            "cooldowns": cool_now,
            "keys": self.keystore.status(),
        }


# --------------------------------------------------------------------- #

_singleton = None
_singleton_lock = threading.Lock()


def get_router():
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = Router()
        return _singleton
