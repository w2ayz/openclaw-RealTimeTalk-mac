# openclaw-RealTimeTalk-mac

Real-time voice conversations with the OpenClaw agent (Five), adapted from
[openclaw-RealTimeTalk](https://github.com/w2ayz/openclaw-RealTimeTalk) (Raspberry Pi)
to run on a Mac Mini.

```
Mic → OpenAI Realtime API (VAD + STT) → OpenClaw gateway → Five → Edge TTS → Speaker
```

A web dashboard on `http://localhost:19000/dashboard` exposes wake/sleep, a
live conversation log, mic level meter, and basic device controls.

---

## What's different from the Pi version

| Concern              | Pi (origin)                       | Mac (this repo)                              |
|----------------------|-----------------------------------|----------------------------------------------|
| Audio framework      | PipeWire + ALSA                   | CoreAudio (via `sounddevice`)                |
| TTS                  | Piper (offline binary)            | Edge TTS (primary), macOS `say` (fallback)   |
| Service manager      | systemd user service              | launchd LaunchAgent                          |
| Audio playback       | `aplay` subprocess                | `sounddevice` (PCM into CoreAudio)           |
| Volume control       | `pactl set-sink-volume`           | `osascript -e 'set volume output volume N'`  |
| Device discovery     | `pactl list`, `pw-cli`            | `sounddevice.query_devices()`                |
| AGC                  | PipeWire WebRTC AGC virtual src   | Removed (CoreAudio handles input gain)       |
| Microphone           | USB mic typical                   | **External required** — Mac Mini has no mic  |

The async architecture (GatewayClient, RealtimeSession, AudioOutputBuffer,
HTTP server, voice-command matcher, EN/ZH language splitter) is reused
verbatim from the Pi version.

---

## Prerequisites

| Dependency                  | Install                                    |
|-----------------------------|--------------------------------------------|
| [OpenClaw](https://openclaw.ai) gateway running | platform requirement (`openclaw gateway start`) |
| `openai.apiKey` in `~/.openclaw/openclaw.json` | regular OpenAI API key, **not** the openai-codex OAuth profile |
| [Edge TTS skill](https://github.com/w2ayz/openclaw-edge-tts) | `~/.openclaw/workspace/skills/edge-tts/` |
| Homebrew + portaudio + ffmpeg + node | `brew install portaudio ffmpeg node`     |
| Python 3.9+                 | system Python or `brew install python`     |
| A microphone                | USB mic, Bluetooth headset, or iPhone via Continuity Camera |

### Adding the OpenAI API key

The daemon reads the key from `talk.providers.openai.apiKey` in
`~/.openclaw/openclaw.json`. Add this block (or merge it into your existing
`talk` block):

```json
"talk": {
  "providers": {
    "openai": { "apiKey": "sk-..." }
  }
}
```

The Realtime API requires the standard OpenAI provider with `api_key` mode.
The `openai-codex` OAuth profile shipped by OpenClaw will NOT work for this
endpoint.

---

## Installation

```bash
git clone https://github.com/w2ayz/openclaw-RealTimeTalk-mac.git ~/openclaw-RealTimeTalk-mac
bash ~/openclaw-RealTimeTalk-mac/RealTimeTalk-install-mac.sh
```

The installer:
1. `brew install`s portaudio, ffmpeg, node (skipped if already present)
2. Creates a Python venv at `./venv` and installs `sounddevice`, `websockets`, `numpy`, `zhconv`
3. Verifies `openai.apiKey` is set in `openclaw.json` (exits with instructions if missing)
4. Lists CoreAudio devices and prompts you for input + output device indices
5. Writes the LaunchAgent plist to `~/Library/LaunchAgents/ai.openclaw.realtimetalk.plist`
6. Loads the agent (boots at every login)

Then open `http://localhost:19000/dashboard`.

---

## Control

```bash
bash RealTimeTalk-toggle.sh start     # load LaunchAgent
bash RealTimeTalk-toggle.sh stop      # unload
bash RealTimeTalk-toggle.sh restart   # bounce
bash RealTimeTalk-toggle.sh status    # launchctl status
bash RealTimeTalk-toggle.sh log       # tail /tmp/openclaw/realtimetalk.log
bash RealTimeTalk-toggle.sh devices   # list CoreAudio inputs/outputs
```

Or via HTTP:

- `GET http://localhost:19000/dashboard` — UI
- `GET http://localhost:19000/wake` — activate
- `GET http://localhost:19000/sleep` — deactivate
- `GET http://localhost:19000/restart` — restart daemon

Or via voice (when active): say "Five wake up", "Five go to sleep",
"calibrate mic", etc.

---

## Microphone selection

The Mac Mini has no built-in microphone. The daemon enumerates all
CoreAudio inputs via `sounddevice.query_devices()`. Common options:

- **USB microphone** — most reliable; full-duplex full-bandwidth
- **Bluetooth headset (AirPods, etc.)** — macOS may switch to SCO mode
  (8 kHz) when the mic is active, degrading playback while you speak.
  The daemon detects this and surfaces a warning in the dashboard.
- **iPhone via Continuity Camera** — appears as a CoreAudio input when
  paired with the same Apple ID. Good mic quality, requires iPhone nearby.

Select the device by passing `--input-device <idx>` (find indices via
`bash RealTimeTalk-toggle.sh devices`) — the installer prompts you for
this on first run.

---

## Configuration

The daemon flags are documented inline via `--help`:

```bash
./venv/bin/python3 RealTimeTalk-daemon.py --help
```

Key flags:

| Flag               | Default     | Purpose                                       |
|--------------------|-------------|-----------------------------------------------|
| `--input-device N` | system def  | sounddevice index for mic                     |
| `--output-device N`| system def  | sounddevice index for speaker                 |
| `--mic-gain F`     | `3.0`       | software gain multiplier                      |
| `--mic-gate N`     | `300`       | noise gate threshold (pre-gain peak)          |
| `--http-port N`    | `19000`     | dashboard HTTP port                           |
| `--list-devices`   | flag        | print devices and exit                        |
| `--calibrate`      | flag        | measure ambient noise → recommend `--mic-gate`|

---

## How it works (signal chain)

```
Mic (CoreAudio)
    └─ sounddevice InputStream  (24 kHz mono int16, 100 ms blocks)
        └─ asyncio.Queue
            └─ RealtimeSession.send_audio()  (forward to OpenAI WS)
                └─ OpenAI gpt-4o-transcribe  (server-side VAD + STT)
                    └─ transcript event
                        ├─ Wake/sleep / command matcher  (skip if matched)
                        └─ GatewayClient.ask()  (OpenClaw chat.send → agent.wait)
                            └─ Five's reply text
                                └─ speak()
                                    ├─ _split_by_script()  (en / zh)
                                    ├─ Edge TTS  (per segment)  ← timeout 8s → say fallback
                                    ├─ ffmpeg → 24 kHz mono PCM int16
                                    ├─ software volume attenuation
                                    └─ sounddevice.play()  (CoreAudio output)
                                        └─ Speech-interrupt polling:
                                            mic peak × 15 → sd.stop()
```

End-to-end latency: ~4–12 seconds, dominated by VAD silence window (1.1s)
and Five's reasoning time.

---

## Limitations

- **No built-in Mac Mini mic** — external input required
- **Edge TTS needs internet** — first-byte ~500 ms; falls back to `say` on
  failure/timeout
- **System-wide volume** — macOS scripting can only set the master output
  volume, not per-device
- **No WebRTC AGC** — CoreAudio handles input gain at the driver level, but
  USB mics with hot mic levels may need `--mic-gain` adjustment

---

## License

MIT
