#!/usr/bin/env python3
"""OLED status panel sidecar — Pal's status board on the 128x64 I2C display.

Two screens, flipped on a timer:
  1. status   — parsed from the /status control output
  2. clock    — local time + today's weather

Pal never drives this panel. There is no tool call, no turn, no token spent on
it: status text arrives over a unix socket on the normal delivery path while the
clock and weather are produced here. Weather uses this process's own refresh
loop, exactly as the morning weather push does, rather than going through Pal.

The I2C bus admits a single driver, so this replaces the old oled_emotion
sidecar rather than running beside it.

Offline self-test (no hardware touched):

    python3 sidecar.py --png-out /tmp/oled

renders both screens to PNG and exits.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import socket
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 128, 64
I2C_BUS = 1
I2C_ADDR = 0x3C
I2C_SLAVE = 0x0703

# Flip budget: status is the main screen, so it holds longer.
STATUS_SECONDS = 12.0
CLOCK_SECONDS = 8.0

WEATHER_URL = "https://wttr.in/Chengdu?format=j1"
WEATHER_REFRESH_SECONDS = 3600.0
WEATHER_TIMEOUT_SECONDS = 20.0
CITY = "Chengdu"

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FONT_SMALL = 10
FONT_MEDIUM = 14

LINE_CHARS = 21  # 10px monospace on a 128px panel, 2px margin each side
DEGREE = "C"


def _flag(flag: str, default: str = "") -> str:
    if flag in sys.argv:
        idx = sys.argv.index(flag)
        if idx + 1 < len(sys.argv):
            return sys.argv[idx + 1]
    return default


SIDECAR_DIR = Path(_flag("--sidecar-dir", str(Path.home() / ".pal" / "data" / "oled_status")))
SOCKET_PATH = SIDECAR_DIR / "oled.sock"
PID_PATH = SIDECAR_DIR / "oled.pid"
LOG_PATH = SIDECAR_DIR / "oled.log"

# The board shows Nathan's time, which is NOT this host's local time: the Pi runs
# Europe/London (BST, with DST) while his day runs on Asia/Shanghai (fixed +08:00,
# no DST since 1991). The plugin owns that value and passes it in; the sidecar
# deliberately keeps no copy of its own so the two can never drift apart.
CLOCK_TIMEZONE = _flag("--timezone", "").strip()


def _clock_zone() -> ZoneInfo | None:
    """Resolve the display timezone, or None to fall back to host local time.

    None only happens when the sidecar is run by hand without --timezone; the
    supervised path always supplies it.
    """
    return ZoneInfo(CLOCK_TIMEZONE) if CLOCK_TIMEZONE else None

# Shown until the first real status snapshot arrives, so the board is never blank.
PLACEHOLDER_STATUS = "State: idle\nActive: - (-)\n"


# --------------------------------------------------------------------------- #
# I2C transport                                                                 #
# --------------------------------------------------------------------------- #


class RawI2C:
    def __init__(self, bus: int = I2C_BUS, addr: int = I2C_ADDR) -> None:
        self.fd = os.open(f"/dev/i2c-{bus}", os.O_RDWR)
        fcntl.ioctl(self.fd, I2C_SLAVE, addr)

    def write_cmd(self, *cmds: int) -> None:
        os.write(self.fd, bytes([0x00] + list(cmds)))

    def write_data(self, data: bytes) -> None:
        # 128-byte chunks carry their own control byte: the kernel can split a
        # larger write and drop 0x40 mid-stream, which corrupts the frame.
        chunk = 128
        payload = bytes(data)
        for start in range(0, len(payload), chunk):
            os.write(self.fd, b"\x40" + payload[start:start + chunk])

    def close(self) -> None:
        os.close(self.fd)


class SSD1306:
    WIDTH, HEIGHT = WIDTH, HEIGHT

    def __init__(self, bus: int = I2C_BUS) -> None:
        self.i2c = RawI2C(bus=bus)
        self._init()

    def _cmd(self, *cmds: int) -> None:
        self.i2c.write_cmd(*cmds)

    def _init(self) -> None:
        self._cmd(0xAE)
        self._cmd(0xD5, 0x80)
        self._cmd(0xA8, 0x3F)
        self._cmd(0xD3, 0x00)
        self._cmd(0x40)
        self._cmd(0x8D, 0x14)
        self._cmd(0x20, 0x00)  # horizontal addressing; page advances itself
        self._cmd(0xA1)
        self._cmd(0xC8)
        self._cmd(0xDA, 0x12)
        self._cmd(0x81, 0xFF)
        self._cmd(0xD9, 0xF1)
        self._cmd(0xDB, 0x40)
        self._cmd(0xA4)
        self._cmd(0xA6)
        self._cmd(0xAF)

    def show_image(self, img: Image.Image) -> None:
        if img.size != (WIDTH, HEIGHT):
            img = img.resize((WIDTH, HEIGHT))
        if img.mode != "1":
            img = img.convert("1")
        buf = bytearray(WIDTH * 8)
        pixels = img.load()
        for page in range(8):
            for x in range(WIDTH):
                byte = 0
                for bit in range(8):
                    if pixels[x, page * 8 + bit]:
                        byte |= 1 << bit
                buf[x + page * WIDTH] = byte
        self._cmd(0x21, 0, WIDTH - 1)
        self._cmd(0x22, 0, 7)
        self.i2c.write_data(buf)

    def close(self) -> None:
        self.i2c.close()


# --------------------------------------------------------------------------- #
# /status parsing                                                              #
# --------------------------------------------------------------------------- #
#
# render_llm_status() labels are literal and pinned by tests in
# tests/test_llm_usage.py, so these patterns track a real contract rather than
# a guess. Everything is optional: the board degrades instead of breaking.

_RE_STATE = re.compile(r"^State:\s*(.+?)\s*$", re.M)
_RE_ACTIVE = re.compile(r"^Active:\s*(\S+)\s*\(([^)]*)\)", re.M)
_RE_CTX = re.compile(
    r"Last request context:\s*([\d,]+)\s*/\s*([\d,]+)\s*tokens\s*\(([\d.]+)%\)",
    re.M,
)
_RE_CACHE = re.compile(r"Prompt cache token ratio:\s*([\d.]+)%", re.M)
_RE_REQUESTS = re.compile(
    r"Logical requests:\s*(\d+)\s*successful,\s*(\d+)\s*failed",
    re.M,
)
# Not part of /status; supplied alongside it when the caller knows them.
_RE_THINK = re.compile(r"^Think:\s*(.+?)\s*$", re.M)
_RE_NEXT = re.compile(r"^Next:\s*(.+?)\s*$", re.M)


def _short_tokens(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.0f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def _activity_label(raw: str) -> str:
    word = str(raw or "").split("(")[0].strip()
    return word.replace("_", " ").upper() or "UNKNOWN"


def parse_status(text: str, *, think_level: str = "", next_due: str = "") -> dict[str, Any]:
    """Extract the handful of fields the board shows from /status output."""

    body = str(text or "")
    out: dict[str, Any] = {
        "activity": "",
        "model": "",
        "think": str(think_level or "").strip(),
        "ctx": "",
        "cache": "",
        "requests": "",
        "next": str(next_due or "").strip(),
    }

    match = _RE_STATE.search(body)
    if match:
        out["activity"] = _activity_label(match.group(1))

    match = _RE_ACTIVE.search(body)
    if match:
        # The endpoint id is the longer of the two; the model id reads better
        # on a 21-column panel (“deepseek-flash”, not “deepseek-v4.1-flash”).
        endpoint = match.group(1).strip()
        model = match.group(2).strip()
        out["model"] = model or endpoint

    match = _RE_CTX.search(body)
    if match:
        used = int(match.group(1).replace(",", ""))
        total = int(match.group(2).replace(",", ""))
        percent = match.group(3)
        out["ctx"] = f"{_short_tokens(used)}/{_short_tokens(total)} {float(percent):.0f}%"

    match = _RE_CACHE.search(body)
    if match:
        out["cache"] = f"{float(match.group(1)):.0f}%"

    match = _RE_REQUESTS.search(body)
    if match:
        out["requests"] = match.group(1)

    if not out["think"]:
        match = _RE_THINK.search(body)
        if match:
            out["think"] = match.group(1).strip()
    if not out["next"]:
        match = _RE_NEXT.search(body)
        if match:
            out["next"] = match.group(1).strip()

    return out


# --------------------------------------------------------------------------- #
# Weather (this process's own refresh loop, same source as the morning push)   #
# --------------------------------------------------------------------------- #


def fetch_weather() -> dict[str, Any]:
    """Fetch current Chengdu weather. Never raises; callers keep the last good."""
    request = Request(WEATHER_URL, headers={"User-Agent": "pal-oled-status"})
    with urlopen(request, timeout=WEATHER_TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read().decode("utf-8", "replace"))
    current = (payload.get("current_condition") or [{}])[0]
    today = (payload.get("weather") or [{}])[0]
    desc = ""
    blocks = current.get("weatherDesc") or []
    if blocks:
        desc = str(blocks[0].get("value") or "").strip()
    return {
        "temp": str(current.get("temp_C") or "").strip(),
        "description": desc[:LINE_CHARS],
        "low": str(today.get("mintempC") or "").strip(),
        "high": str(today.get("maxtempC") or "").strip(),
        "fetched_at": time.time(),
    }


# --------------------------------------------------------------------------- #
# Rendering                                                                    #
# --------------------------------------------------------------------------- #


class Board:
    """Draws both screens. Pure PIL — no hardware, so it renders offline too."""

    def __init__(self) -> None:
        self.small = ImageFont.truetype(FONT_PATH, FONT_SMALL)
        self.medium = ImageFont.truetype(FONT_PATH, FONT_MEDIUM)
        self._line_h_small = self.small.getbbox("M")[3] + 2
        self._line_h_medium = self.medium.getbbox("M")[3] + 2

    @staticmethod
    def _blank() -> Image.Image:
        return Image.new("1", (WIDTH, HEIGHT), 0)

    @staticmethod
    def _center_x(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> int:
        width = draw.textlength(text, font=font)
        return max(0, int((WIDTH - width) / 2))

    def render_status(self, status: dict[str, Any], now: datetime) -> Image.Image:
        img = self._blank()
        draw = ImageDraw.Draw(img)

        activity = str(status.get("activity") or "IDLE")[:11]
        clock = now.strftime("%H:%M")
        head = f"\u25cf {activity}"
        draw.text((2, 2), head, font=self.small, fill=1)
        clock_x = WIDTH - 2 - int(draw.textlength(clock, font=self.small))
        draw.text((clock_x, 2), clock, font=self.small, fill=1)

        model = str(status.get("model") or "-")
        think = str(status.get("think") or "").strip()
        model_line = f"{model} {think}".strip() if think else model

        rows = [
            model_line[:LINE_CHARS],
            f"ctx   {status.get('ctx') or '-'}",
            f"cache {status.get('cache') or '-'}  req {status.get('requests') or '-'}",
            f"next  {status.get('next') or '-'}"[:LINE_CHARS],
        ]

        y = 2 + self._line_h_small
        for row in rows:
            draw.text((2, y), row, font=self.small, fill=1)
            y += self._line_h_small
        return img

    def render_clock(self, now: datetime, weather: dict[str, Any]) -> Image.Image:
        img = self._blank()
        draw = ImageDraw.Draw(img)
        y = 2

        clock = now.strftime("%H:%M:%S")
        draw.text((self._center_x(draw, clock, self.medium), y), clock, font=self.medium, fill=1)
        y += self._line_h_medium

        date_line = now.strftime("%a %m-%d")
        draw.text((self._center_x(draw, date_line, self.small), y), date_line, font=self.small, fill=1)
        y += self._line_h_small

        low, high = str(weather.get("low") or ""), str(weather.get("high") or "")
        temp = str(weather.get("temp") or "")
        city_line = f"{CITY} {low}-{high}{DEGREE}" if low and high else f"{CITY} {temp}{DEGREE}".strip()
        draw.text((self._center_x(draw, city_line, self.small), y), city_line, font=self.small, fill=1)
        y += self._line_h_small

        desc = str(weather.get("description") or "weather unavailable")[:LINE_CHARS]
        draw.text((self._center_x(draw, desc, self.small), y), desc, font=self.small, fill=1)
        return img


# --------------------------------------------------------------------------- #
# Sidecar                                                                      #
# --------------------------------------------------------------------------- #


class OledStatusSidecar:
    def __init__(self, *, dry_run: bool = False) -> None:
        self._stop = False
        self._dry_run = dry_run
        self._board = Board()
        self._panel: SSD1306 | None = None
        self._server: socket.socket | None = None
        self._status = parse_status(PLACEHOLDER_STATUS)
        self._weather: dict[str, Any] = {}
        self._weather_error = ""
        self._weather_fetched_at = 0.0
        self._screen = "status"  # or "clock"
        self._screen_since = time.monotonic()
        self._frames = 0
        self._last_error = ""

    # ---- plumbing ----------------------------------------------------
    def _log(self, message: str) -> None:
        line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}"
        print(line, flush=True)
        if self._dry_run:
            return
        try:
            with LOG_PATH.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except Exception:
            pass

    def _clear_stale(self) -> None:
        if PID_PATH.exists():
            try:
                old = int(PID_PATH.read_text().strip())
                if old != os.getpid() and old > 0:
                    os.kill(old, 15)
                    time.sleep(0.3)
            except (ValueError, ProcessLookupError, PermissionError):
                pass
        if SOCKET_PATH.exists():
            try:
                SOCKET_PATH.unlink()
            except Exception:
                pass

    # ---- state -------------------------------------------------------
    def apply_fields(self, fields: dict[str, Any]) -> None:
        """Merge a structured snapshot from the plugin (the primary path)."""
        for key in ("activity", "model", "think", "ctx", "cache", "requests", "next"):
            if key in fields:
                self._status[key] = str(fields.get(key) or "")
        self._log(
            "status updated: "
            f"activity={self._status.get('activity')!r} "
            f"model={self._status.get('model')!r} "
            f"ctx={self._status.get('ctx')!r} "
            f"cache={self._status.get('cache')!r}"
        )

    def apply_status(self, text: str, *, think_level: str = "", next_due: str = "") -> None:
        self._status = parse_status(text, think_level=think_level, next_due=next_due)
        self._log(
            "status updated: "
            f"activity={self._status.get('activity')!r} "
            f"model={self._status.get('model')!r} "
            f"ctx={self._status.get('ctx')!r} "
            f"cache={self._status.get('cache')!r}"
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "screen": self._screen,
            "frames": self._frames,
            "clock_timezone": CLOCK_TIMEZONE,
            "status": dict(self._status),
            "weather": dict(self._weather),
            "weather_error": self._weather_error,
            "weather_age_seconds": (
                round(time.time() - self._weather_fetched_at, 1)
                if self._weather_fetched_at
                else None
            ),
            "last_error": self._last_error,
        }

    # ---- weather -----------------------------------------------------
    def _weather_loop(self) -> None:
        while not self._stop:
            try:
                self._weather = fetch_weather()
                self._weather_fetched_at = time.time()
                self._weather_error = ""
                self._log(f"weather updated: {self._weather}")
            except Exception as exc:  # keep the last good reading on screen
                self._weather_error = f"{type(exc).__name__}: {exc}"
                self._log(f"weather fetch failed: {self._weather_error}")
            deadline = time.monotonic() + WEATHER_REFRESH_SECONDS
            while not self._stop and time.monotonic() < deadline:
                time.sleep(1.0)

    # ---- rendering ---------------------------------------------------
    def current_image(self) -> Image.Image:
        now = datetime.now(_clock_zone())
        if self._screen == "clock":
            return self._board.render_clock(now, self._weather)
        return self._board.render_status(self._status, now)

    def _advance_screen(self) -> None:
        held = time.monotonic() - self._screen_since
        limit = CLOCK_SECONDS if self._screen == "clock" else STATUS_SECONDS
        if held >= limit:
            self._screen = "status" if self._screen == "clock" else "clock"
            self._screen_since = time.monotonic()

    def _push(self) -> None:
        img = self.current_image()
        if self._panel is None:
            return
        try:
            self._panel.show_image(img)
            self._frames += 1
            self._last_error = ""
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            self._log(f"frame failed: {self._last_error}")
            time.sleep(0.5)

    # ---- socket ------------------------------------------------------
    def _open_socket(self) -> None:
        SIDECAR_DIR.mkdir(parents=True, exist_ok=True)
        self._clear_stale()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(SOCKET_PATH))
        server.listen(4)
        server.setblocking(False)
        self._server = server
        self._log(f"socket listening: {SOCKET_PATH}")

    def _handle_payload(self, raw: str) -> str:
        text = str(raw or "").strip()
        if not text:
            return "ERR: empty\n"
        if text in ("quit", "exit", "stop"):
            self._stop = True
            return "OK: shutting down\n"
        if text == "status":
            return json.dumps(self.snapshot()) + "\n"
        try:
            payload = json.loads(text)
        except ValueError:
            # Bare text is accepted as /status output: the delivery path may
            # hand us the rendered message with no envelope at all.
            self.apply_status(text)
            return "OK\n"
        if not isinstance(payload, dict):
            return "ERR: payload must be an object\n"
        kind = str(payload.get("kind") or "status")
        if kind == "quit":
            self._stop = True
            return "OK: shutting down\n"
        if kind == "snapshot":
            return json.dumps(self.snapshot()) + "\n"
        if kind != "status":
            return f"ERR: unknown kind {kind!r}\n"
        fields = payload.get("status")
        if isinstance(fields, dict):
            self.apply_fields(fields)
            return "OK\n"
        self.apply_status(
            str(payload.get("text") or ""),
            think_level=str(payload.get("think_level") or ""),
            next_due=str(payload.get("next_due") or ""),
        )
        return "OK\n"

    def _drain_socket(self) -> None:
        while True:
            try:
                conn, _ = self._server.accept()
            except BlockingIOError:
                return
            except Exception as exc:
                self._log(f"accept failed: {exc}")
                return
            try:
                conn.settimeout(0.4)
                data = conn.recv(65536).decode("utf-8", "replace")
                response = self._handle_payload(data)
                conn.sendall(response.encode())
            except socket.timeout:
                pass
            except Exception as exc:
                self._log(f"command failed: {exc}")
            finally:
                conn.close()

    # ---- lifecycle ---------------------------------------------------
    def run(self) -> None:
        self._log(f"starting oled_status sidecar (pid {os.getpid()})")
        if _clock_zone() is None:
            self._log(
                "WARNING: no --timezone given; the clock falls back to host local "
                "time, which on this host is Europe/London"
            )
        if not self._dry_run:
            SIDECAR_DIR.mkdir(parents=True, exist_ok=True)
            PID_PATH.write_text(str(os.getpid()))
            self._panel = SSD1306()
            self._open_socket()
        fetcher = threading.Thread(target=self._weather_loop, daemon=True)
        fetcher.start()

        while not self._stop:
            self._drain_socket()
            if self._stop:
                break
            self._advance_screen()
            self._push()
            if self._dry_run and self._frames > 0:
                break
            # The clock screen keeps seconds moving; the status screen only
            # needs a repaint when something actually changed.
            time.sleep(0.2 if self._screen == "clock" else 0.5)

        if self._server is not None:
            try:
                self._server.close()
            except Exception:
                pass
        if not self._dry_run:
            for path in (SOCKET_PATH, PID_PATH):
                try:
                    path.unlink()
                except Exception:
                    pass
            if self._panel is not None:
                try:
                    self._panel.close()
                except Exception:
                    pass
        self._log(f"sidecar stopped after {self._frames} frames")


def main() -> int:
    png_dir = _flag("--png-out")
    if png_dir:
        board = Board()
        sample = parse_status(
            "State: in_turn (processing a turn)\n"
            "Active turns: 1\n"
            "Queued messages: 0\n"
            "\n"
            "🤖 LLM status\n"
            "Active: deepseek-v4.1-flash (deepseek-flash)\n"
            "\n"
            "🧠 Context\n"
            "Last request context: 151,708 / 1,000,000 tokens (15.2%)\n"
            "\n"
            "📡 Requests\n"
            "Logical requests: 22 successful, 0 failed\n"
            "\n"
            "💾 Prompt cache\n"
            "Prompt cache token ratio: 91.8%\n",
            think_level="low",
            next_due="09:20 quiz",
        )
        weather = {
            "temp": "18",
            "description": "Patchy rain nearby",
            "low": "15",
            "high": "18",
        }
        now = datetime.now(_clock_zone())
        out = Path(png_dir)
        out.mkdir(parents=True, exist_ok=True)
        for name, image in (
            ("status", board.render_status(sample, now)),
            ("clock", board.render_clock(now, weather)),
        ):
            path = out / f"{name}.png"
            image.convert("L").resize((WIDTH * 4, HEIGHT * 4), Image.NEAREST).save(path)
            print(f"wrote {path} lit_bbox={image.getbbox()}")
        return 0

    sidecar = OledStatusSidecar()
    sidecar.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
