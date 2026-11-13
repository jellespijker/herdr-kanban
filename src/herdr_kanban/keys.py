"""Manage `herdr-kanban` keybindings in `~/.config/herdr/config.toml` and `~/.config/herdr-app/keys.json`."""

from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path
from typing import Any

DEFAULT_KEY = "prefix+m"
DEFAULT_APP_KEY = "ctrl-shift-m"
APP_ACTION: dict[str, Any] = {
    "command": "pane.split",
    "args": {"direction": "right", "mod": "herdr-kanban"},
}
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
SUGGESTIONS = ("prefix+m", "prefix+u", "prefix+y", "prefix+shift+b", "prefix+shift+k")


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


def app_keys_path(home: Path | None = None) -> Path:
    env_home = os.environ.get("HERDR_APP_HOME")
    if env_home and home is None:
        path = Path(env_home) / "keys.json"
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        base = (home / ".config") if home else Path(xdg)
        path = base / "herdr-app" / "keys.json"
    return path.resolve() if path.is_symlink() else path


def _is_kanban_app_action(value: Any) -> bool:
    if isinstance(value, str):
        return value in (
            "mod.open:herdr-kanban",
            "tab.new:herdr-kanban",
            "pane.split:herdr-kanban",
        )
    if isinstance(value, dict):
        cmd = value.get("command") or value.get("action")
        args = value.get("args") if isinstance(value.get("args"), dict) else value
        if cmd == "mod.open" and args.get("name") == "herdr-kanban":
            return True
        if cmd in ("tab.new", "pane.split") and args.get("mod") == "herdr-kanban":
            return True
        if cmd == "layout.toggle" and args.get("id") in ("herdr-kanban", "kanban-panel"):
            return True
    return False


def installed_app_key(data: dict[str, Any]) -> str | None:
    global_sec = data.get("global")
    if isinstance(global_sec, dict):
        for chord, action in global_sec.items():
            if _is_kanban_app_action(action):
                return chord
    for chord, action in data.items():
        if _is_kanban_app_action(action):
            return chord
    return None


def setup_app_keys(
    key: str | None = None,
    *,
    remove: bool = False,
    path: Path | None = None,
    action: str | dict[str, Any] = APP_ACTION,
) -> tuple[bool, str]:
    target = path or app_keys_path()
    raw = target.read_text(encoding="utf-8") if target.is_file() else "{}"
    try:
        loaded = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        return False, f"refusing to modify invalid keys.json ({target}): {exc}"
    if not isinstance(loaded, dict):
        return False, f"refusing to modify non-object keys.json ({target})"

    data: dict[str, Any] = dict(loaded)
    global_sec: dict[str, Any] = (
        dict(data["global"]) if isinstance(data.get("global"), dict) else {}
    )
    current = installed_app_key(data)

    if remove:
        if current is None:
            return False, f"no herdr-kanban keybinding in {target}"
        for k in [k for k, v in global_sec.items() if _is_kanban_app_action(v)]:
            global_sec.pop(k, None)
        for k in [k for k, v in data.items() if _is_kanban_app_action(v)]:
            data.pop(k, None)
        if isinstance(data.get("mod:herdr-kanban"), dict):
            mod_sec = dict(data["mod:herdr-kanban"])
            mod_sec.pop(current, None)
            if mod_sec:
                data["mod:herdr-kanban"] = mod_sec
            else:
                data.pop("mod:herdr-kanban", None)
        if global_sec or "global" in data:
            data["global"] = global_sec
        message = f"removed the {current} keybinding from {target}"
    else:
        chosen = (key or DEFAULT_APP_KEY).strip().lower()
        if current == chosen and global_sec.get(chosen) == action:
            return False, f"{chosen} already opens the Kanban board ({target})"
        existing = global_sec.get(chosen)
        if (
            existing is None
            and chosen not in ("global", "terminal")
            and not chosen.startswith(("mod:", "layout:"))
        ):
            existing = data.get(chosen)
        if existing is not None and not _is_kanban_app_action(existing):
            return (
                False,
                f"{chosen} is already bound to {existing!r} in {target}. "
                "Pick another chord with `herdr-kanban setup-keys --app --key <chord>`.",
            )
        for k in [k for k, v in global_sec.items() if _is_kanban_app_action(v)]:
            global_sec.pop(k, None)
        for k in [k for k, v in data.items() if _is_kanban_app_action(v)]:
            data.pop(k, None)
        global_sec[chosen] = action
        data["global"] = global_sec
        mod_sec = dict(data["mod:herdr-kanban"]) if isinstance(data.get("mod:herdr-kanban"), dict) else {}
        mod_sec[chosen] = "pane.close"
        data["mod:herdr-kanban"] = mod_sec
        message = f"{chosen} now toggles the docked Kanban board in herdr-app ({target})"

    rendered = json.dumps(data, indent=2) + "\n"
    if target.is_file():
        backup = target.with_name(target.name + ".bak-herdr-kanban")
        backup.write_text(raw, encoding="utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    tmp.write_text(rendered, encoding="utf-8")
    if target.is_file():
        os.chmod(tmp, target.stat().st_mode & 0o777)
    os.replace(tmp, target)
    return True, message
