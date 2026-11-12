"""Per-session background daemon: claim heartbeats and cross-colleague comment relays."""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import time
from pathlib import Path

from . import config, herdr, state
from .engine import KanbanEngine

TICK_SECONDS = 5.0
MAX_SERVER_MISSES = 6


def lock_path() -> Path:
    return state.state_dir() / f"daemon-{state.herdr_session_key()}.lock"


def running() -> bool:
    try:
        pid = int(lock_path().read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return False
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
            return b"daemon" in cmdline.read_bytes()
        except OSError:
            return False
    return True


def ensure() -> bool:
    """Spawn the session daemon if not already running (must run outside agent sandboxes)."""
    if running() or not herdr.binary():
        return False
    cli = config.ROOT / "bin" / "herdr-kanban"
    log_path = state.state_dir() / "daemon.log"
    with open(log_path, "a", encoding="utf-8") as log:
        subprocess.Popen(
            [sys.executable, str(cli), "daemon"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            close_fds=True,
        )
    return True


def run() -> int:
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
    last_heartbeats: dict[str, float] = {}
    misses = 0

    while True:
        settings = config.load_user_settings()
        if engine.herdr.call("pane", "list") is None:
            misses += 1
            if misses >= MAX_SERVER_MISSES:
                return 0
            time.sleep(TICK_SECONDS)
            continue
        misses = 0

        now = time.time()
        claims = state.list_local_claims()
        if claims and (now - last_poll) >= settings.poll_seconds:
            last_poll = now
            engine.sync_claim_heartbeats_and_relays(last_heartbeat_by_key=last_heartbeats)

        time.sleep(TICK_SECONDS)
