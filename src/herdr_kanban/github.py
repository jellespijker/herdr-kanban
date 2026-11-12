"""Isolated GitHub Projects v2 (GraphQL) & Issues (REST) adapter via `gh` CLI (ADR-0001, ADR-0006)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .model import (
    Column,
    DepositMeta,
    Ticket,
    decode_meta,
    is_lease_expired,
    match_column,
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


PROJECT_BOARD_QUERY = """
query($owner: String!, $number: Int!, $cursor: String) {
  repositoryOwner(login: $owner) {
    login
    ... on Organization {
      projectV2(number: $number) {
        ...ProjectFields
      }
    }
    ... on User {
      projectV2(number: $number) {
        ...ProjectFields
      }
    }
  }
}

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
          assignees(first: 10) { nodes { login } }
          labels(first: 15) { nodes { name } }
          repository {
            name
            owner { login }
          }
          comments(last: 30) {
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
        proc = self._run(final_args, stdin_text)
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
        allowed_repos: dict[tuple[str, str], str] | None = None,
    ) -> tuple[ProjectSchema, list[Ticket]]:
        cursor: str | None = None
        schema: ProjectSchema | None = None
        tickets: list[Ticket] = []

        while True:
            data = self.graphql(
                PROJECT_BOARD_QUERY,
                {"owner": project_owner, "number": project_number, "cursor": cursor},
            )
            owner_node = data.get("repositoryOwner") or {}
            proj = owner_node.get("projectV2")
            if not isinstance(proj, dict):
                raise GitHubError(
                    f"GitHub Project #{project_number} under '{project_owner}' not found "
                    "(ensure `gh auth refresh -s project` scope is granted)"
                )

            if schema is None:
                status_field = proj.get("field") or {}
                options = {
                    str(opt["name"]): str(opt["id"])
                    for opt in (status_field.get("options") or [])
                    if isinstance(opt, dict) and opt.get("name") and opt.get("id")
                }
                schema = ProjectSchema(
                    project_id=str(proj["id"]),
                    number=int(proj["number"]),
                    title=str(proj.get("title") or f"Project #{project_number}"),
                    url=str(proj.get("url") or ""),
                    status_field_id=str(status_field.get("id") or ""),
                    options_by_name=options,
                )

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

                assignees = [
                    str(a["login"])
                    for a in ((issue.get("assignees") or {}).get("nodes") or [])
                    if isinstance(a, dict) and a.get("login")
                ]
                tickets.append(
                    Ticket(
                        number=int(issue["number"]),
                        title=str(issue.get("title") or ""),
                        body=body_text,
                        state=str(issue.get("state") or "OPEN"),
                        url=str(issue.get("url") or ""),
                        author=str((issue.get("author") or {}).get("login") or ""),
                        assignees=assignees,
                        labels=labels,
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

    def create_issue(
        self,
        owner: str,
        repo: str,
        title: str,
        body: str = "",
        labels: list[str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        return self._gh_json(["api", "-X", "POST", f"repos/{owner}/{repo}/issues"], payload=payload)

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
        proc = self._run(["api", "-X", "DELETE", f"repos/{owner}/{repo}/issues/comments/{comment_id}"], None)
        if proc.returncode != 0 and "404" not in (proc.stderr or ""):
            raise GitHubError(f"failed to delete comment {comment_id}: {proc.stderr.strip()}")

    def get_issue(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        return self._gh_json(["api", f"repos/{owner}/{repo}/issues/{number}"])

    def trusted_collaborators(self, owner: str, repo: str, extra: list[str] | None = None) -> set[str]:
        key = (owner.lower(), repo.lower())
        if key not in self._collab_cache:
            trusted = {owner.lower()}
            try:
                trusted.add(self.current_user().lower())
            except GitHubError:
                pass
            proc = self._run(["api", f"repos/{owner}/{repo}/collaborators?per_page=100"], None)
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
        combined = set(self._collab_cache[key])
        for login in extra or []:
            if login.strip():
                combined.add(login.strip().lower())
        return combined
