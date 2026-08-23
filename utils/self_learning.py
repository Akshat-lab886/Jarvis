"""
Jarvis Autonomous Procedural Learning
=====================================

Closes the learning loop: after every complex task the agent reviews
its own performance and (a) distills reusable LESSONS that bias future
reasoning, and (b) abstracts successful code into permanent SKILLS —
without any manual intervention.

Artifacts:
    brain/data/lessons.md        human-readable, model-injected
    skills/<name>.json           auto-generated skills (flagged)

Rate limits keep this from polluting memory: max 40 lessons (oldest
pruned), max 3 auto-skills per day.  Disable entirely with
JARVIS_AUTO_LEARN=0.
"""

import os
import re
import json
import datetime
import logging

logger = logging.getLogger("Jarvis.SelfLearning")

MAX_LESSONS = 40
MAX_AUTOSKILLS_PER_DAY = 3
LESSON_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'brain', 'data', 'lessons.md')


def enabled():
    return os.getenv('JARVIS_AUTO_LEARN', '1') != '0'


# ---------------------------------------------------------------------- #
# Lessons store
# ---------------------------------------------------------------------- #
def _load_lessons():
    lessons = []
    try:
        if os.path.exists(LESSON_FILE):
            with open(LESSON_FILE, 'r') as f:
                for line in f:
                    m = re.match(r'- \[(.*?)\] \((.*?)\) (.*)', line.strip())
                    if m:
                        lessons.append({'date': m.group(1),
                                        'scope': m.group(2),
                                        'text': m.group(3)})
    except Exception as e:
        logger.warning(f"lessons read failed: {e}")
    return lessons


def _save_lessons(lessons):
    try:
        os.makedirs(os.path.dirname(LESSON_FILE), exist_ok=True)
        with open(LESSON_FILE, 'w') as f:
            f.write("# JARVIS SELF-LEARNED HEURISTICS\n")
            f.write("# Distilled from task reflections. Injected into "
                    "system prompt.\n\n")
            for l in lessons[-MAX_LESSONS:]:
                f.write(f"- [{l['date']}] ({l['scope']}) {l['text']}\n")
    except Exception as e:
        logger.warning(f"lessons write failed: {e}")


def add_lesson(text, scope='general'):
    """Append a deduplicated lesson; prunes to MAX_LESSONS."""
    text = str(text or '').strip()
    if not text or not enabled():
        return False
    lessons = _load_lessons()
    # crude dedupe on normalized text
    norm = re.sub(r'\W+', '', text.lower())
    for l in lessons:
        if re.sub(r'\W+', '', l['text'].lower()) == norm:
            return False
    today = datetime.date.today().isoformat()
    lessons.append({'date': today, 'scope': scope[:24], 'text': text[:220]})
    if len(lessons) > MAX_LESSONS:
        lessons = lessons[-MAX_LESSONS:]
    _save_lessons(lessons)
    logger.info(f"Lesson learned ({scope}): {text[:80]}")
    return True


def render_lessons(max_chars=900):
    """Lessons block for system-prompt injection ('' when empty/off)."""
    if not enabled():
        return ""
    lessons = _load_lessons()
    if not lessons:
        return ""
    lines = ["SELF-LEARNED HEURISTICS (from past task reflections):"]
    for l in lessons[-12:]:
        lines.append(f"- {l['text']}")
    block = "\n".join(lines)
    if len(block) > max_chars:
        block = block[:max_chars] + "\n…"
    return block


# ---------------------------------------------------------------------- #
# Auto skill abstraction
# ------------------------------------------------------------------ #
def _autoskill_count_today():
    try:
        skills_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'skills')
        today = datetime.date.today().isoformat()
        n = 0
        for fname in os.listdir(skills_dir):
            if not fname.endswith('.json'):
                continue
            with open(os.path.join(skills_dir, fname)) as f:
                data = json.load(f)
            meta = data.get('metadata') or {}
            if (meta.get('auto_generated')
                    and str(meta.get('created_date')) == today):
                n += 1
        return n
    except Exception:
        return 0


def extract_and_save_skill(brain, task_description, step_text, code,
                           registry):
    """
    Ask the planner persona whether a successful script is generically
    reusable; if so, save it as an auto-generated skill.

    Returns confirmation string or '' when nothing was saved.
    """
    if not enabled() or not code:
        return ''
    if _autoskill_count_today() >= MAX_AUTOSKILLS_PER_DAY:
        logger.info("auto-skill daily cap reached — skipping abstraction")
        return ''

    prompt = (
        "A task step just succeeded. Decide if its script is a "
        "generically REUSABLE tool worth saving as a named skill "
        "(not one-off data crunching).\n\n"
        f"Overall task: {task_description[:200]}\n"
        f"Step: {step_text[:150]}\n"
        f"Script:\n{code[:1200]}\n\n"
        "If NOT reusable output exactly: NO\n"
        "If reusable output ONLY raw JSON:\n"
        '{"name": "<snake_case_name>", "description": "<one line>", '
        '"params": {"<param>": "<what it means>"}, '
        '"template": "<script with {{param}} placeholders>"}'
    )
    raw = None
    try:
        raw = brain.complete(prompt, agent='planner', timeout=60,
                             max_tokens=1400)
    except Exception as e:
        logger.warning(f"skill-abstraction call failed: {e}")
    if not raw:
        return ''
    cleaned = raw.strip()
    if cleaned.upper().startswith('NO'):
        return ''

    import json as _json
    try:
        match = re.search(r'\{.*\}', cleaned, re.DOTALL)
        spec = _json.loads(match.group(0)) if match else None
        if not isinstance(spec, dict) or not spec.get('name'):
            return ''
        msg = registry.save_skill(
            spec['name'], spec.get('description', ''),
            spec.get('template') or code,
            params=spec.get('params'))
        # tag provenance
        skill = registry.get_skill(registry._slugify(spec['name']))
        if skill is not None:
            skill.setdefault('metadata', {})
            skill['metadata']['auto_generated'] = True
            skill['metadata']['created_date'] = \
                datetime.date.today().isoformat()
            path = registry._path_for(skill['name'])
            with open(path, 'w') as f:
                _json.dump(skill, f, indent=2)
        logger.info(f"Auto-skill abstracted: {msg}")
        return f"(learned new skill: {spec['name']})"
    except Exception as e:
        logger.warning(f"skill abstraction parse failed: {e}")
        return ''


# ---------------------------------------------------------------------- #
# Post-task hook (called by ComplexTaskManager._run_task)
# ---------------------------------------------------------------------- #
def review_task(brain, task, recovery=None, coder=None, registry=None):
    """
    Fire-and-forget self-review of a finished task:
      - failures → critic lesson
      - success w/ code steps → possible auto-skill
    Safe to call from the worker thread; swallows all errors.
    """
    if not enabled() or brain is None or task is None:
        return

    failed = [s for s in task.steps if s.status.value == 'failed']
    done_code_steps = [s for s in task.steps
                       if s.status.value == 'completed'
                       and str(getattr(s, 'type', '')).endswith('code')]

    # ---- Lesson from failure/recovery -------------------------------- #
    if failed:
        failure_lines = "\n".join(
            f"- Step '{s.text[:70]}' failed: {(s.error or '')[:120]}"
            for s in failed[:4])
        prompt = (
            f"A multi-step task just finished WITH FAILURES.\n"
            f"Goal: {task.description[:200]}\n{failure_lines}\n"
            + (f"\nA recovery attempt {'succeeded' if recovery and recovery.get('recovered') else 'also failed'}."
               if recovery else "")
            + "\n\nState ONE generalizable, actionable heuristic "
              "(≤25 words) that would help avoid this failure class in "
              "future tasks. Output ONLY the heuristic sentence."
        )
        try:
            lesson = brain.complete(prompt, agent='critic', timeout=45,
                                    max_tokens=80)
            if lesson and len(lesson.strip()) > 8:
                add_lesson(lesson.strip(), scope='task-failure')
        except Exception as e:
            logger.debug(f"lesson generation skipped: {e}")

    # ---- Auto skill abstraction --------------------------------------- #
    if done_code_steps and registry is not None:
        best = done_code_steps[0]
        result_text = str(best.result or '')
        # Only abstract when we can recover plausible source-ish content
        if 60 < len(result_text) < 2000 and 'Output:' in result_text:
            code_guess = result_text.split('Output:', 1)[-1].strip()
            try:
                note = extract_and_save_skill(
                    brain, task.description, best.text, code_guess,
                    registry)
                if note and hasattr(task, 'context'):
                    task.context += f"\n[SelfLearning] {note}\n"
            except Exception as e:
                logger.debug(f"auto-skill skipped: {e}")
