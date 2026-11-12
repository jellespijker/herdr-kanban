"""Pure domain types, column mappings, and GitHub comment metadata codec (docs/spec.md §3-§4)."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

META_PREFIX = "<!-- herdr-kanban:v1 "
META_SUFFIX = " -->"
META_PATTERN = re.compile(r"<!--\s*herdr-kanban:v1\s+(\{.*?\})\s*-->", re.DOTALL)
TICKET_REF_RE = re.compile(r"^(?:([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?)#)?#?(\d+)$")


def parse_ticket_ref(raw: str) -> tuple[str | None, int]:
    """Parse `42`, `#42`, `cloud#42`, or `org/cloud#42` into `(repo_selector, number)`."""
    text = raw.strip()
    match = TICKET_REF_RE.match(text)
    if not match:
        raise ValueError(f"invalid ticket reference '{raw}' (expected e.g. `42` or `repo#42`)")
    repo_sel = match.group(1) or None
    num = int(match.group(2))
    if num < 1:
        raise ValueError(f"ticket number must be >= 1, got {num}")
    return repo_sel, num


class Column(StrEnum):
    TRIAGE = "triage"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    REVIEW = "review"
    DONE = "done"


COLUMN_TITLES: dict[Column, str] = {
    Column.TRIAGE: "Triage",
    Column.READY: "Ready",
    Column.IN_PROGRESS: "In Progress",
    Column.BLOCKED: "Blocked",
    Column.REVIEW: "Review",
    Column.DONE: "Done",
}

DEFAULT_COLUMN_ALIASES: dict[Column, tuple[str, ...]] = {
    Column.TRIAGE: ("triage", "backlog", "inbox", "icebox", "new"),
    Column.READY: ("ready", "todo", "to do", "up next", "groomed"),
    Column.IN_PROGRESS: ("in progress", "in_progress", "doing", "active", "working"),
    Column.BLOCKED: ("blocked", "stuck", "on hold", "waiting"),
    Column.REVIEW: ("review", "in review", "needs review", "qa"),
    Column.DONE: ("done", "completed", "complete", "closed", "shipped"),
}


def match_column(status_name: str | None, explicit_map: dict[str, str] | None = None) -> Column:
    """Map a GitHub Project v2 Status option name to a logical Column."""
    if not status_name:
        return Column.TRIAGE
    lowered = status_name.strip().lower()
    if explicit_map:
        for col_key, configured_name in explicit_map.items():
            if configured_name and configured_name.strip().lower() == lowered:
                try:
                    return Column(col_key)
                except ValueError:
                    pass
    for col, aliases in DEFAULT_COLUMN_ALIASES.items():
        if lowered in aliases:
            return col
    return Column.TRIAGE


def utc_now() -> datetime:
    return datetime.now(UTC)


def format_iso(dt: datetime) -> str:
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def utc_now_iso() -> str:
    return format_iso(utc_now())


def lease_until_iso(ttl_minutes: int, now: datetime | None = None) -> str:
    base = now or utc_now()
    return format_iso(base + timedelta(minutes=max(1, ttl_minutes)))


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def is_lease_expired(lease_until: str | None, now: datetime | None = None) -> bool:
    expiry = parse_iso(lease_until)
    if expiry is None:
        return True
    return (now or utc_now()) > expiry


@dataclass(slots=True)
class ClaimMeta:
    issue_number: int
    owner: str
    agent_kind: str
    state: str = "active"  # active | review_ready | released | completed
    started_at: str = ""
    heartbeat_at: str = ""
    lease_until: str = ""
    agent_name: str = ""
    herdr_session: str = ""
    pane_id: str = ""
    machine: str = ""
    branch: str = ""
    risk_gate: bool = False
    touched_files: list[str] = field(default_factory=list)
    checklist: list[str] = field(default_factory=list)
    note: str = ""
    takeover_of: int | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "v": 1,
            "type": "claim",
            "issue_number": self.issue_number,
            "owner": self.owner,
            "agent_kind": self.agent_kind,
            "state": self.state,
            "started_at": self.started_at,
            "heartbeat_at": self.heartbeat_at,
            "lease_until": self.lease_until,
            "agent_name": self.agent_name,
            "herdr_session": self.herdr_session,
            "pane_id": self.pane_id,
            "machine": self.machine,
            "branch": self.branch,
            "risk_gate": self.risk_gate,
            "touched_files": list(self.touched_files),
            "checklist": list(self.checklist),
            "note": self.note,
            "takeover_of": self.takeover_of,
        }
        return data

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ClaimMeta:
        return cls(
            issue_number=int(raw["issue_number"]),
            owner=str(raw["owner"]),
            agent_kind=str(raw.get("agent_kind") or "agent"),
            state=str(raw.get("state") or "active"),
            started_at=str(raw.get("started_at") or ""),
            heartbeat_at=str(raw.get("heartbeat_at") or ""),
            lease_until=str(raw.get("lease_until") or ""),
            agent_name=str(raw.get("agent_name") or ""),
            herdr_session=str(raw.get("herdr_session") or ""),
            pane_id=str(raw.get("pane_id") or ""),
            machine=str(raw.get("machine") or ""),
            branch=str(raw.get("branch") or ""),
            risk_gate=bool(raw.get("risk_gate", False)),
            touched_files=[str(p) for p in raw.get("touched_files") or []],
            checklist=[str(c) for c in raw.get("checklist") or []],
            note=str(raw.get("note") or ""),
            takeover_of=int(raw["takeover_of"]) if raw.get("takeover_of") is not None else None,
        )


@dataclass(slots=True)
class RelayMeta:
    from_owner: str
    to_issue: int
    created_at: str
    from_agent: str = ""
    from_repo: str = ""
    from_issue: int | None = None
    to_owner: str = ""
    urgent: bool = False

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "v": 1,
            "type": "relay",
            "from_owner": self.from_owner,
            "from_agent": self.from_agent,
            "from_issue": self.from_issue,
            "to_issue": self.to_issue,
            "to_owner": self.to_owner,
            "created_at": self.created_at,
            "urgent": self.urgent,
        }
        if self.from_repo:
            data["from_repo"] = self.from_repo
        return data

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RelayMeta:
        return cls(
            from_owner=str(raw["from_owner"]),
            to_issue=int(raw["to_issue"]),
            created_at=str(raw.get("created_at") or ""),
            from_agent=str(raw.get("from_agent") or ""),
            from_repo=str(raw.get("from_repo") or ""),
            from_issue=int(raw["from_issue"]) if raw.get("from_issue") is not None else None,
            to_owner=str(raw.get("to_owner") or ""),
            urgent=bool(raw.get("urgent", False)),
        )


@dataclass(slots=True)
class DepositMeta:
    deposited_by: str
    branch: str
    parent_issue: int | None = None
    parent_repo: str = ""
    file_line: str = ""
    commit_sha: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "v": 1,
            "type": "deposit",
            "deposited_by": self.deposited_by,
            "branch": self.branch,
            "parent_issue": self.parent_issue,
            "file_line": self.file_line,
            "commit_sha": self.commit_sha,
        }
        if self.parent_repo:
            data["parent_repo"] = self.parent_repo
        return data

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> DepositMeta:
        return cls(
            deposited_by=str(raw["deposited_by"]),
            branch=str(raw.get("branch") or ""),
            parent_issue=int(raw["parent_issue"]) if raw.get("parent_issue") is not None else None,
            parent_repo=str(raw.get("parent_repo") or ""),
            file_line=str(raw.get("file_line") or ""),
            commit_sha=str(raw.get("commit_sha") or ""),
        )


def encode_meta(payload: dict[str, Any]) -> str:
    compact = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return f"{META_PREFIX}{compact}{META_SUFFIX}"


def decode_meta(markdown: str | None) -> dict[str, Any] | None:
    if not markdown:
        return None
    match = META_PATTERN.search(markdown)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(1))
    except ValueError:
        return None
    if isinstance(parsed, dict) and parsed.get("v") == 1 and isinstance(parsed.get("type"), str):
        return parsed
    return None


def strip_meta(markdown: str | None) -> str:
    if not markdown:
        return ""
    return META_PATTERN.sub("", markdown).strip()


def render_claim_comment(claim: ClaimMeta) -> str:
    badge = {
        "active": "Claimed & in progress",
        "review_ready": "Ready for human review gate",
        "released": "Released claim",
        "completed": "Completed & landed",
    }.get(claim.state, claim.state)
    agent_label = f"{claim.agent_kind}:{claim.agent_name}" if claim.agent_name else claim.agent_kind
    lines = [
        f"**[herdr-kanban]** {badge} by `@{claim.owner}` (`{agent_label}`)",
    ]
    if claim.branch:
        lines.append(f"- Target branch: `{claim.branch}`")
    if claim.state in ("active", "review_ready"):
        lines.append(f"- Heartbeat: `{claim.heartbeat_at}` (lease until `{claim.lease_until}`)")
    if claim.note:
        lines.append(f"- Status: {claim.note}")
    if claim.checklist:
        lines.append("- Progress:")
        for entry in claim.checklist:
            prefix = entry if entry.startswith(("[ ]", "[x]", "[X]")) else f"[ ] {entry}"
            lines.append(f"  - {prefix}")
    if claim.touched_files:
        shown = ", ".join(f"`{p}`" for p in claim.touched_files[:12])
        more = f" (+{len(claim.touched_files) - 12} more)" if len(claim.touched_files) > 12 else ""
        lines.append(f"- Touched files: {shown}{more}")
    lines.append("")
    lines.append(encode_meta(claim.to_dict()))
    return "\n".join(lines)


def render_relay_comment(relay: RelayMeta, message: str) -> str:
    if relay.from_issue:
        origin = f"{relay.from_repo}#{relay.from_issue}" if relay.from_repo else f"#{relay.from_issue}"
    else:
        origin = "board"
    sender = f"@{relay.from_owner}" + (f" ({relay.from_agent})" if relay.from_agent else "")
    header = f"**[herdr-kanban message from {sender} via {origin}]**"
    return f"{header}\n\n{message.strip()}\n\n{encode_meta(relay.to_dict())}"


def render_deposit_body(body: str, deposit: DepositMeta) -> str:
    parts = [body.strip()] if body.strip() else []
    context_lines = ["---", "**Deposited via `herdr-kanban`**"]
    if deposit.parent_issue:
        ref = (
            f"{deposit.parent_repo}#{deposit.parent_issue}"
            if deposit.parent_repo
            else f"#{deposit.parent_issue}"
        )
        context_lines.append(f"- Discovered while working on: {ref}")
    if deposit.file_line:
        context_lines.append(f"- Location: `{deposit.file_line}`")
    if deposit.branch:
        sha_note = f" (`{deposit.commit_sha[:8]}`)" if deposit.commit_sha else ""
        context_lines.append(f"- Branch: `{deposit.branch}`{sha_note}")
    parts.append("\n".join(context_lines))
    parts.append(encode_meta(deposit.to_dict()))
    return "\n\n".join(parts)


def resolve_winning_claim(comments: list[dict[str, Any]]) -> tuple[int, ClaimMeta] | None:
    """Resolve the canonical active claim from issue comments (ADR-0001).

    `comments` is any iterable of dicts with `id` (int) and `body` (str).
    Earliest active claim wins ties; explicit `takeover_of` supersedes a prior holder.
    """
    ordered = sorted(
        (c for c in comments if isinstance(c.get("id"), int)),
        key=lambda c: int(c["id"]),
    )
    winner: tuple[int, ClaimMeta] | None = None
    for entry in ordered:
        meta = decode_meta(entry.get("body"))
        if not meta or meta.get("type") != "claim":
            continue
        try:
            claim = ClaimMeta.from_dict(meta)
        except (KeyError, TypeError, ValueError):
            continue
        cid = int(entry["id"])
        if claim.state not in ("active", "review_ready"):
            if winner and winner[0] == cid:
                winner = None
            continue
        if winner is None:
            winner = (cid, claim)
            continue
        # An explicit takeover replaces the targeted prior claim when stale or matching ID.
        if claim.takeover_of == winner[0]:
            winner = (cid, claim)
    return winner


@dataclass(slots=True)
class FileOverlap:
    other_issue: int
    other_owner: str
    other_agent: str
    files: list[str]


@dataclass(slots=True)
class Ticket:
    number: int
    title: str
    body: str = ""
    state: str = "OPEN"  # OPEN | CLOSED
    url: str = ""
    author: str = ""
    assignees: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    column: Column = Column.TRIAGE
    status_name: str = ""
    project_item_id: str = ""
    repo_owner: str = ""
    repo_name: str = ""
    repo_alias: str = ""
    claim: ClaimMeta | None = None
    claim_comment_id: int | None = None
    stale: bool = False
    deposit: DepositMeta | None = None
    comments_count: int = 0
    updated_at: str = ""
    overlaps: list[FileOverlap] = field(default_factory=list)

    def ref(self, multi_repo: bool = False) -> str:
        prefix = self.repo_alias or self.repo_name
        if multi_repo and prefix:
            return f"{prefix}#{self.number}"
        return f"#{self.number}"

    def repo_key(self) -> tuple[str, str]:
        return (self.repo_owner.lower(), self.repo_name.lower())

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "title": self.title,
            "repo_owner": self.repo_owner,
            "repo_name": self.repo_name,
            "repo_alias": self.repo_alias,
            "body": strip_meta(self.body),
            "state": self.state,
            "url": self.url,
            "author": self.author,
            "assignees": list(self.assignees),
            "labels": list(self.labels),
            "column": str(self.column),
            "status_name": self.status_name,
            "project_item_id": self.project_item_id,
            "claim": self.claim.to_dict() if self.claim else None,
            "claim_comment_id": self.claim_comment_id,
            "stale": self.stale,
            "deposit": self.deposit.to_dict() if self.deposit else None,
            "comments_count": self.comments_count,
            "updated_at": self.updated_at,
            "overlaps": [asdict(o) for o in self.overlaps],
        }


def populate_file_overlaps(tickets: list[Ticket]) -> None:
    """Populate `ticket.overlaps` in-place, scoped per repository."""
    active = [
        t
        for t in tickets
        if t.claim and t.claim.state in ("active", "review_ready") and t.claim.touched_files
    ]
    active_by_id = {(t.repo_key(), t.number): t for t in active}
    for ticket in tickets:
        t_claim = active_by_id.get((ticket.repo_key(), ticket.number))
        if t_claim is None or t_claim.claim is None:
            ticket.overlaps = []
            continue
        mine = set(t_claim.claim.touched_files)
        hits: list[FileOverlap] = []
        for other in active:
            if other.repo_key() != ticket.repo_key() or other.number == ticket.number or other.claim is None:
                continue
            shared = sorted(mine & set(other.claim.touched_files))
            if shared:
                hits.append(
                    FileOverlap(
                        other_issue=other.number,
                        other_owner=other.claim.owner,
                        other_agent=other.claim.agent_kind,
                        files=shared,
                    )
                )
        ticket.overlaps = hits


def compute_file_overlaps(tickets: list[Ticket]) -> dict[int, list[FileOverlap]]:
    """Return issue_number -> list of overlapping active claims (same-repo scoped)."""
    populate_file_overlaps(tickets)
    return {t.number: list(t.overlaps) for t in tickets if t.overlaps}


@dataclass(slots=True)
class InboxItem:
    comment_id: int
    issue_number: int
    author: str
    body: str
    created_at: str
    from_agent: str = ""
    from_repo: str = ""
    from_issue: int | None = None
    trusted: bool = False
    delivered: bool = False
    acked: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> InboxItem:
        return cls(
            comment_id=int(raw["comment_id"]),
            issue_number=int(raw["issue_number"]),
            author=str(raw.get("author") or ""),
            body=str(raw.get("body") or ""),
            created_at=str(raw.get("created_at") or ""),
            from_agent=str(raw.get("from_agent") or ""),
            from_repo=str(raw.get("from_repo") or ""),
            from_issue=int(raw["from_issue"]) if raw.get("from_issue") is not None else None,
            trusted=bool(raw.get("trusted", False)),
            delivered=bool(raw.get("delivered", False)),
            acked=bool(raw.get("acked", False)),
        )
