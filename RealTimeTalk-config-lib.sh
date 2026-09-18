#!/usr/bin/env bash
# RealTimeTalk-config-lib.sh — shared interview functions for the STT/TTS/
# vocabulary setup steps, sourced by both RealTimeTalk-install-mac.sh (as
# part of a fresh install) and RTT-Config.sh (re-runnable
# anytime, no venv/brew/plist steps). Keeping this in one file means the two
# entry points can't drift out of sync with each other.
#
# Callers must set these before sourcing/calling into this file:
#   VENV_PY        — path to the venv's python3
#   OPENCLAW_JSON  — path to ~/.openclaw/openclaw.json
#   STT_CFG        — path to ~/.openclaw/workspace/rtt_stt_config.json
#   TTS_CFG        — path to ~/.openclaw/workspace/rtt_tts_config.json
#
# Bash 3.2 (macOS system bash) compatible: no `declare -A`, no `${var,,}`,
# and any array expansion is guarded against an empty array under `set -u`.

red()    { printf "\033[31m%s\033[0m\n" "$*"; }
green()  { printf "\033[32m%s\033[0m\n" "$*"; }
yellow() { printf "\033[33m%s\033[0m\n" "$*"; }
bold()   { printf "\033[1m%s\033[0m\n" "$*"; }

# _has_provider_key <provider> — exit 0 if openclaw.json has a non-empty
# apiKey for that provider, 1 otherwise.
_has_provider_key() {
    "$VENV_PY" - "$1" <<PY
import json, sys
sys.exit(0 if json.load(open("$OPENCLAW_JSON")).get("talk", {}).get("providers", {}).get(sys.argv[1], {}).get("apiKey") else 1)
PY
}

has_any_stt_key() {
    _has_provider_key openai || _has_provider_key gemini
}

# ensure_provider_key <provider> <prefix-regex-or-empty> <env-var> [<env-var-2>]
#
# Checks, in order: the environment (env-var, then env-var-2 if given — e.g.
# GEMINI_API_KEY falling back to GOOGLE_API_KEY, Google's more common name
# for the same credential), then openclaw.json's existing value, then a
# fresh interactive prompt. A key found any way still goes through the same
# live-verification call before being written. Returns 0 if a usable key
# ends up configured, 1 otherwise (skip/rejected).
ensure_provider_key() {
    local prov="$1" prefix="$2" envvar="${3:-}" envvar2="${4:-}"
    local existing="no"
    _has_provider_key "$prov" && existing="yes"

    local KEY="" env_val="" src_var=""
    if [[ -n "$envvar" ]]; then
        env_val="${!envvar:-}"
        src_var="$envvar"
    fi
    if [[ -z "$env_val" && -n "$envvar2" ]]; then
        env_val="${!envvar2:-}"
        src_var="$envvar2"
    fi
    if [[ -n "$env_val" ]]; then
        if [[ "$existing" == "yes" ]]; then
            read -r -p "  Found \$$src_var in your environment, and $prov already has a configured key — use the environment value instead? [y/N]: " USE_ENV
            [[ "$USE_ENV" =~ ^[Yy] ]] && KEY="$env_val"
        else
            read -r -p "  Found \$$src_var in your environment — use it for $prov? [Y/n]: " USE_ENV
            USE_ENV="${USE_ENV:-Y}"
            [[ "$USE_ENV" =~ ^[Yy] ]] && KEY="$env_val"
        fi
    fi

    if [[ -z "$KEY" ]]; then
        if [[ "$existing" == "yes" ]]; then
            read -r -p "  $prov key already configured — Enter to keep it, or paste a replacement: " KEY
            [[ -z "$KEY" ]] && { green "  ✓ kept existing $prov key"; return 0; }
        else
            read -rs -p "  Enter $prov API key (hidden input, Enter to skip): " KEY
            echo
            if [[ -z "$KEY" ]]; then
                yellow "  → no $prov key entered — $prov will not be usable"
                return 1
            fi
        fi
    fi

    if [[ -n "$prefix" ]] && ! [[ "$KEY" =~ $prefix ]]; then
        local reply
        read -r -p "  ⚠ That doesn't look like a $prov key (expected ${prefix#^}...). Use it anyway? [y/N]: " reply
        if [[ ! "$reply" =~ ^[Yy] ]]; then return 1; fi
    fi

    # Best-effort live verification — warn-only so an offline install still works.
    local http
    case "$prov" in
        openai)
            http=$(curl -s -o /dev/null -w "%{http_code}" -m 10 \
                https://api.openai.com/v1/models -H "Authorization: Bearer $KEY" || echo 000) ;;
        gemini)
            http=$(curl -s -o /dev/null -w "%{http_code}" -m 10 \
                https://generativelanguage.googleapis.com/v1beta/models -H "x-goog-api-key: $KEY" || echo 000) ;;
        elevenlabs)
            http=$(curl -s -o /dev/null -w "%{http_code}" -m 10 \
                https://api.elevenlabs.io/v1/user -H "xi-api-key: $KEY" || echo 000) ;;
        *) http=000 ;;
    esac
    case "$http" in
        200)     green "  ✓ $prov key verified against the provider API" ;;
        000)     yellow "  → could not reach the $prov API to verify (offline?) — continuing" ;;
        401|403) red   "  ✗ $prov key was REJECTED by the provider (HTTP $http) — not saving it"
                 return 1 ;;
        *)       yellow "  → provider returned HTTP $http — saving anyway (can re-run this later)" ;;
    esac

    "$VENV_PY" - "$prov" "$KEY" <<PY
import json, os, sys
prov, key = sys.argv[1], sys.argv[2]
path = "$OPENCLAW_JSON"
cfg = json.load(open(path))
cfg.setdefault("talk", {}).setdefault("providers", {}).setdefault(prov, {})["apiKey"] = key
json.dump(cfg, open(path, "w"), indent=2)
os.chmod(path, 0o600)
PY
    green "  ✓ $prov key written to openclaw.json"
}

# write_stt_engine <provider> <fallback-or-empty> — merges into rtt_stt_config.json
# instead of overwriting it outright, so a reinstall/reconfigure never wipes a
# "vocabulary" list the daemon seeded (_ensure_stt_config_seeded) or the user
# has since customized. provider "none" (no fallback) marks the STT-bypass,
# TTS-only choice — see run_stt_setup's Skip option.
write_stt_engine() {
    "$VENV_PY" - "$1" "$2" <<PY
import json, os, sys
provider, fallback = sys.argv[1], sys.argv[2]
path = "$STT_CFG"
os.makedirs(os.path.dirname(path), exist_ok=True)
cfg = {}
if os.path.isfile(path):
    try:
        with open(path) as f:
            existing = json.load(f)
        if isinstance(existing, dict):
            cfg = existing
    except Exception:
        pass
cfg["provider"] = provider
if fallback:
    cfg["fallback"] = fallback
else:
    cfg.pop("fallback", None)
json.dump(cfg, open(path, "w"), indent=2)
PY
    green "  ✓ STT engine → $1${2:+ (fallback: $2)} written to $STT_CFG"
}

# run_stt_setup — interview for which STT provider key(s) to use, or skip
# STT entirely for TTS-only (text-only) mode. Re-runnable: every choice
# defaults to "keep what's already there."
run_stt_setup() {
    echo
    bold "STT setup — live speech-to-text (mic wake word / voice commands)"
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
    echo "    [5] Skip — no STT, TTS-only (text-only) mode. OpenClaw can still push text to"
    echo "        RealTimeTalk to read aloud (POST /speak) — there's just no mic/wake-word listening."
    echo "  (Enter to keep existing configuration; nothing here is final — re-run this anytime.)"
    echo
    local STT_CHOICE
    while true; do
        read -r -p "  Choice [1/2/3/4/5, Enter = keep existing]: " STT_CHOICE
        STT_CHOICE="${STT_CHOICE:-4}"
        case "$STT_CHOICE" in 1|2|3|4|5) break ;; esac
        yellow "  → enter 1, 2, 3, 4 or 5"
    done

    case "$STT_CHOICE" in
        1)
            ensure_provider_key openai "^sk-" OPENAI_API_KEY || true
            if _has_provider_key openai; then
                if _has_provider_key gemini; then write_stt_engine openai gemini; else write_stt_engine openai ""; fi
            fi
            ;;
        2)
            ensure_provider_key gemini "^AIza" GEMINI_API_KEY GOOGLE_API_KEY || true
            if _has_provider_key gemini; then
                if _has_provider_key openai; then write_stt_engine gemini openai; else write_stt_engine gemini ""; fi
            fi
            ;;
        3)
            ensure_provider_key openai "^sk-" OPENAI_API_KEY || true
            ensure_provider_key gemini "^AIza" GEMINI_API_KEY GOOGLE_API_KEY || true
            if _has_provider_key openai && _has_provider_key gemini; then
                local DEFAULT_ENGINE
                while true; do
                    read -r -p "  Default STT engine [gemini/openai, Enter = openai]: " DEFAULT_ENGINE
                    DEFAULT_ENGINE="$(printf '%s' "${DEFAULT_ENGINE:-openai}" | tr '[:upper:]' '[:lower:]')"
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
        5)
            write_stt_engine none ""
            green "  ✓ STT bypassed — RealTimeTalk will run TTS-only. OpenClaw can still speak via /speak."
            return 0
            ;;
    esac

    if [[ "$STT_CHOICE" != "4" ]] && ! has_any_stt_key; then
        yellow "  → No STT provider key ended up configured (openai or gemini)."
        local CONT_TEXT_ONLY
        read -r -p "  Continue anyway in TTS-only (no STT) mode? [Y/n]: " CONT_TEXT_ONLY
        if [[ "$CONT_TEXT_ONLY" =~ ^[Nn] ]]; then
            red "  ✗ No STT key configured. Re-run this script when you have one:"
            yellow "    bash \"$SKILL_DIR/RTT-Config.sh\""
            return 1
        fi
        write_stt_engine none ""
        green "  ✓ STT bypassed — RealTimeTalk will run TTS-only."
    elif [[ "$STT_CHOICE" != "4" ]]; then
        green "  ✓ STT provider key(s) ready"
    fi
    echo
}

# run_tts_setup — ElevenLabs key + reorderable/droppable TTS engine chain.
run_tts_setup() {
    echo
    bold "TTS setup — voice output engines"
    echo "  ElevenLabs gives the best multilingual quality (Chinese/mixed replies)."
    echo "  Skip it (Enter at the prompt) and the chain below just falls back automatically."
    echo
    ensure_provider_key elevenlabs "^sk_" ELEVENLABS_API_KEY || true
    echo

    local existing_order
    existing_order=$("$VENV_PY" - <<PY
import json
try:
    with open("$TTS_CFG") as f:
        order = json.load(f).get("order") or []
except Exception:
    order = []
print(",".join(order) if order else "elevenlabs,edge,openai,say")
PY
)
    echo "  TTS engine chain (tried in this order until one produces audio):"
    echo "    Current: $existing_order"
    echo "    Known engines: elevenlabs, edge, openai, say"
    echo "    Drop any you don't want, e.g. 'edge,say' skips ElevenLabs and OpenAI entirely."
    echo "    'say' is always kept as a last-resort fallback even if you leave it out — it's"
    echo "    the only engine that needs no key or network."
    echo
    local NEW_ORDER
    while true; do
        read -r -p "  TTS order [Enter to keep current: $existing_order]: " NEW_ORDER
        NEW_ORDER="${NEW_ORDER:-$existing_order}"
        if "$VENV_PY" - "$NEW_ORDER" <<PY
import sys
known = {"elevenlabs", "edge", "openai", "say"}
terms = [t.strip().lower() for t in sys.argv[1].split(",") if t.strip()]
bad = [t for t in terms if t not in known]
sys.exit(1 if (bad or not terms) else 0)
PY
        then
            break
        fi
        yellow "  → use only: elevenlabs, edge, openai, say (comma-separated)"
    done

    local SAVED_ORDER
    SAVED_ORDER=$("$VENV_PY" - "$NEW_ORDER" <<PY
import json
import sys
terms = []
for t in sys.argv[1].split(","):
    t = t.strip().lower()
    if t and t not in terms:
        terms.append(t)
if "say" not in terms:
    terms.append("say")
json.dump({"order": terms}, open("$TTS_CFG", "w"), indent=2)
print(",".join(terms))
PY
)
    green "  ✓ TTS order saved to $TTS_CFG: $SAVED_ORDER"
    echo
}

# run_vocabulary_setup — review/extend the STT custom-vocabulary hint list.
run_vocabulary_setup() {
    echo
    bold "STT vocabulary — words the speech engine should recognize better"
    echo "  Seeded by default with the agent name plus OpenClaw/RealTimeTalk/RTT/STT/TTS."
    echo "  Add more any time: names, place names, call signs, jargon — it's a hint, not a"
    echo "  guarantee (an unusual term can still come through imperfectly)."
    echo
    local current
    current=$("$VENV_PY" - <<PY
import json
try:
    with open("$STT_CFG") as f:
        vocab = json.load(f).get("vocabulary") or []
except Exception:
    vocab = []
print(", ".join(vocab) if vocab else "(none yet — seeded automatically on first daemon start)")
PY
)
    echo "  Current vocabulary: $current"
    echo
    local NEW_TERMS
    read -r -p "  Add extra words (comma-separated, Enter to skip): " NEW_TERMS
    if [[ -z "$NEW_TERMS" ]]; then
        green "  ✓ vocabulary unchanged"
        echo
        return 0
    fi
    "$VENV_PY" - "$NEW_TERMS" <<PY
import json, os, sys
raw = sys.argv[1]
path = "$STT_CFG"
os.makedirs(os.path.dirname(path), exist_ok=True)
cfg = {}
if os.path.isfile(path):
    try:
        existing = json.load(open(path))
        if isinstance(existing, dict):
            cfg = existing
    except Exception:
        pass
vocab = cfg.get("vocabulary") or []
added = [t.strip() for t in raw.split(",") if t.strip()]
cfg["vocabulary"] = list(dict.fromkeys(vocab + added))
json.dump(cfg, open(path, "w"), indent=2)
print(", ".join(cfg["vocabulary"]))
PY
    green "  ✓ vocabulary updated in $STT_CFG"
    echo
}
