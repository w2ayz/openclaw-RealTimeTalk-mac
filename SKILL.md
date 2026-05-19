---
name: openclaw-realtimetalk-mac
description: >
  Headless always-on voice conversation daemon for macOS (Mac Mini variant of
  openclaw-RealTimeTalk). Captures mic via CoreAudio (sounddevice), streams to
  OpenAI Realtime API for STT, routes transcripts through the OpenClaw gateway,
  and synthesises replies with Edge TTS (with macOS `say` fallback).
  Voice activation: "Five wake up" / "Five go to sleep".
---

# RealTimeTalk Mac — Skill Guide

This is the Mac Mini fork of the Raspberry Pi
[openclaw-RealTimeTalk](https://github.com/w2ayz/openclaw-RealTimeTalk) skill.
Core architecture is unchanged: async WebSocket sessions, audio queues, HTTP
dashboard, voice commands. The platform layer (audio framework, TTS engine,
service manager) is swapped for Mac-native equivalents.

---

## Architecture

| Layer | Pi origin | Mac equivalent |
|---|---|---|
| Audio I/O | PipeWire / ALSA / `aplay` | CoreAudio via `sounddevice` |
| Mic device discovery | `pactl list sources` | `sounddevice.query_devices()` |
| Output device discovery | `pactl list sinks` | `sounddevice.query_devices()` |
| AGC | PipeWire WebRTC AGC virtual source | None — software gain/gate only |
| TTS | Piper binary | Edge TTS (primary) + macOS `say` (fallback) |
| Audio decoding | direct PCM from Piper | ffmpeg → 24 kHz mono int16 |
| Volume control | `pactl set-sink-volume` | `osascript -e 'set volume output volume'` |
| Service manager | systemd user service | launchd LaunchAgent |
| Logs | `journalctl --user-unit ...` | `/tmp/openclaw/realtimetalk.log` |

---

## What stays the same (unchanged from Pi)

These classes/functions are copied verbatim and depend only on stdlib +
websockets + numpy:

- `GatewayClient` — OpenClaw WebSocket chat routing (operator protocol v4)
- `RealtimeSession` — OpenAI Realtime API session: VAD config, audio send,
  transcription event handling, speech interrupt
- `AudioOutputBuffer` — thread-safe PCM ring buffer
- HTTP server + dashboard HTML (port 19000)
- Voice activation matcher (`_matches_phrase`, normalised exact + fuzzy)
- Text helpers: `_is_english_or_chinese`, `_to_simplified` (via zhconv),
  `_split_by_script`, `strip_markdown`, `_is_likely_noise`
- Config loaders: `load_openai_key`, `load_gateway_token`

---

## TTS pipeline

```python
# In speak() — for each (segment, lang) in _split_by_script(text):
1. _edge_tts_to_mp3(text, voice, /tmp/rtt_XXX.mp3, timeout=8s)
   └─ Edge TTS via node tts-converter.js
2. on timeout/failure → _say_fallback_to_aiff(text, lang, /tmp/rtt_XXX.aiff)
   └─ macOS `say -v {Samantha|Tingting} -o <out>`
3. _decode_to_pcm(<file>)
   └─ ffmpeg → 24 kHz mono int16 numpy array
4. concatenate PCM segments, apply software volume
5. sd.play(pcm, device=<output_device>, blocking=False)
6. poll mic level every 50 ms → sd.stop() on speech-interrupt
```

Voice choices:
- Edge: `en-US-AriaNeural`, `zh-CN-XiaoxiaoNeural`
- say: `Samantha` (en), `Tingting` (zh)

---

## OpenAI key requirement

The daemon reads `talk.providers.openai.apiKey` from `~/.openclaw/openclaw.json`.
The Realtime API requires the regular OpenAI provider (api_key mode), not the
`openai-codex` OAuth profile. Add this block to openclaw.json:

```json
"talk": {
  "providers": {
    "openai": { "apiKey": "sk-..." }
  }
}
```

The installer (`RealTimeTalk-install-mac.sh`) checks this precondition and
prints instructions if missing.

---

## LaunchAgent

`~/Library/LaunchAgents/ai.openclaw.realtimetalk.plist`:

- `Label`: `ai.openclaw.realtimetalk`
- `ProgramArguments`: venv-python + daemon path + flags
- `RunAtLoad: true` — starts at every login
- `KeepAlive`: restart on crash, not on clean exit
- `ThrottleInterval: 10` — wait 10 s between restarts
- `StandardOut/ErrorPath`: `/tmp/openclaw/realtimetalk.log`

Toggle via `RealTimeTalk-toggle.sh {start|stop|restart|status|log|devices}`,
or `launchctl bootstrap/bootout/kickstart gui/$UID/ai.openclaw.realtimetalk`.

---

## Pi → Mac code-path differences

Functions that are stubs/no-ops on Mac (referenced by HTTP routes but inert):

- `_agc_source_available()` → always False
- `_activate_agc_source()` → always False
- `_get_default_source()` / `_set_default_source()` → empty / False
- `_detect_headset()` → always False (use sounddevice device name heuristics)
- `_find_usb_speaker_sink()` → None
- `_safe_volume_new_sinks(pct)` → caps via `osascript`

Functions that work natively on Mac:

- `_list_audio_devices()` — CoreAudio enumeration
- `_get_system_volume()` / `_set_system_volume()` — osascript wrappers
- `_save_device_cal(device_name, sw_vol)` — keyed by device name string
- `_apply_device_cal(device_name)` — restores software volume on startup
- `run_speaker_calibration()` — sounddevice play + record, FFT SNR analysis
- `_update_service_*` — edit launchd plist via plistlib

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Daemon exits with "No OpenAI API key" | `talk.providers.openai.apiKey` missing in openclaw.json | Add the key (regular OpenAI API key, not OAuth) |
| `--list-devices` shows no inputs | No mic connected | Plug in a USB mic, pair Bluetooth, or enable iPhone Continuity |
| Bluetooth playback sounds compressed | macOS SCO mode (8 kHz) | Expected when BT mic+speaker on same device — use separate output |
| Daemon won't restart after edit | LaunchAgent throttle (10 s) | `launchctl kickstart -k gui/$UID/ai.openclaw.realtimetalk` |
| TTS plays silently | Output device volume zero | macOS system volume: F11/F12 or System Settings → Sound |
| Edge TTS always failing | Network down / Edge endpoint blocked | Daemon falls back to `say` — check `/tmp/openclaw/realtimetalk.log` for timeouts |
