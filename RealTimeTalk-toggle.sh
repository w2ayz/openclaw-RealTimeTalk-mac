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
PORT=19000   # daemon's default HTTP dashboard port (DEFAULT_HTTP_PORT)

# Any live process that means "RTT is running" — the wrapper app (either
# name) or the Python daemon itself.
RTT_PROC_RE='Applications/RealTimeTalk\.app|Applications/ZeebotTalk\.app|RealTimeTalk-daemon\.py'

_daemon_running() { pgrep -f "$RTT_PROC_RE" >/dev/null 2>&1; }
_port_bound()     { lsof -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; }
_is_disabled()    { launchctl print-disabled "gui/$UID_VAL" 2>/dev/null | grep -q "\"$LABEL\" => disabled"; }

case "${1:-}" in
    start)
        launchctl bootstrap "gui/$UID_VAL" "$PLIST"
        ;;
    stop)
        launchctl bootout "gui/$UID_VAL/$LABEL" 2>/dev/null || true
        ;;
    restart)
        # Full unload/reload rather than `kickstart -k`: bootout+bootstrap
        # also picks up any edits to the plist (device flags, --mic-gate
        # the daemon persists, etc.), which kickstart does not. The retry
        # loop rides out launchd's transient "Bootstrap failed: 5: Input/
        # output error" during teardown.
        launchctl bootout "gui/$UID_VAL/$LABEL" 2>/dev/null || true
        for _ in 1 2 3 4 5; do
            launchctl bootstrap "gui/$UID_VAL" "$PLIST" 2>/dev/null && break
            sleep 2
        done
        ;;
    disable)
        # Stop it now AND keep it from starting again (across reboots),
        # without uninstalling. Use this as a mic kill-switch.
        echo "Disabling ${LABEL} ..."
        launchctl bootout "gui/$UID_VAL/$LABEL" 2>/dev/null || true
        # The 3.18.1+ wrapper forwards SIGTERM and the daemon exits on its
        # own; wait for that.
        for _ in $(seq 1 12); do _daemon_running || break; sleep 1; done
        # Older wrappers orphaned the daemon on bootout — it keeps the port
        # and the mic. Reap it directly if it's still around.
        if _daemon_running; then
            echo "  daemon still up after unload (old wrapper?) — terminating it directly"
            pkill -f 'RealTimeTalk-daemon\.py' 2>/dev/null || true
            sleep 2
            if _daemon_running; then pkill -9 -f 'RealTimeTalk-daemon\.py' 2>/dev/null || true; fi
            sleep 1
        fi
        launchctl disable "gui/$UID_VAL/$LABEL"
        # Verify
        fail=0
        if _daemon_running; then echo "  FAIL: an RTT process is still running"; fail=1; fi
        if _port_bound;     then echo "  FAIL: port $PORT is still bound";        fail=1; fi
        if [[ $fail -eq 0 ]]; then
            echo "  OK: disabled — no daemon, port $PORT free, mic released (menu-bar dot should clear)."
            echo "    Persists across reboots. Re-enable with: $0 enable"
        else
            echo "  WARN: not fully down — check: $0 status  |  lsof -iTCP:$PORT -sTCP:LISTEN"
            exit 1
        fi
        ;;
    enable)
        # Undo 'disable': clear the override and start it. 'enable' must run
        # before 'bootstrap' — launchd silently refuses to load a disabled job.
        launchctl enable "gui/$UID_VAL/$LABEL"
        launchctl bootstrap "gui/$UID_VAL" "$PLIST" 2>/dev/null || true
        launchctl kickstart "gui/$UID_VAL/$LABEL" 2>/dev/null || true
        echo "Enabled - waiting for the daemon to answer on :${PORT} ..."
        for _ in $(seq 1 25); do
            if curl -fsS -m 2 "http://127.0.0.1:$PORT/status" 2>/dev/null; then
                echo
                echo "  OK: RTT is up."
                exit 0
            fi
            sleep 1
        done
        echo "  WARN: no /status response after 25s — check: $0 log"
        exit 1
        ;;
    status)
        if launchctl list | grep -q "$LABEL"; then
            launchctl list | grep "$LABEL"
        else
            echo "Not loaded."
        fi
        if _is_disabled; then echo "LaunchAgent is DISABLED — will not start on login (run: $0 enable)"; fi
        ;;
    log)
        tail -f "$LOG"
        ;;
    devices)
        "$VENV_PY" "$DAEMON_PY" --list-devices
        ;;
    *)
        cat <<USAGE
Usage: $0 {start|stop|restart|disable|enable|status|log|devices}

  start    Load the LaunchAgent (also runs at every login if RunAtLoad=true)
  stop     Unload the LaunchAgent (comes back at next login)
  restart  Bounce the agent, re-reading the plist (preserves config)
  disable  Stop now AND keep it off across reboots; reaps an orphaned
           daemon, then verifies the mic is released. Mic kill-switch.
  enable   Undo 'disable' and start it, waiting until the dashboard responds
  status   Show launchctl status (and whether it's disabled)
  log      Tail $LOG
  devices  List CoreAudio inputs/outputs visible to the daemon
USAGE
        ;;
esac
