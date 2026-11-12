---
name: herdr-kanban
description: "Coordinate work with teammates and sibling coding agents on a shared Git branch via GitHub Projects v2 and Issues. Use whenever working in a repo with .herdr-kanban.toml, when asked to check the Kanban board, claim or finish a ticket, deposit a discovered bug/follow-up issue, report progress, or ask another ticket's agent a question."
---

# herdr-kanban

`herdr-kanban` coordinates multiple colleagues and multiple coding agents working concurrently on
one shared GitHub branch (`origin/<target_branch>`), using isolated local Git worktrees per ticket
and GitHub Projects v2 + Issues as the shared board.

Run `herdr-kanban` as a plain command (pass `--json` when you want structured JSON output).

## Check the board & your inbox

```bash
herdr-kanban board                 # overview of columns, active claims, and file overlaps
herdr-kanban list --column ready   # unclaimed Ready tickets
herdr-kanban show 42               # full ticket body, lease holder, touched files, comments
herdr-kanban inbox --ack           # check & acknowledge messages from sibling/colleague agents
```

## Claim & work on a ticket

If this pane was dispatched from the Herdr Kanban TUI, your ticket is already claimed and your
current working directory (`$PWD`) is already the isolated worktree for that ticket.

If you are claiming a Ready ticket yourself:

```bash
herdr-kanban claim 42 --json
```

`claim` creates an isolated worktree off `origin/<target_branch>` (or reuses it if already inside
it), acquires a lease on GitHub Issue `#42` (first comment ID wins ties), moves the card to
`In Progress`, and warns you if another active ticket is touching overlapping files. Work inside
the returned `worktree_path`.

## Report progress & deposit newly discovered issues

Do **not** expand scope inside your current ticket when you find unrelated bugs or follow-ups.
Deposit them into `Triage` instead so a human can promote them to `Ready`:

```bash
herdr-kanban deposit "Fix race in token refresh" \
  --body "Observed unlocked read when refreshing expired session." \
  --parent 42 \
  --file-line "src/auth/refresh.py:88"
```

Update your live claim status in place (edits your single claim comment—zero comment spam):

```bash
herdr-kanban progress 42 --note "Parser done, adding edge-case tests" \
  --check "[x] Parser" --check "[ ] Unit tests"
```

## Ask another agent or colleague a question

When another active ticket touches files you depend on (shown in `overlap_warnings`), ask that
ticket directly:

```bash
herdr-kanban ask 41 "Are you changing AuthSession's constructor signature?" --from 42
```

This posts a structured relay comment on `#41` and wakes the colleague's (or your own) agent
working on `#41`. Check replies anytime with `herdr-kanban inbox --ack`.

## Finish & land onto the shared branch

Commit all your changes cleanly inside your ticket worktree, then run:

```bash
herdr-kanban done 42 --summary "Implemented parser and added unit tests"
```

`done` automatically:
1. Fetches `origin/<target_branch>` and rebases your worktree onto the latest commits.
2. Runs the board's configured `check_command` (if any).
   - If rebase conflicts or tests fail, `done` exits non-zero with details—fix the conflict or
     failing test in your worktree and run `herdr-kanban done 42` again.
   - If you are genuinely blocked on a human decision, run `herdr-kanban triage 42 --column blocked`
     and explain why via `herdr-kanban comment 42 "..."`.
3. Fast-forward pushes to `origin/<target_branch>` (or moves to `Review` if the ticket/board uses
   a human review gate).
4. Moves the Project card to `Done` and closes GitHub Issue `#42`.
