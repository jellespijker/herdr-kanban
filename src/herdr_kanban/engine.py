"""Core workflow coordinator: claims, worktrees, landing gate, and comment relays."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config, state
from .git_ops import GitClient, GitError
from .github import GitHubClient, GitHubError, ProjectSchema
from .herdr import HerdrClient, current_pane_id, current_scope
from .model import (
     COLUMN_TITLES,
    ClaimMeta,
    Column,
    DepositMeta,
    FileOverlap,
    InboxItem,
    RelayMeta,
    Ticket,
    decode_meta,
    detect_dependency_cycle,
    is_lease_expired,
    lease_until_iso,
    parse_body_dependencies,
    parse_ticket_ref,
    populate_dependency_waves,
    populate_file_overlaps,
    render_claim_comment,
    render_deposit_body,
    render_relay_comment,
    resolve_winning_claim,
    strip_meta,
    utc_now_iso,
    validate_ready_gate,
)

TicketRefInput = int | str


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
    umbrella_root: Path | None = None
    repos: dict[str, config.RepoEntry] = field(default_factory=dict)
    active_repo_alias: str = ""

    @property
    def is_multi_repo(self) -> bool:
        return self.board.is_multi_repo

    @property
    def display_name(self) -> str:
        if self.is_multi_repo:
            label = self.umbrella_root.name if self.umbrella_root else self.board.project_owner
            return f"{label} ({len(self.repos)} repos)"
        return f"{self.repo_owner}/{self.repo_name}"

    def repo_slugs(self) -> list[str]:
        if self.is_multi_repo:
            return [entry.slug for entry in self.repos.values()]
        return [self.slug]


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
    repo_alias: str = ""
    closed_issue: bool = False
    worktree_removed: bool = False
    unblocked_tickets: list[int] = field(default_factory=list)

    def ref(self) -> str:
        return f"{self.repo_alias}#{self.issue_number}" if self.repo_alias else f"#{self.issue_number}"


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
        main_root = (self.git.main_repo_root(wt_root) or wt_root) if wt_root is not None else None
        cwd_claim = state.find_claim_by_cwd(start)

        umbrella_dir = config.find_umbrella_dir(start, main_root)
        if umbrella_dir is None and cwd_claim and cwd_claim.umbrella_root:
            cand = Path(cwd_claim.umbrella_root)
            if cand.is_dir():
                umbrella_dir = cand.resolve()

        if umbrella_dir is not None:
            board = config.load_board_config(wt_root, main_root, None, umbrella_root=umbrella_dir)
            if board is not None and board.is_multi_repo:
                active_alias = ""
                if cwd_claim and cwd_claim.repo_alias in board.repos:
                    active_alias = cwd_claim.repo_alias
                elif main_root is not None and main_root.resolve() != umbrella_dir.resolve():
                    for alias, entry in board.repos.items():
                        if entry.resolved_path(umbrella_dir) == main_root.resolve():
                            active_alias = alias
                            break

                primary_alias = (
                    active_alias
                    if active_alias in board.repos
                    else (
                        board.default_repo if board.default_repo in board.repos else next(iter(board.repos))
                    )
                )
                primary = board.repos[primary_alias]
                slug = f"umbrella__{config.repo_slug(board.project_owner, umbrella_dir.name)}"
                return RepoContext(
                    worktree_cwd=wt_root or start,
                    main_repo_root=main_root or primary.resolved_path(umbrella_dir),
                    repo_owner=primary.owner,
                    repo_name=primary.repo,
                    slug=slug,
                    board=board,
                    settings=config.load_user_settings(),
                    umbrella_root=umbrella_dir,
                    repos=dict(board.repos),
                    active_repo_alias=active_alias,
                )

        if wt_root is None:
            raise KanbanError(
                f"{start} is not inside a Git repository "
                "(run `herdr-kanban init` inside a repo or multi-repo parent directory)"
            )
        assert main_root is not None
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
        single = config.RepoEntry(
            alias=repo_name,
            owner=repo_owner,
            repo=repo_name,
            path=str(main_root),
            target_branch=board.target_branch,
            remote=board.remote,
            check_command=board.check_command,
        )
        return RepoContext(
            worktree_cwd=wt_root,
            main_repo_root=main_root,
            repo_owner=repo_owner,
            repo_name=repo_name,
            slug=slug,
            board=board,
            settings=config.load_user_settings(),
            umbrella_root=None,
            repos={repo_name: single},
            active_repo_alias=repo_name,
        )

    def resolve_repo_entry(
        self,
        ctx: RepoContext,
        selector: str | None = None,
    ) -> config.RepoEntry:
        if not ctx.is_multi_repo:
            only = next(iter(ctx.repos.values()))
            if selector:
                sel_lc = selector.lower()
                valid = {
                    only.alias.lower(),
                    only.repo.lower(),
                    f"{only.owner}/{only.repo}".lower(),
                }
                if sel_lc not in valid:
                    raise KanbanError(f"Repository '{selector}' does not match {only.owner}/{only.repo}")
            return only

        if selector:
            if selector in ctx.repos:
                return ctx.repos[selector]
            sel_lc = selector.lower()
            matches = [
                e
                for e in ctx.repos.values()
                if e.alias.lower() == sel_lc
                or e.repo.lower() == sel_lc
                or f"{e.owner}/{e.repo}".lower() == sel_lc
            ]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                aliases = ", ".join(m.alias for m in matches)
                raise KanbanError(f"Ambiguous repository '{selector}' (matches: {aliases})")
            avail = ", ".join(ctx.repos)
            raise KanbanError(f"Unknown repository '{selector}' (configured: {avail})")

        if ctx.active_repo_alias and ctx.active_repo_alias in ctx.repos:
            return ctx.repos[ctx.active_repo_alias]
        if ctx.board.issues_repo and ctx.board.issues_repo in ctx.repos:
            return ctx.repos[ctx.board.issues_repo]
        if ctx.board.default_repo and ctx.board.default_repo in ctx.repos:
            return ctx.repos[ctx.board.default_repo]
        if len(ctx.repos) == 1:
            return next(iter(ctx.repos.values()))
        avail = ", ".join(ctx.repos)
        raise KanbanError(
            f"Multiple repositories configured ({avail}); pass `--repo <alias>` or `<alias>#<number>`."
        )

    def resolve_issues_entry(
        self,
        ctx: RepoContext,
        fallback_entry: config.RepoEntry | None = None,
    ) -> config.RepoEntry:
        """Return the repository that stores GitHub Issues, labels, and milestones."""
        if ctx.board.issues_repo:
            return self.resolve_repo_entry(ctx, ctx.board.issues_repo)
        if fallback_entry is not None:
            return fallback_entry
        return self.resolve_repo_entry(ctx, None)

    def normalize_labels(self, ctx: RepoContext, raw_labels: list[str] | None) -> list[str]:
        """Expand `facet=value` or `facet:value` shorthands using configured `[taxonomy.facets.*]`."""
        if not raw_labels:
            return []
        out: list[str] = []
        for item in raw_labels:
            for part in item.split(","):
                tok = part.strip()
                if not tok:
                    continue
                if "=" in tok:
                    fname, fval = tok.split("=", 1)
                    spec = ctx.board.facets.get(fname.strip())
                    if spec:
                        tok = f"{spec.prefix}{fval.strip()}"
                out.append(tok)
        seen: set[str] = set()
        deduped: list[str] = []
        for lbl in out:
            if lbl not in seen:
                seen.add(lbl)
                deduped.append(lbl)
        return deduped

    def merge_labels_with_facets(
        self,
        ctx: RepoContext,
        existing_labels: list[str],
        add_labels: list[str],
        rm_labels: list[str],
    ) -> list[str]:
        """Merge labels while replacing single-value (`multiple = false`) facet labels."""
        rm_set = set(self.normalize_labels(ctx, rm_labels))
        norm_add = self.normalize_labels(ctx, add_labels)
        single_prefixes_to_replace: set[str] = set()
        for spec in ctx.board.facets.values():
            if not spec.multiple and spec.prefix and any(lbl.startswith(spec.prefix) for lbl in norm_add):
                single_prefixes_to_replace.add(spec.prefix)

        kept = [
            lbl
            for lbl in existing_labels
            if lbl not in rm_set
            and not any(lbl.startswith(pref) for pref in single_prefixes_to_replace)
        ]
        for lbl in norm_add:
            if lbl not in kept and lbl not in rm_set:
                kept.append(lbl)
        return kept

    def resolve_ticket_target(
        self,
        ctx: RepoContext,
        ref: TicketRefInput,
        tickets: list[Ticket] | None = None,
    ) -> tuple[config.RepoEntry, int]:
        if isinstance(ref, int):
            if ref < 1:
                raise KanbanError(f"Ticket number must be >= 1, got {ref}")
            repo_sel, issue_number = None, ref
        else:
            try:
                repo_sel, issue_number = parse_ticket_ref(str(ref))
            except ValueError as exc:
                raise KanbanError(str(exc)) from exc

        if not ctx.is_multi_repo or repo_sel is not None:
            return self.resolve_repo_entry(ctx, repo_sel), issue_number

        # Bare `<number>` inside an umbrella workspace:
        cwd_claim = state.find_claim_by_cwd(ctx.worktree_cwd)
        if cwd_claim and cwd_claim.issue_number == issue_number and cwd_claim.repo_alias in ctx.repos:
            return ctx.repos[cwd_claim.repo_alias], issue_number

        board_tickets = tickets
        if board_tickets is None:
            cached = self.load_cached_tickets(ctx)
            board_tickets = cached[1] if cached is not None else self.refresh_board(ctx)[1]

        matches = [t for t in board_tickets if t.number == issue_number and t.repo_alias in ctx.repos]
        if len(matches) == 1:
            return ctx.repos[matches[0].repo_alias], issue_number
        if len(matches) > 1:
            if ctx.active_repo_alias and any(m.repo_alias == ctx.active_repo_alias for m in matches):
                return ctx.repos[ctx.active_repo_alias], issue_number
            refs = ", ".join(f"{m.repo_alias}#{issue_number}" for m in matches)
            raise KanbanError(
                f"Ambiguous issue #{issue_number} exists in multiple repositories: {refs}. "
                f"Specify `<repo>#{issue_number}`."
            )

        local_matches = [
            e for e in ctx.repos.values() if state.get_local_claim(e.slug, issue_number) is not None
        ]
        if len(local_matches) == 1:
            return local_matches[0], issue_number

        if ctx.active_repo_alias and ctx.active_repo_alias in ctx.repos:
            return ctx.repos[ctx.active_repo_alias], issue_number
        if ctx.board.default_repo and ctx.board.default_repo in ctx.repos:
            return ctx.repos[ctx.board.default_repo], issue_number

        avail = ", ".join(f"{alias}#{issue_number}" for alias in ctx.repos)
        raise KanbanError(f"Issue #{issue_number} is ambiguous across umbrella repos; specify e.g. {avail}.")

    def _entry_main_root(self, ctx: RepoContext, entry: config.RepoEntry) -> Path:
        if not ctx.is_multi_repo:
            return ctx.main_repo_root
        base = ctx.umbrella_root or ctx.main_repo_root
        root = entry.resolved_path(base)
        if not (root / ".git").exists():
            raise KanbanError(
                f"Repository '{entry.alias}' ({entry.owner}/{entry.repo}) is not cloned at {root}. "
                "Run `herdr-kanban sync-repos` first."
            )
        return root

    @staticmethod
    def _find_board_ticket(
        tickets: list[Ticket],
        entry: config.RepoEntry,
        issue_number: int,
        multi_repo: bool,
    ) -> Ticket | None:
        key = (entry.owner.lower(), entry.repo.lower())
        for ticket in tickets:
            if ticket.number != issue_number:
                continue
            if not multi_repo or ticket.repo_key() == key or ticket.repo_alias == entry.alias:
                return ticket
        return None

    def refresh_board(self, ctx: RepoContext) -> tuple[ProjectSchema, list[Ticket]]:
        extra_kwargs: dict[str, Any] = {}
        if ctx.board.priority_field and ctx.board.priority_field != "Priority":
            extra_kwargs["priority_field"] = ctx.board.priority_field
        if ctx.is_multi_repo:
            allowed = {(entry.owner.lower(), entry.repo.lower()): alias for alias, entry in ctx.repos.items()}
            schema, tickets = self.github.fetch_board(
                project_owner=ctx.board.project_owner,
                project_number=ctx.board.project_number,
                column_map=ctx.board.columns,
                label_filter=ctx.board.label_filter,
                allowed_repos=allowed,
                **extra_kwargs,
            )
            for t in tickets:
                if not t.target_repo:
                    inferred = self._infer_code_entry_from_ticket(
                        ctx,
                        ctx.repos.get(t.repo_alias) or next(iter(ctx.repos.values())),
                        t,
                    )
                    t.target_repo = inferred.alias
        else:
            schema, tickets = self.github.fetch_board(
                project_owner=ctx.board.project_owner,
                project_number=ctx.board.project_number,
                repo_owner=ctx.repo_owner,
                repo_name=ctx.repo_name,
                column_map=ctx.board.columns,
                label_filter=ctx.board.label_filter,
                **extra_kwargs,
            )
        populate_dependency_waves(tickets)
        populate_file_overlaps(tickets)
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
        populate_dependency_waves(tickets)
        populate_file_overlaps(tickets)
        return fetched_at, tickets

    def priority_rank(self, ctx: RepoContext, priority: str) -> int:
        if not priority:
            return 999
        p_lc = priority.strip().lower()
        for idx, configured in enumerate(ctx.board.priorities):
            c_lc = configured.lower()
            if p_lc == c_lc or p_lc.startswith(c_lc) or c_lc.startswith(p_lc):
                return idx
        default_order = ("p0", "p1", "p2", "p3", "critical", "high", "medium", "low")
        for idx, prefix in enumerate(default_order):
            if p_lc.startswith(prefix):
                return idx
        return 500

    def sort_tickets_for_display(self, ctx: RepoContext, tickets: list[Ticket]) -> list[Ticket]:
        return sorted(
            tickets,
            key=lambda t: (
                t.wave,
                self.priority_rank(ctx, t.priority),
                t.repo_alias or t.repo_name,
                t.number,
            ),
        )

    @staticmethod
    def _checklist_counts(ticket: Ticket) -> tuple[int, int]:
        if ticket.claim and ticket.claim.checklist:
            items = [it.strip() for it in ticket.claim.checklist if it.strip()]
            done = sum(1 for it in items if it.lower().startswith("[x]"))
            return done, len(items)
        body = strip_meta(ticket.body or "")
        boxes = re.findall(r"(?m)^\s*[-*]\s*\[([ xX])\]", body)
        if not boxes:
            return 0, 0
        done = sum(1 for b in boxes if b.lower() == "x")
        return done, len(boxes)

    def serialize_ticket_for_board(
        self,
        ctx: RepoContext,
        ticket: Ticket,
        local_by_key: dict[tuple[str, int], state.LocalClaim] | None = None,
    ) -> dict[str, Any]:
        row = ticket.to_dict()
        row["ref"] = ticket.ref(ctx.is_multi_repo)
        row["cli_ref"] = (
            f"{ticket.repo_alias}#{ticket.number}"
            if (ctx.is_multi_repo and ticket.repo_alias)
            else str(ticket.number)
        )
        slug = config.repo_slug(ticket.repo_owner or ctx.repo_owner, ticket.repo_name or ctx.repo_name)
        lc = (
            local_by_key.get((slug, ticket.number))
            if local_by_key is not None
            else state.get_local_claim(slug, ticket.number)
        )
        row["local_claim"] = lc.to_dict() if lc is not None else None
        done_cnt, total_cnt = self._checklist_counts(ticket)
        row["checklist_done"] = done_cnt
        row["checklist_total"] = total_cnt
        return row

    def build_board_payload(
        self,
        ctx: RepoContext,
        schema: ProjectSchema,
        tickets: list[Ticket],
    ) -> dict[str, Any]:
        slugs = {e.slug for e in ctx.repos.values()} | {ctx.slug}
        all_local = [c for c in state.list_local_claims() if c.repo_slug in slugs]
        local_by_key = {(c.repo_slug, c.issue_number): c for c in all_local}
        repos_dict = {
            alias: {
                "alias": alias,
                "owner": e.owner,
                "repo": e.repo,
                "path": e.path,
                "slug": e.slug,
                "target_branch": e.effective_branch(ctx.board),
                "remote": e.effective_remote(ctx.board),
            }
            for alias, e in ctx.repos.items()
        }
        facets_dict = {
            name: {
                "name": f.name,
                "prefix": f.prefix,
                "color": f.color,
                "required": f.required_for_ready,
                "required_for_ready": f.required_for_ready,
                "multiple": f.multiple,
                "values": list(f.values),
                "description": f.description,
            }
            for name, f in ctx.board.facets.items()
        }
        payload: dict[str, Any] = {
            "repo": ctx.display_name if ctx.is_multi_repo else f"{ctx.repo_owner}/{ctx.repo_name}",
            "repo_owner": ctx.repo_owner,
            "repo_name": ctx.repo_name,
            "slug": ctx.slug,
            "worktree_cwd": str(ctx.worktree_cwd),
            "main_repo_root": str(ctx.main_repo_root),
            "umbrella_root": str(ctx.umbrella_root) if ctx.umbrella_root else "",
            "multi_repo": ctx.is_multi_repo,
            "default_repo": ctx.board.default_repo,
            "issues_repo": ctx.board.issues_repo,
            "active_repo_alias": ctx.active_repo_alias,
            "remote": ctx.board.remote,
            "target_branch": ctx.board.target_branch,
            "require_review": ctx.board.require_review,
            "require_priority_for_ready": ctx.board.require_priority_for_ready,
            "priorities": list(ctx.board.priorities),
            "milestones": list(ctx.board.milestones),
            "facets": facets_dict,
            "columns": [{"id": col.value, "title": COLUMN_TITLES[col]} for col in Column],
            "settings": {
                "default_agent_kind": ctx.settings.default_agent_kind,
                "supported_agents": list(config.SUPPORTED_AGENTS),
            },
            "project": {"number": schema.number, "title": schema.title, "url": schema.url},
            "repos": repos_dict,
            "local_claims": [c.to_dict() for c in all_local],
            "tickets": [self.serialize_ticket_for_board(ctx, t, local_by_key) for t in tickets],
            "whats_new": self.build_whats_new_digest(ctx, tickets),
        }
        return payload

    @staticmethod
    def _split_ticket_prefixes(raw_title: str) -> tuple[str, list[str], list[str]]:
        """Extract leading `[JIRA-KEY]` and `[scope]` tags from a ticket title."""
        rest = (raw_title or "").strip()
        jira_keys: list[str] = []
        scopes: list[str] = []
        tag_re = re.compile(r"^\[([^\[\]]+)\]\s*")
        jira_re = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")
        while True:
            m = tag_re.match(rest)
            if not m:
                break
            token = m.group(1).strip()
            rest = rest[m.end() :].strip()
            if jira_re.match(token):
                if token not in jira_keys:
                    jira_keys.append(token)
            elif token and token not in scopes:
                scopes.append(token)
        return (rest or raw_title.strip()), jira_keys, scopes

    @staticmethod
    def _extract_ticket_summary_note(ticket: Ticket) -> str:
        if ticket.claim and ticket.claim.note and ticket.claim.note.strip():
            return ticket.claim.note.strip()
        body = strip_meta(ticket.body or "")
        if not body:
            return ""
        # Strip trailing "--- \n **Deposited via `herdr-kanban`**" footer
        body = re.split(r"\n---\s*\n\*\*Deposited via", body)[0].strip()
        # Strip leading "Jira: ..." line if followed by prose
        lines = [ln.strip() for ln in body.splitlines() if ln.strip() and not ln.strip().startswith("#")]
        if lines and re.match(r"^Jira:\s*[A-Z0-9_,\s-]+$", lines[0], re.IGNORECASE) and len(lines) > 1:
            lines = lines[1:]
        first = lines[0] if lines else ""
        first = re.sub(r"^Jira:\s*[A-Z0-9_,\s-]+\.\s*", "", first)
        return first[:220] + ("…" if len(first) > 220 else "")

    def build_whats_new_digest(
        self,
        ctx: RepoContext,
        tickets: list[Ticket],
        limit: int = 30,
    ) -> dict[str, Any]:
        """Build structured What's New digest of landed and active work across colleagues and agents."""
        done_tickets = [t for t in tickets if t.column == Column.DONE]
        active_tickets = [t for t in tickets if t.column in (Column.IN_PROGRESS, Column.REVIEW)]

        done_sorted = sorted(
            done_tickets,
            key=lambda t: (t.updated_at or "", t.number),
            reverse=True,
        )

        recent_landed: list[dict[str, Any]] = []
        contributors_map: dict[str, dict[str, Any]] = {}
        repo_groups: dict[str, list[dict[str, Any]]] = {}
        release_groups: dict[str, int] = {}

        for t in active_tickets:
            owner = (
                (t.claim.owner if t.claim and t.claim.owner else "")
                or (t.deposit.deposited_by if t.deposit and t.deposit.deposited_by else "")
                or t.author
                or "team"
            )
            agent = t.claim.agent_kind if t.claim and t.claim.agent_kind else ""
            c_entry = contributors_map.setdefault(
                owner,
                {
                    "owner": owner,
                    "agents": set(),
                    "landed_count": 0,
                    "active_count": 0,
                    "repos": set(),
                    "recent_refs": [],
                },
            )
            c_entry["active_count"] += 1
            if agent:
                c_entry["agents"].add(agent)
            repo_key = t.target_repo or t.repo_alias or t.repo_name
            if repo_key:
                c_entry["repos"].add(repo_key)

        for idx, t in enumerate(done_sorted):
            clean_title, jira_keys, scopes = self._split_ticket_prefixes(t.title)
            owner = (
                (t.claim.owner if t.claim and t.claim.owner else "")
                or (t.deposit.deposited_by if t.deposit and t.deposit.deposited_by else "")
                or t.author
                or "team"
            )
            agent = t.claim.agent_kind if t.claim and t.claim.agent_kind else ""
            repo_key = t.target_repo or t.repo_alias or t.repo_name or ctx.repo_name
            ref_str = t.ref(ctx.is_multi_repo)
            cli_ref = (
                f"{t.repo_alias}#{t.number}"
                if (ctx.is_multi_repo and t.repo_alias)
                else str(t.number)
            )
            rel_tags = [lbl.split(":", 1)[1] for lbl in t.labels if lbl.startswith("release:")]
            for rt in rel_tags:
                release_groups[rt] = release_groups.get(rt, 0) + 1

            c_entry = contributors_map.setdefault(
                owner,
                {
                    "owner": owner,
                    "agents": set(),
                    "landed_count": 0,
                    "active_count": 0,
                    "repos": set(),
                    "recent_refs": [],
                },
            )
            c_entry["landed_count"] += 1
            if agent:
                c_entry["agents"].add(agent)
            if repo_key:
                c_entry["repos"].add(repo_key)
            if len(c_entry["recent_refs"]) < 6:
                c_entry["recent_refs"].append(ref_str)

            item = {
                "number": t.number,
                "ref": ref_str,
                "cli_ref": cli_ref,
                "title": t.title,
                "clean_title": clean_title,
                "jira_keys": jira_keys,
                "scopes": scopes,
                "target_repo": repo_key,
                "priority": t.priority,
                "milestone": t.milestone,
                "owner": owner,
                "agent_kind": agent,
                "updated_at": t.updated_at,
                "summary_note": self._extract_ticket_summary_note(t),
                "touched_count": len(t.claim.touched_files) if t.claim else 0,
                "labels": list(t.labels),
                "releases": rel_tags,
            }
            if idx < limit:
                recent_landed.append(item)
            repo_groups.setdefault(repo_key, []).append(item)

        contributors = [
            {
                "owner": k,
                "agents": sorted(v["agents"]),
                "landed_count": v["landed_count"],
                "active_count": v["active_count"],
                "repos": sorted(v["repos"]),
                "recent_refs": v["recent_refs"],
            }
            for k, v in sorted(
                contributors_map.items(),
                key=lambda kv: (kv[1]["landed_count"] + kv[1]["active_count"], kv[0]),
                reverse=True,
            )
        ]

        by_repo = [
            {
                "repo": repo_name,
                "landed_count": len(items),
                "recent": items[:5],
            }
            for repo_name, items in sorted(
                repo_groups.items(),
                key=lambda kv: (len(kv[1]), kv[0]),
                reverse=True,
            )
        ]

        saved = state.load_whats_new_summary(ctx.slug, ctx.board.project_number)
        auto_md = self._render_auto_whats_new_markdown(
            ctx,
            recent_landed[:15],
            contributors,
            by_repo,
            active_tickets,
            len(done_tickets),
        )

        return {
            "total_landed": len(done_tickets),
            "total_active": len(active_tickets),
            "recent_landed": recent_landed,
            "contributors": contributors,
            "by_repo": by_repo,
            "releases": release_groups,
            "saved_summary": saved,
            "auto_markdown": auto_md,
        }

    def _render_auto_whats_new_markdown(
        self,
        ctx: RepoContext,
        recent_landed: list[dict[str, Any]],
        contributors: list[dict[str, Any]],
        by_repo: list[dict[str, Any]],
        active_tickets: list[Ticket],
        total_landed: int,
    ) -> str:
        lines: list[str] = []
        lines.append(
            f"**{total_landed} tickets landed** onto `{ctx.board.remote}/{ctx.board.target_branch}` "
            f"across **{len(by_repo)} repositories**, with **{len(active_tickets)} active** in progress."
        )
        if contributors:
            contrib_parts = []
            for c in contributors[:6]:
                ag_str = f" ({', '.join(c['agents'])})" if c["agents"] else ""
                contrib_parts.append(
                    f"**@{c['owner']}**{ag_str}: {c['landed_count']} landed"
                    + (f", {c['active_count']} active" if c["active_count"] else "")
                )
            lines.append("\n### Team & Agent Velocity\n" + " · ".join(contrib_parts))

        if by_repo:
            lines.append("\n### Highlights by Repository")
            for grp in by_repo[:8]:
                repo_name = grp["repo"]
                cnt = grp["landed_count"]
                lines.append(f"\n#### `{repo_name}` ({cnt} landed)")
                for item in grp["recent"][:4]:
                    ref = item["ref"]
                    cli_ref = item["cli_ref"]
                    prio = f"**{item['priority']}** " if item["priority"] else ""
                    who = f" · *@{item['owner']}" + (f":{item['agent_kind']}*" if item["agent_kind"] else "*")
                    lines.append(f"- [{ref}](kanban://issue/{cli_ref}) {prio}{item['clean_title']}{who}")
                    if item["summary_note"]:
                        lines.append(f"  > {item['summary_note']}")
        return "\n".join(lines)

    def generate_whats_new_summary(
        self,
        ctx: RepoContext,
        tickets: list[Ticket],
        *,
        custom_markdown: str | None = None,
        author: str = "",
        agent_kind: str = "",
    ) -> dict[str, Any]:
        """Generate or store an executive Markdown briefing of landed work + recent Git commits."""
        digest = self.build_whats_new_digest(ctx, tickets)
        who = author
        if not who:
            try:
                who = self.github.current_user()
            except Exception:
                who = "agent"
        kind = agent_kind or ctx.settings.default_agent_kind or "agent"

        if custom_markdown and custom_markdown.strip():
            md = custom_markdown.strip()
        else:
            # Synthesize rich briefing combining board tickets + recent git commits across top repos
            lines: list[str] = [
                f"## Executive Briefing — {ctx.display_name}",
                "",
                digest["auto_markdown"],
            ]
            git_sections: list[str] = []
            checked_repos = [r["repo"] for r in digest["by_repo"][:6]]
            if not checked_repos and ctx.repos:
                checked_repos = list(ctx.repos.keys())[:4]
            for alias in checked_repos:
                entry = ctx.repos.get(alias)
                if not entry:
                    continue
                try:
                    repo_root = self._entry_main_root(ctx, entry)
                    proc = subprocess.run(
                        [
                            "git",
                            "-C",
                            str(repo_root),
                            "log",
                            "-n",
                            "4",
                            "--pretty=format:- `%h` %s (*%cr* by **%an**)",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    if proc.returncode == 0 and proc.stdout.strip():
                        git_sections.append(f"#### Recent commits in `{alias}`\n{proc.stdout.strip()}")
                except Exception:
                    continue
            if git_sections:
                lines.append("\n### Recent Git Commits on Shared Branches\n" + "\n\n".join(git_sections))
            md = "\n".join(lines)

        summary_obj = {
            "markdown": md,
            "updated_at": utc_now_iso(),
            "author": who,
            "agent_kind": kind,
            "landed_count": digest["total_landed"],
        }
        state.save_whats_new_summary(ctx.slug, ctx.board.project_number, summary_obj)
        digest["saved_summary"] = summary_obj
        return digest

    def build_whats_new_agent_prompt(
        self,
        ctx: RepoContext,
        tickets: list[Ticket],
        limit: int = 25,
    ) -> str:
        digest = self.build_whats_new_digest(ctx, tickets, limit=limit)
        lines = [
            f"Synthesize a concise 'What's New' Markdown briefing for teammates in {ctx.display_name}.",
            "Group the recently landed tickets and git commits by architectural theme / repository,",
            "highlight what each colleague and agent accomplished, and call out newly unblocked work.",
            "Format ticket references as `[repo#num](kanban://issue/repo#num)` for clickable Herdr links.",
            "",
            "When ready, publish your Markdown briefing back to the Herdr Kanban board by running:",
            '  herdr-kanban whats-new --write-summary "<YOUR_MARKDOWN_SUMMARY>"',
            "",
            "## Recently Landed Tickets",
        ]
        for item in digest["recent_landed"][:limit]:
            lines.append(
                f"- {item['ref']} [{item['target_repo']}] ({item['priority']}) {item['clean_title']} "
                f"— by @{item['owner']}:{item['agent_kind'] or 'agent'}"
            )
            if item["summary_note"]:
                lines.append(f"  Summary: {item['summary_note']}")
        return "\n".join(lines)


    def sync_labels(self, ctx: RepoContext) -> dict[str, Any]:
        """Provision configured taxonomy labels, milestones, and Project v2 Priority options."""
        issues_entry = self.resolve_issues_entry(ctx)
        existing_labels = self.github.list_labels(issues_entry.owner, issues_entry.repo)
        label_results: list[dict[str, str]] = []
        for facet in ctx.board.facets.values():
            for val in facet.values:
                full_name = f"{facet.prefix}{val}"
                desc = facet.description or f"{facet.name}: {val}"
                status = self.github.upsert_label(
                    issues_entry.owner,
                    issues_entry.repo,
                    full_name,
                    facet.color,
                    desc,
                    existing=existing_labels,
                )
                label_results.append({"name": full_name, "color": facet.color, "status": status})

        existing_ms = self.github.list_milestones(issues_entry.owner, issues_entry.repo)
        milestone_results: list[dict[str, Any]] = []
        for ms_title in ctx.board.milestones:
            ms_num, ms_status = self.github.ensure_milestone(
                issues_entry.owner,
                issues_entry.repo,
                ms_title,
                existing=existing_ms,
            )
            milestone_results.append({"title": ms_title, "number": ms_num, "status": ms_status})

        schema, _ = self.refresh_board(ctx)
        added_priorities: list[str] = []
        if ctx.board.priorities and (schema.priority_field_id or schema.project_id):
            added_priorities = self.github.ensure_single_select_options(
                schema.priority_field_id,
                ctx.board.priority_field,
                ctx.board.priorities,
                schema.priority_options_by_name,
                project_id=schema.project_id,
            )

        return {
            "issues_repo": f"{issues_entry.owner}/{issues_entry.repo}",
            "labels": label_results,
            "milestones": milestone_results,
            "added_priorities": added_priorities,
        }

    def sync_repos(self, ctx: RepoContext, *, create_branches: bool = False) -> list[dict[str, str]]:
        """Clone missing child repos and check out each repo's shared target branch."""
        results: list[dict[str, str]] = []
        base = ctx.umbrella_root or ctx.main_repo_root.parent
        if ctx.is_multi_repo and (base / ".git").exists():
            child_paths = [
                e.path
                for e in ctx.repos.values()
                if e.resolved_path(base) != base.resolve() and not Path(e.path).is_absolute()
            ]
            self.git.ensure_gitignore_entries(base, child_paths)
        for entry in ctx.repos.values():
            dest = entry.resolved_path(base) if ctx.is_multi_repo else ctx.main_repo_root
            cloned = False
            if not (dest / ".git").exists():
                self.github.clone_repo(entry.owner, entry.repo, dest)
                cloned = True
            remote = entry.effective_remote(ctx.board)
            branch = entry.effective_branch(ctx.board)
            branch_state = self.git.checkout_target_branch(
                dest,
                remote,
                branch,
                create_if_missing=create_branches,
            )
            results.append(
                {
                    "alias": entry.alias,
                    "repo": f"{entry.owner}/{entry.repo}",
                    "path": str(dest),
                    "branch": branch,
                    "status": f"cloned + {branch_state}" if cloned else branch_state,
                }
            )
        return results

    def _link_siblings_if_multi(
        self,
        ctx: RepoContext,
        entry: config.RepoEntry,
        wt_path: Path,
    ) -> list[str]:
        if not ctx.is_multi_repo:
            return []
        base = ctx.umbrella_root or ctx.main_repo_root.parent
        siblings = {
            alias: other.resolved_path(base) for alias, other in ctx.repos.items() if alias != entry.alias
        }
        return self.git.link_sibling_repos(wt_path, siblings)

    def claim_ticket(
        self,
        ctx: RepoContext,
        ref: TicketRefInput,
        *,
        agent_kind: str | None = None,
        agent_name: str = "",
        pane_id: str = "",
        risk_gate: bool = False,
        takeover: bool = False,
        repo_selector: str | None = None,
        force: bool = False,
    ) -> ClaimResult:
        chosen_kind = agent_kind or ctx.settings.default_agent_kind
        schema, tickets = self.refresh_board(ctx)
        entry, issue_number = self.resolve_ticket_target(ctx, ref, tickets)
        ticket = self._find_board_ticket(tickets, entry, issue_number, ctx.is_multi_repo)
        disp = f"{entry.alias}#{issue_number}" if ctx.is_multi_repo else f"#{issue_number}"
        if ticket is None:
            raise KanbanError(f"Issue {disp} is not on GitHub Project #{ctx.board.project_number}")

        if ticket.unmet_deps and not force:
            blockers = ", ".join(f"#{n}" for n in ticket.unmet_deps)
            raise KanbanError(
                f"Issue {disp} is blocked by unfinished dependency ticket(s): {blockers}. "
                f"Complete {blockers} first (or pass `--force` to override)."
            )

        existing_local = state.get_local_claim(entry.slug, issue_number)
        if existing_local and Path(existing_local.worktree_path).is_dir():
            code_entry = self._infer_code_entry_from_ticket(ctx, entry, ticket, explicit_repo=repo_selector)
            self._link_siblings_if_multi(ctx, code_entry, Path(existing_local.worktree_path))
            if chosen_kind and existing_local.agent_kind != chosen_kind:
                existing_local.agent_kind = chosen_kind
                state.save_local_claim(existing_local)
            comments = self.github.list_issue_comments(entry.owner, entry.repo, issue_number)
            winning = resolve_winning_claim(comments)
            if winning is not None:
                win_cid, win_claim = winning
                if chosen_kind and win_claim.agent_kind != chosen_kind:
                    win_claim.agent_kind = chosen_kind
                    try:
                        self.github.patch_comment(
                            entry.owner,
                            entry.repo,
                            win_cid,
                            render_claim_comment(win_claim),
                        )
                    except GitHubError:
                        pass
            return ClaimResult(
                claim=existing_local,
                ticket=ticket,
                overlaps=ticket.overlaps,
                reused=True,
            )

        comments = self.github.list_issue_comments(entry.owner, entry.repo, issue_number)
        winning = resolve_winning_claim(comments)
        takeover_of: int | None = None
        if winning is not None:
            win_cid, win_claim = winning
            expired = is_lease_expired(win_claim.lease_until)
            if not expired:
                raise ClaimConflictError(
                    f"Issue {disp} is already claimed by @{win_claim.owner} "
                    f"({win_claim.agent_kind}) until {win_claim.lease_until}"
                )
            if not takeover:
                raise ClaimStaleError(
                    f"Issue {disp} has a STALE claim by @{win_claim.owner} "
                    f"(expired {win_claim.lease_until}). Re-run with `--takeover` to reclaim."
                )
            takeover_of = win_cid

        code_entry = self._infer_code_entry_from_ticket(ctx, entry, ticket, explicit_repo=repo_selector)
        main_repo = self._entry_main_root(ctx, code_entry)
        remote = code_entry.effective_remote(ctx.board)
        target_branch = code_entry.effective_branch(ctx.board)

        user = self.github.current_user()
        wt_base = ctx.settings.resolved_worktree_root() / code_entry.slug / f"issue-{issue_number}"
        had_wt = wt_base.exists()
        self.git.ensure_remote_branch(main_repo, remote, target_branch)
        wt_path, local_branch = self.git.create_ticket_worktree(
            main_repo,
            wt_base,
            remote,
            target_branch,
            issue_number,
        )
        self._link_siblings_if_multi(ctx, code_entry, wt_path)

        now_iso = utc_now_iso()
        lease_iso = lease_until_iso(ctx.board.lease_ttl_minutes)
        caller_pane = pane_id or current_pane_id()
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
            branch=target_branch,
            target_repo=code_entry.alias if ctx.is_multi_repo else "",
            risk_gate=risk_gate or ctx.board.require_review,
            takeover_of=takeover_of,
        )

        posted = self.github.post_comment(
            entry.owner,
            entry.repo,
            issue_number,
            render_claim_comment(meta),
        )
        my_cid = int(posted["id"])

        # Optimistic concurrency verification: earliest active comment ID wins.
        after_comments = self.github.list_issue_comments(entry.owner, entry.repo, issue_number)
        verified = resolve_winning_claim(after_comments)
        if verified is None or verified[0] != my_cid:
            self.github.delete_comment(entry.owner, entry.repo, my_cid)
            if not had_wt and self.git.is_clean(wt_path):
                self.git.remove_ticket_worktree(main_repo, wt_path, local_branch)
            holder = f"@{verified[1].owner}" if verified else "another agent"
            raise ClaimRaceLostError(
                f"Lost simultaneous claim race on {disp} to {holder}; claim rolled back."
            )

        try:
            self.github.assign_issue(entry.owner, entry.repo, issue_number, user)
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
            repo_slug=entry.slug,
            repo_owner=entry.owner,
            repo_name=entry.repo,
            main_repo_root=str(main_repo),
            issue_number=issue_number,
            worktree_path=str(wt_path),
            local_branch=local_branch,
            target_branch=target_branch,
            remote=remote,
            comment_id=my_cid,
            owner=user,
            agent_kind=chosen_kind,
            agent_name=agent_name,
            pane_id=caller_pane,
            workspace_id=os.environ.get("HERDR_WORKSPACE_ID") or current_scope(),
            herdr_session=state.herdr_session_key(),
            risk_gate=meta.risk_gate,
            started_at=now_iso,
            umbrella_root=str(ctx.umbrella_root) if ctx.umbrella_root else "",
            repo_alias=entry.alias if ctx.is_multi_repo else "",
        )
        state.save_local_claim(local_claim)
        if after_comments:
            state.set_watermark(entry.slug, issue_number, max(int(c["id"]) for c in after_comments))

        ticket.claim = meta
        ticket.claim_comment_id = my_cid
        ticket.column = Column.IN_PROGRESS
        populate_file_overlaps(tickets)
        return ClaimResult(
            claim=local_claim,
            ticket=ticket,
            overlaps=list(ticket.overlaps),
            reused=False,
        )

    def dispatch_ticket(
        self,
        ctx: RepoContext,
        ref: TicketRefInput,
        *,
        agent_kind: str | None = None,
        risk_gate: bool = False,
        takeover: bool = False,
        focus: bool = True,
    ) -> ClaimResult:
        chosen_kind = agent_kind or ctx.settings.default_agent_kind
        res = self.claim_ticket(
            ctx,
            ref,
            agent_kind=chosen_kind,
            risk_gate=risk_gate,
            takeover=takeover,
        )
        prompt = self._build_agent_prompt(ctx, res.ticket, res.claim, res.overlaps)
        spawned = self.herdr.spawn_ticket_agent(
            worktree_path=Path(res.claim.worktree_path),
            issue_number=res.claim.issue_number,
            title=res.ticket.title,
            agent_kind=chosen_kind,
            prompt_text=prompt,
            repo_alias=res.claim.repo_alias if ctx.is_multi_repo else "",
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
                    res.claim.repo_owner,
                    res.claim.repo_name,
                    res.claim.comment_id,
                    render_claim_comment(res.ticket.claim),
                )
            except GitHubError:
                pass
        return res

    def release_ticket(
        self,
        ctx: RepoContext,
        ref: TicketRefInput,
        *,
        reason: str = "",
        force_remove_worktree: bool = False,
    ) -> tuple[config.RepoEntry, int]:
        schema, tickets = self.refresh_board(ctx)
        entry, issue_number = self.resolve_ticket_target(ctx, ref, tickets)
        ticket = self._find_board_ticket(tickets, entry, issue_number, ctx.is_multi_repo)
        local_claim = state.get_local_claim(entry.slug, issue_number)

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
                entry.owner,
                entry.repo,
                cid,
                render_claim_comment(claim_meta),
            )

        if ticket and ticket.project_item_id:
            self.github.set_item_column(schema, ticket.project_item_id, Column.READY, ctx.board.columns)
        try:
            user = self.github.current_user()
            self.github.unassign_issue(entry.owner, entry.repo, issue_number, user)
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
            state.remove_local_claim(entry.slug, issue_number)
        return entry, issue_number

    def update_progress(
        self,
        ctx: RepoContext,
        ref: TicketRefInput,
        *,
        note: str | None = None,
        checklist: list[str] | None = None,
    ) -> ClaimMeta:
        entry, issue_number = self.resolve_ticket_target(ctx, ref)
        local_claim = state.get_local_claim(entry.slug, issue_number)
        comments = self.github.list_issue_comments(entry.owner, entry.repo, issue_number)
        winning = resolve_winning_claim(comments)
        disp = f"{entry.alias}#{issue_number}" if ctx.is_multi_repo else f"#{issue_number}"
        if winning is None:
            raise KanbanError(f"Issue {disp} has no active claim to update")
        cid, meta = winning

        wt = Path(local_claim.worktree_path) if local_claim else ctx.worktree_cwd
        base_ref = f"{entry.effective_remote(ctx.board)}/{entry.effective_branch(ctx.board)}"
        if wt.is_dir():
            meta.touched_files = self.git.touched_files(wt, base_ref)
        meta.heartbeat_at = utc_now_iso()
        meta.lease_until = lease_until_iso(ctx.board.lease_ttl_minutes)
        if note is not None:
            meta.note = note
        if checklist is not None:
            meta.checklist = checklist

        self.github.patch_comment(entry.owner, entry.repo, cid, render_claim_comment(meta))
        if meta.checklist and all(
            item.strip().lower().startswith("[x]") for item in meta.checklist if item.strip()
        ):
            cli_ref = f"{entry.alias}#{issue_number}" if ctx.is_multi_repo else str(issue_number)
            if ctx.board.require_review:
                try:
                    self.move_ticket(ctx, cli_ref, Column.REVIEW)
                except KanbanError:
                    pass
            print(
                f"[herdr-kanban] All checklist items on {disp} are [x] complete! "
                f"Commit all changes and run `herdr-kanban done {cli_ref} --summary \"...\"` "
                f"to move {disp} across the board to Done.",
                file=sys.stderr,
            )
        return meta

    def _infer_code_entry_from_ticket(
        self,
        ctx: RepoContext,
        issue_entry: config.RepoEntry,
        ticket: Ticket | None,
        explicit_repo: str | None = None,
    ) -> config.RepoEntry:
        """Resolve the target code repository for a ticket when issues are centralized in `issues_repo`."""
        if explicit_repo:
            return self.resolve_repo_entry(ctx, explicit_repo)
        if not ctx.is_multi_repo or ticket is None:
            return issue_entry
        for cand in (
            ticket.target_repo,
            ticket.claim.target_repo if ticket.claim else "",
            ticket.deposit.target_repo if ticket.deposit else "",
        ):
            if cand and cand in ctx.repos:
                return ctx.repos[cand]
        for tag in re.findall(r"\[([A-Za-z0-9_.-]+)\]", ticket.title or ""):
            if tag in ctx.repos and tag != issue_entry.alias:
                return ctx.repos[tag]
        return issue_entry

    def _parse_dep_numbers(self, refs: list[TicketRefInput] | None) -> list[int]:
        if not refs:
            return []
        nums: set[int] = set()
        for r in refs:
            if isinstance(r, int):
                if r >= 1:
                    nums.add(r)
            else:
                for part in str(r).split(","):
                    tok = part.strip()
                    if not tok:
                        continue
                    _, n = parse_ticket_ref(tok)
                    nums.add(n)
        return sorted(nums)

    def deposit_issue(
        self,
        ctx: RepoContext,
        title: str,
        *,
        body: str = "",
        parent_issue: TicketRefInput | None = None,
        file_line: str = "",
        ready: bool = False,
        repo_selector: str | None = None,
        labels: list[str] | None = None,
        priority: str = "",
        milestone: str = "",
        depends_on: list[TicketRefInput] | None = None,
        blocks: list[TicketRefInput] | None = None,
        force: bool = False,
    ) -> Ticket:
        target_entry = self.resolve_repo_entry(ctx, repo_selector)
        issue_entry = self.resolve_issues_entry(ctx, fallback_entry=target_entry)
        parent_num: int | None = None
        parent_repo = ""
        if parent_issue is not None:
            if isinstance(parent_issue, int):
                parent_num = parent_issue
                if ctx.is_multi_repo:
                    parent_repo = issue_entry.alias
            else:
                p_sel, p_num = parse_ticket_ref(str(parent_issue))
                parent_num = p_num
                if ctx.is_multi_repo:
                    p_entry = (
                        self.resolve_repo_entry(ctx, p_sel)
                        if p_sel
                        else issue_entry
                    )
                    parent_repo = p_entry.alias

        dep_nums = self._parse_dep_numbers(depends_on)
        block_nums = self._parse_dep_numbers(blocks)
        body_deps, body_blocks = parse_body_dependencies(body, None)
        all_deps = sorted(set(dep_nums) | set(body_deps))
        all_blocks = sorted(set(block_nums) | set(body_blocks))

        norm_labels = self.normalize_labels(ctx, labels)
        if ctx.board.label_filter and ctx.board.label_filter not in norm_labels:
            norm_labels.append(ctx.board.label_filter)

        target_col = Column.READY if (ready or ctx.board.auto_ready_deposits) else Column.TRIAGE
        if target_col == Column.READY and not force:
            gate_errors = validate_ready_gate(
                norm_labels,
                priority,
                ctx.board.facets,
                ctx.board.require_priority_for_ready,
            )
            if gate_errors:
                raise KanbanError(
                    "Ticket cannot enter Ready until required taxonomy facets/priority are set:\n  - "
                    + "\n  - ".join(gate_errors)
                )

        user = self.github.current_user()
        commit_cwd = ctx.worktree_cwd
        if ctx.is_multi_repo and not (commit_cwd / ".git").exists():
            cand = target_entry.resolved_path(ctx.umbrella_root or commit_cwd)
            if (cand / ".git").exists():
                commit_cwd = cand

        deposit = DepositMeta(
            deposited_by=user,
            branch=target_entry.effective_branch(ctx.board),
            parent_issue=parent_num,
            parent_repo=parent_repo,
            target_repo=target_entry.alias if ctx.is_multi_repo else "",
            file_line=file_line,
            commit_sha=self.git.head_sha(commit_cwd) if (commit_cwd / ".git").exists() else "",
            depends_on=all_deps,
            blocks=all_blocks,
        )
        full_body = render_deposit_body(body, deposit)

        milestone_num: int | None = None
        if milestone.strip():
            milestone_num, _ = self.github.ensure_milestone(
                issue_entry.owner,
                issue_entry.repo,
                milestone.strip(),
            )

        issue_kwargs: dict[str, Any] = {
            "body": full_body,
            "labels": norm_labels or None,
        }
        if milestone_num is not None:
            issue_kwargs["milestone_number"] = milestone_num

        try:
            created = self.github.create_issue(
                issue_entry.owner,
                issue_entry.repo,
                title,
                **issue_kwargs,
            )
        except GitHubError as exc:
            if (
                ctx.is_multi_repo
                and "Issues has been disabled" in str(exc)
                and ctx.board.default_repo in ctx.repos
                and issue_entry.alias != ctx.board.default_repo
            ):
                fallback_entry = ctx.repos[ctx.board.default_repo]
                tag_prefix = f"[{target_entry.alias}]"
                if tag_prefix.lower() not in title.lower():
                    title = f"{tag_prefix} {title}"
                issue_entry = fallback_entry
                created = self.github.create_issue(
                    issue_entry.owner,
                    issue_entry.repo,
                    title,
                    **issue_kwargs,
                )
            else:
                raise
        issue_number = int(created["number"])
        node_id = str(created.get("node_id") or "")
        url = str(created.get("html_url") or "")

        schema, _ = self.refresh_board(ctx)
        item_id = ""
        if node_id:
            item_id = self.github.add_issue_to_project(schema.project_id, node_id)
            if item_id:
                self.github.set_item_column(schema, item_id, target_col, ctx.board.columns)
                if priority.strip():
                    self.github.set_item_priority(schema, item_id, priority.strip())

        return Ticket(
            number=issue_number,
            title=title,
            body=full_body,
            state="OPEN",
            url=url,
            author=user,
            labels=norm_labels,
            priority=priority.strip(),
            milestone=milestone.strip(),
            target_repo=target_entry.alias if ctx.is_multi_repo else "",
            depends_on=all_deps,
            blocks=all_blocks,
            column=target_col,
            project_item_id=item_id,
            repo_owner=issue_entry.owner,
            repo_name=issue_entry.repo,
            repo_alias=issue_entry.alias if ctx.is_multi_repo else "",
            deposit=deposit,
        )

    def triage_ticket(
        self,
        ctx: RepoContext,
        ref: TicketRefInput,
        *,
        column: Column | None = None,
        add_labels: list[str] | None = None,
        rm_labels: list[str] | None = None,
        priority: str | None = None,
        milestone: str | None = None,
        target_repo: str | None = None,
        add_depends_on: list[TicketRefInput] | None = None,
        rm_depends_on: list[TicketRefInput] | None = None,
        add_blocks: list[TicketRefInput] | None = None,
        rm_blocks: list[TicketRefInput] | None = None,
        force: bool = False,
    ) -> Ticket:
        schema, tickets = self.refresh_board(ctx)
        entry, issue_number = self.resolve_ticket_target(ctx, ref, tickets)
        ticket = self._find_board_ticket(tickets, entry, issue_number, ctx.is_multi_repo)
        disp = f"{entry.alias}#{issue_number}" if ctx.is_multi_repo else f"#{issue_number}"
        if ticket is None or not ticket.project_item_id:
            raise KanbanError(f"Issue {disp} not found on Project #{ctx.board.project_number}")

        new_labels = self.merge_labels_with_facets(
            ctx,
            ticket.labels,
            add_labels or [],
            rm_labels or [],
        )
        effective_priority = priority.strip() if priority is not None else ticket.priority
        effective_col = column if column is not None else ticket.column

        add_deps = set(self._parse_dep_numbers(add_depends_on))
        rm_deps = set(self._parse_dep_numbers(rm_depends_on))
        add_blks = set(self._parse_dep_numbers(add_blocks))
        rm_blks = set(self._parse_dep_numbers(rm_blocks))

        cur_deps, cur_blks = parse_body_dependencies(ticket.body, ticket.deposit)
        next_deps = sorted((set(cur_deps) | add_deps) - rm_deps - {issue_number})
        next_blks = sorted((set(cur_blks) | add_blks) - rm_blks - {issue_number})

        if add_deps or add_blks:
            edges: dict[int, set[int]] = {t.number: set(t.depends_on) for t in tickets}
            edges[issue_number] = set(next_deps)
            for b_num in next_blks:
                edges.setdefault(b_num, set()).add(issue_number)
            cycle = detect_dependency_cycle(edges)
            if cycle is not None:
                cyc_str = " -> ".join(f"#{n}" for n in cycle)
                raise KanbanError(f"Dependency cycle detected: {cyc_str}")

        if effective_col == Column.READY and not force:
            gate_errors = validate_ready_gate(
                new_labels,
                effective_priority,
                ctx.board.facets,
                ctx.board.require_priority_for_ready,
            )
            if gate_errors:
                raise KanbanError(
                    f"Cannot promote {disp} to Ready until required taxonomy facets/priority are set:\n  - "
                    + "\n  - ".join(gate_errors)
                )

        milestone_num: int | None = None
        if milestone is not None:
            if milestone.strip():
                milestone_num, _ = self.github.ensure_milestone(
                    entry.owner,
                    entry.repo,
                    milestone.strip(),
                )
                ticket.milestone = milestone.strip()
            else:
                milestone_num = 0
                ticket.milestone = ""

        need_body_patch = bool(
            target_repo is not None or add_deps or rm_deps or add_blks or rm_blks
        )
        new_body: str | None = None
        if need_body_patch:
            dep_meta = ticket.deposit or DepositMeta(
                deposited_by=ticket.author or self.github.current_user(),
                branch=entry.effective_branch(ctx.board),
            )
            if target_repo is not None:
                resolved_target = self.resolve_repo_entry(ctx, target_repo)
                dep_meta.target_repo = resolved_target.alias
                ticket.target_repo = resolved_target.alias
            dep_meta.depends_on = next_deps
            dep_meta.blocks = next_blks
            ticket.deposit = dep_meta
            clean_user_body = strip_meta(ticket.body)
            marker = "\n---\n**Deposited via `herdr-kanban`**"
            if marker in clean_user_body:
                clean_user_body = clean_user_body.split(marker, 1)[0].strip()
            new_body = render_deposit_body(clean_user_body, dep_meta)
            ticket.body = new_body

        labels_changed = new_labels != ticket.labels
        if labels_changed or milestone_num is not None or new_body is not None:
            self.github.update_issue(
                entry.owner,
                entry.repo,
                issue_number,
                body=new_body,
                labels=new_labels if labels_changed else None,
                milestone_number=milestone_num,
            )
            ticket.labels = new_labels

        if priority is not None and priority.strip():
            ok_prio = self.github.set_item_priority(schema, ticket.project_item_id, priority.strip())
            if not ok_prio:
                avail_prio = ", ".join(schema.priority_options_by_name) or "(run `herdr-kanban sync-labels`)"
                raise KanbanError(
                    f"Priority '{priority}' not found on Project #{ctx.board.project_number} "
                    f"(available: {avail_prio})"
                )
            ticket.priority = priority.strip()

        if column is not None:
            ok_col = self.github.set_item_column(schema, ticket.project_item_id, column, ctx.board.columns)
            if not ok_col:
                raise KanbanError(
                    f"Project #{ctx.board.project_number} has no Status option "
                    f"mapped to column '{column.value}'"
                )
            ticket.column = column

        ticket.depends_on = next_deps
        ticket.blocks = next_blks
        return ticket

    def move_ticket(self, ctx: RepoContext, ref: TicketRefInput, column: Column) -> Ticket:
        return self.triage_ticket(ctx, ref, column=column, force=True)

    def post_comment(self, ctx: RepoContext, ref: TicketRefInput, body: str) -> dict[str, Any]:
        entry, issue_number = self.resolve_ticket_target(ctx, ref)
        return self.github.post_comment(entry.owner, entry.repo, issue_number, body)

    def ask_ticket(
        self,
        ctx: RepoContext,
        to_issue: TicketRefInput,
        message: str,
        *,
        from_issue: TicketRefInput | None = None,
        urgent: bool = False,
    ) -> dict[str, Any]:
        to_entry, to_num = self.resolve_ticket_target(ctx, to_issue)
        user = self.github.current_user()

        resolved_from: int | None = None
        from_repo_alias = ""
        caller_claim: state.LocalClaim | None = None
        if from_issue is not None:
            from_entry, resolved_from = self.resolve_ticket_target(ctx, from_issue)
            caller_claim = state.get_local_claim(from_entry.slug, resolved_from)
            if ctx.is_multi_repo:
                from_repo_alias = from_entry.alias
        else:
            caller_claim = state.find_claim_by_cwd(ctx.worktree_cwd)
            if caller_claim:
                resolved_from = caller_claim.issue_number
                if ctx.is_multi_repo:
                    from_repo_alias = caller_claim.repo_alias

        from_agent = caller_claim.agent_kind if caller_claim else os.environ.get("HERDR_KANBAN_AGENT", "")
        relay = RelayMeta(
            from_owner=user,
            from_agent=from_agent,
            from_repo=from_repo_alias,
            from_issue=resolved_from,
            to_issue=to_num,
            created_at=utc_now_iso(),
            urgent=urgent,
        )
        posted = self.github.post_comment(
            to_entry.owner,
            to_entry.repo,
            to_num,
            render_relay_comment(relay, message),
        )
        cid = int(posted["id"])

        # Local / herdr-machine fast path when target ticket runs on our own session or saved machine.
        delivered_fast = False
        target_local = state.get_local_claim(to_entry.slug, to_num)
        to_ref_str = f"{to_entry.alias}#{to_num}" if ctx.is_multi_repo else str(to_num)
        from_ref_str = (
            (f"{from_repo_alias}#{resolved_from}" if from_repo_alias else str(resolved_from))
            if resolved_from is not None
            else None
        )
        wakeup_text = self._format_wakeup_prompt(
            to_ref_str,
            user,
            from_agent,
            from_ref_str,
            message,
        )
        if target_local is not None:
            target_handle = target_local.agent_name or target_local.pane_id
            delivered_fast = self.herdr.prompt_if_idle(target_handle, wakeup_text)
            state.append_inbox_item(
                to_entry.slug,
                InboxItem(
                    comment_id=cid,
                    issue_number=to_num,
                    author=user,
                    body=message.strip(),
                    created_at=relay.created_at,
                    from_agent=from_agent,
                    from_repo=from_repo_alias,
                    from_issue=resolved_from,
                    trusted=True,
                    delivered=delivered_fast,
                ),
            )
            state.set_watermark(to_entry.slug, to_num, cid)
        else:
            remote_agent = f"{to_entry.alias}-{to_num}" if ctx.is_multi_repo else f"issue-{to_num}"
            for machine in ctx.settings.machines:
                if self.herdr.prompt_if_idle(remote_agent, wakeup_text, machine=machine):
                    delivered_fast = True
                    break

        return {
            "comment_id": cid,
            "url": posted["url"],
            "to_ref": f"{to_entry.alias}#{to_num}" if ctx.is_multi_repo else f"#{to_num}",
            "delivered_immediately": delivered_fast,
        }

    def complete_ticket(
        self,
        ctx: RepoContext,
        ref: TicketRefInput,
        *,
        summary: str = "",
        approve: bool = False,
        allow_empty: bool = False,
    ) -> LandingResult:
        entry, issue_number = self.resolve_ticket_target(ctx, ref)
        disp = f"{entry.alias}#{issue_number}" if ctx.is_multi_repo else f"#{issue_number}"
        cli_ref = f"{entry.alias}#{issue_number}" if ctx.is_multi_repo else str(issue_number)

        local_claim = state.get_local_claim(entry.slug, issue_number)
        schema_pre, tickets_pre = self.refresh_board(ctx)
        ticket_pre = self._find_board_ticket(tickets_pre, entry, issue_number, ctx.is_multi_repo)
        code_entry = self._infer_code_entry_from_ticket(ctx, entry, ticket_pre)
        main_code_root = self._entry_main_root(ctx, code_entry)

        wt = Path(local_claim.worktree_path) if local_claim else ctx.worktree_cwd
        claim_main_mismatch = bool(
            code_entry.alias != entry.alias
            and local_claim
            and Path(local_claim.main_repo_root).resolve() != main_code_root.resolve()
        )
        if not wt.is_dir() or not (wt / ".git").exists() or claim_main_mismatch:
            wt = main_code_root

        remote = code_entry.effective_remote(ctx.board)
        target = code_entry.effective_branch(ctx.board)
        check_cmd = code_entry.effective_check(ctx.board)
        self.git.fetch_branch(wt, remote, target)
        ahead = self.git.commit_count_ahead(wt, f"{remote}/{target}")
        if ahead == 0 and main_code_root.is_dir() and (main_code_root / ".git").exists():
            self.git.fetch_branch(main_code_root, remote, target)
            main_ahead = self.git.commit_count_ahead(main_code_root, f"{remote}/{target}")
            if main_ahead > 0:
                wt = main_code_root
                ahead = main_ahead

        if self.git.is_rebase_in_progress(wt):
            raise LandingGateError(
                f"A rebase is still in progress in {wt}. Resolve conflicts, run `git rebase --continue`, "
                f"and re-run `herdr-kanban done {cli_ref}`."
            )
        if not self.git.is_clean(wt):
            raise LandingGateError(
                f"Worktree {wt} has uncommitted changes. Commit or clean them before calling `done`."
            )

        if ahead == 0 and not allow_empty:
            raise LandingGateError(
                f"No commits ahead of `{remote}/{target}` in {wt} "
                "(pass `--allow-empty` to close without commits)."
            )

        needs_review = (ctx.board.require_review or (local_claim and local_claim.risk_gate)) and not approve
        if needs_review:
            self._rebase_and_verify(wt, remote, target, check_cmd, cli_ref)
            schema, tickets = self.refresh_board(ctx)
            ticket = self._find_board_ticket(tickets, entry, issue_number, ctx.is_multi_repo)
            if ticket and ticket.project_item_id:
                self.github.set_item_column(schema, ticket.project_item_id, Column.REVIEW, ctx.board.columns)
            if ticket and ticket.claim and ticket.claim_comment_id:
                ticket.claim.state = "review_ready"
                ticket.claim.heartbeat_at = utc_now_iso()
                ticket.claim.touched_files = self.git.touched_files(wt, f"{remote}/{target}")
                if summary:
                    ticket.claim.note = summary
                self.github.patch_comment(
                    entry.owner,
                    entry.repo,
                    ticket.claim_comment_id,
                    render_claim_comment(ticket.claim),
                )
            self.herdr.notify(f"Kanban {disp} ready for Review", f"Approve push to {target} in TUI (`a`).")
            return LandingResult(
                issue_number=issue_number,
                status="review_pending",
                target_branch=target,
                commit_sha=self.git.head_sha(wt),
                repo_alias=entry.alias if ctx.is_multi_repo else "",
            )

        # Rebase -> Check -> Fast-Forward Push (up to 3 attempts on concurrent push race).
        pushed = False
        last_push_err = ""
        for _attempt in range(3):
            self._rebase_and_verify(wt, remote, target, check_cmd, cli_ref)
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
        ticket = self._find_board_ticket(tickets, entry, issue_number, ctx.is_multi_repo)
        cid = local_claim.comment_id if local_claim else (ticket.claim_comment_id if ticket else None)
        if cid and ticket and ticket.claim:
            ticket.claim.state = "completed"
            ticket.claim.heartbeat_at = utc_now_iso()
            ticket.claim.note = summary or f"Landed `{sha[:8]}` onto `{target}`"
            self.github.patch_comment(
                entry.owner,
                entry.repo,
                cid,
                render_claim_comment(ticket.claim),
            )
        elif summary:
            self.github.post_comment(
                entry.owner,
                entry.repo,
                issue_number,
                f"**[herdr-kanban]** Landed `{sha[:8]}` onto `{target}`.\n\n{summary}",
            )

        if ticket and ticket.project_item_id:
            self.github.set_item_column(schema, ticket.project_item_id, Column.DONE, ctx.board.columns)

        closed = False
        if ctx.board.close_issue_on_done:
            self.github.close_issue(entry.owner, entry.repo, issue_number)
            closed = True

        # Auto-unblock downstream tickets whose dependencies are now 100% Done
        unblocked_nums: list[int] = []
        if ticket:
            ticket.column = Column.DONE
            ticket.state = "CLOSED"
        populate_dependency_waves(tickets)
        for other in tickets:
            if other.number == issue_number or other.column == Column.DONE or other.state.upper() == "CLOSED":
                continue
            if issue_number in other.depends_on and not other.unmet_deps:
                unblocked_nums.append(other.number)
                if other.column == Column.BLOCKED and other.project_item_id:
                    self.github.set_item_column(
                        schema,
                        other.project_item_id,
                        Column.READY,
                        ctx.board.columns,
                    )
                    other.column = Column.READY

        removed_wt = False
        if local_claim:
            state.remove_local_claim(entry.slug, issue_number)
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
            repo_alias=entry.alias if ctx.is_multi_repo else "",
            closed_issue=closed,
            worktree_removed=removed_wt,
            unblocked_tickets=sorted(unblocked_nums),
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
            ctx_dir = (
                Path(local_claim.umbrella_root)
                if local_claim.umbrella_root and Path(local_claim.umbrella_root).is_dir()
                else main_root
            )
            try:
                ctx = self.resolve_context(ctx_dir)
            except KanbanError:
                continue

            owner = local_claim.repo_owner or ctx.repo_owner
            repo = local_claim.repo_name or ctx.repo_name
            slug = local_claim.repo_slug
            remote = local_claim.remote or ctx.board.remote
            target_branch = local_claim.target_branch or ctx.board.target_branch

            hb_key = f"{slug}:{local_claim.issue_number}"
            due = force_heartbeat or (
                last_heartbeat_by_key is None
                or (now - last_heartbeat_by_key.get(hb_key, 0.0)) >= ctx.settings.heartbeat_seconds
            )

            try:
                comments = self.github.list_issue_comments(owner, repo, local_claim.issue_number)
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
                                f"{remote}/{target_branch}",
                            )
                        meta.heartbeat_at = utc_now_iso()
                        meta.lease_until = lease_until_iso(ctx.board.lease_ttl_minutes)
                        try:
                            self.github.patch_comment(
                                owner,
                                repo,
                                local_claim.comment_id,
                                render_claim_comment(meta),
                            )
                            if last_heartbeat_by_key is not None:
                                last_heartbeat_by_key[hb_key] = now
                        except GitHubError:
                            pass

            # Relay new comments since watermark
            watermark = state.get_watermark(slug, local_claim.issue_number)
            trusted_set = self.github.trusted_collaborators(
                owner,
                repo,
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
                from_repo = ""
                from_issue: int | None = None
                if meta_dict and meta_dict.get("type") == "relay":
                    relay = RelayMeta.from_dict(meta_dict)
                    from_agent = relay.from_agent
                    from_repo = relay.from_repo
                    from_issue = relay.from_issue
                    same_repo = (not from_repo) or (from_repo == local_claim.repo_alias)
                    if (
                        relay.from_owner == local_claim.owner
                        and from_issue == local_claim.issue_number
                        and same_repo
                    ):
                        continue

                clean_text = strip_meta(body)
                if clean_text.startswith("**[herdr-kanban message"):
                    parts = clean_text.split("\n\n", 1)
                    clean_text = parts[1].strip() if len(parts) > 1 else clean_text

                delivered = False
                to_ref_str = (
                    f"{local_claim.repo_alias}#{local_claim.issue_number}"
                    if local_claim.repo_alias
                    else str(local_claim.issue_number)
                )
                from_ref_str = (
                    (f"{from_repo}#{from_issue}" if from_repo else str(from_issue))
                    if from_issue is not None
                    else None
                )
                if is_trusted:
                    target = local_claim.agent_name or local_claim.pane_id
                    wakeup = self._format_wakeup_prompt(
                        to_ref_str,
                        author,
                        from_agent,
                        from_ref_str,
                        clean_text,
                    )
                    delivered = self.herdr.prompt_if_idle(target, wakeup)
                    if not delivered:
                        self.herdr.notify(
                            f"Kanban {to_ref_str}: message from @{author}",
                            clean_text[:100],
                        )

                inbox_item = InboxItem(
                    comment_id=cid,
                    issue_number=local_claim.issue_number,
                    author=author,
                    body=clean_text,
                    created_at=str(entry.get("created_at") or utc_now_iso()),
                    from_agent=from_agent,
                    from_repo=from_repo,
                    from_issue=from_issue,
                    trusted=is_trusted,
                    delivered=delivered,
                )
                if state.append_inbox_item(slug, inbox_item):
                    delivered_or_queued.append(inbox_item)

            if max_seen > watermark:
                state.set_watermark(slug, local_claim.issue_number, max_seen)

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
        disp = (
            f"{local_claim.repo_alias}#{local_claim.issue_number}"
            if local_claim.repo_alias
            else f"#{local_claim.issue_number}"
        )

        if clean and ahead == 0:
            try:
                ctx_dir = (
                    Path(local_claim.umbrella_root)
                    if local_claim.umbrella_root and Path(local_claim.umbrella_root).is_dir()
                    else main_root
                )
                ctx = self.resolve_context(ctx_dir)
                ref: TicketRefInput = (
                    f"{local_claim.repo_alias}#{local_claim.issue_number}"
                    if local_claim.repo_alias
                    else local_claim.issue_number
                )
                self.release_ticket(
                    ctx,
                    ref,
                    reason="Agent pane closed with zero commits",
                    force_remove_worktree=True,
                )
                self.herdr.notify(
                    f"Kanban {disp} released",
                    "Agent pane closed with no commits; returned to Ready.",
                )
                return "released"
            except (KanbanError, GitError, GitHubError):
                return None

        self.herdr.notify(
            f"Kanban {disp}: work preserved",
            f"Pane closed with changes in {wt}",
        )
        return "preserved"

    def _rebase_and_verify(
        self,
        wt: Path,
        remote: str,
        target: str,
        check_command: str,
        cli_ref: str,
    ) -> None:
        rebase = self.git.rebase_onto_target(wt, remote, target)
        if not rebase.ok:
            files = ", ".join(rebase.conflicted_files) or "(inspect `git status`)"
            raise LandingGateError(
                f"Rebase onto `{remote}/{target}` hit conflicts in: {files}.\n"
                f"Resolve conflicts in {wt}, run `git rebase --continue`, and re-run "
                f"`herdr-kanban done {cli_ref}`.\n{rebase.output}"
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
                    f"Fix the failure in {wt} and re-run `herdr-kanban done {cli_ref}`:\n{tail}"
                )

    def build_agent_prompt(
        self,
        ctx: RepoContext,
        ticket: Ticket,
        claim: state.LocalClaim,
        overlaps: list[FileOverlap],
    ) -> str:
        return self._build_agent_prompt(ctx, ticket, claim, overlaps)

    def _build_agent_prompt(
        self,
        ctx: RepoContext,
        ticket: Ticket,
        claim: state.LocalClaim,
        overlaps: list[FileOverlap],
    ) -> str:
        cli_ref = (
            f"{claim.repo_alias}#{ticket.number}"
            if (ctx.is_multi_repo and claim.repo_alias)
            else str(ticket.number)
        )
        disp = f"#{ticket.number}" if not claim.repo_alias else f"{claim.repo_alias}#{ticket.number}"
        lines = [
            f"You are assigned GitHub Issue {disp}: {ticket.title}",
            f"Repository: {claim.repo_owner}/{claim.repo_name} | Shared branch: `{claim.target_branch}`",
            f"Your current directory (`{claim.worktree_path}`) is an isolated Git worktree "
            f"off `{claim.remote}/{claim.target_branch}`.",
        ]
        if ctx.is_multi_repo:
            sibs = [
                f"`.siblings/{alias}` ({entry.owner}/{entry.repo})"
                for alias, entry in ctx.repos.items()
                if alias != claim.repo_alias
            ]
            if sibs:
                lines.append(
                    "Read-only sibling repositories in this umbrella workspace: " + ", ".join(sibs) + "."
                )
        lines.extend(
            [
                "",
                "## Issue Description",
                strip_meta(ticket.body) or "(No description provided.)",
            ]
        )
        if overlaps:
            lines.extend(["", "## Active Teammate Overlap Warnings"])
            for ov in overlaps:
                files_csv = ", ".join(ov.files)
                lines.append(
                    f"- #{ov.other_issue} (@{ov.other_owner}, {ov.other_agent}) touches: {files_csv}"
                )
        dep_repo_flag = f" --repo {claim.repo_alias}" if (ctx.is_multi_repo and claim.repo_alias) else ""
        ask_example = "<repo#issue>" if ctx.is_multi_repo else "<issue>"
        lines.extend(
            [
                "",
                "## Workflow Commands (`herdr-kanban`)",
                f'- Report progress in-place: `herdr-kanban progress {cli_ref} --note "..."`',
                "- Deposit newly discovered bugs into Triage: "
                f'`herdr-kanban deposit "Title"{dep_repo_flag} --parent {cli_ref} --file-line path:line`',
                "- Ask another active ticket's agent: "
                f'`herdr-kanban ask {ask_example} "..." --from {cli_ref}`',
                "- Check incoming agent replies: `herdr-kanban inbox --ack`",
                f"- When finished & committed cleanly, land onto `{claim.target_branch}`: "
                f'`herdr-kanban done {cli_ref} --summary "..."`',
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _format_wakeup_prompt(
        to_ref: str,
        author: str,
        from_agent: str,
        from_ref: str | None,
        message: str,
    ) -> str:
        to_disp = to_ref if "#" in to_ref else f"#{to_ref}"
        from_disp = (from_ref if "#" in from_ref else f"#{from_ref}") if from_ref else ""
        sender = f"@{author}" + (f" ({from_agent})" if from_agent else "")
        via = f" working on {from_disp}" if from_disp else ""
        reply_hint = (
            f'Reply with `herdr-kanban ask {from_ref} "..." --from {to_ref}`.'
            if from_ref
            else f'Reply with `herdr-kanban comment {to_ref} "..."`.'
        )
        return f"[herdr-kanban message on {to_disp} from {sender}{via}]\n{message}\n\n{reply_hint}"

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
            priority=str(raw.get("priority") or ""),
            milestone=str(raw.get("milestone") or ""),
            target_repo=str(raw.get("target_repo") or ""),
            depends_on=[int(x) for x in (raw.get("depends_on") or [])],
            blocks=[int(x) for x in (raw.get("blocks") or [])],
            unmet_deps=[int(x) for x in (raw.get("unmet_deps") or [])],
            wave=int(raw.get("wave") or 0),
            column=col,
            status_name=str(raw.get("status_name") or ""),
            project_item_id=str(raw.get("project_item_id") or ""),
            repo_owner=str(raw.get("repo_owner") or ""),
            repo_name=str(raw.get("repo_name") or ""),
            repo_alias=str(raw.get("repo_alias") or ""),
            claim=claim,
            claim_comment_id=int(raw["claim_comment_id"])
            if raw.get("claim_comment_id") is not None
            else None,
            stale=bool(claim and claim.state == "active" and is_lease_expired(claim.lease_until)),
            deposit=deposit,
            comments_count=int(raw.get("comments_count") or 0),
            updated_at=str(raw.get("updated_at") or ""),
        )
