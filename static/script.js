const socket = io();

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

// --- THEME SWITCHER ---
const themeSwitcher = document.getElementById('theme-switcher');
const themeDots = themeSwitcher ? themeSwitcher.querySelectorAll('.theme-dot') : [];

function applyTheme(theme) {
    document.body.className = theme === 'midnight' ? '' : 'theme-' + theme;
    themeDots.forEach(d => d.classList.toggle('active', d.dataset.theme === theme));
    localStorage.setItem('jarvis-theme', theme);
}

themeDots.forEach(dot => {
    dot.addEventListener('click', () => applyTheme(dot.dataset.theme));
});

// Restore saved theme on load
applyTheme(localStorage.getItem('jarvis-theme') || 'midnight');

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
    'llm-panel'
];
const allTriggers = [
    'memory-btn', 'people-btn', 'meeting-btn', 'privacy-btn',
    'notes-btn', 'tasks-btn', 'librarian-btn', 'skills-btn', 'llm-btn'
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

// Connection status
socket.on('connect', () => {
    console.log('✅ Connected to Jarvis server');
    addMessage("SYSTEM: Connected to Jarvis");
});

socket.on('disconnect', () => {
    console.log('❌ Disconnected from Jarvis server');
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
    addMessage(`JARVIS: ${data.text}`);
    setCoreState('speaking');
});

// Live Terminal Logging
socket.on('new_log', (data) => {
    const terminal = document.getElementById('terminal-box');
    if (terminal) {
        terminal.style.display = 'block'; // Auto-show on log
        const line = document.createElement('div');
        const time = new Date().toLocaleTimeString('en-US', { hour12: false });
        line.innerHTML = `<span style="opacity:0.5">[${time}]</span> ${data.data}`;
        line.style.borderBottom = '1px dashed rgba(0, 255, 0, 0.2)';
        line.style.padding = '4px 0';
        terminal.appendChild(line);
        terminal.scrollTop = terminal.scrollHeight;
    }
});

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

socket.on('system_vitals', (vitals) => {
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

    // Timestamp
    const vitalsTime = document.getElementById('vitals-time');
    if (vitalsTime && vitals.timestamp) {
        vitalsTime.textContent = vitals.timestamp;
    }
});

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
        const project = prompt("Enter project name to focus on (leave empty to clear):");
        if (project !== null) { // User didn't cancel
            const cmd = project.trim() ? "Focus on " + project : "Exit dev mode";
            wakeUp();
            processCommand(cmd);
        }
    });
}

// --- SKILLS DRAWER DATA ---
const skillsBtnEl = document.getElementById('skills-btn');
if (skillsBtnEl) {
    skillsBtnEl.addEventListener('click', () => {
        socket.emit('skills_action', { action: 'list' });
        socket.emit('tools_action', { action: 'list' });
    });
}

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
    memoryBtn.addEventListener('click', () => openPanel('memory-panel', 'memory-btn'));
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
            <div>
                <div class="person-name">${escapeHtml(person.name)}</div>
                <div class="person-rel">${escapeHtml(person.relationship || '')}${hobbies ? ' | ' + escapeHtml(hobbies) : ''}</div>
            </div>
            <div class="person-actions">
                <button data-action="gifts" title="Gift ideas">🎁</button>
                <button data-action="remove" title="Remove">🗑️</button>
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
        meetingStatus.textContent = '🔴 Recording...';
        meetingStatus.style.color = '#ff5050';
        document.body.classList.add('meeting-active');
        // Poll transcript every 5 seconds
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
        meetingStatus.textContent = 'Processing...';
        meetingStatus.style.color = '';
        document.body.classList.remove('meeting-active');
        if (meetingInterval) { clearInterval(meetingInterval); meetingInterval = null; }
    });
}

// --- PRIVACY PANEL ---
const privacyBtn = document.getElementById('privacy-btn');
const privacyPanel = document.getElementById('privacy-panel');
const privacyClose = document.getElementById('privacy-close');
const privacySettings = document.getElementById('privacy-settings');

if (privacyBtn && privacyPanel) {
    privacyBtn.addEventListener('click', () => {
        openPanel('privacy-panel', 'privacy-btn');
        socket.emit('privacy_action', { action: 'summary' });
    });
}
if (privacyClose) privacyClose.addEventListener('click', closeAllPanels);

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

// --- MOBILE MIC LOGIC ---
const mobileMicBtn = document.getElementById('mobile-mic-btn');
if (mobileMicBtn) {
    mobileMicBtn.addEventListener('click', () => {
        if (mobileMicBtn.classList.contains('listening')) {
            // STOP functionality
            try {
                recognition.stop();
            } catch (e) { console.log(e); }
            mobileMicBtn.classList.remove('listening');
            addMessage("SYSTEM: Mic Stopped");
            return;
        }

        // START functionality
        wakeUp();
        addMessage("SYSTEM: Listening...");
        mobileMicBtn.classList.add('listening');

        try {
            recognition.start();
        } catch (e) {
            console.log("Mic error:", e);
            if (e.message.includes('already started')) {
                // Ignore this specific error, it means we are good
                console.log("Mic already running, ignoring.");
            } else {
                alert("Mic Start Error: " + e.message);
                mobileMicBtn.classList.remove('listening');
            }
        }
    });

    // Update mic button state based on recognition events
    if (recognition) {
        const originalOnEnd = recognition.onend;
        recognition.onend = () => {
            if (originalOnEnd) originalOnEnd();
            mobileMicBtn.classList.remove('listening');
        };

        const originalOnError = recognition.onerror;
        recognition.onerror = (e) => {
            if (originalOnError) originalOnError(e);
            mobileMicBtn.classList.remove('listening');
            alert("Mic Error: " + e.error + "\n(Note: Mobile browsers often block mic on HTTP. You may need to use Chrome and enable 'Insecure origins treated as secure' flag for this IP)");
        };
    }
}


// --- UI HELPERS ---

function setCoreState(state) {
    // States: idle, active (listening), speaking, processing
    const dial = core ? core.closest('.reactor') : null;
    if (!dial) return;
    dial.classList.remove('listening', 'speaking');
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

    /* ── 2 · AUTOMATIONS STACK ─────────────────────────────────────── */
    const automationList = $('automation-list');
    let automationJobs = [];

    function renderAutomations() {
        if (!automationList) return;
        const nextEl = $('stat-next');
        if (nextEl) nextEl.textContent =
            automationJobs[0] && automationJobs[0].next
                ? automationJobs[0].next.split('(')[0].trim() : '—';

        if (!automationJobs.length) {
            automationList.innerHTML = '<div class="no-data">none · try: every day at 9am …</div>';
            return;
        }
        automationList.innerHTML = automationJobs.map(j => {
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
        const close = () => backdrop.remove();
        backdrop.querySelector('.arg-cancel').addEventListener('click', close);
        backdrop.addEventListener('click', (e) => { if (e.target === backdrop) close(); });
        backdrop.querySelector('.arg-submit').addEventListener('click', () => {
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
        { code: 'SKL', label: 'skills & tools', act: () => openPanel('capabilities-panel', 'skills-btn') },
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
})();
