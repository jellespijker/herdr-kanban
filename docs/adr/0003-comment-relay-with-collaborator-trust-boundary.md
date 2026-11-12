# ADR-0003: Cross-colleague agent messaging via GitHub comments with collaborator trust boundary

- Status: Accepted
- Date: 2026-11-12

## Context

`herdr machine` tunnels full shell and pane control over SSH, which fits personal remote machines
or shared team dev servers, not colleagues' personal laptops behind NAT or separate trust domains.
Still, agents working on related tickets on the same branch need to ask each other questions.

## Decision

- Use structured GitHub Issue comments (`type = "relay"`) as a durable cross-colleague mailbox,
  combined with a local/`herdr --machine` fast path when two agents share a session or linked SSH
  machine.
- Each colleague's local `herdr-kanban` daemon polls comments on locally claimed issues:
  - Only comments authored by **repo collaborators or configured `trusted_authors`** may
    auto-prompt an `idle`/`done` local agent via `herdr agent prompt`.
  - If the local agent is `working`/`blocked`, trusted messages queue in the local inbox and raise
    a Herdr notification.
  - Comments from untrusted GitHub accounts are marked `trusted = false` and never injected into an
    agent prompt.

## Consequences

- Works across NAT, VPNs, and offline/online schedules with a permanent audit trail on the issue.
- Prevents prompt-injection via drive-by issue comments on public repositories.
