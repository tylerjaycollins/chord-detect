#!/bin/bash
# Build the Overtone desktop app and install it to ~/Applications.
# Only needed when desktop/ changes: Python edits take effect on the next
# launch (or the page's Restart) without rebuilding.
set -euo pipefail
export PATH="$HOME/.cargo/bin:/opt/homebrew/bin:$PATH"
cd "$(dirname "$0")"

npx --yes @tauri-apps/cli@2.12.4 build

DEST="$HOME/Applications/Overtone.app"
osascript -e 'tell application "Overtone" to quit' >/dev/null 2>&1 || true
rm -rf "$DEST"
cp -R target/release/bundle/macos/Overtone.app "$DEST"
echo "Installed $DEST"
