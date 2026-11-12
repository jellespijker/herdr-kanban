"""End-to-end tests for Umbrella Multi-Repository Workspaces (ADR-0007)."""

from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from herdr_kanban import cli, config, terminal
from herdr_kanban.engine import KanbanEngine, KanbanError
from herdr_kanban.github import ProjectSchema
from herdr_kanban.model import (
    Column,
    Ticket,
    decode_meta,
    is_lease_expired,
    match_column,
    parse_ticket_ref,
    populate_file_overlaps,
    resolve_winning_claim,
)
from herdr_kanban.tui import BoardTUI

ROOT = Path(__file__).resolve().parents[1]


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


class MultiRepoFakeGitHub:
    """Multi-repository GitHub Projects v2 + Issues + Clone fake."""

    def __init__(self, bare_remotes: dict[tuple[str, str], Path], login: str = "jelle") -> None:
        self.login = login
        self.bare_remotes = bare_remotes
        self.collaborators = {"jelle", "colleague"}
        self.next_comment = 5000
        self.next_item = 100
        self.issues: dict[tuple[str, str, int], dict[str, Any]] = {}
        self.comments: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
        self.status_by_item: dict[str, str] = {}
        self.item_by_issue: dict[tuple[str, str, int], str] = {}
        self.schema = ProjectSchema(
            project_id="PVT_UMB",
            number=7,
            title="Acme Platform Board",
            url="https://github.com/orgs/acme/projects/7",
            status_field_id="PVTSSF_UMB",
            options_by_name={
                "Triage": "OPT_TRIAGE",
                "Ready": "OPT_READY",
                "In Progress": "OPT_INPROG",
                "Blocked": "OPT_BLOCKED",
                "Review": "OPT_REVIEW",
                "Done": "OPT_DONE",
            },
        )

    def seed_issue(
        self,
        owner: str,
        repo: str,
        number: int,
        title: str,
        status: str = "Ready",
        body: str = "",
    ) -> None:
        key = (owner.lower(), repo.lower(), number)
        self.next_item += 1
        item_id = f"PVTI_{self.next_item}"
        self.issues[key] = {
            "owner": owner,
            "repo": repo,
            "number": number,
            "node_id": f"NODE_{owner}_{repo}_{number}",
            "title": title,
            "body": body,
            "state": "OPEN",
            "html_url": f"https://github.com/{owner}/{repo}/issues/{number}",
            "assignees": [],
            "labels": [],
        }
        self.comments.setdefault(key, [])
        self.item_by_issue[key] = item_id
        self.status_by_item[item_id] = status

    def current_user(self) -> str:
        return self.login

    def auth_check(self) -> tuple[bool, str]:
        return True, f"Logged in as {self.login}"

    def clone_repo(self, owner: str, repo: str, dest: Path) -> None:
        bare = self.bare_remotes[(owner.lower(), repo.lower())]
        dest.parent.mkdir(parents=True, exist_ok=True)
        _git(dest.parent, "clone", str(bare), str(dest))
        _git(dest, "remote", "set-url", "--push", "origin", str(bare))

    def fetch_board(
        self,
        *,
        column_map: dict[str, str],
        allowed_repos: dict[tuple[str, str], str] | None = None,
        repo_owner: str = "",
        repo_name: str = "",
        **_kwargs: Any,
    ) -> tuple[ProjectSchema, list[Ticket]]:
        tickets: list[Ticket] = []
        for key, issue in sorted(self.issues.items()):
            own_lc, rep_lc, num = key
            alias = ""
            if allowed_repos is not None:
                if (own_lc, rep_lc) not in allowed_repos:
                    continue
                alias = allowed_repos[(own_lc, rep_lc)]
            else:
                if repo_owner and own_lc != repo_owner.lower():
                    continue
                if repo_name and rep_lc != repo_name.lower():
                    continue

            comms = self.comments.get(key, [])
            win = resolve_winning_claim(comms)
            claim = win[1] if win else None
            cid = win[0] if win else None
            item_id = self.item_by_issue[key]
            status_name = self.status_by_item.get(item_id, "Triage")
            col = match_column(status_name, column_map)
            if issue["state"] == "CLOSED":
                col = Column.DONE

            tickets.append(
                Ticket(
                    number=num,
                    title=issue["title"],
                    body=issue["body"],
                    state=issue["state"],
                    url=issue["html_url"],
                    author=self.login,
                    assignees=list(issue["assignees"]),
                    column=col,
                    status_name=status_name,
                    project_item_id=item_id,
                    repo_owner=issue["owner"],
                    repo_name=issue["repo"],
                    repo_alias=alias,
                    claim=claim,
                    claim_comment_id=cid,
                    stale=bool(claim and claim.state == "active" and is_lease_expired(claim.lease_until)),
                )
            )
        populate_file_overlaps(tickets)
        tickets.sort(key=lambda t: (t.repo_alias or t.repo_name, t.number))
        return self.schema, tickets

    def set_item_column(
        self,
        schema: ProjectSchema,
        project_item_id: str,
        column: Column,
        column_map: dict[str, str],
    ) -> bool:
        opt_id = schema.option_id_for_column(column, column_map)
        if not opt_id:
            return False
        for name, oid in schema.options_by_name.items():
            if oid == opt_id:
                self.status_by_item[project_item_id] = name
                return True
        return False

    def create_issue(
        self,
        owner: str,
        repo: str,
        title: str,
        body: str = "",
        labels: list[str] | None = None,
    ) -> dict[str, Any]:
        existing_nums = [num for (o, r, num) in self.issues if o == owner.lower() and r == repo.lower()]
        next_num = max(existing_nums, default=20) + 1
        self.seed_issue(owner, repo, next_num, title, status="Triage", body=body)
        key = (owner.lower(), repo.lower(), next_num)
        if labels:
            self.issues[key]["labels"] = list(labels)
        return self.issues[key]

    def add_issue_to_project(self, _project_id: str, issue_node_id: str) -> str:
        for key, issue in self.issues.items():
            if issue["node_id"] == issue_node_id:
                return self.item_by_issue[key]
        return ""

    def close_issue(self, owner: str, repo: str, number: int) -> None:
        self.issues[(owner.lower(), repo.lower(), number)]["state"] = "CLOSED"

    def assign_issue(self, owner: str, repo: str, number: int, login: str) -> None:
        assignees = self.issues[(owner.lower(), repo.lower(), number)]["assignees"]
        if login not in assignees:
            assignees.append(login)

    def unassign_issue(self, owner: str, repo: str, number: int, login: str) -> None:
        key = (owner.lower(), repo.lower(), number)
        self.issues[key]["assignees"] = [a for a in self.issues[key]["assignees"] if a != login]

    def list_issue_comments(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]:
        return list(self.comments.get((owner.lower(), repo.lower(), number), []))

    def post_comment(
        self, owner: str, repo: str, number: int, body: str, author: str | None = None
    ) -> dict[str, Any]:
        cid = self.next_comment
        self.next_comment += 1
        key = (owner.lower(), repo.lower(), number)
        row = {
            "id": cid,
            "body": body,
            "created_at": "2026-11-12T10:00:00Z",
            "author": author or self.login,
            "url": f"https://github.com/{owner}/{repo}/issues/{number}#issuecomment-{cid}",
        }
        self.comments.setdefault(key, []).append(row)
        return row

    def patch_comment(self, owner: str, repo: str, comment_id: int, body: str) -> None:
        for (o, r, _), rows in self.comments.items():
            if o == owner.lower() and r == repo.lower():
                for row in rows:
                    if row["id"] == comment_id:
                        row["body"] = body
                        return

    def delete_comment(self, owner: str, repo: str, comment_id: int) -> None:
        for key, rows in list(self.comments.items()):
            if key[0] == owner.lower() and key[1] == repo.lower():
                self.comments[key] = [r for r in rows if r["id"] != comment_id]

    def trusted_collaborators(self, _owner: str, _repo: str, extra: list[str] | None = None) -> set[str]:
        return self.collaborators | {e.lower() for e in (extra or [])}


class MultiRepoFakeHerdr:
    def __init__(self) -> None:
        self.agent_states: dict[str, str] = {}
        self.prompts_sent: list[tuple[str, str]] = []
        self._seq = 0

    def available(self) -> bool:
        return True

    def current_pane_cwd(self) -> Path | None:
        return None

    def spawn_ticket_agent(
        self,
        *,
        worktree_path: Path,
        issue_number: int,
        repo_alias: str = "",
        prompt_text: str,
        **_kwargs: Any,
    ) -> dict[str, str]:
        self._seq += 1
        pane_id = f"w1:p{self._seq}"
        agent_name = f"{repo_alias}-{issue_number}" if repo_alias else f"issue-{issue_number}"
        self.agent_states[agent_name] = "idle"
        self.agent_states[pane_id] = "idle"
        self.prompts_sent.append((agent_name, prompt_text))
        assert worktree_path.is_dir()
        return {
            "workspace_id": "w1",
            "tab_id": f"w1:t{self._seq}",
            "pane_id": pane_id,
            "agent_name": agent_name,
        }

    def prompt_if_idle(self, target: str, message: str, machine: str | None = None) -> bool:
        if self.agent_states.get(target, "idle") not in ("idle", "done"):
            return False
        self.prompts_sent.append((f"{machine}:{target}" if machine else target, message))
        return True

    def notify(self, _title: str, _body: str = "") -> None:
        pass


@pytest.fixture()
def umbrella_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HERDR_KANBAN_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HERDR_PLUGIN_CONFIG_DIR", str(tmp_path / "plugin-cfg"))
    wt_root = tmp_path / "worktrees"
    config.save_user_settings(config.UserSettings(default_agent_kind="claude", worktree_root=str(wt_root)))

    remotes_dir = tmp_path / "remotes"
    umbrella = tmp_path / "acme-platform"
    umbrella.mkdir(parents=True)

    bare_remotes: dict[tuple[str, str], Path] = {}
    for name, seed_file, content in (
        ("firmware", "include/proto.h", "#define TELEMETRY_V2 1\n"),
        ("cloud", "service/ingest.py", "SCHEMA_VERSION = 2\n"),
    ):
        bare = remotes_dir / f"{name}.git"
        _git(tmp_path, "init", "--bare", str(bare))
        bare_remotes[("acme", name)] = bare

        checkout = umbrella / name
        _git(tmp_path, "clone", str(bare), str(checkout))
        _git(checkout, "config", "user.email", "dev@acme.test")
        _git(checkout, "config", "user.name", "Acme Dev")
        _git(checkout, "checkout", "-b", "feat/telemetry-v2")
        target_file = checkout / seed_file
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_text(content, encoding="utf-8")
        _git(checkout, "add", ".")
        _git(checkout, "commit", "-m", f"Seed {name}")
        _git(checkout, "push", "-u", "origin", "feat/telemetry-v2")
        # Set fetch URL to GitHub-shaped URL for `init` discovery while keeping local bare push URL!
        # Wait: git uses fetch URL unless pushurl is set; to let real git fetch & push locally after
        # init discovery, we'll run `init` with github URLs and then point origin back to local bare!

    gh = MultiRepoFakeGitHub(bare_remotes)
    herdr_fake = MultiRepoFakeHerdr()
    engine = KanbanEngine(github=gh, herdr=herdr_fake)  # type: ignore[arg-type]
    return tmp_path, umbrella, bare_remotes, gh, herdr_fake, engine


def test_parse_ticket_ref_variants() -> None:
    assert parse_ticket_ref("42") == (None, 42)
    assert parse_ticket_ref("#42") == (None, 42)
    assert parse_ticket_ref("cloud#42") == ("cloud", 42)
    assert parse_ticket_ref("acme/cloud#42") == ("acme/cloud", 42)
    with pytest.raises(ValueError):
        parse_ticket_ref("cloud#0")
    with pytest.raises(ValueError):
        parse_ticket_ref("not-a-ref")


def test_umbrella_init_discovery_disambiguation_siblings_and_landing(umbrella_env) -> None:
    _tmp_path, umbrella, bare_remotes, gh, herdr_fake, engine = umbrella_env

    # Temporarily set GitHub-shaped origin URLs so `cmd_init` auto-discovers owner/repo cleanly
    for name in ("firmware", "cloud"):
        _git(umbrella / name, "remote", "set-url", "origin", f"git@github.com:acme/{name}.git")

    parser = cli.build_parser()
    init_args = parser.parse_args(
        [
            "init",
            "--cwd",
            str(umbrella),
            "--project-number",
            "7",
            "--branch",
            "feat/telemetry-v2",
        ]
    )
    assert cli.cmd_init(init_args, engine) == 0

    # Restore local bare remote URLs so real `git fetch` / `git push` work offline
    for name in ("firmware", "cloud"):
        _git(umbrella / name, "remote", "set-url", "origin", str(bare_remotes[("acme", name)]))

    # Validate generated umbrella `.herdr-kanban.toml` against board-config.v1.json schema!
    board_schema = json.loads((ROOT / "schema" / "board-config.v1.json").read_text(encoding="utf-8"))
    parsed_toml = tomllib.loads((umbrella / ".herdr-kanban.toml").read_text(encoding="utf-8"))
    jsonschema.validate(instance=parsed_toml, schema=board_schema)
    assert set(parsed_toml["repos"].keys()) == {"cloud", "firmware"}

    # Seed colliding issue #10 in BOTH repos + unique issue #18 in cloud!
    gh.seed_issue("acme", "firmware", 10, "Emit sequence_id in packet header")
    gh.seed_issue("acme", "cloud", 10, "Ingest sequence_id in telemetry pipeline")
    gh.seed_issue("acme", "cloud", 18, "Add Prometheus counter for dropped packets")

    ctx_umbrella = engine.resolve_context(umbrella)
    assert ctx_umbrella.is_multi_repo is True

    # 1. Bare `10` from umbrella root MUST raise a clear disambiguation error
    with pytest.raises(KanbanError, match="Ambiguous issue #10"):
        engine.claim_ticket(ctx_umbrella, "10")

    # 2. Bare `18` from umbrella root resolves unambiguously to `cloud#18`!
    entry_18, num_18 = engine.resolve_ticket_target(ctx_umbrella, "18")
    assert (entry_18.alias, num_18) == ("cloud", 18)

    # 3. Bare `10` from INSIDE `umbrella/cloud` resolves to `cloud#10` via active_repo_alias!
    ctx_cloud_sub = engine.resolve_context(umbrella / "cloud")
    entry_c10, num_c10 = engine.resolve_ticket_target(ctx_cloud_sub, "10")
    assert (entry_c10.alias, num_c10) == ("cloud", 10)

    # 4. Dispatch BOTH `firmware#10` and `cloud#10` concurrently -> zero collision!
    disp_fw = engine.dispatch_ticket(ctx_umbrella, "firmware#10", agent_kind="claude")
    disp_cl = engine.dispatch_ticket(ctx_umbrella, "cloud#10", agent_kind="agy")
    wt_fw = Path(disp_fw.claim.worktree_path)
    wt_cl = Path(disp_cl.claim.worktree_path)
    assert wt_fw != wt_cl
    assert disp_fw.claim.agent_name == "firmware-10"
    assert disp_cl.claim.agent_name == "cloud-10"

    # 5. Verify `.siblings/` symlinks give cross-repo read access while `git status` stays clean!
    sib_proto = wt_cl / ".siblings" / "firmware" / "include" / "proto.h"
    assert sib_proto.is_file()
    assert "TELEMETRY_V2" in sib_proto.read_text(encoding="utf-8")
    assert engine.git.is_clean(wt_cl) is True
    assert ".siblings" not in engine.git.touched_files(wt_cl, "origin/feat/telemetry-v2")

    # 6. Cross-repo `ask`: firmware#10 asks cloud#10 a question -> wakes `cloud-10` & validates schema!
    prompts_before = len(herdr_fake.prompts_sent)
    ask_res = engine.ask_ticket(
        ctx_umbrella,
        "cloud#10",
        "Are you expecting big-endian uint32 for sequence_id?",
        from_issue="firmware#10",
    )
    assert ask_res["delivered_immediately"] is True
    assert ask_res["to_ref"] == "cloud#10"
    assert len(herdr_fake.prompts_sent) == prompts_before + 1
    comment_meta_schema = json.loads((ROOT / "schema" / "comment-meta.v1.json").read_text(encoding="utf-8"))
    relay_comment = gh.list_issue_comments("acme", "cloud", 10)[-1]
    relay_meta = decode_meta(relay_comment["body"])
    assert relay_meta is not None and relay_meta["from_repo"] == "firmware"
    jsonschema.validate(instance=relay_meta, schema=comment_meta_schema)

    # 7. Cross-repo `deposit`: discovered cloud bug while working on firmware#10!
    dep = engine.deposit_issue(
        ctx_umbrella,
        "Validate packet CRC before parsing sequence_id",
        parent_issue="firmware#10",
        repo_selector="cloud",
        file_line="service/ingest.py:12",
    )
    assert dep.repo_alias == "cloud"
    assert dep.deposit is not None and dep.deposit.parent_repo == "firmware"
    dep_meta = decode_meta(dep.body)
    jsonschema.validate(instance=dep_meta, schema=comment_meta_schema)

    # 8. Commit and land `cloud#10` (even when invoked from INSIDE the ephemeral worktree!)
    _git(wt_cl, "config", "user.email", "agy@acme.test")
    _git(wt_cl, "config", "user.name", "Agy")
    (wt_cl / "service" / "ingest.py").write_text(
        "SCHEMA_VERSION = 2\nSUPPORT_SEQ_ID = True\n", encoding="utf-8"
    )
    _git(wt_cl, "add", "service/ingest.py")
    _git(wt_cl, "commit", "-m", "Support sequence_id in cloud ingest")

    ctx_from_ephemeral_wt = engine.resolve_context(wt_cl)
    assert ctx_from_ephemeral_wt.is_multi_repo is True
    land_cl = engine.complete_ticket(ctx_from_ephemeral_wt, "10", summary="Cloud ingest landed")
    assert land_cl.status == "landed"
    assert land_cl.ref() == "cloud#10"
    # cloud#10 is CLOSED while firmware#10 is still OPEN, and firmware's files are untouched!
    assert gh.issues[("acme", "cloud", 10)]["state"] == "CLOSED"
    assert gh.issues[("acme", "firmware", 10)]["state"] == "OPEN"
    assert (umbrella / "firmware" / "include" / "proto.h").is_file()


def test_sync_repos_clones_and_aligns_colleague_umbrella_workspace(umbrella_env) -> None:
    tmp_path, umbrella, bare_remotes, gh, _herdr_fake, engine = umbrella_env

    # Write umbrella config into a brand-new colleague workspace that has NO child repos cloned yet!
    colleague_umbrella = tmp_path / "colleague-acme"
    colleague_umbrella.mkdir(parents=True)
    cfg = config.BoardConfig(
        project_owner="acme",
        project_number=7,
        target_branch="feat/telemetry-v2",
        repos={
            "firmware": config.RepoEntry(alias="firmware", owner="acme", repo="firmware", path="firmware"),
            "cloud": config.RepoEntry(alias="cloud", owner="acme", repo="cloud", path="cloud"),
        },
    )
    (colleague_umbrella / ".herdr-kanban.toml").write_text(
        config.render_board_config_toml(cfg), encoding="utf-8"
    )

    ctx_colleague = engine.resolve_context(colleague_umbrella)
    synced = engine.sync_repos(ctx_colleague)
    assert len(synced) == 2
    assert all("cloned" in row["status"] and "synced" in row["status"] for row in synced)
    assert (colleague_umbrella / "firmware" / "include" / "proto.h").is_file()
    assert (colleague_umbrella / "cloud" / "service" / "ingest.py").is_file()
    assert engine.git.current_branch(colleague_umbrella / "firmware") == "feat/telemetry-v2"
    assert engine.git.current_branch(colleague_umbrella / "cloud") == "feat/telemetry-v2"

    # Push a new commit from first dev's `firmware` repo, then re-run `sync_repos` on colleague!
    (umbrella / "firmware" / "include" / "v3.h").write_text("#define V3 1\n", encoding="utf-8")
    _git(umbrella / "firmware", "add", ".")
    _git(umbrella / "firmware", "commit", "-m", "Add v3 header")
    _git(umbrella / "firmware", "push", str(bare_remotes[("acme", "firmware")]), "feat/telemetry-v2")

    synced_again = engine.sync_repos(ctx_colleague)
    assert all(row["status"] == "synced" for row in synced_again)
    assert (colleague_umbrella / "firmware" / "include" / "v3.h").is_file()


def test_tui_renders_multi_repo_prefixes_and_distinct_colliding_numbers(umbrella_env) -> None:
    _tmp_path, umbrella, _bare_remotes, gh, _herdr_fake, engine = umbrella_env
    cfg = config.BoardConfig(
        project_owner="acme",
        project_number=7,
        target_branch="feat/telemetry-v2",
        repos={
            "firmware": config.RepoEntry(alias="firmware", owner="acme", repo="firmware", path="firmware"),
            "cloud": config.RepoEntry(alias="cloud", owner="acme", repo="cloud", path="cloud"),
        },
    )
    (umbrella / ".herdr-kanban.toml").write_text(config.render_board_config_toml(cfg), encoding="utf-8")

    gh.seed_issue("acme", "firmware", 10, "Firmware packet framing")
    gh.seed_issue("acme", "cloud", 10, "Cloud packet parser")
    ctx = engine.resolve_context(umbrella)
    engine.dispatch_ticket(ctx, "firmware#10", agent_kind="claude")

    tui = BoardTUI(engine, cwd=umbrella)
    tui.load_initial_state()
    _, tui.tickets = engine.refresh_board(ctx)
    tui._reload_local_overlays()
    try:
        frame = terminal.ANSI_ESCAPE_RE.sub("", tui.render_frame(140, 32))
        assert "acme-platform (2 repos)" in frame
        assert "firmware#10" in frame
        assert "cloud#10" in frame
        # Pressing `n` in multi-repo mode opens the repo picker first, then `2` selects `cloud`
        tui.handle_event(terminal.InputEvent(kind="key", key="n"), 140, 32)
        assert tui.prompt is not None and tui.prompt.mode == "pick_repo"
        tui.handle_event(terminal.InputEvent(kind="key", key="2"), 140, 32)
        assert tui.prompt is not None
        assert tui.prompt.mode == "new_ticket"
        assert tui.prompt.repo_alias == "cloud"
    finally:
        tui.close_pipes()
