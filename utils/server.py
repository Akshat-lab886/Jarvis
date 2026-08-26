from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO
from config import Config
import os
import re
import json
import threading
import time

app = Flask(__name__, template_folder='../templates', static_folder='../static')
app.config.from_object(Config)
# Force threading mode to avoid Eventlet conflicts with Pygame/Asyncio
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

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

def emit_system_vitals():
    """Background thread that emits system vitals every 2 seconds."""
    global vitals_running
    vitals_running = True
    
    while vitals_running:
        try:
            vitals = executor.tools.get_system_vitals()
            socketio.emit('system_vitals', vitals)
        except Exception as e:
            print(f"Vitals emission error: {e}")
        time.sleep(2)

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

    # Recurring automations (cron-style jobs) — persist across restarts
    # and execute real Jarvis actions through the brain + executor.
    def _run_automation(job):
        action_text = job.get('action', '')
        command = brain.think(action_text)
        return executor.execute_command(
            command, brain, original_text=action_text,
            ui_callback=send_to_ui)
    executor.recurring.mouth = executor.mouth
    executor.recurring.on_fire = _run_automation
    if executor.recurring.count() > 0:
        executor.recurring.start()

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
        """think→execute pipeline shared by dashboard/telegram/webhook."""
        text = event.text

        from utils.quick_actions import handle_quick_actions
        if handle_quick_actions(text, executor, brain,
                                ui_callback=send_to_ui):
            return "Quick action executed."

        image_path = None
        if event.kind == 'photo':
            image_path = event.meta.get('image_path')

        silent = (event.source == 'telegram'
                  or bool(event.meta.get('silent_host')))
        try:
            if silent:
                executor.mouth.suppress = True
            command = brain.think(text, image_path=image_path)
            return executor.execute_command(
                command, brain, original_text=text,
                ui_callback=send_to_ui)
        finally:
            if silent:
                executor.mouth.suppress = False

    bus.handler = _event_pipeline

    # Idle hibernation: suspend polling loops + caches when quiet
    from utils.hibernation import wire_default_services, get_manager
    try:
        wire_default_services(executor=executor)
        get_manager().start()
    except Exception as e:
        print(f"Hibernation init skipped: {e}")

    greetings = ["Online and ready, Sir.", "Systems operational.", "Good to see you again, Sir.", "I am Jarvis, at your service."]
    try:
        greeting = random.choice(greetings)
        executor.mouth.speak(greeting)
    except Exception as e:
        print(f"Startup greeting failed: {e}")
        
    # Disable reloader to prevent double initialization of threads/bot
    ssl_ctx = _get_ssl_context()
    socketio.run(app, host='0.0.0.0', port=Config.PORT, allow_unsafe_werkzeug=True,
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
        return jsonify({'ok': False, 'error': str(e)}), 500


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


@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400
        
    if file:
        # Save file as webcam_capture.jpg (overwriting it)
        # This makes it the "current" image for analysis
        filename = 'webcam_capture.jpg'
        static_path = os.path.join(app.static_folder, filename)
        file.save(static_path)
        
        # Update last upload time
        global last_upload_time
        last_upload_time = time.time()
        
        # Emit photo_taken event so dashboard updates instantly
        socketio.emit('photo_taken', {
            'photo_url': '/static/webcam_capture.jpg',
            'timestamp': time.time()
        })
        
        return jsonify({'success': True, 'message': 'File uploaded successfully'}), 200

@app.route('/upload_knowledge', methods=['POST'])
def upload_knowledge():
    if 'files' not in request.files:
        return jsonify({'error': 'No files uploaded'}), 400
    
    files = request.files.getlist('files')
    results = []
    full_text_content = ""
    
    for file in files:
        if file.filename == '':
            continue
            
        try:
            # Save temporarily
            # Use safe temp path
            filename = file.filename
            temp_path = os.path.join(app.static_folder, 'temp_' + filename)
            file.save(temp_path)
            
            # Ingest
            # Ingest (store=False means only Short-Term initially)
            text_content = executor.librarian.ingest_file(temp_path, store=False)
            full_text_content += f"\nFILE: {filename}\nCONTENT:\n{text_content}\n"
            
            # TODO: Store this knowledge in Vector DB (Future)
            # For now, we just acknowledge it.
            print(f"Ingested {filename}: {len(text_content)} chars")
            
            results.append(filename)
            
            # Clean up
            if os.path.exists(temp_path):
                os.remove(temp_path)
            
        except Exception as e:
            print(f"Error ingesting {file.filename}: {e}")
            return jsonify({'error': str(e)}), 500

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
    print('Client connected')
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
    if action == 'start':
        result = executor.meeting.start_recording()
    elif action == 'stop':
        result = executor.meeting.stop_recording()
    elif action == 'transcript':
        result = executor.meeting.get_live_transcript() or "No transcript yet."
    else:
        result = "Unknown meeting action."
    socketio.emit('ai_text', {'text': result})


@socketio.on('privacy_action')
def handle_privacy_action(data):
    """Privacy settings from dashboard."""
    action = (data or {}).get('action', '')
    key = (data or {}).get('key', '')
    value = (data or {}).get('value', '')
    if action == 'get':
        result = json.dumps(executor.privacy.get_all_settings(), indent=2)
    elif action == 'update':
        result = executor.privacy.update_setting(key, value)
    elif action == 'summary':
        result = executor.privacy.get_trust_summary()
    elif action == 'audit':
        entries = executor.privacy.get_audit_log(limit=10)
        result = json.dumps(entries, indent=2)
    else:
        result = "Unknown privacy action."
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
                j['next'] = executor.recurring.describe_next(j)
            except Exception:
                j['next'] = ''
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
