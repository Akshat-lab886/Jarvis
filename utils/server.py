from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO
from config import Config
import os
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

    # Route reminder notifications to Telegram BEFORE the scheduler starts,
    # so no reminder can fire in the window before on_fire is attached.
    if tele_bot is not None:
        executor.scheduler.on_fire = tele_bot.notify_reminder
    executor.scheduler.start()

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

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/health')
def health():
    """Lightweight health check for uptime monitors / scripts."""
    import datetime
    return jsonify({
        'status': 'ok',
        'service': 'jarvis',
        'time': datetime.datetime.now().isoformat(timespec='seconds'),
        'brain_active': brain.active if hasattr(brain, 'active') else False,
    })

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

@socketio.on('disconnect')
def handle_disconnect():
    print('Client disconnected')
