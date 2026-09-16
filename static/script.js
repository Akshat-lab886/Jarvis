// Socket.IO is CDN-loaded; if it fails (offline/blocked CDN) fall back to
// a no-op stub so the rest of the deck (clock, REST vitals, rail renders)
// still paints instead of dying on `io is not defined` → black screen.
const socket = (window.io ? io() : {
    on() {}, emit() {}, connected: false,
});

// DOM Elements
const core = document.getElementById('core');
const statusText = document.getElementById('status');
const messageContainer = document.getElementById('message-container');
const listeningIndicator = document.getElementById('listening-indicator');
const audioPlayer = document.getElementById('audio-player');
const visionBtn = document.getElementById('vision-btn');
const chatInput = document.getElementById('chat-input');
const sendBtn = document.getElementById('send-btn');
const panelBackdrop = document.getElementById('panel-backdrop');

// --- THEME: parchment (brown/white) is the only console skin ---
// No switcher ships. This keeps the body class pinned and migrates any
// stale `jarvis-theme` localStorage value from the retired dark themes.

function applyTheme(theme) {
    try {
        document.body.classList.remove(
            'theme-midnight', 'theme-command', 'theme-brutalist',
            'theme-zen', 'theme-vault');
        document.body.classList.add('theme-parchment');
    } catch (e) { /* non-fatal: theme is cosmetic */ }
    try {
        localStorage.setItem('jarvis-theme', 'parchment');
    } catch (e) { /* private-mode storage may throw */ }
}

// Single theme: pin parchment on load (migrates stale dark-theme values)
try { applyTheme(localStorage.getItem('jarvis-theme') || 'parchment'); }
catch (e) { applyTheme('parchment'); }

// Instant HUD Clock — strip + rail
function updateHUDClock() {
    const t = new Date().toLocaleTimeString('en-US', { hour12: false });
    const vt = document.getElementById('vitals-time');
    const sc = document.getElementById('strip-clock');
    const rc = document.getElementById('rail-clock');
    if (vt) vt.textContent = t;
    if (sc) sc.textContent = t;
    if (rc) rc.textContent = t;
}
setInterval(updateHUDClock, 1000);
updateHUDClock();

// --- PANEL MANAGEMENT (drawers only; ops rail is permanent) ---
const allPanels = [
    'memory-panel', 'people-panel', 'meeting-panel', 'privacy-panel',
    'notes-panel', 'tasks-panel', 'librarian-panel', 'capabilities-panel',
    'llm-panel', 'parity-panel', 'agent-panel'
];
const allTriggers = [
    'memory-btn', 'people-btn', 'meeting-btn', 'privacy-btn',
    'notes-btn', 'tasks-btn', 'librarian-btn', 'skills-btn', 'llm-btn',
    'parity-btn', 'dev-btn'
];
let activePanelId = null;

function openPanel(panelId, triggerId) {
    // If same panel is already open, close it
    if (activePanelId === panelId) {
        closeAllPanels();
        return;
    }
    closeAllPanels();
    const panel = document.getElementById(panelId);
    if (panel) {
        panel.classList.add('active');
        activePanelId = panelId;
    }
    if (triggerId) {
        const trigger = document.getElementById(triggerId);
        if (trigger) trigger.classList.add('active');
    }
    if (panelBackdrop) panelBackdrop.classList.add('active');
}

function closeAllPanels() {
    allPanels.forEach(id => {
        const el = document.getElementById(id);
        if (el) el.classList.remove('active');
    });
    allTriggers.forEach(id => {
        const el = document.getElementById(id);
        if (el) el.classList.remove('active');
    });
    activePanelId = null;
    if (panelBackdrop) panelBackdrop.classList.remove('active');
}

if (panelBackdrop) {
    panelBackdrop.addEventListener('click', closeAllPanels);
}

let recognition;
let isAwake = false;
let wakeTimer = null;
const WAKE_WORD = "jarvis";
const SLEEP_DELAY = 8000; // 8 seconds
const WAKE_ALIASES = ["jarvis", "service", "travis", "jervis", "javis", "harvest", "start", "wake"];

// --- SOCKET IO HANDLERS ---

// Connection truth drives the strip status + dot too, not just the center
// #status line — the header can never claim ONLINE while disconnected.
function setConnState(online) {
    const s = document.getElementById('strip-status');
    const d = document.getElementById('strip-dot');
    if (s) {
        s.textContent = online ? 'ONLINE' : 'OFFLINE';
        s.classList.toggle('online', online);
        s.classList.toggle('offline', !online);
    }
    if (d) d.classList.toggle('off', !online);
}

// Connection status
socket.on('connect', () => {
    console.log('Connected to Jarvis server');
    setConnState(true);
    updateStatus('SYSTEMS NOMINAL');
    addMessage("SYSTEM: Connected to Jarvis");
});

socket.on('disconnect', () => {
    console.log('Disconnected from Jarvis server');
    setConnState(false);
    addMessage("SYSTEM: Connection lost");
    updateStatus("DISCONNECTED", "offline");
});

// Status updates from server
socket.on('status', (data) => {
    console.log('📊 Status:', data.message);
    updateStatus(data.message);
});

// AI text responses
socket.on('ai_text', (data) => {
    console.log('🤖 AI:', data.text);
    // Agent-loop streaming already rendered this answer live —
    // skip the duplicate final emission when content matches.
    if (_streamState.text && data.text &&
        data.text.trim() === _streamState.text.trim()) {
        finalizeStream();
        return;
    }
    finalizeStream();
    addMessage(`JARVIS: ${data.text}`);
    setCoreState('speaking');
});

// --- Phase 2: agent-core token streaming ---
const _streamState = { row: null, body: null, text: '' };
let _lastStreamed = '';

function finalizeStream() {
    if (_streamState.row) {
        _streamState.row.classList.remove('streaming');
    }
    _streamState.row = null;
    _streamState.body = null;
    if (_streamState.text) _lastStreamed = _streamState.text;
    _streamState.text = '';
}

socket.on('ai_text_stream', (data) => {
    const delta = data.delta || '';
    if (!delta) return;
    setCoreState('speaking');
    if (!_streamState.row) {
        const intro = document.getElementById('intro-msg');
        if (intro) intro.remove();
        // Ops-deck grammar — identical to addMessage('JARVIS: …').
        const row = document.createElement('div');
        const ts = new Date().toLocaleTimeString('en-US',
            { hour12: false });
        row.className = 'msg j streaming';
        row.innerHTML =
            `<span class="ts num">${ts}</span>` +
            `<span class="dir">\u00AB</span>` +
            `<span class="body prose"></span>`;
        messageContainer.appendChild(row);
        messageContainer.scrollTop = messageContainer.scrollHeight;
        _streamState.row = row;
        _streamState.body = row.querySelector('.body');
        _streamState.text = '';
        if (window._updateHero) window._updateHero();
    }
    _streamState.text += delta;
    _streamState.body.textContent =
        _streamState.body.textContent + delta;
    messageContainer.scrollTo({ top: messageContainer.scrollHeight });
});

socket.on('ai_text_stream_end', () => {
    finalizeStream();
});

// Live Agent Log — a contained overlay: capped rows, auto-hide after a
// quiet beat, and an explicit dismiss. Rows live in .term-body so the
// header + close control never scroll away.
const TERMINAL_MAX_ROWS = 120;
let terminalMuted = false;
let terminalHideTimer = null;

function terminalAutoHide() {
    if (terminalHideTimer) clearTimeout(terminalHideTimer);
    terminalHideTimer = setTimeout(() => {
        const t = document.getElementById('terminal-box');
        if (t) t.style.display = 'none';
    }, 6000);
}

function dismissTerminal() {
    terminalMuted = true;             // suppress auto-show until a new command
    const t = document.getElementById('terminal-box');
    if (t) t.style.display = 'none';
}

socket.on('new_log', (data) => {
    const terminal = document.getElementById('terminal-box');
    if (!terminal) return;
    if (!terminalMuted) terminal.style.display = 'block';
    const body = terminal.querySelector('.term-body') || terminal;
    const line = document.createElement('div');
    const time = new Date().toLocaleTimeString('en-US', { hour12: false });
    const span = document.createElement('span');
    span.style.opacity = '.5';
    span.textContent = `[${time}] `;
    line.appendChild(span);
    line.appendChild(document.createTextNode(data.data));
    body.appendChild(line);
    while (body.children.length > TERMINAL_MAX_ROWS) {
        body.removeChild(body.firstElementChild);
    }
    body.scrollTop = body.scrollHeight;
    terminalAutoHide();
});

const terminalCloseBtn = document.getElementById('terminal-close');
if (terminalCloseBtn) terminalCloseBtn.addEventListener('click', dismissTerminal);

// Audio response (TTS)
socket.on('audio', (data) => {
    console.log('🔊 Playing audio response');
    if (data.audio_url) {
        audioPlayer.src = data.audio_url + '?t=' + Date.now(); // Cache bust
        audioPlayer.play().catch(e => console.log('Audio play error:', e));
    }
});

// When audio finishes, go back to listening
audioPlayer.addEventListener('ended', () => {
    setCoreState('active');
    updateStatus("LISTENING");
});

// System Vitals real-time updates + sparklines
const _sparkCpu = [];
const _sparkRam = [];

function _drawSpark(id, data) {
    const svg = document.getElementById(id);
    if (!svg) return;
    const poly = svg.querySelector('polyline');
    if (!poly) return;
    const W = 120, H = 18, MAX = 30;
    const view = data.slice(-MAX);
    if (!view.length) {
        poly.setAttribute('points', `0,${H - 2} ${W},${H - 2}`);
        return;
    }
    const span = Math.max(view.length - 1, 1);
    const pts = view.map((v, i) => {
        const x = (i / span) * W;
        const y = H - 2 - (v / 100) * (H - 4);
        return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(' ');
    poly.setAttribute('points', pts);
}

function _setArc(id, pct) {
    const el = document.getElementById(id);
    if (!el) return;
    const r = Number(el.getAttribute('r')) || 80;
    const C = 2 * Math.PI * r;
    el.style.strokeDasharray = String(C);
    el.style.strokeDashoffset = String(C * (1 - Math.min(100, Math.max(0, pct)) / 100));
}

/* VITALS PAINTER — single writer for every vitals-driven pixel.
   Fed by the socket stream (authoritative, 2s cadence) and seeded
   once from REST so first paint never shows a dead instrument. */
window.__emitVitals = function (vitals) {
    // CPU
    const cpuBar = document.getElementById('cpu-bar');
    const cpuValue = document.getElementById('cpu-value');
    if (cpuBar && cpuValue) {
        cpuBar.style.width = vitals.cpu + '%';
        cpuValue.textContent = vitals.cpu + '%';
    }
    _sparkCpu.push(vitals.cpu);
    if (_sparkCpu.length > 30) _sparkCpu.shift();
    _drawSpark('spark-cpu', _sparkCpu);

    // RAM
    const ramBar = document.getElementById('ram-bar');
    const ramValue = document.getElementById('ram-value');
    if (ramBar && ramValue) {
        ramBar.style.width = vitals.ram + '%';
        ramValue.textContent = vitals.ram + '%';
    }
    _sparkRam.push(vitals.ram);
    if (_sparkRam.length > 30) _sparkRam.shift();
    _drawSpark('spark-ram', _sparkRam);

    // Battery
    const batteryBar = document.getElementById('battery-bar');
    const batteryValue = document.getElementById('battery-value');
    if (batteryBar && batteryValue) {
        batteryBar.style.width = vitals.battery + '%';
        batteryValue.textContent = vitals.battery + '%' + (vitals.charging ? ' ⌁' : '');
        if (vitals.battery < 20) batteryBar.classList.add('low');
    }

    // Disk
    const diskBar = document.getElementById('disk-bar');
    const diskValue = document.getElementById('disk-value');
    if (diskBar && diskValue) {
        diskBar.style.width = vitals.disk + '%';
        diskValue.textContent = vitals.disk + '%';
    }

    // Strip compact vitals (visible; rail stays hidden for compat)
    const sc = document.getElementById('strip-cpu');
    if (sc && vitals.cpu != null) sc.textContent = vitals.cpu + '%';
    const sm = document.getElementById('strip-mem');
    if (sm && vitals.ram != null) sm.textContent = vitals.ram + '%';
    const sd = document.getElementById('strip-dsk');
    if (sd && vitals.disk != null) sd.textContent = vitals.disk + '%';

    // Hero gauge arcs (R: 86/72/58)
    _setArc('arc-cpu', vitals.cpu);
    _setArc('arc-ram', vitals.ram);
    _setArc('arc-dsk', vitals.disk);
    const hc = document.getElementById('hero-cpu');
    if (hc) hc.textContent = String(vitals.cpu);
    const hs = document.getElementById('hero-status');
    if (hs) {
        // Real fleet state beats a static slogan (density law).
        const n = (window._fleetOnline != null) ? window._fleetOnline : null;
        hs.textContent =
            vitals.cpu > 85 ? 'HIGH LOAD — CPU ' + vitals.cpu + '%' :
            vitals.battery < 15 ? 'POWER LOW — ' + vitals.battery + '%' :
            n != null ? `FLEET ${n} ONLINE · NOMINAL` :
            'ALL SYSTEMS NOMINAL';
    }

    // Timestamp
    const vitalsTime = document.getElementById('vitals-time');
    if (vitalsTime && vitals.timestamp) {
        vitalsTime.textContent = vitals.timestamp;
    }
};

socket.on('system_vitals', (vitals) => {
    window._vitalsSeen = true;          // socket is authoritative now
    window.__emitVitals(vitals);
});

// First-paint fallback: seed the HUD from REST so the gauge arcs,
// bars and big numerals are alive before the socket's first tick.
fetch('/api/vitals').then(r => r.ok ? r.json() : null).then(v => {
    if (v && !window._vitalsSeen) window.__emitVitals(v);
}).catch(() => {});

// Smart Home device status — build cards on demand, delegate clicks
const DEVICE_LIST = [
    ['living_room_light', 'LIVING ROOM'], ['bedroom_light', 'BEDROOM'],
    ['kitchen_light', 'KITCHEN'], ['bathroom_light', 'BATHROOM'],
    ['fan', 'FAN'], ['ac', 'A/C']
];

function ensureDeviceCards() {
    const grid = document.getElementById('device-grid');
    if (!grid || grid.children.length) return;
    grid.innerHTML = DEVICE_LIST.map(([id, label]) =>
        `<div class="row" data-device="${id}" style="cursor:pointer;">
            <div class="r1"><span class="t">${label}</span>
            <span class="m" id="status-${id}">OFF</span></div>
        </div>`).join('');
}

socket.on('home_update', (devices) => {
    console.log('Home update:', devices);
    ensureDeviceCards();
    for (const [deviceId, device] of Object.entries(devices)) {
        const row = document.querySelector(`[data-device="${deviceId}"]`);
        const statusEl = document.getElementById(`status-${deviceId}`);
        if (row && statusEl) {
            const isOn = device.status === 'on';
            row.classList.toggle('on', isOn);
            statusEl.textContent = isOn ? 'ON' : 'OFF';
            statusEl.style.color = isOn ? 'var(--ok)' : '';
        }
    }
});

document.addEventListener('click', (e) => {
    const card = e.target.closest('[data-device]');
    if (!card || !card.dataset.device) return;
    const deviceId = card.dataset.device;
    const isOn = card.classList.contains('on');
    const command = isOn ? 'turn_off' : 'turn_on';
    socket.emit('user_input', {
        text: `${command.replace('_', ' ')} the ${deviceId.replace(/_/g, ' ')}`
    });
});

// Photo capture display
socket.on('photo_taken', (data) => {
    console.log('📸 Photo taken:', data.photo_url);

    const photoFrame = document.getElementById('photo-frame');
    if (photoFrame) {
        // Add flash effect
        photoFrame.classList.add('flash');
        setTimeout(() => photoFrame.classList.remove('flash'), 300);

        // Check if image already exists
        let img = photoFrame.querySelector('img');
        if (!img) {
            img = document.createElement('img');
            photoFrame.appendChild(img);
        }

        // Update image with cache-busting timestamp
        img.src = data.photo_url + '?t=' + data.timestamp;
        photoFrame.classList.add('has-photo');
    }
});

// Capture button click handler
const captureBtn = document.getElementById('capture-btn');
if (captureBtn) {
    captureBtn.addEventListener('click', () => {
        socket.emit('user_input', { text: 'take a photo' });
    });
}

// Upload button handling
const uploadBtn = document.getElementById('upload-btn');
const fileInput = document.getElementById('image-upload');

if (uploadBtn && fileInput) {
    uploadBtn.addEventListener('click', () => {
        fileInput.click();
    });

    fileInput.addEventListener('change', (e) => {
        if (e.target.files && e.target.files[0]) {
            const formData = new FormData();
            formData.append('file', e.target.files[0]);

            fetch('/upload', {
                method: 'POST',
                body: formData
            })
                .then(response => response.json())
                .then(data => {
                    console.log('Upload success:', data);

                    // Immediately update UI to show uploaded photo
                    const photoFrame = document.getElementById('photo-frame');
                    if (photoFrame) {
                        photoFrame.classList.add('flash');
                        setTimeout(() => photoFrame.classList.remove('flash'), 300);

                        let img = photoFrame.querySelector('img');
                        if (!img) {
                            img = document.createElement('img');
                            photoFrame.appendChild(img);
                        }

                        // Force reload with timestamp
                        img.src = '/static/webcam_capture.jpg?t=' + new Date().getTime();
                        photoFrame.classList.add('has-photo');
                    }
                })
                .catch(error => {
                    console.error('Upload error:', error);
                });
        }
    });
}

// --- WAKE WORD & STATE LOGIC ---

function wakeUp() {
    if (!isAwake) {
        isAwake = true;
        setCoreState('active');
        updateStatus("JARVIS ACTIVE - LISTENING", "online");
        addMessage("SYSTEM: WAKE DETECTED");

        // Visual Pulse
        core.style.transform = "scale(1.2)";
        setTimeout(() => core.style.transform = "scale(1)", 200);
    }
    resetWakeTimer();
}

function GoToSleep() {
    isAwake = false;
    setCoreState('idle');
    updateStatus("Systems Nominal");
    if (wakeTimer) clearTimeout(wakeTimer);
    addMessage("SYSTEM: STANDBY MODE");
}

function resetWakeTimer(customDelay = SLEEP_DELAY) {
    if (wakeTimer) clearTimeout(wakeTimer);
    wakeTimer = setTimeout(GoToSleep, customDelay);
}


// --- SPEECH RECOGNITION (BROWSER BASED) ---
const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;

if (SpeechRecognition) {
    recognition = new SpeechRecognition();
    recognition.continuous = false; // Capture single phrases
    recognition.lang = 'en-US';
    recognition.interimResults = false;

    recognition.onstart = () => {
        console.log("🎤 Speech recognition STARTED");
        addMessage("SYSTEM: Microphone Active");
        if (isAwake) {
            listeningIndicator.classList.add('active');
        }
    };

    recognition.onend = () => {
        console.log("🎤 Speech recognition ENDED"); // Don't auto-restart blindly
        listeningIndicator.classList.remove('active');
        if (isAwake) {
            // Optional: Try restarting if we want continuous listening,
            // but on mobile manual trigger is safer.
        }
    };

    recognition.onerror = (event) => {
        console.error("❌ Speech recognition ERROR:", event.error);
        let errorMsg = "Mic Error: " + event.error;

        if (event.error === 'not-allowed') {
            errorMsg = "🚫 Permission denied! Check settings.";
        } else if (event.error === 'no-speech') {
            errorMsg = "No speech detected.";
        } else if (event.error === 'audio-capture') {
            errorMsg = "🚫 No microphone found!";
        } else if (event.error === 'network') {
            errorMsg = "Network error - need internet";
        }

        addMessage("SYSTEM: " + errorMsg);
        updateStatus(errorMsg);
    };

    recognition.onresult = (event) => {
        const transcript = event.results[0][0].transcript.toLowerCase().trim();
        console.log("🎤 Heard:", transcript);

        if (!isAwake) {
            // PASSIVE MODE: Check for Wake Word or Aliases
            // Check if any alias is in the transcript
            const detectedAlias = WAKE_ALIASES.find(alias => transcript.includes(alias));

            if (detectedAlias) {
                wakeUp();

                // If user said "Jarvis open google", we should process "open google" immediately
                // Remove the alias from the command
                const command = transcript.replace(detectedAlias, "").trim();

                // Only process if there is a command *after* the wake word
                if (command.length > 0) {
                    processCommand(command);
                }
            }
        } else {
            // ACTIVE MODE: Process everything
            processCommand(transcript);
        }
    };

    console.log("🎤 API initialized successfully");

} else {
    alert("Speech API not supported.");
    console.error("SpeechRecognition API missing");
    addMessage("SYSTEM: Speech not supported");
}

function processCommand(text) {
    addMessage(`YOU: ${text}`);
    socket.emit('process_text', { text: text });
    setCoreState('processing');
    updateStatus("Analysing Input...");
    resetWakeTimer(); // Keep awake after a command
}


// --- VISION BUTTON ---
visionBtn.addEventListener('click', () => {
    wakeUp(); // Button manually wakes him up
    addMessage("Initiating Visual Scan...");
    updateStatus("Vision Active");
    socket.emit('process_text', { text: "look at this" });
});

// --- DEV MODE BUTTON ---
const devBtn = document.getElementById('dev-btn');
if (devBtn) {
    devBtn.addEventListener('click', () => {
        // DEV opens the agent-loop inspector instead of a window.prompt.
        openPanel('agent-panel', 'dev-btn');
        loadAgentConfig();
    });
}

// --- AGENT LOOP INSPECTOR (DEV drawer) ---
const agentClose = document.getElementById('agent-close');
if (agentClose) agentClose.addEventListener('click', closeAllPanels);

async function loadAgentConfig() {
    try {
        const res = await fetch('/api/agent');
        if (!res.ok) return;
        const d = await res.json();
        const ms = document.getElementById('agent-max-steps');
        if (ms && d.max_steps != null) ms.value = d.max_steps;
        const m = document.getElementById('agent-model');
        if (m && d.model) m.value = d.model;
        const feed = document.getElementById('agent-tool-feed');
        if (feed) {
            feed.innerHTML = d.running
                ? '<div class="no-data">run in progress — stop to queue changes</div>'
                : '<div class="no-data">idle — run a command to stream tool calls here</div>';
        }
    } catch (_) {}
}

function pushAgentStep(data) {
    const feed = document.getElementById('agent-tool-feed');
    if (!feed) return;
    const row = document.createElement('div');
    row.className = 'drow';
    const status = (data.status || 'step').toUpperCase().padEnd(4);
    const color = data.status === 'call' ? 'var(--accent)'
        : data.status === 'fail' ? 'var(--fail)'
        : data.status === 'ok' ? 'var(--ok)'
        : 'var(--text-3)';
    row.innerHTML = `<span class="mem-cat" style="color:${color}">[${status} #${data.step}]</span>` +
        `<span class="grow">${escapeHtml(data.name || '')}${data.args ? ' ' + escapeHtml(String(data.args).slice(0, 90)) : ''}</span>`;
    feed.appendChild(row);
    feed.scrollTop = feed.scrollHeight;
    // keep feed capped so the inspector never grows unbounded
    while (feed.children.length > 120) feed.removeChild(feed.firstElementChild);
}

function pushAgentStatus(msg) {
    const log = document.getElementById('agent-status-log');
    if (!log) return;
    const ts = new Date().toLocaleTimeString('en-US', { hour12: false });
    const line = document.createElement('div');
    line.className = 'drow';
    line.innerHTML = `<span class="mem-cat">${ts}</span><span class="grow">${escapeHtml(String(msg || ''))}</span>`;
    log.appendChild(line);
    log.scrollTop = log.scrollHeight;
    while (log.children.length > 80) log.removeChild(log.firstElementChild);
}

function agentSet(prop, value, viaInput) {
    const body = {};
    body[prop] = value;
    fetch('/api/agent', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
    }).then(() => loadAgentConfig());
}

const agentModeBtns = document.querySelectorAll('[data-agent-mode]');
agentModeBtns.forEach(b => b.addEventListener('click', () => {
    agentSet('mode', b.dataset.agentMode);
}));
const agentStreamBtns = document.querySelectorAll('[data-agent-stream]');
agentStreamBtns.forEach(b => b.addEventListener('click', () => {
    agentSet('stream', b.dataset.agentStream);
}));
const agentSetStepsBtn = document.getElementById('agent-set-steps');
if (agentSetStepsBtn) agentSetStepsBtn.addEventListener('click', () => {
    const ms = document.getElementById('agent-max-steps');
    agentSet('max_steps', parseInt(ms.value, 10) || 8);
});
const agentSetModelBtn = document.getElementById('agent-set-model');
if (agentSetModelBtn) agentSetModelBtn.addEventListener('click', () => {
    const m = document.getElementById('agent-model');
    agentSet('model', m.value.trim());
});
const agentStopBtn = document.getElementById('agent-stop-btn');
if (agentStopBtn) agentStopBtn.addEventListener('click', () => {
    socket.emit('agent_stop', {});
    pushAgentStatus('STOP requested');
});

socket.on('agent_step', (data) => pushAgentStep(data));
socket.on('agent_status', (data) => pushAgentStatus(data.message));

// --- SKILLS DRAWER DATA ---
const skillsBtnEl = document.getElementById('skills-btn');
if (skillsBtnEl) {
    skillsBtnEl.addEventListener('click', () => {
        // SKL is a nav code like every other — it must open the panel,
        // not just fire list events into a drawer the user can't reach.
        openPanel('capabilities-panel', 'skills-btn');
        socket.emit('skills_action', { action: 'list' });
        socket.emit('tools_action', { action: 'list' });
    });
}

const parityBtn = document.getElementById('parity-btn');
if (parityBtn) {
    parityBtn.addEventListener('click', () => {
        openPanel('parity-panel', 'parity-btn');
        loadParity();
    });
}
const parityClose = document.getElementById('parity-close');
if (parityClose) parityClose.addEventListener('click', closeAllPanels);

const capabilitiesClose = document.getElementById('capabilities-close');
if (capabilitiesClose) capabilitiesClose.addEventListener('click', closeAllPanels);

// --- CMD CURSOR ---
const cmdWrap = document.getElementById('cmd-wrap');
if (chatInput && cmdWrap) {
    chatInput.addEventListener('input', () => {
        cmdWrap.classList.toggle('has-text', !!chatInput.value);
    });
}

// --- CHAT INPUT ---
function sendText(text) {
    text = text || chatInput.value.trim();
    if (text) {
        terminalMuted = false;   // a fresh command unmutes the agent log
        wakeUp();
        processCommand(text);
        chatInput.value = '';
    }
}

chatInput.addEventListener('keypress', (e) => {
    if (e.key === 'Enter') {
        sendText();
    }
});

sendBtn.addEventListener('click', sendText);


// --- TO-DO LIST PANEL ---
const tasksBtn = document.getElementById('tasks-btn');
const tasksPanel = document.getElementById('tasks-panel');
const tasksClose = document.getElementById('tasks-close');
const taskInput = document.getElementById('task-input');
const taskAddBtn = document.getElementById('task-add-btn');
const taskList = document.getElementById('task-list');
const taskClearBtn = document.getElementById('task-clear-btn');

function emitTodo(action, text) {
    socket.emit('todo_action', { action: action, text: text });
}

function renderTasks(items) {
    if (!taskList) return;
    taskList.innerHTML = '';
    if (!items || items.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'task-empty';
        empty.textContent = 'No tasks. All clear, Sir.';
        taskList.appendChild(empty);
        return;
    }
    items.forEach(item => {
        const div = document.createElement('div');
        div.className = 'task-item' + (item.done ? ' done' : '');
        div.dataset.id = item.id;
        div.innerHTML = `
            <span class="task-check" data-action="done" title="Mark done" style="color:${item.done ? 'var(--ok)' : 'var(--text-3)'}">[${item.done ? 'X' : ' '}]</span>
            <span class="task-text" style="flex:1;${item.done ? 'color:var(--text-3);text-decoration:line-through;' : ''}">${escapeHtml(item.text)}</span>
            <span class="task-del" data-action="remove" title="Remove" style="cursor:pointer;">[DEL]</span>
        `;
        taskList.appendChild(div);
    });
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

// --- RLM HIERARCHY SNAPSHOT (memory drawer) ---
// Reads GET /api/rlm — a live dashboard_snapshot the deck never consumed —
// so the recursive-memory hierarchy is visible, not just episodic counts.
const RLM_LEVEL_NAMES = { 0: 'EV', 1: 'SUM', 2: 'ABS', 3: 'WM' };

async function refreshRlm() {
    const el = document.getElementById('rlm-snapshot');
    if (!el) return;
    try {
        const res = await fetch('/api/rlm');
        if (!res.ok) { el.innerHTML = '<div class="no-data">RLM unreachable</div>'; return; }
        const d = await res.json();
        if (d.error) {
            el.innerHTML = '<div class="no-data">RLM off — ' + escapeHtml(String(d.error).slice(0, 80)) + '</div>';
            return;
        }
        const bl = d.by_level || {};
        const chips = Object.entries(bl).length
            ? Object.entries(bl).map(([k, v]) =>
                `<b>${escapeHtml(String(k).toUpperCase())}</b> ${v}`).join(' · ')
            : '—';
        const wm = d.world_model ? String(d.world_model) : '';
        const recents = (d.recent || []).slice(0, 4);
        const wmLine = wm
            ? `<div class="rlm-row"><span class="rl">WORLD</span><span class="rt">${escapeHtml(wm.slice(0, 170))}${wm.length > 170 ? '…' : ''}</span></div>`
            : '';
        el.innerHTML =
            `<div class="rlm-row"><span class="rl">HIER</span><span class="rt">${chips}</span></div>` +
            `<div class="rlm-row"><span class="rl">STATE</span><span class="rt">notes ${d.notes} · pending ${d.pending_events} · unreflected ${d.unreflected} · superseded ${d.superseded} · entities ${d.entities} · ${d.vector ? 'vector ON' : 'vector OFF'}</span></div>` +
            wmLine +
            `<div class="rlm-row"><span class="rl">TICK</span><span class="rt">consolidation ${d.last_consolidation || 'never'} · world ${d.world_model_updated || '—'}</span></div>` +
            (recents.length
                ? recents.map(n => `<div class="rlm-row"><span class="rl">${RLM_LEVEL_NAMES[n.level] != null ? RLM_LEVEL_NAMES[n.level] : n.level}${n.superseded ? ' [OLD]' : ''}</span><span class="rt">${escapeHtml(String(n.text || '').slice(0, 110))}</span></div>`).join('')
                : '<div class="rlm-row"><span class="rt">no consolidated notes yet</span></div>');
    } catch (_) {
        el.innerHTML = '<div class="no-data">RLM unavailable</div>';
    }
}
const rlmRefreshBtn = document.getElementById('rlm-refresh-btn');
if (rlmRefreshBtn) rlmRefreshBtn.addEventListener('click', refreshRlm);

// --- JARVIS ↔ HERMES 26-FEATURE PARITY MATRIX ---
const PARITY_FEATURES = [
    { id: 1, group: 'Bounded learning loop', name: 'Guardrails', short: 'Self-updating USER/MEMORY/.jarvis.md', modules: 'guardrails.py', status: 2 },
    { id: 2, group: 'Bounded learning loop', name: 'Context injection', short: '@file/@dir/@git/@url expansion', modules: 'context_injection.py', status: 2 },
    { id: 3, group: 'Bounded learning loop', name: 'Skill forge', short: 'Telemetry → reusable SKILLS → self-patch', modules: 'skill_forge.py', status: 2 },
    { id: 4, group: 'Bounded learning loop', name: 'FTS5 transcript search', short: 'Bounded full-text index of exchanges', modules: 'transcript_search.py', status: 2 },
    { id: 5, group: 'Execution environments', name: 'Multi-backend runtime', short: 'local/docker/ssh/daytona/singularity/vercel/modal', modules: 'runtimes.py', status: 2 },
    { id: 6, group: 'Execution environments', name: 'Python RPC', short: 'Persistent sandboxed worker over NDJSON', modules: 'python_rpc.py', status: 2 },
    { id: 7, group: 'Execution environments', name: 'Checkpoints + /rollback', short: 'Snapshot before edits; restore any prior state', modules: 'checkpoints.py', status: 2 },
    { id: 8, group: 'Execution environments', name: 'Post-edit diagnostics', short: 'Native checkers / real LSP after edit', modules: 'lsp_diagnostics.py', status: 2 },
    { id: 9, group: 'Execution environments', name: 'Computer use', short: 'Background AX desktop driving', modules: 'computer_use.py', status: 2 },
    { id: 10, group: 'Omnichannel gateways', name: 'Gateway hub + bot mode', short: 'Discord/Slack/webhook/Telegram via event bus', modules: 'gateways.py, telegram_bot.py', status: 2 },
    { id: 11, group: 'Omnichannel gateways', name: 'Terminal TUI', short: 'Headless-native terminal chat', modules: 'tui.py', status: 2 },
    { id: 12, group: 'Omnichannel gateways', name: 'HUD', short: 'Borderless always-on-top composer', modules: 'hud.py', status: 2 },
    { id: 13, group: 'Omnichannel gateways', name: 'Wake-word gating', short: "'Hey Jarvis' / configurable phrase", modules: 'wake_word.py', status: 2 },
    { id: 14, group: 'Automations & media', name: 'Recurring automations', short: 'NL cron + intervals across restarts', modules: 'recurring.py', status: 2 },
    { id: 15, group: 'Automations & media', name: 'Jarvis-as-MCP-server', short: 'Export RLM/FTS/skill catalog over stdio', modules: 'mcp_server.py', status: 2 },
    { id: 16, group: 'Automations & media', name: 'Mixture-of-agents', short: 'N providers + synthesizer', modules: 'moa.py', status: 2 },
    { id: 17, group: 'Automations & media', name: 'Browser automation', short: 'Static → Playwright → cloud facade', modules: 'browser_use.py', status: 1 },
    { id: 18, group: 'Automations & media', name: 'Tool gateway', short: 'Cloud browse/scrape/TTS/image with local fallbacks', modules: 'tool_gateway.py', status: 1 },
    { id: 19, group: 'Automations & media', name: 'Hyperframe', short: 'Instructions → storyboard → self-playing HTML → mp4', modules: 'hyperframe.py', status: 2 },
    { id: 20, group: 'Reasoning + memory', name: 'RLM recursive memory', short: 'L0→L1→L2→L3 compression + world model', modules: 'rlm/memory.py', status: 2 },
    { id: 21, group: 'Reasoning + memory', name: 'Temporal recall scoring', short: 'Relevance × recency × importance, recency floor', modules: 'rlm/memory.py', status: 2 },
    { id: 22, group: 'Reasoning + memory', name: 'Memory supersession', short: 'New insights replace contradicting older notes', modules: 'rlm/memory.py', status: 2 },
    { id: 23, group: 'Reasoning + memory', name: 'Entity graph + subject recall', short: 'Who/what memory is about', modules: 'rlm/entities.py', status: 2 },
    { id: 24, group: 'Reasoning + memory', name: 'Cross-session continuity', short: 'Re-inject prior state after idle gaps', modules: 'rlm, brain.py', status: 2 },
    { id: 25, group: 'Reasoning + memory', name: 'Persistent task plan', short: 'Durable goal+steps plan across turns', modules: 'rlm/plan_state.py', status: 2 },
    { id: 26, group: 'Reasoning + memory', name: 'Reasoner', short: 'Plan-before-acting, effort detection, critic/verify', modules: 'rlm/reasoner.py', status: 2 }
];
const PARITY_STATUS_LABELS = { 0: 'missing', 1: 'partial', 2: 'implemented', 3: 'surface-only' };

function loadParity() {
    const grid = document.getElementById('parity-grid');
    const summary = document.getElementById('parity-summary');
    if (!grid || !summary) return;
    const counts = { 0: 0, 1: 0, 2: 0, 3: 0 };
    PARITY_FEATURES.forEach(f => counts[f.status]++);
    summary.textContent = `${counts[2]}/26 implemented, ${counts[3]} surface-only, ${counts[1]} partial — ${counts[0]} missing.`;
    grid.innerHTML = PARITY_FEATURES.map(f => `
        <div class="row">
            <div class="r1">
                <span class="t">${escapeHtml(f.name)} <span class="mem-cat">${escapeHtml(f.group)}</span></span>
                <span class="m">${f.id}/26</span>
            </div>
            <div class="r2"><span class="a">${escapeHtml(f.short)}</span></div>
            <div class="r2">
                <span class="chip ${f.status === 2 ? 'ok' : f.status === 1 ? 'warn' : f.status === 3 ? 'skip' : 'fail'}">${PARITY_STATUS_LABELS[f.status] || '?'} </span>
                <span class="mem-cat">${escapeHtml(f.modules)}</span>
                <span class="btns" style="margin-left:auto;"><button class="tbtn" data-par="${f.id}">DETAILS</button></span>
            </div>
        </div>`).join('');
}

if (tasksBtn && tasksPanel) {
    tasksBtn.addEventListener('click', () => openPanel('tasks-panel', 'tasks-btn'));
}

if (tasksClose) {
    tasksClose.addEventListener('click', closeAllPanels);
}

if (taskAddBtn) {
    taskAddBtn.addEventListener('click', () => {
        const text = taskInput.value.trim();
        if (text) {
            emitTodo('add', text);
            taskInput.value = '';
        }
    });
}

if (taskInput) {
    taskInput.addEventListener('keypress', (e) => {
        if (e.key === 'Enter') taskAddBtn.click();
    });
}

if (taskList) {
    taskList.addEventListener('click', (e) => {
        const target = e.target.closest('[data-action]');
        if (!target) return;
        const item = target.closest('.task-item');
        if (!item) return;
        const id = item.dataset.id;
        emitTodo(target.dataset.action, id);
    });
}

if (taskClearBtn) {
    taskClearBtn.addEventListener('click', () => {
        if (confirm('Clear the entire to-do list?')) emitTodo('clear', '');
    });
}

// Server pushes fresh todo state after any change
socket.on('todo_update', (data) => {
    console.log('📋 Todo update:', data);
    renderTasks(data.items);
});

// --- NOTES PANEL ---
const notesBtn = document.getElementById('notes-btn');
const notesPanel = document.getElementById('notes-panel');
const notesClose = document.getElementById('notes-close');
const noteInput = document.getElementById('note-input');
const noteAddBtn = document.getElementById('note-add-btn');
const noteList = document.getElementById('note-list');

function emitNote(action, text) {
    socket.emit('note_action', { action: action, text: text });
}

function renderNotes(items) {
    if (!noteList) return;
    noteList.innerHTML = '';
    if (!items || items.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'task-empty';
        empty.textContent = 'No notes yet, Sir.';
        noteList.appendChild(empty);
        return;
    }
    items.forEach(item => {
        const div = document.createElement('div');
        div.className = 'task-item';
        div.dataset.id = item.id;
        const created = item.created ? item.created.slice(5, 16) : '';
        div.innerHTML = `
            <span class="task-text"><span style="opacity:0.5;font-size:0.75rem;">[${escapeHtml(created)}]</span> ${escapeHtml(item.text)}</span>
            <span class="task-del" data-action="remove" title="Delete note" style="cursor:pointer;">[DEL]</span>
        `;
        noteList.appendChild(div);
    });
}

if (notesBtn && notesPanel) {
    notesBtn.addEventListener('click', () => openPanel('notes-panel', 'notes-btn'));
}

if (notesClose) {
    notesClose.addEventListener('click', closeAllPanels);
}

if (noteAddBtn) {
    noteAddBtn.addEventListener('click', () => {
        const text = noteInput.value.trim();
        if (text) {
            emitNote('add', text);
            noteInput.value = '';
        }
    });
}

if (noteInput) {
    noteInput.addEventListener('keypress', (e) => {
        if (e.key === 'Enter') noteAddBtn.click();
    });
}

if (noteList) {
    noteList.addEventListener('click', (e) => {
        const target = e.target.closest('[data-action]');
        if (!target) return;
        const item = target.closest('.task-item');
        if (!item) return;
        emitNote('remove', item.dataset.id);
    });
}

socket.on('notes_update', (data) => {
    console.log('🗒️ Notes update:', data);
    renderNotes(data.items);
});

// --- REMINDER NOTIFICATION TOAST ---
const reminderToast = document.getElementById('reminder-toast');
const reminderToastText = document.getElementById('reminder-toast-text');
const reminderSnoozeBtn = document.getElementById('reminder-snooze-btn');
const reminderDismissBtn = document.getElementById('reminder-dismiss-btn');
let currentReminderId = null;

socket.on('reminder_fired', (data) => {
    console.log('⏰ Reminder fired:', data);
    if (!reminderToast) return;
    currentReminderId = data.id;
    reminderToastText.textContent = data.text || 'Reminder';
    reminderToast.style.display = 'block';
    reminderToast.classList.add('active');
    setCoreState('speaking');
    updateStatus('REMINDER');
});

if (reminderSnoozeBtn) {
    reminderSnoozeBtn.addEventListener('click', () => {
        socket.emit('snooze_reminder', { id: currentReminderId, minutes: 10 });
        hideReminderToast();
    });
}

if (reminderDismissBtn) {
    reminderDismissBtn.addEventListener('click', hideReminderToast);
}

function hideReminderToast() {
    if (!reminderToast) return;
    reminderToast.style.display = 'none';
    reminderToast.classList.remove('active');
    currentReminderId = null;
    setCoreState('active');
    updateStatus('LISTENING');
}

// --- MEMORY PANEL ---
const memoryBtn = document.getElementById('memory-btn');
const memoryPanel = document.getElementById('memory-panel');
const memoryClose = document.getElementById('memory-close');
const memoryInput = document.getElementById('memory-input');
const memoryAddBtn = document.getElementById('memory-add-btn');
const memoryCategory = document.getElementById('memory-category');
const memoryList = document.getElementById('memory-list');
const memoryStats = document.getElementById('memory-stats');

function emitMemory(action, text, category) {
    socket.emit('memory_action', { action: action, text: text, category: category });
}

function renderMemories(items, stats) {
    if (!memoryList) return;
    memoryList.innerHTML = '';
    if (stats && memoryStats) {
        memoryStats.textContent = `Total: ${stats.total} memories | Categories: ${Object.keys(stats.categories || {}).join(', ')}`;
    }
    if (!items || items.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'task-empty';
        empty.textContent = 'No memories yet, Sir.';
        memoryList.appendChild(empty);
        return;
    }
    items.forEach(item => {
        const div = document.createElement('div');
        div.className = 'memory-item';
        div.dataset.id = item.id;
        const cat = item.category || 'fact';
        div.innerHTML = `
            <span class="mem-cat">${escapeHtml(cat)}</span>
            <span class="mem-text">${escapeHtml(item.text)}</span>
            <span class="mem-del" title="Forget" style="cursor:pointer;">[X]</span>
        `;
        memoryList.appendChild(div);
    });
}

if (memoryBtn && memoryPanel) {
    memoryBtn.addEventListener('click', () => {
        openPanel('memory-panel', 'memory-btn');
        refreshRlm();
    });
}
if (memoryClose) memoryClose.addEventListener('click', closeAllPanels);
if (memoryAddBtn) {
    memoryAddBtn.addEventListener('click', () => {
        const text = memoryInput.value.trim();
        if (text) {
            emitMemory('remember', text, memoryCategory.value);
            memoryInput.value = '';
        }
    });
}
if (memoryInput) memoryInput.addEventListener('keypress', (e) => { if (e.key === 'Enter') memoryAddBtn.click(); });
if (memoryList) {
    memoryList.addEventListener('click', (e) => {
        const target = e.target.closest('.mem-del');
        if (!target) return;
        const item = target.closest('.memory-item');
        if (!item) return;
        const text = item.querySelector('.mem-text').textContent;
        emitMemory('forget', text, null);
    });
}
socket.on('memory_update', (data) => {
    renderMemories(data.items, data.stats);
    const st = document.getElementById('stat-mems');
    if (st && data.stats) st.textContent = String(data.stats.total ?? '');
});

// --- PEOPLE PANEL ---
const peopleBtn = document.getElementById('people-btn');
const peoplePanel = document.getElementById('people-panel');
const peopleClose = document.getElementById('people-close');
const personNameInput = document.getElementById('person-name-input');
const personRelInput = document.getElementById('person-rel-input');
const personAddBtn = document.getElementById('person-add-btn');
const peopleList = document.getElementById('people-list');

function emitRelationship(action, name, relationship) {
    socket.emit('relationship_action', { action: action, name: name, relationship: relationship });
}

function renderPeople(people) {
    if (!peopleList) return;
    peopleList.innerHTML = '';
    if (!people || Object.keys(people).length === 0) {
        const empty = document.createElement('div');
        empty.className = 'task-empty';
        empty.textContent = 'No people tracked yet, Sir.';
        peopleList.appendChild(empty);
        return;
    }
    Object.entries(people).forEach(([key, person]) => {
        const div = document.createElement('div');
        div.className = 'person-card';
        const hobbies = (person.hobbies || []).slice(0, 3).join(', ');
        div.innerHTML = `
            <div class="grow">
                <div class="person-name">${escapeHtml(person.name)}</div>
                <div class="person-rel">${escapeHtml(person.relationship || '')}${hobbies ? ' | ' + escapeHtml(hobbies) : ''}</div>
            </div>
            <div class="person-actions">
                <button class="tbtn" data-action="gifts" title="Gift ideas">GIFT</button>
                <button class="tbtn danger" data-action="remove" title="Remove">DEL</button>
            </div>
        `;
        div.dataset.name = person.name;
        peopleList.appendChild(div);
    });
}

if (peopleBtn && peoplePanel) {
    peopleBtn.addEventListener('click', () => openPanel('people-panel', 'people-btn'));
}
if (peopleClose) peopleClose.addEventListener('click', closeAllPanels);
if (personAddBtn) {
    personAddBtn.addEventListener('click', () => {
        const name = personNameInput.value.trim();
        const rel = personRelInput.value.trim();
        if (name) {
            emitRelationship('add', name, rel);
            personNameInput.value = '';
            personRelInput.value = '';
        }
    });
}
if (peopleList) {
    peopleList.addEventListener('click', (e) => {
        const btn = e.target.closest('[data-action]');
        if (!btn) return;
        const card = btn.closest('.person-card');
        if (!card) return;
        const action = btn.dataset.action;
        const name = card.dataset.name;
        if (action === 'gifts') {
            sendText(`gift ideas for ${name}`);
        } else if (action === 'remove') {
            emitRelationship('remove', name);
        }
    });
}
socket.on('relationships_update', (data) => {
    console.log('👥 Relationships update:', data);
    renderPeople(data.people);
});

// --- MEETING MODE ---
const meetingBtn = document.getElementById('meeting-btn');
const meetingPanel = document.getElementById('meeting-panel');
const meetingClose = document.getElementById('meeting-close');
const meetingStartBtn = document.getElementById('meeting-start-btn');
const meetingStopBtn = document.getElementById('meeting-stop-btn');
const meetingStatus = document.getElementById('meeting-status');
const meetingTranscript = document.getElementById('meeting-transcript');
let meetingActive = false;
let meetingInterval = null;

if (meetingBtn && meetingPanel) {
    meetingBtn.addEventListener('click', () => openPanel('meeting-panel', 'meeting-btn'));
}
if (meetingClose) meetingClose.addEventListener('click', closeAllPanels);

if (meetingStartBtn) {
    meetingStartBtn.addEventListener('click', () => {
        socket.emit('meeting_action', { action: 'start' });
        meetingActive = true;
        meetingStartBtn.style.display = 'none';
        meetingStopBtn.style.display = 'block';
        meetingStatus.textContent = '● RECORDING';
        meetingStatus.style.color = 'var(--fail)';
        if (meetingTranscript) meetingTranscript.textContent = '';
        document.body.classList.add('meeting-active');
        // Poll transcript every 5 seconds (lands in the drawer only)
        meetingInterval = setInterval(() => {
            socket.emit('meeting_action', { action: 'transcript' });
        }, 5000);
    });
}

if (meetingStopBtn) {
    meetingStopBtn.addEventListener('click', () => {
        socket.emit('meeting_action', { action: 'stop' });
        meetingActive = false;
        meetingStopBtn.style.display = 'none';
        meetingStartBtn.style.display = 'block';
        meetingStatus.textContent = 'PROCESSED — TRANSCRIPT BELOW';
        meetingStatus.style.color = '';
        document.body.classList.remove('meeting-active');
        if (meetingInterval) { clearInterval(meetingInterval); meetingInterval = null; }
    });
}

// Live meeting transcript renders into the drawer, never into the chat feed.
socket.on('meeting_transcript', (data) => {
    if (!meetingTranscript) return;
    const text = (data && data.text) ? String(data.text).trim() : '';
    if (!text) return;
    meetingTranscript.textContent = text;
});

// --- PRIVACY PANEL ---
const privacyBtn = document.getElementById('privacy-btn');
const privacyPanel = document.getElementById('privacy-panel');
const privacyClose = document.getElementById('privacy-close');
const privacySettings = document.getElementById('privacy-settings');

const TRUST_CATEGORIES = ['read', 'minor', 'medium', 'high', 'critical'];
const TRUST_LABELS = { read: 'READ', minor: 'MINOR', medium: 'MEDIUM',
                       high: 'HIGH', critical: 'CRITICAL' };
const TRUST_OPTIONS = ['always', 'ask', 'deny', 'preview'];
const SCOPE_FLAGS = [
    ['allow_analyze_calendar', 'ANALYZE CALENDAR'],
    ['allow_analyze_email', 'ANALYZE EMAIL'],
    ['allow_analyze_files', 'ANALYZE FILES'],
    ['allow_web_search', 'WEB SEARCH'],
    ['allow_store_conversations', 'STORE CONVERSATIONS'],
    ['prefer_local_processing', 'LOCAL PROCESSING'],
    ['auto_approve_minor', 'AUTO-APPROVE MINOR']
];

function renderPrivacy(d) {
    if (!privacySettings) return;
    const settings = (d && d.settings) || {};
    const audit = (d && d.audit) || [];
    const summary = (d && d.summary) || '';
    const trust = (settings.trust_levels) || {};
    let html = '<div class="d-subhead" style="margin-top:0"><i class="tag">TRUST</i><span>ACTION TRUST LEVELS</span></div>';
    TRUST_CATEGORIES.forEach(cat => {
        const cur = trust[cat] || 'ask';
        html += `<div class="setting-row">
            <label>${TRUST_LABELS[cat] || cat.toUpperCase()}</label>
            <span>${TRUST_OPTIONS.map(opt =>
                `<button class="tbtn${cur === opt ? ' ok' : ''}" data-trust="${cat}" data-val="${opt}">${opt.toUpperCase()}</button>`).join('')}</span>
        </div>`;
    });
    html += '<div class="d-subhead"><i class="tag">SCOPE</i><span>DATA &amp; PROCESSING</span></div>';
    SCOPE_FLAGS.forEach(([k, label]) => {
        const on = !!settings[k];
        html += `<div class="setting-row">
            <label>${label}</label>
            <button class="tbtn${on ? ' ok' : ''}" data-flag="${k}" data-val="${on ? '0' : '1'}">${on ? '[ ON ]' : '[ OFF ]'}</button>
        </div>`;
    });
    if (summary) {
        html += '<div class="d-subhead"><i class="tag">SUM</i><span>TRUST SUMMARY</span></div>' +
            `<div class="rlm-row"><span class="rt" style="white-space:pre-wrap">${escapeHtml(String(summary))}</span></div>`;
    }
    if (audit.length) {
        html += '<div class="d-subhead"><i class="tag">AUD</i><span>LAST ACTIONS</span></div>' +
            audit.map(a =>
                `<div class="rlm-row"><span class="rl">${escapeHtml(String(a.date || '').slice(11, 19))}</span><span class="rt">${escapeHtml(a.action || '')}${a.details ? ' — ' + escapeHtml(String(a.details).slice(0, 80)) : ''}</span></div>`).join('');
    }
    privacySettings.innerHTML = html;
}

if (privacyBtn && privacyPanel) {
    privacyBtn.addEventListener('click', () => {
        openPanel('privacy-panel', 'privacy-btn');
        socket.emit('privacy_action', { action: 'get' });
    });
}
if (privacyClose) privacyClose.addEventListener('click', closeAllPanels);

if (privacySettings) {
    privacySettings.addEventListener('click', (e) => {
        const trust = e.target.closest('[data-trust]');
        const flag = e.target.closest('[data-flag]');
        if (trust) {
            socket.emit('privacy_action', {
                action: 'update_trust', key: trust.dataset.trust, value: trust.dataset.val });
        } else if (flag) {
            socket.emit('privacy_action', {
                action: 'update', key: flag.dataset.flag, value: flag.dataset.val === '1' });
        }
    });
}
socket.on('privacy_update', (data) => {
    renderPrivacy(data || {});
});

// --- MISSIONS · OPS RAIL (dense) ---
const missionsList = document.getElementById('missions-list');
const missionsHistoryList = document.getElementById('missions-history-list');
const missionsHistoryStats = document.getElementById('missions-history-stats');
const missionsHistoryWrap = document.getElementById('missions-history-wrap');
let currentTasks = [];
let currentHistory = [];
let currentHistoryStats = null;

const CHIP = {
    pending:  '[PEND]', running: '[RUN]', paused: '[HOLD]',
    completed: '[ OK ]', failed: '[FAIL]', cancelled: '[CANC]',
    skipped: '[SKIP]'
};
const CHIPCLS = {
    pending: '', running: 'run', paused: 'warn',
    completed: 'ok', failed: 'fail', cancelled: '', skipped: 'skip'
};

function chip(status) {
    return `<span class="chip ${CHIPCLS[status] || ''}">${CHIP[status] || '[????]'}</span>`;
}

function flashStack(stackId) {
    const el = document.getElementById(stackId);
    if (!el) return;
    el.scrollIntoView({ block: 'start' });
    el.style.background = 'var(--accent-dim)';
    setTimeout(() => { el.style.background = ''; }, 350);
}

function renderMissions(tasks) {
    if (!missionsList) return;
    currentTasks = tasks || [];
    const st = document.getElementById('stat-tasks');
    if (st) st.textContent = String(currentTasks.length);

    if (!currentTasks.length) {
        missionsList.innerHTML = '<div class="no-data">no active missions</div>';
        return;
    }
    missionsList.innerHTML = currentTasks.map(task => {
        const pct = task.progress || 0;
        const done = task.steps ? task.steps.filter(s => s.status === 'completed').length : 0;
        const total = task.steps ? task.steps.length : 0;
        const cost = task.cost_usd ? ` $${task.cost_usd.toFixed(4)}` : '';
        const mode = task.has_dependencies ? 'DAG' : 'SEQ';
        const steps = (task.steps || []).map(s =>
            `<div class="mstep">${chip(s.status)}<span>${escapeHtml(s.text.slice(0, 64))}</span>${s.error ? `<span class="e" title="${escapeHtml(s.error)}">ERR</span>` : ''}</div>`
        ).join('');
        const summary = task.final_summary
            ? `<div class="msum">${escapeHtml(task.final_summary.slice(0, 160))}</div>` : '';
        const isActive = ['pending', 'running', 'paused'].includes(task.status);
        const actions = isActive
            ? `<div class="mission-actions">
                 <button class="tbtn" data-action="${task.status === 'paused' ? 'resume' : 'pause'}">${task.status === 'paused' ? '[RESUME]' : '[HOLD]'}</button>
                 <button class="tbtn danger" data-action="cancel">[CANCEL]</button>
               </div>` : '';
        return `<details class="mission" data-task-id="${task.id}">
            <summary>
                ${chip(task.status)}
                <span class="m-desc">${escapeHtml(task.description.slice(0, 46))}</span>
                <span class="m-meta">${done}/${total} ${pct}%${cost} ${mode}</span>
            </summary>
            <div class="mission-body">${steps}${summary}</div>
            ${actions}
        </details>`;
    }).join('');
}

function renderHistory(tasks, stats) {
    if (!missionsHistoryList) return;
    currentHistory = tasks || [];
    currentHistoryStats = stats || null;

    if (missionsHistoryStats && stats) {
        const s = stats;
        const rate = s.total > 0 ? Math.round(s.completed / s.total * 100) : 0;
        missionsHistoryStats.innerHTML =
            `<div class="kv"><span class="k">TOTAL</span><span class="v num">${s.total}</span></div>` +
            `<div class="kv"><span class="k">OK</span><span class="v num">${s.completed} (${rate}%)</span></div>` +
            `<div class="kv"><span class="k">FAIL</span><span class="v num">${s.failed}</span></div>`;
    }

    if (!currentHistory.length) {
        missionsHistoryList.innerHTML = '<div class="no-data">no history yet</div>';
        return;
    }
    missionsHistoryList.innerHTML = currentHistory.map(task => {
        const total = task.steps ? task.steps.length : 0;
        const done = task.steps ? task.steps.filter(s => s.status === 'completed').length : 0;
        const cost = task.cost_usd ? ` · $${task.cost_usd.toFixed(4)}` : '';
        const dur = task.created_at && task.completed_at ? formatDuration(task.created_at, task.completed_at) : '';
        const when = task.completed_at ? task.completed_at.slice(5, 16).replace('T', ' ') : '';
        const steps = (task.steps || []).map(s =>
            `<div class="mstep">${chip(s.status)}<span>${escapeHtml(s.text.slice(0, 60))}</span></div>`
        ).join('');
        const summary = task.final_summary
            ? `<div class="msum">${escapeHtml(task.final_summary.slice(0, 140))}</div>` : '';
        return `<details class="mission" data-task-id="${task.id}">
            <summary>
                ${chip(task.status)}
                <span class="m-desc">${escapeHtml(task.description.slice(0, 42))}</span>
                <span class="m-meta">${done}/${total}${cost}</span>
            </summary>
            <div class="mission-body">${steps}${when ? `<div>${when}${dur ? ' · ' + dur : ''}</div>` : ''}${summary}</div>
        </details>`;
    }).join('');
}

function formatTaskTime(isoString) {
    try {
        const d = new Date(isoString);
        const now = new Date();
        const diffMs = now - d;
        const diffMins = Math.floor(diffMs / 60000);
        const diffHrs = Math.floor(diffMs / 3600000);
        const diffDays = Math.floor(diffMs / 86400000);
        if (diffMins < 1) return 'now';
        if (diffMins < 60) return `${diffMins}m ago`;
        if (diffHrs < 24) return `${diffHrs}h ago`;
        if (diffDays < 7) return `${diffDays}d ago`;
        return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
    } catch { return ''; }
}

function formatDuration(startIso, endIso) {
    try {
        const start = new Date(startIso);
        const end = new Date(endIso);
        const diffMs = end - start;
        if (diffMs < 0) return '';
        const secs = Math.floor(diffMs / 1000);
        if (secs < 60) return `${secs}s`;
        const mins = Math.floor(secs / 60);
        const remSecs = secs % 60;
        return `${mins}m ${remSecs}s`;
    } catch { return ''; }
}

// missions stack controls
const misTabActive = document.getElementById('mis-tab-active');
const misTabHist = document.getElementById('mis-tab-hist');
function setMisView(view) {
    if (misTabActive) misTabActive.style.color = view === 'active' ? 'var(--accent)' : '';
    if (misTabHist) misTabHist.style.color = view === 'history' ? 'var(--accent)' : '';
    if (missionsList) missionsList.style.display = view === 'active' ? '' : 'none';
    if (missionsHistoryWrap) missionsHistoryWrap.style.display = view === 'history' ? '' : 'none';
    if (view === 'history') socket.emit('complex_task_action', { action: 'history' });
}
if (misTabActive) misTabActive.addEventListener('click', () => setMisView('active'));
if (misTabHist) misTabHist.addEventListener('click', () => setMisView('history'));

if (missionsList) {
    missionsList.addEventListener('click', (e) => {
        const btn = e.target.closest('.tbtn[data-action]');
        if (!btn) return;
        const card = btn.closest('[data-task-id]');
        if (!card) return;
        socket.emit('complex_task_action',
                    { action: btn.dataset.action, task_id: card.dataset.taskId });
    });
}
socket.on('task_update', (data) => {
    const idx = currentTasks.findIndex(t => t.id === data.id);
    if (idx >= 0) currentTasks[idx] = data;
    else currentTasks.unshift(data);
    renderMissions(currentTasks);
});
socket.on('tasks_list', (data) => {
    currentTasks = data.tasks || [];
    renderMissions(currentTasks);
});
socket.on('task_step_started', () => {});
socket.on('task_step_completed', () => {});
socket.on('task_step_failed', () => {});
socket.on('task_reflection', (data) => {
    showToast('CRITIC', `${data.assessment || 'analyzing'} — recovery ${data.child_id} (${data.steps} steps)`, 'reflection');
});
socket.on('tasks_history', (data) => {
    renderHistory(data.tasks, data.stats);
});
socket.emit('complex_task_action', { action: 'list' });

// --- VOICE INPUT (browser mic: cmd-zone [TALK] + mobile FAB) ---
const mobileMicBtn = document.getElementById('mobile-mic-btn');

function _micButtons() {
    return document.querySelectorAll('#mic-btn, #mobile-mic-btn');
}
function _micClear() {
    _micButtons().forEach(b => b.classList.remove('listening'));
}
function voiceStop() {
    try { if (recognition) recognition.stop(); } catch (_) {}
}
function voiceStart() {
    if (!recognition) {
        addMessage('SYSTEM: Speech not supported in this browser');
        return;
    }
    wakeUp();
    addMessage('SYSTEM: Listening…');
    _micClear();
    const btn = document.getElementById('mic-btn');
    if (btn) btn.classList.add('listening');
    try {
        recognition.start();
    } catch (e) {
        _micClear();
        const msg = String((e && e.message) || e || '');
        if (!msg.includes('already started')) {
            addMessage('SYSTEM: Mic error — ' + msg);
        }
    }
}
function voiceToggle() {
    const micBtnEl = document.getElementById('mic-btn');
    const active = (micBtnEl && micBtnEl.classList.contains('listening')) ||
                   (mobileMicBtn && mobileMicBtn.classList.contains('listening'));
    if (active) voiceStop();
    else voiceStart();
}

const micBtnEl = document.getElementById('mic-btn');
if (micBtnEl) micBtnEl.addEventListener('click', voiceToggle);
if (mobileMicBtn) mobileMicBtn.addEventListener('click', voiceToggle);

// Keep every mic affordance in sync with the recognizer's lifecycle.
if (recognition) {
    const baseOnEnd = recognition.onend;
    const baseOnError = recognition.onerror;
    recognition.onend = () => { if (baseOnEnd) baseOnEnd(); _micClear(); };
    recognition.onerror = (e) => { if (baseOnError) baseOnError(e); _micClear(); };
}


// --- UI HELPERS ---

function setCoreState(state) {
    // States: idle, active (listening), speaking, processing
    const dial = core ? core.closest('.reactor') : null;
    if (!dial) return;
    dial.classList.remove('listening', 'speaking');
    if (window._waveSpeak) window._waveSpeak(state === 'speaking' || state === 'processing');
    if (state === 'active') dial.classList.add('listening');
    else if (state === 'speaking' || state === 'processing') dial.classList.add('speaking');
}

function updateStatus(text, type = 'normal') {
    if (!statusText) return;
    statusText.innerText = String(text || '').toUpperCase();
    statusText.style.color = type === 'online' ? 'var(--ok)'
        : type === 'offline' ? 'var(--fail)' : 'var(--text-3)';
}

function formatMarkdown(text) {
    if (!text) return '';
    let escaped = text
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
    
    escaped = escaped.replace(/```([\s\S]*?)```/g, '<pre style="background:rgba(0,0,0,0.4);padding:8px 12px;border-radius:4px;border:1px solid rgba(255,255,255,0.1);overflow-x:auto;margin:6px 0;font-family:var(--font-mono);font-size:12px;"><code>$1</code></pre>');
    escaped = escaped.replace(/`([^`]+)`/g, '<code style="background:rgba(255,255,255,0.08);padding:2px 6px;border-radius:3px;font-family:var(--font-mono);font-size:12px;">$1</code>');
    escaped = escaped.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    escaped = escaped.replace(/\*([^*]+)\*/g, '<em>$1</em>');
    escaped = escaped.replace(/\n/g, '<br>');
    return escaped;
}

function quickPrompt(text) {
    if (chatInput) {
        chatInput.value = text;
        chatInput.focus();
        if (typeof sendText === 'function') {
            sendText(text);
        } else if (sendBtn) {
            sendBtn.click();
        }
    }
}
window.quickPrompt = quickPrompt;

function addMessage(text) {
    if (!messageContainer) return;
    const intro = document.getElementById('intro-msg');
    if (intro) intro.remove();

    let dir = 'j', clean = String(text || '');
    if (clean.startsWith('YOU:')) { dir = 'u'; clean = clean.replace(/^YOU:\s*/i, ''); }
    else if (clean.startsWith('JARVIS:')) { clean = clean.replace(/^JARVIS:\s*/i, ''); }
    else if (clean.startsWith('SYSTEM:')) { dir = 'sys'; clean = clean.replace(/^SYSTEM:\s*/i, ''); }
    else if (clean.startsWith('ERROR:')) { dir = 'sys'; clean = 'ERR — ' + clean.replace(/^ERROR:\s*/i, ''); }

    const ts = new Date().toLocaleTimeString('en-US', { hour12: false });
    const mark = dir === 'u' ? '\u00BB' : dir === 'sys' ? '\u00B7' : '\u00AB';
    const row = document.createElement('div');
    row.className = 'msg ' + dir;
    const bodyHtml = dir === 'sys' ? escapeHtml(clean) : formatMarkdown(clean);
    row.innerHTML = `<span class="ts num">${ts}</span><span class="dir">${mark}</span><span class="body ${dir === 'sys' ? '' : 'prose'}">${bodyHtml}</span>`;

    if (messageContainer.children.length > 80) {
        messageContainer.removeChild(messageContainer.firstElementChild);
    }
    messageContainer.appendChild(row);
    messageContainer.scrollTop = messageContainer.scrollHeight;
    if (window._updateHero) window._updateHero();
}

// --- LIBRARIAN MODE LOGIC ---
const librarianBtn = document.getElementById('librarian-btn');
const librarianPanel = document.getElementById('librarian-panel');
const librarianClose = document.getElementById('librarian-close');
const dropZone = document.getElementById('drop-zone');
const knowledgeInput = document.getElementById('knowledge-upload');
const fileList = document.getElementById('file-list');
const feedBtn = document.getElementById('feed-btn');

let selectedFiles = [];

// Toggle Panel
if (librarianBtn) {
    librarianBtn.addEventListener('click', () => openPanel('librarian-panel', 'librarian-btn'));
}

if (librarianClose) {
    librarianClose.addEventListener('click', closeAllPanels);
}

// Drag & Drop Handling
if (dropZone) {
    dropZone.addEventListener('click', () => knowledgeInput.click());

    dropZone.addEventListener('dragover', (e) => {
        e.preventDefault();
        dropZone.classList.add('drag-over');
    });

    dropZone.addEventListener('dragleave', () => {
        dropZone.classList.remove('drag-over');
    });

    dropZone.addEventListener('drop', (e) => {
        e.preventDefault();
        dropZone.classList.remove('drag-over');
        handleFiles(e.dataTransfer.files);
    });

    knowledgeInput.addEventListener('change', (e) => {
        handleFiles(e.target.files);
    });
}

function handleFiles(files) {
    for (let file of files) {
        // Prevent duplicates
        if (!selectedFiles.some(f => f.name === file.name)) {
            selectedFiles.push(file);
        }
    }
    updateFileList();
}

function updateFileList() {
    fileList.innerHTML = '';
    selectedFiles.forEach((file, index) => {
        const item = document.createElement('div');
        item.className = 'file-item';
        item.innerHTML = `
            <span>${file.name}</span>
            <span class="remove-file" style="cursor:pointer;" onclick="removeFile(${index})">[DEL]</span>
        `;
        fileList.appendChild(item);
    });

    if (feedBtn) {
        feedBtn.disabled = selectedFiles.length === 0;
    }
}

// Global function to remove file
window.removeFile = function (index) {
    selectedFiles.splice(index, 1);
    updateFileList();
}

// Feed Button Logic
if (feedBtn) {
    feedBtn.addEventListener('click', async () => {
        if (selectedFiles.length === 0) return;

        feedBtn.disabled = true;
        feedBtn.innerHTML = 'PROCESSING…';
        updateStatus("Ingesting Knowledge...");
        setCoreState('processing');

        const formData = new FormData();
        selectedFiles.forEach(file => {
            formData.append('files', file);
        });

        try {
            const response = await fetch('/upload_knowledge', {
                method: 'POST',
                body: formData
            });

            const result = await response.json();

            if (result.success) {
                addMessage(`SYSTEM: ${result.message}`);
                updateStatus("Knowledge Integration Complete");
                setCoreState('active'); // Back to normal

                selectedFiles = []; // Clear files
                updateFileList();

                // Close panel after success
                setTimeout(closeAllPanels, 1500);
            } else {
                addMessage(`ERROR: ${result.error}`);
                updateStatus("Ingestion Failed");
                setCoreState('active');
            }
        } catch (error) {
            console.error('Upload error:', error);
            addMessage("SYSTEM: Upload Failed");
            updateStatus("Connection Error");
        } finally {
            feedBtn.innerHTML = 'FEED JARVIS';
            // Only re-enable if there are files left (which there shouldn't be if success)
            if (selectedFiles.length > 0) feedBtn.disabled = false;
        }
    });
}

// STUDY PERMANENTLY — same upload, but stored in the knowledge vault
// (server ingests with store=True) instead of the 30-minute feed memory.
const studyBtn = document.getElementById('study-btn');
if (studyBtn) {
    studyBtn.addEventListener('click', () => {
        if (selectedFiles.length === 0) {
            showToast('NO FILES', 'Drop a pdf/txt/md first, Sir.', 'warn', 2600);
            return;
        }
        studyBtn.disabled = true;
        const orig = studyBtn.textContent;
        studyBtn.textContent = 'STUDYING…';
        updateStatus('Studying Knowledge…');
        setCoreState('processing');

        const formData = new FormData();
        selectedFiles.forEach(f => formData.append('files', f));
        formData.append('study', '1');

        fetch('/upload_knowledge', { method: 'POST', body: formData })
            .then(r => r.json())
            .then(result => {
                if (result.success) {
                    addMessage(`SYSTEM: ${result.message}`);
                    updateStatus('Knowledge Stored Permanently');
                    setCoreState('active');
                    selectedFiles = [];
                    updateFileList();
                    setTimeout(closeAllPanels, 1500);
                } else {
                    addMessage(`ERROR: ${result.error}`);
                    updateStatus('Ingestion Failed');
                    setCoreState('active');
                }
            })
            .catch(err => {
                console.error('Study upload error:', err);
                addMessage('SYSTEM: Study Upload Failed');
                updateStatus('Connection Error');
                setCoreState('active');
            })
            .finally(() => {
                studyBtn.textContent = orig;
                if (selectedFiles.length > 0) studyBtn.disabled = false;
            });
    });
}


/* ── WAVEFORM + HERO VISIBILITY ─────────────────────────────────────── */
(function () {
    const bars = document.querySelectorAll('#wave i');
    const n = bars.length;
    let t0 = performance.now();
    let speaking = false;
    window._waveSpeak = (on) => { speaking = !!on; };

    function frame(now) {
        const dt = (now - t0) / 1000;
        const amp = speaking ? 11 : 3.5;
        bars.forEach((b, i) => {
            const v = Math.abs(Math.sin(dt * 3.1 + i * 0.55)) *
                      Math.abs(Math.sin(dt * 7.3 + i * 1.3));
            b.style.height = (2 + v * amp + (speaking ? Math.random() * 2 : 0)).toFixed(1) + 'px';
            b.style.opacity = speaking ? .9 : .4 + v * .3;
        });
        setTimeout(() => requestAnimationFrame(frame), 90);
    }
    if (bars.length) requestAnimationFrame(frame);

    // hero visibility: hide once real conversation flows
    window._updateHero = function () {
        const feed = document.querySelector('.feed');
        const hero = document.getElementById('hero');
        if (!feed || !hero) return;
        const real = feed.querySelectorAll('.msg:not(.intro)').length;
        feed.classList.toggle('busy', real > 2);
    };
})();

/* ====================================================================== *
 * OPS DECK runtime — toasts, ops-rail stacks, palette, ticker, widgets
 * ====================================================================== */
(function () {
    const $ = (id) => document.getElementById(id);

    /* ── 1 · TOASTS ────────────────────────────────────────────────── */
    const toastStack = $('toast-stack');

    window.showToast = function showToast(title, text, kind, timeout) {
        if (!toastStack) return;
        const el = document.createElement('div');
        el.className = `toast ${kind || ''}`;
        el.innerHTML =
            `<div class="tt">[${(kind || 'INFO').toUpperCase()}] ${escapeHtml(title)}</div>` +
            `<div class="tb">${escapeHtml(String(text || '')).slice(0, 400)}</div>`;
        toastStack.appendChild(el);
        requestAnimationFrame(() => el.classList.add('show'));
        const ttl = timeout || (kind === 'error' ? 9000 : 6000);
        setTimeout(() => { el.classList.remove('show'); setTimeout(() => el.remove(), 120); }, ttl);
    };

    socket.on('automation_fired', (data) => {
        showToast('AUTOMATION', `#${data.id} ${data.action}${data.result ? '\n\n' + data.result : ''}`, 'automation', 8000);
        refreshAutomations();
    });

    /* ── 2b · PROACTIVE FEED (goals, tasks, findings — unprompted) ── */
    socket.on('proactive', (data) => {
        const pri = (data && data.priority) || 'normal';
        const kind = pri === 'high' ? 'error' : 'automation';
        showToast('JARVIS', (data && data.text) || '', kind, pri === 'high' ? 12000 : 8000);
    });

    /* ── 2c · GOALS STACK ─────────────────────────────────────────── */
    const goalsList = $('goals-list');
    let currentGoals = [];

    function renderGoals(goals) {
        if (!goalsList) return;
        currentGoals = goals || [];
        const st = document.getElementById('stat-tasks');
        if (currentGoals.length) {
            goalsList.innerHTML = currentGoals.map(g => {
                const over = g.status === 'overdue';
                const done = g.status === 'done';
                const pct = g.progress || 0;
                const dl = g.deadline ? g.deadline.slice(5, 16).replace('T', ' ') : 'no deadline';
                return `<div class="row" data-goal-id="${escapeHtml(g.id)}">
                    <div class="r1">
                        <span class="t" title="${escapeHtml(g.title)}">${over ? '⏰ ' : ''}${escapeHtml(g.title.slice(0, 44))}</span>
                        <span class="m">${pct}% · ${escapeHtml(dl)}${done ? ' · DONE' : ''}</span>
                    </div>
                    <div class="r2">
                        <span class="btns" style="margin-left:auto;">
                            ${!done ? `<button class="tbtn" data-goal-done="${escapeHtml(g.id)}">DONE</button>` : ''}
                            ${!done ? `<button class="tbtn danger" data-goal-drop="${escapeHtml(g.id)}">DROP</button>` : ''}
                        </span>
                    </div>
                </div>`;
            }).join('');
        } else {
            goalsList.innerHTML = '<div class="no-data">no open goals · say "my goal is …"</div>';
        }
        void st;
    }

    function refreshGoals() {
        socket.emit('goals_action', { action: 'list' });
    }
    socket.on('goals_update', (data) => {
        renderGoals(data.goals || []);
    });

    const goalsRefresh = $('goals-refresh');
    if (goalsRefresh) goalsRefresh.addEventListener('click', refreshGoals);
    if (goalsList) goalsList.addEventListener('click', (e) => {
        const doneBtn = e.target.closest('[data-goal-done]');
        const dropBtn = e.target.closest('[data-goal-drop]');
        if (doneBtn) socket.emit('goals_action', { action: 'done', goal: doneBtn.dataset.goalDone });
        else if (dropBtn) socket.emit('goals_action', { action: 'drop', goal: dropBtn.dataset.goalDrop });
    });
    refreshGoals();

    /* ── 1b · CAPABILITIES STACK ───────────────────────────────────── */
    const capsList = $('caps-list');

    function renderCaps(caps) {
        if (!capsList) return;
        if (!caps || !caps.length) {
            capsList.innerHTML = '<div class="no-data">loading…</div>';
            return;
        }
        const grouped = {};
        caps.forEach(c => {
            const cat = c.category || 'other';
            if (!grouped[cat]) grouped[cat] = [];
            grouped[cat].push(c);
        });
        let html = '';
        for (const [cat, items] of Object.entries(grouped)) {
            html += `<div class="cap-cat">${escapeHtml(cat.toUpperCase())}</div>`;
            items.forEach(c => {
                const icon = c.status === 'ready' ? '[ OK ]' : c.status === 'degraded' ? '[WARN]' : '[FAIL]';
                const acquireBtn = c.status !== 'ready'
                    ? ` <button class="tbtn cap-acquire" data-cap="${escapeHtml(c.name)}">ACQUIRE</button>`
                    : '';
                html += `<div class="row cap-row">
                    <div class="r1">
                        <span class="t">${icon} ${escapeHtml(c.name)}</span>
                        <span class="m">${escapeHtml(c.detail || '')}</span>
                        <span class="btns" style="margin-left:auto;">${acquireBtn}</span>
                    </div>
                </div>`;
            });
        }
        capsList.innerHTML = html;
    }

    function refreshCaps() {
        fetch('/api/capabilities').then(r => r.json()).then(data => {
            renderCaps(data.capabilities || []);
        }).catch(() => {});
    }

    const capsRefresh = $('caps-refresh');
    if (capsRefresh) capsRefresh.addEventListener('click', refreshCaps);
    // ACQUIRE button: expand a missing capability
    if (capsList) capsList.addEventListener('click', (e) => {
        const btn = e.target.closest('.cap-acquire');
        if (!btn) return;
        const capName = btn.dataset.cap;
        btn.textContent = '...';
        btn.disabled = true;
        fetch('/api/capabilities/expand', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({capabilities: [capName], auto: true})
        }).then(r => r.json()).then(data => {
            if (data.error) {
                btn.textContent = 'FAILED';
            } else {
                btn.textContent = '✓ PLAN CREATED';
                // Refresh goals stack if it exists
                if (typeof refreshGoals === 'function') refreshGoals();
                // Show the plan in a toast
                if (data.formatted) {
                    socket.emit('send_message', {message: data.formatted});
                }
            }
        }).catch(() => { btn.textContent = 'ERROR'; });
    });
    refreshCaps();

    /* ── 1c · MOBILE LINK STACK ────────────────────────────────────── */
    const mobileList = $('mobile-list');
    const mobileCode = $('mobile-code');
    const mobilePairBtn = $('mobile-pair-btn');
    const mobileQr = $('mobile-qr');
    const mobilePairLink = $('mobile-link');

    // Shareable pair link for the Lite app / QR scan:
    //   jarvis://pair?hub=<origin>&code=<8-CHAR>
    // The link carries no secret — the code is single-use, 10-min TTL,
    // redeemed over POST /api/mobile/redeem like the PWA manual entry.
    function renderPairShare(code) {
        if (!mobileQr && !mobilePairLink) return;
        var hub = '';
        try { hub = window.location.origin || ''; } catch (e) {}
        if (!hub || hub.indexOf('http') !== 0) {
            if (mobilePairLink) {
                mobilePairLink.style.display = '';
                mobilePairLink.innerHTML = '<span class="m">open the dashboard via LAN IP / Tailscale name so the QR encodes a phone-reachable hub</span>';
            }
            return;
        }
        var link = 'jarvis://pair?hub=' + encodeURIComponent(hub) +
            '&code=' + encodeURIComponent(code);
        // Loopback origins are unreachable from a phone — a QR/link
        // encoding 127.0.0.1 would only waste the single-use code. Warn
        // and stop instead of rendering an unscannable share.
        if (/(^|\/\/)(127\.0\.0\.1|localhost|\[::1\])(:|\/|$)/.test(hub)) {
            if (mobileQr) { mobileQr.style.display = 'none'; mobileQr.innerHTML = ''; }
            if (mobilePairLink) {
                mobilePairLink.style.display = '';
                mobilePairLink.innerHTML = '<span class="m">⚠ dashboard is on loopback (' +
                    escapeHtml(hub) + ') — phones cannot reach it. Re-open the dashboard via LAN IP / Tailscale name, then PAIR again.</span>';
            }
            return;
        }
        if (mobileQr) {
            mobileQr.style.display = '';
            mobileQr.innerHTML = '';
            try {
                if (window.QRCode) {
                    new window.QRCode(mobileQr, {
                        text: link, width: 140, height: 140,
                        correctLevel: window.QRCode.CorrectLevel.M });
                } else {
                    mobileQr.innerHTML = '<span class="m">QR lib offline — use the code</span>';
                }
            } catch (e) {
                mobileQr.innerHTML = '<span class="m">QR failed — use the code</span>';
            }
        }
        if (mobilePairLink) {
            mobilePairLink.style.display = '';
            mobilePairLink.innerHTML = ''
                + '<div>' + escapeHtml(link) + '</div>'
                + '<button class="tbtn" id="mob-copy-link">COPY LINK</button>';
            var copyBtn = document.getElementById('mob-copy-link');
            if (copyBtn) copyBtn.addEventListener('click', function () {
                var done = function (ok) {
                    copyBtn.textContent = ok ? 'COPIED' : 'COPY FAILED';
                    setTimeout(function () { copyBtn.textContent = 'COPY LINK'; }, 1600);
                };
                try {
                    if (navigator.clipboard && navigator.clipboard.writeText) {
                        navigator.clipboard.writeText(link).then(
                            function () { done(true); },
                            function () { done(false); });
                    } else { done(false); }
                } catch (e) { done(false); }
            });
        }
    }

    function clearPairShare() {
        if (mobileQr) { mobileQr.style.display = 'none'; mobileQr.innerHTML = ''; }
        if (mobilePairLink) { mobilePairLink.style.display = 'none'; mobilePairLink.innerHTML = ''; }
    }

    function renderMobile(devices) {
        if (!mobileList) return;
        if (!devices || !devices.length) {
            mobileList.innerHTML = '<div class="no-data">no paired phones</div>';
            return;
        }
        mobileList.innerHTML = devices.map(d => {
            const seen = d.last_seen
                ? new Date(d.last_seen * 1000).toLocaleString() : 'never';
            return `<div class="row mob-row"><div class="r1">`
                + `<span class="t">📱 ${escapeHtml(d.name || d.device_id)}</span>`
                + `<span class="m">seen ${escapeHtml(seen)}</span>`
                + `<span class="btns" style="margin-left:auto;">`
                + `<button class="tbtn mob-revoke" data-dev="${escapeHtml(d.device_id)}">REVOKE</button>`
                + `</span></div></div>`;
        }).join('');
    }

    function refreshMobile() {
        fetch('/api/mobile/devices').then(r => r.json()).then(data => {
            renderMobile(data.devices || []);
        }).catch(() => {});
    }

    if (mobilePairBtn) mobilePairBtn.addEventListener('click', () => {
        mobilePairBtn.textContent = '...';
        fetch('/api/mobile/pair', {method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({})}).then(r => r.json()).then(data => {
            mobilePairBtn.textContent = 'PAIR';
            if (!mobileCode) return;
            if (data.ok) {
                const ttl = data.expires_at
                    ? new Date(data.expires_at * 1000).toLocaleTimeString() : '';
                mobileCode.style.display = '';
                mobileCode.innerHTML = `CODE <b>${escapeHtml(data.code)}</b>`
                    + ` <span class="m">expires ${escapeHtml(ttl)} · single-use</span>`;
                renderPairShare(data.code);
            } else {
                clearPairShare();
                mobileCode.style.display = '';
                mobileCode.innerHTML = `<span class="m">pairing failed: `
                    + `${escapeHtml(data.error || 'unknown error')}</span>`;
            }
        }).catch(() => { mobilePairBtn.textContent = 'ERROR'; });
    });
    if (mobileList) mobileList.addEventListener('click', (e) => {
        const btn = e.target.closest('.mob-revoke');
        if (!btn) return;
        btn.textContent = '...';
        btn.disabled = true;
        fetch('/api/mobile/devices/' + encodeURIComponent(btn.dataset.dev),
            {method: 'DELETE'}).then(r => r.json()).then(() => refreshMobile())
            .catch(() => { btn.textContent = 'ERROR'; });
    });
    refreshMobile();

    /* ── 2 · AUTOMATIONS STACK ─────────────────────────────────────── */
    const automationList = $('automation-list');
    let automationJobs = [];

    function renderAutomations() {
        if (!automationList) return;
        if (!automationJobs.length) {
            const nextEl = $('stat-next');
            if (nextEl) nextEl.textContent = '—';
            automationList.innerHTML = '<div class="no-data">none · try: every day at 9am …</div>';
            return;
        }
        // Soonest next-fire first (the server orders by id, not by time —
        // sorting here keeps the NEXT stat honest about what fires next).
        const sorted = automationJobs.slice().sort((a, b) => {
            const ta = a.next_ts == null ? Infinity : a.next_ts;
            const tb = b.next_ts == null ? Infinity : b.next_ts;
            return ta - tb;
        });
        const nextEl = $('stat-next');
        if (nextEl) {
            const soon = sorted.find(j => j.next != null);
            nextEl.textContent = soon ? soon.next.split('(')[0].trim() : '—';
        }
        automationList.innerHTML = sorted.map(j => {
            const hist = (j.history || []).slice(0, 3).map(h =>
                `<i class="${h.ok ? 'ok' : 'fail'}"></i>`).join('');
            return `<div class="row">
                <div class="r1">
                    <span class="t" title="${escapeHtml(j.action)}">#${j.id} ${escapeHtml(j.describe || '')}</span>
                    <span class="m">${hist ? `<span class="hd">${hist}</span>` : ''}</span>
                </div>
                <div class="r2"><span class="a">${escapeHtml(j.action)}</span></div>
                <div class="r2">
                    ${j.next ? `<span>NEXT ${escapeHtml(j.next)}</span>` : ''}
                    <span class="btns" style="margin-left:auto;">
                        <button class="tbtn" data-run="${j.id}">RUN</button>
                        <button class="tbtn danger" data-cancel="${j.id}">DEL</button>
                    </span>
                </div>
            </div>`;
        }).join('');
    }

    function refreshAutomations() {
        socket.emit('automation_action', { action: 'list' });
    }
    socket.on('automations_update', (data) => {
        automationJobs = data.jobs || [];
        renderAutomations();
    });

    const automationInput = $('automation-input');
    const automationAddBtn = $('automation-add-btn');
    function submitAutomation() {
        const text = (automationInput.value || '').trim();
        if (!text) return;
        socket.emit('automation_action', { action: 'add', text });
        automationInput.value = '';
    }
    if (automationAddBtn) automationAddBtn.addEventListener('click', submitAutomation);
    if (automationInput) automationInput.addEventListener('keypress', (e) => {
        if (e.key === 'Enter') submitAutomation();
    });
    const autRefresh = $('automations-refresh');
    if (autRefresh) autRefresh.addEventListener('click', refreshAutomations);

    if (automationList) {
        automationList.addEventListener('click', (e) => {
            const runBtn = e.target.closest('[data-run]');
            if (runBtn) {
                socket.emit('automation_action', { action: 'run_now', keyword: runBtn.dataset.run });
                return;
            }
            const del = e.target.closest('[data-cancel]');
            if (del) socket.emit('automation_action', { action: 'cancel', keyword: del.dataset.cancel });
        });
    }
    refreshAutomations();

    /* ── 3 · APPROVALS STACK ───────────────────────────────────────── */
    const approvalsListEl = $('approvals-list');

    function renderApprovals(pending) {
        if (!approvalsListEl) return;
        if (!pending.length) {
            approvalsListEl.innerHTML = '<div class="no-data">no pending approvals</div>';
            return;
        }
        approvalsListEl.innerHTML = pending.map(p =>
            `<div class="row">
                <div class="r1"><span class="t">${escapeHtml(p.action)}</span></div>
                <div class="r2"><span class="a">${escapeHtml(p.summary)}</span></div>
                <div class="r2">
                    <button class="tbtn ok" data-approve="${p.id}">[OK]</button>
                    <button class="tbtn danger" data-deny="${p.id}">[NO]</button>
                </div>
            </div>`).join('');
    }

    function refreshApprovals() { socket.emit('approvals_list'); }
    socket.on('approvals_update', (data) => renderApprovals(data.pending || []));
    const apprRefresh = $('approvals-refresh');
    if (apprRefresh) apprRefresh.addEventListener('click', refreshApprovals);
    if (approvalsListEl) {
        approvalsListEl.addEventListener('click', (e) => {
            const a = e.target.closest('[data-approve]');
            const d = e.target.closest('[data-deny]');
            if (a) socket.emit('approval_response', { id: a.dataset.approve, approved: true });
            else if (d) socket.emit('approval_response', { id: d.dataset.deny, approved: false });
        });
    }
    refreshApprovals();
    setInterval(refreshApprovals, 15000);

    /* ── 4 · APPROVAL HOLD TOASTS ──────────────────────────────────── */
    const activeHolds = {};
    socket.on('approval_request', (data) => {
        if (!toastStack || activeHolds[data.id]) return;
        const el = document.createElement('div');
        el.className = 'toast approval';
        el.innerHTML =
            `<div class="tt">[HOLD] ${escapeHtml(data.action)}</div>` +
            `<div class="tb">${escapeHtml(data.summary || '')}</div>` +
            `<div class="acts">` +
            `<button class="abtn approve" data-id="${data.id}">[ APPROVE ]</button>` +
            `<button class="abtn deny" data-id="${data.id}">[ DENY ]</button></div>`;
        toastStack.appendChild(el);
        requestAnimationFrame(() => el.classList.add('show'));
        activeHolds[data.id] = el;
        const resolve = (approved) => {
            socket.emit('approval_response', { id: data.id, approved });
            el.classList.remove('show');
            setTimeout(() => el.remove(), 120);
            delete activeHolds[data.id];
            refreshApprovals();
        };
        el.querySelector('.approve').addEventListener('click', () => resolve(true));
        el.querySelector('.deny').addEventListener('click', () => resolve(false));
        setTimeout(() => {
            if (activeHolds[data.id]) { el.remove(); delete activeHolds[data.id]; refreshApprovals(); }
        }, (data.timeout || 120) * 1000);
    });

    /* ── 5 · CAPABILITIES DRAWER (skills/tools) ────────────────────── */
    document.querySelectorAll('[data-cab-tab]').forEach(tab => {
        tab.addEventListener('click', () => {
            document.querySelectorAll('[data-cab-tab]').forEach(t => t.classList.remove('active'));
            tab.classList.add('active');
            const which = tab.dataset.cabTab;
            const sk = $('cab-skills-tab'), tl = $('cab-tools-tab');
            if (sk) sk.classList.toggle('hidden', which !== 'skills');
            if (tl) tl.classList.toggle('hidden', which !== 'tools');
        });
    });

    function openArgsDialog(title, fields, onSubmit) {
        const backdrop = document.createElement('div');
        backdrop.className = 'arg-dialog-backdrop';
        backdrop.innerHTML =
            `<div class="arg-dialog">
                <div class="panel-header"><span>${escapeHtml(title)}</span>
                    <button class="d-close arg-cancel">[X]</button></div>
                <div class="arg-fields">${fields.map(f =>
                    `<label class="arg-field">
                        <span>${escapeHtml(f.name)}${f.required ? ' *' : ''}
                            <em>${escapeHtml(f.description || f.type || '')}</em></span>
                        <input type="text" data-arg="${escapeHtml(f.name)}">
                    </label>`).join('')}</div>
                <button class="arg-submit">RUN</button>
            </div>`;
        document.body.appendChild(backdrop);
        const submitBtn = backdrop.querySelector('.arg-submit');
        const close = () => backdrop.remove();
        const first = backdrop.querySelector('[data-arg]');
        if (first) first.focus();
        // Full keyboard flow, matching the palette: Enter runs, Escape cancels.
        backdrop.querySelectorAll('[data-arg]').forEach(inp => {
            inp.addEventListener('keydown', (ev) => {
                if (ev.key === 'Enter') { ev.preventDefault(); submitBtn.click(); }
            });
        });
        backdrop.addEventListener('keydown', (ev) => {
            if (ev.key === 'Escape') { ev.stopPropagation(); close(); }
        });
        backdrop.querySelector('.arg-cancel').addEventListener('click', close);
        backdrop.addEventListener('click', (e) => { if (e.target === backdrop) close(); });
        submitBtn.addEventListener('click', () => {
            const values = {};
            let missing = null;
            backdrop.querySelectorAll('[data-arg]').forEach(inp => {
                const v = inp.value.trim();
                if (v) values[inp.dataset.arg] = v;
                else if (!missing) missing = inp.dataset.arg;
            });
            if (missing) { showToast('MISSING', `"${missing}" is required`, 'error'); return; }
            close();
            onSubmit(values);
        });
    }

    const skillsListEl = $('skills-list');
    let skillItems = [];
    socket.on('skills_list', (data) => {
        skillItems = data.skills || [];
        const st = $('stat-skills');
        if (st) st.textContent = String(skillItems.length);
        if (!skillsListEl) return;
        if (!skillItems.length) {
            skillsListEl.innerHTML = '<div class="no-data">no skills · say "save this as a skill"</div>';
            return;
        }
        skillsListEl.innerHTML = skillItems.map(s =>
            `<div class="drow" data-name="${escapeHtml(s.name)}">
                <div class="grow">
                    <span style="color:var(--text-1)">${escapeHtml(s.display_name || s.name)}</span>
                    <small style="color:var(--text-3)"> · ${s.runs || 0} runs</small>
                    <span class="sub">${escapeHtml(s.description || '')}</span>
                    ${s.params && s.params.length ? `<span class="sub">${s.params.map(p => `<code>{${escapeHtml(p)}}</code>`).join(' ')}</span>` : ''}
                </div>
                <div style="display:flex;gap:4px;">
                    <button class="tbtn" data-run="${escapeHtml(s.name)}">RUN</button>
                    <button class="tbtn danger" data-del="${escapeHtml(s.name)}">DEL</button>
                </div>
            </div>`).join('');
    });
    if (skillsListEl) {
        skillsListEl.addEventListener('click', (e) => {
            const runBtn = e.target.closest('[data-run]');
            const delBtn = e.target.closest('[data-del]');
            if (runBtn) {
                const name = runBtn.dataset.run;
                const skill = skillItems.find(s => s.name === name) || {};
                const params = skill.params || [];
                if (!params.length) {
                    socket.emit('skills_action', { action: 'run', name, params: {} });
                    return;
                }
                openArgsDialog(`RUN ${name}`,
                    params.map(p => ({ name: p, type: 'value', required: true })),
                    (values) => socket.emit('skills_action', { action: 'run', name, params: values }));
            } else if (delBtn) {
                socket.emit('skills_action', { action: 'delete', name: delBtn.dataset.del });
            }
        });
    }

    const toolsListEl = $('tools-list');
    socket.on('tools_list', (data) => {
        const tools = data.tools || [];
        if (!toolsListEl) return;
        if (!tools.length) {
            toolsListEl.innerHTML = '<div class="no-data">no tools · drop a manifest in tools_registry/</div>';
            return;
        }
        toolsListEl.innerHTML = tools.map(t =>
            `<div class="drow" data-name="${escapeHtml(t.name)}">
                <div class="grow">
                    <span style="color:var(--accent)">[${t.method}]</span>
                    <span style="color:var(--text-1)">${escapeHtml(t.name)}</span>
                    <span class="sub">${escapeHtml(t.description)}</span>
                    ${Object.keys(t.params || {}).length
                        ? `<span class="sub">${Object.entries(t.params).map(([p, spec]) =>
                            `<code>${escapeHtml(p)}${spec.required ? '*' : ''}</code>`).join(' ')}</span>` : ''}
                </div>
                <div style="display:flex;gap:4px;"><button class="tbtn" data-call="${escapeHtml(t.name)}">CALL</button></div>
            </div>`).join('');

        toolsListEl.querySelectorAll('[data-call]').forEach(btn => {
            btn.addEventListener('click', () => {
                const name = btn.dataset.call;
                const tool = tools.find(t => t.name === name) || {};
                const params = Object.entries(tool.params || {}).map(([p, spec]) =>
                    ({ name: p, type: spec.type || 'value', description: spec.description || '', required: !!spec.required }));
                if (!params.length) {
                    socket.emit('tools_action', { action: 'call', name, args: {} });
                    return;
                }
                openArgsDialog(`CALL ${name}`, params,
                    (values) => socket.emit('tools_action', { action: 'call', name, args: values }));
            });
        });
    });

    /* ── 6 · TRANSCRIPT SEARCH (memory drawer) ─────────────────────── */
    const transcriptInput = $('transcript-search-input');
    const transcriptBtn = $('transcript-search-btn');
    const transcriptResults = $('transcript-results');
    function doTranscriptSearch() {
        const q = (transcriptInput.value || '').trim();
        if (!q) return;
        socket.emit('transcript_search', { query: q });
    }
    if (transcriptBtn) transcriptBtn.addEventListener('click', doTranscriptSearch);
    if (transcriptInput) transcriptInput.addEventListener('keypress', (e) => {
        if (e.key === 'Enter') doTranscriptSearch();
    });
    socket.on('transcript_results', (data) => {
        if (!transcriptResults) return;
        const hits = data.results || [];
        transcriptResults.innerHTML = hits.length
            ? hits.map(h => `<div class="drow"><div class="grow"><span class="sub">${escapeHtml(h.snippet)}</span><span class="sub" style="color:var(--text-3)">${escapeHtml(h.created.slice(0, 16))}</span></div></div>`).join('')
            : '<div class="no-data">no matches</div>';
    });

    /* ── 7 · REFLECTION (handled in missions block) ────────────────── */
    socket.on('task_reflection', () => {});

    /* ── 8 · GUARD/BUDGET + UPTIME (strip widgets) ─────────────────── */
    function fmtUsd(v) { return '$' + Number(v).toFixed(2); }
    function fmtUp(s) {
        s = Math.floor(s || 0);
        const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
        return h > 0 ? `${h}:${String(m).padStart(2, '0')}h` : `${m}m`;
    }
    async function pollHealth() {
        try {
            const res = await fetch('/health');
            if (!res.ok) return;
            const d = await res.json();
            const guard = $('w-guard');
            if (guard && d.circuit_breaker) {
                const s = d.circuit_breaker;
                guard.textContent = s.toUpperCase();
                guard.style.color = s === 'ok' ? 'var(--ok)' : s === 'degraded' ? 'var(--warn)' : 'var(--fail)';
            }
            const b = d.budget || {};
            const txt = $('w-budget-text');
            if (txt) txt.textContent = `${fmtUsd(b.spent_usd)}/${fmtUsd(b.limit_usd)}`;
            const tok = $('w-budget-tok');
            if (tok) tok.textContent = String(b.spent_tokens || 0);
            const up = $('strip-up');
            if (up && d.uptime_s != null) up.textContent = fmtUp(d.uptime_s);
        } catch (_) {}
    }
    pollHealth();
    setInterval(pollHealth, 5000);

    /* ── 9 · LOG TICKER ────────────────────────────────────────────── */
    const tickerLine = $('ticker-line');
    let tickerLines = [], tickerIdx = 0;
    function _sanitizeLog(s) {
        return String(s)
            .replace(/(sk-[A-Za-z0-9_-]{6})[A-Za-z0-9_-]+/g, '$1***')
            .replace(/((?:api[_-]?key|token|secret)[=: ]+)\S+/gi, '$1***')
            .slice(0, 200);
    }
    async function pollLogs() {
        try {
            const res = await fetch('/api/logs?n=6');
            if (!res.ok) return;
            const d = await res.json();
            if (Array.isArray(d.lines) && d.lines.length) {
                tickerLines = d.lines.map(_sanitizeLog);
                tickerIdx = tickerLines.length - 1;
                if (tickerLine) tickerLine.textContent = tickerLines[tickerIdx];
            }
        } catch (_) {}
    }
    pollLogs();
    setInterval(pollLogs, 6000);
    setInterval(() => {
        if (!tickerLines.length || !tickerLine) return;
        tickerIdx = (tickerIdx + 1) % tickerLines.length;
        tickerLine.textContent = tickerLines[tickerIdx];
    }, 4000);

    /* ── 10 · COMMAND PALETTE (⌘K / PAL) ───────────────────────────── */
    const COMMANDS = [
        { code: 'TSK', label: 'to-do list', act: () => openPanel('tasks-panel', 'tasks-btn') },
        { code: 'NTS', label: 'notes', act: () => openPanel('notes-panel', 'notes-btn') },
        { code: 'MEM', label: 'memory + transcript search', act: () => openPanel('memory-panel', 'memory-btn') },
        { code: 'PPL', label: 'relationships', act: () => openPanel('people-panel', 'people-btn') },
        { code: 'MTG', label: 'meeting mode', act: () => openPanel('meeting-panel', 'meeting-btn') },
        { code: 'SEC', label: 'privacy', act: () => openPanel('privacy-panel', 'privacy-btn') },
        { code: 'LIB', label: 'knowledge upload', act: () => openPanel('librarian-panel', 'librarian-btn') },
        { code: 'SKL', label: 'skills & tools', act: () => { openPanel('capabilities-panel', 'skills-btn'); socket.emit('skills_action', { action: 'list' }); socket.emit('tools_action', { action: 'list' }); } },
        { code: 'PAR', label: 'jarvis vs hermes parity', act: () => openPanel('parity-panel', 'parity-btn') },
        { code: 'LLM', label: 'ai providers · byok', act: () => openPanel('llm-panel', 'llm-btn') },
        { code: 'VIS', label: 'vision scan', act: () => $('vision-btn') && $('vision-btn').click() },
        { code: 'DEV', label: 'dev focus', act: () => $('dev-btn') && $('dev-btn').click() },
        { code: 'MIS', label: 'missions stack', act: () => flashStack('stack-missions') },
        { code: 'APR', label: 'approvals stack', act: () => flashStack('stack-approvals') },
        { code: 'AUT', label: 'automations stack', act: () => flashStack('stack-automations') },
        { code: 'HIST', label: 'mission history', act: () => { if (window.setMisView) setMisView('history'); flashStack('stack-missions'); } },
    ];
    const palBackdrop = $('palette-backdrop');
    const palInput = $('palette-input');
    const palList = $('palette-list');
    let palSel = 0, palFiltered = COMMANDS;

    function openPalette() {
        if (!palBackdrop) return;
        palBackdrop.classList.add('open');
        if (palInput) palInput.value = '';
        filterPalette('');
        setTimeout(() => palInput && palInput.focus(), 0);
    }
    function closePalette() {
        if (palBackdrop) palBackdrop.classList.remove('open');
    }
    function filterPalette(q) {
        q = q.trim().toLowerCase();
        palFiltered = COMMANDS.filter(c =>
            !q || c.code.toLowerCase().includes(q) || c.label.toLowerCase().includes(q));
        palSel = 0;
        renderPalette();
    }
    function renderPalette() {
        if (!palList) return;
        palList.innerHTML = palFiltered.map((c, i) =>
            `<li class="${i === palSel ? 'sel' : ''}" data-i="${i}">
                <span>${escapeHtml(c.label)}</span><span class="code">${c.code}</span>
            </li>`).join('') || '<li><span>no match</span></li>';
    }
    function execPalette(i) {
        const c = palFiltered[i];
        if (!c) return;
        closePalette();
        c.act();
    }
    if (palInput) palInput.addEventListener('input', () => filterPalette(palInput.value));
    if (palInput) palInput.addEventListener('keydown', (e) => {
        if (e.key === 'ArrowDown') { palSel = Math.min(palSel + 1, palFiltered.length - 1); renderPalette(); e.preventDefault(); }
        else if (e.key === 'ArrowUp') { palSel = Math.max(palSel - 1, 0); renderPalette(); e.preventDefault(); }
        else if (e.key === 'Enter') { execPalette(palSel); e.preventDefault(); }
        else if (e.key === 'Escape') closePalette();
    });
    if (palList) palList.addEventListener('click', (e) => {
        const li = e.target.closest('li[data-i]');
        if (li) execPalette(Number(li.dataset.i));
    });
    if (palBackdrop) palBackdrop.addEventListener('click', (e) => {
        if (e.target === palBackdrop) closePalette();
    });
    const palBtn = $('palette-btn');
    if (palBtn) palBtn.addEventListener('click', openPalette);
    document.addEventListener('keydown', (e) => {
        if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
            e.preventDefault();
            if (palBackdrop && palBackdrop.classList.contains('open')) closePalette();
            else openPalette();
        }
    });
    // Escape walks the overlay stack: arg dialog → palette → open drawer.
    document.addEventListener('keydown', (e) => {
        if (e.key !== 'Escape') return;
        const arg = document.querySelector('.arg-dialog-backdrop');
        if (arg) { arg.remove(); return; }
        if (palBackdrop && palBackdrop.classList.contains('open')) { closePalette(); return; }
        if (typeof closeAllPanels === 'function' &&
            document.querySelector('.drawer.active')) {
            closeAllPanels();
        }
    });
})();

/* ====================================================================== *
 *  LLM PROVIDERS + MCP — drawer data module.
 *  Fleet status, BYOK key management, MCP server states + reload.
 * ====================================================================== */
(function () {
    const panel = document.getElementById('llm-panel');
    if (!panel) return;

    const fleetEl = document.getElementById('llm-fleet');
    const listEl = document.getElementById('llm-provider-list');

    // MCP section lives right above the footer note.
    const noteEl = panel.querySelector('.llm-note');
    let mcpEl = document.getElementById('llm-mcp');
    if (!mcpEl && noteEl) {
        mcpEl = document.createElement('div');
        mcpEl.id = 'llm-mcp';
        noteEl.parentNode.insertBefore(mcpEl, noteEl);
    }

    const PROV_SRC = {
        groq: 'console.groq.com', google: 'aistudio.google.com',
        anthropic: 'console.anthropic.com', openai: 'platform.openai.com',
        deepseek: 'platform.deepseek.com', openrouter: 'openrouter.ai',
        custom: 'custom base_url',
        ollama: 'local · no key', lmstudio: 'local · no key',
    };

    function chip(state) {
        if (state === 'ok') return '<span class="st-ok">[ OK ]</span>';
        if (state === 'cool') return '<span class="st-cool">[COOL]</span>';
        return '<span class="st-off">[ -- ]</span>';
    }

    async function refreshLlm() {
        try {
            const res = await fetch('/api/providers');
            if (!res.ok) return;
            const d = await res.json();
            renderFleet(d);
            renderProviders(d);
            renderMcp(d);
        } catch (_) { /* offline */ }
    }
    window.refreshLlmFleet = refreshLlm;   // hook point for openPanel

    // Publish live fleet size for the hero gauge status line.
    async function publishFleetCount() {
        try {
            const res = await fetch('/api/providers');
            if (!res.ok) return;
            const d = await res.json();
            window._fleetOnline = (d.providers || []).length;
        } catch (_) { /* offline */ }
    }
    publishFleetCount();
    setInterval(publishFleetCount, 60000);

    function renderFleet(d) {
        if (!fleetEl) return;
        const names = (d.providers || []).map(p => p.name);
        if (!names.length) {
            fleetEl.innerHTML =
                'FLEET OFFLINE · <a href="/onboarding" style="color:var(--accent)">run setup →</a>';
            return;
        }
        fleetEl.innerHTML =
            `FLEET <b>${names.length} ONLINE</b> · CHAIN ` +
            `<b>${(d.order || []).join(' → ')}</b>`;
    }

    function renderProviders(d) {
        if (!listEl) return;
        const online = {};
        (d.providers || []).forEach(p => { online[p.name] = p; });
        const cool = new Set(Object.keys(d.cooldowns || {})
            .map(k => k.split('/')[0]));
        listEl.innerHTML = Object.keys(PROV_SRC).map(name => {
            const prov = online[name];
            const keyInfo = (d.keys || {})[name] || {};
            const state = prov ? (cool.has(name) ? 'cool' : 'ok') : 'off';
            const models = prov
                ? `${(prov.models || []).length} model(s)` +
                  (prov.dynamic ? ' · auto-detected' : '')
                : `— ${PROV_SRC[name]}`;
            const preview = keyInfo.preview
                ? `<span class="prov-key">${keyInfo.preview}</span>` : '';
            const del = keyInfo.configured
                ? `<button class="prov-del" data-del="${name}">DEL</button>`
                : '';
            return `<div class="prov-row">
                <span class="prov-name">${name}</span>
                <span class="prov-status">${chip(state)}</span>
                <span class="prov-models">${models}</span>
                ${preview}${del}
            </div>`;
        }).join('');
        // select options mirror the canonical set
        const sel = document.getElementById('llm-provider-select');
        if (sel && !sel.options.length) {
            sel.innerHTML = ['groq', 'google', 'anthropic', 'openai',
                'deepseek', 'openrouter', 'custom']
                .map(p => `<option value="${p}">${p}</option>`).join('');
        }
    }

    function renderMcp(d) {
        if (!mcpEl) return;
        const servers = d.mcp || [];
        if (!servers.length) return;      // keep the drawer quiet
        const rows = servers.map(s =>
            `<div class="prov-row">
                <span class="prov-name">mcp·${s.name}</span>
                <span class="prov-status">${
                    s.state === 'online'
                        ? '<span class="st-ok">[ OK ]</span>'
                        : s.state === 'offline'
                        ? '<span class="prov-key" title="' +
                          (s.error || '') + '">[FAIL]</span>'
                        : '<span class="st-off">[PEND]</span>'
                }</span>
                <span class="prov-models">${s.tools != null
                    ? s.tools + ' tool(s)' : (s.command || '')}</span>
            </div>`).join('');
        mcpEl.innerHTML =
            `<div class="llm-fleet" style="border:none;padding:8px 0 0;">
                MCP TOOL SERVERS
                <button class="prov-del" id="mcp-reload" style="margin-left:auto;">RELOAD</button>
             </div>${rows}`;
        const btn = document.getElementById('mcp-reload');
        if (btn) btn.addEventListener('click', async () => {
            btn.disabled = true;
            try {
                await fetch('/api/mcp/reload', { method: 'POST' });
                await refreshLlm();
            } catch (_) {}
            btn.disabled = false;
        });
    }

    // ---- actions -------------------------------------------------- //
    const saveBtn = document.getElementById('llm-save-btn');
    const keyInput = document.getElementById('llm-key-input');
    const selectEl = document.getElementById('llm-provider-select');

    if (saveBtn) saveBtn.addEventListener('click', async () => {
        const provider = selectEl ? selectEl.value : '';
        const api_key = keyInput ? keyInput.value.trim() : '';
        if (!provider || !api_key) {
            showToast('Missing key', 'Pick a provider and paste a key.',
                      'error', 2600);
            return;
        }
        saveBtn.disabled = true;
        try {
            const res = await fetch('/api/providers', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({provider, api_key})
            });
            const d = await res.json();
            if (d.ok) {
                showToast('Provider saved',
                          `${provider} joined the failover fleet.`,
                          'success', 3000);
                keyInput.value = '';
                await refreshLlm();
            } else {
                showToast('Rejected', d.error || 'unknown error',
                          'error', 3200);
            }
        } catch (_) {
            showToast('Network error', 'Could not reach Jarvis.',
                      'error', 3000);
        } finally { saveBtn.disabled = false; }
    });
    if (keyInput) keyInput.addEventListener('keypress', (e) => {
        if (e.key === 'Enter' && saveBtn) saveBtn.click();
    });
    if (listEl) listEl.addEventListener('click', async (e) => {
        const btn = e.target.closest('[data-del]');
        if (!btn) return;
        btn.disabled = true;
        try {
            await fetch(`/api/providers/${btn.dataset.del}`,
                        { method: 'DELETE' });
            showToast('Key removed',
                      `${btn.dataset.del} left the fleet.`,
                      'success', 2800);
            await refreshLlm();
        } catch (_) {}
    });

    // ---- wiring --------------------------------------------------- //
    const llmBtn = document.getElementById('llm-btn');
    if (llmBtn) llmBtn.addEventListener('click', () => {
        openPanel('llm-panel', 'llm-btn');
        refreshLlm();
    });
    const closeBtn = document.getElementById('llm-close');
    if (closeBtn) closeBtn.addEventListener('click', closeAllPanels);

    refreshLlm();
})();

// Onboarding nudge: first run with zero providers configured.
(async function () {
    try {
        const res = await fetch('/health');
        if (!res.ok) return;
        const d = await res.json();
        if (d.needs_onboarding &&
            !location.pathname.startsWith('/onboarding')) {
            showToast('Setup required',
                      'Jarvis has no AI keys yet. Opening setup…',
                      'warn', 5000);
            setTimeout(() => location.href = '/onboarding', 1600);
        }
    } catch (_) {}
})();
