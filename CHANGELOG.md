# Changelog

## [1.0-mac] — 2026-05-18

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
