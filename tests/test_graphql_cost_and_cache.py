"""Tests for GraphQL query node-budget optimization, 30s disk TTL cache, and GPUI poll throttle."""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any

from herdr_kanban.github import PROJECT_BOARD_QUERY, GitHubClient


def _estimate_graphql_page_nodes(query: str) -> int:
    """Compute a conservative static upper bound on GraphQL nodes requested per page."""
    # Count how many times the items(...) connection is instantiated (e.g. Org + User dual fragments = 2x)
    fragment_uses = max(1, len(re.findall(r"\.\.\.\s*ProjectFields\b", query)))
    items_match = re.search(r"items\s*\(\s*first\s*:\s*(\d+)", query)
    items_n = int(items_match.group(1)) if items_match else 0

    nested_sum = 1  # the item node itself
    for field_name in ("fieldValues", "assignees", "labels", "comments"):
        m = re.search(rf"{field_name}\s*\(\s*(?:first|last)\s*:\s*(\d+)", query)
        if m:
            nested_sum += int(m.group(1))

    fields_match = re.search(r"fields\s*\(\s*first\s*:\s*(\d+)", query)
    fields_n = int(fields_match.group(1)) if fields_match else 0

    return fragment_uses * (fields_n + items_n * nested_sum)


def test_bulk_board_query_slashes_comment_and_node_budget() -> None:
    """Bulk board query must cap comments at <=5 and stay under 3,500 worst-case nodes/page."""
    assert "comments(last: 100)" not in PROJECT_BOARD_QUERY
    assert "comments(last: 30)" not in PROJECT_BOARD_QUERY

    comments_match = re.search(r"comments\s*\(\s*last\s*:\s*(\d+)\s*\)", PROJECT_BOARD_QUERY)
    assert comments_match is not None, "Expected slim comments(last: <=5) in bulk board query"
    assert int(comments_match.group(1)) <= 5

    # Must not double static GraphQL cost by querying both Organization and User inline fragments
    assert len(re.findall(r"\.\.\.\s*ProjectFields\b", PROJECT_BOARD_QUERY)) <= 1

    est_nodes = _estimate_graphql_page_nodes(PROJECT_BOARD_QUERY)
    assert est_nodes <= 3500, f"Expected <=3500 worst-case nodes/page, got {est_nodes}"


def test_fetch_board_uses_org_query_and_falls_back_to_user_query() -> None:
    """GitHubClient.fetch_board queries organization first and falls back to user without dual fragments."""
    seen_queries: list[str] = []

    def fake_runner(args: list[str], stdin_data: str | None) -> subprocess.CompletedProcess[str]:
        assert args[:2] == ["api", "graphql"]
        payload = json.loads(stdin_data or "{}")
        q = payload["query"]
        seen_queries.append(q)
        if "organization(" in q:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=json.dumps(
                    {
                        "data": {"organization": None},
                        "errors": [
                            {"message": "Could not resolve to an Organization with the login of 'alice'."}
                        ],
                    }
                ),
                stderr="",
            )
        assert "user(" in q
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=json.dumps(
                {
                    "data": {
                        "user": {
                            "projectV2": {
                                "id": "PVT_U1",
                                "number": 3,
                                "title": "Alice Board",
                                "url": "https://github.com/users/alice/projects/3",
                                "field": {
                                    "id": "F_STATUS",
                                    "name": "Status",
                                    "options": [{"id": "O1", "name": "Ready"}],
                                },
                                "priorityField": None,
                                "fields": {"nodes": []},
                                "items": {
                                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                                    "nodes": [
                                        {
                                            "id": "PVTI_1",
                                            "fieldValueByName": {"name": "Ready", "optionId": "O1"},
                                            "priorityValue": None,
                                            "fieldValues": {"nodes": []},
                                            "content": {
                                                "id": "I_1",
                                                "number": 7,
                                                "title": "Slim query ticket",
                                                "body": "Body",
                                                "state": "OPEN",
                                                "url": "https://github.com/alice/repo/issues/7",
                                                "updatedAt": "2026-11-24T00:00:00Z",
                                                "author": {"login": "alice"},
                                                "assignees": {"nodes": []},
                                                "labels": {"nodes": []},
                                                "milestone": None,
                                                "repository": {"name": "repo", "owner": {"login": "alice"}},
                                                "comments": {"totalCount": 0, "nodes": []},
                                            },
                                        }
                                    ],
                                },
                            }
                        }
                    }
                }
            ),
            stderr="",
        )

    client = GitHubClient(runner=fake_runner)
    schema, tickets = client.fetch_board(
        project_owner="alice",
        project_number=3,
        repo_owner="alice",
        repo_name="repo",
        column_map={"ready": "Ready"},
    )
    assert schema.project_id == "PVT_U1"
    assert len(tickets) == 1
    assert tickets[0].number == 7
    assert len(seen_queries) == 2

    # Subsequent call for the same owner should go straight to user(...) using cached owner kind
    schema2, tickets2 = client.fetch_board(
        project_owner="alice",
        project_number=3,
        repo_owner="alice",
        repo_name="repo",
        column_map={"ready": "Ready"},
    )
    assert schema2.project_id == "PVT_U1"
    assert len(tickets2) == 1
    assert len(seen_queries) == 3
    assert "user(" in seen_queries[-1]


class _CountingFakeGitHub:
    """Minimal counting wrapper around FakeGitHub from test_git_and_engine_e2e."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.fetch_board_calls = 0

    def fetch_board(self, *args: Any, **kwargs: Any) -> Any:
        self.fetch_board_calls += 1
        return self._inner.fetch_board(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def test_disk_ttl_cache_deduplicates_back_to_back_reads_and_honors_no_cache_and_ttl(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """Back-to-back board/list/show calls share 1 fetch within TTL; --no-cache and TTL=0 bypass it."""
    from herdr_kanban import cli, config
    from herdr_kanban.engine import KanbanEngine
    from test_git_and_engine_e2e import FakeGitHub, FakeHerdr, _git

    cache_dir = tmp_path / "disk-cache"
    monkeypatch.setenv("HERDR_KANBAN_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HERDR_KANBAN_CACHE_DIR", str(cache_dir))
    monkeypatch.setenv("HERDR_PLUGIN_CONFIG_DIR", str(tmp_path / "plugin-cfg"))
    monkeypatch.setenv("HERDR_KANBAN_CACHE_TTL_SEC", "30")

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "dev@example.com")
    _git(repo, "config", "user.name", "Dev")
    board_cfg = config.BoardConfig(
        project_owner="acme",
        project_number=7,
        target_branch="main",
        repo_owner="acme",
        repo_name="app",
    )
    (repo / ".herdr-kanban.toml").write_text(config.render_board_config_toml(board_cfg), encoding="utf-8")

    gh_inner = FakeGitHub()
    gh_inner.seed_issue(42, "Cached ticket", status="Ready")
    gh = _CountingFakeGitHub(gh_inner)
    engine = KanbanEngine(github=gh, herdr=FakeHerdr())  # type: ignore[arg-type]
    ctx = engine.resolve_context(repo)

    # 1. Back-to-back board + list + show share 1 GraphQL fetch
    parser = cli.build_parser()
    assert cli.cmd_board(parser.parse_args(["board", "--cwd", str(repo), "--json"]), engine) == 0
    assert cli.cmd_list(parser.parse_args(["list", "--cwd", str(repo), "--json"]), engine) == 0
    assert cli.cmd_show(parser.parse_args(["show", "42", "--cwd", str(repo), "--json"]), engine) == 0
    assert gh.fetch_board_calls == 1

    # Verify cache file location matches board-<owner>-<project>.json
    expected_cache_file = cache_dir / "board-acme-7.json"
    assert expected_cache_file.is_file()

    # 2. --no-cache forces a fresh GraphQL fetch
    assert (
        cli.cmd_board(parser.parse_args(["board", "--cwd", str(repo), "--no-cache", "--json"]), engine) == 0
    )
    assert gh.fetch_board_calls == 2

    # 3. HERDR_KANBAN_CACHE_TTL_SEC=0 bypasses cache
    monkeypatch.setenv("HERDR_KANBAN_CACHE_TTL_SEC", "0")
    engine.refresh_board(ctx)
    assert gh.fetch_board_calls == 3

    # 4. Expired cache triggers a fresh fetch even with TTL=30
    monkeypatch.setenv("HERDR_KANBAN_CACHE_TTL_SEC", "30")
    raw = json.loads(expected_cache_file.read_text(encoding="utf-8"))
    raw["fetched_at"] = 1.0  # long expired
    expected_cache_file.write_text(json.dumps(raw), encoding="utf-8")
    engine.refresh_board(ctx)
    assert gh.fetch_board_calls == 4


def test_disk_ttl_cache_invalidates_immediately_on_mutations(tmp_path: Any, monkeypatch: Any) -> None:
    """Every local mutation invalidates the disk cache so the next refresh_board fetches live data."""
    from herdr_kanban import config
    from herdr_kanban.engine import KanbanEngine
    from herdr_kanban.model import Column
    from test_git_and_engine_e2e import FakeGitHub, FakeHerdr, _git

    cache_dir = tmp_path / "disk-cache"
    monkeypatch.setenv("HERDR_KANBAN_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HERDR_KANBAN_CACHE_DIR", str(cache_dir))
    monkeypatch.setenv("HERDR_PLUGIN_CONFIG_DIR", str(tmp_path / "plugin-cfg"))
    monkeypatch.setenv("HERDR_KANBAN_CACHE_TTL_SEC", "60")

    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", str(bare))
    repo = tmp_path / "repo"
    _git(tmp_path, "clone", str(bare), str(repo))
    _git(repo, "config", "user.email", "dev@example.com")
    _git(repo, "config", "user.name", "Dev")
    _git(repo, "checkout", "-b", "main")
    board_cfg = config.BoardConfig(
        project_owner="acme",
        project_number=7,
        target_branch="main",
        repo_owner="acme",
        repo_name="app",
    )
    (repo / ".herdr-kanban.toml").write_text(config.render_board_config_toml(board_cfg), encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Init")
    _git(repo, "push", "-u", "origin", "main")
    config.save_user_settings(
        config.UserSettings(default_agent_kind="claude", worktree_root=str(tmp_path / "wt"))
    )

    gh_inner = FakeGitHub()
    gh_inner.seed_issue(10, "Mutation test ticket", status="Ready")
    gh = _CountingFakeGitHub(gh_inner)
    engine = KanbanEngine(github=gh, herdr=FakeHerdr())  # type: ignore[arg-type]
    ctx = engine.resolve_context(repo)

    cache_file = cache_dir / "board-acme-7.json"

    # Populate cache
    engine.refresh_board(ctx)
    assert cache_file.is_file()
    calls = gh.fetch_board_calls

    # 1. post_comment invalidates cache
    engine.post_comment(ctx, 10, "Hello")
    assert not cache_file.exists()
    engine.refresh_board(ctx)
    assert gh.fetch_board_calls == calls + 1
    calls = gh.fetch_board_calls

    # 2. ask_ticket invalidates cache
    engine.ask_ticket(ctx, 10, "Quick question")
    assert not cache_file.exists()
    engine.refresh_board(ctx)
    assert gh.fetch_board_calls == calls + 1
    calls = gh.fetch_board_calls

    # 3. deposit_issue invalidates cache
    dep = engine.deposit_issue(ctx, "New follow-up")
    assert not cache_file.exists()
    engine.refresh_board(ctx)
    assert cache_file.is_file()

    # 4. triage_ticket invalidates cache
    engine.triage_ticket(ctx, dep.number, column=Column.READY, force=True)
    assert not cache_file.exists()

    # 5. claim_ticket -> update_progress -> release_ticket each invalidate cache
    engine.claim_ticket(ctx, 10)
    assert not cache_file.exists()
    engine.refresh_board(ctx)
    assert cache_file.is_file()

    engine.update_progress(ctx, 10, note="Halfway")
    assert not cache_file.exists()
    engine.refresh_board(ctx)
    assert cache_file.is_file()

    engine.release_ticket(ctx, 10, reason="Done testing")
    assert not cache_file.exists()


def test_gpui_mod_throttles_background_polling_and_backs_off_on_rate_limit() -> None:
    """GPUI mod must poll at >=60s, use cached board for silent polls, and back off on rate-limit errors."""
    from pathlib import Path

    main_js = (Path(__file__).resolve().parent.parent / "mods" / "herdr-kanban" / "main.js").read_text(
        encoding="utf-8"
    )
    # Must no longer poll every 12s
    assert "cx.timer.every(12000" not in main_js
    poll_match = re.search(r"cx\.timer\.every\(\s*(?:BACKGROUND_POLL_MS|(\d+))", main_js)
    assert poll_match is not None
    if poll_match.group(1):
        assert int(poll_match.group(1)) >= 60000
    else:
        const_match = re.search(r"const\s+BACKGROUND_POLL_MS\s*=\s*(\d+)", main_js)
        assert const_match is not None and int(const_match.group(1)) >= 60000

    # Explicit manual refresh supports --no-cache while background poll uses cached board
    assert "--no-cache" in main_js
    # Rate-limit backoff guard must exist
    assert "rateLimitBackoffUntil" in main_js
    assert "rate limit" in main_js.lower()


