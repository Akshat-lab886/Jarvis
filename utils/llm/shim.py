"""
OpenAI-shape adapter over the Router.

Lets legacy call sites — ``client.chat.completions.create(...)`` — run
unmodified on top of the multi-provider fleet.  Used by Brain so its
failover loop, guardrails and JSON parsing stay exactly as battle-tested
as they are today while gaining every new provider underneath.
"""

from types import SimpleNamespace


def wrap_completion(result):
    """ChatResult → minimal OpenAI response shape."""
    message = SimpleNamespace(
        content=result.text or '',
        tool_calls=(result.tool_calls or None),
        role='assistant')
    choice = SimpleNamespace(message=message,
                             finish_reason=result.finish_reason or 'stop')
    usage = result.usage
    return SimpleNamespace(choices=[choice], usage=usage,
                           model=result.model)


class RouterCompletionClient:
    """
    Duck-types an OpenAI SDK client.  Model ids may be ``"provider:model"``
    pins (e.g. ``"groq:openai/gpt-oss-120b"``) or bare slugs searched
    across every configured provider.
    """

    def __init__(self, router):
        self.router = router

    @property
    def chat(self):
        return SimpleNamespace(
            completions=SimpleNamespace(create=self._create))

    def _create(self, model=None, messages=None, max_tokens=None,
                temperature=0.2, timeout=45, stream=False, **kwargs):
        result = self.router.chat(
            messages, models=[model] if model else None,
            max_tokens=max_tokens, temperature=temperature,
            timeout=timeout, stream=stream, purpose='shim')
        if stream:
            return result          # chunk iterator (Phase 2 wires UI)
        return wrap_completion(result)
