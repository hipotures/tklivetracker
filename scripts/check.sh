#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    echo "Run TkLiveTracker's deterministic offline tests and compile checks."
    echo "Usage: ./scripts/check.sh"
    exit 0
fi

if [[ "$#" -ne 0 ]]; then
    echo "Error: unexpected arguments. Use --help for usage." >&2
    exit 2
fi

cd "$PROJECT_DIR"

export PYTHONDONTWRITEBYTECODE=1
uv run --frozen pytest -q

PYTHONPYCACHEPREFIX="${TMPDIR:-/tmp}/tklivetracker-compile-cache" \
    uv run --frozen python -m compileall -q \
    recorder \
    persistent_live_manager \
    web_monitor \
    modules \
    utils \
    selenium_supervisor.py \
    scripts
