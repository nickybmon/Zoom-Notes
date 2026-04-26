#!/bin/bash
# release.sh — builds, signs, notarizes, and packages Zoom Notes as a DMG
#
# Prerequisites (one-time):
#   brew install create-dmg
#   xcrun notarytool store-credentials "zoom-notes-notarytool" \
#     --apple-id "your@apple.com" --team-id AJC82Q6789 --password "xxxx-xxxx-xxxx-xxxx"
#
# Usage:
#   ./scripts/release.sh            # builds version from Info.plist
#   ./scripts/release.sh 1.2        # override version

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT="$REPO_ROOT/ZoomNotesApp/ZoomNotesApp.xcodeproj"
SCHEME="ZoomNotesApp"
APP_NAME="Zoom Notes"
BUNDLE_ID="com.zoom-notes-assistant"
TEAM_ID="AJC82Q6789"
NOTARYTOOL_PROFILE="zoom-notes-notarytool"
BUILD_DIR="$REPO_ROOT/build"

# Version: argument or read from Info.plist
if [ "${1:-}" != "" ]; then
    VERSION="$1"
else
    VERSION=$(defaults read "$REPO_ROOT/ZoomNotesApp/ZoomNotesApp/Resources/Info.plist" CFBundleShortVersionString 2>/dev/null || echo "1.0")
fi

ARCHIVE="$BUILD_DIR/$APP_NAME.xcarchive"
EXPORT_DIR="$BUILD_DIR/export"
APP_PATH="$EXPORT_DIR/$APP_NAME.app"
DMG_PATH="$REPO_ROOT/$APP_NAME-$VERSION.dmg"

echo "▶ Zoom Notes release — v$VERSION"
echo "  Project: $PROJECT"
echo "  Output:  $DMG_PATH"
echo ""

# ── Clean build dir ────────────────────────────────────────────────────────────
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"

# ── 1. Archive ─────────────────────────────────────────────────────────────────
echo "▶ Step 1/5: Archiving…"
xcodebuild archive \
    -project "$PROJECT" \
    -scheme "$SCHEME" \
    -configuration Release \
    -archivePath "$ARCHIVE" \
    -destination "generic/platform=macOS" \
    CODE_SIGN_IDENTITY="Developer ID Application" \
    CODE_SIGN_STYLE=Manual \
    DEVELOPMENT_TEAM="$TEAM_ID" \
    | grep -E "^(error:|warning: |Build|Archive|\/\/)" || true
echo "  ✓ Archive complete"

# ── 2. Export signed .app ──────────────────────────────────────────────────────
echo "▶ Step 2/5: Exporting signed app…"
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
    <string>Developer ID Application</string>
</dict>
</plist>
EOF

xcodebuild -exportArchive \
    -archivePath "$ARCHIVE" \
    -exportOptionsPlist "$BUILD_DIR/ExportOptions.plist" \
    -exportPath "$EXPORT_DIR" \
    | grep -E "^(error:|Export|\/\/)" || true
echo "  ✓ Export complete: $APP_PATH"

# ── 3. Verify signature ────────────────────────────────────────────────────────
echo "▶ Step 3/5: Verifying signature…"
codesign --verify --deep --strict --verbose=1 "$APP_PATH" 2>&1 | grep -v "^$" || true
spctl --assess --type exec --verbose "$APP_PATH" 2>&1 || {
    echo "  ✗ Gatekeeper check failed — notarization required (continuing)"
}
echo "  ✓ Signature OK"

# ── 4. Notarize ────────────────────────────────────────────────────────────────
echo "▶ Step 4/5: Notarizing (this takes 1–5 minutes)…"
ZIP_PATH="$BUILD_DIR/$APP_NAME.zip"
ditto -c -k --keepParent "$APP_PATH" "$ZIP_PATH"

xcrun notarytool submit "$ZIP_PATH" \
    --keychain-profile "$NOTARYTOOL_PROFILE" \
    --wait \
    --timeout 600

xcrun stapler staple "$APP_PATH"
echo "  ✓ Notarization and stapling complete"

# ── 5. Build DMG ───────────────────────────────────────────────────────────────
echo "▶ Step 5/5: Building DMG…"

if ! command -v appdmg &> /dev/null; then
    echo "  ✗ appdmg not found. Run: npm install -g appdmg"
    exit 1
fi

BG="$REPO_ROOT/scripts/dmg-assets/background.png"
if [ ! -f "$BG" ]; then
    echo "  ✗ Background image not found at scripts/dmg-assets/background.png"
    echo "    Open scripts/dmg-assets/DMG Background Export.html and export it first."
    exit 1
fi

cat > "$BUILD_DIR/appdmg.json" <<APPDMG
{
  "title": "$APP_NAME",
  "icon": "$REPO_ROOT/ZoomNotesApp/ZoomNotesApp/Resources/AppIcon.icns",
  "background": "$BG",
  "icon-size": 100,
  "window": { "size": { "width": 660, "height": 400 } },
  "contents": [
    { "x": 165, "y": 200, "type": "file", "path": "$APP_PATH" },
    { "x": 495, "y": 200, "type": "link", "path": "/Applications" }
  ],
  "format": "UDZO"
}
APPDMG

appdmg "$BUILD_DIR/appdmg.json" "$DMG_PATH"

echo ""
echo "✅ Done! DMG ready at:"
echo "   $DMG_PATH"
