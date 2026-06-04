#!/usr/bin/env bash

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  echo "Use 'source tools/dev-env.sh' to update the current shell environment." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=runtime.sh
source "$SCRIPT_DIR/runtime.sh"
odaily_setup_runtime_env "$(odaily_repo_root)"

python() {
  "$ODAILY_PYTHON" "$@"
}

pytest() {
  mkdir -p "$ODAILY_REPO_ROOT/.pytest-tmp"
  TMPDIR="$ODAILY_REPO_ROOT/.pytest-tmp" \
    "$ODAILY_PYTHON" -m pytest -s "--basetemp=$ODAILY_REPO_ROOT/.pytest-tmp" "$@"
}

node() {
  if [ -z "${ODAILY_NODE:-}" ]; then
    echo "[odaily-runtime] unable to find Node.js runtime" >&2
    return 1
  fi
  "$ODAILY_NODE" "$@"
}

npm() {
  "$(odaily_resolve_npm)" "$@"
}

npx() {
  "$(odaily_resolve_npx)" "$@"
}
