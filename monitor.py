#!/usr/bin/env python3
"""Claude Code Usage Monitor - Ubuntu system tray indicator.

Reads the OAuth token from Claude Code's credentials file and displays
real-time usage (5h / 7d windows) in the system tray with a dropdown panel.
"""

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pystray
from PIL import Image, ImageDraw, ImageFont

APP_NAME = "claude-usage-monitor"
CONFIG_DIR = Path.home() / ".config" / APP_NAME
CACHE_FILE = CONFIG_DIR / "usage_cache.json"
CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"
# Per-user IPC trigger: the panel subprocess touches this to ask the main
# process to reload the cache. Kept under CONFIG_DIR (not world-writable /tmp).
REFRESH_TRIGGER = CONFIG_DIR / "refresh.trigger"
# Single-instance lock: holds the PID of the running tray monitor so that
# autostart + a manual launcher click don't create duplicate tray icons.
LOCK_FILE = CONFIG_DIR / "monitor.pid"

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

# ── Usage windows ────────────────────────────────────────────────

WINDOW_LABELS = {"5h": "5 Hours", "7d": "7 Days", "24h": "24 Hours", "1h": "1 Hour"}
WINDOW_ORDER = tuple(WINDOW_LABELS)  # ("5h", "7d", "24h", "1h")

_REP_ALIASES = {"five_hour": "5h", "seven_day": "7d"}


def normalize_rep(rep: str) -> str:
    """Map an API representative-claim name to a short window id."""
    return _REP_ALIASES.get(rep, rep)


# ── Usage color thresholds (single source of truth) ──────────────

_USAGE_BOUNDS = (40, 70, 90)  # cutoffs for green / yellow / orange / red
ICON_COLORS = ((0, 255, 65), (255, 255, 0), (255, 160, 0), (255, 40, 40))
PANEL_COLOR_KEYS = ("green", "yellow", "orange", "red")


def usage_level(pct: int) -> int:
    """0=ok 1=warn 2=high 3=critical, by % utilization."""
    for i, bound in enumerate(_USAGE_BOUNDS):
        if pct < bound:
            return i
    return len(_USAGE_BOUNDS)


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
    """Atomically persist the cache.

    Writes to a per-process temp file then os.replace()s it into place, so a
    crash mid-write or a concurrent writer (main process vs --panel
    subprocess) can never leave a truncated/corrupt cache. Errors are
    swallowed: a failed cache write must not propagate and kill the refresh
    timer chain.
    """
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_FILE.with_suffix(f".tmp.{os.getpid()}")
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, CACHE_FILE)  # atomic on the same filesystem
    except OSError:
        pass


# ── API ──────────────────────────────────────────────────────────

def fetch_usage(token: str) -> dict:
    url = "https://api.anthropic.com/v1/messages"
    # The credential is an OAuth access token (claudeAiOauth.accessToken), not
    # an API key — it must be sent as a Bearer token. Sending it via x-api-key
    # returns 401 (which we'd misreport as "token expired").
    headers = {
        "authorization": f"Bearer {token}",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "oauth-2025-04-20",
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
        # 429 (rate limited) and 400 still carry the unified rate-limit
        # headers we care about, so fall through and parse them.
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
        for window_id in WINDOW_ORDER:
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
    rep = normalize_rep(data.get("representative_claim", ""))
    for key in [rep, *WINDOW_ORDER]:
        if key in windows and "utilization" in windows[key]:
            try:
                return int(float(windows[key]["utilization"]) * 100)
            except (TypeError, ValueError):
                continue  # malformed header value — try the next window
    return None


def _color_for_pct(pct: int) -> tuple:
    return ICON_COLORS[usage_level(pct)]


def make_icon(percent: int | None, error: bool = False) -> Image.Image:
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
        # Force the BASIC layout engine: pystray's native deps pull in a
        # libraqm that conflicts with Pillow's default Raqm layout, making
        # truetype rendering non-deterministic / blank (the tray icon would
        # render empty). BASIC is deterministic and fine for single-line text.
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", fs,
            layout_engine=ImageFont.Layout.BASIC)
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
    except (ValueError, TypeError, OSError, OverflowError):
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


class DropdownPanel:
    """A floating dropdown panel that appears near the system tray."""

    def __init__(self):
        self._win: tk.Tk | None = None
        self._lock = threading.Lock()
        self._parent_pid: int | None = None

    def _color_for_percent(self, pct: int) -> str:
        return THEME[PANEL_COLOR_KEYS[usage_level(pct)]]

    def _update_status(self, data):
        status = (data or {}).get("status", "unknown")
        color = THEME["green"] if status == "allowed" else THEME["red"]
        self._status_label.config(text="● " + status.upper(), fg=color)

    def _update_time(self, data):
        fetched = ""
        if data and "fetched_at" in data:
            try:
                ft = datetime.fromisoformat(data["fetched_at"])
                fetched = ft.strftime("%H:%M:%S")
            except ValueError:
                pass
        self._time_label.config(text=fetched)

    def _fill_body(self, body, data):
        """Fill body frame with data widgets."""
        # Clear existing content
        for w in body.winfo_children():
            w.destroy()

        if not data or "error" in data:
            err_msg = (data or {}).get("error", "No data yet")
            tk.Label(body, text=f"! {err_msg}",
                     font=("sans-serif", 10), bg=THEME["bg"],
                     fg=THEME["orange"], wraplength=280,
                     justify=tk.LEFT).pack(anchor=tk.W)
        else:
            windows = data.get("windows", {})
            rep = normalize_rep(data.get("representative_claim", ""))

            for i, wid in enumerate(WINDOW_ORDER):
                if wid not in windows:
                    continue
                w = windows[wid]
                try:
                    util = float(w.get("utilization", 0))
                except (TypeError, ValueError):
                    util = 0.0
                pct = int(util * 100)
                is_rep = (wid == rep)

                if i > 0:
                    tk.Frame(body, bg=THEME["surface2"],
                             height=1).pack(fill=tk.X, pady=8)

                row = tk.Frame(body, bg=THEME["bg"])
                row.pack(fill=tk.X)

                tag = f"[{wid}]"
                label = WINDOW_LABELS.get(wid, wid)
                badge = "  ◀" if is_rep else ""
                tk.Label(row, text=f"{tag}  {label}{badge}",
                         font=("sans-serif", 11, "bold" if is_rep else ""),
                         bg=THEME["bg"], fg=THEME["text"]).pack(
                    side=tk.LEFT, anchor=tk.W)

                tk.Label(row, text=f"{pct}%",
                         font=("sans-serif", 14, "bold"),
                         bg=THEME["bg"],
                         fg=self._color_for_percent(pct)).pack(
                    side=tk.RIGHT, anchor=tk.E)

                bar_frame = tk.Frame(body, bg=THEME["surface"],
                                     height=10, padx=0, pady=0)
                bar_frame.pack(fill=tk.X, pady=(4, 0))
                bar_frame.pack_propagate(False)

                bar_color = self._color_for_percent(pct)
                fill_width = min(max(util, 0.02), 1.0)
                bar_fill = tk.Frame(bar_frame, bg=bar_color)
                bar_fill.place(relwidth=fill_width, relheight=1.0)

                reset_text = f"Resets in {format_reset(w.get('reset', ''))}"
                w_status = w.get("status", "")
                if w_status and w_status != "allowed":
                    reset_text += f"  •  {w_status}"

                tk.Label(body, text=reset_text,
                         font=("sans-serif", 9),
                         bg=THEME["bg"], fg=THEME["subtext"]).pack(
                    anchor=tk.W, pady=(2, 0))

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

        return body

    def show(self, data: dict | None, pos: tuple | None = None,
             parent_pid: int | None = None):
        with self._lock:
            if self._win is not None:
                try:
                    self._win.destroy()
                except tk.TclError:
                    pass
                self._win = None

        self._parent_pid = parent_pid

        win = tk.Tk()
        self._win = win
        win.title("")
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=THEME["border"])

        if pos:
            mx, my = pos
        else:
            mx, my = win.winfo_pointerx(), win.winfo_pointery()

        outer = tk.Frame(win, bg=THEME["border"], padx=1, pady=1)
        outer.pack(fill=tk.BOTH, expand=True)

        container = tk.Frame(outer, bg=THEME["bg"], padx=0, pady=0)
        container.pack(fill=tk.BOTH, expand=True)

        # Header
        header = tk.Frame(container, bg=THEME["surface"], padx=16, pady=10)
        header.pack(fill=tk.X)

        tk.Label(header, text="Claude Code Usage",
                 font=("sans-serif", 12, "bold"),
                 bg=THEME["surface"], fg=THEME["text"]).pack(side=tk.LEFT)

        self._status_label = tk.Label(header, text="", font=("sans-serif", 9),
                                      bg=THEME["surface"])
        self._status_label.pack(side=tk.RIGHT)
        self._update_status(data)

        # Body (replaceable)
        self._body = tk.Frame(container, bg=THEME["bg"], padx=16, pady=12)
        self._body.pack(fill=tk.X)
        self._fill_body(self._body, data)

        # Footer
        footer = tk.Frame(container, bg=THEME["surface"], padx=16, pady=8)
        footer.pack(fill=tk.X, side=tk.BOTTOM)

        self._time_label = tk.Label(footer, text="",
                                    font=("sans-serif", 8),
                                    bg=THEME["surface"], fg=THEME["subtext"])
        self._time_label.pack(side=tk.LEFT)
        self._update_time(data)

        # Button bar
        btn_frame = tk.Frame(footer, bg=THEME["surface"])
        btn_frame.pack(side=tk.RIGHT)

        refresh_btn = tk.Label(btn_frame, text=" Refresh ",
                               font=("sans-serif", 10, "bold"),
                               bg=THEME["blue"], fg="#1e1e2e",
                               cursor="hand2", padx=6, pady=2)
        refresh_btn.pack(side=tk.LEFT, padx=(0, 6))

        # Network fetch runs on a worker thread, but every Tk call must happen
        # on the main thread. The worker only does I/O and hands the result
        # back through this queue; a main-thread poller applies it to the UI.
        result_q: queue.Queue = queue.Queue()

        def _run_refresh():
            token = get_token()
            new_data = None
            if token:
                new_data = fetch_usage(token)
                if "error" not in new_data:
                    save_cache(new_data)
                    # Cache changed — ask the main process to reload it.
                    try:
                        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                        REFRESH_TRIGGER.touch()
                    except OSError:
                        pass
            result_q.put(new_data)  # None means "no token"

        def _done_refresh():
            if not win.winfo_exists():
                return
            refresh_btn.config(text=" Refresh ", bg=THEME["blue"])
            self._refreshing = False
            win.focus_force()

        def _update_ui(new_data):
            if not win.winfo_exists():
                return
            self._fill_body(self._body, new_data)
            self._update_status(new_data)
            self._update_time(new_data)
            _done_refresh()
            win.update_idletasks()

        def _poll_result():
            if not win.winfo_exists():
                return
            try:
                new_data = result_q.get_nowait()
            except queue.Empty:
                win.after(100, _poll_result)
                return
            if new_data is not None:
                _update_ui(new_data)
            else:
                _done_refresh()

        def do_refresh(e=None):
            if self._refreshing:
                return  # a refresh is already in flight — avoid a 2nd probe
            self._refreshing = True
            refresh_btn.config(text=" ... ", bg=THEME["surface2"])
            win.update()
            threading.Thread(target=_run_refresh, daemon=True).start()
            win.after(100, _poll_result)  # scheduled from the main thread

        refresh_btn.bind("<Button-1>", do_refresh)

        # Quit button - kills the monitor process that spawned this panel
        quit_btn = tk.Label(btn_frame, text="  Quit  ",
                            font=("sans-serif", 10, "bold"),
                            bg=THEME["red"], fg="#1e1e2e",
                            cursor="hand2", padx=6, pady=2)
        quit_btn.pack(side=tk.LEFT)

        def quit_monitor(e=None):
            # Signal only the parent monitor by PID — never a broad pgrep
            # that could match an editor or unrelated 'monitor.py' process.
            try:
                if self._parent_pid:
                    os.kill(self._parent_pid, signal.SIGTERM)
            except OSError:
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

        # Close on focus loss, Escape, or the pointer leaving the panel.
        def on_focus_out(e):
            if self._refreshing:
                return  # transient focus drop during refresh — keep panel open
            if e.widget == win:
                self._close()

        def on_escape(e):
            self._close()

        win.bind("<FocusOut>", on_focus_out)
        win.bind("<Escape>", on_escape)

        self._refreshing = False
        self._entered = False
        self._outside_count = 0

        # Auto-close once the pointer leaves the panel (works on X11 and
        # Wayland via tkinter pointer tracking, no Xlib). We arm this only
        # after the pointer has first entered the panel: it opens just below
        # the cursor (y = my + 10), so the pointer starts "outside" and an
        # unguarded check would self-dismiss the panel ~1s after it opens.
        # Clicking away (FocusOut) or Escape still closes it immediately.
        def check_outside():
            try:
                if not win.winfo_exists():
                    return
                if self._refreshing:
                    win.after(150, check_outside)
                    return
                mx = win.winfo_pointerx()
                my = win.winfo_pointery()
                wx = win.winfo_rootx()
                wy = win.winfo_rooty()
                ww = win.winfo_width()
                wh = win.winfo_height()
                inside = wx <= mx <= wx + ww and wy <= my <= wy + wh
                if inside:
                    self._entered = True
                    self._outside_count = 0
                elif self._entered:
                    # Pointer left after being inside — close after a short
                    # grace period of consecutive outside checks.
                    if self._outside_count >= 3:
                        self._close()
                        return
                    self._outside_count += 1
                win.after(150, check_outside)
            except tk.TclError:
                pass
            except Exception:
                win.after(200, check_outside)

        win.focus_force()
        win.after(500, check_outside)
        win.mainloop()

    def _close(self):
        with self._lock:
            if self._win:
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
        self._poll_timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._refresh_inflight = threading.Lock()
        self._running = True
        self._panel_proc: subprocess.Popen | None = None
        self._last_click: float = 0.0

    def refresh(self):
        # Single-flight: collapse overlapping refreshes (auto timer, menu,
        # startup) so we never fire concurrent API probes.
        if not self._refresh_inflight.acquire(blocking=False):
            return
        try:
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
        finally:
            self._refresh_inflight.release()

    def _update_icon(self):
        if not self.icon:
            return
        data = self.usage_data  # single atomic read for a consistent snapshot
        has_error = bool(data and "error" in data)
        pct = get_primary_percent(data)
        self.icon.icon = make_icon(pct, error=has_error)
        if has_error:
            self.icon.title = "Claude Usage: Error"
        elif pct is not None:
            self.icon.title = f"Claude Code: {pct}%"
        else:
            self.icon.title = "Claude Usage Monitor"

    def _poll_trigger(self):
        """Every 2s, reload the cache if the panel signalled a refresh.

        The panel subprocess already performed the network probe and wrote the
        cache, so we only reload it here — no redundant second API call.
        """
        if not self._running:
            return
        if REFRESH_TRIGGER.exists():
            try:
                REFRESH_TRIGGER.unlink(missing_ok=True)
            except OSError:
                pass
            data = load_cache()
            if data is not None:
                with self._lock:
                    self.usage_data = data
                self._update_icon()
        self._poll_timer = threading.Timer(2.0, self._poll_trigger)
        self._poll_timer.daemon = True
        self._poll_timer.start()

    def _schedule_refresh(self):
        if not self._running:
            return
        self._timer = threading.Timer(REFRESH_INTERVAL, self._auto_refresh)
        self._timer.daemon = True
        self._timer.start()

    def _auto_refresh(self):
        if not self._running:
            return
        # Guarantee the timer chain survives any exception in refresh().
        try:
            self.refresh()
        finally:
            self._schedule_refresh()

    def on_details(self, icon=None, item=None):
        """Toggle details panel - show if hidden, hide if visible."""
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

        # Spawn the panel as an isolated subprocess. It reads the pointer
        # position itself, so we never create a Tk root inside this
        # pystray/GTK process. The parent PID lets its Quit button signal us.
        data_json = json.dumps(self.usage_data or {})
        self._panel_proc = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__),
             "--panel", data_json, str(os.getpid())],
            start_new_session=True,
        )

    def on_refresh(self, icon, item):
        threading.Thread(target=self.refresh, daemon=True).start()

    def on_quit(self, icon, item):
        self._running = False
        if self._timer:
            self._timer.cancel()
        if self._poll_timer:
            self._poll_timer.cancel()
        # Tear down an open panel so it isn't orphaned.
        if self._panel_proc and self._panel_proc.poll() is None:
            self._panel_proc.terminate()
            self._panel_proc = None
        release_single_instance()
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

        # Sync the tooltip title with the data we already fetched (refresh()
        # ran before self.icon existed, so its _update_icon was a no-op).
        self._update_icon()

        self._schedule_refresh()
        self._poll_trigger()
        self.icon.run()


def acquire_single_instance() -> bool:
    """Best-effort single-instance lock.

    Returns False if another live instance already holds the lock (so the
    caller should exit), True otherwise — recording our PID. Lock errors never
    block startup. Prevents duplicate tray icons from autostart + a manual
    launcher click.
    """
    try:
        if LOCK_FILE.exists():
            try:
                old = int(LOCK_FILE.read_text().strip() or "0")
            except (ValueError, OSError):
                old = 0
            if old and old != os.getpid():
                try:
                    os.kill(old, 0)   # raises OSError if no such process
                    return False      # another instance is alive
                except OSError:
                    pass              # stale lock — take it over
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        LOCK_FILE.write_text(str(os.getpid()))
    except OSError:
        pass
    return True


def release_single_instance():
    try:
        if LOCK_FILE.exists() and LOCK_FILE.read_text().strip() == str(os.getpid()):
            LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def main():
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    # Subprocess mode: show panel and exit
    if len(sys.argv) >= 3 and sys.argv[1] == "--panel":
        try:
            data = json.loads(sys.argv[2])
        except json.JSONDecodeError:
            data = None
        parent_pid = None
        if len(sys.argv) >= 4:
            try:
                parent_pid = int(sys.argv[3])
            except ValueError:
                pass
        DropdownPanel().show(data, parent_pid=parent_pid)
        return

    if not acquire_single_instance():
        print("claude-usage-monitor is already running.", file=sys.stderr)
        return

    UsageMonitor().run()


if __name__ == "__main__":
    main()
