"""Command-line entrypoint for humans, coding agents, and Herdr plugin hooks."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from . import __version__, config, daemon, herdr, keys, skill, state
from .engine import (
    KanbanEngine,
    KanbanError,
)
from .git_ops import GitError
from .github import GitHubError, gh_binary, has_project_scope
from .model import COLUMN_TITLES, Column, strip_meta
from .tui import BoardTUI

EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_USAGE = 2


def _wait_if_requested(args: argparse.Namespace) -> None:
    if getattr(args, "wait", False) and sys.stdin.isatty():
        try:
            input("\nPress Enter to close…")
        except EOFError:
            pass


def _emit_json(payload: dict[str, Any]) -> int:
    print(json.dumps(payload, indent=2, sort_keys=True))
    return EXIT_OK


def _discover_child_repos(
    parent_dir: Path, engine: KanbanEngine
) -> list[tuple[str, Path, tuple[str, str] | None]]:
    """Return `(alias, path, (owner, repo) | None)` for immediate child Git checkouts."""
    found: list[tuple[str, Path, tuple[str, str] | None]] = []
    if not parent_dir.is_dir():
        return found
    for child in sorted(parent_dir.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if (child / ".git").exists():
            main_root = engine.git.main_repo_root(child) or child
            remote_info = engine.git.parse_github_remote(main_root)
            found.append((child.name, child, remote_info))
    return found


def _parse_repo_spec(spec: str, default_owner: str) -> config.RepoEntry:
    """Parse `[alias=][owner/]repo[:branch]` into a RepoEntry."""
    raw = spec.strip()
    if not raw:
        raise KanbanError("Empty `--repo` specification")
    if "=" in raw:
        alias_part, rest = raw.split("=", 1)
        alias = alias_part.strip()
    else:
        alias = ""
        rest = raw

    if ":" in rest:
        repo_part, branch_part = rest.split(":", 1)
        target_branch = branch_part.strip()
    else:
        repo_part = rest
        target_branch = ""

    repo_part = repo_part.strip().strip("/")
    if "/" in repo_part:
        owner, repo_name = repo_part.split("/", 1)
    else:
        owner = default_owner
        repo_name = repo_part

    owner = owner.strip()
    repo_name = repo_name.strip()
    alias = alias or repo_name
    if not owner or not repo_name or not alias:
        raise KanbanError(
            f"Invalid `--repo` spec '{spec}' (expected `[alias=][owner/]repo[:branch]`)"
        )
    return config.RepoEntry(
        alias=alias,
        owner=owner,
        repo=repo_name,
        path=alias,
        target_branch=target_branch,
    )


def cmd_init(args: argparse.Namespace, engine: KanbanEngine) -> int:
    cwd = engine.resolve_cwd(Path(args.cwd) if args.cwd else None)
    wt_root = engine.git.worktree_root(cwd)
    child_repos = _discover_child_repos(cwd, engine)
    cli_repo_specs: list[str] = list(getattr(args, "repo", None) or [])
    is_umbrella = bool(
        args.umbrella or child_repos or cli_repo_specs or (wt_root is None and args.umbrella)
    )

    if wt_root is None and not is_umbrella:
        print(
            f"herdr-kanban init: {cwd} is not a Git repository and has no child Git checkouts",
            file=sys.stderr,
        )
        return EXIT_RUNTIME

    meta_owner_repo: tuple[str, str] | None = None
    if wt_root is not None:
        main_root = engine.git.main_repo_root(wt_root) or wt_root
        meta_owner_repo = engine.git.parse_github_remote(main_root)

    if is_umbrella:
        first_remote = next((r for _, _, r in child_repos if r is not None), None) or meta_owner_repo
        first_path = child_repos[0][1] if child_repos else wt_root
        default_owner = args.project_owner or (first_remote[0] if first_remote else "")
        repo_owner = ""
        repo_name = meta_owner_repo[1] if meta_owner_repo else cwd.name
        default_branch = args.branch or (engine.git.current_branch(first_path) if first_path else "main")
        config_root = cwd
    else:
        assert wt_root is not None
        main_root = engine.git.main_repo_root(wt_root) or wt_root
        owner_repo = meta_owner_repo
        default_owner = args.project_owner or (owner_repo[0] if owner_repo else "")
        repo_owner = owner_repo[0] if owner_repo else default_owner
        repo_name = owner_repo[1] if owner_repo else main_root.name
        default_branch = args.branch or engine.git.current_branch(wt_root)
        config_root = main_root

    project_owner = default_owner
    project_number = args.project_number
    target_branch = default_branch
    check_cmd = args.check_cmd or ""
    local_only = bool(args.local_only)
    settings = config.load_user_settings()

    if args.interactive and sys.stdin.isatty():
        print("=== herdr-kanban initialization ===")
        if is_umbrella and child_repos:
            names = ", ".join(alias for alias, _, _ in child_repos)
            print(f"Detected multi-repo umbrella workspace ({len(child_repos)} repos: {names})")
        inp_owner = input(f"GitHub Project owner (org or user) [{project_owner}]: ").strip()
        if inp_owner:
            project_owner = inp_owner
        if project_owner and not project_number and not args.create_project:
            try:
                projects = engine.github.list_projects(project_owner)
                if projects:
                    print(f"\nOpen GitHub Projects v2 under '{project_owner}':")
                    for p in projects[:15]:
                        print(f"  #{p['number']:<4} {p.get('title', '')}")
            except GitHubError as exc:
                print(f"  (Could not list projects automatically: {exc})")
            raw_num = input("Project number (or type `new:<Title>` to create one): ").strip()
            if raw_num.lower().startswith("new:"):
                args.create_project = raw_num[4:].strip() or f"{repo_name} Kanban"
            elif raw_num:
                project_number = int(raw_num)

        inp_branch = input(f"Shared target branch [{target_branch}]: ").strip()
        if inp_branch:
            target_branch = inp_branch
        inp_check = input(f"Pre-push check command (e.g. `pytest`, blank for none) [{check_cmd}]: ").strip()
        if inp_check:
            check_cmd = inp_check
        inp_agent = input(
            f"Your personal default agent kind ({'/'.join(config.SUPPORTED_AGENTS[:6])}) "
            f"[{settings.default_agent_kind}]: "
        ).strip()
        if inp_agent:
            settings.default_agent_kind = inp_agent
            config.save_user_settings(settings)

    if args.default_agent:
        settings.default_agent_kind = args.default_agent
        config.save_user_settings(settings)

    if args.create_project:
        if not project_owner:
            raise KanbanError("`--project-owner` is required when creating a project")
        created = engine.github.create_project(project_owner, args.create_project)
        project_number = int(created["number"])
        print(f"Created GitHub Project #{project_number}: {created.get('url', '')}")

    if not project_owner or not project_number:
        raise KanbanError(
            "Provide `--project-owner` and `--project-number` (or `--create-project` / `--interactive`)."
        )

    repos_map: dict[str, config.RepoEntry] = {}
    default_repo = ""
    if is_umbrella:
        if wt_root is not None and wt_root.resolve() == cwd.resolve() and meta_owner_repo and cli_repo_specs:
            meta_alias = meta_owner_repo[1]
            repos_map[meta_alias] = config.RepoEntry(
                alias=meta_alias,
                owner=meta_owner_repo[0],
                repo=meta_owner_repo[1],
                path=".",
                target_branch=engine.git.current_branch(wt_root),
            )
            default_repo = meta_alias
        for alias, child_path, rem in child_repos:
            r_owner = rem[0] if rem else project_owner
            r_name = rem[1] if rem else alias
            child_branch = engine.git.current_branch(child_path)
            repos_map[alias] = config.RepoEntry(
                alias=alias,
                owner=r_owner,
                repo=r_name,
                path=alias,
                target_branch=child_branch if child_branch != target_branch else "",
            )
        for spec in cli_repo_specs:
            entry = _parse_repo_spec(spec, project_owner)
            repos_map[entry.alias] = entry

    board_cfg = config.BoardConfig(
        project_owner=project_owner,
        project_number=int(project_number),
        target_branch=target_branch,
        repo_owner="" if is_umbrella else (repo_owner or project_owner),
        repo_name="" if is_umbrella else repo_name,
        default_repo=default_repo,
        check_command=check_cmd,
        require_review=bool(args.require_review),
        repos=repos_map,
    )

    dest = (
        config.local_repo_config_path(repo_owner or project_owner, repo_name)
        if (local_only and not is_umbrella)
        else (config_root / config.BOARD_FILENAME)
    )
    if dest.exists() and not args.force and not args.interactive:
        raise KanbanError(f"{dest} already exists (pass `--force` to overwrite)")

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(config.render_board_config_toml(board_cfg), encoding="utf-8")
    if is_umbrella and (config_root / ".git").exists():
        child_paths = [e.path for e in repos_map.values() if e.path != "." and not Path(e.path).is_absolute()]
        engine.git.ensure_gitignore_entries(config_root, child_paths)
    print(f"Wrote board configuration to {dest}")
    if is_umbrella:
        print(
            f"Registered {len(repos_map)} child repo(s): "
            + (", ".join(repos_map) if repos_map else "(add [repos.<alias>] entries)")
        )
        print(
            "Colleagues can run `herdr-kanban sync-repos` to clone missing repos "
            f"and check out `{target_branch}`."
        )
    elif not local_only:
        print(
            f"Next: commit `{config.BOARD_FILENAME}` to `{target_branch}` "
            "so colleagues share this board automatically."
        )
    _wait_if_requested(args)
    return EXIT_OK


def cmd_sync_repos(args: argparse.Namespace, engine: KanbanEngine) -> int:
    ctx = engine.resolve_context(Path(args.cwd) if args.cwd else None)
    rows = engine.sync_repos(ctx, create_branches=args.create_branches)
    if args.json:
        return _emit_json({"repos": rows})
    for row in rows:
        print(f"  [{row['alias']}] {row['repo']} ({row['branch']}) -> {row['status']}  [{row['path']}]")
    _wait_if_requested(args)
    return EXIT_OK


def cmd_sync_labels(args: argparse.Namespace, engine: KanbanEngine) -> int:
    ctx = engine.resolve_context(Path(args.cwd) if args.cwd else None)
    res = engine.sync_labels(ctx)
    if args.json:
        return _emit_json(res)
    print(f"Synchronized taxonomy in {res['issues_repo']}:")
    for lbl in res["labels"]:
        print(f"  [label:{lbl['status']:<9}] {lbl['name']} (#{lbl['color']})")
    for ms in res["milestones"]:
        print(f"  [milestone:{ms['status']:<5}] #{ms['number']} {ms['title']}")
    if res["added_priorities"]:
        print(f"  [priority:added]  {', '.join(res['added_priorities'])}")
    return EXIT_OK


def _filter_tickets(
    ctx: Any,
    engine: KanbanEngine,
    tickets: list[Any],
    *,
    repo: str | None = None,
    column: str | None = None,
    unclaimed: bool = False,
    unblocked: bool = False,
    labels: list[str] | None = None,
    priority: str | None = None,
    milestone: str | None = None,
) -> list[Any]:
    out = list(tickets)
    if repo:
        entry = engine.resolve_repo_entry(ctx, repo)
        key = (entry.owner.lower(), entry.repo.lower())
        out = [
            t
            for t in out
            if t.target_repo == entry.alias or t.repo_key() == key or t.repo_alias == entry.alias
        ]
    if column:
        col = Column(column)
        out = [t for t in out if t.column == col]
    if unclaimed:
        out = [t for t in out if not (t.claim and t.claim.state in ("active", "review_ready"))]
    if unblocked:
        out = [t for t in out if not t.unmet_deps]
    if labels:
        req_labels = set(engine.normalize_labels(ctx, labels))
        out = [t for t in out if req_labels.issubset(set(t.labels))]
    if priority:
        p_lc = priority.strip().lower()
        out = [t for t in out if t.priority.lower() == p_lc or t.priority.lower().startswith(p_lc)]
    if milestone:
        m_lc = milestone.strip().lower()
        out = [t for t in out if m_lc in t.milestone.lower()]
    return engine.sort_tickets_for_display(ctx, out)


def cmd_board(args: argparse.Namespace, engine: KanbanEngine) -> int:
    ctx = engine.resolve_context(Path(args.cwd) if args.cwd else None)
    schema, all_tickets = engine.refresh_board(ctx)
    tickets = _filter_tickets(
        ctx,
        engine,
        all_tickets,
        labels=getattr(args, "label", None),
        priority=getattr(args, "priority", None),
        milestone=getattr(args, "milestone", None),
        unblocked=bool(getattr(args, "unblocked", False)),
    )
    if args.json:
        return _emit_json(engine.build_board_payload(ctx, schema, tickets))

    print(
        f"{schema.title} (#{schema.number}) — {ctx.display_name} "
        f"[branch: {ctx.board.remote}/{ctx.board.target_branch}]"
    )
    for col in Column:
        col_tickets = [t for t in tickets if t.column == col]
        print(f"\n## {COLUMN_TITLES[col]} ({len(col_tickets)})")
        if not col_tickets:
            print("  (none)")
            continue
        for t in col_tickets:
            tags: list[str] = []
            if t.priority:
                tags.append(t.priority)
            if t.target_repo and t.target_repo != t.repo_alias:
                tags.append(f"repo:{t.target_repo}")
            if t.milestone:
                tags.append(f"ms:{t.milestone}")
            if t.unmet_deps:
                deps_s = ",".join(f"#{d}" for d in t.unmet_deps)
                tags.append(f"deps:{deps_s}")
            elif col == Column.READY and (t.depends_on or t.blocks):
                tags.append(f"wave:{t.wave}")
            if t.labels:
                tags.extend(t.labels)
            if t.claim and t.claim.state in ("active", "review_ready"):
                tags.append(f"@{t.claim.owner}:{t.claim.agent_kind}")
            if t.stale:
                tags.append("STALE")
            if t.overlaps:
                tags.append("OVERLAP!")
            suffix = f"  [{', '.join(tags)}]" if tags else ""
            ref_lbl = t.ref(ctx.is_multi_repo)
            print(f"  {ref_lbl:<10} {t.title}{suffix}")
    return EXIT_OK


def cmd_list(args: argparse.Namespace, engine: KanbanEngine) -> int:
    ctx = engine.resolve_context(Path(args.cwd) if args.cwd else None)
    schema, all_tickets = engine.refresh_board(ctx)
    tickets = _filter_tickets(
        ctx,
        engine,
        all_tickets,
        repo=args.repo,
        column=args.column,
        unclaimed=args.unclaimed,
        unblocked=getattr(args, "unblocked", False),
        labels=getattr(args, "label", None),
        priority=getattr(args, "priority", None),
        milestone=getattr(args, "milestone", None),
    )
    if args.json:
        return _emit_json(engine.build_board_payload(ctx, schema, tickets))
    for t in tickets:
        holder = f"@{t.claim.owner}" if t.claim and t.claim.state in ("active", "review_ready") else "-"
        ref_lbl = t.ref(ctx.is_multi_repo)
        prio_lbl = f"[{t.priority}]" if t.priority else "[-]"
        wave_lbl = f"deps:{','.join(f'#{d}' for d in t.unmet_deps)}" if t.unmet_deps else f"wave:{t.wave}"
        print(f"{ref_lbl:<14} [{t.column.value:<11}] {prio_lbl:<6} {wave_lbl:<10} {holder:<16} {t.title}")
    return EXIT_OK


def cmd_show(args: argparse.Namespace, engine: KanbanEngine) -> int:
    ctx = engine.resolve_context(Path(args.cwd) if args.cwd else None)
    _, tickets = engine.refresh_board(ctx)
    entry, issue_num = engine.resolve_ticket_target(ctx, args.issue, tickets)
    key = (entry.owner.lower(), entry.repo.lower())
    ticket = next(
        (t for t in tickets if t.number == issue_num and (not ctx.is_multi_repo or t.repo_key() == key)),
        None,
    )
    comments = engine.github.list_issue_comments(entry.owner, entry.repo, issue_num)
    if ticket is None:
        raw_issue = engine.github.get_issue(entry.owner, entry.repo, issue_num)
        if args.json:
            return _emit_json({"issue": raw_issue, "comments": comments})
        disp = f"{entry.alias}#{issue_num}" if ctx.is_multi_repo else f"#{issue_num}"
        print(f"{disp} {raw_issue.get('title', '')} [{raw_issue.get('state', '')}]")
        print(strip_meta(str(raw_issue.get("body") or "")))
        return EXIT_OK

    if args.json:
        return _emit_json({"ticket": ticket.to_dict(), "comments": comments})

    disp = ticket.ref(ctx.is_multi_repo)
    print(f"{disp} {ticket.title}  [{ticket.column.value}]")
    if ticket.url:
        print(f"URL: {ticket.url}")
    if ticket.priority:
        print(f"Priority: {ticket.priority}")
    if ticket.milestone:
        print(f"Milestone: {ticket.milestone}")
    if ticket.target_repo:
        print(f"Target repository: {ticket.target_repo}")
    if ticket.labels:
        print(f"Labels: {', '.join(ticket.labels)}")
    if ticket.depends_on:
        unmet_set = set(ticket.unmet_deps)
        dep_parts = [f"#{d} (OPEN)" if d in unmet_set else f"#{d} (done)" for d in ticket.depends_on]
        print(f"Depends on: {', '.join(dep_parts)}  [wave {ticket.wave}]")
    if ticket.blocks:
        print(f"Blocks: {', '.join(f'#{b}' for b in ticket.blocks)}")
    if ticket.claim and ticket.claim.state in ("active", "review_ready"):
        stale_lbl = " (STALE)" if ticket.stale else ""
        print(
            f"Claimed by: @{ticket.claim.owner} ({ticket.claim.agent_kind}) "
            f"lease until {ticket.claim.lease_until}{stale_lbl}"
        )
        if ticket.claim.touched_files:
            print(f"Touched files: {', '.join(ticket.claim.touched_files)}")
    for ov in ticket.overlaps:
        ov_ref = (
            f"{ticket.repo_alias}#{ov.other_issue}"
            if (ctx.is_multi_repo and ticket.repo_alias)
            else f"#{ov.other_issue}"
        )
        print(f"Warning — overlaps with {ov_ref} (@{ov.other_owner}): {', '.join(ov.files)}")
    print("\n" + (strip_meta(ticket.body) or "(No description)"))
    return EXIT_OK


def cmd_claim(args: argparse.Namespace, engine: KanbanEngine) -> int:
    ctx = engine.resolve_context(Path(args.cwd) if args.cwd else None)
    res = engine.claim_ticket(
        ctx,
        args.issue,
        agent_kind=args.kind,
        agent_name=getattr(args, "agent_name", "") or "",
        pane_id=getattr(args, "pane_id", "") or "",
        risk_gate=args.risk_gate,
        takeover=args.takeover,
        repo_selector=getattr(args, "repo", None),
        force=bool(getattr(args, "force", False)),
    )
    if args.json:
        prompt = engine.build_agent_prompt(ctx, res.ticket, res.claim, res.overlaps)
        return _emit_json(
            {
                "claim": res.claim.to_dict(),
                "ticket": engine.serialize_ticket_for_board(ctx, res.ticket),
                "reused": res.reused,
                "prompt": prompt,
                "agent_argv": herdr.resolve_agent_argv(res.claim.agent_kind),
            }
        )
    disp = res.ticket.ref(ctx.is_multi_repo)
    print(f"Claimed {disp}: {res.ticket.title}")
    print(
        f"Worktree: {res.claim.worktree_path} "
        f"(branch `{res.claim.local_branch}` -> `{res.claim.target_branch}`)"
    )
    for ov in res.overlaps:
        print(f"Overlap warning: #{ov.other_issue} (@{ov.other_owner}) touches {', '.join(ov.files)}")
    return EXIT_OK


def cmd_dispatch(args: argparse.Namespace, engine: KanbanEngine) -> int:
    ctx = engine.resolve_context(Path(args.cwd) if args.cwd else None)
    daemon.ensure()
    res = engine.dispatch_ticket(
        ctx,
        args.issue,
        agent_kind=args.kind,
        risk_gate=args.risk_gate,
        takeover=args.takeover,
        focus=not args.no_focus,
    )
    if args.json:
        return _emit_json({"claim": res.claim.to_dict(), "ticket": res.ticket.to_dict()})
    disp = res.ticket.ref(ctx.is_multi_repo)
    print(
        f"Dispatched {disp} to `{res.claim.agent_kind}` "
        f"(pane {res.claim.pane_id}, worktree {res.claim.worktree_path})"
    )
    return EXIT_OK


def cmd_release(args: argparse.Namespace, engine: KanbanEngine) -> int:
    ctx = engine.resolve_context(Path(args.cwd) if args.cwd else None)
    entry, issue_num = engine.release_ticket(
        ctx,
        args.issue,
        reason=args.reason or "",
        force_remove_worktree=args.force_worktree,
    )
    disp = f"{entry.alias}#{issue_num}" if ctx.is_multi_repo else f"#{issue_num}"
    if args.json:
        return _emit_json({"released": disp if ctx.is_multi_repo else issue_num})
    print(f"Released {disp} back to Ready.")
    return EXIT_OK


def cmd_deposit(args: argparse.Namespace, engine: KanbanEngine) -> int:
    ctx = engine.resolve_context(Path(args.cwd) if args.cwd else None)
    ticket = engine.deposit_issue(
        ctx,
        args.title,
        body=args.body or "",
        parent_issue=args.parent,
        file_line=args.file_line or "",
        ready=args.ready,
        repo_selector=args.repo,
        labels=getattr(args, "label", None),
        priority=getattr(args, "priority", "") or "",
        milestone=getattr(args, "milestone", "") or "",
        depends_on=getattr(args, "depends_on", None),
        blocks=getattr(args, "blocks", None),
        force=bool(getattr(args, "force", False)),
    )
    if args.json:
        return _emit_json({"ticket": ticket.to_dict()})
    disp = ticket.ref(ctx.is_multi_repo)
    print(f"Deposited {disp} into {ticket.column.value}: {ticket.url or ticket.title}")
    return EXIT_OK


def cmd_triage(args: argparse.Namespace, engine: KanbanEngine) -> int:
    ctx = engine.resolve_context(Path(args.cwd) if args.cwd else None)
    col: Column | None = None
    if args.ready:
        col = Column.READY
    elif args.column:
        col = Column(args.column)

    has_mutation = any(
        (
            col is not None,
            getattr(args, "label", None),
            getattr(args, "rm_label", None),
            getattr(args, "priority", None) is not None,
            getattr(args, "milestone", None) is not None,
            getattr(args, "repo", None) is not None,
            getattr(args, "depends_on", None),
            getattr(args, "rm_depends_on", None),
            getattr(args, "blocks", None),
            getattr(args, "rm_blocks", None),
        )
    )
    if not has_mutation:
        raise KanbanError(
            "Pass at least one triage change (`--ready`, `--column`, `--label/-l`, "
            "`--priority/-p`, `--milestone/-m`, `--repo`, `--depends-on`, or `--blocks`)."
        )

    ticket = engine.triage_ticket(
        ctx,
        args.issue,
        column=col,
        add_labels=getattr(args, "label", None),
        rm_labels=getattr(args, "rm_label", None),
        priority=getattr(args, "priority", None),
        milestone=getattr(args, "milestone", None),
        target_repo=getattr(args, "repo", None),
        add_depends_on=getattr(args, "depends_on", None),
        rm_depends_on=getattr(args, "rm_depends_on", None),
        add_blocks=getattr(args, "blocks", None),
        rm_blocks=getattr(args, "rm_blocks", None),
        force=bool(getattr(args, "force", False)),
    )
    if args.json:
        return _emit_json({"ticket": ticket.to_dict()})
    details: list[str] = [f"column={ticket.column.value}"]
    if ticket.priority:
        details.append(f"priority={ticket.priority}")
    if ticket.milestone:
        details.append(f"milestone='{ticket.milestone}'")
    if ticket.target_repo:
        details.append(f"target_repo={ticket.target_repo}")
    if ticket.labels:
        details.append(f"labels=[{', '.join(ticket.labels)}]")
    if ticket.depends_on:
        details.append(f"depends_on=[{', '.join(f'#{d}' for d in ticket.depends_on)}]")
    print(f"Triaged {ticket.ref(ctx.is_multi_repo)}: {', '.join(details)}")
    return EXIT_OK


def cmd_progress(args: argparse.Namespace, engine: KanbanEngine) -> int:
    ctx = engine.resolve_context(Path(args.cwd) if args.cwd else None)
    meta = engine.update_progress(
        ctx,
        args.issue,
        note=args.note,
        checklist=args.check if args.check else None,
    )
    if args.json:
        return _emit_json({"claim": meta.to_dict()})
    disp = args.issue if "#" in str(args.issue) else f"#{meta.issue_number}"
    print(f"Updated live claim status on {disp} (touched files: {len(meta.touched_files)}).")
    return EXIT_OK


def cmd_comment(args: argparse.Namespace, engine: KanbanEngine) -> int:
    ctx = engine.resolve_context(Path(args.cwd) if args.cwd else None)
    posted = engine.post_comment(ctx, args.issue, args.message)
    if args.json:
        return _emit_json(posted)
    disp = args.issue if "#" in str(args.issue) else f"#{args.issue}"
    print(f"Posted comment on {disp}: {posted.get('url', posted['id'])}")
    return EXIT_OK


def cmd_ask(args: argparse.Namespace, engine: KanbanEngine) -> int:
    ctx = engine.resolve_context(Path(args.cwd) if args.cwd else None)
    res = engine.ask_ticket(
        ctx,
        args.to_issue,
        args.message,
        from_issue=args.from_issue,
        urgent=args.urgent,
    )
    if args.json:
        return _emit_json(res)
    fast = " (woke idle agent immediately)" if res["delivered_immediately"] else ""
    to_disp = res.get("to_ref") or f"#{args.to_issue}"
    print(f"Sent message to {to_disp}{fast}: {res.get('url') or res['comment_id']}")
    return EXIT_OK


def cmd_inbox(args: argparse.Namespace, engine: KanbanEngine) -> int:
    ctx = engine.resolve_context(Path(args.cwd) if args.cwd else None)
    engine.sync_claim_heartbeats_and_relays()

    slugs_and_nums: list[tuple[str, str, int | None]] = []
    if args.issue is not None:
        entry, num = engine.resolve_ticket_target(ctx, args.issue)
        slugs_and_nums.append((entry.slug, entry.alias if ctx.is_multi_repo else "", num))
    else:
        local = state.find_claim_by_cwd(ctx.worktree_cwd)
        if local:
            slugs_and_nums.append(
                (local.repo_slug, local.repo_alias if ctx.is_multi_repo else "", local.issue_number)
            )
        elif ctx.is_multi_repo:
            for alias, entry in ctx.repos.items():
                slugs_and_nums.append((entry.slug, alias, None))
        else:
            slugs_and_nums.append((ctx.slug, "", None))

    tagged_items: list[tuple[str, Any]] = []
    for slug, alias, issue_num in slugs_and_nums:
        if args.ack and issue_num is not None:
            rows = state.update_inbox_flags(slug, issue_num, ack_all=True)
        else:
            rows = state.list_inbox(slug, issue_number=issue_num, include_acked=args.all)
            if args.ack:
                for n in {it.issue_number for it in rows}:
                    state.update_inbox_flags(slug, n, ack_all=True)
        for it in rows:
            tagged_items.append((alias, it))

    if args.json:
        return _emit_json({"messages": [it.to_dict() for _, it in tagged_items]})
    if not tagged_items:
        print("Inbox is empty.")
        return EXIT_OK
    for alias, it in tagged_items:
        trust = "" if it.trusted else " [UNTRUSTED]"
        via = ""
        if it.from_issue:
            from_lbl = f"{it.from_repo}#{it.from_issue}" if it.from_repo else f"#{it.from_issue}"
            via = f" (from {from_lbl})"
        to_lbl = f"{alias}#{it.issue_number}" if alias else f"#{it.issue_number}"
        print(f"[{to_lbl}] @{it.author}{via}{trust} at {it.created_at}:\n  {it.body}\n")
    return EXIT_OK


def cmd_done(args: argparse.Namespace, engine: KanbanEngine) -> int:
    ctx = engine.resolve_context(Path(args.cwd) if args.cwd else None)
    res = engine.complete_ticket(
        ctx,
        args.issue,
        summary=args.summary or "",
        approve=args.approve,
        allow_empty=args.allow_empty,
    )
    if args.json:
        return _emit_json(
            {
                "issue_number": res.issue_number,
                "repo_alias": res.repo_alias,
                "status": res.status,
                "target_branch": res.target_branch,
                "commit_sha": res.commit_sha,
                "closed_issue": res.closed_issue,
                "worktree_removed": res.worktree_removed,
                "unblocked_tickets": res.unblocked_tickets,
            }
        )
    disp = res.ref()
    cli_ref = f"{res.repo_alias}#{res.issue_number}" if res.repo_alias else str(res.issue_number)
    if res.status == "review_pending":
        print(
            f"{disp} rebased & verified (`{res.commit_sha[:8]}`), moved to Review. "
            f"Approve push in TUI (`a`) or run `herdr-kanban done {cli_ref} --approve`."
        )
    else:
        closed_msg = ", GitHub Issue closed" if res.closed_issue else ""
        print(f"Landed {disp} (`{res.commit_sha[:8]}`) onto `{res.target_branch}`{closed_msg}.")
        if res.unblocked_tickets:
            unb = ", ".join(f"#{n}" for n in res.unblocked_tickets)
            print(f"Newly unblocked downstream tickets (ready for parallel execution): {unb}")
    return EXIT_OK


def cmd_whats_new(args: argparse.Namespace, engine: KanbanEngine) -> int:
    ctx = engine.resolve_context(Path(args.cwd) if args.cwd else None)
    cached = engine.load_cached_tickets(ctx)
    if cached is not None and not getattr(args, "refresh", False):
        _, tickets = cached
    else:
        _, tickets = engine.refresh_board(ctx)

    limit = int(getattr(args, "limit", 25) or 25)
    if getattr(args, "prompt", False):
        prompt = engine.build_whats_new_agent_prompt(ctx, tickets, limit=limit)
        if getattr(args, "json", False):
            return _emit_json({"prompt": prompt})
        print(prompt)
        return EXIT_OK

    if getattr(args, "write_summary", None) or getattr(args, "generate", False):
        digest = engine.generate_whats_new_summary(
            ctx,
            tickets,
            custom_markdown=getattr(args, "write_summary", None),
            author=getattr(args, "author", "") or "",
            agent_kind=getattr(args, "agent", "") or "",
        )
    else:
        digest = engine.build_whats_new_digest(ctx, tickets, limit=limit)

    if getattr(args, "json", False):
        return _emit_json(digest)

    saved = digest.get("saved_summary")
    if isinstance(saved, dict) and saved.get("markdown"):
        author = saved.get("author", "agent")
        ag_kind = saved.get("agent_kind", "agent")
        print(f"=== What's New Briefing (by @{author}:{ag_kind}) ===")
        print(saved["markdown"])
        print()
    else:
        print(digest.get("auto_markdown") or "No landed tickets yet.")
    return EXIT_OK



def cmd_open(args: argparse.Namespace, engine: KanbanEngine) -> int:
    daemon.ensure()
    cwd = engine.resolve_cwd(Path(args.cwd) if args.cwd else None)
    prefer_tui = bool(getattr(args, "tui", False))
    if not prefer_tui and skill.MOD_SRC.is_dir():
        _, mod_state = skill.app_mod_status()
        if mod_state != "installed" and (herdr.app_home_dir().is_dir() or getattr(args, "mod", False)):
            try:
                skill.install_app_mod()
            except OSError:
                pass
    res = engine.herdr.open_board_pane(
        placement=args.placement,
        cwd=cwd,
        focus=not args.no_focus,
        prefer_app_mod=not prefer_tui,
    )
    if res is None:
        print(
            "herdr-kanban open: could not open board pane or Herdr-App mod (are you inside Herdr?)",
            file=sys.stderr,
        )
        return EXIT_RUNTIME
    return EXIT_OK


def cmd_install_app_mod(args: argparse.Namespace, engine: KanbanEngine) -> int:
    app_home = Path(args.app_home).expanduser() if getattr(args, "app_home", None) else None
    if getattr(args, "status", False):
        dest, st = skill.app_mod_status(app_home=app_home)
        if getattr(args, "json", False):
            return _emit_json({"mod": skill.MOD_NAME, "path": str(dest), "status": st})
        print(f"{st:<10} {skill.MOD_NAME:<8} {dest}  (Herdr-App GPUI Kanban mod)")
        return EXIT_OK

    machines = list(getattr(args, "on", None) or [])
    report = skill.install_app_mod(
        app_home=app_home,
        uninstall=bool(getattr(args, "uninstall", False)),
        machines=machines,
    )
    opened = False
    if getattr(args, "open", False) and not getattr(args, "uninstall", False):
        cwd = engine.resolve_cwd(Path(args.cwd) if getattr(args, "cwd", None) else None)
        opened = (
            engine.herdr.open_board_pane(
                placement=getattr(args, "placement", "window") or "window",
                cwd=cwd,
                focus=True,
                prefer_app_mod=True,
            )
            is not None
        )
    if getattr(args, "json", False):
        dest, st = skill.app_mod_status(app_home=app_home)
        return _emit_json(
            {
                "mod": skill.MOD_NAME,
                "path": str(dest),
                "status": st,
                "report": report,
                "opened": opened,
            }
        )
    for line in report:
        print(line)
    if getattr(args, "open", False) and opened:
        print("Opened Kanban GPUI mod in Herdr-App.")
    return EXIT_OK


def cmd_tui(args: argparse.Namespace, engine: KanbanEngine) -> int:
    tui = BoardTUI(
        engine,
        cwd=Path(args.cwd) if args.cwd else None,
        compact=args.compact,
    )
    return tui.run()


def cmd_popup(args: argparse.Namespace, engine: KanbanEngine) -> int:
    cwd = engine.resolve_cwd()
    res = engine.herdr.call(
        "plugin",
        "pane",
        "open",
        "--plugin",
        "herdr-kanban",
        "--entrypoint",
        args.name,
        "--cwd",
        str(cwd),
        "--env",
        f"HERDR_KANBAN_REPO={cwd}",
    )
    return EXIT_OK if res is not None else EXIT_RUNTIME


def cmd_event(_args: argparse.Namespace, engine: KanbanEngine) -> int:
    event = os.environ.get("HERDR_PLUGIN_EVENT", "")
    raw = os.environ.get("HERDR_PLUGIN_EVENT_JSON") or "{}"
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = {}
    data = payload.get("data", payload) if isinstance(payload, dict) else {}

    daemon.ensure()
    if event == "pane.closed":
        pane_id = data.get("pane_id") or (data.get("pane") or {}).get("pane_id")
        if isinstance(pane_id, str) and pane_id:
            engine.handle_pane_closed(pane_id)
    elif event == "pane.moved":
        old_pane = data.get("previous_pane_id")
        new_pane = (data.get("pane") or {}).get("pane_id")
        if isinstance(old_pane, str) and isinstance(new_pane, str) and old_pane != new_pane:
            state.move_claim_pane(old_pane, new_pane)
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace, engine: KanbanEngine) -> int:
    checks: list[tuple[str, bool, str]] = []
    checks.append(("python>=3.11", sys.version_info >= (3, 11), sys.version.split()[0]))

    git_bin = shutil.which("git")
    checks.append(("git", bool(git_bin), git_bin or "missing"))

    gh_bin = gh_binary()
    checks.append(("gh", bool(gh_bin), gh_bin or "missing (`https://cli.github.com`)"))
    if gh_bin:
        auth_ok, auth_out = engine.github.auth_check()
        first_line = auth_out.splitlines()[0] if auth_out else ""
        checks.append(
            ("gh auth", auth_ok, first_line or ("authenticated" if auth_ok else "run `gh auth login`"))
        )
        if auth_ok:
            scope_ok = has_project_scope(auth_out)
            checks.append(
                (
                    "gh project scope",
                    scope_ok,
                    "granted"
                    if scope_ok
                    else "missing 'project' scope — run `gh auth refresh -s project,read:project`",
                )
            )

    herdr_ok = engine.herdr.available()
    bin_pairs = (
        ("hc", herdr.hc_binary()),
        ("herdr-app", herdr.app_binary()),
        ("herdr", herdr.binary()),
    )
    bins = [name for name, b in bin_pairs if b]
    checks.append(
        (
            "herdr",
            herdr_ok,
            f"detected ({', '.join(bins)})" if herdr_ok else "outside herdr or missing binary",
        )
    )

    if herdr.app_home_dir().is_dir() or herdr.app_binary():
        mod_dest, mod_st = skill.app_mod_status()
        checks.append(
            (
                "herdr-app mod",
                mod_st == "installed",
                f"{mod_st} ({mod_dest})"
                if mod_st == "installed"
                else f"{mod_st} — run `herdr-kanban install-app-mod`",
            )
        )

    skill_rows = skill.status()
    cli_linked = any(name == "cli" and st == "installed" for name, _, _, st in skill_rows)
    checks.append(
        (
            "skill & CLI links",
            cli_linked,
            "installed" if cli_linked else "run `herdr-kanban install-skill`",
        )
    )

    all_ok = all(ok for _, ok, _ in checks)
    if args.notify and engine.herdr.available() and not all_ok:
        missing = ", ".join(label for label, ok, _ in checks if not ok)
        engine.herdr.notify("herdr-kanban setup needed", f"Check: {missing}")

    for label, ok, detail in checks:
        mark = "OK  " if ok else "WARN"
        if args.brief and ok:
            continue
        print(f"  [{mark}] {label:<18} {detail}")
    if args.brief and all_ok:
        print("  [OK  ] Python, git, gh, herdr, and agent skills are ready.")

    _wait_if_requested(args)
    return EXIT_OK if all_ok else EXIT_RUNTIME


def cmd_install_skill(args: argparse.Namespace, engine: KanbanEngine) -> int:
    if args.status:
        for name, dest, readers, st in skill.status():
            print(f"{st:<10} {name:<8} {dest}  ({readers})")
        if herdr.app_home_dir().is_dir() or skill.MOD_SRC.is_dir():
            mod_dest, mod_st = skill.app_mod_status()
            print(f"{mod_st:<10} {'app-mod':<8} {mod_dest}  (Herdr-App GPUI Kanban mod)")
        return EXIT_OK
    try:
        rows = skill.install(
            args.harness or None,
            uninstall=args.uninstall,
            refresh_only=args.refresh,
        )
        machines = list(getattr(args, "on", None) or [])
        if machines and not args.uninstall:
            rows.extend(skill.install_app_mod(machines=machines))
        for line in rows:
            print(line)
        if args.notify and engine.herdr.available():
            title = "Kanban skill removed" if args.uninstall else "Kanban skill & CLI installed"
            engine.herdr.notify(title, f"{len(rows)} target(s) processed")
    except ValueError as exc:
        print(f"herdr-kanban install-skill: {exc}", file=sys.stderr)
        return EXIT_USAGE
    finally:
        _wait_if_requested(args)
    return EXIT_OK


def cmd_setup_keys(args: argparse.Namespace, engine: KanbanEngine) -> int:
    if getattr(args, "app", False):
        changed, message = keys.setup_app_keys(args.key, remove=args.remove)
        print(message)
        if args.notify and engine.herdr.available():
            engine.herdr.notify("Kanban keybinding", message)
        return EXIT_OK

    changed, message = keys.setup(args.key, remove=args.remove)
    print(message)
    if args.key is None and herdr.app_home_dir().is_dir():
        app_changed, app_msg = keys.setup_app_keys(None, remove=args.remove)
        if app_changed:
            print(app_msg)
    if changed and engine.herdr.available():
        reloaded, failed = engine.herdr.reload_config_everywhere()
        if reloaded:
            print(f"Reloaded herdr config in session(s): {', '.join(reloaded)}")
        if failed:
            print(f"Could not reload session(s): {', '.join(failed)}", file=sys.stderr)
    if args.notify and engine.herdr.available():
        engine.herdr.notify("Kanban keybinding", message)
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="herdr-kanban",
        description="Collaborate with colleagues and coding agents on a shared GitHub branch.",
    )
    parser.add_argument("--version", action="version", version=f"herdr-kanban {__version__}")
    parser.add_argument("--cwd", dest="_root_cwd", help="Repository or umbrella directory")
    sub = parser.add_subparsers(dest="command")

    # init
    p_init = sub.add_parser(
        "init", help="Connect repository or multi-repo umbrella directory to a GitHub Project v2"
    )
    p_init.add_argument("--cwd", help="Repository or umbrella parent directory")
    p_init.add_argument("--project-owner", help="GitHub org or user owning the Project v2")
    p_init.add_argument("--project-number", type=int, help="GitHub Project v2 number")
    p_init.add_argument("--create-project", metavar="TITLE", help="Create a new GitHub Project v2")
    p_init.add_argument("--branch", help="Shared target branch name (defaults to current branch)")
    p_init.add_argument("--check-cmd", help="Command run in worktree before pushing (e.g. `pytest`)")
    p_init.add_argument("--require-review", action="store_true", help="Gate pushes behind Review column")
    p_init.add_argument(
        "--default-agent", choices=config.SUPPORTED_AGENTS, help="Personal default agent kind"
    )
    p_init.add_argument(
        "--umbrella",
        action="store_true",
        help="Initialize an umbrella multi-repository `.herdr-kanban.toml`",
    )
    p_init.add_argument(
        "--repo",
        action="append",
        metavar="[ALIAS=][OWNER/]REPO[:BRANCH]",
        help="Register a child repository in an umbrella workspace (repeatable)",
    )
    p_init.add_argument("--local-only", action="store_true", help="Save config locally instead of repo root")
    p_init.add_argument("--interactive", action="store_true", help="Interactive setup wizard")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing config")
    p_init.add_argument("--wait", action="store_true", help=argparse.SUPPRESS)

    # sync-repos
    p_sync = sub.add_parser(
        "sync-repos",
        help="Clone missing repos in an umbrella workspace and check out the shared target branch",
    )
    p_sync.add_argument("--cwd", help="Umbrella or repository directory")
    p_sync.add_argument(
        "--create-branches",
        action="store_true",
        help="Create and push the shared target branch on repos where it does not exist yet",
    )
    p_sync.add_argument("--json", action="store_true", help="Output JSON")
    p_sync.add_argument("--wait", action="store_true", help=argparse.SUPPRESS)

    # sync-labels
    p_lbl = sub.add_parser(
        "sync-labels",
        help="Synchronize configured [taxonomy.facets.*] labels, milestones, and Project Priority options",
    )
    p_lbl.add_argument("--cwd", help="Umbrella or repository directory")
    p_lbl.add_argument("--json", action="store_true", help="Output JSON")

    # board
    p_board = sub.add_parser("board", help="Show full Kanban board summary")
    p_board.add_argument("--cwd", help="Repository or umbrella directory")
    p_board.add_argument("-l", "--label", action="append", help="Filter by label or facet=value (repeatable)")
    p_board.add_argument("-p", "--priority", help="Filter by Project Priority")
    p_board.add_argument("-m", "--milestone", help="Filter by Milestone title")
    p_board.add_argument("--unblocked", action="store_true", help="Only tickets with zero open dependencies")
    p_board.add_argument("--json", action="store_true", help="Output JSON")

    # list
    p_list = sub.add_parser("list", help="List tickets on the board")
    p_list.add_argument("--cwd", help="Repository or umbrella directory")
    p_list.add_argument("--repo", help="Filter by target or issue repository alias")
    p_list.add_argument("--column", choices=[c.value for c in Column], help="Filter by logical column")
    p_list.add_argument("--unclaimed", action="store_true", help="Only unclaimed tickets")
    p_list.add_argument("--unblocked", action="store_true", help="Only tickets with zero open dependencies")
    p_list.add_argument("-l", "--label", action="append", help="Filter by label or facet=value (repeatable)")
    p_list.add_argument("-p", "--priority", help="Filter by Project Priority")
    p_list.add_argument("-m", "--milestone", help="Filter by Milestone title")
    p_list.add_argument("--json", action="store_true", help="Output JSON")

    # show
    p_show = sub.add_parser("show", help="Inspect a single issue (`42` or `repo#42`) and its claim")
    p_show.add_argument("issue", help="Ticket reference (`42` or `repo#42`)")
    p_show.add_argument("--cwd", help="Repository or umbrella directory")
    p_show.add_argument("--json", action="store_true", help="Output JSON")

    # claim
    p_claim = sub.add_parser("claim", help="Claim a ticket and prepare an isolated local worktree")
    p_claim.add_argument("issue", help="Ticket reference (`42` or `repo#42`)")
    p_claim.add_argument("--cwd", help="Repository or umbrella directory")
    p_claim.add_argument("--repo", help="Override target code repository for the worktree")
    p_claim.add_argument("--kind", help="Agent kind label")
    p_claim.add_argument("--agent-name", help="Herdr agent or terminal label")
    p_claim.add_argument("--pane-id", help="Herdr pane or hc terminal ID")
    p_claim.add_argument("--risk-gate", action="store_true", help="Require human Review approval before push")
    p_claim.add_argument("--takeover", action="store_true", help="Explicitly reclaim a STALE lease")
    p_claim.add_argument("--force", action="store_true", help="Bypass open dependency blocker guard")
    p_claim.add_argument("--json", action="store_true", help="Output JSON")

    # dispatch
    p_disp = sub.add_parser("dispatch", help="Claim ticket, open Herdr tab in worktree, and start agent")
    p_disp.add_argument("issue", help="Ticket reference (`42` or `repo#42`)")
    p_disp.add_argument("--cwd", help="Repository or umbrella directory")
    p_disp.add_argument("--kind", choices=config.SUPPORTED_AGENTS, help="Coding agent to launch")
    p_disp.add_argument("--risk-gate", action="store_true", help="Require human Review approval before push")
    p_disp.add_argument("--takeover", action="store_true", help="Explicitly reclaim a STALE lease")
    p_disp.add_argument("--no-focus", action="store_true", help="Keep focus in current pane")
    p_disp.add_argument("--json", action="store_true", help="Output JSON")

    # release
    p_rel = sub.add_parser("release", help="Release an active claim and return ticket to Ready")
    p_rel.add_argument("issue", help="Ticket reference (`42` or `repo#42`)")
    p_rel.add_argument("--cwd", help="Repository or umbrella directory")
    p_rel.add_argument("--reason", help="Optional note recorded on the claim comment")
    p_rel.add_argument("--force-worktree", action="store_true", help="Remove worktree even if dirty")
    p_rel.add_argument("--json", action="store_true", help="Output JSON")

    # deposit
    p_dep = sub.add_parser("deposit", help="Deposit a newly discovered bug or task into Triage")
    p_dep.add_argument("title", help="Issue title")
    p_dep.add_argument("--repo", help="Target code repository alias in multi-repo workspaces")
    p_dep.add_argument("--body", help="Markdown description")
    p_dep.add_argument("--parent", help="Parent ticket (`42` or `repo#42`) where this was discovered")
    p_dep.add_argument("--file-line", help="Source location, e.g. `src/auth.py:42`")
    p_dep.add_argument("-l", "--label", action="append", help="Label or facet=value (repeatable)")
    p_dep.add_argument("-p", "--priority", help="Project Priority option (e.g. P0, P1, P2, P3)")
    p_dep.add_argument("-m", "--milestone", help="Milestone title")
    p_dep.add_argument("--depends-on", action="append", help="Upstream blocker ticket ref (repeatable)")
    p_dep.add_argument(
        "--blocks",
        action="append",
        help="Downstream ticket ref blocked by this ticket (repeatable)",
    )
    p_dep.add_argument("--ready", action="store_true", help="Place directly in Ready instead of Triage")
    p_dep.add_argument("--force", action="store_true", help="Bypass Ready-gate taxonomy check")
    p_dep.add_argument("--cwd", help="Repository or umbrella directory")
    p_dep.add_argument("--json", action="store_true", help="Output JSON")

    # triage
    p_tri = sub.add_parser(
        "triage",
        help="Update a ticket's column, labels, priority, milestone, target repo, or dependency DAG",
    )
    p_tri.add_argument("issue", help="Ticket reference (`42` or `repo#42`)")
    grp = p_tri.add_mutually_exclusive_group(required=False)
    grp.add_argument("--ready", action="store_true", help="Promote to Ready")
    grp.add_argument("--column", choices=[c.value for c in Column], help="Target column")
    p_tri.add_argument("-l", "--label", action="append", help="Add/set label or facet=value (repeatable)")
    p_tri.add_argument("--rm-label", action="append", help="Remove label (repeatable)")
    p_tri.add_argument("-p", "--priority", help="Set Project Priority option (e.g. P0, P1, P2, P3)")
    p_tri.add_argument("-m", "--milestone", help="Set Milestone title (pass '' to clear)")
    p_tri.add_argument("--repo", help="Set target code repository alias")
    p_tri.add_argument("--depends-on", action="append", help="Add upstream blocker ticket ref (repeatable)")
    p_tri.add_argument(
        "--rm-depends-on",
        action="append",
        help="Remove upstream blocker ticket ref (repeatable)",
    )
    p_tri.add_argument("--blocks", action="append", help="Add downstream blocked ticket ref (repeatable)")
    p_tri.add_argument(
        "--rm-blocks",
        action="append",
        help="Remove downstream blocked ticket ref (repeatable)",
    )
    p_tri.add_argument("--force", action="store_true", help="Bypass Ready-gate taxonomy validation")
    p_tri.add_argument("--cwd", help="Repository or umbrella directory")
    p_tri.add_argument("--json", action="store_true", help="Output JSON")

    # progress
    p_prog = sub.add_parser("progress", help="Update your live claim comment in-place (zero spam)")
    p_prog.add_argument("issue", help="Ticket reference (`42` or `repo#42`)")
    p_prog.add_argument("--note", help="Short status note")
    p_prog.add_argument("--check", action="append", help="Checklist item, e.g. `[x] Parser` (repeatable)")
    p_prog.add_argument("--cwd", help="Repository or umbrella directory")
    p_prog.add_argument("--json", action="store_true", help="Output JSON")

    # comment
    p_com = sub.add_parser("comment", help="Post a regular Markdown comment on an issue")
    p_com.add_argument("issue", help="Ticket reference (`42` or `repo#42`)")
    p_com.add_argument("message", help="Comment body")
    p_com.add_argument("--cwd", help="Repository or umbrella directory")
    p_com.add_argument("--json", action="store_true", help="Output JSON")

    # ask
    p_ask = sub.add_parser("ask", help="Send a cross-agent relay message to another ticket's agent")
    p_ask.add_argument("to_issue", help="Target ticket reference (`42` or `repo#42`)")
    p_ask.add_argument("message", help="Question or coordination message")
    p_ask.add_argument("--from", dest="from_issue", help="Your current ticket (`42` or `repo#42`)")
    p_ask.add_argument("--urgent", action="store_true", help="Mark relay urgent")
    p_ask.add_argument("--cwd", help="Repository or umbrella directory")
    p_ask.add_argument("--json", action="store_true", help="Output JSON")

    # inbox
    p_inb = sub.add_parser("inbox", help="Read incoming agent-to-agent relay messages")
    p_inb.add_argument("--issue", help="Filter to a specific ticket (`42` or `repo#42`)")
    p_inb.add_argument("--ack", action="store_true", help="Acknowledge returned messages")
    p_inb.add_argument("--all", action="store_true", help="Include previously acknowledged messages")
    p_inb.add_argument("--cwd", help="Repository or umbrella directory")
    p_inb.add_argument("--json", action="store_true", help="Output JSON")

    # done
    p_done = sub.add_parser(
        "done", help="Rebase worktree, run checks, push to shared branch, and close issue"
    )
    p_done.add_argument("issue", help="Ticket reference (`42` or `repo#42`)")
    p_done.add_argument("--summary", help="Completion summary posted to the issue")
    p_done.add_argument("--approve", action="store_true", help="Approve human Review gate and push")
    p_done.add_argument("--allow-empty", action="store_true", help="Allow closing with zero new commits")
    p_done.add_argument("--cwd", help="Repository or umbrella directory")
    p_done.add_argument("--json", action="store_true", help="Output JSON")

    # whats-new
    p_wn = sub.add_parser(
        "whats-new",
        help="Summarize recently landed work across colleagues, coding agents, and repositories",
    )
    p_wn.add_argument("--limit", type=int, default=25, help="Max landed tickets to include")
    p_wn.add_argument("--refresh", action="store_true", help="Force live GitHub refresh before summarizing")
    p_wn.add_argument(
        "--generate",
        action="store_true",
        help="Synthesize & save an executive Markdown briefing combining landed tickets and git log",
    )
    p_wn.add_argument(
        "--write-summary",
        metavar="MARKDOWN",
        help="Save an agent-authored Markdown summary for the Herdr-App What's New view",
    )
    p_wn.add_argument("--author", help="Author handle for --write-summary")
    p_wn.add_argument("--agent", help="Agent kind for --write-summary")
    p_wn.add_argument(
        "--prompt",
        action="store_true",
        help="Print an agent prompt to synthesize recently landed team work",
    )
    p_wn.add_argument("--cwd", help="Repository or umbrella directory")
    p_wn.add_argument("--json", action="store_true", help="Output JSON")


    # open & tui
    p_open = sub.add_parser(
        "open", help="Open the Kanban board in Herdr-App (GPUI mod) or a Herdr terminal tab/split/popup"
    )
    p_open.add_argument(
        "--placement",
        choices=("tab", "split", "popup", "window", "overlay"),
        default="split",
    )
    p_open.add_argument("--cwd", help="Repository or umbrella directory")
    p_open.add_argument("--tui", action="store_true", help="Force terminal TUI instead of GPUI mod")
    p_open.add_argument("--mod", action="store_true", help="Ensure and prefer Herdr-App GPUI mod")
    p_open.add_argument("--no-focus", action="store_true")

    p_tui = sub.add_parser("tui", help="Run the interactive Kanban TUI directly in this terminal")
    p_tui.add_argument("--cwd", help="Repository or umbrella directory")
    p_tui.add_argument("--compact", action="store_true", help="Single-column compact layout")

    p_mod = sub.add_parser(
        "install-app-mod",
        help="Install or sync the native Herdr-App GPUI Kanban mod (`~/.config/herdr-app/mods/herdr-kanban`)",
    )
    p_mod.add_argument("--app-home", help="Override Herdr-App config home directory")
    p_mod.add_argument(
        "--on",
        action="append",
        metavar="MACHINE",
        help="Also sync mod files to a connected `hc` peer machine (repeatable, e.g. `--on peer-host`)",
    )
    p_mod.add_argument("--status", action="store_true", help="Check GPUI mod installation status")
    p_mod.add_argument("--uninstall", action="store_true", help="Remove installed GPUI mod")
    p_mod.add_argument("--open", action="store_true", help="Open the GPUI mod in Herdr-App after installing")
    p_mod.add_argument(
        "--placement",
        choices=("tab", "split", "popup", "window", "overlay"),
        default="split",
    )
    p_mod.add_argument("--cwd", help="Repository or umbrella directory when opening")
    p_mod.add_argument("--json", action="store_true", help="Output JSON")

    # plugin plumbing
    p_pop = sub.add_parser("popup", help=argparse.SUPPRESS)
    p_pop.add_argument("name")

    sub.add_parser("event", help=argparse.SUPPRESS)
    sub.add_parser("daemon", help=argparse.SUPPRESS)

    p_doc = sub.add_parser("doctor", help="Check Python, git, gh auth, herdr, and installed skills")
    p_doc.add_argument("--brief", action="store_true")
    p_doc.add_argument("--notify", action="store_true")
    p_doc.add_argument("--wait", action="store_true", help=argparse.SUPPRESS)

    p_sk = sub.add_parser(
        "install-skill",
        help="Symlink SKILL.md, CLI, and Herdr-App GPUI mod for Claude, agy, Codex, OpenCode, Herdr-App",
    )
    p_sk.add_argument("harness", nargs="*")
    p_sk.add_argument("--status", action="store_true")
    p_sk.add_argument("--uninstall", action="store_true")
    p_sk.add_argument("--refresh", action="store_true", help="Relink existing outdated herdr-kanban links")
    p_sk.add_argument(
        "--on",
        action="append",
        metavar="MACHINE",
        help="Also sync GPUI mod to a connected `hc` peer machine (repeatable)",
    )
    p_sk.add_argument("--notify", action="store_true")
    p_sk.add_argument("--wait", action="store_true", help=argparse.SUPPRESS)

    p_keys = sub.add_parser(
        "setup-keys",
        help="Add or remove Kanban keybinding in herdr config.toml and/or herdr-app keys.json",
    )
    p_keys.add_argument("--key", help="Custom key chord (default: prefix+m or ctrl-shift-m with --app)")
    p_keys.add_argument("--app", action="store_true", help="Configure ~/.config/herdr-app/keys.json")
    p_keys.add_argument("--remove", action="store_true")
    p_keys.add_argument("--notify", action="store_true")

    return parser


HANDLERS = {
    "init": cmd_init,
    "sync-repos": cmd_sync_repos,
    "sync-labels": cmd_sync_labels,
    "board": cmd_board,
    "list": cmd_list,
    "show": cmd_show,
    "claim": cmd_claim,
    "dispatch": cmd_dispatch,
    "release": cmd_release,
    "deposit": cmd_deposit,
    "triage": cmd_triage,
    "progress": cmd_progress,
    "comment": cmd_comment,
    "ask": cmd_ask,
    "inbox": cmd_inbox,
    "done": cmd_done,
    "whats-new": cmd_whats_new,
    "open": cmd_open,
    "tui": cmd_tui,
    "install-app-mod": cmd_install_app_mod,
    "popup": cmd_popup,
    "event": cmd_event,
    "doctor": cmd_doctor,
    "install-skill": cmd_install_skill,
    "setup-keys": cmd_setup_keys,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "_root_cwd", None) and not getattr(args, "cwd", None):
        args.cwd = args._root_cwd
    if not args.command:
        parser.print_help()
        return EXIT_USAGE
    if args.command == "daemon":
        return daemon.run()

    engine = KanbanEngine()
    handler = HANDLERS[args.command]
    try:
        return handler(args, engine)
    except (KanbanError, GitError, GitHubError, RuntimeError) as exc:
        if getattr(args, "json", False):
            print(json.dumps({"error": str(exc)}), file=sys.stderr)
        else:
            print(f"herdr-kanban {args.command}: {exc}", file=sys.stderr)
        return EXIT_RUNTIME
