#!/usr/bin/env bash
# RealTimeTalk-install-mac.sh — Install the RealTimeTalk daemon on macOS.
#
# Steps:
#   1. Verify Homebrew dependencies (portaudio, ffmpeg, node)
#   2. Resolve the Edge TTS skill (first TTS fallback) and prepare its node deps
#   3. Create Python venv and install dependencies
#   4. Verify openai.apiKey is configured in openclaw.json
#   5. List audio devices and prompt user for input + output device indices,
#      agent name, and wake phrase; optionally download the Voice ID
#      speaker-embedding model
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
# Edge TTS skill — official location is ~/.openclaw/workspace/skills/edge-tts/
# (published skill; `npm install` runs in scripts/ per its skill-info.json).
EDGE_TTS_OFFICIAL="$HOME/.openclaw/workspace/skills/edge-tts/scripts/tts-converter.js"

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

# ── 2. Edge TTS skill (first TTS fallback after ElevenLabs) ──────────────────
# Resolve sibling-first so a relocated OpenClaw workspace still works, then fall
# back to the official location. A missing skill is a warning, not fatal —
# ElevenLabs stays primary and OpenAI TTS + macOS `say` cover the fallback.

EDGE_TTS_SCRIPT=""
for cand in \
    "$SKILL_DIR/../edge-tts/scripts/tts-converter.js" \
    "${OPENCLAW_WORKSPACE:-}/skills/edge-tts/scripts/tts-converter.js" \
    "$EDGE_TTS_OFFICIAL"; do
    if [[ -n "$cand" && -f "$cand" ]]; then
        EDGE_TTS_SCRIPT="$(cd "$(dirname "$cand")" && pwd)/$(basename "$cand")"
        break
    fi
done

if [[ -z "$EDGE_TTS_SCRIPT" ]]; then
    yellow "  → Edge TTS skill not found (looked in skills/edge-tts/)."
    yellow "    Install it at the official path, then re-run this installer:"
    yellow "      clawhub install edge-tts"
    yellow "      # or: git clone <edge-tts repo> ~/.openclaw/workspace/skills/edge-tts"
    yellow "    Continuing without it — ElevenLabs stays primary; OpenAI TTS + 'say' cover fallback."
    EDGE_TTS_SCRIPT="$EDGE_TTS_OFFICIAL"   # daemon re-checks this path at runtime
else
    EDGE_TTS_DIR="$(cd "$(dirname "$EDGE_TTS_SCRIPT")" && pwd)"
    if [[ ! -d "$EDGE_TTS_DIR/node_modules" ]]; then
        yellow "  → Installing Edge TTS node deps (npm install in $EDGE_TTS_DIR)"
        ( cd "$EDGE_TTS_DIR" && npm install --omit=dev --silent ) \
            || yellow "    npm install failed — Edge TTS falls back to OpenAI/say at runtime."
    fi
    if node "$EDGE_TTS_SCRIPT" --help >/dev/null 2>&1; then
        green "  ✓ Edge TTS skill ready ($EDGE_TTS_SCRIPT)"
    else
        yellow "  → Edge TTS script present but not runnable — check 'node' and node_modules."
    fi
fi
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
while true; do
    read -r -p "Agent name          [Enter for default 'Zeebot']: " AGENT_NAME_ARG
    read -r -p "Wake phrase         [Enter for '<name> wake up']: " WAKE_PHRASE_ARG

    echo "  Agent name:  ${AGENT_NAME_ARG:-Zeebot}"
    echo "  Wake phrase: ${WAKE_PHRASE_ARG:-<name> wake up}"
    read -r -p "Are you sure about Agent Name and Wake Phrase? [Y/n]: " CONFIRM_NAME_WAKE
    if [[ ! "$CONFIRM_NAME_WAKE" =~ ^[Nn] ]]; then
        break
    fi
    echo
done
echo

# ── 5.5. Voice ID speaker-embedding model ────────────────────────────────────

SPK_MODEL_DIR="$HOME/.local/share/rtt/speaker"
SPK_MODEL_FILE="$SPK_MODEL_DIR/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"
SPK_MODEL_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"

if [[ -f "$SPK_MODEL_FILE" ]]; then
    green "  ✓ Voice ID speaker model already present at $SPK_MODEL_FILE"
else
    echo
    read -r -p "Download Voice ID speaker model now (~28 MB, enables owner voice recognition)? [Y/n]: " DL_SPK_MODEL
    if [[ ! "$DL_SPK_MODEL" =~ ^[Nn] ]]; then
        mkdir -p "$SPK_MODEL_DIR"
        echo "Downloading speaker-embedding model..."
        if curl -fL -o "$SPK_MODEL_FILE" "$SPK_MODEL_URL"; then
            green "  ✓ Voice ID speaker model installed at $SPK_MODEL_FILE"
        else
            rm -f "$SPK_MODEL_FILE"
            yellow "  ✗ Download failed — Voice ID will fail open (accept all speakers) until you retry."
            yellow "    Manual retry: curl -fL -o \"$SPK_MODEL_FILE\" \"$SPK_MODEL_URL\""
        fi
    else
        yellow "  → Skipped. Voice ID will fail open (accept all speakers) until you download the model — see README §Voice ID."
    fi
fi
echo

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
src = src.replace("__VENV_PYTHON__",     "$VENV_PY")
src = src.replace("__DAEMON_PATH__",      "$DAEMON_PY")
src = src.replace("__SKILL_DIR__",        "$SKILL_DIR")
src = src.replace("__EDGE_TTS_SCRIPT__",  "$EDGE_TTS_SCRIPT")

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
