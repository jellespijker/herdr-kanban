# ADR-0005: Committed `.herdr-kanban.toml` on the shared branch with local overrides

- Status: Accepted
- Date: 2026-11-12

## Context

All colleagues collaborating on `<target_branch>` need identical Project v2 bindings, column
mappings, `check_command`, and lease TTLs, while keeping personal preferences (`default_agent_kind`,
worktree location, saved `herdr` machines) private and still supporting repos where committing
tool dotfiles is disallowed.

## Decision

- `herdr-kanban init` writes `.herdr-kanban.toml` in the repository root on `<target_branch>` (or,
  with `--local-only`, writes to `~/.config/herdr/plugins/config/herdr-kanban/repos/<owner>__<repo>.toml`).
- Config loading merges: defaults -> committed `.herdr-kanban.toml` (resolved from current tree or
  `git --git-common-dir` parent) -> local repo override -> personal `settings.toml`.

## Consequences

- Colleagues checking out `<target_branch>` get zero-setup board alignment, while personal agent
  preferences never pollute Git.
