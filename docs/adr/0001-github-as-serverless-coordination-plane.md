# ADR-0001: GitHub Projects v2 and single-comment leases as serverless coordination plane

- Status: Accepted
- Date: 2026-11-12

## Context

Colleagues running separate Herdr sessions on separate machines want their agents to share a
single Kanban backlog without deploying or operating a custom lock/state server. Concurrent
agents may attempt to claim the same Ready ticket simultaneously, and laptops may sleep or
agent panes may exit mid-ticket.

## Decision

- Use **GitHub Projects v2** (`Status` single-select field) for board columns and **GitHub Issues**
  for ticket records.
- Represent an active claim via **one structured GitHub Issue comment** embedding
  `<!-- herdr-kanban:v1 {"type":"claim",...} -->`.
- **Optimistic race resolution**: After posting a claim comment, read back the issue's comments in
  ascending GitHub comment ID order. The earliest active unexpired claim wins; losing claimants
  immediately delete their comment and abort.
- **In-place heartbeats**: Heartbeats, checklist progress, and `touched_files` `PATCH` that same
  comment ID in place rather than posting new comments.
- **Stale leases & cleanup**: Expired leases (`now > lease_until`, default 30m) are flagged `STALE`
  and require explicit `--takeover`. Local `pane.closed` events auto-release a claim only when the
  worktree is clean and has zero commits ahead of the shared branch.

## Consequences

- Zero custom infrastructure; authentication piggybacks entirely on each developer's `gh` login.
- Zero notification spam on watched repositories because heartbeats edit one comment in place.
- Claim latency is two HTTP round-trips (`POST` comment + `GET` comments), acceptable for ticket
  granularity.
