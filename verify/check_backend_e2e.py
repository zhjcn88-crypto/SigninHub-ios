#!/usr/bin/env python3
"""后端端到端：真的把 app/ 跑起来，用 HTTP 打一遍 WebView 会发的请求。

这一步最有价值——它证明「iOS 版 app/ 目录里的 Python 代码能在
127.0.0.1 上起服务，且内置页面不用口令就能用」。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request

from _common import APP, PYVER, Report, TMP, require

APP_DIR = APP / "app"
r = Report("后端端到端")

if not require(APP_DIR / "signbot" / "server.py", APP_DIR / "_ios_boot.py"):
    sys.exit(1)

TMP.mkdir(parents=True, exist_ok=True)
DATA = TMP / "backend_data"
if DATA.exists():
    shutil.rmtree(DATA)

os.environ["SIGNBOT_DATA"] = str(DATA)
sys.path.insert(0, str(APP_DIR))

import _ios_boot  # noqa: E402

print("  bootstrap:", json.dumps(_ios_boot.INFO, ensure_ascii=False))

from signbot import server as S  # noqa: E402

_ios_boot.prepare_server()

PORT = 18901
BASE = f"http://127.0.0.1:{PORT}"
threading.Thread(target=lambda: S.serve(host="127.0.0.1", port=PORT),
                 daemon=True).start()


def wait_ready(timeout=40.0):
    """轮询直到服务就绪。固定 sleep 在 CI runner 上会假失败。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{BASE}/api/ping", timeout=3) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


r.check("后端在 40 秒内就绪", wait_ready())


def call(path, method="GET", body=None, headers=None):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
    )
    if body is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# ---- 1) 静态资源（WebView 首屏）
for path, marker in [
    ("/", b"<!DOCTYPE html>"),
    ("/manifest.webmanifest", b"name"),
    ("/sw.js", b"self"),
    ("/static/app.css", b"--"),
    ("/static/app.js", b"signhub"),
    ("/static/icon-192.png", b"\x89PNG"),
    ("/favicon.ico", b"\x89PNG"),
]:
    st, body = call(path)
    r.check(f"{path} 可用", st == 200 and marker in body, f"status={st} len={len(body)}")

# ---- 2) 只读 API（本机来源、无口令）
for path in ("/api/ping", "/api/status", "/api/config", "/api/logs", "/api/stats"):
    st, body = call(path)
    parsed = False
    try:
        json.loads(body)
        parsed = True
    except Exception:
        pass
    r.check(f"{path} 返回 JSON", st == 200 and parsed, f"status={st} {body[:60]!r}")
    if path == "/api/ping":
        r.check("/api/ping ok=true", json.loads(body).get("ok") is True)

# ---- 3) 写操作带本机 Origin 应通过
st, _ = call("/api/config", "POST",
             {"scheduler": {"enabled": True, "times": ["09:15"]}},
             {"Origin": BASE})
r.check("POST /api/config 成功", st == 200, f"status={st}")
_, body = call("/api/config")
r.check("配置写入生效",
        json.loads(body).get("scheduler", {}).get("times") == ["09:15"])

# ---- 4) 跨站必须被拒（安全边界，不能为了好用而放开）
st, _ = call("/api/config", "POST", {"scheduler": {"times": ["10:00"]}},
             {"Origin": "https://evil.example"})
r.check("跨站 POST 被拒", st in (401, 403), f"status={st}")

# ---- 5) 数据目录与口令
r.check("DATA_DIR 用环境变量", str(S.DATA_DIR).startswith(str(DATA)), str(S.DATA_DIR))
cfg_path = DATA / "config.json"
r.check("config.json 已生成", cfg_path.is_file())
r.check("sqlite 已建库", (DATA / "signbot.db").is_file())
if cfg_path.is_file():
    cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    tok = cfg.get("server", {}).get("api_token")
    r.check("api_token 为空（WebView 免口令）", not tok, repr(tok))

# ---- 6) 凭据加密（iOS 走 sealed 通道，不能明文落盘）
from signbot import vault  # noqa: E402
sample = "eyJhbGciOiJIUzI1NiJ9.test-payload.sig"
sealed = vault.protect(sample)
r.check("vault.protect 产生了密文", sealed != sample and ("sealed:" in sealed or "dpapi:" in sealed),
        sealed[:30])
r.check("vault 往返一致", vault.unprotect(sealed) == sample)

# ---- 7) CA 证书可用（HTTPS 签到前提）
import ssl  # noqa: E402
stats = ssl.create_default_context().cert_store_stats()
r.check(f"SSL 加载了证书（{stats.get('x509', 0)} 张）", stats.get("x509", 0) > 50, str(stats))

sys.exit(r.finish())
