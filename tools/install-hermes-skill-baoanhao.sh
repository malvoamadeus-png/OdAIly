#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ZIP_PATH="${ROOT_DIR}/third-party/保安号/v1.0.5.zip"
SKILLS_DIR="${HERMES_HOME:-$HOME/.hermes}/skills"

if [[ ! -f "$ZIP_PATH" ]]; then
  echo "缺少安装包: $ZIP_PATH" >&2
  echo "请先从 JakeyCha 获取 v1.0.5.zip 并放到 third-party/保安号/ 目录。" >&2
  exit 1
fi

mkdir -p "$SKILLS_DIR"
unzip -o "$ZIP_PATH" -d "$SKILLS_DIR"

if command -v hermes >/dev/null 2>&1; then
  echo
  echo "已解压到 $SKILLS_DIR"
  echo "当前技能列表："
  hermes skills list 2>/dev/null | rg -i "保安|baoan" || hermes skills list 2>/dev/null | head -20
else
  echo "已解压到 $SKILLS_DIR"
  echo "提示: 未找到 hermes 命令，请先安装 Hermes Agent。"
fi
