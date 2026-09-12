#!/usr/bin/env python3
"""OLED Emotion Sidecar — runs alongside Pal as a managed subprocess"""

import sys
import os
import json
import time
import signal
import socket
import threading
from pathlib import Path
from collections import deque
from PIL import Image

SIDECAR_PATH = os.path.abspath(os.path.dirname(__file__))

PLUGIN_DIR = Path(SIDECAR_PATH)
if "--plugin-dir" in sys.argv:
    idx = sys.argv.index("--plugin-dir")
    PLUGIN_DIR = Path(sys.argv[idx + 1])

SIDECAR_DIR = PLUGIN_DIR.parent.parent.parent / "data" / "oled_sidecar"
if "--sidecar-dir" in sys.argv:
    idx = sys.argv.index("--sidecar-dir")
    SIDECAR_DIR = Path(sys.argv[idx + 1])

sys.path.insert(0, str(PLUGIN_DIR))
from ssd1306 import SSD1306

EMOTIONS_DIR = PLUGIN_DIR / "emotions"
SOCKET_PATH = SIDECAR_DIR / "oled.sock"
PID_PATH = SIDECAR_DIR / "oled.pid"
LOG_PATH = SIDECAR_DIR / "oled.log"

STANDBY_TO_SLEEPY = 3600
SLEEPY_TO_STANDBY = 3600
ACTIVE_TIMEOUT = 30.0  # seconds without new signal before ACTIVE -> STANDBY
I2C_WRITE_TIMEOUT = 5.0  # seconds per frame write before declaring I2C hang

PLAYBACK_SPEED_USER = 3.0
PLAYBACK_SPEED_IDLE = 2.0
MIN_DISPLAY_TIME = 1.0  # minimum seconds before switching to next emotion

VALID_EMOTIONS = [
    "happy", "sad", "angry", "crying", "curious",
    "shock", "sleepy", "thinking", "wink", "error",
    "standby", "working",
]

# Persistent emotions alternate in a carousel while Pal is actively working.
# Expressive emotions (happy, wink, etc.) preempt the carousel, play once,
# then the carousel resumes. Idle emotions (standby, sleepy) exit the carousel.
PERSISTENT_CAROUSEL = ("thinking", "working")
PERSISTENT_EMOTIONS = frozenset(PERSISTENT_CAROUSEL)
IDLE_EMOTIONS = frozenset({"standby", "sleepy"})


def kill_existing():
    if PID_PATH.exists():
        try:
            old_pid = int(PID_PATH.read_text().strip())
            if old_pid != os.getpid() and old_pid > 0:
                os.kill(old_pid, signal.SIGTERM)
                print(f"Killed old sidecar PID {old_pid}")
                time.sleep(0.3)
        except (ValueError, ProcessLookupError, PermissionError):
            pass
    if SOCKET_PATH.exists():
        try:
            SOCKET_PATH.unlink()
        except Exception:
            pass


class EmotionRequest:
    def __init__(self):
        self._lock = threading.Lock()
        self._queue: deque = deque()
        self._last_pop_time: float = 0.0

    def push(self, emotion: str, is_sleeping: bool = False):
        with self._lock:
            # Idle emotions take priority: drain stale persistent entries so
            # "standby" isn't stuck behind 20 accumulated thinking/working items.
            if emotion in IDLE_EMOTIONS:
                self._queue.clear()
            if is_sleeping and emotion != "shock":
                self._queue.append({"emotion": "shock", "wakeup": True, "uninterruptible": True})
                self._queue.append({"emotion": emotion, "wakeup": False, "uninterruptible": False})
            else:
                self._queue.append({"emotion": emotion, "wakeup": False, "uninterruptible": False})

    def pop(self) -> dict | None:
        with self._lock:
            if self._queue:
                self._last_pop_time = time.time()
                return self._queue.popleft()
            return None

    def clear(self) -> None:
        with self._lock:
            self._queue.clear()

    def has_pending(self) -> bool:
        with self._lock:
            if not self._queue:
                return False
            # Enforce minimum display time
            if time.time() - self._last_pop_time < MIN_DISPLAY_TIME:
                return False
            return True


class OLEDSidecar:
    def __init__(self):
        self.oled = SSD1306()
        self.request = EmotionRequest()
        self._stop_event = threading.Event()
        self._last_activity = time.time()
        self._current_emotion = None
        self._log("Sidecar started")

    def _log(self, msg):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        try:
            SIDECAR_DIR.mkdir(parents=True, exist_ok=True)
            with open(LOG_PATH, "a") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def _safe_send_image(self, img) -> bool:
        """Send image to OLED with watchdog timeout.

        Normal I2C writes complete in <10ms.  If the bus hangs, the kernel
        keeps retrying and os.write() can block for 60-90 seconds, freezing
        the entire display loop.  This wrapper runs the write in a daemon
        thread; if it doesn't finish within I2C_WRITE_TIMEOUT, we mark the
        bus as hung, reconnect with a fresh fd, and let the stuck thread die
        on its own (it holds the old fd object, so no fd-number reuse race).
        Returns True on success, False on timeout.
        """
        oled = self.oled  # capture — may change during reconnect
        done = threading.Event()

        def _do_write():
            try:
                oled._send_image(img)
            except Exception:
                pass
            finally:
                done.set()

        t = threading.Thread(target=_do_write, daemon=True)
        t.start()

        if done.wait(I2C_WRITE_TIMEOUT):
            return True

        # Write is still going — bus is hung.
        self._log(f"I2C write timed out after {I2C_WRITE_TIMEOUT}s — reconnecting")
        self._reconnect_oled()
        return False

    def _reconnect_oled(self):
        """Create a fresh SSD1306 after an I2C hang.

        The old object (with its stuck fd) is orphaned — the stuck daemon
        thread holds a reference to it through the closure in
        _safe_send_image, so the fd stays valid until that thread dies.
        We do NOT close the old fd (closing while another thread is blocked
        in os.write on it causes fd-number reuse races).
        """
        try:
            self.oled = SSD1306()
            self.oled._init()
            self.oled.clear()
            self._log("OLED reconnected with fresh fd")
        except Exception as e:
            self._log(f"OLED reconnect failed: {e}")

    def _play_gif_once(self, emotion: str, speed: float = 1.0, uninterruptible: bool = False) -> bool:
        gif_path = EMOTIONS_DIR / f"{emotion}.gif"
        if not gif_path.exists():
            self._log(f"GIF not found: {gif_path}")
            return True

        img = Image.open(str(gif_path))
        total = img.n_frames
        bg = Image.new("RGBA", img.size, (0, 0, 0, 0))

        for i in range(total):
            if not uninterruptible and self.request.has_pending():
                self._log(f"Interrupted: {emotion} -> new request")
                return False
            if self._stop_event.is_set():
                return False

            img.seek(i)
            frame_rgba = img.convert("RGBA")
            disposal = img.info.get("disposal", 0)
            if disposal == 2:
                bg = Image.new("RGBA", img.size, (0, 0, 0, 0))
            composite = Image.alpha_composite(bg, frame_rgba)
            bg = composite.copy()
            frame_out = composite.convert("1", dither=Image.NONE).resize(
                (self.oled.WIDTH, self.oled.HEIGHT)
            )
            self._safe_send_image(frame_out)
            duration = img.info.get("duration", 150) / 1000.0 * speed
            time.sleep(duration)

        return True

    def _is_sleeping(self) -> bool:
        return self._current_emotion == "sleepy"

    def _run_display_loop(self):
        self._log("Display loop started")

        # Explicit state machine:
        #   idle    standby (default), → sleep after STANDBY_TO_SLEEPY
        #   sleep   sleepy, → idle after SLEEPY_TO_STANDBY (auto-wake)
        #   active  thinking/working carousel, → idle after ACTIVE_TIMEOUT
        #   wakeup  shock (sleep interrupted), then → active
        # Expressive emotions preempt everything, play once, then resume
        # the state that was active before the preempt.
        state = "idle"
        cycle_idx = 0
        state_entered = time.time()
        last_signal = time.time()

        while not self._stop_event.is_set():
            try:
                now = time.time()
                req = self.request.pop()

                if req:
                    self._last_activity = now
                    emotion = req["emotion"]
                    tag = " (wakeup!)" if req.get("wakeup") else ""
                    self._log(f"Playing: {emotion}{tag}")

                    if emotion in IDLE_EMOTIONS:
                        # Turn end → STANDBY.  Drain any stale queue.
                        self.request.clear()
                        state = "idle"
                        state_entered = now
                        self._current_emotion = "standby"
                        self._play_gif_once("standby", speed=PLAYBACK_SPEED_IDLE)

                    elif req.get("wakeup"):
                        # Sleep interrupted → shock → ACTIVE.
                        self._current_emotion = "shock"
                        self._play_gif_once("shock", speed=PLAYBACK_SPEED_USER,
                                            uninterruptible=True)
                        last_signal = time.time()
                        state = "active"
                        state_entered = last_signal
                        cycle_idx = 0
                        self._current_emotion = PERSISTENT_CAROUSEL[cycle_idx]
                        self._play_gif_once(self._current_emotion, speed=PLAYBACK_SPEED_IDLE)

                    elif emotion in PERSISTENT_EMOTIONS:
                        # Message signal → ACTIVE.
                        last_signal = now
                        state = "active"
                        state_entered = now
                        cycle_idx = 0
                        self._current_emotion = emotion
                        self._play_gif_once(emotion, speed=PLAYBACK_SPEED_IDLE)

                    else:
                        # Expressive — preempt, play once, resume prior state.
                        prev_state = state
                        self._current_emotion = emotion
                        self._play_gif_once(
                            emotion,
                            speed=PLAYBACK_SPEED_USER,
                            uninterruptible=req.get("uninterruptible", False),
                        )
                        state = prev_state
                    continue

                # --- No new request: state-dependent behaviour ---
                if state == "active":
                    if now - last_signal >= ACTIVE_TIMEOUT:
                        self._log("ACTIVE timeout → STANDBY")
                        state = "idle"
                        state_entered = now
                        self._last_activity = now
                        self._current_emotion = "standby"
                        self._play_gif_once("standby", speed=PLAYBACK_SPEED_IDLE)
                    else:
                        cycle_idx = (cycle_idx + 1) % len(PERSISTENT_CAROUSEL)
                        self._current_emotion = PERSISTENT_CAROUSEL[cycle_idx]
                        self._play_gif_once(self._current_emotion, speed=PLAYBACK_SPEED_IDLE)

                elif state == "sleep":
                    if now - state_entered >= SLEEPY_TO_STANDBY:
                        self._log("SLEEP timeout → auto-wake STANDBY")
                        state = "idle"
                        state_entered = now
                        self._last_activity = now
                        self._current_emotion = "standby"
                        self._play_gif_once("standby", speed=PLAYBACK_SPEED_IDLE)
                    else:
                        self._current_emotion = "sleepy"
                        self._play_gif_once("sleepy", speed=PLAYBACK_SPEED_IDLE)

                else:  # state == "idle"
                    if now - state_entered >= STANDBY_TO_SLEEPY:
                        self._log("IDLE timeout → SLEEP")
                        state = "sleep"
                        state_entered = now
                        self._current_emotion = "sleepy"
                        self._play_gif_once("sleepy", speed=PLAYBACK_SPEED_IDLE)
                    else:
                        self._current_emotion = "standby"
                        self._play_gif_once("standby", speed=PLAYBACK_SPEED_IDLE)

            except Exception as e:
                self._log(f"Display loop error (recovering): {e}")
                state = "idle"
                state_entered = time.time()
                self._last_activity = state_entered
                self._current_emotion = "standby"
                time.sleep(1.0)

        self.oled.clear()
        self._log("Display loop stopped")

    def _run_socket_server(self):
        SIDECAR_DIR.mkdir(parents=True, exist_ok=True)
        if SOCKET_PATH.exists():
            SOCKET_PATH.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(SOCKET_PATH))
        server.listen(4)
        server.settimeout(1.0)
        self._log(f"Socket listening: {SOCKET_PATH}")

        while not self._stop_event.is_set():
            try:
                conn, _ = server.accept()
                conn.settimeout(2.0)
                try:
                    data = conn.recv(1024).decode().strip()
                    self._log(f"Received: {data}")
                    if data in ("quit", "exit", "stop"):
                        self._stop_event.set()
                        conn.sendall(b"OK: shutting down\n")
                    elif data in VALID_EMOTIONS:
                        self.request.push(data, is_sleeping=self._is_sleeping())
                        conn.sendall(b"OK\n")
                    elif data == "status":
                        resp = json.dumps({
                            "current": self._current_emotion,
                            "idle_seconds": round(time.time() - self._last_activity, 1),
                            "sleeping": self._is_sleeping(),
                        })
                        conn.sendall((resp + "\n").encode())
                    else:
                        conn.sendall(f"ERR: unknown emotion '{data}'. Valid: {VALID_EMOTIONS}\n".encode())
                except socket.timeout:
                    pass
                finally:
                    conn.close()
            except socket.timeout:
                continue
            except Exception as e:
                if not self._stop_event.is_set():
                    self._log(f"Socket error: {e}")

        server.close()
        self._log("Socket server stopped")

    def run(self):
        SIDECAR_DIR.mkdir(parents=True, exist_ok=True)
        with open(PID_PATH, "w") as f:
            f.write(str(os.getpid()))

        signal.signal(signal.SIGTERM, lambda *_: self._stop_event.set())
        signal.signal(signal.SIGINT, lambda *_: self._stop_event.set())

        display_thread = threading.Thread(target=self._run_display_loop, daemon=True)
        socket_thread = threading.Thread(target=self._run_socket_server, daemon=True)

        socket_thread.start()
        display_thread.start()

        self._log("All threads started")
        self._stop_event.wait()

        socket_thread.join(timeout=3)
        display_thread.join(timeout=3)

        if SOCKET_PATH.exists():
            SOCKET_PATH.unlink()
        if PID_PATH.exists():
            PID_PATH.unlink()

        self._log("Sidecar stopped")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        oled = SSD1306()
        oled.show_gif(str(EMOTIONS_DIR / "happy.gif"), loop=False)
        return

    kill_existing()
    sidecar = OLEDSidecar()
    sidecar.run()


if __name__ == "__main__":
    main()
