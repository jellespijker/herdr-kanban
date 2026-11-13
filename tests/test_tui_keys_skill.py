"""Tests for Kanban TUI rendering/navigation, keybinding setup, and skill links."""

from __future__ import annotations

from pathlib import Path

import pytest

from herdr_kanban import config, herdr, keys, skill, state, terminal
from herdr_kanban.engine import KanbanEngine, RepoContext
from herdr_kanban.github import ProjectSchema
from herdr_kanban.model import ClaimMeta, Column, FileOverlap, Ticket
from herdr_kanban.tui import BoardTUI


def _sample_tui() -> BoardTUI:
    engine = KanbanEngine()
    tui = BoardTUI(engine)
    board = config.BoardConfig(
        project_owner="team",
        project_number=3,
        target_branch="feat/shared",
        repo_owner="team",
        repo_name="service",
    )
    tui.ctx = RepoContext(
        worktree_cwd=Path("/tmp/service"),
        main_repo_root=Path("/tmp/service"),
        repo_owner="team",
        repo_name="service",
        slug="team__service",
        board=board,
        settings=config.UserSettings(),
    )
    tui.tickets = [
        Ticket(number=1, title="Triaged edge case", column=Column.TRIAGE),
        Ticket(number=2, title="Implement rate limiter", column=Column.READY, body="Token bucket per IP."),
        Ticket(
            number=3,
            title="Refactor middleware",
            column=Column.IN_PROGRESS,
            claim=ClaimMeta(issue_number=3, owner="alice", agent_kind="claude", touched_files=["mw.py"]),
            overlaps=[FileOverlap(other_issue=4, other_owner="bob", other_agent="agy", files=["mw.py"])],
        ),
        Ticket(number=4, title="Await review", column=Column.REVIEW),
        Ticket(number=5, title="Shipped healthcheck", column=Column.DONE, state="CLOSED"),
    ]
    return tui


def test_tui_wide_and_compact_frames_and_navigation() -> None:
    tui = _sample_tui()
    try:
        wide = tui.render_frame(140, 34)
        plain_wide = terminal.ANSI_ESCAPE_RE.sub("", wide)
        assert "team/service" in plain_wide
        assert "origin/feat/shared" in plain_wide
        assert "#2 Implement rate limiter" in plain_wide
        assert "Token bucket per IP." in plain_wide

        # Navigate right into In Progress and verify overlap alert appears in inspector
        assert tui.handle_event(terminal.InputEvent(kind="key", key="l"), 140, 34) is True
        inprog_frame = terminal.ANSI_ESCAPE_RE.sub("", tui.render_frame(140, 34))
        assert "File overlap with #4 (@bob)" in inprog_frame

        # Compact mode (< 110 cols)
        compact = terminal.ANSI_ESCAPE_RE.sub("", tui.render_frame(80, 28))
        assert "3:In Progress (1)" in compact

        # Help toggle & quit
        tui.handle_event(terminal.InputEvent(kind="key", key="?"), 140, 34)
        help_frame = terminal.ANSI_ESCAPE_RE.sub("", tui.render_frame(120, 30))
        assert "herdr-kanban Keybindings" in help_frame
        assert tui.handle_event(terminal.InputEvent(kind="key", key="q"), 120, 30) is False
    finally:
        tui.close_pipes()


def test_parse_input_bytes_handles_keys_utf8_and_sgr_mouse() -> None:
    raw = b"\x1b[A\x1b[<0;45;8Mj\r"
    events = terminal.parse_input_bytes(raw)
    assert [e.kind for e in events] == ["key", "mouse", "key", "key"]
    assert events[0].key == "up"
    assert events[1].col == 45 and events[1].row == 8 and events[1].pressed is True
    assert events[2].key == "j"
    assert events[3].key == "enter"


def test_setup_keys_conflict_detection_and_removal(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text('[keys]\nprefix = "ctrl+b"\n', encoding="utf-8")

    # Built-in conflict (`prefix+k` is focus_pane_up in Herdr)
    changed, msg = keys.setup("prefix+k", path=cfg)
    assert changed is False
    assert "already used" in msg

    # Default `prefix+m` succeeds cleanly
    changed, msg = keys.setup(None, path=cfg)
    assert changed is True
    assert "prefix+m" in msg
    assert keys.installed_key(cfg.read_text(encoding="utf-8")) == "prefix+m"

    # Idempotent second call
    changed2, _ = keys.setup(None, path=cfg)
    assert changed2 is False

    # Removal
    removed, _ = keys.setup(remove=True, path=cfg)
    assert removed is True
    assert keys.installed_key(cfg.read_text(encoding="utf-8")) is None


def test_skill_installer_never_overwrites_foreign_files(tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    (fake_home / ".gemini" / "config").mkdir(parents=True)
    foreign = fake_home / ".claude" / "skills" / "herdr-kanban"
    foreign.mkdir(parents=True)
    (foreign / "custom.md").write_text("keep me", encoding="utf-8")

    report = skill.install(home=fake_home)
    assert any("skipped" in line and "claude" in line for line in report)
    assert (fake_home / ".local" / "bin" / "herdr-kanban").is_symlink()
    assert (fake_home / ".agents" / "skills" / "herdr-kanban").is_symlink()
    assert (fake_home / ".gemini" / "config" / "skills" / "herdr-kanban").is_symlink()
    assert (foreign / "custom.md").read_text(encoding="utf-8") == "keep me"

    # Simulate `herdr plugin update` leaving a symlink to an older herdr-kanban checkout
    cli_link = fake_home / ".local" / "bin" / "herdr-kanban"
    cli_link.unlink()
    cli_link.symlink_to("/tmp/github-installations/herdr-kanban-old/bin/herdr-kanban")
    relinked = skill.install(refresh_only=True, home=fake_home)
    assert any("relinked" in line for line in relinked)
    assert cli_link.resolve() == skill.CLI_SRC.resolve()

    uninstalled = skill.install(uninstall=True, home=fake_home)
    assert any("removed" in line for line in uninstalled)
    assert not (fake_home / ".local" / "bin" / "herdr-kanban").exists()


def test_app_keys_setup_and_conflict_detection(tmp_path: Path) -> None:
    keys_json = tmp_path / "keys.json"
    keys_json.write_text('{\n  "ctrl-b": "mod.open:bar"\n}\n', encoding="utf-8")

    # Conflict with existing ctrl-b binding
    changed, msg = keys.setup_app_keys("ctrl-b", path=keys_json)
    assert changed is False
    assert "already bound" in msg

    # Default ctrl-shift-m succeeds
    changed, msg = keys.setup_app_keys(None, path=keys_json)
    assert changed is True
    assert "ctrl-shift-m" in msg

    # Idempotent second call
    changed2, _ = keys.setup_app_keys(None, path=keys_json)
    assert changed2 is False

    # Removal
    removed, _ = keys.setup_app_keys(remove=True, path=keys_json)
    assert removed is True


def test_install_app_mod_and_status(tmp_path: Path) -> None:
    app_home = tmp_path / "herdr-app"
    dest, st = skill.app_mod_status(app_home=app_home)
    assert st == "missing"

    report = skill.install_app_mod(app_home=app_home)
    assert any("installed" in line for line in report)
    dest, st = skill.app_mod_status(app_home=app_home)
    assert st == "installed"
    assert (dest / "main.js").is_file()
    assert (dest / "mod.json").is_file()
    assert (dest / "RECIPE.md").is_file()
    assert (dest / "kanban.svg").is_file()
    assert (dest / "refresh.svg").is_file()
    assert (dest / "sparkles.svg").is_file()
    assert (dest / "lock.svg").is_file()

    uninstalled = skill.install_app_mod(app_home=app_home, uninstall=True)
    assert any("removed" in line for line in uninstalled)
    assert skill.app_mod_status(app_home=app_home)[1] == "missing"


def test_hc_env_detection_and_board_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    monkeypatch.setenv("HC_TERMINAL_ID", "t42")
    monkeypatch.setenv("HC_TERMINAL_SOCKET", "/tmp/hc-test.sock")
    monkeypatch.setenv("HC_SCOPE", "scope-abc")

    assert herdr.current_pane_id() == "t42"
    assert herdr.current_socket_path() == "/tmp/hc-test.sock"
    assert herdr.current_scope() == "scope-abc"
    assert herdr.in_herdr_env() is True
    assert state.herdr_session_key().startswith("hc-")
    assert terminal.current_terminal_id() == "t42"
    osc = terminal.format_osc7501_status("working")
    assert osc == "\x1b]7501;app=herdr-kanban;state=working\x07"

    # CSI-u enter key parsing
    events = terminal.parse_input_bytes(b"\x1b[13u")
    assert len(events) == 1 and events[0].key == "enter"

    tui = _sample_tui()
    try:
        assert tui.ctx is not None
        t3 = next(t for t in tui.tickets if t.number == 3)
        assert t3.claim is not None
        t3.claim.checklist = ["[x] Step 1", "[ ] Step 2"]
        schema = ProjectSchema(
            project_id="PVT_1",
            number=3,
            title="Team Kanban",
            url="https://example.com",
            status_field_id="PVTSSF_1",
        )
        payload = tui.engine.build_board_payload(tui.ctx, schema, tui.tickets)
        assert payload["repo"] == "team/service"
        assert payload["target_branch"] == "feat/shared"
        assert len(payload["columns"]) == 6
        card3 = next(row for row in payload["tickets"] if row["number"] == 3)
        assert card3["ref"] == "#3"
        assert card3["cli_ref"] == "3"
        assert card3["checklist_done"] == 1
        assert card3["checklist_total"] == 2
        assert "whats_new" in payload
        wn = payload["whats_new"]
        assert wn["total_landed"] == 1
        assert "Shipped healthcheck" in wn["auto_markdown"]
        prompt = tui.engine.build_whats_new_agent_prompt(tui.ctx, tui.tickets)
        assert "herdr-kanban whats-new --write-summary" in prompt
        gen = tui.engine.generate_whats_new_summary(
            tui.ctx,
            tui.tickets,
            author="alice",
            agent_kind="claude",
            use_llm=False,
        )
        assert gen["saved_summary"]["author"] == "alice"
        assert gen["saved_summary"]["agent_kind"] == "claude"
        assert "Executive Briefing" in gen["saved_summary"]["markdown"]
    finally:
        tui.close_pipes()
