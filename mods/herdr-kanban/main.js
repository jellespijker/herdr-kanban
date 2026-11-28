import { View, div, svg, image } from "gpui-kit";
import { v_flex, h_flex, Button, Input, InputState, TextView } from "gpui-base";
import {
  spaces,
  panes,
  agents,
  terminals,
  focused,
  exec,
  run,
  theme,
  closeOverlay,
  notify,
  nextCommand,
  props,
  shared,
  setShared,
  checking,
} from "herdr";

const COLUMNS = [
  { id: "triage", title: "Triage", short: "Tri", key: "1" },
  { id: "ready", title: "Ready", short: "Rdy", key: "2" },
  { id: "in_progress", title: "In Progress", short: "Prog", key: "3" },
  { id: "blocked", title: "Blocked", short: "Blk", key: "4" },
  { id: "review", title: "Review", short: "Rev", key: "5" },
  { id: "done", title: "Done", short: "Done", key: "6" },
];

const COLUMN_TITLE_BY_ID = Object.fromEntries(COLUMNS.map((c) => [c.id, c.title]));
const ACTIVE_COL_MIN_WIDTH = 276;
const ACTIVE_COL_MAX_WIDTH = 360;
const EMPTY_COL_COMPACT_WIDTH = 104;
const UNIFORM_COL_WIDTH = 248;
const CARD_EST_HEIGHT = 124;
const BACKGROUND_POLL_MS = 60000;
const RATE_LIMIT_BASE_BACKOFF_MS = 60000;
const RATE_LIMIT_MAX_BACKOFF_MS = 300000;
const DEFAULT_PRIORITIES = ["P0", "P1", "P2", "P3"];
const DEFAULT_AGENTS = ["agy", "claude", "codex", "opencode", "gemini", "copilot"];

function isRateLimitError(message) {
  const lower = String(message || "").toLowerCase();
  return (
    lower.includes("rate limit") ||
    lower.includes("secondary rate") ||
    lower.includes("http 403") ||
    lower.includes("http 429") ||
    lower.includes("api rate limit exceeded")
  );
}

function withAlpha(hexColor, alphaHex) {
  const c = String(hexColor || "").trim();
  if (/^#[0-9a-fA-F]{6}$/.test(c)) {
    return `${c}${alphaHex}`;
  }
  if (/^#[0-9a-fA-F]{3}$/.test(c)) {
    const r = c[1];
    const g = c[2];
    const b = c[3];
    return `#${r}${r}${g}${g}${b}${b}${alphaHex}`;
  }
  return c;
}

function truncStr(str, maxLen) {
  const s = String(str || "").trim();
  if (s.length <= maxLen) return s;
  return `${s.slice(0, Math.max(1, maxLen - 1))}…`;
}

function renderChunkedPillRows(items, maxCharsPerRow = 38, rowGap = 5, colGap = 5) {
  const rows = [];
  let curRow = [];
  let curChars = 0;
  for (const item of items) {
    if (!item || !item.el) continue;
    const w = Math.max(3, (item.chars || 6) + 2);
    if (curRow.length > 0 && curChars + w > maxCharsPerRow) {
      rows.push(curRow);
      curRow = [item.el];
      curChars = w;
    } else {
      curRow.push(item.el);
      curChars += w;
    }
  }
  if (curRow.length > 0) rows.push(curRow);
  return v_flex()
    .w_full()
    .flex_none()
    .gap(rowGap)
    .children(
      rows.map((r) =>
        h_flex().w_full().items_center().gap(colGap).overflow_hidden().children(r)
      )
    );
}

function priorityColor(prio, t) {
  const p = String(prio || "").toUpperCase();
  if (p.startsWith("P0") || p.startsWith("CRIT")) return t.blocked || t.ansi1 || "#e05252";
  if (p.startsWith("P1") || p.startsWith("HIGH")) return t.working || t.ansi3 || "#d97736";
  if (p.startsWith("P2") || p.startsWith("MED")) return t.accent || "#4574c4";
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

const AGENT_WORKER_PALETTE = [
  "#4c8bf5", // Cobalt Blue (Antigravity / primary)
  "#e07a5f", // Terracotta Coral (Claude)
  "#a855f7", // Electric Violet
  "#10b981", // Emerald Green (Codex / OpenCode)
  "#06b6d4", // Sky Cyan (Gemini)
  "#ec4899", // Rose Pink (Copilot)
  "#f59e0b", // Warm Amber
];

function agentBrandSpec(agentKind, rt, t) {
  const prog = rt && rt.agent && rt.agent.program ? rt.agent.program : null;
  const raw = String(
    agentKind || (rt && rt.agent && (rt.agent.app || prog?.key)) || "agent"
  ).toLowerCase();

  if (raw.includes("claude")) {
    return {
      key: "claude",
      label: "claude",
      icon: (prog && prog.icon) || "icons/claude.svg",
      tint: prog ? Boolean(prog.tint) : false,
      baseColor: "#e07a5f",
    };
  }
  if (raw.includes("agy") || raw.includes("antigravity")) {
    return {
      key: "agy",
      label: "agy",
      icon: (prog && prog.icon) || "icons/antigravity.svg",
      tint: prog ? Boolean(prog.tint) : false,
      baseColor: "#4c8bf5",
    };
  }
  if (raw.includes("codex")) {
    return {
      key: "codex",
      label: "codex",
      icon: (prog && prog.icon) || "icons/codex.svg",
      tint: prog ? Boolean(prog.tint) : false,
      baseColor: "#7c6ff6",
    };
  }
  if (raw.includes("opencode")) {
    return {
      key: "opencode",
      label: "opencode",
      icon: (prog && prog.icon) || "icons/opencode.svg",
      tint: prog ? Boolean(prog.tint) : true,
      baseColor: "#10b981",
    };
  }
  if (raw.includes("gemini")) {
    return {
      key: "gemini",
      label: "gemini",
      icon: (prog && prog.icon) || "icons/gemini.svg",
      tint: prog ? Boolean(prog.tint) : true,
      baseColor: "#06b6d4",
    };
  }
  if (raw.includes("copilot")) {
    return {
      key: "copilot",
      label: "copilot",
      icon: (prog && prog.icon) || "icons/copilot.svg",
      tint: prog ? Boolean(prog.tint) : true,
      baseColor: "#ec4899",
    };
  }
  return {
    key: raw || "agent",
    label: raw || "agent",
    icon: (prog && prog.icon) || "icons/agent.svg",
    tint: prog ? Boolean(prog.tint) : true,
    baseColor: t.accent || "#4c8bf5",
  };
}

function renderAgentIcon(spec, size, color) {
  if (!spec || !spec.icon) return null;
  if (spec.tint) {
    return svg(spec.icon).size(size).flex_none().text_color(color);
  }
  return image(spec.icon).w(size).h(size).flex_none();
}

function resolveTicketAgentVisual(ticket, allTickets, rt, t) {
  const claim = ticket && ticket.claim ? ticket.claim : null;
  const isDoneCol = ticket && ticket.column === "done";
  const hasActiveClaim =
    !isDoneCol &&
    claim &&
    (claim.state === "active" || claim.state === "review_ready");

  if (!hasActiveClaim && !(rt && rt.active && !isDoneCol)) {
    return null;
  }

  const kind = (claim && claim.agent_kind) || (rt && rt.agent && rt.agent.app) || "agent";
  const spec = agentBrandSpec(kind, rt, t);

  // If multiple active tickets share the same agent harness, give each active worker
  // a distinct hue after the primary brand color so parallel agents are easy to distinguish.
  const kindKey = String(kind || "").toLowerCase();
  const cache = RENDER_SNAPSHOT ? RENDER_SNAPSHOT.sameKind : null;
  let activeSameKind = cache && cache.get(kindKey);
  if (!activeSameKind) {
    activeSameKind = (allTickets || []).filter((tk) => {
      if (!tk || tk.column === "done") return false;
      const cl = tk.claim;
      if (!cl || (cl.state !== "active" && cl.state !== "review_ready")) return false;
      return String(cl.agent_kind || "").toLowerCase() === kindKey;
    });
    if (cache) cache.set(kindKey, activeSameKind);
  }

  let color = spec.baseColor;
  let workerIndex = 0;
  if (activeSameKind.length > 1) {
    const idx = activeSameKind.findIndex((tk) => tk.number === ticket.number);
    workerIndex = idx >= 0 ? idx : 0;
    if (workerIndex > 0) {
      const altPalette = AGENT_WORKER_PALETTE.filter(
        (c) => c.toLowerCase() !== spec.baseColor.toLowerCase()
      );
      color = altPalette[(workerIndex - 1) % altPalette.length] || spec.baseColor;
    }
  }

  const tabLabel =
    (rt && rt.agent && rt.agent.tab_name) ||
    (rt && rt.terminal && rt.terminal.name) ||
    (claim && claim.pane_id) ||
    "";

  return {
    ...spec,
    color,
    owner: (claim && claim.owner) || "local",
    tabLabel,
    workerIndex,
    totalSameKind: activeSameKind.length,
    isWorking: Boolean(rt && rt.agent && rt.agent.status === "working"),
  };
}

function parseTicketTitle(ticket) {
  const raw = String(ticket?.title || "").trim();
  let rest = raw;
  const jiraKeys = [];
  const scopes = [];
  const tagRe = /^\[([^\[\]]+)\]\s*/;
  const jiraRe = /^[A-Z][A-Z0-9]+-\d+$/;
  while (true) {
    const m = tagRe.exec(rest);
    if (!m) break;
    const tok = m[1].trim();
    rest = rest.slice(m[0].length).trim();
    if (jiraRe.test(tok)) {
      if (!jiraKeys.includes(tok)) jiraKeys.push(tok);
    } else if (tok && !scopes.includes(tok)) {
      scopes.push(tok);
    }
  }
  const targetRepo = ticket?.target_repo || ticket?.repo_alias || "";
  const filteredScopes = scopes.filter(
    (s) => s.toLowerCase() !== String(targetRepo).toLowerCase()
  );
  return {
    cleanTitle: rest || raw,
    jiraKeys,
    scopes: filteredScopes,
  };
}

function parseFacetLabel(rawLabel, t) {
  const str = String(rawLabel || "").trim();
  const colonIdx = str.indexOf(":");
  if (colonIdx <= 0) {
    return { prefix: "", text: str, color: t.muted, bg: t.background };
  }
  const prefix = str.slice(0, colonIdx).toLowerCase();
  const val = str.slice(colonIdx + 1).trim();
  if (prefix === "domain") {
    return {
      prefix,
      text: val,
      color: t.accent,
      bg: withAlpha(t.accent, "16") || t.background,
    };
  }
  if (prefix === "type") {
    const isBug = val === "bug" || val === "forensics";
    const c = isBug ? t.blocked || t.ansi1 : t.working || t.ansi3;
    return {
      prefix,
      text: val,
      color: c,
      bg: withAlpha(c, "16") || t.background,
    };
  }
  if (prefix === "release") {
    const shortRel = val
      .replace(/^([a-z0-9_]+)-/i, "$1 ")
      .replace(/\+[A-Za-z0-9._-]+$/, "");
    const c = t.done || t.ansi2;
    return {
      prefix,
      text: shortRel,
      color: c,
      bg: withAlpha(c, "14") || t.background,
    };
  }
  if (prefix === "trl" || prefix === "access") {
    return {
      prefix,
      text: val,
      color: t.dim,
      bg: t.background,
    };
  }
  return { prefix, text: str, color: t.muted, bg: t.background };
}

function shortBuildVersion(rawVer) {
  const s = String(rawVer || "").trim();
  if (!s) return "";
  return s.replace(/\+[A-Za-z0-9._-]+$/, "");
}

function compactAlphaLabel(rawVer) {
  const s = shortBuildVersion(rawVer);
  if (!s) return "";
  if (s === "unreleased") return "Unreleased";
  const m = /^(\d+\.\d+\.\d+)-alpha\.(\d+)$/i.exec(s);
  if (m) return `${m[1]}-α${m[2]}`;
  return s;
}

function trainAccentColor(trainMeta, idx, t) {
  const palette = [
    t.done || t.ansi2 || "#10b981",
    t.accent || "#4c8bf5",
    t.ansi5 || "#a855f7",
    t.working || t.ansi3 || "#d97736",
  ];
  const raw = String((trainMeta && trainMeta.color) || "").trim().toLowerCase();
  if (!raw) return palette[idx % palette.length];
  if (raw.startsWith("#")) return raw;
  if (raw === "green" || raw === "done") return t.done || t.ansi2 || "#10b981";
  if (raw === "cyan" || raw === "blue" || raw === "accent") return t.accent || "#4c8bf5";
  if (raw === "amber" || raw === "yellow" || raw === "orange") return t.working || t.ansi3 || "#d97736";
  if (raw === "magenta" || raw === "purple") return t.ansi5 || "#a855f7";
  if (raw === "red") return t.blocked || t.ansi1 || "#ef4444";
  return palette[idx % palette.length];
}

function getTrainMeta(trainId, releaseTrains) {
  const list = Array.isArray(releaseTrains) ? releaseTrains : [];
  const low = String(trainId || "").toLowerCase();
  for (let i = 0; i < list.length; i += 1) {
    const tr = list[i];
    if (!tr) continue;
    const aliases = Array.isArray(tr.aliases) ? tr.aliases.map((a) => String(a).toLowerCase()) : [];
    if (String(tr.id || "").toLowerCase() === low || aliases.includes(low)) {
      return { ...tr, _index: i };
    }
  }
  const titleName = String(trainId || "")
    .replace(/[-_]+/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
  return {
    id: low || "release",
    name: titleName || "Release",
    short_name: titleName || "Rel",
    display_name: titleName || "Release",
    color: "",
    _index: list.length,
  };
}

function resolveTicketBuilds(ticket, releaseTrains) {
  if (!ticket) {
    return {
      entries: [],
      buildsByTrain: {},
      isBaseline: false,
      isReleaseTicket: false,
      unreleasedTrains: [],
    };
  }
  const trainsList = Array.isArray(releaseTrains) ? releaseTrains : [];
  const setsByTrain = new Map();
  const addVer = (tid, ver) => {
    const v = String(ver || "").trim();
    if (!v) return;
    const meta = getTrainMeta(tid, trainsList);
    if (!setsByTrain.has(meta.id)) setsByTrain.set(meta.id, new Set());
    setsByTrain.get(meta.id).add(v);
  };

  if (ticket.builds && typeof ticket.builds === "object") {
    for (const [tid, vers] of Object.entries(ticket.builds)) {
      if (Array.isArray(vers)) {
        for (const v of vers) addVer(tid, v);
      }
    }
  }
  if (ticket.release_builds && typeof ticket.release_builds === "object") {
    for (const [tid, ver] of Object.entries(ticket.release_builds)) {
      addVer(tid, ver);
    }
  }

  let isBaseline = Boolean(ticket.is_baseline);
  const titleStr = String(ticket.title || "");
  const bodyStr = String(ticket.body || "");
  let isReleaseTicket =
    Boolean(ticket.is_release_ticket) ||
    titleStr.toLowerCase().includes("[release]") ||
    bodyStr.toLowerCase().includes("release-ticket:true");

  const rawLabels = Array.isArray(ticket.labels) ? ticket.labels : [];
  for (const raw of rawLabels) {
    const lbl = String(raw || "").trim();
    const low = lbl.toLowerCase();
    if (low === "release:baseline") {
      isBaseline = true;
      continue;
    }
    if (low.startsWith("release:")) {
      let matched = false;
      for (const tr of trainsList) {
        const prefs = Array.isArray(tr.label_prefixes) ? tr.label_prefixes : [`release:${tr.id}-`];
        for (const pref of prefs) {
          if (low.startsWith(String(pref).toLowerCase())) {
            addVer(tr.id, lbl.slice(pref.length));
            matched = true;
            break;
          }
        }
        if (matched) break;
      }
      if (!matched) {
        const rest = lbl.slice("release:".length);
        const dashIdx = rest.indexOf("-");
        if (dashIdx > 0) {
          const tok = rest.slice(0, dashIdx);
          const ver = rest.slice(dashIdx + 1).trim();
          if (/^\d+\.\d+\.\d+/.test(ver)) {
            addVer(tok, ver);
          }
        }
      }
    }
  }

  const relInRe = /Released-In:.*?\(\s*([A-Za-z0-9_-]+)\s*:\s*`?([0-9]+\.[0-9]+\.[0-9]+[^\s`)]*)`?\s*\)/gi;
  let m;
  while ((m = relInRe.exec(bodyStr)) !== null) {
    addVer(m[1], m[2]);
  }

  const orderedIds = [];
  for (const tr of trainsList) {
    if (setsByTrain.has(tr.id)) orderedIds.push(tr.id);
  }
  for (const tid of setsByTrain.keys()) {
    if (!orderedIds.includes(tid)) orderedIds.push(tid);
  }

  const entries = [];
  const buildsByTrain = {};
  for (const tid of orderedIds) {
    const vers = Array.from(setsByTrain.get(tid)).filter(Boolean);
    if (!vers.length) continue;
    const meta = getTrainMeta(tid, trainsList);
    buildsByTrain[tid] = vers[0];
    entries.push({
      train: tid,
      name: meta.name,
      shortName: meta.short_name,
      displayName: meta.display_name,
      color: meta.color,
      index: meta._index,
      version: vers[0],
      versions: vers,
    });
  }

  const unreleasedTrains = Array.isArray(ticket.unreleased_trains)
    ? [...ticket.unreleased_trains]
    : [];

  return {
    entries,
    buildsByTrain,
    isBaseline,
    isReleaseTicket,
    unreleasedTrains,
  };
}

function stripKanbanMeta(rawBody) {
  if (!rawBody) return "";
  let text = String(rawBody).replace(/<!--\s*herdr-kanban:v1\s+\{[\s\S]*?\}\s*-->/g, "").trim();
  text = text.replace(/\n---\s*\n\*\*Deposited via `herdr-kanban`\*\*[\s\S]*$/, "").trim();
  return text;
}

function linkifyKanbanRefs(markdown, defaultRepoAlias) {
  if (!markdown) return "";
  const lines = String(markdown).split("\n");
  let inFence = false;
  return lines
    .map((line) => {
      if (line.trim().startsWith("```")) {
        inFence = !inFence;
        return line;
      }
      if (inFence) return line;
      // Avoid replacing inside existing markdown links `[...](...)`
      return line.replace(
        /(^|[\s(])((?:[A-Za-z0-9_.-]+)?#\d+)\b/g,
        (full, pre, ref) => {
          const cleanRef = ref.startsWith("#")
            ? defaultRepoAlias
              ? `${defaultRepoAlias}${ref}`
              : ref.slice(1)
            : ref;
          return `${pre}[${ref}](kanban://issue/${cleanRef})`;
        }
      );
    })
    .join("\n");
}

function btn(id, label, t, opts = {}) {
  const { primary, danger, dangerOutline, selected, small, iconPath, onClick } = opts;
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
      : dangerOutline
      ? t.blocked || t.ansi1
      : selected
      ? t.accent
      : t.foreground;
  const borderCol = primary
    ? t.accent
    : danger || dangerOutline
    ? t.blocked || t.ansi1
    : selected
    ? t.accent
    : t.border;

  return Button.new(id)
    .px(small ? 7 : 10)
    .py(small ? 3 : 5)
    .rounded(6)
    .cursor_pointer()
    .bg(bg)
    .border_1()
    .border_color(borderCol)
    .text_size(small ? 11 : 12)
    .text_color(fg)
    .selected(Boolean(selected))
    .hover((s) =>
      s.bg(
        primary
          ? t.accent
          : danger || dangerOutline
          ? withAlpha(t.blocked || t.ansi1, "24") || t.hover
          : t.hover
      )
    )
    .when(Boolean(onClick), (el) => el.on_click(onClick))
    .child(
      iconPath
        ? h_flex()
            .items_center()
            .gap(5)
            .child(svg(iconPath).size(12).flex_none().text_color(fg))
            .when(Boolean(label), (r) => r.child(label))
        : label
    );
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
  const spaceTabIds = new Set((targetSpace?.tabs || []).map((tb) => tb.id));
  const spacePanes = allPanes.filter((pn) => spaceTabIds.has(pn.tab));
  const focusedInSpace = Boolean(targetSpace && f?.space === targetSpace.id);
  const activeTabId =
    (focusedInSpace && f?.tab && spaceTabIds.has(f.tab) ? f.tab : null) ||
    targetSpace?.tabs?.find((tb) => tb.active)?.id ||
    null;
  const activeTabPanes = activeTabId
    ? spacePanes.filter((pn) => pn.tab === activeTabId)
    : [];
  const focusedPane =
    focusedInSpace && f?.pane
      ? spacePanes.find((pn) => pn.pane === f.pane && pn.terminal)
      : null;

  const on = cleanTarget(
    p.on ||
      p.machine ||
      targetSpace?.machine ||
      (focusedInSpace && f?.pane === null && f.terminal?.includes("/")
        ? f.terminal.split("/")[0]
        : null)
  );

  const candidates = [];
  if (!overrideSpaceId && (p.cwd || p.folder)) {
    addUniqueCwd(candidates, p.cwd || p.folder);
  }
  if (!overrideSpaceId && focusedPane?.cwd) {
    addUniqueCwd(candidates, focusedPane.cwd);
  }
  for (const pn of activeTabPanes) {
    if (pn.cwd) addUniqueCwd(candidates, pn.cwd);
  }
  if (!overrideSpaceId && focusedInSpace && f?.cwd) {
    addUniqueCwd(candidates, f.cwd);
  }
  for (const pn of spacePanes) {
    if (pn.cwd) addUniqueCwd(candidates, pn.cwd);
  }
  if (candidates.length === 0 && targetSpace?.folder && targetSpace?.name !== "~") {
    addUniqueCwd(candidates, targetSpace.folder);
  }
  if (spaceId && cache[spaceId] && candidates.includes(cache[spaceId])) {
    const preferred = cache[spaceId];
    const idx = candidates.indexOf(preferred);
    if (idx > 0) {
      candidates.splice(idx, 1);
      candidates.unshift(preferred);
    }
  }

  const cwd = candidates[0] || null;
  return {
    cwd,
    candidates,
    on,
    spaceId,
    spaceName: targetSpace?.name || cwd || "Workspace",
  };
}

// Host lists (agents/terminals/panes) are cross-runtime calls that serialize the whole
// list; reading them once per card made large boards exceed the render time budget
// ("interrupted"). render() takes one snapshot per pass and every card reuses it.
let RENDER_SNAPSHOT = null;

function hostSnapshot() {
  if (RENDER_SNAPSHOT) return RENDER_SNAPSHOT;
  return {
    agents: agents() || [],
    terminals: terminals() || [],
    panes: panes() || [],
    sameKind: new Map(),
  };
}

function findTicketRuntime(ticket) {
  const snap = hostSnapshot();
  const allAgents = snap.agents;
  const allTerms = snap.terminals;
  const allPanes = snap.panes;

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
      quickFilter: "active", // "active" | "all" | "p0"
      compactEmpty: true,
      viewMode: "board", // "board" | "changelog" | "whats_new"
      changelogTrain: "all", // "all" | <train_id>
      changelogType: "all", // "all" | "bug" | "feature" | "forensics"
      changelogRanges: {}, // { [trainId]: { from: "", to: "" } }
      agentKind: "agy",
      overrideSpaceId: null,
      showDrawer: true,
      drawerTall: false,
      drawerHeight: 268,
      modalMode: null,
      resolvedCwdBySpace: {},
    });
    if (!bus.quickFilterMigratedV044) {
      bus.quickFilter = "active";
      bus.quickFilterMigratedV044 = true;
    }
    if (
      props()?.view === "board" ||
      props()?.view === "changelog" ||
      props()?.view === "whats_new"
    ) {
      bus.viewMode = props().view;
    }
    bus.resolvedCwdBySpace ??= {};
    bus.boardBySpace ??= {};
    bus.syncSeq ??= 0;
    bus.views.add(this);
    this.bus = bus;

    this.overrideSpaceId = bus.overrideSpaceId;
    const initTarget = resolveSpaceTarget(this.overrideSpaceId, bus.resolvedCwdBySpace);
    this.board = (initTarget.spaceId && bus.boardBySpace[initTarget.spaceId]) || null;
    this.noKanbanInSpace = false;
    this.loading = false;
    this.loadingTargetKey = "";
    this.refreshSeq = 0;
    this.lastNavSeq = 0;
    this.busyText = "";
    this.error = "";
    this.statusMessage = "";
    this.typing = false;

    this.lastTargetKey = "";
    this.repoFilter = bus.repoFilter;
    this.quickFilter = bus.quickFilter || "active";
    this.compactEmpty = bus.compactEmpty !== false;
    this.viewMode = bus.viewMode || "board";
    this.changelogTrain = bus.changelogTrain || "all";
    this.changelogType = bus.changelogType || "all";
    this.changelogRanges = { ...(bus.changelogRanges || {}) };
    this.agentKind = bus.agentKind;
    this.userPickedAgent = false;

    this.colIdx = 2; // In Progress column default when active
    this.rowByCol = { 0: 0, 1: 0, 2: 0, 3: 0, 4: 0, 5: 0 };
    this.selectedRef = null;
    this.showDrawer = bus.showDrawer !== false;
    this.drawerTall = Boolean(bus.drawerTall);
    this.drawerHeight = Number(bus.drawerHeight) || 268;
    if (this.board) {
      this.syncSelectionAfterLoad();
    }

    // 2D Board & Filter strip pan state
    this.panX = 0;
    this.panY = 0;
    this.filterPanX = 0;
    this.drag = null;
    this.justDragged = false;
    this.size = { width: 840, height: 740 };

    // Inline modal modes: null | "deposit" | "ask" | "triage"
    this.modalMode = bus.modalMode;
    this.depositPriority = "P1";
    this.depositRepo = "";
    this.depositReady = true;

    this.searchInput = InputState.new({ placeholder: "Filter tickets, #ref, @owner…" });
    this.depositTitleInput = InputState.new({ placeholder: "[JIRA-KEY][scope] Ticket title…" });
    this.depositBodyInput = InputState.new({ placeholder: "Markdown description or --depends-on #issue…" });
    this.askInput = InputState.new({ placeholder: "Relay message or question for this ticket's agent…" });

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

    if (!checking()) {
      this.consumeSharedNav(cx);
    }
    this.refresh(cx, false);

    cx.timer.every(BACKGROUND_POLL_MS, (cx) => {
      const backoffUntil = Math.max(
        this.rateLimitBackoffUntil || 0,
        this.bus?.rateLimitBackoffUntil || 0
      );
      if (Date.now() < backoffUntil) {
        return;
      }
      if (!this.loading && !this.busyText) {
        this.refresh(cx, true, false);
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
            this.panX = 0;
            this.panY = 0;
            this.refresh(cx, false, true);
          } else if (cmd === "center" || cmd?.action === "center") {
            this.panX = 0;
            this.panY = 0;
            this.zoom = 1;
            cx.notify();
          } else if (cmd === "whats_new" || cmd?.action === "whats_new") {
            this.syncBus({ viewMode: "whats_new" }, cx);
            this.refresh(cx, true);
          } else if (cmd === "changelog" || cmd?.action === "changelog") {
            this.syncBus({ viewMode: "changelog" }, cx);
          } else if (cmd === "board" || cmd?.action === "board") {
            this.syncBus({ viewMode: "board" }, cx);
          } else if (cmd === "summarize" || cmd?.action === "summarize") {
            this.syncBus({ viewMode: "whats_new" }, cx);
            this.synthesizeWhatsNew(cx);
          }
        }
      } catch {}
    });
  }

  consumeSharedNav(cx) {
    const nav = shared("kanban.nav");
    if (!nav || typeof nav !== "object") return false;
    const seq = Number(nav.seq || 0);
    if (!seq || seq <= this.lastNavSeq) return false;
    this.lastNavSeq = seq;
    if (
      nav.viewMode === "board" ||
      nav.viewMode === "changelog" ||
      nav.viewMode === "whats_new"
    ) {
      this.syncBus({ viewMode: nav.viewMode }, cx);
    }
    if (nav.selectedRef) {
      this.selectedRef = String(nav.selectedRef);
    }
    if (nav.center) {
      this.panX = 0;
      this.panY = 0;
      this.zoom = 1;
    }
    return Boolean(nav.refresh || nav.viewMode || nav.selectedRef);
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
      if (patch.quickFilter !== undefined) {
        v.quickFilter = patch.quickFilter;
        v.syncSelectionAfterLoad();
      }
      if (patch.compactEmpty !== undefined) {
        v.compactEmpty = patch.compactEmpty;
      }
      if (patch.viewMode !== undefined) {
        v.viewMode = patch.viewMode;
      }
      if (patch.changelogTrain !== undefined) v.changelogTrain = patch.changelogTrain;
      if (patch.changelogType !== undefined) v.changelogType = patch.changelogType;
      if (patch.changelogRanges !== undefined) v.changelogRanges = { ...patch.changelogRanges };
      if (patch.agentKind !== undefined) {
        v.agentKind = patch.agentKind;
        v.userPickedAgent = true;
      }
      if (patch.overrideSpaceId !== undefined) v.overrideSpaceId = patch.overrideSpaceId;
      if (patch.showDrawer !== undefined) v.showDrawer = patch.showDrawer;
      if (patch.drawerTall !== undefined) v.drawerTall = patch.drawerTall;
      if (patch.drawerHeight !== undefined) v.drawerHeight = patch.drawerHeight;
      if (patch.modalMode !== undefined) v.modalMode = patch.modalMode;
    }
    if (!checking()) {
      this.bus.syncSeq = (this.bus.syncSeq || 0) + 1;
      try {
        setShared("kanban.sync", this.bus.syncSeq);
      } catch {}
    }
    if (cx) cx.notify();
  }

  broadcastBoardState(patch, cx) {
    const targets = this.bus && this.bus.views ? Array.from(this.bus.views) : [this];
    for (const v of targets) {
      if (patch.board !== undefined) v.board = patch.board;
      if (patch.noKanbanInSpace !== undefined) v.noKanbanInSpace = patch.noKanbanInSpace;
      if (patch.activeCwd !== undefined) v.activeCwd = patch.activeCwd;
      if (patch.loading !== undefined) v.loading = patch.loading;
      if (patch.error !== undefined) v.error = patch.error;
      if (patch.statusMessage !== undefined) v.statusMessage = patch.statusMessage;
      if (patch.board !== undefined) {
        if (patch.board && patch.board.settings && patch.board.settings.default_agent_kind && !v.userPickedAgent) {
          v.agentKind = patch.board.settings.default_agent_kind;
        }
        if (patch.board && !v.depositRepo && patch.board.default_repo) {
          v.depositRepo = patch.board.default_repo;
        }
        v.syncSelectionAfterLoad();
      }
    }
    if (!checking() && this.bus) {
      this.bus.syncSeq = (this.bus.syncSeq || 0) + 1;
      try {
        setShared("kanban.sync", this.bus.syncSeq);
      } catch {}
    }
    if (cx) cx.notify();
  }

  async execKanban(args, targetOverride) {
    const tgt = targetOverride || this.target();
    const cwd = (targetOverride && targetOverride.cwd) || this.activeCwd || tgt.cwd;
    const peerKey = tgt.on || "local";
    if (!this._legacyPeers) {
      this._legacyPeers = new Set();
    }
    const buildArgs = (includeSurface) => {
      const fullArgs = includeSurface ? ["--surface", "gpui_mod"] : [];
      if (args.length > 0) {
        fullArgs.push(args[0]);
      }
      if (cwd) {
        fullArgs.push("--cwd", cwd);
      }
      if (args.length > 1) {
        fullArgs.push(...args.slice(1));
      }
      return fullArgs;
    };
    const opts = { cwd, on: tgt.on, env: { HERDR_KANBAN_SURFACE: "gpui_mod" } };
    if (this._legacyPeers.has(peerKey)) {
      return exec("herdr-kanban", buildArgs(false), opts);
    }
    const res = await exec("herdr-kanban", buildArgs(true), opts);
    if (
      res &&
      res.code !== 0 &&
      String(res.stderr || "").includes("unrecognized arguments: --surface")
    ) {
      this._legacyPeers.add(peerKey);
      return exec("herdr-kanban", buildArgs(false), opts);
    }
    return res;
  }

  refresh(cx, silent = false, forceFresh = false) {
    const backoffUntil = Math.max(
      this.rateLimitBackoffUntil || 0,
      this.bus?.rateLimitBackoffUntil || 0
    );
    if (silent && Date.now() < backoffUntil) {
      return;
    }
    const tgt = this.target();
    const candidates =
      Array.isArray(tgt.candidates) && tgt.candidates.length
        ? tgt.candidates
        : tgt.cwd
        ? [tgt.cwd]
        : [];
    const targetKey = `${tgt.on || "local"}:${tgt.spaceId || ""}:${tgt.cwd || ""}`;
    this.lastTargetKey = targetKey;
    const reqSeq = ++this.refreshSeq;

    if (!candidates.length) {
      this.broadcastBoardState(
        {
          board: null,
          noKanbanInSpace: true,
          loading: false,
          error: "",
          statusMessage: "",
        },
        cx
      );
      return;
    }
    this.loadingTargetKey = targetKey;
    if (!silent) {
      if (!checking() && props()?.part === "bar" && (!this.bus || this.bus.views.size <= 1)) {
        try {
          setShared("kanban.nav", { seq: Date.now(), refresh: true });
        } catch {}
      }
      this.broadcastBoardState(
        {
          loading: true,
          error: "",
          statusMessage: "Refreshing board…",
        },
        cx
      );
    }

    const boardArgs = forceFresh ? ["board", "--json", "--no-cache"] : ["board", "--json"];
    cx.spawn(async (cx) => {
      try {
        let lastRes = null;
        let chosenCwd = candidates[0];
        for (const candCwd of candidates) {
          chosenCwd = candCwd;
          let res = null;
          try {
            res = await this.execKanban(boardArgs, { ...tgt, cwd: candCwd });
          } catch {
            continue;
          }
          if (reqSeq !== this.refreshSeq) return;
          lastRes = res;
          if (res && res.code === 0) {
            if (tgt.spaceId && this.bus && this.bus.resolvedCwdBySpace) {
              this.bus.resolvedCwdBySpace[tgt.spaceId] = candCwd;
            }
            break;
          }
          const errOut = ((res && (res.stderr || res.stdout)) || "").toLowerCase();
          if (
            !errOut.includes(".herdr-kanban.toml") &&
            !errOut.includes("not inside a git repository")
          ) {
            break;
          }
        }

        if (reqSeq !== this.refreshSeq) return;

        const res = lastRes;
        if (!res || res.code !== 0) {
          const stderr = (res && (res.stderr || res.stdout)) || "Failed to load herdr-kanban board";
          let parsedErr = stderr.trim();
          try {
            const j = JSON.parse(stderr);
            if (j && j.error) parsedErr = j.error;
          } catch {}
          if (isRateLimitError(parsedErr)) {
            const streak = Math.max(this.rateLimitStreak || 0, this.bus?.rateLimitStreak || 0) + 1;
            const delayMs = Math.min(
              RATE_LIMIT_MAX_BACKOFF_MS,
              RATE_LIMIT_BASE_BACKOFF_MS * Math.pow(2, streak - 1)
            );
            const until = Date.now() + delayMs;
            this.rateLimitStreak = streak;
            this.rateLimitBackoffUntil = until;
            if (this.bus) {
              this.bus.rateLimitStreak = streak;
              this.bus.rateLimitBackoffUntil = until;
            }
          }
          const lowerErr = parsedErr.toLowerCase();
          const isNoBoard =
            lowerErr.includes(".herdr-kanban.toml") ||
            lowerErr.includes("not inside a git repository") ||
            lowerErr.includes("no such file or directory") ||
            lowerErr.includes("command not found");
          if (isNoBoard) {
            if (tgt.spaceId && this.bus?.boardBySpace) {
              delete this.bus.boardBySpace[tgt.spaceId];
            }
            this.broadcastBoardState(
              {
                board: null,
                noKanbanInSpace: true,
                error: "",
                loading: false,
                statusMessage: "",
              },
              cx
            );
          } else if (!silent || !this.board) {
            this.broadcastBoardState(
              {
                noKanbanInSpace: false,
                error: parsedErr,
                loading: false,
                statusMessage: "",
              },
              cx
            );
          } else {
            this.broadcastBoardState({ loading: false, statusMessage: "" }, cx);
          }
          return;
        }

        this.rateLimitStreak = 0;
        this.rateLimitBackoffUntil = 0;
        if (this.bus) {
          this.bus.rateLimitStreak = 0;
          this.bus.rateLimitBackoffUntil = 0;
        }
        const data = JSON.parse(res.stdout);
        if (this.bus && tgt.spaceId) {
          this.bus.boardBySpace ??= {};
          this.bus.boardBySpace[tgt.spaceId] = data;
        }
        this.broadcastBoardState(
          {
            board: data,
            noKanbanInSpace: false,
            activeCwd: chosenCwd,
            error: "",
            loading: false,
            statusMessage: "",
          },
          cx
        );
      } catch (err) {
        if (reqSeq !== this.refreshSeq) return;
        this.broadcastBoardState(
          {
            ...(!silent || !this.board
              ? { error: String(err && err.message ? err.message : err) }
              : {}),
            loading: false,
            statusMessage: "",
          },
          cx
        );
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
      if (this.quickFilter === "active") {
        if (t.column === "done") return false;
      } else if (this.quickFilter === "p0") {
        if (!String(t.priority || "").toUpperCase().startsWith("P0")) return false;
      }
      if (!q) return true;
      const ref = String(t.ref || `#${t.number}`).toLowerCase();
      const title = String(t.title || "").toLowerCase();
      const prio = String(t.priority || "").toLowerCase();
      const owner = t.claim && t.claim.owner ? `@${t.claim.owner.toLowerCase()}` : "";
      const labels = Array.isArray(t.labels) ? t.labels.join(" ").toLowerCase() : "";
      const binfo = resolveTicketBuilds(t, this.board && this.board.release_trains);
      const buildsStr = binfo.entries.map((e) => `${e.shortName} ${e.version}`).join(" ").toLowerCase();
      return (
        ref.includes(q) ||
        title.includes(q) ||
        prio.includes(q) ||
        owner.includes(q) ||
        labels.includes(q) ||
        buildsStr.includes(q)
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

  computeColumnLayout(byColOverride) {
    const byCol = byColOverride || this.ticketsByColumn();
    const viewW = Math.max(360, this.size?.width ?? 840);
    const widths = COLUMNS.map((col) => {
      const cnt = (byCol[col.id] || []).length;
      if (!this.compactEmpty) return UNIFORM_COL_WIDTH;
      return cnt === 0 ? EMPTY_COL_COMPACT_WIDTH : ACTIVE_COL_MIN_WIDTH;
    });

    const rawTotal = widths.reduce((a, b) => a + b, 0);
    if (rawTotal < viewW) {
      const activeIndices = COLUMNS.map((col, idx) =>
        (byCol[col.id] || []).length > 0 ? idx : -1
      ).filter((idx) => idx >= 0);

      if (activeIndices.length > 0) {
        const extraPerCol = Math.floor((viewW - rawTotal) / activeIndices.length);
        for (const idx of activeIndices) {
          widths[idx] = Math.min(ACTIVE_COL_MAX_WIDTH, widths[idx] + extraPerCol);
        }
      }
    }

    const offsets = [];
    let acc = 0;
    for (const w of widths) {
      offsets.push(acc);
      acc += w;
    }
    return { widths, offsets, totalW: acc, viewW };
  }

  syncSelectionAfterLoad() {
    const allTickets =
      (this.board && Array.isArray(this.board.tickets) && this.board.tickets) || [];
    const activeCount = allTickets.filter((t) => t.column !== "done").length;
    if (!this.bus?.userPickedQuickFilter) {
      const autoFilter = activeCount > 0 ? "active" : "all";
      if (this.quickFilter !== autoFilter) {
        this.quickFilter = autoFilter;
        if (this.bus) this.bus.quickFilter = autoFilter;
      }
    }
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
    const curColId = COLUMNS[this.colIdx]?.id || "in_progress";
    const curList = byCol[curColId] || [];
    if (curList.length === 0) {
      // Prefer In Progress / Ready / Review before Done
      const preferredOrder = [2, 1, 4, 3, 0, 5];
      const nonEmptyIdx = preferredOrder.find((idx) => (byCol[COLUMNS[idx].id] || []).length > 0);
      if (nonEmptyIdx !== undefined) {
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

  selectTicketByRef(rawRef, cx) {
    if (!this.board || !Array.isArray(this.board.tickets)) return;
    const clean = String(rawRef || "").replace(/^kanban:\/\/issue\//, "").trim();
    const numMatch = /(\d+)$/.exec(clean);
    const targetNum = numMatch ? Number(numMatch[1]) : -1;

    // Clear filters if ticket is hidden by current filter
    const byCol = this.ticketsByColumn();
    let foundCol = -1;
    let foundRow = -1;
    for (let c = 0; c < COLUMNS.length; c++) {
      const list = byCol[COLUMNS[c].id] || [];
      const r = list.findIndex(
        (t) =>
          t.cli_ref === clean ||
          t.ref === clean ||
          String(t.number) === clean ||
          t.number === targetNum
      );
      if (r >= 0) {
        foundCol = c;
        foundRow = r;
        break;
      }
    }

    if (foundCol < 0 && (this.repoFilter || this.quickFilter !== "all" || this.searchQuery)) {
      this.repoFilter = "";
      this.quickFilter = "all";
      this.searchQuery = "";
      this.searchInput.set_value("");
      this.syncBus({ repoFilter: "", quickFilter: "all", searchQuery: "" }, null);
      const allByCol = this.ticketsByColumn();
      for (let c = 0; c < COLUMNS.length; c++) {
        const list = allByCol[COLUMNS[c].id] || [];
        const r = list.findIndex(
          (t) =>
            t.cli_ref === clean ||
            t.ref === clean ||
            String(t.number) === clean ||
            t.number === targetNum
        );
        if (r >= 0) {
          foundCol = c;
          foundRow = r;
          break;
        }
      }
    }

    if (foundCol >= 0) {
      this.syncBus({ viewMode: "board", showDrawer: true }, null);
      this.selectColumnRow(foundCol, foundRow, cx, true);
    }
  }

  handleLinkClick(url, cx) {
    const str = String(url || "").trim();
    if (str.startsWith("kanban://issue/") || /^([A-Za-z0-9_.-]+)?#\d+$/.test(str)) {
      this.selectTicketByRef(str, cx);
      return;
    }
  }

  maxDrawerHeight() {
    const totalH = this.size?.height ?? 740;
    const chromeH = 74 + 24 + (this.modalMode ? 64 : 0);
    return Math.max(220, totalH - chromeH);
  }

  effectiveDrawerHeight() {
    if (!this.showDrawer) return 28;
    const maxH = this.maxDrawerHeight();
    if (this.drawerTall) return maxH;
    const target = Number(this.drawerHeight) || 268;
    return Math.max(116, Math.min(Math.max(140, maxH - 76), target));
  }

  boardViewportDims() {
    const byCol = this.ticketsByColumn();
    const { widths, offsets, totalW, viewW } = this.computeColumnLayout(byCol);
    const drawerH = this.effectiveDrawerHeight();
    const chromeH = 102 + drawerH + (this.modalMode ? 64 : 0);
    const viewH = Math.max(120, (this.size?.height ?? 740) - chromeH);
    let maxCards = 1;
    for (const col of COLUMNS) {
      const len = (byCol[col.id] || []).length;
      if (len > maxCards) maxCards = len;
    }
    const contentH = Math.max(viewH, maxCards * CARD_EST_HEIGHT + 40);
    const minPanX = Math.min(0, viewW - totalW);
    const minPanY = Math.min(0, viewH - contentH);
    return { viewW, viewH, totalW, contentH, minPanX, minPanY, widths, offsets };
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
    const { viewW, widths, offsets } = this.boardViewportDims();
    const colLeft = offsets[colIdx] || 0;
    const colW = widths[colIdx] || ACTIVE_COL_MIN_WIDTH;
    const targetCenter = colLeft + colW / 2;
    const desiredPanX = Math.round(viewW / 2 - targetCenter);
    this.setPan(desiredPanX, 0, cx);
  }

  ensureVisible(colIdx, rowIdx) {
    const { viewW, viewH, minPanX, minPanY, widths, offsets } = this.boardViewportDims();
    let nextX = this.panX;
    let nextY = this.panY;

    const colLeft = offsets[colIdx] || 0;
    const colRight = colLeft + (widths[colIdx] || ACTIVE_COL_MIN_WIDTH);
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

  startInspectorResize(e, cx) {
    cx.stop_propagation();
    this.handle.focus();
    if (e.button === "left" && (e.click_count ?? 1) >= 2) {
      if (!this.showDrawer) {
        this.syncBus({ showDrawer: true, drawerTall: false }, cx);
      } else {
        this.syncBus({ showDrawer: true, drawerTall: !this.drawerTall }, cx);
      }
      return;
    }
    this.justDragged = false;
    this.drag = {
      kind: "inspector",
      startX: e.position.x,
      startY: e.position.y,
      origHeight: this.effectiveDrawerHeight(),
      wasHidden: !this.showDrawer,
      moved: false,
    };
    cx.notify();
  }

  moveDrag(e, cx) {
    if (!this.drag) return;
    const dx = e.position.x - this.drag.startX;
    const dy = e.position.y - this.drag.startY;
    const threshold = this.drag.kind === "inspector" ? 3 : 5;
    if (!this.drag.moved && Math.hypot(dx, dy) > threshold) {
      this.drag.moved = true;
      this.justDragged = true;
    }
    if (!this.drag.moved) return;
    if (this.drag.kind === "inspector") {
      const rawH = Math.round(this.drag.origHeight - dy);
      const maxH = this.maxDrawerHeight();
      if (rawH < 72) {
        this.syncBus({ showDrawer: false, drawerTall: false }, cx);
      } else if (rawH >= maxH - 32) {
        this.syncBus({ showDrawer: true, drawerTall: true }, cx);
      } else {
        const clamped = Math.max(116, Math.min(Math.max(140, maxH - 76), rawH));
        this.syncBus(
          { showDrawer: true, drawerTall: false, drawerHeight: clamped },
          cx
        );
      }
    } else if (this.drag.kind === "filter") {
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
      if (rt.pane.space) run("space.select", { id: rt.pane.space });
      if (rt.pane.tab) run("tab.select", { id: rt.pane.tab });
      run("pane.focus", { pane: rt.pane.pane });
      this.statusMessage = `Focused pane for ${ticket.ref || "#" + ticket.number}`;
      cx.notify();
      return;
    }
    if (rt.agent) {
      if (rt.agent.space) run("space.select", { id: rt.agent.space });
      if (rt.agent.tab) run("tab.select", { id: rt.agent.tab });
      if (rt.agent.pane) run("pane.focus", { pane: rt.agent.pane });
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
        folder: lc.worktree_path,
      });
      this.statusMessage = `Opened worktree tab for ${ticket.ref || "#" + ticket.number}`;
      cx.notify();
      return;
    }
    notify({
      title: "Kanban",
      message: `No active terminal found for ${ticket.ref || "#" + ticket.number}`,
    });
  }

  claimAndDispatch(ticket, takeover, cx) {
    if (!ticket || this.busyText) return;
    const cliRef = ticket.cli_ref || String(ticket.number);
    const disp = ticket.ref || `#${ticket.number}`;
    const kind = this.agentKind || "agy";
    const tgt = this.target();
    const effectiveCwd = this.activeCwd || (this.bus && this.bus.lastValidCwd) || tgt.cwd;

    this.busyText = `Claiming & dispatching ${disp} (${kind})…`;
    this.error = "";
    cx.notify();

    cx.spawn(async (cx) => {
      try {
        const claimArgs = ["claim", cliRef, "--kind", kind, "--json"];
        if (takeover || ticket.stale) claimArgs.push("--takeover");
        const res = await this.execKanban(claimArgs, { ...tgt, cwd: effectiveCwd });
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
          folder: claim.worktree_path || effectiveCwd,
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

  synthesizeWhatsNew(cx) {
    if (this.busyText) return;
    const tgt = this.target();
    const kind = this.agentKind || "agy";
    const effectiveCwd = this.activeCwd || (this.bus && this.bus.lastValidCwd) || tgt.cwd;
    this.busyText = `Summarizing landed work with ${kind}…`;
    this.error = "";
    cx.notify();

    cx.spawn(async (cx) => {
      try {
        const res = await this.execKanban(
          ["whats-new", "--generate", "--agent", kind, "--json"],
          { ...tgt, cwd: effectiveCwd }
        );
        this.busyText = "";
        if (!res || res.code !== 0) {
          this.error = ((res && (res.stderr || res.stdout)) || "Failed to generate briefing").trim();
          cx.notify();
          return;
        }
        const digest = JSON.parse(res.stdout);
        if (this.board) {
          this.board.whats_new = digest;
        }
        const usedAgent = (digest.saved_summary && digest.saved_summary.agent_kind) || kind;
        this.statusMessage = `Summarized What's New briefing via ${usedAgent}`;
        cx.notify();
      } catch (err) {
        this.busyText = "";
        this.error = String(err && err.message ? err.message : err);
        cx.notify();
      }
    });
  }

  dispatchWhatsNewAgent(cx) {
    if (this.busyText) return;
    const tgt = this.target();
    const kind = this.agentKind || "agy";
    const effectiveCwd = this.activeCwd || (this.bus && this.bus.lastValidCwd) || tgt.cwd;
    this.busyText = `Opening ${kind} tab for What's New briefing…`;
    this.error = "";
    cx.notify();

    cx.spawn(async (cx) => {
      try {
        const res = await this.execKanban(
          ["whats-new", "--prompt", "--agent", kind, "--json"],
          { ...tgt, cwd: effectiveCwd }
        );
        this.busyText = "";
        if (!res || res.code !== 0) {
          this.error = ((res && (res.stderr || res.stdout)) || "Failed to build prompt").trim();
          cx.notify();
          return;
        }
        const payload = JSON.parse(res.stdout);
        const argv =
          Array.isArray(payload.agent_argv) && payload.agent_argv.length > 0
            ? payload.agent_argv
            : [kind];
        run("tab.new", {
          name: `What's New (${kind})`,
          space: tgt.spaceId,
          folder: payload.cwd || effectiveCwd,
          argv,
          task: payload.prompt || "",
        });
        this.statusMessage = `Opened ${kind} tab for What's New briefing`;
        cx.notify();
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
    this.runTicketAction(
      `Sent message to ${ticket.ref || "#" + ticket.number}`,
      [cmd, cliRef, msg],
      cx
    );
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
    const panStepX = 140;
    const panStepY = 104;
    if (key === "w") {
      const nextView = this.viewMode === "whats_new" ? "board" : "whats_new";
      this.syncBus({ viewMode: nextView }, cx);
      cx.stop_propagation();
    } else if (key === "g") {
      const nextView = this.viewMode === "changelog" ? "board" : "changelog";
      this.syncBus({ viewMode: nextView }, cx);
      cx.stop_propagation();
    } else if (key === "b") {
      this.syncBus({ viewMode: "board" }, cx);
      cx.stop_propagation();
    } else if (shift && (key === "h" || key === "H" || key === "left")) {
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
      this.syncBus({ viewMode: "board" }, null);
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
    } else if (key === "D" || (shift && key === "d")) {
      this.syncBus({ showDrawer: true, drawerTall: !this.drawerTall }, cx);
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
    } else if (key === "s") {
      if (this.viewMode === "whats_new") {
        this.synthesizeWhatsNew(cx);
      } else if (ticket) {
        this.claimAndDispatch(ticket, Boolean(ticket.stale), cx);
      }
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
      if (this.viewMode === "whats_new" || this.viewMode === "changelog") {
        this.syncBus({ viewMode: "board" }, cx);
      } else if (this.showDrawer) {
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
    const panelW = this.size?.width ?? 840;
    const isWide = panelW >= 620;
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
    const allTickets =
      (this.board && Array.isArray(this.board.tickets) && this.board.tickets) || [];
    const filteredCount = this.filteredTickets().length;
    const landedCount =
      (this.board && this.board.whats_new && this.board.whats_new.total_landed) ||
      ((byCol && byCol.done && byCol.done.length) || 0);
    const activeCount = allTickets.filter((tk) => tk.column !== "done").length;

    const repoCounts = {};
    for (const tk of allTickets) {
      if (this.quickFilter === "active" && tk.column === "done") continue;
      if (this.quickFilter === "p0" && tk.priority !== "P0") continue;
      const key = tk.target_repo || tk.repo_alias || tk.repo_name || "";
      if (key) repoCounts[key] = (repoCounts[key] || 0) + 1;
    }
    const repoAliases = [...rawRepoAliases]
      .filter((a) => {
        if (this.repoFilter === a) return true;
        if (this.quickFilter === "active" && activeCount > 0) {
          return (repoCounts[a] || 0) > 0;
        }
        return true;
      })
      .sort((a, b) => {
        const ca = repoCounts[a] || 0;
        const cb = repoCounts[b] || 0;
        if (cb !== ca) return cb - ca;
        return a.localeCompare(b);
      });

    const isPanned = this.panX !== 0 || this.panY !== 0;

    return v_flex()
      .w_full()
      .flex_none()
      .bg(t.chrome || t.surface)
      .border_b(1)
      .border_color(t.border)
      .child(
        // Row 1: Brand identity, View Switcher (Board | What's New), Search & Primary Actions
        h_flex()
          .w_full()
          .h(38)
          .flex_none()
          .items_center()
          .justify_between()
          .gap(8)
          .px(12)
          .child(
            h_flex()
              .items_center()
              .gap(7)
              .min_w(0)
              .flex_1()
              .child(svg("mods/herdr-kanban/kanban.svg").size(15).flex_none().text_color(t.accent))
              .child(
                div()
                  .font_semibold()
                  .text_size(13)
                  .text_color(t.foreground)
                  .truncate()
                  .child(boardTitle)
              )
              .when(Boolean(tgt.on), (d) =>
                d.child(
                  h_flex()
                    .flex_none()
                    .items_center()
                    .gap(4)
                    .px(6)
                    .py(2)
                    .rounded(5)
                    .bg(t.raised || t.surface)
                    .border_1()
                    .border_color(t.border)
                    .child(svg("mods/workspace-chrome/server.svg").size(10).text_color(t.accent))
                    .child(div().text_size(10.5).text_color(t.accent).child(tgt.on))
                )
              )
              .when(Boolean(branchBadge && isWide), (d) =>
                d.child(
                  h_flex()
                    .max_w(185)
                    .items_center()
                    .gap(4)
                    .px(6)
                    .py(2)
                    .rounded(5)
                    .bg(t.background)
                    .border_1()
                    .border_color(t.border)
                    .child(svg("mods/workspace-chrome/git-branch.svg").size(10).text_color(t.muted))
                    .child(div().text_size(10.5).text_color(t.muted).truncate().child(branchBadge))
                )
              )
          )
          .child(
            // Segmented View Switcher: Board vs What's New
            h_flex()
              .flex_none()
              .items_center()
              .gap(2)
              .p(2)
              .rounded(7)
              .bg(t.background)
              .border_1()
              .border_color(t.border)
              .child(
                h_flex()
                  .id("view-tab-board")
                  .items_center()
                  .gap(5)
                  .px(8)
                  .py(3)
                  .rounded(5)
                  .cursor_pointer()
                  .bg(this.viewMode === "board" ? t.selected : t.background)
                  .text_size(11.5)
                  .font_medium()
                  .text_color(this.viewMode === "board" ? t.foreground : t.muted)
                  .hover((s) => s.bg(this.viewMode === "board" ? t.selected : t.hover))
                  .on_click((_ev, cx) => this.syncBus({ viewMode: "board" }, cx))
                  .child(svg("mods/workspace-chrome/layers.svg").size(11).text_color(this.viewMode === "board" ? t.accent : t.muted))
                  .child("Board")
                  .when(allTickets.length > 0, (el) =>
                    el.child(
                      div()
                        .text_size(10)
                        .text_color(this.viewMode === "board" ? t.accent : t.dim)
                        .child(
                          this.repoFilter || this.searchQuery || this.quickFilter !== "all"
                            ? `${filteredCount}/${allTickets.length}`
                            : `${allTickets.length}`
                        )
                    )
                  )
              )
              .child(
                h_flex()
                  .id("view-tab-changelog")
                  .items_center()
                  .gap(5)
                  .px(8)
                  .py(3)
                  .rounded(5)
                  .cursor_pointer()
                  .bg(this.viewMode === "changelog" ? t.selected : t.background)
                  .text_size(11.5)
                  .font_medium()
                  .text_color(this.viewMode === "changelog" ? t.foreground : t.muted)
                  .hover((s) => s.bg(this.viewMode === "changelog" ? t.selected : t.hover))
                  .on_click((_ev, cx) => this.syncBus({ viewMode: "changelog" }, cx))
                  .child(
                    svg("mods/workspace-chrome/git-branch.svg")
                      .size(11)
                      .text_color(this.viewMode === "changelog" ? t.accent : t.muted)
                  )
                  .child("Changelog")
                  .when(
                    Boolean(
                      this.board &&
                        this.board.changelog &&
                        Array.isArray(this.board.changelog.builds) &&
                        this.board.changelog.builds.length > 0
                    ),
                    (el) =>
                      el.child(
                        div()
                          .px(4)
                          .rounded(3)
                          .bg(withAlpha(t.accent, "20") || t.surface)
                          .text_size(10)
                          .text_color(t.accent)
                          .child(String(this.board.changelog.builds.length))
                      )
                  )
              )
              .child(
                h_flex()
                  .id("view-tab-whats-new")
                  .items_center()
                  .gap(5)
                  .px(8)
                  .py(3)
                  .rounded(5)
                  .cursor_pointer()
                  .bg(this.viewMode === "whats_new" ? t.selected : t.background)
                  .text_size(11.5)
                  .font_medium()
                  .text_color(this.viewMode === "whats_new" ? t.foreground : t.muted)
                  .hover((s) => s.bg(this.viewMode === "whats_new" ? t.selected : t.hover))
                  .on_click((_ev, cx) => this.syncBus({ viewMode: "whats_new" }, cx))
                  .child(
                    svg("mods/herdr-kanban/sparkles.svg")
                      .size(11)
                      .text_color(this.viewMode === "whats_new" ? t.done || t.ansi2 : t.muted)
                  )
                  .child("What's New")
                  .when(landedCount > 0, (el) =>
                    el.child(
                      div()
                        .px(4)
                        .rounded(3)
                        .bg(withAlpha(t.done || t.ansi2, "20") || t.surface)
                        .text_size(10)
                        .text_color(t.done || t.ansi2)
                        .child(String(landedCount))
                    )
                  )
              )
          )
          .child(
            // Search & Action Controls
            h_flex()
              .items_center()
              .flex_none()
              .gap(6)
              .child(
                h_flex()
                  .id("search-box")
                  .w(isWide ? 168 : 124)
                  .h(26)
                  .px(7)
                  .items_center()
                  .gap(5)
                  .rounded(6)
                  .bg(t.background)
                  .border_1()
                  .border_color(this.searchQuery ? t.accent : t.border)
                  .on_mouse_down("left", (_e, cx) => cx.stop_propagation())
                  .child(svg("mods/workspace-chrome/search.svg").size(11).flex_none().text_color(t.dim))
                  .child(
                    div()
                      .flex_1()
                      .min_w(0)
                      .h_full()
                      .child(
                        Input.new(this.searchInput).h_full().text_size(11.5).text_color(t.foreground)
                      )
                  )
                  .when(Boolean(this.searchQuery), (el) =>
                    el.child(
                      div()
                        .id("clear-search")
                        .p(2)
                        .rounded(3)
                        .cursor_pointer()
                        .text_color(t.muted)
                        .hover((s) => s.text_color(t.foreground))
                        .on_click((_ev, cx) => {
                          this.searchInput.set_value("");
                          this.searchQuery = "";
                          this.syncBus({ searchQuery: "" }, cx);
                        })
                        .child(svg("mods/workspace-chrome/x.svg").size(10).text_color(t.muted))
                    )
                  )
              )
              .child(
                btn("btn-deposit", "Deposit", t, {
                  primary: true,
                  small: true,
                  iconPath: "mods/workspace-chrome/plus.svg",
                  onClick: (_ev, cx) => {
                    const nextMode = this.modalMode === "deposit" ? null : "deposit";
                    this.syncBus({ modalMode: nextMode }, cx);
                    if (nextMode === "deposit") this.depositTitleInput.focus();
                  },
                })
              )
              .child(
                btn("btn-refresh", this.loading ? "Refreshing…" : "", t, {
                  small: true,
                  selected: Boolean(this.loading),
                  iconPath: "mods/herdr-kanban/refresh.svg",
                  onClick: (_ev, cx) => this.refresh(cx, false),
                })
              )
          )
      )
      .when(Boolean(this.board && this.viewMode === "board"), (hdr) =>
        hdr.child(
          // Row 2: Scope Filters + Pannable Repository Filter Strip
          h_flex()
            .id("kanban-subbar")
            .w_full()
            .h(30)
            .flex_none()
            .items_center()
            .justify_between()
            .gap(8)
            .px(12)
            .bg(t.surface)
            .border_t(1)
            .border_color(t.border)
            .child(
              h_flex()
                .items_center()
                .flex_none()
                .gap(4)
                .children(
                  [
                    { id: "active", label: `Active (${activeCount})` },
                    { id: "all", label: `All (${allTickets.length})` },
                    { id: "p0", label: "P0" },
                  ].map((flt) => {
                    const on = this.quickFilter === flt.id;
                    return div()
                      .id(`qflt-${flt.id}`)
                      .px(7)
                      .py(2)
                      .rounded(5)
                      .cursor_pointer()
                      .bg(on ? t.selected : t.background)
                      .border_1()
                      .border_color(on ? t.accent : t.border)
                      .text_size(10.5)
                      .font_medium()
                      .text_color(on ? t.foreground : t.muted)
                      .hover((s) => s.bg(t.hover))
                      .on_click((_ev, cx) => {
                        if (this.bus) this.bus.userPickedQuickFilter = true;
                        this.syncBus({ quickFilter: flt.id }, cx);
                      })
                      .child(flt.label);
                  })
                )
                .when(isPanned, (el) =>
                  el.child(
                    div()
                      .id("btn-reset-pan")
                      .px(6)
                      .py(2)
                      .rounded(5)
                      .cursor_pointer()
                      .bg(t.raised || t.background)
                      .border_1()
                      .border_color(t.accent)
                      .text_size(10.5)
                      .text_color(t.accent)
                      .on_click((_ev, cx) => this.resetPan(cx))
                      .child("0 Center")
                  )
                )
            )
            .when(repoAliases.length > 1, (d) =>
              d
                .child(div().w(1).h(14).flex_none().bg(t.border))
                .child(
                  div()
                    .id("kanban-filter-viewport")
                    .relative()
                    .flex_1()
                    .min_w(100)
                    .h(24)
                    .overflow_hidden()
                    .cursor_grab()
                    .on_mouse_down("left", (e, cx) => this.startFilterDrag(e, cx))
                    .on_mouse_down("middle", (e, cx) => this.startFilterDrag(e, cx))
                    .on_scroll_wheel((e, cx) => {
                      const rawDelta =
                        Math.abs(e.delta?.x ?? 0) >= Math.abs(e.delta?.y ?? 0)
                          ? e.delta?.x ?? 0
                          : e.delta?.y ?? 0;
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
                            .px(7)
                            .py(2)
                            .rounded(5)
                            .cursor_pointer()
                            .bg(!this.repoFilter ? t.selected : t.background)
                            .border_1()
                            .border_color(!this.repoFilter ? t.accent : t.border)
                            .text_size(10.5)
                            .font_medium()
                            .text_color(!this.repoFilter ? t.foreground : t.muted)
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
                            return h_flex()
                              .id(`repo-filter-${alias}`)
                              .flex_none()
                              .items_center()
                              .gap(4)
                              .px(7)
                              .py(2)
                              .rounded(5)
                              .cursor_pointer()
                              .bg(active ? t.selected : t.background)
                              .border_1()
                              .border_color(active ? t.accent : t.border)
                              .hover((s) => s.bg(t.hover))
                              .on_click((_ev, cx) => {
                                if (this.justDragged) {
                                  this.justDragged = false;
                                  return;
                                }
                                this.syncBus({ repoFilter: active ? "" : alias }, cx);
                              })
                              .child(
                                div()
                                  .text_size(10.5)
                                  .text_color(active ? t.accent : cnt > 0 ? t.foreground : t.dim)
                                  .child(alias)
                              )
                              .when(cnt > 0, (c) =>
                                c.child(
                                  div()
                                    .px(4)
                                    .rounded(3)
                                    .bg(t.surface)
                                    .text_size(9.5)
                                    .text_color(active ? t.accent : t.muted)
                                    .child(String(cnt))
                                )
                              );
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
            .child(
              div().text_size(12).font_semibold().text_color(t.accent).child("Deposit New Ticket")
            )
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
                    repos.slice(0, 8).map((alias) => {
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
          v_flex()
            .w_full()
            .gap(6)
            .child(
              div()
                .id("dep-title-wrap")
                .w_full()
                .h(28)
                .px(8)
                .flex()
                .items_center()
                .rounded(6)
                .bg(t.background)
                .border_1()
                .border_color(t.border)
                .on_mouse_down("left", (_e, cx) => cx.stop_propagation())
                .child(
                  Input.new(this.depositTitleInput).h_full().text_size(12).text_color(t.foreground)
                )
            )
            .child(
              h_flex()
                .w_full()
                .items_center()
                .gap(8)
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
                    .child(
                      Input.new(this.depositBodyInput).h_full().text_size(12).text_color(t.foreground)
                    )
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
            .child(
              div().text_size(12).font_semibold().text_color(t.foreground).child(`Move ${disp}:`)
            )
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
              btn("tri-close", "Close", t, {
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
    const { cleanTitle, jiraKeys, scopes } = parseTicketTitle(ticket);
    const claim = ticket.claim || null;
    const isDoneCol = ticket.column === "done";
    const hasActiveClaim =
      !isDoneCol && claim && (claim.state === "active" || claim.state === "review_ready");
    const overlaps = Array.isArray(ticket.overlaps) ? ticket.overlaps : [];
    const unmetDeps = Array.isArray(ticket.unmet_deps) ? ticket.unmet_deps : [];
    const blocks = Array.isArray(ticket.blocks) ? ticket.blocks : [];
    const rawLabels = Array.isArray(ticket.labels) ? ticket.labels : [];
    const buildInfo = resolveTicketBuilds(ticket, this.board && this.board.release_trains);
    const visibleFacets = rawLabels
      .filter(
        (l) =>
          !l.startsWith("trl:") &&
          !l.startsWith("access:") &&
          !l.startsWith("release:")
      )
      .slice(0, 2)
      .map((l) => parseFacetLabel(l, t));
    const checkDone = ticket.checklist_done || 0;
    const checkTotal = ticket.checklist_total || 0;
    const touchedCount =
      claim && Array.isArray(claim.touched_files) ? claim.touched_files.length : 0;
    const rt = findTicketRuntime(ticket);
    const isDepBlocked = !isDoneCol && unmetDeps.length > 0;
    const blockedCol = t.blocked || t.ansi1 || "#e05252";
    const allTickets = (this.board && Array.isArray(this.board.tickets) && this.board.tickets) || [];
    const agentVis = resolveTicketAgentVisual(ticket, allTickets, rt, t);

    let agentStateColor = agentVis ? agentVis.color : t.accent;
    if (rt.agent) {
      const st = rt.agent.status || "idle";
      if (st === "blocked") agentStateColor = blockedCol;
      else if (st === "done") agentStateColor = t.done || t.ansi2;
    }

    const prioCol = priorityColor(ticket.priority, t);
    const repoTag =
      ticket.target_repo && ticket.target_repo !== ticket.repo_alias
        ? ticket.target_repo
        : scopes[0] || "";
    const hasTopAlert =
      !isDoneCol &&
      (overlaps.length > 0 ||
        Boolean(ticket.stale) ||
        (!isDepBlocked && blocks.length > 0));
    const showRepoTopRight = !hasTopAlert && Boolean(repoTag);
    const showJiraTopRight = !hasTopAlert && !showRepoTopRight && jiraKeys.length > 0;

    const cardFacetItems = [];
    let usedFacetChars = 0;
    if (hasTopAlert && repoTag) {
      const rtText = truncStr(repoTag, 13);
      usedFacetChars += rtText.length + 3;
      cardFacetItems.push(
        div()
          .id(`fc-repo-${ticket.number}`)
          .flex_none()
          .px(5)
          .py(1)
          .rounded(4)
          .bg(t.background)
          .border_1()
          .border_color(t.border)
          .text_size(10)
          .text_color(agentVis ? agentVis.color : t.accent)
          .child(rtText)
      );
    }
    for (let i = 0; i < visibleFacets.length; i++) {
      const fc = visibleFacets[i];
      const fcText = truncStr(fc.text, 14);
      if (cardFacetItems.length > 0 && usedFacetChars + fcText.length + 3 > 28) {
        break;
      }
      usedFacetChars += fcText.length + 3;
      cardFacetItems.push(
        div()
          .id(`fc-${ticket.number}-${i}`)
          .flex_none()
          .px(5)
          .py(1)
          .rounded(4)
          .bg(fc.bg)
          .text_size(10)
          .text_color(fc.color)
          .child(fcText)
      );
    }

    // Card surface, border, and left stripe color based on agent working or dependency lock
    let cardBg = isSelected ? t.selected : t.surface;
    let cardBorder = isSelected ? t.accent : t.border;
    let leftStripeColor = null;

    if (overlaps.length > 0) {
      cardBg = withAlpha(blockedCol, isSelected ? "24" : "14") || cardBg;
      cardBorder = blockedCol;
      leftStripeColor = blockedCol;
    } else if (agentVis) {
      cardBg = withAlpha(agentVis.color, isSelected ? "28" : "15") || cardBg;
      cardBorder = isSelected ? agentVis.color : withAlpha(agentVis.color, "88") || agentVis.color;
      leftStripeColor = agentVis.color;
    } else if (isDepBlocked) {
      cardBg = withAlpha(blockedCol, isSelected ? "20" : "0E") || t.background;
      cardBorder = isSelected ? blockedCol : withAlpha(blockedCol, "80") || blockedCol;
      leftStripeColor = withAlpha(blockedCol, "CC") || blockedCol;
    }

    return v_flex()
      .id(`card-${colIdx}-${ticket.cli_ref || ticket.number}`)
      .relative()
      .w_full()
      .flex_none()
      .gap(7)
      .py(11)
      .pr(11)
      .pl(leftStripeColor ? 14 : 11)
      .rounded(8)
      .overflow_hidden()
      .cursor_pointer()
      .bg(cardBg)
      .border_1()
      .when(isDepBlocked && !agentVis, (el) => el.border_dashed())
      .border_color(cardBorder)
      .hover((s) =>
        s.bg(
          agentVis
            ? withAlpha(agentVis.color, "24") || t.hover
            : isDepBlocked
            ? withAlpha(blockedCol, "18") || t.hover
            : isSelected
            ? t.selected
            : t.hover
        )
      )
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
      .when(Boolean(leftStripeColor), (el) =>
        el.child(
          div()
            .id(`stripe-${ticket.number}`)
            .absolute()
            .left(0)
            .top(0)
            .bottom(0)
            .w(4)
            .bg(leftStripeColor)
        )
      )
      .child(
        // Top row: Left = Issue Ref + Priority ONLY; Right = ONE status/repo pill (zero overlap!)
        h_flex()
          .w_full()
          .items_center()
          .justify_between()
          .gap(6)
          .overflow_hidden()
          .child(
            h_flex()
              .items_center()
              .gap(5)
              .flex_none()
              .child(
                div()
                  .flex_none()
                  .text_size(11)
                  .font_semibold()
                  .text_color(
                    agentVis
                      ? agentVis.color
                      : isDepBlocked
                      ? blockedCol
                      : isSelected
                      ? t.accent
                      : t.muted
                  )
                  .child(truncStr(disp, 14))
              )
              .when(Boolean(ticket.priority), (d) =>
                d.child(
                  div()
                    .flex_none()
                    .px(5)
                    .py(1)
                    .rounded(4)
                    .bg(withAlpha(prioCol, "18") || t.background)
                    .text_size(10)
                    .font_semibold()
                    .text_color(prioCol)
                    .child(ticket.priority)
                )
              )
          )
          .child(
            h_flex()
              .items_center()
              .flex_none()
              .gap(4)
              .when(overlaps.length > 0, (d) =>
                d.child(
                  div()
                    .flex_none()
                    .px(5)
                    .py(1)
                    .rounded(4)
                    .bg(withAlpha(blockedCol, "22") || t.background)
                    .text_size(9.5)
                    .font_semibold()
                    .text_color(blockedCol)
                    .child("OVERLAP")
                )
              )
              .when(overlaps.length === 0 && Boolean(ticket.stale), (d) =>
                d.child(
                  div()
                    .flex_none()
                    .px(5)
                    .py(1)
                    .rounded(4)
                    .bg(withAlpha(t.working || t.ansi3, "20") || t.background)
                    .text_size(9.5)
                    .font_semibold()
                    .text_color(t.working || t.ansi3)
                    .child("STALE")
                )
              )
              .when(
                overlaps.length === 0 && !ticket.stale && !isDepBlocked && !isDoneCol && blocks.length > 0,
                (d) =>
                  d.child(
                    div()
                      .flex_none()
                      .px(5)
                      .py(1)
                      .rounded(4)
                      .bg(withAlpha(t.done || t.ansi2, "18") || t.background)
                      .border_1()
                      .border_color(withAlpha(t.done || t.ansi2, "50") || t.border)
                      .text_size(9.5)
                      .font_semibold()
                      .text_color(t.done || t.ansi2)
                      .child(
                        `Unblocks #${blocks[0]}${
                          blocks.length > 1 ? " +" + (blocks.length - 1) : ""
                        }`
                      )
                  )
              )
              .when(showRepoTopRight, (d) =>
                d.child(
                  div()
                    .flex_none()
                    .px(5)
                    .py(1)
                    .rounded(4)
                    .bg(t.background)
                    .border_1()
                    .border_color(t.border)
                    .text_size(10)
                    .text_color(agentVis ? agentVis.color : t.accent)
                    .child(truncStr(repoTag, 15))
                )
              )
              .when(showJiraTopRight, (d) =>
                d.child(
                  div()
                    .flex_none()
                    .px(5)
                    .py(1)
                    .rounded(4)
                    .bg(t.background)
                    .text_size(9.5)
                    .text_color(t.dim)
                    .child(truncStr(jiraKeys[0], 13))
                )
              )
          )
      )
      .when(isDepBlocked, (d) =>
        // Full-width Dependency Lock Callout Banner (much stronger than a tiny pill!)
        d.child(
          h_flex()
            .id(`dep-lock-banner-${ticket.number}`)
            .w_full()
            .items_center()
            .justify_between()
            .gap(6)
            .px(8)
            .py(5)
            .rounded(6)
            .overflow_hidden()
            .bg(withAlpha(blockedCol, "1E") || t.background)
            .border_1()
            .border_color(withAlpha(blockedCol, "60") || blockedCol)
            .child(
              h_flex()
                .items_center()
                .flex_none()
                .gap(5)
                .child(svg("mods/herdr-kanban/lock.svg").size(11).flex_none().text_color(blockedCol))
                .child(
                  div()
                    .flex_none()
                    .text_size(10)
                    .font_semibold()
                    .text_color(blockedCol)
                    .child("BLOCKED BY")
                )
                .children(
                  unmetDeps.slice(0, 2).map((depNum) =>
                    div()
                      .id(`card-dep-chip-${ticket.number}-${depNum}`)
                      .flex_none()
                      .px(5)
                      .py(1)
                      .rounded(4)
                      .cursor_pointer()
                      .bg(t.background)
                      .border_1()
                      .border_color(withAlpha(blockedCol, "75") || blockedCol)
                      .text_size(10)
                      .font_semibold()
                      .text_color(t.foreground)
                      .hover((s) => s.bg(t.hover))
                      .on_click((ev, cx) => {
                        if (ev && ev.stop_propagation) ev.stop_propagation();
                        this.selectTicketByRef(String(depNum), cx);
                      })
                      .child(`#${depNum}`)
                  )
                )
            )
            .child(
              div()
                .flex_none()
                .px(4)
                .py(1)
                .rounded(3)
                .bg(withAlpha(blockedCol, "18") || t.surface)
                .text_size(9.5)
                .font_medium()
                .text_color(blockedCol)
                .child(`Wave ${ticket.wave || 1}`)
            )
        )
      )
      .child(
        // Multi-line wrapped clean human title (dimmed when blocked by dependencies)
        div()
          .w_full()
          .max_h(isDoneCol ? 33 : 50)
          .overflow_hidden()
          .whitespace_normal()
          .line_height(1.38)
          .text_size(12)
          .font_medium()
          .text_color(isDepBlocked && !isSelected ? t.muted : t.foreground)
          .child(cleanTitle)
      )
      .when(
        Boolean(
          isDoneCol ||
            buildInfo.entries.length > 0 ||
            buildInfo.unreleasedTrains.length > 0
        ),
        (d) => {
          const releaseTrains = (this.board && this.board.release_trains) || [];
          const warnCol = t.working || t.ansi3 || "#d97736";
          const buildPills = [];
          for (const entry of buildInfo.entries) {
            const bCol = trainAccentColor(entry, entry.index, t);
            const shortVer = shortBuildVersion(entry.version);
            buildPills.push(
              h_flex()
                .id(`card-bld-${entry.train}-${ticket.number}`)
                .flex_none()
                .items_center()
                .gap(3)
                .px(5)
                .py(1.5)
                .rounded(4)
                .bg(withAlpha(bCol, "1A") || t.background)
                .border_1()
                .border_color(withAlpha(bCol, "66") || bCol)
                .child(div().size(5).rounded_full().bg(bCol))
                .child(
                  div()
                    .text_size(9.5)
                    .font_semibold()
                    .text_color(bCol)
                    .child(`${entry.shortName} ${shortVer}`)
                )
            );
          }
          if (
            buildPills.length === 0 &&
            isDoneCol &&
            releaseTrains.length > 0 &&
            !buildInfo.isBaseline &&
            !buildInfo.isReleaseTicket
          ) {
            const trLabel =
              buildInfo.unreleasedTrains.length > 0
                ? buildInfo.unreleasedTrains
                    .map((trId) => getTrainMeta(trId, releaseTrains).short_name)
                    .join("+")
                : "Next";
            buildPills.push(
              h_flex()
                .id(`card-unrel-bld-${ticket.number}`)
                .flex_none()
                .items_center()
                .gap(3)
                .px(5)
                .py(1.5)
                .rounded(4)
                .bg(withAlpha(warnCol, "1A") || t.background)
                .border_1()
                .border_color(withAlpha(warnCol, "66") || warnCol)
                .child(div().size(5).rounded_full().bg(warnCol))
                .child(
                  div()
                    .text_size(9.5)
                    .font_semibold()
                    .text_color(warnCol)
                    .child(`Pending ${trLabel} Build`)
                )
            );
          } else if (buildPills.length === 0 && buildInfo.isBaseline) {
            buildPills.push(
              div()
                .id(`card-base-bld-${ticket.number}`)
                .flex_none()
                .px(5)
                .py(1)
                .rounded(4)
                .bg(t.background)
                .border_1()
                .border_color(t.border)
                .text_size(9.5)
                .text_color(t.dim)
                .child("Baseline")
            );
          }
          if (buildPills.length === 0) return d;
          return d.child(
            h_flex()
              .w_full()
              .items_center()
              .gap(5)
              .overflow_hidden()
              .children(buildPills)
          );
        }
      )
      .when(cardFacetItems.length > 0, (d) =>
        d.child(
          h_flex()
            .w_full()
            .items_center()
            .gap(5)
            .overflow_hidden()
            .children(cardFacetItems)
        )
      )
      .when(checkTotal > 0 || touchedCount > 0 || hasActiveClaim || Boolean(agentVis), (d) =>
        d.child(
          h_flex()
            .w_full()
            .pt(2)
            .items_center()
            .justify_between()
            .gap(6)
            .overflow_hidden()
            .child(
              h_flex()
                .items_center()
                .flex_none()
                .gap(5)
                .when(Boolean(agentVis), (c) =>
                  c.child(
                    h_flex()
                      .items_center()
                      .flex_none()
                      .gap(4)
                      .px(6)
                      .py(2.5)
                      .rounded(5)
                      .bg(withAlpha(agentVis.color, "22") || t.background)
                      .border_1()
                      .border_color(withAlpha(agentVis.color, "70") || t.border)
                      .when(Boolean(renderAgentIcon(agentVis, 12, agentVis.color)), (ic) =>
                        ic.child(renderAgentIcon(agentVis, 12, agentVis.color))
                      )
                      .child(
                        div()
                          .flex_none()
                          .text_size(10.5)
                          .font_semibold()
                          .text_color(t.foreground)
                          .child(`@${truncStr(agentVis.owner, 11)}`)
                      )
                      .child(
                        div()
                          .flex_none()
                          .px(4)
                          .py(0.5)
                          .rounded(3)
                          .bg(withAlpha(agentVis.color, "30") || t.surface)
                          .text_size(9.5)
                          .font_semibold()
                          .text_color(agentVis.color)
                          .child(
                            agentVis.totalSameKind > 1
                              ? `${agentVis.label} #${agentVis.workerIndex + 1}`
                              : agentVis.label
                          )
                      )
                      .when(Boolean(agentVis.tabLabel && checkTotal === 0), (tb) =>
                        tb.child(
                          div()
                            .flex_none()
                            .text_size(9.5)
                            .text_color(t.muted)
                            .child(`· ${truncStr(agentVis.tabLabel, 8)}`)
                        )
                      )
                      .when(agentVis.isWorking, (sp) =>
                        sp.child(
                          svg("mods/workspace-chrome/spinner.svg")
                            .size(10)
                            .flex_none()
                            .text_color(agentVis.color)
                            .spin(900)
                        )
                      )
                  )
                )
                .when(!agentVis && hasActiveClaim, (c) =>
                  c.child(
                    h_flex()
                      .items_center()
                      .flex_none()
                      .gap(5)
                      .px(6)
                      .py(2)
                      .rounded(5)
                      .bg(t.background)
                      .border_1()
                      .border_color(t.border)
                      .child(
                        div()
                          .size(6)
                          .flex_none()
                          .rounded_full()
                          .bg(rt.active ? agentStateColor : t.accent)
                      )
                      .child(
                        div()
                          .flex_none()
                          .text_size(10.5)
                          .text_color(t.foreground)
                          .child(`@${truncStr(claim.owner, 12)}`)
                      )
                      .when(Boolean(claim.agent_kind), (ag) =>
                        ag.child(
                          div()
                            .flex_none()
                            .px(4)
                            .rounded(3)
                            .bg(t.surface)
                            .text_size(9.5)
                            .text_color(t.accent)
                            .child(claim.agent_kind)
                        )
                      )
                  )
                )
            )
            .child(
              h_flex()
                .items_center()
                .flex_none()
                .gap(5)
                .when(checkTotal > 0, (c) =>
                  c.child(
                    h_flex()
                      .items_center()
                      .gap(4)
                      .child(
                        div()
                          .w(24)
                          .h(4)
                          .rounded(2)
                          .bg(t.background)
                          .overflow_hidden()
                          .child(
                            div()
                              .w(Math.round((checkDone / Math.max(1, checkTotal)) * 24))
                              .h_full()
                              .bg(
                                checkDone === checkTotal
                                  ? t.done || t.ansi2
                                  : agentVis
                                  ? agentVis.color
                                  : t.accent
                              )
                          )
                      )
                      .child(
                        div()
                          .text_size(10)
                          .text_color(
                            checkDone === checkTotal
                              ? t.done || t.ansi2
                              : agentVis
                              ? agentVis.color
                              : t.muted
                          )
                          .child(`${checkDone}/${checkTotal}`)
                      )
                  )
                )
                .when(touchedCount > 0 && checkTotal === 0, (c) =>
                  c.child(
                    h_flex()
                      .items_center()
                      .gap(3)
                      .child(svg("mods/workspace-chrome/files.svg").size(10).text_color(t.dim))
                      .child(
                        div()
                          .text_size(10)
                          .text_color(overlaps.length > 0 ? blockedCol : t.dim)
                          .child(`${touchedCount}`)
                      )
                  )
                )
            )
        )
      );
  }

  renderColumnHeader(col, colIdx, count, colWidth, t) {
    const isFocusedCol = this.colIdx === colIdx;
    const accent = columnAccent(col.id, t);
    const isCompact = colWidth <= EMPTY_COL_COMPACT_WIDTH + 12;

    return h_flex()
      .id(`col-hdr-${col.id}`)
      .w(colWidth)
      .h(34)
      .flex_none()
      .items_center()
      .justify_between()
      .px(isCompact ? 8 : 11)
      .bg(isFocusedCol ? t.selected : t.surface)
      .border_r(1)
      .border_b(1)
      .border_color(isFocusedCol ? accent : t.border)
      .cursor_pointer()
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
          .min_w(0)
          .child(
            div()
              .size(6)
              .rounded_full()
              .flex_none()
              .bg(count > 0 || isFocusedCol ? accent : t.dim)
          )
          .child(
            div()
              .text_size(isCompact ? 11 : 12)
              .font_semibold()
              .text_color(isFocusedCol ? t.foreground : count > 0 ? t.foreground : t.dim)
              .truncate()
              .child(isCompact ? col.short : col.title)
          )
      )
      .child(
        div()
          .flex_none()
          .px(5)
          .py(1)
          .rounded(4)
          .bg(count > 0 ? t.background : t.surface)
          .text_size(10)
          .font_medium()
          .text_color(count > 0 ? t.muted : t.dim)
          .child(String(count))
      );
  }

  renderColumnStack(col, colIdx, tickets, colWidth, selectedRef, t) {
    const isCompact = colWidth <= EMPTY_COL_COMPACT_WIDTH + 12;
    return v_flex()
      .id(`col-${col.id}`)
      .w(colWidth)
      .flex_none()
      .gap(8)
      .p(isCompact ? 6 : 9)
      .when(tickets.length === 0, (d) =>
        d.child(
          v_flex()
            .w_full()
            .py(18)
            .px(6)
            .rounded(7)
            .border_1()
            .border_dashed()
            .border_color(withAlpha(t.border, "80") || t.border)
            .items_center()
            .justify_center()
            .child(
              div()
                .text_size(10.5)
                .text_color(t.dim)
                .child(isCompact ? "Empty" : `No ${col.title.toLowerCase()} tickets`)
            )
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
    const { viewW, totalW, widths } = this.boardViewportDims();
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
                  this.renderColumnHeader(
                    col,
                    idx,
                    (byCol[col.id] || []).length,
                    widths[idx],
                    t
                  )
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
            h_flex()
              .id("kanban-lanes-bg")
              .absolute()
              .left(this.panX)
              .top(0)
              .h_full()
              .children(
                COLUMNS.map((col, idx) =>
                  div()
                    .id(`lane-bg-${col.id}`)
                    .w(widths[idx])
                    .h_full()
                    .flex_none()
                    .border_r(1)
                    .border_color(t.border)
                )
              )
          )
          .child(
            h_flex()
              .id("kanban-columns-track")
              .absolute()
              .left(this.panX)
              .top(this.panY)
              .items_start()
              .children(
                COLUMNS.map((col, idx) =>
                  this.renderColumnStack(
                    col,
                    idx,
                    byCol[col.id] || [],
                    widths[idx],
                    selectedRef,
                    t
                  )
                )
              )
          )
      );
  }

  renderWhatsNewView(t) {
    const wn = (this.board && this.board.whats_new) || {};
    const saved = wn.saved_summary || null;
    const contributors = Array.isArray(wn.contributors) ? wn.contributors : [];
    const recentLanded = Array.isArray(wn.recent_landed) ? wn.recent_landed : [];
    const byRepo = Array.isArray(wn.by_repo) ? wn.by_repo : [];
    const defaultRepo = (this.board && this.board.default_repo) || "";
    const mdRaw = (saved && saved.markdown) || wn.auto_markdown || "No landed tickets recorded yet.";
    const mdLinked = linkifyKanbanRefs(mdRaw, defaultRepo);
    const panelW = this.size?.width ?? 840;
    const isSplit = panelW >= 660;
    const agentChoices = (
      (this.board &&
        this.board.settings &&
        Array.isArray(this.board.settings.supported_agents) &&
        this.board.settings.supported_agents) ||
      DEFAULT_AGENTS
    ).slice(0, 4);

    return v_flex()
      .id("whats-new-root")
      .flex_1()
      .min_h(0)
      .w_full()
      .bg(t.background)
      .overflow_hidden()
      .child(
        // Top Briefing Action Bar
        h_flex()
          .w_full()
          .h(38)
          .flex_none()
          .items_center()
          .justify_between()
          .gap(8)
          .px(14)
          .overflow_hidden()
          .bg(t.surface)
          .border_b(1)
          .border_color(t.border)
          .child(
            h_flex()
              .items_center()
              .min_w(0)
              .flex_1()
              .overflow_hidden()
              .gap(8)
              .child(svg("mods/herdr-kanban/sparkles.svg").size(13).flex_none().text_color(t.done || t.ansi2))
              .child(
                div()
                  .text_size(12.5)
                  .font_semibold()
                  .text_color(t.foreground)
                  .truncate()
                  .child("What's New — Landed Work & Colleague Digest")
              )
              .when(Boolean(saved && saved.author), (el) =>
                el.child(
                  div()
                    .flex_none()
                    .px(6)
                    .py(1)
                    .rounded(4)
                    .bg(t.background)
                    .border_1()
                    .border_color(t.border)
                    .text_size(10)
                    .text_color(t.muted)
                    .child(
                      `by @${saved.author}${saved.agent_kind ? ":" + saved.agent_kind : ""}`
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
                        .id(`wn-agent-${ag}`)
                        .px(5)
                        .py(1)
                        .rounded(4)
                        .cursor_pointer()
                        .bg(active ? t.selected : t.background)
                        .text_size(10)
                        .text_color(active ? t.accent : t.muted)
                        .on_click((_e, cx) => this.syncBus({ agentKind: ag }, cx))
                        .child(ag);
                    })
                  )
              )
              .child(
                btn("wn-synthesize", `Summarize (${this.agentKind || "agy"})`, t, {
                  primary: true,
                  small: true,
                  iconPath: "mods/herdr-kanban/sparkles.svg",
                  onClick: (_ev, cx) => this.synthesizeWhatsNew(cx),
                })
              )
          )
      )
      .child(
        // Split Body on wide viewports (Left = Markdown Executive Briefing, Right = Colleague Cards & Landed Feed),
        // or unified vertical scroll on narrower split panes so the Executive Briefing always leads at the top
        (isSplit ? h_flex() : v_flex())
          .id("whats-new-body")
          .flex_1()
          .min_h(0)
          .w_full()
          .when(isSplit, (el) => el.overflow_hidden())
          .when(!isSplit, (el) => el.overflow_y_scrollbar())
          .child(
            // Left (or Top): Rich Markdown Executive Briefing
            v_flex()
              .id("whats-new-md-pane")
              .when(isSplit, (el) =>
                el
                  .flex_1()
                  .min_w(0)
                  .min_h(0)
                  .h_full()
                  .overflow_y_scrollbar()
                  .border_r(1)
                  .border_color(t.border)
              )
              .when(!isSplit, (el) => el.w_full().flex_none().pb(0))
              .p(14)
              .gap(10)
              .child(
                v_flex()
                  .w_full()
                  .flex_none()
                  .p(14)
                  .rounded(8)
                  .bg(t.surface)
                  .border_1()
                  .border_color(t.border)
                  .child(
                    TextView.markdown("whats-new-briefing-md", mdLinked)
                      .selectable(true)
                      .on_link_click((url, cx) => this.handleLinkClick(url, cx))
                  )
              )
          )
          .child(
            // Right (or Bottom): Colleagues & Recently Landed Cards
            v_flex()
              .id("whats-new-feed-pane")
              .w(isSplit ? Math.min(390, Math.round(panelW * 0.44)) : panelW)
              .flex_none()
              .when(isSplit, (el) => el.min_h(0).h_full().overflow_y_scrollbar())
              .p(14)
              .gap(12)
              .when(contributors.length > 0, (el) =>
                el.child(
                  v_flex()
                    .w_full()
                    .flex_none()
                    .gap(6)
                    .child(
                      div()
                        .text_size(11.5)
                        .font_semibold()
                        .text_color(t.muted)
                        .child("Colleagues & Coding Agents")
                    )
                    .children(
                      contributors.map((c, idx) =>
                        v_flex()
                          .id(`wn-contrib-${idx}`)
                          .w_full()
                          .flex_none()
                          .gap(5)
                          .p(10)
                          .rounded(7)
                          .bg(t.surface)
                          .border_1()
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
                                  .child(
                                    div()
                                      .size(7)
                                      .rounded_full()
                                      .bg(c.active_count > 0 ? t.working || t.ansi3 : t.done || t.ansi2)
                                  )
                                  .child(
                                    div()
                                      .text_size(12)
                                      .font_semibold()
                                      .text_color(t.foreground)
                                      .child(`@${c.owner}`)
                                  )
                                  .children(
                                    (c.agents || []).map((ag) =>
                                      div()
                                        .px(5)
                                        .py(1)
                                        .rounded(4)
                                        .bg(t.background)
                                        .text_size(10)
                                        .text_color(t.accent)
                                        .child(ag)
                                    )
                                  )
                              )
                              .child(
                                h_flex()
                                  .items_center()
                                  .gap(5)
                                  .child(
                                    div()
                                      .px(6)
                                      .py(1)
                                      .rounded(4)
                                      .bg(withAlpha(t.done || t.ansi2, "18") || t.background)
                                      .text_size(10)
                                      .font_semibold()
                                      .text_color(t.done || t.ansi2)
                                      .child(`${c.landed_count} landed`)
                                  )
                                  .when(c.active_count > 0, (a) =>
                                    a.child(
                                      div()
                                        .px(6)
                                        .py(1)
                                        .rounded(4)
                                        .bg(withAlpha(t.working || t.ansi3, "18") || t.background)
                                        .text_size(10)
                                        .font_semibold()
                                        .text_color(t.working || t.ansi3)
                                        .child(`${c.active_count} active`)
                                    )
                                  )
                              )
                          )
                          .when(Array.isArray(c.repos) && c.repos.length > 0, (r) =>
                            r.child(
                              renderChunkedPillRows(
                                c.repos.slice(0, 6).map((rp) => {
                                  const txt = truncStr(rp, 18);
                                  return {
                                    chars: txt.length,
                                    el: div()
                                      .flex_none()
                                      .px(5)
                                      .py(1)
                                      .rounded(4)
                                      .bg(t.background)
                                      .text_size(9.5)
                                      .text_color(t.muted)
                                      .child(txt),
                                  };
                                }),
                                40,
                                5,
                                5
                              )
                            )
                          )
                      )
                    )
                )
              )
              .when(byRepo.length > 0, (el) =>
                el.child(
                  v_flex()
                    .w_full()
                    .flex_none()
                    .gap(6)
                    .child(
                      div()
                        .text_size(11.5)
                        .font_semibold()
                        .text_color(t.muted)
                        .child("Landed by Repository")
                    )
                    .child(
                      renderChunkedPillRows(
                        byRepo.map((grp) => {
                          const txt = truncStr(grp.repo, 20);
                          return {
                            chars: txt.length + String(grp.landed_count).length + 3,
                            el: h_flex()
                              .id(`wn-repo-pill-${grp.repo}`)
                              .flex_none()
                              .items_center()
                              .gap(4)
                              .px(7)
                              .py(3)
                              .rounded(5)
                              .cursor_pointer()
                              .bg(t.surface)
                              .border_1()
                              .border_color(t.border)
                              .hover((s) => s.bg(t.hover))
                              .on_click((_ev, cx) => {
                                this.syncBus({ repoFilter: grp.repo, viewMode: "board" }, cx);
                              })
                              .child(div().text_size(10.5).text_color(t.foreground).child(txt))
                              .child(
                                div()
                                  .px(4)
                                  .rounded(3)
                                  .bg(t.background)
                                  .text_size(9.5)
                                  .text_color(t.done || t.ansi2)
                                  .child(String(grp.landed_count))
                              ),
                          };
                        }),
                        40,
                        6,
                        5
                      )
                    )
                )
              )
              .child(
                v_flex()
                  .w_full()
                  .flex_none()
                  .gap(6)
                  .child(
                    div()
                      .text_size(11.5)
                      .font_semibold()
                      .text_color(t.muted)
                      .child(`Recently Landed Tickets (${recentLanded.length})`)
                  )
                  .children(
                    recentLanded.slice(0, 20).map((item, idx) =>
                      v_flex()
                        .id(`wn-landed-${idx}`)
                        .w_full()
                        .flex_none()
                        .gap(4)
                        .p(9)
                        .rounded(7)
                        .cursor_pointer()
                        .bg(t.surface)
                        .border_1()
                        .border_color(t.border)
                        .hover((s) => s.bg(t.hover))
                        .on_click((_ev, cx) => this.selectTicketByRef(item.cli_ref, cx))
                        .child(
                          h_flex()
                            .w_full()
                            .items_center()
                            .justify_between()
                            .gap(6)
                            .child(
                              h_flex()
                                .items_center()
                                .gap(5)
                                .child(
                                  div()
                                    .text_size(11)
                                    .font_semibold()
                                    .text_color(t.done || t.ansi2)
                                    .child(item.ref)
                                )
                                .when(Boolean(item.priority), (p) =>
                                  p.child(
                                    div()
                                      .px(4)
                                      .rounded(3)
                                      .bg(t.background)
                                      .text_size(9.5)
                                      .font_semibold()
                                      .text_color(priorityColor(item.priority, t))
                                      .child(item.priority)
                                  )
                                )
                                .when(Boolean(item.target_repo), (rp) =>
                                  rp.child(
                                    div()
                                      .px(5)
                                      .rounded(3)
                                      .bg(t.background)
                                      .text_size(9.5)
                                      .text_color(t.accent)
                                      .child(item.target_repo)
                                  )
                                )
                            )
                            .child(
                              div()
                                .text_size(10)
                                .text_color(t.dim)
                                .child(
                                  `@${item.owner}${item.agent_kind ? ":" + item.agent_kind : ""}`
                                )
                            )
                        )
                        .child(
                          div()
                            .w_full()
                            .whitespace_normal()
                            .line_height(1.35)
                            .text_size(11.5)
                            .font_medium()
                            .text_color(t.foreground)
                            .child(item.clean_title || item.title)
                        )
                        .when(
                          Boolean(Array.isArray(item.releases) && item.releases.length > 0),
                          (relRow) =>
                            relRow.child(
                              h_flex()
                                .w_full()
                                .items_center()
                                .gap(5)
                                .overflow_hidden()
                                .children(
                                  item.releases.slice(0, 3).map((relStr, rIdx) => {
                                    const releaseTrains = (this.board && this.board.release_trains) || [];
                                    const strVal = String(relStr || "");
                                    const dashIdx = strVal.indexOf("-");
                                    const tok = dashIdx > 0 ? strVal.slice(0, dashIdx) : "";
                                    const ver = dashIdx > 0 ? strVal.slice(dashIdx + 1) : strVal;
                                    const meta = tok ? getTrainMeta(tok, releaseTrains) : null;
                                    const col = meta
                                      ? trainAccentColor(meta, meta._index, t)
                                      : t.done || t.ansi2;
                                    const cleanRel = meta
                                      ? `${meta.short_name} ${shortBuildVersion(ver)}`
                                      : shortBuildVersion(strVal);
                                    return div()
                                      .id(`wn-rel-${idx}-${rIdx}`)
                                      .flex_none()
                                      .px(5)
                                      .py(1)
                                      .rounded(4)
                                      .bg(withAlpha(col, "18") || t.background)
                                      .border_1()
                                      .border_color(withAlpha(col, "60") || col)
                                      .text_size(9.5)
                                      .font_semibold()
                                      .text_color(col)
                                      .child(cleanRel);
                                  })
                                )
                            )
                        )
                        .when(Boolean(item.summary_note), (sn) =>
                          sn.child(
                            div()
                              .w_full()
                              .max_h(34)
                              .overflow_hidden()
                              .whitespace_normal()
                              .line_height(1.35)
                              .text_size(10.5)
                              .text_color(t.muted)
                              .child(item.summary_note)
                          )
                        )
                    )
                  )
              )
          )
      );
  }

  jumpToBuildChangelog(train, version, cx) {
    if (!train || !version) return;
    const nextRanges = {
      ...(this.changelogRanges || {}),
      [train]: { from: version, to: version },
    };
    this.syncBus(
      {
        viewMode: "changelog",
        changelogTrain: train,
        changelogRanges: nextRanges,
      },
      cx
    );
  }

  toggleVersionRangeChip(train, version, versionList, cx) {
    const prev = (this.changelogRanges && this.changelogRanges[train]) || {};
    const curFrom = prev.from || "";
    const curTo = prev.to || "";
    const setRange = (from, to) => {
      const nextRanges = { ...(this.changelogRanges || {}), [train]: { from, to } };
      this.syncBus({ changelogRanges: nextRanges }, cx);
    };

    if (!curFrom && !curTo) {
      setRange(version, version);
      return;
    }
    if (curFrom === version && curTo === version) {
      setRange("", "");
      return;
    }
    const idxClick = versionList.indexOf(version);
    const idxFrom = curFrom ? versionList.indexOf(curFrom) : 0;
    if (idxClick < 0) return;

    if (curFrom === curTo) {
      const lo = Math.min(idxFrom, idxClick);
      const hi = Math.max(idxFrom, idxClick);
      setRange(versionList[lo], versionList[hi]);
    } else {
      setRange(version, version);
    }
  }

  renderChangelogView(t) {
    const cl = (this.board && this.board.changelog) || {};
    const trainsList = Array.isArray(cl.trains)
      ? cl.trains
      : Array.isArray(this.board && this.board.release_trains)
        ? this.board.release_trains
        : [];
    const trainVersionsMap = (cl.train_versions && typeof cl.train_versions === "object")
      ? cl.train_versions
      : {};
    const allBuilds = Array.isArray(cl.builds) ? cl.builds : [];
    const trainFilter = this.changelogTrain || "all";
    const typeFilter = this.changelogType || "all";
    const ranges = this.changelogRanges || {};
    const q = (this.searchQuery || "").toLowerCase();
    const repoFlt = (this.repoFilter || "").toLowerCase();
    const warnCol = t.working || t.ansi3 || "#d97736";

    const inRange = (ver, verList, rawFrom, rawTo) => {
      if (!verList.length) return true;
      const idx = verList.indexOf(ver);
      if (idx < 0) return true;
      const lo = rawFrom && verList.indexOf(rawFrom) >= 0 ? verList.indexOf(rawFrom) : 0;
      const hi =
        rawTo && verList.indexOf(rawTo) >= 0 ? verList.indexOf(rawTo) : verList.length - 1;
      const minIdx = Math.min(lo, hi);
      const maxIdx = Math.max(lo, hi);
      return idx >= minIdx && idx <= maxIdx;
    };

    const filteredBuilds = [];
    const uniqueTicketsMap = new Map();
    for (const bld of allBuilds) {
      if (trainFilter !== "all" && bld.train !== trainFilter) continue;
      const tVers = Array.isArray(trainVersionsMap[bld.train]) ? trainVersionsMap[bld.train] : [];
      const tRange = ranges[bld.train] || {};
      if (!inRange(bld.version, tVers, tRange.from || "", tRange.to || "")) {
        continue;
      }
      const rawItems = Array.isArray(bld.tickets) ? bld.tickets : [];
      const matchedItems = rawItems.filter((it) => {
        if (repoFlt && String(it.target_repo || "").toLowerCase() !== repoFlt) return false;
        if (typeFilter !== "all" && String(it.type || "").toLowerCase() !== typeFilter) {
          return false;
        }
        if (!q) return true;
        const bStr = Object.entries(it.release_builds || {})
          .map(([k, v]) => `${k} ${v}`)
          .join(" ");
        const hay = `${it.ref} #${it.number} ${it.title} ${it.target_repo} ${bStr} ${(
          it.labels || []
        ).join(" ")} @${it.owner || ""}`.toLowerCase();
        return hay.includes(q);
      });
      if ((q || repoFlt || typeFilter !== "all") && matchedItems.length === 0) {
        continue;
      }
      for (const it of matchedItems) {
        uniqueTicketsMap.set(it.number, it);
      }
      filteredBuilds.push({
        ...bld,
        matchedItems,
      });
    }

    const uniqueTickets = Array.from(uniqueTicketsMap.values());
    const totalBugs = uniqueTickets.filter((x) => x.type === "bug").length;
    const totalFeatures = uniqueTickets.filter((x) => x.type === "feature").length;
    const totalForensics = uniqueTickets.filter((x) => x.type === "forensics").length;
    const hasAnyRange = Object.values(ranges).some((r) => Boolean(r && (r.from || r.to)));
    const hasCustomRange = Boolean(
      hasAnyRange || trainFilter !== "all" || typeFilter !== "all"
    );

    const renderVersionSelectorRow = (trainKey, label, versions, rawFrom, rawTo, accentCol) => {
      if (!versions.length) return null;
      const loIdx = rawFrom && versions.indexOf(rawFrom) >= 0 ? versions.indexOf(rawFrom) : 0;
      const hiIdx =
        rawTo && versions.indexOf(rawTo) >= 0 ? versions.indexOf(rawTo) : versions.length - 1;
      const minIdx = Math.min(loIdx, hiIdx);
      const maxIdx = Math.max(loIdx, hiIdx);
      const isFiltered = Boolean(rawFrom || rawTo);
      const rangeText = isFiltered
        ? minIdx === maxIdx
          ? shortBuildVersion(versions[minIdx])
          : `${shortBuildVersion(versions[minIdx])} → ${shortBuildVersion(versions[maxIdx])}`
        : `All (${versions.length})`;

      return h_flex()
        .id(`cl-range-row-${trainKey}`)
        .w_full()
        .flex_none()
        .items_center()
        .gap(6)
        .px(14)
        .py(5)
        .bg(t.surface)
        .border_b(1)
        .border_color(t.border)
        .child(
          h_flex()
            .flex_none()
            .w(158)
            .items_center()
            .justify_between()
            .gap(4)
            .child(
              h_flex()
                .items_center()
                .gap(5)
                .child(div().size(6).rounded_full().bg(accentCol))
                .child(
                  div()
                    .text_size(11)
                    .font_semibold()
                    .text_color(t.foreground)
                    .child(label)
                )
            )
            .child(
              div()
                .px(5)
                .py(1)
                .rounded(4)
                .bg(withAlpha(accentCol, "18") || t.background)
                .text_size(9.5)
                .font_semibold()
                .text_color(accentCol)
                .child(truncStr(rangeText, 19))
            )
        )
        .child(
          renderChunkedPillRows(
            versions.map((ver, vIdx) => {
              const inSel = vIdx >= minIdx && vIdx <= maxIdx;
              const isEndpoint = isFiltered && (vIdx === minIdx || vIdx === maxIdx);
              const chipLbl =
                ver === "unreleased"
                  ? "Unreleased"
                  : shortBuildVersion(ver).replace(/^\d+\.\d+\.\d+-alpha\./, "α");
              return {
                chars: chipLbl.length + 1,
                el: div()
                  .id(`cl-ver-${trainKey}-${vIdx}`)
                  .flex_none()
                  .px(6)
                  .py(1.5)
                  .rounded(4)
                  .cursor_pointer()
                  .bg(
                    isEndpoint
                      ? withAlpha(accentCol, "32") || t.selected
                      : inSel && isFiltered
                      ? withAlpha(accentCol, "18") || t.selected
                      : t.background
                  )
                  .border_1()
                  .border_color(
                    isEndpoint
                      ? accentCol
                      : inSel && isFiltered
                      ? withAlpha(accentCol, "70") || accentCol
                      : t.border
                  )
                  .text_size(10)
                  .font_semibold()
                  .text_color(inSel ? (isFiltered ? accentCol : t.foreground) : t.dim)
                  .hover((s) => s.bg(t.hover))
                  .on_click((_ev, cx) =>
                    this.toggleVersionRangeChip(trainKey, ver, versions, cx)
                  )
                  .child(chipLbl),
              };
            }),
            82,
            4,
            4
          )
        );
    };

    return v_flex()
      .id("changelog-root")
      .flex_1()
      .min_h(0)
      .w_full()
      .bg(t.background)
      .overflow_hidden()
      .child(
        // Top Controls Bar: Train Filter + Type Filter + Reset
        h_flex()
          .w_full()
          .h(38)
          .flex_none()
          .items_center()
          .justify_between()
          .gap(8)
          .px(14)
          .bg(t.surface)
          .border_b(1)
          .border_color(t.border)
          .child(
            h_flex()
              .items_center()
              .gap(6)
              .child(
                div()
                  .text_size(12)
                  .font_semibold()
                  .text_color(t.foreground)
                  .child("Build Changelog & Fix Availability")
              )
              .child(
                h_flex()
                  .items_center()
                  .gap(3)
                  .p(2)
                  .rounded(6)
                  .bg(t.background)
                  .border_1()
                  .border_color(t.border)
                  .children(
                    [
                      { id: "all", label: "All Trains" },
                      ...trainsList.map((tr) => {
                        const vers = Array.isArray(tr.versions)
                          ? tr.versions
                          : Array.isArray(trainVersionsMap[tr.id])
                            ? trainVersionsMap[tr.id]
                            : [];
                        return { id: tr.id, label: `${tr.name || tr.id} (${vers.length})` };
                      }),
                    ].map((tr) => {
                      const active = trainFilter === tr.id;
                      return div()
                        .id(`cl-train-${tr.id}`)
                        .px(7)
                        .py(2)
                        .rounded(4)
                        .cursor_pointer()
                        .bg(active ? t.selected : t.background)
                        .text_size(10.5)
                        .font_medium()
                        .text_color(active ? t.accent : t.muted)
                        .on_click((_ev, cx) =>
                          this.syncBus({ changelogTrain: tr.id }, cx)
                        )
                        .child(tr.label);
                    })
                  )
              )
          )
          .child(
            h_flex()
              .items_center()
              .gap(5)
              .child(
                h_flex()
                  .items_center()
                  .gap(3)
                  .p(2)
                  .rounded(6)
                  .bg(t.background)
                  .border_1()
                  .border_color(t.border)
                  .children(
                    [
                      { id: "all", label: "All Types" },
                      { id: "bug", label: `Bugs (${totalBugs})` },
                      { id: "feature", label: `Features (${totalFeatures})` },
                      { id: "forensics", label: `Forensics (${totalForensics})` },
                    ].map((tp) => {
                      const active = typeFilter === tp.id;
                      return div()
                        .id(`cl-type-${tp.id}`)
                        .px(6)
                        .py(2)
                        .rounded(4)
                        .cursor_pointer()
                        .bg(active ? t.selected : t.background)
                        .text_size(10)
                        .font_medium()
                        .text_color(active ? t.foreground : t.muted)
                        .on_click((_ev, cx) =>
                          this.syncBus({ changelogType: tp.id }, cx)
                        )
                        .child(tp.label);
                    })
                  )
              )
              .when(hasCustomRange, (el) =>
                el.child(
                  btn("cl-reset-range", "Reset Range", t, {
                    small: true,
                    onClick: (_ev, cx) =>
                      this.syncBus(
                        {
                          changelogTrain: "all",
                          changelogType: "all",
                          changelogRanges: {},
                        },
                        cx
                      ),
                  })
                )
              )
          )
      )
      .children(
        trainsList
          .filter((tr) => trainFilter === "all" || trainFilter === tr.id)
          .map((tr, idx) => {
            const vers = Array.isArray(tr.versions)
              ? tr.versions
              : Array.isArray(trainVersionsMap[tr.id])
                ? trainVersionsMap[tr.id]
                : [];
            const r = ranges[tr.id] || {};
            return renderVersionSelectorRow(
              tr.id,
              tr.name || tr.id,
              vers,
              r.from || "",
              r.to || "",
              trainAccentColor(tr, idx, t)
            );
          })
          .filter(Boolean)
      )
      .child(
        // Scrollable Build-by-Build Feed
        v_flex()
          .id("changelog-scroll-body")
          .flex_1()
          .min_h(0)
          .w_full()
          .overflow_y_scrollbar()
          .p(14)
          .gap(12)
          .child(
            // Range & Search Summary Callout
            h_flex()
              .w_full()
              .flex_none()
              .items_center()
              .justify_between()
              .px(12)
              .py(8)
              .rounded(7)
              .bg(t.surface)
              .border_1()
              .border_color(q ? t.accent : t.border)
              .child(
                h_flex()
                  .items_center()
                  .gap(8)
                  .child(
                    div()
                      .text_size(11.5)
                      .font_semibold()
                      .text_color(t.foreground)
                      .child(
                        q
                          ? `Fix Lookup "${this.searchQuery}": ${uniqueTickets.length} matching ticket(s) across ${filteredBuilds.length} build(s)`
                          : `Showing ${uniqueTickets.length} unique ticket(s) across ${filteredBuilds.length} build(s)`
                      )
                  )
                  .child(
                    div()
                      .text_size(10.5)
                      .text_color(t.muted)
                      .child(
                        `(${totalBugs} bug fixes · ${totalFeatures} features · ${totalForensics} forensics — click two version chips above to compare any build range)`
                      )
                  )
              )
          )
          .when(filteredBuilds.length === 0, (el) =>
            el.child(
              v_flex()
                .w_full()
                .py(28)
                .items_center()
                .justify_center()
                .child(
                  div()
                    .text_size(12)
                    .text_color(t.dim)
                    .child("No builds or tickets match the current filter / version range.")
                )
            )
          )
          .children(
            filteredBuilds.map((bld, bIdx) => {
              const tMeta = getTrainMeta(bld.train, trainsList);
              const isUnrel = Boolean(bld.is_unreleased);
              const bCol = isUnrel
                ? warnCol
                : trainAccentColor(
                    { color: bld.train_color || tMeta.color },
                    tMeta._index,
                    t
                  );
              const rt = bld.release_ticket || null;
              const items = bld.matchedItems || [];
              const badgeTitle = `${String(bld.train_name || tMeta.name || bld.train).toUpperCase()} BUILD`;

              return v_flex()
                .id(`cl-build-card-${bIdx}`)
                .w_full()
                .flex_none()
                .rounded(8)
                .bg(t.surface)
                .border_1()
                .border_color(withAlpha(bCol, "66") || t.border)
                .overflow_hidden()
                .child(
                  // Build Section Header
                  h_flex()
                    .w_full()
                    .items_center()
                    .justify_between()
                    .gap(8)
                    .px(12)
                    .py(8)
                    .bg(withAlpha(bCol, "14") || t.chrome)
                    .border_b(1)
                    .border_color(t.border)
                    .child(
                      h_flex()
                        .items_center()
                        .gap(8)
                        .child(
                          div()
                            .px(6)
                            .py(2)
                            .rounded(4)
                            .bg(withAlpha(bCol, "24") || t.background)
                            .text_size(10.5)
                            .font_semibold()
                            .text_color(bCol)
                            .child(badgeTitle)
                        )
                        .child(
                          div()
                            .text_size(12.5)
                            .font_semibold()
                            .text_color(t.foreground)
                            .child(
                              isUnrel
                                ? `${bld.train_display} — Unreleased (Merged on Branch, Pending Next Build)`
                                : `${bld.train_display} ${bld.version}`
                            )
                        )
                        .when(Boolean(!isUnrel && bld.previous_short_version), (pv) =>
                          pv.child(
                            div()
                              .text_size(10.5)
                              .text_color(t.muted)
                              .child(`(since ${bld.previous_short_version})`)
                          )
                        )
                    )
                    .child(
                      h_flex()
                        .items_center()
                        .gap(6)
                        .when(Boolean(rt && rt.companion_version), (cp) =>
                          cp.child(
                            div()
                              .px(6)
                              .py(1.5)
                              .rounded(4)
                              .bg(t.background)
                              .border_1()
                              .border_color(t.border)
                              .text_size(10)
                              .text_color(t.accent)
                              .child(
                                `Companion: ${shortBuildVersion(rt.companion_version)}`
                              )
                          )
                        )
                        .when(Boolean(rt && rt.ref), (r) =>
                          r.child(
                            div()
                              .id(`cl-rt-link-${bIdx}`)
                              .px(6)
                              .py(1.5)
                              .rounded(4)
                              .cursor_pointer()
                              .bg(t.background)
                              .border_1()
                              .border_color(t.border)
                              .text_size(10)
                              .text_color(t.accent)
                              .hover((s) => s.bg(t.hover))
                              .on_click((_ev, cx) =>
                                this.selectTicketByRef(rt.cli_ref || rt.ref, cx)
                              )
                              .child(`Release ${rt.ref}`)
                          )
                        )
                        .child(
                          div()
                            .px(6)
                            .py(1.5)
                            .rounded(4)
                            .bg(t.background)
                            .text_size(10)
                            .font_semibold()
                            .text_color(t.muted)
                            .child(`${items.length} ticket(s)`)
                        )
                    )
                )
                .child(
                  // Tickets inside this Build
                  v_flex()
                    .w_full()
                    .p(10)
                    .gap(6)
                    .when(items.length === 0, (emptyEl) =>
                      emptyEl.child(
                        div()
                          .text_size(11)
                          .text_color(t.dim)
                          .child("Version bump / rebuild with 0 newly collected tickets.")
                      )
                    )
                    .children(
                      items.map((it, tIdx) => {
                        const isBug = it.type === "bug" || it.type === "forensics";
                        const typeCol = isBug
                          ? t.blocked || t.ansi1
                          : t.working || t.ansi3;
                        return h_flex()
                          .id(`cl-item-${bIdx}-${tIdx}`)
                          .w_full()
                          .items_center()
                          .justify_between()
                          .gap(8)
                          .px(9)
                          .py(6)
                          .rounded(6)
                          .cursor_pointer()
                          .bg(t.background)
                          .border_1()
                          .border_color(t.border)
                          .hover((s) => s.bg(t.hover))
                          .on_click((_ev, cx) => this.selectTicketByRef(it.cli_ref, cx))
                          .child(
                            h_flex()
                              .items_center()
                              .gap(6)
                              .min_w(0)
                              .flex_1()
                              .child(
                                div()
                                  .flex_none()
                                  .text_size(11)
                                  .font_semibold()
                                  .text_color(t.accent)
                                  .child(it.ref)
                              )
                              .when(Boolean(it.priority), (p) =>
                                p.child(
                                  div()
                                    .flex_none()
                                    .px(4)
                                    .py(0.5)
                                    .rounded(3)
                                    .bg(t.surface)
                                    .text_size(9.5)
                                    .font_semibold()
                                    .text_color(priorityColor(it.priority, t))
                                    .child(it.priority)
                                )
                              )
                              .child(
                                div()
                                  .flex_none()
                                  .px(5)
                                  .py(0.5)
                                  .rounded(3)
                                  .bg(withAlpha(typeCol, "18") || t.surface)
                                  .text_size(9.5)
                                  .text_color(typeCol)
                                  .child(it.type || "feature")
                              )
                              .when(Boolean(it.target_repo), (rp) =>
                                rp.child(
                                  div()
                                    .flex_none()
                                    .px(5)
                                    .py(0.5)
                                    .rounded(3)
                                    .bg(t.surface)
                                    .text_size(9.5)
                                    .text_color(t.muted)
                                    .child(it.target_repo)
                                )
                              )
                              .child(
                                div()
                                  .flex_1()
                                  .min_w(0)
                                  .text_size(11.5)
                                  .text_color(t.foreground)
                                  .truncate()
                                  .child(it.clean_title || it.title)
                              )
                          )
                          .child(
                            h_flex()
                              .items_center()
                              .flex_none()
                              .gap(4)
                              .children(
                                Object.entries(it.release_builds || {}).map(([trId, ver]) => {
                                  const meta = getTrainMeta(trId, trainsList);
                                  const col = trainAccentColor(meta, meta._index, t);
                                  return div()
                                    .px(5)
                                    .py(1)
                                    .rounded(4)
                                    .bg(withAlpha(col, "18") || t.surface)
                                    .border_1()
                                    .border_color(withAlpha(col, "60") || col)
                                    .text_size(9.5)
                                    .font_semibold()
                                    .text_color(col)
                                    .child(`${meta.short_name} ${shortBuildVersion(ver)}`);
                                })
                              )
                          );
                      })
                    )
                );
            })
          )
      );
  }

  renderInspector(ticket, t) {
    const panelW = this.size?.width ?? 840;
    const isSplit = panelW >= 540;
    const isResizing = Boolean(this.drag && this.drag.kind === "inspector");

    if (!this.showDrawer) {
      const dispCollapsed = ticket ? ticket.ref || `#${ticket.number}` : "";
      const cleanCollapsed = ticket
        ? parseTicketTitle(ticket).cleanTitle
        : "Detail pane hidden — drag up or click Show";
      const colLabelCollapsed = ticket
        ? COLUMN_TITLE_BY_ID[ticket.column] || ticket.column
        : "";
      const colColorCollapsed = ticket ? columnAccent(ticket.column, t) : t.dim;

      return h_flex()
        .id("inspector-collapsed-bar")
        .w_full()
        .h(28)
        .flex_none()
        .items_center()
        .justify_between()
        .gap(8)
        .px(12)
        .bg(t.chrome || t.surface)
        .border_t(2)
        .border_color(isResizing ? t.accent : t.border)
        .cursor_ns_resize()
        .hover((s) => s.bg(t.hover))
        .on_mouse_down("left", (e, cx) => this.startInspectorResize(e, cx))
        .on_click((_ev, cx) => {
          if (this.justDragged) {
            this.justDragged = false;
            return;
          }
          this.syncBus({ showDrawer: true, drawerTall: false }, cx);
        })
        .child(
          h_flex()
            .items_center()
            .gap(6)
            .min_w(0)
            .flex_1()
            .overflow_hidden()
            .child(
              div()
                .w(24)
                .h(3)
                .rounded(2)
                .flex_none()
                .bg(isResizing ? t.accent : t.muted)
            )
            .when(Boolean(ticket), (d) =>
              d
                .child(
                  div()
                    .flex_none()
                    .text_size(11)
                    .font_semibold()
                    .text_color(t.accent)
                    .child(dispCollapsed)
                )
                .child(
                  h_flex()
                    .flex_none()
                    .items_center()
                    .gap(4)
                    .px(5)
                    .py(1)
                    .rounded(4)
                    .bg(withAlpha(colColorCollapsed, "18") || t.background)
                    .child(div().size(5).rounded_full().bg(colColorCollapsed))
                    .child(
                      div()
                        .text_size(10)
                        .font_semibold()
                        .text_color(colColorCollapsed)
                        .child(colLabelCollapsed)
                    )
                )
                .when(Boolean(ticket && ticket.priority), (p) =>
                  p.child(
                    div()
                      .flex_none()
                      .px(5)
                      .py(1)
                      .rounded(4)
                      .bg(withAlpha(priorityColor(ticket.priority, t), "18") || t.background)
                      .text_size(10)
                      .font_semibold()
                      .text_color(priorityColor(ticket.priority, t))
                      .child(ticket.priority)
                  )
                )
            )
            .child(
              div()
                .flex_1()
                .min_w(0)
                .text_size(11.5)
                .text_color(ticket ? t.foreground : t.dim)
                .truncate()
                .child(cleanCollapsed)
            )
        )
        .child(
          h_flex()
            .flex_none()
            .items_center()
            .gap(5)
            .on_mouse_down("left", (_e, cx) => cx.stop_propagation())
            .child(
              btn("insp-collapsed-show", "Show", t, {
                small: true,
                onClick: (_ev, cx) => {
                  this.syncBus({ showDrawer: true, drawerTall: false }, cx);
                },
              })
            )
            .child(
              btn("insp-collapsed-full", "Full", t, {
                small: true,
                onClick: (_ev, cx) => {
                  this.syncBus({ showDrawer: true, drawerTall: true }, cx);
                },
              })
            )
        );
    }

    const drawerH = this.effectiveDrawerHeight();

    if (!ticket) {
      return v_flex()
        .w_full()
        .h(120)
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
            .child("Select a ticket card to inspect its Markdown description, checklist & actions.")
        );
    }

    const disp = ticket.ref || `#${ticket.number}`;
    const cliRef = ticket.cli_ref || String(ticket.number);
    const { cleanTitle, jiraKeys } = parseTicketTitle(ticket);
    const colLabel = COLUMN_TITLE_BY_ID[ticket.column] || ticket.column;
    const colColor = columnAccent(ticket.column, t);
    const claim = ticket.claim || null;
    const lc = ticket.local_claim || null;
    const isDoneTicket = ticket.column === "done";
    const hasClaim =
      !isDoneTicket && claim && (claim.state === "active" || claim.state === "review_ready");
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
    const prios =
      this.board && Array.isArray(this.board.priorities) && this.board.priorities.length
        ? this.board.priorities
        : DEFAULT_PRIORITIES;

    const defaultRepo = (this.board && this.board.default_repo) || "";
    const cleanBody = stripKanbanMeta(ticket.body) || "*No description provided.*";
    const mdBody = linkifyKanbanRefs(cleanBody, defaultRepo);
    const mdClaimNote =
      claim && claim.note ? linkifyKanbanRefs(String(claim.note), defaultRepo) : "";
    const allTickets = (this.board && Array.isArray(this.board.tickets) && this.board.tickets) || [];
    const agentVis = resolveTicketAgentVisual(ticket, allTickets, rt, t);
    const blockedCol = t.blocked || t.ansi1 || "#e05252";
    const isDepBlocked = !isDoneTicket && unmet.size > 0;
    const firstBlocker = isDepBlocked ? Array.from(unmet)[0] : null;
    const hasLeftPanel =
      !isDoneTicket &&
      Boolean(
        overlaps.length > 0 ||
          hasClaim ||
          checklist.length > 0 ||
          deps.length > 0 ||
          blocks.length > 0 ||
          touched.length > 0
      );

    const renderAgentSelector = () =>
      h_flex()
        .items_center()
        .flex_none()
        .gap(2)
        .p(2)
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
        );

    const leftActions = [];
    const rightActions = [];

    if (isDoneTicket) {
      // Done tickets are completed archive items — zero action bar clutter
    } else if (isDepBlocked) {
      leftActions.push(
        btn(
          "act-jump-blocker",
          `Locked by #${firstBlocker} — Jump to Blocker`,
          t,
          {
            small: true,
            dangerOutline: true,
            iconPath: "mods/herdr-kanban/lock.svg",
            onClick: (_ev, cx) => this.selectTicketByRef(String(firstBlocker), cx),
          }
        )
      );
    } else if (ticket.column === "triage") {
      leftActions.push(
        btn("act-promote", "Promote to Ready (t)", t, {
          primary: true,
          small: true,
          onClick: (_ev, cx) =>
            this.runTicketAction(
              `Promoted ${disp} to Ready`,
              ["triage", cliRef, "--ready", "--force"],
              cx
            ),
        }),
        h_flex()
          .items_center()
          .flex_none()
          .gap(2)
          .p(2)
          .rounded(5)
          .bg(t.background)
          .border_1()
          .border_color(t.border)
          .children(
            prios.map((p) => {
              const active = ticket.priority === p;
              return div()
                .id(`insp-prio-${p}`)
                .px(5)
                .py(1)
                .rounded(4)
                .cursor_pointer()
                .bg(active ? t.selected : t.background)
                .text_size(10)
                .font_semibold()
                .text_color(active ? priorityColor(p, t) : t.dim)
                .child(p)
                .on_click((_ev, cx) =>
                  this.runTicketAction(
                    `Set ${disp} priority to ${p}`,
                    ["triage", cliRef, "--priority", p],
                    cx
                  )
                );
            })
          ),
        btn("act-dispatch", `Dispatch (${this.agentKind}) (s)`, t, {
          small: true,
          iconPath: "mods/workspace-chrome/terminal.svg",
          onClick: (_ev, cx) => this.claimAndDispatch(ticket, false, cx),
        }),
        renderAgentSelector()
      );
    } else if (ticket.column === "in_progress" || hasClaim) {
      if (rt.active) {
        leftActions.push(
          btn("act-focus", "Jump to Agent (f)", t, {
            primary: true,
            small: true,
            iconPath: "mods/workspace-chrome/terminal.svg",
            onClick: (_ev, cx) => this.focusTicketAgent(ticket, cx),
          })
        );
      } else {
        leftActions.push(
          btn(
            "act-dispatch",
            ticket.stale
              ? `Takeover (${this.agentKind}) (s)`
              : `Resume Agent (${this.agentKind}) (s)`,
            t,
            {
              primary: true,
              small: true,
              iconPath: "mods/workspace-chrome/terminal.svg",
              onClick: (_ev, cx) =>
                this.claimAndDispatch(ticket, Boolean(ticket.stale), cx),
            }
          ),
          renderAgentSelector()
        );
        if (lc && lc.worktree_path) {
          leftActions.push(
            btn("act-focus", "Open Shell (f)", t, {
              small: true,
              onClick: (_ev, cx) => this.focusTicketAgent(ticket, cx),
            })
          );
        }
      }
      leftActions.push(
        btn("act-ask", "Steer Agent (c)", t, {
          small: true,
          selected: this.modalMode === "ask",
          onClick: (_ev, cx) => {
            this.modalMode = this.modalMode === "ask" ? null : "ask";
            if (this.modalMode === "ask") this.askInput.focus();
            cx.notify();
          },
        }),
        btn("act-approve", "Approve & Land (a)", t, {
          small: true,
          iconPath: "mods/workspace-chrome/check.svg",
          onClick: (_ev, cx) =>
            this.runTicketAction(`Landing ${disp}`, ["done", cliRef, "--approve"], cx),
        })
      );
      if (hasClaim) {
        rightActions.push(
          btn("act-release", "Unclaim", t, {
            dangerOutline: true,
            small: true,
            onClick: (_ev, cx) =>
              this.runTicketAction(`Unclaimed ${disp}`, ["release", cliRef], cx),
          })
        );
      }
    } else if (ticket.column === "review") {
      leftActions.push(
        btn("act-approve", "Approve & Land (a)", t, {
          primary: true,
          small: true,
          iconPath: "mods/workspace-chrome/check.svg",
          onClick: (_ev, cx) =>
            this.runTicketAction(`Landing ${disp}`, ["done", cliRef, "--approve"], cx),
        })
      );
      if (rt.active || (lc && lc.worktree_path)) {
        leftActions.push(
          btn("act-focus", rt.active ? "Jump to Agent (f)" : "Open Shell (f)", t, {
            small: true,
            onClick: (_ev, cx) => this.focusTicketAgent(ticket, cx),
          })
        );
      }
      leftActions.push(
        btn("act-ask", "Steer Agent (c)", t, {
          small: true,
          selected: this.modalMode === "ask",
          onClick: (_ev, cx) => {
            this.modalMode = this.modalMode === "ask" ? null : "ask";
            if (this.modalMode === "ask") this.askInput.focus();
            cx.notify();
          },
        })
      );
      if (hasClaim) {
        rightActions.push(
          btn("act-release", "Unclaim", t, {
            dangerOutline: true,
            small: true,
            onClick: (_ev, cx) =>
              this.runTicketAction(`Unclaimed ${disp}`, ["release", cliRef], cx),
          })
        );
      }
    } else {
      // Ready or Blocked (manual)
      leftActions.push(
        btn("act-dispatch", `Dispatch (${this.agentKind}) (s)`, t, {
          primary: true,
          small: true,
          iconPath: "mods/workspace-chrome/terminal.svg",
          onClick: (_ev, cx) => this.claimAndDispatch(ticket, false, cx),
        }),
        renderAgentSelector()
      );
    }

    const showActionRow = leftActions.length > 0 || rightActions.length > 0;
    const topBorderColor = isResizing
      ? t.accent
      : agentVis
      ? agentVis.color
      : isDepBlocked
      ? blockedCol
      : t.border;

    return v_flex()
      .id("ticket-inspector")
      .w_full()
      .when(this.drawerTall, (el) => el.flex_1().h_full().min_h(0))
      .when(!this.drawerTall, (el) => el.h(drawerH).min_h(0).flex_none())
      .bg(t.surface)
      .border_t(2)
      .border_color(topBorderColor)
      .child(
        // Interactive Drag Resize Handle (drag up/down to resize, double-click to toggle Full Expand)
        h_flex()
          .id("inspector-resize-handle")
          .w_full()
          .h(7)
          .flex_none()
          .items_center()
          .justify_center()
          .bg(t.chrome || t.surface)
          .cursor_ns_resize()
          .hover((s) => s.bg(t.hover))
          .on_mouse_down("left", (e, cx) => this.startInspectorResize(e, cx))
          .child(
            div()
              .w(38)
              .h(3)
              .rounded(2)
              .bg(isResizing ? t.accent : t.muted)
          )
      )
      .child(
        // Contextual Inspector Header (1 row for Done tickets, 2 rows for active orchestration)
        v_flex()
          .w_full()
          .flex_none()
          .gap(6)
          .px(12)
          .pb(7)
          .pt(2)
          .bg(t.chrome || t.surface)
          .border_b(1)
          .border_color(t.border)
          .child(
            // Row 1: Issue Ref + Clickable Column/Priority Override Pills + Clean Title + Status Badge + Full / Hide
            h_flex()
              .w_full()
              .items_center()
              .justify_between()
              .gap(8)
              .overflow_hidden()
              .cursor_ns_resize()
              .on_mouse_down("left", (e, cx) => this.startInspectorResize(e, cx))
              .child(
                h_flex()
                  .items_center()
                  .gap(6)
                  .min_w(0)
                  .flex_1()
                  .overflow_hidden()
                  .child(
                    div()
                      .flex_none()
                      .text_size(12)
                      .font_semibold()
                      .text_color(agentVis ? agentVis.color : isDepBlocked ? blockedCol : t.accent)
                      .child(disp)
                  )
                  .child(
                    h_flex()
                      .id("insp-col-pill")
                      .flex_none()
                      .items_center()
                      .gap(4)
                      .px(6)
                      .py(1)
                      .rounded(4)
                      .cursor_pointer()
                      .bg(withAlpha(colColor, "18") || t.background)
                      .when(this.modalMode === "triage", (el) =>
                        el.border_1().border_color(t.accent)
                      )
                      .on_mouse_down("left", (_e, cx) => cx.stop_propagation())
                      .on_click((_ev, cx) => {
                        this.modalMode = this.modalMode === "triage" ? null : "triage";
                        cx.notify();
                      })
                      .child(div().size(5).rounded_full().bg(colColor))
                      .child(
                        div()
                          .text_size(10.5)
                          .font_semibold()
                          .text_color(colColor)
                          .child(colLabel)
                      )
                  )
                  .when(Boolean(ticket.priority), (d) =>
                    d.child(
                      div()
                        .id("insp-prio-pill")
                        .flex_none()
                        .px(5)
                        .py(1)
                        .rounded(4)
                        .cursor_pointer()
                        .bg(withAlpha(priorityColor(ticket.priority, t), "18") || t.background)
                        .text_size(10.5)
                        .font_semibold()
                        .text_color(priorityColor(ticket.priority, t))
                        .on_mouse_down("left", (_e, cx) => cx.stop_propagation())
                        .on_click((_ev, cx) => {
                          this.modalMode = this.modalMode === "triage" ? null : "triage";
                          cx.notify();
                        })
                        .child(ticket.priority)
                    )
                  )
                  .when(Boolean(ticket.target_repo), (d) =>
                    d.child(
                      div()
                        .max_w(110)
                        .px(6)
                        .py(1)
                        .rounded(4)
                        .bg(t.background)
                        .border_1()
                        .border_color(t.border)
                        .text_size(10.5)
                        .text_color(t.accent)
                        .truncate()
                        .child(ticket.target_repo)
                    )
                  )
                  .child(
                    div()
                      .flex_1()
                      .min_w(0)
                      .text_size(12.5)
                      .font_semibold()
                      .text_color(t.foreground)
                      .truncate()
                      .child(cleanTitle)
                  )
              )
              .child(
                h_flex()
                  .flex_none()
                  .items_center()
                  .gap(5)
                  .on_mouse_down("left", (_e, cx) => cx.stop_propagation())
                  .when(isDepBlocked, (d) =>
                    d.child(
                      h_flex()
                        .flex_none()
                        .items_center()
                        .gap(4)
                        .px(6)
                        .py(1)
                        .rounded(4)
                        .bg(withAlpha(blockedCol, "20") || t.background)
                        .border_1()
                        .border_color(withAlpha(blockedCol, "70") || blockedCol)
                        .child(svg("mods/herdr-kanban/lock.svg").size(10).text_color(blockedCol))
                        .child(
                          div()
                            .text_size(10)
                            .font_semibold()
                            .text_color(blockedCol)
                            .child(
                              `Blocked by ${Array.from(unmet)
                                .slice(0, 3)
                                .map((n) => "#" + n)
                                .join(", ")}`
                            )
                        )
                    )
                  )
                  .when(Boolean(agentVis), (d) =>
                    d.child(
                      h_flex()
                        .flex_none()
                        .items_center()
                        .gap(4)
                        .px(6)
                        .py(1)
                        .rounded(4)
                        .bg(withAlpha(agentVis.color, "22") || t.background)
                        .border_1()
                        .border_color(withAlpha(agentVis.color, "70") || agentVis.color)
                        .when(Boolean(renderAgentIcon(agentVis, 11, agentVis.color)), (ic) =>
                          ic.child(renderAgentIcon(agentVis, 11, agentVis.color))
                        )
                        .child(
                          div()
                            .text_size(10)
                            .font_semibold()
                            .text_color(agentVis.color)
                            .child(`@${agentVis.owner}:${agentVis.label}`)
                        )
                    )
                  )
                  .child(
                    btn("insp-tall-toggle", this.drawerTall ? "Split" : "Full", t, {
                      small: true,
                      selected: this.drawerTall,
                      onClick: (_ev, cx) => {
                        this.syncBus({ showDrawer: true, drawerTall: !this.drawerTall }, cx);
                      },
                    })
                  )
                  .child(
                    btn("insp-close", "Hide", t, {
                      small: true,
                      iconPath: "mods/workspace-chrome/x.svg",
                      onClick: (_ev, cx) => {
                        this.syncBus({ showDrawer: false, drawerTall: false }, cx);
                      },
                    })
                  )
              )
          )
          .when(showActionRow, (hdr) =>
            hdr.child(
              // Row 2: State-Driven Agent Orchestration Action Bar
              h_flex()
                .w_full()
                .items_center()
                .justify_between()
                .gap(8)
                .overflow_hidden()
                .child(
                  h_flex()
                    .items_center()
                    .min_w(0)
                    .flex_1()
                    .overflow_hidden()
                    .gap(5)
                    .children(leftActions)
                )
                .when(rightActions.length > 0, (r) =>
                  r.child(
                    h_flex()
                      .items_center()
                      .flex_none()
                      .gap(5)
                      .children(rightActions)
                  )
                )
            )
          )
      )
      .child(
        // Adaptive Inspector Body: 2-Column when active agent/dependency state exists, Full-Width Markdown otherwise
        (isSplit && hasLeftPanel ? h_flex() : v_flex())
          .flex_1()
          .min_h(0)
          .w_full()
          .when(isSplit || !hasLeftPanel, (el) => el.overflow_hidden())
          .when(!isSplit && hasLeftPanel, (el) => el.overflow_y_scrollbar())
          .when(hasLeftPanel, (body) =>
            body.child(
              // Left Column: Claim Status, Checklist, Dependencies, Overlaps & Touched Files
              v_flex()
                .id("inspector-left-scroll")
                .w(isSplit ? Math.min(360, Math.round(panelW * 0.42)) : panelW)
                .flex_none()
                .when(isSplit, (el) =>
                  el.min_h(0).h_full().overflow_y_scrollbar().border_r(1).border_color(t.border)
                )
                .gap(8)
                .p(11)
                .when(overlaps.length > 0, (d) =>
                  d.child(
                    v_flex()
                      .w_full()
                      .flex_none()
                      .gap(4)
                      .p(9)
                      .rounded(7)
                      .bg(withAlpha(t.blocked || t.ansi1, "14") || t.background)
                      .border_1()
                      .border_color(t.blocked || t.ansi1)
                      .child(
                        div()
                          .text_size(11)
                          .font_semibold()
                          .text_color(t.blocked || t.ansi1)
                          .child("Touched-File Overlap Detected")
                      )
                      .children(
                        overlaps.map((ov) =>
                          div()
                            .text_size(11)
                            .whitespace_normal()
                            .text_color(t.foreground)
                            .child(
                              `#${ov.other_issue} (@${ov.other_owner}, ${ov.other_agent}): ${(
                                ov.files || []
                              ).join(", ")}`
                            )
                        )
                      )
                  )
                )
                .when(Boolean(hasClaim), (d) =>
                  d.child(
                    v_flex()
                      .w_full()
                      .flex_none()
                      .gap(6)
                      .p(9)
                      .rounded(7)
                      .bg(t.background)
                      .border_1()
                      .border_color(ticket.stale ? t.working || t.ansi3 : t.border)
                      .child(
                        h_flex()
                          .items_center()
                          .justify_between()
                          .child(
                            h_flex()
                              .items_center()
                              .gap(6)
                              .child(
                                div()
                                  .size(7)
                                  .rounded_full()
                                  .bg(rt.active ? t.working || t.ansi3 : t.accent)
                              )
                              .child(
                                div()
                                  .text_size(11.5)
                                  .font_semibold()
                                  .text_color(t.foreground)
                                  .child(`Claimed by @${claim.owner} (${claim.agent_kind})`)
                              )
                          )
                          .child(
                            div()
                              .px(5)
                              .py(1)
                              .rounded(4)
                              .bg(t.surface)
                              .text_size(9.5)
                              .font_semibold()
                              .text_color(ticket.stale ? t.working || t.ansi3 : t.done || t.ansi2)
                              .child(ticket.stale ? "STALE LEASE" : String(claim.state).toUpperCase())
                          )
                      )
                      .when(Boolean(mdClaimNote), (c) =>
                        c.child(
                          div()
                            .w_full()
                            .p(7)
                            .rounded(5)
                            .bg(t.surface)
                            .child(
                              TextView.markdown(`claim-note-md-${ticket.number}`, mdClaimNote)
                                .selectable(true)
                                .on_link_click((url, cx) => this.handleLinkClick(url, cx))
                            )
                        )
                      )
                      .when(Boolean(lc && lc.worktree_path), (c) =>
                        c.child(
                          h_flex()
                            .items_center()
                            .gap(5)
                            .child(svg("mods/workspace-chrome/folder.svg").size(10).text_color(t.dim))
                            .child(
                              div()
                                .flex_1()
                                .min_w(0)
                                .text_size(10)
                                .text_color(t.dim)
                                .truncate()
                                .child(lc.worktree_path)
                            )
                        )
                      )
                  )
                )
                .when(checklist.length > 0, (d) =>
                  d.child(
                    v_flex()
                      .w_full()
                      .flex_none()
                      .gap(6)
                      .p(9)
                      .rounded(7)
                      .bg(t.background)
                      .border_1()
                      .border_color(t.border)
                      .child(
                        h_flex()
                          .w_full()
                          .items_center()
                          .justify_between()
                          .child(
                            div()
                              .text_size(11)
                              .font_semibold()
                              .text_color(t.foreground)
                              .child(
                                `Agent Checklist (${ticket.checklist_done || 0}/${
                                  ticket.checklist_total || checklist.length
                                })`
                              )
                          )
                          .child(
                            div()
                              .w(48)
                              .h(5)
                              .rounded(3)
                              .bg(t.surface)
                              .overflow_hidden()
                              .child(
                                div()
                                  .w(
                                    Math.round(
                                      ((ticket.checklist_done || 0) /
                                        Math.max(1, ticket.checklist_total || checklist.length)) *
                                        48
                                    )
                                  )
                                  .h_full()
                                  .bg(
                                    (ticket.checklist_done || 0) ===
                                      (ticket.checklist_total || checklist.length)
                                      ? t.done || t.ansi2
                                      : t.accent
                                  )
                              )
                          )
                      )
                      .children(
                        checklist.map((item, cIdx) => {
                          const rawItem = String(item).trim();
                          const done = rawItem.toLowerCase().startsWith("[x]");
                          const cleanText = rawItem.replace(/^\[[ xX]\]\s*/, "");
                          return h_flex()
                            .id(`chk-${ticket.number}-${cIdx}`)
                            .w_full()
                            .items_start()
                            .gap(6)
                            .child(
                              div()
                                .pt(2)
                                .flex_none()
                                .child(
                                  svg(
                                    done
                                      ? "mods/workspace-chrome/circle-check.svg"
                                      : "mods/workspace-chrome/square.svg"
                                  )
                                    .size(11)
                                    .text_color(done ? t.done || t.ansi2 : t.dim)
                                )
                            )
                            .child(
                              div()
                                .flex_1()
                                .min_w(0)
                                .whitespace_normal()
                                .line_height(1.35)
                                .text_size(11)
                                .text_color(done ? t.muted : t.foreground)
                                .child(cleanText)
                            );
                        })
                      )
                  )
                )
                .when(deps.length > 0 || blocks.length > 0, (d) =>
                  d.child(
                    v_flex()
                      .w_full()
                      .flex_none()
                      .gap(5)
                      .p(9)
                      .rounded(7)
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
                          renderChunkedPillRows(
                            [
                              {
                                chars: 11,
                                el: div()
                                  .flex_none()
                                  .text_size(10.5)
                                  .text_color(t.muted)
                                  .child("Depends on:"),
                              },
                              ...deps.map((n) => {
                                const isUnmet = unmet.has(n);
                                const txt = `#${n}${isUnmet ? " (open)" : " ✓"}`;
                                return {
                                  chars: txt.length,
                                  el: div()
                                    .id(`dep-link-${n}`)
                                    .flex_none()
                                    .px(5)
                                    .py(1)
                                    .rounded(4)
                                    .cursor_pointer()
                                    .bg(t.surface)
                                    .border_1()
                                    .border_color(isUnmet ? t.blocked || t.ansi1 : t.border)
                                    .text_size(10)
                                    .text_color(isUnmet ? t.blocked || t.ansi1 : t.done || t.ansi2)
                                    .on_click((_ev, cx) => this.selectTicketByRef(String(n), cx))
                                    .child(txt),
                                };
                              }),
                            ],
                            38,
                            5,
                            5
                          )
                        )
                      )
                      .when(blocks.length > 0, (c) =>
                        c.child(
                          renderChunkedPillRows(
                            [
                              {
                                chars: 7,
                                el: div()
                                  .flex_none()
                                  .text_size(10.5)
                                  .text_color(t.muted)
                                  .child("Blocks:"),
                              },
                              ...blocks.map((n) => {
                                const txt = `#${n}`;
                                return {
                                  chars: txt.length,
                                  el: div()
                                    .id(`blk-link-${n}`)
                                    .flex_none()
                                    .px(5)
                                    .py(1)
                                    .rounded(4)
                                    .cursor_pointer()
                                    .bg(t.surface)
                                    .border_1()
                                    .border_color(t.border)
                                    .text_size(10)
                                    .text_color(t.accent)
                                    .on_click((_ev, cx) => this.selectTicketByRef(String(n), cx))
                                    .child(txt),
                                };
                              }),
                            ],
                            38,
                            5,
                            5
                          )
                        )
                      )
                  )
                )
                .when(touched.length > 0, (d) =>
                  d.child(
                    v_flex()
                      .w_full()
                      .flex_none()
                      .gap(4)
                      .p(9)
                      .rounded(7)
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
                          div().text_size(10.5).text_color(t.muted).truncate().child(fp)
                        )
                      )
                  )
                )
            )
          )
          .child(
            // Right Column (or Full-Width when no active left panel): Full Markdown Description
            v_flex()
              .id("inspector-right-scroll")
              .when(isSplit || !hasLeftPanel, (el) =>
                el.flex_1().min_w(0).min_h(0).h_full().overflow_y_scrollbar()
              )
              .when(!isSplit && hasLeftPanel, (el) => el.w_full().flex_none())
              .gap(6)
              .p(11)
              .child(
                h_flex()
                  .w_full()
                  .items_center()
                  .justify_between()
                  .gap(8)
                  .child(
                    h_flex()
                      .items_center()
                      .gap(5)
                      .min_w(0)
                      .overflow_hidden()
                      .child(
                        div()
                          .flex_none()
                          .text_size(11)
                          .font_semibold()
                          .text_color(t.muted)
                          .child("Specification & Notes")
                      )
                      .when(
                        Boolean(
                          isDoneTicket ||
                            resolveTicketBuilds(ticket, this.board && this.board.release_trains)
                              .entries.length > 0
                        ),
                        (bRow) => {
                          const releaseTrains = (this.board && this.board.release_trains) || [];
                          const bi = resolveTicketBuilds(ticket, releaseTrains);
                          const warnCol = t.working || t.ansi3 || "#d97736";
                          return bRow
                            .children(
                              bi.entries.map((entry) => {
                                const col = trainAccentColor(entry, entry.index, t);
                                return div()
                                  .id(`insp-bld-pill-${entry.train}`)
                                  .flex_none()
                                  .px(6)
                                  .py(1)
                                  .rounded(4)
                                  .cursor_pointer()
                                  .bg(withAlpha(col, "1C") || t.background)
                                  .border_1()
                                  .border_color(withAlpha(col, "70") || col)
                                  .text_size(10)
                                  .font_semibold()
                                  .text_color(col)
                                  .hover((s) => s.bg(t.hover))
                                  .on_click((_ev, cx) =>
                                    this.jumpToBuildChangelog(entry.train, entry.version, cx)
                                  )
                                  .child(`${entry.name} Build: ${shortBuildVersion(entry.version)}`);
                              })
                            )
                            .when(
                              Boolean(
                                isDoneTicket &&
                                  bi.entries.length === 0 &&
                                  releaseTrains.length > 0 &&
                                  !bi.isBaseline &&
                                  !bi.isReleaseTicket
                              ),
                              (el) =>
                                el.child(
                                  div()
                                    .id("insp-pending-build-pill")
                                    .flex_none()
                                    .px(6)
                                    .py(1)
                                    .rounded(4)
                                    .cursor_pointer()
                                    .bg(withAlpha(warnCol, "1C") || t.background)
                                    .border_1()
                                    .border_color(withAlpha(warnCol, "70") || warnCol)
                                    .text_size(10)
                                    .font_semibold()
                                    .text_color(warnCol)
                                    .on_click((_ev, cx) =>
                                      this.syncBus({ viewMode: "changelog" }, cx)
                                    )
                                    .child("Build: Merged (Pending Next Release Build)")
                                )
                            );
                        }
                      )
                      .when(Array.isArray(ticket.labels) && ticket.labels.length > 0, (l) =>
                        l.children(
                          ticket.labels
                            .filter((lbl) => !String(lbl).startsWith("release:"))
                            .slice(0, 5)
                            .map((lbl, idx) => {
                              const fc = parseFacetLabel(lbl, t);
                              return div()
                                .id(`desc-lbl-${idx}`)
                                .flex_none()
                                .px(5)
                                .py(1)
                                .rounded(4)
                                .bg(fc.bg)
                                .text_size(9.5)
                                .text_color(fc.color)
                                .child(truncStr(lbl, 20));
                            })
                        )
                      )
                  )
                  .when(Boolean(ticket.milestone), (m) =>
                    m.child(
                      div()
                        .flex_none()
                        .px(5)
                        .py(1)
                        .rounded(4)
                        .bg(t.background)
                        .text_size(10)
                        .text_color(t.dim)
                        .child(ticket.milestone)
                    )
                  )
              )
              .child(
                v_flex()
                  .w_full()
                  .flex_none()
                  .p(11)
                  .rounded(7)
                  .bg(t.background)
                  .border_1()
                  .border_color(t.border)
                  .child(
                    TextView.markdown(`ticket-desc-md-${ticket.number}`, mdBody)
                      .selectable(true)
                      .on_link_click((url, cx) => this.handleLinkClick(url, cx))
                  )
              )
          )
      );
  }

  renderFooter(t, tgt) {
    const hasActiveMessage = Boolean(this.busyText || this.error || this.statusMessage);
    const shortcuts =
      this.viewMode === "whats_new"
        ? [
            ["s", "summarize"],
            ["g", "changelog"],
            ["b", "board"],
            ["r", "refresh"],
          ]
        : this.viewMode === "changelog"
        ? [
            ["b", "board"],
            ["w", "what's new"],
            ["r", "refresh"],
          ]
        : [
            ["hjkl", "nav"],
            ["g", "changelog"],
            ["w", "what's new"],
            ["n", "deposit"],
          ];

    return h_flex()
      .w_full()
      .h(24)
      .flex_none()
      .items_center()
      .justify_between()
      .px(12)
      .bg(t.chrome || t.surface)
      .border_t(1)
      .border_color(t.border)
      .child(
        h_flex()
          .items_center()
          .gap(6)
          .min_w(0)
          .flex_1()
          .when(Boolean(this.busyText), (d) =>
            d.child(
              div()
                .text_size(11)
                .font_semibold()
                .text_color(t.working || t.ansi3)
                .truncate()
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
            d
              .child(svg("mods/workspace-chrome/folder.svg").size(10).flex_none().text_color(t.dim))
              .child(
                div()
                  .text_size(10.5)
                  .text_color(t.dim)
                  .truncate()
                  .child(tgt.cwd ? `${tgt.on ? tgt.on + ":" : ""}${tgt.cwd}` : "No active repo")
              )
          )
      )
      .when(!hasActiveMessage, (f) =>
        f.child(
          h_flex()
            .flex_none()
            .items_center()
            .gap(8)
            .children(
              shortcuts.map(([k, lbl]) =>
                h_flex()
                  .items_center()
                  .gap(3)
                  .child(
                    div()
                      .px(4)
                      .rounded(3)
                      .bg(t.background)
                      .border_1()
                      .border_color(t.border)
                      .text_size(9.5)
                      .text_color(t.muted)
                      .child(k)
                  )
                  .child(div().text_size(10).text_color(t.dim).child(lbl))
              )
            )
        )
      );
  }

  render(cx) {
    RENDER_SNAPSHOT = null;
    RENDER_SNAPSHOT = hostSnapshot(); // fresh per pass
    try {
      return this.renderFrame(cx);
    } finally {
      RENDER_SNAPSHOT = null;
    }
  }

  renderFrame(cx) {
    const t = theme();
    const p = props() || {};
    const part = p.part || "";
    const navTriggered = !checking() && this.consumeSharedNav(cx);
    const tgt = this.target();
    const curTargetKey = `${tgt.on || "local"}:${tgt.spaceId || ""}:${tgt.cwd || ""}`;
    if (!checking() && (curTargetKey !== this.lastTargetKey || navTriggered)) {
      const targetChanged = curTargetKey !== this.lastTargetKey;
      this.lastTargetKey = curTargetKey;
      if (targetChanged) {
        this.activeCwd = null;
        this.board = (tgt.spaceId && this.bus?.boardBySpace?.[tgt.spaceId]) || null;
        this.noKanbanInSpace = false;
        this.error = "";
      }
      this.refresh(cx, Boolean(this.board));
    }

    if (this.bus) {
      this.searchQuery = this.bus.searchQuery;
      this.repoFilter = this.bus.repoFilter;
      this.quickFilter = this.bus.quickFilter || "active";
      this.compactEmpty = this.bus.compactEmpty !== false;
      this.viewMode = this.bus.viewMode || "board";
      this.changelogTrain = this.bus.changelogTrain || "all";
      this.changelogType = this.bus.changelogType || "all";
      this.changelogRanges = { ...(this.bus.changelogRanges || {}) };
      this.agentKind = this.bus.agentKind;
      this.overrideSpaceId = this.bus.overrideSpaceId;
      this.showDrawer = this.bus.showDrawer !== false;
      this.drawerTall = Boolean(this.bus.drawerTall);
      if (this.bus.drawerHeight !== undefined) this.drawerHeight = this.bus.drawerHeight;
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
                .child(
                  div()
                    .text_size(13)
                    .text_color(t.muted)
                    .child("Loading GitHub Project v2 Kanban board…")
                )
            )
          )
          .when(Boolean(!this.loading && !this.board && this.noKanbanInSpace), (d) =>
            d.child(
              v_flex()
                .flex_1()
                .h_full()
                .items_center()
                .justify_center()
                .gap(6)
                .p(24)
                .child(
                  div()
                    .text_size(12.5)
                    .font_medium()
                    .text_color(t.muted)
                    .child("No Kanban board configured for this space")
                )
                .child(
                  div()
                    .text_size(11)
                    .text_color(t.dim)
                    .child("Switch to a workspace with a .herdr-kanban.toml configuration")
                )
            )
          )
          .when(Boolean(!this.loading && !this.board && !this.noKanbanInSpace && this.error), (d) =>
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
          .when(Boolean(this.board && this.viewMode === "whats_new"), (d) =>
            d.child(this.renderWhatsNewView(t))
          )
          .when(Boolean(this.board && this.viewMode === "changelog"), (d) =>
            d.child(this.renderChangelogView(t))
          )
          .when(
            Boolean(
              this.board &&
                this.viewMode !== "whats_new" &&
                this.viewMode !== "changelog"
            ),
            (d) =>
              d
                .when(!this.showDrawer || !this.drawerTall, (col) =>
                  col.child(this.renderBoardCanvas(byCol, selectedRef, t))
                )
                .child(this.renderInspector(selected, t))
          )
      )
      .child(this.renderFooter(t, tgt));
  }
}
