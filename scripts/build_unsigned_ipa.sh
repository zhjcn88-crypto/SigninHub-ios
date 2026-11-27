#!/bin/bash
#
#  build_unsigned_ipa.sh — 构建 TrollStore 可装的无签名 .ipa
#
#  TrollStore 只需要「未签名 .app 打进 Payload/ 的 zip」，
#  不需要证书、不需要描述文件、不需要 Apple ID。
#
#  关键：构建时用 CODE_SIGNING_ALLOWED=NO，让 Xcode 跳过签名，
#  但保留 install_python.sh 里的 .so → .framework 转换（那步与签名无关）。
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCHEME="SigninHub"
PROJECT="${ROOT}/ios/SigninHub.xcodeproj"
CONFIG="${CONFIG:-Release}"
OUT="${ROOT}/build"
ARCHIVE_APP="${OUT}/Build/Products/${CONFIG}-iphoneos/${SCHEME}.app"
IPA="${OUT}/${SCHEME}.ipa"

echo "════════════════════════════════════════"
echo " SigninHub → 无签名 IPA（TrollStore）"
echo "════════════════════════════════════════"

# 0) 前置检查
command -v xcodebuild >/dev/null || { echo "error: 需要 Xcode" >&2; exit 1; }
if [ ! -d "${ROOT}/ios/Python.xcframework" ]; then
  echo "▸ 缺少 Python.xcframework，先执行 fetch_python.sh"
  "${ROOT}/scripts/fetch_python.sh"
fi
if [ ! -d "${ROOT}/ios/SigninHub/app/signbot" ]; then
  echo "▸ 缺少 app/，先执行 prepare_app.sh"
  "${ROOT}/scripts/prepare_app.sh"
fi

rm -rf "${OUT}"
mkdir -p "${OUT}"

# 1) 构建
#
# 用 -showBuildSettings 先问出 TARGET_BUILD_DIR / WRAPPER_NAME，
# 比硬猜 "Build/Products/Release-iphoneos" 可靠：
#   · CODE_SIGNING_ALLOWED=NO 时产物仍在该目录，但不同 Xcode 版本
#     对 CONFIGURATION_BUILD_DIR 的处理有差异；
#   · 一旦路径猜错，后面的完整性检查会以「缺少必需文件」误报，很难排查。
echo "▸ 解析构建输出目录"
BUILD_DIR="$(
  xcodebuild -project "${PROJECT}" -scheme "${SCHEME}" -configuration "${CONFIG}" \
    -sdk iphoneos -destination 'generic/platform=iOS' -showBuildSettings 2>/dev/null \
    | awk -F' = ' '/^ *TARGET_BUILD_DIR = /{d=$2} /^ *WRAPPER_NAME = /{w=$2} END{if(d&&w) print d"/"w}'
)"
if [ -n "${BUILD_DIR}" ]; then
  ARCHIVE_APP="${BUILD_DIR}"
  echo "  产物路径: ${ARCHIVE_APP}"
else
  echo "  (showBuildSettings 未取到，回退默认路径)"
fi

echo "▸ 构建 ${CONFIG}（跳过签名）"
xcodebuild \
  -project "${PROJECT}" \
  -scheme "${SCHEME}" \
  -configuration "${CONFIG}" \
  -sdk iphoneos \
  -destination 'generic/platform=iOS' \
  -derivedDataPath "${OUT}/DerivedData" \
  CONFIGURATION_BUILD_DIR="${OUT}/Build/Products/${CONFIG}-iphoneos" \
  CODE_SIGNING_ALLOWED=NO \
  CODE_SIGNING_REQUIRED=NO \
  CODE_SIGN_IDENTITY="" \
  EXPANDED_CODE_SIGN_IDENTITY="" \
  DEVELOPMENT_TEAM="" \
  PROVISIONING_PROFILE_SPECIFIER="" \
  ONLY_ACTIVE_ARCH=NO \
  build

# 兜底：若 showBuildSettings 给的路径不对，扫一遍 Build/Products 找 .app
# 注意：find | head -1 会让 find 收到 SIGPIPE，pipefail 下被误判为失败 →
# 用 set +o pipefail 局部关掉，或先收全再取第一行。
if [ ! -d "${ARCHIVE_APP}" ]; then
  CANDIDATES="$(find "${OUT}" -maxdepth 6 -type d -name "${SCHEME}.app" 2>/dev/null || true)"
  if [ -n "${CANDIDATES}" ]; then
    FOUND="$(printf '%s\n' "${CANDIDATES}" | sed -n '1p')"
    ARCHIVE_APP="${FOUND}"
    echo "▸ 实际产物: ${ARCHIVE_APP}"
  fi
fi

if [ ! -d "${ARCHIVE_APP}" ]; then
  echo "error: 构建产物不存在: ${ARCHIVE_APP}" >&2
  echo "       已搜索 ${OUT} 下的 ${SCHEME}.app" >&2
  exit 1
fi

# 2) 完整性自检（缺一个都装不上/跑不起来）
echo "▸ 校验包内容"
for must in "${ARCHIVE_APP}/${SCHEME}" \
            "${ARCHIVE_APP}/Frameworks/Python.framework/Python" \
            "${ARCHIVE_APP}/python/lib/python3.13/os.py" \
            "${ARCHIVE_APP}/app/signbot/server.py" \
            "${ARCHIVE_APP}/app/web/index.html"; do
  if [ ! -e "${must}" ]; then
    echo "error: 缺少必需文件: ${must}" >&2
    exit 1
  fi
done
echo "  ✓ 可执行文件 / Python 框架 / 标准库 / 后端代码 / 网页资源 齐全"

FRAMEWORK_COUNT=$(find "${ARCHIVE_APP}/Frameworks" -maxdepth 1 -name '*.framework' 2>/dev/null | wc -l | tr -d ' ')
echo "  ✓ Frameworks 数量: ${FRAMEWORK_COUNT}（含 Python 与扩展模块）"

# grep -q 提前退出会让上游 find 收到 SIGPIPE，pipefail 下被误判成失败。
# 用计数判断，不做短路退出。
FWORK_COUNT=$(find "${ARCHIVE_APP}/python/lib/python3.13/lib-dynload" -name '*.fwork' 2>/dev/null | wc -l | tr -d ' ')
if [ "${FWORK_COUNT}" -eq 0 ]; then
  echo "error: lib-dynload 下没有 .fwork 占位，扩展模块无法 import" >&2
  exit 1
fi
echo "  ✓ lib-dynload 占位文件 ${FWORK_COUNT} 个"

SO_LEFT=$(find "${ARCHIVE_APP}/python/lib/python3.13/lib-dynload" -name '*.so' 2>/dev/null | wc -l | tr -d ' ')
if [ "${SO_LEFT}" -ne 0 ]; then
  echo "error: 仍有 ${SO_LEFT} 个 .so 未转成 framework" >&2
  exit 1
fi
echo "  ✓ 无 .so 残留"

# 3) 打 IPA（Payload/ 结构）
#
# 用 ditto 而不是 zip：
#   · ditto 是 Apple 官方打包工具，能正确处理 HFS+/APFS 元数据、资源分支、
#     符号链接与文件权限；
#   · zip -y 会把符号链接原样存成链接，TrollStore/installd 在解包时可能
#     建不出目标，导致 Python.framework 里的链接（Python → Versions/Current/Python）
#     断掉。
#   · ditto -c -k --sequesterRsrc --keepParent 生成的 zip 与 Xcode 导出的一致。
echo "▸ 打包 Payload/"
STAGE="${OUT}/stage"
rm -rf "${STAGE}"
mkdir -p "${STAGE}/Payload"
ditto "${ARCHIVE_APP}" "${STAGE}/Payload/${SCHEME}.app"

# 去掉可能残留的签名产物，TrollStore 会自己重签
rm -rf "${STAGE}/Payload/${SCHEME}.app/_CodeSignature"
find "${STAGE}/Payload/${SCHEME}.app" -name 'embedded.mobileprovision' -delete 2>/dev/null || true

rm -f "${IPA}"
ditto -c -k --sequesterRsrc "${STAGE}" "${IPA}"
rm -rf "${STAGE}"

if [ ! -f "${IPA}" ]; then
  echo "error: IPA 未生成" >&2
  exit 1
fi

# 4) 校验产物
SIZE=$(du -h "${IPA}" | cut -f1)
SHA=$(shasum -a 256 "${IPA}" | cut -d' ' -f1)
echo ""
echo "════════════════════════════════════════"
echo " ✓ 构建完成"
echo "   ${IPA}"
echo "   大小: ${SIZE}"
echo "   SHA256: ${SHA}"
echo "════════════════════════════════════════"
echo ""
echo "安装：把 .ipa 传到 iPhone，用 TrollStore 打开 → Install。"
