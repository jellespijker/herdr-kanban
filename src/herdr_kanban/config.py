"""Board & user configuration loading and TOML rendering (docs/spec.md §5, ADR-0005)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .model import COLUMN_TITLES, Column

ROOT = Path(__file__).resolve().parents[2]
BOARD_FILENAME = ".herdr-kanban.toml"
LOCAL_BOARD_FILENAME = ".herdr-kanban.local.toml"
SUPPORTED_AGENTS = ("claude", "agy", "codex", "opencode", "gemini", "copilot", "pi", "amp", "droid")


def plugin_config_dir() -> Path:
    explicit = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if explicit:
        return Path(explicit)
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg) / "herdr" / "plugins" / "config" / "herdr-kanban"


def repo_slug(owner: str, repo: str) -> str:
    safe_owner = owner.replace("/", "_").strip()
    safe_repo = repo.replace("/", "_").strip()
    return f"{safe_owner}__{safe_repo}"


def local_repo_config_path(owner: str, repo: str) -> Path:
    return plugin_config_dir() / "repos" / f"{repo_slug(owner, repo)}.toml"


def user_settings_path() -> Path:
    return plugin_config_dir() / "settings.toml"


@dataclass(slots=True)
class BoardConfig:
    project_owner: str
    project_number: int
    target_branch: str
    repo_owner: str = ""
    repo_name: str = ""
    remote: str = "origin"
    check_command: str = ""
    lease_ttl_minutes: int = 30
    require_review: bool = False
    auto_ready_deposits: bool = False
    close_issue_on_done: bool = True
    label_filter: str = ""
    trusted_authors: list[str] = field(default_factory=list)
    columns: dict[str, str] = field(
        default_factory=lambda: {col.value: title for col, title in COLUMN_TITLES.items()}
    )
    source_path: Path | None = None

    @classmethod
    def from_mapping(cls, data: dict[str, Any], source_path: Path | None = None) -> BoardConfig:
        board = data.get("board")
        if not isinstance(board, dict):
            raise ValueError("missing [board] section in configuration")
        project_owner = str(board.get("project_owner") or "").strip()
        target_branch = str(board.get("target_branch") or "").strip()
        try:
            project_number = int(board["project_number"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("board.project_number must be a positive integer") from exc
        if not project_owner or project_number < 1 or not target_branch:
            raise ValueError(
                "board.project_owner, board.project_number, and board.target_branch are required"
            )

        cols = {col.value: title for col, title in COLUMN_TITLES.items()}
        raw_cols = data.get("columns")
        if isinstance(raw_cols, dict):
            for key, val in raw_cols.items():
                if key in cols and isinstance(val, str) and val.strip():
                    cols[key] = val.strip()

        return cls(
            project_owner=project_owner,
            project_number=project_number,
            target_branch=target_branch,
            repo_owner=str(board.get("repo_owner") or "").strip(),
            repo_name=str(board.get("repo_name") or "").strip(),
            remote=str(board.get("remote") or "origin").strip() or "origin",
            check_command=str(board.get("check_command") or "").strip(),
            lease_ttl_minutes=max(5, int(board.get("lease_ttl_minutes") or 30)),
            require_review=bool(board.get("require_review", False)),
            auto_ready_deposits=bool(board.get("auto_ready_deposits", False)),
            close_issue_on_done=bool(board.get("close_issue_on_done", True)),
            label_filter=str(board.get("label_filter") or "").strip(),
            trusted_authors=[str(u).strip() for u in (board.get("trusted_authors") or []) if str(u).strip()],
            columns=cols,
            source_path=source_path,
        )


@dataclass(slots=True)
class UserSettings:
    default_agent_kind: str = "claude"
    worktree_root: str = ""
    poll_seconds: int = 25
    heartbeat_seconds: int = 120
    machines: list[str] = field(default_factory=list)

    def resolved_worktree_root(self) -> Path:
        if self.worktree_root:
            return Path(self.worktree_root).expanduser()
        xdg = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        return Path(xdg) / "herdr-kanban" / "worktrees"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], val)
        else:
            merged[key] = val
    return merged


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return tomllib.loads(path.read_text(encoding="utf-8"))


def load_board_config(
    worktree_root: Path | None,
    common_repo_root: Path | None = None,
    owner_repo: tuple[str, str] | None = None,
) -> BoardConfig | None:
    """Discover and merge board configuration from worktree, main repo, and local overrides."""
    candidates: list[Path] = []
    for root in (common_repo_root, worktree_root):
        if root is not None:
            for name in (BOARD_FILENAME, LOCAL_BOARD_FILENAME):
                path = root / name
                if path not in candidates:
                    candidates.append(path)
    if owner_repo:
        candidates.append(local_repo_config_path(*owner_repo))

    merged: dict[str, Any] = {}
    primary_source: Path | None = None
    for path in candidates:
        if path.is_file():
            data = _read_toml(path)
            if data:
                merged = _deep_merge(merged, data)
                if primary_source is None:
                    primary_source = path
    if not merged:
        return None
    cfg = BoardConfig.from_mapping(merged, source_path=primary_source)
    if owner_repo and not cfg.repo_owner:
        cfg.repo_owner, cfg.repo_name = owner_repo
    return cfg


def load_user_settings() -> UserSettings:
    raw = _read_toml(user_settings_path())
    agent = str(raw.get("default_agent_kind") or os.environ.get("HERDR_KANBAN_AGENT") or "claude").strip()
    return UserSettings(
        default_agent_kind=agent,
        worktree_root=str(raw.get("worktree_root") or "").strip(),
        poll_seconds=max(10, int(raw.get("poll_seconds") or 25)),
        heartbeat_seconds=max(30, int(raw.get("heartbeat_seconds") or 120)),
        machines=[str(m).strip() for m in (raw.get("machines") or []) if str(m).strip()],
    )


def save_user_settings(settings: UserSettings) -> Path:
    path = user_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Personal herdr-kanban preferences (never committed to git).",
        f"default_agent_kind = {_toml_quote(settings.default_agent_kind)}",
        f"poll_seconds = {settings.poll_seconds}",
        f"heartbeat_seconds = {settings.heartbeat_seconds}",
    ]
    if settings.worktree_root:
        lines.append(f"worktree_root = {_toml_quote(settings.worktree_root)}")
    if settings.machines:
        items = ", ".join(_toml_quote(m) for m in settings.machines)
        lines.append(f"machines = [{items}]")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _toml_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_board_config_toml(cfg: BoardConfig) -> str:
    """Serialize a BoardConfig as clean, commented TOML suitable for committing."""
    lines = [
        "# .herdr-kanban.toml — shared multi-agent Kanban configuration.",
        "# Colleagues checking out this branch automatically connect to the same GitHub Project.",
        "",
        "[board]",
        f"project_owner = {_toml_quote(cfg.project_owner)}",
        f"project_number = {cfg.project_number}",
        f"target_branch = {_toml_quote(cfg.target_branch)}",
        f"remote = {_toml_quote(cfg.remote)}",
    ]
    if cfg.repo_owner and cfg.repo_name:
        lines.append(f"repo_owner = {_toml_quote(cfg.repo_owner)}")
        lines.append(f"repo_name = {_toml_quote(cfg.repo_name)}")
    if cfg.check_command:
        lines.append(f"check_command = {_toml_quote(cfg.check_command)}")
    lines.extend(
        [
            f"lease_ttl_minutes = {cfg.lease_ttl_minutes}",
            f"require_review = {'true' if cfg.require_review else 'false'}",
            f"auto_ready_deposits = {'true' if cfg.auto_ready_deposits else 'false'}",
            f"close_issue_on_done = {'true' if cfg.close_issue_on_done else 'false'}",
        ]
    )
    if cfg.label_filter:
        lines.append(f"label_filter = {_toml_quote(cfg.label_filter)}")
    if cfg.trusted_authors:
        authors = ", ".join(_toml_quote(a) for a in cfg.trusted_authors)
        lines.append(f"trusted_authors = [{authors}]")
    lines.extend(["", "[columns]"])
    for col in Column:
        name = cfg.columns.get(col.value, COLUMN_TITLES[col])
        lines.append(f"{col.value} = {_toml_quote(name)}")
    return "\n".join(lines) + "\n"
