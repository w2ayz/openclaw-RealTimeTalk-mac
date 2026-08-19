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

__version__ = "3.14.0"

import argparse
import asyncio
import base64
import collections
import datetime
import json
import logging
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np
import sounddevice as sd
import websockets

try:
    from zhconv import convert as _zh_convert   # traditional → simplified
except Exception:                                # pragma: no cover
    _zh_convert = None

try:
    import sherpa_onnx
    _HAVE_SHERPA = True
except ImportError:
    _HAVE_SHERPA = False

try:
    import serial as _pyserial
    _HAVE_PYSERIAL = True
except ImportError:
    _HAVE_PYSERIAL = False

import radio_interfaces as _radio

# ── Constants ─────────────────────────────────────────────────────────────────

OPENCLAW_CONFIG   = os.path.expanduser("~/.openclaw/openclaw.json")
OPENCLAW_GW_URL   = "ws://127.0.0.1:18789"
OPENCLAW_SESSION  = "agent:main:main"

# OpenAI TTS — fallback TTS engine when ElevenLabs is unavailable.
OPENAI_TTS_MODEL  = "tts-1-hd"
OPENAI_TTS_VOICE  = "nova"        # nova works well for Chinese and English
OPENAI_TTS_TIMEOUT = 15.0         # seconds before falling back to say
_openai_tts_key: list = [""]      # set from openai_key in main()

# ElevenLabs TTS — primary voice engine for all assistant replies.
ELEVENLABS_VOICE_ID = "pFZP5JQG7iQjIQuC4Bku"   # "Lily - Velvety Actress"
ELEVENLABS_MODEL    = "eleven_v3"
ELEVENLABS_TIMEOUT  = 15.0
_elevenlabs_tts_key: list = [""]  # set from load_elevenlabs_key() in main()

# Edge TTS skill — kept for reference but no longer primary
EDGE_TTS_SCRIPT   = os.path.expanduser(
    "~/.openclaw/workspace/skills/edge-tts/scripts/tts-converter.js"
)
EDGE_VOICE_EN     = "en-US-AriaNeural"
EDGE_VOICE_ZH     = "zh-CN-XiaoxiaoNeural"
EDGE_TTS_TIMEOUT  = 8.0
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
AGENT_TIMEOUT_S   = 90
AUTO_SLEEP_SECS   = 600          # go silent after 10 min of no interaction
# Languages accepted in multi-lang WHITELIST mode (langdetect codes + script tokens).
MULTILANG_WHITELIST_LANGS: list = ["en", "zh-cn", "zh-tw", "zh", "ko", "ja", "es", "ms"]
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
SLEEP_STATE_FILE  = os.path.expanduser("~/.openclaw/workspace/rtt_sleep_state.json")
# Speaker verification (owner-only mode) — 3D-Speaker CAM++ zh-en model via
# sherpa-onnx. Embeddings gate transcripts so only the enrolled owner's voice
# is acted on. Missing lib/model/profile degrades to accept-all + banner.
SPK_MODEL_PATH    = os.path.expanduser(
    "~/.local/share/rtt/speaker/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"
)
SPK_SAMPLE_RATE   = 16000        # model input rate
SPK_THRESHOLD_DEFAULT = 0.50     # cosine similarity pass mark (tunable via /ownermode/threshold)
SPK_MIN_SECS      = 0.8          # segments shorter than this can't be verified → rejected
SPK_PREROLL_MS    = 500          # matches server VAD prefix_padding_ms so onsets aren't lost
SPK_MAX_SEGMENT_SECS   = 25      # cap capture buffer growth on runaway VAD segments
SPK_SEGMENT_STALE_SECS = 60      # drop unmatched segments older than this
VOICE_PROFILES_FILE = os.path.expanduser("~/.openclaw/workspace/rtt_voice_profiles.json")
VOICE_MODE_FILE     = os.path.expanduser("~/.openclaw/workspace/rtt_voice_mode.json")
# Speech-interrupt: if the mic sees this many consecutive 50ms blocks above
# the interrupt threshold while Zeebot is speaking, kill TTS immediately.
SPEAK_INTERRUPT_PEAK   = 150   # min threshold floor
SPEAK_INTERRUPT_BLOCKS = 3     # × 50 ms = 150 ms sustained speech → interrupt
SPEAK_COUPLING_EMA     = 0.15  # how fast the post-guard echo/coupling estimate tracks change

# Radio mode (AIOC): RX audio measures very quiet on real hardware
# (~-48dBFS idle, confirmed) — Monitor passthrough needs software gain to
# be audible at normal system volume. Tune live if too quiet/loud.
RADIO_MONITOR_GAIN = 8.0

# EchoTest: detect an incoming transmission, record it, replay it back on-air.
# Ported from Pi's "Playback" feature (renamed EchoTest on the UI there too).
ECHOTEST_MIN_SECS    = 0.6   # shorter captures are noise/squelch-flap, not a real transmission — discard
ECHOTEST_MAX_SECS    = 30.0  # cap a single capture so a stuck-open squelch can't grow memory unbounded
ECHOTEST_COOLDOWN_S  = 2.0   # after transmitting a replay on-air, ignore new segments for this long —
                              # own TX bleeding into RX would otherwise re-trigger capture immediately
ECHOTEST_COS_TAIL_S  = 0.5   # squelch/COS hold-open seconds

# DTMF remote control — transmit a digit sequence over the radio to control
# the daemon's sleep/wake/monitor state without touching the dashboard.
# Ported from Pi's always-on _dtmf_listener thread; sequence defaults match
# both Pi's and this repo's own dtmf_monitor.py training/monitor tool so an
# already-trained profile file works identically here.
DTMF_WAKE_SEQ         = "123"   # activate — routes all speech to the agent
DTMF_SLEEP_SEQ        = "321"   # go Silent (still connected, passive 10-min idle disconnect applies)
DTMF_DEEPSLEEP_SEQ    = "987"   # disconnect from OpenAI immediately (skip the 10-min idle wait)
DTMF_WAKE_SILENT_SEQ  = "789"   # reconnect from Deep Sleep into Silent (no routing) — NOT Active
DTMF_MONITOR_ON_SEQ   = "456"   # passive transcription monitoring (capture-only, no TTS routing)
DTMF_MONITOR_OFF_SEQ  = "654"
DTMF_COS_TAIL_S       = 0.5     # squelch/COS hold-open seconds
DTMF_SEQ_TIMEOUT      = 8.0     # reset the digit buffer if this long passes between digits
DTMF_DIGIT_COOLDOWN   = 0.4     # ignore a repeat of the same digit faster than this (DTMF tones sustain)
DTMF_HOLD_TICKS       = 2       # consecutive 25ms-poll agreements needed to accept a digit (was 3/75ms,
                                 # matching dtmf_monitor.py/Pi — confirmed live that requirement was long
                                 # enough for a fast 3-digit sequence's MIDDLE digit to get squeezed out
                                 # between the transitions in and out of it; 2/50ms still debounces real
                                 # noise but gives a sandwiched digit a real chance to register)
DTMF_PROFILE_FILE     = os.path.expanduser("~/.openclaw/workspace/rtt_dtmf_profiles.json")

# Devices that report valid output channels to CoreAudio/sounddevice but
# aren't real, usable speakers — confirmed live: "LG ULTRAWIDE" (an HDMI
# monitor's phantom audio endpoint) produces no audible output at all, on
# this user's hardware, despite passing every channel-count check. Auto
# fallback-selection logic must skip these explicitly, or a disconnect
# elsewhere can silently pick one as the new "working" output device.
INVALID_OUTPUT_DEVICES = {"LG ULTRAWIDE"}

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
_radio_monitor_active: list = [False]  # Radio Monitor (RX loopback) run flag — same pattern as _headset_cal_loop
_radio_monitor_out_dev: list = [None]  # output device index the monitor is currently routed to
_radio_monitor_out_stream: list = [None]  # dedicated sd.OutputStream for the monitor's target device
_radio_monitor_out_last_cb: list = [0.0]  # epoch of last _out_cb invocation — staleness/watchdog check

# Shared radio RX tap. Originally built on the theory that CoreAudio only
# reliably sustains ~2 concurrent InputStreams on the same physical USB
# device (confirmed once: a 3rd stream on the AIOC input froze the other
# two, no exception, "PaMacCore (AUHAL) Error -50" logged once). Consolidating
# Monitor + EchoTest onto one shared tap reduces concurrent AIOC input
# streams from 3 to 2 — but a repeat test at exactly 2 streams (STT capture
# + this tap) froze again the moment a THIRD unrelated stream opened
# elsewhere (Monitor's own OutputStream, on a completely different
# device). So the real trigger is broader: opening *any* new CoreAudio
# stream on this system can transiently freeze other already-open streams,
# not a strict per-device count. The tap watchdog below (_radio_rx_tap_watchdog)
# is the actual fix — self-heals via a heartbeat/reopen cycle, the same
# pattern _watch_mic_stream already uses for the main mic capture,
# regardless of what triggers the freeze.
_radio_rx_tap_stream:     list = [None]
_radio_rx_tap_users:      list = [0]
_radio_rx_tap_samplerate: list = [48000]
_radio_rx_tap_last_cb:    list = [0.0]   # epoch of last _radio_rx_tap_cb invocation

# Single global lock around every native PortAudio stream lifecycle
# operation in the process: constructing/starting/stopping/closing any
# sd.InputStream/OutputStream/Stream, and any sd._terminate()/_initialize()
# reinit. Reentrant because _start/_stop_radio_monitor hold it for their
# whole body while also calling into _radio_rx_tap_acquire/release, which
# take the same lock.
#
# Consolidated from two separate locks (one for reinit, one for the radio
# tap/monitor pair) after repeated live crashes and stream-open failures
# made clear the actual constraint is broader than either scope alone:
# CoreAudio's AUHAL layer does not reliably tolerate two threads touching
# stream lifecycle at the same time, for ANY pair of streams, not just the
# ones involved in whichever crash was being chased at the time (confirmed:
# segfaults from concurrent reinit calls, from concurrent Monitor
# start/stop, and separately a Play Test loop stream-open failure with the
# exact same PaErrorCode -9986/-10851 signature while unrelated to Monitor
# or reinit at all — same root cause, different call sites, one lock).
_audio_open_lock = _threading.RLock()
_radio_rx_tap_lock = _audio_open_lock       # alias — see _audio_open_lock docstring
_portaudio_reinit_lock = _audio_open_lock   # alias — see _audio_open_lock docstring
_speaker_cal_result: dict = {}
_cal_mode_override = [None]
_device_change_msg = [""]
_audio_fingerprint = [""]
_query_devices_fresh_cache: list = [None, 0.0]   # [cached device list, epoch cached] — see _query_devices_fresh
_paused_speech: list = [None]   # (clean_text, alsa_output) saved on TTS interrupt; None otherwise
_post_busy_until:  list = [0.0] # timestamp; mic sends silence until this time after busy clears
_http_interrupt:   list = [False]  # set by /interrupt HTTP route to cut TTS mid-playback
_is_speaking:      list = [False]  # True while speak() is playing audio
_current_think_task: list = [None]  # asyncio.Task for current gw.ask(); cancelled by /interrupt
_last_history_reply: list = [""]    # last text returned by the status-token history fallback
_last_mic_cb:        list = [0.0]   # epoch of last _mic_cb invocation — used for hot-plug detection
_last_interaction:   list = [0.0]   # epoch of last user→LLM interaction; drives auto-sleep
_clear_audio_buffer: list = [False] # set after TTS interrupt; _send_mic sends input_audio_buffer.clear
_persist_monitoring: list = [False] # monitoring state across 60-min OpenAI session reconnects
_persist_multilang:  list = ["off"] # multilang state across reconnects: "off"|"en-zh"|"whitelist"|"any"
_persist_active:     list = [False] # active (voice-routing) state across 60-min OpenAI session reconnects
_sleep_requested:    list = [False] # watchdog sets this; main() waits for /wake before reconnecting
_wake_event:         list = [None]  # asyncio.Event created in main(); HTTP /wake sets it to reconnect
_event_loop:         list = [None]  # asyncio loop from main(); lets background threads (hotplug watcher)
                                     # call loop.call_soon_threadsafe(...) to trigger a wake cross-thread
_is_sleeping:        list = [False] # True while OpenAI is intentionally disconnected (auto-sleep)
_wake_activate:      list = [False] # HTTP /wake while sleeping — next session starts active immediately
_pending_monitor_wake: list = [False]  # Monitor button pressed while sleeping — pre-arms monitoring on wake
_owner_only:          list = [False]  # owner-only mode: gate all voice on the enrolled profile
_spk_threshold:       list = [SPK_THRESHOLD_DEFAULT]  # cosine pass mark
_spk_extractor:       list = [None]   # lazy sherpa_onnx.SpeakerEmbeddingExtractor singleton
_owner_profiles:      dict = {}       # {device_name: {"mean": ndarray, "samples": [ndarray,...], "created": float}}
_enroll_active:       list = [False]  # True while enrollment records; _mic_cb discards audio
_enroll_staging:      dict = {}       # slot -> {"embedding": list, "secs": float, "lang": str} — staged for whatever device is currently active
_enroll_staging_radio: dict = {}      # same shape, independent staging area for the dedicated Radio Voice Profile section
_spk_threshold_cli:   list = [None]   # --spk-threshold override; wins over the mode file

# Radio mode (AIOC) — see radio_interfaces.py for the interface registry.
_radio_profile_active: list = [False]  # Radio Mode toggle — gates PTT/TX routing, radio AGC path
_radio_prev_input_device: list = [None]  # mic device index to restore when Radio Mode turns off
_radio_prev_output_device: list = [None]  # speaker device index to restore when Radio Mode turns off

# Auto-enable Radio Mode on AIOC plug-in. _radio_auto_enable_suppressed is
# set whenever the user manually turns Radio Mode off while the AIOC is
# still connected, so the hotplug watcher doesn't fight a deliberate
# disable — it's cleared again the next time the AIOC transitions from
# unplugged to plugged in, so a fresh plug-in always gets one auto-enable
# attempt regardless of what happened on the previous connection.
_radio_auto_enable_suppressed: list = [False]
_radio_last_seen_connected:    list = [False]

_echotest_active:          list = [False]  # True while the EchoTest listener is attached to the RX tap
_echotest_stop_flag:       list = [False]  # set True to stop the EchoTest listener thread
_echotest_cooldown_until:  list = [0.0]    # ignore captures until this time.time() (post-TX bleed guard)
_echotest_queue = queue.Queue(maxsize=3)   # captured (secs, int16 ndarray) segments awaiting replay
_echotest_squelch: list = [None]   # active radio_interfaces.SquelchTracker, or None when EchoTest is off
_echotest_state: dict = {"was_open": False, "seg": bytearray(),
                          "prev_tx": False, "ext_tx_grace_until": 0.0}

# DTMF remote control — always-on (no dashboard toggle, unlike Monitor/EchoTest),
# runs whenever a radio interface is connected. _dtmf_squelch being non-None is
# what gates _radio_rx_tap_cb into feeding it chunks, mirroring _echotest_squelch's
# role for EchoTest's own attach/detach lifecycle.
_dtmf_squelch:  list = [None]      # active radio_interfaces.SquelchTracker, or None when not attached
_dtmf_cos_open: list = [False]     # live squelch state, updated per-chunk, read by the decode loop
_dtmf_raw_buf: list = []           # [(timestamp, mono int16 ndarray), ...], trimmed to last ~1s
_dtmf_raw_lock = _threading.Lock()
_dtmf_state: dict = {"seq": "", "last_digit": None, "last_time": 0.0}
# Force-flags: written by the DTMF decode loop (a background thread), applied
# to the live session by _send_mic's poll (runs on the asyncio loop, so this
# is the thread-safe hand-off point) — mirrors Pi's _dtmf_force_* mechanism.
_dtmf_force_active:    list = [False]  # DTMF 123 — activate the current silent session immediately
_dtmf_force_silent:    list = [False]  # DTMF 321 — silence the current active session immediately
_dtmf_force_deepsleep: list = [False]  # DTMF 987 — disconnect from OpenAI immediately
_dtmf_force_monitor:   list = [None]   # DTMF 456/654 — True/False to toggle monitoring, None = no-op

_radio_monitor_buf:      list = [np.zeros(0, dtype=np.float32)]  # ring buffer feeding the monitor's OutputStream
_radio_monitor_buf_lock = _threading.Lock()
_active_radio_iface:   list = [None]   # radio_interfaces.RadioInterface currently connected, or None
_ptt_serial:          list = [None]   # open pyserial.Serial on the radio's PTT port, or None
_is_tx:               list = [False]  # True while PTT is asserted — mutes mic capture (_mic_cb)


def _ptt_prekey_s() -> float:
    iface = _active_radio_iface[0]
    return (iface.ptt_prekey_ms if iface else 250) / 1000


def _ptt_tail_s() -> float:
    iface = _active_radio_iface[0]
    return (iface.ptt_tail_ms if iface else 400) / 1000


def _ptt_open() -> None:
    """Open the connected radio interface's serial port for PTT. Non-fatal —
    logs a warning if no registered interface (AIOC, ...) is found."""
    if not _HAVE_PYSERIAL:
        log.warning("Radio PTT unavailable (pyserial not installed) — PTT disabled")
        _ptt_serial[0] = None
        _active_radio_iface[0] = None
        return
    found = _radio.find_radio_port()
    if not found:
        log.warning("Radio PTT unavailable (no known radio interface found) — PTT disabled")
        _ptt_serial[0] = None
        _active_radio_iface[0] = None
        return
    iface, port = found
    try:
        s = _pyserial.Serial(port, timeout=0)
        s.dtr = False   # PTT released at open
        s.rts = False
        _ptt_serial[0] = s
        _active_radio_iface[0] = iface
        log.info("%s PTT ready on %s (%s line) — audio output will transmit over the air",
                 _radio.detect_hw_variant(iface), port, iface.ptt_line.upper())
    except Exception as exc:
        log.warning("%s PTT unavailable (%s) — PTT disabled", iface.name, exc)
        _ptt_serial[0] = None
        _active_radio_iface[0] = None


def _ptt_alive() -> bool:
    """True if the PTT serial port is open and its radio interface is still
    the one physically connected (VID:PID still present)."""
    if _ptt_serial[0] is None or _active_radio_iface[0] is None:
        return False
    found = _radio.find_radio_port()
    return bool(found and found[0].name == _active_radio_iface[0].name)


def _radio_hotplug_watcher(session_ref: list) -> None:
    """Background thread: periodically (re)opens the PTT serial port so
    plugging in a radio interface is picked up without anyone needing to
    trigger a speak() first. Without this, _ptt_serial[0] stays None from
    daemon startup until the first _ptt_key() call, and that first call is a
    silent no-op (its reopen-on-failure path only fires on an assert
    *exception*, which can't happen when there was never a port open to
    assert against) — so the very first TX after startup would key nothing.

    Also auto-enables Radio Mode itself on a fresh plug-in (audio device
    newly visible, not just the serial/PTT port) — mirrors the /radio-mode
    HTTP handler's "turn on" logic. Skips this if the user manually turned
    Radio Mode off during the current connection (_radio_auto_enable_suppressed),
    so a deliberate disable sticks until the next unplug/replug cycle.

    The serial/PTT port is always detected live (pyserial re-lists comports
    fresh every call, no caching) — but sounddevice/PortAudio's device list
    is cached per-process and does NOT pick up a hot-plugged USB audio
    device on its own (confirmed live: a fresh subprocess sees the AIOC's
    audio device immediately after plug-in, but this same long-running
    process's sd.query_devices() kept reporting it absent until something
    reinitializes PortAudio — the exact reason /device-status re-enumerates
    via a subprocess instead of calling sd.query_devices() directly here).
    So on a fresh serial detection, this also checks whether the cached
    audio device list already agrees; if not, it forces one reinit to
    resync it. Skipped when the device was already visible before this
    process started (no staleness to fix), and skipped after that one-time
    resync per plug-in — not a fixed interval — to minimize disruption
    to already-open streams (the mic stream / RX tap watchdogs self-heal
    from the resulting brief drop either way)."""
    import time as _hpw_time
    # Give the main RealtimeSession time to finish opening its own initial
    # mic stream before this thread's auto-enable logic can touch input
    # device switching at all. Confirmed live: a crash at startup with the
    # AIOC already connected landed right as "Session active" appeared —
    # this thread's auto-enable-triggered _switch_mic_stream call was racing
    # the session's own first-time mic stream setup, a narrower and
    # different race than the Monitor/tap ones already fixed, specific to
    # the first few seconds after the daemon (re)starts.
    _hpw_time.sleep(8.0)
    while True:
        try:
            was_alive = _ptt_alive()
            if not was_alive:
                _ptt_open()
            now_alive = _ptt_alive()

            if now_alive and not _radio_last_seen_connected[0]:
                _audio_found = _radio.find_radio_audio_devices()
                if not _audio_found or _audio_found[1] is None:
                    log.info("Radio hotplug: AIOC serial detected but cached audio "
                             "device list is stale — reinitializing PortAudio")
                    try:
                        with _portaudio_reinit_lock:
                            sd._terminate()
                            sd._initialize()
                            _resync_output_device_after_reinit()
                    except Exception as exc:
                        log.warning("Radio hotplug: PortAudio reinit failed: %s", exc)
                # Let CoreAudio settle before anything else opens a stream on
                # the AIOC, reinit or not — a plug/unplug/replug cycle itself
                # triggers a HAL reconfiguration independent of whether
                # PortAudio's own device cache needed refreshing. Confirmed
                # live this race (-9986/-10851) is somewhat non-deterministic
                # in timing — even 4s didn't guarantee a clean first try on
                # every cycle. This delay is a best-effort reduction, not the
                # actual safety net: _switch_mic_stream's own one retry, and
                # _watch_mic_stream's fallback recovery a few seconds later,
                # reliably converge on the right device either way.
                _hpw_time.sleep(2.0)
                _radio_auto_enable_suppressed[0] = False

            if now_alive:
                _found = _radio.find_radio_audio_devices()
                if (_found and _found[1] is not None
                        and not _radio_profile_active[0]
                        and not _radio_auto_enable_suppressed[0]):
                    _radio_prev_input_device[0] = _selected_input_device[0]
                    _radio_profile_active[0] = True
                    _selected_input_device[0] = _found[1]   # so a reconnect picks this up even if
                                                             # no live session exists to hot-swap onto
                    # Also switch the dashboard's "Active" speaker to the AIOC's
                    # output, mirroring Pi's `pactl set-default-sink <radio_sink>`
                    # — the actual TX routing in speak() already resolves the
                    # radio output fresh on its own regardless of this, but
                    # leaving _selected_output_device pointed at the local
                    # speaker while Radio Mode is on made the dashboard show
                    # "Mac mini Speakers" as active/Running even though nothing
                    # was actually routed there — confirmed live, reads as a bug.
                    if _found[2] is not None:
                        _radio_prev_output_device[0] = _selected_output_device[0]
                        _selected_output_device[0] = _found[2]
                    sess = session_ref[0]
                    if sess is not None:
                        _threading.Thread(target=_switch_mic_stream, args=(sess, _found[1]),
                                           daemon=True).start()
                    elif _is_sleeping[0] and _wake_event[0] and _event_loop[0]:
                        # No live session (daemon asleep) — confirmed live this
                        # silently no-ops otherwise: the flag above flips to
                        # "Radio Mode on" and this log line fires, but nothing
                        # is actually listening on the AIOC until some future,
                        # unrelated wake. Reconnect so transcription comes back
                        # up (NOT _wake_activate — a hardware change should
                        # restore STT/Silent listening, not jump straight to
                        # Active; a genuine wake phrase afterward activates it
                        # the normal way).
                        _last_interaction[0] = _hpw_time.time()
                        _event_loop[0].call_soon_threadsafe(_wake_event[0].set)
                        log.info("Radio hotplug: daemon was asleep — reconnecting to apply AIOC input switch")
                    log.info("Radio Mode auto-enabled on AIOC plug-in — switching input to AIOC audio-in (#%d), "
                             "output to AIOC audio-out (#%s)", _found[1], _found[2])

            if not now_alive and _radio_last_seen_connected[0]:
                # Unplugged. Stop Monitor/EchoTest unconditionally (both can
                # be running independent of Radio Mode's own flag — Monitor's
                # button isn't gated on it at all) before anything else has a
                # chance to touch their now-invalid streams. Confirmed live:
                # leaving them attached to a vanished device segfaulted the
                # process a couple minutes later ("Python exited 11") with no
                # reinit-race context this time — _radio_rx_tap_watchdog's own
                # stop()/close() on the dead stream is the leading suspect,
                # since PortAudio/CoreAudio native calls aren't guaranteed
                # safe against hardware that's already gone, and a segfault
                # bypasses Python's try/except entirely.
                if _echotest_active[0]:
                    _echotest_stop_flag[0] = True
                    _echotest_active[0] = False
                    log.info("EchoTest stopped — AIOC unplugged")
                if _radio_monitor_active[0]:
                    _stop_radio_monitor()
                    log.info("Radio monitor stopped — AIOC unplugged")

                if _radio_profile_active[0]:
                    # Turn Radio Mode off automatically rather than leaving it
                    # pointed at a device that's gone (mirrors the /radio-mode
                    # HTTP handler's manual "turn off" logic). Does NOT set
                    # _radio_auto_enable_suppressed — an unplug isn't a
                    # deliberate user disable, so the next plug-in should
                    # still auto-enable freely.
                    _radio_profile_active[0] = False
                    prev = _radio_prev_input_device[0]
                    _radio_prev_input_device[0] = None
                    prev_out = _radio_prev_output_device[0]
                    _radio_prev_output_device[0] = None
                    if prev_out is not None:
                        _selected_output_device[0] = prev_out
                    sess = session_ref[0]
                    if sess is not None and prev is not None:
                        _threading.Thread(target=_switch_mic_stream, args=(sess, prev),
                                           daemon=True).start()
                    log.info("Radio Mode auto-disabled on AIOC unplug — restoring input device %s, "
                             "output device %s", prev, prev_out)

            _radio_last_seen_connected[0] = now_alive
        except Exception as exc:
            log.warning("Radio hotplug watcher error: %s", exc)
        _hpw_time.sleep(3)


def _ptt_key() -> None:
    """Assert PTT via the active radio interface's serial line (DTR or RTS,
    per its RadioInterface.ptt_line). Sets _is_tx so mic capture (_mic_cb)
    and transcription are suppressed while transmitting."""
    s = _ptt_serial[0]
    iface = _active_radio_iface[0]
    line = iface.ptt_line if iface else "dtr"
    def _assert(sr):
        if line == "rts":
            sr.rts = True; sr.dtr = False
        else:
            sr.dtr = True; sr.rts = False
    if s:
        try:
            _assert(s)
        except Exception as exc:
            log.warning("PTT key failed: %s — reopening port", exc)
            try: s.close()
            except Exception: pass
            _ptt_serial[0] = None
            _ptt_open()   # reopen on new port path (macOS reassigns /dev/cu.usbmodem* on reconnect)
            s2 = _ptt_serial[0]
            if s2:
                try: _assert(s2)
                except Exception as exc2: log.warning("PTT key retry failed: %s", exc2)
    _is_tx[0] = True


def _ptt_release() -> None:
    """Release PTT via the active radio interface's serial line."""
    s = _ptt_serial[0]
    iface = _active_radio_iface[0]
    line = iface.ptt_line if iface else "dtr"
    if s:
        try:
            if line == "rts":
                s.rts = False
            else:
                s.dtr = False
        except Exception as exc:
            log.warning("PTT release failed: %s", exc)
    _is_tx[0] = False


def _radio_rx_tap_cb(indata, frames, time_info, status) -> None:
    """Shared AIOC RX callback — see _radio_rx_tap_stream's docstring at its
    declaration for why Monitor and EchoTest both run out of this single
    stream instead of each opening their own."""
    import time as _rxtap_time
    raw = indata[:, 0]
    now = _rxtap_time.time()
    _radio_rx_tap_last_cb[0] = now
    if _radio_monitor_active[0]:
        chunk = np.clip(raw.astype(np.float32) / 32768.0 * RADIO_MONITOR_GAIN, -1.0, 1.0)
        with _radio_monitor_buf_lock:
            buf = np.concatenate([_radio_monitor_buf[0], chunk])
            cap = _radio_rx_tap_samplerate[0] * 2   # 2s cap — output side should keep this near-empty
            if len(buf) > cap:
                buf = buf[-_radio_rx_tap_samplerate[0]:]
            _radio_monitor_buf[0] = buf
    if _echotest_active[0] and _echotest_squelch[0] is not None:
        _echotest_process_chunk(raw, now)
    if _dtmf_squelch[0] is not None:
        _dtmf_process_chunk(raw, now)


def _dtmf_process_chunk(raw: np.ndarray, now: float) -> None:
    """Cheap per-chunk hook called from the shared RX tap's callback — only
    updates squelch state and appends to a short rolling buffer. The actual
    Goertzel decode is deliberately NOT done here: it's a Python-level loop
    over each candidate digit's row+col frequencies (up to ~28 Goertzel
    passes per decode), too slow to risk running inside a real-time
    PortAudio callback shared with Monitor's live audio — see
    _dtmf_decode_loop, which polls this buffer from its own thread instead,
    mirroring dtmf_monitor.py's own capture/decode thread split."""
    peak = int(np.max(np.abs(raw))) if len(raw) else 0
    _dtmf_cos_open[0] = _dtmf_squelch[0].update(peak, now)
    with _dtmf_raw_lock:
        _dtmf_raw_buf.append((now, raw.copy()))
        cutoff = now - 1.0
        while _dtmf_raw_buf and _dtmf_raw_buf[0][0] < cutoff:
            _dtmf_raw_buf.pop(0)


def _radio_rx_tap_acquire() -> bool:
    """Start the shared AIOC RX tap stream if not already running; increment
    its refcount either way. Returns False if no AIOC input is found."""
    import time as _rxtap_time0
    with _radio_rx_tap_lock:
        if _radio_rx_tap_stream[0] is not None:
            _radio_rx_tap_users[0] += 1
            return True
        found = _radio.find_radio_audio_devices()
        if not found or found[1] is None:
            return False
        _, in_idx, _ = found
        # One reinit-and-retry on failure — observed live that stream opens
        # can start failing (PaErrorCode -9986) after many hours of daemon
        # uptime even with no other thread touching PortAudio at that
        # moment (a fresh external process opens the same device fine),
        # consistent with some native-side resource gradually exhausting
        # over a long run of repeated open/close cycles rather than a live
        # concurrency race. A full terminate/initialize is the same
        # recovery _watch_mic_stream's hot-plug path already uses; trying
        # it here means Monitor can self-heal instead of needing a manual
        # daemon restart every time this shows up.
        for _attempt in (1, 2):
            try:
                samplerate = int(sd.query_devices(in_idx)["default_samplerate"])
                stream = sd.InputStream(device=in_idx, channels=1, samplerate=samplerate,
                                         dtype="int16", blocksize=samplerate * 50 // 1000,
                                         callback=_radio_rx_tap_cb)
                stream.start()
                _radio_rx_tap_stream[0] = stream
                _radio_rx_tap_users[0] = 1
                _radio_rx_tap_samplerate[0] = samplerate
                _radio_rx_tap_last_cb[0] = _rxtap_time0.time()
                log.info("Radio RX tap opened: AIOC in (#%d)%s", in_idx,
                         " (after reinit)" if _attempt == 2 else "")
                return True
            except Exception as exc:
                if _attempt == 1:
                    log.warning("Radio RX tap failed to open (%s) — reinitializing PortAudio and retrying", exc)
                    try:
                        sd._terminate()
                        sd._initialize()
                    except Exception as reinit_exc:
                        log.warning("Radio RX tap reinit failed: %s", reinit_exc)
                    _refound = _radio.find_radio_audio_devices()
                    if not _refound or _refound[1] is None:
                        log.warning("Radio RX tap: AIOC no longer found after reinit")
                        return False
                    in_idx = _refound[1]
                    continue
                log.warning("Radio RX tap failed to open: %s", exc)
                return False
        return False


def _radio_rx_tap_release() -> None:
    """Decrement the refcount; close the shared tap stream once nobody's using it."""
    with _radio_rx_tap_lock:
        _radio_rx_tap_users[0] = max(0, _radio_rx_tap_users[0] - 1)
        if _radio_rx_tap_users[0] == 0 and _radio_rx_tap_stream[0] is not None:
            s = _radio_rx_tap_stream[0]
            _radio_rx_tap_stream[0] = None
            try:
                s.stop(); s.close()
            except Exception:
                pass
            log.info("Radio RX tap closed (no more users)")


def _start_radio_monitor(out_dev=None) -> bool:
    """Start a live RX passthrough (radio audio in -> speaker out): attaches
    to the shared AIOC RX tap for input and opens a dedicated OutputStream
    for whichever device was picked. Returns False if no radio audio input
    is found or the output stream fails to open.

    Holds _radio_rx_tap_lock for its whole body (reentrant — it also calls
    into _radio_rx_tap_acquire, which takes the same lock) so this can never
    interleave with the RX tap watchdog's own stream manipulation. Confirmed
    live: an HTTP-triggered stop racing the watchdog's periodic staleness
    check on the *same* Monitor output stream — both unguarded — segfaulted
    the process with no missing-device involved at all."""
    with _radio_rx_tap_lock:
        target_out = out_dev if out_dev is not None else _selected_output_device[0]
        if target_out is None:
            try:
                target_out = sd.query_devices(kind="output")["index"]
            except Exception:
                pass

        if not _radio_rx_tap_acquire():
            log.warning("Radio monitor: no radio audio input found")
            return False

        with _radio_monitor_buf_lock:
            _radio_monitor_buf[0] = np.zeros(0, dtype=np.float32)

        def _out_cb(outdata, frames, time_info, status):
            import time as _outcb_time
            _radio_monitor_out_last_cb[0] = _outcb_time.time()
            with _radio_monitor_buf_lock:
                buf = _radio_monitor_buf[0]
                n = len(buf)
                if n >= frames:
                    outdata[:, 0] = buf[:frames]
                    _radio_monitor_buf[0] = buf[frames:]
                else:
                    outdata[:n, 0] = buf
                    outdata[n:, 0] = 0.0
                    _radio_monitor_buf[0] = np.zeros(0, dtype=np.float32)

        try:
            out_stream = sd.OutputStream(device=target_out, samplerate=_radio_rx_tap_samplerate[0],
                                          channels=1, dtype="float32", blocksize=480, callback=_out_cb)
            out_stream.start()
        except Exception as exc:
            log.warning("Radio monitor output stream error: %s", exc)
            _radio_rx_tap_release()
            return False

        import time as _srm_time
        _radio_monitor_out_stream[0] = out_stream
        _radio_monitor_out_last_cb[0] = _srm_time.time()
        _radio_monitor_active[0] = True
        _radio_monitor_out_dev[0] = target_out
        log.info("Radio monitor started: AIOC in -> %s", _device_label(target_out))
        return True


def _stop_radio_monitor() -> None:
    with _radio_rx_tap_lock:
        _radio_monitor_active[0] = False
        s = _radio_monitor_out_stream[0]
        _radio_monitor_out_stream[0] = None
        if s is not None:
            try:
                s.stop(); s.close()
            except Exception:
                pass
        _radio_monitor_out_dev[0] = None
        _radio_rx_tap_release()


def _radio_rx_tap_watchdog() -> None:
    """Background thread: detects a stalled shared RX tap or Monitor output
    stream (no callbacks for several seconds while it should be running)
    and reopens it. Confirmed live that opening any new CoreAudio stream on
    this system can transiently freeze other already-open streams with no
    exception raised — same class of problem _watch_mic_stream already
    handles for the main mic capture, applied here to the tap/Monitor pair
    since they're independent streams that watchdog doesn't know about."""
    import time as _wd_time
    while True:
        _wd_time.sleep(2.0)

        with _radio_rx_tap_lock:
            tap_stream = _radio_rx_tap_stream[0]
            tap_users  = _radio_rx_tap_users[0]
        if tap_stream is not None and tap_users > 0:
            stale = _wd_time.time() - _radio_rx_tap_last_cb[0]
            if stale > 3.0:
                # Tried skipping .stop()/.close() entirely once the device
                # looked gone, on the theory that calling them on a stream
                # whose USB device physically disappeared risked a segfault
                # (one was observed once, in this exact vicinity). That
                # traded a rare, self-recovering crash for a guaranteed,
                # non-recovering resource leak instead — confirmed live:
                # skipping close() left the native stream handle open,
                # permanently blocking every later attempt to open a new
                # stream on the same device with the same PaErrorCode -9986
                # this was meant to avoid, but with no restart to clear it.
                # Always attempt the close; try/except covers the ordinary
                # failure modes, and an occasional crash that launchd
                # restarts is the better failure mode of the two.
                found = _radio.find_radio_audio_devices()
                if not found or found[1] is None:
                    log.warning("Radio RX tap stalled (%.1fs, device gone) — stopping Monitor/EchoTest",
                                stale)
                    with _radio_rx_tap_lock:
                        old = _radio_rx_tap_stream[0]
                        _radio_rx_tap_stream[0] = None
                        _radio_rx_tap_users[0] = 0
                        if old is not None:
                            try: old.stop(); old.close()
                            except Exception: pass
                    if _echotest_active[0]:
                        _echotest_stop_flag[0] = True
                        _echotest_active[0] = False
                    if _radio_monitor_active[0]:
                        _radio_monitor_active[0] = False
                        _radio_monitor_out_stream[0] = None
                        _radio_monitor_out_dev[0] = None
                else:
                    log.warning("Radio RX tap stalled (%.1fs no callbacks) — reopening", stale)
                    with _radio_rx_tap_lock:
                        old = _radio_rx_tap_stream[0]
                        _radio_rx_tap_stream[0] = None
                        if old is not None:
                            try: old.stop(); old.close()
                            except Exception: pass
                        _, in_idx, _ = found
                        try:
                            samplerate = int(sd.query_devices(in_idx)["default_samplerate"])
                            new_stream = sd.InputStream(
                                device=in_idx, channels=1, samplerate=samplerate, dtype="int16",
                                blocksize=samplerate * 50 // 1000, callback=_radio_rx_tap_cb)
                            new_stream.start()
                            _radio_rx_tap_stream[0] = new_stream
                            _radio_rx_tap_samplerate[0] = samplerate
                            _radio_rx_tap_last_cb[0] = _wd_time.time()
                            log.info("Radio RX tap reopened successfully")
                        except Exception as exc:
                            log.warning("Radio RX tap reopen failed: %s", exc)

        if _radio_monitor_active[0]:
            stale = _wd_time.time() - _radio_monitor_out_last_cb[0]
            if stale > 3.0:
                target = _radio_monitor_out_dev[0]
                # Same reasoning as the tap check above: confirm the target
                # output device is still actually present before touching
                # its stream's native stop()/close() — a vanished device
                # (this output disconnected too, e.g. Bluetooth) makes that
                # call a plausible crash rather than just a no-op.
                _target_present = False
                try:
                    _target_present = any(
                        (d.get("name") == target or i == target) and d.get("max_output_channels", 0) > 0
                        for i, d in enumerate(sd.query_devices()))
                except Exception:
                    pass
                if not _target_present:
                    # Same reasoning as the tap branch above: always attempt
                    # the close rather than leaking the stream handle — a
                    # leaked handle permanently blocks reopening even after
                    # the device comes back, which is worse than an
                    # occasional crash that launchd auto-restarts. Locked so
                    # this can't interleave with an HTTP-triggered start/stop
                    # touching the same stream object concurrently.
                    log.warning("Radio monitor output stalled (%.1fs, device gone) — stopping", stale)
                    with _radio_rx_tap_lock:
                        _radio_monitor_active[0] = False
                        _mon_old = _radio_monitor_out_stream[0]
                        _radio_monitor_out_stream[0] = None
                        _radio_monitor_out_dev[0] = None
                        if _mon_old is not None:
                            try: _mon_old.stop(); _mon_old.close()
                            except Exception: pass
                        _radio_rx_tap_release()
                else:
                    log.warning("Radio monitor output stalled (%.1fs no callbacks) — reopening on %s",
                                stale, _device_label(target))
                    with _radio_rx_tap_lock:
                        _stop_radio_monitor()
                        _start_radio_monitor(out_dev=target)


def _echotest_process_chunk(raw: np.ndarray, now: float) -> None:
    """Called from the shared RX tap's callback (_radio_rx_tap_cb) once per
    ~50ms chunk while EchoTest is active. Same squelch/capture state machine
    Pi's _playback_listener used, just driven inline from the shared tap
    instead of from its own InputStream — see _radio_rx_tap_stream's
    docstring for why. Ignores audio both during any active PTT-keyed
    transmission (_is_tx) and for a cooldown window after one ends: without
    that grace window, TX-into-RX crosstalk on this hardware makes the
    listener capture its own transmission's bleed/tail as a "new" segment
    the instant PTT releases — a self-sustaining echo chamber."""
    st = _echotest_state
    squelch = _echotest_squelch[0]
    if _is_tx[0]:
        st["prev_tx"] = True
    elif st["prev_tx"]:
        st["prev_tx"] = False
        st["ext_tx_grace_until"] = now + ECHOTEST_COOLDOWN_S
    if _is_tx[0] or now < _echotest_cooldown_until[0] or now < st["ext_tx_grace_until"]:
        st["was_open"] = False
        st["seg"] = bytearray()
        return
    peak = int(np.max(np.abs(raw))) if len(raw) else 0
    cos_open = squelch.update(peak, now)
    chunk_bytes = raw.tobytes()

    if cos_open and not st["was_open"]:
        st["seg"] = bytearray(chunk_bytes)
    elif cos_open and st["was_open"]:
        max_bytes = int(ECHOTEST_MAX_SECS * _radio_rx_tap_samplerate[0] * 2)
        if len(st["seg"]) < max_bytes:
            st["seg"].extend(chunk_bytes)
    elif not cos_open and st["was_open"]:
        secs = len(st["seg"]) / 2 / _radio_rx_tap_samplerate[0]
        if secs >= ECHOTEST_MIN_SECS:
            try:
                _echotest_queue.put_nowait(
                    (secs, np.frombuffer(bytes(st["seg"]), dtype=np.int16)))
            except queue.Full:
                log.warning("EchoTest: queue full — dropping %.1fs capture", secs)
        st["seg"] = bytearray()
    st["was_open"] = cos_open


def _echotest_worker() -> None:
    """Single always-on daemon thread: serially transmits captured segments
    from _echotest_queue back out over the radio — keys PTT, plays the clip
    to the AIOC's audio-out, releases PTT once playback finishes. If PTT or
    Radio Mode isn't available, the segment is just dropped (never falls
    back to local playback). Sets a cooldown after each transmission so
    _echotest_listener doesn't immediately re-capture our own tail/echo as a
    new transmission. Runs for the life of the process; harmless and idle
    when the queue is empty. Ported from Pi's _playback_worker — sd.play()
    replaces the paplay subprocess."""
    import time as _et_time
    while True:
        secs, pcm_i16 = _echotest_queue.get()
        if not (_ptt_alive() and _radio_profile_active[0]):
            log.warning("EchoTest: captured %.1fs but Radio Mode is off — dropped "
                        "(listener runs regardless of profile, but transmitting "
                        "requires Radio Mode to be explicitly on)", secs)
            continue
        _found = _radio.find_radio_audio_devices()
        if not _found or _found[2] is None:
            log.warning("EchoTest: captured %.1fs but no radio audio-out found — dropped", secs)
            continue
        out_dev = _found[2]
        try:
            _ptt_key()
            _et_time.sleep(_ptt_prekey_s())
            log.info("EchoTest: PTT keyed — transmitting %.1fs on-air", secs)
            pcm_f32 = pcm_i16.astype(np.float32) / 32768.0
            sd.play(pcm_f32, samplerate=48000, device=out_dev, blocking=True)
            _et_time.sleep(_ptt_tail_s())
            _ptt_release()
            log.info("EchoTest: PTT released")
            _log_entry("system", f"EchoTest: transmitted {secs:.1f}s radio recording on-air")
        except Exception as exc:
            log.warning("EchoTest: on-air transmit failed: %s", exc)
            try: _ptt_release()
            except Exception: pass
        finally:
            _echotest_cooldown_until[0] = _et_time.time() + ECHOTEST_COOLDOWN_S


def _echotest_listener(stop_flag: list) -> None:
    """Controller thread: sets up the squelch tracker/state and attaches to
    the shared AIOC RX tap (see _radio_rx_tap_stream's docstring). The
    actual per-chunk squelch/capture logic runs inline inside the tap's
    callback (_echotest_process_chunk) — this thread just manages lifecycle
    and re-resolves/reattaches if the radio interface drops and comes back."""
    import time as _el_time

    while not stop_flag[0]:
        found = _radio.find_radio_audio_devices()
        if not found or found[1] is None:
            _el_time.sleep(3); continue
        iface = found[0]
        _echotest_squelch[0] = _radio.SquelchTracker(iface.cos_threshold, ECHOTEST_COS_TAIL_S)
        _echotest_state["was_open"] = False
        _echotest_state["seg"] = bytearray()
        _echotest_state["prev_tx"] = False
        _echotest_state["ext_tx_grace_until"] = 0.0

        if not _radio_rx_tap_acquire():
            log.warning("EchoTest: could not attach to radio RX tap")
            _echotest_squelch[0] = None
            _el_time.sleep(3); continue

        log.info("EchoTest listener ready via %s (COS>=%d, tail=%.1fs, min=%.1fs)",
                 iface.name, _echotest_squelch[0].base_threshold,
                 ECHOTEST_COS_TAIL_S, ECHOTEST_MIN_SECS)
        try:
            while not stop_flag[0] and _radio.find_radio_audio_devices():
                _el_time.sleep(0.5)
        finally:
            _radio_rx_tap_release()
            _echotest_squelch[0] = None
        if not stop_flag[0]:
            _el_time.sleep(3)


# ── DTMF remote control ──────────────────────────────────────────────────────
# Ported from Pi's always-on _dtmf_listener thread. Decodes via the same
# learned-profile Goertzel approach as this repo's own dtmf_monitor.py
# training/monitor tool (NOT Pi's 8kHz-decimated version — dtmf_monitor.py
# trains and decodes at the AIOC's native 48kHz with no decimation step, so
# using Pi's decimated math here would silently mismatch profiles already
# trained via `python3 dtmf_monitor.py --train` on this machine).

def _load_dtmf_profiles() -> dict:
    try:
        with open(DTMF_PROFILE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _dtmf_goertzel_energy(samples, freq: float, rate: int) -> float:
    n = len(samples)
    k = int(0.5 + n * freq / rate)
    w = 2 * np.pi * k / n
    c = 2 * np.cos(w)
    q1 = q2 = 0.0
    for s in samples:
        q0 = s + c * q1 - q2; q2 = q1; q1 = q0
    return q2 * q2 + q1 * q1 - c * q1 * q2


def _dtmf_decode_with_profiles(frame: np.ndarray, profiles: dict, rate: int) -> str | None:
    """Decode a DTMF digit using learned frequency profiles — verbatim port
    of dtmf_monitor.py's decode_with_profiles (same thresholds), so a
    profile file trained via that tool decodes identically here."""
    samples = frame.astype(np.float64).tolist()
    scores = {}
    for digit, prof in profiles.items():
        row_e = _dtmf_goertzel_energy(samples, prof["row_hz"], rate)
        col_e = _dtmf_goertzel_energy(samples, prof["col_hz"], rate)
        scores[digit] = row_e + col_e
    if not scores:
        return None
    best = max(scores, key=scores.get)
    best_e = scores[best]
    median_e = sorted(scores.values())[len(scores) // 2]
    if best_e < 1e6 or (median_e > 0 and best_e / median_e < 3.0):
        return None
    return best


def _dtmf_handle_digit(digit: str, now: float, seq: str) -> str:
    """Match `seq` against the configured DTMF command sequences and
    dispatch the corresponding action. Ported from Pi's _dtmf_listener's
    _handle_digit, adapted to this daemon's sleep/wake globals: Pi tracks
    sleep with a single _idle_disconnected flag and closes the WebSocket
    via loop.call_soon_threadsafe from a background thread; this daemon
    splits that into _sleep_requested/_is_sleeping (checked at the top of
    main()'s reconnect loop) and hands the actual ws.close() off to
    _send_mic's poll, which already runs on the event loop. Returns the
    (possibly-cleared) sequence buffer."""
    st = _dtmf_state
    if seq and now - st["last_time"] > DTMF_SEQ_TIMEOUT:
        seq = ""
    if digit == st["last_digit"] and now - st["last_time"] < DTMF_DIGIT_COOLDOWN:
        return seq
    st["last_digit"] = digit
    st["last_time"] = now
    if not seq or seq[-1] != digit:
        seq += digit
        log.info("DTMF digit: %s -> seq=%s", digit, seq)
    max_len = max(len(DTMF_WAKE_SEQ), len(DTMF_SLEEP_SEQ), len(DTMF_DEEPSLEEP_SEQ),
                  len(DTMF_MONITOR_ON_SEQ), len(DTMF_MONITOR_OFF_SEQ), len(DTMF_WAKE_SILENT_SEQ))
    if len(seq) > max_len:
        seq = seq[-max_len:]

    if DTMF_WAKE_SEQ in seq:
        seq = ""
        log.info("DTMF wake %r received", DTMF_WAKE_SEQ)
        _log_entry("system", f"DTMF {DTMF_WAKE_SEQ} — waking {AGENT_NAME}")
        _persist_monitoring[0] = False   # Active supersedes Monitoring — don't leave it stuck on
        if _is_sleeping[0] and _wake_event[0] and _event_loop[0]:
            _last_interaction[0] = now; _wake_activate[0] = True
            _persist_active[0] = True; _save_sleep_state(False)
            _event_loop[0].call_soon_threadsafe(_wake_event[0].set)
        elif _wake_event[0]:
            _persist_active[0] = True; _wake_activate[0] = True
            _dtmf_force_active[0] = True   # applied to the live session by _send_mic's poll
    elif DTMF_SLEEP_SEQ in seq:
        seq = ""
        log.info("DTMF sleep %r received", DTMF_SLEEP_SEQ)
        _log_entry("system", f"DTMF {DTMF_SLEEP_SEQ} — {AGENT_NAME} silent")
        _persist_active[0] = False
        _dtmf_force_silent[0] = True
    elif DTMF_DEEPSLEEP_SEQ in seq:
        seq = ""
        log.info("DTMF deep-sleep %r received", DTMF_DEEPSLEEP_SEQ)
        _log_entry("system", f"DTMF {DTMF_DEEPSLEEP_SEQ} — {AGENT_NAME} sleeping (disconnecting)")
        _persist_active[0] = False
        _wake_activate[0] = False        # discard any stale activation intent
        _persist_monitoring[0] = False   # clear monitoring when going to deep sleep
        _dtmf_force_deepsleep[0] = True
    elif DTMF_MONITOR_ON_SEQ in seq:
        seq = ""
        log.info("DTMF monitor-on %r received", DTMF_MONITOR_ON_SEQ)
        _log_entry("system", f"DTMF {DTMF_MONITOR_ON_SEQ} — monitoring on")
        _persist_monitoring[0] = True
        _persist_active[0] = False       # monitoring is passive, not active
        if _is_sleeping[0] and _wake_event[0] and _event_loop[0]:
            # Pre-arm monitoring for the next session (same mechanism the
            # /monitor/start HTTP handler uses while sleeping) rather than
            # _wake_activate, so the reconnected session starts silent+
            # monitoring, not fully Active.
            _pending_monitor_wake[0] = True
            _last_interaction[0] = now
            _event_loop[0].call_soon_threadsafe(_wake_event[0].set)
        else:
            _dtmf_force_monitor[0] = True
    elif DTMF_MONITOR_OFF_SEQ in seq:
        seq = ""
        log.info("DTMF monitor-off %r received", DTMF_MONITOR_OFF_SEQ)
        _log_entry("system", f"DTMF {DTMF_MONITOR_OFF_SEQ} — monitoring off")
        _persist_monitoring[0] = False
        _dtmf_force_monitor[0] = False
    elif DTMF_WAKE_SILENT_SEQ in seq:
        seq = ""
        log.info("DTMF wake-silent %r received", DTMF_WAKE_SILENT_SEQ)
        _log_entry("system", f"DTMF {DTMF_WAKE_SILENT_SEQ} — waking to silent")
        _persist_active[0] = False       # silent, not active
        _wake_activate[0] = False        # override any stale activation intent
        _persist_monitoring[0] = False   # not monitoring
        if _is_sleeping[0] and _wake_event[0] and _event_loop[0]:
            _last_interaction[0] = now
            _save_sleep_state(False)
            _event_loop[0].call_soon_threadsafe(_wake_event[0].set)
        # If already awake (silent/monitoring), nothing extra needed.
    return seq


def _dtmf_decode_loop(profiles: dict) -> None:
    """Polls the rolling buffer _dtmf_process_chunk fills, decoupled from
    the real-time audio callback — mirrors dtmf_monitor.py's dtmf_thread
    (same 25ms poll cadence, same 100ms analysis window), except dispatching
    real actions via _dtmf_handle_digit instead of printing to a terminal,
    decimating before Goertzel (dtmf_monitor.py doesn't need to), and using
    a shorter hold-to-confirm debounce (DTMF_HOLD_TICKS, see its comment).

    Decimating to ~8kHz before Goertzel is Pi's own mitigation for exactly
    this situation, ported here after confirming live that it's needed:
    dtmf_monitor.py (a standalone process with only 3 threads total) caught
    every digit in a fast sequence, but this listener — one of many threads
    inside the full daemon, competing for the GIL with the asyncio loop,
    WebRTC AGC, HTTP server, other radio threads, etc. — dropped digits
    under the same fast keying. The Goertzel recurrence is a raw Python
    loop (can't vectorize — each step depends on the previous), run twice
    per candidate digit for up to 12 trained digits: at native 48kHz
    (~4800 samples/window) that's ~115,000 loop iterations per decode
    attempt, up to 40 times/second while squelch is open — enough to fall
    behind under real daemon contention. Frequency resolution is set by
    the window's DURATION (100ms), not its sample rate, so decimating
    doesn't lose discriminating power between DTMF tones ~140Hz+ apart —
    it only cuts the Python-level cost roughly 6x. Profiles stay trained
    at native rate (dtmf_monitor.py --train doesn't change); only the
    decode-time rate passed to Goertzel changes to match the decimated
    samples, which is all that's needed for the math to stay correct."""
    import time as _ddl_time
    prev_digit = [None]; hold = [0]; seq = [""]
    native_rate = _radio_rx_tap_samplerate[0]
    decim       = max(1, round(native_rate / 8000))
    decode_rate = native_rate // decim
    FRAME       = decode_rate // 10   # 100ms analysis window
    log.info("DTMF decode: native=%dHz -> decimated=%dHz (/%d), frame=%d samples",
             native_rate, decode_rate, decim, FRAME)
    while True:
        _ddl_time.sleep(0.025)
        if not _dtmf_cos_open[0]:
            prev_digit[0] = None; hold[0] = 0; continue
        with _dtmf_raw_lock:
            recent = [(t, f) for t, f in _dtmf_raw_buf if t > _ddl_time.time() - 0.15]
        if not recent:
            continue
        frames = np.concatenate([f for _, f in recent])[::decim]
        if len(frames) < FRAME:
            continue
        digit = _dtmf_decode_with_profiles(frames[-FRAME:], profiles, decode_rate)
        if digit == prev_digit[0]:
            hold[0] += 1
        else:
            prev_digit[0] = digit; hold[0] = 1
        if digit and hold[0] == DTMF_HOLD_TICKS:
            seq[0] = _dtmf_handle_digit(digit, _ddl_time.time(), seq[0])


def _dtmf_listener() -> None:
    """Controller thread: attaches to the shared AIOC RX tap whenever a
    radio interface is connected and starts the decode loop once profiles
    are available. Unlike Monitor/EchoTest there's no dashboard toggle —
    this runs for the life of the daemon, mirroring Pi's always-on design
    (DTMF remote control should work even if nobody's looking at a browser).
    Silently does nothing if no profiles are trained yet — Mac has no
    multimon-ng fallback (see dtmf_monitor.py's module docstring), so
    `python3 dtmf_monitor.py --train` is required before this is useful."""
    import time as _dl_time
    profiles = _load_dtmf_profiles()
    if not profiles:
        log.info("DTMF listener disabled — no trained profiles "
                 "(run dtmf_monitor.py --train first)")
        return
    decode_started = False
    while True:
        found = _radio.find_radio_audio_devices()
        if not found or found[1] is None:
            _dl_time.sleep(3); continue
        iface = found[0]
        _dtmf_squelch[0] = _radio.SquelchTracker(iface.cos_threshold, DTMF_COS_TAIL_S)
        if not _radio_rx_tap_acquire():
            log.warning("DTMF: could not attach to radio RX tap")
            _dtmf_squelch[0] = None
            _dl_time.sleep(3); continue

        log.info("DTMF listener ready via learned profiles (%d digit(s)) — "
                 "wake=%s sleep=%s deepsleep=%s/%s mon=%s/%s",
                 len(profiles), DTMF_WAKE_SEQ, DTMF_SLEEP_SEQ,
                 DTMF_DEEPSLEEP_SEQ, DTMF_WAKE_SILENT_SEQ,
                 DTMF_MONITOR_ON_SEQ, DTMF_MONITOR_OFF_SEQ)
        if not decode_started:
            _threading.Thread(target=_dtmf_decode_loop, args=(profiles,),
                               daemon=True, name="dtmf-decode").start()
            decode_started = True

        try:
            while _radio.find_radio_audio_devices():
                _dl_time.sleep(0.5)
        finally:
            _radio_rx_tap_release()
            _dtmf_squelch[0] = None
        _dl_time.sleep(3)


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
    """Friendly label for a sounddevice device index or name (or None for default)."""
    if idx is None:
        try:
            d = sd.query_devices(kind="input")
            return f"default ({d['name']})"
        except Exception:
            return "default"
    if isinstance(idx, str):
        return idx   # already a device name — nothing to look up
    try:
        d = sd.query_devices(idx)
        return f"{d['name']} (#{idx})"
    except Exception:
        return f"device #{idx}"


def _query_devices_fresh() -> list | None:
    """Enumerate audio devices in a fresh subprocess, bypassing this
    process's own PortAudio device-list cache — sd.query_devices() in a
    long-running process does not reliably pick up devices that were
    unplugged since it last initialized (confirmed live: a device that had
    fully disconnected still resolved a name via a direct in-process
    sd.query_devices(idx) call, hours after it was gone). Returns None on
    any failure — callers should fall back to the in-process (possibly
    stale) view rather than break."""
    import time as _qdf_time
    now = _qdf_time.time()
    cached = _query_devices_fresh_cache[0]
    if cached is not None and now - _query_devices_fresh_cache[1] < 5.0:
        return cached
    try:
        _qr = subprocess.run(
            [sys.executable, "-c",
             "import sounddevice as sd, json;"
             "print(json.dumps([{'name':d['name'],"
             "'max_input_channels':d['max_input_channels'],"
             "'max_output_channels':d['max_output_channels']}"
             " for d in sd.query_devices()]))"],
            capture_output=True, text=True, timeout=5)
        if _qr.returncode != 0:
            return None
        result = json.loads(_qr.stdout)
        _query_devices_fresh_cache[0] = result
        _query_devices_fresh_cache[1] = now
        return result
    except Exception:
        return None


def _fresh_device_label_and_resync(dev_ref: list, kind: str) -> str:
    """Resolve a friendly label for whatever's in dev_ref[0] (an index),
    verified against a fresh device list rather than this process's own
    possibly-stale cache — and self-heal dev_ref[0] in place if the device
    it names has actually disconnected, so later code (speak(), etc.)
    doesn't keep trying to use hardware that's gone. kind is "input" or
    "output". Falls back to the plain (possibly stale) _device_label() if
    the fresh subprocess query itself fails, rather than showing nothing."""
    idx = dev_ref[0]
    stale_label = _device_label(idx)
    if idx is None:
        return stale_label
    # Extract the bare name this process currently believes idx maps to.
    try:
        stale_name = sd.query_devices(idx)["name"]
    except Exception:
        return stale_label

    fresh = _query_devices_fresh()
    if fresh is None:
        return stale_label   # couldn't verify — better a possibly-stale label than none

    ch_key = "max_output_channels" if kind == "output" else "max_input_channels"

    def _resync_to(name: str) -> int | None:
        """Re-resolve `name` to an index actually valid in THIS process's
        own PortAudio cache — never write a fresh-SUBPROCESS index directly
        into dev_ref[0]. Subprocess and in-process device numbering aren't
        guaranteed to match once this process's own cache is stale (e.g.
        after a hot-unplug this process hasn't reinitialized for yet) —
        confirmed live: writing the subprocess-resolved index straight into
        dev_ref[0] here left it pointing at the wrong physical slot in this
        process's own stale cache, and the next InputStream open failed
        with "Invalid number of channels" [PaErrorCode -9998] because that
        slot's actual channel count didn't match what was requested. One
        reinit-and-retry, matching /device-set's established recovery
        pattern for the same class of staleness."""
        i = _resolve_device_by_name(name, kind)
        if i is not None:
            return i
        try:
            with _audio_open_lock:
                sd._terminate()
                sd._initialize()
        except Exception as exc:
            log.warning("Device resync: PortAudio reinit failed: %s", exc)
        return _resolve_device_by_name(name, kind)

    for i, d in enumerate(fresh):
        if d.get("name") == stale_name and d.get(ch_key, 0) > 0:
            resynced = _resync_to(stale_name)
            if resynced is not None and resynced != idx:
                dev_ref[0] = resynced   # index shifted (other devices connected/disconnected) — resync
            return f"{stale_name} (#{resynced if resynced is not None else i})"

    # Named device isn't in the fresh list at all — it's genuinely gone.
    # Fall back to whatever fresh device is capable in the requested
    # direction and resync to that, so the daemon stops pointing at
    # vanished hardware instead of just mislabeling it. Skip known-bad
    # devices (INVALID_OUTPUT_DEVICES) — passing the channel-count check
    # doesn't mean the device actually produces audible output.
    for i, d in enumerate(fresh):
        if d.get(ch_key, 0) > 0 and d.get("name") not in INVALID_OUTPUT_DEVICES:
            resynced = _resync_to(d["name"])
            if resynced is not None:
                dev_ref[0] = resynced
            return (f"{d['name']} (#{resynced if resynced is not None else i}) "
                    f"— replaced {stale_name!r}, disconnected")
    return f"{stale_name} (disconnected, no replacement found)"


def _current_input_device_name() -> str:
    """Bare device name (no index suffix) of the currently selected input
    device, or the system default input if none is explicitly selected —
    the stable key used for per-device voice profiles and calibration."""
    try:
        idx = _selected_input_device[0]
        d = sd.query_devices(idx if idx is not None else None, kind="input")
        return d["name"]
    except Exception:
        return "default"


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
        "radio_active":    _radio_profile_active[0],
        "monitor_active":  _radio_monitor_active[0],
        "echotest_active": _echotest_active[0],
    }
    try:
        in_dev  = _selected_input_device[0]
        out_dev = _selected_output_device[0]
        # Verified against a fresh (subprocess) device list rather than this
        # process's own cache, which doesn't reliably notice a device has
        # actually disconnected — confirmed live: this panel kept showing a
        # Bluetooth speaker hours after it was gone, while the Audio
        # Devices table below (already subprocess-based) correctly no
        # longer listed it at all.
        result["mic"] = _fresh_device_label_and_resync(_selected_input_device, "input")
        in_dev = _selected_input_device[0]   # may have been resynced above
        if out_dev is not None:
            result["speaker_name"] = _fresh_device_label_and_resync(_selected_output_device, "output")
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

AGENT_NAME = "Zeebot"  # overridden at startup by --agent-name

TRANSCRIPTION_PROMPT = "Zeebot."  # teaches name spelling only; too short to hallucinate as a command
TRANSCRIPTION_PROMPT_NORM = "zeebot"

WAKE_PHRASES     = {"zeebot wake up", "real time talk on", "real-time talk on", "realtimetalk on",
                    "zibob wake up", "zibot wake up", "libot wake up", "ziba wake up"}
SLEEP_PHRASES    = {"zeebot go to sleep", "real time talk off", "real-time talk off", "realtimetalk off"}

# Wake confirmation — affirmative responses accepted after Zeebot asks "Yes?"
_WAKE_CONFIRM_AFFIRM = {
    "yes", "yeah", "yep", "yup", "ok", "okay", "sure", "correct", "affirmative",
    "go ahead", "wake up", "wake", "activate", "please", "do it", "yes please",
    "好", "是", "对", "好的", "可以", "醒来",
}
_WAKE_CONFIRM_TIMEOUT = 15.0  # seconds to wait for confirmation before treating as mis-fire

MONITOR_ON_PHRASES  = {
    "zeebot start monitoring", "start monitoring", "zeebot monitor on",
    "monitor on", "zeebot monitoring on", "monitoring on",
    "start monitor", "zeebot start monitor",
    "begin monitoring", "begin monitor", "zeebot begin monitoring",
    "turn on monitoring", "turn monitoring on", "enable monitoring",
    "activate monitoring", "starting monitoring", "monitoring please",
    "monitor please", "please start monitoring", "please monitor",
}
MONITOR_OFF_PHRASES = {
    "zeebot stop monitoring", "stop monitoring", "zeebot monitor off",
    "monitor off", "zeebot monitoring off", "monitoring off",
    "stop monitor", "zeebot stop monitor",
    "end monitoring", "end monitor", "zeebot end monitoring",
    "turn off monitoring", "turn monitoring off", "disable monitoring",
    "deactivate monitoring", "stopping monitoring", "please stop monitoring",
}
CONTINUE_PHRASES = {"continue", "zeebot continue", "please continue", "go on", "go ahead",
                    "keep going", "继续", "继续说", "你继续", "请继续"}
# Owner-only mode toggles. Keep phrases ≥3 words — _matches_phrase's 60%
# word-overlap fuzzy pass makes short phrases trigger-happy.
OWNER_ONLY_ON_PHRASES  = {"only listen to me", "zeebot only listen to me",
                          "owner only mode", "owner mode on",
                          "只听我的", "只听我说话", "只听我的话"}
OWNER_ONLY_OFF_PHRASES = {"listen to everyone", "zeebot listen to everyone",
                          "everyone mode", "owner mode off",
                          "听大家的", "听所有人的", "听大家说话"}

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

def _is_in_multilang_whitelist(text: str) -> bool:
    """True if text is in a MULTILANG_WHITELIST_LANGS language.

    Script ranges checked first (fast); langdetect for Latin-script text.
    Inconclusive → let through. Extend MULTILANG_WHITELIST_LANGS to add languages.
    """
    has_hangul = has_kana = has_arabic = has_cyril = has_deva = has_cjk = False
    for ch in text:
        cp = ord(ch)
        if   0xAC00 <= cp <= 0xD7AF:                             has_hangul = True
        elif 0x3040 <= cp <= 0x30FF:                             has_kana   = True
        elif 0x0600 <= cp <= 0x06FF:                             has_arabic = True
        elif 0x0400 <= cp <= 0x04FF:                             has_cyril  = True
        elif 0x0900 <= cp <= 0x097F:                             has_deva   = True
        elif 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF:  has_cjk    = True

    if has_hangul: return "ko" in MULTILANG_WHITELIST_LANGS
    if has_kana:   return "ja" in MULTILANG_WHITELIST_LANGS
    if has_arabic: return "ar" in MULTILANG_WHITELIST_LANGS
    if has_cyril:  return any(c in MULTILANG_WHITELIST_LANGS for c in ("ru","uk","bg","sr","mk"))
    if has_deva:   return any(c in MULTILANG_WHITELIST_LANGS for c in ("hi","mr","ne"))
    if has_cjk:    return any(c in MULTILANG_WHITELIST_LANGS for c in ("zh","zh-cn","zh-tw"))

    # Pure Latin-script — use langdetect to distinguish EN/ES/MS/FR/etc.
    if _HAVE_LANGDETECT and len(text.split()) >= 2:
        try:
            lang = _langdetect(text)
            if lang not in MULTILANG_WHITELIST_LANGS:
                log.debug("whitelist rejected %r as %r", text[:60], lang)
                return False
        except _LangDetectException:
            pass  # inconclusive → let through
    return True

def _normalize(text: str) -> str:
    import string
    t = text.strip().lower()
    t = t.translate(str.maketrans(string.punctuation, " " * len(string.punctuation)))
    # Insert space at CJK↔Latin boundaries so "我係wake up" → "我係 wake up"
    t = re.sub(r'([一-鿿㐀-䶿])([a-zA-Z0-9])', r'\1 \2', t)
    t = re.sub(r'([a-zA-Z0-9])([一-鿿㐀-䶿])', r'\1 \2', t)
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

def _resolve_provider_api_key(cfg: dict, provider: str) -> str:
    """Read talk.providers.<provider>.apiKey, resolving OpenClaw SecretRefs
    ({"source":"file","provider":"...","id":"/a/b/c"}) if present."""
    key = (
        cfg.get("talk", {})
           .get("providers", {})
           .get(provider, {})
           .get("apiKey", "")
    )
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
    return key or ""

def load_openai_key() -> str:
    key = _resolve_provider_api_key(_load_json(OPENCLAW_CONFIG), "openai")
    if not key:
        raise RuntimeError(
            "No OpenAI API key at talk.providers.openai.apiKey in openclaw.json"
        )
    return key

def load_elevenlabs_key() -> str:
    """Returns "" (not an error) if unset — ElevenLabs TTS is optional; speak()
    falls back to OpenAI TTS / macOS `say` when no key is configured."""
    try:
        return _resolve_provider_api_key(_load_json(OPENCLAW_CONFIG), "elevenlabs")
    except Exception as e:
        log.warning("Could not load ElevenLabs key: %s", e)
        return ""

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


def _resync_output_device_after_reinit() -> None:
    """Re-resolve _selected_output_device[0] by name after a PortAudio
    reinit (sd._terminate()/_initialize()) — device indices are not stable
    across a reinit cycle. Confirmed live: after enough reinit churn from
    repeated AIOC plug/unplug testing, a previously-correct stored index
    for "AIOC Audio" (output, 1 channel) ended up pointing at "AIOC Audio"
    (input, 0 output channels) instead — same name, different physical
    port, silently broken speak()/TTS output. _switch_mic_stream's own
    hot-plug recovery already re-resolves the INPUT device by name for
    exactly this reason; nothing did the equivalent for output until now."""
    try:
        prefs = _load_device_prefs()
        name = prefs.get("output_device_name") if prefs else None
        if not name:
            return
        idx = _resolve_device_by_name(name, "output")
        if idx is not None and idx != _selected_output_device[0]:
            log.info("Resyncing output device after PortAudio reinit: %r -> #%d", name, idx)
            _selected_output_device[0] = idx
    except Exception as exc:
        log.warning("Output device resync failed: %s", exc)

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

def _save_sleep_state(sleeping: bool) -> None:
    """Persist sleep state to disk so it survives daemon/service restarts."""
    try:
        os.makedirs(os.path.dirname(SLEEP_STATE_FILE), exist_ok=True)
        with open(SLEEP_STATE_FILE, "w") as f:
            json.dump({"sleeping": sleeping}, f)
    except Exception as e:
        log.warning("Could not save sleep state: %s", e)

def _load_sleep_state() -> bool:
    """Return True if the daemon was sleeping when it last stopped."""
    try:
        with open(SLEEP_STATE_FILE) as f:
            return bool(json.load(f).get("sleeping", False))
    except (FileNotFoundError, json.JSONDecodeError):
        return False

# ── Speaker verification (owner-only mode) ───────────────────────────────────

_spk_extractor_lock = threading.Lock()
_spk_warned: list = [False]   # log the missing-lib/model warning only once

def _get_spk_extractor():
    """Lazy singleton for the sherpa-onnx speaker-embedding extractor.
    Returns None (and warns once) when the lib or model is unavailable."""
    with _spk_extractor_lock:
        if _spk_extractor[0] is not None:
            return _spk_extractor[0]
        if not _HAVE_SHERPA or not os.path.exists(SPK_MODEL_PATH):
            if not _spk_warned[0]:
                _spk_warned[0] = True
                log.warning("Speaker verification unavailable: %s",
                            "sherpa-onnx not installed" if not _HAVE_SHERPA
                            else f"model missing at {SPK_MODEL_PATH}")
            return None
        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=SPK_MODEL_PATH, num_threads=2, provider="cpu")
        _spk_extractor[0] = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
        log.info("Speaker-embedding extractor loaded (dim=%d)", _spk_extractor[0].dim)
        return _spk_extractor[0]

def _resample_to_16k(pcm_int16: np.ndarray, src_rate: int) -> np.ndarray:
    """int16 PCM at src_rate → float32 [-1,1] at 16 kHz (FFT resample, scipy-free)."""
    x = pcm_int16.astype(np.float64) / 32768.0
    if src_rate == SPK_SAMPLE_RATE:
        return x.astype(np.float32)
    n_out = int(len(x) * SPK_SAMPLE_RATE / src_rate)
    y = np.fft.irfft(np.fft.rfft(x), n_out) * (n_out / len(x))
    return y.astype(np.float32)

def _compute_embedding(pcm_int16: np.ndarray, rate: int) -> "np.ndarray | None":
    """Blocking — call from an executor or HTTP handler thread."""
    ex = _get_spk_extractor()
    if ex is None:
        return None
    try:
        s = ex.create_stream()
        s.accept_waveform(SPK_SAMPLE_RATE, _resample_to_16k(pcm_int16, rate))
        s.input_finished()
        emb = np.asarray(ex.compute(s), dtype=np.float32)
        norm = np.linalg.norm(emb)
        return emb / norm if norm > 0 else None
    except Exception as e:
        log.warning("Speaker embedding failed: %s", e)
        return None

def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b))   # inputs are L2-normalized

def _owner_score(emb: np.ndarray, device_key: str) -> float:
    """Max cosine over the profile mean + per-sample embeddings for the given
    device's profile — max keeps cross-language scoring robust since
    enrollment mixes EN and ZH samples. Caller must check
    _verification_available(device_key) first."""
    prof = _owner_profiles[device_key]
    refs = [prof["mean"]] + prof["samples"]
    return max(_cosine(emb, r) for r in refs)

def _read_voice_profiles_raw() -> dict:
    """Read the on-disk multi-device store as-is (full per-sample metadata:
    lang/prompt/secs/embedding — richer than the in-memory scoring shape)."""
    try:
        with open(VOICE_PROFILES_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def _load_voice_profiles() -> None:
    """Load all per-device voice profiles from disk into _owner_profiles
    (bare-ndarray scoring shape; on-disk keeps richer per-sample metadata).

    One-time migration: if the old single-profile file from before per-device
    profiles existed exists and the new store doesn't, adopt it under the
    name of the currently selected/default input device (it was almost
    certainly enrolled on whatever mic was in use at the time) and archive
    the old file rather than deleting it.
    """
    global _owner_profiles
    _owner_profiles = {}
    raw = _read_voice_profiles_raw()
    if not raw:
        _OLD_VOICE_PROFILE_FILE = os.path.expanduser("~/.openclaw/workspace/rtt_voice_profile.json")
        try:
            with open(_OLD_VOICE_PROFILE_FILE) as f:
                old_data = json.load(f)
            migrate_key = _current_input_device_name()
            raw = {migrate_key: old_data}
            os.makedirs(os.path.dirname(VOICE_PROFILES_FILE), exist_ok=True)
            with open(VOICE_PROFILES_FILE, "w") as f:
                json.dump(raw, f)
            os.rename(_OLD_VOICE_PROFILE_FILE, _OLD_VOICE_PROFILE_FILE + ".migrated")
            log.info("Migrated single voice profile -> per-device profile for %r", migrate_key)
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            raw = {}

    for device_name, data in raw.items():
        try:
            samples = [np.asarray(s["embedding"], dtype=np.float32) for s in data["samples"]]
            _owner_profiles[device_name] = {
                "mean": np.asarray(data["mean"], dtype=np.float32),
                "samples": samples, "created": data.get("created", 0),
            }
        except (KeyError, ValueError):
            log.warning("Skipping malformed voice profile for %r", device_name)
    log.info("Loaded voice profiles for %d device(s): %s",
             len(_owner_profiles), ", ".join(_owner_profiles) or "none")

def _save_voice_profile(samples: list, device_key: str) -> None:
    """samples: [{"lang","prompt","secs","embedding":[...]}]. Computes the mean,
    read-modify-writes the on-disk multi-device store (preserving other
    devices' entries and full per-sample metadata), then reloads
    _owner_profiles from disk so the in-memory scoring shape matches."""
    import time as _tvp
    embs = np.stack([np.asarray(s["embedding"], dtype=np.float32) for s in samples])
    mean = embs.mean(axis=0)
    mean = mean / np.linalg.norm(mean)
    raw = _read_voice_profiles_raw()
    raw[device_key] = {"version": 1, "model": "campplus_zh_en_advanced", "dim": int(embs.shape[1]),
                        "created": _tvp.time(), "samples": samples, "mean": mean.tolist()}
    os.makedirs(os.path.dirname(VOICE_PROFILES_FILE), exist_ok=True)
    with open(VOICE_PROFILES_FILE, "w") as f:
        json.dump(raw, f)
    _load_voice_profiles()

def _save_voice_mode() -> None:
    try:
        os.makedirs(os.path.dirname(VOICE_MODE_FILE), exist_ok=True)
        with open(VOICE_MODE_FILE, "w") as f:
            json.dump({"owner_only": _owner_only[0],
                       "threshold": _spk_threshold[0]}, f)
    except Exception as e:
        log.warning("Could not save voice mode: %s", e)

def _load_voice_mode() -> None:
    try:
        with open(VOICE_MODE_FILE) as f:
            data = json.load(f)
        _owner_only[0] = bool(data.get("owner_only", False))
        _spk_threshold[0] = float(data.get("threshold", SPK_THRESHOLD_DEFAULT))
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        pass

def _verification_available(device_key: str) -> bool:
    return device_key in _owner_profiles and _get_spk_extractor() is not None

def _record_pcm_blocking(secs: float, device=None) -> np.ndarray:
    """Record for enrollment/testing. Sets _enroll_active so _mic_cb discards
    audio (the utterance must not reach transcription). Applies MIC_GAIN (no
    gate) for channel consistency with runtime audio.

    device: explicit sounddevice index/name to record from, or None to use
    whatever's currently selected (_selected_input_device[0]) — NOT sd.rec's
    own default, which is the OS-wide system default input and silently
    ignores Radio Mode's input switch entirely (sd.default.device is never
    set anywhere in this file)."""
    _enroll_active[0] = True
    try:
        rec_device = device if device is not None else _selected_input_device[0]
        rec = sd.rec(int(secs * DEVICE_RATE), samplerate=DEVICE_RATE,
                     channels=CHANNELS, dtype="int16", device=rec_device)
        sd.wait()
        pcm = rec.reshape(-1).astype(np.float32) * MIC_GAIN
        return np.clip(pcm, -32768, 32767).astype(np.int16)
    finally:
        _enroll_active[0] = False

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
    text = re.sub(r'`{1,3}([^`\n]*)`{1,3}', r'\1', text)
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
    """Split text into [(segment, 'zh'|'en')] so each segment uses its correct voice.

    Digits and ASCII punctuation are treated as sticky — they follow the current
    language rather than forcing a break, so "5月24日" stays in one zh segment.
    """
    segments: list[tuple[str, str]] = []
    current_chars: list[str] = []
    current_lang = None
    for ch in text:
        if _is_cjk(ch):
            lang = 'zh'
        elif ch.isdigit() or ch in ' \t\n\r，。！？；：、""''「」《》':
            # sticky: follow current language (default 'en' if nothing yet)
            lang = current_lang or 'en'
        else:
            lang = 'en'
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
    return [(s, l) for s, l in segments if s]


def _num_to_zh(n: int) -> str:
    """Convert a non-negative integer to Chinese character representation."""
    if n == 0:
        return '零'
    digits = '零一二三四五六七八九'
    units = ['', '十', '百', '千']
    groups = ['', '万', '亿']
    def _group(num: int) -> str:
        result = ''
        for i in range(3, -1, -1):
            d = (num // (10 ** i)) % 10
            if d:
                result += digits[d] + units[i]
            elif result and not result.endswith('零'):
                result += '零'
        return result.rstrip('零') or '零'
    parts = []
    g = 0
    while n > 0:
        chunk = n % 10000
        if chunk:
            parts.append(_group(chunk) + groups[g])
        n //= 10000
        g += 1
    result = ''.join(reversed(parts))
    # normalize leading 一十 → 十
    if result.startswith('一十'):
        result = result[1:]
    return result


def _zh_numbers(text: str) -> str:
    """Replace ASCII digit sequences in a Chinese segment with Chinese numerals."""
    import re
    return re.sub(r'\d+', lambda m: _num_to_zh(int(m.group())), text)


_ZH_DIGITS = '零一二三四五六七八九'

def _to_zh_num(n: int) -> str:
    """Convert 0-99 to Chinese numeral string."""
    if n == 0:
        return '零'
    if n < 10:
        return _ZH_DIGITS[n]
    tens = ('' if n // 10 == 1 else _ZH_DIGITS[n // 10]) + '十'
    ones = _ZH_DIGITS[n % 10] if n % 10 else ''
    return tens + ones


def _preprocess_zh_time(text: str) -> str:
    """Convert H:MM / HH:MM time patterns to Chinese when text contains CJK characters.

    "4:20" → "四点二十分", "12:00" → "十二点整", "10:05" → "十点零五分"
    Only applied when the text is primarily Chinese to avoid mangling English timestamps.
    """
    import re
    if not any(_is_cjk(c) for c in text):
        return text

    def _replace(m: "re.Match") -> str:
        h, mn = int(m.group(1)), int(m.group(2))
        result = _to_zh_num(h) + '点'
        if mn == 0:
            result += '整'
        elif mn < 10:
            result += '零' + _ZH_DIGITS[mn] + '分'
        else:
            result += _to_zh_num(mn) + '分'
        return result

    return re.sub(r'(?<!\d)(\d{1,2}):(\d{2})(?!\d)', _replace, text)


def _preprocess_acronyms(text: str) -> str:
    """Space-separate 2-4 letter uppercase codes so TTS reads them letter by letter.

    Matches sequences of uppercase letters not adjacent to other letters,
    so ICN → I C N, JFK → J F K, KE → K E, but not English words like "The".
    Applied regardless of language since codes should always be spelled out.
    """
    import re
    return re.sub(r'(?<![a-zA-Z])([A-Z]{2,4})(?![a-zA-Z])',
                  lambda m: ' '.join(m.group(1)), text)


def _preprocess_units(text: str) -> str:
    """Expand percent/degree symbols into words before TTS.

    eleven_v3 doesn't apply ElevenLabs' usual automatic text normalization,
    so raw symbols like "72°F" or "45%" come out mangled or silently dropped
    instead of spoken — confirmed live. Spelling them out here sidesteps that
    regardless of which TTS engine ends up handling the call.

    Branches on language like _preprocess_zh_time: a reply containing CJK
    gets Chinese unit words (华氏度/摄氏度/百分之) so speech doesn't code-switch
    into English mid-sentence for a number embedded in an otherwise-Chinese
    reply; everything else gets spelled-out English.

    "72°F" → "72 degrees Fahrenheit" / "72华氏度" (zh)
    "20°C" → "20 degrees Celsius" / "20摄氏度" (zh)
    "15°"  → "15 degrees" / "15度" (zh)
    "45%"  → "45 percent" / "百分之45" (zh — unit precedes the number, as spoken)
    """
    import re
    if any(_is_cjk(c) for c in text):
        text = re.sub(r'(\d+(?:\.\d+)?)\s*°\s*F\b', r'\1华氏度', text)
        text = re.sub(r'(\d+(?:\.\d+)?)\s*°\s*C\b', r'\1摄氏度', text)
        text = re.sub(r'(\d+(?:\.\d+)?)\s*°', r'\1度', text)
        text = re.sub(r'(\d+(?:\.\d+)?)\s*%', r'百分之\1', text)
        return text
    text = re.sub(r'(?<=\d)\s*°\s*F\b', ' degrees Fahrenheit', text)
    text = re.sub(r'(?<=\d)\s*°\s*C\b', ' degrees Celsius', text)
    text = re.sub(r'(?<=\d)\s*°', ' degrees', text)
    text = re.sub(r'(?<=\d)\s*%', ' percent', text)
    return text


# ── TTS (OpenAI TTS primary, macOS `say` fallback, ffmpeg PCM decode) ────────

TTS_SAMPLE_RATE = 24000  # daemon plays back at 24 kHz mono PCM int16

def _openai_tts_to_mp3(text: str, out_path: str, timeout: float = OPENAI_TTS_TIMEOUT) -> bool:
    """Render text via OpenAI TTS API → MP3 at out_path. Handles mixed Chinese/English natively."""
    import json, urllib.request, urllib.error
    key = _openai_tts_key[0]
    if not key:
        log.warning("OpenAI TTS key not set")
        return False
    try:
        payload = json.dumps({
            "model": OPENAI_TTS_MODEL,
            "input": text,
            "voice": OPENAI_TTS_VOICE,
        }).encode()
        req = urllib.request.Request(
            "https://api.openai.com/v1/audio/speech",
            data=payload,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            with open(out_path, "wb") as f:
                f.write(resp.read())
        return os.path.exists(out_path) and os.path.getsize(out_path) > 0
    except urllib.error.HTTPError as e:
        log.warning("OpenAI TTS HTTP error %d: %s", e.code, e.read()[:200])
        return False
    except Exception as e:
        log.warning("OpenAI TTS error: %s", e)
        return False


def _elevenlabs_tts_to_mp3(text: str, out_path: str, timeout: float = ELEVENLABS_TIMEOUT) -> bool:
    """Render text via ElevenLabs → MP3 at out_path."""
    import json, urllib.request, urllib.error
    key = _elevenlabs_tts_key[0]
    if not key:
        return False
    try:
        payload = json.dumps({
            "text": text,
            "model_id": ELEVENLABS_MODEL,
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        }).encode()
        req = urllib.request.Request(
            f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}",
            data=payload,
            headers={
                "xi-api-key": key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            with open(out_path, "wb") as f:
                f.write(resp.read())
        return os.path.exists(out_path) and os.path.getsize(out_path) > 0
    except urllib.error.HTTPError as e:
        log.warning("ElevenLabs TTS HTTP error %d: %s", e.code, e.read()[:200])
        return False
    except Exception as e:
        log.warning("ElevenLabs TTS error: %s", e)
        return False


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
    """Synthesise text via ElevenLabs TTS (with OpenAI/macOS fallback) and play via sounddevice.

    Sends the full text in one call. Decodes to 24 kHz mono PCM int16,
    applies software volume, and plays via the selected CoreAudio output.
    Polls the mic level during playback; if the user starts speaking, calls
    sd.stop() to interrupt.
    """
    import tempfile
    if volume < 0:
        volume = _cal_sw_volume
    clean = strip_markdown(text)
    if not clean:
        log.warning("speak() called with empty text after strip_markdown: %r", text)
        return
    clean = _preprocess_zh_time(clean)
    clean = _preprocess_acronyms(clean)
    clean = _preprocess_units(clean)

    log.info("speak() → %r  vol=%.2f sys_vol=%d out_dev=%s",
             clean[:80], volume, _cal_sys_vol_pct[0], _selected_output_device[0])

    pcm_parts: list[np.ndarray] = []
    temp_files: list[str] = []
    silence_samples = int(TTS_SAMPLE_RATE * silence_ms / 1000)
    _tx_radio = False   # set True below once TTS is ready, if Radio Mode is transmitting this reply

    try:
        if silence_ms > 0:
            pcm_parts.append(np.zeros(silence_samples, dtype=np.int16))

        mp3_path = tempfile.mktemp(suffix=".mp3")
        temp_files.append(mp3_path)
        pcm = np.zeros(0, dtype=np.int16)

        # Prefer ElevenLabs for the whole reply. OpenAI stays as the network/key
        # fallback so voice responses continue working if ElevenLabs is down.
        if _elevenlabs_tts_to_mp3(clean, mp3_path):
            pcm = _decode_to_pcm(mp3_path)
            log.info("  ElevenLabs TTS OK — PCM decode: %d samples (%.1fs)",
                     pcm.size, pcm.size / TTS_SAMPLE_RATE)
        else:
            log.info("  ElevenLabs TTS unavailable/failed — falling back to OpenAI TTS")

        if pcm.size == 0:
            ok_oai = _openai_tts_to_mp3(clean, mp3_path)
            log.info("  OpenAI TTS %s", "OK" if ok_oai else "FAILED")
            if ok_oai:
                pcm = _decode_to_pcm(mp3_path)
                log.info("  PCM decode: %d samples (%.1fs)", pcm.size, pcm.size / TTS_SAMPLE_RATE)

        if pcm.size == 0:
            # Fall back to macOS `say` — split by script for correct voice selection
            for seg_text, lang in _split_by_script(clean):
                if not seg_text.strip():
                    continue
                aiff_path = tempfile.mktemp(suffix=".aiff")
                temp_files.append(aiff_path)
                ok_say = _say_fallback_to_aiff(seg_text, lang, aiff_path)
                log.info("  say fallback %s for %r", "OK" if ok_say else "FAILED", seg_text[:40])
                if ok_say:
                    seg_pcm = _decode_to_pcm(aiff_path)
                    if seg_pcm.size:
                        pcm_parts.append(seg_pcm)
        else:
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
        # Radio Mode: route TTS out over the air instead of the local speaker,
        # keyed by PTT for the duration of playback. _ptt_alive() confirms the
        # radio interface is actually connected right now, not just that the
        # Radio Mode toggle is on.
        _tx_radio = bool(_radio_profile_active[0] and _ptt_alive())
        if _tx_radio:
            _radio_out = _radio.find_radio_audio_devices()
            if _radio_out and _radio_out[2] is not None:
                out_dev = _radio_out[2]
            else:
                _tx_radio = False   # no radio output resolved — fall back to local speaker
        log.info("  sd.play() %d samples peak=%d gain=%.3f dev=%s%s",
                 final.size, int(np.max(np.abs(final))), total_gain, out_dev,
                 " (radio TX)" if _tx_radio else "")
        import time as _t
        try:
            _is_speaking[0] = True
            if _tx_radio:
                _ptt_key()
                _t.sleep(_ptt_prekey_s())
            sd.play(final, samplerate=TTS_SAMPLE_RATE, device=out_dev, blocking=False)
        except Exception as e:
            _is_speaking[0] = False
            if _tx_radio:
                try: _ptt_release()
                except Exception: pass
            log.error("sd.play() failed: %s", e)
            return

        if _tx_radio:
            # Radio TX: skip the self-interrupt monitor entirely. Mic capture
            # is already forced to silence while _is_tx is set (see _mic_cb),
            # so there is no real mic signal to measure coupling against, and
            # known TX->RX crosstalk on this hardware would make any barge-in
            # detection built from it unreliable anyway — same reasoning as
            # Pi's v3.8.0 change.
            pass
        else:
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
            output_peak = int(np.max(np.abs(final)))
            INTERRUPT_GUARD_TICKS  = 20    # 1 s guard — 300 ms silence + 700 ms speech to measure
            INTERRUPT_SAFETY       = 1.8   # threshold = measured_echo × 1.8 (user must be clearly louder)
            TICK_SAMPLES = TTS_SAMPLE_RATE * 50 // 1000   # samples per 50 ms tick

            interrupt_threshold = SPEAK_INTERRUPT_PEAK    # updated after guard, then tracked continuously
            guard_floor = SPEAK_INTERRUPT_PEAK   # threshold floor set by the guard measurement — see below
            guard_max_out = 0   # peak output PCM seen during guard
            guard_max_mic = 0   # peak mic level seen during guard (echo baseline)
            coupling: float | None = None   # continuously-tracked echo/coupling ratio, past the guard window
            consec = 0
            guard  = INTERRUPT_GUARD_TICKS
            tick_idx = 0

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

                s0 = tick_idx * TICK_SAMPLES
                s1 = s0 + TICK_SAMPLES
                tick_out = int(np.max(np.abs(final[s0:s1]))) if s1 <= len(final) else 0
                tick_idx += 1

                if guard > 0:
                    # Accumulate peak output and peak mic separately — dividing per-tick
                    # ratios inflates the coupling when a quiet output tick is divided
                    # against any mic background noise.
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
                            guard_floor = interrupt_threshold
                            log.info("  coupling=%.3f (echo=%d/out=%d) interrupt_threshold=%d",
                                     coupling, guard_max_mic, guard_max_out, interrupt_threshold)
                        else:
                            log.info("  no coupling data — using floor threshold=%d",
                                     interrupt_threshold)
                    continue

                # Keep tracking coupling past the initial guard so a long or unevenly-
                # loud reply doesn't outrun a threshold frozen from the first second —
                # but never learn from a tick that already looks like a real barge-in,
                # or a genuine interruption would just get EMA'd away. Also require the
                # tick to be genuinely loud (comparable to this reply's peak), not just
                # above the 200 floor — a quiet tick's mic/output ratio is dominated by
                # room noise floor rather than real echo, and letting those ticks drag
                # the EMA down was observed live shrinking the threshold ~40% within a
                # second (561 → 316), causing a normal loud syllable later in the same
                # reply to falsely trigger a self-interrupt.
                if tick_out > max(200, int(output_peak * 0.3)) and p <= interrupt_threshold:
                    local = p / tick_out
                    coupling = local if coupling is None else (
                        coupling * (1 - SPEAK_COUPLING_EMA) + local * SPEAK_COUPLING_EMA)
                    # guard_floor never shrinks below the guard's own measurement: the guard
                    # takes a MAX over a full second, which is statistically always ≥ any
                    # single later EMA sample, so unclamped tracking only ever drifts down
                    # over a long reply — confirmed live (1420 → 399 within 36s, tripping a
                    # false self-interrupt on an ordinary loud syllable). The EMA can still
                    # push the threshold higher if echo genuinely grows louder later on.
                    interrupt_threshold = max(
                        int(output_peak * coupling * INTERRUPT_SAFETY), SPEAK_INTERRUPT_PEAK,
                        guard_floor)

                if _http_interrupt[0]:
                    log.info("HTTP interrupt — stopping TTS")
                    _http_interrupt[0] = False
                    _interrupted[0] = True
                    _clear_audio_buffer[0] = True  # flush OpenAI VAD buffer after interrupt
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
                        _clear_audio_buffer[0] = True  # flush OpenAI VAD buffer after interrupt
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
        if _tx_radio:
            _t.sleep(_ptt_tail_s())
            _ptt_release()

        if not _interrupted[0] and resumable:
            _paused_speech[0] = None   # finished normally — nothing to resume

    except Exception as e:
        log.error("speak() error: %s", e)
    finally:
        _is_speaking[0] = False
        if _tx_radio and _is_tx[0]:
            # Belt-and-suspenders: an exception between _ptt_key() and the
            # normal release point above would otherwise leave PTT stuck
            # asserted — a stuck radio transmission, not just a silent bug.
            try: _ptt_release()
            except Exception: pass
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
        self._ready: asyncio.Event = asyncio.Event()  # set on connect, cleared on disconnect
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
            err = hello.get("error", {})
            if isinstance(err, dict) and err.get("retryable"):
                raise ConnectionError(f"Gateway not ready (retryable): {err.get('message', err)}")
            raise RuntimeError(f"Gateway connect failed: {err}")
        scopes = hello.get("payload", {}).get("auth", {}).get("scopes", [])
        log.info("OpenClaw gateway connected (scopes: %s)", scopes)
        self._ready.set()

    async def listen(self, stop_event: asyncio.Event):
        """Route incoming gateway events to waiting futures. Run as a task."""
        while not stop_event.is_set():
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
                if stop_event.is_set():
                    break
                log.warning("Gateway WebSocket closed — reconnecting…")
                _log_entry("system", "Gateway disconnected — reconnecting…")
            except Exception as e:
                if stop_event.is_set():
                    break
                log.warning("Gateway listen error (%s) — reconnecting…", e)

            if stop_event.is_set():
                break

            # Fail in-flight futures so ask() doesn't hang during reconnect window
            self._ready.clear()
            for fut in list(self._send_acks.values()):
                if not fut.done():
                    fut.set_exception(ConnectionError("Gateway reconnecting"))
            self._send_acks.clear()
            for fut in list(self._reply_futs.values()):
                if not fut.done():
                    fut.set_exception(ConnectionError("Gateway reconnecting"))
            self._reply_futs.clear()

            while not stop_event.is_set():
                try:
                    await self.connect()
                    log.info("Gateway reconnected.")
                    _log_entry("system", "Gateway reconnected.")
                    break
                except Exception as e:
                    log.warning("Gateway reconnect failed (%s) — retrying in 5s", e)
                    await asyncio.sleep(5)

    async def ask(self, message: str, session_key: str = OPENCLAW_SESSION) -> str:
        """Send a message to the agent and return its complete reply text."""
        await asyncio.wait_for(self._ready.wait(), timeout=20)
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
        self._active      = _persist_active[0]       # restored across 60-min OpenAI reconnects
        self._monitoring  = _persist_monitoring[0]  # restored across 60-min OpenAI reconnects
        self._multilang   = _persist_multilang[0]   # "off"|"en-zh"|"whitelist"|"any"
        self._mic_stream_ref: list = [None]   # current sd.InputStream; swapped on hot-plug
        self._pending_wake_confirm = False    # True while waiting for voice confirmation to activate
        self._pending_wake_t       = 0.0      # timestamp when confirmation was requested
        # Speaker verification: mirror the PCM we stream to OpenAI so each
        # VAD segment can be embedded and matched against the owner profile.
        self._preroll = collections.deque(maxlen=SPK_PREROLL_MS // 100)  # 100 ms blocks
        self._capture_buf = None   # open segment (speech_started..stopped)
        self._capture_consumed = False   # transcript already took the open segment
        self._pending_segments: collections.deque = collections.deque(maxlen=4)  # (epoch, bytes)

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
        # Same treatment while PTT is asserted (_is_tx) — known TX->RX
        # crosstalk on this radio hardware means the mic can pick up our own
        # transmitted audio; keep sending silence rather than a stream gap so
        # the OpenAI Realtime session's VAD stays continuous.
        import time as _tcb
        if self._busy.is_set() or _tcb.time() < _post_busy_until[0] or _is_tx[0]:
            silence = np.zeros_like(raw)
            self.loop.call_soon_threadsafe(self._enqueue_mic, silence.tobytes())
            return
        if _enroll_active[0]:
            return  # enrollment recording in progress — keep it out of transcription
        if raw_peak < MIC_GATE_PEAK:
            out_arr = np.zeros_like(raw)
            # Gain frozen during silence — WebRTC AGC won't pump up on noise
        elif _webrtc_proc is not None and not _radio_profile_active[0]:
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
            # Numpy fallback: RMS leveler + tanh soft limiter. Also the
            # forced path for Radio Mode (see the elif above) — Pi's AGC
            # profile for radio audio individually disables WebRTC's voice
            # detection, transient suppression, and echo cancellation
            # (radio audio's frequency response/compression confuses VAD,
            # transient suppression clips consonants, and AEC has nothing
            # real to cancel), but webrtc_noise_gain's Mac binding only
            # exposes __init__(rate, aggressiveness) + Process10ms() with
            # no per-effect toggles, so that fine-grained config can't be
            # replicated here. This RMS+tanh leveler has none of those
            # WebRTC effects to begin with, which sidesteps the problem.
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
        # Runs on the event loop (call_soon_threadsafe) — same thread as
        # _recv_ws, so the capture structures need no locking.
        self._preroll.append(data)
        if (self._capture_buf is not None
                and len(self._capture_buf) < SPK_MAX_SEGMENT_SECS * 10):
            self._capture_buf.append(data)

    def _pop_segment(self):
        """Return the PCM segment matching the transcript that just arrived.
        Server VAD emits segments and transcripts in the same order → FIFO."""
        import time as _tps
        now = _tps.time()
        while self._pending_segments and now - self._pending_segments[0][0] > SPK_SEGMENT_STALE_SECS:
            self._pending_segments.popleft()
        if self._pending_segments:
            return self._pending_segments.popleft()[1]
        if self._capture_buf is not None and not self._capture_consumed:
            # Transcript beat speech_stopped — snapshot the open buffer and
            # mark it consumed so the late stop event doesn't push an orphan.
            self._capture_consumed = True
            return b"".join(self._capture_buf)
        return None

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

    async def _idle_watcher(self, ws):
        """Disconnect from OpenAI after AUTO_SLEEP_SECS of inactivity.

        Runs regardless of active/monitoring/silent state. Disabled while
        multilang != 'off' (non-English sessions should never auto-sleep).
        Resets on wake phrase and any LLM route. Closes ws directly so the
        async-with context in run() exits cleanly (matches Debian behaviour).
        """
        import time as _as
        while not self.stop_event.is_set():
            await asyncio.sleep(30.0)
            if self._multilang != "off":
                continue   # multilang active — never auto-sleep
            idle = _as.time() - _last_interaction[0]
            if idle < AUTO_SLEEP_SECS:
                continue
            mins = int(idle / 60)
            log.info("Auto-sleep: idle %d min — disconnecting OpenAI", mins)
            self._active = False
            _persist_active[0] = False
            if self._monitoring:
                self._monitoring = False
                _persist_monitoring[0] = False
                log.info("Auto-sleep: monitoring cleared")
            _log_entry("system", f"Auto-sleep after {mins} min idle. Press Wake to reconnect.")
            await asyncio.get_running_loop().run_in_executor(
                None, speak,
                "Going to sleep. Press Wake to reconnect.",
                self.alsa_output,
            )
            _sleep_requested[0] = True
            _is_sleeping[0] = True
            _save_sleep_state(True)
            await ws.close()   # closes OpenAI WS; run()'s async-with exits cleanly
            return

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
                with _portaudio_reinit_lock:
                    sd._terminate()
                    sd._initialize()
                log.info("PortAudio reinitialized for hot-plug")
                _resync_output_device_after_reinit()
            except Exception as e:
                log.warning("PortAudio reinit error: %s", e)

            # Resolve new device index from prefs by name using the already-reinited
            # PortAudio context. Fall back to the last known selected device.
            #
            # If Radio Mode is active, prefer reopening on the AIOC's audio-in
            # over the saved mic preference — otherwise a mid-session AIOC
            # glitch (e.g. contention from another stream opening on the same
            # USB device around the same time) silently falls back to the
            # normal mic without anyone telling _radio_profile_active[0],
            # leaving the Radio button showing "on" while nothing is actually
            # listening to the radio anymore.
            saved_name = None
            new_idx = _selected_input_device[0]  # fallback to last known good index
            if _radio_profile_active[0]:
                _rf = _radio.find_radio_audio_devices()
                if _rf and _rf[1] is not None:
                    new_idx = _rf[1]
                    saved_name = "AIOC (Radio Mode)"
                else:
                    log.warning("Hot-plug: Radio Mode active but AIOC not found — "
                                "falling back to saved mic preference, turning Radio Mode off")
                    _radio_profile_active[0] = False
                    _radio_prev_input_device[0] = None
            if saved_name is None:
                prefs = _load_device_prefs()
                pref_name = prefs.get("input_device_name") if prefs else None
                if pref_name:
                    resolved = _resolve_device_by_name(pref_name, "input")
                    if resolved is not None:
                        new_idx = resolved
                        saved_name = pref_name
                    else:
                        log.warning("Hot-plug: saved device %r not found after reinit", pref_name)
                if _radio_profile_active[0]:
                    # Reopening on the normal mic while Radio Mode still claims
                    # to be on would be the same inconsistency — turn it off.
                    log.warning("Hot-plug: reopening on non-radio device — turning Radio Mode off")
                    _radio_profile_active[0] = False
                    _radio_prev_input_device[0] = None

            log.info("Hot-plug: attempting to reopen mic on device idx=%s (%s)",
                     new_idx, saved_name or "default")
            try:
                with _audio_open_lock:
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
            # Apply DTMF force flags immediately (don't wait for next transcript)
            # — this loop iterates on every mic chunk, so this checks at least
            # every DEVICE_BLOCKSIZE and at most every 0.5s (the timeout below).
            if _dtmf_force_active[0]:
                _dtmf_force_active[0] = False
                if self._monitoring:
                    self._monitoring = False   # Active supersedes Monitoring
                    _log_entry("system", "Monitoring stopped")
                if not self._active:
                    self._active = True
                    _last_interaction[0] = __import__("time").time()
                    _log_entry("system", "Voice activated")
                    log.info("DTMF force-active applied to session")
            if _dtmf_force_monitor[0] is not None:
                _mon = _dtmf_force_monitor[0]
                _dtmf_force_monitor[0] = None
                if _mon and not self._monitoring:
                    self._monitoring = True
                    self._active = False   # monitoring is passive
                    _log_entry("system", "Monitoring started")
                    log.info("DTMF force-monitor ON")
                elif not _mon and self._monitoring:
                    self._monitoring = False
                    _log_entry("system", "Monitoring stopped")
                    log.info("DTMF force-monitor OFF")
            if _dtmf_force_deepsleep[0]:
                _dtmf_force_deepsleep[0] = False
                _persist_active[0] = False
                _persist_monitoring[0] = False
                self._monitoring = False   # turn off monitoring on current session
                _sleep_requested[0] = True
                _is_sleeping[0] = True
                _save_sleep_state(True)
                log.info("DTMF deep-sleep — closing WebSocket")
                await ws.close()   # closes OpenAI WS; run()'s async-with exits cleanly
                return
            if _dtmf_force_silent[0]:
                _dtmf_force_silent[0] = False
                self._active = False
                _log_entry("system", "Voice silenced")
                log.info("DTMF force-silent applied to session")
            try:
                chunk = await asyncio.wait_for(self._mic_q.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if self._busy.is_set():
                continue
            # After TTS interrupt, clear OpenAI's audio buffer so stale VAD data
            # doesn't generate a spurious transcript of the interrupted echo.
            if _clear_audio_buffer[0]:
                _clear_audio_buffer[0] = False
                await ws.send(json.dumps({"type": "input_audio_buffer.clear"}))
                # Mirror the server-side discard in the verification capture,
                # or the segment FIFO desyncs from the transcript stream.
                self._pending_segments.clear()
                self._capture_buf = None
            await ws.send(json.dumps({
                "type":  "input_audio_buffer.append",
                "audio": base64.b64encode(chunk).decode(),
            }))

    async def _verify_speaker(self, transcript: str) -> bool:
        """Owner-only gate. Pops the matching audio segment (always — mode
        toggles mid-stream must not desync the FIFO), embeds it, and compares
        against the profile enrolled for whichever input device is currently
        active. Silence is the fail-safe: missing or too-short segments are
        rejected in owner-only mode."""
        segment = self._pop_segment()
        if not _owner_only[0]:
            return True
        device_key = _current_input_device_name()
        if not _verification_available(device_key):
            return True   # not enrolled for this device / model missing — dashboard banner covers this
        if segment is None:
            _log_entry("system", f"Voice check: no audio segment — ignored ({transcript[:40]!r})")
            return False
        secs = len(segment) / 2 / SAMPLE_RATE
        if secs < SPK_MIN_SECS:
            _log_entry("system", f"Voice check: too short to verify ({secs:.1f}s) — ignored ({transcript[:40]!r})")
            return False
        emb = await asyncio.get_running_loop().run_in_executor(
            None, _compute_embedding, np.frombuffer(segment, np.int16), SAMPLE_RATE)
        if emb is None:
            log.warning("Voice check: embedding failed — accepting (fail-open)")
            return True
        score = _owner_score(emb, device_key)
        if score >= _spk_threshold[0]:
            log.info("Voice check PASS %.3f (%.1fs) [%s]: %r", score, secs, device_key, transcript[:60])
            return True
        _log_entry("system", f"Voice check: rejected non-owner (sim {score:.2f}) — {transcript[:40]!r}")
        log.info("Voice check REJECT %.3f (%.1fs) [%s]: %r", score, secs, device_key, transcript[:60])
        return False

    async def _handle_transcript(self, transcript: str):
        # Apply DTMF force flags (belt-and-suspenders alongside _send_mic —
        # that poll runs at least every 0.5s, almost always faster, so this
        # is only relevant on the unlikely chance a transcript raced it).
        if _dtmf_force_active[0]:
            _dtmf_force_active[0] = False
            if self._monitoring:
                self._monitoring = False   # Active supersedes Monitoring
                _log_entry("system", "Monitoring stopped")
            if not self._active:
                self._active = True
                _last_interaction[0] = __import__("time").time()
                _log_entry("system", "Voice activated")
        if _dtmf_force_silent[0]:
            _dtmf_force_silent[0] = False
            self._active = False
            _log_entry("system", "Voice silenced")

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

        # Noise hallucination filter: drop consonant-heavy gibberish from background
        # noise that slipped past the VAD. (Monitoring mode is exempt so you can
        # still diagnose what the transcriber produces.)
        if not self._monitoring and _is_likely_noise(transcript):
            log.debug("Dropped noise hallucination: %r", transcript)
            return

        normalized = transcript.strip().rstrip(".!?,").lower()

        # Drop punctuation-only transcripts (e.g. ".", "...") — nothing left after strip.
        # Without this they fall through every guard below (single-word check requires
        # len==1, but split() on "" gives zero words) and get routed to Zeebot as blanks.
        if not normalized:
            log.debug("Dropped punctuation-only transcript: %r", transcript)
            return

        # Owner-only gate — BEFORE wake/sleep/control phrases so that in
        # owner-only mode EVERYTHING requires the enrolled voice. Dashboard
        # HTTP buttons remain ungated fallbacks by design (they never reach
        # this method).
        if not await self._verify_speaker(transcript):
            return

        import time as _ti
        import functools as _ft

        def _busy_clear():
            """Clear busy flag and start 500 ms post-TTS silence cooldown."""
            self._busy.clear()
            _post_busy_until[0] = _ti.time() + 0.5

        # Wake confirmation pending — check affirmative response before anything else.
        if self._pending_wake_confirm:
            elapsed = _ti.time() - self._pending_wake_t
            self._pending_wake_confirm = False
            if elapsed > _WAKE_CONFIRM_TIMEOUT:
                log.info("Wake confirmation timed out (%.1fs) — mis-fire: %r", elapsed, transcript)
                _log_entry("system", "Wake mis-fire (timeout) — staying silent")
            elif normalized in _WAKE_CONFIRM_AFFIRM or _matches_phrase(normalized, WAKE_PHRASES):
                log.info("Wake confirmed — voice active")
                _log_entry("system", "Voice activated")
                self._busy.set()
                try:
                    if self._monitoring:
                        self._monitoring = False
                        _persist_monitoring[0] = False
                    self._active = True
                    _persist_active[0] = True
                    _last_interaction[0] = _ti.time()
                    await asyncio.get_running_loop().run_in_executor(
                        None, speak, "I'm listening.", self.alsa_output
                    )
                finally:
                    _busy_clear()
            else:
                log.info("Wake mis-fire — not confirmed: %r", transcript)
                _log_entry("system", f"Wake mis-fire — ignored ({transcript!r})")
            return

        # Wake phrase — always checked regardless of active/monitoring state.
        # Already active → simple acknowledgement. Silent or monitoring → ask for
        # confirmation before activating (avoids self-triggering off Zeebot's own TTS
        # or background chatter that happens to include the wake phrase) —
        # UNLESS owner-only mode already biometrically verified this transcript
        # came from the enrolled voice (via _verify_speaker above), in which
        # case the confirmation round-trip is redundant and skipped.
        if _matches_phrase(normalized, WAKE_PHRASES):
            if self._active:
                self._busy.set()
                try:
                    log.info("Wake phrase detected — already active")
                    await asyncio.get_running_loop().run_in_executor(
                        None, speak, "Yes, I'm here.", self.alsa_output
                    )
                finally:
                    _busy_clear()
                return
            if _owner_only[0] and _verification_available(_current_input_device_name()):
                log.info("Wake phrase detected — owner voice verified, activating immediately")
                _log_entry("system", "Voice activated (owner verified)")
                self._busy.set()
                try:
                    if self._monitoring:
                        self._monitoring = False
                        _persist_monitoring[0] = False
                    self._active = True
                    _persist_active[0] = True
                    _last_interaction[0] = _ti.time()
                    await asyncio.get_running_loop().run_in_executor(
                        None, speak, "I'm listening.", self.alsa_output
                    )
                finally:
                    _busy_clear()
                return
            self._pending_wake_confirm = True
            self._pending_wake_t = _ti.time()
            log.info("Wake phrase detected — requesting confirmation")
            self._busy.set()
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, speak, "Yes?", self.alsa_output
                )
            finally:
                _busy_clear()
            return

        # Sleep phrase — only meaningful when active
        if _matches_phrase(normalized, SLEEP_PHRASES):
            if self._active:
                self._active = False
                _persist_active[0] = False
                self._monitoring = False
                _persist_monitoring[0] = False
                log.info("Sleep phrase detected — going silent")
                _log_entry("system", "Voice silenced")
                self._busy.set()
                try:
                    await asyncio.get_running_loop().run_in_executor(
                        None, speak, f"Going silent now. Say {AGENT_NAME} wake up to resume.", self.alsa_output
                    )
                finally:
                    _busy_clear()
            return

        # Calibration — works in both modes (audio feedback either way)
        if normalized in CALIBRATE_PHRASES:
            log.info("Voice command: calibrate mic")
            asyncio.create_task(self._run_calibration())
            return

        # Monitoring toggle phrases — work regardless of active state.
        # Strip non-ASCII before matching: OpenAI Realtime sometimes garbles
        # "start" as foreign-script characters, e.g. "Tá monitoring." (Irish-
        # looking) or "Star monitoring།" (Tibetan punctuation attached to the
        # word, breaking the set lookup).  After stripping, "Star monitoring"
        # matches via "star" ≈ mishear of "start", and a bare "monitoring"
        # (≤3 words after strip) is unambiguous enough to treat as ON.
        import re as _re
        _ascii_norm = _re.sub(r'[^\x00-\x7F]+', '', normalized).strip()
        _norm_words = set(_normalize(_ascii_norm if _ascii_norm else normalized).split())
        _has_monitor = bool(_norm_words & {"monitor", "monitoring"})
        _start_words = {"start", "starting", "begin", "beginning", "on", "enable",
                        "activate", "please", "turn", "star"}  # "star" = common mishear of "start"
        _stop_words  = {"stop", "end", "off", "disable", "deactivate"}
        _monitor_on  = (_has_monitor
                        and not bool(_norm_words & _stop_words)
                        and (bool(_norm_words & _start_words) or len(_norm_words) <= 3))
        _monitor_off = (_has_monitor and bool(_norm_words & _stop_words))

        if _matches_phrase(normalized, MONITOR_ON_PHRASES) or _monitor_on:
            if not self._monitoring:
                self._monitoring = True
                _persist_monitoring[0] = True
                log.info("Voice command: monitoring ON")
                _log_entry("system", "Monitoring started.")
                await asyncio.get_running_loop().run_in_executor(
                    None, speak, "Monitoring started.", self.alsa_output
                )
            return
        if _matches_phrase(normalized, MONITOR_OFF_PHRASES) or _monitor_off:
            if self._monitoring:
                self._monitoring = False
                _persist_monitoring[0] = False
                log.info("Voice command: monitoring OFF")
                _log_entry("system", "Monitoring stopped.")
                await asyncio.get_running_loop().run_in_executor(
                    None, speak, "Monitoring stopped.", self.alsa_output
                )
            return

        # Owner-only mode toggles — already owner-gated by _verify_speaker above
        if _matches_phrase(normalized, OWNER_ONLY_ON_PHRASES):
            if not _owner_profiles:
                await asyncio.get_running_loop().run_in_executor(
                    None, speak,
                    "No voice profile enrolled yet. Use the web dashboard to enroll first.",
                    self.alsa_output)
                return
            if not _owner_only[0]:
                _owner_only[0] = True
                _save_voice_mode()
                log.info("Voice command: owner-only mode ON")
                _log_entry("system", "Owner-only mode on.")
                await asyncio.get_running_loop().run_in_executor(
                    None, speak, "Owner only mode on. I'll only listen to you.",
                    self.alsa_output)
                current_device = _current_input_device_name()
                if current_device not in _owner_profiles:
                    _log_entry("system", f"Note: no voice profile enrolled for {current_device!r} — "
                                          "accepting all speakers on it until you add one.")
                    await asyncio.get_running_loop().run_in_executor(
                        None, speak,
                        "Note: this microphone doesn't have a voice profile yet, so I'll accept "
                        "anyone on it until you add one.",
                        self.alsa_output)
            return
        if _matches_phrase(normalized, OWNER_ONLY_OFF_PHRASES):
            if _owner_only[0]:
                _owner_only[0] = False
                _save_voice_mode()
                log.info("Voice command: owner-only mode OFF")
                _log_entry("system", "Everyone mode — listening to all voices.")
                await asyncio.get_running_loop().run_in_executor(
                    None, speak, "Everyone mode. Listening to all voices.",
                    self.alsa_output)
            return

        # Monitoring-only mode: passively log captured segments (no Zeebot/TTS).
        # NOTE: this block is intentionally AFTER all control phrase checks so
        # that wake/sleep/monitoring-toggle phrases work even while monitoring.
        # Intentionally does NOT update _last_interaction — monitoring is passive
        # and must not prevent auto-sleep from firing.
        if self._monitoring:
            t = transcript.strip()
            if t:
                log.info("Monitor: %s", t)
                _log_entry("monitor", t)
            return

        # Language gate: drop non-EN/ZH (or off-whitelist) before routing to Zeebot.
        # Wake/sleep/monitoring phrases are already handled above and are exempt.
        if self._multilang in ("off", "en-zh"):
            if not _is_english_or_chinese(transcript):
                log.debug("Dropped non-EN/ZH (mode=%s): %r", self._multilang, transcript)
                return
        elif self._multilang == "whitelist":
            if not _is_in_multilang_whitelist(transcript):
                log.debug("Dropped off-whitelist: %r", transcript)
                return
        # "any" → all languages pass through

        # All other speech: only route to Zeebot when active
        if not self._active:
            log.debug("Silent mode — ignoring: %s", transcript)
            return

        # Short-word noise guard: single words under 9 characters that aren't
        # known commands are almost always noise hallucinations or foreign-word
        # hallucinations (e.g. "Esquece", "Senhores", "Legjeni") that slip past
        # the character-level language filter. langdetect is unreliable on single
        # short words so we handle them here instead.
        _SHORT_CMDS = {"ok", "okay", "yes", "no", "sure", "go", "stop", "wait",
                       "help", "hey", "hi", "bye", "right", "great", "thanks",
                       "please", "repeat", "exactly", "correct", "alright",
                       "好", "是", "否", "不", "对", "继续", "再来", "谢谢", "好的"}
        _nwords = normalized.split()
        if len(_nwords) == 1 and len(normalized) < 9 and normalized not in _SHORT_CMDS:
            log.info("Short noise guard — dropped single word: %r", transcript)
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
            _last_interaction[0] = _ti.time()  # reset auto-sleep timer on each query
            _log_entry("you", transcript)
            _log_entry("thinking", "Zeebot is thinking...")  # live counter shown on dashboard
            # Prefix tells Zeebot to ignore cron/heartbeat background context
            voice_msg = f"[voice] {transcript}"
            _think_task = asyncio.ensure_future(
                self.gw.ask(voice_msg, session_key=self.session_key)
            )
            _current_think_task[0] = _think_task
            try:
                reply = await asyncio.wait_for(
                    asyncio.shield(_think_task), timeout=AGENT_TIMEOUT_S
                )
            except asyncio.TimeoutError:
                _think_task.cancel()
                log.warning("Agent timed out after %ds — aborting", AGENT_TIMEOUT_S)
                _log_entry("system", f"Timed out after {AGENT_TIMEOUT_S}s.")
                return
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
                # Reject stale history: if it matches the last reply we already
                # delivered, the agent hasn't produced a new response yet.
                if reply and reply == _last_history_reply[0]:
                    log.warning("History returned same reply as last time — treating as stale")
                    reply = ""
                if not reply:
                    log.warning("History fallback also empty")
                    _log_entry("system", "No reply from Zeebot — please try again.")
                    await asyncio.get_running_loop().run_in_executor(
                        None, speak,
                        "Sorry, I didn't get a response. Please try again.",
                        self.alsa_output,
                    )
                    return
                _last_history_reply[0] = reply
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
            _log_entry("system", "Gateway error — please try again.")
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

            elif t == "input_audio_buffer.speech_started":
                self._capture_buf = list(self._preroll)
                self._capture_consumed = False

            elif t == "input_audio_buffer.speech_stopped":
                if self._capture_buf is not None and not self._capture_consumed:
                    import time as _tss
                    self._pending_segments.append(
                        (_tss.time(), b"".join(self._capture_buf)))
                self._capture_buf = None

            elif t not in (
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
            log.info("Session active — speak now (routed through %s / OpenClaw)", AGENT_NAME)

            import time as _rt
            with _audio_open_lock:
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
                    asyncio.create_task(self._idle_watcher(ws)),
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

def _switch_mic_stream(session, new_idx: int) -> None:
    """Live-switch the mic input stream to new_idx without restarting the daemon.

    Stops the current InputStream and opens a fresh one on new_idx.
    Resets _last_mic_cb immediately so _watch_mic_stream doesn't race us.
    No PortAudio reinit needed — only required when a device is physically
    plugged in after init (hot-plug), not when switching between existing devices.
    Runs in a background thread — safe to call from the HTTP handler.
    """
    import time as _t
    log.info("Switching mic stream → device %d", new_idx)

    # Reset the watchdog timestamp first so _watch_mic_stream doesn't
    # trigger its own recovery while we're mid-switch.
    _last_mic_cb[0] = _t.time()

    old = session._mic_stream_ref[0]
    session._mic_stream_ref[0] = None
    try:
        if old:
            old.stop()
            old.close()
    except Exception:
        pass

    _t.sleep(0.1)

    # One retry after a short backoff — opening a stream on a USB audio
    # interface (like the AIOC) right as another stream on the same
    # physical device is also initializing (e.g. Radio Mode's input switch
    # racing a Play Test loop's output-stream open) can transiently fail at
    # the CoreAudio level even though the device itself is fine; confirmed
    # live (PaMacCore err -10851 / PortAudio -9986) when both happened
    # within about a second of each other.
    for _attempt in (1, 2):
        try:
            with _audio_open_lock:
                new_stream = sd.InputStream(
                    samplerate=DEVICE_RATE, channels=CHANNELS, dtype="int16",
                    blocksize=DEVICE_BLOCKSIZE, callback=session._mic_cb,
                    device=new_idx,
                )
                new_stream.start()
            session._mic_stream_ref[0] = new_stream
            _selected_input_device[0] = new_idx
            _last_mic_cb[0] = _t.time()
            log.info("Mic stream switched to device %d OK%s", new_idx,
                     " (retry)" if _attempt == 2 else "")
            _log_entry("system", "Mic switched.")
            break
        except Exception as e:
            if _attempt == 1:
                log.warning("Mic stream switch failed (%s) — retrying once", e)
                _t.sleep(0.8)
                continue
            log.warning("Mic stream switch failed: %s", e)
            _log_entry("system", f"Mic switch failed: {e}")


def _dashboard_dynamic(sess) -> dict:
    """Compute the dashboard's dynamic fragments (state pill, nav, device
    panel, banner, conversation log). Shared by the full-page dashboard
    render and the /dashboard-frag polling endpoint, so periodic refresh
    can patch the DOM in place instead of a full page reload — a full
    reload every 3s re-fetched Google Fonts/CSS and repainted the entire
    page, producing a visible flash. Ported from Pi's identical v3.12.6
    fix, adapted to this file's own device_banner/hint/state logic (Mac's
    hover-hint UI uses a dedicated #hzone element instead of overwriting
    #dbanner's text, so that part of Pi's version doesn't apply here)."""
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
    elif _owner_only[0] and not _verification_available(_current_input_device_name()):
        device_banner = (
            '<div id="dbanner" style="background:#3a1500;border:1px solid #7a3000;'
            'color:#f59e0b;padding:3px 8px;border-radius:8px;">'
            f'&#9888; Owner-only requested but no voice profile for '
            f'{_current_input_device_name()!r} &mdash; accepting ALL speakers on it. '
            '<a href="/voice-enroll" style="color:#f59e0b">Enroll</a></div>'
        )
    else:
        # Always emit the wrapper (even empty) so /dashboard-frag polling
        # can reliably find and replace #dbanner via outerHTML — a bare ""
        # would mean this element doesn't exist in the DOM at all on a
        # load where no banner was active, breaking the very next refresh.
        device_banner = '<div id="dbanner"></div>'

    active = sess._active if sess else False
    monitoring = sess._monitoring if sess else False
    multilang  = sess._multilang if sess else "off"
    owner_only = _owner_only[0]
    enrolled   = _current_input_device_name() in _owner_profiles

    _ml_desc = {
        "off":       "Now: OFF — EN/ZH only, auto-sleep on. Click → EN/ZH mode (auto-sleep off)",
        "en-zh":     f"Now: EN/ZH — auto-sleep off. Click → Whitelist ({', '.join(MULTILANG_WHITELIST_LANGS[:4])}…)",
        "whitelist": f"Now: Whitelist — {', '.join(MULTILANG_WHITELIST_LANGS[:4])}… Click → Any language",
        "any":       "Now: Any language — auto-sleep off. Click → OFF",
    }
    _hints = {
        "wake":    "Activate voice — the agent will listen and respond",
        "sleep":   "Silence voice and stop monitoring. Say the wake phrase or press Wake to resume" if monitoring else "Silence voice. Say the wake phrase or press Wake to resume",
        "monitor": "Now: Monitoring ON. Click → stop monitoring" if monitoring else "Now: OFF. Click → start passive monitoring (transcribes without routing to agent)",
        "multilang": _ml_desc.get(multilang, "Toggle multi-language mode"),
        "ownermode": ("Now: Owner-only — only the enrolled voice is obeyed. Click → listen to everyone" if owner_only
                      else "Now: Everyone. Click → owner-only (only your enrolled voice is obeyed)" if enrolled
                      else "Enroll a voice profile first (Calibrate → Voice ID)"),
        "reset":   "Clear the conversation log (does not affect the agent's memory)",
        "restart": "Restart the RealTimeTalk daemon (reconnects OpenAI and gateway)",
        "gwreset": "Drop and reconnect the gateway WebSocket without restarting",
        "refresh": "Reload the dashboard now",
    }

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
            rows += f'<div class="zeebot">{ts_span}<b>{AGENT_NAME}:</b> {e["text"]}</div>'
        elif e["role"] == "monitor":
            rows += f'<div class="mon">{ts_span}{e["text"]}</div>'
        elif e["role"] == "thinking":
            ep  = e.get("epoch", 0.0)
            dur = thinking_dur.get(ep)
            if dur is None:
                rows += (f'<div class="thinking">{ts_span}'
                         f'{AGENT_NAME} is thinking... '
                         f'<span class="tctr" data-start="{ep:.3f}">0</span>s'
                         f' &nbsp;<a href="/interrupt" class="irupt">✕ Interrupt</a></div>')
        else:
            rows += f'<div class="sys">{ts_span}{e["text"]}</div>'

    _ds = _get_device_status()
    _voice_lbl = ("Owner-only" if owner_only else "Everyone") + \
                 ("" if enrolled else " (not enrolled)")
    device_panel = (
        f'<div id="dp">'
        f'&#127908; {_ds["mic"]} &ensp;'
        f'&#128266; {_ds["speaker_name"]} &middot; Vol {_ds["spk_vol"]} &middot; SW {_ds["sw_pct"]}% &ensp;'
        f'Gate {_ds["gate"]} &middot; Gain {_ds["gain"]}x'
        f' &ensp;&#128100; {_voice_lbl}'
        f'</div>'
    )

    paused   = _paused_speech[0] is not None
    speaking = _is_speaking[0]
    thinking = _current_think_task[0] is not None
    sleeping = _is_sleeping[0]
    state = ("SPEAKING" if speaking
             else "THINKING" if thinking
             else "PAUSED" if paused
             else "MONITORING" if monitoring
             else "ACTIVE" if active
             else "SLEEPING" if sleeping
             else "SILENT")
    _sc = {"ACTIVE":("#0d2818","#34d399"),"SILENT":("#141d2b","#64748b"),
           "THINKING":("#1c1304","#f59e0b"),"SPEAKING":("#031a10","#2dd4bf"),
           "PAUSED":("#150d2e","#a5b4fc"),"MONITORING":("#071a2e","#60a5fa"),
           "SLEEPING":("#1a1205","#78716c"),
           }.get(state, ("#141d2b","#64748b"))
    state_pill_style = f"background:{_sc[0]};color:{_sc[1]};border-color:{_sc[1]};"
    speaking_banner = (
        f'<div class="speaking">&#128266; {AGENT_NAME} is speaking&hellip;'
        ' &nbsp;<a href="/interrupt" class="irupt">&#10005; Stop</a></div>'
        if speaking else
        '<div class="speaking">&#9646;&#9646; Paused'
        ' &nbsp;<a href="/continue" class="cont">&#9654; Continue</a></div>'
        if paused else ""
    )

    nav_html = (
        f'<a href="/wake" class="btn" data-hint="{_hints["wake"]}">&#9889; Wake</a>'
        f'<a href="/sleep" class="btn" data-hint="{_hints["sleep"]}">&#9790; Sleep</a>'
        f'<a href="/monitor" class="btn {"on" if monitoring else ""}" data-hint="{_hints["monitor"]}">&#9678; {"Monitor On" if monitoring else "Monitor"}</a>'
        f'<a href="/multilang" class="btn {"on" if multilang != "off" else ""}" data-hint="{_hints["multilang"]}">&#8853; {multilang.upper() if multilang != "off" else "Multi-lang"}</a>'
        f'<a href="/ownermode" class="btn {"on" if owner_only else ""}" data-hint="{_hints["ownermode"]}">&#128100; {"Owner Only" if owner_only else "Everyone"}</a>'
        f'<a href="/reset" class="btn danger" data-hint="{_hints["reset"]}">&#10006; Clear Log</a>'
        f'<a href="/restart" class="btn" data-hint="{_hints["restart"]}">&#8635; Restart</a>'
        f'<a href="/gateway-reset" class="btn danger" data-hint="{_hints["gwreset"]}">&#9888; Gateway Reset</a>'
        f'<a href="/dashboard" class="btn" data-hint="{_hints["refresh"]}">&#8635;</a>'
    )

    return {
        "device_banner":    device_banner,
        "device_panel":     device_panel,
        "state":            state,
        "state_pill_style": state_pill_style,
        "speaking_banner":  speaking_banner,
        "rows":             rows if rows else "<div class='sys'>No conversation yet</div>",
        "nav_html":         nav_html,
    }


def start_http_server(port: int, on_stop, session_ref: list, loop=None):
    """session_ref is a one-element list holding the current RealtimeSession (or None)."""
    def _html(handler, code: int, body: str):
        data = body.encode()
        handler.send_response(code)
        handler.send_header("Content-Type",   "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    def _send_json(handler, code: int, obj) -> None:
        # NOT named _json: dozens of other branches in do_GET below do
        # `import json as _json` as a local module alias — since Python
        # treats any name assigned anywhere in a function as local to the
        # WHOLE function, a helper called _json here gets shadowed by
        # those (unrelated, unreached) local imports and raises
        # UnboundLocalError at call time. Confirmed live.
        data = json.dumps(obj).encode()
        handler.send_response(code)
        handler.send_header("Content-Type",   "application/json")
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
                import time as _twk
                if _is_sleeping[0] and _wake_event[0] and loop:
                    # Waking from auto-sleep: bypass confirmation, activate immediately,
                    # stamp idle clock, then signal main() to reconnect.
                    _wake_activate[0] = True
                    _last_interaction[0] = _twk.time()
                    loop.call_soon_threadsafe(_wake_event[0].set)
                    log.info("HTTP wake — reconnecting from auto-sleep")
                elif sess:
                    sess._active = True
                    _persist_active[0] = True
                    sess._pending_wake_confirm = False  # HTTP wake bypasses confirmation
                    if sess._monitoring:
                        sess._monitoring = False
                        _persist_monitoring[0] = False
                        log.info("HTTP wake — exiting monitoring mode")
                    _last_interaction[0] = _twk.time()
                    log.info("HTTP wake")
                self.send_response(302)
                self.send_header("Location", "/log")
                self.end_headers()
            elif self.path == "/sleep":
                if sess and (sess._active or sess._monitoring):
                    sess._active = False
                    _persist_active[0] = False
                    sess._monitoring = False
                    _persist_monitoring[0] = False
                    log.info("HTTP sleep (active + monitoring cleared)")
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
                        _persist_monitoring[0] = True
                        sess._active = False  # ensure fully silent
                        _persist_active[0] = False
                        log.info("HTTP monitor START — capture-only")
                        _log_entry("system", "Monitoring only - capture display, silent")
                    elif not new_state and sess._monitoring:
                        sess._monitoring = False
                        _persist_monitoring[0] = False
                        log.info("HTTP monitor STOP")
                        _log_entry("system", "Monitoring stopped")
                elif _is_sleeping[0] and self.path != "/monitor/stop" and _wake_event[0] and loop:
                    # Sleeping: pre-arm monitoring and wake so the next session starts in it.
                    _pending_monitor_wake[0] = True
                    loop.call_soon_threadsafe(_wake_event[0].set)
                    log.info("HTTP monitor — waking from sleep into Monitoring")
                self.send_response(302)
                self.send_header("Location", "/dashboard")
                self.end_headers()
            elif self.path == "/multilang":
                # Cycle: off → en-zh → whitelist → any → off
                _MULTILANG_CYCLE = ("off", "en-zh", "whitelist", "any")
                _MULTILANG_LABELS = {
                    "off":       "OFF (EN/ZH, auto-sleep on)",
                    "en-zh":     "EN/ZH (auto-sleep suppressed)",
                    "whitelist": f"Whitelist ({', '.join(MULTILANG_WHITELIST_LANGS[:4])}…)",
                    "any":       "Any language",
                }
                if sess:
                    cur = sess._multilang if sess._multilang in _MULTILANG_CYCLE else "off"
                    nxt = _MULTILANG_CYCLE[(_MULTILANG_CYCLE.index(cur) + 1) % len(_MULTILANG_CYCLE)]
                    sess._multilang = nxt
                    _persist_multilang[0] = nxt
                    log.info("HTTP multilang: %s → %s", cur, nxt)
                    _log_entry("system", f"Multi-language: {_MULTILANG_LABELS[nxt]}")
                    if nxt == "off":
                        import time as _tms; _last_interaction[0] = _tms.time()
                self.send_response(302)
                self.send_header("Location", "/dashboard")
                self.end_headers()

            elif self.path in ("/ownermode", "/ownermode/on", "/ownermode/off"):
                want = (not _owner_only[0]) if self.path == "/ownermode" \
                       else self.path.endswith("/on")
                if want and not _owner_profiles:
                    _log_entry("system", "Cannot enable owner-only mode — no voice profile enrolled.")
                    log.info("HTTP ownermode: refused — not enrolled")
                elif want != _owner_only[0]:
                    _owner_only[0] = want
                    _save_voice_mode()
                    label = "Owner-only mode on." if want else "Everyone mode — listening to all voices."
                    log.info("HTTP ownermode: %s", "ON" if want else "OFF")
                    _log_entry("system", label)
                    _current_dev = _current_input_device_name()
                    if want and _current_dev not in _owner_profiles:
                        _log_entry("system", f"Note: no voice profile enrolled for {_current_dev!r} — "
                                              "accepting all speakers on it until you add one.")
                self.send_response(302)
                self.send_header("Location", "/dashboard")
                self.end_headers()

            elif self.path.startswith("/ownermode/threshold"):
                import json as _json, urllib.parse as _up
                qs  = _up.parse_qs(_up.urlparse(self.path).query)
                try:
                    val = float(qs.get("value", [_spk_threshold[0]])[0])
                except ValueError:
                    val = _spk_threshold[0]
                val = max(0.2, min(0.9, val))
                _spk_threshold[0] = val
                _save_voice_mode()
                log.info("Speaker threshold set to %.2f", val)
                resp = _json.dumps({"threshold": val}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            elif self.path.startswith("/voice-enroll/record"):
                import json as _json, urllib.parse as _up
                qs   = _up.parse_qs(_up.urlparse(self.path).query)
                slot = qs.get("slot", ["0"])[0]
                secs = max(2.0, min(10.0, float(qs.get("secs", ["5"])[0])))
                lang = qs.get("lang", ["en"])[0]
                if _get_spk_extractor() is None:
                    out = {"ok": False, "error": "sherpa-onnx or model unavailable"}
                elif _is_speaking[0]:
                    out = {"ok": False, "error": "Zeebot is speaking — try again"}
                else:
                    pcm  = _record_pcm_blocking(secs)
                    peak = int(np.max(np.abs(pcm))) if len(pcm) else 0
                    emb  = _compute_embedding(pcm, DEVICE_RATE)
                    if emb is None:
                        out = {"ok": False, "error": "embedding failed"}
                    elif peak < 500:
                        out = {"ok": False, "error": f"too quiet (peak {peak}) — speak closer to the mic"}
                    else:
                        _enroll_staging[slot] = {"embedding": emb.tolist(),
                                                 "secs": secs, "lang": lang}
                        out = {"ok": True, "slot": slot, "secs": secs, "peak": peak,
                               "staged": len(_enroll_staging)}
                resp = _json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            elif self.path == "/voice-enroll/save":
                import json as _json
                if len(_enroll_staging) < 3:
                    out = {"ok": False, "error": f"need 3 samples, have {len(_enroll_staging)}"}
                else:
                    device_key = _current_input_device_name()
                    samples = [{"lang": v["lang"], "prompt": k, "secs": v["secs"],
                                "embedding": v["embedding"]}
                               for k, v in sorted(_enroll_staging.items())]
                    _save_voice_profile(samples, device_key)
                    _enroll_staging.clear()
                    _log_entry("system", f"Voice profile enrolled for {device_key!r} (3 samples).")
                    log.info("Voice profile saved for %r: %d samples", device_key, len(samples))
                    out = {"ok": True, "samples": len(samples), "device": device_key}
                resp = _json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            elif self.path.startswith("/voice-enroll/radio-record"):
                # Dedicated Radio Voice Profile section (matches Pi's fixed
                # Mic/Radio layout) — records explicitly from the AIOC
                # regardless of whichever device is currently selected for
                # the main enrollment section above, so switching input
                # devices first isn't required. Independent staging area
                # (_enroll_staging_radio) so it can't collide with an
                # in-progress recording in the main section.
                import json as _json, urllib.parse as _up
                qs   = _up.parse_qs(_up.urlparse(self.path).query)
                slot = qs.get("slot", ["0"])[0]
                secs = max(2.0, min(10.0, float(qs.get("secs", ["5"])[0])))
                lang = qs.get("lang", ["en"])[0]
                _found = _radio.find_radio_audio_devices()
                if _get_spk_extractor() is None:
                    out = {"ok": False, "error": "sherpa-onnx or model unavailable"}
                elif not _found or _found[1] is None:
                    out = {"ok": False, "error": "no radio audio input found"}
                elif _is_speaking[0]:
                    out = {"ok": False, "error": "Zeebot is speaking — try again"}
                else:
                    pcm  = _record_pcm_blocking(secs, device=_found[1])
                    peak = int(np.max(np.abs(pcm))) if len(pcm) else 0
                    emb  = _compute_embedding(pcm, DEVICE_RATE)
                    if emb is None:
                        out = {"ok": False, "error": "embedding failed"}
                    elif peak < 500:
                        out = {"ok": False, "error": f"too quiet (peak {peak}) — transmit closer/louder"}
                    else:
                        _enroll_staging_radio[slot] = {"embedding": emb.tolist(),
                                                       "secs": secs, "lang": lang}
                        out = {"ok": True, "slot": slot, "secs": secs, "peak": peak,
                               "staged": len(_enroll_staging_radio)}
                resp = _json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            elif self.path == "/voice-enroll/radio-save":
                import json as _json
                _found = _radio.find_radio_audio_devices()
                if len(_enroll_staging_radio) < 3:
                    out = {"ok": False, "error": f"need 3 samples, have {len(_enroll_staging_radio)}"}
                elif not _found or _found[1] is None:
                    out = {"ok": False, "error": "no radio audio input found"}
                else:
                    device_key = sd.query_devices(_found[1])["name"]
                    samples = [{"lang": v["lang"], "prompt": k, "secs": v["secs"],
                                "embedding": v["embedding"]}
                               for k, v in sorted(_enroll_staging_radio.items())]
                    _save_voice_profile(samples, device_key)
                    _enroll_staging_radio.clear()
                    _log_entry("system", f"Radio voice profile enrolled for {device_key!r} (3 samples).")
                    log.info("Radio voice profile saved for %r: %d samples", device_key, len(samples))
                    out = {"ok": True, "samples": len(samples), "device": device_key}
                resp = _json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            elif self.path == "/voice-enroll/test":
                import json as _json
                device_key = _current_input_device_name()
                if not _verification_available(device_key):
                    out = {"ok": False, "error": f"no profile enrolled for {device_key!r} or model unavailable"}
                elif _is_speaking[0]:
                    out = {"ok": False, "error": "Zeebot is speaking — try again"}
                else:
                    pcm = _record_pcm_blocking(4.0)
                    emb = _compute_embedding(pcm, DEVICE_RATE)
                    if emb is None:
                        out = {"ok": False, "error": "embedding failed"}
                    else:
                        score = _owner_score(emb, device_key)
                        out = {"ok": True, "score": round(score, 3),
                               "threshold": _spk_threshold[0],
                               "pass": score >= _spk_threshold[0],
                               "device": device_key}
                resp = _json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            elif self.path.startswith("/voice-enroll/clear"):
                import json as _json, urllib.parse as _up
                qs = _up.parse_qs(_up.urlparse(self.path).query)
                device_key = qs.get("device", [None])[0] or _current_input_device_name()
                raw = _read_voice_profiles_raw()
                raw.pop(device_key, None)
                os.makedirs(os.path.dirname(VOICE_PROFILES_FILE), exist_ok=True)
                with open(VOICE_PROFILES_FILE, "w") as f:
                    json.dump(raw, f)
                _load_voice_profiles()
                _enroll_staging.clear()
                if _owner_only[0] and not _owner_profiles:
                    _owner_only[0] = False
                    _save_voice_mode()
                _log_entry("system", f"Voice profile cleared for {device_key!r}.")
                log.info("Voice profile cleared for %r", device_key)
                resp = _json.dumps({"ok": True, "device": device_key}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            elif self.path == "/voice-enroll":
                current_device = _current_input_device_name()
                enrolled = current_device in _owner_profiles
                model_ok = _get_spk_extractor() is not None
                enrolled_since = ""
                if enrolled and _owner_profiles[current_device].get("created"):
                    enrolled_since = datetime.datetime.fromtimestamp(
                        _owner_profiles[current_device]["created"]).strftime("%Y-%m-%d %H:%M")
                status_line = (
                    f"Profile enrolled {enrolled_since}" if enrolled
                    else "No profile enrolled for this device" if model_ok
                    else "Speaker model unavailable — install sherpa-onnx + model first")

                # Dedicated Radio Voice Profile section — matches Pi's fixed
                # Mic/Radio layout (see reference screenshot) rather than
                # only showing whatever device is currently selected.
                # Resolves and records from the AIOC directly regardless of
                # what's active in the section above, so switching input
                # devices on the Calibrate page first isn't required.
                _ve_radio_found = _radio.find_radio_audio_devices()
                _radio_in_name = None
                if _ve_radio_found and _ve_radio_found[1] is not None:
                    try:
                        _radio_in_name = sd.query_devices(_ve_radio_found[1])["name"]
                    except Exception:
                        _radio_in_name = None
                _is_current_radio = _radio_in_name is not None and current_device == _radio_in_name
                _mic_section_label = "Radio Voice Profile" if _is_current_radio else "Mic Voice Profile"
                _show_radio_section = _radio_in_name is not None and not _is_current_radio

                _radio_enrolled = _radio_in_name in _owner_profiles if _radio_in_name else False
                _radio_since = ""
                if _radio_enrolled and _owner_profiles[_radio_in_name].get("created"):
                    _radio_since = datetime.datetime.fromtimestamp(
                        _owner_profiles[_radio_in_name]["created"]).strftime("%Y-%m-%d %H:%M")
                _radio_status_line = (
                    f"Radio profile enrolled {_radio_since} — used automatically whenever Radio mode "
                    "is active. If missing, Owner-Only mode still works but accepts all speakers over radio."
                    if _radio_enrolled else
                    "No radio profile enrolled — used automatically whenever Radio mode is active. "
                    "If missing, Owner-Only mode still works but accepts all speakers over radio."
                )
                _radio_section = (f"""
<h3 style="margin-top:20px;">Radio Voice Profile</h3>
<p class="info">{_radio_status_line}</p>
<div class="card"><b>Sample 1 — English</b>
<p class="info">Read aloud: &ldquo;{AGENT_NAME} wake up. Please check my calendar and read me the news for today.&rdquo;</p>
<button onclick="recRadio(this,'1','en')">&#9210; Record 5s</button> <span id="rs1"></span></div>
<div class="card"><b>Sample 2 — Chinese</b>
<p class="info">Read aloud: &ldquo;{AGENT_NAME} 醒来。今天天气怎么样？请帮我看一下我的日程安排。&rdquo;</p>
<button onclick="recRadio(this,'2','zh')">&#9210; Record 5s</button> <span id="rs2"></span></div>
<div class="card"><b>Sample 3 — free speech</b>
<p class="info">Speak naturally for 5 seconds — mix English and Chinese if you like.</p>
<button onclick="recRadio(this,'3','mixed')">&#9210; Record 5s</button> <span id="rs3"></span></div>
<div class="card">
<button id="saveRadio" onclick="saveRadio()">&#128190; Save radio profile</button>
<button onclick="clearDevice('{_radio_in_name}')" style="border-color:var(--rd)">&#10006; Clear radio profile</button>
<div id="resultRadio" class="info"></div></div>""") if _show_radio_section else ""

                other_devices = sorted(d for d in _owner_profiles
                                        if d != current_device and d != _radio_in_name)
                other_rows = "".join(
                    f'<div class="card" style="display:flex;justify-content:space-between;'
                    f'align-items:center;"><span>{d}</span>'
                    f'<button onclick="clearDevice(\'{d}\')" style="border-color:var(--rd)">'
                    f'&#10006; Clear</button></div>'
                    for d in other_devices
                )
                other_section = (
                    f'<h3 style="margin-top:20px;">Other enrolled devices</h3>{other_rows}'
                    if other_devices else ""
                )
                body = f"""<!DOCTYPE html><html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Voice ID — RealTimeTalk</title>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{--bg:#07090f;--sf:#0d1119;--bd:#1a2535;--tx:#dde4ef;--mu:#5a7088;--you:#38bdf8;--gn:#34d399;--rd:#ef4444;--r:8px;}}
body{{font-family:system-ui,sans-serif;font-size:15px;background:var(--bg);color:var(--tx);padding:12px 16px;max-width:680px;}}
h3{{margin:8px 0}} .info{{color:var(--mu);font-size:13px;margin:4px 0}}
.card{{background:var(--sf);border:1px solid var(--bd);border-radius:var(--r);padding:12px;margin:10px 0}}
button{{padding:8px 14px;border:1px solid var(--bd);border-radius:var(--r);background:#121925;color:var(--tx);font-size:14px;cursor:pointer}}
button:disabled{{opacity:.4}} .ok{{color:var(--gn)}} .bad{{color:var(--rd)}}
a{{color:var(--you)}} .meter{{height:8px;background:#121925;border-radius:4px;overflow:hidden;margin:8px 0}}
.meter>div{{height:100%;width:0;background:var(--gn)}}
</style></head><body>
<h3>&#127908; Voice ID enrollment</h3>
<p class="info">{status_line} &middot; threshold {_spk_threshold[0]:.2f} &middot; <a href="/calibration">&larr; calibration</a></p>
<div class="meter"><div id="meter"></div></div>
<h3>{_mic_section_label}</h3>
<p class="info">Targets whichever input device is currently active — {current_device}.
Switch devices (Calibrate page) before recording to enroll a different one.</p>
<div class="card"><b>Sample 1 — English</b>
<p class="info">Read aloud: &ldquo;{AGENT_NAME} wake up. Please check my calendar and read me the news for today.&rdquo;</p>
<button onclick="rec(this,'1','en')">&#9210; Record 5s</button> <span id="s1"></span></div>
<div class="card"><b>Sample 2 — Chinese</b>
<p class="info">Read aloud: &ldquo;{AGENT_NAME} 醒来。今天天气怎么样？请帮我看一下我的日程安排。&rdquo;</p>
<button onclick="rec(this,'2','zh')">&#9210; Record 5s</button> <span id="s2"></span></div>
<div class="card"><b>Sample 3 — free speech</b>
<p class="info">Speak naturally for 5 seconds — mix English and Chinese if you like.</p>
<button onclick="rec(this,'3','mixed')">&#9210; Record 5s</button> <span id="s3"></span></div>
<div class="card">
<button id="save" onclick="save()">&#128190; Save profile for {current_device}</button>
<button onclick="test(this)">&#127897; Test my voice</button>
<button onclick="clearDevice('{current_device}')" style="border-color:var(--rd)">&#10006; Clear this device's profile</button>
<div id="result" class="info"></div></div>
{_radio_section}
{other_section}
<script>
const es = new EventSource('/levels');
es.onmessage = e => {{ const p = parseInt(e.data.split(',')[0]);
  document.getElementById('meter').style.width = Math.min(100, p/150) + '%'; }};
async function rec(btn, slot, lang) {{
  btn.disabled = true; const lbl = document.getElementById('s'+slot);
  let n = 5; lbl.textContent = 'Recording… speak now';
  const timer = setInterval(()=>{{ n--; if(n>0) lbl.textContent = 'Recording… '+n; }}, 1000);
  try {{
    const r = await fetch(`/voice-enroll/record?slot=${{slot}}&secs=5&lang=${{lang}}`);
    const j = await r.json();
    lbl.innerHTML = j.ok ? `<span class="ok">&#10004; captured (peak ${{j.peak}})</span>`
                         : `<span class="bad">&#10006; ${{j.error}}</span>`;
  }} finally {{ clearInterval(timer); btn.disabled = false; }}
}}
async function save() {{
  const r = await fetch('/voice-enroll/save'); const j = await r.json();
  document.getElementById('result').innerHTML = j.ok
    ? `<span class="ok">Profile saved for ${{j.device}}. You can enable Owner Only on the dashboard.</span>`
    : `<span class="bad">${{j.error}}</span>`;
}}
async function recRadio(btn, slot, lang) {{
  btn.disabled = true; const lbl = document.getElementById('rs'+slot);
  let n = 5; lbl.textContent = 'Recording… transmit now';
  const timer = setInterval(()=>{{ n--; if(n>0) lbl.textContent = 'Recording… '+n; }}, 1000);
  try {{
    const r = await fetch(`/voice-enroll/radio-record?slot=${{slot}}&secs=5&lang=${{lang}}`);
    const j = await r.json();
    lbl.innerHTML = j.ok ? `<span class="ok">&#10004; captured (peak ${{j.peak}})</span>`
                         : `<span class="bad">&#10006; ${{j.error}}</span>`;
  }} finally {{ clearInterval(timer); btn.disabled = false; }}
}}
async function saveRadio() {{
  const r = await fetch('/voice-enroll/radio-save'); const j = await r.json();
  document.getElementById('resultRadio').innerHTML = j.ok
    ? `<span class="ok">Radio profile saved for ${{j.device}}. You can enable Owner Only on the dashboard.</span>`
    : `<span class="bad">${{j.error}}</span>`;
}}
async function test(btn) {{
  btn.disabled = true;
  document.getElementById('result').textContent = 'Recording 4s — speak now…';
  try {{
    const r = await fetch('/voice-enroll/test'); const j = await r.json();
    document.getElementById('result').innerHTML = j.ok
      ? `[${{j.device}}] Similarity <b class="${{j.pass?'ok':'bad'}}">${{j.score}}</b> vs threshold ${{j.threshold}} — ${{j.pass?'PASS':'FAIL'}}`
      : `<span class="bad">${{j.error}}</span>`;
  }} finally {{ btn.disabled = false; }}
}}
async function clearDevice(device) {{
  if (!confirm(`Delete the enrolled voice profile for "${{device}}"?`)) return;
  await fetch(`/voice-enroll/clear?device=${{encodeURIComponent(device)}}`); location.reload();
}}
</script></body></html>"""
                _html(self, 200, body)

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
                _voice_current_device = _current_input_device_name()
                _voice_enrolled = _voice_current_device in _owner_profiles
                _voice_owner_only = _owner_only[0]
                _voice_id_btn = (
                    '<a href="/voice-enroll" style="padding:4px 11px;font-size:13px;'
                    'text-decoration:none;border:1px solid {c};border-radius:8px;'
                    'color:{c};background:{bg};" title="{title}">'
                    '&#127908; Voice ID{chk}</a>'
                ).format(
                    c="#dc2626" if _voice_owner_only and not _voice_enrolled
                      else "#34d399" if _voice_owner_only
                      else "#38bdf8" if _voice_enrolled
                      else "#334155",
                    bg="#3b0000" if _voice_owner_only and not _voice_enrolled
                       else "#021a0e" if _voice_owner_only
                       else "#051928" if _voice_enrolled
                       else "transparent",
                    title=f"Enroll or test the owner voice profile for {_voice_current_device} "
                          f"({len(_owner_profiles)} device(s) enrolled total)",
                    chk="&nbsp;&#10003;" if _voice_owner_only else "",
                )
                # Radio + Monitor render unconditionally (matches Pi exactly);
                # DTMF/EchoTest are gated on _radio_profile_active[0], also
                # matching Pi exactly — hardware presence alone doesn't show
                # them, only actually having Radio Mode on does.
                _radio_btn = (
                    '<button id="radiobtn" onclick="toggleRadioMode()" '
                    'style="padding:4px 11px;font-size:13px;'
                    + ('color:#ef4444;border-color:#ef4444;background:#1a0303;'
                       if _radio_profile_active[0] else
                       'color:#475569;border-color:#334155;')
                    + '" title="Radio Mode: listen + reply over the AIOC instead of the local mic/speaker">'
                    + '&#128246; Radio' + ('&nbsp;&#10003;' if _radio_profile_active[0] else '')
                    + '</button>'
                )
                _monitor_btn = (
                    '<button id="monitorbtn" onclick="toggleRadioMonitor()" '
                    'style="padding:4px 11px;font-size:13px;'
                    + ('color:#34d399;border-color:#34d399;background:#021a0e;'
                       if _radio_monitor_active[0] else
                       'color:#475569;border-color:#334155;')
                    + '" title="Live RX passthrough: radio audio in &#8594; speaker out">'
                    + '&#128266; Monitor' + ('&nbsp;&#10003;' if _radio_monitor_active[0] else '')
                    + '</button>'
                )
                _dtmf_btns = (
                    '<a href="/dtmf-monitor" style="padding:4px 11px;font-size:13px;'
                    'text-decoration:none;border:1px solid #334155;border-radius:8px;'
                    'color:#60a5fa;background:#071a2e;" title="DTMF signal monitor">'
                    '&#128225; DTMF Mon</a>'
                    '<a href="/dtmf-train" style="padding:4px 11px;font-size:13px;'
                    'text-decoration:none;border:1px solid #334155;border-radius:8px;'
                    'color:#f59e0b;background:#130e02;" title="Train DTMF profiles">'
                    '&#9881; DTMF Train</a>'
                    '<a href="/dtmf-retrain" style="padding:4px 11px;font-size:13px;'
                    'text-decoration:none;border:1px solid #334155;border-radius:8px;'
                    'color:#a78bfa;background:#0e0820;" title="Retrain specific digits">'
                    '&#8635; DTMF Retrain</a>'
                ) if _radio_profile_active[0] else ""
                _echotest_btn = (
                    '<button id="echotestbtn" onclick="toggleEchoTest()" '
                    'style="padding:4px 11px;font-size:13px;'
                    + ('color:#34d399;border-color:#34d399;background:#021a0e;'
                       if _echotest_active[0] else
                       'color:#475569;border-color:#334155;')
                    + '" title="Detect a radio transmission (squelch), record it, and transmit it '
                    + 'back on-air by keying PTT until playback finishes">'
                    + '&#9654; EchoTest' + ('&nbsp;&#10003;' if _echotest_active[0] else '')
                    + '</button>'
                ) if _radio_profile_active[0] else ""
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
  <a href="/dashboard" class="back" onclick="sessionStorage.removeItem('devExpanded')">&#8592; Dashboard</a>
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
  {_voice_id_btn}
  {_radio_btn}
  {_monitor_btn}
  {_echotest_btn}
  {_dtmf_btns}
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
// Snapshot of toggle state as rendered — Radio Mode can flip on its own
// (auto-enable/disable on AIOC plug/unplug) with no user click on this
// page, so upd()'s regular poll below also watches for a mismatch and
// reloads to pick up the buttons/sections that depend on it (Radio,
// Monitor, EchoTest, DTMF Mon/Train/Retrain, device panel).
let _lastRadioActive={str(_radio_profile_active[0]).lower()};
let _lastMonitorActive={str(_radio_monitor_active[0]).lower()};
let _lastEchotestActive={str(_echotest_active[0]).lower()};
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
  // Radio Mode can flip itself (AIOC plug/unplug auto-enable/disable) with
  // no click on this page — reload to pick up the buttons/sections that
  // depend on it once noticed, rather than requiring a manual refresh.
  if(d.radio_active!==_lastRadioActive || d.monitor_active!==_lastMonitorActive
     || d.echotest_active!==_lastEchotestActive){{
    location.reload();
  }}
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
/* --- Radio Mode toggle --- */
function toggleRadioMode(){{
  fetch('/radio-mode').then(r=>r.json()).then(d=>{{
    const b=document.getElementById('radiobtn');
    if(!b)return;
    if(d.active){{
      b.style.color='#ef4444';b.style.borderColor='#ef4444';b.style.background='#1a0303';
      b.innerHTML='&#128246; Radio&nbsp;&#10003;';
    }} else {{
      b.style.color='#475569';b.style.borderColor='#334155';b.style.background='';
      b.innerHTML='&#128246; Radio';
    }}
  }});
}}
/* --- EchoTest toggle --- */
function toggleEchoTest(){{
  fetch('/echotest').then(r=>r.json()).then(d=>{{
    const b=document.getElementById('echotestbtn');
    if(!b)return;
    if(d.active){{
      b.style.color='#34d399';b.style.borderColor='#34d399';b.style.background='#021a0e';
      b.innerHTML='&#9654; EchoTest&nbsp;&#10003;';
    }} else {{
      b.style.color='#475569';b.style.borderColor='#334155';b.style.background='';
      b.innerHTML='&#9654; EchoTest';
    }}
  }});
}}
/* --- Radio monitor toggle --- */
function toggleRadioMonitor(){{
  fetch('/radio-monitor').then(r=>r.json()).then(d=>{{
    const b=document.getElementById('monitorbtn');
    if(!b)return;
    if(d.active){{
      b.style.color='#34d399';b.style.borderColor='#34d399';b.style.background='#021a0e';
      b.innerHTML='&#128266; Monitor&nbsp;&#10003;';
    }} else {{
      b.style.color='#475569';b.style.borderColor='#334155';b.style.background='';
      b.innerHTML='&#128266; Monitor';
    }}
  }});
}}
/* --- Device selection --- */
let _devExpanded=false, _devTimer=null;
function toggleDevices(){{
  _devExpanded=!_devExpanded;
  // Persist across reloads (device-change / Radio Mode auto-refresh) —
  // only a manual collapse or leaving the Calibrate page clears this.
  if(_devExpanded) sessionStorage.setItem('devExpanded','1');
  else sessionStorage.removeItem('devExpanded');
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
    h+='<table class="snrtbl"><tr><th>Name</th><th>Card</th><th>State</th><th></th><th>&#128266; Monitor</th></tr>';
    (d.sinks||[]).forEach(s=>{{
      if(s.name.startsWith('rtt_agc')||s.name.includes('monitor')) return;
      const active=(s.name===d.default_sink);
      // Match by device NAME, not index — CoreAudio renumbers indices when
      // Bluetooth devices connect/disconnect, so the index isn't a stable
      // identity across page loads the way the name is.
      const monHere=d.monitor_active&&d.monitor_out===(s.desc||s.name);
      h+='<tr'+(active?' class="active-row"':'')+'>'
        +'<td>'+(s.desc||s.name)+(active?' <span style="color:#5f5">✓</span>':'')+'</td>'
        +'<td style="white-space:nowrap">'+(s.card?'card '+s.card:'BT')+'</td>'
        +'<td>'+(s.state==='SUSPENDED'?'Idle':s.state==='RUNNING'?'<span style="color:#5f5">Running</span>':s.state)+'</td>'
        +'<td><button class="use-btn'+(active?' active':'')+'"'
        +' data-dtype="sink" data-dname="'+encodeURIComponent(s.desc||s.name)+'"'
        +' onclick="setDevice(this.dataset.dtype,this.dataset.dname)"'
        +(active?' disabled':'')
        +'>'+(active?'Active':'Use')+'</button></td>'
        +'<td>'+(d.radio_available
          ?'<button class="use-btn'+(monHere?' active':'')+'" onclick="toggleMonitorFor(\\''+encodeURIComponent(s.desc||s.name)+'\\')">'
           +(monHere?'&#10003; On':'On')+'</button>'
          :'&mdash;')+'</td></tr>';
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
        +' data-dtype="source" data-dname="'+encodeURIComponent(s.desc||s.name)+'"'
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
function toggleMonitorFor(name){{
  fetch('/radio-monitor?out='+name).then(r=>r.json()).then(d=>{{
    loadDevices();
    const b=document.getElementById('monitorbtn');
    if(!b)return;
    if(d.active){{
      b.style.color='#34d399';b.style.borderColor='#34d399';b.style.background='#021a0e';
      b.innerHTML='&#128266; Monitor&nbsp;&#10003;';
    }} else {{
      b.style.color='#475569';b.style.borderColor='#334155';b.style.background='';
      b.innerHTML='&#128266; Monitor';
    }}
  }});
}}
function setDevice(type,name){{
  // name arrives already URI-encoded (see data-dname above) — don't
  // encode again here, that would double-encode it.
  const msg=document.getElementById('devmsg');
  msg.textContent=(type==='sink'?'Setting speaker':'Setting mic')+': '+decodeURIComponent(name)+' — restarting audio in 1s…';
  fetch('/device-set?type='+type+'&name='+name)
    .then(r=>r.json()).then(d=>{{
      msg.textContent=d.msg||'Done.';
      if(d.ok){{
        if(_devTimer){{ clearInterval(_devTimer); _devTimer=null; }}
        setTimeout(()=>location.reload(),4500);
      }} else msg.style.color='#f55';
    }}).catch(e=>{{msg.textContent='Error: '+e; msg.style.color='#f55';}});
}}
// Restore expanded state on load — survives any reload of this page
// (device switch, Radio Mode auto-refresh) until manually collapsed or
// the Dashboard link is used to leave.
if(sessionStorage.getItem('devExpanded')==='1'){{
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
                # Target priority mirrors Pi: Radio Mode transmitting (PTT'd,
                # on-air) beats Monitor (play to whatever device Monitor is
                # currently listening through) beats the plain local speaker.
                _loop_tx_radio = bool(_radio_profile_active[0] and _ptt_alive())
                _loop_radio_out = None
                if _loop_tx_radio:
                    _rf = _radio.find_radio_audio_devices()
                    if _rf and _rf[2] is not None:
                        _loop_radio_out = _rf[2]
                    else:
                        _loop_tx_radio = False
                if _loop_tx_radio:
                    _loop_out_dev = _loop_radio_out
                    _loop_via = "radio (PTT)"
                elif _radio_monitor_active[0] and _radio_monitor_out_dev[0] is not None:
                    _loop_out_dev = _radio_monitor_out_dev[0]
                    _loop_via = f"monitor ({_device_label(_loop_out_dev)})"
                else:
                    _loop_out_dev = _selected_output_device[0]
                    _loop_via = "speaker"

                def _loop(tx_radio=_loop_tx_radio, out_dev=_loop_out_dev):
                    import tempfile as _tf, time as _t2
                    # Render the test phrase once at full amplitude (gain applied live).
                    phrase = "This is an audio test. One, two, three, four, five."
                    parts  = []
                    mp3 = _tf.mktemp(suffix=".mp3")
                    if _openai_tts_to_mp3(strip_markdown(phrase), mp3):
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

                    try:
                        if tx_radio:
                            _ptt_key()
                            _t2.sleep(_ptt_prekey_s())
                        # One retry after a short backoff — opening this stream
                        # right as Radio Mode's own mic-input switch is also
                        # initializing on the same physical AIOC USB device can
                        # transiently fail at the CoreAudio level even though
                        # the device itself is fine (confirmed live).
                        # Construction + start both touch native PortAudio
                        # stream lifecycle, so both happen under the shared
                        # audio-open lock — but only briefly, not held for
                        # the loop's whole (possibly long) playback duration.
                        _stream = None
                        for _attempt in (1, 2):
                            try:
                                with _audio_open_lock:
                                    _stream = sd.OutputStream(samplerate=TTS_SAMPLE_RATE, device=out_dev,
                                                              channels=1, dtype='float32',
                                                              blocksize=1024, callback=_cb)
                                    _stream.start()
                                break
                            except Exception as e:
                                if _attempt == 1:
                                    log.warning("Test loop stream open failed (%s) — retrying once", e)
                                    _t2.sleep(0.8)
                                    continue
                                raise
                        try:
                            while _headset_cal_loop[0]:
                                _t2.sleep(0.05)
                        finally:
                            try: _stream.stop(); _stream.close()
                            except Exception: pass
                    except Exception as e:
                        log.error("Test loop error: %s", e)
                    finally:
                        if tx_radio:
                            _t2.sleep(_ptt_tail_s())
                            try: _ptt_release()
                            except Exception: pass
                import threading as _tloop
                _tloop.Thread(target=_loop, daemon=True).start()
                _html(self, 200, f"<p>Loop started via {_loop_via}.</p>")

            elif self.path == "/speaker-cal/loop-stop":
                _headset_cal_loop[0] = False
                _html(self, 200, "<p>Loop stopped.</p>")

            elif self.path == "/radio-mode":
                import json as _json
                sess = session_ref[0]
                if _radio_profile_active[0]:
                    _radio_profile_active[0] = False
                    # Deliberate manual disable — don't let the hotplug
                    # watcher immediately auto-re-enable it while the AIOC
                    # is still connected; only a fresh unplug/replug clears this.
                    _radio_auto_enable_suppressed[0] = True
                    active = False
                    prev = _radio_prev_input_device[0]
                    _radio_prev_input_device[0] = None
                    prev_out = _radio_prev_output_device[0]
                    _radio_prev_output_device[0] = None
                    if prev_out is not None:
                        _selected_output_device[0] = prev_out
                    if sess is not None and prev is not None:
                        threading.Thread(target=_switch_mic_stream, args=(sess, prev),
                                          daemon=True).start()
                    log.info("Radio Mode OFF — restoring input device %s, output device %s", prev, prev_out)
                else:
                    _found = _radio.find_radio_audio_devices()
                    if _found and _found[1] is not None:
                        _radio_prev_input_device[0] = _selected_input_device[0]
                        _radio_profile_active[0] = True
                        active = True
                        if _found[2] is not None:
                            _radio_prev_output_device[0] = _selected_output_device[0]
                            _selected_output_device[0] = _found[2]
                        if sess is not None:
                            threading.Thread(target=_switch_mic_stream, args=(sess, _found[1]),
                                              daemon=True).start()
                        log.info("Radio Mode ON — switching input to AIOC audio-in (#%d), "
                                 "output to AIOC audio-out (#%s)", _found[1], _found[2])
                    else:
                        active = False   # no radio connected — can't turn on
                resp = _json.dumps({"active": active}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            elif self.path == "/echotest":
                import json as _json
                if _echotest_active[0]:
                    _echotest_stop_flag[0] = True
                    _echotest_active[0] = False
                    log.info("EchoTest listener stopped")
                else:
                    _echotest_stop_flag[0] = False
                    threading.Thread(target=_echotest_listener, args=(_echotest_stop_flag,),
                                      daemon=True, name="echotest-listener").start()
                    _echotest_active[0] = True
                    log.info("EchoTest listener started")
                resp = _json.dumps({"active": _echotest_active[0]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            elif self.path.startswith("/radio-monitor"):
                import json as _json, urllib.parse as _up
                _qs = _up.parse_qs(_up.urlparse(self.path).query)
                # Target by device NAME, not index — CoreAudio renumbers indices
                # whenever a Bluetooth device connects/disconnects (confirmed:
                # Mac mini Speakers alone shifted from #4 to #7 across one
                # session), so a fixed index resolved at page-render time can
                # already be stale by the time this request lands. sounddevice
                # re-resolves a name string fresh against the live device list
                # at stream-open time, which sidesteps the whole problem —
                # verified: sd.query_devices('AW720H Chat', 'output') still
                # finds it correctly even after other devices' indices moved.
                _target = _qs.get("out", [None])[0]
                if _target is not None:
                    _target = _up.unquote(_target)
                if _target is not None:
                    # Per-device picker (Speakers table "Monitor" column): if this
                    # exact device is already the active target, toggle off;
                    # otherwise (re)start pointed at the newly-picked device.
                    if _radio_monitor_active[0] and _radio_monitor_out_dev[0] == _target:
                        _stop_radio_monitor()
                        active = False
                    else:
                        if _radio_monitor_active[0]:
                            _stop_radio_monitor()
                            import time as _t9
                            _t9.sleep(0.2)
                        active = _start_radio_monitor(out_dev=_target)
                elif _radio_monitor_active[0]:
                    _stop_radio_monitor()
                    active = False
                else:
                    active = _start_radio_monitor()
                resp = _json.dumps({"active": active,
                                     "out": (str(_radio_monitor_out_dev[0])
                                             if _radio_monitor_out_dev[0] is not None else None)}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            elif self.path in ("/dtmf-monitor", "/dtmf-train", "/dtmf-retrain"):
                _script  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dtmf_monitor.py")
                # Hardcoded venv path rather than deriving from sys.executable —
                # this handler runs inside the launchd-spawned daemon process,
                # whose sys.executable may not reflect what a plain `python3`
                # on the user's own shell PATH resolves to, and this same
                # string is shown to the user as a copy-pasteable command.
                _python  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "venv", "bin", "python3")
                _mode    = {"dtmf-monitor": "monitor", "dtmf-train": "train",
                            "dtmf-retrain": "retrain"}[self.path.lstrip("/")]
                _args    = {"monitor": "", "train": "--train", "retrain": "--retrain"}[_mode]
                _titles  = {"monitor": "DTMF Monitor", "train": "DTMF Train", "retrain": "DTMF Retrain"}
                _colors  = {"monitor": "#60a5fa", "train": "#f59e0b", "retrain": "#a78bfa"}
                _profiles = {}
                try:
                    with open(os.path.expanduser("~/.openclaw/workspace/rtt_dtmf_profiles.json")) as _pf:
                        _profiles = json.load(_pf)
                except Exception:
                    pass
                # macOS has no xterm — open the CLI tool in Terminal.app via
                # osascript instead (the launchd wrapper's DYLD_LIBRARY_PATH
                # env is needed for `hid`, and training/retrain need a real
                # tty for raw-keypress input, so run it through the login
                # shell rather than as a bare Popen with captured stdio).
                _cmd = f"DYLD_LIBRARY_PATH=/opt/homebrew/lib {_python} {_script} {_args}"
                _launched = False
                try:
                    _osa = ('tell application "Terminal" to do script '
                            + json.dumps(_cmd))
                    subprocess.Popen(["osascript", "-e", _osa])
                    _launched = True
                except Exception:
                    pass
                _n = len(_profiles)
                _prof_rows = "".join(
                    f"<tr><td style='padding:3px 10px;font-weight:bold'>{_d}</td>"
                    f"<td style='padding:3px 10px;'>{_p['row_hz']:.1f}</td>"
                    f"<td style='padding:3px 10px;'>{_p['col_hz']:.1f}</td>"
                    f"<td style='padding:3px 10px;'>{_p['samples']}</td></tr>"
                    for _d, _p in sorted(_profiles.items())) if _profiles else ""
                _prof_html = (f"<table style='border-collapse:collapse;font-size:13px;margin:10px 0;'>"
                              f"<tr><th>Digit</th><th>Row Hz</th><th>Col Hz</th><th>Samples</th></tr>"
                              f"{_prof_rows}</table>") if _prof_rows else "<p style='color:#475569'>No profiles trained yet.</p>"
                _cmd_map = {"monitor": _cmd,
                            "train":   _cmd,
                            "retrain": _cmd}
                _body = f"""<!DOCTYPE html><html><head><meta charset='utf-8'>
<title>{_titles[_mode]} — RealTimeTalk</title>
<style>body{{font-family:monospace;background:#07090f;color:#dde4ef;padding:20px;max-width:600px;}}
h2{{color:{_colors[_mode]};}} a{{color:#38bdf8;text-decoration:none;}}
table{{border:1px solid #1a2535;}} th{{background:#0d1119;padding:6px 12px;color:#64748b;}}
.nav{{display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap;}}
.nav a{{padding:5px 12px;border:1px solid #1a2535;border-radius:6px;font-size:13px;color:#64748b;}}
.nav a.active{{color:{_colors[_mode]};border-color:{_colors[_mode]};}}
.cmd{{background:#0d1119;border:1px solid #1a2535;padding:10px 14px;border-radius:6px;
      font-size:14px;color:#34d399;margin:10px 0;display:flex;align-items:center;gap:10px;}}
.copybtn{{padding:3px 10px;font-size:12px;border:1px solid #1a2535;border-radius:5px;
          background:#0d1119;color:#64748b;cursor:pointer;font-family:monospace;white-space:nowrap;}}
.copybtn:hover{{border-color:#34d399;color:#34d399;}}
.copybtn.copied{{color:#34d399;border-color:#34d399;}}</style></head><body>
<div class='nav'>
  <a href='/calibration'>&larr; Calibration</a>
  <a href='/dtmf-monitor' {'class="active"' if _mode=='monitor' else ''}>&#128225; Monitor</a>
  <a href='/dtmf-train'   {'class="active"' if _mode=='train'   else ''}>&#9881; Train</a>
  <a href='/dtmf-retrain' {'class="active"' if _mode=='retrain' else ''}>&#8635; Retrain</a>
</div>
<h2>{_titles[_mode]}</h2>
<p>Profiles: <b>{_n} digit(s) trained</b>  |  File: <code style='font-size:11px'>~/.openclaw/workspace/rtt_dtmf_profiles.json</code></p>
{_prof_html}
<hr style='border-color:#1a2535;margin:14px 0;'>
{'<p style="color:#34d399;">&#10003; Terminal.app launched</p>' if _launched else '<p style="color:#64748b;font-size:12px;">Could not open Terminal.app — run manually:</p>'}
<div class='cmd'><span id='cmd'>{_cmd_map[_mode]}</span><button class='copybtn' onclick='copyCmd()'>Copy</button></div>
<script>
function copyCmd(){{
  var t=document.getElementById('cmd').textContent;
  navigator.clipboard.writeText(t).then(function(){{
    var b=document.querySelector('.copybtn');
    b.textContent='Copied!';b.classList.add('copied');
    setTimeout(function(){{b.textContent='Copy';b.classList.remove('copied');}},1500);
  }});
}}
</script>
<p style='color:#475569;font-size:12px;margin-top:12px;'>
Restart daemon after training to reload profiles.</p>
</body></html>"""
                _enc = _body.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(_enc)))
                self.end_headers()
                self.wfile.write(_enc)

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
                    # Fresh subprocess query (shared, throttled helper) so
                    # PortAudio re-initialises and picks up hot-plug changes
                    # (plugged/unplugged mics/speakers) instead of relying
                    # on this process's own cache.
                    _devs = _query_devices_fresh() or sd.query_devices()
                    # Resolve the selected index by NAME against this fresh
                    # list rather than reusing the stored index directly —
                    # confirmed live: after a device disconnects, other
                    # devices can end up renumbered into its old array
                    # position, so blindly trusting the stale index marks
                    # the WRONG device as active instead of correctly
                    # showing that the original one is just gone.
                    _out_idx = _selected_output_device[0]
                    if _out_idx is not None:
                        try:
                            _out_name = sd.query_devices(_out_idx)["name"]
                            _out_idx = next((i for i, d in enumerate(_devs)
                                             if d["name"] == _out_name and d["max_output_channels"] > 0),
                                            None)
                        except Exception:
                            _out_idx = None
                    _in_idx = _selected_input_device[0]
                    if _in_idx is not None:
                        try:
                            _in_name = sd.query_devices(_in_idx)["name"]
                            _in_idx = next((i for i, d in enumerate(_devs)
                                            if d["name"] == _in_name and d["max_input_channels"] > 0),
                                           None)
                        except Exception:
                            _in_idx = None
                    if _out_idx is None:
                        for _i, _d in enumerate(_devs):
                            if _d["max_output_channels"] > 0 and _d["name"] not in INVALID_OUTPUT_DEVICES:
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
                        "monitor_active": _radio_monitor_active[0],
                        "monitor_out": (str(_radio_monitor_out_dev[0])
                                        if _radio_monitor_out_dev[0] is not None else None),
                        "radio_available": bool(_radio.find_radio_audio_devices()),
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
                    # Resolve by NAME, not a raw index — confirmed live: the
                    # Audio Devices table (built from a fresh subprocess
                    # query) showed a speaker at one index, but this
                    # handler's own in-process sd.query_devices() disagreed
                    # on what was at that same index, rejecting a device the
                    # UI had just shown as selectable. Two-step: validate the
                    # name genuinely exists via the fresh (subprocess) list —
                    # catches "device doesn't exist" correctly — then resolve
                    # the index actually usable for opening streams IN THIS
                    # PROCESS via _resolve_device_by_name (in-process). If
                    # the fresh check confirms it exists but this process's
                    # own cache still doesn't see it, that's the same
                    # staleness _watch_mic_stream's recovery already handles
                    # — reinit once and retry the in-process resolution.
                    # Backward-compatible: if dev_name still looks like a
                    # bare index (older cached page / manual URL), accept it
                    # directly instead.
                    _kind = "output" if dev_type == "sink" else "input"
                    _ch_key = "max_output_channels" if dev_type == "sink" else "max_input_channels"
                    _dev_idx = None
                    if dev_name.lstrip("-").isdigit():
                        _i = int(dev_name)
                        _devs = sd.query_devices()
                        if 0 <= _i < len(_devs) and _devs[_i].get(_ch_key, 0) > 0:
                            _dev_idx = _i
                            dev_name = _devs[_i]["name"]
                    else:
                        _fresh = _query_devices_fresh()
                        _exists = bool(_fresh) and any(
                            _d.get("name") == dev_name and _d.get(_ch_key, 0) > 0 for _d in _fresh)
                        if _exists:
                            _dev_idx = _resolve_device_by_name(dev_name, _kind)
                            if _dev_idx is None:
                                log.info("device-set: %r confirmed present but not in this "
                                         "process's cache — reinitializing PortAudio", dev_name)
                                try:
                                    with _audio_open_lock:
                                        sd._terminate()
                                        sd._initialize()
                                except Exception as exc:
                                    log.warning("device-set: PortAudio reinit failed: %s", exc)
                                _dev_idx = _resolve_device_by_name(dev_name, _kind)
                    if _dev_idx is None:
                        raise ValueError(f"Device {dev_name!r} not found or has no "
                                          f"{'output' if dev_type == 'sink' else 'input'} channels")
                    _dev_info = sd.query_devices(_dev_idx)
                    if dev_type == "sink":
                        if _dev_info["max_output_channels"] < 1:
                            raise ValueError(f"Device {_dev_idx} has no output channels")
                        # Hot-switch: stop any playing audio, update globals, apply cal.
                        try:
                            sd.stop()
                        except Exception:
                            pass
                        _selected_output_device[0] = _dev_idx
                        _known = _apply_device_cal(_dev_info["name"])
                        _save_device_prefs(output_name=_dev_info["name"])
                        log.info("HTTP device-set: output → %d %s (%s)",
                                 _dev_idx, _dev_info["name"],
                                 "calibrated" if _known else "new/unknown → minimum")
                        result["ok"]  = True
                        result["msg"] = (
                            f"Speaker set to {_dev_info['name']}. "
                            + ("Restored calibrated levels." if _known
                               else "New device — starting at safe minimum. Use Manual adjustment.")
                        )
                    elif dev_type == "source":
                        if _dev_info["max_input_channels"] < 1:
                            raise ValueError(f"Device {_dev_idx} has no input channels")
                        _selected_input_device[0] = _dev_idx
                        _save_device_prefs(input_name=_dev_info["name"])
                        log.info("HTTP device-set: input → %d %s", _dev_idx, _dev_info["name"])
                        sess = session_ref[0]
                        if sess is not None:
                            threading.Thread(
                                target=_switch_mic_stream,
                                args=(sess, _dev_idx),
                                daemon=True,
                            ).start()
                            result["msg"] = f"Mic set to {_dev_info['name']}."
                        elif _is_sleeping[0] and _wake_event[0] and loop:
                            # No live session to hot-swap onto (daemon is
                            # asleep) — the preference above is saved and
                            # WILL be picked up whenever the daemon next
                            # reconnects, but silently waiting for that with
                            # no live effect and no indication anything is
                            # pending reads as "the switch didn't work" (this
                            # exact confusion is why this branch exists —
                            # confirmed live). Reconnect so transcription
                            # comes back up (NOT _wake_activate — a device
                            # change should restore STT/Silent listening, not
                            # jump straight to Active; a genuine wake phrase
                            # afterward activates it the normal way).
                            import time as _tds
                            _last_interaction[0] = _tds.time()
                            loop.call_soon_threadsafe(_wake_event[0].set)
                            log.info("device-set: daemon was asleep — reconnecting to apply mic switch")
                            result["msg"] = f"Mic set to {_dev_info['name']}. Reconnecting to apply it…"
                        else:
                            result["msg"] = f"Mic set to {_dev_info['name']}."
                        result["ok"]  = True
                    else:
                        result["msg"] = "Missing type or name"
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

            elif self.path.startswith("/speak"):
                host = self.client_address[0] if self.client_address else ""
                if host not in ("127.0.0.1", "::1", "localhost"):
                    _send_json(self, 403, {"ok": False, "error": "local callers only"})
                    return
                parsed = urllib.parse.urlparse(self.path)
                params = urllib.parse.parse_qs(parsed.query)
                text = (params.get("text") or [""])[0].strip()
                if not text:
                    _send_json(self, 400, {"ok": False, "error": "missing text"})
                    return
                alsa = sess.alsa_output if sess else ALSA_OUTPUT
                _log_entry("zeebot", text)
                threading.Thread(target=speak, args=(text, alsa), daemon=True).start()
                log.info("HTTP speak — queued %d chars", len(text))
                _send_json(self, 200, {"ok": True, "queued": True, "chars": len(text)})

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
                d = _dashboard_dynamic(sess)
                state             = d["state"]
                state_pill_style  = d["state_pill_style"]
                device_panel      = d["device_panel"]
                device_banner     = d["device_banner"]
                speaking_banner   = d["speaking_banner"]
                rows              = d["rows"]
                nav_html          = d["nav_html"]
                _hint_calibrate   = "Open speaker & mic level calibration"
                body = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
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
.nav{{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:7px;}}
a.btn{{display:inline-flex;align-items:center;gap:3px;padding:5px 9px;border-radius:7px;font-family:'Outfit',sans-serif;font-size:12px;font-weight:500;color:var(--mu);background:var(--sf2);border:1px solid var(--bd);text-decoration:none;min-height:28px;white-space:nowrap;transition:background .12s,border-color .12s,color .12s;}}
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
.ts{{font-family:'JetBrains Mono',monospace;font-size:.75em;color:var(--mu);margin-right:4px;}}
a.irupt{{color:var(--rd);background:var(--rdb);border:1px solid var(--rd);border-radius:4px;padding:2px 8px;font-size:.82em;font-style:normal;text-decoration:none;margin-left:8px;}}
a.irupt:hover{{background:var(--rd);color:#fff;}}
a.cont{{color:var(--gn);background:var(--gnb);border:1px solid var(--gn);border-radius:4px;padding:2px 8px;font-size:.82em;font-style:normal;text-decoration:none;margin-left:8px;}}
a.cont:hover{{background:var(--gn);color:#000;}}
@media(max-width:520px){{body{{font-size:15px;}}#top{{padding:10px 12px 8px;}}a.btn{{padding:5px 8px;font-size:11px;}}#dp{{font-size:12px;}}#log{{padding:8px 10px;}}}}
@media(min-width:900px){{body{{font-size:17px;}}#top{{padding:14px 24px 10px;}}a.btn{{font-size:13px;padding:6px 12px;}}#dp{{font-size:13px;}}#log{{padding:14px 24px;}}}}
</style></head><body>
<div id="top">
<div class="hrow"><span class="brand">&#9679;&nbsp;RealTimeTalk</span><span class="spill" id="pill" style="{state_pill_style}">{state}</span><a href="/calibration" class="btn" style="margin-left:10px;" data-hint="{_hint_calibrate}">&#9999; Calibrate</a></div>
<div class="nav" id="navbar">{nav_html}</div>
{device_panel}{device_banner}<div id="hzone" style="min-height:28px;padding:5px 10px;border-radius:8px;background:var(--sf2);border:1px solid var(--bd);color:var(--mu);font-size:15px;opacity:0;transition:opacity .15s;pointer-events:none;">&nbsp;</div></div>
<div id="log">{speaking_banner}{rows}</div>
<script>
setInterval(function(){{
  var now=Date.now()/1000;
  document.querySelectorAll('.tctr').forEach(function(el){{
    el.textContent=Math.max(0,Math.floor(now-parseFloat(el.dataset.start)));
  }});
}},500);
(function(){{
  var hz=document.getElementById('hzone'),ht,hovering=false;
  function show(txt){{clearTimeout(ht);hz.textContent=txt;hz.style.opacity='1';}}
  function hide(){{ht=setTimeout(function(){{hz.style.opacity='0';}},60000);}}
  // Event delegation (not per-button listeners) so this keeps working
  // after _refresh() replaces #navbar's innerHTML every few seconds.
  document.addEventListener('mouseover',function(e){{
    var b=e.target.closest && e.target.closest('a[data-hint]');
    if(!b) return;
    hovering=true;
    show(b.dataset.hint);
  }});
  document.addEventListener('mouseout',function(e){{
    var b=e.target.closest && e.target.closest('a[data-hint]');
    if(!b||b.contains(e.relatedTarget)) return;
    hovering=false;
    hide();
  }});
  // Periodically patch just the dynamic pieces of the dashboard in place
  // instead of location.reload()'ing the whole document — a full reload
  // every few seconds re-fetched Google Fonts/CSS and repainted the
  // entire page, producing a visible flash. Fetching a small JSON
  // fragment and patching only the changed elements avoids that. Paused
  // while hovering a nav button so the hint tooltip (and the hovered
  // element itself) aren't yanked out from under the cursor mid-display.
  function _refresh(){{
    if(hovering) return;
    fetch('/dashboard-frag').then(function(r){{return r.json();}}).then(function(d){{
      var pill=document.getElementById('pill');
      if(pill){{pill.textContent=d.state;pill.setAttribute('style',d.state_pill_style);}}
      var nav=document.getElementById('navbar');
      if(nav) nav.innerHTML=d.nav_html;
      var dp=document.getElementById('dp');
      if(dp) dp.outerHTML=d.device_panel;
      var db=document.getElementById('dbanner');
      if(db) db.outerHTML=d.device_banner;
      var log=document.getElementById('log');
      if(log) log.innerHTML=d.speaking_banner+d.rows;
    }}).catch(function(){{}});
  }}
  setInterval(_refresh,3000);
}})();
</script>
</body></html>"""
                _html(self, 200, body)
            elif self.path == "/dashboard-frag":
                d = _dashboard_dynamic(sess)
                _frag = json.dumps(d).encode()
                self.send_response(200)
                self.send_header("Content-Type",   "application/json")
                self.send_header("Content-Length", str(len(_frag)))
                self.end_headers()
                self.wfile.write(_frag)
            elif self.path == "/status":
                sess = session_ref[0]
                active = sess._active if sess else False
                body = json.dumps({"status": "running", "voice": "active" if active else "silent",
                                   "owner_mode": "owner-only" if _owner_only[0] else "everyone",
                                   "enrolled_devices": sorted(_owner_profiles),
                                   "current_device": _current_input_device_name(),
                                   "spk_threshold": _spk_threshold[0]}).encode()
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
    _openai_tts_key[0] = openai_key
    _elevenlabs_tts_key[0] = load_elevenlabs_key()
    if not _elevenlabs_tts_key[0]:
        log.info("No ElevenLabs key configured — Chinese/mixed TTS will use OpenAI TTS")

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

    _wake_event[0] = asyncio.Event()
    _event_loop[0] = loop
    _last_interaction[0] = __import__("time").time()   # seed idle clock before first session

    session_ref: list = [None]
    start_http_server(http_port, lambda: loop.call_soon_threadsafe(stop_event.set), session_ref, loop=loop)
    _threading.Thread(target=_radio_hotplug_watcher, args=(session_ref,), daemon=True, name="radio-hotplug").start()
    _threading.Thread(target=_echotest_worker, daemon=True, name="echotest-worker").start()
    _threading.Thread(target=_radio_rx_tap_watchdog, daemon=True, name="radio-rx-tap-watchdog").start()
    _threading.Thread(target=_dtmf_listener, daemon=True, name="dtmf-radio").start()
    log.info("OpenClaw RealTimeTalk daemon starting — silent mode (say '%s wake up' to activate)", AGENT_NAME)

    # Restore sleep state persisted across daemon/service restarts (e.g. mic device change).
    if _load_sleep_state():
        _is_sleeping[0] = True
        _sleep_requested[0] = True
        log.info("Restored sleep state from disk — waiting for wake signal…")

    # Speaker verification: restore mode/threshold and all per-device profiles.
    _load_voice_mode()
    if _spk_threshold_cli[0] is not None:
        _spk_threshold[0] = _spk_threshold_cli[0]
    _load_voice_profiles()
    if not _HAVE_SHERPA or not os.path.exists(SPK_MODEL_PATH):
        log.info("Speaker verification: disabled — %s",
                 "sherpa-onnx not installed" if not _HAVE_SHERPA else "model file missing")
    else:
        log.info("Speaker verification: %s, %s, threshold %.2f",
                 "owner-only" if _owner_only[0] else "everyone mode",
                 f"{len(_owner_profiles)} device(s) enrolled" if _owner_profiles else "no profiles",
                 _spk_threshold[0])
        # Pre-warm the extractor so the first utterance isn't slow.
        _threading.Thread(target=_get_spk_extractor, daemon=True, name="spk-prewarm").start()

    while not stop_event.is_set():
        if _sleep_requested[0]:
            # Sleeping (auto-sleep, or restored from disk): wait for /wake before connecting.
            _sleep_requested[0] = False
            log.info("Sleeping. Waiting for /wake to reconnect…")
            _wake_event[0].clear()
            wake_task = asyncio.create_task(_wake_event[0].wait())
            stop_task = asyncio.create_task(stop_event.wait())
            done, pending = await asyncio.wait([wake_task, stop_task], return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            _is_sleeping[0] = False
            _save_sleep_state(False)
            if stop_event.is_set():
                break
            log.info("Wake received — reconnecting to OpenAI…")
            _log_entry("system", "Reconnecting…")

        session = RealtimeSession(
            api_key=openai_key, loop=loop, gw=gw,
            stop_event=stop_event,
            input_device=_selected_input_device[0], alsa_output=alsa_output,
            session_key=session_key,
        )
        if _wake_activate[0]:
            session._active = True
            _persist_active[0] = True
            _wake_activate[0] = False
            log.info("Wake-from-sleep: session started active (HTTP wake)")
        if _pending_monitor_wake[0]:
            session._monitoring = True
            _persist_monitoring[0] = True
            _pending_monitor_wake[0] = False
            log.info("Wake-from-sleep: session started in Monitoring (HTTP monitor)")
        session_ref[0] = session
        try:
            await session.run()
            log.info("Session ended.")
        except websockets.exceptions.ConnectionClosedError as e:
            log.warning("Realtime connection closed: %s", e)
        except Exception as e:
            log.error("Session error: %s", e)

        session_ref[0] = None

        if not _sleep_requested[0] and not stop_event.is_set():
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
    p.add_argument("--spk-threshold",   type=float, default=None,
                   help=f"Speaker-verification cosine threshold override (default: {SPK_THRESHOLD_DEFAULT})")
    p.add_argument("--agent-name",      type=str, default="Zeebot",
                   help="Agent name used in wake/sleep phrases and UI (default: Zeebot)")
    p.add_argument("--wake-phrase",     type=str, default=None,
                   help="Primary wake phrase override (default: '<name> wake up')")
    p.add_argument("--list-devices",    action="store_true",
                   help="Print available CoreAudio devices and exit")
    p.add_argument("--calibrate",       action="store_true",
                   help="Measure ambient noise and print recommended --mic-gate value, then exit")
    args = p.parse_args()

    # --- Agent name / wake phrase configuration ---
    _agent_name    = args.agent_name.strip()
    _agent_name_lc = _agent_name.lower()
    _wake_primary  = args.wake_phrase.strip().lower() if args.wake_phrase else f"{_agent_name_lc} wake up"

    AGENT_NAME                = _agent_name
    TRANSCRIPTION_PROMPT      = f"{_agent_name}."
    TRANSCRIPTION_PROMPT_NORM = _agent_name_lc

    WAKE_PHRASES  = {_wake_primary, "real time talk on", "real-time talk on", "realtimetalk on"}
    if args.wake_phrase:
        WAKE_PHRASES.add(f"{_agent_name_lc} wake up")  # keep name-derived phrase alongside custom one
    SLEEP_PHRASES = {f"{_agent_name_lc} go to sleep",
                     "real time talk off", "real-time talk off", "realtimetalk off"}

    _n = _agent_name_lc
    MONITOR_ON_PHRASES  = ({p for p in MONITOR_ON_PHRASES  if not p.startswith("zeebot ")} |
                           {f"{_n} start monitoring", f"{_n} monitor on", f"{_n} monitoring on",
                            f"{_n} start monitor",    f"{_n} begin monitoring"})
    MONITOR_OFF_PHRASES = ({p for p in MONITOR_OFF_PHRASES if not p.startswith("zeebot ")} |
                           {f"{_n} stop monitoring",  f"{_n} monitor off", f"{_n} monitoring off",
                            f"{_n} stop monitor",     f"{_n} end monitoring"})
    CONTINUE_PHRASES       = {p for p in CONTINUE_PHRASES       if p != "zeebot continue"}       | {f"{_n} continue"}
    OWNER_ONLY_ON_PHRASES  = {p for p in OWNER_ONLY_ON_PHRASES  if p != "zeebot only listen to me"}  | {f"{_n} only listen to me"}
    OWNER_ONLY_OFF_PHRASES = {p for p in OWNER_ONLY_OFF_PHRASES if p != "zeebot listen to everyone"} | {f"{_n} listen to everyone"}

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
    if args.spk_threshold is not None:
        _spk_threshold_cli[0] = max(0.2, min(0.9, args.spk_threshold))
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
