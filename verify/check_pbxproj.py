#!/usr/bin/env python3
"""pbxproj 语义校验（用真正的 OpenStep plist 解析器）。

结构对但语义错是最难查的一类：引用了不存在的对象、Embed phase 忘了加、
HEADER_SEARCH_PATHS 没指到 Python headers —— 这些都要到编译期才炸。
"""
from __future__ import annotations

import sys

from _common import APP, PBX, XCFW, Report, require

r = Report("pbxproj 语义校验")

if not require(PBX):
    sys.exit(1)

try:
    from pbxproj import XcodeProject
except ImportError:
    print("\n需要 pbxproj 库：pip install pbxproj")
    print("（仅用于离线校验；没装也可以直接跑 xcodebuild）")
    sys.exit(0)

try:
    proj = XcodeProject.load(str(PBX))
except Exception as e:
    r.check("pbxproj 可被解析", False, f"{type(e).__name__}: {e}")
    sys.exit(r.finish())

r.check("pbxproj 可被解析", True)

targets = list(proj.objects.get_targets())
names = [getattr(t, "name", "?") for t in targets]
print("  targets:", names)
r.check("有 SigninHub target", "SigninHub" in names, str(names))

target = proj.get_target_by_name("SigninHub")
if target is None:
    sys.exit(r.finish())

r.check("target 类型是 application",
        target.productType == "com.apple.product-type.application",
        target.productType)

# ---- build phases
phases = []
for ph in target.buildPhases:
    obj = proj.objects[ph]
    phases.append((obj.isa, getattr(obj, "name", None) or obj.isa))
    print(f"  phase: {obj.isa:<28} {getattr(obj, 'name', '')}")
isas = [i for i, _ in phases]
for required in ("PBXSourcesBuildPhase", "PBXResourcesBuildPhase",
                 "PBXFrameworksBuildPhase", "PBXShellScriptBuildPhase",
                 "PBXCopyFilesBuildPhase"):
    r.check(f"有 {required}", required in isas)

# ---- Sources 文件都存在
def phase_files(isa, attr="files"):
    for ph in target.buildPhases:
        obj = proj.objects[ph]
        if obj.isa == isa:
            out = []
            for bf in getattr(obj, attr):
                b = proj.objects[bf]
                ref = proj.objects[b.fileRef]
                out.append(ref.path)
            return out
    return []

src_files = phase_files("PBXSourcesBuildPhase")
print("  Sources:", src_files)
r.check("Sources 有 main.m", "main.m" in src_files)
missing = [f for f in src_files if not (APP / f).is_file()]
r.check(f"Sources 的 {len(src_files)} 个文件都存在", not missing, str(missing))

# ---- Resources
res_files = phase_files("PBXResourcesBuildPhase")
print("  Resources:", res_files)
missing = [f for f in res_files if not (APP / f).exists()]
r.check(f"Resources 的 {len(res_files)} 项都存在", not missing, str(missing))
r.check("Info.plist 不在 Resources（应由 INFOPLIST_FILE 处理）",
        "Info.plist" not in res_files)

# ---- Frameworks / Embed
fw = phase_files("PBXFrameworksBuildPhase")
em = phase_files("PBXCopyFilesBuildPhase")
print("  Frameworks:", fw, " Embed:", em)
r.check("Python.xcframework 已链接", any("Python.xcframework" in p for p in fw))
r.check("Python.xcframework 已 Embed", any("Python.xcframework" in p for p in em),
        "没 Embed 的话运行时会找不到 dylib")

for ph in target.buildPhases:
    obj = proj.objects[ph]
    if obj.isa == "PBXCopyFilesBuildPhase":
        spec = getattr(obj, "dstSubfolderSpec", None)
        r.check(f"Embed 目标是 Frameworks/（spec=10，实际 {spec}）", str(spec) == "10")

# ---- build settings
def setting(name, conf="Release"):
    bcl = proj.objects[target.buildConfigurationList]
    for cfg in bcl.buildConfigurations:
        c = proj.objects[cfg]
        if c.name == conf:
            return getattr(c.buildSettings, name, None)
    return None

for k, expect in {
    "PRODUCT_BUNDLE_IDENTIFIER": "com.signhub.app",
    "INFOPLIST_FILE": "SigninHub/Info.plist",
    "SWIFT_OBJC_BRIDGING_HEADER": "SigninHub/SigninHub-Bridging-Header.h",
    "SWIFT_VERSION": "5.0",
}.items():
    actual = setting(k)
    r.check(f"{k} = {expect}", str(actual) == expect, f"实际 {actual}")

hsp = setting("HEADER_SEARCH_PATHS") or []
hsp_s = " ".join(hsp) if isinstance(hsp, list) else str(hsp)
r.check("HEADER_SEARCH_PATHS 指向 Python headers",
        "Python.framework/Headers" in hsp_s, hsp_s)

for conf in ("Debug", "Release"):
    r.check(f"{conf} 配置绑定完整",
            bool(setting("PRODUCT_BUNDLE_IDENTIFIER", conf)))

# ---- shell script phase
for ph in target.buildPhases:
    obj = proj.objects[ph]
    if obj.isa == "PBXShellScriptBuildPhase":
        s = getattr(obj, "shellScript", "") or ""
        r.check("Shell phase 调用 install_python.sh", "install_python.sh" in s, s[:80].strip())

sys.exit(r.finish())
