#!/bin/bash
# fetch-python-runtime.sh — downloads and assembles a universal Python 3.12
# runtime into python-runtime/ at the repo root.
#
# Called automatically by release.sh and the Xcode build script.
# Safe to re-run: skips download if python-runtime/bin/python3.12 already exists.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$REPO_ROOT/python-runtime"
PYTHON_VERSION="3.12.13"
PBS_TAG="20260414"
BASE_URL="https://github.com/astral-sh/python-build-standalone/releases/download/$PBS_TAG"

if [ -f "$DEST/bin/python3.12" ]; then
    echo "▶ Python runtime already present at python-runtime/ — skipping download."
    exit 0
fi

echo "▶ Fetching Python $PYTHON_VERSION universal runtime…"
STAGING="$REPO_ROOT/build/python-staging"
mkdir -p "$STAGING"

ARM_URL="$BASE_URL/cpython-${PYTHON_VERSION}%2B${PBS_TAG}-aarch64-apple-darwin-install_only_stripped.tar.gz"
X86_URL="$BASE_URL/cpython-${PYTHON_VERSION}%2B${PBS_TAG}-x86_64-apple-darwin-install_only_stripped.tar.gz"

echo "  Downloading arm64…"
curl -sL "$ARM_URL" -o "$STAGING/arm64.tar.gz"
echo "  Downloading x86_64…"
curl -sL "$X86_URL" -o "$STAGING/x86_64.tar.gz"

echo "  Extracting…"
mkdir -p "$STAGING/arm64" "$STAGING/x86_64"
tar -xzf "$STAGING/arm64.tar.gz" -C "$STAGING/arm64"
tar -xzf "$STAGING/x86_64.tar.gz" -C "$STAGING/x86_64"

echo "  Building universal binary…"
cp -R "$STAGING/arm64/python/" "$DEST/"

# Merge main executable
lipo -create \
    "$STAGING/arm64/python/bin/python3.12" \
    "$STAGING/x86_64/python/bin/python3.12" \
    -output "$DEST/bin/python3.12"

# Merge .so extension modules
for arm_so in "$STAGING/arm64/python/lib/python3.12/lib-dynload/"*.so; do
    name=$(basename "$arm_so")
    x86_so="$STAGING/x86_64/python/lib/python3.12/lib-dynload/$name"
    dest_so="$DEST/lib/python3.12/lib-dynload/$name"
    [ -f "$x86_so" ] && lipo -create "$arm_so" "$x86_so" -output "$dest_so" 2>/dev/null || true
done

# Merge libpython dylib
for f in "$STAGING/arm64/python/lib/libpython"*.dylib; do
    name=$(basename "$f")
    x86f="$STAGING/x86_64/python/lib/$name"
    [ -f "$x86f" ] && lipo -create "$f" "$x86f" -output "$DEST/lib/$name" 2>/dev/null || true
done

# Strip unnecessary modules and Tcl/Tk (causes codesign failures — not needed)
rm -rf \
    "$DEST/lib/python3.12/test" \
    "$DEST/lib/python3.12/idlelib" \
    "$DEST/lib/python3.12/tkinter" \
    "$DEST/lib/python3.12/turtledemo" \
    "$DEST/lib/python3.12/ensurepip" \
    "$DEST/lib/python3.12/lib2to3" \
    "$DEST/lib/python3.12/__phello__" \
    "$DEST/lib/python3.12/lib-dynload/_tkinter"*.so \
    "$DEST/lib/itcl"* \
    "$DEST/lib/libtcl"* \
    "$DEST/lib/libtk"* \
    "$DEST/lib/tcl"* \
    "$DEST/lib/thread"* \
    "$DEST/lib/tk"* \
    "$DEST/lib/pkgconfig" \
    "$DEST/include" \
    "$DEST/share" \
    "$DEST/bin/idle3"* \
    "$DEST/bin/pip"* 2>/dev/null || true

# Remove __pycache__ dirs
find "$DEST" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

# Clean up staging tarballs
rm -f "$STAGING/arm64.tar.gz" "$STAGING/x86_64.tar.gz"

echo "  ✓ Python runtime ready: $(du -sh "$DEST" | cut -f1)"
"$DEST/bin/python3.12" --version
