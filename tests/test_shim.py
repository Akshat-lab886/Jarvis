"""Unit tests for utils.llm.shim — the OpenAI-shape adapter over the Router.

RouterCompletionClient lets legacy ``client.chat.completions.create(...)``
call sites run unmodified on top of the multi-provider BYOK fleet.  It
had only indirect coverage via test_router.py. These tests pin down:

- wrap_completion: ChatResult -> SimpleNamespace(choices[0].message{content,
  tool_calls, role}, finish_reason, usage, model)
- _create non-stream: delegates to router.chat with purpose='shim',
  returns a wrapped completion; model=None -> models=None passthrough
- _create streaming: returns the raw iterator from router.chat(stream=True)
- _create passthrough: **kwargs + temperature/max_tokens/timeout forwarded
- The chat property exposes a duck-typed completions.create callable
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.llm.shim import RouterCompletionClient, wrap_completion
from utils.llm.providers.base import ChatResult, Usage


def _result(text="hello", model="groq:openai/gpt-oss-120b",
            finish_reason="stop", tool_calls=None,
            usage=None):
    return ChatResult(text=text, model=model,
                      finish_reason=finish_reason,
                      tool_calls=tool_calls,
                      usage=usage or Usage(prompt_tokens=10,
                                          completion_tokens=2))


class TestWrapCompletion(unittest.TestCase):
    def test_basic_shape(self):
        r = _result(text="hi there", model="groq:abc",
                    finish_reason='stop')
        wrapped = wrap_completion(r)
        self.assertEqual(wrapped.model, "groq:abc")
        self.assertEqual(len(wrapped.choices), 1)
        self.assertEqual(wrapped.choices[0].message.content, "hi there")
        self.assertEqual(wrapped.choices[0].message.role, "assistant")
        self.assertEqual(wrapped.choices[0].finish_reason, "stop")
        self.assertEqual(wrapped.usage, r.usage)

    def test_tool_calls_passed_through(self):
        tc = [{"id": "1", "type": "function",
               "function": {"name": "search", "arguments": "{}"}}]
        r = _result(text="", model="openai:gpt-4o", tool_calls=tc)
        wrapped = wrap_completion(r)
        self.assertEqual(wrapped.choices[0].message.tool_calls, tc)
        self.assertEqual(wrapped.choices[0].finish_reason, "stop")

    def test_none_text_becomes_empty_string(self):
        """A tool-only response (text=None) must still produce a message
        with content='' so callers don't crash on .content."""
        r = _result(text=None, model="anthropic:sonnet",
                    finish_reason='tool_calls')
        wrapped = wrap_completion(r)
        self.assertEqual(wrapped.choices[0].message.content, "")
        self.assertEqual(wrapped.choices[0].message.role, "assistant")

    def test_finish_reason_defaults_to_stop(self):
        r = _result(text="ok", model="google:gemini", finish_reason="")
        wrapped = wrap_completion(r)
        self.assertEqual(wrapped.choices[0].finish_reason, "stop")

    def test_usage_none_passes_through(self):
        r = _result(text="ok", model="deepseek:chat")
        r.usage = None
        wrapped = wrap_completion(r)
        self.assertIsNone(wrapped.usage)


class TestCreateNonStream(unittest.TestCase):
    def _client(self, result=None, stream_return=None):
        router = MagicMock()
        if stream_return is not None and not result:
            router.chat.return_value = stream_return
        else:
            router.chat.return_value = result or _result()
        return RouterCompletionClient(router), router

    def test_delegates_to_router_with_purpose_shim(self):
        client, router = self._client()
        client._create(model="groq:openai/gpt-oss-120b",
                       messages=[{"role": "user", "content": "hi"}],
                       max_tokens=512, temperature=0.5, timeout=10)
        router.chat.assert_called_once()
        kwargs = router.chat.call_args.kwargs
        self.assertEqual(kwargs['purpose'], 'shim')
        self.assertFalse(kwargs['stream'])
        self.assertEqual(kwargs['models'], ["groq:openai/gpt-oss-120b"])
        self.assertEqual(kwargs['max_tokens'], 512)
        self.assertEqual(kwargs['temperature'], 0.5)
        self.assertEqual(kwargs['timeout'], 10)

    def test_model_none_passes_models_none(self):
        """When the caller omits a model pin, the shim must pass
        models=None (search the whole fleet), NOT models=[] or ['None']."""
        client, router = self._client()
        client._create(messages=[{"role": "user", "content": "hi"}])
        kwargs = router.chat.call_args.kwargs
        self.assertIsNone(kwargs['models'])

    def test_returns_wrapped_completion(self):
        result = _result(text="the answer", model="groq:model-x",
                         finish_reason='stop')
        client, _ = self._client(result=result)
        out = client._create(model="groq:model-x",
                             messages=[{"role": "user", "content": "q"}])
        self.assertEqual(out.choices[0].message.content, "the answer")
        self.assertEqual(out.model, "groq:model-x")

    def test_passthrough_kwargs_forwarded(self):
        """Extra OpenAI-style kwargs (tools, user, etc.) flow through
        **kwargs without the shim ever inspecting them."""
        client, router = self._client()
        client._create(model="openai:gpt-4o",
                       messages=[],
                       tools=[{"type": "function", "function": {}}],
                       user="conv-123",
                       extra_custom_thing=True)
        kwargs = router.chat.call_args.kwargs
        self.assertTrue(kwargs['tools'])
        # **kwargs beyond the named ones are absorbed — router.chat
        # accepts **extra, so unknown kwargs must not raise here.
        # (They reach the provider via the Router's **kwargs path.)

    def test_temperature_default(self):
        client, router = self._client()
        client._create(messages=[])
        self.assertEqual(router.chat.call_args.kwargs['temperature'], 0.2)

    def test_timeout_default(self):
        client, router = self._client()
        client._create(messages=[])
        self.assertEqual(router.chat.call_args.kwargs['timeout'], 45)

    def test_tools_argument_forwarded(self):
        """REGRESSION: the shim previously swallowed `tools=` (and every
        other OpenAI-style kwarg) because _create's **kwargs was never
        spread into router.chat(...). Tool-use LLM calls silently lost
        their tool definitions."""
        tools = [{"type": "function",
                  "function": {"name": "search", "arguments": "{}"}}]
        client, router = self._client()
        client._create(model="groq:abc", messages=[], tools=tools)
        self.assertEqual(router.chat.call_args.kwargs['tools'], tools)


class TestCreateStream(unittest.TestCase):
    def test_stream_returns_iterator_directly(self):
        """When stream=True the shim must NOT wrap — it returns the raw
        chunk iterator so Phase 2 UI can consume it incrementally."""
        chunk_iter = iter([_result(text="chunk1", finish_reason=""),
                           _result(text="chunk2", finish_reason="stop")])
        router = MagicMock()
        router.chat.return_value = chunk_iter
        client = RouterCompletionClient(router)
        out = client._create(model="groq:abc", messages=[], stream=True)
        self.assertIs(out, chunk_iter)
        # And purpose='shim' + stream=True were forwarded
        self.assertTrue(router.chat.call_args.kwargs['stream'])
        self.assertEqual(router.chat.call_args.kwargs['purpose'], 'shim')


class TestChatProperty(unittest.TestCase):
    def _client(self, result=None, stream_return=None):
        router = MagicMock()
        if stream_return is not None and not result:
            router.chat.return_value = stream_return
        else:
            router.chat.return_value = result or _result()
        return RouterCompletionClient(router), router

    def test_duck_typed_openai_client_shape(self):
        """The .chat property mimics openai.Client.chat.completions.create
        so legacy 'client.chat.completions.create(...)' call sites work."""
        client, router = self._client()
        self.assertTrue(callable(client.chat.completions.create))

    def test_messages_passthrough(self):
        client, router = self._client()
        msgs = [{"role": "system", "content": "x"},
                {"role": "user", "content": "y"}]
        client._creat = client._create  # alias sanity
        client.chat.completions.create(model="groq:m",
                                       messages=msgs)
        self.assertEqual(router.chat.call_args.args[0], msgs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
