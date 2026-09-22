"""
J.A.R.V.I.S. — Capability Awareness System
============================================

Makes Jarvis self-aware: it knows what it can do, assesses incoming
tasks against its capabilities, and when something is missing, creates
a concrete plan to acquire that capability before executing the task.

Core loop:
    user task → assess(task) → available? → execute
                                → missing?  → plan upgrade → acquire → execute

Kill switch: ``JARVIS_CAPABILITY_CHECK=0`` disables pre-task assessment
(returns everything as available).
"""

import os
import re
import sys
import time
import shutil
import logging
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger("Jarvis.Capabilities")

# --------------------------------------------------------------------- #
#  Data types
# --------------------------------------------------------------------- #

@dataclass
class Capability:
    """One capability Jarvis may or may not have."""
    name: str
    description: str
    category: str          # core | information | communication | development | device | knowledge
    check: Callable        # () -> bool
    keywords: list = field(default_factory=list)
    auto_install: list = field(default_factory=list)  # pip packages
    setup_instructions: str = ''


@dataclass
class CapabilityStatus:
    """Result of checking one capability."""
    name: str
    description: str
    category: str
    status: str            # ready | missing | degraded | disabled
    detail: str            # human-readable explanation
    upgrade_steps: list = field(default_factory=list)


@dataclass
class AssessmentResult:
    """Result of assessing a task against capabilities."""
    task: str
    available: list = field(default_factory=list)   # CapabilityStatus
    missing: list = field(default_factory=list)      # CapabilityStatus
    upgrade_plan: list = field(default_factory=list)  # step dicts


# --------------------------------------------------------------------- #
#  Readiness checks (lazy — import only when called)
# --------------------------------------------------------------------- #

def _check_llm():
    """LLM reasoning available?"""
    try:
        from utils.brain import Brain
        # Check if any provider key exists
        env_keys = ['GROQ_API_KEY', 'GOOGLE_API_KEY', 'OPENAI_API_KEY',
                    'ANTHROPIC_API_KEY', 'DEEPSEEK_API_KEY',
                    'OPENROUTER_API_KEY', 'CUSTOM_OPENAI_API_KEY']
        has_key = any(os.getenv(k) for k in env_keys)
        if has_key:
            return True, 'API key configured'
        # Check keystore
        try:
            from utils.llm.keystore import get_keystore
            ks = get_keystore()
            for name in ('groq', 'google', 'openai', 'anthropic',
                         'deepseek', 'openrouter', 'custom'):
                if ks.get(name):
                    return True, f'{name} key in keystore'
        except Exception:
            pass
        # Check local LLM
        if os.getenv('JARVIS_DISABLE_LOCAL', '') != '1':
            import socket
            for port in (11434, 1234):
                try:
                    s = socket.create_connection(('localhost', port), timeout=1)
                    s.close()
                    local = 'Ollama' if port == 11434 else 'LM Studio'
                    return True, f'{local} running locally'
                except (OSError, ConnectionRefusedError):
                    pass
        return False, 'No LLM provider configured (set GROQ_API_KEY or run Ollama)'
    except Exception as e:
        return False, f'LLM check failed: {e}'


def _check_agent_loop():
    """Agent loop with tool-calling available?"""
    try:
        from utils.agent_loop import agent_tools_enabled
        if not agent_tools_enabled():
            return False, 'JARVIS_AGENT_MODE is off'
        # Need a tool-capable model via router
        try:
            from utils.llm import get_router
            router = get_router()
            if not router.providers:
                return False, 'No LLM providers in router'
            if hasattr(router, '_chain'):
                chain = router._chain(require={'tools'})
                if chain:
                    return True, 'Tool-capable model available'
            return False, 'No tool-capable model in fleet'
        except Exception:
            return False, 'Router not initialized'
    except Exception as e:
        return False, f'Agent loop check failed: {e}'


def _check_web_search():
    """Web search available?"""
    try:
        import duckduckgo_search
        return True, 'duckduckgo_search installed'
    except ImportError:
        return False, 'pip install duckduckgo-search'


def _check_email():
    """Email (Gmail API) available?"""
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    has_secret = os.path.exists(os.path.join(base, 'client_secret.json'))
    has_token = os.path.exists(os.path.join(base, 'token.json'))
    if has_secret and has_token:
        return True, 'Gmail credentials configured'
    if has_secret:
        return False, 'client_secret.json found but token.json missing (run auth flow)'
    return False, 'No Gmail credentials (place client_secret.json in project root)'


def _check_calendar():
    """Calendar available? (same deps as email)"""
    return _check_email()


def _check_code_execution():
    """Code execution backends available?"""
    try:
        from utils.runtimes import available_backends, enabled
        if not enabled():
            return False, 'JARVIS_RUNTIMES=0'
        backends = available_backends()
        if 'docker' in backends:
            return True, f'Backends: {", ".join(backends)}'
        if 'local' in backends:
            return True, 'Local backend (install Docker for sandboxed execution)'
        return False, 'No execution backends available'
    except Exception as e:
        return False, f'Code execution check failed: {e}'


def _check_browser():
    """Browser automation available?"""
    try:
        from utils.browser_use import enabled, status
        if not enabled():
            return False, 'JARVIS_BROWSER_USE=0'
        st = status()
        if st.get('browser_agent'):
            return True, 'Playwright browser agent ready'
        return False, 'Playwright not installed (pip install playwright && playwright install chromium)'
    except Exception as e:
        return False, f'Browser check failed: {e}'


def _check_desktop_control():
    """Desktop control (Accessibility/CGEvent) available?"""
    try:
        from utils.computer_use import enabled
        if not enabled():
            return False, 'JARVIS_COMPUTER_USE=0'
        if sys.platform != 'darwin':
            return True, 'Desktop control available (non-macOS)'
        # macOS: check Accessibility
        try:
            import ApplicationServices
            # If we can import, Accessibility is likely available
            return True, 'macOS Accessibility available'
        except ImportError:
            return False, ('pip install pyobjc-framework-ApplicationServices '
                          'and enable Accessibility in System Settings')
    except Exception as e:
        return False, f'Desktop control check failed: {e}'


def _check_mobile_dev():
    """Mobile development (Flutter) available?"""
    flutter = shutil.which('flutter')
    if flutter:
        return True, f'Flutter at {flutter}'
    # Check common locations
    for path in ('~/flutter/bin/flutter', '~/.flutter/bin/flutter'):
        expanded = os.path.expanduser(path)
        if os.path.exists(expanded):
            return True, f'Flutter at {expanded}'
    return False, 'Flutter SDK not found (git clone https://github.com/flutter/flutter -b stable ~/flutter)'


def _check_mcp():
    """MCP tools available?"""
    try:
        from utils.mcp_client import get_pool
        pool = get_pool()
        pool.ensure_started()
        st = pool.status()
        online = [s for s in st if s.get('state') == 'online']
        if online:
            names = [s.get('name', '?') for s in online]
            return True, f'MCP servers online: {", ".join(names)}'
        unstarted = [s for s in st if s.get('state') == 'unstarted']
        if unstarted:
            return False, f'MCP servers configured but not running ({len(unstarted)} unstarted)'
        return False, 'No MCP servers online (configure in config/mcp_servers.json)'
    except Exception as e:
        return False, f'MCP check failed: {e}'


def _check_external_tools():
    """External tool registry has tools?"""
    try:
        from utils.tool_registry import ToolRegistry
        tr = ToolRegistry()
        count = tr.count()
        if count > 0:
            return True, f'{count} external tools registered'
        return False, 'No tools in tools_registry/ (add JSON manifests)'
    except Exception as e:
        return False, f'Tool registry check failed: {e}'


def _check_skills():
    """Skills registry has skills?"""
    try:
        from utils.skills import SkillRegistry
        sr = SkillRegistry()
        count = sr.count()
        if count > 0:
            return True, f'{count} skills installed'
        return False, 'No skills yet (skills are created from successful workflows)'
    except Exception as e:
        return False, f'Skills check failed: {e}'


def _check_smart_home():
    """Smart home (virtual simulation) — always available."""
    return True, 'Virtual smart home simulation'


def _check_rlm_memory():
    """RLM long-term memory available?"""
    try:
        from utils.rlm import enabled, get_rlm
        if not enabled():
            return False, 'JARVIS_RLM=0'
        rlm = get_rlm()
        stats = rlm.stats()
        notes = stats.get('notes', 0)
        return True, f'RLM memory active ({notes} notes)'
    except Exception as e:
        return False, f'RLM check failed: {e}'


def _check_knowledge_vault():
    """Knowledge vault (vector search) available?"""
    try:
        import chromadb
        return True, 'ChromaDB installed'
    except ImportError:
        return False, 'pip install chromadb (vector search for knowledge vault)'


def _check_voice():
    """Voice I/O available?"""
    if sys.platform == 'darwin':
        return True, 'macOS voice available (SAX/说speak)'
    return False, 'Voice requires macOS speech frameworks'


def _check_proactive():
    """Proactive monitoring available?"""
    try:
        has_secret = os.path.exists('client_secret.json')
        has_token = os.path.exists('token.json')
        if has_secret and has_token:
            return True, 'Calendar + email access for proactive monitoring'
        return False, 'Requires Gmail/Calendar credentials (client_secret.json + token.json)'
    except Exception:
        return False, 'Proactive check failed'


def _check_dev_studio():
    """Dev studio (project scaffolding) available?"""
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    projects = os.path.join(base, 'Jarvis_Projects')
    if os.path.isdir(projects):
        return True, 'Project workspace ready'
    try:
        os.makedirs(projects, exist_ok=True)
        return True, 'Project workspace created'
    except Exception as e:
        return False, f'Cannot create workspace: {e}'


def _check_scheduling():
    """Reminders + recurring automations available? (stdlib only)"""
    try:
        from utils.scheduler import ReminderScheduler
        from utils.recurring import RecurringAutomations
        return True, 'Reminders + recurring automations ready'
    except Exception as e:
        return False, f'Scheduling check failed: {e}'


def _check_goal_tracking():
    """Goal engine + complex-task manager available?"""
    try:
        from utils.goals import enabled as _goals_on
        if not _goals_on():
            return False, 'JARVIS_GOALS=0'
        return True, 'Goal tracking ready'
    except Exception as e:
        return False, f'Goal tracking check failed: {e}'


def _check_transcript_search():
    """Transcript full-text search available? (stdlib sqlite3)"""
    try:
        import sqlite3
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        db = os.path.join(base, 'brain', 'data', 'transcripts.db')
        if os.path.exists(db):
            return True, 'Transcript archive ready'
        return True, 'Transcript search ready (archive builds on first chat)'
    except Exception as e:
        return False, f'Transcript search check failed: {e}'


def _check_self_improvement():
    """Self-learning loop (lessons + skill forge) available?"""
    try:
        from utils.self_learning import enabled as _learn_on
        if not _learn_on():
            return False, 'JARVIS_AUTO_LEARN=0'
        return True, 'Self-improvement loop ready (lessons + skill forge)'
    except Exception as e:
        return False, f'Self-improvement check failed: {e}'


def _check_checkpoints():
    """Workspace checkpoints / rollback available?"""
    try:
        from utils.checkpoints import enabled as _cp_on
        if not _cp_on():
            return False, 'JARVIS_CHECKPOINTS=0'
        return True, 'Workspace checkpoints ready'
    except Exception as e:
        return False, f'Checkpoints check failed: {e}'


def _check_budget():
    """Session budget guard available?"""
    try:
        from utils.budget import Budget
        b = Budget()
        return True, f'Budget guard ready (${b.limit_usd:.2f} / {b.limit_tokens} tokens)'
    except Exception as e:
        return False, f'Budget check failed: {e}'


def _check_relationships():
    """Relationship memory available?"""
    try:
        from utils.relationships import RelationshipManager
        return True, 'Relationship memory ready'
    except Exception as e:
        return False, f'Relationship check failed: {e}'


def _check_code_health():
    """Post-edit diagnostics available? (tier-1 checkers always work)"""
    try:
        from utils.lsp_diagnostics import enabled as _lsp_on
        if not _lsp_on():
            return False, 'JARVIS_LSP_DIAGNOSTICS=0'
        tiers = ['python compile/AST']
        if shutil.which('node'):
            tiers.append('node --check')
        if shutil.which('tsc'):
            tiers.append('tsc --noEmit')
        if os.getenv('JARVIS_LSP_CMD'):
            tiers.append('LSP server')
        return True, f"Diagnostics ready ({', '.join(tiers)})"
    except Exception as e:
        return False, f'Code health check failed: {e}'


def _check_multi_model():
    """Mixture-of-Agents panel available? (needs 2+ providers)"""
    try:
        from utils.moa import enabled as _moa_on
        if not _moa_on():
            return False, 'JARVIS_MOA=0'
        try:
            from utils.llm import get_router
            router = get_router()
            providers = getattr(router, 'providers', {}) or {}
            n = len(providers)
            if n >= 2:
                return True, f'MoA panel ready ({n} providers)'
            return False, f'Need 2+ LLM providers for a panel (have {n})'
        except Exception:
            return False, 'Router not initialized'
    except Exception as e:
        return False, f'Multi-model check failed: {e}'


def _check_ambient_context():
    """Passive context engine available?"""
    try:
        from utils.context_engine import ContextEngine
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        has_secret = os.path.exists(os.path.join(base, 'client_secret.json'))
        has_token = os.path.exists(os.path.join(base, 'token.json'))
        if has_secret and has_token:
            return True, 'Ambient context ready (calendar + email sources)'
        return True, 'Ambient context ready (local files only — add Gmail creds for inbox/calendar)'
    except Exception as e:
        return False, f'Ambient context check failed: {e}'


def _check_remote_access():
    """Telegram / gateway remote access available?"""
    try:
        if os.getenv('JARVIS_GATEWAYS', '1') == '0':
            return False, 'JARVIS_GATEWAYS=0'
        token = os.getenv('TELEGRAM_TOKEN', '')
        try:
            import telegram
            has_ptb = True
        except ImportError:
            has_ptb = False
        if not token:
            return False, 'Set TELEGRAM_TOKEN + TELEGRAM_ALLOWED_IDS in .env'
        if not has_ptb:
            return False, 'pip install python-telegram-bot'
        return True, 'Telegram remote access ready'
    except Exception as e:
        return False, f'Remote access check failed: {e}'


def _check_meeting_notes():
    """Meeting transcription available?"""
    try:
        import speech_recognition
        return True, 'Meeting transcription ready'
    except ImportError:
        return False, 'pip install SpeechRecognition (optional: pyaudio for mic capture)'


def _check_git_ops():
    """Git + GitHub push available?"""
    git = shutil.which('git')
    if not git:
        return False, 'git binary not found (install Xcode CLT / git)'
    try:
        import github
        has_gh = True
    except ImportError:
        has_gh = False
    token = os.getenv('GITHUB_TOKEN', '')
    if has_gh and token:
        return True, 'Git + GitHub push ready'
    if has_gh:
        return True, 'Git ready (set GITHUB_TOKEN for remote push)'
    return True, 'Local git ready (pip install PyGithub + GITHUB_TOKEN for remote push)'


def _check_file_ops():
    """File downloads / web reader / organizer available?"""
    try:
        import requests
        import bs4
        return True, 'File ops ready (downloads + web reader + organizer)'
    except ImportError:
        return False, 'pip install requests beautifulsoup4 yagmail'


def _check_desktop_vision():
    """Vision-driven desktop agent available? (distinct from AX control)"""
    try:
        import pyautogui
        import PIL
        return True, 'Desktop vision agent ready (needs Screen Recording permission)'
    except ImportError:
        return False, 'pip install pyautogui pillow (+ Screen Recording permission on macOS)'


def _check_wake_free():
    """Hands-free wake-word gating available?"""
    if os.getenv('JARVIS_WAKE_WORD_ENABLE', '0') != '1':
        return False, 'Set JARVIS_WAKE_WORD_ENABLE=1 for hands-free activation'
    try:
        import speech_recognition
        return True, 'Wake-word gating ready'
    except ImportError:
        return False, 'pip install SpeechRecognition (optional: openwakeword)'


def _check_vision_cooking():
    """Fridge vision + recipe engine available?"""
    try:
        from utils.fridge_vision import FridgeVision
        return True, 'Fridge vision ready (needs a food photo + vision LLM)'
    except Exception as e:
        return False, f'Vision cooking check failed: {e}'


# --------------------------------------------------------------------- #
#  Capability registry
# --------------------------------------------------------------------- #

_CAPABILITIES = [
    Capability(
        name='llm_reasoning',
        description='LLM reasoning and conversation',
        category='core',
        check=_check_llm,
        keywords=['think', 'reason', 'analyze', 'understand', 'explain',
                  'what is', 'how do', 'why', 'compare', 'summarize'],
    ),
    Capability(
        name='agent_loop',
        description='Native tool-calling agent loop',
        category='core',
        check=_check_agent_loop,
        keywords=['use tools', 'browse and', 'search and'],
    ),
    Capability(
        name='web_search',
        description='Live web search',
        category='information',
        check=_check_web_search,
        keywords=['web search', 'search the web', 'find online', 'look up',
                  'google', 'news', 'weather', 'stock price'],
    ),
    Capability(
        name='email',
        description='Gmail read/send',
        category='communication',
        check=_check_email,
        keywords=['email', 'send mail', 'inbox', 'message', 'gmail'],
    ),
    Capability(
        name='calendar',
        description='Google Calendar access',
        category='communication',
        check=_check_calendar,
        keywords=['calendar', 'schedule', 'meeting', 'event',
                  'appointment', 'what\'s on'],
    ),
    Capability(
        name='code_execution',
        description='Run Python/shell code in sandbox',
        category='development',
        check=_check_code_execution,
        keywords=['run code', 'execute', 'script', 'python', 'program',
                  'shell', 'command', 'terminal'],
    ),
    Capability(
        name='browser',
        description='Browser automation (Playwright)',
        category='device',
        check=_check_browser,
        keywords=['browse', 'website', 'web page', 'open url', 'click on page',
                  'screenshot of', 'scrape', 'fill form'],
    ),
    Capability(
        name='desktop_control',
        description='Desktop GUI control (Accessibility/CGEvent)',
        category='device',
        check=_check_desktop_control,
        keywords=['desktop', 'screen', 'click element', 'type in app',
                  'app window', 'gui', 'mouse', 'keyboard'],
    ),
    Capability(
        name='mobile_dev',
        description='Mobile app development (Flutter)',
        category='development',
        check=_check_mobile_dev,
        keywords=['mobile app', 'flutter', 'react native', 'ios app',
                  'android app', 'phone app'],
    ),
    Capability(
        name='mcp_tools',
        description='MCP server tools',
        category='core',
        check=_check_mcp,
        keywords=['mcp'],
    ),
    Capability(
        name='external_tools',
        description='Registered external REST tools',
        category='core',
        check=_check_external_tools,
        keywords=['external tool', 'api tool'],
    ),
    Capability(
        name='skills',
        description='Reusable skill scripts',
        category='core',
        check=_check_skills,
        keywords=['skill', 'automate workflow', 'reusable'],
    ),
    Capability(
        name='smart_home',
        description='Smart home device control (virtual)',
        category='device',
        check=_check_smart_home,
        keywords=['smart home', 'living room light', 'bedroom light',
                  'turn on the', 'turn off the', 'dim the', 'thermostat'],
    ),
    Capability(
        name='rlm_memory',
        description='Recursive long-term memory',
        category='knowledge',
        check=_check_rlm_memory,
        keywords=['remember', 'recall', 'what did we', 'past conversation',
                  'memory', 'history'],
    ),
    Capability(
        name='knowledge_vault',
        description='Vector search over documents',
        category='knowledge',
        check=_check_knowledge_vault,
        keywords=['document', 'pdf', 'knowledge base', 'vector',
                  'similarity search'],
    ),
    Capability(
        name='voice',
        description='Voice I/O (speech synthesis/recognition)',
        category='device',
        check=_check_voice,
        keywords=['my voice', 'use voice', 'voice reply', 'voice input',
                  'read aloud', 'out loud', 'microphone'],
    ),
    Capability(
        name='proactive',
        description='Proactive monitoring (calendar+email)',
        category='core',
        check=_check_proactive,
        keywords=['proactive', 'monitor', 'watch', 'alert',
                  'morning briefing', 'daily summary'],
    ),
    Capability(
        name='dev_studio',
        description='Project scaffolding and code generation',
        category='development',
        check=_check_dev_studio,
        keywords=['project', 'build app', 'create project', 'code project',
                  'scaffold', 'new app'],
    ),
    # ---- Tier 1: stdlib-only, always-ready subsystems ---- #
    Capability(
        name='scheduling',
        description='Reminders + recurring automations',
        category='core',
        check=_check_scheduling,
        keywords=['remind me', 'reminder', 'set alarm', 'wake me',
                  'every day at', 'every morning',
                  'recurring', 'cron', 'daily at', 'weekly at',
                  'every monday', 'every friday'],
    ),
    Capability(
        name='goal_tracking',
        description='Goal tracking + multi-step missions',
        category='core',
        check=_check_goal_tracking,
        keywords=['goal', 'track progress', 'track my', 'mission',
                  'multi-step plan', 'objective', 'track this',
                  'finish', 'deadline', 'milestone'],
    ),
    Capability(
        name='transcript_search',
        description='Full-text search over past conversations',
        category='knowledge',
        check=_check_transcript_search,
        keywords=['what did i say', 'what did we discuss', 'search history',
                  'find conversation', 'earlier we', 'past chat',
                  'what did we talk about'],
    ),
    Capability(
        name='self_improvement',
        description='Self-learning loop (lessons + skill forge)',
        category='core',
        check=_check_self_improvement,
        keywords=['learn from this', 'lesson', 'improve yourself',
                  'self-improve', 'remember how to do this'],
    ),
    Capability(
        name='checkpoints',
        description='Workspace checkpoints + rollback',
        category='development',
        check=_check_checkpoints,
        keywords=['checkpoint', 'rollback', 'undo changes',
                  'restore snapshot', 'before you edit', 'revert my'],
    ),
    Capability(
        name='budget_guard',
        description='Session budget guard (tokens + USD)',
        category='core',
        check=_check_budget,
        keywords=['budget', 'spending', 'token usage', 'cost so far',
                  'api cost', 'how much have'],
    ),
    Capability(
        name='relationships',
        description='Relationship memory + briefings',
        category='knowledge',
        check=_check_relationships,
        keywords=['birthday', 'anniversary', 'gift for', 'my wife',
                  'my husband', 'my mother', 'my father', 'relationship',
                  'who is my', 'briefing about'],
    ),
    Capability(
        name='code_health',
        description='Post-edit diagnostics (syntax/lint)',
        category='development',
        check=_check_code_health,
        keywords=['check syntax', 'diagnose code', 'lint', 'type error',
                  'why is this broken', 'py_compile'],
    ),
    Capability(
        name='multi_model',
        description='Mixture-of-Agents panel (2+ models)',
        category='core',
        check=_check_multi_model,
        keywords=['second opinion', 'ask all models', 'panel of models',
                  'compare models', 'best answer from'],
    ),
    Capability(
        name='ambient_context',
        description='Passive background context (inbox/calendar/files)',
        category='core',
        check=_check_ambient_context,
        keywords=['keep an eye on', 'watch my inbox', 'background context',
                  "what's new with", 'passive monitor'],
    ),
    # ---- Tier 2: pip-installable / credential-gated ---- #
    Capability(
        name='remote_access',
        description='Remote control via Telegram/Discord/Slack',
        category='communication',
        check=_check_remote_access,
        keywords=['telegram', 'discord', 'slack', 'remote control',
                  'from my phone', 'notify me on telegram'],
    ),
    Capability(
        name='meeting_notes',
        description='Meeting transcription + action items',
        category='communication',
        check=_check_meeting_notes,
        keywords=['meeting mode', 'transcribe meeting', 'transcribe this call',
                  'transcribe this', 'transcription of', 'action items from',
                  'start meeting mode', 'stop meeting'],
    ),
    Capability(
        name='git_ops',
        description='Git version control + GitHub push',
        category='development',
        check=_check_git_ops,
        keywords=['git init', 'git commit', 'push to github', 'push this',
                  'create repo', 'github repo', 'pull request', 'git push'],
    ),
    Capability(
        name='file_ops',
        description='File downloads + web reader + organizer',
        category='information',
        check=_check_file_ops,
        keywords=['organize downloads', 'organize my', 'clean downloads',
                  'download pdf', 'download file', 'download this',
                  'read webpage', 'deep read', 'fetch page', 'read this page'],
    ),
    Capability(
        name='desktop_vision',
        description='Vision-driven desktop agent (eyes + hands)',
        category='device',
        check=_check_desktop_vision,
        keywords=['like a human', 'eyes and hands', 'see my screen and',
                  'do this task in', 'operate this app for me'],
    ),
    Capability(
        name='wake_free',
        description='Hands-free wake-word activation',
        category='device',
        check=_check_wake_free,
        keywords=['hands-free', 'hands free', 'wake word',
                  'always listening'],
    ),
    Capability(
        name='vision_cooking',
        description='Fridge vision + recipe suggestions',
        category='information',
        check=_check_vision_cooking,
        keywords=['fridge', 'what can i cook', 'recipe from',
                  'look at my fridge', 'what to cook', 'leftovers'],
    ),
]


# --------------------------------------------------------------------- #
#  Upgrade steps
# --------------------------------------------------------------------- #

UPGRADE_STEPS = {
    'email': [
        {'step': 'Get Gmail API credentials',
         'instructions': ('Go to console.cloud.google.com → APIs & Services '
                          '→ Credentials → Create OAuth 2.0 Client ID → '
                          'Download client_secret.json → Place in project root')},
        {'step': 'Authenticate Gmail',
         'instructions': 'Run the Jarvis email auth flow to generate token.json'},
    ],
    'calendar': [
        {'step': 'Get Google Calendar API credentials',
         'instructions': ('Enable Google Calendar API in console.cloud.google.com '
                          '→ same OAuth credentials as email work here')},
        {'step': 'Authenticate Calendar',
         'instructions': 'Same auth flow as email — token.json covers both'},
    ],
    'browser': [
        {'step': 'Install Playwright',
         'action': 'pip_install',
         'packages': ['playwright'],
         'command': 'playwright install chromium'},
    ],
    'desktop_control': [
        {'step': 'Enable Accessibility permission',
         'instructions': ('System Settings → Privacy & Security → Accessibility '
                          '→ Add Terminal (or Python) to the allowed list')},
        {'step': 'Install pyobjc (macOS)',
         'action': 'pip_install',
         'packages': ['pyobjc-framework-ApplicationServices']},
    ],
    'mobile_dev': [
        {'step': 'Install Flutter SDK',
         'instructions': ('git clone https://github.com/flutter/flutter '
                          '-b stable ~/flutter && '
                          'export PATH="$PATH:~/flutter/bin"')},
    ],
    'web_search': [
        {'step': 'Install duckduckgo-search',
         'action': 'pip_install',
         'packages': ['duckduckgo-search']},
    ],
    'knowledge_vault': [
        {'step': 'Install ChromaDB for vector search',
         'action': 'pip_install',
         'packages': ['chromadb']},
    ],
    'llm_reasoning': [
        {'step': 'Set at least one LLM API key',
         'instructions': ('Set one of: GROQ_API_KEY, GOOGLE_API_KEY, '
                          'OPENAI_API_KEY, ANTHROPIC_API_KEY, '
                          'DEEPSEEK_API_KEY, OPENROUTER_API_KEY '
                          'in .env — or run Ollama/LM Studio locally')},
    ],
    'code_execution': [
        {'step': 'Install Docker (optional, for sandboxed execution)',
         'instructions': 'Install Docker Desktop from docker.com'},
    ],
    'proactive': [
        {'step': 'Set up Gmail/Calendar credentials',
         'instructions': 'Same as email + calendar setup above'},
    ],
    'remote_access': [
        {'step': 'Install python-telegram-bot',
         'action': 'pip_install',
         'packages': ['python-telegram-bot']},
        {'step': 'Create a Telegram bot + allowlist yourself',
         'instructions': ('Message @BotFather on Telegram → /newbot → paste the '
                          'token as TELEGRAM_TOKEN in .env → add your numeric user '
                          'ID to TELEGRAM_ALLOWED_IDS')},
    ],
    'meeting_notes': [
        {'step': 'Install SpeechRecognition',
         'action': 'pip_install',
         'packages': ['SpeechRecognition']},
        {'step': 'Install mic capture backend (optional)',
         'instructions': 'brew install portaudio, then pip install pyaudio'},
    ],
    'git_ops': [
        {'step': 'Install PyGithub',
         'action': 'pip_install',
         'packages': ['PyGithub']},
        {'step': 'Add GitHub credentials',
         'instructions': ('Create a personal access token on github.com → Settings → '
                          'Developer settings → set GITHUB_TOKEN + GITHUB_USERNAME in .env')},
    ],
    'file_ops': [
        {'step': 'Install download/reader packages',
         'action': 'pip_install',
         'packages': ['requests', 'beautifulsoup4', 'yagmail']},
    ],
    'desktop_vision': [
        {'step': 'Install desktop-vision packages',
         'action': 'pip_install',
         'packages': ['pyautogui', 'pillow']},
        {'step': 'Grant Screen Recording permission (macOS)',
         'instructions': ('System Settings → Privacy & Security → Screen Recording '
                          '→ enable for Terminal (or your IDE)')},
    ],
    'wake_free': [
        {'step': 'Enable wake-word gating',
         'instructions': 'Set JARVIS_WAKE_WORD_ENABLE=1 in .env'},
        {'step': 'Install wake-word engine (optional, better accuracy)',
         'action': 'pip_install',
         'packages': ['openwakeword']},
    ],
    'multi_model': [
        {'step': 'Add a second LLM provider key',
         'instructions': ('Set any second key in .env (e.g. GOOGLE_API_KEY alongside '
                          'GROQ_API_KEY) so the MoA panel has 2+ distinct providers')},
    ],
    'ambient_context': [
        {'step': 'Set up Gmail/Calendar credentials',
         'instructions': 'Same as email + calendar setup above (local files work without)'},
    ],
    'code_health': [
        {'step': 'Point Jarvis at a real language server (optional)',
         'instructions': 'Set JARVIS_LSP_CMD to a command template taking {file}, e.g. "pylsp --check {file}"'},
    ],
    'mcp_tools': [
        {'step': 'Add servers to config/mcp_servers.json',
         'instructions': ('Open config/mcp_servers.json → add a server entry '
                          '(command, args, env) → restart Jarvis so the pool connects')},
    ],
    'external_tools': [
        {'step': 'Add a tool manifest to tools_registry/',
         'instructions': ('Drop a JSON manifest (name, description, endpoint, '
                          'auth) into tools_registry/ → it appears on next refresh')},
    ],
    'skills': [
        {'step': 'Create skills from successful workflows',
         'instructions': ('Finish a multi-step task well → Jarvis saves it as '
                          'a reusable skill automatically; nothing to install')},
    ],
}


# --------------------------------------------------------------------- #
#  Public API
# --------------------------------------------------------------------- #

def enabled():
    return os.getenv('JARVIS_CAPABILITY_CHECK', '1') != '0'


# TTL cache for readiness probes (socket/HTTP checks are expensive)
_CHECK_CACHE = {}   # name -> (ok, detail, timestamp)
_CHECK_TTL_S = int(os.getenv('JARVIS_CAP_TTL_S', '120'))


def check(name, _use_cache=True):
    """
    Check one capability by name.  Returns CapabilityStatus.
    Never raises — returns degraded/missing on error.
    Results cached for JARVIS_CAP_TTL_S (default 120s); use
    _use_cache=False to force a fresh probe (e.g. after installs).
    """
    for cap in _CAPABILITIES:
        if cap.name == name:
            if _use_cache and name in _CHECK_CACHE:
                ok, detail, ts = _CHECK_CACHE[name]
                if time.time() - ts < _CHECK_TTL_S:
                    status = 'ready' if ok else 'missing'
                    upgrade = UPGRADE_STEPS.get(name, [])
                    return CapabilityStatus(
                        name=cap.name,
                        description=cap.description,
                        category=cap.category,
                        status=status,
                        detail=detail,
                        upgrade_steps=upgrade,
                    )
            try:
                ok, detail = cap.check()
                _CHECK_CACHE[name] = (ok, detail, time.time())
                status = 'ready' if ok else 'missing'
                upgrade = UPGRADE_STEPS.get(name, [])
                return CapabilityStatus(
                    name=cap.name,
                    description=cap.description,
                    category=cap.category,
                    status=status,
                    detail=detail,
                    upgrade_steps=upgrade,
                )
            except Exception as e:
                return CapabilityStatus(
                    name=cap.name,
                    description=cap.description,
                    category=cap.category,
                    status='degraded',
                    detail=f'Check failed: {e}',
                    upgrade_steps=UPGRADE_STEPS.get(name, []),
                )
    return CapabilityStatus(
        name=name, description='Unknown capability',
        category='unknown', status='missing',
        detail=f'No capability named "{name}"',
    )


def _probe(cap):
    """Run a capability's check fn. Returns (ok, detail, status)."""
    try:
        ok, detail = cap.check()
        return ok, detail, ('ready' if ok else 'missing')
    except Exception as e:
        return False, f'Check failed: {e}', 'degraded'


def list_all(_use_cache=True):
    """
    Check all capabilities.  Returns list of dicts for JSON serialization.
    Uses the TTL cache by default — pass _use_cache=False to force fresh.
    """
    results = []
    for cap in _CAPABILITIES:
        if _use_cache and cap.name in _CHECK_CACHE:
            ok, detail, ts = _CHECK_CACHE[cap.name]
            if time.time() - ts < _CHECK_TTL_S:
                status = 'ready' if ok else 'missing'
            else:
                ok, detail, status = _probe(cap)
                _CHECK_CACHE[cap.name] = (ok, detail, time.time())
        else:
            ok, detail, status = _probe(cap)
            _CHECK_CACHE[cap.name] = (ok, detail, time.time())
        results.append({
            'name': cap.name,
            'description': cap.description,
            'category': cap.category,
            'status': status,
            'detail': detail,
        })
    return results


def assess(task_text):
    """
    Assess what capabilities a task needs.  Keyword-based, no LLM, <1ms.

    Returns AssessmentResult with available/missing capability lists
    and a concrete upgrade plan.
    """
    if not enabled():
        return AssessmentResult(task=task_text)

    task_lower = task_text.lower()
    needed = set()

    for cap in _CAPABILITIES:
        for kw in cap.keywords:
            # Word-boundary match so 'ac' doesn't fire on 'action',
            # 'say' doesn't fire on 'essay', etc.  Trailing ``s?``
            # covers plurals (light/lights, reminder/reminders).
            # Multi-word phrases match as their own span.
            if re.search(r'\b' + re.escape(kw) + r's?\b', task_lower):
                needed.add(cap.name)
                break

    # Always need LLM for any task
    needed.add('llm_reasoning')

    available = []
    missing = []
    for name in sorted(needed):
        cs = check(name)
        if cs.status == 'ready':
            available.append(cs)
        else:
            missing.append(cs)

    # Build upgrade plan for missing capabilities
    upgrade_plan = []
    for cs in missing:
        steps = UPGRADE_STEPS.get(cs.name, [])
        if steps:
            upgrade_plan.extend(steps)
        else:
            upgrade_plan.append({
                'step': f'{cs.description} — manual setup required',
                'instructions': cs.detail,
            })

    return AssessmentResult(
        task=task_text,
        available=available,
        missing=missing,
        upgrade_plan=upgrade_plan,
    )


def format_assessment(result):
    """
    Format an AssessmentResult into a human-readable string.
    """
    lines = []
    if result.available:
        lines.append('✅ Available:')
        for cs in result.available:
            lines.append(f'  • {cs.description} — {cs.detail}')
    if result.missing:
        lines.append('❌ Missing:')
        for cs in result.missing:
            lines.append(f'  • {cs.description} — {cs.detail}')
    if result.upgrade_plan:
        lines.append('')
        lines.append('📋 Upgrade plan:')
        for i, step in enumerate(result.upgrade_plan, 1):
            lines.append(f'  {i}. {step["step"]}')
            if 'instructions' in step:
                lines.append(f'     {step["instructions"]}')
    return '\n'.join(lines)


def format_upgrade_plan(missing_names):
    """
    Format just the upgrade steps for a list of missing capability names.
    """
    lines = []
    for name in missing_names:
        steps = UPGRADE_STEPS.get(name, [])
        if steps:
            for step in steps:
                lines.append(f"• {step['step']}")
                if 'instructions' in step:
                    lines.append(f"  {step['instructions']}")
        else:
            cs = check(name)
            lines.append(f"• {cs.description}: {cs.detail}")
    return '\n'.join(lines)


# --------------------------------------------------------------------- #
#  Capability Expansion Planner
# --------------------------------------------------------------------- #

def expand(missing_names, original_task=''):
    """
    Generate a concrete expansion plan for acquiring missing capabilities.

    Returns a dict:
      {
        'goal_title': str,
        'original_task': str,
        'steps': [{'text': str, 'auto': bool, 'command': str|None,
                    'verify': str, 'capability': str}],
        'auto_packages': [str],   # pip packages that can be auto-installed
        'manual_steps': [str],    # human-readable instructions
      }
    """
    steps = []
    auto_packages = []
    manual_steps = []

    for name in missing_names:
        cap = check(name)
        cap_steps = UPGRADE_STEPS.get(name, [])

        if not cap_steps:
            # No structured steps — fall back to detail text
            step_text = f"Set up {cap.description}: {cap.detail}"
            steps.append({
                'text': step_text,
                'auto': False,
                'command': None,
                'verify': name,
                'capability': name,
            })
            manual_steps.append(step_text)
            continue

        for s in cap_steps:
            is_auto = s.get('action') == 'pip_install' and s.get('packages')
            command = None
            if is_auto:
                pkgs = s['packages']
                command = f"pip install {' '.join(pkgs)}"
                auto_packages.extend(pkgs)

            step_text = s.get('step', s.get('instructions', ''))
            if 'command' in s and not is_auto:
                step_text += f" — run: {s['command']}"

            steps.append({
                'text': step_text,
                'auto': is_auto,
                'command': command,
                'verify': name,
                'capability': name,
            })

            if is_auto:
                pass  # already in auto_packages
            elif 'instructions' in s:
                manual_steps.append(f"{s['step']}: {s['instructions']}")

    # Build goal title
    if len(missing_names) == 1:
        goal_title = f"Acquire capability: {missing_names[0]}"
    else:
        goal_title = f"Acquire {len(missing_names)} capabilities: {', '.join(missing_names[:3])}"

    if original_task:
        goal_title += f" (for: {original_task[:60]})"

    return {
        'goal_title': goal_title,
        'original_task': original_task,
        'steps': steps,
        'auto_packages': auto_packages,
        'manual_steps': manual_steps,
    }


def auto_install(packages):
    """
    Attempt to pip-install a list of packages.

    Returns (ok: bool, output: str).
    Only call when JARVIS_AUTO_UPGRADE=1.
    Never raises.
    """
    if not packages:
        return True, 'No packages to install'
    if os.getenv('JARVIS_AUTO_UPGRADE', '0') != '1':
        return False, 'JARVIS_AUTO_UPGRADE is not enabled'

    import subprocess
    pkgs = list(set(packages))  # dedupe
    cmd = [sys.executable, '-m', 'pip', 'install', '--quiet'] + pkgs
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode == 0:
            # Invalidate cache so probes see freshly-installed packages
            for k in list(_CHECK_CACHE):
                del _CHECK_CACHE[k]
            logger.info("auto-install OK: %s", ', '.join(pkgs))
            return True, result.stdout[-500:] if result.stdout else 'installed'
        else:
            err = (result.stderr or result.stdout or 'unknown error')[-500:]
            logger.warning("auto-install FAILED: %s — %s", ', '.join(pkgs), err)
            return False, err
    except subprocess.TimeoutExpired:
        return False, 'pip install timed out (120s)'
    except Exception as e:
        return False, f'pip install error: {e}'


def format_expansion_plan(plan):
    """
    Format an expansion plan dict into a human-readable string.
    """
    lines = [f"[PLAN] Expansion plan: {plan['goal_title']}", '']
    if plan['auto_packages']:
        lines.append(f"[AUTO] Auto-installable: {', '.join(plan['auto_packages'])}")
        lines.append('')
    for i, step in enumerate(plan['steps'], 1):
        tag = '[AUTO]' if step['auto'] else '[MANUAL]'
        lines.append(f"  {i}. {tag} {step['text']}")
    if plan['manual_steps']:
        lines.append('')
        lines.append("Manual steps required:")
        for s in plan['manual_steps']:
            lines.append(f"  • {s}")
    return '\n'.join(lines)


def expansion_to_steps(plan):
    """
    Convert an expansion plan to step texts suitable for ComplexTaskManager.
    """
    return [s['text'] for s in plan['steps']]


def reassess(task_text):
    """
    Re-assess a task after expansion. Returns the new AssessmentResult.
    """
    return assess(task_text)
