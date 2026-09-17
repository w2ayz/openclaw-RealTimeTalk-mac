# CLAUDE.md — RealTimeTalk (Mac fork)

- Two version-locked forks: this one (Mac, `sounddevice`/PortAudio,
  ElevenLabs → Edge → OpenAI → `say` TTS chain) and the Pi fork
  (github.com/w2ayz/openclaw-RealTimeTalk, PipeWire/Piper). Bump
  `__version__` in `RealTimeTalk-daemon.py` and add matching CHANGELOG
  entries in both when a fix or feature applies to both fork — port by
  adapting to each fork's idioms, not by cherry-picking the diff.

- Bash 3.2 (macOS system bash) in the install/toggle scripts: no
  `declare -A`, no `${var,,}`, and `"${ARR[@]}"` on an empty array trips
  `set -u` — use `"${ARR[@]+"${ARR[@]}"}"`.

- Before committing a change to `RealTimeTalk-daemon.py` (or any `.py`
  file here): run `git config core.hooksPath .githooks` once per clone —
  this is a manual, one-time step; nothing in this repo automates it,
  since `.claude/` (which could otherwise self-activate it via a
  `PreToolUse` hook) is gitignored entirely — see the bullet below for
  why. Once active, `.githooks/pre-commit` runs `ruff check --select
  F821,E9` on staged files. It exists because two real bugs shipped here
  from exactly this failure mode: v3.22.0's `_cli_stt_engine` referenced
  but never declared on the Pi fork, and v3.21.0's bare
  `time.monotonic()` with no top-level `import time` — both syntactically
  valid, both crashed at runtime, neither caught before commit. If it's
  not catching something it should, widen the `--select` list — don't
  bypass it with `--no-verify`.

- `.claude/` is fully gitignored — this repo is public, and `.claude/`
  can hold session artifacts (transcripts, worktrees created by
  `EnterWorktree`, scratch scripts) that must never be committed. A
  blanket `git add -A` from inside a Claude Code session is the failure
  mode this guards against — it very nearly happened on the Pi fork.
  Never narrow this rule to "just ignore the risky files" — track nothing
  under `.claude/` at all. The pre-commit gate above needs none of it to
  work; a `.claude/settings.json` PreToolUse convenience hook is fine to
  keep *locally, untracked*, but never stage or commit it here.

- No test suite exists yet beyond two standalone eval scripts
  (`test-gemini-transcribe.py`, `test_speak.py`). Verify runtime behavior
  manually: `RealTimeTalk-toggle.sh restart` + `tail -f
  /tmp/openclaw/realtimetalk.log`, or `venv/bin/python
  RealTimeTalk-daemon.py --list-devices` as a cheap "does it even import"
  smoke check. Note `--list-devices` exits before STT-engine resolution
  runs, so it would *not* have caught either bug above — that's what the
  pre-commit hook is for.

- `openclaw.json`'s `talk` schema has no `stt` key. STT engine choice
  lives in `~/.openclaw/workspace/rtt_stt_config.json` (the daemon's own
  config file, not `openclaw.json`) — see README's "STT engine selection"
  section.
