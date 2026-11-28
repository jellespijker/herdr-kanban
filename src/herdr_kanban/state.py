"""Local runtime state: active claims, relay inboxes, watermarks, and board cache."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import threading
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .model import InboxItem

_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.Lock] = {}


def _thread_lock_for(lock_file: Path) -> threading.Lock:
    key = str(lock_file.resolve())
    with _THREAD_LOCKS_GUARD:
        tlock = _THREAD_LOCKS.get(key)
        if tlock is None:
            tlock = threading.Lock()
            _THREAD_LOCKS[key] = tlock
        return tlock


def state_dir() -> Path:
    explicit = os.environ.get("HERDR_KANBAN_STATE_DIR")
    if explicit:
        root = Path(explicit)
    else:
        xdg = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
        root = Path(xdg) / "herdr-kanban"
    try:
        root.mkdir(parents=True, exist_ok=True)
        return root
    except OSError:
        fallback = Path(f"/tmp/herdr-kanban-{os.getuid()}")
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def herdr_session_key() -> str:
    sock = (
        os.environ.get("HERDR_SOCKET_PATH")
        or os.environ.get("HC_TERMINAL_SOCKET")
        or os.environ.get("HC_SCOPE")
        or "default"
    )
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in Path(sock).stem)
    return safe or "default"


@contextlib.contextmanager
def _locked(lock_file: Path) -> Iterator[None]:
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    tlock = _thread_lock_for(lock_file)
    with tlock, open(lock_file, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


@dataclass(slots=True)
class LocalClaim:
    repo_slug: str
    repo_owner: str
    repo_name: str
    main_repo_root: str
    issue_number: int
    worktree_path: str
    local_branch: str
    target_branch: str
    remote: str
    comment_id: int
    owner: str
    agent_kind: str
    agent_name: str = ""
    pane_id: str = ""
    tab_id: str = ""
    workspace_id: str = ""
    herdr_session: str = ""
    risk_gate: bool = False
    started_at: str = ""
    umbrella_root: str = ""
    repo_alias: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> LocalClaim:
        return cls(
            repo_slug=str(raw["repo_slug"]),
            repo_owner=str(raw.get("repo_owner") or ""),
            repo_name=str(raw.get("repo_name") or ""),
            main_repo_root=str(raw.get("main_repo_root") or ""),
            issue_number=int(raw["issue_number"]),
            worktree_path=str(raw["worktree_path"]),
            local_branch=str(raw["local_branch"]),
            target_branch=str(raw["target_branch"]),
            remote=str(raw.get("remote") or "origin"),
            comment_id=int(raw["comment_id"]),
            owner=str(raw["owner"]),
            agent_kind=str(raw.get("agent_kind") or "agent"),
            agent_name=str(raw.get("agent_name") or ""),
            pane_id=str(raw.get("pane_id") or ""),
            tab_id=str(raw.get("tab_id") or ""),
            workspace_id=str(raw.get("workspace_id") or ""),
            herdr_session=str(raw.get("herdr_session") or ""),
            risk_gate=bool(raw.get("risk_gate", False)),
            started_at=str(raw.get("started_at") or ""),
            umbrella_root=str(raw.get("umbrella_root") or ""),
            repo_alias=str(raw.get("repo_alias") or ""),
        )


def _claim_path(slug: str, issue_number: int) -> Path:
    return state_dir() / "claims" / slug / f"issue-{issue_number}.json"


def save_local_claim(claim: LocalClaim) -> None:
    path = _claim_path(claim.repo_slug, claim.issue_number)
    with _locked(state_dir() / "claims" / ".lock"):
        _atomic_write_json(path, claim.to_dict())


def get_local_claim(slug: str, issue_number: int) -> LocalClaim | None:
    raw = _read_json(_claim_path(slug, issue_number), None)
    if not isinstance(raw, dict):
        return None
    try:
        return LocalClaim.from_dict(raw)
    except (KeyError, TypeError, ValueError):
        return None


def remove_local_claim(slug: str, issue_number: int) -> None:
    path = _claim_path(slug, issue_number)
    with _locked(state_dir() / "claims" / ".lock"):
        if path.exists():
            path.unlink()


def list_local_claims(slug: str | None = None) -> list[LocalClaim]:
    claims_root = state_dir() / "claims"
    if not claims_root.is_dir():
        return []
    dirs = [claims_root / slug] if slug else [p for p in claims_root.iterdir() if p.is_dir()]
    results: list[LocalClaim] = []
    for directory in dirs:
        if not directory.is_dir():
            continue
        for file in sorted(directory.glob("issue-*.json")):
            raw = _read_json(file, None)
            if isinstance(raw, dict):
                try:
                    results.append(LocalClaim.from_dict(raw))
                except (KeyError, TypeError, ValueError):
                    continue
    return results


def find_claim_by_pane(pane_id: str) -> LocalClaim | None:
    for claim in list_local_claims():
        if claim.pane_id == pane_id:
            return claim
    return None


def find_claim_by_cwd(cwd: Path) -> LocalClaim | None:
    resolved = cwd.resolve()
    for claim in list_local_claims():
        try:
            wt = Path(claim.worktree_path).resolve()
        except OSError:
            continue
        if resolved == wt or wt in resolved.parents:
            return claim
    return None


def move_claim_pane(old_pane: str, new_pane: str) -> None:
    claim = find_claim_by_pane(old_pane)
    if claim is not None:
        claim.pane_id = new_pane
        save_local_claim(claim)


# --- Relay Inbox & Comment Watermarks ---


def _inbox_path(slug: str, issue_number: int) -> Path:
    return state_dir() / "inbox" / slug / f"issue-{issue_number}.json"


def append_inbox_item(slug: str, item: InboxItem) -> bool:
    """Append item if its comment_id was not seen yet. Returns True when newly added."""
    path = _inbox_path(slug, item.issue_number)
    with _locked(state_dir() / "inbox" / ".lock"):
        rows = _read_json(path, [])
        if not isinstance(rows, list):
            rows = []
        if any(isinstance(r, dict) and r.get("comment_id") == item.comment_id for r in rows):
            return False
        rows.append(item.to_dict())
        _atomic_write_json(path, rows)
        return True


def list_inbox(slug: str, issue_number: int | None = None, include_acked: bool = False) -> list[InboxItem]:
    inbox_root = state_dir() / "inbox" / slug
    if not inbox_root.is_dir():
        return []
    files = (
        [_inbox_path(slug, issue_number)]
        if issue_number is not None
        else sorted(inbox_root.glob("issue-*.json"))
    )
    items: list[InboxItem] = []
    for file in files:
        for raw in _read_json(file, []):
            if not isinstance(raw, dict):
                continue
            try:
                parsed = InboxItem.from_dict(raw)
            except (KeyError, TypeError, ValueError):
                continue
            if include_acked or not parsed.acked:
                items.append(parsed)
    return sorted(items, key=lambda x: x.comment_id)


def update_inbox_flags(
    slug: str,
    issue_number: int,
    *,
    ack_all: bool = False,
    delivered_ids: set[int] | None = None,
) -> list[InboxItem]:
    path = _inbox_path(slug, issue_number)
    with _locked(state_dir() / "inbox" / ".lock"):
        raw_rows = _read_json(path, [])
        updated: list[dict[str, Any]] = []
        returned: list[InboxItem] = []
        for raw in raw_rows if isinstance(raw_rows, list) else []:
            if not isinstance(raw, dict):
                continue
            item = InboxItem.from_dict(raw)
            if not item.acked:
                returned.append(item)
            if ack_all:
                item.acked = True
            if delivered_ids and item.comment_id in delivered_ids:
                item.delivered = True
            updated.append(item.to_dict())
        _atomic_write_json(path, updated)
    return returned


def get_watermark(slug: str, issue_number: int) -> int:
    data = _read_json(state_dir() / "watermarks.json", {})
    if not isinstance(data, dict):
        return 0
    return int(data.get(f"{slug}:{issue_number}", 0))


def set_watermark(slug: str, issue_number: int, comment_id: int) -> None:
    path = state_dir() / "watermarks.json"
    with _locked(state_dir() / ".watermarks.lock"):
        data = _read_json(path, {})
        if not isinstance(data, dict):
            data = {}
        key = f"{slug}:{issue_number}"
        if comment_id > int(data.get(key, 0)):
            data[key] = comment_id
            _atomic_write_json(path, data)


# --- Board Snapshot Cache ---

DEFAULT_BOARD_CACHE_TTL_SEC = 30.0


def board_cache_dir() -> Path:
    explicit = os.environ.get("HERDR_KANBAN_CACHE_DIR")
    if explicit:
        root = Path(explicit)
    elif os.environ.get("HERDR_KANBAN_STATE_DIR"):
        root = state_dir() / "cache"
    else:
        xdg_cache = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
        root = Path(xdg_cache) / "herdr-kanban"
    try:
        root.mkdir(parents=True, exist_ok=True)
        return root
    except OSError:
        fallback = Path(f"/tmp/herdr-kanban-cache-{os.getuid()}")
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def board_cache_ttl_sec() -> float:
    raw = os.environ.get("HERDR_KANBAN_CACHE_TTL_SEC")
    if raw is None or not raw.strip():
        return DEFAULT_BOARD_CACHE_TTL_SEC
    try:
        return max(0.0, float(raw.strip()))
    except ValueError:
        return DEFAULT_BOARD_CACHE_TTL_SEC


def _safe_cache_token(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in value.strip())
    return safe or "default"


def _cache_path(slug: str, project_number: int, *, project_owner: str = "") -> Path:
    if project_owner.strip():
        return board_cache_dir() / f"board-{_safe_cache_token(project_owner)}-{project_number}.json"
    return board_cache_dir() / f"{slug}-p{project_number}.json"


def save_board_cache(
    slug: str,
    project_number: int,
    tickets: list[dict[str, Any]],
    schema: dict[str, Any] | None = None,
    *,
    project_owner: str = "",
) -> None:
    payload: dict[str, Any] = {"fetched_at": time.time(), "tickets": tickets}
    if schema is not None:
        payload["schema"] = schema
    _atomic_write_json(_cache_path(slug, project_number, project_owner=project_owner), payload)


def load_board_cache(
    slug: str,
    project_number: int,
    *,
    project_owner: str = "",
) -> tuple[float, list[dict[str, Any]]] | None:
    raw = _read_json(_cache_path(slug, project_number, project_owner=project_owner), None)
    if raw is None and project_owner:
        raw = _read_json(_cache_path(slug, project_number), None)
    if not isinstance(raw, dict) or not isinstance(raw.get("tickets"), list):
        return None
    return float(raw.get("fetched_at") or 0.0), raw["tickets"]


def load_board_cache_with_schema(
    slug: str,
    project_number: int,
    *,
    project_owner: str = "",
) -> tuple[float, dict[str, Any] | None, list[dict[str, Any]]] | None:
    raw = _read_json(_cache_path(slug, project_number, project_owner=project_owner), None)
    if raw is None and project_owner:
        raw = _read_json(_cache_path(slug, project_number), None)
    if not isinstance(raw, dict) or not isinstance(raw.get("tickets"), list):
        return None
    schema_raw = raw.get("schema") if isinstance(raw.get("schema"), dict) else None
    return float(raw.get("fetched_at") or 0.0), schema_raw, raw["tickets"]


def invalidate_board_cache(slug: str, project_number: int, *, project_owner: str = "") -> None:
    paths = [_cache_path(slug, project_number, project_owner=project_owner)]
    if project_owner:
        paths.append(_cache_path(slug, project_number))
    for path in paths:
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass


@contextlib.contextmanager
def board_fetch_lock(slug: str, project_number: int, *, project_owner: str = "") -> Iterator[None]:
    """Singleflight file+thread lock so concurrent cache misses coalesce into 1 GraphQL fetch."""
    cache_file = _cache_path(slug, project_number, project_owner=project_owner)
    lock_file = cache_file.with_name(f".{cache_file.name}.fetch.lock")
    with _locked(lock_file):
        yield


# --- Persisted Daemon Sync & Heartbeat Cooldown State ---


def load_daemon_sync_state() -> tuple[float, dict[str, float]]:
    raw = _read_json(state_dir() / "daemon-sync.json", {})
    if not isinstance(raw, dict):
        return 0.0, {}
    last_sync_at = float(raw.get("last_sync_at") or 0.0)
    hb_raw = raw.get("heartbeats")
    heartbeats: dict[str, float] = {}
    if isinstance(hb_raw, dict):
        for k, v in hb_raw.items():
            with contextlib.suppress(TypeError, ValueError):
                heartbeats[str(k)] = float(v)
    return last_sync_at, heartbeats


def save_daemon_sync_state(last_sync_at: float, heartbeats: dict[str, float]) -> None:
    with _locked(state_dir() / ".daemon-sync.lock"):
        _atomic_write_json(
            state_dir() / "daemon-sync.json",
            {"last_sync_at": last_sync_at, "heartbeats": heartbeats},
        )


# --- What's New Digest State ---


def _whats_new_path(slug: str, project_number: int) -> Path:
    return state_dir() / "whats-new" / f"{slug}-p{project_number}.json"


def save_whats_new_summary(slug: str, project_number: int, summary: dict[str, Any]) -> None:
    with _locked(state_dir() / "whats-new" / ".lock"):
        _atomic_write_json(_whats_new_path(slug, project_number), summary)


def load_whats_new_summary(slug: str, project_number: int) -> dict[str, Any] | None:
    raw = _read_json(_whats_new_path(slug, project_number), None)
    if not isinstance(raw, dict) or not raw.get("markdown"):
        return None
    return raw


# --- Privacy-Safe GitHub Polling & Cache Telemetry Counters ---

VALID_TELEMETRY_SURFACES: tuple[str, ...] = ("daemon", "gpui_mod", "tui", "cli")
_REPO_KEY_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def telemetry_counters_path() -> Path:
    return state_dir() / "telemetry-counters.json"


def current_telemetry_surface(explicit: str = "") -> str:
    cand = (explicit or os.environ.get("HERDR_KANBAN_SURFACE") or "cli").strip().lower()
    return cand if cand in VALID_TELEMETRY_SURFACES else "cli"


def normalize_telemetry_repo_key(repo_key: str) -> str:
    raw = (repo_key or "").strip().strip("/")
    if "/" not in raw:
        return ""
    owner, repo = raw.split("/", 1)
    owner = owner.strip()
    repo = repo.strip()
    if not owner or not repo:
        return ""
    if not _REPO_KEY_TOKEN_RE.match(owner) or not _REPO_KEY_TOKEN_RE.match(repo):
        return ""
    return f"{owner}/{repo}"


def load_telemetry_counters() -> dict[str, Any]:
    raw = _read_json(telemetry_counters_path(), None)
    if not isinstance(raw, dict) or not isinstance(raw.get("repos"), dict):
        return {
            "schema_version": 1,
            "herdr_kanban_version": __version__,
            "updated_at": "",
            "repos": {},
        }
    return raw


def record_telemetry_counter(
    repo_key: str,
    *,
    surface: str = "",
    rest_calls: int = 0,
    graphql_calls: int = 0,
    board_cache_hits: int = 0,
    singleflight_coalesced: int = 0,
    claims_gc_reaped: int = 0,
    rate_limit_errors: int = 0,
    version: str = "",
) -> None:
    """Atomically increment monotonic per-`<Owner>/<Repo>` GitHub polling & cache counters."""
    norm_key = normalize_telemetry_repo_key(repo_key)
    if not norm_key:
        return
    if not any(
        x > 0
        for x in (
            rest_calls,
            graphql_calls,
            board_cache_hits,
            singleflight_coalesced,
            claims_gc_reaped,
            rate_limit_errors,
        )
    ):
        return

    surf = current_telemetry_surface(surface)
    ver = (version or __version__).strip() or __version__
    now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    path = telemetry_counters_path()

    with contextlib.suppress(OSError), _locked(state_dir() / ".telemetry-counters.lock"):
        data = _read_json(path, {})
        if not isinstance(data, dict):
            data = {}
        repos = data.get("repos")
        if not isinstance(repos, dict):
            repos = {}

        # Match existing case-insensitively if already recorded under canonical casing
        canonical_key = norm_key
        norm_lc = norm_key.lower()
        for existing_key in repos:
            if isinstance(existing_key, str) and existing_key.lower() == norm_lc:
                canonical_key = existing_key
                break

        entry = repos.get(canonical_key)
        if not isinstance(entry, dict):
            entry = {}

        for field_name, delta in (
            ("rest_calls", rest_calls),
            ("graphql_calls", graphql_calls),
            ("board_cache_hits", board_cache_hits),
            ("singleflight_coalesced", singleflight_coalesced),
            ("claims_gc_reaped", claims_gc_reaped),
            ("rate_limit_errors", rate_limit_errors),
        ):
            cur_val = int(entry.get(field_name) or 0)
            entry[field_name] = cur_val + max(0, int(delta))

        by_surface = entry.get("by_surface")
        if not isinstance(by_surface, dict):
            by_surface = {}
        for s_name in VALID_TELEMETRY_SURFACES:
            s_cur = by_surface.get(s_name)
            if not isinstance(s_cur, dict):
                by_surface[s_name] = {"rest_calls": 0, "graphql_calls": 0}
            else:
                by_surface[s_name] = {
                    "rest_calls": int(s_cur.get("rest_calls") or 0),
                    "graphql_calls": int(s_cur.get("graphql_calls") or 0),
                }
        if rest_calls > 0:
            by_surface[surf]["rest_calls"] += int(rest_calls)
        if graphql_calls > 0:
            by_surface[surf]["graphql_calls"] += int(graphql_calls)
        entry["by_surface"] = by_surface

        repos[canonical_key] = entry
        payload = {
            "schema_version": 1,
            "herdr_kanban_version": ver,
            "updated_at": now_iso,
            "repos": repos,
        }
        _atomic_write_json(path, payload)


