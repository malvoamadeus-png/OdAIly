#!/usr/bin/env bash

odaily_repo_root() {
  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  cd "$script_dir/.." && pwd
}

odaily_die() {
  echo "[odaily-runtime] $*" >&2
  return 1
}

odaily_has_command() {
  command -v "$1" >/dev/null 2>&1
}

odaily_find_codex_python() {
  local candidate
  for candidate in /mnt/c/Users/*/.cache/codex-runtimes/*/dependencies/python/python.exe; do
    if [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

odaily_find_codex_node() {
  local candidate
  for candidate in /mnt/c/Users/*/.cache/codex-runtimes/*/dependencies/node/bin/node.exe; do
    if [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

odaily_find_codex_node_modules() {
  local candidate
  for candidate in /mnt/c/Users/*/.cache/codex-runtimes/*/dependencies/node/node_modules; do
    if [ -d "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

odaily_find_venv_site_packages() {
  local root="${1:-$(odaily_repo_root)}"
  local candidate
  for candidate in "$root"/.venv/lib/python*/site-packages; do
    if [ -d "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

odaily_resolve_python() {
  local root="${1:-$(odaily_repo_root)}"
  if [ -x "$root/.venv/bin/python" ]; then
    printf '%s\n' "$root/.venv/bin/python"
    return 0
  fi
  if [ -x "$root/.venv/bin/python3" ]; then
    printf '%s\n' "$root/.venv/bin/python3"
    return 0
  fi
  if odaily_has_command python3; then
    command -v python3
    return 0
  fi
  if odaily_has_command python; then
    command -v python
    return 0
  fi
  if odaily_find_codex_python >/dev/null 2>&1; then
    odaily_find_codex_python
    return 0
  fi
  odaily_die "unable to find Python runtime"
}

odaily_resolve_node() {
  if odaily_has_command node; then
    command -v node
    return 0
  fi
  if odaily_find_codex_node >/dev/null 2>&1; then
    odaily_find_codex_node
    return 0
  fi
  odaily_die "unable to find Node.js runtime"
}

odaily_resolve_npm() {
  local node_path
  if odaily_has_command npm; then
    command -v npm
    return 0
  fi
  node_path="$(odaily_resolve_node 2>/dev/null || true)"
  if [ -n "$node_path" ]; then
    local node_dir
    node_dir="$(dirname "$node_path")"
    if [ -x "$node_dir/npm" ]; then
      printf '%s\n' "$node_dir/npm"
      return 0
    fi
    if [ -x "$node_dir/npm.cmd" ]; then
      printf '%s\n' "$node_dir/npm.cmd"
      return 0
    fi
  fi
  odaily_die "unable to find npm runtime"
}

odaily_resolve_npx() {
  local node_path
  if odaily_has_command npx; then
    command -v npx
    return 0
  fi
  node_path="$(odaily_resolve_node 2>/dev/null || true)"
  if [ -n "$node_path" ]; then
    local node_dir
    node_dir="$(dirname "$node_path")"
    if [ -x "$node_dir/npx" ]; then
      printf '%s\n' "$node_dir/npx"
      return 0
    fi
    if [ -x "$node_dir/npx.cmd" ]; then
      printf '%s\n' "$node_dir/npx.cmd"
      return 0
    fi
  fi
  odaily_die "unable to find npx runtime"
}

odaily_prepend_path() {
  local dir="$1"
  if [ -z "$dir" ] || [ ! -d "$dir" ]; then
    return 0
  fi
  case ":${PATH:-}:" in
    *":$dir:"*) ;;
    *) PATH="$dir${PATH:+:$PATH}" ;;
  esac
}

odaily_prepend_env_path() {
  local var_name="$1"
  local dir="$2"
  local current
  if [ -z "$dir" ] || [ ! -d "$dir" ]; then
    return 0
  fi
  current="${!var_name:-}"
  case ":$current:" in
    *":$dir:"*) ;;
    *) printf -v "$var_name" '%s' "$dir${current:+:$current}" ;;
  esac
}

odaily_setup_runtime_env() {
  local root="${1:-$(odaily_repo_root)}"
  local python_path node_path site_packages node_modules

  python_path="$(odaily_resolve_python "$root")" || return 1
  node_path="$(odaily_resolve_node 2>/dev/null || true)"
  site_packages="$(odaily_find_venv_site_packages "$root" 2>/dev/null || true)"
  node_modules="$(odaily_find_codex_node_modules 2>/dev/null || true)"

  export ODAILY_REPO_ROOT="$root"
  export ODAILY_PYTHON="$python_path"
  export ODAILY_NODE="${node_path:-}"
  export ODAILY_VENV_SITE_PACKAGES="${site_packages:-}"
  export ODAILY_NODE_MODULES="${node_modules:-}"

  odaily_prepend_path "$root/.venv/bin"
  odaily_prepend_path "$(dirname "$python_path")"
  if [ -n "$node_path" ]; then
    odaily_prepend_path "$(dirname "$node_path")"
  fi
  export PATH

  if [ -n "$site_packages" ]; then
    odaily_prepend_env_path PYTHONPATH "$site_packages"
  fi
  odaily_prepend_env_path PYTHONPATH "$root/backend"
  odaily_prepend_env_path PYTHONPATH "$root"
  export PYTHONPATH

  if [ -n "$node_modules" ]; then
    odaily_prepend_env_path NODE_PATH "$node_modules"
    export NODE_PATH
  fi
}

