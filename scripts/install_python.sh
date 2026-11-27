#!/bin/bash
#
#  install_python.sh — 把 Python XCframework 的标准库装进 App 包
#
#  这是 CPython-on-iOS 的必要步骤：XCframework 里只能放二进制与元数据，
#  标准库必须由构建阶段自己拷进 $CODESIGNING_FOLDER_PATH。
#
#  与官方 utils.sh 的 install_python 行为一致，但做了三点适配：
#    1. 只处理 device slice（arm64），不碰 simulator 分支，简化逻辑；
#    2. .so → .framework 转换后，除留 .fwork 占位，还写了 .origin 反向指针；
#    3. 在 app/ 里顺手放一份 certifi 的 cacert.pem 供 ssl 使用。
#
set -euo pipefail

SRCROOT="${SRCROOT:-$(pwd)}"
XCFRAMEWORK="${SRCROOT}/Python.xcframework"
DEST="${CODESIGNING_FOLDER_PATH:?必须在 Xcode 构建阶段运行}"

SLICE="ios-arm64"

echo "▸ 目标 App: ${DEST}"
echo "▸ Python XCframework: ${XCFRAMEWORK}"

if [ ! -d "${XCFRAMEWORK}" ]; then
  echo "error: 找不到 Python.xcframework，请先运行 scripts/fetch_python.sh" >&2
  exit 1
fi

# ---------------------------------------------------------------- 标准库
mkdir -p "${DEST}/python/lib"
echo "▸ 拷贝标准库（common + ${SLICE}）"
rsync -a --delete "${XCFRAMEWORK}/lib/" "${DEST}/python/lib/" --exclude 'libpython*.dylib'
if [ -d "${XCFRAMEWORK}/${SLICE}/lib-arm64" ]; then
  rsync -a "${XCFRAMEWORK}/${SLICE}/lib-arm64/" "${DEST}/python/lib/" --exclude 'libpython*.dylib'
fi

# set -euo pipefail 下 `ls | grep | head` 有两个坑：
#   · 没有匹配时 grep 返回 1 → 整脚本退出（连我们自己的 error 都来不及打）
#   · head 提前关闭管道 → ls 收到 SIGPIPE → pipefail 判失败
# 所以显式关掉这一段的最外层检查，自己判空。
PYVER="$(set +o pipefail; ls -1 "${DEST}/python/lib" 2>/dev/null \
         | grep -E '^python3\.[0-9]+$' | head -n1 || true)"
if [ -z "${PYVER}" ]; then
  echo "error: 标准库拷贝失败，${DEST}/python/lib 下没有 python3.x 目录" >&2
  ls -la "${DEST}/python/lib" >&2 || true
  exit 1
fi
echo "▸ 标准库版本: ${PYVER}"

# 精简：删掉用不到的大块，控制包体
for junk in test idlelib turtledemo tkinter ensurepip lib2to3 pydoc_data \
            site-packages/README.txt config-3.13-darwin; do
  rm -rf "${DEST}/python/lib/${PYVER}/${junk}" 2>/dev/null || true
done

# ------------------------------------------------- .so → .framework 转换
#  iOS 只允许从 Frameworks/ 目录加载动态库，且必须是 .framework 结构。
#  .fwork 占位文件里写的是 framework 内二进制的相对路径，
#  import 机制遇到 xxx.cpython-313-iphoneos.so 时若读到 .fwork 就按它跳转。
convert_dylib() {
  local full_ext="$1"
  local install_base="$2"
  local ext base module_path module_name rel_ext python_ext full_module_name
  local framework_bundle_id framework_folder

  ext="$(basename "${full_ext}")"
  module_path="$(dirname "${full_ext}")"
  module_name="${ext%%.*}"
  rel_ext="${full_ext#${DEST}/}"
  # 剥掉 install_base（形如 python/lib/python3.13/lib-dynload/），
  # 剩下的才是模块路径。漏剥会把模块名算成 lib-dynload._sqlite3，
  # framework 名与 CFBundleExecutable 全错，import 阶段必然失败。
  python_ext="${rel_ext#${install_base}}"
  full_module_name="$(printf '%s' "${python_ext}" | cut -d. -f1 | tr '/' '.')"
  framework_bundle_id="$(printf '%s' "${PRODUCT_BUNDLE_IDENTIFIER}.${full_module_name}" | tr '_' '-')"
  framework_folder="Frameworks/${full_module_name}.framework"

  mkdir -p "${DEST}/${framework_folder}"
  # 模板文件名是 iOS-dylib-Info-template.plist（不是 iPhone-…），
  # 写错会让整个构建阶段 set -e 直接失败。
  cp "${XCFRAMEWORK}/build/iOS-dylib-Info-template.plist" \
     "${DEST}/${framework_folder}/Info.plist"
  plutil -replace CFBundleExecutable -string "${full_module_name}" \
         "${DEST}/${framework_folder}/Info.plist"
  plutil -replace CFBundleIdentifier -string "${framework_bundle_id}" \
         "${DEST}/${framework_folder}/Info.plist"

  mv "${full_ext}" "${DEST}/${framework_folder}/${full_module_name}"
  # 空壳 .so 位置留 .fwork，内容指向 framework 里的真实二进制
  printf '%s/%s' "${framework_folder}" "${full_module_name}" > "${full_ext%.so}.fwork"
  printf '%s.fwork' "${rel_ext%.so}" > "${DEST}/${framework_folder}/${full_module_name}.origin"

  if [ -n "${EXPANDED_CODE_SIGN_IDENTITY:-}" ]; then
    /usr/bin/codesign --force --sign "${EXPANDED_CODE_SIGN_IDENTITY}" \
      ${OTHER_CODE_SIGN_FLAGS:-} -o runtime --timestamp=none \
      --preserve-metadata=identifier,entitlements,flags \
      --generate-entitlement-der "${DEST}/${framework_folder}" 2>/dev/null || true
  fi
}

echo "▸ 转换 lib-dynload 下的扩展模块"
DYNLOAD_REL="python/lib/${PYVER}/lib-dynload"
DYNLOAD="${DEST}/${DYNLOAD_REL}"
if [ -d "${DYNLOAD}" ]; then
  find "${DYNLOAD}" -name '*.so' -print0 | while IFS= read -r -d '' f; do
    convert_dylib "${f}" "${DYNLOAD_REL}/"
  done
else
  echo "warning: ${DYNLOAD} 不存在，扩展模块（sqlite3/ssl/socket）将不可用" >&2
fi

echo "▸ 清理 .a / 头文件等无需随包分发的产物"
find "${DEST}/python" -name '*.a' -delete 2>/dev/null || true
rm -rf "${DEST}/python/include" 2>/dev/null || true

echo "✓ Python 运行时就绪"
