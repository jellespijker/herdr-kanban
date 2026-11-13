# Herdr Kanban

Multi-colleague, multi-agent GitHub Projects v2 Kanban board (`herdr-kanban`) for Herdr-App. Coordinates isolated Git worktrees, optimistic claim leases, touched-file overlap alerts, dependency waves, and one-key agent dispatch across local and remote `hc` machines.

![Herdr Kanban](preview.png)

## What you see

- **Optimized two-row header**:
  - **Row 1**: Project title, remote peer badge (`@peer-host` when viewing a remote space), shared target branch pill (`origin/main`), total/filtered ticket counter, live filter box with clear (`×`) button, `Center` pan-reset button (when panned), `+ Deposit`, `Detail` toggle, and `↻` refresh.
  - **Row 2**: Compact **Column Quick-Jump pills** (`Tri`, `Rdy`, `Prog`, `Blk`, `Rev`, `Done`) with live ticket counts and status dots, paired with a drag/wheel-pannable **Repo Filter strip** (`all repos` plus active-first repo pills showing ticket counts).
- **2D Pannable Kanban canvas**: `Triage`, `Ready`, `In Progress`, `Blocked`, `Review`, and `Done` columns with sticky column headers, a horizontal viewport indicator bar, click-and-drag 2D panning, and trackpad/mouse-wheel panning.
- **Rich ticket cards**: Issue ref (`#42` or `repo#42`), priority badge (`P0`–`P3`), dependency wave (`wave:0`) or open blocker badge (`deps:#12`), facet label chips, active claim holder (`@owner:agent`) with live Herdr agent state dot (`working`, `blocked`, `done`, `idle`), checklist progress (`[2/4]`), touched-file count (`3f`), `STALE` lease warning, and red `OVERLAP!` alert when two active claims touch the same file.
- **Docked bottom Ticket Detail drawer**: Full issue description, dependency graph, checklist items, touched-file list, overlap warnings, and action toolbar (`Dispatch` with inline agent harness selector `claude | agy | codex | opencode`, `Focus`, `-> Ready`, `Approve / Land`, `Move / Triage`, `Ask Agent / Comment`, `Release`).
- **Inline action bars**: Deposit new ticket bar (title, description, priority, target repo, Triage/Ready toggle), Triage/Move bar, and cross-agent Ask/Comment bar.

## How it behaves

- Opens as a docked split panel beside your active terminal (`Ctrl+Shift+M` toggles open/close via `pane.split` / `pane.close`).
- Resolves `{ cwd, on }` from `props()` or the active Herdr space (`spaces()`, `panes()`, `focused()`), scanning candidate pane `cwd`s in the space to find `.herdr-kanban.toml` and passing `{ cwd, on }` to every `exec("herdr-kanban", [...], { cwd, on })` call so remote spaces execute `herdr-kanban` on the space's host machine.
- **2D Panning**: Left-click drag or middle-click drag anywhere on the board (or scroll with a trackpad / mouse wheel / `Shift + wheel`) to pan across columns and cards (`panX`, `panY`). Double-clicking a column header, clicking `Center`, or pressing `0` resets pan to `(0, 0)`.
- **Claim & Dispatch (`s`)** runs `herdr-kanban claim <ref> --kind <agent> --json` to acquire the GitHub comment lease and create the isolated worktree, then calls `run("tab.new", { name, space, cwd: claim.worktree_path, argv, task: prompt })` to launch the coding agent directly in a Herdr tab.
- **Focus Agent (`f`)** matches the ticket's active terminal/pane/agent via `agents()`, `terminals()`, and `panes()` and focuses it with `run("pane.focus", ...)` and `run("tab.select", ...)`.
- **Keyboard navigation**: `h`/`l` (columns, with auto-pan), `j`/`k` (cards, with auto-pan), `Shift+h/j/k/l` (manual pan), `1`–`6` (jump & pan to column), `0` (reset pan), `d`/`Enter`/`Space` (toggle detail drawer), `s` (claim & dispatch), `f` (focus agent), `t` (promote Triage -> Ready or open triage bar), `a` (approve & land Review ticket), `n` (deposit ticket), `c` (ask/comment), `r` (refresh), `Escape`/`q` (close modal, drawer, or pane).

## Built from

- `gpui-kit`: `View`, `div`, `svg`
- `gpui-base`: `v_flex`, `h_flex`, `Button`, `Input`, `InputState`
- `herdr`: `spaces`, `panes`, `agents`, `terminals`, `focused`, `exec`, `hc`, `run`, `theme`, `closeOverlay`, `notify`, `nextCommand`, `props`, `knownAgents`

## Watch out

- Never construct `InputState.new(...)` or `cx.focus_handle()` inside `render()` — all inputs (`searchInput`, `depositTitleInput`, `depositBodyInput`, `askInput`) and `this.handle` are created once in `init(cx)`.
- Always pass `{ cwd, on }` to `exec("herdr-kanban", ...)` so viewing a space hosted on a peer machine (e.g. viewing a remote workstation space from a laptop peer) runs `herdr-kanban` on the machine that owns the repository checkout and worktrees.
- Never hold `cx` across `await`; use the `cx` provided by `cx.spawn(async (cx) => ...)` and call `cx.notify()` after state updates.

## Done when

- `herdr-app check ~/.config/herdr-app/mods/herdr-kanban` passes with 0 errors and 0 lint warnings both locally and on connected `hc` peers.
- All 6 columns, badges, overlap warnings, inspector drawer, keyboard navigation, and interactive actions (`claim & dispatch`, `focus`, `triage`, `approve`, `ask`, `deposit`) work seamlessly.
