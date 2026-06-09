"""
╔══════════════════════════════════════════════════════════════════╗
║           AI EXAM PROCTORING SYSTEM — Production Grade           ║
║           Author: Built with Claude (Anthropic)                  ║
║           Stack:  Python · OpenCV · MediaPipe · NumPy            ║
╚══════════════════════════════════════════════════════════════════╝

Installation:
    pip install opencv-python mediapipe numpy pyaudio

Run:
    python exam_proctor.py

Controls:
    Q  — Quit and save session report
    S  — Manual screenshot
    R  — Reset violation counters
"""

import cv2
import mediapipe as mp
import numpy as np
import json
import csv
import time
import os
import threading
import queue
from datetime import datetime
from collections import deque
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Tuple, Dict

# ─────────────────────────────────────────────────────────────────
#  CONSTANTS & CONFIGURATION
# ─────────────────────────────────────────────────────────────────

CONFIG = {
    # Camera
    "CAMERA_INDEX": 0,
    "TARGET_FPS": 30,
    "FRAME_WIDTH": 1280,
    "FRAME_HEIGHT": 720,
    "PROCESS_EVERY_N_FRAMES": 2,

    # Detection thresholds
    "GAZE_AWAY_THRESHOLD_SEC": 2.0,
    "NO_FACE_THRESHOLD_SEC": 3.0,
    "SPEAKING_THRESHOLD_SEC": 2.0,
    "HEAD_MOVE_THRESHOLD_SEC": 4.0,       # ↑ slightly longer window
    "MULTIPLE_FACE_THRESHOLD_SEC": 1.0,

    # Head pose thresholds (degrees) — RELAXED from 25/20 → 35/30
    # plus a hysteresis band: once triggered, must drop below CLEAR to reset
    "YAW_THRESHOLD":        45,           # was 35 → relaxed further
    "YAW_THRESHOLD_CLEAR":  28,           # hysteresis: reset when yaw < this
    "PITCH_THRESHOLD":      38,           # was 30 → relaxed further
    "PITCH_THRESHOLD_CLEAR":24,           # hysteresis: reset when pitch < this

    # Mouth aspect ratio
    "MAR_THRESHOLD": 0.04,

    # Blink detection
    "EAR_THRESHOLD": 0.20,
    "BLINK_CONSEC_FRAMES": 2,

    # Suspicious score weights
    "SCORE_GAZE_AWAY": 30,
    "SCORE_NO_FACE": 40,
    "SCORE_MULTIPLE_FACE": 50,
    "SCORE_SPEAKING": 20,
    "SCORE_HEAD_MOVE": 15,
    "SCORE_AUDIO_NOISE": 25,

    # ── Audio Detection ───────────────────────────────────────────
    "AUDIO_ENABLED": True,               # set False to disable entirely
    "AUDIO_SAMPLE_RATE": 16000,
    "AUDIO_CHUNK_SIZE": 1024,
    "AUDIO_CHANNELS": 1,
    # RMS energy thresholds (0–32768 range for int16)
    "AUDIO_NOISE_RMS_THRESHOLD": 800,    # sustained ambient noise
    "AUDIO_VOICE_RMS_THRESHOLD": 1800,   # likely speech/voice
    "AUDIO_NOISE_DURATION_SEC": 3.0,     # seconds of loud noise = violation
    "AUDIO_VOICE_DURATION_SEC": 2.0,     # seconds of voice-level = speaking

    # Logging
    "LOG_DIR": "proctor_logs",
    "SCREENSHOT_DIR": "proctor_screenshots",
    "LOG_FORMAT": "both",

    # Alert display duration (seconds)
    "ALERT_DISPLAY_SEC": 3.0,
}

# ─────────────────────────────────────────────────────────────────
#  DATA CLASSES
# ─────────────────────────────────────────────────────────────────

@dataclass
class FaceData:
    detected: bool = False
    count: int = 0
    landmarks: Optional[object] = None
    bbox: Optional[Tuple] = None
    transform_matrix: Optional[object] = None   # MediaPipe facial_transformation_matrixes[0]

@dataclass
class GazeData:
    looking_away: bool = False
    left_ear: float = 0.0
    right_ear: float = 0.0
    avg_ear: float = 0.0
    blink_count: int = 0
    blink_rate: float = 0.0

@dataclass
class HeadPoseData:
    yaw: float = 0.0
    pitch: float = 0.0
    roll: float = 0.0
    is_turned: bool = False

@dataclass
class MouthData:
    mar: float = 0.0
    is_open: bool = False
    speaking: bool = False

@dataclass
class AudioData:
    rms: float = 0.0
    is_noisy: bool = False          # above ambient noise threshold
    is_voice: bool = False          # above voice-level threshold
    noise_duration: float = 0.0
    voice_duration: float = 0.0

@dataclass
class ViolationEvent:
    timestamp: str
    violation_type: str
    severity: str
    details: str
    screenshot_path: Optional[str] = None

@dataclass
class SessionStats:
    session_id: str = ""
    start_time: str = ""
    end_time: str = ""
    duration_sec: float = 0.0
    total_violations: int = 0
    violation_counts: Dict[str, int] = field(default_factory=dict)
    avg_suspicious_score: float = 0.0
    max_suspicious_score: float = 0.0
    total_blinks: int = 0
    frames_processed: int = 0


# ─────────────────────────────────────────────────────────────────
#  CLASS: AudioMonitor
# ─────────────────────────────────────────────────────────────────

class AudioMonitor:
    """
    Captures microphone audio in a background thread and computes
    RMS energy to detect:
      • Background noise spikes  (ambient cheating, whispers)
      • Voice-level sound        (the student speaking)

    Uses a rolling 0.5-second window of RMS values for smoothing so
    brief coughs / keyboard clicks don't trigger false positives.
    """

    def __init__(self, sample_rate: int = 16000, chunk: int = 1024,
                 channels: int = 1):
        self.sample_rate = sample_rate
        self.chunk       = chunk
        self.channels    = channels
        self._running    = False
        self._thread: Optional[threading.Thread] = None

        # Rolling RMS window (~0.5 s worth of chunks)
        window_chunks = max(1, int(0.5 * sample_rate / chunk))
        self._rms_window: deque = deque(maxlen=window_chunks)
        self._lock = threading.Lock()

        self._available = False
        self._pa = None
        self._stream = None

        # State timers
        self._noise_start: Optional[float] = None
        self._voice_start: Optional[float] = None

        self._try_init()

    def _try_init(self):
        """Attempt to import pyaudio and open a stream."""
        try:
            import pyaudio
            self._pa = pyaudio.PyAudio()
            self._stream = self._pa.open(
                format=pyaudio.paInt16,
                channels=self.channels,
                rate=self.sample_rate,
                input=True,
                frames_per_buffer=self.chunk,
            )
            self._available = True
            print("[AudioMonitor] Microphone opened successfully.")
        except ImportError:
            print("[AudioMonitor] pyaudio not found — audio detection disabled.")
            print("               Install: pip install pyaudio")
        except Exception as e:
            print(f"[AudioMonitor] Could not open microphone: {e}")
            print("               Audio detection disabled.")

    def start(self):
        if not self._available:
            return
        self._running = True
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()

    def _capture_loop(self):
        while self._running:
            try:
                raw = self._stream.read(self.chunk, exception_on_overflow=False)
                samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
                rms = float(np.sqrt(np.mean(samples ** 2)))
                with self._lock:
                    self._rms_window.append(rms)
            except Exception:
                time.sleep(0.01)

    @property
    def current_rms(self) -> float:
        with self._lock:
            if not self._rms_window:
                return 0.0
            return float(np.mean(self._rms_window))

    def get_audio_data(self, now: float) -> Tuple[AudioData, List[Tuple[str, str, str]]]:
        """
        Returns current AudioData and any triggered violations.
        Call once per frame (or at reduced rate).
        """
        if not self._available:
            return AudioData(), []

        rms = self.current_rms
        noise_thresh = CONFIG["AUDIO_NOISE_RMS_THRESHOLD"]
        voice_thresh = CONFIG["AUDIO_VOICE_RMS_THRESHOLD"]

        is_noisy = rms > noise_thresh
        is_voice = rms > voice_thresh

        violations = []

        # ── Noise level (background talking, whispers) ────────────
        if is_noisy:
            if self._noise_start is None:
                self._noise_start = now
            noise_dur = now - self._noise_start
            if noise_dur > CONFIG["AUDIO_NOISE_DURATION_SEC"]:
                violations.append((
                    "AUDIO_NOISE", "WARNING",
                    f"BACKGROUND NOISE ({rms:.0f} RMS)"
                ))
        else:
            self._noise_start = None
            noise_dur = 0.0

        # ── Voice level (student speaking) ───────────────────────
        if is_voice:
            if self._voice_start is None:
                self._voice_start = now
            voice_dur = now - self._voice_start
            if voice_dur > CONFIG["AUDIO_VOICE_DURATION_SEC"]:
                violations.append((
                    "AUDIO_VOICE", "VIOLATION",
                    f"SPEAKING DETECTED ({rms:.0f} RMS)"
                ))
        else:
            self._voice_start = None
            voice_dur = 0.0

        return AudioData(
            rms=rms,
            is_noisy=is_noisy,
            is_voice=is_voice,
            noise_duration=noise_dur if is_noisy else 0.0,
            voice_duration=voice_dur if is_voice else 0.0,
        ), violations

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
        if self._stream:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
        if self._pa:
            try:
                self._pa.terminate()
            except Exception:
                pass
        print("[AudioMonitor] Microphone released.")


# ─────────────────────────────────────────────────────────────────
#  CLASS: CameraManager
# ─────────────────────────────────────────────────────────────────

class CameraManager:
    def __init__(self, index=0, width=1280, height=720, target_fps=30):
        self.index = index
        self.width = width
        self.height = height
        self.target_fps = target_fps
        self.cap = None
        self._frame_buffer: deque = deque(maxlen=2)
        self._lock = threading.Lock()
        self._running = False
        self._thread = None
        self.actual_fps = 0.0
        self._fps_counter: deque = deque(maxlen=30)

    def start(self) -> bool:
        self.cap = cv2.VideoCapture(self.index)
        if not self.cap.isOpened():
            for idx in [1, 2]:
                self.cap = cv2.VideoCapture(idx)
                if self.cap.isOpened():
                    break
            else:
                print("[CameraManager] ERROR: No camera found.")
                return False
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH,  self.width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        self.cap.set(cv2.CAP_PROP_FPS,          self.target_fps)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE,   1)
        self._running = True
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()
        print(f"[CameraManager] Camera started at index {self.index}")
        return True

    def _capture_loop(self):
        while self._running:
            ret, frame = self.cap.read()
            if ret:
                t = time.time()
                with self._lock:
                    self._frame_buffer.append((frame, t))
                    self._fps_counter.append(t)
                    if len(self._fps_counter) > 1:
                        elapsed = self._fps_counter[-1] - self._fps_counter[0]
                        if elapsed > 0:
                            self.actual_fps = (len(self._fps_counter) - 1) / elapsed

    def read(self) -> Tuple[bool, Optional[np.ndarray]]:
        with self._lock:
            if self._frame_buffer:
                frame, _ = self._frame_buffer[-1]
                return True, frame.copy()
        return False, None

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
        if self.cap:
            self.cap.release()
        print("[CameraManager] Camera released.")


# ─────────────────────────────────────────────────────────────────
#  CLASS: FaceAnalyzer
# ─────────────────────────────────────────────────────────────────

class FaceAnalyzer:
    LEFT_EYE   = [362, 385, 387, 263, 373, 380]
    RIGHT_EYE  = [33,  160, 158, 133, 153, 144]
    LEFT_IRIS  = [474, 475, 476, 477]
    RIGHT_IRIS = [469, 470, 471, 472]
    MOUTH_TOP    = 13
    MOUTH_BOTTOM = 14
    MOUTH_LEFT   = 78
    MOUTH_RIGHT  = 308
    NOSE_TIP     = 1
    CHIN         = 199
    LEFT_EAR_PT  = 234
    RIGHT_EAR_PT = 454
    LEFT_EYE_L   = 263
    RIGHT_EYE_R  = 33

    def __init__(self):
        from mediapipe.tasks import python
        from mediapipe.tasks.python import vision

        model_path = 'face_landmarker.task'
        if not os.path.exists(model_path):
            pass

        base_options = python.BaseOptions(model_asset_path=model_path)
        options = vision.FaceLandmarkerOptions(
            base_options=base_options,
            output_face_blendshapes=True,
            output_facial_transformation_matrixes=True,
            num_faces=3,
            min_face_detection_confidence=0.4,
            min_face_presence_confidence=0.4,
            min_tracking_confidence=0.4,
        )
        self.face_mesh = vision.FaceLandmarker.create_from_options(options)
        self.mp_drawing = vision.drawing_utils
        self.mp_drawing_styles = vision.drawing_styles
        self.FaceLandmarksConnections = vision.FaceLandmarksConnections

    def process(self, frame: np.ndarray) -> Tuple[FaceData, Optional[np.ndarray]]:
        h, w = frame.shape[:2]
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        results = self.face_mesh.detect(mp_image)

        face_data = FaceData()
        annotated = frame.copy()

        if results.face_landmarks:
            face_data.detected = True
            face_data.count = len(results.face_landmarks)
            face_data.landmarks = results.face_landmarks[0]
            # Pass MediaPipe's built-in transformation matrix when available
            if (results.facial_transformation_matrixes and
                    len(results.facial_transformation_matrixes) > 0):
                face_data.transform_matrix = results.facial_transformation_matrixes[0]
            pts = [(int(lm.x * w), int(lm.y * h))
                   for lm in results.face_landmarks[0]]
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            face_data.bbox = (min(xs), min(ys), max(xs), max(ys))
            for fl in results.face_landmarks:
                self.mp_drawing.draw_landmarks(
                    image=annotated,
                    landmark_list=fl,
                    connections=self.FaceLandmarksConnections.FACE_LANDMARKS_TESSELATION,
                    landmark_drawing_spec=None,
                    connection_drawing_spec=self.mp_drawing_styles
                        .get_default_face_mesh_tesselation_style()
                )

        return face_data, annotated

    def get_landmark_px(self, landmarks, index, w, h):
        lm = landmarks[index]
        return int(lm.x * w), int(lm.y * h)

    def get_landmark_np(self, landmarks, indices, w, h):
        return np.array([
            [landmarks[i].x * w, landmarks[i].y * h]
            for i in indices
        ], dtype=np.float32)


# ─────────────────────────────────────────────────────────────────
#  CLASS: EyeTracker
# ─────────────────────────────────────────────────────────────────

class EyeTracker:
    def __init__(self, ear_threshold=0.20, blink_consec_frames=2):
        self.ear_threshold = ear_threshold
        self.blink_consec_frames = blink_consec_frames
        self._blink_counter = 0
        self.blink_count = 0
        self._session_start = time.time()

    def eye_aspect_ratio(self, eye_pts: np.ndarray) -> float:
        A = np.linalg.norm(eye_pts[1] - eye_pts[5])
        B = np.linalg.norm(eye_pts[2] - eye_pts[4])
        C = np.linalg.norm(eye_pts[0] - eye_pts[3])
        if C < 1e-6:
            return 0.0
        return (A + B) / (2.0 * C)

    def gaze_score(self, landmarks, w, h, analyzer):
        left_pts  = analyzer.get_landmark_np(landmarks, FaceAnalyzer.LEFT_EYE,  w, h)
        right_pts = analyzer.get_landmark_np(landmarks, FaceAnalyzer.RIGHT_EYE, w, h)
        left_ear  = self.eye_aspect_ratio(left_pts)
        right_ear = self.eye_aspect_ratio(right_pts)
        avg_ear   = (left_ear + right_ear) / 2.0

        if avg_ear < self.ear_threshold:
            self._blink_counter += 1
        else:
            if self._blink_counter >= self.blink_consec_frames:
                self.blink_count += 1
            self._blink_counter = 0

        looking_away = False
        try:
            l_iris = analyzer.get_landmark_np(
                landmarks, FaceAnalyzer.LEFT_IRIS, w, h).mean(axis=0)
            l_inner = np.array([landmarks[362].x * w, landmarks[362].y * h])
            l_outer = np.array([landmarks[263].x * w, landmarks[263].y * h])
            l_ratio = (l_iris[0] - l_outer[0]) / (l_inner[0] - l_outer[0] + 1e-6)

            r_iris = analyzer.get_landmark_np(
                landmarks, FaceAnalyzer.RIGHT_IRIS, w, h).mean(axis=0)
            r_inner = np.array([landmarks[133].x * w, landmarks[133].y * h])
            r_outer = np.array([landmarks[33].x * w,  landmarks[33].y * h])
            r_ratio = (r_iris[0] - r_outer[0]) / (r_inner[0] - r_outer[0] + 1e-6)

            avg_ratio = (l_ratio + r_ratio) / 2.0
            looking_away = not (0.25 < avg_ratio < 0.75)
        except (IndexError, AttributeError):
            pass

        return left_ear, right_ear, looking_away

    def blink_rate(self) -> float:
        elapsed = (time.time() - self._session_start) / 60.0
        return self.blink_count / elapsed if elapsed > 0 else 0.0


# ─────────────────────────────────────────────────────────────────
#  CLASS: HeadPoseEstimator  (with hysteresis)
# ─────────────────────────────────────────────────────────────────

class HeadPoseEstimator:
    """
    Estimates yaw/pitch/roll using MediaPipe's own facial_transformation_matrixes
    (4x4 model-space matrix already computed by FaceLandmarker).

    This is MUCH more stable than solvePnP because:
      - MediaPipe uses its own calibrated model geometry
      - No gimbal-lock / solution-flip issues
      - Works reliably in low light and off-axis poses

    Falls back to a simple landmark-geometry approach if the matrix
    is unavailable.

    Hysteresis:
      is_turned → True  only when |yaw| > YAW_THRESHOLD  OR |pitch| > PITCH_THRESHOLD
      is_turned → False only when |yaw| < YAW_THRESHOLD_CLEAR AND |pitch| < PITCH_THRESHOLD_CLEAR
    """

    def __init__(self):
        self._is_turned = False   # persistent hysteresis state

    def estimate(self, landmarks, w: int, h: int,
                 transform_matrix=None) -> HeadPoseData:
        """
        Primary path: extract Euler angles from MediaPipe's 4x4
        facial_transformation_matrixes (model → world space).

        Fallback: use nose/eye/mouth landmark geometry when the
        matrix is unavailable (e.g. older MediaPipe versions).
        """
        yaw = pitch = roll = 0.0

        if transform_matrix is not None:
            try:
                # MediaPipe 4x4 column-major matrix → extract 3x3 rotation
                m = np.array(transform_matrix.data, dtype=np.float64).reshape(4, 4)
                R = m[:3, :3]

                # Standard ZYX Euler decomposition (same convention as before)
                sy = np.sqrt(R[0, 0]**2 + R[1, 0]**2)
                if sy > 1e-6:
                    pitch = np.degrees(np.arctan2( R[2, 1],  R[2, 2]))
                    yaw   = np.degrees(np.arctan2(-R[2, 0],  sy))
                    roll  = np.degrees(np.arctan2( R[1, 0],  R[0, 0]))
                else:
                    pitch = np.degrees(np.arctan2(-R[1, 2],  R[1, 1]))
                    yaw   = np.degrees(np.arctan2(-R[2, 0],  sy))
                    roll  = 0.0

                # Clamp to sane range — matrix should never produce >90 on a
                # visible face, so anything outside is a bad frame
                if abs(pitch) > 90 or abs(yaw) > 90:
                    return HeadPoseData(yaw=0.0, pitch=0.0, roll=roll,
                                        is_turned=self._is_turned)

            except Exception:
                # Matrix decode failed — fall through to landmark fallback
                transform_matrix = None

        if transform_matrix is None:
            # ── Landmark geometry fallback ────────────────────────
            # Estimate yaw from horizontal nose offset between eye corners,
            # pitch from nose-tip vs midpoint of eye line vertical offset.
            try:
                nose  = landmarks[1]
                l_eye = landmarks[33]
                r_eye = landmarks[263]
                chin  = landmarks[199]

                eye_mid_x = (l_eye.x + r_eye.x) / 2.0
                eye_mid_y = (l_eye.y + r_eye.y) / 2.0
                eye_width = abs(r_eye.x - l_eye.x)

                if eye_width > 1e-4:
                    # Yaw: nose offset left/right of eye centre (normalised)
                    yaw = np.degrees(np.arcsin(
                        np.clip((nose.x - eye_mid_x) / eye_width * 2.5, -1, 1)))
                    # Pitch: nose below vs above eye mid-line (normalised)
                    face_height = abs(chin.y - eye_mid_y)
                    if face_height > 1e-4:
                        pitch = np.degrees(np.arcsin(
                            np.clip((nose.y - eye_mid_y) / face_height * 1.5
                                    - 0.3, -1, 1)))
            except Exception:
                pass

        # ── Hysteresis ────────────────────────────────────────────
        if not self._is_turned:
            if (abs(yaw)   > CONFIG["YAW_THRESHOLD"] or
                    abs(pitch) > CONFIG["PITCH_THRESHOLD"]):
                self._is_turned = True
        else:
            if (abs(yaw)   < CONFIG["YAW_THRESHOLD_CLEAR"] and
                    abs(pitch) < CONFIG["PITCH_THRESHOLD_CLEAR"]):
                self._is_turned = False

        return HeadPoseData(yaw=yaw, pitch=pitch, roll=roll,
                            is_turned=self._is_turned)


# ─────────────────────────────────────────────────────────────────
#  CLASS: MouthAnalyzer
# ─────────────────────────────────────────────────────────────────

class MouthAnalyzer:
    UPPER_LIP    = 13
    LOWER_LIP    = 14
    LEFT_CORNER  = 78
    RIGHT_CORNER = 308

    def analyze(self, landmarks, w, h) -> MouthData:
        def pt(idx):
            lm = landmarks[idx]
            return np.array([lm.x * w, lm.y * h])

        upper = pt(self.UPPER_LIP)
        lower = pt(self.LOWER_LIP)
        left  = pt(self.LEFT_CORNER)
        right = pt(self.RIGHT_CORNER)
        vertical   = np.linalg.norm(upper - lower)
        horizontal = np.linalg.norm(left  - right)
        mar = vertical / (horizontal + 1e-6)
        is_open = mar > CONFIG["MAR_THRESHOLD"]
        return MouthData(mar=mar, is_open=is_open)


# ─────────────────────────────────────────────────────────────────
#  CLASS: ViolationDetector
# ─────────────────────────────────────────────────────────────────

class ViolationDetector:
    def __init__(self):
        self._timers: Dict[str, Optional[float]] = {
            "gaze_away":   None,
            "no_face":     None,
            "multi_face":  None,
            "speaking":    None,
            "head_turned": None,
        }
        self._score = 0.0
        self._score_history: deque = deque(maxlen=300)
        self._speaking_frames: deque = deque(maxlen=10)

    def update(self, face: FaceData, gaze: GazeData,
               head: HeadPoseData, mouth: MouthData,
               now: float) -> List[Tuple[str, str, str]]:
        triggered = []

        # No Face
        if not face.detected:
            if self._timers["no_face"] is None:
                self._timers["no_face"] = now
            elif now - self._timers["no_face"] > CONFIG["NO_FACE_THRESHOLD_SEC"]:
                triggered.append(("NO_FACE", "VIOLATION", "NO FACE DETECTED"))
                self._add_score(CONFIG["SCORE_NO_FACE"])
        else:
            self._timers["no_face"] = None

        # Multiple Faces
        if face.detected and face.count > 1:
            if self._timers["multi_face"] is None:
                self._timers["multi_face"] = now
            elif now - self._timers["multi_face"] > CONFIG["MULTIPLE_FACE_THRESHOLD_SEC"]:
                triggered.append(("MULTIPLE_FACES", "VIOLATION",
                                   f"MULTIPLE FACES: {face.count}"))
                self._add_score(CONFIG["SCORE_MULTIPLE_FACE"])
        else:
            self._timers["multi_face"] = None

        # Gaze Away
        if face.detected and gaze.looking_away:
            if self._timers["gaze_away"] is None:
                self._timers["gaze_away"] = now
            elif now - self._timers["gaze_away"] > CONFIG["GAZE_AWAY_THRESHOLD_SEC"]:
                triggered.append(("GAZE_AWAY", "WARNING", "LOOK AT SCREEN"))
                self._add_score(CONFIG["SCORE_GAZE_AWAY"])
        else:
            self._timers["gaze_away"] = None

        # Head Turned
        if face.detected and head.is_turned:
            if self._timers["head_turned"] is None:
                self._timers["head_turned"] = now
            elif now - self._timers["head_turned"] > CONFIG["HEAD_MOVE_THRESHOLD_SEC"]:
                triggered.append(("HEAD_TURNED", "WARNING",
                                   f"HEAD TURNED (Y:{head.yaw:+.0f}° P:{head.pitch:+.0f}°)"))
                self._add_score(CONFIG["SCORE_HEAD_MOVE"])
        else:
            self._timers["head_turned"] = None

        # Speaking (visual)
        self._speaking_frames.append(1 if mouth.is_open else 0)
        speak_ratio = sum(self._speaking_frames) / max(len(self._speaking_frames), 1)
        mouth.speaking = speak_ratio > 0.6

        if face.detected and mouth.speaking:
            if self._timers["speaking"] is None:
                self._timers["speaking"] = now
            elif now - self._timers["speaking"] > CONFIG["SPEAKING_THRESHOLD_SEC"]:
                triggered.append(("SPEAKING", "WARNING", "STOP SPEAKING"))
                self._add_score(CONFIG["SCORE_SPEAKING"])
        else:
            self._timers["speaking"] = None

        # Score decay
        self._score = max(0.0, self._score - 0.15)
        self._score_history.append(self._score)
        return triggered

    def _add_score(self, amount: float):
        self._score = min(100.0, self._score + amount)

    @property
    def suspicious_score(self) -> float:
        return self._score

    @property
    def avg_score(self) -> float:
        if not self._score_history:
            return 0.0
        return sum(self._score_history) / len(self._score_history)

    @property
    def max_score(self) -> float:
        return max(self._score_history) if self._score_history else 0.0

    def reset_counters(self):
        for k in self._timers:
            self._timers[k] = None
        self._score = 0.0


# ─────────────────────────────────────────────────────────────────
#  CLASS: Logger
# ─────────────────────────────────────────────────────────────────

class Logger:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self._events: List[ViolationEvent] = []
        self._lock = threading.Lock()
        self.event_callback = None
        self.screenshot_callback = None
        self._log_dir = CONFIG["LOG_DIR"]
        self._ss_dir  = CONFIG["SCREENSHOT_DIR"]
        os.makedirs(self._log_dir, exist_ok=True)
        os.makedirs(self._ss_dir,  exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._json_path = os.path.join(self._log_dir, f"session_{ts}.json")
        self._csv_path  = os.path.join(self._log_dir, f"session_{ts}.csv")

        if CONFIG["LOG_FORMAT"] in ("csv", "both"):
            with open(self._csv_path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["timestamp", "type", "severity",
                             "details", "screenshot"])
        print(f"[Logger] Session {session_id} started. Logs → {self._log_dir}")

    def log(self, event: ViolationEvent):
        with self._lock:
            self._events.append(event)
        if self.event_callback:
            try:
                self.event_callback(event)
            except Exception as e:
                print(f"[Logger] Event callback failed: {e}")
        if CONFIG["LOG_FORMAT"] in ("csv", "both"):
            with open(self._csv_path, "a", newline="") as f:
                w = csv.writer(f)
                w.writerow([event.timestamp, event.violation_type,
                             event.severity, event.details,
                             event.screenshot_path or ""])

    def save_screenshot(self, frame: np.ndarray, vtype: str) -> str:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = os.path.join(self._ss_dir, f"{vtype}_{ts}.jpg")
        cv2.imwrite(path, frame)
        if self.screenshot_callback:
            try:
                self.screenshot_callback(path, vtype)
            except Exception as e:
                print(f"[Logger] Screenshot callback failed: {e}")
        return path

    def finalize(self, stats: SessionStats):
        summary = {
            "session": asdict(stats),
            "events":  [asdict(e) for e in self._events]
        }
        if CONFIG["LOG_FORMAT"] in ("json", "both"):
            with open(self._json_path, "w") as f:
                json.dump(summary, f, indent=2)
        print(f"\n[Logger] Session summary:")
        print(f"  Duration:          {stats.duration_sec:.1f}s")
        print(f"  Total violations:  {stats.total_violations}")
        print(f"  Avg suspicion:     {stats.avg_suspicious_score:.1f}/100")
        print(f"  Logs saved →       {self._log_dir}")


# ─────────────────────────────────────────────────────────────────
#  CLASS: OverlayRenderer
# ─────────────────────────────────────────────────────────────────

class OverlayRenderer:
    C_GREEN  = (50,  205,  50)
    C_YELLOW = (0,   200, 255)
    C_RED    = (50,   50, 220)
    C_WHITE  = (240, 240, 240)
    C_DARK   = (20,   20,  20)
    C_PANEL  = (15,   15,  15)
    C_ACCENT = (0,   180, 255)
    C_CYAN   = (200, 200,   0)

    def __init__(self):
        self._alerts: deque = deque()
        self._font      = cv2.FONT_HERSHEY_SIMPLEX
        self._font_mono = cv2.FONT_HERSHEY_DUPLEX

    def add_alert(self, message: str, color: Tuple, duration: float = 3.0):
        expire = time.time() + duration
        self._alerts = deque(
            [(m, c, e) for m, c, e in self._alerts if m != message], maxlen=5)
        self._alerts.append((message, color, expire))

    def _draw_rect_alpha(self, frame, x1, y1, x2, y2, color, alpha=0.6):
        overlay = frame.copy()
        cv2.rectangle(overlay, (x1, y1), (x2, y2), color, -1)
        cv2.addWeighted(overlay, alpha, frame, 1 - alpha, 0, frame)

    def _draw_rounded_rect(self, frame, x1, y1, x2, y2, color, radius=8, alpha=0.7):
        overlay = frame.copy()
        cv2.rectangle(overlay, (x1 + radius, y1), (x2 - radius, y2), color, -1)
        cv2.rectangle(overlay, (x1, y1 + radius), (x2, y2 - radius), color, -1)
        for cx, cy in [(x1+radius, y1+radius), (x2-radius, y1+radius),
                        (x1+radius, y2-radius), (x2-radius, y2-radius)]:
            cv2.circle(overlay, (cx, cy), radius, color, -1)
        cv2.addWeighted(overlay, alpha, frame, 1 - alpha, 0, frame)

    def render(self, frame, face: FaceData, gaze: GazeData,
               head: HeadPoseData, mouth: MouthData,
               audio: AudioData,
               detector: ViolationDetector, fps: float,
               session_elapsed: float, total_violations: int):
        h, w = frame.shape[:2]
        now = time.time()

        # ── Left Metrics Panel ────────────────────────────────────
        panel_w = 270
        panel_h = 370
        self._draw_rounded_rect(frame, 8, 8, panel_w, panel_h,
                                  self.C_PANEL, radius=10, alpha=0.75)
        y = 35
        cv2.putText(frame, "PROCTOR  METRICS", (20, y),
                    self._font, 0.52, self.C_ACCENT, 1, cv2.LINE_AA)
        y += 8
        cv2.line(frame, (20, y), (panel_w - 10, y), self.C_ACCENT, 1)
        y += 22

        def metric_row(label, value, status_color):
            nonlocal y
            cv2.putText(frame, label, (20, y),
                        self._font, 0.42, (160, 160, 160), 1, cv2.LINE_AA)
            cv2.putText(frame, value, (145, y),
                        self._font, 0.46, status_color, 1, cv2.LINE_AA)
            cv2.circle(frame, (panel_w - 18, y - 4), 5, status_color, -1)
            y += 22

        face_val   = (f"{face.count} face{'s' if face.count != 1 else ''}"
                      if face.detected else "none")
        face_color = self.C_RED if not face.detected or face.count > 1 else self.C_GREEN
        metric_row("FACE",   face_val,  face_color)

        gaze_val   = "away" if gaze.looking_away else "ok"
        gaze_color = self.C_YELLOW if gaze.looking_away else self.C_GREEN
        metric_row("GAZE",   gaze_val,  gaze_color)

        metric_row("EAR",    f"{gaze.avg_ear:.3f}", self.C_WHITE)
        metric_row("BLINKS", f"{gaze.blink_count}  ({gaze.blink_rate:.1f}/min)", self.C_CYAN)

        head_val   = f"Y:{head.yaw:+.0f}° P:{head.pitch:+.0f}°"
        head_color = self.C_YELLOW if head.is_turned else self.C_GREEN
        metric_row("HEAD",   head_val,  head_color)

        mouth_val   = "speaking" if mouth.speaking else ("open" if mouth.is_open else "closed")
        mouth_color = self.C_YELLOW if mouth.speaking else self.C_GREEN
        metric_row("MOUTH",  mouth_val, mouth_color)

        metric_row("ROLL",   f"{head.roll:+.0f}°", self.C_WHITE)

        y += 5
        cv2.line(frame, (20, y), (panel_w - 10, y), (60, 60, 60), 1)
        y += 18

        # ── Audio rows ────────────────────────────────────────────
        audio_rms_color = (self.C_RED    if audio.is_voice  else
                           self.C_YELLOW if audio.is_noisy else
                           self.C_GREEN)
        audio_label = ("VOICE!" if audio.is_voice else
                        "NOISY"  if audio.is_noisy else
                        "quiet")
        metric_row("AUDIO",  f"{audio.rms:.0f} RMS", audio_rms_color)
        metric_row("SOUND",  audio_label,             audio_rms_color)

        y += 5
        cv2.line(frame, (20, y), (panel_w - 10, y), (60, 60, 60), 1)
        y += 18

        v_color = (self.C_RED    if total_violations > 5 else
                   self.C_YELLOW if total_violations > 0 else
                   self.C_GREEN)
        cv2.putText(frame, f"VIOLATIONS: {total_violations}", (20, y),
                    self._font, 0.50, v_color, 1, cv2.LINE_AA)

        # ── Audio waveform mini-bar ───────────────────────────────
        self._draw_audio_bar(frame, audio, 8, h - 120, panel_w, h - 80)

        # ── Suspicious score gauge ────────────────────────────────
        self._draw_score_gauge(frame, detector.suspicious_score,
                               8, h - 80, panel_w, h - 15)

        # ── Status badge ──────────────────────────────────────────
        self._draw_status_badge(frame, detector.suspicious_score, w, total_violations)

        # ── FPS + timer ───────────────────────────────────────────
        elapsed_str = time.strftime("%M:%S", time.gmtime(session_elapsed))
        cv2.putText(frame, f"FPS: {fps:.1f}   {elapsed_str}",
                    (w - 200, 75), self._font, 0.48, (140, 140, 140), 1, cv2.LINE_AA)

        # ── Face bounding box ─────────────────────────────────────
        if face.bbox:
            x1, y1_, x2, y2_ = face.bbox
            box_col = self.C_GREEN if face.count == 1 else self.C_RED
            cv2.rectangle(frame, (x1, y1_), (x2, y2_), box_col, 2)
            if face.count > 1:
                cv2.putText(frame, "EXTRA FACE", (x1, y1_ - 8),
                            self._font, 0.5, self.C_RED, 2, cv2.LINE_AA)

        # ── Alerts ────────────────────────────────────────────────
        alive = [(m, c, e) for m, c, e in self._alerts if e > now]
        self._alerts = deque(alive, maxlen=5)
        self._draw_alerts(frame, w, h)

        # ── Bottom banner ─────────────────────────────────────────
        self._draw_rect_alpha(frame, 0, h - 14, w, h, (0, 0, 0), 0.5)
        cv2.putText(frame, "Q: Quit   S: Screenshot   R: Reset",
                    (w // 2 - 150, h - 3),
                    self._font, 0.38, (120, 120, 120), 1, cv2.LINE_AA)

    def _draw_audio_bar(self, frame, audio: AudioData,
                        x1, y1, x2, y2):
        """Small horizontal RMS energy bar above the risk gauge."""
        self._draw_rect_alpha(frame, x1, y1, x2, y2, self.C_PANEL, 0.7)
        bar_x1 = x1 + 75
        bar_y1 = y1 + 10
        bar_y2 = y2 - 10
        bar_w  = x2 - bar_x1 - 10
        # Background
        cv2.rectangle(frame, (bar_x1, bar_y1), (bar_x1 + bar_w, bar_y2),
                       (50, 50, 50), -1)
        # Fill — scale against voice threshold
        max_rms = CONFIG["AUDIO_VOICE_RMS_THRESHOLD"] * 1.5
        fill_ratio = min(1.0, audio.rms / max_rms)
        fill_w = int(bar_w * fill_ratio)
        if fill_w > 0:
            fill_color = (self.C_RED    if audio.is_voice  else
                          self.C_YELLOW if audio.is_noisy else
                          self.C_GREEN)
            cv2.rectangle(frame, (bar_x1, bar_y1),
                           (bar_x1 + fill_w, bar_y2), fill_color, -1)
        cv2.rectangle(frame, (bar_x1, bar_y1), (bar_x1 + bar_w, bar_y2),
                       (100, 100, 100), 1)
        cv2.putText(frame, "MIC", (x1 + 5, y1 + 20),
                    self._font, 0.44, self.C_WHITE, 1, cv2.LINE_AA)

    def _draw_score_gauge(self, frame, score, x1, y1, x2, y2):
        self._draw_rect_alpha(frame, x1, y1, x2, y2, self.C_PANEL, 0.7)
        bar_x1 = x1 + 65
        bar_y1 = y1 + 12
        bar_y2 = y2 - 12
        bar_w  = x2 - bar_x1 - 10
        cv2.rectangle(frame, (bar_x1, bar_y1), (bar_x1 + bar_w, bar_y2),
                       (50, 50, 50), -1)
        fill_w = int(bar_w * score / 100.0)
        if fill_w > 0:
            fill_color = (self.C_GREEN  if score < 30 else
                          self.C_YELLOW if score < 60 else self.C_RED)
            cv2.rectangle(frame, (bar_x1, bar_y1),
                           (bar_x1 + fill_w, bar_y2), fill_color, -1)
        cv2.rectangle(frame, (bar_x1, bar_y1), (bar_x1 + bar_w, bar_y2),
                       (100, 100, 100), 1)
        cv2.putText(frame, f"RISK {score:.0f}%", (x1 + 5, y1 + 22),
                    self._font, 0.44, self.C_WHITE, 1, cv2.LINE_AA)

    def _draw_status_badge(self, frame, score, w, violations):
        if score > 60 or violations > 5:
            status, color = "VIOLATION", self.C_RED
        elif score > 25 or violations > 0:
            status, color = "WARNING",   self.C_YELLOW
        else:
            status, color = "ACTIVE",    self.C_GREEN
        bw, bh = 160, 36
        bx = w - bw - 10
        by = 10
        self._draw_rounded_rect(frame, bx, by, bx + bw, by + bh,
                                  color, radius=6, alpha=0.85)
        tw = cv2.getTextSize(status, self._font_mono, 0.65, 2)[0][0]
        cv2.putText(frame, status, (bx + (bw - tw) // 2, by + 24),
                    self._font_mono, 0.65, (20, 20, 20), 2, cv2.LINE_AA)

    def _draw_alerts(self, frame, w, h):
        if not self._alerts:
            return
        base_y = h // 2 - 30
        for i, (msg, color, _) in enumerate(reversed(list(self._alerts))):
            tw = cv2.getTextSize(msg, self._font, 1.0, 3)[0][0]
            x  = (w - tw) // 2
            y  = base_y + i * 60
            cv2.putText(frame, msg, (x + 2, y + 2),
                        self._font, 1.0, (0, 0, 0), 4, cv2.LINE_AA)
            cv2.putText(frame, msg, (x, y),
                        self._font, 1.0, color, 3, cv2.LINE_AA)


# ─────────────────────────────────────────────────────────────────
#  CLASS: ProctorSystem  (Main Controller)
# ─────────────────────────────────────────────────────────────────

class ProctorSystem:
    def __init__(self):
        self.session_id  = datetime.now().strftime("SES_%Y%m%d_%H%M%S")
        self._start_time = time.time()

        self.camera    = CameraManager(
            index=CONFIG["CAMERA_INDEX"],
            width=CONFIG["FRAME_WIDTH"],
            height=CONFIG["FRAME_HEIGHT"],
            target_fps=CONFIG["TARGET_FPS"],
        )
        self.analyzer    = FaceAnalyzer()
        self.eye_tracker = EyeTracker(
            ear_threshold=CONFIG["EAR_THRESHOLD"],
            blink_consec_frames=CONFIG["BLINK_CONSEC_FRAMES"],
        )
        self.head_pose = HeadPoseEstimator()
        self.mouth     = MouthAnalyzer()
        self.detector  = ViolationDetector()
        self.logger    = Logger(self.session_id)
        self.overlay   = OverlayRenderer()

        # Audio monitor (gracefully disabled if pyaudio missing)
        self.audio_monitor = (AudioMonitor(
            sample_rate=CONFIG["AUDIO_SAMPLE_RATE"],
            chunk=CONFIG["AUDIO_CHUNK_SIZE"],
            channels=CONFIG["AUDIO_CHANNELS"],
        ) if CONFIG["AUDIO_ENABLED"] else None)

        self._frame_count      = 0
        self._total_violations = 0
        self._violation_counts: Dict[str, int] = {}
        self._running          = True
        self._violation_queue: queue.Queue = queue.Queue()

        # Per-violation type cooldown tracking
        self._last_violation_times: Dict[str, float] = {}

        self._log_thread = threading.Thread(
            target=self._log_worker, daemon=True)

    def _log_worker(self):
        while self._running or not self._violation_queue.empty():
            try:
                event = self._violation_queue.get(timeout=0.5)
                self.logger.log(event)
                self._violation_queue.task_done()
            except queue.Empty:
                continue

    def _handle_violations(self, violations, frame):
        now = time.time()
        for vtype, severity, message in violations:
            last = self._last_violation_times.get(vtype, 0)
            if now - last < 4.0:
                continue
            self._last_violation_times[vtype] = now

            self._total_violations += 1
            self._violation_counts[vtype] = \
                self._violation_counts.get(vtype, 0) + 1

            color = (self.overlay.C_RED
                     if severity == "VIOLATION"
                     else self.overlay.C_YELLOW)
            self.overlay.add_alert(message, color,
                                    duration=CONFIG["ALERT_DISPLAY_SEC"])

            ss_path = None
            if severity == "VIOLATION":
                ss_path = self.logger.save_screenshot(frame, vtype)

            self._violation_queue.put(ViolationEvent(
                timestamp=datetime.now().isoformat(),
                violation_type=vtype,
                severity=severity,
                details=message,
                screenshot_path=ss_path,
            ))

    def run(self):
        if not self.camera.start():
            print("[ProctorSystem] Cannot start camera. Exiting.")
            return

        if self.audio_monitor:
            self.audio_monitor.start()

        self._log_thread.start()
        print("[ProctorSystem] Proctoring ACTIVE. Press Q to quit.")
        print(f"[ProctorSystem] Session ID: {self.session_id}\n")

        window_name = f"AI Exam Proctor — {self.session_id}"
        cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(window_name,
                          CONFIG["FRAME_WIDTH"], CONFIG["FRAME_HEIGHT"])

        # Pre-initialize cached state
        self._last_face  = FaceData()
        self._last_gaze  = GazeData()
        self._last_head  = HeadPoseData()
        self._last_mouth = MouthData()
        self._last_audio = AudioData()
        self._last_frame = None

        while self._running:
            ret, raw_frame = self.camera.read()
            if not ret or raw_frame is None:
                time.sleep(0.01)
                continue

            self._frame_count += 1
            now     = time.time()
            elapsed = now - self._start_time
            h, w    = raw_frame.shape[:2]

            should_analyze = (self._frame_count %
                               CONFIG["PROCESS_EVERY_N_FRAMES"] == 0)

            all_violations = []

            if should_analyze:
                # Vision analysis
                face_data, annotated = self.analyzer.process(raw_frame)

                if face_data.detected and face_data.landmarks:
                    lm = face_data.landmarks
                    l_ear, r_ear, looking_away = self.eye_tracker.gaze_score(
                        lm, w, h, self.analyzer)
                    avg_ear = (l_ear + r_ear) / 2.0
                    gaze_data = GazeData(
                        looking_away=looking_away,
                        left_ear=l_ear,
                        right_ear=r_ear,
                        avg_ear=avg_ear,
                        blink_count=self.eye_tracker.blink_count,
                        blink_rate=self.eye_tracker.blink_rate(),
                    )
                    head_data  = self.head_pose.estimate(lm, w, h)
                    mouth_data = self.mouth.analyze(lm, w, h)
                else:
                    annotated  = raw_frame.copy()
                    gaze_data  = GazeData()
                    head_data  = HeadPoseData()
                    mouth_data = MouthData()

                vis_violations = self.detector.update(
                    face_data, gaze_data, head_data, mouth_data, now)
                all_violations.extend(vis_violations)

                # Audio analysis (every analyzed frame)
                if self.audio_monitor and self.audio_monitor._available:
                    audio_data, aud_violations = \
                        self.audio_monitor.get_audio_data(now)
                    all_violations.extend(aud_violations)
                    # Also add audio score contribution
                    if audio_data.is_voice:
                        self.detector._add_score(
                            CONFIG["SCORE_AUDIO_NOISE"] * 0.5)
                else:
                    audio_data = AudioData()

                self._last_face  = face_data
                self._last_gaze  = gaze_data
                self._last_head  = head_data
                self._last_mouth = mouth_data
                self._last_audio = audio_data
                self._last_frame = annotated

                if all_violations:
                    self._handle_violations(all_violations, annotated)
            else:
                annotated  = raw_frame.copy()
                face_data  = self._last_face
                gaze_data  = self._last_gaze
                head_data  = self._last_head
                mouth_data = self._last_mouth
                audio_data = self._last_audio

            # Render
            self.overlay.render(
                annotated, face_data, gaze_data, head_data, mouth_data,
                audio_data, self.detector,
                fps=self.camera.actual_fps,
                session_elapsed=elapsed,
                total_violations=self._total_violations,
            )

            cv2.imshow(window_name, annotated)

            key = cv2.waitKey(1) & 0xFF
            if key == ord('q') or key == 27:
                self._running = False
            elif key == ord('s'):
                path = self.logger.save_screenshot(annotated, "MANUAL")
                self.overlay.add_alert("Screenshot saved", self.overlay.C_CYAN)
                print(f"[ProctorSystem] Manual screenshot → {path}")
            elif key == ord('r'):
                self.detector.reset_counters()
                self._total_violations = 0
                self.overlay.add_alert("Counters reset", self.overlay.C_GREEN)
                print("[ProctorSystem] Violation counters reset.")

        self._shutdown()

    def _shutdown(self):
        print("\n[ProctorSystem] Shutting down…")
        self._running = False
        self.camera.stop()
        if self.audio_monitor:
            self.audio_monitor.stop()
        cv2.destroyAllWindows()
        self._violation_queue.join()

        duration = time.time() - self._start_time
        stats = SessionStats(
            session_id=self.session_id,
            start_time=datetime.fromtimestamp(self._start_time).isoformat(),
            end_time=datetime.now().isoformat(),
            duration_sec=round(duration, 2),
            total_violations=self._total_violations,
            violation_counts=self._violation_counts,
            avg_suspicious_score=round(self.detector.avg_score, 2),
            max_suspicious_score=round(self.detector.max_score, 2),
            total_blinks=self.eye_tracker.blink_count,
            frames_processed=self._frame_count,
        )
        self.logger.finalize(stats)
        print("[ProctorSystem] Session ended. Goodbye.")


# ─────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 64)
    print("  AI EXAM PROCTORING SYSTEM  |  Starting…")
    print("=" * 64)
    print(f"  Session: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Logs   : ./{CONFIG['LOG_DIR']}/")
    print(f"  Shots  : ./{CONFIG['SCREENSHOT_DIR']}/")
    print("  Keys   : Q=Quit  S=Screenshot  R=Reset")
    print("=" * 64)

    try:
        system = ProctorSystem()
        system.run()
    except KeyboardInterrupt:
        print("\n[Main] Interrupted by user.")
    except Exception as e:
        print(f"\n[Main] Unexpected error: {e}")
        import traceback
        traceback.print_exc()
