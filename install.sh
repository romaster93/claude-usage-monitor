#!/bin/bash
set -e

APP_NAME="claude-usage-monitor"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MONITOR_PY="$SCRIPT_DIR/monitor.py"

echo "=== Claude Usage Monitor - Installer ==="

# Check Python
PYTHON="$(which python3 2>/dev/null)"
if [ -z "$PYTHON" ]; then
    echo "Error: python3 not found"
    exit 1
fi
echo "[OK] Python: $PYTHON"

# Install Python dependencies
echo "Installing dependencies..."
"$PYTHON" -m pip install --user --quiet pystray pillow keyring 2>/dev/null || \
"$PYTHON" -m pip install --user --break-system-packages --quiet pystray pillow keyring 2>/dev/null || \
echo "[WARN] pip install had issues - continuing anyway"

# Verify imports
"$PYTHON" -c "import pystray, PIL, keyring" 2>/dev/null || {
    echo "Error: Required Python modules not available"
    exit 1
}
echo "[OK] Dependencies installed"

# Make executable
chmod +x "$MONITOR_PY"
echo "[OK] Made monitor.py executable"

# Create desktop autostart entry
AUTOSTART_DIR="$HOME/.config/autostart"
mkdir -p "$AUTOSTART_DIR"
cat > "$AUTOSTART_DIR/$APP_NAME.desktop" << EOF
[Desktop Entry]
Type=Application
Name=Claude Usage Monitor
Comment=Monitor Claude Code usage in system tray
Exec=$PYTHON $MONITOR_PY
Icon=utilities-system-monitor
Terminal=false
Hidden=false
X-GNOME-Autostart-enabled=true
StartupNotify=false
EOF

echo "[OK] Autostart entry created"

# Create .desktop launcher
LAUNCHER_DIR="$HOME/.local/share/applications"
mkdir -p "$LAUNCHER_DIR"
cat > "$LAUNCHER_DIR/$APP_NAME.desktop" << EOF
[Desktop Entry]
Type=Application
Name=Claude Usage Monitor
Comment=Monitor Claude Code usage in system tray
Exec=$PYTHON $MONITOR_PY
Icon=utilities-system-monitor
Terminal=false
Categories=Utility;Monitor;
EOF

echo "[OK] Application launcher created"

# Check Claude Code credentials
if [ -f "$HOME/.claude/.credentials.json" ]; then
    echo "[OK] Claude Code credentials found"
else
    echo "[WARN] Claude Code credentials not found"
    echo "       Run 'claude' and log in first"
fi

echo ""
echo "Installation complete!"
echo ""
echo "Usage:"
echo "  Start now:    python3 $MONITOR_PY"
echo "  Auto-starts on login"
echo ""
echo "Reads credentials from ~/.claude/.credentials.json automatically."
echo "Just make sure you're logged into Claude Code."
