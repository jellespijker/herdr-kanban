import { View, div } from "gpui-kit";
import { v_flex, h_flex, Button, Input, InputState } from "gpui-base";
import {
  spaces,
  panes,
  agents,
  terminals,
  focused,
  exec,
  hc,
  run,
  theme,
  closeOverlay,
  notify,
  nextCommand,
  props,
} from "herdr";

const COLUMNS = [
  { id: "triage", title: "Triage", short: "Tri", key: "1" },
  { id: "ready", title: "Ready", short: "Rdy", key: "2" },
  { id: "in_progress", title: "In Progress", short: "Prog", key: "3" },
  { id: "blocked", title: "Blocked", short: "Blk", key: "4" },
  { id: "review", title: "Review", short: "Rev", key: "5" },
  { id: "done", title: "Done", short: "Done", key: "6" },
];

const COL_WIDTH = 228;
const CARD_EST_HEIGHT = 104;
const DEFAULT_PRIORITIES = ["P0", "P1", "P2", "P3"];
const DEFAULT_AGENTS = ["claude", "agy", "codex", "opencode", "gemini", "copilot"];

function priorityColor(prio, t) {
  const p = String(prio || "").toUpperCase();
  if (p.startsWith("P0") || p.startsWith("CRIT")) return t.blocked || t.ansi1;
  if (p.startsWith("P1") || p.startsWith("HIGH")) return t.working || t.ansi3;
  if (p.startsWith("P2") || p.startsWith("MED")) return t.accent;
  return t.muted;
}

function columnAccent(colId, t) {
  if (colId === "triage") return t.muted;
  if (colId === "ready") return t.accent;
  if (colId === "in_progress") return t.working || t.ansi3;
  if (colId === "blocked") return t.blocked || t.ansi1;
  if (colId === "review") return t.accent;
  if (colId === "done") return t.done || t.ansi2;
  return t.muted;
}

function btn(id, label, t, opts = {}) {
  const { primary, danger, selected, small, onClick } = opts;
  const bg = primary
    ? t.accent
    : danger
    ? t.blocked || t.ansi1
    : selected
    ? t.selected
    : t.raised || t.background;
  const fg =
    primary || danger
      ? t.solid_foreground || t.background
      : selected
      ? t.accent
      : t.foreground;
  return Button.new(id)
    .px(small ? 7 : 10)
    .py(small ? 2 : 4)
    .rounded(5)
    .cursor_pointer()
    .bg(bg)
    .border_1()
    .border_color(
      primary
        ? t.accent
        : danger
        ? t.blocked || t.ansi1
        : selected
        ? t.accent
        : t.border
    )
    .text_size(small ? 11 : 12)
    .text_color(fg)
    .selected(Boolean(selected))
    .hover((s) => s.bg(primary ? t.accent : t.hover))
    .when(Boolean(onClick), (el) => el.on_click(onClick))
    .child(label);
}

function cleanTarget(rawOn) {
  if (!rawOn || rawOn === "local") return null;
  return String(rawOn);
}

function addUniqueCwd(list, cwd) {
  if (cwd && typeof cwd === "string" && !list.includes(cwd)) {
    list.push(cwd);
  }
}

function resolveSpaceTarget(overrideSpaceId, resolvedCwdBySpace) {
  const p = props() || {};
  const allSpaces = spaces() || [];
  const allPanes = panes() || [];
  const f = focused() || {};
  const cache = resolvedCwdBySpace || {};

  let targetSpace = null;
  if (overrideSpaceId) {
    targetSpace = allSpaces.find((s) => s.id === overrideSpaceId) || null;
  }
  if (!targetSpace && (p.cwd || p.folder)) {
    const folder = p.cwd || p.folder;
    targetSpace =
      allSpaces.find((s) => s.id === p.space) ||
      allSpaces.find((s) => s.folder === folder) ||
      allSpaces.find((s) => s.active) ||
      null;
  }
  if (!targetSpace) {
    targetSpace = allSpaces.find((s) => s.active) || allSpaces[0] || null;
  }

  const spaceId = targetSpace?.id || p.space || null;
  const focusedPane =
    (f.pane ? allPanes.find((pn) => pn.pane === f.pane && pn.terminal) : null) ||
    allPanes.find((pn) => pn.focused && pn.terminal) ||
    null;

  const on = cleanTarget(
    p.on ||
      p.machine ||
      targetSpace?.machine ||
      (f?.pane === null && f.terminal?.includes("/") ? f.terminal.split("/")[0] : null)
  );

  const candidates = [];
  if (!overrideSpaceId && (p.cwd || p.folder)) {
    addUniqueCwd(candidates, p.cwd || p.folder);
  }
  if (spaceId && cache[spaceId]) {
    addUniqueCwd(candidates, cache[spaceId]);
  }
  if (!overrideSpaceId && focusedPane && (!spaceId || focusedPane.space === spaceId)) {
    addUniqueCwd(candidates, focusedPane.cwd);
  }
  if (spaceId) {
    for (const pn of allPanes) {
      if (pn.space === spaceId && pn.cwd) {
        addUniqueCwd(candidates, pn.cwd);
      }
    }
  } else {
    for (const pn of allPanes) {
      if (pn.cwd) addUniqueCwd(candidates, pn.cwd);
    }
  }
  if (!overrideSpaceId && f?.cwd) {
    addUniqueCwd(candidates, f.cwd);
  }
  if (targetSpace?.folder) {
    addUniqueCwd(candidates, targetSpace.folder);
  }

  const cwd = (spaceId && cache[spaceId]) || candidates[0] || null;
  return {
    cwd,
    candidates,
    on,
    spaceId,
    spaceName: targetSpace?.name || cwd || "Workspace",
  };
}

function findTicketRuntime(ticket) {
  const allAgents = agents() || [];
  const allTerms = terminals() || [];
  const allPanes = panes() || [];

  const lc = ticket.local_claim || null;
  const claim = ticket.claim || null;
  const paneId = (lc && lc.pane_id) || (claim && claim.pane_id) || "";
  const wtPath = (lc && lc.worktree_path) || "";
  const issueStr = String(ticket.number);
  const branchName = `kanban/issue-${ticket.number}`;

  let matchedTerm = null;
  if (paneId) {
    matchedTerm = allTerms.find((tm) => tm.id === paneId || tm.name === paneId);
  }
  if (!matchedTerm) {
    matchedTerm = allTerms.find(
      (tm) =>
        (tm.labels && tm.labels["herdr.kanban.issue"] === issueStr) ||
        (wtPath && tm.cwd === wtPath) ||
        tm.branch === branchName
    );
  }

  let matchedPane = null;
  if (paneId) {
    matchedPane = allPanes.find((pn) => String(pn.pane) === paneId || pn.terminal === paneId);
  }
  if (!matchedPane && matchedTerm) {
    matchedPane = allPanes.find((pn) => pn.terminal === matchedTerm.id);
  }
  if (!matchedPane && wtPath) {
    matchedPane = allPanes.find((pn) => pn.cwd === wtPath);
  }

  let matchedAgent = null;
  if (paneId) {
    matchedAgent = allAgents.find((ag) => String(ag.pane) === paneId || ag.terminal === paneId);
  }
  if (!matchedAgent && matchedPane) {
    matchedAgent = allAgents.find(
      (ag) => ag.pane === matchedPane.pane || ag.terminal === matchedPane.terminal
    );
  }
  if (!matchedAgent) {
    matchedAgent = allAgents.find(
      (ag) =>
        (wtPath && ag.cwd === wtPath) ||
        ag.branch === branchName ||
        (ag.title && ag.title.includes(`#${ticket.number}`))
    );
  }

  return {
    agent: matchedAgent,
    terminal: matchedTerm,
    pane: matchedPane,
    active: Boolean(matchedAgent || matchedTerm || matchedPane),
  };
}

export default class KanbanBoard extends View {
  init(_props, cx) {
    this.handle = cx.focus_handle();
    if (props()?.part !== "bar") {
      this.handle.focus();
    }

    const bus = (globalThis.__herdrKanbanBus ??= {
      views: new Set(),
      searchQuery: "",
      repoFilter: "",
      agentKind: "claude",
      overrideSpaceId: null,
      showDrawer: false,
      modalMode: null,
      resolvedCwdBySpace: {},
    });
    bus.resolvedCwdBySpace ??= {};
    bus.views.add(this);
    this.bus = bus;

    this.board = null;
    this.loading = false;
    this.busyText = "";
    this.error = "";
    this.statusMessage = "";
    this.typing = false;

    this.overrideSpaceId = bus.overrideSpaceId;
    this.lastTargetKey = "";
    this.repoFilter = bus.repoFilter;
    this.agentKind = bus.agentKind;
    this.userPickedAgent = false;

    this.colIdx = 1; // Ready column
    this.rowByCol = { 0: 0, 1: 0, 2: 0, 3: 0, 4: 0, 5: 0 };
    this.selectedRef = null;
    this.showDrawer = bus.showDrawer;

    // 2D Board & Filter strip pan state
    this.panX = 0;
    this.panY = 0;
    this.filterPanX = 0;
    this.drag = null;
    this.justDragged = false;
    this.size = { width: 640, height: 720 };

    // Inline modal modes: null | "deposit" | "ask" | "triage"
    this.modalMode = bus.modalMode;
    this.depositPriority = "P1";
    this.depositRepo = "";
    this.depositReady = true;

    this.searchInput = InputState.new({ placeholder: "Filter tickets…" });
    this.depositTitleInput = InputState.new({ placeholder: "Ticket title…" });
    this.depositBodyInput = InputState.new({ placeholder: "Description or depends-on #issue…" });
    this.askInput = InputState.new({ placeholder: "Message or question for this ticket's agent…" });

    this.searchQuery = bus.searchQuery;
    this.searchInput.on("change", (_e, cx) => {
      this.searchQuery = (this.searchInput.value() || "").trim();
      this.syncBus({ searchQuery: this.searchQuery }, cx);
    });
    this.searchInput.on("focus", (_e, cx) => {
      this.typing = true;
      cx.notify();
    });
    this.searchInput.on("blur", (_e, cx) => {
      this.typing = false;
      cx.notify();
    });

    for (const inp of [this.depositTitleInput, this.depositBodyInput, this.askInput]) {
      inp.on("focus", (_e, cx) => {
        this.typing = true;
        cx.notify();
      });
      inp.on("blur", (_e, cx) => {
        this.typing = false;
        cx.notify();
      });
    }
    this.depositTitleInput.on("submit", (_e, cx) => this.submitDeposit(cx));
    this.askInput.on("submit", (_e, cx) => this.submitAsk(this.selectedTicket(), cx));

    this.refresh(cx, false);

    cx.timer.every(12000, (cx) => {
      if (!this.loading && !this.busyText) {
        this.refresh(cx, true);
      }
    });

    cx.spawn(async (cx) => {
      try {
        while (true) {
          const cmd = await nextCommand();
          if (!cmd) break;
          if (cmd === "focus" || cmd?.action === "focus") {
            this.handle.focus();
            cx.notify();
          } else if (cmd === "refresh" || cmd?.action === "refresh") {
            this.refresh(cx, false);
          }
        }
      } catch {}
    });
  }

  target() {
    return resolveSpaceTarget(this.overrideSpaceId, this.bus?.resolvedCwdBySpace);
  }

  syncBus(patch, cx) {
    if (!this.bus) return;
    Object.assign(this.bus, patch);
    for (const v of this.bus.views) {
      if (patch.searchQuery !== undefined) v.searchQuery = patch.searchQuery;
      if (patch.repoFilter !== undefined) {
        v.repoFilter = patch.repoFilter;
        v.syncSelectionAfterLoad();
      }
      if (patch.agentKind !== undefined) {
        v.agentKind = patch.agentKind;
        v.userPickedAgent = true;
      }
      if (patch.overrideSpaceId !== undefined) v.overrideSpaceId = patch.overrideSpaceId;
      if (patch.showDrawer !== undefined) v.showDrawer = patch.showDrawer;
      if (patch.modalMode !== undefined) v.modalMode = patch.modalMode;
    }
    if (cx) cx.notify();
  }

  execKanban(args, targetOverride) {
    const tgt = targetOverride || this.target();
    const cwd = tgt.cwd;
    const fullArgs = args.length > 0 ? [args[0]] : [];
    if (cwd) {
      fullArgs.push("--cwd", cwd);
    }
    if (args.length > 1) {
      fullArgs.push(...args.slice(1));
    }
    return exec("herdr-kanban", fullArgs, { cwd, on: tgt.on });
  }

  refresh(cx, silent = false) {
    const tgt = this.target();
    const candidates = Array.isArray(tgt.candidates) && tgt.candidates.length ? tgt.candidates : tgt.cwd ? [tgt.cwd] : [];
    this.lastTargetKey = `${tgt.on || "local"}:${tgt.spaceId || ""}:${tgt.cwd || ""}`;
    if (!candidates.length) {
      this.board = null;
      this.loading = false;
      this.error = "Open or focus a repository workspace in Herdr to view its Kanban board.";
      cx.notify();
      return;
    }
    if (!silent) {
      this.loading = true;
      this.error = "";
      cx.notify();
    }

    cx.spawn(async (cx) => {
      try {
        let lastRes = null;
        let chosenCwd = candidates[0];
        for (const candCwd of candidates) {
          chosenCwd = candCwd;
          const res = await this.execKanban(["board", "--json"], { ...tgt, cwd: candCwd });
          lastRes = res;
          if (res && res.code === 0) {
            if (tgt.spaceId && this.bus && this.bus.resolvedCwdBySpace) {
              this.bus.resolvedCwdBySpace[tgt.spaceId] = candCwd;
            }
            break;
          }
          const errOut = ((res && (res.stderr || res.stdout)) || "").toLowerCase();
          if (!errOut.includes(".herdr-kanban.toml")) {
            break;
          }
        }

        const res = lastRes;
        if (!res || res.code !== 0) {
          const stderr = (res && (res.stderr || res.stdout)) || "Failed to load herdr-kanban board";
          let parsedErr = stderr.trim();
          try {
            const j = JSON.parse(stderr);
            if (j && j.error) parsedErr = j.error;
          } catch {}
          if (!silent || !this.board) {
            this.error = parsedErr;
          }
          this.loading = false;
          cx.notify();
          return;
        }

        const data = JSON.parse(res.stdout);
        this.board = data;
        this.activeCwd = chosenCwd;
        this.error = "";
        this.loading = false;

        if (data.settings && data.settings.default_agent_kind && !this.userPickedAgent) {
          this.agentKind = data.settings.default_agent_kind;
        }
        if (!this.depositRepo && data.default_repo) {
          this.depositRepo = data.default_repo;
        }
        this.syncSelectionAfterLoad();
        cx.notify();
      } catch (err) {
        if (!silent || !this.board) {
          this.error = String(err && err.message ? err.message : err);
        }
        this.loading = false;
        cx.notify();
      }
    });
  }

  filteredTickets() {
    if (!this.board || !Array.isArray(this.board.tickets)) return [];
    const q = (this.searchQuery || "").toLowerCase();
    return this.board.tickets.filter((t) => {
      if (this.repoFilter) {
        const matchRepo =
          t.repo_alias === this.repoFilter ||
          t.target_repo === this.repoFilter ||
          t.repo_name === this.repoFilter;
        if (!matchRepo) return false;
      }
      if (!q) return true;
      const ref = String(t.ref || `#${t.number}`).toLowerCase();
      const title = String(t.title || "").toLowerCase();
      const prio = String(t.priority || "").toLowerCase();
      const owner = t.claim && t.claim.owner ? `@${t.claim.owner.toLowerCase()}` : "";
      const labels = Array.isArray(t.labels) ? t.labels.join(" ").toLowerCase() : "";
      return (
        ref.includes(q) ||
        title.includes(q) ||
        prio.includes(q) ||
        owner.includes(q) ||
        labels.includes(q)
      );
    });
  }

  ticketsByColumn() {
    const all = this.filteredTickets();
    const map = {};
    for (const col of COLUMNS) {
      map[col.id] = [];
    }
    for (const t of all) {
      const colId = map[t.column] ? t.column : "triage";
      map[colId].push(t);
    }
    return map;
  }

  syncSelectionAfterLoad() {
    const byCol = this.ticketsByColumn();
    if (this.selectedRef) {
      for (let c = 0; c < COLUMNS.length; c++) {
        const list = byCol[COLUMNS[c].id] || [];
        const r = list.findIndex((t) => (t.cli_ref || String(t.number)) === this.selectedRef);
        if (r >= 0) {
          this.colIdx = c;
          this.rowByCol[c] = r;
          return;
        }
      }
    }
    const curColId = COLUMNS[this.colIdx]?.id || "ready";
    const curList = byCol[curColId] || [];
    if (curList.length === 0) {
      const nonEmptyIdx = COLUMNS.findIndex((col) => (byCol[col.id] || []).length > 0);
      if (nonEmptyIdx >= 0) {
        this.colIdx = nonEmptyIdx;
      }
    }
    const activeList = byCol[COLUMNS[this.colIdx].id] || [];
    const curRow = Math.min(this.rowByCol[this.colIdx] || 0, Math.max(0, activeList.length - 1));
    this.rowByCol[this.colIdx] = curRow;
    const picked = activeList[curRow];
    this.selectedRef = picked ? picked.cli_ref || String(picked.number) : null;
  }

  selectedTicket() {
    const byCol = this.ticketsByColumn();
    if (this.selectedRef) {
      for (const col of COLUMNS) {
        const found = (byCol[col.id] || []).find(
          (t) => (t.cli_ref || String(t.number)) === this.selectedRef
        );
        if (found) return found;
      }
    }
    const list = byCol[COLUMNS[this.colIdx]?.id] || [];
    const r = Math.min(this.rowByCol[this.colIdx] || 0, Math.max(0, list.length - 1));
    return list[r] || null;
  }

  boardViewportDims() {
    const viewW = Math.max(320, this.size?.width ?? 640);
    const chromeH = 128 + (this.showDrawer ? 280 : 0) + (this.modalMode ? 64 : 0);
    const viewH = Math.max(160, (this.size?.height ?? 720) - chromeH);
    const totalW = COLUMNS.length * COL_WIDTH;
    const byCol = this.ticketsByColumn();
    let maxCards = 1;
    for (const col of COLUMNS) {
      const len = (byCol[col.id] || []).length;
      if (len > maxCards) maxCards = len;
    }
    const contentH = Math.max(viewH, maxCards * CARD_EST_HEIGHT + 32);
    const minPanX = Math.min(0, viewW - totalW);
    const minPanY = Math.min(0, viewH - contentH);
    return { viewW, viewH, totalW, contentH, minPanX, minPanY };
  }

  setPan(nextX, nextY, cx) {
    const { minPanX, minPanY } = this.boardViewportDims();
    this.panX = Math.max(minPanX, Math.min(0, Math.round(nextX)));
    this.panY = Math.max(minPanY, Math.min(0, Math.round(nextY)));
    if (cx) cx.notify();
  }

  panBy(dx, dy, cx) {
    this.setPan(this.panX + dx, this.panY + dy, cx);
  }

  resetPan(cx) {
    this.panX = 0;
    this.panY = 0;
    this.drag = null;
    if (cx) cx.notify();
  }

  panToColumn(colIdx, cx) {
    const { viewW } = this.boardViewportDims();
    const targetCenter = colIdx * COL_WIDTH + COL_WIDTH / 2;
    const desiredPanX = Math.round(viewW / 2 - targetCenter);
    this.setPan(desiredPanX, 0, cx);
  }

  ensureVisible(colIdx, rowIdx) {
    const { viewW, viewH, minPanX, minPanY } = this.boardViewportDims();
    let nextX = this.panX;
    let nextY = this.panY;

    const colLeft = colIdx * COL_WIDTH;
    const colRight = colLeft + COL_WIDTH;
    if (colLeft < -nextX) {
      nextX = -colLeft;
    } else if (colRight > -nextX + viewW) {
      nextX = -(colRight - viewW);
    }

    const cardTop = rowIdx * CARD_EST_HEIGHT;
    const cardBottom = cardTop + CARD_EST_HEIGHT + 16;
    if (cardTop < -nextY) {
      nextY = -cardTop;
    } else if (cardBottom > -nextY + viewH) {
      nextY = -(cardBottom - viewH);
    }

    this.panX = Math.max(minPanX, Math.min(0, Math.round(nextX)));
    this.panY = Math.max(minPanY, Math.min(0, Math.round(nextY)));
  }

  startBoardDrag(e, cx) {
    this.handle.focus();
    if (e.button === "left" && (e.click_count ?? 1) >= 2 && !e._fromCard) {
      this.resetPan(cx);
      return;
    }
    this.justDragged = false;
    this.drag = {
      kind: "board",
      startX: e.position.x,
      startY: e.position.y,
      origPanX: this.panX,
      origPanY: this.panY,
      moved: false,
    };
  }

  startFilterDrag(e, _cx) {
    this.justDragged = false;
    this.drag = {
      kind: "filter",
      startX: e.position.x,
      startY: e.position.y,
      origPanX: this.filterPanX,
      origPanY: 0,
      moved: false,
    };
  }

  moveDrag(e, cx) {
    if (!this.drag) return;
    const dx = e.position.x - this.drag.startX;
    const dy = e.position.y - this.drag.startY;
    if (!this.drag.moved && Math.hypot(dx, dy) > 5) {
      this.drag.moved = true;
      this.justDragged = true;
    }
    if (!this.drag.moved) return;
    if (this.drag.kind === "filter") {
      this.filterPanX = Math.min(0, Math.round(this.drag.origPanX + dx));
      cx.notify();
    } else {
      this.setPan(this.drag.origPanX + dx, this.drag.origPanY + dy, cx);
    }
  }

  endDrag(_e, cx) {
    if (!this.drag) return;
    const wasMoving = this.drag.moved;
    this.drag = null;
    if (wasMoving && cx) cx.notify();
  }

  selectColumnRow(cIdx, rIdx, cx, autoPan = true) {
    const byCol = this.ticketsByColumn();
    const clampedCol = Math.max(0, Math.min(COLUMNS.length - 1, cIdx));
    const list = byCol[COLUMNS[clampedCol].id] || [];
    const clampedRow = Math.max(0, Math.min(Math.max(0, list.length - 1), rIdx));
    this.colIdx = clampedCol;
    this.rowByCol[clampedCol] = clampedRow;
    const t = list[clampedRow];
    this.selectedRef = t ? t.cli_ref || String(t.number) : null;
    if (autoPan) {
      this.ensureVisible(clampedCol, clampedRow);
    }
    cx.notify();
  }

  moveHorizontal(delta, cx) {
    const nextCol = Math.max(0, Math.min(COLUMNS.length - 1, this.colIdx + delta));
    const byCol = this.ticketsByColumn();
    const list = byCol[COLUMNS[nextCol].id] || [];
    const row = Math.min(this.rowByCol[nextCol] || 0, Math.max(0, list.length - 1));
    this.selectColumnRow(nextCol, row, cx, true);
  }

  moveVertical(delta, cx) {
    const byCol = this.ticketsByColumn();
    const list = byCol[COLUMNS[this.colIdx].id] || [];
    if (!list.length) return;
    const cur = this.rowByCol[this.colIdx] || 0;
    const next = Math.max(0, Math.min(list.length - 1, cur + delta));
    this.selectColumnRow(this.colIdx, next, cx, true);
  }

  focusTicketAgent(ticket, cx) {
    if (!ticket) return;
    const rt = findTicketRuntime(ticket);
    if (rt.pane) {
      if (rt.pane.space) {
        run("space.select", { id: rt.pane.space });
      }
      if (rt.pane.tab) {
        run("tab.select", { id: rt.pane.tab });
      }
      run("pane.focus", { pane: rt.pane.pane });
      this.statusMessage = `Focused pane for ${ticket.ref || "#" + ticket.number}`;
      cx.notify();
      return;
    }
    if (rt.agent) {
      if (rt.agent.space) {
        run("space.select", { id: rt.agent.space });
      }
      if (rt.agent.tab) {
        run("tab.select", { id: rt.agent.tab });
      }
      if (rt.agent.pane) {
        run("pane.focus", { pane: rt.agent.pane });
      }
      this.statusMessage = `Focused agent ${rt.agent.name || rt.agent.title}`;
      cx.notify();
      return;
    }
    const lc = ticket.local_claim;
    if (lc && lc.worktree_path) {
      const tgt = this.target();
      run("tab.new", {
        name: `${ticket.ref || "#" + ticket.number} ${ticket.title.slice(0, 24)}`,
        space: tgt.spaceId,
        cwd: lc.worktree_path,
      });
      this.statusMessage = `Opened worktree tab for ${ticket.ref || "#" + ticket.number}`;
      cx.notify();
      return;
    }
    notify({ title: "Kanban", message: `No active terminal found for ${ticket.ref || "#" + ticket.number}` });
  }

  claimAndDispatch(ticket, takeover, cx) {
    if (!ticket || this.busyText) return;
    const cliRef = ticket.cli_ref || String(ticket.number);
    const disp = ticket.ref || `#${ticket.number}`;
    const kind = this.agentKind || "claude";
    const tgt = this.target();

    this.busyText = `Claiming & dispatching ${disp} (${kind})…`;
    this.error = "";
    cx.notify();

    cx.spawn(async (cx) => {
      try {
        const claimArgs = ["claim", cliRef, "--kind", kind, "--json"];
        if (takeover || ticket.stale) claimArgs.push("--takeover");
        const res = await this.execKanban(claimArgs, tgt);
        if (!res || res.code !== 0) {
          let errText = (res && (res.stderr || res.stdout)) || "Claim failed";
          try {
            const j = JSON.parse(errText);
            if (j && j.error) errText = j.error;
          } catch {}
          this.error = errText.trim();
          this.busyText = "";
          cx.notify();
          return;
        }

        const payload = JSON.parse(res.stdout);
        const claim = payload.claim || {};
        const prompt = payload.prompt || "";
        const argv =
          Array.isArray(payload.agent_argv) && payload.agent_argv.length > 0
            ? payload.agent_argv
            : [kind];
        const tabName = `${disp} ${String(ticket.title || "").slice(0, 26)}`.trim();

        run("tab.new", {
          name: tabName,
          space: tgt.spaceId,
          cwd: claim.worktree_path || tgt.cwd,
          argv,
          task: prompt,
        });

        this.busyText = "";
        this.statusMessage = `Dispatched ${disp} to ${kind} in ${claim.worktree_path || "worktree"}`;
        this.refresh(cx, true);
      } catch (err) {
        this.busyText = "";
        this.error = String(err && err.message ? err.message : err);
        cx.notify();
      }
    });
  }

  runTicketAction(label, args, cx) {
    if (this.busyText) return;
    const tgt = this.target();
    this.busyText = label;
    this.error = "";
    cx.notify();

    cx.spawn(async (cx) => {
      try {
        const res = await this.execKanban([...args, "--json"], tgt);
        this.busyText = "";
        if (!res || res.code !== 0) {
          let errText = (res && (res.stderr || res.stdout)) || `${label} failed`;
          try {
            const j = JSON.parse(errText);
            if (j && j.error) errText = j.error;
          } catch {}
          this.error = errText.trim();
          cx.notify();
          return;
        }
        this.statusMessage = label;
        this.modalMode = null;
        this.refresh(cx, true);
      } catch (err) {
        this.busyText = "";
        this.error = String(err && err.message ? err.message : err);
        cx.notify();
      }
    });
  }

  submitDeposit(cx) {
    const title = (this.depositTitleInput.value() || "").trim();
    const body = (this.depositBodyInput.value() || "").trim();
    if (!title) {
      this.error = "Ticket title is required to deposit a ticket.";
      cx.notify();
      return;
    }
    const args = ["deposit", title];
    if (body) args.push("--body", body);
    if (this.depositPriority) args.push("--priority", this.depositPriority);
    if (this.depositRepo) args.push("--repo", this.depositRepo);
    if (this.depositReady) args.push("--ready", "--force");

    this.depositTitleInput.set_value("");
    this.depositBodyInput.set_value("");
    this.runTicketAction(`Deposited "${title.slice(0, 32)}"`, args, cx);
  }

  submitAsk(ticket, cx) {
    if (!ticket) return;
    const msg = (this.askInput.value() || "").trim();
    if (!msg) return;
    const cliRef = ticket.cli_ref || String(ticket.number);
    const hasClaim =
      ticket.claim && (ticket.claim.state === "active" || ticket.claim.state === "review_ready");
    const cmd = hasClaim ? "ask" : "comment";
    this.askInput.set_value("");
    this.runTicketAction(`Sent message to ${ticket.ref || "#" + ticket.number}`, [cmd, cliRef, msg], cx);
  }

  handleKeyDown(ev, cx) {
    const key = ev.key || "";
    const shift = Boolean(ev.modifiers?.shift);
    if (this.typing) {
      if (key === "escape") {
        this.typing = false;
        this.modalMode = null;
        this.handle.focus();
        cx.stop_propagation();
        cx.notify();
      }
      return;
    }
    if (this.modalMode) {
      if (key === "escape") {
        this.modalMode = null;
        this.handle.focus();
        cx.stop_propagation();
        cx.notify();
      }
      return;
    }

    const ticket = this.selectedTicket();
    const panStepX = 120;
    const panStepY = 96;
    if (shift && (key === "h" || key === "H" || key === "left")) {
      this.panBy(panStepX, 0, cx);
      cx.stop_propagation();
    } else if (shift && (key === "l" || key === "L" || key === "right")) {
      this.panBy(-panStepX, 0, cx);
      cx.stop_propagation();
    } else if (shift && (key === "k" || key === "K" || key === "up")) {
      this.panBy(0, panStepY, cx);
      cx.stop_propagation();
    } else if (shift && (key === "j" || key === "J" || key === "down")) {
      this.panBy(0, -panStepY, cx);
      cx.stop_propagation();
    } else if (key === "0") {
      this.resetPan(cx);
      cx.stop_propagation();
    } else if (key >= "1" && key <= "6") {
      const targetCol = Number(key) - 1;
      const byCol = this.ticketsByColumn();
      const list = byCol[COLUMNS[targetCol].id] || [];
      const row = Math.min(this.rowByCol[targetCol] || 0, Math.max(0, list.length - 1));
      this.selectColumnRow(targetCol, row, cx, false);
      this.panToColumn(targetCol, cx);
      cx.stop_propagation();
    } else if (key === "h" || key === "left") {
      this.moveHorizontal(-1, cx);
      cx.stop_propagation();
    } else if (key === "l" || key === "right") {
      this.moveHorizontal(1, cx);
      cx.stop_propagation();
    } else if (key === "j" || key === "down") {
      this.moveVertical(1, cx);
      cx.stop_propagation();
    } else if (key === "k" || key === "up") {
      this.moveVertical(-1, cx);
      cx.stop_propagation();
    } else if (key === "r") {
      this.refresh(cx, false);
      cx.stop_propagation();
    } else if (key === "d" || key === "i" || key === " " || key === "enter") {
      this.syncBus({ showDrawer: !this.showDrawer }, cx);
      cx.stop_propagation();
    } else if (key === "n") {
      this.modalMode = "deposit";
      this.depositTitleInput.focus();
      cx.stop_propagation();
      cx.notify();
    } else if (key === "c" && ticket) {
      this.modalMode = "ask";
      this.askInput.focus();
      cx.stop_propagation();
      cx.notify();
    } else if (key === "s" && ticket) {
      this.claimAndDispatch(ticket, Boolean(ticket.stale), cx);
      cx.stop_propagation();
    } else if (key === "f" && ticket) {
      this.focusTicketAgent(ticket, cx);
      cx.stop_propagation();
    } else if (key === "t" && ticket) {
      const cliRef = ticket.cli_ref || String(ticket.number);
      if (ticket.column === "triage") {
        this.runTicketAction(
          `Promoted ${ticket.ref || "#" + ticket.number} to Ready`,
          ["triage", cliRef, "--ready", "--force"],
          cx
        );
      } else {
        this.modalMode = "triage";
        cx.notify();
      }
      cx.stop_propagation();
    } else if (key === "a" && ticket) {
      const cliRef = ticket.cli_ref || String(ticket.number);
      this.runTicketAction(
        `Approved & landing ${ticket.ref || "#" + ticket.number}`,
        ["done", cliRef, "--approve"],
        cx
      );
      cx.stop_propagation();
    } else if (key === "q") {
      const myPane = props()?.pane;
      if (myPane) run("pane.close", { pane: myPane });
      else closeOverlay(props()?.overlay || "herdr-kanban");
      cx.stop_propagation();
    } else if (key === "escape") {
      if (this.showDrawer) {
        this.syncBus({ showDrawer: false }, cx);
      } else {
        const myPane = props()?.pane;
        if (myPane) run("pane.close", { pane: myPane });
        else closeOverlay(props()?.overlay || "herdr-kanban");
      }
      cx.stop_propagation();
    }
  }

  renderHeader(t, tgt, byCol) {
    const panelW = this.size?.width ?? 640;
    const isWide = panelW >= 560;
    const boardTitle =
      (this.board && this.board.project && this.board.project.title) ||
      (this.board && this.board.repo) ||
      tgt.spaceName ||
      "Herdr Kanban";
    const branchBadge =
      this.board && this.board.target_branch
        ? `${this.board.remote || "origin"}/${this.board.target_branch}`
        : "";
    const reposMap = (this.board && this.board.repos) || {};
    const rawRepoAliases = Object.keys(reposMap);
    const allTickets = (this.board && Array.isArray(this.board.tickets) && this.board.tickets) || [];
    const filteredCount = this.filteredTickets().length;

    // Count tickets per repo alias and sort active repos first
    const repoCounts = {};
    for (const tk of allTickets) {
      const key = tk.target_repo || tk.repo_alias || tk.repo_name || "";
      if (key) repoCounts[key] = (repoCounts[key] || 0) + 1;
    }
    const repoAliases = [...rawRepoAliases].sort((a, b) => {
      const ca = repoCounts[a] || 0;
      const cb = repoCounts[b] || 0;
      if (cb !== ca) return cb - ca;
      return a.localeCompare(b);
    });

    const isPanned = this.panX !== 0 || this.panY !== 0;

    return v_flex()
      .w_full()
      .flex_none()
      .bg(t.surface)
      .border_b(1)
      .border_color(t.border)
      .child(
        // Row 1: Clean board identity & essential controls
        h_flex()
          .w_full()
          .h(34)
          .flex_none()
          .items_center()
          .justify_between()
          .gap(6)
          .px(10)
          .child(
            h_flex()
              .items_center()
              .gap(6)
              .min_w(0)
              .flex_1()
              .child(
                div()
                  .font_semibold()
                  .text_size(12.5)
                  .text_color(t.foreground)
                  .truncate()
                  .child(boardTitle)
              )
              .when(Boolean(tgt.on), (d) =>
                d.child(
                  div()
                    .flex_none()
                    .px(5)
                    .py(1)
                    .rounded(4)
                    .bg(t.selected)
                    .text_size(10)
                    .text_color(t.accent)
                    .child(`@${tgt.on}`)
                )
              )
              .when(Boolean(branchBadge && isWide), (d) =>
                d.child(
                  div()
                    .max_w(140)
                    .px(5)
                    .py(1)
                    .rounded(4)
                    .bg(t.background)
                    .border_1()
                    .border_color(t.border)
                    .text_size(10)
                    .text_color(t.muted)
                    .truncate()
                    .child(branchBadge)
                )
              )
              .when(allTickets.length > 0, (d) =>
                d.child(
                  div()
                    .flex_none()
                    .px(5)
                    .py(1)
                    .rounded(4)
                    .bg(t.background)
                    .text_size(10)
                    .text_color(this.repoFilter || this.searchQuery ? t.accent : t.dim)
                    .child(
                      this.repoFilter || this.searchQuery
                        ? `${filteredCount}/${allTickets.length}`
                        : `${allTickets.length}`
                    )
                )
              )
          )
          .child(
            h_flex()
              .items_center()
              .flex_none()
              .gap(5)
              .child(
                h_flex()
                  .id("search-box")
                  .w(isWide ? 160 : 120)
                  .h(24)
                  .px(6)
                  .items_center()
                  .gap(4)
                  .rounded(5)
                  .bg(t.background)
                  .border_1()
                  .border_color(this.searchQuery ? t.accent : t.border)
                  .on_mouse_down("left", (_e, cx) => cx.stop_propagation())
                  .child(
                    div()
                      .flex_1()
                      .min_w(0)
                      .h_full()
                      .child(Input.new(this.searchInput).h_full().text_size(11).text_color(t.foreground))
                  )
                  .when(Boolean(this.searchQuery), (el) =>
                    el.child(
                      div()
                        .id("clear-search")
                        .px(3)
                        .rounded(3)
                        .cursor_pointer()
                        .text_size(10)
                        .text_color(t.muted)
                        .hover((s) => s.text_color(t.foreground))
                        .on_click((_ev, cx) => {
                          this.searchInput.set_value("");
                          this.searchQuery = "";
                          this.syncBus({ searchQuery: "" }, cx);
                        })
                        .child("×")
                    )
                  )
              )
              .when(isPanned, (el) =>
                el.child(
                  btn("btn-reset-pan", "Center", t, {
                    small: true,
                    onClick: (_ev, cx) => this.resetPan(cx),
                  })
                )
              )
              .child(
                btn("btn-deposit", "+ Deposit", t, {
                  primary: true,
                  small: true,
                  onClick: (_ev, cx) => {
                    const nextMode = this.modalMode === "deposit" ? null : "deposit";
                    this.syncBus({ modalMode: nextMode }, cx);
                    if (nextMode === "deposit") this.depositTitleInput.focus();
                  },
                })
              )
              .child(
                btn("btn-drawer", "Detail", t, {
                  small: true,
                  selected: this.showDrawer,
                  onClick: (_ev, cx) => {
                    this.syncBus({ showDrawer: !this.showDrawer }, cx);
                  },
                })
              )
              .child(
                btn("btn-refresh", "↻", t, {
                  small: true,
                  onClick: (_ev, cx) => this.refresh(cx, false),
                })
              )
          )
      )
      .when(Boolean(this.board), (hdr) =>
        hdr.child(
          // Row 2: Column quick-jump pills + pannable repo filter strip
          h_flex()
            .id("kanban-subbar")
            .w_full()
            .h(26)
            .flex_none()
            .items_center()
            .justify_between()
            .gap(8)
            .px(10)
            .border_t(1)
            .border_color(t.border)
            .child(
              // Column quick-jump & count strip
              h_flex()
                .items_center()
                .flex_none()
                .gap(3)
                .children(
                  COLUMNS.map((col, idx) => {
                    const count = ((byCol && byCol[col.id]) || []).length;
                    const active = this.colIdx === idx;
                    const accent = columnAccent(col.id, t);
                    return h_flex()
                      .id(`col-jump-${col.id}`)
                      .items_center()
                      .gap(4)
                      .px(5)
                      .py(1)
                      .rounded(4)
                      .cursor_pointer()
                      .bg(active ? t.selected : t.background)
                      .border_1()
                      .border_color(active ? accent : t.border)
                      .hover((s) => s.bg(t.hover))
                      .on_click((_ev, cx) => {
                        const list = (byCol && byCol[col.id]) || [];
                        const row = Math.min(this.rowByCol[idx] || 0, Math.max(0, list.length - 1));
                        this.selectColumnRow(idx, row, cx, false);
                        this.panToColumn(idx, cx);
                      })
                      .child(div().size(5).rounded(3).bg(accent))
                      .child(
                        div()
                          .text_size(10)
                          .font_medium()
                          .text_color(active ? t.foreground : count > 0 ? t.muted : t.dim)
                          .child(`${col.short} ${count}`)
                      );
                  })
                )
            )
            .when(repoAliases.length > 1, (d) =>
              d.child(
                div()
                  .id("kanban-filter-viewport")
                  .relative()
                  .flex_1()
                  .min_w(80)
                  .h(22)
                  .overflow_hidden()
                  .cursor_grab()
                  .on_mouse_down("left", (e, cx) => this.startFilterDrag(e, cx))
                  .on_mouse_down("middle", (e, cx) => this.startFilterDrag(e, cx))
                  .on_scroll_wheel((e, cx) => {
                    const rawDelta = Math.abs(e.delta?.x ?? 0) >= Math.abs(e.delta?.y ?? 0)
                      ? (e.delta?.x ?? 0)
                      : (e.delta?.y ?? 0);
                    const step = Math.abs(rawDelta) <= 3 ? rawDelta * 28 : rawDelta;
                    this.filterPanX = Math.min(0, Math.round(this.filterPanX + step));
                    cx.notify();
                  })
                  .child(
                    h_flex()
                      .id("kanban-filter-track")
                      .absolute()
                      .left(this.filterPanX)
                      .top(1)
                      .items_center()
                      .gap(4)
                      .child(
                        div()
                          .id("repo-filter-all")
                          .flex_none()
                          .px(6)
                          .py(1)
                          .rounded(4)
                          .cursor_pointer()
                          .bg(!this.repoFilter ? t.selected : t.background)
                          .text_size(10)
                          .text_color(!this.repoFilter ? t.foreground : t.dim)
                          .child("all repos")
                          .on_click((_ev, cx) => {
                            if (this.justDragged) {
                              this.justDragged = false;
                              return;
                            }
                            this.syncBus({ repoFilter: "" }, cx);
                          })
                      )
                      .children(
                        repoAliases.map((alias) => {
                          const active = this.repoFilter === alias;
                          const cnt = repoCounts[alias] || 0;
                          return div()
                            .id(`repo-filter-${alias}`)
                            .flex_none()
                            .px(6)
                            .py(1)
                            .rounded(4)
                            .cursor_pointer()
                            .bg(active ? t.selected : t.background)
                            .text_size(10)
                            .text_color(active ? t.accent : cnt > 0 ? t.muted : t.dim)
                            .child(cnt > 0 ? `${alias} · ${cnt}` : alias)
                            .on_click((_ev, cx) => {
                              if (this.justDragged) {
                                this.justDragged = false;
                                return;
                              }
                              this.syncBus({ repoFilter: active ? "" : alias }, cx);
                            });
                        })
                      )
                  )
              )
            )
        )
      );
  }

  renderModalBar(t, selectedTicket) {
    if (!this.modalMode) return null;

    if (this.modalMode === "deposit") {
      const prios =
        this.board && Array.isArray(this.board.priorities) && this.board.priorities.length
          ? this.board.priorities
          : DEFAULT_PRIORITIES;
      const repos = Object.keys((this.board && this.board.repos) || {});

      return v_flex()
        .w_full()
        .flex_none()
        .gap(8)
        .px(12)
        .py(10)
        .bg(t.surface)
        .border_b(1)
        .border_color(t.accent)
        .child(
          h_flex()
            .items_center()
            .justify_between()
            .child(div().text_size(12).font_semibold().text_color(t.accent).child("Deposit New Ticket"))
            .child(
              h_flex()
                .items_center()
                .gap(6)
                .children(
                  prios.map((p) => {
                    const active = this.depositPriority === p;
                    return div()
                      .id(`dep-prio-${p}`)
                      .px(6)
                      .py(2)
                      .rounded(4)
                      .cursor_pointer()
                      .bg(active ? t.selected : t.background)
                      .text_size(11)
                      .text_color(active ? priorityColor(p, t) : t.dim)
                      .child(p)
                      .on_click((_ev, cx) => {
                        this.depositPriority = p;
                        cx.notify();
                      });
                  })
                )
                .when(repos.length > 1, (d) =>
                  d.children(
                    repos.map((alias) => {
                      const active = this.depositRepo === alias;
                      return div()
                        .id(`dep-repo-${alias}`)
                        .px(6)
                        .py(2)
                        .rounded(4)
                        .cursor_pointer()
                        .bg(active ? t.selected : t.background)
                        .text_size(11)
                        .text_color(active ? t.accent : t.dim)
                        .child(`repo:${alias}`)
                        .on_click((_ev, cx) => {
                          this.depositRepo = alias;
                          cx.notify();
                        });
                    })
                  )
                )
                .child(
                  div()
                    .id("dep-ready-toggle")
                    .px(8)
                    .py(2)
                    .rounded(4)
                    .cursor_pointer()
                    .bg(this.depositReady ? t.selected : t.background)
                    .text_size(11)
                    .text_color(this.depositReady ? t.done || t.ansi2 : t.dim)
                    .child(this.depositReady ? "Column: Ready" : "Column: Triage")
                    .on_click((_ev, cx) => {
                      this.depositReady = !this.depositReady;
                      cx.notify();
                    })
                )
            )
        )
        .child(
          h_flex()
            .items_center()
            .gap(8)
            .child(
              div()
                .id("dep-title-wrap")
                .flex_1()
                .h(28)
                .px(8)
                .flex()
                .items_center()
                .rounded(6)
                .bg(t.background)
                .border_1()
                .border_color(t.border)
                .on_mouse_down("left", (_e, cx) => cx.stop_propagation())
                .child(Input.new(this.depositTitleInput).h_full().text_size(12).text_color(t.foreground))
            )
            .child(
              div()
                .id("dep-body-wrap")
                .flex_1()
                .h(28)
                .px(8)
                .flex()
                .items_center()
                .rounded(6)
                .bg(t.background)
                .border_1()
                .border_color(t.border)
                .on_mouse_down("left", (_e, cx) => cx.stop_propagation())
                .child(Input.new(this.depositBodyInput).h_full().text_size(12).text_color(t.foreground))
            )
            .child(
              btn("dep-submit", "Create Ticket", t, {
                primary: true,
                small: true,
                onClick: (_ev, cx) => this.submitDeposit(cx),
              })
            )
            .child(
              btn("dep-cancel", "Cancel", t, {
                small: true,
                onClick: (_ev, cx) => {
                  this.modalMode = null;
                  this.handle.focus();
                  cx.notify();
                },
              })
            )
        );
    }

    if (this.modalMode === "ask" && selectedTicket) {
      const disp = selectedTicket.ref || `#${selectedTicket.number}`;
      return h_flex()
        .w_full()
        .flex_none()
        .items_center()
        .gap(8)
        .px(12)
        .py(8)
        .bg(t.surface)
        .border_b(1)
        .border_color(t.accent)
        .child(div().text_size(12).font_semibold().text_color(t.accent).child(`Message ${disp}:`))
        .child(
          div()
            .id("ask-input-wrap")
            .flex_1()
            .h(28)
            .px(8)
            .flex()
            .items_center()
            .rounded(6)
            .bg(t.background)
            .border_1()
            .border_color(t.border)
            .on_mouse_down("left", (_e, cx) => cx.stop_propagation())
            .child(Input.new(this.askInput).h_full().text_size(12).text_color(t.foreground))
        )
        .child(
          btn("ask-send", "Send", t, {
            primary: true,
            small: true,
            onClick: (_ev, cx) => this.submitAsk(selectedTicket, cx),
          })
        )
        .child(
          btn("ask-cancel", "Cancel", t, {
            small: true,
            onClick: (_ev, cx) => {
              this.modalMode = null;
              this.handle.focus();
              cx.notify();
            },
          })
        );
    }

    if (this.modalMode === "triage" && selectedTicket) {
      const cliRef = selectedTicket.cli_ref || String(selectedTicket.number);
      const disp = selectedTicket.ref || `#${selectedTicket.number}`;
      const prios =
        this.board && Array.isArray(this.board.priorities) && this.board.priorities.length
          ? this.board.priorities
          : DEFAULT_PRIORITIES;

      return h_flex()
        .w_full()
        .flex_none()
        .items_center()
        .justify_between()
        .gap(8)
        .px(12)
        .py(8)
        .bg(t.surface)
        .border_b(1)
        .border_color(t.accent)
        .child(
          h_flex()
            .items_center()
            .gap(6)
            .child(div().text_size(12).font_semibold().text_color(t.foreground).child(`Move ${disp}:`))
            .children(
              COLUMNS.map((col) =>
                btn(`tri-col-${col.id}`, col.title, t, {
                  small: true,
                  selected: selectedTicket.column === col.id,
                  onClick: (_ev, cx) =>
                    this.runTicketAction(
                      `Moved ${disp} to ${col.title}`,
                      ["triage", cliRef, "--column", col.id, "--force"],
                      cx
                    ),
                })
              )
            )
        )
        .child(
          h_flex()
            .items_center()
            .gap(6)
            .child(div().text_size(11).text_color(t.muted).child("Priority:"))
            .children(
              prios.map((p) =>
                btn(`tri-prio-${p}`, p, t, {
                  small: true,
                  selected: selectedTicket.priority === p,
                  onClick: (_ev, cx) =>
                    this.runTicketAction(
                      `Set ${disp} priority to ${p}`,
                      ["triage", cliRef, "--priority", p],
                      cx
                    ),
                })
              )
            )
            .child(
              btn("tri-close", "Done", t, {
                small: true,
                onClick: (_ev, cx) => {
                  this.modalMode = null;
                  this.handle.focus();
                  cx.notify();
                },
              })
            )
        );
    }

    return null;
  }

  renderCard(ticket, colIdx, rowIdx, isSelected, t) {
    const disp = ticket.ref || `#${ticket.number}`;
    const claim = ticket.claim || null;
    const hasActiveClaim =
      claim && (claim.state === "active" || claim.state === "review_ready");
    const overlaps = Array.isArray(ticket.overlaps) ? ticket.overlaps : [];
    const unmetDeps = Array.isArray(ticket.unmet_deps) ? ticket.unmet_deps : [];
    const labels = Array.isArray(ticket.labels) ? ticket.labels : [];
    const checkDone = ticket.checklist_done || 0;
    const checkTotal = ticket.checklist_total || 0;
    const touchedCount =
      claim && Array.isArray(claim.touched_files) ? claim.touched_files.length : 0;
    const rt = findTicketRuntime(ticket);

    let agentStateColor = t.accent;
    if (rt.agent) {
      const st = rt.agent.status || "idle";
      if (st === "working") agentStateColor = t.working || t.ansi3;
      else if (st === "blocked") agentStateColor = t.blocked || t.ansi1;
      else if (st === "done") agentStateColor = t.done || t.ansi2;
    }

    return v_flex()
      .id(`card-${colIdx}-${ticket.cli_ref || ticket.number}`)
      .w_full()
      .flex_none()
      .gap(6)
      .p(10)
      .rounded(8)
      .cursor_pointer()
      .bg(isSelected ? t.selected : t.surface)
      .border_1()
      .border_color(
        overlaps.length > 0
          ? t.blocked || t.ansi1
          : isSelected
          ? t.accent
          : t.border
      )
      .hover((s) => s.bg(isSelected ? t.selected : t.hover))
      .on_mouse_down("left", (e, cx) => this.startBoardDrag({ ...e, _fromCard: true }, cx))
      .on_mouse_down("middle", (e, cx) => this.startBoardDrag({ ...e, _fromCard: true }, cx))
      .on_click((_ev, cx) => {
        if (this.justDragged) {
          this.justDragged = false;
          return;
        }
        this.selectColumnRow(colIdx, rowIdx, cx, false);
        this.syncBus({ showDrawer: true }, cx);
      })
      .child(
        // Top row: ref, priority, wave/deps, overlap/stale badges
        h_flex()
          .w_full()
          .items_center()
          .justify_between()
          .gap(4)
          .child(
            h_flex()
              .items_center()
              .gap(6)
              .child(
                div()
                  .text_size(11)
                  .font_semibold()
                  .text_color(isSelected ? t.accent : t.muted)
                  .child(disp)
              )
              .when(Boolean(ticket.priority), (d) =>
                d.child(
                  div()
                    .px(5)
                    .py(1)
                    .rounded(4)
                    .bg(t.background)
                    .text_size(10)
                    .font_semibold()
                    .text_color(priorityColor(ticket.priority, t))
                    .child(ticket.priority)
                )
              )
              .when(
                Boolean(ticket.target_repo && ticket.target_repo !== ticket.repo_alias),
                (d) =>
                  d.child(
                    div()
                      .px(5)
                      .py(1)
                      .rounded(4)
                      .bg(t.background)
                      .text_size(10)
                      .text_color(t.accent)
                      .child(ticket.target_repo)
                  )
              )
          )
          .child(
            h_flex()
              .items_center()
              .gap(4)
              .when(unmetDeps.length > 0, (d) =>
                d.child(
                  div()
                    .px(5)
                    .py(1)
                    .rounded(4)
                    .bg(t.background)
                    .text_size(10)
                    .text_color(t.blocked || t.ansi1)
                    .child(`deps:${unmetDeps.map((n) => "#" + n).join(",")}`)
                )
              )
              .when(
                unmetDeps.length === 0 &&
                  ((Array.isArray(ticket.depends_on) && ticket.depends_on.length > 0) ||
                    (Array.isArray(ticket.blocks) && ticket.blocks.length > 0)),
                (d) =>
                  d.child(
                    div()
                      .px(5)
                      .py(1)
                      .rounded(4)
                      .bg(t.background)
                      .text_size(10)
                      .text_color(t.done || t.ansi2)
                      .child(`wave:${ticket.wave || 0}`)
                  )
              )
              .when(Boolean(ticket.stale), (d) =>
                d.child(
                  div()
                    .px(5)
                    .py(1)
                    .rounded(4)
                    .bg(t.background)
                    .text_size(10)
                    .font_semibold()
                    .text_color(t.working || t.ansi3)
                    .child("STALE")
                )
              )
              .when(overlaps.length > 0, (d) =>
                d.child(
                  div()
                    .px(5)
                    .py(1)
                    .rounded(4)
                    .bg(t.background)
                    .text_size(10)
                    .font_semibold()
                    .text_color(t.blocked || t.ansi1)
                    .child("OVERLAP!")
                )
              )
          )
      )
      .child(
        // Title
        div()
          .w_full()
          .text_size(12)
          .font_medium()
          .text_color(t.foreground)
          .truncate()
          .child(String(ticket.title || ""))
      )
      .when(labels.length > 0, (d) =>
        d.child(
          h_flex()
            .gap(4)
            .overflow_hidden()
            .children(
              labels.slice(0, 3).map((lbl) =>
                div()
                  .px(5)
                  .py(1)
                  .rounded(4)
                  .bg(t.background)
                  .text_size(10)
                  .text_color(t.dim)
                  .truncate()
                  .child(lbl)
              )
            )
        )
      )
      .when(checkTotal > 0 || touchedCount > 0 || hasActiveClaim, (d) =>
        d.child(
          h_flex()
            .w_full()
            .items_center()
            .justify_between()
            .gap(4)
            .child(
              h_flex()
                .items_center()
                .gap(6)
                .when(hasActiveClaim, (c) =>
                  c.child(
                    h_flex()
                      .items_center()
                      .gap(5)
                      .px(6)
                      .py(2)
                      .rounded(4)
                      .bg(t.background)
                      .child(
                        div()
                          .size(6)
                          .rounded(3)
                          .bg(rt.active ? agentStateColor : t.dim)
                      )
                      .child(
                        div()
                          .text_size(10)
                          .text_color(t.foreground)
                          .child(`@${claim.owner}:${claim.agent_kind}`)
                      )
                  )
                )
            )
            .child(
              h_flex()
                .items_center()
                .gap(6)
                .when(checkTotal > 0, (c) =>
                  c.child(
                    div()
                      .text_size(10)
                      .text_color(checkDone === checkTotal ? t.done || t.ansi2 : t.dim)
                      .child(`[${checkDone}/${checkTotal}]`)
                  )
                )
                .when(touchedCount > 0, (c) =>
                  c.child(
                    div()
                      .text_size(10)
                      .text_color(overlaps.length > 0 ? t.blocked || t.ansi1 : t.dim)
                      .child(`${touchedCount}f`)
                  )
                )
            )
        )
      );
  }

  renderColumnHeader(col, colIdx, count, t) {
    const isFocusedCol = this.colIdx === colIdx;
    const accent = columnAccent(col.id, t);

    return h_flex()
      .id(`col-hdr-${col.id}`)
      .w(COL_WIDTH)
      .h(34)
      .flex_none()
      .items_center()
      .justify_between()
      .px(10)
      .bg(isFocusedCol ? t.selected : t.surface)
      .border_r(1)
      .border_b(1)
      .border_color(isFocusedCol ? accent : t.border)
      .on_mouse_down("left", (e, cx) => this.startBoardDrag(e, cx))
      .on_mouse_down("middle", (e, cx) => this.startBoardDrag(e, cx))
      .on_click((_ev, cx) => {
        if (this.justDragged) {
          this.justDragged = false;
          return;
        }
        const byCol = this.ticketsByColumn();
        const list = byCol[col.id] || [];
        const row = Math.min(this.rowByCol[colIdx] || 0, Math.max(0, list.length - 1));
        this.selectColumnRow(colIdx, row, cx, false);
      })
      .child(
        h_flex()
          .items_center()
          .gap(6)
          .child(div().size(7).rounded(4).bg(accent))
          .child(
            div()
              .text_size(12)
              .font_semibold()
              .text_color(isFocusedCol ? t.foreground : t.muted)
              .child(col.title)
          )
      )
      .child(
        div()
          .px(6)
          .py(1)
          .rounded(4)
          .bg(t.background)
          .text_size(10)
          .text_color(t.dim)
          .child(String(count))
      );
  }

  renderColumnStack(col, colIdx, tickets, selectedRef, t) {
    return v_flex()
      .id(`col-${col.id}`)
      .w(COL_WIDTH)
      .flex_none()
      .gap(8)
      .p(8)
      .when(tickets.length === 0, (d) =>
        d.child(
          div()
            .w_full()
            .p(16)
            .text_size(11)
            .text_color(t.dim)
            .child("No tickets")
        )
      )
      .children(
        tickets.map((ticket, rIdx) => {
          const refKey = ticket.cli_ref || String(ticket.number);
          const isSelected = selectedRef === refKey;
          return this.renderCard(ticket, colIdx, rIdx, isSelected, t);
        })
      );
  }

  renderBoardCanvas(byCol, selectedRef, t) {
    const { viewW, totalW } = this.boardViewportDims();
    const showHorizBar = totalW > viewW;
    const thumbW = showHorizBar ? Math.max(36, Math.round((viewW / totalW) * viewW)) : 0;
    const maxScrollX = Math.max(1, totalW - viewW);
    const scrollProgressX = Math.max(0, Math.min(1, -this.panX / maxScrollX));
    const thumbLeft = showHorizBar ? Math.round(scrollProgressX * Math.max(0, viewW - thumbW)) : 0;

    return v_flex()
      .id("kanban-board-canvas")
      .flex_1()
      .min_h(0)
      .w_full()
      .overflow_hidden()
      .child(
        // Sticky column header strip that pans horizontally in lockstep with the board
        div()
          .id("kanban-col-headers-viewport")
          .relative()
          .w_full()
          .h(34)
          .flex_none()
          .overflow_hidden()
          .bg(t.surface)
          .when(Boolean(this.drag), (el) => el.cursor_grabbing())
          .when(!this.drag, (el) => el.cursor_grab())
          .on_mouse_down("left", (e, cx) => this.startBoardDrag(e, cx))
          .on_mouse_down("middle", (e, cx) => this.startBoardDrag(e, cx))
          .child(
            h_flex()
              .id("kanban-col-headers-track")
              .absolute()
              .left(this.panX)
              .top(0)
              .h(34)
              .children(
                COLUMNS.map((col, idx) =>
                  this.renderColumnHeader(col, idx, (byCol[col.id] || []).length, t)
                )
              )
          )
          .when(showHorizBar, (el) =>
            el.child(
              div()
                .id("kanban-pan-thumb-x")
                .absolute()
                .left(thumbLeft)
                .top(32)
                .w(thumbW)
                .h(2)
                .rounded(1)
                .bg(t.accent)
            )
          )
      )
      .child(
        // 2D Pannable columns & cards viewport
        div()
          .id("kanban-viewport")
          .relative()
          .flex_1()
          .min_h(0)
          .w_full()
          .overflow_hidden()
          .bg(t.background)
          .when(Boolean(this.drag), (el) => el.cursor_grabbing())
          .when(!this.drag, (el) => el.cursor_grab())
          .on_mouse_down("left", (e, cx) => this.startBoardDrag(e, cx))
          .on_mouse_down("middle", (e, cx) => this.startBoardDrag(e, cx))
          .on_scroll_wheel((e, cx) => {
            const rawDx = e.delta?.x ?? 0;
            const rawDy = e.delta?.y ?? 0;
            const dx = Math.abs(rawDx) > 0 && Math.abs(rawDx) <= 3 ? rawDx * 28 : rawDx;
            const dy = Math.abs(rawDy) > 0 && Math.abs(rawDy) <= 3 ? rawDy * 28 : rawDy;
            if (e.modifiers?.shift) {
              this.panBy(dy !== 0 ? dy : dx, 0, cx);
              return;
            }
            this.panBy(dx, dy, cx);
          })
          .child(
            // Full-height column lanes & vertical grid dividers
            h_flex()
              .id("kanban-lanes-bg")
              .absolute()
              .left(this.panX)
              .top(0)
              .h_full()
              .children(
                COLUMNS.map((col) =>
                  div()
                    .id(`lane-bg-${col.id}`)
                    .w(COL_WIDTH)
                    .h_full()
                    .flex_none()
                    .border_r(1)
                    .border_color(t.border)
                )
              )
          )
          .child(
            // 2D Translated columns & cards track
            h_flex()
              .id("kanban-columns-track")
              .absolute()
              .left(this.panX)
              .top(this.panY)
              .items_start()
              .children(
                COLUMNS.map((col, idx) =>
                  this.renderColumnStack(col, idx, byCol[col.id] || [], selectedRef, t)
                )
              )
          )
      );
  }

  renderInspector(ticket, t) {
    if (!this.showDrawer) return null;

    if (!ticket) {
      return v_flex()
        .w_full()
        .h(140)
        .flex_none()
        .p(16)
        .bg(t.surface)
        .border_t(1)
        .border_color(t.border)
        .items_center()
        .justify_center()
        .child(
          div()
            .text_size(12)
            .text_color(t.dim)
            .child("Select a ticket card to inspect details & actions.")
        );
    }

    const disp = ticket.ref || `#${ticket.number}`;
    const cliRef = ticket.cli_ref || String(ticket.number);
    const claim = ticket.claim || null;
    const lc = ticket.local_claim || null;
    const hasClaim = claim && (claim.state === "active" || claim.state === "review_ready");
    const overlaps = Array.isArray(ticket.overlaps) ? ticket.overlaps : [];
    const deps = Array.isArray(ticket.depends_on) ? ticket.depends_on : [];
    const unmet = new Set(Array.isArray(ticket.unmet_deps) ? ticket.unmet_deps : []);
    const blocks = Array.isArray(ticket.blocks) ? ticket.blocks : [];
    const checklist = claim && Array.isArray(claim.checklist) ? claim.checklist : [];
    const touched = claim && Array.isArray(claim.touched_files) ? claim.touched_files : [];
    const rt = findTicketRuntime(ticket);
    const agentChoices =
      (this.board &&
        this.board.settings &&
        Array.isArray(this.board.settings.supported_agents) &&
        this.board.settings.supported_agents) ||
      DEFAULT_AGENTS;

    return v_flex()
      .id("ticket-inspector")
      .w_full()
      .h(280)
      .min_h(0)
      .flex_none()
      .bg(t.surface)
      .border_t(1)
      .border_color(t.border)
      .child(
        // Inspector top header
        v_flex()
          .w_full()
          .flex_none()
          .gap(8)
          .p(12)
          .border_b(1)
          .border_color(t.border)
          .child(
            h_flex()
              .w_full()
              .items_center()
              .justify_between()
              .child(
                h_flex()
                  .items_center()
                  .gap(6)
                  .child(div().text_size(12).font_semibold().text_color(t.accent).child(disp))
                  .child(
                    div()
                      .px(6)
                      .py(2)
                      .rounded(4)
                      .bg(t.background)
                      .text_size(11)
                      .font_semibold()
                      .text_color(columnAccent(ticket.column, t))
                      .child(ticket.column)
                  )
                  .when(Boolean(ticket.priority), (d) =>
                    d.child(
                      div()
                        .px(6)
                        .py(2)
                        .rounded(4)
                        .bg(t.background)
                        .text_size(11)
                        .font_semibold()
                        .text_color(priorityColor(ticket.priority, t))
                        .child(ticket.priority)
                    )
                  )
              )
              .child(
                btn("insp-close", "×", t, {
                  small: true,
                  onClick: (_ev, cx) => {
                    this.syncBus({ showDrawer: false }, cx);
                  },
                })
              )
          )
          .child(
            div()
              .w_full()
              .text_size(13)
              .font_semibold()
              .text_color(t.foreground)
              .child(String(ticket.title || ""))
          )
          .child(
            // Primary action buttons + compact agent harness selector for ticket dispatch
            h_flex()
              .items_center()
              .justify_between()
              .gap(6)
              .child(
                h_flex()
                  .items_center()
                  .gap(6)
                  .children([
                    btn(
                      "act-dispatch",
                      ticket.stale
                        ? `Takeover (${this.agentKind})`
                        : hasClaim && lc
                        ? `Re-dispatch (${this.agentKind})`
                        : `Dispatch (${this.agentKind})`,
                      t,
                      {
                        primary: true,
                        small: true,
                        onClick: (_ev, cx) => this.claimAndDispatch(ticket, Boolean(ticket.stale), cx),
                      }
                    ),
                    ...(rt.active || (lc && lc.worktree_path)
                      ? [
                          btn("act-focus", rt.active ? "Focus (f)" : "Worktree (f)", t, {
                            small: true,
                            onClick: (_ev, cx) => this.focusTicketAgent(ticket, cx),
                          }),
                        ]
                      : []),
                    ...(ticket.column === "triage"
                      ? [
                          btn("act-promote", "-> Ready (t)", t, {
                            small: true,
                            onClick: (_ev, cx) =>
                              this.runTicketAction(
                                `Promoted ${disp} to Ready`,
                                ["triage", cliRef, "--ready", "--force"],
                                cx
                              ),
                          }),
                        ]
                      : []),
                    ...(ticket.column === "review" || ticket.column === "in_progress"
                      ? [
                          btn("act-approve", ticket.column === "review" ? "Approve (a)" : "Land (a)", t, {
                            small: true,
                            onClick: (_ev, cx) =>
                              this.runTicketAction(`Landing ${disp}`, ["done", cliRef, "--approve"], cx),
                          }),
                        ]
                      : []),
                  ])
              )
              .child(
                h_flex()
                  .items_center()
                  .gap(2)
                  .px(3)
                  .py(2)
                  .rounded(5)
                  .bg(t.background)
                  .border_1()
                  .border_color(t.border)
                  .children(
                    agentChoices.slice(0, 4).map((ag) => {
                      const active = this.agentKind === ag;
                      return div()
                        .id(`insp-agent-${ag}`)
                        .px(5)
                        .py(1)
                        .rounded(4)
                        .cursor_pointer()
                        .bg(active ? t.selected : t.background)
                        .text_size(10)
                        .text_color(active ? t.accent : t.dim)
                        .child(ag)
                        .on_click((_ev, cx) => {
                          this.syncBus({ agentKind: ag }, cx);
                        });
                    })
                  )
              )
          )
          .child(
            h_flex()
              .gap(6)
              .children([
                btn("act-triage", "Move / Triage", t, {
                  small: true,
                  selected: this.modalMode === "triage",
                  onClick: (_ev, cx) => {
                    this.modalMode = this.modalMode === "triage" ? null : "triage";
                    cx.notify();
                  },
                }),
                btn("act-ask", hasClaim ? "Ask Agent (c)" : "Comment (c)", t, {
                  small: true,
                  selected: this.modalMode === "ask",
                  onClick: (_ev, cx) => {
                    this.modalMode = this.modalMode === "ask" ? null : "ask";
                    if (this.modalMode === "ask") this.askInput.focus();
                    cx.notify();
                  },
                }),
                ...(hasClaim
                  ? [
                      btn("act-release", "Release", t, {
                        danger: true,
                        small: true,
                        onClick: (_ev, cx) =>
                          this.runTicketAction(`Released ${disp}`, ["release", cliRef], cx),
                      }),
                    ]
                  : []),
              ])
          )
      )
      .child(
        // Scrollable detail body
        v_flex()
          .id("inspector-scroll")
          .flex_1()
          .min_h(0)
          .w_full()
          .gap(10)
          .p(12)
          .overflow_y_scrollbar()
          // Overlap warning box
          .when(overlaps.length > 0, (d) =>
            d.child(
              v_flex()
                .w_full()
                .flex_none()
                .gap(4)
                .p(8)
                .rounded(6)
                .bg(t.background)
                .border_1()
                .border_color(t.blocked || t.ansi1)
                .child(
                  div()
                    .text_size(11)
                    .font_semibold()
                    .text_color(t.blocked || t.ansi1)
                    .child("⚠ Touched-File Overlap Detected")
                )
                .children(
                  overlaps.map((ov) =>
                    div()
                      .text_size(11)
                      .text_color(t.foreground)
                      .child(
                        `#${ov.other_issue} (@${ov.other_owner}, ${ov.other_agent}): ${(ov.files || []).join(", ")}`
                      )
                  )
                )
            )
          )
          // Active claim info
          .when(Boolean(hasClaim), (d) =>
            d.child(
              v_flex()
                .w_full()
                .flex_none()
                .gap(4)
                .p(8)
                .rounded(6)
                .bg(t.background)
                .border_1()
                .border_color(ticket.stale ? t.working || t.ansi3 : t.border)
                .child(
                  h_flex()
                    .items_center()
                    .justify_between()
                    .child(
                      div()
                        .text_size(11)
                        .font_semibold()
                        .text_color(t.foreground)
                        .child(`Claimed by @${claim.owner} (${claim.agent_kind})`)
                    )
                    .child(
                      div()
                        .text_size(10)
                        .text_color(ticket.stale ? t.working || t.ansi3 : t.dim)
                        .child(ticket.stale ? "STALE LEASE" : claim.state)
                    )
                )
                .when(Boolean(claim.note), (c) =>
                  c.child(div().text_size(11).text_color(t.accent).child(`Note: ${claim.note}`))
                )
                .when(Boolean(lc && lc.worktree_path), (c) =>
                  c.child(
                    div()
                      .text_size(10)
                      .text_color(t.dim)
                      .truncate()
                      .child(`wt: ${lc.worktree_path}`)
                  )
                )
            )
          )
          // Dependencies & Wave
          .when(deps.length > 0 || blocks.length > 0, (d) =>
            d.child(
              v_flex()
                .w_full()
                .flex_none()
                .gap(4)
                .p(8)
                .rounded(6)
                .bg(t.background)
                .border_1()
                .border_color(t.border)
                .child(
                  div()
                    .text_size(11)
                    .font_semibold()
                    .text_color(t.foreground)
                    .child(`Dependency Graph (Wave ${ticket.wave || 0})`)
                )
                .when(deps.length > 0, (c) =>
                  c.child(
                    div()
                      .text_size(11)
                      .text_color(unmet.size > 0 ? t.blocked || t.ansi1 : t.done || t.ansi2)
                      .child(
                        "Depends on: " +
                          deps.map((n) => `#${n}${unmet.has(n) ? " (open)" : " (done)"}`).join(", ")
                      )
                  )
                )
                .when(blocks.length > 0, (c) =>
                  c.child(
                    div()
                      .text_size(11)
                      .text_color(t.dim)
                      .child("Blocks: " + blocks.map((n) => `#${n}`).join(", "))
                  )
                )
            )
          )
          // Checklist
          .when(checklist.length > 0, (d) =>
            d.child(
              v_flex()
                .w_full()
                .flex_none()
                .gap(4)
                .p(8)
                .rounded(6)
                .bg(t.background)
                .border_1()
                .border_color(t.border)
                .child(
                  div()
                    .text_size(11)
                    .font_semibold()
                    .text_color(t.foreground)
                    .child(
                      `Agent Checklist (${ticket.checklist_done || 0}/${ticket.checklist_total || checklist.length})`
                    )
                )
                .children(
                  checklist.map((item) => {
                    const done = String(item).trim().toLowerCase().startsWith("[x]");
                    return div()
                      .text_size(11)
                      .text_color(done ? t.done || t.ansi2 : t.foreground)
                      .child(item);
                  })
                )
            )
          )
          // Touched files
          .when(touched.length > 0, (d) =>
            d.child(
              v_flex()
                .w_full()
                .flex_none()
                .gap(4)
                .p(8)
                .rounded(6)
                .bg(t.background)
                .border_1()
                .border_color(t.border)
                .child(
                  div()
                    .text_size(11)
                    .font_semibold()
                    .text_color(t.foreground)
                    .child(`Touched Files (${touched.length})`)
                )
                .children(
                  touched.slice(0, 12).map((fp) =>
                    div().text_size(10).text_color(t.dim).truncate().child(fp)
                  )
                )
            )
          )
          // Description
          .child(
            v_flex()
              .w_full()
              .flex_none()
              .gap(4)
              .child(div().text_size(11).font_semibold().text_color(t.muted).child("Description"))
              .child(
                div()
                  .w_full()
                  .p(8)
                  .rounded(6)
                  .bg(t.background)
                  .border_1()
                  .border_color(t.border)
                  .text_size(11)
                  .text_color(t.foreground)
                  .child(String(ticket.body || "(No description provided.)"))
              )
          )
      );
  }

  renderFooter(t, tgt) {
    return h_flex()
      .w_full()
      .h(28)
      .flex_none()
      .items_center()
      .justify_between()
      .px(12)
      .bg(t.surface)
      .border_t(1)
      .border_color(t.border)
      .child(
        h_flex()
          .items_center()
          .gap(8)
          .when(Boolean(this.busyText), (d) =>
            d.child(
              div()
                .text_size(11)
                .font_semibold()
                .text_color(t.working || t.ansi3)
                .child(this.busyText)
            )
          )
          .when(Boolean(!this.busyText && this.error), (d) =>
            d.child(
              div()
                .text_size(11)
                .text_color(t.blocked || t.ansi1)
                .truncate()
                .child(this.error)
            )
          )
          .when(Boolean(!this.busyText && !this.error && this.statusMessage), (d) =>
            d.child(
              div()
                .text_size(11)
                .text_color(t.done || t.ansi2)
                .truncate()
                .child(this.statusMessage)
            )
          )
          .when(Boolean(!this.busyText && !this.error && !this.statusMessage), (d) =>
            d.child(
              div()
                .text_size(11)
                .text_color(t.dim)
                .child(tgt.cwd ? `${tgt.on ? tgt.on + ":" : ""}${tgt.cwd}` : "No active repo")
            )
          )
      )
      .child(
        div()
          .text_size(10.5)
          .text_color(t.dim)
          .truncate()
          .child("drag/wheel pan · 1-6 jump · 0 center · h/j/k/l nav · Enter detail · s dispatch · n deposit")
      );
  }

  render(cx) {
    const t = theme();
    const p = props() || {};
    const part = p.part || "";
    const tgt = this.target();
    const curTargetKey = `${tgt.on || "local"}:${tgt.spaceId || ""}:${tgt.cwd || ""}`;
    if (curTargetKey !== this.lastTargetKey && !this.loading) {
      this.lastTargetKey = curTargetKey;
      this.refresh(cx, true);
    }

    if (this.bus) {
      this.searchQuery = this.bus.searchQuery;
      this.repoFilter = this.bus.repoFilter;
      this.agentKind = this.bus.agentKind;
      this.overrideSpaceId = this.bus.overrideSpaceId;
      this.showDrawer = this.bus.showDrawer;
      if (this.bus.modalMode !== undefined) this.modalMode = this.bus.modalMode;
    }

    const byCol = this.ticketsByColumn();

    if (part === "bar") {
      return v_flex()
        .id("kanban-bar-root")
        .size_full()
        .justify_center()
        .bg(t.surface)
        .text_color(t.foreground)
        .text_size(12)
        .on_mouse_move((e, cx) => this.moveDrag(e, cx))
        .on_mouse_up("left", (e, cx) => this.endDrag(e, cx))
        .on_mouse_up("middle", (e, cx) => this.endDrag(e, cx))
        .on_size_change((size, cx) => {
          if (
            Math.abs((this.size?.width ?? 0) - size.width) > 2 ||
            Math.abs((this.size?.height ?? 0) - size.height) > 2
          ) {
            this.size = size;
            cx.notify();
          }
        })
        .child(this.renderHeader(t, tgt, byCol));
    }

    const selected = this.selectedTicket();
    const selectedRef = selected ? selected.cli_ref || String(selected.number) : null;
    const modalBar = this.renderModalBar(t, selected);

    return v_flex()
      .id("kanban-root")
      .track_focus(this.handle)
      .on_key_down((ev, cx) => this.handleKeyDown(ev, cx))
      .on_mouse_move((e, cx) => this.moveDrag(e, cx))
      .on_mouse_up("left", (e, cx) => this.endDrag(e, cx))
      .on_mouse_up("middle", (e, cx) => this.endDrag(e, cx))
      .on_hover((hovered, cx) => {
        if (!hovered && this.drag) {
          this.drag = null;
          cx.notify();
        }
      })
      .on_size_change((size, cx) => {
        if (
          Math.abs((this.size?.width ?? 0) - size.width) > 2 ||
          Math.abs((this.size?.height ?? 0) - size.height) > 2
        ) {
          this.size = size;
          cx.notify();
        }
      })
      .size_full()
      .min_h(0)
      .overflow_hidden()
      .bg(t.background)
      .text_color(t.foreground)
      .text_size(12)
      .when(part !== "board", (d) => d.child(this.renderHeader(t, tgt, byCol)))
      .when(Boolean(modalBar), (d) => d.child(modalBar))
      .child(
        v_flex()
          .flex_1()
          .min_h(0)
          .w_full()
          .overflow_hidden()
          .when(Boolean(this.loading && !this.board), (d) =>
            d.child(
              v_flex()
                .flex_1()
                .h_full()
                .items_center()
                .justify_center()
                .gap(8)
                .child(div().text_size(13).text_color(t.muted).child("Loading GitHub Project v2 Kanban board…"))
            )
          )
          .when(Boolean(!this.loading && !this.board && this.error), (d) =>
            d.child(
              v_flex()
                .flex_1()
                .h_full()
                .items_center()
                .justify_center()
                .gap(10)
                .p(24)
                .child(
                  div()
                    .text_size(13)
                    .font_semibold()
                    .text_color(t.blocked || t.ansi1)
                    .child("Kanban Board Unavailable")
                )
                .child(div().text_size(11).text_color(t.muted).child(this.error))
                .child(
                  btn("retry-load", "Retry", t, {
                    primary: true,
                    small: true,
                    onClick: (_ev, cx) => this.refresh(cx, false),
                  })
                )
            )
          )
          .when(Boolean(this.board), (d) =>
            d
              .child(this.renderBoardCanvas(byCol, selectedRef, t))
              .when(Boolean(this.showDrawer), (col) => col.child(this.renderInspector(selected, t)))
          )
      )
      .child(this.renderFooter(t, tgt));
  }
}
