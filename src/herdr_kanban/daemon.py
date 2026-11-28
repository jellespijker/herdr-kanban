"""Single-instance per-machine background daemon: claim heartbeats and comment relays."""

from __future__ import annotations

import contextlib
import fcntl
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import config, herdr, state
from .engine import KanbanEngine
from .github import GitHubError

TICK_SECONDS = 5.0
MAX_SERVER_MISSES = 6
RATE_LIMIT_BASE_BACKOFF_SEC = 60.0
RATE_LIMIT_MAX_BACKOFF_SEC = 300.0


def lock_path() -> Path:
    """Return a single per-state-dir lockfile so `herdr` and `herdr-core` never duplicate daemons."""
    return state.state_dir() / "daemon.lock"


def _pid_is_herdr_kanban_daemon(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

    cmdline = Path(f"/proc/{pid}/cmdline")
    if cmdline.exists():
        try:
            raw = cmdline.read_bytes()
            return b"herdr-kanban" in raw and b"daemon" in raw
        except OSError:
            return False

    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=3,
        )
        out = (proc.stdout or "").strip()
        return proc.returncode == 0 and "herdr-kanban" in out and "daemon" in out
    except (OSError, subprocess.SubprocessError):
        return False


def reap_legacy_session_daemons() -> list[int]:
    """Terminate older per-session `daemon-*.lock` duplicate daemons and delete stale lockfiles."""
    reaped: list[int] = []
    st_dir = state.state_dir()
    if not st_dir.is_dir():
        return reaped
    me = os.getpid()
    for legacy_lock in sorted(st_dir.glob("daemon-*.lock")):
        pid = 0
        with contextlib.suppress(OSError, ValueError):
            pid = int(legacy_lock.read_text(encoding="utf-8").strip() or "0")
        if pid > 0 and pid != me and _pid_is_herdr_kanban_daemon(pid):
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGTERM)
                reaped.append(pid)
        with contextlib.suppress(OSError):
            legacy_lock.unlink()
    return reaped


def running() -> bool:
    try:
        pid = int(lock_path().read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return False
    return _pid_is_herdr_kanban_daemon(pid)


def _is_rate_limit_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return (
        "rate limit" in msg
        or "secondary rate" in msg
        or "http 403" in msg
        or "http 429" in msg
    )


def ensure() -> bool:
    """Spawn the machine daemon if not already running (must run outside agent sandboxes)."""
    reap_legacy_session_daemons()
    if running() or not herdr.any_binary():
        return False
    cli = config.ROOT / "bin" / "herdr-kanban"
    log_path = state.state_dir() / "daemon.log"
    with open(log_path, "a", encoding="utf-8") as log:
        env = dict(os.environ)
        env["HERDR_KANBAN_SURFACE"] = "daemon"
        subprocess.Popen(
            [sys.executable, str(cli), "--surface", "daemon", "daemon"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
    return True


def run() -> int:
    os.environ["HERDR_KANBAN_SURFACE"] = "daemon"
    reap_legacy_session_daemons()
    handle = open(lock_path(), "a+", encoding="utf-8")  # noqa: SIM115 - held open as flock lifetime
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()

    engine = KanbanEngine()
    last_poll = 0.0
    backoff_until = 0.0
    backoff_sec = RATE_LIMIT_BASE_BACKOFF_SEC
    _, last_heartbeats = state.load_daemon_sync_state()
    misses = 0

    while True:
        settings = config.load_user_settings()
        if not engine.herdr.server_alive():
            misses += 1
            if misses >= MAX_SERVER_MISSES:
                return 0
            time.sleep(TICK_SECONDS)
            continue
        misses = 0

        now = time.time()
        claims = state.list_local_claims()
        if claims and now >= backoff_until and (now - last_poll) >= settings.poll_seconds:
            last_poll = now
            try:
                engine.sync_claim_heartbeats_and_relays(last_heartbeat_by_key=last_heartbeats)
                backoff_sec = RATE_LIMIT_BASE_BACKOFF_SEC
            except GitHubError as exc:
                if _is_rate_limit_error(exc):
                    backoff_until = time.time() + backoff_sec
                    backoff_sec = min(RATE_LIMIT_MAX_BACKOFF_SEC, backoff_sec * 2.0)

        time.sleep(TICK_SECONDS)
