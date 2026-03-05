#!/bin/bash
APP_NAME="claude-usage-monitor"

echo "=== Claude Usage Monitor - Uninstaller ==="

# Remove autostart
rm -f "$HOME/.config/autostart/$APP_NAME.desktop"
echo "[OK] Removed autostart entry"

# Remove launcher
rm -f "$HOME/.local/share/applications/$APP_NAME.desktop"
echo "[OK] Removed application launcher"

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

# Remove keyring entry
python3 -c "
import keyring
try:
    keyring.delete_password('claude-usage-monitor', 'anthropic-api-key')
    print('[OK] Removed stored API key from keyring')
except:
    print('[OK] No stored API key found')
" 2>/dev/null

echo ""
echo "Uninstall complete. You can safely delete this directory."
