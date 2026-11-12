# ADR-0002: Ephemeral local worktree per ticket, rebased and fast-forward pushed to shared branch

- Status: Accepted
- Date: 2026-11-12

## Context

Multiple agents on one developer's machine cannot safely edit a single Git working tree at the
same time, and coding agent sandboxes (Claude Code, Codex, Antigravity) scope write permissions
to the `cwd` where the agent process started. Meanwhile, colleagues want continuous integration
onto one shared branch without micro-PR overhead on every routine task.

## Decision

- Every claimed ticket gets an **ephemeral local Git worktree** branched from
  `origin/<target_branch>`.
- TUI dispatch creates the Herdr tab directly inside that worktree (`--cwd`) before calling
  `herdr agent start`, so the agent's root workspace is the isolated worktree.
- Landing (`herdr-kanban done`) fetches `origin/<target_branch>`, rebases the worktree, runs the
  configured `check_command` (with an optional per-ticket/board `Review` gate), fast-forward pushes
  `HEAD:refs/heads/<target_branch>` (never `--force`), closes the GitHub Issue immediately, moves
  the Project card to `Done`, and cleans up the worktree.

## Consequences

- Parallel local agents never dirty each other's index or working tree.
- Sandboxed agents work inside their initial `cwd` without cross-directory write failures.
- Rebase conflicts or check failures stay isolated in the ticket's worktree for the agent to fix
  before anything touches `origin/<target_branch>`.
