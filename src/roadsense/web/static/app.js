/**
 * RoadSense Real-Time Web Telemetry & Dashboard Engine
 */

// Global State
const state = {
  ws: null,
  wsConnected: false,
  audioEnabled: true,
  currentStatus: 'NORMAL',
  totalPackets: 0,
  totalEvents: 0,
  events: [],
  rawLines: [],
  isRecording: false,
  recordingStartTime: null,
  recordingTimerInterval: null,
  simProfile: 'normal',
  chartShock: null,
  chartDistance: null,
  maxChartPoints: 40,
  autoDrive: true,
};

// --- Web Audio Synthesizer for Hazard Chimes ---
class HazardAudio {
  constructor() {
    this.ctx = null;
  }
  init() {
    if (!this.ctx) {
      const AudioContext = window.AudioContext || window.webkitAudioContext;
      if (AudioContext) this.ctx = new AudioContext();
    }
  }
  playPothole() {
    if (!state.audioEnabled || !this.ctx) return;
    try {
      const t = this.ctx.currentTime;
      const osc = this.ctx.createOscillator();
      const gain = this.ctx.createGain();
      osc.type = 'sawtooth';
      osc.frequency.setValueAtTime(180, t);
      osc.frequency.exponentialRampToValueAtTime(60, t + 0.25);
      gain.gain.setValueAtTime(0.2, t);
      gain.gain.exponentialRampToValueAtTime(0.01, t + 0.25);
      osc.connect(gain);
      gain.connect(this.ctx.destination);
      osc.start(t);
      osc.stop(t + 0.25);
    } catch (e) {}
  }
  playSpeedBreaker() {
    if (!state.audioEnabled || !this.ctx) return;
    try {
      const t = this.ctx.currentTime;
      const osc = this.ctx.createOscillator();
      const gain = this.ctx.createGain();
      osc.type = 'sine';
      osc.frequency.setValueAtTime(420, t);
      osc.frequency.exponentialRampToValueAtTime(840, t + 0.18);
      gain.gain.setValueAtTime(0.18, t);
      gain.gain.exponentialRampToValueAtTime(0.01, t + 0.18);
      osc.connect(gain);
      gain.connect(this.ctx.destination);
      osc.start(t);
      osc.stop(t + 0.18);
    } catch (e) {}
  }
}
const audio = new HazardAudio();

// --- Document Initialization ---
document.addEventListener('DOMContentLoaded', () => {
  initTabs();
  initAudioToggle();
  initCharts();
  initCanvasVisualizer();
  initWebSocket();
  scanComPorts();
  loadSessionsList();

  // User interaction resumes audio context
  document.body.addEventListener('click', () => audio.init(), { once: true });
});

// --- Tab Navigation ---
function initTabs() {
  const tabButtons = document.querySelectorAll('.tab-btn');
  const tabContents = document.querySelectorAll('.tab-content');

  tabButtons.forEach(btn => {
    btn.addEventListener('click', () => {
      tabButtons.forEach(b => {
        b.classList.remove('active');
        b.setAttribute('aria-selected', 'false');
      });
      tabContents.forEach(c => c.classList.remove('active'));

      btn.classList.add('active');
      btn.setAttribute('aria-selected', 'true');
      const targetId = btn.dataset.tab;
      const targetEl = document.getElementById(targetId);
      if (targetEl) targetEl.classList.add('active');

      if (btn.dataset.tabAction === 'loadSessions') {
        loadSessionsList();
      }
    });
  });
}

function initAudioToggle() {
  const btn = document.getElementById('audioToggleBtn');
  const icon = document.getElementById('audioIcon');
  btn.addEventListener('click', () => {
    state.audioEnabled = !state.audioEnabled;
    audio.init();
    if (state.audioEnabled) {
      btn.classList.remove('muted');
      icon.className = 'fa-solid fa-volume-high';
    } else {
      btn.classList.add('muted');
      icon.className = 'fa-solid fa-volume-xmark';
    }
  });
}

// --- WebSocket Real-Time Stream ---
function initWebSocket() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = `${protocol}//${window.location.host}/ws/telemetry`;

  state.ws = new WebSocket(wsUrl);

  state.ws.onopen = () => {
    state.wsConnected = true;
    updateConnectionUI('CONNECTED', false);
    appendTerminalLine('[SYSTEM] Connected to RoadSense WebSocket telemetry stream.', 'term-system');
  };

  state.ws.onmessage = event => {
    try {
      const data = JSON.parse(event.data);
      handleWebSocketMessage(data);
    } catch (err) {
      console.error('WS parse error:', err);
    }
  };

  state.ws.onclose = () => {
    state.wsConnected = false;
    updateConnectionUI('DISCONNECTED', false);
    appendTerminalLine('[SYSTEM] WebSocket disconnected. Retrying in 2s...', 'term-err');
    setTimeout(initWebSocket, 2000);
  };

  state.ws.onerror = () => {
    state.wsConnected = false;
  };
}

function handleWebSocketMessage(data) {
  if (data.type === 'init') {
    if (data.status) updateStatusFields(data.status);
    if (data.raw_history) {
      data.raw_history.forEach(line => appendTerminalLine(line));
    }
    if (data.recent_events) {
      data.recent_events.forEach(evt => addEventToLog(evt, false));
    }
  } else if (data.type === 'packet') {
    handlePacket(data.packet);
    if (data.is_recording !== undefined) {
      updateRecordingUI(data.is_recording, data.session_counts);
    }
  } else if (data.type === 'hello') {
    appendTerminalLine(`HELLO: Firmware Version ${data.version} connected`, 'term-hello');
  }
}

// --- Packet Processing & Dashboard Update ---
function handlePacket(p) {
  state.totalPackets++;

  // Update Numbers
  document.getElementById('valShock').textContent = p.shock.toLocaleString();
  document.getElementById('valAy').textContent = (p.ay > 0 ? '+' : '') + p.ay.toLocaleString();
  document.getElementById('valDistance').textContent = p.distance_str.replace(' cm', '');
  document.getElementById('unitDistance').textContent = p.distance_cm === null ? 'NA' : 'cm';
  document.getElementById('valTotalPackets').textContent = state.totalPackets.toLocaleString();
  document.getElementById('valArdTime').textContent = `${p.arduino_time_ms.toLocaleString()} ms`;

  // Update Progress Bars
  const shockPercent = Math.min(100, (p.shock / 20000) * 100);
  document.getElementById('barShock').style.width = `${Math.max(4, shockPercent)}%`;

  const ayPercent = Math.min(100, Math.max(0, ((p.ay + 20000) / 40000) * 100));
  document.getElementById('barAy').style.width = `${ayPercent}%`;

  const distPercent = p.distance_cm === null ? 0 : Math.min(100, (p.distance_cm / 100) * 100);
  document.getElementById('barDistance').style.width = `${distPercent}%`;

  // Update Hero Condition Card
  updateHeroStatus(p.status, p.shock);

  // If Event Anomaly ('E')
  if (p.kind === 'E') {
    state.totalEvents++;
    document.getElementById('valTotalEvents').textContent = state.totalEvents.toLocaleString();
    document.getElementById('eventBadgeCount').textContent = state.totalEvents;
    addEventToLog(p, true);

    if (p.status === 'POTHOLE') audio.playPothole();
    else if (p.status === 'SPEED_BREAKER') audio.playSpeedBreaker();
  }

  // Update Charts
  pushChartData(p);

  // Update Terminal
  appendTerminalLine(p.raw, p.kind === 'E' ? 'term-e' : 'term-t');

  // Update Visualizer
  updateVisualizerData(p);
}

function updateHeroStatus(status, shock) {
  const card = document.getElementById('heroConditionCard');
  const text = document.getElementById('heroStatusText');
  const desc = document.getElementById('heroStatusSub');
  const icon = document.getElementById('heroStatusIcon');

  card.classList.remove('state-speedbreaker', 'state-pothole');

  if (status === 'POTHOLE') {
    card.classList.add('state-pothole');
    text.textContent = 'POTHOLE HAZARD DETECTED';
    desc.textContent = `Severe downward drop & shock impulse detected (${shock.toLocaleString()} raw).`;
    icon.className = 'fa-solid fa-triangle-exclamation';
  } else if (status === 'SPEED_BREAKER') {
    card.classList.add('state-speedbreaker');
    text.textContent = 'SPEED BREAKER DETECTED';
    desc.textContent = `Upward bump compression detected (${shock.toLocaleString()} raw).`;
    icon.className = 'fa-solid fa-wave-square';
  } else {
    text.textContent = 'NORMAL ROAD';
    desc.textContent = 'Road surface within standard parameters. Low vertical vibration.';
    icon.className = 'fa-solid fa-shield-halved';
  }
}

function updateStatusFields(statusObj) {
  updateConnectionUI(statusObj.connection_status, statusObj.is_simulator, statusObj.active_port);
  if (statusObj.is_recording) {
    startTimerUI(statusObj.session_duration_sec);
  } else {
    stopTimerUI();
  }
}

function updateConnectionUI(connStatus, isSim, portName) {
  const led = document.getElementById('connLed');
  const label = document.getElementById('connLabel');
  const sourceDesc = document.getElementById('sourceDesc');

  if (isSim) {
    led.className = 'status-led led-green';
    label.textContent = 'SIMULATOR ACTIVE';
    sourceDesc.textContent = 'Virtual Synthetic Stream';
  } else if (connStatus === 'Connected') {
    led.className = 'status-led led-green';
    label.textContent = `HARDWARE: ${portName || 'SERIAL'}`;
    sourceDesc.textContent = `Arduino USB (${portName})`;
  } else {
    led.className = 'status-led led-red';
    label.textContent = 'DISCONNECTED';
    sourceDesc.textContent = 'No Hardware Feed';
  }
}

// --- Event Log Table ---
function addEventToLog(evt, isNew = true) {
  state.events.unshift(evt);
  if (state.events.length > 50) state.events.pop();

  const tbody = document.getElementById('eventsTableBody');
  const emptyRow = tbody.querySelector('.empty-row');
  if (emptyRow) emptyRow.remove();

  const tr = document.createElement('tr');
  if (isNew) tr.style.animation = 'fadeIn 0.4s ease';

  const isPothole = evt.status === 'POTHOLE';
  const badgeClass = isPothole ? 'badge-pothole' : 'badge-speedbreaker';
  const badgeIcon = isPothole ? 'fa-triangle-exclamation' : 'fa-wave-square';

  const timeStr = evt.timestamp ? new Date(evt.timestamp).toLocaleTimeString() : new Date().toLocaleTimeString();

  tr.innerHTML = `
    <td><strong>${timeStr}</strong></td>
    <td><span class="badge-event ${badgeClass}"><i class="fa-solid ${badgeIcon}"></i> ${evt.status}</span></td>
    <td><strong class="font-mono ${isPothole ? 'text-danger' : 'text-warning'}">${evt.shock.toLocaleString()}</strong></td>
    <td class="font-mono">${evt.ay > 0 ? '+' : ''}${evt.ay.toLocaleString()}</td>
    <td class="font-mono">${evt.distance_str}</td>
    <td class="font-mono text-muted">${evt.arduino_time_ms} ms</td>
    <td><code class="font-mono text-muted">${evt.raw || ''}</code></td>
  `;

  tbody.insertBefore(tr, tbody.firstChild);
}

function filterEventTable(type) {
  const buttons = document.querySelectorAll('.events-filter-bar .btn');
  buttons.forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');

  const rows = document.querySelectorAll('#eventsTableBody tr');
  rows.forEach(r => {
    if (type === 'ALL') {
      r.style.display = '';
    } else {
      const txt = r.textContent;
      r.style.display = txt.includes(type) ? '' : 'none';
    }
  });
}

function clearLocalEventLog() {
  state.events = [];
  const tbody = document.getElementById('eventsTableBody');
  tbody.innerHTML = `<tr class="empty-row"><td colspan="7"><i class="fa-solid fa-shield-heart"></i> Feed cleared. Awaiting new events...</td></tr>`;
}

// --- Terminal Log ---
function appendTerminalLine(text, className = 'term-t') {
  const terminal = document.getElementById('rawTerminalFeed');
  if (!terminal) return;

  const lineEl = document.createElement('div');
  lineEl.className = `term-line ${className}`;
  lineEl.textContent = text;
  terminal.appendChild(lineEl);

  state.rawLines.push(text);
  if (state.rawLines.length > 80) {
    state.rawLines.shift();
    if (terminal.firstChild) terminal.removeChild(terminal.firstChild);
  }

  document.getElementById('termLinesCount').textContent = `${state.rawLines.length} lines`;
  terminal.scrollTop = terminal.scrollHeight;
}

function clearTerminal() {
  const terminal = document.getElementById('rawTerminalFeed');
  terminal.innerHTML = '<div class="term-line term-system">[SYSTEM] Terminal buffer cleared.</div>';
  state.rawLines = [];
  document.getElementById('termLinesCount').textContent = '0 lines';
}

// --- Real-time Chart.js Setup ---
function initCharts() {
  Chart.defaults.color = '#94a3b8';
  Chart.defaults.font.family = 'JetBrains Mono, Inter, sans-serif';

  // 1. Shock & AY Chart
  const ctxShock = document.getElementById('shockChart').getContext('2d');
  state.chartShock = new Chart(ctxShock, {
    type: 'line',
    data: {
      labels: Array(state.maxChartPoints).fill(''),
      datasets: [
        {
          label: 'Shock |ΔAY|',
          data: Array(state.maxChartPoints).fill(0),
          borderColor: '#00f2fe',
          backgroundColor: 'rgba(0, 242, 254, 0.1)',
          fill: true,
          tension: 0.35,
          borderWidth: 2,
          pointRadius: 0,
          yAxisID: 'yShock',
        },
        {
          label: 'Vertical AY',
          data: Array(state.maxChartPoints).fill(0),
          borderColor: '#a855f7',
          backgroundColor: 'transparent',
          tension: 0.35,
          borderWidth: 1.5,
          borderDash: [4, 4],
          pointRadius: 0,
          yAxisID: 'yAy',
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      scales: {
        x: { display: false },
        yShock: {
          position: 'left',
          min: 0,
          max: 25000,
          grid: { color: 'rgba(255, 255, 255, 0.05)' },
          ticks: { font: { size: 10 } },
        },
        yAy: {
          position: 'right',
          min: -20000,
          max: 20000,
          grid: { display: false },
          ticks: { font: { size: 10 } },
        },
      },
      plugins: { legend: { display: false } },
    },
  });

  // 2. Ultrasonic Distance Chart
  const ctxDistance = document.getElementById('distanceChart').getContext('2d');
  state.chartDistance = new Chart(ctxDistance, {
    type: 'line',
    data: {
      labels: Array(state.maxChartPoints).fill(''),
      datasets: [
        {
          label: 'Clearance (cm)',
          data: Array(state.maxChartPoints).fill(25),
          borderColor: '#10b981',
          backgroundColor: 'rgba(16, 185, 129, 0.12)',
          fill: true,
          tension: 0.3,
          borderWidth: 2,
          pointRadius: 0,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      scales: {
        x: { display: false },
        y: {
          min: 0,
          max: 100,
          grid: { color: 'rgba(255, 255, 255, 0.05)' },
          ticks: { font: { size: 10 } },
        },
      },
      plugins: { legend: { display: false } },
    },
  });
}

function pushChartData(p) {
  if (!state.chartShock || !state.chartDistance) return;

  // Shock & AY
  const shockData = state.chartShock.data.datasets[0].data;
  const ayData = state.chartShock.data.datasets[1].data;
  shockData.push(p.shock);
  ayData.push(p.ay);
  if (shockData.length > state.maxChartPoints) {
    shockData.shift();
    ayData.shift();
  }
  state.chartShock.update('none');

  // Distance (convert NA to null or keep previous)
  const distData = state.chartDistance.data.datasets[0].data;
  distData.push(p.distance_cm !== null ? p.distance_cm : 0);
  if (distData.length > state.maxChartPoints) {
    distData.shift();
  }
  state.chartDistance.update('none');
}

// --- Interactive 2D Suspension & Road Simulator (Canvas) ---
let roadOffset = 0;
let carPitch = 0;
let suspCompression = 0;
let currentDistCm = 25;
let currentObstacleName = 'None (Clear Road)';

function updateVisualizerData(p) {
  currentDistCm = p.distance_cm !== null ? p.distance_cm : 25;
  // Kinetic reaction
  suspCompression = (p.shock / 25000) * 22; // in mm
  carPitch = (p.ay / 20000) * 7.5; // in degrees

  document.getElementById('visPitch').textContent = `${carPitch.toFixed(1)}°`;
  document.getElementById('visSusp').textContent = `${suspCompression.toFixed(1)} mm`;
  document.getElementById('visBeam').textContent = p.distance_str;

  if (p.status === 'POTHOLE') currentObstacleName = '⚠ Pothole Trench';
  else if (p.status === 'SPEED_BREAKER') currentObstacleName = '⚡ Speed Bump';
  else currentObstacleName = 'None (Smooth Road)';
  document.getElementById('visObstacle').textContent = currentObstacleName;
}

function initCanvasVisualizer() {
  const canvas = document.getElementById('roadCanvas');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');

  document.getElementById('chkAutoDrive').addEventListener('change', e => {
    state.autoDrive = e.target.checked;
  });

  function render() {
    if (state.autoDrive) roadOffset = (roadOffset + 4) % 1000;

    const w = canvas.width;
    const h = canvas.height;

    // Clear background
    ctx.fillStyle = '#060a14';
    ctx.fillRect(0, 0, w, h);

    // Horizon & sky grid
    ctx.strokeStyle = 'rgba(56, 189, 248, 0.08)';
    ctx.lineWidth = 1;
    for (let y = 40; y < 240; y += 30) {
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(w, y);
      ctx.stroke();
    }

    const roadBaseY = 270;

    // Draw Road Surface with Moving Dashes
    ctx.fillStyle = '#111827';
    ctx.fillRect(0, roadBaseY, w, h - roadBaseY);

    ctx.strokeStyle = '#38bdf8';
    ctx.lineWidth = 3;
    ctx.beginPath();
    ctx.moveTo(0, roadBaseY);
    ctx.lineTo(w, roadBaseY);
    ctx.stroke();

    // Road Lane Markings
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.35)';
    ctx.lineWidth = 4;
    ctx.setLineDash([30, 25]);
    ctx.lineDashOffset = -roadOffset;
    ctx.beginPath();
    ctx.moveTo(0, roadBaseY + 45);
    ctx.lineTo(w, roadBaseY + 45);
    ctx.stroke();
    ctx.setLineDash([]);

    // Vehicle Chassis Geometry
    const carX = 400;
    const carY = roadBaseY - 55 + (suspCompression * 0.8);

    ctx.save();
    ctx.translate(carX + 90, carY + 30);
    ctx.rotate((carPitch * Math.PI) / 180);
    ctx.translate(-(carX + 90), -(carY + 30));

    // Vehicle Body Shell
    ctx.fillStyle = 'rgba(2, 132, 199, 0.85)';
    ctx.strokeStyle = '#00f2fe';
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.roundRect(carX, carY, 180, 42, [14, 18, 4, 4]);
    ctx.fill();
    ctx.stroke();

    // Car Roof / Cabin
    ctx.fillStyle = 'rgba(15, 23, 42, 0.9)';
    ctx.beginPath();
    ctx.roundRect(carX + 35, carY - 26, 100, 28, [12, 12, 0, 0]);
    ctx.fill();
    ctx.stroke();

    // Windows
    ctx.fillStyle = 'rgba(56, 189, 248, 0.35)';
    ctx.fillRect(carX + 45, carY - 20, 36, 18);
    ctx.fillRect(carX + 88, carY - 20, 38, 18);

    // Headlight Beam
    ctx.fillStyle = 'rgba(0, 242, 254, 0.12)';
    ctx.beginPath();
    ctx.moveTo(carX + 180, carY + 12);
    ctx.lineTo(carX + 340, carY - 10);
    ctx.lineTo(carX + 360, roadBaseY + 40);
    ctx.lineTo(carX + 180, carY + 32);
    ctx.closePath();
    ctx.fill();

    // Downward Ultrasonic Rangefinder Ray (HC-SR04)
    ctx.strokeStyle = 'rgba(16, 185, 129, 0.85)';
    ctx.lineWidth = 2;
    ctx.setLineDash([5, 4]);
    ctx.beginPath();
    ctx.moveTo(carX + 160, carY + 40);
    ctx.lineTo(carX + 160, roadBaseY);
    ctx.stroke();
    ctx.setLineDash([]);

    // Ultrasonic Target Echo Pulse
    ctx.fillStyle = '#10b981';
    ctx.beginPath();
    ctx.arc(carX + 160, roadBaseY, 5, 0, Math.PI * 2);
    ctx.fill();

    // Suspension Springs & Dampers
    ctx.strokeStyle = '#f59e0b';
    ctx.lineWidth = 3;
    // Front suspension
    ctx.beginPath();
    ctx.moveTo(carX + 145, carY + 40);
    ctx.lineTo(carX + 145, roadBaseY - 12);
    ctx.stroke();
    // Rear suspension
    ctx.beginPath();
    ctx.moveTo(carX + 35, carY + 40);
    ctx.lineTo(carX + 35, roadBaseY - 12);
    ctx.stroke();

    ctx.restore();

    // Wheels (Stay grounded on the road)
    const wheelRadius = 18;
    const wheelAngle = (roadOffset * 0.1) % (Math.PI * 2);

    function drawWheel(wx, wy) {
      ctx.save();
      ctx.translate(wx, wy);
      ctx.rotate(wheelAngle);
      ctx.fillStyle = '#1f2937';
      ctx.strokeStyle = '#94a3b8';
      ctx.lineWidth = 3;
      ctx.beginPath();
      ctx.arc(0, 0, wheelRadius, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
      // Wheel Spokes
      ctx.strokeStyle = '#00f2fe';
      ctx.lineWidth = 2;
      for (let a = 0; a < 4; a++) {
        ctx.rotate(Math.PI / 2);
        ctx.beginPath();
        ctx.moveTo(0, 0);
        ctx.lineTo(wheelRadius - 2, 0);
        ctx.stroke();
      }
      ctx.restore();
    }

    drawWheel(carX + 35, roadBaseY - wheelRadius + 4);
    drawWheel(carX + 145, roadBaseY - wheelRadius + 4);

    requestAnimationFrame(render);
  }

  requestAnimationFrame(render);
}

// --- Simulator Profile & Test Events ---
function setSimProfile(profile) {
  state.simProfile = profile;
  ['btnSimNormal', 'btnSimBumpy', 'btnSimHighway'].forEach(id => {
    document.getElementById(id).classList.remove('active');
  });

  if (profile === 'normal') document.getElementById('btnSimNormal').classList.add('active');
  else if (profile === 'bumpy') document.getElementById('btnSimBumpy').classList.add('active');
  else if (profile === 'highway') document.getElementById('btnSimHighway').classList.add('active');

  fetch('/api/simulator/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ profile }),
  });
}

function triggerEvent(eventType) {
  audio.init();
  fetch('/api/simulator/trigger', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ event_type: eventType }),
  });
}

// --- Session Recording & SQLite Database ---
function startRecordingSession() {
  const name = document.getElementById('sessionNameInput').value;
  const notes = document.getElementById('sessionNotesInput').value;

  fetch('/api/recording/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, notes }),
  })
    .then(r => r.json())
    .then(res => {
      if (res.status === 'ok') {
        state.isRecording = true;
        document.getElementById('sessionNameInput').value = '';
        document.getElementById('sessionNotesInput').value = '';
        startTimerUI();
        document.getElementById('recSessionId').textContent = `#${res.session_id} (${res.session_name})`;
      }
    });
}

function stopRecordingSession() {
  fetch('/api/recording/stop', { method: 'POST' })
    .then(r => r.json())
    .then(() => {
      state.isRecording = false;
      stopTimerUI();
      loadSessionsList();
    });
}

function updateRecordingUI(isRec, counts) {
  state.isRecording = isRec;
  const pill = document.getElementById('recordingPill');
  const btnStart = document.getElementById('btnStartRecording');
  const btnStop = document.getElementById('btnStopRecording');
  const statsBox = document.getElementById('activeSessionStats');

  if (isRec) {
    pill.className = 'status-pill recording-pill active';
    document.getElementById('recordingText').textContent = 'RECORDING';
    document.getElementById('recordingTimer').style.display = 'inline';
    btnStart.style.display = 'none';
    btnStop.style.display = 'inline-flex';
    statsBox.style.display = 'flex';
    if (counts) {
      document.getElementById('recTeleCount').textContent = counts.telemetry.toLocaleString();
      document.getElementById('recEventCount').textContent = counts.events.toLocaleString();
    }
  } else {
    pill.className = 'status-pill recording-pill inactive';
    document.getElementById('recordingText').textContent = 'STANDBY';
    document.getElementById('recordingTimer').style.display = 'none';
    btnStart.style.display = 'inline-flex';
    btnStop.style.display = 'none';
    statsBox.style.display = 'none';
  }
}

function startTimerUI(initialSec = 0) {
  let seconds = Math.floor(initialSec);
  if (state.recordingTimerInterval) clearInterval(state.recordingTimerInterval);

  function formatTime(s) {
    const mins = Math.floor(s / 60).toString().padStart(2, '0');
    const remSec = (s % 60).toString().padStart(2, '0');
    return `${mins}:${remSec}`;
  }

  document.getElementById('recordingTimer').textContent = formatTime(seconds);
  document.getElementById('recDuration').textContent = formatTime(seconds);

  state.recordingTimerInterval = setInterval(() => {
    seconds++;
    const str = formatTime(seconds);
    document.getElementById('recordingTimer').textContent = str;
    document.getElementById('recDuration').textContent = str;
  }, 1000);
}

function stopTimerUI() {
  if (state.recordingTimerInterval) clearInterval(state.recordingTimerInterval);
  state.recordingTimerInterval = null;
}

function loadSessionsList() {
  fetch('/api/sessions')
    .then(r => r.json())
    .then(data => {
      const tbody = document.getElementById('sessionsTableBody');
      if (!data.sessions || data.sessions.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" class="text-center py-4">No recorded sessions in SQLite yet. Click "Start Recording" to create one!</td></tr>';
        return;
      }

      tbody.innerHTML = data.sessions
        .map(s => {
          const dateStr = s.started_at ? new Date(s.started_at).toLocaleString() : '--';
          return `
          <tr>
            <td><strong>#${s.id}</strong></td>
            <td><strong>${s.name}</strong><br><small class="text-muted">${s.notes || ''}</small></td>
            <td>${dateStr}</td>
            <td class="font-mono">${s.duration}</td>
            <td><span class="badge-tag tag-blue font-mono">${s.telemetry_count.toLocaleString()} rows</span></td>
            <td><span class="badge-tag tag-amber font-mono">${s.event_count} events</span></td>
            <td>
              <a href="/api/sessions/${s.id}/export/zip" class="btn btn-xs btn-primary" title="Download telemetry.csv, events.csv, session_metadata.csv">
                <i class="fa-solid fa-download"></i> CSV ZIP
              </a>
            </td>
          </tr>
        `;
        })
        .join('');
    })
    .catch(() => {
      document.getElementById('sessionsTableBody').innerHTML = '<tr><td colspan="7" class="text-center py-4 text-danger">Failed to load SQLite sessions</td></tr>';
    });
}

// --- Hardware Serial COM Management ---
function scanComPorts() {
  const select = document.getElementById('comPortSelect');
  select.innerHTML = '<option value="">Scanning USB ports...</option>';

  fetch('/api/ports')
    .then(r => r.json())
    .then(data => {
      select.innerHTML = '';
      if (!data.ports || data.ports.length === 0) {
        select.innerHTML = '<option value="">No Arduino ports found (Use Simulator)</option>';
        return;
      }
      data.ports.forEach(p => {
        const opt = document.createElement('option');
        opt.value = p.port;
        opt.textContent = `${p.port} - ${p.description}`;
        select.appendChild(opt);
      });
    })
    .catch(() => {
      select.innerHTML = '<option value="">Failed to scan ports</option>';
    });
}

function connectHardwareSerial() {
  const port = document.getElementById('comPortSelect').value;
  const baud = parseInt(document.getElementById('baudRateSelect').value) || 115200;

  if (!port) {
    alert('Please select a valid COM port, or switch to Simulator Mode.');
    return;
  }

  fetch('/api/connect', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ port, baud }),
  })
    .then(r => r.json())
    .then(res => {
      if (res.status === 'ok') {
        document.getElementById('btnConnectSerial').style.display = 'none';
        document.getElementById('btnDisconnectSerial').style.display = 'inline-flex';
        appendTerminalLine(`[SYSTEM] Connected to hardware serial port: ${port}`, 'term-system');
      } else {
        alert(`Connection failed: ${res.detail || res.message}`);
      }
    });
}

function disconnectHardwareSerial() {
  fetch('/api/disconnect', { method: 'POST' }).then(() => {
    document.getElementById('btnConnectSerial').style.display = 'inline-flex';
    document.getElementById('btnDisconnectSerial').style.display = 'none';
    appendTerminalLine('[SYSTEM] Serial port disconnected.', 'term-system');
  });
}

function toggleSimulationMode() {
  fetch('/api/simulator/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ profile: 'normal' }),
  }).then(() => {
    appendTerminalLine('[SYSTEM] Switched to Developer Simulation Mode.', 'term-system');
  });
}
