"""
Offline tests for the Hermes-parity stack — the 16 modules that brought
Jarvis to feature parity with the Hermes agent platform — plus their
env kill switches:

    guardrails        USER.md / MEMORY.md / .jarvis.md self-updating
    context_injection @marker expansion (files, dirs, git, URLs)
    skill_forge       workflow telemetry → SKILL.md creation → patching
    runtimes          multi-backend execution (local offline; argv shapes)
    python_rpc        persistent sandboxed worker (real subprocess, stdlib)
    checkpoints       snapshot / list / rollback / prune
    lsp_diagnostics   post-edit syntax + checker tiering
    computer_use      tier selection + keycode mapping (no clicking!)
    gateways          bot-mode routing, Slack/Discord adapters, hub
    mcp_server        JSON-RPC 2.0 stdio protocol round-trip
    moa               mixture-of-agents panel + synthesis
    wake_word         phrase matching + stripping
    hyperframe        storyboard parsing + HTML rendering
    tui               slash-command dispatch + live status feed

No network, no server, no real providers: scripted fakes stand in for
the brain/router/registry and every store uses temp dirs.
"""

import os
import io
import sys
import json
import time
import shutil
import tempfile
import unittest
import platform
import datetime
from contextlib import redirect_stdout
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Hard-block any real provider calls from unit tests (.env may carry keys)
from config import Config
Config.GOOGLE_API_KEY = None

# Keep self-learning artifacts OUT of the real store during tests
import utils.self_learning as _sl
_sl.LESSON_FILE = os.path.join(
    tempfile.mkdtemp(prefix='jarvis_parity_t_'), 'lessons.md')

from utils.llm.providers.base import ChatResult, Usage    # noqa: E402

import utils.guardrails as guardrails                     # noqa: E402
import utils.context_injection as ci                      # noqa: E402
import utils.skill_forge as forge_mod                     # noqa: E402
import utils.runtimes as runtimes                         # noqa: E402
import utils.python_rpc as python_rpc                     # noqa: E402
import utils.checkpoints as checkpoints                   # noqa: E402
import utils.lsp_diagnostics as lsp                       # noqa: E402
import utils.computer_use as computer_use                 # noqa: E402
import utils.gateways as gateways                         # noqa: E402
import utils.mcp_server as mcp_server                     # noqa: E402
import utils.moa as moa                                   # noqa: E402
import utils.wake_word as wake_word                       # noqa: E402
import utils.hyperframe as hyperframe                     # noqa: E402
import utils.tui as tui                                   # noqa: E402
import utils.recurring as recurring                       # noqa: E402
import utils.meeting_audio as meeting_audio               # noqa: E402
import utils.brain as brain                               # noqa: E402
import utils.agent_loop as agent_loop                     # noqa: E402
import utils.event_bus as event_bus                       # noqa: E402
import utils.hud as hud                                   # noqa: E402
import utils.mobile_studio as mobile_studio               # noqa: E402
import utils.dev_studio as dev_studio                     # noqa: E402
import queue                                              # noqa: E402
import threading                                          # noqa: E402


def _make_tmp():
    return tempfile.mkdtemp(prefix="jarvis_parity_")


def _text_result(text):
    return ChatResult(text=text, finish_reason='stop', provider='fake',
                      model='fake-model', usage=Usage(10, 5))


class FakeBrain:
    """Scripted Brain.complete() stand-in keyed by agent profile."""

    def __init__(self, replies=None, default=None):
        self.replies = dict(replies or {})
        self.default = default
        self.calls = []

    def complete(self, prompt, system=None, timeout=60, max_tokens=None,
                 temperature=None, agent=None):
        self.calls.append(agent or 'default')
        reply = self.replies.get(agent, self.default)
        if callable(reply):
            reply = reply(prompt)
        return reply


class FakeUI:
    def __init__(self):
        self.said = []

    def say(self, text):
        self.said.append(str(text))


class FakeRegistry:
    """SkillRegistry stand-in for the forge."""

    def __init__(self):
        self.saved = []
        self.skills = {}

    def save_skill(self, name, description, code, params=None):
        self.saved.append(name)
        self.skills[name] = {'name': name, 'description': description,
                             'code': code, 'params': params or {}}
        return f"Skill '{name}' saved."

    def get_skill(self, name):
        return self.skills.get(name)

    def list_skills(self):
        return list(self.skills.values())


# ====================================================================== #
# Guardrails — USER.md / MEMORY.md / .jarvis.md
# ====================================================================== #

class TestGuardrails(unittest.TestCase):

    def setUp(self):
        self.tmp = _make_tmp()
        self.g = guardrails.Guardrails(
            user_md=os.path.join(self.tmp, 'USER.md'),
            memory_md=os.path.join(self.tmp, 'MEMORY.md'))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_update_creates_file_and_section(self):
        out = self.g.update('user', 'Preferences',
                            'Prefers concise replies')
        self.assertIn('Recorded', out)
        with open(self.g.user_md) as f:
            body = f.read()
        self.assertIn('## Preferences', body)
        self.assertIn('Prefers concise replies.', body)
        self.assertIn('# USER.md', body)

    def test_load_block_renders(self):
        self.g.update('user', 'Preferences', 'Prefers concise replies')
        block = self.g.load_block()
        self.assertIn('GUARDRAILS', block)
        self.assertIn('USER PROFILE', block)
        self.assertIn('concise replies', block)

    def test_duplicate_line_deduplicated(self):
        self.g.update('memory', 'Facts', 'Lives in Shimla')
        out = self.g.update('memory', 'Facts', 'Lives in Shimla')
        self.assertIn('Already recorded', out)
        with open(self.g.memory_md) as f:
            self.assertEqual(f.read().count('Lives in Shimla'), 1)

    def test_insert_before_next_section(self):
        with open(self.g.user_md, 'w') as f:
            f.write("# USER.md\n\n## Preferences\n- Old line\n\n"
                    "## Rules\n- Be terse\n")
        self.g.update('user', 'Preferences', 'New preference')
        with open(self.g.user_md) as f:
            body = f.read()
        self.assertLess(body.index('New preference'),
                        body.index('## Rules'))
        self.assertIn('- Old line', body)     # existing content intact

    def test_project_target_uses_env_dir(self):
        proj = os.path.join(self.tmp, 'proj')
        os.makedirs(proj)
        with patch.dict(os.environ, {'JARVIS_PROJECT_DIR': proj}):
            self.g.update('project', 'Boundaries',
                          'Never touch the migrations folder')
            self.assertTrue(os.path.exists(
                os.path.join(proj, '.jarvis.md')))
            block = self.g.load_block(project_dir=proj)
        self.assertIn('PROJECT BOUNDARIES', block)
        self.assertIn('migrations', block)

    def test_capture_insight_routes_by_kind(self):
        self.g.capture_insight('Prefers dark mode', 'preference')
        with open(self.g.user_md) as f:
            self.assertIn('dark mode', f.read())
        self.assertIsNone(self.g.capture_insight('x', 'unknown-kind'))

    def test_kill_switch(self):
        with patch.dict(os.environ, {'JARVIS_GUARDRAILS': '0'}):
            self.assertEqual(self.g.load_block(), '')
            out = self.g.update('user', 'Preferences', 'nope')
        self.assertIn('disabled', out.lower())
        self.assertFalse(os.path.exists(self.g.user_md))

    def test_update_invalidates_cache(self):
        self.g.update('user', 'Preferences', 'First fact')
        self.assertIn('First fact', self.g.load_block())
        self.g.update('user', 'Preferences', 'Second fact')
        self.assertIn('Second fact', self.g.load_block())

    def test_file_stays_bounded(self):
        for i in range(130):
            self.g.update('memory', 'Facts', f'uniquefact{i:03d} worth '
                                             f'keeping')
        with open(self.g.memory_md) as f:
            body = f.read()
        self.assertNotIn('uniquefact000', body)   # oldest evicted
        self.assertIn('uniquefact129', body)
        self.assertLessEqual(body.count('\n- '), 130)

    def test_truncation_preserves_structure(self):
        # Same eviction scenario, but the surviving file must still be
        # a well-formed markdown doc: title + section header above the
        # bullets — truncation must not leave bullets dangling.
        for i in range(130):
            self.g.update('memory', 'Facts', f'uniquefact{i:03d} worth '
                                             f'keeping')
        with open(self.g.memory_md) as f:
            body = f.read()
        self.assertTrue(body.startswith('# MEMORY.md'),
                        "title survived truncation")
        self.assertIn('\n## Facts\n', body,
                      "section header survives above its bullets")
        # The section header must precede its first bullet, not trail.
        self.assertLess(body.index('## Facts'), body.index('- uniquefact'))
        # A subsequent append reuses the same section (no duplicate header).
        self.g.update('memory', 'Facts', 'a fresh fact')
        with open(self.g.memory_md) as f:
            body2 = f.read()
        self.assertEqual(body2.count('## Facts'), 1)
        self.assertIn('- a fresh fact', body2)

    def test_load_block_cache_hit_honors_max_chars(self):
        self.g.update('user', 'Preferences', 'Prefers concise replies')
        full = self.g.load_block(max_chars=10 ** 9)     # warm the cache
        self.assertGreater(len(full), 100)
        short = self.g.load_block(max_chars=50)          # cached hit
        self.assertLessEqual(len(short), 50)

    def test_multi_section_bounding_never_orphans_bullets(self):
        # Two sections grown past max_lines: bounding must evict the
        # oldest bullets WITHOUT stranding the older section's surviving
        # bullets under a dropped header or losing the file title.  A
        # whole-file tail-cut used to cut above "## Facts" and leave its
        # tail dangling under "## Goals".
        facts = "".join(f"- fact{i:03d} value\n" for i in range(85))
        goals = "".join(f"- goal{i:03d} value\n" for i in range(45))
        with open(self.g.memory_md, 'w', encoding='utf-8') as f:
            f.write("# MEMORY.md\n\n## Facts\n" + facts +
                    "\n## Goals\n" + goals)
        self.g.update('memory', 'Goals', 'latest goal note')
        with open(self.g.memory_md, encoding='utf-8') as f:
            body = f.read()

        bullets = [ln for ln in body.split('\n') if ln.startswith('- ')]
        self.assertEqual(len(bullets), 120)              # bounded exactly
        self.assertTrue(body.startswith('# MEMORY.md'),
                        "title survived multi-section truncation")
        self.assertIn('latest goal note.', body)         # newest kept
        self.assertNotIn('fact000', body)                # oldest evicted

        # No bullet may precede the first section header (no orphans)...
        self.assertNotIn('- ', body[:body.index('## Facts')])
        # ...and every bullet sits under the section that owned it.
        seg_facts, _, seg_goals = body.partition('## Goals')
        for ln in seg_facts.split('\n'):
            if ln.startswith('- '):
                self.assertTrue(ln.startswith('- fact'),
                                f"Facts bullet migrated: {ln!r}")
        for ln in seg_goals.split('\n'):
            if ln.startswith('- '):
                self.assertTrue(
                    ln.startswith('- goal') or ln == '- latest goal note.',
                    f"Goals bullet migrated: {ln!r}")
        self.assertEqual(body.count('## Facts'), 1)
        self.assertEqual(body.count('## Goals'), 1)



# ====================================================================== #
# Dynamic context injection — @markers
# ====================================================================== #

class TestContextInjection(unittest.TestCase):

    def setUp(self):
        self.tmp = _make_tmp()
        self.notes = os.path.join(self.tmp, 'notes.md')
        with open(self.notes, 'w') as f:
            f.write("# Notes\nplan alpha is go\n" + "x" * 100)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_file_marker_expands(self):
        out = ci.expand(f"review @{self.notes} and summarize")
        self.assertIn('[CONTEXT EXPANSION]', out)
        self.assertIn('plan alpha is go', out)
        self.assertIn('review', out)              # original preserved

    def test_idempotent(self):
        once = ci.expand(f"read @{self.notes}")
        self.assertEqual(ci.expand(once), once)

    def test_directory_marker_expands(self):
        out = ci.expand(f"clean up @{self.tmp}")
        self.assertIn('[CONTEXT EXPANSION]', out)
        self.assertIn('notes.md', out)

    def test_git_marker_expands(self):
        out = ci.expand("what changed? @git")
        self.assertIn('[CONTEXT EXPANSION]', out)
        self.assertIn('git status', out)

    def test_url_marker_expands(self):
        with patch('utils.web_reader.extract_page_content',
                   return_value='PAGE TEXT ' * 40):
            out = ci.expand("summarize @https://example.com/article now")
        self.assertIn('[CONTEXT EXPANSION]', out)
        self.assertIn('PAGE TEXT', out)

    def test_email_never_expands(self):
        text = "email me at someone@example.com please"
        self.assertEqual(ci.expand(text), text)

    def test_unknown_marker_left_alone(self):
        text = "see @nonexistent_zz_file for details"
        self.assertEqual(ci.expand(text), text)

    def test_trailing_punctuation_stripped_from_token(self):
        out = ci.expand(f"review @{self.notes}.")
        self.assertIn('[CONTEXT EXPANSION]', out)

    def test_budget_caps_expansion(self):
        big = os.path.join(self.tmp, 'big.md')
        with open(big, 'w') as f:
            f.write("z" * 5000)
        out = ci.expand(f"read @{big}", total_cap=200)
        self.assertIn('[CONTEXT EXPANSION]', out)
        self.assertLess(len(out), 500)

    def test_notes_lists_markers(self):
        found = ci.notes(f"check @{self.notes} and @git")
        self.assertIn(self.notes, found)
        self.assertIn('git', found)

    def test_notes_strips_trailing_punctuation(self):
        # notes() is the UI hint for what will expand; a marker written
        # before a period must normalize the same way expand() does, or
        # the hint lies ("@notes.md." shown, nothing expands).
        found = ci.notes(f"check @{self.notes}.")
        self.assertIn(self.notes, found)
        self.assertNotIn(self.notes + '.', found)

    def test_kill_switch(self):
        text = f"read @{self.notes}"
        with patch.dict(os.environ, {'JARVIS_CONTEXT_INJECTION': '0'}):
            self.assertEqual(ci.expand(text), text)


# ====================================================================== #
# Skill forge — autonomous creation + self-improvement
# ====================================================================== #

class TestSkillForge(unittest.TestCase):

    def setUp(self):
        self.tmp = _make_tmp()
        self.md_dir = os.path.join(self.tmp, 'md')
        self.legacy_dir = os.path.join(self.tmp, 'legacy')
        os.makedirs(self.legacy_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_and_load_skill_md(self):
        with patch.object(forge_mod, 'SKILL_MD_DIR', self.md_dir):
            msg = forge_mod.save_skill_md(
                'Daily Digest', 'Compile the morning briefing',
                '## Steps\n1. gather\n2. render',
                params={'topics': 'comma list'})
            self.assertIn('saved', msg)
            rec = forge_mod.load_skill_md('daily digest')
        self.assertIsNotNone(rec)
        self.assertEqual(rec['name'], 'daily_digest')
        self.assertIn('briefing', rec['description'])
        self.assertIn('gather', rec['instructions'])

    def test_tick_env_parses_safely(self):
        # JARVIS_SKILL_FORGE_TICK is parsed at module scope: a malformed
        # value must fall back to the default, and a 0/negative value
        # must not turn the forge daemon's sleep(0) into a busy-spin.
        with patch.dict(os.environ, {'JARVIS_SKILL_FORGE_TICK': 'abc'}):
            self.assertEqual(forge_mod._env_float(
                'JARVIS_SKILL_FORGE_TICK', '900'), 900.0)
        with patch.dict(os.environ, {'JARVIS_SKILL_FORGE_TICK': '0'}):
            self.assertEqual(forge_mod._env_float(
                'JARVIS_SKILL_FORGE_TICK', '900', floor=30.0), 30.0)

    def test_yaml_frontmatter_quotes_hazardous_values(self):
        # An LLM-authored description may contain double quotes or colons.
        # Unquoted, those corrupt the SKILL.md frontmatter for any YAML
        # reader (the file claims the open agentskills.io format).
        with patch.object(forge_mod, 'SKILL_MD_DIR', self.md_dir):
            forge_mod.save_skill_md(
                'Risky Skill',
                'He said "ship it": now — #urgent',
                'just do it',
                params={'note': 'a: b "c"'})
            raw = open(os.path.join(self.md_dir, 'risky_skill',
                                    'SKILL.md')).read()
            rec = forge_mod.load_skill_md('risky skill')
        desc_line = next(l for l in raw.splitlines()
                         if l.startswith('description:'))
        # Double-quoted scalar with proper escaping, not a broken plain
        # scalar ("He said "ship it": now — #urgent" would be invalid).
        self.assertTrue(desc_line.startswith('description: "'))
        self.assertTrue(desc_line.endswith('"'))
        self.assertIn('ship it', desc_line)
        # Round-trip must return the REAL string, not the YAML literal —
        # a naive reader would hand back the outer quotes and the \" and
        # \\ escapes.  This is what a real YAML parser would return.
        self.assertEqual(
            rec['description'], 'He said "ship it": now — #urgent')

    def test_yaml_param_keys_are_sanitized(self):
        # Param names are LLM-authored.  A key smuggling a colon or a raw
        # newline must not become a real YAML mapping key (it would
        # rewrite the frontmatter structure for every reader).
        with patch.object(forge_mod, 'SKILL_MD_DIR', self.md_dir):
            forge_mod.save_skill_md(
                'Key Skill', 'desc', 'body',
                params={'good_key': 'ok',
                        'evil: key\n  injected: 1': 'x'})
            raw = open(os.path.join(self.md_dir, 'key_skill',
                                    'SKILL.md'), encoding='utf-8').read()
        self.assertIn('  good_key: ok', raw)
        self.assertIn('injected', raw)          # embedded, neutered
        self.assertNotIn('evil:', raw)          # no raw colon from the key
        self.assertNotIn('injected: 1', raw)    # no injected mapping line

    def test_read_skill_progressive_disclosure(self):
        with patch.object(forge_mod, 'SKILL_MD_DIR', self.md_dir):
            forge_mod.save_skill_md('Deploy Runbook', 'How to ship',
                                    '1. run tests\n2. deploy')
            body = forge_mod.read_skill('deploy runbook')
        self.assertIn('SKILL deploy_runbook', body)
        self.assertIn('run tests', body)

    def test_render_catalog_lists_md_skills(self):
        with patch.object(forge_mod, 'SKILL_MD_DIR', self.md_dir):
            forge_mod.save_skill_md('Deploy Runbook', 'How to ship',
                                    '1. tests')
            catalog = forge_mod.render_catalog()
        self.assertIn('AVAILABLE SKILLS', catalog)
        self.assertIn('deploy_runbook', catalog)
        self.assertIn('[md]', catalog)
        truncated = forge_mod.render_catalog(max_chars=40)
        self.assertLessEqual(len(truncated), 60)

    def test_record_workflow_requires_three_steps(self):
        forge = forge_mod.SkillForge(
            workflows_file=os.path.join(self.tmp, 'wf.json'))
        self.assertFalse(forge.record_workflow("do a thing",
                                               [('a', '{}'),
                                                ('b', '{}')]))
        self.assertTrue(forge.record_workflow(
            "complex thing", [('a', '{}'), ('b', '{}'), ('c', '{}')]))
        self.assertEqual(len(forge.pending()), 1)
        self.assertFalse(forge.record_workflow("failed thing",
                                               [('a', '{}')] * 3,
                                               success=False))

    def test_analyze_creates_md_skill(self):
        brain = FakeBrain({'coder': json.dumps({
            'name': 'deploy_check',
            'description': 'Verify the deploy is healthy',
            'format': 'md',
            'instructions': '1. hit /healthz\n2. report'})})
        forge = forge_mod.SkillForge(
            workflows_file=os.path.join(self.tmp, 'wf.json'))
        forge.record_workflow("check the deploy",
                              [('run_skill', '{}')] * 3)
        with patch.object(forge_mod, 'SKILL_MD_DIR', self.md_dir):
            created = forge.analyze(brain)
        self.assertEqual(created, 1)
        self.assertTrue(os.path.exists(
            os.path.join(self.md_dir, 'deploy_check', 'SKILL.md')))
        self.assertEqual(forge.pending(), [])   # analyzed even if created

    def test_analyze_skip_and_bad_json(self):
        forge = forge_mod.SkillForge(
            workflows_file=os.path.join(self.tmp, 'wf.json'))
        forge.record_workflow("trivial", [('a', '{}')] * 3)
        forge.record_workflow("broken", [('b', '{}')] * 3)
        self.assertEqual(
            forge.analyze(FakeBrain({'coder': '{"skip": true}'})), 0)
        self.assertEqual(
            forge.analyze(FakeBrain({'coder': 'not json at all'})), 0)
        self.assertEqual(len(forge.pending()), 0)

    def test_analyze_saves_code_skill_via_registry(self):
        brain = FakeBrain({'coder': json.dumps({
            'name': 'uptime_probe',
            'description': 'Probe uptime',
            'params': {'host': 'hostname'},
            'code': 'print("probe {{host}}")'})})
        registry = FakeRegistry()
        forge = forge_mod.SkillForge(
            workflows_file=os.path.join(self.tmp, 'wf.json'),
            registry=registry)
        forge.record_workflow("probe uptime",
                              [('run_skill', '{}')] * 3)
        with patch.object(forge_mod, 'SKILL_MD_DIR', self.md_dir):
            created = forge.analyze(brain)
        self.assertEqual(created, 1)
        self.assertEqual(registry.saved, ['uptime_probe'])

    def test_note_run_tracks_failures(self):
        skill_path = os.path.join(self.legacy_dir, 'flaky.json')
        with open(skill_path, 'w') as f:
            json.dump({'name': 'flaky', 'failures': 0}, f)
        forge = forge_mod.SkillForge(
            workflows_file=os.path.join(self.tmp, 'wf.json'))
        with patch.object(forge_mod, '_LEGACY_SKILLS_DIR',
                          self.legacy_dir):
            self.assertFalse(forge.needs_patch('flaky'))
            forge.note_run('flaky', ok=False, duration_ms=120)
            forge.note_run('flaky', ok=False, duration_ms=140)
            self.assertTrue(forge.needs_patch('flaky'))
            forge.note_run('flaky', ok=True)
            self.assertFalse(forge.needs_patch('flaky'))
        with open(skill_path) as f:
            skill = json.load(f)
        self.assertEqual(skill['failures'], 1)
        self.assertEqual(skill['avg_ms'], 130)

    def test_patch_resets_streak_for_non_slug_name(self):
        # A hand-authored skill whose stored `name` is not slugified
        # must still get its failure streak reset after a successful
        # patch — otherwise improve() re-patches it forever.
        skill_path = os.path.join(self.legacy_dir,
                                  'weather_forecast.json')
        with open(skill_path, 'w') as f:
            json.dump({'name': 'Weather Forecast', 'failures': 3,
                       'revisions': 0, 'code': 'print(1)'}, f)
        registry = FakeRegistry()
        registry.skills['Weather Forecast'] = {
            'name': 'Weather Forecast', 'description': 'Forecast',
            'params': {}, 'code': 'print(1)'}
        forge = forge_mod.SkillForge(
            workflows_file=os.path.join(self.tmp, 'wf.json'),
            registry=registry)
        with patch.object(forge_mod, '_LEGACY_SKILLS_DIR',
                          self.legacy_dir):
            msg = forge.patch(FakeBrain({'coder': 'print(2)'}),
                              'Weather Forecast')
        self.assertIn('Patched', msg)
        with open(skill_path) as f:
            updated = json.load(f)
        self.assertEqual(updated['failures'], 0)
        self.assertEqual(updated['revisions'], 1)

    def test_patch_fixes_and_resets_streak(self):
        skill_path = os.path.join(self.legacy_dir, 'broken.json')
        with open(skill_path, 'w') as f:
            json.dump({'name': 'broken', 'failures': 3, 'revisions': 0,
                       'description': 'd', 'code': 'x = 1'}, f)
        registry = FakeRegistry()
        registry.save_skill('broken', 'd', 'x = 1')
        forge = forge_mod.SkillForge(
            workflows_file=os.path.join(self.tmp, 'wf.json'),
            registry=registry)
        brain = FakeBrain({'coder': 'x = 2   # fixed'})
        with patch.object(forge_mod, '_LEGACY_SKILLS_DIR',
                          self.legacy_dir):
            out = forge.patch(brain, 'broken', last_error='NameError: x')
        self.assertIn('Patched', out)
        self.assertIn('(self-patched)',
                      registry.skills['broken']['description'])
        with open(skill_path) as f:
            skill = json.load(f)
        self.assertEqual(skill['failures'], 0)
        self.assertEqual(skill['revisions'], 1)

    def test_improve_patches_worst_skill(self):
        with open(os.path.join(self.legacy_dir, 'worst.json'), 'w') as f:
            json.dump({'name': 'worst', 'failures': 4}, f)
        forge = forge_mod.SkillForge(
            workflows_file=os.path.join(self.tmp, 'wf.json'))
        with patch.object(forge_mod, '_LEGACY_SKILLS_DIR',
                          self.legacy_dir), \
                patch.object(forge_mod.SkillForge, 'patch') as p:
            p.return_value = 'Patched skill worst.'
            out = forge.improve(FakeBrain())
        self.assertEqual(out, 'Patched skill worst.')
        p.assert_called_once()

    def test_parse_json_variants(self):
        self.assertEqual(forge_mod.SkillForge._parse_json(
            '```json\n{"a": 1}\n```'), {'a': 1})
        self.assertEqual(forge_mod.SkillForge._parse_json(
            'noise {"b": 2} noise'), {'b': 2})
        self.assertIsNone(forge_mod.SkillForge._parse_json('no json'))

    def test_kill_switch(self):
        with patch.dict(os.environ, {'JARVIS_SKILL_FORGE': '0'}):
            forge = forge_mod.SkillForge(
                workflows_file=os.path.join(self.tmp, 'wf.json'))
            self.assertFalse(forge.record_workflow("x",
                                                   [('a', '{}')] * 3))
            self.assertEqual(forge.analyze(FakeBrain()), 0)


# ====================================================================== #
# Multi-backend runtimes
# ====================================================================== #

class TestRuntimes(unittest.TestCase):

    def test_local_command(self):
        res = runtimes.run_command("echo hello-runtime", backend='local')
        self.assertTrue(res['success'])
        self.assertIn('hello-runtime', res['stdout'])
        self.assertEqual(res['backend'], 'local')

    def test_empty_command_rejected(self):
        res = runtimes.run_command("   ", backend='local')
        self.assertFalse(res['success'])
        self.assertIn('no command', res['stderr'])

    def test_unknown_backend(self):
        res = runtimes.run_command("ls", backend='bogus')
        self.assertFalse(res['success'])
        self.assertIn('unknown backend', res['stderr'])

    def test_ssh_unconfigured_message(self):
        res = runtimes.run_command("ls", backend='ssh')
        self.assertFalse(res['success'])
        self.assertIn('JARVIS_SSH_HOST', res['stderr'])

    def test_ssh_argv_construction(self):
        env = {'JARVIS_SSH_HOST': 'box.example.com',
               'JARVIS_SSH_USER': 'ops', 'JARVIS_SSH_PORT': '2222'}
        with patch.dict(os.environ, env):
            argv = runtimes._ssh_argv("df -h")
        self.assertEqual(
            argv, ['ssh', '-o', 'BatchMode=yes', '-o',
                   'ConnectTimeout=10', '-p', '2222',
                   'ops@box.example.com', 'df -h'])

    def test_local_always_available(self):
        self.assertTrue(runtimes.backend_configured('local'))
        self.assertIn('local', runtimes.available_backends())
        self.assertIn('runtimes:', runtimes.describe())

    def test_kill_switch_forces_local(self):
        with patch.dict(os.environ, {'JARVIS_RUNTIMES': '0'}):
            self.assertFalse(runtimes.backend_configured('docker'))
            res = runtimes.run_command("echo x", backend='docker')
            self.assertEqual(res['backend'], 'local')
            self.assertTrue(res['success'])

    def test_docker_cli_missing_message(self):
        with patch.object(runtimes, '_cli_available', return_value=False):
            res = runtimes.run_command("echo x", backend='docker')
        self.assertFalse(res['success'])
        self.assertIn('docker CLI/daemon not available',
                      res['stderr'])

    def test_run_code_local_uses_coder(self):
        fake_coder = type('C', (), {})()
        fake_coder.execute_with_retry = lambda code, timeout=30: {
            'success': True, 'stdout': '42\n', 'stderr': ''}
        with patch.dict(os.environ, {'JARVIS_RUNTIMES': '0'}), \
                patch('utils.coder.Coder', return_value=fake_coder):
            res = runtimes.run_code("print(40 + 2)")
        self.assertTrue(res['success'])
        self.assertIn('42', res['stdout'])
        self.assertEqual(res['backend'], 'local')

    def test_run_code_empty(self):
        res = runtimes.run_code("   ")
        self.assertFalse(res['success'])
        self.assertIn('no code', res['stderr'])

    def test_heredoc_uses_random_delimiter(self):
        # A snippet that itself contains the fixed marker string must be
        # embedded verbatim — the heredoc terminator is a per-call
        # random token, so no code can truncate the script or smuggle
        # shell into the backend command.
        captured = {}

        def fake_run(argv, backend, timeout, cwd=None, env=None):
            captured['argv'] = argv
            return {'success': True, 'returncode': 0, 'stdout': '',
                    'stderr': '', 'backend': backend, 'timed_out': False}

        code = "print('x')\nJARVIS_EOF\nprint('y')"
        with patch.object(runtimes, '_cli_available', return_value=True), \
                patch.object(runtimes, '_run_argv', side_effect=fake_run):
            runtimes.run_code(code, backend='daytona')
        cmd = captured['argv'][-1]
        self.assertTrue(cmd.startswith("python3 - <<'JARVIS_EOF_"))
        # the full snippet, marker line included, survives to the backend
        self.assertIn("print('x')\nJARVIS_EOF\nprint('y')", cmd)
        # and the terminator is a fresh random token, not the marker
        self.assertRegex(cmd, r"JARVIS_EOF_[0-9a-f]{10}$")
        self.assertLess(cmd.index('JARVIS_EOF\nprint'),
                        cmd.rindex('JARVIS_EOF_'))

    def test_modal_unparseable_command_returns_error(self):
        # run_code embeds arbitrary Python in a heredoc; if that code
        # carries an unmatched quote the modal backend's shlex split
        # cannot parse it.  That must surface as a structured result,
        # never as a ValueError escaping run_command/run_code.
        env = {'JARVIS_MODAL_ENTRY': 'runner'}
        with patch.dict(os.environ, env), \
                patch.object(runtimes, '_cli_available',
                             return_value=True):
            res = runtimes.run_command("echo 'unterminated",
                                       backend='modal')
        self.assertFalse(res['success'])
        self.assertIn('shell-parseable', res['stderr'])
        self.assertEqual(res['backend'], 'modal')


# ====================================================================== #
# Python RPC — persistent sandboxed worker
# ====================================================================== #

class TestPythonRPC(unittest.TestCase):

    def setUp(self):
        self.rpc = python_rpc.PythonRPC()

    def tearDown(self):
        self.rpc.close()

    def test_simple_call(self):
        # The worker reports the repr of 'result'/'_'/'out' — the
        # convention for "the answer" — not of arbitrary names.
        res = self.rpc.call("result = 40 + 2")
        self.assertTrue(res['success'])
        self.assertEqual(res['repr'], '42')
        self.assertEqual(res['error'], '')

    def test_namespace_persists_between_calls(self):
        self.assertTrue(self.rpc.call("data = [3, 1, 2]")['success'])
        res = self.rpc.call("data.sort(); result = data")
        self.assertEqual(res['repr'], '[1, 2, 3]')
        res = self.rpc.call("result = sum(data)")
        self.assertEqual(res['repr'], '6')

    def test_stdout_captured(self):
        res = self.rpc.call("print('rpc hello')")
        self.assertIn('rpc hello', res['stdout'])

    def test_error_reported_not_fatal(self):
        res = self.rpc.call("1 / 0")
        self.assertFalse(res['success'])
        self.assertIn('ZeroDivisionError', res['error'])
        # the worker survives errors
        self.assertTrue(self.rpc.call("2 + 2")['success'])

    def test_timeout_recycles_worker(self):
        res = self.rpc.call("import time\ntime.sleep(8)", timeout=1)
        self.assertFalse(res['success'])
        self.assertIn('timed out', res['error'])
        self.assertFalse(self.rpc.stats()['alive'])
        # recycled: the next call works again
        self.assertEqual(self.rpc.call("result = 7 * 6")['repr'], '42')

    def test_reset_clears_namespace(self):
        self.rpc.call("marker = 123")
        self.assertIn('namespace cleared', self.rpc.reset())
        res = self.rpc.call("result = 'marker' in dir()")
        self.assertEqual(res['repr'], 'False')

    def test_empty_code(self):
        res = self.rpc.call("  ")
        self.assertFalse(res['success'])
        self.assertIn('no code', res['error'])

    def test_format_rendering(self):
        out = self.rpc.format("result = 5; print('fmt')")
        self.assertIn('fmt', out)
        self.assertIn('→ 5', out)

    def test_disabled_falls_back_to_coder(self):
        fake_coder = type('C', (), {})()
        fake_coder.execute_with_retry = lambda code, timeout=30: {
            'success': True, 'stdout': 'fallback', 'stderr': ''}
        with patch.dict(os.environ, {'JARVIS_PYTHON_RPC': '0'}), \
                patch('utils.coder.Coder', return_value=fake_coder):
            res = self.rpc.call("print('x')")
        self.assertTrue(res['success'])
        self.assertEqual(res['stdout'], 'fallback')

    def test_close_reaps_worker(self):
        """close() must fully reap the worker — no zombie, no runaway."""
        self.assertTrue(self.rpc.call("result = 1")['success'])
        pid = self.rpc._proc.pid
        self.rpc.close()
        self.assertIsNone(self.rpc._proc)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)          # raises if the pid is truly gone


# ====================================================================== #
# Checkpoints — snapshot / rollback
# ====================================================================== #

class TestCheckpoints(unittest.TestCase):

    def setUp(self):
        self.tmp = _make_tmp()
        self.proj = os.path.join(self.tmp, 'proj')
        os.makedirs(self.proj)
        self.app = os.path.join(self.proj, 'app.py')
        with open(self.app, 'w') as f:
            f.write("print('v1')\n")
        self.mgr = checkpoints.CheckpointManager(
            root=os.path.join(self.tmp, 'snaps'))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_create_and_rollback(self):
        with patch.object(checkpoints, '_BASE_DIR', self.tmp):
            msg = self.mgr.create('before-fix', [self.proj],
                                  note='pre-edit safety')
            self.assertIn('Checkpoint saved', msg)
            with open(self.app, 'w') as f:
                f.write("print('v2 broken')\n")
            out = self.mgr.rollback('before-fix')
            self.assertIn('restored', out)
        with open(self.app) as f:
            self.assertEqual(f.read(), "print('v1')\n")

    def test_rollback_defaults_to_newest(self):
        with patch.object(checkpoints, '_BASE_DIR', self.tmp):
            self.mgr.create('snap-one', [self.proj])
            with open(self.app, 'w') as f:
                f.write("print('v2')\n")
            self.mgr.create('snap-two', [self.proj])
            with open(self.app, 'w') as f:
                f.write("print('v3 broken')\n")
            out = self.mgr.rollback()
        self.assertIn('Rolled back', out)
        with open(self.app) as f:
            self.assertEqual(f.read(), "print('v2')\n")

    def test_list_and_stats(self):
        with patch.object(checkpoints, '_BASE_DIR', self.tmp):
            self.mgr.create('alpha', [self.proj])
            listing = self.mgr.list()
            self.assertIn('alpha', listing)
            stats = self.mgr.stats()
        self.assertEqual(stats['snapshots'], 1)
        self.assertTrue(stats['enabled'])

    def test_ambiguous_selector(self):
        root = os.path.join(self.tmp, 'snaps')
        for name, label in (('20260101_000001_a', 'same'),
                            ('20260101_000002_b', 'same')):
            d = os.path.join(root, name)
            os.makedirs(d)
            with open(os.path.join(d, 'manifest.json'), 'w') as f:
                json.dump({'label': label, 'files': []}, f)
        out = self.mgr.rollback('same')
        self.assertIn('ambiguous', out)

    def test_list_survives_corrupt_manifest(self):
        # A manifest that parses as JSON but whose 'files' is not a list
        # (hand-edited / torn write) must degrade in list() — one bad
        # snapshot must not take down the whole checkpoint listing.
        root = os.path.join(self.tmp, 'snaps')
        good = os.path.join(root, '20260101_000001_good')
        os.makedirs(good)
        with open(os.path.join(good, 'manifest.json'), 'w') as f:
            json.dump({'label': 'good', 'files': []}, f)
        bad = os.path.join(root, '20260101_000002_bad')
        os.makedirs(bad)
        with open(os.path.join(bad, 'manifest.json'), 'w') as f:
            json.dump({'label': 'bad', 'files': 42,
                       'note': {'x': 1}}, f)
        listing = self.mgr.list(limit=10)
        self.assertIn('bad', listing)
        self.assertIn('good', listing)

    def test_drop(self):
        with patch.object(checkpoints, '_BASE_DIR', self.tmp):
            self.mgr.create('tobedropped', [self.proj])
            out = self.mgr.drop('tobedropped')
        self.assertIn('deleted', out)
        self.assertEqual(self.mgr.stats()['snapshots'], 0)

    def test_same_second_creates_do_not_collide(self):
        # Two snapshots within one second must land in distinct dirs,
        # else the second overwrites the first manifest and a recovery
        # point silently vanishes.
        with patch.object(checkpoints, '_BASE_DIR', self.tmp):
            self.mgr.create('a', [self.proj])
            self.mgr.create('b', [self.proj])
        snaps = self.mgr._snapshots()
        self.assertEqual(len(snaps), 2)
        self.assertNotEqual(snaps[0][0], snaps[1][0])
        self.assertEqual({m['label'] for _, m in snaps},
                         {'a', 'b'})

    def test_rollback_never_escapes_repo_root(self):
        # Manifest entries with ``..`` traversal or an absolute path
        # must be skipped — rollback is confined to the repo root.
        abs_target = '/tmp/jarvis_abs_escape_test'
        with patch.object(checkpoints, '_BASE_DIR', self.tmp):
            snap_dir = os.path.join(self.mgr.root, 'evil_snap')
            os.makedirs(snap_dir)
            with open(os.path.join(snap_dir, 'x.py'), 'w') as f:
                f.write("pwned")
            with open(os.path.join(snap_dir, 'manifest.json'), 'w') as f:
                json.dump({'label': 'evil', 'files': [
                    '../../../tmp/jarvis_escape_test',
                    abs_target,
                    os.path.relpath(self.app, self.tmp),
                ]}, f)
            out = self.mgr.rollback('evil_snap')
        self.assertIn('restored', out)
        # The in-repo path (app.py) is restored...
        with open(self.app) as f:
            self.assertEqual(f.read(), "print('v1')\n")
        # ...and nothing was written outside the repo root.
        self.assertFalse(os.path.exists('/tmp/jarvis_escape_test'))
        self.assertFalse(os.path.exists(abs_target))

    def test_create_skips_out_of_repo_files(self):
        # A file outside the repo root must not be snapshotted: it would
        # be stored under ``..`` components rollback refuses to restore
        # (and writing it would physically escape the snapshot dir).
        outside_dir = os.path.join(self.tmp, '..', 'jarvis_outside_proj')
        outside = os.path.join(outside_dir, 'o.py')
        os.makedirs(outside_dir, exist_ok=True)
        with open(outside, 'w') as f:
            f.write("secret")
        try:
            with patch.object(checkpoints, '_BASE_DIR', self.tmp):
                self.mgr.create('outer', [self.proj, outside])
                snaps = self.mgr._snapshots()
            self.assertEqual(len(snaps), 1)
            # manifest lists only the in-repo file
            self.assertEqual(snaps[0][1]['files'], ['proj/app.py'])
            # snapshot dir holds exactly manifest + the one in-repo copy
            snap_dir = os.path.join(self.mgr.root, snaps[0][0])
            found = []
            for dirpath, _, filenames in os.walk(snap_dir):
                for fn in filenames:
                    found.append(os.path.relpath(
                        os.path.join(dirpath, fn), snap_dir))
            self.assertEqual(sorted(found),
                             ['manifest.json', 'proj/app.py'])
        finally:
            shutil.rmtree(outside_dir, ignore_errors=True)

    def test_kill_switch(self):
        with patch.dict(os.environ, {'JARVIS_CHECKPOINTS': '0'}), \
                patch.object(checkpoints, '_BASE_DIR', self.tmp):
            out = self.mgr.create('x', [self.proj])
            self.assertIn('disabled', out.lower())
            self.assertIn('disabled', self.mgr.rollback().lower())


# ====================================================================== #
# LSP diagnostics after edits
# ====================================================================== #

class TestLSPDiagnostics(unittest.TestCase):

    def setUp(self):
        self.tmp = _make_tmp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_check_code_bad_python(self):
        res = lsp.check_code("def broken(:\n    pass")
        self.assertFalse(res['ok'])
        self.assertEqual(res['checker'], 'compile')
        self.assertEqual(res['issues'][0]['severity'], 'error')

    def test_check_code_good(self):
        res = lsp.check_code("x = 1 + 2\n")
        self.assertTrue(res['ok'])
        self.assertEqual(res['issues'], [])

    def test_check_file_python_syntax_error(self):
        path = os.path.join(self.tmp, 'bad.py')
        with open(path, 'w') as f:
            f.write("def f(:\n")
        res = lsp.check_file(path)
        self.assertFalse(res['ok'])
        self.assertIsNotNone(res['issues'][0]['line'])

    def test_check_file_json(self):
        good = os.path.join(self.tmp, 'ok.json')
        with open(good, 'w') as f:
            f.write('{"a": 1}')
        self.assertTrue(lsp.check_file(good)['ok'])
        bad = os.path.join(self.tmp, 'bad.json')
        with open(bad, 'w') as f:
            f.write('{"a": ')
        res = lsp.check_file(bad)
        self.assertFalse(res['ok'])
        self.assertEqual(res['checker'], 'json')

    def test_missing_file_skipped(self):
        res = lsp.check_file(os.path.join(self.tmp, 'nope.py'))
        self.assertEqual(res['checker'], 'skipped')

    def test_render_clean_and_broken(self):
        self.assertEqual(lsp.render({'ok': True, 'issues': []}), '')
        block = lsp.render({'ok': False, 'issues': [
            {'line': 3, 'message': 'invalid syntax'}]})
        self.assertIn('DIAGNOSTICS', block)
        self.assertIn('line 3', block)
        self.assertIn('invalid syntax', block)

    def test_check_and_render(self):
        path = os.path.join(self.tmp, 'meh.py')
        with open(path, 'w') as f:
            f.write("x = (\n")
        self.assertNotEqual(lsp.check_and_render(path), '')

    def test_tier2_command_wins(self):
        path = os.path.join(self.tmp, 'fine.py')
        with open(path, 'w') as f:
            f.write("x = 1\n")
        with patch.dict(os.environ, {'JARVIS_LSP_CMD': 'false {file}'}):
            res = lsp.check_file(path)
        self.assertFalse(res['ok'])
        self.assertEqual(res['checker'], 'false')

    def test_kill_switch(self):
        with patch.dict(os.environ, {'JARVIS_LSP_DIAGNOSTICS': '0'}):
            res = lsp.check_code("def broken(:")
            self.assertTrue(res['ok'])
            self.assertEqual(res['checker'], 'disabled')

    def test_js_without_node_is_not_reported_clean(self):
        # No node binary => the file was never checked; an all-clear would
        # let the agent confirm a broken JS file "clean" (module's point).
        path = os.path.join(self.tmp, 'app.js')
        with open(path, 'w') as f:
            f.write("function broken( { return 1; }")
        with patch('utils.lsp_diagnostics.shutil.which',
                   return_value=None):
            res = lsp.check_file(path)
        self.assertTrue(res['ok'])                       # edit not claimed broken
        self.assertEqual(res['checker'], 'none')
        self.assertEqual(res['issues'][0]['severity'], 'warning')
        self.assertIn('was NOT validated', res['issues'][0]['message'])
        rendered = lsp.render(res)
        self.assertTrue(rendered.startswith('NOTICE'))
        self.assertIn('was NOT validated', rendered)

    def test_ts_without_tsc_is_not_reported_clean(self):
        path = os.path.join(self.tmp, 'app.ts')
        with open(path, 'w') as f:
            f.write("const x: number = 'nope';")
        with patch('utils.lsp_diagnostics.shutil.which',
                   return_value=None):
            res = lsp.check_file(path)
        self.assertTrue(res['ok'])
        self.assertEqual(res['checker'], 'none')
        self.assertIn('was NOT validated', res['issues'][0]['message'])

    def test_internal_checker_failure_is_not_clean(self):
        # A checker that blows up (rather than a real syntax error) must
        # still never render as '' — the file simply could not be checked.
        path = os.path.join(self.tmp, 'ok.json')
        with open(path, 'w') as f:
            f.write('{"a": 1}')
        with patch('utils.lsp_diagnostics.json.load',
                   side_effect=RuntimeError('boom')):
            res = lsp.check_file(path)
        self.assertTrue(res['ok'])
        self.assertEqual(res['checker'], 'error')
        self.assertIn('could not be validated', res['issues'][0]['message'])
        self.assertNotEqual(lsp.render(res), '')


# ====================================================================== #
# Computer use — tier selection + keycodes (no actual clicking)
# ====================================================================== #

class TestComputerUse(unittest.TestCase):

    def setUp(self):
        self.driver = computer_use.ComputerUse()

    def test_mac_keycode_mapping(self):
        self.assertEqual(computer_use._mac_keycode('enter'), 36)
        self.assertEqual(computer_use._mac_keycode('CMD'), 55)
        self.assertEqual(computer_use._mac_keycode('99'), 99)    # numeric code
        self.assertEqual(computer_use._mac_keycode('F10'), 36)   # unknown → enter
        self.assertEqual(computer_use._mac_keycode(None), 36)    # garbage-safe

    def test_tier_selection(self):
        tier = self.driver._tier()
        if platform.system() == 'Darwin':
            self.assertEqual(tier, 'ax')
        else:
            self.assertIn(tier, ('quartz', 'pyautogui', None))

    def test_disabled_message(self):
        with patch.dict(os.environ, {'JARVIS_COMPUTER_USE': '0'}):
            out = self.driver.click_element('Finder', 'Downloads')
        self.assertIn('disabled', out.lower())

    def test_as_quote_escapes_apple_script(self):
        # Quotes and backslashes in any interpolated value must not
        # break out of the AppleScript string literal.
        self.assertEqual(computer_use._as_quote('a"b'), 'a\\"b')
        self.assertEqual(computer_use._as_quote('a\\b'), 'a\\\\b')
        self.assertEqual(computer_use._as_quote('x'), 'x')

    def test_apple_script_values_are_escaped(self):
        # Hostile values (app names, UI elements, field values) are
        # escaped before interpolation into osascript — an injection
        # payload can never survive to the desktop-controlling script.
        scripts = []

        def fake_osascript(script, timeout=None):
            scripts.append(script)
            return True, 'OK'

        with patch.object(computer_use, '_SYSTEM', 'Darwin'), \
                patch.object(computer_use, '_osascript',
                             side_effect=fake_osascript):
            self.driver.click_element(
                'Finder", tell app "Finder',
                'Downloads", return "PWNED',
                role='button", return "PWNED')
            self.driver.set_field('Mail', 'Subject',
                                  'hi", return "PWNED')
            self.driver.activate('Safari", return "PWNED')
        self.assertEqual(len(scripts), 3)
        for s in scripts:
            self.assertIn('\\"', s)               # every quote escaped
            self.assertNotIn('tell app "Finder', s)
            self.assertNotIn('return "PWNED', s)
        self.assertIn('PWNED', '\n'.join(scripts))  # literal text survives

    def test_junk_coordinates_never_reach_osascript(self):
        # Non-numeric coordinates/amounts on the macOS path used to blow
        # up inside int() and raise ValueError out of the public API (the
        # other tiers already degraded to a message).  They must return a
        # message and never fire a live desktop-controlling script.
        def boom(script, timeout=None):          # pragma: no cover
            raise AssertionError("osascript must not run for junk input")
        with patch.object(computer_use, '_SYSTEM', 'Darwin'), \
                patch.object(computer_use, '_osascript',
                             side_effect=boom):
            out = self.driver.click('abc', '5')
            self.assertIn('must be integers', out)
            out = self.driver.click(None, 5)
            self.assertIn('must be integers', out)
            out = self.driver.scroll('12x')
            self.assertIn('must be an integer', out)
            out = self.driver.scroll(None)
            self.assertIn('must be an integer', out)


# ====================================================================== #
# Gateways — bot mode, Slack/Discord adapters, hub
# ====================================================================== #

class TestGateways(unittest.TestCase):

    def test_pick_persona(self):
        self.assertEqual(gateways.pick_persona("can you code me a script"),
                         'coder')
        self.assertEqual(gateways.pick_persona("please summarize this"),
                         'synthesizer')
        self.assertIsNone(gateways.pick_persona("hello there friend"))

    def test_strip_mention(self):
        self.assertEqual(gateways.strip_mention("@jarvis, do the thing"),
                         'do the thing')
        self.assertEqual(gateways.strip_mention("Jarvis: status?"),
                         'status?')

    def test_route_message_specialist(self):
        brain = FakeBrain({'coder': 'Script drafted.'})
        used, answer = gateways.route_message(
            "@jarvis code me a cleanup script", brain)
        self.assertTrue(used)
        self.assertEqual(answer, 'Script drafted.')
        self.assertEqual(brain.calls, ['coder'])

    def test_route_message_fallback_without_persona(self):
        brain = FakeBrain({'coder': 'x'})
        used, answer = gateways.route_message(
            "just chatting", brain, fallback=lambda t: f"fb:{t}")
        self.assertFalse(used)
        self.assertEqual(answer, 'fb:just chatting')

    def test_route_message_bot_mode_off(self):
        brain = FakeBrain({'coder': 'x'})
        with patch.dict(os.environ, {'JARVIS_BOT_MODE': '0'}):
            used, answer = gateways.route_message(
                "code me a thing", brain, fallback=lambda t: 'fb')
        self.assertFalse(used)
        self.assertEqual(answer, 'fb')

    def test_hub_handle_message_specialist_replies(self):
        brain = FakeBrain({'coder': 'Done in background.'})
        hub = gateways.GatewayHub(brain=brain)
        replies = []
        hub.handle_message('discord', '@jarvis code me a thing',
                           reply=replies.append, chat_id='c1')
        self.assertEqual(replies, ['Done in background.'])

    def test_hub_no_specialist_falls_back_to_event_bus(self):
        brain = FakeBrain({'coder': 'x'})
        hub = gateways.GatewayHub(brain=brain)
        replies = []
        with patch('utils.event_bus.get_bus') as fake_bus:
            fake_bus.return_value.publish.return_value = 'bus result'
            hub.handle_message('discord', 'plain chatter',
                               reply=replies.append)
            fake_bus.return_value.publish.assert_called_once()
        # No specialist claimed it; the hub's reply callback is the
        # pipeline's to use — nothing was sent through it directly.
        self.assertEqual(replies, [])

    def test_slack_url_verification(self):
        hub = gateways.GatewayHub()
        slack = gateways.SlackAdapter(hub, webhook_url='',
                                      verify_token='tok')
        status, body = slack.handle_http(
            {'type': 'url_verification', 'challenge': 'abc123'})
        self.assertEqual(status, 200)
        self.assertEqual(body, 'abc123')

    def test_slack_bad_token_rejected(self):
        hub = gateways.GatewayHub()
        slack = gateways.SlackAdapter(hub, webhook_url='',
                                      verify_token='tok')
        status, _ = slack.handle_http({'token': 'wrong',
                                       'event': {'text': 'hi'}})
        self.assertEqual(status, 401)

    def test_slack_missing_token_rejected(self):
        # With a verification token configured, a request that omits the
        # token entirely must NOT be trusted — previously it slipped past
        # (None was allowed alongside the valid token).
        hub = gateways.GatewayHub()
        slack = gateways.SlackAdapter(hub, webhook_url='',
                                      verify_token='tok')
        status, _ = slack.handle_http(
            {'event': {'text': '@jarvis wipe everything'}})
        self.assertEqual(status, 401)

    def test_slack_event_routed_to_hub(self):
        seen = []

        class FakeHub:
            def handle_message(self, source, text, reply=None,
                               chat_id=None, user=None):
                seen.append((source, text, user))

        slack = gateways.SlackAdapter(FakeHub(), webhook_url='',
                                      verify_token='')
        status, _ = slack.handle_http(
            {'event': {'text': '@jarvis status?', 'user': 'U42'}})
        self.assertEqual(status, 200)
        self.assertEqual(seen, [('slack', 'status?', 'U42')])

    def test_slack_bad_payload(self):
        slack = gateways.SlackAdapter(gateways.GatewayHub())
        status, _ = slack.handle_http("not a dict")
        self.assertEqual(status, 400)

    def test_discord_send_chunks_long_text(self):
        adapter = gateways.DiscordAdapter(gateways.GatewayHub(),
                                          token='t', channel_id='c')
        calls = []
        with patch.object(adapter, '_api',
                          side_effect=lambda m, p, payload=None:
                          calls.append(payload)):
            adapter.send("z" * 4500)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(len(c['content']) <= 2000 for c in calls))

    def test_hub_status_shape(self):
        status = gateways.GatewayHub().status()
        for key in ('gateways_enabled', 'bot_mode', 'discord', 'slack',
                    'telegram', 'webhook'):
            self.assertIn(key, status)

    def test_gateways_disabled_start_noop(self):
        hub = gateways.GatewayHub()
        with patch.dict(os.environ, {'JARVIS_GATEWAYS': '0',
                                     'JARVIS_DISCORD_TOKEN': 'x',
                                     'JARVIS_DISCORD_CHANNEL': 'y'}):
            hub.start()
            self.assertIsNone(hub.discord)
            self.assertFalse(hub.status()['gateways_enabled'])

    def test_poll_env_parses_safely(self):
        # A malformed JARVIS_GATEWAY_POLL must not crash the module at
        # import (it is parsed at module scope), and a 0/negative value
        # must not turn the Discord poll loop into a busy-spin.
        with patch.dict(os.environ, {'JARVIS_GATEWAY_POLL': 'abc'}):
            self.assertEqual(gateways._env_float('JARVIS_GATEWAY_POLL',
                                                 '3'), 3.0)
        with patch.dict(os.environ, {'JARVIS_GATEWAY_POLL': '0'}):
            self.assertEqual(gateways._env_float('JARVIS_GATEWAY_POLL',
                                                 '3', floor=1.0), 1.0)


# ====================================================================== #
# MCP server — JSON-RPC over stdio
# ====================================================================== #

class TestMCPServer(unittest.TestCase):

    @staticmethod
    def _serve(lines, env=None):
        stdin = io.StringIO('\n'.join(lines) + '\n')
        stdout = io.StringIO()
        with patch.dict(os.environ, env or {}):
            mcp_server.serve(stdin=stdin, stdout=stdout)
        return [json.loads(l) for l in stdout.getvalue().splitlines()
                if l.strip()]

    @staticmethod
    def _req(method, _id=None, **params):
        msg = {'jsonrpc': '2.0', 'method': method}
        if _id is not None:
            msg['id'] = _id
        if params:
            msg['params'] = params
        return json.dumps(msg)

    def test_initialize_handshake(self):
        out = self._serve([self._req('initialize', _id=1)])
        result = out[0]['result']
        self.assertEqual(result['protocolVersion'], '2024-11-05')
        self.assertEqual(result['serverInfo']['name'], 'jarvis')

    def test_notifications_get_no_response(self):
        out = self._serve([self._req('notifications/initialized'),
                           self._req('ping', _id=2)])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['id'], 2)
        self.assertEqual(out[0]['result'], {})

    def test_tools_list_catalog(self):
        out = self._serve([self._req('tools/list', _id=1)])
        names = {t['name'] for t in out[0]['result']['tools']}
        self.assertEqual(names, {'jarvis_recall', 'jarvis_search_memory',
                                 'jarvis_list_skills', 'jarvis_goals',
                                 'jarvis_tasks', 'jarvis_ask'})

    def test_unknown_method_and_parse_error(self):
        out = self._serve([self._req('bogus/method', _id=1),
                           'this is not json'])
        self.assertEqual(out[0]['error']['code'], -32601)
        self.assertEqual(out[1]['error']['code'], -32700)

    def test_non_object_request_does_not_crash_loop(self):
        # Valid JSON that isn't an object (array / string / null) is an
        # Invalid Request (-32600) per JSON-RPC — and the stdio loop
        # must survive it to serve the next request, not raise.
        out = self._serve(['[1, 2, 3]',
                           '"hello"',
                           'null',
                           self._req('ping', _id=9)])
        bad = [o['error']['code'] for o in out
               if o.get('id') is None]
        self.assertEqual(bad, [-32600, -32600, -32600])
        self.assertEqual(out[-1]['id'], 9)
        self.assertEqual(out[-1]['result'], {})

    def test_unknown_tool_is_error_result(self):
        out = self._serve([self._req('tools/call', _id=1,
                                     name='nope', arguments={})])
        self.assertTrue(out[0]['result'].get('isError'))

    def test_tools_call_rejects_non_object_arguments(self):
        # A malformed client sending a list/string for `arguments` gets
        # a clean -32602 invalid-params error, not a generic crash.
        out = self._serve([self._req('tools/call', _id=1,
                                     name='jarvis_list_skills',
                                     arguments=['not', 'an', 'object'])])
        self.assertEqual(out[0]['error']['code'], -32602)

    def test_jarvis_ask_disabled_by_default(self):
        out = self._serve(
            [self._req('tools/call', _id=1, name='jarvis_ask',
                       arguments={'text': 'hi'})],
            env={'JARVIS_MCP_EXPOSE_ASK': '0'})
        text = out[0]['result']['content'][0]['text']
        self.assertIn('disabled', text)

    def test_jarvis_recall_uses_rlm(self):
        with patch('utils.rlm.get_rlm') as fake:
            fake.return_value.recall.return_value = \
                'RECALLED: the user builds Atlas.'
            out = self._serve([self._req('tools/call', _id=1,
                                         name='jarvis_recall',
                                         arguments={'query': 'atlas'})])
        text = out[0]['result']['content'][0]['text']
        self.assertIn('Atlas', text)
        fake.return_value.recall.assert_called_once()

    def test_list_skills_returns_catalog(self):
        out = self._serve([self._req('tools/call', _id=1,
                                     name='jarvis_list_skills',
                                     arguments={})])
        self.assertIn('content', out[0]['result'])

    def test_mcp_client_timeouts_parse_safely(self):
        # The MCP client parses timeouts at module scope — a malformed
        # env var must fall back to the default (not crash the import),
        # and a 0/negative timeout must not produce an instant or
        # negative subprocess timeout.
        from utils.mcp_client import _env_float, _env_int
        with patch.dict(os.environ, {'JARVIS_MCP_CALL_TIMEOUT': 'oops'}):
            self.assertEqual(_env_float('JARVIS_MCP_CALL_TIMEOUT',
                                        '30'), 30.0)
        with patch.dict(os.environ, {'JARVIS_MCP_CALL_TIMEOUT': '0'}):
            self.assertEqual(_env_float('JARVIS_MCP_CALL_TIMEOUT',
                                        '30', floor=1.0), 1.0)
        with patch.dict(os.environ, {'JARVIS_MCP_MAX_SERVERS': '0'}):
            self.assertEqual(_env_int('JARVIS_MCP_MAX_SERVERS', '8',
                                      floor=1), 1)

    def test_kill_switch(self):
        stdin = io.StringIO(self._req('ping', _id=1) + '\n')
        stdout = io.StringIO()
        with patch.dict(os.environ, {'JARVIS_MCP_SERVER': '0'}):
            rc = mcp_server.serve(stdin=stdin, stdout=stdout)
        self.assertEqual(rc, 0)
        self.assertEqual(stdout.getvalue(), '')


# ====================================================================== #
# Mixture of Agents
# ====================================================================== #

class _PanelProvider:
    def list_models(self):
        return ['some-model']


class _PanelRouter:
    def __init__(self, names):
        self.providers = {n: _PanelProvider() for n in names}
        self.calls = []

    def chat(self, messages, *, require=None, models=None, tools=None,
             max_tokens=None, temperature=0.2, timeout=45, stream=False,
             purpose='test'):
        self.calls.append(list(models or []))
        return _text_result(
            f"answer from {(models or ['?'])[0]}")


class _MoABrain:
    def __init__(self, router, reply='MERGED ANSWER'):
        self.router = router
        self.reply = reply
        self.agents = []

    def complete(self, prompt, system=None, timeout=60, max_tokens=None,
                 temperature=None, agent=None):
        self.agents.append(agent)
        return self.reply


class _FlakyRouter(_PanelRouter):
    """A panel where one provider's chat() always fails."""

    def __init__(self, names, fail):
        super().__init__(names)
        self.fail = fail

    def chat(self, messages, **kwargs):
        models = kwargs.get('models') or []
        if models and models[0] == self.fail:
            raise RuntimeError('provider down')
        return super().chat(messages, **kwargs)


class TestMoA(unittest.TestCase):

    def test_two_providers_synthesized(self):
        router = _PanelRouter(['gemini', 'groq'])
        brain = _MoABrain(router)
        res = moa.ask_moa(brain, "design a rate limiter")
        self.assertEqual(res['answer'], 'MERGED ANSWER')
        self.assertEqual(res['models'], ['gemini', 'groq'])
        self.assertEqual(router.calls, [['gemini'], ['groq']])
        self.assertEqual(brain.agents, ['synthesizer'])

    def test_each_provider_targeted_individually(self):
        router = _PanelRouter(['gemini', 'groq', 'deepseek'])
        moa.ask_moa(_MoABrain(router), "q", n=3)
        self.assertEqual(router.calls,
                         [['gemini'], ['groq'], ['deepseek']])

    def test_single_provider_returns_none(self):
        # A one-model "panel" is just a normal answer — don't pretend.
        router = _PanelRouter(['gemini'])
        brain = _MoABrain(router)
        self.assertIsNone(moa.ask_moa(brain, "q"))
        self.assertEqual(router.calls, [])
        self.assertEqual(brain.agents, [])

    def test_one_provider_failing_degrades_to_single_proposal(self):
        router = _FlakyRouter(['gemini', 'groq'], fail='groq')
        brain = _MoABrain(router)
        res = moa.ask_moa(brain, "q")
        self.assertEqual(res['answer'], 'answer from gemini')
        self.assertEqual(res['models'], ['gemini'])
        self.assertEqual(brain.agents, [])   # no synthesis call

    def test_no_providers_returns_none(self):
        self.assertIsNone(moa.ask_moa(_MoABrain(_PanelRouter([])), "q"))

    def test_disabled_returns_none(self):
        router = _PanelRouter(['a', 'b'])
        with patch.dict(os.environ, {'JARVIS_MOA': '0'}):
            self.assertIsNone(moa.ask_moa(_MoABrain(router), "q"))

    def test_format_header(self):
        router = _PanelRouter(['gemini', 'groq'])
        out = moa.format(_MoABrain(router), "q")
        self.assertIn('[mixture of 2 models: gemini, groq]', out)
        self.assertIn('MERGED ANSWER', out)

    def test_synthesis_failure_degrades_to_first_proposal(self):
        router = _PanelRouter(['gemini', 'groq'])
        brain = _MoABrain(router, reply=None)    # synthesis returns None
        res = moa.ask_moa(brain, "q")
        self.assertEqual(res['answer'], 'answer from gemini')

    def test_panel_filters_modeless_but_keeps_probe_failures(self):
        # A provider that reports no models is filtered out of the panel;
        # one whose probe raises is still offered a chance (the later
        # chat attempt fails and ask_moa catches it) so an offline local
        # endpoint can't silently shrink the panel.
        class _NoModels:
            def list_models(self):
                return []

        class _ProbeDown:
            def list_models(self):
                raise OSError('probe failed')

        router = _PanelRouter(['gemini', 'groq', 'deepseek'])
        router.providers['deepseek'] = _NoModels()
        router.providers['ollama'] = _ProbeDown()

        panel = moa._panel_providers(router, 10)
        self.assertEqual(panel, ['gemini', 'groq', 'ollama'])

    def test_panel_walks_router_preference_order(self):
        # The panel honours the router's own ordering (env override /
        # local-first) instead of raw registry insertion order, while
        # still capping at *n* and filtering model-less providers.
        class _OrderedRouter(_PanelRouter):
            def __init__(self, names, order):
                super().__init__(names)
                self.order = order

            def _ordered_providers(self):
                return list(self.order)

        class _Empty:
            def list_models(self):
                return []

        router = _OrderedRouter(['anthropic', 'groq', 'gemini'],
                                ['groq', 'gemini', 'anthropic'])
        router.providers['deepseek'] = _Empty()
        self.assertEqual(moa._panel_providers(router, 2),
                         ['groq', 'gemini'])
        self.assertEqual(moa._panel_providers(router, 10),
                         ['groq', 'gemini', 'anthropic'])

    def test_panel_never_seats_same_provider_twice(self):
        # Distinctness is by provider object: even if a registry aliases
        # one vendor under a second key, the panel seats it only once.
        router = _PanelRouter(['gemini', 'groq', 'deepseek'])
        router.providers['gemini2'] = router.providers['gemini']
        self.assertEqual(moa._panel_providers(router, 10),
                         ['gemini', 'groq', 'deepseek'])
        self.assertNotIn('gemini2',
                         moa._panel_providers(router, 10))


# ====================================================================== #
# Wake word
# ====================================================================== #

class TestWakeWord(unittest.TestCase):

    def test_matches_default_phrase(self):
        self.assertTrue(wake_word.matches("Hey Jarvis turn on the lights"))
        self.assertFalse(wake_word.matches("what's the weather"))

    def test_custom_phrase(self):
        with patch.dict(os.environ, {'JARVIS_WAKE_WORD': 'computer'}):
            self.assertTrue(wake_word.matches("hey COMPUTER status"))
            self.assertEqual(wake_word.phrase(), 'computer')

    def test_strip_wake_word(self):
        self.assertEqual(
            wake_word.strip_wake_word("hey jarvis turn on the lights"),
            'turn on the lights')
        self.assertEqual(wake_word.strip_wake_word("jarvis what time"),
                         'what time')
        self.assertEqual(wake_word.strip_wake_word("no phrase here"),
                         'no phrase here')

    def test_wake_gating_is_opt_in(self):
        self.assertFalse(wake_word.wake_enabled())
        with patch.dict(os.environ, {'JARVIS_WAKE_WORD_ENABLE': '1'}):
            self.assertTrue(wake_word.wake_enabled())


# ====================================================================== #
# Hyperframe
# ====================================================================== #

_STORYBOARD_JSON = json.dumps({
    'title': 'Onboarding Flow',
    'frames': [
        {'heading': 'Welcome', 'bullets': ['greet', 'pitch'],
         'caption': 'first impression'},
        {'heading': 'Plan Picker', 'bullets': ['choose goals'],
         'caption': ''},
    ]})


class TestHyperframe(unittest.TestCase):

    def setUp(self):
        self.tmp = _make_tmp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_storyboard_parsed(self):
        sb = hyperframe.storyboard_from_instructions(
            FakeBrain({'coder': _STORYBOARD_JSON}),
            "onboarding flow for a fitness app")
        self.assertEqual(sb['title'], 'Onboarding Flow')
        self.assertEqual(len(sb['frames']), 2)

    def test_storyboard_fenced_json(self):
        sb = hyperframe.storyboard_from_instructions(
            FakeBrain({'coder': f'```json\n{_STORYBOARD_JSON}\n```'}),
            "x")
        self.assertEqual(len(sb['frames']), 2)

    def test_storyboard_bad_or_thin_input(self):
        brain = FakeBrain({'coder': 'no json here'})
        self.assertIsNone(
            hyperframe.storyboard_from_instructions(brain, "x"))
        thin = json.dumps({'title': 'T', 'frames': [
            {'heading': 'Only One'}]})
        self.assertIsNone(hyperframe.storyboard_from_instructions(
            FakeBrain({'coder': thin}), "x"))
        self.assertIsNone(
            hyperframe.storyboard_from_instructions(None, "x"))

    def test_render_html_escapes_and_frames(self):
        sb = {'title': 'Demo', 'frames': [
            {'heading': 'Frame A', 'bullets': ['<script>bad()</script>'],
             'caption': ''},
            {'heading': 'Frame B', 'bullets': ['safe'], 'caption': 'c'}]}
        html = hyperframe.render_html(sb)
        self.assertIn('FRAME 1 / 2', html)
        self.assertIn('Frame A', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertNotIn('<script>bad', html)

    def test_create_writes_artifacts(self):
        brain = FakeBrain({'coder': _STORYBOARD_JSON})
        with patch.object(hyperframe, '_OUTPUT_DIR',
                          os.path.join(self.tmp, 'hf')):
            msg = hyperframe.create(brain, "onboarding flow")
            self.assertIn('Hyperframe mockup ready', msg)
            self.assertIn('2 frames', msg)
            out_dirs = os.listdir(os.path.join(self.tmp, 'hf'))
        self.assertEqual(len(out_dirs), 1)
        made = os.path.join(self.tmp, 'hf', out_dirs[0])
        self.assertTrue(os.path.exists(
            os.path.join(made, 'mockup.html')))
        self.assertTrue(os.path.exists(
            os.path.join(made, 'storyboard.json')))

    def test_two_creates_same_second_do_not_collide(self):
        # Two mockups created in the same wall-clock second must land in
        # distinct directories (microsecond stamp) — otherwise the second
        # write silently overwrites the first.
        brain = FakeBrain({'coder': _STORYBOARD_JSON})
        with patch.object(hyperframe, '_OUTPUT_DIR',
                          os.path.join(self.tmp, 'hf2')):
            with patch('utils.hyperframe.datetime') as fake_dt:
                # Two creates land in the same wall-clock second but with
                # distinct microseconds — the stamp must keep them apart.
                fake_dt.datetime.now.side_effect = [
                    datetime.datetime(2026, 1, 1, 12, 0, 0, 123456),
                    datetime.datetime(2026, 1, 1, 12, 0, 0, 654321),
                ]
                hyperframe.create(brain, "onboarding flow")
                hyperframe.create(brain, "onboarding flow")
            out_dirs = os.listdir(os.path.join(self.tmp, 'hf2'))
        self.assertEqual(len(out_dirs), 2)

    def test_create_disabled(self):
        with patch.dict(os.environ, {'JARVIS_HYPERFRAME': '0'}):
            msg = hyperframe.create(FakeBrain(), "anything")
        self.assertIn('disabled', msg.lower())

    def test_list_mockups(self):
        with patch.object(hyperframe, '_OUTPUT_DIR',
                          os.path.join(self.tmp, 'hf')):
            os.makedirs(os.path.join(self.tmp, 'hf', '20260101_demo'))
            listing = hyperframe.list_mockups()
        self.assertIn('20260101_demo', listing)


# ====================================================================== #
# TUI — slash commands + status feed
# ====================================================================== #

class _TuiBrain:
    def __init__(self):
        self.router = type('R', (), {'providers': {'groq': 1}})()
        self.cleared = False

    def clear_history(self):
        self.cleared = True


class _TuiExecutor:
    pass


class TestTUI(unittest.TestCase):

    def test_slash_command_catalog(self):
        commands = tui._slash_commands()
        for expected in ('/skills', '/read_skill', '/checkpoint',
                         '/rollback', '/runtimes', '/rlm', '/quit'):
            self.assertIn(expected, commands)

    def _run(self, line, brain=None, executor=None):
        ui = FakeUI()
        brain = brain or _TuiBrain()
        executor = executor or _TuiExecutor()
        handled = tui._run_slash(line, brain, executor, ui)
        return handled, ui

    def test_help(self):
        handled, ui = self._run('/help')
        self.assertTrue(handled)
        self.assertIn('Slash commands', ui.said[0])

    def test_providers(self):
        handled, ui = self._run('/providers')
        self.assertTrue(handled)
        self.assertIn('groq', ui.said[0])

    def test_runtimes(self):
        handled, ui = self._run('/runtimes')
        self.assertTrue(handled)
        self.assertIn('runtimes:', ui.said[0])

    def test_rlm_uses_singleton(self):
        with patch('utils.rlm.get_rlm') as fake:
            fake.return_value.stats.return_value = {
                'notes': 7, 'entities': 3}
            handled, ui = self._run('/rlm')
        self.assertTrue(handled)
        self.assertIn('entities', ui.said[0])

    def test_checkpoint_uses_manager(self):
        with patch('utils.checkpoints.get_checkpoints') as fake:
            fake.return_value.create.return_value = \
                'Checkpoint saved: tui-manual.'
            handled, ui = self._run('/checkpoint')
        self.assertTrue(handled)
        self.assertIn('Checkpoint saved', ui.said[0])
        fake.return_value.create.assert_called_once()

    def test_rollback_lists_without_arg(self):
        with patch('utils.checkpoints.get_checkpoints') as fake:
            fake.return_value.list.return_value = 'No checkpoints yet.'
            handled, ui = self._run('/rollback')
        self.assertTrue(handled)
        self.assertIn('No checkpoints', ui.said[0])

    def test_read_skill_requires_arg(self):
        handled, ui = self._run('/read_skill')
        self.assertTrue(handled)
        self.assertIn('Usage', ui.said[0])

    def test_unknown_command_not_handled(self):
        handled, _ui = self._run('/definitely-not-a-command')
        self.assertFalse(handled)

    def test_quit_raises_system_exit(self):
        with self.assertRaises(SystemExit):
            self._run('/quit')

    def test_extract_reply_prefers_command_response(self):
        cmd = {'response': 'From the command.'}
        self.assertEqual(tui._extract_reply(cmd, 'fallback'),
                         'From the command.')
        self.assertEqual(tui._extract_reply({}, 'fallback result'),
                         'fallback result')

    def test_status_feed_prints_once(self):
        ui = tui.TerminalUI()
        buf = io.StringIO()
        with redirect_stdout(buf):
            ui.callback('status', {'message': 'running tool'})
            ui.callback('status', {'message': 'running tool'})
            ui.callback('ai_text', {'text': 'suppressed'})
        out = buf.getvalue()
        self.assertEqual(out.count('running tool'), 1)
        self.assertNotIn('suppressed', out)


# ====================================================================== #
# GUI automator — shell-safe app-name sanitizing
# ====================================================================== #

class TestGUIAutomatorSanitize(unittest.TestCase):

    def setUp(self):
        try:
            from utils.gui_automator import _safe_app_name
        except Exception as e:
            self.skipTest(f"gui_automator unavailable: {e}")
        self._safe_app_name = _safe_app_name

    def _safe(self, name):
        return self._safe_app_name(name)

    def test_plain_launcher_names_survive(self):
        self.assertEqual(self._safe('Google Chrome'), 'Google Chrome')
        self.assertEqual(self._safe('Visual Studio Code'),
                         'Visual Studio Code')
        self.assertEqual(self._safe('Firefox.app'), 'Firefox.app')

    def test_shell_metacharacters_are_stripped(self):
        meta = set(';&|$`()<>^')
        for hostile in ('; rm -rf ~', '$(touch /tmp/jarvis_pwn)',
                        'foo & bar', 'a`id`b', 'rm | sh', 'x<y>z',
                        'echo ^calc'):
            out = self._safe(hostile)
            self.assertFalse(set(out) & meta,
                             f"metachar survived in {hostile!r} → {out!r}")
            self.assertTrue(out.isascii())

    def test_empty_result_is_rejected(self):
        # The Linux/Windows branches refuse names that sanitize to empty
        # rather than silently running an empty command.
        self.assertEqual(self._safe('!!!'), '')


class TestRecurringParse(unittest.TestCase):
    """Time/date parsing of natural-language automations."""

    def setUp(self):
        self.tmp = _make_tmp()
        self.ra = recurring.RecurringAutomations(
            file_path=os.path.join(self.tmp, 'jobs.json'),
            poll_interval=999)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_single_digit_minute_is_accepted(self):
        # "9:5pm" used to slip past the two-digit-minute regex and
        # silently schedule 9:00 AM.  It must read as 9:05 PM.
        job, err = self.ra.parse("every day at 9:5pm do the thing")
        self.assertIsNone(err)
        self.assertEqual((job['hour'], job['minute']), (21, 5))
        self.assertEqual(job['action'], 'do the thing')

    def test_bare_colon_after_hour_is_rejected(self):
        # "every day at 9: do yoga" has a colon but no minutes — reject
        # it rather than schedule 9:00 AM and run ": do yoga".
        _, err = self.ra.parse("every day at 9: do yoga")
        self.assertIsNotNone(err)
        self.assertIn('minute', err.lower())

    def test_separator_colon_after_complete_time_is_clean(self):
        # A colon separating time from instruction ("9am: stand up") is a
        # separator, not an incomplete time — the action is cleaned of it.
        job, err = self.ra.parse("every day at 9am: stand up")
        self.assertIsNone(err)
        self.assertEqual(job['action'], 'stand up')
        self.assertEqual((job['hour'], job['minute']), (9, 0))

    def test_two_digit_minute_and_pm_still_work(self):
        job, err = self.ra.parse("every weekday at 18:30 check the calendar")
        self.assertIsNone(err)
        self.assertEqual((job['hour'], job['minute']), (18, 30))
        self.assertEqual(job['schedule'], 'weekdays')


class _FakeTasks:
    def __init__(self):
        self.items = []

    def add(self, text):
        self.items.append(text)


class TestMeetingTaskExtraction(unittest.TestCase):
    """MeetingTranscriber._extract_and_add_tasks must only turn items
    under an action/follow-up section into todos — not every numbered
    line of the model's reply (summary + decisions included)."""

    def _run(self, analysis):
        tasks = _FakeTasks()
        stub = type('_Stub', (), {'tasks': tasks})()
        meeting_audio.MeetingTranscriber._extract_and_add_tasks(stub,
                                                                analysis)
        return tasks.items

    def test_only_action_section_items_become_tasks(self):
        analysis = (
            "1. A brief summary (2-3 sentences)\n"
            "The team agreed to ship the refactor next sprint.\n"
            "2. Key decisions made\n"
            "- We will cut the flaky integration test suite.\n"
            "3. Action items (as a numbered list)\n"
            "1. Book the Q4 retro room\n"
            "2. Ping legal about the vendor renewal\n"
            "4. Follow-ups needed\n"
            "- Draft the incident summary for Friday\n")
        got = self._run(analysis)
        # Only the real to-dos land in the list.
        self.assertEqual(got, [
            "[Meeting] Book the Q4 retro room",
            "[Meeting] Ping legal about the vendor renewal",
            "[Meeting] Draft the incident summary for Friday",
        ])
        # The model's own outline headers must never be added as tasks.
        joined = "\n".join(got).lower()
        self.assertNotIn('brief summary', joined)
        self.assertNotIn('key decisions', joined)
        self.assertNotIn('action items', joined)

    def test_numbered_items_beneath_action_header_captured(self):
        got = self._run(
            "Action items:\n"
            "- Send the NDA for signature\n"
            "- Circulate the agenda by Tuesday\n")
        self.assertEqual(len(got), 2)
        self.assertIn('Send the NDA', got[0])

    def test_no_action_section_yields_no_tasks(self):
        # A reply that never opens an action/follow-up section must not
        # spawn todos from numbered prose (the old catch-all regex did).
        got = self._run(
            "1. The team reviewed the roadmap\n"
            "2. Ship date is next Tuesday\n"
            "3. Everyone should read the design doc\n")
        self.assertEqual(got, [])

    def test_action_mention_outside_list_is_ignored(self):
        # The phrase 'action items' appearing in prose (not as a header)
        # used to arm the old regex and swallow every numbered line.
        got = self._run(
            "There are no open action items from last week.\n"
            "1. The demo went well\n"
            "2. No blockers remain\n")
        self.assertEqual(got, [])


# ====================================================================== #
# Brain — R3 local-arithmetic exponent guard (bigint / nested DoS)
# ====================================================================== #

class TestBrainMathExponentGuard(unittest.TestCase):
    """Regression: a bare numeric prompt like '9**99999999' used to reach
    eval() and materialise a multi-gigabyte bigint (freeze/OOM).  Nested
    '9**9**9' (= 9^387M) too.  Both must fall back to None (the LLM path)
    instead of being evaluated."""

    def test_small_exponent_still_computes(self):
        ans = brain.Brain._math_answer('2**10')
        self.assertIsNotNone(ans)
        self.assertIn('1024', ans)

    def test_negative_small_exponent_still_computes(self):
        ans = brain.Brain._math_answer('2**-3')
        self.assertIsNotNone(ans)
        self.assertIn('0.125', ans)

    def test_huge_literal_exponent_refused(self):
        self.assertIsNone(brain.Brain._math_answer('9**99999999'))

    def test_nested_power_refused(self):
        # right-associative: exponent of the outer ** is itself a Pow
        self.assertIsNone(brain.Brain._math_answer('9**9**9'))

    def test_oversized_exponent_just_over_limit_refused(self):
        self.assertIsNone(brain.Brain._math_answer('2**1000001'))


# ====================================================================== #
# Brain — rolling-digest lost update under concurrent eviction
# ====================================================================== #

class TestBrainDigestNoLostUpdate(unittest.TestCase):

    def _blank(self):
        b = object.__new__(brain.Brain)     # never __init__ (live clients)
        b._digest_lock = threading.RLock()
        b.digest_text = 'old digest'
        b.digest_upto = 5
        return b

    def test_concurrent_evictions_survive_compression(self):
        b = self._blank()
        batch = [(f'u{i}', f'a{i}') for i in range(brain.Brain.COMPRESS_EVERY)]
        b._evicted_buffer = list(batch)
        late = ('u-extra', 'a-extra')       # evicted while digest in flight
        saved = []
        b.complete = lambda prompt, agent=None, timeout=0, max_tokens=0: (
            b._evicted_buffer.append(late) or 'digest-v2')
        b._save_digest = lambda: saved.append(b.digest_text)

        b._compress_history_if_needed()

        self.assertEqual(b.digest_text, 'digest-v2')
        self.assertEqual(b.digest_upto, 5 + len(batch))
        # the concurrently-evicted exchange must still await the next fold —
        # wiping the buffer would have silently erased it from the digest
        self.assertEqual(b._evicted_buffer, [late])
        self.assertEqual(saved, ['digest-v2'])

    def test_quiet_compression_empties_buffer(self):
        b = self._blank()
        batch = [(f'u{i}', f'a{i}')
                 for i in range(brain.Brain.COMPRESS_EVERY)]
        b._evicted_buffer = list(batch)
        b.complete = lambda prompt, agent=None, timeout=0, max_tokens=0: (
            'digest')
        b._save_digest = lambda: None
        b._compress_history_if_needed()
        self.assertEqual(b._evicted_buffer, [])
        self.assertEqual(b.digest_upto, 5 + len(batch))


# ====================================================================== #
# AgentLoop — mouth.suppress serialized under the executor lock
# ====================================================================== #

class TestAgentLoopSuppressRace(unittest.TestCase):
    """Regression: the global mouth.suppress flag was toggled OUTSIDE the
    executor lock, so concurrent tool calls tore it — a thread could run
    its whole body with suppress already reset by a peer's finally (its
    speech leaked mid-loop) and one thread's finally could leave suppress
    stuck True, muting Jarvis for the process lifetime."""

    def test_parallel_tools_keep_suppress_whole_and_restored(self):
        from types import SimpleNamespace
        from utils.llm.providers.base import ToolCall

        mouth = SimpleNamespace(suppress=False)

        class FakeExecutor:
            def __init__(self):
                self.mouth = mouth
                self.samples = []
                self.active = 0
                self.max_active = 0
                self.guard = threading.Lock()

            def execute_command(self, command, brain, ui_callback=None):
                with self.guard:
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                time.sleep(0.003)
                s1 = self.mouth.suppress          # mid-body sample
                time.sleep(0.003)
                s2 = self.mouth.suppress
                with self.guard:
                    self.active -= 1
                self.samples.append((s1, s2))
                return 'done'

        exe = FakeExecutor()
        loop = object.__new__(agent_loop.AgentLoop)
        loop._executor = exe
        loop.brain = object()

        calls = [(i, ToolCall(name='search_web', arguments='{"q":"x"}'))
                 for i in range(8)]
        with patch.dict(os.environ, {'JARVIS_AGENT_PARALLEL': '1'}):
            results = loop._run_tool_calls(calls)

        self.assertEqual(len(results), 8)
        # the whole toggle+body ran under _EXEC_LOCK -> never overlapped
        self.assertEqual(exe.max_active, 1)
        # every thread saw suppression active for its ENTIRE body
        for s1, s2 in exe.samples:
            self.assertTrue(s1)
            self.assertTrue(s2)
        # and it was restored for the process afterward
        self.assertFalse(mouth.suppress)

    def test_exception_still_restores_suppress(self):
        from types import SimpleNamespace
        from utils.llm.providers.base import ToolCall

        mouth = SimpleNamespace(suppress=False)

        class BoomExecutor:
            def execute_command(self, command, brain, ui_callback=None):
                # mimic a tool blowing up MID-body with suppression active
                self.mouth.suppress = True
                raise RuntimeError('kaboom')

        exe = BoomExecutor()
        exe.mouth = mouth
        loop = object.__new__(agent_loop.AgentLoop)
        loop._executor = exe
        loop.brain = object()
        calls = [(0, ToolCall(name='search_web', arguments='{}'))]
        with patch.dict(os.environ, {'JARVIS_AGENT_PARALLEL': '0'}):
            results = loop._run_tool_calls(calls)
        self.assertTrue(results[0].startswith('ERROR'))
        self.assertFalse(mouth.suppress)


# ====================================================================== #
# Automation — close_chrome only kills what this instance owns
# ====================================================================== #

class TestAutomationCloseChrome(unittest.TestCase):
    """utils.logger (imported by automation) creates jarvis.log on import,
    so the module is pulled in under a stubbed sys.modules entry."""

    def setUp(self):
        self.tmp = _make_tmp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _module(self):
        if 'utils.automation' in sys.modules:
            return sys.modules['utils.automation']
        import types
        import logging as _logging
        fake = types.ModuleType('utils.logger')
        fake.logger = _logging.getLogger('Jarvis.TestAutomation')
        with patch.dict(sys.modules, {'utils.logger': fake}):
            import utils.automation as _a
        return _a

    def _obj(self):
        automation = self._module()
        a = object.__new__(automation.Automation)
        a.chrome_process = None
        a.port = 9222
        a.profile_dir = os.path.join(self.tmp, 'chrome_profile')
        return automation, a

    def test_leftover_kill_is_scoped_to_our_profile(self):
        automation, a = self._obj()
        from types import SimpleNamespace
        with patch.object(
                automation.subprocess, 'run',
                return_value=SimpleNamespace(returncode=0,
                                             stdout='', stderr='')) as m:
            out = a.close_chrome()
        self.assertEqual(out, 'Browser processes terminated.')
        argv = m.call_args[0][0]
        self.assertEqual(argv[0], 'pkill')
        # port AND our dedicated profile dir — never any Chrome on the port
        self.assertIn('remote-debugging-port=9222', argv[2])
        self.assertIn('user-data-dir=', argv[2])
        # re.escape mangles the '/' separators, so check the surviving pieces
        self.assertIn('chrome_profile', argv[2])
        self.assertIn(os.path.basename(self.tmp), argv[2])

    def test_no_leftover_reports_honestly(self):
        automation, a = self._obj()
        from types import SimpleNamespace
        with patch.object(automation.subprocess, 'run',
                          return_value=SimpleNamespace(returncode=1)):
            out = a.close_chrome()
        self.assertEqual(out, 'No automation browser is running.')

    def test_instance_launched_process_terminated(self):
        automation, a = self._obj()
        proc = {'terminated': False, 'waited': False}
        class _P:
            def terminate(self):
                proc['terminated'] = True
            def wait(self, timeout=0):
                proc['waited'] = True
        a.chrome_process = _P()
        self.assertEqual(a.close_chrome(), 'Browser closed.')
        self.assertTrue(proc['terminated'])
        self.assertTrue(proc['waited'])
        self.assertIsNone(a.chrome_process)


# ====================================================================== #
# TUI — Ctrl+D exits cleanly on the main prompt and a continuation line
# ====================================================================== #

class TestTUIEOFHandling(unittest.TestCase):

    def test_eof_on_main_prompt_exits_zero(self):
        with patch('builtins.input', side_effect=EOFError):
            with self.assertRaises(SystemExit) as cm:
                tui._read_multiline('you ▸ ')
            self.assertEqual(cm.exception.code, 0)

    def test_eof_on_continuation_line_exits_zero(self):
        # a trailing '\' asks for more input; Ctrl+D there used to escape
        # as an ugly uncaught EOFError traceback
        with patch('builtins.input', side_effect=['draft \\', EOFError]):
            with self.assertRaises(SystemExit) as cm:
                tui._read_multiline('you ▸ ')
            self.assertEqual(cm.exception.code, 0)

    def test_backslash_joins_lines(self):
        with patch('builtins.input', side_effect=['first \\', 'second']):
            self.assertEqual(tui._read_multiline('you ▸ '), 'first second')


# ====================================================================== #
# Meeting audio — session token stops a stale worker writing the restart
# ====================================================================== #

class TestMeetingSessionToken(unittest.TestCase):

    def _blank(self):
        t = object.__new__(meeting_audio.MeetingTranscriber)
        t._recording = True
        t._session = 2                  # the restarted, live session
        t._lock = threading.RLock()
        t._transcript_lines = []
        return t

    def test_stale_worker_cannot_append_to_restarted_session(self):
        t = self._blank()
        # a worker that captured audio under the OLD session (1) must be
        # dropped even if stop()'s 5s join timed out mid-STT
        self.assertFalse(t._append_if_current(1, 'stale words'))
        self.assertEqual(t._transcript_lines, [])
        # a live worker of the current session still lands
        self.assertTrue(t._append_if_current(2, 'new words'))
        self.assertEqual(len(t._transcript_lines), 1)
        self.assertIn('new words', t._transcript_lines[0])
        # once recording stops nothing else lands, even from session 2
        t._recording = False
        self.assertFalse(t._append_if_current(2, 'after stop'))
        self.assertEqual(len(t._transcript_lines), 1)

    def test_blank_text_is_never_appended(self):
        t = self._blank()
        self.assertFalse(t._append_if_current(2, '   '))
        self.assertFalse(t._append_if_current(2, ''))
        self.assertEqual(t._transcript_lines, [])


# ====================================================================== #
# Mobile studio / dev studio — LLM-supplied paths cannot escape workspace
# ====================================================================== #

class TestWorkspacePathContainment(unittest.TestCase):

    def setUp(self):
        self.tmp = _make_tmp()
        self.mobile_ws = os.path.join(self.tmp, 'mobile_projects')
        self.dev_ws = os.path.join(self.tmp, 'dev_projects')
        os.makedirs(self.mobile_ws)
        os.makedirs(self.dev_ws)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mobile(self):
        m = object.__new__(mobile_studio.MobileManager)
        m.workspace_dir = self.mobile_ws
        return m

    def _dev(self):
        d = object.__new__(dev_studio.ProjectManager)
        d.workspace_dir = self.dev_ws
        return d

    # ---- _resolve containment --------------------------------------- #
    def test_resolve_rejects_escape(self):
        for mgr in (self._mobile(), self._dev()):
            self.assertIsNone(mgr._resolve('..'))
            self.assertIsNone(mgr._resolve('a', '..', '..', 'x'))
            self.assertIsNone(mgr._resolve('app', '../../x'))
            self.assertIsNone(mgr._resolve('/etc/passwd'))
            self.assertIsNone(mgr._resolve('app', '/etc/passwd'))
        inside = self._mobile()._resolve('app')
        self.assertEqual(inside,
                         os.path.join(os.path.abspath(self.mobile_ws), 'app'))

    def test_safe_component_rules(self):
        comp = mobile_studio.MobileManager._safe_component
        self.assertTrue(comp('my_app'))
        self.assertTrue(comp('my.app'))
        self.assertFalse(comp(''))
        self.assertFalse(comp('..'))
        self.assertFalse(comp('.'))
        self.assertFalse(comp('a/b'))
        self.assertFalse(comp('/abs'))
        self.assertFalse(comp('../x'))

    def test_init_names_refuse_escape_before_touching_disk(self):
        m = self._mobile()
        self.assertIn('Invalid project name', m.init_flutter_app('../evil'))
        self.assertIn('Invalid project name', m.init_react_native('../../x'))

    # ---- mobile read/write ------------------------------------------ #
    def test_mobile_write_refuses_escape_but_writes_inside(self):
        m = self._mobile()
        safe = os.path.join(self.mobile_ws, 'safe')
        os.makedirs(safe)
        r = m.write_feature('safe', '../../pwn.txt', 'owned')
        self.assertIn('Refusing', r)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'pwn.txt')))
        self.assertEqual(m.write_feature('safe', 'lib/models/user.dart',
                                         'class User{}'),
                         'Updated lib/models/user.dart')
        fp = os.path.join(safe, 'lib', 'models', 'user.dart')
        self.assertTrue(os.path.exists(fp))
        self.assertEqual(m.read_file('safe', 'lib/models/user.dart'),
                         'class User{}')
        self.assertIn('Refusing', m.read_file('safe', '../../etc/passwd'))
        # no escape file materialised anywhere above the workspace
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'etc')))

    # ---- dev studio create/write/zip -------------------------------- #
    def test_dev_create_rejects_escape(self):
        d = self._dev()
        self.assertIn('Error', d.create_project('..', 'python'))
        # nothing materialised in (or above) the workspace
        self.assertEqual(os.listdir(self.dev_ws), [])

    def test_dev_write_zip_stay_in_workspace(self):
        d = self._dev()
        self.assertIn('Created', d.create_project('demo', 'web'))
        # '../../x.py' from inside 'demo' climbs above the workspace
        self.assertIn('Refusing', d.write_to_file('demo', '../../x.py', 'x'))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'x.py')))
        self.assertEqual(d.write_to_file('demo', 'js/app.js',
                                         'console.log(1)'),
                         'Successfully wrote code to js/app.js in demo.')
        z = d.zip_project('demo')
        self.assertIsNotNone(z)
        self.assertTrue(z.startswith(os.path.abspath(self.dev_ws) + os.sep))
        self.assertTrue(os.path.isfile(z))
        self.assertIsNone(d.zip_project('..'))
        self.assertIsNone(d.zip_project('missing'))


# ====================================================================== #
# Event bus — background saturation drops with a busy reply
# ====================================================================== #

class TestEventBusSaturation(unittest.TestCase):

    def test_ninth_bg_event_is_answered_busy_and_dropped(self):
        bus = event_bus.EventBus()
        entered = []
        entered_evt = threading.Event()
        release = threading.Event()

        def handler(ev):
            entered.append(ev.text)
            if len(entered) >= 8:
                entered_evt.set()
            release.wait(5)
            return 'ok'

        bus.handler = handler
        replies = []

        def publish(i):
            ev = event_bus.InternalEvent(
                'webhook', text=f'req{i}',
                reply=lambda m, _i=i: replies.append((_i, str(m))))
            bus.publish(ev, background=True)

        for i in range(8):
            publish(i)
        self.assertTrue(entered_evt.wait(5),
                        'the 8 slots must all be occupied by now')
        publish(8)                          # slot pool is exhausted
        release.set()
        time.sleep(0.4)
        # 8 handlers ran and answered; the 9th got a busy reply and never ran
        self.assertEqual(len(entered), 8)
        busy = [(i, m) for (i, m) in replies if m.startswith('Jarvis is')]
        self.assertEqual([i for i, _ in busy], [8])
        self.assertNotIn(8, [i for i, m in replies if m == 'ok'])


# ====================================================================== #
# HUD — replies cross threads via a queue, never direct Tk calls
# ====================================================================== #

class TestHudThreadSafeReplies(unittest.TestCase):

    def _stub(self):
        w = object.__new__(hud.HudWindow)
        w._outbox = queue.Queue()
        w._hidden = True

        class Label:
            def __init__(self):
                self.configured = []
            def configure(self, **kw):
                self.configured.append(kw)

        class Root:
            def __init__(self):
                self.afters = []
            def after(self, ms, fn):
                self.afters.append((ms, fn))
                return len(self.afters)
            def deiconify(self):
                pass
            def lift(self):
                pass
            def withdraw(self):
                pass

        class Entry:
            def __init__(self):
                self.deleted = []
            def focus_set(self):
                pass
            def delete(self, *a):
                self.deleted.append(a)

        w.label = Label()
        w.root = Root()
        w.entry = Entry()
        return w

    def test_worker_reply_only_queues(self):
        w = self._stub()
        w._reply('the answer')             # called on the pipeline thread
        self.assertEqual(w.label.configured, [])   # no widget access here
        self.assertEqual(w._outbox.qsize(), 1)
        self.assertEqual(w._outbox.get_nowait(), 'the answer')

    def test_poller_applies_on_main_thread_and_reschedules(self):
        w = self._stub()
        w._reply('hello sir')
        w._poll_outbox()
        self.assertEqual(w.label.configured, [{'text': '▸ hello sir'}])
        self.assertFalse(w._hidden)
        self.assertEqual(w.root.afters[-1][0], 150)   # next poll on Tk thread

    def test_show_preserves_composer_text(self):
        w = self._stub()
        w.entry.deleted.clear()
        w.show()                            # fade-in must not clear typing
        self.assertEqual(w.entry.deleted, [])
        self.assertFalse(w._hidden)


# ====================================================================== #
# Web reader — graceful blank queries + quoted multi-token weather lookup
# ====================================================================== #

class TestWebReaderAnswerGuards(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import utils.web_reader as _wr
        cls.wr = _wr

    def test_blank_query_answers_gracefully(self):
        # None / '' used to crash on query.lower(); no network is touched
        for q in (None, '', '   '):
            self.assertIn('need a question',
                          self.wr.get_answer_from_web(q))

    def test_weather_location_is_multitoken_and_quoted(self):
        captured = {}

        class _Resp:
            status_code = 200
            text = 'New York, NY: 72°F'

        def _fake_get(url, timeout=5):
            captured['url'] = url
            return _Resp()

        with patch.object(self.wr.requests, 'get',
                          side_effect=_fake_get):
            out = self.wr.get_answer_from_web('weather in New York today')
        self.assertIn('Current weather', out)
        # multi-word location captured, trailing filler trimmed, URL-encoded
        self.assertIn('new%20york', captured['url'])
        self.assertNotIn(' ', captured['url'].split('?')[0])
        self.assertNotIn('today', captured['url'])

    def test_unknown_weather_place_falls_through(self):
        captured = {}

        class _Resp:
            status_code = 200
            text = 'Sorry, I do not understand'

        def _fake_get(url, timeout=5):
            captured['url'] = url
            return _Resp()

        # wttr.in's "Sorry" body is not reported as weather; we fall through
        # to the general search path instead of asserting a wrong reading.
        with patch.object(self.wr.requests, 'get',
                          side_effect=_fake_get), \
             patch.object(self.wr, 'search_and_read',
                          return_value='search fallback path'):
            out = self.wr.get_answer_from_web('weather in zzzzzz')
        self.assertEqual(out, 'search fallback path')
        self.assertIn('zzzzzz', captured['url'])


if __name__ == '__main__':
    unittest.main()
