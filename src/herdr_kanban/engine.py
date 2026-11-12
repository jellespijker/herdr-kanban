"""Core workflow coordinator: claims, worktrees, landing gate, and comment relays."""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config, state
from .git_ops import GitClient, GitError
from .github import GitHubClient, GitHubError, ProjectSchema
from .herdr import HerdrClient
from .model import (
    ClaimMeta,
    Column,
    DepositMeta,
    FileOverlap,
    InboxItem,
    RelayMeta,
    Ticket,
    compute_file_overlaps,
    decode_meta,
    is_lease_expired,
    lease_until_iso,
    render_claim_comment,
    render_deposit_body,
    render_relay_comment,
    resolve_winning_claim,
    strip_meta,
    utc_now_iso,
)


class KanbanError(RuntimeError):
    """Base error for actionable workflow failures."""


class ConfigNotFoundError(KanbanError):
    """Raised when no `.herdr-kanban.toml` or local repo config exists."""


class ClaimConflictError(KanbanError):
    """Raised when a ticket is actively held by another unexpired lease."""


class ClaimStaleError(KanbanError):
    """Raised when a ticket has an expired lease and `--takeover` was not set."""


class ClaimRaceLostError(KanbanError):
    """Raised when a simultaneous claim won on GitHub comment ordering."""


class LandingGateError(KanbanError):
    """Raised when rebase, check_command, or fast-forward push cannot complete."""


@dataclass(slots=True)
class RepoContext:
    worktree_cwd: Path
    main_repo_root: Path
    repo_owner: str
    repo_name: str
    slug: str
    board: config.BoardConfig
    settings: config.UserSettings


@dataclass(slots=True)
class ClaimResult:
    claim: state.LocalClaim
    ticket: Ticket
    overlaps: list[FileOverlap] = field(default_factory=list)
    reused: bool = False


@dataclass(slots=True)
class LandingResult:
    issue_number: int
    status: str  # landed | review_pending
    target_branch: str
    commit_sha: str
    closed_issue: bool = False
    worktree_removed: bool = False


class KanbanEngine:
    def __init__(
        self,
        *,
        git: GitClient | None = None,
        github: GitHubClient | None = None,
        herdr: HerdrClient | None = None,
    ) -> None:
        self.git = git or GitClient()
        self.github = github or GitHubClient()
        self.herdr = herdr or HerdrClient()

    def resolve_cwd(self, explicit_cwd: Path | None = None) -> Path:
        if explicit_cwd is not None:
            return explicit_cwd.resolve()
        env_repo = os.environ.get("HERDR_KANBAN_REPO")
        if env_repo and Path(env_repo).is_dir():
            return Path(env_repo).resolve()
        cwd = Path.cwd().resolve()
        if cwd != config.ROOT.resolve():
            return cwd
        pane_cwd = self.herdr.current_pane_cwd()
        if pane_cwd and pane_cwd.is_dir():
            return pane_cwd.resolve()
        return cwd

    def resolve_context(self, explicit_cwd: Path | None = None) -> RepoContext:
        start = self.resolve_cwd(explicit_cwd)
        wt_root = self.git.worktree_root(start)
        if wt_root is None:
            raise KanbanError(f"{start} is not inside a Git repository")
        main_root = self.git.main_repo_root(wt_root) or wt_root
        owner_repo = self.git.parse_github_remote(main_root)
        board = config.load_board_config(wt_root, main_root, owner_repo)
        if board is None:
            raise ConfigNotFoundError(
                "No `.herdr-kanban.toml` found for this repository. Run `herdr-kanban init` first."
            )
        repo_owner = board.repo_owner or (owner_repo[0] if owner_repo else board.project_owner)
        repo_name = board.repo_name or (owner_repo[1] if owner_repo else main_root.name)
        board.repo_owner = repo_owner
        board.repo_name = repo_name
        slug = config.repo_slug(repo_owner, repo_name)
        return RepoContext(
            worktree_cwd=wt_root,
            main_repo_root=main_root,
            repo_owner=repo_owner,
            repo_name=repo_name,
            slug=slug,
            board=board,
            settings=config.load_user_settings(),
        )

    def refresh_board(self, ctx: RepoContext) -> tuple[ProjectSchema, list[Ticket]]:
        schema, tickets = self.github.fetch_board(
            project_owner=ctx.board.project_owner,
            project_number=ctx.board.project_number,
            repo_owner=ctx.repo_owner,
            repo_name=ctx.repo_name,
            column_map=ctx.board.columns,
            label_filter=ctx.board.label_filter,
        )
        state.save_board_cache(
            ctx.slug,
            ctx.board.project_number,
            [self._serialize_cached_ticket(t) for t in tickets],
        )
        return schema, tickets

    def load_cached_tickets(self, ctx: RepoContext) -> tuple[float, list[Ticket]] | None:
        cached = state.load_board_cache(ctx.slug, ctx.board.project_number)
        if cached is None:
            return None
        fetched_at, rows = cached
        tickets = [self._deserialize_cached_ticket(r) for r in rows if isinstance(r, dict)]
        overlaps = compute_file_overlaps(tickets)
        for t in tickets:
            t.overlaps = overlaps.get(t.number, [])
        return fetched_at, tickets

    def claim_ticket(
        self,
        ctx: RepoContext,
        issue_number: int,
        *,
        agent_kind: str | None = None,
        agent_name: str = "",
        pane_id: str = "",
        risk_gate: bool = False,
        takeover: bool = False,
    ) -> ClaimResult:
        chosen_kind = agent_kind or ctx.settings.default_agent_kind
        schema, tickets = self.refresh_board(ctx)
        ticket = next((t for t in tickets if t.number == issue_number), None)
        if ticket is None:
            raise KanbanError(f"Issue #{issue_number} is not on GitHub Project #{ctx.board.project_number}")

        existing_local = state.get_local_claim(ctx.slug, issue_number)
        if existing_local and Path(existing_local.worktree_path).is_dir():
            return ClaimResult(
                claim=existing_local,
                ticket=ticket,
                overlaps=ticket.overlaps,
                reused=True,
            )

        comments = self.github.list_issue_comments(ctx.repo_owner, ctx.repo_name, issue_number)
        winning = resolve_winning_claim(comments)
        takeover_of: int | None = None
        if winning is not None:
            win_cid, win_claim = winning
            expired = is_lease_expired(win_claim.lease_until)
            if not expired:
                raise ClaimConflictError(
                    f"Issue #{issue_number} is already claimed by @{win_claim.owner} "
                    f"({win_claim.agent_kind}) until {win_claim.lease_until}"
                )
            if not takeover:
                raise ClaimStaleError(
                    f"Issue #{issue_number} has a STALE claim by @{win_claim.owner} "
                    f"(expired {win_claim.lease_until}). Re-run with `--takeover` to reclaim."
                )
            takeover_of = win_cid

        user = self.github.current_user()
        wt_base = ctx.settings.resolved_worktree_root() / ctx.slug / f"issue-{issue_number}"
        had_wt = wt_base.exists()
        self.git.ensure_remote_branch(ctx.main_repo_root, ctx.board.remote, ctx.board.target_branch)
        wt_path, local_branch = self.git.create_ticket_worktree(
            ctx.main_repo_root,
            wt_base,
            ctx.board.remote,
            ctx.board.target_branch,
            issue_number,
        )

        now_iso = utc_now_iso()
        lease_iso = lease_until_iso(ctx.board.lease_ttl_minutes)
        caller_pane = pane_id or os.environ.get("HERDR_PANE_ID", "")
        meta = ClaimMeta(
            issue_number=issue_number,
            owner=user,
            agent_kind=chosen_kind,
            agent_name=agent_name,
            state="active",
            started_at=now_iso,
            heartbeat_at=now_iso,
            lease_until=lease_iso,
            herdr_session=state.herdr_session_key(),
            pane_id=caller_pane,
            branch=ctx.board.target_branch,
            risk_gate=risk_gate or ctx.board.require_review,
            takeover_of=takeover_of,
        )

        posted = self.github.post_comment(
            ctx.repo_owner,
            ctx.repo_name,
            issue_number,
            render_claim_comment(meta),
        )
        my_cid = int(posted["id"])

        # Optimistic concurrency verification: earliest active comment ID wins.
        after_comments = self.github.list_issue_comments(ctx.repo_owner, ctx.repo_name, issue_number)
        verified = resolve_winning_claim(after_comments)
        if verified is None or verified[0] != my_cid:
            self.github.delete_comment(ctx.repo_owner, ctx.repo_name, my_cid)
            if not had_wt and self.git.is_clean(wt_path):
                self.git.remove_ticket_worktree(ctx.main_repo_root, wt_path, local_branch)
            holder = f"@{verified[1].owner}" if verified else "another agent"
            raise ClaimRaceLostError(
                f"Lost simultaneous claim race on #{issue_number} to {holder}; claim rolled back."
            )

        try:
            self.github.assign_issue(ctx.repo_owner, ctx.repo_name, issue_number, user)
        except GitHubError:
            pass
        if ticket.project_item_id:
            self.github.set_item_column(
                schema,
                ticket.project_item_id,
                Column.IN_PROGRESS,
                ctx.board.columns,
            )

        local_claim = state.LocalClaim(
            repo_slug=ctx.slug,
            repo_owner=ctx.repo_owner,
            repo_name=ctx.repo_name,
            main_repo_root=str(ctx.main_repo_root),
            issue_number=issue_number,
            worktree_path=str(wt_path),
            local_branch=local_branch,
            target_branch=ctx.board.target_branch,
            remote=ctx.board.remote,
            comment_id=my_cid,
            owner=user,
            agent_kind=chosen_kind,
            agent_name=agent_name,
            pane_id=caller_pane,
            workspace_id=os.environ.get("HERDR_WORKSPACE_ID", ""),
            herdr_session=state.herdr_session_key(),
            risk_gate=meta.risk_gate,
            started_at=now_iso,
        )
        state.save_local_claim(local_claim)
        if after_comments:
            state.set_watermark(ctx.slug, issue_number, max(int(c["id"]) for c in after_comments))

        ticket.claim = meta
        ticket.claim_comment_id = my_cid
        ticket.column = Column.IN_PROGRESS
        overlaps = compute_file_overlaps(tickets)
        return ClaimResult(
            claim=local_claim,
            ticket=ticket,
            overlaps=overlaps.get(issue_number, []),
            reused=False,
        )

    def dispatch_ticket(
        self,
        ctx: RepoContext,
        issue_number: int,
        *,
        agent_kind: str | None = None,
        risk_gate: bool = False,
        takeover: bool = False,
        focus: bool = True,
    ) -> ClaimResult:
        chosen_kind = agent_kind or ctx.settings.default_agent_kind
        res = self.claim_ticket(
            ctx,
            issue_number,
            agent_kind=chosen_kind,
            risk_gate=risk_gate,
            takeover=takeover,
        )
        prompt = self._build_agent_prompt(ctx, res.ticket, res.claim, res.overlaps)
        spawned = self.herdr.spawn_ticket_agent(
            worktree_path=Path(res.claim.worktree_path),
            issue_number=issue_number,
            title=res.ticket.title,
            agent_kind=chosen_kind,
            prompt_text=prompt,
            focus=focus,
        )
        res.claim.pane_id = spawned["pane_id"]
        res.claim.tab_id = spawned["tab_id"]
        res.claim.workspace_id = spawned["workspace_id"]
        res.claim.agent_name = spawned["agent_name"]
        state.save_local_claim(res.claim)

        if res.ticket.claim is not None:
            res.ticket.claim.pane_id = spawned["pane_id"]
            res.ticket.claim.agent_name = spawned["agent_name"]
            try:
                self.github.patch_comment(
                    ctx.repo_owner,
                    ctx.repo_name,
                    res.claim.comment_id,
                    render_claim_comment(res.ticket.claim),
                )
            except GitHubError:
                pass
        return res

    def release_ticket(
        self,
        ctx: RepoContext,
        issue_number: int,
        *,
        reason: str = "",
        force_remove_worktree: bool = False,
    ) -> None:
        schema, tickets = self.refresh_board(ctx)
        ticket = next((t for t in tickets if t.number == issue_number), None)
        local_claim = state.get_local_claim(ctx.slug, issue_number)

        cid = local_claim.comment_id if local_claim else (ticket.claim_comment_id if ticket else None)
        claim_meta = ticket.claim if (ticket and ticket.claim) else None
        if claim_meta is None and local_claim is not None:
            claim_meta = ClaimMeta(
                issue_number=issue_number,
                owner=local_claim.owner,
                agent_kind=local_claim.agent_kind,
                agent_name=local_claim.agent_name,
                started_at=local_claim.started_at,
                heartbeat_at=utc_now_iso(),
                lease_until=utc_now_iso(),
                branch=local_claim.target_branch,
            )

        if cid and claim_meta:
            claim_meta.state = "released"
            claim_meta.heartbeat_at = utc_now_iso()
            if reason:
                claim_meta.note = reason
            self.github.patch_comment(
                ctx.repo_owner,
                ctx.repo_name,
                cid,
                render_claim_comment(claim_meta),
            )

        if ticket and ticket.project_item_id:
            self.github.set_item_column(schema, ticket.project_item_id, Column.READY, ctx.board.columns)
        try:
            user = self.github.current_user()
            self.github.unassign_issue(ctx.repo_owner, ctx.repo_name, issue_number, user)
        except GitHubError:
            pass

        if local_claim:
            wt = Path(local_claim.worktree_path)
            if (
                wt.exists()
                and (force_remove_worktree or self.git.is_clean(wt))
                and Path.cwd().resolve() != wt.resolve()
            ):
                self.git.remove_ticket_worktree(
                    Path(local_claim.main_repo_root),
                    wt,
                    local_claim.local_branch,
                    force=force_remove_worktree,
                )
            state.remove_local_claim(ctx.slug, issue_number)

    def update_progress(
        self,
        ctx: RepoContext,
        issue_number: int,
        *,
        note: str | None = None,
        checklist: list[str] | None = None,
    ) -> ClaimMeta:
        local_claim = state.get_local_claim(ctx.slug, issue_number)
        comments = self.github.list_issue_comments(ctx.repo_owner, ctx.repo_name, issue_number)
        winning = resolve_winning_claim(comments)
        if winning is None:
            raise KanbanError(f"Issue #{issue_number} has no active claim to update")
        cid, meta = winning

        wt = Path(local_claim.worktree_path) if local_claim else ctx.worktree_cwd
        base_ref = f"{ctx.board.remote}/{ctx.board.target_branch}"
        if wt.is_dir():
            meta.touched_files = self.git.touched_files(wt, base_ref)
        meta.heartbeat_at = utc_now_iso()
        meta.lease_until = lease_until_iso(ctx.board.lease_ttl_minutes)
        if note is not None:
            meta.note = note
        if checklist is not None:
            meta.checklist = checklist

        self.github.patch_comment(ctx.repo_owner, ctx.repo_name, cid, render_claim_comment(meta))
        return meta

    def deposit_issue(
        self,
        ctx: RepoContext,
        title: str,
        *,
        body: str = "",
        parent_issue: int | None = None,
        file_line: str = "",
        ready: bool = False,
    ) -> Ticket:
        user = self.github.current_user()
        deposit = DepositMeta(
            deposited_by=user,
            branch=ctx.board.target_branch,
            parent_issue=parent_issue,
            file_line=file_line,
            commit_sha=self.git.head_sha(ctx.worktree_cwd),
        )
        full_body = render_deposit_body(body, deposit)
        labels = [ctx.board.label_filter] if ctx.board.label_filter else None
        created = self.github.create_issue(
            ctx.repo_owner,
            ctx.repo_name,
            title,
            body=full_body,
            labels=labels,
        )
        issue_number = int(created["number"])
        node_id = str(created.get("node_id") or "")
        url = str(created.get("html_url") or "")

        schema, _ = self.refresh_board(ctx)
        target_col = Column.READY if (ready or ctx.board.auto_ready_deposits) else Column.TRIAGE
        item_id = ""
        if node_id:
            item_id = self.github.add_issue_to_project(schema.project_id, node_id)
            if item_id:
                self.github.set_item_column(schema, item_id, target_col, ctx.board.columns)

        return Ticket(
            number=issue_number,
            title=title,
            body=full_body,
            state="OPEN",
            url=url,
            author=user,
            labels=labels or [],
            column=target_col,
            project_item_id=item_id,
            deposit=deposit,
        )

    def move_ticket(self, ctx: RepoContext, issue_number: int, column: Column) -> Ticket:
        schema, tickets = self.refresh_board(ctx)
        ticket = next((t for t in tickets if t.number == issue_number), None)
        if ticket is None or not ticket.project_item_id:
            raise KanbanError(f"Issue #{issue_number} not found on Project #{ctx.board.project_number}")
        ok = self.github.set_item_column(schema, ticket.project_item_id, column, ctx.board.columns)
        if not ok:
            raise KanbanError(
                f"Project #{ctx.board.project_number} has no Status option mapped to column '{column.value}'"
            )
        ticket.column = column
        return ticket

    def post_comment(self, ctx: RepoContext, issue_number: int, body: str) -> dict[str, Any]:
        return self.github.post_comment(ctx.repo_owner, ctx.repo_name, issue_number, body)

    def ask_ticket(
        self,
        ctx: RepoContext,
        to_issue: int,
        message: str,
        *,
        from_issue: int | None = None,
        urgent: bool = False,
    ) -> dict[str, Any]:
        user = self.github.current_user()
        caller_claim = (
            state.get_local_claim(ctx.slug, from_issue)
            if from_issue
            else state.find_claim_by_cwd(ctx.worktree_cwd)
        )
        resolved_from = from_issue or (caller_claim.issue_number if caller_claim else None)
        from_agent = caller_claim.agent_kind if caller_claim else os.environ.get("HERDR_KANBAN_AGENT", "")

        relay = RelayMeta(
            from_owner=user,
            from_agent=from_agent,
            from_issue=resolved_from,
            to_issue=to_issue,
            created_at=utc_now_iso(),
            urgent=urgent,
        )
        posted = self.github.post_comment(
            ctx.repo_owner,
            ctx.repo_name,
            to_issue,
            render_relay_comment(relay, message),
        )
        cid = int(posted["id"])

        # Local / herdr-machine fast path when target ticket runs on our own session or saved machine.
        delivered_fast = False
        target_local = state.get_local_claim(ctx.slug, to_issue)
        wakeup_text = self._format_wakeup_prompt(to_issue, user, from_agent, resolved_from, message)
        if target_local is not None:
            target_handle = target_local.agent_name or target_local.pane_id
            delivered_fast = self.herdr.prompt_if_idle(target_handle, wakeup_text)
            state.append_inbox_item(
                ctx.slug,
                InboxItem(
                    comment_id=cid,
                    issue_number=to_issue,
                    author=user,
                    body=message.strip(),
                    created_at=relay.created_at,
                    from_agent=from_agent,
                    from_issue=resolved_from,
                    trusted=True,
                    delivered=delivered_fast,
                ),
            )
            state.set_watermark(ctx.slug, to_issue, cid)
        else:
            for machine in ctx.settings.machines:
                if self.herdr.prompt_if_idle(f"issue-{to_issue}", wakeup_text, machine=machine):
                    delivered_fast = True
                    break

        return {"comment_id": cid, "url": posted["url"], "delivered_immediately": delivered_fast}

    def complete_ticket(
        self,
        ctx: RepoContext,
        issue_number: int,
        *,
        summary: str = "",
        approve: bool = False,
        allow_empty: bool = False,
    ) -> LandingResult:
        local_claim = state.get_local_claim(ctx.slug, issue_number)
        wt = Path(local_claim.worktree_path) if local_claim else ctx.worktree_cwd
        if not wt.is_dir():
            raise LandingGateError(f"Worktree {wt} does not exist for issue #{issue_number}")

        if self.git.is_rebase_in_progress(wt):
            raise LandingGateError(
                f"A rebase is still in progress in {wt}. Resolve conflicts, run `git rebase --continue`, "
                f"and re-run `herdr-kanban done {issue_number}`."
            )
        if not self.git.is_clean(wt):
            raise LandingGateError(
                f"Worktree {wt} has uncommitted changes. Commit or clean them before calling `done`."
            )

        remote = ctx.board.remote
        target = ctx.board.target_branch
        self.git.fetch_branch(wt, remote, target)
        ahead = self.git.commit_count_ahead(wt, f"{remote}/{target}")
        if ahead == 0 and not allow_empty:
            raise LandingGateError(
                f"No commits ahead of `{remote}/{target}` in {wt} "
                "(pass `--allow-empty` to close without commits)."
            )

        needs_review = (ctx.board.require_review or (local_claim and local_claim.risk_gate)) and not approve
        if needs_review:
            self._rebase_and_verify(wt, remote, target, ctx.board.check_command, issue_number)
            schema, tickets = self.refresh_board(ctx)
            ticket = next((t for t in tickets if t.number == issue_number), None)
            if ticket and ticket.project_item_id:
                self.github.set_item_column(schema, ticket.project_item_id, Column.REVIEW, ctx.board.columns)
            if ticket and ticket.claim and ticket.claim_comment_id:
                ticket.claim.state = "review_ready"
                ticket.claim.heartbeat_at = utc_now_iso()
                ticket.claim.touched_files = self.git.touched_files(wt, f"{remote}/{target}")
                if summary:
                    ticket.claim.note = summary
                self.github.patch_comment(
                    ctx.repo_owner,
                    ctx.repo_name,
                    ticket.claim_comment_id,
                    render_claim_comment(ticket.claim),
                )
            self.herdr.notify(
                f"Kanban #{issue_number} ready for Review", f"Approve push to {target} in TUI (`a`)."
            )
            return LandingResult(
                issue_number=issue_number,
                status="review_pending",
                target_branch=target,
                commit_sha=self.git.head_sha(wt),
            )

        # Rebase -> Check -> Fast-Forward Push (up to 3 attempts on concurrent push race).
        pushed = False
        last_push_err = ""
        for _attempt in range(3):
            self._rebase_and_verify(wt, remote, target, ctx.board.check_command, issue_number)
            if ahead == 0 and allow_empty:
                pushed = True
                break
            ok, out = self.git.fast_forward_push(wt, remote, target)
            if ok:
                pushed = True
                break
            last_push_err = out

        if not pushed:
            raise LandingGateError(
                f"Could not fast-forward push to `{remote}/{target}` "
                f"after 3 rebase attempts:\n{last_push_err}"
            )

        sha = self.git.head_sha(wt)
        schema, tickets = self.refresh_board(ctx)
        ticket = next((t for t in tickets if t.number == issue_number), None)
        cid = local_claim.comment_id if local_claim else (ticket.claim_comment_id if ticket else None)
        if cid and ticket and ticket.claim:
            ticket.claim.state = "completed"
            ticket.claim.heartbeat_at = utc_now_iso()
            ticket.claim.note = summary or f"Landed `{sha[:8]}` onto `{target}`"
            self.github.patch_comment(
                ctx.repo_owner,
                ctx.repo_name,
                cid,
                render_claim_comment(ticket.claim),
            )
        elif summary:
            self.github.post_comment(
                ctx.repo_owner,
                ctx.repo_name,
                issue_number,
                f"**[herdr-kanban]** Landed `{sha[:8]}` onto `{target}`.\n\n{summary}",
            )

        if ticket and ticket.project_item_id:
            self.github.set_item_column(schema, ticket.project_item_id, Column.DONE, ctx.board.columns)

        closed = False
        if ctx.board.close_issue_on_done:
            self.github.close_issue(ctx.repo_owner, ctx.repo_name, issue_number)
            closed = True

        removed_wt = False
        if local_claim:
            state.remove_local_claim(ctx.slug, issue_number)
            cwd_now = Path.cwd().resolve()
            wt_resolved = wt.resolve()
            if cwd_now != wt_resolved and wt_resolved not in cwd_now.parents:
                self.git.remove_ticket_worktree(
                    Path(local_claim.main_repo_root),
                    wt_resolved,
                    local_claim.local_branch,
                )
                removed_wt = True

        return LandingResult(
            issue_number=issue_number,
            status="landed",
            target_branch=target,
            commit_sha=sha,
            closed_issue=closed,
            worktree_removed=removed_wt,
        )

    def sync_claim_heartbeats_and_relays(
        self,
        *,
        force_heartbeat: bool = False,
        last_heartbeat_by_key: dict[str, float] | None = None,
    ) -> list[InboxItem]:
        """Maintain active local claims: in-place heartbeats + trusted incoming comment relays."""
        delivered_or_queued: list[InboxItem] = []
        claims = state.list_local_claims()
        if not claims:
            return delivered_or_queued

        now = time.time()
        for local_claim in claims:
            main_root = Path(local_claim.main_repo_root)
            wt = Path(local_claim.worktree_path)
            if not main_root.is_dir():
                continue
            try:
                ctx = self.resolve_context(main_root)
            except KanbanError:
                continue

            hb_key = f"{local_claim.repo_slug}:{local_claim.issue_number}"
            due = force_heartbeat or (
                last_heartbeat_by_key is None
                or (now - last_heartbeat_by_key.get(hb_key, 0.0)) >= ctx.settings.heartbeat_seconds
            )

            try:
                comments = self.github.list_issue_comments(
                    ctx.repo_owner,
                    ctx.repo_name,
                    local_claim.issue_number,
                )
            except GitHubError:
                continue

            if due:
                winning = resolve_winning_claim(comments)
                if winning and winning[0] == local_claim.comment_id:
                    meta = winning[1]
                    if meta.state == "active":
                        if wt.is_dir():
                            meta.touched_files = self.git.touched_files(
                                wt,
                                f"{ctx.board.remote}/{ctx.board.target_branch}",
                            )
                        meta.heartbeat_at = utc_now_iso()
                        meta.lease_until = lease_until_iso(ctx.board.lease_ttl_minutes)
                        try:
                            self.github.patch_comment(
                                ctx.repo_owner,
                                ctx.repo_name,
                                local_claim.comment_id,
                                render_claim_comment(meta),
                            )
                            if last_heartbeat_by_key is not None:
                                last_heartbeat_by_key[hb_key] = now
                        except GitHubError:
                            pass

            # Relay new comments since watermark
            watermark = state.get_watermark(ctx.slug, local_claim.issue_number)
            trusted_set = self.github.trusted_collaborators(
                ctx.repo_owner,
                ctx.repo_name,
                ctx.board.trusted_authors,
            )
            max_seen = watermark
            for entry in sorted(comments, key=lambda c: int(c["id"])):
                cid = int(entry["id"])
                max_seen = max(max_seen, cid)
                if cid <= watermark or cid == local_claim.comment_id:
                    continue
                body = str(entry.get("body") or "")
                meta_dict = decode_meta(body)
                if meta_dict and meta_dict.get("type") == "claim":
                    continue

                author = str(entry.get("author") or "").strip()
                is_trusted = bool(author and author.lower() in trusted_set)
                from_agent = ""
                from_issue: int | None = None
                if meta_dict and meta_dict.get("type") == "relay":
                    relay = RelayMeta.from_dict(meta_dict)
                    from_agent = relay.from_agent
                    from_issue = relay.from_issue
                    if relay.from_owner == local_claim.owner and from_issue == local_claim.issue_number:
                        continue

                clean_text = strip_meta(body)
                # Strip rendered relay header line if present so inbox stays crisp
                if clean_text.startswith("**[herdr-kanban message"):
                    parts = clean_text.split("\n\n", 1)
                    clean_text = parts[1].strip() if len(parts) > 1 else clean_text

                delivered = False
                if is_trusted:
                    target = local_claim.agent_name or local_claim.pane_id
                    wakeup = self._format_wakeup_prompt(
                        local_claim.issue_number,
                        author,
                        from_agent,
                        from_issue,
                        clean_text,
                    )
                    delivered = self.herdr.prompt_if_idle(target, wakeup)
                    if not delivered:
                        self.herdr.notify(
                            f"Kanban #{local_claim.issue_number}: message from @{author}",
                            clean_text[:100],
                        )

                inbox_item = InboxItem(
                    comment_id=cid,
                    issue_number=local_claim.issue_number,
                    author=author,
                    body=clean_text,
                    created_at=str(entry.get("created_at") or utc_now_iso()),
                    from_agent=from_agent,
                    from_issue=from_issue,
                    trusted=is_trusted,
                    delivered=delivered,
                )
                if state.append_inbox_item(ctx.slug, inbox_item):
                    delivered_or_queued.append(inbox_item)

            if max_seen > watermark:
                state.set_watermark(ctx.slug, local_claim.issue_number, max_seen)

        return delivered_or_queued

    def handle_pane_closed(self, pane_id: str) -> str | None:
        """Auto-release zero-commit clean claims on pane exit; preserve dirty/committed work."""
        local_claim = state.find_claim_by_pane(pane_id)
        if local_claim is None:
            return None
        wt = Path(local_claim.worktree_path)
        main_root = Path(local_claim.main_repo_root)
        base_ref = f"{local_claim.remote}/{local_claim.target_branch}"
        clean = not wt.exists() or self.git.is_clean(wt)
        ahead = self.git.commit_count_ahead(wt, base_ref) if wt.is_dir() else 0

        if clean and ahead == 0:
            try:
                ctx = self.resolve_context(main_root)
                self.release_ticket(
                    ctx,
                    local_claim.issue_number,
                    reason="Agent pane closed with zero commits",
                    force_remove_worktree=True,
                )
                self.herdr.notify(
                    f"Kanban #{local_claim.issue_number} released",
                    "Agent pane closed with no commits; returned to Ready.",
                )
                return "released"
            except (KanbanError, GitError, GitHubError):
                return None

        self.herdr.notify(
            f"Kanban #{local_claim.issue_number}: work preserved",
            f"Pane closed with changes in {wt}",
        )
        return "preserved"

    def _rebase_and_verify(
        self,
        wt: Path,
        remote: str,
        target: str,
        check_command: str,
        issue_number: int,
    ) -> None:
        rebase = self.git.rebase_onto_target(wt, remote, target)
        if not rebase.ok:
            files = ", ".join(rebase.conflicted_files) or "(inspect `git status`)"
            raise LandingGateError(
                f"Rebase onto `{remote}/{target}` hit conflicts in: {files}.\n"
                f"Resolve conflicts in {wt}, run `git rebase --continue`, and re-run "
                f"`herdr-kanban done {issue_number}`.\n{rebase.output}"
            )
        if check_command:
            proc = subprocess.run(
                check_command,
                shell=True,
                cwd=str(wt),
                capture_output=True,
                text=True,
                timeout=600,
            )
            if proc.returncode != 0:
                combined = "\n".join(p for p in (proc.stdout.strip(), proc.stderr.strip()) if p)
                tail = "\n".join(combined.splitlines()[-35:])
                raise LandingGateError(
                    f"Check command `{check_command}` failed (exit {proc.returncode}). "
                    f"Fix the failure in {wt} and re-run `herdr-kanban done {issue_number}`:\n{tail}"
                )

    def _build_agent_prompt(
        self,
        ctx: RepoContext,
        ticket: Ticket,
        claim: state.LocalClaim,
        overlaps: list[FileOverlap],
    ) -> str:
        lines = [
            f"You are assigned GitHub Issue #{ticket.number}: {ticket.title}",
            f"Repository: {ctx.repo_owner}/{ctx.repo_name} | Shared branch: `{ctx.board.target_branch}`",
            f"Your current directory (`{claim.worktree_path}`) is an isolated Git worktree "
            f"off `{ctx.board.remote}/{ctx.board.target_branch}`.",
            "",
            "## Issue Description",
            strip_meta(ticket.body) or "(No description provided.)",
        ]
        if overlaps:
            lines.extend(["", "## Active Teammate Overlap Warnings"])
            for ov in overlaps:
                files_csv = ", ".join(ov.files)
                lines.append(
                    f"- #{ov.other_issue} (@{ov.other_owner}, {ov.other_agent}) touches: {files_csv}"
                )
        num = ticket.number
        lines.extend(
            [
                "",
                "## Workflow Commands (`herdr-kanban`)",
                f'- Report progress in-place: `herdr-kanban progress {num} --note "..."`',
                "- Deposit newly discovered bugs into Triage: "
                f'`herdr-kanban deposit "Title" --parent {num} --file-line path:line`',
                f'- Ask another active ticket\'s agent: `herdr-kanban ask <issue> "..." --from {num}`',
                "- Check incoming agent replies: `herdr-kanban inbox --ack`",
                f"- When finished & committed cleanly, land onto `{ctx.board.target_branch}`: "
                f'`herdr-kanban done {num} --summary "..."`',
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _format_wakeup_prompt(
        issue_number: int,
        author: str,
        from_agent: str,
        from_issue: int | None,
        message: str,
    ) -> str:
        sender = f"@{author}" + (f" ({from_agent})" if from_agent else "")
        via = f" working on #{from_issue}" if from_issue else ""
        reply_hint = (
            f'Reply with `herdr-kanban ask {from_issue} "..." --from {issue_number}`.'
            if from_issue
            else f'Reply with `herdr-kanban comment {issue_number} "..."`.'
        )
        return f"[herdr-kanban message on #{issue_number} from {sender}{via}]\n{message}\n\n{reply_hint}"

    @staticmethod
    def _serialize_cached_ticket(ticket: Ticket) -> dict[str, Any]:
        data = ticket.to_dict()
        data["raw_body"] = ticket.body
        return data

    @staticmethod
    def _deserialize_cached_ticket(raw: dict[str, Any]) -> Ticket:
        claim = ClaimMeta.from_dict(raw["claim"]) if isinstance(raw.get("claim"), dict) else None
        deposit = DepositMeta.from_dict(raw["deposit"]) if isinstance(raw.get("deposit"), dict) else None
        try:
            col = Column(raw.get("column") or "triage")
        except ValueError:
            col = Column.TRIAGE
        return Ticket(
            number=int(raw["number"]),
            title=str(raw.get("title") or ""),
            body=str(raw.get("raw_body") or raw.get("body") or ""),
            state=str(raw.get("state") or "OPEN"),
            url=str(raw.get("url") or ""),
            author=str(raw.get("author") or ""),
            assignees=[str(a) for a in (raw.get("assignees") or [])],
            labels=[str(lbl) for lbl in (raw.get("labels") or [])],
            column=col,
            status_name=str(raw.get("status_name") or ""),
            project_item_id=str(raw.get("project_item_id") or ""),
            claim=claim,
            claim_comment_id=int(raw["claim_comment_id"])
            if raw.get("claim_comment_id") is not None
            else None,
            stale=bool(claim and claim.state == "active" and is_lease_expired(claim.lease_until)),
            deposit=deposit,
            comments_count=int(raw.get("comments_count") or 0),
            updated_at=str(raw.get("updated_at") or ""),
        )
