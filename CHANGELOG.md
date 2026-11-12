# Changelog

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
