"""Manage the `herdr-kanban` keybinding block in `~/.config/herdr/config.toml`."""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

DEFAULT_KEY = "prefix+shift+k"
BEGIN = "# >>> herdr-kanban keys (managed by `herdr-kanban setup-keys`) >>>"
END = "# <<< herdr-kanban keys <<<"
ACTION = "herdr-kanban.open"

BUILTIN = {
    "prefix+b",
    "prefix+c",
    "prefix+d",
    "prefix+e",
    "prefix+g",
    "prefix+h",
    "prefix+j",
    "prefix+k",
    "prefix+l",
    "prefix+n",
    "prefix+o",
    "prefix+p",
    "prefix+q",
    "prefix+r",
    "prefix+s",
    "prefix+v",
    "prefix+w",
    "prefix+x",
    "prefix+z",
    "prefix+minus",
    "prefix+tab",
    "prefix+alt+g",
    "prefix+ctrl+k",
    "prefix+shift+d",
    "prefix+shift+g",
    "prefix+shift+n",
    "prefix+shift+p",
    "prefix+shift+r",
    "prefix+shift+t",
    "prefix+shift+tab",
    "prefix+shift+w",
    "prefix+shift+x",
}
SUGGESTIONS = ("prefix+shift+k", "prefix+m", "prefix+u", "prefix+y", "prefix+shift+b")


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    path = Path(base) / "herdr" / "config.toml"
    return path.resolve() if path.is_symlink() else path


def _strip_block(text: str) -> str:
    return re.sub(rf"\n?{re.escape(BEGIN)}.*?{re.escape(END)}\n?", "\n", text, flags=re.DOTALL)


def used_keys(text: str) -> dict[str, str]:
    try:
        data = tomllib.loads(_strip_block(text))
    except tomllib.TOMLDecodeError:
        return {}
    used: dict[str, str] = {}
    keys = data.get("keys") or {}
    for name, value in keys.items():
        for entry in value if isinstance(value, list) else [value]:
            if isinstance(entry, str) and entry:
                used[entry.lower()] = f"herdr keys.{name}"
    commands = keys.get("command", [])
    if isinstance(commands, list):
        for entry in commands:
            if isinstance(entry, dict) and isinstance(entry.get("key"), str):
                used[entry["key"].lower()] = str(
                    entry.get("description") or entry.get("command") or "command"
                )
    return used


def conflict(key: str, text: str) -> str | None:
    lowered = key.lower()
    used = used_keys(text)
    if lowered in used:
        return used[lowered]
    if lowered in BUILTIN:
        return "a built-in herdr binding"
    return None


def free_suggestion(text: str) -> str | None:
    return next((k for k in SUGGESTIONS if not conflict(k, text)), None)


def block(key: str) -> str:
    return (
        f"{BEGIN}\n"
        "[[keys.command]]\n"
        f'key = "{key}"\n'
        'type = "plugin_action"\n'
        f'command = "{ACTION}"\n'
        'description = "Kanban: open board tab"\n'
        f"{END}\n"
    )


def installed_key(text: str) -> str | None:
    match = re.search(rf'{re.escape(BEGIN)}.*?key = "([^"]+)".*?{re.escape(END)}', text, re.DOTALL)
    return match.group(1) if match else None


def setup(key: str | None = None, *, remove: bool = False, path: Path | None = None) -> tuple[bool, str]:
    target = path or config_path()
    text = target.read_text(encoding="utf-8") if target.is_file() else ""
    current = installed_key(text)

    if remove:
        if current is None:
            return False, f"no herdr-kanban keybinding in {target}"
        new = _strip_block(text).rstrip("\n") + "\n"
        message = f"removed the {current} keybinding from {target}"
    else:
        chosen = (key or DEFAULT_KEY).lower()
        if current == chosen:
            return False, f"{chosen} already opens the Kanban board ({target})"
        taken = conflict(chosen, text)
        if taken:
            hint = free_suggestion(text)
            suffix = f"Try: herdr-kanban setup-keys --key {hint}" if hint else "Pick another key."
            return False, f"{chosen} is already used by {taken}. {suffix}"
        stripped = _strip_block(text).rstrip("\n")
        new = (stripped + "\n\n" if stripped else "") + block(chosen)
        message = f"{chosen} now opens the Kanban board (added to {target})"

    try:
        tomllib.loads(new)
    except tomllib.TOMLDecodeError as exc:
        return False, f"refusing to write invalid config.toml: {exc}"

    if target.is_file():
        backup = target.with_name(target.name + ".bak-herdr-kanban")
        backup.write_text(text, encoding="utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    tmp.write_text(new, encoding="utf-8")
    if target.is_file():
        os.chmod(tmp, target.stat().st_mode & 0o777)
    os.replace(tmp, target)
    return True, message
