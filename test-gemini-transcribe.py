#!/usr/bin/env python3
"""
test-gemini-transcribe.py — standalone eval of gemini-3.5-transcribe-live
as a potential STT backup for RealTimeTalk (RTT).

Eval tooling ONLY, by design: this file never imports or touches
RealTimeTalk-daemon.py, so the production daemon is at zero risk while
testing. Production integration (a --stt-engine flag / failover) is a
separate, later task — the owner-only voice gate depends on OpenAI's
VAD event vocabulary (speech_started/speech_stopped), which Gemini's
transcription-only event shape did not historically provide.

Update 2026-09-14: google-genai 1.47.0's typed LiveConnectConfig cannot
represent the live-transcription model's setup fields, and it sends
response_modalities in a way the transcription model rejects. This script
therefore bypasses the SDK for the Gemini leg and speaks the raw v1alpha
WebSocket protocol directly. The OpenAI compare leg still uses the same
Realtime API WebSocket payload as the production daemon.

Two modes:
  default       Gemini-only smoke test — mic at 16 kHz straight into
                gemini-3.5-transcribe-live, interim + final transcripts,
                proactive reconnect just before Gemini's 10-minute
                session cap.
  --compare     Side-by-side vs OpenAI — one mic capture at 24 kHz fanned
                to both engines: OpenAI Realtime (same session.update
                payload as the production daemon) and Gemini (24 kHz
                chunks downsampled to 16 kHz). Finals print labeled with
                timestamps so latency/accuracy are visually comparable.
                At shutdown a scored summary prints: per-utterance
                end-of-speech→final latency, time-to-first-partial, and
                (with --reference) WER against a known transcript.

Scoring (compare mode):
  latency       end→final: VAD end (speech_stopped / ACTIVITY_END) to
                final transcript arrival — what the daemon pays before
                routing to the LLM. start→partial: speech start to first
                interim/delta — perceived responsiveness.
  WER           word error rate vs --reference (pure-python Levenshtein;
                no new deps). File tests are the repeatable case.
  --no-prompt   drop the production "Zeebot." prompt on the OpenAI leg —
                the prompt biases toward wake-word-only transcripts on
                non-wake audio (observed 2026-09-14/15), so run both ways
                to separate prompt bias from engine accuracy.
  --json PATH   dump per-utterance records + metrics for further analysis.

Both modes deliberately skip RTT's AGC / noise-gate / WebRTC front-end —
raw mic passthrough, so noisy-room tests measure the engines' own
robustness, not RTT's signal processing.

Usage:
    ./venv/bin/python test-gemini-transcribe.py --list-devices
    ./venv/bin/python test-gemini-transcribe.py                       # Gemini only
    ./venv/bin/python test-gemini-transcribe.py --compare             # vs OpenAI
    ./venv/bin/python test-gemini-transcribe.py --duration 120        # auto-stop after 2 min
    ./venv/bin/python test-gemini-transcribe.py --language "en-US,zh-CN"
    ./venv/bin/python test-gemini-transcribe.py --smart               # SMART vs VERBATIM
    ./venv/bin/python test-gemini-transcribe.py --vocab "OpenClaw,Zeebot,sherpa-onnx"
    ./venv/bin/python test-gemini-transcribe.py --input-device 2
    # Scored A/B: latency + WER vs a reference transcript
    ./venv/bin/python test-gemini-transcribe.py --compare --file /tmp/x.wav \
        --reference "the quick brown fox" --vocab "OpenClaw,Zeebot" --json /tmp/ab.json
    ./venv/bin/python test-gemini-transcribe.py --compare --duration 300 --no-prompt

Requires (eval-only deps; intentionally NOT in requirements.txt):
    ./venv/bin/pip install numpy sounddevice websockets

API keys are read from ~/.openclaw/openclaw.json at
talk.providers.gemini.apiKey (and talk.providers.openai.apiKey for
--compare) as plain strings, matching the existing openai/elevenlabs
entries. If the config ever migrates to SecretRef indirection, port the
daemon's _resolve_provider_api_key() logic — not copied here on purpose.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import time

import numpy as np
import sounddevice as sd
import websockets

OPENCLAW_CONFIG = os.path.expanduser("~/.openclaw/openclaw.json")

# Gemini live transcription — constants from the Gemini API docs and raw
# WebSocket experiments 2026-09-14. Model requires the v1alpha live
# endpoint and rejects response_modalities in the setup message.
GEMINI_MODEL        = "gemini-3.5-transcribe-live"
GEMINI_API_VERSION  = "v1alpha"
GEMINI_SAMPLE_RATE  = 16000          # raw 16-bit PCM mono, little-endian
GEMINI_BLOCKSIZE    = 1600           # 100 ms at 16 kHz
SESSION_RESET_SECS  = 9 * 60         # proactive reconnect before the 10-min cap
RECONNECT_DELAY     = 2.0

# OpenAI leg — copied verbatim from the production daemon so the compare
# baseline is exactly what RTT runs (RealTimeTalk-daemon.py lines 130-136,
# 1814, 5064-5091). Do not "improve" these without updating the daemon too.
OPENAI_WS_URL          = "wss://api.openai.com/v1/realtime?intent=transcription"
OPENAI_TRANSCRIBE_MODEL = "gpt-4o-transcribe"
TRANSCRIPTION_PROMPT   = "Zeebot."
OPENAI_SAMPLE_RATE     = 24000
OPENAI_TAIL_GRACE      = 5.0   # secs to keep the socket open after EOF so
                               # transcription.completed (trails item.done) lands
OPENAI_BLOCKSIZE       = 2400        # 100 ms at 24 kHz

# Compare-mode scoring. Records are appended per utterance by the two legs;
# print_summary() turns them into the latency/WER table at shutdown.
RESULTS = {"Gemini": [], "OpenAI": []}


def _wer(ref: str, hyp: str) -> float:
    """Word error rate via Levenshtein distance on word tokens (no deps)."""
    import re as _re
    rw = _re.findall(r"\w+", ref.lower(), _re.UNICODE)
    hw = _re.findall(r"\w+", hyp.lower(), _re.UNICODE)
    if not rw:
        return 0.0 if not hw else 1.0
    prev = list(range(len(hw) + 1))
    for i, r in enumerate(rw, 1):
        cur = [i] + [0] * len(hw)
        for j, h in enumerate(hw, 1):
            cur[j] = min(prev[j] + 1,          # deletion
                         cur[j - 1] + 1,       # insertion
                         prev[j - 1] + (r != h))  # substitution
        prev = cur
    return prev[-1] / len(rw)


def _pct(vals):
    """Median and p95 of a list of floats (seconds), or (None, None) if empty."""
    if not vals:
        return None, None
    s = sorted(vals)
    n = len(s)
    med = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0
    p95 = s[min(n - 1, int(round(0.95 * (n - 1))))]
    return med, p95


def _pair_utterances(g_rec, o_rec, max_gap: float = 2.5):
    """Greedy-match Gemini and OpenAI utterances by speech-start time so the
    table can show both engines' output for the same spoken utterance."""
    pairs, used_o = [], set()
    for g in sorted(g_rec, key=lambda r: r["t_start"] if r["t_start"] is not None else 1e18):
        best, best_dt = None, max_gap
        for j, o in enumerate(o_rec):
            if j in used_o or o["t_start"] is None or g["t_start"] is None:
                continue
            dt = abs(o["t_start"] - g["t_start"])
            if dt < best_dt:
                best, best_dt = j, dt
        used_o.add(best if best is not None else -1)
        pairs.append((g, o_rec[best] if best is not None else None))
    for j, o in enumerate(o_rec):
        if j not in used_o:
            pairs.append((None, o))
    return pairs


def print_summary(results, args):
    """Scored end-of-run table: latency percentiles + optional WER."""
    print(f"\n{_ts()} ── Compare summary " + "─" * 40)
    ref = (args.reference or "").strip()
    wer_by_engine = {}
    for eng in ("Gemini", "OpenAI"):
        recs = results.get(eng, [])
        if not recs:
            print(f"{eng:>7}: no finals captured")
            continue
        # start→final is the primary cross-engine latency: Gemini's final
        # typically arrives BEFORE its ACTIVITY_END (near-synchronous
        # transcription), so end→final is not comparable across engines.
        lag_final = [r["lag_start_to_final"] for r in recs
                     if r["lag_start_to_final"] is not None]
        lag_partial = [r["lag_start_to_partial"] for r in recs
                       if r["lag_start_to_partial"] is not None]
        med_f, p95_f = _pct(lag_final)
        med_p, p95_p = _pct(lag_partial)
        line = (f"{eng:>7}: {len(recs)} final(s) | "
                f"start→final  median {med_f:.2f}s / p95 {p95_f:.2f}s"
                if med_f is not None else
                f"{eng:>7}: {len(recs)} final(s) | start→final n/a (no VAD-start)")
        if med_p is not None:
            line += f" | start→partial median {med_p:.2f}s"
        print(line)
        if ref:
            joined = " ".join(r["text"] for r in recs).strip()
            w = _wer(ref, joined)
            wer_by_engine[eng] = w
            print(f"{'':>7}  WER vs reference: {w * 100:.1f}%   "
                  f"got: {joined[:90]!r}")
    if ref and not wer_by_engine:
        pass
    # Side-by-side rows (paired by speech-start proximity).
    pairs = _pair_utterances(results.get("Gemini", []), results.get("OpenAI", []))
    if pairs:
        print(f"\n{'':>7}  per-utterance (start→final lag in s):")
        for i, (g, o) in enumerate(pairs, 1):
            g_txt = (g or {}).get("text", "—")
            o_txt = (o or {}).get("text", "—")
            g_lag = (f"{g['lag_start_to_final']:.2f}" if g and g["lag_start_to_final"] is not None else "n/a")
            o_lag = (f"{o['lag_start_to_final']:.2f}" if o and o["lag_start_to_final"] is not None else "n/a")
            print(f"{'':>7}   #{i}  Gemini [{g_lag}] {g_txt}")
            print(f"{'':>7}       OpenAI [{o_lag}] {o_txt}")
    if getattr(args, "json", None):
        out = {"gemini": results.get("Gemini", []),
               "openai": results.get("OpenAI", []),
               "reference": ref or None,
               "wer": {k: v for k, v in wer_by_engine.items()}}
        try:
            with open(args.json, "w") as f:
                json.dump(out, f, indent=1, ensure_ascii=False)
            print(f"{_ts()} wrote {args.json}")
        except OSError as e:
            print(f"{_ts()} could not write {args.json}: {e}")


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def load_provider_key(name: str) -> str:
    """Read talk.providers.<name>.apiKey as a plain string from openclaw.json."""
    try:
        with open(OPENCLAW_CONFIG) as f:
            cfg = json.load(f)
    except FileNotFoundError:
        sys.exit(f"No config at {OPENCLAW_CONFIG} — cannot read {name} API key.")
    key = (((cfg.get("talk") or {}).get("providers") or {}).get(name) or {}).get("apiKey", "")
    if isinstance(key, str) and key.strip():
        return key.strip()
    sys.exit(f"Missing talk.providers.{name}.apiKey in {OPENCLAW_CONFIG}.\n"
             f"Add it as a plain string, e.g.: \"talk\": {{\"providers\": "
             f"{{\"{name}\": {{\"apiKey\": \"...\"}}}}}}")


def list_devices() -> None:
    print(sd.query_devices())
    print("\nInput devices:")
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            print(f"  [{i}] {d['name']}  (in:{d['max_input_channels']} "
                  f"@ {int(d['default_samplerate'])} Hz)")


# ── Shared mic fan-out ────────────────────────────────────────────────────────

class MicFanout:
    """One sounddevice InputStream, its callback fanning raw PCM chunks to any
    number of asyncio queues via loop.call_soon_threadsafe (same bridging
    pattern as the daemon's _mic_cb). No gain, no gate — raw passthrough."""

    def __init__(self, loop: asyncio.AbstractEventLoop, samplerate: int,
                 blocksize: int, device=None):
        self.loop = loop
        self.queues = []
        self.device = device
        self.samplerate = samplerate
        self.blocksize = blocksize
        self._stream = None

    def _cb(self, indata, frames, time_info, status):
        if status:
            print(f"{_ts()} [mic] {status}", file=sys.stderr)
        data = indata[:, 0].tobytes()          # int16 mono → bytes
        for q in self.queues:
            self.loop.call_soon_threadsafe(self._put, q, data)

    @staticmethod
    def _put(q, data):
        try:
            q.put_nowait(data)
        except asyncio.QueueFull:
            pass                              # same drop policy as the daemon

    def add_queue(self):
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self.queues.append(q)
        return q

    def start(self):
        self._stream = sd.InputStream(
            samplerate=self.samplerate, channels=1, dtype="int16",
            blocksize=self.blocksize, callback=self._cb, device=self.device)
        self._stream.start()

    def stop(self):
        if self._stream:
            try:
                self._stream.stop(); self._stream.close()
            except Exception:
                pass
            self._stream = None


def downsample_24k_to_16k(raw: np.ndarray) -> bytes:
    """24 kHz int16 → 16 kHz int16 via linear interpolation — the same
    technique as the daemon's internal 24k↔16k WebRTC round-trip (daemon lines
    4237-4241), reimplemented standalone rather than importing it."""
    n_16k = len(raw) * 2 // 3
    idx_src = np.linspace(0, len(raw) - 1, n_16k)
    s16 = np.interp(idx_src, np.arange(len(raw)),
                    raw.astype(np.float32)).astype(np.int16)
    return s16.tobytes()


def resample_int16(raw: np.ndarray, src_rate: int, dst_rate: int) -> bytes:
    """Generic int16 resample via linear interpolation."""
    if src_rate == dst_rate:
        return raw.astype(np.int16).tobytes()
    n_dst = int(len(raw) * dst_rate / src_rate)
    idx_src = np.linspace(0, len(raw) - 1, n_dst)
    s16 = np.interp(idx_src, np.arange(len(raw)),
                    raw.astype(np.float32)).astype(np.int16)
    return s16.tobytes()


# ── File feeder (repeatable test input) ───────────────────────────────────────

class FileFeeder:
    """Read a mono 16-bit WAV file and feed realtime PCM chunks into an
    asyncio queue, optionally resampling to the target rate.

    tail_secs of silence is appended after the file: OpenAI's server VAD
    only evaluates on incoming audio, so a file that ends at speech end
    never yields speech_stopped → no commit → no transcript. Real mics
    always have ambient tail; synthetic files don't."""

    def __init__(self, path: str, samplerate: int, blocksize: int,
                 tail_secs: float = 1.0):
        self.path = path
        self.samplerate = samplerate
        self.blocksize = blocksize
        self.tail_secs = tail_secs
        self.queues = []

    def add_queue(self):
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self.queues.append(q)
        return q

    async def run(self):
        import wave
        with wave.open(self.path, "rb") as wf:
            if wf.getsampwidth() != 2:
                raise ValueError(f"{self.path} must be 16-bit PCM")
            n_channels = wf.getnchannels()
            file_rate = wf.getframerate()
            raw = wf.readframes(wf.getnframes())
        # Convert to mono int16 if needed
        arr = np.frombuffer(raw, np.int16)
        if n_channels > 1:
            arr = arr.reshape(-1, n_channels).mean(axis=1).astype(np.int16)
        # Resample to target rate
        pcm = resample_int16(arr, file_rate, self.samplerate)
        # Chunk at target blocksize (100 ms)
        chunk_size = self.blocksize * 2  # int16 bytes
        sleep_per_chunk = self.blocksize / self.samplerate
        for i in range(0, len(pcm), chunk_size):
            chunk = pcm[i:i + chunk_size]
            for q in self.queues:
                try:
                    q.put_nowait(chunk)
                except asyncio.QueueFull:
                    pass
            await asyncio.sleep(sleep_per_chunk)
        # Trailing silence: lets server-side VAD close the final utterance
        # (evaluates on input only — see class docstring).
        if self.tail_secs > 0:
            silence = b"\x00" * chunk_size
            for _ in range(int(self.tail_secs / sleep_per_chunk)):
                for q in self.queues:
                    try:
                        q.put_nowait(silence)
                    except asyncio.QueueFull:
                        pass
                await asyncio.sleep(sleep_per_chunk)
        # End-of-stream sentinel for each queue
        for q in self.queues:
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass


# ── Gemini leg (raw WebSocket) ──────────────────────────────────────────────

def gemini_ws_uri(api_key: str) -> str:
    return (
        "wss://generativelanguage.googleapis.com/ws/"
        f"google.ai.generativelanguage.{GEMINI_API_VERSION}."
        "GenerativeService.BidiGenerateContent"
        f"?key={api_key}"
    )


def gemini_setup_payload(args):
    lang_codes = [c.strip() for c in (args.language or "").split(",") if c.strip()]
    vocab = [v.strip() for v in (args.vocab or "").split(",") if v.strip()]
    if len(vocab) > 100:
        print(f"{_ts()} note: {len(vocab)} vocab terms — docs suggest ≤100 for best results")
    setup = {"setup": {"model": f"models/{GEMINI_MODEL}"}}
    iat = {}
    if lang_codes:
        iat["language_codes"] = lang_codes
    if vocab:
        iat["custom_vocabulary"] = vocab
    if args.smart:
        iat["mode"] = "SMART"
    elif lang_codes or vocab:
        # If we pass any other transcription field, mode is required by the
        # API in practice; default to VERBATIM for command-style transcripts.
        iat["mode"] = "VERBATIM"
    if iat:
        setup["setup"]["input_audio_transcription"] = iat
    return setup


async def gemini_sender(ws, q: asyncio.Queue, reset_after: float,
                        shutdown: asyncio.Event) -> bool:
    """Drain mic chunks into the WebSocket. Returns True when the proactive
    9-minute reset fired (caller should reconnect), False on queue-end."""
    started = time.monotonic()
    last_send = time.monotonic()
    while True:
        # Short timeout so we never leave the server idle for more than ~100 ms
        # while waiting for the next mic chunk.
        try:
            chunk = await asyncio.wait_for(q.get(), timeout=0.1)
        except asyncio.TimeoutError:
            now = time.monotonic()
            if shutdown.is_set():
                # Graceful shutdown: signal end of stream and exit.
                try:
                    await ws.send(json.dumps({"realtime_input": {"audio_stream_end": True}}))
                except websockets.exceptions.ConnectionClosed:
                    pass
                return False
            if now - started > reset_after:
                await ws.send(json.dumps({"realtime_input": {"audio_stream_end": True}}))
                return True
            # If we haven't sent anything in a while, inject a silence
            # chunk as a keepalive (100 ms). This prevents the server from
            # closing an idle session before the mic queue primes.
            if now - last_send > 0.5:
                try:
                    await ws.send(json.dumps({
                        "realtime_input": {
                            "audio": {
                                "data": base64.b64encode(b"\x00" * 3200).decode(),
                                "mime_type": "audio/pcm;rate=16000",
                            }
                        }
                    }))
                except websockets.exceptions.ConnectionClosed:
                    return False
                last_send = now
            continue
        if chunk is None:                        # shutdown sentinel
            try:
                await ws.send(json.dumps({"realtime_input": {"audio_stream_end": True}}))
            except websockets.exceptions.ConnectionClosed:
                pass
            return False
        try:
            await ws.send(json.dumps({
                "realtime_input": {
                    "audio": {
                        "data": base64.b64encode(chunk).decode(),
                        "mime_type": "audio/pcm;rate=16000",
                    }
                }
            }))
        except websockets.exceptions.ConnectionClosed:
            return False
        last_send = time.monotonic()


async def gemini_receiver(ws, label: str, show_interim: bool,
                          records: list = None, ready: asyncio.Event = None):
    """Print interim (overwritten in place) and final transcripts.

    With `records`, also timestamps each utterance for the compare summary:
    t_start/t_end from voiceActivity ACTIVITY_START/END, t_first_partial from
    the first interim, t_final + lag_start_to_final from inputTranscription.
    `ready` is set when setupComplete arrives.
    """
    last_interim = ""
    cur = {"t_start": None, "t_end": None, "t_first_partial": None}
    while True:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=15.0)
        except asyncio.TimeoutError:
            continue
        except websockets.exceptions.ConnectionClosed as e:
            print(f"{_ts()} -- {label}: receiver connection closed: {e.code} {e.reason}")
            break
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            print(f"{_ts()} [{label}] non-JSON: {raw[:200]}")
            continue

        sc = msg.get("serverContent") or msg.get("server_content")
        if sc is None:
            # setupComplete and other top-level messages
            if msg.get("setupComplete"):
                print(f"{_ts()} -- {label}: setup complete")
                if ready is not None:
                    ready.set()
            continue

        if show_interim:
            interim = sc.get("interimInputTranscription") or sc.get("interim_input_transcription")
            if interim and interim.get("text"):
                txt = interim["text"]
                if cur["t_first_partial"] is None:
                    cur["t_first_partial"] = time.monotonic()
                sys.stdout.write("\r\x1b[2K\x1b[2m" + f"[{label} ·] {txt}" + "\x1b[0m")
                sys.stdout.flush()
                last_interim = txt

        final = sc.get("inputTranscription") or sc.get("input_transcription")
        if final and final.get("text"):
            now = time.monotonic()
            if show_interim and last_interim:
                sys.stdout.write("\r\x1b[2K")
            lag = (now - cur["t_end"]) if cur["t_end"] is not None else None
            sp = ((cur["t_first_partial"] - cur["t_start"])
                  if cur["t_first_partial"] is not None and cur["t_start"] is not None else None)
            sf = ((now - cur["t_start"])
                  if cur["t_start"] is not None else None)
            print(f"{_ts()} [{label}] {final['text']}")
            sys.stdout.flush()
            last_interim = ""
            if records is not None:
                records.append({"t_start": cur["t_start"], "t_end": cur["t_end"],
                                "t_first_partial": cur["t_first_partial"],
                                "t_final": now, "text": final["text"],
                                "lag_end_to_final": lag,
                                "lag_start_to_partial": sp,
                                "lag_start_to_final": sf})
            cur = {"t_start": None, "t_end": None, "t_first_partial": None}

        # Voice activity is interesting for production integration later.
        va = msg.get("voiceActivity") or msg.get("voice_activity")
        if va:
            vtype = va.get("type")
            if vtype == "ACTIVITY_START":
                cur["t_start"] = time.monotonic()
                cur["t_first_partial"] = None   # partials belong to this utterance
            elif vtype == "ACTIVITY_END":
                cur["t_end"] = time.monotonic()
            print(f"{_ts()} [{label}] VAD {vtype} @ {va.get('audioOffset') or va.get('audio_offset')}")


async def gemini_leg(api_key: str, args, q: asyncio.Queue,
                     label: str = "Gemini", show_interim: bool = True,
                     shutdown: asyncio.Event = None,
                     records: list = None, ready: asyncio.Event = None):
    """Reconnecting Gemini session loop over one shared mic queue.
    If shutdown event is set, the loop exits cleanly after the current
    session ends instead of reconnecting. `ready` is set when setupComplete
    arrives (the session can actually transcribe)."""
    shutdown = shutdown or asyncio.Event()
    session_no = 0
    while True:
        session_no += 1
        print(f"{_ts()} -- {label}: session {session_no} connecting "
              f"(model={GEMINI_MODEL}, reset every {SESSION_RESET_SECS // 60} min)")
        try:
            async with websockets.connect(gemini_ws_uri(api_key)) as ws:
                setup_json = json.dumps(gemini_setup_payload(args))
                await ws.send(setup_json)
                sender = asyncio.create_task(
                    gemini_sender(ws, q, SESSION_RESET_SECS, shutdown))
                receiver = asyncio.create_task(
                    gemini_receiver(ws, label, show_interim,
                                    records=records, ready=ready))
                fallback = None
                if ready is not None:
                    # setupComplete is unreliable for the transcription model
                    # (observed missing for >15 s on 2026-09-15 while the
                    # session transcribed fine) — treat connect+setup as
                    # ready after 2 s regardless.
                    async def _ready_fallback():
                        await asyncio.sleep(2.0)
                        if not ready.is_set():
                            print(f"{_ts()} -- {label}: setupComplete not "
                                  f"received; assuming ready")
                            ready.set()
                    fallback = asyncio.create_task(_ready_fallback())
                done, pending = await asyncio.wait(
                    [sender, receiver], return_when=asyncio.FIRST_COMPLETED)
                if fallback is not None:
                    fallback.cancel()
                reset_fired = (sender in done and not sender.cancelled()
                               and sender.exception() is None
                               and sender.result() is True)
                end_of_stream = (sender in done and not sender.cancelled()
                               and sender.exception() is None
                               and sender.result() is False)
                if end_of_stream:
                    # Graceful end-of-stream: give the receiver a few seconds
                    # to collect final transcripts before tearing down.
                    if receiver in pending:
                        try:
                            await asyncio.wait_for(receiver, timeout=5.0)
                        except asyncio.TimeoutError:
                            receiver.cancel()
                else:
                    for t in pending:
                        t.cancel()
                err = next((t.exception() for t in done
                            if not t.cancelled() and t.exception()), None)
                if err:
                    print(f"{_ts()} -- {label}: session error: {err}")
                if reset_fired:
                    print(f"{_ts()} -- {label}: proactive session reset "
                          f"(before 10-min cap) — reconnecting")
                # drain any audio buffered during the gap so the fresh
                # session doesn't get a stale burst
                while not q.empty():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        break
        except Exception as e:
            print(f"{_ts()} -- {label}: connect failed: {type(e).__name__}: {e}")
        if shutdown.is_set():
            break
        await asyncio.sleep(RECONNECT_DELAY)


# ── OpenAI leg (compare mode only) ────────────────────────────────────────────
# Minimal reproduction of the production RealtimeSession subset: same URL,
# same session.update payload (daemon lines 5072-5091), same transcript
# extraction (daemon lines 5022-5030). This IS the baseline being compared.

async def openai_leg(api_key: str, q: asyncio.Queue,
                     shutdown: asyncio.Event, label: str = "OpenAI",
                     prompt: str = TRANSCRIPTION_PROMPT,
                     records: list = None, ready: asyncio.Event = None,
                     debug: bool = False):
    while True:
        print(f"{_ts()} -- {label}: session connecting")
        try:
            async with websockets.connect(
                OPENAI_WS_URL,
                additional_headers={"Authorization": f"Bearer {api_key}"},
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
                                    **({"prompt": prompt} if prompt else {}),
                                },
                                "turn_detection": {
                                    "type": "server_vad",
                                    "threshold": 0.35,
                                    "prefix_padding_ms": 500,
                                    "silence_duration_ms": 700,
                                },
                            },
                        },
                    },
                }))
                print(f"{_ts()} -- {label}: session active"
                      + (f" (prompt: {prompt!r})" if prompt else " (no prompt)"))
                if ready is not None:
                    ready.set()

                async def sender():
                    while not shutdown.is_set():
                        try:
                            chunk = await asyncio.wait_for(q.get(), timeout=0.5)
                        except asyncio.TimeoutError:
                            continue
                        if chunk is None:
                            # EOF: keep the socket open a few seconds so the
                            # trailing transcription.completed (arrives after
                            # item.done) can land before teardown.
                            await asyncio.sleep(OPENAI_TAIL_GRACE)
                            return
                        await ws.send(json.dumps({
                            "type": "input_audio_buffer.append",
                            "audio": base64.b64encode(chunk).decode(),
                        }))

                async def receiver():
                    cur = {"t_start": None, "t_end": None, "t_first_partial": None}
                    try:
                        async for raw in ws:
                            msg = json.loads(raw)
                            t = msg.get("type", "")
                            if debug:
                                print(f"{_ts()} [{label}] event: {t}")
                            if t in ("conversation.item.done",
                                     "conversation.item.input_audio_transcription.completed"):
                                if debug:
                                    print(f"{_ts()} [{label}] raw: {raw[:400]}")
                                transcript = msg.get("transcript", "")
                                if not transcript:
                                    for c in msg.get("item", {}).get("content", []):
                                        if c.get("type") == "input_audio" and c.get("transcript"):
                                            transcript = c["transcript"]
                                            break
                                transcript = transcript.strip()
                                if transcript:
                                    now = time.monotonic()
                                    lag = ((now - cur["t_end"])
                                           if cur["t_end"] is not None else None)
                                    sp = ((cur["t_first_partial"] - cur["t_start"])
                                          if cur["t_first_partial"] is not None
                                          and cur["t_start"] is not None else None)
                                    sf = ((now - cur["t_start"])
                                          if cur["t_start"] is not None else None)
                                    print(f"{_ts()} [{label}] {transcript}")
                                    if records is not None:
                                        records.append({"t_start": cur["t_start"],
                                                        "t_end": cur["t_end"],
                                                        "t_first_partial": cur["t_first_partial"],
                                                        "t_final": now, "text": transcript,
                                                        "lag_end_to_final": lag,
                                                        "lag_start_to_partial": sp,
                                                        "lag_start_to_final": sf})
                                    cur = {"t_start": None, "t_end": None,
                                           "t_first_partial": None}
                            elif t == "input_audio_buffer.speech_started":
                                cur["t_start"] = time.monotonic()
                                cur["t_first_partial"] = None
                            elif t == "input_audio_buffer.speech_stopped":
                                cur["t_end"] = time.monotonic()
                            elif (t == "conversation.item.input_audio_transcription.delta"
                                  and cur["t_first_partial"] is None):
                                cur["t_first_partial"] = time.monotonic()
                            elif t == "error":
                                print(f"{_ts()} [{label}] error: {msg.get('error', msg)}")
                    except websockets.exceptions.ConnectionClosed as e:
                        print(f"{_ts()} -- {label}: receiver closed: {e.code} {e.reason}")

                s = asyncio.create_task(sender())
                r = asyncio.create_task(receiver())
                done, pending = await asyncio.wait(
                    [s, r], return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
        except Exception as e:
            print(f"{_ts()} -- {label}: connect failed: {e}")
        if shutdown.is_set():
            break
        await asyncio.sleep(RECONNECT_DELAY)


# ── Modes ─────────────────────────────────────────────────────────────────────

async def run_gemini_only(args):
    api_key = load_provider_key("gemini")
    loop = asyncio.get_running_loop()
    shutdown = asyncio.Event()
    feeder_task = None
    if args.file:
        feeder = FileFeeder(args.file, GEMINI_SAMPLE_RATE, GEMINI_BLOCKSIZE)
        q = feeder.add_queue()
        async def _feeder_done():
            try:
                await feeder.run()
            except asyncio.CancelledError:
                pass
            finally:
                shutdown.set()
        feeder_task = asyncio.create_task(_feeder_done())
        print(f"{_ts()} Gemini-only mode — file={args.file}, "
              f"model={GEMINI_MODEL}, raw passthrough.")
    else:
        mic = MicFanout(loop, GEMINI_SAMPLE_RATE, GEMINI_BLOCKSIZE, args.input_device)
        q = mic.add_queue()
        mic.start()
        print(f"{_ts()} Gemini-only mode — model {GEMINI_MODEL}, "
              f"mic {GEMINI_SAMPLE_RATE} Hz, raw passthrough (no AGC/gate). "
              f"Ctrl-C to stop.")
    leg = asyncio.create_task(gemini_leg(api_key, args, q, shutdown=shutdown))
    stop_at = time.monotonic() + args.duration if args.duration else None
    try:
        if stop_at:
            await asyncio.sleep(max(0.0, stop_at - time.monotonic()))
        else:
            await leg
    except KeyboardInterrupt:
        pass
    finally:
        shutdown.set()
        if feeder_task:
            feeder_task.cancel()
        else:
            q.put_nowait(None)          # tell sender to stream-end and exit
        await asyncio.gather(leg, return_exceptions=True)
        if feeder_task:
            await asyncio.gather(feeder_task, return_exceptions=True)
        if not args.file:
            mic.stop()
        print(f"\n{_ts()} stopped.")


async def run_compare(args):
    gemini_key = load_provider_key("gemini")
    openai_key = load_provider_key("openai")
    loop = asyncio.get_running_loop()
    shutdown = asyncio.Event()
    feeder = None
    mic = None
    if args.file:
        # Compare mode needs 24 kHz source; FileFeeder produces 24 kHz chunks
        # and we resample to 16 kHz for Gemini. Created now, started only
        # after both engines are ready (else OpenAI's ~2.5 s connect eats
        # the first file utterance).
        feeder = FileFeeder(args.file, OPENAI_SAMPLE_RATE, OPENAI_BLOCKSIZE)
        q_openai = feeder.add_queue()
        q_gemini = feeder.add_queue()
        print(f"{_ts()} Compare mode — file={args.file}, 24 kHz fanned to "
              f"OpenAI ({OPENAI_TRANSCRIBE_MODEL}) and Gemini ({GEMINI_MODEL}, "
              f"downsampled 24k→16k). Finals only, labeled.")
    else:
        mic = MicFanout(loop, OPENAI_SAMPLE_RATE, OPENAI_BLOCKSIZE, args.input_device)
        q_openai = mic.add_queue()
        q_gemini = mic.add_queue()
        print(f"{_ts()} Compare mode — one mic @ {OPENAI_SAMPLE_RATE} Hz fanned to "
              f"OpenAI ({OPENAI_TRANSCRIBE_MODEL}) and Gemini ({GEMINI_MODEL}, "
              f"downsampled 24k→16k). Finals only, labeled. Ctrl-C to stop.")
    stop_at = time.monotonic() + args.duration if args.duration else None

    # Gemini's 16 kHz leg needs a resampling shim around the shared queue.
    async def gemini_resampled_q(src: asyncio.Queue) -> asyncio.Queue:
        out: asyncio.Queue = asyncio.Queue(maxsize=200)
        async def pump():
            while True:
                chunk = await src.get()
                if chunk is None:
                    out.put_nowait(None)
                    return
                arr = np.frombuffer(chunk, np.int16)
                try:
                    out.put_nowait(downsample_24k_to_16k(arr))
                except asyncio.QueueFull:
                    pass
        return out, asyncio.create_task(pump())

    q_gemini16, pump_task = await gemini_resampled_q(q_gemini)
    feeder_task = None

    def _make_feeder_done():
        """Feeder wrapper: EOF → keep sessions open a few seconds so in-flight
        finals (OpenAI's transcription.completed trails item.done) can land,
        then flip shutdown."""
        async def _feeder_done():
            try:
                await feeder.run()
                await asyncio.sleep(4.0)
            except asyncio.CancelledError:
                pass
            finally:
                shutdown.set()
        return asyncio.create_task(_feeder_done())

    ready_openai, ready_gemini = asyncio.Event(), asyncio.Event()
    legs = [
        asyncio.create_task(openai_leg(
            openai_key, q_openai, shutdown,
            prompt="" if args.no_prompt else TRANSCRIPTION_PROMPT,
            records=RESULTS["OpenAI"], ready=ready_openai,
            debug=args.debug)),
        asyncio.create_task(gemini_leg(gemini_key, args, q_gemini16,
                                       show_interim=False, shutdown=shutdown,
                                       records=RESULTS["Gemini"],
                                       ready=ready_gemini)),
    ]
    # Hold audio until both engines can actually transcribe — the scored
    # comparison is unfair otherwise (engine that connects late misses audio).
    async def _both_ready():
        await asyncio.gather(ready_openai.wait(), ready_gemini.wait())
    try:
        await asyncio.wait_for(_both_ready(), timeout=15.0)
        if feeder is not None:
            feeder_task = _make_feeder_done()
        else:
            mic.start()
            print(f"{_ts()} both engines ready — speak now")
        if stop_at:
            await asyncio.sleep(max(0.0, stop_at - time.monotonic()))
        else:
            await asyncio.gather(*legs)
    except asyncio.TimeoutError:
        print(f"{_ts()} WARN: engine(s) not ready after 15 s "
              f"(openai={ready_openai.is_set()} gemini={ready_gemini.is_set()}) — "
              f"feeding audio anyway")
        if feeder is not None:
            feeder_task = _make_feeder_done()
        elif mic is not None:
            mic.start()
        await asyncio.gather(*legs, return_exceptions=True)
    except KeyboardInterrupt:
        pass
    finally:
        shutdown.set()
        q_openai.put_nowait(None)
        q_gemini.put_nowait(None)
        if feeder_task:
            feeder_task.cancel()
        await asyncio.gather(*legs, pump_task, return_exceptions=True)
        if feeder_task:
            await asyncio.gather(feeder_task, return_exceptions=True)
        if mic is not None:
            mic.stop()
        print_summary(RESULTS, args)
        print(f"\n{_ts()} stopped.")


def main():
    parser = argparse.ArgumentParser(
        description="Standalone eval: gemini-3.5-transcribe-live vs OpenAI "
                    "Realtime STT (never touches the production daemon).",
        epilog="Examples:\n"
               "  %(prog)s --list-devices\n"
               "  %(prog)s --compare --duration 300 --vocab 'OpenClaw,Zeebot'\n"
               "  %(prog)s --language 'en-US,zh-CN' --smart\n"
               "  %(prog)s --file /tmp/test_speech.wav --vocab 'OpenClaw,Zeebot'\n",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input-device", type=int, default=None,
                        help="CoreAudio input device index (default: system default)")
    parser.add_argument("--list-devices", action="store_true",
                        help="List audio devices and exit")
    parser.add_argument("--compare", action="store_true",
                        help="Side-by-side OpenAI vs Gemini (default: Gemini only)")
    parser.add_argument("--duration", type=float, default=None,
                        help="Stop after N seconds (default: run until Ctrl-C)")
    parser.add_argument("--language", default="",
                        help="BCP-47 language hints, comma-separated "
                             "(e.g. 'en-US,zh-CN'); empty = auto-detect")
    parser.add_argument("--smart", action="store_true",
                        help="SMART transcription mode (disfluency cleanup) "
                             "instead of VERBATIM")
    parser.add_argument("--vocab", default="",
                        help="Custom vocabulary biasing, comma-separated "
                             "(max 1000 terms, best results ≤100)")
    parser.add_argument("--file", default=None,
                        help="Stream a mono 16-bit WAV file instead of mic "
                             "(useful for repeatable tests)")
    parser.add_argument("--reference", default=None,
                        help="Known transcript for WER scoring in compare mode "
                             "(typically with --file)")
    parser.add_argument("--no-prompt", action="store_true",
                        help="Drop the production 'Zeebot.' prompt on the OpenAI "
                             "leg (it biases toward wake-word-only transcripts)")
    parser.add_argument("--json", default=None, metavar="PATH",
                        help="Write per-utterance records + metrics JSON to PATH")
    parser.add_argument("--debug", action="store_true",
                        help="Print every OpenAI-leg event type (diagnose silent legs)")
    args = parser.parse_args()

    if args.list_devices:
        list_devices()
        return

    try:
        if args.compare:
            asyncio.run(run_compare(args))
        else:
            asyncio.run(run_gemini_only(args))
    except KeyboardInterrupt:
        print(f"\n{_ts()} interrupted — stopped.")


if __name__ == "__main__":
    main()
