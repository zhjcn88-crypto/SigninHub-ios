#!/usr/bin/env python3
"""工程自检套件的统一入口。

    python3 verify/run_all.py

区分三种结果，避免"环境没准备好"被误判成"工程坏了"：
    ✓ 全部通过          → 退出码 0
    ⚠ 前置条件未满足    → 退出码 0（打印怎么补齐）
    ✗ 真正的检查失败    → 退出码 1
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

SUITES = [
    ("工程一致性（文件/入口/pbxproj 平衡）", "check_project.py", False),
    ("pbxproj 语义（真 OpenStep 解析器）", "check_pbxproj.py", False),
    ("C ↔ Python 调用契约", "check_contract.py", False),
    ("shell 脚本静态审查", "check_shell.py", False),
    ("后端端到端（HTTP/API/CSRF）", "check_backend_e2e.py", False),
    ("安装包结构（路径与扩展模块）", "check_bundle.py", True),
    ("IPA 打包结构（TrollStore 要求）", "check_ipa.py", True),
]

# 这些脚本依赖 python 运行时；没有 xcframework 时属于「环境未就绪」
NEEDS_XCFRAMEWORK = {"check_bundle.py", "check_ipa.py"}

MISSING_DEPS_HINT = (
    "\n需要先补齐 Python 运行时与 app/：\n"
    "  ./scripts/fetch_python.sh     # 下载 CPython for iOS（约 34 MB）\n"
    "  ./scripts/prepare_app.sh      # 生成 app/（会自动拉上游）\n"
    "然后重跑 verify/run_all.py\n"
)


def has_xcframework() -> bool:
    return (ROOT / "ios" / "Python.xcframework" / "lib").is_dir()


def main() -> int:
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
           "SIGNINHUB_ROOT": str(ROOT)}
    xc_ready = has_xcframework()

    fails = []
    skipped = []

    for title, script, needs_xc in SUITES:
        path = HERE / script
        if not path.is_file():
            skipped.append((title, "脚本缺失"))
            print(f"\n[skip] {script} 不存在")
            continue
        if needs_xc and not xc_ready:
            skipped.append((title, "需要 Python 运行时"))
            print(f"\n[skip] {title}\n        需要 ios/Python.xcframework")
            continue

        print(f"\n{'='*68}\n▸ {title}\n  {script}\n{'='*68}")
        r = subprocess.run([sys.executable, "-X", "utf8", str(path)],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, cwd=str(ROOT))
        out = (r.stdout or "").strip().splitlines()
        for line in out[-8:]:
            print("   ", line.rstrip())
        if r.returncode != 0:
            fails.append(title)
            if r.stderr:
                tail = r.stderr.strip()[-400:]
                print("    STDERR:", tail)

    print("\n" + "=" * 68)
    if fails:
        print(f"✗ {len(fails)}/{len(SUITES)} 项失败")
        for t in fails:
            print("   -", t)
        if not xc_ready:
            print(MISSING_DEPS_HINT)
        return 1
    if skipped:
        print(f"✓ 已跑的检查全部通过；{len(skipped)} 项跳过")
        for t, why in skipped:
            print(f"   - {t}（{why}）")
        if not xc_ready:
            print(MISSING_DEPS_HINT)
        return 0
    print(f"✓ 全部 {len(SUITES)} 项通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
