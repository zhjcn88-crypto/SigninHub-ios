#!/usr/bin/env python3
"""安装包结构检查：复刻 Xcode 构建产物，断言所有运行时路径成立。

C 代码与 Python 引导里写死了 python/lib/python3.13、app/、cacert.pem 等路径。
这些在 Windows 上跑不了 xcodebuild，但可以用文件系统精确复刻 .app 结构，
再逐项断言 —— 这是「构建成功但运行时找不到文件」的最后防线。
"""
from __future__ import annotations

import shutil
import sys

from _common import APP, PYVER, Report, TMP, XCFW, require

r = Report("安装包结构")

if not require(XCFW / "lib", APP / "app" / "signbot" / "server.py"):
    sys.exit(1)

BUNDLE = TMP / "payload" / "Payload" / "SigninHub.app"
if BUNDLE.parent.parent.exists():
    shutil.rmtree(BUNDLE.parent.parent)
BUNDLE.mkdir(parents=True)

print("  → 复刻 install_python.sh")
shutil.copytree(XCFW / "lib", BUNDLE / "python" / "lib", dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("libpython*.dylib"))
if (XCFW / "ios-arm64" / "lib-arm64").is_dir():
    shutil.copytree(XCFW / "ios-arm64" / "lib-arm64", BUNDLE / "python" / "lib",
                    dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("libpython*.dylib"))

stdlib = BUNDLE / "python" / "lib" / PYVER
r.check("标准库已装", stdlib.is_dir())
for junk in ("test", "idlelib", "turtledemo", "tkinter", "ensurepip", "lib2to3"):
    shutil.rmtree(stdlib / junk, ignore_errors=True)

# .so → .framework（严格按 install_python.sh 的语义）
dynload_rel = f"python/lib/{PYVER}/lib-dynload"
dyn = BUNDLE / dynload_rel
converted = []
for so in sorted(dyn.glob("*.so")):
    rel = so.relative_to(BUNDLE).as_posix()
    base = dynload_rel + "/"
    if not rel.startswith(base):
        r.check(f"{so.name} 在 lib-dynload 下", False, rel)
        continue
    mod = rel[len(base):].split(".")[0].replace("/", ".")
    fw_folder = f"Frameworks/{mod}.framework"
    fw = BUNDLE / fw_folder
    fw.mkdir(parents=True, exist_ok=True)

    tpl = (XCFW / "build" / "iOS-dylib-Info-template.plist").read_text(encoding="utf-8")
    out = tpl.replace("<string></string>", f"<string>{mod}</string>", 1)
    out = out.replace("<string></string>", f"<string>com.signhub.app.{mod}</string>", 1)
    (fw / "Info.plist").write_text(out, encoding="utf-8")

    shutil.move(str(so), str(fw / mod))
    (dyn / f"{mod}.fwork").write_text(f"{fw_folder}/{mod}")
    (fw / f"{mod}.origin").write_text(f"{rel[:-3]}.fwork")
    converted.append(mod)

r.check(f"扩展模块转换（{len(converted)} 个）", len(converted) > 60)
r.check("模块名为裸名（无 lib-dynload 前缀）",
        not [m for m in converted if "." in m or "/" in m], str(converted[:3]))
r.check("无 .so 残留", not list(dyn.glob("*.so")))

# 关键模块
for m in ("_sqlite3", "_socket", "_ssl", "_hashlib", "_json", "binascii", "zlib"):
    fw = BUNDLE / f"Frameworks/{m}.framework"
    r.check(f"{m} 就绪",
            (fw / m).is_file() and (fw / "Info.plist").is_file()
            and (dyn / f"{m}.fwork").is_file())

# .fwork 内容必须指向真实存在的二进制
bad = []
for fwork in dyn.glob("*.fwork"):
    if not (BUNDLE / fwork.read_text().strip()).is_file():
        bad.append(fwork.name)
r.check(f"所有 .fwork 指向存在的二进制（{len(list(dyn.glob('*.fwork')))} 个）",
        not bad, str(bad[:3]))

# 资源拷贝
print("  → 复刻资源拷贝")
shutil.copytree(APP / "app", BUNDLE / "app")
shutil.copytree(APP / "app_packages", BUNDLE / "app_packages", dirs_exist_ok=True)
shutil.copy2(APP / "cacert.pem", BUNDLE / "cacert.pem")
shutil.copy2(APP / "Info.plist", BUNDLE / "Info.plist")
shutil.copytree(XCFW / "ios-arm64" / "Python.framework",
                BUNDLE / "Frameworks" / "Python.framework")
(BUNDLE / "SigninHub").write_bytes(b"\xcf\xfa\xed\xfe")
(BUNDLE / "Assets.car").write_bytes(b"BOMStore\x00\x00\x00\x02")
icon = APP / "Assets.xcassets/AppIcon.appiconset/icon-1024.png"
for nm in ("AppIcon60x60@2x.png", "AppIcon60x60@3x.png"):
    shutil.copy2(icon, BUNDLE / nm)

# ---- 运行时路径断言（对应 main.m 的 module_search_paths 与 _ios_boot）
print("  → 断言运行时路径")
python_home = BUNDLE / "python"
for p in (python_home / "lib" / PYVER,
          python_home / "lib" / PYVER / "lib-dynload",
          BUNDLE / "app",
          BUNDLE / "app_packages"):
    r.check(f"PYTHONPATH {p.relative_to(BUNDLE)}", p.is_dir())

r.check("_ios_boot 首选 CA 路径", (BUNDLE / "cacert.pem").is_file())
for mod in ("os.py", "json/__init__.py", "http/server.py", "sqlite3",
            "ssl.py", "urllib/request.py", "encodings/__init__.py"):
    r.check(f"stdlib {mod}", (stdlib / mod).exists())

# ---- 真的用这份 app/ 起一次服务
print("  → 用这份 app/ 启动后端")
import json  # noqa: E402
import os  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import urllib.request  # noqa: E402

data = TMP / "bundle_data"
shutil.rmtree(data, ignore_errors=True)
os.environ["SIGNBOT_DATA"] = str(data)
os.environ["SSL_CERT_FILE"] = str(BUNDLE / "cacert.pem")
sys.path.insert(0, str(BUNDLE / "app"))

import _ios_boot  # noqa: E402
from signbot import server as S  # noqa: E402

_ios_boot.prepare_server()
PORT = 18960
threading.Thread(target=lambda: S.serve(host="127.0.0.1", port=PORT),
                 daemon=True).start()

# 不能固定 sleep：CI 的 runner 上解释器 import + 建库 + 绑端口明显更慢，
# 固定 3 秒会假失败。改成轮询直到 /api/ping 通（最多 40 秒）。
import urllib.request as _ur  # noqa: E402

def _wait_ready(timeout=40.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with _ur.urlopen(f"http://127.0.0.1:{PORT}/api/ping", timeout=3) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False

r.check("后端在 40 秒内就绪", _wait_ready())

for p in ("/api/ping", "/api/status", "/"):
    try:
        with _ur.urlopen(f"http://127.0.0.1:{PORT}{p}", timeout=10) as resp:
            st = resp.status
    except Exception as e:
        st = f"ERR:{e}"
    r.check(f"模拟包内 {p} 可用", st == 200, str(st))

sys.exit(r.finish())
