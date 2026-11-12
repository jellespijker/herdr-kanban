"""Install the shared `herdr-kanban` skill and CLI as symlinks across coding agents."""

from __future__ import annotations

import os
from pathlib import Path

from .config import ROOT

SKILL_NAME = "herdr-kanban"
SKILL_SRC = ROOT / "skills" / SKILL_NAME
CLI_SRC = ROOT / "bin" / "herdr-kanban"

ALIASES = {
    "codex": "agents",
    "opencode": "agents",
    "copilot": "agents",
    "gemini": "agents",
}


def targets(home: Path | None = None) -> dict[str, tuple[Path, str]]:
    base = home or Path.home()
    claude_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (base / ".claude"))
    return {
        "agents": (
            base / ".agents" / "skills" / SKILL_NAME,
            "Codex, OpenCode, Copilot CLI, Gemini CLI",
        ),
        "claude": (claude_dir / "skills" / SKILL_NAME, "Claude Code"),
        "agy": (base / ".gemini" / "config" / "skills" / SKILL_NAME, "Antigravity CLI (agy)"),
        "cli": (base / ".local" / "bin" / "herdr-kanban", "the herdr-kanban command"),
    }


def _source_for(name: str) -> Path:
    return CLI_SRC if name == "cli" else SKILL_SRC


def status(home: Path | None = None) -> list[tuple[str, Path, str, str]]:
    rows: list[tuple[str, Path, str, str]] = []
    for name, (dest, readers) in targets(home).items():
        source = _source_for(name)
        if dest.is_symlink():
            raw_link = os.readlink(dest)
            resolved = (dest.parent / raw_link).resolve()
            if resolved == source.resolve():
                state = "installed"
            elif SKILL_NAME in raw_link:
                state = "outdated"
            else:
                state = "other-link"
        elif dest.exists():
            state = "foreign"
        else:
            state = "missing"
        rows.append((name, dest, readers, state))
    return rows


def install(
    names: list[str] | None = None,
    *,
    uninstall: bool = False,
    refresh_only: bool = False,
    home: Path | None = None,
) -> list[str]:
    all_targets = targets(home)
    wanted = {ALIASES.get(n, n) for n in names} if names else set(all_targets)
    unknown = wanted - set(all_targets)
    if unknown:
        raise ValueError(f"unknown harness: {', '.join(sorted(unknown))}")

    report: list[str] = []
    for name, dest, readers, state in status(home):
        if name not in wanted:
            continue
        source = _source_for(name)
        if uninstall:
            if state in ("installed", "outdated"):
                dest.unlink()
                report.append(f"removed  {dest}")
            else:
                report.append(f"skipped  {dest} ({state})")
            continue
        if refresh_only:
            if state == "outdated":
                dest.unlink()
                dest.symlink_to(source)
                report.append(f"relinked {dest}  ({readers})")
            continue
        if name == "agy" and not dest.parent.parent.is_dir() and names is None:
            report.append(f"skipped  {dest} (agy not configured)")
            continue
        if state == "installed":
            report.append(f"ok       {dest}  ({readers})")
        elif state == "outdated":
            dest.unlink()
            dest.symlink_to(source)
            report.append(f"relinked {dest}  ({readers})")
        elif state == "missing":
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.symlink_to(source)
            report.append(f"linked   {dest}  ({readers})")
        else:
            report.append(f"skipped  {dest}: exists and is not managed by herdr-kanban ({state})")
    return report
