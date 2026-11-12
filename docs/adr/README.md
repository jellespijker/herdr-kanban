# Architecture decision records

Format: Michael Nygard's (Context, Decision, Consequences). One decision per file,
numbered, never renumbered. To change a decision, add a new record that supersedes the
old one and set the old one's status to `Superseded by ADR-NNNN`.

| ADR | Title | Status |
|---|---|---|
| [0001](0001-github-as-serverless-coordination-plane.md) | GitHub Projects v2 and single-comment leases as serverless coordination plane | Accepted |
| [0002](0002-ephemeral-worktree-per-ticket-rebased-to-shared-branch.md) | Ephemeral local worktree per ticket, rebased and fast-forward pushed to shared branch | Accepted |
| [0003](0003-comment-relay-with-collaborator-trust-boundary.md) | Cross-colleague agent messaging via GitHub comments with collaborator trust boundary | Accepted |
| [0004](0004-triage-first-for-agent-deposited-issues.md) | Triage-first workflow for agent-deposited issues | Accepted |
| [0005](0005-committed-board-config-with-local-override.md) | Committed `.herdr-kanban.toml` on the shared branch with local overrides | Accepted |
| [0006](0006-python-stdlib-and-external-clis.md) | Python 3.11+ standard library at runtime; `gh`, `git`, and `herdr` as external CLIs | Accepted |
