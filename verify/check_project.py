#!/usr/bin/env python3
"""工程一致性与接线检查。

回答一个问题：**Xcode 打开这个工程、点 Build，会不会因为「引用了不存在的东西」
而在某个环节炸掉？** 这类错误在 macOS 上要等到 xcodebuild 才暴露。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from _common import APP, IOS, PBX, ROOT, PYVER, Report, XCFW, require

r = Report("工程一致性与接线")

if not require(PBX, APP / "main.m", APP / "Info.plist"):
    sys.exit(1)

# 允许「只有源码、还没 fetch 运行时」的状态：那属于环境未就绪，
# 不是工程错误。缺 Python.xcframework 时跳过相关断言。
HAS_RUNTIME = (XCFW / "lib").is_dir()

text = PBX.read_text(encoding="utf-8")

# ---- 1) 括号平衡（pbxproj 是 OpenStep plist，结构靠括号）
for o, c, label in (("{", "}", "花括号"), ("(", ")", "圆括号")):
    a, b = text.count(o), text.count(c)
    r.check(f"pbxproj {label}平衡（{a}）", a == b, f"{o}={a} {c}={b}")

# ---- 2) 对象 ID 唯一性与引用完整性
defined = set(re.findall(r"^\t\t(AA[0-9A-F]{22})\b", text, re.M))
referenced = set(re.findall(r"\b(AA[0-9A-F]{22})\b", text))
r.check(f"对象 ID 无悬空引用（定义 {len(defined)}）",
        referenced <= defined, str(sorted(referenced - defined)[:5]))
r.check("对象 ID 无重复定义",
        len(re.findall(r"^\t\t(AA[0-9A-F]{22})\b", text, re.M)) == len(defined))

# ---- 3) 源文件全部存在且注册
sources_in_pbx = set(re.findall(r"/\* (\S+\.(?:swift|m)) in Sources \*/", text))
on_disk = {p.name for p in APP.iterdir() if p.suffix in (".swift", ".m")}
r.check(f"源文件全部参与编译（{len(on_disk)} 个）",
        sources_in_pbx == on_disk,
        f"未注册={sorted(on_disk - sources_in_pbx)} 幽灵={sorted(sources_in_pbx - on_disk)}")
for name in sorted(on_disk):
    r.check(f"{name} 有 PBXFileReference",
            f"/* {name} */ = {{isa = PBXFileReference" in text)

# ---- 4) 关键文件
must = {
    "main.m": APP / "main.m",
    "Info.plist": APP / "Info.plist",
    "Bridging header": APP / "SigninHub-Bridging-Header.h",
    "Assets.xcassets": APP / "Assets.xcassets",
    "AppIcon": APP / "Assets.xcassets/AppIcon.appiconset/icon-1024.png",
    "cacert.pem": APP / "cacert.pem",
    "app/": APP / "app",
    "app_packages/": APP / "app_packages",
    "install_python.sh": ROOT / "scripts" / "install_python.sh",
}
if HAS_RUNTIME:
    must["Python.xcframework"] = XCFW
else:
    print("  (跳过 Python.xcframework —— 还没跑 scripts/fetch_python.sh)")
for label, p in must.items():
    r.check(f"存在 {label}", p.exists(), str(p))

# ---- 5) app/ 里的后端
for rel in ("signbot/server.py", "signbot/runner.py", "signbot/config.py",
            "signbot/vault.py", "signbot/qoder.py", "_ios_boot.py",
            "web/index.html", "web/static/app.js", "web/static/app.css"):
    r.check(f"后端 {rel}", (APP / "app" / rel).is_file())

# ---- 6) 补丁已应用（否则 import signbot 直接 ValueError）
for rel in ("signbot/vault.py", "signbot/qoder.py"):
    t = (APP / "app" / rel).read_text(encoding="utf-8")
    head = t.split("try:")[0] + "\n"
    r.check(f"{rel} 已打 iOS 补丁",
            "iOS 移植补丁" in t and "\nfrom ctypes import wintypes\n" not in head)
r.check("_ios_boot 有 prepare_server",
        "def prepare_server" in (APP / "app" / "_ios_boot.py").read_text(encoding="utf-8"))

# ---- 7) Python 标准库与扩展模块
if HAS_RUNTIME:
    for p in (XCFW / "lib" / PYVER / "os.py",
              XCFW / "lib" / PYVER / "http" / "server.py",
              XCFW / "ios-arm64" / "lib-arm64" / PYVER / "lib-dynload",
              XCFW / "ios-arm64" / "Python.framework" / "Python",
              XCFW / "ios-arm64" / "Python.framework" / "Headers" / "Python.h",
              XCFW / "build" / "iOS-dylib-Info-template.plist"):
        r.check(f"runtime {p.name}", p.exists(), str(p.relative_to(ROOT)))
    # 关键扩展模块必须存在（sqlite/ssl/socket 缺一个后端就残）
    dyn = XCFW / "ios-arm64" / "lib-arm64" / PYVER / "lib-dynload"
    for m in ("_sqlite3", "_socket", "_ssl", "_hashlib", "_json"):
        hits = list(dyn.glob(f"{m}.cpython*.so")) if dyn.is_dir() else []
        r.check(f"扩展模块 {m}", bool(hits), str(dyn.relative_to(ROOT)))
else:
    print("  (跳过标准库与扩展模块 —— 需要 ios/Python.xcframework)")

# ---- 8) 入口一致性
mainm = (APP / "main.m").read_text(encoding="utf-8")
appd = (APP / "AppDelegate.swift").read_text(encoding="utf-8")
r.check("main.m 调用 UIApplicationMain",
        "UIApplicationMain" in mainm and '"SigninHubAppDelegate"' in mainm)
r.check("AppDelegate 类名与 main.m 一致",
        "@objc(SigninHubAppDelegate)" in appd)
r.check("无 @main 冲突（会与 C main() 撞）",
        not re.search(r"^\s*@main\b", appd, re.M))
r.check("main.m 接入 _ios_boot", "_ios_boot" in mainm)
r.check("main.m 调 prepare_server", "prepare_server" in mainm)
r.check("main.m 释放 GIL（否则后台唤醒死锁）",
        "PyEval_SaveThread" in mainm and "PyEval_RestoreThread" in mainm)

# 只看非注释代码：注释里出现 use_system_logger 是说明文字，不算错误
_code_lines = [ln for ln in mainm.splitlines()
               if not ln.lstrip().startswith(("//", "/*", "*", "#"))]
_code = "\n".join(_code_lines)
r.check("main.m 不设置 use_system_logger（3.14 才有）",
        "use_system_logger" not in _code)

# ---- 9) Info.plist 必需键
plist = (APP / "Info.plist").read_text(encoding="utf-8")
for key in ("CFBundleIdentifier", "CFBundleExecutable", "NSAppTransportSecurity",
            "UILaunchScreen", "UISupportedInterfaceOrientations",
            "BGTaskSchedulerPermittedIdentifiers", "UIBackgroundModes"):
    r.check(f"Info.plist 含 {key}", key in plist)

# ---- 10) BGTask identifier 与 Swift 常量一致
sched = (APP / "CheckinScheduler.swift").read_text(encoding="utf-8")
r.check("BGTask id 在 plist 与 Swift 中一致",
        "com.signhub.app.refresh" in plist and "com.signhub.app.refresh" in sched)

# ---- 11) 图标尺寸
icon = APP / "Assets.xcassets/AppIcon.appiconset/icon-1024.png"
if icon.is_file():
    d = icon.read_bytes()
    ok_png = d[:8] == b"\x89PNG\r\n\x1a\n"
    w = int.from_bytes(d[16:20], "big") if ok_png else 0
    h = int.from_bytes(d[20:24], "big") if ok_png else 0
    r.check(f"AppIcon 1024x1024（实际 {w}x{h}）", (w, h) == (1024, 1024))

sys.exit(r.finish())
