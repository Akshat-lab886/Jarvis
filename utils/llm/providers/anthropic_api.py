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

        System text is hoisted out; images become content blocks.  Native
        tool conversations survive too: an assistant message's top-level
        ``tool_calls`` become ``tool_use`` content blocks, and ``role:
        "tool"`` results become ``tool_result`` blocks inside the user
        turn that must follow them.  Consecutive results are coalesced so
        the history keeps strictly alternating roles, as the Anthropic
        API requires.
        """
        system_parts = []
        out = []
        pending_tool_results = []   # tool_result blocks awaiting a user turn

        def _flush_tool_results():
            if not pending_tool_results:
                return
            if out and out[-1].get('role') == 'user':
                out[-1]['content'].extend(pending_tool_results)
            else:
                out.append({"role": "user",
                            "content": list(pending_tool_results)})
            del pending_tool_results[:]

        def _append(role, blocks):
            if not blocks:
                return
            # Two consecutive user turns (a text message landing right
            # after a batch of tool results) merge into one.
            if role == 'user' and out and out[-1].get('role') == 'user':
                out[-1]['content'].extend(blocks)
            else:
                out.append({"role": role, "content": blocks})

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

            if role == 'tool':
                pending_tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": msg.get('tool_call_id') or '',
                    "content": str(content or ''),
                })
                continue

            # Any non-tool message ends the run of tool results first.
            _flush_tool_results()

            text, images = (content, []) if isinstance(content, str) \
                else BaseProvider._split_images(content)
            blocks = [{"type": "image",
                       "source": {"type": "base64",
                                  "media_type": media,
                                  "data": b64}}
                      for media, b64 in images]

            tool_calls = msg.get('tool_calls') or []
            if role == 'assistant' and tool_calls:
                # Requested calls become tool_use blocks.
                for i, tc in enumerate(tool_calls):
                    if isinstance(tc, dict):
                        fn = tc.get('function') or tc
                        tcid = tc.get('id') or fn.get('id') or f"call_{i}"
                        name = fn.get('name', '') or ''
                        args = fn.get('arguments', '{}')
                    else:
                        fn = getattr(tc, 'function', tc)
                        tcid = (getattr(tc, 'id', '') or ''
                                or getattr(fn, 'id', '') or f"call_{i}")
                        name = getattr(fn, 'name', '') or ''
                        args = getattr(fn, 'arguments', '{}')
                    if isinstance(args, str):
                        try:
                            parsed = json.loads(args)
                        except (TypeError, ValueError, json.JSONDecodeError):
                            parsed = {}
                    else:
                        parsed = args
                    if not isinstance(parsed, dict):
                        parsed = {}
                    if text:
                        blocks.insert(0, {"type": "text", "text": text})
                        text = ""            # text emitted once, before calls
                    blocks.append({"type": "tool_use", "id": tcid,
                                   "name": name, "input": parsed})
            if text:
                blocks.insert(0, {"type": "text", "text": text})
            _append(role, blocks)

        _flush_tool_results()          # trailing results still pending
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
