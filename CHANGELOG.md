# Changelog

## [3.9.1] — 2026-08-12

Radio Mode — the AIOC ham-radio dongle support explicitly deferred in
3.8.0's "Not ported this round". Digirig Mobile is not included: its CM108
codec reports a generic product string that collides with unrelated USB
mics, needing IOKit-based USB topology correlation to disambiguate that
this port doesn't attempt (see `radio_interfaces.py`'s module docstring).

### Added
- **Radio Mode.** Toggle on the Calibrate page routes STT input from the
  AIOC's audio-in and TTS replies out over the AIOC's audio-out, keyed by
  PTT (DTR line). Auto-enables on AIOC plug-in and auto-disables on
  unplug, with a manual override that sticks until the next unplug/replug
  cycle. New files: `radio_interfaces.py` (interface registry, PTT/audio
  device resolution, `SquelchTracker`), `dtmf_monitor.py` (standalone
  DTMF Mon/Train/Retrain CLI, launched from the dashboard).
- **Radio-aware AGC.** Radio Mode routes mic capture through the numpy
  RMS+tanh leveler instead of WebRTC AGC — `webrtc_noise_gain`'s Mac
  binding has no per-effect toggles to replicate Pi's PipeWire config
  (disabling VAD/transient-suppression/AEC for radio audio), and the
  leveler sidesteps the problem by not having those effects to begin with.
- **Monitor.** Live RX passthrough (radio audio in → any speaker), with a
  per-device picker in the Audio Devices table. Software gain (AIOC's RX
  output measures ~-48dBFS idle).
- **EchoTest.** Detects an incoming transmission via squelch, records it,
  replays it back on-air via PTT.
- **DTMF Mon/Train/Retrain**, ported from Pi's `dtmf_monitor.py` —
  Goertzel tone detection against learned per-digit profiles, launched in
  Terminal.app from the Calibrate page.
- **Radio Voice Profile.** Dedicated enrollment section (separate from the
  per-device flow generalized in 3.8.0) that records directly from the
  AIOC regardless of which device is currently selected for the main mic
  — matches Pi's fixed Mic/Radio layout without requiring a device switch
  first.
- Play Test loop routes over PTT when Radio Mode is on, or to the
  Monitor's current target when Monitor is on, matching Pi's priority
  order.

### Fixed
- `_record_pcm_blocking` (voice enrollment/test) never passed an explicit
  `device=` to `sd.rec()`, so it silently used the OS-wide default input
  regardless of Radio Mode's input switch.
- Output device selection (`/device-set`, the device status panel) trusted
  a raw CoreAudio index that isn't stable across reinit/hot-plug cycles —
  AIOC's identically-named input/output entries made this concretely
  wrong (an output stream landing on the input-only entry, 0 channels).
  Resolution is now name-based, verified against a fresh subprocess device
  query rather than this process's own cache, which does not reliably
  notice a device has disconnected.
- A cluster of PortAudio/CoreAudio concurrency issues surfaced by Radio
  Mode's background watchdogs, consolidated behind one process-wide
  reentrant lock (`_audio_open_lock`) around every native stream
  open/close and `sd._terminate()`/`_initialize()` call: concurrent reinit
  from two independent recovery loops (segfault), a resource leak from an
  earlier fix that skipped cleanup on a vanished device (which then
  permanently blocked reopening), and an 8s startup grace period so the
  hot-plug watcher's auto-enable can't race the session's own first mic
  stream.

### Known limitations
- The AIOC HID config protocol (`detect_hw_variant`, hardware-revision
  labeling) needs `DYLD_LIBRARY_PATH` set at process launch for the `hid`
  package's native loader — included in the LaunchAgent plist template,
  but `DYLD_LIBRARY_PATH` set via `os.environ` *after* process start does
  not retroactively affect ctypes' library search, so a wrapper binary
  that execs the daemon with its own stripped environment (rather than
  the plist's `ProgramArguments` launching the daemon directly) will not
  pick this up. Cosmetic only — PTT/audio routing/DTMF do not depend on it.

## [3.8.0] — 2026-07-29

Version number tracks the Pi release this port draws from, same convention
as every previous Mac bump. Pi caught up fast (v3.0.1 → v3.9.1 in 9 days),
almost entirely built around a new Radio Mode hardware layer — this release
covers the Pi v3.8.0 concepts, generalized. Pi v3.8.0's own headline feature
was "separate voice profile for Radio mode"; Mac has no radio hardware, but
the underlying problem (a voice embedding enrolled on one audio path not
matching reliably on a different one) applies just as much to switching
between a USB mic, a Bluetooth headset, or an iPhone Continuity mic. Plus
the two other Pi v3.8.0 improvements, which port directly as-is. The rest of
Pi's v3.1.0-v3.9.1 range (Radio Mode itself: AIOC/Digirig, PTT, DTMF,
EchoTest) is intentionally not in this release — see "Not ported" below.

### Added
- **Per-device voice profiles.** Owner-only verification now supports an
  enrolled profile *per input device* instead of exactly one — generalizes
  Pi's `radio: bool` parameter (mic vs. radio) to an arbitrary device-name
  key, following the same dict-keyed-by-device-name pattern Mac's speaker
  calibration already uses (`_cal_store`). `_verify_speaker` resolves which
  profile to score against from whichever input device is currently active,
  automatically — no manual mode switch needed.
- `/voice-enroll` now always enrolls/tests against the currently active
  input device (labeled by name) and lists any other already-enrolled
  devices with a per-device Clear button, instead of a fixed two-profile
  (mic/radio) layout.
- Dashboard fail-open banner and the Calibrate page's Voice ID button both
  name the specific device that's missing a profile, rather than a generic
  "no profile" message.
- `/status` reports `enrolled_devices: [...]` and `current_device` instead
  of a single `enrolled` bool.
- One-time migration: the old single-profile file
  (`rtt_voice_profile.json`) is adopted into the new per-device store
  (`rtt_voice_profiles.json`) under whichever input device is active at
  first boot after this update, then archived as `.migrated` rather than
  deleted.
- **Continuous echo-coupling tracking during TTS playback** (Pi v3.8.0):
  the self-interrupt threshold used to be frozen from a 1-second guard
  window at the start of each reply; it now keeps an EMA of the
  output/mic coupling ratio for the whole reply, so a long or unevenly-loud
  response doesn't drift out of range of a threshold set from its first
  second. Ticks that already look like a real barge-in are excluded from
  the running estimate. New `SPEAK_COUPLING_EMA = 0.15` constant.

### Changed
- **Compact dashboard nav** (Pi v3.8.0 CSS): nav button padding/font-size
  trimmed across all breakpoints so the full button row (Wake, Sleep,
  Monitor, Multi-lang, Owner Only, Clear Log, Restart, Gateway Reset)
  reliably fits on one line instead of occasionally wrapping — Mac hit the
  same width problem Pi did once Owner Only was added.

### Not ported this round
The rest of Pi's v3.1.0-v3.9.1 range is the Radio Mode hardware layer
(AIOC/Digirig ham-radio dongles, PTT, DTMF, EchoTest) — scoped separately;
see the project plan for the macOS port's open risks (Digirig's audio
device disambiguates cleanly on Linux via ALSA `usbid`, with no direct
macOS equivalent yet) before that lands.

---

## [3.0.1] — 2026-07-20

### Changed
- **Moved the `Voice ID` button** from the dashboard nav to the Calibrate
  page (next to the Headset/Speaker/Auto mode buttons) — enrollment is a
  one-time/rare action, not something used every day. `/voice-enroll` itself
  is unchanged; its "back" link now points to `/calibration` instead of
  `/dashboard`. The `Owner Only`/`Everyone` toggle stays on the dashboard
  since it's the frequently-used control. Button color reflects owner-only +
  enrollment state (red = owner-only requested but not enrolled, green =
  owner-only active, blue = enrolled but everyone-mode, gray = nothing set
  up yet) — same logic Pi uses.

---

## [3.0.0] — 2026-07-20

Ports the Raspbian build's v3.0.0 speaker verification (owner-only mode) to
the Mac fork. Behavioral major, matching Pi's own version bump: enabling
owner-only mode intentionally rejects voice input previously accepted.

### Added
- **Speaker verification (owner-only mode)**: when enabled, every voice
  transcript's matching audio segment is embedded with a bilingual (EN/ZH)
  speaker-recognition model (3D-Speaker CAM++ zh-en, via `sherpa-onnx`) and
  compared against an enrolled profile by cosine similarity. Non-matching
  speech is silently ignored and logged with its similarity score. Gates
  *everything* downstream in `_handle_transcript` — wake-confirmation
  replies, wake/sleep phrases, monitor toggles, the owner-mode toggles
  themselves — not just the final agent routing.
- **Voice enrollment** at `/voice-enroll`: records three 5s samples (English,
  Chinese, free speech) via `sd.rec()`, embeds each, saves the mean +
  per-sample embeddings to `rtt_voice_profile.json`. **Test my voice** button
  reports a live similarity score against the enrolled profile.
- **Dashboard**: Owner Only/Everyone toggle button, Voice ID enrollment link,
  `👤` label in the device panel, amber banner when owner-only is requested
  but no profile/model is available (fails open — accepts all speakers rather
  than going silent).
- **Voice commands**: "only listen to me" / "只听我的" (enable), "listen to
  everyone" / "听大家的" (disable) — both already owner-gated, so once
  owner-only is on, only the owner can turn it off by voice (dashboard button
  is the fallback).
- New HTTP endpoints: `/ownermode`, `/ownermode/on`, `/ownermode/off`,
  `/ownermode/threshold?value=N`, `/voice-enroll`, `/voice-enroll/record`,
  `/voice-enroll/save`, `/voice-enroll/test`, `/voice-enroll/clear`.
  `/status` now reports `owner_mode`, `enrolled`, `spk_threshold`.
- New CLI flag `--spk-threshold` (cosine pass mark override, default 0.50).
- Threshold, mode, and enrolled profile all persist across restarts
  (`rtt_voice_mode.json`, `rtt_voice_profile.json` in `~/.openclaw/workspace/`).

### Setup
Requires `./venv/bin/pip install sherpa-onnx` and the ~28 MB 3D-Speaker model
downloaded to `~/.local/share/rtt/speaker/` — see README for the exact
command. Missing library or model degrades to accept-all-speakers with a
dashboard warning, not a crash or a silent lockout.

### Known risk — not yet resolved by live testing
`_record_pcm_blocking()` (enrollment/test recording) opens a **second**
`sd.rec()` input stream on the same device while `RealtimeSession` already
holds a persistent `sd.InputStream` open for live transcription. Verified in
isolation (model load, embedding computation, profile save/load round-trip,
mode persistence — all pass against synthetic audio outside the live daemon).
**Not yet verified**: whether the USB mic in use allows two simultaneous
open streams, and real voice-vs-voice discrimination (synthetic noise can't
validate that only real speech comparison can). Needs an interactive pass
through `/voice-enroll` on the running daemon.

### Not ported (Pi/PipeWire-specific)
DTMF-bypasses-verification note from Pi's README doesn't apply — Mac has no
DTMF. `journalctl`-based score log-tailing replaced with
`RealTimeTalk-toggle.sh log` in the docs.

---

## [2.11.0] — 2026-07-20

Ports the Raspbian build's v2.0.2 → v2.11.0 improvements to the Mac fork,
skipping Linux/hardware-specific and openWakeWord-dependent items (see
"Not ported" below). Version number jumps to match the Pi release it was
ported from, not ten Mac-specific releases.

### Added
- **ElevenLabs multilingual v2 TTS** (`_elevenlabs_tts_to_mp3`) for Chinese and
  mixed Chinese/English replies — voice "Rachel", sent as one full-text call so
  the voice doesn't switch mid-reply. Falls back to OpenAI TTS
  (`tts-1-hd`/nova), then macOS `say`, on failure or if no key is configured.
  Key read from `talk.providers.elevenlabs.apiKey` in `openclaw.json` via new
  `load_elevenlabs_key()` (shares SecretRef resolution with `load_openai_key()`
  through new `_resolve_provider_api_key()`).
  **Known issue**: the configured ElevenLabs account is on the Free plan, which
  returns HTTP 402 for premade/library voices (including "Rachel") via the
  API — Free-tier API access is limited to voices you've cloned yourself. Until
  the plan is upgraded (Starter+) or `ELEVENLABS_VOICE_ID` is pointed at an
  owned voice, every call falls through to OpenAI TTS. The fallback chain
  handles this correctly (verified — no crash, no silence), it just means the
  ElevenLabs improvement is not yet actually in effect.
- **Wake confirmation step**: wake phrase in Silent/Monitoring now prompts
  "Yes?" instead of activating instantly. Confirmed by an affirmative reply
  (`yes`, `yeah`, `ok`, `sure`, `wake up`, `yes please`, `好`, `是`, …) or a
  repeated wake phrase within 15s; anything else is logged as a mis-fire and
  Zeebot stays silent. The dashboard `/wake` button still bypasses confirmation
  for immediate activation.
- **`_persist_active`**: Active (voice-routing) state now survives the 60-min
  OpenAI session reconnect, matching the existing `_persist_monitoring` /
  `_persist_multilang` pattern. Previously a session recycle silently dropped
  the user back to Silent with no indication.
- **Sleep-state persistence** (`rtt_sleep_state.json`): SLEEPING now survives a
  daemon/service restart — on restart the daemon waits for `/wake` instead of
  reconnecting to OpenAI immediately.
- **Monitor button works from SLEEPING**: previously a no-op when no session
  was live; now pre-arms Monitoring and wakes, so the new session starts in it.
- Stale-reply guard: the status-token → `chat.history` fallback now rejects a
  reply identical to the last one it already delivered (agent hadn't produced
  a new response yet).
- Punctuation-only transcripts (e.g. `"..."`) are now dropped explicitly —
  previously they slipped past the short-word guard (empty string has zero
  words, not one) and got routed to Zeebot as blank queries.

### Changed
- `AGENT_TIMEOUT_S` 60 → 90.
- Short-word noise guard: single non-command words under 9 characters (was 6)
  are dropped; whitelist extended with `right`, `great`, `thanks`, `please`,
  `repeat`, `exactly`, `correct`, `alright`.
- Monitoring mode no longer resets the auto-sleep idle clock — it's passive
  capture and must not block auto-sleep from firing (previously monitoring
  activity kept resetting `_last_interaction`, the opposite of the intended
  behaviour).

### Not ported (Linux/hardware-specific or openWakeWord-dependent)
AIOC ham radio integration, DTMF wake/sleep state machine, PipeWire AGC
profile switching — all Pi-hardware-specific. openWakeWord-driven wake
detection/confirmation, mic-level-during-sleep, and the mic-gate
auto-calibration reconnect fix — all depend on OWW's always-on local listener
thread, which the Mac build doesn't run. Bluetooth `paplay` fix — PipeWire-
specific. Speaker-echo auto-calibration UI fixes — the Mac dashboard's
calibration flow already updates fields immediately on response, not on a
polling tick, so the underlying Pi bug doesn't reproduce here. **v3.0.0
speaker verification (owner-only mode) — excluded, not part of this pass.**

---

## [2.0.2] — 2026-05-23

### Fixed
- **Auto-sleep idle watcher overhauled** to match Debian reference build:
  - Renamed `_auto_sleep_watchdog` → `_idle_watcher(ws)` and passes the live
    WebSocket; calls `await ws.close()` directly so the `async with` context in
    `run()` exits cleanly — no `_sleep_event` side-channel needed.
  - **Critical bug**: removed `if not self._active: continue` guard that
    prevented the watcher from firing in SILENT and MONITORING states. The watcher
    now runs against all states (ACTIVE, SILENT, MONITORING).
  - Disabled only when `multilang != "off"` (non-English sessions stay alive
    indefinitely, matching Debian behaviour).
  - Idle clock (`_last_interaction`) seeded in `main()` before the first session
    starts, not inside the watcher, so the clock is correct from daemon boot.
  - HTTP `/wake` now stamps `_last_interaction` on both paths (sleep-reconnect
    and live-session wake) so pressing Wake fully resets the idle countdown.
- Dashboard shows **SLEEPING** state pill (warm-grey) while disconnected from
  OpenAI due to auto-sleep, distinct from SILENT (connected but inactive).

---

## [2.0.0] — 2026-05-23

### Breaking changes
- **OpenAI TTS** replaces Edge TTS as the primary synthesis engine. Mixed
  Chinese/English is now handled natively in a single API call without
  script-splitting. Edge TTS dependency is retained but unused by default.
- `sess._multilang` is now a `str` (`"off"` | `"en-zh"` | `"whitelist"` |
  `"any"`) instead of a `bool` — callers that read it as a bool need updating.

### Added
- **Auto-sleep watchdog** (`_auto_sleep_watchdog`): goes silent after 10 min
  (`AUTO_SLEEP_SECS = 600`) of no user→LLM interaction. Resets monitoring and
  multilang mode. Timer stamps on every query, wake phrase, and active
  monitoring capture.
- **`_clear_audio_buffer` flag**: set when TTS is interrupted mid-sentence;
  `_send_mic` sends `input_audio_buffer.clear` to OpenAI before the next mic
  chunk so stale VAD audio does not generate a spurious post-interrupt
  transcript.
- **`_persist_monitoring` / `_persist_multilang`** module-level lists: preserve
  monitoring and multilang state across the 60-min OpenAI Realtime session
  reconnect. `RealtimeSession.__init__` reads these instead of resetting to
  defaults.
- **Multi-language 4-state cycle** (`off` → `en-zh` → `whitelist` → `any`):
  `off` = EN/ZH only with auto-sleep; `en-zh` = EN/ZH but auto-sleep
  suppressed; `whitelist` = languages in `MULTILANG_WHITELIST_LANGS`; `any` =
  all pass. `/multilang` cycles through the four states and persists.
- **`_is_in_multilang_whitelist()`**: Unicode script range detection (Hangul,
  Kana, Arabic, Cyrillic, Devanagari, CJK) plus `langdetect` fallback for
  Latin-script text.
- **Short-word noise guard**: single transcribed words < 6 characters not in
  `_SHORT_CMDS` are dropped before LLM routing (OpenAI Realtime STT sometimes
  hallucinates single words from background noise).
- **CJK↔Latin boundary splitting** in `_normalize()`: inserts a space between
  CJK and ASCII characters so mixed phrases like `"我係wake up"` tokenise
  correctly for phrase matching.
- **Monitoring phrase robustness**: non-ASCII stripped before matching + `"star"`
  added as a `_start_words` alias for "start" (common STT mishear). Bare
  `"monitoring"` alone (≤ 3 words after strip) is treated as ON. Expanded
  `MONITOR_ON_PHRASES` / `MONITOR_OFF_PHRASES` sets.
- **Chinese TTS preprocessing**: `_num_to_zh` / `_zh_numbers` convert ASCII
  digit sequences in Chinese segments to Chinese numerals; `_preprocess_zh_time`
  rewrites `H:MM` patterns to `X点Y分` form before synthesis.
- **Acronym expansion** (`_preprocess_acronyms`): 2–4-letter uppercase codes
  (e.g. `ICN`, `JFK`) are space-separated so TTS reads them letter by letter.
- **Live mic hot-switch** (`_switch_mic_stream`): selecting a mic via the
  dashboard now does a live stream swap without restarting the daemon. The
  `_watch_mic_stream` watchdog timestamp is reset first to avoid a race.
- **Agent timeout** hardened: `asyncio.wait_for` + `asyncio.shield` guard
  `gw.ask()` so a stalled agent raises `TimeoutError` cleanly instead of
  hanging the transcript handler indefinitely.
- **Dashboard hover hints**: `<div id="hzone">` below the device panel shows
  button descriptions on mouse hover; hints fade after 60 s or on mouseleave.
  Button symbols updated to BMP-safe Unicode (✏ ☾ ◎ ⊕) matching the
  [UI-BUTTONS.md](https://github.com/w2ayz/openclaw-RealTimeTalk/blob/main/UI-BUTTONS.md)
  spec from the Debian reference build.
- **Auto-refresh pauses on hover**: replaced `<meta http-equiv="refresh">` with
  JS `setTimeout`/`clearTimeout` so the page does not reload while the cursor
  is over the nav.

### Fixed
- **Monitoring check order** (critical): the monitoring passive-log `return` was
  evaluated *before* wake/sleep/calibrate/monitoring-toggle phrase checks, so
  "wake up" while monitoring was silently dropped. Control phrase checks now
  all precede the monitoring block.
- **Wake phrase exits monitoring**: if `_monitoring` is True when the wake
  phrase fires, monitoring is cleared and voice is activated rather than
  being ignored.
- **Sleep button clears monitoring**: `/sleep` route condition was
  `if sess._active` — monitoring sets `_active=False`, so the button had no
  effect while monitoring. Fixed to `sess._active or sess._monitoring`.
- **"No device change detected" noisy banner**: green banner in the no-change
  branch suppressed (`device_banner = ""`); orange device-change alert still
  appears.
- **PAUSED state display**: PAUSED badge no longer requires `_active=True`;
  speaking banner moved above conversation log for visibility.

---

## [1.3.0] — 2026-05-19

### Added
- **USB mic hot-plug recovery**: `_watch_mic_stream()` watchdog coroutine
  detects when mic callbacks stop for > 4 s, calls `sd._terminate()` /
  `sd._initialize()` to force PortAudio to refresh its device list, resolves
  the device by saved name via subprocess, and reopens the stream — all
  without dropping the OpenAI WebSocket session. Logs "Mic reconnected." in
  the dashboard on success.

### Changed
- `sd.InputStream` switched from `with`-block to manual `start()` / `stop()`
  / `close()` with `try/finally` so the session survives hot-plug without
  a full reconnect.
- Gate calibration multiplier **1.25× → 1.5×** noise floor peak across all
  three calibration paths (voice command, HTTP `/calibrate/run`, `--calibrate`
  CLI). Gives a more comfortable margin above noise spikes; speech (typically
  5–20× above the noise floor) still passes cleanly into the AGC stage.

---

## [1.2.0] — 2026-05-19

### Dashboard redesign
- New design system: **Outfit** (UI) + **JetBrains Mono** (monospace) fonts via
  Google Fonts; shared CSS custom properties across dashboard and calibration page.
- **State pill** in header (next to brand) — colour-coded badge for SILENT /
  ACTIVE / THINKING / SPEAKING / PAUSED / MONITORING with full-opacity border.
- **Nav buttons** restyled as rounded rectangles (radius 8 px) to visually
  distinguish interactive controls from the round status pill; all buttons now
  have hover effects including `.on`-state buttons.
- **Calibrate** button moved to header row next to state pill for faster access.
- **Monitor Off** no longer styled as active when monitoring is off (redundant
  with state pill); `.on` hover rule added for Monitor On when active.
- Device status bar: larger font (12 px), brighter text colour for readability.
- Responsive breakpoints: 15 px / 42 px touch targets on phone ≤ 520 px;
  17 px / 38 px on monitor ≥ 900 px; `viewport-fit=cover` for notched phones.
- **Calibration page** redesigned to match dashboard: same header layout,
  device panel, button palette, SNR table, section headings, and cal mode
  toggles (Headset / Speaker / Auto highlight active choice in accent colour).

---

## [1.1.0] — 2026-05-19

### Added
- Dashboard **interrupt button** on "Zeebot is thinking…" line — cancels
  in-flight `gw.ask()` task via `asyncio.Task.cancel()`.
- Dashboard **✕ Stop** button on "Zeebot is speaking…" banner — stops TTS
  mid-sentence and saves text for resume.
- Dashboard **▶ Continue** button — resumes paused TTS via `/continue` route
  and `RealtimeSession._resume_from_http()`.
- `/interrupt`, `/continue` HTTP routes; `start_http_server` now receives the
  asyncio loop for thread-safe task cancellation.
- `_is_speaking`, `_current_think_task`, `_http_interrupt` global flags for
  cross-thread interrupt coordination.
- Voice commands: **"Zeebot start/stop monitoring"** toggle monitoring mode
  without going through the gateway (`MONITOR_ON_PHRASES` / `MONITOR_OFF_PHRASES`).
- `langdetect` integration for non-EN/ZH filtering of Latin-script hallucinations.
- History fallback for `message`-tool replies: when gateway returns a status
  token ("Sent.", "Done."), daemon fetches real content from `chat.history`
  instead of erroring.
- `__version__` constant in daemon for version tracking.

### Changed
- Wake/sleep/monitor phrases now checked **before** the language gate — fixes
  "Zeebot wake up" being dropped as non-English.
- Dashboard nav reordered: Clear Log → Restart → Gateway Reset → Calibration.
- "Reset" renamed **Clear Log**; separate **Gateway Reset** button added.
- **Restart** button fixed to use `launchctl kickstart -k` (was `systemctl`).
- Thinking entries now resolve in the log when any "system" entry follows them
  (fixes stale "thinking…" counter after gateway errors or cancellation).
- Removed automatic gateway restart on status-token responses — Gateway Reset
  is manual only.
- History fallback sleep increased 0.6 s → 1.2 s for tool-call persistence.
- Dashboard state banner now shows THINKING / SPEAKING / PAUSED states.

### Audio tuning
- `MIC_GAIN` 3.0 → 5.0×
- `MIC_GATE_PEAK` 300 → 20 (ambient noise floor)
- `MIC_GATE_MIN` 300 → 15
- OpenAI VAD `threshold` 0.45 → 0.35 (more sensitive)
- `prefix_padding_ms` 300 → 500 ms (capture speech onset)
- `silence_duration_ms` 1100 → 700 ms (faster end-of-utterance)
- WebRTC noise suppression aggressiveness 1 → 2
- `SPEAK_INTERRUPT_PEAK` floor 300 → 150
- `SPEAK_INTERRUPT_BLOCKS` 6 → 3 (150 ms sustained to interrupt)
- `INTERRUPT_SAFETY` factor 3.0 → 1.8×

---

## [1.0.0] — 2026-05-18

Initial Mac Mini fork of [openclaw-RealTimeTalk](https://github.com/w2ayz/openclaw-RealTimeTalk) (v1.3 Pi).

### Added
- CoreAudio device discovery via `sounddevice.query_devices()` (replaces `pactl`).
- Edge TTS as primary synthesiser, invoking the existing
  `~/.openclaw/workspace/skills/edge-tts/scripts/tts-converter.js`.
- macOS `say` as offline TTS fallback when Edge TTS times out or fails.
- ffmpeg-based audio decoder (`_decode_to_pcm`) — handles both Edge MP3 and
  `say` AIFF output, normalising to 24 kHz mono int16.
- `sounddevice.play()`-based output with speech-interrupt via mic-level polling.
- LaunchAgent template (`ai.openclaw.realtimetalk.plist`) and toggle script
  (`RealTimeTalk-toggle.sh`) wrapping `launchctl bootstrap/bootout/kickstart`.
- `RealTimeTalk-install-mac.sh` installer: brew dependency check, venv setup,
  OpenAI key precondition check, interactive device selection, plist install.
- Bluetooth-mic warning surfaced in dashboard `/device-status` (macOS SCO
  degradation when BT mic+speaker share a device).
- `_mac_notify()` helper for macOS Notification Center pop-ups on wake/sleep.

### Changed
- Removed PipeWire WebRTC AGC virtual source — CoreAudio handles input gain
  at the driver level; software `MIC_GAIN`/`MIC_GATE_PEAK` retained as
  fine-grained control.
- `speak()` rewritten end-to-end: Edge TTS primary, `say` fallback, ffmpeg
  PCM decode, sounddevice playback with interruptible monitoring.
- `run_speaker_calibration()` replaced — same FFT/SNR algorithm but no
  PipeWire sink switching or `paplay` / `aplay` subprocesses.
- Service-file editing helpers (`_update_service_*`) operate on the launchd
  plist (`plistlib`) instead of the systemd unit file.
- `--alsa-output` flag renamed to `--output-device` (accepts CoreAudio device
  index); old flag still accepted for compatibility but ignored.
- `--input-source` flag now logs a warning and is ignored — use `--input-device`.

### Removed
- All `pactl`, `pw-cli`, `paplay`, `aplay` subprocess calls from the daemon's
  audio paths.
- PipeWire WebRTC AGC config file management (`~/.config/pipewire/*.conf`).
- ALSA card-detection helpers (`_alsa_card_info`, `_find_usb_speaker_sink_name`).
- Piper TTS binary dependency (replaced by Edge TTS + `say`).
- systemd service file management (replaced by launchd plist editing).

### Known issues
- Some legacy HTTP routes still reference PipeWire concepts (sink switching,
  AGC source picker) and will log warnings on click. Mac-native equivalents
  for these are TODO; the core daemon flow is unaffected.
- `--data-format=LEI16@24000` flag to `say` was found unsupported on the
  current macOS — `say` now emits its default AIFF format, and ffmpeg decodes
  whatever it produces.
