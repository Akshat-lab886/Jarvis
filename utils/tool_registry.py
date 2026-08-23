"""
Jarvis Extensible Tool Registry
===============================

Lets custom REST tools be injected into the agent WITHOUT code changes.
Drop a JSON manifest into ``tools_registry/`` and it becomes callable
by the brain::

    {
      "name": "office_ac_control",
      "description": "Set office AC temperature",
      "method": "POST",                       // GET (default) or POST
      "url": "https://home.local/api/ac/{action}",
      "params": {
        "action": {"type": "string", "required": true,
                   "description": "on | off | mode"},
        "temperature": {"type": "number", "required": false,
                        "description": "target temp celsius"}
      },
      "headers": {"X-Client": "jarvis"},       // optional static headers
      "auth_env": "HOME_API_TOKEN",            // optional -> Bearer token
      "timeout": 10                            // optional seconds
    }

Rules:
  - ``{param}`` placeholders in *url* are substituted from arguments.
  - GET  → remaining args become query string parameters.
  - POST → remaining args become a JSON body.
  - Required/unknown argument validation mirrors SkillRegistry.
  - Responses are truncated before reaching the prompt.
"""

import os
import re
import json
import time
import logging

import requests

logger = logging.getLogger("Jarvis.ToolRegistry")

_URL_PARAM_RE = re.compile(r"\{(\w+)\}")


class ToolError(Exception):
    """Raised for invalid manifests or bad tool invocations."""
    pass


class ToolRegistry:
    def __init__(self, tools_dir=None):
        self.base_dir = os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))
        self.tools_dir = tools_dir or os.path.join(self.base_dir,
                                                   'tools_registry')
        os.makedirs(self.tools_dir, exist_ok=True)
        self._cache = None          # name -> manifest dict
        self._cache_ts = 0.0
        self.CACHE_TTL = 20         # hot-reload manifests while running

    # ------------------------------------------------------------------ #
    # Loading / validation
    # ------------------------------------------------------------------ #
    def _load_all(self, force=False):
        now = time.time()
        if self._cache is not None and not force \
                and (now - self._cache_ts) < self.CACHE_TTL:
            return self._cache

        manifests = {}
        try:
            entries = sorted(os.listdir(self.tools_dir))
        except FileNotFoundError:
            entries = []
        for fname in entries:
            if not fname.endswith('.json'):
                continue
            path = os.path.join(self.tools_dir, fname)
            try:
                with open(path, 'r') as f:
                    data = json.load(f)
                manifest = self._validate(data)
                manifests[manifest['name']] = manifest
            except Exception as e:
                logger.warning(f"Skipping invalid tool manifest "
                               f"{fname}: {e}")
        self._cache = manifests
        self._cache_ts = now
        return manifests

    @staticmethod
    def _validate(data):
        if not isinstance(data, dict):
            raise ToolError("manifest must be a JSON object")
        name = str(data.get('name', '')).strip()
        if not re.fullmatch(r'[a-z0-9_]{2,40}', name):
            raise ToolError("name must be snake_case (a-z0-9_, 2-40 chars)")
        if not data.get('description'):
            raise ToolError("description is required")
        url = str(data.get('url', '')).strip()
        if not url.startswith(('http://', 'https://')):
            raise ToolError("url must be an absolute http(s) URL")

        method = str(data.get('method', 'GET')).upper()
        if method not in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE'):
            raise ToolError(f"unsupported method: {method}")

        params = {}
        raw_params = data.get('params') or {}
        if not isinstance(raw_params, dict):
            raise ToolError("params must be an object")
        for pname, spec in raw_params.items():
            if not isinstance(spec, dict):
                spec = {'description': str(spec)}
            params[str(pname)] = {
                'type': str(spec.get('type', 'string')),
                'required': bool(spec.get('required', False)),
                'description': str(spec.get('description', '')),
            }

        return {
            'name': name,
            'display_name': data.get('display_name') or name,
            'description': str(data['description']),
            'method': method,
            'url': url,
            'params': params,
            'headers': {str(k): str(v)
                        for k, v in (data.get('headers') or {}).items()},
            'auth_env': data.get('auth_env'),
            'timeout': min(int(data.get('timeout', 15)), 60),
        }

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #
    def list_tools(self):
        return sorted(self._load_all().values(),
                      key=lambda m: m['name'])

    def get_tool(self, name):
        name = str(name or '').strip().lower()
        return self._load_all().get(name)

    def count(self):
        return len(self._load_all())

    def render_catalog(self, max_chars=1800):
        """Render the tool list for injection into the brain prompt."""
        tools = self.list_tools()
        if not tools:
            return ""
        lines = ["AVAILABLE EXTERNAL TOOLS (custom REST integrations):"]
        for t in tools:
            pnames = ", ".join(t['params']) if t['params'] else "none"
            lines.append(
                f"- {t['name']} ({t['method']}) — "
                f"{t['description'][:90]} | params: {pnames}"
            )
        catalog = "\n".join(lines)
        if len(catalog) > max_chars:
            catalog = catalog[:max_chars] + "\n…(truncated)"
        return catalog

    # ------------------------------------------------------------------ #
    # Execution
    # ------------------------------------------------------------------ #
    def call(self, name, args=None):
        """
        Invoke a registered tool.  Returns (ok: bool, text_result: str).
        Raises ToolError for unknown tools / bad arguments.
        """
        tool = self.get_tool(name)
        if not tool:
            known = ", ".join(sorted(self._load_all())[:8])
            raise ToolError(f"Unknown tool '{name}'."
                            + (f" Known tools: {known}" if known else
                               " No tools registered."))

        supplied = dict(args or {})
        declared = tool['params']

        unknown = sorted(set(supplied) - set(declared))
        if unknown:
            raise ToolError(f"Tool '{tool['name']}' got unexpected "
                            f"arguments: {unknown}")
        missing = sorted(p for p, spec in declared.items()
                         if spec['required'] and p not in supplied)
        if missing:
            raise ToolError(f"Tool '{tool['name']}' missing required "
                            f"arguments: {missing}. "
                            f"Signature: {{{', '.join(declared)}}}")

        # Coerce types per manifest
        clean = {}
        for pname, value in supplied.items():
            ptype = declared[pname]['type'].lower()
            try:
                if ptype == 'number':
                    clean[pname] = float(value) if '.' in str(value) \
                        else int(value)
                elif ptype == 'boolean':
                    clean[pname] = bool(value) if isinstance(value, bool) \
                        else str(value).lower() in ('1', 'true', 'yes', 'on')
                else:
                    clean[pname] = str(value)
            except (TypeError, ValueError):
                raise ToolError(f"Argument '{pname}' must be {ptype}.")

        # URL placeholder substitution
        url = tool['url']
        for match in list(_URL_PARAM_RE.finditer(url)):
            pname = match.group(1)
            if pname not in clean:
                raise ToolError(f"URL placeholder '{pname}' not supplied.")
            url = url.replace(match.group(0),
                              requests.utils.quote(str(clean.pop(pname)),
                                                   safe=''))

        headers = dict(tool['headers'])
        if tool['auth_env']:
            token = os.getenv(tool['auth_env'], '')
            if token:
                headers.setdefault('Authorization', f'Bearer {token}')

        method = tool['method']
        try:
            if method == 'GET':
                resp = requests.get(url, params=clean, headers=headers,
                                    timeout=tool['timeout'])
            else:
                resp = requests.request(method, url, json=clean,
                                        headers=headers,
                                        timeout=tool['timeout'])
        except requests.RequestException as e:
            logger.warning(f"Tool '{tool['name']}' request failed: {e}")
            return False, f"Tool '{tool['name']}' request failed: {e}"

        body = (resp.text or '').strip()
        if len(body) > 1500:
            body = body[:1500] + "…[truncated]"

        if resp.status_code >= 400:
            return False, (f"Tool '{tool['name']}' returned HTTP "
                           f"{resp.status_code}: {body[:400]}")

        result = (f"Tool '{tool['name']}' succeeded"
                  f" (HTTP {resp.status_code}).\n{body}") \
            if body else f"Tool '{tool['name']}' succeeded (no content)."

        # Best-effort pretty-print JSON responses
        try:
            parsed = resp.json()
            pretty = json.dumps(parsed, indent=2)[:1200]
            result = (f"Tool '{tool['name']}' succeeded"
                      f" (HTTP {resp.status_code}).\n{pretty}")
        except (ValueError, TypeError):
            pass
        return True, result


if __name__ == '__main__':
    reg = ToolRegistry()
    print(reg.render_catalog() or "(no tools registered)")
