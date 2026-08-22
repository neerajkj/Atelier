#!/bin/sh
#
# Atelier Runner Script
#
set -e

SCRIPT_DIR="$(dirname "$0")"
PYTHONSAFEPATH=1 PYTHONPATH="$SCRIPT_DIR" exec uv run \
  --project "$SCRIPT_DIR" \
  --quiet \
  -m atelier.main \
  "$@"
