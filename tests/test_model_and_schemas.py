"""Schema validation and pure domain model tests."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import jsonschema

from herdr_kanban import config
from herdr_kanban.model import (
    ClaimMeta,
    Column,
    DepositMeta,
    RelayMeta,
    Ticket,
    compute_file_overlaps,
    decode_meta,
    detect_dependency_cycle,
    match_column,
    populate_dependency_waves,
    render_claim_comment,
    render_deposit_body,
    render_relay_comment,
    resolve_winning_claim,
    strip_meta,
    validate_ready_gate,
)

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schema"


def _load_schema(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


def test_comment_metadata_conforms_to_json_schema() -> None:
    schema = _load_schema("comment-meta.v1.json")

    claim = ClaimMeta(
        issue_number=42,
        owner="jelle",
        agent_kind="claude",
        agent_name="issue-42",
        state="active",
        started_at="2026-11-12T04:00:00Z",
        heartbeat_at="2026-11-12T04:05:00Z",
        lease_until="2026-11-12T04:35:00Z",
        branch="feat/shared",
        touched_files=["src/auth.py"],
        checklist=["[x] Parser", "[ ] Tests"],
    )
    rendered_claim = render_claim_comment(claim)
    decoded_claim = decode_meta(rendered_claim)
    assert decoded_claim is not None
    jsonschema.validate(decoded_claim, schema)
    assert "src/auth.py" in strip_meta(rendered_claim)

    relay = RelayMeta(
        from_owner="alice",
        from_agent="agy",
        from_issue=41,
        to_issue=42,
        created_at="2026-11-12T04:10:00Z",
    )
    decoded_relay = decode_meta(render_relay_comment(relay, "Did you touch TokenStore?"))
    assert decoded_relay is not None
    jsonschema.validate(decoded_relay, schema)

    deposit = DepositMeta(
        deposited_by="jelle",
        branch="feat/shared",
        parent_issue=42,
        file_line="src/token.py:77",
        commit_sha="deadbeef1234",
    )
    decoded_deposit = decode_meta(render_deposit_body("Race condition on expiry.", deposit))
    assert decoded_deposit is not None
    jsonschema.validate(decoded_deposit, schema)


def test_board_config_roundtrip_conforms_to_json_schema() -> None:
    schema = _load_schema("board-config.v1.json")
    cfg = config.BoardConfig(
        project_owner="jellespijker",
        project_number=7,
        target_branch="feat/shared",
        repo_owner="jellespijker",
        repo_name="demo",
        check_command="pytest -q",
        trusted_authors=["colleague1"],
    )
    toml_text = config.render_board_config_toml(cfg)
    parsed = tomllib.loads(toml_text)
    jsonschema.validate(parsed, schema)
    reloaded = config.BoardConfig.from_mapping(parsed)
    assert reloaded.project_number == 7
    assert reloaded.target_branch == "feat/shared"
    assert reloaded.check_command == "pytest -q"


def test_resolve_winning_claim_tie_breaker_release_and_takeover() -> None:
    alice = ClaimMeta(
        issue_number=10,
        owner="alice",
        agent_kind="claude",
        state="active",
        started_at="2026-11-12T04:00:00Z",
        heartbeat_at="2026-11-12T04:00:00Z",
        lease_until="2026-11-12T04:30:00Z",
    )
    bob = ClaimMeta(
        issue_number=10,
        owner="bob",
        agent_kind="codex",
        state="active",
        started_at="2026-11-12T04:00:01Z",
        heartbeat_at="2026-11-12T04:00:01Z",
        lease_until="2026-11-12T04:30:01Z",
    )
    comments = [
        {"id": 200, "body": render_claim_comment(bob)},
        {"id": 199, "body": render_claim_comment(alice)},
    ]
    # Even if returned unsorted, lowest ID (199 -> alice) wins!
    win = resolve_winning_claim(comments)
    assert win is not None
    assert win[0] == 199
    assert win[1].owner == "alice"

    # Explicit takeover of comment 199 wins
    carol_takeover = ClaimMeta(
        issue_number=10,
        owner="carol",
        agent_kind="agy",
        state="active",
        started_at="2026-11-12T05:00:00Z",
        heartbeat_at="2026-11-12T05:00:00Z",
        lease_until="2026-11-12T05:30:00Z",
        takeover_of=199,
    )
    comments.append({"id": 205, "body": render_claim_comment(carol_takeover)})
    win2 = resolve_winning_claim(comments)
    assert win2 is not None
    assert win2[0] == 205
    assert win2[1].owner == "carol"

    # When Carol releases in-place, no active claim remains (since 200 did not win)
    carol_takeover.state = "released"
    comments[-1]["body"] = render_claim_comment(carol_takeover)
    # Remove Bob's stale race comment (as losing claimant deletes it)
    comments = [c for c in comments if c["id"] != 200]
    alice.state = "released"
    comments[0]["body"] = render_claim_comment(alice)
    assert resolve_winning_claim(comments) is None


def test_file_overlap_detection_and_column_aliases() -> None:
    t1 = Ticket(
        number=1,
        title="Auth refactor",
        column=Column.IN_PROGRESS,
        claim=ClaimMeta(issue_number=1, owner="alice", agent_kind="claude", touched_files=["a.py", "b.py"]),
    )
    t2 = Ticket(
        number=2,
        title="Session refresh",
        column=Column.IN_PROGRESS,
        claim=ClaimMeta(issue_number=2, owner="bob", agent_kind="agy", touched_files=["b.py", "c.py"]),
    )
    overlaps = compute_file_overlaps([t1, t2])
    assert overlaps[1][0].other_issue == 2
    assert overlaps[1][0].files == ["b.py"]
    assert match_column("To Do") == Column.READY
    assert match_column("Backlog") == Column.TRIAGE
    assert match_column("Needs Review") == Column.REVIEW


def test_generic_taxonomy_ready_gate_and_dependency_waves() -> None:
    schema = _load_schema("board-config.v1.json")
    cfg = config.BoardConfig(
        project_owner="acme",
        project_number=7,
        target_branch="main",
        issues_repo="meta",
        priorities=["P0", "P1", "P2", "P3"],
        require_priority_for_ready=True,
        milestones=["v1.0 Launch", "v1.1 Hardening"],
        facets={
            "domain": config.FacetSpec(
                name="domain",
                prefix="domain:",
                required_for_ready=True,
                multiple=True,
                color="1d76db",
                values=["auth", "api", "storage"],
            ),
            "type": config.FacetSpec(
                name="type",
                prefix="type:",
                required_for_ready=True,
                multiple=False,
                color="0e8a16",
                values=["feature", "bug", "refactor", "spike", "doc", "chore"],
            ),
            "stage": config.FacetSpec(
                name="stage",
                prefix="stage:",
                required_for_ready=True,
                multiple=False,
                color="fbca04",
                values=["alpha", "beta", "ga"],
            ),
        },
    )
    rendered = config.render_board_config_toml(cfg)
    parsed = tomllib.loads(rendered)
    jsonschema.validate(parsed, schema)
    reloaded = config.BoardConfig.from_mapping(parsed)
    assert reloaded.issues_repo == "meta"
    assert reloaded.priorities == ["P0", "P1", "P2", "P3"]
    assert "refactor" in reloaded.facets["type"].values

    errs = validate_ready_gate(["domain:auth"], "", reloaded.facets, True)
    assert len(errs) == 3  # missing priority, type:*, stage:*

    ok_errs = validate_ready_gate(
        ["domain:storage", "type:refactor", "stage:beta"],
        "P1",
        reloaded.facets,
        True,
    )
    assert ok_errs == []

    t1 = Ticket(number=1, title="Foundation", column=Column.IN_PROGRESS)
    t2 = Ticket(
        number=2,
        title="Child work",
        body="Depends on: #1",
        column=Column.READY,
    )
    t3 = Ticket(
        number=3,
        title="Downstream",
        deposit=DepositMeta(deposited_by="jelle", branch="main", depends_on=[2]),
        column=Column.READY,
    )
    populate_dependency_waves([t1, t2, t3])
    assert t1.wave == 0
    assert t2.unmet_deps == [1]
    assert t2.wave == 1
    assert t3.unmet_deps == [2]
    assert t3.wave == 2

    # Completing t1 shifts t2 to wave 0 and t3 to wave 1
    t1.column = Column.DONE
    populate_dependency_waves([t1, t2, t3])
    assert t2.unmet_deps == []
    assert t2.wave == 0
    assert t3.wave == 1

    cyc = detect_dependency_cycle({1: {2}, 2: {3}, 3: {1}})
    assert cyc == [1, 2, 3, 1]
    assert detect_dependency_cycle({1: set(), 2: {1}, 3: {2}}) is None
