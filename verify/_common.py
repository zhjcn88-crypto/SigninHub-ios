#!/usr/bin/env python3
"""工程自检用的公共路径与工具。所有检查脚本都从这里取路径，
保证「在仓库任意位置 clone 下来都能跑」，不依赖绝对路径。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# SIGNINHUB_ROOT 由 run_all.py 注入；单独跑某个脚本时按文件位置推断
_env_root = os.environ.get("SIGNINHUB_ROOT")
ROOT = Path(_env_root).resolve() if _env_root else Path(__file__).resolve().parent.parent
IOS = ROOT / "ios"
APP = IOS / "SigninHub"
PBX = IOS / "SigninHub.xcodeproj" / "project.pbxproj"
XCFW = IOS / "Python.xcframework"
SCRIPTS = ROOT / "scripts"
PYVER = "python3.13"

# 检查过程产生的临时目录放这里，避免污染仓库
TMP = Path(os.environ.get("SIGNINHUB_TMP") or (ROOT / ".verify_tmp"))


class Report:
    """收集 PASS/FAIL，最后统一输出，避免中断式断言让排查变难。"""

    def __init__(self, title: str):
        self.title = title
        self.ok: list[str] = []
        self.errors: list[str] = []
        print("=" * 68)
        print(title)
        print("=" * 68)

    def check(self, name: str, cond, detail: str = "") -> bool:
        if cond:
            self.ok.append(name)
            print(f"  PASS  {name}")
        else:
            self.errors.append(f"{name}" + (f"  [{detail}]" if detail else ""))
            print(f"  FAIL  {name}" + (f"  [{detail}]" if detail else ""))
        return bool(cond)

    def finish(self) -> int:
        print("-" * 68)
        print(f"共 {len(self.ok) + len(self.errors)} 项，失败 {len(self.errors)}")
        if self.errors:
            for e in self.errors:
                print("  FAILED:", e)
            return 1
        print("OK")
        return 0


def require(*paths: Path) -> bool:
    """前置条件：这些路径必须存在，否则直接给出可读原因。"""
    missing = [p for p in paths if not p.exists()]
    if missing:
        print("缺少必需文件，无法继续：")
        for m in missing:
            print("  -", m)
        print("\n请先运行:")
        print("  ./scripts/fetch_python.sh")
        print("  ./scripts/prepare_app.sh")
        return False
    return True
