#!/usr/bin/env python3
"""Claude Code Usage Monitor - Ubuntu system tray indicator.

Reads the OAuth token from Claude Code's credentials file and displays
real-time usage (5h / 7d windows) in the system tray with a dropdown panel.
Codex, Grok and Antigravity usage are read from the files their CLIs write.
Each provider gets its own pixel-art tray icon; clicking any of them opens one
panel that bundles every provider.
"""

import base64
import io
import json
import math
import os
import queue
import selectors
import shutil
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
from PIL import Image

APP_NAME = "claude-usage-monitor"
CONFIG_DIR = Path.home() / ".config" / APP_NAME
CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"
CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
GROK_LOG_FILE = Path.home() / ".grok" / "logs" / "unified.jsonl"
AGY_DIR = Path.home() / ".gemini" / "antigravity-cli"
# Written by the agy statusLine command (hud/agy-hud.py): a verbatim copy of
# the JSON agy pipes to it, which includes the account's quota buckets.
AGY_STATUS_FILE = AGY_DIR / "hud" / "cache" / "last_stdin.json"

PROBE_MODEL = "claude-haiku-4-5-20251001"

# ── Providers ────────────────────────────────────────────────────
#
# Each provider runs as its own process with its own tray icon, cache, lock
# and refresh trigger, so one provider failing can never take down another.
# Only the panel is shared: it reads every provider's cache itself.
#
# "claude" probes the API live. "codex" asks its local app-server for the
# account's live rate limits, falling back to its session logs. "grok" and
# "agy" have no queryable usage endpoint, so they read the numbers their own
# CLI already wrote to disk: that costs nothing and needs no credentials, but
# the value only advances when you actually run that CLI — hence `local:
# True`, which makes the panel label the reading with its age instead of
# implying it is live. `watch` names the file that CLI rewrites; the tray
# re-reads as soon as it changes rather than waiting out `interval`.
PROVIDERS = {
    "claude": {
        "label": "Claude Code",
        "interval": 300,
        "local": False,
    },
    "codex": {
        "label": "Codex",
        "interval": 60,
        "local": True,
        "hint": "No usage recorded yet - run `codex` once.",
    },
    "grok": {
        "label": "Grok",
        "interval": 60,
        "local": True,
        "hint": "No usage recorded yet - run `grok` once.",
        "watch": GROK_LOG_FILE,
    },
    "agy": {
        "label": "Antigravity",
        "interval": 60,
        "local": True,
        "hint": "No usage recorded yet - run `agy` once (needs the "
                "hud/agy-hud.py statusLine).",
        "watch": AGY_STATUS_FILE,
    },
}
DEFAULT_PROVIDER = "claude"



# Each provider gets distinct files so concurrent processes never collide.
def cache_path(provider: str) -> Path:
    return CONFIG_DIR / f"usage_cache_{provider}.json"


def status_path(provider: str) -> Path:
    """The tray's latest refresh outcome. The cache keeps only the last good
    reading; this file says whether the newest attempt failed, so the shared
    panel can show the same error the tray icon shows."""
    return CONFIG_DIR / f"status_{provider}.json"


def trigger_path(provider: str) -> Path:
    """Per-user IPC trigger: the panel subprocess touches this to ask that
    provider's tray process to reload its cache. Kept under CONFIG_DIR (not
    world-writable /tmp)."""
    return CONFIG_DIR / f"refresh_{provider}.trigger"


def lock_path(provider: str) -> Path:
    """Single-instance lock: holds the PID of the running tray monitor so that
    autostart + a manual launcher click don't create duplicate tray icons."""
    return CONFIG_DIR / f"monitor_{provider}.pid"


# Holds the PID of the open combined panel, whichever tray icon opened it.
PANEL_LOCK = CONFIG_DIR / "panel.pid"

# Set by select_provider() before anything touches the paths below.
PROVIDER = DEFAULT_PROVIDER
CACHE_FILE = cache_path(PROVIDER)
REFRESH_TRIGGER = trigger_path(PROVIDER)
LOCK_FILE = lock_path(PROVIDER)
REFRESH_INTERVAL = PROVIDERS[PROVIDER]["interval"]


def select_provider(provider: str):
    """Point every per-provider path at `provider`. Call once, at startup."""
    global PROVIDER, CACHE_FILE, REFRESH_TRIGGER, LOCK_FILE, REFRESH_INTERVAL
    PROVIDER = provider
    CACHE_FILE = cache_path(provider)
    REFRESH_TRIGGER = trigger_path(provider)
    LOCK_FILE = lock_path(provider)
    REFRESH_INTERVAL = PROVIDERS[provider]["interval"]

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

WINDOW_LABELS = {"5h": "5 Hours", "7d": "7 Days", "24h": "24 Hours",
                 "1h": "1 Hour", "30d": "30 Days",
                 # agy meters third-party (non-Gemini) models separately
                 "3p-5h": "5 Hours (3P)", "3p-7d": "7 Days (3P)"}
WINDOW_ORDER = tuple(WINDOW_LABELS)  # ("5h", "7d", ..., "3p-5h", "3p-7d")

# Codex reports its window as a raw minute count; Grok as a period enum.
# Both map onto the same window ids the Claude headers use, as do agy's
# named quota buckets.
_MINUTES_TO_WINDOW = {60: "1h", 300: "5h", 1440: "24h", 10080: "7d", 43200: "30d"}
_GROK_PERIOD_TO_WINDOW = {
    "USAGE_PERIOD_TYPE_WEEKLY": "7d",
    "USAGE_PERIOD_TYPE_MONTHLY": "30d",
    "USAGE_PERIOD_TYPE_DAILY": "24h",
}

_AGY_BUCKET_TO_WINDOW = {
    "gemini-5h": "5h",
    "gemini-weekly": "7d",
    "3p-5h": "3p-5h",
    "3p-weekly": "3p-7d",
}

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

def load_cache(provider: str | None = None) -> dict | None:
    """Read a provider's cache (default: this process's provider)."""
    path = cache_path(provider) if provider else CACHE_FILE
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None
    return None


def save_cache(data: dict, provider: str | None = None):
    """Atomically persist the cache.

    Writes to a per-process temp file then os.replace()s it into place, so a
    crash mid-write or a concurrent writer (main process vs --panel
    subprocess) can never leave a truncated/corrupt cache. Errors are
    swallowed: a failed cache write must not propagate and kill the refresh
    timer chain. `provider` defaults to this process's provider; the panel
    passes it explicitly because it refreshes every provider.
    """
    path = cache_path(provider) if provider else CACHE_FILE
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".tmp.{os.getpid()}")
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)  # atomic on the same filesystem
    except OSError:
        pass


def save_status(provider: str, error: str | None):
    """Record the outcome of the newest refresh: an error message, or None
    once a refresh succeeded. Same atomic write as save_cache()."""
    path = status_path(provider)
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".tmp.{os.getpid()}")
        with open(tmp, "w") as f:
            json.dump({"error": error,
                       "at": datetime.now(timezone.utc).isoformat()}, f)
        os.replace(tmp, path)
    except OSError:
        pass


def load_status_error(provider: str) -> str | None:
    """The error from the provider's newest refresh, or None if it succeeded
    (or no status was ever written)."""
    try:
        with open(status_path(provider)) as f:
            err = json.load(f).get("error")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
    return err if isinstance(err, str) and err else None


def load_panel_data(provider: str) -> dict | None:
    """What the panel shows for a provider: the last good cache, with the
    newest refresh error (if any) laid over it under "error"."""
    data = load_cache(provider)
    err = load_status_error(provider)
    if err is None:
        return data
    merged = dict(data or {})
    merged["error"] = err
    return merged


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


# ── Local providers (Codex / Grok / agy) ─────────────────────────
#
# Grok and agy offer no headless "show my usage" command, but both already
# write the figure their own UI displays into a file under $HOME. We read
# that file. No network, no credentials, no quota spent — at the cost of the
# reading being only as fresh as the last time you ran that CLI. Codex's
# session logs have the same limit, so they are only its fallback: usage
# from anywhere but this machine's CLI (web, IDE, another host) never
# reaches them.

def _derived_status(pct: float) -> str:
    """Local sources carry no server status field, so derive one from usage."""
    return "allowed" if pct < 90 else "allowed_warning"


def _tail_lines(path: Path, max_bytes: int = 512 * 1024) -> list[str]:
    """Read the last `max_bytes` of a file as lines, newest last.

    These logs grow without bound, so never read the whole thing. A partial
    first line (from cutting mid-record) is dropped by the caller's JSON
    parse, which is why the slice boundary needs no special handling.
    """
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            start = max(0, f.tell() - max_bytes)
            f.seek(start)
            blob = f.read()
    except OSError:
        return []
    return blob.decode("utf-8", errors="replace").splitlines()


CODEX_LIVE_TIMEOUT = 15   # seconds for the whole app-server exchange
CODEX_LIVE_MAX_BYTES = 1 << 20   # a reply this size is not a rate-limit read


def _codex_binary() -> str | None:
    """The codex CLI: on PATH, else the newest nvm install (autostart PATHs
    don't always include nvm's bin dir)."""
    found = shutil.which("codex")
    if found:
        return found
    candidates = []
    for path in Path.home().glob(".nvm/versions/node/*/bin/codex"):
        try:
            candidates.append((path.stat().st_mtime, path))
        except OSError:
            continue   # dangling symlink, or removed mid-scan
    return str(max(candidates)[1]) if candidates else None


def fetch_codex_usage() -> dict:
    """Codex's live rate limits, else the last ones its session logs hold."""
    data = _fetch_codex_live()
    if data is not None:
        return data
    return _fetch_codex_logged()


def _fetch_codex_live() -> dict | None:
    """Ask `codex app-server` for the account's current rate limits.

    `account/rateLimits/read` is a read: it reports usage without spending
    any. Any failure (no CLI, not logged in, protocol change, timeout)
    returns None so the caller falls back to the session logs.
    """
    binary = _codex_binary()
    if binary is None:
        return None
    env = dict(os.environ)
    # codex is a node script; let `#!/usr/bin/env node` find the node that
    # sits next to it even when that bin dir is not on our PATH.
    env["PATH"] = os.path.dirname(binary) + os.pathsep + env.get("PATH", "")
    try:
        proc = subprocess.Popen(
            [binary, "app-server"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
    except OSError:
        return None
    deadline = time.monotonic() + CODEX_LIVE_TIMEOUT
    reader = _JsonRpcReader(proc)

    def send(*msgs):
        proc.stdin.write(b"".join(json.dumps(m).encode() + b"\n"
                                  for m in msgs))
        proc.stdin.flush()

    try:
        # The protocol wants the initialize reply before anything else.
        send({"id": 1, "method": "initialize",
              "params": {"clientInfo": {"name": APP_NAME, "version": "1"}}})
        result = None
        if reader.result(1, deadline) is not None:
            send({"method": "initialized"},
                 {"id": 2, "method": "account/rateLimits/read"})
            result = reader.result(2, deadline)
    except (OSError, ValueError):
        result = None
    finally:
        proc.kill()
        proc.wait()
    if not isinstance(result, dict):
        return None
    limits = result.get("rateLimits")
    if not isinstance(limits, dict):
        return None
    data = _parse_codex_limits(
        limits, used_key="usedPercent", minutes_key="windowDurationMins",
        resets_key="resetsAt", plan_key="planType",
        fetched_at=datetime.now(timezone.utc).isoformat())
    return None if "error" in data else data


class _JsonRpcReader:
    """Newline-delimited JSON-RPC replies from a child's stdout.

    Notifications arrive interleaved with responses, so skip until the id
    matches. A selector bounds every wait and the buffer is capped: a wedged
    or runaway server must never hang or bloat the tray's refresh thread.
    """

    def __init__(self, proc: subprocess.Popen):
        self._fd = proc.stdout.fileno()
        self._buf = bytearray()
        self._read = 0

    def result(self, want_id: int, deadline: float):
        """The `result` of response `want_id`, or None (error reply,
        timeout, server exit or oversized output)."""
        with selectors.DefaultSelector() as sel:
            sel.register(self._fd, selectors.EVENT_READ)
            while True:
                while (nl := self._buf.find(b"\n")) >= 0:
                    line = bytes(self._buf[:nl])
                    del self._buf[:nl + 1]
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(msg, dict) and msg.get("id") == want_id:
                        return msg.get("result")
                left = deadline - time.monotonic()
                if left <= 0 or not sel.select(left):
                    return None
                chunk = os.read(self._fd, 65536)
                if not chunk:
                    return None   # server exited
                self._read += len(chunk)
                if self._read > CODEX_LIVE_MAX_BYTES:
                    return None
                self._buf += chunk


def _fetch_codex_logged() -> dict:
    """Latest rate limits Codex recorded in its session rollout logs.

    Codex appends a `token_count` event carrying `rate_limits` after each
    turn. The newest rollout file holds the newest figure, so walk files by
    mtime and take the last matching record from the first file that has one.
    """
    try:
        files = sorted(CODEX_SESSIONS_DIR.glob("**/rollout-*.jsonl"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return {"error": "Cannot read ~/.codex/sessions"}
    if not files:
        return {"error": PROVIDERS["codex"]["hint"]}

    for path in files[:10]:   # bound the scan; older files cannot be newer
        record = None
        for line in _tail_lines(path):
            if '"rate_limits"' not in line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue      # truncated first line, or a non-JSON line
            payload = obj.get("payload") or {}
            if payload.get("rate_limits"):
                record = obj
        if record:
            return _parse_codex_record(record)

    return {"error": PROVIDERS["codex"]["hint"]}


def _parse_codex_record(record: dict) -> dict:
    return _parse_codex_limits(
        record["payload"]["rate_limits"], used_key="used_percent",
        minutes_key="window_minutes", resets_key="resets_at",
        plan_key="plan_type",
        fetched_at=_iso_or_now(record.get("timestamp")))


def _parse_codex_limits(limits: dict, *, used_key: str, minutes_key: str,
                        resets_key: str, plan_key: str,
                        fetched_at: str) -> dict:
    """Codex rate limits -> our data shape. The session logs spell the keys
    in snake_case and the app-server in camelCase; the shape is the same."""
    data = {
        "fetched_at": fetched_at,
        "representative_claim": "",
        "plan": limits.get(plan_key) or "",
        "windows": {},
    }

    worst = 0.0
    # `primary`/`secondary` are positional, not semantic: which real window
    # each holds varies by plan, so key them by their own window_minutes.
    for slot in ("primary", "secondary"):
        window = limits.get(slot)
        if not isinstance(window, dict):
            continue
        try:
            pct = float(window[used_key])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(pct):
            continue
        minutes = window.get(minutes_key)
        wid = _MINUTES_TO_WINDOW.get(minutes)
        if wid is None:
            continue   # an unmapped window has no label or ordering slot
        data["windows"][wid] = {
            "utilization": str(pct / 100.0),
            "reset": str(window.get(resets_key, "")),
            "status": _derived_status(pct),
        }
        worst = max(worst, pct)
        if not data["representative_claim"]:
            data["representative_claim"] = wid

    if not data["windows"]:
        return {"error": "Codex reported no usable rate limit window"}
    data["status"] = _derived_status(worst)
    return data


def fetch_grok_usage() -> dict:
    """Latest weekly usage Grok recorded in its unified log.

    Grok logs `billing: fetched credits config` whenever it refreshes billing
    (session start, and on /usage). The record carries the same
    `creditUsagePercent` its /usage screen shows.
    """
    if not GROK_LOG_FILE.exists():
        return {"error": PROVIDERS["grok"]["hint"]}

    record = None
    for line in _tail_lines(GROK_LOG_FILE):
        if "fetched credits config" not in line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ((obj.get("ctx") or {}).get("config")):
            record = obj
    if record is None:
        return {"error": PROVIDERS["grok"]["hint"]}

    ctx = record["ctx"]
    config = ctx["config"]
    try:
        pct = float(config["creditUsagePercent"])
    except (KeyError, TypeError, ValueError):
        return {"error": "Grok reported no usage percentage"}

    period = config.get("currentPeriod") or {}
    wid = _GROK_PERIOD_TO_WINDOW.get(period.get("type"), "7d")

    data = {
        "fetched_at": _iso_or_now(record.get("ts")),
        "status": _derived_status(pct),
        "representative_claim": wid,
        "plan": ctx.get("subscriptionTier") or "",
        "windows": {
            wid: {
                "utilization": str(pct / 100.0),
                "reset": period.get("end", ""),
                "status": _derived_status(pct),
            }
        },
    }

    # Pay-as-you-go top-up, shown only once it is actually in play.
    used, cap = _grok_val(config, "onDemandUsed"), _grok_val(config, "onDemandCap")
    if cap or used:
        data["extra"] = f"On-demand: {used:g} / {cap:g}"
    return data


def _grok_val(config: dict, key: str) -> float:
    """Grok wraps numbers as {"val": n}; absent keys mean zero."""
    raw = config.get(key)
    if isinstance(raw, dict):
        raw = raw.get("val")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def fetch_agy_usage() -> dict:
    """Latest quota agy handed to its statusLine command.

    agy pipes a JSON status blob to the statusLine command on every render;
    hud/agy-hud.py saves it verbatim. Its `quota` object holds one bucket per
    meter (Gemini vs third-party models, 5h vs weekly) as `remaining_fraction`.
    The file has no timestamp of its own, so its mtime is the reading's age.
    """
    try:
        mtime = AGY_STATUS_FILE.stat().st_mtime
        with open(AGY_STATUS_FILE) as f:
            status = json.load(f)
    except FileNotFoundError:
        return {"error": PROVIDERS["agy"]["hint"]}
    except (OSError, json.JSONDecodeError):
        return {"error": f"Cannot read {AGY_STATUS_FILE}"}

    quota = status.get("quota") if isinstance(status, dict) else None
    if not isinstance(quota, dict) or not quota:
        return {"error": "agy reported no quota"}

    data = {
        "fetched_at": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
        "representative_claim": "",
        "plan": "",
        "windows": {},
    }

    worst = -1.0
    for bucket, wid in _AGY_BUCKET_TO_WINDOW.items():
        b = quota.get(bucket)
        if not isinstance(b, dict):
            continue
        try:
            pct = (1.0 - float(b["remaining_fraction"])) * 100
        except (KeyError, TypeError, ValueError):
            continue
        data["windows"][wid] = {
            "utilization": str(pct / 100.0),
            "reset": b.get("reset_time", ""),
            "status": _derived_status(pct),
        }
        # Four meters but one tray number: show whichever is closest to
        # running out, since that is the one that will stop you first.
        if pct > worst:
            worst = pct
            data["representative_claim"] = wid

    if not data["windows"]:
        return {"error": "agy reported no usable quota bucket"}
    data["status"] = _derived_status(worst)
    return data


def _iso_or_now(value) -> str:
    """Normalize a logged timestamp to ISO-8601, falling back to now."""
    if isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc).isoformat()
        except ValueError:
            pass
    return datetime.now(timezone.utc).isoformat()


def fetch_for_provider(provider: str) -> dict | None:
    """Fetch usage for `provider`. None means "cannot even try" (no login)."""
    if provider == "codex":
        return fetch_codex_usage()
    if provider == "grok":
        return fetch_grok_usage()
    if provider == "agy":
        return fetch_agy_usage()
    token = get_token()
    if not token:
        return None
    return fetch_usage(token)


# ── Icon ─────────────────────────────────────────────────────────
#
# Each tray image is a 16x16 brand-coloured pixel tile holding the provider's
# mark, followed by the usage number in a 5x9 pixel font. Everything is drawn
# 1:1 with no antialiasing and no scaling.
#
# The canvas is deliberately non-square: GNOME's appindicator extension
# rescales square icons to 16x16 (blurring them) but shows wide ones at their
# native size, so a 36x16 image lands on the top bar pixel-exact.
#
# Sprite data is plain string grids: '.' transparent, '#' glyph ink.

TILE = 16
TILE_GAP = 1          # transparent column between the tile and the number
NUMBER_W = 19         # 3 digits (5) + 2 gaps (1) + 1 px outline each side
ICON_W = TILE + TILE_GAP + NUMBER_W   # 36
ICON_H = 16

ICON_ERROR_COLOR = (255, 40, 40)
ICON_NODATA_COLOR = (0, 200, 255)
ICON_OUTLINE = (0, 0, 0)

# Rounded tile: 'x' = tile, '.' = transparent corner.
TILE_MASK = [
    "..xxxxxxxxxxxx..",
    ".xxxxxxxxxxxxxx.",
] + ["xxxxxxxxxxxxxxxx"] * 12 + [
    ".xxxxxxxxxxxxxx.",
    "..xxxxxxxxxxxx..",
]

# Tile fill (top, bottom: a vertical gradient), 1 px rim, glyph ink. The rim
# keeps the white and the black tile visible on a bar of the same colour.
BRANDS = {
    "claude": {"top": (217, 119, 87), "bot": (217, 119, 87),
               "rim": None, "ink": (250, 240, 230)},
    "codex": {"top": (245, 245, 245), "bot": (245, 245, 245),
              "rim": (150, 150, 150), "ink": (10, 10, 10)},
    "grok": {"top": (0, 0, 0), "bot": (0, 0, 0),
             "rim": (120, 120, 120), "ink": (255, 255, 255)},
    "agy": {"top": (66, 133, 244), "bot": (155, 114, 203),
            "rim": None, "ink": (255, 255, 255)},
}

GLYPHS = {
    # Claude: cream starburst with uneven rays, like the real mark.
    "claude": [
        ".....#......",
        "..#..#...#..",
        "...#.#..#...",
        "....###.#...",
        ".##.####..##",
        "...######...",
        "#########...",
        "...######.##",
        ".##.####....",
        "...#.##.#...",
        "..#..#...#..",
        "......#.....",
    ],
    # OpenAI / Codex: 6-blade aperture around a hexagonal hole. 13x13 so the
    # whole grid fits inside the rim; cropped to 12x12 it came out lopsided.
    "codex": [
        ".....####....",
        "..##.#####...",
        ".###.###..##.",
        ".###.##..###.",
        "####....#####",
        "..##......###",
        "#...........#",
        "###......##..",
        "#####....####",
        ".###..##.###.",
        ".##..###.###.",
        "...#####.##..",
        "....####.....",
    ],
    # Grok / xAI: slashed X - a heavy '\' plus a '/' broken at the centre.
    "grok": [
        "##.........#",
        "###.......#.",
        ".###.....#..",
        "..###...#...",
        "...###......",
        "....###.....",
        ".....###....",
        "..##..###...",
        ".##....###..",
        "##......###.",
        "#........###",
        "..........##",
    ],
    # Gemini / Antigravity: four-point star.
    "agy": [
        ".....##.....",
        ".....##.....",
        "....####....",
        "....####....",
        "...######...",
        "############",
        "############",
        "...######...",
        "....####....",
        "....####....",
        ".....##.....",
        ".....##.....",
    ],
}

# Bold 5x9 digits ("!" and "-" are narrower) for the usage number.
PIXEL_FONT = {
    "0": [".###.", "##.##", "##.##", "##.##", "##.##",
          "##.##", "##.##", "##.##", ".###."],
    "1": ["..##.", ".###.", "####.", "..##.", "..##.",
          "..##.", "..##.", "..##.", "#####"],
    "2": [".###.", "##.##", "...##", "...##", "..##.",
          ".##..", "##...", "##...", "#####"],
    "3": ["####.", "...##", "...##", "...##", ".###.",
          "...##", "...##", "...##", "####."],
    "4": ["...##", "..###", ".####", "##.##", "##.##",
          "#####", "...##", "...##", "...##"],
    "5": ["#####", "##...", "##...", "####.", "...##",
          "...##", "...##", "##.##", ".###."],
    "6": [".###.", "##...", "##...", "####.", "##.##",
          "##.##", "##.##", "##.##", ".###."],
    "7": ["#####", "...##", "...##", "..##.", "..##.",
          ".##..", ".##..", ".##..", ".##.."],
    "8": [".###.", "##.##", "##.##", "##.##", ".###.",
          "##.##", "##.##", "##.##", ".###."],
    "9": [".###.", "##.##", "##.##", "##.##", ".####",
          "...##", "...##", "...##", ".###."],
    "!": ["##", "##", "##", "##", "##", "##", "..", "##", "##"],
    "-": ["....", "....", "....", "....", "####",
          "####", "....", "....", "...."],
}
PIXEL_FONT_H = 9
PIXEL_CHAR_GAP = 1


def parse_utilization(raw) -> float | None:
    """A window's utilization (0..1) as a finite float, else None.

    float() happily accepts "nan" and "inf", and int(inf) raises
    OverflowError — which, on a timer thread, would end that timer's chain.
    """
    try:
        util = float(raw)
    except (TypeError, ValueError):
        return None
    return util if math.isfinite(util) else None


def get_primary_percent(data: dict | None) -> int | None:
    if not data or "error" in data:
        return None
    windows = data.get("windows", {})
    rep = normalize_rep(data.get("representative_claim", ""))
    for key in [rep, *WINDOW_ORDER]:
        if key in windows and "utilization" in windows[key]:
            util = parse_utilization(windows[key]["utilization"])
            if util is not None:
                return int(util * 100)
            # malformed header value — try the next window
    return None


def brand_tile(provider: str) -> Image.Image:
    """The provider's 16x16 brand tile (also reused, enlarged, by the panel)."""
    b = BRANDS[provider]
    img = Image.new("RGBA", (TILE, TILE), (0, 0, 0, 0))
    px = img.load()
    for y, row in enumerate(TILE_MASK):
        t = y / (TILE - 1)
        col = tuple(round(b["top"][i] + (b["bot"][i] - b["top"][i]) * t)
                    for i in range(3))
        for x, c in enumerate(row):
            if c == "x":
                px[x, y] = (*col, 255)
    if b["rim"]:
        # A tile pixel is rim if any 4-neighbour is off the tile.
        for y, row in enumerate(TILE_MASK):
            for x, c in enumerate(row):
                if c != "x":
                    continue
                edge = any(
                    not (0 <= x + dx < TILE and 0 <= y + dy < TILE)
                    or TILE_MASK[y + dy][x + dx] != "x"
                    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))
                if edge:
                    px[x, y] = (*b["rim"], 255)
    glyph = GLYPHS[provider]
    off = (TILE - len(glyph)) // 2   # 12 -> 2, 13 -> 1 (still inside the rim)
    for y, row in enumerate(glyph):
        for x, c in enumerate(row):
            if c == "#":
                px[off + x, off + y] = (*b["ink"], 255)
    return img


def _draw_pixel_text(img: Image.Image, text: str, color: tuple,
                     x0: int, y0: int):
    """Draw `text` with a 1 px black 8-neighbour outline, then the fill."""
    px = img.load()
    ink = []
    x = x0
    for ch in text:
        glyph = PIXEL_FONT[ch]
        for gy, row in enumerate(glyph):
            for gx, c in enumerate(row):
                if c == "#":
                    ink.append((x + gx, y0 + gy))
        x += len(glyph[0]) + PIXEL_CHAR_GAP
    w, h = img.size
    for ix, iy in ink:
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                ox, oy = ix + dx, iy + dy
                if 0 <= ox < w and 0 <= oy < h:
                    px[ox, oy] = (*ICON_OUTLINE, 255)
    for ix, iy in ink:
        px[ix, iy] = (*color, 255)


def make_icon(provider: str, percent: int | None,
              error: bool = False) -> Image.Image:
    """36x16 tray image: brand tile + usage number. Hand to pystray unscaled."""
    img = Image.new("RGBA", (ICON_W, ICON_H), (0, 0, 0, 0))
    img.paste(brand_tile(provider), (0, 0))
    if error:
        text, color = "!", ICON_ERROR_COLOR
    elif percent is None:
        text, color = "--", ICON_NODATA_COLOR
    else:
        pct = max(0, min(100, int(percent)))
        text, color = str(pct), ICON_COLORS[usage_level(pct)]
    # Left-aligned against the tile so each tile + number reads as one unit.
    _draw_pixel_text(img, text, color, TILE + TILE_GAP + 1,
                     (ICON_H - PIXEL_FONT_H) // 2)
    return img


# ── Process discovery ────────────────────────────────────────────
#
# The combined panel acts on every provider's monitor (Quit stops them all),
# so a PID read from a lock file is only trusted once /proc confirms it is
# still *this* program in the expected role. PIDs get reused; a stale lock
# must never make us signal an unrelated process.

def _monitor_args(pid: int) -> list[str] | None:
    """argv after `monitor.py` for a live monitor.py process, else None."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    args = raw.decode("utf-8", errors="replace").split("\0")
    if args and args[-1] == "":
        args.pop()   # cmdline is NUL-terminated
    for i, arg in enumerate(args[:3]):   # [python, monitor.py, ...]
        if os.path.basename(arg) != "monitor.py":
            continue
        # Same script, not merely some file called monitor.py: resolve a
        # relative argv entry against that process's own cwd.
        try:
            if not os.path.isabs(arg):
                arg = os.path.join(os.readlink(f"/proc/{pid}/cwd"), arg)
        except OSError:
            return None
        if os.path.realpath(arg) != os.path.realpath(__file__):
            return None
        return args[i + 1:]
    return None


def _read_pid(path: Path) -> int | None:
    try:
        pid = int(path.read_text().strip() or "0")
    except (ValueError, OSError):
        return None
    return pid if pid > 0 and pid != os.getpid() else None


def running_monitor_pid(provider: str) -> int | None:
    """PID of the live tray monitor for `provider`, verified via /proc."""
    pid = _read_pid(lock_path(provider))
    if pid is None:
        return None
    args = _monitor_args(pid)
    if args is None:
        return None
    if (args[:2] == ["--provider", provider]
            or args[:1] == [f"--provider={provider}"]
            or (not args and provider == DEFAULT_PROVIDER)):
        return pid
    return None


def running_panel_pid() -> int | None:
    """PID of the open combined panel (from any monitor), verified via /proc."""
    pid = _read_pid(PANEL_LOCK)
    if pid is None:
        return None
    args = _monitor_args(pid)
    if args and args[0] == "--panel":
        return pid
    return None


def panel_providers() -> list[str]:
    """Providers the panel shows: those with a cache or a running monitor.

    A provider that has never produced a reading and has no tray process is
    simply not installed here, so it gets no section at all.
    """
    return [p for p in PROVIDERS
            if cache_path(p).exists() or running_monitor_pid(p) is not None]


# ── Dropdown Panel ───────────────────────────────────────────────

def age_seconds(when: datetime) -> int:
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - when).total_seconds()))


def format_age(total_sec: int) -> str:
    days, rem = divmod(total_sec, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    if minutes > 0:
        return f"{minutes}m"
    return "<1m"


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

    total_sec = int((reset_dt - datetime.now(timezone.utc)).total_seconds())
    if total_sec < 0:
        # The window already rolled over after this (stale) reading.
        return "passed"
    return format_age(total_sec)


def reset_passed(reset_val: str) -> bool:
    return format_reset(reset_val) == "passed"


# Panel layout. Fixed column widths keep the rows of every section aligned
# with each other, so the bundle reads as one table.
PANEL_MARK_SCALE = 2          # 16x16 tile -> 32x32, nearest-neighbour
PANEL_INDENT = TILE * PANEL_MARK_SCALE + 10   # rows start under the label
PANEL_BAR_W = 110
PANEL_COL_MIN = (104, 40, PANEL_BAR_W + 8, 92)   # label, %, bar, reset


class DropdownPanel:
    """One floating panel near the tray, bundling every provider's usage.

    Whichever tray icon is clicked, the panel reads every provider's cache
    itself and shows one section per provider, in PROVIDERS order.
    """

    def __init__(self):
        self._win: tk.Tk | None = None
        self._lock = threading.Lock()
        self._providers: list[str] = []
        self._data: dict[str, dict | None] = {}
        self._marks: dict[str, tk.PhotoImage] = {}

    def _color_for_percent(self, pct: int) -> str:
        return THEME[PANEL_COLOR_KEYS[usage_level(pct)]]

    def _mark_image(self, provider: str) -> tk.PhotoImage:
        """The tray sprite's brand tile, enlarged for the panel.

        Passed to Tk as PNG data so this needs no PIL.ImageTk (a separate
        distro package); Tk 8.6 decodes PNG, alpha included.
        """
        if provider not in self._marks:
            size = TILE * PANEL_MARK_SCALE
            buf = io.BytesIO()
            brand_tile(provider).resize((size, size), Image.NEAREST).save(
                buf, "PNG")
            self._marks[provider] = tk.PhotoImage(
                master=self._win, data=base64.b64encode(buf.getvalue()))
        return self._marks[provider]

    @staticmethod
    def _status_text(data):
        if data and "error" in data:
            return "● ERROR", THEME["red"]
        status = (data or {}).get("status", "unknown") or "unknown"
        color = THEME["green"] if status == "allowed" else THEME["red"]
        return "● " + status.upper(), color

    @staticmethod
    def _time_text(provider, data):
        fetched, age = "", None
        if data and "fetched_at" in data:
            try:
                ft = datetime.fromisoformat(data["fetched_at"])
            except ValueError:
                ft = None
            if ft is not None:
                fetched = ft.astimezone().strftime("%H:%M:%S")
                age = age_seconds(ft)

        is_local = PROVIDERS[provider]["local"]
        color = THEME["subtext"]
        # For a local provider the number only moves when that CLI runs, so an
        # unqualified clock time would read as "live". Always state the age,
        # and flag it once the reading is old enough to be misleading.
        # With a refresh error the reading is the last good one, however
        # recent its clock time looks, so its age is always shown then.
        has_error = bool(data and "error" in data)
        if age is not None and (is_local or has_error or age > 600):
            fetched += f"  ({format_age(age)} ago)"
            if has_error or (is_local and age > 3600):
                color = THEME["orange"]
            if has_error:
                fetched = "last good " + fetched
        return fetched, color

    def _fill_body(self):
        """(Re)build every provider section from self._data."""
        body = self._body
        for w in body.winfo_children():
            w.destroy()
        for i, provider in enumerate(self._providers):
            if i:
                tk.Frame(body, bg=THEME["surface2"],
                         height=1).pack(fill=tk.X, pady=8)
            self._fill_section(body, provider, self._data.get(provider))

    def _fill_section(self, body, provider, data):
        bg = THEME["bg"]
        spec = PROVIDERS[provider]
        section = tk.Frame(body, bg=bg)
        section.pack(fill=tk.X)

        # Section header: brand mark | label + plan / age | status
        head = tk.Frame(section, bg=bg)
        head.pack(fill=tk.X)
        tk.Label(head, image=self._mark_image(provider), bg=bg,
                 bd=0).pack(side=tk.LEFT, padx=(0, 10))

        status_text, status_color = self._status_text(data)
        tk.Label(head, text=status_text, font=("sans-serif", 8),
                 bg=bg, fg=status_color).pack(side=tk.RIGHT, anchor=tk.N)

        titles = tk.Frame(head, bg=bg)
        titles.pack(side=tk.LEFT, fill=tk.X, expand=True)
        name_row = tk.Frame(titles, bg=bg)
        name_row.pack(anchor=tk.W)
        tk.Label(name_row, text=spec["label"],
                 font=("sans-serif", 11, "bold"),
                 bg=bg, fg=THEME["text"]).pack(side=tk.LEFT)
        plan = (data or {}).get("plan", "")
        if plan:
            tk.Label(name_row, text=plan, font=("sans-serif", 9),
                     bg=bg, fg=THEME["subtext"]).pack(side=tk.LEFT,
                                                       padx=(6, 0))
        time_text, time_color = self._time_text(provider, data)
        tk.Label(titles, text=time_text, font=("sans-serif", 8),
                 bg=bg, fg=time_color).pack(anchor=tk.W)

        if not data or "error" in data:
            err_msg = (data or {}).get("error", "No data yet")
            tk.Label(section, text=f"! {err_msg}",
                     font=("sans-serif", 9), bg=bg,
                     fg=THEME["orange"], wraplength=320,
                     justify=tk.LEFT).pack(anchor=tk.W, padx=(PANEL_INDENT, 0),
                                           pady=(4, 0))
            # Below the error, still show the last good reading (if any);
            # the header's "last good ... ago" line dates it.
            if not (data or {}).get("windows"):
                return

        # One compact grid row per window: label, %, bar, reset.
        rows = tk.Frame(section, bg=bg)
        rows.pack(fill=tk.X, padx=(PANEL_INDENT, 0), pady=(4, 0))
        for col, minsize in enumerate(PANEL_COL_MIN):
            rows.grid_columnconfigure(col, minsize=minsize)

        windows = data.get("windows", {})
        rep = normalize_rep(data.get("representative_claim", ""))
        r = 0
        for wid in WINDOW_ORDER:
            if wid not in windows:
                continue
            w = windows[wid]
            util = parse_utilization(w.get("utilization", 0)) or 0.0
            util = min(max(util, 0.0), 1.0)
            pct = int(util * 100)
            # A window whose reset already passed no longer holds this
            # usage; grey it out so a stale 98% doesn't read as live.
            stale = reset_passed(w.get("reset", ""))
            color = (THEME["subtext"] if stale
                     else self._color_for_percent(pct))
            # Bold marks the window that drives this provider's tray number.
            weight = "bold" if wid == rep else "normal"

            tk.Label(rows, text=WINDOW_LABELS.get(wid, wid),
                     font=("sans-serif", 9, weight),
                     bg=bg, fg=THEME["text"]).grid(row=r, column=0,
                                                   sticky=tk.W)
            tk.Label(rows, text=f"{pct}%", font=("sans-serif", 10, "bold"),
                     bg=bg, fg=color).grid(row=r, column=1, sticky=tk.E,
                                           padx=(0, 8))

            bar = tk.Frame(rows, bg=THEME["surface"], width=PANEL_BAR_W,
                           height=6)
            bar.grid(row=r, column=2, sticky=tk.W)
            bar.pack_propagate(False)
            tk.Frame(bar, bg=color).place(
                relwidth=min(max(util, 0.02), 1.0), relheight=1.0)

            reset_text = f"reset {format_reset(w.get('reset', ''))}"
            w_status = w.get("status", "")
            if w_status and w_status != "allowed":
                reset_text += f"  •  {w_status}"
            tk.Label(rows, text=reset_text, font=("sans-serif", 8),
                     bg=bg, fg=THEME["subtext"]).grid(row=r, column=3,
                                                      sticky=tk.W)
            r += 1

        notes = []
        extra = data.get("extra", "")
        if extra:
            notes.append(extra)
        overage = data.get("overage_status", "")
        if overage and overage != "allowed":
            reason = data.get("overage_disabled_reason", "")
            overage_text = f"Overage: {overage}"
            if reason:
                overage_text += f" ({reason.replace('_', ' ')})"
            notes.append(overage_text)
        for note in notes:
            tk.Label(section, text=note, font=("sans-serif", 8),
                     bg=bg, fg=THEME["subtext"]).pack(
                anchor=tk.W, padx=(PANEL_INDENT, 0), pady=(2, 0))

    def show(self, origin: str = DEFAULT_PROVIDER, pos: tuple | None = None):
        with self._lock:
            if self._win is not None:
                try:
                    self._win.destroy()
                except tk.TclError:
                    pass
                self._win = None

        # Exactly one bundle on screen: replace a panel another tray icon
        # opened, then record ourselves so the next click can find us.
        old = running_panel_pid()
        if old is not None:
            try:
                os.kill(old, signal.SIGTERM)
            except OSError:
                pass
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            PANEL_LOCK.write_text(str(os.getpid()))
        except OSError:
            pass

        # The clicked provider always gets a section, even before it has
        # written its first cache.
        self._providers = panel_providers() or [origin]
        self._data = {p: load_panel_data(p) for p in self._providers}

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
        header = tk.Frame(container, bg=THEME["surface"], padx=16, pady=8)
        header.pack(fill=tk.X)
        tk.Label(header, text="AI Usage",
                 font=("sans-serif", 12, "bold"),
                 bg=THEME["surface"], fg=THEME["text"]).pack(side=tk.LEFT)
        self._hint_label = tk.Label(header, text="", font=("sans-serif", 8),
                                    bg=THEME["surface"], fg=THEME["subtext"])
        self._hint_label.pack(side=tk.RIGHT)

        # Body (replaceable)
        self._body = tk.Frame(container, bg=THEME["bg"], padx=16, pady=12)
        self._body.pack(fill=tk.X)
        self._fill_body()

        # Footer
        footer = tk.Frame(container, bg=THEME["surface"], padx=16, pady=8)
        footer.pack(fill=tk.X, side=tk.BOTTOM)

        # Button bar
        btn_frame = tk.Frame(footer, bg=THEME["surface"])
        btn_frame.pack(side=tk.RIGHT)

        refresh_btn = tk.Label(btn_frame, text=" Refresh ",
                               font=("sans-serif", 10, "bold"),
                               bg=THEME["blue"], fg="#1e1e2e",
                               cursor="hand2", padx=6, pady=2)
        refresh_btn.pack(side=tk.LEFT, padx=(0, 6))

        # Fetches run on a worker thread, but every Tk call must happen on
        # the main thread. The worker only does I/O and hands the results
        # back through this queue; a main-thread poller applies them.
        result_q: queue.Queue = queue.Queue()

        def _run_refresh():
            results = {}
            for provider in self._providers:
                try:
                    new_data = fetch_for_provider(provider)
                except Exception as e:   # one provider must not sink the rest
                    new_data = {"error": f"Refresh failed: {e}"}
                if new_data is None:
                    new_data = {"error": "Claude Code not logged in"}
                if "error" not in new_data:
                    save_cache(new_data, provider)
                save_status(provider, new_data.get("error"))
                # Ask that provider's tray to reload cache + status.
                try:
                    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                    trigger_path(provider).touch()
                except OSError:
                    pass
                results[provider] = new_data
            result_q.put(results)

        def _done_refresh():
            if not win.winfo_exists():
                return
            refresh_btn.config(text=" Refresh ", bg=THEME["blue"])
            self._refreshing = False
            win.focus_force()

        def _poll_result():
            if not win.winfo_exists():
                return
            try:
                results = result_q.get_nowait()
            except queue.Empty:
                win.after(100, _poll_result)
                return
            for provider in results:
                # Re-read so an error keeps the last good windows under it.
                self._data[provider] = load_panel_data(provider)
            self._fill_body()
            _done_refresh()
            win.update_idletasks()
            _place()

        def do_refresh(e=None):
            if self._refreshing:
                return  # a refresh is already in flight — avoid a 2nd probe
            self._refreshing = True
            refresh_btn.config(text=" ... ", bg=THEME["surface2"])
            win.update()
            threading.Thread(target=_run_refresh, daemon=True).start()
            win.after(100, _poll_result)  # scheduled from the main thread

        refresh_btn.bind("<Button-1>", do_refresh)

        # Quit button - the icons are one bundle, so stop every monitor
        quit_btn = tk.Label(btn_frame, text="  Quit  ",
                            font=("sans-serif", 10, "bold"),
                            bg=THEME["red"], fg="#1e1e2e",
                            cursor="hand2", padx=6, pady=2)
        quit_btn.pack(side=tk.LEFT)

        def quit_monitors(e=None):
            # Signal each monitor by the PID in its own lock file, and only
            # once /proc confirms it is that provider's monitor.py — never a
            # broad pgrep/pkill that could hit an editor or unrelated process.
            for provider in PROVIDERS:
                pid = running_monitor_pid(provider)
                if pid is None:
                    continue
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
            self._close()

        quit_btn.bind("<Button-1>", quit_monitors)

        self._hint_label.config(
            text=f"{len(self._providers)} provider"
                 f"{'' if len(self._providers) == 1 else 's'}")

        # Position & size
        def _place():
            w = win.winfo_reqwidth()
            h = win.winfo_reqheight()
            screen_w = win.winfo_screenwidth()
            screen_h = win.winfo_screenheight()
            # Below the tray area, aligned near mouse X, kept on screen.
            x = max(8, min(mx - w // 2, screen_w - w - 8))
            y = max(8, min(my + 10, screen_h - h - 8))
            win.geometry(f"{w}x{h}+{x}+{y}")

        win.update_idletasks()
        _place()

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
                px = win.winfo_pointerx()
                py = win.winfo_pointery()
                wx = win.winfo_rootx()
                wy = win.winfo_rooty()
                ww = win.winfo_width()
                wh = win.winfo_height()
                inside = wx <= px <= wx + ww and wy <= py <= wy + wh
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
        try:
            win.mainloop()
        finally:
            release_panel_lock()

    def _close(self):
        with self._lock:
            if self._win:
                try:
                    self._win.destroy()
                except tk.TclError:
                    pass
                self._win = None


def release_panel_lock():
    try:
        if PANEL_LOCK.exists() and PANEL_LOCK.read_text().strip() == str(os.getpid()):
            PANEL_LOCK.unlink(missing_ok=True)
    except OSError:
        pass


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
        self._watch_mtime: float | None = self._watched_mtime()

    def refresh(self) -> bool:
        """Fetch and apply. False if another refresh was already running."""
        # Single-flight: collapse overlapping refreshes (auto timer, menu,
        # startup) so we never fire concurrent API probes.
        if not self._refresh_inflight.acquire(blocking=False):
            return False
        try:
            data = fetch_for_provider(PROVIDER)
            if data is None:
                data = {"error": "Claude Code not logged in"}

            with self._lock:
                self.usage_data = data
                if "error" not in data:
                    save_cache(data)
                # Lets the shared panel show this icon's current error.
                save_status(PROVIDER, data.get("error"))
            self._update_icon()
        finally:
            self._refresh_inflight.release()
        return True

    def _update_icon(self):
        if not self.icon:
            return
        data = self.usage_data  # single atomic read for a consistent snapshot
        has_error = bool(data and "error" in data)
        pct = get_primary_percent(data)
        spec = PROVIDERS[PROVIDER]
        self.icon.icon = make_icon(PROVIDER, pct, error=has_error)
        if has_error:
            self.icon.title = f"{spec['label']}: {data['error']}"
        elif pct is not None:
            title = f"{spec['label']}: {pct}%"
            # The tooltip is the only place a stale local reading can be
            # called out without opening the panel.
            if spec["local"] and data and "fetched_at" in data:
                try:
                    when = datetime.fromisoformat(data["fetched_at"])
                    title += f"  ({format_age(age_seconds(when))} ago)"
                except ValueError:
                    pass
            self.icon.title = title
        else:
            self.icon.title = f"{spec['label']} Usage Monitor"

    def _watched_mtime(self) -> float | None:
        watch = PROVIDERS[PROVIDER].get("watch")
        try:
            return watch.stat().st_mtime if watch else None
        except OSError:
            return None

    def _refresh_for_watch(self, mtime: float):
        # Only mark this change seen once a refresh actually read it. If one
        # was already in flight (it may have read the file just before the
        # change), the next 2s poll sees the same mtime and tries again.
        if self.refresh():
            self._watch_mtime = mtime

    def _poll_trigger(self):
        """Every 2s, reload the cache if the panel signalled a refresh, and
        re-read a local provider whose CLI just rewrote its usage file.

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
            # Same lock refresh() holds while it records a result, so an
            # error it records can't be overwritten by a cache read that
            # started just before it.
            with self._lock:
                err = load_status_error(PROVIDER)
                data = {"error": err} if err else load_cache()
                if data is not None:
                    self.usage_data = data
            if data is not None:
                self._update_icon()
        # A stat every 2s is cheap; the read itself runs on its own thread
        # so a slow parse never delays this timer chain.
        mtime = self._watched_mtime()
        if mtime is not None and mtime != self._watch_mtime:
            threading.Thread(target=self._refresh_for_watch, args=(mtime,),
                             daemon=True).start()
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

        # The panel is shared by every tray icon, so one opened from another
        # icon toggles closed here too, instead of a second one stacking up.
        other = running_panel_pid()
        if other is not None:
            try:
                os.kill(other, signal.SIGTERM)
            except OSError:
                pass
            return

        # Spawn the panel as an isolated subprocess. It reads the pointer
        # position and every provider's cache itself, so we never create a Tk
        # root inside this pystray/GTK process and pass it only our provider.
        self._panel_proc = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--panel", PROVIDER],
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
        spec = PROVIDERS[PROVIDER]
        # Only the Claude provider needs credentials, and only it can be so
        # broken at startup that there is nothing to show. Codex, Grok and agy read
        # files that simply may not exist yet, which the panel reports as a
        # hint rather than a fatal error — their tray icon still comes up.
        if not spec["local"] and not get_token():
            self._show_login_guide()
            sys.exit(1)

        self.refresh()

        self.icon = pystray.Icon(
            name=f"{APP_NAME}-{PROVIDER}",
            icon=make_icon(PROVIDER, get_primary_percent(self.usage_data),
                           error=bool(self.usage_data and "error" in self.usage_data)),
            title=f"{spec['label']} Usage Monitor",
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
    # (`--panel <provider>`; the provider is the icon that was clicked).
    if len(sys.argv) >= 2 and sys.argv[1] == "--panel":
        origin = next((a for a in reversed(sys.argv[2:]) if a in PROVIDERS),
                      DEFAULT_PROVIDER)
        DropdownPanel().show(origin)
        return

    provider = DEFAULT_PROVIDER
    args = sys.argv[1:]
    if args:
        if args[0] == "--provider" and len(args) >= 2:
            provider = args[1]
        elif args[0].startswith("--provider="):
            provider = args[0].split("=", 1)[1]
        else:
            print(f"Usage: {sys.argv[0]} [--provider "
                  f"{'|'.join(PROVIDERS)}]", file=sys.stderr)
            sys.exit(2)
        if provider not in PROVIDERS:
            print(f"Unknown provider '{provider}' "
                  f"(expected one of: {', '.join(PROVIDERS)})", file=sys.stderr)
            sys.exit(2)
    select_provider(provider)

    if not acquire_single_instance():
        print(f"{APP_NAME} ({provider}) is already running.", file=sys.stderr)
        return

    UsageMonitor().run()


if __name__ == "__main__":
    main()
