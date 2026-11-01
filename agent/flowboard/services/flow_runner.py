"""Run the Google Flow tab in the background.

Flow Studio reaches Google Flow through the "Flowboard Bridge" extension inside a signed-in Flow tab.
This module owns a *separate* Chrome/Edge profile (``~/.flowboard/flow-profile``) so that tab can live
in its own window, off screen, while the user keeps working in their normal browser.

  * Set up once: a normal visible window opens on Flow. The user signs in and loads the extension in
    that profile (current Chrome/Edge no longer allow loading it from the command line).
  * After that, "Start in background" opens the same profile off screen, and a watchdog restarts it
    if it closes.

A truly hidden (headless) browser would fail Google's captcha, so the window is real but out of sight.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from flowboard.services.flow_client import flow_client

logger = logging.getLogger(__name__)

PROFILE_DIR = Path(os.getenv("FLOWBOARD_FLOW_PROFILE", Path.home() / ".flowboard" / "flow-profile"))
FLOW_URL = "https://flow.google.com/"
EXTENSION_DIR = Path(__file__).resolve().parents[3] / "extension"   # <repo>/extension
_EXT_URL_HINT = "chrome://extensions"

_BROWSERS = {
    "chrome": [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
               r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
               "/usr/bin/google-chrome", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"],
    "edge": [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
             r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
             "/usr/bin/microsoft-edge", "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"],
}
_BACKGROUND_FLAGS = [
    "--window-position=-2400,-2400", "--window-size=1280,800", "--start-minimized",
    # keep timers and rendering alive for a hidden window, otherwise captcha scripts stall
    "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling", "--disable-features=CalculateNativeWinOcclusion",
]

LEASE_S = 120                 # the window may stay while Studio pings us at least this often
JOB_LEASE_S = 600             # a running generation keeps it alive this long

_proc: Optional[subprocess.Popen] = None
_want_running = False         # True only while *we* opened the window and it should stay
_suppressed = False           # the user closed it: never reopen until a fresh Studio visit
_lease_until = 0.0
_watchdog: Optional["asyncio.Task[None]"] = None


class RunnerError(Exception):
    pass


# ── settings (saved next to the profile) ────────────────────────────────────

def _cfg_path() -> Path:
    return PROFILE_DIR / "runner.json"


def _cfg() -> dict[str, Any]:
    try:
        return json.loads(_cfg_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_cfg(patch: dict[str, Any]) -> dict[str, Any]:
    cfg = {**_cfg(), **patch}
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    _cfg_path().write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return cfg


def find_browsers() -> dict[str, str]:
    """Browsers installed on this computer: {"chrome": path, "edge": path}."""
    found: dict[str, str] = {}
    for name, paths in _BROWSERS.items():
        path = next((p for p in paths if Path(p).is_file()), None)
        if path:
            found[name] = path
    return found


# ── process helpers ─────────────────────────────────────────────────────────

def _pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    if sys.platform == "win32":
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                                 capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            return False
        return f'"{pid}"' in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _kill_tree(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=20)
    else:
        try:
            os.kill(pid, 15)
        except OSError:
            pass


def _pid_file() -> Path:
    return PROFILE_DIR / "runner.pid"


def _current_pid() -> Optional[int]:
    if _proc is not None and _proc.poll() is None:
        return _proc.pid
    try:
        pid = int(_pid_file().read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if _pid_alive(pid) else None


def is_running() -> bool:
    return _current_pid() is not None


# ── public API ──────────────────────────────────────────────────────────────

def status() -> dict[str, Any]:
    cfg = _cfg()
    browsers = find_browsers()
    running = is_running()
    connected = flow_client.connected
    setup_done = bool(cfg.get("setup_done"))
    if not browsers:
        state, hint = "no_browser", "Chrome or Edge was not found on this computer."
    elif running and connected:
        state, hint = "working", "Flow is running in the background and the bridge is connected."
    elif running and not connected:
        state, hint = "waiting", ("Window is open but the bridge is not connected yet. "
                                   + ("Load the extension and sign in to Flow in that window." if not setup_done else "Wait a few seconds."))
    elif connected:
        state, hint = "other_browser", "The bridge is connected through another browser window. Start the background window to keep it running by itself."
    elif not setup_done:
        state, hint = "needs_setup", "One-time setup: sign in to Flow in a separate window."
    else:
        state, hint = "stopped", "Background Flow is stopped."
    return {
        "state": state, "hint": hint, "running": running, "bridge_connected": connected,
        "setup_done": setup_done, "browsers": sorted(browsers), "browser": cfg.get("browser") or (sorted(browsers)[0] if browsers else None),
        "keep_running": bool(cfg.get("keep_running")), "mode": cfg.get("mode"),
        "profile": str(PROFILE_DIR), "extension_dir": str(EXTENSION_DIR),
    }


def _launch(mode: str) -> int:
    global _proc
    browsers = find_browsers()
    if not browsers:
        raise RunnerError("Chrome or Edge was not found on this computer.")
    name = _cfg().get("browser")
    path = browsers.get(name) or next(iter(browsers.values()))
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    args = [path, f"--user-data-dir={PROFILE_DIR}", "--no-first-run", "--no-default-browser-check"]
    if mode == "background":
        args += _BACKGROUND_FLAGS
    cfg = _cfg()
    args.append(cfg.get("flow_url") or FLOW_URL)
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "DETACHED_PROCESS", 0)
    _proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    _pid_file().write_text(str(_proc.pid))
    _save_cfg({"mode": mode})
    logger.info("flow runner: started %s (%s) pid=%s", Path(path).name, mode, _proc.pid)
    return _proc.pid


def touch(seconds: int = LEASE_S) -> bool:
    """Studio (or a running job) says "still here". Returns True when this is a fresh visit."""
    global _lease_until
    now = time.time()
    fresh = now > _lease_until
    _lease_until = max(_lease_until, now + seconds)
    return fresh


def heartbeat() -> dict[str, Any]:
    """Called every ~20 s by an open Studio tab. On a fresh visit, opens background Flow if the
    user turned on 'open it with Studio'. Never reopens a window the user closed."""
    global _suppressed, _want_running
    fresh = touch()
    if fresh:
        _suppressed = False
    cfg = _cfg()
    if cfg.get("keep_running") and cfg.get("setup_done") and fresh and not _suppressed and not is_running():
        try:
            _launch("background")
            _want_running = True
        except RunnerError:
            logger.info("flow runner: could not open background Flow")
    return status()


def start(mode: str = "background") -> dict[str, Any]:
    """mode 'setup' = normal visible window (sign in, load the extension); 'background' = off screen."""
    global _want_running, _suppressed
    if mode not in ("setup", "background"):
        raise RunnerError("mode must be setup or background")
    if is_running():
        raise RunnerError("The Flow window is already open. Stop it first." if mode == "setup" else "Background Flow is already running.")
    if mode == "background" and not _cfg().get("setup_done"):
        raise RunnerError("Do the one-time setup first (sign in to Flow and load the extension in that window).")
    _launch(mode)
    _suppressed = False
    _want_running = mode == "background"
    touch()
    return status()


def stop() -> dict[str, Any]:
    global _want_running, _proc
    _want_running = False
    pid = _current_pid()
    if pid:
        _kill_tree(pid)
    _proc = None
    try:
        _pid_file().unlink(missing_ok=True)
    except OSError:
        pass
    return status()


def mark_setup_done(done: bool = True) -> dict[str, Any]:
    _save_cfg({"setup_done": done})
    return status()


def configure(browser: Optional[str] = None, keep_running: Optional[bool] = None, flow_url: Optional[str] = None) -> dict[str, Any]:
    patch: dict[str, Any] = {}
    if browser is not None:
        if browser not in find_browsers():
            raise RunnerError(f"{browser} was not found on this computer.")
        patch["browser"] = browser
    if keep_running is not None:
        patch["keep_running"] = bool(keep_running)
    if flow_url is not None:
        if flow_url and not flow_url.startswith("https://flow.google.com/"):
            raise RunnerError("The address must start with https://flow.google.com/")
        patch["flow_url"] = flow_url
    _save_cfg(patch)
    return status()


# ── watchdog ────────────────────────────────────────────────────────────────

async def _watch() -> None:
    """Every 15 s, for a window this agent opened:
    * the user closed it  -> leave it closed (never reopen by itself)
    * Studio is gone and nothing is generating -> close it, so no browser is left running."""
    global _want_running, _suppressed
    while True:
        await asyncio.sleep(15)
        try:
            if not _want_running:
                continue
            if not is_running():
                _want_running = False
                _suppressed = True
                logger.info("flow runner: window was closed, not reopening")
            elif time.time() > _lease_until:
                logger.info("flow runner: Studio is closed and nothing is running, closing background Flow")
                stop()
        except Exception:  # noqa: BLE001 - never let the watchdog die
            logger.exception("flow runner watchdog")


def start_watchdog() -> None:
    """Called once when the agent starts. It never opens a browser by itself."""
    global _watchdog
    if _watchdog is None or _watchdog.done():
        _watchdog = asyncio.create_task(_watch(), name="flow-runner-watchdog")


def stop_watchdog() -> None:
    global _watchdog
    if _watchdog is not None:
        _watchdog.cancel()
        _watchdog = None
