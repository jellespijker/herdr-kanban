#!/bin/sh
# Build step run by `herdr plugin install`: verify platform & external CLIs.
set -eu
case "$(uname -s)" in
  Linux|Darwin) ;;
  *) echo "herdr-kanban supports Linux and macOS; $(uname -s) is not supported yet." >&2; exit 1 ;;
esac

chmod +x bin/herdr-kanban
python3 bin/herdr-kanban install-skill --refresh || true

echo "herdr-kanban installed. Checking this machine:"
python3 bin/herdr-kanban doctor --brief || true
python3 bin/herdr-kanban doctor --notify >/dev/null 2>&1 || true
