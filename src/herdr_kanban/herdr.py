"""Isolated wrapper around the `herdr` CLI (plugin socket API)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

PLUGIN_ID = "herdr-kanban"

HerdrRunner = Callable[[list[str], dict[str, str] | None, float], subprocess.CompletedProcess[str]]


def binary() -> str | None:
    explicit = os.environ.get("HERDR_BIN_PATH")
    if explicit:
        return explicit if os.access(explicit, os.X_OK) else None
    return shutil.which("herdr")


def default_herdr_runner(
    args: list[str],
    env: dict[str, str] | None = None,
    timeout: float = 15.0,
) -> subprocess.CompletedProcess[str]:
    exe = binary()
    if not exe:
        raise FileNotFoundError("herdr binary not available")
    return subprocess.run(
        [exe, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def sanitize_agent_name(raw: str) -> str:
    """Conform to Herdr's agent naming rule: `[a-z][a-z0-9_-]{0,31}`."""
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", raw.lower()).strip("-")
    if not cleaned or not cleaned[0].isalpha():
        cleaned = f"k-{cleaned}"
    return cleaned[:32].rstrip("-") or "kanban-agent"


class HerdrClient:
    def __init__(self, runner: HerdrRunner = default_herdr_runner) -> None:
        self._run = runner

    def available(self) -> bool:
        return binary() is not None

    def call(
        self,
        *args: str,
        timeout: float = 15.0,
        socket: str | None = None,
        machine: str | None = None,
    ) -> dict[str, Any] | None:
        env = None
        if socket:
            env = {
                k: v
                for k, v in os.environ.items()
                if k not in ("HERDR_SESSION", "HERDR_PANE_ID", "HERDR_TAB_ID", "HERDR_WORKSPACE_ID")
            }
            env["HERDR_SOCKET_PATH"] = socket
        cmd = ["--machine", machine, *args] if machine else list(args)
        try:
            proc = self._run(cmd, env, timeout)
        except (OSError, subprocess.TimeoutExpired):
            return None
        for stream in (proc.stdout, proc.stderr):
            if not stream:
                continue
            try:
                data = json.loads(stream)
            except ValueError:
                continue
            if isinstance(data, dict) and "result" in data:
                res = data["result"]
                return res if isinstance(res, dict) else {"value": res}
        return None

    def current_pane_cwd(self) -> Path | None:
        pane_id = os.environ.get("HERDR_PANE_ID")
        args = ["pane", "get", pane_id] if pane_id else ["pane", "current"]
        res = self.call(*args)
        pane = (res or {}).get("pane") or {}
        cwd = pane.get("cwd")
        return Path(cwd) if isinstance(cwd, str) and cwd else None

    def pane_get(self, pane_id: str) -> dict[str, Any] | None:
        res = self.call("pane", "get", pane_id)
        pane = (res or {}).get("pane")
        return pane if isinstance(pane, dict) else None

    def pane_list(self, workspace_id: str | None = None) -> list[dict[str, Any]]:
        args = ["pane", "list"]
        if workspace_id:
            args.extend(["--workspace", workspace_id])
        res = self.call(*args)
        panes = (res or {}).get("panes")
        return [p for p in panes if isinstance(p, dict)] if isinstance(panes, list) else []

    def agent_get(self, target: str, machine: str | None = None) -> dict[str, Any] | None:
        res = self.call("agent", "get", target, machine=machine)
        agent = (res or {}).get("agent")
        return agent if isinstance(agent, dict) else None

    def spawn_ticket_agent(
        self,
        *,
        worktree_path: Path,
        issue_number: int,
        title: str,
        agent_kind: str,
        prompt_text: str,
        repo_alias: str = "",
        workspace_id: str | None = None,
        focus: bool = True,
    ) -> dict[str, str]:
        """Create a new Herdr tab in `worktree_path`, start `agent_kind`, and send initial prompt."""
        ws = workspace_id or os.environ.get("HERDR_WORKSPACE_ID")
        short_title = title[:28].strip()
        ref_txt = f"{repo_alias}#{issue_number}" if repo_alias else f"#{issue_number}"
        label = f"{ref_txt} {short_title}"
        tab_args = ["tab", "create", "--cwd", str(worktree_path), "--label", label]
        if ws:
            tab_args.extend(["--workspace", ws])
        tab_args.append("--focus" if focus else "--no-focus")

        tab_res = self.call(*tab_args, timeout=15.0)
        if not tab_res:
            raise RuntimeError("herdr tab create failed (ensure you are running inside Herdr)")
        tab_info = tab_res.get("tab") or {}
        pane_info = tab_res.get("root_pane") or {}
        pane_id = str(pane_info.get("pane_id") or "")
        tab_id = str(tab_info.get("tab_id") or "")
        resolved_ws = str(tab_info.get("workspace_id") or ws or "")
        if not pane_id:
            raise RuntimeError("herdr tab create did not return a root_pane ID")

        base_agent = f"{repo_alias}-{issue_number}" if repo_alias else f"issue-{issue_number}"
        agent_name = sanitize_agent_name(base_agent)
        started = self.call(
            "agent",
            "start",
            agent_name,
            "--kind",
            agent_kind,
            "--pane",
            pane_id,
            "--timeout",
            "45000",
            timeout=50.0,
        )
        if started is None:
            # Fallback to unique name if `issue-<N>` was already live in another workspace
            agent_name = sanitize_agent_name(f"{base_agent}-{pane_id.replace(':', '-')}")
            started = self.call(
                "agent",
                "start",
                agent_name,
                "--kind",
                agent_kind,
                "--pane",
                pane_id,
                "--timeout",
                "45000",
                timeout=50.0,
            )
            if started is None:
                raise RuntimeError(f"failed to start `{agent_kind}` agent in pane {pane_id}")

        self.call("agent", "prompt", agent_name, prompt_text, timeout=20.0)
        return {
            "workspace_id": resolved_ws,
            "tab_id": tab_id,
            "pane_id": pane_id,
            "agent_name": agent_name,
        }

    def prompt_if_idle(self, target: str, message: str, machine: str | None = None) -> bool:
        """Deliver `message` immediately only when target agent is `idle` or `done`."""
        if not target:
            return False
        info = self.agent_get(target, machine=machine)
        if not info:
            return False
        state = str(info.get("state") or info.get("status") or "").lower()
        if state not in ("idle", "done"):
            return False
        res = self.call("agent", "prompt", target, message, machine=machine, timeout=15.0)
        return res is not None

    def focus_pane(self, pane_id: str) -> bool:
        return self.call("pane", "focus", pane_id) is not None

    def open_board_pane(
        self,
        *,
        placement: str = "tab",
        cwd: Path | None = None,
        target_pane: str | None = None,
        focus: bool = True,
    ) -> dict[str, Any] | None:
        entrypoint = {
            "tab": "board",
            "split": "board-split",
            "popup": "board-popup",
            "overlay": "board-popup",
        }.get(placement, "board")

        if entrypoint == "board-popup":
            return self.call("plugin", "pane", "open", "--plugin", PLUGIN_ID, "--entrypoint", entrypoint)

        args = ["plugin", "pane", "open", "--plugin", PLUGIN_ID, "--entrypoint", entrypoint]
        if placement in ("tab", "split"):
            args.extend(["--placement", placement])
        if placement == "split":
            pane = target_pane or os.environ.get("HERDR_PANE_ID")
            if pane:
                args.extend(["--target-pane", pane, "--direction", "right"])
        ws = os.environ.get("HERDR_WORKSPACE_ID")
        if ws and placement == "tab":
            args.extend(["--workspace", ws])
        if cwd:
            args.extend(["--cwd", str(cwd)])
        args.append("--focus" if focus else "--no-focus")
        return self.call(*args)

    def notify(self, title: str, body: str = "") -> None:
        args = ["notification", "show", title]
        if body:
            args.extend(["--body", body])
        self.call(*args, timeout=5.0)

    def running_sessions(self) -> list[dict[str, Any]]:
        exe = binary()
        if not exe:
            return []
        try:
            proc = self._run(["session", "list", "--json"], None, 10.0)
            data = json.loads(proc.stdout)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return []
        sessions = data.get("sessions") or (data.get("result") or {}).get("sessions") or []
        return [s for s in sessions if isinstance(s, dict) and s.get("running") and s.get("socket_path")]

    def reload_config_everywhere(self) -> tuple[list[str], list[str]]:
        reloaded: list[str] = []
        failed: list[str] = []
        sessions = self.running_sessions()
        if not sessions:
            ok = self.call("server", "reload-config") is not None
            return (["current"], []) if ok else ([], ["current"])
        for session in sessions:
            ok = self.call("server", "reload-config", socket=str(session["socket_path"])) is not None
            (reloaded if ok else failed).append(str(session.get("name") or "?"))
        return reloaded, failed
