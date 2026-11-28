"""Isolated GitHub Projects v2 (GraphQL) & Issues (REST) adapter via `gh` CLI (ADR-0001, ADR-0006)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import state
from .model import (
    Column,
    DepositMeta,
    Ticket,
    decode_meta,
    extract_ticket_builds,
    is_lease_expired,
    match_column,
    populate_dependency_waves,
    populate_file_overlaps,
    resolve_winning_claim,
)

GhRunner = Callable[[list[str], str | None], subprocess.CompletedProcess[str]]


class GitHubError(RuntimeError):
    """Raised when `gh` fails or returns an unexpected response."""


def _is_missing_project_scope_error(detail: str) -> bool:
    lowered = detail.lower()
    return ("read:project" in lowered or "'project'" in lowered) and (
        "scope" in lowered or "insufficient_scopes" in lowered
    )


def _is_rate_limit_text(detail: str) -> bool:
    lowered = detail.lower()
    return (
        "rate limit" in lowered
        or "rate_limited" in lowered
        or "secondary rate" in lowered
        or "abuse detection" in lowered
        or "http 429" in lowered
        or ("http 403" in lowered and "rate" in lowered)
    )


_REPOS_ENDPOINT_RE = re.compile(r"^/?repos/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(?:/|\?|$)")


def has_project_scope(auth_status_output: str) -> bool:
    """Return False only when `gh auth status` explicitly lists token scopes without `project`."""
    for line in auth_status_output.splitlines():
        if "token scopes:" in line.lower():
            scopes_part = line.split(":", 1)[1].lower()
            tokens = {tok.strip(" '\",") for tok in scopes_part.split(",")}
            return "project" in tokens
    return True



@dataclass(slots=True)
class ProjectSchema:
    project_id: str
    number: int
    title: str
    url: str
    status_field_id: str
    options_by_name: dict[str, str] = field(default_factory=dict)  # exact option name -> optionId
    priority_field_id: str = ""
    priority_options_by_name: dict[str, str] = field(default_factory=dict)
    text_fields_by_name: dict[str, str] = field(default_factory=dict)

    def option_id_for_column(self, col: Column, column_map: dict[str, str]) -> str | None:
        preferred = column_map.get(col.value, "")
        if preferred:
            for opt_name, opt_id in self.options_by_name.items():
                if opt_name.lower() == preferred.lower():
                    return opt_id
        for opt_name, opt_id in self.options_by_name.items():
            if match_column(opt_name, column_map) == col:
                return opt_id
        return None

    def option_id_for_priority(self, priority: str) -> str | None:
        target = priority.strip().lower()
        if not target:
            return None
        for opt_name, opt_id in self.priority_options_by_name.items():
            if opt_name.lower() == target:
                return opt_id
        for opt_name, opt_id in self.priority_options_by_name.items():
            if opt_name.lower().startswith(target):
                return opt_id
        return None


def gh_binary() -> str | None:
    found = shutil.which("gh")
    if found:
        return found
    home = Path.home()
    candidates = (
        home / ".local" / "share" / "mise" / "shims" / "gh",
        home / ".local" / "bin" / "gh",
        Path("/opt/homebrew/bin/gh"),
        Path("/usr/local/bin/gh"),
    )
    for candidate in candidates:
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def default_gh_runner(args: list[str], stdin_data: str | None = None) -> subprocess.CompletedProcess[str]:
    exe = gh_binary()
    if not exe:
        raise GitHubError("GitHub CLI (`gh`) not found in PATH")
    return subprocess.run(
        [exe, *args],
        input=stdin_data,
        capture_output=True,
        text=True,
        timeout=45,
    )


_PROJECT_FIELDS_FRAGMENT = """
fragment ProjectFields on ProjectV2 {
  id
  number
  title
  url
  field(name: "Status") {
    ... on ProjectV2SingleSelectField {
      id
      name
      options {
        id
        name
      }
    }
  }
  priorityField: field(name: $priorityField) {
    ... on ProjectV2SingleSelectField {
      id
      name
      options {
        id
        name
      }
    }
  }
  fields(first: 25) {
    nodes {
      ... on ProjectV2Field {
        id
        name
      }
      ... on ProjectV2SingleSelectField {
        id
        name
      }
    }
  }
  items(first: 100, after: $cursor) {
    pageInfo {
      hasNextPage
      endCursor
    }
    nodes {
      id
      fieldValueByName(name: "Status") {
        ... on ProjectV2ItemFieldSingleSelectValue {
          name
          optionId
        }
      }
      priorityValue: fieldValueByName(name: $priorityField) {
        ... on ProjectV2ItemFieldSingleSelectValue {
          name
          optionId
        }
      }
      fieldValues(first: 8) {
        nodes {
          ... on ProjectV2ItemFieldTextValue {
            text
            field {
              ... on ProjectV2FieldCommon {
                name
              }
            }
          }
          ... on ProjectV2ItemFieldSingleSelectValue {
            name
            field {
              ... on ProjectV2FieldCommon {
                name
              }
            }
          }
        }
      }
      content {
        ... on Issue {
          id
          number
          title
          body
          state
          url
          updatedAt
          author { login }
          assignees(first: 5) { nodes { login } }
          labels(first: 15) { nodes { name } }
          milestone {
            number
            title
          }
          repository {
            name
            owner { login }
          }
          comments(last: 5) {
            totalCount
            nodes {
              databaseId
              body
              createdAt
              author { login }
            }
          }
        }
      }
    }
  }
}
"""

PROJECT_BOARD_QUERY = (
    """
query($owner: String!, $number: Int!, $cursor: String, $priorityField: String!) {
  organization(login: $owner) {
    login
    projectV2(number: $number) {
      ...ProjectFields
    }
  }
}
"""
    + _PROJECT_FIELDS_FRAGMENT
)

USER_PROJECT_BOARD_QUERY = (
    """
query($owner: String!, $number: Int!, $cursor: String, $priorityField: String!) {
  user(login: $owner) {
    login
    projectV2(number: $number) {
      ...ProjectFields
    }
  }
}
"""
    + _PROJECT_FIELDS_FRAGMENT
)

LIST_PROJECTS_QUERY = """
query($owner: String!) {
  repositoryOwner(login: $owner) {
    ... on Organization {
      projectsV2(first: 30) {
        nodes { id number title closed url }
      }
    }
    ... on User {
      projectsV2(first: 30) {
        nodes { id number title closed url }
      }
    }
  }
}
"""

OWNER_ID_QUERY = """
query($owner: String!) {
  repositoryOwner(login: $owner) {
    id
    login
  }
}
"""

CREATE_PROJECT_MUTATION = """
mutation($ownerId: ID!, $title: String!) {
  createProjectV2(input: {ownerId: $ownerId, title: $title}) {
    projectV2 {
      id
      number
      title
      url
    }
  }
}
"""

ADD_ITEM_MUTATION = """
mutation($projectId: ID!, $contentId: ID!) {
  addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {
    item {
      id
    }
  }
}
"""

SET_STATUS_MUTATION = """
mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
  updateProjectV2ItemFieldValue(
    input: {
      projectId: $projectId
      itemId: $itemId
      fieldId: $fieldId
      value: { singleSelectOptionId: $optionId }
    }
  ) {
    projectV2Item { id }
  }
}
"""


class GitHubClient:
    def __init__(self, runner: GhRunner = default_gh_runner) -> None:
        self._run = runner
        self._user_cache: str | None = None
        self._collab_cache: dict[tuple[str, str], set[str]] = {}
        self._owner_kind_cache: dict[str, str] = {}
        self._project_owner_by_id: dict[str, str] = {}
        self._telemetry_owner: str = ""
        self._telemetry_repo: str = ""

    def set_telemetry_owner(self, owner: str, repo: str = "") -> None:
        self._telemetry_owner = (owner or "").strip()
        self._telemetry_repo = (repo or "").strip()

    def _default_telemetry_repo_key(self) -> str:
        if self._telemetry_owner and self._telemetry_repo:
            return f"{self._telemetry_owner}/{self._telemetry_repo}"
        if self._telemetry_owner:
            return f"{self._telemetry_owner}/__project__"
        return ""

    def _infer_api_repo_key(self, args: list[str], payload: dict[str, Any] | None = None) -> str:
        if len(args) >= 2 and args[0] == "api" and args[1] == "graphql":
            variables = (payload or {}).get("variables")
            if isinstance(variables, dict):
                owner = str(variables.get("owner") or "").strip()
                repo = str(variables.get("repo") or "").strip()
                if owner and repo:
                    return f"{owner}/{repo}"
                if owner:
                    return f"{owner}/__project__"
                proj_id = str(variables.get("projectId") or "").strip()
                if proj_id and proj_id in self._project_owner_by_id:
                    return f"{self._project_owner_by_id[proj_id]}/__project__"
            return self._default_telemetry_repo_key()

        if args and args[0] == "api":
            for token in args[1:]:
                if token.startswith("-"):
                    continue
                matched = _REPOS_ENDPOINT_RE.match(token)
                if matched:
                    return f"{matched.group(1)}/{matched.group(2)}"
            return self._default_telemetry_repo_key()
        return ""

    def _run_api(
        self,
        args: list[str],
        stdin_text: str | None = None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], str]:
        repo_key = self._infer_api_repo_key(args, payload=payload)
        is_graphql = len(args) >= 2 and args[0] == "api" and args[1] == "graphql"
        proc = self._run(args, stdin_text)
        rate_err = 0
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            if _is_rate_limit_text(detail):
                rate_err = 1
        if repo_key and args and args[0] == "api":
            state.record_telemetry_counter(
                repo_key,
                rest_calls=0 if is_graphql else 1,
                graphql_calls=1 if is_graphql else 0,
                rate_limit_errors=rate_err,
            )
        return proc, repo_key

    def _gh_json(
        self,
        args: list[str],
        *,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        stdin_text = json.dumps(payload) if payload is not None else None
        final_args = list(args)
        if payload is not None and "--input" not in final_args:
            final_args.extend(["--input", "-"])
        proc, _ = self._run_api(final_args, stdin_text, payload=payload)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()
            if _is_missing_project_scope_error(detail):
                raise GitHubError(
                    "GitHub token is missing Projects v2 permissions (`project` / `read:project`). "
                    "Run: `gh auth refresh -s project,read:project`"
                )
            raise GitHubError(f"gh {' '.join(args[:3])} failed: {detail}")
        text = proc.stdout.strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except ValueError as exc:
            raise GitHubError(f"invalid JSON from gh: {text[:200]}") from exc

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        payload = {"query": query, "variables": variables}
        resp = self._gh_json(["api", "graphql"], payload=payload)
        if isinstance(resp, dict) and resp.get("errors"):
            messages = "; ".join(str(e.get("message", e)) for e in resp["errors"])
            if _is_rate_limit_text(messages):
                repo_key = self._infer_api_repo_key(["api", "graphql"], payload=payload)
                if repo_key:
                    state.record_telemetry_counter(repo_key, rate_limit_errors=1)
            if _is_missing_project_scope_error(messages):
                raise GitHubError(
                    "GitHub token is missing Projects v2 permissions (`project` / `read:project`). "
                    "Run: `gh auth refresh -s project,read:project`"
                )
            raise GitHubError(f"GitHub GraphQL error: {messages}")
        return resp.get("data") or {} if isinstance(resp, dict) else {}

    def auth_check(self) -> tuple[bool, str]:
        proc = self._run(["auth", "status"], None)
        out = "\n".join(p for p in (proc.stdout.strip(), proc.stderr.strip()) if p)
        return proc.returncode == 0, out

    def current_user(self) -> str:
        if self._user_cache:
            return self._user_cache
        data = self._gh_json(["api", "user"])
        login = str((data or {}).get("login") or "").strip()
        if not login:
            raise GitHubError("could not determine authenticated GitHub login via `gh api user`")
        self._user_cache = login
        return login

    def list_projects(self, owner: str) -> list[dict[str, Any]]:
        data = self.graphql(LIST_PROJECTS_QUERY, {"owner": owner})
        owner_node = data.get("repositoryOwner") or {}
        proj_conn = owner_node.get("projectsV2") or {}
        nodes = proj_conn.get("nodes") or []
        return [n for n in nodes if isinstance(n, dict) and not n.get("closed")]

    def create_project(self, owner: str, title: str) -> dict[str, Any]:
        owner_data = self.graphql(OWNER_ID_QUERY, {"owner": owner})
        owner_id = ((owner_data.get("repositoryOwner") or {}).get("id") or "").strip()
        if not owner_id:
            raise GitHubError(f"could not resolve GitHub owner '{owner}'")
        created = self.graphql(CREATE_PROJECT_MUTATION, {"ownerId": owner_id, "title": title})
        proj = ((created.get("createProjectV2") or {}).get("projectV2")) or {}
        if not proj.get("number"):
            raise GitHubError("failed to create GitHub Project v2")
        return proj

    def clone_repo(self, owner: str, repo: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        proc = self._run(["repo", "clone", f"{owner}/{repo}", str(dest)], None)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()
            raise GitHubError(f"gh repo clone {owner}/{repo} failed: {detail}")

    def fetch_board(
        self,
        *,
        project_owner: str,
        project_number: int,
        repo_owner: str = "",
        repo_name: str = "",
        column_map: dict[str, str],
        label_filter: str = "",
        priority_field: str = "Priority",
        allowed_repos: dict[tuple[str, str], str] | None = None,
        release_trains: dict[str, Any] | None = None,
        release_label_prefix: str = "release:",
        release_baseline_label: str = "release:baseline",
    ) -> tuple[ProjectSchema, list[Ticket]]:
        cursor: str | None = None
        schema: ProjectSchema | None = None
        tickets: list[Ticket] = []
        owner_key = project_owner.lower()
        owner_kind = self._owner_kind_cache.get(owner_key, "organization")

        while True:
            variables = {
                "owner": project_owner,
                "number": project_number,
                "cursor": cursor,
                "priorityField": priority_field or "Priority",
            }
            if owner_kind == "organization":
                try:
                    data = self.graphql(PROJECT_BOARD_QUERY, variables)
                    owner_node = data.get("organization") or data.get("repositoryOwner") or {}
                    proj = owner_node.get("projectV2")
                except GitHubError as exc:
                    if cursor is None and "could not resolve to an organization" in str(exc).lower():
                        owner_kind = "user"
                        data = self.graphql(USER_PROJECT_BOARD_QUERY, variables)
                        owner_node = data.get("user") or data.get("repositoryOwner") or {}
                        proj = owner_node.get("projectV2")
                    else:
                        raise
                if not isinstance(proj, dict) and cursor is None:
                    owner_kind = "user"
                    data = self.graphql(USER_PROJECT_BOARD_QUERY, variables)
                    owner_node = data.get("user") or data.get("repositoryOwner") or {}
                    proj = owner_node.get("projectV2")
            else:
                data = self.graphql(USER_PROJECT_BOARD_QUERY, variables)
                owner_node = data.get("user") or data.get("repositoryOwner") or {}
                proj = owner_node.get("projectV2")

            if not isinstance(proj, dict):
                raise GitHubError(
                    f"GitHub Project #{project_number} under '{project_owner}' not found "
                    "(ensure `gh auth refresh -s project` scope is granted)"
                )
            self._owner_kind_cache[owner_key] = owner_kind

            if schema is None:
                status_field = proj.get("field") or {}
                options = {
                    str(opt["name"]): str(opt["id"])
                    for opt in (status_field.get("options") or [])
                    if isinstance(opt, dict) and opt.get("name") and opt.get("id")
                }
                prio_field = proj.get("priorityField") or {}
                prio_options = {
                    str(opt["name"]): str(opt["id"])
                    for opt in (prio_field.get("options") or [])
                    if isinstance(opt, dict) and opt.get("name") and opt.get("id")
                }
                text_fields: dict[str, str] = {}
                for fnode in ((proj.get("fields") or {}).get("nodes") or []):
                    if not isinstance(fnode, dict):
                        continue
                    fname = str(fnode.get("name") or "").strip().lower()
                    fid = str(fnode.get("id") or "").strip()
                    if fname and fid:
                        text_fields[fname] = fid
                schema = ProjectSchema(
                    project_id=str(proj["id"]),
                    number=int(proj["number"]),
                    title=str(proj.get("title") or f"Project #{project_number}"),
                    url=str(proj.get("url") or ""),
                    status_field_id=str(status_field.get("id") or ""),
                    options_by_name=options,
                    priority_field_id=str(prio_field.get("id") or ""),
                    priority_options_by_name=prio_options,
                    text_fields_by_name=text_fields,
                )
                self._project_owner_by_id[schema.project_id] = project_owner

            items_conn = proj.get("items") or {}
            for node in items_conn.get("nodes") or []:
                if not isinstance(node, dict):
                    continue
                issue = node.get("content")
                if not isinstance(issue, dict) or not issue.get("number"):
                    continue
                issue_repo = issue.get("repository") or {}
                raw_issue_owner = str((issue_repo.get("owner") or {}).get("login") or "").strip()
                raw_issue_repo = str(issue_repo.get("name") or "").strip()
                issue_owner_lc = raw_issue_owner.lower()
                issue_repo_lc = raw_issue_repo.lower()

                alias = ""
                if allowed_repos is not None:
                    matched_alias = allowed_repos.get((issue_owner_lc, issue_repo_lc))
                    if matched_alias is None:
                        continue
                    alias = matched_alias
                else:
                    if repo_owner and issue_owner_lc and issue_owner_lc != repo_owner.lower():
                        continue
                    if repo_name and issue_repo_lc and issue_repo_lc != repo_name.lower():
                        continue

                labels = [
                    str(lbl["name"])
                    for lbl in ((issue.get("labels") or {}).get("nodes") or [])
                    if isinstance(lbl, dict) and lbl.get("name")
                ]
                if label_filter and label_filter not in labels:
                    continue

                field_val = node.get("fieldValueByName") or {}
                status_name = str(field_val.get("name") or "")
                col = match_column(status_name, column_map)
                if str(issue.get("state") or "").upper() == "CLOSED" and col != Column.DONE:
                    col = Column.DONE

                prio_val = node.get("priorityValue") or {}
                priority_name = str(prio_val.get("name") or "")

                raw_field_values: dict[str, str] = {}
                for fv in ((node.get("fieldValues") or {}).get("nodes") or []):
                    if not isinstance(fv, dict):
                        continue
                    f_info = fv.get("field") or {}
                    f_name = str(f_info.get("name") or "").strip().lower()
                    val_str = str(fv.get("text") or fv.get("name") or "").strip()
                    if f_name and val_str:
                        raw_field_values[f_name] = val_str

                project_train_fields: dict[str, str] = {}
                for tid, spec in (release_trains or {}).items():
                    pf_name = str(getattr(spec, "project_field", "") or "").strip().lower()
                    if pf_name and pf_name in raw_field_values:
                        project_train_fields[tid] = raw_field_values[pf_name]

                milestone_node = issue.get("milestone") or {}
                milestone_title = (
                    str(milestone_node.get("title") or "") if isinstance(milestone_node, dict) else ""
                )

                comments_conn = issue.get("comments") or {}
                raw_comments = [
                    {
                        "id": int(c["databaseId"]),
                        "body": str(c.get("body") or ""),
                        "created_at": str(c.get("createdAt") or ""),
                        "author": str((c.get("author") or {}).get("login") or ""),
                    }
                    for c in (comments_conn.get("nodes") or [])
                    if isinstance(c, dict) and c.get("databaseId") is not None
                ]
                winning = resolve_winning_claim(raw_comments)
                claim_cid = winning[0] if winning else None
                claim_meta = winning[1] if winning else None
                stale = bool(
                    claim_meta and claim_meta.state == "active" and is_lease_expired(claim_meta.lease_until)
                )

                body_text = str(issue.get("body") or "")
                raw_body_meta = decode_meta(body_text)
                deposit = (
                    DepositMeta.from_dict(raw_body_meta)
                    if raw_body_meta and raw_body_meta.get("type") == "deposit"
                    else None
                )
                target_repo = (
                    (claim_meta.target_repo if claim_meta and claim_meta.target_repo else "")
                    or (deposit.target_repo if deposit and deposit.target_repo else "")
                )

                assignees = [
                    str(a["login"])
                    for a in ((issue.get("assignees") or {}).get("nodes") or [])
                    if isinstance(a, dict) and a.get("login")
                ]
                title_text = str(issue.get("title") or "")
                binfo = extract_ticket_builds(
                    labels,
                    body_text,
                    title_text,
                    project_fields=project_train_fields,
                    trains=release_trains,
                    label_prefix=release_label_prefix,
                    baseline_label=release_baseline_label,
                )
                tickets.append(
                    Ticket(
                        number=int(issue["number"]),
                        title=title_text,
                        body=body_text,
                        state=str(issue.get("state") or "OPEN"),
                        url=str(issue.get("url") or ""),
                        author=str((issue.get("author") or {}).get("login") or ""),
                        assignees=assignees,
                        labels=labels,
                        priority=priority_name,
                        milestone=milestone_title,
                        target_repo=target_repo,
                        release_builds=binfo["primary_builds"],
                        column=col,
                        status_name=status_name,
                        project_item_id=str(node["id"]),
                        repo_owner=raw_issue_owner or repo_owner,
                        repo_name=raw_issue_repo or repo_name,
                        repo_alias=alias,
                        claim=claim_meta,
                        claim_comment_id=claim_cid,
                        stale=stale,
                        deposit=deposit,
                        comments_count=int(comments_conn.get("totalCount") or len(raw_comments)),
                        updated_at=str(issue.get("updatedAt") or ""),
                    )
                )

            page_info = items_conn.get("pageInfo") or {}
            if page_info.get("hasNextPage") and page_info.get("endCursor"):
                cursor = str(page_info["endCursor"])
            else:
                break

        assert schema is not None
        populate_dependency_waves(tickets)
        populate_file_overlaps(tickets)
        tickets.sort(key=lambda t: (t.repo_alias or t.repo_name, t.number))
        return schema, tickets

    def set_item_column(
        self,
        schema: ProjectSchema,
        project_item_id: str,
        column: Column,
        column_map: dict[str, str],
    ) -> bool:
        if not schema.status_field_id or not project_item_id:
            return False
        option_id = schema.option_id_for_column(column, column_map)
        if not option_id:
            return False
        self.graphql(
            SET_STATUS_MUTATION,
            {
                "projectId": schema.project_id,
                "itemId": project_item_id,
                "fieldId": schema.status_field_id,
                "optionId": option_id,
            },
        )
        return True

    def set_item_priority(
        self,
        schema: ProjectSchema,
        project_item_id: str,
        priority: str,
    ) -> bool:
        if not schema.priority_field_id or not project_item_id:
            return False
        option_id = schema.option_id_for_priority(priority)
        if not option_id:
            return False
        self.graphql(
            SET_STATUS_MUTATION,
            {
                "projectId": schema.project_id,
                "itemId": project_item_id,
                "fieldId": schema.priority_field_id,
                "optionId": option_id,
            },
        )
        return True

    def ensure_text_field(
        self,
        project_id: str,
        field_name: str,
        existing_field_id: str = "",
    ) -> str:
        """Ensure a ProjectV2 TEXT field exists and return its `fieldId`."""
        if existing_field_id:
            return existing_field_id
        if not project_id or not field_name.strip():
            return ""
        mutation = """
        mutation($projectId: ID!, $name: String!) {
          createProjectV2Field(
            input: {
              projectId: $projectId
              dataType: TEXT
              name: $name
            }
          ) {
            projectV2Field {
              ... on ProjectV2Field {
                id
                name
              }
            }
          }
        }
        """
        data = self.graphql(mutation, {"projectId": project_id, "name": field_name.strip()})
        fld = ((data.get("createProjectV2Field") or {}).get("projectV2Field")) or {}
        return str(fld.get("id") or "")

    def set_item_text_field(
        self,
        project_id: str,
        item_id: str,
        field_id: str,
        text: str,
    ) -> bool:
        """Set a ProjectV2 TEXT field value on an item."""
        if not project_id or not item_id or not field_id:
            return False
        mutation = """
        mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $text: String!) {
          updateProjectV2ItemFieldValue(
            input: {
              projectId: $projectId
              itemId: $itemId
              fieldId: $fieldId
              value: { text: $text }
            }
          ) {
            projectV2Item { id }
          }
        }
        """
        self.graphql(
            mutation,
            {
                "projectId": project_id,
                "itemId": item_id,
                "fieldId": field_id,
                "text": text,
            },
        )
        return True

    def ensure_single_select_options(
        self,
        field_id: str,
        field_name: str,
        desired_options: list[str],
        existing_options: dict[str, str],
        *,
        project_id: str = "",
    ) -> list[str]:
        """Ensure a ProjectV2 single-select field contains `desired_options`."""
        if not desired_options or (not field_id and not project_id):
            return []
        existing_lc = {k.lower() for k in existing_options}
        missing = [opt for opt in desired_options if opt.lower() not in existing_lc]
        if not missing:
            return []
        palette = ("RED", "ORANGE", "YELLOW", "BLUE", "GREEN", "PURPLE", "GRAY", "PINK")
        merged_names = list(existing_options.keys()) + missing
        options_input = [
            {
                "name": name,
                "color": palette[idx % len(palette)],
                "description": f"{field_name} {name}",
            }
            for idx, name in enumerate(merged_names)
        ]
        create_mutation = """
        mutation(
          $projectId: ID!,
          $name: String!,
          $options: [ProjectV2SingleSelectFieldOptionInput!]!
        ) {
          createProjectV2Field(
            input: {
              projectId: $projectId
              dataType: SINGLE_SELECT
              name: $name
              singleSelectOptions: $options
            }
          ) {
            projectV2Field {
              ... on ProjectV2SingleSelectField {
                id
                options { id name }
              }
            }
          }
        }
        """
        if not field_id and project_id:
            self.graphql(
                create_mutation,
                {
                    "projectId": project_id,
                    "name": field_name,
                    "options": options_input,
                },
            )
            return missing
        mutation = """
        mutation($fieldId: ID!, $name: String!, $options: [ProjectV2SingleSelectFieldOptionInput!]!) {
          updateProjectV2Field(
            input: {
              fieldId: $fieldId
              name: $name
              singleSelectOptions: $options
            }
          ) {
            projectV2Field {
              ... on ProjectV2SingleSelectField {
                id
                options { id name }
              }
            }
          }
        }
        """
        try:
            self.graphql(
                mutation,
                {
                    "fieldId": field_id,
                    "name": field_name,
                    "options": options_input,
                },
            )
        except GitHubError:
            if not existing_options and project_id:
                del_mut = (
                    "mutation($fieldId: ID!) { "
                    "deleteProjectV2Field(input: { fieldId: $fieldId }) { clientMutationId } }"
                )
                self.graphql(del_mut, {"fieldId": field_id})
                self.graphql(
                    create_mutation,
                    {
                        "projectId": project_id,
                        "name": field_name,
                        "options": options_input,
                    },
                )
            else:
                raise
        return missing

    def create_issue(
        self,
        owner: str,
        repo: str,
        title: str,
        body: str = "",
        labels: list[str] | None = None,
        milestone_number: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        if milestone_number is not None:
            payload["milestone"] = milestone_number
        return self._gh_json(["api", "-X", "POST", f"repos/{owner}/{repo}/issues"], payload=payload)

    def update_issue(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        title: str | None = None,
        body: str | None = None,
        labels: list[str] | None = None,
        milestone_number: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if title is not None:
            payload["title"] = title
        if body is not None:
            payload["body"] = body
        if labels is not None:
            payload["labels"] = labels
        if milestone_number is not None:
            payload["milestone"] = milestone_number if milestone_number > 0 else None
        if not payload:
            return self.get_issue(owner, repo, number)
        return self._gh_json(
            ["api", "-X", "PATCH", f"repos/{owner}/{repo}/issues/{number}"],
            payload=payload,
        )

    def list_labels(self, owner: str, repo: str) -> dict[str, dict[str, str]]:
        raw = self._gh_json(["api", f"repos/{owner}/{repo}/labels?per_page=100"])
        if not isinstance(raw, list):
            return {}
        return {
            str(item["name"]): {
                "name": str(item["name"]),
                "color": str(item.get("color") or "").lower(),
                "description": str(item.get("description") or ""),
            }
            for item in raw
            if isinstance(item, dict) and item.get("name")
        }

    def upsert_label(
        self,
        owner: str,
        repo: str,
        name: str,
        color: str,
        description: str = "",
        existing: dict[str, dict[str, str]] | None = None,
    ) -> str:
        clean_color = color.strip().lstrip("#").lower() or "ededed"
        desc = description[:100]
        known = existing if existing is not None else self.list_labels(owner, repo)
        cur = known.get(name)
        if cur is None:
            self._gh_json(
                ["api", "-X", "POST", f"repos/{owner}/{repo}/labels"],
                payload={"name": name, "color": clean_color, "description": desc},
            )
            return "created"
        if cur.get("color") != clean_color or (desc and cur.get("description") != desc):
            self._gh_json(
                ["api", "-X", "PATCH", f"repos/{owner}/{repo}/labels/{name}"],
                payload={"new_name": name, "color": clean_color, "description": desc},
            )
            return "updated"
        return "unchanged"

    def list_milestones(self, owner: str, repo: str) -> dict[str, int]:
        raw = self._gh_json(["api", f"repos/{owner}/{repo}/milestones?state=all&per_page=100"])
        if not isinstance(raw, list):
            return {}
        out: dict[str, int] = {}
        for item in raw:
            if isinstance(item, dict) and item.get("title") and item.get("number"):
                title = str(item["title"]).strip()
                num = int(item["number"])
                out[title.lower()] = num
        return out

    def ensure_milestone(
        self,
        owner: str,
        repo: str,
        title: str,
        description: str = "",
        existing: dict[str, int] | None = None,
    ) -> tuple[int, str]:
        known = existing if existing is not None else self.list_milestones(owner, repo)
        key = title.strip().lower()
        if key in known:
            return known[key], "unchanged"
        created = self._gh_json(
            ["api", "-X", "POST", f"repos/{owner}/{repo}/milestones"],
            payload={"title": title.strip(), "description": description},
        )
        num = int(created["number"])
        if existing is not None:
            existing[key] = num
        return num, "created"

    def add_issue_to_project(self, project_id: str, issue_node_id: str) -> str:
        data = self.graphql(ADD_ITEM_MUTATION, {"projectId": project_id, "contentId": issue_node_id})
        return str(((data.get("addProjectV2ItemById") or {}).get("item") or {}).get("id") or "")

    def close_issue(self, owner: str, repo: str, number: int) -> None:
        self._gh_json(
            ["api", "-X", "PATCH", f"repos/{owner}/{repo}/issues/{number}"],
            payload={"state": "closed", "state_reason": "completed"},
        )

    def assign_issue(self, owner: str, repo: str, number: int, login: str) -> None:
        self._gh_json(
            ["api", "-X", "POST", f"repos/{owner}/{repo}/issues/{number}/assignees"],
            payload={"assignees": [login]},
        )

    def unassign_issue(self, owner: str, repo: str, number: int, login: str) -> None:
        self._gh_json(
            ["api", "-X", "DELETE", f"repos/{owner}/{repo}/issues/{number}/assignees"],
            payload={"assignees": [login]},
        )

    def list_issue_comments(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]:
        raw = self._gh_json(["api", f"repos/{owner}/{repo}/issues/{number}/comments?per_page=100"])
        if not isinstance(raw, list):
            return []
        return [
            {
                "id": int(item["id"]),
                "body": str(item.get("body") or ""),
                "created_at": str(item.get("created_at") or ""),
                "author": str((item.get("user") or {}).get("login") or ""),
                "url": str(item.get("html_url") or ""),
            }
            for item in raw
            if isinstance(item, dict) and item.get("id") is not None
        ]

    def post_comment(self, owner: str, repo: str, number: int, body: str) -> dict[str, Any]:
        resp = self._gh_json(
            ["api", "-X", "POST", f"repos/{owner}/{repo}/issues/{number}/comments"],
            payload={"body": body},
        )
        return {
            "id": int(resp["id"]),
            "url": str(resp.get("html_url") or ""),
            "created_at": str(resp.get("created_at") or ""),
        }

    def patch_comment(self, owner: str, repo: str, comment_id: int, body: str) -> None:
        self._gh_json(
            ["api", "-X", "PATCH", f"repos/{owner}/{repo}/issues/comments/{comment_id}"],
            payload={"body": body},
        )

    def delete_comment(self, owner: str, repo: str, comment_id: int) -> None:
        proc, _ = self._run_api(
            ["api", "-X", "DELETE", f"repos/{owner}/{repo}/issues/comments/{comment_id}"],
            None,
        )
        if proc.returncode != 0 and "404" not in (proc.stderr or ""):
            raise GitHubError(f"failed to delete comment {comment_id}: {proc.stderr.strip()}")

    def get_issue(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        return self._gh_json(["api", f"repos/{owner}/{repo}/issues/{number}"])

    def fetch_issues_poll_snapshot(
        self,
        owner: str,
        repo: str,
        numbers: list[int],
    ) -> dict[int, tuple[str, list[dict[str, Any]] | None]]:
        """Batch-fetch `(state, recent_comments)` for up to 25 issues per GraphQL query (0 REST calls)."""
        unique_nums = sorted({int(n) for n in numbers if int(n) > 0})
        if not unique_nums:
            return {}
        out: dict[int, tuple[str, list[dict[str, Any]] | None]] = {}
        batch_size = 25
        for i in range(0, len(unique_nums), batch_size):
            chunk = unique_nums[i : i + batch_size]
            fields = "\n".join(
                f"    i_{num}: issue(number: {num}) {{\n"
                "      state\n"
                "      comments(last: 30) {\n"
                "        nodes {\n"
                "          databaseId\n"
                "          body\n"
                "          createdAt\n"
                "          url\n"
                "          author { login }\n"
                "        }\n"
                "      }\n"
                "    }"
                for num in chunk
            )
            query = (
                "query($owner: String!, $repo: String!) {\n"
                "  repository(owner: $owner, name: $repo) {\n"
                f"{fields}\n"
                "  }\n"
                "}"
            )
            data = self.graphql(query, {"owner": owner, "repo": repo})
            repo_node = data.get("repository")
            if not isinstance(repo_node, dict):
                continue
            for num in chunk:
                inode = repo_node.get(f"i_{num}")
                if not isinstance(inode, dict):
                    out[num] = ("MISSING", None)
                    continue
                st = str(inode.get("state") or "OPEN").upper()
                raw_nodes = ((inode.get("comments") or {}).get("nodes")) or []
                parsed_comments: list[dict[str, Any]] = []
                for cnode in raw_nodes:
                    if not isinstance(cnode, dict) or cnode.get("databaseId") is None:
                        continue
                    parsed_comments.append(
                        {
                            "id": int(cnode["databaseId"]),
                            "body": str(cnode.get("body") or ""),
                            "created_at": str(cnode.get("createdAt") or ""),
                            "author": str((cnode.get("author") or {}).get("login") or ""),
                            "url": str(cnode.get("url") or ""),
                        }
                    )
                out[num] = (st, parsed_comments)
        return out

    @staticmethod
    def _collab_disk_cache_path(owner: str, repo: str) -> Path:
        explicit = os.environ.get("HERDR_KANBAN_CACHE_DIR")
        if explicit:
            root = Path(explicit)
        elif os.environ.get("HERDR_KANBAN_STATE_DIR"):
            root = Path(os.environ["HERDR_KANBAN_STATE_DIR"]) / "cache"
        else:
            xdg = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
            root = Path(xdg) / "herdr-kanban"
        safe_owner = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in owner.lower())
        safe_repo = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in repo.lower())
        return root / f"collaborators-{safe_owner}-{safe_repo}.json"

    def trusted_collaborators(self, owner: str, repo: str, extra: list[str] | None = None) -> set[str]:
        key = (owner.lower(), repo.lower())
        if key not in self._collab_cache:
            disk_path = self._collab_disk_cache_path(owner, repo)
            loaded: set[str] | None = None
            if disk_path.is_file():
                try:
                    cached_raw = json.loads(disk_path.read_text(encoding="utf-8"))
                    fetched_at = float(cached_raw.get("fetched_at") or 0.0)
                    logins = cached_raw.get("logins")
                    if isinstance(logins, list) and (time.time() - fetched_at) < 3600.0:
                        loaded = {str(x).lower() for x in logins if str(x).strip()}
                except (OSError, ValueError, TypeError):
                    loaded = None

            if loaded is not None:
                self._collab_cache[key] = loaded
            else:
                trusted = {owner.lower()}
                try:
                    trusted.add(self.current_user().lower())
                except GitHubError:
                    pass
                proc, _ = self._run_api(
                    ["api", f"repos/{owner}/{repo}/collaborators?per_page=100"],
                    None,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    try:
                        rows = json.loads(proc.stdout)
                        if isinstance(rows, list):
                            for row in rows:
                                if isinstance(row, dict) and row.get("login"):
                                    trusted.add(str(row["login"]).lower())
                    except ValueError:
                        pass
                self._collab_cache[key] = trusted
                try:
                    disk_path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = disk_path.with_name(f".{disk_path.name}.{os.getpid()}.tmp")
                    tmp.write_text(
                        json.dumps(
                            {"fetched_at": time.time(), "logins": sorted(trusted)},
                            indent=2,
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    os.replace(tmp, disk_path)
                except OSError:
                    pass
        combined = set(self._collab_cache[key])
        for login in extra or []:
            if login.strip():
                combined.add(login.strip().lower())
        return combined
