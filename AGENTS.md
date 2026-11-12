# Agent notes

Herdr plugin: multi-colleague, multi-agent Kanban collaboration on a shared Git branch,
backed by GitHub Projects v2 and Issues.

Read first: `docs/spec.md` (what to build) and `docs/adr/` (why). Do not contradict an
accepted ADR silently; propose a superseding ADR instead.

- Manifest: `herdr-plugin.toml`.
- Plugin processes run with the plugin root as cwd and get `HERDR_BIN_PATH`,
  `HERDR_PLUGIN_ID`, `HERDR_PLUGIN_ROOT`, `HERDR_PLUGIN_CONFIG_DIR`, `HERDR_PANE_ID`.
  Commands are argv arrays, no shell expansion.
- Agent harnesses run inside ephemeral worktrees and load `skills/herdr-kanban/SKILL.md`.
- Python 3.11+ standard library only at runtime (ADR-0006). Dev tools via `uv`.
- Module boundaries (`docs/spec.md` §7) are enforced by `import-linter`; keep them green.

Dev loop:
- `uv run pytest`
- `uv run ruff check src tests`
- `PYTHONPATH=src uv run lint-imports`
- `herdr plugin link "$PWD"`, then `herdr plugin log list --plugin herdr-kanban`.
