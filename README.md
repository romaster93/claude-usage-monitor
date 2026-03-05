# Claude Usage Monitor

A lightweight Ubuntu system tray application that displays your Claude Code usage as a percentage.

![Python](https://img.shields.io/badge/python-3.10+-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Platform](https://img.shields.io/badge/platform-Linux-lightgrey)

## Features

- **System tray indicator** showing current usage as a bold, color-coded number
- **Dropdown panel** with detailed usage breakdown (5-hour and 7-day windows)
- **Auto-refresh** every 5 minutes
- **Zero configuration** - reads credentials directly from Claude Code
- Color-coded usage levels: green (<40%), yellow (<70%), orange (<90%), red (>=90%)
- Catppuccin-themed detail panel with progress bars and reset timers

## How It Works

The monitor reads your OAuth token from `~/.claude/.credentials.json` (created when you log into Claude Code) and makes a minimal API call to retrieve rate limit headers. No API key setup required.

## Requirements

- Ubuntu / Linux with X11
- Python 3.10+
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed and logged in

## Installation

```bash
git clone https://github.com/romaster93/claude-usage-monitor.git
cd claude-usage-monitor
./install.sh
```

This will:
1. Install Python dependencies (`pystray`, `Pillow`, `keyring`)
2. Create a desktop autostart entry (starts on login)
3. Create an application launcher

## Manual Setup

```bash
pip install -r requirements.txt
python3 monitor.py
```

## Usage

- **Left-click** the tray icon to toggle the detail panel
- **Right-click** for menu (Show Details / Refresh Now / Quit)
- Click outside the panel or press **Escape** to close it
- The icon auto-starts on login after installation

## Detail Panel

The panel shows:
- **5-hour window** utilization with progress bar
- **7-day window** utilization with progress bar
- Reset countdown timers for each window
- Current rate limit status

## Uninstall

```bash
./uninstall.sh
```

## License

MIT
