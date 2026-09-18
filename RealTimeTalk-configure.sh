#!/usr/bin/env bash
# RealTimeTalk-configure.sh — re-runnable setup for STT keys/engine, TTS
# keys/engine order, and STT custom vocabulary. Safe to run anytime after
# the initial install (RealTimeTalk-install-mac.sh) — it never touches
# Homebrew deps, the Python venv, audio device selection, or the LaunchAgent
# plist. Use it to add a key you skipped earlier, change the TTS engine
# order, or add more vocabulary terms.
#
# Usage:
#   bash RealTimeTalk-configure.sh

set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PY="$SKILL_DIR/venv/bin/python3"
OPENCLAW_JSON="$HOME/.openclaw/openclaw.json"
STT_CFG="$HOME/.openclaw/workspace/rtt_stt_config.json"
TTS_CFG="$HOME/.openclaw/workspace/rtt_tts_config.json"

# shellcheck source=RealTimeTalk-config-lib.sh
source "$SKILL_DIR/RealTimeTalk-config-lib.sh"

if [[ ! -x "$VENV_PY" ]]; then
    red "  ✗ venv not found at $VENV_PY — run RealTimeTalk-install-mac.sh first."
    exit 1
fi
if [[ ! -f "$OPENCLAW_JSON" ]]; then
    red "  ✗ $OPENCLAW_JSON not found — run RealTimeTalk-install-mac.sh first."
    exit 1
fi

bold "=== RealTimeTalk configure ==="
echo
echo "  Every step below can be skipped — press Enter to keep what's already"
echo "  there. This whole script is safe to re-run anytime:"
echo "    bash \"$SKILL_DIR/RealTimeTalk-configure.sh\""
echo "  Run it again later to add a key you skipped now, change the TTS"
echo "  engine order, or add more STT vocabulary."

run_stt_setup
run_tts_setup
run_vocabulary_setup

bold "=== Configure complete ==="
echo
read -r -p "Restart RealTimeTalk now to apply changes? [y/N]: " DO_RESTART
if [[ "$DO_RESTART" =~ ^[Yy] ]]; then
    bash "$SKILL_DIR/RealTimeTalk-toggle.sh" restart
else
    yellow "  → Not restarted. Changes take effect on next restart:"
    yellow "    bash \"$SKILL_DIR/RealTimeTalk-toggle.sh\" restart"
fi
echo
green "Re-run this anytime: bash \"$SKILL_DIR/RealTimeTalk-configure.sh\""
