#!/usr/bin/env python3
"""C（main.m / PythonBridge.m）与 Python 之间的调用契约校验。

C 侧是字符串动态取属性（PyObject_GetAttrString），拼错了编译期发现不了，
要到运行时空指针才崩。这里把所有字面量提取出来，逐个核对真实存在。
"""
from __future__ import annotations

import ast
import re
import sys

from _common import APP, XCFW, PYVER, Report, require

r = Report("C ↔ Python 调用契约")

MAINM = APP / "main.m"
BRIDGE = APP / "PythonBridge.m"
APP_DIR = APP / "app"

if not require(MAINM, BRIDGE, APP_DIR / "signbot" / "server.py"):
    sys.exit(1)

mainm = MAINM.read_text(encoding="utf-8")
bridge = BRIDGE.read_text(encoding="utf-8")
c_code = mainm + "\n" + bridge

# ---- _ios_boot 的公开接口
boot_tree = ast.parse((APP_DIR / "_ios_boot.py").read_text(encoding="utf-8"))
iface = set()
for node in boot_tree.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        iface.add(node.name)
    elif isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name):
                iface.add(t.id)

# ---- C 侧 import 的模块
imported = set(re.findall(r'PyImport_ImportModule\("([^"]+)"\)', c_code))
print("  C import:", sorted(imported))
known_stdlib = {"traceback", "site", "runpy", "json", "sys", "os"}
for m in imported:
    if m in ("_ios_boot", "signbot.server", "signbot.runner"):
        ok = True
        if m == "_ios_boot":
            ok = (APP_DIR / "_ios_boot.py").is_file()
        else:
            sub = m.split(".")[1] + ".py"
            ok = (APP_DIR / "signbot" / sub).is_file()
        r.check(f"C import {m} 存在", ok)
    elif m in known_stdlib:
        r.check(f"C import {m}（标准库）", True)
    else:
        r.check(f"C import {m} 可解析", False, "未知模块")

# ---- 按变量名分别取属性（boot → _ios_boot；mod_fn → runner；mod → server）
print("  boot 取属性:", sorted(set(re.findall(r'PyObject_GetAttrString\(boot,\s*"([^"]+)"\)', c_code))))
for a in set(re.findall(r'PyObject_GetAttrString\(boot,\s*"([^"]+)"\)', c_code)):
    r.check(f"_ios_boot.{a} 存在", a in iface)

# mod 变量在不同函数里指向不同模块（server / runner），逐个按真实出处核对
attr_contexts = re.findall(
    r'PyImport_ImportModule\("([^"]+)"\)(.*?)(?=PyImport_ImportModule|\Z)',
    c_code, re.S)
seen = set()
for modname, body in attr_contexts:
    if not modname.startswith("signbot."):
        continue
    sub = modname.split(".")[1]
    src_path = APP_DIR / "signbot" / f"{sub}.py"
    if not src_path.is_file():
        continue
    src = src_path.read_text(encoding="utf-8")
    for attr in set(re.findall(r'PyObject_GetAttrString\(\w+,\s*"([^"]+)"\)', body)):
        if (modname, attr) in seen:
            continue
        seen.add((modname, attr))
        exists = bool(re.search(rf"^(def {re.escape(attr)}\b|{re.escape(attr)}\s*=)", src, re.M))
        r.check(f"{modname}.{attr} 存在", exists)

# ---- runner.run_all 必须接受 trigger 关键字参数
runner_src = (APP_DIR / "signbot" / "runner.py").read_text(encoding="utf-8")
m = re.search(r"^def run_all\(([^)]*)\)", runner_src, re.M)
r.check("signbot.runner.run_all 存在", bool(m))
if m:
    params = m.group(1)
    r.check("run_all 接受 trigger 参数", "trigger" in params, params)

# ---- serve 签名：C 侧传 (host, port) 两个位置参数
srv = (APP_DIR / "signbot" / "server.py").read_text(encoding="utf-8")
m = re.search(r"^def serve\(([^)]*)\)", srv, re.M)
r.check("signbot.server.serve 存在", bool(m))
if m:
    args = [p.split("=")[0].strip() for p in m.group(1).split(",") if p.strip()]
    r.check(f"serve 接受 ≥2 个位置参数（{args[:2]}）", len(args) >= 2, str(args))

# ---- 桥头文件声明的函数在 .m 里都有定义
hdr = (APP / "SigninHub-Bridging-Header.h").read_text(encoding="utf-8")
decls = set(re.findall(r"\bint\s+(SHB\w+)\s*\(", hdr))
print("  bridging header 声明:", sorted(decls))
for fn in decls:
    r.check(f"{fn} 在 .m 中有实现", re.search(rf"\bint\s+{fn}\s*\(", bridge) is not None)

# ---- Swift 调用的 SHB* 都已声明
swift_files = list(APP.glob("*.swift"))
swift_calls = set()
for f in swift_files:
    swift_calls |= set(re.findall(r"\b(SHB\w+)\s*\(", f.read_text(encoding="utf-8")))
print("  Swift 调用:", sorted(swift_calls))
for fn in swift_calls:
    r.check(f"Swift 调的 {fn} 已在 header 声明", fn in decls)

# ---- Python 路径里的版本号必须与运行时一致
libdirs = []
if (XCFW / "lib").is_dir():
    libdirs = [p.name for p in (XCFW / "lib").iterdir()
               if p.is_dir() and re.fullmatch(r"python3\.\d+", p.name)]
c_vers = set(re.findall(r"python3\.(\d+)", c_code))
print("  实际标准库:", libdirs, " C 代码版本:", sorted(c_vers))
if libdirs:
    actual = {d.replace("python", "") for d in libdirs}
    used = {"3." + v for v in c_vers}
    r.check(f"main.m 的 python 版本与实际一致（{sorted(used)} vs {sorted(actual)}）",
            bool(used & actual))

sys.exit(r.finish())
