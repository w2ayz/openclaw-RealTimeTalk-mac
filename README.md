# openclaw-RealTimeTalk-mac

Real-time voice conversations with your OpenClaw agent (default name
**Zeebot**, configurable — see `--agent-name`), adapted from
[openclaw-RealTimeTalk](https://github.com/w2ayz/openclaw-RealTimeTalk) (Raspberry Pi)
to run on a Mac Mini.

```
Mic → OpenAI Realtime API or Gemini 3.5 Transcribe Live (VAD + STT)
       ↓
    OpenClaw gateway → Agent → TTS → Speaker
```

TTS chain: ElevenLabs (`eleven_v3`, primary) → Edge TTS (free, no key, native
zh-CN / en-US neural voices — first fallback) → OpenAI TTS (`tts-1-hd`) → macOS
`say` (offline last resort).

A web dashboard on `http://localhost:19000/dashboard` exposes wake/sleep, a
live conversation log, mic level meter, and basic device controls.

---

## What's different from the Pi version

| Concern              | Pi (origin)                       | Mac (this repo)                              |
|----------------------|-----------------------------------|----------------------------------------------|
| Audio framework      | PipeWire + ALSA                   | CoreAudio (via `sounddevice`)                |
| TTS                  | Piper (offline binary)            | ElevenLabs → Edge TTS → OpenAI TTS → `say`   |
| Service manager      | systemd user service              | launchd LaunchAgent                          |
| Audio playback       | `aplay` subprocess                | `sounddevice` (PCM into CoreAudio)           |
| Volume control       | `pactl set-sink-volume`           | `osascript -e 'set volume output volume N'`  |
| Device discovery     | `pactl list`, `pw-cli`            | `sounddevice.query_devices()`                |
| AGC                  | PipeWire WebRTC AGC virtual src   | Removed (CoreAudio handles input gain)       |
| Microphone           | USB mic typical                   | **External required** — Mac Mini has no mic  |
| Radio Mode audio     | PipeWire loopback module          | Shared `sounddevice.InputStream` tap (see "Radio Mode" below) |

### Radio Mode

Optional — requires an [AIOC](https://github.com/skuep/AIOC) (All-In-One-Cable)
USB ham-radio dongle. Digirig Mobile is not supported on Mac: its CM108
codec reports a generic product string that collides with unrelated USB
mics, and disambiguating it needs USB topology correlation this port
doesn't implement (Pi's fix is ALSA/Linux-only).

Toggle it from the Calibrate page — auto-enables when the AIOC is plugged
in, auto-disables on unplug, and a manual toggle sticks until the next
unplug/replug cycle. Monitor (live RX passthrough), EchoTest, and DTMF
Mon/Train/Retrain all appear on the Calibrate page once Radio Mode is on
(EchoTest/DTMF) or the AIOC is detected (Monitor).

The async architecture (GatewayClient, RealtimeSession, AudioOutputBuffer,
HTTP server, voice-command matcher, EN/ZH language splitter) is reused
verbatim from the Pi version.

---

## Prerequisites

| Dependency                  | Install                                    |
|-----------------------------|--------------------------------------------|
| [OpenClaw](https://openclaw.ai) gateway running | platform requirement (`openclaw gateway start`) |
| STT provider key(s) in `~/.openclaw/openclaw.json` (optional) | OpenAI (`talk.providers.openai.apiKey`, regular `sk-...` key, **not** the openai-codex OAuth profile) and/or Gemini (`talk.providers.gemini.apiKey`, `AIza...` from AI Studio) — either works on its own; with neither, RealTimeTalk runs TTS-only (no mic/wake-word listening — see "STT engine selection" below) |
| [Edge TTS skill](https://github.com/w2ayz/openclaw-edge-tts) (first TTS fallback — optional) | install at the official path `~/.openclaw/workspace/skills/edge-tts/` (`npm install` in `scripts/`); the installer resolves it and prepares its deps |
| Homebrew + portaudio + ffmpeg + node | `brew install portaudio ffmpeg node`     |
| `hidapi` (only for Radio Mode's AIOC hardware-revision detection — cosmetic, everything else works without it) | `brew install hidapi` |
| Python 3.9+                 | system Python or `brew install python`     |
| A microphone                | USB mic, Bluetooth headset, or iPhone via Continuity Camera |

### Adding the STT API key(s)

STT (mic/wake-word listening) is optional — skip this section entirely to
run RealTimeTalk TTS-only (OpenClaw can still push text to speak via
`POST /speak`; see "Pushing text from OpenClaw" below).

The easiest way to add, change, or remove these keys after install is the
re-runnable configure script:

```bash
bash RTT-Config.sh
```

It also checks your shell environment (`OPENAI_API_KEY`, `GEMINI_API_KEY`/
`GOOGLE_API_KEY`) and offers a key found there before prompting for one.

To edit `openclaw.json` directly instead: the daemon reads the STT key(s)
from `talk.providers.openai.apiKey` and/or `talk.providers.gemini.apiKey`.
Add this block (or merge it into your existing `talk` block) — **the
`providers` part only; STT engine selection lives in the daemon's own
config file (next section)**:

```json
"talk": {
  "providers": {
    "openai": { "apiKey": "sk-..." },
    "gemini": { "apiKey": "..." }
  }
}
```

### STT engine selection (`~/.openclaw/workspace/rtt_stt_config.json`)

The engine is configured in the daemon's own config file, NOT in
`openclaw.json` — OpenClaw's TalkSchema has no `stt` key, so a `talk.stt`
block there gets stripped by every OpenClaw config rewrite, blocks config
hot-reloads, and fails `openclaw config validate`:

```json
{
  "provider": "openai",
  "fallback": "gemini",
  "vocabulary": ["Zeebot", "OpenClaw"]
}
```

The default engine is OpenAI Realtime. To make Gemini the default, set
`provider` to `"gemini"` (and optionally `fallback` to `"openai"`).
You can also pass `--stt-engine gemini` to override at startup. The legacy
`openclaw.json` `talk.stt` block is still read if the daemon config file is
absent, so old configs keep working until migrated.

Set `"provider": "none"` (what `RTT-Config.sh`'s Skip option
writes) to run TTS-only on purpose even if a key is configured. With
neither an OpenAI nor a Gemini key present at all, the daemon resolves to
this same TTS-only mode automatically regardless of what `"provider"` says.

`"vocabulary"` is a **single shared list sent to both engines** — Gemini's
`custom_vocabulary` and OpenAI's `keywords` (OpenAI's `gpt-live-transcribe`
model only; the daemon always uses that model for this reason). Add proper
nouns, names, or jargon that either engine tends to mishear. Both treat it
as a *hint*, not a guarantee — it measurably helps common misspellings but
won't fix everything (an unusual acronym or call sign, for example, may
still come through imperfectly). Restart the daemon after editing this
list; it's only read at startup.

The Realtime API requires the standard OpenAI provider with `api_key` mode.
The `openai-codex` OAuth profile shipped by OpenClaw will NOT work for this
endpoint.

### Adding the ElevenLabs API key (optional)

Chinese and mixed Chinese/English replies use ElevenLabs multilingual v2
(voice "Rachel") for a consistent voice across languages, read from
`talk.providers.elevenlabs.apiKey`:

```json
"talk": {
  "providers": {
    "openai":     { "apiKey": "sk-..." },
    "elevenlabs": { "apiKey": "..." }
  }
}
```

Optional — if unset, Chinese/mixed replies fall back down the TTS chain
(OpenAI TTS by default). `bash RTT-Config.sh` prompts for this
key too (checking `$ELEVENLABS_API_KEY` in your environment first).

### TTS engine order (`~/.openclaw/workspace/rtt_tts_config.json`)

```json
{ "order": ["elevenlabs", "edge", "openai", "say"] }
```

Same daemon-owned-config pattern as STT engine selection above. The default
is ElevenLabs → Edge TTS → OpenAI TTS → macOS `say`, tried in order until
one produces audio. `RTT-Config.sh` lets you reorder this list
or drop engines you don't want (e.g. `["edge", "say"]` to never touch
ElevenLabs/OpenAI TTS) — `say` is always kept as the last-resort entry even
if you leave it out, since it needs no key or network. Restart the daemon
after editing this file directly; it's only read at startup.

---

## Installation

For a full walkthrough (prerequisites, file structure, permissions,
troubleshooting), see [DEPLOYMENT.md](DEPLOYMENT.md). Quick version:

```bash
mkdir -p ~/.openclaw/workspace/skills
git clone https://github.com/w2ayz/openclaw-RealTimeTalk-mac.git ~/.openclaw/workspace/skills/realtimetalk
bash ~/.openclaw/workspace/skills/realtimetalk/RealTimeTalk-install-mac.sh
```

(Clone directly into the OpenClaw skills directory — same convention as
every other skill, e.g. `skills/edge-tts/`. The installer and every other
script here resolve paths relative to wherever they're run from, so a
different location works too, but this is what the rest of the OpenClaw
setup expects.)

The installer:
1. `brew install`s portaudio, ffmpeg, node (skipped if already present)
2. Creates a Python venv at `./venv` and installs `sounddevice`, `websockets`, `numpy`, `zhconv`
3. STT keys/engine, TTS keys/engine order, and STT vocabulary — a choice
   menu (OpenAI / Gemini / both / keep existing / **skip for TTS-only**),
   hidden key input verified against each provider's API, an ElevenLabs key
   prompt, and a reorderable/droppable TTS engine chain. These three steps
   live in `RealTimeTalk-config-lib.sh` so you can re-run just this part
   later with `bash RTT-Config.sh` — see the sections above
4. Lists CoreAudio devices and prompts you for input + output device indices
5. Writes the LaunchAgent plist to `~/Library/LaunchAgents/ai.openclaw.realtimetalk.plist`
6. Loads the agent (boots at every login)

Then open `http://localhost:19000/dashboard`.

To change any STT/TTS key, the STT engine choice, the TTS engine order, or
the STT vocabulary later without repeating the whole install, re-run:

```bash
bash ~/.openclaw/workspace/skills/realtimetalk/RTT-Config.sh
```

### Microphone permission reliability (recommended)

A bare `python3` process launched by a LaunchAgent has no stable app
identity for macOS's TCC (privacy) subsystem, so microphone access can be
flaky — it may not prompt reliably, or the grant may not persist across
restarts. `RealTimeTalk-build-wrapper-mac.sh` builds a tiny signed wrapper
app (`~/Applications/RealTimeTalk.app`) that requests mic access via
AVFoundation under its own stable bundle identity before launching the
daemon as its child process:

```bash
bash ~/.openclaw/workspace/skills/realtimetalk/RealTimeTalk-build-wrapper-mac.sh
```

Then point the LaunchAgent plist's `ProgramArguments` at the built app
(`~/Applications/RealTimeTalk.app/Contents/MacOS/RealTimeTalk`) instead of the
venv's `python3` directly — any extra args (`--mic-gate 64`, etc.) pass
straight through. Reload with a full unload/reload, not just a restart:
`launchctl kickstart -k` does **not** pick up a changed plist file —

```bash
launchctl bootout gui/$(id -u)/ai.openclaw.realtimetalk
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.openclaw.realtimetalk.plist
```

First launch prompts for microphone access. If the dialog doesn't appear
(background/agent launches sometimes suppress it), run the app once via
Finder (double-click) to trigger it, then grant it in System Settings →
Privacy & Security → Microphone if needed.

---

## Control

```bash
bash RealTimeTalk-toggle.sh start     # load LaunchAgent
bash RealTimeTalk-toggle.sh stop      # unload (comes back at next login)
bash RealTimeTalk-toggle.sh restart   # bounce, re-reading the plist
bash RealTimeTalk-toggle.sh disable   # stop + keep off across reboots (mic kill-switch); verifies mic released
bash RealTimeTalk-toggle.sh enable    # undo disable, wait until the dashboard answers
bash RealTimeTalk-toggle.sh status    # launchctl status (+ whether it's disabled)
bash RealTimeTalk-toggle.sh log       # tail /tmp/openclaw/realtimetalk.log
bash RealTimeTalk-toggle.sh devices   # list CoreAudio inputs/outputs
```

Or via HTTP:

- `GET http://localhost:19000/dashboard` — UI
- `GET http://localhost:19000/wake` — activate
- `GET http://localhost:19000/sleep` — deactivate
- `GET http://localhost:19000/restart` — restart daemon
- `POST http://127.0.0.1:19000/speak` — speak arbitrary text (see below)

Or via voice: say "\<agent name\> wake up" (e.g. "Zeebot wake up" with the
default name) while Silent or Monitoring — the agent asks "Yes?" and
activates only on an affirmative reply ("yes", "ok", "wake up", "好", etc.)
or a repeated wake phrase within 15s, to avoid self-triggering off its own
TTS or background chatter. The dashboard Wake button skips this and
activates immediately. Once active: "\<agent name\> go to sleep",
"calibrate mic", etc.

### Pushing text from OpenClaw (or any local process)

`/speak` lets any process on the same machine make the daemon read text
aloud on demand — the piece that lets an OpenClaw agent do work triggered
by keyboard/text (not voice) and still deliver the result through RTT.
Useful when the request was typed but the answer should come back spoken —
away from the keyboard, on the radio, hands busy, etc. This is also what
makes TTS-only (no STT key configured) mode useful rather than just inert —
see "STT engine selection" above.

```bash
curl -s -X POST --data-urlencode "text=Your text here" http://127.0.0.1:19000/speak
# → {"ok": true, "queued": true, "chars": N}
```

- **Local-only** — rejects anything not from `127.0.0.1`/`::1`/`localhost`.
- **POST the text in the body** (form-encoded `text=...` or raw UTF-8) — this
  survives `&`, `#`, `+` and other characters that appear in real copy. The
  old `GET /speak?text=...` still works for simple one-liners, but mangles
  those characters.
- **Streaming** — reading starts as soon as the first sentence is
  synthesised; the rest of a long text is processed while the earlier
  sentences are already being spoken. No need to summarize first.
- Works even while RTT is in auto-sleep.
- Text runs through the normal TTS pipeline (markdown stripped, TTS
  engine as configured) and plays on whatever output device is currently
  selected — including transmitting on-air if Radio Mode is active. The
  line is also logged into the dashboard's conversation history like any
  other reply.

**Wiring it up in OpenClaw:** add a note to the `## Tools` section of the
agent's `AGENTS.md` (in `~/.openclaw/workspace/`) so it knows the capability
exists and when to reach for it — it won't discover the endpoint on its own.
Something like:

```markdown
### RealTimeTalk — push text to be read aloud

If <you> asks for something via keyboard/text (not voice) and wants the
result spoken through RealTimeTalk once it's ready — e.g. "look into X and
read me what you find" typed instead of said — call this instead of just
replying in text:

​```bash
curl -s -X POST --data-urlencode "text=YOUR TEXT HERE" http://127.0.0.1:19000/speak
​```

- Local-only. Use POST (not `?text=` in the URL) so long copy with `&`,
  `#`, `+` etc. survives intact.
- Reading starts streaming within the first sentence or two — long text is
  fine, no need to summarize first.
- Success looks like `{"ok": true, "queued": true, "chars": N}`.
- Only use this when RTT is the actual delivery channel wanted — not as a
  substitute for normal chat replies.
```

Since OpenClaw's `AGENTS.md` convention is to read the workspace fresh each
session, this takes effect on the next session with no daemon restart
required.

(OpenClaw 2026.8+ retired the standalone `TOOLS.md`; tool notes live in
`AGENTS.md`'s `## Tools` section. If your workspace still has a `TOOLS.md`,
`openclaw doctor --fix` folds it in.)

### Disabling RealTimeTalk (make it inert)

`RealTimeTalk-toggle.sh stop` only unloads the agent until the next login —
the LaunchAgent has `RunAtLoad`, so it comes back on reboot. To stop it
**and** keep it from starting again (no mic capture, no OpenAI Realtime
connection, no spoken output, dashboard down) without uninstalling
anything, use the `disable` / `enable` subcommands — this is the mic
kill-switch:

```bash
bash RealTimeTalk-toggle.sh disable    # stop now, keep off across reboots, verify the mic is released
bash RealTimeTalk-toggle.sh enable     # undo it and start again, waiting until the dashboard answers
bash RealTimeTalk-toggle.sh status     # shows "DISABLED" when it's off
```

`disable` runs `launchctl bootout` then `launchctl disable`, reaps the
daemon if an older wrapper orphaned it, and then confirms nothing is
running and port 19000 is free. `enable` runs `launchctl enable` (required
first — `bootstrap` silently refuses a disabled job) then `bootstrap`, and
polls `/status` until the daemon is up.

macOS shows an **orange dot** by the menu-bar clock whenever anything is
using the mic — with RTT disabled you should never see it (unless another
app is). For belt-and-suspenders you can also switch **RealTimeTalk** off
in System Settings → Privacy & Security → Microphone (re-grant it before
`enable` if you do).

This leaves the gateway (`ai.openclaw.gateway`) and everything else in
OpenClaw untouched — it only uses the mic through this daemon.

<details><summary>Equivalent raw <code>launchctl</code> commands</summary>

```bash
UID_VAL=$(id -u)
# disable
launchctl bootout  "gui/$UID_VAL/ai.openclaw.realtimetalk" 2>/dev/null
launchctl disable  "gui/$UID_VAL/ai.openclaw.realtimetalk"
# verify: all three should show nothing / "=> disabled"
pgrep -fl 'RealTimeTalk.app|RealTimeTalk-daemon.py'
lsof -iTCP:19000 -sTCP:LISTEN
launchctl print-disabled "gui/$UID_VAL" | grep realtimetalk
# enable
launchctl enable    "gui/$UID_VAL/ai.openclaw.realtimetalk"
launchctl bootstrap "gui/$UID_VAL" ~/Library/LaunchAgents/ai.openclaw.realtimetalk.plist
```

</details>

---

## Speaker verification (owner-only mode)

When enabled, the agent only acts on the enrolled owner's voice — every
transcript's audio segment is embedded with a bilingual (EN/ZH)
speaker-recognition model and compared against the enrolled profile by cosine
similarity. Non-matching speech is silently ignored and logged to the
dashboard with its similarity score.

### Setup

```bash
# 1. Install sherpa-onnx into the daemon's venv
./venv/bin/pip install sherpa-onnx

# 2. Download the 3D-Speaker CAM++ zh-en model (~28 MB)
mkdir -p ~/.local/share/rtt/speaker
curl -L -o ~/.local/share/rtt/speaker/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx \
  https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx
# (the release tag really is spelled "recongition" upstream)

# 3. Restart, then enroll from the Calibrate page (🎤 Voice ID button)
bash RealTimeTalk-toggle.sh restart
```

### Enrollment — per input device

Voice profiles are enrolled **per input device**, not just once. A profile
trained on your USB mic won't reliably match the same voice heard through a
Bluetooth headset or an iPhone Continuity mic — different frequency
response and compression — so `/voice-enroll` always targets whichever
input device is currently active, and `_verify_speaker` automatically picks
the right profile for whatever device you're using at the moment, with no
manual mode switch.

Open the dashboard's **Calibrate** page and click **🎤 Voice ID**
(`/voice-enroll`, next to Headset/Speaker/Auto) and record the three
5-second samples (English, Chinese, free speech) on the mic you want to
enroll — this keeps the audio channel identical to runtime. Save, then use
**Test my voice** to check your similarity score (expect ≥ ~0.6 for
yourself, ≤ ~0.4 for others). To enroll a second device, switch to it on
the Calibrate page first, then repeat. The enrollment page lists every
other already-enrolled device with a Clear button. Enable **Owner Only**
from the dashboard or by saying "only listen to me" — if the device you're
currently on has no profile yet, the agent warns you and accepts all speakers
on that device until you add one, rather than blocking Owner Only
entirely. (Voice ID lives on Calibrate rather than the main dashboard nav
since enrollment is a one-time/rare action; Owner Only/Everyone stays on
the dashboard as the frequently-used toggle.)

### Voice commands

| Say | Effect |
|-----|--------|
| "Only listen to me" / "只听我的" | Enable owner-only mode (requires enrolled profile) |
| "Listen to everyone" / "听大家的" | Disable owner-only mode |

In owner-only mode **everything** — wake phrases, sleep, monitor toggles, and
the mode toggles themselves — requires the owner's voice.

### Tuning

- Threshold defaults to **0.50** cosine similarity; adjust live with
  `/ownermode/threshold?value=0.55` or at startup with `--spk-threshold`.
  Every pass/reject is logged with its score
  (`bash RealTimeTalk-toggle.sh log | grep "Voice check"`).
- Segments shorter than ~0.8 s can't be verified and are ignored in
  owner-only mode — prefer "yes please" over a bare "yes" for wake
  confirmation.

### Known limitations

- The web dashboard buttons bypass verification by design (local-network
  fallback) — they never route through the transcript pipeline.
- Verification resists other *people*, not a **recording** of the owner's
  voice (replay attack) — this is out of scope.
- If the profile, model, or library is missing, the daemon accepts all
  speakers and the dashboard shows an amber warning.
- Enrollment/test recording opens a second input stream on the same mic
  device while the live session's stream stays open — this works on
  PipeWire (Pi) but depends on the specific USB mic's CoreAudio driver on
  Mac. If enrollment recordings come back silent or the live transcription
  stream drops afterward, that's this conflict — file it and we'll switch
  `_record_pcm_blocking` to briefly pause the live stream first.

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
            └─ RealtimeSession.send_audio()  (forward to STT engine WS)
                └─ Gemini 3.5 Transcribe Live (server-side VAD)
                   or OpenAI gpt-live-transcribe (client-side VAD, see
                   OpenAIRealtimeSession) — whichever is configured
                    └─ transcript event
                        ├─ Wake/sleep / command matcher  (skip if matched)
                        └─ GatewayClient.ask()  (OpenClaw chat.send → agent.wait)
                            └─ Agent's reply text
                                └─ speak() / _synthesize()
                                    ├─ TTS_ORDER (configurable, default shown):
                                    │   ├─ ElevenLabs eleven_v3  (full text)
                                    │   ├─ Edge TTS  (per-segment, native zh/en voices)
                                    │   ├─ OpenAI TTS tts-1-hd  (paid network fallback)
                                    │   └─ macOS `say`  (per-segment, offline last resort — always kept)
                                    ├─ ffmpeg → 24 kHz mono PCM int16
                                    ├─ software volume attenuation
                                    └─ sounddevice.play()  (CoreAudio output)
                                        └─ Speech-interrupt polling:
                                            adaptive threshold from measured
                                            speaker→mic coupling → sd.stop()
```

End-to-end latency: ~4–12 seconds, dominated by VAD silence window (1.1s)
and the agent's reasoning time.

---

## Limitations

- **No built-in Mac Mini mic** — external input required
- **ElevenLabs / Edge TTS / OpenAI TTS need internet** — falls back to offline
  `say` on failure/timeout or if no API key is configured (Edge TTS needs no key)
- **System-wide volume** — macOS scripting can only set the master output
  volume, not per-device
- **No WebRTC AGC** — CoreAudio handles input gain at the driver level, but
  USB mics with hot mic levels may need `--mic-gain` adjustment

---

## License

MIT
