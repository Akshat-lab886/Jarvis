"""
J.A.R.V.I.S. — Skill Forge (Hermes parity: autonomous skill creation)
=====================================================================

The bounded learning loop that turns successful multi-turn workflows
into reusable, self-improving skills:

    1. WORKFLOW TELEMETRY — the agent loop reports every successful
       multi-tool run (``record_workflow``).  Nothing is sent anywhere;
       a bounded local log accumulates candidates.
    2. AUTONOMOUS CREATION — a background pass asks the coder persona
       to distill a candidate into either:
         * an executable skill  → saved through the SkillRegistry
           (``{{param}}`` slots, sandboxed execution, usage tracking)
         * a procedure skill    → a SKILL.md file (open agentskills.io
           progressive-disclosure format: YAML frontmatter +
           markdown instructions), loaded on demand so the catalog
           costs almost no tokens.
    3. SELF-IMPROVEMENT — every skill run is scored (success, duration).
       Failing skills get their code PATCHED by the coder persona;
       persistently slow skills get optimized; hopeless skills are
       retired with a note.

``JARVIS_SKILL_FORGE=0`` turns the whole loop off.  All LLM work runs
on a daemon thread — the forge never blocks chat.
"""

import os
import re
import json
import time
import threading
import datetime
import logging

logger = logging.getLogger("Jarvis.SkillForge")

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LEGACY_SKILLS_DIR = os.path.join(_BASE_DIR, 'skills')
SKILL_MD_DIR = os.path.join(_BASE_DIR, 'skills', 'md')

_WORKFLOW_LOG = os.path.join(_BASE_DIR, 'brain', 'data',
                             'skill_workflows.json')
_MAX_WORKFLOWS = 100          # bounded telemetry log
_MIN_TOOLS_FOR_CANDIDATE = 3  # "complex multi-turn" threshold
_ANALYZE_BATCH = 3


def _env_float(name, default, floor=None):
    """Parse a float env var safely: a malformed value falls back to the
    default instead of crashing the module at import, and a floor keeps
    a 0/negative value from turning the maintenance loop into a
    busy-spin."""
    try:
        value = float(os.getenv(name, default))
    except (TypeError, ValueError):
        value = float(default)
    return value if floor is None else max(floor, value)


_TICK_S = _env_float('JARVIS_SKILL_FORGE_TICK', '900', floor=30.0)
_PATCH_RETRIES = 2
_RETIRE_FAILURES = 5

_FRONTMATTER_RE = re.compile(r'^---\s*\n(.*?)\n---\s*\n(.*)$', re.DOTALL)


def enabled():
    return os.getenv('JARVIS_SKILL_FORGE', '1') != '0'


def _slugify(name):
    return re.sub(r'[^a-z0-9_]+', '_', str(name or '').strip().lower()).strip('_')


def _yaml_quote(value):
    """Double-quote a YAML scalar only when it would otherwise break a
    plain scalar — colons, hashes, quotes or leading/trailing space.
    SKILL.md frontmatter is an open format (agentskills.io) read by
    other tools, not just our naive loader, so malformed YAML here is a
    real interop bug — e.g. an LLM-authored description containing a
    double quote or a colon would otherwise corrupt the frontmatter.
    """
    s = str(value)
    if s and not re.search(r'[:#"\'\x00-\x1f]|^[ \t]|[ \t]$', s):
        return s
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _yaml_unquote(value):
    """Inverse of ``_yaml_quote`` for the naive frontmatter loader.

    ``_yaml_quote`` emits double-quoted scalars (with ``\\`` and ``\"``
    escaped) for values that would break a plain scalar.  A reader that
    just strips the value would hand back the YAML *literal* — outer
    quotes and backslash escapes included.  This undoes exactly the
    escapes we emit so internal consumers (catalog, read_skill) see the
    real string, matching what a real YAML parser returns.
    """
    v = str(value).strip()
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        inner = v[1:-1]
        out, i = [], 0
        while i < len(inner):
            c = inner[i]
            if c == '\\' and i + 1 < len(inner):
                nxt = inner[i + 1]
                if nxt in ('\\', '"'):
                    out.append('\\' if nxt == '\\' else '"')
                    i += 2
                    continue
            out.append(c)
            i += 1
        return ''.join(out)
    return v


def _yaml_key(key):
    """Sanitize a parameter name into a portable YAML mapping key.

    Keys are LLM-authored.  A key smuggling a colon or a raw newline
    (``params: {"a: b\\n  injected: 1": ...}``) would otherwise rewrite
    the frontmatter structure — corrupting the open SKILL.md format for
    every reader, exactly the interop hazard ``_yaml_quote`` guards
    against on values.  Unusable keys are dropped.
    """
    key = re.sub(r'[^A-Za-z0-9_]+', '_', str(key or '')).strip('_')
    key = key.lstrip('-')
    return key


# --------------------------------------------------------------------- #
# Progressive-disclosure SKILL.md layer (open standard)
# --------------------------------------------------------------------- #

def save_skill_md(name, description, instructions, params=None):
    """
    Persist a procedure skill as ``skills/md/<slug>/SKILL.md`` with YAML
    frontmatter.  Returns a status message (never raises).
    """
    slug = _slugify(name)
    if not slug:
        return "ERROR: skill name needs at least one letter or digit."
    params = params or {}
    lines = ["---"]
    lines.append(f"name: {slug}")
    desc = str(description or '').replace('\n', ' ')[:300]
    lines.append(f"description: {_yaml_quote(desc)}")
    if params:
        lines.append("parameters:")
        for k, v in params.items():
            key = _yaml_key(k)
            if not key:
                continue
            v = str(v).replace('\n', ' ')[:120]
            lines.append(f"  {key}: {_yaml_quote(v)}")
    lines.append("---")
    body = str(instructions or '').strip()
    if len(body) > 12000:
        body = body[:12000] + "\n…[truncated]"
    path = os.path.join(SKILL_MD_DIR, slug, 'SKILL.md')
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write("\n".join(lines) + "\n\n" + body + "\n")
        logger.info("saved SKILL.md skill '%s'", slug)
        return f"Skill '{slug}' saved (SKILL.md)."
    except Exception as e:
        return f"ERROR: could not save skill: {e}"


def load_skill_md(name):
    """Full record of a SKILL.md skill or None."""
    slug = _slugify(name)
    path = os.path.join(SKILL_MD_DIR, slug, 'SKILL.md')
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            raw = f.read()
        m = _FRONTMATTER_RE.match(raw.strip())
        meta, body = (m.group(1), m.group(2)) if m else ("", raw)
        header = {}
        for line in meta.splitlines():
            if ':' in line and not line.startswith(' '):
                k, v = line.split(':', 1)
                header[k.strip()] = _yaml_unquote(v)
        return {
            'name': header.get('name', slug),
            'description': header.get('description', ''),
            'instructions': body.strip(),
            'kind': 'md',
            'path': path,
        }
    except Exception as e:
        logger.warning("SKILL.md load failed for %s: %s", slug, e)
        return None


def list_skills_md():
    """Compact metadata for every SKILL.md skill (progressive disclosure:
    name + description only)."""
    out = []
    if not os.path.isdir(SKILL_MD_DIR):
        return out
    for entry in sorted(os.listdir(SKILL_MD_DIR)):
        rec = load_skill_md(entry)
        if rec:
            out.append({'name': rec['name'],
                        'description': rec['description'],
                        'kind': 'md'})
    return out


def read_skill(name):
    """
    Progressive disclosure: full body of a skill on demand — SKILL.md
    instructions, or the code of a legacy JSON skill.
    """
    rec = load_skill_md(name)
    if rec:
        return (f"SKILL {rec['name']} — {rec['description']}\n\n"
                f"{rec['instructions']}")
    try:
        from utils.skills import SkillRegistry
        legacy = SkillRegistry().get_skill(name)
    except Exception:
        legacy = None
    if legacy:
        return (f"SKILL {legacy['name']} — "
                f"{legacy.get('description', '')}\n\n"
                f"params: {legacy.get('params', {})}\n\n"
                f"```python\n{legacy['code']}\n```")
    return f"No skill named '{name}'."


def render_catalog(max_chars=1600):
    """Merged catalog: legacy executable skills + SKILL.md procedures.
    Names and one-line descriptions only — the token-cheap tier."""
    entries = []
    try:
        from utils.skills import SkillRegistry
        for s in SkillRegistry().list_skills():
            entries.append({'name': s['name'],
                            'description': s.get('description', ''),
                            'kind': 'code'})
    except Exception:
        pass
    entries.extend(list_skills_md())
    if not entries:
        return ""
    lines = ["AVAILABLE SKILLS (call read_skill to load full instructions):"]
    for s in entries:
        lines.append(f"- {s['name']} [{s['kind']}] — "
                     f"{s['description'][:90]}")
    catalog = "\n".join(lines)
    return catalog[:max_chars] + ("\n…(truncated)"
                                  if len(catalog) > max_chars else "")


# --------------------------------------------------------------------- #
# The forge itself
# --------------------------------------------------------------------- #

class SkillForge:
    """Records workflows, distills skills, patches and retires them."""

    def __init__(self, workflows_file=None, registry=None):
        self.workflows_file = workflows_file or _WORKFLOW_LOG
        self._registry = registry          # lazy SkillRegistry
        self._lock = threading.RLock()
        self._running = False
        self._thread = None
        self._load()

    # ------------------------- telemetry --------------------------- #
    def _load(self):
        self._workflows = []
        try:
            if os.path.exists(self.workflows_file):
                with open(self.workflows_file, 'r') as f:
                    data = json.load(f)
                self._workflows = [
                    w for w in data.get('workflows', [])
                    if isinstance(w, dict)
                ][-_MAX_WORKFLOWS:]
        except Exception as e:
            logger.debug("workflow log load failed: %s", e)

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.workflows_file), exist_ok=True)
            with open(self.workflows_file, 'w') as f:
                json.dump({'workflows': self._workflows[-_MAX_WORKFLOWS:]},
                          f, indent=2)
        except Exception as e:
            logger.debug("workflow log save failed: %s", e)

    def record_workflow(self, prompt, steps, success=True):
        """
        Log a completed agent run as a skill-creation candidate.
        *steps* is a list of (tool_name, arguments) tuples.  Fire-safe.
        """
        if not enabled() or not success:
            return False
        try:
            steps = [(str(n), str(a or '')[:120])
                     for n, a in (steps or [])][:12]
            if len(steps) < _MIN_TOOLS_FOR_CANDIDATE:
                return False
            with self._lock:
                self._workflows.append({
                    'prompt': str(prompt)[:400],
                    'steps': steps,
                    'at': datetime.datetime.now().isoformat(),
                    'analyzed': False,
                })
                self._workflows = self._workflows[-_MAX_WORKFLOWS:]
                self._save()
            return True
        except Exception as e:
            logger.debug("workflow record failed: %s", e)
            return False

    def pending(self):
        with self._lock:
            return [w for w in self._workflows if not w.get('analyzed')]

    # ---------------------- autonomous creation --------------------- #
    def analyze(self, brain):
        """
        Distill up to _ANALYZE_BATCH un-analyzed successful workflows
        into skills.  Returns the number created (0 when nothing to do,
        or when the brain is unavailable).
        """
        if not enabled() or brain is None:
            return 0
        todo = self.pending()[:_ANALYZE_BATCH]
        created = 0
        for wf in todo:
            try:
                if self._distill(brain, wf):
                    created += 1
            except Exception as e:
                logger.debug("distill failed: %s", e)
            with self._lock:
                wf['analyzed'] = True
        if todo:
            self._save()
        return created

    def _distill(self, brain, workflow):
        steps_text = "\n".join(
            f"{i+1}. {name}({args})" for i, (name, args)
            in enumerate(workflow.get('steps', [])))
        prompt = (
            "A personal assistant repeatedly succeeds at this kind of "
            "request:\n\n"
            f"REQUEST: {workflow.get('prompt', '')[:300]}\n"
            f"TOOL SEQUENCE THAT WORKED:\n{steps_text}\n\n"
            "Distill this into ONE reusable skill the assistant can run "
            "next time.  Decide the format:\n"
            "- If it is pure executable logic, emit JSON: "
            '{"name":"snake_case","description":"...",'
            '"params":{"p":"desc"},"code":"python code with {{p}} '
            'slots"}\n'
            "- If it is a procedure (tool orchestration, judgment), emit "
            "JSON: "
            '{"name":"snake_case","description":"...",'
            '"format":"md","instructions":"step-by-step markdown '
            'instructions"}\n'
            "Emit ONLY the JSON object. If the workflow is too trivial "
            'to deserve a skill, emit {"skip": true}.'
        )
        try:
            raw = brain.complete(prompt, agent='coder',
                                 timeout=60, max_tokens=900)
        except Exception as e:
            logger.debug("forge LLM call failed: %s", e)
            return False
        data = self._parse_json(raw)
        if not data or data.get('skip'):
            return False
        name = _slugify(data.get('name'))
        desc = str(data.get('description') or '')[:300]
        if not name or not desc:
            return False
        if str(data.get('format', '')).lower() == 'md' or \
                (not data.get('code') and data.get('instructions')):
            msg = save_skill_md(name, desc, data.get('instructions'),
                                data.get('params'))
        else:
            try:
                from utils.skills import SkillRegistry
                if self._registry is None:
                    self._registry = SkillRegistry()
                msg = self._registry.save_skill(
                    name, desc, data.get('code', ''),
                    data.get('params'))
            except Exception as e:
                logger.info("forge skill save rejected: %s", e)
                return False
        ok = not str(msg).startswith('ERROR')
        if ok:
            logger.info("skill forge created '%s' from workflow: %s",
                        name, msg)
        return ok

    @staticmethod
    def _parse_json(raw):
        text = str(raw or '').strip()
        if text.startswith('```'):
            text = re.sub(r'^```(json)?|```$', '', text).strip()
        try:
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if match:
                return json.loads(match.group(0))
        except (json.JSONDecodeError, ValueError):
            pass
        return None

    # ---------------------- self-improvement ------------------------ #
    def note_run(self, name, ok, duration_ms=None):
        """
        Runtime scoring hook — called after every skill execution.
        Failure streaks trigger a patch attempt; hopeless skills retire.
        """
        if not enabled():
            return
        try:
            path = os.path.join(_LEGACY_SKILLS_DIR,
                                f"{_slugify(name)}.json")
            if not os.path.exists(path):
                return
            with open(path, 'r') as f:
                skill = json.load(f)
            skill.setdefault('failures', 0)
            skill.setdefault('runs', 0)
            timings = skill.setdefault('timings_ms', [])
            if duration_ms is not None:
                timings.append(int(duration_ms))
                skill['timings_ms'] = timings[-20:]
                avg = sum(timings) // max(1, len(timings))
                skill['avg_ms'] = avg
            skill['failures'] = max(0, skill['failures'] + (-1 if ok else 1))
            with open(path, 'w') as f:
                json.dump(skill, f, indent=2)
        except Exception as e:
            logger.debug("note_run failed for %s: %s", name, e)

    def needs_patch(self, name):
        """A skill qualifies for a patch attempt when it keeps failing."""
        try:
            path = os.path.join(_LEGACY_SKILLS_DIR,
                                f"{_slugify(name)}.json")
            if not os.path.exists(path):
                return False
            with open(path, 'r') as f:
                skill = json.load(f)
            return int(skill.get('failures', 0)) >= 2
        except Exception:
            return False

    def patch(self, brain, name, last_error=''):
        """
        Ask the coder persona to fix a failing skill's code.  Returns a
        status message.  Syntax-validated before saving.
        """
        if not enabled() or brain is None:
            return "Skill forge disabled or no brain available."
        try:
            from utils.skills import SkillRegistry
            if self._registry is None:
                self._registry = SkillRegistry()
        except Exception as e:
            return f"ERROR: registry unavailable: {e}"
        skill = self._registry.get_skill(name)
        if not skill:
            return f"No executable skill named '{name}' to patch."
        prompt = (
            "This saved skill keeps failing.  Fix it.\n\n"
            f"NAME: {skill['name']}\n"
            f"DESCRIPTION: {skill.get('description', '')}\n"
            f"PARAMS: {skill.get('params', {})}\n"
            f"CODE:\n```python\n{skill['code']}\n```\n"
            f"LAST ERROR: {str(last_error)[:400]}\n\n"
            "Return ONLY the corrected python code — same {{param}} "
            "slots, no commentary."
        )
        try:
            raw = brain.complete(prompt, agent='coder',
                                 timeout=60, max_tokens=1200)
        except Exception as e:
            return f"ERROR: patch call failed: {e}"
        code = str(raw or '').strip()
        code = re.sub(r'^```(python)?|```$', '', code).strip()
        if not code:
            return "ERROR: patch produced no code."
        try:
            from utils.skills import SkillRegistry
            msg = self._registry.save_skill(
                skill['name'],
                skill.get('description', '') + " (self-patched)",
                code, skill.get('params'))
        except Exception as e:
            return f"ERROR: patched code rejected: {e}"
        # Reset the failure streak so it gets a fresh chance.  Slugify
        # like every other file lookup here — a stored `name` field that
        # is not already a slug (e.g. hand-authored skills) must still
        # resolve to the same <slug>.json that note_run/needs_patch use.
        try:
            path = os.path.join(_LEGACY_SKILLS_DIR,
                                f"{_slugify(skill['name'])}.json")
            with open(path, 'r') as f:
                patched = json.load(f)
            patched['failures'] = 0
            patched['revisions'] = int(patched.get('revisions', 0)) + 1
            with open(path, 'w') as f:
                json.dump(patched, f, indent=2)
        except Exception:
            pass
        logger.info("skill forge patched '%s'", skill['name'])
        return f"Patched skill '{skill['name']}': {msg}"

    def improve(self, brain):
        """
        One maintenance pass: patch the worst failing skill.  Returns a
        status string ("" when nothing to do).
        """
        if not enabled() or brain is None:
            return ""
        worst, worst_fails = None, 0
        try:
            if not os.path.isdir(_LEGACY_SKILLS_DIR):
                return ""
            for fname in os.listdir(_LEGACY_SKILLS_DIR):
                if not fname.endswith('.json'):
                    continue
                try:
                    with open(os.path.join(_LEGACY_SKILLS_DIR,
                                           fname)) as f:
                        skill = json.load(f)
                    fails = int(skill.get('failures', 0))
                    if fails > worst_fails:
                        worst, worst_fails = skill.get('name'), fails
                except Exception:
                    continue
        except Exception:
            return ""
        if worst and worst_fails >= 2:
            return self.patch(brain, worst)
        return ""

    # --------------------------- daemon ----------------------------- #
    def start(self, brain):
        """Background maintenance loop (analyze + improve)."""
        if self._running or not enabled():
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, args=(brain,), daemon=True,
            name="SkillForge")
        self._thread.start()
        logger.info("skill forge maintenance started")

    def stop(self):
        self._running = False

    def _loop(self, brain):
        while self._running:
            try:
                self.analyze(brain)
                self.improve(brain)
            except Exception as e:
                logger.debug("forge tick failed: %s", e)
            time.sleep(_TICK_S)


# --------------------------------------------------------------------- #
# Singleton
# --------------------------------------------------------------------- #

_singleton = None
_singleton_lock = threading.Lock()


def get_forge():
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = SkillForge()
        return _singleton


def _reset_singleton():
    """Test helper."""
    global _singleton
    with _singleton_lock:
        _singleton = None
