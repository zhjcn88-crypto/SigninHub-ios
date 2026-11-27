#!/bin/bash
#
#  fetch_python.sh — 下载并放置 CPython for iOS 运行时
#
#  用 beeware/Python-Apple-support 的官方预编译包（MIT/PSF 授权）。
#  产物是 Python.xcframework，放在 ios/ 目录下供 Xcode 引用。
#
set -euo pipefail

PY_MINOR="${PY_MINOR:-3.13}"
PY_TAG="${PY_TAG:-3.13-b18}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${ROOT}/ios/Python.xcframework"
CACHE="${ROOT}/.cache"

URL="https://github.com/beeware/Python-Apple-support/releases/download/${PY_TAG}/Python-${PY_MINOR}-iOS-support.${PY_TAG#*-}.tar.gz"
TARBALL="${CACHE}/python-ios-${PY_TAG}.tar.gz"

echo "▸ 运行时来源: ${URL}"

if [ -d "${DEST}" ]; then
  echo "✓ 已存在 ${DEST}，跳过下载"
  exit 0
fi

mkdir -p "${CACHE}"
if [ ! -f "${TARBALL}" ]; then
  echo "▸ 下载中（约 34 MB）…"
  curl -fL --retry 4 --retry-delay 3 -o "${TARBALL}" "${URL}"
fi

echo "▸ 解包到 ios/"
TMP="$(mktemp -d)"
tar -xzf "${TARBALL}" -C "${TMP}" Python.xcframework
mv "${TMP}/Python.xcframework" "${DEST}"
rm -rf "${TMP}"

echo "✓ 运行时就绪:"
du -sh "${DEST}"
ls -1 "${DEST}"
