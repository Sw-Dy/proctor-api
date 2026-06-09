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
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, jsonify, request, render_template_string
from flask_cors import CORS
from flask_socketio import SocketIO, emit
from werkzeug.security import check_password_hash, generate_password_hash
import jwt
import psycopg2
from psycopg2.extras import DictCursor, Json
from dotenv import load_dotenv

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
app.config['SECRET_KEY'] = os.environ.get('JWT_SECRET', 'proctor-secret-key-change-in-production')
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")
JWT_ALGORITHM = "HS256"

load_dotenv()

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/protraq"
)

JWT_EXP_HOURS = int(os.environ.get("JWT_EXP_HOURS", "8"))

print("Current dir:", os.getcwd())
print("ENV DATABASE_URL:", os.getenv("DATABASE_URL"))
print("Using DATABASE_URL:", DATABASE_URL) 

DEFAULT_USERS = (
    ("user1", "User1@123", "User 1"),
    ("user2", "User2@123", "User 2"),
    ("user3", "User3@123", "User 3"),
)


class Database:
    """Small Postgres gateway for users and per-user proctor metrics."""

    def __init__(self, database_url: str):
        self.database_url = database_url
        self.available = False

    def connect(self):
        return psycopg2.connect(self.database_url, cursor_factory=DictCursor)

    def initialize(self):
        try:
            with self.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS users (
                            id SERIAL PRIMARY KEY,
                            username TEXT UNIQUE NOT NULL,
                            password_hash TEXT NOT NULL,
                            display_name TEXT NOT NULL,
                            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        );
                    """)
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS proctor_sessions (
                            id SERIAL PRIMARY KEY,
                            session_id TEXT UNIQUE NOT NULL,
                            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                            status TEXT NOT NULL DEFAULT 'running',
                            start_time TIMESTAMPTZ,
                            end_time TIMESTAMPTZ,
                            uptime_sec DOUBLE PRECISION NOT NULL DEFAULT 0,
                            frame_count INTEGER NOT NULL DEFAULT 0,
                            total_violations INTEGER NOT NULL DEFAULT 0,
                            suspicious_score DOUBLE PRECISION NOT NULL DEFAULT 0,
                            avg_score DOUBLE PRECISION NOT NULL DEFAULT 0,
                            max_score DOUBLE PRECISION NOT NULL DEFAULT 0,
                            total_blinks INTEGER NOT NULL DEFAULT 0,
                            flags INTEGER NOT NULL DEFAULT 0,
                            screenshot_count INTEGER NOT NULL DEFAULT 0,
                            face_detected BOOLEAN NOT NULL DEFAULT FALSE,
                            gaze_away BOOLEAN NOT NULL DEFAULT FALSE,
                            gaze_focus BOOLEAN NOT NULL DEFAULT TRUE,
                            head_turned BOOLEAN NOT NULL DEFAULT FALSE,
                            mouth_open BOOLEAN NOT NULL DEFAULT FALSE,
                            audio_rms DOUBLE PRECISION NOT NULL DEFAULT 0,
                            violation_counts JSONB NOT NULL DEFAULT '{}'::jsonb,
                            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        );
                    """)
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS session_screenshots (
                            id SERIAL PRIMARY KEY,
                            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                            session_id TEXT NOT NULL,
                            path TEXT NOT NULL,
                            reason TEXT,
                            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        );
                    """)
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS session_logs (
                            id SERIAL PRIMARY KEY,
                            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                            session_id TEXT,
                            event_type TEXT NOT NULL,
                            message TEXT NOT NULL,
                            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        );
                    """)
                    for username, password, display_name in DEFAULT_USERS:
                        cur.execute("""
                            INSERT INTO users (username, password_hash, display_name)
                            VALUES (%s, %s, %s)
                            ON CONFLICT (username) DO NOTHING;
                        """, (username, generate_password_hash(password), display_name))
            self.available = True
            print("[Database] Postgres initialized.")
        except Exception as e:
            self.available = False
            print(f"[Database] Postgres unavailable: {e}")

    def get_user_by_username(self, username: str):
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE username = %s;", (username,))
            return cur.fetchone()

    def get_user_by_id(self, user_id: int):
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT id, username, display_name FROM users WHERE id = %s;", (user_id,))
            return cur.fetchone()

    def create_session(self, user, session_id: str, start_time: str):
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO proctor_sessions (session_id, user_id, status, start_time)
                VALUES (%s, %s, 'running', %s)
                ON CONFLICT (session_id) DO UPDATE
                    SET user_id = EXCLUDED.user_id,
                        status = 'running',
                        start_time = EXCLUDED.start_time,
                        end_time = NULL,
                        updated_at = NOW();
            """, (session_id, user["id"], start_time))

    def update_session(self, user_id: int, state: dict, status: str = None, end_time: str = None):
        if not state.get("session_id"):
            return
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute("""
                UPDATE proctor_sessions
                   SET status = COALESCE(%s, status),
                       end_time = COALESCE(%s, end_time),
                       uptime_sec = %s,
                       frame_count = %s,
                       total_violations = %s,
                       suspicious_score = %s,
                       avg_score = %s,
                       max_score = %s,
                       total_blinks = %s,
                       flags = %s,
                       screenshot_count = %s,
                       face_detected = %s,
                       gaze_away = %s,
                       gaze_focus = %s,
                       head_turned = %s,
                       mouth_open = %s,
                       audio_rms = %s,
                       violation_counts = %s,
                       updated_at = NOW()
                 WHERE session_id = %s AND user_id = %s;
            """, (
                status,
                end_time,
                state.get("uptime_sec", 0),
                state.get("frame_count", 0),
                state.get("violations", 0),
                state.get("suspicious_score", 0),
                state.get("avg_score", 0),
                state.get("max_score", 0),
                state.get("total_blinks", 0),
                state.get("flags", state.get("violations", 0)),
                state.get("screenshot_count", 0),
                state.get("face_detected", False),
                state.get("gaze_away", False),
                state.get("gaze_focus", not state.get("gaze_away", False)),
                state.get("head_turned", False),
                state.get("mouth_open", False),
                state.get("audio_rms", 0),
                Json(state.get("violation_counts", {})),
                state["session_id"],
                user_id,
            ))

    def record_screenshot(self, user_id: int, session_id: str, path: str, reason: str):
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO session_screenshots (user_id, session_id, path, reason)
                VALUES (%s, %s, %s, %s);
            """, (user_id, session_id, path, reason))
            cur.execute("""
                UPDATE proctor_sessions
                   SET screenshot_count = screenshot_count + 1,
                       updated_at = NOW()
                 WHERE session_id = %s AND user_id = %s;
            """, (session_id, user_id))

    def record_log(self, user_id: int, session_id: str, event_type: str, message: str, payload=None):
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO session_logs (user_id, session_id, event_type, message, payload)
                VALUES (%s, %s, %s, %s, %s);
            """, (user_id, session_id, event_type, message, Json(payload or {})))

    def get_user_summary(self, user_id: int):
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*)::int AS session_count,
                    COALESCE(SUM(total_violations), 0)::int AS total_violations,
                    COALESCE(SUM(flags), 0)::int AS total_flags,
                    COALESCE(SUM(screenshot_count), 0)::int AS total_screenshots,
                    COALESCE(MAX(max_score), 0) AS highest_score,
                    COALESCE(AVG(avg_score), 0) AS average_score
                FROM proctor_sessions
                WHERE user_id = %s;
            """, (user_id,))
            totals = dict(cur.fetchone())
            cur.execute("""
                SELECT session_id, status, start_time, end_time, uptime_sec,
                       total_violations, suspicious_score, avg_score, max_score,
                       flags, screenshot_count, gaze_focus, violation_counts
                  FROM proctor_sessions
                 WHERE user_id = %s
                 ORDER BY updated_at DESC
                 LIMIT 5;
            """, (user_id,))
            sessions = [dict(row) for row in cur.fetchall()]
            cur.execute("""
                SELECT session_id, path, reason, created_at
                  FROM session_screenshots
                 WHERE user_id = %s
                 ORDER BY created_at DESC
                 LIMIT 10;
            """, (user_id,))
            screenshots = [dict(row) for row in cur.fetchall()]
            cur.execute("""
                SELECT session_id, event_type, message, payload, created_at
                  FROM session_logs
                 WHERE user_id = %s
                 ORDER BY created_at DESC
                 LIMIT 20;
            """, (user_id,))
            logs = [dict(row) for row in cur.fetchall()]
            return {"totals": totals, "recent_sessions": sessions, "screenshots": screenshots, "logs": logs}


db = Database(DATABASE_URL)
db.initialize()


def serialize_db(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [serialize_db(item) for item in value]
    if isinstance(value, dict):
        return {key: serialize_db(item) for key, item in value.items()}
    return value


def create_token(user) -> str:
    payload = {
        "sub": str(user["id"]),
        "username": user["username"],
        "exp": datetime.utcnow() + timedelta(hours=JWT_EXP_HOURS),
    }
    return jwt.encode(payload, app.config["SECRET_KEY"], algorithm=JWT_ALGORITHM)


def authenticate_token(token: str):
    if not token:
        return None
    try:
        payload = jwt.decode(token, app.config["SECRET_KEY"], algorithms=[JWT_ALGORITHM])
        return db.get_user_by_id(int(payload["sub"]))
    except Exception:
        return None


def current_user_from_request():
    auth_header = request.headers.get("Authorization", "")
    token = auth_header.replace("Bearer ", "", 1).strip() if auth_header.startswith("Bearer ") else None
    return authenticate_token(token)


def require_auth(handler):
    @wraps(handler)
    def wrapper(*args, **kwargs):
        user = current_user_from_request()
        if not user:
            return jsonify({"error": "Authentication required"}), 401
        request.current_user = user
        return handler(*args, **kwargs)
    return wrapper

# ─────────────────────────────────────────────────────────────────
#  GLOBAL STATE MANAGEMENT
# ─────────────────────────────────────────────────────────────────

class ProctorServiceManager:
    """Manages the ProctorSystem as a background service."""
    
    def __init__(self):
        self.proctor_system: ProctorSystem = None
        self.system_thread: threading.Thread = None
        self._running = False
        self._state = self._blank_state()
        self._state_lock = threading.Lock()
        self._stats_history = queue.Queue(maxsize=100)
        self._last_db_update = 0.0

    def _blank_state(self):
        return {
            "status": "idle",          # idle, running, stopped
            "user_id": None,
            "username": None,
            "display_name": None,
            "session_id": None,
            "start_time": None,
            "uptime_sec": 0.0,
            "frame_count": 0,
            "violations": 0,
            "flags": 0,
            "suspicious_score": 0.0,
            "avg_score": 0.0,
            "max_score": 0.0,
            "total_blinks": 0,
            "screenshot_count": 0,
            "last_screenshot_path": None,
            "violation_counts": {},
            "face_detected": False,
            "gaze_away": False,
            "gaze_focus": True,
            "head_turned": False,
            "mouth_open": False,
            "audio_rms": 0.0,
        }

    def start_session(self, user):
        """Start a new proctoring session in background thread."""
        if self._running:
            return {"error": "Session already running"}
        if not db.available:
            return {"error": "Postgres is not available. Check DATABASE_URL and database status."}

        self._running = True
        self.proctor_system = ProctorSystem()
        self._attach_logger_callbacks(user)

        start_time = datetime.now().isoformat()
        with self._state_lock:
            self._state = self._blank_state()
            self._state["status"] = "running"
            self._state["user_id"] = int(user["id"])
            self._state["username"] = user["username"]
            self._state["display_name"] = user["display_name"]
            self._state["session_id"] = self.proctor_system.session_id
            self._state["start_time"] = start_time
        db.create_session(user, self.proctor_system.session_id, start_time)
        db.record_log(user["id"], self.proctor_system.session_id, "session_started", "Session started")
        
        self.system_thread = threading.Thread(
            target=self._run_system_loop, daemon=True
        )
        self.system_thread.start()
        
        return {
            "status": "started",
            "session_id": self.proctor_system.session_id,
            "user": {"id": int(user["id"]), "username": user["username"], "display_name": user["display_name"]},
            "timestamp": datetime.now().isoformat()
        }

    def _attach_logger_callbacks(self, user):
        def record_event(event):
            payload = {
                "severity": event.severity,
                "details": event.details,
                "screenshot_path": event.screenshot_path,
            }
            db.record_log(user["id"], self.proctor_system.session_id, event.violation_type, event.details, payload)

        def record_screenshot(path, reason):
            db.record_screenshot(user["id"], self.proctor_system.session_id, path, reason)
            with self._state_lock:
                self._state["screenshot_count"] += 1
                self._state["last_screenshot_path"] = path

        self.proctor_system.logger.event_callback = record_event
        self.proctor_system.logger.screenshot_callback = record_screenshot

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
                        self._state["flags"] = self.proctor_system._total_violations
                        self._state["suspicious_score"] = self.proctor_system.detector.suspicious_score
                        self._state["avg_score"] = round(self.proctor_system.detector.avg_score, 2)
                        self._state["max_score"] = round(self.proctor_system.detector.max_score, 2)
                        self._state["total_blinks"] = self.proctor_system.eye_tracker.blink_count
                        self._state["violation_counts"] = dict(self.proctor_system._violation_counts)
                        self._state["face_detected"] = face_data.detected
                        self._state["gaze_away"] = gaze_data.looking_away
                        self._state["gaze_focus"] = not gaze_data.looking_away
                        self._state["head_turned"] = head_data.is_turned
                        self._state["mouth_open"] = mouth_data.is_open
                        self._state["audio_rms"] = audio_data.rms
                    snapshot = self._get_state()

                    if snapshot.get("user_id") and now - self._last_db_update > 2.0:
                        try:
                            db.update_session(snapshot["user_id"], snapshot)
                        except Exception as e:
                            print(f"[Database] Session update failed: {e}")
                        self._last_db_update = now

                    # Emit update every 100ms
                    if frame_count % 3 == 0:
                        socketio.emit('state_update', self._get_state())

                time.sleep(0.005)

        except Exception as e:
            print(f"[Service] Error in system loop: {e}")
            self._running = False
        finally:
            self._shutdown_system()

    def stop_session(self, user):
        """Stop the current proctoring session."""
        if not self._running:
            return {"error": "No session running"}
        if not self._session_belongs_to(user):
            return {"error": "Session belongs to another user"}

        self._running = False
        if self.system_thread:
            self.system_thread.join(timeout=5.0)

        snapshot = self._get_state()
        db.record_log(user["id"], snapshot["session_id"], "session_stopped", "Session stopped")
        db.update_session(user["id"], snapshot, status="stopped", end_time=datetime.now().isoformat())

        return {
            "status": "stopped",
            "session_id": snapshot["session_id"],
            "total_violations": snapshot["violations"],
            "timestamp": datetime.now().isoformat()
        }

    def _shutdown_system(self):
        """Clean up system resources."""
        if self.proctor_system:
            self.proctor_system._shutdown()
        with self._state_lock:
            self._state["status"] = "stopped"
            snapshot = self._state.copy()
        if snapshot.get("user_id"):
            db.update_session(snapshot["user_id"], snapshot, status="stopped", end_time=datetime.now().isoformat())
        print("[Service] Session stopped")

    def perform_action(self, user, action: str):
        """Perform a control action."""
        if not self._running or not self.proctor_system:
            return {"error": "No session running"}
        if not self._session_belongs_to(user):
            return {"error": "Session belongs to another user"}

        if action == "screenshot":
            if self.proctor_system._last_frame is not None:
                path = self.proctor_system.logger.save_screenshot(
                    self.proctor_system._last_frame, "API_SCREENSHOT")
                db.record_log(user["id"], self._state["session_id"], "manual_screenshot", "Manual screenshot captured", {"path": path})
                return {"action": "screenshot", "path": path}
            return {"error": "No frame available"}

        elif action == "reset":
            self.proctor_system.detector.reset_counters()
            self.proctor_system._total_violations = 0
            self.proctor_system._violation_counts = {}
            with self._state_lock:
                self._state["violations"] = 0
                self._state["flags"] = 0
                self._state["violation_counts"] = {}
                snapshot = self._state.copy()
            db.record_log(user["id"], snapshot["session_id"], "reset", "Violation counters reset")
            db.update_session(user["id"], snapshot)
            return {"action": "reset", "status": "counters cleared"}

        return {"error": f"Unknown action: {action}"}

    def _session_belongs_to(self, user) -> bool:
        with self._state_lock:
            return self._state.get("user_id") == int(user["id"])

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

    def get_state_for_user(self, user) -> dict:
        state = self._get_state()
        if state.get("user_id") and state["user_id"] != int(user["id"]):
            return {
                "status": "busy",
                "message": "Another user has an active proctoring session",
                "active_user": state.get("display_name") or state.get("username"),
            }
        return state

    def get_session_stats(self, user) -> dict:
        """Get full session statistics."""
        if not self.proctor_system:
            return {"error": "No session running"}
        if not self._session_belongs_to(user):
            return {"error": "Session belongs to another user"}

        with self._state_lock:
            return {
                "user_id": self._state["user_id"],
                "username": self._state["username"],
                "session_id": self._state["session_id"],
                "start_time": self._state["start_time"],
                "uptime_sec": self._state["uptime_sec"],
                "frame_count": self._state["frame_count"],
                "total_violations": self._state["violations"],
                "flags": self._state["flags"],
                "suspicious_score": self._state["suspicious_score"],
                "avg_score": self._state["avg_score"],
                "max_score": self._state["max_score"],
                "total_blinks": self._state["total_blinks"],
                "screenshot_count": self._state["screenshot_count"],
                "gaze_focus": self._state["gaze_focus"],
                "violation_counts": self.proctor_system._violation_counts,
            }


# Global service manager
service_manager = ProctorServiceManager()


# ─────────────────────────────────────────────────────────────────
#  REST API ENDPOINTS
# ─────────────────────────────────────────────────────────────────

@app.route('/api/auth/login', methods=['POST'])
def api_login():
    """Authenticate one of the seeded users and return a JWT."""
    if not db.available:
        return jsonify({"error": "Postgres is not available"}), 503

    data = request.get_json() or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        return jsonify({"error": "Username and password are required"}), 400

    user = db.get_user_by_username(username)
    if not user or not check_password_hash(user["password_hash"], password):
        return jsonify({"error": "Invalid username or password"}), 401

    return jsonify({
        "token": create_token(user),
        "expires_in_hours": JWT_EXP_HOURS,
        "user": {
            "id": int(user["id"]),
            "username": user["username"],
            "display_name": user["display_name"],
        },
    })


@app.route('/api/auth/me', methods=['GET'])
@require_auth
def api_me():
    """Return the signed-in user."""
    user = request.current_user
    return jsonify({
        "user": {
            "id": int(user["id"]),
            "username": user["username"],
            "display_name": user["display_name"],
        },
    })


@app.route('/api/user/summary', methods=['GET'])
@require_auth
def api_user_summary():
    """Return persisted metrics, screenshots, and logs for the signed-in user."""
    try:
        summary = db.get_user_summary(request.current_user["id"])
        return jsonify(serialize_db(summary))
    except Exception as e:
        return jsonify({"error": f"Could not load user summary: {e}"}), 500


@app.route('/api/status', methods=['GET'])
@require_auth
def api_status():
    """Get current system status."""
    return jsonify(service_manager.get_state_for_user(request.current_user))


@app.route('/api/start', methods=['POST'])
@require_auth
def api_start():
    """Start a new proctoring session."""
    result = service_manager.start_session(request.current_user)
    return jsonify(result), 200 if "error" not in result else 400


@app.route('/api/stop', methods=['POST'])
@require_auth
def api_stop():
    """Stop the current proctoring session."""
    result = service_manager.stop_session(request.current_user)
    return jsonify(result), 200 if "error" not in result else 400


@app.route('/api/stats', methods=['GET'])
@require_auth
def api_stats():
    """Get detailed session statistics."""
    result = service_manager.get_session_stats(request.current_user)
    return jsonify(result), 200 if "error" not in result else 400


@app.route('/api/action', methods=['POST'])
@require_auth
def api_action():
    """Perform a control action (screenshot, reset)."""
    data = request.get_json()
    action = data.get('action') if data else None
    
    if not action:
        return jsonify({"error": "Missing 'action' field"}), 400

    result = service_manager.perform_action(request.current_user, action)
    return jsonify(result), 200 if "error" not in result else 400


@app.route('/api/config', methods=['GET'])
@require_auth
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
def on_connect(auth=None):
    """Handle WebSocket connection."""
    token = auth.get("token") if isinstance(auth, dict) else None
    user = authenticate_token(token)
    if not user:
        return False
    print("[WebSocket] Client connected")
    emit('connect_response', {'status': 'connected', 'user_id': int(user["id"])})


@socketio.on('disconnect')
def on_disconnect():
    """Handle WebSocket disconnection."""
    print("[WebSocket] Client disconnected")


@socketio.on('request_state')
def on_request_state(data=None):
    """Client requests current state."""
    token = data.get("token") if isinstance(data, dict) else None
    user = authenticate_token(token)
    if not user:
        emit('auth_error', {'error': 'Authentication required'})
        return
    emit('state_update', service_manager.get_state_for_user(user))


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

        .login-panel {
            max-width: 420px;
            margin: 10vh auto;
            background: rgba(30, 30, 46, 0.9);
            border: 1px solid rgba(0, 180, 255, 0.25);
            border-radius: 8px;
            padding: 28px;
        }

        .login-panel h1 {
            font-size: 2em;
            margin-bottom: 8px;
        }

        .login-panel p {
            color: #aaa;
            margin-bottom: 22px;
        }

        .form-field {
            display: grid;
            gap: 8px;
            margin-bottom: 16px;
        }

        .form-field label {
            color: #aaa;
            font-size: 0.85em;
            text-transform: uppercase;
        }

        .form-field input {
            width: 100%;
            border: 1px solid rgba(200, 200, 200, 0.2);
            border-radius: 8px;
            padding: 12px 14px;
            background: rgba(255, 255, 255, 0.08);
            color: #e0e0e0;
            font-size: 16px;
        }

        .login-error {
            min-height: 22px;
            color: #ff6b6b;
            margin-top: 12px;
        }

        .user-bar {
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 12px;
            margin-top: 16px;
            color: #aaa;
            flex-wrap: wrap;
        }

        .summary-list {
            display: grid;
            gap: 10px;
            margin-top: 12px;
        }

        .summary-row {
            display: grid;
            grid-template-columns: 160px 1fr;
            gap: 12px;
            padding: 10px 0;
            border-bottom: 1px solid rgba(200, 200, 200, 0.08);
        }

        .summary-row:last-child {
            border-bottom: none;
        }

        .summary-meta {
            color: #aaa;
            font-size: 0.9em;
        }
    </style>
</head>
<body>
    <div class="login-panel" id="loginPanel">
        <h1>AI Exam Proctor</h1>
        <p>Sign in to view and store your proctoring session data.</p>
        <form id="loginForm">
            <div class="form-field">
                <label for="username">Username</label>
                <input id="username" autocomplete="username" value="user1">
            </div>
            <div class="form-field">
                <label for="password">Password</label>
                <input id="password" type="password" autocomplete="current-password" value="User1@123">
            </div>
            <button class="btn-primary" type="submit" style="width:100%;">SIGN IN</button>
            <div class="login-error" id="loginError"></div>
        </form>
    </div>

    <div class="container" id="dashboardContainer" style="display:none;">
        <header>
            <h1>🎓 AI Exam Proctor Service</h1>
            <p>Real-time Proctoring Dashboard</p>
            <div class="user-bar">
                <span id="userLabel"></span>
                <button class="btn-secondary" onclick="logout()">SIGN OUT</button>
            </div>
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
                <div class="detail-item">
                    <div class="detail-label">Flags</div>
                    <div class="detail-value" id="detailFlags">0</div>
                </div>
                <div class="detail-item">
                    <div class="detail-label">Screenshots</div>
                    <div class="detail-value" id="detailScreenshots">0</div>
                </div>
            </div>
        </div>

        <div class="details-section">
            <h2>Stored User Data</h2>
            <div class="details-grid">
                <div class="detail-item">
                    <div class="detail-label">Saved Sessions</div>
                    <div class="detail-value" id="savedSessions">0</div>
                </div>
                <div class="detail-item">
                    <div class="detail-label">Saved Violations</div>
                    <div class="detail-value" id="savedViolations">0</div>
                </div>
                <div class="detail-item">
                    <div class="detail-label">Saved Flags</div>
                    <div class="detail-value" id="savedFlags">0</div>
                </div>
                <div class="detail-item">
                    <div class="detail-label">Saved Screenshots</div>
                    <div class="detail-value" id="savedScreenshots">0</div>
                </div>
            </div>
            <div class="summary-list" id="sessionLogList"></div>
        </div>

        <footer>
            <p>© 2025 AI Exam Proctor Service | Built with Flask & WebSocket</p>
        </footer>
    </div>

    <script>
        let socket = null;
        let authToken = localStorage.getItem('proctorToken');
        let currentUser = JSON.parse(localStorage.getItem('proctorUser') || 'null');

        document.getElementById('loginForm').addEventListener('submit', async (event) => {
            event.preventDefault();
            document.getElementById('loginError').textContent = '';
            try {
                const response = await fetch('/api/auth/login', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        username: document.getElementById('username').value,
                        password: document.getElementById('password').value
                    })
                });
                const data = await response.json();
                if (!response.ok) {
                    document.getElementById('loginError').textContent = data.error || 'Sign in failed';
                    return;
                }
                authToken = data.token;
                currentUser = data.user;
                localStorage.setItem('proctorToken', authToken);
                localStorage.setItem('proctorUser', JSON.stringify(currentUser));
                showDashboard();
            } catch (error) {
                document.getElementById('loginError').textContent = error.message;
            }
        });

        async function apiFetch(url, options = {}) {
            const headers = {
                ...(options.headers || {}),
                Authorization: 'Bearer ' + authToken
            };
            const response = await fetch(url, { ...options, headers });
            if (response.status === 401) {
                logout();
                throw new Error('Sign in again');
            }
            return response;
        }

        async function showDashboard() {
            document.getElementById('loginPanel').style.display = 'none';
            document.getElementById('dashboardContainer').style.display = 'block';
            document.getElementById('userLabel').textContent =
                `Signed in as ${currentUser.display_name} (${currentUser.username})`;
            connectSocket();
            await refreshStatus();
            await loadUserSummary();
        }

        function showLogin() {
            document.getElementById('loginPanel').style.display = 'block';
            document.getElementById('dashboardContainer').style.display = 'none';
        }

        function logout() {
            localStorage.removeItem('proctorToken');
            localStorage.removeItem('proctorUser');
            authToken = null;
            currentUser = null;
            if (socket) socket.disconnect();
            showLogin();
        }

        function connectSocket() {
            if (socket) socket.disconnect();
            socket = io({ auth: { token: authToken } });

            socket.on('connect', () => {
                console.log('Connected to server');
                socket.emit('request_state', { token: authToken });
            });

            socket.on('state_update', (data) => {
                updateDashboard(data);
            });

            socket.on('auth_error', () => logout());
        }

        async function loadUserSummary() {
            try {
                const response = await apiFetch('/api/user/summary');
                const data = await response.json();
                if (data.error) return;
                const totals = data.totals || {};
                document.getElementById('savedSessions').textContent = totals.session_count || 0;
                document.getElementById('savedViolations').textContent = totals.total_violations || 0;
                document.getElementById('savedFlags').textContent = totals.total_flags || 0;
                document.getElementById('savedScreenshots').textContent = totals.total_screenshots || 0;

                const logList = document.getElementById('sessionLogList');
                logList.innerHTML = '';
                (data.logs || []).slice(0, 6).forEach((item) => {
                    const row = document.createElement('div');
                    row.className = 'summary-row';
                    row.innerHTML = `
                        <div class="summary-meta">${new Date(item.created_at).toLocaleString()}</div>
                        <div>${item.event_type}: ${item.message}</div>
                    `;
                    logList.appendChild(row);
                });
            } catch (error) {
                console.error('Summary error:', error);
            }
        }

        async function startSession() {
            try {
                const response = await apiFetch('/api/start', { method: 'POST' });
                const data = await response.json();
                if (!data.error) {
                    console.log('Session started:', data);
                    alert('Session started: ' + data.session_id);
                    loadUserSummary();
                } else {
                    alert('Error: ' + data.error);
                }
            } catch (error) {
                console.error('Error:', error);
            }
        }

        async function stopSession() {
            if (!confirm('Are you sure you want to stop the session?')) return;
            try {
                const response = await apiFetch('/api/stop', { method: 'POST' });
                const data = await response.json();
                if (!data.error) {
                    console.log('Session stopped:', data);
                    alert('Session stopped');
                    loadUserSummary();
                } else {
                    alert('Error: ' + data.error);
                }
            } catch (error) {
                console.error('Error:', error);
            }
        }

        async function takeScreenshot() {
            try {
                const response = await apiFetch('/api/action', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ action: 'screenshot' })
                });
                const data = await response.json();
                if (!data.error) {
                    alert('Screenshot saved: ' + data.path);
                    loadUserSummary();
                } else {
                    alert('Error: ' + data.error);
                }
            } catch (error) {
                console.error('Error:', error);
            }
        }

        async function resetCounters() {
            if (!confirm('Reset all violation counters?')) return;
            try {
                const response = await apiFetch('/api/action', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ action: 'reset' })
                });
                const data = await response.json();
                if (!data.error) {
                    alert('Counters reset');
                    loadUserSummary();
                } else {
                    alert('Error: ' + data.error);
                }
            } catch (error) {
                console.error('Error:', error);
            }
        }

        async function refreshStatus() {
            try {
                const response = await apiFetch('/api/status');
                const data = await response.json();
                updateDashboard(data);
            } catch (error) {
                console.error('Status error:', error);
            }
        }

        function updateDashboard(state) {
            // Status
            const statusElement = document.getElementById('statusText');
            const statusDot = document.getElementById('statusDot');
            const sessionIdDiv = document.getElementById('sessionId');

            if (state.status === 'busy') {
                statusElement.textContent = 'BUSY';
                statusDot.className = 'status-dot warning';
                sessionIdDiv.textContent = state.message || 'Another user has an active session';
                sessionIdDiv.style.display = 'block';
                return;
            }

            if (state.status === 'running') {
                statusElement.textContent = 'ACTIVE';
                statusDot.className = 'status-dot active';
                if (state.session_id) {
                    sessionIdDiv.textContent = 'Session: ' + state.session_id;
                    sessionIdDiv.style.display = 'block';
                    document.getElementById('detailSessionId').textContent = state.session_id;
                    document.getElementById('detailStartTime').textContent = state.start_time;
                }
            } else if (state.status === 'idle') {
                statusElement.textContent = 'IDLE';
                statusDot.className = 'status-dot idle';
            } else if (state.status === 'stopped') {
                statusElement.textContent = 'STOPPED';
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
            document.getElementById('detailFlags').textContent = state.flags || state.violations || 0;
            document.getElementById('detailScreenshots').textContent = state.screenshot_count || 0;
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
            document.getElementById('detailAvgScore').textContent =
                state.avg_score !== undefined ? Number(state.avg_score).toFixed(1) : '—';
            document.getElementById('detailMaxScore').textContent =
                state.max_score !== undefined ? Number(state.max_score).toFixed(1) : '—';
            document.getElementById('detailBlinks').textContent = state.total_blinks || 0;
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
            if (socket && socket.connected && authToken) {
                socket.emit('request_state', { token: authToken });
            }
        }, 1000);

        setInterval(async () => {
            if (authToken) {
                loadUserSummary();
            }
        }, 5000);

        (async function initAuth() {
            if (!authToken || !currentUser) {
                showLogin();
                return;
            }
            try {
                const response = await apiFetch('/api/auth/me');
                const data = await response.json();
                currentUser = data.user;
                localStorage.setItem('proctorUser', JSON.stringify(currentUser));
                showDashboard();
            } catch (error) {
                logout();
            }
        })();
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
    print("    POST /api/auth/login   - Sign in and receive JWT")
    print("    GET  /api/user/summary - Persisted user metrics")
    print("    GET  /api/status    — Current status")
    print("    POST /api/start     — Start session")
    print("    POST /api/stop      — Stop session")
    print("    GET  /api/stats     — Session statistics")
    print("    POST /api/action    — Control actions")
    print()
    print("=" * 64)

    socketio.run(app, host='0.0.0.0', port=8765, debug=False)
