"""
Anthropic provider — native Messages API over ``requests`` (no SDK
dependency; ``requests`` is already in requirements).
"""

import json
import time
import logging
import requests

from utils.llm.providers.base import (BaseProvider, ChatResult, ToolCall,
                                      Usage, ProviderError, classify_error)

logger = logging.getLogger("Jarvis.LLM.Anthropic")

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 2048


class AnthropicProvider(BaseProvider):

    def __init__(self, key=None, models=None):
        super().__init__(models or [
            "claude-sonnet-4-5",
            "claude-3-5-haiku-latest",
        ])
        self.name = "anthropic"
        self.key = key

    def available(self):
        return bool(self.key)

    # ---------------------------------------------------------------- #

    @staticmethod
    def _convert_messages(messages):
        """
        OpenAI-style → Anthropic style.  Returns ``(system, messages)``.
        System text is hoisted out; images become content blocks.
        """
        system_parts = []
        out = []
        for msg in messages:
            role = msg.get('role')
            content = msg.get('content', '')
            if role == 'system':
                if isinstance(content, str):
                    system_parts.append(content)
                else:
                    text, _ = BaseProvider._split_images(content)
                    if text:
                        system_parts.append(text)
                continue
            if isinstance(content, str):
                out.append({"role": role,
                            "content": [{"type": "text", "text": content}]})
                continue
            text, images = BaseProvider._split_images(content)
            blocks = []
            if text:
                blocks.append({"type": "text", "text": text})
            for media, b64 in images:
                blocks.append({"type": "image",
                               "source": {"type": "base64",
                                          "media_type": media,
                                          "data": b64}})
            if blocks:
                out.append({"role": role, "content": blocks})
        return "\n\n".join(system_parts), out

    @staticmethod
    def _convert_tools(tools):
        """OpenAI function-tool format → Anthropic tool definitions."""
        converted = []
        for tool in tools or []:
            fn = tool.get('function', tool)  # tolerate native format
            if fn.get('input_schema') or fn.get('parameters'):
                converted.append({
                    "name": fn.get('name', ''),
                    "description": fn.get('description', ''),
                    "input_schema": fn.get('input_schema')
                    or fn.get('parameters') or {"type": "object"},
                })
        return converted

    # ---------------------------------------------------------------- #

    def chat(self, messages, *, model=None, tools=None, max_tokens=None,
             temperature=0.2, timeout=45, stream=False):
        system, conv = self._convert_messages(messages)
        body = {
            "model": model or (self.models[0] if self.models else None),
            "max_tokens": int(max_tokens or DEFAULT_MAX_TOKENS),
            "temperature": temperature,
            "messages": conv,
        }
        if system:
            body["system"] = system
        converted_tools = self._convert_tools(tools)
        if converted_tools:
            body["tools"] = converted_tools
        if stream:  # full SSE streaming lands with the agent-core phase
            raise ProviderError("anthropic cannot stream yet",
                                kind='no_stream')

        t0 = time.time()
        try:
            resp = requests.post(
                API_URL,
                headers={"x-api-key": self.key,
                         "anthropic-version": API_VERSION,
                         "content-type": "application/json"},
                json=body, timeout=timeout)
        except requests.exceptions.Timeout as err:
            raise classify_error(err) from err
        except Exception as err:
            raise classify_error(err) from err

        if resp.status_code != 200:
            kind = 'auth' if resp.status_code in (401, 403) else \
                   'quota' if resp.status_code == 429 else \
                   'context' if resp.status_code == 413 else 'generic'
            raise ProviderError(
                f"anthropic {resp.status_code}: {resp.text[:300]}",
                kind=kind, status=resp.status_code)

        data = resp.json()
        texts, tool_calls = [], []
        for block in data.get('content', []):
            btype = block.get('type')
            if btype == 'text':
                texts.append(block.get('text', ''))
            elif btype == 'tool_use':
                tool_calls.append(ToolCall(
                    id=block.get('id', ''),
                    name=block.get('name', ''),
                    arguments=json.dumps(block.get('input', {}))))
        usage_raw = data.get('usage', {}) or {}
        usage = Usage(prompt_tokens=int(usage_raw.get('input_tokens', 0)),
                      completion_tokens=int(
                          usage_raw.get('output_tokens', 0)))
        stop = data.get('stop_reason', '') or ''
        finish = 'tool_calls' if stop == 'tool_use' else (
            'length' if stop == 'max_tokens' else 'stop')

        return ChatResult(text="".join(texts), tool_calls=tool_calls,
                          usage=usage, provider=self.name,
                          model=data.get('model', '') or (model or ''),
                          finish_reason=finish,
                          elapsed_s=time.time() - t0, raw=data)
