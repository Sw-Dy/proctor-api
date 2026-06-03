"""
╔══════════════════════════════════════════════════════════════════╗
║        AI EXAM PROCTORING SYSTEM — Flask REST API Service        ║
║        Wrapper for proctor.py to enable remote monitoring        ║
║        Stack: Flask · WebSockets · Threading                     ║
╚══════════════════════════════════════════════════════════════════╝

Installation:
    pip install flask flask-cors flask-socketio python-socketio

Run:
    python proctor_service.py

API Endpoints:
    GET  /api/status          — Get current session status
    POST /api/start           — Start proctoring session
    POST /api/stop            — Stop proctoring session
    GET  /api/stats           — Get session statistics
    POST /api/action          — Perform action (screenshot, reset)
    WS   /socket.io           — WebSocket for real-time updates
"""

import cv2
import json
import threading
import time
import queue
import numpy as np
from datetime import datetime
from flask import Flask, jsonify, request, render_template_string
from flask_cors import CORS
from flask_socketio import SocketIO, emit

# Import the original proctor system
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
from proctor import (
    ProctorSystem, FaceData, GazeData, HeadPoseData, 
    MouthData, AudioData, SessionStats, CONFIG
)

# ─────────────────────────────────────────────────────────────────
#  FLASK APPLICATION SETUP
# ─────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.config['SECRET_KEY'] = 'proctor-secret-key-change-in-production'
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

# ─────────────────────────────────────────────────────────────────
#  GLOBAL STATE MANAGEMENT
# ─────────────────────────────────────────────────────────────────

class ProctorServiceManager:
    """Manages the ProctorSystem as a background service."""
    
    def __init__(self):
        self.proctor_system: ProctorSystem = None
        self.system_thread: threading.Thread = None
        self._running = False
        self._state = {
            "status": "idle",          # idle, running, stopped
            "session_id": None,
            "start_time": None,
            "uptime_sec": 0.0,
            "frame_count": 0,
            "violations": 0,
            "suspicious_score": 0.0,
            "avg_score": 0.0,
            "max_score": 0.0,
            "total_blinks": 0,
            "face_detected": False,
            "gaze_away": False,
            "head_turned": False,
            "mouth_open": False,
            "audio_rms": 0.0,
        }
        self._state_lock = threading.Lock()
        self._stats_history = queue.Queue(maxsize=100)

    def start_session(self):
        """Start a new proctoring session in background thread."""
        if self._running:
            return {"error": "Session already running"}

        self._running = True
        self.proctor_system = ProctorSystem()
        self._state["status"] = "running"
        self._state["session_id"] = self.proctor_system.session_id
        self._state["start_time"] = datetime.now().isoformat()
        
        self.system_thread = threading.Thread(
            target=self._run_system_loop, daemon=True
        )
        self.system_thread.start()
        
        return {
            "status": "started",
            "session_id": self.proctor_system.session_id,
            "timestamp": datetime.now().isoformat()
        }

    def _run_system_loop(self):
        """Run the ProctorSystem and emit state updates via WebSocket."""
        try:
            # Start camera and audio
            if not self.proctor_system.camera.start():
                print("[Service] Cannot start camera")
                self._running = False
                return

            if self.proctor_system.audio_monitor:
                self.proctor_system.audio_monitor.start()

            self.proctor_system._log_thread.start()
            print("[Service] Proctoring session started")

            frame_count = 0
            while self._running:
                ret, raw_frame = self.proctor_system.camera.read()
                if not ret or raw_frame is None:
                    time.sleep(0.01)
                    continue

                frame_count += 1
                now = time.time()
                elapsed = now - self.proctor_system._start_time
                h, w = raw_frame.shape[:2]

                should_analyze = (frame_count % CONFIG["PROCESS_EVERY_N_FRAMES"] == 0)
                all_violations = []

                if should_analyze:
                    # Vision analysis
                    face_data, annotated = self.proctor_system.analyzer.process(raw_frame)

                    if face_data.detected and face_data.landmarks:
                        lm = face_data.landmarks
                        l_ear, r_ear, looking_away = self.proctor_system.eye_tracker.gaze_score(
                            lm, w, h, self.proctor_system.analyzer)
                        avg_ear = (l_ear + r_ear) / 2.0
                        gaze_data = GazeData(
                            looking_away=looking_away,
                            left_ear=l_ear,
                            right_ear=r_ear,
                            avg_ear=avg_ear,
                            blink_count=self.proctor_system.eye_tracker.blink_count,
                            blink_rate=self.proctor_system.eye_tracker.blink_rate(),
                        )
                        head_data = self.proctor_system.head_pose.estimate(lm, w, h)
                        mouth_data = self.proctor_system.mouth.analyze(lm, w, h)
                    else:
                        annotated = raw_frame.copy()
                        gaze_data = GazeData()
                        head_data = HeadPoseData()
                        mouth_data = MouthData()

                    vis_violations = self.proctor_system.detector.update(
                        face_data, gaze_data, head_data, mouth_data, now)
                    all_violations.extend(vis_violations)

                    # Audio analysis
                    if self.proctor_system.audio_monitor and self.proctor_system.audio_monitor._available:
                        audio_data, aud_violations = \
                            self.proctor_system.audio_monitor.get_audio_data(now)
                        all_violations.extend(aud_violations)
                    else:
                        audio_data = AudioData()

                    self.proctor_system._last_face = face_data
                    self.proctor_system._last_gaze = gaze_data
                    self.proctor_system._last_head = head_data
                    self.proctor_system._last_mouth = mouth_data
                    self.proctor_system._last_audio = audio_data

                    if all_violations:
                        self.proctor_system._handle_violations(all_violations, annotated)

                    # Update state for WebSocket broadcast
                    with self._state_lock:
                        self._state["frame_count"] = self.proctor_system._frame_count
                        self._state["uptime_sec"] = elapsed
                        self._state["violations"] = self.proctor_system._total_violations
                        self._state["suspicious_score"] = self.proctor_system.detector.suspicious_score
                        self._state["avg_score"] = round(self.proctor_system.detector.avg_score, 2)
                        self._state["max_score"] = round(self.proctor_system.detector.max_score, 2)
                        self._state["total_blinks"] = self.proctor_system.eye_tracker.blink_count
                        self._state["face_detected"] = face_data.detected
                        self._state["gaze_away"] = gaze_data.looking_away
                        self._state["head_turned"] = head_data.is_turned
                        self._state["mouth_open"] = mouth_data.is_open
                        self._state["audio_rms"] = audio_data.rms

                    # Emit update every 100ms
                    if frame_count % 3 == 0:
                        socketio.emit('state_update', self._get_state())

                time.sleep(0.005)

        except Exception as e:
            print(f"[Service] Error in system loop: {e}")
            self._running = False
        finally:
            self._shutdown_system()

    def stop_session(self):
        """Stop the current proctoring session."""
        if not self._running:
            return {"error": "No session running"}

        self._running = False
        if self.system_thread:
            self.system_thread.join(timeout=5.0)

        return {
            "status": "stopped",
            "session_id": self._state["session_id"],
            "total_violations": self._state["violations"],
            "timestamp": datetime.now().isoformat()
        }

    def _shutdown_system(self):
        """Clean up system resources."""
        if self.proctor_system:
            self.proctor_system._shutdown()
        self._state["status"] = "stopped"
        print("[Service] Session stopped")

    def perform_action(self, action: str):
        """Perform a control action."""
        if not self._running or not self.proctor_system:
            return {"error": "No session running"}

        if action == "screenshot":
            if self.proctor_system._last_frame is not None:
                path = self.proctor_system.logger.save_screenshot(
                    self.proctor_system._last_frame, "API_SCREENSHOT")
                return {"action": "screenshot", "path": path}
            return {"error": "No frame available"}

        elif action == "reset":
            self.proctor_system.detector.reset_counters()
            self.proctor_system._total_violations = 0
            return {"action": "reset", "status": "counters cleared"}

        return {"error": f"Unknown action: {action}"}

    def _get_state(self) -> dict:
        """Get current state snapshot."""
        with self._state_lock:
            state = self._state.copy()
            # Ensure all values are JSON-serializable
            # Convert numpy bools and floats to Python native types
            for key in state:
                if isinstance(state[key], (bool, np.bool_)):
                    state[key] = bool(state[key])
                elif isinstance(state[key], (float, np.floating)):
                    state[key] = float(state[key])
                elif isinstance(state[key], (int, np.integer)):
                    state[key] = int(state[key])
            return state

    def get_session_stats(self) -> dict:
        """Get full session statistics."""
        if not self.proctor_system:
            return {"error": "No session running"}

        with self._state_lock:
            return {
                "session_id": self._state["session_id"],
                "start_time": self._state["start_time"],
                "uptime_sec": self._state["uptime_sec"],
                "frame_count": self._state["frame_count"],
                "total_violations": self._state["violations"],
                "suspicious_score": self._state["suspicious_score"],
                "avg_score": self._state["avg_score"],
                "max_score": self._state["max_score"],
                "total_blinks": self._state["total_blinks"],
                "violation_counts": self.proctor_system._violation_counts,
            }


# Global service manager
service_manager = ProctorServiceManager()


# ─────────────────────────────────────────────────────────────────
#  REST API ENDPOINTS
# ─────────────────────────────────────────────────────────────────

@app.route('/api/status', methods=['GET'])
def api_status():
    """Get current system status."""
    return jsonify(service_manager._get_state())


@app.route('/api/start', methods=['POST'])
def api_start():
    """Start a new proctoring session."""
    result = service_manager.start_session()
    return jsonify(result), 200 if "error" not in result else 400


@app.route('/api/stop', methods=['POST'])
def api_stop():
    """Stop the current proctoring session."""
    result = service_manager.stop_session()
    return jsonify(result), 200 if "error" not in result else 400


@app.route('/api/stats', methods=['GET'])
def api_stats():
    """Get detailed session statistics."""
    result = service_manager.get_session_stats()
    return jsonify(result), 200 if "error" not in result else 400


@app.route('/api/action', methods=['POST'])
def api_action():
    """Perform a control action (screenshot, reset)."""
    data = request.get_json()
    action = data.get('action') if data else None
    
    if not action:
        return jsonify({"error": "Missing 'action' field"}), 400

    result = service_manager.perform_action(action)
    return jsonify(result), 200 if "error" not in result else 400


@app.route('/api/config', methods=['GET'])
def api_config():
    """Get current configuration."""
    return jsonify({"config": CONFIG})


@app.route('/', methods=['GET'])
def index():
    """Serve the web dashboard."""
    return render_template_string(DASHBOARD_HTML)


# ─────────────────────────────────────────────────────────────────
#  WEBSOCKET EVENTS
# ─────────────────────────────────────────────────────────────────

@socketio.on('connect')
def on_connect():
    """Handle WebSocket connection."""
    print("[WebSocket] Client connected")
    emit('connect_response', {'status': 'connected'})


@socketio.on('disconnect')
def on_disconnect():
    """Handle WebSocket disconnection."""
    print("[WebSocket] Client disconnected")


@socketio.on('request_state')
def on_request_state():
    """Client requests current state."""
    emit('state_update', service_manager._get_state())


# ─────────────────────────────────────────────────────────────────
#  WEB DASHBOARD (Embedded HTML)
# ─────────────────────────────────────────────────────────────────

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AI Exam Proctor Service Dashboard</title>
    <script src="https://cdn.socket.io/4.5.4/socket.io.min.js"></script>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            background: linear-gradient(135deg, #1e1e2e 0%, #2d2d44 100%);
            color: #e0e0e0;
            min-height: 100vh;
            padding: 20px;
        }

        .container {
            max-width: 1200px;
            margin: 0 auto;
        }

        header {
            text-align: center;
            margin-bottom: 40px;
            padding: 20px;
            background: rgba(0, 180, 255, 0.1);
            border-radius: 12px;
            border: 1px solid rgba(0, 180, 255, 0.3);
        }

        h1 {
            font-size: 2.5em;
            margin-bottom: 10px;
            background: linear-gradient(135deg, #00b4ff, #0096ff);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }

        .controls {
            display: flex;
            gap: 15px;
            justify-content: center;
            margin-top: 20px;
            flex-wrap: wrap;
        }

        button {
            padding: 12px 30px;
            border: none;
            border-radius: 8px;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s ease;
            text-transform: uppercase;
            letter-spacing: 1px;
        }

        .btn-primary {
            background: linear-gradient(135deg, #00b4ff, #0096ff);
            color: #000;
        }

        .btn-primary:hover {
            transform: translateY(-2px);
            box-shadow: 0 8px 20px rgba(0, 180, 255, 0.3);
        }

        .btn-danger {
            background: linear-gradient(135deg, #ff6b6b, #ff4757);
            color: #fff;
        }

        .btn-danger:hover {
            transform: translateY(-2px);
            box-shadow: 0 8px 20px rgba(255, 107, 107, 0.3);
        }

        .btn-secondary {
            background: rgba(200, 200, 200, 0.2);
            color: #e0e0e0;
            border: 1px solid rgba(200, 200, 200, 0.4);
        }

        .btn-secondary:hover {
            background: rgba(200, 200, 200, 0.3);
        }

        button:disabled {
            opacity: 0.5;
            cursor: not-allowed;
            transform: none !important;
        }

        .dashboard {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }

        .card {
            background: rgba(50, 50, 70, 0.8);
            border: 1px solid rgba(200, 200, 200, 0.1);
            border-radius: 12px;
            padding: 25px;
            backdrop-filter: blur(10px);
            transition: all 0.3s ease;
        }

        .card:hover {
            border-color: rgba(0, 180, 255, 0.3);
            background: rgba(50, 50, 70, 0.95);
        }

        .card h3 {
            font-size: 0.9em;
            color: #aaa;
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 15px;
        }

        .card-value {
            font-size: 2.5em;
            font-weight: bold;
            margin-bottom: 10px;
        }

        .status-dot {
            display: inline-block;
            width: 12px;
            height: 12px;
            border-radius: 50%;
            margin-right: 8px;
            animation: pulse 2s infinite;
        }

        .status-dot.active {
            background: #4ade80;
            box-shadow: 0 0 10px rgba(74, 222, 128, 0.5);
        }

        .status-dot.idle {
            background: #94a3b8;
        }

        .status-dot.warning {
            background: #fbbf24;
            animation: pulse-warning 1s infinite;
        }

        .status-dot.danger {
            background: #ff6b6b;
            animation: pulse-danger 0.5s infinite;
        }

        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.6; }
        }

        @keyframes pulse-warning {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.7; }
        }

        @keyframes pulse-danger {
            0%, 100% { opacity: 1; box-shadow: 0 0 10px rgba(255, 107, 107, 0.5); }
            50% { opacity: 0.8; box-shadow: 0 0 20px rgba(255, 107, 107, 0.7); }
        }

        .card-value.alert-high {
            color: #ff6b6b;
        }

        .card-value.alert-medium {
            color: #fbbf24;
        }

        .card-value.alert-low {
            color: #4ade80;
        }

        .bar-chart {
            width: 100%;
            height: 8px;
            background: rgba(200, 200, 200, 0.1);
            border-radius: 4px;
            overflow: hidden;
            margin-top: 10px;
        }

        .bar-fill {
            height: 100%;
            background: linear-gradient(90deg, #4ade80, #fbbf24, #ff6b6b);
            border-radius: 4px;
            transition: width 0.3s ease;
        }

        .badge {
            display: inline-block;
            padding: 6px 12px;
            border-radius: 6px;
            font-size: 0.85em;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-top: 10px;
        }

        .badge.face-detected { background: rgba(74, 222, 128, 0.2); color: #4ade80; }
        .badge.gaze-away { background: rgba(251, 191, 36, 0.2); color: #fbbf24; }
        .badge.head-turned { background: rgba(251, 191, 36, 0.2); color: #fbbf24; }
        .badge.speaking { background: rgba(255, 107, 107, 0.2); color: #ff6b6b; }

        .details-section {
            background: rgba(30, 30, 46, 0.8);
            border: 1px solid rgba(200, 200, 200, 0.1);
            border-radius: 12px;
            padding: 25px;
            margin-top: 30px;
        }

        .details-section h2 {
            font-size: 1.3em;
            margin-bottom: 20px;
            color: #00b4ff;
        }

        .details-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
        }

        .detail-item {
            background: rgba(50, 50, 70, 0.5);
            padding: 15px;
            border-radius: 8px;
            border-left: 3px solid #00b4ff;
        }

        .detail-label {
            font-size: 0.85em;
            color: #aaa;
            text-transform: uppercase;
            margin-bottom: 8px;
        }

        .detail-value {
            font-size: 1.3em;
            font-weight: bold;
            color: #e0e0e0;
        }

        .session-id {
            background: rgba(0, 180, 255, 0.1);
            padding: 12px;
            border-radius: 8px;
            font-family: monospace;
            font-size: 0.9em;
            color: #00b4ff;
            margin-top: 15px;
            word-break: break-all;
        }

        footer {
            text-align: center;
            margin-top: 50px;
            padding: 20px;
            color: #666;
            font-size: 0.9em;
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>🎓 AI Exam Proctor Service</h1>
            <p>Real-time Proctoring Dashboard</p>
            <div class="controls">
                <button class="btn-primary" onclick="startSession()">START SESSION</button>
                <button class="btn-danger" onclick="stopSession()">STOP SESSION</button>
                <button class="btn-secondary" onclick="takeScreenshot()">📸 SCREENSHOT</button>
                <button class="btn-secondary" onclick="resetCounters()">↻ RESET</button>
            </div>
        </header>

        <div class="dashboard">
            <div class="card">
                <h3>Session Status</h3>
                <div class="card-value">
                    <span class="status-dot idle" id="statusDot"></span>
                    <span id="statusText">IDLE</span>
                </div>
                <div id="sessionId" class="session-id" style="display:none;"></div>
            </div>

            <div class="card">
                <h3>Uptime</h3>
                <div class="card-value" id="uptime">00:00</div>
                <div style="color: #aaa; font-size: 0.9em;">Frames: <span id="frameCount">0</span></div>
            </div>

            <div class="card">
                <h3>Total Violations</h3>
                <div class="card-value" id="violations">0</div>
                <div class="bar-chart">
                    <div class="bar-fill" id="violationBar" style="width: 0%;"></div>
                </div>
            </div>

            <div class="card">
                <h3>Suspicious Score</h3>
                <div class="card-value" id="suspiciousScore">0.0</div>
                <div class="bar-chart">
                    <div class="bar-fill" id="scoreBar" style="width: 0%; background: linear-gradient(90deg, #4ade80, #fbbf24, #ff6b6b);"></div>
                </div>
            </div>

            <div class="card">
                <h3>Face Detection</h3>
                <div style="margin-top: 20px;">
                    <span class="badge face-detected" id="faceBadge" style="display: none;">✓ DETECTED</span>
                    <span style="color: #ff6b6b;" id="noFaceBadge" style="display: none;">✗ NOT DETECTED</span>
                </div>
            </div>

            <div class="card">
                <h3>Audio (RMS)</h3>
                <div class="card-value" id="audioRms">0</div>
                <div style="font-size: 0.9em; color: #aaa;">Range: 0 - 3000</div>
                <div class="bar-chart" style="margin-top: 10px;">
                    <div class="bar-fill" id="audioBar" style="width: 0%; background: #00d4ff;"></div>
                </div>
            </div>

            <div class="card">
                <h3>Gaze Status</h3>
                <div style="margin-top: 20px;">
                    <span class="badge gaze-away" id="gazeBadge" style="display: none;">↗ LOOKING AWAY</span>
                    <span style="color: #4ade80;" id="gazeOkBadge" style="display: none;">✓ FOCUSED</span>
                </div>
            </div>

            <div class="card">
                <h3>Head Pose</h3>
                <div style="margin-top: 20px;">
                    <span class="badge head-turned" id="headBadge" style="display: none;">⟲ HEAD TURNED</span>
                    <span style="color: #4ade80;" id="headOkBadge" style="display: none;">✓ NORMAL</span>
                </div>
            </div>

            <div class="card">
                <h3>Speaking Detection</h3>
                <div style="margin-top: 20px;">
                    <span class="badge speaking" id="speakingBadge" style="display: none;">🎤 SPEAKING</span>
                    <span style="color: #4ade80;" id="silentBadge" style="display: none;">✓ QUIET</span>
                </div>
            </div>
        </div>

        <div class="details-section">
            <h2>Detailed Statistics</h2>
            <div class="details-grid">
                <div class="detail-item">
                    <div class="detail-label">Session ID</div>
                    <div class="detail-value" id="detailSessionId" style="font-size: 0.9em; font-family: monospace;">—</div>
                </div>
                <div class="detail-item">
                    <div class="detail-label">Start Time</div>
                    <div class="detail-value" id="detailStartTime">—</div>
                </div>
                <div class="detail-item">
                    <div class="detail-label">Avg Suspicious Score</div>
                    <div class="detail-value" id="detailAvgScore">—</div>
                </div>
                <div class="detail-item">
                    <div class="detail-label">Max Suspicious Score</div>
                    <div class="detail-value" id="detailMaxScore">—</div>
                </div>
                <div class="detail-item">
                    <div class="detail-label">Total Blinks</div>
                    <div class="detail-value" id="detailBlinks">—</div>
                </div>
            </div>
        </div>

        <footer>
            <p>© 2025 AI Exam Proctor Service | Built with Flask & WebSocket</p>
        </footer>
    </div>

    <script>
        const socket = io();

        socket.on('connect', () => {
            console.log('Connected to server');
            socket.emit('request_state');
        });

        socket.on('state_update', (data) => {
            updateDashboard(data);
        });

        async function startSession() {
            try {
                const response = await fetch('/api/start', { method: 'POST' });
                const data = await response.json();
                if (!data.error) {
                    console.log('Session started:', data);
                    alert('✓ Session started: ' + data.session_id);
                } else {
                    alert('✗ Error: ' + data.error);
                }
            } catch (error) {
                console.error('Error:', error);
            }
        }

        async function stopSession() {
            if (!confirm('Are you sure you want to stop the session?')) return;
            try {
                const response = await fetch('/api/stop', { method: 'POST' });
                const data = await response.json();
                if (!data.error) {
                    console.log('Session stopped:', data);
                    alert('✓ Session stopped');
                } else {
                    alert('✗ Error: ' + data.error);
                }
            } catch (error) {
                console.error('Error:', error);
            }
        }

        async function takeScreenshot() {
            try {
                const response = await fetch('/api/action', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ action: 'screenshot' })
                });
                const data = await response.json();
                if (!data.error) {
                    alert('✓ Screenshot saved: ' + data.path);
                } else {
                    alert('✗ Error: ' + data.error);
                }
            } catch (error) {
                console.error('Error:', error);
            }
        }

        async function resetCounters() {
            if (!confirm('Reset all violation counters?')) return;
            try {
                const response = await fetch('/api/action', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ action: 'reset' })
                });
                const data = await response.json();
                if (!data.error) {
                    alert('✓ Counters reset');
                } else {
                    alert('✗ Error: ' + data.error);
                }
            } catch (error) {
                console.error('Error:', error);
            }
        }

        function updateDashboard(state) {
            // Status
            const statusElement = document.getElementById('statusText');
            const statusDot = document.getElementById('statusDot');
            const sessionIdDiv = document.getElementById('sessionId');

            if (state.status === 'running') {
                statusElement.textContent = 'ACTIVE';
                statusDot.className = 'status-dot active';
                if (state.session_id && !sessionIdDiv.textContent) {
                    sessionIdDiv.textContent = 'Session: ' + state.session_id;
                    sessionIdDiv.style.display = 'block';
                    document.getElementById('detailSessionId').textContent = state.session_id;
                    document.getElementById('detailStartTime').textContent = state.start_time;
                }
            } else if (state.status === 'idle') {
                statusElement.textContent = 'IDLE';
                statusDot.className = 'status-dot idle';
            }

            // Uptime
            const minutes = Math.floor(state.uptime_sec / 60);
            const seconds = Math.floor(state.uptime_sec % 60);
            document.getElementById('uptime').textContent = 
                `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
            document.getElementById('frameCount').textContent = state.frame_count;

            // Violations
            document.getElementById('violations').textContent = state.violations;
            const violationBar = document.getElementById('violationBar');
            violationBar.style.width = Math.min(state.violations * 5, 100) + '%';
            const violationValue = document.querySelector('[id="violations"]');
            if (state.violations > 5) {
                violationValue.className = 'card-value alert-high';
            } else if (state.violations > 0) {
                violationValue.className = 'card-value alert-medium';
            } else {
                violationValue.className = 'card-value alert-low';
            }

            // Suspicious Score
            document.getElementById('suspiciousScore').textContent = state.suspicious_score.toFixed(1);
            const scoreBar = document.getElementById('scoreBar');
            scoreBar.style.width = state.suspicious_score + '%';
            const scoreValue = document.querySelector('[id="suspiciousScore"]');
            if (state.suspicious_score > 60) {
                scoreValue.className = 'card-value alert-high';
            } else if (state.suspicious_score > 25) {
                scoreValue.className = 'card-value alert-medium';
            } else {
                scoreValue.className = 'card-value alert-low';
            }

            // Face Detection
            document.getElementById('faceBadge').style.display = state.face_detected ? 'inline-block' : 'none';
            document.getElementById('noFaceBadge').style.display = state.face_detected ? 'none' : 'inline-block';

            // Audio
            document.getElementById('audioRms').textContent = Math.round(state.audio_rms);
            const audioBar = document.getElementById('audioBar');
            audioBar.style.width = Math.min(state.audio_rms / 30, 100) + '%';

            // Gaze
            document.getElementById('gazeBadge').style.display = state.gaze_away ? 'inline-block' : 'none';
            document.getElementById('gazeOkBadge').style.display = state.gaze_away ? 'none' : 'inline-block';

            // Head
            document.getElementById('headBadge').style.display = state.head_turned ? 'inline-block' : 'none';
            document.getElementById('headOkBadge').style.display = state.head_turned ? 'none' : 'inline-block';

            // Speaking
            document.getElementById('speakingBadge').style.display = state.mouth_open ? 'inline-block' : 'none';
            document.getElementById('silentBadge').style.display = state.mouth_open ? 'none' : 'inline-block';
        }

        // Request state periodically
        setInterval(async () => {
            if (socket.connected) {
                socket.emit('request_state');
            }
        }, 1000);
    </script>
</body>
</html>
"""


# ─────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print("=" * 64)
    print("  AI EXAM PROCTORING SERVICE — Flask API")
    print("=" * 64)
    print()
    print("  Dashboard: http://localhost:8765")
    print()
    print("  REST API Endpoints:")
    print("    GET  /api/status    — Current status")
    print("    POST /api/start     — Start session")
    print("    POST /api/stop      — Stop session")
    print("    GET  /api/stats     — Session statistics")
    print("    POST /api/action    — Control actions")
    print()
    print("=" * 64)

    socketio.run(app, host='0.0.0.0', port=8765, debug=False)
