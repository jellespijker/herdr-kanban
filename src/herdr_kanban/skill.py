"""Install the shared `herdr-kanban` skill, CLI links, and native `herdr-app` GPUI mod."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .config import ROOT

SKILL_NAME = "herdr-kanban"
SKILL_SRC = ROOT / "skills" / SKILL_NAME
CLI_SRC = ROOT / "bin" / "herdr-kanban"
MOD_NAME = "herdr-kanban"
MOD_SRC = ROOT / "mods" / MOD_NAME
MOD_FILES = ("main.js", "mod.json", "RECIPE.md")

ALIASES = {
    "codex": "agents",
    "opencode": "agents",
    "copilot": "agents",
    "gemini": "agents",
    "app": "herdr-app",
    "mod": "herdr-app",
    "gpui": "herdr-app",
}


def app_mod_dir(home: Path | None = None, app_home: Path | None = None) -> Path:
    if app_home is not None:
        return app_home / "mods" / MOD_NAME
    env_home = os.environ.get("HERDR_APP_HOME")
    if env_home and home is None:
        return Path(env_home) / "mods" / MOD_NAME
    base = home or Path.home()
    return base / ".config" / "herdr-app" / "mods" / MOD_NAME


def app_mod_status(home: Path | None = None, app_home: Path | None = None) -> tuple[Path, str]:
    dest = app_mod_dir(home=home, app_home=app_home)
    if dest.is_symlink():
        raw_link = os.readlink(dest)
        resolved = (dest.parent / raw_link).resolve()
        if resolved == MOD_SRC.resolve():
            return dest, "installed"
        return dest, "outdated"
    if not dest.exists():
        return dest, "missing"
    if not dest.is_dir():
        return dest, "foreign"
    all_present = all((dest / fname).is_file() for fname in MOD_FILES)
    if not all_present:
        return dest, "outdated"
    for fname in MOD_FILES:
        src_file = MOD_SRC / fname
        dst_file = dest / fname
        if src_file.is_file():
            try:
                if src_file.read_bytes() != dst_file.read_bytes():
                    return dest, "outdated"
            except OSError:
                return dest, "outdated"
    return dest, "installed"


def install_app_mod(
    *,
    home: Path | None = None,
    app_home: Path | None = None,
    uninstall: bool = False,
    machines: list[str] | None = None,
) -> list[str]:
    """Install or remove the native GPUI Kanban mod locally and optionally on remote `hc` peers."""
    dest = app_mod_dir(home=home, app_home=app_home)
    report: list[str] = []

    if uninstall:
        if dest.is_symlink():
            dest.unlink()
            report.append(f"removed  {dest}")
        elif dest.is_dir():
            shutil.rmtree(dest)
            report.append(f"removed  {dest}")
        else:
            report.append(f"skipped  {dest} (missing)")
        return report

    if not MOD_SRC.is_dir():
        raise FileNotFoundError(f"GPUI mod source directory not found: {MOD_SRC}")

    if dest.is_symlink():
        dest.unlink()
    dest.mkdir(parents=True, exist_ok=True)
    for fname in MOD_FILES:
        src_file = MOD_SRC / fname
        if src_file.is_file():
            dst_file = dest / fname
            tmp_file = dest / f".{fname}.{os.getpid()}.tmp"
            tmp_file.write_bytes(src_file.read_bytes())
            os.replace(tmp_file, dst_file)
    report.append(f"installed {dest}  (Herdr-App GPUI Kanban mod)")

    for machine in machines or []:
        m_clean = machine.strip()
        if not m_clean:
            continue
        hc_bin = shutil.which("hc") or shutil.which("herdr-core")
        if not hc_bin:
            report.append(f"skipped  {m_clean}: `hc` binary not found in PATH")
            continue
        home_proc = subprocess.run(
            [hc_bin, "exec", "--on", m_clean, "sh", "-c", 'printf %s "$HOME"'],
            capture_output=True,
            text=True,
            timeout=15,
        )
        remote_home = (
            home_proc.stdout.strip()
            if home_proc.returncode == 0 and home_proc.stdout.strip()
            else str(Path.home())
        )
        remote_dir = f"{remote_home}/.config/herdr-app/mods/{MOD_NAME}"
        subprocess.run(
            [hc_bin, "exec", "--on", m_clean, "mkdir", "-p", remote_dir],
            capture_output=True,
            text=True,
            timeout=15,
        )
        ok_all = True
        for fname in MOD_FILES:
            src_file = MOD_SRC / fname
            if not src_file.is_file():
                continue
            proc = subprocess.run(
                [hc_bin, "file", "write", "--on", m_clean, f"{remote_dir}/{fname}"],
                input=src_file.read_bytes(),
                capture_output=True,
                timeout=20,
            )
            if proc.returncode != 0:
                ok_all = False
                err = proc.stderr.decode("utf-8", errors="replace").strip()
                report.append(f"failed   {m_clean}:{remote_dir}/{fname} ({err})")
        if ok_all:
            report.append(f"synced   {m_clean}:{remote_dir}  (Herdr-App GPUI Kanban mod)")

    return report


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
    normalized = {ALIASES.get(n, n) for n in names} if names else set(all_targets)
    want_app_mod = "herdr-app" in normalized
    wanted = normalized - {"herdr-app"}
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

    base = home or Path.home()
    has_herdr_app_dir = (base / ".config" / "herdr-app").is_dir()
    if (want_app_mod or (names is None and has_herdr_app_dir)) and MOD_SRC.is_dir():
        if refresh_only:
            _, mod_state = app_mod_status(home=home)
            if mod_state == "outdated":
                report.extend(install_app_mod(home=home))
        else:
            report.extend(install_app_mod(home=home, uninstall=uninstall))

    return report
