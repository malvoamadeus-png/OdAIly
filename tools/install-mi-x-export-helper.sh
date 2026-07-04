#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ZIP_PATH="${ROOT_DIR}/third-party/老Mi-X导出助手/老Mi-X导出助手1.0.zip"
OUT_DIR="${ROOT_DIR}/third-party/老Mi-X导出助手/unpacked"

if [[ ! -f "$ZIP_PATH" ]]; then
  echo "缺少安装包: $ZIP_PATH" >&2
  echo "请先从 JakeyCha 获取 老Mi-X导出助手1.0.zip 并放到 third-party/老Mi-X导出助手/ 目录。" >&2
  exit 1
fi

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"
unzip -o "$ZIP_PATH" -d "$OUT_DIR"

MANIFEST="$(find "$OUT_DIR" -name manifest.json -print -quit || true)"
if [[ -z "$MANIFEST" ]]; then
  echo "解压完成，但未找到 manifest.json。请检查 zip 内容或联系 JakeyCha。" >&2
  exit 1
fi

EXT_DIR="$(dirname "$MANIFEST")"
echo "解压完成。"
echo "Chrome 加载目录: $EXT_DIR"
echo
echo "下一步:"
echo "1. 打开 chrome://extensions/"
echo "2. 开启开发者模式"
echo "3. 加载已解压的扩展程序，选择上面的目录"
