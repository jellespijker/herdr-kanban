"""Isolated wrapper around `herdr` (0.9.x), `herdr-core` (`hc`), and `herdr-app` CLIs."""

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

AGENT_ARGV_MAP: dict[str, list[str]] = {
    "claude": ["claude"],
    "agy": ["agy"],
    "codex": ["codex"],
    "opencode": ["opencode"],
    "gemini": ["gemini"],
    "copilot": ["copilot"],
    "pi": ["pi"],
    "amp": ["amp"],
    "droid": ["droid"],
}


def binary() -> str | None:
    """Return path to legacy `herdr` 0.9.x binary if installed."""
    explicit = os.environ.get("HERDR_BIN_PATH")
    if explicit:
        return explicit if os.access(explicit, os.X_OK) else None
    return shutil.which("herdr")


def hc_binary() -> str | None:
    """Return path to `hc` (`herdr-core`) binary if installed."""
    explicit = os.environ.get("HC_BIN_PATH")
    if explicit:
        return explicit if os.access(explicit, os.X_OK) else None
    return shutil.which("hc")


def app_binary() -> str | None:
    """Return path to `herdr-app` binary if installed."""
    explicit = os.environ.get("HERDR_APP_BIN_PATH")
    if explicit:
        return explicit if os.access(explicit, os.X_OK) else None
    return shutil.which("herdr-app")


def any_binary() -> str | None:
    """Return the first available Herdr binary (`herdr`, `hc`, or `herdr-app`)."""
    return binary() or hc_binary() or app_binary()


def current_pane_id() -> str:
    """Return the active pane or core terminal identifier from the environment."""
    return os.environ.get("HERDR_PANE_ID") or os.environ.get("HC_TERMINAL_ID") or ""


def current_socket_path() -> str:
    """Return the active Herdr or herdr-core socket path from the environment."""
    return os.environ.get("HERDR_SOCKET_PATH") or os.environ.get("HC_TERMINAL_SOCKET") or ""


def current_scope() -> str:
    """Return the active herdr-core scope (`HC_SCOPE`) or legacy session name."""
    return os.environ.get("HC_SCOPE") or os.environ.get("HERDR_SESSION") or ""


def in_herdr_env() -> bool:
    """Return True when running inside Herdr 0.9.x or herdr-core (`hc`) / `herdr-app`."""
    return bool(
        os.environ.get("HERDR_ENV")
        or os.environ.get("HERDR_PANE_ID")
        or os.environ.get("HERDR_SOCKET_PATH")
        or os.environ.get("HC_TERMINAL_ID")
        or os.environ.get("HC_TERMINAL_SOCKET")
        or os.environ.get("HC_SCOPE")
    )


def app_home_dir(home: Path | None = None) -> Path:
    """Return the `herdr-app` configuration directory (`~/.config/herdr-app` or `$HERDR_APP_HOME`)."""
    explicit = os.environ.get("HERDR_APP_HOME")
    if explicit:
        return Path(explicit).expanduser()
    base = home or Path.home()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg and home is None:
        return Path(xdg) / "herdr-app"
    return base / ".config" / "herdr-app"


def resolve_agent_argv(agent_kind: str, home: Path | None = None) -> list[str]:
    """Resolve the command argv used to start `agent_kind` in `herdr-app` or `hc`."""
    kind = agent_kind.strip() or "claude"
    progs_file = app_home_dir(home) / "programs.json"
    if progs_file.is_file():
        try:
            raw = json.loads(progs_file.read_text(encoding="utf-8"))
            programs = raw.get("programs") if isinstance(raw, dict) else None
            entry = programs.get(kind) if isinstance(programs, dict) else None
            run_cmd = entry.get("run") if isinstance(entry, dict) else None
            if isinstance(run_cmd, list) and run_cmd and all(isinstance(x, str) for x in run_cmd):
                return list(run_cmd)
        except (OSError, ValueError):
            pass
    return list(AGENT_ARGV_MAP.get(kind, [kind]))


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


def default_hc_runner(
    args: list[str],
    env: dict[str, str] | None = None,
    timeout: float = 15.0,
) -> subprocess.CompletedProcess[str]:
    exe = hc_binary()
    if not exe:
        raise FileNotFoundError("hc binary not available")
    return subprocess.run(
        [exe, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def default_app_runner(
    args: list[str],
    env: dict[str, str] | None = None,
    timeout: float = 15.0,
) -> subprocess.CompletedProcess[str]:
    exe = app_binary()
    if not exe:
        raise FileNotFoundError("herdr-app binary not available")
    return subprocess.run(
        [exe, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def sanitize_agent_name(raw: str) -> str:
    """Conform to Herdr's agent/terminal naming rule: `[a-z][a-z0-9_-]{0,31}`."""
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", raw.lower()).strip("-")
    if not cleaned or not cleaned[0].isalpha():
        cleaned = f"k-{cleaned}"
    return cleaned[:32].rstrip("-") or "kanban-agent"


class HerdrClient:
    def __init__(
        self,
        runner: HerdrRunner = default_herdr_runner,
        *,
        hc_runner: HerdrRunner = default_hc_runner,
        app_runner: HerdrRunner = default_app_runner,
    ) -> None:
        self._run = runner
        self._hc_run = hc_runner
        self._app_run = app_runner
        self._custom_legacy = runner is not default_herdr_runner
        self._custom_hc = hc_runner is not default_hc_runner
        self._custom_app = app_runner is not default_app_runner

    def legacy_available(self) -> bool:
        return self._custom_legacy or binary() is not None

    def hc_available(self) -> bool:
        return self._custom_hc or hc_binary() is not None

    def app_available(self) -> bool:
        return self._custom_app or app_binary() is not None

    def available(self) -> bool:
        return self.legacy_available() or self.hc_available() or self.app_available()

    def server_alive(self) -> bool:
        """Return True if either legacy `herdr` server or `herdr-core` (`hc`) responds."""
        if self.legacy_available() and self.call("pane", "list") is not None:
            return True
        return bool(self.hc_available() and self.hc_call("ls") is not None)

    def call(
        self,
        *args: str,
        timeout: float = 15.0,
        socket: str | None = None,
        machine: str | None = None,
    ) -> dict[str, Any] | None:
        if not self.legacy_available():
            return None
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

    def hc_call(
        self,
        *args: str,
        timeout: float = 15.0,
        socket: str | None = None,
    ) -> dict[str, Any] | None:
        """Invoke `hc` (`herdr-core`) with `--json` and return the parsed JSON object."""
        if not self.hc_available():
            return None
        env = None
        if socket:
            env = dict(os.environ)
            env["HC_TERMINAL_SOCKET"] = socket
        cmd = list(args)
        if "--json" not in cmd:
            if "--" in cmd:
                sep_idx = cmd.index("--")
                cmd.insert(sep_idx, "--json")
            else:
                cmd.append("--json")
        try:
            proc = self._hc_run(cmd, env, timeout)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0:
            return None
        for stream in (proc.stdout, proc.stderr):
            if not stream or not stream.strip():
                continue
            try:
                data = json.loads(stream)
            except ValueError:
                continue
            if isinstance(data, dict):
                if "error" in data and len(data) == 1:
                    return None
                return data
        return {} if proc.returncode == 0 else None

    def connected_hc_machines(self, timeout: float = 5.0) -> list[str]:
        """Return names of connected peer machines reported by `hc ls --json`."""
        if not self.hc_available():
            return []
        res = self.hc_call("ls", timeout=timeout)
        machines = (res or {}).get("machines")
        if not isinstance(machines, list):
            return []
        return [
            str(m["name"])
            for m in machines
            if isinstance(m, dict) and m.get("state") == "connected" and m.get("name")
        ]

    def _run_app_raw(
        self,
        args: list[str],
        *,
        machine: str | None = None,
        timeout: float = 10.0,
    ) -> subprocess.CompletedProcess[str] | None:
        if machine:
            if not self.hc_available():
                return None
            try:
                return self._hc_run(["exec", "--on", machine, "herdr-app", *args], None, timeout)
            except (OSError, subprocess.TimeoutExpired):
                return None
        if not self.app_available():
            return None
        try:
            return self._app_run(args, None, timeout)
        except (OSError, subprocess.TimeoutExpired):
            return None

    def _app_candidate_machines(self) -> list[str | None]:
        candidates: list[str | None] = [None]
        if not self._custom_app:
            candidates.extend(self.connected_hc_machines())
        return candidates

    def app_do(
        self,
        command: str,
        payload: dict[str, Any] | None = None,
        *,
        machine: str | None = None,
        timeout: float = 10.0,
    ) -> dict[str, Any] | None:
        """Run a `herdr-app do <command> [json_payload]` command locally or on a connected peer."""
        clean_payload = (
            {k: v for k, v in payload.items() if v is not None}
            if payload
            else None
        )
        cmd = ["do", command]
        if clean_payload:
            cmd.append(json.dumps(clean_payload))
        machines = [machine] if machine is not None else self._app_candidate_machines()
        for cand in machines:
            proc = self._run_app_raw(cmd, machine=cand, timeout=timeout)
            if proc is None or proc.returncode != 0:
                continue
            out = (proc.stdout or "").strip()
            if not out:
                return {"ok": True, "machine": cand}
            try:
                parsed = json.loads(out)
            except ValueError:
                return {"ok": True, "raw": out, "machine": cand}
            if isinstance(parsed, dict):
                if "error" in parsed:
                    continue
                if cand and "machine" not in parsed:
                    parsed = {**parsed, "machine": cand}
                return parsed
            return {"value": parsed, "machine": cand}
        return None

    def app_status(
        self,
        *,
        machine: str | None = None,
        timeout: float = 5.0,
    ) -> dict[str, Any] | None:
        """Query `herdr-app status` locally or on a connected peer machine."""
        machines = [machine] if machine is not None else self._app_candidate_machines()
        for cand in machines:
            proc = self._run_app_raw(["status"], machine=cand, timeout=timeout)
            if proc is None or proc.returncode != 0 or not proc.stdout:
                continue
            try:
                data = json.loads(proc.stdout)
            except ValueError:
                continue
            if isinstance(data, dict):
                if cand and "app_machine" not in data:
                    data = {**data, "app_machine": cand}
                return data
        return None

    def app_ls(
        self,
        *,
        machine: str | None = None,
        timeout: float = 5.0,
    ) -> tuple[dict[str, Any], str | None] | None:
        """Query `herdr-app ls` locally or on a connected peer machine, returning `(data, machine)`."""
        machines = [machine] if machine is not None else self._app_candidate_machines()
        for cand in machines:
            proc = self._run_app_raw(["ls"], machine=cand, timeout=timeout)
            if proc is None or proc.returncode != 0 or not proc.stdout:
                continue
            try:
                data = json.loads(proc.stdout)
            except ValueError:
                continue
            if isinstance(data, dict) and "spaces" in data:
                return data, cand
        return None

    def current_pane_cwd(self) -> Path | None:
        if self.legacy_available():
            pane_id = os.environ.get("HERDR_PANE_ID")
            args = ["pane", "get", pane_id] if pane_id else ["pane", "current"]
            res = self.call(*args)
            pane = (res or {}).get("pane") or {}
            cwd = pane.get("cwd")
            if isinstance(cwd, str) and cwd:
                return Path(cwd)

        term_id = os.environ.get("HC_TERMINAL_ID") or os.environ.get("HERDR_PANE_ID")
        if term_id and self.hc_available():
            info = self.hc_call("get", term_id)
            if isinstance(info, dict):
                fg = info.get("foreground")
                cwd = (fg.get("cwd") if isinstance(fg, dict) else None) or info.get("cwd")
                if isinstance(cwd, str) and cwd:
                    return Path(cwd)

        st = self.app_status()
        if isinstance(st, dict) and self.hc_available():
            agent_addr = st.get("agent")
            if isinstance(agent_addr, str) and agent_addr:
                local_addr = agent_addr.split("/", 1)[-1]
                info = self.hc_call("get", local_addr)
                if isinstance(info, dict):
                    fg = info.get("foreground")
                    cwd = (fg.get("cwd") if isinstance(fg, dict) else None) or info.get("cwd")
                    if isinstance(cwd, str) and cwd:
                        return Path(cwd)
        return None

    def pane_get(self, pane_id: str) -> dict[str, Any] | None:
        if self.legacy_available():
            res = self.call("pane", "get", pane_id)
            pane = (res or {}).get("pane")
            if isinstance(pane, dict):
                return pane
        if self.hc_available():
            info = self.hc_call("get", pane_id)
            if isinstance(info, dict) and info:
                fg = info.get("foreground")
                cwd = (fg.get("cwd") if isinstance(fg, dict) else None) or info.get("cwd")
                return {
                    "pane_id": str(info.get("address") or pane_id),
                    "address": str(info.get("address") or pane_id),
                    "cwd": cwd,
                    "state": info.get("state"),
                    "status": info.get("status"),
                    "labels": info.get("labels") or {},
                    "name": info.get("name"),
                }
        return None

    def pane_list(self, workspace_id: str | None = None) -> list[dict[str, Any]]:
        if self.legacy_available():
            args = ["pane", "list"]
            if workspace_id:
                args.extend(["--workspace", workspace_id])
            res = self.call(*args)
            panes = (res or {}).get("panes")
            if isinstance(panes, list):
                return [p for p in panes if isinstance(p, dict)]

        if self.hc_available():
            ls_res = self.hc_call("ls")
            terms = (ls_res or {}).get("terminals")
            if isinstance(terms, list):
                out: list[dict[str, Any]] = []
                for t in terms:
                    if not isinstance(t, dict):
                        continue
                    labels = t.get("labels") if isinstance(t.get("labels"), dict) else {}
                    if workspace_id and labels and labels.get("herdr.space") not in (None, workspace_id):
                        continue
                    out.append(
                        {
                            "pane_id": str(t.get("address") or ""),
                            "address": str(t.get("address") or ""),
                            "cwd": t.get("cwd"),
                            "state": t.get("state"),
                            "status": t.get("status"),
                            "agent": t.get("agent"),
                            "name": t.get("name"),
                            "parent": t.get("parent"),
                        }
                    )
                return out
        return []

    @staticmethod
    def _normalize_hc_agent(info: dict[str, Any], default_address: str = "") -> dict[str, Any]:
        status_raw = info.get("status")
        if isinstance(status_raw, dict):
            state_str = str(status_raw.get("state") or "idle").lower()
            app_str = str(status_raw.get("app") or info.get("agent") or "")
        elif isinstance(status_raw, str) and status_raw:
            state_str = status_raw.lower()
            app_str = str(info.get("agent") or "")
        else:
            state_str = "idle"
            app_str = str(info.get("agent") or "")
        addr = str(info.get("address") or default_address)
        return {
            **info,
            "address": addr,
            "pane_id": addr,
            "state": state_str,
            "status": state_str,
            "app": app_str,
        }

    def agent_get(self, target: str, machine: str | None = None) -> dict[str, Any] | None:
        if not target:
            return None
        if self.legacy_available():
            res = self.call("agent", "get", target, machine=machine)
            agent = (res or {}).get("agent")
            if isinstance(agent, dict):
                return agent

        if self.hc_available():
            qualified = (
                f"{machine}/{target}"
                if machine and not target.startswith(f"{machine}/")
                else target
            )
            info = self.hc_call("get", qualified)
            if isinstance(info, dict) and info.get("state") == "live":
                return self._normalize_hc_agent(info, qualified)

            ls_res = self.hc_call("ls")
            terms = (ls_res or {}).get("terminals")
            if isinstance(terms, list):
                for t in terms:
                    if not isinstance(t, dict) or t.get("state") != "live":
                        continue
                    addr = str(t.get("address") or "")
                    name = str(t.get("name") or "")
                    if qualified in (addr, name) or (not machine and target in (addr, name)):
                        return self._normalize_hc_agent(t, addr)
        return None

    def _resolve_hc_space_and_tab(self) -> tuple[str, str]:
        """Find the active `herdr.space` and `herdr.tab` labels from `HC_TERMINAL_ID` (walking `parent`)."""
        cur = os.environ.get("HC_TERMINAL_ID") or ""
        visited: set[str] = set()
        while cur and cur not in visited and len(visited) < 4:
            visited.add(cur)
            info = self.hc_call("get", cur)
            if not isinstance(info, dict):
                break
            labels = info.get("labels")
            if isinstance(labels, dict):
                sp = str(labels.get("herdr.space") or "")
                tb = str(labels.get("herdr.tab") or "")
                if sp:
                    return sp, tb
            parent = info.get("parent")
            cur = str(parent) if isinstance(parent, str) and parent else ""

        st = self.app_status()
        if isinstance(st, dict):
            sp = str(st.get("space") or "")
            tb = str(st.get("tab") or "")
            if sp:
                return sp, tb
        return "", ""

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
        machine: str | None = None,
    ) -> dict[str, str]:
        """Create a new Herdr tab/terminal in `worktree_path`, start `agent_kind`, and send initial prompt."""
        ws = workspace_id or os.environ.get("HERDR_WORKSPACE_ID")
        short_title = title[:28].strip()
        ref_txt = f"{repo_alias}#{issue_number}" if repo_alias else f"#{issue_number}"
        label = f"{ref_txt} {short_title}"
        base_agent = f"{repo_alias}-{issue_number}" if repo_alias else f"issue-{issue_number}"
        agent_name = sanitize_agent_name(base_agent)

        # 1. Legacy Herdr 0.9.x socket API
        if self.legacy_available() and not machine:
            tab_args = ["tab", "create", "--cwd", str(worktree_path), "--label", label]
            if ws:
                tab_args.extend(["--workspace", ws])
            tab_args.append("--focus" if focus else "--no-focus")

            tab_res = self.call(*tab_args, timeout=15.0)
            if tab_res:
                tab_info = tab_res.get("tab") or {}
                pane_info = tab_res.get("root_pane") or {}
                pane_id = str(pane_info.get("pane_id") or "")
                tab_id = str(tab_info.get("tab_id") or "")
                resolved_ws = str(tab_info.get("workspace_id") or ws or "")
                if not pane_id:
                    raise RuntimeError("herdr tab create did not return a root_pane ID")

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

        agent_argv = resolve_agent_argv(agent_kind)

        # 2. Native Herdr-App window (`herdr-app do tab.new`)
        if self.app_available() and not machine:
            tab_payload: dict[str, Any] = {
                "name": label,
                "folder": str(worktree_path),
                "argv": agent_argv,
                "task": prompt_text,
            }
            if ws:
                tab_payload["space"] = ws
            app_res = self.app_do("tab.new", tab_payload, timeout=15.0)
            if isinstance(app_res, dict) and (app_res.get("terminal") or app_res.get("tab")):
                term_id = str(app_res.get("terminal") or "")
                tab_id = str(app_res.get("tab") or "")
                space_id = str(app_res.get("space") or ws or "")
                if term_id and self.hc_available():
                    self.hc_call(
                        "label",
                        term_id,
                        f"herdr.kanban.issue={issue_number}",
                        f"herdr.kanban.agent={agent_name}",
                    )
                return {
                    "workspace_id": space_id,
                    "tab_id": tab_id,
                    "pane_id": term_id or tab_id,
                    "agent_name": agent_name,
                }

        # 3. Native herdr-core (`hc run` + `hc send`)
        if self.hc_available():
            hc_space, _ = self._resolve_hc_space_and_tab()
            resolved_ws = ws or hc_space
            gen_tab_id = f"tbkanban-{issue_number}-{os.getpid() % 10000}"

            def _build_hc_run(name_cand: str) -> list[str]:
                cmd = [
                    "run",
                    "--bg",
                    "--keep",
                    "--outlive-parent",
                    "--no-notify",
                    "--cwd",
                    str(worktree_path),
                    "--name",
                    name_cand,
                    "--label",
                    f"herdr.kanban.issue={issue_number}",
                    "--label",
                    f"herdr.kanban.agent={name_cand}",
                ]
                if resolved_ws:
                    local_ws = resolved_ws.split("/", 1)[-1]
                    cmd.extend(
                        [
                            "--label",
                            f"herdr.space={local_ws}",
                            "--label",
                            f"herdr.tab={gen_tab_id}",
                        ]
                    )
                if machine:
                    cmd.extend(["--on", machine])
                cmd.extend(["--", *agent_argv])
                return cmd

            hc_res = self.hc_call(*_build_hc_run(agent_name), timeout=20.0)
            if hc_res is None:
                agent_name = sanitize_agent_name(f"{base_agent}-{os.getpid() % 10000}")
                hc_res = self.hc_call(*_build_hc_run(agent_name), timeout=20.0)
            if hc_res is not None:
                term_id = str(hc_res.get("address") or agent_name)
                self.hc_call("send", term_id, prompt_text, timeout=20.0)
                if focus and self.app_available():
                    self.app_do("pane.focus", {"terminal": term_id})
                return {
                    "workspace_id": resolved_ws,
                    "tab_id": gen_tab_id,
                    "pane_id": term_id,
                    "agent_name": agent_name,
                }

        raise RuntimeError(
            "herdr tab/terminal creation failed (ensure Herdr, herdr-app, or herdr-core `hc` is running)"
        )

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
        if self.legacy_available():
            res = self.call("agent", "prompt", target, message, machine=machine, timeout=15.0)
            if res is not None:
                return True
        if self.hc_available():
            addr = str(
                info.get("address")
                or (f"{machine}/{target}" if machine and not target.startswith(f"{machine}/") else target)
            )
            sent = self.hc_call("send", addr, message, timeout=15.0)
            return sent is not None
        return False

    def focus_pane(self, pane_id: str) -> bool:
        if not pane_id:
            return False
        if self.legacy_available() and self.call("pane", "focus", pane_id) is not None:
            return True
        if self.app_available():
            if (
                pane_id.startswith("p")
                and not pane_id.startswith("pane")
                and self.app_do("pane.focus", {"pane": pane_id}) is not None
            ):
                return True
            if self.app_do("pane.focus", {"terminal": pane_id}) is not None:
                return True
            if self.app_do("agent.focus", {"terminal": pane_id}) is not None:
                return True
        return False

    def _resolve_app_target_pane(
        self, explicit_pane: str | None = None
    ) -> tuple[str | None, str | None, str | None]:
        """Return `(app_machine, pane_id, space_machine)` for the current terminal in `herdr-app`."""
        ls_info = self.app_ls()
        if not ls_info:
            return None, explicit_pane, None
        ls_data, app_machine = ls_info
        raw_local = explicit_pane or current_pane_id()
        local_id = raw_local.rsplit("/", 1)[-1] if raw_local else ""
        remote_only = bool(app_machine)

        if local_id:
            for space in ls_data.get("spaces") or []:
                if not isinstance(space, dict):
                    continue
                sp_machine = str(space.get("machine") or "") or None
                if remote_only and not sp_machine:
                    continue
                for tab in space.get("tabs") or []:
                    if not isinstance(tab, dict):
                        continue
                    for term in tab.get("terminals") or []:
                        if not isinstance(term, dict):
                            continue
                        t_addr = str(term.get("terminal") or "")
                        p_id = str(term.get("pane") or "")
                        sub_agents = [str(s) for s in (term.get("sub_agents") or [])]
                        if not p_id:
                            continue
                        if not remote_only and (
                            t_addr == local_id or p_id == raw_local or local_id in sub_agents
                        ):
                            return app_machine, p_id, sp_machine
                        if remote_only and (
                            t_addr.endswith(f"/{local_id}")
                            or p_id == raw_local
                            or any(s.endswith(f"/{local_id}") for s in sub_agents)
                        ):
                            return app_machine, p_id, sp_machine

        # Fallback to the active space's active tab's first pane
        for space in ls_data.get("spaces") or []:
            if not isinstance(space, dict) or not space.get("active"):
                continue
            sp_machine = str(space.get("machine") or "") or None
            for tab in space.get("tabs") or []:
                if not isinstance(tab, dict) or not tab.get("active"):
                    continue
                for term in tab.get("terminals") or []:
                    if isinstance(term, dict) and term.get("pane"):
                        return app_machine, str(term["pane"]), sp_machine
        return app_machine, explicit_pane, None

    def open_board_pane(
        self,
        *,
        placement: str = "split",
        cwd: Path | None = None,
        target_pane: str | None = None,
        focus: bool = True,
        use_mod: bool = True,
        prefer_app_mod: bool = True,
    ) -> dict[str, Any] | None:
        # 1. Legacy Herdr 0.9.x plugin pane
        if self.legacy_available():
            entrypoint = {
                "tab": "board",
                "split": "board-split",
                "popup": "board-popup",
                "overlay": "board-popup",
            }.get(placement, "board")

            if entrypoint == "board-popup":
                legacy_res = self.call(
                    "plugin", "pane", "open", "--plugin", PLUGIN_ID, "--entrypoint", entrypoint
                )
            else:
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
                legacy_res = self.call(*args)
            if legacy_res is not None:
                return legacy_res

        # 2. Native Herdr-App GPUI mod (docked split pane by default) or terminal tab/split
        if self.app_available() or self.hc_available():
            app_machine, resolved_pane, sp_machine = self._resolve_app_target_pane(target_pane)
            props: dict[str, Any] = {"cwd": str(cwd)} if cwd else {}
            if sp_machine:
                props["on"] = sp_machine
            if use_mod and prefer_app_mod:
                if placement in ("popup", "overlay", "window"):
                    return self.app_do("mod.open", {"name": "herdr-kanban"}, machine=app_machine)
                if placement == "tab":
                    return self.app_do(
                        "tab.new",
                        {"name": "Herdr Kanban", "mod": "herdr-kanban", "props": props},
                        machine=app_machine,
                    )
                split_args: dict[str, Any] = {
                    "direction": "right",
                    "mod": "herdr-kanban",
                    "props": props,
                }
                if resolved_pane:
                    split_args["pane"] = resolved_pane
                res = self.app_do("pane.split", split_args, machine=app_machine)
                if res is not None:
                    return res
                return self.app_do(
                    "tab.new",
                    {"name": "Herdr Kanban", "mod": "herdr-kanban", "props": props},
                    machine=app_machine,
                )

            argv = ["herdr-kanban", "tui", *(["--cwd", str(cwd)] if cwd else [])]
            if placement == "split":
                return self.app_do(
                    "pane.split", {"direction": "right", "argv": argv}, machine=app_machine
                )
            return self.app_do(
                "tab.new",
                {"name": "Herdr Kanban", "folder": str(cwd) if cwd else None, "argv": argv},
                machine=app_machine,
            )
        return None

    def notify(self, title: str, body: str = "") -> None:
        if self.legacy_available():
            args = ["notification", "show", title]
            if body:
                args.extend(["--body", body])
            self.call(*args, timeout=5.0)

    def running_sessions(self) -> list[dict[str, Any]]:
        if not self.legacy_available():
            return []
        try:
            proc = self._run(["session", "list", "--json"], None, 10.0)
            data = json.loads(proc.stdout)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return []
        sessions = data.get("sessions") or (data.get("result") or {}).get("sessions") or []
        return [s for s in sessions if isinstance(s, dict) and s.get("running") and s.get("socket_path")]

    def reload_config_everywhere(self) -> tuple[list[str], list[str]]:
        if not self.legacy_available():
            return (["herdr-app (live)"], []) if self.app_available() else ([], [])
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
