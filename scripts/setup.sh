#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
cd "$repo_root"

if ! command -v uv >/dev/null 2>&1; then
  echo "Error: uv is required. Install it from https://docs.astral.sh/uv/" >&2
  exit 1
fi

uv sync --frozen

if [[ ! -e config.yaml ]]; then
  umask 077
  cp config.example.yaml config.yaml
  echo "Created private config.yaml from config.example.yaml."
else
  echo "Existing config.yaml left unchanged."
fi
chmod 600 "$(readlink -f config.yaml)" 2>/dev/null || true
mkdir -p private
chmod 700 private 2>/dev/null || true

uv run python scripts/doctor.py --config config.yaml

echo
echo "Next: authenticate once with: uv run python scripts/selenium_live_monitor.py --config config.yaml --headless-off"
