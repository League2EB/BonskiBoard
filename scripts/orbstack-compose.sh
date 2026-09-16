#!/bin/sh
# Backward-compatible entry point. Prefer scripts/compose.sh for new commands.

script_dir=$(CDPATH= cd -P "$(dirname "$0")" && pwd)
exec sh "$script_dir/compose.sh" "$@"
