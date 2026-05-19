#!/usr/bin/env python3
"""
RealTimeTalk-daemon.py — OpenClaw RealTimeTalk daemon (Mac Mini / CoreAudio).

Audio flow:
  Mic → OpenAI Realtime API (VAD + STT only) → transcript
  transcript → OpenClaw gateway (chat.send / agent.wait) → Five's reply
  Five's reply → Edge TTS (primary) | macOS `say` (fallback) → speaker

Stop via:
  http://localhost:19000/dashboard         — local browser
  launchctl bootout gui/$UID/ai.openclaw.realtimetalk  — terminal
  SIGTERM / Ctrl-C

Usage:
  python3 RealTimeTalk-daemon.py [options]
  python3 RealTimeTalk-daemon.py --list-devices
  python3 RealTimeTalk-daemon.py --input-device 1 --output-device 2

Requires:
  brew install portaudio ffmpeg
  pip install "websockets>=12" sounddevice numpy zhconv
  Edge TTS skill at ~/.openclaw/workspace/skills/edge-tts/scripts/tts-converter.js
  OpenAI API key in openclaw.json at talk.providers.openai.apiKey
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import datetime
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np
import sounddevice as sd
import websockets

try:
    from zhconv import convert as _zh_convert   # traditional → simplified
except Exception:                                # pragma: no cover
    _zh_convert = None

# ── Constants ─────────────────────────────────────────────────────────────────

OPENCLAW_CONFIG   = os.path.expanduser("~/.openclaw/openclaw.json")
OPENCLAW_GW_URL   = "ws://127.0.0.1:18789"
OPENCLAW_SESSION  = "agent:main:main"

# Edge TTS skill — primary TTS engine on Mac (online, high quality)
EDGE_TTS_SCRIPT   = os.path.expanduser(
    "~/.openclaw/workspace/skills/edge-tts/scripts/tts-converter.js"
)
EDGE_VOICE_EN     = "en-US-AriaNeural"
EDGE_VOICE_ZH     = "zh-CN-XiaoxiaoNeural"
EDGE_TTS_TIMEOUT  = 8.0       # seconds — fall back to `say` if Edge takes longer
# macOS `say` — offline fallback. Voices are pre-installed on macOS.
SAY_VOICE_EN      = "Samantha"
SAY_VOICE_ZH      = "Tingting"
FFMPEG_CMD        = "/opt/homebrew/bin/ffmpeg"

OPENAI_TRANSCRIBE_MODEL = "gpt-4o-transcribe"
OPENAI_WS_URL     = "wss://api.openai.com/v1/realtime?intent=transcription"
SAMPLE_RATE       = 24000        # OpenAI Realtime API rate
DEVICE_RATE       = 24000        # capture at 24 kHz — CoreAudio resamples from native
RESAMPLE_RATIO    = 1            # no decimation needed — DEVICE_RATE == SAMPLE_RATE
CHANNELS          = 1
BLOCKSIZE         = 2400         # 100 ms at 24 kHz
DEVICE_BLOCKSIZE  = BLOCKSIZE    # same as BLOCKSIZE when RESAMPLE_RATIO == 1
DEFAULT_HTTP_PORT = 19000
RECONNECT_DELAY   = 5
AGENT_TIMEOUT_S   = 45
MIC_GAIN          = 3.0
MIC_GATE_PEAK     = 300          # noise gate — pre-gain peak below this → silence
MIC_GATE_MIN      = 300          # calibration clamp — quietest usable room
MIC_GATE_MAX      = 3000         # calibration clamp — above this, use a headset

# Output volume control — macOS uses system-wide volume via osascript.
# Per-device volume isn't scriptable on macOS, so software attenuation is the
# primary fine-grained control, with osascript for coarse adjustment.
CAL_FALLBACK_VOL  = 0.70         # fallback software gain when cal can't measure
CAL_NEW_DEV_VOL   = 0.20         # safe default for unknown speakers (20%)
CAL_STORE_FILE    = os.path.expanduser("~/.openclaw/workspace/speaker_cal_store.json")
# Speech-interrupt: if the mic sees this many consecutive 50ms blocks above
# the interrupt threshold while Five is speaking, kill TTS immediately.
SPEAK_INTERRUPT_PEAK   = 1200
SPEAK_INTERRUPT_BLOCKS = 6     # × 50 ms = 300 ms sustained speech → interrupt

# Compat alias — many places still reference ALSA_OUTPUT; on Mac it's a no-op label
ALSA_OUTPUT       = "coreaudio:default"

# ── Compat stubs for Pi-only constants/funcs referenced from legacy HTTP routes ─
# These keep the daemon importable and stop NameError when older calibration /
# speaker-control routes are hit on Mac. The routes themselves still degrade —
# Mac-native equivalents are exposed via the dashboard.
AGC_SOURCE_NAME   = "__mac_no_agc__"
AGC_MIC_GAIN      = 3.0
AGC_MIC_GATE      = 300
RAW_MIC_SOURCE    = ""
PIPER_CMD         = "/usr/bin/false"   # absent on Mac — exits non-zero
PIPER_ENV         = dict(os.environ)
PIPER_VOICE_EN    = ""
PIPER_VOICE_ZH    = ""
PIPER_SAMPLE_RATE = 24000
CAL_FALLBACK_PW   = 50
CAL_FALLBACK_SW   = CAL_FALLBACK_VOL
CAL_NEW_DEV_PW    = 20
CAL_NEW_DEV_SW    = CAL_NEW_DEV_VOL
CAL_ANNOUNCE_PW   = 45
CAL_ANNOUNCE_SW   = 0.75
CAL_AUDIBLE_SNR   = 80.0
NEW_DEVICE_VOLUME = 0.05


def _detect_headset() -> bool:
    """Mac: no PipeWire — returns False so legacy code paths skip the headset
    branch. Use sounddevice device name heuristics if needed."""
    return False


def _find_usb_speaker_sink():
    """Mac compat stub — returns None (no PipeWire sink concept)."""
    return None


def _safe_volume_new_sinks(safe_pct: int = 70):
    """Mac compat stub — uses osascript to cap system volume."""
    _set_system_volume(min(safe_pct, 70))

CONVERSATION_LOG: list[dict] = []   # {"role":"you"/"five"/"system", "text":...}

import threading as _threading
_mic_level_lock = _threading.Lock()
_mic_level_current = [0]   # latest raw pre-gain peak, written by audio thread
_mic_gate_ref     = [500]  # mutable wrapper for MIC_GATE_PEAK, readable across threads

# ── Mac audio device helpers (CoreAudio via sounddevice + osascript) ─────────

_headset_cal_loop = [False]
_speaker_cal_result: dict = {}
_cal_mode_override = [None]
_device_change_msg = [""]
_audio_fingerprint = [""]


def _list_audio_devices() -> dict:
    """Enumerate CoreAudio devices via sounddevice. Returns {inputs:[...], outputs:[...]}.

    Each entry: {"index": int, "name": str, "channels": int, "kind": "usb"|"bluetooth"|"builtin"|"other"}
    """
    inputs, outputs = [], []
    try:
        for idx, dev in enumerate(sd.query_devices()):
            name = str(dev.get("name", "")).strip()
            lower = name.lower()
            if "bluetooth" in lower or "airpods" in lower or "beats" in lower:
                kind = "bluetooth"
            elif "usb" in lower:
                kind = "usb"
            elif "macbook" in lower or "built-in" in lower or "internal" in lower or "mac mini" in lower:
                kind = "builtin"
            else:
                kind = "other"
            entry = {"index": idx, "name": name, "kind": kind}
            if dev.get("max_input_channels", 0) > 0:
                inputs.append({**entry, "channels": dev["max_input_channels"]})
            if dev.get("max_output_channels", 0) > 0:
                outputs.append({**entry, "channels": dev["max_output_channels"]})
    except Exception as e:
        log.warning("Could not enumerate audio devices: %s", e)
    return {"inputs": inputs, "outputs": outputs}


def _device_label(idx) -> str:
    """Friendly label for a sounddevice device index (or None for default)."""
    if idx is None:
        try:
            d = sd.query_devices(kind="input")
            return f"default ({d['name']})"
        except Exception:
            return "default"
    try:
        d = sd.query_devices(idx)
        return f"{d['name']} (#{idx})"
    except Exception:
        return f"device #{idx}"


def _get_system_volume() -> int:
    """Return current macOS output volume 0-100 via AppleScript."""
    try:
        out = subprocess.run(
            ["osascript", "-e", "output volume of (get volume settings)"],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
        return int(out) if out.isdigit() else 50
    except Exception:
        return 50


def _set_system_volume(pct: int) -> bool:
    """Set macOS output volume 0-100 via AppleScript."""
    pct = max(0, min(100, int(pct)))
    try:
        subprocess.run(
            ["osascript", "-e", f"set volume output volume {pct}"],
            check=False, timeout=3,
        )
        return True
    except Exception as e:
        log.warning("Could not set system volume: %s", e)
        return False


def _bt_mic_warning(input_device) -> str:
    """Return warning string if the selected input is Bluetooth (SCO degrades playback), else ''."""
    if input_device is None:
        return ""
    try:
        d = sd.query_devices(input_device)
        n = str(d.get("name", "")).lower()
        if "bluetooth" in n or "airpods" in n or "beats" in n:
            return ("Bluetooth mic active — macOS may switch to SCO mode (8kHz), "
                    "degrading playback quality while mic is in use.")
    except Exception:
        pass
    return ""


def _mac_notify(title: str, msg: str) -> None:
    """Display a macOS notification (best-effort, fire-and-forget)."""
    try:
        # Escape quotes in the message for AppleScript
        msg_esc = msg.replace('"', '\\"')
        title_esc = title.replace('"', '\\"')
        subprocess.Popen(
            ["osascript", "-e",
             f'display notification "{msg_esc}" with title "{title_esc}"'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def _get_device_status() -> dict:
    """Return current audio device info for the dashboard status panel."""
    result = {
        "mic": "?",
        "speaker_alsa": ALSA_OUTPUT,
        "speaker_name": "default output",
        "spk_vol": f"{_get_system_volume()}%",
        "sw_pct": 100,
        "gate": 500,
        "gain": 3.0,
        "bt_warning": "",
    }
    try:
        in_dev  = _selected_input_device[0]
        out_dev = _selected_output_device[0]
        result["mic"] = _device_label(in_dev)
        if out_dev is not None:
            result["speaker_name"] = _device_label(out_dev)
        else:
            try:
                d = sd.query_devices(kind="output")
                result["speaker_name"] = d["name"]
            except Exception:
                pass
        result["bt_warning"] = _bt_mic_warning(in_dev)
    except Exception:
        pass
    try:
        result["sw_pct"] = int(_cal_sw_volume * 100)
    except Exception:
        pass
    result["gate"] = _mic_gate_ref[0]
    result["gain"] = MIC_GAIN
    return result


def _get_audio_fingerprint() -> str:
    """Fingerprint of connected audio devices — names only, for change detection."""
    try:
        devs = sd.query_devices()
        return "\n".join(sorted(str(d.get("name", "")) for d in devs if d))
    except Exception:
        return ""


# Currently selected sounddevice indices (None = system default)
_selected_input_device: list = [None]
_selected_output_device: list = [None]


def _cal_capture(n_samples: int, sample_rate: int) -> "np.ndarray":
    """Capture mono int16 from the currently selected input for calibration."""
    try:
        rec = sd.rec(n_samples, samplerate=sample_rate, channels=1,
                     dtype="int16", device=_selected_input_device[0], blocking=True)
        return rec[:n_samples, 0].copy()
    except Exception as e:
        log.warning("Cal capture error: %s", e)
        return np.zeros(n_samples, dtype=np.int16)


def run_speaker_calibration(alsa_output: str = None,
                             test_freq: float = 440.0,
                             duration: float = 0.2,
                             snr_target: float = 50000.0) -> dict:
    """Find the MINIMUM usable speaker software-volume on macOS.

    Plays a tone at increasing software gain via sounddevice on the selected
    output, records simultaneously via sounddevice on the selected input,
    measures tone energy vs noise floor (FFT), picks the first level that
    clears the audibility threshold. macOS system volume is not changed by
    this function — only the daemon's internal `_cal_sw_volume` is set.
    """
    import time as _t

    sample_rate = 24000
    n_samples   = int(sample_rate * duration)
    freq_idx    = int(np.round(test_freq * n_samples / sample_rate))
    in_dev  = _selected_input_device[0]
    out_dev = _selected_output_device[0]

    # Noise floor — measure with silence
    try:
        ref_rec   = _cal_capture(n_samples, sample_rate)
        ref_data  = ref_rec.astype(np.float32) / 32768.0
        ref_fft   = np.abs(np.fft.rfft(ref_data)) / n_samples
        noise_floor = float(np.median(ref_fft)) or 1e-6
    except Exception:
        noise_floor = 1e-6

    # Software gain steps from very quiet to loud
    steps = [0.005, 0.01, 0.02, 0.05, 0.10, 0.20, 0.35, 0.50, 0.70, 0.90]
    measurements: list[dict] = []
    found_sw = CAL_FALLBACK_VOL
    status = "ok"

    try:
        for sw_vol in steps:
            # Generate tone at this gain
            t_arr  = np.linspace(0, duration, n_samples, endpoint=False)
            tone16 = (0.5 * sw_vol * np.sin(2 * np.pi * test_freq * t_arr) * 32767).astype(np.int16)

            # Play + record simultaneously via sounddevice
            recording = np.zeros(n_samples, dtype=np.int16)
            done_ev   = _threading.Event()
            def _rec(buf=recording, ev=done_ev):
                try:
                    buf[:] = _cal_capture(n_samples, sample_rate)
                except Exception as e:
                    log.warning("Cal mic error: %s", e)
                finally:
                    ev.set()
            _threading.Thread(target=_rec, daemon=True).start()
            _t.sleep(0.05)  # let capture spin up
            try:
                sd.play(tone16, samplerate=sample_rate, device=out_dev, blocking=True)
            except Exception as e:
                log.warning("Cal playback error at sw=%.2f: %s", sw_vol, e)
            done_ev.wait(timeout=duration + 2.0)

            data    = recording.astype(np.float32) / 32768.0
            fft_mag = np.abs(np.fft.rfft(data)) / n_samples
            tone_e  = float(fft_mag[freq_idx])
            snr     = tone_e / noise_floor
            measurements.append({"sw": round(sw_vol, 3),
                                 "tone": round(tone_e, 7), "snr": round(snr, 2)})
            log.info("Speaker cal: SW=%.2f tone=%.6f SNR=%.1f", sw_vol, tone_e, snr)

            if snr >= snr_target:
                found_sw = sw_vol
                log.info("Speaker cal: target SNR reached at SW=%.2f", sw_vol)
                break
        else:
            # No step reached target — pick best clearly-audible step
            if measurements:
                audible = next(
                    (m for m in measurements if m["snr"] >= 80.0),
                    None,
                )
                if audible:
                    found_sw = audible["sw"]
                    status = "weak_coupling"
                else:
                    best = max(measurements, key=lambda m: m["tone"])
                    if best["tone"] < 0.00005:
                        status = "no_mic"
                        found_sw = CAL_FALLBACK_VOL
                    else:
                        found_sw = best["sw"]
                        status = "weak_coupling"

        global _cal_sw_volume
        _cal_sw_volume = found_sw
        log.info("Speaker cal complete: SW=%.2f status=%s", found_sw, status)

        # Save to per-device store keyed by output device name
        out_name = "default"
        try:
            d = sd.query_devices(out_dev if out_dev is not None else None, kind="output")
            out_name = d.get("name", "default")
        except Exception:
            pass
        _save_device_cal(out_name, found_sw)

    except Exception as e:
        log.error("Speaker calibration error: %s", e)
        status = f"error: {e}"
        found_sw = CAL_FALLBACK_VOL

    return {
        "safe_vol": int(found_sw * 100),
        "safe_sw_vol": found_sw,
        "speaker_alsa": ALSA_OUTPUT,
        "measurements": measurements,
        "mic_source": _device_label(in_dev),
        "speaker_sink": _device_label(out_dev),
        "test_freq": test_freq,
        "snr_target": snr_target,
        "status": status,
    }

_cal_sw_volume: float = 1.0   # updated after calibration; used by speak() for normal TTS
MAX_LOG_ENTRIES = 40

def _log_entry(role: str, text: str):
    now = datetime.datetime.now()
    ts  = now.strftime("%H:%M:%S")
    CONVERSATION_LOG.append({"role": role, "text": text, "ts": ts,
                              "epoch": now.timestamp()})
    if len(CONVERSATION_LOG) > MAX_LOG_ENTRIES:
        CONVERSATION_LOG.pop(0)

CALIBRATE_PHRASES = {
    "calibrate mic", "calibrate microphone", "calibrate noise",
    "recalibrate mic", "recalibrate microphone",
    "mic calibration", "microphone calibration",
    "adjust mic for noise", "adjust microphone for noise",
}

WAKE_PHRASES  = {"five wake up", "5 wake up", "real time talk on", "real-time talk on", "realtimetalk on"}
SLEEP_PHRASES = {"five go to sleep", "5 go to sleep", "real time talk off", "real-time talk off", "realtimetalk off"}

def _is_english_or_chinese(text: str) -> bool:
    """Return True only if the transcript appears to be English or Chinese.
    Filters out Japanese (hiragana/katakana), Arabic, Cyrillic, Korean, etc.
    that gpt-4o-transcribe hallucinates when audio is noisy.
    """
    # Reject if it contains Japanese kana, Arabic, Cyrillic, Korean, etc.
    reject_ranges = (
        (0x3040, 0x30FF),   # hiragana + katakana (Japanese)
        (0x0600, 0x06FF),   # Arabic
        (0x0400, 0x04FF),   # Cyrillic
        (0xAC00, 0xD7AF),   # Korean Hangul
        (0x0900, 0x097F),   # Devanagari
    )
    for ch in text:
        cp = ord(ch)
        if any(lo <= cp <= hi for lo, hi in reject_ranges):
            return False
    # Accept if all characters are ASCII or CJK (Chinese/Japanese kanji — kanji
    # without kana means it's Chinese in practice here)
    for ch in text:
        cp = ord(ch)
        if cp <= 0x7F:
            continue  # ASCII = English
        if 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF:
            continue  # CJK unified ideographs = Chinese
        if ch in ' \t\n\r':
            continue
        # Anything else (accented Latin for German/French/etc.) → reject
        return False
    return True

def _normalize(text: str) -> str:
    import string
    t = text.strip().lower()
    t = t.translate(str.maketrans(string.punctuation, " " * len(string.punctuation)))
    # treat digit "5" as "five"
    t = re.sub(r'\b5\b', 'five', t)
    return " ".join(t.split())

def _matches_phrase(transcript: str, phrases: set) -> bool:
    """True if the transcript contains any trigger phrase, or is a fuzzy word-overlap match.

    Two-pass:
    1. Exact substring after normalisation.
    2. Fuzzy: if the transcript shares ≥ 60% of a phrase's words it counts as a match
       (handles car-noise garbling like 'five wake up' → 'five break up').
    """
    t = _normalize(transcript)
    for phrase in phrases:
        p = _normalize(phrase)
        # Pass 1: substring
        if p in t:
            return True
        # Pass 2: word overlap ratio
        t_words = set(t.split())
        p_words  = set(p.split())
        if p_words and len(t_words & p_words) / len(p_words) >= 0.6:
            return True
    return False

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [RTT] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("RealTimeTalk")

# ── Config / secrets ──────────────────────────────────────────────────────────

def _load_json(path: str) -> dict:
    with open(path) as f:
        return json.load(f)

def load_openai_key() -> str:
    cfg = _load_json(OPENCLAW_CONFIG)
    key = (
        cfg.get("talk", {})
           .get("providers", {})
           .get("openai", {})
           .get("apiKey", "")
    )
    # Resolve OpenClaw SecretRef: {"source":"file","provider":"...","id":"/a/b/c"}
    if isinstance(key, dict) and key.get("source") == "file":
        provider_name = key.get("provider", "")
        secret_path = os.path.expanduser(
            cfg.get("secrets", {})
               .get("providers", {})
               .get(provider_name, {})
               .get("path", "")
        )
        secrets = _load_json(secret_path)
        for part in [p for p in key.get("id", "").split("/") if p]:
            secrets = secrets[part]
        key = secrets
    if not key:
        raise RuntimeError(
            "No OpenAI API key at talk.providers.openai.apiKey in openclaw.json"
        )
    return key

def load_gateway_token() -> str:
    cfg = _load_json(OPENCLAW_CONFIG)
    token = cfg.get("gateway", {}).get("auth", {}).get("token", "")
    if not token:
        raise RuntimeError("No gateway.auth.token in openclaw.json")
    return token

# ── PipeWire/AGC compat stubs (Mac uses CoreAudio; these always return safe defaults) ─

def _agc_source_available() -> bool:
    return False  # No PipeWire on Mac

def _activate_agc_source() -> bool:
    return False  # No WebRTC AGC virtual source on Mac

def _update_agc_capture_source(physical_source: str) -> bool:
    return False

def _get_default_source() -> str:
    return ""

def _set_default_source(name: str) -> bool:
    return False

# ── Per-device calibration store ─────────────────────────────────────────────

_cal_store: dict = {}   # {device_name: {"sw_vol": float, "name": str}}

def _load_cal_store() -> None:
    global _cal_store
    try:
        with open(CAL_STORE_FILE) as f:
            _cal_store = json.load(f)
        log.info("Loaded calibration store: %d device(s)", len(_cal_store))
    except (FileNotFoundError, json.JSONDecodeError):
        _cal_store = {}

def _save_cal_store() -> None:
    try:
        os.makedirs(os.path.dirname(CAL_STORE_FILE), exist_ok=True)
        with open(CAL_STORE_FILE, "w") as f:
            json.dump(_cal_store, f, indent=2)
    except Exception as e:
        log.warning("Could not save calibration store: %s", e)

def _save_device_cal(device_name: str, sw_vol: float) -> None:
    """Record calibrated software volume for an output device and persist to disk."""
    _cal_store[device_name] = {"sw_vol": float(sw_vol), "name": device_name}
    _save_cal_store()
    log.info("Saved calibration for %r: SW=%.2f", device_name, sw_vol)

def _apply_device_cal(device_name: str) -> bool:
    """Apply saved software volume for an output device, or safe default if unknown.

    Returns True if a previously calibrated level was found and applied,
    False if a safe default was applied (new/unknown device).
    """
    if device_name in _cal_store:
        entry = _cal_store[device_name]
        sw    = float(entry.get("sw_vol", CAL_FALLBACK_VOL))
        globals()['_cal_sw_volume'] = sw
        log.info("Restored calibration for %r: SW=%.2f", device_name, sw)
        return True
    else:
        globals()['_cal_sw_volume'] = CAL_NEW_DEV_VOL
        log.info("New/unknown device %r — using safe default SW=%.2f",
                 device_name, CAL_NEW_DEV_VOL)
        return False

# ── Service file (launchd plist) helpers ─────────────────────────────────────

SERVICE_FILE = os.path.expanduser(
    "~/Library/LaunchAgents/ai.openclaw.realtimetalk.plist"
)
SERVICE_LABEL = "ai.openclaw.realtimetalk"

def _kickstart_service() -> None:
    """Restart the LaunchAgent after editing the plist."""
    try:
        uid = os.getuid()
        subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/{SERVICE_LABEL}"],
                       check=False, capture_output=True)
    except Exception as e:
        log.warning("Could not kickstart service: %s", e)

def _plist_replace_arg(flag: str, new_value: str | None) -> bool:
    """Add/replace/remove a CLI flag inside the plist's ProgramArguments array.

    flag: e.g. '--input-device' or '--mic-gate'
    new_value: str to set, or None to remove the flag and its argument.
    Returns True on successful write.
    """
    if not os.path.exists(SERVICE_FILE):
        log.info("No service plist at %s — skipping update", SERVICE_FILE)
        return False
    try:
        import plistlib
        with open(SERVICE_FILE, "rb") as f:
            plist = plistlib.load(f)
        args = list(plist.get("ProgramArguments", []))
        # Remove any existing instance of `flag` and its value
        cleaned: list = []
        skip_next = False
        for a in args:
            if skip_next:
                skip_next = False
                continue
            if a == flag:
                skip_next = True
                continue
            cleaned.append(a)
        if new_value is not None:
            cleaned.extend([flag, str(new_value)])
        plist["ProgramArguments"] = cleaned
        with open(SERVICE_FILE, "wb") as f:
            plistlib.dump(plist, f)
        return True
    except Exception as e:
        log.warning("Could not update service plist (%s): %s", flag, e)
        return False

def _update_service_alsa_output(new_alsa: str):
    """Persist --output-device <idx-or-name> in the plist (Mac equivalent of ALSA arg)."""
    if _plist_replace_arg("--output-device", new_alsa):
        _kickstart_service()
        log.info("Service updated: --output-device %s", new_alsa)

def _update_service_input_source(source_name: str):
    """Persist --input-device <idx-or-name> in the plist."""
    val = source_name if source_name else None
    if _plist_replace_arg("--input-device", val):
        _kickstart_service()
        log.info("Service updated: --input-device %s", source_name or "<unset>")

def _update_service_gate(new_gate: int):
    """Persist --mic-gate <n> in the plist."""
    if _plist_replace_arg("--mic-gate", str(int(new_gate))):
        _kickstart_service()

# ── Text helpers ──────────────────────────────────────────────────────────────

def strip_markdown(text: str) -> str:
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'`{1,3}[^`\n]*`{1,3}', '', text)
    text = re.sub(r'^\s*#+\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\[(.+?)\]\(.+?\)', r'\1', text)
    text = re.sub(r'^\s*[-*+]\s+', '', text, flags=re.MULTILINE)
    # Strip emoji and symbol characters — Piper reads them as their Unicode names
    # (e.g. Five's ⚡ becomes "high voltage"). Keep CJK for Chinese TTS.
    text = re.sub(
        r'[\U0001F000-\U0001FFFF'   # emoji / pictographs
        r'☀-➿'            # misc symbols, dingbats (includes ⚡ U+26A1)
        r'⬀-⯿'            # misc symbols & arrows
        r'︀-️]',          # variation selectors
        '', text
    )
    return text.strip()

# ── Piper TTS ─────────────────────────────────────────────────────────────────

def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return (0x4E00 <= cp <= 0x9FFF or
            0x3400 <= cp <= 0x4DBF or
            0x20000 <= cp <= 0x2A6DF)

def _is_chinese_text(text: str) -> bool:
    return any(_is_cjk(c) for c in text)

def _is_likely_noise(text: str) -> bool:
    """Return True if the transcript looks like a noise hallucination.

    Two checks:
    1. Any word ≥ 4 Latin letters with ZERO standard vowels (a/e/i/o/u) —
       impossible in real English (e.g. 'Dyftm', 'ftm', 'knopk').
    2. Whole-text vowel ratio < 10% across 10+ Latin letters — catches
       dense consonant hallucinations even when split across short words.
    Skipped entirely for mostly-CJK text (Chinese has no Latin vowels).
    """
    cjk_count = sum(1 for c in text if _is_cjk(c))
    all_latin  = [c for c in text if c.isalpha() and ord(c) < 256]
    if cjk_count > len(all_latin):
        return False                            # mostly Chinese — skip

    # Check 1: any individual word with zero vowels
    for word in text.split():
        letters = [c for c in word if c.isalpha() and ord(c) < 256]
        if len(letters) >= 4 and not any(c.lower() in "aeiou" for c in letters):
            return True

    # Check 2: extremely low overall vowel density
    if len(all_latin) >= 10:
        vowels = sum(1 for c in all_latin if c.lower() in "aeiou")
        if vowels / len(all_latin) < 0.10:
            return True

    return False

def _to_simplified(text: str) -> str:
    """Normalize captured Chinese to Simplified. gpt-4o-transcribe often
    returns Traditional; convert deterministically (zhconv, pure-Python).
    Non-Chinese text passes through unchanged."""
    if not text or _zh_convert is None or not _is_chinese_text(text):
        return text
    try:
        return _zh_convert(text, "zh-cn")
    except Exception:
        return text

def _split_by_script(text: str) -> list[tuple[str, str]]:
    """Split text into [(segment, 'zh'|'en')] so each segment uses its correct Piper voice."""
    segments: list[tuple[str, str]] = []
    current_chars: list[str] = []
    current_lang = None
    for ch in text:
        lang = 'zh' if _is_cjk(ch) else 'en'
        # Chinese punctuation stays with Chinese; spaces/ASCII punct follow current lang
        if ch in ' \t\n\r，。！？；：、""‘’「」《》':
            lang = current_lang or 'en'
        if lang != current_lang and current_chars:
            seg = ''.join(current_chars).strip()
            if seg:
                segments.append((seg, current_lang or 'en'))
            current_chars = []
        current_lang = lang
        current_chars.append(ch)
    if current_chars:
        seg = ''.join(current_chars).strip()
        if seg:
            segments.append((seg, current_lang or 'en'))
    return segments

    if current_chars:
        segments.append((''.join(current_chars).strip(), current_lang or 'en'))

    return [(s, l) for s, l in segments if s]

# ── TTS (Edge TTS primary, macOS `say` fallback, ffmpeg PCM decode) ──────────

TTS_SAMPLE_RATE = 24000  # daemon plays back at 24 kHz mono PCM int16

def _edge_tts_to_mp3(text: str, voice: str, out_path: str, timeout: float = EDGE_TTS_TIMEOUT) -> bool:
    """Render `text` via Edge TTS skill → MP3 at out_path. Returns True on success."""
    try:
        result = subprocess.run(
            ["node", EDGE_TTS_SCRIPT, text, "--voice", voice, "--output", out_path],
            capture_output=True, timeout=timeout, text=True,
        )
        if result.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return True
        log.warning("Edge TTS failed for %r (rc=%d): %s",
                    text[:30], result.returncode, result.stderr[:160])
        return False
    except subprocess.TimeoutExpired:
        log.warning("Edge TTS timed out (%ss) for %r — falling back to say", timeout, text[:30])
        return False
    except Exception as e:
        log.warning("Edge TTS error: %s", e)
        return False


def _say_fallback_to_aiff(text: str, lang: str, out_path: str, timeout: float = 15.0) -> bool:
    """Render `text` via macOS `say -o <out>` as AIFF.

    On modern macOS, `say` accepts only file output without a data-format flag;
    ffmpeg handles whatever format `say` emits (AIFF by default). Returns True
    on success.
    """
    voice = SAY_VOICE_ZH if lang == "zh" else SAY_VOICE_EN
    try:
        result = subprocess.run(
            ["say", "-v", voice, "-o", out_path, text],
            capture_output=True, timeout=timeout,
        )
        if result.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return True
        log.error("say failed for %r (rc=%d): %s",
                  text[:30], result.returncode,
                  result.stderr[:160].decode(errors="replace") if result.stderr else "")
        return False
    except Exception as e:
        log.error("say fallback error: %s", e)
        return False


def _decode_to_pcm(audio_path: str) -> "np.ndarray":
    """Decode any audio file to 24 kHz mono int16 PCM via ffmpeg. Returns empty array on failure."""
    try:
        result = subprocess.run(
            [FFMPEG_CMD, "-loglevel", "quiet", "-i", audio_path,
             "-f", "s16le", "-ar", str(TTS_SAMPLE_RATE), "-ac", "1", "-"],
            capture_output=True, timeout=20,
        )
        if result.returncode != 0:
            log.error("ffmpeg decode failed for %s: %s",
                      audio_path, result.stderr[:160].decode(errors="replace"))
            return np.zeros(0, dtype=np.int16)
        return np.frombuffer(result.stdout, dtype=np.int16).copy()
    except Exception as e:
        log.error("ffmpeg decode error: %s", e)
        return np.zeros(0, dtype=np.int16)


def speak(text: str, alsa_output: str = ALSA_OUTPUT, volume: float = -1.0, silence_ms: int = 300):
    """Synthesise text via Edge TTS (with `say` fallback) and play via sounddevice.

    Splits text by script (en/zh), renders each segment with the appropriate
    voice, decodes to 24 kHz mono PCM int16, applies software volume, and
    plays via the selected CoreAudio output. Polls the mic level during
    playback; if the user starts speaking, calls sd.stop() to interrupt.
    """
    import tempfile
    if volume < 0:
        volume = _cal_sw_volume
    clean = strip_markdown(text)
    if not clean:
        return

    segments = _split_by_script(clean)
    pcm_parts: list[np.ndarray] = []
    temp_files: list[str] = []
    silence_samples = int(TTS_SAMPLE_RATE * silence_ms / 1000)

    try:
        if silence_ms > 0:
            pcm_parts.append(np.zeros(silence_samples, dtype=np.int16))

        for seg_text, lang in segments:
            if not seg_text.strip():
                continue
            edge_voice = EDGE_VOICE_ZH if lang == "zh" else EDGE_VOICE_EN
            mp3_path = tempfile.mktemp(suffix=".mp3")
            temp_files.append(mp3_path)
            pcm = np.zeros(0, dtype=np.int16)
            if _edge_tts_to_mp3(seg_text, edge_voice, mp3_path):
                pcm = _decode_to_pcm(mp3_path)
            if pcm.size == 0:
                # Fall back to macOS `say`
                aiff_path = tempfile.mktemp(suffix=".aiff")
                temp_files.append(aiff_path)
                if _say_fallback_to_aiff(seg_text, lang, aiff_path):
                    pcm = _decode_to_pcm(aiff_path)
            if pcm.size == 0:
                log.error("Both Edge TTS and say failed for segment: %r", seg_text[:60])
                continue
            pcm_parts.append(pcm)

        if silence_ms > 0 and len(pcm_parts) > 1:
            pcm_parts.append(np.zeros(silence_samples, dtype=np.int16))

        if not pcm_parts:
            return

        final = np.concatenate(pcm_parts) if len(pcm_parts) > 1 else pcm_parts[0]
        if volume < 1.0:
            final = np.clip(final.astype(np.float32) * volume, -32768, 32767).astype(np.int16)

        # Sample mic level before playback (ambient baseline)
        with _mic_level_lock:
            baseline_peak = _mic_level_current[0]

        # Play via sounddevice; poll mic ~every 50 ms for speech-interrupt.
        mic_peaks_during: list[int] = []
        _interrupted = [False]
        out_dev = _selected_output_device[0]
        try:
            sd.play(final, samplerate=TTS_SAMPLE_RATE, device=out_dev, blocking=False)
        except Exception as e:
            log.error("sd.play() failed: %s", e)
            return

        consec = 0
        while True:
            try:
                stream = sd.get_stream()
                active = stream is not None and stream.active
            except Exception:
                active = False
            if not active:
                break
            import time as _t
            _t.sleep(0.05)
            with _mic_level_lock:
                p = _mic_level_current[0]
            mic_peaks_during.append(p)
            if p > max(baseline_peak * 15, SPEAK_INTERRUPT_PEAK):
                consec += 1
                if consec >= SPEAK_INTERRUPT_BLOCKS:
                    log.info("Speech interrupt — stopping TTS")
                    _interrupted[0] = True
                    try:
                        sd.stop()
                    except Exception:
                        pass
                    break
            else:
                consec = 0

        try:
            sd.wait()
        except Exception:
            pass

    except Exception as e:
        log.error("speak() error: %s", e)
    finally:
        for p in temp_files:
            try: os.unlink(p)
            except FileNotFoundError: pass

# ── OpenClaw gateway client ───────────────────────────────────────────────────

class GatewayClient:
    """
    Persistent WebSocket operator connection to the local OpenClaw gateway.

    Uses the trusted backend-client path (client.id="gateway-client",
    client.mode="backend") which bypasses device-pairing scope upgrades for
    loopback connections authenticated with the shared gateway token.
    """

    def __init__(self, token: str):
        self.token = token
        self._ws = None
        # Maps request-id → Future for chat.send acks
        self._send_acks: dict[str, asyncio.Future] = {}
        # Maps runId → Future[str] for final chat replies
        self._reply_futs: dict[str, asyncio.Future] = {}
        # Maps runId → latest assistant-stream text (fallback if chat final empty)
        self._assistant_text: dict[str, str] = {}

    async def connect(self):
        self._ws = await websockets.connect(OPENCLAW_GW_URL)
        await self._ws.recv()  # connect.challenge — backend clients skip signing
        await self._ws.send(json.dumps({
            "type": "req", "id": "gw-connect", "method": "connect",
            "params": {
                "minProtocol": 4, "maxProtocol": 4,
                "client": {
                    "id": "gateway-client", "version": "1.2.0",
                    "platform": "linux", "mode": "backend",
                },
                "role": "operator",
                "scopes": ["operator.read", "operator.write"],
                "caps": [], "commands": [], "permissions": {},
                "auth": {"token": self.token},
                "locale": "en-US",
                "userAgent": "realtimetalk/1.2",
            },
        }))
        hello = json.loads(await self._ws.recv())
        if not hello.get("ok"):
            raise RuntimeError(f"Gateway connect failed: {hello.get('error')}")
        scopes = hello.get("payload", {}).get("auth", {}).get("scopes", [])
        log.info("OpenClaw gateway connected (scopes: %s)", scopes)

    async def listen(self, stop_event: asyncio.Event):
        """Route incoming gateway events to waiting futures. Run as a task."""
        try:
            async for raw in self._ws:
                if stop_event.is_set():
                    break
                msg = json.loads(raw)
                mtype = msg.get("type", "")
                event = msg.get("event", "")
                payload = msg.get("payload") or {}
                msg_id = msg.get("id", "")

                # Resolve chat.send acks
                if mtype == "res" and msg_id in self._send_acks:
                    fut = self._send_acks.pop(msg_id)
                    if not fut.done():
                        fut.set_result(msg)

                # Track assistant-stream text as a reliable reply source
                elif event == "agent" and payload.get("stream") == "assistant":
                    rid = payload.get("runId")
                    atext = (payload.get("data") or {}).get("text", "")
                    if rid and atext:
                        self._assistant_text[rid] = atext

                # Resolve agent replies on final chat event
                elif event == "chat" and payload.get("state") == "final":
                    run_id = payload.get("runId")
                    cmsg = payload.get("message", {}) or {}
                    content = cmsg.get("content", []) or []
                    # Standard content array (type=text)
                    text = " ".join(
                        c.get("text", "") for c in content if c.get("type") == "text"
                    ).strip()
                    # Fallback: Responses API output_text items
                    if not text:
                        text = " ".join(
                            c.get("text", "") for c in content
                            if c.get("type") in ("output_text", "text_delta")
                        ).strip()
                    # Fallback: top-level text / deltaText
                    if not text:
                        text = (cmsg.get("text") or payload.get("deltaText") or "").strip()
                    # Fallback: assistant-stream text captured during the run
                    if not text:
                        text = self._assistant_text.get(run_id, "").strip()
                    if not text:
                        log.warning("chat final empty: payload=%s",
                                    json.dumps(payload)[:600])
                    self._assistant_text.pop(run_id, None)
                    fut = self._reply_futs.pop(run_id, None)
                    if fut and not fut.done():
                        fut.set_result(text)

        except websockets.ConnectionClosed:
            pass

    async def ask(self, message: str, session_key: str = OPENCLAW_SESSION) -> str:
        """Send a message to the agent and return its complete reply text."""
        loop = asyncio.get_running_loop()
        idem = str(uuid.uuid4())
        req_id = f"send:{idem}"

        ack_fut: asyncio.Future = loop.create_future()
        self._send_acks[req_id] = ack_fut

        await self._ws.send(json.dumps({
            "type": "req", "id": req_id, "method": "chat.send",
            "params": {
                "sessionKey": session_key,
                "message": message,
                "idempotencyKey": idem,
            },
        }))

        ack = await asyncio.wait_for(ack_fut, timeout=10)
        if not ack.get("ok"):
            raise RuntimeError(f"chat.send failed: {ack.get('error')}")

        run_id = ack.get("payload", {}).get("runId")
        if not run_id:
            raise RuntimeError("chat.send returned no runId")

        reply_fut: asyncio.Future = loop.create_future()
        self._reply_futs[run_id] = reply_fut

        # Register with agent.wait so the gateway tracks this run
        await self._ws.send(json.dumps({
            "type": "req", "id": f"wait:{run_id}", "method": "agent.wait",
            "params": {"runId": run_id, "timeoutMs": AGENT_TIMEOUT_S * 1000},
        }))

        text = await asyncio.wait_for(reply_fut, timeout=AGENT_TIMEOUT_S + 5)
        # Codex harness delivers replies via the `message` tool, not chat
        # content — the chat-final event is empty. Pull the reply from
        # chat.history where the message-tool call arguments are persisted.
        if not text:
            await asyncio.sleep(0.6)  # let message-tool result persist
            text = await self._reply_from_history(session_key)
        return text

    async def _reply_from_history(self, session_key: str) -> str:
        """Fetch the latest assistant reply from chat.history.

        Handles the codex harness `message`-tool delivery as well as plain
        assistant text (automatic mode).
        """
        loop = asyncio.get_running_loop()
        hid = f"hist:{uuid.uuid4()}"
        hfut: asyncio.Future = loop.create_future()
        self._send_acks[hid] = hfut
        try:
            await self._ws.send(json.dumps({
                "type": "req", "id": hid, "method": "chat.history",
                "params": {"sessionKey": session_key, "limit": 8},
            }))
            resp = await asyncio.wait_for(hfut, timeout=10)
        except (asyncio.TimeoutError, Exception) as e:
            self._send_acks.pop(hid, None)
            log.warning("chat.history fetch failed: %s", e)
            return ""
        msgs = resp.get("payload", {}).get("messages", []) or []
        for m in reversed(msgs):
            if m.get("role") != "assistant":
                continue
            content = m.get("content", [])
            if isinstance(content, str):
                if content.strip():
                    return content.strip()
                continue
            if not isinstance(content, list):
                continue
            # Codex message-tool call
            for c in content:
                if c.get("type") == "toolCall" and c.get("name") == "message":
                    args = c.get("arguments") or c.get("input") or {}
                    txt = (args.get("message") or "").strip()
                    if txt:
                        return txt
            # Plain assistant text (automatic / non-codex)
            txt = " ".join(
                c.get("text", "") for c in content
                if isinstance(c, dict) and c.get("type") == "text"
            ).strip()
            if txt:
                return txt
        log.warning("chat.history: no assistant reply found in %d msgs", len(msgs))
        return ""

    async def close(self):
        if self._ws:
            await self._ws.close()

# ── OpenAI Realtime session (VAD + STT only) ──────────────────────────────────

class RealtimeSession:
    """
    Connects to OpenAI Realtime API solely for voice activity detection and
    speech-to-text. Does not generate AI responses (create_response: false).
    """

    def __init__(self, api_key: str, loop: asyncio.AbstractEventLoop,
                 gw: GatewayClient, stop_event: asyncio.Event,
                 input_device=None, alsa_output: str = ALSA_OUTPUT,
                 session_key: str = OPENCLAW_SESSION):
        self.api_key      = api_key
        self.loop         = loop
        self.gw           = gw
        self.stop_event   = stop_event
        self.input_device = input_device
        self.alsa_output  = alsa_output
        self.session_key  = session_key
        self._mic_q       = asyncio.Queue(maxsize=200)
        self._busy        = asyncio.Event()   # set while Five is speaking
        self._cal_peaks: list[int] = []       # raw peaks collected during calibration
        self._calibrating = False
        self._active      = False             # start silent; wake phrase enables voice
        self._monitoring  = False             # passive capture-only mode (no Five, no TTS)
        self._multilang   = False             # False = only show/process EN/ZH

    def _mic_cb(self, indata, frames, time_info, status):
        raw = indata[::RESAMPLE_RATIO, 0]
        raw_peak = int(np.max(np.abs(raw)))
        with _mic_level_lock:
            _mic_level_current[0] = raw_peak
        # While calibrating, record raw peaks (no gain/gate applied, mic suppression off)
        if self._calibrating:
            self.loop.call_soon_threadsafe(self._cal_peaks.append, raw_peak)
            return
        if self._busy.is_set():
            return  # discard mic input while Five is speaking to prevent feedback
        if raw_peak < MIC_GATE_PEAK:
            out_arr = np.zeros_like(raw)
        else:
            boosted = raw.astype(np.float32) * MIC_GAIN
            out_arr = np.clip(boosted, -32768, 32767).astype(np.int16)
        self.loop.call_soon_threadsafe(self._enqueue_mic, out_arr.tobytes())

    def _enqueue_mic(self, data: bytes):
        try:
            self._mic_q.put_nowait(data)
        except asyncio.QueueFull:
            pass

    async def _run_calibration(self):
        """Measure ambient noise via the live mic stream and update MIC_GATE_PEAK."""
        global MIC_GATE_PEAK
        await asyncio.get_running_loop().run_in_executor(
            None, speak, "Calibrating mic. Stay quiet for three seconds.", self.alsa_output
        )
        self._cal_peaks.clear()
        self._calibrating = True
        await asyncio.sleep(3.0)
        self._calibrating = False
        peaks = self._cal_peaks[2:]  # discard startup frames
        if not peaks:
            await asyncio.get_running_loop().run_in_executor(
                None, speak, "Calibration failed. No mic data.", self.alsa_output
            )
            return
        noise_peak = max(peaks)
        new_gate = max(MIC_GATE_MIN, min(MIC_GATE_MAX, int(noise_peak * 1.25)))
        MIC_GATE_PEAK = new_gate
        log.info("Calibration: noise_peak=%d → MIC_GATE_PEAK=%d", noise_peak, new_gate)
        # Persist to service file so it survives restarts
        _update_service_gate(new_gate)
        await asyncio.get_running_loop().run_in_executor(
            None, speak,
            f"Done. Noise gate set to {new_gate}. Speak normally now.",
            self.alsa_output
        )

    async def _send_mic(self, ws):
        while not self.stop_event.is_set():
            try:
                chunk = await asyncio.wait_for(self._mic_q.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if self._busy.is_set():
                continue
            await ws.send(json.dumps({
                "type":  "input_audio_buffer.append",
                "audio": base64.b64encode(chunk).decode(),
            }))

    async def _handle_transcript(self, transcript: str):
        # Default to Simplified Chinese (transcriber often returns Traditional)
        transcript = _to_simplified(transcript)

        # Language gate: by default only English/Chinese are shown/processed.
        # Other languages (often noise hallucinations) are dropped unless the
        # user enables multi-language mode.
        if not self._multilang and not _is_english_or_chinese(transcript):
            log.debug("Dropped non-EN/ZH (multilang off): %r", transcript)
            return

        # Monitoring-only mode: passively log captured segments (no Five/TTS).
        # Shows everything verbatim — including noise — so capture quality is visible.
        if self._monitoring:
            t = transcript.strip()
            if t:
                log.info("Monitor: %s", t)
                _log_entry("monitor", t)
            return

        # Noise hallucination filter: drop consonant-heavy gibberish from background
        # noise that slipped past the VAD. (Monitoring mode is exempt so you can
        # still diagnose what the transcriber produces.)
        if _is_likely_noise(transcript):
            log.debug("Dropped noise hallucination: %r", transcript)
            return

        normalized = transcript.strip().rstrip(".!?,").lower()

        # Wake phrase — always checked regardless of active state
        if _matches_phrase(normalized, WAKE_PHRASES):
            self._busy.set()
            try:
                if not self._active:
                    self._active = True
                    log.info("Wake phrase detected — voice active")
                    _log_entry("system", "Voice activated")
                    await asyncio.get_running_loop().run_in_executor(
                        None, speak, "I'm listening.", self.alsa_output
                    )
                else:
                    log.info("Wake phrase detected — already active")
                    await asyncio.get_running_loop().run_in_executor(
                        None, speak, "Yes, I'm here.", self.alsa_output
                    )
            finally:
                self._busy.clear()
            return

        # Sleep phrase — only meaningful when active
        if _matches_phrase(normalized, SLEEP_PHRASES):
            if self._active:
                self._active = False
                log.info("Sleep phrase detected — going silent")
                _log_entry("system", "Voice silenced")
                self._busy.set()
                try:
                    await asyncio.get_running_loop().run_in_executor(
                        None, speak, "Going silent now. Say Five wake up to resume.", self.alsa_output
                    )
                finally:
                    self._busy.clear()
            return

        # Calibration — works in both modes (audio feedback either way)
        if normalized in CALIBRATE_PHRASES:
            log.info("Voice command: calibrate mic")
            asyncio.create_task(self._run_calibration())
            return

        # All other speech: only route to Five when active
        if not self._active:
            log.debug("Silent mode — ignoring: %s", transcript)
            return

        self._busy.set()
        try:
            log.info("Routing to Five: %s", transcript)
            _log_entry("you", transcript)
            _log_entry("thinking", "Five is thinking...")  # live counter shown on dashboard
            # Prefix tells Five to ignore cron/heartbeat background context
            voice_msg = f"[voice] {transcript}"
            reply = await self.gw.ask(voice_msg, session_key=self.session_key)
            log.info("Five: %s", reply)
            _log_entry("five", reply)
            await asyncio.get_running_loop().run_in_executor(
                None, speak, reply, self.alsa_output
            )
        except asyncio.TimeoutError:
            log.error("OpenClaw agent timed out")
            await asyncio.get_running_loop().run_in_executor(
                None, speak, "Sorry, I timed out on that.", self.alsa_output
            )
        except Exception as e:
            log.error("Error routing transcript: %s", e)
        finally:
            self._busy.clear()

    async def _recv_ws(self, ws):
        async for raw in ws:
            if self.stop_event.is_set():
                break
            msg = json.loads(raw)
            t   = msg.get("type", "")

            if t in ("conversation.item.done", "conversation.item.input_audio_transcription.completed"):
                # transcription endpoint: transcript in item.content[].transcript
                # old realtime endpoint: transcript in top-level .transcript
                transcript = msg.get("transcript", "")
                if not transcript:
                    for chunk in msg.get("item", {}).get("content", []):
                        if chunk.get("type") == "input_audio" and chunk.get("transcript"):
                            transcript = chunk["transcript"]
                            break
                transcript = transcript.strip()
                if transcript and not self._busy.is_set():
                    log.info("You: %s", transcript)
                    asyncio.create_task(self._handle_transcript(transcript))

            elif t == "error":
                log.error("OpenAI error: %s", msg.get("error", msg))

            elif t not in (
                "input_audio_buffer.speech_started",
                "input_audio_buffer.speech_stopped",
                "input_audio_buffer.committed",
                "conversation.item.created",
                "conversation.item.added",
                "conversation.item.done",
                "conversation.item.input_audio_transcription.delta",
                "transcription_session.updated",
                "session.updated",
                "session.created",
            ):
                log.debug("OpenAI event: %s", t)

    async def run(self):
        log.info("Connecting to OpenAI Realtime API (STT mode)…")
        async with websockets.connect(
            OPENAI_WS_URL,
            additional_headers={
                "Authorization": f"Bearer {self.api_key}",
            },
            ping_interval=20,
            ping_timeout=10,
        ) as ws:
            await ws.send(json.dumps({
                "type": "session.update",
                "session": {
                    "type": "transcription",
                    "audio": {
                        "input": {
                            "transcription": {"model": OPENAI_TRANSCRIBE_MODEL},
                            "turn_detection": {
                                "type":                "server_vad",
                                "threshold":           0.45,  # AGC normalises speech; 0.45 is sensitive enough to catch all speech while noise suppression prevents false triggers
                                "prefix_padding_ms":   300,   # capture sentence lead-in ("Five,…")
                                # WebRTC AGC noise-suppression turns brief
                                # inter-word gaps into hard silence; 600ms ended
                                # turns mid-sentence ("Can you" → rest lost while
                                # busy). 1100ms keeps a full sentence as one turn.
                                "silence_duration_ms": 1100,
                            },
                        },
                    },
                },
            }))
            log.info("Session active — speak now (routed through Five / OpenClaw)")

            in_stream = sd.InputStream(
                samplerate=DEVICE_RATE, channels=CHANNELS, dtype="int16",
                blocksize=DEVICE_BLOCKSIZE, callback=self._mic_cb,
                device=self.input_device,
            )

            with in_stream:
                tasks = [
                    asyncio.create_task(self._send_mic(ws)),
                    asyncio.create_task(self._recv_ws(ws)),
                    asyncio.create_task(self.stop_event.wait()),
                ]
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()

# ── HTTP toggle server ────────────────────────────────────────────────────────

def start_http_server(port: int, on_stop, session_ref: list):
    """session_ref is a one-element list holding the current RealtimeSession (or None)."""
    def _html(handler, code: int, body: str):
        data = body.encode()
        handler.send_response(code)
        handler.send_header("Content-Type",   "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.debug("[http] %s", fmt % args)

        def do_GET(self):
            sess = session_ref[0]
            if self.path == "/stop":
                _html(self, 200, "<h2>OpenClaw RealTimeTalk: stopping…</h2>")
                on_stop()
            elif self.path == "/restart":
                _html(self, 200, "<h2>Restarting…</h2><p>Page will reload in 5 seconds.</p><script>setTimeout(()=>location.href='/dashboard',5000)</script>")
                threading.Thread(target=lambda: (
                    __import__('time').sleep(1),
                    __import__('subprocess').run(['systemctl','--user','restart','openclaw-realtimetalk'])
                ), daemon=True).start()
            elif self.path == "/wake":
                if sess and not sess._active:
                    sess._active = True
                    log.info("HTTP wake")
                self.send_response(302)
                self.send_header("Location", "/log")
                self.end_headers()
            elif self.path == "/sleep":
                if sess and sess._active:
                    sess._active = False
                    log.info("HTTP sleep")
                self.send_response(302)
                self.send_header("Location", "/log")
                self.end_headers()
            elif self.path in ("/monitor", "/monitor/start", "/monitor/stop"):
                # Passive capture-only monitoring (no Five, no TTS).
                # /monitor toggles; /monitor/start and /monitor/stop are explicit.
                if sess:
                    if self.path == "/monitor/start":
                        new_state = True
                    elif self.path == "/monitor/stop":
                        new_state = False
                    else:
                        new_state = not sess._monitoring
                    if new_state and not sess._monitoring:
                        sess._monitoring = True
                        sess._active = False  # ensure fully silent
                        log.info("HTTP monitor START — capture-only")
                        _log_entry("system", "Monitoring only - capture display, silent")
                    elif not new_state and sess._monitoring:
                        sess._monitoring = False
                        log.info("HTTP monitor STOP")
                        _log_entry("system", "Monitoring stopped")
                self.send_response(302)
                self.send_header("Location", "/dashboard")
                self.end_headers()
            elif self.path == "/multilang":
                # Toggle multi-language mode. Off (default) = only English/
                # Chinese shown/processed; other languages dropped.
                if sess:
                    sess._multilang = not sess._multilang
                    state_txt = "ON (all languages)" if sess._multilang else "OFF (EN/ZH only)"
                    log.info("HTTP multilang %s", state_txt)
                    _log_entry("system", f"Multi-language mode: {state_txt}")
                self.send_response(302)
                self.send_header("Location", "/dashboard")
                self.end_headers()
            elif self.path == "/reset":
                # Clear the on-screen conversation/capture log
                CONVERSATION_LOG.clear()
                log.info("HTTP reset — conversation log cleared")
                self.send_response(302)
                self.send_header("Location", "/dashboard")
                self.end_headers()
            elif self.path in ("/calibrate", "/speaker-cal") and "/" not in self.path[1:]:
                # Legacy top-level routes redirect to combined page (sub-routes like /speaker-cal/run pass through)
                # Note: /calibrate and /speaker-cal exactly (no sub-path)
                self.send_response(302)
                self.send_header("Location", "/calibration")
                self.end_headers()
            elif self.path == "/calibration":
                # Determine headset mode: manual override > auto-detection
                _override = _cal_mode_override[0]
                if _override == "headset":
                    is_headset = True
                elif _override == "speaker":
                    is_headset = False
                else:
                    is_headset = _detect_headset()
                _mode_label = ("Headset" if is_headset else "Speaker") + \
                              (" (auto)" if _override is None else " (manual)")
                ds = _get_device_status()
                gate = _mic_gate_ref[0]
                prev = _speaker_cal_result
                prev_html = ""
                if prev:
                    snr_target = prev.get("snr_target", 5.0)
                    def _row(m):
                        snr = m.get("snr", 0)
                        col = "#5f5" if snr >= snr_target else "#aaa"
                        return (f'<tr><td>PW {m.get("pw","-")}% SW {int(m.get("sw",1)*100)}%</td>'
                                f'<td style="color:{col}">SNR {snr:.1f}x</td></tr>')
                    spk_rows = "".join(_row(m) for m in prev.get("measurements", []))
                    sw_pct = int(prev.get("safe_sw_vol", 1.0) * 100)
                    warn = ('<div class="warn">Mic cannot hear speaker — use Manual adjustment below.</div>'
                            ) if prev.get("status") == "no_mic" else ""
                    prev_html = (warn +
                        f'<p>Last result: PW <b>{prev.get("safe_vol")}%</b> + software <b>{sw_pct}%</b></p>'
                        f'<table class="snrtbl"><tr><th>Level</th><th>Mic SNR</th></tr>{spk_rows}</table>')
                headset_notice = ('<p class="info" style="margin:4px 0;color:#fa0;">'
                    'Headset mode — use Manual adjustment to set volume.</p>'
                    ) if is_headset else ""
                spk_adj_section = f"""
<div class="sect"><h4>Manual adjustment</h4>
{headset_notice}
<table style="border-collapse:collapse;margin:4px 0;width:100%;">
  <tr>
    <td style="color:#aaa;font-size:13px;width:32px;">Vol</td>
    <td style="font-weight:bold;font-size:1.1em;width:52px;" id="volval">{ds["spk_vol"]}</td>
    <td><div class="row" style="margin:0;gap:5px;">
      <button class="bQ" onclick="adjVol(-10)">− Quieter</button>
      <button class="bL" onclick="adjVol(+10)">+ Louder</button>
    </div></td>
  </tr>
  <tr>
    <td style="color:#aaa;font-size:13px;">SW</td>
    <td style="font-weight:bold;font-size:1.1em;" id="swval">{ds["sw_pct"]}%</td>
    <td><div class="row" style="margin:0;gap:5px;">
      <button class="bQ" onclick="adjSW(-10)">− Softer</button>
      <button class="bL" onclick="adjSW(+10)">+ Louder</button>
    </div></td>
  </tr>
</table>
<div class="row" style="margin:4px 0;">
  <button class="bP" onclick="startLoop()">Play test</button>
  <button class="bS" onclick="stopLoop()">Stop</button>
  <button class="bSet" onclick="setLevel()">Set this level</button>
</div>
<div id="mstatus" class="info"></div></div>"""
                auto_cal_section = ("" if is_headset else f"""
<div class="sect"><h4>Auto calibration (mic leakage)</h4>
<p class="info">Plays 440 Hz tone at increasing volumes and measures mic response.</p>
<div id="calstatus">Ready.</div>
{prev_html}
<div class="row"><button id="acbtn" onclick="runCal()">Run auto calibration</button></div>
</div>""")
                body = f"""<!DOCTYPE html><html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Calibration</title>
<style>
body{{font-family:sans-serif;font-size:14px;background:#111;color:#eee;padding:8px 12px;max-width:640px;}}
h3{{margin:0 0 6px;font-size:16px;}} h4{{margin:4px 0 2px;font-size:14px;color:#9cf;}}
.info{{color:#aaa;font-size:13px;margin:2px 0;}}
.warn{{background:#5a1a00;border-radius:5px;padding:5px 8px;margin-bottom:4px;font-size:13px;}}
.devpanel{{background:#1a1a2a;border-radius:6px;padding:6px 10px;margin-bottom:6px;font-size:13px;line-height:1.6;}}
.devpanel b{{color:#eee;}}
.sect{{border-top:1px solid #333;margin-top:8px;padding-top:6px;}}
canvas{{width:100%;height:36px;border-radius:5px;display:block;margin:4px 0;}}
#micinfo{{font-size:13px;color:#aaa;margin:2px 0;min-height:16px;}}
#micresult{{margin-top:6px;padding:6px;background:#1a3a1a;border-radius:5px;font-size:13px;color:#7f7;display:none;}}
#calstatus{{margin:4px 0;font-size:13px;min-height:16px;}}
#mstatus{{margin-top:3px;font-size:13px;}}
.row{{display:flex;gap:6px;flex-wrap:wrap;margin:4px 0;}}
button{{padding:7px 14px;border:none;color:#fff;border-radius:6px;font-size:13px;cursor:pointer;}}
#micbtn{{background:#2a5;}} #micbtn:disabled{{background:#555;cursor:default;}}
#acbtn{{background:#2a5;}} #acbtn:disabled{{background:#555;cursor:default;}}
.bQ{{background:#555;}} .bL{{background:#2a5;}} .bP{{background:#226;}}
.bS{{background:#622;}} .bSet{{background:#a62;}}
.snrtbl{{border-collapse:collapse;font-size:12px;margin:4px 0;width:100%;}}
.snrtbl th,.snrtbl td{{border:1px solid #333;padding:3px 6px;text-align:left;}}
.snrtbl tr.active-row{{background:#1a3a1a;}}
.use-btn{{padding:3px 10px;font-size:12px;background:#446;border:none;color:#fff;border-radius:4px;cursor:pointer;white-space:nowrap;}}
.use-btn:hover{{background:#558;}}
.use-btn.active{{background:#272;cursor:default;}}
#devbtn{{background:#446;font-size:13px;padding:6px 14px;}}
#devtoggle{{color:#9cf;cursor:pointer;font-size:13px;background:none;border:none;padding:0;margin-left:6px;}}
#devlist{{margin-top:6px;}}
#devmsg{{font-size:13px;color:#fa0;}}
a{{color:#7af;font-size:14px;}}
</style></head><body>
<div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:4px;">
  <h3 style="margin:0;">Calibration</h3>
  <a href="/dashboard" style="font-size:13px;color:#7af;">← Dashboard</a>
</div>
<div class="devpanel" id="curdev">
  <b>Mic:</b> {ds["mic"]} &nbsp;|&nbsp; Gate: <span id="panelgate">{ds["gate"]}</span> &nbsp;|&nbsp; Gain: {ds["gain"]}x<br>
  <b>Speaker:</b> {ds["speaker_name"]} &nbsp;|&nbsp; Vol: <span id="panelvol">{ds["spk_vol"]}</span> &nbsp;|&nbsp; SW: <span id="panelsw">{ds["sw_pct"]}%</span>
</div>
<div style="display:flex;align-items:center;gap:8px;margin:4px 0 6px;">
  <span style="font-size:12px;color:#aaa;">Cal mode:</span>
  <b style="font-size:13px;color:{'#fa0' if is_headset else '#5f5'};">{_mode_label}</b>
  <button onclick="setCalMode('headset')" style="padding:3px 10px;font-size:12px;background:{'#622' if is_headset and _override else '#444'};border:none;color:#fff;border-radius:4px;cursor:pointer;">Headset</button>
  <button onclick="setCalMode('speaker')" style="padding:3px 10px;font-size:12px;background:{'#2a5' if not is_headset and _override else '#444'};border:none;color:#fff;border-radius:4px;cursor:pointer;">Speaker</button>
  <button onclick="setCalMode('auto')" style="padding:3px 10px;font-size:12px;background:{'#446' if _override is None else '#444'};border:none;color:#fff;border-radius:4px;cursor:pointer;">Auto</button>
</div>
{spk_adj_section}
<div style="margin:10px 0 4px;display:flex;align-items:center;gap:10px;">
  <button id="devbtn" onclick="toggleDevices()">Audio Devices</button>
  <span id="devtoggle" onclick="toggleDevices()">▼ expand</span>
</div>
<div id="devlist" style="display:none;">
  <div id="devout" style="font-size:14px;">Loading…</div>
</div>


<div class="sect"><h4>Mic calibration</h4>
<p class="info">Yellow line = gate threshold. Speech above the line passes; noise below is silenced.</p>
<canvas id="meter" height="36"></canvas>
<div id="micinfo" style="font-size:12px;color:#aaa;margin:2px 0;min-height:14px;"></div>
<div style="display:flex;align-items:center;gap:8px;margin:6px 0;">
  <span style="font-size:12px;color:#aaa;white-space:nowrap;">Gate:</span>
  <input type="range" id="gateslider" min="{MIC_GATE_MIN}" max="{MIC_GATE_MAX}" step="25"
         value="{gate}" style="flex:1;accent-color:#ff0;" oninput="onGateSlide(this.value)"
         onchange="saveGate(this.value)">
  <span id="gateval" style="font-size:13px;color:#ff0;font-weight:bold;width:36px;text-align:right;">{gate}</span>
</div>
<div id="micresult"></div>
<div class="row">
  <button id="micbtn" onclick="startMicCal()">Auto-calibrate (3 sec quiet)</button>
</div>
</div>
{auto_cal_section}
<p><a href="/dashboard">← Dashboard</a></p>
<script>
/* --- Mic level meter --- */
const MAX=32768, gate0={gate};
let calRunning=false;
const canvas=document.getElementById('meter');
const ctx=canvas.getContext('2d');
const micinfo=document.getElementById('micinfo');
const micresult=document.getElementById('micresult');
const micbtn=document.getElementById('micbtn');
const grad=(w)=>{{const g=ctx.createLinearGradient(0,0,w,0);
  g.addColorStop(0,'#1155cc');g.addColorStop(0.35,'#22bb55');g.addColorStop(0.75,'#cc4411');return g;}};
function draw(peak,gateVal){{
  const W=canvas.width,H=canvas.height;
  ctx.clearRect(0,0,W,H);ctx.fillStyle='#222';ctx.fillRect(0,0,W,H);
  const ratio=Math.min(peak/MAX,1);
  ctx.fillStyle=grad(W);ctx.fillRect(0,0,W*ratio,H);
  const gx=Math.min((gateVal/MAX)*W,W-2);
  ctx.strokeStyle='#ffee00';ctx.lineWidth=2;
  ctx.beginPath();ctx.moveTo(gx,0);ctx.lineTo(gx,H);ctx.stroke();
  ctx.fillStyle='#eee';ctx.font='11px monospace';
  ctx.fillText('peak:'+peak+'  gate:'+gateVal,6,H-6);
}}
const es=new EventSource('/levels');
es.onmessage=e=>{{
  const [peak,gate]=e.data.split(',').map(Number);
  draw(peak,gate);
  // Keep slider in sync with live gate (e.g. after auto-calibrate)
  const sl=document.getElementById('gateslider');
  const gv=document.getElementById('gateval');
  if(sl && !sl.matches(':active')){{ sl.value=gate; if(gv) gv.textContent=gate; }}
  if(!calRunning) micinfo.textContent=
    peak<gate?'Below gate — noise silenced':
    peak<MAX*0.5?'Speech range':'Very loud';
}};
let _gateTimer=null;
function onGateSlide(val){{
  document.getElementById('gateval').textContent=val;
  // Clear stale auto-calibrate result when user manually adjusts
  const r=document.getElementById('micresult');
  if(r) r.style.display='none';
  clearTimeout(_gateTimer);
  _gateTimer=setTimeout(()=>fetch('/mic-gate/set?value='+val),150);
}}
function saveGate(val){{
  // Persist to service file on mouseup
  clearTimeout(_gateTimer);
  fetch('/mic-gate/set?value='+val).then(r=>r.json()).then(d=>{{
    document.getElementById('gateval').textContent=d.gate;
  }});
}}
function startMicCal(){{
  calRunning=true; micbtn.disabled=true;
  let secs=3; micinfo.textContent='Stay quiet… '+secs+'s';
  const t=setInterval(()=>{{secs--;micinfo.textContent=secs>0?'Stay quiet… '+secs+'s':'Measuring…';}},1000);
  fetch('/calibrate/run').then(r=>r.json()).then(d=>{{
    clearInterval(t); calRunning=false;
    micresult.style.display='block';
    micresult.innerHTML='Done! New gate: <b>'+d.gate+'</b> (noise peak: '+d.noise_peak+')';
    micinfo.textContent='Yellow line updated.'; micbtn.disabled=false;
    // Auto-hide after announcement has played (~4s)
    setTimeout(()=>{{micresult.style.display='none';}},4000);
    // Sync slider to the new gate from auto-calibrate
    const sl=document.getElementById('gateslider');
    const gv=document.getElementById('gateval');
    if(sl){{ sl.value=d.gate; }} if(gv){{ gv.textContent=d.gate; }}
  }}).catch(()=>{{clearInterval(t);calRunning=false;micbtn.disabled=false;
    micinfo.textContent='Calibration failed — try again.';}});
}}
/* --- Speaker controls --- */
function upd(){{fetch('/speaker-cal/vol').then(r=>r.json()).then(d=>{{
  const vv=document.getElementById('volval');
  const sv=document.getElementById('swval');
  if(vv) vv.textContent=d.spk_vol;
  if(sv) sv.textContent=d.sw_pct+'%';
  // Keep top panel in sync
  const pv=document.getElementById('panelvol');
  const ps=document.getElementById('panelsw');
  const pg=document.getElementById('panelgate');
  if(pv) pv.textContent=d.spk_vol;
  if(ps) ps.textContent=d.sw_pct+'%';
  if(pg) pg.textContent=d.gate;
}});}}
function adjVol(d){{fetch('/speaker-cal/adjust?type=vol&delta='+d).then(()=>upd());}}
function adjSW(d){{fetch('/speaker-cal/adjust?type=sw&delta='+d).then(()=>upd());}}
function adj(d){{adjVol(d);}}

function adj(d){{fetch('/speaker-cal/adjust?delta='+d).then(()=>upd());}}
function startLoop(){{fetch('/speaker-cal/loop-start').then(()=>{{
  const m=document.getElementById('mstatus');if(m)m.textContent='Playing test loop…';}});}}
function stopLoop(){{fetch('/speaker-cal/loop-stop').then(()=>{{
  const m=document.getElementById('mstatus');if(m)m.textContent='Stopped.';}});}}
function setLevel(){{fetch('/speaker-cal/set').then(r=>r.json()).then(d=>{{
  const m=document.getElementById('mstatus');
  if(m)m.textContent='Level saved: '+d.spk_vol+' PW, '+d.sw_pct+'% SW';
  stopLoop(); setTimeout(()=>location.href='/dashboard',3000);}});}}
function runCal(){{
  stopLoop();
  const btn=document.getElementById('acbtn');
  const st=document.getElementById('calstatus');
  if(btn)btn.disabled=true; if(st)st.textContent='Calibrating…';
  fetch('/speaker-cal/run').then(r=>r.json()).then(d=>{{
    if(btn)btn.disabled=false;
    if(st)st.innerHTML=d.status=='no_mic'?'Mic cannot hear speaker — adjust manually.':
      'Set to PW <b>'+d.safe_vol+'%</b> SW <b>'+Math.round(d.safe_sw_vol*100)+'%</b>';
    setTimeout(()=>location.reload(),4000);
  }}).catch(e=>{{if(btn)btn.disabled=false;if(st)st.textContent='Error: '+e;}});
}}
setInterval(upd,2000);
/* --- Device selection --- */
let _devExpanded=false, _devTimer=null;
function toggleDevices(){{
  _devExpanded=!_devExpanded;
  const list=document.getElementById('devlist');
  const tog=document.getElementById('devtoggle');
  list.style.display=_devExpanded?'block':'none';
  tog.textContent=_devExpanded?'▲ collapse':'▼ expand';
  if(_devExpanded){{
    loadDevices();
    _devTimer=setInterval(loadDevices, 2000);
  }} else {{
    if(_devTimer){{ clearInterval(_devTimer); _devTimer=null; }}
  }}
}}
function loadDevices(){{
  const out=document.getElementById('devout');
  if(!out) return;
  // Don't show "Loading…" on refresh — only on first open (when empty)
  if(!out.dataset.loaded) out.textContent='Loading…';
  fetch('/device-status').then(r=>r.json()).then(d=>{{
    if(d.error){{out.innerHTML='<span style="color:#f55">Error: '+d.error+'</span>';return;}}
    let h='';
    h+='<p style="margin:4px 0 8px;color:#9cf;font-weight:bold">Speakers</p>';
    h+='<table class="snrtbl"><tr><th>Name</th><th>Card</th><th>State</th><th></th></tr>';
    (d.sinks||[]).forEach(s=>{{
      if(s.name.startsWith('rtt_agc')||s.name.includes('monitor')) return;
      const active=(s.name===d.default_sink);
      h+='<tr'+(active?' class="active-row"':'')+'>'
        +'<td>'+(s.desc||s.name)+(active?' <span style="color:#5f5">✓</span>':'')+'</td>'
        +'<td style="white-space:nowrap">'+(s.card?'card '+s.card:'BT')+'</td>'
        +'<td>'+(s.state==='SUSPENDED'?'Idle':s.state==='RUNNING'?'<span style="color:#5f5">Running</span>':s.state)+'</td>'
        +'<td><button class="use-btn'+(active?' active':'')+'"'
        +' data-dtype="sink" data-dname="'+s.name+'"'
        +' onclick="setDevice(this.dataset.dtype,this.dataset.dname)"'
        +(active?' disabled':'')
        +'>'+(active?'Active':'Use')+'</button></td></tr>';
    }});
    h+='</table>';
    h+='<p style="margin:12px 0 8px;color:#9cf;font-weight:bold">Microphones</p>';
    h+='<table class="snrtbl"><tr><th>Name</th><th>Card</th><th>State</th><th></th></tr>';
    (d.sources||[]).forEach(s=>{{
      if(s.name.includes('monitor')||s.name==='rtt_agc_sink'||s.name==='rtt_agc_source') return;
      const active=(s.name===d.default_source);
      h+='<tr'+(active?' class="active-row"':'')+'>'
        +'<td>'+(s.desc||s.name)+(active?' <span style="color:#5f5">✓</span>':'')+'</td>'
        +'<td style="white-space:nowrap">'+(s.card?'card '+s.card:'-')+'</td>'
        +'<td>'+(s.state==='SUSPENDED'?'Idle':s.state==='RUNNING'?'<span style="color:#5f5">Running</span>':s.state)+'</td>'
        +'<td><button class="use-btn'+(active?' active':'')+'"'
        +' data-dtype="source" data-dname="'+s.name+'"'
        +' onclick="setDevice(this.dataset.dtype,this.dataset.dname)"'
        +(active?' disabled':'')
        +'>'+(active?'Active':'Use')+'</button></td></tr>';
    }});
    h+='</table>';
    // Reserved status area — fixed min-height so no layout shift when message appears/clears
    h+='<div id="devmsg" style="min-height:52px;padding:6px 0;font-size:14px;color:#fa0;"></div>';
    if((d.alsa_cards||[]).length){{
      h+='<p style="margin:6px 0 2px;font-size:12px;color:#666;">ALSA: '
        +d.alsa_cards.map(c=>'<span style="color:#888">'+c.num+'</span> '+c.name).join(' &nbsp;|&nbsp; ')+'</p>';
    }}
    out.innerHTML=h;
    out.dataset.loaded='1';
  }}).catch(e=>{{out.innerHTML='<span style="color:#f55">Failed: '+e+'</span>';}});
}}
function setDevice(type,name){{
  const msg=document.getElementById('devmsg');
  msg.textContent=(type==='sink'?'Setting speaker':'Setting mic')+': '+name+' — restarting audio in 1s…';
  fetch('/device-set?type='+type+'&name='+encodeURIComponent(name))
    .then(r=>r.json()).then(d=>{{
      msg.textContent=d.msg||'Done.';
      if(d.ok){{
        sessionStorage.setItem('devExpanded','1');
        if(_devTimer){{ clearInterval(_devTimer); _devTimer=null; }}
        setTimeout(()=>location.reload(),4500);
      }} else msg.style.color='#f55';
    }}).catch(e=>{{msg.textContent='Error: '+e; msg.style.color='#f55';}});
}}
// Restore expanded state after a device-switch reload
if(sessionStorage.getItem('devExpanded')){{
  sessionStorage.removeItem('devExpanded');
  toggleDevices();
}}
function setCalMode(mode){{
  fetch('/cal-mode?mode='+mode).then(()=>location.reload());
}}
</script></body></html>"""
                _html(self, 200, body)
            elif self.path.startswith("/cal-mode"):
                import json as _json, urllib.parse as _up
                qs   = _up.parse_qs(_up.urlparse(self.path).query)
                mode = qs.get("mode", ["auto"])[0]   # "auto", "headset", "speaker"
                if mode in ("auto", "headset", "speaker"):
                    _cal_mode_override[0] = None if mode == "auto" else mode
                    log.info("Cal mode override → %s", mode)
                resp = _json.dumps({"mode": mode}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            elif self.path.startswith("/mic-gate/set"):
                import json as _json, urllib.parse as _up
                qs   = _up.parse_qs(_up.urlparse(self.path).query)
                val  = int(qs.get("value", [_mic_gate_ref[0]])[0])
                val  = max(MIC_GATE_MIN, min(MIC_GATE_MAX, val))
                _mic_gate_ref[0] = val
                globals()['MIC_GATE_PEAK'] = val
                _update_service_gate(val)
                log.info("Mic gate set to %d via slider", val)
                resp = _json.dumps({"gate": val}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            elif self.path == "/calibrate/run":
                if sess:
                    import asyncio as _aio, json as _json, time as _time
                    # collect 3s of mic samples (audio thread already fills _mic_level_current)
                    peaks = []
                    for _ in range(30):
                        _time.sleep(0.1)
                        with _mic_level_lock:
                            peaks.append(_mic_level_current[0])
                    peaks = peaks[2:]
                    noise_peak = max(peaks) if peaks else 0
                    new_gate = max(MIC_GATE_MIN, min(MIC_GATE_MAX, int(noise_peak * 1.25)))
                    _mic_gate_ref[0] = new_gate
                    MIC_GATE_PEAK = new_gate
                    log.info("HTTP calibration: noise_peak=%d → gate=%d", noise_peak, new_gate)
                    _update_service_gate(new_gate)
                    # speak confirmation in background thread (we're already in HTTP thread)
                    import threading as _t
                    _t.Thread(target=speak,
                              args=(f"Noise gate set to {new_gate}.", sess.alsa_output),
                              daemon=True).start()
                    resp = _json.dumps({"gate": new_gate, "noise_peak": noise_peak}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(resp)))
                    self.end_headers()
                    self.wfile.write(resp)
                else:
                    _html(self, 503, "<h2>No active session</h2>")

            elif self.path == "/speaker-cal":
                is_headset = _detect_headset()
                ds = _get_device_status()
                if is_headset:
                    # Headset mode: interactive play+adjust (can't use mic leakage measurement)
                    body = f"""<!DOCTYPE html><html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Speaker Calibration — Headset</title>
<style>body{{font-family:sans-serif;font-size:17px;background:#111;color:#eee;padding:16px;}}
h3{{margin:0 0 8px;}} .info{{color:#aaa;font-size:16px;margin:6px 0;}}
#vol{{font-size:2em;font-weight:bold;margin:16px 0;text-align:center;}}
.row{{display:flex;gap:10px;justify-content:center;margin:8px 0;}}
button{{padding:12px 24px;border:none;color:#fff;border-radius:6px;font-size:16px;cursor:pointer;}}
#btnLouder{{background:#2a5;}} #btnQuieter{{background:#555;}}
#btnPlay{{background:#226;}} #btnStop{{background:#622;}} #btnSet{{background:#a62;}}
a{{color:#7af;}}</style></head><body>
<h3>Speaker Calibration — Headset</h3>
<div class="info">Headset detected: mic + speaker on same device.</div>
<div class="info">Acoustic leakage measurement is not suitable for headphones.<br>
Play the test sentence and adjust until comfortable.</div>
<div id="vol">Vol: {ds["spk_vol"]}  SW: {ds["sw_pct"]}%</div>
<div class="row">
  <button id="btnQuieter" onclick="adj(-10)">− Quieter</button>
  <button id="btnLouder"  onclick="adj(+10)">+ Louder</button>
</div>
<div class="row">
  <button id="btnPlay" onclick="startLoop()">Play test</button>
  <button id="btnStop" onclick="stopLoop()">Stop</button>
</div>
<div class="row">
  <button id="btnSet" onclick="setLevel()">✓ Set this level</button>
</div>
<div id="status" style="margin-top:12px;color:#aaa;font-size:13px;"></div>
<div class="sect">
<h4>Device status</h4>
<div class="row"><button id="devbtn" onclick="checkDevices()">Check Device Status</button></div>
<div id="devout" style="margin-top:10px;display:none;font-size:14px;"></div>
</div>
<p><a href="/dashboard">← Dashboard</a></p>
<script>
function upd(){{fetch('/speaker-cal/vol').then(r=>r.json()).then(d=>{{
  document.getElementById('vol').textContent='Vol: '+d.spk_vol+'  SW: '+d.sw_pct+'%';
}});}}
function adj(d){{fetch('/speaker-cal/adjust?delta='+d).then(()=>upd());}}
function startLoop(){{fetch('/speaker-cal/loop-start').then(()=>{{
  document.getElementById('status').textContent='Playing test sentence in loop…';
}});}}
function stopLoop(){{fetch('/speaker-cal/loop-stop').then(()=>{{
  document.getElementById('status').textContent='Stopped.';
}});}}
function setLevel(){{fetch('/speaker-cal/set').then(r=>r.json()).then(d=>{{
  document.getElementById('status').textContent='✓ Level saved: '+d.spk_vol+' PW, '+d.sw_pct+'% SW';
  stopLoop();
  setTimeout(()=>location.href='/dashboard',3000);
}});}}
setInterval(upd, 2000);
</script></body></html>"""
                else:
                    # Speaker mode: acoustic calibration via mic leakage
                    prev = _speaker_cal_result
                    prev_html = ""
                    if prev:
                        snr_target = prev.get("snr_target", 5.0)
                        def _row(m):
                            snr = m.get("snr", 0)
                            col = "#5f5" if snr >= snr_target else "#aaa"
                            return (f'<tr><td>PW {m.get("pw","-")}% SW {int(m.get("sw",1)*100)}%</td>'
                                    f'<td style="color:{col}">SNR {snr:.1f}×</td></tr>')
                        rows = "".join(_row(m) for m in prev.get("measurements", []))
                        sw_pct = int(prev.get("safe_sw_vol", 1.0) * 100)
                        warn = ('<div style="background:#5a1a00;border-radius:6px;padding:8px;'
                                'margin-bottom:6px;">Mic cannot hear speaker — use Manual adjustment below.</div>'
                                ) if prev.get("status") == "no_mic" else ""
                        prev_html = (
                            warn +
                            f'<h4>Last result: PW <b>{prev.get("safe_vol")}%</b> + '
                            f'software <b>{sw_pct}%</b></h4>'
                            f'<table border=1 style="border-collapse:collapse;font-size:12px">'
                            f'<tr><th>Level</th><th>Mic SNR</th></tr>{rows}</table>'
                        )
                    body = f"""<!DOCTYPE html><html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Speaker Calibration</title>
<style>body{{font-family:sans-serif;font-size:17px;background:#111;color:#eee;padding:16px;}}
h3,h4{{margin:0 0 8px;}} .info{{color:#aaa;font-size:13px;margin:4px 0;}}
#status{{margin:10px 0;font-size:14px;min-height:18px;}}
.sect{{border-top:1px solid #333;margin-top:16px;padding-top:12px;}}
#vol{{font-size:1.6em;font-weight:bold;margin:8px 0;}}
.row{{display:flex;gap:8px;flex-wrap:wrap;margin:6px 0;}}
button{{padding:12px 22px;border:none;color:#fff;border-radius:8px;font-size:17px;cursor:pointer;}}
#btn{{background:#2a5;}} #btn:disabled{{background:#555;}}
#devbtn{{background:#446;}} #devbtn:disabled{{background:#555;cursor:default;}}
.bAdj{{background:#335;}} .bPlay{{background:#226;}}
.bStop{{background:#622;}} .bSet{{background:#a62;}}
a{{color:#7af;}}</style></head><body>
<h3>Speaker Calibration</h3>
<div class="info">Speaker: {ds["speaker_name"]}</div>
<h4>Auto calibration (mic leakage)</h4>
<div class="info">Plays 440 Hz tone at increasing volumes, measures mic leakage via FFT.</div>
<div id="status">Ready.</div>
{prev_html}
<div class="row"><button id="btn" onclick="runCal()">Run auto calibration</button></div>
<div class="sect">
<h4>Manual adjustment</h4>
<div class="info">Play test sound and adjust until comfortable.</div>
<div id="vol">Vol: {ds["spk_vol"]}</div>
<div class="row">
  <button class="bAdj" onclick="adj(-10)">− Quieter</button>
  <button class="bAdj" onclick="adj(+10)">+ Louder</button>
  <button class="bPlay" onclick="startLoop()">Play test</button>
  <button class="bStop" onclick="stopLoop()">Stop</button>
  <button class="bSet"  onclick="setLevel()">✓ Set this level</button>
</div>
<div id="mstatus" style="color:#aaa;font-size:13px;margin-top:6px;"></div>
</div>
<p><a href="/dashboard">← Back</a></p>
<script>
function upd(){{fetch('/speaker-cal/vol').then(r=>r.json()).then(d=>{{
  document.getElementById('vol').textContent='Vol: '+d.spk_vol;
}});}}
function adj(d){{fetch('/speaker-cal/adjust?delta='+d).then(()=>upd());}}
function startLoop(){{fetch('/speaker-cal/loop-start').then(()=>{{
  document.getElementById('mstatus').textContent='Playing test loop…';
}});}}
function stopLoop(){{fetch('/speaker-cal/loop-stop').then(()=>{{
  document.getElementById('mstatus').textContent='Stopped.';
}});}}
function setLevel(){{fetch('/speaker-cal/set').then(r=>r.json()).then(d=>{{
  document.getElementById('mstatus').textContent='✓ Level saved: '+d.spk_vol;
  stopLoop();
  setTimeout(()=>location.href='/dashboard',3000);
}});}}
function runCal(){{
  stopLoop();
  document.getElementById('btn').disabled=true;
  document.getElementById('status').textContent='Calibrating…';
  fetch('/speaker-cal/run').then(r=>r.json()).then(d=>{{
    document.getElementById('btn').disabled=false;
    document.getElementById('status').innerHTML=
      (d.status=='no_mic' ? 'Mic cannot hear speaker — adjust manually.' :
      'Set to PW <b>'+d.safe_vol+'%</b> SW <b>'+Math.round(d.safe_sw_vol*100)+'%</b>');
    setTimeout(()=>location.reload(),4000);
  }}).catch(e=>{{
    document.getElementById('btn').disabled=false;
    document.getElementById('status').textContent='Error: '+e;
  }});
}}
setInterval(upd, 2000);
</script></body></html>"""
                _html(self, 200, body)

            elif self.path == "/speaker-cal/run":
                import json as _json
                result = run_speaker_calibration(
                    alsa_output=sess.alsa_output if sess else ALSA_OUTPUT
                    # calibration will auto-find the working output device
                )
                _speaker_cal_result.clear()
                _speaker_cal_result.update(result)
                # Update live session's alsa_output immediately (no restart needed)
                if sess and result.get("status") == "ok":
                    new_alsa = result.get("speaker_alsa", sess.alsa_output)
                    if new_alsa != sess.alsa_output:
                        log.info("Updating live session alsa_output: %s → %s",
                                 sess.alsa_output, new_alsa)
                        sess.alsa_output = new_alsa

                # Announce result ALWAYS at a guaranteed-audible level (the
                # calibrated level may be near-silent), then drop the speaker
                # to the calibrated operating level for normal use.
                if sess:
                    import threading as _t
                    sw  = result.get("safe_sw_vol", _cal_sw_volume)
                    pw  = result.get("safe_vol", CAL_FALLBACK_PW)
                    snk = _find_usb_speaker_sink()
                    def _cal_announce(sw=sw, pw=pw, snk=snk,
                                      alsa=sess.alsa_output,
                                      st=result.get("status", "ok")):
                        if st == "no_mic":
                            msg = ("Auto calibration could not measure the speaker — "
                                   "the microphone and speaker are not acoustically coupled. "
                                   f"Speaker set to {pw} percent. Use Manual adjustment to fine-tune.")
                        elif st == "ok":
                            msg = (f"Calibration done. Speaker set to {pw} percent.")
                        else:
                            msg = ("Calibration had a problem. Speaker set to a "
                                   "safe default. Use Manual adjustment.")
                        speak.__globals__["_skip_auto_reduce"] = True
                        try:
                            # Force an audible level for the announcement itself
                            if snk:
                                subprocess.run(["pactl", "set-sink-volume", snk,
                                                f"{CAL_ANNOUNCE_PW}%"],
                                               capture_output=True)
                            speak(msg, alsa, volume=CAL_ANNOUNCE_SW)
                            # Settle to the calibrated operating level
                            if snk:
                                subprocess.run(["pactl", "set-sink-volume", snk,
                                                f"{pw}%"], capture_output=True)
                        finally:
                            speak.__globals__["_skip_auto_reduce"] = False
                    _t.Thread(target=_cal_announce, daemon=True).start()
                resp = _json.dumps(result).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            elif self.path == "/speaker-cal/loop-start":
                # Headset mode: start looping test speech
                _headset_cal_loop[0] = True
                alsa = sess.alsa_output if sess else ALSA_OUTPUT
                def _loop(alsa=alsa):
                    import tempfile as _tf, os as _os
                    # Pre-render WAV once — avoids Piper startup overhead on every iteration
                    _pre = _tf.mktemp(suffix=".wav")
                    try:
                        import subprocess as _sp
                        _sp.run(
                            [PIPER_CMD, "--model", PIPER_VOICE_EN, "-f", _pre, "-q"],
                            input=b"This is an audio test. 1, 2, 3, 4, 5.",
                            capture_output=True, env=PIPER_ENV,
                        )
                        while _headset_cal_loop[0]:
                            _sp.run(["aplay", "-D", alsa, "-q", _pre],
                                    capture_output=True)
                    finally:
                        try: _os.unlink(_pre)
                        except FileNotFoundError: pass
                import threading as _t2
                _t2.Thread(target=_loop, daemon=True).start()
                _html(self, 200, "<p>Loop started.</p>")

            elif self.path == "/speaker-cal/loop-stop":
                _headset_cal_loop[0] = False
                _html(self, 200, "<p>Loop stopped.</p>")

            elif self.path.startswith("/speaker-cal/adjust"):
                import json as _json, re as _re5, urllib.parse as _up
                qs    = _up.parse_qs(_up.urlparse(self.path).query)
                delta = int(qs.get("delta", ["0"])[0])
                kind  = qs.get("type", ["vol"])[0]   # "vol" or "sw"

                def _snap10(val, d):
                    """Snap to nearest multiple of 10, then step by 10; min 1."""
                    snapped = round(val / 10) * 10
                    result  = snapped + d
                    return max(1, min(100, result))

                if kind == "sw":
                    # Adjust software gain (_cal_sw_volume)
                    cur_sw  = int(_cal_sw_volume * 100)
                    new_sw  = _snap10(cur_sw, delta)
                    globals()['_cal_sw_volume'] = new_sw / 100.0
                else:
                    # Adjust PipeWire volume of the default sink
                    sink = subprocess.run(["pactl","get-default-sink"],
                                          capture_output=True,text=True).stdout.strip()
                    if sink:
                        cur_out = subprocess.run(["pactl", "get-sink-volume", sink],
                                                 capture_output=True, text=True).stdout
                        m = _re5.search(r'(\d+)%', cur_out)
                        cur = int(m.group(1)) if m else 50
                        new_vol = _snap10(cur, delta)
                        subprocess.run(["pactl", "set-sink-volume", sink, f"{new_vol}%"],
                                       capture_output=True)
                resp = _json.dumps(_get_device_status()).encode()
                self.send_response(200); self.send_header("Content-Type","application/json")
                self.send_header("Content-Length", str(len(resp))); self.end_headers()
                self.wfile.write(resp)

            elif self.path == "/device-status":
                import json as _json, re as _re5
                try:
                    sinks_raw   = subprocess.run(["pactl", "list", "sinks"],
                                                  capture_output=True, text=True, timeout=8).stdout
                    sources_raw = subprocess.run(["pactl", "list", "sources"],
                                                  capture_output=True, text=True, timeout=8).stdout
                    cards_pb    = subprocess.run(["aplay",   "-l"],
                                                  capture_output=True, text=True, timeout=5).stdout
                    cards_cap   = subprocess.run(["arecord", "-l"],
                                                  capture_output=True, text=True, timeout=5).stdout
                    cards_raw   = cards_pb + cards_cap
                    def _parse_pw_blocks(raw, kind):
                        blocks = []
                        cur = {}
                        for line in raw.splitlines():
                            s = line.strip()
                            if line.startswith(f"\t{kind} #") or line.startswith(f"{kind} #"):
                                if cur:
                                    blocks.append(cur)
                                cur = {}
                            elif s.startswith("Name:"):
                                cur["name"] = s.split(":",1)[1].strip()
                            elif s.startswith("Description:"):
                                cur["desc"] = s.split(":",1)[1].strip()
                            elif s.startswith("State:"):
                                cur["state"] = s.split(":",1)[1].strip()
                            elif "alsa.card =" in s:
                                m = _re5.search(r'"(\d+)"', s)
                                if m: cur["card"] = m.group(1)
                        if cur:
                            blocks.append(cur)
                        return [b for b in blocks if "name" in b]
                    default_sink   = subprocess.run(["pactl","get-default-sink"],
                                                    capture_output=True,text=True).stdout.strip()
                    default_source = subprocess.run(["pactl","get-default-source"],
                                                    capture_output=True,text=True).stdout.strip()
                    sinks   = _parse_pw_blocks(sinks_raw, "Sink")
                    sources = _parse_pw_blocks(sources_raw, "Source")
                    # Parse ALSA cards — deduplicate by card number (aplay+arecord both list each card)
                    _seen_cards = set()
                    alsa_cards = []
                    for line in cards_raw.splitlines():
                        if line.startswith("card "):
                            m = _re5.match(r'card (\d+): (\S+) \[([^\]]+)\]', line)
                            if m and m.group(1) not in _seen_cards:
                                _seen_cards.add(m.group(1))
                                alsa_cards.append({"num": m.group(1), "id": m.group(2), "name": m.group(3)})
                    data = {
                        "default_sink":   default_sink,
                        "default_source": default_source,
                        "sinks":   sinks,
                        "sources": sources,
                        "alsa_cards": alsa_cards,
                    }
                except Exception as e:
                    data = {"error": str(e)}
                resp = _json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            elif self.path.startswith("/device-set"):
                import json as _json, urllib.parse as _up
                qs  = _up.parse_qs(self.path.split("?",1)[1] if "?" in self.path else "")
                dev_type = qs.get("type",[""])[0]   # "source" or "sink"
                dev_name = _up.unquote(qs.get("name",[""])[0])
                result   = {"ok": False, "msg": ""}
                try:
                    if dev_type == "sink" and dev_name:
                        subprocess.run(["pactl","set-default-sink", dev_name],
                                        check=True, capture_output=True)
                        # Ensure the new sink has an audible volume — speaker-cal safety
                        # resets all sinks to 1%, which would leave it inaudible.
                        # Apply saved calibration levels, or minimum safe if unknown
                        _known = _apply_device_cal(dev_name)
                        log.info("HTTP device-set: default sink → %s (%s)",
                                 dev_name, "calibrated" if _known else "new/unknown → minimum")
                        result["ok"]  = True
                        result["msg"] = (
                            f"Speaker set to {dev_name}. "
                            + ("Restored calibrated levels. " if _known
                               else "New device — starting at minimum. Use Manual adjustment. ")
                            + "Restarting audio…"
                        )
                    elif dev_type == "source" and dev_name:
                        # AGC is always the daemon's default source.
                        # Selecting a physical mic redirects AGC to capture
                        # from it — AGC never gets bypassed.
                        if dev_name == AGC_SOURCE_NAME:
                            # User picked the AGC source explicitly — no change needed
                            subprocess.run(["pactl","set-default-source", AGC_SOURCE_NAME],
                                           capture_output=True)
                            log.info("HTTP device-set: AGC source confirmed as default")
                            result["ok"]  = True
                            result["msg"] = "AGC mic is already active. No change needed."
                        else:
                            # Redirect AGC to capture from the chosen physical mic
                            ok = _update_agc_capture_source(dev_name)
                            # AGC gain/gate always applies (AGC normalises)
                            g = globals()
                            g['MIC_GAIN']      = AGC_MIC_GAIN
                            g['MIC_GATE_PEAK'] = AGC_MIC_GATE
                            _mic_gate_ref[0]   = AGC_MIC_GATE
                            # Clear any --input-source override so AGC stays active on restart
                            _update_service_input_source("")
                            log.info("HTTP device-set: AGC redirected to %s", dev_name)
                            result["ok"]  = True
                            result["msg"] = (
                                f"AGC mic redirected to {dev_name}. "
                                "WebRTC AGC still active. Restarting audio…"
                                if ok else
                                f"Could not redirect AGC — check PipeWire. Restarting…"
                            )
                    else:
                        result["msg"] = "Missing type or name"
                    if result["ok"]:
                        # Restart daemon so new defaults are picked up by sd.InputStream
                        threading.Thread(target=lambda: (
                            __import__("time").sleep(0.5),
                            __import__("subprocess").run(
                                ["systemctl","--user","restart","openclaw-realtimetalk"])
                        ), daemon=True).start()
                except Exception as e:
                    result["msg"] = str(e)
                resp = _json.dumps(result).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            elif self.path == "/speaker-cal/vol":
                import json as _json
                resp = _json.dumps(_get_device_status()).encode()
                self.send_response(200); self.send_header("Content-Type","application/json")
                self.send_header("Content-Length", str(len(resp))); self.end_headers()
                self.wfile.write(resp)

            elif self.path == "/speaker-cal/set":
                # Headset mode: save current PipeWire level as calibrated
                import json as _json, re as _re6
                _headset_cal_loop[0] = False
                ds = _get_device_status()
                _update_service_alsa_output(ds["speaker_alsa"])
                # Always use the PipeWire default sink (currently selected speaker)
                sink = subprocess.run(["pactl","get-default-sink"],
                                      capture_output=True,text=True).stdout.strip()
                if sink:
                    cur_out = subprocess.run(["pactl", "get-sink-volume", sink],
                                             capture_output=True, text=True).stdout
                    m = _re6.search(r'(\d+)%', cur_out)
                    pw = int(m.group(1)) if m else 50
                    # Save to per-device calibration store
                    _save_device_cal(sink, pw, _cal_sw_volume)
                log.info("Headset cal: saved level PW=%s SW=%d%%", ds["spk_vol"], ds["sw_pct"])
                # Announce
                if sess:
                    import threading as _t3
                    _t3.Thread(target=speak,
                               args=("Headset volume saved.", sess.alsa_output),
                               daemon=True).start()
                resp = _json.dumps(ds).encode()
                self.send_response(200); self.send_header("Content-Type","application/json")
                self.send_header("Content-Length", str(len(resp))); self.end_headers()
                self.wfile.write(resp)

            elif self.path == "/levels":
                import time as _time
                self.send_response(200)
                self.send_header("Content-Type",  "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection",    "keep-alive")
                self.end_headers()
                try:
                    while True:
                        with _mic_level_lock:
                            peak = _mic_level_current[0]
                        msg = f"data: {peak},{_mic_gate_ref[0]}\n\n".encode()
                        self.wfile.write(msg)
                        self.wfile.flush()
                        _time.sleep(0.1)
                except Exception:
                    pass
            elif self.path == "/log":
                # Legacy redirect
                self.send_response(301)
                self.send_header("Location", "/dashboard")
                self.end_headers()
            elif self.path in ("/dashboard", "/"):
                # Check for device changes on every page load
                new_fp = _get_audio_fingerprint()
                device_banner = ""
                if new_fp and new_fp != _audio_fingerprint[0]:
                    _audio_fingerprint[0] = new_fp
                    msg = "Audio devices changed. Please recalibrate the mic."
                    _device_change_msg[0] = msg
                    log.info("Device change detected on /log refresh")
                    if sess:
                        import threading as _t
                        def _announce_change():
                            _safe_volume_new_sinks(1)   # PipeWire at 1% safety reset
                            import time as _time; _time.sleep(0.5)
                            # Apply saved calibration for the new default sink
                            # (or minimum safe if unknown device)
                            _cur_sink = subprocess.run(
                                ["pactl","get-default-sink"],
                                capture_output=True,text=True).stdout.strip()
                            if _cur_sink:
                                _apply_device_cal(_cur_sink)
                            speak(msg, sess.alsa_output, volume=_cal_sw_volume)
                        _t.Thread(target=_announce_change, daemon=True).start()

                if _device_change_msg[0]:
                    device_banner = (
                        f'<div id="dbanner" style="background:#5a2200;border-radius:8px;'
                        f'padding:10px;margin-bottom:8px;font-weight:bold;">'
                        f'{_device_change_msg[0]}</div>'
                        f'<script>setTimeout(()=>{{var b=document.getElementById("dbanner");'
                        f'if(b)b.remove();}},5000);</script>'
                    )
                    _device_change_msg[0] = ""
                else:
                    device_banner = (
                        f'<div id="dbanner" style="background:#1a3a1a;border-radius:8px;'
                        f'padding:8px;margin-bottom:8px;color:#5f5;font-size:13px;">'
                        f'No device change detected.</div>'
                        f'<script>setTimeout(()=>{{var b=document.getElementById("dbanner");'
                        f'if(b)b.remove();}},5000);</script>'
                    )

                active = sess._active if sess else False
                monitoring = sess._monitoring if sess else False
                multilang  = sess._multilang if sess else False
                # Pre-compute how long each "thinking" entry waited for a Five reply.
                # None = still thinking (show live counter); float = seconds taken (show static).
                thinking_dur: dict = {}
                for _i, _e in enumerate(CONVERSATION_LOG):
                    if _e["role"] == "thinking":
                        _ep = _e.get("epoch", 0.0)
                        for _j in range(_i + 1, len(CONVERSATION_LOG)):
                            if CONVERSATION_LOG[_j]["role"] == "five":
                                thinking_dur[_ep] = (
                                    CONVERSATION_LOG[_j].get("epoch", _ep) - _ep
                                )
                                break
                        else:
                            thinking_dur[_ep] = None  # still waiting

                rows = ""
                for e in reversed(CONVERSATION_LOG):
                    ts = e.get("ts", "")
                    ts_span = f'<span class="ts">{ts}</span> ' if ts else ""
                    if e["role"] == "you":
                        rows += f'<div class="you">{ts_span}<b>You:</b> {e["text"]}</div>'
                    elif e["role"] == "five":
                        rows += f'<div class="five">{ts_span}<b>Five:</b> {e["text"]}</div>'
                    elif e["role"] == "monitor":
                        rows += f'<div class="mon">{ts_span}{e["text"]}</div>'
                    elif e["role"] == "thinking":
                        ep  = e.get("epoch", 0.0)
                        dur = thinking_dur.get(ep)
                        if dur is None:
                            # Still waiting — live counter
                            rows += (f'<div class="thinking">{ts_span}'
                                     f'Five is thinking... '
                                     f'<span class="tctr" data-start="{ep:.3f}">0</span>s</div>')
                        # else: Five replied — hide this line entirely
                    else:
                        rows += f'<div class="sys">{ts_span}{e["text"]}</div>'
                # All device info gathered outside do_GET to avoid UnboundLocalError scoping
                _ds = _get_device_status()
                device_panel = (
                    f'<div style="background:#1a1a2a;border-radius:8px;padding:8px 12px;'
                    f'margin-bottom:8px;font-size:12px;color:#aaa;line-height:1.8;">'
                    f'<b style="color:#eee">Audio devices</b><br>'
                    f'Mic: {_ds["mic"]}<br>'
                    f'Speaker: {_ds["speaker_name"]} &nbsp;|&nbsp; '
                    f'Vol {_ds["spk_vol"]} &nbsp;|&nbsp; SW {_ds["sw_pct"]}%<br>'
                    f'Mic gate: {_ds["gate"]} &nbsp;|&nbsp; Gain: {_ds["gain"]}x'
                    f'</div>'
                )

                state = ("MONITORING" if monitoring
                         else "ACTIVE" if active else "SILENT")
                body = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="3">
<title>RealTimeTalk Dashboard</title>
<style>
html,body{{height:100%;margin:0;}}
body{{font-family:sans-serif;font-size:17px;background:#111;color:#eee;display:flex;flex-direction:column;}}
#top{{padding:12px 12px 0;flex-shrink:0;}}
#log{{flex:1;overflow-y:auto;padding:0 12px 12px;}}
.you{{background:#1a3a1a;border-radius:8px;padding:8px;margin:6px 0;}}
.five{{background:#1a2a3a;border-radius:8px;padding:8px;margin:6px 0;}}
.mon{{background:#2a2030;border-left:3px solid #b58;border-radius:6px;padding:8px;margin:6px 0;}}
.sys{{color:#888;font-size:0.85em;text-align:center;margin:4px 0;}}
.thinking{{color:#f90;background:#1a1400;border-left:3px solid #f90;border-radius:6px;padding:8px;margin:6px 0;font-style:italic;}}
.ts{{color:#666;font-size:0.8em;font-family:monospace;}}
h3{{margin:0 0 10px;}}
a{{color:#7af;margin-right:14px;font-size:17px;}}
a.on{{color:#5f5;font-weight:bold;}}
a.reset{{color:#f86;}}
</style></head><body>
<div id="top">
<h3>RealTimeTalk Dashboard — {state}</h3>
<a href="/wake">Wake</a><a href="/sleep">Sleep</a><a href="/monitor/start" class="{'on' if monitoring else ''}">Start Monitor</a><a href="/monitor/stop" class="{'' if monitoring else 'on'}">Stop Monitor</a><a href="/multilang" class="{'on' if multilang else ''}">Multi-lang: {'ON' if multilang else 'OFF'}</a><a href="/reset" class="reset">Reset</a><a href="/calibration">Calibration</a><a href="/restart">Restart</a><a href="/dashboard">Dashboard</a>
<hr>{device_panel}{device_banner}</div>
<div id="log">{rows if rows else "<div class='sys'>No conversation yet</div>"}</div>
<script>
setInterval(function(){{
  var now=Date.now()/1000;
  document.querySelectorAll('.tctr').forEach(function(el){{
    el.textContent=Math.max(0,Math.floor(now-parseFloat(el.dataset.start)));
  }});
}},500);
</script>
</body></html>"""
                _html(self, 200, body)
            elif self.path == "/status":
                sess = session_ref[0]
                active = sess._active if sess else False
                body = json.dumps({"status": "running", "voice": "active" if active else "silent"}).encode()
                self.send_response(200)
                self.send_header("Content-Type",   "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                _html(self, 404, "<h2>Not found</h2>")

    from socketserver import ThreadingMixIn
    class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True
    server = _ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Toggle: http://<pi-ip>:%d/stop  |  /wake  |  /sleep  |  /status", port)

# ── Main ──────────────────────────────────────────────────────────────────────

async def main(http_port: int, input_device=None, output_device=None,
               session_key: str = OPENCLAW_SESSION):
    # `output_device` is a sounddevice index (or None for system default).
    # The legacy `alsa_output` label is passed through for compatibility with
    # the HTTP handlers and RealtimeSession; speak() reads the actual device
    # from _selected_output_device[0].
    alsa_output = ALSA_OUTPUT
    loop       = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    try:
        openai_key = load_openai_key()
        gw_token   = load_gateway_token()
    except Exception as e:
        log.error(str(e))
        sys.exit(1)

    gw = GatewayClient(gw_token)
    while not stop_event.is_set():
        try:
            await gw.connect()
            break
        except (ConnectionRefusedError, OSError) as e:
            log.warning("Gateway not ready (%s) — retrying in 5s…", e)
            await asyncio.sleep(5)
    if stop_event.is_set():
        return
    gw_task = asyncio.create_task(gw.listen(stop_event))

    session_ref: list = [None]
    start_http_server(http_port, lambda: loop.call_soon_threadsafe(stop_event.set), session_ref)
    log.info("OpenClaw RealTimeTalk daemon starting — silent mode (say 'Five wake up' to activate)")

    while not stop_event.is_set():
        session = RealtimeSession(
            api_key=openai_key, loop=loop, gw=gw,
            stop_event=stop_event,
            input_device=input_device, alsa_output=alsa_output,
            session_key=session_key,
        )
        session_ref[0] = session
        try:
            await session.run()
            log.info("Session ended.")
        except websockets.exceptions.ConnectionClosedError as e:
            log.warning("Realtime connection closed: %s", e)
        except Exception as e:
            log.error("Session error: %s", e)

        if not stop_event.is_set():
            log.info("Reconnecting in %ds…", RECONNECT_DELAY)
            await asyncio.sleep(RECONNECT_DELAY)

    gw_task.cancel()
    await gw.close()
    log.info("Daemon stopped.")


def calibrate_mic(input_device=None, duration: float = 3.0) -> int:
    """Record ambient noise and return a recommended MIC_GATE_PEAK value (2× noise peak)."""
    print(f"Calibrating mic — measuring ambient noise for {duration:.0f}s. Stay quiet.")
    peaks = []
    def cb(indata, frames, t, s):
        raw = indata[::RESAMPLE_RATIO, 0]
        peaks.append(int(np.max(np.abs(raw))))
    with sd.InputStream(samplerate=DEVICE_RATE, channels=CHANNELS, dtype="int16",
                        blocksize=DEVICE_BLOCKSIZE, callback=cb,
                        device=input_device):
        import time; time.sleep(duration)
    peaks = peaks[2:]  # discard first two frames (hardware warmup)
    noise_peak = max(peaks) if peaks else 0
    recommended = max(MIC_GATE_MIN, min(MIC_GATE_MAX, int(noise_peak * 1.25)))
    print(f"Noise floor peak: {noise_peak}  →  recommended MIC_GATE_PEAK: {recommended} (clamped {MIC_GATE_MIN}–{MIC_GATE_MAX})")
    return recommended


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="OpenClaw RealTimeTalk daemon (Mac)")
    p.add_argument("--http-port",       type=int, default=DEFAULT_HTTP_PORT,
                   help=f"HTTP dashboard port (default {DEFAULT_HTTP_PORT})")
    p.add_argument("--input-device",    type=int, default=None,
                   help="sounddevice input device index (see --list-devices)")
    p.add_argument("--output-device",   type=int, default=None,
                   help="sounddevice output device index for TTS playback (default: system default)")
    # Compat aliases for users coming from the Pi version
    p.add_argument("--input-source",    type=str, default=None,
                   help="(compat) ignored on Mac — use --input-device instead")
    p.add_argument("--alsa-output",     type=str, default=ALSA_OUTPUT,
                   help="(compat) ignored on Mac — use --output-device instead")
    p.add_argument("--session-key",     type=str, default=OPENCLAW_SESSION,
                   help=f"OpenClaw session key (default: {OPENCLAW_SESSION})")
    p.add_argument("--mic-gain",        type=float, default=MIC_GAIN,
                   help=f"Software mic gain multiplier (default: {MIC_GAIN})")
    p.add_argument("--mic-gate",        type=int, default=MIC_GATE_PEAK,
                   help=f"Noise gate threshold — pre-gain peak below this → silence (default: {MIC_GATE_PEAK})")
    p.add_argument("--list-devices",    action="store_true",
                   help="Print available CoreAudio devices and exit")
    p.add_argument("--calibrate",       action="store_true",
                   help="Measure ambient noise and print recommended --mic-gate value, then exit")
    args = p.parse_args()

    if args.list_devices:
        devs = _list_audio_devices()
        print("INPUTS:")
        for d in devs["inputs"]:
            print(f"  [{d['index']:>2}] {d['name']!r} ({d['channels']} ch, {d['kind']})")
        print("\nOUTPUTS:")
        for d in devs["outputs"]:
            print(f"  [{d['index']:>2}] {d['name']!r} ({d['channels']} ch, {d['kind']})")
        sys.exit(0)

    if args.calibrate:
        val = calibrate_mic(input_device=args.input_device)
        print(f"\nRun with:  --mic-gate {val}")
        print(f"Or update service:  launchctl kickstart -k gui/$UID/{SERVICE_LABEL}")
        sys.exit(0)

    MIC_GAIN      = args.mic_gain
    MIC_GATE_PEAK = args.mic_gate
    if args.input_source:
        log.warning("--input-source is ignored on Mac; use --input-device <idx> instead")
    _selected_input_device[0]  = args.input_device
    _selected_output_device[0] = args.output_device
    _mic_gate_ref[0] = MIC_GATE_PEAK
    log.info("Audio: in=%s out=%s gain=%.1f gate=%d",
             _device_label(args.input_device), _device_label(args.output_device),
             MIC_GAIN, MIC_GATE_PEAK)

    bt_warn = _bt_mic_warning(args.input_device)
    if bt_warn:
        log.warning(bt_warn)

    # Load per-device calibration store and apply to current selected output
    _load_cal_store()
    try:
        _out_d = sd.query_devices(args.output_device if args.output_device is not None else None,
                                  kind="output")
        _out_name = _out_d.get("name", "default")
    except Exception:
        _out_name = "default"
    _known = _apply_device_cal(_out_name)
    if not _known:
        log.info("Unknown speaker %r at startup — using safe default volume", _out_name)

    asyncio.run(main(
        args.http_port,
        args.input_device,
        args.output_device,
        args.session_key,
    ))
