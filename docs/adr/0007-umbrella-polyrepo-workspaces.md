# ADR-0007: Umbrella multi-repository workspaces and `.siblings/` worktree links

- Status: Accepted
- Date: 2026-11-12

## Context

Teams often coordinate one feature or release across several repositories at once (for example
`firmware`, `cloud`, and `software`) on a single GitHub Project v2 board. Running separate
single-repo boards hides cross-repo dependencies and prevents agents from easily inspecting
adjacent repositories' interfaces or messaging sibling repos' agents (`firmware#42` <-> `cloud#18`).
At the same time, coding agent sandboxes scope write and read access to the directory tree where
the agent started.

## Decision

- Support an **umbrella `.herdr-kanban.toml`** in a parent directory (either a plain folder or a
  Git meta-repo) declaring child repositories under `[repos.<alias>]` (`owner`, `repo`, `path`,
  and optional per-repo `target_branch`, `remote`, and `check_command` overrides).
- `herdr-kanban init` auto-discovers child Git checkouts when run in a parent folder, and
  `herdr-kanban sync-repos` clones any missing repositories and checks out each repo's target
  branch so colleagues can bootstrap an identical multi-repo workspace in one command.
- Address tickets across CLI and TUI as `<alias>#<number>` (e.g., `cloud#18`), while still
  accepting bare `<number>` whenever unambiguous or inside that repo's worktree.
- When creating an ephemeral worktree for a ticket in an umbrella workspace, link locally present
  sibling repos into `.siblings/<alias>` inside the worktree and add `/.siblings/` to Git's local
  `info/exclude` so `git status` stays clean while sandboxed agents can read sibling repos.

## Consequences

- One unified Kanban board coordinates all repositories in the GitHub Project v2 without merging
  their Git histories.
- Ticket claims, inboxes, file-overlap checks, and TUI state key on `(repo_slug, issue_number)`,
  eliminating cross-repo issue number collisions (`firmware#10` vs `cloud#10`).
