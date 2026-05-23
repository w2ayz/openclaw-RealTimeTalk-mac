# Changelog

## [2.0.1] — 2026-05-23

### Fixed
- **Auto-sleep now cuts the OpenAI Realtime connection** instead of just going
  silent. After 10 min of inactivity the daemon speaks a goodbye, sets
  `_sleep_event`, and exits `session.run()` — closing the WebSocket and stopping
  mic streaming so no VAD/transcription tokens are billed while idle.
- `main()` outer loop detects the sleep exit (`_sleep_requested`) and blocks on
  `_wake_event` instead of reconnecting automatically. Pressing **Wake** on the
  dashboard (or calling `/wake`) sets the event and triggers an immediate
  OpenAI reconnect.
- Dashboard shows **SLEEPING** state pill (warm-grey) while disconnected from
  OpenAI due to auto-sleep, distinct from SILENT (which means connected but
  inactive).

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
