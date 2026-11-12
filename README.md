# herdr-kanban

Collaborate with your colleagues—and combine all of your coding agents—on **one shared GitHub
branch**, coordinated through **GitHub Projects v2** and **GitHub Issues** inside
[Herdr](https://herdr.dev).

No custom backend server, no mutual SSH access between colleagues' laptops, and zero Python
runtime dependencies beyond Python 3.11+, `git`, and `gh`.

---

## How It Works

1. **One shared branch, isolated local worktrees**:
   - Your team points `.herdr-kanban.toml` at a GitHub Project v2 board and a shared target branch
     (for example `feat/unified-auth`).
   - Whenever an issue is claimed or dispatched, `herdr-kanban` creates an ephemeral local Git
     worktree off `origin/<target-branch>` and opens a Herdr tab inside it so Claude Code,
     Antigravity (`agy`), Codex, OpenCode, Gemini CLI, or Copilot CLI runs with full sandbox access
     and never dirties another local agent's working tree.
2. **Conflict-free distributed claims (zero comment spam)**:
   - Claiming posts **one** structured comment (`<!-- herdr-kanban:v1 ... -->`). Earliest GitHub
     comment ID wins simultaneous races; losers roll back automatically.
   - Heartbeats, progress checklists, and **touched file paths** `PATCH` that same comment in
     place—giving teammates live file-overlap warnings without notification spam.
   - Stale leases (>30m) are flagged `STALE` for explicit takeover; local panes closed with zero
     commits auto-release back to `Ready`.
3. **Triage-first issue deposits**:
   - Agents deposit newly discovered bugs/follow-ups via `herdr-kanban deposit`, landing in
     `Triage` (linked to parent issue + `file:line`) until a human promotes them to `Ready` with
     `t` in the TUI.
4. **Cross-colleague agent-to-agent messaging**:
   - `herdr-kanban ask <issue> "..."` posts a structured relay comment and wakes the target agent
     when authored by a trusted collaborator (or uses the local / `herdr --machine` fast path).
5. **Guarded shared-branch landing**:
   - `herdr-kanban done <issue>` fetches `origin/<target-branch>`, rebases the worktree, runs the
     board's `check_command`, fast-forward pushes to `origin/<target-branch>` (or pauses in
     `Review` when gated), moves the card to `Done`, and closes the GitHub Issue immediately.

---

## Requirements

- [Herdr](https://herdr.dev) `>= 0.9.3`
- Python `>= 3.11`
- `git`
- GitHub CLI (`gh`) authenticated with `repo` and `project` scopes:
  ```bash
  gh auth login
  gh auth refresh -s project
  ```

---

## Install

From GitHub:

```bash
herdr plugin install jellespijker/herdr-kanban
```

Or link a local checkout during development:

```bash
herdr plugin link /path/to/herdr-kanban
```

Then run two one-time setup commands (or trigger them from Herdr's plugin action menu):

```bash
bin/herdr-kanban install-skill   # links SKILL.md + ~/.local/bin/herdr-kanban for Claude, agy, Codex, OpenCode
bin/herdr-kanban setup-keys      # binds prefix+m to open the Kanban board tab
```

---

## Quickstart on a Shared Branch

1. On your shared branch, connect a GitHub Project v2:
   ```bash
   herdr-kanban init --interactive
   git add .herdr-kanban.toml && git commit -m "Configure herdr-kanban board" && git push
   ```
   When your colleagues pull the branch, their `herdr-kanban` connects automatically.

2. Open the Kanban board inside Herdr (`prefix+m` or `herdr-kanban open`):
   - `h` / `l`: switch columns (`Triage`, `Ready`, `In Progress`, `Blocked / Review`, `Done`)
   - `j` / `k`: select ticket
   - `Enter` / `s`: claim & dispatch default coding agent in a fresh worktree tab
   - `A`: pick a specific coding agent (`claude`, `agy`, `codex`, `opencode`, `gemini`, `copilot`)
   - `f`: jump to the local Herdr agent pane working on the selected ticket
   - `t`: promote a `Triage` issue to `Ready`
   - `a`: approve a `Review`-gated ticket and push to the shared branch
   - `c`: send a cross-agent message/comment to a ticket
   - `n`: create a new Ready ticket

---

## Agent CLI Reference

Installed coding agents receive the `herdr-kanban` skill automatically:

```bash
herdr-kanban board
herdr-kanban list --column ready --unclaimed --json
herdr-kanban claim 42 --json
herdr-kanban progress 42 --note "Parser implemented" --check "[x] Parser" --check "[ ] Tests"
herdr-kanban deposit "Handle empty token header" --parent 42 --file-line "src/auth.py:88"
herdr-kanban ask 41 "Did you rename SessionStore?" --from 42
herdr-kanban inbox --ack
herdr-kanban done 42 --summary "Added parser and unit tests"
```

---

## Architecture & Development

See [`docs/spec.md`](docs/spec.md) and [`docs/adr/`](docs/adr/README.md).

```bash
uv run pytest
uv run ruff check src tests
PYTHONPATH=src uv run lint-imports
```
