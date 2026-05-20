#!/usr/bin/env bash
# RealTimeTalk-toggle.sh — Control the macOS LaunchAgent for the daemon.
set -euo pipefail

LABEL="ai.openclaw.realtimetalk"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_VAL=$(id -u)
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAEMON_PY="$SKILL_DIR/RealTimeTalk-daemon.py"
VENV_PY="$SKILL_DIR/venv/bin/python3"
LOG="/tmp/openclaw/realtimetalk.log"

case "${1:-}" in
    start)
        launchctl bootstrap "gui/$UID_VAL" "$PLIST"
        ;;
    stop)
        launchctl bootout "gui/$UID_VAL/$LABEL" 2>/dev/null || true
        ;;
    restart)
        launchctl kickstart -k "gui/$UID_VAL/$LABEL"
        ;;
    status)
        if launchctl list | grep -q "$LABEL"; then
            launchctl list | grep "$LABEL"
        else
            echo "Not loaded."
        fi
        ;;
    log)
        tail -f "$LOG"
        ;;
    devices)
        "$VENV_PY" "$DAEMON_PY" --list-devices
        ;;
    *)
        cat <<USAGE
Usage: $0 {start|stop|restart|status|log|devices}

  start    Load the LaunchAgent (also runs at every login if RunAtLoad=true)
  stop     Unload the LaunchAgent
  restart  Bounce the agent (preserves config)
  status   Show launchctl status
  log      Tail $LOG
  devices  List CoreAudio inputs/outputs visible to the daemon
USAGE
        ;;
esac
