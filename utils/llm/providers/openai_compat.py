"""
OpenAI-compatible provider — one class covers MANY vendors:

    OpenAI · Groq · OpenRouter · DeepSeek · Together · Mistral ·
    Ollama (v1 endpoint) · LM Studio · vLLM · llama.cpp server · any
    ``CUSTOM_OPENAI_BASE_URL``.

All of them speak the /chat/completions dialect, so BYOK support for a
new vendor is usually just another instance with a different base URL.
"""

import time
import logging
from openai import OpenAI

from utils.llm.providers.base import (BaseProvider, ChatResult, ToolCall,
                                      Usage, ProviderError, classify_error)

logger = logging.getLogger("Jarvis.LLM.OpenAICompat")


class OpenAICompatProvider(BaseProvider):
    """
    :param key_optional: local servers (Ollama/LM Studio) need no key.
    :param dynamic_models: probe ``GET {base_url}/models`` to discover
        the model list instead of trusting a static chain.
    """

    PROBE_TTL = 300  # seconds between endpoint reachability probes

    def __init__(self, name, base_url, models=None, key=None,
                 key_optional=False, dynamic_models=False):
        super().__init__(models)
        self.name = name
        self.base_url = base_url.rstrip('/')
        self.key = key or ('not-needed' if key_optional else None)
        self.key_optional = key_optional
        self.dynamic_models = dynamic_models
        self._client = None
        self._probe_ts = 0.0
        self._probe_ok = False

    # ---------------------------------------------------------------- #

    @property
    def client(self):
        if self._client is None:
            self._client = OpenAI(base_url=self.base_url,
                                  api_key=self.key or 'missing')
        return self._client

    def available(self):
        if not self.key_optional and not self.key:
            return False
        if self.dynamic_models:
            return self._probe()
        return True

    def _probe(self):
        now = time.time()
        if now - self._probe_ts < self.PROBE_TTL:
            return self._probe_ok
        try:
            import requests
            r = requests.get(f"{self.base_url}/models", timeout=1.5)
            ok = (r.status_code == 200)
            if ok:
                ids = [m.get('id') for m in r.json().get('data', [])
                       if m.get('id')]
                if ids and not self.models:
                    self.models = ids[:6]
        except Exception:
            ok = False
        self._probe_ok = ok
        self._probe_ts = now
        return ok

    def list_models(self):
        if self.dynamic_models:
            self._probe()
        return super().list_models()

    # ---------------------------------------------------------------- #

    # Mercury-2.5 (Inception Labs) is a diffusion/reasoning model: it
    # budgets the whole output window into `reasoning_tokens` at default
    # effort and finishes on "length" before emitting any text, so a small
    # max_tokens yields content=None.  Pass reasoning_effort=low + a sane
    # temperature/window and it returns content.  temperature must be in
    # [0.5, 1.0] or mercury resets it to 1.0 with a warning.
    _MERCURY_HINTS = ("mercury", "inception")

    def _is_mercury(self):
        name = (getattr(self, "name", "") or "").lower()
        mdl = (self.models[0] if self.models else "") or ""
        return any(h in name for h in self._MERCURY_HINTS) or \
               any(h in mdl.lower() for h in self._MERCURY_HINTS)

    def chat(self, messages, *, model=None, tools=None, max_tokens=None,
             temperature=0.2, timeout=45, stream=False):
        kwargs = dict(
            model=model or (self.models[0] if self.models else None),
            messages=messages,
            temperature=temperature,
            timeout=timeout,
        )
        if max_tokens:
            kwargs['max_tokens'] = max_tokens
        if tools:
            kwargs['tools'] = tools

        # Mercury-specific shaping (see _MERCURY_HINTS above).
        if self._is_mercury():
            # temperature is rejected below 0.5 / above 1.0 (mercury resets
            # it to 1.0 with a warning) — clamp into the valid band.
            raw = kwargs['temperature']
            kwargs['temperature'] = max(0.5, min(1.0, raw))
            # never let a small completion budget starve the answer:
            # mercury burns the whole budget on reasoning tokens first,
            # so a tiny max_tokens finishes on "length" with content=None.
            mt = kwargs.get('max_tokens')
            if not mt or mt < 512:
                kwargs['max_tokens'] = 512
            kwargs['extra_body'] = kwargs.get('extra_body') or {}
            kwargs['extra_body'].setdefault('reasoning_effort', 'low')
            kwargs.setdefault('max_completion_tokens', kwargs['max_tokens'])

        t0 = time.time()
        try:
            completion = self.client.chat.completions.create(
                stream=stream, **kwargs)
        except Exception as err:
            raise classify_error(err) from err

        if stream:
            return self._stream(completion, model, t0)

        usage = None
        u = getattr(completion, 'usage', None)
        if u is not None:
            usage = Usage(prompt_tokens=int(getattr(u, 'prompt_tokens', 0) or 0),
                          completion_tokens=int(
                              getattr(u, 'completion_tokens', 0) or 0))
        choice = completion.choices[0] if completion.choices else None
        msg = getattr(choice, 'message', None) if choice else None

        tool_calls = []
        for tc in (getattr(msg, 'tool_calls', None) or []):
            fn = getattr(tc, 'function', None)
            tool_calls.append(ToolCall(
                id=getattr(tc, 'id', '') or '',
                name=getattr(fn, 'name', '') or '',
                arguments=getattr(fn, 'arguments', '') or '{}'))

        return ChatResult(
            text=(getattr(msg, 'content', '') or '') if msg else '',
            tool_calls=tool_calls,
            usage=usage,
            provider=self.name,
            model=model or '',
            finish_reason=(getattr(choice, 'finish_reason', '') or '')
            if choice else '',
            elapsed_s=time.time() - t0,
            raw=completion,
        )

    # ---------------------------------------------------------------- #

    def _stream(self, completion, model, t0):
        """
        Yield ChatResult chunks; text accumulates across chunks.
        Tool-call argument fragments are merged incrementally and the
        COMPLETE tool_calls list is attached to a terminal chunk (the
        one carrying finish_reason), so consumers never see fragments.
        """
        acc = []

        def _gen():
            finish = ''
            # index → {"id","name","args"} accumulating fragments
            pending = {}
            flushed = False
            try:
                for chunk in completion:
                    if not getattr(chunk, 'choices', None):
                        continue
                    choice = chunk.choices[0]
                    delta = choice.delta
                    fr = getattr(choice, 'finish_reason', None)
                    piece = getattr(delta, 'content', None) or ''

                    # merge tool_call fragments by index
                    for tc in (getattr(delta, 'tool_calls', None) or []):
                        idx = getattr(tc, 'index', 0) or 0
                        slot = pending.setdefault(
                            idx, {"id": "", "name": "", "args": ""})
                        fn = getattr(tc, 'function', None)
                        if getattr(tc, 'id', None):
                            slot["id"] = tc.id
                        if fn:
                            if getattr(fn, 'name', None):
                                slot["name"] += fn.name
                            if getattr(fn, 'arguments', None):
                                slot["args"] += fn.arguments

                    if fr:
                        finish = fr
                    if piece:
                        acc.append(piece)
                        yield ChatResult(text=piece, provider=self.name,
                                         model=model,
                                         finish_reason=finish or '',
                                         elapsed_s=time.time() - t0)
                    elif finish and pending and not flushed:
                        # Terminal chunk: hand over complete tool calls.
                        flushed = True
                        tool_calls = [
                            ToolCall(id=slot["id"],
                                     name=slot["name"],
                                     arguments=slot["args"] or '{}')
                            for _, slot in sorted(pending.items())]
                        yield ChatResult(text='', provider=self.name,
                                         model=model, finish_reason=finish,
                                         tool_calls=tool_calls,
                                         elapsed_s=time.time() - t0)
                # Some providers never send a terminal content chunk —
                # flush accumulated tool calls once the iterator ends.
                if pending and not flushed:
                    flushed = True
                    tool_calls = [
                        ToolCall(id=slot["id"], name=slot["name"],
                                 arguments=slot["args"] or '{}')
                        for _, slot in sorted(pending.items())]
                    yield ChatResult(text='', provider=self.name,
                                     model=model,
                                     finish_reason=finish or 'tool_calls',
                                     tool_calls=tool_calls,
                                     elapsed_s=time.time() - t0)
            except Exception as err:
                raise classify_error(err) from err
            finally:
                logger.info("%s stream done (%d chars, %.1fs)",
                            self.name, len(''.join(acc)),
                            time.time() - t0)

        return _gen()
