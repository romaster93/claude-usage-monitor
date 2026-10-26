# Claude Usage Monitor

A lightweight Ubuntu system tray application that displays your Claude Code usage as a percentage,
plus Codex, Grok and Antigravity (agy) usage when those CLIs are installed.

![Python](https://img.shields.io/badge/python-3.10+-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Platform](https://img.shields.io/badge/platform-Linux-lightgrey)

## Features

- **One pixel-art tray icon per provider**: a brand-coloured tile with the
  provider's mark (Claude, OpenAI/Codex, xAI/Grok, Gemini/Antigravity) next to
  its color-coded usage number, so you can tell them apart without clicking
- **One combined panel**: clicking any of the icons opens a single panel with a
  section per provider (Claude, Codex, Grok, Antigravity)
- **Auto-refresh**: Claude every 5 minutes; Codex every minute, live from
  `codex app-server` (`account/rateLimits/read`, a read that spends no quota),
  falling back to its session logs; Grok and agy within ~2 seconds of their CLI
  rewriting its usage file
- **Zero configuration** - reads credentials directly from Claude Code
- Color-coded usage levels: green (<40%), yellow (<70%), orange (<90%), red (>=90%)
- Catppuccin-themed detail panel with progress bars and reset timers

## How It Works

The monitor reads your OAuth token from `~/.claude/.credentials.json` (created when you log into Claude Code) and makes a minimal API call to retrieve rate limit headers. No API key setup required.

## Requirements

- Ubuntu / Linux (X11; Wayland may work but is untested)
- Python 3.10+
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed and logged in
- System tray backend: on Debian/Ubuntu install `python3-gi`, `gir1.2-gtk-3.0`,
  `gir1.2-ayatanaappindicator3-0.1` (the installer does this for you). On vanilla
  GNOME, also enable the "AppIndicator and KStatusNotifierItem Support" extension —
  Ubuntu ships it enabled by default. Without these the tray icon may not appear.

## Installation

```bash
git clone https://github.com/romaster93/claude-usage-monitor.git
cd claude-usage-monitor
./install.sh
```

This will:
1. Install Python dependencies (`pystray`, `Pillow`)
2. Create a desktop autostart entry (starts on login)
3. Create an application launcher

## Manual Setup

```bash
pip install -r requirements.txt
python3 monitor.py
```

## Usage

- **Click** any tray icon to toggle the combined panel (all icons share one panel)
- Click outside the panel, move the pointer out of it, or press **Escape** to close it
- **Refresh** re-reads every provider shown in the panel and updates every tray icon
- **Quit** stops every provider's tray monitor (the icons are one bundle);
  each icon's right-click menu still has its own Quit
- The icon auto-starts on login after installation

## Detail Panel

The panel shows one section per provider that is installed (it has a usage
cache or a running tray monitor). Each section has:
- The provider's pixel brand mark, name and plan
- Current rate limit status
- When the reading was taken; for Codex, Grok and agy the age is always shown,
  in orange once it is over an hour old. Grok and agy only update while you run
  that CLI (agy: while its statusLine is redrawn); Codex does too if its live
  query fails and the session-log fallback is used
- One compact row per usage window: utilization %, progress bar and reset countdown

## Uninstall

```bash
./uninstall.sh
```

## License

MIT
