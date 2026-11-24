#!/usr/bin/env bash
# Removes PyStudio. Your projects are not touched.
set -e
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
rm -rf "$HOME/.local/share/pystudio"
rm -f "$HOME/.local/bin/pystudio"
rm -f "$HOME/.local/share/applications/pystudio.desktop"
rm -f "$DESKTOP_DIR/pystudio.desktop"
rm -rf "$HOME/.config/PyStudio"
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
echo "PyStudio removed."
