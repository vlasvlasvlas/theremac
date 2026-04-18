#!/usr/bin/env python3
"""Realtime accelerometer theremin for Apple Silicon Macs."""

from __future__ import annotations

import argparse
import curses
import math
import multiprocessing
import os
import signal
import struct
import sys
import time
from dataclasses import dataclass, field
from multiprocessing import shared_memory
from pathlib import Path
from threading import Lock

BREW_SITE_PACKAGES = Path("/opt/homebrew/lib/python3.11/site-packages")
if str(BREW_SITE_PACKAGES) not in sys.path:
    sys.path.insert(0, str(BREW_SITE_PACKAGES))

try:
    import numpy as np
    import sounddevice as sd
    from lib.bootstrap import require_root
    from lib.spu_sensor import SHM_LID_SIZE, SHM_SIZE, sensor_worker, shm_read_new, shm_snap_read
except ImportError as exc:
    raise SystemExit(
        "theremac.py requires the Homebrew-installed mac-hardware-toys runtime "
        f"and sounddevice dependencies: {exc}"
    )

SENSOR_RATE_HZ = 800.0
TAU = math.tau

COLOR_TITLE = 1
COLOR_PITCH = 2
COLOR_VOLUME = 3
COLOR_WARN = 4
COLOR_ACCENT = 5
COLOR_ACTIVE = 6
NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
WHITE_NOTE_CLASSES = {0, 2, 4, 5, 7, 9, 11}
BLACK_NOTE_CLASSES = {1, 3, 6, 8, 10}
NOTE_ALIASES = {
    "CB": 11,
    "C": 0,
    "C#": 1,
    "DB": 1,
    "D": 2,
    "D#": 3,
    "EB": 3,
    "E": 4,
    "FB": 4,
    "E#": 5,
    "F": 5,
    "F#": 6,
    "GB": 6,
    "G": 7,
    "G#": 8,
    "AB": 8,
    "A": 9,
    "A#": 10,
    "BB": 10,
    "B": 11,
    "B#": 0,
}
SCALES: dict[str, tuple[int, ...]] = {
    "continuous": (),
    "chromatic": tuple(range(12)),
    "major": (0, 2, 4, 5, 7, 9, 11),
    "minor": (0, 2, 3, 5, 7, 8, 10),
    "major-pentatonic": (0, 2, 4, 7, 9),
    "minor-pentatonic": (0, 3, 5, 7, 10),
    "blues": (0, 3, 5, 6, 7, 10),
    "dorian": (0, 2, 3, 5, 7, 9, 10),
}
DEFAULT_ROOT_OCTAVE = 4
PIANO_WHITE_MIDIS = tuple(midi for midi in range(12, 120) if midi % 12 in WHITE_NOTE_CLASSES)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def apply_deadzone(value: float, deadzone: float) -> float:
    if abs(value) <= deadzone:
        return 0.0
    return math.copysign(abs(value) - deadzone, value)


def map_exp(value: float, in_min: float, in_max: float, out_min: float, out_max: float) -> float:
    span = max(1e-9, in_max - in_min)
    norm = clamp((value - in_min) / span, 0.0, 1.0)
    return math.exp(math.log(out_min) + norm * (math.log(out_max) - math.log(out_min)))


def vector_to_angles_deg(x: float, y: float, z: float) -> tuple[float, float]:
    pitch = math.degrees(math.atan2(x, math.sqrt(y * y + z * z)))
    roll = math.degrees(math.atan2(y, math.sqrt(x * x + z * z)))
    return pitch, roll


def mean_vector(samples: list[tuple[float, float, float]]) -> tuple[float, float, float]:
    if not samples:
        raise ValueError("expected at least one accelerometer sample for calibration")
    sx = sy = sz = 0.0
    for x, y, z in samples:
        sx += x
        sy += y
        sz += z
    count = float(len(samples))
    return sx / count, sy / count, sz / count


def freq_to_note(freq: float) -> str:
    if freq <= 0:
        return "--"
    midi = int(round(69 + 12 * math.log2(freq / 440.0)))
    name = NOTE_NAMES[midi % 12]
    octave = midi // 12 - 1
    return f"{name}{octave}"


def freq_to_midi(freq: float) -> int | None:
    if freq <= 0:
        return None
    return int(round(69 + 12 * math.log2(freq / 440.0)))


def freq_to_midi_float(freq: float) -> float | None:
    if freq <= 0:
        return None
    return 69 + 12 * math.log2(freq / 440.0)


def parse_note_name(note: str) -> int:
    cleaned = note.strip().upper().replace("♯", "#").replace("♭", "B")
    if not cleaned:
        raise ValueError(f"invalid note: {note}")
    idx = 1
    if len(cleaned) >= 2 and cleaned[1] in {"#", "B"}:
        idx = 2
    head = cleaned[:idx]
    tail = cleaned[idx:]
    if head not in NOTE_ALIASES:
        raise ValueError(f"invalid note name: {note}")
    if tail == "":
        octave = DEFAULT_ROOT_OCTAVE
    else:
        try:
            octave = int(tail)
        except ValueError as exc:
            raise ValueError(f"invalid octave in note: {note}") from exc
    return (octave + 1) * 12 + NOTE_ALIASES[head]


def midi_to_note(midi: int) -> str:
    return f"{NOTE_NAMES[midi % 12]}{midi // 12 - 1}"


def midi_to_freq(midi: int) -> float:
    return 440.0 * (2.0 ** ((midi - 69) / 12.0))


def axis_label(axis: str) -> str:
    if axis == "roll":
        return "IZQ/DER"
    return "ADELANTE/ATRAS"


class ScaleMapper:
    def __init__(self, scale_name: str, root_note: str, span_steps: int) -> None:
        if scale_name not in SCALES:
            raise ValueError(f"unknown scale: {scale_name}")
        if scale_name == "continuous":
            raise ValueError("continuous mode does not use ScaleMapper")
        self.scale_name = scale_name
        self.root_midi = parse_note_name(root_note)
        self.root_note = midi_to_note(self.root_midi)
        self.span_steps = max(1, int(span_steps))
        intervals = SCALES[scale_name]
        offsets = set()
        for octave in range(-16, 17):
            base = octave * 12
            for interval in intervals:
                offsets.add(base + interval)
        self.offsets = sorted(offsets)
        self.zero_index = self.offsets.index(0)

    def degree_to_midi(self, degree: int) -> int:
        idx = clamp(self.zero_index + degree, 0, len(self.offsets) - 1)
        return self.root_midi + self.offsets[int(idx)]

    def map_delta_to_freq(self, delta_deg: float, pitch_range_deg: float) -> tuple[float, str]:
        norm = clamp(delta_deg / max(1e-6, pitch_range_deg), -1.0, 1.0)
        degree = int(round(norm * self.span_steps))
        midi = self.degree_to_midi(degree)
        return midi_to_freq(midi), midi_to_note(midi)


def make_bar(width: int, value: float, low: float, high: float, fill: str = "█") -> str:
    if width <= 2:
        return ""
    norm = clamp((value - low) / max(1e-9, high - low), 0.0, 1.0)
    filled = int(round(norm * width))
    return fill * filled + " " * max(0, width - filled)


def make_meter(width: int, norm: float, fill: str = "#", empty: str = "-") -> str:
    if width <= 0:
        return ""
    norm = clamp(norm, 0.0, 1.0)
    filled = int(round(norm * width))
    return fill * filled + empty * max(0, width - filled)


def make_centered_meter(width: int, norm: float, fill: str = "#", empty: str = "-") -> str:
    if width < 3:
        return "|"[:width]

    cells = [empty] * width
    center = width // 2
    cells[center] = "|"
    norm = clamp(norm, -1.0, 1.0)

    if norm > 0:
        span = max(1, width - center - 1)
        filled = int(round(norm * span))
        for idx in range(center + 1, min(width, center + 1 + filled)):
            cells[idx] = fill
    elif norm < 0:
        span = max(1, center)
        filled = int(round(abs(norm) * span))
        start = max(0, center - filled)
        for idx in range(start, center):
            cells[idx] = fill

    return "".join(cells)


@dataclass
class SharedState:
    target_freq: float
    target_amp: float
    current_freq: float
    current_amp: float
    phase: float = 0.0
    last_pitch_deg: float = 0.0
    last_roll_deg: float = 0.0
    center_pitch_deg: float = 0.0
    center_roll_deg: float = 0.0
    last_freq: float = 0.0
    last_amp: float = 0.0
    current_cutoff_hz: float = 0.0
    target_cutoff_hz: float = 0.0
    current_resonance: float = 0.0
    target_resonance: float = 0.0
    last_lid_angle_deg: float = 0.0
    status: str = "booting"
    lock: Lock = field(default_factory=Lock)

    def set_targets(self, freq: float, amp: float) -> None:
        with self.lock:
            self.target_freq = freq
            self.target_amp = amp

    def get_targets(self) -> tuple[float, float]:
        with self.lock:
            return self.target_freq, self.target_amp

    def update_motion(self, pitch_deg: float, roll_deg: float, freq: float, amp: float) -> None:
        with self.lock:
            self.last_pitch_deg = pitch_deg
            self.last_roll_deg = roll_deg
            self.last_freq = freq
            self.last_amp = amp

    def set_filter_targets(self, cutoff_hz: float, resonance: float, lid_angle_deg: float) -> None:
        with self.lock:
            self.target_cutoff_hz = cutoff_hz
            self.target_resonance = resonance
            self.last_lid_angle_deg = lid_angle_deg

    def set_center(self, pitch_deg: float, roll_deg: float) -> None:
        with self.lock:
            self.center_pitch_deg = pitch_deg
            self.center_roll_deg = roll_deg

    def set_status(self, status: str) -> None:
        with self.lock:
            self.status = status

    def snapshot(self) -> dict[str, float | str]:
        with self.lock:
            return {
                "target_freq": self.target_freq,
                "target_amp": self.target_amp,
                "current_freq": self.current_freq,
                "current_amp": self.current_amp,
                "last_pitch_deg": self.last_pitch_deg,
                "last_roll_deg": self.last_roll_deg,
                "center_pitch_deg": self.center_pitch_deg,
                "center_roll_deg": self.center_roll_deg,
                "last_freq": self.last_freq,
                "last_amp": self.last_amp,
                "current_cutoff_hz": self.current_cutoff_hz,
                "target_cutoff_hz": self.target_cutoff_hz,
                "current_resonance": self.current_resonance,
                "target_resonance": self.target_resonance,
                "last_lid_angle_deg": self.last_lid_angle_deg,
                "status": self.status,
            }


class ThereminSynth:
    def __init__(
        self,
        state: SharedState,
        sample_rate: float,
        glide_ms: float,
        vibrato_rate_hz: float,
        vibrato_depth_cents: float,
        delay_ms: float,
        delay_feedback: float,
        delay_mix: float,
    ) -> None:
        self.state = state
        self.sample_rate = float(sample_rate)
        glide_seconds = max(glide_ms, 1.0) / 1000.0
        self.glide_coeff = 1.0 - math.exp(-1.0 / (glide_seconds * self.sample_rate))
        self.filter = ResonantLowPass(sample_rate=self.sample_rate)
        self.vibrato_rate_hz = max(0.0, float(vibrato_rate_hz))
        self.vibrato_depth_cents = max(0.0, float(vibrato_depth_cents))
        self.vibrato_phase = 0.0
        self.delay = DelayEffect(
            sample_rate=self.sample_rate,
            delay_ms=delay_ms,
            feedback=delay_feedback,
            mix=delay_mix,
        )

    def callback(self, outdata: np.ndarray, frames: int, _time: object, status: sd.CallbackFlags) -> None:
        if status:
            self.state.set_status(f"audio callback warning: {status}")

        target_freq, target_amp = self.state.get_targets()
        freq = self.state.current_freq
        amp = self.state.current_amp
        phase = self.state.phase
        cutoff = self.state.current_cutoff_hz
        resonance = self.state.current_resonance
        vibrato_phase = self.vibrato_phase
        column = outdata[:, 0]

        for idx in range(frames):
            freq += (target_freq - freq) * self.glide_coeff
            amp += (target_amp - amp) * self.glide_coeff
            target_cutoff = self.state.target_cutoff_hz
            target_resonance = self.state.target_resonance
            cutoff += (target_cutoff - cutoff) * self.glide_coeff
            resonance += (target_resonance - resonance) * self.glide_coeff
            inst_freq = freq
            if self.vibrato_depth_cents > 0.0 and self.vibrato_rate_hz > 0.0:
                cents = math.sin(vibrato_phase) * self.vibrato_depth_cents
                inst_freq *= 2.0 ** (cents / 1200.0)
                vibrato_phase += TAU * self.vibrato_rate_hz / self.sample_rate
                if vibrato_phase >= TAU:
                    vibrato_phase -= TAU * int(vibrato_phase / TAU)
            phase += TAU * inst_freq / self.sample_rate
            if phase >= TAU:
                phase -= TAU * int(phase / TAU)
            sample = math.sin(phase) * amp
            sample = self.filter.process(sample, cutoff_hz=cutoff, resonance=resonance)
            column[idx] = self.delay.process(sample)

        self.state.current_freq = freq
        self.state.current_amp = amp
        self.state.current_cutoff_hz = cutoff
        self.state.current_resonance = resonance
        self.state.phase = phase
        self.vibrato_phase = vibrato_phase


class ResonantLowPass:
    def __init__(self, sample_rate: float) -> None:
        self.sample_rate = float(sample_rate)
        self.low = 0.0
        self.band = 0.0

    def process(self, sample: float, cutoff_hz: float, resonance: float) -> float:
        cutoff_hz = clamp(cutoff_hz, 20.0, min(18_000.0, self.sample_rate * 0.45))
        resonance = clamp(resonance, 0.1, 40.0)
        f = 2.0 * math.sin(math.pi * cutoff_hz / self.sample_rate)
        damping = min(1.98, 1.0 / resonance)
        high = sample - self.low - damping * self.band
        self.band += f * high
        self.low += f * self.band
        return self.low


class DelayEffect:
    def __init__(self, sample_rate: float, delay_ms: float, feedback: float, mix: float) -> None:
        self.sample_rate = float(sample_rate)
        self.delay_ms = max(0.0, float(delay_ms))
        self.feedback = clamp(float(feedback), 0.0, 0.98)
        self.mix = clamp(float(mix), 0.0, 1.0)
        self.enabled = self.delay_ms > 0.0 and self.mix > 0.0
        self.write_idx = 0

        if self.enabled:
            delay_samples = max(1, int(round(self.sample_rate * self.delay_ms / 1000.0)))
            self.buffer = np.zeros(delay_samples, dtype=np.float32)
        else:
            self.buffer = np.zeros(1, dtype=np.float32)

    def process(self, sample: float) -> float:
        if not self.enabled:
            return sample

        delayed = float(self.buffer[self.write_idx])
        self.buffer[self.write_idx] = sample + delayed * self.feedback
        self.write_idx += 1
        if self.write_idx >= len(self.buffer):
            self.write_idx = 0

        dry = 1.0 - self.mix
        return sample * dry + delayed * self.mix


class ThereminUI:
    def __init__(self, state: SharedState, args: argparse.Namespace) -> None:
        self.state = state
        self.args = args
        self.enabled = False
        self.screen: curses.window | None = None
        self.last_draw = 0.0
        self.show_details = False
        self.recenter_requested = False

    def start(self) -> None:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            return
        if os.environ.get("TERM", "dumb") == "dumb":
            return
        self.screen = curses.initscr()
        self.enabled = True
        curses.noecho()
        curses.cbreak()
        curses.curs_set(0)
        self.screen.keypad(True)
        self.screen.nodelay(True)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(COLOR_TITLE, curses.COLOR_CYAN, -1)
            curses.init_pair(COLOR_PITCH, curses.COLOR_MAGENTA, -1)
            curses.init_pair(COLOR_VOLUME, curses.COLOR_GREEN, -1)
            curses.init_pair(COLOR_WARN, curses.COLOR_YELLOW, -1)
            curses.init_pair(COLOR_ACCENT, curses.COLOR_BLUE, -1)
            curses.init_pair(COLOR_ACTIVE, curses.COLOR_WHITE, curses.COLOR_MAGENTA)
    def stop(self) -> None:
        if not self.enabled or self.screen is None:
            return
        try:
            self.screen.keypad(False)
            curses.nocbreak()
            curses.echo()
            curses.endwin()
        finally:
            self.enabled = False
            self.screen = None

    def poll_quit(self, running: bool) -> bool:
        if not self.enabled or self.screen is None:
            return running
        while True:
            try:
                key = self.screen.getch()
            except curses.error:
                return running
            if key == -1:
                return running
            if key in (ord("q"), ord("Q")):
                return False
            if key in (ord("d"), ord("D")):
                self.show_details = not self.show_details
                self.last_draw = 0.0
            if key in (ord("c"), ord("C")):
                self.recenter_requested = True
                self.last_draw = 0.0
        return running

    def consume_recenter_request(self) -> bool:
        requested = self.recenter_requested
        self.recenter_requested = False
        return requested

    def _add_line(self, row: int, text: str, attr: int) -> None:
        if self.screen is None:
            return
        height, width = self.screen.getmaxyx()
        if row < 0 or row >= height:
            return
        try:
            self.screen.addnstr(row, 0, text, max(0, width - 1), attr)
        except curses.error:
            pass

    def _add_at(self, row: int, col: int, text: str, attr: int) -> None:
        if self.screen is None:
            return
        height, width = self.screen.getmaxyx()
        if row < 0 or row >= height or col >= width:
            return
        start_col = max(0, col)
        start_idx = max(0, -col)
        if start_idx >= len(text):
            return
        try:
            self.screen.addnstr(row, start_col, text[start_idx:], max(0, width - start_col - 1), attr)
        except curses.error:
            pass

    def _chip_line(self, items: list[str]) -> str:
        return " ".join(f"[{item}]" for item in items if item)

    def _fit_text(self, text: str, width: int) -> str:
        if width <= 0:
            return ""
        if len(text) <= width:
            return text
        if width <= 3:
            return text[:width]
        return text[: width - 3] + "..."

    def _join_columns(self, left: str, right: str, width: int) -> str:
        if width < 20:
            return self._fit_text(left, width)
        gap = 3
        left_width = max(1, (width - gap) // 2)
        right_width = max(1, width - gap - left_width)
        return f"{self._fit_text(left, left_width):<{left_width}}{' ' * gap}{self._fit_text(right, right_width)}"

    def _center_midi(self) -> int:
        if self.args.scale != "continuous":
            try:
                return parse_note_name(self.args.root_note)
            except ValueError:
                pass
        fallback = freq_to_midi(math.sqrt(self.args.min_hz * self.args.max_hz))
        return 60 if fallback is None else fallback

    def _keyboard_layout(
        self, width: int, center_midi: int, current_midi: int | None, current_midi_float: float | None
    ) -> tuple[list[str], dict[int, list[tuple[int, int, str]]]]:
        if width < 48:
            return [], {}

        white_cell = 7 if width >= 110 else 6
        step = white_cell - 1
        visible_white = max(7, min(22, (width - 1) // step))
        center_idx = min(
            range(len(PIANO_WHITE_MIDIS)),
            key=lambda idx: abs(PIANO_WHITE_MIDIS[idx] - center_midi),
        )
        start_idx = max(0, min(len(PIANO_WHITE_MIDIS) - visible_white, center_idx - visible_white // 2))
        visible_midis = PIANO_WHITE_MIDIS[start_idx : start_idx + visible_white]
        keyboard_width = len(visible_midis) * step + 1
        left_pad = max(0, (width - keyboard_width) // 2)
        rows = [[" "] * keyboard_width for _ in range(7)]
        note_positions: dict[int, int] = {}
        key_spans: dict[int, list[tuple[int, int, str]]] = {}

        def paint(row_idx: int, start: int, text: str) -> None:
            if row_idx < 0 or row_idx >= len(rows):
                return
            for offset, ch in enumerate(text):
                pos = start + offset
                if 0 <= pos < keyboard_width:
                    rows[row_idx][pos] = ch

        for idx, midi in enumerate(visible_midis):
            x = idx * step
            note_positions[midi] = x + (white_cell // 2)
            label = midi_to_note(midi)
            top_fill = " " * (white_cell - 2)
            white_top = "|" + top_fill + "|"
            white_mid = "|" + label.center(white_cell - 2)[: white_cell - 2] + "|"
            white_bot = "+" + "-" * (white_cell - 2) + "+"
            paint(4, x, white_top)
            paint(5, x, white_mid)
            paint(6, x, white_bot)
            key_spans[midi] = [
                (4, left_pad + x, white_top),
                (5, left_pad + x, white_mid),
                (6, left_pad + x, white_bot),
            ]

            if midi % 12 in {4, 11}:
                continue

            black_midi = midi + 1
            black_width = 5
            black_x = x + step - (black_width // 2)
            note_positions[black_midi] = black_x + (black_width // 2)
            black_top = " " + "_" * (black_width - 2) + " "
            black_mid = "|" + "#" * (black_width - 2) + "|"
            paint(2, black_x, black_top)
            paint(3, black_x, black_mid)
            active_black = "|" + midi_to_note(black_midi).center(black_width - 2)[: black_width - 2] + "|"
            key_spans[black_midi] = [
                (2, left_pad + black_x, black_top),
                (3, left_pad + black_x, active_black),
            ]

        center_x = note_positions.get(center_midi, keyboard_width // 2)

        def midi_to_keyboard_x(midi_value: float | None) -> int:
            if midi_value is None or not note_positions:
                return center_x

            semitone = int(math.floor(midi_value))
            frac = midi_value - semitone
            if frac < 1e-6 and semitone in note_positions:
                return note_positions[semitone]

            if semitone in note_positions and (semitone + 1) in note_positions:
                left_x = note_positions[semitone]
                right_x = note_positions[semitone + 1]
                return int(round(left_x + (right_x - left_x) * frac))

            ordered = sorted(note_positions.items())
            if midi_value <= ordered[0][0]:
                return ordered[0][1]
            if midi_value >= ordered[-1][0]:
                return ordered[-1][1]

            for idx in range(len(ordered) - 1):
                left_midi, left_x = ordered[idx]
                right_midi, right_x = ordered[idx + 1]
                if left_midi <= midi_value <= right_midi:
                    span = max(1e-6, right_midi - left_midi)
                    local_frac = (midi_value - left_midi) / span
                    return int(round(left_x + (right_x - left_x) * local_frac))

            return center_x

        pointer_x = midi_to_keyboard_x(current_midi_float)
        pointer_x = int(clamp(pointer_x, 0, max(0, keyboard_width - 1)))
        paint(0, 0, "-" * keyboard_width)
        paint(1, 0, " " * keyboard_width)
        if 0 <= center_x < keyboard_width:
            rows[0][center_x] = "|"
        if 0 <= pointer_x < keyboard_width:
            rows[1][pointer_x] = "v"

        padded_rows = [(" " * left_pad) + "".join(row) for row in rows]
        return padded_rows, key_spans

    def draw(self, force: bool = False) -> None:
        if not self.enabled or self.screen is None:
            return
        now = time.monotonic()
        if not force and (now - self.last_draw) < (1.0 / max(1.0, self.args.ui_fps)):
            return
        self.last_draw = now

        snapshot = self.state.snapshot()
        screen = self.screen
        height, width = screen.getmaxyx()
        screen.erase()

        pitch = float(snapshot["last_pitch_deg"])
        roll = float(snapshot["last_roll_deg"])
        center_pitch = float(snapshot["center_pitch_deg"])
        center_roll = float(snapshot["center_roll_deg"])
        freq = float(snapshot["last_freq"])
        amp = float(snapshot["last_amp"])
        current_freq = float(snapshot["current_freq"])
        current_amp = float(snapshot["current_amp"])
        current_cutoff = float(snapshot["current_cutoff_hz"])
        target_resonance = float(snapshot["target_resonance"])
        lid_angle = float(snapshot["last_lid_angle_deg"])
        status = str(snapshot["status"])
        audible_freq = current_freq if current_freq > 0 else freq
        note = freq_to_note(audible_freq)
        target_note = freq_to_note(freq)
        current_midi = freq_to_midi(audible_freq)
        current_midi_float = freq_to_midi_float(audible_freq)
        center_midi = self._center_midi()
        pitch_delta = pitch - center_pitch
        roll_delta = roll - center_roll
        pitch_norm = clamp(pitch_delta / max(1e-6, self.args.pitch_range_deg), -1.0, 1.0)
        roll_norm = clamp(roll_delta / max(1e-6, self.args.volume_range_deg), -1.0, 1.0)
        level_norm = clamp(current_amp / max(1e-6, self.args.max_volume), 0.0, 1.0)
        filter_norm = clamp(
            (current_cutoff - self.args.filter_low_hz) / max(1e-6, self.args.filter_high_hz - self.args.filter_low_hz),
            0.0,
            1.0,
        )

        title_attr = curses.color_pair(COLOR_TITLE) | curses.A_BOLD if curses.has_colors() else curses.A_BOLD
        pitch_attr = curses.color_pair(COLOR_PITCH) if curses.has_colors() else curses.A_NORMAL
        volume_attr = curses.color_pair(COLOR_VOLUME) if curses.has_colors() else curses.A_NORMAL
        warn_attr = curses.color_pair(COLOR_WARN) if curses.has_colors() else curses.A_NORMAL
        accent_attr = curses.color_pair(COLOR_ACCENT) if curses.has_colors() else curses.A_NORMAL
        active_attr = curses.color_pair(COLOR_ACTIVE) | curses.A_BOLD if curses.has_colors() else curses.A_REVERSE | curses.A_BOLD
        status_attr = warn_attr if "warning" in status else accent_attr
        status_label = status.upper() if status == "live" else status
        header = "THEREMAC"
        if width > len(header) + len(status_label) + 2:
            header = f"{header}{' ' * (width - len(header) - len(status_label) - 1)}{status_label}"
        self._add_line(0, header, title_attr if status_attr == accent_attr else title_attr | curses.A_BOLD)

        if height < 14 or width < 56:
            meter_width = max(10, min(28, width - 24))
            self._add_line(2, f"PIANO {note:>4}   {audible_freq:7.1f} Hz   VOL {current_amp:0.3f}", accent_attr)
            self._add_line(4, f"PITCHd [{make_centered_meter(meter_width, pitch_norm)}] {pitch_delta:+6.1f} deg", pitch_attr)
            self._add_line(5, f"ROLLd  [{make_centered_meter(meter_width, roll_norm)}] {roll_delta:+6.1f} deg", pitch_attr)
            self._add_line(6, f"LEVEL  [{make_meter(meter_width, level_norm)}] {current_amp:0.3f}", volume_attr)
            footer = "c recalibrar   d detalles   q volver"
            self._add_line(height - 1, footer, status_attr)
            screen.refresh()
            return

        midi_label = "--" if current_midi is None else str(current_midi)
        hero_left = f"PIANO {note:>4}   MIDI {midi_label:>3}   {audible_freq:7.1f} Hz"
        hero_right = f"VOL {current_amp:0.3f}"
        self._add_line(2, self._join_columns(hero_left, hero_right, width), accent_attr)
        target_line = self._join_columns(
            f"OBJETIVO {target_note:>4}   {freq:7.1f} Hz",
            f"RANGO {self.args.min_hz:0.0f}-{self.args.max_hz:0.0f} Hz",
            width,
        )
        self._add_line(3, target_line, accent_attr)
        chips_row = 4

        chips = [
            f"P:{self.args.pitch_axis}",
            (
                f"V:{self.args.volume_mode}"
                if self.args.volume_mode == "fixed"
                else f"V:{self.args.volume_mode}:{self.args.volume_direction}"
            ),
            (
                f"S:{self.args.scale}"
                if self.args.scale == "continuous"
                else f"S:{self.args.scale}:{self.args.root_note}"
            ),
        ]
        if self.args.filter_source != "none":
            chips.append(f"F:{self.args.filter_source}")
        if self.args.vibrato_depth_cents > 0.0 and self.args.vibrato_rate_hz > 0.0:
            chips.append(f"VIB:{self.args.vibrato_rate_hz:0.1f}Hz")
        if self.args.delay_ms > 0.0 and self.args.delay_mix > 0.0:
            chips.append(f"DLY:{self.args.delay_ms:0.0f}ms")
        self._add_line(chips_row, self._fit_text(self._chip_line(chips), width), accent_attr)

        next_row = chips_row + 2
        keyboard_rows, key_spans = self._keyboard_layout(width, center_midi, current_midi, current_midi_float)
        for idx, line in enumerate(keyboard_rows):
            self._add_line(next_row + idx, line, pitch_attr)
        if current_midi is not None and current_midi in key_spans:
            for rel_row, col, text in key_spans[current_midi]:
                self._add_at(next_row + rel_row, col, text, active_attr)
        next_row += len(keyboard_rows) + (1 if keyboard_rows else 0)

        left_meter_width = max(10, min(28, ((width - 3) // 2) - 18))
        right_meter_width = max(10, min(28, ((width - 3) // 2) - 18))
        filter_text = (
            f"FILTER [{make_meter(right_meter_width, filter_norm)}] {current_cutoff:7.1f} Hz"
            if self.args.filter_source != "none"
            else f"TARGET {target_note:>4}   {freq:7.1f} Hz"
        )
        stat_lines = [
            (
                f"PITCHd [{make_centered_meter(left_meter_width, pitch_norm)}] {pitch_delta:+6.1f} deg",
                f"LEVEL  [{make_meter(right_meter_width, level_norm)}] {current_amp:0.3f}",
                pitch_attr,
                volume_attr,
            ),
            (
                f"ROLLd  [{make_centered_meter(left_meter_width, roll_norm)}] {roll_delta:+6.1f} deg",
                filter_text,
                pitch_attr,
                accent_attr,
            ),
            (
                f"CENTRO {midi_to_note(center_midi):>4}   {midi_to_freq(center_midi):7.1f} Hz",
                f"RANGO P {self.args.pitch_range_deg:0.1f} / V {self.args.volume_range_deg:0.1f} deg",
                accent_attr,
                accent_attr,
            ),
        ]
        if width >= 92:
            gap = 3
            left_width = max(1, (width - gap) // 2)
            right_width = max(1, width - gap - left_width)
            for left, right, left_attr, right_attr in stat_lines:
                self._add_at(next_row, 0, self._fit_text(left, left_width).ljust(left_width), left_attr)
                self._add_at(next_row, left_width + gap, self._fit_text(right, right_width), right_attr)
                next_row += 1
        else:
            for left, right, left_attr, right_attr in stat_lines:
                self._add_line(next_row, self._fit_text(left, width), left_attr)
                next_row += 1
                self._add_line(next_row, self._fit_text(right, width), right_attr)
                next_row += 1

        if self.show_details:
            detail_lines: list[tuple[str, int]] = [
                (
                    f"Abs pitch {pitch:+6.1f} deg   Abs roll {roll:+6.1f} deg",
                    accent_attr,
                ),
                (
                    f"Pitch axis {axis_label(self.args.pitch_axis)}   Volume axis {axis_label('roll' if self.args.volume_mode == 'roll' else 'pitch') if self.args.volume_mode in {'pitch', 'roll'} else 'FIJO'}",
                    accent_attr,
                ),
                (
                    f"Filter [{make_meter(right_meter_width, filter_norm)}] {current_cutoff:7.1f} Hz   Q {target_resonance:4.1f}   Lid {lid_angle:5.1f} deg",
                    accent_attr,
                ),
                (
                    f"Vibrato {self.args.vibrato_rate_hz:0.2f} Hz / {self.args.vibrato_depth_cents:0.1f} cents   "
                    f"Delay {self.args.delay_ms:0.1f} ms mix {self.args.delay_mix:0.2f} fb {self.args.delay_feedback:0.2f}",
                    accent_attr,
                ),
            ]
            for text, attr in detail_lines:
                if next_row >= height - 1:
                    break
                self._add_line(next_row, self._fit_text(text, width), attr)
                next_row += 1

        footer = "c recalibrar   d detalles" if not self.show_details else "c recalibrar   d simple"
        footer += "   q volver"
        self._add_line(height - 1, footer, status_attr)
        screen.refresh()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Play a realtime theremin controlled by Mac accelerometer tilt."
    )
    parser.add_argument("--sample-rate", type=float, default=48_000.0, help="Audio output sample rate.")
    parser.add_argument("--block-size", type=int, default=64, help="Audio callback block size in frames.")
    parser.add_argument("--min-hz", type=float, default=220.0, help="Minimum theremin pitch.")
    parser.add_argument("--max-hz", type=float, default=1760.0, help="Maximum theremin pitch.")
    parser.add_argument(
        "--scale",
        choices=tuple(SCALES.keys()),
        default="continuous",
        help="Pitch mapping mode. 'continuous' keeps the original theremin behavior.",
    )
    parser.add_argument(
        "--root-note",
        type=str,
        default="A3",
        help="Center note used when a quantized scale mode is active. Octave is optional; bare notes default to octave 4.",
    )
    parser.add_argument(
        "--scale-span-steps",
        type=int,
        default=10,
        help="How many scale degrees exist from center to each tilt extreme.",
    )
    parser.add_argument(
        "--pitch-axis",
        choices=("pitch", "roll"),
        default="pitch",
        help="Tilt axis used for note pitch.",
    )
    parser.add_argument(
        "--volume-mode",
        choices=("fixed", "roll", "pitch"),
        default="fixed",
        help="How output amplitude is controlled.",
    )
    parser.add_argument(
        "--volume-direction",
        choices=("both", "positive", "negative"),
        default="both",
        help="Whether tilt volume reacts on both sides or only one direction from center.",
    )
    parser.add_argument(
        "--volume-curve",
        type=float,
        default=1.0,
        help="Power curve for volume response. >1 softens near silence, <1 makes it more immediate.",
    )
    parser.add_argument("--fixed-volume", type=float, default=0.18, help="Amplitude when volume mode is fixed.")
    parser.add_argument("--max-volume", type=float, default=0.28, help="Maximum output amplitude.")
    parser.add_argument(
        "--pitch-range-deg",
        type=float,
        default=35.0,
        help="Degrees around the calibrated center mapped across min/max pitch.",
    )
    parser.add_argument(
        "--volume-range-deg",
        type=float,
        default=25.0,
        help="Degrees away from center needed to reach maximum amplitude in tilt volume modes.",
    )
    parser.add_argument("--pitch-deadzone-deg", type=float, default=1.5, help="Ignore tiny pitch motion near center.")
    parser.add_argument("--volume-deadzone-deg", type=float, default=2.0, help="Ignore tiny volume motion near center.")
    parser.add_argument(
        "--center-seconds",
        type=float,
        default=0.75,
        help="Calibration time while holding the Mac in the neutral pose.",
    )
    parser.add_argument(
        "--gravity-cutoff-hz",
        type=float,
        default=8.0,
        help="Low-pass cutoff used to estimate gravity from the accelerometer stream.",
    )
    parser.add_argument("--glide-ms", type=float, default=18.0, help="Pitch/amplitude smoothing in milliseconds.")
    parser.add_argument(
        "--filter-source",
        choices=("none", "lid"),
        default="none",
        help="Optional synth filter controller.",
    )
    parser.add_argument("--filter-low-hz", type=float, default=180.0, help="Lowest low-pass cutoff.")
    parser.add_argument("--filter-high-hz", type=float, default=4200.0, help="Highest low-pass cutoff.")
    parser.add_argument("--filter-resonance", type=float, default=12.0, help="Low-pass resonance/Q amount.")
    parser.add_argument("--lid-angle-min", type=float, default=15.0, help="Lid angle treated as filter minimum.")
    parser.add_argument("--lid-angle-max", type=float, default=120.0, help="Lid angle treated as filter maximum.")
    parser.add_argument("--vibrato-rate-hz", type=float, default=0.0, help="Vibrato LFO rate in Hz. 0 disables vibrato.")
    parser.add_argument(
        "--vibrato-depth-cents",
        type=float,
        default=0.0,
        help="Peak vibrato depth in cents. 0 disables vibrato even if rate is set.",
    )
    parser.add_argument("--delay-ms", type=float, default=0.0, help="Delay time in milliseconds. 0 disables delay.")
    parser.add_argument("--delay-feedback", type=float, default=0.35, help="Delay feedback amount from 0.0 to 0.98.")
    parser.add_argument("--delay-mix", type=float, default=0.0, help="Wet mix for delay from 0.0 to 1.0.")
    parser.add_argument(
        "--ui",
        choices=("auto", "always", "none"),
        default="auto",
        help="Fullscreen ASCII UI mode.",
    )
    parser.add_argument("--ui-fps", type=float, default=24.0, help="Refresh rate for the terminal UI.")
    parser.add_argument("--debug", action="store_true", help="Print live pitch/roll/freq diagnostics to stderr.")
    return parser.parse_args()


def calibrate_center(
    shm_buf: memoryview,
    center_seconds: float,
    state: SharedState | None = None,
    start_total: int = 0,
) -> tuple[float, float, tuple[float, float, float], int]:
    started = time.monotonic()
    last_total = start_total
    captured: list[tuple[float, float, float]] = []

    while time.monotonic() - started < center_seconds:
        samples, last_total = shm_read_new(shm_buf, last_total)
        if samples:
            captured.extend(samples)
            if state is not None:
                elapsed = time.monotonic() - started
                remain = max(0.0, center_seconds - elapsed)
                state.set_status(f"calibrating center... {remain:0.2f}s")
            continue
        time.sleep(0.001)

    if not captured:
        raise RuntimeError("no accelerometer samples received during calibration")

    baseline = mean_vector(captured)
    center_pitch, center_roll = vector_to_angles_deg(*baseline)
    return center_pitch, center_roll, baseline, last_total


def compute_volume(
    volume_mode: str,
    volume_direction: str,
    volume_curve: float,
    fixed_volume: float,
    max_volume: float,
    value_pitch: float,
    value_roll: float,
    center_pitch: float,
    center_roll: float,
    volume_range_deg: float,
    volume_deadzone_deg: float,
) -> float:
    if volume_mode == "fixed":
        return clamp(fixed_volume, 0.0, max_volume)

    if volume_mode == "pitch":
        delta = value_pitch - center_pitch
    else:
        delta = value_roll - center_roll

    if volume_direction == "both":
        directional = abs(delta)
    elif volume_direction == "negative":
        directional = max(0.0, -delta)
    else:
        directional = max(0.0, delta)

    directional = apply_deadzone(directional, volume_deadzone_deg)
    norm = clamp(directional / max(1e-6, volume_range_deg), 0.0, 1.0)
    shaped = norm ** max(1e-3, volume_curve)
    return shaped * max_volume


def should_use_ui(mode: str) -> bool:
    if mode == "none":
        return False
    if mode == "always":
        return True
    return sys.stdin.isatty() and sys.stdout.isatty()


def map_lid_to_cutoff(angle_deg: float, angle_min: float, angle_max: float, low_hz: float, high_hz: float) -> float:
    if angle_max <= angle_min:
        return low_hz
    clamped_angle = clamp(angle_deg, angle_min, angle_max)
    norm = (clamped_angle - angle_min) / (angle_max - angle_min)
    return map_exp(norm, 0.0, 1.0, low_hz, high_hz)


def main() -> int:
    args = parse_args()
    if args.sample_rate <= 0:
        raise SystemExit("--sample-rate must be > 0")
    if args.block_size <= 0:
        raise SystemExit("--block-size must be > 0")
    if args.min_hz <= 0 or args.max_hz <= args.min_hz:
        raise SystemExit("expected 0 < --min-hz < --max-hz")
    if args.pitch_range_deg <= 0 or args.volume_range_deg <= 0:
        raise SystemExit("--pitch-range-deg and --volume-range-deg must be > 0")
    if args.center_seconds <= 0:
        raise SystemExit("--center-seconds must be > 0")
    if args.gravity_cutoff_hz <= 0:
        raise SystemExit("--gravity-cutoff-hz must be > 0")
    if args.ui_fps <= 0:
        raise SystemExit("--ui-fps must be > 0")
    if args.scale_span_steps <= 0:
        raise SystemExit("--scale-span-steps must be > 0")
    if args.volume_curve <= 0:
        raise SystemExit("--volume-curve must be > 0")
    if args.filter_low_hz <= 0 or args.filter_high_hz <= args.filter_low_hz:
        raise SystemExit("expected 0 < --filter-low-hz < --filter-high-hz")
    if args.filter_resonance <= 0:
        raise SystemExit("--filter-resonance must be > 0")
    if args.lid_angle_max <= args.lid_angle_min:
        raise SystemExit("--lid-angle-max must be > --lid-angle-min")
    if args.vibrato_rate_hz < 0:
        raise SystemExit("--vibrato-rate-hz must be >= 0")
    if args.vibrato_depth_cents < 0:
        raise SystemExit("--vibrato-depth-cents must be >= 0")
    if args.delay_ms < 0:
        raise SystemExit("--delay-ms must be >= 0")
    if not 0 <= args.delay_feedback < 1:
        raise SystemExit("--delay-feedback must be in [0, 1)")
    if not 0 <= args.delay_mix <= 1:
        raise SystemExit("--delay-mix must be in [0, 1]")

    scale_mapper: ScaleMapper | None = None
    if args.scale != "continuous":
        try:
            scale_mapper = ScaleMapper(
                scale_name=args.scale,
                root_note=args.root_note,
                span_steps=args.scale_span_steps,
            )
        except ValueError as exc:
            raise SystemExit(str(exc))
        args.root_note = scale_mapper.root_note

    require_root(__file__)

    initial_freq = midi_to_freq(scale_mapper.root_midi) if scale_mapper is not None else math.sqrt(args.min_hz * args.max_hz)
    initial_amp = clamp(args.fixed_volume, 0.0, args.max_volume) if args.volume_mode == "fixed" else 0.0
    initial_cutoff = args.filter_high_hz
    initial_resonance = args.filter_resonance if args.filter_source != "none" else 0.7
    state = SharedState(
        target_freq=initial_freq,
        target_amp=initial_amp,
        current_freq=initial_freq,
        current_amp=initial_amp,
        last_freq=initial_freq,
        last_amp=initial_amp,
        current_cutoff_hz=initial_cutoff,
        target_cutoff_hz=initial_cutoff,
        current_resonance=initial_resonance,
        target_resonance=initial_resonance,
        status="booting",
    )

    ui = ThereminUI(state=state, args=args)
    if should_use_ui(args.ui):
        ui.start()

    shm = shared_memory.SharedMemory(create=True, size=SHM_SIZE)
    for idx in range(SHM_SIZE):
        shm.buf[idx] = 0
    lid_shm: shared_memory.SharedMemory | None = None
    lid_last_count = 0
    lid_angle = args.lid_angle_max
    if args.filter_source == "lid":
        lid_shm = shared_memory.SharedMemory(create=True, size=SHM_LID_SIZE)
        for idx in range(SHM_LID_SIZE):
            lid_shm.buf[idx] = 0

    worker: multiprocessing.Process | None = None
    running = True
    last_total = 0

    def stop(_sig: int, _frame: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    try:
        worker = multiprocessing.Process(
            target=sensor_worker,
            args=(shm.name, 0, 1, None, None, lid_shm.name if lid_shm is not None else None),
            daemon=True,
        )
        worker.start()

        if ui.enabled:
            state.set_status(f"calibrating center... hold still for {args.center_seconds:0.2f}s")
            ui.draw(force=True)
        else:
            print(
                f"Calibrating center for {args.center_seconds:.2f}s. Hold the Mac steady.",
                file=sys.stderr,
                flush=True,
            )

        center_pitch, center_roll, gravity, last_total = calibrate_center(
            shm.buf,
            args.center_seconds,
            state=state,
            start_total=last_total,
        )
        state.set_center(center_pitch, center_roll)
        state.set_status("live")

        if not ui.enabled:
            print(
                (
                    f"Center locked: pitch={center_pitch:+.1f}deg "
                    f"roll={center_roll:+.1f}deg. Press q in UI or Ctrl-C to stop."
                ),
                file=sys.stderr,
                flush=True,
            )

        synth = ThereminSynth(
            state=state,
            sample_rate=args.sample_rate,
            glide_ms=args.glide_ms,
            vibrato_rate_hz=args.vibrato_rate_hz,
            vibrato_depth_cents=args.vibrato_depth_cents,
            delay_ms=args.delay_ms,
            delay_feedback=args.delay_feedback,
            delay_mix=args.delay_mix,
        )
        alpha = 1.0 - math.exp(-TAU * args.gravity_cutoff_hz / SENSOR_RATE_HZ)
        debug_next = time.monotonic()

        with sd.OutputStream(
            channels=1,
            samplerate=args.sample_rate,
            dtype="float32",
            blocksize=args.block_size,
            latency="low",
            callback=synth.callback,
        ):
            while running:
                running = ui.poll_quit(running)
                if worker.exitcode is not None:
                    raise RuntimeError(f"accelerometer worker exited with code {worker.exitcode}")

                if ui.consume_recenter_request():
                    state.set_targets(freq=state.current_freq, amp=0.0)
                    state.set_status(f"recalibrating center... hold still for {args.center_seconds:0.2f}s")
                    ui.draw(force=True)
                    center_pitch, center_roll, gravity, last_total = calibrate_center(
                        shm.buf,
                        args.center_seconds,
                        state=state,
                        start_total=last_total,
                    )
                    state.set_center(center_pitch, center_roll)
                    state.set_status("live")
                    ui.draw(force=True)
                    continue

                samples, last_total = shm_read_new(shm.buf, last_total)
                if lid_shm is not None:
                    lid_payload, lid_last_count = shm_snap_read(lid_shm.buf, lid_last_count, 4)
                    if lid_payload is not None:
                        lid_angle = float(struct.unpack("<f", lid_payload)[0])
                        filter_cutoff = map_lid_to_cutoff(
                            angle_deg=lid_angle,
                            angle_min=args.lid_angle_min,
                            angle_max=args.lid_angle_max,
                            low_hz=args.filter_low_hz,
                            high_hz=args.filter_high_hz,
                        )
                        state.set_filter_targets(
                            cutoff_hz=filter_cutoff,
                            resonance=args.filter_resonance,
                            lid_angle_deg=lid_angle,
                        )
                if not samples:
                    ui.draw()
                    time.sleep(0.001)
                    continue

                for sx, sy, sz in samples:
                    gx, gy, gz = gravity
                    gx += (sx - gx) * alpha
                    gy += (sy - gy) * alpha
                    gz += (sz - gz) * alpha
                    gravity = (gx, gy, gz)

                    pitch_deg, roll_deg = vector_to_angles_deg(gx, gy, gz)

                    pitch_value = pitch_deg if args.pitch_axis == "pitch" else roll_deg
                    pitch_center = center_pitch if args.pitch_axis == "pitch" else center_roll
                    pitch_delta = apply_deadzone(pitch_value - pitch_center, args.pitch_deadzone_deg)
                    pitch_delta = clamp(pitch_delta, -args.pitch_range_deg, args.pitch_range_deg)

                    if scale_mapper is None:
                        freq = map_exp(
                            pitch_delta,
                            -args.pitch_range_deg,
                            args.pitch_range_deg,
                            args.min_hz,
                            args.max_hz,
                        )
                    else:
                        freq, _ = scale_mapper.map_delta_to_freq(
                            delta_deg=pitch_delta,
                            pitch_range_deg=args.pitch_range_deg,
                        )
                    amp = compute_volume(
                        volume_mode=args.volume_mode,
                        volume_direction=args.volume_direction,
                        volume_curve=args.volume_curve,
                        fixed_volume=args.fixed_volume,
                        max_volume=args.max_volume,
                        value_pitch=pitch_deg,
                        value_roll=roll_deg,
                        center_pitch=center_pitch,
                        center_roll=center_roll,
                        volume_range_deg=args.volume_range_deg,
                        volume_deadzone_deg=args.volume_deadzone_deg,
                    )
                    state.set_targets(freq=freq, amp=amp)
                    state.update_motion(pitch_deg=pitch_deg, roll_deg=roll_deg, freq=freq, amp=amp)
                    if lid_shm is None:
                        state.set_filter_targets(
                            cutoff_hz=args.filter_high_hz,
                            resonance=0.7,
                            lid_angle_deg=0.0,
                        )

                    if args.debug and time.monotonic() >= debug_next:
                        debug_next = time.monotonic() + 0.1
                        debug_cutoff = state.snapshot()["target_cutoff_hz"]
                        print(
                            (
                                f"pitch={pitch_deg:+6.2f}deg roll={roll_deg:+6.2f}deg "
                                f"freq={freq:7.1f}Hz amp={amp:.3f} "
                                f"lid={lid_angle:6.1f}deg cutoff={float(debug_cutoff):7.1f}"
                            ),
                            file=sys.stderr,
                            flush=True,
                        )

                ui.draw()

    finally:
        ui.stop()
        if worker is not None and worker.is_alive():
            worker.terminate()
            worker.join(timeout=1.0)
            if worker.is_alive():
                worker.kill()
                worker.join(timeout=1.0)
        if lid_shm is not None:
            lid_shm.close()
            try:
                lid_shm.unlink()
            except FileNotFoundError:
                pass
        shm.close()
        try:
            shm.unlink()
        except FileNotFoundError:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
