#!/bin/bash
APP_NAME="claude-usage-monitor"

echo "=== Claude Usage Monitor - Uninstaller ==="

# Stop any running tray processes (one per provider)
pkill -f "monitor.py --provider" 2>/dev/null && echo "[OK] Stopped running monitors"

# Remove autostart + launcher entries. The bare name is the pre-provider
# layout; the -* forms are the per-provider ones.
rm -f "$HOME/.config/autostart/$APP_NAME.desktop" \
      "$HOME/.config/autostart/$APP_NAME"-*.desktop
echo "[OK] Removed autostart entries"

rm -f "$HOME/.local/share/applications/$APP_NAME.desktop" \
      "$HOME/.local/share/applications/$APP_NAME"-*.desktop
echo "[OK] Removed application launchers"

# Remove the installed icon
rm -f "$HOME/.local/share/icons/$APP_NAME.png"

# Remove config (ask first)
CONFIG_DIR="$HOME/.config/$APP_NAME"
if [ -d "$CONFIG_DIR" ]; then
    read -p "Remove config ($CONFIG_DIR)? [y/N] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        rm -rf "$CONFIG_DIR"
        echo "[OK] Removed config"
    fi
fi

echo ""
echo "Uninstall complete. You can safely delete this directory."
