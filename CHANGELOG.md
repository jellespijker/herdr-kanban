# Changelog

## 0.2.0 (2026-11-12)

- **Umbrella multi-repository workspaces (`ADR-0007`)**:
  - Support a parent folder or Git meta-repo containing multiple child Git checkouts (`[repos.<alias>]`
    in `.herdr-kanban.toml`) sharing one GitHub Project v2 board and shared target branch.
  - Auto-discover child Git repositories during `herdr-kanban init`.
  - Add `herdr-kanban sync-repos` (`--create-branches`) so teammates can clone missing child repos
    and align every repo onto `<target_branch>` in one command.
  - Address cross-repo tickets unambiguously as `<alias>#<number>` (e.g. `firmware#10`, `cloud#18`)
    across CLI (`claim`, `dispatch`, `show`, `progress`, `ask`, `done`, `triage`, `release`) and TUI.
  - Symlink sibling repositories into `.siblings/<alias>` inside ephemeral ticket worktrees (excluded
    via Git `info/exclude`) so sandboxed coding agents can inspect cross-repo APIs/headers while
    `git status` stays completely clean.

## 0.1.0 (2026-11-12)

- Initial release of `herdr-kanban`:
  - Shared-branch multi-colleague collaboration backed by GitHub Projects v2 and Issues.
  - Ephemeral local Git worktrees per claimed ticket, auto-rebased and fast-forward pushed onto
    `origin/<target_branch>` with configurable pre-push `check_command` and optional human `Review`
    gate.
  - Optimistic single-comment leases (`<!-- herdr-kanban:v1 ... -->`) edited in place with
    heartbeats, checklist progress, and file-overlap warnings.
  - Triage-first `deposit` workflow for agent-discovered issues.
  - Cross-colleague agent-to-agent comment relay with collaborator trust boundary and local Herdr
    fast path.
  - Responsive multi-column & compact Kanban TUI (`tab`, `split`, and `popup` placements) plus
    shared `SKILL.md` for Claude Code, Antigravity (`agy`), Codex, OpenCode, Gemini CLI, and
    Copilot CLI.
