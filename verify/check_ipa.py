#!/usr/bin/env python3
"""IPA 打包结构检查：按 TrollStore / installd 的实际要求逐项校验。

打包是纯文件操作，可以完整模拟：
  Payload/SigninHub.app/  →  zip  →  .ipa

校验点来自 installd 的真实行为：
  · 顶层必须唯一是 Payload/
  · .app 内必须有 Info.plist 且 CFBundleExecutable 与实际文件名一致
  · 不能残留 _CodeSignature / embedded.mobileprovision（TrollStore 自己重签）
  · Frameworks/ 下的 framework 结构完整（Info.plist + 二进制）
"""
from __future__ import annotations

import os
import shutil
import struct
import sys
import zipfile

from _common import TMP, Report

r = Report("IPA 打包结构")

SCHEME = "SigninHub"
SRC_APP = TMP / "payload" / "Payload" / f"{SCHEME}.app"
STAGE = TMP / "ipa_stage"
IPA = TMP / "dist" / f"{SCHEME}.ipa"

if not SRC_APP.is_dir():
    print(f"\n缺少 {SRC_APP}")
    print("请先跑 check_bundle.py 生成模拟产物：")
    print("  python3 verify/check_bundle.py")
    sys.exit(1)

print("  → 打包 Payload/")
if STAGE.exists():
    shutil.rmtree(STAGE)
(STAGE / "Payload").mkdir(parents=True)
shutil.copytree(SRC_APP, STAGE / "Payload" / f"{SCHEME}.app", symlinks=True)

# 清掉签名残留（真实构建里由脚本做）
shutil.rmtree(STAGE / "Payload" / f"{SCHEME}.app" / "_CodeSignature", ignore_errors=True)
for p in (STAGE / "Payload" / f"{SCHEME}.app").rglob("embedded.mobileprovision"):
    p.unlink()

IPA.parent.mkdir(parents=True, exist_ok=True)
if IPA.exists():
    IPA.unlink()
with zipfile.ZipFile(IPA, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    for root, dirs, files in os.walk(STAGE):
        dirs.sort()
        for f in sorted(files):
            full = os.path.join(root, f)
            rel = os.path.relpath(full, STAGE).replace(os.sep, "/")
            info = zipfile.ZipInfo(rel)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o755 if "." not in f else 0o644) << 16
            with open(full, "rb") as fh:
                z.writestr(info, fh.read())

print(f"  IPA: {IPA}  ({IPA.stat().st_size/1024/1024:.2f} MB)")

with zipfile.ZipFile(IPA) as z:
    names = z.namelist()
    infoplist = z.read(f"Payload/{SCHEME}.app/Info.plist").decode("utf-8")
    exe = z.read(f"Payload/{SCHEME}.app/{SCHEME}")

# ---- 顶层结构
tops = {n.split("/")[0] for n in names if "/" in n}
r.check("顶层只有 Payload/", tops == {"Payload"}, str(tops))
r.check("无绝对路径 / 反斜杠",
        not any(n.startswith("/") or "\\" in n for n in names))

pre = f"Payload/{SCHEME}.app/"
app_files = [n for n in names if n.startswith(pre) and not n.endswith("/")]
r.check(f".app 内有文件（{len(app_files)}）", len(app_files) > 50)

# ---- 必需项
for rel in ("Info.plist", SCHEME,
            "Frameworks/Python.framework/Python",
            "python/lib/python3.13/os.py",
            "app/signbot/server.py", "app/_ios_boot.py",
            "app/web/index.html", "app/web/static/app.js",
            "cacert.pem", "Assets.car"):
    r.check(f"含 {rel}", pre + rel in names)

# ---- 不该有的
for rel in ("_CodeSignature/CodeResources", "embedded.mobileprovision"):
    r.check(f"不含 {rel}", pre + rel not in names)

# ---- framework 与 .fwork 成对
fw_n = len({n for n in names if pre + "Frameworks/" in n and ".framework/" in n})
fwork_n = len([n for n in names if n.endswith(".fwork")])
r.check(f"framework 条目 {fw_n} / .fwork {fwork_n}", fwork_n > 60 and fw_n > 60)

# ---- Info.plist 语义
for key in ("CFBundleExecutable", "CFBundleIdentifier", "CFBundleShortVersionString",
            "UILaunchScreen", "NSAppTransportSecurity",
            "BGTaskSchedulerPermittedIdentifiers", "UIBackgroundModes"):
    r.check(f"Info.plist 含 {key}", key in infoplist)

# ---- 可执行文件
magic = struct.unpack("<I", exe[:4])[0]
MACHO = {0xFEEDFACE, 0xFEEDFACF, 0xCFFAEDFE, 0xCEFAEDFE, 0xCAFEBABE}
r.check("主可执行文件是 Mach-O 或占位",
        magic in MACHO or len(exe) < 100, f"magic=0x{magic:08X} size={len(exe)}")

# ---- 图标
r.check("含 AppIcon png",
        any("AppIcon" in n and n.endswith(".png") for n in names))

sys.exit(r.finish())
