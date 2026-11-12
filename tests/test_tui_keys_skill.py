"""Tests for Kanban TUI rendering/navigation, keybinding setup, and skill links."""

from __future__ import annotations

from pathlib import Path

from herdr_kanban import config, keys, skill, terminal
from herdr_kanban.engine import KanbanEngine, RepoContext
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

    # Default `prefix+shift+k` succeeds cleanly
    changed, msg = keys.setup(None, path=cfg)
    assert changed is True
    assert "prefix+shift+k" in msg
    assert keys.installed_key(cfg.read_text(encoding="utf-8")) == "prefix+shift+k"

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

    uninstalled = skill.install(uninstall=True, home=fake_home)
    assert any("removed" in line for line in uninstalled)
    assert not (fake_home / ".local" / "bin" / "herdr-kanban").exists()
