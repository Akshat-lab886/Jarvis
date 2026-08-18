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
function sendText() {
    const text = chatInput.value.trim();
    if (text) {
        wakeUp(); // Manual input wakes him up
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
    tasksBtn.addEventListener('click', () => {
        tasksPanel.classList.toggle('active');
        addMessage("SYSTEM: To-Do Interface Opened");
    });
}

if (tasksClose) {
    tasksClose.addEventListener('click', () => tasksPanel.classList.remove('active'));
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
    librarianBtn.addEventListener('click', () => {
        librarianPanel.classList.add('active');
        addMessage("SYSTEM: Knowledge Interface Opened");
    });
}

if (librarianClose) {
    librarianClose.addEventListener('click', () => {
        librarianPanel.classList.remove('active');
    });
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
                setTimeout(() => {
                    librarianPanel.classList.remove('active');
                }, 1500);
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
