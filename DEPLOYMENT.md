# Mac Deployment Guide

Step-by-step reference for installing RealTimeTalk on a Mac (Mac Mini or
any Mac with CoreAudio). Written to be followed by someone who hasn't seen
this repo before. For a feature overview, see [README.md](README.md); for
internals, see [SKILL.md](SKILL.md).

---

## 1. Prerequisites

### Accounts / services

| Requirement | Notes |
|---|---|
| [OpenClaw](https://openclaw.ai) gateway running | `openclaw gateway start`. RealTimeTalk talks to Zeebot through this — it won't start without it. |
| OpenAI API key | Regular `sk-...` key in `~/.openclaw/openclaw.json`, **not** the `openai-codex` OAuth profile (the Realtime API rejects it). See §3. |
| ElevenLabs API key (optional) | Only used for Chinese/mixed-language TTS. Falls back to OpenAI TTS if unset. |

### System

| Requirement | Install |
|---|---|
| macOS with Homebrew | <https://brew.sh> |
| Xcode Command Line Tools | `xcode-select --install` — provides the system Python 3.9 and `swiftc` (needed for the mic-permission wrapper, §5) |
| `portaudio`, `ffmpeg`, `node` | `brew install portaudio ffmpeg node` (the installer does this for you) |
| `hidapi` | `brew install hidapi` (installer does this too) — only needed for Radio Mode's AIOC hardware-revision detection; everything else works without it |
| `librsvg` | `brew install librsvg` (the wrapper-build script does this too) — only needed to render `ZeebotTalk.app`'s icon; the wrapper still builds fine without it, just with the generic default icon |
| Python 3.9+ | System Python from Command Line Tools is fine |
| Edge TTS skill | Must be installed at `~/.openclaw/workspace/skills/edge-tts/` before running the installer. See §3.5. |

### Hardware

| Requirement | Notes |
|---|---|
| A microphone | **Required** — Mac Mini has no built-in mic. USB mic, Bluetooth headset, or iPhone Continuity Camera mic all work. |
| Speakers | Built-in (Mac mini has none) or any CoreAudio output device. |
| AIOC dongle (optional) | Only for Radio Mode — see §7. [skuep/AIOC](https://github.com/skuep/AIOC), VID:PID `1209:7388`. Digirig Mobile is **not** supported on Mac (see README's Radio Mode section for why). |

---

## 2. File structure

### Repo layout (after clone)

```
~/.openclaw/workspace/skills/realtimetalk/
├── RealTimeTalk-daemon.py          # the daemon — everything runs from this one file
├── radio_interfaces.py             # Radio Mode: AIOC registry, PTT/audio device resolution, SquelchTracker
├── dtmf_monitor.py                 # standalone DTMF Mon/Train/Retrain CLI (launched from the dashboard)
├── requirements.txt                # Python deps (installed into venv/)
├── RealTimeTalk-install-mac.sh     # installer — run once
├── RealTimeTalk-build-wrapper-mac.sh   # builds the mic-permission wrapper app — run once, optional but recommended
├── RealTimeTalk-toggle.sh          # start/stop/restart/status/log/devices — day-to-day control
├── ai.openclaw.realtimetalk.plist  # LaunchAgent template — installer copies + fills this in
├── test_speak.py                   # standalone TTS smoke-test script
├── assets/ZeebotTalk-icon.svg      # wrapper app icon source — rendered to .icns at build time (§5)
├── README.md, SKILL.md, CHANGELOG.md, DEPLOYMENT.md (this file)
└── venv/                           # created by the installer, not in git
```

### Files the installer creates outside the repo

| Path | Purpose |
|---|---|
| `~/Library/LaunchAgents/ai.openclaw.realtimetalk.plist` | The actual LaunchAgent — a filled-in copy of the template above |
| `~/Applications/ZeebotTalk.app` | Mic-permission wrapper (§5), if you build it |
| `/tmp/openclaw/realtimetalk.log` | stdout+stderr log — `tail -f` this for everything |

### Runtime state (created by the daemon itself, first run)

All under `~/.openclaw/workspace/` — none of this is in git, all of it is
safe to delete to reset that specific piece of state:

| File | Purpose |
|---|---|
| `device_prefs.json` | Last-selected input/output device names (by name, not index — see CHANGELOG 3.9.1 for why) |
| `speaker_cal_store.json` | Per-speaker volume/SW calibration, keyed by device name |
| `rtt_voice_profiles.json` | Owner-only voice enrollment, keyed by device name (one entry per mic + one for the radio, if enrolled) |
| `rtt_voice_mode.json` | Owner-only on/off + similarity threshold |
| `rtt_dtmf_profiles.json` | Learned DTMF tone frequencies (Radio Mode) |
| `rtt_sleep_state.json` | Whether the daemon was asleep at last shutdown (restored on restart) |

---

## 3. Adding API keys

Use the Python one-liner below — it merges safely into the existing JSON
without disturbing other keys, and avoids the character-corruption risk
of hand-editing a long API key:

```bash
python3 - <<'PY'
import json, sys

KEY   = "sk-..."          # your regular OpenAI key (sk-proj-... or sk-...)
ELKEY = ""                # optional ElevenLabs key, or leave blank

path = __import__("os").path.expanduser("~/.openclaw/openclaw.json")
d = json.load(open(path))
p = d.setdefault("talk", {}).setdefault("providers", {})
p.setdefault("openai", {})["apiKey"] = KEY
if ELKEY:
    p.setdefault("elevenlabs", {})["apiKey"] = ELKEY
json.dump(d, open(path, "w"), indent=2)
print("done — key length:", len(KEY))
PY
```

> **Key format:** use a regular `sk-...` or `sk-proj-...` key. The OAuth
> profile (`openai:victorzengyi@gmail.com` / `openai-codex`) is rejected
> by the Realtime API. A project-scoped `sk-proj-...` key works fine.

`elevenlabs` is optional — omit or leave blank and it falls back to
OpenAI TTS for Chinese/mixed content.

---

## 3.5. Installing the Edge TTS skill

The installer checks for `~/.openclaw/workspace/skills/edge-tts/scripts/tts-converter.js`
and exits if it's missing. The skill wraps `node-edge-tts` (npm). Set it up once:

```bash
mkdir -p ~/.openclaw/workspace/skills/edge-tts/scripts

# Create package.json
cat > ~/.openclaw/workspace/skills/edge-tts/package.json <<'EOF'
{
  "name": "openclaw-edge-tts",
  "version": "2.0.0",
  "dependencies": {
    "node-edge-tts": "^1.2.10",
    "commander": "^12.0.0"
  }
}
EOF

npm install --prefix ~/.openclaw/workspace/skills/edge-tts
```

Then copy `tts-converter.js` from your `c2e-slack` repo (or any other
source that exports the same CLI interface) into
`~/.openclaw/workspace/skills/edge-tts/scripts/tts-converter.js`.

> **Note:** edge-tts is a legacy fallback only — it's present in the
> daemon's source but is never called by default (OpenAI TTS is primary).
> The installer still gates on the file existing, so this step is required
> even though the feature isn't in active use.

---

## 4. Installing

```bash
mkdir -p ~/.openclaw/workspace/skills
git clone https://github.com/w2ayz/openclaw-RealTimeTalk-mac.git ~/.openclaw/workspace/skills/realtimetalk
bash ~/.openclaw/workspace/skills/realtimetalk/RealTimeTalk-install-mac.sh
```

Clone directly into the OpenClaw skills directory — same convention as
every other skill (e.g. `skills/edge-tts/`). Every script here resolves
its own paths relative to wherever it's run from, so a different location
technically works too, but this is what the rest of an OpenClaw setup
expects, and what the rest of this guide assumes.

The installer:
1. `brew install`s `portaudio`, `ffmpeg`, `node`, `hidapi`
2. Creates a Python venv at `venv/` and installs everything in `requirements.txt`
3. Verifies `openai.apiKey` is set (exits with instructions if missing)
4. Lists CoreAudio devices and prompts for input/output device indices
5. Writes and loads the LaunchAgent plist

> **Known installer bug — system-default devices:** If you press Enter
> for both device prompts (accepting system defaults), the installer exits
> with `EXTRA_ARGS[@]: unbound variable`. This is a `set -u` bug with
> empty bash arrays. Workaround: run steps 4–5 manually after the venv
> is created (the installer will have completed steps 1–3 before failing):
>
> ```bash
> SKILL_DIR=~/.openclaw/workspace/skills/realtimetalk
> VENV_PY=$SKILL_DIR/venv/bin/python3
> PLIST_DEST=~/Library/LaunchAgents/ai.openclaw.realtimetalk.plist
> mkdir -p ~/Library/LaunchAgents /tmp/openclaw
> python3 - <<PY
> src = open("$SKILL_DIR/ai.openclaw.realtimetalk.plist").read()
> src = src.replace("__VENV_PYTHON__", "$VENV_PY")
> src = src.replace("__DAEMON_PATH__", "$SKILL_DIR/RealTimeTalk-daemon.py")
> src = src.replace("__SKILL_DIR__",   "$SKILL_DIR")
> open("$PLIST_DEST", "w").write(src)
> PY
> launchctl bootout gui/$(id -u)/ai.openclaw.realtimetalk 2>/dev/null || true
> launchctl bootstrap gui/$(id -u) "$PLIST_DEST"
> ```
>
> If your mic wasn't connected during install (device list showed no
> inputs), add `--input-device <index>` after the daemon is running by
> editing the plist and reloading via bootout+bootstrap (§5). Get the
> index from `./RealTimeTalk-toggle.sh devices` after plugging the mic in.
> Note: `--input-device` takes an **integer** index, not a device name.

Then open **http://localhost:19000/dashboard**.

---

## 5. Microphone permissions (recommended)

A bare `python3` process launched by a LaunchAgent has no stable app
identity for macOS's TCC (privacy) subsystem — microphone access can fail
to prompt reliably, or the grant can fail to persist across restarts. This
repo works around it with a small signed wrapper app that requests mic
access under its own stable bundle identity before launching the daemon
as a child process.

```bash
bash ~/.openclaw/workspace/skills/realtimetalk/RealTimeTalk-build-wrapper-mac.sh
```

This builds `~/Applications/ZeebotTalk.app` (ad-hoc signed, no Apple
Developer account needed — requires `swiftc` from Xcode Command Line
Tools) and renders its app icon from `assets/ZeebotTalk-icon.svg`
(auto-installs `librsvg` via Homebrew if missing; skips the icon and
falls back to the generic default if the SVG or `rsvg-convert` isn't
available, rather than failing the whole build). Then:

1. Edit `~/Library/LaunchAgents/ai.openclaw.realtimetalk.plist` — make
   **two changes** to `ProgramArguments`:
   - Replace the first `<string>` (the venv `python3` path) with the wrapper binary:
     ```
     /Users/<you>/Applications/ZeebotTalk.app/Contents/MacOS/ZeebotTalk
     ```
   - **Remove** the second `<string>` — the daemon script path (e.g.
     `/Users/<you>/.openclaw/workspace/skills/realtimetalk/RealTimeTalk-daemon.py`).
     ZeebotTalk has this path baked in at compile time and launches Python
     itself; passing it again as an argument causes `unrecognized arguments`
     and the daemon exits immediately with code 2.

   The array should look like this afterwards (any extra device flags go here too):
   ```xml
   <array>
       <string>/Users/<you>/Applications/ZeebotTalk.app/Contents/MacOS/ZeebotTalk</string>
       <string>--http-port</string>
       <string>19000</string>
   </array>
   ```
2. Reload with a **full unload/reload, not a restart**:
   ```bash
   launchctl bootout gui/$(id -u)/ai.openclaw.realtimetalk
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.openclaw.realtimetalk.plist
   ```
   `launchctl kickstart -k` (what `RealTimeTalk-toggle.sh restart` uses)
   restarts the *process* but does **not** reload a changed *plist file* —
   confirmed live, a `DYLD_LIBRARY_PATH` addition silently didn't take
   effect through over an hour of otherwise-successful `kickstart -k`
   restarts, only a bootout+bootstrap picked it up.

First launch prompts for microphone access. If the dialog doesn't appear
(background/agent launches sometimes suppress it), run the app once via
Finder (double-click `ZeebotTalk.app`) to trigger it, then check
**System Settings → Privacy & Security → Microphone** and grant it there
if it's listed but unchecked.

Re-run `RealTimeTalk-build-wrapper-mac.sh` any time you need to rebuild —
it's idempotent (rebuilds and re-signs in place).

---

## 5.5. Speaker verification model (Voice ID)

The Voice ID enrollment page (`/voice-enroll`) requires the sherpa-onnx
speaker-embedding model. Without it the page shows
`sherpa-onnx or model unavailable` and recording is disabled.

```bash
mkdir -p ~/.local/share/rtt/speaker
curl -L -o ~/.local/share/rtt/speaker/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx \
  "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"
```

> **Note the typo in the release tag:** `recongition` (not `recognition`) —
> that's the upstream tag name; the URL above is correct as written.

The file is ~27 MB. After downloading, restart the daemon — the log
should then show `Speaker-embedding extractor loaded (dim=192)` on startup.
Voice ID is optional; omitting it leaves speaker verification disabled
(everyone's voice is accepted) with a banner on the Voice ID page.

---

## 6. Verifying the install

1. `curl -s -o /dev/null -w '%{http_code}\n' http://localhost:19000/dashboard` → `200`
2. Say the wake phrase (default: "Zeebot wake up", or whatever you've configured) near the mic → daemon should respond
3. Check the log for a clean startup, no repeated errors:
   ```bash
   tail -n 30 /tmp/openclaw/realtimetalk.log
   ```
4. Confirm mic/speaker on the Calibrate page (`/calibration`) show real
   device names, not `device #N` or a device you've since disconnected
5. If you built the wrapper: `ps -o ppid= -p $(pgrep -f RealTimeTalk-daemon.py)` should show `ZeebotTalk`'s PID as the parent, not `1` (launchd) directly

---

## 7. Radio Mode (optional)

Requires an AIOC dongle connected via USB.

1. `brew install hidapi` (the installer already does this)
2. Plug in the AIOC — Radio Mode should **auto-enable** within a few
   seconds (confirm on the Calibrate page: the Radio button turns red/on).
   Unplugging auto-disables it and restores your normal mic. A manual
   toggle sticks until the next unplug/replug cycle.
3. Optional, from the Calibrate page once Radio Mode is on:
   - **Monitor** — live RX passthrough to any speaker (works even with
     Radio Mode off, as long as the AIOC is connected)
   - **EchoTest** — records an incoming transmission, replays it back on-air
   - **DTMF Mon/Train/Retrain** — launches `dtmf_monitor.py` in Terminal.app;
     train profiles for the digits you plan to use before relying on Mon
   - **Radio Voice Profile** — separate owner-only enrollment for the
     radio audio path (voice characteristics differ enough over radio
     that a mic-enrolled profile won't reliably match)

See the README's "Radio Mode" section and `radio_interfaces.py`'s module
docstring for what's and isn't supported (notably: no Digirig Mobile).

---

## 8. Day-to-day control

```bash
cd ~/.openclaw/workspace/skills/realtimetalk
./RealTimeTalk-toggle.sh start      # load the LaunchAgent
./RealTimeTalk-toggle.sh stop       # unload it
./RealTimeTalk-toggle.sh restart    # bounce it (does NOT reload a changed plist — see §5)
./RealTimeTalk-toggle.sh status     # launchctl status
./RealTimeTalk-toggle.sh log        # tail -f the log
./RealTimeTalk-toggle.sh devices    # list CoreAudio devices the daemon can see
```

---

## 9. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `OSError: [Errno 48] Address already in use` on restart | A stale process is still holding port 19000 — `launchctl kickstart -k` doesn't reliably kill the previous child. Kill it first: `lsof -i:19000 -P \| grep LISTEN \| awk '{print $2}' \| xargs kill -TERM`, then do a full bootout+bootstrap. |
| Plist edit doesn't seem to take effect | You used `kickstart -k` or `RealTimeTalk-toggle.sh restart` — neither reloads a changed plist file. Use `launchctl bootout` + `bootstrap` instead (§5). |
| `EXTRA_ARGS[@]: unbound variable` during install | You pressed Enter for both device prompts (system defaults). This is a `set -u` bug in the installer. See the workaround in §4. |
| Daemon exits immediately with code 2 after switching to ZeebotTalk | The daemon script path is still in `ProgramArguments` as the second element. ZeebotTalk bakes that path in at compile time — passing it again causes `unrecognized arguments`. Remove the `__DAEMON_PATH__` entry from the plist array (see §5). |
| `argument --input-device: invalid int value` | `--input-device` takes an integer index (e.g. `1`), not a device name string. Use `./RealTimeTalk-toggle.sh devices` to get the index. |
| Voice ID page shows `sherpa-onnx or model unavailable` | The speaker-embedding model file is missing. Download it — see §5.5. |
| Daemon exits with signal 11 (segfault), auto-restarts via launchd | Known failure class from concurrent PortAudio stream operations — should be fixed as of v3.9.1 (see CHANGELOG), but if you hit a new one, check `/tmp/openclaw/realtimetalk.log` around the crash for what else was happening (Monitor/EchoTest toggling, device hot-plug) and report it. |
| `import hid` fails / AIOC shows generic name instead of "AIOC v1.2+" | `DYLD_LIBRARY_PATH` isn't reaching the process. Confirm `brew install hidapi` succeeded, confirm the plist's `EnvironmentVariables` includes `DYLD_LIBRARY_PATH`, and confirm you reloaded via bootout+bootstrap, not kickstart. Cosmetic only — nothing else depends on `hid`. |
| Speaker/mic panel shows a device you disconnected | Should self-correct within ~2 seconds (the Calibrate page polls and re-resolves by name against a fresh device list as of v3.9.1). If it doesn't, restart the daemon. |
| Mic never picks up audio | Check mic permission was actually granted (§5) — `tccutil reset Microphone ai.openclaw.zeebottalk` and re-launch if you're unsure, then check System Settings → Privacy & Security → Microphone. |
| `sd.rec()`/enrollment records from the wrong device | Fixed as of v3.9.1 — update if you're on an older commit. |

---

## 10. Updating an existing install

```bash
cd ~/.openclaw/workspace/skills/realtimetalk
git pull
./venv/bin/pip install -r requirements.txt --upgrade
./RealTimeTalk-toggle.sh restart
```

If the update touched the LaunchAgent plist template or added new
`EnvironmentVariables`, re-run the installer's plist-writing step (or
manually diff `ai.openclaw.realtimetalk.plist` against
`~/Library/LaunchAgents/ai.openclaw.realtimetalk.plist`) and reload with
bootout+bootstrap, not just restart.
