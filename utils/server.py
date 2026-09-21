from flask import Flask, render_template, request, jsonify, Response
from flask_socketio import SocketIO
from config import Config
import os
import re
import json
import threading
import time

app = Flask(__name__, template_folder='../templates', static_folder='../static')
app.config.from_object(Config)
# Force threading mode to avoid Eventlet conflicts with Pygame/Asyncio.
# CORS is restricted to the app's own origin(s) by default so a page from
# any other website cannot cross-site-WebSocket-hijack the control plane
# (every socket handler below can drive the executor with host-user
# privileges, and connect-time pushes personal data).  Widen deliberately
# with JARVIS_ALLOWED_ORIGINS=http://host:5001,http://host:5001 when a
# phone/other origin is an intended client.
_CFG_ORIGINS = os.getenv('JARVIS_ALLOWED_ORIGINS', '').strip()
if _CFG_ORIGINS:
    _ALLOWED_ORIGINS = [o.strip() for o in _CFG_ORIGINS.split(',')
                        if o.strip()]
else:
    _port = getattr(Config, 'PORT', 5001)
    _ALLOWED_ORIGINS = [f"http://127.0.0.1:{_port}",
                        f"http://localhost:{_port}",
                        f"https://127.0.0.1:{_port}",
                        f"https://localhost:{_port}"]
socketio = SocketIO(app, cors_allowed_origins=_ALLOWED_ORIGINS,
                    async_mode='threading')

# Serializes the host-mute read/modify/restore across silent (remote)
# events so the shared mouth.suppress flag is never left stuck True or
# reset early by an overlapping event (see _event_pipeline).
_MUTE_LOCK = threading.RLock()

print("DEBUG: Registered Routes:")
print(app.url_map)

# Register SocketIO with Logger for Web Terminal
from utils.logger import register_socketio
register_socketio(socketio)

# Background thread for system vitals
vitals_thread = None
vitals_running = False

# Track upload time to avoid overwriting user uploads with webcam
last_upload_time = 0

from utils.brain import Brain
from utils.executor import JarvisExecutor
from utils.telegram_bot import JarvisTeleBot

# Initialize Core Modules (single instance, shared everywhere)
brain = Brain()
executor = JarvisExecutor()
_SERVER_START = time.time()

# Auto-RAG: let the brain pull relevant document excerpts from the
# knowledge vault on every query.  Readiness-gated so a broken or
# still-initializing vector stack can NEVER block a live request;
# warm the Librarian up off the request path at boot.
def _vault_query_for_brain(text, n_results=3):
    lib = getattr(executor, '_librarian', None)
    if lib is None or not getattr(lib, 'ready', False):
        return ''
    return lib.query_vault(text, n_results=n_results)
brain.set_knowledge_vault(_vault_query_for_brain)


def _warm_librarian():
    try:
        _ = executor.librarian      # lazy init happens here, offline
    except Exception as e:
        print(f"Librarian warmup failed: {e}")
threading.Thread(target=_warm_librarian, daemon=True,
                  name="librarian-warmup").start()

def _has_socket_clients():
    """True if any SocketIO client is currently connected."""
    try:
        mgr = getattr(socketio, 'server', None)
        if mgr is None:
            return True  # unknown — emit rather than go silent
        rooms = getattr(mgr, 'manager', None)
        if rooms is not None:
            rooms = getattr(rooms, 'rooms', None)
        if isinstance(rooms, dict):
            for ns_rooms in rooms.values():
                if isinstance(ns_rooms, dict):
                    for room, members in ns_rooms.items():
                        if room is None and members:
                            return True
            return False
        return True
    except Exception:
        return True  # fail open for telemetry, not for actions

def emit_system_vitals():
    """Background thread: emit vitals only when a client is connected."""
    global vitals_running
    vitals_running = True
    from utils.logger import logger

    while vitals_running:
        try:
            if _has_socket_clients():
                vitals = executor.tools.get_system_vitals()
                socketio.emit('system_vitals', vitals)
        except Exception as e:
            logger.debug("Vitals emission skipped: %s", e)
        time.sleep(5)

def start_vitals_thread():
    """Start the background vitals monitoring thread."""
    global vitals_thread
    if vitals_thread is None or not vitals_thread.is_alive():
        vitals_thread = threading.Thread(target=emit_system_vitals, daemon=True)
        vitals_thread.start()
        print("System vitals monitoring started")


def _get_ssl_context():
    """
    Persistent self-signed cert so browsers warn only once (vs adhoc which
    regenerates a cert on every boot). Returns None when HTTPS is disabled.
    """
    if not Config.HTTPS:
        return None
    cert_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'certs')
    os.makedirs(cert_dir, exist_ok=True)
    cert_file = os.path.join(cert_dir, 'cert.pem')
    key_file = os.path.join(cert_dir, 'key.pem')
    if not (os.path.exists(cert_file) and os.path.exists(key_file)):
        import subprocess
        try:
            subprocess.run([
                'openssl', 'req', '-x509', '-newkey', 'rsa:2048',
                '-keyout', key_file, '-out', cert_file, '-days', '365', '-nodes',
                '-subj', '/CN=localhost'
            ], check=True, capture_output=True)
            print(f"Generated persistent HTTPS cert in {cert_dir}")
        except Exception as e:
            print(f"HTTPS cert generation failed, falling back to HTTP: {e}")
            return None
    return (cert_file, key_file)


def start_server():
    print(f"Starting server on port {Config.PORT}")
    import random
    
    # Initialize Telegram Bot (Single Instance, shares the main Brain)
    tele_bot = None
    if Config.TELEGRAM_TOKEN:
        try:
            tele_bot = JarvisTeleBot(executor, brain=brain)
            tele_bot.start_polling()
        except Exception as e:
            print(f"Telegram Init Failed: {e}")

    # Route reminder notifications to Telegram.
    # NOTE: Scheduler does NOT auto-start — reminders only fire when
    # the user explicitly asks Jarvis to set one.
    if tele_bot is not None:
        executor.scheduler.on_fire = tele_bot.notify_reminder

    # Proactive notifier → Telegram push for HIGH-priority findings
    # (goal escalations, urgent proactive items).  The notifier itself
    # gates on JARVIS_NOTIFY_TELEGRAM=1; the hook below is a silent
    # no-op when the bot is down or has no chat id yet.
    if tele_bot is not None:
        try:
            from utils.notify import register_telegram_hook
            _bot = tele_bot

            def _push_proactive(text):
                try:
                    import asyncio
                    coro = _bot.application.bot.send_message(
                        chat_id=(_bot._notify_chat_id
                                 or (Config.TELEGRAM_ALLOWED_IDS[0]
                                     if Config.TELEGRAM_ALLOWED_IDS
                                     else None)),
                        text=str(text)[:1000])
                    _bot._send(coro)
                except Exception as e:
                    print(f"Proactive Telegram push skipped: {e}")
            register_telegram_hook(_push_proactive)
        except Exception as e:
            print(f"Proactive Telegram hook skipped: {e}")

    # Recurring automations (cron-style jobs) — persist across restarts
    # and execute real Jarvis actions through the brain + executor.
    # Trusted origin: AUTO_APPROVABLE actions self-approve in critical
    # mode; destructive ones still hold for a human (see approvals.py).
    def _run_automation(job):
        action_text = job.get('action', '')
        command = brain.think(action_text)
        if isinstance(command, dict):
            command['_origin'] = 'recurring'
        # Chained follow-ups (full autonomy): a finished automation may
        # queue ONE follow-up task for itself via the brain's verdict
        # line, so "check X, and if bad, do Y" works.  The flag lives on
        # the JOB (not the brain's command dict) because _fire passes a
        # copy of the job to this callback — _maybe_followup reads the
        # same job object it was given at fire time.
        try:
            from utils.goals import enabled as _goals_on
            if _goals_on():
                job['_allow_followup'] = True
        except Exception:
            pass
        result = executor.execute_command(
            command, brain, original_text=action_text,
            ui_callback=send_to_ui)
        try:
            from utils.notify import get_notifier
            get_notifier().announce(
                f"🤖 Automation ran: {action_text[:80]}",
                priority='low')
        except Exception:
            pass
        return result
    executor.recurring.mouth = executor.mouth
    executor.recurring.on_fire = _run_automation
    if executor.recurring.count() > 0:
        executor.recurring.start()

    # Goal engine watchdog — deadline escalation + auto-completion +
    # opt-in autostart of planned tasks (JARVIS_GOALS_* in .env).
    try:
        from utils.goals import start_watchdog
        from utils.notify import get_notifier
        start_watchdog(brain=brain, notifier=get_notifier())
    except Exception as e:
        print(f"Goals watchdog skipped: {e}")

    # Interrupted-task recovery: tasks parked as PAUSED by a restart
    # auto-resume in the background (full autonomy) instead of waiting
    # for a manual resume click.  Bounded: at most 2 resume, newest
    # first, only when the fleet is actually up, and NEVER while the
    # circuit breaker reports tripped (no LLM = the resumed task would
    # instantly fail every brain.complete step and burn cooldowns).
    try:
        from utils.circuit_breaker import get_breaker
        _fleet_ok = get_breaker().allow_llm_spend()
    except Exception:
        _fleet_ok = True   # breaker itself broken → don't block recovery
    if _fleet_ok:
        try:
            _paused = [t for t in executor.task_manager.get_active_tasks()
                       if str(getattr(t.status, 'value', t.status))
                       == 'paused']
            if _paused:
                _paused.sort(key=lambda t: t.created_at or '',
                             reverse=True)
                _resumed = 0
                for _t in _paused[:2]:
                    try:
                        ok, _msg = executor.task_manager.start_task(_t.id)
                        if ok:
                            _resumed += 1
                            print(f"Autonomy: resumed interrupted task "
                                  f"{_t.id} ({_t.description[:50]})")
                    except Exception as e:
                        print(f"Autonomy: resume of task {_t.id} "
                              f"failed: {e}")
                if _resumed:
                    try:
                        from utils.notify import get_notifier
                        get_notifier().announce(
                            f"🔄 Resumed {_resumed} interrupted background "
                            f"task(s) after restart.", priority='low')
                    except Exception:
                        pass
        except Exception as e:
            print(f"Autonomy: interrupted-task scan skipped: {e}")
    else:
        print("Autonomy: fleet down (breaker tripped) — interrupted "
              "tasks stay parked for manual resume.")

    # Omnichannel gateway hub (Discord polling + Slack webhook/route).
    # Telegram is started above; every adapter activates per its env
    # config and funnels through the same event-bus pipeline.
    try:
        from utils.gateways import get_hub
        get_hub().start(brain)
    except Exception as e:
        print(f"Gateway hub init skipped: {e}")

    # Skill forge maintenance loop — distills skills from successful
    # workflows and patches failing ones (background, bounded).
    try:
        from utils.skill_forge import get_forge
        get_forge().start(brain)
    except Exception as e:
        print(f"Skill forge start skipped: {e}")

    # Proactive engine — background schedule-clash + urgent-mail
    # narration (dashboard always; voice/Telegram per notifier config,
    # deduped daily).  Opt-in via JARVIS_PROACTIVE_NARRATE=1; the scan
    # itself is read-only, announcements go through the rate-limited
    # notifier, and JARVIS_NOTIFY=0 silences everything.
    try:
        from utils.proactive import ProactiveEngine
        from utils.notify import get_notifier
        _proactive = ProactiveEngine(
            secretary=getattr(executor.tools, 'secretary', None),
            notifier=get_notifier())
        _proactive.start()
    except Exception as e:
        print(f"Proactive engine start skipped: {e}")

    # Human-in-the-loop approvals: route hold requests to the dashboard
    executor.approvals.emit_fn = lambda event, payload: socketio.emit(
        event, payload)

    # Pre-execution circuit breaker: validate the runtime once at boot
    from utils.circuit_breaker import get_breaker
    _report = get_breaker().validate(force=True)
    if _report.status == 'tripped':
        msg = ("⚠️ Circuit breaker TRIPPED at startup: " +
               "; ".join(c['detail'] for c in _report.failures()
                         if c['fatal']))
        print(msg)
        socketio.emit('ai_text', {'text': msg})
    elif _report.status == 'degraded':
        print("⚠️ Circuit breaker degraded: " +
              "; ".join(f"{c['name']}: {c['detail']}"
                        for c in _report.failures()))

    # ------------------------------------------------------------------ #
    # Integration gateway: every channel funnels through ONE pipeline
    # ------------------------------------------------------------------ #
    from utils.event_bus import get_bus
    bus = get_bus()

    def _event_pipeline(event):
        """think→execute pipeline shared by dashboard/telegram/webhook/mobile."""
        text = event.text
        is_mobile = (event.source == 'mobile')

        # Mobile skips keyword quick-actions: they execute side effects
        # (missions, app builds) without passing the approvals gate.
        # The brain→executor path below always gates destructive actions.
        if not is_mobile:
            from utils.quick_actions import handle_quick_actions
            if handle_quick_actions(text, executor, brain,
                                    ui_callback=send_to_ui):
                return "Quick action executed."

        image_path = None
        if event.kind == 'photo':
            image_path = event.meta.get('image_path')

        # Mobile (paired phone) is remote + untrusted like Telegram:
        # host stays silent.  The think→execute result is additionally
        # stamped _origin='mobile:<device>' — an origin OUTSIDE
        # TRUSTED_ORIGINS — so the approvals gate holds destructive
        # actions for a human on the dashboard (never a HITL bypass).
        silent = (event.source == 'telegram' or is_mobile
                  or bool(event.meta.get('silent_host')))

        def _stamp(command):
            if is_mobile and isinstance(command, dict):
                try:
                    dev = str(event.meta.get('device_id', '') or '')
                    command['_origin'] = f"mobile:{dev}" if dev \
                        else 'mobile'
                except Exception:
                    pass
            return command

        if silent:
            # Mute the host speakers for the whole remote/silent event.
            # Save/restore the PRIOR flag value under one mute lock: a
            # naive set-True/`finally: set-False` lets two overlapping
            # silent events (or a concurrent agent-loop tool call) leave
            # mouth.suppress stuck True (Jarvis muted for the process
            # lifetime) or reset it while the other is still executing
            # (the host speaks aloud mid-silent).
            with _MUTE_LOCK:
                mouth = getattr(executor, 'mouth', None)
                can_suppress = mouth is not None and \
                    hasattr(mouth, 'suppress')
                was = getattr(mouth, 'suppress', False)
                try:
                    if can_suppress:
                        mouth.suppress = True
                    command = _stamp(brain.think(text,
                                                 image_path=image_path))
                    return executor.execute_command(
                        command, brain, original_text=text,
                        ui_callback=send_to_ui)
                finally:
                    if can_suppress:
                        mouth.suppress = was
        command = _stamp(brain.think(text, image_path=image_path))
        return executor.execute_command(
            command, brain, original_text=text,
            ui_callback=send_to_ui)

    bus.handler = _event_pipeline

    # Idle hibernation: suspend polling loops + caches when quiet
    from utils.hibernation import wire_default_services, get_manager
    try:
        wire_default_services(executor=executor)
        get_manager().start()
    except Exception as e:
        print(f"Hibernation init skipped: {e}")

    # RLM (recursive memory) maintenance daemon: periodic 'sleep' passes
    # that fold events into summaries/abstractions and refresh the world
    # model.  Observation-driven threads handle busy periods; this loop
    # is the idle-time backstop.  JARVIS_RLM=0 disables everything.
    try:
        from utils.rlm import get_rlm
        get_rlm().start_maintenance(brain)
    except Exception as e:
        print(f"RLM maintenance skipped: {e}")

    greetings = ["Online and ready, Sir.", "Systems operational.", "Good to see you again, Sir.", "I am Jarvis, at your service."]
    try:
        greeting = random.choice(greetings)
        executor.mouth.speak(greeting)
    except Exception as e:
        print(f"Startup greeting failed: {e}")
        
    # Disable reloader to prevent double initialization of threads/bot
    ssl_ctx = _get_ssl_context()
    # Bind loopback by default (no LAN exposure of the unauthenticated
    # control plane); set JARVIS_HOST=0.0.0.0 to serve other devices.
    _bind_host = os.getenv('JARVIS_HOST', '127.0.0.1')

    # Graceful port fallback: if the configured port is already taken
    # (e.g. another app squatting on 5001), walk upward to the next free
    # port instead of crashing with "Address already in use".
    import socket as _socket
    def _port_free(port):
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        try:
            s.bind((_bind_host if _bind_host != '0.0.0.0' else '127.0.0.1', port))
            return True
        except OSError:
            return False
        finally:
            s.close()
    _port = Config.PORT
    if not _port_free(_port):
        print(f"⚠ Port {_port} in use — searching for a free port…")
        _orig = _port
        for _candidate in range(_port + 1, _port + 100):
            if _port_free(_candidate):
                _port = _candidate
                break
        print(f"⚠ Falling back to port {_port} (configured {_orig} was busy)")
    # Keep CORS consistent with the port we actually bind (the SocketIO
    # server captured the origin list at import time, so re-point it now).
    if _port != Config.PORT:
        try:
            _eio = socketio.server.eio
            _eio.cors_allowed_origins = [
                o.replace(f':{Config.PORT}', f':{_port}')
                for o in (_eio.cors_allowed_origins or [])]
        except Exception as _e:
            print(f"CORS port re-point skipped: {_e}")

    socketio.run(app, host=_bind_host, port=_port,
                 allow_unsafe_werkzeug=True,
                 ssl_context=ssl_ctx, use_reloader=False)

def send_to_ui(event, data):
    socketio.emit(event, data)

# Phase 2 agent core: give the brain a direct line to the dashboard so
# streamed tokens (ai_text_stream) reach the transcript from EVERY
# surface — voice loop, Telegram, webhook — not just socket handlers.
brain.ui_emit = send_to_ui

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/vitals')
def api_vitals():
    """REST snapshot of system vitals — first-paint fallback for the
    HUD (socket stream stays authoritative once connected)."""
    try:
        return jsonify(executor.tools.get_system_vitals())
    except Exception as e:
        return jsonify({'cpu': 0, 'ram': 0, 'disk': 0,
                        'battery': 100, 'error': str(e)})


@app.route('/api/rlm')
def api_rlm():
    """RLM (recursive memory) snapshot: hierarchy stats, world model and
    the most recent notes at every abstraction level."""
    try:
        from utils.rlm import get_rlm
        return jsonify(get_rlm().dashboard_snapshot())
    except Exception as e:
        from utils.logger import logger
        logger.exception("api_rlm failed")
        return jsonify({'error': 'Internal error'}), 500


@app.route('/api/goals')
def api_goals():
    """Tracked goals: open list with progress + countdowns."""
    try:
        from utils.goals import get_goals
        return jsonify({'goals': get_goals().list(),
                        'stats': get_goals().stats()})
    except Exception as e:
        from utils.logger import logger
        logger.exception("api_goals failed")
        return jsonify({'error': 'Internal error'}), 500


@app.route('/api/capabilities')
def api_capabilities():
    """Capability awareness: what Jarvis can and can't do right now."""
    try:
        from utils.capabilities import list_all
        caps = list_all()
        ready = sum(1 for c in caps if c['status'] == 'ready')
        missing = sum(1 for c in caps if c['status'] != 'ready')
        return jsonify({'capabilities': caps,
                        'ready': ready,
                        'missing': missing,
                        'total': len(caps)})
    except Exception as e:
        from utils.logger import logger
        logger.exception("api_capabilities failed")
        return jsonify({'error': 'Internal error'}), 500


@app.route('/api/capabilities/expand', methods=['POST'])
def api_capabilities_expand():
    """Create a capability expansion plan as a tracked goal."""
    try:
        from utils.capabilities import expand, auto_install, format_expansion_plan
        data = request.get_json() or {}
        cap_names = data.get('capabilities', [])
        do_auto = data.get('auto', False)
        if not cap_names:
            return jsonify({'error': 'No capabilities specified'}), 400
        plan = expand(cap_names)
        install_result = None
        if do_auto and plan['auto_packages']:
            ok, output = auto_install(plan['auto_packages'])
            install_result = {'ok': ok, 'output': output}
        # Create a goal to track this expansion
        goal_id = None
        try:
            from utils.goals import get_goals
            g = get_goals().set(plan['goal_title'], priority='normal')
            if g:
                goal_id = g['id']
        except Exception:
            pass
        return jsonify({
            'plan': plan,
            'formatted': format_expansion_plan(plan),
            'install': install_result,
            'goal_id': goal_id,
        })
    except Exception as e:
        return jsonify({'error': str(e)})


def _is_loopback():
    """True when the request comes from this Mac itself."""
    return (request.remote_addr or '') in (
        '127.0.0.1', '::1', '::ffff:127.0.0.1')


def _dashboard_only():
    """
    Gate for dashboard-only routes (pair/list/revoke, handoff export).

    Loopback (dashboard on the Mac) always passes.  Off-loopback passes
    only with a valid JARVIS_WEBHOOK_KEY (?key= or X-Jarvis-Key) — so
    opening JARVIS_HOST=0.0.0.0 to the LAN doesn't let anyone on the
    network mint pairing codes or read your session.  Note _provider_gate
    passes (None) when NO key is configured — meaning "no gate" — so
    off-loopback without a configured key must still 403 here.
    """
    if _is_loopback():
        return None
    if not os.getenv('JARVIS_WEBHOOK_KEY', ''):
        return jsonify({'ok': False,
                        'error': 'dashboard-only (loopback)'}), 403
    gate = _provider_gate()
    if gate is not None:
        return gate
    return None


def _mobile_auth():
    """
    Bearer auth for paired-phone routes.  Returns (device, error_resp).

    Everything the PHONE calls (chat/sync/handoff POST) needs the
    per-device token.  Dashboard routes (pair/list/revoke) do NOT use
    this — they use _dashboard_only instead.  Redeem consumes the
    pairing code, not a token.  Failures are rate-limited per IP.
    """
    from utils.mobile_link import get_link
    link = get_link()
    ip = (request.remote_addr or '?')
    if link.is_rate_limited(ip):
        return None, (jsonify({'ok': False,
                               'error': 'too many failures — try later'}),
                      429)
    auth = request.headers.get('Authorization', '')
    token = auth[7:].strip() if auth.startswith('Bearer ') else ''
    device = link.verify_token(token) if token else None
    if device is None:
        link.record_auth_failure(ip)
        return None, (jsonify({'ok': False, 'error': 'unauthorized'}),
                      401)
    return device, None


@app.route('/api/mobile/pair', methods=['POST'])
def api_mobile_pair():
    """Dashboard: mint a single-use pairing code (shown as QR/manual)."""
    gate = _dashboard_only()
    if gate is not None:
        return gate
    try:
        from utils.mobile_link import get_link
        data = request.get_json(silent=True) or {}
        code, expires_at = get_link().create_pairing_code(
            label=data.get('label', ''))
        return jsonify({'ok': True, 'code': code,
                        'expires_at': expires_at})
    except Exception as e:
        from utils.logger import logger
        logger.exception("api_mobile_pair failed")
        return jsonify({'ok': False, 'error': 'Internal error'}), 500


@app.route('/api/mobile/redeem', methods=['POST'])
def api_mobile_redeem():
    """Phone: exchange a pairing code for a per-device token (once)."""
    try:
        from utils.mobile_link import get_link
        link = get_link()
        ip = (request.remote_addr or '?')
        if link.is_rate_limited(ip):
            return jsonify({'ok': False,
                            'error': 'too many failures — try later'}), 429
        data = request.get_json(silent=True) or {}
        ok, payload = link.redeem_pairing_code(
            data.get('code', ''), device_name=data.get('device_name', ''))
        if not ok:
            link.record_auth_failure(ip)
            return jsonify({'ok': False, 'error': payload}), 400
        return jsonify({'ok': True, **payload})
    except Exception as e:
        from utils.logger import logger
        logger.exception("api_mobile_redeem failed")
        return jsonify({'ok': False, 'error': 'Internal error'}), 500


@app.route('/api/mobile/devices')
def api_mobile_devices():
    """Dashboard: paired devices (no token material ever leaves)."""
    gate = _dashboard_only()
    if gate is not None:
        return gate
    try:
        from utils.mobile_link import get_link
        return jsonify({'ok': True,
                        'devices': get_link().list_devices()})
    except Exception as e:
        from utils.logger import logger
        logger.exception("api_mobile_devices failed")
        return jsonify({'ok': False, 'error': 'Internal error'}), 500


@app.route('/api/mobile/devices/<device_id>', methods=['DELETE'])
def api_mobile_revoke(device_id):
    """Dashboard: revoke a device token (phone goes dark immediately)."""
    gate = _dashboard_only()
    if gate is not None:
        return gate
    try:
        from utils.mobile_link import get_link
        if get_link().revoke_device(device_id):
            return jsonify({'ok': True, 'revoked': device_id})
        return jsonify({'ok': False, 'error': 'unknown device'}), 404
    except Exception as e:
        from utils.logger import logger
        logger.exception("api_mobile_revoke failed")
        return jsonify({'ok': False, 'error': 'Internal error'}), 500


@app.route('/api/mobile/chat', methods=['POST'])
def api_mobile_chat():
    """
    Phone: run text through the SAME think→execute pipeline (host-silent,
    mobile-stamped so destructive actions hold for a human).  wait=true →
    synchronous reply; else 202 accepted (reply is dropped — the phone
    should poll sync or re-ask with wait=true).
    """
    try:
        device, err = _mobile_auth()
        if err:
            return err
        from utils.event_bus import get_bus
        data = request.get_json(silent=True) or {}
        text = str(data.get('text', '') or '').strip()
        if not text:
            return jsonify({'ok': False, 'error': 'text required'}), 400
        text = text[:4000]
        bus = get_bus()
        event = bus.from_mobile_text(
            text, device_id=device.get('device_id'))
        if data.get('wait'):
            result = bus.publish(event) or 'Done.'
            return jsonify({'ok': True,
                            'result': str(result)[:4000]})
        bus.publish(event, background=True)
        return jsonify({'ok': True, 'accepted': event.id}), 202
    except Exception as e:
        from utils.logger import logger
        logger.exception("api_mobile_chat failed")
        return jsonify({'ok': False, 'error': 'Internal error'}), 500


@app.route('/api/mobile/sync', methods=['POST'])
def api_mobile_sync():
    """
    Phone: push memory events (idempotent by client UUID) + pull hub
    events newer than `cursor`.  Body: {events: [...], cursor: N}.
    """
    try:
        device, err = _mobile_auth()
        if err:
            return err
        from utils.mobile_link import get_link
        data = request.get_json(silent=True) or {}
        inbound = data.get('events') or []
        if not isinstance(inbound, list):
            return jsonify({'ok': False,
                            'error': 'events must be a list'}), 400
        if len(inbound) > 100:
            return jsonify({'ok': False,
                            'error': 'too many events (max 100)'}), 400
        link = get_link()
        outcome = link.apply_sync(device.get('device_id'), inbound)
        events, cursor = link.read_since(data.get('cursor', 0))
        return jsonify({'ok': True, **outcome,
                        'events': events, 'cursor': cursor})
    except Exception as e:
        from utils.logger import logger
        logger.exception("api_mobile_sync failed")
        return jsonify({'ok': False, 'error': 'Internal error'}), 500


@app.route('/api/mobile/handoff', methods=['GET', 'POST'])
def api_mobile_handoff():
    """
    GET (dashboard/phone): export the live session bundle so a
    conversation can move Mac ↔ phone mid-stream.
    POST (Bearer): fold a phone-side bundle back into hub memory.
    """
    try:
        from utils.mobile_link import get_link
        if request.method == 'GET':
            gate = _dashboard_only()
            if gate is not None:
                return gate
            bundle = get_link().export_bundle(brain, executor)
            return jsonify({'ok': True, 'bundle': bundle})
        device, err = _mobile_auth()
        if err:
            return err
        data = request.get_json(silent=True) or {}
        summary = get_link().import_bundle(data.get('bundle'),
                                           device.get('device_id', ''))
        return jsonify({'ok': True, 'summary': summary})
    except Exception as e:
        from utils.logger import logger
        logger.exception("api_mobile_handoff failed")
        return jsonify({'ok': False, 'error': 'Internal error'}), 500


@app.route('/api/tasks')
def api_tasks():
    """Background tasks: active list + recent history + stats."""
    try:
        tm = executor.task_manager
        active = [t.to_dict() for t in tm.get_active_tasks()]
        history = [t.to_dict() for t in tm.get_history(limit=10)]
        return jsonify({'active': active, 'history': history,
                        'stats': tm.get_history_stats()})
    except Exception as e:
        from utils.logger import logger
        logger.exception("api_tasks failed")
        return jsonify({'error': 'Internal error'}), 500


@app.route('/api/tasks/<task_id>/pause', methods=['POST'])
def api_task_pause(task_id):
    try:
        return jsonify({'ok': True,
                        'message': executor.task_manager.pause_task(
                            task_id)})
    except Exception as e:
        from utils.logger import logger
        logger.exception("api_task_pause failed")
        return jsonify({'ok': False, 'error': 'Internal error'}), 500


@app.route('/api/tasks/<task_id>/resume', methods=['POST'])
def api_task_resume(task_id):
    try:
        return jsonify({'ok': True,
                        'message': executor.task_manager.resume_task(
                            task_id)})
    except Exception as e:
        from utils.logger import logger
        logger.exception("api_task_resume failed")
        return jsonify({'ok': False, 'error': 'Internal error'}), 500


@app.route('/api/tasks/<task_id>/cancel', methods=['POST'])
def api_task_cancel(task_id):
    try:
        return jsonify({'ok': True,
                        'message': executor.task_manager.cancel_task(
                            task_id)})
    except Exception as e:
        from utils.logger import logger
        logger.exception("api_task_cancel failed")
        return jsonify({'ok': False, 'error': 'Internal error'}), 500


# --- Agent-loop inspector (DEV drawer) ----------------------------------- #

def _agent_loop():
    """Process-wide AgentLoop for stop/config (lazily built from brain)."""
    from utils.agent_loop import get_agent_loop
    return get_agent_loop(brain)


@app.route('/api/agent', methods=['GET', 'POST'])
def api_agent():
    """Config view for the live agent loop (mode / step cap / model / streaming)."""
    loop = _agent_loop()
    if request.method == 'GET':
        mode = os.getenv('JARVIS_AGENT_MODE', 'auto')
        return jsonify({
            'mode': mode,
            'max_steps': loop.max_steps,
            'stream': os.getenv('JARVIS_AGENT_STREAM', '1') != '0',
            'model': os.getenv('JARVIS_AGENT_MODEL', ''),
            'running': getattr(loop, '_running', False),
        })
    data = request.get_json(silent=True) or {}
    if 'mode' in data:
        os.environ['JARVIS_AGENT_MODE'] = str(data['mode'])
    if 'max_steps' in data:
        try:
            loop.max_steps = max(1, int(data['max_steps']))
            os.environ['JARVIS_AGENT_MAX_STEPS'] = str(loop.max_steps)
        except (TypeError, ValueError):
            pass
    if 'model' in data:
        os.environ['JARVIS_AGENT_MODEL'] = str(data['model']).strip()
    if 'stream' in data:
        os.environ['JARVIS_AGENT_STREAM'] = '1' if data['stream'] else '0'
    return jsonify({'ok': True})


@socketio.on('agent_stop')
def handle_agent_stop(data):
    """Cooperative abort of the in-flight agent run (DEV drawer STOP)."""
    loop = _agent_loop()
    loop.stop()
    socketio.emit('agent_status', {'message': 'stop signal sent'})


@app.route('/health')
def health():
    """Lightweight health check for uptime monitors / scripts."""
    import datetime
    from utils.circuit_breaker import get_breaker
    from utils.budget import get_budget
    fleet = {}
    try:
        from utils.llm import get_router
        d = get_router().describe()
        fleet = {'providers': [p['name'] for p in d['providers']],
                 'cooldowns': d['cooldowns']}
    except Exception:
        pass
    return jsonify({
        'status': 'ok',
        'service': 'jarvis',
        'time': datetime.datetime.now().isoformat(timespec='seconds'),
        'uptime_s': round(time.time() - _SERVER_START, 1),
        'brain_active': brain.active if hasattr(brain, 'active') else False,
        'needs_onboarding': _needs_onboarding(),
        'llm_fleet': fleet,
        'circuit_breaker': get_breaker().report.status,
        'budget': get_budget().status(),
    })


_LOG_SECRET_PATTERNS = re.compile(
    r'((sk-[A-Za-z0-9_-]{6})[A-Za-z0-9_-]+|'
    r'((?:api[_-]?key|token|secret|password)[=: ]+\S+))',
    re.IGNORECASE)


@app.route('/api/logs')
def api_logs():
    """Sanitized tail of jarvis.log for the ops-deck ticker."""
    n = request.args.get('n', default=6, type=int)
    try:
        n = max(1, min(int(n or 6), 30))
    except (TypeError, ValueError):
        n = 6
    log_path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'jarvis.log')
    lines = []
    try:
        with open(log_path, 'r', errors='replace') as f:
            lines = f.readlines()[-n:]
        lines = [_LOG_SECRET_PATTERNS.sub('[REDACTED]', l).rstrip()[-220:]
                 for l in lines if l.strip()]
    except Exception as e:
        lines = [f'log unavailable: {e}']
    return jsonify({'lines': lines})


def _needs_onboarding():
    """True until ANY LLM provider is configured (first-run signal)."""
    try:
        router = getattr(brain, 'router', None)
        return not (router and len(router.providers) > 0)
    except Exception:
        return True


@app.route('/onboarding')
def onboarding():
    """First-run setup wizard: bring your own key, go live in a minute."""
    return render_template('onboarding.html')


# --------------------------------------------------------------------- #
# Jarvis Lite PWA (Phase 0 remote) — installable phone client.
# /mobile is the app shell; manifest + service worker + icon are static.
# The shell itself carries no auth: pairing codes are redeemed from the
# phone (single-use, 10-min TTL) and the per-device Bearer token lives
# only in the phone's localStorage, never in a URL or cookie.
# --------------------------------------------------------------------- #

@app.route('/mobile')
def mobile_app():
    """Jarvis Lite phone UI (Add to Home Screen → standalone)."""
    return render_template('mobile.html')


@app.route('/mobile-manifest.json')
def mobile_manifest():
    """PWA manifest: name, icons, standalone display."""
    return app.send_static_file('mobile-manifest.json')


@app.route('/mobile-sw.js')
def mobile_service_worker():
    """Service worker: cache-first app shell, never caches /api/*."""
    resp = app.send_static_file('mobile-sw.js')
    # A new SW version must activate promptly, not stick behind a cache.
    resp.headers['Cache-Control'] = 'no-cache'
    # SW scope is derived from its URL — serve from root so it can
    # control /mobile.
    resp.headers['Service-Worker-Allowed'] = '/'
    return resp


# --------------------------------------------------------------------- #
# BYOK provider management — bring any key at runtime, no restart.
# Mutations require the webhook key when JARVIS_WEBHOOK_KEY is set.
# --------------------------------------------------------------------- #

def _provider_gate():
    """Return an error response when the mutation gate rejects."""
    import os
    required = os.getenv('JARVIS_WEBHOOK_KEY', '')
    if not required:
        return None
    supplied = (request.args.get('key', '')
                or request.headers.get('X-Jarvis-Key', ''))
    if supplied != required:
        return jsonify({'ok': False, 'error': 'unauthorized'}), 401
    return None


@app.route('/api/providers')
def providers_status():
    from utils.llm import get_router
    payload = {'ok': True, **get_router().describe()}
    try:
        from utils.mcp_client import get_pool
        payload['mcp'] = get_pool().status()
    except Exception:
        payload['mcp'] = []
    return jsonify(payload)


@app.route('/api/mcp/reload', methods=['POST'])
def mcp_reload():
    """Re-read config/mcp_servers.json (restarts all MCP servers)."""
    gate = _provider_gate()
    if gate is not None:
        return gate
    try:
        from utils.mcp_client import reload_pool
        pool = reload_pool()
        pool.ensure_started()
        return jsonify({'ok': True, 'servers': pool.status()})
    except Exception as e:
        from utils.logger import logger
        logger.exception("mcp_reload failed")
        return jsonify({'ok': False, 'error': 'Internal error'}), 500


@app.route('/api/providers', methods=['POST'])
def providers_set():
    gate = _provider_gate()
    if gate is not None:
        return gate
    payload = request.get_json(silent=True) or {}
    provider = str(payload.get('provider', '')).strip().lower()
    api_key = str(payload.get('api_key', '')).strip()
    if not provider or not api_key:
        return jsonify({'ok': False,
                        'error': 'provider and api_key required'}), 400
    from utils.llm.keystore import ENV_MAP
    if provider not in ENV_MAP:
        return jsonify({'ok': False,
                        'error': f"unknown provider '{provider}'"}), 400
    from utils.llm import get_router
    router = get_router()
    router.keystore.set(provider, api_key)
    status = router.refresh()
    return jsonify({'ok': True, 'provider': provider,
                    'fleet': [p['name'] for p in status['providers']]})


@app.route('/api/providers/<provider>', methods=['DELETE'])
def providers_delete(provider):
    gate = _provider_gate()
    if gate is not None:
        return gate
    from utils.llm.keystore import ENV_MAP
    provider = provider.strip().lower()
    if provider not in ENV_MAP:
        return jsonify({'ok': False,
                        'error': f"unknown provider '{provider}'"}), 400
    from utils.llm import get_router
    router = get_router()
    router.keystore.delete(provider)
    status = router.refresh()
    return jsonify({'ok': True, 'removed': provider,
                    'note': 'env-var keys reappear on restart; '
                            'remove them from .env to disable fully',
                    'fleet': [p['name'] for p in status['providers']]})


@app.route('/webhook', methods=['POST'])
def webhook_gateway():
    """
    Integration gateway for external systems (curl, IFTTT, n8n, IDE
    plugins…).  POST JSON {"text": "...", "wait": true|false}.

    Auth: ?key=SECRET or X-Jarvis-Key header matching JARVIS_WEBHOOK_KEY.
    wait=true → synchronous {"ok":true,"result":"…"}; else 202 accepted.
    """
    from utils.event_bus import get_bus
    bus = get_bus()
    if bus.webhook_key:
        supplied = (request.args.get('key', '')
                    or request.headers.get('X-Jarvis-Key', ''))
        if supplied != bus.webhook_key:
            return jsonify({'ok': False, 'error': 'unauthorized'}), 401

    payload = request.get_json(silent=True)
    if payload is None:
        payload = {'text': request.form.get('text', '')}
    wait = bool(payload.get('wait'))

    event = bus.from_webhook(payload)
    if wait:
        result = bus.publish(event) or 'Done.'
        return jsonify({'ok': True, 'result': str(result)[:4000]})
    bus.publish(event, background=True)
    return jsonify({'ok': True, 'accepted': event.id}), 202


@app.route('/gateway/slack', methods=['POST'])
def slack_gateway():
    """
    Slack Events API + slash-command receiver (part of the omnichannel
    gateway hub).  Handles the url_verification handshake, verifies the
    request token when configured, and routes the message through the
    shared think→execute pipeline — replies go out over the configured
    incoming-webhook URL.
    """
    from utils.gateways import get_hub
    hub = get_hub()
    if hub.slack is None:
        return jsonify({'ok': False,
                        'error': 'slack gateway not configured'}), 503
    payload = request.get_json(silent=True)
    if payload is None:
        payload = {k: v for k, v in request.form.items()}
    status, body = hub.slack.handle_http(payload or {})
    if body and body.startswith('{'):
        return body, status
    return Response(body, status=status, mimetype='text/plain')


@app.route('/upload', methods=['POST'])
def upload_file():
    from werkzeug.utils import secure_filename
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400

    # Validate file type (images only)
    _ALLOWED_UPLOAD = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'}
    import uuid
    orig = secure_filename(file.filename or 'upload')
    ext = os.path.splitext(orig)[1].lower()
    if ext not in _ALLOWED_UPLOAD:
        return jsonify({'error': f'File type {ext} not allowed. Use: {", ".join(_ALLOWED_UPLOAD)}'}), 400

    # Size check (10 MB max)
    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(0)
    if size > 10 * 1024 * 1024:
        return jsonify({'error': 'File too large (10 MB max)'}), 400

    if file:
        # Save with UUID prefix to prevent traversal/overwrite
        safe_name = f"upload_{uuid.uuid4().hex[:12]}{ext}"
        static_path = os.path.join(app.static_folder, safe_name)
        file.save(static_path)

        # Update last upload time
        global last_upload_time
        last_upload_time = time.time()

        # Emit photo_taken event so dashboard updates instantly
        socketio.emit('photo_taken', {
            'photo_url': f'/static/{safe_name}',
            'timestamp': time.time()
        })

        return jsonify({'success': True, 'message': 'File uploaded successfully',
                        'filename': safe_name}), 200

@app.route('/api/intel')
def api_intel():
    """LIVE INTEL — one snapshot of real data from the connected public-API
    fleet for the console's intel strip. Cached ~5 min server-side so the
    strip is fast: no-key paths run live, key-gated ones report NEEDS_KEY.
    Never raises: every cell degrades to {'ok': False, 'why': ...}.
    """
    import time as _t
    now = _t.time()
    cache = getattr(api_intel, '_cache', None)
    if cache and (now - cache[0]) < 300:
        return jsonify(cache[1])

    def cell(fn, *a, **k):
        try:
            v = fn(*a, **k)
            if not v:
                return {'ok': False, 'why': 'empty'}
            t = str(v)[:220]
            if 'unavailable' in t.lower():
                return {'ok': False, 'why': t[:80]}
            return {'ok': True, 'text': t}
        except Exception as exc:  # noqa: BLE001 — degrade, never 500
            return {'ok': False, 'why': str(exc)[:80]}

    out = {'ts': int(now), 'cells': {}}
    try:
        from utils.weather_api import weather_report
        out['cells']['weather'] = cell(weather_report, city='London')
    except Exception as exc:
        out['cells']['weather'] = {'ok': False, 'why': str(exc)[:80]}
    try:
        from utils.info_api import crypto_price, space_apod
        out['cells']['crypto'] = cell(crypto_price, 'bitcoin')
        out['cells']['space'] = cell(space_apod)
    except Exception as exc:
        out['cells']['crypto'] = {'ok': False, 'why': str(exc)[:80]}
        out['cells']['space'] = out['cells'].get('space', {'ok': False, 'why': 'n/a'})
    try:
        from utils.flight_api import flights_near
        out['cells']['flights'] = cell(flights_near, 51.47, -0.4543, 80)
    except Exception as exc:
        out['cells']['flights'] = {'ok': False, 'why': str(exc)[:80]}
    try:
        from utils.ext_api import research
        out['cells']['papers'] = cell(research, 'large language models')
    except Exception as exc:
        out['cells']['papers'] = {'ok': False, 'why': str(exc)[:80]}

    api_intel._cache = (now, out)
    return jsonify(out)

@app.route('/api/vision', methods=['POST'])
def api_vision():
    """Describe an image using a vision-capable model from the fleet.

    Accepts JSON: ``{"image": "<data:image/png;base64,...> | /static/foo.png>",
    "prompt": "<optional instructions>"}``. Routes through the router with
    ``require={'vision'}`` so it lands on a vision model (e.g. Mimo 2.5, Gemini,
    Claude). Returns ``{"ok": true, "text": ...}``.
    """
    import base64 as _b64
    try:
        body = request.get_json(silent=True) or {}
    except Exception:
        body = {}
    image = (body.get('image') or '').strip()
    prompt = (body.get('prompt') or '').strip() or \
        'Describe exactly what is visible in this image in a few clear sentences.'
    if not image:
        return jsonify({'ok': False, 'error': 'No image provided'}), 400

    data_url = image
    if image.startswith('/'):
        # Served static path -> turn into a data URL
        rel = image.lstrip('/')
        if rel.startswith('static/'):
            rel = rel[len('static/'):]
        p = os.path.join(app.static_folder, rel.split('?')[0])
        if not os.path.exists(p):
            return jsonify({'ok': False, 'error': 'Image not found'}), 404
        mime = 'image/png' if p.lower().endswith('.png') else 'image/jpeg'
        with open(p, 'rb') as fh:
            data_url = f"data:{mime};base64,{_b64.b64encode(fh.read()).decode()}"

    if not data_url.startswith('data:image'):
        return jsonify({'ok': False, 'error': 'Unsupported image source'}), 400

    from utils.llm import get_router
    messages = [{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": data_url}},
    ]}]
    try:
        result = get_router().chat(messages, require={'vision'}, max_tokens=400)
        text = (result.text or '').strip() or '(vision model returned no text)'
        return jsonify({'ok': True, 'text': text,
                        'provider': result.provider, 'model': result.model})
    except Exception as exc:  # noqa: BLE001 — surface fleet failures to the UI
        return jsonify({'ok': False, 'error': str(exc)[:400]}), 502

@app.route('/upload_knowledge', methods=['POST'])
def upload_knowledge():
    from werkzeug.utils import secure_filename
    from utils.logger import logger
    if 'files' not in request.files:
        return jsonify({'error': 'No files uploaded'}), 400

    files = request.files.getlist('files')
    results = []
    full_text_content = ""

    for file in files:
        if file.filename == '':
            continue

        try:
            # Save temporarily with safe name
            import uuid
            orig = secure_filename(file.filename or 'upload')
            temp_path = os.path.join(app.static_folder, f'temp_{uuid.uuid4().hex[:8]}_{orig}')
            file.save(temp_path)

            # store=True writes to the knowledge vault (STUDY PERMANENTLY);
            # store=False keeps only the 30-min feed memory.
            store = 'study' in request.form
            text_content = executor.librarian.ingest_file(temp_path, store=store)
            full_text_content += f"\nFILE: {orig}\nCONTENT:\n{text_content}\n"

            logger.info("Ingested %s: %d chars", orig, len(text_content))
            results.append(orig)

            # Clean up
            if os.path.exists(temp_path):
                os.remove(temp_path)

        except Exception as e:
            logger.exception("Error ingesting %s", file.filename)
            return jsonify({'error': 'File ingestion failed'}), 500

    if results:
        # Inject into Brain's short-term memory
        brain.inject_knowledge(full_text_content)
        
        # TTL is 30 minutes (Config/Brain MEMORY_TTL)
        msg = f"I have read {len(results)} new documents, Sir. I will remember this for 30 minutes."
        try:
            executor.mouth.speak(msg)
        except:
            pass
        return jsonify({'success': True, 'message': msg}), 200
    else:
        return jsonify({'error': 'No valid files processed'}), 400

# Server routes and logic below

@socketio.on('connect')
def handle_connect():
    from utils.logger import logger
    # Auth gate: require JARVIS_WEBHOOK_KEY when set, or verify Origin
    webhook_key = getattr(Config, 'WEBHOOK_KEY', None) or os.getenv('JARVIS_WEBHOOK_KEY')
    if webhook_key:
        # Check auth from query param or first message header
        from flask import request as _req
        client_key = _req.args.get('key', '')
        if client_key != webhook_key:
            logger.warning("SocketIO connect rejected: invalid webhook key")
            return False  # reject connection
    logger.info("Client connected")
    send_to_ui('status', {'message': 'Jarvis Online'})
    
    # Start system vitals monitoring
    start_vitals_thread()
    
    # Send initial vitals immediately
    try:
        initial_vitals = executor.tools.get_system_vitals()
        socketio.emit('system_vitals', initial_vitals)
    except:
        pass
    
    # Send initial home status
    socketio.emit('home_update', executor.smarthome.get_devices_json())
    
    # Send initial todo list
    socketio.emit('todo_update', {'items': executor.tasks.items_json()})
    
    # Send initial notes list
    socketio.emit('notes_update', {'items': executor.notes.items_json()})
    
    # Send initial memory stats
    socketio.emit('memory_update', {
        'items': executor.episodic.items_json(20),
        'stats': executor.episodic.stats()
    })
    
    # Send relationship data
    socketio.emit('relationships_update', {
        'people': executor.relationships.items_json()
    })

    # Send active/recent tasks
    tm = executor.task_manager
    recent_tasks = tm.get_recent_tasks(10)
    socketio.emit('tasks_list', {'tasks': [t.to_dict() for t in recent_tasks]})

VISION_KEYWORDS = ["look at", "what's on my screen", "read screen", "see this"]


def _process_user_text(text):
    """
    Shared pipeline for user text from any channel (dashboard, voice, buttons).
    1. Confirm what the user said
    2. Handle vision triggers (screenshot or recently-uploaded photo)
    3. Check keyword quick actions (missions, dev studio, git, email, GUI)
    4. Fall through to the Brain
    """
    from utils.logger import logger
    if not text or not text.strip():
        return
    text = text.strip()
    logger.info(f"Received text: {text}")

    send_to_ui('status', {'message': f'You said: {text}'})

    # Vision triggers: analyze the screen, or honor a recently uploaded photo
    image_path = None
    if any(keyword in text.lower() for keyword in VISION_KEYWORDS):
        send_to_ui('status', {'message': 'Analyzing screen...'})
        executor.mouth.speak("One moment, looking at your screen.")
        uploaded_path = os.path.join(app.static_folder, 'webcam_capture.jpg')
        if time.time() - last_upload_time < Config.UPLOAD_PROTECTION_SECONDS and os.path.exists(uploaded_path):
            image_path = uploaded_path
            send_to_ui('status', {'message': 'Analyzing your uploaded photo...'})
        else:
            image_path = executor.tools.take_screenshot()

    # Keyword quick actions (missions, auto-build, dev studio, git, email, GUI)
    from utils.quick_actions import handle_quick_actions
    if handle_quick_actions(text, executor, brain, ui_callback=send_to_ui):
        return

    # Normal Brain processing
    command = brain.think(text, image_path=image_path)
    executor.execute_command(
        command,
        brain,
        original_text=text,
        ui_callback=send_to_ui
    )


def _safe_process(data):
    from utils.logger import logger
    try:
        _process_user_text(data.get('text', ''))
    except Exception as e:
        logger.critical(f"Critical Error processing text: {e}", exc_info=True)
        try:
            executor.mouth.speak("Sir, I encountered a critical error. Checking systems.")
        except Exception:
            pass
        send_to_ui('ai_text', {'text': f"Critical System Error: {str(e)}"})


@socketio.on('process_text')
def handle_text(data):
    _safe_process(data)


@socketio.on('user_input')
def handle_user_input(data):
    # Dashboard buttons (device toggles, capture photo) emit 'user_input'
    _safe_process(data)


@socketio.on('todo_action')
def handle_todo_action(data):
    """Direct to-do manipulation from the dashboard panel (no LLM needed)."""
    action = (data or {}).get('action', '')
    text = (data or {}).get('text', '')
    result = ""
    if action == 'add':
        result = executor.tasks.add(text)
    elif action == 'done':
        result = executor.tasks.done(text)
    elif action == 'remove':
        result = executor.tasks.remove(text)
    elif action == 'clear':
        result = executor.tasks.clear()
    else:
        result = "Unknown todo action."
    socketio.emit('todo_update', {'items': executor.tasks.items_json()})
    socketio.emit('ai_text', {'text': result})


@socketio.on('note_action')
def handle_note_action(data):
    """Direct note manipulation from the dashboard panel (no LLM needed)."""
    action = (data or {}).get('action', '')
    text = (data or {}).get('text', '')
    result = ""
    if action == 'add':
        result = executor.notes.save(text)
    elif action == 'remove':
        result = executor.notes.remove(text)
    else:
        result = "Unknown note action."
    socketio.emit('notes_update', {'items': executor.notes.items_json()})
    socketio.emit('ai_text', {'text': result})


@socketio.on('snooze_reminder')
def handle_snooze_reminder(data):
    """Dashboard Snooze button: reschedule a fired reminder."""
    rid = (data or {}).get('id')
    minutes = (data or {}).get('minutes', 10)
    result = executor.scheduler.snooze(rid, minutes)
    socketio.emit('ai_text', {'text': result})

@socketio.on('memory_action')
def handle_memory_action(data):
    """Direct memory manipulation from the dashboard panel."""
    action = (data or {}).get('action', '')
    text = (data or {}).get('text', '')
    category = (data or {}).get('category', 'fact')
    result = ""
    if action == 'remember':
        mem = executor.episodic.remember(text, category=category, source='dashboard')
        result = f"Remembered: {text[:60]}" if mem else "Failed to store."
    elif action == 'search':
        results = executor.episodic.search(text, limit=10)
        result = f"Found {len(results)} memories." if results else "No memories found."
    elif action == 'forget':
        count = executor.episodic.forget(keyword=text)
        result = f"Forgot {count} memories." if count else "No memories found."
    else:
        result = "Unknown memory action."
    socketio.emit('memory_update', {
        'items': executor.episodic.items_json(20),
        'stats': executor.episodic.stats()
    })
    socketio.emit('ai_text', {'text': result})


@socketio.on('relationship_action')
def handle_relationship_action(data):
    """Direct relationship manipulation from the dashboard."""
    action = (data or {}).get('action', '')
    name = (data or {}).get('name', '')
    rel = (data or {}).get('relationship', '')
    result = ""
    if action == 'add':
        person = executor.relationships.add_person(name, relationship_type=rel)
        result = f"Added {name} ({rel})."
    elif action == 'info':
        person = executor.relationships.get_person(name)
        if person:
            result = f"{person['name']}: {person.get('relationship', '?')}"
            if person.get('hobbies'): result += f" | Likes: {', '.join(person['hobbies'][:3])}"
        else:
            result = f"I don't have info about {name}."
    elif action == 'gifts':
        suggestions = executor.relationships.get_gift_suggestions(name)
        result = f"Gift ideas for {name}: {', '.join(suggestions[:3])}"
    elif action == 'remove':
        result = executor.relationships.remove_person(name)
    else:
        result = "Unknown relationship action."
    socketio.emit('relationships_update', {
        'people': executor.relationships.items_json()
    })
    socketio.emit('ai_text', {'text': result})


@socketio.on('meeting_action')
def handle_meeting_action(data):
    """Meeting mode control from dashboard."""
    action = (data or {}).get('action', '')
    # Live transcript rides a dedicated event into the MTG drawer — never
    # the chat feed, which the client polls every 5s while recording.
    if action == 'transcript':
        text = executor.meeting.get_live_transcript() or ''
        socketio.emit('meeting_transcript', {'text': text})
        return
    if action == 'start':
        result = executor.meeting.start_recording()
    elif action == 'stop':
        result = executor.meeting.stop_recording()
    else:
        result = "Unknown meeting action."
    socketio.emit('ai_text', {'text': result})


@socketio.on('privacy_action')
def handle_privacy_action(data):
    """Privacy settings from dashboard → structured snapshot for SEC drawer."""
    action = (data or {}).get('action', '')
    key = (data or {}).get('key', '')
    value = (data or {}).get('value', '')
    if action not in ('get', 'update', 'update_trust', 'summary', 'audit'):
        socketio.emit('ai_text', {'text': 'Unknown privacy action.'})
        return

    result = ''
    if action == 'update':
        result = executor.privacy.update_setting(key, value)
    elif action == 'update_trust':
        result = executor.privacy.update_trust(key, value)
    elif action in ('get', 'summary'):
        result = executor.privacy.get_trust_summary()

    # Read results belong in the drawer, not dumped into the chat feed.
    socketio.emit('privacy_update', {
        'settings': executor.privacy.get_all_settings(),
        'audit': executor.privacy.get_audit_log(limit=10),
        'summary': result,
    })
    if action in ('update', 'update_trust') and result:
        socketio.emit('ai_text', {'text': result})


@socketio.on('fridge_action')
def handle_fridge_action(data):
    """Fridge vision from dashboard."""
    action = (data or {}).get('action', '')
    if action == 'analyze':
        result_data = executor.fridge.analyze_fridge()
        if result_data.get('error'):
            result = result_data['error']
        else:
            ingredients = result_data.get('ingredients', [])
            recipes = result_data.get('recipes', [])
            shopping = result_data.get('shopping_list', [])
            lines = [f"Found {len(ingredients)} ingredients: {', '.join(ingredients[:10])}."]
            if recipes:
                lines.append(f"Suggested {len(recipes)} recipes:")
                for r in recipes[:3]:
                    name = r.get('name', 'Recipe') if isinstance(r, dict) else str(r)[:60]
                    lines.append(f"  🍳 {name}")
            if shopping:
                lines.append(f"Shopping: {', '.join(shopping[:8])}")
            result = "\n".join(lines)
    else:
        result = "Unknown fridge action."
    socketio.emit('ai_text', {'text': result})


@socketio.on('complex_task_action')
def handle_complex_task_action(data):
    """Dashboard controls for complex tasks: pause, resume, cancel."""
    action = (data or {}).get('action', '')
    task_id = (data or {}).get('task_id', '')
    tm = executor.task_manager
    result = ''
    if action == 'pause':
        result = tm.pause_task(task_id)
    elif action == 'resume':
        result = tm.resume_task(task_id)
    elif action == 'cancel':
        result = tm.cancel_task(task_id)
    elif action == 'list':
        tasks = tm.get_active_tasks()
        socketio.emit('tasks_list', {'tasks': [t.to_dict() for t in tasks]})
        return
    elif action == 'history':
        tasks = tm.get_history(30)
        stats = tm.get_history_stats()
        socketio.emit('tasks_history', {
            'tasks': [t.to_dict() for t in tasks],
            'stats': stats,
        })
        return
    else:
        result = 'Unknown task action.'
    socketio.emit('ai_text', {'text': result})


@socketio.on('goals_action')
def handle_goals_action(data):
    """Dashboard controls for tracked goals: list/set/done/drop."""
    action = (data or {}).get('action', '')
    try:
        from utils.goals import get_goals
        store = get_goals()
    except Exception as e:
        socketio.emit('ai_text', {'text': f"Goals unavailable: {e}"})
        return
    if action == 'list':
        socketio.emit('goals_update', {'goals': store.list(),
                                       'stats': store.stats()})
        return
    if action == 'set':
        g = store.set((data or {}).get('title', ''),
                      deadline=str((data or {}).get('deadline') or ''),
                      priority=(data or {}).get('priority'))
        socketio.emit('ai_text', {'text':
                                  f"Goal [{g['id']}] tracked: {g['title']}."
                                  if g else "Goals are disabled."})
    elif action in ('done', 'drop'):
        ref = str((data or {}).get('goal', '')).strip().lower()
        matched = None
        for g in store.list(include_done=False):
            if ref in g.get('id', '').lower() \
                    or (ref and ref in g.get('title', '').lower()):
                matched = g
                break
        if matched is None:
            socketio.emit('ai_text',
                          {'text': f"No open goal matches '{ref}'."})
            return
        if action == 'done':
            store.complete(matched['id'])
            socketio.emit('ai_text',
                          {'text': f"Goal complete: {matched['title']}."})
        else:
            store.drop(matched['id'])
            socketio.emit('ai_text',
                          {'text': f"Goal dropped: {matched['title']}."})
    else:
        socketio.emit('ai_text', {'text': 'Unknown goals action.'})
        return
    socketio.emit('goals_update', {'goals': store.list(),
                                   'stats': store.stats()})


@socketio.on('automation_action')
def handle_automation_action(data):
    """Dashboard controls for recurring automations: list/add/cancel."""
    action = (data or {}).get('action', '')
    ra = executor.recurring
    if action == 'list':
        _emit_automations()
        return
    if action == 'run_now':
        result = ra.run_now((data or {}).get('keyword', ''))
        _emit_automations()
        socketio.emit('ai_text', {'text': result})
        return
    if action == 'add':
        result = ra.add((data or {}).get('text', ''))
    elif action == 'cancel':
        result = ra.remove((data or {}).get('keyword', ''))
    else:
        result = 'Unknown automation action.'
    socketio.emit('ai_text', {'text': result})
    _emit_automations()


def _emit_automations():
    """Push the full automations list to the dashboard."""
    jobs = []
    try:
        with executor.recurring._lock:
            jobs = [dict(j) for j in sorted(executor.recurring._jobs,
                                            key=lambda x: x['id'])]
        for j in jobs:
            j['describe'] = executor.recurring._describe(j)
            try:
                nxt = executor.recurring._next_fire(j)
                j['next'] = executor.recurring.describe_next(j)
                j['next_ts'] = nxt.timestamp() if nxt else None
            except Exception:
                j['next'] = ''
                j['next_ts'] = None
    except Exception as e:
        print(f"Automations emit failed: {e}")
    socketio.emit('automations_update', {'jobs': jobs})


@socketio.on('skills_action')
def handle_skills_action(data):
    """Dashboard controls for saved skills: list/run/delete."""
    action = (data or {}).get('action', '')
    reg = executor.skills
    if action == 'list':
        socketio.emit('skills_list', {'skills': reg.list_skills()})
        return
    name = (data or {}).get('name', '')
    if action == 'run':
        params = (data or {}).get('params') or {}
        try:
            res = reg.run(name, params=params, coder=executor.coder)
            result = res['output'] if res['success'] else \
                f"Skill '{name}' failed:\n{res['output'][:400]}"
        except Exception as e:
            result = f"Skill run failed: {e}"
    elif action == 'delete':
        result = reg.delete_skill(name)
        socketio.emit('skills_list', {'skills': reg.list_skills()})
        socketio.emit('ai_text', {'text': result})
        return
    else:
        result = 'Unknown skill action.'
    socketio.emit('ai_text', {'text': result})


@socketio.on('tools_action')
def handle_tools_action(data):
    """Dashboard controls for registered external tools: list/call."""
    action = (data or {}).get('action', '')
    reg = executor.tool_registry
    if action == 'list':
        tools = [{'name': t['name'], 'method': t['method'],
                  'description': t['description'],
                  'params': {p: {'required': s['required'],
                                 'description': s['description'],
                                 'type': s['type']}
                             for p, s in t['params'].items()}}
                 for t in reg.list_tools()]
        socketio.emit('tools_list', {'tools': tools})
        return
    if action == 'call':
        name = (data or {}).get('name', '')
        args = (data or {}).get('args') or {}
        try:
            ok, text = reg.call(name, args=args)
            result = text
        except Exception as e:
            result = f"Tool call failed: {e}"
    else:
        result = 'Unknown tool action.'
    socketio.emit('ai_text', {'text': result})


@socketio.on('approvals_list')
def handle_approvals_list():
    """R5: persistent approvals inbox (survives missed toasts)."""
    items = []
    with executor.approvals._lock:
        for aid, st in executor.approvals._pending.items():
            if st.get('decision') is None:
                items.append({'id': aid, 'action': st['action'],
                              'summary': st['summary']})
    socketio.emit('approvals_update', {'pending': items})


@socketio.on('transcript_search')
def handle_transcript_search(data):
    """R4: memory-panel search over the FTS conversation archive."""
    q = (data or {}).get('query', '')
    from utils.transcript_search import get_archive
    hits = get_archive().search(q, limit=6)
    socketio.emit('transcript_results', {
        'query': q,
        'results': [{'snippet': h['snippet'], 'created': h['created']}
                    for h in hits]})


@socketio.on('approval_response')
def handle_approval_response(data):
    """Dashboard APPROVE / DENY for held critical actions."""
    approval_id = (data or {}).get('id', '')
    approved = bool((data or {}).get('approved', False))
    ok = executor.approvals.resolve(approval_id, approved)
    if not ok:
        socketio.emit('ai_text',
                      {'text': 'That approval was already resolved or expired.'})


@socketio.on('disconnect')
def handle_disconnect():
    print('Client disconnected')
