"""Interactive multi-column and compact Kanban TUI for Herdr panes."""

from __future__ import annotations

import os
import select
import signal
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import config, daemon, state, terminal
from .engine import (
    ClaimStaleError,
    ConfigNotFoundError,
    KanbanEngine,
    KanbanError,
    RepoContext,
)
from .model import Column, Ticket, strip_meta

UI_GROUPS: tuple[tuple[str, tuple[Column, ...]], ...] = (
    ("Triage", (Column.TRIAGE,)),
    ("Ready", (Column.READY,)),
    ("In Progress", (Column.IN_PROGRESS,)),
    ("Blocked / Review", (Column.BLOCKED, Column.REVIEW)),
    ("Done", (Column.DONE,)),
)

MOVE_TARGETS: tuple[tuple[str, Column], ...] = (
    ("1:Triage", Column.TRIAGE),
    ("2:Ready", Column.READY),
    ("3:In Progress", Column.IN_PROGRESS),
    ("4:Blocked", Column.BLOCKED),
    ("5:Review", Column.REVIEW),
    ("6:Done", Column.DONE),
)


@dataclass(slots=True)
class PromptState:
    mode: str  # pick_repo | new_ticket | comment | pick_agent | pick_column | confirm_takeover
    label: str
    buffer: str = ""
    ticket_ref: str = ""
    repo_alias: str = ""


class BoardTUI:
    def __init__(
        self,
        engine: KanbanEngine,
        *,
        cwd: Path | None = None,
        compact: bool = False,
        in_fd: int = 0,
        out_fd: int = 1,
    ) -> None:
        self.engine = engine
        self.explicit_cwd = cwd
        self.force_compact = compact
        self.in_fd = in_fd
        self.out_fd = out_fd

        self.ctx: RepoContext | None = None
        self.tickets: list[Ticket] = []
        self.local_claims: dict[tuple[str, int], state.LocalClaim] = {}
        self.inbox_counts: dict[tuple[str, int], int] = {}

        self.col_idx = 1  # Default focus on Ready
        self.row_by_col: list[int] = [0 for _ in UI_GROUPS]
        self.scroll_by_col: list[int] = [0 for _ in UI_GROUPS]

        self.syncing = False
        self.last_sync = 0.0
        self.status_msg = ""
        self.error_msg = ""
        self.show_help = False
        self.prompt: PromptState | None = None

        self._lock = threading.Lock()
        self._wake_r, self._wake_w = os.pipe()
        self._running = True
        self._last_heartbeats: dict[str, float] = {}

    def close_pipes(self) -> None:
        for fd in (self._wake_r, self._wake_w):
            try:
                os.close(fd)
            except OSError:
                pass

    def _tref(self, ticket: Ticket) -> str:
        return ticket.ref(bool(self.ctx and self.ctx.is_multi_repo))

    def _cli_ref(self, ticket: Ticket) -> str:
        if self.ctx and self.ctx.is_multi_repo and ticket.repo_alias:
            return f"{ticket.repo_alias}#{ticket.number}"
        return str(ticket.number)

    def _state_key(self, ticket: Ticket) -> tuple[str, int]:
        owner = ticket.repo_owner or (self.ctx.repo_owner if self.ctx else "")
        repo = ticket.repo_name or (self.ctx.repo_name if self.ctx else "")
        return config.repo_slug(owner, repo), ticket.number

    def load_initial_state(self) -> None:
        try:
            self.ctx = self.engine.resolve_context(self.explicit_cwd)
        except ConfigNotFoundError as exc:
            self.error_msg = str(exc)
            return
        except KanbanError as exc:
            self.error_msg = str(exc)
            return

        self._reload_local_overlays()
        cached = self.engine.load_cached_tickets(self.ctx)
        if cached is not None:
            self.last_sync, self.tickets = cached

    def _reload_local_overlays(self) -> None:
        if not self.ctx:
            return
        claims_map: dict[tuple[str, int], state.LocalClaim] = {}
        counts: dict[tuple[str, int], int] = {}
        for slug in self.ctx.repo_slugs():
            for claim in state.list_local_claims(slug):
                claims_map[(claim.repo_slug, claim.issue_number)] = claim
            for item in state.list_inbox(slug, include_acked=False):
                key = (slug, item.issue_number)
                counts[key] = counts.get(key, 0) + 1
        self.local_claims = claims_map
        self.inbox_counts = counts

    def group_tickets(self, idx: int) -> list[Ticket]:
        _, cols = UI_GROUPS[idx]
        filtered = [t for t in self.tickets if t.column in cols]
        if self.ctx:
            return self.engine.sort_tickets_for_display(self.ctx, filtered)
        return filtered

    def selected_ticket(self) -> Ticket | None:
        group = self.group_tickets(self.col_idx)
        if not group:
            return None
        row = max(0, min(self.row_by_col[self.col_idx], len(group) - 1))
        self.row_by_col[self.col_idx] = row
        return group[row]

    def _wake(self) -> None:
        try:
            os.write(self._wake_w, b"x")
        except OSError:
            pass

    def trigger_refresh(self, message: str = "") -> None:
        with self._lock:
            if self.syncing or self.ctx is None:
                return
            self.syncing = True
            if message:
                self.status_msg = message

        def worker() -> None:
            assert self.ctx is not None
            err = ""
            tickets: list[Ticket] | None = None
            try:
                _, tickets = self.engine.refresh_board(self.ctx)
                self.engine.sync_claim_heartbeats_and_relays(last_heartbeat_by_key=self._last_heartbeats)
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
            with self._lock:
                self.syncing = False
                if tickets is not None:
                    self.tickets = tickets
                    self.last_sync = time.time()
                    self.error_msg = ""
                if err:
                    self.error_msg = err
                self._reload_local_overlays()
            self._wake()

        threading.Thread(target=worker, daemon=True).start()

    def _run_background_action(self, description: str, fn) -> None:
        with self._lock:
            self.syncing = True
            self.status_msg = description
            self.error_msg = ""
        self._wake()

        def worker() -> None:
            msg = ""
            err = ""
            need_takeover_ref = ""
            need_takeover_disp = ""
            try:
                msg = fn() or ""
                if self.ctx:
                    _, refreshed = self.engine.refresh_board(self.ctx)
                    with self._lock:
                        self.tickets = refreshed
                        self.last_sync = time.time()
            except ClaimStaleError as exc:
                err = str(exc)
                cur = self.selected_ticket()
                if cur:
                    need_takeover_ref = self._cli_ref(cur)
                    need_takeover_disp = self._tref(cur)
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
            with self._lock:
                self.syncing = False
                if msg:
                    self.status_msg = msg
                if err:
                    self.error_msg = err
                if need_takeover_ref:
                    self.prompt = PromptState(
                        mode="confirm_takeover",
                        label=f"Claim on {need_takeover_disp} is STALE. Reclaim with takeover? [y/N]: ",
                        ticket_ref=need_takeover_ref,
                    )
                self._reload_local_overlays()
            self._wake()

        threading.Thread(target=worker, daemon=True).start()

    # --- Rendering ---

    def render_frame(self, cols: int, rows: int) -> str:
        with self._lock:
            lines: list[str] = [self._render_header(cols)]
            if self.ctx is None:
                lines.extend(self._render_unconfigured(cols, rows - 2))
            elif self.show_help:
                lines.extend(self._render_help(cols, rows - 2))
            else:
                compact = self.force_compact or cols < 110
                body_rows = max(6, rows - 2)
                inspector_rows = max(6, min(11, body_rows // 3))
                board_rows = max(4, body_rows - inspector_rows)
                if compact:
                    lines.extend(self._render_compact_board(cols, board_rows))
                else:
                    lines.extend(self._render_wide_board(cols, board_rows))
                lines.extend(self._render_inspector(cols, inspector_rows))
            lines.append(self._render_footer(cols))
            padded = [terminal.pad_ansi(line, cols) for line in lines[:rows]]
            while len(padded) < rows:
                padded.append(" " * cols)
            return "\r\n".join(padded)

    def _render_header(self, cols: int) -> str:
        if self.ctx is None:
            left = terminal.bold(" herdr-kanban ") + terminal.yellow(" (not initialized)")
            return terminal.pad_ansi(left, cols)

        repo_lbl = self.ctx.display_name
        branch_lbl = f"{self.ctx.board.remote}/{self.ctx.board.target_branch}"
        proj_lbl = f"Project #{self.ctx.board.project_number}"
        local_n = len(self.local_claims)
        overlaps_n = sum(1 for t in self.tickets if t.overlaps)
        sync_state = (
            terminal.cyan("⟳ syncing")
            if self.syncing
            else (
                terminal.dim(f"synced {int(time.time() - self.last_sync)}s ago")
                if self.last_sync
                else terminal.dim("not synced")
            )
        )

        parts = [
            terminal.bold(" Kanban "),
            terminal.cyan(repo_lbl),
            f"branch:{terminal.green(branch_lbl)}",
            terminal.dim(proj_lbl),
            f"agents:{terminal.bold(str(local_n))}",
        ]
        if overlaps_n:
            parts.append(terminal.yellow(f"overlaps:{overlaps_n}"))
        parts.append(sync_state)
        joined = " │ ".join(parts)
        return terminal.pad_ansi(joined, cols)

    def _render_wide_board(self, cols: int, height: int) -> list[str]:
        num_cols = len(UI_GROUPS)
        gap = 1
        col_w = max(18, (cols - gap * (num_cols - 1)) // num_cols)
        extra = cols - (col_w * num_cols + gap * (num_cols - 1))

        col_blocks: list[list[str]] = []
        for g_idx, (title, _) in enumerate(UI_GROUPS):
            w = col_w + (extra if g_idx == num_cols - 1 else 0)
            col_blocks.append(self._render_column_box(g_idx, title, w, height))

        rows_out: list[str] = []
        for r in range(height):
            row_parts = [col_blocks[c][r] for c in range(num_cols)]
            rows_out.append((" " * gap).join(row_parts))
        return rows_out

    def _render_compact_board(self, cols: int, height: int) -> list[str]:
        tabs: list[str] = []
        for idx, (title, _) in enumerate(UI_GROUPS):
            cnt = len(self.group_tickets(idx))
            label = f" {idx + 1}:{title} ({cnt}) "
            tabs.append(
                terminal.reverse(terminal.bold(label)) if idx == self.col_idx else terminal.dim(label)
            )
        tab_bar = terminal.pad_ansi(" ".join(tabs), cols)
        box = self._render_column_box(self.col_idx, UI_GROUPS[self.col_idx][0], cols, max(3, height - 1))
        return [tab_bar, *box]

    def _render_column_box(self, g_idx: int, title: str, width: int, height: int) -> list[str]:
        focused = g_idx == self.col_idx
        cards = self.group_tickets(g_idx)
        inner_w = max(8, width - 2)
        hdr_txt = terminal.truncate(f" {title} ({len(cards)}) ", inner_w)
        border_color = terminal.cyan if focused else terminal.dim
        top = (
            border_color("┌")
            + (terminal.bold(terminal.cyan(hdr_txt)) if focused else hdr_txt)
            + border_color("─" * max(0, inner_w - terminal.visible_len(hdr_txt)) + "┐")
        )
        bottom = border_color("└" + ("─" * inner_w) + "┘")

        body_h = max(1, height - 2)
        card_slot_h = 2 if body_h >= 6 else 1
        visible_slots = max(1, body_h // card_slot_h)

        sel_row = max(0, min(self.row_by_col[g_idx], max(0, len(cards) - 1)))
        self.row_by_col[g_idx] = sel_row
        scroll = self.scroll_by_col[g_idx]
        if sel_row < scroll:
            scroll = sel_row
        elif sel_row >= scroll + visible_slots:
            scroll = sel_row - visible_slots + 1
        scroll = max(0, min(scroll, max(0, len(cards) - visible_slots)))
        self.scroll_by_col[g_idx] = scroll

        content_lines: list[str] = []
        if not cards:
            empty = terminal.dim(terminal.truncate(" (empty)", inner_w))
            content_lines.append(terminal.pad_ansi(empty, inner_w))
        else:
            for idx in range(scroll, min(len(cards), scroll + visible_slots)):
                ticket = cards[idx]
                is_sel = focused and idx == sel_row
                rendered = self._render_card_lines(ticket, inner_w, card_slot_h, selected=is_sel)
                content_lines.extend(rendered)

        while len(content_lines) < body_h:
            content_lines.append(" " * inner_w)

        framed = [top]
        for line in content_lines[:body_h]:
            framed.append(border_color("│") + terminal.pad_ansi(line, inner_w) + border_color("│"))
        framed.append(bottom)
        return framed

    def _render_card_lines(
        self,
        ticket: Ticket,
        width: int,
        slot_h: int,
        *,
        selected: bool,
    ) -> list[str]:
        prefix = f"{self._tref(ticket)} "
        title_space = max(4, width - len(prefix))
        line1_plain = prefix + terminal.truncate(ticket.title, title_space)
        if selected:
            line1 = terminal.reverse(terminal.bold(line1_plain.ljust(width)))
        else:
            line1 = terminal.bold(prefix) + terminal.truncate(ticket.title, title_space)

        if slot_h == 1:
            return [terminal.pad_ansi(line1, width)]

        skey = self._state_key(ticket)
        badges: list[str] = []
        if ticket.priority:
            badges.append(terminal.yellow(ticket.priority))
        if ticket.target_repo and ticket.target_repo != ticket.repo_alias:
            badges.append(terminal.cyan(f"[{ticket.target_repo}]"))
        if ticket.unmet_deps:
            deps_str = ",".join(f"#{d}" for d in ticket.unmet_deps[:2])
            badges.append(terminal.red(f"⏳{deps_str}"))
        if skey in self.local_claims:
            lc = self.local_claims[skey]
            badges.append(terminal.green(f"●local:{lc.agent_kind}"))
        elif ticket.claim and ticket.claim.state in ("active", "review_ready"):
            badges.append(terminal.cyan(f"@{ticket.claim.owner}·{ticket.claim.agent_kind}"))
        elif ticket.assignees:
            badges.append(terminal.dim(f"@{ticket.assignees[0]}"))

        if ticket.stale:
            badges.append(terminal.red("STALE"))
        if ticket.column == Column.REVIEW or (ticket.claim and ticket.claim.state == "review_ready"):
            badges.append(terminal.magenta("REVIEW"))
        elif ticket.column == Column.BLOCKED:
            badges.append(terminal.red("BLOCKED"))
        if ticket.overlaps:
            badges.append(terminal.yellow("OVERLAP!"))
        unread = self.inbox_counts.get(skey, 0)
        if unread:
            badges.append(terminal.magenta(f"✉{unread}"))
        if ticket.deposit and ticket.deposit.parent_issue:
            parent_ref = (
                f"{ticket.deposit.parent_repo}#{ticket.deposit.parent_issue}"
                if ticket.deposit.parent_repo
                else f"#{ticket.deposit.parent_issue}"
            )
            badges.append(terminal.dim(f"←{parent_ref}"))

        meta_str = " ".join(badges) if badges else terminal.dim("unclaimed")
        if terminal.visible_len(meta_str) > width - 2:
            plain_badges = []
            if skey in self.local_claims:
                plain_badges.append("local")
            elif ticket.claim:
                plain_badges.append(f"@{ticket.claim.owner}")
            if ticket.stale:
                plain_badges.append("STALE")
            if ticket.overlaps:
                plain_badges.append("OVLP")
            meta_str = terminal.dim(terminal.truncate(" ".join(plain_badges), width - 2))

        line2 = "  " + meta_str
        return [terminal.pad_ansi(line1, width), terminal.pad_ansi(line2, width)]

    def _render_inspector(self, cols: int, height: int) -> list[str]:
        inner_w = max(10, cols - 2)
        ticket = self.selected_ticket()
        sep = terminal.dim("─" * cols)
        if ticket is None:
            msg = terminal.dim(
                " Select a ticket (`h`/`l` columns, `j`/`k` cards) or press `n` to create one."
            )
            out = [sep, msg]
            while len(out) < height:
                out.append("")
            return [terminal.pad_ansi(line, cols) for line in out[:height]]

        tref = self._tref(ticket)
        skey = self._state_key(ticket)
        short_title = terminal.truncate(ticket.title, inner_w - 28)
        hdr = (
            f" {terminal.bold(tref)} {terminal.bold(short_title)} {terminal.dim(f'[{ticket.column.value}]')}"
        )
        lines = [sep, hdr]

        meta_parts: list[str] = []
        if ticket.priority:
            meta_parts.append(f"Prio: {terminal.yellow(ticket.priority)}")
        if ticket.milestone:
            meta_parts.append(f"Milestone: {terminal.cyan(ticket.milestone)}")
        if ticket.target_repo:
            meta_parts.append(f"Target: {terminal.green(ticket.target_repo)}")
        elif self.ctx and self.ctx.is_multi_repo and ticket.repo_owner and ticket.repo_name:
            meta_parts.append(terminal.dim(f"{ticket.repo_owner}/{ticket.repo_name}"))
        if ticket.depends_on:
            unmet_set = set(ticket.unmet_deps)
            dep_str = ", ".join(
                terminal.red(f"#{d}") if d in unmet_set else terminal.green(f"#{d}")
                for d in ticket.depends_on
            )
            meta_parts.append(f"Deps: {dep_str} (wave {ticket.wave})")
        if ticket.blocks:
            meta_parts.append("Blocks: " + ", ".join(f"#{b}" for b in ticket.blocks))
        if ticket.claim and ticket.claim.state in ("active", "review_ready"):
            holder = f"@{ticket.claim.owner} ({ticket.claim.agent_kind})"
            if skey in self.local_claims and self.local_claims[skey].pane_id:
                holder += f" pane:{self.local_claims[skey].pane_id}"
            meta_parts.append(f"Claim: {terminal.cyan(holder)}")
            if ticket.stale:
                meta_parts.append(terminal.red(f"STALE (expired {ticket.claim.lease_until})"))
            else:
                meta_parts.append(terminal.dim(f"lease:{ticket.claim.lease_until}"))
            if ticket.claim.note:
                meta_parts.append(f"Note: {terminal.truncate(ticket.claim.note, 42)}")
        elif ticket.deposit:
            loc = f" at {ticket.deposit.file_line}" if ticket.deposit.file_line else ""
            parent = ""
            if ticket.deposit.parent_issue:
                pref = (
                    f"{ticket.deposit.parent_repo}#{ticket.deposit.parent_issue}"
                    if ticket.deposit.parent_repo
                    else f"#{ticket.deposit.parent_issue}"
                )
                parent = f" from {pref}"
            meta_parts.append(terminal.dim(f"Deposited by @{ticket.deposit.deposited_by}{parent}{loc}"))
        if ticket.labels:
            meta_parts.append("Labels: " + ", ".join(ticket.labels[:4]))
        if meta_parts:
            lines.append(" " + " │ ".join(meta_parts))

        if ticket.claim and ticket.claim.touched_files:
            files_str = ", ".join(ticket.claim.touched_files[:6])
            lines.append(" " + terminal.dim(f"Touched: {terminal.truncate(files_str, inner_w - 10)}"))

        if ticket.overlaps:
            ov = ticket.overlaps[0]
            ov_ref = (
                f"{ticket.repo_alias}#{ov.other_issue}"
                if (self.ctx and self.ctx.is_multi_repo and ticket.repo_alias)
                else f"#{ov.other_issue}"
            )
            warn = (
                f" ⚠ File overlap with {ov_ref} (@{ov.other_owner}): "
                f"{', '.join(ov.files[:4])} — press `c` to message"
            )
            lines.append(terminal.yellow(terminal.truncate(warn, cols)))

        remaining = max(0, height - len(lines))
        if remaining > 0:
            clean_body = strip_meta(ticket.body) or "(No description)"
            for wline in terminal.wrap_lines(clean_body, inner_w, remaining):
                lines.append(" " + terminal.dim(wline))

        while len(lines) < height:
            lines.append("")
        return [terminal.pad_ansi(line, cols) for line in lines[:height]]

    def _render_footer(self, cols: int) -> str:
        if self.prompt is not None:
            prompt_txt = terminal.bold(terminal.cyan(self.prompt.label)) + self.prompt.buffer + "█"
            return terminal.pad_ansi(" " + terminal.truncate(prompt_txt, cols - 2), cols)

        if self.error_msg:
            err = terminal.red(" ✗ " + terminal.truncate(self.error_msg, cols - 18)) + terminal.dim(
                "  [?] help"
            )
            return terminal.pad_ansi(err, cols)

        keys_bar = (
            " Enter/s:dispatch  A:agent  f:focus  t:triage→ready  a:approve-push  "
            "c:ask/comment  n:new  m:move  R:release  o:web  r:sync  ?:help  q:quit"
        )
        if self.status_msg:
            msg = terminal.green(" ✓ " + terminal.truncate(self.status_msg, max(20, cols // 2)))
            rest_w = max(0, cols - terminal.visible_len(msg) - 2)
            return terminal.pad_ansi(msg + "  " + terminal.dim(terminal.truncate(keys_bar, rest_w)), cols)

        return terminal.pad_ansi(terminal.dim(terminal.truncate(keys_bar, cols)), cols)

    def _render_unconfigured(self, cols: int, height: int) -> list[str]:
        msg_lines = [
            "",
            terminal.bold("  No `.herdr-kanban.toml` found for this workspace."),
            "",
            "  Connect this repo or multi-repo umbrella directory to a GitHub Project v2:",
            f"    • Run {terminal.cyan('herdr-kanban init --interactive')} in your terminal, or",
            f"    • Run Herdr action {terminal.cyan('Kanban: connect branch to GitHub Project')}",
            "",
            f"  {terminal.dim(self.error_msg)}" if self.error_msg else "",
            "",
            "  Press `q` to close.",
        ]
        while len(msg_lines) < height:
            msg_lines.append("")
        return msg_lines[:height]

    def _render_help(self, cols: int, height: int) -> list[str]:
        help_rows = [
            "",
            terminal.bold("  herdr-kanban Keybindings"),
            "  ─────────────────────────────────────────────────────────────",
            "  h / l  or  ← / →     Switch Kanban column (or `1`-`5`)",
            "  j / k  or  ↑ / ↓     Select ticket card (mouse click & wheel supported)",
            "  Enter  or  s         Claim & dispatch default coding agent in isolated worktree tab",
            "  A                    Choose agent kind (`claude`, `agy`, `codex`, `opencode`, ...) & dispatch",
            "  f                    Focus local Herdr pane of the agent working on this ticket",
            "  t                    Promote `Triage` issue → `Ready`",
            "  m                    Move selected ticket to any column (`1`-`6`)",
            "  a                    Approve human `Review` gate & push worktree to shared branch",
            "  R                    Release active claim (or reclaim a `STALE` teammate lease)",
            "  n                    Create a new Ready ticket on the board",
            "  c                    Send a cross-agent relay question / comment to selected ticket",
            "  o                    Open selected GitHub Issue in browser",
            "  r                    Refresh board & sync comment relays from GitHub now",
            "  ? / Esc              Close help overlay",
            "  q                    Quit Kanban board",
        ]
        while len(help_rows) < height:
            help_rows.append("")
        return [terminal.truncate(line, cols) for line in help_rows[:height]]

    # --- Event Handling ---

    def handle_event(self, ev: terminal.InputEvent, cols: int, rows: int) -> bool:
        """Process one input event. Returns False when the TUI should exit."""
        if self.prompt is not None:
            self._handle_prompt_event(ev)
            return True

        if ev.kind == "mouse":
            self._handle_mouse(ev, cols, rows)
            return True

        key = ev.key
        if key in ("ctrl+c", "q"):
            return False
        if key == "esc":
            if self.show_help:
                self.show_help = False
                return True
            if self.error_msg or self.status_msg:
                self.error_msg = ""
                self.status_msg = ""
                return True
            return True
        if key == "?":
            self.show_help = not self.show_help
            return True
        if self.show_help or self.ctx is None:
            return True

        self.error_msg = ""
        if key in ("h", "left"):
            self.col_idx = (self.col_idx - 1) % len(UI_GROUPS)
        elif key in ("l", "right", "tab"):
            self.col_idx = (self.col_idx + 1) % len(UI_GROUPS)
        elif key in ("1", "2", "3", "4", "5"):
            self.col_idx = int(key) - 1
        elif key in ("j", "down"):
            cards = self.group_tickets(self.col_idx)
            if cards:
                self.row_by_col[self.col_idx] = min(len(cards) - 1, self.row_by_col[self.col_idx] + 1)
        elif key in ("k", "up"):
            self.row_by_col[self.col_idx] = max(0, self.row_by_col[self.col_idx] - 1)
        elif key == "r":
            self.trigger_refresh("Refreshing from GitHub…")
        elif key in ("enter", "s"):
            self._action_dispatch(None)
        elif key == "A":
            cur = self.selected_ticket()
            if cur:
                opts = " ".join(f"{i + 1}:{k}" for i, k in enumerate(config.SUPPORTED_AGENTS[:6]))
                self.prompt = PromptState(
                    mode="pick_agent",
                    label=f"Dispatch {self._tref(cur)} with agent ({opts}): ",
                    ticket_ref=self._cli_ref(cur),
                )
        elif key == "f":
            self._action_focus_pane()
        elif key == "t":
            self._action_promote_triage()
        elif key == "m":
            cur = self.selected_ticket()
            if cur:
                opts = " ".join(lbl for lbl, _ in MOVE_TARGETS)
                self.prompt = PromptState(
                    mode="pick_column",
                    label=f"Move {self._tref(cur)} to ({opts}): ",
                    ticket_ref=self._cli_ref(cur),
                )
        elif key == "a":
            self._action_approve_push()
        elif key == "R":
            self._action_release_or_takeover()
        elif key == "n":
            if self.ctx.is_multi_repo and len(self.ctx.repos) > 1:
                aliases = list(self.ctx.repos.keys())[:9]
                opts = " ".join(f"{i + 1}:{a}" for i, a in enumerate(aliases))
                self.prompt = PromptState(
                    mode="pick_repo",
                    label=f"Select repo for new ticket ({opts}): ",
                )
            else:
                default_alias = next(iter(self.ctx.repos)) if self.ctx.is_multi_repo else ""
                self.prompt = PromptState(
                    mode="new_ticket",
                    label="New Ready ticket title: ",
                    repo_alias=default_alias,
                )
        elif key == "c":
            cur = self.selected_ticket()
            if cur:
                self.prompt = PromptState(
                    mode="comment",
                    label=f"Message/comment to {self._tref(cur)}: ",
                    ticket_ref=self._cli_ref(cur),
                )
        elif key == "o":
            cur = self.selected_ticket()
            if cur and cur.url:
                terminal.open_url(cur.url)
                self.status_msg = f"Opened {self._tref(cur)} in browser"
        return True

    def _handle_prompt_event(self, ev: terminal.InputEvent) -> None:
        if ev.kind != "key" or self.prompt is None or self.ctx is None:
            return
        p = self.prompt
        if ev.key in ("esc", "ctrl+c"):
            self.prompt = None
            return

        if p.mode == "pick_repo":
            aliases = list(self.ctx.repos.keys())[:9]
            if ev.key.isdigit() and 1 <= int(ev.key) <= len(aliases):
                chosen_repo = aliases[int(ev.key) - 1]
                self.prompt = PromptState(
                    mode="new_ticket",
                    label=f"New Ready ticket in {chosen_repo}: ",
                    repo_alias=chosen_repo,
                )
            return

        if p.mode == "pick_agent":
            if ev.key in ("1", "2", "3", "4", "5", "6"):
                chosen = config.SUPPORTED_AGENTS[int(ev.key) - 1]
                tref = p.ticket_ref
                self.prompt = None
                self._action_dispatch(chosen, ticket_ref=tref)
            return

        if p.mode == "pick_column":
            if ev.key in ("1", "2", "3", "4", "5", "6"):
                _, target_col = MOVE_TARGETS[int(ev.key) - 1]
                tref = p.ticket_ref
                disp = tref if "#" in tref else f"#{tref}"
                ctx = self.ctx
                self.prompt = None
                self._run_background_action(
                    f"Moving {disp} → {target_col.value}…",
                    lambda: (
                        self.engine.move_ticket(ctx, tref, target_col),
                        f"Moved {disp} to {target_col.value}",
                    )[1],
                )
            return

        if p.mode == "confirm_takeover":
            tref = p.ticket_ref
            self.prompt = None
            if ev.key.lower() == "y":
                self._action_dispatch(None, ticket_ref=tref, takeover=True)
            return

        if ev.key == "backspace":
            p.buffer = p.buffer[:-1]
        elif ev.key == "enter":
            text = p.buffer.strip()
            mode = p.mode
            tref = p.ticket_ref
            repo_alias = p.repo_alias
            ctx = self.ctx
            self.prompt = None
            if not text:
                return
            if mode == "new_ticket":
                sel = repo_alias or None

                def _create_new() -> str:
                    created = self.engine.deposit_issue(ctx, text, ready=True, repo_selector=sel)
                    return f"Created {created.ref(ctx.is_multi_repo)}"

                self._run_background_action("Creating Ready ticket…", _create_new)
            elif mode == "comment":
                disp = tref if "#" in tref else f"#{tref}"
                self._run_background_action(
                    f"Sending message to {disp}…",
                    lambda: (
                        self.engine.ask_ticket(ctx, tref, text),
                        f"Sent message to {disp}",
                    )[1],
                )
        elif len(ev.key) == 1 and ev.key.isprintable():
            p.buffer += ev.key

    def _handle_mouse(self, ev: terminal.InputEvent, cols: int, rows: int) -> None:
        if not ev.pressed or self.ctx is None:
            return
        if ev.button == 64:  # wheel up
            self.row_by_col[self.col_idx] = max(0, self.row_by_col[self.col_idx] - 1)
            return
        if ev.button == 65:  # wheel down
            cards = self.group_tickets(self.col_idx)
            if cards:
                self.row_by_col[self.col_idx] = min(len(cards) - 1, self.row_by_col[self.col_idx] + 1)
            return
        if ev.button == 0 and not (self.force_compact or cols < 110):
            col_w = max(18, cols // len(UI_GROUPS))
            clicked_col = min(len(UI_GROUPS) - 1, max(0, (ev.col - 1) // col_w))
            self.col_idx = clicked_col
            body_rows = max(6, rows - 2)
            inspector_rows = max(6, min(11, body_rows // 3))
            board_rows = max(4, body_rows - inspector_rows)
            if 3 <= ev.row <= board_rows:
                slot_h = 2 if (board_rows - 2) >= 6 else 1
                slot = (ev.row - 3) // slot_h
                cards = self.group_tickets(clicked_col)
                idx = self.scroll_by_col[clicked_col] + slot
                if 0 <= idx < len(cards):
                    self.row_by_col[clicked_col] = idx

    def _action_dispatch(
        self,
        agent_kind: str | None,
        *,
        ticket_ref: str | None = None,
        takeover: bool = False,
    ) -> None:
        if self.ctx is None:
            return
        ref = ticket_ref
        if ref is None:
            cur = self.selected_ticket()
            if cur is None:
                return
            skey = self._state_key(cur)
            if skey in self.local_claims and self.local_claims[skey].pane_id:
                self._action_focus_pane()
                return
            ref = self._cli_ref(cur)
        disp = ref if "#" in ref else f"#{ref}"
        kind = agent_kind or self.ctx.settings.default_agent_kind
        ctx = self.ctx

        def job() -> str:
            res = self.engine.dispatch_ticket(
                ctx,
                ref,
                agent_kind=kind,
                takeover=takeover,
                focus=True,
            )
            return f"Dispatched {disp} to {kind} ({res.claim.pane_id})"

        self._run_background_action(f"Dispatching {disp} to `{kind}`…", job)

    def _action_focus_pane(self) -> None:
        cur = self.selected_ticket()
        if not cur:
            return
        lc = self.local_claims.get(self._state_key(cur))
        disp = self._tref(cur)
        if not lc or not lc.pane_id:
            self.error_msg = f"{disp} has no local Herdr agent pane on this machine"
            return
        if self.engine.herdr.focus_pane(lc.pane_id):
            self.status_msg = f"Focused {lc.pane_id}"
        else:
            self.error_msg = f"Pane {lc.pane_id} is no longer open"

    def _action_promote_triage(self) -> None:
        cur = self.selected_ticket()
        if not cur or self.ctx is None:
            return
        ctx = self.ctx
        ref = self._cli_ref(cur)
        disp = self._tref(cur)
        self._run_background_action(
            f"Promoting {disp} → Ready…",
            lambda: (self.engine.move_ticket(ctx, ref, Column.READY), f"Promoted {disp} to Ready")[1],
        )

    def _action_approve_push(self) -> None:
        cur = self.selected_ticket()
        if not cur or self.ctx is None:
            return
        ctx = self.ctx
        ref = self._cli_ref(cur)
        disp = self._tref(cur)
        self._run_background_action(
            f"Approving & pushing {disp} to {ctx.board.target_branch}…",
            lambda: f"Landed {disp} ({self.engine.complete_ticket(ctx, ref, approve=True).commit_sha[:8]})",
        )

    def _action_release_or_takeover(self) -> None:
        cur = self.selected_ticket()
        if not cur or self.ctx is None:
            return
        ref = self._cli_ref(cur)
        disp = self._tref(cur)
        if cur.stale and self._state_key(cur) not in self.local_claims:
            self.prompt = PromptState(
                mode="confirm_takeover",
                label=f"Reclaim STALE {disp} and dispatch agent? [y/N]: ",
                ticket_ref=ref,
            )
            return
        ctx = self.ctx
        self._run_background_action(
            f"Releasing {disp}…",
            lambda: (
                self.engine.release_ticket(ctx, ref, reason="Released from TUI"),
                f"Released {disp}",
            )[1],
        )

    # --- Main Event Loop ---

    def run(self) -> int:
        daemon.ensure()
        self.load_initial_state()
        if self.ctx is not None:
            self.trigger_refresh("Syncing GitHub Project…")

        resized = False

        def _on_winch(_signum: int, _frame) -> None:
            nonlocal resized
            resized = True
            self._wake()

        prev_winch = signal.signal(signal.SIGWINCH, _on_winch)
        try:
            with terminal.raw_terminal(self.in_fd, self.out_fd, mouse=True):
                last_auto = time.time()
                while self._running:
                    cols, rows = terminal.term_size()
                    frame = self.render_frame(cols, rows)
                    os.write(self.out_fd, (terminal.CLEAR + frame).encode("utf-8"))

                    poll_every = self.ctx.settings.poll_seconds if self.ctx else 30
                    timeout = max(1.0, min(5.0, poll_every - (time.time() - last_auto)))
                    readable, _, _ = select.select([self.in_fd, self._wake_r], [], [], timeout)

                    if self._wake_r in readable:
                        try:
                            os.read(self._wake_r, 1024)
                        except OSError:
                            pass
                    if resized:
                        resized = False

                    if self.in_fd in readable:
                        raw = os.read(self.in_fd, 256)
                        if not raw:
                            break
                        for ev in terminal.parse_input_bytes(raw):
                            if not self.handle_event(ev, cols, rows):
                                self._running = False
                                break

                    if self.ctx and (time.time() - last_auto) >= self.ctx.settings.poll_seconds:
                        last_auto = time.time()
                        self.trigger_refresh()
        finally:
            signal.signal(signal.SIGWINCH, prev_winch)
            self.close_pipes()
        return 0
