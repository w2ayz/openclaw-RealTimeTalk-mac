#!/usr/bin/env python3
"""
RealTimeTalk-daemon.py — OpenClaw RealTimeTalk daemon (Mac Mini / CoreAudio).

Audio flow:
  Mic → OpenAI Realtime API (VAD + STT only) → transcript
  transcript → OpenClaw gateway (chat.send / agent.wait) → Zeebot's reply
  Zeebot's reply → Edge TTS (primary) | macOS `say` (fallback) → speaker

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

__version__ = "1.3.0"

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
MIC_GAIN          = 5.0
MIC_GATE_PEAK     = 20           # noise gate — pre-gain peak below this → silence
MIC_GATE_MIN      = 15           # calibration clamp — quietest usable room
MIC_GATE_MAX      = 3000         # calibration clamp — above this, use a headset

# Output volume control — macOS uses system-wide volume via osascript.
# Per-device volume isn't scriptable on macOS, so software attenuation is the
# primary fine-grained control, with osascript for coarse adjustment.
CAL_FALLBACK_VOL  = 0.70         # fallback SW when cal measurement itself fails (mid-run error)
CAL_NEW_DEV_VOL   = 0.01         # Vol+SW for first-seen devices — start at minimum, user adjusts up
CAL_NEW_DEV_SYS_VOL = 1          # macOS system volume (%) for new/unrecognised devices
CAL_STORE_FILE    = os.path.expanduser("~/.openclaw/workspace/speaker_cal_store.json")
DEVICE_PREFS_FILE = os.path.expanduser("~/.openclaw/workspace/device_prefs.json")
# Speech-interrupt: if the mic sees this many consecutive 50ms blocks above
# the interrupt threshold while Zeebot is speaking, kill TTS immediately.
SPEAK_INTERRUPT_PEAK   = 150   # min threshold floor
SPEAK_INTERRUPT_BLOCKS = 3     # × 50 ms = 150 ms sustained speech → interrupt

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
CAL_NEW_DEV_PW    = CAL_NEW_DEV_SYS_VOL
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

CONVERSATION_LOG: list[dict] = []   # {"role":"you"/"zeebot"/"system", "text":...}

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
_paused_speech: list = [None]   # (clean_text, alsa_output) saved on TTS interrupt; None otherwise
_post_busy_until:  list = [0.0] # timestamp; mic sends silence until this time after busy clears
_http_interrupt:   list = [False]  # set by /interrupt HTTP route to cut TTS mid-playback
_is_speaking:      list = [False]  # True while speak() is playing audio
_current_think_task: list = [None]  # asyncio.Task for current gw.ask(); cancelled by /interrupt
_last_mic_cb:        list = [0.0]   # epoch of last _mic_cb invocation — used for hot-plug detection


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
    """Set macOS output volume 0-100 via AppleScript and update _cal_sys_vol_pct."""
    pct = max(0, min(100, int(pct)))
    _cal_sys_vol_pct[0] = pct
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
    vol_pct = _cal_sys_vol_pct[0]
    sw_pct  = int(_cal_sw_volume * 100)
    result = {
        "mic": "?",
        "speaker_alsa": ALSA_OUTPUT,
        "speaker_name": "default output",
        "spk_vol":      f"{vol_pct}%",
        "sw_pct":       sw_pct,
        "effective_pct": max(1, round(vol_pct * sw_pct / 100)),
        "loop_playing":  _headset_cal_loop[0],
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
_selected_input_device: list  = [None]
_selected_output_device: list = [None]

# Vol axis (0-100 int) for manual adjustment — read live by the test loop callback.
# Kept in sync with osascript system volume by _set_system_volume().
_cal_sys_vol_pct: list = [50]

# WebRTC AGC2 + NS processor (16 kHz, aggressiveness 2).
# None until initialised at startup; fallback to numpy AGC if unavailable.
_webrtc_proc = None
_agc_gain: list = [3.0]   # fallback numpy AGC state


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
    """Find the MINIMUM usable speaker volume on macOS by sweeping both
    macOS system volume (Vol) and daemon software gain (SW) from 1% to 100%.

    Both axes start at minimum and increase together in lockstep until the
    mic can hear the tone above the SNR target. macOS system volume is
    temporarily changed during the sweep and set to the found optimal level
    on success (or restored to the pre-cal value on failure).
    """
    import time as _t

    sample_rate = 24000
    n_samples   = int(sample_rate * duration)
    freq_idx    = int(np.round(test_freq * n_samples / sample_rate))
    in_dev  = _selected_input_device[0]
    out_dev = _selected_output_device[0]

    # Save current system volume so we can restore on failure
    saved_sys_vol = _get_system_volume()

    # Sweep steps: (system_vol_pct, sw_frac) — both start at 1% and climb to 100%
    steps = [
        (1,   0.01),
        (2,   0.02),
        (5,   0.05),
        (10,  0.10),
        (20,  0.20),
        (35,  0.35),
        (50,  0.50),
        (70,  0.70),
        (90,  0.90),
        (100, 1.00),
    ]
    measurements: list[dict] = []
    found_sw      = CAL_FALLBACK_VOL
    found_sys_vol = saved_sys_vol
    status        = "ok"

    # Noise floor — measure with silence at minimum volume
    try:
        _set_system_volume(1)
        _t.sleep(0.1)
        ref_rec     = _cal_capture(n_samples, sample_rate)
        ref_data    = ref_rec.astype(np.float32) / 32768.0
        ref_fft     = np.abs(np.fft.rfft(ref_data)) / n_samples
        noise_floor = float(np.median(ref_fft)) or 1e-6
    except Exception:
        noise_floor = 1e-6

    try:
        for vol_pct, sw_vol in steps:
            _set_system_volume(vol_pct)
            _t.sleep(0.05)  # let CoreAudio ramp settle

            t_arr  = np.linspace(0, duration, n_samples, endpoint=False)
            tone16 = (0.5 * sw_vol * np.sin(2 * np.pi * test_freq * t_arr) * 32767).astype(np.int16)

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
            _t.sleep(0.05)
            try:
                sd.play(tone16, samplerate=sample_rate, device=out_dev, blocking=True)
            except Exception as e:
                log.warning("Cal playback error at vol=%d sw=%.2f: %s", vol_pct, sw_vol, e)
            done_ev.wait(timeout=duration + 2.0)

            data    = recording.astype(np.float32) / 32768.0
            fft_mag = np.abs(np.fft.rfft(data)) / n_samples
            tone_e  = float(fft_mag[freq_idx])
            snr     = tone_e / noise_floor
            measurements.append({"vol": vol_pct, "sw": round(sw_vol, 3),
                                  "tone": round(tone_e, 7), "snr": round(snr, 2)})
            log.info("Speaker cal: Vol=%d%% SW=%.2f tone=%.6f SNR=%.1f",
                     vol_pct, sw_vol, tone_e, snr)

            if snr >= snr_target:
                found_sw      = sw_vol
                found_sys_vol = vol_pct
                log.info("Speaker cal: target SNR reached at Vol=%d%% SW=%.2f",
                         vol_pct, sw_vol)
                break
        else:
            # Target not reached — pick best audible step
            if measurements:
                audible = next((m for m in measurements if m["snr"] >= CAL_AUDIBLE_SNR), None)
                if audible:
                    found_sw      = audible["sw"]
                    found_sys_vol = audible["vol"]
                    status = "weak_coupling"
                else:
                    best = max(measurements, key=lambda m: m["tone"])
                    if best["tone"] < 0.00005:
                        status        = "no_mic"
                        found_sw      = CAL_FALLBACK_VOL
                        found_sys_vol = saved_sys_vol   # restore — mic couldn't hear anything
                    else:
                        found_sw      = best["sw"]
                        found_sys_vol = best["vol"]
                        status        = "weak_coupling"

        global _cal_sw_volume
        _cal_sw_volume = found_sw
        _set_system_volume(found_sys_vol)
        log.info("Speaker cal complete: Vol=%d%% SW=%.2f status=%s",
                 found_sys_vol, found_sw, status)

        out_name = "default"
        try:
            d = sd.query_devices(out_dev if out_dev is not None else None, kind="output")
            out_name = d.get("name", "default")
        except Exception:
            pass
        _save_device_cal(out_name, found_sw, found_sys_vol)

    except Exception as e:
        log.error("Speaker calibration error: %s", e)
        _set_system_volume(saved_sys_vol)
        status        = f"error: {e}"
        found_sw      = CAL_FALLBACK_VOL
        found_sys_vol = saved_sys_vol

    return {
        "safe_vol":     found_sys_vol,
        "safe_sw_vol":  found_sw,
        "speaker_alsa": ALSA_OUTPUT,
        "measurements": measurements,
        "mic_source":   _device_label(in_dev),
        "speaker_sink": _device_label(out_dev),
        "test_freq":    test_freq,
        "snr_target":   snr_target,
        "status":       status,
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

TRANSCRIPTION_PROMPT = "Zeebot."  # teaches name spelling only; too short to hallucinate as a command
TRANSCRIPTION_PROMPT_NORM = "zeebot"

WAKE_PHRASES     = {"zeebot wake up", "real time talk on", "real-time talk on", "realtimetalk on",
                    "zibob wake up", "zibot wake up", "libot wake up", "ziba wake up"}
SLEEP_PHRASES    = {"zeebot go to sleep", "real time talk off", "real-time talk off", "realtimetalk off"}
MONITOR_ON_PHRASES  = {"zeebot start monitoring", "start monitoring", "zeebot monitor on",
                       "monitor on", "zeebot monitoring on"}
MONITOR_OFF_PHRASES = {"zeebot stop monitoring", "stop monitoring", "zeebot monitor off",
                       "monitor off", "zeebot monitoring off"}
CONTINUE_PHRASES = {"continue", "zeebot continue", "please continue", "go on", "go ahead",
                    "keep going", "继续", "继续说", "你继续", "请继续"}

try:
    from langdetect import detect as _langdetect, LangDetectException as _LangDetectException
    _HAVE_LANGDETECT = True
except ImportError:
    _HAVE_LANGDETECT = False

def _is_english_or_chinese(text: str) -> bool:
    """Return True only if the transcript appears to be English or Chinese.
    Filters out Japanese, Arabic, Cyrillic, Korean, and other Latin-script
    languages (Dutch, French, German, etc.) that gpt-4o-transcribe may
    hallucinate from background audio.
    """
    # Reject non-Latin/non-CJK scripts via unicode range (fast path)
    reject_ranges = (
        (0x3040, 0x30FF),   # hiragana + katakana
        (0x0600, 0x06FF),   # Arabic
        (0x0400, 0x04FF),   # Cyrillic
        (0xAC00, 0xD7AF),   # Korean Hangul
        (0x0900, 0x097F),   # Devanagari
    )
    has_cjk = False
    all_ascii = True
    for ch in text:
        cp = ord(ch)
        if any(lo <= cp <= hi for lo, hi in reject_ranges):
            return False
        if 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF:
            has_cjk = True
            all_ascii = False
        elif 0x3000 <= cp <= 0x303F or 0xFF00 <= cp <= 0xFFEF:
            pass  # CJK punctuation / fullwidth — ok
        elif cp > 0x7F and ch not in ' \t\n\r':
            # Accented Latin (French/German/etc.) — reject
            return False

    if has_cjk:
        return True  # Chinese confirmed

    # Pure ASCII — could be English or any other Latin-script language.
    # Use langdetect on texts with >=3 words to verify; short phrases pass.
    if _HAVE_LANGDETECT and len(text.split()) >= 3:
        try:
            lang = _langdetect(text)
            if lang not in ("en", "zh-cn", "zh-tw"):
                log.debug("langdetect rejected %r as %r", text[:60], lang)
                return False
        except _LangDetectException:
            pass  # inconclusive — let it through
    return True

def _normalize(text: str) -> str:
    import string
    t = text.strip().lower()
    t = t.translate(str.maketrans(string.punctuation, " " * len(string.punctuation)))
    t = re.sub(r'\b5\b', '5', t)  # no numeric shorthand for Zeebot
    return " ".join(t.split())

def _matches_phrase(transcript: str, phrases: set) -> bool:
    """True if the transcript contains any trigger phrase, or is a fuzzy word-overlap match.

    Two-pass:
    1. Exact substring after normalisation.
    2. Fuzzy: if the transcript shares ≥ 60% of a phrase's words it counts as a match
       (handles car-noise garbling like 'zeebot wake up' → 'zeebot break up').
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

_cal_store: dict = {}   # {device_name: {"sw_vol": float, "sys_vol": int, "name": str}}

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

def _save_device_cal(device_name: str, sw_vol: float, sys_vol: int = None) -> None:
    """Record calibrated Vol + SW for an output device and persist to disk."""
    entry = {"sw_vol": float(sw_vol), "name": device_name}
    if sys_vol is not None:
        entry["sys_vol"] = int(sys_vol)
    _cal_store[device_name] = entry
    _save_cal_store()
    log.info("Saved calibration for %r: Vol=%s%% SW=%.2f",
             device_name, sys_vol if sys_vol is not None else "?", sw_vol)

def _apply_device_cal(device_name: str) -> bool:
    """Apply saved Vol + SW for an output device, or safe defaults if unknown.

    Returns True if a previously calibrated level was found and applied,
    False if safe defaults were applied (new/unknown device).
    """
    if device_name in _cal_store:
        entry   = _cal_store[device_name]
        sw      = float(entry.get("sw_vol", CAL_FALLBACK_VOL))
        sys_vol = entry.get("sys_vol")
        globals()['_cal_sw_volume'] = sw
        if sys_vol is not None:
            _set_system_volume(int(sys_vol))
        log.info("Restored calibration for %r: Vol=%s%% SW=%.2f",
                 device_name, sys_vol if sys_vol is not None else "?", sw)
        return True
    else:
        globals()['_cal_sw_volume'] = CAL_NEW_DEV_VOL
        _set_system_volume(CAL_NEW_DEV_SYS_VOL)
        log.info("New/unknown device %r — starting at minimum Vol=%d%% SW=%.0f%%",
                 device_name, CAL_NEW_DEV_SYS_VOL, CAL_NEW_DEV_VOL * 100)
        return False

def _resolve_device_by_name(name: str, kind: str) -> int | None:
    """Return the current sounddevice index for a saved device name, or None if not found."""
    ch_key = "max_output_channels" if kind == "output" else "max_input_channels"
    try:
        for i, d in enumerate(sd.query_devices()):
            if d["name"] == name and d[ch_key] > 0:
                return i
    except Exception:
        pass
    return None

def _load_device_prefs() -> dict:
    try:
        with open(DEVICE_PREFS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def _save_device_prefs(output_name: str = None, input_name: str = None) -> None:
    prefs = _load_device_prefs()
    if output_name is not None:
        prefs["output_device_name"] = output_name
    if input_name is not None:
        prefs["input_device_name"] = input_name
    try:
        os.makedirs(os.path.dirname(DEVICE_PREFS_FILE), exist_ok=True)
        with open(DEVICE_PREFS_FILE, "w") as f:
            json.dump(prefs, f, indent=2)
    except Exception as e:
        log.warning("Could not save device prefs: %s", e)

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
    # (e.g. Zeebot's ⚡ becomes "high voltage"). Keep CJK for Chinese TTS.
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


def speak(text: str, alsa_output: str = ALSA_OUTPUT, volume: float = -1.0, silence_ms: int = 300,
          resumable: bool = False):
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
        log.warning("speak() called with empty text after strip_markdown: %r", text)
        return

    log.info("speak() → %r  vol=%.2f sys_vol=%d out_dev=%s",
             clean[:80], volume, _cal_sys_vol_pct[0], _selected_output_device[0])

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
            ok_edge = _edge_tts_to_mp3(seg_text, edge_voice, mp3_path)
            log.info("  Edge TTS %s for %r", "OK" if ok_edge else "FAILED", seg_text[:40])
            if ok_edge:
                pcm = _decode_to_pcm(mp3_path)
                log.info("  PCM decode: %d samples (%.1fs)", pcm.size, pcm.size/TTS_SAMPLE_RATE)
            if pcm.size == 0:
                # Fall back to macOS `say`
                aiff_path = tempfile.mktemp(suffix=".aiff")
                temp_files.append(aiff_path)
                ok_say = _say_fallback_to_aiff(seg_text, lang, aiff_path)
                log.info("  say fallback %s", "OK" if ok_say else "FAILED")
                if ok_say:
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
        # Apply combined Vol × SW gain in PCM so it works on all devices including USB.
        total_gain = (_cal_sys_vol_pct[0] / 100.0) * volume
        if total_gain != 1.0:
            final = np.clip(final.astype(np.float32) * total_gain, -32768, 32767).astype(np.int16)

        _interrupted = [False]
        out_dev = _selected_output_device[0]
        log.info("  sd.play() %d samples peak=%d gain=%.3f dev=%s",
                 final.size, int(np.max(np.abs(final))), total_gain, out_dev)
        try:
            _is_speaking[0] = True
            sd.play(final, samplerate=TTS_SAMPLE_RATE, device=out_dev, blocking=False)
        except Exception as e:
            _is_speaking[0] = False
            log.error("sd.play() failed: %s", e)
            return

        # Auto-calibrating interrupt threshold.
        #
        # During the 500 ms guard period we compare what the output PCM is playing
        # at each 50 ms tick against what the mic picks up.  The ratio (mic/output)
        # is the acoustic coupling for this room/device combination.  After the guard
        # we set threshold = max_coupling × safety_factor × output_peak so the
        # threshold automatically scales to any speaker+mic setup and volume level.
        #
        # If the guard produces no usable coupling data (e.g. the audio starts with
        # a long silence) we fall back to SPEAK_INTERRUPT_PEAK as the floor.
        import time as _t
        output_peak = int(np.max(np.abs(final)))
        INTERRUPT_GUARD_TICKS  = 20    # 1 s guard — 300 ms silence + 700 ms speech to measure
        INTERRUPT_SAFETY       = 1.8   # threshold = measured_echo × 1.8 (user must be clearly louder)
        TICK_SAMPLES = TTS_SAMPLE_RATE * 50 // 1000   # samples per 50 ms tick

        interrupt_threshold = SPEAK_INTERRUPT_PEAK    # updated after guard
        guard_max_out = 0   # peak output PCM seen during guard
        guard_max_mic = 0   # peak mic level seen during guard (echo baseline)
        consec = 0
        guard  = INTERRUPT_GUARD_TICKS

        while True:
            try:
                stream = sd.get_stream()
                active = stream is not None and stream.active
            except Exception:
                active = False
            if not active:
                break
            _t.sleep(0.05)
            with _mic_level_lock:
                p = _mic_level_current[0]

            if guard > 0:
                # Accumulate peak output and peak mic separately — dividing per-tick
                # ratios inflates the coupling when a quiet output tick is divided
                # against any mic background noise.
                tick_idx = (INTERRUPT_GUARD_TICKS - guard)
                s0 = tick_idx * TICK_SAMPLES
                s1 = s0 + TICK_SAMPLES
                tick_out = int(np.max(np.abs(final[s0:s1]))) if s1 <= len(final) else 0
                if tick_out > guard_max_out:
                    guard_max_out = tick_out
                if p > guard_max_mic:
                    guard_max_mic = p
                guard -= 1
                if guard == 0:
                    if guard_max_out > 200:
                        coupling = guard_max_mic / guard_max_out
                        interrupt_threshold = max(
                            int(output_peak * coupling * INTERRUPT_SAFETY),
                            SPEAK_INTERRUPT_PEAK,
                        )
                        log.info("  coupling=%.3f (echo=%d/out=%d) interrupt_threshold=%d",
                                 coupling, guard_max_mic, guard_max_out, interrupt_threshold)
                    else:
                        log.info("  no coupling data — using floor threshold=%d",
                                 interrupt_threshold)
                continue

            if _http_interrupt[0]:
                log.info("HTTP interrupt — stopping TTS")
                _http_interrupt[0] = False
                _interrupted[0] = True
                if resumable:
                    _paused_speech[0] = (clean, alsa_output)
                    log.info("  Saved %d chars for resume", len(clean))
                try:
                    sd.stop()
                except Exception:
                    pass
                break

            if p > interrupt_threshold:
                consec += 1
                if consec >= SPEAK_INTERRUPT_BLOCKS:
                    log.info("Speech interrupt — stopping TTS (peak=%d threshold=%d)",
                             p, interrupt_threshold)
                    _interrupted[0] = True
                    if resumable:
                        _paused_speech[0] = (clean, alsa_output)
                        log.info("  Saved %d chars for resume", len(clean))
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

        if not _interrupted[0] and resumable:
            _paused_speech[0] = None   # finished normally — nothing to resume

    except Exception as e:
        log.error("speak() error: %s", e)
    finally:
        _is_speaking[0] = False
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
            await asyncio.sleep(1.2)  # let message-tool result persist
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
        self._busy        = asyncio.Event()   # set while Zeebot is speaking
        self._cal_peaks: list[int] = []       # raw peaks collected during calibration
        self._calibrating = False
        self._active      = False             # start silent; wake phrase enables voice
        self._monitoring  = False             # passive capture-only mode (no Zeebot, no TTS)
        self._multilang   = False             # False = only show/process EN/ZH
        self._mic_stream_ref: list = [None]   # current sd.InputStream; swapped on hot-plug

    def _mic_cb(self, indata, frames, time_info, status):
        import time as _tcb0
        _last_mic_cb[0] = _tcb0.time()
        raw = indata[::RESAMPLE_RATIO, 0]
        raw_peak = int(np.max(np.abs(raw)))
        with _mic_level_lock:
            _mic_level_current[0] = raw_peak
        # While calibrating, record raw peaks (no gain/gate applied, mic suppression off)
        if self._calibrating:
            self.loop.call_soon_threadsafe(self._cal_peaks.append, raw_peak)
            return
        # While Zeebot is speaking (or for 500 ms after it stops), send silence
        # so Zeebot's own TTS echo can't leak into the transcription stream.
        import time as _tcb
        if self._busy.is_set() or _tcb.time() < _post_busy_until[0]:
            silence = np.zeros_like(raw)
            self.loop.call_soon_threadsafe(self._enqueue_mic, silence.tobytes())
            return
        if raw_peak < MIC_GATE_PEAK:
            out_arr = np.zeros_like(raw)
            # Gain frozen during silence — WebRTC AGC won't pump up on noise
        elif _webrtc_proc is not None:
            # Resample 24 kHz → 16 kHz (linear interp — fine for speech)
            n_16k = len(raw) * 2 // 3
            idx_src = np.linspace(0, len(raw) - 1, n_16k)
            s16_16k = np.interp(idx_src, np.arange(len(raw)),
                                raw.astype(np.float32)).astype(np.int16)
            # Process in 10 ms chunks (160 samples at 16 kHz)
            chunks = []
            for i in range(0, len(s16_16k), 160):
                chunk = s16_16k[i:i + 160]
                if len(chunk) < 160:
                    chunk = np.pad(chunk, (0, 160 - len(chunk)))
                res = _webrtc_proc.Process10ms(chunk.tobytes())
                chunks.append(np.frombuffer(res.audio, dtype=np.int16))
            proc_16k = np.concatenate(chunks)[:len(s16_16k)]
            # Upsample 16 kHz → 24 kHz
            idx_dst = np.linspace(0, len(proc_16k) - 1, len(raw))
            out_arr = np.interp(idx_dst, np.arange(len(proc_16k)),
                                proc_16k.astype(np.float32)).astype(np.int16)
        else:
            # Numpy fallback: RMS leveler + tanh soft limiter
            _AGC_TARGET = 4000.0
            _AGC_MAX    = 8.0
            _STEP_UP    = 10 ** (1 / 20)
            _STEP_DN    = 10 ** (2 / 20)
            _CEIL       = 30000.0
            f32 = raw.astype(np.float32)
            rms = float(np.sqrt(np.mean(f32 ** 2)))
            if rms > 10:
                target = min(_AGC_MAX, _AGC_TARGET / max(rms, 1.0))
                if target > _agc_gain[0]:
                    _agc_gain[0] = min(target, _agc_gain[0] * _STEP_UP)
                else:
                    _agc_gain[0] = max(target, _agc_gain[0] / _STEP_DN)
            boosted = f32 * _agc_gain[0]
            out_arr = (_CEIL * np.tanh(boosted / _CEIL)).astype(np.int16)
        self.loop.call_soon_threadsafe(self._enqueue_mic, out_arr.tobytes())

    def _enqueue_mic(self, data: bytes):
        try:
            self._mic_q.put_nowait(data)
        except asyncio.QueueFull:
            pass

    async def _resume_from_http(self, text: str, alsa_output):
        """Resume paused TTS playback triggered by the /continue HTTP button."""
        import functools as _fct
        if self._busy.is_set():
            return
        self._busy.set()
        try:
            _log_entry("system", "Resuming…")
            await asyncio.get_running_loop().run_in_executor(
                None, _fct.partial(speak, text, alsa_output, resumable=True)
            )
        finally:
            self._busy.clear()

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
        new_gate = max(MIC_GATE_MIN, min(MIC_GATE_MAX, int(noise_peak * 1.5)))
        MIC_GATE_PEAK = new_gate
        log.info("Calibration: noise_peak=%d → MIC_GATE_PEAK=%d", noise_peak, new_gate)
        # Persist to service file so it survives restarts
        _update_service_gate(new_gate)
        await asyncio.get_running_loop().run_in_executor(
            None, speak,
            f"Done. Noise gate set to {new_gate}. Speak normally now.",
            self.alsa_output
        )

    async def _watch_mic_stream(self):
        """Detect USB mic hot-unplug and reopen the stream when replugged."""
        import time as _wm
        await asyncio.sleep(5.0)   # let stream settle before watching
        while not self.stop_event.is_set():
            await asyncio.sleep(2.0)
            if self.stop_event.is_set():
                break
            elapsed = _wm.time() - _last_mic_cb[0]
            if elapsed < 4.0:
                continue
            # Callbacks stopped for 4 s — assume hot-unplug. Try to reopen.
            log.warning("Mic silent %.1fs — hot-plug recovery starting", elapsed)
            old = self._mic_stream_ref[0]
            try:
                if old:
                    old.stop()
                    old.close()
            except Exception:
                pass
            self._mic_stream_ref[0] = None

            # PortAudio caches the device list at init time — it won't see the
            # replugged USB mic without a full terminate + reinitialize cycle.
            # Wait briefly first so the OS has time to enumerate the new device.
            await asyncio.sleep(1.5)
            try:
                sd._terminate()
                sd._initialize()
                log.info("PortAudio reinitialized for hot-plug")
            except Exception as e:
                log.warning("PortAudio reinit error: %s", e)

            # Resolve new device index via subprocess (same technique used by
            # /device-status) so we get a fresh enumeration, not the stale cache.
            prefs = _load_device_prefs()
            saved_name = prefs.get("input_device_name") if prefs else None
            new_idx = self.input_device  # fallback
            if saved_name:
                try:
                    _qr = subprocess.run(
                        [sys.executable, "-c",
                         "import sounddevice as sd, json;"
                         "print(json.dumps([d['name'] for d in sd.query_devices()]))"],
                        capture_output=True, text=True, timeout=5)
                    if _qr.returncode == 0:
                        names = json.loads(_qr.stdout)
                        for i, n in enumerate(names):
                            if (saved_name.lower() in n.lower()
                                    or n.lower() in saved_name.lower()):
                                new_idx = i
                                break
                except Exception as e:
                    log.warning("Subprocess device query failed: %s", e)

            log.info("Hot-plug: attempting to reopen mic on device idx=%s (%s)",
                     new_idx, saved_name or "default")
            try:
                new_stream = sd.InputStream(
                    samplerate=DEVICE_RATE, channels=CHANNELS, dtype="int16",
                    blocksize=DEVICE_BLOCKSIZE, callback=self._mic_cb,
                    device=new_idx,
                )
                new_stream.start()
                self._mic_stream_ref[0] = new_stream
                _selected_input_device[0] = new_idx
                _last_mic_cb[0] = _wm.time()   # reset to avoid immediate re-trigger
                log.info("Mic stream reopened after hot-plug (device idx=%s)", new_idx)
                _log_entry("system", "Mic reconnected.")
            except Exception as e:
                log.warning("Mic reconnect failed (%s) — will retry in 2s", e)
                _last_mic_cb[0] = _wm.time()   # back off; don't spam

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
        # Discard transcripts that arrive while Zeebot is speaking — they are
        # echo of Zeebot's own TTS, not the user's voice.
        if self._busy.is_set():
            log.debug("Discarded echo transcript during TTS: %r", transcript)
            return

        # Default to Simplified Chinese (transcriber often returns Traditional)
        transcript = _to_simplified(transcript)

        # Drop bare prompt echoes — "Zeebot." hallucinated on silence.
        _tnorm = _normalize(transcript)
        if _tnorm == TRANSCRIPTION_PROMPT_NORM:
            log.debug("Dropped prompt echo: %r", transcript)
            return

        # Monitoring-only mode: passively log captured segments (no Zeebot/TTS).
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

        import time as _ti
        import functools as _ft

        def _busy_clear():
            """Clear busy flag and start 500 ms post-TTS silence cooldown."""
            self._busy.clear()
            _post_busy_until[0] = _ti.time() + 0.5

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
                _busy_clear()
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
                        None, speak, "Going silent now. Say Zeebot wake up to resume.", self.alsa_output
                    )
                finally:
                    _busy_clear()
            return

        # Calibration — works in both modes (audio feedback either way)
        if normalized in CALIBRATE_PHRASES:
            log.info("Voice command: calibrate mic")
            asyncio.create_task(self._run_calibration())
            return

        # Monitoring toggle phrases — work regardless of active state
        if _matches_phrase(normalized, MONITOR_ON_PHRASES):
            if not self._monitoring:
                self._monitoring = True
                log.info("Voice command: monitoring ON")
                _log_entry("system", "Monitoring started.")
                await asyncio.get_running_loop().run_in_executor(
                    None, speak, "Monitoring started.", self.alsa_output
                )
            return
        if _matches_phrase(normalized, MONITOR_OFF_PHRASES):
            if self._monitoring:
                self._monitoring = False
                log.info("Voice command: monitoring OFF")
                _log_entry("system", "Monitoring stopped.")
                await asyncio.get_running_loop().run_in_executor(
                    None, speak, "Monitoring stopped.", self.alsa_output
                )
            return

        # Language gate: drop non-EN/ZH before routing to Zeebot (wake/sleep phrases
        # are already handled above and are exempt from this check).
        if not self._multilang and not _is_english_or_chinese(transcript):
            log.info("Dropped non-EN/ZH: %r", transcript)
            return

        # All other speech: only route to Zeebot when active
        if not self._active:
            log.debug("Silent mode — ignoring: %s", transcript)
            return

        # Continue phrase — resume paused TTS without asking Zeebot
        if _matches_phrase(normalized, CONTINUE_PHRASES):
            saved = _paused_speech[0]
            if saved:
                saved_text, saved_dev = saved
                _paused_speech[0] = (saved_text, saved_dev)  # keep in case resume is re-interrupted
                log.info("Resume: replaying %d chars", len(saved_text))
                _log_entry("system", "Resuming…")
                self._busy.set()
                try:
                    await asyncio.get_running_loop().run_in_executor(
                        None, _ft.partial(speak, saved_text, saved_dev, resumable=True)
                    )
                finally:
                    _busy_clear()
            else:
                log.info("Continue requested but nothing paused — asking Zeebot")
                # fall through to Zeebot routing
                pass
            return

        # New request — discard any paused speech
        _paused_speech[0] = None

        self._busy.set()
        try:
            log.info("Routing to Zeebot: %s", transcript)
            _log_entry("you", transcript)
            _log_entry("thinking", "Zeebot is thinking...")  # live counter shown on dashboard
            # Prefix tells Zeebot to ignore cron/heartbeat background context
            voice_msg = f"[voice] {transcript}"
            _think_task = asyncio.ensure_future(
                self.gw.ask(voice_msg, session_key=self.session_key)
            )
            _current_think_task[0] = _think_task
            try:
                reply = await _think_task
            except asyncio.CancelledError:
                log.info("Thinking interrupted via /interrupt")
                _log_entry("system", "Interrupted.")
                return
            finally:
                _current_think_task[0] = None
            # Detect gateway status tokens — Zeebot delivered its reply via the
            # `message` tool whose ack ("Sent.", "Done.", etc.) surfaced as the
            # chat-final text.  The actual content is in chat.history; fetch it.
            _reply_stripped = reply.strip().rstrip(".")
            if len(reply) < 25 and _reply_stripped.lower() in (
                "sent", "ok", "done", "error", "failed", "accepted", "received"
            ):
                log.info("Status token %r — fetching reply from history", reply)
                await asyncio.sleep(1.2)  # let message-tool result fully persist
                reply = await self.gw._reply_from_history(self.session_key)
                if not reply:
                    log.warning("History fallback also empty")
                    _log_entry("system", "No reply from Zeebot — please try again.")
                    await asyncio.get_running_loop().run_in_executor(
                        None, speak,
                        "Sorry, I didn't get a response. Please try again.",
                        self.alsa_output,
                    )
                    return
            log.info("Zeebot: %s", reply)
            _log_entry("zeebot", reply)
            await asyncio.get_running_loop().run_in_executor(
                None, _ft.partial(speak, reply, self.alsa_output, resumable=True)
            )
        except asyncio.TimeoutError:
            log.error("OpenClaw agent timed out")
            await asyncio.get_running_loop().run_in_executor(
                None, speak, "Sorry, I timed out on that.", self.alsa_output
            )
        except Exception as e:
            log.error("Error routing transcript: %s", e)
        finally:
            _busy_clear()

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
                            "transcription": {
                                "model": OPENAI_TRANSCRIBE_MODEL,
                                **( {"prompt": TRANSCRIPTION_PROMPT} if TRANSCRIPTION_PROMPT else {} ),
                            },
                            "turn_detection": {
                                "type":                "server_vad",
                                "threshold":           0.35,
                                "prefix_padding_ms":   500,
                                "silence_duration_ms": 700,
                            },
                        },
                    },
                },
            }))
            log.info("Session active — speak now (routed through Zeebot / OpenClaw)")

            import time as _rt
            in_stream = sd.InputStream(
                samplerate=DEVICE_RATE, channels=CHANNELS, dtype="int16",
                blocksize=DEVICE_BLOCKSIZE, callback=self._mic_cb,
                device=self.input_device,
            )
            in_stream.start()
            self._mic_stream_ref[0] = in_stream
            _last_mic_cb[0] = _rt.time()   # seed so watchdog doesn't fire immediately

            try:
                tasks = [
                    asyncio.create_task(self._send_mic(ws)),
                    asyncio.create_task(self._recv_ws(ws)),
                    asyncio.create_task(self.stop_event.wait()),
                    asyncio.create_task(self._watch_mic_stream()),
                ]
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
            finally:
                s = self._mic_stream_ref[0]
                if s:
                    try: s.stop(); s.close()
                    except Exception: pass
                self._mic_stream_ref[0] = None

# ── HTTP toggle server ────────────────────────────────────────────────────────

def start_http_server(port: int, on_stop, session_ref: list, loop=None):
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
            if self.path == "/continue":
                saved = _paused_speech[0]
                if saved and sess and loop:
                    import functools as _fct
                    saved_text, saved_dev = saved
                    def _resume():
                        import asyncio as _asyncio
                        _asyncio.run_coroutine_threadsafe(
                            sess._resume_from_http(saved_text, saved_dev), loop
                        )
                    threading.Thread(target=_resume, daemon=True).start()
                    log.info("HTTP continue — resuming %d chars", len(saved_text))
                self.send_response(302)
                self.send_header("Location", "/dashboard")
                self.end_headers()
            elif self.path == "/interrupt":
                # Cancel thinking task if pending
                task = _current_think_task[0]
                was_thinking = task is not None
                if was_thinking and loop is not None:
                    loop.call_soon_threadsafe(task.cancel)
                # Stop TTS if speaking
                _http_interrupt[0] = True
                if sess:
                    sess._busy.clear()
                    sess._active = True   # ensure back in listening mode
                _log_entry("system", "Interrupted — listening.")
                log.info("HTTP interrupt (thinking=%s speaking=%s)",
                         was_thinking, _is_speaking[0])
                self.send_response(302)
                self.send_header("Location", "/dashboard")
                self.end_headers()
            elif self.path == "/stop":
                _html(self, 200, "<h2>OpenClaw RealTimeTalk: stopping…</h2>")
                on_stop()
            elif self.path == "/restart":
                _html(self, 200, "<h2>Restarting…</h2><p>Page will reload in 5 seconds.</p><script>setTimeout(()=>location.href='/dashboard',5000)</script>")
                import os as _os
                threading.Thread(target=lambda: (
                    __import__('time').sleep(1),
                    __import__('subprocess').run([
                        'launchctl', 'kickstart', '-k',
                        f'gui/{_os.getuid()}/ai.openclaw.realtimetalk',
                    ])
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
                # Passive capture-only monitoring (no Zeebot, no TTS).
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
                CONVERSATION_LOG.clear()
                log.info("HTTP reset — conversation log cleared")
                self.send_response(302)
                self.send_header("Location", "/dashboard")
                self.end_headers()
            elif self.path == "/gateway-reset":
                CONVERSATION_LOG.clear()
                _log_entry("system", "Restarting OpenClaw gateway…")
                log.info("HTTP gateway-reset — restarting ai.openclaw.gateway")
                import os as _os2
                threading.Thread(target=lambda: (
                    __import__('time').sleep(0.3),
                    __import__('subprocess').run([
                        'launchctl', 'kickstart', '-k',
                        f'gui/{_os2.getuid()}/ai.openclaw.gateway',
                    ]),
                ), daemon=True).start()
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
                        return (f'<tr><td>{m.get("vol","-")}%</td>'
                                f'<td>{int(m.get("sw",1)*100)}%</td>'
                                f'<td style="color:{col}">SNR {snr:.1f}x</td></tr>')
                    spk_rows = "".join(_row(m) for m in prev.get("measurements", []))
                    sw_pct  = int(prev.get("safe_sw_vol", 1.0) * 100)
                    vol_pct = prev.get("safe_vol", "-")
                    warn = ('<div class="warn">Mic cannot hear speaker — use Manual adjustment below.</div>'
                            ) if prev.get("status") == "no_mic" else ""
                    prev_html = (warn +
                        f'<p>Last result: Vol <b>{vol_pct}%</b> + SW <b>{sw_pct}%</b></p>'
                        f'<table class="snrtbl"><tr><th>Vol</th><th>SW</th><th>Mic SNR</th></tr>{spk_rows}</table>')
                headset_notice = ('<p class="info" style="margin:4px 0;color:#fa0;">'
                    'Headset mode — use Manual adjustment to set volume.</p>'
                    ) if is_headset else ""
                spk_adj_section = f"""
<div class="sect"><h4>Manual adjustment</h4>
{headset_notice}
<table style="border-collapse:collapse;margin:4px 0;width:100%;">
  <tr>
    <td style="color:#5a7088;font-size:13px;width:32px;font-family:'JetBrains Mono',monospace;">Vol</td>
    <td style="font-weight:bold;font-size:1.1em;width:62px;font-family:'JetBrains Mono',monospace;" id="volval">{ds["spk_vol"]}</td>
    <td><div class="row" style="margin:0;gap:5px;">
      <button class="bQ" onclick="adjVol(-10)">− Quieter</button>
      <button class="bL" onclick="adjVol(+10)">+ Louder</button>
    </div></td>
  </tr>
  <tr>
    <td style="color:#5a7088;font-size:13px;font-family:'JetBrains Mono',monospace;">SW</td>
    <td style="font-weight:bold;font-size:1.1em;font-family:'JetBrains Mono',monospace;" id="swval">{ds["sw_pct"]}%</td>
    <td><div class="row" style="margin:0;gap:5px;">
      <button class="bQ" onclick="adjSW(-10)">− Softer</button>
      <button class="bL" onclick="adjSW(+10)">+ Louder</button>
    </div></td>
  </tr>
  <tr>
    <td style="color:#5a7088;font-size:13px;font-family:'JetBrains Mono',monospace;">Eff</td>
    <td style="font-weight:bold;font-size:1.1em;color:#34d399;font-family:'JetBrains Mono',monospace;" id="effval">{ds["effective_pct"]}%</td>
    <td style="color:#5a7088;font-size:12px;">Vol × SW combined</td>
  </tr>
</table>
<div class="row" style="margin:4px 0;">
  <button id="btnPlay" class="bP" onclick="startLoop()">Play test</button>
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
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Calibration — RealTimeTalk</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600&family=JetBrains+Mono:wght@500;600&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{--bg:#07090f;--sf:#0d1119;--sf2:#121925;--bd:#1a2535;--tx:#dde4ef;--mu:#5a7088;--di:#253344;--you:#38bdf8;--bot:#f59e0b;--bb:#130e02;--rd:#ef4444;--rdb:#150303;--gn:#34d399;--gnb:#021a0e;--r:8px;}}
body{{font-family:'Outfit',system-ui,sans-serif;font-size:15px;background:var(--bg);color:var(--tx);padding:12px 16px;max-width:680px;-webkit-text-size-adjust:100%;}}
.ph{{display:flex;align-items:center;gap:10px;margin-bottom:12px;padding-bottom:10px;border-bottom:1px solid var(--bd);}}
.pt{{font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:600;color:var(--tx);letter-spacing:.08em;text-transform:uppercase;}}
a.back{{margin-left:auto;display:inline-flex;align-items:center;gap:4px;padding:5px 12px;border-radius:8px;font-size:13px;font-weight:500;color:var(--mu);background:var(--sf2);border:1px solid var(--bd);text-decoration:none;transition:border-color .12s,color .12s;}}
a.back:hover{{border-color:var(--you);color:var(--you);}}
.devpanel{{font-family:'JetBrains Mono',monospace;font-size:12px;color:#8aa0b8;line-height:1.7;padding:7px 10px;background:var(--bg);border-radius:5px;border:1px solid var(--di);margin-bottom:10px;}}
.devpanel b{{color:var(--tx);}}
.sect{{border-top:1px solid var(--bd);margin-top:14px;padding-top:10px;}}
h4{{font-family:'Outfit',sans-serif;font-size:14px;font-weight:600;color:var(--you);margin:0 0 6px;}}
.info{{color:var(--mu);font-size:13px;margin:3px 0;}}
.warn{{background:#3a1500;border:1px solid #7a3000;border-radius:6px;padding:6px 10px;margin-bottom:6px;font-size:13px;color:var(--bot);}}
canvas{{width:100%;height:38px;border-radius:5px;display:block;margin:6px 0;}}
#micinfo{{font-size:12px;color:var(--mu);margin:2px 0;min-height:16px;font-family:'JetBrains Mono',monospace;}}
#micresult{{margin-top:6px;padding:7px 10px;background:var(--gnb);border:1px solid var(--gn);border-radius:6px;font-size:13px;color:var(--gn);display:none;}}
#calstatus{{margin:4px 0;font-size:13px;min-height:16px;color:var(--mu);font-family:'JetBrains Mono',monospace;}}
#mstatus{{margin-top:4px;font-size:13px;color:var(--mu);}}
.row{{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0;}}
button{{padding:7px 14px;border:1px solid var(--bd);color:var(--mu);background:var(--sf2);border-radius:8px;font-family:'Outfit',sans-serif;font-size:14px;font-weight:500;cursor:pointer;transition:border-color .12s,color .12s,background .12s;}}
button:hover{{border-color:var(--you);color:var(--you);background:#1e2d3d;}}
button:disabled{{opacity:.4;cursor:default;border-color:var(--bd);color:var(--mu);background:var(--sf2);}}
#micbtn,#acbtn{{color:var(--gn);border-color:var(--gn);background:var(--gnb);}}
#micbtn:hover,#acbtn:hover{{background:#042e18;color:var(--gn);border-color:var(--gn);}}
.bL{{color:var(--gn);border-color:var(--gn);background:var(--gnb);}}
.bL:hover{{background:#042e18;}}
.bP{{color:var(--you);border-color:var(--you);background:#051928;}}
.bP:hover{{background:#0a2840;}}
.bS{{color:var(--rd);border-color:var(--rd);background:var(--rdb);}}
.bS:hover{{background:#2a0808;}}
.bSet{{color:var(--bot);border-color:var(--bot);background:var(--bb);}}
.bSet:hover{{background:#261b03;}}
.snrtbl{{border-collapse:collapse;font-size:12px;margin:6px 0;width:100%;font-family:'JetBrains Mono',monospace;}}
.snrtbl th{{background:var(--sf2);color:var(--mu);font-weight:600;border:1px solid var(--bd);padding:4px 8px;text-align:left;}}
.snrtbl td{{border:1px solid var(--bd);padding:4px 8px;color:var(--tx);}}
.snrtbl tr.active-row{{background:var(--gnb);}}
.use-btn{{padding:4px 10px;font-size:12px;background:var(--sf2);border:1px solid var(--bd);color:var(--mu);border-radius:5px;cursor:pointer;white-space:nowrap;font-family:'Outfit',sans-serif;transition:border-color .12s,color .12s;}}
.use-btn:hover{{border-color:var(--you);color:var(--you);}}
.use-btn.active{{background:var(--gnb);border-color:var(--gn);color:var(--gn);cursor:default;}}
#devbtn{{color:var(--you);border-color:var(--you);background:#051928;}}
#devbtn:hover{{background:#0a2840;color:var(--you);border-color:var(--you);}}
#devtoggle{{color:var(--you);cursor:pointer;font-size:13px;background:none;border:none;padding:0;margin-left:6px;font-family:'Outfit',sans-serif;}}
#devlist{{margin-top:8px;}}
#devmsg{{font-size:13px;color:var(--bot);font-family:'JetBrains Mono',monospace;}}
a{{color:var(--you);text-decoration:none;}}
a:hover{{text-decoration:underline;}}
</style></head><body>
<div class="ph">
  <span class="pt">&#9679;&nbsp;Calibration</span>
  <a href="/dashboard" class="back">&#8592; Dashboard</a>
</div>
<div class="devpanel" id="curdev">
  <b>Mic:</b> {ds["mic"]} &nbsp;&middot;&nbsp; Gate: <span id="panelgate">{ds["gate"]}</span> &nbsp;&middot;&nbsp; Gain: {ds["gain"]}x<br>
  <b>Speaker:</b> {ds["speaker_name"]} &nbsp;&middot;&nbsp; Vol: <span id="panelvol">{ds["spk_vol"]}</span> &nbsp;&middot;&nbsp; SW: <span id="panelsw">{ds["sw_pct"]}%</span> &nbsp;&middot;&nbsp; <b>Eff: <span id="paneleff" style="color:var(--gn)">{ds["effective_pct"]}%</span></b>
</div>
<div style="display:flex;align-items:center;gap:8px;margin:4px 0 10px;flex-wrap:wrap;">
  <span style="font-size:12px;color:var(--mu);font-family:'JetBrains Mono',monospace;">Cal mode:</span>
  <b style="font-size:13px;color:{'#f59e0b' if is_headset else '#34d399'};">{_mode_label}</b>
  <button onclick="setCalMode('headset')" style="padding:4px 11px;font-size:13px;{'color:#f59e0b;border-color:#f59e0b;background:#130e02;' if is_headset and _override else ''}">Headset</button>
  <button onclick="setCalMode('speaker')" style="padding:4px 11px;font-size:13px;{'color:#34d399;border-color:#34d399;background:#021a0e;' if not is_headset and _override else ''}">Speaker</button>
  <button onclick="setCalMode('auto')" style="padding:4px 11px;font-size:13px;{'color:#38bdf8;border-color:#38bdf8;background:#051928;' if _override is None else ''}">Auto</button>
</div>
{spk_adj_section}
<div style="margin:14px 0 6px;display:flex;align-items:center;gap:10px;">
  <button id="devbtn" onclick="toggleDevices()">Audio Devices</button>
  <span id="devtoggle" onclick="toggleDevices()">&#9660; expand</span>
</div>
<div id="devlist" style="display:none;">
  <div id="devout" style="font-size:14px;">Loading…</div>
</div>
<div class="sect"><h4>Mic calibration</h4>
<p class="info">Yellow line = gate threshold. Speech above passes; noise below is silenced.</p>
<canvas id="meter" height="36"></canvas>
<div id="micinfo"></div>
<div style="display:flex;align-items:center;gap:8px;margin:6px 0;">
  <span style="font-size:12px;color:var(--mu);white-space:nowrap;font-family:'JetBrains Mono',monospace;">Gate:</span>
  <input type="range" id="gateslider" min="{MIC_GATE_MIN}" max="{MIC_GATE_MAX}" step="25"
         value="{gate}" style="flex:1;accent-color:#f59e0b;" oninput="onGateSlide(this.value)"
         onchange="saveGate(this.value)">
  <span id="gateval" style="font-size:13px;color:#f59e0b;font-weight:bold;width:40px;text-align:right;font-family:'JetBrains Mono',monospace;">{gate}</span>
</div>
<div id="micresult"></div>
<div class="row">
  <button id="micbtn" onclick="startMicCal()">Auto-calibrate (3 sec quiet)</button>
</div>
</div>
{auto_cal_section}
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
  const ev=document.getElementById('effval');
  if(vv) vv.textContent=d.spk_vol;
  if(sv) sv.textContent=d.sw_pct+'%';
  if(ev) ev.textContent=d.effective_pct+'%';
  // Keep top panel in sync
  const pv=document.getElementById('panelvol');
  const ps=document.getElementById('panelsw');
  const pe=document.getElementById('paneleff');
  const pg=document.getElementById('panelgate');
  if(pv) pv.textContent=d.spk_vol;
  if(ps) ps.textContent=d.sw_pct+'%';
  if(pe) pe.textContent=d.effective_pct+'%';
  if(pg) pg.textContent=d.gate;
  const bp=document.getElementById('btnPlay');
  if(bp) bp.disabled=d.loop_playing;
}});}}
function adjVol(d){{fetch('/speaker-cal/adjust?type=vol&delta='+d).then(()=>upd());}}
function adjSW(d){{fetch('/speaker-cal/adjust?type=sw&delta='+d).then(()=>upd());}}
function startLoop(){{
  const bp=document.getElementById('btnPlay');
  if(bp) bp.disabled=true;
  fetch('/speaker-cal/loop-start').then(()=>{{
    const m=document.getElementById('mstatus');if(m)m.textContent='Playing test loop…';}});}}
function stopLoop(){{fetch('/speaker-cal/loop-stop').then(()=>{{
  const bp=document.getElementById('btnPlay');if(bp) bp.disabled=false;
  const m=document.getElementById('mstatus');if(m)m.textContent='Stopped.';}});}}
function setLevel(){{fetch('/speaker-cal/set').then(r=>r.json()).then(d=>{{
  const m=document.getElementById('mstatus');
  if(m)m.textContent='Level saved: Vol '+d.spk_vol+' × SW '+d.sw_pct+'% = '+d.effective_pct+'% effective';
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
                    new_gate = max(MIC_GATE_MIN, min(MIC_GATE_MAX, int(noise_peak * 1.5)))
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
                            return (f'<tr><td>{m.get("vol","-")}%</td>'
                                    f'<td>{int(m.get("sw",1)*100)}%</td>'
                                    f'<td style="color:{col}">SNR {snr:.1f}×</td></tr>')
                        rows    = "".join(_row(m) for m in prev.get("measurements", []))
                        sw_pct  = int(prev.get("safe_sw_vol", 1.0) * 100)
                        vol_pct = prev.get("safe_vol", "-")
                        warn = ('<div style="background:#5a1a00;border-radius:6px;padding:8px;'
                                'margin-bottom:6px;">Mic cannot hear speaker — use Manual adjustment below.</div>'
                                ) if prev.get("status") == "no_mic" else ""
                        prev_html = (
                            warn +
                            f'<h4>Last result: Vol <b>{vol_pct}%</b> + SW <b>{sw_pct}%</b></h4>'
                            f'<table border=1 style="border-collapse:collapse;font-size:12px">'
                            f'<tr><th>Vol</th><th>SW</th><th>Mic SNR</th></tr>{rows}</table>'
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
      'Set to Vol <b>'+d.safe_vol+'%</b> SW <b>'+Math.round(d.safe_sw_vol*100)+'%</b>');
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

                # Announce calibration result at the calibrated level.
                # run_speaker_calibration() already set system vol + _cal_sw_volume.
                if sess:
                    import threading as _t
                    sw  = result.get("safe_sw_vol", _cal_sw_volume)
                    vol = result.get("safe_vol", _get_system_volume())
                    def _cal_announce(sw=sw, vol=vol,
                                      alsa=sess.alsa_output,
                                      st=result.get("status", "ok")):
                        if st == "no_mic":
                            msg = ("Auto calibration could not measure the speaker. "
                                   "Microphone and speaker are not acoustically coupled. "
                                   "Use Manual adjustment to set the volume.")
                        elif st == "ok":
                            msg = (f"Calibration done. "
                                   f"Volume set to {vol} percent, software level {int(sw*100)} percent.")
                        else:
                            msg = ("Calibration could not complete. "
                                   "Speaker set to minimum. Use Manual adjustment.")
                        # Ensure system vol is at calibrated level before speaking
                        _set_system_volume(vol)
                        speak(msg, alsa, volume=sw)
                    _t.Thread(target=_cal_announce, daemon=True).start()
                resp = _json.dumps(result).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            elif self.path == "/speaker-cal/loop-start":
                _headset_cal_loop[0] = True
                def _loop():
                    import tempfile as _tf, time as _t2
                    # Render the test phrase once at full amplitude (gain applied live).
                    phrase = "This is an audio test. One, two, three, four, five."
                    parts  = []
                    for seg, lang in _split_by_script(strip_markdown(phrase)):
                        voice = EDGE_VOICE_ZH if lang == "zh" else EDGE_VOICE_EN
                        mp3 = _tf.mktemp(suffix=".mp3")
                        if _edge_tts_to_mp3(seg, voice, mp3):
                            pcm = _decode_to_pcm(mp3)
                            if pcm.size: parts.append(pcm)
                        try: os.unlink(mp3)
                        except FileNotFoundError: pass
                    if not parts:
                        log.warning("loop-start: TTS render failed")
                        return
                    # Normalised float32 phrase, looped seamlessly via callback.
                    phrase_f32 = np.concatenate(parts).astype(np.float32) / 32768.0
                    n_phrase   = len(phrase_f32)
                    pos        = [0]

                    def _cb(outdata, frames, _time, _status):
                        gain = (_cal_sys_vol_pct[0] / 100.0) * _cal_sw_volume
                        needed = frames
                        out    = np.empty(needed, dtype=np.float32)
                        filled = 0
                        while filled < needed:
                            avail = min(n_phrase - pos[0], needed - filled)
                            out[filled:filled + avail] = phrase_f32[pos[0]:pos[0] + avail]
                            filled  += avail
                            pos[0]   = (pos[0] + avail) % n_phrase
                        outdata[:, 0] = np.clip(out * gain, -1.0, 1.0)

                    out_dev = _selected_output_device[0]
                    try:
                        with sd.OutputStream(samplerate=TTS_SAMPLE_RATE, device=out_dev,
                                             channels=1, dtype='float32',
                                             blocksize=1024, callback=_cb):
                            while _headset_cal_loop[0]:
                                _t2.sleep(0.05)
                    except Exception as e:
                        log.error("Test loop error: %s", e)
                import threading as _tloop
                _tloop.Thread(target=_loop, daemon=True).start()
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
                    cur_sw  = int(_cal_sw_volume * 100)
                    new_sw  = _snap10(cur_sw, delta)
                    globals()['_cal_sw_volume'] = new_sw / 100.0
                else:
                    cur_vol = _cal_sys_vol_pct[0]
                    new_vol = _snap10(cur_vol, delta)
                    _set_system_volume(new_vol)

                # Auto-save so switching devices doesn't lose the adjustment.
                try:
                    _od = sd.query_devices(_selected_output_device[0]
                                           if _selected_output_device[0] is not None else None,
                                           kind="output")
                    _save_device_cal(_od.get("name", "default"),
                                     _cal_sw_volume, _cal_sys_vol_pct[0])
                except Exception:
                    pass

                resp = _json.dumps(_get_device_status()).encode()
                self.send_response(200); self.send_header("Content-Type","application/json")
                self.send_header("Content-Length", str(len(resp))); self.end_headers()
                self.wfile.write(resp)

            elif self.path == "/device-status":
                import json as _json
                try:
                    # Query devices in a subprocess so PortAudio re-initialises and
                    # picks up hot-plug changes (plugged/unplugged mics/speakers).
                    _qr = subprocess.run(
                        [sys.executable, "-c",
                         "import sounddevice as sd, json;"
                         "print(json.dumps([{'name':d['name'],"
                         "'max_input_channels':d['max_input_channels'],"
                         "'max_output_channels':d['max_output_channels']}"
                         " for d in sd.query_devices()]))"],
                        capture_output=True, text=True, timeout=5)
                    _devs = _json.loads(_qr.stdout) if _qr.returncode == 0 else sd.query_devices()
                    _out_idx = _selected_output_device[0]
                    _in_idx  = _selected_input_device[0]
                    if _out_idx is None:
                        for _i, _d in enumerate(_devs):
                            if _d["max_output_channels"] > 0:
                                _out_idx = _i; break
                    if _in_idx is None:
                        for _i, _d in enumerate(_devs):
                            if _d["max_input_channels"] > 0:
                                _in_idx = _i; break
                    _default_sink   = str(_out_idx) if _out_idx is not None else ""
                    _default_source = str(_in_idx)  if _in_idx  is not None else ""
                    _sinks, _sources = [], []
                    for _i, _d in enumerate(_devs):
                        _bt = any(kw in _d["name"].lower()
                                  for kw in ("airpod","bluetooth","wireless"))
                        if _d["max_output_channels"] > 0:
                            _sinks.append({
                                "name":  str(_i),
                                "desc":  _d["name"],
                                "state": "RUNNING" if str(_i) == _default_sink else "SUSPENDED",
                                "card":  None if _bt else str(_i),
                            })
                        if _d["max_input_channels"] > 0:
                            _sources.append({
                                "name":  str(_i),
                                "desc":  _d["name"],
                                "state": "RUNNING" if str(_i) == _default_source else "SUSPENDED",
                                "card":  None if _bt else str(_i),
                            })
                    data = {
                        "default_sink":   _default_sink,
                        "default_source": _default_source,
                        "sinks":   _sinks,
                        "sources": _sources,
                        "alsa_cards": [],
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
                    _dev_idx = int(dev_name)
                    _devs    = sd.query_devices()
                    if _dev_idx < 0 or _dev_idx >= len(_devs):
                        raise ValueError(f"Device index {_dev_idx} out of range (0–{len(_devs)-1})")
                    _dev_info = _devs[_dev_idx]
                    if dev_type == "sink":
                        if _dev_info["max_output_channels"] < 1:
                            raise ValueError(f"Device {_dev_idx} has no output channels")
                        _selected_output_device[0] = _dev_idx
                        _update_service_alsa_output(str(_dev_idx))
                        _known = _apply_device_cal(_dev_info["name"])
                        _save_device_prefs(output_name=_dev_info["name"])
                        log.info("HTTP device-set: output → %d %s (%s)",
                                 _dev_idx, _dev_info["name"],
                                 "calibrated" if _known else "new/unknown → minimum")
                        result["ok"]  = True
                        result["msg"] = (
                            f"Speaker set to {_dev_info['name']}. "
                            + ("Restored calibrated levels. " if _known
                               else "New device — starting at minimum. Use Manual adjustment. ")
                            + "Restarting audio…"
                        )
                    elif dev_type == "source":
                        if _dev_info["max_input_channels"] < 1:
                            raise ValueError(f"Device {_dev_idx} has no input channels")
                        _selected_input_device[0] = _dev_idx
                        _update_service_input_source(str(_dev_idx))
                        _save_device_prefs(input_name=_dev_info["name"])
                        log.info("HTTP device-set: input → %d %s", _dev_idx, _dev_info["name"])
                        result["ok"]  = True
                        result["msg"] = f"Mic set to {_dev_info['name']}. Restarting audio…"
                    else:
                        result["msg"] = "Missing type or name"
                    if result["ok"]:
                        _uid = os.getuid()
                        threading.Thread(target=lambda: (
                            __import__("time").sleep(0.5),
                            __import__("subprocess").run(
                                ["launchctl", "kickstart", "-k",
                                 f"gui/{_uid}/ai.openclaw.realtimetalk"],
                                capture_output=True)
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
                import json as _json
                _headset_cal_loop[0] = False
                sys_vol  = _get_system_volume()
                sw_vol   = _cal_sw_volume
                out_dev  = _selected_output_device[0]
                out_name = "default"
                try:
                    _od = sd.query_devices(out_dev if out_dev is not None else None, kind="output")
                    out_name = _od.get("name", "default")
                except Exception:
                    pass
                _save_device_cal(out_name, sw_vol, sys_vol)
                log.info("Manual cal saved: %r Vol=%d%% SW=%d%%",
                         out_name, sys_vol, int(sw_vol * 100))
                if sess:
                    import threading as _t3
                    _t3.Thread(
                        target=speak,
                        args=(f"Audio settings saved. Volume {sys_vol} percent, software {int(sw_vol*100)} percent.",
                              sess.alsa_output),
                        daemon=True,
                    ).start()
                resp = _json.dumps(_get_device_status()).encode()
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
                    msg = "Audio devices changed."
                    _device_change_msg[0] = msg
                    log.info("Device change detected on /log refresh")
                    if sess and sess._active:
                        import threading as _t
                        def _announce_change():
                            import time as _time; _time.sleep(0.5)
                            # Apply saved cal for the current output device (or minimum if new)
                            _out = _selected_output_device[0]
                            try:
                                _od = sd.query_devices(
                                    _out if _out is not None else None, kind="output")
                                _apply_device_cal(_od.get("name", "default"))
                            except Exception:
                                pass
                            speak(msg, sess.alsa_output)
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
                # Pre-compute how long each "thinking" entry waited for a Zeebot reply.
                # None = still thinking (show live counter); float = seconds taken (show static).
                thinking_dur: dict = {}
                for _i, _e in enumerate(CONVERSATION_LOG):
                    if _e["role"] == "thinking":
                        _ep = _e.get("epoch", 0.0)
                        for _j in range(_i + 1, len(CONVERSATION_LOG)):
                            _jr = CONVERSATION_LOG[_j]["role"]
                            if _jr in ("zeebot", "system"):
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
                    elif e["role"] == "zeebot":
                        rows += f'<div class="zeebot">{ts_span}<b>Zeebot:</b> {e["text"]}</div>'
                    elif e["role"] == "monitor":
                        rows += f'<div class="mon">{ts_span}{e["text"]}</div>'
                    elif e["role"] == "thinking":
                        ep  = e.get("epoch", 0.0)
                        dur = thinking_dur.get(ep)
                        if dur is None:
                            # Still waiting — live counter + interrupt button
                            rows += (f'<div class="thinking">{ts_span}'
                                     f'Zeebot is thinking... '
                                     f'<span class="tctr" data-start="{ep:.3f}">0</span>s'
                                     f' &nbsp;<a href="/interrupt" class="irupt">✕ Interrupt</a></div>')
                        # else: Zeebot replied — hide this line entirely
                    else:
                        rows += f'<div class="sys">{ts_span}{e["text"]}</div>'
                # All device info gathered outside do_GET to avoid UnboundLocalError scoping
                _ds = _get_device_status()
                device_panel = (
                    f'<div id="dp">'
                    f'&#127908; {_ds["mic"]} &ensp;'
                    f'&#128266; {_ds["speaker_name"]} &middot; Vol {_ds["spk_vol"]} &middot; SW {_ds["sw_pct"]}% &ensp;'
                    f'Gate {_ds["gate"]} &middot; Gain {_ds["gain"]}x'
                    f'</div>'
                )

                paused   = _paused_speech[0] is not None
                speaking = _is_speaking[0]
                thinking = _current_think_task[0] is not None
                state = ("MONITORING" if monitoring
                         else "SPEAKING" if speaking
                         else "THINKING" if thinking
                         else "PAUSED" if (active and paused)
                         else "ACTIVE" if active else "SILENT")
                _sc = {"ACTIVE":("#0d2818","#34d399"),"SILENT":("#141d2b","#64748b"),
                       "THINKING":("#1c1304","#f59e0b"),"SPEAKING":("#031a10","#2dd4bf"),
                       "PAUSED":("#150d2e","#a5b4fc"),"MONITORING":("#071a2e","#60a5fa"),
                       }.get(state, ("#141d2b","#64748b"))
                state_pill_style = f"background:{_sc[0]};color:{_sc[1]};border-color:{_sc[1]};"
                speaking_banner = (
                    '<div class="speaking">&#128266; Zeebot is speaking&hellip;'
                    ' &nbsp;<a href="/interrupt" class="irupt">&#10005; Stop</a></div>'
                    if speaking else
                    '<div class="speaking">&#9646;&#9646; Paused'
                    ' &nbsp;<a href="/continue" class="cont">&#9654; Continue</a></div>'
                    if (active and paused) else ""
                )
                body = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta http-equiv="refresh" content="3">
<title>RealTimeTalk</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600&family=JetBrains+Mono:wght@500;600&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{--bg:#07090f;--sf:#0d1119;--sf2:#121925;--bd:#1a2535;--tx:#dde4ef;--mu:#5a7088;--di:#253344;--you:#38bdf8;--yb:#051928;--bot:#f59e0b;--bb:#130e02;--mon:#a78bfa;--mb:#0e0820;--sy:#304558;--rd:#ef4444;--rdb:#150303;--gn:#34d399;--gnb:#021a0e;--r:8px;}}
html,body{{height:100%;}}
body{{font-family:'Outfit',system-ui,sans-serif;font-size:16px;background:var(--bg);color:var(--tx);display:flex;flex-direction:column;overflow:hidden;-webkit-text-size-adjust:100%;}}
#top{{flex-shrink:0;background:var(--sf);border-bottom:1px solid var(--bd);padding:10px 14px 8px;}}
.hrow{{display:flex;align-items:center;gap:8px;margin-bottom:8px;}}
.brand{{font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:600;color:var(--tx);letter-spacing:.08em;text-transform:uppercase;}}
.spill{{margin-left:10px;font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;padding:5px 14px;border-radius:20px;border:2px solid transparent;white-space:nowrap;}}
.nav{{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:7px;}}
a.btn{{display:inline-flex;align-items:center;gap:3px;padding:7px 14px;border-radius:8px;font-family:'Outfit',sans-serif;font-size:14px;font-weight:500;color:var(--mu);background:var(--sf2);border:1px solid var(--bd);text-decoration:none;min-height:36px;white-space:nowrap;transition:background .12s,border-color .12s,color .12s;}}
a.btn:hover{{background:#1e2d3d;border-color:var(--you);color:var(--you);}}
a.btn.on{{background:var(--gnb);border-color:var(--gn);color:var(--gn);}}
a.btn.on:hover{{background:#053d20;border-color:var(--gn);color:#fff;}}
a.btn.danger{{color:var(--rd);}}
a.btn.danger:hover{{background:var(--rdb);border-color:var(--rd);}}
#dp{{font-family:'JetBrains Mono',monospace;font-size:12px;color:#8aa0b8;line-height:1.7;padding:6px 10px;background:var(--bg);border-radius:5px;border:1px solid var(--di);margin-top:4px;}}
#log{{flex:1;overflow-y:auto;padding:10px 14px;}}
.you{{background:var(--yb);border-left:3px solid var(--you);border-radius:var(--r);padding:8px 10px;margin:3px 0;}}
.you b{{color:var(--you);}}
.zeebot{{background:var(--bb);border-left:3px solid var(--bot);border-radius:var(--r);padding:8px 10px;margin:3px 0;}}
.zeebot b{{color:var(--bot);}}
.mon{{background:var(--mb);border-left:3px solid var(--mon);border-radius:var(--r);padding:8px 10px;margin:3px 0;}}
.sys{{color:var(--sy);font-size:.8em;text-align:center;margin:3px 0;font-family:'JetBrains Mono',monospace;}}
.thinking{{background:var(--bb);border-left:3px solid var(--bot);border-radius:var(--r);padding:8px 10px;margin:3px 0;color:var(--bot);font-style:italic;}}
.speaking{{background:var(--gnb);border-left:3px solid var(--gn);border-radius:var(--r);padding:8px 10px;margin:3px 0;color:var(--gn);font-style:italic;}}
.ts{{font-family:'JetBrains Mono',monospace;font-size:.75em;color:var(--di);margin-right:4px;}}
a.irupt{{color:var(--rd);background:var(--rdb);border:1px solid var(--rd);border-radius:4px;padding:2px 8px;font-size:.82em;font-style:normal;text-decoration:none;margin-left:8px;}}
a.irupt:hover{{background:var(--rd);color:#fff;}}
a.cont{{color:var(--gn);background:var(--gnb);border:1px solid var(--gn);border-radius:4px;padding:2px 8px;font-size:.82em;font-style:normal;text-decoration:none;margin-left:8px;}}
a.cont:hover{{background:var(--gn);color:#000;}}
@media(max-width:520px){{body{{font-size:15px;}}#top{{padding:10px 12px 8px;}}a.btn{{padding:9px 13px;min-height:42px;font-size:14px;}}#dp{{font-size:12px;}}#log{{padding:8px 10px;}}}}
@media(min-width:900px){{body{{font-size:17px;}}#top{{padding:14px 24px 10px;}}a.btn{{font-size:15px;padding:8px 16px;min-height:38px;}}#dp{{font-size:13px;}}#log{{padding:14px 24px;}}}}
</style></head><body>
<div id="top">
<div class="hrow"><span class="brand">&#9679;&nbsp;RealTimeTalk</span><span class="spill" style="{state_pill_style}">{state}</span><a href="/calibration" class="btn" style="margin-left:10px;">&#127908; Calibrate</a></div>
<div class="nav"><a href="/wake" class="btn">&#9889; Wake</a><a href="/sleep" class="btn">&#128276; Sleep</a><a href="/monitor/start" class="btn {'on' if monitoring else ''}">&#128065; Monitor On</a><a href="/monitor/stop" class="btn">Monitor Off</a><a href="/multilang" class="btn {'on' if multilang else ''}">&#127760; {'ON' if multilang else 'OFF'} Multi-lang</a><a href="/reset" class="btn danger">&#10006; Clear Log</a><a href="/restart" class="btn">&#8635; Restart</a><a href="/gateway-reset" class="btn danger">&#9888; Gateway Reset</a><a href="/dashboard" class="btn">&#8635;</a></div>
{device_panel}{device_banner}</div>
<div id="log">{rows if rows else "<div class='sys'>No conversation yet</div>"}{speaking_banner}</div>
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
    start_http_server(http_port, lambda: loop.call_soon_threadsafe(stop_event.set), session_ref, loop=loop)
    log.info("OpenClaw RealTimeTalk daemon starting — silent mode (say 'Zeebot wake up' to activate)")

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
    recommended = max(MIC_GATE_MIN, min(MIC_GATE_MAX, int(noise_peak * 1.5)))
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
    _agc_gain[0]  = MIC_GAIN   # seed numpy AGC fallback

    # Initialise WebRTC processor (aggressiveness 2 = moderate NS + AGC2).
    # Only 16 kHz is supported by the library; we resample in _mic_cb.
    try:
        from webrtc_noise_gain import AudioProcessor as _WrtcAP
        _webrtc_proc = _WrtcAP(16000, 2)
        log.info("WebRTC AGC2 + NS active (aggressiveness=2, 16 kHz)")
    except Exception as _e:
        log.warning("webrtc-noise-gain unavailable (%s) — using numpy AGC fallback", _e)
    if args.input_source:
        log.warning("--input-source is ignored on Mac; use --input-device <idx> instead")
    _selected_input_device[0]  = args.input_device
    _selected_output_device[0] = args.output_device

    # If no explicit device flags, restore from last-used device prefs.
    _prefs = _load_device_prefs()
    if _selected_output_device[0] is None and _prefs.get("output_device_name"):
        _idx = _resolve_device_by_name(_prefs["output_device_name"], "output")
        if _idx is not None:
            _selected_output_device[0] = _idx
            log.info("Restored output device from prefs: %r → #%d",
                     _prefs["output_device_name"], _idx)
        else:
            log.info("Saved output device %r not found, using system default",
                     _prefs["output_device_name"])
    if _selected_input_device[0] is None and _prefs.get("input_device_name"):
        _idx = _resolve_device_by_name(_prefs["input_device_name"], "input")
        if _idx is not None:
            _selected_input_device[0] = _idx
            log.info("Restored input device from prefs: %r → #%d",
                     _prefs["input_device_name"], _idx)
        else:
            log.info("Saved input device %r not found, using system default",
                     _prefs["input_device_name"])

    # Save the resolved devices as the new prefs baseline.
    try:
        _pout = sd.query_devices(_selected_output_device[0]
                                 if _selected_output_device[0] is not None else None,
                                 kind="output").get("name")
        _pin  = sd.query_devices(_selected_input_device[0]
                                 if _selected_input_device[0] is not None else None,
                                 kind="input").get("name")
        _save_device_prefs(output_name=_pout, input_name=_pin)
    except Exception:
        pass

    _mic_gate_ref[0] = MIC_GATE_PEAK
    log.info("Audio: in=%s out=%s gain=%.1f gate=%d",
             _device_label(_selected_input_device[0]),
             _device_label(_selected_output_device[0]),
             MIC_GAIN, MIC_GATE_PEAK)

    # Seed fingerprint so first dashboard load doesn't falsely announce a change.
    _audio_fingerprint[0] = _get_audio_fingerprint()

    bt_warn = _bt_mic_warning(_selected_input_device[0])
    if bt_warn:
        log.warning(bt_warn)

    # Load per-device calibration store and apply to current selected output
    _load_cal_store()
    try:
        _out_d = sd.query_devices(_selected_output_device[0]
                                  if _selected_output_device[0] is not None else None,
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
