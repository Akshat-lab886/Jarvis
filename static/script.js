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

// --- PANEL MANAGEMENT ---
const allPanels = [
    'memory-panel', 'people-panel', 'meeting-panel', 'privacy-panel',
    'notes-panel', 'tasks-panel', 'librarian-panel', 'missions-panel'
];
const allTriggers = [
    'memory-btn', 'people-btn', 'meeting-btn', 'privacy-btn',
    'notes-btn', 'tasks-btn', 'librarian-btn', 'missions-btn'
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

// System Vitals real-time updates
socket.on('system_vitals', (vitals) => {
    // CPU
    const cpuBar = document.getElementById('cpu-bar');
    const cpuValue = document.getElementById('cpu-value');
    if (cpuBar && cpuValue) {
        cpuBar.style.width = vitals.cpu + '%';
        cpuValue.textContent = vitals.cpu + '%';
        cpuBar.className = 'vital-bar';
        if (vitals.cpu > 90) cpuBar.classList.add('critical');
        else if (vitals.cpu > 70) cpuBar.classList.add('warning');
    }

    // RAM
    const ramBar = document.getElementById('ram-bar');
    const ramValue = document.getElementById('ram-value');
    if (ramBar && ramValue) {
        ramBar.style.width = vitals.ram + '%';
        ramValue.textContent = vitals.ram + '%';
        ramBar.className = 'vital-bar';
        if (vitals.ram > 90) ramBar.classList.add('critical');
        else if (vitals.ram > 80) ramBar.classList.add('warning');
    }

    // Battery
    const batteryBar = document.getElementById('battery-bar');
    const batteryValue = document.getElementById('battery-value');
    const batteryIcon = document.getElementById('battery-icon');
    if (batteryBar && batteryValue) {
        batteryBar.style.width = vitals.battery + '%';
        batteryValue.textContent = vitals.battery + '%';
        batteryBar.className = 'vital-bar battery';
        if (vitals.battery < 20) batteryBar.classList.add('low');

        // Charging indicator
        if (batteryIcon) {
            batteryIcon.className = 'fa-solid';
            if (vitals.charging) {
                batteryIcon.classList.add('fa-battery-bolt', 'charging');
            } else if (vitals.battery > 75) {
                batteryIcon.classList.add('fa-battery-full');
            } else if (vitals.battery > 50) {
                batteryIcon.classList.add('fa-battery-three-quarters');
            } else if (vitals.battery > 25) {
                batteryIcon.classList.add('fa-battery-half');
            } else {
                batteryIcon.classList.add('fa-battery-quarter');
            }
        }
    }

    // Disk
    const diskBar = document.getElementById('disk-bar');
    const diskValue = document.getElementById('disk-value');
    if (diskBar && diskValue) {
        diskBar.style.width = vitals.disk + '%';
        diskValue.textContent = vitals.disk + '%';
        diskBar.className = 'vital-bar';
        if (vitals.disk > 90) diskBar.classList.add('critical');
        else if (vitals.disk > 80) diskBar.classList.add('warning');
    }

    // Timestamp
    const vitalsTime = document.getElementById('vitals-time');
    if (vitalsTime && vitals.timestamp) {
        vitalsTime.textContent = vitals.timestamp;
    }
});

// Smart Home device status updates
socket.on('home_update', (devices) => {
    console.log('🏠 Home update:', devices);
    for (const [deviceId, device] of Object.entries(devices)) {
        const card = document.querySelector(`[data-device="${deviceId}"]`);
        const statusEl = document.getElementById(`status-${deviceId}`);

        if (card && statusEl) {
            const isOn = device.status === 'on';
            card.classList.toggle('on', isOn);
            statusEl.textContent = isOn ? 'ON' : 'OFF';
            statusEl.className = `device-status ${isOn ? 'on' : 'off'}`;
        }
    }
});

// Device card click handlers
document.querySelectorAll('.device-card').forEach(card => {
    card.addEventListener('click', () => {
        const deviceId = card.dataset.device;
        const isOn = card.classList.contains('on');
        const command = isOn ? 'turn_off' : 'turn_on';

        // Send toggle command to server
        socket.emit('user_input', {
            text: `${command.replace('_', ' ')} the ${deviceId.replace(/_/g, ' ')}`
        });
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
            <span class="task-check" data-action="done" title="Mark done">
                <i class="fa-solid ${item.done ? 'fa-square-check' : 'fa-square'}"></i>
            </span>
            <span class="task-text">${escapeHtml(item.text)}</span>
            <span class="task-del" data-action="remove" title="Remove">
                <i class="fa-solid fa-trash"></i>
            </span>
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
            <span class="task-del" data-action="remove" title="Delete note">
                <i class="fa-solid fa-trash"></i>
            </span>
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
            <span class="mem-del" title="Forget"><i class="fa-solid fa-xmark"></i></span>
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
    console.log('🧠 Memory update:', data);
    renderMemories(data.items, data.stats);
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

// --- MISSIONS / COMPLEX TASKS PANEL ---
const missionsBtn = document.getElementById('missions-btn');
const missionsPanel = document.getElementById('missions-panel');
const missionsClose = document.getElementById('missions-close');
const missionsList = document.getElementById('missions-list');
const missionsHistoryList = document.getElementById('missions-history-list');
const missionsHistoryStats = document.getElementById('missions-history-stats');
const missionsActiveTab = document.getElementById('missions-active-tab');
const missionsHistoryTab = document.getElementById('missions-history-tab');
const missionsTabBtns = document.querySelectorAll('.missions-tab');
let currentTasks = [];
let currentHistory = [];
let currentHistoryStats = null;
let activeMissionsTab = 'active';

function switchMissionsTab(tab) {
    activeMissionsTab = tab;
    missionsTabBtns.forEach(btn => btn.classList.toggle('active', btn.dataset.tab === tab));
    if (missionsActiveTab) missionsActiveTab.style.display = tab === 'active' ? 'block' : 'none';
    if (missionsHistoryTab) missionsHistoryTab.style.display = tab === 'history' ? 'block' : 'none';
    if (tab === 'history') {
        socket.emit('complex_task_action', { action: 'history' });
    }
}
missionsTabBtns.forEach(btn => {
    btn.addEventListener('click', () => switchMissionsTab(btn.dataset.tab));
});

function renderMissions(tasks) {
    if (!missionsList) return;
    missionsList.innerHTML = '';
    currentTasks = tasks || [];
    if (!tasks || tasks.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'task-empty';
        empty.textContent = 'No active missions, Sir.';
        missionsList.appendChild(empty);
        return;
    }
    tasks.forEach(task => {
        const div = document.createElement('div');
        div.className = 'mission-card glass';
        div.dataset.taskId = task.id;
        const statusIcon = {
            'pending': '⏳', 'running': '⚡', 'paused': '⏸️',
            'completed': '✅', 'failed': '❌', 'cancelled': '🚫'
        }[task.status] || '⏳';
        const isActive = ['pending', 'running', 'paused'].includes(task.status);
        const pct = task.progress || 0;
        const hasDeps = task.has_dependencies || false;
        const modeBadge = hasDeps
            ? '<span class="mission-mode-badge parallel">⚡ PARALLEL</span>'
            : '<span class="mission-mode-badge sequential">🔗 SEQUENTIAL</span>';

        // Build steps HTML with dependency info
        let stepsHtml = '';
        if (task.steps && task.steps.length > 0) {
            stepsHtml = '<div class="mission-steps">' + task.steps.map(s => {
                const sIcon = {
                    'pending': '⬜', 'running': '🔄', 'completed': '✅',
                    'failed': '❌', 'skipped': '⏭️'
                }[s.status] || '⬜';
                const deps = s.depends_on && s.depends_on.length > 0
                    ? `<span class="step-deps">← from ${s.depends_on.join(', ')}</span>`
                    : '';
                const thread = s.thread_id && hasDeps
                    ? `<span class="step-thread" title="Thread: ${escapeHtml(s.thread_id)}">🔩</span>`
                    : '';
                return `<div class="mission-step ${s.status}">` +
                    `<span class="step-icon">${sIcon}</span> ` +
                    `<span class="step-id">${s.id}.</span> ` +
                    `<span class="step-text">${escapeHtml(s.text.slice(0, 70))}</span> ` +
                    `${deps}${thread}` +
                    (s.error ? `<span class="step-error" title="${escapeHtml(s.error)}">⚠️</span>` : '') +
                    `</div>`;
            }).join('') + '</div>';
        }

        // Layer info for parallel tasks
        let layerHtml = '';
        if (hasDeps && task.dependency_layers && task.dependency_layers.length > 1) {
            const layerLabels = task.dependency_layers.map((layer, i) => {
                const count = layer.length;
                const parallel = count > 1 ? ' ⚡' : '';
                return `L${i + 1}(${count}step${count > 1 ? 's' : ''}${parallel})`;
            }).join(' → ');
            layerHtml = `<div class="mission-layers">${layerLabels}</div>`;
        }

        let actionsHtml = '';
        if (isActive) {
            const pauseResume = task.status === 'paused'
                ? `<button class="mission-action" data-action="resume" title="Resume">▶️ Resume</button>`
                : `<button class="mission-action" data-action="pause" title="Pause">⏸️ Pause</button>`;
            actionsHtml = `<div class="mission-actions">${pauseResume}<button class="mission-action" data-action="cancel" title="Cancel">🚫 Cancel</button></div>`;
        }
        const costHtml = (task.cost_usd && task.cost_usd > 0.0001)
            ? `<span class="mission-cost" title="${task.cost_tokens || 0} tokens">· $${task.cost_usd.toFixed(4)}</span>` : '';
        div.innerHTML = `
            <div class="mission-header">
                <span class="mission-status-icon">${statusIcon}</span>
                <span class="mission-desc">${escapeHtml(task.description.slice(0, 80))}</span>
                ${modeBadge}
                <span class="mission-id">#${task.id}</span>
            </div>
            <div class="mission-progress-bar"><div class="mission-progress-fill" style="width: ${pct}%"></div></div>
            <div class="mission-meta">${task.steps ? task.steps.length : 0} steps · ${pct}% · ${task.status} ${costHtml}</div>
            ${layerHtml}
            ${stepsHtml}
            ${task.final_summary ? '<div class="mission-summary">' + escapeHtml(task.final_summary.slice(0, 200)) + '</div>' : ''}
            ${actionsHtml}
        `;
        missionsList.appendChild(div);
    });
}

function renderHistory(tasks, stats) {
    if (!missionsHistoryList) return;
    missionsHistoryList.innerHTML = '';
    currentHistory = tasks || [];
    currentHistoryStats = stats || null;

    // Render stats header
    if (missionsHistoryStats && stats) {
        const s = stats;
        const rate = s.total > 0 ? Math.round(s.completed / s.total * 100) : 0;
        missionsHistoryStats.innerHTML = `
            <div class="history-stats">
                <span class="stat-item"><span class="stat-num">${s.total}</span> total</span>
                <span class="stat-item completed"><span class="stat-num">${s.completed}</span> done</span>
                <span class="stat-item failed"><span class="stat-num">${s.failed}</span> failed</span>
                <span class="stat-item"><span class="stat-num">${rate}%</span> success</span>
            </div>
        `;
    }

    if (!tasks || tasks.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'task-empty';
        empty.textContent = 'No mission history yet, Sir.';
        missionsHistoryList.appendChild(empty);
        return;
    }

    tasks.forEach(task => {
        const div = document.createElement('div');
        div.className = 'mission-card glass history-card';
        div.dataset.taskId = task.id;

        const statusIcon = {
            'completed': '✅', 'failed': '❌', 'cancelled': '🚫'
        }[task.status] || '⏳';

        const pct = task.progress || 0;
        const completedSteps = task.steps ? task.steps.filter(s => s.status === 'completed').length : 0;
        const totalSteps = task.steps ? task.steps.length : 0;
        const failedSteps = task.steps ? task.steps.filter(s => s.status === 'failed').length : 0;

        // Format timestamps
        const created = task.created_at ? formatTaskTime(task.created_at) : '';
        const completed = task.completed_at ? formatTaskTime(task.completed_at) : '';
        const duration = task.created_at && task.completed_at
            ? formatDuration(task.created_at, task.completed_at) : '';

        // Build steps HTML (collapsed by default)
        let stepsHtml = '';
        if (task.steps && task.steps.length > 0) {
            stepsHtml = '<div class="mission-steps history-steps collapsed">' + task.steps.map(s => {
                const sIcon = {
                    'completed': '✅', 'failed': '❌', 'skipped': '⏭️',
                    'pending': '⬜', 'running': '🔄'
                }[s.status] || '⬜';
                return `<div class="mission-step ${s.status}">` +
                    `<span class="step-icon">${sIcon}</span> ` +
                    `<span class="step-id">${s.id}.</span> ` +
                    `<span class="step-text">${escapeHtml(s.text.slice(0, 70))}</span>` +
                    (s.error ? `<span class="step-error" title="${escapeHtml(s.error)}">⚠️</span>` : '') +
                    `</div>`;
            }).join('') + '</div>';
        }

        div.innerHTML = `
            <div class="mission-header">
                <span class="mission-status-icon">${statusIcon}</span>
                <span class="mission-desc">${escapeHtml(task.description.slice(0, 80))}</span>
                <span class="mission-id">#${task.id}</span>
            </div>
            <div class="mission-meta history-meta">
                <span>${completedSteps}/${totalSteps} steps${failedSteps > 0 ? ' · ' + failedSteps + ' failed' : ''}</span>
                <span>${pct}%</span>
                ${task.cost_usd ? `<span class="mission-cost">$${task.cost_usd.toFixed(4)} · ${task.cost_tokens || 0} tok</span>` : ''}
            </div>
            <div class="mission-timestamps">
                ${created ? `<span class="ts-item"><i class="fa-solid fa-clock"></i> ${created}</span>` : ''}
                ${duration ? `<span class="ts-item"><i class="fa-solid fa-stopwatch"></i> ${duration}</span>` : ''}
            </div>
            ${stepsHtml}
            ${task.final_summary ? '<div class="mission-summary">' + escapeHtml(task.final_summary.slice(0, 300)) + '</div>' : ''}
        `;

        // Toggle steps on click
        const header = div.querySelector('.mission-header');
        if (header && stepsHtml) {
            header.style.cursor = 'pointer';
            header.addEventListener('click', () => {
                const steps = div.querySelector('.history-steps');
                if (steps) steps.classList.toggle('collapsed');
            });
        }

        missionsHistoryList.appendChild(div);
    });
}

function formatTaskTime(isoString) {
    try {
        const d = new Date(isoString);
        const now = new Date();
        const diffMs = now - d;
        const diffMins = Math.floor(diffMs / 60000);
        const diffHrs = Math.floor(diffMs / 3600000);
        const diffDays = Math.floor(diffMs / 86400000);

        if (diffMins < 1) return 'just now';
        if (diffMins < 60) return `${diffMins}m ago`;
        if (diffHrs < 24) return `${diffHrs}h ago`;
        if (diffDays < 7) return `${diffDays}d ago`;
        return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
    } catch {
        return '';
    }
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
    } catch {
        return '';
    }
}

if (missionsBtn && missionsPanel) {
    missionsBtn.addEventListener('click', () => {
        openPanel('missions-panel', 'missions-btn');
        if (activeMissionsTab === 'history') {
            socket.emit('complex_task_action', { action: 'history' });
        } else {
            socket.emit('complex_task_action', { action: 'list' });
        }
    });
}
if (missionsClose) missionsClose.addEventListener('click', closeAllPanels);
if (missionsList) {
    missionsList.addEventListener('click', (e) => {
        const btn = e.target.closest('.mission-action');
        if (!btn) return;
        const card = btn.closest('.mission-card');
        if (!card) return;
        const taskId = card.dataset.taskId;
        const action = btn.dataset.action;
        socket.emit('complex_task_action', { action: action, task_id: taskId });
    });
}
socket.on('task_update', (data) => {
    console.log('🚀 Task update:', data);
    const idx = currentTasks.findIndex(t => t.id === data.id);
    if (idx >= 0) {
        currentTasks[idx] = data;
    } else {
        currentTasks.unshift(data);
    }
    renderMissions(currentTasks);
});
socket.on('tasks_list', (data) => {
    console.log('📋 Tasks list:', data);
    currentTasks = data.tasks || [];
    renderMissions(currentTasks);
});
socket.on('task_step_started', (data) => {
    console.log('▶️ Step started:', data);
});
socket.on('task_step_completed', (data) => {
    console.log('✅ Step completed:', data);
});
socket.on('task_step_failed', (data) => {
    console.log('❌ Step failed:', data);
});
socket.on('tasks_history', (data) => {
    console.log('📜 Tasks history:', data);
    renderHistory(data.tasks, data.stats);
});

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
    // States: idle, active (awake/listening), speaking, processing
    core.classList.remove('listening', 'speaking');

    // 'active' in JS maps to 'listening' CSS class (Orange/Red)
    // 'speaking' maps to 'speaking' CSS class (Cyan)

    if (state === 'active') { // Awake and Listening
        core.classList.add('listening');
    } else if (state === 'speaking' || state === 'processing') {
        core.classList.add('speaking');
    }
    // idle has no extra classes
}

function updateStatus(text, type = 'normal') {
    statusText.innerText = text.toUpperCase();
    if (type === 'online') {
        statusText.style.color = '#00f3ff';
        statusText.style.textShadow = '0 0 10px #00f3ff';
    } else {
        statusText.style.color = '#fff';
        statusText.style.textShadow = 'none';
    }
}

function addMessage(text) {
    const msgDiv = document.createElement('div');
    msgDiv.innerText = text;
    msgDiv.style.margin = "10px 0";
    msgDiv.style.animation = "fadeInUp 0.5s ease";

    // Keep only last 3 messages
    if (messageContainer.children.length > 2) {
        messageContainer.removeChild(messageContainer.firstChild);
    }

    messageContainer.appendChild(msgDiv);
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
            <span><i class="fa-solid fa-file-alt"></i> ${file.name}</span>
            <i class="fa-solid fa-trash remove-file" onclick="removeFile(${index})"></i>
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
        feedBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Processing...';
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
 *  UI UPGRADE v3 — clustered nav, automations, capabilities, toasts,
 *  reflection visibility.  Self-contained section appended below.
 * ====================================================================== */
(function () {
    const $ = (id) => document.getElementById(id);

    /* ------------------------------------------------------------------ *
     * 1. Generalized toast stack
     * ------------------------------------------------------------------ */
    const toastStack = $('toast-stack');

    window.showToast = function showToast(title, text, kind, timeout) {
        if (!toastStack) return;
        const el = document.createElement('div');
        el.className = `hud-toast glass ${kind || 'info'}`;
        el.innerHTML =
            `<div class="hud-toast-title"><i class="fa-solid ${
                kind === 'automation' ? 'fa-repeat' :
                kind === 'reflection' ? 'fa-rotate-left' :
                kind === 'success' ? 'fa-circle-check' :
                kind === 'error' ? 'fa-triangle-exclamation' : 'fa-bell'
            }"></i> ${title}</div>` +
            `<div class="hud-toast-body">${escapeHtml(String(text || '')).slice(0, 400)}</div>`;
        toastStack.appendChild(el);
        requestAnimationFrame(() => el.classList.add('show'));
        const ttl = timeout || (kind === 'error' ? 9000 : 6000);
        setTimeout(() => {
            el.classList.remove('show');
            setTimeout(() => el.remove(), 400);
        }, ttl);
    };

    socket.on('automation_fired', (data) => {
        showToast(`Automation #${data.id} ran`,
                  data.result ? `${data.action}\n\n${data.result}` : data.action,
                  'automation', 8000);
    });

    /* ------------------------------------------------------------------ *
     * 2. Nav launcher (overflow for small screens)
     * ------------------------------------------------------------------ */
    const navMoreBtn = $('nav-more-btn');
    const navLauncher = $('nav-launcher');
    const navLauncherClose = $('nav-launcher-close');

    function toggleLauncher(show) {
        if (!navLauncher) return;
        navLauncher.style.display = show ? 'block' : 'none';
    }
    if (navMoreBtn) navMoreBtn.addEventListener('click',
        () => toggleLauncher(navLauncher.style.display === 'none'));
    if (navLauncherClose) navLauncherClose.addEventListener('click',
        () => toggleLauncher(false));

    if (navLauncher) {
        navLauncher.addEventListener('click', (e) => {
            const item = e.target.closest('.launcher-item');
            if (!item) return;
            toggleLauncher(false);
            openPanel(item.dataset.panel, null);
        });
    }

    // Trigger → panel map used by both header and launcher
    const TRIGGER_PANEL = {
        'vision-btn': null,
        'meeting-btn': 'meeting-panel',
        'tasks-btn': 'tasks-panel',
        'notes-btn': 'notes-panel',
        'librarian-btn': 'librarian-panel',
        'memory-btn': 'memory-panel',
        'people-btn': 'people-panel',
        'dev-btn': null,
        'privacy-btn': 'privacy-panel',
        'missions-btn': 'missions-panel',
        'automations-btn': 'automations-panel',
        'skills-btn': 'capabilities-panel',
        'approvals-btn': 'approvals-panel',
    };
    Object.entries(TRIGGER_PANEL).forEach(([tid, panel]) => {
        if (!panel) return;                       // vision/dev keep own logic
        const btn = $(tid);
        if (btn && !btn.dataset.wired) {
            btn.dataset.wired = '1';
            btn.addEventListener('click', () => openPanel(panel, tid));
        }
    });

    /* ------------------------------------------------------------------ *
     * 3. Automations panel
     * ------------------------------------------------------------------ */
    const automationList = $('automation-list');
    let automationJobs = [];

    function renderAutomations() {
        if (!automationList) return;
        if (!automationJobs.length) {
            automationList.innerHTML =
                '<div class="empty-hint">No recurring automations yet.<br>' +
                'Try: <i>every day at 9am give me my briefing</i></div>';
            return;
        }
        automationList.innerHTML = automationJobs.map(j => {
            const hist = (j.history || []).map(h => `<span class="history-dot ${h.ok ? 'ok' : 'fail'}" title="${h.at} ${h.ok ? 'ok' : 'fail'}"></span>`).join('');
            return `<div class="list-item automation-item" data-id="${j.id}">
                <div class="item-main">
                    <span class="item-title">#${j.id} ${escapeHtml(j.describe || '')}</span>
                    <span class="item-sub">${escapeHtml(j.action)}</span>
                    <div class="item-meta">
                        ${j.next ? `<span>next: ${escapeHtml(j.next)}</span>` : ''}
                        ${j.last_fired ? `<span>last: ${escapeHtml(j.last_fired)}</span>` : ''}
                    </div>
                    ${hist ? `<div class="history-dots">${hist}</div>` : ''}
                </div>
                <div class="item-actions">
                    <button class="item-action" data-run="${j.id}" title="Run now"><i class="fa-solid fa-play"></i></button>
                    <button class="item-action danger" data-cancel="${j.id}" title="Cancel"><i class="fa-solid fa-trash"></i></button>
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
        automationAddBtn.disabled = true;
        socket.emit('automation_action', { action: 'add', text });
        automationInput.value = '';
        setTimeout(() => { automationAddBtn.disabled = false; }, 800);
    }
    if (automationAddBtn) automationAddBtn.addEventListener('click', submitAutomation);
    if (automationInput) automationInput.addEventListener('keypress', (e) => {
        if (e.key === 'Enter') submitAutomation();
    });

    if (automationList) {
        automationList.addEventListener('click', (e) => {
            const runBtn = e.target.closest('[data-run]');
            if (runBtn) {
                socket.emit('automation_action', { action: 'run_now', keyword: runBtn.dataset.run });
                return;
            }
            const btn = e.target.closest('[data-cancel]');
            if (!btn) return;
            socket.emit('automation_action',
                        { action: 'cancel', keyword: btn.dataset.cancel });
        });
    }

    // Refresh when panel opens (hook the existing openPanel)
    const _openPanel = window.openPanel || openPanel;
    window.openPanel = function (panelId, triggerId) {
        _openPanel(panelId, triggerId);
        if (panelId === 'automations-panel') refreshAutomations();
        if (panelId === 'capabilities-panel') {
            socket.emit('skills_action', { action: 'list' });
            socket.emit('tools_action', { action: 'list' });
        }
        if (panelId === 'approvals-panel') {
            socket.emit('approvals_list');
        }
    };

    /* ---- Approvals inbox ---- */
    const approvalsListEl = $('approvals-list');
    socket.on('approvals_update', (data) => {
        const pending = data.pending || [];
        if (!approvalsListEl) return;
        if (!pending.length) {
            approvalsListEl.innerHTML = '<div class="empty-hint">No pending approvals.</div>';
            return;
        }
        approvalsListEl.innerHTML = pending.map(p =>
            `<div class="list-item">
                <div class="item-main">
                    <span class="item-title">${escapeHtml(p.action)}</span>
                    <span class="item-sub">${escapeHtml(p.summary)}</span>
                </div>
                <div class="item-actions">
                    <button class="item-action approve" data-approve="${p.id}" title="Approve"><i class="fa-solid fa-check"></i></button>
                    <button class="item-action danger" data-deny="${p.id}" title="Deny"><i class="fa-solid fa-xmark"></i></button>
                </div>
            </div>`).join('');
    });
    if (approvalsListEl) {
        approvalsListEl.addEventListener('click', (e) => {
            const a = e.target.closest('[data-approve]');
            const d = e.target.closest('[data-deny]');
            if (a) socket.emit('approval_response', { id: a.dataset.approve, approved: true });
            else if (d) socket.emit('approval_response', { id: d.dataset.deny, approved: false });
        });
    }

    /* ---- Transcript search (Memory panel) ---- */
    const transcriptInput = $('transcript-search-input');
    const transcriptBtn = $('transcript-search-btn');
    const transcriptResults = $('transcript-results');
    function doTranscriptSearch() {
        const q = (transcriptInput.value || '').trim();
        if (!q) return;
        socket.emit('transcript_search', { query: q });
    }
    if (transcriptBtn) transcriptBtn.addEventListener('click', doTranscriptSearch);
    if (transcriptInput) transcriptInput.addEventListener('keypress', (e) => { if (e.key === 'Enter') doTranscriptSearch(); });
    socket.on('transcript_results', (data) => {
        if (!transcriptResults) return;
        const hits = data.results || [];
        if (!hits.length) {
            transcriptResults.innerHTML = `<div class="empty-hint">No matches for "${escapeHtml(data.query)}"</div>`;
            return;
        }
        transcriptResults.innerHTML = hits.map(h =>
            `<div class="list-item"><div class="item-main"><span class="item-sub">${escapeHtml(h.snippet)}</span><span class="item-meta">${escapeHtml(h.created.slice(0,16))}</span></div></div>`
        ).join('');
    });

    /* ------------------------------------------------------------------ *
     * 4. Capabilities panel (Skills + Tools)
     * ------------------------------------------------------------------ */
    document.querySelectorAll('[data-cab-tab]').forEach(tab => {
        tab.addEventListener('click', () => {
            document.querySelectorAll('[data-cab-tab]').forEach(t =>
                t.classList.remove('active'));
            tab.classList.add('active');
            const which = tab.dataset.cabTab;
            $('cab-skills-tab').style.display = which === 'skills' ? '' : 'none';
            $('cab-tools-tab').style.display = which === 'tools' ? '' : 'none';
        });
    });

    /* ---- inline args dialog ------------------------------------------ */
    function openArgsDialog(title, fields, onSubmit) {
        const backdrop = document.createElement('div');
        backdrop.className = 'arg-dialog-backdrop';
        backdrop.innerHTML =
            `<div class="arg-dialog glass">
                <div class="panel-header"><span>${escapeHtml(title)}</span>
                    <button class="close-btn arg-cancel"><i class="fa-solid fa-xmark"></i></button>
                </div>
                <div class="arg-fields">${fields.map(f =>
                    `<label class="arg-field">
                        <span>${escapeHtml(f.name)}${f.required ? ' *' : ''}
                            <em>${escapeHtml(f.description || f.type || '')}</em></span>
                        <input type="text" data-arg="${escapeHtml(f.name)}"
                               placeholder="${escapeHtml(f.type || 'value')}">
                    </label>`).join('')}
                </div>
                <button class="feed-btn arg-submit">RUN</button>
            </div>`;
        document.body.appendChild(backdrop);
        const close = () => backdrop.remove();
        backdrop.querySelector('.arg-cancel').addEventListener('click', close);
        backdrop.addEventListener('click', (e) => {
            if (e.target === backdrop) close(); });
        backdrop.querySelector('.arg-submit').addEventListener('click', () => {
            const values = {};
            let missing = null;
            backdrop.querySelectorAll('[data-arg]').forEach(inp => {
                const v = inp.value.trim();
                if (v) values[inp.dataset.arg] = v;
                else if (!missing) missing = inp.dataset.arg;
            });
            if (missing) { inp => inp; showToast('Missing value', `"${missing}" is required`, 'error'); return; }
            close();
            onSubmit(values);
        });
    }

    /* ---- skills ------------------------------------------------------- */
    let skillItems = [];
    const skillsListEl = $('skills-list');

    socket.on('skills_list', (data) => {
        skillItems = data.skills || [];
        if (!skillsListEl) return;
        if (!skillItems.length) {
            skillsListEl.innerHTML =
                '<div class="empty-hint">No skills saved yet.<br>Say: ' +
                '<i>"save this as a skill named X"</i> after a code task.</div>';
            return;
        }
        skillsListEl.innerHTML = skillItems.map(s =>
            `<div class="list-item skill-item" data-name="${escapeHtml(s.name)}">
                <div class="item-main">
                    <span class="item-title">${escapeHtml(s.display_name || s.name)}
                        <small>· ${s.runs || 0} runs</small></span>
                    <span class="item-sub">${escapeHtml(s.description || '(no description)')}</span>
                    ${s.params && s.params.length ?
                        `<span class="param-chips">${s.params.map(p =>
                            `<code>${escapeHtml(p)}</code>`).join('')}</span>` : ''}
                </div>
                <div class="item-actions">
                    <button class="item-action" data-run="${escapeHtml(s.name)}" title="Run">
                        <i class="fa-solid fa-play"></i></button>
                    <button class="item-action danger" data-del="${escapeHtml(s.name)}" title="Delete">
                        <i class="fa-solid fa-trash"></i></button>
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
                openArgsDialog(`Run skill: ${name}`,
                    params.map(p => ({ name: p, type: 'value', required: true })),
                    (values) => socket.emit('skills_action',
                        { action: 'run', name, params: values }));
            } else if (delBtn) {
                socket.emit('skills_action', { action: 'delete', name: delBtn.dataset.del });
            }
        });
    }

    /* ---- tools --------------------------------------------------------- */
    const toolsListEl = $('tools-list');

    socket.on('tools_list', (data) => {
        const tools = data.tools || [];
        if (!toolsListEl) return;
        if (!tools.length) {
            toolsListEl.innerHTML =
                '<div class="empty-hint">No external tools registered.<br>' +
                'Drop a JSON manifest into <b>tools_registry/</b>.</div>';
            return;
        }
        toolsListEl.innerHTML = tools.map(t =>
            `<div class="list-item tool-item" data-name="${escapeHtml(t.name)}">
                <div class="item-main">
                    <span class="item-title">
                        <span class="method-badge method-${(t.method || 'GET').toLowerCase()}">${t.method}</span>
                        ${escapeHtml(t.name)}</span>
                    <span class="item-sub">${escapeHtml(t.description)}</span>
                    ${Object.keys(t.params || {}).length ?
                        `<span class="param-chips">${Object.entries(t.params).map(([p, spec]) =>
                            `<code title="${escapeHtml(spec.description || '')}">${escapeHtml(p)}${spec.required ? '*' : ''}</code>`
                        ).join('')}</span>` : ''}
                </div>
                <div class="item-actions">
                    <button class="item-action" data-call="${escapeHtml(t.name)}" title="Call">
                        <i class="fa-solid fa-bolt"></i></button>
                </div>
            </div>`).join('');

        toolsListEl.querySelectorAll('[data-call]').forEach(btn => {
            btn.addEventListener('click', () => {
                const name = btn.dataset.call;
                const tool = tools.find(t => t.name === name) || {};
                const params = Object.entries(tool.params || {}).map(([p, spec]) =>
                    ({ name: p, type: spec.type || 'value',
                       description: spec.description || '',
                       required: !!spec.required }));
                if (!params.length) {
                    socket.emit('tools_action', { action: 'call', name, args: {} });
                    return;
                }
                openArgsDialog(`Call tool: ${name}`, params,
                    (values) => socket.emit('tools_action',
                        { action: 'call', name, args: values }));
            });
        });
    });

    /* ------------------------------------------------------------------ *
     * 5. Reflection visibility in Missions + skipped steps
     * ------------------------------------------------------------------ */
    socket.on('task_reflection', (data) => {
        showToast('Critic engaged',
                  `${data.assessment || 'Analyzing failures'} — recovery task ` +
                  `${data.child_id} running (${data.steps} step(s)).`,
                  'reflection', 7000);
        const card = document.querySelector(
            `.mission-card[data-task-id="${data.task_id}"]`);
        if (card && !card.querySelector('.reflection-badge')) {
            const badge = document.createElement('span');
            badge.className = 'reflection-badge';
            badge.title = data.assessment || '';
            badge.textContent = '↻ RECOVERY';
            card.querySelector('.mission-header')?.appendChild(badge);
        }
    });

    socket.on('task_step_skipped', (data) => {
        // Lightweight feed note so skips are visible even with panel closed
        if (data.step && data.step.error) {
            updateStatus(`Step ${data.step.id} skipped`);
        }
    });
})();

/* ====================================================================== *
 *  Guardrails UI — human-in-the-loop approval toasts
 * ====================================================================== */
(function () {
    const active = {};   // id -> toast element

    socket.on('approval_request', (data) => {
        if (!toastStack) return;
        if (active[data.id]) return;          // dedupe
        const el = document.createElement('div');
        el.className = 'hud-toast glass approval';
        el.innerHTML =
            `<div class="hud-toast-title"><i class="fa-solid fa-hand"></i> ` +
            `APPROVAL REQUIRED</div>` +
            `<div class="hud-toast-body">${escapeHtml(data.summary || data.action)}</div>` +
            `<div class="approval-actions">` +
            `<button class="approval-btn approve" data-id="${data.id}">` +
            `<i class="fa-solid fa-check"></i> APPROVE</button>` +
            `<button class="approval-btn deny" data-id="${data.id}">` +
            `<i class="fa-solid fa-xmark"></i> DENY</button>` +
            `</div>`;
        toastStack.appendChild(el);
        requestAnimationFrame(() => el.classList.add('show'));
        active[data.id] = el;

        const resolve = (approved) => {
            socket.emit('approval_response', { id: data.id, approved });
            el.classList.remove('show');
            setTimeout(() => el.remove(), 350);
            delete active[data.id];
            showToast(approved ? 'Approved' : 'Denied',
                      `${data.action} ${approved ? 'resuming…' : 'cancelled.'}`,
                      approved ? 'success' : 'error', 3000);
        };
        el.querySelector('.approve').addEventListener('click', () => resolve(true));
        el.querySelector('.deny').addEventListener('click', () => resolve(false));
        // auto-expire visually at timeout
        setTimeout(() => {
            if (active[data.id]) {
                el.classList.remove('show');
                setTimeout(() => el.remove(), 350);
                delete active[data.id];
            }
        }, (data.timeout || 120) * 1000);
    });
})();

/* ====================================================================== *
 *  R7 — Header/rail status widgets: circuit breaker + budget, polled
 *  from the existing /health endpoint.
 * ====================================================================== */
(function () {
    function fmtUsd(v) {
        return '$' + Number(v).toFixed(2);
    }

    async function pollHealth() {
        try {
            const res = await fetch('/health');
            if (!res.ok) return;
            const d = await res.json();

            // GUARD state
            const guard = document.getElementById('w-guard');
            if (guard && d.circuit_breaker) {
                const s = d.circuit_breaker;
                guard.textContent = s.toUpperCase();
                guard.className = 'rw-value num ' +
                    (s === 'ok' ? 'ok' : s === 'degraded' ? 'warn' : 'fail');
            }

            // BUDGET meter
            const b = d.budget || {};
            const txt = document.getElementById('w-budget-text');
            const bar = document.getElementById('w-budget-bar');
            if (txt) txt.textContent =
                `${fmtUsd(b.spent_usd || 0)} / ${fmtUsd(b.limit_usd || 0)}`;
            if (bar) {
                const pct = Math.min(100, b.pct || 0);
                bar.style.width = pct + '%';
                bar.style.background =
                    pct >= 90 ? 'var(--fail)' :
                    pct >= 70 ? 'var(--warn)' : 'var(--accent)';
            }
        } catch (_) { /* offline — leave last values */ }
    }

    pollHealth();
    setInterval(pollHealth, 5000);
})();
