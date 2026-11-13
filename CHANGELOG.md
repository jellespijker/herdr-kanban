# Changelog

## 0.4.2 (2026-11-13)

- **Deterministic Non-Overlapping Pill Layout (`renderChunkedPillRows` & Card Header Budget)**:
  - Keep card top rows strictly to `issue ref + priority` on the left and a single status/repo badge on the right (`OVERLAP`, `STALE`, `Unblocks #N`, `repo`, or `Jira key`), moving secondary repo pills into the character-budgeted facet row below the title so left and right card header pills never collide.
  - Replace GPUI `.flex_wrap()` pill containers in the Inspector (`Dependency Graph`, `Ticket Metadata`) and What's New (`Colleagues & Coding Agents`, `Landed by Repository`) with explicit `renderChunkedPillRows(...)` (`v_flex` of character-budgeted `h_flex` rows) so multi-line pill lists never overlap vertically or horizontally.

## 0.4.1 (2026-11-13)

- **Agent-Colored Active Ticket Cards (`resolveTicketAgentVisual`)**:
  - Color active claimed cards by the working coding agent (`claude` `#e07a5f` + `icons/claude.svg`, `agy` `#4c8bf5` + `icons/antigravity.svg`, `codex` `#7c6ff6` + `icons/codex.svg`, `opencode` `#10b981` + `icons/opencode.svg`, `gemini` `#06b6d4` + `icons/gemini.svg`, `copilot` `#ec4899` + `icons/copilot.svg`) with a 4px left accent stripe, tinted card surface & border, spinning `spinner.svg` while working, and distinct worker colors (`AGENT_WORKER_PALETTE`, `agy #1`, `agy #2`) when multiple agents of the same harness work in parallel.
- **Dependency-Blocked Card Lock Callout Banner (`isDepBlocked` + `lock.svg`)**:
  - Give cards with unmet upstream dependencies (`unmet_deps`) a clear visual treatment beyond a pill: 4px left blocked bar, dashed border (`border_dashed()`), recessed tinted background, dimmed title text, and a full-width **`BLOCKED BY #71 · Wave N`** callout banner (`lock.svg`) with clickable blocker chips that jump directly to the blocking ticket.
  - Show a green `Unblocks #69` pill on unblocked upstream tickets (`blocks.length > 0`).
- **Non-Overlapping Bounded Pill Layout & Multi-Space Fallback**:
  - Enforce bounded truncation (`max_w`, `min_w(0)`, `overflow_hidden()`) across card headers, facet rows, agent footers, subbars, and inspector headers so pills never overlap at any split-pane width.
  - Automatically fall back across sibling spaces on the same Herdr machine when opening the Kanban overlay from a non-Git scratch directory.

## 0.4.0 (2026-11-13)

- **What's New — Landed Work & Colleague Digest (`herdr-kanban whats-new` + `w` view)**:
  - Add `herdr-kanban whats-new` (`--generate`, `--write-summary`, `--prompt`, `--refresh`, `--json`) to synthesize and persist executive briefings of landed work across repositories and recent Git commits.
  - Add a dedicated **What's New** view (`w` shortcut or header switcher) in the Herdr-App GPUI mod showing a rich Markdown Executive Briefing alongside Colleagues & Coding Agents velocity cards, Landed by Repository filter pills, and a Recently Landed Tickets feed.
- **Native Markdown Rendering (`TextView.markdown`)**:
  - Render ticket descriptions, live claim progress notes, and What's New briefings via `gpui-base` `TextView.markdown(...)` with selectable text and auto-linkified `#issue` / `repo#issue` references (`kanban://issue/...`) that jump directly to the referenced ticket on click.
- **Impeccable GPUI Mod Redesign (`mods/herdr-kanban/main.js`)**:
  - **Adaptive Column Layout (`Compact Empty: On`)**: Compress empty columns (`0` tickets) to `104px` while expanding active columns (`276px–360px`) so `In Progress` and `Done` stay visible simultaneously.
  - **Multi-Line Wrapped Card Titles & Semantic Chips**: Parse `[TRACKER-KEY]` and `[scope]` prefixes into compact header badges, wrap full human titles across lines (`line_height(1.38)`), strip raw `domain:`/`type:`/`release:` prefixes into color-coded chips, and add visual checklist progress bars (`28px × 4px`).
  - **Split 2-Column Ticket Inspector**: Left column for claim status, visual checklist, dependency graph, and touched files; right column for full Markdown description; plus `Expand` / `Compact` (`Shift+D`) height toggle and custom SVG icon assets (`kanban.svg`, `refresh.svg`, `sparkles.svg`).

## 0.3.0 (2026-11-13)

- **Native Herdr-App GPUI Kanban Mod (`mods/herdr-kanban/`)**:
  - Docked split-panel Kanban board (`Ctrl+Shift+M`) with 2D drag/wheel panning, sticky column headers, and remote `hc` peer execution (`--on <peer>`).

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
