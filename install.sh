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

# System tray backend (pip cannot provide these). The pystray Linux backend
# needs GTK + AppIndicator/StatusNotifier typelibs. Skip the install entirely
# if a working backend is already present; non-fatal otherwise (XEmbed-tray
# desktops like XFCE/KDE/MATE work without them).
check_tray_backend() {
    "$PYTHON" -c "import gi; gi.require_version('Gtk','3.0'); gi.require_version('AyatanaAppIndicator3','0.1'); from gi.repository import Gtk, AyatanaAppIndicator3" 2>/dev/null \
      || "$PYTHON" -c "import gi; gi.require_version('Gtk','3.0'); gi.require_version('AppIndicator3','0.1'); from gi.repository import Gtk, AppIndicator3" 2>/dev/null
}
if check_tray_backend; then
    echo "[OK] AppIndicator tray backend already available"
elif command -v apt-get >/dev/null 2>&1; then
    echo "Installing AppIndicator system packages (may prompt for sudo)..."
    sudo apt-get install -y python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1 \
        || echo "[WARN] Could not install AppIndicator packages; tray icon may not appear on GNOME"
else
    echo "[WARN] Non-apt distro: ensure PyGObject + Gtk3 + AppIndicator (Ayatana) typelibs are installed"
fi

# Install Python dependencies
echo "Installing dependencies..."
"$PYTHON" -m pip install --user --quiet pystray pillow 2>/dev/null || \
"$PYTHON" -m pip install --user --break-system-packages --quiet pystray pillow 2>/dev/null || \
echo "[WARN] pip install had issues - continuing anyway"

# Verify imports
"$PYTHON" -c "import pystray, PIL" 2>/dev/null || {
    echo "Error: Required Python modules not available"
    exit 1
}
echo "[OK] Dependencies installed"

# Final reminder if the tray backend still isn't usable (e.g. apt step failed)
check_tray_backend || echo "[WARN] AppIndicator tray backend unavailable - on vanilla GNOME, install & enable the 'AppIndicator and KStatusNotifierItem Support' extension"

# Make executable
chmod +x "$MONITOR_PY"
echo "[OK] Made monitor.py executable"

# Install the application icon used by the launcher and autostart entries
ICON_DEST="$HOME/.local/share/icons/$APP_NAME.png"
if [ -f "$SCRIPT_DIR/icon.png" ]; then
    mkdir -p "$HOME/.local/share/icons"
    cp "$SCRIPT_DIR/icon.png" "$ICON_DEST"
    echo "[OK] Installed application icon -> $ICON_DEST"
else
    ICON_DEST="utilities-system-monitor"
    echo "[WARN] icon.png not found - using generic system icon"
fi

# Create desktop autostart entry
AUTOSTART_DIR="$HOME/.config/autostart"
mkdir -p "$AUTOSTART_DIR"
cat > "$AUTOSTART_DIR/$APP_NAME.desktop" << EOF
[Desktop Entry]
Type=Application
Name=Claude Usage Monitor
Comment=Monitor Claude Code usage in system tray
Exec="$PYTHON" "$MONITOR_PY"
Icon=$ICON_DEST
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
Exec="$PYTHON" "$MONITOR_PY"
Icon=$ICON_DEST
Terminal=false
Categories=Utility;Monitor;
StartupNotify=false
EOF

echo "[OK] Application launcher created"

# Refresh the application database so the launcher shows up immediately
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$LAUNCHER_DIR" 2>/dev/null || true
fi
# Refresh the icon cache (best effort; harmless if absent)
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f -t "$HOME/.local/share/icons" 2>/dev/null || true
fi

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
