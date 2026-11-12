# ADR-0004: Triage-first workflow for agent-deposited issues

- Status: Accepted
- Date: 2026-11-12

## Context

Agents frequently uncover latent bugs, refactors, or follow-up tasks while working on a ticket. If
agent-filed issues enter `Ready` automatically, concurrent agents can trigger unbounded scope creep.

## Decision

- `herdr-kanban deposit` places newly created issues into the **`Triage`** column by default,
  recording `parent_issue`, `file_line`, `branch`, and `commit_sha` in `DepositMeta`.
- Humans promote triaged items to `Ready` with a single keystroke (`t`) in the Kanban TUI (or
  `herdr-kanban triage <N> --ready`), unless a board explicitly opts into `auto_ready_deposits = true`
  in `.herdr-kanban.toml`.

## Consequences

- Agents capture findings immediately instead of losing context or yak-shaving inside unrelated
  commits, while humans retain scope control over `Ready`.
