#!/usr/bin/env bash
# RealTimeTalk-install-mac.sh — Install the RealTimeTalk daemon on macOS.
#
# Steps:
#   1. Verify Homebrew dependencies (portaudio, ffmpeg, node)
#   2. Verify Edge TTS skill is installed
#   3. Create Python venv and install dependencies
#   4. Verify openai.apiKey is configured in openclaw.json
#   5. List audio devices and prompt user for input + output device indices
#   6. Render and install the LaunchAgent plist
#   7. Load the agent

set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SKILL_DIR/venv"
VENV_PY="$VENV_DIR/bin/python3"
DAEMON_PY="$SKILL_DIR/RealTimeTalk-daemon.py"
PLIST_TEMPLATE="$SKILL_DIR/ai.openclaw.realtimetalk.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/ai.openclaw.realtimetalk.plist"
LABEL="ai.openclaw.realtimetalk"
OPENCLAW_JSON="$HOME/.openclaw/openclaw.json"
EDGE_TTS="$HOME/.openclaw/workspace/skills/edge-tts/scripts/tts-converter.js"

red()    { printf "\033[31m%s\033[0m\n" "$*"; }
green()  { printf "\033[32m%s\033[0m\n" "$*"; }
yellow() { printf "\033[33m%s\033[0m\n" "$*"; }
bold()   { printf "\033[1m%s\033[0m\n" "$*"; }

bold "=== RealTimeTalk-mac installer ==="
echo

# ── 1. Homebrew dependencies ─────────────────────────────────────────────────

echo "Checking Homebrew dependencies..."
for pkg in portaudio ffmpeg node hidapi; do
    if brew list "$pkg" >/dev/null 2>&1; then
        green "  ✓ $pkg installed"
    else
        yellow "  → installing $pkg"
        brew install "$pkg"
    fi
done
echo

# ── 2. Edge TTS skill ────────────────────────────────────────────────────────

if [[ ! -f "$EDGE_TTS" ]]; then
    red "  ✗ Edge TTS skill not found at $EDGE_TTS"
    red "    Install the openclaw edge-tts skill first."
    exit 1
fi
green "  ✓ Edge TTS skill present"
echo

# ── 3. Python venv ───────────────────────────────────────────────────────────

if [[ ! -x "$VENV_PY" ]]; then
    echo "Creating Python venv at $VENV_DIR..."
    /usr/bin/python3 -m venv "$VENV_DIR"
fi
echo "Installing Python deps (see requirements.txt)..."
"$VENV_PY" -m pip install --quiet --upgrade pip
"$VENV_PY" -m pip install --quiet -r "$SKILL_DIR/requirements.txt"
green "  ✓ venv ready"
echo

# ── 4. openclaw.json: openai apiKey precondition ─────────────────────────────

if [[ ! -f "$OPENCLAW_JSON" ]]; then
    red "  ✗ $OPENCLAW_JSON not found"
    exit 1
fi

HAS_KEY=$("$VENV_PY" - <<PY
import json
cfg = json.load(open("$OPENCLAW_JSON"))
k = cfg.get("talk", {}).get("providers", {}).get("openai", {}).get("apiKey", "")
print("yes" if k else "no")
PY
)

if [[ "$HAS_KEY" != "yes" ]]; then
    red "  ✗ Missing OpenAI API key in $OPENCLAW_JSON"
    echo
    echo "  The Realtime API requires a regular OpenAI API key. Add this to openclaw.json:"
    echo
    cat <<'EXAMPLE'
  "talk": {
      "providers": {
          "openai": { "apiKey": "sk-..." }
      }
  }
EXAMPLE
    echo
    echo "  Then re-run this installer."
    exit 1
fi
green "  ✓ openai.apiKey present in openclaw.json"
echo

# ── 5. Audio devices ─────────────────────────────────────────────────────────

bold "Available CoreAudio devices:"
"$VENV_PY" "$DAEMON_PY" --list-devices
echo

read -r -p "Input device index  [Enter for system default]: " IN_DEV
read -r -p "Output device index [Enter for system default]: " OUT_DEV
read -r -p "Agent name          [Enter for default 'Zeebot']: " AGENT_NAME_ARG
read -r -p "Wake phrase         [Enter for '<name> wake up']: " WAKE_PHRASE_ARG

EXTRA_ARGS=()
if [[ -n "$IN_DEV" ]];         then EXTRA_ARGS+=("--input-device"  "$IN_DEV");         fi
if [[ -n "$OUT_DEV" ]];        then EXTRA_ARGS+=("--output-device" "$OUT_DEV");        fi
if [[ -n "$AGENT_NAME_ARG" ]]; then EXTRA_ARGS+=("--agent-name"    "$AGENT_NAME_ARG"); fi
if [[ -n "$WAKE_PHRASE_ARG" ]]; then EXTRA_ARGS+=("--wake-phrase"  "$WAKE_PHRASE_ARG"); fi

# ── 6. Render and install LaunchAgent plist ──────────────────────────────────

mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p /tmp/openclaw

# Build ProgramArguments XML fragment with optional flags
EXTRA_XML=""
for arg in "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}"; do
    EXTRA_XML+="        <string>${arg}</string>
"
done

# Read template, substitute placeholders, and inject extra args before close of array
"$VENV_PY" - <<PY
import re
src = open("$PLIST_TEMPLATE").read()
src = src.replace("__VENV_PYTHON__", "$VENV_PY")
src = src.replace("__DAEMON_PATH__", "$DAEMON_PY")
src = src.replace("__SKILL_DIR__",   "$SKILL_DIR")

extra = """$EXTRA_XML"""
if extra.strip():
    # Inject extra args before </array> in ProgramArguments
    src = src.replace("    </array>\n\n    <key>EnvironmentVariables>",
                      extra + "    </array>\n\n    <key>EnvironmentVariables>", 1)
    # The above tag may have been altered; use a robust replacement instead
    src = src.replace("</string>\n    </array>\n\n    <key>EnvironmentVariables</key>",
                      "</string>\n" + extra + "    </array>\n\n    <key>EnvironmentVariables</key>", 1)

open("$PLIST_DEST", "w").write(src)
print("  ✓ installed plist:", "$PLIST_DEST")
PY

# ── 7. Load LaunchAgent ──────────────────────────────────────────────────────

UID_VAL=$(id -u)
# Unload first if already loaded (idempotent)
launchctl bootout "gui/$UID_VAL/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_VAL" "$PLIST_DEST"
launchctl kickstart -k "gui/$UID_VAL/$LABEL" 2>/dev/null || true

green "  ✓ LaunchAgent loaded"
echo

bold "=== Install complete ==="
echo
echo "  Dashboard: http://localhost:19000/dashboard"
echo "  Logs:      tail -f /tmp/openclaw/realtimetalk.log"
echo "  Toggle:    bash $SKILL_DIR/RealTimeTalk-toggle.sh {start|stop|restart|status|log}"
echo
