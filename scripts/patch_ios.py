#!/usr/bin/env python3
"""iOS 平台补丁：让 backend/ 在非 Windows 环境下可导入。

背景
----
signbot/vault.py 与 signbot/qoder.py 在**模块顶层**执行

    from ctypes import wintypes

非 Windows 平台上该语句会抛

    ValueError: _type_ 'v' not supported

于是 config.py → vault 的导入链整体失败，整个后端无法启动。
iOS 上同样如此（iOS 的 ctypes 不带 wintypes）。

修法
----
把 ctypes / wintypes 改成软导入，模块级结构体仅在 ctypes 可用时定义。
DPAPI 相关函数本来就被 `_available()`（sys.platform == "win32"）挡在门外，
iOS 上永远走 seal（HMAC-CTR + vault.key）兜底，因此不损失任何功能。

用法
----
    python3 patch_ios.py <app_dir>
"""
from __future__ import annotations

import sys
from pathlib import Path

SOFT_IMPORT_VAULT = (
    "import base64\n"
    "import ctypes\n"
    "import hashlib\n"
    "import hmac\n"
    "import os\n"
    "import sys\n"
    "import threading\n"
    "import time\n"
    "from ctypes import wintypes\n",
    "import base64\n"
    "import hashlib\n"
    "import hmac\n"
    "import os\n"
    "import sys\n"
    "import threading\n"
    "import time\n"
    "\n"
    "# iOS 移植补丁：non-Windows 上 ctypes.wintypes 会抛 ValueError（_type_ 'v'），\n"
    "# 而本模块在顶层就要用它定义 _DATA_BLOB。软导入后 iOS 走 seal 兜底路径。\n"
    "try:\n"
    "    import ctypes\n"
    "    from ctypes import wintypes\n"
    "except Exception:  # pragma: no cover - 非 Windows 平台\n"
    "    ctypes = None\n"
    "    wintypes = None\n",
)

VAULT_BLOB = (
    'class _DATA_BLOB(ctypes.Structure):\n'
    '    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]\n'
    "\n"
    "\n"
    "def _blob(data: bytes) -> _DATA_BLOB:\n"
    "    buf = ctypes.create_string_buffer(data, len(data))\n"
    "    return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))\n",
    "if ctypes is not None:  # iOS 移植补丁：无 ctypes 时不定义 DPAPI 结构体\n"
    "    class _DATA_BLOB(ctypes.Structure):\n"
    '        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]\n'
    "\n"
    "    def _blob(data: bytes) -> _DATA_BLOB:\n"
    "        buf = ctypes.create_string_buffer(data, len(data))\n"
    "        return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))\n"
    "else:  # pragma: no cover - 非 Windows 平台\n"
    "    _DATA_BLOB = None\n"
    "\n"
    "    def _blob(data: bytes):\n"
    '        raise OSError("DPAPI 仅在 Windows 可用")\n',
)

SOFT_IMPORT_QODER = (
    "import base64\n"
    "import ctypes\n"
    "import json\n"
    "import os\n"
    "import re\n"
    "import urllib.error\n"
    "import urllib.request\n"
    "import uuid\n"
    "from ctypes import wintypes\n",
    "import base64\n"
    "import json\n"
    "import os\n"
    "import re\n"
    "import urllib.error\n"
    "import urllib.request\n"
    "import uuid\n"
    "\n"
    "# iOS 移植补丁：non-Windows 上 ctypes.wintypes 会抛 ValueError（_type_ 'v'）。\n"
    "try:\n"
    "    import ctypes\n"
    "    from ctypes import wintypes\n"
    "except Exception:  # pragma: no cover - 非 Windows 平台\n"
    "    ctypes = None\n"
    "    wintypes = None\n",
)

QODER_BLOB = (
    "# ── Windows DPAPI ────────────────────────────────────────────────────────────\n"
    "class _DATA_BLOB(ctypes.Structure):\n"
    '    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]\n'
    "\n"
    "\n"
    "def _dpapi_unprotect(blob: bytes) -> bytes:\n"
    '    """CryptUnprotectData：解当前用户 DPAPI 保护的字节。"""\n',
    "# ── Windows DPAPI ────────────────────────────────────────────────────────────\n"
    "if ctypes is not None:  # iOS 移植补丁：无 ctypes 时不定义 DPAPI 结构体\n"
    "    class _DATA_BLOB(ctypes.Structure):\n"
    '        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]\n'
    "else:  # pragma: no cover - 非 Windows 平台\n"
    "    _DATA_BLOB = None\n"
    "\n"
    "\n"
    "def _dpapi_unprotect(blob: bytes) -> bytes:\n"
    '    """CryptUnprotectData：解当前用户 DPAPI 保护的字节。"""\n'
    "    if ctypes is None:\n"
    '        raise OSError("DPAPI 仅在 Windows 可用")\n',
)

PATCHES = {
    "signbot/vault.py": [SOFT_IMPORT_VAULT, VAULT_BLOB],
    "signbot/qoder.py": [SOFT_IMPORT_QODER, QODER_BLOB],
}


def patch_file(path: Path, rel: str) -> None:
    text = path.read_text(encoding="utf-8")
    for old, new in PATCHES[rel]:
        if new in text:
            print(f"  [skip] {rel}")
            continue
        if old not in text:
            raise SystemExit(f"  [FAIL] {rel}: 未找到待替换片段 {old[:50]!r}")
        text = text.replace(old, new, 1)
        print(f"  [ok]   {rel}")
    path.write_text(text, encoding="utf-8")


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: patch_ios.py <app_dir>", file=sys.stderr)
        return 2
    root = Path(sys.argv[1])
    for rel in PATCHES:
        patch_file(root / rel, rel)
    print("✓ 平台补丁已应用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
