# herdr-kanban

Collaborate with your colleagues—and combine all of your coding agents—on **one shared GitHub
branch**, coordinated through **GitHub Projects v2** and **GitHub Issues** inside both the
**[Herdr desktop app](https://herdr.dev) (`herdr-app` + `herdr-core` / `hc`, as a native GPUI
board mod)** and the **`herdr` TUI**.

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

### In the Herdr desktop app (`herdr-app` + `herdr-core`)

Paste this prompt to any coding agent running in your Herdr window:

```text
Read ~/.config/herdr-app/AGENTS.md, then add this mod: https://github.com/jellespijker/herdr-kanban
```

Or install from a terminal:

```bash
git clone https://github.com/jellespijker/herdr-kanban.git ~/.local/share/herdr-kanban
cd ~/.local/share/herdr-kanban
./bin/herdr-kanban install-skill
./bin/herdr-kanban install-app-mod
./bin/herdr-kanban setup-keys
herdr-app check ~/.config/herdr-app/mods/herdr-kanban
```

- Installed as `mods/herdr-kanban` (`~/.config/herdr-app/mods/herdr-kanban`) so it coexists cleanly alongside the built-in agent-status `mods/kanban` layout.
- Open the native GPUI board with `Ctrl+Shift+M`, `herdr-kanban open --mod`, or `herdr-app do mod.open herdr-kanban`.
- Supports remote `hc` spaces across connected peer machines automatically (`{ cwd, on }`).

### In the `herdr` TUI (0.9.3+)

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
bin/herdr-kanban install-skill   # links SKILL.md + ~/.local/bin/herdr-kanban + Herdr-App GPUI mod
bin/herdr-kanban setup-keys      # binds prefix+m (TUI) and ctrl-shift-m (Herdr-App) to open the board
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
   - `w`: toggle the **What's New** colleague & agent landed work briefing (`s` to synthesize)

---

## Multi-Repository ("Umbrella") Workspaces

GitHub Projects v2 naturally span multiple repositories (`firmware`, `cloud`, `software`, etc.).
To coordinate across multiple repos on a shared feature branch:

1. Create a parent directory (either a plain folder or a small Git meta-repo) and clone your repos
   inside it:
   ```bash
   mkdir acme-platform && cd acme-platform
   gh repo clone acme/firmware
   gh repo clone acme/cloud
   gh repo clone acme/software
   herdr-kanban init --interactive   # auto-discovers firmware, cloud, software into [repos.<alias>]
   ```
2. Teammates who pull or copy `.herdr-kanban.toml` into their umbrella folder can clone any missing
   repos and align every repo onto the shared `target_branch` in one step:
   ```bash
   herdr-kanban sync-repos --create-branches
   ```
3. Tickets are displayed and addressed as `<alias>#<number>` (`firmware#10`, `cloud#18`), so issue
   `#10` in `firmware` and issue `#10` in `cloud` never collide.
4. When an agent is dispatched into an ephemeral worktree for `cloud#18`, `herdr-kanban`
   automatically symlinks `.siblings/firmware` and `.siblings/software` inside that worktree (and
   excludes `/.siblings/` via Git `info/exclude`), giving sandboxed coding agents read access to
   sibling repos while keeping `git status` clean.

---

## Agent CLI Reference

Installed coding agents receive the `herdr-kanban` skill automatically:

```bash
herdr-kanban board
herdr-kanban whats-new --generate
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
