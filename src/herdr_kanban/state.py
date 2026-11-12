"""Local runtime state: active claims, relay inboxes, watermarks, and board cache."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .model import InboxItem


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
    sock = os.environ.get("HERDR_SOCKET_PATH") or "default"
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in Path(sock).stem)
    return safe or "default"


@contextlib.contextmanager
def _locked(lock_file: Path) -> Iterator[None]:
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_file, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
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


def _cache_path(slug: str, project_number: int) -> Path:
    return state_dir() / "cache" / f"{slug}-p{project_number}.json"


def save_board_cache(slug: str, project_number: int, tickets: list[dict[str, Any]]) -> None:
    payload = {"fetched_at": time.time(), "tickets": tickets}
    _atomic_write_json(_cache_path(slug, project_number), payload)


def load_board_cache(slug: str, project_number: int) -> tuple[float, list[dict[str, Any]]] | None:
    raw = _read_json(_cache_path(slug, project_number), None)
    if not isinstance(raw, dict) or not isinstance(raw.get("tickets"), list):
        return None
    return float(raw.get("fetched_at") or 0.0), raw["tickets"]
