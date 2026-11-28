from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from herdr_kanban.herdr import HerdrClient


def _ok(payload: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["mock"],
        returncode=0,
        stdout=json.dumps({"result": payload}),
        stderr="",
    )


def _raw_ok(payload: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["mock"],
        returncode=0,
        stdout=json.dumps(payload),
        stderr="",
    )


def test_spawn_prefers_herdr_app_over_legacy_when_in_hc_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    monkeypatch.setenv("HC_TERMINAL_ID", "t4t")

    legacy_calls: list[list[str]] = []
    app_calls: list[list[str]] = []
    hc_calls: list[list[str]] = []

    def legacy_runner(args, env, timeout):
        legacy_calls.append(list(args))
        return _ok({})

    def app_runner(args, env, timeout):
        app_calls.append(list(args))
        return _raw_ok({"terminal": "t99", "tab": "tb99", "space": "unimotion"})

    def hc_runner(args, env, timeout):
        hc_calls.append(list(args))
        return _raw_ok({"ok": True})

    client = HerdrClient(legacy_runner, hc_runner=hc_runner, app_runner=app_runner)
    res = client.spawn_ticket_agent(
        worktree_path=tmp_path,
        issue_number=556,
        title="Fix missing i18n keys",
        agent_kind="agy",
        prompt_text="Fix the i18n keys",
        repo_alias="unimotion",
        focus=False,
    )

    assert res["pane_id"] == "t99"
    assert legacy_calls == []
    assert any("tab.new" in call for call in app_calls)
    payload = json.loads(app_calls[0][-1])
    assert "-i" in payload["argv"]
    assert payload["task"] == "Fix the i18n keys"


def test_spawn_hc_passes_agy_interactive_prompt_inline(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    monkeypatch.setenv("HC_TERMINAL_ID", "t4t")

    hc_calls: list[list[str]] = []

    def legacy_runner(args, env, timeout):
        raise AssertionError("legacy should not be called when hc succeeds")

    def app_runner(args, env, timeout):
        return subprocess.CompletedProcess(args=["app"], returncode=1, stdout="", stderr="not running")

    def hc_runner(args, env, timeout):
        hc_calls.append(list(args))
        if args and args[0] == "get":
            return _raw_ok({"labels": {"herdr.space": "unimotion"}})
        if args and args[0] == "run":
            return _raw_ok({"address": "t88"})
        return _raw_ok({})

    client = HerdrClient(legacy_runner, hc_runner=hc_runner, app_runner=app_runner)
    res = client.spawn_ticket_agent(
        worktree_path=tmp_path,
        issue_number=557,
        title="Fix NodeShadow crash",
        agent_kind="agy",
        prompt_text="Fix NodeShadow.tsx",
        repo_alias="unimotion",
        focus=False,
    )

    assert res["pane_id"] == "t88"
    run_calls = [c for c in hc_calls if c and c[0] == "run"]
    assert len(run_calls) == 1
    assert "-i" in run_calls[0]
    assert "Fix NodeShadow.tsx" in run_calls[0]
    # Must not race with a redundant `hc send` when `-i` already passes the prompt.
    assert not any(c and c[0] == "send" for c in hc_calls)


def test_spawn_legacy_agy_waits_for_tui_and_falls_back_when_idle(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HERDR_PANE_ID", "wK:p1")
    monkeypatch.delenv("HC_TERMINAL_ID", raising=False)

    legacy_calls: list[list[str]] = []

    def legacy_runner(args, env, timeout):
        legacy_calls.append(list(args))
        if args[:2] == ["tab", "create"]:
            return _ok(
                {
                    "tab": {"tab_id": "wK:t1", "workspace_id": "wK"},
                    "root_pane": {"pane_id": "wK:p9"},
                }
            )
        if args[:2] == ["agent", "start"]:
            return _ok({"status": "started"})
        if args[:2] == ["pane", "wait-output"]:
            return _ok({"matched": True})
        if args[:2] == ["agent", "prompt"]:
            return _ok({"sent": True})
        if args[:2] == ["pane", "read"]:
            return _ok({"output": "> \n? for shortcuts   Gemini 4 Hydrogen"})
        return _ok({})

    client = HerdrClient(legacy_runner)
    res = client.spawn_ticket_agent(
        worktree_path=tmp_path,
        issue_number=569,
        title="Fix agy dispatch",
        agent_kind="agy",
        prompt_text="Kickoff prompt",
        repo_alias="unimotion",
        focus=False,
    )

    assert res["pane_id"] == "wK:p9"
    assert any(c[:2] == ["pane", "wait-output"] for c in legacy_calls)
    assert any(c[:2] == ["pane", "send-text"] and "Kickoff prompt" in c for c in legacy_calls)
    assert any(c[:2] == ["pane", "send-keys"] and "enter" in c for c in legacy_calls)
