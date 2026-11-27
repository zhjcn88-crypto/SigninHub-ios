#!/usr/bin/env python3
"""shell 脚本静态审查。

无法在非 macOS 上执行 bash，但把「在 set -euo pipefail 下会误失败」的
已知坑逐条静态排查，能提前发现绝大多数构建中断。
"""
from __future__ import annotations

import re
import sys

from _common import SCRIPTS, Report

r = Report("shell 脚本静态审查")

files = sorted(SCRIPTS.glob("*.sh"))
r.check("找到 shell 脚本", bool(files), str(SCRIPTS))
if not files:
    sys.exit(r.finish())

for f in files:
    text = f.read_text(encoding="utf-8")
    if not text.startswith("#!/bin/bash"):
        r.check(f"{f.name} shebang 为 bash", False)
        continue
    r.check(f"{f.name} shebang", True)
    r.check(f"{f.name} 有 set -euo pipefail", "set -euo pipefail" in text)
    r.check(f"{f.name} 无 CRLF", "\r" not in text)

    lines = text.splitlines()
    for i, ln in enumerate(lines, 1):
        s = ln.strip()
        if s.startswith("#") or not s:
            continue

        # SIGPIPE：cmd | grep -q / head -1 在 pipefail 下会误判失败
        if re.search(r"\|\s*grep\s+-q", ln):
            guarded = ("set +o pipefail" in text) or re.search(r"\|\|\s*true", ln)
            r.check(f"{f.name}:{i} grep -q 有 pipefail 保护",
                    bool(guarded), s[:70])
        if re.search(r"\|\s*head\s+-n?\s*1", ln):
            guarded = ("set +o pipefail" in ln or "set +o pipefail" in text
                       or re.search(r"\|\|\s*true", ln))
            r.check(f"{f.name}:{i} head -1 有 pipefail 保护",
                    bool(guarded), s[:70])

        # GNU 专有，macOS 上不存在
        if re.search(r"\bstat\s+-c\b|\bdate\s+-d\b|\breadlink\b", ln):
            r.check(f"{f.name}:{i} 未使用 GNU 专有命令", False, s[:70])

        # sed -i 在 macOS 上必须带备份后缀
        if re.search(r"sed\s+-i\s+'", ln):
            r.check(f"{f.name}:{i} sed -i 兼容 macOS", False, s[:70])

# ---- 关键内容存在性
MUST = {
    "install_python.sh": ["CODESIGNING_FOLDER_PATH",
                          "iOS-dylib-Info-template.plist", "install_base",
                          "lib-dynload", ".fwork"],
    "build_unsigned_ipa.sh": ["CODE_SIGNING_ALLOWED=NO", "Payload", "ditto -c -k",
                              "xcodebuild", "showBuildSettings"],
    "fetch_python.sh": ["Python-Apple-support", "Python.xcframework"],
    "prepare_app.sh": ["patch_ios.py", "make_boot.py"],
}
for name, keys in MUST.items():
    p = SCRIPTS / name
    if not p.is_file():
        r.check(f"{name} 存在", False)
        continue
    t = p.read_text(encoding="utf-8")
    for k in keys:
        r.check(f"{name} 含 {k!r}", k in t)

sys.exit(r.finish())
