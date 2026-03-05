#!/usr/bin/env python3
"""Claude Code Usage Monitor - Ubuntu system tray indicator.

Reads the OAuth token from Claude Code's credentials file and displays
real-time usage (5h / 7d windows) in the system tray with a dropdown panel.
"""

import json
import os
import signal
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import math

import pystray
from PIL import Image, ImageDraw, ImageFont

APP_NAME = "claude-usage-monitor"
CONFIG_DIR = Path.home() / ".config" / APP_NAME
CACHE_FILE = CONFIG_DIR / "usage_cache.json"
CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"

REFRESH_INTERVAL = 300
PROBE_MODEL = "claude-haiku-4-5-20251001"

# ── Theme ────────────────────────────────────────────────────────

THEME = {
    "bg": "#1e1e2e",
    "surface": "#313244",
    "surface2": "#45475a",
    "text": "#cdd6f4",
    "subtext": "#a6adc8",
    "green": "#a6e3a1",
    "yellow": "#f9e2af",
    "orange": "#fab387",
    "red": "#f38ba8",
    "blue": "#89b4fa",
    "mauve": "#cba6f7",
    "teal": "#94e2d5",
    "pink": "#f5c2e7",
    "border": "#585b70",
}


# ── Token ────────────────────────────────────────────────────────

def get_token() -> str | None:
    if not CREDENTIALS_FILE.exists():
        return None
    try:
        with open(CREDENTIALS_FILE) as f:
            creds = json.load(f)
        return creds.get("claudeAiOauth", {}).get("accessToken")
    except (json.JSONDecodeError, OSError):
        return None


# ── Cache ────────────────────────────────────────────────────────

def load_cache() -> dict | None:
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None
    return None


def save_cache(data: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ── API ──────────────────────────────────────────────────────────

def fetch_usage(token: str) -> dict:
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": token,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = json.dumps({
        "model": PROBE_MODEL,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "."}],
    }).encode()

    req = Request(url, data=body, headers=headers, method="POST")
    resp_headers = {}

    try:
        resp = urlopen(req, timeout=15)
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        resp.read()
    except HTTPError as e:
        resp_headers = {k.lower(): v for k, v in e.headers.items()}
        if e.code == 401:
            return {"error": "Token expired - restart Claude Code"}
        if e.code != 429 and e.code != 400:
            return {"error": f"API error: {e.code}"}
    except (URLError, OSError) as e:
        return {"error": f"Network error: {e}"}

    return parse_unified_limits(resp_headers)


def parse_unified_limits(headers: dict) -> dict:
    data = {"fetched_at": datetime.now(timezone.utc).isoformat()}
    prefix = "anthropic-ratelimit-unified-"

    data["status"] = headers.get(f"{prefix}status", "")
    data["representative_claim"] = headers.get(f"{prefix}representative-claim", "")
    data["fallback_pct"] = headers.get(f"{prefix}fallback-percentage", "")
    data["overage_status"] = headers.get(f"{prefix}overage-status", "")
    data["overage_disabled_reason"] = headers.get(f"{prefix}overage-disabled-reason", "")

    windows = {}
    for k, v in headers.items():
        if not k.startswith(prefix):
            continue
        rest = k[len(prefix):]
        for window_id in ("5h", "7d", "24h", "1h"):
            wp = f"{window_id}-"
            if rest.startswith(wp):
                windows.setdefault(window_id, {})[rest[len(wp):]] = v

    data["windows"] = windows
    if not data["status"] and not windows:
        data["error"] = "No rate limit headers received"
    return data


# ── Icon ─────────────────────────────────────────────────────────

def get_primary_percent(data: dict | None) -> int | None:
    if not data or "error" in data:
        return None
    windows = data.get("windows", {})
    rep = data.get("representative_claim", "").replace("five_hour", "5h").replace("seven_day", "7d")
    for key in [rep, "5h", "7d", "24h", "1h"]:
        if key in windows and "utilization" in windows[key]:
            return int(float(windows[key]["utilization"]) * 100)
    return None


def _color_for_pct(pct: int) -> tuple:
    if pct < 40:
        return (0, 255, 65)      # neon green
    if pct < 70:
        return (255, 255, 0)     # neon yellow
    if pct < 90:
        return (255, 160, 0)     # neon orange
    return (255, 40, 40)         # neon red


def make_icon(percent: int | None, error: bool = False,
              anim_frame: int = 0) -> Image.Image:
    """Big bold number, rendered at 4x then downscaled for clean result."""
    OUT = 24
    SCALE = 4
    R = OUT * SCALE  # render size
    img = Image.new("RGBA", (R, R), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    if error:
        text = "!"
        color = (255, 40, 40)
    elif percent is None:
        text = "--"
        color = (0, 200, 255)
    else:
        text = str(percent)
        color = _color_for_pct(percent)

    try:
        if len(text) <= 1:
            fs = 80
        elif len(text) == 2:
            fs = 64
        else:
            fs = 48
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", fs)
    except (IOError, OSError):
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (R - tw) // 2
    y = (R - th) // 2

    # Clean black outline at 4x (becomes smooth after downscale)
    for dx in range(-3, 4):
        for dy in range(-3, 4):
            if dx == 0 and dy == 0:
                continue
            draw.text((x + dx, y + dy), text, fill=(0, 0, 0), font=font)

    draw.text((x, y), text, fill=color, font=font)

    return img.resize((OUT, OUT), Image.LANCZOS)


# ── Dropdown Panel ───────────────────────────────────────────────

def format_reset(reset_val: str) -> str:
    if not reset_val:
        return "N/A"
    try:
        ts = float(reset_val)
        reset_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    except (ValueError, TypeError, OSError):
        try:
            reset_dt = datetime.fromisoformat(reset_val.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return reset_val

    diff = reset_dt - datetime.now(timezone.utc)
    total_sec = max(0, int(diff.total_seconds()))
    hours, rem = divmod(total_sec, 3600)
    minutes, _ = divmod(rem, 60)
    if hours > 0:
        return f"{hours}h {minutes}m"
    if minutes > 0:
        return f"{minutes}m"
    return "<1m"


WINDOW_LABELS = {"5h": "5 Hours", "7d": "7 Days", "24h": "24 Hours", "1h": "1 Hour"}
WINDOW_EMOJI = {"5h": "\u23f1", "7d": "\U0001f4c5", "24h": "\u2600", "1h": "\u26a1"}


class DropdownPanel:
    """A floating dropdown panel that appears near the system tray."""

    def __init__(self):
        self._win: tk.Tk | None = None
        self._lock = threading.Lock()

    def _color_for_percent(self, pct: int) -> str:
        if pct < 40:
            return THEME["green"]
        if pct < 70:
            return THEME["yellow"]
        if pct < 90:
            return THEME["orange"]
        return THEME["red"]

    def show(self, data: dict | None, pos: tuple | None = None):
        with self._lock:
            if self._win is not None:
                try:
                    self._win.destroy()
                except tk.TclError:
                    pass
                self._win = None

        win = tk.Tk()
        self._win = win
        win.title("")
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=THEME["border"])

        # Use passed position (captured at click time) or fallback to current mouse
        if pos:
            mx, my = pos
        else:
            mx, my = win.winfo_pointerx(), win.winfo_pointery()

        # Main container with 1px border effect
        outer = tk.Frame(win, bg=THEME["border"], padx=1, pady=1)
        outer.pack(fill=tk.BOTH, expand=True)

        container = tk.Frame(outer, bg=THEME["bg"], padx=0, pady=0)
        container.pack(fill=tk.BOTH, expand=True)

        # Header
        header = tk.Frame(container, bg=THEME["surface"], padx=16, pady=10)
        header.pack(fill=tk.X)

        tk.Label(header, text="\U0001f916  Claude Code Usage",
                 font=("sans-serif", 12, "bold"),
                 bg=THEME["surface"], fg=THEME["text"]).pack(side=tk.LEFT)

        status = (data or {}).get("status", "unknown")
        status_color = THEME["green"] if status == "allowed" else THEME["red"]
        status_text = "\u25cf " + status.upper()
        tk.Label(header, text=status_text, font=("sans-serif", 9),
                 bg=THEME["surface"], fg=status_color).pack(side=tk.RIGHT)

        # Body
        body = tk.Frame(container, bg=THEME["bg"], padx=16, pady=12)
        body.pack(fill=tk.X)

        if not data or "error" in data:
            err_msg = (data or {}).get("error", "No data yet")
            tk.Label(body, text=f"\u26a0  {err_msg}",
                     font=("sans-serif", 10), bg=THEME["bg"],
                     fg=THEME["orange"], wraplength=280,
                     justify=tk.LEFT).pack(anchor=tk.W)
        else:
            windows = data.get("windows", {})
            rep = data.get("representative_claim", "").replace(
                "five_hour", "5h").replace("seven_day", "7d")

            for i, wid in enumerate(("5h", "7d", "24h", "1h")):
                if wid not in windows:
                    continue
                w = windows[wid]
                util = float(w.get("utilization", 0))
                pct = int(util * 100)
                is_rep = (wid == rep)

                if i > 0:
                    # Separator
                    tk.Frame(body, bg=THEME["surface2"],
                             height=1).pack(fill=tk.X, pady=8)

                row = tk.Frame(body, bg=THEME["bg"])
                row.pack(fill=tk.X)

                emoji = WINDOW_EMOJI.get(wid, "")
                label = WINDOW_LABELS.get(wid, wid)
                badge = "  \u25c0" if is_rep else ""
                tk.Label(row, text=f"{emoji}  {label}{badge}",
                         font=("sans-serif", 11, "bold" if is_rep else ""),
                         bg=THEME["bg"], fg=THEME["text"]).pack(
                    side=tk.LEFT, anchor=tk.W)

                tk.Label(row, text=f"{pct}%",
                         font=("sans-serif", 14, "bold"),
                         bg=THEME["bg"],
                         fg=self._color_for_percent(pct)).pack(
                    side=tk.RIGHT, anchor=tk.E)

                # Progress bar
                bar_frame = tk.Frame(body, bg=THEME["surface"],
                                     height=10, padx=0, pady=0)
                bar_frame.pack(fill=tk.X, pady=(4, 0))
                bar_frame.pack_propagate(False)

                bar_color = self._color_for_percent(pct)
                fill_width = max(util, 0.02)  # min visible
                bar_fill = tk.Frame(bar_frame, bg=bar_color)
                bar_fill.place(relwidth=fill_width, relheight=1.0)

                # Reset time
                reset_text = f"Resets in {format_reset(w.get('reset', ''))}"
                w_status = w.get("status", "")
                if w_status and w_status != "allowed":
                    reset_text += f"  \u2022  {w_status}"

                tk.Label(body, text=reset_text,
                         font=("sans-serif", 9),
                         bg=THEME["bg"], fg=THEME["subtext"]).pack(
                    anchor=tk.W, pady=(2, 0))

            # Overage info
            overage = data.get("overage_status", "")
            if overage and overage != "allowed":
                tk.Frame(body, bg=THEME["surface2"],
                         height=1).pack(fill=tk.X, pady=8)
                reason = data.get("overage_disabled_reason", "")
                overage_text = f"Overage: {overage}"
                if reason:
                    overage_text += f" ({reason.replace('_', ' ')})"
                tk.Label(body, text=overage_text,
                         font=("sans-serif", 9),
                         bg=THEME["bg"], fg=THEME["subtext"]).pack(
                    anchor=tk.W)

        # Footer
        footer = tk.Frame(container, bg=THEME["surface"], padx=16, pady=8)
        footer.pack(fill=tk.X, side=tk.BOTTOM)

        fetched = ""
        if data and "fetched_at" in data:
            try:
                ft = datetime.fromisoformat(data["fetched_at"])
                fetched = ft.strftime("%H:%M:%S")
            except ValueError:
                pass

        tk.Label(footer, text=f"\U0001f552  {fetched}" if fetched else "",
                 font=("sans-serif", 8),
                 bg=THEME["surface"], fg=THEME["subtext"]).pack(side=tk.LEFT)

        # Quit button - kills the main monitor process
        quit_btn = tk.Label(footer, text="  Quit  ",
                            font=("sans-serif", 10, "bold"),
                            bg=THEME["red"], fg="#1e1e2e",
                            cursor="hand2", padx=8, pady=2)
        quit_btn.pack(side=tk.RIGHT)

        def quit_monitor(e=None):
            # Kill all monitor.py processes except this panel
            import os as _os
            my_pid = _os.getpid()
            try:
                out = subprocess.check_output(
                    ["pgrep", "-f", "monitor.py"], text=True)
                for line in out.strip().split("\n"):
                    pid = int(line.strip())
                    if pid != my_pid:
                        _os.kill(pid, signal.SIGTERM)
            except Exception:
                pass
            self._close()

        quit_btn.bind("<Button-1>", quit_monitor)

        # Position & size
        win.update_idletasks()
        w = win.winfo_reqwidth()
        h = win.winfo_reqheight()
        screen_w = win.winfo_screenwidth()

        # Position below the tray area, aligned near mouse X
        x = min(mx - w // 2, screen_w - w - 8)
        x = max(8, x)
        y = my + 10  # just below the click point

        win.geometry(f"{w}x{h}+{x}+{y}")

        # Close on focus loss, escape, or click inside
        def on_focus_out(e):
            if e.widget == win:
                self._close()

        def on_escape(e):
            self._close()

        win.bind("<FocusOut>", on_focus_out)
        win.bind("<Escape>", on_escape)
        win.bind("<Button-1>", lambda e: win.after(200, self._close))

        # Poll: close when mouse clicks outside the window
        def check_outside():
            try:
                if not win.winfo_exists():
                    return
                # Check if our window still has focus
                try:
                    focused = win.focus_get()
                except KeyError:
                    focused = None
                if focused is None:
                    self._close()
                    return
                win.after(200, check_outside)
            except tk.TclError:
                pass

        # Grab all input so clicks outside dismiss the window
        win.focus_force()
        win.after(100, lambda: self._grab(win))
        win.after(500, check_outside)
        win.mainloop()

    def _grab(self, win):
        """Grab pointer so clicking outside closes the panel."""
        try:
            win.grab_set_global()
        except tk.TclError:
            pass

    def _close(self):
        with self._lock:
            if self._win:
                try:
                    self._win.grab_release()
                except tk.TclError:
                    pass
                try:
                    self._win.destroy()
                except tk.TclError:
                    pass
                self._win = None


# ── Main App ─────────────────────────────────────────────────────

class UsageMonitor:
    def __init__(self):
        self.usage_data: dict | None = load_cache()
        self.icon: pystray.Icon | None = None
        self.panel = DropdownPanel()
        self._timer: threading.Timer | None = None
        self._anim_timer: threading.Timer | None = None
        self._anim_frame: int = 0
        self._lock = threading.Lock()
        self._running = True
        self._panel_proc: subprocess.Popen | None = None
        self._last_click: float = 0.0

    def refresh(self):
        token = get_token()
        if not token:
            self.usage_data = {"error": "Claude Code not logged in"}
            self._update_icon()
            return

        data = fetch_usage(token)
        with self._lock:
            self.usage_data = data
            if "error" not in data:
                save_cache(data)
        self._update_icon()

    def _update_icon(self):
        if not self.icon:
            return
        has_error = self.usage_data and "error" in self.usage_data
        pct = get_primary_percent(self.usage_data)
        self.icon.icon = make_icon(pct, error=has_error,
                                   anim_frame=self._anim_frame)
        if has_error:
            self.icon.title = "Claude Usage: Error"
        elif pct is not None:
            self.icon.title = f"Claude Code: {pct}%"
        else:
            self.icon.title = "Claude Usage Monitor"

    def _animate(self):
        """Cycle animation frames every 2 seconds."""
        if not self._running:
            return
        self._anim_frame = (self._anim_frame + 1) % 12
        self._update_icon()
        self._anim_timer = threading.Timer(2.0, self._animate)
        self._anim_timer.daemon = True
        self._anim_timer.start()

    def _schedule_refresh(self):
        if not self._running:
            return
        self._timer = threading.Timer(REFRESH_INTERVAL, self._auto_refresh)
        self._timer.daemon = True
        self._timer.start()

    def _auto_refresh(self):
        if not self._running:
            return
        self.refresh()
        self._schedule_refresh()

    def on_details(self, icon=None, item=None):
        """Toggle details panel - show if hidden, hide if visible."""
        import time
        now = time.monotonic()
        # Debounce: ignore clicks within 0.5s
        if now - self._last_click < 0.5:
            return
        self._last_click = now

        # Kill existing panel process if any
        if self._panel_proc and self._panel_proc.poll() is None:
            self._panel_proc.terminate()
            self._panel_proc = None
            return

        # Capture mouse position NOW (before subprocess delay)
        try:
            tmp = tk.Tk()
            mx, my = tmp.winfo_pointerx(), tmp.winfo_pointery()
            tmp.destroy()
        except Exception:
            mx, my = 0, 0

        data_json = json.dumps(self.usage_data or {})
        self._panel_proc = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__),
             "--panel", data_json, str(mx), str(my)],
            start_new_session=True,
        )

    def on_refresh(self, icon, item):
        threading.Thread(target=self.refresh, daemon=True).start()

    def on_quit(self, icon, item):
        self._running = False
        if self._timer:
            self._timer.cancel()
        if self._anim_timer:
            self._anim_timer.cancel()
        icon.stop()

    def _show_login_guide(self):
        """Show a GUI dialog explaining how to log in to Claude Code."""
        win = tk.Tk()
        win.title("Claude Usage Monitor - Setup Required")
        win.configure(bg=THEME["bg"])
        win.resizable(False, False)

        frame = tk.Frame(win, bg=THEME["bg"], padx=30, pady=20)
        frame.pack(fill=tk.BOTH, expand=True)

        tk.Label(frame, text="Claude Code Login Required",
                 font=("sans-serif", 14, "bold"),
                 bg=THEME["bg"], fg=THEME["red"]).pack(pady=(0, 12))

        tk.Label(frame,
                 text="Claude Code credentials were not found.\n"
                      "Please log in to Claude Code first.",
                 font=("sans-serif", 11),
                 bg=THEME["bg"], fg=THEME["text"],
                 justify=tk.LEFT).pack(anchor=tk.W, pady=(0, 16))

        # Steps
        steps_frame = tk.Frame(frame, bg=THEME["surface"], padx=16, pady=12)
        steps_frame.pack(fill=tk.X)

        tk.Label(steps_frame, text="How to set up:",
                 font=("sans-serif", 11, "bold"),
                 bg=THEME["surface"], fg=THEME["blue"]).pack(anchor=tk.W, pady=(0, 8))

        steps = [
            "1. Install Claude Code:  npm install -g @anthropic-ai/claude-code",
            "2. Run in terminal:  claude",
            "3. Follow the login prompts in your browser",
            "4. Once logged in, restart this app",
        ]
        for step in steps:
            tk.Label(steps_frame, text=step,
                     font=("monospace", 10),
                     bg=THEME["surface"], fg=THEME["text"],
                     justify=tk.LEFT).pack(anchor=tk.W, pady=2)

        tk.Label(frame,
                 text=f"Expected: {CREDENTIALS_FILE}",
                 font=("sans-serif", 9),
                 bg=THEME["bg"], fg=THEME["subtext"]).pack(pady=(12, 8))

        close_btn = tk.Label(frame, text="  Close  ",
                             font=("sans-serif", 11, "bold"),
                             bg=THEME["red"], fg="#1e1e2e",
                             cursor="hand2", padx=12, pady=4)
        close_btn.pack(pady=(4, 0))
        close_btn.bind("<Button-1>", lambda e: win.destroy())

        # Center on screen
        win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        x = (win.winfo_screenwidth() - w) // 2
        y = (win.winfo_screenheight() - h) // 2
        win.geometry(f"+{x}+{y}")

        win.mainloop()

    def run(self):
        token = get_token()
        if not token:
            self._show_login_guide()
            sys.exit(1)

        self.refresh()

        self.icon = pystray.Icon(
            name=APP_NAME,
            icon=make_icon(get_primary_percent(self.usage_data),
                           error=bool(self.usage_data and "error" in self.usage_data)),
            title="Claude Code Usage Monitor",
            menu=pystray.Menu(
                pystray.MenuItem("Show Details", self.on_details, default=True),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Refresh Now", self.on_refresh),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", self.on_quit),
            ),
        )

        self._schedule_refresh()
        self._animate()
        self.icon.run()


def main():
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    # Subprocess mode: show panel and exit
    if len(sys.argv) >= 3 and sys.argv[1] == "--panel":
        try:
            data = json.loads(sys.argv[2])
        except json.JSONDecodeError:
            data = None
        pos = None
        if len(sys.argv) >= 5:
            try:
                pos = (int(sys.argv[3]), int(sys.argv[4]))
            except ValueError:
                pass
        DropdownPanel().show(data, pos=pos)
        return

    UsageMonitor().run()


if __name__ == "__main__":
    main()
