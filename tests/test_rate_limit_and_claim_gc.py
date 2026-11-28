"""Regression tests for REST & GraphQL rate-limit protection, single-daemon locking, and claim GC."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from herdr_kanban import cli, config, daemon, state
from herdr_kanban.engine import KanbanEngine
from herdr_kanban.github import GitHubClient
from herdr_kanban.model import (
    ClaimMeta,
    Column,
    RelayMeta,
    lease_until_iso,
    render_claim_comment,
    render_relay_comment,
    utc_now_iso,
)
from test_git_and_engine_e2e import FakeGitHub, FakeHerdr, _git


def _init_repo_with_board(tmp_path: Path, monkeypatch: Any) -> tuple[Path, config.BoardConfig]:
    monkeypatch.setenv("HERDR_KANBAN_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HERDR_KANBAN_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("HERDR_PLUGIN_CONFIG_DIR", str(tmp_path / "plugin-cfg"))

    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", str(bare))
    repo = tmp_path / "repo"
    _git(tmp_path, "clone", str(bare), str(repo))
    _git(repo, "config", "user.email", "dev@example.com")
    _git(repo, "config", "user.name", "Dev")
    _git(repo, "checkout", "-b", "main")
    board_cfg = config.BoardConfig(
        project_owner="Ultimaker",
        project_number=7,
        target_branch="main",
        repo_owner="Ultimaker",
        repo_name="app",
    )
    (repo / ".herdr-kanban.toml").write_text(config.render_board_config_toml(board_cfg), encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Init")
    _git(repo, "push", "-u", "origin", "main")
    config.save_user_settings(
        config.UserSettings(
            default_agent_kind="agy",
            worktree_root=str(tmp_path / "wt"),
            poll_seconds=60,
            heartbeat_seconds=300,
        )
    )
    return repo, board_cfg


def test_single_instance_daemon_lock_and_legacy_reaper(tmp_path: Path, monkeypatch: Any) -> None:
    """daemon.lock_path() must use one lockfile per state_dir across sockets and reap legacy locks."""
    st_dir = tmp_path / "state"
    monkeypatch.setenv("HERDR_KANBAN_STATE_DIR", str(st_dir))

    monkeypatch.setenv("HERDR_SOCKET_PATH", "/run/user/1000/herdr.sock")
    monkeypatch.delenv("HC_TERMINAL_SOCKET", raising=False)
    path_legacy = daemon.lock_path()

    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    monkeypatch.setenv("HC_TERMINAL_SOCKET", "/run/user/1000/herdr-core/default.sock")
    path_hc = daemon.lock_path()

    assert path_legacy == path_hc == st_dir / "daemon.lock"

    # Seed stale legacy session lockfiles and verify reap_legacy_session_daemons removes them safely
    st_dir.mkdir(parents=True, exist_ok=True)
    stale_herdr = st_dir / "daemon-herdr.lock"
    stale_default = st_dir / "daemon-default.lock"
    stale_herdr.write_text("99999999\n", encoding="utf-8")
    stale_default.write_text(f"{os.getpid()}\n", encoding="utf-8")

    reaped = daemon.reap_legacy_session_daemons()
    assert not stale_herdr.exists()
    assert not stale_default.exists()
    assert isinstance(reaped, list)


def test_sync_claims_garbage_collects_closed_released_superseded_and_triaged_done_claims(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Closed, released, superseded, and Done-triaged local claims are auto-removed."""
    repo, _ = _init_repo_with_board(tmp_path, monkeypatch)
    gh = FakeGitHub(current_login="jelle")
    for num in (10, 11, 12, 13):
        gh.seed_issue(num, f"Ticket {num}", status="Ready")

    engine = KanbanEngine(github=gh, herdr=FakeHerdr())  # type: ignore[arg-type]
    ctx = engine.resolve_context(repo)

    engine.claim_ticket(ctx, 10)
    c11 = engine.claim_ticket(ctx, 11).claim
    c12 = engine.claim_ticket(ctx, 12).claim
    c13 = engine.claim_ticket(ctx, 13).claim
    assert {c.issue_number for c in state.list_local_claims()} == {10, 11, 12, 13}

    # #10 closed externally (e.g. via gh issue close)
    gh.close_issue("Ultimaker", "app", 10)

    # #11 released on GitHub
    rel_meta = ClaimMeta(
        issue_number=11,
        owner="jelle",
        agent_kind="agy",
        branch=c11.local_branch,
        state="released",
        started_at=utc_now_iso(),
        heartbeat_at=utc_now_iso(),
        lease_until=lease_until_iso(30),
    )
    gh.patch_comment("Ultimaker", "app", c11.comment_id, render_claim_comment(rel_meta))

    # #12 superseded by a colleague takeover comment after lease expiry
    expired_meta = ClaimMeta(
        issue_number=12,
        owner="jelle",
        agent_kind="agy",
        branch=c12.local_branch,
        state="active",
        started_at="2020-01-01T00:00:00Z",
        heartbeat_at="2020-01-01T00:00:00Z",
        lease_until="2020-01-01T00:30:00Z",
    )
    gh.patch_comment("Ultimaker", "app", c12.comment_id, render_claim_comment(expired_meta))
    takeover_meta = ClaimMeta(
        issue_number=12,
        owner="colleague",
        agent_kind="claude",
        branch="kanban/issue-12",
        state="active",
        started_at=utc_now_iso(),
        heartbeat_at=utc_now_iso(),
        lease_until=lease_until_iso(30),
        takeover_of=c12.comment_id,
    )
    gh.post_comment("Ultimaker", "app", 12, render_claim_comment(takeover_meta), author="colleague")

    # Sync once: #10, #11, #12 must be garbage-collected; #13 stays active
    engine.sync_claim_heartbeats_and_relays()
    remaining = [c.issue_number for c in state.list_local_claims()]
    assert remaining == [c13.issue_number]

    # Moving #13 to Done via triage_ticket must immediately remove its local claim too
    engine.triage_ticket(ctx, 13, column=Column.DONE, force=True)
    assert state.list_local_claims() == []


def test_batched_graphql_claim_poll_zero_rest_and_persisted_heartbeat_cooldown(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """GitHubClient batches multi-claim polling into 1 GraphQL call and persists heartbeats."""
    repo, _ = _init_repo_with_board(tmp_path, monkeypatch)

    graphql_calls: list[str] = []
    rest_calls: list[list[str]] = []

    claim_body_21 = render_claim_comment(
        ClaimMeta(
            issue_number=21,
            owner="jelle",
            agent_kind="agy",
            branch="kanban/issue-21",
            state="active",
            started_at=utc_now_iso(),
            heartbeat_at=utc_now_iso(),
            lease_until=lease_until_iso(30),
        )
    )
    claim_body_22 = render_claim_comment(
        ClaimMeta(
            issue_number=22,
            owner="jelle",
            agent_kind="agy",
            branch="kanban/issue-22",
            state="active",
            started_at=utc_now_iso(),
            heartbeat_at=utc_now_iso(),
            lease_until=lease_until_iso(30),
        )
    )
    relay_body = render_relay_comment(
        RelayMeta(
            to_issue=22,
            from_owner="colleague",
            from_agent="claude",
            from_issue=99,
            created_at=utc_now_iso(),
        ),
        "Did you change the schema?",
    )

    def runner(args: list[str], stdin_data: str | None) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["api", "graphql"]:
            payload = json.loads(stdin_data or "{}")
            q = payload["query"]
            graphql_calls.append(q)
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=json.dumps(
                    {
                        "data": {
                            "repository": {
                                "i_20": {"state": "CLOSED", "comments": {"nodes": []}},
                                "i_21": {
                                    "state": "OPEN",
                                    "comments": {
                                        "nodes": [
                                            {
                                                "databaseId": 5001,
                                                "body": claim_body_21,
                                                "createdAt": "2026-11-28T09:00:00Z",
                                                "url": "https://github.com/Ultimaker/app/issues/21#issuecomment-5001",
                                                "author": {"login": "jelle"},
                                            }
                                        ]
                                    },
                                },
                                "i_22": {
                                    "state": "OPEN",
                                    "comments": {
                                        "nodes": [
                                            {
                                                "databaseId": 5002,
                                                "body": claim_body_22,
                                                "createdAt": "2026-11-28T09:00:00Z",
                                                "url": "https://github.com/Ultimaker/app/issues/22#issuecomment-5002",
                                                "author": {"login": "jelle"},
                                            },
                                            {
                                                "databaseId": 5099,
                                                "body": relay_body,
                                                "createdAt": "2026-11-28T09:05:00Z",
                                                "url": "https://github.com/Ultimaker/app/issues/22#issuecomment-5099",
                                                "author": {"login": "colleague"},
                                            },
                                        ]
                                    },
                                },
                            }
                        }
                    }
                ),
                stderr="",
            )

        rest_calls.append(args)
        if args == ["api", "user"]:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=json.dumps({"login": "jelle"}),
                stderr="",
            )
        if "collaborators" in " ".join(args):
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=json.dumps([{"login": "jelle"}, {"login": "colleague"}]),
                stderr="",
            )
        if "-X" in args and "PATCH" in args:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="{}", stderr="")
        raise AssertionError(f"Unexpected REST call: {args}")

    slug = config.repo_slug("Ultimaker", "app")
    for num, cid in ((20, 5000), (21, 5001), (22, 5002)):
        wt = tmp_path / "wt" / f"issue-{num}"
        wt.mkdir(parents=True, exist_ok=True)
        state.save_local_claim(
            state.LocalClaim(
                repo_slug=slug,
                repo_owner="Ultimaker",
                repo_name="app",
                main_repo_root=str(repo),
                issue_number=num,
                worktree_path=str(wt),
                local_branch=f"kanban/issue-{num}",
                target_branch="main",
                remote="origin",
                comment_id=cid,
                owner="jelle",
                agent_kind="agy",
                pane_id=f"t{num}",
            )
        )

    # First CLI process syncs: 1 batched GraphQL poll, #20 GC'd, #21 & #22 patched once, relay delivered
    gh1 = GitHubClient(runner=runner)
    eng1 = KanbanEngine(github=gh1, herdr=FakeHerdr())
    items1 = eng1.sync_claim_heartbeats_and_relays()
    assert len(graphql_calls) == 1
    assert [c.issue_number for c in state.list_local_claims()] == [21, 22]
    assert len(items1) == 1 and items1[0].issue_number == 22
    assert not any("/comments?per_page=" in " ".join(c) for c in rest_calls)
    patch_count_after_first = sum(1 for c in rest_calls if "PATCH" in c)
    assert patch_count_after_first == 2

    # Second brand-new CLI process (simulating `herdr-kanban inbox --ack` 2s later):
    # persisted heartbeat timestamps + collaborator disk cache mean 0 PATCHes and 0 collaborator REST calls!
    gh2 = GitHubClient(runner=runner)
    eng2 = KanbanEngine(github=gh2, herdr=FakeHerdr())
    eng2.sync_claim_heartbeats_and_relays()
    assert sum(1 for c in rest_calls if "PATCH" in c) == patch_count_after_first
    assert sum(1 for c in rest_calls if "collaborators" in " ".join(c)) == 1


def test_cmd_show_and_cmd_inbox_skip_unneeded_api_calls(tmp_path: Path, monkeypatch: Any) -> None:
    """`show` in text mode skips REST comments; `inbox --ack` skips full-machine sync when fresh."""
    repo, _ = _init_repo_with_board(tmp_path, monkeypatch)
    gh = FakeGitHub(current_login="jelle")
    gh.seed_issue(42, "Show without REST comments", status="Ready")

    comment_fetches = 0
    orig_list_comments = gh.list_issue_comments

    def counting_comments(owner: str, repo_name: str, number: int) -> list[dict[str, Any]]:
        nonlocal comment_fetches
        comment_fetches += 1
        return orig_list_comments(owner, repo_name, number)

    gh.list_issue_comments = counting_comments  # type: ignore[method-assign]
    engine = KanbanEngine(github=gh, herdr=FakeHerdr())  # type: ignore[arg-type]
    ctx = engine.resolve_context(repo)

    # Claim #42 in an isolated worktree, then run `show` and `inbox --ack` from main repo
    engine.claim_ticket(ctx, 42)
    engine.sync_claim_heartbeats_and_relays()
    comment_fetches = 0

    parser = cli.build_parser()
    assert cli.cmd_show(parser.parse_args(["show", "42", "--cwd", str(repo)]), engine) == 0
    assert comment_fetches == 0, "Text-mode `herdr-kanban show` must not fetch REST issue comments"

    # `inbox --ack` from main repo right after recent sync must make 0 comment fetches
    assert cli.cmd_inbox(parser.parse_args(["inbox", "--ack", "--cwd", str(repo)]), engine) == 0
    assert comment_fetches == 0, "`inbox --ack` must not re-poll claims within cooldown"


def test_refresh_board_singleflight_coalesces_concurrent_cache_misses(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Concurrent callers on an expired board cache coalesce into 1 GraphQL fetch."""
    repo, _ = _init_repo_with_board(tmp_path, monkeypatch)
    monkeypatch.setenv("HERDR_KANBAN_CACHE_TTL_SEC", "30")

    gh_inner = FakeGitHub(current_login="jelle")
    gh_inner.seed_issue(7, "Concurrent ticket", status="Ready")
    fetch_count = 0
    count_lock = threading.Lock()

    class SlowFakeGitHub:
        def fetch_board(self, *args: Any, **kwargs: Any) -> Any:
            nonlocal fetch_count
            time.sleep(0.06)
            with count_lock:
                fetch_count += 1
            return gh_inner.fetch_board(*args, **kwargs)

        def __getattr__(self, name: str) -> Any:
            return getattr(gh_inner, name)

    slow_gh = SlowFakeGitHub()
    barrier = threading.Barrier(6)
    results: list[int] = []

    def worker() -> None:
        eng = KanbanEngine(github=slow_gh, herdr=FakeHerdr())  # type: ignore[arg-type]
        c = eng.resolve_context(repo)
        barrier.wait()
        _, tks = eng.refresh_board(c)
        results.append(len(tks))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results == [1, 1, 1, 1, 1, 1]
    assert fetch_count == 1
    counters = state.load_telemetry_counters()
    repo_stats = counters["repos"]["Ultimaker/app"]
    assert repo_stats["board_cache_hits"] == 5
    assert repo_stats["singleflight_coalesced"] == 5


def test_telemetry_counters_surface_attribution_rate_limit_and_zero_pii(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """GitHubClient + KanbanEngine record REST/GraphQL/surface/GC/rate-limit counters with zero PII."""
    repo, _ = _init_repo_with_board(tmp_path, monkeypatch)

    def runner(args: list[str], stdin_data: str | None) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["api", "graphql"]:
            payload = json.loads(stdin_data or "{}")
            if "rateLimitTrigger" in payload.get("query", ""):
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=1,
                    stdout="",
                    stderr="gh: API rate limit exceeded for user ID 12345 (HTTP 403)",
                )
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=json.dumps(
                    {
                        "data": {
                            "repository": {
                                "i_99": {"state": "CLOSED", "comments": {"nodes": []}}
                            }
                        }
                    }
                ),
                stderr="",
            )
        if args == ["api", "repos/Ultimaker/app/issues/99"]:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=json.dumps({"number": 99, "title": "Secret Title", "state": "open"}),
                stderr="",
            )
        raise AssertionError(f"Unexpected call: {args}")

    gh = GitHubClient(runner=runner)
    gh.set_telemetry_owner("Ultimaker", "app")

    monkeypatch.setenv("HERDR_KANBAN_SURFACE", "daemon")
    snap = gh.fetch_issues_poll_snapshot("Ultimaker", "app", [99])
    assert snap[99][0] == "CLOSED"

    monkeypatch.setenv("HERDR_KANBAN_SURFACE", "gpui_mod")
    gh.get_issue("Ultimaker", "app", 99)

    monkeypatch.setenv("HERDR_KANBAN_SURFACE", "cli")
    try:
        gh.graphql("query { rateLimitTrigger }", {"owner": "Ultimaker"})
    except Exception:
        pass

    # Non-Ultimaker call is isolated under its own `<Owner>/<Repo>` key
    gh.set_telemetry_owner("jellespijker", "omabook")
    try:
        gh.graphql("query { rateLimitTrigger }", {"owner": "jellespijker", "repo": "omabook"})
    except Exception:
        pass

    # Verify GC counter on closed claim
    slug = config.repo_slug("Ultimaker", "app")
    wt = tmp_path / "wt" / "issue-99"
    wt.mkdir(parents=True, exist_ok=True)
    state.save_local_claim(
        state.LocalClaim(
            repo_slug=slug,
            repo_owner="Ultimaker",
            repo_name="app",
            main_repo_root=str(repo),
            issue_number=99,
            worktree_path=str(wt),
            local_branch="kanban/issue-99",
            target_branch="main",
            remote="origin",
            comment_id=999,
            owner="jelle",
            agent_kind="agy",
        )
    )
    eng = KanbanEngine(github=gh, herdr=FakeHerdr())
    eng.sync_claim_heartbeats_and_relays()

    raw_text = state.telemetry_counters_path().read_text(encoding="utf-8")
    assert "Secret Title" not in raw_text
    assert str(tmp_path) not in raw_text
    assert "12345" not in raw_text

    counters = state.load_telemetry_counters()
    app_entry = counters["repos"]["Ultimaker/app"]
    assert app_entry["graphql_calls"] == 2  # initial poll + sync_claim_heartbeats_and_relays
    assert app_entry["rest_calls"] == 1
    assert app_entry["claims_gc_reaped"] == 1
    assert app_entry["by_surface"]["daemon"]["graphql_calls"] == 1
    assert app_entry["by_surface"]["gpui_mod"]["rest_calls"] == 1

    proj_entry = counters["repos"]["Ultimaker/__project__"]
    assert proj_entry["graphql_calls"] == 1
    assert proj_entry["rate_limit_errors"] == 1

    other_entry = counters["repos"]["jellespijker/omabook"]
    assert other_entry["graphql_calls"] == 1
    assert other_entry["rate_limit_errors"] == 1


def test_triage_ticket_retries_fresh_board_when_missing_from_disk_cache(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """`triage_ticket` refreshes bypassing the 30s disk cache if the issue was added externally."""
    repo, _ = _init_repo_with_board(tmp_path, monkeypatch)
    monkeypatch.setenv("HERDR_KANBAN_CACHE_TTL_SEC", "30")
    gh = FakeGitHub(current_login="jelle")
    gh.seed_issue(1, "Existing ticket", status="Ready")

    engine = KanbanEngine(github=gh, herdr=FakeHerdr())  # type: ignore[arg-type]
    ctx = engine.resolve_context(repo)
    engine.refresh_board(ctx)  # Prime 30s disk cache with only #1

    # External tool adds #2 to Project #7 without invalidating local disk cache
    gh.seed_issue(2, "Externally added ticket", status="Triage")
    updated = engine.triage_ticket(ctx, 2, column=Column.IN_PROGRESS, force=True)
    assert updated.number == 2
    assert updated.column == Column.IN_PROGRESS
