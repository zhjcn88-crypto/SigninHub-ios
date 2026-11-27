#!/bin/bash
#
#  prepare_app.sh — 从上游 SigninHub 仓库生成 ios/SigninHub/app/
#
#  做的事：
#    1. 复制 backend/signbot + backend/web 到 app/
#    2. 打 iOS 平台补丁（ctypes.wintypes 在非 Windows 上会抛 ValueError）
#    3. 生成 _ios_boot.py（CA / 数据目录 / 口令）
#
#  上游来源按顺序找：
#    $SIGNINHUB_SRC  →  vendor/SigninHub/backend  →  ../SigninHub-main/backend
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DST="${ROOT}/ios/SigninHub/app"

# ---- 定位上游 backend（找不到就自动下载）----
SRC="${SIGNINHUB_SRC:-}"
if [ -z "${SRC}" ]; then
  for cand in "${ROOT}/vendor/SigninHub/backend" \
              "${ROOT}/../SigninHub-main/backend"; do
    if [ -d "${cand}/signbot" ]; then
      SRC="${cand}"
      break
    fi
  done
fi

if [ -z "${SRC}" ] || [ ! -d "${SRC}/signbot" ]; then
  echo "▸ 没找到上游 backend，自动下载（约 3 MB）"
  REF="${SIGNINHUB_REF:-main}"
  TMPTAR="$(mktemp -t signinhub-upstream).tar.gz"
  TMPDIR_U="$(mktemp -d)"
  URL="https://codeload.github.com/Ayanguser/SigninHub/tar.gz/refs/heads/${REF}"
  echo "  来源: ${URL}"
  if ! curl -fL --retry 4 --retry-delay 2 -o "${TMPTAR}" "${URL}"; then
    echo "error: 下载失败。请检查网络，或手动 clone 到 vendor/SigninHub" >&2
    echo "       git clone --depth 1 https://github.com/Ayanguser/SigninHub vendor/SigninHub" >&2
    rm -f "${TMPTAR}"; rmdir "${TMPDIR_U}" 2>/dev/null || true
    exit 1
  fi
  tar -xzf "${TMPTAR}" -C "${TMPDIR_U}" --strip-components=1
  if [ ! -d "${TMPDIR_U}/backend/signbot" ]; then
    echo "error: 下载包里没有 backend/signbot" >&2
    exit 1
  fi
  mkdir -p "${ROOT}/vendor/SigninHub"
  rm -rf "${ROOT}/vendor/SigninHub/backend"
  mv "${TMPDIR_U}/backend" "${ROOT}/vendor/SigninHub/backend"
  SRC="${ROOT}/vendor/SigninHub/backend"
  rm -f "${TMPTAR}"
  rm -rf "${TMPDIR_U}"
  echo "✓ 已放到 vendor/SigninHub/backend"
fi

if [ ! -d "${SRC}/signbot" ]; then
  echo "error: 上游 backend 无效: ${SRC}" >&2
  exit 1
fi

# ---- 找可用的 python（macOS 上可能只有 python3，CI 里也有 python）
PYBIN="${PYTHON:-}"
if [ -z "${PYBIN}" ]; then
  for cand in python3 python; do
    if command -v "${cand}" >/dev/null 2>&1; then
      PYBIN="${cand}"
      break
    fi
  done
fi
if [ -z "${PYBIN}" ]; then
  echo "error: 找不到 python3，无法打补丁 / 生成引导" >&2
  exit 1
fi

echo "▸ 来源: ${SRC}"
echo "▸ Python: $(command -v "${PYBIN}")"

rm -rf "${DST}"
mkdir -p "${DST}"

cp -R "${SRC}/signbot" "${DST}/signbot"
cp -R "${SRC}/web" "${DST}/web"

# 清掉不该进包的产物
find "${DST}" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "${DST}" -name '*.pyc' -delete 2>/dev/null || true
rm -rf "${DST}/signbot/tools" 2>/dev/null || true

# ---- 平台补丁（必需：否则 import signbot 直接失败）
"${PYBIN}" "${ROOT}/scripts/patch_ios.py" "${DST}"

# ---- 生成运行时引导
"${PYBIN}" "${ROOT}/scripts/make_boot.py" "${DST}"

# 清掉上面步骤新产生的字节码（.pyc 不应进 App 包）
find "${DST}" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "${DST}" -name '*.pyc' -delete 2>/dev/null || true

# ---- 自检：补丁与引导都必须到位
"${PYBIN}" - "$DST" <<'PY'
import ast, sys
from pathlib import Path

dst = Path(sys.argv[1])
# 1) 语法全部能过
for py in dst.rglob("*.py"):
    try:
        ast.parse(py.read_text(encoding="utf-8"))
    except SyntaxError as e:
        raise SystemExit(f"[FAIL] {py}: {e}")

# 2) 补丁到位
for rel in ("signbot/vault.py", "signbot/qoder.py"):
    t = (dst / rel).read_text(encoding="utf-8")
    assert "iOS 移植补丁" in t, f"{rel} 未打补丁"
    assert "\nfrom ctypes import wintypes\n" not in t.split("try:")[0] + "\n", \
        f"{rel} 仍有顶层 wintypes 导入"

# 3) 引导到位
boot = dst / "_ios_boot.py"
assert boot.is_file(), "_ios_boot.py 缺失"
assert "prepare_server" in boot.read_text(encoding="utf-8")

# 4) 关键后端文件齐全
for rel in ("signbot/server.py", "signbot/runner.py", "signbot/config.py",
            "signbot/provider.py", "web/index.html", "web/static/app.js"):
    assert (dst / rel).is_file(), f"{rel} 缺失"

print(f"[ok] app/ 自检通过，{len(list(dst.rglob('*.py')))} 个 .py")
PY

echo "✓ app/ 就绪（$(find "${DST}" -type f | wc -l | tr -d ' ') 个文件）"
