# herdr-kanban specification

Version: 0.1.0

`herdr-kanban` is a [Herdr](https://herdr.dev) plugin that lets a team of colleagues combine
their coding agents (Claude Code, Antigravity, Codex, OpenCode, Gemini CLI, Copilot CLI, etc.)
to collaborate concurrently on a **single shared Git branch**, coordinated through **GitHub
Projects v2** and **GitHub Issues** with zero custom backend servers.

---

## 1. Goals & Non-Goals

### Goals

1. **Same-branch multi-colleague concurrency**: Multiple humans across separate machines—each
   running multiple local agents in Herdr—land work onto one shared GitHub branch
   (`origin/<target-branch>`) without stepping on each other's working trees or breaking the
   branch.
2. **Serverless coordination via GitHub**: Use GitHub Projects v2 (columns) + Issues + one
   in-place-edited structured comment per claim for distributed locking, heartbeats, touched-file
   conflict radar, and cross-colleague agent-to-agent messaging.
3. **Hybrid human + agent workflow**:
   - Humans inspect and manage the board from a dedicated Herdr **Tab** (or responsive **Split** /
     **Popup**) and dispatch tickets into ephemeral worktrees + new Herdr agent tabs with a single
     keystroke.
   - Running agents use the `herdr-kanban` CLI (taught via a shared `SKILL.md`) to claim work,
     deposit newly discovered issues, report progress, ask sibling/colleague agents questions, and
     land validated commits.
4. **Scope-creep & prompt-injection safety**:
   - Agent-deposited ("found") issues enter `Triage` by default until promoted to `Ready`.
   - Incoming GitHub comment relays only auto-wake idle local agents when authored by trusted
     collaborators / configured team logins.
5. **Zero runtime Python dependencies**: Python 3.11+ standard library only at runtime,
   shelling out to `git`, `gh`, and `herdr`.

### Non-Goals

- Hosting a bespoke coordination server, database, or WebSocket broker.
- Opening inbound SSH access between colleagues' personal laptops.
- Supporting non-GitHub issue trackers in v0.1 (adapter boundary keeps future trackers possible).

---

## 2. Shared-Branch Git & Worktree Model (ADR-0002)

1. The team shares a target branch on `origin` (e.g., `feat/unified-auth`), configured in
   `.herdr-kanban.toml` (`target_branch`).
2. When ticket `#N` is claimed or dispatched locally:
   - `git fetch origin <target_branch>` updates the remote-tracking ref.
   - An **ephemeral Git worktree** is created at `<worktree_root>/<repo-slug>/issue-<N>` on local
     branch `kanban/issue-<N>` starting at `origin/<target_branch>`.
   - When dispatched via Herdr, a new tab is opened with `--cwd <worktree_path>`, so the coding
     agent's workspace/sandbox root is that isolated worktree.
3. While working, the agent commits normally inside its worktree. Local heartbeats compute
   `touched_files` (`git diff --name-only origin/<target_branch>` plus untracked/modified working
   tree paths) and publish them to the claim metadata so teammates' boards and agents get early
   file-overlap warnings.
4. On `herdr-kanban done <N>`:
   - Verifies a clean working tree and at least one commit ahead of `origin/<target_branch>`.
   - If the ticket or board requires a human `Review` gate (and `--approve` was not passed),
     rebases onto `origin/<target_branch>`, runs `check_command`, moves the card to `Review`, and
     notifies the user in Herdr without pushing yet.
   - Otherwise fetches `origin/<target_branch>`, runs `git rebase origin/<target_branch>`, runs
     configured `check_command` (if any), and fast-forward pushes
     `git push origin HEAD:refs/heads/<target_branch>` (retrying rebase + check up to 3 times on a
     concurrent push race).
   - Marks the claim completed, moves the Project v2 item to `Done`, **closes the GitHub Issue
     immediately**, and removes the ephemeral worktree (immediately if outside cwd, or on
     `pane.closed`).

---

## 3. Kanban Columns & Lifecycle (ADR-0004)

Logical columns mapped to options of the GitHub Project v2 `Status` single-select field:

| Logical Column | Default Project v2 Status Matches | Purpose |
|---|---|---|
| `triage` | `Triage`, `Backlog`, `Inbox`, `Icebox` | Default landing zone for agent-deposited ("found") issues |
| `ready` | `Ready`, `Todo`, `To Do`, `To do` | Approved work ready to be claimed by humans or agents |
| `in_progress` | `In Progress`, `In progress`, `Doing` | Actively claimed with a live lease |
| `blocked` | `Blocked`, `Stuck`, `On Hold` | Waiting on conflict resolution or an unanswered dependency |
| `review` | `Review`, `In Review`, `Needs Review` | Optional human approval gate before pushing to `<target_branch>` |
| `done` | `Done`, `Completed`, `Closed` | Pushed to `origin/<target_branch>` and issue closed |

Custom status names in an existing GitHub Project v2 are mapped explicitly in `.herdr-kanban.toml`
under `[columns]`.

---

## 4. Structured GitHub Metadata Contract v1 (`schema/comment-meta.v1.json`)

Machine-readable metadata is embedded inside standard GitHub Markdown bodies as an HTML comment:

```markdown
Human-readable summary...

<!-- herdr-kanban:v1 {"v":1,"type":"claim",...} -->
```

### 4.1 Claim Lease (`type = "claim"`, ADR-0001)

- Claiming posts **one** claim comment on `#N`, then immediately lists comments on `#N` in
  ascending GitHub comment ID order.
- **Tie-breaker**: The earliest comment with `state == "active"` and unexpired lease (unless
  superseded by an explicit `takeover_of` when stale) owns the ticket. Any losing concurrent
  claimant deletes its own comment and aborts cleanly.
- **Zero comment spam**: Subsequent heartbeats, checklist updates, and `touched_files` updates
  `PATCH` that exact `comment_id` in place.
- **Staleness & Crash Recovery**:
  - Default `lease_ttl_minutes = 30`. When `now > lease_until`, boards display `STALE` and allow an
    explicit `--takeover` (never silent background theft of a colleague's work).
  - When Herdr emits `pane.closed` for a locally claimed pane: if the worktree is clean and has
    zero commits ahead of `origin/<target_branch>`, the plugin automatically releases the claim and
    returns the card to `Ready`; otherwise it preserves the worktree and alerts the user.

### 4.2 Agent-to-Agent Comment Relay (`type = "relay"`, ADR-0003)

- Agents message another ticket/colleague via `herdr-kanban ask <to-issue> "<message>" [--from <my-issue>]`.
- **Same-session / `herdr --machine` fast path**: If `<to-issue>` is active locally (or on a
  configured Herdr SSH machine), the plugin delivers the wakeup directly when the target agent is
  `idle`/`done` (or queues it in the local inbox if `working`), while still posting the durable
  comment on GitHub.
- **Cross-colleague relay**: Each colleague's local `daemon` polls active claims' issue comments:
  - Verifies comment author against `repo_collaborators ∪ config.trusted_authors`.
  - **Trusted author**: queues in local inbox; if the local agent is `idle`/`done`, prompts it via
    `herdr agent prompt`; if busy, triggers a toast notification and surfaces the message via
    `herdr-kanban inbox`.
  - **Untrusted author**: never auto-prompts the agent; marks `trusted = false` for human review.

### 4.3 Deposited Issue Metadata (`type = "deposit"`, ADR-0004)

- Embedded at the bottom of issues created via `herdr-kanban deposit`, recording `parent_issue`,
  `file_line`, `branch`, `commit_sha`, and `deposited_by`.

---

## 5. Configuration Contract (`schema/board-config.v1.json`, ADR-0005)

1. **Shared branch config**: `.herdr-kanban.toml` at the repository root on `<target_branch>`,
   generated via `herdr-kanban init`.
2. **Local repo override**: `~/.config/herdr/plugins/config/herdr-kanban/repos/<owner>__<repo>.toml`
   (allows using `herdr-kanban` in repositories where committing dotfiles is undesirable).
3. **Personal settings**: `~/.config/herdr/plugins/config/herdr-kanban/settings.toml`
   (`default_agent_kind`, `worktree_root`, `poll_seconds`, `machines`).

Because ephemeral worktrees may run before `.herdr-kanban.toml` is committed or from a subdirectory,
config discovery checks both the current worktree root and the main checkout root derived from
`git rev-parse --git-common-dir`.

---

## 6. Surfaces

### 6.1 Herdr TUI (`src/herdr_kanban/tui.py`)

- **Default placement**: Dedicated workspace `tab` (`prefix+m` or action `Kanban: open board tab`).
- **Responsive layout**:
  - Width >= 110 cols: 5-column Kanban grid (`Triage`, `Ready`, `In Progress`, `Blocked / Review`,
    `Done`) above a rich ticket inspector (lease status, agent identity, overlap alerts, touched
    files, body/comments).
  - Width < 110 cols (or `--compact` in `split` placement): horizontal column switcher bar +
    scrollable card list + inspector.
- **Core keys**: `h`/`l` column, `j`/`k` card, `Enter`/`s` dispatch default agent, `A` choose agent
  kind & dispatch, `f` focus local agent pane, `t` promote `Triage -> Ready`, `m` move column,
  `a` approve & push `Review` ticket, `R` release / takeover stale, `n` new ticket, `c` comment/ask,
  `o` open in browser, `r` refresh, `?` help, `q` quit.

### 6.2 CLI & Shared Agent Skill (`bin/herdr-kanban`, `skills/herdr-kanban/SKILL.md`)

All agent commands support `--json` for deterministic machine consumption:
`init`, `board`, `list`, `show`, `claim`, `dispatch`, `release`, `deposit`, `triage`, `progress`,
`comment`, `ask`, `inbox`, `done`, `open`, `tui`, `daemon`, `event`, `doctor`, `install-skill`,
`setup-keys`.

---

## 7. Module Boundaries (`pyproject.toml` `import-linter`)

1. `herdr_kanban.model` and `herdr_kanban.config` are pure data/config layers and MUST NOT import
   `github`, `git_ops`, `herdr`, `engine`, `daemon`, `tui`, or `cli`.
2. Adapter modules (`github`, `git_ops`, `herdr`) are mutually isolated:
   - `github` knows nothing about `git_ops`, `herdr`, `tui`, or `daemon`.
   - `git_ops` knows nothing about `github`, `herdr`, `tui`, or `daemon`.
   - `herdr` knows nothing about `github`, `git_ops`, `tui`, or `daemon`.
3. `terminal` knows nothing about `github`, `git_ops`, `engine`, or `daemon`.
