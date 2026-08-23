"""
Jarvis Skill Registry
=====================

Turns successful one-off generated scripts into reusable, named skills —
the "dynamic learning loop" of the assistant.

Storage: one JSON file per skill in ``<project>/skills/``::

    {
      "name": "word_count",
      "description": "Counts words in the given text",
      "params": {"text": "The text to analyze"},
      "code": "print(len('{{text}}'.split()))",
      "created_at": "2026-08-22T10:00:00",
      "runs": 3,
      "last_run": "..."
    }

Skills are executed through the Coder engine (same safety checks and,
later, the Docker sandbox).  Parameters use ``{{param_name}}``
placeholders that are substituted before execution.
"""

import os
import re
import json
import datetime
import logging

logger = logging.getLogger("Jarvis.Skills")


class SkillError(Exception):
    """Raised for invalid skill names, missing params, or bad definitions."""
    pass


_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


class SkillRegistry:
    def __init__(self, skills_dir=None):
        self.base_dir = os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))
        self.skills_dir = skills_dir or os.path.join(self.base_dir, 'skills')
        os.makedirs(self.skills_dir, exist_ok=True)

    # ------------------------------------------------------------------ #
    # Storage helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _slugify(name):
        slug = re.sub(r'[^a-z0-9_]+', '_', str(name).strip().lower())
        return slug.strip('_')

    def _path_for(self, name):
        slug = self._slugify(name)
        if not slug:
            raise SkillError("Skill name needs at least one letter or digit.")
        return os.path.join(self.skills_dir, f"{slug}.json")

    def _load_file(self, path):
        with open(path, 'r') as f:
            return json.load(f)

    # ------------------------------------------------------------------ #
    # CRUD
    # ------------------------------------------------------------------ #
    def save_skill(self, name, description, code, params=None):
        """
        Validate and persist a skill.  Returns a human-readable message.

        ``params`` maps parameter name -> short description, e.g.
        {"city": "City to forecast for"}.
        """
        if not name or not str(name).strip():
            raise SkillError("A skill name is required.")
        if not code or not str(code).strip():
            raise SkillError("Skill code cannot be empty.")

        code = str(code)
        try:
            compile(code, self._slugify(name), 'exec')
        except SyntaxError as e:
            raise SkillError(f"Skill code has a syntax error: {e}")

        declared = {}
        if isinstance(params, dict):
            declared = {str(k): str(v) for k, v in params.items()}
        elif isinstance(params, (list, tuple)):
            declared = {str(k): "" for k in params}

        used = set(_PLACEHOLDER_RE.findall(code))
        undeclared = sorted(used - set(declared))
        if undeclared:
            # Auto-declare placeholders found in code so old callers
            # that omit the params map still produce valid skills.
            for p in undeclared:
                declared[p] = ""

        record = {
            "name": self._slugify(name),
            "display_name": str(name).strip(),
            "description": str(description or "").strip(),
            "params": declared,
            "code": code,
            "created_at": datetime.datetime.now().isoformat(),
            "runs": 0,
        }
        path = self._path_for(record["name"])
        with open(path, 'w') as f:
            json.dump(record, f, indent=2)

        logger.info(f"Saved skill '{record['name']}' "
                    f"({len(declared)} params)")
        return f"Skill '{record['name']}' saved."

    def get_skill(self, name):
        """Return the full skill record or None."""
        try:
            path = self._path_for(name)
        except SkillError:
            return None
        if not os.path.exists(path):
            return None
        try:
            return self._load_file(path)
        except Exception as e:
            logger.warning(f"Corrupt skill file for '{name}': {e}")
            return None

    def delete_skill(self, name):
        path = self._path_for(name)
        if os.path.exists(path):
            os.remove(path)
            return f"Skill '{self._slugify(name)}' deleted."
        return f"No skill named '{self._slugify(name)}'."

    def list_skills(self):
        """Return compact metadata for every stored skill."""
        out = []
        try:
            entries = sorted(os.listdir(self.skills_dir))
        except FileNotFoundError:
            return out
        for fname in entries:
            if not fname.endswith('.json'):
                continue
            try:
                data = self._load_file(os.path.join(self.skills_dir, fname))
                out.append({
                    "name": data.get("name"),
                    "display_name": data.get("display_name")
                                    or data.get("name"),
                    "description": data.get("description", ""),
                    "params": list((data.get("params") or {}).keys()),
                    "runs": data.get("runs", 0),
                })
            except Exception:
                continue
        return out

    def count(self):
        return len([f for f in os.listdir(self.skills_dir)
                    if f.endswith('.json')]) \
            if os.path.isdir(self.skills_dir) else 0

    # ------------------------------------------------------------------ #
    # Execution + prompt integration
    # ------------------------------------------------------------------ #
    def run(self, name, params=None, coder=None, timeout=60):
        """
        Execute a saved skill with parameter substitution.

        Returns the coder result dict {'success', 'output', ...}.
        Raises SkillError for unknown skills / bad parameters.
        """
        skill = self.get_skill(name)
        if not skill:
            known = ", ".join(s["name"] for s in self.list_skills()[:8])
            raise SkillError(
                f"Unknown skill '{self._slugify(name)}'."
                + (f" Known skills: {known}" if known else
                   " No skills saved yet.")
            )

        supplied = dict(params or {})
        declared = skill.get("params") or {}

        unknown = sorted(set(supplied) - set(declared))
        if unknown:
            raise SkillError(
                f"Skill '{skill['name']}' takes "
                f"{sorted(declared)} — got unexpected {unknown}."
            )
        missing = sorted(set(declared) - set(supplied))
        if missing:
            raise SkillError(
                f"Skill '{skill['name']}' is missing parameters: {missing}."
                f" Descriptions: {declared}"
            )

        values = {
            k: (v if isinstance(v, str) else json.dumps(v))
            for k, v in supplied.items()
        }

        def _substitute(match):
            return values[match.group(1)]

        code = _PLACEHOLDER_RE.sub(_substitute, skill["code"])
        leftovers = _PLACEHOLDER_RE.findall(code)
        if leftovers:
            raise SkillError(f"Unresolved placeholders: {leftovers}")

        if coder is None:
            from utils.coder import Coder
            coder = Coder()

        result = coder.execute_with_retry(code, timeout=timeout)

        # Usage tracking (learning-loop telemetry)
        try:
            skill["runs"] = int(skill.get("runs", 0)) + 1
            skill["last_run"] = datetime.datetime.now()
            skill["last_run"] = skill["last_run"].isoformat()
            skill["last_success"] = bool(result.get('success'))
            with open(self._path_for(name), 'w') as f:
                json.dump(skill, f, indent=2)
        except Exception:
            pass

        return result

    def render_catalog(self, max_chars=1800):
        """
        Render the skill list for injection into the brain's system
        prompt.  Empty string when no skills exist.
        """
        skills = self.list_skills()
        if not skills:
            return ""
        lines = ["AVAILABLE SKILLS (reusable saved scripts):"]
        for s in skills:
            params = ", ".join(s["params"]) if s["params"] else "none"
            desc = s.get("description") or "(no description)"
            lines.append(
                f"- {s['name']} — {desc[:80]} | params: {params}"
            )
        catalog = "\n".join(lines)
        if len(catalog) > max_chars:
            catalog = catalog[:max_chars] + "\n…(truncated)"
        return catalog
