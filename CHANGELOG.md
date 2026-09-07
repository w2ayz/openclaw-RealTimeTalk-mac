# Changelog

## [3.21.2] — 2026-09-07

### Fixed

- **`/continue` after an interrupted `/continue` hung silently — you couldn't resume a reading.** The 3.21.0 streaming refactor split `speak()` into `speak()` + `_synthesize()` + `_play_audio()` and **dropped the `_speak_lock.release()` from `speak()`'s `finally`**. The first `speak()` call (any `/continue`, `/replay`, wake confirmation, or one-off readout) acquired `_speak_lock` and never let go; the *next* `speak()` blocked forever on the acquire — no synthesis, no playback, no log line. Streamed replies (`StreamingSpeaker._playback_worker`) were unaffected because they release the lock correctly, which is why it only showed up on the second consecutive resume. `speak()` now releases the lock in its `finally` (guarded, so it can't double-release).
- **Barge-in during a streamed reply left the synthesis worker running.** A mid-reply interrupt stopped playback but never signalled `StreamingSpeaker`'s synth worker, so it kept calling `_synthesize()` on every remaining sentence — each a 10–15 s ElevenLabs request billing real quota — queuing audio nobody would play, for 20+ seconds after the reading stopped. The playback worker's interrupt path now sets `_gen_stop` and drains the queues; at most one already-in-flight sentence completes.
- **A long or quiet streamed reply could interrupt itself on its own echo.** `_play_audio`'s per-tick coupling EMA was allowed to drift *downward* unbounded, and the streaming pipeline hands each sentence's ending `coupling_now` to the next sentence as its skip-guard threshold basis — so the barge-in threshold ratcheted down sentence over sentence (observed: 1191 → 626 in two sentences) until the speaker's own output tripped it (`peak=708 threshold=626`). `coupling_now` is now floored at the guard's honest measurement: the EMA may still rise for genuinely louder passages but never falls below what the 1 s guard actually measured.
- **`/continue` and `/replay` with nothing paused now log the no-op** instead of silently redirecting to the dashboard.

## [3.21.1] — 2026-09-07

### Added

- **POST `/speak` — OpenClaw can now push long text to the speaker reliably.** The local-only `/speak` endpoint previously accepted text only via the URL query string (`?text=...`), which mangles `&`, `#`, `+` and other characters that appear in real news copy, and was undocumented for the agent. It now also accepts the text in the request body — form-encoded `text=...` or raw UTF-8 — so an OpenClaw agent can read out a gathered news roundup with `curl -s -X POST --data-urlencode "text=<copy>" http://127.0.0.1:19000/speak`. GET still works unchanged. The endpoint works even while RTT is in auto-sleep.
- **README.md / DEPLOYMENT.md readout sections updated to the POST syntax** — both still showed the old GET-only `?text=` + manual-URL-encode curl one-liner and a "keep it to a spoken-length summary" caveat; the embedded `TOOLS.md` snippet installers copy into the agent workspace now uses `--data-urlencode` and notes long text streams (reading starts within the first sentence or two).

### Changed

- **`/speak` now reads through `StreamingSpeaker`** (new `_queue_speak`, shared by GET and POST) instead of the monolithic `speak()` path, so a long pushed text starts being read after its first sentence is synthesised rather than after the whole text is processed. Kept in lockstep with the Pi fork's v3.21.1.

## [3.21.0] — 2026-09-06

### Added

- **Streaming TTS — the reply starts being spoken while OpenClaw is still writing it.** The daemon no longer waits for the complete reply (chat-final) before synthesizing: a new `StreamingSpeaker` consumes the gateway's incremental `assistant` stream events and starts voicing the reply as soon as a configurable start threshold is met, overlapping generation with speech. The pipeline reuses the existing synthesis and playback machinery, refactored into two shared halves — `_synthesize()` and `_play_audio()` — so `speak()` keeps its exact previous behavior for `/speak`, `/continue`, `/replay`, wake confirmations, and one-off readouts.
  - New `GatewayClient.ask_stream()` async generator: races the gateway's stream queue against the reply future and yields `("delta", data)` events, then `("final", text)` resolved with the exact same fallbacks as `ask()` (chat-final → assistant stream → status-token → chat.history → stale-reply rejection). Codex `message`-tool turns (no streaming) fall through to today's whole-reply path automatically.
  - `TTS_MAX_SENTENCE` (env `RTT_TTS_MAX_SENTENCE`, default 500 chars) caps how long a single long sentence can stall the pipeline — the buffer flushes at the last clause boundary.
  - `replace` stream events (model rewrites its answer) reset the speaker mid-turn and re-arm it for the new text.

### Changed

- **New `RTT_TTS_START` environment variable** controls when TTS first starts on a reply (parsed once at startup):
  - `sentence` (default) — wait for one complete sentence before starting speech.
  - `time:N` — start after N seconds of streamed text.
  - `chars:N` — start once N characters have accumulated.
  - `words:N` — start once N words have accumulated.
  After the first release, the pipeline flushes on sentence boundaries as they complete, so a long reply keeps flowing.
- **Live "now reading" cue on the dashboard.** A `#nowreading` panel (outside the 3s-polled `#log`) streams the current speaking position from a new `/speech` SSE endpoint: the sentence being read with the in-progress word highlighted, plus a progress bar (`pos/tot`) that trails the live reply text by however long synthesis+playback takes. Shown for streamed replies and manual readouts (`/speak`, `/continue`, `/replay`) alike.
- **Voice barge-in still works across streamed sentences.** The playback worker measures the mic↔speaker coupling on the first sentence's guard and passes it to subsequent parts (`skip_guard`), so barge-in is never deaf for a full guard at the start of each sentence, and an `on_tick` callback reports read-along position.
- **`speak()` refactored** into `_synthesize()` (markdown strip → per-script split → TTS chain → concatenation → volume) + `_play_audio()` (sounddevice playback, PTT routing/keying, coupling monitor, Continue/Replay bookkeeping, auto-reduce) with no behavior change; the streaming pipeline reuses both. `_synthesize` gained `pad_lead`/`pad_tail` so streamed sentences don't get a silence gap between them — only the first sentence is lead-padded and the last tail-padded. Radio mode keys PTT once for the whole streamed turn.

## [3.20.1] — 2026-09-05

### Fixed
- **`_ptt_open()` flooded the log with an identical warning every 3 seconds when no radio was attached.** `_radio_hotplug_watcher` polls on a 3s loop and calls `_ptt_open()` whenever no PTT port is live; each call unconditionally logged `Radio PTT unavailable (no known radio interface found) — PTT disabled` (or the pyserial-missing / port-open-failed variants). On a Mac with no AIOC that's ~1,200 lines/hour of pure noise. `_ptt_open()` now logs the unavailable reason at `warning` **once per absent streak** (new `_ptt_unavail_logged` flag, cleared on a successful port open) and at `debug` — suppressed by the daemon's `INFO` level — thereafter. A radio plug-in still logs its `PTT ready` line, and a later unplug logs exactly one fresh warning.

## [3.20.0] — 2026-09-02

### Changed
- **Auto-sleep (10 min idle) is now text-only.** `_idle_watcher` no longer speaks "Going to sleep. Press Wake to reconnect." — it just writes the `Auto-sleep after N min idle. Press Wake to reconnect.` line to the dashboard log and flips the state pill to **SLEEPING**. The spoken line fired during quiet time (often an empty room) and was more startling than useful. The explicit "sleep phrase" voice command is unchanged and still speaks its confirmation.
- **TTS engine chain is now ElevenLabs → Edge TTS → OpenAI TTS → macOS `say`.** Edge TTS moves from unused-legacy to the first fallback after ElevenLabs: it's free, needs no API key, and speaks Chinese with native `zh-CN-XiaoxiaoNeural` / English with `en-US-AriaNeural` — for a bilingual (EN + ZH) user that beats OpenAI's English-accented Mandarin. `speak()` calls the new `_edge_tts_to_pcm()`, which splits the reply by script (`_split_by_script`) so each run uses its native voice, and abandons the whole engine (falls through to OpenAI TTS) if any segment fails rather than playing a half-rendered reply. OpenAI TTS is now strictly the paid network fallback.

### Fixed
- **Edge TTS skill path was a fragile hardcode.** Both the daemon (`EDGE_TTS_SCRIPT`) and the installer hardcoded `~/.openclaw/workspace/skills/edge-tts/scripts/tts-converter.js` as two independent absolute strings, each keyed off `$HOME` — a relocated OpenClaw workspace broke them, and the installer's `-f` check only proved the `.js` existed, not that `npm install` had run. Now:
  - The daemon resolves the path at import via `_resolve_edge_tts_script()`: `$RTT_EDGE_TTS_SCRIPT` → sibling `skills/edge-tts/scripts/tts-converter.js` (relative to the daemon file) → `$OPENCLAW_WORKSPACE/skills/edge-tts/...` → the official `~/.openclaw/workspace/skills/edge-tts/...`. First hit wins; missing Edge just drops the chain to OpenAI TTS.
  - `RealTimeTalk-install-mac.sh` step 2 resolves the same way, runs `npm install --omit=dev` in the skill's `scripts/` if `node_modules` is absent, verifies the script runs, and — key change — **treats a missing skill as a warning, not a fatal `exit 1`** (it's a fallback engine, not a hard dependency).
  - The installer writes the resolved path into the LaunchAgent plist as `RTT_EDGE_TTS_SCRIPT` (new `EnvironmentVariables` entry + `__EDGE_TTS_SCRIPT__` template placeholder), so the installer and the running daemon always agree on the location.
- **DEPLOYMENT.md §3.5 documented a hand-rolled Edge TTS install** (hand-write `package.json`, `npm install --prefix` at the skill root, hand-copy `tts-converter.js` "from your c2e-slack repo") that put `node_modules` in the wrong directory and depended on an unrelated repo. Replaced with `clawhub install edge-tts` / a repo clone to the official path, matching the skill's own `skill-info.json` (`install: { path: "scripts" }`).

## [3.19.0] — 2026-08-30

### Added
- **`RealTimeTalk-toggle.sh disable` / `enable`** — a persistent mic kill-switch. `disable` runs `launchctl bootout` + `launchctl disable` (so it stays off across reboots, not just until next login like `stop`), reaps the daemon directly if an older wrapper orphaned it, then verifies no RTT process is running and port 19000 is free before reporting success. `enable` runs `launchctl enable` (required before `bootstrap` — launchd silently refuses a disabled job) + `bootstrap` + `kickstart`, then polls `/status` until the daemon answers. `status` now also reports when the LaunchAgent is disabled. README's "Disabling RealTimeTalk" section leads with these; the raw `launchctl` sequence is kept as a collapsed reference.

### Fixed
- **DEPLOYMENT.md still said `toggle.sh restart` uses `kickstart -k` and "does NOT reload a changed plist".** Stale since 3.18.1 — `restart` switched to `bootout` + `bootstrap` and does reload the plist. Updated there and in the troubleshooting table.

## [3.18.1] — 2026-08-30

Mac-only packaging/lifecycle changes — no daemon behavior change. (Groups in commit `3f924b7`, which shipped unversioned.)

### Changed
- **The mic-permission wrapper app is renamed `ZeebotTalk.app` → `RealTimeTalk.app`**, bundle identifier `ai.openclaw.zeebottalk` → `ai.openclaw.realtimetalk`, to match the daemon / repo / LaunchAgent naming. **This is a new app identity to macOS:** after rebuilding and repointing the LaunchAgent's `ProgramArguments`, Microphone access must be re-granted once (System Settings → Privacy & Security → Microphone, or launch `RealTimeTalk.app` from Finder once to trigger the prompt) — voice input stays dead until then. The stale "ZeebotTalk" row in that list can be removed afterward (`tccutil reset Microphone ai.openclaw.zeebottalk`).

### Fixed
- **`ZeebotTalk.app` wrapper orphaned the daemon on `launchctl bootout`.** The Swift wrapper didn't forward `SIGTERM`/`SIGINT` to its Python child, so unloading the LaunchAgent left the daemon reparented to `launchd` (PID 1) — still holding port 19000 and the mic — and the next launch crash-looped on `Address already in use`. The wrapper now forwards a graceful terminate to the child (3s grace, then `SIGKILL`) and re-raises the original signal so `launchctl kickstart -k`'s respawn still works. Verified live: `bootout` now brings the daemon fully down within ~15–20s with no manual `pkill`.
- **`RealTimeTalk-build-wrapper-mac.sh` icon step no longer hard-fails without librsvg.** Falls back to macOS `sips` (native SVG decode on macOS 13+) when `rsvg-convert` is absent, and a failed `brew install librsvg` just skips the custom icon instead of aborting the build.
- **Build script executed backticked commands from its own Swift comments.** The `launcher.swift` heredoc is unquoted (needs `$SKILL_DIR` etc. expansion), and comments added in `3f924b7` contained `` `launchctl …` `` in backticks — the shell ran them (harmlessly, no args) at every build. Backticks removed from those comments.

### Added
- **README: "Disabling RealTimeTalk (make it inert)"** — `launchctl bootout` + `launchctl disable` to stop it now and across reboots without uninstalling, verification commands, and re-enabling (`launchctl enable` must precede `bootstrap`).
- **`RealTimeTalk-toggle.sh restart`** now does `bootout` + `bootstrap` (with a retry loop for launchd's transient teardown error) instead of `kickstart -k`, so edits to the plist (device flags, persisted `--mic-gate`) are actually picked up.

## [3.18.0] — 2026-08-28

Kept in lockstep with the [Pi fork](https://github.com/w2ayz/openclaw-RealTimeTalk)'s v3.18.0 — same change, released on both at once (Mac was on 3.17.0, Pi on 3.17.1; both land on 3.18.0).

### Changed
- **`/voice-enroll`: the "Clear this device's profile" button is now separated from Save/Test into its own red-bordered "Danger zone" card**, with a line explaining what clearing costs (re-recording all three samples). Previously it sat inline right next to Save, one slip away from wiping an enrolled profile.
- **Clearing a profile now asks for confirmation inline instead of via a native `confirm()` dialog.** Clicking any Clear button (active device, radio profile, or a row under "Other enrolled devices") swaps it in place for a "Delete voice profile for …? This can't be undone." prompt with explicit **Yes, clear** / **Cancel** buttons; only "Yes, clear" issues the `/voice-enroll/clear` request. The native `confirm()` was replaced partly because it can wedge headless/automated browser sessions.

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
