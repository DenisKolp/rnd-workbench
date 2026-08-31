#!/bin/zsh
set -euo pipefail

project_dir="$1"
cd "$project_dir"
exec "$project_dir/.venv/bin/python" \
  -m voice_assistant.ui_backend \
  --config "$project_dir/config.toml" \
  --data "$project_dir/data/assistant.sqlite3"
