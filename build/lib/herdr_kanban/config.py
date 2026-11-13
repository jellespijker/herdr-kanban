"""Board & user configuration loading and TOML rendering (docs/spec.md §5, ADR-0005, ADR-0007)."""

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
class RepoEntry:
    alias: str
    owner: str
    repo: str
    path: str
    target_branch: str = ""
    remote: str = ""
    check_command: str = ""

    @property
    def slug(self) -> str:
        return repo_slug(self.owner, self.repo)

    def resolved_path(self, base_dir: Path) -> Path:
        p = Path(self.path).expanduser()
        return p.resolve() if p.is_absolute() else (base_dir / p).resolve()

    def effective_branch(self, board: BoardConfig) -> str:
        return self.target_branch or board.target_branch

    def effective_remote(self, board: BoardConfig) -> str:
        return self.remote or board.remote

    def effective_check(self, board: BoardConfig) -> str:
        return self.check_command if self.check_command else board.check_command


@dataclass(slots=True)
class FacetSpec:
    name: str
    prefix: str
    required_for_ready: bool = False
    multiple: bool = True
    color: str = "ededed"
    description: str = ""
    values: list[str] = field(default_factory=list)


@dataclass(slots=True)
class BoardConfig:
    project_owner: str
    project_number: int
    target_branch: str
    repo_owner: str = ""
    repo_name: str = ""
    default_repo: str = ""
    issues_repo: str = ""
    remote: str = "origin"
    check_command: str = ""
    lease_ttl_minutes: int = 30
    require_review: bool = False
    auto_ready_deposits: bool = False
    close_issue_on_done: bool = True
    label_filter: str = ""
    priority_field: str = "Priority"
    priorities: list[str] = field(default_factory=list)
    require_priority_for_ready: bool = False
    milestones: list[str] = field(default_factory=list)
    facets: dict[str, FacetSpec] = field(default_factory=dict)
    trusted_authors: list[str] = field(default_factory=list)
    columns: dict[str, str] = field(
        default_factory=lambda: {col.value: title for col, title in COLUMN_TITLES.items()}
    )
    repos: dict[str, RepoEntry] = field(default_factory=dict)
    source_path: Path | None = None

    @property
    def is_multi_repo(self) -> bool:
        return len(self.repos) > 0

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

        repos_map: dict[str, RepoEntry] = {}
        raw_repos = data.get("repos")
        if isinstance(raw_repos, dict):
            for alias, entry in raw_repos.items():
                if not isinstance(entry, dict):
                    continue
                owner = str(entry.get("owner") or project_owner).strip()
                rname = str(entry.get("repo") or alias).strip()
                rpath = str(entry.get("path") or alias).strip()
                if owner and rname and rpath:
                    repos_map[str(alias)] = RepoEntry(
                        alias=str(alias),
                        owner=owner,
                        repo=rname,
                        path=rpath,
                        target_branch=str(entry.get("target_branch") or "").strip(),
                        remote=str(entry.get("remote") or "").strip(),
                        check_command=str(entry.get("check_command") or "").strip(),
                    )

        facets_map: dict[str, FacetSpec] = {}
        taxonomy_raw = data.get("taxonomy")
        raw_facets = taxonomy_raw.get("facets") if isinstance(taxonomy_raw, dict) else None
        if isinstance(raw_facets, dict):
            for fname, fentry in raw_facets.items():
                if not isinstance(fentry, dict):
                    continue
                prefix = str(fentry.get("prefix") or f"{fname}:").strip()
                color = str(fentry.get("color") or "ededed").strip().lstrip("#") or "ededed"
                vals = [str(v).strip() for v in (fentry.get("values") or []) if str(v).strip()]
                facets_map[str(fname)] = FacetSpec(
                    name=str(fname),
                    prefix=prefix,
                    required_for_ready=bool(fentry.get("required_for_ready", False)),
                    multiple=bool(fentry.get("multiple", True)),
                    color=color,
                    description=str(fentry.get("description") or "").strip(),
                    values=vals,
                )

        priorities = [str(p).strip() for p in (board.get("priorities") or []) if str(p).strip()]
        milestones = [str(m).strip() for m in (board.get("milestones") or []) if str(m).strip()]

        return cls(
            project_owner=project_owner,
            project_number=project_number,
            target_branch=target_branch,
            repo_owner=str(board.get("repo_owner") or "").strip(),
            repo_name=str(board.get("repo_name") or "").strip(),
            default_repo=str(board.get("default_repo") or "").strip(),
            issues_repo=str(board.get("issues_repo") or "").strip(),
            remote=str(board.get("remote") or "origin").strip() or "origin",
            check_command=str(board.get("check_command") or "").strip(),
            lease_ttl_minutes=max(5, int(board.get("lease_ttl_minutes") or 30)),
            require_review=bool(board.get("require_review", False)),
            auto_ready_deposits=bool(board.get("auto_ready_deposits", False)),
            close_issue_on_done=bool(board.get("close_issue_on_done", True)),
            label_filter=str(board.get("label_filter") or "").strip(),
            priority_field=str(board.get("priority_field") or "Priority").strip() or "Priority",
            priorities=priorities,
            require_priority_for_ready=bool(board.get("require_priority_for_ready", False)),
            milestones=milestones,
            facets=facets_map,
            trusted_authors=[str(u).strip() for u in (board.get("trusted_authors") or []) if str(u).strip()],
            columns=cols,
            repos=repos_map,
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


def find_umbrella_dir(start: Path, main_repo_root: Path | None = None) -> Path | None:
    """Walk upward to find an umbrella directory whose `.herdr-kanban.toml` has `[repos]`,
    or `start` itself if it is a non-git folder containing `.herdr-kanban.toml`.
    """
    roots_to_walk: list[Path] = []
    if main_repo_root is not None:
        roots_to_walk.extend([main_repo_root.parent, main_repo_root])
    roots_to_walk.extend([start, *list(start.parents)[:4]])

    seen: set[Path] = set()
    for directory in roots_to_walk:
        resolved = directory.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        cfg_file = resolved / BOARD_FILENAME
        if not cfg_file.is_file():
            continue
        try:
            raw = _read_toml(cfg_file)
        except tomllib.TOMLDecodeError:
            continue
        if isinstance(raw.get("repos"), dict) and raw["repos"]:
            return resolved
        if resolved == start.resolve() and main_repo_root is None:
            return resolved
    return None


def load_board_config(
    worktree_root: Path | None,
    common_repo_root: Path | None = None,
    owner_repo: tuple[str, str] | None = None,
    umbrella_root: Path | None = None,
) -> BoardConfig | None:
    """Discover and merge board configuration from umbrella root, main repo, worktree, and local overrides."""
    candidates: list[Path] = []
    roots = [umbrella_root] if umbrella_root is not None else [common_repo_root, worktree_root]
    for root in roots:
        if root is not None:
            for name in (BOARD_FILENAME, LOCAL_BOARD_FILENAME):
                path = root / name
                if path not in candidates:
                    candidates.append(path)
    if owner_repo and umbrella_root is None:
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
    if owner_repo and not cfg.is_multi_repo and not cfg.repo_owner:
        cfg.repo_owner, cfg.repo_name = owner_repo
    return cfg


def _detect_env_agent_kind() -> str:
    env_agent = (os.environ.get("HERDR_KANBAN_AGENT") or "").strip()
    if env_agent:
        return env_agent
    if os.environ.get("ANTIGRAVITY_AGENT") or os.environ.get("ANTIGRAVITY_CONVERSATION_ID"):
        return "agy"
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_ENTRYPOINT"):
        return "claude"
    if os.environ.get("OPENCODE"):
        return "opencode"
    if os.environ.get("CODEX_SANDBOX") or os.environ.get("CODEX_CI"):
        return "codex"
    return ""


def load_user_settings() -> UserSettings:
    raw = _read_toml(user_settings_path())
    agent = str(_detect_env_agent_kind() or raw.get("default_agent_kind") or "claude").strip()
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
    """Serialize a BoardConfig as clean, commented TOML."""
    lines = [
        "# .herdr-kanban.toml — shared multi-agent Kanban configuration.",
        "",
        "[board]",
        f"project_owner = {_toml_quote(cfg.project_owner)}",
        f"project_number = {cfg.project_number}",
        f"target_branch = {_toml_quote(cfg.target_branch)}",
        f"remote = {_toml_quote(cfg.remote)}",
    ]
    if not cfg.is_multi_repo and cfg.repo_owner and cfg.repo_name:
        lines.append(f"repo_owner = {_toml_quote(cfg.repo_owner)}")
        lines.append(f"repo_name = {_toml_quote(cfg.repo_name)}")
    if cfg.default_repo:
        lines.append(f"default_repo = {_toml_quote(cfg.default_repo)}")
    if cfg.issues_repo:
        lines.append(f"issues_repo = {_toml_quote(cfg.issues_repo)}")
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
    if cfg.priority_field and cfg.priority_field != "Priority":
        lines.append(f"priority_field = {_toml_quote(cfg.priority_field)}")
    if cfg.priorities:
        prios = ", ".join(_toml_quote(p) for p in cfg.priorities)
        lines.append(f"priorities = [{prios}]")
    if cfg.require_priority_for_ready:
        lines.append("require_priority_for_ready = true")
    if cfg.milestones:
        ms = ", ".join(_toml_quote(m) for m in cfg.milestones)
        lines.append(f"milestones = [{ms}]")
    if cfg.trusted_authors:
        authors = ", ".join(_toml_quote(a) for a in cfg.trusted_authors)
        lines.append(f"trusted_authors = [{authors}]")

    lines.extend(["", "[columns]"])
    for col in Column:
        name = cfg.columns.get(col.value, COLUMN_TITLES[col])
        lines.append(f"{col.value} = {_toml_quote(name)}")

    for fname, fspec in cfg.facets.items():
        vals = ", ".join(_toml_quote(v) for v in fspec.values)
        lines.extend(
            [
                "",
                f"[taxonomy.facets.{fname}]",
                f"prefix = {_toml_quote(fspec.prefix)}",
                f"required_for_ready = {'true' if fspec.required_for_ready else 'false'}",
                f"multiple = {'true' if fspec.multiple else 'false'}",
                f"color = {_toml_quote(fspec.color)}",
            ]
        )
        if fspec.description:
            lines.append(f"description = {_toml_quote(fspec.description)}")
        lines.append(f"values = [{vals}]")

    for alias, entry in cfg.repos.items():
        lines.extend(
            [
                "",
                f"[repos.{alias}]",
                f"owner = {_toml_quote(entry.owner)}",
                f"repo = {_toml_quote(entry.repo)}",
                f"path = {_toml_quote(entry.path)}",
            ]
        )
        if entry.target_branch:
            lines.append(f"target_branch = {_toml_quote(entry.target_branch)}")
        if entry.remote:
            lines.append(f"remote = {_toml_quote(entry.remote)}")
        if entry.check_command:
            lines.append(f"check_command = {_toml_quote(entry.check_command)}")

    return "\n".join(lines) + "\n"
