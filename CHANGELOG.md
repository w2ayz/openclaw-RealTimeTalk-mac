# Changelog

## [3.17.0] — 2026-08-20

Jumps straight from 3.15.0 to 3.17.0 to match the [Pi fork](https://github.com/w2ayz/openclaw-RealTimeTalk)'s version number — same precedent as the 3.12.6→3.14.0 jump: no standalone 3.16.0 ever existed on this fork, this port brings across both Pi's v3.16.0 feature and its v3.17.0 fixes in one commit.

### Added
- **Continue / Replay / Cancel buttons** on the dashboard's paused banner, replacing the old single Continue link (which replayed the whole message from the top regardless of what "Continue" implied). Continue now does a real resume: `speak()` estimates how far into playback it got when interrupted (from the same 50ms-tick position the barge-in monitor already tracks), maps that to a character offset in the text, and rounds back to the start of whichever sentence was in progress (`_sentence_start_before()`) — so it picks up close to where it left off instead of replaying everything. Replay plays the whole message again from the top (the old Continue behavior). Cancel discards the paused state with no replay. Deliberately not named `/restart` — that path already exists (nav bar) for restarting the RealTimeTalk daemon itself via `launchctl kickstart`, and since `do_GET`'s `elif` chain matches whichever handler is defined first, reusing it would have silently shadowed the new route.

### Fixed
- **`/speak` never saved resume state.** The endpoint called `speak()` without `resumable=True`, so an interrupted `/speak` readout had nothing for Continue/Replay to act on — silent no-op on both buttons for that content. Now passes `resumable=True`, matching the main conversational reply.
- **Two concurrent `speak()` calls raced on shared state.** `_is_speaking` and `_http_interrupt` are single global flags with no serialization between calls; two overlapping `speak()` calls (e.g. two `/speak` requests close together) could have the shorter one's cleanup clear `_is_speaking` while the longer one was still playing, and both writing `_paused_speech` unpredictably. `speak()` is now serialized behind a module-level lock so a second item waits for the first to fully finish before it starts.
- **A queued item could auto-play over an unresolved pause and wipe it.** Same lock also holds a new `speak()` call back entirely while `_paused_speech` is set (re-checked after each lock acquisition, not just once), so an unrelated item queued behind an interrupted reading can't start — and finish normally, clearing the still-unresolved pause as a side effect — before the pause is resolved via Continue, Replay, or Cancel.
- **Stop was silently deferred for the first second of any reply.** The acoustic-coupling calibration guard's `continue` skipped straight past the `_http_interrupt` check for its full `INTERRUPT_GUARD_TICKS` window, so clicking Stop early did nothing until the guard ended. The explicit interrupt check now also runs during the guard window; only the mic-based auto-barge-in threshold stays gated by it.
- **`/continue` silently no-op'd whenever RTT was asleep.** It routed playback through `sess._resume_from_http()`, which needs a live `RealtimeSession`; `session_ref[0]` is `None` until the device has actually been woken once, so any `/speak` readout interrupted while asleep (the common case) made the button do nothing with no error. `/continue` and the new `/replay` now call `speak()` directly in a plain thread, the same pattern `/speak` already used, with no session dependency. `_resume_from_http()` removed as dead code.
- **A voice-triggered reply could get read aloud twice.** After using `/speak` to read something aloud on a voice turn, Zeebot's own normal reply for that turn was *also* spoken automatically on top of it — any non-empty reply on a voice turn gets voiced, not just a long one. Now tracked structurally: `_speak_used_this_turn` records whether `/speak` fired during the current turn, and if so the turn's reply is logged to the dashboard as a de-emphasized status line (same styling as other system/meta entries) instead of being spoken.
- **Mic audio wasn't muted (at capture time) during `/speak`/Continue/Replay playback.** Only `self._busy` gated whether mic input got forwarded to OpenAI's transcription API, and those paths never set it (unlike a normal reply), so Zeebot's own voice kept getting streamed and transcribed — wastefully, though harmlessly, since a separate downstream check (`_is_speaking`) already discarded any resulting transcript before acting on it. Mic callback now also checks `_is_speaking` directly, closing the gap for all speaking paths without touching `_mic_level_current` (updated earlier in the same callback), so voice barge-in during any of them is unaffected.

## [3.15.0] — 2026-08-19

### Fixed
- **`/speak?text=...` crashed on every call.** This local-only endpoint —
  lets any process on the machine (an OpenClaw agent, a script, etc.) push
  arbitrary text through the normal `speak()` TTS pipeline on demand,
  independent of the voice conversation flow — called an undefined
  `_json(self, code, obj)` helper. The `speak()` call itself still fired
  correctly (confirmed live: the text was actually spoken), but the
  handler then crashed trying to send the JSON confirmation response, so
  every caller saw a reset connection with no way to tell the call had
  actually worked. Added the missing helper — named `_send_json`, not
  `_json`: dozens of unrelated branches elsewhere in the same `do_GET`
  method do `import json as _json` as a local module alias, and Python
  treats any name assigned anywhere in a function as local to the whole
  function, so a helper literally named `_json` gets shadowed by those
  (unreached) local imports and hits the identical `UnboundLocalError` —
  confirmed live this was the actual second failure mode.

### Added
- **README.md / DEPLOYMENT.md**: documented `/speak` under "Control" /
  "Day-to-day control", including the exact `TOOLS.md` snippet to add to
  an OpenClaw agent's workspace so it knows the capability exists and
  reaches for it when a keyboard-typed request should be delivered back
  through RTT's voice instead of as text — e.g. "look into X and read me
  what you find." Verified live end-to-end: OpenClaw calling `/speak`
  after finishing a text-triggered task, RTT speaking the result.

## [3.14.0] — 2026-08-18

### Fixed
- **Dashboard flashed the whole page every ~3 seconds.** The page used
  `location.reload()` on a 3s timer to keep the state pill, nav, device
  panel, banner, and conversation log fresh — a full reload re-fetched
  Google Fonts/CSS and repainted everything from scratch each time.
  Ported from Pi v3.12.6: factored the dynamic pieces out of the
  full-page render into a shared `_dashboard_dynamic()` helper, added a
  `/dashboard-frag` JSON endpoint backed by it, and rewrote the client-side
  timer to poll that endpoint and patch only the changed elements in
  place. Adapted (not a direct copy) to this file's own device-banner/
  hover-hint logic, which differs from Pi's — Mac shows button hints in a
  dedicated `#hzone` element rather than overwriting the banner's text, so
  the hover-hint listeners were switched to event delegation (survive the
  nav being replaced every poll) and now also pause the poll itself while
  hovering, so a tooltip or the hovered button isn't yanked out from
  under the cursor mid-display. Verified live: `/dashboard-frag` returns
  correct state/HTML fragments and updates in step with real daemon state
  changes (confirmed via /wake and /sleep).
- **Voice enrollment recording failing after a device disconnect** (stale
  in-process device index). `_fresh_device_label_and_resync()` — the
  self-heal that keeps the dashboard's device display accurate after a
  hot-unplug — wrote a fresh-SUBPROCESS-resolved device index directly
  into `_selected_input_device[0]`/`_selected_output_device[0]`, the same
  globals used to actually open the live InputStream/record audio
  in-process. Subprocess and in-process PortAudio device numbering aren't
  guaranteed to match once this process's own cache is stale. Confirmed
  live: after AIOC was unplugged, the dashboard correctly displayed "USB
  PnP Sound Device (#1)", but the daemon was stuck in a permanent
  crash-reconnect loop (`Invalid number of channels [PaErrorCode -9998]`)
  because this process's own index 1 pointed at a different physical slot
  with a different channel count. Voice enrollment recording hit the
  identical bug (`_record_pcm_blocking` defaults to the same global).
  Fixed by re-resolving by NAME via the existing in-process
  `_resolve_device_by_name()` (matches `/device-set`'s already-proven
  pattern), with a reinit-and-retry fallback, instead of trusting the
  fresh subprocess's index number directly.
- **DTMF wake from Monitoring left the dashboard stuck showing
  Monitoring.** Ported from Pi v3.12.4/v3.12.5. `_dtmf_force_active`'s
  handling (in both `_send_mic` and its `_handle_transcript`
  belt-and-suspenders copy) set `self._active=True` without clearing
  `self._monitoring`, unlike every other wake path — so DTMF `123`
  received while in Monitoring (e.g. after DTMF `456`) left the session
  active+monitoring simultaneously. Also clears the persisted monitoring
  flag in the WAKE digit handler so a reconnect doesn't restore it.
  Verified live: `456` then `123` now correctly logs "Monitoring
  stopped" / "Voice activated" and the dashboard's Monitor button returns
  to OFF.

## [3.13.0] — 2026-08-18

### Added
- **DTMF remote control**, ported from Pi's always-on `_dtmf_listener` thread
  (previously Mac only had `dtmf_monitor.py`'s standalone training/monitor
  CLI, with no live wiring back into the daemon). Transmit a digit sequence
  over the radio to control sleep/wake/monitor state without touching the
  dashboard: `123` Wake (goes fully Active), `321` Sleep (Silent, still
  connected), `987` Deep Sleep (disconnects from OpenAI immediately, skips
  the 10-min idle wait), `789` Wake-Silent (reconnects from Deep Sleep into
  Silent — NOT Active), `456`/`654` Monitor ON/OFF. Runs unconditionally
  whenever a radio interface is connected, decoding via the shared AIOC RX
  tap (same one Monitor/EchoTest already use) rather than opening a third
  independent stream. Requires DTMF profiles already trained via
  `dtmf_monitor.py --train` — silently disabled if none exist.

### Fixed
- **DTMF digit decode was too slow for real multi-digit sequences when
  running inside the full daemon**, even though decode logic is otherwise
  identical to `dtmf_monitor.py`'s own proven Goertzel/profile-matching
  algorithm. `dtmf_monitor.py`'s standalone process (only 3 threads) caught
  every digit; the in-daemon listener — competing for the GIL with the
  asyncio loop, WebRTC AGC, HTTP server, and other radio threads — dropped
  digits under the same fast keying, confirmed live. Root cause: the
  Goertzel recurrence is a raw Python loop (can't vectorize), run twice per
  candidate digit for all trained digits; at native 48kHz that's ~115,000
  loop iterations per decode attempt, up to 40/second while squelch is
  open — enough to fall behind under real contention. Fixed by decimating
  to ~8kHz before Goertzel (frequency resolution comes from the 100ms
  window's *duration*, not its sample rate, so this doesn't lose
  discriminating power between DTMF tones) — the same mitigation Pi's own
  `_dtmf_listener` already uses, for the same reason.
- **The middle digit of a fast 3-digit sequence was still getting dropped**
  even after the decimation fix, e.g. `789` decoding as `7,9` with no `8`.
  The decoder required a digit to read identically for 3 consecutive 25ms
  polls (75ms) before accepting it; a digit sandwiched between two
  transitions often didn't get 75ms of clean tone before the next one
  started. Reduced to 2 consecutive polls (50ms) — confirmed live this
  reliably catches all three digits of `789`/`987`/`123`/`321`/`456`/`654`
  transmitted at normal keying speed, still enough of a debounce to reject
  noise.

## [3.12.0] — 2026-08-13

Version-number alignment with the [Pi fork](https://github.com/w2ayz/openclaw-RealTimeTalk)
— no functional changes here. The Pi fork ported this session's applicable
fixes (owner-only wake-confirmation skip, the `strip_markdown()` backtick
bug, the self-interrupt threshold decay fix) and bumped to v3.12.0 to match;
this repo jumps its own counter from 3.9.3 to the same number so `vX.Y.Z`
means the same release point on both platforms going forward. Mac-specific
work from this session (ElevenLabs-as-primary, CJK unit reading, the LG
ULTRAWIDE output blocklist) doesn't apply to the Pi fork and stays as-is here.

## [3.9.3] — 2026-08-12

Owner-only wake UX, TTS text fidelity (dropped/mispronounced numbers,
English leaking into Chinese replies), and a self-interrupt threshold
that decayed over long replies until it falsely triggered on Zeebot's
own voice. Also switches ElevenLabs to the primary TTS engine for all
replies (previously CJK-only), model `eleven_v3`.

### Changed
- **Owner-only mode skips the wake confirmation round-trip.** Previously
  every wake phrase — even in owner-only mode — got a "Yes?" and waited
  for a second confirming utterance before activating, to filter
  unauthenticated false-positives. Once voice biometric verification
  already confirms the speaker is the enrolled owner, that confirmation
  step is redundant; owner-only now activates immediately on a verified
  wake phrase.
- **ElevenLabs is now the primary TTS engine for every reply**, not just
  CJK text. Default voice/model: `eleven_v3` / "Lily - Velvety Actress".
  OpenAI TTS remains the fallback on network/key failure.

### Fixed
- **`strip_markdown()` deleted backtick-wrapped content instead of
  keeping it.** Unlike the adjacent bold/italic regexes (which correctly
  preserve inner text via a capture group), the backtick regex replaced
  the whole span — including its content — with an empty string. Replies
  routinely wrap numeric values in backticks (e.g. `` `72°F` ``), so
  temperatures, percentages, and other figures were being silently
  erased from spoken replies entirely rather than mispronounced.
- **Percent/degree symbols now spelled out before TTS**, since `eleven_v3`
  skips ElevenLabs' usual automatic text normalization and read raw `%`/
  `°F`/`°C` incorrectly. New `_preprocess_units()`, mirroring the existing
  `_preprocess_zh_time()`/`_preprocess_acronyms()` pipeline. Branches on
  language like `_preprocess_zh_time`: Chinese-language replies get
  Chinese unit words (`华氏度`/`摄氏度`/`百分之`, with `百分之` correctly
  placed before the number per spoken word order) instead of code-switching
  into English mid-sentence (previously "白天最高大概 72 degrees
  Fahrenheit"); everything else gets spelled-out English.
- **Self-interrupt threshold silently decayed below its own guard
  measurement on long replies**, eventually causing an ordinary loud
  syllable in Zeebot's own voice to falsely trigger a "someone spoke"
  interrupt. Root cause was two-fold: the continuous EMA-based coupling
  tracker (added in Pi's v3.8.0 port) updated from any tick above a flat
  200-sample floor, including quiet passages where the mic/output ratio
  is dominated by room noise floor rather than real echo; and even after
  gating that, the initial guard-period measurement is a max-over-max
  ratio across a full second — statistically always ≥ any later per-tick
  sample — so unclamped continuous tracking would still trend downward
  over any long-enough reply. Fixed by requiring learning ticks to be
  comparably loud to the reply's peak (≥30%), and by never letting the
  threshold drop below its guard-measured baseline (it can still rise if
  echo genuinely gets louder later in the reply). Confirmed live: before
  the fix, threshold decayed 25-72% within seconds and triggered false
  interrupts on 20-25s replies; after, three consecutive long replies
  (20s/37s/83s) played through with zero false interrupts.

## [3.9.2] — 2026-08-12

Mac-only patch. Adds configurable agent name and wake phrase so each
deployment can brand the daemon to its local OpenClaw agent (e.g. Grogu,
Aria, Zeebot) without editing source. The default remains **Zeebot**,
preserving full backwards compatibility for existing installs that don't
pass the new flags.

### Added
- **`--agent-name <name>` flag.** Sets the agent's display name in the
  dashboard HTML, conversation log, startup log message, and all
  voice-command phrase sets (wake, sleep, monitor, continue,
  owner-only). Default: `Zeebot`.
- **`--wake-phrase <phrase>` flag.** Overrides the primary wake phrase.
  Optional — omit it and the wake phrase derives automatically as
  `<name> wake up`. When supplied, `<name> wake up` is kept as an
  additional recognised phrase alongside the override, so the natural
  form always works regardless of what custom phrase is configured.
- **Installer prompts for agent name and wake phrase.** The installer
  now asks for both after the device prompts (press Enter for
  defaults). The chosen values are written as `--agent-name` /
  `--wake-phrase` flags directly into the LaunchAgent plist — no
  source editing required per deployment.
- All phrase sets rebuilt at startup from the configured name: `WAKE_PHRASES`,
  `SLEEP_PHRASES`, `MONITOR_ON_PHRASES`, `MONITOR_OFF_PHRASES`,
  `CONTINUE_PHRASES`, `OWNER_ONLY_ON_PHRASES`, `OWNER_ONLY_OFF_PHRASES`.
  Name-agnostic phrases (e.g. `"real time talk on"`) are preserved as-is.

### Fixed
- `EXTRA_ARGS[@]: unbound variable` in the installer when all prompts
  were left blank (system defaults selected). Root cause: bash 3.2's
  `set -u` treats an empty array as unbound. Fixed with the standard
  bash 3.2-safe expansion idiom:
  `"${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}"`
- "Session active — speak now (routed through **Zeebot** / OpenClaw)"
  startup log line was hardcoded; it now uses the configured agent name.

### Deployment notes
- **Existing installs:** no action required — `--agent-name` defaults to
  `Zeebot`, behaviour is identical to 3.9.1.
- **To switch agent name post-install:** edit the LaunchAgent plist to
  add `--agent-name <name>` (and optionally `--wake-phrase`) to
  `ProgramArguments`, then do a full `bootout`+`bootstrap` (not
  `kickstart`). See DEPLOYMENT.md §8.

---

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
  package's native loader — now included in the LaunchAgent plist
  template's `EnvironmentVariables`. Setting it via `os.environ` *after*
  process start does not retroactively affect ctypes' library search, so
  it has to be set at exec time; a launcher that execs the daemon as a
  child process (see `RealTimeTalk-build-wrapper-mac.sh`) still passes
  this through correctly as long as it forwards its own inherited
  environment rather than constructing a stripped one from scratch.
  Cosmetic only either way — PTT/audio routing/DTMF do not depend on it.
- **If you edit the LaunchAgent plist directly**, `launchctl kickstart -k`
  restarts the process but does not reload a changed plist file from
  disk — use `launchctl bootout gui/$(id -u)/ai.openclaw.realtimetalk`
  followed by `launchctl bootstrap gui/$(id -u) <plist path>` to pick up
  edits (confirmed live: a `kickstart -k` after adding
  `DYLD_LIBRARY_PATH` to the plist kept running the old environment
  definition for over an hour of otherwise-successful restarts).

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
