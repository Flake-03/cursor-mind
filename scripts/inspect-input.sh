#!/usr/bin/env bash
set -euo pipefail
# Read-only report: no input devices, focus or keymaps are modified.
echo "=== X11 input hierarchy ==="
xinput list --short
echo "=== Keyboard configuration ==="
setxkbmap -query
echo "=== CursorMind processes ==="
pgrep -af 'cursormind|CursorMind' || true
