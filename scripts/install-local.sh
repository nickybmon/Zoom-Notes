#!/bin/bash
# install-local.sh — rebuild Zoom Notes from the working tree, replace the
# copy in /Applications, and relaunch.
#
# This is the lightweight personal-install path. It's NOT for distribution
# (use scripts/release.sh for that — it notarizes and packages a DMG).
#
# Default: Release build signed with the user's Developer ID identity.
#   --debug         skip signing for a faster build (~5s vs ~25s)
#   --no-launch     install but don't launch
#   --keep-running  don't try to quit a running instance first
#
# Exit codes:
#   0  installed successfully
#   1  generic failure
#   2  working tree dirty (informational; pass --force to override)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT="$REPO_ROOT/ZoomNotesApp/ZoomNotesApp.xcodeproj"
SCHEME="ZoomNotesApp"
APP_NAME="Zoom Notes"
INSTALLED_PATH="/Applications/$APP_NAME.app"
BUILD_DIR="$REPO_ROOT/build/install-local"
TEAM_ID="AJC82Q6789"
SIGN_IDENTITY="Developer ID Application"

# ── Args ──────────────────────────────────────────────────────────────────────
CONFIGURATION="Release"
DO_LAUNCH=1
DO_QUIT=1
FORCE=0
for arg in "$@"; do
    case "$arg" in
        --debug)        CONFIGURATION="Debug" ;;
        --no-launch)    DO_LAUNCH=0 ;;
        --keep-running) DO_QUIT=0 ;;
        --force)        FORCE=1 ;;
        --help|-h)
            sed -n '2,18p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *)
            echo "unknown arg: $arg" >&2
            echo "(try --help)" >&2
            exit 1
            ;;
    esac
done

# ── Pretty output ─────────────────────────────────────────────────────────────
say()  { printf "▶ %s\n" "$*"; }
ok()   { printf "✓ %s\n" "$*"; }
warn() { printf "⚠ %s\n" "$*" >&2; }
die()  { printf "✗ %s\n" "$*" >&2; exit 1; }

# ── Sanity checks ─────────────────────────────────────────────────────────────
[ -d "$PROJECT" ] || die "Xcode project not found at $PROJECT"

if [ "$FORCE" -eq 0 ] && ! git -C "$REPO_ROOT" diff-index --quiet HEAD -- 2>/dev/null; then
    warn "Working tree has uncommitted changes — building them anyway."
    warn "(pass --force to silence, or commit first)"
fi

# ── 1. Quit running instance ──────────────────────────────────────────────────
if [ "$DO_QUIT" -eq 1 ]; then
    if pgrep -f "$INSTALLED_PATH/Contents/MacOS/" >/dev/null 2>&1; then
        say "Quitting running instance…"
        osascript -e "tell application \"$APP_NAME\" to quit" 2>/dev/null || true
        # AppleScript quit can be slow; give it a moment, then force-kill
        # anything that's still around (the engine subprocess in particular
        # doesn't always exit cleanly on app quit).
        for _ in 1 2 3 4 5; do
            sleep 0.4
            pgrep -f "$INSTALLED_PATH/Contents/MacOS/" >/dev/null 2>&1 || break
        done
        if pgrep -f "$INSTALLED_PATH/Contents/" >/dev/null 2>&1; then
            pkill -f "$INSTALLED_PATH/Contents/" || true
            sleep 0.5
        fi
    fi
fi

# ── 2. Ensure Python runtime is staged ────────────────────────────────────────
# The Xcode "Run Script" phase copies python-runtime/ into the bundle and signs
# the dylibs. The fetch script is idempotent — fast no-op if already present.
say "Checking bundled Python runtime…"
"$REPO_ROOT/scripts/fetch-python-runtime.sh" >/dev/null

# ── 3. Build ─────────────────────────────────────────────────────────────────
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"

ARCHIVE="$BUILD_DIR/ZoomNotes.xcarchive"
EXPORT_DIR="$BUILD_DIR/export"
APP_PATH="$EXPORT_DIR/$APP_NAME.app"

if [ "$CONFIGURATION" = "Release" ]; then
    say "Building Release (signed, ~25s)…"
    xcodebuild archive \
        -project "$PROJECT" \
        -scheme "$SCHEME" \
        -configuration Release \
        -archivePath "$ARCHIVE" \
        -destination "generic/platform=macOS" \
        CODE_SIGN_IDENTITY="$SIGN_IDENTITY" \
        CODE_SIGN_STYLE=Manual \
        DEVELOPMENT_TEAM="$TEAM_ID" \
        > "$BUILD_DIR/archive.log" 2>&1 \
        || { tail -40 "$BUILD_DIR/archive.log" >&2; die "Archive failed (full log: $BUILD_DIR/archive.log)"; }

    cat > "$BUILD_DIR/ExportOptions.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>method</key>
    <string>developer-id</string>
    <key>teamID</key>
    <string>$TEAM_ID</string>
    <key>signingStyle</key>
    <string>manual</string>
    <key>signingCertificate</key>
    <string>$SIGN_IDENTITY</string>
</dict>
</plist>
EOF

    xcodebuild -exportArchive \
        -archivePath "$ARCHIVE" \
        -exportOptionsPlist "$BUILD_DIR/ExportOptions.plist" \
        -exportPath "$EXPORT_DIR" \
        > "$BUILD_DIR/export.log" 2>&1 \
        || { tail -40 "$BUILD_DIR/export.log" >&2; die "Export failed (full log: $BUILD_DIR/export.log)"; }
else
    say "Building Debug (ad-hoc signed, ~5s)…"
    xcodebuild build \
        -project "$PROJECT" \
        -scheme "$SCHEME" \
        -configuration Debug \
        -derivedDataPath "$BUILD_DIR/derived" \
        CODE_SIGN_IDENTITY="-" \
        CODE_SIGN_STYLE=Manual \
        > "$BUILD_DIR/build.log" 2>&1 \
        || { tail -40 "$BUILD_DIR/build.log" >&2; die "Build failed (full log: $BUILD_DIR/build.log)"; }
    APP_PATH="$BUILD_DIR/derived/Build/Products/Debug/$APP_NAME.app"
fi

[ -d "$APP_PATH" ] || die "Built app not found at $APP_PATH"

# ── 4. Install ────────────────────────────────────────────────────────────────
say "Installing to ${INSTALLED_PATH}…"
rm -rf "$INSTALLED_PATH"
cp -R "$APP_PATH" "$INSTALLED_PATH"

# ── 5. Launch ─────────────────────────────────────────────────────────────────
VERSION=$(defaults read "$INSTALLED_PATH/Contents/Info.plist" CFBundleShortVersionString 2>/dev/null || echo "?")
SHA=$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo "no-git")

if [ "$DO_LAUNCH" -eq 1 ]; then
    say "Launching…"
    open "$INSTALLED_PATH"
    sleep 1.5
    if ! pgrep -f "$INSTALLED_PATH/Contents/MacOS/" >/dev/null 2>&1; then
        warn "Launched but process not detected — check Console.app for errors."
    fi
fi

ok "$APP_NAME $VERSION ($SHA, $CONFIGURATION) installed and running"
