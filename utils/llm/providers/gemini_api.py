"""
Google Gemini provider — wraps the ``google-genai`` SDK that Jarvis
already ships with (same client the legacy brain path used).
"""

import json
import time
import logging

from utils.llm.providers.base import (BaseProvider, ChatResult, ToolCall,
                                      Usage, ProviderError, classify_error)

logger = logging.getLogger("Jarvis.LLM.Gemini")


class GeminiProvider(BaseProvider):

    def __init__(self, key=None, models=None):
        super().__init__(models or [
            "gemini-2.5-flash",
            "gemini-2.5-pro",
        ])
        self.name = "google"
        self.key = key
        self._client = None

    def available(self):
        return bool(self.key)

    @property
    def client(self):
        if self._client is None:
            from google import genai
            self._client = genai.Client(api_key=self.key)
        return self._client

    # ---------------------------------------------------------------- #

    def _build_contents(self, messages):
        """OpenAI-style messages → (contents list, system text)."""
        from google.genai import types
        system_parts = []
        contents = []
        for msg in messages:
            role = msg.get('role')
            content = msg.get('content', '')
            if role == 'system':
                if isinstance(content, str):
                    system_parts.append(content)
                else:
                    text, _ = self._split_images(content)
                    system_parts.append(text)
                continue
            grole = 'model' if role == 'assistant' else 'user'
            parts = []
            if isinstance(content, str):
                parts.append(types.Part.from_text(text=content))
            else:
                text, images = self._split_images(content)
                for media, b64 in images:
                    parts.append(types.Part.from_bytes(data=b64.encode()
                                                       if isinstance(b64, str)
                                                       else b64,
                                                       mime_type=media))
                if text:
                    parts.append(types.Part.from_text(text=text))
            contents.append(types.Content(role=grole, parts=parts))
        return contents, "\n\n".join(system_parts)

    @staticmethod
    def _convert_tools(tools):
        """OpenAI function-tool format → Gemini declarations."""
        decls = []
        for tool in tools or []:
            fn = tool.get('function', tool)
            params = fn.get('parameters') or fn.get('input_schema')
            if not params:
                continue
            decls.append({"name": fn.get('name', ''),
                          "description": fn.get('description', ''),
                          "parameters": params})
        return decls or None

    # ---------------------------------------------------------------- #

    def chat(self, messages, *, model=None, tools=None, max_tokens=None,
             temperature=0.2, timeout=45, stream=False):
        from google.genai import types

        contents, system = self._build_contents(messages)
        cfg_kwargs = {
            "temperature": temperature,
            "http_options_timeout": timeout * 1000,
        }
        if system:
            cfg_kwargs["system_instruction"] = system
        if max_tokens:
            cfg_kwargs["max_output_tokens"] = int(max_tokens)
        decls = self._convert_tools(tools)
        if decls:
            cfg_kwargs["tools"] = [types.Tool(function_declarations=decls)]
        if stream:
            raise ProviderError("gemini cannot stream yet",
                                kind='no_stream')

        t0 = time.time()
        try:
            response = self.client.models.generate_content(
                model=model or (self.models[0] if self.models else None),
                contents=contents,
                config=types.GenerateContentConfig(**cfg_kwargs),
            )
        except Exception as err:
            raise classify_error(err) from err

        text = (getattr(response, 'text', '') or '')
        tool_calls = []
        try:
            for cand in response.candidates or []:
                for part in (cand.content.parts if cand.content else []):
                    fc = getattr(part, 'function_call', None)
                    if fc and getattr(fc, 'name', ''):
                        args = getattr(fc, 'args', {}) or {}
                        tool_calls.append(ToolCall(
                            id='', name=fc.name,
                            arguments=args if isinstance(args, str)
                            else json.dumps(dict(args))))
        except Exception:
            pass

        usage = None
        um = getattr(response, 'usage_metadata', None)
        if um is not None:
            usage = Usage(
                prompt_tokens=int(getattr(um, 'prompt_token_count', 0) or 0),
                completion_tokens=int(
                    getattr(um, 'candidates_token_count', 0) or 0))

        finish = 'tool_calls' if tool_calls else (
            'length' if not text and not tool_calls else 'stop')

        return ChatResult(text=text.strip(), tool_calls=tool_calls,
                          usage=usage, provider=self.name,
                          model=model or '', finish_reason=finish,
                          elapsed_s=time.time() - t0, raw=response)
