#!/usr/bin/env bash
# RealTimeTalk-install-mac.sh — Install the RealTimeTalk daemon on macOS.
#
# Steps:
#   1. Verify Homebrew dependencies (portaudio, ffmpeg, node)
#   2. Resolve the Edge TTS skill (first TTS fallback) and prepare its node deps
#   3. Create Python venv and install dependencies
#   4. Prompt for STT provider keys (OpenAI and/or Gemini — either or both),
#      verify each against its provider API, and write the engine choice to
#      ~/.openclaw/workspace/rtt_stt_config.json
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

# ── 4. STT provider keys — OpenAI and/or Gemini ──────────────────────────────
# Keys live in openclaw.json (talk.providers.<name>.apiKey). The ENGINE choice
# lives in the daemon's own config file (~/.openclaw/workspace/rtt_stt_config.json,
# v3.22.4+) — NOT in openclaw.json: OpenClaw's TalkSchema has no `stt` key, so
# a talk.stt block there gets stripped by gateway config rewrites and fails
# `openclaw config validate`. The daemon still reads a legacy openclaw.json
# talk.stt block as a fallback source, but this installer never writes one.

if [[ ! -f "$OPENCLAW_JSON" ]]; then
    red "  ✗ $OPENCLAW_JSON not found"
    exit 1
fi

STT_CFG="$HOME/.openclaw/workspace/rtt_stt_config.json"

echo "STT providers currently configured in openclaw.json:"
"$VENV_PY" - <<PY
import json
cfg = json.load(open("$OPENCLAW_JSON"))
prov = cfg.get("talk", {}).get("providers", {})
for name in ("openai", "gemini"):
    k = (prov.get(name) or {}).get("apiKey", "")
    print(f"  {name:8s} {'configured' if k else '- not set'}")
PY
echo

echo "  STT engine setup — which provider key(s) do you want to use?"
echo "    [1] OpenAI Realtime        (regular sk-... API key — NOT the ChatGPT OAuth profile)"
echo "    [2] Gemini Transcribe Live (Gemini API key, AIza...)"
echo "    [3] Both                   (pick the default engine; the other becomes the fallback)"
echo "    [4] Keep existing configuration"
echo
while true; do
    read -r -p "  Choice [1/2/3/4, Enter = keep existing]: " STT_CHOICE
    STT_CHOICE="${STT_CHOICE:-4}"
    case "$STT_CHOICE" in 1|2|3|4) break ;; esac
    yellow "  → enter 1, 2, 3 or 4"
done

# ensure_stt_key <provider> — prompts (hidden) if needed and writes the key.
# Returns 0 if the provider has a usable key afterwards, 1 otherwise.
ensure_stt_key() {
    local prov="$1" prefix="^sk-"
    [[ "$prov" == "gemini" ]] && prefix="^AIza"
    local existing
    existing=$("$VENV_PY" - "$prov" <<PY
import json, sys
cfg = json.load(open("$OPENCLAW_JSON"))
k = cfg.get("talk", {}).get("providers", {}).get(sys.argv[1], {}).get("apiKey", "")
print("yes" if k else "no")
PY
)
    local KEY=""
    if [[ "$existing" == "yes" ]]; then
        read -r -p "  $prov key already configured — Enter to keep it, or paste a replacement: " KEY
        [[ -z "$KEY" ]] && { green "  ✓ kept existing $prov key"; return 0; }
    else
        read -rs -p "  Enter $prov API key (hidden input): " KEY
        echo
        if [[ -z "$KEY" ]]; then
            yellow "  → no $prov key entered — $prov STT will not be usable"
            return 1
        fi
    fi
    if ! [[ "$KEY" =~ $prefix ]]; then
        local reply
        read -r -p "  ⚠ That doesn't look like a $prov key (expected ${prefix#^}...). Use it anyway? [y/N]: " reply
        if [[ ! "$reply" =~ ^[Yy] ]]; then return 1; fi
    fi
    # Best-effort live verification — warn-only so an offline install still works.
    local http
    if [[ "$prov" == "openai" ]]; then
        http=$(curl -s -o /dev/null -w "%{http_code}" -m 10 \
            https://api.openai.com/v1/models -H "Authorization: Bearer $KEY" || echo 000)
    else
        http=$(curl -s -o /dev/null -w "%{http_code}" -m 10 \
            https://generativelanguage.googleapis.com/v1beta/models -H "x-goog-api-key: $KEY" || echo 000)
    fi
    case "$http" in
        200)     green "  ✓ $prov key verified against the provider API" ;;
        000)     yellow "  → could not reach the $prov API to verify (offline?) — continuing" ;;
        401|403) red   "  ✗ $prov key was REJECTED by the provider (HTTP $http) — not saving it"
                 return 1 ;;
        *)       yellow "  → provider returned HTTP $http — saving anyway (can re-run this installer)" ;;
    esac
    "$VENV_PY" - "$prov" "$KEY" <<'PY'
import json, os, sys
prov, key = sys.argv[1], sys.argv[2]
path = os.path.expanduser("~/.openclaw/openclaw.json")
cfg = json.load(open(path))
cfg.setdefault("talk", {}).setdefault("providers", {}).setdefault(prov, {})["apiKey"] = key
json.dump(cfg, open(path, "w"), indent=2)
os.chmod(path, 0o600)
PY
    green "  ✓ $prov key written to openclaw.json"
}

# write_stt_engine <provider> <fallback-or-empty>
write_stt_engine() {
    "$VENV_PY" - "$1" "$2" <<'PY'
import json, os, sys
provider, fallback = sys.argv[1], sys.argv[2]
path = os.path.expanduser("~/.openclaw/workspace/rtt_stt_config.json")
os.makedirs(os.path.dirname(path), exist_ok=True)
cfg = {"provider": provider}
if fallback:
    cfg["fallback"] = fallback
json.dump(cfg, open(path, "w"), indent=2)
PY
    green "  ✓ STT engine → $1${2:+ (fallback: $2)} written to $STT_CFG"
}

STT_ENGINE=""      # what rtt_stt_config.json should say at the end
case "$STT_CHOICE" in
    1)
        ensure_stt_key openai || true
        if "$VENV_PY" - <<PY
import json, sys
sys.exit(0 if json.load(open("$OPENCLAW_JSON")).get("talk", {}).get("providers", {}).get("openai", {}).get("apiKey") else 1)
PY
        then
            # A gemini key (if present) becomes a free fallback.
            if "$VENV_PY" - <<PY
import json, sys
sys.exit(0 if json.load(open("$OPENCLAW_JSON")).get("talk", {}).get("providers", {}).get("gemini", {}).get("apiKey", "") else 1)
PY
            then write_stt_engine openai gemini; else write_stt_engine openai ""; fi
        fi
        ;;
    2)
        ensure_stt_key gemini || true
        if "$VENV_PY" - <<PY
import json, sys
sys.exit(0 if json.load(open("$OPENCLAW_JSON")).get("talk", {}).get("providers", {}).get("gemini", {}).get("apiKey", "") else 1)
PY
        then
            if "$VENV_PY" - <<PY
import json, sys
sys.exit(0 if json.load(open("$OPENCLAW_JSON")).get("talk", {}).get("providers", {}).get("openai", {}).get("apiKey", "") else 1)
PY
            then write_stt_engine gemini openai; else write_stt_engine gemini ""; fi
        fi
        ;;
    3)
        ensure_stt_key openai || true
        ensure_stt_key gemini || true
        if "$VENV_PY" - <<PY
import json, sys
prov = json.load(open("$OPENCLAW_JSON")).get("talk", {}).get("providers", {})
sys.exit(0 if (prov.get("openai", {}).get("apiKey", "") and prov.get("gemini", {}).get("apiKey", "")) else 1)
PY
        then
            while true; do
                read -r -p "  Default STT engine [gemini/openai, Enter = openai]: " DEFAULT_ENGINE
                DEFAULT_ENGINE="$(echo "${DEFAULT_ENGINE:-openai}" | tr '[:upper:]' '[:lower:]')"
                case "$DEFAULT_ENGINE" in gemini|openai) break ;; esac
                yellow "  → enter 'gemini' or 'openai'"
            done
            if [[ "$DEFAULT_ENGINE" == "gemini" ]]; then write_stt_engine gemini openai
            else write_stt_engine openai gemini; fi
        else
            yellow "  → 'Both' needs both keys — set the engine manually in $STT_CFG"
        fi
        ;;
    4) green "  ✓ keeping existing configuration" ;;
esac

# Final precondition: at least one usable STT key.
if ! "$VENV_PY" - <<PY
import json, sys
prov = json.load(open("$OPENCLAW_JSON")).get("talk", {}).get("providers", {})
sys.exit(0 if (prov.get("openai", {}).get("apiKey", "") or prov.get("gemini", {}).get("apiKey", "")) else 1)
PY
then
    red "  ✗ No STT provider key configured (openai or gemini) in $OPENCLAW_JSON"
    echo
    echo "  Add one to openclaw.json — either provider works on its own:"
    echo
    cat <<'EXAMPLE'
  "talk": {
      "providers": {
          "openai": { "apiKey": "sk-..." },
          "gemini": { "apiKey": "AIza..." }
      }
  }
EXAMPLE
    echo
    echo "  Then re-run this installer."
    exit 1
fi
green "  ✓ STT provider key(s) ready"
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
