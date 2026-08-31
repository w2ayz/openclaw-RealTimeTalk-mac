#!/usr/bin/env bash
# RealTimeTalk-build-wrapper-mac.sh — Build RealTimeTalk.app, a tiny signed
# wrapper around the RealTimeTalk daemon.
#
# Why this exists: a bare `python3` process launched by a launchd
# LaunchAgent has no stable app identity for macOS's TCC (privacy/security)
# subsystem to track, so microphone access can fail to prompt reliably or
# fail to persist across restarts. This wrapper is a minimal Swift binary,
# packaged as a proper .app bundle (Info.plist + NSMicrophoneUsageDescription
# + ad-hoc code signature + audio-input entitlement), that requests mic
# access itself via AVFoundation before launching the daemon as its child
# process — TCC then associates the grant with this app's stable identity
# instead of an anonymous interpreter process.
#
# Usage: bash RealTimeTalk-build-wrapper-mac.sh
# Run from anywhere; paths below are resolved from this script's own location.
# Safe to re-run — rebuilds and re-signs in place.

set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_NAME="RealTimeTalk"
APP_DIR="$HOME/Applications/$APP_NAME.app"
VENV_SITE_PACKAGES="$SKILL_DIR/venv/lib/python3.9/site-packages"
DAEMON_PY="$SKILL_DIR/RealTimeTalk-daemon.py"
ICON_SVG="$SKILL_DIR/assets/RealTimeTalk-icon.svg"

# The system Python that ships with Xcode Command Line Tools — same
# interpreter the venv's own python3 is a symlink to, but invoking it
# directly (with PYTHONPATH pointed at the venv's site-packages) avoids
# depending on that symlink resolving the same way under every launch
# context.
SYSTEM_PYTHON="/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/Resources/Python.app/Contents/MacOS/Python"

red()    { printf "\033[31m%s\033[0m\n" "$*"; }
green()  { printf "\033[32m%s\033[0m\n" "$*"; }
bold()   { printf "\033[1m%s\033[0m\n" "$*"; }

bold "=== Building $APP_NAME.app ==="
echo

if [[ ! -x "$SYSTEM_PYTHON" ]]; then
    red "  ✗ System Python not found at $SYSTEM_PYTHON"
    red "    Install Xcode Command Line Tools: xcode-select --install"
    exit 1
fi
if ! command -v swiftc >/dev/null 2>&1; then
    red "  ✗ swiftc not found — install Xcode Command Line Tools: xcode-select --install"
    exit 1
fi
if [[ ! -d "$VENV_SITE_PACKAGES" ]]; then
    red "  ✗ venv not found at $VENV_SITE_PACKAGES"
    red "    Run RealTimeTalk-install-mac.sh first."
    exit 1
fi
# SVG rasteriser: prefer librsvg's rsvg-convert; otherwise fall back to
# macOS `sips`, which gained native SVG decoding in macOS 13+. Only try to
# install librsvg if neither is available (and don't fail the build if the
# install can't complete — the icon is cosmetic).
_have_svg_raster() { command -v rsvg-convert >/dev/null 2>&1 || command -v sips >/dev/null 2>&1; }
if [[ -f "$ICON_SVG" ]] && ! _have_svg_raster; then
    echo "Installing librsvg (for icon rendering)..."
    brew install librsvg || echo "  (librsvg install failed — will skip custom icon)"
fi

# Rasterise $1 (SVG) to a $2 x $2 PNG at $3, using whichever tool is present.
_raster_png() {
    local svg="$1" px="$2" out="$3"
    if command -v rsvg-convert >/dev/null 2>&1; then
        rsvg-convert -w "$px" -h "$px" "$svg" -o "$out"
    else
        # sips can't resize straight from SVG reliably; render once at
        # native size to a temp PNG (cached across calls) then downscale.
        local master="${TMPDIR:-/tmp}/.rtt-icon-master.png"
        [[ -f "$master" ]] || sips -s format png "$svg" --out "$master" >/dev/null 2>&1
        sips -z "$px" "$px" "$master" --out "$out" >/dev/null 2>&1
    fi
}

mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources"

# Clean up stray source files an older hand-built version of this app may
# have left directly in Contents/MacOS/ — this script builds those in a
# scratch dir instead (see below), but a loose leftover .swift/.plist file
# sitting next to the binary makes some codesign versions choke trying to
# seal it as a bundle resource, even on a rebuild.
rm -f "$APP_DIR/Contents/MacOS/launcher.swift" "$APP_DIR/Contents/MacOS/entitlements.plist"

# Scratch dir for build-time-only inputs (Swift source, entitlements,
# iconset) that should never end up sitting inside the shipped bundle —
# see the launcher.swift section below for why.
SWIFT_SRC_DIR="$(mktemp -d)"
trap 'rm -rf "$SWIFT_SRC_DIR"' EXIT

# ── App icon ──────────────────────────────────────────────────────────────
# Rendered from assets/RealTimeTalk-icon.svg at build time rather than
# committing a binary .icns — keeps the source diffable and the bundle
# always in sync with the current design. Optional: falls back to no
# custom icon (generic .app icon) if the SVG or rsvg-convert is missing,
# so this script still works for anyone who deletes/doesn't have it.

ICON_PLIST_ENTRY=""
if [[ -f "$ICON_SVG" ]] && _have_svg_raster; then
    echo "Rendering app icon..."
    rm -f "${TMPDIR:-/tmp}/.rtt-icon-master.png"
    ICONSET="$SWIFT_SRC_DIR/$APP_NAME.iconset"
    mkdir -p "$ICONSET"
    for size in 16 32 128 256 512; do
        double=$((size * 2))
        _raster_png "$ICON_SVG" "$size"   "$ICONSET/icon_${size}x${size}.png"
        _raster_png "$ICON_SVG" "$double" "$ICONSET/icon_${size}x${size}@2x.png"
    done
    iconutil -c icns "$ICONSET" -o "$APP_DIR/Contents/Resources/$APP_NAME.icns"
    ICON_PLIST_ENTRY="    <key>CFBundleIconFile</key>
    <string>$APP_NAME</string>
"
    green "  ✓ icon rendered: $APP_DIR/Contents/Resources/$APP_NAME.icns"
else
    echo "  skipping custom icon (SVG or SVG rasteriser not found)"
fi
echo

# ── Info.plist ────────────────────────────────────────────────────────────

cat > "$APP_DIR/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key>
    <string>ai.openclaw.realtimetalk</string>
    <key>CFBundleName</key>
    <string>$APP_NAME</string>
    <key>CFBundleExecutable</key>
    <string>$APP_NAME</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>NSMicrophoneUsageDescription</key>
    <string>RealTimeTalk needs microphone access to listen for voice commands.</string>
    <key>LSUIElement</key>
    <true/>
$ICON_PLIST_ENTRY</dict>
</plist>
PLIST

# ── entitlements.plist ───────────────────────────────────────────────────
# Build-time input to codesign only — entitlements get embedded into the
# binary's signature, this file itself doesn't need to ship in the bundle.

cat > "$SWIFT_SRC_DIR/entitlements.plist" <<ENT
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>com.apple.security.device.audio-input</key>
    <true/>
</dict>
</plist>
ENT

# ── launcher.swift ────────────────────────────────────────────────────────
# Paths are baked in at build time (Swift string literals, no runtime env
# lookup) — re-run this script if SKILL_DIR ever moves.
#
# Compiled in the scratch temp dir set up above, not directly inside
# Contents/MacOS/ — a loose non-Mach-O file sitting next to the binary
# makes some codesign versions seal/verify the whole bundle as a resource
# manifest and choke on it ("code object is not signed at all — In
# subcomponent: launcher.swift"), confirmed live on this machine's
# toolchain. Only the compiled binary gets copied into the bundle; the
# source never lands there.

cat > "$SWIFT_SRC_DIR/launcher.swift" <<SWIFT
import AVFoundation
import Foundation

func log(_ msg: String) {
    fputs("[RealTimeTalk] \(msg)\n", stderr)
    fflush(stderr)
}

log("starting pid=\(ProcessInfo.processInfo.processIdentifier)")

// Request mic permission synchronously on a background queue so the main
// thread (and RunLoop) stay responsive.
let sema = DispatchSemaphore(value: 0)
AVCaptureDevice.requestAccess(for: .audio) { granted in
    log("mic access: \(granted)")
    sema.signal()
}
_ = sema.wait(timeout: .now() + 30)

let python = "$SYSTEM_PYTHON"
let script = "$DAEMON_PY"
let venv   = "$VENV_SITE_PACKAGES"

var env = ProcessInfo.processInfo.environment
env["PYTHONPATH"] = venv

let proc = Process()
proc.executableURL = URL(fileURLWithPath: python)
proc.arguments = [script] + CommandLine.arguments.dropFirst()
proc.environment = env
proc.currentDirectoryURL = URL(fileURLWithPath: "$SKILL_DIR")

// Tracks a signal-initiated shutdown so terminationHandler doesn't race
// the signal path to exit with the wrong status.
var _shutdownBySignal: Int32 = 0

func _dieBySignal(_ sig: Int32) {
    signal(sig, SIG_DFL)
    raise(sig)
}

proc.terminationHandler = { p in
    let s = _shutdownBySignal
    if s != 0 {
        // Child exited in response to our forwarded signal — re-raise now
        // rather than waiting out the grace timer, so kickstart's respawn
        // isn't blocked by us holding the port.
        DispatchQueue.main.async { _dieBySignal(s) }
        return
    }
    log("Python exited \(p.terminationStatus) — exiting wrapper")
    exit(p.terminationStatus)
}

// Forward a graceful shutdown to the Python child, then terminate *by the
// same signal* so the parent's exit status reflects it. Without forwarding,
// the child is orphaned and keeps its HTTP port, and the next launch
// crash-loops on "Address already in use". Re-raising (rather than exit(0))
// matters for 'launchctl kickstart -k' — which SIGTERMs then expects the
// job to die by signal before it respawns; a clean exit(0) suppresses the
// respawn. 'launchctl bootout' removes the job either way.
var _signalSources: [DispatchSourceSignal] = []
for sig in [SIGTERM, SIGINT] {
    signal(sig, SIG_IGN)
    let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    src.setEventHandler {
        if _shutdownBySignal != 0 { return }
        _shutdownBySignal = sig
        log("got signal \(sig) — terminating Python pid=\(proc.processIdentifier)")
        if proc.isRunning { proc.terminate() }
        // Fallback if the child ignores SIGTERM: give it a moment, hard-
        // kill, then die by the signal anyway. The common path is
        // terminationHandler above, which re-raises the instant the child
        // actually exits.
        DispatchQueue.main.asyncAfter(deadline: .now() + 3) {
            if proc.isRunning { kill(proc.processIdentifier, SIGKILL) }
            _dieBySignal(sig)
        }
    }
    src.resume()
    _signalSources.append(src)
}

do {
    try proc.run()
    log("Python launched pid=\(proc.processIdentifier)")
} catch {
    log("FAILED to launch Python: \(error)")
    exit(1)
}

// dispatchMain() blocks the calling thread indefinitely, keeping this
// process alive as the responsible parent for Python's CoreAudio access.
log("entering dispatchMain")
dispatchMain()
SWIFT

echo "Compiling..."
swiftc -O "$SWIFT_SRC_DIR/launcher.swift" -o "$APP_DIR/Contents/MacOS/$APP_NAME"
green "  ✓ compiled $APP_DIR/Contents/MacOS/$APP_NAME"

echo "Signing (ad-hoc)..."
# Sign the executable itself, not the whole bundle directory — matches how
# the reference build was actually done (confirmed live: "Sealed
# Resources=none", no TeamIdentifier — a directory-target codesign creates
# a _CodeSignature/CodeResources manifest covering every file in the
# bundle, which chokes on the loose launcher.swift source sitting next to
# the binary; signing just the Mach-O skips that step entirely).
codesign --force --sign - --entitlements "$SWIFT_SRC_DIR/entitlements.plist" "$APP_DIR/Contents/MacOS/$APP_NAME"
green "  ✓ signed"
echo

bold "=== Build complete ==="
echo
echo "  App: $APP_DIR"
echo
echo "  If a LaunchAgent plist is already pointing at the venv's python3"
echo "  directly, update its ProgramArguments to launch this app instead:"
echo "    $APP_DIR/Contents/MacOS/$APP_NAME"
echo "  (any extra args, e.g. --mic-gate 64, pass straight through)"
echo
echo "  First launch will prompt for microphone access — grant it in"
echo "  System Settings → Privacy & Security → Microphone if the dialog"
echo "  doesn't appear (headless/agent launches sometimes suppress it;"
echo "  running the app once via Finder/double-click reliably triggers it)."
