# ADR-0006: Python 3.11+ standard library at runtime; `gh`, `git`, and `herdr` as external CLIs

- Status: Accepted
- Date: 2026-11-12

## Context

Herdr plugins install directly from GitHub and run `[[build]]`. Requiring virtualenvs, compiled
wheels, or bundled OAuth secrets slows installation and creates cross-platform breakage.

## Decision

- Implement `herdr-kanban` in **Python 3.11+ standard library only** at runtime (`tomllib`,
  `termios`, `tty`, `select`, `fcntl`, `urllib`, `json`, `dataclasses`).
- Delegate GitHub GraphQL/REST and auth to `gh`, Git/worktree operations to `git`, and terminal
  multiplexer orchestration to `herdr`.
- Enforce strict architectural boundaries between `model`/`config`, isolated adapters (`github`,
  `git_ops`, `herdr`), `engine`, and UI/CLI surfaces via `import-linter` in `uv` dev dependencies.

## Consequences

- Instant, zero-package install anywhere `python3 >= 3.11`, `git`, `gh`, and `herdr` are installed.
- Every external boundary (`gh`, `git`, `herdr`) is behind a tiny subprocess seam that can be
  deterministically tested offline with fixtures.
